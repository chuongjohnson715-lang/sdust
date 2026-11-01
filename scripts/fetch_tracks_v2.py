# -*- coding: utf-8 -*-
"""抓取器 v2：直连 captures 端点拿双轨直链，ffmpeg 直接下完整 mp4。

根因修复（2026-10-26）：
  旧 _get_tracks 靠 pg.on("response") 里调 resp.text() 抓配置，同步 API 在事件
  回调内读响应体会抛异常，被 except 静默吞掉 -> 永远"配置未捕获"。
  调试实测真实端点为：
    http://lms.example.edu:8002/api/embed/lesson-activities/captures/<hash>?lesson_activity_id=<aid>
  其中 <hash> 直接出现在 player URL 路径里 (/lesson-activity/<hash>/detail)。
  故改为：goto player URL（同源 8002）后用 evaluate 直接 fetch 该端点，取
  data.lesson_videos[].download_url，再用 ffmpeg 按直链下完整 mp4。

用法：
  python scripts/fetch_tracks_v2.py --only <活动id1>,<活动id2>
  --course-id <课程id>（用于换课程）  --max-sec 0 全量 / >0 试下
"""
from __future__ import annotations
import argparse, json, re, subprocess, sys, time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from coursereview.config import (load_config, PLAYER_BASE, RAW_ROOT,
                                 safe_name)  # noqa: E402
from coursereview.browser import open_browser, ensure_login        # noqa: E402

FFMPEG = "ffmpeg"
HASH_RE = re.compile(r"/lesson-activity/([0-9a-f]+)/detail")


def _player_url(pg, aid: str) -> str:
    """取播放器 URL。平台在连续请求后会返回非 url 结构（限流/校验），故加重试与诊断。"""
    last = ""
    for i in range(6):
        r = pg.evaluate(
            """async (aid) => {
                 const resp = await fetch(`/api/lessons/${aid}/player-url?from_page=course`,
                                         {credentials:'include'});
                 return {s: resp.status, t: await resp.text()}; }""", aid)
        last = r["t"]
        try:
            j = json.loads(last)
        except Exception:
            j = {}
        u = j.get("url") if isinstance(j, dict) else None
        if u:
            return u
        print(f"  [player-url 第{i+1}次失败] status={r['s']} body={last[:180]}", flush=True)
        time.sleep(6)
    raise RuntimeError(f"取 player-url 失败 {aid}: {last[:300]}")


def _hash_of(purl: str) -> str:
    m = HASH_RE.search(purl or "")
    if m:
        return m.group(1)
    # 兜底：从 JWT 里取
    import base64
    tok = purl.split("token=")[1].split("&")[0]
    p = tok.split(".")[1]; p += "=" * (-len(p) % 4)
    meta = json.loads(base64.urlsafe_b64decode(p))
    return re.search(r"/lesson-activity/([0-9a-f]+)/detail", meta["url"]).group(1)


def get_tracks(pg, purl: str, aid: str) -> dict:
    """捕获 captures 配置响应。

    实测要点（2026-10-26，headless）：
    1) 页面必须**不阻断媒体**且要**点击 8002 frame 里的 video** 才会发起 captures 请求；
       阻断媒体 → 播放器不初始化 → 永远抓不到配置。
    2) Playwright 同步 API 在 response 回调里调 resp.text() 会抛异常（旧 _get_tracks
       就因此永远"配置未捕获"）；正确做法是回调里只存**响应对象**，回调外再读 body。
    3) 页面先 GET /api/v1/auth/embed-token?token=<JWT> 换 access token，
       再带认证请求 /api/embed/lesson-activities/captures/<hash>。
    """
    captured: dict[str, object] = {}

    def on_resp(resp):
        u = resp.url
        if "lesson-activities/captures/" in u:
            captured.setdefault("cfg", resp)

    pg.on("response", on_resp)

    last = "未开始"
    for attempt in range(4):
        try:
            captured.clear()
            pg.goto(purl, wait_until="domcontentloaded", timeout=60000)
            # 点击 8002 frame 里的 video 触发播放器初始化（与成功调试一致）
            for _ in range(4):
                hit = False
                for f in pg.frames:
                    if "lesson-activity" in (f.url or ""):
                        try:
                            f.locator("video").first.click(timeout=3000)
                            hit = True
                        except Exception:
                            pass
                        break
                if "cfg" in captured:
                    break
                time.sleep(6 if hit else 4)
            if "cfg" in captured:
                body = captured["cfg"].text()          # 回调外读取，安全
                d = json.loads(body)
                vids = (d.get("data") or {}).get("lesson_videos") or []
                if vids:
                    return {v["label"]: v for v in vids}
                last = f"响应无 lesson_videos: {str(d)[:200]}"
            else:
                last = "超时未捕获 captures 响应"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(3)
    raise RuntimeError(f"取 captures 配置失败（4 次）：{last}")


def dl(url: str, out: Path, max_sec: int) -> bool:
    out.parent.mkdir(parents=True, exist_ok=True)
    headers = (f"Referer: {PLAYER_BASE}/\r\n"
               f"Origin: {PLAYER_BASE}\r\n")
    base = [FFMPEG, "-hide_banner", "-loglevel", "error", "-icy", "0",
            "-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "-headers", headers, "-i", url]
    for extra in (["-c", "copy"], ["-c:v", "libx264", "-preset", "ultrafast",
                                  "-c:a", "aac", "-b:a", "64k"]):
        cmd = base + extra + (["-t", str(max_sec)] if max_sec > 0 else []) + ["-y", str(out)]
        t0 = time.time()
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=5400)
        except subprocess.TimeoutExpired:
            print("    [warn] 下载超时", flush=True); return False
        if r.returncode == 0 and out.exists() and out.stat().st_size > 1024:
            print(f"    OK {out.name} {out.stat().st_size/1e6:.1f}MB "
                  f"({time.time()-t0:.0f}s, {'copy' if extra[1]=='copy' else 'transcode'})", flush=True)
            return True
        print(f"    [warn] {'copy' if extra[1]=='copy' else 'transcode'} 失败 rc={r.returncode} "
              f"{r.stderr[:200]}", flush=True)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", required=True, help="逗号分隔 activity id")
    ap.add_argument("--manifest", default=str(REPO / ".state" / "manifest.json"))
    ap.add_argument("--out-root", default=str(RAW_ROOT))
    ap.add_argument("--max-sec", type=int, default=0)
    args = ap.parse_args()

    ids = [x.strip() for x in args.only.split(",") if x.strip()]
    man = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    meta = {a["activity_id"]: a for a in man["activities"]}
    course_title = (man.get("course") or {}).get("title", "课程")
    course_title = course_title.split("\n")[0]
    out_root = Path(args.out_root)
    cfg = load_config()

    s = open_browser(cfg, headless=True)
    ok_list, fail_list = [], []
    try:
        if not ensure_login(cfg, s):
            raise SystemExit("登录失败")
        pg = s.context.new_page()
        # 必须先落到课程页（8085 同源），否则相对路径 fetch 无基准 URL
        cid = (man.get("course") or {}).get("course_id")
        if not cid:
            raise SystemExit("清单里缺 course_id，请先运行 discover")
        pg.goto(f"{cfg.lms_base.rstrip('/')}/course/{cid}/learning-activity",
                wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)
        for aid in ids:
            a = meta.get(aid)
            if not a:
                print(f"[skip] {aid} 不在清单", flush=True); continue
            date = a.get("date") or ""
            print(f"\n=== {aid} {date} ===", flush=True)
            # 重置 SPA 状态：实测连续取第二节课的 player-url 会返回非 url 结构
            course_page = f"{cfg.lms_base.rstrip('/')}/course/{cid}/learning-activity"
            try:
                pg.goto(course_page, wait_until="domcontentloaded", timeout=60000)
                time.sleep(3)
            except Exception:
                pass
            purl = None
            for attempt in range(2):
                try:
                    purl = _player_url(pg, aid)
                    break
                except Exception as e:
                    print(f"  [player-url 失败 {attempt+1}/2] {e}", flush=True)
                    # 兜底：换到活动页 hash 路由再取
                    try:
                        pg.goto(f"{course_page}#/{aid}", wait_until="domcontentloaded", timeout=60000)
                        time.sleep(4)
                        purl = _player_url(pg, aid)
                        break
                    except Exception as e2:
                        print(f"  [活动页取 url 也失败] {e2}", flush=True)
                        time.sleep(10)
            if not purl:
                print(f"  [FAIL] 取不到 player-url，跳过 {aid}", flush=True)
                fail_list.append(aid); continue
            try:
                tracks = get_tracks(pg, purl, aid)
            except Exception as e:
                print(f"  [FAIL] {type(e).__name__}: {e}", flush=True)
                fail_list.append(aid); continue
            (out_root / f"_tracks_{aid}.json").write_text(
                json.dumps(tracks, ensure_ascii=False, indent=2), encoding="utf-8")
            instr, enc = tracks.get("INSTRUCTOR"), tracks.get("ENCODER")
            if not instr or not enc:
                print(f"  [FAIL] 缺轨道 {list(tracks)}", flush=True); fail_list.append(aid); continue
            print(f"  轨道: INSTR {instr['size']/1e9:.2f}GB | ENC {enc['size']/1e9:.2f}GB", flush=True)
            d = out_root / safe_name(f"{date}_{aid}") / "extracted" / f"{course_title}_{date}"
            print("  ↓ INSTRUCTOR → 0.mp4", flush=True)
            ok0 = dl(instr["download_url"], d / "0.mp4", args.max_sec)
            print("  ↓ ENCODER → 1.mp4", flush=True)
            ok1 = dl(enc["download_url"], d / "1.mp4", args.max_sec)
            if ok0 and ok1:
                print(f"  [OK] {aid} 双轨就绪 → {d}", flush=True); ok_list.append(aid)
            else:
                print(f"  [PARTIAL] {aid} ok0={ok0} ok1={ok1}", flush=True); fail_list.append(aid)
            time.sleep(12)
    finally:
        s.close()
    print(f"\n=== SUMMARY ok={len(ok_list)} fail={len(fail_list)} ===", flush=True)
    print("OK:", ",".join(ok_list), flush=True)
    print("FAIL:", ",".join(fail_list), flush=True)
    print("ALL_DONE", flush=True)


if __name__ == "__main__":
    main()

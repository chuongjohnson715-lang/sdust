# -*- coding: utf-8 -*-
"""单节录播下载（已实测路径，2026-10-25，示例课程）。

验证过的链路：
  播放页 #/<activity_id>
    → iframe http://lms.example.edu:8002/embed/lms/lesson-activity/<hash>/detail?token=…
    → 悬停 video 让控制条出现
    → 控制条 .mvp-controls-item-wrapper 里「视频源」右边那个**无文字图标**（内层是 <svg>）
      —— 点容器 div 无效，必须点内层 svg
    → 触发 GET …/captures/<hash>/download → 返回 8001/zip/<id>… 的 ZIP
    → 下载文件名形如「<课程名>_<日期>.zip」

坑：
  1. 控制条会自动淡出，悬停后必须**立即**在同一段 JS 里点击，否则点到视频区；
  2. iframe 有时不加载（实测 1/3 概率），需要重载重试；
  3. 平台服务端打包有延迟（历史实测约 50s），等待判据要同时看下载事件与落盘文件。
"""
from __future__ import annotations

import json
import subprocess
import time
import zipfile
from pathlib import Path

from .browser import Session
from .config import Config, DOWNLOAD_DIR, RAW_ROOT, safe_name
from .paths import FFPROBE

PLAYER_HINT = ("8002", "lesson-activity")
MEDIA_EXT = (".mp4", ".mkv", ".flv", ".avi", ".mov", ".ts")
VOLATILE = (".crdownload", ".part", ".tmp")


def wait_player(page, activity_url: str, tries: int = 3, per_try_s: int = 150):
    """等播放器 iframe + video 真正就绪。

    坑（实测）：活动页是同路由不同 hash，SPA 不会真跳转，page.frames 里会残留
    上一节的 8002 frame —— 直接拿它会在 hover 时报「video 不可悬停」。
    所以每次先回 about:blank 清掉旧 frame，再校验 video 可见才返回。
    """
    for i in range(tries):
        try:
            page.goto("about:blank", wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        page.goto(activity_url, wait_until="domcontentloaded", timeout=150000)
        end = time.time() + per_try_s
        while time.time() < end:
            for f in list(page.frames):
                if not all(h in (f.url or "") for h in PLAYER_HINT):
                    continue
                try:
                    v = f.locator("video").first
                    if v.count() and v.is_visible(timeout=1500):
                        time.sleep(2)
                        return f
                except Exception:
                    continue
            time.sleep(2)
        print(f"  [retry {i + 1}/{tries}] 播放器未就绪，重载", flush=True)
    return None


CLICK_DOWNLOAD_JS = r"""
() => {
  const wrap = document.querySelector('.mvp-controls-item-wrapper');
  if (!wrap) return {ok: false, why: 'no controls wrapper'};
  const items = [...wrap.children].map(e => {
    const r = e.getBoundingClientRect();
    return {e, x: r.x, y: r.y, w: r.width, h: r.height,
            text: (e.innerText || '').trim(), cls: (e.className || '').toString()};
  }).filter(o => o.w > 4 && o.h > 4);
  const vs = items.find(o => o.text.includes('视频源'));
  if (!vs) return {ok: false, why: 'no 视频源 item'};
  const cand = items.filter(o => o.x > vs.x + vs.w - 6 && !o.text).sort((a, b) => a.x - b.x);
  const target = cand[0];
  if (!target) return {ok: false, why: 'no icon right of 视频源'};
  // 只点一次：历史踩过「重复点击导致同一节课并发下载两份」的坑。
  const inner = target.e.querySelector('svg,i,button,a,span') || target.e;
  const r = inner.getBoundingClientRect();
  inner.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true,
                                               clientX: r.x + r.width / 2, clientY: r.y + r.height / 2}));
  return {ok: true, x: Math.round(target.x), cls: target.cls.slice(0, 60),
          innerTag: inner.tagName};
}
"""


def _snapshot(dirpath: Path) -> dict[str, tuple[int, float]]:
    if not dirpath.exists():
        return {}
    return {p.name: (p.stat().st_size, p.stat().st_mtime)
            for p in dirpath.iterdir()
            if p.is_file() and p.stat().st_size > 0}


def wait_download_file(before: dict, dirpath: Path, timeout_s: int = 1800,
                       settle_s: int = 20, min_bytes: int = 1024 * 1024) -> Path | None:
    deadline = time.time() + timeout_s
    last_size, stable_since = None, None
    while time.time() < deadline:
        time.sleep(3)
        new = {k: v for k, v in _snapshot(dirpath).items() if k not in before}
        if not new:
            continue
        name, (size, _m) = max(new.items(), key=lambda kv: kv[1][0])
        if size < min_bytes:
            continue
        if name.lower().endswith(VOLATILE):
            stable_since = None
            continue
        if last_size is not None and size == last_size:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= settle_s:
                return dirpath / name
        else:
            stable_since = None
        last_size = size
    new = {k: v for k, v in _snapshot(dirpath).items() if k not in before}
    if new:
        return dirpath / max(new.items(), key=lambda kv: kv[1][0])[0]
    return None


def _run(cmd: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, p.stdout or "", p.stderr or ""


def probe_movie(path: Path) -> dict:
    rc, out, err = _run([str(FFPROBE), "-v", "error", "-show_streams",
                         "-show_format", "-of", "json", str(path)])
    if rc != 0:
        return {"ok": False, "error": err[:300]}
    info = json.loads(out)
    vids = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    auds = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    fmt = info.get("format", {})
    return {
        "ok": bool(vids),
        "n_video": len(vids),
        "n_audio": len(auds),
        "duration_s": float(fmt.get("duration") or 0),
        "size": int(fmt.get("size") or 0),
        "resolution": f"{vids[0].get('width')}x{vids[0].get('height')}" if vids else "",
    }


def unzip_media(zip_path: Path, dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad:
            raise RuntimeError(f"ZIP 校验失败：{bad}")
        for member in zf.infolist():
            if member.filename.endswith("/"):
                continue
            target = (dest / member.filename).resolve()
            if not str(target).startswith(str(dest.resolve())):
                raise RuntimeError(f"ZIP 含路径穿越：{member.filename}")
            zf.extract(member, dest)
            if target.suffix.lower() in MEDIA_EXT:
                out.append(target)
    return sorted(out)


def fetch_activity(session: Session, cfg: Config, act: dict,
                   timeout_s: int = 1800) -> dict:
    """下载一条录播活动，返回结构化结果（不抛异常，失败原因写进 result）。"""
    page = session.page
    aid = act["activity_id"]
    result = {"activity_id": aid, "title": act.get("title", ""), "date": act.get("date", ""),
              "status": "FAILED", "reason": None, "file": None, "media": []}

    t0 = time.time()
    frame = wait_player(page, act["url"])
    if frame is None:
        result["reason"] = "播放器 iframe 未加载（已重试）"
        return result

    # 清空下载目录里同批残留的 .crdownload，避免误判
    before = _snapshot(DOWNLOAD_DIR)
    caught: list = []
    page.on("download", lambda d: caught.append(d))

    hovered = False
    for _ in range(3):
        try:
            frame.locator("video").first.hover(timeout=10000)
            hovered = True
            break
        except Exception:
            time.sleep(3)
    if not hovered:
        result["reason"] = "video 元素不可悬停（播放器未就绪）"
        return result
    time.sleep(0.4)

    clicked = frame.evaluate(CLICK_DOWNLOAD_JS)
    if not clicked.get("ok"):
        result["reason"] = f"未找到下载图标：{clicked.get('why')}"
        return result
    print(f"  已点击下载图标（x={clicked.get('x')}）", flush=True)

    # 优先用 Playwright download 事件（save_as 会阻塞到下载完成）；
    # 兜底才看落盘，且必须排除 .crdownload 半成品、并要求大小稳定 20s。
    got: Path | None = None
    deadline = time.time() + timeout_s
    stable: tuple[str, int] | None = None
    stable_since: float | None = None
    while time.time() < deadline:
        if caught and got is None:
            d = caught[0]
            target = DOWNLOAD_DIR / d.suggested_filename
            try:
                d.save_as(str(target))
                got = target
                break
            except Exception as e:  # noqa: BLE001
                print(f"  [warn] save_as 失败：{e}，继续等落盘", flush=True)
        new = {k: v for k, v in _snapshot(DOWNLOAD_DIR).items() if k not in before}
        done = {k: v for k, v in new.items() if not k.lower().endswith(VOLATILE)}
        if done:
            name, (size, _m) = max(done.items(), key=lambda kv: kv[1][0])
            if size >= 50 * 1024 * 1024:
                if stable == (name, size):
                    if stable_since and time.time() - stable_since >= 20:
                        got = DOWNLOAD_DIR / name
                        break
                else:
                    stable, stable_since = (name, size), time.time()
        time.sleep(3)
    if got is None:
        partial = [f for f in DOWNLOAD_DIR.iterdir() if f.name.lower().endswith(VOLATILE)]
        for f in partial:
            try:
                f.unlink()
            except Exception:
                pass
        result["reason"] = "点击后未等到下载完成（已清理半成品）"
        return result

    act_dir = RAW_ROOT / safe_name(f"{act.get('date') or ''}_{aid}")
    act_dir.mkdir(parents=True, exist_ok=True)
    zip_path = act_dir / got.name
    if got != zip_path:
        got.replace(zip_path)
    result["file"] = str(zip_path)
    result["zip_bytes"] = zip_path.stat().st_size

    try:
        media = unzip_media(zip_path, act_dir / "extracted")
    except Exception as e:  # noqa: BLE001
        result["reason"] = f"解压失败：{e}"
        return result

    metas = [{"path": str(p), **probe_movie(p)} for p in media]
    result["media"] = metas
    result["status"] = "OK" if len(media) >= 2 else ("SINGLE_TRACK" if media else "FAILED")
    if not media:
        result["reason"] = "压缩包内没有视频文件"
    elif len(media) < 2:
        result["reason"] = "只有一路视频（双轨流程会退化）"
    result["elapsed_s"] = round(time.time() - t0, 1)
    return result

# -*- coding: utf-8 -*-
"""单节全流程（WAF 绕行版）：浏览器下载 → 本地提音频/抽帧 → 管线 → 五件套。

背景：平台 WAF 拦非浏览器客户端，ffmpeg/OpenCV 不能直连直链。
所以改为「先用浏览器把两条轨道下到本地，再用本地 ffmpeg/OpenCV 处理」。
本地处理不涉及网络，不受 WAF 影响。

流程：
  1. 浏览器取配置（纯 API，阻断 8804）
  2. 逐音频 chunk 取字节（~34MB）→ 本地合成 16k wav
  3. 只下关键帧（~62MB）→ 本地跳读抽帧 + 像素去重
  4. 调 run_light_lesson.py → 7 阶段管线 → 语义层 → 五件套

断点续跑：light/audio 与 light/slides 已存在则跳过；五件套 ok=true 直接跳过整节。

用法：
  python scripts/run_lesson_browser.py \
      --activity <活动id> --date 2025-09-16 --course "课程名" \
      --course-id <课程id> --deliver-root ./_work/deliver
"""
from __future__ import annotations

import argparse
import asyncio
import os
import json
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from coursereview.config import load_config, RAW_ROOT, STATE_DIR, WORK_ROOT  # noqa: E402
from coursereview.paths import FFMPEG  # noqa: E402
from browser_download import BrowserDownloader  # noqa: E402
import audio_chunks as AC  # noqa: E402
import keyframe_frames as KF  # noqa: E402

PY = os.environ.get("COURSE_REVIEW_PYTHON", sys.executable)
TMP_ROOT = WORK_ROOT / "tmp_dl"
HASH_RE = re.compile(r"/lesson-activity/([0-9a-f]+)/detail")

JS_PURL = """async (aid) => {
    const r = await fetch('/api/lessons/' + aid + '/player-url?from_page=course',
                          {credentials: 'include'});
    return {s: r.status, t: await r.text()};
}"""
JS_EMBED = """async (jwt) => {
    const r = await fetch('/api/v1/auth/embed-token?token=' + encodeURIComponent(jwt),
                          {credentials: 'include'});
    return {s: r.status, t: await r.text()};
}"""
JS_CAP = """async (args) => {
    const h = args[0], aid = args[1], tk = args[2];
    const r = await fetch('/api/embed/lesson-activities/captures/' + h +
                          '?lesson_activity_id=' + aid,
                          {credentials: 'include',
                           headers: {'Authorization': 'Bearer ' + tk}});
    return {s: r.status, t: await r.text()};
}"""


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


async def get_tracks(bd, aid: str, course_id: str, lms_base: str) -> dict:
    pg = bd.page
    await pg.goto(f"{lms_base.rstrip('/')}/course/{course_id}/learning-activity",
                  wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(2)
    r = await pg.evaluate(JS_PURL, aid)
    if r["s"] != 200:
        raise RuntimeError(f"player-url HTTP {r['s']}: {r['t'][:150]}")
    purl = json.loads(r["t"])["url"]
    hsh = HASH_RE.search(purl).group(1)
    jwt = purl.split("token=")[1].split("&")[0]

    async def block(route):
        if ":8804" in route.request.url:
            await route.abort()
        else:
            await route.continue_()

    await pg.route("**/*", block)
    await pg.goto(purl, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(2)
    r2 = await pg.evaluate(JS_EMBED, jwt)
    if r2["s"] != 200:
        raise RuntimeError(f"embed-token HTTP {r2['s']}")
    tok = json.loads(r2["t"])["data"]
    r3 = await pg.evaluate(JS_CAP, [hsh, aid, tok])
    if r3["s"] != 200:
        raise RuntimeError(f"captures HTTP {r3['s']}: {r3['t'][:150]}")
    d = json.loads(r3["t"])
    return {v["label"]: v for v in d["data"]["lesson_videos"]}


def extract_audio(src_mp4: Path, out_wav: Path) -> bool:
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
           "-i", str(src_mp4), "-vn", "-ac", "1", "-ar", "16000",
           "-c:a", "pcm_s16le", str(out_wav)]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=3600)
    return r.returncode == 0 and out_wav.exists() and out_wav.stat().st_size > 10000


def sample_frames(src_mp4: Path, out_dir: Path, step_s: int = 15) -> tuple[int, int]:
    """OpenCV 从本地 mp4 跳读抽帧 + 流式去重。返回 (采样数, 保留页数)。"""
    import cv2
    out_dir.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(src_mp4))
    if not cap.isOpened():
        raise RuntimeError("无法打开本地 ENCODER mp4")
    dur_ms = cap.get(cv2.CAP_PROP_FRAME_COUNT) / max(cap.get(cv2.CAP_PROP_FPS), 1) * 1000
    recs, n, t, idx = [], 0, 0.0, 0
    prev_gray = None
    while t * 1000 < dur_ms:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, fr = cap.read()
        if ok and fr is not None:
            gray = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
            if prev_gray is None:
                keep = True
            else:
                gb = gray
                if prev_gray.shape != gb.shape:
                    gb = cv2.resize(gb, (prev_gray.shape[1], prev_gray.shape[0]))
                d = cv2.absdiff(prev_gray, gb)
                keep = (d > 25).sum() / d.size * 100.0 >= 0.8
            if keep:
                idx += 1
                p = out_dir / f"slide_{idx:03d}.jpg"
                cv2.imwrite(str(p), fr)
                recs.append({"index": idx, "t_rep_s": float(t),
                             "t_rep_hms": f"{int(t)//3600:02d}:{(int(t)%3600)//60:02d}:{int(t)%60:02d}",
                             "image": str(p), "image_rel": f"slides/{p.name}",
                             "image_kb": round(p.stat().st_size / 1024, 1)})
            prev_gray = gray
        n += 1
        t += step_s
    cap.release()
    (out_dir / "slides.json").write_text(json.dumps({
        "source": "browser_download", "sample_step_s": step_s, "n_samples": n,
        "n_slides": len(recs), "slides": recs}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return n, len(recs)


def make_audio_only_page(light: Path) -> tuple[int, float]:
    """只取音频模式：用一张自带说明的占位页满足管线的「媒体文件」输入契约。

    为什么需要占位页：pipeline 以 mp4 为唯一输入，且交付层要求证据层至少有 1 个代表帧。
    占位页上写明「未下载课件轨」，所以它可以出现在 A 册里而不冒充课件截图。
    """
    import wave
    from PIL import Image, ImageDraw, ImageFont

    wav = light / "audio" / "raw_16k.wav"
    with wave.open(str(wav), "rb") as w:
        dur = w.getnframes() / float(w.getframerate() or 16000)

    sd = light / "slides"
    sd.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (1280, 720), (255, 255, 255))
    d = ImageDraw.Draw(img)
    fp = Path(r"C:\Windows\Fonts\msyh.ttc")
    f1 = ImageFont.truetype(str(fp), 46) if fp.exists() else ImageFont.load_default()
    f2 = ImageFont.truetype(str(fp), 26) if fp.exists() else ImageFont.load_default()
    title = "本讲按「只取音频」模式采集"
    notes = [
        "未下载课件轨（ENCODER），A 册无课件证据。",
        f"音频时长 {int(dur)//60} 分 {int(dur)%60} 秒，来源 INSTRUCTOR 音轨逐 chunk 取字节。",
        "本页是占位页，不是课堂截图——知识点与考情只能依据 B/D 册转写。",
    ]
    tw = d.textlength(title, font=f1)
    d.text(((1280 - tw) / 2, 210), title, fill=(28, 28, 30), font=f1)
    y = 320
    for ln in notes:
        w0 = d.textlength(ln, font=f2)
        d.text(((1280 - w0) / 2, y), ln, fill=(110, 110, 115), font=f2)
        y += 54

    p = sd / "slide_001.jpg"
    img.save(p, quality=88)
    (sd / "slides.json").write_text(json.dumps({
        "source": "audio_only_placeholder", "sample_step_s": 0,
        "n_samples": 1, "n_slides": 1, "total_s": round(dur, 3),
        "slides": [{"index": 1, "t_rep_s": 0.0, "t_rep_hms": "00:00:00",
                    "image": str(p), "image_rel": "slides/slide_001.jpg",
                    "image_kb": round(p.stat().st_size / 1024, 1)}],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1, dur


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--activity", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--course", required=True)
    ap.add_argument("--course-id", required=True)
    ap.add_argument("--deliver-root", default=str(WORK_ROOT / "deliver"))
    ap.add_argument("--step", type=int, default=15)
    ap.add_argument("--duration", type=float, default=None,
                    help="课时时长（秒）；不传则按 30fps 从 sample 数推算")
    ap.add_argument("--keep-mp4", action="store_true")
    ap.add_argument("--audio-only", action="store_true",
                    help="不下课件轨（省 62MB/节），A 册只有一张说明占位页")
    a = ap.parse_args()

    tag = f"{a.date}_{a.activity}"
    deliver = Path(a.deliver_root) / tag
    if (deliver / "_自检.json").exists():
        try:
            if json.loads((deliver / "_自检.json").read_text(encoding="utf-8")).get("ok"):
                log(f"{tag} 五件套已存在（ok=true），跳过")
                return 0
        except Exception:
            pass

    light = RAW_ROOT / f"light_{tag}"
    light.mkdir(parents=True, exist_ok=True)
    tmp = TMP_ROOT / tag
    tmp.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    t_all = time.time()

    with BrowserDownloader(workers=8, chunk_mb=8) as bd:
        if not bd.run(bd.ensure_login(cfg)):
            log("登录失败"); return 2

        # 1) 取配置
        try:
            tracks = bd.run(get_tracks(bd, a.activity, a.course_id, cfg.lms_base))
        except Exception as e:
            log(f"取配置失败: {e}"); return 3
        instr, enc = tracks.get("INSTRUCTOR"), tracks.get("ENCODER")
        if not instr or (not enc and not a.audio_only):
            log(f"缺轨道 {list(tracks)}"); return 3
        (light / "tracks.json").write_text(
            json.dumps(tracks, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"轨道 INSTR {instr['size']/1e9:.2f}GB / "
            f"ENC {(enc['size']/1e9 if enc else 0):.2f}GB"
            f"（{'只取音频，不碰课件轨' if a.audio_only else '只取音频 chunk + 关键帧'}，"
            f"不下载完整文件）")

        # 2) 音频：逐 chunk 只取音频字节（~34MB，而非 1.6GB）
        wav = light / "audio" / "raw_16k.wav"
        if wav.exists() and wav.stat().st_size > 10000:
            log(f"  音频已存在（{wav.stat().st_size/1e6:.0f}MB），跳过")
        else:
            log("  ↓ 抽取音频（逐 chunk Range，只下音频字节）...")
            ok, msg = bd.run(AC.fetch_audio_chunks(
                bd.ctx, instr["download_url"], wav, workers=16, log=log))
            log(f"  音频 {'✅' if ok else '❌'} {msg}")
            if not ok:
                return 5

        # 3) 抽帧：只下关键帧（~62MB，而非 2.6GB）；--audio-only 时改为说明占位页
        slides_dir = light / "slides"
        if a.audio_only:
            n, dur = make_audio_only_page(light)
            log(f"  只取音频模式：占位页 1 张（时长 {dur:.0f}s），A 册无课件证据")
        else:
            if (slides_dir / "slides.json").exists():
                try:
                    n0 = json.loads((slides_dir / "slides.json").read_text(encoding="utf-8")).get("n_slides") or 0
                except Exception:
                    n0 = 0
                if n0 > 0:
                    log(f"  抽帧已存在（{n0} 页），跳过")
                    n0 = -1
                else:
                    n0 = 0
            else:
                n0 = 0
            if n0 == 0:
                log(f"  ↓ 抽取关键帧（每 {a.step}s，只下关键帧）...")
                ok2, msg2 = bd.run(KF.sample_keyframes(
                    bd.ctx, enc["download_url"], slides_dir, step_s=a.step,
                    duration_s=a.duration, workers=16, log=log))
                log(f"  抽帧 {'✅' if ok2 else '❌'} {msg2}")
                if not ok2:
                    return 5

    # 6) 管线 + 五件套（流式：全程落单节日志，只在关键行回显，便于并行时看进度）
    log("  跑管线（7 阶段）...")
    lesson_log = STATE_DIR / "logs" / f"{tag}.log"
    lesson_log.parent.mkdir(parents=True, exist_ok=True)
    ECHO_KEYS = ("[RUN ]", "[OK  ]", "[FAIL]", "[SKIP]", "语义层", "五件套", "合成 mp4")
    proc = subprocess.Popen(
        [PY, str(REPO / "scripts" / "run_light_lesson.py"),
         str(light), a.activity, a.date, a.course, str(a.deliver_root)],
        cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1)
    with lesson_log.open("w", encoding="utf-8") as lf:
        for line in proc.stdout:
            lf.write(line)
            lf.flush()
            if any(k in line for k in ECHO_KEYS):
                log("  " + line.rstrip()[:160])
    try:
        proc.wait(timeout=7200)
    except subprocess.TimeoutExpired:
        proc.kill()
        log("  管线超时 7200s，已终止")
        return 7
    ok = False
    if (deliver / "_自检.json").exists():
        try:
            ok = bool(json.loads((deliver / "_自检.json").read_text(encoding="utf-8")).get("ok"))
        except Exception:
            pass
    log(f"{tag} 完成 ok={ok} 总耗时 {(time.time()-t_all)/60:.1f} 分钟")
    return 0 if ok else 6


if __name__ == "__main__":
    sys.exit(main())

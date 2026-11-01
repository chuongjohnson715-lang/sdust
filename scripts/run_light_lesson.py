# -*- coding: utf-8 -*-
"""轻量课程（无完整双轨 mp4）的单节编排：

  轻量产出(音频wav + 抽帧jpg)
    → 合成单条「屏录视频+音频」的 mp4（按真实时间戳铺时间轴）
    → 标准 12 阶段管线（--only 跳过双轨对齐 5 阶段）
    → 语义层 → 五件套

为什么要合成 mp4：主管线以「媒体文件」为唯一输入契约，直接改 pipeline.py 风险高；
合成一条兼容输入可让证据层逻辑零改动复用，产物结构与完整双轨版一致。
代价：幻灯片时间分辨率受轻量采样步长（默认 15s）限制，低于完整双轨的 2s。

用法:
  python scripts/run_light_lesson.py <light_dir> <aid> <date> <course> <deliver_root>
"""
from __future__ import annotations
import json, os, subprocess, sys, time, wave
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from coursereview.config import WORK_ROOT, STATE_DIR      # noqa: E402
from coursereview.paths import FFMPEG, FFPROBE            # noqa: E402
from coursereview import semantic_auto                     # noqa: E402

PY = os.environ.get("COURSE_REVIEW_PYTHON", sys.executable)
TPL = str(REPO / "templates" / "课程复盘五件套" / "assets" / "全课程思维导图.html")


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def build_synth_mp4(light_dir: Path) -> tuple[Path, float]:
    """把抽帧 + 音频合成一条 mp4（屏录画面按时间戳铺开）。返回 (mp4, 时长秒)。"""
    sj = json.loads((light_dir / "slides" / "slides.json").read_text(encoding="utf-8"))
    slides = sj.get("slides") or []
    if not slides:
        raise RuntimeError("无抽帧")
    wav = light_dir / "audio" / "raw_16k.wav"
    if not wav.exists():
        raise RuntimeError("缺音频")
    synth = light_dir / "synth"
    synth.mkdir(parents=True, exist_ok=True)
    mp4 = synth / "0.mp4"
    lst = synth / "concat.txt"
    # 每张图持续到下一张图的时间点；total_s 缺省时回退到音频时长。
    # 单页课件（slides.json 无 total_s）若按「末页 + 15 秒」收尾，
    # 合成 mp4 会被截成十几秒，ASR 只听到开头 → 整节被误判「录播无有效内容」。
    total_s = float(sj.get("total_s") or 0)
    if total_s <= 0:
        with wave.open(str(wav), "rb") as w:
            total_s = w.getnframes() / float(w.getframerate())
    if total_s < 600:
        # 规则：判空课前至少要听完 10–15 分钟，短于 10 分钟只能标「待核实」
        log(f"  ⚠️ 音频仅 {total_s/60:.1f} 分钟（<10 分钟）：不得据此判「无内容」，只能标待核实")
    lines = []
    for i, s in enumerate(slides):
        nxt = slides[i + 1]["t_rep_s"] if i + 1 < len(slides) else (total_s or s["t_rep_s"] + 15.0)
        dur = max(0.5, float(nxt) - float(s["t_rep_s"]))
        p = Path(s["image"]).resolve().as_posix().replace("'", r"'\''")
        lines.append(f"file '{p}'\nduration {dur:.3f}")
    last = Path(slides[-1]["image"]).resolve().as_posix().replace("'", r"'\''")
    lines.append(f"file '{last}'")
    lst.write_text("\n".join(lines), encoding="utf-8")

    # 语料保真：音频 PCM 直通（不重编码，AAC 二次压缩会让 ASR 冒出「师生」等凭空词），
    # 视频无损（qp 0）。合成 mp4 只是管线输入兼容层，不是交付物。
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "concat", "-safe", "0", "-i", str(lst),
           "-i", str(wav),
           "-map", "0:v", "-map", "1:a",
           "-c:v", "libx264", "-preset", "veryfast", "-qp", "0", "-pix_fmt", "yuv420p",
           "-r", "1", "-c:a", "pcm_s16le", "-ar", "16000", "-ac", "1",
           "-shortest", str(mp4)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600)
    if r.returncode != 0 or not mp4.exists():
        raise RuntimeError("合成 mp4 失败: " + r.stderr[:400])
    d = 0.0
    rr = subprocess.run([str(FFPROBE), "-v", "error", "-show_entries", "format=duration",
                         "-of", "csv=p=0", str(mp4)], capture_output=True, text=True)
    try:
        d = float(rr.stdout.strip())
    except Exception:
        pass
    with wave.open(str(wav), "rb") as w:
        wdur = w.getnframes() / float(w.getframerate())
    if d and wdur - d > 5:
        # 单页课件曾按「末页 + 15 秒」收尾，把 110 分钟的课截成 15 秒 → ASR 只听到开头
        raise RuntimeError(f"合成 mp4 被截断：成品 {d:.0f}s < 源音频 {wdur:.0f}s")
    log(f"  合成 mp4 {mp4.stat().st_size/1e6:.0f}MB 时长 {d:.0f}s（{len(slides)} 页，PCM 直通）")
    return mp4, d


def main():
    light_dir = Path(sys.argv[1]); aid, date, course, deliver_root = sys.argv[2:6]
    t0 = time.time()
    try:
        mp4, dur = build_synth_mp4(light_dir)
    except Exception as e:
        log(f"{aid} 合成失败：{e}"); return 2
    win = f"0:{int(dur) + 5}"

    log(f"{aid} 管线开始（仅 7 阶段，跳过双轨对齐）")
    # 流式转印：pipeline 一次多则 20 分钟，capture_output 会让人以为卡死
    proc = subprocess.Popen([PY, str(REPO / "scripts" / "pipeline.py"),
                             "--input-dir", str(mp4.parent), "--window", win,
                             "--only", "probe,audio,asr,slides,ocr,notes,verify",
                             "--keep-seq",
                             "--work-root", str(WORK_ROOT / "data")],
                            cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    lines = []
    for line in proc.stdout:
        lines.append(line.rstrip())
        log("  " + line.rstrip()[:160])
    proc.wait()
    if proc.returncode != 0:
        log(f"{aid} 管线 rc={proc.returncode}: " + " | ".join(lines[-3:])[:400])
    task = None
    for line in lines:
        if "成果目录" in line:
            task = Path(line.split("：", 1)[-1].strip())
    if not task or not task.exists():
        log(f"{aid} 未解析出任务目录"); return 3
    log(f"{aid} 任务目录 {task.name}")

    out = STATE_DIR / "study" / aid
    try:
        semantic_auto.write(task, out, course=course, recorded_at=date)
        log(f"{aid} 语义层规则法完成")
    except Exception as e:
        log(f"{aid} 语义层失败 {type(e).__name__}: {e}")
    rs = subprocess.run([PY, str(REPO / "scripts" / "semantic_review.py"), "all",
                         "--data", str(task), "--out", str(out)],
                        cwd=str(REPO), capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    passed = "?"
    chk = out / "quality_selfcheck.json"
    if chk.exists():
        d = json.loads(chk.read_text(encoding="utf-8"))
        passed = f"{d.get('passed')}/{d.get('total')}"
    log(f"{aid} 语义层校验 {passed} rc={rs.returncode}")

    deliver = Path(deliver_root) / f"{date}_{aid}"
    rd = subprocess.run([PY, str(REPO / "scripts" / "build_deliverables.py"),
                         "--data", str(task), "--study", str(out), "--out", str(deliver),
                         "--course", course, "--template", TPL, "--prefer-single"],
                        cwd=str(REPO), capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    ok = False
    sc = deliver / "_自检.json"
    if sc.exists():
        ok = bool(json.loads(sc.read_text(encoding="utf-8")).get("ok"))
    log(f"{aid} 五件套 ok={ok} rc={rd.returncode} → {deliver}")
    # 回收合成 mp4（音频已入管线，帧已入 slides）
    try:
        mp4.unlink(missing_ok=True)
    except Exception:
        pass
    log(f"{aid} 全程 {time.time()-t0:.0f}s")
    return 0 if ok else 4


if __name__ == "__main__":
    sys.exit(main())

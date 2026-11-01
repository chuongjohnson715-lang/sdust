# -*- coding: utf-8 -*-
"""空课判定：先听完 10–15 分钟再下结论。

规则（2026-10-31 用户口径）：
  - 音频时长 < 10 分钟            → 待核实（**不得**判「无内容」）
  - 前 15 分钟内，1 秒窗 RMS ≥ 30 的语音帧占比 ≥ 5% → 有内容
  - 占比 < 1%                     → 疑似无内容
  - 1% ≤ 占比 < 5%                → 待核实
阈值 30 来自实测：国庆空录 RMS 9–15，正常课 173–652。
老师开头点名、等学生、讲事务，前 60 秒静音是常态，不是没开课。

用法:
  check_lesson_content.py <音频或视频文件> [更多文件...]
  check_lesson_content.py <目录>          # 递归找 *.wav / *.mp4 / *.m4a / *.mkv
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import wave
from array import array
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from coursereview.paths import FFMPEG  # noqa: E402

LISTEN_S = 15 * 60          # 判定窗口：前 15 分钟
MIN_TOTAL_S = 10 * 60       # 短于 10 分钟一律只标待核实
RMS_SPEECH = 30             # 语音段阈值（实测空录 9–15，正常课 173–652）
HI = 0.05                   # 语音帧占比 ≥5% → 有内容
LO = 0.01                   # 语音帧占比 <1%  → 疑似无内容
EXTS = {".wav", ".mp4", ".m4a", ".mkv", ".aac", ".flac"}


def to_pcm16(path: Path, tmpdir: Path) -> Path:
    """统一成 16k 单声道 16bit wav（已是目标格式则原样返回）。"""
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as w:
                if (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2):
                    return path
        except wave.Error:
            pass
    out = tmpdir / (path.stem + "_16k.wav")
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
                        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not out.exists():
        raise RuntimeError(f"解码失败: {path.name}: {r.stderr[:200]}")
    return out


def judge(path: Path, tmpdir: Path) -> dict:
    wav = to_pcm16(path, tmpdir)
    with wave.open(str(wav), "rb") as w:
        sr = w.getframerate()
        total_s = w.getnframes() / float(sr)
        frames_per_s = sr
        blocks: list[float] = []
        read_s = 0.0
        while read_s < min(LISTEN_S, total_s):
            raw = w.readframes(frames_per_s)
            if not raw:
                break
            arr = array("h")
            arr.frombytes(raw)
            if not arr:
                break
            rms = (sum(s * s for s in arr) / len(arr)) ** 0.5
            blocks.append(rms)
            read_s += 1.0

    voiced = sum(1 for r in blocks if r >= RMS_SPEECH)
    ratio = (voiced / len(blocks)) if blocks else 0.0
    if total_s < MIN_TOTAL_S:
        verdict, why = "待核实", f"总时长仅 {total_s/60:.1f} 分钟 < 10 分钟，不足以判空课"
    elif ratio >= HI:
        verdict, why = "有内容", f"前 15 分钟语音帧占比 {ratio:.0%}"
    elif ratio < LO:
        verdict, why = "疑似无内容", f"前 15 分钟语音帧占比 {ratio:.1%}（<1%）"
    else:
        verdict, why = "待核实", f"前 15 分钟语音帧占比 {ratio:.1%}（1%–5%，证据不足）"
    return {"file": path.name, "total_min": total_s / 60, "ratio": ratio,
            "verdict": verdict, "why": why}


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    targets: list[Path] = []
    for a in sys.argv[1:]:
        p = Path(a)
        if p.is_dir():
            targets += sorted(x for x in p.rglob("*") if x.suffix.lower() in EXTS)
        else:
            targets.append(p)
    if not targets:
        print("没有找到音频/视频文件")
        return 2

    bad = 0
    with tempfile.TemporaryDirectory(prefix="cklesson_") as td:
        tmpdir = Path(td)
        for i, p in enumerate(targets, 1):
            try:
                r = judge(p, tmpdir)
            except Exception as e:
                print(f"[{i:02d}] {p.name}  判定失败：{e}")
                bad += 1
                continue
            bad += r["verdict"] == "疑似无内容"
            print(f"[{i:02d}] {r['file']}  {r['total_min']:.1f} 分钟  "
                  f"语音帧 {r['ratio']:.1%}  →  {r['verdict']}（{r['why']}）")
    print(f"\n共 {len(targets)} 个；疑似无内容 {bad} 个"
          f"（「待核实」不等于没课，必须回听后才能下结论）")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

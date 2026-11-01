# -*- coding: utf-8 -*-
"""对本地已有 wav 做分块 ASR（不联网、不下载）。

用于「轻量取数已完成，但合成 mp4 被截断导致管线只转了开头几秒」的讲次补转写。
输入是 `light_*/audio/raw_16k.wav`，输出 `<light>/asr_full/transcript.jsonl|.md`。

用法:
  python scripts/asr_from_wav.py \
      --wav ./_work/raw/light_<日期>_<活动id>/audio/raw_16k.wav \
      --title "第0X讲（<日期> · activity <活动id>）"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

TAG_RE = re.compile(r"<\|[^|]*\|>")

os.environ.setdefault("MODELSCOPE_OFFLINE", "1")
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return x, sr


def write_wav(path: Path, x: np.ndarray, sr: int) -> None:
    y = np.clip(x, -1.0, 1.0)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((y * 32767).astype(np.int16).tobytes())


def hms(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", required=True)
    ap.add_argument("--out", default=None, help="默认 <wav>/../../asr_full")
    ap.add_argument("--title", required=True)
    ap.add_argument("--chunk-s", type=float, default=30.0)
    a = ap.parse_args()

    wav = Path(a.wav)
    out = Path(a.out) if a.out else wav.parent.parent / "asr_full"
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "_chunk.wav"

    from paths import SENSEVOICE_DIR

    x, sr = read_wav(wav)
    dur = len(x) / sr
    step = int(a.chunk_s * sr)
    n = (len(x) + step - 1) // step
    log(f"音频 {dur:.1f}s / {sr}Hz，共 {n} 块")

    from funasr import AutoModel
    log("加载 SenseVoiceSmall ...")
    model = AutoModel(model=str(SENSEVOICE_DIR), trust_remote_code=False,
                      disable_update=True, device="cpu",
                      disable_pbar=True, disable_log=True)

    chunks = []
    t0 = time.perf_counter()
    for i in range(n):
        seg = x[i * step:(i + 1) * step]
        if len(seg) < sr * 0.3:
            continue
        write_wav(tmp, seg, sr)
        try:
            r = model.generate(input=str(tmp), cache={}, language="auto",
                               use_itn=True, batch_size_s=60, merge_vad=False)
            raw = r[0].get("text", "") if r else ""
        except Exception as e:
            raw = f"[ERROR] {e}"
        chunks.append({"idx": i,
                       "start_s": round(i * a.chunk_s, 2),
                       "end_s": round(min((i + 1) * a.chunk_s, dur), 2),
                       "text": TAG_RE.sub("", raw).strip()})
        if (i + 1) % 20 == 0 or i == n - 1:
            log(f"{i + 1}/{n} 块，累计 {time.perf_counter() - t0:.0f}s，"
                f"字符 {sum(len(c['text']) for c in chunks)}")

    tmp.unlink(missing_ok=True)
    with (out / "transcript.jsonl").open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    with (out / "transcript.md").open("w", encoding="utf-8") as f:
        f.write(f"# {a.title} 转写\n\n")
        f.write(f"> 音频 {hms(dur)} ｜ 分块 {a.chunk_s:.0f}s ｜ 块数 {len(chunks)}\n")
        f.write("> SenseVoiceSmall（CPU），30s 块级时间戳，不代表句级起点。\n\n---\n\n")
        for c in chunks:
            if c["text"]:
                f.write(f"[{hms(c['start_s'])}] {c['text']}\n\n")
    log(f"完成：{out / 'transcript.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

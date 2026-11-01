"""双轨音视频独立处理：逐路提取音频、逐路 SenseVoice 转写、逐路落盘。

设计原则（对应任务书第四节）：
- 两路各自独立提取、独立转写、独立落盘，**不做任何跨轨合并**；
- 不预设哪一路更准：选路逻辑在此脚本中不存在，两路一律处理；
- 使用已验证的原始音频方案（**不做降噪**，不加 afftdn/loudnorm）；
- SenseVoiceSmall 不输出置信度，字段如实置 null 并说明，不伪造分数；
- 数字、考试范围、成绩相关片段自动打「待核实」标记。

用法：
    python transcribe_tracks.py --extracted-dir <含 mp4 的目录> --out-dir <output/tracks>
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

from paths import FFMPEG as _FFMPEG, FFPROBE as _FFPROBE, SENSEVOICE_DIR

FFMPEG = Path(_FFMPEG)
FFPROBE = Path(_FFPROBE)
SENSEVOICE = Path(SENSEVOICE_DIR)

CHUNK_S = 30.0
TAG_RE = re.compile(r"<\|[^|]*\|>")
# 需人工复核的信号：数字、考试/成绩/范围相关表述
REVIEW_RE = re.compile(
    r"\d|百分之|考试|考核|成绩|分数|重点|范围|必考|不考|及格|重修|学分|平时|期末|期中")


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run(cmd: list[str], timeout: int = 3600) -> tuple[int, str, str, float]:
    t0 = time.perf_counter()
    p = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, p.stdout, p.stderr, time.perf_counter() - t0


def ffprobe(path: Path) -> dict:
    rc, out, err, dt = run([str(FFPROBE), "-v", "error", "-print_format", "json",
                            "-show_format", "-show_streams", str(path)], timeout=300)
    if rc != 0:
        return {"error": err.strip()[:300]}
    d = json.loads(out)
    fmt = d.get("format", {})
    streams = d.get("streams", [])
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    video = [s for s in streams if s.get("codec_type") == "video"]
    v = video[0] if video else {}
    return {
        "duration_s": float(fmt.get("duration") or 0),
        "size_bytes": int(fmt.get("size") or 0),
        "bit_rate": int(fmt.get("bit_rate") or 0),
        "n_video_streams": len(video),
        "n_audio_streams": len(audio),
        "width": v.get("width"), "height": v.get("height"),
        "vcodec": v.get("codec_name"), "vbitrate": int(v.get("bit_rate") or 0),
        "audio": [{"index": s.get("index"), "codec": s.get("codec_name"),
                   "sample_rate": int(s.get("sample_rate") or 0),
                   "channels": s.get("channels"),
                   "bit_rate": int(s.get("bit_rate") or 0),
                   "layout": s.get("channel_layout")} for s in audio],
    }


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0, sr


def write_wav(path: Path, x: np.ndarray, sr: int):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())


def quality_checks(chunks: list[dict], duration_s: float) -> dict:
    """时间戳范围、空文本、重复、时序四类检查。"""
    n = len(chunks)
    empty = [c["idx"] for c in chunks if not c["text"].strip()]
    oob = [c["idx"] for c in chunks if c["start_s"] < 0 or c["end_s"] > duration_s + 1.0]
    non_mono = [chunks[i]["idx"] for i in range(1, n)
                if chunks[i]["start_s"] <= chunks[i - 1]["start_s"]]

    # 重复检测：同一段文本连续出现
    max_run, run, run_start = 0, 1, 0
    for i in range(1, n):
        if chunks[i]["text"] and chunks[i]["text"] == chunks[i - 1]["text"]:
            run += 1
            if run > max_run:
                max_run, run_start = run, i - run + 1
        else:
            run = 1

    # n-gram 复读：同一 10 字片段出现次数
    joined = "".join(c["text"] for c in chunks)
    grams: dict[str, int] = {}
    for i in range(0, max(0, len(joined) - 10), 5):
        g = joined[i:i + 10]
        if len(g) == 10:
            grams[g] = grams.get(g, 0) + 1
    top = sorted(grams.items(), key=lambda kv: kv[1], reverse=True)[:3]

    chars = [len(c["text"]) for c in chunks]
    return {
        "n_chunks": n,
        "duration_s": round(duration_s, 2),
        "covered_s": round(chunks[-1]["end_s"] if chunks else 0, 2),
        "empty_chunks": len(empty),
        "empty_ratio": round(len(empty) / n, 4) if n else 0,
        "empty_idx_sample": empty[:10],
        "out_of_range": oob,
        "non_monotonic": non_mono,
        "max_identical_run": max_run,
        "max_identical_run_start_idx": run_start if max_run > 1 else None,
        "top_repeated_10gram": [{"text": g, "count": c} for g, c in top if c > 1],
        "chars_total": int(sum(chars)),
        "chars_per_chunk_mean": round(float(np.mean(chars)), 1) if chars else 0,
        "chars_per_chunk_min": int(min(chars)) if chars else 0,
        "chars_per_chunk_max": int(max(chars)) if chars else 0,
        "needs_review_chunks": sum(1 for c in chunks if c["needs_review"]),
        "error_chunks": sum(1 for c in chunks if c["error"]),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extracted-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--chunk-s", type=float, default=CHUNK_S)
    ap.add_argument("--limit-s", type=float, default=0.0,
                    help="仅处理前 N 秒（冒烟测试用，0 表示全片）")
    args = ap.parse_args()

    src = Path(args.extracted_dir)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "_tmp"
    tmp.mkdir(parents=True, exist_ok=True)

    mp4s = sorted(src.glob("*.mp4"))
    if len(mp4s) < 2:
        print(f"预期 2 个 MP4，实际找到 {len(mp4s)} 个：{[p.name for p in mp4s]}")
        return 2

    log = out / "processing_log.md"
    lf = log.open("w", encoding="utf-8")
    lf.write(f"# 双轨处理日志\n\n> 生成时间：{now()}\n> 源目录：`{src}`\n\n"
             "| 时刻 | 轨道 | 阶段 | 说明 |\n|---|---|---|---|\n")

    def L(track: str, stage: str, msg: str):
        lf.write(f"| {now()} | {track} | {stage} | {msg} |\n")
        lf.flush()
        print(f"[{now()}] {track} {stage:<10} {msg}", flush=True)

    # ---------- 1. 媒体清单 ----------
    L("-", "PROBE", f"发现 {len(mp4s)} 个 MP4：{[p.name for p in mp4s]}")
    inventory = {"generated": now(), "source_dir": str(src), "tracks": []}
    for p in mp4s:
        info = ffprobe(p)
        rec = {"track": p.stem, "file": p.name, "path": str(p), **info}
        inventory["tracks"].append(rec)
        if "error" in info:
            L(p.stem, "PROBE", f"ffprobe 失败：{info['error']}")
        else:
            a = info["audio"][0] if info["audio"] else {}
            L(p.stem, "PROBE",
              f"{info['duration_s']:.1f}s  {info['width']}x{info['height']}  "
              f"音轨 {info['n_audio_streams']}  {a.get('codec')} "
              f"{a.get('sample_rate')}Hz {a.get('channels')}ch")
    (out / "media_inventory.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------- 2. 加载模型（只加载一次） ----------
    L("-", "MODEL", "加载 SenseVoiceSmall")
    from funasr import AutoModel
    t0 = time.perf_counter()
    model = AutoModel(model=str(SENSEVOICE), trust_remote_code=False, disable_update=True,
                      device="cpu", disable_pbar=True, disable_log=True)
    load_s = time.perf_counter() - t0
    L("-", "MODEL", f"加载完成 {load_s:.2f}s")

    summary = {"generated": now(), "model": "SenseVoiceSmall", "model_load_s": round(load_s, 2),
               "chunk_s": args.chunk_s, "denoise": False, "tracks": []}

    # ---------- 3. 逐轨处理 ----------
    for rec in inventory["tracks"]:
        track = rec["track"]
        if "error" in rec:
            continue
        tdir = out / track
        tdir.mkdir(exist_ok=True)
        adir = tdir / "audio"
        adir.mkdir(exist_ok=True)

        # 3.1 提取音频（原始方案，无降噪）
        wav = adir / "raw_16k.wav"
        cmd = [str(FFMPEG), "-hide_banner", "-loglevel", "error",
               "-i", rec["path"], "-map", "0:a:0", "-vn",
               "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)]
        rc, so, se, dt_ex = run(cmd, timeout=3600)
        if rc != 0 or not wav.exists():
            L(track, "EXTRACT", f"失败：{se.strip()[:200]}")
            summary["tracks"].append({"track": track, "status": "EXTRACT_FAILED",
                                      "error": se.strip()[:300]})
            continue
        x, sr = read_wav(wav)
        if args.limit_s > 0:
            x = x[: int(args.limit_s * sr)]
            L(track, "EXTRACT", f"冒烟模式：仅取前 {args.limit_s:.0f}s")
        L(track, "EXTRACT", f"{dt_ex:.2f}s  {len(x)/sr:.1f}s  {sr}Hz  "
                             f"{wav.stat().st_size/1024**2:.1f} MB（无降噪）")

        (tdir / "audio_meta.json").write_text(json.dumps({
            "track": track, "source_file": rec["path"],
            "source_audio": rec["audio"][0] if rec["audio"] else None,
            "extract_cmd": " ".join(cmd),
            "params": {"map": "0:a:0", "vn": True, "ac": 1, "ar": 16000,
                       "codec": "pcm_s16le", "denoise": False, "filters": "none"},
            "extract_s": round(dt_ex, 2),
            "out_path": str(wav), "out_bytes": wav.stat().st_size,
            "out_duration_s": round(len(x) / sr, 3),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        # 3.2 分块转写
        step = int(args.chunk_s * sr)
        n_chunks = (len(x) + step - 1) // step
        L(track, "ASR", f"开始分块转写，共 {n_chunks} 块 × {args.chunk_s:.0f}s")

        chunks, t_asr, errs = [], 0.0, 0
        cw = tmp / "c.wav"
        for i in range(n_chunks):
            seg = x[i * step:(i + 1) * step]
            if len(seg) < sr * 0.3:
                continue
            write_wav(cw, seg, sr)
            t1 = time.perf_counter()
            err = None
            try:
                r = model.generate(input=str(cw), cache={}, language="auto",
                                   use_itn=True, batch_size_s=60, merge_vad=False)
                raw = r[0].get("text", "") if r else ""
                text = TAG_RE.sub("", raw).strip()
            except Exception as e:
                raw, text, err = "", "", str(e)[:300]
                errs += 1
            dt = time.perf_counter() - t1
            t_asr += dt
            chunks.append({
                "idx": i, "track": track,
                "start_s": round(i * args.chunk_s, 2),
                "end_s": round(min((i + 1) * args.chunk_s, len(x) / sr), 2),
                "text": text, "text_raw": raw,
                "chars": len(text),
                "infer_s": round(dt, 3),
                "confidence": None,
                "confidence_note": "SenseVoiceSmall 不输出逐词/逐句置信度",
                "needs_review": bool(REVIEW_RE.search(text)),
                "error": err,
            })
            if (i + 1) % 20 == 0 or i == n_chunks - 1:
                L(track, "ASR", f"{i+1}/{n_chunks} 块，累计 {t_asr:.1f}s，"
                                 f"错误 {errs}，字符 {sum(c['chars'] for c in chunks)}")
        cw.unlink(missing_ok=True)

        # 3.3 落盘（每轨独立，不合并）
        with (tdir / "transcript.jsonl").open("w", encoding="utf-8") as f:
            for c in chunks:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")

        joined = "".join(c["text"] for c in chunks)
        with (tdir / "transcript.md").open("w", encoding="utf-8") as f:
            f.write(f"# 转写结果 · 轨道 {track}\n\n")
            f.write(f"> 源文件：`{rec['path']}`  \n")
            f.write(f"> 模型：SenseVoiceSmall（CPU，原始音频，**未降噪**）  \n")
            f.write(f"> 时长：{len(x)/sr:.1f}s ｜ 分块：{args.chunk_s:.0f}s ｜ "
                    f"块数：{len(chunks)} ｜ 字符：{len(joined)}  \n")
            f.write(f"> 生成：{now()}  \n\n")
            f.write("> ⚠️ 本文件仅为**单轨**结果，未与另一轨合并，也未做任何仲裁。\n\n")
            f.write("---\n\n")
            for c in chunks:
                mark = " ⚠️待核实" if c["needs_review"] else ""
                f.write(f"**[{c['start_s']:07.2f} – {c['end_s']:07.2f}]**{mark}  \n")
                f.write(f"{c['text'] if c['text'] else '（空）'}\n\n")

        qc = quality_checks(chunks, len(x) / sr)
        (tdir / "quality.json").write_text(
            json.dumps(qc, ensure_ascii=False, indent=2), encoding="utf-8")
        L(track, "QC", f"空块 {qc['empty_chunks']}/{qc['n_chunks']}，"
                        f"越界 {len(qc['out_of_range'])}，"
                        f"最长同文连续 {qc['max_identical_run']}，"
                        f"待核实 {qc['needs_review_chunks']}")

        summary["tracks"].append({
            "track": track, "source_file": rec["file"],
            "duration_s": round(len(x) / sr, 2),
            "extract_s": round(dt_ex, 2),
            "asr_s": round(t_asr, 2),
            "rtf": round(t_asr / (len(x) / sr), 4),
            "n_chunks": len(chunks),
            "chars": len(joined),
            "errors": errs,
            "transcript_jsonl": str(tdir / "transcript.jsonl"),
            "transcript_md": str(tdir / "transcript.md"),
            "transcript_bytes": (tdir / "transcript.jsonl").stat().st_size,
            "audio_bytes": wav.stat().st_size,
            "quality": qc,
            "status": "SUCCESS" if errs == 0 else "SUCCESS_WITH_ERRORS",
        })

    shutil.rmtree(tmp, ignore_errors=True)
    (out / "processing_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    L("-", "DONE", f"完成，{len(summary['tracks'])} 条轨道")
    lf.close()
    print(json.dumps({t["track"]: {k: t.get(k) for k in
                                  ("duration_s", "extract_s", "asr_s", "rtf", "chars", "status")}
                      for t in summary["tracks"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

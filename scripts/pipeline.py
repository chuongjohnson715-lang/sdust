# -*- coding: utf-8 -*-
"""
MVP-0 课程录播复盘管线
======================

阶段：probe -> audio -> asr -> slides -> ocr -> notes -> verify

设计约束（与 docs/01_取数方案.md 一致）：
1. 不安装任何新依赖，只使用 <VENV> 环境已有包。
2. SenseVoice 无原生时间戳，因此采用「30 秒块级时间戳」，不伪造精确时间。
3. 所有阶段带状态机与输入指纹，支持断点恢复。
4. 每个阶段写一条 JSONL 记录（耗时、峰值内存、CPU、输入输出、状态、错误）。

用法示例：
    python scripts/pipeline.py --window 0:600
    python scripts/pipeline.py --window 0:600 --force-stage asr
    python scripts/pipeline.py --window 0:600 --print-state
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from res_sampler import ResourceSampler  # noqa: E402
# 方案 A：复用阶段四的独立实现（裁剪/锐度/文本去重/时间线报告），避免两份代码
import extract_slides as xs  # noqa: E402

# ---------------------------------------------------------------- 外部依赖路径
# ffmpeg / ffprobe / SenseVoice 的绝对路径统一由 paths.py 解析（环境变量 → PATH → 本机兜底），
# 换机器只需改一处环境变量，不必改 5 个脚本。
from paths import FFMPEG, FFPROBE, SENSEVOICE_DIR, DEFAULT_INPUT_DIR  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent

STAGES = ["probe", "audio", "asr", "slides", "ocr", "notes", "verify"]
# 方案 A 追加：双轨音视频 + 课件证据对齐。既有阶段一行未改，只在末尾追加。
STAGES = STAGES + ["tracks_audio", "tracks_asr", "tracks_slides", "tracks_ocr", "tracks_align"]

# ---------------------------------------------------------------- 可调参数
DEFAULTS = {
    "chunk_s": 30,          # ASR 分块长度（秒）→ 决定时间戳精度
    "chunk_search_s": 5,    # 在目标边界附近搜索静音点的半径
    "chunk_min_s": 15,      # 最短块长，防止退化
    "sample_step_s": 2,     # PPT 抽帧间隔（秒）
    "change_pct": 2.0,      # 变化像素占比阈值（%）
    "burst_gap_s": 6,       # 变化簇合并间隔（秒）
    "dedup_pct": 1.0,       # 相邻关键帧去重阈值（%）
    "asr_denoise": True,    # 是否对 ASR 输入做降噪+归一化
    "keep_seq": False,      # 是否保留抽帧序列
    "min_free_gb": 2.0,     # 启动前最低剩余空间
    "decode_check_s": 60,   # 解码健康检查时长
}

# 方案 A 追加参数。刻意不放进 RESULT_AFFECTING_CFG，
# 这样既有任务的指纹不变，不会因新增参数而产生重复工作目录。
DEFAULTS.update({
    "track_ocr_scale": 1.5,          # 双轨流程 OCR 放大倍数
    "track_text_dedup": 0.55,        # 课件文本去重 Jaccard 阈值（0 关闭）
    "track_align_window_s": 30.0,    # 代表帧 ±窗口 关联转写
    "track_probe_frames": 8,         # 课件路判定每路抽帧数
    "track_edit_crop": "0.146,0.221,0.967,0.866",  # 编辑视图画布几何 l,t,r,b
})


# ---------------------------------------------------------------- 基础工具
def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def mmss(sec: float) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def sha256_short(obj) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run_cmd(cmd: list[str], timeout: int | None = None) -> tuple[int, str, str, float]:
    """执行外部命令，返回 (rc, stdout, stderr, 耗时)。不抛异常，由调用方判定。"""
    t0 = time.perf_counter()
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )
        return p.returncode, p.stdout, p.stderr, time.perf_counter() - t0
    except subprocess.TimeoutExpired:
        return -9, "", f"TIMEOUT after {timeout}s", time.perf_counter() - t0
    except Exception as e:  # noqa: BLE001
        return -1, "", f"{type(e).__name__}: {e}", time.perf_counter() - t0


def free_gb(path: Path) -> float:
    try:
        return shutil.disk_usage(str(path)).free / 1024 ** 3
    except OSError:
        return -1.0


def _pid_alive(pid: int) -> bool:
    import ctypes
    k32 = ctypes.WinDLL("kernel32")
    k32.OpenProcess.restype = ctypes.c_void_p
    h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        k32.CloseHandle(ctypes.c_void_p(h))
        return True
    return False


def acquire_lock(work: Path) -> Path:
    """防止同一任务被重复启动。锁文件记录 PID，PID 已死则视为陈旧锁。"""
    lock = work / "state.lock"
    if lock.exists():
        try:
            info = json.loads(lock.read_text(encoding="utf-8"))
            pid = int(info.get("pid", -1))
        except Exception:  # noqa: BLE001
            pid = -1
        if pid > 0 and pid != os.getpid() and _pid_alive(pid):
            raise SystemExit(f"任务正在被进程 {pid} 处理，拒绝重复启动：{work}")
        print(f"  发现陈旧锁（PID {pid} 已不存在），接管")
    lock.write_text(json.dumps({"pid": os.getpid(), "ts": now_iso()}), encoding="utf-8")
    import atexit
    atexit.register(lambda: lock.unlink(missing_ok=True))
    return lock


# ---------------------------------------------------------------- 上下文
RESULT_AFFECTING_CFG = [
    "chunk_s", "chunk_search_s", "chunk_min_s", "sample_step_s",
    "change_pct", "burst_gap_s", "dedup_pct", "asr_denoise",
]


class Ctx:
    def __init__(self, args):
        self.args = args
        self.input_dir = Path(args.input_dir)
        self.files = sorted(self.input_dir.glob("*.mp4"))
        if not self.files:
            raise SystemExit(f"输入目录中没有 mp4 文件：{self.input_dir}")
        self.win_start, self.win_end = self._parse_window(args.window)
        self.cfg = dict(DEFAULTS)
        for k, v in vars(args).items():
            if k in self.cfg and v is not None:
                self.cfg[k] = v

        fps = [{"path": str(f), "size": f.stat().st_size, "mtime": int(f.stat().st_mtime)} for f in self.files]
        # 只有影响结果的配置参与指纹；keep_seq / min_free_gb 等不影响产出的开关不参与，
        # 否则同一任务会因无关参数变化而产生重复工作目录。
        self.fingerprint_base = {
            "inputs": fps,
            "window": [self.win_start, self.win_end],
            "config": {k: self.cfg[k] for k in RESULT_AFFECTING_CFG},
        }
        self.task_id = sha256_short(self.fingerprint_base)
        label = f"{self.input_dir.name}_{self.win_start}-{self.win_end}_{self.task_id}"
        self.work = Path(args.work_root) / label
        self.work.mkdir(parents=True, exist_ok=True)
        self.state_path = self.work / "state.json"
        self.log_path = PROJECT_ROOT / "logs" / "run.jsonl"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.state = read_json(self.state_path, None) or {
            "task_id": self.task_id,
            "label": label,
            "created": now_iso(),
            "inputs": [str(f) for f in self.files],
            "window": [self.win_start, self.win_end],
            "config": self.cfg,
            "stages": {s: {"status": "pending"} for s in STAGES},
        }

    def _parse_window(self, s: str) -> tuple[float, float]:
        if not s:
            return 0.0, -1.0  # -1 表示到结尾
        a, b = s.split(":")
        return float(a), float(b)

    def save(self):
        atomic_write_json(self.state_path, self.state)

    def log(self, rec: dict):
        rec.setdefault("ts", now_iso())
        rec.setdefault("task_id", self.task_id)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def outputs_ok(paths: list[str]) -> bool:
    for p in paths:
        fp = Path(p)
        if not fp.exists() or fp.stat().st_size <= 0:
            return False
    return True


def run_stage(ctx: Ctx, name: str, fn, extra_fp=None, outputs_fn=None):
    """带状态机、指纹校验、下游失效的阶段执行器。"""
    rec = ctx.state["stages"].setdefault(name, {"status": "pending"})
    fp = sha256_short({"base": ctx.fingerprint_base, "extra": extra_fp or {}})

    # running 残留 → 上次异常退出，视为 failed
    if rec.get("status") == "running":
        rec["status"] = "failed"
        rec["error"] = (rec.get("error") or "") + " | 上次运行未正常结束（进程中断），已标记为 failed"

    # 先算 force：否则永远走不到下面（原实现的缺陷：SKIP 判断在 force 之前就 return 了）
    force = name in ctx.args.force_stage

    if (not force) and rec.get("status") == "succeeded" and rec.get("fingerprint") == fp:
        outs = rec.get("outputs", [])
        if outputs_ok(outs):
            print(f"[SKIP] {name}（已完成且输入未变）")
            ctx.log({"stage": name, "status": "skipped", "reason": "fingerprint+outputs ok"})
            return rec.get("result")

    if force:
        print(f"[FORCE] {name}")

    print(f"[RUN ] {name} ...")
    rec["started"] = now_iso()
    sampler = ResourceSampler(interval=0.5).start()
    t0 = time.perf_counter()
    status, error, result = "succeeded", None, None
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001
        status = "failed"
        error = f"{type(e).__name__}: {e}"
        print(f"[FAIL] {name}: {error}")
        traceback.print_exc()
    dur = time.perf_counter() - t0
    res = sampler.stop()

    outputs = []
    if outputs_fn and status == "succeeded":
        try:
            outputs = [str(p) for p in outputs_fn()]
        except Exception:  # noqa: BLE001
            outputs = []

    rec.update({
        "status": status,
        "fingerprint": fp,
        "ended": now_iso(),
        "dur_s": round(dur, 3),
        "peak_mem_mb": res["peak_mem_mb"],
        "cpu_s": res["cpu_s"],
        "cpu_pct_one_core": res["cpu_pct_of_one_core"],
        "outputs": outputs,
        "error": error,
    })
    if status == "succeeded":
        rec["result"] = result
    ctx.state["stages"][name] = rec
    ctx.save()

    ctx.log({
        "stage": name, "status": status, "dur_s": round(dur, 3),
        "peak_mem_mb": res["peak_mem_mb"], "cpu_s": res["cpu_s"],
        "cpu_pct_one_core": res["cpu_pct_of_one_core"],
        "outputs": outputs, "error": error,
        "result_summary": _summarize(result),
    })

    if status == "failed":
        raise RuntimeError(f"阶段 {name} 失败：{error}")

    # 下游失效
    idx = STAGES.index(name)
    for downstream in STAGES[idx + 1:]:
        d = ctx.state["stages"].get(downstream, {})
        if d.get("status") == "succeeded":
            d["status"] = "stale"
            ctx.state["stages"][downstream] = d
    ctx.save()
    print(f"[OK  ] {name}  {dur:.2f}s  peak={res['peak_mem_mb']:.0f}MB")
    return result


def _summarize(result):
    if isinstance(result, dict):
        keep = {k: v for k, v in result.items() if isinstance(v, (int, float, str, bool))}
        return keep
    return None


# ---------------------------------------------------------------- 阶段 1: probe
def stage_probe(ctx: Ctx):
    out_dir = ctx.work / "probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    media = {"files": [], "window": [ctx.win_start, ctx.win_end]}

    for f in ctx.files:
        rc, so, se, dt = run_cmd([
            FFPROBE, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(f)
        ], timeout=120)
        if rc != 0:
            raise RuntimeError(f"ffprobe 失败：{f.name} rc={rc} {se[:300]}")
        info = json.loads(so)
        (out_dir / f"{f.stem}.ffprobe.json").write_text(so, encoding="utf-8")

        fmt = info["format"]
        entry = {
            "file": str(f), "name": f.name,
            "size_bytes": int(fmt["size"]),
            "size_gb": round(int(fmt["size"]) / 1024 ** 3, 3),
            "duration_s": round(float(fmt["duration"]), 3),
            "bit_rate_kbps": round(int(fmt["bit_rate"]) / 1000, 1),
            "n_streams": int(fmt["nb_streams"]),
            "ffprobe_s": round(dt, 2),
            "streams": [],
        }
        for s in info["streams"]:
            item = {
                "index": s["index"], "type": s["codec_type"],
                "codec": s.get("codec_name"),
                "duration_s": round(float(s["duration"]), 3) if s.get("duration") else None,
            }
            if s["codec_type"] == "video":
                num, den = (s.get("r_frame_rate") or "0/1").split("/")
                item.update({
                    "width": s.get("width"), "height": s.get("height"),
                    "fps": round(float(num) / float(den), 2) if float(den) else None,
                    "pix_fmt": s.get("pix_fmt"), "nb_frames": s.get("nb_frames"),
                })
            elif s["codec_type"] == "audio":
                item.update({
                    "sample_rate": int(s.get("sample_rate") or 0),
                    "channels": s.get("channels"),
                    "channel_layout": s.get("channel_layout"),
                    "bit_rate_kbps": round(int(s["bit_rate"]) / 1000, 1) if s.get("bit_rate") else None,
                })
            entry["streams"].append(item)
        media["files"].append(entry)

    # 解码健康检查（窗口内前 N 秒，双流都过一遍）
    checks = []
    for f in ctx.files:
        dur = ctx.cfg["decode_check_s"]
        rc, so, se, dt = run_cmd([
            FFMPEG, "-hide_banner", "-v", "error", "-ss", str(ctx.win_start),
            "-t", str(dur), "-i", str(f), "-f", "null", "-"
        ], timeout=300)
        checks.append({
            "file": f.name, "checked_s": dur, "rc": rc,
            "decode_s": round(dt, 2), "errors": se.strip()[:500] if se.strip() else "",
            "ok": rc == 0 and not se.strip(),
        })
    media["decode_checks"] = checks

    atomic_write_json(ctx.work / "media.json", media)
    return {
        "files": len(media["files"]),
        "total_gb": round(sum(x["size_gb"] for x in media["files"]), 3),
        "all_decode_ok": all(c["ok"] for c in checks),
    }


# ---------------------------------------------------------------- 阶段 2: audio
def _audio_stats(wav: Path) -> dict:
    import numpy as np
    import soundfile as sf
    x, sr = sf.read(str(wav), dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    if len(x) == 0:
        return {"rms": 0.0, "peak": 0.0, "dur_s": 0.0, "sr": sr}
    rms = float(np.sqrt(np.mean(x ** 2)))
    # 语音段 RMS：只统计能量高于 -45dBFS 的帧，避免静音拉低均值
    win = int(0.05 * sr)
    n = len(x) // win
    if n > 0:
        fr = np.sqrt(np.mean(x[: n * win].reshape(n, win) ** 2, axis=1))
        speech = fr[fr > 10 ** (-45 / 20)]
        speech_rms = float(speech.mean()) if len(speech) else rms
    else:
        speech_rms = rms
    return {
        "rms": round(rms, 6), "speech_rms": round(speech_rms, 6),
        "peak": round(float(np.max(np.abs(x))), 6),
        "dur_s": round(len(x) / sr, 3), "sr": sr,
    }


def stage_audio(ctx: Ctx):
    import numpy as np  # noqa: F401
    probe = read_json(ctx.work / "media.json")
    if not probe:
        raise RuntimeError("缺少 media.json，请先运行 probe 阶段")

    win_dur = ctx.win_end - ctx.win_start
    cands = []
    tmp = ctx.work / "audio" / "_cand"
    tmp.mkdir(parents=True, exist_ok=True)

    for entry in probe["files"]:
        fpath = Path(entry["file"])
        file_dur = entry["duration_s"]
        # 取样位置：窗口内 25% 处（避开开场与结尾）
        pos = ctx.win_start + max(1.0, win_dur * 0.25)
        for st in entry["streams"]:
            if st["type"] != "audio":
                continue
            out = tmp / f"{fpath.stem}_a{st['index']}.wav"
            rc, so, se, dt = run_cmd([
                FFMPEG, "-hide_banner", "-loglevel", "error",
                "-ss", f"{pos:.2f}", "-t", "20", "-i", str(fpath),
                "-map", f"0:a:{_audio_ordinal(entry, st['index'])}",
                "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out),
            ], timeout=120)
            if rc != 0 or not out.exists():
                cands.append({
                    "file": fpath.name, "stream_index": st["index"],
                    "error": se.strip()[:300], "usable": False,
                })
                continue
            stats = _audio_stats(out)
            cands.append({
                "file": fpath.name, "file_path": str(fpath), "stream_index": st["index"],
                "source_sample_rate": st["sample_rate"], "channels": st["channels"],
                "probe_pos_s": round(pos, 2), "extract_s": round(dt, 2),
                "usable": stats["peak"] > 0.01,
                **stats,
            })

    usable = [c for c in cands if c.get("usable")]
    if not usable:
        raise RuntimeError("所有候选音轨均无有效电平，疑似录音缺失")

    # 选轨规则：语音段 RMS 最高者
    usable.sort(key=lambda c: c["speech_rms"], reverse=True)
    best = usable[0]
    print(f"  选轨：{best['file']} stream#{best['stream_index']} "
          f"speech_rms={best['speech_rms']} peak={best['peak']}")

    adir = ctx.work / "audio"
    raw16k = adir / "raw_16k.wav"
    asr_in = adir / "asr_in.wav"

    map_arg = f"0:a:{_audio_ordinal_by_path(probe, best['file_path'], best['stream_index'])}"
    rc, so, se, dt_raw = run_cmd([
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-ss", f"{ctx.win_start:.2f}", "-t", f"{win_dur:.2f}",
        "-i", best["file_path"], "-map", map_arg,
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(raw16k),
    ], timeout=1800)
    if rc != 0:
        raise RuntimeError(f"音频提取失败：{se[:400]}")

    t0 = time.perf_counter()
    if ctx.cfg["asr_denoise"]:
        rc, so, se, _ = run_cmd([
            FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(raw16k),
            "-af", "highpass=f=80,afftdn=nr=12:nf=-30,loudnorm=I=-16:TP=-1.5:LRA=11",
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(asr_in),
        ], timeout=1800)
        if rc != 0:
            raise RuntimeError(f"音频预处理失败：{se[:400]}")
    else:
        shutil.copy2(raw16k, asr_in)
    dt_proc = time.perf_counter() - t0

    stats_raw = _audio_stats(raw16k)
    stats_asr = _audio_stats(asr_in)

    meta = {
        "candidates": cands,
        "selected": {
            "file": best["file"], "stream_index": best["stream_index"],
            "reason": "语音段 RMS 最高（多候选音轨实测比对，非按文件名顺序）",
            "speech_rms": best["speech_rms"], "peak": best["peak"],
        },
        "raw_16k": {"path": str(raw16k), "size_mb": round(raw16k.stat().st_size / 1024 ** 2, 2),
                    "extract_s": round(dt_raw, 2), **stats_raw},
        "asr_input": {"path": str(asr_in), "size_mb": round(asr_in.stat().st_size / 1024 ** 2, 2),
                      "preprocess_s": round(dt_proc, 2),
                      "filter": "highpass=80,afftdn=nr=12:nf=-30,loudnorm" if ctx.cfg["asr_denoise"] else "none",
                      **stats_asr},
        "warnings": [],
    }
    if best["source_sample_rate"] < 16000:
        meta["warnings"].append(
            f"源音频采样率仅 {best['source_sample_rate']} Hz，有效带宽约 0–4 kHz；"
            "上采样到 16k 不会恢复丢失的高频，转写准确率存在物理上限"
        )
    atomic_write_json(adir / "audio_meta.json", meta)
    shutil.rmtree(tmp, ignore_errors=True)
    return {
        "selected_file": best["file"], "selected_stream": best["stream_index"],
        "source_sr": best["source_sample_rate"], "window_s": round(win_dur, 2),
        "extract_s": round(dt_raw, 2),
    }


def _audio_ordinal(entry, stream_index):
    """把容器内的 stream index 换算成 audio 流序号（0 起）。"""
    n = 0
    for st in entry["streams"]:
        if st["type"] == "audio":
            if st["index"] == stream_index:
                return n
            n += 1
    return 0


def _audio_ordinal_by_path(probe, file_path, stream_index):
    for e in probe["files"]:
        if e["file"] == file_path:
            return _audio_ordinal(e, stream_index)
    return 0


# ---------------------------------------------------------------- 阶段 3: asr
def _chunk_bounds(x, sr, chunk_s, search_s, min_s):
    """在目标边界附近找最低能量点作为切点，避免把词切断。"""
    import numpy as np
    N = len(x)
    hop = int(0.1 * sr)
    win = int(0.2 * sr)
    nfr = max(1, (N - win) // hop)
    csum = np.concatenate([[0.0], np.cumsum(x.astype(np.float64) ** 2)])
    rms = np.empty(nfr, dtype=np.float64)
    for i in range(nfr):
        s = i * hop
        e = s + win
        rms[i] = np.sqrt(max((csum[e] - csum[s]) / win, 0.0))

    bounds = [0]
    while True:
        tgt = bounds[-1] + int(chunk_s * sr)
        if tgt >= N - int(min_s * sr):
            break
        lo = max(bounds[-1] + int(min_s * sr), tgt - int(search_s * sr))
        hi = min(N - int(min_s * sr), tgt + int(search_s * sr))
        i0, i1 = lo // hop, max(lo // hop + 1, hi // hop)
        i1 = min(i1, nfr)
        if i1 <= i0:
            bounds.append(tgt)
            continue
        best = i0 + int(np.argmin(rms[i0:i1]))
        bounds.append(best * hop)
    bounds.append(N)
    return bounds


def stage_asr(ctx: Ctx):
    import re as _re
    import numpy as np
    import soundfile as sf

    ameta = read_json(ctx.work / "audio" / "audio_meta.json")
    if not ameta:
        raise RuntimeError("缺少 audio_meta.json，请先运行 audio 阶段")
    wav = ameta["asr_input"]["path"]

    os.environ["MODELSCOPE_OFFLINE"] = "1"
    os.environ["HF_HUB_OFFLINE"] = "1"

    x, sr = sf.read(wav, dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    total_s = len(x) / sr

    bounds = _chunk_bounds(x, sr, ctx.cfg["chunk_s"], ctx.cfg["chunk_search_s"], ctx.cfg["chunk_min_s"])
    chunks = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]

    t_imp0 = time.perf_counter()
    from funasr import AutoModel
    t_import = time.perf_counter() - t_imp0

    t0 = time.perf_counter()
    model = AutoModel(model=SENSEVOICE_DIR, trust_remote_code=False, disable_update=True,
                      device="cpu", disable_pbar=True, disable_log=True)
    t_load = time.perf_counter() - t0

    tag_re = _re.compile(r"<\|[^|]*\|>")
    rows = []
    t_infer_all = 0.0
    for i, (s, e) in enumerate(chunks):
        seg = x[s:e]
        t0 = time.perf_counter()
        res = model.generate(input=seg, cache={}, language="auto", use_itn=True,
                             batch_size_s=60, merge_vad=False)
        dt = time.perf_counter() - t0
        t_infer_all += dt
        raw = res[0]["text"] if res else ""
        tags = tag_re.findall(raw)
        text = tag_re.sub("", raw).strip()
        rows.append({
            "idx": i,
            "start_s": round(s / sr, 2),
            "end_s": round(e / sr, 2),
            "start_hms": mmss(s / sr),
            "start_abs_s": round(s / sr + ctx.win_start, 2),
            "end_abs_s": round(e / sr + ctx.win_start, 2),
            "start_abs_hms": mmss(s / sr + ctx.win_start),
            "end_abs_hms": mmss(e / sr + ctx.win_start),
            "dur_s": round((e - s) / sr, 2),
            "text": text,
            "tags": tags,
            "infer_s": round(dt, 3),
            "chars": len(text),
        })
        print(f"  [{i + 1}/{len(chunks)}] {mmss(s / sr)} {dt:.2f}s  {text[:40]}")

    out_jsonl = ctx.work / "asr" / "segments.jsonl"
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with open(out_jsonl, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    total_chars = sum(r["chars"] for r in rows)
    meta = {
        "engine": "funasr 1.4.16 / SenseVoiceSmall",
        "model_dir": SENSEVOICE_DIR,
        "device": "cpu",
        "timestamp_granularity": f"块级，约 {ctx.cfg['chunk_s']} 秒；模型本身不输出时间戳",
        "timestamp_note": "SenseVoiceSmall 为非自回归模型，不产生词级/句级时间戳。"
                          "此处时间戳含义为「该段文字出现在该 30 秒窗口内」，不是句子起始时刻。",
        "import_s": round(t_import, 2),
        "model_load_s": round(t_load, 2),
        "audio_total_s": round(total_s, 2),
        "n_chunks": len(rows),
        "infer_total_s": round(t_infer_all, 3),
        "rtf": round(t_infer_all / total_s, 4) if total_s else None,
        "total_chars": total_chars,
        "chars_per_min": round(total_chars / (total_s / 60), 1) if total_s else None,
        "chunk_bounds_s": [round(b / sr, 2) for b in bounds],
    }
    atomic_write_json(ctx.work / "asr" / "asr_meta.json", meta)
    return {
        "n_chunks": len(rows), "rtf": meta["rtf"],
        "infer_s": round(t_infer_all, 2), "total_chars": total_chars,
        "model_load_s": round(t_load, 2),
    }


# ---------------------------------------------------------------- 阶段 4: slides
def _img_gray_512(path: Path):
    from PIL import Image
    import numpy as np
    im = Image.open(path).convert("L")
    w, h = im.size
    im = im.crop((0, int(h * 0.115), w, int(h * 0.86)))
    return np.asarray(im.resize((512, 340)), dtype=np.float32)


def _diff_pct(a, b, thr=25):
    import numpy as np
    return float((np.abs(a - b) > thr).mean() * 100.0)


def stage_slides(ctx: Ctx):
    from PIL import Image  # noqa: F401
    probe = read_json(ctx.work / "media.json")
    # 选画面源：分辨率低但帧率高、且是屏幕录制的那个文件
    # 规则：优先选择"宽高比更接近 4:3 且分辨率 ≤1280 宽"的视频流（屏幕录制特征），否则选第一条视频流
    best = None
    for e in probe["files"]:
        for st in e["streams"]:
            if st["type"] != "video":
                continue
            ar = st["width"] / st["height"]
            score = 0
            if st["width"] <= 1280:
                score += 2
            if abs(ar - 4 / 3) < 0.05:
                score += 2
            if score > (best["score"] if best else -1):
                best = {"file": e["file"], "name": e["name"], "stream": st["index"],
                        "width": st["width"], "height": st["height"], "fps": st["fps"], "score": score}
    if not best:
        raise RuntimeError("未找到可用视频流")

    win_dur = ctx.win_end - ctx.win_start
    step = ctx.cfg["sample_step_s"]
    seq = ctx.work / "slides" / "_seq"
    if seq.exists() and not ctx.cfg.get("keep_seq", False):
        shutil.rmtree(seq)
    seq.mkdir(parents=True, exist_ok=True)

    rc, so, se, dt_seq = run_cmd([
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-ss", f"{ctx.win_start:.2f}", "-t", f"{win_dur:.2f}", "-i", best["file"],
        "-an", "-vf", f"fps=1/{step},scale=640:-1", "-q:v", "3",
        str(seq / "f_%05d.jpg"),
    ], timeout=3600)
    if rc != 0:
        raise RuntimeError(f"抽帧失败：{se[:400]}")

    frames = sorted(seq.glob("*.jpg"))
    if not frames:
        raise RuntimeError("抽帧结果为空")
    print(f"  抽帧 {len(frames)} 张，耗时 {dt_seq:.2f}s")

    grays = [_img_gray_512(p) for p in frames]
    changes = []
    for i in range(1, len(grays)):
        changes.append(_diff_pct(grays[i - 1], grays[i]))
    changes = [0.0] + changes

    thr = ctx.cfg["change_pct"]
    gap_frames = max(1, int(ctx.cfg["burst_gap_s"] / step))

    # 变化簇合并
    burst_idx = [i for i, c in enumerate(changes) if c > thr]
    bursts = []
    for i in burst_idx:
        if bursts and i - bursts[-1][-1] <= gap_frames:
            bursts[-1].append(i)
        else:
            bursts.append([i])

    # 稳定区间
    segs = []
    prev_end = 0
    for b in bursts:
        start = prev_end
        end = b[-1]
        if end > start:
            segs.append((start, end))
        prev_end = b[-1]
    if prev_end < len(frames) - 1:
        segs.append((prev_end, len(frames) - 1))

    # 代表帧 = 稳定区间中点（用序列帧做去重判断）
    reps = []
    for (s, e) in segs:
        mid = (s + e) // 2
        if reps and _diff_pct(grays[reps[-1]["mid"]], grays[mid]) < ctx.cfg["dedup_pct"]:
            reps[-1]["end_idx"] = e
            continue
        reps.append({"start_idx": s, "end_idx": e, "mid": mid})

    sdir = ctx.work / "slides"
    sdir.mkdir(parents=True, exist_ok=True)
    slides = []
    for k, r in enumerate(reps, start=1):
        t_rep = ctx.win_start + r["mid"] * step
        img = sdir / f"slide_{k:03d}.jpg"
        rc, so, se, _ = run_cmd([
            FFMPEG, "-hide_banner", "-loglevel", "error",
            "-ss", f"{t_rep:.2f}", "-i", best["file"],
            "-frames:v", "1", "-q:v", "2", str(img),
        ], timeout=120)
        if rc != 0 or not img.exists():
            print(f"  [WARN] 代表帧抽取失败 t={t_rep:.1f}s")
            continue
        slides.append({
            "index": k,
            "t_start_s": round(ctx.win_start + r["start_idx"] * step, 1),
            "t_end_s": round(ctx.win_start + r["end_idx"] * step, 1),
            "t_rep_s": round(t_rep, 1),
            "t_start_hms": mmss(ctx.win_start + r["start_idx"] * step),
            "t_end_hms": mmss(ctx.win_start + r["end_idx"] * step),
            "image": str(img),
            "image_rel": f"slides/{img.name}",
            "image_kb": round(img.stat().st_size / 1024, 1),
            "change_at_entry_pct": round(changes[r["start_idx"]], 2) if r["start_idx"] < len(changes) else 0.0,
        })

    if ctx.cfg["keep_seq"]:
        kept_seq = sdir / "seq"
        if kept_seq.exists():
            shutil.rmtree(kept_seq)
        shutil.move(str(seq), str(kept_seq))
    else:
        shutil.rmtree(seq, ignore_errors=True)

    meta = {
        "source": {k: best[k] for k in ("file", "name", "width", "height", "fps")},
        "source_selection_rule": "优先选择分辨率 ≤1280 宽且宽高比接近 4:3 的视频流（屏幕录制特征）",
        "sample_step_s": step,
        "n_sampled_frames": len(frames),
        "change_threshold_pct": thr,
        "n_bursts": len(bursts),
        "n_slides": len(slides),
        "sample_decode_s": round(dt_seq, 2),
        "change_series_pct": [round(c, 2) for c in changes],
        "slides": slides,
    }
    atomic_write_json(sdir / "slides.json", meta)
    return {"n_slides": len(slides), "n_frames": len(frames), "decode_s": round(dt_seq, 2)}


# ---------------------------------------------------------------- 阶段 5: ocr
def _crop_black_bars(img):
    import numpy as np
    from PIL import Image
    a = np.asarray(img.convert("L"), dtype=np.float32)
    row = a.mean(axis=1)
    h = len(row)
    top = 0
    while top < h * 0.3 and row[top] < 18:
        top += 1
    bot = h - 1
    while bot > h * 0.7 and row[bot] < 18:
        bot -= 1
    if bot <= top:
        return img
    return img.crop((0, top, img.width, bot + 1))


def stage_ocr(ctx: Ctx):
    import numpy as np
    import easyocr
    from PIL import Image

    smeta = read_json(ctx.work / "slides" / "slides.json")
    if not smeta:
        raise RuntimeError("缺少 slides.json")

    t0 = time.perf_counter()
    reader = easyocr.Reader(["ch_sim", "en"], gpu=False, download_enabled=False, verbose=False)
    t_init = time.perf_counter() - t0

    results = []
    for s in smeta["slides"]:
        p = Path(s["image"])
        im = Image.open(p)
        cropped = _crop_black_bars(im)
        t0 = time.perf_counter()
        res = reader.readtext(np.array(cropped), detail=1, paragraph=False)
        dt = time.perf_counter() - t0
        boxes, texts = [], []
        ch = cropped.height
        for bbox, txt, conf in res:
            ys = [pt[1] for pt in bbox]
            cy = (min(ys) + max(ys)) / 2
            is_wm = bool(cy > ch * 0.93)  # 底部水印区
            boxes.append({
                "text": txt, "conf": round(float(conf), 3),
                "x": int(min(pt[0] for pt in bbox)), "y": int(min(ys)),
                "w": int(max(pt[0] for pt in bbox) - min(pt[0] for pt in bbox)),
                "h": int(max(ys) - min(ys)),
                "watermark_region": is_wm,
            })
            if not is_wm:
                texts.append(txt)
        results.append({
            "slide_index": s["index"],
            "t_rep_s": s["t_rep_s"],
            "image": s["image"],
            "ocr_s": round(dt, 3),
            "n_boxes": len(boxes),
            "avg_conf": round(sum(b["conf"] for b in boxes) / len(boxes), 3) if boxes else 0.0,
            "text": "".join(texts),
            "boxes": boxes,
        })
        print(f"  slide {s['index']:03d}  {dt:.2f}s  {len(boxes)} 框  "
              f"conf={results[-1]['avg_conf']:.2f}  {results[-1]['text'][:40]}")

    meta = {
        "engine": "easyocr 1.7.2 (ch_sim+en), CPU",
        "reader_init_s": round(t_init, 2),
        "n_images": len(results),
        "ocr_total_s": round(sum(r["ocr_s"] for r in results), 2),
        "avg_conf": round(sum(r["avg_conf"] for r in results) / max(len(results), 1), 3),
        "watermark_rule": "裁剪框底部 7% 区域判定为水印区，不计入正文文本",
        "results": results,
    }
    atomic_write_json(ctx.work / "slides" / "ocr.json", meta)
    return {"n_images": len(results), "ocr_s": meta["ocr_total_s"], "avg_conf": meta["avg_conf"]}


# ---------------------------------------------------------------- 阶段 6: notes
# 考试信号分级（规则匹配，非模型推理）
EXAM_PATTERNS = [
    # (类别, 正则, 说明)
    ("A_老师明确说明", r"(期末|期中|考试|考点|考题|题型|分值|开卷|闭卷|必考|会考|要考|考这个|考到|重点考)", "直接指向考试本身"),
    ("A_老师明确说明", r"(大题|计算题|证明题|简答题|选择题|填空题|论述题)", "明确题型"),
    ("B_一般性强调", r"(重点|重要|注意|特别注意|强调|掌握|记住|记一下|必须|一定要|关键)", "一般性强调，未直接指向考试"),
    ("B_一般性强调", r"(作业|习题|练习|复习|预习|下节课|下次课)", "任务性提示"),
]


def _find_exam_signals(chunks):
    hits = []
    for i, c in enumerate(chunks):
        for cat, pat, note in EXAM_PATTERNS:
            m = re.search(pat, c["text"])
            if not m:
                continue
            hits.append({
                "category": cat,
                "matched": m.group(0),
                "note": note,
                "chunk_idx": i,
                "t_start_hms": c["start_abs_hms"],
                "t_end_hms": c["end_abs_hms"],
                "quote": c["text"],
                "context_prev": chunks[i - 1]["text"] if i > 0 else "",
                "context_next": chunks[i + 1]["text"] if i + 1 < len(chunks) else "",
            })
            break  # 每块只取一条最强信号，避免重复
    return hits


def stage_notes(ctx: Ctx):
    probe = read_json(ctx.work / "media.json")
    ameta = read_json(ctx.work / "audio" / "audio_meta.json")
    asr_meta = read_json(ctx.work / "asr" / "asr_meta.json")
    smeta = read_json(ctx.work / "slides" / "slides.json")
    ometa = read_json(ctx.work / "slides" / "ocr.json")

    chunks = []
    with open(ctx.work / "asr" / "segments.jsonl", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                chunks.append(json.loads(line))

    slides = smeta["slides"]
    ocr_by_slide = {r["slide_index"]: r for r in ometa["results"]}

    def slide_at(t):
        for s in slides:
            if s["t_start_s"] <= t <= s["t_end_s"]:
                return s
        return None

    hits = _find_exam_signals(chunks)
    a_hits = [h for h in hits if h["category"].startswith("A")]
    b_hits = [h for h in hits if h["category"].startswith("B")]

    L = []
    add = L.append
    add(f"# 课程笔记（MVP-0 样例）：{ctx.input_dir.name}")
    add("")
    add("> **本文件由脚本自动生成，属于「结构化草稿」，未经人工校对。**")
    add("> 转写文本受源音频限制（8 kHz 采样率、教室远场收音），存在错字与漏字；")
    add("> 任何用于考试复习的判断，请以音频原声为准。")
    add("")
    add("## 1. 样本基本信息")
    add("")
    add("| 项目 | 内容 |")
    add("|---|---|")
    add(f"| 任务 ID | `{ctx.task_id}` |")
    add(f"| 处理区间 | {mmss(ctx.win_start)} – {mmss(ctx.win_end)} |")
    add(f"| 输入文件 | " + "；".join(f"`{Path(e['file']).name}`（{e['size_gb']} GB，{mmss(e['duration_s'])}）" for e in probe["files"]) + " |")
    add(f"| 画面来源 | `{smeta['source']['name']}`（{smeta['source']['width']}×{smeta['source']['height']}，{smeta['source']['fps']} fps） |")
    add(f"| 音频来源 | `{ameta['selected']['file']}` stream#{ameta['selected']['stream_index']}（源采样率 {ameta['candidates'][0]['source_sample_rate']} Hz） |")
    add(f"| 转写引擎 | {asr_meta['engine']}（{asr_meta['device']}） |")
    add(f"| 时间戳精度 | {asr_meta['timestamp_granularity']} |")
    add(f"| 生成时间 | {now_iso()} |")
    add("")

    add("## 2. 初步课程结构（按 PPT 画面切换）")
    add("")
    add("| # | 起 | 止 | 首行文字（OCR） |")
    add("|---|---|---|---|")
    for s in slides:
        o = ocr_by_slide.get(s["index"], {})
        first = (o.get("text") or "")[:28]
        add(f"| {s['index']} | {s['t_start_hms']} | {s['t_end_hms']} | {first} |")
    add("")
    add(f"> 共识别出 {len(slides)} 个稳定画面段（每 {smeta['sample_step_s']} 秒采样一次，"
        f"变化像素占比阈值 {smeta['change_threshold_pct']}%）。")
    add("")

    add("## 3. 疑似考试重点与证据")
    add("")
    add("### 3.1 类别说明")
    add("")
    add("| 类别 | 含义 | 本项目是否可判定 |")
    add("|---|---|---|")
    add("| A 老师明确说明 | 直接提到考试、题型、分值、必考 | 仅规则匹配，**需人工听原声确认** |")
    add("| B 老师一般性强调 | 提到重要、注意、掌握、作业 | 仅规则匹配，**需人工听原声确认** |")
    add("| C 历史资料支持 | 往年真题、大纲等外部资料 | **本阶段无历史资料，未生成** |")
    add("| D 模型推测 | 模型根据语义推断的可能考点 | **本阶段未启用推测，不做任何推断** |")
    add("")
    add(f"### 3.2 A 类：老师明确说明（{len(a_hits)} 条）")
    add("")
    if not a_hits:
        add("_本区间内未匹配到考试类关键词。_")
        add("")
    for h in a_hits:
        s = slide_at(_hms_to_s(h["t_start_hms"]))
        add(f"**{h['t_start_hms']} – {h['t_end_hms']}** ｜ 命中：`{h['matched']}`")
        add("")
        add(f"- 转写原话（机器转写，未经校对）：> {h['quote']}")
        add(f"- 上一块上下文：{h['context_prev'][:60] or '（无）'}")
        add(f"- 下一块上下文：{h['context_next'][:60] or '（无）'}")
        if s:
            add(f"- 对应画面：{mmss(s['t_rep_s'])}（[{s['image_rel']}]({s['image_rel']})）")
        add(f"- 核验方式：回听音频 **{h['t_start_hms']}** 起约 30 秒")
        add("")
    add(f"### 3.3 B 类：老师一般性强调（{len(b_hits)} 条）")
    add("")
    if not b_hits:
        add("_本区间内未匹配到强调类关键词。_")
        add("")
    for h in b_hits:
        add(f"**{h['t_start_hms']} – {h['t_end_hms']}** ｜ 命中：`{h['matched']}`")
        add("")
        add(f"- 转写原话（机器转写，未经校对）：> {h['quote']}")
        add(f"- 核验方式：回听音频 **{h['t_start_hms']}** 起约 30 秒")
        add("")
    add("### 3.4 C 类与 D 类")
    add("")
    add("- C 类（历史资料支持）：本阶段未接入往年真题/课程大纲，**未生成**。")
    add("- D 类（模型推测）：本阶段**明确不做推测**，以避免把推测伪装成老师原话。")
    add("")

    add("## 4. PPT 画面与文字")
    add("")
    for s in slides:
        o = ocr_by_slide.get(s["index"], {})
        add(f"### 画面 {s['index']} ｜ {s['t_start_hms']} – {s['t_end_hms']}")
        add("")
        add(f"![slide {s['index']}]({s['image_rel']})")
        add("")
        add(f"- OCR 平均置信度：{o.get('avg_conf', 0)}（{o.get('n_boxes', 0)} 个文本框）")
        add(f"- 识别文字：{o.get('text', '')[:300]}")
        add("")

    add("## 5. 转写全文（块级时间戳）")
    add("")
    add(f"> 共 {len(chunks)} 块，每块约 {ctx.cfg['chunk_s']} 秒。时间戳表示「该段文字出现在该时间窗内」，"
        "不代表句子起始时刻。")
    add("")
    add("| 时间 | 内容 |")
    add("|---|---|")
    for c in chunks:
        add(f"| {c['start_abs_hms']} | {c['text']} |")
    add("")

    add("## 6. 无法确认 / 需人工核验")
    add("")
    add("1. **转写准确性**：源音频 8 kHz，实测同一句出现多种识别结果。所有引用的「原话」都必须回听确认。")
    add("2. **时间戳精度**：块级（约 30 秒），不能用于精确定位单句。")
    add("3. **OCR 专有名词**：实测出现 `R&D→R&0`、`项目→项耳`、编号误识等，术语与数字需人工核对。")
    add("4. **考试范围归属**：规则匹配只能提示「这句话提到了考试」，无法判断考试范围边界。")
    add("5. **音画同步**：仅验证了两路音频之间约 0.5 秒偏移，未逐段核验音画同步。")
    add("")

    notes_path = ctx.work / "课程笔记.md"
    notes_path.write_text("\n".join(L), encoding="utf-8")

    # 结构化草稿（供后续阶段/Notion 使用）
    struct = {
        "task_id": ctx.task_id,
        "window": [ctx.win_start, ctx.win_end],
        "slides": [{k: s[k] for k in ("index", "t_start_s", "t_end_s", "t_rep_s", "image_rel")} for s in slides],
        "exam_signals": hits,
        "chunks": chunks,
        "disclaimer": "机器生成，未经人工校对；A/B 类均为规则匹配结果",
    }
    atomic_write_json(ctx.work / "notes.json", struct)

    return {
        "notes_chars": len("\n".join(L)),
        "n_exam_A": len(a_hits), "n_exam_B": len(b_hits),
        "n_slides": len(slides), "n_chunks": len(chunks),
    }


def _hms_to_s(hms: str) -> float:
    parts = [int(x) for x in hms.split(":")]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return parts[0] * 60 + parts[1]


# ---------------------------------------------------------------- 阶段 7: verify
def stage_verify(ctx: Ctx):
    from PIL import Image
    probe = read_json(ctx.work / "media.json")
    smeta = read_json(ctx.work / "slides" / "slides.json")
    asr_meta = read_json(ctx.work / "asr" / "asr_meta.json")
    notes = ctx.work / "课程笔记.md"

    checks = []

    def chk(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": str(detail)})

    # 1. 笔记存在
    chk("课程笔记.md 存在且非空", notes.exists() and notes.stat().st_size > 2000,
        f"{notes.stat().st_size if notes.exists() else 0} bytes")

    md = notes.read_text(encoding="utf-8") if notes.exists() else ""

    # 2. 图片链接可解析
    links = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", md)
    missing = [l for l in links if not (ctx.work / l).exists()]
    chk("Markdown 图片链接全部可解析", len(links) > 0 and not missing,
        f"{len(links)} 个链接，缺失 {len(missing)}")

    # 3. 图片可打开
    bad = []
    for s in smeta["slides"]:
        try:
            im = Image.open(s["image"])
            im.verify()
            if im.width < 320:
                bad.append(f"{Path(s['image']).name} 尺寸异常 {im.size}")
        except Exception as e:  # noqa: BLE001
            bad.append(f"{Path(s['image']).name}: {e}")
    chk("关键帧图片均可打开且尺寸合理", not bad, f"{len(smeta['slides'])} 张，异常 {len(bad)}")

    # 4. 时间戳在媒体范围内
    media_dur = min(e["duration_s"] for e in probe["files"])
    over = [c for c in _load_chunks(ctx) if c["end_abs_s"] > media_dur + 1.0]
    chk("转写时间戳未超出媒体时长", not over, f"媒体 {media_dur:.0f}s，越界 {len(over)} 块")

    # 5. 转写覆盖率
    chunks = _load_chunks(ctx)
    covered = sum(c["dur_s"] for c in chunks)
    chk("音频窗口覆盖率 > 95%", covered / max(1e-6, ctx.win_end - ctx.win_start) > 0.95,
        f"{covered:.1f}s / {ctx.win_end - ctx.win_start:.1f}s")

    # 6. 空转写块比例
    empty = [c for c in chunks if c["chars"] == 0]
    chk("空转写块比例 < 10%", len(empty) / max(1, len(chunks)) < 0.1,
        f"{len(empty)}/{len(chunks)} 空块")

    # 7. 考点信号存在性（不判对错，只判是否产出）
    struct = read_json(ctx.work / "notes.json") or {}
    chk("已产出考试信号候选（不代表正确）", "exam_signals" in struct,
        f"{len(struct.get('exam_signals', []))} 条候选")

    # 8. 时间戳口径声明存在
    chk("笔记中明确声明时间戳精度限制", "不代表句子起始时刻" in md, "见第 5 节")

    total = len(checks)
    passed = sum(1 for c in checks if c["ok"])

    rep = ["# 输出验证报告", "", f"- 任务 ID：`{ctx.task_id}`", f"- 验证时间：{now_iso()}",
           f"- 结果：**{passed}/{total} 项通过**", "", "| # | 检查项 | 结果 | 详情 |", "|---|---|---|---|"]
    for i, c in enumerate(checks, 1):
        rep.append(f"| {i} | {c['check']} | {'✅' if c['ok'] else '❌'} | {c['detail']} |")
    rep += ["", "> 说明：本验证只检查「输出是否存在、结构是否有效、链接是否可解析」，",
            "> 不代表转写内容正确或考点识别准确。内容质量需人工抽检。"]

    (ctx.work / "验证报告.md").write_text("\n".join(rep), encoding="utf-8")
    atomic_write_json(ctx.work / "verify.json", {"checks": checks, "passed": passed, "total": total})

    if passed < total:
        print(f"  [WARN] {total - passed} 项验证未通过，详见 验证报告.md")
    return {"passed": passed, "total": total}


def _load_chunks(ctx):
    out = []
    p = ctx.work / "asr" / "segments.jsonl"
    if p.exists():
        with open(p, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    out.append(json.loads(line))
    return out


# ================================================================
# 阶段 8–12：双轨音视频 + 课件证据对齐（方案 A 新增）
# ----------------------------------------------------------------
# 设计约束：
# 1. 上面任何既有阶段与工具函数一行未改，本块只在 STAGES 末尾追加阶段。
# 2. 产物写入独立子目录 tracks/ 与 slides_tracks/，与单轨 audio/、asr/、slides/、ocr/ 并行。
# 3. 双轨原则：两路各自独立提取、独立转写、不合并；不预设哪一路更准，
#    两路一致不代表正确，不一致也不代表某一路一定错误。
# 4. 不降噪。实测 afftdn=nr=12 削弱 4 kHz 以上擦音，「六十分」误识为「个59科」，
#    CER 20.34% → 28.81%。
# 5. 课件路判定用画面内容证据（锐度 + 纯色占比），不按分辨率猜。
# ================================================================

_TRACK_REVIEW_RE = re.compile(
    r"\d|百分之|考试|考核|成绩|分数|重点|范围|必考|不考|及格|重修|学分|平时|期末|期中"
)


def _edit_crop_tuple(ctx: Ctx):
    raw = str(ctx.cfg.get("track_edit_crop") or "0.146,0.221,0.967,0.866")
    try:
        vals = tuple(float(x) for x in raw.split(","))
        if len(vals) == 4 and all(0.0 <= v <= 1.0 for v in vals):
            return vals
    except ValueError:
        pass
    return xs.EDIT_CROP_RATIO


def _track_quality(rows: list[dict], duration_s: float) -> dict:
    """双轨转写质检。与单轨 quality 口径一致，便于横向对比。

    越界/时序检查用窗口内相对时间（start_rel_s），因为 duration_s 是窗口长度；
    若 rows 没有相对时间字段则回退到 start_s。
    """
    n = len(rows)
    rel = [(r.get("start_rel_s", r["start_s"]), r.get("end_rel_s", r["end_s"]))
           for r in rows]
    out_of_range = [r["idx"] for r, (s, e) in zip(rows, rel)
                    if s < 0 or e > duration_s + 1.0]
    non_mono = [rows[i]["idx"] for i in range(1, n)
                if rel[i][0] < rel[i - 1][0]]
    empty = [r["idx"] for r in rows if not r["text"].strip()]
    longest, cur, start = 0, 1, 0
    for i in range(1, n):
        if rows[i]["text"] == rows[i - 1]["text"]:
            cur += 1
            if cur > longest:
                longest, start = cur, i - cur + 1
        else:
            cur = 1
    chars = [r["chars"] for r in rows] or [0]
    return {
        "n_chunks": n,
        "duration_s": round(duration_s, 2),
        "out_of_range": out_of_range,
        "non_monotonic": non_mono,
        "empty_chunks": len(empty),
        "empty_idx_sample": empty[:10],
        "max_identical_run": longest,
        "max_identical_run_start_idx": start if longest > 1 else None,
        "chars_total": sum(chars),
        "chars_per_chunk_mean": round(sum(chars) / max(n, 1), 1),
        "chars_per_chunk_min": min(chars),
        "chars_per_chunk_max": max(chars),
        "needs_review_chunks": sum(1 for r in rows if r["needs_review"]),
        "error_chunks": sum(1 for r in rows if r.get("error")),
    }


def stage_tracks_audio(ctx: Ctx):
    """双轨音频：每个 mp4 各提一路 16k 单声道，原样提取，不加任何滤波。"""
    probe = read_json(ctx.work / "media.json")
    if not probe:
        raise RuntimeError("缺少 media.json，请先运行 probe 阶段")

    tdir = ctx.work / "tracks"
    tdir.mkdir(parents=True, exist_ok=True)
    win_dur = (ctx.win_end - ctx.win_start) if ctx.win_end > 0 else -1.0

    inv = {
        "generated": now_iso(),
        "window": [ctx.win_start, ctx.win_end],
        "denoise": False,
        "denoise_note": "不降噪。实测 afftdn=nr=12 会削弱 4 kHz 以上擦音，"
                        "使「六十分」误识为「个59科」，CER 20.34% → 28.81%。",
        "tracks": [],
    }

    for entry in probe["files"]:
        fpath = Path(entry["file"])
        td = tdir / fpath.stem
        (td / "audio").mkdir(parents=True, exist_ok=True)
        astreams = [s for s in entry["streams"] if s["type"] == "audio"]
        vstreams = [s for s in entry["streams"] if s["type"] == "video"]

        base = {
            "track": fpath.stem, "file": fpath.name, "path": str(fpath),
            "source_video_duration_s": entry["duration_s"],
            "width": vstreams[0].get("width") if vstreams else None,
            "height": vstreams[0].get("height") if vstreams else None,
            "video_fps": vstreams[0].get("fps") if vstreams else None,
        }

        if not astreams:
            inv["tracks"].append({**base, "status": "SKIPPED", "error": "容器内无音频流"})
            print(f"  [SKIP] {fpath.name}：无音频流")
            continue

        st = astreams[0]
        wav = td / "audio" / "raw_16k.wav"
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", f"{ctx.win_start:.2f}"]
        if win_dur > 0:
            cmd += ["-t", f"{win_dur:.2f}"]
        cmd += ["-i", str(fpath),
                "-map", f"0:a:{_audio_ordinal(entry, st['index'])}",
                "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)]
        rc, so, se, dt = run_cmd(cmd, timeout=7200)
        if rc != 0 or not wav.exists():
            inv["tracks"].append({**base, "status": "FAILED",
                                  "error": f"ffmpeg rc={rc}: {se.strip()[:300]}"})
            print(f"  [FAIL] {fpath.name} 音频提取：{se.strip()[:160]}")
            continue

        import soundfile as sf
        info = sf.info(str(wav))
        stats = _audio_stats(wav)
        meta = {
            **base,
            "status": "SUCCESS",
            "error": None,
            "audio_stream_index": st["index"],
            "source_sample_rate": st["sample_rate"],
            "source_channels": st["channels"],
            "source_codec": st.get("codec"),
            "wav_path": str(wav),
            "wav_bytes": wav.stat().st_size,
            "duration_s": round(info.duration, 3),
            "sample_rate": info.samplerate,
            "channels": info.channels,
            "extract_s": round(dt, 2),
            "filter": "none（-map 0:a:0 -vn -ac 1 -ar 16000 -c:a pcm_s16le）",
            **stats,
        }
        if st["sample_rate"] < 16000:
            meta["warning"] = (
                f"源音频采样率仅 {st['sample_rate']} Hz，有效带宽约 0–4 kHz；"
                "上采样到 16k 不会恢复丢失的高频，转写准确率存在物理上限"
            )
            print(f"  [WARN] {fpath.name} 源采样率 {st['sample_rate']} Hz")
        atomic_write_json(td / "audio_meta.json", meta)
        inv["tracks"].append({k: meta[k] for k in (
            "track", "file", "path", "status", "error", "duration_s",
            "source_video_duration_s", "width", "height", "video_fps",
            "audio_stream_index", "source_sample_rate", "source_channels",
            "wav_path", "wav_bytes", "sample_rate", "channels", "extract_s",
            "rms", "peak", "speech_rms") if k in meta})
        print(f"  track {fpath.stem}: {info.duration:.1f}s  {wav.stat().st_size / 1024 ** 2:.1f} MB  "
              f"提取 {dt:.2f}s  speech_rms={meta.get('speech_rms')}")

    atomic_write_json(tdir / "media_inventory.json", inv)
    ok = [t for t in inv["tracks"] if t["status"] == "SUCCESS"]
    if not ok:
        raise RuntimeError("没有任何音轨提取成功")
    return {"n_tracks": len(inv["tracks"]), "n_ok": len(ok),
            "tracks": [t["track"] for t in ok]}


def stage_tracks_asr(ctx: Ctx):
    """双轨转写：两路各自独立 30s 分块转写，模型只加载一次。"""
    import soundfile as sf

    inv = read_json(ctx.work / "tracks" / "media_inventory.json")
    if not inv:
        raise RuntimeError("缺少 tracks/media_inventory.json，请先运行 tracks_audio 阶段")
    todo = [t for t in inv["tracks"] if t.get("status") == "SUCCESS"]
    if not todo:
        raise RuntimeError("没有可转写的音轨")

    os.environ["MODELSCOPE_OFFLINE"] = "1"
    os.environ["HF_HUB_OFFLINE"] = "1"

    t_imp0 = time.perf_counter()
    from funasr import AutoModel
    t_import = time.perf_counter() - t_imp0
    t0 = time.perf_counter()
    model = AutoModel(model=SENSEVOICE_DIR, trust_remote_code=False, disable_update=True,
                      device="cpu", disable_pbar=True, disable_log=True)
    t_load = time.perf_counter() - t0
    print(f"  模型加载 {t_load:.2f}s（两路共用一次加载）")

    tag_re = re.compile(r"<\|[^|]*\|>")
    summary = {
        "generated": now_iso(),
        "model": "SenseVoiceSmall",
        "model_dir": SENSEVOICE_DIR,
        "model_load_s": round(t_load, 2),
        "import_s": round(t_import, 2),
        "chunk_s": ctx.cfg["chunk_s"],
        "denoise": False,
        "confidence_note": "SenseVoiceSmall 为非自回归模型，不输出置信度；"
                           "transcript.jsonl 中 confidence 置 null，不以字符数冒充置信度。",
        "tracks": [],
    }

    for t in todo:
        tr = t["track"]
        td = ctx.work / "tracks" / tr
        wav = Path(t["wav_path"])
        x, sr = sf.read(str(wav), dtype="float32")
        if x.ndim > 1:
            x = x.mean(axis=1)
        total_s = len(x) / sr
        bounds = _chunk_bounds(x, sr, ctx.cfg["chunk_s"],
                               ctx.cfg["chunk_search_s"], ctx.cfg["chunk_min_s"])
        chunks = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]
        print(f"  track {tr}: {total_s:.1f}s → {len(chunks)} 块")

        rows, t_infer, errors = [], 0.0, 0
        for i, (s, e) in enumerate(chunks):
            seg = x[s:e]
            t1 = time.perf_counter()
            err = None
            try:
                res = model.generate(input=seg, cache={}, language="auto", use_itn=True,
                                     batch_size_s=60, merge_vad=False)
                raw = res[0]["text"] if res else ""
            except Exception as ex:  # noqa: BLE001
                raw = ""
                err = f"{type(ex).__name__}: {ex}"
                errors += 1
            dt = time.perf_counter() - t1
            t_infer += dt
            tags = tag_re.findall(raw)
            text = tag_re.sub("", raw).strip()
            rows.append({
                "idx": i, "track": tr,
                # start_s / end_s 用绝对时间（含窗口偏移），与课件事件的 t_start_s 同一基准，
                # 否则窗口非 0 时对齐会全部落空。
                "start_s": round(s / sr + ctx.win_start, 2),
                "end_s": round(e / sr + ctx.win_start, 2),
                "start_rel_s": round(s / sr, 2),
                "end_rel_s": round(e / sr, 2),
                "start_hms": mmss(s / sr + ctx.win_start),
                "end_hms": mmss(e / sr + ctx.win_start),
                "text": text, "text_raw": raw, "chars": len(text),
                "infer_s": round(dt, 3),
                "confidence": None,
                "confidence_note": "模型不输出置信度（非自回归），此处置 null",
                "needs_review": bool(_TRACK_REVIEW_RE.search(text)),
                "error": err,
            })
            if (i + 1) % 25 == 0 or i == 0:
                print(f"    [{i + 1}/{len(chunks)}] {mmss(s / sr)} {dt:.2f}s  {text[:36]}")

        out_jsonl = td / "transcript.jsonl"
        with open(out_jsonl, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

        # 人读版
        md = [f"# 转写 track {tr}（{t['file']}）", "",
              f"- 模型：SenseVoiceSmall（CPU），分块 {ctx.cfg['chunk_s']}s",
              f"- 时长 {mmss(total_s)}，{len(rows)} 块，{sum(r['chars'] for r in rows)} 字",
              f"- **时间戳为块级**：表示该段文字出现在该 {ctx.cfg['chunk_s']} 秒窗口内，"
              f"不是句子起始时刻",
              f"- **无置信度**：模型不输出置信度，需结合另一轨与音频复核",
              ""]
        for r in rows:
            flag = " ⚠待核实" if r["needs_review"] else ""
            md.append(f"**[{r['start_hms']}–{r['end_hms']}]**{flag}  {r['text'] or '（空）'}")
            md.append("")
        (td / "transcript.md").write_text("\n".join(md), encoding="utf-8")

        quality = _track_quality(rows, total_s)
        atomic_write_json(td / "quality.json", quality)

        rtf = round(t_infer / total_s, 4) if total_s else None
        summary["tracks"].append({
            "track": tr, "source_file": t["file"],
            "duration_s": round(total_s, 2),
            "n_chunks": len(rows), "chars": sum(r["chars"] for r in rows),
            "asr_s": round(t_infer, 2), "rtf": rtf, "errors": errors,
            "transcript_jsonl": str(out_jsonl),
            "transcript_md": str(td / "transcript.md"),
            "transcript_bytes": out_jsonl.stat().st_size,
            "quality": quality, "status": "SUCCESS",
        })
        print(f"  track {tr}: {sum(r['chars'] for r in rows)} 字  "
              f"RTF={rtf}  错误 {errors}  质检 "
              f"空块={quality['empty_chunks']} 越界={len(quality['out_of_range'])}")

    atomic_write_json(ctx.work / "tracks" / "processing_summary.json", summary)
    return {"tracks": [x["track"] for x in summary["tracks"]],
            "chars": {x["track"]: x["chars"] for x in summary["tracks"]},
            "rtf": {x["track"]: x["rtf"] for x in summary["tracks"]}}


def stage_tracks_slides(ctx: Ctx):
    """课件证据：内容判定课件路 → 抽帧 → 自适应裁剪 → 变化检测 → 代表帧。"""
    probe = read_json(ctx.work / "media.json")
    if not probe:
        raise RuntimeError("缺少 media.json，请先运行 probe 阶段")

    sdir = ctx.work / "slides_tracks"
    sdir.mkdir(parents=True, exist_ok=True)
    edit_crop = _edit_crop_tuple(ctx)
    step = float(ctx.cfg["sample_step_s"])
    win_start = ctx.win_start
    if ctx.win_end > 0:
        win_dur = ctx.win_end - ctx.win_start
    else:
        win_dur = max(e["duration_s"] for e in probe["files"]) - win_start
    if win_dur <= 0:
        raise RuntimeError(f"窗口无效：{win_start}–{ctx.win_end}")

    # ---- 1. 课件路判定：锐度 + 纯色占比，不按分辨率 ----
    n_probe = max(2, int(ctx.cfg["track_probe_frames"]))
    pdir = sdir / "probe"
    pdir.mkdir(parents=True, exist_ok=True)
    cands = []
    for entry in probe["files"]:
        fpath = Path(entry["file"])
        dur = entry["duration_s"]
        times = [round(20 + (dur - 40) * i / (n_probe - 1), 1) for i in range(n_probe)]
        feats = []
        for tt in times:
            if tt <= 0 or tt >= dur:
                continue
            img = pdir / f"{fpath.stem}_t{int(tt)}.jpg"
            if not img.exists():
                rc, _, _, _ = run_cmd([
                    FFMPEG, "-hide_banner", "-loglevel", "error",
                    "-ss", f"{tt:.2f}", "-i", str(fpath),
                    "-frames:v", "1", "-q:v", "2", "-y", str(img)], timeout=180)
                if rc != 0 or not img.exists():
                    continue
            try:
                lv, flat = xs._sharpness_and_flat(img)
            except Exception:  # noqa: BLE001
                continue
            feats.append({"t_s": tt, "lap_var": round(lv, 1),
                          "flat_ratio": round(flat, 3), "image": str(img)})
        vs = [s for s in entry["streams"] if s["type"] == "video"]
        cands.append({
            "track": fpath.stem, "file": fpath.name, "path": str(fpath),
            "width": vs[0].get("width") if vs else None,
            "height": vs[0].get("height") if vs else None,
            "duration_s": dur, "n_probed": len(feats),
            "lap_var_mean": round(sum(f["lap_var"] for f in feats) / len(feats), 1) if feats else 0.0,
            "flat_ratio_mean": round(sum(f["flat_ratio"] for f in feats) / len(feats), 3) if feats else 0.0,
            "frames": feats,
        })

    decision = {
        "rule": "取画面锐度(Laplacian方差)与最大量化色占比同时更高的一路；"
                "要求锐度倍数 ≥1.5 且胜出者纯色占比 ≥0.35，否则判为需人工确认。"
                "不依据分辨率判定——屏幕录制为像素级渲染，实拍为光学成像。",
        "ok": False, "reason": None,
    }
    sel = None
    valid = [c for c in cands if c["n_probed"] > 0 and c["lap_var_mean"] > 0]
    if len(valid) >= 2:
        a, b = valid[0], valid[1]
        sw = a if a["lap_var_mean"] >= b["lap_var_mean"] else b
        sl = b if sw is a else a
        fw = a if a["flat_ratio_mean"] >= b["flat_ratio_mean"] else b
        ratio = sw["lap_var_mean"] / max(sl["lap_var_mean"], 1e-6)
        decision.update({"sharpness_ratio": round(ratio, 2),
                         "sharp_win": sw["track"], "flat_win": fw["track"]})
        if sw["track"] == fw["track"] and ratio >= 1.5 and sw["flat_ratio_mean"] >= 0.35:
            decision["ok"] = True
            decision["reason"] = (f"track {sw['track']} 锐度 {sw['lap_var_mean']} 是对手 "
                                  f"{sl['lap_var_mean']} 的 {ratio:.1f} 倍，纯色占比 "
                                  f"{sw['flat_ratio_mean']} 亦更高 → 像素级渲染特征")
            sel = sw
        else:
            decision["reason"] = ("锐度胜出者与纯色占比胜出者不一致，"
                                  "或倍数不足 1.5 / 纯色占比 < 0.35，信号不足以自动判定")
    elif len(valid) == 1:
        sel = valid[0]
        decision.update({"ok": True, "reason": f"仅 {sel['track']} 一路有有效探测帧"})
    else:
        decision["reason"] = "有效探测帧不足"

    probe_meta = {
        "generated": now_iso(),
        "selection_rule": decision["rule"],
        "decision": decision,
        "selected_track": sel["track"] if sel else None,
        "selected_file": sel["file"] if sel else None,
        "tracks": cands,
    }
    atomic_write_json(sdir / "track_probe.json", probe_meta)
    if not sel:
        raise RuntimeError(f"课件路无法自动判定：{decision['reason']}。"
                           f"证据见 {sdir / 'track_probe.json'} 与 probe/ 目录")
    for c in cands:
        mark = " ←课件路" if c["track"] == sel["track"] else ""
        print(f"  track {c['track']}  {c['width']}x{c['height']}  "
              f"锐度={c['lap_var_mean']:>8}  纯色={c['flat_ratio_mean']:.3f}{mark}")
    print(f"  判定：{decision['reason']}")

    # ---- 2. 抽帧 ----
    src = Path(sel["path"])
    seq = sdir / f"_seq_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    seq.mkdir(parents=True, exist_ok=True)
    rc, _, se, dt_seq = run_cmd([
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-ss", f"{win_start:.2f}", "-t", f"{win_dur:.2f}", "-i", str(src),
        "-an", "-vf", f"fps=1/{step},scale=640:-1", "-q:v", "3",
        str(seq / "f_%05d.jpg"),
    ], timeout=7200)
    if rc != 0:
        raise RuntimeError(f"抽帧失败：{se[:400]}")
    frames = sorted(seq.glob("*.jpg"))
    if not frames:
        raise RuntimeError("抽帧结果为空")
    print(f"  抽帧 {len(frames)} 张，耗时 {dt_seq:.2f}s")

    # ---- 3. 变化检测（复用阶段四）----
    ch = xs.stage_change(frames, sdir, step, ctx.cfg["change_pct"],
                         ctx.cfg["burst_gap_s"], ctx.cfg["dedup_pct"], None, edit_crop)

    # ---- 4. 代表帧 + 自适应裁剪（复用阶段四，带窗口偏移）----
    slides, t_ext = xs.stage_slides(src, sdir, ch, step, None, edit_crop,
                                    t_offset=win_start)

    meta = {
        "generated": now_iso(),
        "source": {"track": sel["track"], "file": sel["file"], "path": str(src),
                   "width": sel["width"], "height": sel["height"],
                   "duration_s": sel["duration_s"]},
        "window": [win_start, round(win_start + win_dur, 2)],
        "selection_rule": decision["rule"],
        "decision": decision,
        "sample_step_s": step,
        "change_threshold_pct": ctx.cfg["change_pct"],
        "burst_gap_s": ctx.cfg["burst_gap_s"],
        "dedup_pct": ctx.cfg["dedup_pct"],
        "edit_crop_ratio": list(edit_crop),
        "n_sampled_frames": len(frames),
        "n_bursts": ch["n_bursts"],
        "n_segs": ch["n_segs"],
        "n_slides": len(slides),
        "mode_count": ch["mode_count"],
        "seq_dir": str(seq),
        "sample_decode_s": round(dt_seq, 2),
        "representative_extract_s": t_ext.get("extract_s"),
        "representative_crop_s": t_ext.get("crop_s"),
        "slides": slides,
    }
    atomic_write_json(sdir / "slides.json", meta)
    return {"selected_track": sel["track"], "n_slides": len(slides),
            "n_frames": len(frames), "decode_s": round(dt_seq, 2)}


def stage_tracks_ocr(ctx: Ctx):
    """对代表帧做 OCR（复用阶段四的 EasyOCR 实现）。"""
    sdir = ctx.work / "slides_tracks"
    smeta = read_json(sdir / "slides.json")
    if not smeta:
        raise RuntimeError("缺少 slides_tracks/slides.json，请先运行 tracks_slides 阶段")
    scale = float(ctx.cfg["track_ocr_scale"])
    # 幂等复用：上一轮 OCR 已跑完但后续阶段失败时，不必重跑这 2 分钟
    existing = read_json(sdir / "ocr.json")
    if (existing and existing.get("n_slides") == len(smeta["slides"])
            and abs(float(existing.get("ocr_scale") or 0) - scale) < 1e-6):
        print(f"  复用已有 ocr.json（{existing.get('n_slides')} 张，"
              f"{existing.get('ocr_total_s')}s，scale={scale}）")
        return {"n_images": existing["n_slides"], "n_ok": existing["n_ocr_ok"],
                "n_fail": existing["n_ocr_fail"], "ocr_s": existing["ocr_total_s"],
                "avg_s_per_image": existing["avg_ocr_s_per_slide"], "reused": True}
    meta = xs.stage_ocr(smeta["slides"], sdir, scale)
    # xs.stage_ocr 只返回汇总并原地写入 slides[i]["ocr"]，落盘由调用方负责
    payload = {**meta, "results": [
        {"slide_index": s["index"], "t_rep_s": s["t_rep_s"], **s["ocr"]}
        for s in smeta["slides"]
    ]}
    atomic_write_json(sdir / "ocr.json", payload)
    # xs.stage_ocr 返回的键是 n_slides / n_ocr_ok / n_ocr_fail / ocr_total_s
    return {"n_images": meta["n_slides"], "n_ok": meta["n_ocr_ok"],
            "n_fail": meta["n_ocr_fail"], "ocr_s": meta["ocr_total_s"],
            "avg_s_per_image": meta["avg_ocr_s_per_slide"]}


def stage_tracks_align(ctx: Ctx):
    """课件事件 × 双轨转写：文本去重 → 时间对齐 → 画面类型标注 → 时间线报告。"""
    sdir = ctx.work / "slides_tracks"
    smeta = read_json(sdir / "slides.json")
    ocr_full = read_json(sdir / "ocr.json")
    if not smeta or not ocr_full:
        raise RuntimeError("缺少 slides.json 或 ocr.json，请先运行 tracks_slides / tracks_ocr 阶段")
    inv = read_json(ctx.work / "tracks" / "media_inventory.json") or {"tracks": []}

    t0 = time.perf_counter()
    slides = smeta["slides"]
    # xs.stage_ocr 只写汇总，把逐帧 OCR 回填到 slides 供对齐与去重使用
    by_idx = {r["slide_index"]: r for r in ocr_full.get("results", [])}
    for s in slides:
        r = by_idx.get(s["index"])
        if r:
            s["ocr"] = {k: v for k, v in r.items() if k not in ("slide_index", "t_rep_s")}

    thresh = float(ctx.cfg["track_text_dedup"])
    if thresh > 0:
        kept, merged = xs.dedup_slides_by_text(slides, thresh)
    else:
        kept, merged = slides, []
    if merged:
        print(f"  文本去重：{len(slides)} → {len(kept)} 张（合并 {len(merged)} 张）")
        for m in merged:
            print(f"    slide {m['dropped_index']:03d} 并入 {m['kept_index']:03d}"
                  f"（Jaccard {m['jaccard']}）")

    tracks_meta = [{"track": t["track"]} for t in inv["tracks"]
                   if t.get("status") == "SUCCESS"]
    events = xs.stage_align(kept, ctx.work / "tracks", tracks_meta,
                            float(ctx.cfg["track_align_window_s"]))
    for e in events:
        ft, usable = xs.classify_frame(e.get("ocr_text", ""), e.get("ocr_n_lines", 0))
        e["frame_type"] = ft
        e["usable_for_study"] = usable

    with open(sdir / "timeline.jsonl", "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    probe_meta = read_json(sdir / "track_probe.json") or {
        "selected_track": smeta["source"]["track"], "tracks": [],
        "selection_rule": smeta.get("selection_rule", ""), "decision": {}}
    ocr_meta = {k: v for k, v in ocr_full.items() if k != "results"}
    ch = {"n_bursts": smeta.get("n_bursts"), "n_segs": smeta.get("n_segs"),
          "mode_count": smeta.get("mode_count"),
          "n_sampled": smeta.get("n_sampled_frames")}
    timing = {
        "抽帧解码": f"{smeta.get('sample_decode_s')}s",
        "代表帧抽取": f"{smeta.get('representative_extract_s')}s",
        "画布裁剪": f"{smeta.get('representative_crop_s')}s",
        "OCR": f"{ocr_meta.get('ocr_total_s')}s",
        "文本去重+时间对齐": f"{time.perf_counter() - t0:.2f}s",
    }
    xs.write_timeline_md(sdir / "timeline.md", events, probe_meta, ch, ocr_meta,
                         timing, inv["tracks"], None,
                         merged=merged, n_slides_raw=len(slides))

    usable = [e for e in events if e.get("usable_for_study", True)]
    noncw = len(events) - len(usable)
    print(f"  课件事件 {len(events)} 条，有效 {len(usable)} 条，非课件 {noncw} 条")
    return {
        "n_events": len(events), "n_usable": len(usable), "n_non_courseware": noncw,
        "n_merged": len(merged),
        "span": [usable[0]["t_start_hms"], usable[-1]["t_end_hms"]] if usable else None,
    }


# ---------------------------------------------------------------- 主流程
STAGE_FUNCS = {
    "probe": stage_probe,
    "audio": stage_audio,
    "asr": stage_asr,
    "slides": stage_slides,
    "ocr": stage_ocr,
    "notes": stage_notes,
    "verify": stage_verify,
    # 方案 A 追加
    "tracks_audio": stage_tracks_audio,
    "tracks_asr": stage_tracks_asr,
    "tracks_slides": stage_tracks_slides,
    "tracks_ocr": stage_tracks_ocr,
    "tracks_align": stage_tracks_align,
}

STAGE_OUTPUTS = {
    "probe": lambda c: [c.work / "media.json"],
    "audio": lambda c: [c.work / "audio" / "raw_16k.wav", c.work / "audio" / "asr_in.wav",
                        c.work / "audio" / "audio_meta.json"],
    "asr": lambda c: [c.work / "asr" / "segments.jsonl", c.work / "asr" / "asr_meta.json"],
    "slides": lambda c: [c.work / "slides" / "slides.json"] + [
        Path(s["image"]) for s in (read_json(c.work / "slides" / "slides.json") or {"slides": []})["slides"]],
    "ocr": lambda c: [c.work / "slides" / "ocr.json"],
    "notes": lambda c: [c.work / "课程笔记.md", c.work / "notes.json"],
    "verify": lambda c: [c.work / "验证报告.md", c.work / "verify.json"],
    # 方案 A 追加
    "tracks_audio": lambda c: [c.work / "tracks" / "media_inventory.json"] + [
        Path(t["wav_path"]) for t in
        (read_json(c.work / "tracks" / "media_inventory.json") or {"tracks": []})["tracks"]
        if t.get("status") == "SUCCESS" and t.get("wav_path")],
    "tracks_asr": lambda c: [c.work / "tracks" / "processing_summary.json"] + [
        c.work / "tracks" / t["track"] / "transcript.jsonl" for t in
        (read_json(c.work / "tracks" / "processing_summary.json") or {"tracks": []})["tracks"]],
    "tracks_slides": lambda c: [c.work / "slides_tracks" / "slides.json"] + [
        Path(s["image"]) for s in
        (read_json(c.work / "slides_tracks" / "slides.json") or {"slides": []})["slides"]
        if s.get("image")],
    "tracks_ocr": lambda c: [c.work / "slides_tracks" / "ocr.json"],
    "tracks_align": lambda c: [c.work / "slides_tracks" / "timeline.jsonl",
                               c.work / "slides_tracks" / "timeline.md"],
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-dir", default=str(DEFAULT_INPUT_DIR))
    ap.add_argument("--work-root", default=str(PROJECT_ROOT / "data"))
    ap.add_argument("--window", default="0:600", help="处理区间，秒，格式 start:end")
    ap.add_argument("--only", default="", help="只运行指定阶段（逗号分隔）")
    ap.add_argument("--force-stage", default="", help="强制重跑指定阶段（逗号分隔）")
    ap.add_argument("--print-state", action="store_true")
    ap.add_argument("--chunk-s", type=int, default=None)
    ap.add_argument("--sample-step-s", type=int, default=None)
    ap.add_argument("--change-pct", type=float, default=None)
    ap.add_argument("--keep-seq", action="store_true", default=None)
    ap.add_argument("--min-free-gb", type=float, default=None)
    # 方案 A 追加：双轨/课件阶段参数（键名需与 DEFAULTS 一致）
    ap.add_argument("--track-ocr-scale", type=float, default=None)
    ap.add_argument("--track-text-dedup", type=float, default=None)
    ap.add_argument("--track-align-window-s", type=float, default=None)
    ap.add_argument("--track-probe-frames", type=int, default=None)
    ap.add_argument("--track-edit-crop", default=None,
                    help="编辑视图画布几何 l,t,r,b，如 0.146,0.221,0.967,0.866")
    args = ap.parse_args()

    args.force_stage = [s.strip() for s in args.force_stage.split(",") if s.strip()]
    only = [s.strip() for s in args.only.split(",") if s.strip()]

    ctx = Ctx(args)

    if args.print_state:
        print(json.dumps(ctx.state, ensure_ascii=False, indent=2))
        return

    acquire_lock(ctx.work)
    fg = free_gb(ctx.work)
    print(f"任务 {ctx.task_id}  工作目录 {ctx.work}")
    print(f"输入 {len(ctx.files)} 个文件  窗口 {ctx.win_start}–{ctx.win_end}s  剩余空间 {fg:.1f} GB")
    if fg >= 0 and fg < ctx.cfg["min_free_gb"]:
        raise SystemExit(f"剩余空间 {fg:.1f} GB 低于阈值 {ctx.cfg['min_free_gb']} GB，拒绝启动")

    t_all = time.perf_counter()
    for name in STAGES:
        if only and name not in only:
            continue
        run_stage(ctx, name, lambda n=name: STAGE_FUNCS[n](ctx),
                  extra_fp={"stage": name, "cfg": ctx.cfg},
                  outputs_fn=lambda n=name: STAGE_OUTPUTS[n](ctx))
    print(f"\n全部阶段完成，总耗时 {time.perf_counter() - t_all:.1f}s")
    print(f"成果目录：{ctx.work}")


if __name__ == "__main__":
    main()

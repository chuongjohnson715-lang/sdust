# -*- coding: utf-8 -*-
"""Plan B 轻量取证共享原语（音频抽取 + 跳读抽帧 + 直链新鲜度守卫）。

为什么需要这个模块（2026-10-26 实测结论）：

1. **直链是「时效签名 URL」**，形如
   `https://storage.example.edu:<port>/videos/...mp4?filename=..&ts=<unix>&token=<签名>`
   其中 `ts` 是签名时刻，`token` 是签名。实测：用 urllib 对**同一 URL** 逐组合测试
   UA / Referer / Origin / Cookie 六种组合，**全部返回同一个 403**，而 TCP 层完全可达。
   结论：**403 与请求头无关，是 token/ts 过期**。所以
   - 加 UA/Referer 解决不了 403；
   - 必须「**现取现下**」：每次下载前重新取 captures 配置拿新直链；
   - 长期保存的 tracks.json 里的旧直链**不可复用**（本仓库据此加了新鲜度守卫）。

2. **抽帧必须流式去重**：把 440 帧原分辨率全累积在内存 ≈ 2.7GB/进程，
   是 OOM 主因；改为只保留前一帧灰度图做差异比较，内存降到常数级。

3. **抽帧用 OpenCV 单连接 seek**（`CAP_PROP_POS_MSEC`）优于 ffmpeg 逐帧：
   ffmpeg 每个进程重读 moov 索引，每帧 4.1MB；OpenCV 只开一次连接，每帧 1.2MB。

本模块只放「与平台无关」的纯函数与 I/O 辅助；平台交互（登录、取配置）在
`fetch_tracks_v2.py`，职责分离，便于单测。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

from coursereview.config import PLAYER_BASE
from coursereview.paths import FFMPEG

# 平台直链要求的浏览器 UA（实测仅在 ffmpeg 侧需要，urllib 侧无影响）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
REFERER = f"{PLAYER_BASE}/"
ORIGIN = PLAYER_BASE

# 直链签名年龄上限（秒）。超过即认为可能过期 → 重新取配置拿新直链。
# 取 30 分钟是保守值：实测 15 小时后必然 403，而正常单节处理也就几分钟。
MAX_URL_AGE_S = 1800

# 抽帧参数
STEP_S = 15          # 每 15 秒抽一帧
DIFF_PCT = 0.8       # 相邻帧差异 < 该百分比视为重复页，合并


# --------------------------------------------------------------------------
# 直链新鲜度
# --------------------------------------------------------------------------
def url_ts(url: str) -> int | None:
    """取直链签名里的 ts（unix 秒）。取不到返回 None。"""
    try:
        q = parse_qs(urlparse(url).query)
        v = q.get("ts", [None])[0]
        return int(v) if v is not None else None
    except Exception:
        return None


def url_age_s(url: str, now: float | None = None) -> float | None:
    """直链签名已过去多少秒。取不到 ts 返回 None（视为未知，不拦截）。"""
    ts = url_ts(url)
    if ts is None:
        return None
    return (now if now is not None else time.time()) - ts


def is_url_fresh(url: str, max_age_s: int = MAX_URL_AGE_S) -> bool:
    """直链是否还新鲜。未知 ts 视为新鲜（不因为缺字段而误杀）。"""
    age = url_age_s(url)
    return True if age is None else age <= max_age_s


def assert_fresh(url: str, max_age_s: int = MAX_URL_AGE_S) -> None:
    age = url_age_s(url)
    if age is not None and age > max_age_s:
        raise RuntimeError(
            f"直链已过期（签名年龄 {age/60:.0f} 分钟 > {max_age_s/60:.0f} 分钟）："
            "必须重新取 captures 配置拿新直链，不能复用旧 URL")


def probe_url(url: str, timeout: int = 15) -> tuple[int, str]:
    """只读前 1KB 探测直链是否可用。返回 (status, 简短说明)。

    status==0 表示网络层失败（连不上）；403 表示 token 过期；206/200 表示可用。
    """
    req = urllib.request.Request(url, method="GET")
    req.add_header("Range", "bytes=0-1023")
    req.add_header("User-Agent", UA)
    req.add_header("Referer", REFERER)
    req.add_header("Origin", ORIGIN)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            n = len(r.read())
            return r.status, f"{n}B {r.headers.get('Content-Type', '')}"
    except urllib.error.HTTPError as e:
        try:
            body = e.read(120).decode("utf-8", "replace").replace("\n", " ")
        except Exception:
            body = ""
        return e.code, (e.reason or "") + (" | " + body if body else "")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def preflight(url: str) -> tuple[bool, str]:
    """下载前的可用性检查。返回 (是否可用, 原因)。"""
    status, detail = probe_url(url)
    if status in (200, 206):
        return True, f"HTTP {status} {detail}"
    if status == 403:
        return False, "HTTP 403 —— token/ts 已过期，必须重新取 captures 配置"
    return False, f"直链不可用 HTTP {status} {detail}"


# --------------------------------------------------------------------------
# 音频抽取（INSTRUCTOR 轨：只要最佳音轨，画面不要）
# --------------------------------------------------------------------------
def fetch_audio(url: str, out_wav: Path, timeout: int = 5400) -> tuple[bool, str]:
    """从 INSTRUCTOR 直链只抽音频（16k 单声道 PCM）。返回 (是否成功, 说明)。"""
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(FFMPEG), "-hide_banner", "-loglevel", "error", "-icy", "0",
        "-user_agent", UA,
        "-headers", f"Referer: {REFERER}\r\nOrigin: {ORIGIN}\r\n",
        "-i", url, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        "-y", str(out_wav),
    ]
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "抽音频超时"
    if r.returncode == 0 and out_wav.exists() and out_wav.stat().st_size > 10000:
        mb = out_wav.stat().st_size / 1e6
        return True, f"{mb:.0f}MB（{time.time()-t0:.0f}s）"
    err = (r.stderr or "")[:200]
    hint = "（多半是 token 过期，需重新取配置）" if "403" in err else ""
    return False, f"rc={r.returncode} {err}{hint}"


# --------------------------------------------------------------------------
# 跳读抽帧（ENCODER 轨：屏录 PPT）
# --------------------------------------------------------------------------
def _diff_gray(ga: np.ndarray, gb: np.ndarray) -> float:
    """两灰度图差异占比（%）。输入已是灰度，避免重复转换。"""
    if ga.shape != gb.shape:
        gb = cv2.resize(gb, (ga.shape[1], ga.shape[0]))
    d = cv2.absdiff(ga, gb)
    return float((d > 25).sum()) / d.size * 100.0


def _hms(s: float) -> str:
    s = int(s)
    return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"


def sample_frames(
    enc_url: str,
    out_dir: Path,
    dur_s: float,
    step_s: int = STEP_S,
    diff_pct: float = DIFF_PCT,
) -> tuple[list[dict], int]:
    """OpenCV 单连接 seek 跳读抽帧 + 像素去重。返回 (代表页记录, 采样次数)。

    **流式去重**：只保留前一帧的灰度图做差异比较，命中即落盘。
    原实现把 440 帧原分辨率全部累积在内存（≈2.7GB/进程），是内存瓶颈主因。

    失败时抛 RuntimeError（消息含 403 提示，便于上层触发「重取配置」重试）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = f"user_agent;{UA}"
    cap = cv2.VideoCapture(enc_url)
    if not cap.isOpened():
        raise RuntimeError(
            "无法打开 ENCODER 流（多半是 token 过期或 URL 无效，"
            "需重新取 captures 配置拿新直链）")
    recs: list[dict] = []
    n = 0
    t = 0.0
    idx = 0
    prev_gray = None
    try:
        while t < dur_s:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, fr = cap.read()
            if ok and fr is not None:
                gray = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
                if prev_gray is None or _diff_gray(prev_gray, gray) >= diff_pct:
                    idx += 1
                    p = out_dir / f"slide_{idx:03d}.jpg"
                    cv2.imwrite(str(p), fr)
                    recs.append({
                        "index": idx, "t_rep_s": float(t), "t_rep_hms": _hms(t),
                        "image": str(p), "image_rel": f"slides/{p.name}",
                        "image_kb": round(p.stat().st_size / 1024, 1),
                    })
                prev_gray = gray
            n += 1
            t += step_s
    finally:
        cap.release()
    if not recs:
        raise RuntimeError("抽帧结果为空（连接被拒或视频损坏，需重新取配置重试）")
    return recs, n


def write_slides_json(out_dir: Path, recs: list[dict], n: int, step_s: int, source: str) -> Path:
    p = out_dir / "slides.json"
    p.write_text(json.dumps({
        "source": source, "sample_step_s": step_s, "n_samples": n,
        "n_slides": len(recs), "slides": recs,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return p

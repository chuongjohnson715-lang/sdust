# -*- coding: utf-8 -*-
"""
阶段四：课件证据提取 + 双轨转写时间对齐

设计原则
--------
1. 复用 pipeline.py 已验证算法（变化检测 / burst 合并 / 代表帧 / EasyOCR / 黑边裁剪），
   不改动 pipeline.py 任何一行。
2. 轨道判定用「画面内容证据」（OCR 文本密度 + Office UI 关键词），不用分辨率猜。
3. 裁剪自适应：全屏放映模式裁黑边，编辑视图模式裁 Office UI 框。
4. OCR 原样保存，不做任何「修正」；无法识别的内容留空并标记，绝不补写。
5. 画面出现时间 != 讲解时间，用时间窗口关联而非精确对齐。

用法
----
python scripts/extract_slides.py --day-dir "E:\\课程复盘数据\\...\\2026-10-20"
python scripts/extract_slides.py --day-dir ... --limit-s 600     # 冒烟测试
python scripts/extract_slides.py --day-dir ... --skip-probe      # 复用已有判定
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from paths import FFMPEG as _FFMPEG, FFPROBE as _FFPROBE

FFMPEG = Path(_FFMPEG)
FFPROBE = Path(_FFPROBE)

# ---- 复用 pipeline.py 的算法常量（保持与既有实现一致）----
SAMPLE_STEP_S = 2.0      # 抽帧间隔（秒）
CHANGE_PCT = 2.0         # 变化像素占比阈值（%）
BURST_GAP_S = 6.0        # 变化簇合并间隔（秒）
DEDUP_PCT = 1.0          # 代表帧去重阈值（%）
DIFF_THR = 25            # 灰度差阈值（_diff_pct 默认值）

# 编辑视图模式的画布几何（本课程 1024x768 实测标定，可用 --edit-crop 覆盖）
# 上边界 0.221 = 170px：功能区（含"字体/段落/绘图/编辑"一行）实际延伸到 y≈145，
# 画布顶部在 y≈170。用 0.143 会把功能区末行带进画面，导致 OCR 混入 UI 文字。
EDIT_CROP_RATIO = (0.146, 0.221, 0.967, 0.866)

# Office / 桌面 UI 关键词：命中即说明该路是电脑屏幕录制
UI_KEYWORDS = [
    "文件", "开始", "插入", "设计", "切换", "动画", "幻灯片放映", "审阅", "视图",
    "单击此处添加备注", "中文", "登录", "共享", "PowerPoint", "剪贴板", "字体", "段落",
    "绘图", "编辑", "新建幻灯片", "版式", "重置", "形状", "排列", "查找", "替换",
]

WATERMARK_RATIO = 0.93   # 底部 7% 为水印区（复用 stage_ocr 规则）

# 画面类型判定（基于 OCR 文本，仅用于标注，不删任何证据）
DESKTOP_HINTS = ["Visual Fo", "Photoshop", "腾讯", "WorkBuddy", "回收站", "此电脑",
                 "资源管理器", "微信", "Chrome", "Microsoft Edge", "政策文件解读"]
NO_SIGNAL_HINTS = ["无视频信号", "无信号", "NO SIGNAL"]


def classify_frame(ocr_text: str, n_lines: int) -> tuple[str, bool]:
    """返回 (frame_type, usable_for_study)。

    slide      = 课件画面（PPT 放映或编辑视图）
    desktop    = 电脑桌面 / 资源管理器（老师切出演示软件）
    no_signal  = 录制信号中断
    blank      = 过渡帧，OCR 无任何文字
    """
    t = ocr_text or ""
    if any(k in t for k in NO_SIGNAL_HINTS):
        return "no_signal", False
    if n_lines == 0:
        return "blank", False
    if sum(1 for k in DESKTOP_HINTS if k in t) >= 1 and len(t) < 120:
        return "desktop", False
    return "slide", True


# ------------------------------------------------------------------ 工具
def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def mmss(sec: float) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def run_cmd(cmd, timeout=7200):
    t0 = time.perf_counter()
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    return p.returncode, p.stdout, p.stderr, time.perf_counter() - t0


# ------------------------------------------------------------------ 阶段 1: 轨道判定
def _sharpness_and_flat(img_path: Path):
    """画面锐度（Laplacian 方差）与最大量化色占比。

    屏幕录制 = 像素级渲染 → 边缘极锐利 + 大面积纯色背景；
    实拍摄像 = 光学成像 → 边缘模糊 + 色彩渐变。
    这两个特征与分辨率无关（实测 1920x1080 的摄像机路锐度反而只有
    1024x768 屏幕录制路的 1/5~1/8），因此可避免「按分辨率猜」。
    """
    import numpy as np
    from PIL import Image

    im = Image.open(img_path).convert("L")
    a = np.asarray(im, dtype=np.float32)
    # Laplacian 方差（等价于 cv2.Laplacian(...).var()，避免引入 opencv）
    lap = np.zeros_like(a)
    lap[1:-1, 1:-1] = (a[:-2, 1:-1] + a[2:, 1:-1] + a[1:-1, :-2] + a[1:-1, 2:]
                       - 4.0 * a[1:-1, 1:-1])
    lv = float(lap[1:-1, 1:-1].var())
    q = (a // 16).astype(np.uint8)
    _, cnts = np.unique(q, return_counts=True)
    flat = float(cnts.max() / q.size)
    return lv, flat


def probe_track(video: Path, times, out_dir: Path, tag: str, reader):
    """抽若干帧，测锐度/纯色占比/OCR 文本量/UI 关键词，用于判定该路性质。"""
    import numpy as np
    from PIL import Image

    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for t in times:
        img = out_dir / f"{tag}_t{int(t)}.jpg"
        if not img.exists():
            rc, _, se, _ = run_cmd([
                FFMPEG, "-hide_banner", "-loglevel", "error",
                "-ss", f"{t:.2f}", "-i", str(video),
                "-frames:v", "1", "-q:v", "2", "-y", str(img),
            ], timeout=180)
            if rc != 0 or not img.exists():
                rows.append({"t_s": t, "error": f"抽帧失败: {se[:200]}"})
                continue
        try:
            lv, flat = _sharpness_and_flat(img)
        except Exception as e:  # noqa: BLE001
            rows.append({"t_s": t, "error": f"图像特征失败: {type(e).__name__}: {e}"})
            continue
        try:
            im = Image.open(img).convert("RGB")
            res = reader.readtext(np.array(im), detail=1, paragraph=False)
        except Exception as e:  # noqa: BLE001
            rows.append({"t_s": t, "image": str(img), "lap_var": round(lv, 1),
                         "flat_ratio": round(flat, 3),
                         "error": f"OCR 失败: {type(e).__name__}: {e}"})
            continue
        texts = [r[1] for r in res]
        confs = [float(r[2]) for r in res]
        joined = "".join(texts)
        ui_hits = sum(1 for kw in UI_KEYWORDS if kw in joined)
        rows.append({
            "t_s": t,
            "image": str(img),
            "lap_var": round(lv, 1),
            "flat_ratio": round(flat, 3),
            "n_boxes": len(res),
            "chars": len(joined),
            "avg_conf": round(sum(confs) / len(confs), 3) if confs else 0.0,
            "ui_keyword_hits": ui_hits,
            "ui_keywords_found": [kw for kw in UI_KEYWORDS if kw in joined],
            "sample_text": joined[:160],
        })

    ok = [r for r in rows if "error" not in r]
    with_feat = [r for r in rows if "lap_var" in r]
    agg = {
        "n_probed": len(rows),
        "n_ok": len(ok),
        "lap_var_mean": round(sum(r["lap_var"] for r in with_feat) / len(with_feat), 1) if with_feat else 0.0,
        "flat_ratio_mean": round(sum(r["flat_ratio"] for r in with_feat) / len(with_feat), 3) if with_feat else 0.0,
        "chars_per_frame": round(sum(r["chars"] for r in ok) / len(ok), 1) if ok else 0.0,
        "boxes_per_frame": round(sum(r["n_boxes"] for r in ok) / len(ok), 1) if ok else 0.0,
        "avg_conf": round(sum(r["avg_conf"] for r in ok) / len(ok), 3) if ok else 0.0,
        "ui_frames": sum(1 for r in ok if r["ui_keyword_hits"] > 0),
        "ui_keywords_all": sorted({kw for r in ok for kw in r["ui_keywords_found"]}),
        "frames": rows,
    }
    return agg


def stage_probe(videos, out_dir: Path, n: int):
    """轨道判定：用画面内容证据（锐度 + 纯色占比为主，OCR 为辅），不按分辨率判定。"""
    import easyocr

    print("[1/6] 轨道判定（画面内容证据：锐度 / 纯色占比 / 文本量 / UI 关键词）")
    t0 = time.perf_counter()
    reader = easyocr.Reader(["ch_sim", "en"], gpu=False,
                            download_enabled=False, verbose=False)
    t_init = time.perf_counter() - t0
    print(f"  EasyOCR 初始化 {t_init:.2f}s")

    probe_dir = out_dir / "probe"
    result = {"generated": now_str(), "reader_init_s": round(t_init, 2), "tracks": []}
    for tr in videos:
        dur = tr["duration_s"]
        # 均匀覆盖全片（避开片头 20s 与片尾 20s）；探测只抽少数帧，不受 --limit-s 限制
        times = [round(20 + (dur - 40) * i / max(n - 1, 1), 1) for i in range(n)]
        agg = probe_track(Path(tr["path"]), times, probe_dir, f"track{tr['track']}", reader)
        agg.update({"track": str(tr["track"]), "file": tr["file"],
                    "width": tr["width"], "height": tr["height"],
                    "duration_s": tr["duration_s"]})
        result["tracks"].append(agg)
        print(f"  track {tr['track']}  {tr['width']}x{tr['height']}  "
              f"锐度={agg['lap_var_mean']:>8}  纯色={agg['flat_ratio_mean']:.3f}  "
              f"字符/帧={agg['chars_per_frame']:>6}  conf={agg['avg_conf']:.3f}  "
              f"UI命中帧={agg['ui_frames']}/{agg['n_ok']}")

    # ---- 判定：相对比较，取锐度与纯色占比都更高的一路 ----
    ts = [t for t in result["tracks"] if t["n_ok"] > 0 and t["lap_var_mean"] > 0]
    decision = {"rule": "取画面锐度(Laplacian方差)与最大纯色占比同时更高的一路；"
                        "要求锐度倍数 ≥ 1.5 且胜出者纯色占比 ≥ 0.35，否则判为需人工确认。"
                        "OCR 文本量与 Office UI 关键词仅作佐证，不单独决定。",
                "reason": None, "ok": False}
    if len(ts) >= 2:
        a, b = ts[0], ts[1]
        sharp_win = a if a["lap_var_mean"] >= b["lap_var_mean"] else b
        sharp_lose = b if sharp_win is a else a
        flat_win = a if a["flat_ratio_mean"] >= b["flat_ratio_mean"] else b
        ratio = sharp_win["lap_var_mean"] / max(sharp_lose["lap_var_mean"], 1e-6)
        decision["sharpness_ratio"] = round(ratio, 2)
        decision["sharp_win"] = sharp_win["track"]
        decision["flat_win"] = flat_win["track"]
        if sharp_win["track"] == flat_win["track"] and ratio >= 1.5 \
                and sharp_win["flat_ratio_mean"] >= 0.35:
            decision["ok"] = True
            decision["reason"] = (f"track {sharp_win['track']} 锐度 {sharp_win['lap_var_mean']} "
                                  f"是对手 {sharp_lose['lap_var_mean']} 的 {ratio:.1f} 倍，"
                                  f"纯色占比 {sharp_win['flat_ratio_mean']} 亦更高 → 像素级渲染特征，判为屏幕录制")
            result["selected_track"] = sharp_win["track"]
            result["selected_file"] = sharp_win["file"]
        else:
            decision["reason"] = ("锐度胜出者与纯色占比胜出者不一致，或倍数不足 1.5 / 纯色占比 < 0.35，"
                                  "信号不足以自动判定")
    else:
        decision["reason"] = "有效探测帧不足 2 路"
    result["decision"] = decision
    result["selection_rule"] = decision["rule"]
    result["probe_total_s"] = round(time.perf_counter() - t0, 2)

    # 无论成败都落盘，保证失败可追溯
    atomic_write_json(out_dir / "track_probe.json", result)

    if not decision["ok"]:
        raise RuntimeError(f"课件路无法自动判定：{decision['reason']}。"
                           f"请查看 {out_dir / 'track_probe.json'} 与 probe/ 下的证据帧后人工确认，"
                           f"并用 --track N 指定。")
    print(f"  → 课件路 = track {result['selected_track']} ({result['selected_file']})，"
          f"锐度倍数 {decision['sharpness_ratio']}，耗时 {result['probe_total_s']:.1f}s")
    return result


# ------------------------------------------------------------------ 阶段 2: 抽帧
def stage_sample(video: Path, out_dir: Path, limit_s: float | None, step: float):
    print(f"[2/6] 抽帧（每 {step}s 一帧，缩放 640 宽）")
    # 每次用全新子目录，避免删除旧帧（批量删除会被安全策略拦截，且残留旧帧会污染计数）
    seq = out_dir / f"_seq_{now_ts()}"
    seq.mkdir(parents=True, exist_ok=True)
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error"]
    if limit_s:
        cmd += ["-t", f"{limit_s:.2f}"]
    cmd += ["-i", str(video), "-an", "-vf", f"fps=1/{step},scale=640:-1",
            "-q:v", "3", str(seq / "f_%05d.jpg")]
    rc, _, se, dt = run_cmd(cmd, timeout=7200)
    if rc != 0:
        raise RuntimeError(f"抽帧失败：{se[:400]}")
    frames = sorted(seq.glob("*.jpg"))
    if not frames:
        raise RuntimeError("抽帧结果为空")
    print(f"  {len(frames)} 帧，耗时 {dt:.2f}s（{len(frames)/max(dt,1e-6):.0f} 帧/秒）")
    return frames, dt


# ------------------------------------------------------------------ 阶段 3: 裁剪
def crop_frame(im, edit_crop_ratio=EDIT_CROP_RATIO):
    """自适应裁剪。返回 (cropped, mode, box)。

    fullscreen: 检出上下（或左右）黑边 → 按黑边裁
    edit      : 无黑边（PowerPoint 编辑视图）→ 按标定几何裁 Office UI
    """
    import numpy as np

    g = np.asarray(im.convert("L"), dtype=np.float32)
    h, w = g.shape
    row = g.mean(axis=1)

    top = 0
    while top < int(h * 0.30) and row[top] < 18:
        top += 1
    bot = h - 1
    while bot > int(h * 0.70) and row[bot] < 18:
        bot -= 1
    has_letterbox = (top > h * 0.03) or (bot < h * 0.97)

    if has_letterbox:
        col = g[top:bot + 1].mean(axis=0)
        left = 0
        while left < int(w * 0.10) and col[left] < 18:
            left += 1
        right = w - 1
        while right > int(w * 0.90) and col[right] < 18:
            right -= 1
        if right - left < w * 0.5:
            left, right = 0, w - 1
        box = (left, top, right + 1, bot + 1)
        mode = "fullscreen"
    else:
        l, t, r, b = edit_crop_ratio
        box = (int(w * l), int(h * t), int(w * r), int(h * b))
        mode = "edit"

    if box[2] - box[0] < 64 or box[3] - box[1] < 64:
        return im, "nocrop", (0, 0, im.width, im.height)
    return im.crop(box), mode, box


def _img_gray_512(path: Path, edit_crop_ratio=EDIT_CROP_RATIO):
    """复用 _img_gray_512 的思路：自适应裁剪后 resize 512x340。"""
    import numpy as np
    from PIL import Image
    im = Image.open(path)
    cropped, mode, _ = crop_frame(im, edit_crop_ratio)
    return np.asarray(cropped.convert("L").resize((512, 340)), dtype=np.float32), mode


def _diff_pct(a, b, thr=DIFF_THR):
    import numpy as np
    return float((np.abs(a - b) > thr).mean() * 100.0)


# ------------------------------------------------------------------ 阶段 4: 变化检测
def stage_change(frames, out_dir: Path, step, change_pct, burst_gap_s,
                 dedup_pct, limit_s, edit_crop_ratio):
    print("[3/6] 画面变化检测")
    t0 = time.perf_counter()
    grays, modes = [], []
    for p in frames:
        g, m = _img_gray_512(p, edit_crop_ratio)
        grays.append(g)
        modes.append(m)
    t_gray = time.perf_counter() - t0
    print(f"  灰度化 {len(grays)} 帧，耗时 {t_gray:.2f}s")

    t0 = time.perf_counter()
    changes = [0.0]
    for i in range(1, len(grays)):
        changes.append(_diff_pct(grays[i - 1], grays[i]))
    t_diff = time.perf_counter() - t0
    print(f"  逐帧差分耗时 {t_diff:.2f}s，"
          f"变化率 min={min(changes):.2f}% max={max(changes):.2f}% "
          f"mean={sum(changes)/len(changes):.2f}%")

    gap_frames = max(1, int(burst_gap_s / step))
    burst_idx = [i for i, c in enumerate(changes) if c > change_pct]
    bursts = []
    for i in burst_idx:
        if bursts and i - bursts[-1][-1] <= gap_frames:
            bursts[-1].append(i)
        else:
            bursts.append([i])

    segs, prev_end = [], 0
    for b in bursts:
        start, end = prev_end, b[-1]
        if end > start:
            segs.append((start, end))
        prev_end = b[-1]
    if prev_end < len(frames) - 1:
        segs.append((prev_end, len(frames) - 1))

    reps = []
    for (s, e) in segs:
        mid = (s + e) // 2
        if reps and _diff_pct(grays[reps[-1]["mid"]], grays[mid]) < dedup_pct:
            reps[-1]["end_idx"] = e
            continue
        reps.append({"start_idx": s, "end_idx": e, "mid": mid})

    print(f"  变化簇 {len(bursts)} 个 → 稳定区间 {len(segs)} 个 → 去重后代表帧 {len(reps)} 张")
    mode_count = {}
    for m in modes:
        mode_count[m] = mode_count.get(m, 0) + 1
    print(f"  画面模式分布：{mode_count}")

    return {
        "reps": reps, "changes": changes, "modes": modes,
        "n_bursts": len(bursts), "n_segs": len(segs),
        "gray_s": round(t_gray, 2), "diff_s": round(t_diff, 2),
        "mode_count": mode_count,
        "change_min": round(min(changes), 2), "change_max": round(max(changes), 2),
        "change_mean": round(sum(changes) / len(changes), 2),
    }


# ------------------------------------------------------------------ 阶段 5: 代表帧抽取 + OCR
def stage_slides(video: Path, out_dir: Path, ch, step, limit_s, edit_crop_ratio,
                 t_offset: float = 0.0):
    """抽取代表帧（原分辨率）+ 画布裁剪。

    t_offset：抽帧时的起始偏移（秒）。pipeline 传入 win_start 时使用；
    extract_slides 独立运行时窗口恒从 0 开始，故默认 0.0，保持向后兼容。
    """
    print("[4/6] 代表帧抽取（原分辨率）+ 画布裁剪")
    import numpy as np
    from PIL import Image

    raw_dir = out_dir / "raw"
    canvas_dir = out_dir / "canvas"
    # 不删除旧目录：直接覆盖同名文件。批量删除会被安全策略拦截，
    # 且 slides.json 是权威索引，残留的旧文件不影响正确性。
    for d in (raw_dir, canvas_dir):
        d.mkdir(parents=True, exist_ok=True)

    slides = []
    t_extract = 0.0
    t_crop = 0.0
    for k, r in enumerate(ch["reps"], start=1):
        t_rep = t_offset + r["mid"] * step
        if limit_s and t_rep > limit_s:
            continue
        raw = raw_dir / f"slide_{k:03d}.jpg"
        t0 = time.perf_counter()
        rc, _, se, _ = run_cmd([
            FFMPEG, "-hide_banner", "-loglevel", "error",
            "-ss", f"{t_rep:.2f}", "-i", str(video),
            "-frames:v", "1", "-q:v", "2", "-y", str(raw),
        ], timeout=180)
        t_extract += time.perf_counter() - t0
        if rc != 0 or not raw.exists():
            slides.append({
                "index": k, "t_rep_s": round(t_rep, 1), "t_rep_hms": mmss(t_rep),
                "image": None, "image_raw": None,
                "extract_error": f"ffmpeg rc={rc}: {se[:200]}",
            })
            print(f"  [WARN] 代表帧抽取失败 t={t_rep:.1f}s")
            continue

        t0 = time.perf_counter()
        im = Image.open(raw)
        cropped, mode, box = crop_frame(im, edit_crop_ratio)
        canvas = canvas_dir / f"slide_{k:03d}.jpg"
        cropped.save(canvas, quality=95)
        t_crop += time.perf_counter() - t0

        slides.append({
            "index": k,
            "image": str(canvas),
            "image_raw": str(raw),
            "source_video": str(video),
            "t_start_s": round(t_offset + r["start_idx"] * step, 1),
            "t_end_s": round(t_offset + r["end_idx"] * step, 1),
            "t_rep_s": round(t_rep, 1),
            "t_start_hms": mmss(t_offset + r["start_idx"] * step),
            "t_end_hms": mmss(t_offset + r["end_idx"] * step),
            "t_rep_hms": mmss(t_rep),
            "duration_s": round((r["end_idx"] - r["start_idx"]) * step, 1),
            "frame_mode": mode,
            "crop_box": list(box),
            "image_kb": round(canvas.stat().st_size / 1024, 1),
            "change_at_entry_pct": round(ch["changes"][r["start_idx"]], 2),
            "extract_error": None,
        })

    print(f"  抽取 {len(slides)} 张，抽帧耗时 {t_extract:.2f}s，裁剪耗时 {t_crop:.2f}s")
    return slides, {"extract_s": round(t_extract, 2), "crop_s": round(t_crop, 2)}


def stage_ocr(slides, out_dir: Path, ocr_scale: float):
    print(f"[5/6] OCR（EasyOCR ch_sim+en, CPU, 放大 {ocr_scale}x）")
    import easyocr
    import numpy as np
    from PIL import Image

    t0 = time.perf_counter()
    reader = easyocr.Reader(["ch_sim", "en"], gpu=False,
                            download_enabled=False, verbose=False)
    t_init = time.perf_counter() - t0
    print(f"  初始化 {t_init:.2f}s")

    ok, fail = 0, 0
    for s in slides:
        if not s.get("image"):
            s["ocr"] = {"error": "无截图，跳过 OCR", "text": "", "n_boxes": 0,
                        "avg_conf": 0.0, "boxes": [], "ocr_s": 0.0}
            fail += 1
            continue
        try:
            im = Image.open(s["image"]).convert("RGB")
            if ocr_scale and ocr_scale != 1.0:
                im = im.resize((int(im.width * ocr_scale), int(im.height * ocr_scale)),
                               Image.LANCZOS)
            t0 = time.perf_counter()
            res = reader.readtext(np.array(im), detail=1, paragraph=False)
            dt = time.perf_counter() - t0
        except Exception as e:  # noqa: BLE001
            s["ocr"] = {"error": f"{type(e).__name__}: {e}", "text": "", "n_boxes": 0,
                        "avg_conf": 0.0, "boxes": [], "ocr_s": 0.0}
            fail += 1
            print(f"  [WARN] slide {s['index']:03d} OCR 失败：{e}")
            continue

        boxes, texts, edge_ui = [], [], []
        ch_w, ch_h = im.width, im.height
        for bbox, txt, conf in res:
            ys = [pt[1] for pt in bbox]
            xs = [pt[0] for pt in bbox]
            cy = (min(ys) + max(ys)) / 2
            is_wm = bool(cy > ch_h * WATERMARK_RATIO)
            # 编辑视图下裁剪框仍可能漏进边缘 UI（功能区末行 / 任务栏）。
            # 判据：短文本 + 命中 UI 词汇 + 位于画面极边缘 → 判为残余 UI，排除出正文。
            is_edge_ui = False
            if s.get("frame_mode") == "edit" and len(txt) <= 6:
                near_edge = (max(ys) < ch_h * 0.05 or min(ys) > ch_h * 0.96
                             or max(xs) < ch_w * 0.03 or min(xs) > ch_w * 0.97)
                if near_edge and any(kw in txt for kw in UI_KEYWORDS):
                    is_edge_ui = True
            b = {
                "text": txt, "conf": round(float(conf), 3),
                "x": int(min(xs)), "y": int(min(ys)),
                "w": int(max(xs) - min(xs)), "h": int(max(ys) - min(ys)),
                "watermark_region": is_wm,
                "edge_ui_region": is_edge_ui,
            }
            boxes.append(b)
            if is_wm:
                continue
            if is_edge_ui:
                edge_ui.append(txt)
                continue
            texts.append(b)

        # 按行（y 聚类）再按 x 排序，还原阅读顺序
        texts.sort(key=lambda b: (round(b["y"] / max(b["h"], 8)), b["x"]))
        body = [b["text"] for b in texts]
        avg_conf = (sum(b["conf"] for b in texts) / len(texts)) if texts else 0.0

        s["ocr"] = {
            "error": None,
            "text": "".join(body),
            "n_boxes": len(boxes),
            "n_body_boxes": len(texts),
            "n_watermark_boxes": sum(1 for b in boxes if b["watermark_region"]),
            "n_edge_ui_boxes": len(edge_ui),
            "edge_ui_text": edge_ui,
            "avg_conf": round(avg_conf, 3),
            "min_conf": round(min((b["conf"] for b in texts), default=0.0), 3),
            "boxes": boxes,
            "ocr_s": round(dt, 3),
            "ocr_input_size": [im.width, im.height],
        }
        ok += 1
        print(f"  slide {s['index']:03d}  {dt:5.2f}s  {len(texts):3d} 行  "
              f"conf={avg_conf:.2f}  {''.join(body)[:36]}")

    total = sum(s["ocr"].get("ocr_s", 0.0) for s in slides)
    meta = {
        "engine": "easyocr (ch_sim+en), CPU",
        "reader_init_s": round(t_init, 2),
        "n_slides": len(slides),
        "n_ocr_ok": ok,
        "n_ocr_fail": fail,
        "ocr_total_s": round(total, 2),
        "avg_ocr_s_per_slide": round(total / max(ok, 1), 2),
        "ocr_scale": ocr_scale,
        "watermark_rule": f"底部 {(1-WATERMARK_RATIO)*100:.0f}% 区域判定为水印区，不计入正文",
        "note": "OCR 文本原样保存，未做任何修正/补全；识别不全的公式与下标需回看原图。",
    }
    print(f"  成功 {ok} / 失败 {fail}，OCR 总耗时 {total:.2f}s（均 {meta['avg_ocr_s_per_slide']}s/张）")
    return meta


# ------------------------------------------------------------------ 阶段 6: 时间对齐
def _ngrams(s: str, n: int = 3):
    s = re.sub(r"\s+", "", s)
    return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}


def dedup_slides_by_text(slides, thresh: float = 0.55, min_chars: int = 20):
    """按 OCR 文本 3-gram Jaccard 去重。

    画面像素级去重（dedup_pct）无法识别「同一页在编辑视图与放映视图各出现一次」：
    两种模式的裁剪框与缩放比不同，像素差很大，但内容其实是同一页。

    做法：两两相似度 >= thresh 连边 → 连通分量分组 → 每组保留**停留时间最长**的
    那一帧作为代表（它最可能对应老师真正讲解该页的时刻），其余标记 duplicate_of。
    被合并的图片仍保留在 canvas/ 下，只是不进时间线。

    实测本课程：同页 edit/edit ≈ 0.84，同页 edit/fullscreen ≈ 0.61~0.63，不同页 < 0.5。
    """
    cand = [s for s in slides if len(s["ocr"].get("text", "")) >= min_chars]
    short = [s for s in slides if len(s["ocr"].get("text", "")) < min_chars]

    n = len(cand)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    grams = [_ngrams(s["ocr"].get("text", "")) for s in cand]
    sims = {}
    for i in range(n):
        if not grams[i]:
            continue
        for j in range(i + 1, n):
            if not grams[j]:
                continue
            inter = len(grams[i] & grams[j])
            if inter == 0:
                continue
            jac = inter / len(grams[i] | grams[j])
            if jac >= thresh:
                sims[(i, j)] = round(jac, 3)
                union(i, j)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    kept, merged = [], []
    for members in groups.values():
        if len(members) == 1:
            kept.append(cand[members[0]])
            continue
        # 保留停留最久者；并列时取 OCR 字符更多的
        rep_i = max(members, key=lambda i: (cand[i].get("duration_s", 0),
                                            len(cand[i]["ocr"].get("text", ""))))
        rep = cand[rep_i]
        for i in members:
            if i == rep_i:
                continue
            s = cand[i]
            # 组内相似度取该帧与代表帧的 Jaccard
            a, b = min(i, rep_i), max(i, rep_i)
            merged.append({
                "dropped_index": s["index"], "kept_index": rep["index"],
                "jaccard": sims.get((a, b), None),
                "t_dropped_s": s["t_rep_s"], "dropped_mode": s.get("frame_mode"),
                "dropped_duration_s": s.get("duration_s"),
            })
            s["duplicate_of"] = rep["index"]
            rep["t_start_s"] = min(rep["t_start_s"], s["t_start_s"])
            rep["t_end_s"] = max(rep["t_end_s"], s["t_end_s"])
        rep["t_start_hms"] = mmss(rep["t_start_s"])
        rep["t_end_hms"] = mmss(rep["t_end_s"])
        rep["duration_s"] = round(rep["t_end_s"] - rep["t_start_s"], 1)
        rep["merged_from"] = sorted(cand[i]["index"] for i in members if i != rep_i)
        kept.append(rep)

    kept.extend(short)
    kept.sort(key=lambda s: s["t_rep_s"])
    merged.sort(key=lambda m: m["t_dropped_s"])
    return kept, merged


def load_transcript(path: Path):
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def stage_align(slides, tracks_dir: Path, tracks_meta, window_s: float):
    print(f"[6/6] 与双轨转写时间对齐（窗口 ±{window_s:.0f}s）")
    tr = {}
    for t in tracks_meta:
        p = tracks_dir / str(t["track"]) / "transcript.jsonl"
        rows = load_transcript(p)
        tr[str(t["track"])] = rows
        print(f"  track {t['track']}: 载入 {len(rows)} 块")

    def pick(rows, lo, hi):
        out = []
        for r in rows:
            s, e = r.get("start_s", 0), r.get("end_s", 0)
            if e <= lo or s >= hi:
                continue
            out.append({
                "idx": r.get("idx"),
                "start_s": s, "end_s": e,
                "start_hms": mmss(s),
                "text": r.get("text", ""),
                "needs_review": r.get("needs_review", False),
            })
        return out

    events = []
    for s in slides:
        if not s.get("image"):
            continue
        t_rep = s["t_rep_s"]
        ftype, usable = classify_frame(s["ocr"].get("text", ""),
                                       s["ocr"].get("n_body_boxes", 0))
        ev = {
            "slide_index": s["index"],
            "frame_type": ftype,
            "usable_for_study": usable,
            "t_start_s": s["t_start_s"], "t_end_s": s["t_end_s"],
            "t_rep_s": t_rep,
            "t_start_hms": s["t_start_hms"],
            "t_end_hms": s["t_end_hms"],
            "t_rep_hms": s["t_rep_hms"],
            "image": s["image"],
            "image_raw": s["image_raw"],
            "source_video": s["source_video"],
            "frame_mode": s["frame_mode"],
            "ocr_text": s["ocr"].get("text", ""),
            "ocr_n_lines": s["ocr"].get("n_body_boxes", 0),
            "ocr_n_edge_ui": s["ocr"].get("n_edge_ui_boxes", 0),
            "ocr_edge_ui_text": s["ocr"].get("edge_ui_text", []),
            "ocr_avg_conf": s["ocr"].get("avg_conf", 0.0),
            "ocr_error": s["ocr"].get("error"),
            "extract_error": s.get("extract_error"),
            "merged_from": s.get("merged_from", []),
            # 画面停留期间两路各说了什么
            "speech_in_span": {},
            # 代表帧 ±窗口 两路各说了什么（画面出现 != 讲解时刻）
            "speech_near_rep": {},
        }
        for tk, rows in tr.items():
            ev["speech_in_span"][tk] = pick(rows, s["t_start_s"], s["t_end_s"])
            ev["speech_near_rep"][tk] = pick(rows, t_rep - window_s, t_rep + window_s)
        # 两路在该画面区间内的字符数（仅统计，不用于裁决对错）
        ev["chars_in_span"] = {
            tk: sum(len(c["text"]) for c in v)
            for tk, v in ev["speech_in_span"].items()
        }
        events.append(ev)

    events.sort(key=lambda e: e["t_rep_s"])
    print(f"  生成 {len(events)} 条课件事件")
    return events


# ------------------------------------------------------------------ 报告
def _brief(chunks, n_chunks=1, width=34):
    """从对齐的转写块里取前 n 块做摘要。"""
    if not chunks:
        return "（无）"
    parts = []
    for c in chunks[:n_chunks]:
        t = (c.get("text") or "").replace("|", "\\|").replace("\n", " ")
        if len(t) > width:
            t = t[:width] + "…"
        parts.append(t)
    return " ".join(parts) if parts else "（无）"


def write_timeline_md(path: Path, events, probe, ch, ocr_meta, timing, videos, limit_s,
                      merged=None, n_slides_raw=None):
    lines = []
    lines.append("# 课件证据时间线（阶段四）")
    lines.append("")
    lines.append(f"- 生成时间：{now_str()}")
    lines.append(f"- 课件路：track {probe['selected_track']}（{probe.get('selected_file', '')}）")
    lines.append(f"- 判定规则：{probe['selection_rule']}")
    lines.append(f"- 抽帧间隔 {SAMPLE_STEP_S}s，变化阈值 {CHANGE_PCT}%，"
                 f"簇合并 {BURST_GAP_S}s，像素去重 {DEDUP_PCT}%")
    lines.append(f"- 课件事件数：{len(events)}")
    lines.append("")
    lines.append("> **画面出现时间不等于老师讲解该内容的准确时间。** 表中「同期讲解」列是"
                 "该画面**停留区间内**的转写摘要，只作线索，不作因果断言。")
    lines.append("> **OCR 结果不是课件原文。** 公式、上下标、希腊字母、矩阵识别常出错，"
                 "本阶段未做任何修正或补全，缺失处保持缺失，关键内容必须回看原图。")
    lines.append("")

    # 轨道判定证据
    lines.append("## 一、轨道判定证据（画面内容，非分辨率）")
    lines.append("")
    if probe.get("tracks"):
        lines.append("| 轨道 | 分辨率 | 时长 | 锐度(Laplacian方差) | 最大纯色占比 | 字符/帧 | 平均置信度 | UI 关键词命中帧 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for t in probe["tracks"]:
            mark = " **←课件路**" if str(t["track"]) == str(probe["selected_track"]) else ""
            cpf = t.get("chars_per_frame")
            conf = t.get("avg_conf")
            ui, nok = t.get("ui_frames"), t.get("n_ok")
            ui_s = f"{ui}/{nok}" if ui is not None and nok is not None else "—"
            lines.append(f"| {t['track']}{mark} | {t['width']}×{t['height']} | "
                         f"{mmss(t['duration_s'])} | {t.get('lap_var_mean', '—')} | "
                         f"{t.get('flat_ratio_mean', '—')} | "
                         f"{cpf if cpf is not None else '—'} | "
                         f"{conf if conf is not None else '—'} | {ui_s} |")
        lines.append("")
        lines.append("> 判定不依据分辨率：屏幕录制为像素级渲染，边缘极锐利且有大面积纯色背景；"
                     "实拍摄像为光学成像，边缘模糊、色彩渐变。本课程中**分辨率更高的一路"
                     "锐度反而低 5–8 倍**，正说明按分辨率判定会出错。")
        lines.append("")
        lines.append("> 「字符/帧」「平均置信度」「UI 命中帧」为 OCR 佐证项；"
                     "pipeline 内嵌运行时为省算力跳过该步，显示为「—」，"
                     "不影响主判据（锐度 + 纯色占比）。")
        lines.append("")
        for t in probe["tracks"]:
            kws_all = t.get("ui_keywords_all") or []
            kws = "、".join(kws_all[:18]) if kws_all else "（未做 OCR 佐证）"
            lines.append(f"- **track {t['track']}** 命中的 UI 关键词：{kws}")
    else:
        lines.append(f"- 判定方式：{probe.get('selection_rule')}")
    dec = probe.get("decision") or {}
    if dec.get("reason"):
        lines.append("")
        lines.append(f"- **判定依据**：{dec['reason']}")
    lines.append("")

    # 事件清单（含双轨讲解对齐）
    lines.append("## 二、课件事件清单（按时间排序，含双轨讲解对齐）")
    lines.append("")
    tracks_sorted = sorted({tk for e in events for tk in e.get("speech_in_span", {})})
    head = "| # | 时间 | 停留 | 模式 | 类型 | 画面 OCR（截断） |"
    for tk in tracks_sorted:
        head += f" track{tk} 同期讲解 |"
    head += " 标记 |"
    lines.append(head)
    lines.append("|" + "---|" * (6 + len(tracks_sorted) + 1))
    for e in events:
        txt = (e["ocr_text"] or "").replace("|", "\\|").replace("\n", " ")
        if len(txt) > 56:
            txt = txt[:56] + "…"
        if not txt:
            txt = "**（OCR 无可识别文字，需看原图）**"
        flags = []
        if not e.get("usable_for_study", True):
            flags.append("非课件")
        if e["ocr_error"]:
            flags.append("OCR失败")
        if e.get("extract_error"):
            flags.append("抽帧失败")
        if e["ocr_n_lines"] == 0:
            flags.append("无文字")
        elif e["ocr_avg_conf"] < 0.5:
            flags.append("低置信")
        if e.get("merged_from"):
            flags.append(f"含合并{e['merged_from']}")
        row = (f"| {e['slide_index']:03d} | {e['t_rep_hms']} | "
               f"{int(e['t_end_s'] - e['t_start_s'])}s | {e['frame_mode']} | "
               f"{e.get('frame_type', 'slide')} | {txt} |")
        for tk in tracks_sorted:
            row += f" {_brief(e.get('speech_in_span', {}).get(tk, []))} |"
        row += f" {'、'.join(flags) if flags else '—'} |"
        lines.append(row)
    lines.append("")

    # 有效授课区间
    usable = [e for e in events if e.get("usable_for_study", True)]
    noncw = [e for e in events if not e.get("usable_for_study", True)]
    if usable:
        lines.append(f"**有效课件画面 {len(usable)} 条，时间跨度 "
                     f"{usable[0]['t_start_hms']} – {usable[-1]['t_end_hms']}。**")
    if noncw:
        kinds = {}
        for e in noncw:
            kinds[e.get("frame_type", "?")] = kinds.get(e.get("frame_type", "?"), 0) + 1
        lines.append(f"非课件画面 {len(noncw)} 条（{kinds}），已标记 `usable_for_study=false`，"
                     f"不应纳入复习材料：")
        for e in noncw:
            lines.append(f"- {e['slide_index']:03d}  {e['t_start_hms']}–{e['t_end_hms']}  "
                         f"{e.get('frame_type')}  `{Path(e['image_raw']).name}`")
    lines.append("")

    # 低置信度 / 无法确认清单
    bad = [e for e in events
           if e["ocr_n_lines"] == 0 or e["ocr_avg_conf"] < 0.5 or e["ocr_error"]]
    lines.append("## 三、无法确认 / 低置信度清单（不得作为事实依据）")
    lines.append("")
    if not bad:
        lines.append("（无）")
    else:
        lines.append("| # | 时间 | 原因 | 原图 |")
        lines.append("|---|---|---|---|")
        for e in bad:
            why = []
            if e["ocr_error"]:
                why.append(f"OCR 报错：{e['ocr_error']}")
            if e["ocr_n_lines"] == 0:
                why.append("OCR 未识别到任何文字")
            elif e["ocr_avg_conf"] < 0.5:
                why.append(f"平均置信度 {e['ocr_avg_conf']:.2f} < 0.5")
            lines.append(f"| {e['slide_index']:03d} | {e['t_rep_hms']} | "
                         f"{'；'.join(why)} | `{Path(e['image_raw']).name}` |")
    lines.append("")

    # 耗时
    lines.append("## 四、耗时与产物")
    lines.append("")
    lines.append("| 环节 | 耗时 |")
    lines.append("|---|---|")
    for k, v in timing.items():
        lines.append(f"| {k} | {v} |")
    lines.append("")
    lines.append(f"- 抽帧总数 {ch['n_sampled'] if 'n_sampled' in ch else '—'}")
    lines.append(f"- 变化簇 {ch['n_bursts']} → 稳定区间 {ch['n_segs']} → "
                 f"代表帧 {n_slides_raw if n_slides_raw else len(events)}")
    lines.append(f"- 画面模式分布：{ch['mode_count']}")
    lines.append(f"- OCR：{ocr_meta['n_ocr_ok']} 成功 / {ocr_meta['n_ocr_fail']} 失败，"
                 f"均 {ocr_meta['avg_ocr_s_per_slide']}s/张")
    if merged:
        lines.append(f"- 文本去重合并 {len(merged)} 张（同一页多次出现，保留停留最久者）：")
        for m in merged:
            lines.append(f"  - slide {m['dropped_index']:03d} 并入 {m['kept_index']:03d}"
                         f"（Jaccard {m['jaccard']}，模式 {m['dropped_mode']}，"
                         f"t={mmss(m['t_dropped_s'])}，停留 {m.get('dropped_duration_s')}s）")
    n_edge = sum(e.get("ocr_n_edge_ui", 0) for e in events)
    if n_edge:
        lines.append(f"- 已排除的边缘 UI 文本框：{n_edge} 个（编辑视图下功能区/任务栏残余）")
    lines.append("")

    # 限制声明
    lines.append("## 五、本阶段限制（必读）")
    lines.append("")
    lines.append("1. **画面出现时间 ≠ 讲解时间**：抽帧只能定位「画面何时变化」，"
                 "老师可能在画面切换前已开始讲、或切换后才补充。")
    lines.append("2. **OCR 不是课件原文**：公式、上下标、希腊字母、矩阵、积分号识别率低，"
                 "本阶段未做任何补全或修正，缺失处保持缺失。")
    lines.append("3. **未生成考试重点**：本阶段只做证据提取与时间关联。")
    lines.append("4. **低置信度标记**：`ocr_avg_conf < 0.5` 或 `ocr_n_lines == 0` 的事件，"
                 "其文本不可作为事实依据。")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="课件证据提取 + 双轨转写时间对齐")
    ap.add_argument("--day-dir", required=True, help="归档日目录，如 E:\\课程复盘数据\\...\\2026-10-20")
    ap.add_argument("--tracks-dir", default=None, help="双轨转写目录（默认 {day}/output/tracks）")
    ap.add_argument("--out-dir", default=None, help="输出目录（默认 {day}/output/slides）")
    ap.add_argument("--log-dir", default=None, help="日志目录（默认 {day}/logs）")
    ap.add_argument("--sample-step-s", type=float, default=SAMPLE_STEP_S)
    ap.add_argument("--change-pct", type=float, default=CHANGE_PCT)
    ap.add_argument("--burst-gap-s", type=float, default=BURST_GAP_S)
    ap.add_argument("--dedup-pct", type=float, default=DEDUP_PCT)
    ap.add_argument("--ocr-scale", type=float, default=1.5,
                    help="OCR 前放大倍数（2.0 更准但慢约 1.8 倍）")
    ap.add_argument("--align-window-s", type=float, default=30.0)
    ap.add_argument("--text-dedup", type=float, default=0.55,
                    help="OCR 文本 3-gram Jaccard 去重阈值（0 关闭）。"
                         "实测同页 edit/edit≈0.84、edit/fullscreen≈0.62、不同页<0.5")
    ap.add_argument("--probe-frames", type=int, default=8, help="轨道判定每路抽帧数")
    ap.add_argument("--track", default=None, help="手动指定课件路（跳过自动判定）")
    ap.add_argument("--edit-crop", default=None,
                    help="编辑视图裁剪比例 l,t,r,b（默认 0.146,0.143,0.969,0.866）")
    ap.add_argument("--limit-s", type=float, default=None, help="只处理前 N 秒（冒烟测试）")
    ap.add_argument("--skip-probe", action="store_true", help="复用已有 track_probe.json")
    ap.add_argument("--regen-md-only", action="store_true",
                    help="只用已有 slides.json/ocr.json/timeline.jsonl 重新生成 timeline.md，"
                         "不重跑抽帧与 OCR")
    args = ap.parse_args()

    day = Path(args.day_dir)
    tracks_dir = Path(args.tracks_dir) if args.tracks_dir else day / "output" / "tracks"
    out_dir = Path(args.out_dir) if args.out_dir else day / "output" / "slides"
    log_dir = Path(args.log_dir) if args.log_dir else day / "logs"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    edit_crop = EDIT_CROP_RATIO
    if args.edit_crop:
        edit_crop = tuple(float(x) for x in args.edit_crop.split(","))
        assert len(edit_crop) == 4, "--edit-crop 需要 4 个逗号分隔的数"

    inv = read_json(tracks_dir / "media_inventory.json")
    if not inv:
        print(f"[ERR] 缺少 {tracks_dir / 'media_inventory.json'}，请先跑 transcribe_tracks.py")
        return 3
    videos = inv["tracks"]

    # ---- 仅重生成报告：不重跑抽帧/OCR ----
    if args.regen_md_only:
        smeta = read_json(out_dir / "slides.json")
        ocr_full = read_json(out_dir / "ocr.json")
        if not smeta or not ocr_full:
            print(f"[ERR] 缺少 slides.json 或 ocr.json，无法只重生成报告")
            return 3
        probe = read_json(out_dir / "track_probe.json") or {
            "selected_track": smeta["source"]["track"], "tracks": [],
            "selection_rule": smeta.get("selection_rule", "")}
        events = []
        tj = out_dir / "timeline.jsonl"
        if tj.exists():
            for line in tj.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    events.append(json.loads(line))
        # 补齐/刷新画面类型（旧版本 jsonl 可能没有该字段）
        for e in events:
            ftype, usable = classify_frame(e.get("ocr_text", ""), e.get("ocr_n_lines", 0))
            e["frame_type"] = ftype
            e["usable_for_study"] = usable
        with tj.open("w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        ch = {"n_bursts": smeta.get("n_bursts"), "n_segs": smeta.get("n_segs"),
              "mode_count": smeta.get("mode_count"),
              "n_sampled": smeta.get("n_sampled_frames")}
        ocr_meta = {k: v for k, v in ocr_full.items() if k != "results"}
        timing = {}
        logs = sorted(log_dir.glob("slides_*.json"))
        if logs:
            timing = (read_json(logs[-1]) or {}).get("timing", {})
        write_timeline_md(out_dir / "timeline.md", events, probe, ch, ocr_meta, timing,
                          videos, None, merged=smeta.get("text_dedup_merged"),
                          n_slides_raw=smeta.get("n_slides"))
        print(f"已重新生成 {out_dir / 'timeline.md'}（{len(events)} 条事件）")
        return 0

    t_all = time.perf_counter()
    timing = {}

    # 1. 轨道判定
    if args.track is not None:
        probe = {"selected_track": str(args.track), "tracks": [],
                 "selection_rule": f"用户手动指定 --track {args.track}",
                 "decision": {"rule": "手动指定", "ok": True,
                              "reason": f"--track {args.track}"},
                 "probe_total_s": 0.0}
        if (out_dir / "track_probe.json").exists():
            old = read_json(out_dir / "track_probe.json") or {}
            probe["tracks"] = old.get("tracks", [])
        print(f"[1/6] 手动指定课件路 = track {args.track}")
        atomic_write_json(out_dir / "track_probe.json", probe)
    elif args.skip_probe and (out_dir / "track_probe.json").exists():
        probe = read_json(out_dir / "track_probe.json")
        if not probe.get("selected_track"):
            raise RuntimeError(f"已有 track_probe.json 未包含判定结果，请去掉 --skip-probe 重跑")
        print(f"[1/6] 复用已有判定：课件路 = track {probe['selected_track']}")
    else:
        probe = stage_probe(videos, out_dir, args.probe_frames)
    timing["轨道判定"] = f"{probe.get('probe_total_s', 0):.1f}s"

    sel = next(v for v in videos if str(v["track"]) == str(probe["selected_track"]))
    src = Path(sel["path"])
    print(f"  课件源：{src}（{sel['width']}×{sel['height']}, {mmss(sel['duration_s'])}）")

    # 2. 抽帧
    frames, dt_sample = stage_sample(src, out_dir, args.limit_s, args.sample_step_s)
    timing["抽帧解码"] = f"{dt_sample:.1f}s"

    # 3. 变化检测
    ch = stage_change(frames, out_dir, args.sample_step_s, args.change_pct,
                      args.burst_gap_s, args.dedup_pct, args.limit_s, edit_crop)
    ch["n_sampled"] = len(frames)
    timing["灰度化"] = f"{ch['gray_s']}s"
    timing["逐帧差分"] = f"{ch['diff_s']}s"

    # 4. 代表帧 + 裁剪
    slides, t_ext = stage_slides(src, out_dir, ch, args.sample_step_s,
                                 args.limit_s, edit_crop)
    timing["代表帧抽取"] = f"{t_ext['extract_s']}s"
    timing["画布裁剪"] = f"{t_ext['crop_s']}s"

    # 5. OCR
    ocr_meta = stage_ocr(slides, out_dir, args.ocr_scale)
    timing["OCR"] = f"{ocr_meta['ocr_total_s']}s（含初始化 {ocr_meta['reader_init_s']}s）"

    # 5.5 文本级去重（合并「同一页在编辑视图/放映视图各出现一次」）
    n_before = len(slides)
    if args.text_dedup and args.text_dedup > 0:
        kept_slides, merged = dedup_slides_by_text(slides, args.text_dedup)
    else:
        kept_slides, merged = slides, []
    if merged:
        print(f"  文本去重：{n_before} → {len(kept_slides)} 张（合并 {len(merged)} 张）")
        for m in merged:
            print(f"    slide {m['dropped_index']:03d} 与 {m['kept_index']:03d} 相似度 "
                  f"{m['jaccard']} → 合并")

    # 6. 对齐
    t0 = time.perf_counter()
    events = stage_align(kept_slides, tracks_dir, videos, args.align_window_s)
    timing["时间对齐"] = f"{time.perf_counter() - t0:.2f}s"

    # 序列帧保留（不删除）：沙箱会在批量删除中途阻断进程，导致后续产物无法写出。
    # 保留下来也更利于复核「变化检测是否漏帧」，代价约 40 KB/帧。
    if frames:
        seq_dir = frames[0].parent
        seq_mb = sum(f.stat().st_size for f in frames) / 1024 / 1024
        print(f"  序列帧保留于 {seq_dir}（{len(frames)} 帧，{seq_mb:.1f} MB，可手动清理）")

    # 落盘
    slides_meta = {
        "generated": now_str(),
        "source": {"track": probe["selected_track"], "file": probe["selected_file"],
                   "path": str(src), "width": sel["width"], "height": sel["height"],
                   "duration_s": sel["duration_s"]},
        "selection_rule": probe["selection_rule"],
        "sample_step_s": args.sample_step_s,
        "change_threshold_pct": args.change_pct,
        "burst_gap_s": args.burst_gap_s,
        "dedup_pct": args.dedup_pct,
        "edit_crop_ratio": list(edit_crop),
        "n_sampled_frames": len(frames),
        "n_bursts": ch["n_bursts"],
        "n_segs": ch["n_segs"],
        "n_slides": len(slides),
        "n_slides_after_text_dedup": len(kept_slides),
        "text_dedup_threshold": args.text_dedup,
        "text_dedup_merged": merged,
        "mode_count": ch["mode_count"],
        "change_series_pct": [round(c, 2) for c in ch["changes"]],
        "slides": slides,
    }
    atomic_write_json(out_dir / "slides.json", slides_meta)
    atomic_write_json(out_dir / "ocr.json", {**ocr_meta, "results": [
        {"slide_index": s["index"], "t_rep_s": s["t_rep_s"], **s["ocr"]} for s in slides
    ]})
    with (out_dir / "timeline.jsonl").open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    total_s = time.perf_counter() - t_all
    timing["总耗时"] = f"{total_s:.1f}s"
    write_timeline_md(out_dir / "timeline.md", events, probe, ch, ocr_meta, timing,
                      videos, args.limit_s, merged=merged, n_slides_raw=len(slides))

    # 处理日志
    log = {
        "generated": now_str(),
        "day_dir": str(day),
        "args": vars(args),
        "source_video": str(src),
        "selected_track": probe["selected_track"],
        "n_sampled_frames": len(frames),
        "seq_dir": str(frames[0].parent) if frames else None,
        "n_slides": len(slides),
        "n_events": len(events),
        "ocr_ok": ocr_meta["n_ocr_ok"],
        "ocr_fail": ocr_meta["n_ocr_fail"],
        "timing": timing,
        "total_s": round(total_s, 2),
        "outputs": {
            "slides_json": str(out_dir / "slides.json"),
            "ocr_json": str(out_dir / "ocr.json"),
            "timeline_jsonl": str(out_dir / "timeline.jsonl"),
            "timeline_md": str(out_dir / "timeline.md"),
            "canvas_dir": str(out_dir / "canvas"),
            "raw_dir": str(out_dir / "raw"),
            "probe_json": str(out_dir / "track_probe.json"),
        },
    }
    ts = now_ts()
    atomic_write_json(log_dir / f"slides_{ts}.json", log)

    md = ["# 课件证据提取日志", "", f"- 时间：{now_str()}",
          f"- 课件源：`{src}`", f"- 判定：{probe['selection_rule']}", "",
          "| 环节 | 耗时 |", "|---|---|"]
    md += [f"| {k} | {v} |" for k, v in timing.items()]
    md += ["", f"- 抽帧 {len(frames)} 帧 → 变化簇 {ch['n_bursts']} → "
               f"稳定区间 {ch['n_segs']} → 代表帧 {len(slides)}",
           f"- OCR {ocr_meta['n_ocr_ok']} 成功 / {ocr_meta['n_ocr_fail']} 失败",
           f"- 课件事件 {len(events)} 条", ""]
    (log_dir / f"slides_{ts}.md").write_text("\n".join(md), encoding="utf-8")

    print("\n完成")
    print(f"  课件路      track {probe['selected_track']} ({probe['selected_file']})")
    print(f"  代表帧      {len(slides)} 张 → {out_dir / 'canvas'}")
    print(f"  课件事件    {len(events)} 条 → {out_dir / 'timeline.jsonl'}")
    print(f"  总耗时      {total_s:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())

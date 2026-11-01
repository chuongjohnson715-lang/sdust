# -*- coding: utf-8 -*-
"""对已抽的关键帧做像素去重（离线，不重新下载）。

场景：keyframe_frames 早期版本按 441 个采样点各存一张，未去重；
OCR 要对 441 页识别会慢 5 倍。本脚本读现有 slides.json + 图片，
按「与上一保留帧灰度差 >25 的像素占比 ≥0.8%」筛选，重建成去重后的 slides/。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import cv2


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: dedupe_slides.py <light_dir>"); return 2
    light = Path(sys.argv[1])
    sd = light / "slides"
    sj = sd / "slides.json"
    if not sj.exists():
        print("无 slides.json"); return 3
    d = json.loads(sj.read_text(encoding="utf-8"))
    old = d.get("slides") or []
    print(f"原 {len(old)} 页，开始去重...")

    out_dir = light / "_slides_dedup"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    recs, idx, prev = [], 0, None
    for s in old:
        p = Path(s["image"])
        if not p.exists():
            p = sd / Path(s["image_rel"]).name
        im = cv2.imread(str(p))
        if im is None:
            continue
        gray = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
        if prev is None:
            keep = True
        else:
            gb = gray
            if prev.shape != gb.shape:
                gb = cv2.resize(gb, (prev.shape[1], prev.shape[0]))
            diff = cv2.absdiff(prev, gb)
            keep = (diff > 25).sum() / diff.size * 100.0 >= 0.8
        if keep:
            idx += 1
            dst = out_dir / f"slide_{idx:03d}.jpg"
            shutil.copyfile(p, dst)
            recs.append({**s, "index": idx, "image": str(dst),
                         "image_rel": f"slides/{dst.name}",
                         "image_kb": round(dst.stat().st_size / 1024, 1)})
        prev = gray

    print(f"去重后 {len(recs)} 页（保留率 {len(recs)/max(len(old),1)*100:.0f}%）")

    # 替换：删旧 slides 目录，改名
    bak = light / "_slides_old"
    if bak.exists():
        shutil.rmtree(bak)
    sd.rename(bak)
    out_dir.rename(sd)
    shutil.rmtree(bak)

    d["slides"] = recs
    d["n_slides"] = len(recs)
    d["dedup"] = {"from": len(old), "to": len(recs), "threshold_pct": 0.8}
    (sd / "slides.json").write_text(json.dumps(d, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    print("完成：slides/ 已替换为去重版")
    return 0


if __name__ == "__main__":
    sys.exit(main())

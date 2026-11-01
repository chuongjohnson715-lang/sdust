# -*- coding: utf-8 -*-
"""把逐节交付的原样五件套，按「日期在最前」的命名规则归档到课程目录。

产物路径：`<out>/原样/<课程名>/MM-DD_第X讲-<章或主题>/`

用法（默认预演，不写盘）：

    # 1) 先导出一份映射模板（从已有交付目录 + 清单猜日期/活动 id，讲次与主题留空）
    python scripts/stage_originals.py --deliver ./_work/deliver --course "示例课程" --init-map --map map.csv

    # 2) 手工补齐 lecture / topic 两列（讲次用阿拉伯数字，主题写可确证的章号或主题）
    #    然后归档
    python scripts/stage_originals.py --deliver ./_work/deliver --course "示例课程" \
        --map map.csv --out ./courses --apply

映射表列：date,activity_id,lecture,topic
  date         YYYY-MM-DD
  activity_id  平台活动 id（只用于定位源目录）
  lecture      讲次数字，如 3
  topic        第三段：章号（如 第2章）或主题；查不到就写主题，**不许猜章号**
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CN = "零一二三四五六七八九"


def cn_num(n: int) -> str:
    """1 → 一，12 → 十二，20 → 二十，123 → 一百二十三（讲次一般 < 100）。"""
    if n < 10:
        return CN[n]
    if n < 20:
        return "十" + (CN[n % 10] if n % 10 else "")
    if n < 100:
        return CN[n // 10] + "十" + (CN[n % 10] if n % 10 else "")
    return str(n)


def safe_seg(s: str, limit: int = 40) -> str:
    out = "".join(ch for ch in (s or "").strip() if ch not in '\\/:*?"<>|')
    return out.replace(" ", "_")[:limit] or "未命名"


def read_map(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    need = {"date", "activity_id", "lecture", "topic"}
    missing = need - set(rows[0].keys() if rows else [])
    if missing:
        raise SystemExit(f"映射表缺列：{', '.join(sorted(missing))}")
    return rows


def cmd_init_map(deliver: Path, course: str, out: Path) -> int:
    rows = []
    for d in sorted(deliver.iterdir()):
        if not d.is_dir() or not (d / "_自检.json").exists():
            continue
        date, _, aid = d.name.partition("_")
        rows.append({"date": date, "activity_id": aid, "lecture": "", "topic": ""})
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "activity_id", "lecture", "topic"])
        w.writeheader()
        w.writerows(rows)
    print(f"已写出映射模板：{out}（{len(rows)} 行；请补 lecture / topic）")
    return 0


def cmd_stage(deliver: Path, course: str, rows: list[dict], out_root: Path, apply: bool) -> int:
    dest_root = out_root / "原样" / course
    planned: list[tuple[Path, Path]] = []
    skipped = 0
    for r in rows:
        date, aid, lecture, topic = (r.get(k, "").strip() for k in
                                     ("date", "activity_id", "lecture", "topic"))
        if not (date and aid and lecture and topic):
            print(f"[SKIP] 映射不完整：{date} {aid} 讲次={lecture!r} 主题={topic!r}")
            skipped += 1
            continue
        src = deliver / f"{date}_{aid}"
        if not (src / "_自检.json").exists():
            print(f"[SKIP] 无交付自检：{src.name}")
            skipped += 1
            continue
        if not (src / "_自检.json").read_text(encoding="utf-8").strip():
            print(f"[SKIP] 自检为空：{src.name}")
            skipped += 1
            continue
        try:
            ok = json.loads((src / "_自检.json").read_text(encoding="utf-8")).get("ok")
        except Exception:
            ok = False
        if not ok:
            print(f"[SKIP] 自检未通过：{src.name}")
            skipped += 1
            continue
        name = f"{date[5:]}_第{cn_num(int(lecture))}讲-{safe_seg(topic)}"
        dst = dest_root / name
        if dst.exists():
            print(f"[EXIST] {dst.relative_to(out_root).as_posix()}")
            skipped += 1
            continue
        planned.append((src, dst))

    print(f"待归档 {len(planned)} 套，跳过 {skipped} 套")
    for src, dst in planned:
        print("  ", src.name, "->", dst.relative_to(out_root).as_posix())
    if not apply:
        print("（预演，未写入；确认无误后加 --apply）")
        return 0
    for src, dst in planned:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst)
    print(f"已归档 {len(planned)} 套 → {dest_root}")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="原样五件套归档（日期在最前）")
    ap.add_argument("--deliver", default=str(REPO / "_work" / "deliver"),
                    help="逐节交付目录（子目录名 date_activity）")
    ap.add_argument("--course", required=True, help="课程名（一级目录）")
    ap.add_argument("--map", default="", help="映射 CSV（date,activity_id,lecture,topic）")
    ap.add_argument("--out", default=str(REPO / "courses"), help="课程根（默认 ./courses）")
    ap.add_argument("--init-map", action="store_true", help="只导出映射模板")
    ap.add_argument("--apply", action="store_true", help="真正写盘（默认预演）")
    a = ap.parse_args(argv)

    deliver = Path(a.deliver)
    if not deliver.is_dir():
        raise SystemExit(f"交付目录不存在：{deliver}")
    if a.init_map:
        return cmd_init_map(deliver, a.course, Path(a.map or "map.csv"))
    if not a.map:
        raise SystemExit("需要 --map 映射表；没有就先加 --init-map 生成模板")
    rows = read_map(Path(a.map))
    return cmd_stage(deliver, a.course, rows, Path(a.out), a.apply)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

# -*- coding: utf-8 -*-
"""给精修用的单讲速览：A 页原文 + B 关键词块 + B 页码映射 + D 原话候选。

用法：python scripts/lesson_brief.py <授课日期> <活动id> > _brief.md
"""
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
D = Path(os.environ.get("COURSE_REVIEW_WORK_ROOT", REPO / "_work")) / "deliver"
KEYS = ("考", "掌握", "记住", "了解", "重点", "实验", "作业", "期末", "成绩", "点名",
        "平时", "闭卷", "复习", "题", "章", "算法")


def main() -> int:
    date, aid = sys.argv[1], sys.argv[2]
    base = D / f"{date}_{aid}" / "04_证据文件夹"
    out = []

    a = (base / "证据源A_课件OCR.md").read_text(encoding="utf-8", errors="replace")
    parts = re.split(r"### (A-\d+) ｜ 证据页 (P\.\d+)", a)
    out.append("## A 课件 OCR")
    for i in range(1, len(parts), 3):
        body = parts[i + 2]
        tm = re.search(r"@ ([\w:–-]+)", body)
        txt = re.search(r"原文\*\*：\s*\n\s*> (.+?)\n- \*\*支撑结论", body, flags=re.S)
        s = re.sub(r"\s+", " ", txt.group(1)) if txt else ""
        out.append(f"{parts[i]} {parts[i+1]} {tm.group(1) if tm else ''}\n  {s[:400]}")

    b = (base / "证据源B_课堂转写.md").read_text(encoding="utf-8", errors="replace")
    parts = re.split(r"### (B-\d+) ｜ 证据页 (P\.\d+)", b)
    out.append("\n## B 转写（仅含关键词的块）")
    for i in range(1, len(parts), 3):
        code, page, body = parts[i], parts[i + 1], parts[i + 2]
        for m in re.finditer(r"\[(\d{1,2}:\d{2}(?::\d{2})?)[–-](\d{1,2}:\d{2}(?::\d{2})?)\]\s*(.+)", body):
            line = m.group(3).strip()
            if any(k in line for k in KEYS):
                out.append(f"{code} {page} {m.group(1)}: {line[:150]}")

    d = (base / "证据源D_课堂原话待回听.md").read_text(encoding="utf-8", errors="replace")
    out.append("\n## D 原话候选")
    for m in re.finditer(r"### (D-\d+) ｜ 证据页 (P\.\d+)(.{0,300}?)原文（转写直读）\*\*：\s*\n\s*> (.{0,150})",
                         d, flags=re.S):
        tm = re.search(r"@ (\d{1,2}:\d{2}(?::\d{2})?)", m.group(3))
        out.append(f"{m.group(1)} {m.group(2)} {tm.group(1) if tm else '-'} | {re.sub(chr(10), ' ', m.group(4)).strip()[:140]}")

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""精修稿机检：引用编号/页码是否存在、04 是否与原样一致、有无遗留占位、有无已取消的速记卡。

用法：python check_refined.py 示例课程 [09-07_第三讲-第2章]
"""
import hashlib
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT_ROOT = Path(os.environ.get("COURSE_REVIEW_OUT", REPO / "courses"))


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def main() -> int:
    course = sys.argv[1]
    only = sys.argv[2] if len(sys.argv) > 2 else ""
    refined = OUT_ROOT / course / "每节课的简短总结"
    if not refined.is_dir():
        print(f"找不到精修目录：{refined}\n"
              f"（先按 AGENTS.md §4.3 归档并精修，逐讲目录放进「每节课的简短总结/」）")
        return 2
    problems: list[str] = []
    n = 0
    for d in sorted(refined.iterdir()):
        if not d.is_dir() or (only and d.name != only):
            continue
        md = d / "02_逐节课知识点与课堂内容记录.md"
        if not md.exists():
            problems.append(f"{d.name}: 缺 02")
            continue
        n += 1
        text = md.read_text(encoding="utf-8")
        live = re.sub(r"```.*?```", "", text, flags=re.S)

        ev = d / "04_证据文件夹"
        codes: set[str] = set()
        maxpage = 0
        for f in ev.rglob("*.md"):
            t = f.read_text(encoding="utf-8", errors="replace")
            codes |= {m.group(0) for m in re.finditer(r"[ABCD]-\d{2,3}", t)}
            for m in re.finditer(r"P\.(\d{3})", t):
                maxpage = max(maxpage, int(m.group(1)))

        cited = set(re.findall(r"〔([ABCD])-(\d{2,3})", live))
        for book, num in sorted(cited):
            code = f"{book}-{int(num):03d}"
            if code not in codes:
                problems.append(f"{d.name}: 悬空引用 {code}")

        pages = {int(m) for m in re.findall(r"P\.(\d{3})", live)}
        over = sorted(p for p in pages if p > maxpage)
        if over:
            problems.append(f"{d.name}: 越界页码 {over[:5]}（上限 P.{maxpage:03d}）")

        for pat, why in ((r"\{\{[^{}\n]{1,60}\}\}", "残留占位符"), (r"<!--", "残留注释"),
                         (r"待填|TODO|TBD", "待办标记")):
            if re.search(pat, live):
                problems.append(f"{d.name}: {why}")

        orig = OUT_ROOT / "原样" / course / d.name / "04_证据文件夹"
        if orig.exists() and tree_hash(orig) != tree_hash(ev):
            problems.append(f"{d.name}: 04 与原样不一致")

    for md in sorted(list(refined.glob("*.md")) + list((OUT_ROOT / course).glob("*.md"))):
        t = md.read_text(encoding="utf-8", errors="replace")
        hits = [
            ln.strip() for ln in t.splitlines()
            if re.search(r"一页速记卡|速记卡", ln)
            and not re.search(r"删除|取消|移除|已作废", ln)
        ]
        if hits:
            problems.append(f"{md.name}: 残留已取消的「一页速记卡」")

    for legacy in ("01_期末考试重点与考情分析.md", "03_全课程知识体系与思维导图.md"):
        if (OUT_ROOT / course / "每节课的简短总结" / legacy).exists():
            problems.append(f"每节课的简短总结 仍残留带编号的课程级文档：{legacy}")

    print(f"检查 {n} 讲；问题 {len(problems)}")
    for p in problems:
        print("  -", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

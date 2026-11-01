# -*- coding: utf-8 -*-
"""
阶段五：基于课件时间线生成复习材料

设计原则（硬约束）
------------------
1. **不做要点总结**。本脚本不调用任何生成式模型，不产出「本章重点是什么」这类
   推断性文字——那需要模型推理，会引入幻觉。全部内容为课件 OCR 原文 + 双轨
   讲解原文，逐条可溯源到截图与时间码。
2. **OCR 不是课件原文**。OCR 文本原样搬运并标注置信度；conf < 0.5 的页明确
   提示「需回看原图」，不修正、不补全、不猜公式。
3. **画面时间 ≠ 讲解时间**。讲解原文按「画面停留区间内」归集，并同时给出
   区间时间码，读者可自行核对。
4. **考试信号是规则匹配，不是语义判断**。命中的是关键词，不代表该句真的在
   讲考试；输出保留完整原句供人工判断。

用法
----
python scripts/make_review.py --day-dir "E:\\课程复盘数据\\...\\2026-10-20"
python scripts/make_review.py --day-dir ... --out-dir ... --min-conf 0.5
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---------------------------------------------------------------- 规则
# 只提取**编号**，不提取标题文字。原因：OCR 文本里标题与正文连在一起
# （如「第一章绪论课程概述」），用贪婪正则抓标题必然把正文抓进来，
# 产生「第一章 绪论课程概述」这种伪标题。编号是稳定的，标题不可靠。
CHAPTER_NUM_RE = re.compile(r"第\s*([一二三四五六七八九十百\d\-—－])\s*章")
SECTION_NUM_RE = re.compile(r"(?<![\d.])(\d{1,2})\s*[\.．]\s*(\d{1,2})(?![\d.])")
# 编号条目：例2.4 / 定义2.12 / 性质2.3 / 定理2.1
ITEM_RE = re.compile(r"(例|定义|性质|定理|推论)\s*(\d{1,2}\s*[\.．]\s*\d{1,2})")
# 小节编号前若是这些字，说明是「例2.4」而非「小节 2.4」
ITEM_PREFIX = ("例", "定义", "性质", "定理", "推论", "式", "表", "图")

# 课程管理类页面（片头成绩/作业/考试说明），不属于正文章节
ADMIN_RE = re.compile(r"作业要求|成绩评定|考试说明|考勤|平时成绩|重修|学分|扣\s*\d+\s*分")

# 考试信号分级（规则匹配，非语义判断）
# 注意：正文里的数学语境会大量误命中（如「积分值」含「分值」），
# 故 A 级「分值」加负向后行断言；并在输出中标注所属章节，
# 片头「课程说明」里的命中通常才是真正的考试安排。
EXAM_RULES = [
    ("A", "直接指向考试",
     r"考试|考点|考题|题型|(?<!积)分值|开卷|闭卷|必考|会考|要考|考这个|考到|重点考|"
     r"大题|计算题|证明题|简答题|选择题|填空题|论述题|卷面"),
    ("B", "一般性强调",
     r"重点|重要|注意|特别注意|强调|掌握|记住|记一下|必须|一定要|关键|核心"),
    ("C", "任务性提示",
     r"作业|习题|练习|复习|预习|下节课|下次课|课后"),
    ("D", "成绩相关",
     r"成绩|分数|扣分|平时|期末|期中|及格|重修|学分"),
]
EXAM_RULES = [(lv, desc, re.compile(pat)) for lv, desc, pat in EXAM_RULES]

# 待核实信号
NUM_RE = re.compile(r"\d")
FORMULA_HINTS = ("=", "∑", "∫", "√", "≥", "≤", "≠", "±", "×", "÷", "σ", "μ", "Σ", "λ", "ρ")

CONF_OK = 0.60      # ≥ 此值视为可信
CONF_LOW = 0.50     # < 此值明确标「需回看原图」

LEVEL_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3}


# ---------------------------------------------------------------- 工具
def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_jsonl(path: Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _cn_num(s: str) -> str:
    """把 OCR 误识的章节序号还原成可读形式（仅用于展示，不改原文）。"""
    return s.replace("-", "一").replace("—", "一").replace("－", "一")


def extract_structure(ocr_text: str) -> dict:
    """从 OCR 文本提取章节/小节**编号**。只读不改。

    只返回编号（如「第一章」「2.1」），不返回标题文字——OCR 里标题与正文粘连，
    取标题必然带入正文，反而制造错误信息。标题由分组时用「首句参考」呈现。
    """
    t = ocr_text or ""
    out = {"chapter": None, "section": None, "items": [], "is_admin": False}

    m = CHAPTER_NUM_RE.search(t)
    if m:
        out["chapter"] = f"第{_cn_num(m.group(1))}章"

    for m in SECTION_NUM_RE.finditer(t):
        pre = t[max(0, m.start() - 2):m.start()]
        if any(k in pre for k in ITEM_PREFIX):
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if a > 20:          # 排除 1928.5 这类年份小数
            continue
        out["section"] = f"{a}.{b}"
        break

    for m in ITEM_RE.finditer(t):
        out["items"].append(f"{m.group(1)}{_norm(m.group(2)).replace('．', '.')}")

    if ADMIN_RE.search(t) and not out["chapter"]:
        out["is_admin"] = True
    return out


def scan_exam_signals(events: list[dict], slide2chapter: dict | None = None) -> list[dict]:
    """在双轨转写原文里做关键词匹配。返回命中片段，附完整原句与所属章节。"""
    slide2chapter = slide2chapter or {}
    hits = []
    for e in events:
        for track, chunks in (e.get("speech_in_span") or {}).items():
            for c in chunks:
                text = c.get("text") or ""
                if not text.strip():
                    continue
                for lv, desc, pat in EXAM_RULES:
                    m = pat.search(text)
                    if not m:
                        continue
                    hits.append({
                        "level": lv, "level_desc": desc,
                        "matched": m.group(0),
                        "track": track,
                        "slide_index": e["slide_index"],
                        "chapter": slide2chapter.get(e["slide_index"], ""),
                        "start_hms": c.get("start_hms"),
                        "end_s": c.get("end_s"),
                        "text": text,
                    })
                    break  # 每块只记最高等级的一条
    hits.sort(key=lambda h: (LEVEL_ORDER[h["level"]], h["start_hms"] or ""))
    return hits


def page_flags(e: dict) -> dict:
    """单页质量与待核实标记。"""
    conf = float(e.get("ocr_avg_conf") or 0.0)
    text = e.get("ocr_text") or ""
    n_lines = int(e.get("ocr_n_lines") or 0)

    nums = len(NUM_RE.findall(text))
    formulas = [h for h in FORMULA_HINTS if h in text]

    # 讲解里数字密集的块（数字/公式最易听错，需回听）
    review_chunks = []
    for track, chunks in (e.get("speech_in_span") or {}).items():
        for c in chunks:
            t = c.get("text") or ""
            if c.get("needs_review") and len(NUM_RE.findall(t)) >= 2:
                review_chunks.append({"track": track, "start_hms": c.get("start_hms"),
                                      "text": t})

    return {
        "conf": conf,
        "conf_level": "ok" if conf >= CONF_OK else ("mid" if conf >= CONF_LOW else "low"),
        "n_lines": n_lines,
        "empty_ocr": n_lines == 0,
        "ocr_error": e.get("ocr_error"),
        "extract_error": e.get("extract_error"),
        "n_numbers": nums,
        "formula_hints": formulas,
        "review_chunks": review_chunks,
        "needs_original": conf < CONF_LOW or n_lines == 0,
    }


# ---------------------------------------------------------------- 分组
def mmss(sec: float) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def assign_chunks_by_rep(events: list[dict]) -> None:
    """把每个转写块归属到「代表帧时间最接近」的那一页，每块只出现一次。

    文本去重合并后，代表页的 t_start/t_end 会扩展到被合并页的范围，导致
    相邻页区间重叠、同一段讲解在两页各出现一次。按代表帧就近归属可消除重复，
    且比「谁先出现归谁」更合理（讲解通常围绕当时正在展示的那一页）。
    """
    pool: dict[str, dict] = {}
    for e in events:
        for track, chunks in (e.get("speech_in_span") or {}).items():
            for c in chunks:
                pool.setdefault(track, {})[c.get("idx")] = c

    assigned: dict[int, dict[str, list]] = {id(e): {} for e in events}
    for track, cmap in pool.items():
        for c in cmap.values():
            best, best_d = None, None
            for e in events:
                if c["end_s"] <= e["t_start_s"] or c["start_s"] >= e["t_end_s"]:
                    continue
                d = abs(c["start_s"] - e["t_rep_s"])
                if best_d is None or d < best_d:
                    best, best_d = e, d
            if best is not None:
                assigned[id(best)].setdefault(track, []).append(c)

    for e in events:
        e["speech_in_span"] = {
            t: sorted(v, key=lambda x: x["start_s"])
            for t, v in assigned[id(e)].items()
        }


def build_groups(events: list[dict]) -> list[dict]:
    """按「章节 → 小节」分组，保持时间顺序。无章节的页沿用上一个标签。"""
    groups: list[dict] = []
    cur_chapter = "未标注章节"
    cur_section = "（未标注小节）"
    cur = None

    for e in events:
        st = extract_structure(e.get("ocr_text", ""))
        if st["is_admin"]:
            cur_chapter, cur_section = "课程说明", "作业 / 成绩 / 考试安排"
        elif st["chapter"]:
            cur_chapter = st["chapter"]
            cur_section = st["section"] or "（未标注小节）"
        elif st["section"]:
            cur_section = st["section"]

        if cur is None or cur["chapter"] != cur_chapter or cur["section"] != cur_section:
            cur = {"chapter": cur_chapter, "section": cur_section,
                   "pages": [], "ref": None}
            groups.append(cur)
        if cur["ref"] is None:
            cur["ref"] = (e.get("ocr_text") or "").strip()[:36]
        cur["pages"].append({**e, "_struct": st})
    return groups


# ---------------------------------------------------------------- 输出
def _group_span(g: dict) -> str:
    """组的时间跨度取所有页的 min(t_start) – max(t_end)。

    不能用「第一页 start – 最后一页 end」：文本去重合并会让代表页的区间
    扩展到被合并页范围，页序与区间端点不一致。
    """
    t0 = min(p["t_start_s"] for p in g["pages"])
    t1 = max(p["t_end_s"] for p in g["pages"])
    return f"{mmss(t0)}–{mmss(t1)}"


def _page_block(e: dict, idx: int, flags: dict, base_rel: str) -> list[str]:
    """单页的 Markdown 片段。"""
    L = []
    ocr_text = (e.get("ocr_text") or "").strip()
    conf = flags["conf"]

    badge = {"ok": "✅", "mid": "◐", "low": "⚠"}[flags["conf_level"]]
    L.append(f"#### 页 {e['slide_index']:03d} · {e['t_start_hms']}–{e['t_end_hms']}"
             f"（停留 {int(e['t_end_s'] - e['t_start_s'])}s）")
    L.append("")

    if e.get("merged_from"):
        L.append(f"> 本页合并了画面完全相同的第 {e['merged_from']} 页，"
                 f"区间已扩展至 {e['t_start_hms']}–{e['t_end_hms']}。")
        L.append("")

    img = Path(e["image"]).name if e.get("image") else None
    raw = Path(e["image_raw"]).name if e.get("image_raw") else None
    if img:
        L.append(f"- 截图：`{base_rel}/canvas/{img}`"
                 + (f"（原分辨率：`{base_rel}/raw/{raw}`）" if raw else ""))
    L.append(f"- 画面模式：`{e.get('frame_mode')}`　OCR 置信度：**{conf:.2f}** {badge}")

    if flags["needs_original"]:
        L.append(f"- ⚠ **本页 OCR 不可靠**（置信度 {conf:.2f}"
                 + ("，且未识别出任何文字" if flags["empty_ocr"] else "")
                 + "），课件内容必须以原图为准，本文档不补全、不推测")
    if flags["ocr_error"]:
        L.append(f"- OCR 报错：`{flags['ocr_error']}`")
    if flags["extract_error"]:
        L.append(f"- 抽帧失败：`{flags['extract_error']}`")
    L.append("")

    L.append("**课件文字（OCR 原文，未做任何修正）**")
    L.append("")
    if ocr_text:
        L.append("> " + ocr_text)
    else:
        L.append("> （OCR 未识别出文字）")
    L.append("")

    if flags["n_numbers"]:
        L.append(f"*本页 OCR 含 {flags['n_numbers']} 处数字"
                 + (f"，含公式符号 {' '.join(flags['formula_hints'][:6])}" if flags["formula_hints"] else "")
                 + "——OCR 对数字与公式的识别错误率最高，引用前请核对原图。*")
        L.append("")

    # 双轨讲解原文
    for track in sorted((e.get("speech_in_span") or {}).keys()):
        chunks = e["speech_in_span"][track] or []
        if not chunks:
            L.append(f"<details><summary>讲解 · track {track}（该区间无转写）</summary>")
            L.append("")
            L.append("</details>")
            L.append("")
            continue
        opened = " open" if track == sorted(e["speech_in_span"].keys())[0] else ""
        L.append(f"<details{opened}><summary>讲解 · track {track}"
                 f"（{len(chunks)} 块，{sum(len(c.get('text') or '') for c in chunks)} 字）</summary>")
        L.append("")
        for c in chunks:
            flag = " ⚠待核实" if c.get("needs_review") else ""
            L.append(f"- `[{c.get('start_hms')}]`{flag} {c.get('text')}")
        L.append("")
        L.append("</details>")
        L.append("")

    if flags["review_chunks"]:
        L.append("**⚠ 讲解中数字密集、需回听的片段**")
        L.append("")
        for r in flags["review_chunks"][:8]:
            L.append(f"- track {r['track']} `[{r['start_hms']}]` {r['text']}")
        L.append("")

    return L


def write_full_md(path: Path, events: list[dict], groups: list[dict],
                  signals: list[dict], base_rel: str, meta: dict) -> None:
    L = []
    L.append(f"# {meta['course']} · 课程复习材料")
    L.append("")
    L.append(f"- 课程日期：{meta['date']}")
    L.append(f"- 生成时间：{now_str()}")
    L.append(f"- 数据源：{len(events)} 条有效课件画面，双轨转写 "
             f"{meta['n_tracks']} 路覆盖")
    L.append(f"- 课件路：`{meta['source_file']}`")
    L.append("")

    L.append("## 使用说明（请先读）")
    L.append("")
    L.append("1. **本材料不含任何「要点总结」。** 全部内容为课件 OCR 原文与课堂讲解原文，"
             "逐条标注时间码与截图路径，可回看核对。任何总结都会引入推断，本阶段不做。")
    L.append("2. **OCR 不是课件原文。** 置信度 < 0.5 的页标 ⚠，公式、上下标、希腊字母、"
             "矩阵几乎必然出错；引用数字与公式前必须看原图。")
    L.append("3. **画面时间 ≠ 讲解时间。** 「讲解」是画面停留区间内的转写，老师可能提前讲"
             "或延后补充，故同时给出区间时间码。")
    L.append("4. **双轨并列，不预设谁更准。** 两路来自不同采集路径，一致不代表正确，"
             "不一致也不代表某一路必错。")
    L.append("5. **考试信号是关键词匹配**，命中的是词，不是判断。原句完整保留，请自行判断。")
    L.append("6. **每个转写块只出现一次**，归属「代表帧时间最近」的那一页。"
             "因为文本去重合并后相邻页区间会重叠，不分配会导致同一段讲解重复出现。")
    L.append("")
    L.append("标记说明：✅ 置信度 ≥ 0.60　◐ 0.50–0.60　⚠ < 0.50 或未识别　"
             "⚠待核实 = 含数字/考试相关表述的转写块")
    L.append("")

    # 目录
    L.append("## 目录")
    L.append("")
    cur_ch = None
    for g in groups:
        if g["chapter"] != cur_ch:
            cur_ch = g["chapter"]
            L.append(f"- **{cur_ch}**")
        span = _group_span(g)
        L.append(f"  - {g['section']}（{span}，{len(g['pages'])} 页）")
    L.append("")

    # 正文
    L.append("## 正文")
    L.append("")
    cur_ch = None
    for g in groups:
        if g["chapter"] != cur_ch:
            cur_ch = g["chapter"]
            L.append(f"### {cur_ch}")
            L.append("")
        span = _group_span(g)
        L.append(f"#### {g['section']}")
        L.append("")
        L.append(f"*时间跨度 {span}，共 {len(g['pages'])} 页*")
        if g.get("ref"):
            L.append("")
            L.append(f"*该组首句参考（OCR 原文）：{g['ref']}*")
        L.append("")
        for e in g["pages"]:
            L += _page_block(e, e["slide_index"], page_flags(e), base_rel)
        L.append("---")
        L.append("")

    # 考试信号
    L.append("## 考试与成绩相关表述（关键词匹配）")
    L.append("")
    if not signals:
        L.append("（未匹配到）")
    else:
        L.append(f"共命中 {len(signals)} 处。**这只是关键词匹配，不代表该句真在讲考试**，"
                 f"原句完整保留供判断。")
        L.append("")
        L.append("> 经验判断：真正的考试安排通常集中在**课程说明**（片头）；"
                 "正文里的命中多为数学语境误报（如「积分值」含「分值」）。"
                 "每条已标注所属章节，可据此估计可信度。")
        L.append("")
        cur_lv = None
        for h in signals:
            if h["level"] != cur_lv:
                cur_lv = h["level"]
                L.append(f"### {cur_lv} 级 · {h['level_desc']}")
                L.append("")
            L.append(f"- `[{h['start_hms']}]` track {h['track']} · 页 {h['slide_index']:03d}"
                     f" · {h.get('chapter') or '—'} · 命中「**{h['matched']}**」")
            L.append(f"  > {h['text']}")
            L.append("")

    # 待核实清单
    L.append("## 待核实清单")
    L.append("")
    low = [e for e in events if page_flags(e)["needs_original"]]
    L.append(f"### OCR 不可靠、必须看原图的页面（{len(low)} 页）")
    L.append("")
    if not low:
        L.append("（无）")
    else:
        L.append("| 页 | 时间 | 置信度 | 行数 | 原图 |")
        L.append("|---|---|---|---|---|")
        for e in low:
            f = page_flags(e)
            L.append(f"| {e['slide_index']:03d} | {e['t_rep_hms']} | {f['conf']:.2f} | "
                     f"{f['n_lines']} | `raw/{Path(e['image_raw']).name}` |")
    L.append("")

    many = [(e, page_flags(e)) for e in events if page_flags(e)["n_numbers"] >= 5]
    L.append(f"### 数字密集的课件页（{len(many)} 页，OCR 对数字错误率最高）")
    L.append("")
    if not many:
        L.append("（无）")
    else:
        L.append("| 页 | 时间 | 数字处数 | 公式符号 | 截图 |")
        L.append("|---|---|---|---|---|")
        for e, f in many:
            L.append(f"| {e['slide_index']:03d} | {e['t_rep_hms']} | {f['n_numbers']} | "
                     f"{' '.join(f['formula_hints'][:5]) or '—'} | "
                     f"`canvas/{Path(e['image']).name}` |")
    L.append("")

    L.append("## 本阶段限制")
    L.append("")
    L.append("1. 本材料**不是**老师给的复习提纲，也不代表考试范围。")
    L.append("2. 章节/小节标题由 OCR 文本正则提取，OCR 错误会导致标题错字"
             "（如「第一章」被识别成「第-章」，已在展示层还原，原文未改）。")
    L.append("3. 未收录非课件画面（电脑桌面、「无视频信号」等），这些时段无教学价值。")
    L.append("4. 转写为 SenseVoiceSmall 块级时间戳（约 30 秒粒度），"
             "块内文字的实际起止时刻不可知。")
    L.append("")
    path.write_text("\n".join(L), encoding="utf-8")


def write_brief_md(path: Path, groups: list[dict], signals: list[dict], meta: dict) -> None:
    """速览版：每页一行，便于快速定位。"""
    L = [f"# {meta['course']} · 复习速览", "",
         f"- 课程日期：{meta['date']}　生成：{now_str()}",
         "- 逐页一行，详细内容见 `复习材料.md`", ""]
    sig_by_slide: dict[int, list[str]] = {}
    for h in signals:
        sig_by_slide.setdefault(h["slide_index"], []).append(f"{h['level']}:{h['matched']}")

    L.append("| 时间 | 章节 | 页 | conf | 课件文字首句 | 信号 |")
    L.append("|---|---|---|---|---|---|")
    cur_ch = None
    for g in groups:
        for e in g["pages"]:
            f = page_flags(e)
            ch = g["chapter"] if g["chapter"] != cur_ch else ""
            cur_ch = g["chapter"]
            first = (e.get("ocr_text") or "")[:34].replace("|", "\\|") or "（无文字）"
            raw_sig = sig_by_slide.get(e["slide_index"], [])
            sig = "、".join(dict.fromkeys(raw_sig)) or "—"
            conf = f"{f['conf']:.2f}" + ("⚠" if f["needs_original"] else "")
            L.append(f"| {e['t_rep_hms']} | {ch} | {e['slide_index']:03d} | {conf} | "
                     f"{first} | {sig} |")
    L.append("")
    path.write_text("\n".join(L), encoding="utf-8")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="基于课件时间线生成复习材料（不总结，全部原文）")
    ap.add_argument("--day-dir", required=True, help="归档日目录")
    ap.add_argument("--slides-dir", default=None, help="默认 {day}/output/slides")
    ap.add_argument("--out-dir", default=None, help="默认 {day}/output/review")
    ap.add_argument("--course", default=None, help="课程名，默认取归档目录上一级名")
    args = ap.parse_args()

    day = Path(args.day_dir)
    sdir = Path(args.slides_dir) if args.slides_dir else day / "output" / "slides"
    out = Path(args.out_dir) if args.out_dir else day / "output" / "review"
    out.mkdir(parents=True, exist_ok=True)

    tj = sdir / "timeline.jsonl"
    events_all = read_jsonl(tj)
    if not events_all:
        print(f"[ERR] 缺少 {tj}，请先运行 extract_slides.py 或 pipeline 的 tracks_align 阶段")
        return 3

    events = [e for e in events_all if e.get("usable_for_study", True)]
    # 按代表帧时间排序：合并页的 t_start 会被拉到更早，用 t_rep 才符合阅读直觉
    events.sort(key=lambda e: e["t_rep_s"])
    if not events:
        print("[ERR] 没有可用的课件事件（全部被标为非课件画面）")
        return 3

    smeta = {}
    sp = sdir / "slides.json"
    if sp.exists():
        try:
            smeta = json.loads(sp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            smeta = {}

    # 先分配讲解块（会改写 speech_in_span），再分组——build_groups 是浅拷贝
    assign_chunks_by_rep(events)
    groups = build_groups(events)
    slide2chapter = {p["slide_index"]: g["chapter"] for g in groups for p in g["pages"]}
    signals = scan_exam_signals(events, slide2chapter)

    meta = {
        "course": args.course or day.parent.name or "课程",
        "date": day.name,
        "n_tracks": len({t for e in events for t in (e.get("speech_in_span") or {})}),
        "source_file": (smeta.get("source") or {}).get("file", "—"),
    }
    base_rel = "../slides"

    write_full_md(out / "复习材料.md", events, groups, signals, base_rel, meta)
    write_brief_md(out / "复习速览.md", groups, signals, meta)

    index = {
        "generated": now_str(),
        "course": meta["course"], "date": meta["date"],
        "source_file": meta["source_file"],
        "n_events_total": len(events_all),
        "n_events_used": len(events),
        "n_excluded_non_courseware": len(events_all) - len(events),
        "n_tracks": meta["n_tracks"],
        "n_exam_signals": len(signals),
        "n_low_conf_pages": sum(1 for e in events if page_flags(e)["needs_original"]),
        "chapters": [
            {"chapter": g["chapter"], "section": g["section"],
             "t_start_s": min(p["t_start_s"] for p in g["pages"]),
             "t_end_s": max(p["t_end_s"] for p in g["pages"]),
             "t_start_hms": mmss(min(p["t_start_s"] for p in g["pages"])),
             "t_end_hms": mmss(max(p["t_end_s"] for p in g["pages"])),
             "n_pages": len(g["pages"]),
             "slide_indexes": [p["slide_index"] for p in g["pages"]]}
            for g in groups
        ],
        "pages": [
            {"slide_index": e["slide_index"], "t_start_hms": e["t_start_hms"],
             "t_end_hms": e["t_end_hms"],
             "image": e.get("image"), "image_raw": e.get("image_raw"),
             "ocr_text": e.get("ocr_text"),
             "ocr_conf": e.get("ocr_avg_conf"),
             "n_speech_chunks": {t: len(v) for t, v in (e.get("speech_in_span") or {}).items()},
             "needs_original": page_flags(e)["needs_original"],
             "struct": {k: v for k, v in (e.get("_struct") or {}).items() if k != "is_admin"}}
            for e in events
        ],
        "exam_signals": signals,
        "outputs": {
            "full_md": str(out / "复习材料.md"),
            "brief_md": str(out / "复习速览.md"),
        },
    }
    write_json(out / "review_index.json", index)

    print(f"课件事件：{len(events_all)} 条 → 采用 {len(events)} 条"
          f"（排除非课件 {len(events_all) - len(events)} 条）")
    print(f"章节分组：{len(groups)} 组，覆盖 {len({g['chapter'] for g in groups})} 个章")
    for g in groups:
        print(f"  {g['chapter']} / {g['section']}：{len(g['pages'])} 页 "
              f"（{g['pages'][0]['t_start_hms']}–{g['pages'][-1]['t_end_hms']}）")
    print(f"考试信号：{len(signals)} 处"
          + (f"（A 级 {sum(1 for s in signals if s['level'] == 'A')} 处）" if signals else ""))
    low = sum(1 for e in events if page_flags(e)["needs_original"])
    print(f"OCR 不可靠页：{low} 页")
    print(f"输出：{out / '复习材料.md'}")
    print(f"      {out / '复习速览.md'}")
    print(f"      {out / 'review_index.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

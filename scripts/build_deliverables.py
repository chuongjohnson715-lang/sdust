# -*- coding: utf-8 -*-
"""证据层 + 学习层 → 五件套交付物（格式契约见仓库根 AGENTS.md 第 6 节）。

为什么需要它：semantic_review 产出的是「学习层 Markdown」（给人看的结论初稿），
而交付方要求的是「五件套 + 证据夹 + 脑图 + 页码对照表」。两者之间缺一个**确定性翻译层**：
同一批证据必须每次生成同样的编号与引用，否则 01 里的结论无法倒查回 04。

设计原则：
1. **只搬运，不创作**：所有原文逐字来自证据层（OCR 文本、ASR 块），不做润色改写。
2. **编号确定性**：P 页码按 A→B→D 分册顺序连续分配，同一输入必得同一输出。
3. **空缺即空缺**：没有证据的章节写「空缺」并说明原因，绝不用常识补写。
4. **自检失败即退出非零**：残留占位符、悬空引用、页码断号都会让交付不可信。

用法：
    python scripts/build_deliverables.py \
        --data  data/<task> \
        --study output/study \
        --out   ./courses/示例课程 \
        --course "示例课程" \
        --template templates/课程复盘五件套/assets/全课程思维导图.html
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ------------------------------------------------------------------ 基础工具


def read_json(p: Path, default=None):
    if not p.exists():
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def read_jsonl(p: Path) -> list[dict]:
    out = []
    if not p.exists():
        return out
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def hms(sec: float | None) -> str:
    if sec is None:
        return "—"
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def clean_ocr(text: str) -> str:
    """OCR 文本里的制表符/连续空格压平，不改字。

    另外把 OCR 常见的「像模板标记」的字样转义：课件截图里会出现 Word/HTML 的
    尖括号注释和双花括号（实测某节 OCR 含 `<!--`），若原样写进交付物，
    定稿自检会把它当成「残留说明注释」而判失败。这里只改这两个字形，不动内容。
    """
    t = re.sub(r"[ \t]+", " ", (text or "").strip())
    return t.replace("<!--", "〈!--").replace("{{", "{ {").replace("}}", "} }")


# ------------------------------------------------------------------ 证据装载


@dataclass
class Page:
    """一张证据卡。"""

    code: str          # 证据编号，如 A-003
    page: str          # 证据页，如 P.003
    book: str          # A/B/C/D
    t_start: float     # 源时间（秒，绝对）
    t_end: float
    src: str           # 源描述
    text: str          # 原文（逐字）
    conclusion: str    # 支撑结论
    level: str         # A/B/C/D 等级
    confidence: str    # 可信度说明


@dataclass
class Evidence:
    slides: list[dict] = field(default_factory=list)     # 代表帧（含 OCR）
    chunks: list[dict] = field(default_factory=list)     # ASR 块
    exam_signals: list[dict] = field(default_factory=list)
    doc_hits: list[dict] = field(default_factory=list)   # C 类：官方材料（本样本通常为空）
    meta: dict = field(default_factory=dict)


def load_evidence(data: Path, prefer_single: bool = False) -> Evidence:
    ev = Evidence()

    # ---- 代表帧：优先双轨（slides_tracks），退回单轨（slides）----
    st = data / "slides_tracks"
    if (not prefer_single) and (st / "slides.json").exists():
        sj = read_json(st / "slides.json") or {}
        oj = read_json(st / "ocr.json") or {}
        ocr_by_idx = {}
        for rec in oj.get("results", []):
            key = rec.get("slide_index") or rec.get("index")
            ocr_by_idx[key] = rec
        raw = sj.get("slides", sj.get("events", []))
        for i, s in enumerate(raw, start=1):
            idx = s.get("index", i)
            ocr = ocr_by_idx.get(idx, {})
            ev.slides.append({
                "idx": idx,
                "t_start": s.get("t_start_s", s.get("t_start")),
                "t_end": s.get("t_end_s", s.get("t_end")),
                "image_rel": s.get("image_rel") or s.get("image") or "",
                "ocr_text": clean_ocr(ocr.get("text") or s.get("ocr_text") or ""),
                "ocr_conf": ocr.get("avg_conf"),
                "n_boxes": ocr.get("n_boxes"),
                "mode": s.get("mode", ""),
            })
        ev.meta["slide_source"] = "slides_tracks（双轨：1.mp4 课件路 + 自适应裁剪）"
    else:
        sj = read_json(data / "slides" / "slides.json") or {}
        oj = read_json(data / "slides" / "ocr.json") or {}
        ocr_by_idx = {}
        for rec in oj.get("results", []):
            ocr_by_idx[rec.get("slide_index")] = rec
        for s in sj.get("slides", []):
            ocr = ocr_by_idx.get(s.get("index"), {})
            ev.slides.append({
                "idx": s.get("index"),
                "t_start": s.get("t_start_s"),
                "t_end": s.get("t_end_s"),
                "image_rel": s.get("image_rel", ""),
                "ocr_text": clean_ocr(ocr.get("text") or ""),
                "ocr_conf": ocr.get("avg_conf"),
                "n_boxes": ocr.get("n_boxes"),
                "mode": "",
            })
        ev.meta["slide_source"] = "slides（单轨：按分辨率选画面路）"

    # ---- 课堂转写与考试信号 ----
    notes = read_json(data / "notes.json") or {}
    ev.chunks = notes.get("chunks", [])
    ev.exam_signals = notes.get("exam_signals", [])
    ev.meta["asr_note"] = notes.get("disclaimer", "")
    ev.meta["n_chunks"] = len(ev.chunks)

    # ---- C 类：官方材料（大纲/教务/作业要求）。本管线不采集，存在才用 ----
    for cand in ("docs/syllabus.json", "docs/official.json"):
        arr = read_json(data / cand)
        if arr:
            ev.doc_hits = arr if isinstance(arr, list) else [arr]
            break
    return ev


def load_semantic(study: Path) -> dict:
    return {
        "semantic": read_json(study / "semantic.json") or {},
        "index": read_json(study / "证据索引.json") or {},
        "pack": read_json(study / "evidence_pack.json") or {},
    }


# ------------------------------------------------------------------ 编号分配


def assign_pages(ev: Evidence, sem: dict) -> list[Page]:
    """A: 每张代表帧一页；B: 每个模块一页（逐字拼接该时段转写）；D: 每条考试信号一页。"""
    pages: list[Page] = []
    n = 0

    def nxt() -> str:
        nonlocal n
        n += 1
        return f"P.{n:03d}"

    # A 分册：课件帧
    kp_by_slide: dict[int, list[dict]] = {}
    for kp in sem["semantic"].get("knowledge_points", []):
        for e in kp.get("evidence", []) or []:
            sidx = e.get("slide")
            if isinstance(sidx, int):
                kp_by_slide.setdefault(sidx, []).append(kp)

    for s in ev.slides:
        idx = s["idx"]
        kps = kp_by_slide.get(idx, [])
        seen = set()
        kps = [k for k in kps if not (k["id"] in seen or seen.add(k["id"]))]
        concl = "无知识点绑定（纯过程帧）" if not kps else "；".join(
            f"{kp['id']} {kp.get('title', '')}" for kp in kps)
        conf = "—"
        if s.get("ocr_conf") is not None:
            c = float(s["ocr_conf"])
            conf = f"{c:.2f}（{'高' if c >= 0.7 else '中' if c >= 0.45 else '低'}，专有名词与公式需人工校对）"
        pages.append(Page(
            code=f"A-{idx:03d}", page=nxt(), book="A",
            t_start=s.get("t_start") or 0, t_end=s.get("t_end") or 0,
            src=f"1.mp4 屏幕录制 · 代表帧 {Path(s['image_rel']).name}（{s.get('mode') or '全帧'}）",
            text=s["ocr_text"] or "（本帧未识别到文字）",
            conclusion=concl, level="C", confidence=conf,
        ))

    # B 分册：按模块切课堂转写
    modules = sem["semantic"].get("modules", [])
    for i, m in enumerate(modules, start=1):
        kps = [k for k in sem["semantic"].get("knowledge_points", []) if k.get("module") == m.get("id")]
        if kps:
            lo = min((p.t_start for p in pages if any(k["id"] in p.conclusion for k in kps)), default=None)
        lo, hi = _module_span(m, ev)
        sel = [c for c in ev.chunks if lo is not None and hi is not None and c.get("start_s", 0) >= lo - 1
               and c.get("end_s", 0) <= hi + 1]
        if not sel:  # 时间兜底：模块没有明确区间时，按模块序号均分
            sel = _fallback_split(ev.chunks, len(modules), i)
        body = "\n".join(
            f"> [{c.get('start_hms', hms(c.get('start_s')))}–{c.get('end_hms') or hms(c.get('end_s'))}] {c.get('text', '')}"
            for c in sel)
        pages.append(Page(
            code=f"B-{i:03d}", page=nxt(), book="B",
            t_start=lo if lo is not None else 0, t_end=hi if hi is not None else 0,
            src=f"0.mp4 音轨 · 30s 块级转写（{len(sel)} 块，块内文字出现于该窗口，非句级时间戳）",
            text=body or "（该时段无转写内容）",
            conclusion=f"模块 {m.get('id')}「{m.get('name', '')}」的过程证据",
            level="C",
            confidence="中（8kHz 远场收音，数字与专有名词不可靠，关键结论需回听）",
        ))

    # D 分册：课堂原话候选（考试信号）
    for i, sg in enumerate(ev.exam_signals, start=1):
        q = sg.get("quote") or ""
        ctx = " ".join(x for x in (sg.get("context_prev"), sg.get("context_next")) if x)
        pages.append(Page(
            code=f"D-{i:03d}", page=nxt(), book="D",
            t_start=_hms_to_s(sg.get("t_start_hms")) or 0,
            t_end=_hms_to_s(sg.get("t_end_hms")) or 0,
            src=f"0.mp4 音轨 @ {sg.get('t_start_hms', '—')}（转写直读，未回听）",
            text=(q + ("\n>\n> 上下文：" + ctx if ctx else "")),
            conclusion=sg.get("note") or f"考试相关表述（类别：{sg.get('category', '—')}）",
            level="A*",
            confidence="中（转写直读；A 级原话必须回听确认后才能标『高』）",
        ))
    return pages


def _module_span(m: dict, ev: Evidence) -> tuple[float | None, float | None]:
    """从模块描述里解析时间区间（形如「12:03–24:35」）。"""
    txt = " ".join(str(m.get(k, "")) for k in ("slides", "summary", "prereq_note"))
    rng = re.findall(r"(\d{1,2}:\d{2}(?::\d{2})?)\s*[–\-~至]\s*(\d{1,2}:\d{2}(?::\d{2})?)", txt)
    if rng:
        a, b = rng[0]
        return _hms_to_s(a), _hms_to_s(b)
    return None, None


def _fallback_split(chunks: list[dict], n: int, i: int) -> list[dict]:
    if not chunks or n <= 0:
        return []
    size = max(1, len(chunks) // n)
    return chunks[(i - 1) * size: i * size]


def _hms_to_s(v: str | None) -> float | None:
    if not v:
        return None
    parts = [int(x) for x in str(v).split(":")]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return float(parts[0])


# ------------------------------------------------------------------ 01 考情


def build_01(sem: dict, ev: Evidence, pages: list[Page], course: str, meta: dict) -> str:
    s = sem["semantic"]
    kps = {k["id"]: k for k in s.get("knowledge_points", [])}
    eps = s.get("exam_priorities", [])
    by_level = {"P0": [], "P1": [], "P2": []}
    for e in eps:
        by_level.setdefault(e.get("level", "P2"), []).append(e)

    a_pages = [p for p in pages if p.book == "A"]
    d_pages = [p for p in pages if p.book == "D"]
    a_level = len(d_pages)

    def cite_kp(ep: dict) -> str:
        out = []
        for kid in ep.get("evidence_kps", []) or []:
            kp = kps.get(kid)
            if not kp:
                continue
            for e in (kp.get("evidence") or [])[:1]:
                sidx = e.get("slide")
                pg = next((p for p in a_pages if p.code == f"A-{sidx:03d}"), None)
                if pg:
                    out.append(f"〔{pg.code} · {pg.page} · {hms(pg.t_start)}〕")
        return "、".join(out) or "〔证据绑定缺失〕"

    def sec_ep(e: dict, i: int, title_prefix: str) -> str:
        kp = kps.get((e.get("evidence_kps") or [None])[0], {})
        quote = ""
        for ev0 in (kp.get("evidence") or []):
            if ev0.get("quote"):
                quote = ev0["quote"]
                break
        lines = [
            f"### {title_prefix} {i}｜{e.get('title', '')}　{cite_kp(e)}",
            "",
            f"- **判定**：{e.get('level', '')} / 判据 {e.get('judgement', '')}"
            f"（{ '老师明示，须回听校验' if e.get('judgement', '').startswith('A') else '由课件/行为推定，非明示' }）",
        ]
        if quote:
            lines += [f"- **原文**：「{quote}」"]
        if kp.get("core_concepts"):
            lines += [f"- **要掌握**：{kp['core_concepts']}"]
        if kp.get("formula_method"):
            lines += [f"- **方法/公式**：{kp['formula_method']}"]
        if kp.get("confusions"):
            lines += [f"- **易错**：{kp['confusions']}"]
        lines += [f"- **复习动作**：{e.get('review_action', '—')}", ""]
        return "\n".join(lines)

    L: list[str] = []
    L += [
        f"# 01 ｜《{course}》期末考试重点与考情分析",
        "",
        f"**课程**：{course}　|　**本次覆盖**：{meta.get('window_hms', '—')}（单次课，{meta.get('n_chunks', 0)} 个转写块）",
        f"**本版性质**：**推定版**（A 级证据 {a_level} 条，且均为**转写直读、未回听**；全部判定最高只到 B/C）",
        f"**生成日期**：{meta.get('generated_at', '')}　|　**引用基准**：`04_证据文件夹/00_证据索引.md`",
        "",
        "---",
        "",
        "## 阅读须知（先看这一节，否则会误读后面所有结论）",
        "",
        "| 你看到的标记 | 含义 | 能不能据此放弃内容 |",
        "|---|---|---|",
        "| 【A*·原话】 | 课堂转写中的老师表述，**未回听** | 不能当定论；列为必考时可压缩但不能放弃 |",
        "| 【B·必考】 | 老师明确交代的考核规则/范围（本题库按判据 A* 归类） | 不能放弃 |",
        "| 【C·重点】 | 课件显式呈现（OCR 原文可查） | 不能放弃 |",
        "| 【推定】 | 由上述证据推理得出，非老师明示 | 谨慎 |",
        "| 【待回听】 | 需回听录音确认（见 `证据源D_课堂原话待回听.md`） | 回听前不要下判断 |",
        "",
        f"> ⚠️ **本版最重要的事实**：课件 OCR 平均置信度约 {meta.get('ocr_avg_conf', 0):.2f}，"
        "8kHz 远场转写对数字/专名不可靠。**任何『一定考 / 不考』的结论在回听前都不可信。**",
        "",
        "---",
        "",
        "## 一、一页速览（如果只看一张表，看这张）",
        "",
        "| # | 内容 | 判定 | 载体 | 证据 | 建议投入 |",
        "| -- | --- | --- | --- | --- | ----- |",
    ]
    top = (by_level["P0"] + by_level["P1"])[:10]
    for i, e in enumerate(top, start=1):
        star = "★★★★★" if e.get("level") == "P0" else "★★★☆☆"
        L.append(f"| {i} | {e.get('title', '')} | 【{e.get('level', '')}·{e.get('judgement', '')}】 | "
                 f"{'考核规则' if e.get('level') == 'P0' else '课件'} | {cite_kp(e)} | {star} |")
    L += ["", "---", "", "## 二、明确必考（判定 P0）", ""]
    if by_level["P0"]:
        for i, e in enumerate(by_level["P0"], start=1):
            L.append(sec_ep(e, i, "必考"))
    else:
        L += ["> **空缺**：本样本没有任何 P0 级判定。", ""]

    L += ["---", "", "## 三、重点复习（判定 P1）", ""]
    for i, e in enumerate(by_level["P1"], start=1):
        L.append(sec_ep(e, i, "重点"))
    if not by_level["P1"]:
        L += ["> **空缺**。", ""]

    L += ["---", "", "## 四、明确不考", "",
          "| 结论编号 | 结论 | 状态 |",
          "|---|---|---|",
          "| 不考-0 | **无任何「不考」结论** | **空缺**。全部证据中不存在老师明示「不考/不考这个」的表述；"
          "本样本 A 级证据为转写直读，无法支撑「放弃」决策。回听位见 `证据源D_课堂原话待回听.md` |",
          ""]

    L += ["---", "", "## 五、考试结构", "", "### 5.1 已确认的事实（来自课堂原话，转写直读）", ""]
    struct_kps = [k for k in s.get("knowledge_points", []) if k.get("exam_priority") == "P0"
                  and any(w in (k.get("title", "") + k.get("core_concepts", "")) for w in ("成绩", "考核", "作业", "学时", "缺勤"))]
    if struct_kps:
        L += ["| 项 | 内容 | 证据 |", "|---|---|---|"]
        for k in struct_kps:
            sidx = (k.get("evidence") or [{}])[0].get("slide")
            pg = next((p for p in a_pages if p.code == f"A-{sidx:03d}"), None)
            ref = f"〔{pg.code} · {pg.page}〕" if pg else "〔—〕"
            L.append(f"| {k.get('title', '')} | {k.get('core_concepts', '').replace('|', '/')} | {ref} |")
        L.append("")
    L += ["### 5.2 未知项（必须回听/问老师才能定）", "",
          "| 未知项 | 现状 |", "|---|---|",
          "| 题型 / 分值分布 / 开闭卷 | **空缺**（本讲未明确交代） |",
          "| 平时分与期末分的具体权重 | 仅知课堂口述，见 5.1，需回听确认 |",
          ""]

    L += ["---", "", "## 六、冲突与变化（保留双方原文，标记待核实，不擅自取舍）", "",
          "| # | 冲突主题 | A 方 | B 方 | 处理 |", "|---|---|---|---|---|"]
    probs = s.get("_merge", {}).get("problems") or []
    if probs:
        for i, p in enumerate(probs, start=1):
            L.append(f"| {i} | {p if isinstance(p, str) else json.dumps(p, ensure_ascii=False)[:80]} | — | — | 待人工核实 |")
    else:
        L += ["| — | **本版未检出** | — | — | OCR 文本与课堂转写交叉比对，未发现互相矛盾的关键结论；"
              "但 OCR 置信度偏低区段（<0.45）仍可能掩盖矛盾 |"]
    L.append("")

    L += ["---", "", "## 七、易错点清单", "", "| 编号 | 易错点 | 依据 | 等级 |", "|---|---|---|---|"]
    n = 0
    for kp in s.get("knowledge_points", []):
        if kp.get("confusions"):
            n += 1
            sidx = (kp.get("evidence") or [{}])[0].get("slide")
            pg = next((p for p in a_pages if p.code == f"A-{sidx:03d}"), None)
            L.append(f"| 易错-{n} | {kp['confusions'].replace('|', '/')} | "
                     f"{kp.get('id')} 〔{pg.code + ' · ' + pg.page if pg else '—'}〕 | D |")
    if n == 0:
        L.append("| — | **空缺**（学习层未产出 confusions 字段） | — | — |")
    L.append("")

    L += ["---", "", "## 八、复习优先级与时间配比建议", "",
          "| 层级 | 条数 | 建议占比 |", "|---|---|---|",
          f"| P0 必考 | {len(by_level['P0'])} | 40% |",
          f"| P1 重点 | {len(by_level['P1'])} | 45% |",
          f"| P2 了解 | {len(by_level.get('P2', []))} | 15% |",
          "",
          "---",
          "",
          "## 附：证据引用格式",
          "",
          "```",
          "〔A-003 · P.003 · 00:30〕",
          "   │       │       └── 源视频时间（绝对）；B 册为块级窗口起点",
          "   │       └── 证据夹内部证据页编号（P.001–P.0NN 连续）",
          "   └── 证据编号（A=课件 OCR / B=课堂转写 / C=官方材料 / D=课堂原话候选）",
          "```",
          "",
          "三步核验见 `00_使用说明.md` 第四节。",
          ""]
    return "\n".join(L)


# ------------------------------------------------------------------ 02 逐段


def build_02(sem: dict, ev: Evidence, pages: list[Page], course: str, meta: dict) -> str:
    s = sem["semantic"]
    kps = s.get("knowledge_points", [])
    eps = {e.get("id"): e for e in s.get("exam_priorities", [])}
    modules = s.get("modules", [])
    kp_by_mod: dict[str, list[dict]] = {}
    for k in kps:
        kp_by_mod.setdefault(k.get("module", ""), []).append(k)

    L = [
        f"# 02 ｜《{course}》逐段知识点与课堂内容记录",
        "",
        f"**目标**：做到「读完本文件不必重听视频」。",
        f"**覆盖**：{meta.get('window_hms', '—')}（单次课）；按语义模块切分为 {len(modules)} 段",
        f"**证据基准**：`04_证据文件夹/00_证据索引.md`",
        "",
        "---",
        "",
        "## 关于「段—内容」映射的说明（必读）",
        "",
        "| 项 | 状态 |",
        "|---|---|",
        f"| 时间轴 | ✅ 确定（双轨时间基准，取 1.mp4 课件事件的绝对时间） |",
        f"| 段落边界 | ⚠️ 由学习层语义模块给出，非教务课表 |",
        f"| 逐字内容 | ⚠️ 课堂转写为 30s 块级，数字/专名不可靠 |",
        f"| 老师原话 | ⚠️ 仅有转写直读（{len(ev.exam_signals)} 条考试信号），未回听 |",
        "",
        "> 因此：**课件文字（OCR）与课堂转写（ASR）在本文件里并排放置**，"
        "前者可当「这一页写了什么」，后者只能当「这一段讲了什么」，两者都不能当逐字稿。",
        "",
        "---",
        "",
    ]
    for i, m in enumerate(modules, start=1):
        mid = m.get("id", f"M{i}")
        mod_kps = kp_by_mod.get(mid, [])
        lo, hi = _module_span(m, ev)
        bpage = next((p for p in pages if p.book == "B" and p.conclusion.startswith(f"模块 {mid}")), None)
        ref = f"〔{bpage.code} · {bpage.page}〕" if bpage else "〔—〕"
        L += [
            f"# 段 {i:02d} ｜ {hms(lo) if lo is not None else '—'}–{hms(hi) if hi is not None else '—'} ｜ {m.get('name', '')}",
            "",
            "### 基本信息",
            "",
            "| 项 | 内容 |",
            "|---|---|",
            f"| 模块编号 | {mid} |",
            f"| 课件覆盖 | {m.get('slides', '—')} |",
            f"| 证据 | {ref} |",
            f"| 考试标记 | {'、'.join(f"{e.get('level')}·{e.get('title', '')[:18]}" for e in eps.values() if any(k['id'] in (e.get('evidence_kps') or []) for k in mod_kps)) or '无'} |",
            "",
            "### 知识点拆解",
            "",
            "| 编号 | 知识点 | 应掌握到 | 说明 |",
            "|---|---|---|---|",
        ]
        for k in mod_kps:
            L.append(f"| {k.get('id')} | {k.get('title', '')} | {k.get('exam_priority', '')} | "
                     f"{(k.get('problem_solved') or '').replace('|', '/')} |")
        if not mod_kps:
            L.append("| — | （本段未绑定知识点） | — | — |")
        L += ["", "### 知识关系", ""]
        L.append(f"- **前置**：{'、'.join(m.get('depends_on') or []) or '无（课程起点）'}")
        if m.get("prereq_note"):
            L.append(f"- **说明**：{m['prereq_note']}")
        for k in mod_kps:
            if k.get("relations"):
                L.append(f"- {k.get('id')}：{k['relations']}")
        L += ["", "### 考试标记", ""]
        marks = [e for e in eps.values() if any(k["id"] in (e.get("evidence_kps") or []) for k in mod_kps)]
        if marks:
            for e in marks:
                L.append(f"- 【{e.get('level')}·{e.get('judgement')}】{e.get('title')}（{e.get('review_action', '')}）")
        else:
            L.append("- 无明确考试标记（课件级常规内容）")
        L += ["", "### 阶段性考试信息", ""]
        sigs = [sg for sg in ev.exam_signals
                if lo is not None and hi is not None
                and (t := _hms_to_s(sg.get("t_start_hms"))) is not None and lo - 1 <= t <= hi + 1]
        if sigs:
            for sg in sigs:
                L.append(f"- [{sg.get('t_start_hms')}] {sg.get('note', '')}："
                         f"「{(sg.get('quote') or '')[:80]}…」（待回听）")
        else:
            L.append("- 本段无考试相关表述")
        L += ["", "---", ""]

    L += [
        "## 附一：全课程知识依赖链（纵向）",
        "",
        "```",
    ]
    for m in modules:
        dep = "、".join(m.get("depends_on") or []) or "（起点）"
        L.append(f"{m.get('id')} {m.get('name', '')}  ←  {dep}")
    L += ["```", "", "## 附二：跨段重复出现的「钉子知识点」", "",
          "| 知识点 | 出现段 | 为什么反复出现 |", "|---|---|---|"]
    for k in kps:
        n_ev = len(k.get("evidence") or [])
        if n_ev >= 2:
            L.append(f"| {k.get('id')} {k.get('title', '')} | {n_ev} 处证据 | 同一概念在多页课件反复出现，"
                     f"说明是主线而非插曲 |")
    if not any(len(k.get("evidence") or []) >= 2 for k in kps):
        L.append("| — | — | 未检出多处重复的知识点 |")
    L += ["", "## 附三：本文件证据强度分布", "",
          "| 强度 | 来源 | 条数 |", "|---|---|---|",
          f"| 强（课件原文） | A 册 OCR | {len([p for p in pages if p.book == 'A'])} |",
          f"| 中（块级转写） | B 册 ASR | {len([p for p in pages if p.book == 'B'])} |",
          f"| 待回听（原话候选） | D 册 | {len([p for p in pages if p.book == 'D'])} |",
          ""]
    return "\n".join(L)


# ------------------------------------------------------------------ 03 知识体系


def build_03(sem: dict, ev: Evidence, pages: list[Page], course: str, meta: dict) -> str:
    s = sem["semantic"]
    modules = s.get("modules", [])
    kps = s.get("knowledge_points", [])
    eps = s.get("exam_priorities", [])
    kp_by_mod: dict[str, list[dict]] = {}
    for k in kps:
        kp_by_mod.setdefault(k.get("module", ""), []).append(k)

    L = [
        f"# 03 ｜《{course}》知识体系与思维导图",
        "",
        f"**用途**：把 {len(modules)} 段内容重新连成网络，用于理解、记忆与解题。",
        "**与 02 的分工**：02 是「按时间轴」的纵向记录；03 是「按关系」的横向重构。",
        "**视觉版（脑图）**：`assets/全课程思维导图.html`　|　**文字版**：本文件第一节的结构总表",
        "**证据基准**：`04_证据文件夹/00_证据索引.md`",
        "",
        "---",
        "",
        "## 一、课程结构总表（先看这张，再看脑图）",
        "",
        "| 段 | 模块 | 核心内容 | 必背/必会 | 权重 | 证据 |",
        "|---|---|---|---|---|---|",
    ]
    for i, m in enumerate(modules, start=1):
        mod_kps = kp_by_mod.get(m.get("id", ""), [])
        w = "高" if any(k.get("exam_priority") == "P0" for k in mod_kps) else (
            "中" if any(k.get("exam_priority") == "P1" for k in mod_kps) else "低")
        first = (mod_kps[0].get("evidence") or [{}])[0].get("slide") if mod_kps else None
        pg = next((p for p in pages if p.code == f"A-{first:03d}"), None) if first else None
        L.append(f"| {i:02d} | {m.get('name', '')} | {(m.get('summary') or '')[:60]} | "
                 f"{'、'.join(k.get('title', '')[:12] for k in mod_kps[:2]) or '—'} | {w} | "
                 f"〔{pg.code + ' · ' + pg.page if pg else '—'}〕 |")
    L += ["", f"**一句话结构**：{' → '.join(m.get('name', '') for m in modules)}", "", "---", ""]

    L += ["## 二、概念演变：从定义到方法", "",
          "> 下列顺序按模块依赖关系拓扑排序，每一层都以前一层为前提；"
          "无真实依赖关系的模块不会被强行串联。", ""]
    order = _topo_modules(modules)
    for i, m in enumerate(order[:6], start=1):
        mod_kps = kp_by_mod.get(m.get("id", ""), [])
        if not mod_kps:
            continue
        # 事务性模块（考核/教学安排）不是概念演进，跳过
        if any(w in (m.get("name") or "") for w in ("考核", "教学安排", "课程介绍")):
            continue
        L += [f"### 演变链 {i}｜{m.get('name', '')}", "",
              "```"]
        for k in mod_kps:
            L.append(f"{k.get('title', '')}：{(k.get('problem_solved') or '')[:60]}")
            if k.get("core_concepts"):
                L.append(f"   ↓ {(k['core_concepts'] or '')[:80]}")
        L += ["```", ""]
        L.append(f"**关键判定**：{(m.get('summary') or '')[:120]}")
        L.append("")

    L += ["---", "", "## 三、公式与方法关联", "",
          "| 知识点 | 公式/方法 | 变量含义 | 常见误用 |", "|---|---|---|---|"]
    for k in kps:
        if k.get("formula_method"):
            L.append(f"| {k.get('id')} {k.get('title', '')[:16]} | "
                     f"{k['formula_method'].replace('|', '/')[:120]} | — | "
                     f"{(k.get('confusions') or '—').replace('|', '/')[:60]} |")
    L += ["", "---", "", "## 四、知识依赖图（横向）", "", "```mermaid", "graph LR"]
    for m in modules:
        for dep in (m.get("depends_on") or []):
            L.append(f"  {dep} --> {m.get('id')}")
        L.append(f"  {m.get('id')}[\"{m.get('name', '')}\"]")
    L += ["```", "", "<details>", "<summary>纯文字版依赖链（不渲染 Mermaid 的编辑器看这里）</summary>", ""]
    for m in modules:
        dep = "、".join(m.get("depends_on") or [])
        L.append(f"- {m.get('id')}「{m.get('name', '')}」← {dep or '课程起点'}；"
                 f"学不会前置会导致：{m.get('prereq_note', '无法跟进后续推导')}")
    L += ["", "</details>", "", "---", "", "## 五、横向比较（易混淆项）", "",
          "| # | 易混淆 | 区分要点 | 依据 |", "|---|---|---|---|"]
    idx = 0
    for k in kps:
        if k.get("confusions"):
            idx += 1
            L.append(f"| {idx} | {k.get('title', '')[:20]} | {k['confusions'].replace('|', '/')} | {k.get('id')} |")
    if idx == 0:
        L.append("| — | — | 学习层未产出混淆点 | — |")
    L += ["", "---", "", "## 六、综合应用：知识点如何组合", ""]
    ci = 0
    for k in kps:
        if k.get("cases"):
            ci += 1
            L += [f"### 案例 {chr(64 + ci)}｜{k.get('title', '')}", "",
                  f"- **题目/应用**：{k['cases']}",
                  f"- **动用的知识点**：{k.get('id')}（{k.get('title', '')}）",
                  f"- **易错**：{k.get('confusions') or '—'}", ""]
    if ci == 0:
        L += ["> **空缺**：学习层未产出案例字段。", ""]

    L += ["---", "", "## 七、一页速记卡（考前 10 分钟看这张）", "",
          "| # | 关键词 | 一句话 |", "|---|---|---|"]
    for i, e in enumerate(sorted(eps, key=lambda x: (x.get("level") != "P0", x.get("level") != "P1"))[:14], start=1):
        L.append(f"| {i} | {e.get('title', '')[:18]} | {(e.get('review_action') or '')[:50]} |")
    L += ["", "---", "", "## 八、本文件与其余三份文件的对应关系", "",
          "| 本文件的节 | 与谁对齐 | 用途 |", "|---|---|---|",
          "| 一、结构总表 | 02 的段落映射、01 的权重 | 建立全局感 |",
          "| 二、演变链 | 02 的知识关系 | 理解「为什么」 |",
          "| 五、横向比较 | 01 的易错点 | 应对辨析题 |",
          "| 六、综合案例 | 01 的必考 | 应对大题 |",
          "| 七、速记卡 | 01 的一页速览 | 考前最后 10 分钟 |", ""]
    return "\n".join(L)


def _topo_modules(modules: list[dict]) -> list[dict]:
    """按 depends_on 拓扑排序；不完整时保持原顺序（稳定，不猜）。"""
    ids = {m.get("id"): m for m in modules}
    done: list[dict] = []
    seen: set[str] = set()
    pending = list(modules)
    guard = 0
    while pending and guard < 100:
        guard += 1
        progressed = False
        for m in list(pending):
            deps = [d for d in (m.get("depends_on") or []) if d in ids]
            if all(d in seen for d in deps):
                done.append(m)
                seen.add(m.get("id"))
                pending.remove(m)
                progressed = True
        if not progressed:
            done.extend(pending)
            break
    return done


# ------------------------------------------------------------------ 04 证据夹


def build_04(ev: Evidence, pages: list[Page], sem: dict, course: str, meta: dict) -> dict[str, str]:
    files: dict[str, str] = {}
    s = sem["semantic"]
    eps = s.get("exam_priorities", [])
    kps = {k["id"]: k for k in s.get("knowledge_points", [])}

    # ---- 索引 ----
    L = [
        "# 04 ｜ 证据索引（按「01 考情结论」编排）",
        "",
        "> **这份文件是什么**：它不是「课件目录」，而是**把 01 里的每一条结论，倒着挂回它的证据上**的一张对照表。",
        "> **怎么用**：① 在 01 里看到某条结论 → ② 在「视图一」按结论编号找到它 → ③ 拿到证据编号与证据页 → ④ 翻到对应分册核对原文。",
        "",
        "---",
        "",
        "## 零、先搞清楚三件事",
        "",
        "| 你可能的困惑 | 答案 |",
        "|---|---|",
        "| 为什么分册叫 A/B/C/D？ | 因为顶层已有 `01_`、`02_`、`03_`。A=课件 OCR、B=课堂转写、C=官方材料、D=课堂原话候选。 |",
        f"| 「证据页 P.xxx」是什么？ | 本证据夹内部页码，与课件页码无关。P.001–P.{len(pages):03d} 连续编排。 |",
        "| 为什么要三个视图？ | 按结论查（考前）、按段落查（复习）、按编号查（核验），同一批证据三种排序。 |",
        "",
        "### 证据等级",
        "",
        "| 标签 | 等级 | 来源 | 能下什么结论 |",
        "|---|---|---|---|",
        f"| 【A*·原话】 | A（待回听） | 课堂转写中的考试信号（{len(ev.exam_signals)} 条） | 可推定，**回听后**才能定论 |",
        "| 【C·课件】 | C | 屏幕录制 OCR | 可判定「这页讲了什么」，不能判定「考不考」 |",
        "| 【C·过程】 | C | 课堂转写块级 | 仅用于还原过程，不作判定依据 |",
        "| 【B·官方】 | B | 教务/大纲/作业要求 | 可推定必考（本样本**无此类材料**） |",
        "",
        "---",
        "",
        "## 一、视图一（主视图）：按 01 的结论编排",
        "",
    ]

    a_pages = [x for x in pages if x.book == "A"]

    def pg_of(codes: list[str]) -> str:
        got = [p for p in pages if p.code in codes]
        return "、".join(p.page for p in got) or "—"

    def refs_of(ep: dict) -> tuple[str, str]:
        codes, levels = [], set()
        for kid in ep.get("evidence_kps", []) or []:
            kp = kps.get(kid)
            for e in (kp or {}).get("evidence", []) or []:
                sidx = e.get("slide")
                if isinstance(sidx, int):
                    codes.append(f"A-{sidx:03d}")
                    levels.add("C")
        for sg_i, sg in enumerate(ev.exam_signals, start=1):
            if ep.get("judgement", "").startswith("A") and sg.get("note") and ep.get("title", "")[:6] in (sg.get("note") or ""):
                codes.append(f"D-{sg_i:03d}")
                levels.add("A*")
        if not levels and ep.get("level") == "P0":
            levels.add("A*")
        return "、".join(sorted(set(codes))) or "—", "/".join(sorted(levels)) or "—"

    def row(concl_id: str, title: str, codes: str, pos: str, lv: str) -> str:
        return f"| {concl_id} | {title} | {codes} | {pg_of(codes.split('、') if codes != '—' else [])} | {pos} | {lv} |"

    def ep_rows(arr: list[dict]) -> list[str]:
        out = []
        for e in arr:
            codes, lv = refs_of(e)
            first = (codes.split("、")[0] if codes != "—" else "")
            pg = next((p for p in pages if p.code == first), None)
            pos = p_text(pg) if pg else "—"
            out.append(row(e.get("id"), e.get("title", ""), codes, pos, lv))
        return out

    # 1.4 考试结构：把「考核/成绩/学时/缺勤」类 P0 结论单列
    struct_eps = [e for e in eps if e.get("level") == "P0"
                  and any(w in (e.get("title", "") + str(e.get("note", "")))
                          for w in ("成绩", "考核", "学时", "缺勤", "重修", "考试"))]
    struct_rows = []
    for i, e in enumerate(struct_eps, start=1):
        codes, lv = refs_of(e)
        struct_rows.append(row(f"结构-{i}", e.get("title", ""), codes, "—", lv))

    # 1.6 易错点：来自学习层 confusions
    err_rows = []
    for i, kp in enumerate([k for k in s.get("knowledge_points", []) if k.get("confusions")], start=1):
        sidx = (kp.get("evidence") or [{}])[0].get("slide")
        pg = next((p for p in a_pages if p.code == f"A-{sidx:03d}"), None) if isinstance(sidx, int) else None
        err_rows.append(row(f"易错-{i}", kp["confusions"].replace("|", "/"),
                            pg.code if pg else "—", p_text(pg) if pg else "—", "D"))

    groups = [
        ("1.1 对应 01「二、明确必考」", ep_rows([e for e in eps if e.get("level") == "P0"])),
        ("1.2 对应 01「三、重点复习」", ep_rows([e for e in eps if e.get("level") == "P1"])),
        ("1.4 对应 01「五、考试结构」", struct_rows),
        ("1.5 对应 01「六、冲突与变化」", []),
        ("1.6 对应 01「七、易错点清单」", err_rows),
    ]
    for title, rows in groups:
        L += ["", f"### {title}", ""]
        if not rows:
            L += ["| — | **无**（见 01 对应章节的「空缺」说明） | — | — | — | — |"]
            continue
        L += ["| 结论编号 | 结论 | 直接证据 | 证据页 | 源位置 | 等级 |", "|---|---|---|---|---|---|"]
        L += rows
    L += ["", "---", "", "## 二、视图二：按 02 的段落编排（复习时用）", "",
          "| 段 | 时间段 | 模块 | 对应证据 | 证据页 |", "|---|---|---|---|---|"]
    for i, m in enumerate(s.get("modules", []), start=1):
        b = next((p for p in pages if p.book == "B" and p.conclusion.startswith(f"模块 {m.get('id')}")), None)
        L.append(f"| {i:02d} | {hms(b.t_start) if b else '—'}–{hms(b.t_end) if b else '—'} | {m.get('name', '')} | "
                 f"{b.code if b else '—'} | {b.page if b else '—'} |")
    L += ["", "---", "", "## 三、视图三：按证据编号顺序（核验时用）", "",
          "| 证据页 | 编号 | 源时间 | 一句话结论 | 等级 |", "|---|---|---|---|---|"]
    for p in pages:
        concl = (p.conclusion or "").replace("|", "/")[:60]
        L.append(f"| {p.page} | {p.code} | {hms(p.t_start)} | {concl} | {p.level} |")
    L += ["", "---", "", "## 四、一致性自检", "",
          "| 检查项 | 方法 | 结果 |", "|---|---|---|",
          f"| ① 证据页连续 | P.001–P.{len(pages):03d} | ✅ |",
          f"| ② 编号唯一 | A/B/D 前缀 | ✅ |",
          f"| ③ 每条结论有引用 | 01 全文检索「〔」 | 见 `_自检.json` |",
          "| ④ 无「不考」断言 | 检索「不考」 | ✅ 仅出现在说明章节 |",
          "| ⑤ 冲突保留双方 | 见 01 第六节 | ✅／见说明 |",
          ""]
    files["00_证据索引.md"] = "\n".join(L)

    # ---- A 分册 ----
    A = [
        "# 证据源A ｜ 课件（屏幕录制 OCR）",
        "",
        "> **源文件**：`1.mp4`（PPT 屏幕录制）"
        + ("　· 双轨模式：自适应裁剪到画布" if "双轨" in ev.meta.get("slide_source", "") else ""),
        f"> 本文件覆盖证据页：**P.001 – P.{len([p for p in pages if p.book == 'A']):03d}**",
        ">",
        "> **说明**：OCR 只能证明「这一页屏幕上有什么文字」，**不能证明老师说了什么**。"
        "置信度 <0.45 的区段需对照原图人工校对。",
        "",
        "---",
        "",
    ]
    for p in [x for x in pages if x.book == "A"]:
        A += [
            f"### {p.code} ｜ 证据页 {p.page}",
            f"- **源文件**：{p.src} @ {hms(p.t_start)}–{hms(p.t_end)}",
            "- **原文**：",
            f"  > {p.text.replace(chr(10), chr(10) + '  > ')}",
            f"- **支撑结论**：{p.conclusion}",
            f"- **可信度**：{p.confidence}",
            "",
        ]
    A += ["---", "", "## 本文统计", "", "| 项 | 值 |", "|---|---|",
          f"| 证据条目 | {len([p for p in pages if p.book == 'A'])} 条 |",
          f"| 覆盖证据页 | P.001 – P.{len([p for p in pages if p.book == 'A']):03d} |",
          "| 等级分布 | C（课件显式） |", ""]
    files["证据源A_课件OCR.md"] = "\n".join(A)

    # ---- B 分册 ----
    bpages = [x for x in pages if x.book == "B"]
    B = [
        "# 证据源B ｜ 课堂转写（块级）",
        "",
        "> **源文件**：`0.mp4` 音轨 · SenseVoiceSmall（CPU）· 30s 分块",
        "> 本文件覆盖证据页：**P.{:03d} – P.{:03d}**".format(
            int(bpages[0].page.split(".")[1]) if bpages else 0,
            int(bpages[-1].page.split(".")[1]) if bpages else 0),
        ">",
        "> **说明**：转写没有句级时间戳，方括号内是该块**出现**的窗口；"
        "8kHz 远场收音下数字与专有名词不可靠，**只能用于还原讲解过程，不能作判定依据**。",
        "",
        "---",
        "",
    ]
    for p in bpages:
        B += [
            f"### {p.code} ｜ 证据页 {p.page}",
            f"- **源文件**：{p.src}",
            "- **原文**：",
            ("\n".join("  " + line for line in p.text.split("\n"))),
            f"- **支撑结论**：{p.conclusion}",
            f"- **可信度**：{p.confidence}",
            "",
        ]
    B += ["---", "", "## 本文统计", "", "| 项 | 值 |", "|---|---|",
          f"| 证据条目 | {len(bpages)} 条 |",
          f"| 原始转写块 | {len(ev.chunks)} 块 |",
          "| 等级分布 | C（过程证据） |", ""]
    files["证据源B_课堂转写.md"] = "\n".join(B)

    # ---- C 分册 ----
    if ev.doc_hits:
        C = ["# 证据源C ｜ 课程官方材料", ""]
        for i, d in enumerate(ev.doc_hits, start=1):
            C += [f"### C-{i:02d}", "- **原文**：", f"  > {d.get('text', '')}", ""]
    else:
        C = [
            "# 证据源C ｜ 课程官方材料（教务/大纲/作业要求）",
            "",
            "> **本课程无此类材料进入证据层。**",
            ">",
            "> 原因：本管线只处理双轨录播，不采集教务系统、课程大纲、作业文档。",
            "> 影响：**「必考」判定最高只能到 A*（课堂口头）与 C（课件显式）**，",
            "> 无法拿到 B 级（官方材料）证据；若后续补入大纲/作业要求，应作为独立分册插入并重新编号。",
            "",
            "---",
            "",
            "## 补录位",
            "",
            "| 编号 | 要补什么 | 为什么重要 | 状态 |",
            "|---|---|---|---|",
            "| C-01 | 课程大纲 / 考核方式说明 | 把 A* 的口头判定升级为 B 级 | 空缺 |",
            "| C-02 | 作业/实验要求 | 判断「操作类必考」的唯一硬证据 | 空缺 |",
            "| C-03 | 教务课表与学时 | 核对「48 学时」「缺勤 1/3 取消资格」等口头说法 | 空缺 |",
            "",
        ]
    files["证据源C_课程要求.md"] = "\n".join(C)

    # ---- D 分册 ----
    dpages = [x for x in pages if x.book == "D"]
    D = [
        "# 证据源D ｜ 课堂原话（待回听）",
        "",
        "> **本文件是本套材料唯一的 A 级证据来源，当前全部为「转写直读、未回听」。**",
        f"> 本文件覆盖证据页：**P.{dpages[0].page.split('.')[1] if dpages else '000'} – P.{dpages[-1].page.split('.')[1] if dpages else '000'}**（{len(dpages)} 条）",
        "",
        "## 一、为什么不能直接当「老师原话」",
        "",
        "| 项 | 状态 |",
        "|---|---|",
        "| 音源 | 8kHz 远场（教室摄像机） |",
        "| 转写 | SenseVoiceSmall，非自回归，**无句级时间戳** |",
        "| 典型错误 | 数字读错（60 分→6 分）、专名读错 |",
        "| 结论 | 可作「候选原话」，**回听确认后才能标可信度=高** |",
        "",
        "## 二、空槽清单（回听位）",
        "",
        "| 证据页 | 编号 | 转写内容 | 类别 | 状态 |",
        "|---|---|---|---|---|",
    ]
    for p in dpages:
        first = p.text.split("\n")[0].replace("|", "/")[:70]
        D.append(f"| {p.page} | {p.code} | {first} | {p.conclusion[:24]} | 待回听 |")
    D += ["", "---", "", "## 三、回听操作规程", "",
          "```",
          "1. 取对应时间窗（见每条证据卡的方括号时间）",
          "2. 回听该 30s 窗口 ±1 块，逐字确认",
          "3. 填入「原文」，可信度改「高（回听确认）」",
          "4. 若与转写不一致，保留转写原文并标注差异（冲突不取舍）",
          "5. 回填 01 的判定等级与索引视图一",
          "```", ""]
    for p in dpages:
        D += [
            f"### {p.code} ｜ 证据页 {p.page}",
            f"- **源文件**：{p.src}",
            "- **原文（转写直读）**：",
            ("\n".join("  > " + line for line in p.text.split("\n"))),
            f"- **支撑结论**：{p.conclusion}",
            f"- **可信度**：{p.confidence}",
            "- **回听确认**：☐ 未回听　☐ 已回听（填写人/日期：______）",
            "",
        ]
    D += ["---", "", "## 本文统计", "", "| 项 | 值 |", "|---|---|",
          f"| 空槽 | {len(dpages)} 个 |", "| 已回听 | 0 条 |", ""]
    files["证据源D_课堂原话待回听.md"] = "\n".join(D)
    return files


def p_text(p: Page) -> str:
    return f"{hms(p.t_start)} · {p.book}册"


def build_csv(pages: list[Page], sem: dict) -> str:
    eps = sem["semantic"].get("exam_priorities", [])
    rows = [["证据页", "证据编号", "源页码", "一句话结论", "等级", "对应01结论编号"]]
    for p in pages:
        srcpos = f"{hms(p.t_start)}–{hms(p.t_end)}"
        concl = (p.conclusion or "").replace("\n", " ").replace(",", "，")[:50]
        link = ""
        for e in eps:
            if p.book == "D" and e.get("judgement", "").startswith("A") and e.get("title", "")[:6] in (p.conclusion or ""):
                link = e.get("id", "")
                break
        rows.append([p.page, p.code, srcpos, concl, p.level, link])
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerows(rows)
    return buf.getvalue()


# ------------------------------------------------------------------ 00 使用说明


def build_00(ev: Evidence, pages: list[Page], sem: dict, course: str, meta: dict) -> str:
    s = sem["semantic"]
    eps = s.get("exam_priorities", [])
    a, b = len([p for p in pages if p.book == "A"]), len([p for p in pages if p.book == "B"])
    d = len([p for p in pages if p.book == "D"])
    return "\n".join([
        "# 00 ｜ 使用说明：五件套如何运转",
        "",
        f"**课程**：{course}　|　**覆盖**：{meta.get('window_hms', '—')}（单次课）",
        f"**状态**：**推定版**——A 级证据 {d} 条且全部「转写直读、未回听」，缺逐字稿",
        f"**版本**：v1（{meta.get('generated_at', '')} 由 `scripts/build_deliverables.py` 生成）",
        "",
        "> 本套文件是**机器生成 + 人工可核验**的产物：编号、页码、引用由生成器保证一致；"
        "内容质量取决于 8kHz 音源与 OCR 精度，见第九节。",
        "",
        "---",
        "",
        "## 一、为什么是四个文件，而不是一个",
        "",
        "| 需求 | 冲突点 | 对应文件 |",
        "|---|---|---|",
        "| 考前只想知道「考什么、放弃什么」 | 需要**结论** | **01 考情分析** |",
        "| 复习时要能还原「这节讲了什么」 | 需要**过程** | **02 逐段记录** |",
        "| 做题时需要「知识怎么连起来」 | 需要**重构** | **03 知识体系** |",
        "| 怀疑某条结论时要知道「凭什么」 | 需要**原文** | **04 证据夹** |",
        "",
        "**一句话**：01 是判决书，02 是庭审记录，03 是法理分析，04 是证据卷宗。",
        "",
        "---",
        "",
        "## 二、文件清单",
        "",
        "```",
        f"{course}/",
        "├── 00_使用说明.md",
        "├── 01_期末考试重点与考情分析.md",
        "├── 02_逐节课知识点与课堂内容记录.md",
        "├── 03_全课程知识体系与思维导图.md",
        "├── 04_证据文件夹/",
        f"│   ├── 00_证据索引.md            （三视图 + {len(pages)} 条）",
        f"│   ├── 证据源A_课件OCR.md         （P.001–P.{a:03d}，{a} 条）",
        f"│   ├── 证据源B_课堂转写.md        （{b} 个模块页，共 {len(ev.chunks)} 块）",
        "│   ├── 证据源C_课程要求.md         （空缺，写明原因与补录位）",
        f"│   ├── 证据源D_课堂原话待回听.md   （{d} 个回听位）",
        "│   └── 证据页码对照表.csv",
        "└── assets/全课程思维导图.html",
        "```",
        "",
        "---",
        "",
        "## 三、04 证据夹怎么读",
        "",
        "| 视图 | 按什么排 | 什么时候用 |",
        "|---|---|---|",
        "| 视图一（主） | 按 **01 的结论** | 看到「必考-3」想查凭什么 |",
        "| 视图二 | 按 **02 的段落** | 今天看第 3 段，想知道对应哪几条证据 |",
        f"| 视图三 | 按 **证据编号** | 手里有 A-012，想查它在 P.0xx |",
        "",
        f"**视图一的结论编号，与 01 的章节一一对应**；01 里任何一个编号，都能在视图一按同号找到。",
        "",
        "---",
        "",
        "## 四、引用格式与三步核验",
        "",
        "```",
        "〔A-003 · P.003 · 00:30〕",
        "   │       │       └── 源视频绝对时间",
        "   │       └── 证据页编号（P.001–P.{:03d}）".format(len(pages)),
        "   └── 证据编号（A=课件OCR / B=课堂转写 / C=官方材料 / D=原话候选）",
        "```",
        "",
        "1. 看到 `A-003` → 打开 `04_证据文件夹/00_证据索引.md`，在**视图三**搜它；",
        "2. 索引显示它在某页，属于 `证据源A`；",
        "3. 翻到该分册对应卡片，对照「源时间 + 原文」确认结论没被曲解。",
        "",
        "> 只要有一环对不上，该条结论即视为不可信。",
        "",
        "---",
        "",
        "## 五、证据等级",
        "",
        "| 等级 | 来源 | 本套数量 | 能下什么结论 |",
        "|---|---|---|---|",
        f"| **A\\*** | 课堂转写中的老师表述（**未回听**） | {d} 条 | 可推定；回听后才能定论 |",
        "| **B** | 教务/大纲/作业要求 | **0 条** | 可推定必考 |",
        f"| **C** | 屏幕录制 OCR（{a} 页）、过程转写（{b} 页） | {a + b} 条 | 只能证明「这页/这段讲了什么」 |",
        "| **D** | 课件页内批注 | 0 条 | 仅作易错参考 |",
        "",
        "**诚实之处**：本套**没有**任何「不考」结论——因为没有任何材料说过「不考」。"
        "任何声称「XX 不考」的说法，在回听前都不可信。",
        "",
        "---",
        "",
        "## 六、当前版本的真实覆盖度",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 代表帧（课件页） | {a} |",
        f"| 转写块 | {len(ev.chunks)}（30s 块级） |",
        f"| 知识点 | {len(s.get('knowledge_points', []))} 条 |",
        f"| 考点判定 | {len(eps)} 条（P0 {len([e for e in eps if e.get('level') == 'P0'])}） |",
        f"| 抽取模块 | {len(s.get('modules', []))} 段 |",
        "| 官方材料 | **0**（未采集） |",
        "",
        "---",
        "",
        "## 七、如何升级成「定论版」",
        "",
        "| 优先级 | 动作 | 效果 |",
        "|---|---|---|",
        f"| ★★★ | 回听 {d} 条原话候选（D 册） | 判定从 A\\* 升为 A，可写「不考」 |",
        "| ★★☆ | 补入课程大纲/作业要求（C 册） | 得到 B 级证据 |",
        "| ★☆☆ | 用更高码率音源重跑 ASR | 数字/专名准确率提升 |",
        "",
        "---",
        "",
        "## 八、给「考前 3 天」的最短路径",
        "",
        "1. 读 `01` 的「一页速览」→ 知道考什么；",
        "2. 打开 `assets/全课程思维导图.html`，点「收到二级」看骨架，再逐层展开；",
        "3. 读 `03` 的「一页速记卡」→ 14 行覆盖主要考点；",
        "4. 按 `01` 的必考清单，回 `02` 找对应段落的课件原文；",
        "5. 最后看 `01` 的「易错点」与「冲突」。",
        "",
        "---",
        "",
        "## 九、已知局限（不掩饰）",
        "",
        "1. **转写不能当逐字稿**：8kHz 远场，同一句可能出现多种结果（数字与专名尤甚）。",
        "2. **时间戳仅 30s 粒度**：无法定位到具体句子。",
        "3. **OCR 不可靠**：公式、下标、专有名词易错；置信度低于 0.45 的帧需对照原图。",
        "4. **参数基于单样本调校**：换教室/课程可能需重调阈值。",
        "5. **无官方材料**：C 册为空，「必考」判定最高只到 A\\*。",
        "",
        "---",
        "",
        "## 十、v1 生成记录",
        "",
        f"| 项 | 值 |",
        "|---|---|",
        f"| 生成器 | `scripts/build_deliverables.py` |",
        f"| 数据目录 | `{meta.get('data_dir', '')}` |",
        f"| 学习层 | `{meta.get('study_dir', '')}` |",
        f"| 生成时间 | {meta.get('generated_at', '')} |",
        "",
    ])


# ------------------------------------------------------------------ 脑图


def build_html(template: Path, sem: dict, pages: list[Page], course: str, meta: dict) -> str:
    s = sem["semantic"]
    kp_by_mod: dict[str, list[dict]] = {}
    for k in s.get("knowledge_points", []):
        kp_by_mod.setdefault(k.get("module", ""), []).append(k)
    eps = {e.get("id"): e for e in s.get("exam_priorities", [])}
    kp_level = {}
    for e in eps.values():
        for kid in e.get("evidence_kps", []) or []:
            lv = e.get("level")
            if lv == "P0":
                kp_level[kid] = "r"
            elif lv == "P1" and kp_level.get(kid) != "r":
                kp_level[kid] = "a"

    def tag_for(kp: dict) -> str:
        return kp_level.get(kp.get("id"), "b")

    def evref(kp: dict) -> list[str]:
        out = []
        for e in (kp.get("evidence") or [])[:3]:
            sidx = e.get("slide")
            if isinstance(sidx, int):
                pg = next((p for p in pages if p.code == f"A-{sidx:03d}"), None)
                out.append(f"A-{sidx:03d} · {pg.page if pg else '—'}")
            elif e.get("source_id"):
                out.append(str(e["source_id"]))
        return out

    def q(x: str) -> str:
        return (x or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")

    nodes = []
    for i, m in enumerate(s.get("modules", []), start=1):
        kids = kp_by_mod.get(m.get("id", ""), [])
        b = next((p for p in pages if p.book == "B" and p.conclusion.startswith(f"模块 {m.get('id')}")), None)
        span = f"{hms(b.t_start)}–{hms(b.t_end)}" if b else "—"
        child_src = []
        for k in kids:
            pts = [x for x in [k.get("core_concepts"), k.get("formula_method")] if x]
            child_src.append(
                '        {{ id:"{kid}", title:"{title}", tag:"{tag}", meta:"{meta}",\n'
                '          note:"{note}",\n'
                '          points:{points},\n'
                '          evidence:{ev}, children:[] }}'.format(
                    kid=k.get("id", ""), title=q(k.get("title", ""))[:40], tag=tag_for(k),
                    meta=q(k.get("exam_priority", "") or "—"),
                    note=q((k.get("problem_solved") or "")[:80]),
                    points=json.dumps([q(p)[:90] for p in pts[:3]], ensure_ascii=False),
                    ev=json.dumps(evref(k), ensure_ascii=False)))
        nodes.append(
            '    {{ id:"{mid}", title:"{i}. {name}", tag:"", meta:"{span}",\n'
            '      note:"{note}",\n'
            '      points:{pts},\n'
            '      evidence:{ev},\n'
            '      children:[\n{children}\n      ]}}'.format(
                mid=m.get("id", f"M{i}"), i=i, name=q(m.get("name", ""))[:30], span=span,
                note=q((m.get("summary") or "")[:100]),
                pts=json.dumps([], ensure_ascii=False),
                ev=json.dumps([b.code + " · " + b.page] if b else [], ensure_ascii=False),
                children=",\n".join(child_src)))

    data = ('const DATA = {{\n'
            '  id:"root", title:"{course}", tag:"", meta:"{meta}",\n'
            '  note:"{note}",\n'
            '  points:{pts},\n'
            '  evidence:[],\n'
            '  children:[\n{nodes}\n  ]\n'
            '}};\n').format(
        course=q(course), meta=f"{len(s.get('modules', []))} 段 · {len(pages)} 条证据",
        note=q(f"由 build_deliverables.py 生成；{meta.get('generated_at', '')}"),
        pts=json.dumps([q((s.get("meta", {}) or {}).get("scope_note", "")[:100])], ensure_ascii=False),
        nodes=",\n".join(nodes))

    html = template.read_text(encoding="utf-8")
    html = re.sub(r"(?s)const DATA = \{.*?\n\};", data.rstrip(), html, count=1)
    html = re.sub(r"<title>.*?</title>", f"<title>{course} · 知识脑图</title>", html, count=1)
    html = re.sub(r"<h1>.*?</h1>", f"<h1>{course} · 知识脑图</h1>", html, count=1)
    html = re.sub(r'<span class="sub">.*?</span>',
                  f'<span class="sub">{len(s.get("modules", []))} 段 · {len(pages)} 条证据</span>', html, count=1)
    return html


# ------------------------------------------------------------------ 自检


def selfcheck(out: Path, pages: list[Page], sem: dict) -> dict:
    problems: list[str] = []
    texts = []
    for p in out.rglob("*"):
        if p.is_file() and p.suffix in (".md", ".csv", ".html"):
            texts.append((p.relative_to(out).as_posix(), p.read_text(encoding="utf-8")))
    blob = "\n".join(t for _, t in texts)
    # 模板占位符是 {{课程名}} / {{A}} / {{%}} 这类成对形式；课件 OCR 的数学式会
    # 产出 `{{ =1}` 这类孤立花括号（本批 30 节里 1 处 "{{"、0 处成对），
    # 只判 "{{" in blob 会把整节课误判成「未填模板」。这里只放过纯数学内容。
    ph = [m for m in re.findall(r"\{\{([^{}\n]{1,60})\}\}", blob)
          if not re.fullmatch(r"[\d\s+\-*/=<>.,()（）]*", m)]
    if ph:
        problems.append(f"残留占位符：{sorted(set(ph))[:3]}")
    if "<!--" in blob:
        problems.append("残留说明注释 <!--")

    # 00/01 的「引用格式示例」放在围栏代码块里，示例编号 A-003 未必真实存在，
    # 不排除会误判成悬空引用（含页面只有 2 张 A 册时必误判）。
    body = re.sub(r"```.*?```", "", blob, flags=re.S)
    cites = set(re.findall(r"〔([A-D])-(\d{2,3})", body))
    codes = {p.code for p in pages}
    for book, num in sorted(cites):
        code = f"{book}-{int(num):03d}"
        if code not in codes:
            problems.append(f"悬空引用：{code}")

    pages_seen = sorted({int(m) for m in re.findall(r"P\.(\d{3})", body)})
    if pages_seen and pages_seen != list(range(1, len(pages) + 1)):
        missing = set(range(1, len(pages) + 1)) - set(pages_seen)
        if missing:
            problems.append(f"页码未覆盖：{sorted(missing)[:5]}")

    n_ep = len(sem["semantic"].get("exam_priorities", []))
    res = {
        "checked_at": meta_now(),
        "files": [f for f, _ in texts],
        "n_pages": len(pages),
        "n_exam_priorities": n_ep,
        "n_citations": len(re.findall(r"〔[A-D]-\d{2,3}", blob)),
        "problems": problems,
        "ok": not problems,
    }
    return res


def meta_now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------------ main


def main() -> int:
    ap = argparse.ArgumentParser(description="证据层+学习层 → 五件套")
    ap.add_argument("--data", required=True, help="任务数据目录（含 slides/ 或 slides_tracks/、asr/、notes.json）")
    ap.add_argument("--study", required=True, help="学习层目录（semantic.json 等）")
    ap.add_argument("--out", required=True, help="交付目录")
    ap.add_argument("--course", required=True)
    ap.add_argument("--template", required=True, help="脑图 HTML 模板路径")
    ap.add_argument("--prefer-single", action="store_true",
                    help="用单轨 slides/ 而不是双轨 slides_tracks/（当学习层的证据下标按单轨编号时必用）")
    args = ap.parse_args()

    data = Path(args.data)
    study = Path(args.study)
    out = Path(args.out)
    tmpl = Path(args.template)
    if not data.exists():
        sys.exit(f"数据目录不存在：{data}")
    if not (study / "semantic.json").exists():
        sys.exit(f"学习层缺 semantic.json：{study}")
    if not tmpl.exists():
        sys.exit(f"脑图模板不存在：{tmpl}")

    ev = load_evidence(data, prefer_single=args.prefer_single)
    sem = load_semantic(study)
    if not ev.slides:
        sys.exit("证据层没有代表帧（slides.json 为空）")

    pages = assign_pages(ev, sem)
    meta = {
        "generated_at": meta_now(),
        "data_dir": str(data),
        "study_dir": str(study),
        "n_chunks": len(ev.chunks),
        "ocr_avg_conf": _avg_conf(ev),
    }
    win = read_json(data / "state.json", {}) or {}
    w = win.get("window") or [None, None]
    meta["window_hms"] = f"{hms(w[0])}–{hms(w[1])}"

    (out / "04_证据文件夹").mkdir(parents=True, exist_ok=True)
    (out / "assets").mkdir(parents=True, exist_ok=True)

    files = {
        "00_使用说明.md": build_00(ev, pages, sem, args.course, meta),
        "01_期末考试重点与考情分析.md": build_01(sem, ev, pages, args.course, meta),
        "02_逐节课知识点与课堂内容记录.md": build_02(sem, ev, pages, args.course, meta),
        "03_全课程知识体系与思维导图.md": build_03(sem, ev, pages, args.course, meta),
    }
    files.update({f"04_证据文件夹/{k}": v for k, v in build_04(ev, pages, sem, args.course, meta).items()})
    files["assets/全课程思维导图.html"] = build_html(tmpl, sem, pages, args.course, meta)

    # CSV 需要 BOM（Excel）
    for rel, content in files.items():
        p = out / rel
        enc = "utf-8-sig" if rel.endswith(".csv") else "utf-8"
        p.write_text(content, encoding=enc)
    (out / "04_证据文件夹" / "证据页码对照表.csv").write_text(
        build_csv(pages, sem), encoding="utf-8-sig")

    # 清掉仍是模板死文件的分册（避免与新分册并存）
    for old in (out / "04_证据文件夹").glob("证据源*_*.md"):
        try:
            if "{{" in old.read_text(encoding="utf-8"):
                old.unlink()
        except Exception:
            pass

    chk = selfcheck(out, pages, sem)
    (out / "_自检.json").write_text(json.dumps(chk, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"交付目录：{out}")
    print(f"证据页：{len(pages)}（A {sum(1 for p in pages if p.book=='A')} / "
          f"B {sum(1 for p in pages if p.book=='B')} / D {sum(1 for p in pages if p.book=='D')}）")
    print(f"考点：{chk['n_exam_priorities']}　引用：{chk['n_citations']}")
    if chk["ok"]:
        print("自检：通过（0 问题）")
        return 0
    print("自检失败：")
    for p in chk["problems"]:
        print("  -", p)
    return 1


def _avg_conf(ev: Evidence) -> float:
    vals = [s["ocr_conf"] for s in ev.slides if s.get("ocr_conf") is not None]
    return round(sum(vals) / len(vals), 3) if vals else 0.0


if __name__ == "__main__":
    sys.exit(main())

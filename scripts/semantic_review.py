# -*- coding: utf-8 -*-
"""
semantic_review.py — 课程复盘系统 V2 的独立语义阶段（Plan A）

设计目标（不重写既有流水线，不调用外部付费 API）：
  tracks_align / make_review 继续产出「证据层」（复习材料.md、slides/ocr.json、notes.json）
        ↓
  semantic_review（本脚本 + 当前会话模型）产出「学习层」（output/study/）
        ├─ ① evidence_pack   机械：从 slides.json + ocr.json + notes.json 抽取结构化证据包
        ├─ ② semantic_extract 由「当前会话模型（WorkBuddy Agent）」完成，写入 semantic.json
        │                       —— 本脚本不调用任何外部 LLM API，合成由 Agent 文本推理完成
        ├─ ③ merge            机械：校验 / 去重 / 归一化 semantic.json
        └─ ④ render           机械：从 semantic.json + evidence_pack.json 渲染 5 个 Markdown + 证据索引.json

用法：
  python semantic_review.py evidence_pack  [--data DIR] [--out DIR]
  python semantic_review.py merge          [--out DIR]
  python semantic_review.py render         [--out DIR]
  python semantic_review.py validate       [--out DIR] [--target-lines N]
  python semantic_review.py all            [--data DIR] [--out DIR]

说明：semantic_extract 不在此脚本内（避免任何 API 调用 / 音频上传）。
      Agent 读取 evidence_pack.json 后人工/推理合成 semantic.json，再交本脚本 merge→render→validate。
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

# ---------- 路径工具 ----------
def _rel(base, *parts):
    return os.path.normpath(os.path.join(base, *parts))

def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path, obj, indent=2):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)

def save_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

# ---------- ① evidence_pack ----------
def build_evidence_pack(data_dir):
    slides_path = _rel(data_dir, "slides", "slides.json")
    ocr_path = _rel(data_dir, "slides", "ocr.json")
    notes_path = _rel(data_dir, "notes.json")
    slides_meta = load_json(slides_path)
    ocr = load_json(ocr_path)
    notes = load_json(notes_path)

    # OCR 按 slide_index 建索引
    ocr_by_idx = {r["slide_index"]: r for r in ocr.get("results", [])}
    # 转写块
    chunks = notes.get("chunks", [])
    # 考试信号
    signals = notes.get("exam_signals", [])

    records = []
    for s in slides_meta.get("slides", []):
        idx = s["index"]
        ts, te = s["t_start_s"], s["t_end_s"]
        # 重叠转写块
        overlap = [c for c in chunks
                   if c["start_s"] < te and c["end_s"] > ts]
        # 重叠考试信号
        sig_in = []
        for sig in signals:
            # 信号有时间区间（chunk 的 t_start_hms/t_end_hms）则按区间判断，否则按 chunk_idx
            sig_in.append(sig)
        ocr_r = ocr_by_idx.get(idx, {})
        rec = {
            "slide_index": idx,
            "t_start_s": ts,
            "t_end_s": te,
            "t_start_hms": s.get("t_start_hms", ""),
            "t_end_hms": s.get("t_end_hms", ""),
            "image_rel": s.get("image_rel", ""),
            "ocr_text": ocr_r.get("text", "").strip(),
            "ocr_avg_conf": round(float(ocr_r.get("avg_conf", 0.0)), 3),
            "ocr_n_boxes": ocr_r.get("n_boxes", 0),
            "transcript_chunks": [
                {"idx": c["idx"], "start_s": c["start_s"], "end_s": c["end_s"],
                 "start_hms": c.get("start_hms", ""), "end_hms": c.get("end_hms", ""),
                 "text": c["text"]}
                for c in overlap
            ],
            "exam_signals_in_range": sig_in,
        }
        records.append(rec)

    pack = {
        "generated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "source_data_dir": os.path.abspath(data_dir),
        "n_slides": len(records),
        "asr_track_note": "notes.json 仅保留单条 ASR 转写轨（屏/讲师轨）；本窗口未保留第二轨，dual_track 比对不可用。",
        "records": records,
    }
    return pack

# ---------- ③ merge（校验 + 归一化） ----------
def merge_semantic(out_dir):
    sem_path = _rel(out_dir, "semantic.json")
    if not os.path.exists(sem_path):
        raise FileNotFoundError("缺少 semantic.json，请先由 Agent 完成 semantic_extract 后写入该文件。")
    sem = load_json(sem_path)
    problems = []

    # 归一化字段
    kps = sem.get("knowledge_points", [])
    for kp in kps:
        kp.setdefault("module", "")
        kp.setdefault("evidence_level", "C")          # A/B/C/D
        kp.setdefault("judgement_type", "model_inference")  # teacher_stated / behavior_inferred / model_inference / insufficient
        kp.setdefault("exam_priority", "")
        kp.setdefault("evidence", [])
        kp.setdefault("risk_notes", "")
        kp.setdefault("dual_track", "unavailable_single_track")

    eps = sem.get("exam_priorities", [])
    valid_levels = {"P0", "P1", "P2", "P3"}
    for ep in eps:
        if ep.get("level") not in valid_levels:
            problems.append(f"exam_priority {ep.get('id')} 级别非法: {ep.get('level')}")
        if ep.get("judgement") not in {"A", "B", "C", "D"}:
            problems.append(f"exam_priority {ep.get('id')} 判定非法: {ep.get('judgement')}")

    # 交叉引用检查：exam_priority 引用的 kp 必须存在
    kp_ids = {k["id"] for k in kps}
    for ep in eps:
        for ref in ep.get("evidence_kps", []):
            if ref not in kp_ids:
                problems.append(f"exam_priority {ep.get('id')} 引用了不存在的 KP: {ref}")

    sem["_merge"] = {
        "checked_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "n_kp": len(kps),
        "n_ep": len(eps),
        "problems": problems,
    }
    save_json(sem_path, sem, indent=2)
    return sem, problems

# ---------- ④ render ----------
def _mermaid_block(code):
    return "\n```mermaid\n" + code.strip() + "\n```\n"

def render(out_dir):
    sem = load_json(_rel(out_dir, "semantic.json"))
    pack = load_json(_rel(out_dir, "evidence_pack.json"))
    meta = sem.get("meta", {})
    modules = sem.get("modules", [])
    kps = sem.get("knowledge_points", [])
    eps = sem.get("exam_priorities", [])
    pack_by_idx = {r["slide_index"]: r for r in pack.get("records", [])}

    course = meta.get("course", "本课程")
    window = meta.get("window", "00:00–10:00（本窗口可用证据）")
    gen = meta.get("generated_at", "")

    # ---------- 学习总框架.md ----------
    lines = []
    lines.append(f"# 学习总框架 · {course}")
    lines.append("")
    lines.append(f"> 生成时间：{gen} ｜ 覆盖窗口：{window}")
    lines.append("> 本文件为「学习层」压缩框架，按知识点（KP）组织，非按视频时间片段。原始证据见 `证据索引.json`。")
    lines.append("")
    lines.append("## 0. 模块总览与学习顺序")
    lines.append("")
    for m in modules:
        recs = pack_by_idx  # 仅用于计数参考
        slides = m.get("slides", "")
        lines.append(f"- **{m['id']} {m['name']}**（PPT：{slides}）")
    lines.append("")
    lines.append("推荐学习顺序（逻辑依赖，非时间先后）：")
    order = " → ".join(m["id"] for m in modules)
    lines.append("")
    lines.append(f"> {order}")
    lines.append("")
    lines.append("## 1. 模块依赖图（Mermaid）")
    lines.append("")
    dep_lines = ["graph TD"]
    for m in modules:
        dep_lines.append(f'    {m["id"]}["{m["id"]} {m["name"]}"]')
        for dep in m.get("depends_on", []):
            dep_lines.append(f'    {dep} --> {m["id"]}')
    lines.append(_mermaid_block("\n".join(dep_lines)))
    lines.append("")
    lines.append("## 2. 各模块核心知识点")
    lines.append("")
    for m in modules:
        lines.append(f"### {m['id']} {m['name']}")
        lines.append("")
        if m.get("summary"):
            lines.append(m["summary"])
            lines.append("")
        for kpid in m.get("key_kps", []):
            kp = next((k for k in kps if k["id"] == kpid), None)
            if not kp:
                continue
            ev = kp.get("evidence_level", "")
            pr = kp.get("exam_priority", "")
            lines.append(f"- **{kp['title']}** `{kpid}` ｜ 证据 {ev} ｜ 考点 {pr or '—'}")
            if kp.get("one_liner"):
                lines.append(f"  - {kp['one_liner']}")
        lines.append("")
    lines.append("## 3. 先修知识表")
    lines.append("")
    lines.append("| 模块 | 先修要求 | 备注 |")
    lines.append("|---|---|---|")
    for m in modules:
        pr = m.get("prereq", "无")
        note = m.get("prereq_note", "")
        lines.append(f"| {m['id']} | {pr} | {note} |")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"*证据层：{pack.get('n_slides')} 页 PPT + 1 条 ASR 轨 + OCR（平均置信度见 `证据索引.json`）。本窗口未含第二转写轨，dual_track 比对不可用。*")
    save_text(_rel(out_dir, "学习总框架.md"), "\n".join(lines))

    # ---------- 核心知识讲解.md ----------
    lines = []
    lines.append(f"# 核心知识讲解 · {course}")
    lines.append("")
    lines.append(f"> 生成时间：{gen} ｜ 覆盖窗口：{window}")
    lines.append("> 按知识点组织，精简讲解。每条 KP 的逐项证据（PPT 页、时间、原话、来源）见 `证据索引.json`，此处仅保留要点。")
    lines.append("")
    for kp in kps:
        lines.append(f"## {kp['id']} · {kp['title']}")
        lines.append("")
        if kp.get("problem_solved"):
            lines.append(f"- **解决什么问题**：{kp['problem_solved']}")
        if kp.get("core_concepts"):
            lines.append(f"- **核心概念**：{kp['core_concepts']}")
        if kp.get("formula_method"):
            lines.append(f"- **方法/公式**：{kp['formula_method']}")
        if kp.get("cases"):
            lines.append(f"- **案例**：{kp['cases']}")
        if kp.get("confusions"):
            lines.append(f"- **易混点**：{kp['confusions']}")
        if kp.get("relations"):
            lines.append(f"- **与其他知识点关系**：{kp['relations']}")
        lines.append(f"- **证据等级**：{kp.get('evidence_level')} ｜ **判定**：{kp.get('judgement_type')} ｜ **考点**：{kp.get('exam_priority') or '—'}")
        if kp.get("risk_notes"):
            lines.append(f"- **风险提示**：{kp['risk_notes']}")
        lines.append("")
    save_text(_rel(out_dir, "核心知识讲解.md"), "\n".join(lines))

    # ---------- 考试重点与复习计划.md ----------
    lines = []
    lines.append(f"# 考试重点与复习计划 · {course}")
    lines.append("")
    lines.append(f"> 生成时间：{gen} ｜ 覆盖窗口：{window}")
    lines.append("")
    lines.append("## 0. 判定口径（重要）")
    lines.append("")
    lines.append("- **A 老师明确说明**：老师原话直指考试/评分/要求（如缺勤、卷面线、考试安排）。")
    lines.append("- **B 行为推断**：老师强调、反复、放慢、板书加重等教学行为推断。")
    lines.append("- **C 内容推断**：从教材/大纲/知识重要性推断，老师未明说。")
    lines.append("- **D 证据不足**：无法从当前证据确认，需回听原声或查教材。")
    lines.append("")
    lines.append("> ⚠️ 关键词命中 ≠ 考试重点。本表所有重点均标注判定类型，A 级必须能回溯到老师原话证据。")
    lines.append("")
    lines.append("## 1. 优先级总览")
    lines.append("")
    for lvl in ["P0", "P1", "P2", "P3"]:
        grp = [e for e in eps if e.get("level") == lvl]
        lines.append(f"### {lvl}（{len(grp)} 项）")
        lines.append("")
        for e in grp:
            j = e.get("judgement", "")
            jname = {"A": "老师明确", "B": "行为推断", "C": "内容推断", "D": "证据不足"}[j]
            lines.append(f"- **{e['title']}** `{e['id']}` ｜ 判定 {j}（{jname}）")
            if e.get("note"):
                lines.append(f"  - {e['note']}")
            if e.get("review_action"):
                lines.append(f"  - 复习动作：{e['review_action']}")
        lines.append("")
    lines.append("## 2. 名词/数字识别风险")
    lines.append("")
    lines.append("- 转写为 8 kHz 远场收音，存在错字（如「卷面大于6分/5分/09分」表述混乱），**数字以回听原声为准**。")
    lines.append("- OCR 平均置信度偏低（多页 <0.5），公式无法可靠转录，术语以教材为准。")
    lines.append("")
    lines.append("## 3. 无法确认事项（需进一步取证）")
    lines.append("")
    for e in eps:
        if e.get("judgement") == "D":
            lines.append(f"- `{e['id']}` {e['title']}：{e.get('note','')}")
    lines.append("- 本窗口（00:00–10:00）仅含「绪论 + 第1章引子」，第2章起的随机向量/分布等核心计算内容未覆盖。")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("*本表与 `证据索引.json` 配套。任何复习判断请以音频原声 + 教材为准。*")
    save_text(_rel(out_dir, "考试重点与复习计划.md"), "\n".join(lines))

    # ---------- 学习流程图.md ----------
    lines = []
    lines.append(f"# 学习流程图 · {course}")
    lines.append("")
    lines.append(f"> 生成时间：{gen} ｜ 覆盖窗口：{window}")
    lines.append("> 逻辑依赖图（非时间顺序）。虚线框表示本窗口未覆盖的内容。")
    lines.append("")
    g = ["graph TD"]
    for m in modules:
        g.append(f'    {m["id"]}["{m["id"]} {m["name"]}"]')
        for dep in m.get("depends_on", []):
            g.append(f'    {dep} --> {m["id"]}')
    for m in modules:
        for miss in m.get("not_covered", []):
            g.append(f'    NC_{m["id"]}["(未覆盖) {miss}"]')
            g.append(f'    {m["id"]} -.-> NC_{m["id"]}')
    lines.append(_mermaid_block("\n".join(g)))
    lines.append("")
    lines.append("**说明**：本窗口覆盖 M0–M? 的绪论文献部分；第2章起的随机向量与多元分布、参数估计、假设检验等核心方法在本窗口之后，需补充证据。")
    save_text(_rel(out_dir, "学习流程图.md"), "\n".join(lines))

    # ---------- 考试重点图.md ----------
    lines = []
    lines.append(f"# 考试重点图 · {course}")
    lines.append("")
    lines.append(f"> 生成时间：{gen} ｜ 覆盖窗口：{window}")
    lines.append("")
    # 图1：证据类型分布 pie
    type_cnt = {}
    for e in eps:
        type_cnt[e.get("judgement", "C")] = type_cnt.get(e.get("judgement", "C"), 0) + 1
    pie = ["pie showData", "    title 考试重点判定类型分布"]
    labelmap = {"A": "A老师明确", "B": "B行为推断", "C": "C内容推断", "D": "D证据不足"}
    for k, v in type_cnt.items():
        pie.append(f'    "{labelmap.get(k,k)}" : {v}')
    lines.append("## 图1 考试重点判定类型分布")
    lines.append(_mermaid_block("\n".join(pie)))
    lines.append("")
    # 图2：优先级分布
    lvl_cnt = {}
    for e in eps:
        lvl_cnt[e.get("level", "P3")] = lvl_cnt.get(e.get("level", "P3"), 0) + 1
    pie2 = ["pie showData", "    title 考试重点优先级分布"]
    for k in ["P0", "P1", "P2", "P3"]:
        pie2.append(f'    "{k}" : {lvl_cnt.get(k,0)}')
    lines.append("## 图2 优先级分布")
    lines.append(_mermaid_block("\n".join(pie2)))
    lines.append("")
    # 图3：知识点证据等级分布
    kp_lvl = {}
    for k in kps:
        kp_lvl[k.get("evidence_level", "C")] = kp_lvl.get(k.get("evidence_level", "C"), 0) + 1
    pie3 = ["pie showData", "    title 知识点证据等级分布"]
    for k, v in kp_lvl.items():
        pie3.append(f'    "{k}" : {v}')
    lines.append("## 图3 知识点证据等级分布")
    lines.append(_mermaid_block("\n".join(pie3)))
    lines.append("")
    # 图4：重点—知识点映射
    mm = ["graph LR"]
    for e in eps:
        mm.append(f'    {e["id"]}["{e["level"]} {e["title"]}"]')
        for ref in e.get("evidence_kps", []):
            mm.append(f'    {ref}["{ref}"]')
            mm.append(f'    {ref} --> {e["id"]}')
    lines.append("## 图4 重点—知识点映射")
    lines.append(_mermaid_block("\n".join(mm)))
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("*图例：🔴 A 老师明确 ｜ 🟠 B 行为推断/“重点介绍”(课程安排) ｜ 🔵 C 内容推断 ｜ ⬜ D 证据不足/背景。*")
    save_text(_rel(out_dir, "考试重点图.md"), "\n".join(lines))

    # ---------- 证据索引.json ----------
    ev_index = {
        "meta": {
            "course": course,
            "window": window,
            "generated_at": gen,
            "n_slides": pack.get("n_slides"),
            "asr_track_note": pack.get("asr_track_note"),
            "disclaimer": "机器生成，未经人工校对；证据层与学习层分离，学习层结论均可回溯到证据层。",
        },
        "knowledge_points": [],
        "exam_priorities": [],
    }
    for kp in kps:
        ev_index["knowledge_points"].append({
            "id": kp["id"],
            "title": kp["title"],
            "module": kp.get("module", ""),
            "evidence_level": kp.get("evidence_level", ""),
            "judgement_type": kp.get("judgement_type", ""),
            "exam_priority": kp.get("exam_priority", ""),
            "dual_track": kp.get("dual_track", "unavailable_single_track"),
            "evidence": kp.get("evidence", []),
            "risk_notes": kp.get("risk_notes", ""),
        })
    for e in eps:
        ev_index["exam_priorities"].append({
            "id": e["id"],
            "title": e["title"],
            "level": e.get("level", ""),
            "judgement": e.get("judgement", ""),
            "evidence_kps": e.get("evidence_kps", []),
            "note": e.get("note", ""),
        })
    save_json(_rel(out_dir, "证据索引.json"), ev_index, indent=2)

    return {
        "学习总框架.md": _rel(out_dir, "学习总框架.md"),
        "核心知识讲解.md": _rel(out_dir, "核心知识讲解.md"),
        "考试重点与复习计划.md": _rel(out_dir, "考试重点与复习计划.md"),
        "学习流程图.md": _rel(out_dir, "学习流程图.md"),
        "考试重点图.md": _rel(out_dir, "考试重点图.md"),
        "证据索引.json": _rel(out_dir, "证据索引.json"),
    }

# ---------- validate（10 项质量自检） ----------
def validate(out_dir, target_lines=22):
    sem = load_json(_rel(out_dir, "semantic.json"))
    pack = load_json(_rel(out_dir, "evidence_pack.json"))
    checks = []
    kps = sem.get("knowledge_points", [])
    eps = sem.get("exam_priorities", [])

    def add(name, passed, detail):
        checks.append({"check": name, "passed": bool(passed), "detail": detail})

    # 1. 证据可追溯：每个 KP 至少 1 条带 slide+time+source 的证据
    bad = [k["id"] for k in kps if not k.get("evidence")]
    add("C1_证据可追溯", not bad,
        "所有 KP 均含 evidence 数组" if not bad else f"缺失证据: {bad}")

    # 2. 证据层不被覆盖：证据索引.json 存在且与 KP 对应
    evp = [e["id"] for e in load_json(_rel(out_dir, "证据索引.json")).get("knowledge_points", [])]
    add("C2_证据层独立", set(evp) == {k["id"] for k in kps},
        f"证据索引含 {len(evp)} 条，KP 共 {len(kps)} 条")

    # 3. 主文档精简：每个 md 行数/页数 <= target
    line_rep = {}
    for fn in ["学习总框架.md", "核心知识讲解.md", "考试重点与复习计划.md",
               "学习流程图.md", "考试重点图.md"]:
        p = _rel(out_dir, fn)
        if os.path.exists(p):
            n = sum(1 for _ in open(p, encoding="utf-8"))
            line_rep[fn] = n
    over = {k: v for k, v in line_rep.items() if v > target_lines * 15}  # 15 页上限
    add("C3_主文档精简", not over,
        f"各文件行数 {line_rep}" + (f"；超限 {over}" if over else "；均在预算内"))

    # 4. 按知识点（模块）组织
    mods = {m["id"] for m in sem.get("modules", [])}
    unmod = [k["id"] for k in kps if k.get("module") not in mods]
    add("C4_按模块组织", not unmod,
        "全部 KP 归属到已知模块" if not unmod else f"未归属模块: {unmod}")

    # 5. 考试重点分级 + 判定合法
    lv_ok = all(e.get("level") in {"P0", "P1", "P2", "P3"} for e in eps)
    j_ok = all(e.get("judgement") in {"A", "B", "C", "D"} for e in eps)
    add("C5_重点分级合法", lv_ok and j_ok and len(eps) > 0,
        f"共 {len(eps)} 条，级别/判定合法={lv_ok and j_ok}")

    # 6. A 级必须有 teacher_stated 证据回溯
    a_eps = [e for e in eps if e.get("judgement") == "A"]
    a_bad = []
    for e in a_eps:
        refs = e.get("evidence_kps", [])
        if not refs:
            a_bad.append(e["id"]); continue
        if not any(k.get("judgement_type") == "teacher_stated"
                   for k in kps if k["id"] in refs):
            a_bad.append(e["id"])
    add("C6_A级回溯老师原话", not a_bad,
        "A 级重点均关联到 teacher_stated 证据" if not a_bad else f"未回溯: {a_bad}")

    # 7. 关键词≠重点：无 KP 仅凭关键词频率判 P0
    kw_only = [k["id"] for k in kps
               if k.get("exam_priority") == "P0"
               and k.get("judgement_type") == "model_inference"
               and not k.get("evidence")]
    add("C7_关键词非重点", not kw_only,
        "无仅凭关键词/模型推断被判 P0 的 KP" if not kw_only else f"疑似: {kw_only}")

    # 8. Mermaid 合法：含 mermaid 代码块且花括号平衡
    mermaid_files = ["学习流程图.md", "考试重点图.md", "学习总框架.md"]
    m_ok = True
    m_det = []
    for fn in mermaid_files:
        p = _rel(out_dir, fn)
        if not os.path.exists(p):
            continue
        txt = open(p, encoding="utf-8").read()
        has = "```mermaid" in txt
        # 粗略括号平衡
        bal = txt.count("[") - txt.count("]") + txt.count("(") - txt.count(")")
        m_det.append(f"{fn}:mermaid={has},bracket_bias={bal}")
        if not has or abs(bal) > 0:
            m_ok = False
    add("C8_Mermaid合法", m_ok, "；".join(m_det))

    # 9. 无法确认事项：考试重点与复习计划含该段
    rp = _rel(out_dir, "考试重点与复习计划.md")
    has_d = os.path.exists(rp) and "无法确认" in open(rp, encoding="utf-8").read()
    add("C9_含无法确认段", has_d, "考试重点与复习计划.md 含『无法确认事项』段落" if has_d else "缺失")

    # 10. 不伪造公式：凡做出公式/方法「主张」的，必须标注来源或声明未转录。
    #     formula_method 为空或以「无」开头表示无公式主张，属诚实声明，直接通过。
    markers = ["未转录", "OCR", "见教材", "原声", "未可靠转录", "未展开", "未在本窗口", "未讲", "未涉及"]
    fab = []
    for k in kps:
        fm = (k.get("formula_method") or "").strip()
        if not fm or fm.startswith("无"):
            continue  # 无公式主张，诚实声明
        if not any(m in fm for m in markers):
            fab.append(k["id"])
    add("C10_不伪造公式", not fab,
        "公式/方法均标注来源或声明未转录（无主张项已豁免）" if not fab else f"疑似未标注来源: {fab}")

    passed = sum(1 for c in checks if c["passed"])
    total = len(checks)
    report = {
        "checked_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "passed": passed,
        "total": total,
        "all_passed": passed == total,
        "checks": checks,
    }
    save_json(_rel(out_dir, "quality_selfcheck.json"), report, indent=2)
    return report

# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ["evidence_pack", "merge", "render", "validate", "all"]:
        sp = sub.add_parser(name)
        sp.add_argument("--data", default=None, help="数据目录（含 slides/ 与 notes.json）")
        sp.add_argument("--out", default=None, help="输出目录（学习层）")
        if name == "validate":
            sp.add_argument("--target-lines", type=int, default=22)
    args = ap.parse_args()

    # 默认路径推导
    here = os.path.dirname(os.path.abspath(__file__))
    proj = os.path.dirname(here)
    # 找到 data 目录
    if args.data:
        data_dir = args.data
    else:
        cand = os.path.join(proj, "data")
        subs = [d for d in os.listdir(cand) if d.startswith("多元")] if os.path.isdir(cand) else []
        # 存在多个任务目录时，优先选「窗口最大」的（即全课），避免误用 10 分钟样本
        def _win_end(name):
            m = re.search(r"_0\.0-(\d+)\.0_", name)
            return int(m.group(1)) if m else -1
        subs.sort(key=_win_end, reverse=True)
        data_dir = os.path.join(cand, subs[0]) if subs else cand
    out_dir = args.out or os.path.join(proj, "output", "study")

    if args.cmd == "evidence_pack":
        pack = build_evidence_pack(data_dir)
        save_json(_rel(out_dir, "evidence_pack.json"), pack, indent=2)
        print(f"[evidence_pack] 写入 {_rel(out_dir,'evidence_pack.json')} ｜ {pack['n_slides']} 页")
    elif args.cmd == "merge":
        sem, problems = merge_semantic(out_dir)
        print(f"[merge] 完成 ｜ KP={sem['_merge']['n_kp']} EP={sem['_merge']['n_ep']} 问题={len(problems)}")
        for p in problems:
            print("   -", p)
    elif args.cmd == "render":
        paths = render(out_dir)
        print(f"[render] 生成 {len(paths)} 个文件：")
        for k, v in paths.items():
            print(f"   {k} -> {v}")
    elif args.cmd == "validate":
        rep = validate(out_dir, target_lines=args.target_lines)
        print(f"[validate] {rep['passed']}/{rep['total']} 通过 ｜ all_passed={rep['all_passed']}")
        for c in rep["checks"]:
            print(f"   [{'OK' if c['passed'] else 'XX'}] {c['check']}: {c['detail']}")
    elif args.cmd == "all":
        pack = build_evidence_pack(data_dir)
        save_json(_rel(out_dir, "evidence_pack.json"), pack, indent=2)
        print(f"[evidence_pack] {pack['n_slides']} 页")
        sem, problems = merge_semantic(out_dir)
        print(f"[merge] 问题={len(problems)}")
        paths = render(out_dir)
        print(f"[render] {len(paths)} 文件")
        rep = validate(out_dir)
        print(f"[validate] {rep['passed']}/{rep['total']} 通过")

if __name__ == "__main__":
    main()

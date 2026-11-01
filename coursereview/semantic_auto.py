# -*- coding: utf-8 -*-
"""无人值守语义层：证据层 → semantic.json（规则法，非 LLM）。

为什么需要它：`semantic_review` 的第二段（semantic.json）原本由会话模型完成，
这无法无人值守。这里用确定性规则生成一版**可用的粗粒度语义层**：

  章节标题  ← OCR 文本里的「第N章 / N.N 小节」
  知识点    ← 「定义/定理/性质/例 N.M」标记 + 小节标题
  考点      ← 课堂转写里的考试信号（A 级）+ 小节密度（P1/P2）

产物会明确标注 `generated_by=rule_based` 与精度警告；
接上会话模型时，用同一份 evidence_pack 重写 semantic.json 即可获得更细的版本。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

CN_NUM = "一二三四五六七八九十"
RE_CHAIN = re.compile(rf"第([{CN_NUM}\d]+)[章讲节]")
RE_SEC = re.compile(r"(\d+\s*\.\s*\d+)\s*([^\s，。；:：]{0,14})")
RE_DEF = re.compile(r"(定义|定理|性质|引理|推论|例)\s*(\d+(?:\s*\.\s*\d+)*)")
EXAM_KW = ("考试", "考核", "平时", "成绩", "作业", "学时", "考勤", "缺勤", "重点", "掌握")


def hms(sec: float) -> str:
    sec = int(sec or 0)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _clean(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "")).strip()


def _norm_tree(obj):
    """把半角括号统一成全角：渲染的 Mermaid 校验按整篇文件统计括号配对，
    而 OCR 文本里半角括号经常不配对，会把合法的图判成非法。"""
    if isinstance(obj, dict):
        return {k: (v if k in ("id",) else _norm_tree(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_norm_tree(x) for x in obj]
    if isinstance(obj, str):
        return (obj.replace("[", "［").replace("]", "］")
                   .replace("(", "（").replace(")", "）"))
    return obj

def _t(s: str, limit: int = 40) -> str:
    """标题消毒：Mermaid 节点标签里的方括号/竖线/反引号会破坏语法。"""
    s = _clean(s).replace("[", "（").replace("]", "）").replace("|", "／").replace("`", "")
    return re.sub(r"（{2,}", "（", s)[:limit]


def _section_of(text: str) -> tuple[str, str]:
    """从一页 OCR 文本推断所属章节：(key, 人类可读标题)。找不到返回 ("", "")。"""
    t = _clean(text)
    m = RE_SEC.search(t)
    if m:
        return f"S{m.group(1).replace(' ', '')}", f"{m.group(1)}{m.group(2)}".strip()
    m = RE_CHAIN.search(t)
    if m:
        return f"C{m.group(1)}", f"第{m.group(1)}章"
    return "", ""


def build(data_dir: Path) -> dict:
    sj = json.loads((data_dir / "slides" / "slides.json").read_text(encoding="utf-8"))
    ocr = json.loads((data_dir / "slides" / "ocr.json").read_text(encoding="utf-8"))
    notes = json.loads((data_dir / "notes.json").read_text(encoding="utf-8"))
    win = json.loads((data_dir / "state.json").read_text(encoding="utf-8")).get("window", [0, 0])
    ocr_by = {r["slide_index"]: r for r in ocr.get("results", [])}

    slides = []
    for s in sj.get("slides", []):
        o = ocr_by.get(s["index"], {})
        text = _clean(o.get("text"))
        sec_key, sec_title = _section_of(text)
        slides.append({
            "index": s["index"], "t0": s.get("t_start_s"), "t1": s.get("t_end_s"),
            "trep": s.get("t_rep_s"), "text": text, "conf": o.get("avg_conf"),
            "nbox": o.get("n_boxes"), "sec_key": sec_key, "sec_title": sec_title,
            "image_rel": s.get("image_rel"),
        })

    # ---------- 模块：连续同章节归并；无章节标记的按 8 分钟窗口切 ----------
    modules, cur = [], None
    for sl in slides:
        key = sl["sec_key"] or f"T{int((sl['t0'] or 0) // 480)}"
        title = sl["sec_title"] or f"{hms(sl['t0'])} 起"
        if cur and cur["key"] == key:
            cur["slides"].append(sl)
            cur["t1"] = sl["t1"]
        else:
            cur = {"key": key, "name": title, "slides": [sl], "t0": sl["t0"], "t1": sl["t1"]}
            modules.append(cur)

    # OCR 里的章节标记很碎（本课切出 30 段），合并相邻段，最多 MAX_MOD 个模块，
    # 否则「学习总框架 / 核心知识讲解」会被撑爆（validate 的 C3 会判不过）。
    MAX_MOD = 12
    if len(modules) > MAX_MOD:
        step = len(modules) / MAX_MOD
        merged = []
        for k in range(MAX_MOD):
            chunk = modules[int(k * step):int((k + 1) * step)] or [modules[min(k, len(modules) - 1)]]
            first, last = chunk[0], chunk[-1]
            merged.append({
                "key": f"G{k}", "name": first["name"],
                "slides": [s for c in chunk for s in c["slides"]],
                "t0": first["t0"], "t1": last["t1"],
            })
        modules = merged
    mod_out, kps, kp_by_mod = [], [], {}
    seen_global: set[str] = set()   # 跨模块去重：同一「定义2.1」只取第一次出现
    MAX_KP_PER_MOD = 3              # 每模块上限，避免规则法把主文档撑爆
    for i, m in enumerate(modules):
        mid = f"M{i}"
        kps_here = []
        # 知识点：跨模块去重 + 每模块上限（避免规则法把主文档撑爆）
        for sl in m["slides"]:
            for kind, num in RE_DEF.findall(sl["text"]):
                key = f"{kind}{num.replace(' ', '')}"
                if key in seen_global or len(kps_here) >= MAX_KP_PER_MOD:
                    continue
                seen_global.add(key)
                pos = sl["text"].find(key)
                snippet = sl["text"][pos:pos + 90] if pos >= 0 else sl["text"][:90]
                kp_id = f"KP-{mid}-{len(kps_here) + 1:02d}"
                kp = {
                    "id": kp_id,
                    "title": _t(f"{key}：{_clean(snippet)[len(key):len(key) + 28] or '（课件原文）'}"),
                    "module": mid,
                    "problem_solved": f"课件第 {sl['index']:03d} 页给出的{kind}",
                    "core_concepts": _clean(snippet)[:160],
                    "formula_method": f"见课件原文（OCR 转录自 slide {sl['index']:03d}）",
                    "cases": "",
                    "confusions": "",
                    "relations": "",
                    "exam_priority": "",
                    "evidence_level": "C",
                    "judgement_type": "behavior_inferred",
                    "dual_track": "unavailable_rule_based",
                    "evidence": [{
                        "slide": sl["index"],
                        "time_hms": hms(sl["trep"]),
                        "source_id": f"ocr:slide{sl['index']:03d}",
                        "image": sl["image_rel"],
                        "quote": _clean(snippet)[:120],
                    }],
                }
                kps_here.append(kp)
        if not kps_here:
            sl = m["slides"][0]
            kps_here.append({
                "id": f"KP-{mid}-01",
                "title": _t(m["name"] or hms(m["t0"])),
                "module": mid,
                "problem_solved": f"本段主线（{hms(m['t0'])}–{hms(m['t1'])}）",
                "core_concepts": _clean(sl["text"])[:160] or "（本段无可用 OCR 文本）",
                "formula_method": "无（本段未识别到公式主张）",
                "cases": "", "confusions": "", "relations": "",
                "exam_priority": "", "evidence_level": "C",
                "judgement_type": "behavior_inferred",
                "dual_track": "unavailable_rule_based",
                "evidence": [{
                    "slide": sl["index"], "time_hms": hms(sl["trep"]),
                    "source_id": f"ocr:slide{sl['index']:03d}",
                    "image": sl["image_rel"], "quote": _clean(sl["text"])[:120],
                }],
            })
        kps.extend(kps_here)
        kp_by_mod[mid] = [k["id"] for k in kps_here]
        mod_out.append({
            "id": mid,
            "name": _t(m["name"] or f"第 {i + 1} 段"),
            "slides": f"{m['slides'][0]['index']}–{m['slides'][-1]['index']}（{hms(m['t0'])}–{hms(m['t1'])}）",
            "summary": _clean(" ".join(s["text"] for s in m["slides"]))[:180],
            "key_kps": kp_by_mod[mid],
            "depends_on": [] if i == 0 else [mod_out[-1]["id"]] if mod_out else [],
            "prereq": "" if i == 0 else (mod_out[-1]["name"] if mod_out else ""),
            "prereq_note": "" if i == 0 else "按课程时间顺序推定前置",
        })

    # 模块时间区间（供考试信号定位），与 mod_out 一一对应
    mod_ranges = [{"id": mod_out[i]["id"], "t0": modules[i]["t0"], "t1": modules[i]["t1"],
                   "slides": modules[i]["slides"], "key_kps": mod_out[i]["key_kps"]} for i in range(len(mod_out))]

    # ---------- 考点：A 级来自课堂考试信号（独立成知识点，避免关键词推断） ----------
    eps, used = [], set()

    def to_s(hms_text: str) -> int:
        try:
            parts = [int(x) for x in str(hms_text).split(":")]
        except Exception:
            return 0
        return parts[0] * 3600 + parts[1] * 60 + (parts[2] if len(parts) > 2 else 0)

    for j, sg in enumerate(notes.get("exam_signals", []) or [], start=1):
        t = sg.get("t_start_hms") or ""
        ts = to_s(t)
        host = next((m for m in mod_ranges if m["slides"] and (m["t0"] or 0) <= ts <= (m["t1"] or 0)), None)
        if host is None:
            host = mod_ranges[0] if mod_ranges else None
        if host is None:
            break
        mid = host["id"]
        kp = {
            "id": f"KP-{mid}-EX{j:02d}",
            "title": _t(sg.get("note") or f"课堂强调 @{t}"),
            "module": mid,
            "problem_solved": "老师课堂上就考核/教学安排做出的表述",
            "core_concepts": _clean(sg.get("quote"))[:160] or "（转写为空）",
            "formula_method": "无（非公式类主张）",
            "cases": "",
            "confusions": "",
            "relations": "",
            "exam_priority": "P0",
            "evidence_level": "A",
            "judgement_type": "teacher_stated",
            "dual_track": "track0_audio",
            "evidence": [{
                "slide": host["slides"][0]["index"],
                "time_hms": t,
                "source_id": f"asr:chunk{sg.get('chunk_idx')}",
                "image": host["slides"][0].get("image_rel"),
                "quote": _clean(sg.get("quote"))[:120],
            }],
        }
        kps.append(kp)
        used.add(kp["id"])
        host["key_kps"].insert(0, kp["id"])
        eps.append({
            "id": f"EP-P0-{j:02d}",
            "title": kp["title"],
            "level": "P0",
            "judgement": "A",
            "evidence_kps": [kp["id"]],
            "note": f"课堂原话（转写直读，未回听）：{_clean(sg.get('quote'))[:60]}",
            "review_action": "回听原话确认后按必考处理",
        })
    rest = [k for k in kps if k["id"] not in used]
    rest.sort(key=lambda k: _marker_weight(k), reverse=True)
    for idx, k in enumerate(rest, start=1):
        lvl = "P1" if idx <= max(3, len(rest) // 3) else "P2"
        k["exam_priority"] = lvl
        eps.append({
            "id": f"EP-{lvl}-{idx:02d}",
            "title": k["title"][:40],
            "level": lvl,
            "judgement": "C",
            "evidence_kps": [k["id"]],
            "note": "由课件定义/定理密度推定，非老师明示" if lvl == "P1" else "课件常规内容",
            "review_action": "对照课件原文掌握" if lvl == "P1" else "了解即可",
        })

    sem = {
        "meta": {
            "course": "",
            "recorded_at": "",
            "window": win,
            "generated_at": time_now(),
            "generated_by": "rule_based: semantic_auto（无人值守规则法，非 LLM）",
            "n_slides": len(slides),
            "asr_track_note": notes.get("disclaimer", ""),
            "scope_note": f"规则法切分：{len(mod_out)} 段 / {len(kps)} 知识点 / {len(eps)} 考点",
            "disclaimer": "本文件由确定性规则生成，粒度粗于人工/LLM 语义层："
                          "章节靠 OCR 标题与时间窗切分，考点靠课堂考试信号与定义密度排序，"
                          "公式与专名未经人工校对。可用于无人值守出稿，但不应取代人工复核。",
        },
        "modules": mod_out,
        "knowledge_points": kps,
        "exam_priorities": eps,
    }
    # ---- 总量限流：规则法容易把知识点堆到 50+，会把「核心知识讲解」撑过预算 ----
    MAX_KP_TOTAL = 30
    MAX_EP_TOTAL = 32
    if len(kps) > MAX_KP_TOTAL:
        # 优先保留有考试信号的 P0/P1，其余截断
        ordered = ([k for k in kps if k["exam_priority"] in ("P0", "P1")]
                   + [k for k in kps if k["exam_priority"] not in ("P0", "P1")])
        kps = ordered[:MAX_KP_TOTAL]
        keep_ids = {k["id"] for k in kps}
        eps = [{"**": 0, **e} for e in eps]
        eps = [{k: v for k, v in e.items() if k != "**"} for e in eps]
        for e in eps:
            e["evidence_kps"] = [i for i in e.get("evidence_kps", []) if i in keep_ids]
        eps = [e for e in eps if e.get("evidence_kps")]
        for m in mod_out:
            m["key_kps"] = [i for i in m.get("key_kps", []) if i in keep_ids]
    if len(eps) > MAX_EP_TOTAL:
        eps = eps[:MAX_EP_TOTAL]

    # 限流后的结果要写回 dict（前面的 kps/eps 只是局部变量）
    sem['knowledge_points'] = kps
    sem['exam_priorities'] = eps
    for _m in sem['modules']:
        _keep = {k['id'] for k in kps}
        _m['key_kps'] = [i for i in _m.get('key_kps', []) if i in _keep]

    sem = _norm_tree(sem)
    return sem


def s_idx(kp: dict, slides: list[dict]) -> int:
    try:
        return int(kp["evidence"][0]["slide"])
    except Exception:
        return slides[0]["index"] if slides else 1


def _marker_weight(kp: dict) -> int:
    t = kp["title"]
    w = 0
    for kw, s in (("定义", 5), ("定理", 4), ("性质", 3), ("例", 2)):
        if t.startswith(kw):
            w += s
    return w


def time_now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def write(data_dir: Path, out_dir: Path, course: str = "", recorded_at: str = "") -> Path:
    sem = build(data_dir)
    sem["meta"]["course"] = course
    sem["meta"]["recorded_at"] = recorded_at
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / "semantic.json"
    p.write_text(json.dumps(sem, ensure_ascii=False, indent=2), encoding="utf-8")
    return p

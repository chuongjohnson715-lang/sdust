# -*- coding: utf-8 -*-
"""课程与活动发现（DOM 驱动，不依赖平台内部接口）。

平台是 SPA（TronClass），页面结构可能随版本变化，所以：
  · 主路径：按链接/哈希模式收集候选，再按文字关键字挑选；
  · 兜底：把页面可见文本与候选链接整段落盘到 .state/discover_raw/，便于人工核对后改选择器；
  · 任何「找不到」都必须显式失败并给出已落盘的证据，不静默返回空。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .browser import Session, goto_checked
from .config import Config, STATE_DIR, safe_name

RAW_DIR = STATE_DIR / "discover_raw"


def _dump(page, tag: str) -> Path:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{int(time.time())}_{safe_name(tag)}"
    (RAW_DIR / f"{stem}.txt").write_text(page.inner_text("body", timeout=15000) or "", encoding="utf-8")
    (RAW_DIR / f"{stem}.html").write_text(page.content(), encoding="utf-8")
    return RAW_DIR / f"{stem}.txt"


def list_courses(session: Session, cfg: Config) -> list[dict]:
    page = session.page
    if not goto_checked(session, cfg.user_index):
        raise SystemExit("登录态失效：请先运行 `python run_course.py login`")

    # SPA 需要等首屏渲染
    for _ in range(30):
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        anchors = page.evaluate(
            """() => [...document.querySelectorAll('a[href]')]
                 .map(a => ({href: a.getAttribute('href') || '', text: (a.innerText || '').trim()}))
                 .filter(x => /\\/course\\/\\d+/.test(x.href))"""
        )
        if anchors:
            break
        time.sleep(2)
    else:
        d = _dump(page, "user_index")
        raise SystemExit(f"未在首页发现任何课程链接。已落盘页面文本：{d}")

    seen: dict[str, dict] = {}
    for a in anchors:
        m = re.search(r"/course/(\d+)", a["href"])
        if not m:
            continue
        cid = m.group(1)
        title = a["text"] or ""
        if cid in seen and len(title) <= len(seen[cid]["title"]):
            continue
        seen[cid] = {
            "course_id": cid,
            "title": title,
            "url": f"{cfg.lms_base}/course/{cid}/learning-activity",
        }
    return list(seen.values())


def pick_course(courses: list[dict], keyword: str) -> dict:
    kw = (keyword or "").strip().lower()

    def score(c: dict) -> int:
        t = (c.get("title") or "").lower()
        if not kw:
            return 1
        if kw in t:
            return 3
        # 兼容「示例课程（示例学期）」这类写法：逐个词命中
        parts = [p for p in re.split(r"[\s（）()、]+", kw) if p]
        return sum(1 for p in parts if p in t)

    ranked = sorted(courses, key=score, reverse=True)
    if not ranked or score(ranked[0]) <= 0:
        listing = "\n".join(f"  [{c['course_id']}] {c['title']}" for c in courses)
        raise SystemExit(f"没找到匹配「{keyword}」的课程。当前可见课程：\n{listing}")
    return ranked[0]


def _api(page, path: str) -> dict | None:
    """用会话 cookie 调平台 JSON 接口（同源 fetch）。"""
    try:
        res = page.evaluate(
            """async (u) => {
                 const r = await fetch(u, {headers: {'Accept': 'application/json'}});
                 const t = await r.text();
                 return {status: r.status, text: t};
               }""", path)
    except Exception:
        return None
    if res.get("status") != 200:
        return None
    try:
        return json.loads(res["text"])
    except Exception:
        return None


def list_activities(session: Session, cfg: Config, course: dict) -> list[dict]:
    """活动列表走真实接口 /api/courses/<id>/activities（比 DOM 稳）。

    实测（2026-10-25，示例课程）：20 条活动 = 10 条 lecture_live（直播课）
    + 10 条 lesson（「观看录播教材」，data.allow_download=True, duration=6600s，
    download_setting=ALLOWALL）——**lesson 才是可下的双轨录播**。
    """
    page = session.page
    if not goto_checked(session, course["url"]):
        raise SystemExit("登录态失效：请先运行 `python run_course.py login`")
    time.sleep(4)

    data = _api(page, f"/api/courses/{course['course_id']}/activities")
    if not data:
        d = _dump(page, f"course_{course['course_id']}")
        raise SystemExit(f"活动接口无响应。已落盘页面证据：{d}")
    acts = data.get("activities") or []

    out: list[dict] = []
    for a in acts:
        typ = a.get("type") or ""
        d = a.get("data") or {}
        title = (a.get("title") or "").strip()
        date_m = re.search(r"(20\d{2}-\d{2}-\d{2})", title) or \
                 re.search(r"(20\d{2}-\d{2}-\d{2})", a.get("start_time") or "")
        tm = re.search(r"(\d{2}:\d{2}:\d{2}-\d{2}:\d{2}:\d{2})", title)
        out.append({
            "activity_id": str(a.get("id")),
            "type": typ,
            "title": title[:120],
            "date": date_m.group(1) if date_m else "",
            "time_text": tm.group(1) if tm else "",
            "duration_s": d.get("duration"),
            "allow_download": d.get("allow_download"),
            "download_setting": d.get("download_setting"),
            "external_live_id": d.get("external_live_id"),
            "lesson_resource_id": d.get("lesson_resource_id"),
            "url": f"{cfg.lms_base}/course/{course['course_id']}/learning-activity#/{a.get('id')}",
        })
    # 录播（lesson）排在前，按日期升序
    out.sort(key=lambda x: (x["type"] != "lesson", x["date"], x["activity_id"]))
    return out

def build_manifest(session: Session, cfg: Config, course_keyword: str) -> dict:
    courses = list_courses(session, cfg)
    course = pick_course(courses, course_keyword)
    acts = list_activities(session, cfg, course)
    manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "course": course,
        "n_activities": len(acts),
        "activities": acts,
        "all_courses_seen": courses,
    }
    out = STATE_DIR / "manifest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest

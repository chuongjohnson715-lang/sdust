# -*- coding: utf-8 -*-
"""端到端编排：登录 → 发现 → 下载 → 管线 → 语义层 → 五件套。

每一步都可单独运行，进度写在 .state/run_state.json，中断后重跑会跳过已完成项。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from . import semantic_auto
from .browser import ensure_login, open_browser
from .config import (DOWNLOAD_DIR, RAW_ROOT, REPO_ROOT, STATE_DIR, WORK_ROOT, Config,
                     ensure_dirs, load_config, redact, safe_name)
from .discover import build_manifest
from .fetch import fetch_activity, probe_movie

RUN_STATE = STATE_DIR / "run_state.json"


# ------------------------------------------------------------------ 状态


def load_state() -> dict:
    if RUN_STATE.exists():
        return json.loads(RUN_STATE.read_text(encoding="utf-8"))
    return {"activities": {}, "log": []}


def save_state(st: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    RUN_STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def note(st: dict, msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {redact(msg)}"
    st.setdefault("log", []).append(line)
    print(line, flush=True)


# ------------------------------------------------------------------ 步骤


def do_login(cfg: Config, headless: bool = False) -> bool:
    ensure_dirs()
    s = open_browser(cfg, headless=headless, downloads_dir=DOWNLOAD_DIR)
    try:
        return ensure_login(cfg, s)
    finally:
        s.close()


def do_discover(cfg: Config, headless: bool = False) -> dict:
    ensure_dirs()
    s = open_browser(cfg, headless=False, downloads_dir=DOWNLOAD_DIR)
    try:
        if not ensure_login(cfg, s):
            raise SystemExit("登录失败/超时，无法继续发现课程")
        man = build_manifest(s, cfg, cfg.target_course_keyword)
    finally:
        s.close()
    print(f"\n课程：{man['course']['title']}（id={man['course']['course_id']}）")
    print(f"活动数：{man['n_activities']}")
    for a in man["activities"]:
        print(f"  [{a['activity_id']}] {a.get('date') or '—'} {a.get('time_text') or ''} {a['title'][:40]}")
    print(f"\n清单已写入：{STATE_DIR / 'manifest.json'}")
    return man


def do_fetch(cfg: Config, only_ids: list[str] | None = None, headless: bool = False,
             include_live: bool = False) -> dict:
    ensure_dirs()
    man_path = STATE_DIR / "manifest.json"
    if not man_path.exists():
        raise SystemExit("缺少课程清单，请先运行 `discover`")
    man = json.loads(man_path.read_text(encoding="utf-8"))
    st = reconcile()

    s = open_browser(cfg, headless=headless, downloads_dir=DOWNLOAD_DIR)
    try:
        if not ensure_login(cfg, s):
            raise SystemExit("登录失败/超时，无法下载")
        for act in man["activities"]:
            aid = act["activity_id"]
            if only_ids and aid not in only_ids:
                continue
            # 直播课（lecture_live）没有点播播放器，默认跳过
            if act.get("type") == "lecture_live" and not include_live:
                note(st, f"跳过 {aid}（直播课，无点播播放器）")
                continue
            rec = st["activities"].setdefault(aid, {})
            if rec.get("fetch", {}).get("status") in ("OK", "SINGLE_TRACK"):
                note(st, f"跳过 {aid}（已下载）")
                continue
            time.sleep(15)  # 平台敏感：每节之间留间隔，避免被限流
            note(st, f"下载 {aid} {act.get('date') or ''} {act['title'][:40]}")
            res = fetch_activity(s, cfg, act)
            rec["meta"] = {"date": act.get("date"), "title": act.get("title"), "url": act["url"]}
            rec["fetch"] = {k: v for k, v in res.items() if k != "media"}
            rec["fetch"]["media"] = res.get("media", [])
            note(st, f"  -> {res['status']} {res.get('reason') or ''}")
            save_state(st)
    finally:
        s.close()
    save_state(st)
    return st


def reconcile(st: dict | None = None) -> dict:
    """按磁盘对账：raw/<date>_<aid>/ 下有两路 mp4 就认定该节已下载。

    为什么需要：批量跑时进程被杀或平台超时会让 run_state 与磁盘脱节
    （实测 4 节已下好、状态只记 2 节）。每次入口先对账，避免重复下载 4 GB。
    """
    st = st or load_state()
    if not RAW_ROOT.exists():
        return st
    fixed = 0
    for d in sorted(RAW_ROOT.iterdir()):
        if not d.is_dir():
            continue
        mp4s = sorted(p for p in d.rglob("*") if p.suffix.lower() in (".mp4", ".mkv", ".flv"))
        if len(mp4s) < 2:
            continue
        aid = d.name.rsplit("_", 1)[-1]
        rec = st["activities"].setdefault(aid, {})
        rec.setdefault("meta", {"date": d.name.split("_")[0], "title": "", "url": ""})
        cur = rec.get("fetch") or {}
        if cur.get("status") in ("OK", "SINGLE_TRACK"):
            continue
        rec["fetch"] = {**cur, "activity_id": aid, "status": "OK",
                        "media": [{"path": str(p)} for p in mp4s], "reconciled": True}
        fixed += 1
    if fixed:
        save_state(st)
        print(f"[reconcile] 按磁盘补记 {fixed} 节已下载", flush=True)
    return st


def _find_dual_input(act_dir: Path) -> Path | None:
    """在解压目录里找含 mp4 的目录（pipeline 的 --input-dir）。

    目录不存在（该节还没下载）时返回 None，由调用方跳过。
    """
    if not act_dir.exists():
        return None
    for d in [act_dir] + [p for p in act_dir.rglob("*") if p.is_dir()]:
        mp4s = [p for p in d.iterdir() if p.suffix.lower() in (".mp4", ".mkv", ".flv", ".avi", ".mov")]
        if len(mp4s) >= 2:
            return d
    for d in [act_dir] + [p for p in act_dir.rglob("*") if p.is_dir()]:
        mp4s = [p for p in d.iterdir() if p.suffix.lower() in (".mp4", ".mkv", ".flv", ".avi", ".mov")]
        if mp4s:
            return d
    return None


def do_pipeline(cfg: Config, task_root: Path, only_ids: list[str] | None = None) -> dict:
    st = reconcile()
    py = cfg.python_exe
    script = REPO_ROOT / "scripts" / "pipeline.py"
    for aid, rec in st["activities"].items():
        if only_ids and aid not in only_ids:
            continue
        if not rec.get("fetch"):
            note(st, f"跳过 {aid}（未下载）")
            continue
        act_dir = RAW_ROOT / safe_name(f"{rec['meta'].get('date') or ''}_{aid}")
        if not act_dir.exists():
            note(st, f"跳过 {aid}（raw 目录不存在，该节未下载成功）")
            continue
        inp = _find_dual_input(act_dir)
        if inp is None:
            note(st, f"跳过 {aid}（未找到视频文件）")
            continue
        # 时长：取最大一路，向上取整加 5 秒余量
        dur = max((probe_movie(p).get("duration_s") or 0) for p in inp.iterdir()
                  if p.suffix.lower() in (".mp4", ".mkv", ".flv", ".avi", ".mov"))
        win = f"0:{int(dur) + 5}" if dur else "0:6600"
        cmd = [py, str(script), "--input-dir", str(inp), "--window", win,
               "--work-root", str(WORK_ROOT / "data")]
        note(st, f"管线 {aid} window={win}")
        r = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        tail = (r.stdout or "").strip().splitlines()[-3:]
        for line in tail:
            note(st, "  " + line)
        rec["pipeline"] = {"returncode": r.returncode, "window": win, "tail": tail,
                           "stderr_tail": (r.stderr or "").strip().splitlines()[-3:]}
        save_state(st)
        if r.returncode != 0:
            note(st, f"  [WARN] 管线退出码 {r.returncode}（多为 stderr 噪音，需看 state.json）")
    return st


def _task_dir_for(rec: dict) -> Path | None:
    """从 pipeline 输出里解析任务目录（成果目录：...）。"""
    for line in rec.get("pipeline", {}).get("tail", []):
        if "成果目录" in line:
            p = line.split("：", 1)[-1].strip()
            return Path(p)
    return None


def do_semantic(cfg: Config, out_root: Path, course: str, only_ids: list[str] | None = None) -> dict:
    st = reconcile()
    py = cfg.python_exe
    sem_script = REPO_ROOT / "scripts" / "semantic_review.py"
    for aid, rec in st["activities"].items():
        if only_ids and aid not in only_ids:
            continue
        task = _task_dir_for(rec)
        if not task or not task.exists():
            note(st, f"跳过 {aid}（没有任务目录）")
            continue
        out = out_root / aid
        note(st, f"语义层 {aid} → {out}")
        try:
            semantic_auto.write(task, out, course=course, recorded_at=rec["meta"].get("date") or "")
        except Exception as e:  # noqa: BLE001
            note(st, f"  [FAIL] 规则语义层：{type(e).__name__}: {e}")
            continue
        r = subprocess.run([py, str(sem_script), "all", "--data", str(task), "--out", str(out)],
                           cwd=str(REPO_ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        chk = out / "quality_selfcheck.json"
        passed = None
        if chk.exists():
            d = json.loads(chk.read_text(encoding="utf-8"))
            passed = f"{d['passed']}/{d['total']}"
        note(st, f"  -> validate {passed}  rc={r.returncode}")
        rec["semantic"] = {"out": str(out), "validate": passed, "returncode": r.returncode}
        save_state(st)
    return st


def do_deliver(cfg: Config, study_root: Path, deliver_root: Path, course: str,
               template: Path, only_ids: list[str] | None = None) -> dict:
    st = load_state()
    py = cfg.python_exe
    gen = REPO_ROOT / "scripts" / "build_deliverables.py"
    for aid, rec in st["activities"].items():
        if only_ids and aid not in only_ids:
            continue
        task = _task_dir_for(rec)
        sem = Path(rec.get("semantic", {}).get("out", "")) if rec.get("semantic") else None
        if not task or not sem or not sem.exists():
            note(st, f"跳过 {aid}（缺任务目录或学习层）")
            continue
        out = deliver_root / safe_name(f"{rec['meta'].get('date') or ''}_{aid}")
        note(st, f"五件套 {aid} → {out}")
        r = subprocess.run([py, str(gen), "--data", str(task), "--study", str(sem),
                            "--out", str(out), "--course", course,
                            "--template", str(template), "--prefer-single"],
                           cwd=str(REPO_ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        ok = (out / "_自检.json").exists() and json.loads(
            (out / "_自检.json").read_text(encoding="utf-8")).get("ok")
        note(st, f"  -> 交付自检 ok={ok} rc={r.returncode}")
        rec["deliver"] = {"out": str(out), "ok": bool(ok), "returncode": r.returncode}
        save_state(st)
    return st


def cmd_all(cfg: Config, headless: bool, deliver_root: Path, template: Path) -> int:
    if not do_login(cfg, headless=False):
        return 3
    man = do_discover(cfg, headless=headless)
    do_fetch(cfg, headless=headless)
    do_pipeline(cfg, RAW_ROOT)
    do_semantic(cfg, STATE_DIR / "study", man["course"]["title"])
    do_deliver(cfg, STATE_DIR / "study", deliver_root, man["course"]["title"], template)
    st = load_state()
    done = sum(1 for r in st["activities"].values() if r.get("deliver", {}).get("ok"))
    print(f"\n完成：{done}/{len(st['activities'])} 节已产出五件套；交付目录 {deliver_root}")
    return 0 if done else 2


def main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="run_course.py", description="课程录播复盘：端到端自动化")
    ap.add_argument("command", choices=["login", "discover", "fetch", "pipeline", "semantic",
                                        "deliver", "all", "status"])
    ap.add_argument("--headless", action="store_true", help="无头模式（需已有有效会话）")
    ap.add_argument("--include-live", action="store_true", help="也尝试直播课活动（通常无播放器）")
    ap.add_argument("--only", default="", help="只处理指定活动 id（逗号分隔）")
    ap.add_argument("--course", default="", help="课程名（写入产物/语义层），默认取清单里的标题")
    ap.add_argument("--deliver-root", default=str(REPO_ROOT / "courses"),
                    help="五件套交付根目录（默认 ./courses）")
    ap.add_argument("--template", default=str(REPO_ROOT / "templates" / "课程复盘五件套"
                                              / "assets" / "全课程思维导图.html"))
    args = ap.parse_args(argv)

    # status 是只读的进度查询，不依赖账号配置，必须在 load_config 之前处理
    if args.command == "status":
        st = load_state()
        print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "meta"}
                          for k, v in st["activities"].items()}, ensure_ascii=False, indent=2))
        return 0

    cfg = load_config()
    only = [x.strip() for x in args.only.split(",") if x.strip()]
    course = args.course or cfg.target_course_keyword
    template = Path(args.template)

    if args.command == "login":
        ok = do_login(cfg, headless=args.headless)
        print("登录态可用" if ok else "登录未完成")
        return 0 if ok else 3
    if args.command == "discover":
        do_discover(cfg)
        return 0
    if args.command == "fetch":
        do_fetch(cfg, only_ids=only, headless=args.headless, include_live=args.include_live)
        return 0
    if args.command == "pipeline":
        do_pipeline(cfg, RAW_ROOT, only_ids=only)
        return 0
    if args.command == "semantic":
        man = json.loads((STATE_DIR / "manifest.json").read_text(encoding="utf-8")) if (STATE_DIR / "manifest.json").exists() else {"course": {"title": course}}
        do_semantic(cfg, STATE_DIR / "study", args.course or man["course"]["title"], only_ids=only)
        return 0
    if args.command == "deliver":
        man = json.loads((STATE_DIR / "manifest.json").read_text(encoding="utf-8")) if (STATE_DIR / "manifest.json").exists() else {"course": {"title": course}}
        do_deliver(cfg, STATE_DIR / "study", Path(args.deliver_root),
                   args.course or man["course"]["title"], template, only_ids=only)
        return 0
    if args.command == "all":
        return cmd_all(cfg, args.headless, Path(args.deliver_root), template)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

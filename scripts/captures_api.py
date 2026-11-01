# -*- coding: utf-8 -*-
"""省流量取配置：在 8002 播放器域内**阻断媒体**，纯 API 拿 lesson_videos[]。

为什么值得做：
  `fetch_tracks_v2.get_tracks()` 必须 goto 播放页并点击 video，播放器会自动
  加载预览流（实测每次 ~220MB）。46 节课 = 10GB 纯浪费。
  本模块改为：goto 播放页 → **route 阻断 media** → 手动 fetch embed-token + captures。
  视频数据一个字节都不下，只走三个 JSON 接口（~几十 KB）。

调用链（实测 2026-10-26）：
  1) 8085: /api/lessons/<aid>/player-url?from_page=course
         → {url: "http://lms.example.edu:8002/embed/lms/lesson-activity/<hash>/detail?token=<JWT>"}
  2) 8002: /api/v1/auth/embed-token?token=<JWT>      → access token
  3) 8002: /api/embed/lesson-activities/captures/<hash>?lesson_activity_id=<aid>
         → {data: {lesson_videos: [{label, download_url, size, ...}]}}
"""
from __future__ import annotations

import json
import re
import time

from coursereview.config import LMS_BASE

HASH_RE = re.compile(r"/lesson-activity/([0-9a-f]+)/detail")


def _block_media(route):
    """阻断视频数据请求，只放行 API / 文档 / 脚本 / 样式。

    实测要点：播放器拉视频分片走的是 XHR（`resource_type` 是 `xhr` 而非 `media`），
    只按 media 过滤拦不住——13 秒仍会下 270MB。所以必须**按域名阻断**：
    视频直链都在 `:8804` 存储域，配置 API 在 `:8002`，两者天然分离。
    """
    try:
        url = route.request.url or ""
        if ":8804" in url or route.request.resource_type == "media":
            route.abort()
        else:
            route.continue_()
    except Exception:
        try:
            route.continue_()
        except Exception:
            pass


def player_url(pg, aid: str, course_id: str, base: str | None = None) -> str:
    """8085 课程页上下文取播放器 URL。带重试（平台限流时返回 404）。

    会先确保页面在 8085 课程页：`get_tracks_api` 上一轮已跳到 8002，
    若不归位则相对路径 fetch 会打到 8002 而 404。
    """
    cur = pg.url or ""
    root = (base or LMS_BASE).rstrip("/")
    if not cur.startswith(root + "/course/"):
        try:
            pg.goto(f"{root}/course/{course_id}/learning-activity",
                    wait_until="domcontentloaded", timeout=60000)
            time.sleep(1.5)
        except Exception:
            pass
    last = ""
    for i in range(5):
        r = pg.evaluate("""async (aid) => {
            const resp = await fetch('/api/lessons/' + aid + '/player-url?from_page=course',
                                     {credentials: 'include'});
            return {s: resp.status, t: await resp.text()};
        }""", aid)
        last = r["t"]
        try:
            j = json.loads(last)
        except Exception:
            j = {}
        if r["s"] == 200 and isinstance(j, dict) and j.get("url"):
            return j["url"]
        time.sleep(5)
    raise RuntimeError(f"player-url 失败 {aid}: {last[:200]}")


def get_tracks_api(pg, aid: str, course_id: str, stats: dict | None = None,
                   base: str | None = None) -> dict:
    """纯 API 取 tracks（零视频流量）。返回 {label: video_dict}。

    stats 若传入 dict，会写入：
      stats['api_bytes']   —— 放行的响应 content-length 累加
      stats['blocked']     —— 被阻断的请求数
      stats['blocked_urls']—— 被阻断的前若干 URL（诊断用）
    """
    if stats is None:
        stats = {}
    stats.setdefault("api_bytes", 0)
    stats.setdefault("blocked", 0)
    stats.setdefault("blocked_urls", [])

    purl = player_url(pg, aid, course_id, base)
    m = HASH_RE.search(purl or "")
    if not m:
        raise RuntimeError(f"purl 里没有 hash: {purl[:120]}")
    hsh = m.group(1)
    jwt = purl.split("token=")[1].split("&")[0] if "token=" in purl else ""

    # 统计放行响应的实际字节（比 netstat 全系统统计精确）
    def on_resp(resp):
        try:
            cl = resp.headers.get("content-length")
            if cl and cl.isdigit():
                stats["api_bytes"] += int(cl)
        except Exception:
            pass

    pg.on("response", on_resp)

    def blocker(route):
        try:
            url = route.request.url or ""
            if ":8804" in url or route.request.resource_type == "media":
                stats["blocked"] += 1
                if len(stats["blocked_urls"]) < 5:
                    stats["blocked_urls"].append(url[:100])
                route.abort()
            else:
                route.continue_()
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass

    # 关键：必须进入 8002 域（embed-token / captures 都在 8002），且阻断 8804 视频域
    pg.route("**/*", blocker)
    try:
        pg.goto(purl, wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)

        # 1) 换 access token
        #    实测响应结构：{"data": "<JWT 字符串>", "error": {...}}
        #    注意 data 直接是字符串，不是对象——早期按 data.access_token 取会拿到 None
        tok = None
        r2 = pg.evaluate("""async (jwt) => {
            const r = await fetch('/api/v1/auth/embed-token?token=' + encodeURIComponent(jwt),
                                  {credentials: 'include'});
            return {s: r.status, t: await r.text()};
        }""", jwt)
        if r2["s"] == 200:
            try:
                j2 = json.loads(r2["t"])
                data = j2.get("data")
                if isinstance(data, str):
                    tok = data
                elif isinstance(data, dict):
                    tok = data.get("access_token") or data.get("token") or data.get("accessToken")
                if not tok:
                    tok = j2.get("access_token") or j2.get("token")
            except Exception:
                pass

        # 2) 取 captures（带 Bearer，若拿到 token）
        r3 = pg.evaluate("""async (args) => {
            const [hash, aid, tok] = args;
            const h = {'Content-Type': 'application/json'};
            if (tok) h['Authorization'] = 'Bearer ' + tok;
            const r = await fetch(`/api/embed/lesson-activities/captures/${hash}?lesson_activity_id=${aid}`,
                                  {credentials:'include', headers: h});
            return {s: r.status, t: await r.text()};
        }""", [hsh, aid, tok])
        if r3["s"] != 200:
            raise RuntimeError(f"captures HTTP {r3['s']}: {r3['t'][:200]}")
        d = json.loads(r3["t"])
        vids = (d.get("data") or {}).get("lesson_videos") or []
        if not vids:
            raise RuntimeError(f"响应无 lesson_videos: {str(d)[:200]}")
        return {v["label"]: v for v in vids}
    finally:
        try:
            pg.remove_listener("response", on_resp)
        except Exception:
            pass
        try:
            pg.unroute("**/*", blocker)
        except Exception:
            pass

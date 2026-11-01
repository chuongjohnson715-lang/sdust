# -*- coding: utf-8 -*-
"""浏览器网络栈并发下载器（绕 WAF 的 TLS 指纹拦截）。

为什么必须用它：
  平台 WAF 按 TLS/HTTP2 指纹识别客户端，ffmpeg / urllib / OpenCV 直连一律 403，
  只有真实浏览器（Playwright/Edge）能拿到 206。实测单连接 1.78MB/s，
  4.2GB/节要 40 分钟；本模块用 async + 多路 Range 并发提速。

关键实现约束（踩过的坑）：
  1. Playwright **sync API 不能多线程共享 context**（会 `Request context disposed`），
     必须用 async API + asyncio 并发。
  2. 必须 `channel="msedge"`（默认 chromium headless_shell 未安装）。
  3. profile 路径要用**绝对路径**，相对路径在 async 下解析基准可能不同。
  4. 一次性 gather 全部 chunk 会把整个文件读进内存 → 用**滑动窗口**分批下载并顺序落盘。

用法：
  from browser_download import BrowserDownloader
  with BrowserDownloader() as bd:
      bd.download(url, out_path, total_size)
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from playwright.async_api import async_playwright  # noqa: E402

from coursereview.config import PROFILE_DIR, REPO_ROOT  # noqa: E402

CHUNK_MB = 8          # 每个 Range 段大小
WORKERS = 8           # 并发数


def _abs_profile() -> Path:
    p = Path(PROFILE_DIR)
    return p if p.is_absolute() else (REPO_ROOT / p)


class BrowserDownloader:
    """同步门面 + 内部 asyncio 事件循环（方便在现有同步脚本里调用）。"""

    def __init__(self, workers: int = WORKERS, chunk_mb: int = CHUNK_MB):
        self.workers = workers
        self.chunk_mb = chunk_mb
        self._pw = None
        self._ctx = None
        self._page = None

    # ---------------- 生命周期 ----------------
    def __enter__(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._start())
        return self

    def __exit__(self, *exc):
        try:
            self._loop.run_until_complete(self._stop())
        finally:
            self._loop.close()
        return False

    async def _start(self):
        self._pw = await async_playwright().start()
        args = ["--disable-blink-features=AutomationControlled"]
        # 与 coursereview/browser.py 同一开关：系统代理挂掉时 Chromium 会
        # ERR_PROXY_CONNECTION_FAILED，而校内域名直连可达。
        if os.environ.get("COURSE_REVIEW_BYPASS_PROXY") == "1":
            args.append("--no-proxy-server")
        self._ctx = await self._pw.chromium.launch_persistent_context(
            str(_abs_profile()), headless=True, channel="msedge",
            args=args,
            accept_downloads=True,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()

    async def ensure_login(self, cfg, timeout_s: int = 120) -> bool:
        """async 版登录（复现 sync 版 ensure_login 的全自动流程）。

        为什么必须自己登录：async 启动的 Edge 实例与 sync 实例**不共享内存会话**，
        实测直接复用 profile 仍会被重定向到 CAS（Keycloak 服务端会话已过期）。
        """
        page = self._page
        await page.goto(cfg.user_index, wait_until="domcontentloaded", timeout=150000)
        await asyncio.sleep(2)
        if "cas/login" not in (page.url or ""):
            return True

        await page.goto(cfg.cas_login + "?service=" + cfg.portal_url,
                        wait_until="domcontentloaded", timeout=150000)
        await asyncio.sleep(1)
        try:
            await page.fill("#username", cfg.username, timeout=10000)
            try:
                await page.fill("#ppassword", cfg.password, timeout=8000)
            except Exception:
                await page.fill("input[name=password]", cfg.password, timeout=8000)
            await page.evaluate(
                """() => { const p = document.querySelector('#ppassword');
                           for (const ev of ['input','keyup','change']) {
                             p && p.dispatchEvent(new Event(ev, {bubbles: true}));
                           } }""")
            await asyncio.sleep(0.8)
            for sel in ("input[type=submit]", "button[type=submit]",
                        "#loginBtn", ".login-btn"):
                try:
                    await page.click(sel, timeout=4000)
                    break
                except Exception:
                    continue
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] 自动填表失败：{type(e).__name__}: {e}")

        for _ in range(max(10, timeout_s // 2)):
            await asyncio.sleep(2)
            if "cas/login" not in (page.url or ""):
                await asyncio.sleep(1)
                if "cas/login" not in (page.url or ""):
                    return True
        return False

    async def _stop(self):
        try:
            if self._ctx:
                await self._ctx.close()
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass

    # ---------------- 属性 ----------------
    @property
    def page(self):
        return self._page

    @property
    def ctx(self):
        return self._ctx

    def run(self, coro):
        """在内部事件循环上跑一个协程。"""
        return self._loop.run_until_complete(coro)

    # ---------------- 下载 ----------------
    async def _fetch_range(self, url: str, start: int, end: int, sem: asyncio.Semaphore,
                           retries: int = 3) -> tuple[int, bytes] | None:
        async with sem:
            for attempt in range(retries):
                try:
                    r = await self._ctx.request.get(
                        url, headers={"Range": f"bytes={start}-{end}"}, timeout=600000)
                    if r.status in (200, 206):
                        return start, await r.body()
                    if r.status == 416:
                        return start, b""
                except Exception:
                    pass
                await asyncio.sleep(1 + attempt * 2)
            return None

    async def download_async(self, url: str, out: Path, total_size: int,
                             log=print, progress_every_mb: int = 128) -> tuple[bool, str]:
        out.parent.mkdir(parents=True, exist_ok=True)
        chunk = self.chunk_mb * 1024 * 1024
        # 切成 Range 段
        ranges = []
        pos = 0
        while pos < total_size:
            end = min(pos + chunk, total_size) - 1
            ranges.append((pos, end))
            pos = end + 1

        sem = asyncio.Semaphore(self.workers)
        t0 = time.time()
        done_bytes = 0
        next_log = progress_every_mb * 1024 * 1024
        failed = 0

        # 滑动窗口：维持 workers 个在飞，完成一个就落盘一个（顺序写）
        pending: dict[int, asyncio.Task] = {}
        idx = 0
        with out.open("wb") as f:
            while idx < len(ranges) or pending:
                while len(pending) < self.workers and idx < len(ranges):
                    s, e = ranges[idx]
                    pending[idx] = asyncio.create_task(self._fetch_range(url, s, e, sem))
                    idx += 1
                # 等待最早完成的任务
                finished = await asyncio.wait(pending.values(),
                                              return_when=asyncio.FIRST_COMPLETED)
                for task in finished[0]:
                    # 找到对应 idx
                    k = next((i for i, t in pending.items() if t is task), None)
                    if k is None:
                        continue
                    pending.pop(k, None)
                    try:
                        res = task.result()
                    except Exception:
                        res = None
                    if res is None:
                        failed += 1
                        # 重试一次该段
                        s, e = ranges[k]
                        res = await self._fetch_range(url, s, e, sem, retries=5)
                        if res is None:
                            return False, f"段 {k} 下载失败（{failed} 次）"
                    data = res[1]
                    # 顺序写：需要按 k 递增落盘
                    # 为保证顺序，先把结果缓存，按序 flush
                    self._buf[k] = data
                    while self._flush_idx in self._buf:
                        d = self._buf.pop(self._flush_idx)
                        f.write(d)
                        done_bytes += len(d)
                        self._flush_idx += 1
                    if done_bytes >= next_log:
                        sp = done_bytes / (time.time() - t0) / 1e6
                        log(f"    {done_bytes/1e6:.0f}/{total_size/1e6:.0f}MB "
                            f"({sp:.1f}MB/s)")
                        next_log += progress_every_mb * 1024 * 1024

        dt = time.time() - t0
        sz = out.stat().st_size
        ok = abs(sz - total_size) < 1024 * 1024
        return ok, (f"{sz/1e6:.0f}MB / {dt:.0f}s = {sz/1e6/dt:.2f}MB/s"
                    + ("" if ok else f"（大小不符，期望 {total_size/1e6:.0f}MB）"))

    def download(self, url: str, out: Path, total_size: int, **kw) -> tuple[bool, str]:
        self._buf = {}
        self._flush_idx = 0
        return self._loop.run_until_complete(self.download_async(url, out, total_size, **kw))

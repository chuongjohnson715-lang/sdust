# -*- coding: utf-8 -*-
"""浏览器与登录态。

设计取舍（重要）：
统一身份认证（CAS）**服务端强制验证码**，本程序不做验证码识别/绕过——那是绕过安全控制。
因此采用「持久化浏览器配置 + 首次人工过一次验证码」：
  · 第一次运行：打开真实浏览器窗口，自动填好学号/密码，停在验证码处等你输入；
  · 之后：同一 profile 里 CAS/Keycloak cookie 仍然有效，后续运行**全程无需人工**；
  · 会话过期时自动重新走一次同样的流程（同一个函数）。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config, PROFILE_DIR, redact

CAS_MARKERS = ("cas/login", "cas.example.edu")


@dataclass
class Session:
    playwright: object
    context: object
    page: object

    def close(self) -> None:
        try:
            self.context.close()
        finally:
            try:
                self.playwright.stop()
            except Exception:
                pass


def _looks_cas(url: str) -> bool:
    return any(m in (url or "") for m in CAS_MARKERS)


def open_browser(cfg: Config, headless: bool = False, downloads_dir: Path | None = None) -> Session:
    """启动持久化 Chromium/Edge（优先使用本机 Edge，落盘位置可指定）。"""
    from playwright.sync_api import sync_playwright

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    launch_kwargs = dict(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        accept_downloads=True,
        args=["--disable-blink-features=AutomationControlled"],
        viewport={"width": 1440, "height": 900},
        locale="zh-CN",
    )
    if downloads_dir is not None:
        downloads_dir.mkdir(parents=True, exist_ok=True)
        launch_kwargs["downloads_path"] = str(downloads_dir)
    # 系统代理（如 127.0.0.1:10090 的本地客户端）挂掉时，Chromium 会直接
    # ERR_PROXY_CONNECTION_FAILED，而校内域名往往直连可达。用环境变量显式绕过，
    # 不改用户系统设置；需要代理时去掉该变量即可。
    if os.environ.get("COURSE_REVIEW_BYPASS_PROXY") == "1":
        launch_kwargs["args"].append("--no-proxy-server")

    context = None
    errors = []
    # 先用本机 Edge（平台对 Edge 兼容性最好），失败退回 Playwright 自带 Chromium
    for channel in ("msedge", None):
        try:
            kw = dict(launch_kwargs)
            if channel:
                kw["channel"] = channel
            context = pw.chromium.launch_persistent_context(**kw)
            break
        except Exception as e:  # noqa: BLE001
            errors.append(f"{channel or 'chromium'}: {type(e).__name__}: {e}")
    if context is None:
        pw.stop()
        raise RuntimeError("无法启动浏览器：\n  " + "\n  ".join(errors))

    page = context.pages[0] if context.pages else context.new_page()
    return Session(playwright=pw, context=context, page=page)


def ensure_login(cfg: Config, session: Session, timeout_s: int = 120) -> bool:
    """确保已登录平台。

    实测（2026-10-25，msedge 无头）：CAS 登录页的 authcode 字段存在于 HTML 但**不渲染**，
    直接填账号密码 + 点登录即可进入「在线教学平台」，无验证码。

    因此默认走**全自动登录**；仅当平台真的启用风控验证码（提交后仍停在 CAS 且
    authcode 变为可见）时，才回退为「人工输入一次」——绝不破解验证码。
    """
    page = session.page
    page.goto(cfg.user_index, wait_until="domcontentloaded", timeout=150000)
    time.sleep(2)
    if not _looks_cas(page.url):
        return True

    page.goto(cfg.cas_login + "?service=" + cfg.portal_url, wait_until="domcontentloaded", timeout=150000)
    time.sleep(1)

    def captcha_visible() -> bool:
        try:
            return bool(page.evaluate(
                """() => { const el = document.querySelector('#authcode, input[name=authcode]');
                           if (!el) return false;
                           const r = el.getBoundingClientRect();
                           const s = getComputedStyle(el);
                           return r.width > 2 && r.height > 2
                                  && s.visibility !== 'hidden' && s.display !== 'none'; }"""))
        except Exception:
            return False

    # ---------- 全自动登录 ----------
    try:
        page.fill("#username", cfg.username, timeout=10000)
        try:
            page.fill("#ppassword", cfg.password, timeout=8000)   # 可见框，页面 JS 会同步到 hidden
        except Exception:
            page.fill("input[name=password]", cfg.password, timeout=8000)
        # 触发 oninput/onkeyup，让页面的密码同步/校验逻辑跑起来
        page.evaluate(
            """() => { const p = document.querySelector('#ppassword');
                       for (const ev of ['input', 'keyup', 'change']) {
                         p && p.dispatchEvent(new Event(ev, {bubbles: true}));
                       } }""")
        time.sleep(0.8)
        for sel in ("input[type=submit]", "button[type=submit]", "#loginBtn", ".login-btn"):
            try:
                page.click(sel, timeout=4000)
                break
            except Exception:
                continue
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 自动填表失败：{type(e).__name__}: {e}")

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(2)
        if not _looks_cas(page.url):
            time.sleep(1)
            if not _looks_cas(page.url):
                print("登录成功（全自动），会话已保存在 .secrets/browser-profile。")
                return True
        if captcha_visible():
            break

    # ---------- 兜底：确实出现可见验证码时，人工过一次 ----------
    if captcha_visible():
        print("\n" + "=" * 66)
        print("平台本次要求验证码（风控触发）。程序不破解验证码，请人工输入一次：")
        print("=" * 66)
        print(f"在弹出的浏览器窗口输入验证码并登录；程序最多等待 {timeout_s} 秒。")
        while time.time() < deadline + 240:
            time.sleep(2)
            if not _looks_cas(page.url):
                print("登录成功，会话已保存，后续运行不再需要人工。")
                return True
    print("登录未完成。")
    return False

def goto_checked(session: Session, url: str, wait: str = "domcontentloaded", timeout: int = 60000) -> bool:
    """带登录态检查的跳转。"""
    page = session.page
    page.goto(url, wait_until=wait, timeout=timeout)
    time.sleep(2)
    if _looks_cas(page.url):
        print(f"[WARN] 会话失效，被重定向：{redact(page.url)[:120]}")
        return False
    return True

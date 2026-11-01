# -*- coding: utf-8 -*-
"""配置与敏感信息处理。

凭据只从 .secrets/config.json 读取（该目录在 .gitignore 中），
任何日志、异常、报告都必须先过 redact()。
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SECRETS = REPO_ROOT / ".secrets"
CONFIG_PATH = SECRETS / "config.json"
# 默认用持久化 profile；若该 profile 被其它 Edge 实例占用，可用
# COURSE_REVIEW_PROFILE 指向一个独立目录（不设则行为与原来完全一致）。
PROFILE_DIR = Path(os.environ.get("COURSE_REVIEW_PROFILE", str(SECRETS / "browser-profile")))
STATE_DIR = REPO_ROOT / ".state"

# 课程门户域（8085 课程页与相对路径 fetch 的基准）。默认值与播放器域同主机。
LMS_BASE = os.environ.get("LMS_BASE", "https://lms.example.edu:8085").rstrip("/")

# 播放器域（直链请求的 Referer / Origin）。默认值与课程门户同主机的 8002 端口；
# 换学校改这里或设环境变量，不要改脚本。
PLAYER_BASE = os.environ.get("COURSE_REVIEW_PLAYER_BASE",
                             "http://lms.example.edu:8002").rstrip("/")


# 下载与解压体积大（单节约 3.6 GB），默认放仓库内 _work/；可用环境变量覆盖
def _default_work_root() -> Path:
    env = os.environ.get("COURSE_REVIEW_WORK_ROOT")
    if env:
        return Path(env)
    return REPO_ROOT / "_work"


WORK_ROOT = _default_work_root()
DOWNLOAD_DIR = WORK_ROOT / "downloads"
RAW_ROOT = WORK_ROOT / "raw"

_PWD_HOLDERS: list[str] = []


def redact(text: str) -> str:
    """把已加载的密码、学号从任意文本中抹掉。"""
    if not text:
        return text
    out = str(text)
    for secret in _PWD_HOLDERS:
        if secret:
            out = out.replace(secret, "<redacted>")
    return out


@dataclass
class Config:
    username: str
    password: str = field(repr=False)
    lms_base: str = LMS_BASE
    portal_url: str = os.environ.get("LMS_PORTAL", "https://portal.example.edu/application-center")
    cas_login: str = os.environ.get("LMS_CAS", "https://cas.example.edu/cas/login")
    target_course_keyword: str = ""
    python_exe: str = os.environ.get("COURSE_REVIEW_PYTHON", sys.executable)

    @property
    def user_index(self) -> str:
        return f"{self.lms_base}/user/index"


def load_config() -> Config:
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"缺少凭据文件：{CONFIG_PATH}\n"
            f"请复制仓库根目录的 config.example.json 为 .secrets/config.json 并填写。"
        )
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    plat = raw.get("platform", {}) or {}
    cfg = Config(
        username=str(raw["username"]),
        password=str(raw["password"]),
        lms_base=plat.get("lms_base", Config.lms_base),
        portal_url=plat.get("portal_url", Config.portal_url),
        cas_login=plat.get("cas_login", Config.cas_login),
        target_course_keyword=raw.get("target_course_keyword", ""),
        python_exe=raw.get("python_exe") or Config.python_exe,
    )
    _PWD_HOLDERS.append(cfg.password)
    _PWD_HOLDERS.append(cfg.username)
    return cfg


def ensure_dirs() -> None:
    for d in (SECRETS, PROFILE_DIR, STATE_DIR, WORK_ROOT, DOWNLOAD_DIR, RAW_ROOT):
        d.mkdir(parents=True, exist_ok=True)


def safe_name(s: str, limit: int = 60) -> str:
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", (s or "").strip())
    return s[:limit] or "unnamed"

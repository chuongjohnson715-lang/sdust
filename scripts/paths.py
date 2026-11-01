# -*- coding: utf-8 -*-
"""外部程序与模型路径的唯一解析点。

原实现把 ffmpeg / ffprobe / SenseVoice 的绝对路径硬编码在 5 个脚本里，换机器就要改 5 处。
这里统一成一条优先级链：**环境变量 → PATH → 本机已知安装位置（兜底）**。

可覆盖的环境变量：

    COURSE_REVIEW_FFMPEG       ffmpeg.exe / ffmpeg 绝对路径
    COURSE_REVIEW_FFPROBE      ffprobe 绝对路径（缺省取 ffmpeg 同目录）
    COURSE_REVIEW_SENSEVOICE   SenseVoiceSmall 模型目录
    COURSE_REVIEW_INPUT        默认输入目录（默认素材所在课程目录）
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# 本机兜底位置：只在环境变量与 PATH 都失败时使用，保证零配置也能跑
# 兜底位置：环境变量与 PATH 都失败时才用，保证常见安装能零配置跑起来
_KNOWN_FFMPEG = (
    r"C:\ffmpeg\bin\ffmpeg.exe",
)
_KNOWN_SENSEVOICE = str(Path.home() / ".cache" / "modelscope" / "iic" / "SenseVoiceSmall")
_KNOWN_INPUT = str(REPO_ROOT / "input")


def _resolve_file(what: str, *candidates: str | None) -> str:
    for c in candidates:
        if not c:
            continue
        p = Path(c)
        if p.is_file():
            return str(p)
    raise FileNotFoundError(
        f"找不到 {what}。\n"
        f"请设置环境变量（例：$env:COURSE_REVIEW_FFMPEG='D:\\ffmpeg\\bin\\ffmpeg.exe'），"
        f"或把 ffmpeg 加入 PATH。"
    )


FFMPEG = _resolve_file(
    "ffmpeg",
    os.getenv("COURSE_REVIEW_FFMPEG"),
    shutil.which("ffmpeg"),
    *_KNOWN_FFMPEG,
)

FFPROBE = _resolve_file(
    "ffprobe",
    os.getenv("COURSE_REVIEW_FFPROBE"),
    shutil.which("ffprobe"),
    str(Path(FFMPEG).with_name("ffprobe.exe")),
    str(Path(FFMPEG).with_name("ffprobe")),
)

SENSEVOICE_DIR = os.getenv("COURSE_REVIEW_SENSEVOICE") or _KNOWN_SENSEVOICE
DEFAULT_INPUT_DIR = Path(os.getenv("COURSE_REVIEW_INPUT") or _KNOWN_INPUT)

__all__ = ["FFMPEG", "FFPROBE", "SENSEVOICE_DIR", "DEFAULT_INPUT_DIR"]

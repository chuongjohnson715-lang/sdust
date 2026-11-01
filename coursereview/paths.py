# -*- coding: utf-8 -*-
"""把 scripts/paths.py 的解析结果暴露给包内使用（避免两套 ffmpeg 定位逻辑）。"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from paths import DEFAULT_INPUT_DIR, FFMPEG, FFPROBE, SENSEVOICE_DIR  # noqa: E402

__all__ = ["FFMPEG", "FFPROBE", "SENSEVOICE_DIR", "DEFAULT_INPUT_DIR"]

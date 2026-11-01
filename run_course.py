# -*- coding: utf-8 -*-
"""课程录播复盘系统 · 唯一入口。

    python run_course.py login      # 首次：过一次验证码（之后自动复用会话）
    python run_course.py discover   # 列出目标课程的全部录播活动
    python run_course.py fetch      # 逐节下载双轨 mp4
    python run_course.py pipeline   # 12 阶段证据层
    python run_course.py semantic   # 规则法学习层 + 质量自检
    python run_course.py deliver    # 五件套交付物
    python run_course.py all        # 以上全跑
    python run_course.py status     # 看每节进度

解释器固定 python（只调用、不安装）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from coursereview.runner import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

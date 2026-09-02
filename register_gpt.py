# -*- coding: utf-8 -*-
"""ChatGPT 注册（指纹浏览器版）— 兼容入口。

实现已拆到 register/ 包：
  register/log.py, page.py, onboarding.py, flow.py, batch.py, sessions.py
"""

from __future__ import annotations

from register import (
    JobCancelled,
    force_close_all_sessions,
    register_one,
    run_register,
)

__all__ = [
    "JobCancelled",
    "force_close_all_sessions",
    "register_one",
    "run_register",
]


if __name__ == "__main__":
    from cli import main as _cli_main

    _cli_main()

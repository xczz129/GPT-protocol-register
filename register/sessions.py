# -*- coding: utf-8 -*-
"""Active browser session registry for force-stop."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from register.log import _log


class JobCancelled(Exception):
    """Raised when the web panel (or caller) requests an immediate stop."""


def _default_stop_check() -> bool:
    return False


# Active browser sessions for force-stop (bb, pid)
_ACTIVE_SESSIONS: list[tuple[Any, str]] = []
_ACTIVE_SESSIONS_LOCK = __import__("threading").Lock()


def _register_session(bb, pid: str) -> None:
    with _ACTIVE_SESSIONS_LOCK:
        _ACTIVE_SESSIONS.append((bb, pid))


def _unregister_session(bb, pid: str) -> None:
    with _ACTIVE_SESSIONS_LOCK:
        for i, (b, p) in enumerate(_ACTIVE_SESSIONS):
            if p == pid:
                _ACTIVE_SESSIONS.pop(i)
                break


async def force_close_all_sessions() -> int:
    """Immediately close all open fingerprint browsers (used by Stop)."""
    from common.browser import teardown

    with _ACTIVE_SESSIONS_LOCK:
        items = list(_ACTIVE_SESSIONS)
        _ACTIVE_SESSIONS.clear()
    closed = 0
    for bb, pid in items:
        try:
            await __import__("asyncio").wait_for(
                teardown(bb, pid, delete=True), timeout=8.0
            )
            closed += 1
            _log(f"[停止] 已强制关闭浏览器 {pid}")
        except Exception as e:
            try:
                bb.delete_browser(pid)
            except Exception:
                pass
            _log(f"[停止] 强制关闭 {pid} 失败: {str(e)[:80]}")
    return closed

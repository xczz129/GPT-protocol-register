# -*- coding: utf-8 -*-
"""Task-aware logging helpers (contextvars prefixes)."""

from __future__ import annotations

import contextvars
import logging
import sys
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PLATFORM = "chatgpt"

_TASK_IDX: contextvars.ContextVar[int] = contextvars.ContextVar(
    "gpt_reg_task_idx", default=0
)
_TASK_EMAIL: contextvars.ContextVar[str] = contextvars.ContextVar(
    "gpt_reg_task_email", default=""
)
_LOG_SINK: contextvars.ContextVar[Callable[[str], None] | None] = contextvars.ContextVar(
    "gpt_reg_log_sink", default=None
)

logger = logging.getLogger("gpt_register")


def _short_email(email: str, max_len: int = 36) -> str:
    """Compact email for log prefix; keep full address when short enough."""
    e = (email or "").strip()
    if not e:
        return ""
    if len(e) <= max_len:
        return e
    if "@" in e:
        local, _, domain = e.partition("@")
        keep_local = max(6, max_len - len(domain) - 4)
        if len(local) > keep_local:
            local = local[: keep_local - 1] + "…"
        return f"{local}@{domain}"
    return e[: max_len - 1] + "…"


def _task_prefix() -> str:
    idx = _TASK_IDX.get()
    email = _short_email(_TASK_EMAIL.get())
    if idx and email:
        return f"#{idx} {email}"
    if idx:
        return f"#{idx}"
    if email:
        return email
    return ""


def _log(msg: str) -> None:
    """Print with task prefix: `  #2 lists-tick-0n@icloud.com [2] 填写邮箱…`"""
    prefix = _task_prefix()
    text = msg if msg.startswith(" ") or msg.startswith("\n") else f"  {msg}"
    body = text.lstrip("\n")
    leading_nl = "\n" if text.startswith("\n") else ""
    body = body[2:] if body.startswith("  ") else body.lstrip()
    if prefix and body.startswith("#"):
        parts = body.split(None, 1)
        if parts and parts[0].startswith("#"):
            body = parts[1] if len(parts) > 1 else ""
    if prefix:
        line = (
            f"{leading_nl}  {prefix} {body}".rstrip()
            if body
            else f"{leading_nl}  {prefix}"
        )
    else:
        line = f"{leading_nl}  {body}" if body else leading_nl or ""
    print(line, flush=True)
    sink = _LOG_SINK.get()
    if callable(sink) and line.strip():
        try:
            sink(line.strip())
        except Exception:
            pass


def _otp_channel_label(provider: str | None) -> str:
    p = (provider or "").lower()
    if "fastmail" in p or "mailcode" in p:
        return "Fastmail API"
    if "icloud" in p:
        return "iCloud API"
    if "outlook" in p or "graph" in p or "token" in p:
        return "Outlook Graph"
    if "imap" in p:
        return "IMAP"
    return provider or "邮箱池"


# Backward-compat no-ops: we no longer monkey-patch builtins.print.
_ORIG_PRINT = None
_PRINT_WRAPPED = False


def _ensure_print_prefix() -> None:
    """Kept for API compatibility; print is no longer globally patched."""
    return

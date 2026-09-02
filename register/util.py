# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import contextvars
import os
import random
import secrets
import string
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import ROOT as CONFIG_ROOT
from common.account_store import upsert_account_record

PLATFORM = "chatgpt"
SIGNUP_URL = "https://chatgpt.com/auth/login"
KEY_COOKIES = [
    "__Secure-next-auth.session-token",
    "__Secure-next-auth.session-token.0",
]
REGISTER_TIMEOUT = 480
KEEP_ON_FAIL = False
ACCOUNTS_FILE = CONFIG_ROOT / "data" / "registered_accounts.json"

_cf_node_idx = [0]

_FIRST_NAMES = [
    "James", "Mary", "John", "Anna", "David", "Laura", "Mike", "Emma",
    "Chris", "Sara", "Paul", "Lucy", "Mark", "Nina", "Tom", "Kate",
    "Alex", "Ella", "Sam", "Lily", "Ben", "Zoe", "Leo", "Ruby",
]
_LAST_NAMES = [
    "Smith", "Jones", "Brown", "Davis", "Evans", "Clark", "Hall", "Lee",
    "Walker", "Young", "King", "Wright", "Green", "Baker", "Adams", "Carter",
    "Reed", "Cook", "Bell", "Ward", "Gray", "Hughes", "Price", "Wood",
]

_COOKIE_BTNS = [
    "すべて受け入れる", "必須項目以外を拒否する",
    "Accept all", "Reject all", "Reject non-essential", "Accept", "Got it",
    "全部接受", "接受所有", "拒绝所有", "拒绝非必要", "同意", "知道了",
]


def rand_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "Aa1!" + "".join(
        secrets.choice(alphabet) for _ in range(max(8, length - 4))
    )


def rand_name() -> tuple[str, str]:
    # Non-security UI profile filler; unpredictability is not required here.
    return random.choice(_FIRST_NAMES), random.choice(_LAST_NAMES)  # nosemgrep: gitlab.bandit.B311


def _append_account(record: dict[str, Any]) -> None:
    # Persist first, then probe the Plus/free-trial campaign asynchronously.
    # Registration completion is not held up by the direct accounts/check call.
    saved = upsert_account_record(record, ACCOUNTS_FILE)
    try:
        from common.trial_eligibility import schedule_trial_eligibility_check

        from common.account_store import account_is_deactivated

        if not account_is_deactivated(saved):
            schedule_trial_eligibility_check(saved, path=ACCOUNTS_FILE)
    except Exception:
        # Eligibility metadata is best-effort; the account itself is already
        # safely stored and the panel can enqueue a later check on refresh.
        pass

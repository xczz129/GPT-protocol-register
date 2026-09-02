# -*- coding: utf-8 -*-
"""Stateless signed web-session primitives.

This module deliberately contains no FastAPI or configuration dependencies so
session behavior can be tested independently from the web application.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import hmac
import time


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


PBKDF2_ITERATIONS = 260_000


def hash_password(password: str, *, salt: str | None = None, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        str(password or "").encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()
    return "pbkdf2_sha256$" + str(iterations) + "$" + salt + "$" + digest


def verify_password(password: str, stored: str) -> bool:
    raw = str(stored or "").strip()
    if not raw.startswith("pbkdf2_sha256$"):
        return False
    try:
        _, iter_text, salt, digest = raw.split("$", 3)
        iterations = int(iter_text)
        expected = hashlib.pbkdf2_hmac(
            "sha256",
            str(password or "").encode("utf-8"),
            salt.encode("utf-8"),
            iterations,
        ).hex()
        return hmac.compare_digest(expected, digest)
    except (ValueError, TypeError):
        return False


def sign_session(username: str, secret: str, *, max_age: int, now: int | None = None) -> str:
    expires_at = int(now if now is not None else time.time()) + max(60, int(max_age))
    payload = f"{username}|{expires_at}"
    signature = hmac.new(
        str(secret).encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{_b64url(payload.encode('utf-8'))}.{signature}"


def verify_session(
    token: str,
    secret: str,
    expected_username: str,
    *,
    now: int | None = None,
) -> str | None:
    raw = str(token or "").strip()
    if not raw or "." not in raw:
        return None
    body, signature = raw.rsplit(".", 1)
    try:
        payload = _b64url_decode(body).decode("utf-8")
        username, expires_at = payload.split("|", 1)
        if int(expires_at) < int(now if now is not None else time.time()):
            return None
    except (TypeError, ValueError, UnicodeError):
        return None
    expected = hmac.new(
        str(secret).encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    return username if hmac.compare_digest(username, str(expected_username)) else None


def sign_redeem_session(cdk_id: str, secret: str, *, max_age: int, now: int | None = None) -> str:
    expires_at = int(now if now is not None else time.time()) + max(60, int(max_age))
    payload = f"redeem|{str(cdk_id or '').strip()}|{expires_at}"
    signature = hmac.new(
        str(secret).encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{_b64url(payload.encode('utf-8'))}.{signature}"


def verify_redeem_session(
    token: str,
    secret: str,
    *,
    now: int | None = None,
) -> str | None:
    raw = str(token or "").strip()
    if not raw or "." not in raw:
        return None
    body, signature = raw.rsplit(".", 1)
    try:
        payload = _b64url_decode(body).decode("utf-8")
        scope, cdk_id, expires_at = payload.split("|", 2)
        if scope != "redeem" or int(expires_at) < int(now if now is not None else time.time()):
            return None
    except (TypeError, ValueError, UnicodeError):
        return None
    expected = hmac.new(
        str(secret).encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return cdk_id if hmac.compare_digest(signature, expected) else None

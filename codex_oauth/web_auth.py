"""Small in-process authentication layer for the local dashboard.

The dashboard is intentionally self-contained: sessions live in memory and
expire automatically, while the configured password is represented only by a
PBKDF2 digest.  No credential is sent to the browser or written to the page.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from typing import Any


WEB_LOGIN_USERNAME = "admin"
WEB_LOGIN_SALT = b"codex-quota-login-v1"
# PBKDF2-HMAC-SHA256 digest for the configured dashboard password.
WEB_LOGIN_PASSWORD_DIGEST = bytes.fromhex(
    "308a3c64a6807887dd208b66fb540fc660b8f88a4eadb58882f22d63fd09d950"
)
WEB_LOGIN_ITERATIONS = 200_000
WEB_SESSION_COOKIE = "codex_quota_session"
WEB_SESSION_TTL_SECONDS = 8 * 60 * 60


class WebAuth:
    """Thread-safe, expiring session store used by the HTTP dashboard."""

    def __init__(self, *, username: str = WEB_LOGIN_USERNAME, password_digest: bytes = WEB_LOGIN_PASSWORD_DIGEST):
        self.username = username
        self.password_digest = bytes(password_digest)
        self._sessions: dict[str, tuple[str, float]] = {}
        self._lock = threading.RLock()

    def verify_credentials(self, username: Any, password: Any) -> bool:
        supplied_user = str(username or "").strip()
        supplied_password = str(password or "")
        if not hmac.compare_digest(supplied_user, self.username):
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            supplied_password.encode("utf-8"),
            WEB_LOGIN_SALT,
            WEB_LOGIN_ITERATIONS,
        )
        return hmac.compare_digest(digest, self.password_digest)

    def login(self, username: Any, password: Any) -> str | None:
        if not self.verify_credentials(username, password):
            return None
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._purge_locked()
            self._sessions[token] = (self.username, time.time() + WEB_SESSION_TTL_SECONDS)
        return token

    def authenticated(self, token: Any) -> bool:
        value = str(token or "").strip()
        if not value:
            return False
        with self._lock:
            self._purge_locked()
            record = self._sessions.get(value)
            return bool(record and record[0] == self.username and record[1] > time.time())

    def logout(self, token: Any) -> None:
        value = str(token or "").strip()
        if value:
            with self._lock:
                self._sessions.pop(value, None)

    def _purge_locked(self) -> None:
        now = time.time()
        expired = [token for token, (_, deadline) in self._sessions.items() if deadline <= now]
        for token in expired:
            self._sessions.pop(token, None)

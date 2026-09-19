# -*- coding: utf-8 -*-
"""Durable per-account operation history.

Operation history is intentionally kept outside the account store.  Account
rows can be deleted or replaced while an audit trail should remain available.
SQLite WAL mode makes appends from registration/maintenance worker threads
cheap and safe, and the alias table keeps an email-only row linked after an
account id becomes known.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from config import ROOT
from common.account_store import account_delete_key
from common.diagnostic_codes import public_error_code


OPERATION_LOG_DB_FILE = ROOT / "data" / "account_operation_logs.sqlite3"
DEFAULT_DB_PATH = OPERATION_LOG_DB_FILE
TRACE_DIR = Path(os.environ.get("REGISTRATION_TRACE_DIR") or ROOT / "data" / "registration_logs")
_LOCK = threading.RLock()
# The production audit database is written from registration/maintenance
# worker threads. Keep one configured connection per thread so each event does
# not reopen SQLite and rerun the schema migration script. Temporary paths used
# by tests/tools intentionally keep the old open/close lifecycle.
_CONNECTION_CACHE = threading.local()

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS operation_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_key TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    account_id TEXT NOT NULL DEFAULT '',
    operation TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    step TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    created_epoch REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_operation_events_account_time
    ON operation_events(account_key, created_epoch DESC, event_id DESC);
CREATE INDEX IF NOT EXISTS idx_operation_events_email
    ON operation_events(email) WHERE email <> '';
CREATE INDEX IF NOT EXISTS idx_operation_events_account_id
    ON operation_events(account_id) WHERE account_id <> '';
CREATE TABLE IF NOT EXISTS operation_aliases (
    alias_key TEXT NOT NULL,
    account_key TEXT NOT NULL,
    PRIMARY KEY(alias_key, account_key)
);
CREATE INDEX IF NOT EXISTS idx_operation_aliases_account
    ON operation_aliases(account_key);
"""

_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "passphrase",
        "new_password",
        "old_password",
        "password_confirm",
        "confirm_password",
        "totp",
        "totp_secret",
        "mfa_secret",
        "secret",
        "access_token",
        "refresh_token",
        "id_token",
        "session_token",
        "authorization",
        "cookie",
        "cookies",
        "api_key",
        "admin_key",
        "client_secret",
        "license_key",
        "token",
        "code",
        "otp",
        "mfa_code",
        "verification_code",
        "csrf_token",
        "challenge_token",
    }
)
_SENSITIVE_QUERY_KEYS = _SENSITIVE_KEYS | {
    "token",
    "code",
    "otp",
    "mfa_code",
    "verification_code",
    "key",
    "state",
}
_COMPACT_SENSITIVE_KEYS = frozenset(item.replace("_", "") for item in _SENSITIVE_KEYS)
_COMPACT_SENSITIVE_QUERY_KEYS = frozenset(
    item.replace("_", "") for item in _SENSITIVE_QUERY_KEYS
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _norm_email(value: Any) -> str:
    return _text(value).lower()


def _normal_key(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    lowered = raw.lower()
    if lowered.startswith(("email:", "account_id:")):
        prefix, _, rest = lowered.partition(":")
        return f"{prefix}:{rest.strip()}" if rest.strip() else ""
    if "@" in lowered and " " not in lowered:
        return f"email:{lowered}"
    return lowered


def _identity(value: Any) -> tuple[str, str, str, set[str]]:
    """Return canonical key, email, account id, and identity aliases."""
    if isinstance(value, dict):
        record = value
        email = _norm_email(record.get("email"))
        account_id = _text(record.get("account_id")).lower()
        try:
            canonical = _normal_key(account_delete_key(record))
        except Exception:
            canonical = ""
        aliases = {item for item in (canonical, f"email:{email}" if email else "", f"account_id:{account_id}" if account_id else "") if item}
        if not canonical:
            canonical = next(iter(aliases), "")
        return canonical, email, account_id, aliases

    key = _normal_key(value)
    email = key.removeprefix("email:") if key.startswith("email:") else ""
    account_id = key.removeprefix("account_id:") if key.startswith("account_id:") else ""
    return key, email, account_id, {key} if key else set()


def _redact_text(value: Any, secrets: Iterable[Any] = ()) -> str:
    text = _text(value)
    if not text:
        return ""
    for secret in sorted(
        (_text(item) for item in secrets if _text(item)), key=len, reverse=True
    ):
        text = text.replace(secret, "***")
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1***", text)
    # Provider diagnostics use both snake_case and camelCase names.  Redact
    # quoted JSON values first, then the unquoted/header form.  The optional
    # separators deliberately cover ``accessToken``, ``newPassword``,
    # ``totpSecret`` and their snake_case equivalents without matching words
    # such as ``passwordless``.
    sensitive_name = (
        r"password|passwd|passphrase|new[_-]?password|old[_-]?password|"
        r"confirm[_-]?password|password[_-]?confirm|totp(?:[_-]?secret)?|"
        r"mfa[_-]?secret|secret|access[_-]?token|refresh[_-]?token|id[_-]?token|"
        r"session[_-]?token|authorization|cookie|api[_-]?key|admin[_-]?key|"
        r"client[_-]?secret|license[_-]?key|token|otp|mfa[_-]?code|"
        r"verification[_-]?code|csrf[_-]?token|challenge[_-]?token|code"
    )
    text = re.sub(
        rf"(?i)([\"'])({sensitive_name})\1\s*:\s*([\"'])(.*?)(\3)",
        lambda match: f"{match.group(1)}{match.group(2)}{match.group(1)}:"
        f"{match.group(3)}{match.group(4) if public_error_code(match.group(2), match.group(4)) else '***'}{match.group(5)}",
        text,
    )
    text = re.sub(
        rf"(?i)(?<![A-Za-z0-9])({sensitive_name})(?![A-Za-z0-9])"
        r"(\s*[:=]\s*)([\"'])(.*?)(\3)",
        lambda match: f"{match.group(1)}{match.group(2)}{match.group(3)}"
        f"{match.group(4) if public_error_code(match.group(1), match.group(4)) else '***'}{match.group(5)}",
        text,
    )
    text = re.sub(
        rf"(?i)(?<![A-Za-z0-9])({sensitive_name})(?![A-Za-z0-9])"
        r"(\s*[:=]\s*)([^\s,;}\]]+)",
        lambda match: f"{match.group(1)}{match.group(2)}"
        f"{match.group(3) if public_error_code(match.group(1), match.group(3)) else '***'}",
        text,
    )
    text = re.sub(
        r"(?i)(https?://)([^:/\s@]+):([^@/\s]+)@",
        r"\1***:***@",
        text,
    )
    # Keep URLs readable while removing credential-bearing query parameters.
    def _url(match: re.Match[str]) -> str:
        raw = match.group(0)
        try:
            parsed = urlsplit(raw)
            query = [
                (
                    key,
                    "***"
                    if (
                        key.lower() in _SENSITIVE_QUERY_KEYS
                        or re.sub(r"[^a-z0-9]+", "", key.lower())
                        in _COMPACT_SENSITIVE_QUERY_KEYS
                    )
                    else value,
                )
                for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            ]
            return urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
            )
        except Exception:
            return raw

    text = re.sub(r"https?://[^\s<>]+", _url, text)
    return text[:1200]


def _redact_value(value: Any, *, key: str = "", secrets: Iterable[Any] = ()) -> Any:
    normalized = re.sub(r"[^a-z0-9]+", "_", _text(key).lower()).strip("_")
    compact = normalized.replace("_", "")
    if public_error_code(normalized, value):
        return value
    # Provider payloads use both snake_case and camelCase (for example
    # ``newPassword``). Treat the compact form as sensitive too; short
    # counters such as ``promotion_count`` remain unaffected.
    if normalized in _SENSITIVE_KEYS or compact in _COMPACT_SENSITIVE_KEYS:
        return "***" if value not in (None, "", [], {}) else ""
    if isinstance(value, dict):
        return {
            str(name): _redact_value(item, key=str(name), secrets=secrets)
            for name, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_value(item, secrets=secrets) for item in value[:50]]
    if isinstance(value, str):
        return _redact_text(value, secrets)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _redact_text(value, secrets)


def _redact_event_for_read(event: dict[str, Any]) -> dict[str, Any]:
    """Re-sanitize an event loaded from an older or manually edited store.

    Events written by :func:`append_operation_event` are already sanitized,
    but the panel also reads historical SQLite/JSONL data created by older
    versions.  Applying the same recursive sanitizer at the read boundary
    prevents stale metadata or diagnostic text from bypassing the writer.
    """
    safe = dict(event)
    for field in ("message", "step", "error"):
        safe[field] = _redact_text(safe.get(field, ""))
    metadata = safe.get("metadata")
    safe["metadata"] = (
        _redact_value(metadata if isinstance(metadata, dict) else {})
        if metadata is not None
        else {}
    )
    return safe


def redact_operation_event(event: Any) -> dict[str, Any]:
    """Return a panel-safe copy of an operation event.

    This public wrapper is used at API composition boundaries as a final
    defense for events supplied by legacy stores or test/import adapters.
    """
    if not isinstance(event, dict):
        return {}
    return _redact_event_for_read(event)


def _connect(path: Path) -> sqlite3.Connection:
    """Open and initialize one audit connection.

    This low-level helper always returns an owned connection. Production
    callers use :func:`_acquire` below to reuse a thread-local connection.
    Keeping the helper independent makes temporary-path tests deterministic.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=30.0)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.executescript(_SCHEMA_SQL)
    # SQLite creates WAL/SHM sidecars lazily. Apply the same private mode when
    # they already exist so credentials never become readable through them.
    for suffix in ("-wal", "-shm"):
        try:
            os.chmod(str(path) + suffix, 0o600)
        except OSError:
            pass
    return connection


def _default_path(path: Path) -> bool:
    try:
        return Path(path).resolve() == Path(DEFAULT_DB_PATH).resolve()
    except OSError:
        return False


def _acquire(path: Path) -> tuple[sqlite3.Connection, bool]:
    """Return ``(connection, owned)`` for the current worker thread."""
    path = Path(path)
    if not _default_path(path):
        return _connect(path), True
    cached_path = getattr(_CONNECTION_CACHE, "path", None)
    cached = getattr(_CONNECTION_CACHE, "connection", None)
    if cached is not None and cached_path == str(path.resolve()):
        # 备份恢复会用 os.replace 换掉日志库文件;已缓存的连接仍指向
        # 旧 inode,写入会静默丢失。按 inode 校验,文件被换后自动重连。
        try:
            if os.stat(path).st_ino == getattr(_CONNECTION_CACHE, "ino", None):
                return cached, False
        except OSError:
            pass
        _close_cached_connection()
    if cached is not None:
        try:
            cached.close()
        except Exception:
            pass
    connection = _connect(path)
    _CONNECTION_CACHE.path = str(path.resolve())
    _CONNECTION_CACHE.connection = connection
    try:
        _CONNECTION_CACHE.ino = os.stat(path).st_ino
    except OSError:
        _CONNECTION_CACHE.ino = None
    return connection, False


def _release(connection: sqlite3.Connection, owned: bool) -> None:
    if owned:
        connection.close()


def _close_cached_connection() -> None:
    connection = getattr(_CONNECTION_CACHE, "connection", None)
    if connection is not None:
        try:
            connection.close()
        except Exception:
            pass
        _CONNECTION_CACHE.connection = None
        _CONNECTION_CACHE.path = None


atexit.register(_close_cached_connection)


def _resolve_account_keys(
    connection: sqlite3.Connection,
    key: str,
) -> set[str]:
    wanted = _normal_key(key)
    if not wanted:
        return set()
    found = {wanted}
    frontier = {wanted}
    # Alias expansion is intentionally bounded by the tiny identity graph.
    for _ in range(4):
        if not frontier:
            break
        placeholders = ",".join("?" for _ in frontier)
        rows = connection.execute(
            f"SELECT alias_key, account_key FROM operation_aliases "
            f"WHERE alias_key IN ({placeholders}) OR account_key IN ({placeholders})",
            [*frontier, *frontier],
        ).fetchall()
        additions = {str(value) for row in rows for value in row if str(value)} - found
        found.update(additions)
        frontier = additions
    return found


def append_operation_event(
    account: Any,
    operation: str,
    status: str,
    *,
    message: Any = "",
    step: Any = "",
    error: Any = "",
    metadata: dict[str, Any] | None = None,
    secrets: Iterable[Any] = (),
    aliases: Iterable[Any] = (),
    at: str | None = None,
    path: Path = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    """Append one sanitized event and return its API-safe representation."""
    canonical, email, account_id, identity_aliases = _identity(account)
    if not canonical:
        return None
    # A rebinding can change the canonical email while retaining the same
    # remote account. Callers may supply the previous identity so historical
    # events remain visible from the new account key. Normalize aliases through
    # the same identity helper used for the primary record.
    alias_set = set(identity_aliases)
    for alias in aliases or ():
        _alias_key, _alias_email, _alias_account_id, normalized = _identity(alias)
        alias_set.update(normalized)
    identity_aliases = alias_set
    operation_value = re.sub(r"[^a-z0-9_.-]+", "_", _text(operation).lower())[:64] or "other"
    status_value = re.sub(r"[^a-z0-9_.-]+", "_", _text(status).lower())[:32] or "info"
    created_at = _text(at) or _now()
    try:
        created_epoch = datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
    except Exception:
        created_epoch = time.time()
        created_at = _now()
    # Materialize once because callers may provide a generator of credentials;
    # each field must receive the same complete redaction set.
    secret_values = tuple(secrets or ())
    safe_message = _redact_text(message, secret_values)
    safe_step = _redact_text(step, secret_values)
    safe_error = _redact_text(error, secret_values)
    safe_metadata = _redact_value(metadata or {}, secrets=secret_values)
    with _LOCK:
        connection, owned = _acquire(Path(path))
        try:
            connection.execute("BEGIN IMMEDIATE")
            # If an email-only event predates account_id discovery, link its
            # old key and the new key into one alias component.
            linked: set[str] = set()
            for alias in identity_aliases:
                linked.update(_resolve_account_keys(connection, alias))
            linked.update(identity_aliases)
            for account_key in linked:
                for alias in identity_aliases | linked:
                    connection.execute(
                        "INSERT OR IGNORE INTO operation_aliases(alias_key, account_key) VALUES (?, ?)",
                        (alias, account_key),
                    )
            cursor = connection.execute(
                """
                INSERT INTO operation_events(
                    account_key, email, account_id, operation, status,
                    message, step, error, metadata, created_at, created_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    canonical,
                    email,
                    account_id,
                    operation_value,
                    status_value,
                    safe_message,
                    safe_step,
                    safe_error,
                    json.dumps(safe_metadata, ensure_ascii=False, separators=(",", ":")),
                    created_at,
                    created_epoch,
                ),
            )
            event_id = int(cursor.lastrowid or 0)
            connection.commit()
            return {
                "id": event_id,
                "event_id": event_id,
                "account_key": canonical,
                "email": email,
                "account_id": account_id,
                "operation": operation_value,
                "status": status_value,
                "message": safe_message,
                "step": safe_step,
                "error": safe_error,
                "metadata": safe_metadata,
                "created_at": created_at,
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            _release(connection, owned)


def record_operation_event(
    account: Any,
    operation: str,
    status: str,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """Best-effort append used by workers so logging never breaks an account op."""
    try:
        return append_operation_event(account, operation, status, **kwargs)
    except Exception:
        return None


def list_operation_events(
    account_key: str,
    *,
    limit: int = 200,
    newest_first: bool = True,
    path: Path = DEFAULT_DB_PATH,
) -> tuple[list[dict[str, Any]], int]:
    wanted = _normal_key(account_key)
    if not wanted:
        return [], 0
    cap = max(1, min(int(limit or 200), 1000))
    with _LOCK:
        connection, owned = _acquire(Path(path))
        try:
            keys = _resolve_account_keys(connection, wanted)
            if not keys:
                return [], 0
            placeholders = ",".join("?" for _ in keys)
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM operation_events WHERE account_key IN ({placeholders})",
                    tuple(keys),
                ).fetchone()[0]
                or 0
            )
            direction = "DESC" if newest_first else "ASC"
            rows = connection.execute(
                f"SELECT event_id, account_key, email, account_id, operation, status, "
                f"message, step, error, metadata, created_at FROM operation_events "
                f"WHERE account_key IN ({placeholders}) "
                f"ORDER BY created_epoch {direction}, event_id {direction} LIMIT ?",
                (*keys, cap),
            ).fetchall()
            items: list[dict[str, Any]] = []
            for row in rows:
                try:
                    metadata = json.loads(str(row[9] or "{}"))
                except Exception:
                    metadata = {}
                items.append(
                    _redact_event_for_read(
                        {
                            "id": int(row[0]),
                            "event_id": int(row[0]),
                            "account_key": str(row[1] or ""),
                            "email": str(row[2] or ""),
                            "account_id": str(row[3] or ""),
                            "operation": str(row[4] or ""),
                            "status": str(row[5] or ""),
                            "message": str(row[6] or ""),
                            "step": str(row[7] or ""),
                            "error": str(row[8] or ""),
                            "metadata": metadata if isinstance(metadata, dict) else {},
                            "created_at": str(row[10] or ""),
                        }
                    )
                )
            return items, total
        finally:
            _release(connection, owned)


def list_registration_trace_events(
    trace_file: Any,
    *,
    limit: int = 200,
    newest_first: bool = True,
) -> tuple[list[dict[str, Any]], int]:
    """Read the existing per-registration JSONL trace as panel-safe events."""
    raw_path = Path(_text(trace_file))
    if not raw_path:
        return [], 0
    if not raw_path.is_absolute():
        raw_path = ROOT / raw_path
    try:
        path = raw_path.resolve()
        root = TRACE_DIR.resolve()
        if root not in path.parents or not path.is_file():
            return [], 0
    except OSError:
        return [], 0
    cap = max(1, min(int(limit or 200), 1000))
    # A trace can contain thousands of diagnostic lines. Keep bounded memory
    # while preserving the newest records needed by the account dialog.
    lines: deque[str] = deque(maxlen=max(2000, cap * 4))
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            lines.extend(handle)
    except OSError:
        return [], 0
    events: list[dict[str, Any]] = []
    # Include the trace stem in the synthetic id.  Multiple historical traces
    # can contain the same timestamp, and Vue uses ``id`` as the row key.
    trace_id_prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", path.stem)[:80] or "trace"
    for raw in lines:
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        kind = _text(payload.get("type")).lower()
        status = _text(payload.get("status")).lower()
        operation = "registration"
        message = _text(payload.get("message"))
        step = _text(payload.get("step"))
        error = _text(payload.get("error"))
        if kind == "trace_started":
            status = status or "running"
            message = message or "注册任务开始"
        elif kind == "trace_finished":
            status = status or "completed"
            message = message or "注册任务结束"
        elif kind == "trial_eligibility":
            operation = "trial_eligibility"
            message = message or _text(payload.get("label")) or "资格检测"
            error = error or _text(payload.get("reason"))
        elif kind == "log":
            status = status or "info"
            message = message or _text(payload.get("message"))
        elif kind == "fingerprint":
            status = status or "info"
            message = "运行环境已记录"
        elif kind == "device_sync":
            status = status or "info"
            message = "设备会话已同步"
        elif kind != "event":
            status = status or "info"
        event = _redact_event_for_read({
            "id": f"trace:{trace_id_prefix}:{_text(payload.get('at'))}:{len(events)}",
            "event_id": f"trace:{trace_id_prefix}:{_text(payload.get('at'))}:{len(events)}",
            "account_key": "",
            "email": _text(payload.get("email")),
            "account_id": _text(payload.get("account_id")),
            "operation": operation,
            "status": status or "info",
            "message": _redact_text(message),
            "step": _redact_text(step),
            "error": _redact_text(error),
            "metadata": {},
            "created_at": _text(payload.get("at") or payload.get("started_at")),
            "source": "registration_trace",
        })
        if event["message"] or event["step"] or event["error"] or kind in {"trace_started", "trace_finished"}:
            events.append(event)
    events.sort(key=lambda item: str(item.get("created_at") or ""), reverse=newest_first)
    return events[:cap], len(events)


def delete_operation_events(*, path: Path = DEFAULT_DB_PATH) -> None:
    """Testing/maintenance helper; normal account deletion never calls this."""
    with _LOCK:
        connection, owned = _acquire(Path(path))
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM operation_events")
            connection.execute("DELETE FROM operation_aliases")
            connection.commit()
        finally:
            _release(connection, owned)


__all__ = [
    "DEFAULT_DB_PATH",
    "OPERATION_LOG_DB_FILE",
    "append_operation_event",
    "record_operation_event",
    "list_operation_events",
    "list_registration_trace_events",
    "redact_operation_event",
    "delete_operation_events",
]

"""Private, durable counters for paid mailbox registration attempts.

Call ``begin_attempt`` once an account branch actually starts, keep its returned
token across transport retries, and finish only after that branch's final
outcome. A child branch needs its own token (do not copy the parent's token).
OTP wait IDs identify logical requests: repeat callbacks reuse an ID, while a
new request uses a new one. No addresses, codes, credentials or errors are saved.

Writes are best effort and never raise into registration. This module keeps no
attempt cache in memory; SQLite uniqueness provides replay/process safety.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Mapping, MutableMapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from config import ROOT


DEFAULT_DB_PATH = ROOT / "data" / "mail_registration_metrics.sqlite3"
ATTEMPT_FIELD = "_registration_metric_attempt_id"
PROVIDER = "smsbower_mail"
PROVIDERS = ("smsbower_mail", "online_disposablemail")
OUTCOMES = ("success", "already_registered", "otp_timeout", "other_failed", "cancelled")
_DOMAINS = (
    "gmail.com", "icloud.com", "outlook.com", "hotmail.com",
    "mail.ru", "yahoo.com", "aol.com",
)
_ROLES = ("primary", "child")
_TOKEN_LOCK = threading.Lock()
_SCHEMA_LOCK = threading.Lock()
_WARNING_LOCK = threading.Lock()
_LAST_WARNING_AT = float("-inf")
_LOGGER = logging.getLogger(__name__)


def _path() -> Path:
    override = os.environ.get("MAIL_REGISTRATION_METRICS_DB", "").strip()
    return Path(override).expanduser() if override else Path(DEFAULT_DB_PATH)


def _safe_warning(error: Exception) -> None:
    """Bound warning volume and never log DB paths, input or exception text."""
    global _LAST_WARNING_AT
    now = time.monotonic()
    with _WARNING_LOCK:
        if now - _LAST_WARNING_AT < 60:
            return
        _LAST_WARNING_AT = now
    _LOGGER.warning("Mailbox registration metrics unavailable (%s)", type(error).__name__)


def _open_connection() -> sqlite3.Connection:
    target = _path()
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(target), timeout=2.0)
    try:
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        connection.execute("PRAGMA busy_timeout=2000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS mail_registration_attempts (
                attempt_key TEXT PRIMARY KEY CHECK(length(attempt_key) = 64),
                provider TEXT NOT NULL CHECK(provider IN ('smsbower_mail', 'online_disposablemail')),
                domain TEXT NOT NULL CHECK(domain IN ('gmail.com', 'icloud.com', 'outlook.com',
                    'hotmail.com', 'mail.ru', 'yahoo.com', 'aol.com', 'unknown')),
                role TEXT NOT NULL CHECK(role IN ('primary', 'child')),
                outcome TEXT CHECK(outcome IN ('success', 'already_registered',
                    'otp_timeout', 'other_failed', 'cancelled')),
                started_at REAL NOT NULL,
                finished_at REAL
            );
            CREATE INDEX IF NOT EXISTS mail_registration_group
                ON mail_registration_attempts(provider, domain, role);
            CREATE TABLE IF NOT EXISTS mail_registration_otp_waits (
                attempt_key TEXT NOT NULL REFERENCES mail_registration_attempts(attempt_key),
                wait_key TEXT NOT NULL CHECK(length(wait_key) = 64),
                elapsed_seconds REAL NOT NULL CHECK(elapsed_seconds >= 0),
                received INTEGER NOT NULL CHECK(received IN (0, 1)),
                recorded_at REAL NOT NULL,
                PRIMARY KEY (attempt_key, wait_key)
            );
            """
        )
        _migrate_legacy_schema(connection)
        return connection
    except Exception:
        connection.close()
        raise


def _migrate_legacy_schema(connection: sqlite3.Connection) -> None:
    """Rebuild the attempts table when it still carries the old single-provider
    CHECK constraints (SQLite cannot ALTER a CHECK away in place)."""
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='mail_registration_attempts'"
    ).fetchone()
    if not row or not row[0]:
        return
    table_sql = str(row[0])
    if "provider = 'smsbower_mail'" not in table_sql and "'unknown'" in table_sql:
        return
    connection.executescript(
        """
        PRAGMA foreign_keys=OFF;
        BEGIN;
        CREATE TABLE mail_registration_attempts_new (
            attempt_key TEXT PRIMARY KEY CHECK(length(attempt_key) = 64),
            provider TEXT NOT NULL CHECK(provider IN ('smsbower_mail', 'online_disposablemail')),
            domain TEXT NOT NULL CHECK(domain IN ('gmail.com', 'icloud.com', 'outlook.com',
                'hotmail.com', 'mail.ru', 'yahoo.com', 'aol.com', 'unknown')),
            role TEXT NOT NULL CHECK(role IN ('primary', 'child')),
            outcome TEXT CHECK(outcome IN ('success', 'already_registered',
                'otp_timeout', 'other_failed', 'cancelled')),
            started_at REAL NOT NULL,
            finished_at REAL
        );
        INSERT INTO mail_registration_attempts_new
            SELECT attempt_key, provider, domain, role, outcome, started_at, finished_at
            FROM mail_registration_attempts;
        DROP TABLE mail_registration_attempts;
        ALTER TABLE mail_registration_attempts_new RENAME TO mail_registration_attempts;
        CREATE INDEX IF NOT EXISTS mail_registration_group
            ON mail_registration_attempts(provider, domain, role);
        COMMIT;
        PRAGMA foreign_keys=ON;
        """
    )


@contextmanager
def _connection() -> Iterator[sqlite3.Connection]:
    # journal_mode/schema initialization can race on the very first writes,
    # before SQLite has established WAL. Serialize initialization locally and
    # retry its short SQLITE_BUSY window across processes; normal transactions
    # still use independent connections and SQLite's busy timeout.
    with _SCHEMA_LOCK:
        for attempt in range(3):
            try:
                connection = _open_connection()
                break
            except sqlite3.OperationalError as error:
                if attempt == 2 or not any(part in str(error).lower() for part in ("locked", "busy")):
                    raise
                time.sleep(0.02)
    try:
        yield connection
    finally:
        connection.close()


def _key(token: Any, *, namespace: str = "attempt") -> str:
    if not isinstance(token, str) or not token.strip() or len(token) > 512:
        raise ValueError("Invalid metric identity")
    return hashlib.sha256(f"mail-registration:v1:{namespace}:{token}".encode()).hexdigest()


def _domain(mailbox: Mapping[str, Any]) -> str:
    raw = str(mailbox.get("domain") or "").strip().lower()
    if not raw:
        raw = str(mailbox.get("email") or mailbox.get("address") or "").rsplit("@", 1)[-1].strip().lower()
    if raw == "googlemail.com":
        raw = "gmail.com"
    return raw if raw in _DOMAINS else "unknown"


def begin_attempt(
    mailbox: Mapping[str, Any], *, attempt_id: str | None = None, role: str | None = None
) -> str | None:
    """Start/replay one SMSBower account branch, returning its stable token.

    A mutable mailbox receives ``ATTEMPT_FIELD`` before the write, so even a
    failed write or concurrent retry reuses the same identity. Immutable callers
    must retain the returned token or supply an explicit stable ``attempt_id``.
    Already terminal attempts are never reopened by a replay.
    """
    try:
        provider_name = str(mailbox.get("provider") or mailbox.get("type") or "").strip().lower()
        if provider_name not in PROVIDERS:
            return None
        account_role = role or str(mailbox.get("_smsbower_pair_role") or "primary")
        if account_role not in _ROLES:
            raise ValueError("Invalid registration role")
        domain = _domain(mailbox)
        with _TOKEN_LOCK:
            token = attempt_id if attempt_id is not None else mailbox.get(ATTEMPT_FIELD)
            if token is None:
                token = uuid.uuid4().hex
            attempt_key = _key(token)
            if isinstance(mailbox, MutableMapping):
                mailbox[ATTEMPT_FIELD] = token
        with _connection() as connection, connection:
            connection.execute(
                "INSERT OR IGNORE INTO mail_registration_attempts "
                "(attempt_key, provider, domain, role, started_at) VALUES (?, ?, ?, ?, ?)",
                (attempt_key, provider_name, domain, account_role, time.time()),
            )
            existing = connection.execute(
                "SELECT provider, domain, role FROM mail_registration_attempts WHERE attempt_key = ?",
                (attempt_key,),
            ).fetchone()
            if existing != (provider_name, domain, account_role):
                raise ValueError("Metric identity belongs to another registration branch")
        return token
    except Exception as error:
        _safe_warning(error)
        return None


def finish_attempt(attempt_id: str | None, outcome: str) -> bool:
    """Set a final outcome once. Replaying it succeeds without adding counts."""
    if attempt_id is None:
        return False
    try:
        if outcome not in OUTCOMES:
            raise ValueError("Invalid registration outcome")
        attempt_key = _key(attempt_id)
        with _connection() as connection, connection:
            connection.execute(
                "UPDATE mail_registration_attempts SET outcome = ?, finished_at = ? "
                "WHERE attempt_key = ? AND outcome IS NULL",
                (outcome, time.time(), attempt_key),
            )
            row = connection.execute(
                "SELECT outcome FROM mail_registration_attempts WHERE attempt_key = ?",
                (attempt_key,),
            ).fetchone()
            return row is not None and row[0] == outcome
    except Exception as error:
        _safe_warning(error)
        return False


def record_otp_wait(
    attempt_id: str | None, wait_id: str, elapsed_seconds: float, *, received: bool
) -> bool:
    """Record one finished code wait, deduplicated within its account branch.

    ``elapsed_seconds`` must be a monotonic duration supplied by the caller;
    it may start at the send boundary, before polling starts. Timeouts count
    separately and never enter received-code average/minimum/maximum values.
    Delayed callbacks may arrive after a terminal outcome and are still kept.
    """
    if attempt_id is None:
        return False
    try:
        attempt_key = _key(attempt_id)
        wait_key = _key(wait_id, namespace=f"wait:{attempt_key}")
        elapsed = float(elapsed_seconds)
        if not math.isfinite(elapsed) or elapsed < 0 or not isinstance(received, bool):
            raise ValueError("Invalid OTP timing sample")
        with _connection() as connection, connection:
            connection.execute(
                "INSERT OR IGNORE INTO mail_registration_otp_waits "
                "(attempt_key, wait_key, elapsed_seconds, received, recorded_at) "
                "SELECT ?, ?, ?, ?, ? WHERE EXISTS "
                "(SELECT 1 FROM mail_registration_attempts WHERE attempt_key = ?)",
                (attempt_key, wait_key, elapsed, int(received), time.time(), attempt_key),
            )
            row = connection.execute(
                "SELECT received FROM mail_registration_otp_waits "
                "WHERE attempt_key = ? AND wait_key = ?",
                (attempt_key, wait_key),
            ).fetchone()
            return row is not None and row[0] == int(received)
    except Exception as error:
        _safe_warning(error)
        return False


def _bucket() -> dict[str, Any]:
    return {
        "totals": {name: 0 for name in ("attempted", *OUTCOMES, "in_progress")},
        "rates": {},
        "otp_wait": {
            "received_count": 0, "timeout_count": 0, "average_seconds": None,
            "min_seconds": None, "max_seconds": None,
        },
    }


def _finish_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    denominator = bucket["totals"]["attempted"]
    bucket["rates"] = {
        outcome: {
            "numerator": bucket["totals"][outcome],
            "denominator": denominator,
            "value": bucket["totals"][outcome] / denominator if denominator else None,
        }
        for outcome in OUTCOMES
    }
    wait = bucket["otp_wait"]
    elapsed_sum = wait.pop("_elapsed_sum", 0.0)
    if wait["received_count"]:
        wait["average_seconds"] = elapsed_sum / wait["received_count"]
    return bucket


def _add_bucket(target: dict[str, Any], source: dict[str, Any]) -> None:
    for name, value in source["totals"].items():
        target["totals"][name] += value
    wait, incoming = target["otp_wait"], source["otp_wait"]
    for name in ("received_count", "timeout_count"):
        wait[name] += incoming[name]
    wait["_elapsed_sum"] = wait.get("_elapsed_sum", 0.0) + incoming.get("_elapsed_sum", 0.0)
    for name, operation in (("min_seconds", min), ("max_seconds", max)):
        value = incoming[name]
        if value is not None:
            wait[name] = value if wait[name] is None else operation(wait[name], value)


def get_summary(provider: str = PROVIDER) -> dict[str, Any]:
    """Return a bounded aggregate snapshot. All outcome rates use attempts.

    In-progress branches remain visible across restarts until their owner
    resumes/cancels them; a metrics reader never guesses an account outcome.
    An unavailable database returns ``ok=False`` so the UI does not claim zero
    failures/successes as an observed result.
    """
    result = {
        "ok": True, "provider": provider, **_bucket(), "by_domain": [],
        "by_role": [], "groups": [], "updated_at": None,
    }
    try:
        if provider not in PROVIDERS:
            raise ValueError("Unsupported metrics provider")
        with _connection() as connection, connection:
            # Both aggregations see one transaction snapshot under concurrent
            # completion/OTP callbacks; a sample cannot outrun its attempt row.
            connection.execute("BEGIN")
            attempt_rows = connection.execute(
                "SELECT domain, role, outcome, COUNT(*), MAX(COALESCE(finished_at, started_at)) "
                "FROM mail_registration_attempts WHERE provider = ? GROUP BY domain, role, outcome",
                (provider,),
            ).fetchall()
            wait_rows = connection.execute(
                "SELECT a.domain, a.role, SUM(w.received), SUM(1 - w.received), "
                "SUM(CASE WHEN w.received = 1 THEN w.elapsed_seconds ELSE 0 END), "
                "MIN(CASE WHEN w.received = 1 THEN w.elapsed_seconds END), "
                "MAX(CASE WHEN w.received = 1 THEN w.elapsed_seconds END), MAX(w.recorded_at) "
                "FROM mail_registration_otp_waits w "
                "JOIN mail_registration_attempts a ON a.attempt_key = w.attempt_key "
                "WHERE a.provider = ? GROUP BY a.domain, a.role",
                (provider,),
            ).fetchall()
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        last_update = 0.0
        for domain, role, outcome, count, updated_at in attempt_rows:
            group = groups.setdefault((domain, role), _bucket())
            group["totals"]["attempted"] += count
            group["totals"][outcome or "in_progress"] += count
            last_update = max(last_update, updated_at)
        for domain, role, received_count, timeout_count, elapsed_sum, shortest, longest, updated_at in wait_rows:
            groups[(domain, role)]["otp_wait"].update({
                "received_count": received_count, "timeout_count": timeout_count,
                "_elapsed_sum": elapsed_sum, "min_seconds": shortest, "max_seconds": longest,
            })
            last_update = max(last_update, updated_at)
        domains = {name: _bucket() for name in _DOMAINS}
        roles = {name: _bucket() for name in _ROLES}
        for (domain, role), group in sorted(groups.items()):
            _add_bucket(result, group)
            _add_bucket(domains.setdefault(domain, _bucket()), group)
            _add_bucket(roles[role], group)
            result["groups"].append({"provider": provider, "domain": domain, "role": role, **_finish_bucket(group)})
        result["by_domain"] = [{"domain": name, **_finish_bucket(value)} for name, value in domains.items()]
        result["by_role"] = [{"role": name, **_finish_bucket(value)} for name, value in roles.items()]
        if last_update:
            result["updated_at"] = datetime.fromtimestamp(last_update, timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception as error:
        _safe_warning(error)
        result.update({"ok": False, "error": "metrics_unavailable"})
    return _finish_bucket(result)

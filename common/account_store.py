# -*- coding: utf-8 -*-
"""Account result store helpers.

Production data is stored in SQLite so concurrent registration tasks update a
single account row instead of rewriting the complete JSON export.  Explicit
non-production paths retain the JSON backend used by imports and tests.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterable

try:  # Linux in production; fallback keeps the module importable elsewhere.
    import fcntl  # type: ignore
except Exception:  # pragma: no cover - non-posix fallback
    fcntl = None  # type: ignore[assignment]

from config import ROOT

ACCOUNTS_FILE = ROOT / "data" / "registered_accounts.json"
ACCOUNTS_DB_FILE = ROOT / "data" / "registered_accounts.sqlite3"

_ACCOUNT_STORE_LOCK = RLock()
_ACCOUNT_SNAPSHOT_LOCK = RLock()
_ACCOUNT_SNAPSHOT_CACHE: dict[Path, tuple[tuple[int, int, int], list[dict[str, Any]]]] = {}
# Health summaries are requested by the status endpoint and operations view at
# the same time.  Keep the derived counters keyed by the SQLite store revision
# so repeated reads avoid scanning every account's public JSON payload.  The
# value is copied at both boundaries because callers may augment the response.
_ACCOUNT_HEALTH_SUMMARY_CACHE_LOCK = RLock()
_ACCOUNT_HEALTH_SUMMARY_CACHE: dict[tuple[str, int], dict[str, int]] = {}
_ACCOUNT_HEALTH_SUMMARY_CACHE_MAX_ENTRIES = 16
# Full-snapshot readers (health checks, chatgpt2api sync, import jobs) decode
# every payload row, which dominates panel memory on large stores.  Cache the
# decoded list keyed by the SQLite revision — the same cheap invalidation
# signal the health summary uses — so repeat reads between writes reuse one
# decode instead of rebuilding a full snapshot per request.  A decoded
# snapshot of a large store costs hundreds of MB, so keep exactly one live
# entry per store and drop the stale one before decoding a replacement.
_ACCOUNT_ROWS_CACHE_LOCK = RLock()
_ACCOUNT_ROWS_CACHE: dict[tuple[str, int], list[dict[str, Any]]] = {}
_ACCOUNT_ROWS_CACHE_MAX_ENTRIES = 1


def invalidate_store_caches(path: str | Path = "") -> None:
    """Drop in-memory account caches (decoded rows + health summary).

    Needed after an out-of-band store replacement such as a backup restore:
    the restored SQLite file can carry the same revision as the pre-restore
    snapshot, which would keep serving the stale decoded rows.
    """
    store_key = ""
    if str(path or "").strip():
        try:
            store_key = str(Path(path).resolve())
        except Exception:
            store_key = ""
    with _ACCOUNT_ROWS_CACHE_LOCK:
        for key in [
            k for k in list(_ACCOUNT_ROWS_CACHE)
            if not store_key or k[0] == store_key
        ]:
            _ACCOUNT_ROWS_CACHE.pop(key, None)
    with _ACCOUNT_HEALTH_SUMMARY_CACHE_LOCK:
        for key in [
            k for k in list(_ACCOUNT_HEALTH_SUMMARY_CACHE)
            if not store_key or k[0] == store_key
        ]:
            _ACCOUNT_HEALTH_SUMMARY_CACHE.pop(key, None)
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_FIELD_ORDER = (
    "email",
    "password",
    "totp_secret",
    "mfa_enabled",
    "mfa_factor_type",
    "mfa_factor_id",
    "access_token",
    "session_token",
    "account_id",
    "plan_type",
    "expires_at",
    "proxy",
    "registration_method",
    "registration_state",
    "browser_type",
    "group_id",
    "source_type",
        "registered_at",
        "last_invalid_at",
        "updated_at",
    "session",
)
_CLEARABLE_FIELDS = frozenset(
    {
        "health_error",
        "health_probe_error",
        "last_token_refresh_error",
        "last_reauth_error",
        "last_reauth_failed_at",
        "c2a_upload_error",
        "reauth_blocked_reason",
        "security_completion_error",
        "security_completion_failed_at",
    }
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _s(value: Any = "") -> str:
    return str(value if value is not None else "").strip()


def _norm_email(value: Any = "") -> str:
    email = _s(value).lower()
    return email if _EMAIL_RE.match(email) else ""


def _truthy(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


_DEACTIVATED_BOOLEAN_FIELDS = frozenset(
    {
        "is_deactivated",
        "is_suspended",
        "is_banned",
        "is_deleted",
        "is_disabled",
        "is_terminated",
        "is_access_restricted",
        "account_deactivated",
        "account_suspended",
        "account_banned",
        "account_deleted",
        "account_disabled",
    }
)
_DEACTIVATED_STATUS_FIELDS = frozenset(
    {
        "status",
        "state",
        "account_status",
        "account_state",
        "user_status",
        "deactivation_status",
        "suspension_status",
    }
)
_DEACTIVATED_STATUS_VALUES = frozenset(
    {
        "banned",
        "ban",
        "blocked",
        "deactivated",
        "deleted",
        "disabled",
        "suspended",
        "terminated",
        "account_banned",
        "account_blocked",
        "account_deactivated",
        "account_deleted",
        "account_disabled",
        "account_suspended",
        "account_terminated",
        "user_banned",
        "user_blocked",
        "user_deactivated",
        "user_deleted",
        "user_suspended",
        "user_terminated",
    }
)
_DEACTIVATED_TEXT_MARKERS = (
    "account_deactivated",
    "account_suspended",
    "account_banned",
    "account_deleted",
    "account_disabled",
    "account_terminated",
    "user_deactivated",
    "user_suspended",
    "user_banned",
    "user_deleted",
    "account has been deleted",
    "account has been deactivated",
    "account has been suspended",
    "account has been banned",
    "account is suspended",
    "account is banned",
    "account is disabled",
    "account is blocked",
    "account was terminated",
    "账号已封号",
    "账号封禁",
    "账号已停用",
    "账号停用",
    "账号已删除",
    "账户已封禁",
    "账户已停用",
    "账户已删除",
)


def _status_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _s(value).lower()).strip("_")


def _flag_enabled(value: Any) -> bool:
    """Parse API boolean fields without treating the string ``false`` as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return _status_token(value) in {"1", "true", "yes", "on", "enabled"}


def account_deactivation_reason(record: Any) -> str:
    """Return a stable reason when a record explicitly describes a dead account.

    The account API has used more than one field name over time.  This helper
    intentionally accepts only explicit terminal markers; token expiry,
    transient HTTP errors, and generic ``restricted`` responses remain
    recoverable health states.
    """
    if not isinstance(record, dict):
        return ""

    raw_status = _s(record.get("status")).lower()
    if raw_status in {"已封号", "账号已封号", "账号已停用", "账户已封禁", "账户已停用"}:
        return f"status={raw_status}"

    blocked = _status_token(record.get("reauth_blocked_reason"))
    if blocked == "account_deactivated":
        return "reauth_blocked_reason=account_deactivated"

    for raw_key, value in record.items():
        key = _status_token(raw_key)
        if key in _DEACTIVATED_BOOLEAN_FIELDS and _flag_enabled(value):
            return f"{key}=true"
        if key in _DEACTIVATED_STATUS_FIELDS:
            token = _status_token(value)
            if token in _DEACTIVATED_STATUS_VALUES:
                return f"{key}={token}"

    for raw_key in (
        "status",
        "state",
        "account_status",
        "account_state",
        "user_status",
        "deactivation_status",
        "suspension_status",
        "reauth_blocked_reason",
        "health_error",
        "last_reauth_error",
        "error",
        "message",
        "code",
        "reason",
    ):
        text = _s(record.get(raw_key)).lower()
        if text and any(marker in text for marker in _DEACTIVATED_TEXT_MARKERS):
            return f"{raw_key}={text[:160]}"
    return ""


def account_is_deactivated(record: Any) -> bool:
    return bool(account_deactivation_reason(record))


def _epoch(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value / 1000 if value > 1e11 else value)
    text = _s(value)
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _jwt_auth_claims(token: Any) -> dict[str, Any]:
    """Decode the OpenAI auth claim section from a JWT (no signature check)."""
    text = _s(token)
    parts = text.split(".")
    if len(parts) < 2:
        return {}
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception:
        return {}
    if not isinstance(claims, dict):
        return {}
    auth = claims.get("https://api.openai.com/auth")
    return auth if isinstance(auth, dict) else {}


def _normalize_plan_type(value: Any) -> str:
    """Map raw ChatGPT plan slugs to display-friendly plan names.

    Workspace sessions report slugs like ``self_serve_business_prolite``
    (ChatGPT Team) instead of the short names used for personal plans.
    """
    plan = _s(value or "free").strip().lower() or "free"
    if "prolite" in plan or "team" in plan:
        return "team"
    if "enterprise" in plan:
        return "enterprise"
    if "business" in plan:
        return "team"
    return plan


def account_stable_user_id(record: dict[str, Any]) -> str:
    """Return the context-independent ChatGPT user id for one record.

    The NextAuth ``account_id`` flips between a member id and the shared
    workspace id depending on which workspace the session last selected,
    so it cannot identify a person. ``chatgpt_user_id`` from the access
    token is stable across personal/workspace contexts.
    """
    if not isinstance(record, dict):
        return ""
    direct = _s(record.get("user_id"))
    if direct:
        return direct.lower()
    auth = _jwt_auth_claims(record.get("access_token"))
    return _s(auth.get("chatgpt_user_id") or auth.get("user_id")).lower()


def _ensure_stable_user_id(record: dict[str, Any]) -> dict[str, Any]:
    """Backfill the ``user_id`` field from the access token when missing."""
    auth = _jwt_auth_claims(record.get("access_token"))
    uid = _s(auth.get("chatgpt_user_id") or auth.get("user_id"))
    direct = _s(record.get("user_id"))
    if direct:
        if uid and uid.lower() != direct.lower():
            raise ValueError("account_identity_conflict: 用户身份与访问凭证不一致")
        return record
    if uid:
        record["user_id"] = uid
    return record


def _record_identity(record: dict[str, Any]) -> tuple[str, str]:
    """Return a stable de-dup key: user id wins, then account_id, then email.

    ``account_id`` is context-dependent for workspace accounts (personal id
    on first login, shared workspace id after the session picks a team
    workspace), so the JWT user id is the only collision-safe identity for
    team members sharing one workspace.
    """
    user_id = account_stable_user_id(record)
    if user_id:
        return ("user", user_id)
    account_id = _s(record.get("account_id"))
    if account_id:
        return ("account_id", account_id.lower())
    email = _norm_email(record.get("email"))
    if email:
        return ("email", email)
    access = _s(record.get("access_token"))
    if access:
        return ("access_token", access[:64])
    return ("anonymous", _s(record.get("registered_at")) or _utc_now_iso())


def _sqlite_path(path: Path) -> Path | None:
    """Return the production DB path; custom paths intentionally stay JSON."""
    try:
        if Path(path).resolve() == Path(ACCOUNTS_FILE).resolve():
            return Path(ACCOUNTS_DB_FILE)
    except OSError:
        pass
    return None


def _ordered_record(record: dict[str, Any]) -> dict[str, Any]:
    record = dict(record)
    record.pop("plan_type_override", None)
    ordered: dict[str, Any] = {}
    for key in _FIELD_ORDER:
        if key in record:
            ordered[key] = record[key]
    for key in sorted(k for k in record if k not in ordered):
        ordered[key] = record[key]
    return ordered


def _merge_record(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Merge two records for the same account without dropping useful fields.

    New non-empty values replace old values. Empty new values never erase old
    data. ``registered_at`` is treated as the latest successful registration
    timestamp; ``updated_at`` records this upsert operation.
    """
    merged = dict(old)
    for key, value in new.items():
        if key in _CLEARABLE_FIELDS or _truthy(value) or not _truthy(merged.get(key)):
            merged[key] = value

    old_ts = _epoch(old.get("registered_at"))
    new_ts = _epoch(new.get("registered_at"))
    if old_ts and new_ts:
        merged["registered_at"] = old.get("registered_at") if old_ts >= new_ts else new.get("registered_at")
    elif old.get("registered_at") and not new.get("registered_at"):
        merged["registered_at"] = old.get("registered_at")
    else:
        merged["registered_at"] = new.get("registered_at") or old.get("registered_at") or _utc_now_iso()

    merged["updated_at"] = _utc_now_iso()
    return _ordered_record(merged)


@contextmanager
def _file_lock(path: Path, *, timeout: float = 5.0):
    """Exclusive flock with timeout so a stuck writer cannot freeze the panel."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _ACCOUNT_STORE_LOCK:
        lock_path = path.with_suffix(path.suffix + ".lock")
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            if fcntl is not None:
                deadline = time.monotonic() + max(0.2, float(timeout))
                while True:
                    try:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(f"account store lock timeout: {path}")
                        time.sleep(0.05)
            try:
                yield
            finally:
                if fcntl is not None:
                    try:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    except Exception:
                        pass



def _backup_corrupt(path: Path) -> None:
    if not path.is_file():
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    bad_path = path.with_name(f"{path.stem}.bad-{stamp}{path.suffix}")
    try:
        path.replace(bad_path)
    except Exception:
        pass


def _read_unlocked(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _backup_corrupt(path)
        return []
    if not isinstance(data, list):
        _backup_corrupt(path)
        return []
    return [_ordered_record(item) for item in data if isinstance(item, dict)]


def _snapshot_stamp(path: Path) -> tuple[int, int, int]:
    try:
        stat = path.stat()
        return stat.st_ino, stat.st_mtime_ns, stat.st_size
    except OSError:
        return 0, 0, 0


def _cached_snapshot(path: Path) -> list[dict[str, Any]] | None:
    key = path.absolute()
    stamp = _snapshot_stamp(path)
    with _ACCOUNT_SNAPSHOT_LOCK:
        cached = _ACCOUNT_SNAPSHOT_CACHE.get(key)
        if cached is None or cached[0] != stamp:
            return None
        return [dict(item) for item in cached[1]]


def _cache_snapshot(
    path: Path,
    records: Iterable[dict[str, Any]],
    *,
    stamp: tuple[int, int, int] | None = None,
) -> None:
    prepared = [dict(item) for item in records if isinstance(item, dict)]
    with _ACCOUNT_SNAPSHOT_LOCK:
        _ACCOUNT_SNAPSHOT_CACHE[path.absolute()] = (stamp or _snapshot_stamp(path), prepared)


def _canonical_snapshot(path: Path) -> list[dict[str, Any]]:
    cached = _cached_snapshot(path)
    return cached if cached is not None else normalize_accounts(_read_unlocked(path))


def _write_unlocked(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    prepared = list(records)
    payload = json.dumps(prepared, indent=2, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        Path(tmp_name).replace(path)
        _cache_snapshot(path, prepared)
    finally:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except Exception:
            pass


def _account_match_keys(record: dict[str, Any]) -> set[tuple[str, str]]:
    keys = {_record_identity(record)}
    email = _norm_email(record.get("email"))
    account_id = _s(record.get("account_id")).lower()
    if email:
        keys.add(("email", email))
    if account_id:
        keys.add(("account_id", account_id))
    return keys


def _safe_account_matches(
    candidates: Iterable[dict[str, Any]], record: dict[str, Any],
) -> list[dict[str, Any]]:
    """Resolve one person; a shared workspace is only a weak identity hint."""
    rows = list(candidates)
    user = account_stable_user_id(record)
    email = _norm_email(record.get("email"))
    account_id = _s(record.get("account_id")).lower()
    same_email = [row for row in rows if email and _norm_email(row.get("email")) == email]
    same_account = [row for row in rows if account_id and _s(row.get("account_id")).lower() == account_id]
    email_users = {account_stable_user_id(row) for row in same_email} - {""}
    if len(email_users) > 1 or (user and email_users - {user}):
        raise ValueError("account_identity_conflict: 同一邮箱对应不同用户，未合并账号")
    foreign_members = [
        row for row in same_account
        if (account_stable_user_id(row) and account_stable_user_id(row) != user)
        or (_norm_email(row.get("email")) and _norm_email(row.get("email")) != email)
    ]
    account_only = [
        row for row in same_account
        if not account_stable_user_id(row) and not _norm_email(row.get("email"))
    ] if not foreign_members else []
    if user:
        matches = [row for row in rows if account_stable_user_id(row) == user or row in same_email]
        # An account-only legacy fragment can complete a known user record.
        # A different named member of the workspace must remain separate.
        matches.extend(row for row in account_only if row not in matches)
        return matches
    if same_email:
        matches = list(same_email)
        matches.extend(row for row in account_only if row not in matches)
        return matches
    if same_account:
        if len(same_account) > 1 or (
            email and _norm_email(same_account[0].get("email"))
            and email != _norm_email(same_account[0].get("email"))
        ):
            raise ValueError("account_identity_ambiguous: 工作区属于多个或不同成员，请提供用户身份或原邮箱")
        return same_account
    identity = _record_identity(record)
    return [row for row in rows if _record_identity(row) == identity]


def normalize_accounts(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """De-duplicate and sort records newest first.

    Records may arrive partially populated: one write may only know email, a
    later token refresh may only know account_id, and a third write may contain
    both.  When that bridge record appears we must merge the two existing
    canonical groups instead of only linking future records.
    """
    groups: dict[int, dict[str, Any]] = {}
    aliases: dict[tuple[str, str], set[int]] = {}

    for sequence, raw in enumerate(records):
        if not isinstance(raw, dict):
            continue
        rec = _ensure_stable_user_id(_ordered_record(raw))
        candidate_ids: set[int] = set()
        for key in _account_match_keys(rec):
            candidate_ids.update(aliases.get(key, ()))
        candidates = {number: groups[number] for number in sorted(candidate_ids)}
        matches = _safe_account_matches(candidates.values(), rec)
        matched_objects = {id(row) for row in matches}
        matched_ids = [number for number, row in candidates.items() if id(row) in matched_objects]
        base = None
        for number in matched_ids:
            previous = groups.pop(number)
            for key in _account_match_keys(previous):
                aliases[key].discard(number)
            base = _merge_record(previous, base) if base is not None else previous
        merged = _merge_record(base, rec) if base is not None else rec
        groups[sequence] = merged
        for key in _account_match_keys(merged):
            aliases.setdefault(key, set()).add(sequence)

    return sorted(
        (_ordered_record(item) for item in groups.values()),
        key=lambda item: (_epoch(item.get("registered_at")), _norm_email(item.get("email"))),
        reverse=True,
    )


def _connect_sqlite(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=30.0)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            canonical_key TEXT PRIMARY KEY,
            email TEXT NOT NULL DEFAULT '',
            account_id TEXT NOT NULL DEFAULT '',
            group_id TEXT NOT NULL DEFAULT '',
            health_status TEXT NOT NULL DEFAULT '',
            registered_epoch REAL NOT NULL DEFAULT 0,
            updated_epoch REAL NOT NULL DEFAULT 0,
            payload TEXT NOT NULL,
            public_payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_accounts_email
            ON accounts(email) WHERE email <> '';
        CREATE INDEX IF NOT EXISTS idx_accounts_account_id
            ON accounts(account_id) WHERE account_id <> '';
        CREATE INDEX IF NOT EXISTS idx_accounts_group
            ON accounts(group_id);
        CREATE INDEX IF NOT EXISTS idx_accounts_registered
            ON accounts(registered_epoch DESC, email DESC);
        -- Include the deterministic tie-breaker used by panel pagination so
        -- SQLite can satisfy the complete ORDER BY from the index.
        CREATE INDEX IF NOT EXISTS idx_accounts_registered_order
            ON accounts(registered_epoch DESC, email DESC, canonical_key ASC);
        CREATE INDEX IF NOT EXISTS idx_accounts_health
            ON accounts(health_status);
        CREATE TABLE IF NOT EXISTS account_meta (
            name TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    return connection


def _meta_value(connection: sqlite3.Connection, name: str) -> str:
    row = connection.execute(
        "SELECT value FROM account_meta WHERE name = ?", (name,)
    ).fetchone()
    return str(row[0]) if row else ""


def _bump_revision(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO account_meta(name, value) VALUES ('revision', '1')
        ON CONFLICT(name) DO UPDATE SET value = CAST(value AS INTEGER) + 1
        """
    )


def _decode_payload(value: Any) -> dict[str, Any] | None:
    try:
        record = json.loads(str(value))
    except Exception:
        return None
    return _ordered_record(record) if isinstance(record, dict) else None


def _store_sqlite_record(connection: sqlite3.Connection, record: dict[str, Any]) -> dict[str, Any]:
    prepared = _ordered_record(record)
    public = account_public_view(prepared)
    connection.execute(
        """
        INSERT INTO accounts(
            canonical_key, email, account_id, group_id, health_status,
            registered_epoch, updated_epoch, payload, public_payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(canonical_key) DO UPDATE SET
            email=excluded.email,
            account_id=excluded.account_id,
            group_id=excluded.group_id,
            health_status=excluded.health_status,
            registered_epoch=excluded.registered_epoch,
            updated_epoch=excluded.updated_epoch,
            payload=excluded.payload,
            public_payload=excluded.public_payload
        """,
        (
            account_delete_key(prepared),
            _norm_email(prepared.get("email")),
            _s(prepared.get("account_id")).lower(),
            _s(prepared.get("group_id")),
            _s(public.get("health_status")).lower(),
            _epoch(prepared.get("registered_at")),
            _epoch(prepared.get("updated_at")) or time.time(),
            json.dumps(prepared, ensure_ascii=False, separators=(",", ":")),
            json.dumps(public, ensure_ascii=False, separators=(",", ":")),
        ),
    )
    return prepared


def _update_sqlite_group_records(
    connection: sqlite3.Connection,
    target_group_id: str,
    where_sql: str,
    params: Iterable[Any],
) -> int:
    """Update indexed and JSON group fields together in one SQL statement."""
    target = _s(target_group_id)
    now_iso = _utc_now_iso()
    cursor = connection.execute(
        f"""
        UPDATE accounts
           SET group_id = ?,
               updated_epoch = ?,
               payload = json_set(
                   payload,
                   '$.group_id', ?,
                   '$.updated_at', ?
               ),
               public_payload = json_set(
                   public_payload,
                   '$.group_id', ?,
                   '$.updated_at', ?
               )
         WHERE {where_sql}
        """,
        (
            target,
            time.time(),
            target,
            now_iso,
            target,
            now_iso,
            *params,
        ),
    )
    return max(0, int(cursor.rowcount or 0))


def _replace_sqlite_records(
    connection: sqlite3.Connection,
    records: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized = normalize_accounts(records)
    connection.execute("DELETE FROM accounts")
    for record in normalized:
        _store_sqlite_record(connection, record)
    return normalized


def _ensure_sqlite(path: Path) -> tuple[Path, sqlite3.Connection] | None:
    db_path = _sqlite_path(path)
    if db_path is None:
        return None
    connection = _connect_sqlite(db_path)
    if _meta_value(connection, "json_migrated") == "1":
        return db_path, connection

    # The JSON lock also coordinates first migration with an older running
    # process that may still be writing the legacy file before restart.
    with _file_lock(Path(path), timeout=120.0):
        try:
            connection.execute("BEGIN IMMEDIATE")
            if _meta_value(connection, "json_migrated") != "1":
                _replace_sqlite_records(connection, _read_unlocked(Path(path)))
                connection.execute(
                    "INSERT INTO account_meta(name, value) VALUES ('json_migrated', '1') "
                    "ON CONFLICT(name) DO UPDATE SET value='1'"
                )
                connection.execute(
                    "INSERT INTO account_meta(name, value) VALUES ('migrated_at', ?) "
                    "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                    (_utc_now_iso(),),
                )
                _bump_revision(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            connection.close()
            raise
    return db_path, connection


def _sqlite_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT payload FROM accounts "
        "ORDER BY registered_epoch DESC, email DESC, canonical_key"
    ).fetchall()
    return [record for row in rows if (record := _decode_payload(row[0])) is not None]


def _matching_sqlite_rows(
    connection: sqlite3.Connection,
    record: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    # The email/account_id indexes are partial (empty identities are omitted).
    # Include their predicates in the query; otherwise SQLite cannot prove
    # that a partial index is usable and falls back to a full table scan.
    clauses = ["canonical_key = ?"]
    params: list[Any] = [account_delete_key(record)]
    email = _norm_email(record.get("email"))
    account_id = _s(record.get("account_id")).lower()
    if email:
        clauses.append("(email <> '' AND email = ?)")
        params.append(email)
    if account_id:
        clauses.append("(account_id <> '' AND account_id = ?)")
        params.append(account_id)
    rows = connection.execute(
        f"SELECT canonical_key, payload FROM accounts WHERE {' OR '.join(clauses)}",
        params,
    ).fetchall()
    result: list[tuple[str, dict[str, Any]]] = []
    for key, payload in rows:
        decoded = _decode_payload(payload)
        if decoded is not None:
            result.append((str(key), decoded))
    matches = _safe_account_matches((decoded for _, decoded in result), record)
    selected = {id(row) for row in matches}
    return [(key, decoded) for key, decoded in result if id(decoded) in selected]


def _upsert_sqlite_record(
    connection: sqlite3.Connection,
    prepared: dict[str, Any],
) -> dict[str, Any]:
    matches = _matching_sqlite_rows(connection, prepared)
    normalized = normalize_accounts([*(item for _, item in matches), prepared])
    if matches:
        connection.executemany(
            "DELETE FROM accounts WHERE canonical_key = ?",
            ((key,) for key, _ in matches),
        )
    for record in normalized:
        _store_sqlite_record(connection, record)

    email = _norm_email(prepared.get("email"))
    account_id = _s(prepared.get("account_id")).lower()
    for record in normalized:
        if account_id and _s(record.get("account_id")).lower() == account_id:
            return record
        if email and _norm_email(record.get("email")) == email:
            return record
        if _record_identity(record) == _record_identity(prepared):
            return record
    return normalized[0] if normalized else prepared


def load_accounts(path: Path = ACCOUNTS_FILE) -> list[dict[str, Any]]:
    sqlite_store = _ensure_sqlite(Path(path))
    if sqlite_store is not None:
        db_path, connection = sqlite_store
        try:
            revision = 0
            try:
                revision = int(_meta_value(connection, "revision") or 0)
            except (TypeError, ValueError, sqlite3.Error):
                revision = 0
            cache_key: tuple[str, int] | None = None
            try:
                cache_key = (str(Path(db_path).resolve()), revision)
            except OSError:
                cache_key = (str(db_path), revision)
            if cache_key is not None:
                with _ACCOUNT_ROWS_CACHE_LOCK:
                    cached = _ACCOUNT_ROWS_CACHE.get(cache_key)
                    if cached is not None:
                        # Callers may mutate the returned records; hand out
                        # shallow copies like the JSON snapshot cache does.
                        return [dict(item) for item in cached]
                    # Miss: drop the stale snapshot for this store before
                    # decoding a fresh one.  Holding both versions at once
                    # would roughly double the resident footprint.
                    store_key = cache_key[0]
                    for existing in [
                        key for key in _ACCOUNT_ROWS_CACHE if key[0] == store_key
                    ]:
                        _ACCOUNT_ROWS_CACHE.pop(existing, None)
            records = _sqlite_rows(connection)
            if cache_key is not None:
                # Re-read while the connection is open.  If a writer bumped
                # the revision during the decode, skip publishing a snapshot
                # under the stale key; the next read rebuilds it.
                try:
                    current_revision = int(_meta_value(connection, "revision") or 0)
                except (TypeError, ValueError, sqlite3.Error):
                    current_revision = revision
                if current_revision == revision:
                    with _ACCOUNT_ROWS_CACHE_LOCK:
                        while len(_ACCOUNT_ROWS_CACHE) >= _ACCOUNT_ROWS_CACHE_MAX_ENTRIES:
                            _ACCOUNT_ROWS_CACHE.pop(next(iter(_ACCOUNT_ROWS_CACHE)))
                        _ACCOUNT_ROWS_CACHE[cache_key] = records
            return [dict(item) for item in records]
        finally:
            connection.close()
    # Writers build a complete temporary file and atomically replace ``path``.
    # Reading the current snapshot therefore never observes a partial write and
    # does not need to wait behind registration or maintenance writers.
    cached = _cached_snapshot(path)
    if cached is not None:
        return cached
    before = _snapshot_stamp(path)
    records = _canonical_snapshot(path)
    after = _snapshot_stamp(path)
    if before == after:
        _cache_snapshot(path, records, stamp=after)
    return records


def upsert_account_record(
    record: dict[str, Any],
    path: Path = ACCOUNTS_FILE,
    *,
    create: bool = True,
) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise TypeError("account record must be a dict")
    prepared = _ordered_record(dict(record))
    prepared = _ensure_stable_user_id(prepared)
    prepared.setdefault("registered_at", _utc_now_iso())

    sqlite_store = _ensure_sqlite(Path(path))
    if sqlite_store is not None:
        _, connection = sqlite_store
        try:
            connection.execute("BEGIN IMMEDIATE")
            if not create and not _matching_sqlite_rows(connection, prepared):
                # Worker write-back for an account deleted mid-batch: drop
                # the write instead of resurrecting the row.
                connection.rollback()
                return prepared
            saved = _upsert_sqlite_record(connection, prepared)
            _bump_revision(connection)
            connection.commit()
            return saved
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    with _file_lock(path):
        records = _canonical_snapshot(path)
        if not create and not _record_matches_existing(records, prepared):
            return prepared
        records.append(prepared)
        normalized = normalize_accounts(records)
        matches = _safe_account_matches(normalized, prepared)
        _write_unlocked(path, normalized)
        return matches[0] if matches else prepared


def upsert_account_records(
    records: Iterable[dict[str, Any]],
    path: Path = ACCOUNTS_FILE,
    *,
    create: bool = True,
    skip_existing: bool = False,
) -> list[dict[str, Any]]:
    """Merge a batch with one locked read/normalize/write cycle.

    ``skip_existing`` atomically adds missing people without changing existing
    rows, for backup merge/restore. Return only records actually written.
    Callers that need a full
    store snapshot should use :func:`load_accounts`; returning every payload
    here made a large health-check batch repeatedly decode the whole SQLite
    database after each small write.
    """
    prepared: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            raise TypeError("account record must be a dict")
        item = _ordered_record(dict(record))
        item = _ensure_stable_user_id(item)
        item.setdefault("registered_at", _utc_now_iso())
        prepared.append(item)
    if not prepared:
        return []

    sqlite_store = _ensure_sqlite(Path(path))
    if sqlite_store is not None:
        _, connection = sqlite_store
        try:
            connection.execute("BEGIN IMMEDIATE")
            saved: list[dict[str, Any]] = []
            for item in prepared:
                matches = _matching_sqlite_rows(connection, item)
                if (skip_existing and matches) or (not create and not matches):
                    continue
                saved.append(_upsert_sqlite_record(connection, item))
            if saved:
                _bump_revision(connection)
            connection.commit()
            return saved
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    # Large account records include nested session payloads. A batch rewrite can
    # legitimately take longer than the short timeout used by interactive writes.
    with _file_lock(path, timeout=30.0):
        snapshot = _canonical_snapshot(path)
        touched = []
        for item in prepared:
            matches = _record_matches_existing([*snapshot, *touched], item)
            if (skip_existing and matches) or (not create and not matches):
                continue
            touched.append(item)
        if not touched:
            return []
        merged = [*snapshot, *touched]
        normalized = normalize_accounts(merged)
        saved = [matches[0] for item in touched if (matches := _safe_account_matches(normalized, item))]
        _write_unlocked(path, normalized)
        return saved


def compact_account_file(path: Path = ACCOUNTS_FILE) -> int:
    """Rewrite the store in canonical form and return the final record count."""
    sqlite_store = _ensure_sqlite(Path(path))
    if sqlite_store is not None:
        db_path, connection = sqlite_store
        try:
            count = int(connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])
            connection.execute("PRAGMA optimize")
        finally:
            connection.close()
        vacuum = sqlite3.connect(str(db_path), timeout=30.0)
        try:
            vacuum.execute("PRAGMA busy_timeout=30000")
            vacuum.execute("VACUUM")
        finally:
            vacuum.close()
        return count
    with _file_lock(path):
        normalized = _canonical_snapshot(path)
        _write_unlocked(path, normalized)
        return len(normalized)


def account_delete_key(record: dict[str, Any]) -> str:
    kind, value = _record_identity(record)
    return f"{kind}:{value}"


def rebind_account_record(
    old_key: str,
    new_email: str,
    fields: dict[str, Any] | None = None,
    *,
    path: Path = ACCOUNTS_FILE,
) -> dict[str, Any]:
    """Atomically replace one account's email and refreshed credentials.

    A normal upsert intentionally merges partial registration results. Email
    rebinding has a stricter contract: the target address must not belong to a
    different account, and the source row must be changed in one transaction.
    This helper keeps that invariant for both the production SQLite store and
    the legacy JSON fallback used by tests/imports.
    """
    wanted = _s(old_key).strip()
    target_email = _norm_email(new_email)
    if not wanted:
        raise ValueError("账号标识不能为空")
    if not target_email:
        raise ValueError("新邮箱格式无效")
    updates = dict(fields or {})
    updates["email"] = target_email
    updates["updated_at"] = _utc_now_iso()

    def _matches_key(record: dict[str, Any]) -> bool:
        key = account_delete_key(record)
        if key.casefold() == wanted.casefold():
            return True
        lowered = wanted.casefold()
        email = _norm_email(record.get("email"))
        account_id = _s(record.get("account_id")).lower()
        return (
            (lowered.startswith("email:") and email == lowered[6:])
            or (lowered.startswith("account_id:") and account_id == lowered[11:])
            or (email and email == lowered)
            or (account_id and account_id == lowered)
        )

    sqlite_store = _ensure_sqlite(Path(path))
    if sqlite_store is not None:
        _, connection = sqlite_store
        try:
            connection.execute("BEGIN IMMEDIATE")
            source_rows = connection.execute(
                "SELECT canonical_key, payload FROM accounts "
                "WHERE canonical_key = ? OR email = ? OR account_id = ?",
                (
                    wanted,
                    wanted.removeprefix("email:").lower()
                    if wanted.lower().startswith("email:")
                    else wanted.lower(),
                    wanted.removeprefix("account_id:").lower()
                    if wanted.lower().startswith("account_id:")
                    else wanted.lower(),
                ),
            ).fetchall()
            source: dict[str, Any] | None = None
            source_canonical = ""
            for canonical, payload in source_rows:
                decoded = _decode_payload(payload)
                if decoded is not None and _matches_key(decoded):
                    source = decoded
                    source_canonical = str(canonical)
                    break
            if source is None:
                raise KeyError(f"账号不存在: {wanted}")

            conflicts = connection.execute(
                "SELECT canonical_key, payload FROM accounts WHERE email = ?",
                (target_email,),
            ).fetchall()
            for canonical, payload in conflicts:
                if str(canonical) == source_canonical:
                    continue
                decoded = _decode_payload(payload)
                if decoded is not None:
                    raise ValueError(f"新邮箱已绑定其它账号: {target_email}")

            updated = _ordered_record({**source, **updates})
            # Preserve the original registration timestamp while making the
            # rebinding visible to maintenance and exports.
            updated["registered_at"] = source.get("registered_at") or _utc_now_iso()
            connection.execute(
                "DELETE FROM accounts WHERE canonical_key = ?", (source_canonical,)
            )
            saved = _store_sqlite_record(connection, updated)
            _bump_revision(connection)
            connection.commit()
            return saved
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    with _file_lock(path, timeout=30.0):
        records = _canonical_snapshot(path)
        source_index = next(
            (index for index, record in enumerate(records) if _matches_key(record)),
            -1,
        )
        if source_index < 0:
            raise KeyError(f"账号不存在: {wanted}")
        source = dict(records[source_index])
        for index, record in enumerate(records):
            if index == source_index:
                continue
            if _norm_email(record.get("email")) == target_email:
                raise ValueError(f"新邮箱已绑定其它账号: {target_email}")
        updated = _ordered_record({**source, **updates})
        updated["registered_at"] = source.get("registered_at") or _utc_now_iso()
        records[source_index] = updated
        normalized = normalize_accounts(records)
        _write_unlocked(path, normalized)
        return next(
            (record for record in normalized if _norm_email(record.get("email")) == target_email),
            updated,
        )


def load_account_records_by_keys(
    keys: Iterable[str],
    path: Path = ACCOUNTS_FILE,
) -> dict[str, dict[str, Any]]:
    """Load only explicitly requested full account records, keyed by identity."""
    wanted = tuple(
        dict.fromkeys(str(key or "").strip() for key in keys if str(key or "").strip())
    )
    if not wanted:
        return {}

    sqlite_store = _ensure_sqlite(Path(path))
    if sqlite_store is not None:
        _, connection = sqlite_store
        try:
            found: dict[str, dict[str, Any]] = {}
            for start in range(0, len(wanted), 500):
                chunk = wanted[start : start + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"SELECT canonical_key, payload FROM accounts "
                    f"WHERE canonical_key IN ({placeholders})",
                    chunk,
                ).fetchall()
                for key, payload in rows:
                    record = _decode_payload(payload)
                    if record is not None:
                        found[str(key)] = record
            return found
        finally:
            connection.close()

    wanted_set = set(wanted)
    return {
        key: record
        for record in load_accounts(path)
        if (key := account_delete_key(record)) in wanted_set
    }


def load_account_records_by_emails(
    emails: Iterable[Any],
    path: Path = ACCOUNTS_FILE,
) -> list[dict[str, Any]]:
    """Load full records for the requested emails via the indexed column.

    Batch endpoints (SMS docking, RT history backfill) select a handful of
    accounts while the store can hold thousands of rows; decoding the whole
    payload snapshot just to filter by email wastes memory and CPU.  The
    ``email`` column stores ``_norm_email`` output, so query it directly in
    chunks; the JSON backend keeps the full-scan fallback.
    """
    wanted = list(
        dict.fromkeys(
            _norm_email(email)
            for email in emails
            if _norm_email(email)
        )
    )
    if not wanted:
        return []

    sqlite_store = _ensure_sqlite(Path(path))
    if sqlite_store is not None:
        _, connection = sqlite_store
        try:
            records: list[dict[str, Any]] = []
            for start in range(0, len(wanted), 500):
                chunk = wanted[start : start + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"SELECT payload FROM accounts "
                    f"WHERE email <> '' AND email IN ({placeholders})",
                    chunk,
                ).fetchall()
                for (payload,) in rows:
                    record = _decode_payload(payload)
                    if record is not None:
                        records.append(record)
            return records
        finally:
            connection.close()

    wanted_set = set(wanted)
    return [
        record
        for record in load_accounts(path)
        if _norm_email(record.get("email")) in wanted_set
    ]


def _record_matches_existing(
    records: list[dict[str, Any]],
    prepared: dict[str, Any],
) -> bool:
    """Whether ``prepared`` matches a stored row by identity, email or id."""
    return bool(_safe_account_matches(records, prepared))


def account_record_exists(
    record: dict[str, Any],
    path: Path = ACCOUNTS_FILE,
) -> bool:
    """Whether a row matching this record's identity still exists.

    Long-running workers (reauth / health-check batches) hold in-memory
    snapshots; write-backs consult this so accounts deleted mid-batch are
    skipped instead of being resurrected by the next upsert.
    """
    if not isinstance(record, dict):
        return False
    sqlite_store = _ensure_sqlite(Path(path))
    if sqlite_store is not None:
        _, connection = sqlite_store
        try:
            return bool(_matching_sqlite_rows(connection, record))
        finally:
            connection.close()
    return _record_matches_existing(_canonical_snapshot(path), record)


def load_account_record_by_identity(
    identity: Any,
    path: Path = ACCOUNTS_FILE,
) -> dict[str, Any] | None:
    """Load one full account row by its canonical identity.

    The panel frequently needs the credentials for one pasted email while the
    account table can contain thousands of rows.  The production SQLite store
    keeps separate indexes for ``canonical_key``, ``email`` and ``account_id``;
    query those columns directly instead of decoding the complete payload
    snapshot.  The JSON backend retains the legacy full-snapshot fallback.

    ``identity`` accepts the same forms used by ``account_delete_key`` and row
    actions: ``user:...``, ``email:...``, ``account_id:...``, a raw email, or a raw account
    id.  Email/account-id prefixes are normalized case-insensitively.
    """
    raw = _s(identity).strip()
    if not raw:
        return None
    lowered = raw.lower()
    canonical = ""
    email = ""
    account_id = ""
    if lowered.startswith("user:"):
        user_id = _s(lowered[5:])
        canonical = f"user:{user_id}" if user_id else ""
    elif lowered.startswith("email:"):
        email = _norm_email(lowered[6:])
        canonical = f"email:{email}" if email else ""
    elif lowered.startswith("account_id:"):
        account_id = _s(lowered[11:]).lower()
        canonical = f"account_id:{account_id}" if account_id else ""
    elif "@" in lowered and " " not in lowered:
        email = _norm_email(lowered)
        canonical = f"email:{email}" if email else ""
    else:
        account_id = lowered
        canonical = f"account_id:{account_id}" if account_id else ""

    sqlite_store = _ensure_sqlite(Path(path))
    if sqlite_store is not None:
        _, connection = sqlite_store
        try:
            # Keep the lookup order explicit.  It avoids an OR plan choosing
            # an unexpected row if a legacy database briefly contains an
            # inconsistent duplicate identity.
            queries: list[tuple[str, str]] = []
            if canonical:
                queries.append(("canonical_key = ?", canonical))
            if email:
                queries.append(("email <> '' AND email = ?", email))
            if account_id:
                queries.append(("account_id <> '' AND account_id = ?", account_id))
            for clause, value in queries:
                row = connection.execute(
                    f"SELECT payload FROM accounts WHERE {clause} LIMIT 1",
                    (value,),
                ).fetchone()
                if row is None:
                    continue
                record = _decode_payload(row[0])
                if record is not None:
                    return record
            return None
        finally:
            connection.close()

    # JSON-only deployments have no indexed columns.  Preserve their existing
    # compatibility behavior while keeping the public API identical.
    for record in load_accounts(path):
        record_email = _norm_email(record.get("email"))
        record_account_id = _s(record.get("account_id")).lower()
        record_key = account_delete_key(record).lower()
        if (
            (canonical and record_key == canonical)
            or (email and record_email == email)
            or (account_id and record_account_id == account_id)
        ):
            return record
    return None


def delete_account_records(
    keys: Iterable[str],
    *,
    delete_all: bool = False,
    include_records: bool = True,
    path: Path = ACCOUNTS_FILE,
) -> tuple[int, list[dict[str, Any]]]:
    """Delete records by public delete_key.

    The legacy return contract is ``(removed, remaining)``.  Callers that only
    need the count can pass ``include_records=False`` to avoid decoding every
    remaining account payload after the delete.
    """
    wanted = {str(k or "").strip() for k in keys if str(k or "").strip()}
    sqlite_store = _ensure_sqlite(Path(path))
    if sqlite_store is not None:
        _, connection = sqlite_store
        try:
            connection.execute("BEGIN IMMEDIATE")
            before = connection.total_changes
            if delete_all:
                connection.execute("DELETE FROM accounts")
            elif wanted:
                connection.executemany(
                    "DELETE FROM accounts WHERE canonical_key = ?",
                    ((key,) for key in wanted),
                )
            removed = connection.total_changes - before
            if removed:
                _bump_revision(connection)
            connection.commit()
            return removed, _sqlite_rows(connection) if include_records else []
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    with _file_lock(path):
        records = _canonical_snapshot(path)
        if delete_all:
            removed = len(records)
            remaining: list[dict[str, Any]] = []
        else:
            remaining = [r for r in records if account_delete_key(r) not in wanted]
            removed = len(records) - len(remaining)
        if removed:
            _write_unlocked(path, remaining)
        return removed, remaining if include_records else []


def assign_account_group(
    keys: Iterable[str],
    group_id: str,
    *,
    delete_all: bool = False,
    include_records: bool = True,
    path: Path = ACCOUNTS_FILE,
) -> tuple[int, list[dict[str, Any]]]:
    """Assign selected account records to one group."""
    wanted = {str(k or "").strip() for k in keys if str(k or "").strip()}
    target = str(group_id or "").strip()
    sqlite_store = _ensure_sqlite(Path(path))
    if sqlite_store is not None:
        _, connection = sqlite_store
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = 0
            if delete_all:
                changed = _update_sqlite_group_records(
                    connection,
                    target,
                    "group_id <> ?",
                    (target,),
                )
            elif wanted:
                wanted_list = tuple(wanted)
                for start in range(0, len(wanted_list), 500):
                    chunk = wanted_list[start : start + 500]
                    placeholders = ",".join("?" for _ in chunk)
                    changed += _update_sqlite_group_records(
                        connection,
                        target,
                        f"canonical_key IN ({placeholders}) AND group_id <> ?",
                        (*chunk, target),
                    )
            if changed:
                _bump_revision(connection)
            connection.commit()
            return changed, _sqlite_rows(connection) if include_records else []
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    with _file_lock(path):
        records = _canonical_snapshot(path)
        changed = 0
        for record in records:
            if delete_all or account_delete_key(record) in wanted:
                if _s(record.get("group_id")) != target:
                    record["group_id"] = target
                    record["updated_at"] = _utc_now_iso()
                    changed += 1
        normalized = normalize_accounts(records) if changed else records
        if changed:
            _write_unlocked(path, normalized)
        return changed, normalized if include_records else []


def reassign_account_group(
    source_group_id: str,
    target_group_id: str,
    *,
    path: Path = ACCOUNTS_FILE,
) -> int:
    """Move every account in one group without materializing the full store."""
    source = _s(source_group_id)
    target = _s(target_group_id)
    if not target:
        raise ValueError("目标分组不能为空")
    if source == target:
        return 0

    sqlite_store = _ensure_sqlite(Path(path))
    if sqlite_store is not None:
        _, connection = sqlite_store
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = _update_sqlite_group_records(
                connection,
                target,
                "group_id = ?",
                (source,),
            )
            if changed:
                _bump_revision(connection)
            connection.commit()
            return changed
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    with _file_lock(path):
        records = _canonical_snapshot(path)
        changed = 0
        now_iso = _utc_now_iso()
        for record in records:
            if _s(record.get("group_id")) != source:
                continue
            record["group_id"] = target
            record["updated_at"] = now_iso
            changed += 1
        if changed:
            _write_unlocked(path, records)
        return changed


def update_account_records(
    keys: Iterable[str],
    fields: dict[str, Any],
    *,
    include_records: bool = True,
    path: Path = ACCOUNTS_FILE,
) -> tuple[int, list[dict[str, Any]]]:
    """Apply operational metadata to selected account records.

    ``include_records`` preserves the historical full-snapshot return by
    default for callers that use the second tuple item.  Writers that only
    need the changed count can pass ``False`` and avoid decoding every account
    payload after the update.
    """
    wanted = {str(key or "").strip() for key in keys if str(key or "").strip()}
    sqlite_store = _ensure_sqlite(Path(path))
    if sqlite_store is not None:
        _, connection = sqlite_store
        try:
            connection.execute("BEGIN IMMEDIATE")
            if wanted:
                wanted_list = tuple(wanted)
                rows: list[tuple[Any]] = []
                # Keep each statement below SQLite's variable limit. This also
                # bounds parse/plan cost when a whole account page is updated.
                for start in range(0, len(wanted_list), 500):
                    chunk = wanted_list[start : start + 500]
                    placeholders = ",".join("?" for _ in chunk)
                    rows.extend(
                        connection.execute(
                            f"SELECT payload FROM accounts WHERE canonical_key IN ({placeholders})",
                            chunk,
                        ).fetchall()
                    )
            else:
                rows = []
            changed = 0
            now_iso = _utc_now_iso()
            for row in rows:
                record = _decode_payload(row[0])
                if record is None:
                    continue
                record.update({str(field): value for field, value in fields.items()})
                record["updated_at"] = now_iso
                _store_sqlite_record(connection, record)
                changed += 1
            if changed:
                _bump_revision(connection)
            connection.commit()
            return changed, _sqlite_rows(connection) if include_records else []
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    with _file_lock(path):
        records = _canonical_snapshot(path)
        changed = 0
        for record in records:
            if account_delete_key(record) not in wanted:
                continue
            for field, value in fields.items():
                record[str(field)] = value
            record["updated_at"] = _utc_now_iso()
            changed += 1
        normalized = normalize_accounts(records) if changed else records
        if changed:
            _write_unlocked(path, normalized)
        return changed, normalized if include_records else []


def update_account_record_fields(
    updates: dict[str, dict[str, Any]],
    *,
    include_records: bool = True,
    path: Path = ACCOUNTS_FILE,
    expected_fields: dict[str, dict[str, Any]] | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    """Apply distinct field maps to many account rows in one atomic rewrite.

    The default return contract remains ``(changed, full_snapshot)``.  Batch
    workers should pass ``include_records=False`` when they do not consume the
    snapshot, avoiding a full payload decode on every flush.

    Optional per-key expectations are checked against the current row inside
    the write transaction. A stale row is skipped. Identity-key migrations
    remove the old row atomically; an occupied target key aborts the batch
    instead of overwriting another row or implicitly merging its credentials.
    """
    wanted = {str(key): value for key, value in updates.items() if str(key) and isinstance(value, dict)}
    expected = {} if expected_fields is None else {
        str(key): value for key, value in expected_fields.items()
    }
    if any(not isinstance(value, dict) for value in expected.values()):
        raise TypeError("expected_fields values must be field maps")
    if not wanted:
        return 0, load_accounts(path) if include_records else []

    def prepare_update(key: str, record: dict[str, Any], now_iso: str) -> dict[str, Any] | None:
        fields = wanted.get(key)
        if not fields:
            return None
        condition = expected.get(key)
        if condition is not None and any(
            record.get(field) != value for field, value in condition.items()
        ):
            return None
        updated = _ensure_stable_user_id({**record, **fields, "updated_at": now_iso})
        old_user = account_stable_user_id(record)
        new_user = account_stable_user_id(updated)
        if old_user and new_user != old_user:
            raise ValueError("account_identity_conflict: 字段更新不能替换已有用户身份")
        return updated

    sqlite_store = _ensure_sqlite(Path(path))
    if sqlite_store is not None:
        _, connection = sqlite_store
        try:
            connection.execute("BEGIN IMMEDIATE")
            wanted_list = tuple(wanted)
            rows: list[tuple[Any, Any]] = []
            for start in range(0, len(wanted_list), 500):
                chunk = wanted_list[start : start + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows.extend(
                    connection.execute(
                        f"SELECT canonical_key, payload FROM accounts WHERE canonical_key IN ({placeholders})",
                        chunk,
                    ).fetchall()
                )
            changed = 0
            now_iso = _utc_now_iso()
            reserved_keys = {str(key) for key, _ in rows}
            for key, payload in rows:
                record = _decode_payload(payload)
                if record is None:
                    continue
                updated = prepare_update(str(key), record, now_iso)
                if updated is None:
                    continue
                new_key = account_delete_key(updated)
                if new_key != str(key):
                    occupied = connection.execute(
                        "SELECT 1 FROM accounts WHERE canonical_key = ?", (new_key,)
                    ).fetchone()
                    if new_key in reserved_keys or occupied is not None:
                        raise ValueError("account_identity_conflict: 更新后的账号标识已存在，未覆盖原记录")
                    reserved_keys.add(new_key)
                    connection.execute("DELETE FROM accounts WHERE canonical_key = ?", (key,))
                _store_sqlite_record(connection, updated)
                changed += 1
            if changed:
                _bump_revision(connection)
            connection.commit()
            return changed, _sqlite_rows(connection) if include_records else []
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    with _file_lock(path, timeout=30.0):
        records = _canonical_snapshot(path)
        reserved_keys = {account_delete_key(record) for record in records}
        changed = 0
        now_iso = _utc_now_iso()
        for index, record in enumerate(records):
            key = account_delete_key(record)
            updated = prepare_update(key, record, now_iso)
            if updated is None:
                continue
            new_key = account_delete_key(updated)
            if new_key != key:
                if new_key in reserved_keys:
                    raise ValueError("account_identity_conflict: 更新后的账号标识已存在，未覆盖原记录")
                reserved_keys.add(new_key)
            records[index] = updated
            changed += 1
        normalized = normalize_accounts(records) if changed else records
        # A stale key set is a valid no-op. Avoid rewriting the entire legacy
        # JSON export (and its fsync) when no row matched.
        if changed:
            _write_unlocked(path, normalized)
        return changed, normalized if include_records else []


def _mask_account_secret(value: Any) -> str:
    text = _s(value)
    if not text:
        return ""
    if len(text) <= 4:
        return "••••"
    return f"{text[:2]}••••{text[-2:]}"


def normalized_health_status(record: dict[str, Any]) -> str:
    """Map legacy token-rejection errors to the invalid account state."""
    status = _s(record.get("health_status")).lower()
    error = _s(record.get("health_error")).lower()
    token = _s(record.get("access_token"))
    try:
        segment = token.split(".")[1]
        segment += "=" * (-len(segment) % 4)
        expires_at = int(json.loads(base64.urlsafe_b64decode(segment)).get("exp") or 0)
    except Exception:
        expires_at = 0
    if (
        status == "invalid"
        and expires_at > 0
        and expires_at <= int(time.time())
        and ("token invalidated" in error or "oauth_refresh_http_401" in error)
    ):
        return "reauth_required"
    if account_is_deactivated(record):
        return "invalid"
    if "oauth_refresh_http_401" in error:
        return "invalid"
    return status


def account_is_usable(record: dict[str, Any]) -> bool:
    """Return whether a health-checked account may enter a usable pool."""
    return not account_is_deactivated(record) and normalized_health_status(record) in {
        "alive",
        "limited",
    }


def _access_token_expires_at(record: dict[str, Any]) -> int:
    """Return the stored AT's JWT exp epoch (0 when absent/unparseable)."""
    token = _s(record.get("access_token"))
    if not token:
        return 0
    try:
        segment = token.split(".")[1]
        segment += "=" * (-len(segment) % 4)
        return int(json.loads(base64.urlsafe_b64decode(segment)).get("exp") or 0)
    except Exception:
        return 0


def account_access_token_expired(record: dict[str, Any]) -> bool:
    """Return True when the stored AT parses and its exp has already passed."""
    expires_at = _access_token_expires_at(record)
    return expires_at > 0 and expires_at <= int(time.time())


def account_public_view(record: dict[str, Any]) -> dict[str, Any]:
    """Return a panel-safe account row without raw tokens or raw password."""
    password = _s(record.get("password"))
    totp_secret = _s(record.get("totp_secret"))
    source_type = _s(record.get("source_type")).lower()
    registration_method = _s(record.get("registration_method")).lower()
    browser_type = _s(record.get("browser_type")).lower()
    health_status = normalized_health_status(record)
    health_error = _s(record.get("health_error")).lower()
    stored_registration_state = _s(record.get("registration_state")).lower()
    registration_state = stored_registration_state or "security_pending"
    is_deactivated = account_is_deactivated(record) or _s(record.get("status")) == "已封号"
    reauth_available = (
        bool(_norm_email(record.get("email")))
        and health_status in {"invalid", "error", "reauth_required"}
        and not is_deactivated
    )
    display_status = "已封号" if is_deactivated else record.get("status")
    if not is_deactivated and health_status in {"invalid", "error", "reauth_required"}:
        display_status = "异常"
    # A browser flow can persist a generated candidate password before the
    # password endpoint confirms it.  In that case the string is present but
    # ``password_set`` is explicitly False, so the row still needs the repair
    # workflow.  Legacy rows often predate this marker; an absent value keeps
    # their historical password-present semantics.
    password_unconfirmed = record.get("password_set") is False
    # Keep the raw presence flag separate from confirmation state. Browser
    # registration can persist a candidate password before the server accepts
    # it; callers that perform credential operations must use this field.
    password_confirmed = bool(password) and not password_unconfirmed
    security_repair_available = (
        bool(_norm_email(record.get("email")))
        and not is_deactivated
        and (not password or password_unconfirmed or not totp_secret)
    )
    # Records created before these fields existed still carry an unambiguous
    # registration source. The exact legacy browser provider was not stored.
    if not registration_method:
        if source_type == "fingerprint_browser":
            registration_method = "browser"
        elif source_type in {"web", "microsoft"}:
            registration_method = "protocol"
    session = record.get("session") if isinstance(record.get("session"), dict) else {}
    has_access_token = bool(
        _s(record.get("access_token"))
        or _s(session.get("accessToken"))
        or _s(session.get("access_token"))
    )
    # A missing/optimistic state must not make a partial record look complete.
    # A TOTP seed is only a local secret; the server must have confirmed MFA.
    credential_set_complete = bool(
        has_access_token
        and _s(record.get("account_id"))
        and password_confirmed
        and totp_secret
        and record.get("mfa_enabled") is True
    )
    if not credential_set_complete:
        registration_state = "security_pending"
    elif not stored_registration_state:
        registration_state = "complete"
    trial_status = _s(record.get("trial_eligibility_status"))
    trial_campaign = _s(record.get("trial_eligibility_campaign_id"))
    # Older rows stored every non-free-trial campaign as ``ineligible``.
    # Normalize those rows at read time so existing discount offers become
    # visible immediately without rewriting the whole account database.
    if trial_status == "ineligible" and trial_campaign and trial_campaign != "plus-1-month-free":
        trial_status = "discount"
    trial_label = _s(record.get("trial_eligibility_label"))
    if trial_status == "discount":
        trial_label = "有折扣优惠"
    return {
        "delete_key": account_delete_key(record),
        "email": record.get("email"),
        "has_password": bool(password),
        "password_confirmed": password_confirmed,
        "password_masked": _mask_account_secret(password),
        "mfa_enabled": record.get("mfa_enabled") is True,
        "mfa_confirmed": record.get("mfa_enabled") is True,
        "has_totp": bool(totp_secret),
        "totp_secret_masked": _mask_account_secret(totp_secret),
        "mfa_factor_type": _s(record.get("mfa_factor_type")),
        "security_repair_available": security_repair_available,
        "plan_type": _normalize_plan_type(record.get("plan_type")),
        "account_id": record.get("account_id"),
        "user_id": _s(record.get("user_id")),
        "workspace_account_id": _s(record.get("workspace_account_id")),
        "proxy": record.get("proxy"),
        "registration_method": registration_method,
        "registration_trace_file": _s(record.get("registration_trace_file")),
        "registration_state": registration_state,
        "registration_state_label": {
            "security_pending": "待补全安全字段",
            "complete": "已完成",
        }.get(registration_state, registration_state),
        "browser_type": browser_type,
        "group_id": _s(record.get("group_id")),
        "registered_at": record.get("registered_at"),
        "updated_at": record.get("updated_at"),
        "last_invalid_at": record.get("last_invalid_at"),
        "has_access_token": has_access_token,
        "at_expired": account_access_token_expired(record),
        "at_expires_epoch": _access_token_expires_at(record),
        "has_refresh_token": bool(record.get("refresh_token")),
        "has_session_token": bool(record.get("session_token")),
        "source_type": record.get("source_type"),
        "status": display_status,
        "health_status": health_status,
        "is_deactivated": is_deactivated,
        "reauth_available": reauth_available,
        "health_checked_at": record.get("health_checked_at"),
        "health_error": record.get("health_error") if health_status in {"error", "invalid", "reauth_required"} else "",
        "health_probe_error": record.get("health_probe_error") if health_status == "unknown" else "",
        "health_last_confirmed_status": record.get("health_last_confirmed_status") or "",
        "health_last_confirmed_at": record.get("health_last_confirmed_at") or "",
        "trial_eligibility_status": trial_status or ("pending" if has_access_token else "no_at"),
        "trial_eligibility_label": trial_label or ("待检测" if has_access_token else "无 AT"),
        "trial_eligibility_checked_at": record.get("trial_eligibility_checked_at") or "",
        "trial_eligibility_reason": _s(record.get("trial_eligibility_reason")),
        "trial_eligibility_campaign_id": trial_campaign,
        "trial_eligibility_method": _s(record.get("trial_eligibility_method")),
        "trial_eligibility_proxy_mode": _s(record.get("trial_eligibility_proxy_mode")) or "direct",
        "trial_eligibility_has_promotion": bool(record.get("trial_eligibility_has_promotion")),
        "trial_eligibility_promotion_count": int(record.get("trial_eligibility_promotion_count") or 0),
        "trial_eligibility_error": _s(record.get("trial_eligibility_error")) if _s(record.get("trial_eligibility_status")) in {"error", "invalid"} else "",
        "quota": record.get("quota"),
        "image_quota_unknown": bool(record.get("image_quota_unknown")),
        "restore_at": record.get("restore_at"),
        "c2a_upload_status": record.get("c2a_upload_status"),
        "c2a_uploaded_at": record.get("c2a_uploaded_at"),
        "c2a_upload_error": record.get("c2a_upload_error"),
    }


def _normalize_trial_public_view(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize legacy public rows without rewriting the account database."""
    # Public rows cached by older SQLite snapshots predate the explicit
    # password confirmation flag. Infer the only ambiguous case from the
    # fields that were already exposed, so pagination remains compatible until
    # the account is next written.
    if "password_confirmed" not in item:
        item["password_confirmed"] = bool(item.get("has_password")) and not (
            bool(item.get("security_repair_available"))
            and bool(item.get("has_totp"))
        )
    status = _s(item.get("trial_eligibility_status"))
    campaign = _s(item.get("trial_eligibility_campaign_id"))
    if status == "ineligible" and campaign and campaign != "plus-1-month-free":
        item["trial_eligibility_status"] = "discount"
        item["trial_eligibility_label"] = "有折扣优惠"
    if "trial_eligibility_has_promotion" not in item:
        item["trial_eligibility_has_promotion"] = bool(campaign)
    if "trial_eligibility_promotion_count" not in item:
        item["trial_eligibility_promotion_count"] = 1 if campaign else 0
    return item


def load_account_public_views(
    limit: int | None = None,
    path: Path = ACCOUNTS_FILE,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Load panel-safe rows without decoding full session/cookie payloads."""
    start = max(0, int(offset))
    sqlite_store = _ensure_sqlite(Path(path))
    if sqlite_store is None:
        records = load_accounts(path)
        stop = None if limit is None else start + max(0, int(limit))
        selected = records[start:stop]
        return [_normalize_trial_public_view(account_public_view(record)) for record in selected]

    _, connection = sqlite_store
    try:
        sql = (
            "SELECT public_payload FROM accounts "
            "ORDER BY registered_epoch DESC, email DESC, canonical_key"
        )
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params = (max(0, int(limit)), start)
        elif start:
            sql += " LIMIT -1 OFFSET ?"
            params = (start,)
        rows = connection.execute(sql, params).fetchall()
        result: list[dict[str, Any]] = []
        now = time.time()
        for row in rows:
            try:
                item = json.loads(str(row[0]))
            except Exception:
                continue
            if isinstance(item, dict):
                normalized = _normalize_trial_public_view(item)
                # at_expired 是随时间变化的派生值：public_payload 只存稳定的
                # at_expires_epoch，这里每次读取都按当前时间重算。
                expires_at = int(normalized.get("at_expires_epoch") or 0)
                normalized["at_expired"] = expires_at > 0 and expires_at <= now
                result.append(normalized)
        return result
    finally:
        connection.close()


def count_accounts(path: Path = ACCOUNTS_FILE) -> int:
    sqlite_store = _ensure_sqlite(Path(path))
    if sqlite_store is None:
        return len(load_accounts(path))
    _, connection = sqlite_store
    try:
        return int(connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])
    finally:
        connection.close()


def account_email_set(path: Path = ACCOUNTS_FILE) -> set[str]:
    """Return registered email identities without decoding full account payloads."""
    sqlite_store = _ensure_sqlite(Path(path))
    if sqlite_store is None:
        return {
            email
            for record in load_accounts(path)
            if (email := _norm_email(record.get("email")))
        }
    _, connection = sqlite_store
    try:
        return {
            str(row[0]).strip().lower()
            for row in connection.execute(
                "SELECT email FROM accounts WHERE email <> ''"
            )
            if str(row[0]).strip()
        }
    finally:
        connection.close()


def account_group_counts(path: Path = ACCOUNTS_FILE) -> dict[str, int]:
    sqlite_store = _ensure_sqlite(Path(path))
    if sqlite_store is None:
        counts: dict[str, int] = {}
        for record in load_accounts(path):
            group_id = _s(record.get("group_id"))
            counts[group_id] = counts.get(group_id, 0) + 1
        return counts
    _, connection = sqlite_store
    try:
        return {
            str(group_id): int(count)
            for group_id, count in connection.execute(
                "SELECT group_id, COUNT(*) FROM accounts GROUP BY group_id"
            )
        }
    finally:
        connection.close()


def account_health_counts(path: Path = ACCOUNTS_FILE) -> dict[str, int]:
    sqlite_store = _ensure_sqlite(Path(path))
    if sqlite_store is None:
        counts: dict[str, int] = {}
        for record in load_accounts(path):
            status = normalized_health_status(record) or "unknown"
            counts[status] = counts.get(status, 0) + 1
        return counts
    _, connection = sqlite_store
    try:
        return {
            str(status or "unknown"): int(count)
            for status, count in connection.execute(
                "SELECT health_status, COUNT(*) FROM accounts GROUP BY health_status"
            )
        }
    finally:
        connection.close()


def account_health_summary(path: Path = ACCOUNTS_FILE) -> dict[str, int]:
    """Return record, usable, and terminal-account counts as separate metrics."""
    states = {
        "alive": 0,
        "limited": 0,
        "invalid": 0,
        "reauth_required": 0,
        "error": 0,
        "unknown": 0,
    }
    sqlite_store = _ensure_sqlite(Path(path))
    deactivated = 0
    sqlite_cache_key: tuple[str, int] | None = None
    sqlite_cache_revision: int | None = None
    if sqlite_store is None:
        records = load_accounts(path)
        for record in records:
            state = normalized_health_status(record) or "unknown"
            states[state if state in states else "unknown"] += 1
            deactivated += int(account_is_deactivated(record))
    else:
        db_path, connection = sqlite_store
        current_revision = None
        try:
            # ``revision`` is bumped by every normal account-store write.  It
            # is a much cheaper and more precise invalidation signal than
            # decoding all account rows for each status request.  Include the
            # resolved DB path so test/secondary stores never share entries.
            try:
                sqlite_cache_revision = int(_meta_value(connection, "revision") or 0)
            except (TypeError, ValueError, sqlite3.Error):
                sqlite_cache_revision = 0
            try:
                sqlite_cache_key = (
                    str(Path(db_path).resolve()),
                    sqlite_cache_revision,
                )
            except OSError:
                sqlite_cache_key = (str(db_path), sqlite_cache_revision)
            with _ACCOUNT_HEALTH_SUMMARY_CACHE_LOCK:
                cached = _ACCOUNT_HEALTH_SUMMARY_CACHE.get(sqlite_cache_key)
                if cached is not None:
                    return dict(cached)
            try:
                grouped = connection.execute(
                    "SELECT health_status, "
                    "CASE WHEN json_extract(public_payload, '$.is_deactivated') = 1 "
                    "THEN 1 ELSE 0 END AS deactivated, COUNT(*) "
                    "FROM accounts GROUP BY health_status, deactivated"
                ).fetchall()
                for raw_state, row_deactivated, count in grouped:
                    state = "invalid" if int(row_deactivated or 0) else (_s(raw_state).lower() or "unknown")
                    states[state if state in states else "unknown"] += int(count)
                    deactivated += int(count) if int(row_deactivated or 0) else 0
            except sqlite3.OperationalError:
                for raw_state, payload in connection.execute(
                    "SELECT health_status, public_payload FROM accounts"
                ):
                    record = _decode_payload(payload) or {}
                    state = normalized_health_status(record) or _s(raw_state).lower() or "unknown"
                    states[state if state in states else "unknown"] += 1
                    deactivated += int(account_is_deactivated(record))
            # Re-read while the connection is open.  If a writer committed a
            # newer revision during the aggregate query, skip publishing this
            # snapshot under the old cache key.
            try:
                current_revision = int(_meta_value(connection, "revision") or 0)
            except (TypeError, ValueError, sqlite3.Error):
                current_revision = sqlite_cache_revision
        finally:
            connection.close()

    total = sum(states.values())
    usable = states["alive"] + states["limited"]
    deactivated = max(0, int(deactivated))
    # Terminally deactivated rows remain visible for audit/export, but they
    # are not part of the population that can be probed or used downstream.
    checkable = max(0, total - deactivated)
    checked = max(0, total - states["unknown"] - deactivated)
    result = {
        "total": total,
        "requested": total,
        "checkable": checkable,
        "usable": usable,
        "checked": checked,
        "unavailable": states["invalid"] + states["reauth_required"] + states["error"],
        "skipped_known_deactivated": deactivated,
        "deactivated": deactivated,
        "invalid_other": max(0, states["invalid"] - deactivated),
        **states,
    }
    if sqlite_cache_key is not None:
        # Do not publish a result if a concurrent writer changed the revision
        # while the aggregate query was running; the next request will build a
        # fresh snapshot under the new key.
        if current_revision == sqlite_cache_revision:
            with _ACCOUNT_HEALTH_SUMMARY_CACHE_LOCK:
                _ACCOUNT_HEALTH_SUMMARY_CACHE[sqlite_cache_key] = dict(result)
                while len(_ACCOUNT_HEALTH_SUMMARY_CACHE) > _ACCOUNT_HEALTH_SUMMARY_CACHE_MAX_ENTRIES:
                    _ACCOUNT_HEALTH_SUMMARY_CACHE.pop(
                        next(iter(_ACCOUNT_HEALTH_SUMMARY_CACHE))
                    )
    return dict(result)


def account_store_revision(path: Path = ACCOUNTS_FILE) -> int:
    sqlite_store = _ensure_sqlite(Path(path))
    if sqlite_store is None:
        inode, mtime, size = _snapshot_stamp(path)
        return hash((inode, mtime, size))
    _, connection = sqlite_store
    try:
        try:
            return int(_meta_value(connection, "revision") or 0)
        except ValueError:
            return 0
    finally:
        connection.close()


def replace_account_records(
    records: Iterable[dict[str, Any]],
    path: Path = ACCOUNTS_FILE,
) -> list[dict[str, Any]]:
    """Replace the account store from a JSON-compatible backup/import."""
    prepared = [dict(item) for item in records if isinstance(item, dict)]
    sqlite_store = _ensure_sqlite(Path(path))
    if sqlite_store is None:
        normalized = normalize_accounts(prepared)
        with _file_lock(path, timeout=30.0):
            _write_unlocked(path, normalized)
        return normalized

    _, connection = sqlite_store
    try:
        connection.execute("BEGIN IMMEDIATE")
        normalized = _replace_sqlite_records(connection, prepared)
        _bump_revision(connection)
        connection.commit()
        return normalized
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def export_account_records(path: Path = ACCOUNTS_FILE) -> bytes:
    payload = json.dumps(load_accounts(path), ensure_ascii=False, indent=2) + "\n"
    return payload.encode("utf-8")

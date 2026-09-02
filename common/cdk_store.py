# -*- coding: utf-8 -*-
"""Persistent CDKs bound to registered account mailboxes."""

from __future__ import annotations

import hashlib
import json
import secrets
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from config import ROOT

CDKS_FILE = ROOT / "data" / "cdks.json"
_LOCK = RLock()
_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now_dt()).isoformat().replace("+00:00", "Z")


def normalize_code(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def code_hash(value: Any) -> str:
    normalized = normalize_code(value)
    return hashlib.sha256(f"gpt-cdk-v1:{normalized}".encode("utf-8")).hexdigest()


def _new_code() -> str:
    payload = "".join(secrets.choice(_ALPHABET) for _ in range(20))
    chunks = [payload[index:index + 4] for index in range(0, len(payload), 4)]
    return "GPT-" + "-".join(chunks)


def _hint(code: str) -> str:
    parts = str(code).split("-")
    return f"GPT-{parts[1]}-••••-••••-••••-{parts[-1]}"


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [dict(item) for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with open(fd, "w", encoding="utf-8", closefd=True) as handle:
            json.dump(rows, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
        Path(tmp_name).replace(path)
    finally:
        Path(tmp_name).unlink(missing_ok=True)
    try:
        path.chmod(0o600)
    except Exception:
        pass


def _expired(record: dict[str, Any], now: datetime | None = None) -> bool:
    raw = str(record.get("expires_at") or "").strip()
    if not raw:
        return False
    try:
        expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return expires <= (now or _now_dt())
    except Exception:
        return False


def list_cdks(path: Path = CDKS_FILE) -> list[dict[str, Any]]:
    with _LOCK:
        return _read(path)


def lookup_cdk(code: str, path: Path = CDKS_FILE) -> dict[str, Any]:
    digest = code_hash(code)
    if len(normalize_code(code)) < 20:
        raise KeyError("CDK 无效")
    with _LOCK:
        record = next(
            (
                item
                for item in _read(path)
                if secrets.compare_digest(str(item.get("code_hash") or ""), digest)
            ),
            None,
        )
        if record is None:
            raise KeyError("CDK 无效")
        return dict(record)


def ensure_plain_codes(path: Path = CDKS_FILE) -> list[dict[str, Any]]:
    """Reissue unused legacy hash-only records so the admin can display them."""
    with _LOCK:
        rows = _read(path)
        known_hashes = {str(item.get("code_hash") or "") for item in rows}
        changed = False
        for record in rows:
            if str(record.get("code") or "").strip():
                continue
            if str(record.get("redeemed_at") or "").strip():
                continue
            code = _new_code()
            digest = code_hash(code)
            while digest in known_hashes:
                code = _new_code()
                digest = code_hash(code)
            record["code"] = code
            record["code_hash"] = digest
            record["code_hint"] = _hint(code)
            record["updated_at"] = _iso()
            known_hashes.add(digest)
            changed = True
        if changed:
            _write(path, rows)
        return [dict(item) for item in rows]


def create_cdks(
    bindings: list[dict[str, Any]],
    *,
    expires_days: int = 0,
    path: Path = CDKS_FILE,
) -> list[dict[str, Any]]:
    """Create and persist one unique code for each unbound account."""
    with _LOCK:
        rows = _read(path)
        bound = {str(item.get("account_key") or "") for item in rows}
        known_hashes = {str(item.get("code_hash") or "") for item in rows}
        created: list[dict[str, Any]] = []
        now = _now_dt()
        expires_at = _iso(now + timedelta(days=expires_days)) if expires_days > 0 else ""
        for binding in bindings:
            account_key = str(binding.get("account_key") or "").strip()
            email = str(binding.get("email") or "").strip()
            if not account_key or not email or account_key in bound:
                continue
            code = _new_code()
            digest = code_hash(code)
            while digest in known_hashes:
                code = _new_code()
                digest = code_hash(code)
            record = {
                "id": f"cdk_{uuid.uuid4().hex[:16]}",
                "code": code,
                "code_hash": digest,
                "code_hint": _hint(code),
                "account_key": account_key,
                "account_id": str(binding.get("account_id") or ""),
                "email": email,
                "provider_type": str(binding.get("provider_type") or "auto"),
                "group_id": str(binding.get("group_id") or ""),
                "group_name": str(binding.get("group_name") or ""),
                "status": "available",
                "created_at": _iso(now),
                "updated_at": _iso(now),
                "redeemed_at": "",
                "last_access_at": "",
                "expires_at": expires_at,
            }
            rows.append(record)
            bound.add(account_key)
            known_hashes.add(digest)
            created.append(dict(record))
        if created:
            _write(path, rows)
        return created


def replace_available_bindings(
    invalid_accounts: list[dict[str, Any]],
    replacements: list[dict[str, Any]],
    path: Path = CDKS_FILE,
) -> dict[str, Any]:
    """Replace invalid accounts bound to unused CDKs without changing the codes."""
    invalid_keys = {
        str(item.get("account_key") or "").strip()
        for item in invalid_accounts
        if str(item.get("account_key") or "").strip()
    }
    invalid_emails = {
        str(item.get("email") or "").strip().lower()
        for item in invalid_accounts
        if str(item.get("email") or "").strip()
    }
    invalid_account_ids = {
        str(item.get("account_id") or "").strip().lower()
        for item in invalid_accounts
        if str(item.get("account_id") or "").strip()
    }
    if not invalid_keys and not invalid_emails and not invalid_account_ids:
        return {"replaced": 0, "unresolved": 0, "items": []}

    def is_invalid(record: dict[str, Any]) -> bool:
        return (
            str(record.get("account_key") or "").strip() in invalid_keys
            or str(record.get("email") or "").strip().lower() in invalid_emails
            or str(record.get("account_id") or "").strip().lower() in invalid_account_ids
        )

    with _LOCK:
        rows = _read(path)
        targets = [
            item
            for item in rows
            if str(item.get("status") or "available") == "available"
            and not _expired(item)
            and is_invalid(item)
        ]
        if not targets:
            return {"replaced": 0, "unresolved": 0, "items": []}

        bound_keys = {
            str(item.get("account_key") or "").strip()
            for item in rows
            if str(item.get("account_key") or "").strip()
        }
        bound_emails = {
            str(item.get("email") or "").strip().lower()
            for item in rows
            if str(item.get("email") or "").strip()
        }
        bound_account_ids = {
            str(item.get("account_id") or "").strip().lower()
            for item in rows
            if str(item.get("account_id") or "").strip()
        }
        candidate_keys: set[str] = set()
        candidate_emails: set[str] = set()
        candidate_account_ids: set[str] = set()
        pools: dict[str, list[dict[str, Any]]] = {}
        for candidate in replacements:
            key = str(candidate.get("account_key") or "").strip()
            email = str(candidate.get("email") or "").strip()
            email_l = email.lower()
            account_id = str(candidate.get("account_id") or "").strip().lower()
            group_id = str(candidate.get("group_id") or "").strip()
            if (
                not key
                or not email
                or key in bound_keys
                or email_l in bound_emails
                or (account_id and account_id in bound_account_ids)
                or key in candidate_keys
                or email_l in candidate_emails
                or (account_id and account_id in candidate_account_ids)
            ):
                continue
            pools.setdefault(group_id, []).append(candidate)
            candidate_keys.add(key)
            candidate_emails.add(email_l)
            if account_id:
                candidate_account_ids.add(account_id)

        changed: list[dict[str, Any]] = []
        now = _iso()
        for record in targets:
            group_id = str(record.get("group_id") or "").strip()
            pool = pools.get(group_id) or []
            if not pool:
                continue
            candidate = pool.pop(0)
            old_email = str(record.get("email") or "")
            new_key = str(candidate.get("account_key") or "").strip()
            record.update(
                account_key=new_key,
                account_id=str(candidate.get("account_id") or ""),
                email=str(candidate.get("email") or ""),
                provider_type=str(candidate.get("provider_type") or "auto"),
                group_id=group_id,
                group_name=str(candidate.get("group_name") or record.get("group_name") or ""),
                updated_at=now,
                last_replaced_at=now,
                last_replaced_from_email=old_email,
                replacement_count=max(0, int(record.get("replacement_count") or 0)) + 1,
            )
            bound_keys.add(new_key)
            bound_emails.add(str(record.get("email") or "").strip().lower())
            if str(record.get("account_id") or "").strip():
                bound_account_ids.add(str(record.get("account_id") or "").strip().lower())
            changed.append(
                {
                    "id": str(record.get("id") or ""),
                    "email": str(record.get("email") or ""),
                    "replaced_from_email": old_email,
                }
            )
        if changed:
            _write(path, rows)
        return {
            "replaced": len(changed),
            "unresolved": len(targets) - len(changed),
            "items": changed,
        }


def redeem_cdk(
    code: str,
    path: Path = CDKS_FILE,
    *,
    allow_redeemed: bool = True,
) -> dict[str, Any]:
    digest = code_hash(code)
    if len(normalize_code(code)) < 20:
        raise KeyError("CDK 无效")
    with _LOCK:
        rows = _read(path)
        record = next(
            (item for item in rows if secrets.compare_digest(str(item.get("code_hash") or ""), digest)),
            None,
        )
        if record is None:
            raise KeyError("CDK 无效")
        if str(record.get("status") or "") == "disabled":
            raise PermissionError("CDK 已停用")
        if _expired(record):
            raise PermissionError("CDK 已过期")
        if str(record.get("status") or "") == "redeemed" and not allow_redeemed:
            raise FileExistsError("CDK 已兑换")
        now = _iso()
        if str(record.get("status") or "available") == "available":
            record["status"] = "redeemed"
            record["redeemed_at"] = now
        record["last_access_at"] = now
        record["updated_at"] = now
        _write(path, rows)
        return dict(record)


def get_cdk(cdk_id: str, path: Path = CDKS_FILE, *, touch: bool = False) -> dict[str, Any] | None:
    target = str(cdk_id or "").strip()
    with _LOCK:
        rows = _read(path)
        record = next((item for item in rows if str(item.get("id") or "") == target), None)
        if record is None or str(record.get("status") or "") == "disabled" or _expired(record):
            return None
        if touch:
            record["last_access_at"] = _iso()
            record["updated_at"] = record["last_access_at"]
            _write(path, rows)
        return dict(record)


def set_cdk_status(
    ids: list[str], status: str, path: Path = CDKS_FILE
) -> tuple[int, list[dict[str, Any]]]:
    target_ids = {str(value or "").strip() for value in ids if str(value or "").strip()}
    if status not in {"available", "disabled", "reset"}:
        raise ValueError("不支持的 CDK 状态")
    changed = 0
    with _LOCK:
        rows = _read(path)
        for record in rows:
            if str(record.get("id") or "") not in target_ids:
                continue
            if status == "reset":
                needs_reset = (
                    str(record.get("status") or "available") != "available"
                    or bool(str(record.get("redeemed_at") or "").strip())
                    or bool(str(record.get("last_access_at") or "").strip())
                )
                if needs_reset:
                    record["status"] = "available"
                    record["redeemed_at"] = ""
                    record["last_access_at"] = ""
                    record["updated_at"] = _iso()
                    changed += 1
                continue
            if str(record.get("status") or "") != status:
                record["status"] = status
                record["updated_at"] = _iso()
                changed += 1
        if changed:
            _write(path, rows)
        return changed, [dict(item) for item in rows]


def cdk_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    result = {"total": len(rows), "available": 0, "redeemed": 0, "disabled": 0, "expired": 0}
    for item in rows:
        if _expired(item):
            result["expired"] += 1
            continue
        status = str(item.get("status") or "available")
        if status in result:
            result[status] += 1
    return result

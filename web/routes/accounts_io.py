"""Account IO routes: credential export, download and deletion."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from web.routes.shared_models import BulkDeleteRequest

router = APIRouter()
router_bulk = APIRouter()


def _state() -> Any:
    # Delayed import is intentional: web_app includes these routers.
    import web_app

    return web_app


class AccountEmailExportRequest(BaseModel):
    """Email filters used by the credential export workspace."""

    emails: list[str] = Field(default_factory=list, max_length=5000)
    text: str = Field(default="", max_length=2_000_000)


def _security_credential_lines(records: list[dict[str, Any]]) -> tuple[list[str], int]:
    lines: list[str] = []
    missing = 0
    for account in records:
        email = str(account.get("email") or "").strip()
        password = str(account.get("password") or "").strip()
        totp_secret = str(account.get("totp_secret") or "").strip()
        if not email or not password or not totp_secret:
            missing += 1
            continue
        lines.append("----".join((email, password, totp_secret)))
    return lines, missing


def _normalize_credential_export_emails(body: AccountEmailExportRequest) -> list[str]:
    values = [str(value or "") for value in body.emails]
    if body.text:
        values.append(str(body.text))
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        for candidate in re.split(r"[\s,，;；]+", value):
            email = candidate.strip().strip("`'\"").lower()
            if not email or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
                continue
            if email not in seen:
                seen.add(email)
                result.append(email)
    return result


@router.post("/api/accounts/export-security-by-email")
async def accounts_export_security_by_email(body: AccountEmailExportRequest):
    """Download credentials for accounts matched by an explicit email list."""
    requested = _normalize_credential_export_emails(body)
    if not requested:
        raise HTTPException(400, "请输入至少一个有效邮箱")
    # Public rows contain the stable delete key without decoding the large
    # session/cookie payload. Decode full records only for requested emails.
    public_rows = await asyncio.to_thread(_state().load_account_public_views, None, _state().ACCOUNTS_FILE)
    key_by_email = {
        str(record.get("email") or "").strip().lower(): str(record.get("delete_key") or "")
        for record in public_rows
        if str(record.get("email") or "").strip()
        and str(record.get("delete_key") or "").strip()
    }
    requested_keys = [key_by_email[email] for email in requested if email in key_by_email]
    records_by_key = await asyncio.to_thread(_state()._records_by_keys_with_fallback, requested_keys)
    legacy_by_email: dict[str, dict[str, Any]] = {}
    # An unknown address alongside valid matches is already represented by the
    # missing count; do not re-read the full store just for that address.
    compatibility_loader = _state()._load_accounts is not _state()._DEFAULT_LOAD_ACCOUNTS
    legacy_store = (
        Path(_state().ACCOUNTS_FILE).resolve() != (_state().ROOT / "data" / "registered_accounts.json").resolve()
        or not Path(_state().ACCOUNTS_DB_FILE).is_file()
    )
    if not records_by_key and (compatibility_loader or legacy_store):
        legacy_records = await asyncio.to_thread(_state()._load_accounts)
        legacy_by_email = {
            str(record.get("email") or "").strip().lower(): record
            for record in legacy_records
            if str(record.get("email") or "").strip()
        }
    selected = []
    for email in requested:
        key = key_by_email.get(email)
        record = records_by_key.get(key) if key else None
        if record is None:
            record = legacy_by_email.get(email)
        if record is not None:
            selected.append(record)
    lines, missing = _security_credential_lines(selected)
    missing += len(requested) - len(selected)
    payload = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    headers = {
        "Content-Disposition": 'attachment; filename="accounts.security.by-email.txt"',
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "X-Requested-Count": str(len(requested)),
        "X-Matched-Count": str(len(selected)),
        "X-Exported-Count": str(len(lines)),
        "X-Missing-Count": str(missing),
    }
    return StreamingResponse(
        iter([payload]),
        media_type="text/plain; charset=utf-8",
        headers=headers,
    )


@router_bulk.post("/api/accounts/delete")
async def accounts_delete(body: BulkDeleteRequest):
    wanted = {str(key or "").strip() for key in body.keys if str(key or "").strip()}
    if body.delete_all:
        removed_records = await asyncio.to_thread(_state()._load_accounts)
    elif wanted:
        records_by_key = await asyncio.to_thread(
            _state()._records_by_keys_with_fallback,
            wanted,
        )
        removed_records = [
            records_by_key[key]
            for key in dict.fromkeys(
                str(raw_key or "").strip() for raw_key in body.keys
            )
            if key in records_by_key
        ]
    else:
        removed_records = []
    await asyncio.to_thread(_state().archive_accounts, removed_records, reason="manual")
    removed, _ = await asyncio.to_thread(
        _state().delete_account_records,
        body.keys,
        delete_all=body.delete_all,
        include_records=False,
        path=_state().ACCOUNTS_FILE,
    )
    return {
        "ok": True,
        "removed": removed,
        "count": await asyncio.to_thread(_state().count_accounts, _state().ACCOUNTS_FILE),
    }


@router_bulk.post("/api/accounts/delete-invalid")
async def accounts_delete_invalid(body: BulkDeleteRequest):
    records = await asyncio.to_thread(_state()._load_accounts)
    wanted = {str(key or "").strip() for key in body.keys if str(key or "").strip()}
    if not body.delete_all and not wanted:
        raise HTTPException(400, "请选择要删除的失效账号")
    keys = [
        _state()._account_key(item)
        for item in records
        if _state().normalized_health_status(item) == "invalid"
        and not _state().account_is_deactivated(item)
        and (body.delete_all or _state()._account_key(item) in wanted)
    ]
    if not keys:
        return {"ok": True, "removed": 0, "count": len(records)}
    await asyncio.to_thread(
        _state().archive_accounts,
        [item for item in records if _state()._account_key(item) in set(keys)],
        reason="invalid",
    )
    removed, _ = await asyncio.to_thread(
        _state().delete_account_records,
        keys,
        include_records=False,
        path=_state().ACCOUNTS_FILE,
    )
    return {
        "ok": True,
        "removed": removed,
        "count": await asyncio.to_thread(_state().count_accounts, _state().ACCOUNTS_FILE),
    }


@router_bulk.post("/api/accounts/download")
async def accounts_download(body: BulkDeleteRequest):
    wanted = {str(k or "").strip() for k in body.keys if str(k or "").strip()}
    if body.delete_all:
        records = await asyncio.to_thread(_state()._load_accounts)
    elif wanted:
        records_by_key = await asyncio.to_thread(_state()._records_by_keys_with_fallback, wanted)
        records = []
        for raw_key in body.keys:
            key = str(raw_key or "").strip()
            if key in records_by_key:
                records.append(records_by_key[key])
    else:
        records = []
    payload = await asyncio.to_thread(
        lambda: (json.dumps(records, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    headers = {
        "Content-Disposition": 'attachment; filename="registered_accounts.selected.json"'
    }
    return StreamingResponse(
        iter([payload]), media_type="application/json", headers=headers
    )


@router_bulk.post("/api/accounts/export-security")
async def accounts_export_security(body: BulkDeleteRequest):
    """Export selected account email, password, and TOTP secret records."""
    wanted = list(dict.fromkeys(str(key or "").strip() for key in body.keys))
    wanted = [key for key in wanted if key]
    if not wanted:
        raise HTTPException(400, "请选择要导出的账号")
    records_by_key = await asyncio.to_thread(_state()._records_by_keys_with_fallback, wanted)
    selected = [records_by_key[key] for key in wanted if key in records_by_key]
    lines, missing = _security_credential_lines(selected)
    payload = await asyncio.to_thread(
        lambda: ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    )
    headers = {
        "Content-Disposition": 'attachment; filename="accounts.security.selected.txt"',
        "X-Exported-Count": str(len(lines)),
        "X-Missing-Count": str(missing + len(wanted) - len(selected)),
    }
    return StreamingResponse(
        iter([payload]), media_type="text/plain; charset=utf-8", headers=headers
    )


@router_bulk.get("/api/accounts/download-all")
async def accounts_download_all():
    records = await asyncio.to_thread(_state()._load_accounts)
    payload = await asyncio.to_thread(
        lambda: (json.dumps(records, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    headers = {
        "Content-Disposition": 'attachment; filename="registered_accounts.all.json"'
    }
    return StreamingResponse(
        iter([payload]), media_type="application/json", headers=headers
    )

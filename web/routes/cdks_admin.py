"""CDK admin panel: bindings, generation and status routes."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()


def _state() -> Any:
    # Delayed import is intentional: web_app includes this router.
    import web_app

    return web_app


class CDKGenerateRequest(BaseModel):
    group_id: str = ""
    count: int = Field(1, ge=1, le=500)
    expires_days: int = Field(0, ge=0, le=3650)
    account_key: str = Field(default="", max_length=256)


class CDKStatusRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)
    status: str


def _account_cdk_security_ready(record: dict[str, Any]) -> bool:
    """Only expose accounts with a usable password-backed TOTP setup to CDK."""
    if _state().account_is_deactivated(record):
        return False
    password = str(record.get("password") or "").strip()
    totp_secret = str(record.get("totp_secret") or "").strip()
    mfa_enabled = bool(record.get("mfa_enabled")) or bool(totp_secret)
    return bool(password and totp_secret and mfa_enabled)


def _cdk_account_marks() -> dict[str, dict[str, Any]]:
    """Index CDK binding/redeem state by every stable account identity."""
    marks: dict[str, dict[str, Any]] = {}
    priority = {"redeemed": 3, "available": 2, "disabled": 1}
    for record in _state().list_cdks(_state().CDKS_FILE):
        status = str(record.get("status") or "available").strip().lower()
        mark = {
            "cdk_bound": True,
            "cdk_status": status,
            "cdk_redeemed": status == "redeemed",
            "cdk_redeemed_at": str(record.get("redeemed_at") or ""),
            "cdk_last_access_at": str(record.get("last_access_at") or ""),
        }
        aliases = {
            str(record.get("account_key") or "").strip(),
            f"email:{str(record.get('email') or '').strip().lower()}" if str(record.get("email") or "").strip() else "",
            f"account_id:{str(record.get('account_id') or '').strip().lower()}" if str(record.get("account_id") or "").strip() else "",
        }
        for alias in aliases:
            if not alias:
                continue
            current = marks.get(alias)
            if current is None or priority.get(status, 0) >= priority.get(str(current.get("cdk_status") or ""), 0):
                marks[alias] = dict(mark)
    return marks


def _eligible_cdk_bindings(group_id: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    group = _state().get_group(group_id, _state().GROUPS_FILE)
    if group is None:
        raise KeyError("分组不存在")
    provider_map = _state()._mailbox_provider_map(_state()._read_config())
    existing = _state().list_cdks(_state().CDKS_FILE)
    bound_keys = {str(item.get("account_key") or "") for item in existing}
    bindings: list[dict[str, Any]] = []
    stats = {
        "group_accounts": 0,
        "without_mailbox": 0,
        "invalid": 0,
        "already_bound": 0,
        "security_incomplete": 0,
    }
    for account in _state()._load_accounts():
        if str(account.get("group_id") or "").strip() != group_id:
            continue
        stats["group_accounts"] += 1
        email = str(account.get("email") or "").strip()
        email_l = email.lower()
        provider_type = provider_map.get(email_l, str(account.get("provider_type") or "").strip())
        if not email or not provider_type:
            stats["without_mailbox"] += 1
            continue
        if _state().normalized_health_status(account) == "invalid":
            stats["invalid"] += 1
            continue
        account_key = _state()._account_key(account) or f"email:{email_l}"
        if account_key in bound_keys:
            stats["already_bound"] += 1
            continue
        if not _account_cdk_security_ready(account):
            stats["security_incomplete"] += 1
            continue
        bindings.append(
            {
                "account_key": account_key,
                "account_id": str(account.get("account_id") or ""),
                "email": email,
                "provider_type": provider_type,
                "group_id": group_id,
                "group_name": str(group.get("name") or ""),
                "registered_at": str(account.get("registered_at") or ""),
            }
        )
    bindings.sort(key=lambda item: (item.get("registered_at") or "", item["email"]))
    stats["eligible"] = len(bindings)
    return bindings, stats


def _replace_invalid_unredeemed_cdks(
    checked_records: list[dict[str, Any]],
    *,
    accounts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    # Health callers already hold a complete account snapshot. Reusing it
    # avoids decoding the full payload store a second time at the end of a
    # large run.
    accounts = accounts if accounts is not None else _state()._load_accounts()
    account_keys = {_state()._account_key(item) for item in accounts if _state()._account_key(item)}
    account_emails = {
        str(item.get("email") or "").strip().lower()
        for item in accounts
        if str(item.get("email") or "").strip()
    }
    account_ids = {
        str(item.get("account_id") or "").strip().lower()
        for item in accounts
        if str(item.get("account_id") or "").strip()
    }
    invalid_accounts: list[dict[str, Any]] = []
    invalid_identities: set[tuple[str, str, str]] = set()

    def add_invalid(account: dict[str, Any]) -> None:
        item = {
            "account_key": str(account.get("account_key") or _state()._account_key(account) or ""),
            "account_id": str(account.get("account_id") or ""),
            "email": str(account.get("email") or ""),
        }
        identity = (
            item["account_key"].strip(),
            item["account_id"].strip().lower(),
            item["email"].strip().lower(),
        )
        if identity not in invalid_identities:
            invalid_identities.add(identity)
            invalid_accounts.append(item)

    for account in [*accounts, *checked_records]:
        if _state().normalized_health_status(account) == "invalid":
            add_invalid(account)

    # Keep unresolved CDKs repairable after their invalid account was archived.
    for record in _state().list_cdks(_state().CDKS_FILE):
        key = str(record.get("account_key") or "").strip()
        email = str(record.get("email") or "").strip().lower()
        account_id = str(record.get("account_id") or "").strip().lower()
        exists = (
            (key and key in account_keys)
            or (email and email in account_emails)
            or (account_id and account_id in account_ids)
        )
        if not exists:
            add_invalid(record)

    if not invalid_accounts:
        return {"replaced": 0, "unresolved": 0, "items": []}

    provider_map = _state()._mailbox_provider_map(_state()._read_config())
    group_names = {item["id"]: item["name"] for item in _state()._groups_with_counts()}
    candidates: list[dict[str, Any]] = []
    for account in accounts:
        health = _state().normalized_health_status(account)
        if health not in {"alive", "limited"}:
            continue
        if not _account_cdk_security_ready(account):
            continue
        email = str(account.get("email") or "").strip()
        provider_type = provider_map.get(email.lower(), str(account.get("provider_type") or "").strip())
        group_id = str(account.get("group_id") or "").strip()
        if not email or not provider_type or not group_id:
            continue
        candidates.append(
            {
                "account_key": _state()._account_key(account) or f"email:{email.lower()}",
                "account_id": str(account.get("account_id") or ""),
                "email": email,
                "provider_type": provider_type,
                "group_id": group_id,
                "group_name": group_names.get(group_id, ""),
                "health_status": health,
                "registered_at": str(account.get("registered_at") or ""),
            }
        )
    candidates.sort(
        key=lambda item: (
            0 if item["health_status"] == "alive" else 1,
            item["registered_at"],
            item["email"].lower(),
        )
    )
    return _state().replace_available_bindings(invalid_accounts, candidates, _state().CDKS_FILE)


def _merge_health_snapshot(
    accounts: list[dict[str, Any]],
    checked_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Overlay this run's health results without decoding the store again."""
    if not checked_records:
        return accounts
    return _state().normalize_accounts([*accounts, *checked_records])


def _replace_cdks_after_health(
    accounts: list[dict[str, Any]],
    checked_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Merge health results and repair bindings in one worker-thread call."""
    refreshed_records = _merge_health_snapshot(accounts, checked_records)
    return _replace_invalid_unredeemed_cdks(
        checked_records,
        accounts=refreshed_records,
    )


def _cdk_admin_row(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: record.get(key)
        for key in (
            "id",
            "code",
            "code_hint",
            "email",
            "account_id",
            "provider_type",
            "group_id",
            "group_name",
            "status",
            "created_at",
            "updated_at",
            "redeemed_at",
            "last_access_at",
            "expires_at",
            "replacement_count",
            "last_replaced_at",
        )
    }


def _cdk_admin_payload(group_id: str = "") -> dict[str, Any]:
    rows = _state().ensure_plain_codes(_state().CDKS_FILE)
    groups = _state()._groups_with_counts()
    selected = str(group_id or "").strip()
    eligible: dict[str, Any] = {}
    if selected:
        try:
            _bindings, eligible = _eligible_cdk_bindings(selected)
        except KeyError:
            eligible = {}
    items = [_cdk_admin_row(item) for item in rows]
    items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {
        "ok": True,
        "items": items,
        "summary": _state().cdk_summary(rows),
        "groups": groups,
        "eligibility": eligible,
    }


@router.get("/api/cdks")
async def cdks(group_id: str = ""):
    return await asyncio.to_thread(_cdk_admin_payload, group_id)


@router.post("/api/cdks/generate")
async def cdks_generate(body: CDKGenerateRequest):
    account_key = str(body.account_key or "").strip()
    group_id = str(body.group_id or "").strip()
    if account_key:
        account = await asyncio.to_thread(
            lambda: _state()._records_by_keys_with_fallback([account_key]).get(account_key)
        )
        if account is None:
            raise HTTPException(404, "指定账号不存在")
        group_id = str(account.get("group_id") or "").strip()
        if not group_id:
            raise HTTPException(400, "指定账号没有有效分组")
    if not group_id:
        raise HTTPException(400, "请选择账号分组")
    try:
        bindings, eligibility = await asyncio.to_thread(
            _eligible_cdk_bindings, group_id
        )
    except KeyError as error:
        raise HTTPException(404, str(error).strip("'")) from error
    selected = (
        [item for item in bindings if str(item.get("account_key") or "") == account_key]
        if account_key
        else bindings[: body.count]
    )
    if not selected:
        message = "指定账号无法生成 CDK：可能未在邮箱池、已失效或已绑定 CDK" if account_key else "该分组没有可生成 CDK 的账号"
        raise HTTPException(400, message)
    created = await asyncio.to_thread(
        _state().create_cdks,
        selected,
        expires_days=body.expires_days,
        path=_state().CDKS_FILE,
    )
    if not created:
        raise HTTPException(409, "所选账号已绑定 CDK，请刷新后重试")
    codes = [
        {
            "id": str(item.get("id") or ""),
            "code": str(item.get("code") or ""),
            "email": str(item.get("email") or ""),
            "expires_at": str(item.get("expires_at") or ""),
        }
        for item in created
    ]
    for item in created:
        _state().record_operation_event(
            {
                "email": item.get("email"),
                "account_id": item.get("account_id"),
            },
            "cdk_generate",
            "success",
            message="CDK 生成成功",
            metadata={
                "cdk_id": str(item.get("id") or ""),
                "group_id": str(item.get("group_id") or group_id),
                "expires_at": str(item.get("expires_at") or ""),
            },
        )
    return {
        "ok": True,
        "created": len(codes),
        "codes": codes,
        "eligibility": eligibility,
        "group_id": group_id,
        "message": f"已为指定账号生成 CDK" if account_key else f"已生成 {len(codes)} 个 CDK",
    }


@router.post("/api/cdks/status")
async def cdks_status(body: CDKStatusRequest):
    try:
        changed, rows = await asyncio.to_thread(
            _state().set_cdk_status, body.ids, body.status, _state().CDKS_FILE
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    wanted_ids = {str(value or "").strip() for value in body.ids if str(value or "").strip()}
    for item in rows:
        if str(item.get("id") or "") not in wanted_ids:
            continue
        _state().record_operation_event(
            {
                "email": item.get("email"),
                "account_id": item.get("account_id"),
            },
            "cdk_status",
            "success",
            message="CDK 状态已更新",
            metadata={
                "cdk_id": str(item.get("id") or ""),
                "status": str(item.get("status") or body.status),
            },
        )
    return {
        "ok": True,
        "changed": changed,
        "summary": _state().cdk_summary(rows),
    }

"""Account reauthentication & security operations (重登/安全操作) routes.

Request models, batch helpers and handlers moved from web_app.py. Runtime
state and shared helpers resolve lazily through web_app to avoid import
cycles with the application bootstrap.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from web.routes.shared_models import AccountTokenRequest


router = APIRouter()


def _state() -> Any:
    # Delayed import is intentional: web_app includes this router.
    import web_app

    return web_app


class AccountReauthRequest(BaseModel):
    key: str = Field(min_length=1, max_length=256)
    mode: str = Field(default="protocol", min_length=1, max_length=16)

class AccountReauthBatchRequest(BaseModel):
    keys: list[str] = Field(min_length=1)
    mode: str = Field(default="protocol", min_length=1, max_length=16)
    concurrency: int = Field(default=3, ge=1)

class AccountPasswordChangeBatchRequest(BaseModel):
    """Batch request for changing the password of existing accounts.

    ``password_mode=random`` generates a separate 16-character password for
    each account. ``custom`` requires a password of at least 12 characters.
    ``password`` is accepted as an input alias for older panel builds.  The
    value is consumed by the worker and is deliberately absent from all job
    snapshots and response payloads.
    """

    keys: list[str] = Field(min_length=1)
    password_mode: str = Field(default="custom", max_length=16)
    new_password: str = Field(default="", max_length=512)
    password: str = Field(default="", max_length=512)
    confirm_password: str = Field(default="", max_length=512)
    concurrency: int = Field(default=3, ge=1)

class AccountTotpChangeBatchRequest(BaseModel):
    """Batch request for replacing an account's TOTP factor."""

    keys: list[str] = Field(min_length=1)
    concurrency: int = Field(default=3, ge=1)

class AccountSecurityOperationRequest(AccountReauthBatchRequest):
    """Generic account security operation request used by API clients."""

    operation: str = Field(default="security_repair", min_length=1, max_length=32)
    password_mode: str = Field(default="custom", max_length=16)
    new_password: str = Field(default="", max_length=512)
    password: str = Field(default="", max_length=512)
    confirm_password: str = Field(default="", max_length=512)
    force_password_confirmation: bool = False

class AccountPasswordChangeRequest(BaseModel):
    key: str = Field(min_length=1, max_length=256)
    password_mode: str = Field(default="custom", max_length=16)
    new_password: str = Field(default="", max_length=512)
    password: str = Field(default="", max_length=512)
    confirm_password: str = Field(default="", max_length=512)

class AccountTotpChangeRequest(BaseModel):
    key: str = Field(min_length=1, max_length=256)

class AccountEmailReauthRequest(BaseModel):
    """Start protocol reauthentication for a pasted list of email addresses."""

    emails: list[str] = Field(default_factory=list, max_length=5000)
    text: str = Field(default="", max_length=2_000_000)
    concurrency: int = Field(default=3, ge=1, le=20)


@router.post("/api/accounts/reauth/start")
async def account_reauthenticate_start(body: AccountReauthBatchRequest):
    try:
        reauth_mode = _state()._normalize_reauth_mode(body.mode)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    requested = list(dict.fromkeys(str(key or "").strip() for key in body.keys))
    requested = [key for key in requested if key]
    if not requested:
        raise HTTPException(400, "没有选择账号")
    records = await asyncio.to_thread(_state()._load_accounts)
    record_map = {_state()._account_key(item): item for item in records if _state()._account_key(item)}
    selected: list[dict[str, Any]] = []
    for key in requested:
        account = record_map.get(key)
        if account is None:
            raise HTTPException(404, "所选账号中存在已删除记录，请刷新列表后重试")
        if _state().account_public_view(account).get("is_deactivated"):
            raise HTTPException(409, f"{account.get('email') or key} 已封号，不支持重新登录")
        account_protocol = _state()._account_requires_protocol_reauth(account)
        if (reauth_mode == "protocol" or account_protocol) and not _state()._protocol_reauth_credentials_ready(account):
            raise HTTPException(
                409,
                f"{account.get('email') or key} 此账号必须使用协议重新登录，且需要同时保存密码和 TOTP 2FA"
                if account_protocol
                else f"{account.get('email') or key} 协议重新登录要求同时保存密码和 TOTP 2FA",
            )
        selected.append(account)
    if any(_state()._account_requires_protocol_reauth(account) for account in selected):
        reauth_mode = "protocol"
    try:
        job = await asyncio.to_thread(
            _state().ACCOUNT_REAUTH_JOBS.start,
            selected,
            reauth_mode,
            "reauth",
            body.concurrency,
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    return {"ok": True, **job}


@router.post("/api/accounts/reauth-by-email/start")
async def account_reauthenticate_by_email_start(body: AccountEmailReauthRequest):
    raw_values = [*body.emails, *re.split(r"[\s,，;；]+", body.text or "")]
    requested: list[str] = []
    seen: set[str] = set()
    for value in raw_values:
        email = str(value or "").strip().strip("'\"`").lower()
        if not email or email in seen:
            continue
        seen.add(email)
        requested.append(email)
    if not requested:
        raise HTTPException(400, "没有识别到有效邮箱")
    invalid_format = [email for email in requested if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)]
    requested = [email for email in requested if email not in invalid_format]
    if not requested:
        raise HTTPException(400, "没有识别到有效邮箱")
    records = await asyncio.to_thread(_state()._load_accounts)
    record_map = {
        str(item.get("email") or "").strip().lower(): item
        for item in records
        if str(item.get("email") or "").strip()
    }
    selected: list[dict[str, Any]] = []
    not_found: list[str] = []
    missing_credentials: list[dict[str, Any]] = []
    for email in requested:
        account = record_map.get(email)
        if account is None:
            not_found.append(email)
            continue
        missing: list[str] = []
        if not str(account.get("password") or "").strip():
            missing.append("password")
        if not str(account.get("totp_secret") or "").strip():
            missing.append("totp")
        if missing:
            missing_credentials.append({"email": email, "missing": missing})
            continue
        selected.append(account)
    if not selected:
        return {
            "ok": True,
            "job_id": "",
            "status": "preflight",
            "running": False,
            "stopping": False,
            "mode": "protocol",
            "operation": "reauth",
            "concurrency": 0,
            "total": 0,
            "done": 0,
            "success": 0,
            "failed": 0,
            "cancelled": 0,
            "items": [],
            "message": "预检完成：没有具备密码和 TOTP 2FA 的可登录账号",
            "requested": len(requested),
            "matched": len(requested) - len(not_found),
            "submitted": 0,
            "invalid_format": invalid_format,
            "not_found": not_found,
            "missing_credentials": missing_credentials,
        }
    try:
        job = await asyncio.to_thread(
            _state().ACCOUNT_REAUTH_JOBS.start,
            selected,
            "protocol",
            "reauth",
            body.concurrency,
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    return {
        "ok": True,
        **job,
        "requested": len(requested),
        "matched": len(requested) - len(not_found),
        "submitted": len(selected),
        "invalid_format": invalid_format,
        "not_found": not_found,
        "missing_credentials": missing_credentials,
    }


@router.get("/api/accounts/reauth/progress")
async def account_reauthenticate_progress():
    job = await asyncio.to_thread(_state().ACCOUNT_REAUTH_JOBS.active_snapshot)
    return {"ok": True, "job": job}


@router.post("/api/accounts/security-repair/start")
async def account_security_repair_start(body: AccountReauthBatchRequest):
    requested = list(dict.fromkeys(str(key or "").strip() for key in body.keys))
    requested = [key for key in requested if key]
    if not requested:
        raise HTTPException(400, "没有选择账号")
    records = await asyncio.to_thread(_state()._load_accounts)
    record_map = {_state()._account_key(item): item for item in records if _state()._account_key(item)}
    selected: list[dict[str, Any]] = []
    for key in requested:
        account = record_map.get(key)
        if account is None:
            raise HTTPException(404, "所选账号中存在已删除记录，请刷新列表后重试")
        if not _state().account_public_view(account).get("security_repair_available"):
            raise HTTPException(409, f"{account.get('email') or key} 没有缺失的安全字段")
        selected.append(account)
    try:
        job = await asyncio.to_thread(
            _state().ACCOUNT_REAUTH_JOBS.start,
            selected,
            "protocol",
            "security_repair",
            body.concurrency,
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    return {"ok": True, **job}


@router.post("/api/accounts/security-repair")
async def account_security_repair(body: AccountTokenRequest):
    target = str(body.key or "").strip()
    selected = await _load_security_accounts([target], operation="security_repair")
    try:
        saved = await asyncio.to_thread(_state()._repair_account_security_sync, selected[0])
    except Exception as error:
        # Keep protocol diagnostics useful to the caller without ever echoing
        # a stored password/TOTP value returned by a provider error.
        raise HTTPException(
            409,
            _state()._redact_security_detail(error, selected[0]),
        ) from error
    return {
        "ok": True,
        "message": f"{saved.get('email')} 安全字段补全成功",
        "item": _state().account_public_view(saved),
    }


def _security_batch_password(body: Any) -> tuple[str, str]:
    """Normalize the two accepted password field names without echoing them."""
    try:
        password_mode = _state()._normalize_security_password_mode(
            getattr(body, "password_mode", "custom")
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    raw_new_password = str(getattr(body, "new_password", "") or "").strip()
    raw_password_alias = str(getattr(body, "password", "") or "").strip()
    if raw_new_password and raw_password_alias and raw_new_password != raw_password_alias:
        raise HTTPException(400, "新密码字段不一致")
    new_password = raw_new_password or raw_password_alias
    confirmation = str(getattr(body, "confirm_password", "") or "").strip()
    if password_mode == "random":
        if new_password or confirmation:
            raise HTTPException(400, "随机生成模式无需填写新密码")
        return "", ""
    if not new_password:
        raise HTTPException(400, "请填写新密码")
    if confirmation and confirmation != new_password:
        raise HTTPException(400, "两次输入的新密码不一致")
    if len(new_password) < _state()._SECURITY_PASSWORD_MIN_LENGTH:
        raise HTTPException(
            400,
            f"新密码至少需要 {_state()._SECURITY_PASSWORD_MIN_LENGTH} 个字符",
        )
    return new_password, confirmation


async def _load_security_accounts(keys: list[str], *, operation: str) -> list[dict[str, Any]]:
    requested = list(dict.fromkeys(str(key or "").strip() for key in keys if str(key or "").strip()))
    if not requested:
        raise HTTPException(400, "没有选择账号")
    record_map = await asyncio.to_thread(
        _state()._records_by_keys_with_fallback,
        requested,
    )
    selected: list[dict[str, Any]] = []
    for key in requested:
        account = record_map.get(key)
        if account is None:
            raise HTTPException(404, "所选账号中存在已删除记录，请刷新列表后重试")
        email = str(account.get("email") or key)
        public = _state().account_public_view(account)
        if public.get("is_deactivated"):
            raise HTTPException(409, f"{email} 已封号，不能执行安全字段操作")
        if operation in {"password_change", "totp_change"}:
            missing: list[str] = []
            if not str(account.get("password") or "").strip():
                missing.append("密码")
            elif account.get("password_set") is False:
                missing.append("已确认密码")
            if not str(account.get("totp_secret") or "").strip():
                missing.append("TOTP 2FA")
            if missing:
                missing_text = "、".join(missing)
                raise HTTPException(
                    409,
                    f"{email} 缺少{missing_text}，请先补齐安全字段",
                )
        elif operation == "security_repair" and not public.get(
            "security_repair_available"
        ):
            raise HTTPException(409, f"{email} 没有缺失的安全字段")
        elif operation == "reauth":
            if not public.get("reauth_available"):
                raise HTTPException(409, f"{email} 当前不支持重新登录")
            if not _state()._protocol_reauth_credentials_ready(account):
                raise HTTPException(
                    409,
                    f"{email} 协议重新登录要求同时保存密码和 TOTP 2FA",
                )
        selected.append(account)
    return selected


async def _start_security_batch(
    keys: list[str],
    *,
    operation: str,
    concurrency: int,
    new_password: str = "",
    password_mode: str = "custom",
    force_password_confirmation: bool = False,
) -> dict[str, Any]:
    operation = _state()._normalize_account_security_operation(operation)
    selected = await _load_security_accounts(keys, operation=operation)
    options: dict[str, Any] = {}
    if operation == "password_change":
        try:
            password_mode = _state()._normalize_security_password_mode(password_mode)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        options["password_mode"] = password_mode
        options["new_password"] = new_password
    if force_password_confirmation:
        options["force_password_confirmation"] = True
    try:
        start_args = [selected, "protocol", operation, concurrency]
        if options:
            start_args.append(options)
        job = await asyncio.to_thread(_state().ACCOUNT_REAUTH_JOBS.start, *start_args)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    return {"ok": True, **job}


@router.post("/api/accounts/password-change/start")
@router.post("/api/accounts/change-password/start")
async def account_password_change_start(body: AccountPasswordChangeBatchRequest):
    new_password, _ = _security_batch_password(body)
    return await _start_security_batch(
        body.keys,
        operation="password_change",
        concurrency=body.concurrency,
        new_password=new_password,
        password_mode=body.password_mode,
    )


@router.post("/api/accounts/totp-change/start")
@router.post("/api/accounts/2fa-change/start")
@router.post("/api/accounts/mfa-change/start")
@router.post("/api/accounts/change-2fa/start")
async def account_totp_change_start(body: AccountTotpChangeBatchRequest):
    return await _start_security_batch(
        body.keys,
        operation="totp_change",
        concurrency=body.concurrency,
    )


@router.post("/api/accounts/security-operation/start")
async def account_security_operation_start(body: AccountSecurityOperationRequest):
    try:
        operation = _state()._normalize_account_security_operation(body.operation)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    new_password = ""
    password_mode = "custom"
    if operation == "password_change":
        new_password, _ = _security_batch_password(body)
        password_mode = body.password_mode
    return await _start_security_batch(
        body.keys,
        operation=operation,
        concurrency=body.concurrency,
        new_password=new_password,
        password_mode=password_mode,
        force_password_confirmation=bool(body.force_password_confirmation),
    )


@router.post("/api/accounts/password-change")
@router.post("/api/accounts/change-password")
async def account_password_change(body: AccountPasswordChangeRequest):
    new_password, _ = _security_batch_password(body)
    target = str(body.key or "").strip()
    selected = await _load_security_accounts([target], operation="password_change")
    try:
        saved = await asyncio.to_thread(
            _state()._change_account_password_sync,
            selected[0],
            new_password,
            password_mode=body.password_mode,
        )
    except Exception as error:
        raise HTTPException(
            409,
            _state()._redact_security_detail(error, selected[0], new_password),
        ) from error
    return {
        "ok": True,
        "message": f"{saved.get('email')} 修改密码成功",
        "item": _state().account_public_view(saved),
    }


@router.post("/api/accounts/totp-change")
@router.post("/api/accounts/2fa-change")
@router.post("/api/accounts/mfa-change")
@router.post("/api/accounts/change-2fa")
async def account_totp_change(body: AccountTotpChangeRequest):
    target = str(body.key or "").strip()
    selected = await _load_security_accounts([target], operation="totp_change")
    try:
        saved = await asyncio.to_thread(_state()._change_account_totp_sync, selected[0])
    except Exception as error:
        raise HTTPException(
            409,
            _state()._redact_security_detail(error, selected[0]),
        ) from error
    return {
        "ok": True,
        "message": f"{saved.get('email')} 修改 2FA 成功",
        "item": _state().account_public_view(saved),
    }


@router.get("/api/accounts/reauth/{job_id}")
async def account_reauthenticate_status(job_id: str):
    job = await asyncio.to_thread(_state().ACCOUNT_REAUTH_JOBS.snapshot, job_id)
    if job is None:
        raise HTTPException(404, "重新登录任务不存在或已清理")
    return {"ok": True, **job}


@router.post("/api/accounts/reauth/{job_id}/stop")
async def account_reauthenticate_stop(job_id: str):
    job = await asyncio.to_thread(_state().ACCOUNT_REAUTH_JOBS.stop, job_id)
    if job is None:
        raise HTTPException(404, "重新登录任务不存在或已清理")
    return {"ok": True, **job}


@router.post("/api/accounts/reauth")
async def account_reauthenticate(body: AccountReauthRequest):
    target = str(body.key or "").strip()
    records = await asyncio.to_thread(_state()._load_accounts)
    account = next((item for item in records if _state()._account_key(item) == target), None)
    if account is None:
        raise HTTPException(404, "账号不存在")
    if _state().account_public_view(account).get("is_deactivated"):
        raise HTTPException(409, "该账号已封号，不支持重新登录")
    try:
        reauth_mode = _state()._normalize_reauth_mode(body.mode)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    account_protocol = _state()._account_requires_protocol_reauth(account)
    if (reauth_mode == "protocol" or account_protocol) and not _state()._protocol_reauth_credentials_ready(account):
        raise HTTPException(409, "协议重新登录要求账号同时保存密码和 TOTP 2FA")
    if account_protocol:
        reauth_mode = "protocol"
    try:
        saved = await asyncio.to_thread(
            _state()._reauthenticate_account_sync,
            account,
            reauth_mode,
        )
    except Exception as error:
        raise HTTPException(409, str(error)) from error
    return {
        "ok": True,
        "message": f"{saved.get('email')} 重新登录成功",
        "item": _state().account_public_view(saved),
    }

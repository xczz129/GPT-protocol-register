"""Authentication and public CDK redemption routes.

The application keeps its runtime state in :mod:`web_app` for compatibility
with the existing panel helpers and tests.  Handlers resolve that module only
when a request is served, which keeps this route module independent from the
application bootstrap and avoids an import cycle.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field


router = APIRouter()


class LoginRequest(BaseModel):
    username: str = ""
    password: str = ""


class MailMessagesRequest(BaseModel):
    email: str
    provider_type: str | None = "auto"
    limit: int = Field(10, ge=1, le=50)


class RedeemRequest(BaseModel):
    code: str = Field(min_length=8, max_length=96)


class BatchRedeemRequest(BaseModel):
    codes: list[str] = Field(min_length=1, max_length=20)


class RedeemMessagesRequest(BaseModel):
    limit: int = Field(10, ge=1, le=30)


class RedeemMessageRequest(BaseModel):
    message_id: str = Field(min_length=1, max_length=128)


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=256)


def _state() -> Any:
    # Delayed import is intentional: web_app re-exports these handlers.
    import web_app

    return web_app


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    state = _state()
    if state._auth_ok(request):
        return state.RedirectResponse(url="/cdk", status_code=302)
    login_path = state.STATIC_DIR / "login.html"
    if not login_path.is_file():
        return HTMLResponse("<h1>login.html missing</h1>", status_code=500)
    return FileResponse(
        login_path,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@router.get("/redeem", response_class=HTMLResponse)
@router.get("/cdk-query", response_class=HTMLResponse)
async def redeem_page():
    state = _state()
    redeem_path = state.STATIC_DIR / "redeem.html"
    if not redeem_path.is_file():
        return HTMLResponse("<h1>redeem.html missing</h1>", status_code=500)
    return FileResponse(
        redeem_path,
        headers={
            "Cache-Control": "public, max-age=60, s-maxage=300, stale-while-revalidate=60"
        },
    )


@router.get("/api/auth/status")
async def auth_status(request: Request):
    state = _state()
    user = state._auth_user(request)
    return {
        "ok": True,
        "authenticated": bool(user),
        "username": user or "",
        "auth_mode": "password",
    }


@router.post("/api/auth/login")
async def auth_login(body: LoginRequest, response: Response, request: Request):
    state = _state()
    client_ip = state._request_client_ip(request)
    now = time.time()
    with state._LOGIN_ATTEMPTS_LOCK:
        recent = [
            stamp
            for stamp in state._LOGIN_ATTEMPTS.get(client_ip, [])
            if now - stamp < 600
        ]
        state._LOGIN_ATTEMPTS[client_ip] = recent
        if len(recent) >= 5 and now - recent[-1] < 300:
            retry_after = max(1, int(300 - (now - recent[-1])))
            raise HTTPException(429, f"登录失败次数过多，请 {retry_after} 秒后重试")
    creds = state._ensure_web_credentials()
    username = str(body.username or "").strip()
    password = str(body.password or "")
    from common.web_auth import verify_password

    ok_user = secrets.compare_digest(username, creds["username"])
    stored_pass = str(creds["password"] or "")
    if stored_pass.startswith("pbkdf2_sha256$"):
        ok_pass = verify_password(password, stored_pass)
    else:
        ok_pass = secrets.compare_digest(password, stored_pass)
    if not (ok_user and ok_pass):
        with state._LOGIN_ATTEMPTS_LOCK:
            state._LOGIN_ATTEMPTS.setdefault(client_ip, []).append(now)
        raise HTTPException(401, "用户名或密码错误")
    with state._LOGIN_ATTEMPTS_LOCK:
        state._LOGIN_ATTEMPTS.pop(client_ip, None)
    if not stored_pass.startswith("pbkdf2_sha256$"):
        try:
            from common.web_auth import hash_password

            with state._edit_config() as cfg:
                web_cfg = cfg.setdefault("web", {})
                if str(web_cfg.get("password") or "") == stored_pass:
                    web_cfg["password"] = hash_password(password)
            state._WEB_CREDS_CACHE = None
            state._WEB_CREDS_CACHE_MTIME = None
        except Exception:
            state_log = getattr(state, "logger", None)
            if state_log:
                state_log.exception("failed to upgrade panel password to hash")
    session = state._sign_session(creds["username"])
    state._set_session_cookie(response, session, secure=state._request_is_secure(request))
    return {
        "ok": True,
        "username": creds["username"],
        "message": "登录成功",
    }


@router.post("/api/auth/logout")
async def auth_logout(response: Response):
    _state()._clear_session_cookie(response)
    return {"ok": True, "message": "已退出登录"}


@router.post("/api/redeem")
async def redeem_exchange(body: RedeemRequest, response: Response, request: Request):
    state = _state()
    client_ip = state._request_client_ip(request)
    now = time.time()
    with state._REDEEM_ATTEMPTS_LOCK:
        recent = [
            stamp
            for stamp in state._REDEEM_ATTEMPTS.get(client_ip, [])
            if now - stamp < 600
        ]
        state._REDEEM_ATTEMPTS[client_ip] = recent
        if len(recent) >= 10:
            raise HTTPException(429, "兑换尝试过于频繁，请稍后重试")
    try:
        record = await state.asyncio.to_thread(
            state.redeem_cdk, body.code, state.CDKS_FILE, allow_redeemed=False
        )
    except KeyError:
        with state._REDEEM_ATTEMPTS_LOCK:
            state._REDEEM_ATTEMPTS.setdefault(client_ip, []).append(now)
        raise HTTPException(400, "CDK 无效")
    except PermissionError as error:
        with state._REDEEM_ATTEMPTS_LOCK:
            state._REDEEM_ATTEMPTS.setdefault(client_ip, []).append(now)
        raise HTTPException(403, str(error))
    except FileExistsError:
        raise HTTPException(409, "CDK 已兑换，请前往 CDK 查询")
    with state._REDEEM_ATTEMPTS_LOCK:
        state._REDEEM_ATTEMPTS.pop(client_ip, None)
    state._set_redeem_cookie(response, str(record.get("id") or ""))
    account = await state.asyncio.to_thread(state._redeem_bound_account, record)
    security = state._redeem_security_payload(account or {})
    return {
        "ok": True,
        "authenticated": True,
        "email": str(record.get("email") or ""),
        "provider_type": str(record.get("provider_type") or "auto"),
        "expires_at": str(record.get("expires_at") or ""),
        **security,
    }


@router.post("/api/redeem/batch")
async def redeem_batch_exchange(
    body: BatchRedeemRequest,
    response: Response,
    request: Request,
):
    state = _state()
    client_ip = state._request_client_ip(request)
    codes: list[str] = []
    seen: set[str] = set()
    for raw_code in body.codes:
        code = str(raw_code or "").strip().upper()
        if code and code not in seen:
            seen.add(code)
            codes.append(code)
    if not codes:
        raise HTTPException(400, "请至少输入一个 CDK")

    results: list[dict[str, Any]] = []
    last_record: dict[str, Any] | None = None
    for code in codes:
        now = time.time()
        with state._REDEEM_ATTEMPTS_LOCK:
            recent = [
                stamp
                for stamp in state._REDEEM_ATTEMPTS.get(client_ip, [])
                if now - stamp < 600
            ]
            state._REDEEM_ATTEMPTS[client_ip] = recent
            rate_limited = len(recent) >= 10
        if rate_limited:
            results.append(
                {
                    "code": code,
                    "ok": False,
                    "queryable": False,
                    "email": "",
                    "error": "兑换尝试过于频繁，请稍后重试",
                }
            )
            continue
        if not 8 <= len(code) <= 96:
            with state._REDEEM_ATTEMPTS_LOCK:
                state._REDEEM_ATTEMPTS.setdefault(client_ip, []).append(now)
            results.append(
                {
                    "code": code,
                    "ok": False,
                    "queryable": False,
                    "email": "",
                    "error": "CDK 无效",
                }
            )
            continue
        try:
            record = await state.asyncio.to_thread(
                state.redeem_cdk, code, state.CDKS_FILE, allow_redeemed=False
            )
        except KeyError:
            with state._REDEEM_ATTEMPTS_LOCK:
                state._REDEEM_ATTEMPTS.setdefault(client_ip, []).append(now)
            results.append(
                {
                    "code": code,
                    "ok": False,
                    "queryable": False,
                    "email": "",
                    "error": "CDK 无效",
                }
            )
        except PermissionError as error:
            with state._REDEEM_ATTEMPTS_LOCK:
                state._REDEEM_ATTEMPTS.setdefault(client_ip, []).append(now)
            results.append(
                {
                    "code": code,
                    "ok": False,
                    "queryable": False,
                    "email": "",
                    "error": str(error),
                }
            )
        except FileExistsError:
            results.append(
                {
                    "code": code,
                    "ok": False,
                    "queryable": True,
                    "email": "",
                    "error": "CDK 已兑换",
                }
            )
        else:
            last_record = record
            account = await state.asyncio.to_thread(state._redeem_bound_account, record)
            security = state._redeem_security_payload(account or {})
            results.append(
                {
                    "code": code,
                    "ok": True,
                    "queryable": True,
                    "email": str(record.get("email") or ""),
                    "password_set": bool(security.get("password_set")),
                    "mfa_enabled": bool(security.get("mfa_enabled")),
                    "mfa_factor_type": str(security.get("mfa_factor_type") or ""),
                    "error": "",
                }
            )

    if last_record is not None:
        with state._REDEEM_ATTEMPTS_LOCK:
            state._REDEEM_ATTEMPTS.pop(client_ip, None)
        state._set_redeem_cookie(response, str(last_record.get("id") or ""))
    return {
        "ok": True,
        "total": len(results),
        "success": sum(1 for item in results if item["ok"]),
        "failed": sum(1 for item in results if not item["ok"]),
        "items": results,
    }


@router.post("/api/cdk-query")
async def cdk_query(body: RedeemRequest, response: Response, request: Request):
    state = _state()
    client_ip = state._request_client_ip(request)
    now = time.time()
    with state._REDEEM_ATTEMPTS_LOCK:
        recent = [
            stamp
            for stamp in state._REDEEM_ATTEMPTS.get(client_ip, [])
            if now - stamp < 600
        ]
        state._REDEEM_ATTEMPTS[client_ip] = recent
        if len(recent) >= 10:
            raise HTTPException(429, "查询尝试过于频繁，请稍后重试")
    try:
        record = await state.asyncio.to_thread(
            state.lookup_cdk, body.code, state.CDKS_FILE
        )
    except KeyError:
        with state._REDEEM_ATTEMPTS_LOCK:
            state._REDEEM_ATTEMPTS.setdefault(client_ip, []).append(now)
        raise HTTPException(400, "CDK 无效")
    status = str(record.get("status") or "available")
    if status == "disabled":
        raise HTTPException(403, "CDK 已停用")
    expires_at = str(record.get("expires_at") or "")
    if expires_at:
        try:
            if state.datetime.fromisoformat(expires_at.replace("Z", "+00:00")) <= state.datetime.now(state.timezone.utc):
                raise HTTPException(403, "CDK 已过期")
        except HTTPException:
            raise
        except Exception:
            pass
    with state._REDEEM_ATTEMPTS_LOCK:
        state._REDEEM_ATTEMPTS.pop(client_ip, None)
    can_read_mail = status == "redeemed"
    security: dict[str, Any] = {}
    if can_read_mail:
        state._set_redeem_cookie(response, str(record.get("id") or ""))
        account = await state.asyncio.to_thread(state._redeem_bound_account, record)
        security = state._redeem_security_payload(account or {})
    return {
        "ok": True,
        "status": status,
        "status_label": "已兑换" if status == "redeemed" else "待兑换",
        "can_read_mail": can_read_mail,
        "email": str(record.get("email") or ""),
        "created_at": str(record.get("created_at") or ""),
        "redeemed_at": str(record.get("redeemed_at") or ""),
        "expires_at": expires_at,
        **security,
    }


@router.get("/api/redeem/session")
async def redeem_session(request: Request):
    state = _state()
    record = state._redeem_record_from_request(request)
    if record is None:
        return {"ok": True, "authenticated": False}
    account = await state.asyncio.to_thread(state._redeem_bound_account, record)
    security = state._redeem_security_payload(account or {})
    return {
        "ok": True,
        "authenticated": True,
        "email": str(record.get("email") or ""),
        "provider_type": str(record.get("provider_type") or "auto"),
        "redeemed_at": str(record.get("redeemed_at") or ""),
        "expires_at": str(record.get("expires_at") or ""),
        **security,
    }


@router.get("/api/redeem/access-token")
async def redeem_access_token(request: Request):
    state = _state()
    account = await state.asyncio.to_thread(state._require_redeemed_account, request)
    token = state._stored_access_token(account)
    if not token:
        raise HTTPException(409, "绑定账号没有 Access Token")
    return JSONResponse(
        {"ok": True, "access_token": token},
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@router.get("/api/redeem/account.json")
async def redeem_account_json(request: Request):
    state = _state()
    account = await state.asyncio.to_thread(state._require_redeemed_account, request)
    email = str(account.get("email") or "account").replace("/", "_")
    content = json.dumps(state._redeem_account_export(account), ensure_ascii=False, indent=2) + "\n"
    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Content-Disposition": f'attachment; filename="{email}.json"',
        },
    )


@router.post("/api/redeem/messages")
async def redeem_messages(body: RedeemMessagesRequest, request: Request):
    state = _state()
    record = state._redeem_record_from_request(request, touch=True)
    if record is None:
        raise HTTPException(401, "兑换会话已失效")
    mailbox_request = state.MailMessagesRequest(
        email=str(record.get("email") or ""),
        provider_type=str(record.get("provider_type") or "auto"),
        limit=body.limit,
    )
    try:
        return await state.asyncio.to_thread(
            state._fetch_mailbox_messages_sync, state._read_config(), mailbox_request
        )
    except HTTPException as error:
        raise HTTPException(error.status_code, str(error.detail))
    except Exception as error:
        return {
            "ok": False,
            "email": mailbox_request.email,
            "provider_type": mailbox_request.provider_type,
            "items": [],
            "error": str(error)[:240],
        }


@router.post("/api/redeem/message")
async def redeem_message(body: RedeemMessageRequest, request: Request):
    state = _state()
    record = state._redeem_record_from_request(request, touch=True)
    if record is None:
        raise HTTPException(401, "查询会话已失效")
    try:
        return await state.asyncio.to_thread(
            state._fetch_icloud_message_sync,
            state._read_config(),
            str(record.get("email") or ""),
            str(record.get("provider_type") or "auto"),
            body.message_id,
        )
    except HTTPException as error:
        raise HTTPException(error.status_code, str(error.detail))
    except Exception as error:
        raise HTTPException(502, f"邮件正文读取失败: {str(error)[:200]}")


@router.post("/api/redeem/logout")
async def redeem_logout(response: Response):
    _state()._clear_redeem_cookie(response)
    return {"ok": True, "authenticated": False}


@router.post("/api/auth/change-password")
async def auth_change_password(
    body: PasswordChangeRequest,
    response: Response,
    request: Request,
):
    state = _state()
    with state._edit_config() as cfg:
        web = cfg.setdefault("web", {})
        from common.web_auth import hash_password, verify_password

        current = str(web.get("password") or "")
        if current.startswith("pbkdf2_sha256$"):
            ok_current = verify_password(str(body.current_password or ""), current)
        else:
            ok_current = secrets.compare_digest(str(body.current_password or ""), current)
        if not ok_current:
            raise HTTPException(400, "当前密码不正确")
        web["password"] = hash_password(body.new_password)
        web["token"] = secrets.token_urlsafe(32)
    state._WEB_CREDS_CACHE = None
    state._WEB_CREDS_CACHE_MTIME = None
    session = state._sign_session(str(web.get("username") or "admin"))
    state._set_session_cookie(response, session, secure=state._request_is_secure(request))
    return {"ok": True, "message": "密码已修改，其他登录会话已失效"}


@router.post("/api/auth/revoke-sessions")
async def auth_revoke_sessions(response: Response, request: Request):
    state = _state()
    with state._edit_config() as cfg:
        web = cfg.setdefault("web", {})
        web["token"] = secrets.token_urlsafe(32)
    state._WEB_CREDS_CACHE = None
    state._WEB_CREDS_CACHE_MTIME = None
    session = state._sign_session(str(web.get("username") or "admin"))
    state._set_session_cookie(response, session, secure=state._request_is_secure(request))
    return {"ok": True, "message": "其他登录会话已退出"}


auth_router = router

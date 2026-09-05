"""Authentication routes for the ops panel.

The application keeps its runtime state in :mod:`web_app` for compatibility
with the existing panel helpers and tests.  Handlers resolve that module only
when a request is served, which keeps this route module independent from the
application bootstrap and avoids an import cycle.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field


router = APIRouter()


class LoginRequest(BaseModel):
    username: str = ""
    password: str = ""


class MailMessagesRequest(BaseModel):
    email: str
    provider_type: str | None = "auto"
    limit: int = Field(10, ge=1, le=50)


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
        return state.RedirectResponse(url="/", status_code=302)
    index_path = state.STATIC_DIR / "vue" / "index.html"
    if not index_path.is_file():
        return HTMLResponse("<h1>login page missing</h1>", status_code=500)
    return FileResponse(
        index_path,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
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

    # compare_digest 的 str 参数必须 ASCII-only，非 ASCII 输入会抛 TypeError 变 500；
    # 统一按 utf-8 bytes 比较
    ok_user = secrets.compare_digest(
        username.encode("utf-8"), str(creds["username"]).encode("utf-8")
    )
    stored_pass = str(creds["password"] or "")
    if stored_pass.startswith("pbkdf2_sha256$"):
        ok_pass = verify_password(password, stored_pass)
    else:
        ok_pass = secrets.compare_digest(
            password.encode("utf-8"), stored_pass.encode("utf-8")
        )
    if not (ok_user and ok_pass):
        with state._LOGIN_ATTEMPTS_LOCK:
            # 顺带清理长期未访问 IP 的历史条目，防止字典无界增长
            stale_ips = [
                ip
                for ip, stamps in state._LOGIN_ATTEMPTS.items()
                if not stamps or now - max(stamps) > 600
            ]
            for ip in stale_ips:
                state._LOGIN_ATTEMPTS.pop(ip, None)
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
            # 非 ASCII 输入走 bytes 比较，避免 compare_digest 抛 TypeError
            ok_current = secrets.compare_digest(
                str(body.current_password or "").encode("utf-8"),
                current.encode("utf-8"),
            )
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

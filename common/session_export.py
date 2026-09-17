# -*- coding: utf-8 -*-
"""Export ChatGPT session / access_token after browser registration."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urlparse

from config import TOKEN_OUTPUT_DIR

OPENAI_AUTH_CLAIM = "https://api.openai.com/auth"
OPENAI_PROFILE_CLAIM = "https://api.openai.com/profile"
PLATFORM_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"
PLATFORM_REDIRECT_URI = "https://platform.openai.com/auth/callback"
PLATFORM_AUDIENCE = "https://api.openai.com/v1"
PLATFORM_TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _s(value="") -> str:
    return str(value if value is not None else "").strip()


def _first_non_empty(*values) -> str:
    for v in values:
        n = _s(v)
        if n:
            return n
    return ""


def _email_or_empty(value="") -> str:
    e = _s(value)
    return e if _EMAIL_RE.match(e) else ""


def _is_obj(value) -> bool:
    return isinstance(value, dict)


def _b64url_decode(segment="") -> str:
    seg = _s(segment)
    if not seg:
        return ""
    padded = seg + "=" * (-len(seg) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", "replace")
    except Exception:
        return ""


def _b64url_encode_json(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse_jwt_payload(token=""):
    t = _s(token)
    if not t:
        return None
    parts = t.split(".")
    if len(parts) < 2:
        return None
    try:
        return json.loads(_b64url_decode(parts[1]))
    except Exception:
        return None


def _get_claim_section(payload, claim):
    if not _is_obj(payload):
        return {}
    section = payload.get(claim)
    return section if _is_obj(section) else {}


def _iso_from_unix_seconds(value):
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return ""
    try:
        return datetime.fromtimestamp(numeric, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return ""


def _iso_from_any(value):
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        ms = value if value > 1e11 else value * 1000
        try:
            return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return ""
    s = _s(value)
    return s


def _epoch_seconds(value):
    if value is None or value == "":
        return 0
    try:
        numeric = float(value)
        return int(numeric / 1000 if numeric > 1e11 else numeric)
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(_s(value).replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return 0


def _build_synthetic_codex_id_token(email, account_id, plan_type, user_id, expires_at):
    aid = _s(account_id)
    if not aid:
        return ""
    now = int(time.time())
    expires = _epoch_seconds(expires_at) or (now + 90 * 24 * 60 * 60)
    auth_info = {"chatgpt_account_id": aid}
    if plan_type:
        auth_info["chatgpt_plan_type"] = _s(plan_type)
    if user_id:
        auth_info["chatgpt_user_id"] = _s(user_id)
        auth_info["user_id"] = _s(user_id)
    payload = {"iat": now, "exp": expires, OPENAI_AUTH_CLAIM: auth_info}
    if email:
        payload["email"] = _s(email)
    header = _b64url_encode_json({"alg": "none", "typ": "JWT", "cpa_synthetic": True})
    return f"{header}.{_b64url_encode_json(payload)}.synthetic"


def _sanitize_file_segment(value="", fallback="chatgpt-session"):
    n = _s(value)
    n = re.sub(r'[\\/:*?"<>|]+', "-", n)
    n = re.sub(r"\s+", "-", n)
    n = re.sub(r"-+", "-", n)
    n = n.strip("-")
    return n or fallback


def build_account_record(
    session,
    email="",
    password="",
    proxy="",
    session_cookie="",
    browser_type="",
):
    """Build registered_accounts.json style record from browser session."""
    sess = session if _is_obj(session) else {}
    access_token = _s(sess.get("accessToken")) or _s(sess.get("access_token"))
    access_payload = parse_jwt_payload(access_token) or {}
    access_auth = _get_claim_section(access_payload, OPENAI_AUTH_CLAIM)
    profile = _get_claim_section(access_payload, OPENAI_PROFILE_CLAIM)
    user = sess.get("user") if _is_obj(sess.get("user")) else {}
    account = sess.get("account") if _is_obj(sess.get("account")) else {}

    email_val = _first_non_empty(
        _email_or_empty(user.get("email")),
        _email_or_empty(sess.get("email")),
        _email_or_empty(email),
        _email_or_empty(profile.get("email")),
        _email_or_empty(access_payload.get("email")),
    )
    account_id = _first_non_empty(
        account.get("id"),
        sess.get("account_id"),
        access_auth.get("chatgpt_account_id"),
    )
    plan_type = _first_non_empty(
        account.get("planType"),
        account.get("plan_type"),
        sess.get("planType"),
        sess.get("plan_type"),
        access_auth.get("chatgpt_plan_type"),
        "free",
    )
    expires_at = _first_non_empty(
        _iso_from_unix_seconds(access_payload.get("exp")),
        _iso_from_any(sess.get("expires")),
        _iso_from_any(sess.get("expiresAt")),
    )

    return {
        "email": email_val or email,
        "password": password,
        "access_token": access_token,
        "session_token": _first_non_empty(
            sess.get("sessionToken"), sess.get("session_token"), session_cookie
        ),
        "account_id": account_id,
        "plan_type": plan_type,
        "expires_at": expires_at,
        "proxy": proxy or "",
        "registration_method": "browser",
        "browser_type": _s(browser_type) or "local",
        "source_type": "fingerprint_browser",
        "registered_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "session": sess if sess else None,
    }


def _oauth_code_from_url(value: str) -> str:
    try:
        return str((parse_qs(urlparse(str(value or "")).query).get("code") or [""])[0])
    except Exception:
        return ""


async def fetch_platform_refresh_tokens(
    context,
    *,
    email: str = "",
    device_id: str = "",
    timeout: float = 25.0,
) -> dict[str, str] | None:
    """Use the authenticated browser context to mint renewable OAuth tokens."""
    request_context = getattr(context, "request", None)
    if request_context is None or not callable(getattr(context, "new_page", None)):
        return None

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode("ascii").rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    params = {
        "issuer": "https://auth.openai.com",
        "client_id": PLATFORM_CLIENT_ID,
        "audience": PLATFORM_AUDIENCE,
        "redirect_uri": PLATFORM_REDIRECT_URI,
        "device_id": str(device_id or ""),
        "screen_hint": "login",
        "login_hint": str(email or ""),
        "scope": "openid profile email offline_access",
        "response_type": "code",
        "response_mode": "query",
        "state": secrets.token_urlsafe(32),
        "nonce": secrets.token_urlsafe(32),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    authorize_url = "https://auth.openai.com/api/accounts/authorize?" + urlencode(params)
    page = await context.new_page()
    callback_code = ""

    def observe_url(source) -> None:
        nonlocal callback_code
        value = str(getattr(source, "url", source) or "")
        callback_code = callback_code or _oauth_code_from_url(value)

    if callable(getattr(page, "on", None)):
        page.on("request", observe_url)
        page.on("response", observe_url)
    try:
        try:
            await page.goto(
                authorize_url,
                wait_until="domcontentloaded",
                timeout=max(5_000, int(timeout * 1000)),
            )
        except Exception:
            observe_url(getattr(page, "url", ""))

        deadline = time.monotonic() + max(5.0, timeout)
        consent_clicked = False
        while not callback_code and time.monotonic() < deadline:
            observe_url(getattr(page, "url", ""))
            if callback_code:
                break
            if not consent_clicked and callable(getattr(page, "evaluate", None)):
                try:
                    consent_clicked = bool(
                        await page.evaluate(
                            """() => {
                              const wanted = /^(continue|allow|authorize|accept|同意|继续|授权)$/i;
                              const button = [...document.querySelectorAll('button')]
                                .find((node) => wanted.test((node.innerText || node.textContent || '').trim()));
                              if (!button) return false;
                              button.click();
                              return true;
                            }"""
                        )
                    )
                except Exception:
                    pass
            await asyncio.sleep(0.25)
        if not callback_code:
            return None

        response = await request_context.post(
            PLATFORM_TOKEN_URL,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            data={
                "client_id": PLATFORM_CLIENT_ID,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "code": callback_code,
                "redirect_uri": PLATFORM_REDIRECT_URI,
            },
            timeout=60_000,
        )
        if not bool(getattr(response, "ok", False)):
            return None
        payload = await response.json()
        if not isinstance(payload, dict) or not payload.get("refresh_token"):
            return None
        return {
            key: str(payload.get(key) or "").strip()
            for key in ("access_token", "refresh_token", "id_token")
            if str(payload.get(key) or "").strip()
        }
    finally:
        if callable(getattr(page, "remove_listener", None)):
            try:
                page.remove_listener("request", observe_url)
                page.remove_listener("response", observe_url)
            except Exception:
                pass
        try:
            await page.close()
        except Exception:
            pass


def build_cpa_codex_json(session, email=""):
    sess = session if _is_obj(session) else {}
    access_token = _s(sess.get("accessToken")) or _s(sess.get("access_token"))
    if not access_token:
        raise ValueError("未读取到可导入的 ChatGPT accessToken。")

    input_id_token = _first_non_empty(sess.get("idToken"), sess.get("id_token"))
    refresh_token = _first_non_empty(sess.get("refreshToken"), sess.get("refresh_token"))
    session_token = _first_non_empty(sess.get("sessionToken"), sess.get("session_token"))

    access_payload = parse_jwt_payload(access_token) or {}
    id_payload = parse_jwt_payload(input_id_token) or {}
    access_auth = _get_claim_section(access_payload, OPENAI_AUTH_CLAIM)
    id_auth = _get_claim_section(id_payload, OPENAI_AUTH_CLAIM)
    profile = _get_claim_section(access_payload, OPENAI_PROFILE_CLAIM)

    expires_at = _first_non_empty(
        _iso_from_unix_seconds(access_payload.get("exp")),
        _iso_from_any(sess.get("expires")),
        _iso_from_any(sess.get("expiresAt")),
    )

    user = sess.get("user") if _is_obj(sess.get("user")) else {}
    account = sess.get("account") if _is_obj(sess.get("account")) else {}
    email_val = _first_non_empty(
        _email_or_empty(user.get("email")),
        _email_or_empty(sess.get("email")),
        _email_or_empty(email),
        _email_or_empty(profile.get("email")),
    )
    account_id = _first_non_empty(
        account.get("id"),
        sess.get("account_id"),
        access_auth.get("chatgpt_account_id"),
        id_auth.get("chatgpt_account_id"),
    )
    user_id = _first_non_empty(
        user.get("id"),
        sess.get("user_id"),
        access_auth.get("chatgpt_user_id"),
        access_auth.get("user_id"),
    )
    plan_type = _first_non_empty(
        account.get("planType"),
        account.get("plan_type"),
        sess.get("planType"),
        access_auth.get("chatgpt_plan_type"),
    )

    exported_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    synthetic_id_token = "" if input_id_token else _build_synthetic_codex_id_token(
        email_val, account_id, plan_type, user_id, expires_at
    )
    id_token = input_id_token or synthetic_id_token

    raw = {
        "type": "codex",
        "account_id": account_id,
        "chatgpt_account_id": account_id,
        "email": email_val,
        "name": _first_non_empty(email_val, email, "ChatGPT Account"),
        "plan_type": plan_type,
        "chatgpt_plan_type": plan_type,
        "id_token": id_token,
        "id_token_synthetic": True if synthetic_id_token else None,
        "access_token": access_token,
        "refresh_token": refresh_token or "",
        "session_token": session_token,
        "last_refresh": exported_at,
        "expired": expires_at,
    }
    auth_json = {k: v for k, v in raw.items() if v is not None and v != ""}
    name = _sanitize_file_segment(email_val or email or account_id or "account")
    return {
        "auth_json": auth_json,
        "file_name": f"codex-{name}.json",
        "email": email_val,
        "account_id": account_id,
    }


def build_chatgpt2api_account(session, email=""):
    """ChatGPT web session -> chatgpt2api ordinary-account import object.

    Do NOT set type to "codex" — that marks a codex source on the server.
    type here is plan tier (free/Plus/Pro...); source_type is "web".
    """
    sess = session if _is_obj(session) else {}
    access_token = _s(sess.get("accessToken")) or _s(sess.get("access_token"))
    if not access_token:
        raise ValueError("未读取到可导入的 ChatGPT accessToken。")

    access_payload = parse_jwt_payload(access_token) or {}
    access_auth = _get_claim_section(access_payload, OPENAI_AUTH_CLAIM)
    profile = _get_claim_section(access_payload, OPENAI_PROFILE_CLAIM)

    user = sess.get("user") if _is_obj(sess.get("user")) else {}
    account = sess.get("account") if _is_obj(sess.get("account")) else {}
    email_val = _first_non_empty(
        _email_or_empty(user.get("email")),
        _email_or_empty(sess.get("email")),
        _email_or_empty(email),
        _email_or_empty(profile.get("email")),
        _email_or_empty(access_payload.get("email")),
    )
    account_id = _first_non_empty(
        account.get("id"),
        sess.get("account_id"),
        access_auth.get("chatgpt_account_id"),
    )
    plan_type = _first_non_empty(
        account.get("planType"),
        account.get("plan_type"),
        sess.get("planType"),
        sess.get("plan_type"),
        access_auth.get("chatgpt_plan_type"),
    )

    item = {"access_token": access_token, "source_type": "web"}
    if email_val:
        item["email"] = email_val
    if account_id:
        item["account_id"] = account_id
    if plan_type:
        item["type"] = plan_type
    return item


async def fetch_chatgpt_session(page, timeout: float = 7.0):
    """Race browser-context and in-page session reads with a hard deadline."""
    stamp = int(time.time() * 1000)

    async def via_page():
        try:
            return await page.evaluate(
                """(stamp) => fetch(`/api/auth/session?update=${stamp}`, {
                    credentials: 'include',
                    cache: 'no-store',
                    headers: {'Cache-Control': 'no-cache', 'Pragma': 'no-cache'},
                }).then(r => r.ok ? r.json() : null).catch(() => null)""",
                stamp,
            )
        except Exception:
            return None

    async def via_context(request_context):
        try:
            response = await request_context.get(
                f"https://chatgpt.com/api/auth/session?update={stamp}",
                headers={
                    "Accept": "application/json",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
                timeout=max(1000, int(timeout * 1000)),
            )
            if not bool(getattr(response, "ok", False)):
                return None
            return await response.json()
        except Exception:
            return None

    tasks = {asyncio.create_task(via_page())}
    request_context = getattr(getattr(page, "context", None), "request", None)
    if request_context is not None and callable(getattr(request_context, "get", None)):
        tasks.add(asyncio.create_task(via_context(request_context)))

    deadline = time.monotonic() + max(0.5, timeout)
    pending = tasks
    try:
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, pending = await asyncio.wait(
                pending,
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                break
            for task in done:
                try:
                    result = task.result()
                except Exception:
                    result = None
                if _is_obj(result) and _s(result.get("accessToken")):
                    return result
        return None
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


def save_chatgpt_tokens(session, email=""):
    if not _is_obj(session) or not _s(session.get("accessToken")):
        return False
    pdir = os.path.join(TOKEN_OUTPUT_DIR, "chatgpt")
    os.makedirs(pdir, exist_ok=True)
    name = _sanitize_file_segment(email or session.get("user", {}).get("email") or "account")

    session_path = os.path.join(pdir, f"{name}.session.json")
    session_fd = os.open(
        session_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
    )
    with os.fdopen(session_fd, "w", encoding="utf-8") as f:
        json.dump(session, f, indent=2, ensure_ascii=False)
    try:
        os.chmod(session_path, 0o600)
    except Exception:
        pass

    try:
        cpa = build_cpa_codex_json(session, email=email)
        cpa_path = os.path.join(pdir, cpa["file_name"])
        with open(cpa_path, "w", encoding="utf-8") as f:
            json.dump(cpa["auth_json"], f, indent=2, ensure_ascii=False)
        try:
            os.chmod(cpa_path, 0o600)
        except Exception:
            pass
        print(f"  [chatgpt] token 已保存 · 账号={email or name}")
        print(f"  [chatgpt] session: {session_path}")
        print(f"  [chatgpt] codex:   {cpa_path}")
    except Exception as e:
        print(f"  [chatgpt] 会话已保存 · 账号={email or name} · {session_path} (CPA 跳过: {e})")

    # chatgpt2api ordinary web account (for POST /api/accounts)
    try:
        c2a = build_chatgpt2api_account(session, email=email)
        c2a_path = os.path.join(pdir, f"c2a-{name}.json")
        with open(c2a_path, "w", encoding="utf-8") as f:
            json.dump(c2a, f, indent=2, ensure_ascii=False)
        try:
            os.chmod(c2a_path, 0o600)
        except Exception:
            pass
        print(f"  [chatgpt] chatgpt2api: {c2a_path}")
    except Exception as e:
        print(f"  [chatgpt] chatgpt2api 转换跳过: {e}")
    return True

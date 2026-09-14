"""协议层 HTTP 基础设施：会话创建、Sentinel 头、本地重试与 OAuth token 交换。"""

from __future__ import annotations

import base64
import json
import re
import secrets
import time
import uuid
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse
from curl_cffi import requests
from common.proxy_clearance import headers_with_clearance
from proxy_util import is_proxy_transport_error, is_rate_limit_error, normalize_proxy_url
from register.protocol_sentinel import build_sentinel_token as _build_sentinel_token_tuple, build_sentinel_with_so_token
from register.protocol_runtime import current_runtime_settings

from register.protocol_context import (
    _PROXY_CONFIG,
    _STOP_CHECK,
    _make_trace_headers,
    _oauth_timeout,
    _response_debug_detail,
    _response_json,
    _tls_verify,
    auth_base,
    config,
    platform_auth0_client,
    platform_base,
    platform_oauth_client_id,
    platform_oauth_redirect_uri,
)

from register.protocol_fingerprint import (
    _browser_fingerprint,
    _client_hint_headers,
    _fingerprint_with_user_agent,
    _header_fingerprint,
    common_headers,
    navigate_headers,
)




def _authorize_landed_page(resp) -> str:
    """诊断用：粗判 authorize 之后落在哪个页面。返回 signup / login / "" 仅供日志。

    注意：email-verification / email_otp_verification 在注册和登录流程里都会出现，
    无法据此可靠区分，所以这里只用于打日志，绝不据此中断注册流程。
    """
    if resp is None:
        return ""
    final_url = str(getattr(resp, "url", "") or "").lower()
    data = _response_json(resp)
    page_type = ""
    page = data.get("page") if isinstance(data, dict) else None
    if isinstance(page, dict):
        page_type = str(page.get("type") or "").lower()
    if "create-account" in final_url or "signup" in final_url or "create_account" in page_type:
        return "signup"
    if "/log-in" in final_url or "/login" in final_url or page_type in {"login", "password_verification"}:
        return "login"
    return ""




def _auth_step_details(payload: dict[str, Any] | None) -> tuple[str, str]:
    data = payload if isinstance(payload, dict) else {}
    page = data.get("page") if isinstance(data.get("page"), dict) else {}
    page_payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
    continue_url = str(
        data.get("continue_url")
        or page_payload.get("url")
        or page_payload.get("continue_url")
        or ""
    ).strip()
    return continue_url, str(page.get("type") or "").strip()




def _auth_step_requires_password(continue_url: str, page_type: str) -> bool:
    return "password" in f"{continue_url or ''} {page_type or ''}".lower()




def _is_chatgpt_callback_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return False
    return parsed.netloc.lower() == "chatgpt.com" or "/api/auth/callback" in parsed.path.lower()




def build_sentinel_token(
    session: requests.Session,
    device_id: str,
    flow: str,
    fingerprint: dict[str, str] | None = None,
    diagnostic: Callable[[str], None] | None = None,
) -> str:
    """请求 sentinel token，返回 sentinel header 字符串（兼容旧接口）。"""
    fp = _browser_fingerprint(fingerprint)
    sentinel_val, oai_sc_val = _build_sentinel_token_tuple(
        session,
        device_id,
        flow,
        user_agent=fp["user_agent"],
        sec_ch_ua=fp["sec_ch_ua"],
        diagnostic=diagnostic,
        sid=str(fp.get("sentinel_sid") or ""),
        device_profile=fp,
    )
    _set_oai_sc_cookie(session, oai_sc_val)
    return sentinel_val




def _set_oai_sc_cookie(session: Any, value: str) -> None:
    value = str(value or "").strip()
    if not value:
        return
    try:
        session.cookies.set("oai-sc", value, domain=".openai.com", path="/")
        session.cookies.set("oai-sc", value, domain="auth.openai.com", path="/")
    except Exception:
        # Test doubles and alternate cookie jars may not support domain/path.
        try:
            session.cookies.set("oai-sc", value)
        except Exception:
            pass




def _build_sentinel_headers(
    session: requests.Session,
    device_id: str,
    flow: str,
    fingerprint: dict[str, str] | None = None,
    diagnostic: Callable[[str], None] | None = None,
) -> dict[str, str]:
    """Build the Sentinel headers observed on auth JSON requests."""
    fp = _browser_fingerprint(fingerprint)
    token, so_token, oai_sc = build_sentinel_with_so_token(
        session,
        device_id,
        flow,
        user_agent=fp["user_agent"],
        sec_ch_ua=fp["sec_ch_ua"],
        diagnostic=diagnostic,
        sid=str(fp.get("sentinel_sid") or ""),
        device_profile=fp,
    )
    _set_oai_sc_cookie(session, oai_sc)
    headers = {"openai-sentinel-token": token}
    if so_token:
        headers["openai-sentinel-so-token"] = so_token
    return headers




def create_session(proxy: str = "", fingerprint: dict[str, str] | None = None) -> Any:
    fp = _browser_fingerprint(fingerprint)
    runtime = current_runtime_settings()
    kwargs: dict[str, Any] = {
        "impersonate": fp["impersonate"],
        "verify": runtime.tls_verify,
    }
    normalized = normalize_proxy_url(proxy)
    if normalized:
        kwargs["proxies"] = {"http": normalized, "https": normalized}
    session = requests.Session(**kwargs)
    session.headers.update({"user-agent": fp["user_agent"]})
    return session




def _headers_with_clearance(
    headers: dict[str, str],
    target_url: str,
    proxy: str = "",
    user_agent_override: str = "",
) -> dict[str, str]:
    scoped_proxy_config = _PROXY_CONFIG.get()
    proxy_config = scoped_proxy_config if isinstance(scoped_proxy_config, dict) else config.get("proxy_config")
    return headers_with_clearance(
        proxy_config,
        headers,
        target_url,
        user_agent_override,
        request_proxy=proxy,
    )




def _cloudflare_block_message(resp, prefix: str = "被 Cloudflare 拦截", reason: str = "") -> str:
    status = getattr(resp, "status_code", "unknown")
    debug = _response_debug_detail(resp)
    reason = reason or "clearance 刷新失败或重试后仍失败，请更换 IP/代理重试"
    return f"{prefix}，{reason}: status={status}, {debug}"




def request_with_local_retry(session: requests.Session, method: str, url: str, retry_attempts: int = 3, **kwargs):
    """Request with bounded transport/temporary-status retry.

    Rate limits are returned immediately so the owning task can stop. Other
    temporary responses retain the existing bounded transport retry behavior.
    """
    last_error = ""
    method_name = str(method or "GET").upper()
    retryable_statuses = {408, 425, 500, 502, 503, 504}
    if method_name not in {"GET", "HEAD", "OPTIONS"}:
        retryable_statuses.discard(500)
    attempts = max(1, retry_attempts)
    runtime = current_runtime_settings()
    request_kwargs = dict(kwargs)
    request_kwargs.setdefault("timeout", runtime.timeout_for(runtime.request_timeout))
    for attempt in range(attempts):
        if _STOP_CHECK.get()():
            return None, "stop requested"
        if runtime.expired():
            return None, "protocol_account_timeout"
        try:
            response = session.request(method_name, url, **request_kwargs)
            status = int(getattr(response, "status_code", 0) or 0)
            should_retry = status in retryable_statuses and attempt + 1 < attempts
            if not should_retry:
                return response, ""
            if _STOP_CHECK.get()():
                return None, "stop requested"
            retry_after = 0.0
            try:
                raw_retry_after = str(getattr(response, "headers", {}).get("retry-after") or "").strip()
                if raw_retry_after.isdigit():
                    retry_after = float(raw_retry_after)
                elif raw_retry_after:
                    retry_after = max(0.0, (parsedate_to_datetime(raw_retry_after).timestamp() - time.time()))
            except Exception:
                retry_after = 0.0
            time.sleep(min(8.0, max(0.25, retry_after or (0.5 * (2 ** attempt)))))
        except Exception as error:
            last_error = str(error)
            if is_rate_limit_error(error):
                return None, "rate_limit_exceeded: " + last_error
            if is_proxy_transport_error(last_error):
                return None, last_error
            if attempt + 1 >= attempts:
                break
            time.sleep(min(4.0, 0.5 * (2 ** attempt)))
    return None, last_error




def validate_otp(
    session: requests.Session,
    device_id: str,
    code: str,
    fingerprint: dict[str, str] | None = None,
    diagnostic: Callable[[str], None] | None = None,
    allow_sentinel_retry: bool = True,
):
    headers = _header_fingerprint(common_headers, fingerprint)
    headers["referer"] = f"{auth_base}/email-verification"
    headers["oai-device-id"] = device_id
    headers["x-access-flow-invocation-id"] = str(uuid.uuid4())
    headers.update(_make_trace_headers())
    sentinel_error = ""
    if allow_sentinel_retry:
        try:
            headers.update(
                _build_sentinel_headers(
                    session,
                    device_id,
                    "email_otp_validate",
                    fingerprint,
                    diagnostic,
                )
            )
        except Exception as error:
            sentinel_error = f"sentinel_prepare_failed: {str(error)[:200]}"
            if diagnostic:
                diagnostic(sentinel_error)
    resp, error = request_with_local_retry(session, "post", f"{auth_base}/api/accounts/email-otp/validate", json={"code": code}, headers=headers, verify=_tls_verify())
    if not allow_sentinel_retry:
        return resp, error
    if resp is not None and resp.status_code == 200:
        return resp, ""
    # 仅在 sentinel 准备阶段失败（或服务端明确拒绝 sentinel token）时才重放，
    # 避免对错误验证码等确定性失败做二次提交、加倍消耗 OTP 尝试次数触发风控
    resp_error = ""
    if resp is not None:
        try:
            err_obj = resp.json()
        except Exception:
            err_obj = {}
        if isinstance(err_obj, dict):
            raw_err = err_obj.get("error")
            if isinstance(raw_err, str):
                resp_error = raw_err
            elif isinstance(raw_err, dict):
                resp_error = str(raw_err.get("code") or "")
    deterministic_otp_failure = bool(
        resp is not None
        and resp.status_code in {400, 403}
        and any(
            marker in resp_error.lower()
            for marker in ("otp", "code", "rate_limit", "invalid")
        )
    )
    if deterministic_otp_failure and not sentinel_error:
        return resp, error or resp_error
    try:
        headers.update(
            _build_sentinel_headers(
                session,
                device_id,
                "email_otp_validate",
                fingerprint,
                diagnostic,
            )
        )
    except Exception as retry_error:
        return resp, error or sentinel_error or f"sentinel_retry_failed: {str(retry_error)[:200]}"
    resp, error = request_with_local_retry(session, "post", f"{auth_base}/api/accounts/email-otp/validate", json={"code": code}, headers=headers, verify=_tls_verify())
    return resp, error




def extract_oauth_callback_params_from_url(url: str) -> dict[str, str] | None:
    if not url:
        return None
    try:
        params = parse_qs(urlparse(url).query)
    except Exception:
        return None
    code = str((params.get("code") or [""])[0]).strip()
    if not code:
        return None
    return {"code": code, "state": str((params.get("state") or [""])[0]).strip(), "scope": str((params.get("scope") or [""])[0]).strip()}




def _callback_state_matches(callback: dict[str, str] | None, expected_state: str = "") -> bool:
    """Validate OAuth state when both sides provide it.

    Some consent redirects omit state, so absence remains compatible while an
    explicit mismatch is rejected before token exchange.
    """
    expected = str(expected_state or "").strip()
    actual = str((callback or {}).get("state") or "").strip()
    return not expected or not actual or secrets.compare_digest(expected, actual)




def _absolute_auth_url(url: str) -> str:
    value = str(url or "").strip()
    if value.startswith("/"):
        return f"{auth_base}{value}"
    return value




def _safe_url_for_log(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        return "-"
    try:
        parsed = urlparse(value)
    except Exception:
        return value[:160]
    if parsed.scheme and parsed.netloc:
        return f"{parsed.netloc}{parsed.path}"[:160]
    return parsed.path[:160] if parsed.path else value[:160]




def _auth_error_details_from_url(url: str) -> dict[str, str]:
    """Decode the error code embedded in Auth API redirect URLs."""
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return {}
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/").lower()
    if not (
        (host == "auth.openai.com" and path == "/error")
        or (host == "chatgpt.com" and path == "/auth/error")
    ):
        return {}
    query = parse_qs(parsed.query, keep_blank_values=True)
    direct = str((query.get("error") or [""])[0]).strip().lower()
    details = {
        "code": re.sub(r"[^a-z0-9_]+", "_", direct).strip("_") or "unknown",
        "request_id": "",
    }
    payload = str((query.get("payload") or [""])[0]).strip()
    if not payload:
        return details
    try:
        payload += "=" * (-len(payload) % 4)
        decoded = json.loads(base64.b64decode(payload).decode("utf-8"))
    except Exception:
        return details
    if not isinstance(decoded, dict):
        return details
    code = str(decoded.get("errorCode") or decoded.get("error_code") or "").strip().lower()
    request_id = str(decoded.get("requestId") or decoded.get("request_id") or "").strip()
    if code:
        details["code"] = re.sub(r"[^a-z0-9_]+", "_", code).strip("_") or "unknown"
    details["request_id"] = request_id[:120]
    return details




def _url_path(url: str) -> str:
    value = _absolute_auth_url(url)
    try:
        return urlparse(value).path.rstrip("/") or "/"
    except Exception:
        return ""




def _cookie_values(session) -> dict[str, str]:
    try:
        jar = getattr(session.cookies, "jar", session.cookies)
        return {
            str(getattr(cookie, "name", "") or ""): str(
                getattr(cookie, "value", "") or ""
            )
            for cookie in list(jar)
            if str(getattr(cookie, "name", "") or "")
        }
    except Exception:
        try:
            return {
                str(key): str(value)
                for key, value in session.cookies.get_dict().items()
            }
        except Exception:
            return {}




def _nextauth_session_cookie(session) -> str:
    cookies = _cookie_values(session)
    base = "__Secure-next-auth.session-token"
    if cookies.get(base):
        return cookies[base]
    chunks: list[tuple[int, str]] = []
    for name, value in cookies.items():
        if not name.startswith(base + "."):
            continue
        try:
            index = int(name.rsplit(".", 1)[1])
        except (TypeError, ValueError):
            continue
        chunks.append((index, value))
    return "".join(value for _, value in sorted(chunks))




def _append_exchange_error(errors: list[str] | None, message: str) -> None:
    if errors is not None and message:
        errors.append(message)




def request_platform_oauth_token(
    session: requests.Session,
    code: str,
    code_verifier: str,
    errors: list[str] | None = None,
    fingerprint: dict[str, str] | None = None,
) -> dict | None:
    fp = _browser_fingerprint(fingerprint)
    headers = {
        "accept": "*/*",
        "accept-language": fp["accept_language"],
        "auth0-client": platform_auth0_client,
        "cache-control": "no-cache",
        "content-type": "application/json",
        "origin": platform_base,
        "pragma": "no-cache",
        "priority": "u=1, i",
        "referer": f"{platform_base}/",
        **_client_hint_headers(fp),
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": fp["user_agent"],
    }
    try:
        resp = session.post(
            f"{auth_base}/api/accounts/oauth/token",
            headers=headers,
            json={
                "client_id": platform_oauth_client_id,
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": platform_oauth_redirect_uri,
            },
            verify=_tls_verify(),
            timeout=_oauth_timeout(),
        )
    except Exception as error:
        _append_exchange_error(errors, f"api token 请求异常: {str(error)[:300]}")
        return None
    if resp.status_code != 200:
        _append_exchange_error(errors, f"api token 接口拒绝: status={resp.status_code}, {_response_debug_detail(resp, 500)}")
        return None
    data = _response_json(resp)
    missing = [key for key in ("access_token", "refresh_token") if not data.get(key)]
    if missing:
        _append_exchange_error(errors, f"api token 返回缺少字段: {', '.join(missing)}")
        return None
    return data




def request_platform_oauth_token_legacy(
    session: requests.Session,
    code: str,
    code_verifier: str,
    proxy: str = "",
    errors: list[str] | None = None,
    fresh_session: bool = False,
    fingerprint: dict[str, str] | None = None,
) -> dict | None:
    token_session = create_session(proxy, fingerprint) if fresh_session else session
    resp = None
    try:
        resp = token_session.post(
            f"{auth_base}/oauth/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": platform_oauth_redirect_uri,
                "client_id": platform_oauth_client_id,
                "code_verifier": code_verifier,
            },
            verify=_tls_verify(),
            timeout=_oauth_timeout(),
        )
    except Exception as error:
        _append_exchange_error(errors, f"legacy token 请求异常: {str(error)[:300]}")
        return None
    finally:
        if fresh_session:
            try:
                token_session.close()
            except Exception:
                pass
    if resp is None:
        _append_exchange_error(errors, "legacy token 未返回响应")
        return None
    data = _response_json(resp)
    if resp.status_code != 200:
        _append_exchange_error(errors, f"legacy token 接口拒绝: status={resp.status_code}, {_response_debug_detail(resp, 500)}")
        return None
    missing = [key for key in ("access_token", "refresh_token", "id_token") if not data.get(key)]
    if missing:
        _append_exchange_error(errors, f"legacy token 返回缺少字段: {', '.join(missing)}")
        return None
    return data




def extract_callback_via_consent(
    session: requests.Session,
    consent_url: str,
    device_id: str,
    proxy: str = "",
    user_agent_override: str = "",
    fingerprint: dict[str, str] | None = None,
) -> dict[str, str] | None:
    current = _absolute_auth_url(consent_url)
    if not current:
        return None
    fp = _fingerprint_with_user_agent(fingerprint, user_agent_override) if user_agent_override else _browser_fingerprint(fingerprint)
    for _ in range(10):
        headers = _headers_with_clearance(_header_fingerprint(navigate_headers, fp), current, proxy, user_agent_override)
        resp, _error = request_with_local_retry(session, "get", current, headers=headers, verify=_tls_verify(), allow_redirects=False)
        if resp is None:
            return None
        callback = extract_oauth_callback_params_from_url(str(getattr(resp, "url", "") or ""))
        callback = callback or extract_oauth_callback_params_from_url(str(getattr(resp, "headers", {}).get("Location") or ""))
        if callback:
            return callback
        location = str(getattr(resp, "headers", {}).get("Location") or "").strip()
        if getattr(resp, "status_code", 0) not in (301, 302, 303, 307, 308) or not location:
            break
        current = _absolute_auth_url(location)

    raw = session.cookies.get("oai-client-auth-session", domain=".auth.openai.com") or session.cookies.get("oai-client-auth-session")
    if not raw:
        return None
    try:
        first = raw.split(".")[0]
        pad = 4 - len(first) % 4
        if pad != 4:
            first += "=" * pad
        payload = json.loads(base64.urlsafe_b64decode(first))
        workspace_id = payload["workspaces"][0]["id"]
    except Exception:
        return None

    url = f"{auth_base}/api/accounts/workspace/select"
    headers = _header_fingerprint(common_headers, fp)
    headers["referer"] = current
    headers["oai-device-id"] = device_id
    headers.update(_make_trace_headers())
    headers = _headers_with_clearance(headers, url, proxy, user_agent_override)
    ws_resp, _error = request_with_local_retry(session, "post", url, json={"workspace_id": workspace_id}, headers=headers, verify=_tls_verify(), allow_redirects=False)
    if ws_resp is None:
        return None
    callback = extract_oauth_callback_params_from_url(str(getattr(ws_resp, "headers", {}).get("Location") or ""))
    if callback:
        return callback

    ws_data = _response_json(ws_resp)
    orgs = ((ws_data.get("data") or {}).get("orgs") or []) if isinstance(ws_data, dict) else []
    if not orgs:
        return None
    org_id = str((orgs[0] or {}).get("id") or "").strip()
    project_id = str(((orgs[0] or {}).get("projects") or [{}])[0].get("id") or "").strip()
    if not org_id:
        return None
    org_url = f"{auth_base}/api/accounts/organization/select"
    org_headers = _header_fingerprint(common_headers, fp)
    org_headers["referer"] = str(ws_data.get("continue_url") or current)
    org_headers["oai-device-id"] = device_id
    org_headers.update(_make_trace_headers())
    org_headers = _headers_with_clearance(org_headers, org_url, proxy, user_agent_override)
    body = {"org_id": org_id}
    if project_id:
        body["project_id"] = project_id
    org_resp, _error = request_with_local_retry(session, "post", org_url, json=body, headers=org_headers, verify=_tls_verify(), allow_redirects=False)
    if org_resp is None:
        return None
    return extract_oauth_callback_params_from_url(str(getattr(org_resp, "headers", {}).get("Location") or ""))




def exchange_tokens_from_continue_url(
    session: requests.Session,
    device_id: str,
    code_verifier: str,
    continue_url: str,
    proxy: str = "",
    user_agent_override: str = "",
    errors: list[str] | None = None,
    fingerprint: dict[str, str] | None = None,
    expected_state: str = "",
) -> dict | None:
    callback = extract_oauth_callback_params_from_url(continue_url)
    if callback and not _callback_state_matches(callback, expected_state):
        _append_exchange_error(errors, "OAuth callback state 不匹配")
        return None
    fp = _fingerprint_with_user_agent(fingerprint, user_agent_override) if user_agent_override else _browser_fingerprint(fingerprint)
    callback = callback or extract_callback_via_consent(
        session,
        continue_url,
        device_id,
        proxy,
        user_agent_override,
        fp,
    )
    if callback and not _callback_state_matches(callback, expected_state):
        _append_exchange_error(errors, "OAuth consent callback state 不匹配")
        return None
    if not callback:
        url = _absolute_auth_url(continue_url)
        try:
            headers = _headers_with_clearance(_header_fingerprint(navigate_headers, fp), url, proxy, user_agent_override)
            runtime = current_runtime_settings()
            resp = session.get(
                url,
                headers=headers,
                allow_redirects=True,
                verify=_tls_verify(),
                timeout=runtime.timeout_for(runtime.request_timeout),
            )
            callback = extract_oauth_callback_params_from_url(str(getattr(resp, "url", "") or ""))
            if not callback:
                for history in getattr(resp, "history", []) or []:
                    callback = extract_oauth_callback_params_from_url(str(history.headers.get("Location") or ""))
                    if callback:
                        break
            if callback and not _callback_state_matches(callback, expected_state):
                _append_exchange_error(errors, "OAuth redirect callback state 不匹配")
                callback = None
            if not callback:
                _append_exchange_error(
                    errors,
                    f"跟随 continue_url 后仍未拿到 callback: status={getattr(resp, 'status_code', 'unknown')}, final={_safe_url_for_log(str(getattr(resp, 'url', '') or ''))}",
                )
        except Exception as error:
            _append_exchange_error(errors, f"跟随 continue_url 异常: {str(error)[:300]}")
            callback = None
    code = str((callback or {}).get("code") or "").strip()
    if not code:
        _append_exchange_error(errors, f"未拿到 OAuth callback code: continue={_safe_url_for_log(continue_url)}")
        return None
    return request_platform_oauth_token_legacy(session, code, code_verifier, proxy, errors, fresh_session=True, fingerprint=fp)

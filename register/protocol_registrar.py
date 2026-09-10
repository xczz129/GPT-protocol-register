"""纯 HTTP 协议注册 / 登录核心类 PlatformRegistrar。"""

from __future__ import annotations

import base64
import json
import random
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlencode
from common.proxy_clearance import clearance_error, refresh_clearance
from common.session_export import build_account_record, parse_jwt_payload
from common.totp import totp_code
from common.team_workspace import select_team_workspace, validate_team_session, workspace_entries
from register.sessions import JobCancelled
from mail import mark_mailbox_code_boundary, mark_mailbox_result, release_mailbox
from mail.parsing import CANONICAL_PROVIDER_TYPES, normalize_provider_type
from proxy_util import proxy_brief
from register.protocol_pkce import generate_pkce as _generate_pkce
from register.protocol_sentinel import build_sentinel_with_so_token
from register.protocol_runtime import current_runtime_settings

from register.protocol_context import (
    PasswordResetRequired,
    _FIXED_EMAIL,
    _PASSWORD_AUTH_FLOW_GATE,
    _PROXY_CONFIG,
    _REGISTRATION_TRACE,
    _STOP_CHECK,
    _add_task_event_secrets,
    _auth_error_has_code,
    _is_cloudflare_challenge,
    _is_wrong_email_otp_error,
    _make_trace_headers,
    _mfa_activation_error_text,
    _otp_wait_timeout_message,
    _random_birthdate,
    _random_name,
    _random_password,
    _response_debug_detail,
    _response_json,
    _tls_verify,
    _totp_code,
    _truthy,
    auth_base,
    chatgpt_base,
    config,
    create_mailbox,
    platform_auth0_client,
    platform_base,
    platform_oauth_audience,
    platform_oauth_client_id,
    platform_oauth_redirect_uri,
    step,
    wait_for_code,
)

from register.protocol_fingerprint import (
    _browser_fingerprint,
    _client_hint_headers,
    _header_fingerprint,
    _make_browser_fingerprint,
    _proxy_country_code,
    common_headers,
    navigate_headers,
)

from register.protocol_http import (
    _absolute_auth_url,
    _auth_error_details_from_url,
    _auth_step_details,
    _auth_step_requires_password,
    _authorize_landed_page,
    _callback_state_matches,
    _cloudflare_block_message,
    _cookie_values,
    _headers_with_clearance,
    _is_chatgpt_callback_url,
    _nextauth_session_cookie,
    _safe_url_for_log,
    _set_oai_sc_cookie,
    _url_path,
    build_sentinel_token,
    create_session,
    exchange_tokens_from_continue_url,
    extract_oauth_callback_params_from_url,
    request_platform_oauth_token,
    request_with_local_retry,
    validate_otp,
)




class PlatformRegistrar:
    def __init__(self, proxy: str = "") -> None:
        self.proxy = str(proxy or "").strip()
        self.fingerprint = _make_browser_fingerprint(self.proxy)
        self.session = create_session(self.proxy, self.fingerprint)
        self.clearance_user_agent = ""
        self.clearance_failure_reason = ""
        self.device_id = str(uuid.uuid4())
        self.code_verifier = ""
        self.platform_auth_code = ""
        self.authorization_state = ""
        self.authorization_nonce = ""
        self._sync_session_identity()

    def close(self) -> None:
        self.session.close()

    def _sync_session_identity(self) -> None:
        """Keep the HTTP session identity aligned with the current profile."""
        fp = _browser_fingerprint(self.fingerprint)
        try:
            if hasattr(self.session, "impersonate"):
                self.session.impersonate = fp["impersonate"]
            self.session.headers.update(
                {
                    "user-agent": fp["user_agent"],
                    "oai-device-id": self.device_id,
                }
            )
            for key in ("sec-ch-ua", "sec-ch-ua-full-version-list", "sec-ch-ua-mobile", "sec-ch-ua-platform"):
                self.session.headers.pop(key, None)
            self.session.headers.update(_client_hint_headers(fp))
        except Exception:
            pass

    @staticmethod
    def _diagnostic(index: int, message: str) -> None:
        step(index, f"[协议诊断] {message}")

    def _log_identity(self, index: int) -> None:
        fp = _browser_fingerprint(self.fingerprint)
        self._diagnostic(
            index,
            "TLS/HTTP 指纹 "
            f"impersonate={fp['impersonate']} firefox={fp['full_version']} "
            f"platform=Windows/{fp['platform_version']} language={fp['accept_language']}",
        )
        self._diagnostic(index, f"User-Agent={fp['user_agent']}")
        self._diagnostic(index, f"sec-ch-ua={fp['sec_ch_ua']}")
        self._diagnostic(index, f"设备标识 oai-did={self.device_id}")
        self._diagnostic(
            index,
            f"设备画像 profile={fp.get('profile_id')} screen={fp.get('screen')} "
            f"cpu={fp.get('hardware_concurrency')} memory={fp.get('device_memory')} "
            f"locale={fp.get('locale')} tz={fp.get('timezone')} "
            f"offset={fp.get('timezone_offset_min')} webgl={fp.get('webgl_vendor')} "
            f"sentinel_sid={fp.get('sentinel_sid')}",
        )
        self._diagnostic(
            index,
            f"网络出口 country={_proxy_country_code(self.proxy)} "
            f"proxy={proxy_brief(self.proxy) if self.proxy else 'direct'}",
        )

    def _navigate_headers(self, referer: str = "") -> dict[str, str]:
        headers = _header_fingerprint(navigate_headers, self.fingerprint)
        if referer:
            headers["referer"] = referer
        return headers

    def _json_headers(self, referer: str) -> dict[str, str]:
        headers = _header_fingerprint(common_headers, self.fingerprint)
        headers["referer"] = referer
        headers["oai-device-id"] = self.device_id
        headers.update(_make_trace_headers())
        return headers

    def _refresh_cloudflare_clearance(self, target_url: str, index: int):
        self.clearance_failure_reason = ""
        step(index, "检测到 Cloudflare 拦截，正在通过 FlareSolverr 清障", "yellow")
        scoped_proxy_config = _PROXY_CONFIG.get()
        proxy_config = (
            scoped_proxy_config
            if isinstance(scoped_proxy_config, dict)
            else config.get("proxy_config")
        )
        bundle = refresh_clearance(
            proxy_config,
            target_url,
            request_proxy=self.proxy,
        )
        if bundle is None:
            detail = clearance_error()
            self.clearance_failure_reason = detail or "FlareSolverr 未返回可用 clearance，请检查服务、代理和出口 IP"
            step(index, self.clearance_failure_reason, "yellow")
            return None
        # Keep the protocol identity stable.  FlareSolverr may report its own
        # browser UA (often Chromium); it is only used for clearance cookies,
        # never as the account's Firefox fingerprint.
        self.clearance_user_agent = _browser_fingerprint(self.fingerprint)["user_agent"]
        self._sync_session_identity()
        for name, value in bundle.cookies.items():
            try:
                self.session.cookies.set(name, value, domain=".openai.com")
                self.session.cookies.set(name, value, domain="auth.openai.com")
            except Exception:
                pass
        step(index, "FlareSolverr clearance 已缓存，重试当前请求", "yellow")
        return bundle

    def _platform_authorize(self, email: str, index: int, screen_hint: str = "signup") -> str:
        step(index, "开始 platform authorize")
        self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
        self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")
        self.code_verifier, code_challenge = _generate_pkce()
        params = {
            "issuer": auth_base,
            "client_id": platform_oauth_client_id,
            "audience": platform_oauth_audience,
            "redirect_uri": platform_oauth_redirect_uri,
            "device_id": self.device_id,
            # 注册流程显式声明 signup：throwaway 域名 OpenAI 会自动当新账号走注册，
            # 但 @outlook.com/@hotmail.com 这类真实消费邮箱会被 login_or_signup 路由到登录分支，
            # 后续 user/register 落在错误的 auth step 上报 invalid_auth_step。
            "screen_hint": screen_hint,
            "max_age": "0",
            "login_hint": email,
            "scope": "openid profile email offline_access",
            "response_type": "code",
            "response_mode": "query",
            "state": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(32),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "auth0Client": platform_auth0_client,
        }
        self.authorization_state = str(params["state"])
        self.authorization_nonce = str(params["nonce"])
        self._sync_session_identity()
        self._diagnostic(
            index,
            "OAuth/PKCE "
            f"screen_hint={screen_hint} state={self.authorization_state[:12]}... "
            f"nonce={self.authorization_nonce[:12]}... verifier_len={len(self.code_verifier)} "
            f"challenge={code_challenge[:16]}... method=S256",
        )
        target_url = f"{auth_base}/api/accounts/authorize?{urlencode(params)}"
        headers = self._navigate_headers(f"{platform_base}/")
        headers = _headers_with_clearance(headers, target_url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "get", target_url, headers=headers, allow_redirects=True, verify=_tls_verify())
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(target_url, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            retry_headers = _headers_with_clearance(self._navigate_headers(f"{platform_base}/"), target_url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, "get", target_url, headers=retry_headers, allow_redirects=True, verify=_tls_verify())
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code != 200:
            err = _response_json(resp).get("error") if resp is not None else None
            # Auth0 的 error 字段可能是字符串（如 "access_denied"）而非对象，
            # 直接 .get 会在错误路径上再抛 AttributeError 掩盖真实 HTTP 错误
            if isinstance(err, dict):
                detail = f": {err.get('code', '')} - {err.get('message', '')}".strip(" -")
            else:
                detail = f": {err}".strip(" -") if err else ""
            debug = _response_debug_detail(resp)
            status = getattr(resp, "status_code", "unknown")
            raise RuntimeError(error or f"platform_authorize_http_{status}{detail}, {debug}")
        landed = _authorize_landed_page(resp)
        response_headers = getattr(resp, "headers", {}) or {}
        trace = " ".join(
            f"{name}={response_headers.get(name)}"
            for name in ("x-request-id", "cf-ray", "openai-processing-ms")
            if response_headers.get(name)
        )
        step(
            index,
            f"platform authorize 完成[{landed or '?'}] http={resp.status_code} "
            f"url={str(getattr(resp, 'url', '') or '')[:160]}{(' ' + trace) if trace else ''}",
        )
        return landed

    def _complete_chatgpt_session(
        self,
        email: str,
        password: str,
        index: int,
        *,
        start_only: bool = False,
        post_login_action: str = "",
    ) -> dict[str, Any]:
        """Finish the registered identity through ChatGPT NextAuth.

        Platform OAuth can return syntactically valid tokens before a ChatGPT
        account/workspace exists.  The NextAuth session and /backend-api/me are
        the completion boundary used by the actual ChatGPT web application.
        """
        step(index, "开始建立 ChatGPT 会话")
        fp = _browser_fingerprint(self.fingerprint)
        common = {
            "accept": "application/json",
            "accept-language": fp["accept_language"],
            "user-agent": fp["user_agent"],
        **_client_hint_headers(fp),
        }

        csrf_url = chatgpt_base + "/api/auth/csrf"

        def fetch_csrf() -> tuple[str, Any, str]:
            csrf_headers = _headers_with_clearance(
                {**common, "referer": chatgpt_base + "/"},
                csrf_url,
                self.proxy,
                self.clearance_user_agent,
            )
            csrf_response, csrf_error = request_with_local_retry(
                self.session,
                "get",
                csrf_url,
                headers=csrf_headers,
                verify=_tls_verify(),
            )
            csrf_payload = _response_json(csrf_response)
            csrf_token = str(csrf_payload.get("csrfToken") or "").strip()
            return csrf_token, csrf_response, csrf_error

        # Fast-path: /api/auth/csrf usually creates the required NextAuth CSRF
        # cookie by itself.  Only fetch the heavy homepage HTML when direct CSRF
        # fails or lands on a challenge.  This removes one high-latency request
        # from the common protocol path.
        csrf_token, csrf_response, csrf_error = fetch_csrf()
        if _is_cloudflare_challenge(csrf_response):
            bundle = self._refresh_cloudflare_clearance(csrf_url, index)
            if bundle is None:
                raise RuntimeError(
                    _cloudflare_block_message(
                        csrf_response,
                        reason=self.clearance_failure_reason,
                    )
                )
            csrf_token, csrf_response, csrf_error = fetch_csrf()
        if (
            csrf_response is None
            or csrf_response.status_code != 200
            or not csrf_token
        ):
            step(index, "CSRF fast-path 未命中，回退 ChatGPT 首页预热", "yellow")
            home_headers = {**common, "accept": "text/html,application/xhtml+xml"}
            home_headers = _headers_with_clearance(
                home_headers,
                chatgpt_base + "/",
                self.proxy,
                self.clearance_user_agent,
            )
            home, home_error = request_with_local_retry(
                self.session,
                "get",
                chatgpt_base + "/",
                retry_attempts=4,
                headers=home_headers,
                allow_redirects=True,
                verify=_tls_verify(),
            )
            if _is_cloudflare_challenge(home):
                bundle = self._refresh_cloudflare_clearance(chatgpt_base + "/", index)
                if bundle is None:
                    raise RuntimeError(
                        _cloudflare_block_message(
                            home,
                            reason=self.clearance_failure_reason,
                        )
                    )
                home_headers = _headers_with_clearance(
                    {**common, "accept": "text/html,application/xhtml+xml"},
                    chatgpt_base + "/",
                    self.proxy,
                    self.clearance_user_agent,
                )
                home, home_error = request_with_local_retry(
                    self.session,
                    "get",
                    chatgpt_base + "/",
                    retry_attempts=4,
                    headers=home_headers,
                    allow_redirects=True,
                    verify=_tls_verify(),
                )
                if _is_cloudflare_challenge(home):
                    raise RuntimeError(
                        _cloudflare_block_message(
                            home,
                            "ChatGPT 首页 clearance 重试仍被拦截",
                        )
                    )
            if home is None or int(getattr(home, "status_code", 0) or 0) >= 400:
                raise RuntimeError(
                    home_error
                    or f"chatgpt_home_http_{getattr(home, 'status_code', 'unknown')}: "
                    f"{_response_debug_detail(home)}"
                )
            csrf_token, csrf_response, csrf_error = fetch_csrf()
            if (
                csrf_response is None
                or csrf_response.status_code != 200
                or not csrf_token
            ):
                raise RuntimeError(
                    csrf_error
                    or f"chatgpt_csrf_http_{getattr(csrf_response, 'status_code', 'unknown')}"
                )

        signin_params = {
            "prompt": "login",
            "ext-passkey-client-capabilities": "11111",
            "ext-oai-did": self.device_id,
            "auth_session_logging_id": str(uuid.uuid4()),
            "screen_hint": "login_or_signup",
            "login_hint": email,
        }
        if str(password or "").strip():
            # A login_hint identifies the account but does not require the
            # password connection.  Without this hint some existing accounts
            # are routed into email OTP and password/verify then fails with
            # invalid_state even though a password is stored locally.
            signin_params.update({"connection": "password", "max_age": "0"})
            # ChatGPT uses the same NextAuth transaction for account
            # maintenance actions.  Keep this opt-in so registration and
            # ordinary credential login retain their existing query shape.
            action = str(post_login_action or "").strip().lower().replace("-", "_")
            if action in {"password_reset", "reset_password"}:
                signin_params.update(
                    {
                        "reauth": "password",
                        "post_login_password_reset": "true",
                    }
                )
            elif action in {"password_add", "add_password"}:
                signin_params.update(
                    {
                        "reauth": "password",
                        "post_login_add_password": "true",
                    }
                )
        query = urlencode(signin_params)
        signin_url = f"{chatgpt_base}/api/auth/signin/openai?{query}"
        signin_headers = _headers_with_clearance(
            {
                **common,
                "content-type": "application/x-www-form-urlencoded",
                "origin": chatgpt_base,
                "referer": chatgpt_base + "/",
            },
            signin_url,
            self.proxy,
            self.clearance_user_agent,
        )
        signin_response, signin_error = request_with_local_retry(
            self.session,
            "post",
            signin_url,
            headers=signin_headers,
            data={
                "callbackUrl": chatgpt_base + "/",
                "csrfToken": csrf_token,
                "json": "true",
            },
            allow_redirects=False,
            verify=_tls_verify(),
        )
        signin_payload = _response_json(signin_response)
        authorize_url = str(
            signin_payload.get("url")
            or getattr(signin_response, "headers", {}).get("Location")
            or ""
        ).strip()
        if (
            signin_response is None
            or signin_response.status_code >= 400
            or not authorize_url
        ):
            raise RuntimeError(
                signin_error
                or f"chatgpt_signin_http_{getattr(signin_response, 'status_code', 'unknown')}"
            )

        authorize_headers = _headers_with_clearance(
            {**common, "accept": "text/html,application/xhtml+xml", "referer": chatgpt_base + "/"},
            authorize_url,
            self.proxy,
            self.clearance_user_agent,
        )
        authorize_response, authorize_error = request_with_local_retry(
            self.session,
            "get",
            authorize_url,
            headers=authorize_headers,
            allow_redirects=True,
            verify=_tls_verify(),
        )
        # The authorize URL is served by auth.openai.com and can be challenged
        # independently from the ChatGPT CSRF/home requests above.  In the HAR
        # flow this appears as a 403 Cloudflare page followed by a successful
        # redirect after clearance is refreshed.  Treat it like the other
        # authenticated navigation steps instead of surfacing a transient
        # challenge as a permanent login failure.
        if _is_cloudflare_challenge(authorize_response):
            bundle = self._refresh_cloudflare_clearance(authorize_url, index)
            if bundle is None:
                raise RuntimeError(
                    _cloudflare_block_message(
                        authorize_response,
                        reason=self.clearance_failure_reason,
                    )
                )
            authorize_headers = _headers_with_clearance(
                {**common, "accept": "text/html,application/xhtml+xml", "referer": chatgpt_base + "/"},
                authorize_url,
                self.proxy,
                self.clearance_user_agent,
            )
            authorize_response, authorize_error = request_with_local_retry(
                self.session,
                "get",
                authorize_url,
                retry_attempts=5,
                headers=authorize_headers,
                allow_redirects=True,
                verify=_tls_verify(),
            )
            if _is_cloudflare_challenge(authorize_response):
                raise RuntimeError(
                    _cloudflare_block_message(
                        authorize_response,
                        "ChatGPT authorize clearance 重试仍被拦截",
                    )
                )
        final_url = str(getattr(authorize_response, "url", "") or "")
        auth_error = _auth_error_details_from_url(final_url)
        if (
            authorize_response is None
            or authorize_response.status_code >= 400
            or auth_error
        ):
            if auth_error:
                code = auth_error.get("code") or "unknown"
                request_id = auth_error.get("request_id") or ""
                request_hint = f" request_id={request_id}" if request_id else ""
                step(
                    index,
                    f"初始认证进入错误页 · code={code}{request_hint}",
                    "yellow",
                )
                raise RuntimeError(
                    f"auth_step_error_page: code={code}{request_hint}"
                )
            raise RuntimeError(
                authorize_error
                or f"chatgpt_authorize_http_{getattr(authorize_response, 'status_code', 'unknown')}"
            )

        device_cookie = str(_cookie_values(self.session).get("oai-did") or "").strip()
        if device_cookie:
            self.device_id = device_cookie
            self._sync_session_identity()
        if start_only:
            step(
                index,
                (
                    "ChatGPT NextAuth 凭据认证事务已建立（邮箱仅作账号标识）"
                    if str(password or "").strip()
                    else "ChatGPT NextAuth 初始化完成，等待邮箱验证码"
                ),
            )
            return {
                "authorize_url": authorize_url,
                "final_url": final_url,
                "device_id": self.device_id,
            }

        return self._read_chatgpt_session(email, password, index)

    def _navigate_auth_step(self, url: str, referer: str, index: int) -> Any:
        target = _absolute_auth_url(url)
        if not target:
            return None
        headers = _headers_with_clearance(
            self._navigate_headers(referer),
            target,
            self.proxy,
            self.clearance_user_agent,
        )
        response, error = request_with_local_retry(
            self.session,
            "get",
            target,
            retry_attempts=5,
            headers=headers,
            allow_redirects=True,
            verify=_tls_verify(),
        )
        if _is_cloudflare_challenge(response):
            bundle = self._refresh_cloudflare_clearance(target, index)
            if bundle is None:
                raise RuntimeError(
                    _cloudflare_block_message(
                        response,
                        reason=self.clearance_failure_reason,
                    )
                )
            headers = _headers_with_clearance(
                self._navigate_headers(referer),
                target,
                self.proxy,
                self.clearance_user_agent,
            )
            response, error = request_with_local_retry(
                self.session,
                "get",
                target,
                retry_attempts=5,
                headers=headers,
                allow_redirects=True,
                verify=_tls_verify(),
            )
            if _is_cloudflare_challenge(response):
                raise RuntimeError(
                    _cloudflare_block_message(
                        response,
                        "认证跳转 clearance 重试仍被拦截",
                    )
                )
        if response is None or int(getattr(response, "status_code", 0) or 0) >= 400:
            raise RuntimeError(
                error
                or f"auth_step_http_{getattr(response, 'status_code', 'unknown')}: "
                f"{_safe_url_for_log(target)}, {_response_debug_detail(response)}"
            )
        final_url = str(getattr(response, "url", "") or target)
        auth_error = _auth_error_details_from_url(final_url)
        if auth_error:
            code = auth_error.get("code") or "unknown"
            request_id = auth_error.get("request_id") or ""
            request_hint = f" request_id={request_id}" if request_id else ""
            step(
                index,
                f"认证步骤进入错误页 · code={code}{request_hint}",
                "yellow",
            )
            raise RuntimeError(
                f"auth_step_error_page: code={code}{request_hint}"
            )
        step(
            index,
            f"认证步骤已跟随 · {getattr(response, 'status_code', 0)} "
            f"{_safe_url_for_log(final_url)}",
        )
        return response

    def _post_auth_json(
        self,
        url: str,
        referer: str,
        payload: dict[str, Any],
        index: int,
        operation: str,
        *,
        flow_invocation: bool = False,
    ) -> dict[str, Any]:
        headers = self._json_headers(referer)
        if operation == "password_verify":
            headers["openai-sentinel-token"] = build_sentinel_token(
                self.session,
                self.device_id,
                "password_verify",
                self.fingerprint,
                lambda message: self._diagnostic(index, message),
            )
        if operation in {"password_verify", "mfa_verify"} or flow_invocation:
            headers["x-access-flow-invocation-id"] = str(uuid.uuid4())
        headers = _headers_with_clearance(
            headers, url, self.proxy, self.clearance_user_agent
        )
        response, error = request_with_local_retry(
            self.session,
            "post",
            url,
            json=payload,
            headers=headers,
            allow_redirects=False,
            verify=_tls_verify(),
        )
        data = _response_json(response)
        if response is None or response.status_code != 200:
            error_data = data.get("error") if isinstance(data, dict) else {}
            error_code = (
                str(error_data.get("code") or "").strip()
                if isinstance(error_data, dict)
                else str(error_data or "").strip()
            )
            detail = f": {error_code}" if error_code else ""
            raise RuntimeError(
                error
                or f"{operation}_http_{getattr(response, 'status_code', 'unknown')}{detail}"
            )
        if isinstance(data.get("error"), dict):
            error_data = data["error"]
            raise RuntimeError(
                f"{operation}_failed: {str(error_data.get('code') or error_data)[:160]}"
            )
        return data

    def _dump_client_auth_session(self, referer: str, index: int) -> dict[str, Any]:
        """Refresh the auth-session checksum observed during the MFA page load."""
        url = f"{auth_base}/api/accounts/client_auth_session_dump"
        headers = _headers_with_clearance(
            self._navigate_headers(referer),
            url,
            self.proxy,
            self.clearance_user_agent,
        )
        response, error = request_with_local_retry(
            self.session,
            "get",
            url,
            headers=headers,
            allow_redirects=False,
            verify=_tls_verify(),
        )
        if response is None or response.status_code != 200:
            step(
                index,
                f"认证会话同步跳过 · {error or getattr(response, 'status_code', 'unknown')}",
                "yellow",
            )
            return {}
        return _response_json(response)

    @staticmethod
    def _totp_factor(data: dict[str, Any]) -> tuple[str, str]:
        page = data.get("page") if isinstance(data.get("page"), dict) else {}
        payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
        factors = payload.get("factors") if isinstance(payload.get("factors"), list) else []
        factor = next(
            (
                item
                for item in factors
                if isinstance(item, dict)
                and str(item.get("factor_type") or "").strip().lower() == "totp"
                and not _truthy(item.get("is_recovery"))
            ),
            None,
        )
        factor_id = str(payload.get("factor_id") or "").strip()
        if not factor_id and isinstance(factor, dict):
            factor_id = str(factor.get("id") or "").strip()
        factor_type = str((factor or {}).get("factor_type") or "").strip().lower()
        return factor_id, factor_type

    def _auth_session_workspaces(self, *auth_payloads: Any) -> list[dict[str, Any]]:
        """Prefer the current auth response; some responses do not update the cookie."""
        # The first payload is the current MFA/dump response. Older snapshots
        # (e.g. the pre-MFA dump) must not hide an updated post-MFA cookie.
        for payload in auth_payloads[:1]:
            items = workspace_entries(payload)
            if items:
                return items
        for domain in (".auth.openai.com", "auth.openai.com", None):
            try:
                raw = self.session.cookies.get("oai-client-auth-session", **({"domain": domain} if domain else {}))
            except Exception:
                continue
            items = workspace_entries(raw)
            if items:
                return items
        for payload in auth_payloads[1:]:
            items = workspace_entries(payload)
            if items:
                return items
        return []

    def _select_workspace(
        self, index: int, workspace_name: str, referer: str,
        *, require_team_workspace: bool = False, auth_payloads: tuple[Any, ...] = (),
    ) -> str:
        """Select the requested workspace before consuming its callback."""
        workspaces = self._auth_session_workspaces(*auth_payloads)
        if not workspaces and require_team_workspace:
            refreshed = self._dump_client_auth_session(referer, index)
            workspaces = self._auth_session_workspaces(refreshed, *auth_payloads)
        if not workspaces:
            raise RuntimeError("workspace_page_without_workspaces: 同步认证会话后仍无工作区，未选择个人空间")
        wanted = str(workspace_name or "").strip().lower()
        selected = select_team_workspace(workspaces, workspace_name) if require_team_workspace else None
        if wanted and selected is None:
            for item in workspaces:
                label = str(
                    item.get("name")
                    or item.get("title")
                    or item.get("label")
                    or ""
                ).strip().lower()
                if label == wanted:
                    selected = item
                    break
            if selected is None:
                available = "、".join(
                    str(
                        item.get("name")
                        or item.get("title")
                        or item.get("label")
                        or item.get("id")
                    )
                    for item in workspaces
                )
                raise RuntimeError(
                    "workspace_name_not_found: "
                    f"未找到工作区「{workspace_name}」，可用工作区: {available[:200]}"
                )
        if selected is None:
            selected = workspaces[0]
        workspace_id = str(selected.get("id"))
        label = str(
            selected.get("name") or selected.get("title") or selected.get("label") or workspace_id
        )
        step(index, f"检测到工作区选择页(Team)，选择工作区: {label}", "yellow")
        url = f"{auth_base}/api/accounts/workspace/select"
        data = self._post_auth_json(
            url,
            referer,
            {"workspace_id": workspace_id},
            index,
            "workspace_select",
        )
        continue_url = str(data.get("continue_url") or "").strip()
        if not continue_url:
            page = data.get("page") if isinstance(data.get("page"), dict) else {}
            payload = page.get("payload") if isinstance(page, dict) else {}
            continue_url = str((payload or {}).get("url") or "").strip()
        if not continue_url:
            raise RuntimeError(
                "workspace_select_missing_continue: "
                f"page={str(data.get('page') or '?')[:160]}"
            )
        self._selected_workspace_id = workspace_id
        self._selected_workspace_name = label
        # 抓包确认: workspace/select 的 continue_url 直接就是 chatgpt.com 的
        # OAuth callback URL，返回给调用方统一跟随（与个人账号 MFA 后流程一致）
        return continue_url

    def login_with_password_totp(
        self,
        email: str,
        password: str,
        totp_secret: str,
        index: int,
        workspace_name: str = "",
        require_team_workspace: bool = False,
    ) -> dict[str, Any]:
        """Complete the HAR-observed password plus TOTP login transaction."""
        email = str(email or "").strip()
        password = str(password or "").strip()
        totp_secret = str(totp_secret or "").strip()
        self._selected_workspace_id = ""
        self._selected_workspace_name = ""
        if not email:
            raise RuntimeError("协议凭据登录缺少邮箱")
        if not password or not totp_secret:
            raise RuntimeError("协议凭据登录要求同时具备密码和 TOTP 2FA")

        auth_start = self._complete_chatgpt_session(
            email,
            password,
            index,
            start_only=True,
        )
        landed_url = str(auth_start.get("final_url") or "").strip()
        if _url_path(landed_url) != "/log-in/password":
            identity_result = self._authorize_continue_login(
                email,
                index,
                passwordless=False,
                restart_invalid_state=False,
            )
            identity_url, identity_page = _auth_step_details(identity_result)
            if not _auth_step_requires_password(identity_url, identity_page):
                raise RuntimeError(
                    "credential_login_password_route_unavailable: "
                    f"page={identity_page or '?'} "
                    f"continue={_safe_url_for_log(identity_url)}; "
                    "服务端将该账号导向邮箱验证，未进入密码认证"
                )
        step(
            index,
            "协议凭据登录[1/6] 邮箱账号已识别（未读取验证码），准备密码验证",
        )
        password_url = f"{auth_base}/api/accounts/password/verify"
        password_page = f"{auth_base}/log-in/password"
        password_result = self._post_auth_json(
            password_url,
            password_page,
            {"password": password},
            index,
            "password_verify",
        )
        factor_id, factor_type = self._totp_factor(password_result)
        continue_url, page_type = _auth_step_details(password_result)
        step(
            index,
            "协议凭据登录[2/6] 密码验证返回 · "
            f"page={page_type or '?'} · "
            f"TOTP因子={'已返回' if factor_id and factor_type == 'totp' else '缺失'}",
        )
        if page_type != "mfa_challenge" or not factor_id or factor_type != "totp":
            raise RuntimeError(
                f"password_verify_missing_totp_factor: page={page_type or '?'}"
            )
        mfa_referer = _absolute_auth_url(continue_url) or f"{auth_base}/mfa-challenge"
        session_dump = self._dump_client_auth_session(password_page, index)
        step(
            index,
            "协议凭据登录[3/6] "
            + (
                "认证会话同步完成，准备签发 TOTP challenge"
                if session_dump
                else "认证会话同步未返回数据，继续签发 TOTP challenge"
            ),
        )

        issue_url = f"{auth_base}/api/accounts/mfa/issue_challenge"
        self._post_auth_json(
            issue_url,
            password_page,
            {"id": factor_id, "type": "totp", "force_fresh_challenge": False},
            index,
            "mfa_issue_challenge",
        )
        step(index, "协议凭据登录[4/6] TOTP challenge 已签发 · factor_type=totp")
        code = totp_code(totp_secret)
        step(index, "协议凭据登录[5/6] TOTP 验证码已生成，提交 MFA 校验（验证码不写入日志）")
        verify_url = f"{auth_base}/api/accounts/mfa/verify"
        verified = self._post_auth_json(
            verify_url,
            mfa_referer,
            {"id": factor_id, "type": "totp", "code": code},
            index,
            "mfa_verify",
        )
        step(index, "协议凭据登录[6/6] MFA 校验通过，准备跟随 OAuth callback")
        callback_url, callback_page = _auth_step_details(verified)
        workspace_page = "workspace" in f"{callback_page or ''} {callback_url or ''}".lower()
        if require_team_workspace:
            # An immediate callback can represent the personal workspace. Select
            # the team while the auth transaction is still live, BEFORE following
            # either callback. Never replay a consumed callback as a fallback.
            workspace_referer = _absolute_auth_url(callback_url) if workspace_page else mfa_referer
            step(index, "Team 导入：强制选择团队工作区，不接受个人空间")
            callback_url = self._select_workspace(
                index, workspace_name, workspace_referer or mfa_referer,
                require_team_workspace=True,
                auth_payloads=(verified, session_dump),
            )
            if not _is_chatgpt_callback_url(callback_url):
                raise RuntimeError("team_workspace_missing_callback: 团队选择未完成，未继续个人登录")
        elif not _is_chatgpt_callback_url(callback_url):
            # Team 账号 MFA 后会进入工作区选择页：选择目标工作区后拿新 callback
            # 抓包确认: select 请求的 Referer 必须是 workspace 页面本身
            if "workspace" in f"{callback_page or ''} {callback_url or ''}".lower():
                workspace_referer = (
                    _absolute_auth_url(callback_url) or mfa_referer
                )
                callback_url = self._select_workspace(
                    index, workspace_name, workspace_referer
                )
            if not _is_chatgpt_callback_url(callback_url):
                raise RuntimeError(
                    "mfa_verify_missing_callback: "
                    f"page={callback_page or '?'} continue={_safe_url_for_log(callback_url)}"
                )
        callback_response = self._navigate_auth_step(
            callback_url,
            mfa_referer,
            index,
        )
        step(index, "OAuth callback 跟随完成，开始读取 ChatGPT 会话")
        final_url = str(getattr(callback_response, "url", "") or "")
        if not (_is_chatgpt_callback_url(callback_url) or _is_chatgpt_callback_url(final_url)):
            raise RuntimeError(
                "协议凭据登录未完成 OAuth callback: "
                f"continue={_safe_url_for_log(callback_url)} "
                f"final={_safe_url_for_log(final_url)}"
            )
        tokens = self._read_chatgpt_session(email, password, index)
        if require_team_workspace:
            identity = validate_team_session(tokens, selected_workspace_id=self._selected_workspace_id)
            tokens["selected_workspace_id"] = self._selected_workspace_id
            tokens["selected_workspace_name"] = self._selected_workspace_name
            tokens["workspace_selection_verified"] = True
            tokens["user_id"] = identity["user_id"]
            step(index, f"Team 工作区验证通过 · 套餐={identity['plan_type']}")
        step(index, "ChatGPT 会话读取完成，AT/Refresh 会话校验通过")
        tokens["mfa_factor_id"] = factor_id
        tokens["mfa_factor_type"] = "totp"
        return tokens

    def change_password(
        self,
        email: str,
        old_password: str,
        new_password: str,
        totp_secret: str,
        index: int,
    ) -> dict[str, Any]:
        """Change an existing password through one re-auth transaction.

        The password-reset flag is attached to the initial NextAuth authorize
        request.  After password and TOTP verification, the reset POST is
        issued before following the OAuth callback, matching the browser HAR.
        """
        email = str(email or "").strip()
        old_password = str(old_password or "").strip()
        new_password = str(new_password or "").strip()
        totp_secret = str(totp_secret or "").strip()
        if not email:
            raise RuntimeError("change_password_missing_email")
        if not old_password or not new_password:
            raise RuntimeError("change_password_requires_old_and_new_password")
        if not totp_secret:
            raise RuntimeError("change_password_requires_totp_secret")
        if len(new_password) < 12:
            raise RuntimeError("change_password_requires_minimum_12_characters")
        if old_password == new_password:
            raise RuntimeError("change_password_new_password_matches_old")

        auth_start = self._complete_chatgpt_session(
            email,
            old_password,
            index,
            start_only=True,
            post_login_action="password_reset",
        )
        landed_url = str(auth_start.get("final_url") or "").strip()
        if _url_path(landed_url) != "/log-in/password":
            raise RuntimeError(
                "change_password_password_route_unavailable: "
                f"final={_safe_url_for_log(landed_url)}"
            )
        step(index, "改密[1/6] 已建立密码重置认证事务，提交旧密码")

        password_result = self._post_auth_json(
            f"{auth_base}/api/accounts/password/verify",
            f"{auth_base}/log-in/password",
            {"password": old_password},
            index,
            "password_verify",
        )
        factor_id, factor_type = self._totp_factor(password_result)
        continue_url, page_type = _auth_step_details(password_result)
        if page_type != "mfa_challenge" or not factor_id or factor_type != "totp":
            raise RuntimeError(
                f"change_password_missing_totp_factor: page={page_type or '?'}"
            )
        mfa_referer = _absolute_auth_url(continue_url) or f"{auth_base}/mfa-challenge"
        self._post_auth_json(
            f"{auth_base}/api/accounts/mfa/issue_challenge",
            f"{auth_base}/log-in/password",
            {"id": factor_id, "type": "totp", "force_fresh_challenge": False},
            index,
            "mfa_issue_challenge",
            flow_invocation=True,
        )
        code = _totp_code(totp_secret)
        verified = self._post_auth_json(
            f"{auth_base}/api/accounts/mfa/verify",
            mfa_referer,
            {"id": factor_id, "type": "totp", "code": code},
            index,
            "mfa_verify",
        )
        verify_url, verify_page = _auth_step_details(verified)
        step(
            index,
            "改密[2/6] MFA 校验通过，" 
            + ("进入新密码页面" if _url_path(verify_url) == "/reset-password/new-password" else "准备提交密码重置"),
        )

        # A successful MFA response normally points to the reset form.  Some
        # edge responses expose the callback first; in that case there is no
        # second reset POST to issue and we simply continue the callback.
        callback_url = ""
        if _url_path(verify_url) == "/reset-password/new-password" or verify_page in {
            "reset_password_new_password",
            "password_reset_new_password",
        }:
            reset_result = self._add_password(
                new_password,
                index,
                reset_existing=True,
            )
            callback_url, reset_page = _auth_step_details(reset_result)
            if not callback_url and reset_page == "reset_password_success":
                # The success page may require one explicit navigation before
                # the callback becomes available in the session redirect.
                success_url = f"{auth_base}/reset-password/success"
                success_response = self._navigate_auth_step(
                    success_url,
                    f"{auth_base}/reset-password/new-password",
                    index,
                )
                success_final = str(getattr(success_response, "url", "") or "")
                if _is_chatgpt_callback_url(success_final):
                    callback_url = success_final
        elif _is_chatgpt_callback_url(verify_url):
            callback_url = verify_url
        else:
            raise RuntimeError(
                "change_password_unexpected_mfa_step: "
                f"page={verify_page or '?'} continue={_safe_url_for_log(verify_url)}"
            )

        if callback_url:
            callback_response = self._navigate_auth_step(
                callback_url,
                f"{auth_base}/reset-password/new-password",
                index,
            )
            final_url = str(getattr(callback_response, "url", "") or "")
            if not (_is_chatgpt_callback_url(callback_url) or _is_chatgpt_callback_url(final_url)):
                raise RuntimeError(
                    "change_password_callback_not_followed: "
                    f"continue={_safe_url_for_log(callback_url)}"
                )
        tokens = self._read_chatgpt_session(email, new_password, index)
        tokens = dict(tokens or {})
        tokens.update(
            {
                "password": new_password,
                "password_set": True,
                "mfa_factor_id": factor_id,
                "mfa_factor_type": "totp",
                "totp_secret": totp_secret,
                "mfa_enabled": True,
            }
        )
        step(index, "改密完成，已读取新密码对应的 ChatGPT 会话")
        return tokens

    # Explicit aliases keep callers that use the UI terminology independent
    # of the internal password-reset endpoint name.
    reset_password_with_totp = change_password
    change_password_with_totp = change_password

    def _read_chatgpt_session(
        self,
        email: str,
        password: str,
        index: int,
    ) -> dict[str, Any]:
        fp = _browser_fingerprint(self.fingerprint)
        common = {
            "accept": "application/json",
            "accept-language": fp["accept_language"],
            "user-agent": fp["user_agent"],
            **_client_hint_headers(fp),
        }
        last_error = ""
        for attempt in range(3):
            session_url = f"{chatgpt_base}/api/auth/session?update={int(time.time() * 1000)}"
            session_headers = _headers_with_clearance(
                {**common, "referer": chatgpt_base + "/"},
                session_url,
                self.proxy,
                self.clearance_user_agent,
            )
            session_response, session_error = request_with_local_retry(
                self.session,
                "get",
                session_url,
                headers=session_headers,
                verify=_tls_verify(),
            )
            payload = _response_json(session_response)
            session_cookie = _nextauth_session_cookie(self.session)
            record = build_account_record(
                payload,
                email=email,
                password=password,
                proxy=self.proxy,
                session_cookie=session_cookie,
                browser_type=f"curl_cffi/{fp['impersonate']}",
            )
            access_token = str(record.get("access_token") or "").strip()
            account_id = str(record.get("account_id") or "").strip()
            if (
                session_response is not None
                and session_response.status_code == 200
                and access_token
                and session_cookie
                and account_id
            ):
                token_payload = parse_jwt_payload(access_token) or {}
                if current_runtime_settings().verify_chatgpt_me:
                    me_url = chatgpt_base + "/backend-api/me"
                    me_headers = _headers_with_clearance(
                        {
                            **common,
                            "authorization": f"Bearer {access_token}",
                            "origin": chatgpt_base,
                            "referer": chatgpt_base + "/",
                            "oai-device-id": self.device_id,
                        },
                        me_url,
                        self.proxy,
                        self.clearance_user_agent,
                    )
                    me_response, me_error = request_with_local_retry(
                        self.session,
                        "get",
                        me_url,
                        headers=me_headers,
                        verify=_tls_verify(),
                    )
                    if me_response is None or me_response.status_code != 200:
                        last_error = me_error or f"chatgpt_me_http_{getattr(me_response, 'status_code', 'unknown')}"
                        if attempt < 2:
                            time.sleep(1.0)
                            continue
                        break
                    step(
                        index,
                        "ChatGPT 会话验证完成 · "
                        f"Account ID {account_id[:12]}… client_id="
                        f"{str(token_payload.get('client_id') or '?')}",
                    )
                else:
                    step(
                        index,
                        "ChatGPT 会话已保存 · 跳过 /backend-api/me 快速验证",
                        "yellow",
                    )
                return {
                    "access_token": access_token,
                    "session_token": session_cookie,
                    "account_id": account_id,
                    "plan_type": str(record.get("plan_type") or "free"),
                    "expires_at": str(record.get("expires_at") or ""),
                    "session": payload,
                    "cookies": _cookie_values(self.session),
                    "profile": payload.get("account")
                    if isinstance(payload.get("account"), dict)
                    else {},
                    "oauth_client_id": str(token_payload.get("client_id") or ""),
                }
            else:
                missing = []
                if not access_token:
                    missing.append("access_token")
                if not session_cookie:
                    missing.append("session_token")
                if not account_id:
                    missing.append("account_id")
                last_error = session_error or "ChatGPT session 缺少 " + ", ".join(missing)
            if attempt < 2:
                time.sleep(1.0)
        raise RuntimeError(f"chatgpt_session_incomplete: {last_error[:300]}")

    def _chatgpt_mfa_request(
        self,
        method: str,
        path: str,
        access_token: str,
        index: int,
        payload: dict[str, Any] | None = None,
    ) -> tuple[Any, str]:
        token = str(access_token or "").strip()
        if not token:
            raise RuntimeError("mfa_missing_access_token")
        fp = _browser_fingerprint(self.fingerprint)
        headers = _header_fingerprint(
            {
                "accept": "application/json",
                "authorization": f"Bearer {token}",
                "referer": f"{chatgpt_base}/",
                "oai-device-id": self.device_id,
            },
            fp,
        )
        if payload is not None:
            headers["content-type"] = "application/json"
            headers["origin"] = chatgpt_base
        url = f"{chatgpt_base}{path}"
        headers = _headers_with_clearance(
            headers,
            url,
            self.proxy,
            str(getattr(self, "clearance_user_agent", "") or ""),
        )
        kwargs: dict[str, Any] = {
            "headers": headers,
            "verify": _tls_verify(),
        }
        if payload is not None:
            kwargs["json"] = payload
        return request_with_local_retry(self.session, method, url, **kwargs)

    def _enroll_totp(
        self,
        tokens: dict[str, Any],
        index: int,
        replace_existing: bool = False,
    ) -> dict[str, Any]:
        """Enroll and activate a TOTP factor.

        ``replace_existing`` follows the account-settings flow: disable the
        currently active in-house factor, then enroll and activate a fresh
        secret in the same authenticated ChatGPT session.  The default keeps
        the registration path's original add-only behavior.
        """
        access_token = str(tokens.get("access_token") or "").strip()
        replacing_active_factor = False
        info_response, info_error = self._chatgpt_mfa_request(
            "get",
            "/backend-api/accounts/mfa_info",
            access_token,
            index,
        )
        if info_response is None or info_response.status_code != 200:
            raise RuntimeError(
                info_error
                or f"mfa_info_http_{getattr(info_response, 'status_code', 'unknown')}"
            )
        initial_info = _response_json(info_response)
        if _truthy(initial_info.get("mfa_enabled")) or _truthy(
            initial_info.get("mfa_enabled_v2")
        ):
            if not replace_existing:
                raise RuntimeError("mfa_already_enabled_without_new_totp_secret")
            factors_payload = initial_info.get("factors")
            totp_factors = (
                factors_payload.get("totp")
                if isinstance(factors_payload, dict)
                else factors_payload
            )
            if not isinstance(totp_factors, list):
                totp_factors = []
            # ``factors.totp`` is an explicit TOTP collection, and some API
            # responses omit ``factor_type`` on its elements.  A bare/list
            # ``factors`` payload still requires an explicit type so a
            # passkey or another provider can never be selected implicitly.
            factors_are_explicit_totp = isinstance(factors_payload, dict) and isinstance(
                factors_payload.get("totp"), list
            )
            old_factor_id = ""
            for item in totp_factors:
                if not isinstance(item, dict) or _truthy(item.get("is_recovery")):
                    continue
                factor_type = str(
                    item.get("factor_type") or item.get("type") or ""
                ).strip().lower()
                if factor_type and factor_type != "totp":
                    continue
                if not factors_are_explicit_totp and factor_type != "totp":
                    continue
                old_factor_id = str(item.get("id") or "").strip()
                if old_factor_id:
                    break
            if not old_factor_id:
                raise RuntimeError("mfa_enabled_totp_factor_id_missing")
            if _STOP_CHECK.get()():
                raise JobCancelled("stop requested")
            disable_response, disable_error = self._chatgpt_mfa_request(
                "post",
                "/backend-api/accounts/mfa/user/disable_in_house",
                access_token,
                index,
                {"factor_id": old_factor_id},
            )
            if disable_response is None or disable_response.status_code != 200:
                detail = _response_json(disable_response)
                raise RuntimeError(
                    disable_error
                    or f"mfa_disable_http_{getattr(disable_response, 'status_code', 'unknown')}: "
                    f"{json.dumps(detail, ensure_ascii=False)[:240]}"
                )
            replacing_active_factor = True
            step(index, f"旧 TOTP 因子已停用 · factor_id={old_factor_id[:12]}…")
            # The settings UI refreshes mfa_info after disabling.  Confirm the
            # server has observed the transition before creating a replacement.
            disabled_response = None
            disabled_error = ""
            disabled_info: dict[str, Any] = {}
            # The disable endpoint is eventually consistent.  Match the UI's
            # observed confirmation window with 1s, 2s and 4s backoff (7s
            # total) while checking cancellation between short sleep chunks.
            disable_confirm_delays = (1.0, 2.0, 4.0)
            for disable_attempt in range(len(disable_confirm_delays) + 1):
                if _STOP_CHECK.get()():
                    raise JobCancelled("stop requested")
                disabled_response, disabled_error = self._chatgpt_mfa_request(
                    "get",
                    "/backend-api/accounts/mfa_info",
                    access_token,
                    index,
                )
                disabled_info = _response_json(disabled_response)
                if (
                    disabled_response is not None
                    and disabled_response.status_code == 200
                    and not (
                        _truthy(disabled_info.get("mfa_enabled"))
                        or _truthy(disabled_info.get("mfa_enabled_v2"))
                    )
                ):
                    break
                if disable_attempt < len(disable_confirm_delays):
                    delay = disable_confirm_delays[disable_attempt]
                    step(
                        index,
                        f"等待旧 TOTP 停用确认 · {delay:g}s 后重试",
                        "yellow",
                    )
                    remaining = delay
                    while remaining > 0:
                        if _STOP_CHECK.get()():
                            raise JobCancelled("stop requested")
                        chunk = min(0.25, remaining)
                        time.sleep(chunk)
                        remaining -= chunk
            if (
                disabled_response is None
                or disabled_response.status_code != 200
                or _truthy(disabled_info.get("mfa_enabled"))
                or _truthy(disabled_info.get("mfa_enabled_v2"))
            ):
                raise RuntimeError(disabled_error or "mfa_disable_not_confirmed")

        security_response, security_error = self._chatgpt_mfa_request(
            "get",
            "/backend-api/accounts/security_settings/info",
            access_token,
            index,
        )
        if security_response is None or security_response.status_code != 200:
            raise RuntimeError(
                security_error
                or "security_settings_info_http_"
                f"{getattr(security_response, 'status_code', 'unknown')}"
            )
        security_info = _response_json(security_response)
        if security_info.get("aas_eligible") is False:
            raise RuntimeError("mfa_enrollment_not_eligible")

        enroll_response, enroll_error = self._chatgpt_mfa_request(
            "post",
            "/backend-api/accounts/mfa/enroll",
            access_token,
            index,
            {"factor_type": "totp"},
        )
        if enroll_response is None or enroll_response.status_code != 200:
            raise RuntimeError(
                enroll_error
                or f"mfa_enroll_http_{getattr(enroll_response, 'status_code', 'unknown')}"
            )
        enrollment = _response_json(enroll_response)
        secret = str(enrollment.get("secret") or "").strip()
        session_id = str(enrollment.get("session_id") or "").strip()
        factor = enrollment.get("factor") if isinstance(enrollment.get("factor"), dict) else {}
        factor_id = str(factor.get("id") or "").strip()
        if not secret or not session_id or not factor_id:
            raise RuntimeError("mfa_enroll_incomplete_response")
        registration_trace = _REGISTRATION_TRACE.get()
        if registration_trace is not None:
            registration_trace.set_secrets(secret)
        _add_task_event_secrets(secret)

        activate_response = None
        activate_error = ""
        activate_data: dict[str, Any] = {}
        previous_code = ""
        for activate_attempt in range(2):
            code = _totp_code(secret)
            if activate_attempt and code == previous_code:
                for _ in range(32):
                    # After disabling the old factor this becomes a critical
                    # section: finish activating the replacement even if the
                    # batch receives a stop request, otherwise the account is
                    # left with MFA disabled and the stored secret invalid.
                    if _STOP_CHECK.get()() and not replacing_active_factor:
                        raise JobCancelled("stop requested")
                    time.sleep(1.0)
                    code = _totp_code(secret)
                    if code != previous_code:
                        break
            activate_response, activate_error = self._chatgpt_mfa_request(
                "post",
                "/backend-api/accounts/mfa/user/activate_enrollment",
                access_token,
                index,
                {
                    "code": code,
                    "factor_type": "totp",
                    "session_id": session_id,
                },
            )
            activate_data = _response_json(activate_response)
            activation_error_payload: Any = activate_data
            if not activation_error_payload and activate_response is not None:
                raw_activation_body = str(
                    getattr(activate_response, "text", "") or ""
                ).strip()
                if raw_activation_body:
                    try:
                        activation_error_payload = json.loads(raw_activation_body)
                    except Exception:
                        activation_error_payload = raw_activation_body
            if (
                activate_response is not None
                and activate_response.status_code == 200
                and activate_data.get("success") is True
            ):
                break
            # Depending on the edge and the current TOTP window, the API may
            # return HTTP 200 with ``success=false`` or HTTP 403 with an
            # ``invalid_code`` detail (sometimes encoded as JSON text).  Both
            # represent a retryable stale code; other 403 responses are hard
            # authorization failures.
            activation_error_text = _mfa_activation_error_text(
                activation_error_payload
            )
            retryable_invalid_code = (
                "invalid_code" in activation_error_text
                or "invalid code" in activation_error_text
            )
            if (
                activate_attempt == 0
                and activate_response is not None
                and (
                    activate_response.status_code == 200
                    or (
                        activate_response.status_code == 403
                        and retryable_invalid_code
                    )
                )
            ):
                previous_code = code
                step(
                    index,
                    "TOTP 激活码被拒绝，等待下一个验证码时间窗后重试",
                    "yellow",
                )
                continue
            break
        if (
            activate_response is None
            or activate_response.status_code != 200
            or activate_data.get("success") is not True
        ):
            detail = str(
                activate_data.get("error")
                or activate_data.get("detail")
                or "activation rejected"
            )
            raise RuntimeError(
                activate_error
                or f"mfa_activate_failed: {detail[:200]}"
            )

        final_response, final_error = self._chatgpt_mfa_request(
            "get",
            "/backend-api/accounts/mfa_info",
            access_token,
            index,
        )
        final_info = _response_json(final_response)
        final_factors = final_info.get("factors")
        final_factors_are_explicit_totp = isinstance(final_factors, dict) and isinstance(
            final_factors.get("totp"), list
        )
        totp_factors = (
            final_factors.get("totp", [])
            if isinstance(final_factors, dict)
            else final_factors
        )
        if not isinstance(totp_factors, list):
            totp_factors = []
        factor_confirmed = any(
            isinstance(item, dict)
            and str(item.get("id") or "") == factor_id
            and not _truthy(item.get("is_recovery"))
            and (
                final_factors_are_explicit_totp
                or str(item.get("factor_type") or item.get("type") or "")
                .strip()
                .lower()
                == "totp"
            )
            for item in totp_factors
        )
        if (
            final_response is None
            or final_response.status_code != 200
            or not (
                _truthy(final_info.get("mfa_enabled"))
                or _truthy(final_info.get("mfa_enabled_v2"))
            )
            or not factor_confirmed
        ):
            raise RuntimeError(
                final_error or "mfa_activation_not_confirmed"
            )
        step(index, "TOTP 2FA 已启用并完成复核")
        return {
            "mfa_enabled": True,
            "mfa_factor_type": "totp",
            "mfa_factor_id": factor_id,
            "totp_secret": secret,
        }

    def change_totp(
        self,
        email: str,
        password: str,
        current_totp_secret: str,
        index: int,
    ) -> dict[str, Any]:
        """Replace the account's TOTP factor after credential login."""
        email = str(email or "").strip()
        password = str(password or "").strip()
        current_totp_secret = str(current_totp_secret or "").strip()
        if not email or not password or not current_totp_secret:
            raise RuntimeError("change_totp_requires_email_password_and_current_totp")
        tokens = self.login_with_password_totp(
            email,
            password,
            current_totp_secret,
            index,
        )
        replacement = self._enroll_totp(
            tokens,
            index,
            replace_existing=True,
        )
        result = dict(tokens or {})
        result.update(replacement)
        result.update(
            {
                "email": email,
                "password": password,
                "password_set": True,
                "mfa_enabled": True,
            }
        )
        step(index, "TOTP 2FA 替换完成，已保存新密钥")
        return result

    # Alias used by a few older task payloads.
    replace_totp = change_totp
    rotate_totp = change_totp
    change_2fa = change_totp

    def _reset_auth_cookies(self) -> None:
        jar = getattr(self.session.cookies, "jar", self.session.cookies)
        for cookie in list(jar):
            domain = str(getattr(cookie, "domain", "") or "")
            if "auth.openai.com" not in domain and "chatgpt.com" not in domain:
                continue
            try:
                self.session.cookies.delete(
                    str(getattr(cookie, "name", "") or ""),
                    domain=domain,
                    path=str(getattr(cookie, "path", "/") or "/"),
                )
            except Exception:
                continue
        self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
        self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")

    def _authorize_continue_login(
        self,
        email: str,
        index: int,
        *,
        passwordless: bool = True,
        restart_invalid_state: bool = True,
    ) -> dict:
        step(
            index,
            (
                "提交邮箱进入 passwordless 登录验证"
                if passwordless
                else "提交邮箱作为登录账号标识（不读取邮箱验证码）"
            ),
        )
        url = f"{auth_base}/api/accounts/authorize/continue"

        def send():
            headers = self._json_headers(f"{auth_base}/log-in?usernameKind=email")
            headers["openai-sentinel-token"] = build_sentinel_token(
                self.session,
                self.device_id,
                "authorize_continue",
                self.fingerprint,
                lambda message: self._diagnostic(index, message),
            )
            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
            return request_with_local_retry(
                self.session,
                "post",
                url,
                json={"username": {"kind": "email", "value": email}},
                headers=headers,
                allow_redirects=False,
                verify=_tls_verify(),
            )

        resp, error = send()
        if (
            restart_invalid_state
            and resp is not None
            and resp.status_code == 409
        ):
            step(index, "登录会话过期，重新发起登录授权", "yellow")
            self._reset_auth_cookies()
            self._platform_authorize(email, index, screen_hint="login_or_signup")
            resp, error = send()
        if resp is None or resp.status_code != 200:
            detail = _response_json(resp) if resp is not None else {}
            raise RuntimeError(error or f"login_continue_http_{getattr(resp, 'status_code', 'unknown')}, detail={json.dumps(detail, ensure_ascii=False)[:300]}")
        data = _response_json(resp)
        if (
            passwordless
            and ((data.get("page") or {}).get("payload") or {}).get("passwordless_disabled")
        ):
            raise RuntimeError("当前账号不支持 passwordless 登录，请换邮箱或使用已有密码重登")
        return data

    def _send_passwordless_otp(self, index: int) -> None:
        step(index, "发送 passwordless 登录验证码")
        url = f"{auth_base}/api/accounts/passwordless/send-otp"
        headers = self._json_headers(f"{auth_base}/log-in/password")
        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "post", url, json={}, headers=headers, allow_redirects=False, verify=_tls_verify())
        if resp is None or resp.status_code not in (200, 201, 204):
            detail = _response_json(resp) if resp is not None else {}
            raise RuntimeError(error or f"passwordless_send_otp_http_{getattr(resp, 'status_code', 'unknown')}, detail={json.dumps(detail, ensure_ascii=False)[:300]}")

    @staticmethod
    def _is_passwordless_invalid_state(resp) -> bool:
        if resp is None or getattr(resp, "status_code", None) != 409:
            return False
        data = _response_json(resp)
        error = data.get("error") if isinstance(data, dict) else None
        if not isinstance(error, dict):
            return False
        code = str(error.get("code") or "").strip().lower()
        message = str(error.get("message") or "").strip().lower()
        return code == "invalid_state" or "sign-in session is no longer valid" in message

    def _complete_passwordless_login(self, code: str, index: int) -> dict:
        """Validate one passwordless OTP and exchange the resulting OAuth code."""
        resp, error = validate_otp(
            self.session,
            self.device_id,
            str(code or "").strip(),
            self.fingerprint,
            lambda message: self._diagnostic(index, message),
        )
        if resp is None or resp.status_code != 200:
            body = ""
            try:
                body = (resp.text or "")[:500] if resp is not None else ""
            except Exception:
                pass
            raise RuntimeError(
                error
                or f"passwordless_validate_otp_http_"
                f"{getattr(resp, 'status_code', 'unknown')}_body={body}"
            )
        data = _response_json(resp)
        continue_url = (
            str(data.get("continue_url") or "").strip()
            or f"{auth_base}/sign-in-with-chatgpt/platform/consent"
        )
        if _url_path(continue_url) == "/about-you":
            first_name, last_name = _random_name()
            step(index, "passwordless 登录验证完成，需要完善账号资料")
            self._create_account(
                f"{first_name} {last_name}",
                _random_birthdate(),
                index,
                require_platform_callback=True,
            )
            return self._exchange_registered_tokens(index)
        exchange_errors: list[str] = []
        tokens = exchange_tokens_from_continue_url(
            self.session,
            self.device_id,
            self.code_verifier,
            continue_url,
            self.proxy,
            self.clearance_user_agent,
            exchange_errors,
            self.fingerprint,
            getattr(self, "authorization_state", ""),
        )
        if not tokens:
            detail = (
                "；".join(exchange_errors[-4:])
                if exchange_errors
                else "未返回 token"
            )
            raise RuntimeError(f"passwordless token 换取失败: {detail}")
        return tokens

    def _passwordless_login(self, email: str, mailbox: dict, index: int) -> dict:
        provider = str(mailbox.get("provider") or "").strip().lower()
        if normalize_provider_type(provider) not in CANONICAL_PROVIDER_TYPES:
            raise RuntimeError(f"OpenAI 返回登录流，但邮箱来源不支持读取登录验证码: {provider or 'unknown'}")
        step(index, f"OpenAI 返回登录流，转入 passwordless 登录[{provider}]", "yellow")
        for attempt in range(2):
            if attempt:
                step(index, "登录会话失效，重新发起 passwordless 登录", "yellow")
                self._reset_auth_cookies()
                self._platform_authorize(email, index, screen_hint="login_or_signup")
            try:
                self._authorize_continue_login(email, index)
                mark_mailbox_code_boundary(mailbox)
                try:
                    self._send_passwordless_otp(index)
                except RuntimeError as otp_error:
                    if "invalid_state" not in str(otp_error).lower():
                        raise
                    # OpenAI 提交邮箱后会自动发送登录验证码,再次调用 send-otp
                    # 会返回 409 invalid_state;视为已发送,直接进入等待收码。
                    step(index, "验证码已自动发送,无需重复请求", "yellow")
            except RuntimeError as error:
                if attempt == 0 and "invalid_state" in str(error).lower():
                    continue
                raise
            step(index, "开始等待 passwordless 登录验证码")
            code = wait_for_code(mailbox, register_proxy=self.proxy)
            if not code:
                raise RuntimeError("等待 passwordless 登录验证码超时")
            step(index, "收到 passwordless 登录验证码")
            try:
                tokens = self._complete_passwordless_login(code, index)
                break
            except RuntimeError as error:
                if attempt == 0 and "invalid_state" in str(error).lower():
                    continue
                raise
        step(index, "passwordless token 换取完成")
        return tokens

    def login(self, email: str, mailbox: dict, index: int) -> dict[str, Any]:
        """Log in an existing account through the HAR-observed NextAuth flow."""
        step(index, "开始协议登录")
        self._complete_chatgpt_session(
            email,
            "",
            index,
            start_only=True,
        )
        mark_mailbox_code_boundary(mailbox)
        code = wait_for_code(mailbox, register_proxy=self.proxy)
        if not code:
            raise RuntimeError("协议登录验证码获取超时")

        validated = self._validate_otp(code, index)
        continue_url, _page_type = _auth_step_details(validated)
        if not continue_url:
            raise RuntimeError("协议登录 OTP 响应缺少 continue_url")

        response = self._navigate_auth_step(
            continue_url,
            f"{auth_base}/email-verification",
            index,
        )
        final_url = str(getattr(response, "url", "") or "")
        if not (_is_chatgpt_callback_url(continue_url) or _is_chatgpt_callback_url(final_url)):
            raise RuntimeError(
                "协议登录未完成 OAuth callback: "
                f"continue={_safe_url_for_log(continue_url)} "
                f"final={_safe_url_for_log(final_url)}"
            )
        tokens = self._read_chatgpt_session(email, "", index)
        step(index, "协议登录完成")
        return tokens

    def build_login_record(
        self,
        email: str,
        mailbox: dict[str, Any],
        tokens: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the same account-store shape as registration without a password."""
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        fp = _browser_fingerprint(self.fingerprint)
        return {
            "email": email,
            "password": "",
            "access_token": str(tokens.get("access_token") or "").strip(),
            "refresh_token": str(tokens.get("refresh_token") or "").strip(),
            "id_token": str(tokens.get("id_token") or "").strip(),
            "session_token": str(tokens.get("session_token") or "").strip(),
            "account_id": str(tokens.get("account_id") or "").strip(),
            "plan_type": str(tokens.get("plan_type") or "free").strip(),
            "expires_at": str(tokens.get("expires_at") or "").strip(),
            "session": tokens.get("session"),
            "cookies": tokens.get("cookies"),
            "profile": tokens.get("profile") or {},
            "oauth_client_id": str(tokens.get("oauth_client_id") or "").strip(),
            "auth_flow": "chatgpt_nextauth_login",
            "password_set": False,
            "status": "正常",
            "health_status": "alive",
            "health_checked_at": now,
            "health_error": "",
            "last_reauthenticated_at": now,
            "last_token_refresh_error": "",
            "source_type": str(mailbox.get("provider") or ""),
            "created_at": now,
            "registered_at": now,
            "login_at": now,
            "proxy": self.proxy,
            "registration_method": "protocol_login",
            "browser_type": f"curl_cffi/{fp['impersonate']}",
            "tls_impersonate": fp["impersonate"],
            "user_agent": fp["user_agent"],
            "sec_ch_ua": fp["sec_ch_ua"],
            "sec_ch_ua_full_version_list": fp["sec_ch_ua_full_version_list"],
            "platform_version": fp["platform_version"],
            "accept_language": fp["accept_language"],
            "locale": fp["accept_language"].split(",", 1)[0],
            "timezone": str(fp.get("timezone") or "Asia/Tokyo"),
            "timezone_offset_min": int(fp.get("timezone_offset_min") or 0),
            "device_id": self.device_id,
            "protocol_profile_id": str(fp.get("profile_id") or ""),
            "protocol_screen": str(fp.get("screen") or ""),
            "protocol_hardware_concurrency": str(
                fp.get("hardware_concurrency") or ""
            ),
            "protocol_device_memory": str(fp.get("device_memory") or ""),
            "protocol_platform": str(fp.get("platform") or "Win32"),
            "protocol_webgl_vendor": str(fp.get("webgl_vendor") or ""),
            "protocol_webgl_renderer": str(fp.get("webgl_renderer") or ""),
            "provider_type": str(mailbox.get("provider") or ""),
            "mailbox_provider_ref": str(mailbox.get("provider_ref") or ""),
        }

    def _register_user(self, email: str, password: str, index: int) -> dict[str, Any]:
        step(index, "开始提交注册密码")
        url = f"{auth_base}/api/accounts/user/register"
        headers = self._json_headers(f"{auth_base}/create-account/password")
        headers["openai-sentinel-token"] = build_sentinel_token(
            self.session,
            self.device_id,
            "username_password_create",
            self.fingerprint,
            lambda message: self._diagnostic(index, message),
        )
        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "post", url, json={"username": email, "password": password}, headers=headers, verify=_tls_verify())
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(url, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            headers = self._json_headers(f"{auth_base}/create-account/password")
            headers["openai-sentinel-token"] = build_sentinel_token(
                self.session,
                self.device_id,
                "username_password_create",
                self.fingerprint,
                lambda message: self._diagnostic(index, message),
            )
            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, "post", url, json={"username": email, "password": password}, headers=headers, verify=_tls_verify())
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code != 200:
            data = _response_json(resp) if resp is not None else {}
            if data.get("message") == "Failed to create account. Please try again.":
                step(index, "注册失败提示: 邮箱域名很可能因滥用被封禁，请更换邮箱域名", "yellow")
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(error or f"user_register_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        step(index, "提交注册密码完成")
        return _response_json(resp)

    def _set_registration_password(
        self,
        email: str,
        password: str,
        index: int,
    ) -> dict[str, Any]:
        """Submit the password and reject an application-level error response."""
        if not str(password or "").strip():
            raise RuntimeError("set_password_failed: generated password is empty")
        result = self._register_user(email, password, index)
        error_data = result.get("error")
        if error_data:
            code_value = (
                str(error_data.get("code") or "")
                if isinstance(error_data, dict)
                else str(error_data)
            )
            raise RuntimeError(f"set_password_failed: {code_value or error_data}")
        return result

    def _start_password_add_authorize(self, email: str, index: int) -> str:
        """Start the post-registration password-add authentication transaction."""
        fp = _browser_fingerprint(self.fingerprint)
        common = {
            "accept": "application/json",
            "accept-language": fp["accept_language"],
            "user-agent": fp["user_agent"],
            **_client_hint_headers(fp),
        }
        home_url = f"{chatgpt_base}/"
        home_headers = _headers_with_clearance(
            {**common, "accept": "text/html,application/xhtml+xml"},
            home_url,
            self.proxy,
            self.clearance_user_agent,
        )
        home, home_error = request_with_local_retry(
            self.session,
            "get",
            home_url,
            headers=home_headers,
            allow_redirects=True,
            verify=_tls_verify(),
        )
        if home is None or int(getattr(home, "status_code", 0) or 0) >= 400:
            raise RuntimeError(
                home_error
                or f"password_add_chatgpt_home_http_{getattr(home, 'status_code', 'unknown')}"
            )

        csrf_url = f"{chatgpt_base}/api/auth/csrf"
        csrf_headers = _headers_with_clearance(
            {**common, "referer": home_url},
            csrf_url,
            self.proxy,
            self.clearance_user_agent,
        )
        csrf_response, csrf_error = request_with_local_retry(
            self.session,
            "get",
            csrf_url,
            headers=csrf_headers,
            verify=_tls_verify(),
        )
        csrf_token = str(_response_json(csrf_response).get("csrfToken") or "").strip()
        if (
            csrf_response is None
            or csrf_response.status_code != 200
            or not csrf_token
        ):
            raise RuntimeError(
                csrf_error
                or f"password_add_chatgpt_csrf_http_{getattr(csrf_response, 'status_code', 'unknown')}"
            )

        def signin(params: dict[str, str]) -> Any:
            query = urlencode(params)
            signin_url = f"{chatgpt_base}/api/auth/signin/openai?{query}"
            signin_headers = _headers_with_clearance(
                {
                    **common,
                    "content-type": "application/x-www-form-urlencoded",
                    "origin": chatgpt_base,
                    "referer": home_url,
                },
                signin_url,
                self.proxy,
                self.clearance_user_agent,
            )
            with _PASSWORD_AUTH_FLOW_GATE:
                response, error = request_with_local_retry(
                    self.session,
                    "post",
                    signin_url,
                    headers=signin_headers,
                    data={
                        "callbackUrl": home_url,
                        "csrfToken": csrf_token,
                        "json": "true",
                    },
                    allow_redirects=False,
                    verify=_tls_verify(),
                )
                payload = _response_json(response)
                authorize_url = str(
                    payload.get("url")
                    or getattr(response, "headers", {}).get("Location")
                    or ""
                ).strip()
                if (
                    response is None
                    or response.status_code >= 400
                    or not authorize_url
                ):
                    raise RuntimeError(
                        error
                        or f"password_add_signin_http_{getattr(response, 'status_code', 'unknown')}"
                    )
                return self._navigate_auth_step(authorize_url, home_url, index)

        self._diagnostic(
            index,
            "启动注册后补密码授权 "
            "reauth=password post_login_add_password=true",
        )
        response = signin(
            {
                "connection": "password",
                "ext-oai-did": self.device_id,
                "login_hint": email,
                "max_age": "0",
                "post_login_add_password": "true",
                "reauth": "password",
            }
        )
        return str(getattr(response, "url", "") or "")

    def _send_password_reset_otp(
        self,
        index: int,
        *,
        referer_path: str = "/log-in/password",
    ) -> dict[str, Any]:
        """Request the email OTP used by the existing-password reset route.

        The auth service uses the page that initiated the transaction as the
        request referer. Keep the historical login-page default for callers
        that start there, while the HAR-observed forgot-password flow passes
        ``/reset-password`` explicitly.
        """
        step(index, "已有密码未知，开始发送密码重置验证码", "yellow")
        url = f"{auth_base}/api/accounts/password/send-otp"
        normalized_referer_path = str(referer_path or "/log-in/password").strip()
        if not normalized_referer_path.startswith("/"):
            normalized_referer_path = "/" + normalized_referer_path
        referer = f"{auth_base}{normalized_referer_path}"
        headers = _headers_with_clearance(
            self._json_headers(referer),
            url,
            self.proxy,
            self.clearance_user_agent,
        )
        headers["x-access-flow-invocation-id"] = str(uuid.uuid4())
        with _PASSWORD_AUTH_FLOW_GATE:
            resp, error = request_with_local_retry(
                self.session,
                "post",
                url,
                headers=headers,
                verify=_tls_verify(),
            )
        if resp is None or resp.status_code != 200:
            data = _response_json(resp) if resp is not None else {}
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(
                error
                or f"password_reset_send_otp_http_{getattr(resp, 'status_code', 'unknown')}{detail}"
            )
        step(index, "密码重置验证码已发送")
        return _response_json(resp)

    def _add_password(
        self,
        password: str,
        index: int,
        *,
        reset_existing: bool = False,
    ) -> dict[str, Any]:
        """Add a new password or reset an existing password after email OTP."""
        if not str(password or "").strip():
            raise RuntimeError("set_password_failed: generated password is empty")
        action = "重置" if reset_existing else "补充"
        step(index, f"开始{action}账号密码")
        endpoint = "reset" if reset_existing else "add"
        url = f"{auth_base}/api/accounts/password/{endpoint}"
        referer = f"{auth_base}/reset-password/new-password"

        def send():
            headers = self._json_headers(referer)
            headers["x-access-flow-invocation-id"] = str(uuid.uuid4())
            headers["openai-sentinel-token"] = build_sentinel_token(
                self.session,
                self.device_id,
                "password_reset",
                self.fingerprint,
                lambda message: self._diagnostic(index, message),
            )
            headers = _headers_with_clearance(
                headers, url, self.proxy, self.clearance_user_agent
            )
            with _PASSWORD_AUTH_FLOW_GATE:
                return request_with_local_retry(
                    self.session,
                    "post",
                    url,
                    json={"password": password},
                    headers=headers,
                    verify=_tls_verify(),
                )

        resp, error = send()
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(url, index)
            if bundle is None:
                raise RuntimeError(
                    _cloudflare_block_message(
                        resp, reason=self.clearance_failure_reason
                    )
                )
            resp, error = send()
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(
                    _cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截")
                )
        if resp is None or resp.status_code != 200:
            data = _response_json(resp) if resp is not None else {}
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            operation = "password_reset" if reset_existing else "password_add"
            raise RuntimeError(
                error
                or f"{operation}_http_{getattr(resp, 'status_code', 'unknown')}{detail}"
            )
        step(index, f"{action}账号密码完成")
        return _response_json(resp)

    def _complete_post_registration_password(
        self,
        email: str,
        password: str,
        mailbox: dict[str, Any],
        index: int,
        *,
        totp_secret: str = "",
        skip_existing_password: bool = False,
        credential_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> str:
        """Run the second OTP transaction used by the password-add HAR flow."""
        def reset_transaction_identity() -> None:
            self.device_id = str(uuid.uuid4())
            self._reset_auth_cookies()
            self._sync_session_identity()

        def start_with_retry(*, fresh: bool) -> str:
            if fresh:
                reset_transaction_identity()
            # A new password transaction may legitimately reuse the same
            # six-digit value. The timestamp boundary, rather than value
            # deduplication, separates it from the registration transaction.
            mark_mailbox_code_boundary(
                mailbox,
                grace_seconds=0,
                reset_seen_codes=True,
            )
            for authorize_attempt in range(2):
                try:
                    return self._start_password_add_authorize(email, index)
                except RuntimeError as error:
                    if authorize_attempt >= 1:
                        raise
                    if _auth_error_has_code(error, "rate_limit_exceeded"):
                        # Let the outer transaction apply the longer backoff;
                        # retrying here would issue six auth starts per account.
                        raise
                    reset_transaction_identity()
                    mark_mailbox_code_boundary(
                        mailbox,
                        grace_seconds=0,
                        reset_seen_codes=True,
                    )
                    delay = (
                        random.uniform(2.0, 4.0)
                        if _auth_error_has_code(error, "rate_limit_exceeded")
                        else 1.0
                    )
                    step(
                        index,
                        f"补密码授权启动失败，{delay:.1f}s 后重建认证事务",
                        "yellow",
                    )
                    time.sleep(delay)
            raise RuntimeError("password_authorize_start_exhausted")

        def run_transaction(
            *,
            fresh: bool,
            force_password_reset: bool,
        ) -> tuple[dict[str, Any], bool]:
            start_url = start_with_retry(fresh=fresh)
            reset_existing = (
                force_password_reset
                or _url_path(start_url) == "/log-in/password"
            )
            if reset_existing:
                self._diagnostic(
                    index,
                    (
                        "补密码授权识别到已有密码，注册任务直接跳过，"
                        "不发送密码重置验证码"
                        if skip_existing_password
                        else "补密码授权识别到已有密码，切换邮箱 OTP 密码重置协议"
                    ),
                )
                if skip_existing_password:
                    raise PasswordResetRequired(
                        f"existing_password_route={_url_path(start_url) or '/log-in/password'}",
                        email=email,
                    )
                self._send_password_reset_otp(
                    index,
                    referer_path="/reset-password",
                )

            validated: dict[str, Any] | None = None
            max_otp_attempts = 3
            for otp_attempt in range(max_otp_attempts):
                step(index, f"开始等待补密码验证码 ({otp_attempt + 1}/{max_otp_attempts})")
                code = wait_for_code(mailbox, register_proxy=self.proxy)
                if not code:
                    raise RuntimeError(
                        _otp_wait_timeout_message(mailbox, "补密码")
                    )
                if mailbox.pop("_post_registration_otp_fallback_used", False):
                    step(
                        index,
                        "补密码邮件未产生新验证码，复用本轮已验证的注册验证码",
                        "yellow",
                    )
                step(index, "收到补密码验证码")
                try:
                    validated = self._validate_otp(
                        code,
                        index,
                        allow_sentinel_retry=True,
                    )
                    break
                except RuntimeError as error:
                    if (
                        _is_wrong_email_otp_error(error)
                        and otp_attempt + 1 < max_otp_attempts
                    ):
                        step(
                            index,
                            "补密码验证码被服务端判定错误，已排除本次候选并继续等待新码",
                            "yellow",
                        )
                        continue
                    raise
            if not isinstance(validated, dict):
                raise RuntimeError("password_add_otp_validate_empty_response")
            continue_url, page_type = _auth_step_details(validated)
            self._diagnostic(
                index,
                "补密码 OTP 后认证分支 "
                f"page_type={page_type or '?'} continue={_safe_url_for_log(continue_url)}",
            )
            if _url_path(continue_url) != "/reset-password/new-password":
                raise RuntimeError(
                    "password_add_unexpected_auth_step: "
                    f"{_safe_url_for_log(continue_url)}"
                )
            # The browser HAR submits password/add directly after OTP
            # validation. Loading this page again can rotate the auth-session
            # cookies and make the immediately following POST fail invalid_state.
            password_result = (
                self._add_password(password, index, reset_existing=True)
                if reset_existing
                else self._add_password(password, index)
            )
            return password_result, reset_existing

        force_password_reset = False
        password_result: dict[str, Any] | None = None
        reset_existing = False
        for transaction_attempt in range(3):
            try:
                password_result, reset_existing = run_transaction(
                    fresh=transaction_attempt > 0,
                    force_password_reset=force_password_reset,
                )
                break
            except RuntimeError as error:
                invalid_state = _auth_error_has_code(error, "invalid_state")
                password_already_set = _auth_error_has_code(
                    error,
                    "password_already_set",
                )
                auth_rate_limited = _auth_error_has_code(
                    error,
                    "rate_limit_exceeded",
                )
                auth_error_page = _auth_error_has_code(
                    error,
                    "auth_step_error_page",
                )
                if transaction_attempt >= 2 or not (
                    invalid_state
                    or password_already_set
                    or auth_rate_limited
                    or auth_error_page
                ):
                    raise
                if password_already_set:
                    if skip_existing_password:
                        raise PasswordResetRequired(
                            "password_already_set", email=email
                        ) from error
                    force_password_reset = True
                    step(
                        index,
                        "账号密码已存在，正在重建认证事务并切换密码重置",
                        "yellow",
                    )
                elif auth_rate_limited:
                    delay = random.uniform(10.0, 14.0) * (2 ** transaction_attempt)
                    step(
                        index,
                        f"补密码授权触发频率限制，{delay:.1f}s 后重建认证事务",
                        "yellow",
                    )
                    time.sleep(delay)
                elif auth_error_page:
                    step(
                        index,
                        "补密码授权进入错误页，正在重建整段认证事务",
                        "yellow",
                    )
                else:
                    step(
                        index,
                        "补密码认证状态已失效，正在重建整段认证事务",
                        "yellow",
                    )
        if not isinstance(password_result, dict):
            raise RuntimeError("password_transaction_empty_response")
        error_data = password_result.get("error")
        if error_data:
            code_value = (
                str(error_data.get("code") or "")
                if isinstance(error_data, dict)
                else str(error_data)
            )
            raise RuntimeError(f"set_password_failed: {code_value or error_data}")
        if reset_existing:
            if str(totp_secret or "").strip():
                step(
                    index,
                    "密码重置已提交，正在用保存的 TOTP 验证新密码",
                    "yellow",
                )
                credential_tokens = self.login_with_password_totp(
                    email,
                    password,
                    totp_secret,
                    index,
                )
                if callable(credential_sink):
                    credential_sink(dict(credential_tokens or {}))
                return password
            next_url, next_page_type = _auth_step_details(password_result)
            normalized_next_page = (
                str(next_page_type or "").strip().lower().replace("-", "_")
            )
            if (
                normalized_next_page == "mfa_challenge"
                or "/mfa-challenge" in str(next_url or "").lower()
            ):
                raise RuntimeError(
                    "password_reset_existing_mfa_secret_missing: "
                    f"{_safe_url_for_log(next_url)}"
                )
            if next_url:
                self._navigate_auth_step(
                    next_url,
                    f"{auth_base}/reset-password/new-password",
                    index,
                )
            step(index, "密码重置完成，继续建立会话并创建 TOTP")
            return password
        next_url, next_page_type = _auth_step_details(password_result)
        normalized_next_page = str(next_page_type or "").strip().lower().replace("-", "_")
        if (
            str(totp_secret or "").strip()
            and (
                normalized_next_page == "mfa_challenge"
                or "/mfa-challenge" in str(next_url or "").lower()
            )
        ):
            step(
                index,
                "密码已提交，认证进入现有 MFA challenge，正在用保存的 TOTP 完成确认",
                "yellow",
            )
            credential_tokens = self.login_with_password_totp(
                email,
                password,
                totp_secret,
                index,
            )
            if callable(credential_sink):
                credential_sink(dict(credential_tokens or {}))
            return password
        if not next_url or not _is_chatgpt_callback_url(next_url):
            raise RuntimeError(
                "password_add_missing_callback: "
                f"{_safe_url_for_log(next_url)}"
            )
        self._navigate_auth_step(
            next_url,
            f"{auth_base}/reset-password/new-password",
            index,
        )
        return password

    def _send_otp(self, index: int) -> None:
        step(index, "开始发送验证码")
        url = f"{auth_base}/api/accounts/email-otp/send"
        headers = _headers_with_clearance(self._navigate_headers(f"{auth_base}/create-account/password"), url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "get", url, headers=headers, allow_redirects=True, verify=_tls_verify())
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(url, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            headers = _headers_with_clearance(self._navigate_headers(f"{auth_base}/create-account/password"), url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, "get", url, headers=headers, allow_redirects=True, verify=_tls_verify())
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code not in (200, 302):
            raise RuntimeError(error or f"send_otp_http_{getattr(resp, 'status_code', 'unknown')}")
        step(index, "发送验证码完成")

    def _validate_otp(
        self,
        code: str,
        index: int,
        *,
        allow_sentinel_retry: bool = True,
    ) -> dict[str, Any]:
        step(index, "开始校验验证码")
        resp, error = validate_otp(
            self.session,
            self.device_id,
            code,
            self.fingerprint,
            lambda message: self._diagnostic(index, message),
            allow_sentinel_retry=allow_sentinel_retry,
        )
        if resp is None or resp.status_code != 200:
            body = ""
            try:
                body = (resp.text or "")[:500] if resp is not None else ""
            except Exception:
                pass
            raise RuntimeError(error or f"validate_otp_http_{getattr(resp, 'status_code', 'unknown')}_body={body}")
        step(index, "验证码校验完成")
        return _response_json(resp)

    def _create_account(
        self,
        name: str,
        birthdate: str,
        index: int,
        *,
        require_platform_callback: bool = False,
    ) -> dict[str, Any]:
        step(index, "开始创建账号资料")
        url = f"{auth_base}/api/accounts/create_account"
        headers = self._json_headers(f"{auth_base}/about-you")

        # 使用新的 Sentinel 函数，同时获取 Sentinel Token 和 SO Token
        fp = _browser_fingerprint(self.fingerprint)
        sentinel_token, so_token, _oai_sc = build_sentinel_with_so_token(
            self.session,
            self.device_id,
            "oauth_create_account",
            user_agent=fp["user_agent"],
            sec_ch_ua=fp["sec_ch_ua"],
            diagnostic=lambda message: self._diagnostic(index, message),
            sid=str(fp.get("sentinel_sid") or ""),
            device_profile=fp,
        )
        _set_oai_sc_cookie(self.session, _oai_sc)

        headers["openai-sentinel-token"] = sentinel_token
        if so_token:
            headers["openai-sentinel-so-token"] = so_token

        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "post", url, json={"name": name, "birthdate": birthdate}, headers=headers, verify=_tls_verify())
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(url, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            headers = self._json_headers(f"{auth_base}/about-you")

            # 重新生成 Sentinel Token 和 SO Token
            sentinel_token, so_token, _oai_sc = build_sentinel_with_so_token(
                self.session,
                self.device_id,
                "oauth_create_account",
                user_agent=fp["user_agent"],
                sec_ch_ua=fp["sec_ch_ua"],
                diagnostic=lambda message: self._diagnostic(index, message),
                sid=str(fp.get("sentinel_sid") or ""),
                device_profile=fp,
            )
            _set_oai_sc_cookie(self.session, _oai_sc)

            headers["openai-sentinel-token"] = sentinel_token
            if so_token:
                headers["openai-sentinel-so-token"] = so_token

            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, "post", url, json={"name": name, "birthdate": birthdate}, headers=headers, verify=_tls_verify())
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code not in (200, 302):
            data = _response_json(resp) if resp is not None else {}
            create_error = data.get("error") if isinstance(data, dict) else None
            error_code = (
                str(create_error.get("code") or "").strip()
                if isinstance(create_error, dict)
                else str(create_error or "").strip()
            )
            if error_code in {
                "invalid_auth_step",
                "user_already_exists",
                "username_already_exists",
            }:
                step(
                    index,
                    f"创建账号资料返回可恢复状态: {error_code}",
                    "yellow",
                )
                return data
            if data.get("message") == "Failed to create account. Please try again.":
                step(index, "创建账号失败提示: 邮箱域名很可能因滥用被封禁，请更换邮箱域名", "yellow")
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(error or f"create_account_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        data = _response_json(resp)
        callback_params = (
            extract_oauth_callback_params_from_url(str(data.get("continue_url") or "").strip())
            or extract_oauth_callback_params_from_url(str(getattr(resp, "headers", {}).get("Location") or "").strip())
            or extract_oauth_callback_params_from_url(str(getattr(resp, "url", "") or "").strip())
        )
        if callback_params and not _callback_state_matches(
            callback_params, getattr(self, "authorization_state", "")
        ):
            raise RuntimeError("create_account_callback_state_mismatch")
        self.platform_auth_code = str((callback_params or {}).get("code") or "").strip()
        if require_platform_callback and not self.platform_auth_code:
            continue_hint = str(data.get("continue_url") or getattr(resp, "headers", {}).get("Location") or getattr(resp, "url", "") or "")
            raise RuntimeError(f"create_account_missing_callback: continue={_safe_url_for_log(continue_hint)}")
        step(index, "创建账号资料完成")
        return data

    def _exchange_registered_tokens(self, index: int) -> dict:
        step(index, "开始换 token")
        if not self.platform_auth_code:
            raise RuntimeError("token换取失败: 缺少 OAuth callback code")
        exchange_errors: list[str] = []
        tokens = request_platform_oauth_token(self.session, self.platform_auth_code, self.code_verifier, exchange_errors, self.fingerprint)
        if not tokens:
            detail = "；".join(exchange_errors[-3:]) if exchange_errors else "未返回 token"
            raise RuntimeError(f"token换取失败: {detail}")
        step(index, "token 换取完成")
        return tokens

    def _build_registration_record(
        self,
        email: str,
        password: str,
        tokens: dict[str, Any],
        mfa_result: dict[str, Any],
        source_type: str,
        mailbox: dict[str, Any],
        *,
        registration_state: str = "complete",
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        fp = _browser_fingerprint(self.fingerprint)
        # 完整注册环境存档：面板"设备信息"弹窗直接渲染该 JSON。
        register_fingerprint = {
            key: value
            for key, value in fp.items()
            if key not in {"sec_ch_ua", "sec_ch_ua_full_version_list"} or value
        }
        register_fingerprint["device_id"] = self.device_id
        return {
            "email": email,
            "password": password,
            "access_token": str(tokens.get("access_token") or "").strip(),
            "refresh_token": str(tokens.get("refresh_token") or "").strip(),
            "id_token": str(tokens.get("id_token") or "").strip(),
            "session_token": str(tokens.get("session_token") or "").strip(),
            "account_id": str(tokens.get("account_id") or "").strip(),
            "plan_type": str(tokens.get("plan_type") or "free").strip(),
            "expires_at": str(tokens.get("expires_at") or "").strip(),
            "session": tokens.get("session"),
            "cookies": tokens.get("cookies"),
            "profile": tokens.get("profile") or {},
            "oauth_client_id": str(tokens.get("oauth_client_id") or "").strip(),
            "auth_flow": "chatgpt_nextauth",
            "password_set": bool(password),
            "mfa_enabled": bool(mfa_result.get("mfa_enabled")),
            "mfa_factor_type": str(mfa_result.get("mfa_factor_type") or ""),
            "mfa_factor_id": str(mfa_result.get("mfa_factor_id") or ""),
            "totp_secret": str(mfa_result.get("totp_secret") or ""),
            "registration_state": registration_state,
            "source_type": source_type,
            "created_at": now,
            "registered_at": now,
            "proxy": self.proxy,
            "registration_method": "protocol",
            "browser_type": f"curl_cffi/{fp['impersonate']}",
            "browser_family": str(fp.get("browser_family") or "firefox"),
            "register_fingerprint": json.dumps(register_fingerprint, ensure_ascii=False, sort_keys=True),
            "tls_impersonate": fp["impersonate"],
            "user_agent": fp["user_agent"],
            "sec_ch_ua": fp["sec_ch_ua"],
            "sec_ch_ua_full_version_list": fp["sec_ch_ua_full_version_list"],
            "platform_version": fp["platform_version"],
            "accept_language": fp["accept_language"],
            "locale": fp["accept_language"].split(",", 1)[0],
            "timezone": str(fp.get("timezone") or "Asia/Tokyo"),
            "timezone_offset_min": int(fp.get("timezone_offset_min") or 0),
            "device_id": self.device_id,
            "protocol_profile_id": str(fp.get("profile_id") or ""),
            "protocol_screen": str(fp.get("screen") or ""),
            "protocol_hardware_concurrency": str(fp.get("hardware_concurrency") or ""),
            "protocol_device_memory": str(fp.get("device_memory") or ""),
            "protocol_platform": str(fp.get("platform") or "Win32"),
            "protocol_webgl_vendor": str(fp.get("webgl_vendor") or ""),
            "protocol_webgl_renderer": str(fp.get("webgl_renderer") or ""),
            "protocol_device_pixel_ratio": str(fp.get("device_pixel_ratio") or "1"),
            "protocol_navigator_vendor": str(fp.get("navigator_vendor") or ""),
            "protocol_max_touch_points": str(fp.get("max_touch_points") or "0"),
            "protocol_audio_sample_rate": str(fp.get("audio_sample_rate") or "48000"),
            "protocol_color_depth": str(fp.get("color_depth") or "24"),
            "protocol_sentinel_sid": str(fp.get("sentinel_sid") or ""),
            "provider_type": str(mailbox.get("provider") or ""),
            "mailbox_provider_ref": str(mailbox.get("provider_ref") or ""),
        }

    def register(
        self,
        index: int,
        *,
        partial_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict:
        def ensure_running() -> None:
            if _STOP_CHECK.get()():
                raise JobCancelled("stop requested")

        ensure_running()
        self._log_identity(index)
        step(index, "开始创建邮箱")
        mailbox = create_mailbox(_FIXED_EMAIL.get(), register_proxy=self.proxy)
        email = str(mailbox.get("address") or "").strip()
        if not email:
            release_mailbox(mailbox)
            raise RuntimeError("邮箱服务未返回 address")
        label = str(mailbox.get("label") or "")
        step(index, f"邮箱创建完成[{label}]: {email}")
        try:
            password_candidate = _random_password()
            registration_trace = _REGISTRATION_TRACE.get()
            if registration_trace is not None:
                registration_trace.set_secrets(password_candidate)
            _add_task_event_secrets(password_candidate)
            password = ""
            mfa_result: dict[str, Any] = {}
            first_name, last_name = _random_name()
            birthdate = _random_birthdate()
            source_type = (
                "microsoft"
                if str(mailbox.get("provider") or "") == "outlook_token"
                else "web"
            )

            def persist_security_pending(
                snapshot_tokens: dict[str, Any] | None = None,
                *,
                announce: bool = False,
            ) -> None:
                if not callable(partial_sink) or not password:
                    return
                partial_sink(
                    self._build_registration_record(
                        email,
                        password,
                        snapshot_tokens or {},
                        {},
                        source_type,
                        mailbox,
                        registration_state="security_pending",
                    )
                )
                if announce:
                    step(
                        index,
                        "注册密码已保存，等待会话和 TOTP 2FA；中断后可继续补全安全字段",
                        "yellow",
                    )

            mark_mailbox_code_boundary(mailbox)
            mailbox["_registration_auth_started"] = True
            ensure_running()
            self._complete_chatgpt_session(
                email,
                "",
                index,
                start_only=True,
            )
            ensure_running()
            registration_password_result = self._set_registration_password(
                email,
                password_candidate,
                index,
            )
            # user/register returns the next auth step rather than the
            # password itself. Keep the generated credential locally, then
            # follow email-otp/send in the same session before validating OTP.
            # Test doubles from older callers may still return a string.
            password = (
                str(registration_password_result)
                if isinstance(registration_password_result, str)
                else password_candidate
            )
            registration_continue, registration_page_type = _auth_step_details(
                registration_password_result
                if isinstance(registration_password_result, dict)
                else {}
            )
            if registration_continue:
                self._diagnostic(
                    index,
                    "注册密码后认证步骤 "
                    f"page_type={registration_page_type or '?'} "
                    f"continue={_safe_url_for_log(registration_continue)}",
                )
                self._navigate_auth_step(
                    registration_continue,
                    f"{auth_base}/create-account/password",
                    index,
                )
            ensure_running()
            persist_security_pending(announce=True)
            validated: dict[str, Any] | None = None
            max_otp_attempts = 3
            for otp_attempt in range(max_otp_attempts):
                step(index, f"开始等待注册验证码 ({otp_attempt + 1}/{max_otp_attempts})")
                code = wait_for_code(mailbox, register_proxy=self.proxy)
                if not code:
                    ensure_running()
                    raise RuntimeError(
                        _otp_wait_timeout_message(mailbox, "注册")
                    )
                step(index, "收到注册验证码")
                try:
                    validated = self._validate_otp(
                        code,
                        index,
                        allow_sentinel_retry=True,
                    )
                    break
                except RuntimeError as error:
                    if _is_wrong_email_otp_error(error) and otp_attempt + 1 < max_otp_attempts:
                        step(
                            index,
                            "注册验证码被服务端判定错误，已排除本次候选并继续等待新码",
                            "yellow",
                        )
                        ensure_running()
                        continue
                    if "invalid_state" not in str(error).lower() or otp_attempt + 1 >= max_otp_attempts:
                        raise
                    step(index, "NextAuth 登录状态已失效，清理会话并重新发送验证码", "yellow")
                    self._reset_auth_cookies()
                    mark_mailbox_code_boundary(mailbox, grace_seconds=0)
                    self._complete_chatgpt_session(
                        email,
                        "",
                        index,
                        start_only=True,
                    )
                    ensure_running()
            if not isinstance(validated, dict):
                raise RuntimeError("email_otp_validate_empty_response")
            ensure_running()
            continue_url, page_type = _auth_step_details(validated)
            self._diagnostic(
                index,
                "OTP 后认证分支 "
                f"page_type={page_type or '?'} continue={_safe_url_for_log(continue_url)}",
            )
            normalized_page_type = str(page_type or "").strip().lower().replace("-", "_")
            if normalized_page_type == "mfa_challenge" or "/mfa-challenge" in str(
                continue_url or ""
            ).lower():
                step(
                    index,
                    "该邮箱已是启用 MFA 的现有账号，缺少原密码和 TOTP 密钥，已退出注册候选池",
                    "yellow",
                )
                raise RuntimeError(
                    "existing_account_mfa_credentials_missing: "
                    f"page={page_type or '?'} continue={_safe_url_for_log(continue_url)}"
                )

            # Some auth sessions expose the legacy password route before
            # about-you. The happy path now submits the registration password
            # before OTP; keep the older callback fallback for unusual states.
            if _auth_step_requires_password(continue_url, page_type):
                if (
                    _url_path(continue_url) == "/log-in/password"
                    or str(page_type or "").strip().lower() == "password_verification"
                ):
                    step(
                        index,
                        "验证码后进入已有密码认证，已跳过注册（未发送密码重置验证码）",
                        "yellow",
                    )
                    raise PasswordResetRequired("otp_password_route", email=email)
                if continue_url:
                    self._navigate_auth_step(
                        continue_url,
                        f"{auth_base}/email-verification",
                        index,
                    )
                ensure_running()
            elif _is_chatgpt_callback_url(continue_url):
                self._diagnostic(
                    index,
                    "OTP 已直接进入 ChatGPT 回调，沿用旧补密码兜底",
                )
                self._navigate_auth_step(
                    continue_url,
                    f"{auth_base}/email-verification",
                    index,
                )
                ensure_running()
            else:
                self._diagnostic(
                    index,
                    "OTP 已直接进入资料步骤，继续创建账号并读取会话",
                )
                if continue_url:
                    self._navigate_auth_step(
                        continue_url,
                        f"{auth_base}/email-verification",
                        index,
                    )
                    ensure_running()

            if _is_chatgpt_callback_url(continue_url):
                tokens = self._read_chatgpt_session(email, password, index)
                ensure_running()
                mailbox["_post_registration_otp_fallback"] = code
                password = self._complete_post_registration_password(
                    email,
                    password_candidate,
                    mailbox,
                    index,
                    skip_existing_password=True,
                )
                if registration_trace is not None:
                    registration_trace.set_secrets(password)
                _add_task_event_secrets(password)
                ensure_running()
                persist_security_pending(tokens, announce=True)
                tokens = self._read_chatgpt_session(email, password, index)
            else:
                create_result = self._create_account(
                    f"{first_name} {last_name}",
                    birthdate,
                    index,
                )
                ensure_running()
                create_error = create_result.get("error")
                if create_error:
                    error_code = (
                        str(create_error.get("code") or "")
                        if isinstance(create_error, dict)
                        else str(create_error)
                    )
                    if error_code not in {
                        "invalid_auth_step",
                        "user_already_exists",
                        "username_already_exists",
                    }:
                        raise RuntimeError(
                            f"create_account_failed: {error_code or create_error}"
                        )
                create_continue, _create_page_type = _auth_step_details(create_result)
                if create_continue:
                    self._navigate_auth_step(
                        create_continue,
                        f"{auth_base}/about-you",
                        index,
                    )
                ensure_running()
                tokens = self._read_chatgpt_session(email, password, index)
            persist_security_pending(tokens)
            if current_runtime_settings().enable_totp_after_register:
                mfa_result = self._enroll_totp(tokens, index)
            else:
                step(
                    index,
                    "协议注册已跳过即时 TOTP 2FA，账号以 security_pending 保存，后续可批量补全",
                    "yellow",
                )
            tokens["refresh_token"] = ""
            tokens["id_token"] = ""
        except JobCancelled:
            if mailbox.get("_registration_auth_started"):
                mark_mailbox_result(
                    mailbox,
                    success=False,
                    error="partial_registration_otp_already_sent",
                )
            else:
                release_mailbox(mailbox)
            raise
        except PasswordResetRequired as error:
            mark_mailbox_result(
                mailbox,
                success=False,
                error=PasswordResetRequired.code,
            )
            step(
                index,
                "检测到账号已有密码，已跳过注册（未发送密码重置验证码）",
                "yellow",
            )
            raise
        except Exception as error:
            mark_mailbox_result(mailbox, success=False, error=error)
            raise
        mark_mailbox_result(mailbox, success=True)
        final_state = (
            "complete"
            if current_runtime_settings().enable_totp_after_register
            else "security_pending"
        )
        return self._build_registration_record(
            email,
            password,
            tokens,
            mfa_result,
            source_type,
            mailbox,
            registration_state=final_state,
        )

"""Standalone protocol security operations.

This module exposes password and TOTP maintenance flows in a dedicated
registrar subclass.  The existing register.protocol.PlatformRegistrar is
left untouched; its session, fingerprint, OAuth, and password-flow helpers
are reused through inheritance.

Usage::

    from register.protocol_security_standalone import ProtocolSecurityRegistrar

    registrar = ProtocolSecurityRegistrar(PROXY)
    try:
        result = registrar.change_password(
            EMAIL, OLD_PASSWORD, NEW_PASSWORD, TOTP_SECRET, INDEX
        )
    finally:
        registrar.close()

The class is intentionally not wired into the batch/UI entry points.  It is a
drop-in standalone implementation for callers that want the protocol security
operations in one module.
"""

from __future__ import annotations

import json
import time
from typing import Any

from common.totp import totp_code
from register.sessions import JobCancelled
from register.protocol import (
    PlatformRegistrar,
    _REGISTRATION_TRACE,
    _STOP_CHECK,
    _absolute_auth_url,
    _add_task_event_secrets,
    _auth_step_details,
    _auth_step_requires_password,
    _browser_fingerprint,
    _header_fingerprint,
    _headers_with_clearance,
    _is_chatgpt_callback_url,
    _mfa_activation_error_text,
    _response_json,
    request_with_local_retry,
    _safe_url_for_log,
    _tls_verify,
    _totp_code,
    _truthy,
    _url_path,
    auth_base,
    chatgpt_base,
    step,
)

__all__ = ["ProtocolSecurityRegistrar"]


class ProtocolSecurityRegistrar(PlatformRegistrar):
    """Platform registrar with standalone password/TOTP operations."""

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

    def login_with_password_totp(
        self,
        email: str,
        password: str,
        totp_secret: str,
        index: int,
    ) -> dict[str, Any]:
        """Complete the HAR-observed password plus TOTP login transaction."""
        email = str(email or "").strip()
        password = str(password or "").strip()
        totp_secret = str(totp_secret or "").strip()
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

"""Protocol email-rebinding workflow.

The registration protocol already owns the curl_cffi fingerprint, OAuth
session handling and password/TOTP login implementation.  This module keeps
the email-change state machine small and reuses that implementation instead of
creating a second authentication stack.
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from mail import mark_mailbox_code_boundary, wait_for_code
from register.protocol import (
    PlatformRegistrar,
    _PROXY_CONFIG,
    _STOP_CHECK,
    _browser_fingerprint,
    _cookie_values,
    _headers_with_clearance,
    _response_json,
    _nextauth_session_cookie,
    activate_runtime,
    chatgpt_base,
    parse_jwt_payload,
    request_with_local_retry,
)
from register.protocol_runtime import ProtocolRuntimeSettings, current_runtime_settings


DEFAULT_CLIENT_VERSION = "prod-1e268a33279bcedafc2fe5526bfe230880444b77"
DEFAULT_CLIENT_BUILD = "9748354"


class RebindError(RuntimeError):
    """A classified, panel-safe workflow error."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code or "REBIND_FAILED")
        self.message = str(message or self.code)
        super().__init__(f"{self.code}: {self.message}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _short_body(response: Any, limit: int = 500) -> str:
    try:
        return str(getattr(response, "text", "") or "")[:limit]
    except Exception:
        return ""


def _flatten_text(value: Any, depth: int = 0) -> str:
    if depth > 4 or value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(
            f"{key} {_flatten_text(item, depth + 1)}" for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten_text(item, depth + 1) for item in value)
    return str(value)


def _session_email(value: Any, depth: int = 0) -> str:
    """Find an explicit session email without trusting arbitrary text fields."""
    if depth > 4 or value is None:
        return ""
    if isinstance(value, dict):
        for key in ("email", "user_email", "login_email"):
            candidate = str(value.get(key) or "").strip().lower()
            if "@" in candidate:
                return candidate
        for key in ("user", "account", "profile", "session"):
            candidate = _session_email(value.get(key), depth + 1)
            if candidate:
                return candidate
    elif isinstance(value, (list, tuple)):
        for item in value:
            candidate = _session_email(item, depth + 1)
            if candidate:
                return candidate
    return ""


def _mask(value: Any, head: int = 8, tail: int = 5) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= head + tail:
        return "*" * len(text)
    return f"{text[:head]}...{text[-tail:]}"


def _safe_error(value: Any, secrets: tuple[Any, ...] = ()) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    for secret in sorted(
        (str(item).strip() for item in secrets if str(item or "").strip()),
        key=len,
        reverse=True,
    ):
        text = text.replace(secret, "[已隐藏]")
    return text[:500]


@contextmanager
def _runtime_scope(
    stop_check: Callable[[], bool],
    proxy_config: dict[str, Any],
    runtime: ProtocolRuntimeSettings,
) -> Iterator[None]:
    stop_token = _STOP_CHECK.set(stop_check)
    proxy_token = _PROXY_CONFIG.set(dict(proxy_config or {}))
    try:
        with activate_runtime(runtime):
            yield
    finally:
        _PROXY_CONFIG.reset(proxy_token)
        _STOP_CHECK.reset(stop_token)


class ChangeEmailClient:
    """Change-email API bound to one freshly authenticated registrar."""

    def __init__(
        self,
        registrar: PlatformRegistrar,
        tokens: dict[str, Any],
        *,
        account_id: str = "",
        client_version: str = DEFAULT_CLIENT_VERSION,
        client_build: str = DEFAULT_CLIENT_BUILD,
        language: str = "zh-CN",
    ) -> None:
        self.registrar = registrar
        self.tokens = dict(tokens or {})
        self.account_id = str(
            account_id
            or self.tokens.get("account_id")
            or self._account_id_from_token()
            or ""
        ).strip()
        self.client_version = str(client_version or DEFAULT_CLIENT_VERSION).strip()
        self.client_build = str(client_build or DEFAULT_CLIENT_BUILD).strip()
        self.language = str(language or "zh-CN").strip()
        self.session_id = str(uuid.uuid4())

    def _account_id_from_token(self) -> str:
        payload = parse_jwt_payload(str(self.tokens.get("access_token") or "")) or {}
        claim = payload.get("https://api.openai.com/auth")
        return str(claim.get("chatgpt_account_id") or "").strip() if isinstance(claim, dict) else ""

    def _cookie_header(self) -> str:
        # curl_cffi already applies domain/path rules.  Supplying a compact
        # header as well covers the backend's session-cookie parser, including
        # NextAuth chunk names, without inventing a fixed chunk size.
        cookies = _cookie_values(self.registrar.session)
        return "; ".join(f"{key}={value}" for key, value in cookies.items() if key and value)

    def _headers(self, path: str, *, json_body: bool = False) -> dict[str, str]:
        fp = _browser_fingerprint(self.registrar.fingerprint)
        headers = {
            "accept": "*/*",
            "accept-language": str(fp.get("accept_language") or "en-US,en;q=0.9"),
            "authorization": f"Bearer {str(self.tokens.get('access_token') or '').strip()}",
            "referer": f"{chatgpt_base}/",
            "origin": chatgpt_base,
            "oai-device-id": str(self.registrar.device_id),
            "oai-session-id": self.session_id,
            "oai-client-version": self.client_version,
            "oai-client-build-number": self.client_build,
            "oai-language": self.language,
            "x-openai-target-path": path,
            "x-openai-target-route": path,
            "user-agent": str(fp.get("user_agent") or ""),
        }
        if self.account_id and path != "/backend-api/accounts/change_email/eligibility":
            headers["chatgpt-account-id"] = self.account_id
        cookie = self._cookie_header()
        if cookie:
            headers["cookie"] = cookie
        if json_body:
            headers["content-type"] = "application/json"
        return _headers_with_clearance(
            headers,
            f"{chatgpt_base}{path}",
            self.registrar.proxy,
            str(getattr(self.registrar, "clearance_user_agent", "") or ""),
        )

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        if not str(self.tokens.get("access_token") or "").strip():
            raise RebindError("LOGIN_FAILED", "登录结果缺少 Access Token")
        kwargs: dict[str, Any] = {
            "headers": self._headers(path, json_body=payload is not None),
            "verify": current_runtime_settings().tls_verify,
            "allow_redirects": False,
        }
        if payload is not None:
            kwargs["json"] = payload
        # ``begin`` sends an OTP and is not idempotent: replaying a POST after
        # a gateway/timeout response can issue multiple codes or invalidate
        # the first one.  Use one attempt there and let ``begin`` classify a
        # missing/ambiguous response conservatively. Other protocol requests
        # retain the bounded local retry policy.
        retry_attempts = 1 if (
            method.upper() == "POST"
            and path.rstrip("/") == "/backend-api/accounts/change_email/begin"
        ) else 3
        response, error = request_with_local_retry(
            self.registrar.session,
            method,
            f"{chatgpt_base}{path}",
            retry_attempts=retry_attempts,
            **kwargs,
        )
        if response is None:
            failure = RebindError("REQUEST_FAILED", str(error or "换绑接口没有响应")[:300])
            # Keep transport metadata on the classified error.  A lost
            # response does not prove that a non-idempotent request was not
            # accepted by the remote service; ``begin`` uses this marker to
            # quarantine the mailbox instead of allowing a later task to
            # consume a delayed OTP.
            setattr(failure, "transport_error", True)
            setattr(failure, "transport_error_before_send", str(error or "").strip() == "protocol_account_timeout")
            raise failure
        body = _response_json(response)
        status = int(getattr(response, "status_code", 0) or 0)
        if status < 200 or status >= 300:
            detail = _flatten_text(body) or _short_body(response)
            low = detail.lower()
            if status in {401, 403} or any(
                marker in low for marker in ("recent", "reauth", "re-auth", "fresh auth")
            ):
                failure = RebindError("REAUTH_REQUIRED", f"换绑接口要求近期重新认证（HTTP {status}）")
                setattr(failure, "status_code", status)
                raise failure
            failure = RebindError("REQUEST_FAILED", f"换绑接口 HTTP {status}: {detail[:280]}")
            setattr(failure, "status_code", status)
            raise failure
        return body

    def eligibility(self) -> dict[str, Any]:
        body = self.request("GET", "/backend-api/accounts/change_email/eligibility")
        if not isinstance(body, dict) or body.get("eligible") is not True:
            kind = str((body or {}).get("eligibility_type") or "") if isinstance(body, dict) else ""
            raise RebindError("NOT_ELIGIBLE", f"账号不满足换绑条件{(': ' + kind) if kind else ''}")
        return body

    def begin(self, new_email: str) -> dict[str, Any]:
        target = str(new_email or "").strip().lower()
        if "@" not in target:
            raise RebindError("BEGIN_FAILED", "目标邮箱格式无效")
        try:
            body = self.request(
                "POST",
                "/backend-api/accounts/change_email/begin",
                {"email": target},
            )
        except RebindError as error:
            status = int(getattr(error, "status_code", 0) or 0)
            transport = bool(getattr(error, "transport_error", False))
            before_send = bool(getattr(error, "transport_error_before_send", False))
            # POST /begin is not safely replayable.  For a transport failure
            # after the request may have left the client, or for a transient
            # gateway response, conservatively treat acceptance as unknown.
            # The worker then avoids proxy replay and quarantines the target
            # mailbox until an operator can inspect the task.
            transient_status = status in {408, 425, 429, 500, 502, 503, 504}
            if (transport and not before_send) or transient_status:
                setattr(error, "began", True)
                setattr(error, "begin_acceptance_unknown", True)
            raise
        return body if isinstance(body, dict) else {"ok": True}

    def verify(self, new_email: str, code: str) -> dict[str, Any]:
        target = str(new_email or "").strip().lower()
        value = str(code or "").strip()
        if "@" not in target or not value:
            raise RebindError("VERIFY_FAILED", "目标邮箱或验证码为空")
        body = self.request(
            "POST",
            "/backend-api/accounts/change_email/verify",
            {"email": target, "code": value},
        )
        return body if isinstance(body, dict) else {"ok": True}


def _decorate_tokens(registrar: PlatformRegistrar, tokens: dict[str, Any]) -> dict[str, Any]:
    fp = _browser_fingerprint(registrar.fingerprint)
    result = dict(tokens or {})
    result.update(
        {
            "browser_type": f"curl_cffi/{fp['impersonate']}",
            "tls_impersonate": fp["impersonate"],
            "user_agent": fp["user_agent"],
            "sec_ch_ua": fp.get("sec_ch_ua", ""),
            "sec_ch_ua_full_version_list": fp.get("sec_ch_ua_full_version_list", ""),
            "platform_version": fp.get("platform_version", ""),
            "accept_language": fp.get("accept_language", ""),
            "locale": str(fp.get("accept_language") or "").split(",", 1)[0],
            "timezone": str(fp.get("timezone") or "Asia/Tokyo"),
            "timezone_offset_min": int(fp.get("timezone_offset_min") or 0),
            "protocol_screen": str(fp.get("screen") or ""),
            "protocol_hardware_concurrency": str(fp.get("hardware_concurrency") or ""),
            "protocol_device_memory": str(fp.get("device_memory") or ""),
            "protocol_platform": str(fp.get("platform") or "Win32"),
            "protocol_webgl_vendor": str(fp.get("webgl_vendor") or ""),
            "protocol_webgl_renderer": str(fp.get("webgl_renderer") or ""),
            "device_id": str(registrar.device_id),
            "protocol_profile_id": str(fp.get("profile_id") or ""),
        }
    )
    return result


def _session_email_from_tokens(tokens: dict[str, Any]) -> str:
    return _session_email(tokens.get("session")) or _session_email(tokens.get("profile"))


def run_rebind(
    *,
    old_email: str,
    password: str,
    totp_secret: str,
    new_email: str,
    mail_config: dict[str, Any],
    mailbox: dict[str, Any] | None = None,
    proxy: str = "",
    proxy_config: dict[str, Any] | None = None,
    runtime: ProtocolRuntimeSettings | None = None,
    stop_check: Callable[[], bool] | None = None,
    event: Callable[..., Any] | None = None,
    index: int = 1,
    client_version: str = DEFAULT_CLIENT_VERSION,
    client_build: str = DEFAULT_CLIENT_BUILD,
    mail_timeout: float = 120.0,
) -> dict[str, Any]:
    """Execute one six-stage email rebinding transaction."""
    old = str(old_email or "").strip().lower()
    target = str(new_email or "").strip().lower()
    pwd = str(password or "").strip()
    totp = str(totp_secret or "").strip()
    stop = stop_check if callable(stop_check) else (lambda: False)
    if not old or "@" not in old:
        raise RebindError("LOGIN_FAILED", "原账号邮箱格式无效")
    if not pwd or not totp:
        raise RebindError("LOGIN_FAILED", "换绑要求账号已保存密码和 TOTP 2FA")
    if not target or "@" not in target:
        raise RebindError("BEGIN_FAILED", "目标邮箱格式无效")

    runtime_value = runtime or ProtocolRuntimeSettings()
    cfg = dict(proxy_config or {})
    registrar: PlatformRegistrar | None = None
    tokens: dict[str, Any] = {}
    began = False

    def emit(status: str = "running", **fields: Any) -> None:
        if callable(event):
            event(index, status, email=old, new_email=target, **fields)

    def checkpoint() -> None:
        if stop():
            raise RebindError("CANCELLED", "换绑任务已停止")

    try:
        with _runtime_scope(stop, cfg, runtime_value):
            emit(step="[1/6] 登录原账号并验证密码 + TOTP")
            registrar = PlatformRegistrar(proxy)
            tokens = _decorate_tokens(
                registrar,
                registrar.login_with_password_totp(old, pwd, totp, index),
            )
            account_id = str(tokens.get("account_id") or "").strip()
            if not account_id:
                raise RebindError("LOGIN_FAILED", "原账号登录结果缺少 Account ID")

            checkpoint()
            emit(step="[2/6] 检查邮箱换绑资格")
            client = ChangeEmailClient(
                registrar,
                tokens,
                account_id=account_id,
                client_version=client_version,
                client_build=client_build,
            )
            client.eligibility()

            # Establish the inbox boundary before the begin request. The
            # provider can deliver the OTP while that request is in flight;
            # setting the boundary afterwards would classify that valid code
            # as stale and force an unnecessary timeout.
            mailbox_value = dict(mailbox or {})
            mailbox_value.setdefault("address", target)
            mark_mailbox_code_boundary(mailbox_value, grace_seconds=0, reset_seen_codes=True)

            checkpoint()
            emit(step="[3/6] 发起换绑并发送新邮箱验证码")
            try:
                client.begin(target)
                began = True
            except RebindError as error:
                if error.code != "REAUTH_REQUIRED":
                    raise
                # The service can require a very recent auth even after the
                # initial login. Rebuild the protocol session and retry once.
                emit(step="换绑接口要求近期认证，重新登录后重试", status="running")
                registrar.close()
                registrar = PlatformRegistrar(proxy)
                tokens = _decorate_tokens(
                    registrar,
                    registrar.login_with_password_totp(old, pwd, totp, index),
                )
                refreshed_account_id = str(tokens.get("account_id") or "").strip()
                if refreshed_account_id != account_id:
                    raise RebindError("LOGIN_FAILED", "重新认证后的 Account ID 与原账号不一致")
                client = ChangeEmailClient(
                    registrar,
                    tokens,
                    account_id=refreshed_account_id or account_id,
                    client_version=client_version,
                    client_build=client_build,
                )
                client.eligibility()
                client.begin(target)
                began = True

            checkpoint()
            emit(step="[4/6] 等待新邮箱验证码")
            # Keep the rebind timeout independent from the registration/mail
            # panel default. A short-lived task should not silently wait for a
            # much longer global mailbox timeout.
            wait_config = dict(mail_config or {})
            try:
                wait_config["wait_timeout"] = max(1.0, float(mail_timeout or 120.0))
            except (TypeError, ValueError):
                wait_config["wait_timeout"] = 120.0
            code = wait_for_code(wait_config, mailbox_value, stop_check=stop)
            if not code:
                raise RebindError("MAIL_TIMEOUT", "未取得新邮箱验证码")

            checkpoint()
            emit(step="[5/6] 提交新邮箱验证码")
            try:
                client.verify(target, code)
            except RebindError as error:
                if error.code != "REAUTH_REQUIRED":
                    raise
                # A recent-auth challenge after begin is retried against the
                # same target/code only after a fresh login session.
                registrar.close()
                registrar = PlatformRegistrar(proxy)
                tokens = _decorate_tokens(
                    registrar,
                    registrar.login_with_password_totp(old, pwd, totp, index),
                )
                refreshed_account_id = str(tokens.get("account_id") or "").strip()
                if refreshed_account_id != account_id:
                    raise RebindError("LOGIN_FAILED", "重新认证后的 Account ID 与原账号不一致")
                client = ChangeEmailClient(
                    registrar,
                    tokens,
                    account_id=refreshed_account_id or account_id,
                    client_version=client_version,
                    client_build=client_build,
                )
                client.verify(target, code)

            checkpoint()
            emit(step="[6/6] 新邮箱重登并校验会话")
            registrar.close()
            registrar = None
            last_error: Exception | None = None
            for attempt in range(3):
                checkpoint()
                candidate: PlatformRegistrar | None = None
                try:
                    candidate = PlatformRegistrar(proxy)
                    candidate_tokens = _decorate_tokens(
                        candidate,
                        candidate.login_with_password_totp(target, pwd, totp, index),
                    )
                    session_email = _session_email_from_tokens(candidate_tokens)
                    if session_email and session_email != target:
                        candidate.close()
                        raise RebindError(
                            "RELOGIN_FAILED",
                            f"重登会话邮箱不匹配: {session_email}",
                        )
                    if str(candidate_tokens.get("account_id") or "").strip() != account_id:
                        candidate.close()
                        raise RebindError("RELOGIN_FAILED", "重登后的 Account ID 与原账号不一致")
                    # Retain the active registrar's cookies/session payload for
                    # callers that persist the refreshed account bundle.
                    candidate_tokens.setdefault("session_token", _nextauth_session_cookie(candidate.session))
                    candidate_tokens.setdefault("cookies", _cookie_values(candidate.session))
                    candidate_tokens["session_email"] = session_email or target
                    candidate_tokens["account_id"] = account_id
                    emit(
                        status="success",
                        step="换绑完成，已取得新邮箱会话",
                        account_id=account_id,
                        access_token_masked=_mask(candidate_tokens.get("access_token")),
                    )
                    return {
                        "old_email": old,
                        "new_email": target,
                        "account_id": account_id,
                        "tokens": candidate_tokens,
                        "began": began,
                    }
                except RebindError:
                    raise
                except Exception as error:
                    last_error = error
                    if attempt < 2:
                        time.sleep(1.5 * (attempt + 1))
                finally:
                    if candidate is not None:
                        try:
                            candidate.close()
                        except Exception:
                            pass
            raise RebindError("RELOGIN_FAILED", _safe_error(last_error or "新邮箱重登失败", (pwd, totp)))
    except RebindError as error:
        # The caller must know whether the remote ``begin`` step had already
        # been accepted.  A timeout while waiting for the OTP, or a failure
        # during the final relogin, must quarantine the mailbox instead of
        # releasing it for a second account.  Keep this marker out of the
        # serialized/public error text.
        try:
            setattr(error, "began", bool(began or getattr(error, "began", False)))
        except Exception:
            pass
        raise
    except Exception as error:
        wrapped = RebindError("REBIND_FAILED", _safe_error(error, (pwd, totp)))
        try:
            setattr(wrapped, "began", bool(began or getattr(error, "began", False)))
        except Exception:
            pass
        raise wrapped from error
    finally:
        if registrar is not None:
            try:
                registrar.close()
            except Exception:
                pass


__all__ = ["ChangeEmailClient", "RebindError", "run_rebind"]

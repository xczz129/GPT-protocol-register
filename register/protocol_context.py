"""协议层共享运行时上下文：全局配置、ContextVar、日志与基础工具函数。"""

from __future__ import annotations

import contextvars
import json
import random
import re
import secrets
import string
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable
from common.registration_trace import RegistrationTrace
from common.totp import totp_code
from mail import create_mailbox as allocate_mailbox
from mail import wait_for_code as read_mail_code
from register.protocol_runtime import current_runtime_settings



config = {
    "mail": {
        "request_timeout": 30,
        "wait_timeout": 90,
        "wait_interval": 2,
        "api_use_register_proxy": True,
        "providers": [],
    },
    "proxy": "",
    "total": 10,
    "threads": 3,
    "proxy_config": {},
}



_TASK_EVENT: contextvars.ContextVar[Callable[..., Any] | None] = contextvars.ContextVar(
    "protocol_task_event", default=None
)


_STOP_CHECK: contextvars.ContextVar[Callable[[], bool]] = contextvars.ContextVar(
    "protocol_stop_check", default=lambda: False
)


_FIXED_EMAIL: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "protocol_fixed_email", default=None
)


_PROVIDER_TYPE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "protocol_provider_type", default=None
)


_MAIL_CONFIG: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "protocol_mail_config", default=None
)


_PROXY_CONFIG: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "protocol_proxy_config", default=None
)


_REGISTRATION_TRACE: contextvars.ContextVar[RegistrationTrace | None] = contextvars.ContextVar(
    "protocol_registration_trace", default=None
)



auth_base = "https://auth.openai.com"


chatgpt_base = "https://chatgpt.com"


platform_base = "https://platform.openai.com"


platform_oauth_client_id = "app_2SKx67EdpoN0G6j64rFvigXD"


chatgpt_oauth_client_id = "app_X8zY6vW2pQ9tR3dE7nK1jL5gH"


platform_oauth_redirect_uri = f"{platform_base}/auth/callback"


platform_oauth_audience = "https://api.openai.com/v1"


platform_auth0_client = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"




def is_dead_slot_error(detail: str) -> bool:
    """Slot-quality failures that retire the mailbox as used.

    These never heal by retrying, so they must not consume the transient
    retry budget: the task should keep switching to other slots instead.
    """
    text = str(detail or "").lower()
    return any(
        marker in text
        for marker in (
            "invalid_auth_step",
            "user_already_exists",
            "username_already_exists",
            "account_deactivated",
        )
    )


print_lock = threading.Lock()


stats_lock = threading.Lock()


_PASSWORD_AUTH_FLOW_GATE = threading.BoundedSemaphore(value=3)


stats = {"done": 0, "success": 0, "fail": 0, "skipped": 0, "start_time": 0.0}


register_log_sink = None




class PasswordResetRequired(RuntimeError):
    """Signal that a registration candidate already has a password.

    Registration must not rotate or reset credentials belonging to an existing
    account.  The batch scheduler handles this as a terminal skip so the
    mailbox is not retried through another proxy.
    """

    code = "password_reset_required"

    def __init__(self, detail: str = "", *, email: str = "") -> None:
        self.email = str(email or "").strip()
        suffix = f": {detail.strip()[:180]}" if str(detail or "").strip() else ""
        super().__init__(f"{self.code}{suffix}")




def log(text: str, color: str = "") -> None:
    colors = {"red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m"}
    if register_log_sink:
        try:
            register_log_sink(text, color)
        except Exception:
            pass
    with print_lock:
        trace = _REGISTRATION_TRACE.get()
        if trace is not None:
            try:
                trace.log_line(text, color)
            except Exception:
                pass
        prefix = colors.get(color, "")
        suffix = "\033[0m" if prefix else ""
        print(f"{prefix}{time.strftime('%H:%M:%S')} {text}{suffix}")




def step(index: int, text: str, color: str = "") -> None:
    log(f"[任务{index}] {text}", color)
    event = _TASK_EVENT.get()
    if callable(event):
        try:
            event(index, "running", step=text)
        except Exception:
            pass
    else:
        # Registration tasks install ``traced_task_event`` as ``_TASK_EVENT``;
        # that wrapper records the trace event before forwarding the live
        # callback.  Only retain the direct path for low-level callers that
        # use a trace without installing a task callback, avoiding duplicate
        # JSONL writes on the normal registration hot path.
        trace = _REGISTRATION_TRACE.get()
        if trace is not None:
            try:
                trace.event("running", step=text)
            except Exception:
                pass




def _make_trace_headers() -> dict[str, str]:
    # trace-id / parent-id 各只生成一次：x-datadog-* 头用十进制、traceparent 用
    # 对应的小写十六进制，保证两者描述的是同一条 trace（真实 RUM 上报行为）
    trace_id = random.getrandbits(64)
    parent_id = random.getrandbits(64)
    return {
        "traceparent": f"00-{format(trace_id, '032x')}-{format(parent_id, '016x')}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": str(parent_id),
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": str(trace_id),
    }




def _random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    value = list(
        secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + secrets.choice("!@#$%")
        + "".join(secrets.choice(chars) for _ in range(max(0, length - 4)))
    )
    random.shuffle(value)
    return "".join(value)




def _totp_code(secret: str, timestamp: float | None = None) -> str:
    return totp_code(secret, timestamp)




def _random_name() -> tuple[str, str]:
    return random.choice(["James", "Robert", "John", "Michael", "David", "Mary", "Emma", "Olivia"]), random.choice(
        ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller"]
    )




def _random_birthdate() -> str:
    return f"{random.randint(1996, 2006):04d}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"




def _response_json(resp) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}




def _is_wrong_email_otp_error(error: BaseException | str) -> bool:
    text = str(error or "").strip().lower()
    return "wrong_email_otp_code" in text or "wrong code" in text




def _auth_error_has_code(error: BaseException | str, code: str) -> bool:
    return str(code or "").strip().lower() in str(error or "").strip().lower()




def _mfa_activation_error_text(payload: Any) -> str:
    """Flatten activation error fields, including JSON-in-string details."""
    parts: list[str] = []

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 3 or value is None:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in {
                    "error",
                    "detail",
                    "message",
                    "code",
                    "type",
                }:
                    visit(item, depth + 1)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                visit(item, depth + 1)
            return
        text = str(value).strip()
        if not text:
            return
        parts.append(text)
        if text[:1] in "[{":
            try:
                visit(json.loads(text), depth + 1)
            except Exception:
                pass

    visit(payload)
    return " ".join(parts).lower()




def _otp_wait_timeout_message(mailbox: dict[str, Any], stage: str) -> str:
    message = f"等待{stage}验证码超时"
    provider = str(mailbox.get("provider") or "unknown").strip()
    attempts = max(0, int(mailbox.get("_code_wait_attempts") or 0))
    last_error = str(mailbox.get("_last_code_wait_error") or "").strip()
    details = [f"provider={provider}", f"polls={attempts}"]
    if last_error:
        details.append(f"last_error={last_error[:240]}")
    else:
        details.append("接口未返回新验证码")
    return f"{message} ({'; '.join(details)})"




def _response_debug_detail(resp, limit: int = 800) -> str:
    if resp is None:
        return ""
    data = _response_json(resp)
    parts = [
        f"url={str(getattr(resp, 'url', '') or '')[:300]}",
        f"content_type={str(getattr(resp, 'headers', {}).get('content-type') or '')}",
    ]
    for key in ("cf-ray", "x-request-id", "openai-processing-ms"):
        value = str(getattr(resp, "headers", {}).get(key) or "").strip()
        if value:
            parts.append(f"{key}={value}")
    if data:
        parts.append(f"json={json.dumps(data, ensure_ascii=False)[:limit]}")
    else:
        parts.append(f"body={str(getattr(resp, 'text', '') or '')[:limit]}")
    return ", ".join(parts)




def _is_cloudflare_challenge(resp) -> bool:
    if resp is None:
        return False
    try:
        status_code = int(getattr(resp, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status_code = 0
    if status_code not in (403, 503):
        return False
    text = str(getattr(resp, "text", "") or "").lower()
    return (
        "<title>just a moment" in text
        or "<title>attention required! | cloudflare" in text
        or "cf-chl-" in text
        or "__cf_chl_" in text
        or "cf-browser-verification" in text
    )




def _truthy(value: object, fallback: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return fallback




def _redact_operation_error(
    error: BaseException | str,
    *secrets: object,
    limit: int = 400,
) -> str:
    """Keep credential values out of maintenance logs and task events."""
    text = str(error or "").replace("\r", " ").replace("\n", " ")
    values = sorted(
        {
            str(value).strip()
            for value in secrets
            if str(value or "").strip()
        },
        key=len,
        reverse=True,
    )
    for value in values:
        text = text.replace(value, "[已隐藏]")
    return text[:limit]




def _operation_event_forwarder(
    task_event: Callable[..., Any] | None,
    account_log_sink: Callable[[dict[str, Any]], None] | None,
    *,
    operation: str,
    index: int,
    email: str = "",
    secrets: tuple[object, ...] = (),
) -> Callable[..., Any] | None:
    """Forward protocol task events to an optional per-account audit sink.

    Existing callers consume the historical callback signature
    ``(index, status, **fields)``.  The account log is deliberately a small,
    structured projection of that callback so credentials, proxy details and
    arbitrary provider payloads never enter the durable audit store.
    """
    if not callable(task_event) and not callable(account_log_sink):
        return task_event if callable(task_event) else None

    safe_operation = str(operation or "account_operation").strip()[:64]
    safe_email = str(email or "").strip()[:320]
    # Keep this mutable because security repair creates a password only after
    # the forwarder has been installed.  The operation can add that value
    # before invoking a provider so an echoed credential is still redacted.
    safe_secrets: list[object] = []
    for value in secrets or ():
        if str(value or "").strip() and not any(
            str(value) == str(existing) for existing in safe_secrets
        ):
            safe_secrets.append(value)

    def add_secrets(*values: object) -> None:
        for value in values:
            if not str(value or "").strip() or any(
                str(value) == str(existing) for existing in safe_secrets
            ):
                continue
            safe_secrets.append(value)

    def forward(event_index: int, status: str | None = None, **fields: Any) -> None:
        nonlocal safe_email
        # Keep the original callback behavior (including its exception
        # semantics), while guaranteeing a sink event when the callback is
        # well-behaved.  Sink failures are observational and must not change
        # the protocol result.
        try:
            if callable(task_event):
                task_event(event_index, status, **fields)
        finally:
            if not callable(account_log_sink):
                return
            event: dict[str, Any] = {
                "status": str(status or ""),
                "operation": safe_operation,
                "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "index": int(event_index) if str(event_index).lstrip("-").isdigit() else int(index),
            }
            event_email = str(fields.get("email") or safe_email).strip()
            if event_email:
                event["email"] = event_email[:320]
                safe_email = event_email[:320]
            # Registration completion is the first point where an Account ID
            # is guaranteed to be available.  Keep it in the small structured
            # projection so the durable log can bridge email-only attempts to
            # the later account-id identity.
            account_id = str(fields.get("account_id") or "").strip()
            if account_id:
                event["account_id"] = account_id[:320]
            trace_file = str(fields.get("trace_file") or "").strip()
            if trace_file:
                # Registration traces are resolved against the configured
                # trace directory by the API; retain only a bounded relative
                # path and never arbitrary provider payloads.
                event["trace_file"] = trace_file[:512]
            for key in (
                "step",
                "message",
                "error",
                "group_id",
                "skip_reason",
                "attempt",
            ):
                value = fields.get(key)
                if value is None or value == "":
                    continue
                event[key] = _redact_operation_error(value, *safe_secrets, limit=800)
            try:
                account_log_sink(event)
            except Exception:
                pass

    # This is intentionally an attribute rather than a second callback
    # argument, preserving the historical task-event callable signature.
    setattr(forward, "add_secrets", add_secrets)
    return forward




def _add_task_event_secrets(*values: object) -> None:
    """Extend the active event forwarder's redaction set when credentials appear."""
    callback = getattr(_TASK_EVENT.get(), "add_secrets", None)
    if callable(callback):
        try:
            callback(*values)
        except Exception:
            pass




def _tls_verify() -> bool | str:
    return current_runtime_settings().tls_verify




def _oauth_timeout() -> float:
    runtime = current_runtime_settings()
    return runtime.timeout_for(runtime.oauth_timeout)




def _protocol_proxy_for_task(
    proxy_pool: list[str],
    task_index: int,
    attempt: int,
    *,
    rotate: bool,
    start_offset: int = 0,
    retry_stride: int = 1,
    excluded: set[str] | None = None,
) -> str:
    """Choose a stable proxy slot for a task and advance it on retry."""
    if not proxy_pool:
        return ""
    blocked = excluded or set()
    if not rotate and not blocked:
        return proxy_pool[0]
    slot = (
        int(start_offset)
        + max(0, int(task_index) - 1)
        + max(0, int(attempt)) * max(1, int(retry_stride))
    ) % len(proxy_pool)
    for offset in range(len(proxy_pool)):
        candidate = proxy_pool[(slot + offset) % len(proxy_pool)]
        if candidate not in blocked:
            return candidate
    return proxy_pool[slot]




def _mail_config(register_proxy: str = "") -> dict:
    scoped_mail = _MAIL_CONFIG.get()
    mail = scoped_mail if isinstance(scoped_mail, dict) else (
        config["mail"] if isinstance(config.get("mail"), dict) else {}
    )
    use_register_proxy = _truthy(mail.get("api_use_register_proxy"), True)
    proxy = str(register_proxy or "").strip() if use_register_proxy else ""
    return {**mail, "api_use_register_proxy": use_register_proxy, "proxy": proxy}




def create_mailbox(
    username: str | None = None,
    register_proxy: str = "",
    *,
    allow_used: bool = False,
) -> dict:
    return allocate_mailbox(
        _mail_config(register_proxy),
        username,
        provider_type=_PROVIDER_TYPE.get(),
        allow_used=allow_used,
    )




def wait_for_code(mailbox: dict, register_proxy: str = "") -> str | None:
    mail_config = _mail_config(register_proxy)
    fallback_code = str(
        mailbox.get("_post_registration_otp_fallback") or ""
    ).strip()
    if re.fullmatch(r"\d{6}", fallback_code):
        mail_config = dict(mail_config)
        mail_config["wait_timeout"] = min(
            60.0,
            float(mail_config.get("wait_timeout") or 90),
        )
    code = read_mail_code(
        mail_config,
        mailbox,
        stop_check=_STOP_CHECK.get(),
    )
    if code:
        mailbox.pop("_post_registration_otp_fallback", None)
        return code
    if re.fullmatch(r"\d{6}", fallback_code):
        mailbox.pop("_post_registration_otp_fallback", None)
        mailbox["_post_registration_otp_fallback_used"] = True
        return fallback_code
    return None

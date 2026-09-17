"""Retry the password-login completion of an owned, partially saved account."""
from __future__ import annotations

import time
from typing import Any, Callable
from urllib.parse import quote

from proxy_util import is_auth_identity_error, is_rate_limit_error
from register.protocol_runtime import ProtocolRuntimeSettings
from register.registration_outcome import RegisteredEmailDetected
from register.sessions import JobCancelled

CREDENTIAL_FIELDS = {"password", "access_token", "refresh_token", "session_token", "id_token", "totp_secret", "account_id", "mfa_factor_id"}


def is_registration_guard_error(error: BaseException | str | None) -> bool:
    """An unconfirmed registration identity must not enter a retry workflow."""
    return any(marker in str(error or "") for marker in (
        "registration_password_route_unavailable", "registration_identity_response_unconfirmed",
        "registration_identity_response_unavailable", "registration_auth_identity_unconfirmed",
        "registration_auth_http_", "registration_auth_failed",
    ))


def merge_registration_progress(saved: dict[str, Any], update: dict[str, Any]) -> None:
    for key, value in update.items():
        if key in CREDENTIAL_FIELDS and saved.get(key) and not str(value or "").strip():
            continue
        if key in {"password_set", "mfa_enabled"} and saved.get(key) is True and value is False:
            continue
        saved[key] = value


def can_recover_registration(account: dict[str, Any], mailbox: dict[str, Any] | None) -> bool:
    if (mailbox or {}).get("_registration_guard_error"):
        return False
    if account.get("registration_recovery_state") == "identity_unconfirmed" or any(
        is_auth_identity_error(value) or is_registration_guard_error(value) for value in (
            account.get("registration_initial_error"), account.get("registration_recovery_error"),
            (mailbox or {}).get("_registration_identity_error"),
            (mailbox or {}).get("_registration_guard_error"),
        )
    ):
        return False
    if (mailbox or {}).get("_registration_outcome") == "already_registered":
        return False
    if account.get("registration_recovery_state") == "awaiting_email_verification" and not (mailbox or {}).get("_otp_accepted_by_server"):
        return False
    if (mailbox or {}).get("_registration_identity_created") and not (mailbox or {}).get("_otp_accepted_by_server"):
        # A newly saved password does not prove that email verification ended.
        # Preserve the original mailbox failure instead of starting another login.
        return False
    owned = bool((mailbox or {}).get("_registration_identity_created")) or account.get("registration_state") == "security_pending"
    return bool(owned and str(account.get("email") or "").strip() and str(account.get("password") or "").strip() and not str(account.get("access_token") or "").strip())


def _recover_once(index: int, **kwargs):
    from register.protocol_ops import recover_pending_registration_one
    return recover_pending_registration_one(index, **kwargs)


def recover_registration(
    index: int, *, account: dict[str, Any], mailbox: dict[str, Any] | None,
    mail_config: dict[str, Any], proxy: str = "", stop_check: Callable[[], bool] | None = None,
    task_event=None, partial_sink=None, protocol_runtime: ProtocolRuntimeSettings | None = None,
) -> dict[str, Any]:
    """Up to three completion attempts; stop immediately on explicit limits."""
    if not can_recover_registration(account, mailbox):
        raise RuntimeError("registration_recovery_not_eligible")
    stop = stop_check or (lambda: False)
    saved = dict(account)
    runtime = protocol_runtime or ProtocolRuntimeSettings()

    def persist(update: dict[str, Any]) -> None:
        merge_registration_progress(saved, update)
        saved["registration_state"] = "security_pending"
        if callable(partial_sink):
            partial_sink(dict(saved))

    def progress_event(event_index: int, status: str | None = None, **fields: Any) -> None:
        # Only the owning scheduler decides when the account task is terminal.
        # An inner login failure can still be retried by the next attempt.
        if callable(task_event):
            task_event(event_index, "running" if status in {"failed", "success"} else status, **fields)

    for attempt in range(1, 4):
        if stop():
            raise JobCancelled("stop requested")
        persist({"registration_recovery_attempts": attempt, "registration_recovery_state": "running"})
        if callable(task_event):
            task_event(index, "running", email=saved["email"], step=f"密码已保存，正在密码登录补全 AT 和 2FA（第 {attempt} 次）")
        try:
            result = _recover_once(
                index, account=dict(saved), mail_config=mail_config, proxy=proxy, mailbox_override=mailbox,
                owned_new_identity=bool((mailbox or {}).get("_registration_identity_created")),
                stop_check=stop, task_event=progress_event, partial_sink=persist,
                protocol_runtime=runtime.with_deadline(time.monotonic() + runtime.account_timeout),
                max_attempts=1,
            )
            if not isinstance(result, dict) or not all(str(result.get(key) or "").strip() for key in ("access_token", "account_id", "password", "totp_secret")) or result.get("mfa_enabled") is not True:
                raise RuntimeError("registration_recovery_incomplete_result")
            result.update(registration_recovery_state="complete", registration_recovery_attempts=attempt,
                          registration_recovery_error="", registration_state="complete")
            return result
        except JobCancelled:
            persist({"registration_recovery_state": "cancelled"})
            raise
        except Exception as error:
            failure_text = str(error).lower()
            detail = str(error)
            secrets = [str(saved.get(key) or "") for key in CREDENTIAL_FIELDS]
            secrets.append(str((mailbox or {}).get("_smsbower_api_key") or ""))
            secrets.append(str((mailbox or {}).get("api_key") or ""))
            for value in secrets:
                if value:
                    detail = detail.replace(value, "[REDACTED]").replace(quote(value, safe=""), "[REDACTED]")
            identity_missing = is_auth_identity_error(error)
            if identity_missing and not detail.startswith("username_not_found:"):
                detail = "username_not_found: " + detail
            rate_limited = not identity_missing and is_rate_limit_error(error)
            if rate_limited and "rate_limit_exceeded" not in detail.lower():
                # The status may only live on the exception.response. Keep a
                # semantic marker when outer schedulers inspect the message.
                detail = "rate_limit_exceeded: " + detail
            persist({
                "registration_recovery_state": "identity_unconfirmed" if identity_missing else ("rate_limited" if rate_limited else "pending"),
                "registration_recovery_error": detail[:300],
            })
            if identity_missing:
                if mailbox is not None:
                    mailbox["_registration_identity_error"] = "username_not_found"
                    mailbox["_registration_recovery_pending"] = False
                if callable(task_event):
                    task_event(index, "running", email=saved["email"],
                        step="认证接口未找到当前身份，已停止自动恢复；原有凭据已保留",
                        error="username_not_found")
                raise RuntimeError("registration_recovery_pending: " + detail[:300]) from error
            if rate_limited:
                if callable(task_event):
                    task_event(
                        index, "running", email=saved["email"],
                        step="服务端限流，已停止本轮自动恢复；密码和安全字段已保留",
                        error="auth_rate_limit_exceeded",
                    )
                raise RuntimeError("registration_recovery_pending: " + detail[:300]) from error
            permanent = any(marker in failure_text for marker in (
                "incorrect_password", "invalid_password", "invalid_credentials", "invalid_username_or_password", "account_deactivated",
                "already_registered", "not_eligible", "totp_required", "mfa_secret_missing", "mailbox_unavailable",
                "no activation found", "activation is already", "phone_verification_required",
                "saved_password_required", "owned_identity_and_saved_password_required", "registered_candidate_rejected",
                "existing_totp_credentials_required", "email_mailbox_required", "registration_or_reset_route_rejected",
                "account_identity_mismatch", "mailbox_identity_mismatch", "password_route_unavailable", "totp_secret_missing",
            ))
            if permanent or attempt == 3:
                if "password_route_unavailable" in failure_text:
                    # 密码路由不存在 => 该身份无密码（不是本事务创建的回收邮箱）。
                    # 密码登录恢复对这类身份必然失败，按"已注册"跳过而不是失败挂起；
                    # 我们自己的半成品重试密码已绑定，不会走到这个分支。
                    if mailbox is not None:
                        mailbox["_registration_outcome"] = RegisteredEmailDetected.code
                    if callable(task_event):
                        task_event(
                            index,
                            "running",
                            email=str(saved.get("email") or ""),
                            step="已存在身份无密码路由（回收邮箱），按已注册跳过",
                        )
                    raise RegisteredEmailDetected(
                        "recovery_password_route_missing: existing_passwordless_identity",
                        email=str(saved.get("email") or ""),
                    ) from error
                raise RuntimeError("registration_recovery_pending: " + detail[:300]) from error
            deadline = time.monotonic() + min(2 ** (attempt - 1), 4)
            while time.monotonic() < deadline:
                if stop():
                    persist({"registration_recovery_state": "cancelled"})
                    raise JobCancelled("stop requested")
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
    raise RuntimeError("registration_recovery_pending")

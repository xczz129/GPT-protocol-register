"""协议层单账号操作：安全修复、密码登录、改密、改 2FA。"""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable
from common.session_export import parse_jwt_payload
from register.sessions import JobCancelled
from mail import mark_mailbox_code_boundary, mark_mailbox_result, release_mailbox
from proxy_util import is_proxy_transport_error, is_rate_limit_error, proxy_brief
from register.protocol_runtime import (
    ProtocolRuntimeSettings,
    activate_runtime,
    current_runtime_settings,
)
from register.util import _append_account

from register.protocol_context import (
    _FIXED_EMAIL,
    _MAIL_CONFIG,
    _PROVIDER_TYPE,
    _PROXY_CONFIG,
    _STOP_CHECK,
    _TASK_EVENT,
    _operation_event_forwarder,
    _random_password,
    _redact_operation_error,
    _response_json,
    _truthy,
    chatgpt_oauth_client_id,
    create_mailbox,
    log,
    step,
)

from register.protocol_fingerprint import _browser_fingerprint

from register.protocol_registrar import PlatformRegistrar


def can_recover_pending_registration(
    account: dict[str, Any], *, mailbox_override: dict[str, Any] | None = None,
    owned_new_identity: bool = False,
) -> bool:
    """Accept saved pending records or this attempt's confirmed new identity.

    Historical pending records can predate password_set; the saved password is
    still verified by login. A random candidate without either ownership proof
    is insufficient. An explicit already-registered skip is never recoverable.
    """
    if not isinstance(account, dict):
        return False
    email = str(account.get("email") or "").strip().lower()
    if not email or not str(account.get("password") or "").strip():
        return False
    mailbox = mailbox_override if isinstance(mailbox_override, dict) else {}
    if any(str(source.get("_registration_outcome") or source.get("registration_outcome") or "") == "already_registered"
           for source in (account, mailbox)):
        return False
    if mailbox_override is not None and str(mailbox.get("address") or "").strip().lower() != email:
        return False
    saved_pending = str(account.get("registration_state") or "").strip().lower() == "security_pending"
    fresh_owned = bool(owned_new_identity and mailbox.get("_registration_identity_created"))
    return saved_pending or fresh_owned


def recover_pending_registration_one(
    index: int,
    *,
    account: dict[str, Any],
    mail_config: dict[str, Any],
    proxy: str = "",
    provider_type: str | None = None,
    mailbox_override: dict[str, Any] | None = None,
    owned_new_identity: bool = False,
    stop_check: Callable[[], bool] | None = None,
    task_event=None,
    partial_sink: Callable[[dict[str, Any]], None] | None = None,
    account_log_sink: Callable[[dict[str, Any]], None] | None = None,
    proxy_config: dict[str, Any] | None = None,
    protocol_runtime: ProtocolRuntimeSettings | None = None,
    max_attempts: int = 2,
) -> dict[str, Any]:
    """Recover AT and verified MFA using the pending account's existing password.

    This function never allocates mail, calls user/register, resets passwords or
    disables an unknown factor. The caller owns the mailbox lease and final
    persistence; partial_sink receives credential updates before later steps.
    """
    if not can_recover_pending_registration(
        account, mailbox_override=mailbox_override, owned_new_identity=owned_new_identity,
    ):
        raise RuntimeError("pending_recovery_owned_identity_and_saved_password_required")
    email = str(account["email"]).strip()
    password = str(account["password"]).strip()
    secret = str(account.get("totp_secret") or "").strip()
    stop = stop_check if callable(stop_check) else lambda: False
    event = _operation_event_forwarder(
        task_event, account_log_sink, operation="registration_recovery", index=index,
        email=email, secrets=(password, secret, str((mailbox_override or {}).get("_smsbower_api_key") or ""), str((mailbox_override or {}).get("api_key") or "")),
    )
    context_tokens = (
        (_TASK_EVENT, _TASK_EVENT.set(event if callable(event) else None)),
        (_STOP_CHECK, _STOP_CHECK.set(stop)),
        (_FIXED_EMAIL, _FIXED_EMAIL.set(email)),
        (_PROVIDER_TYPE, _PROVIDER_TYPE.set(provider_type or account.get("provider_type"))),
        (_MAIL_CONFIG, _MAIL_CONFIG.set(dict(mail_config))),
        (_PROXY_CONFIG, _PROXY_CONFIG.set(dict(proxy_config or {}))),
    )
    runtime = replace(protocol_runtime or ProtocolRuntimeSettings(), verify_chatgpt_me=True,
                      enable_totp_after_register=True)
    if runtime.deadline is None:
        runtime = runtime.with_deadline(time.monotonic() + runtime.account_timeout)
    runtime_context = activate_runtime(runtime)
    runtime_context.__enter__()
    registrar: PlatformRegistrar | None = None
    result = dict(account)
    stage = "password_login"

    def ensure_running() -> None:
        if stop():
            raise JobCancelled("stop requested")
        if current_runtime_settings().expired():
            raise RuntimeError("pending_recovery_account_timeout")

    def save_partial(update: dict[str, Any]) -> None:
        credential_keys = {"password", "totp_secret", "access_token", "session_token", "refresh_token", "id_token", "account_id"}
        update = {
            key: value for key, value in update.items()
            if not (key in credential_keys and value in (None, "", {}, []) and result.get(key) not in (None, "", {}, []))
            and not (key in {"mfa_enabled", "password_set"} and value is False and result.get(key) is True)
        }
        add_secrets = getattr(event, "add_secrets", None)
        if callable(add_secrets):
            add_secrets(update.get("password"), update.get("totp_secret"), update.get("access_token"), update.get("session_token"))
        result.update(update)
        if callable(partial_sink):
            partial_sink(dict(update))

    def validate_tokens(tokens: dict[str, Any]) -> None:
        _validate_protocol_login_result(tokens)
        payload = parse_jwt_payload(str(tokens.get("access_token") or "")) or {}
        expiration = payload.get("exp")
        try:
            if expiration is not None and float(expiration) <= time.time():
                raise RuntimeError("pending_recovery_access_token_expired")
        except (TypeError, ValueError) as error:
            raise RuntimeError("pending_recovery_access_token_expiry_invalid") from error
        profile = payload.get("https://api.openai.com/profile")
        session = tokens.get("session")
        user = session.get("user") if isinstance(session, dict) else {}
        returned_emails = [payload.get("email"), (profile or {}).get("email") if isinstance(profile, dict) else None,
                           (user or {}).get("email") if isinstance(user, dict) else None]
        if any(str(value).strip().lower() != email.lower() for value in returned_emails if value):
            raise RuntimeError("pending_recovery_account_identity_mismatch")
        original_id = str(account.get("account_id") or "").strip()
        if original_id and original_id != str(tokens.get("account_id") or "").strip():
            raise RuntimeError("pending_recovery_account_identity_mismatch")

    def save_tokens(tokens: dict[str, Any]) -> None:
        validate_tokens(tokens)
        decorated = _decorate_protocol_tokens(registrar, {key: value for key, value in tokens.items() if not key.startswith("_")})
        save_partial({**decorated, "password": password, "password_set": True,
                      "registration_state": "security_pending", "last_security_error": ""})

    def mfa_info(tokens: dict[str, Any]) -> dict[str, Any]:
        response, error = registrar._chatgpt_mfa_request("get", "/backend-api/accounts/mfa_info", tokens["access_token"], index)
        if response is None or getattr(response, "status_code", 0) != 200:
            raise RuntimeError(error or "pending_recovery_mfa_info_unavailable")
        return _response_json(response)

    def confirms_factor(info: dict[str, Any], factor_id: str) -> bool:
        factors = info.get("factors")
        explicit = isinstance(factors, dict) and isinstance(factors.get("totp"), list)
        values = factors.get("totp", []) if isinstance(factors, dict) else factors
        if not isinstance(values, list):
            return False
        return bool(factor_id) and any(
            isinstance(factor, dict) and str(factor.get("id") or "") == factor_id
            and not _truthy(factor.get("is_recovery"))
            and (explicit or str(factor.get("type") or factor.get("factor_type") or "").lower() == "totp")
            for factor in values
        )

    try:
        ensure_running()
        save_partial({"registration_state": "security_pending", "registration_recovery_stage": stage})
        attempt_limit = max(1, min(3, int(max_attempts or 2)))
        tokens: dict[str, Any] = {}
        for attempt in range(attempt_limit):
            ensure_running()
            if mailbox_override is not None:
                from mail import prepare_smsbower_next_code

                prepare_smsbower_next_code(mailbox_override)
            registrar = PlatformRegistrar(proxy)
            try:
                tokens = registrar.login_pending_with_password(email, password, index,
                    mailbox=mailbox_override, totp_secret=secret)
                validate_tokens(tokens)
                break
            except Exception as error:
                retryable = not isinstance(error, JobCancelled) and not is_rate_limit_error(error) and (
                    is_proxy_transport_error(error) or any(marker in str(error).lower() for marker in (
                        "chatgpt_session_incomplete", "http_502", "http_503", "http_504", "invalid_state",
                    ))
                )
                registrar.close()
                registrar = None
                if not retryable or attempt + 1 >= attempt_limit:
                    raise
                step(index, "恢复登录遇到临时网络/会话错误，关闭旧会话后重试", "yellow")
                for _ in range(10 * (attempt + 1)):
                    ensure_running()
                    time.sleep(0.1)
        ensure_running()
        save_tokens(tokens)
        stage = "verify_2fa"
        save_partial({"registration_recovery_stage": stage})
        info = mfa_info(tokens)
        enabled = _truthy(info.get("mfa_enabled")) or _truthy(info.get("mfa_enabled_v2"))
        if enabled:
            if not secret:
                raise RuntimeError("pending_recovery_remote_totp_secret_missing")
            if not tokens.get("_pending_totp_verified"):
                # Possessing a string in the DB is not verification. Confirm
                # it against the active factor without rotating that factor.
                ensure_running()
                tokens = registrar.login_with_password_totp(email, password, secret, index)
                save_tokens(tokens)
                info = mfa_info(tokens)
            factor_id = str(tokens.get("mfa_factor_id") or "")
            if not confirms_factor(info, factor_id) or not (
                _truthy(info.get("mfa_enabled")) or _truthy(info.get("mfa_enabled_v2"))
            ):
                raise RuntimeError("pending_recovery_totp_verification_not_confirmed")
            save_partial({"totp_secret": secret, "mfa_enabled": True, "mfa_factor_id": factor_id,
                          "mfa_factor_type": "totp", "totp_setup_pending": False})
        else:
            ensure_running()
            stage = "enable_2fa"
            save_partial({"registration_recovery_stage": stage})
            security = registrar._enroll_totp(tokens, index, credential_sink=save_partial)
            save_partial(security)
            # Enrollment already verifies the exact activated factor against
            # mfa_info before returning. Incomplete/mocked responses fail here.
            if not str(result.get("totp_secret") or "").strip() or result.get("mfa_enabled") is not True:
                raise RuntimeError("pending_recovery_totp_enrollment_incomplete")
        ensure_running()
        _validate_protocol_account_result(result)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        save_partial({"registration_state": "complete", "registration_recovery_stage": "complete",
                      "registration_recovered_at": now, "password_verified_at": now,
                      "totp_verified_at": now, "last_security_error": ""})
        if callable(event):
            event(index, "success", email=email, step="已用保存密码恢复 AT，并完成 2FA 复核")
        return dict(result)
    except Exception as error:
        safe = _redact_operation_error(error, password, secret, result.get("totp_secret"),
                                       result.get("access_token"), result.get("session_token"),
                                       (mailbox_override or {}).get("_smsbower_api_key"), (mailbox_override or {}).get("api_key"))
        save_partial({"registration_state": "security_pending", "registration_recovery_stage": stage,
                      "last_security_error": safe[:300]})
        if callable(event):
            event(index, "cancelled" if isinstance(error, JobCancelled) else "failed", email=email,
                  step="恢复已停止，已保存当前进展" if isinstance(error, JobCancelled) else "恢复未完成，保留待补全状态",
                  error=safe[:300])
        if isinstance(error, JobCancelled):
            raise
        raise RuntimeError(f"pending_recovery_failed: {safe}") from error
    finally:
        if registrar is not None:
            registrar.close()
        runtime_context.__exit__(None, None, None)
        for variable, token in reversed(context_tokens):
            variable.reset(token)




def repair_security_one(
    index: int,
    *,
    account: dict[str, Any],
    mail_config: dict[str, Any],
    proxy: str = "",
    provider_type: str | None = None,
    stop_check: Callable[[], bool] | None = None,
    task_event=None,
    account_log_sink: Callable[[dict[str, Any]], None] | None = None,
    proxy_config: dict[str, Any] | None = None,
    partial_sink: Callable[[dict[str, Any]], None] | None = None,
    protocol_runtime: ProtocolRuntimeSettings | None = None,
    force_password_confirmation: bool = False,
    mailbox_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Complete browser security in one protocol session.

    Browser registration is followed by one email-code protocol login. The
    resulting session is used to set the password and enroll TOTP; the final
    AT is then read from that same session without a second password/TOTP
    login.
    """
    email = str(account.get("email") or "").strip()
    if not email:
        raise RuntimeError("安全字段补全缺少邮箱")
    if mailbox_override is not None and (
        not isinstance(mailbox_override, dict)
        or str(mailbox_override.get("address") or "").strip().lower() != email.lower()
    ):
        raise RuntimeError("安全字段补全预分配邮箱与目标账号不一致")

    existing_password = str(account.get("password") or "").strip()
    existing_totp = str(account.get("totp_secret") or "").strip()
    is_browser_registration = (
        str(account.get("registration_method") or "").strip().lower()
        == "browser"
    )
    # A browser registration may contain a generated candidate that was never
    # submitted. Only an explicit server-confirmed flag makes it reusable.
    needs_password = (
        bool(force_password_confirmation)
        or not existing_password
        or account.get("password_set") is not True
    )
    needs_totp = not existing_totp
    if not needs_password and not needs_totp:
        return {
            "password": existing_password,
            "password_set": True,
            "totp_secret": existing_totp,
            "mfa_enabled": bool(account.get("mfa_enabled")) or bool(existing_totp),
        }
    remote_mfa_enabled = _truthy(account.get("mfa_enabled"))

    stop = stop_check if callable(stop_check) else (lambda: False)
    task_event = _operation_event_forwarder(
        task_event,
        account_log_sink,
        operation="security_repair",
        index=index,
        email=email,
        secrets=(existing_password, existing_totp),
    )
    event_token = _TASK_EVENT.set(task_event if callable(task_event) else None)
    stop_token = _STOP_CHECK.set(stop)
    email_token = _FIXED_EMAIL.set(email)
    provider_token = _PROVIDER_TYPE.set(provider_type)
    mail_config_token = _MAIL_CONFIG.set(dict(mail_config))
    proxy_config_token = _PROXY_CONFIG.set(dict(proxy_config or {}))
    runtime_context = activate_runtime(protocol_runtime or ProtocolRuntimeSettings())
    runtime_context.__enter__()
    registrar: PlatformRegistrar | None = None
    mailbox: dict[str, Any] | None = mailbox_override
    fields: dict[str, Any] = {}
    protocol_tokens: dict[str, Any] = {}
    early_totp_replaced = False

    def ensure_running() -> None:
        if stop():
            raise JobCancelled("stop requested")

    def save_partial(update: dict[str, Any]) -> None:
        fields.update(update)
        if callable(partial_sink):
            partial_sink(dict(update))

    # Password and MFA flows can refresh the authenticated protocol session.
    # Keep all credential/session fields in the same incremental persistence
    # path so a successful repair never leaves the account with stale tokens.
    protocol_token_keys = (
        "access_token",
        "refresh_token",
        "id_token",
        "session_token",
        "account_id",
        "plan_type",
        "expires_at",
        "session",
        "cookies",
        "profile",
        "oauth_client_id",
        "mfa_factor_id",
        "mfa_factor_type",
    )

    def save_protocol_tokens(tokens: dict[str, Any] | None = None) -> None:
        source = tokens if isinstance(tokens, dict) else protocol_tokens
        snapshot = {
            key: source[key]
            for key in protocol_token_keys
            if source.get(key) not in (None, "", {})
            and source.get(key) != fields.get(key, account.get(key))
        }
        if snapshot:
            save_partial(snapshot)

    def probe_remote_mfa(token_source: dict[str, Any] | None = None) -> None:
        """Refresh the remote MFA flag once an authenticated AT is available."""
        nonlocal remote_mfa_enabled
        if remote_mfa_enabled or is_browser_registration:
            return
        source = token_source if isinstance(token_source, dict) else protocol_tokens
        probe_token = str(
            source.get("access_token")
            or account.get("access_token")
            or ""
        ).strip()
        if not probe_token:
            return
        try:
            probe_response, _probe_error = registrar._chatgpt_mfa_request(
                "get",
                "/backend-api/accounts/mfa_info",
                probe_token,
                index,
            )
            probe_info = _response_json(probe_response)
            if (
                probe_response is not None
                and getattr(probe_response, "status_code", 0) == 200
                and isinstance(probe_info, dict)
            ):
                remote_mfa_enabled = _truthy(
                    probe_info.get("mfa_enabled")
                ) or _truthy(probe_info.get("mfa_enabled_v2"))
        except Exception:
            # Keep the normal enrollment path as the fallback when a probe is
            # unavailable or the endpoint is temporarily inconsistent.
            pass

    def merged_protocol_tokens(source: dict[str, Any] | None = None) -> dict[str, Any]:
        """Overlay freshly obtained credentials on the persisted account data."""
        merged = {
            key: account.get(key)
            for key in protocol_token_keys
            if account.get(key) not in (None, "", {})
        }
        if isinstance(source, dict):
            merged.update(
                {
                    key: value
                    for key, value in source.items()
                    if value not in (None, "", {})
                }
            )
        return merged

    def has_session_credentials(source: dict[str, Any] | None = None) -> bool:
        # A freshly returned AT must be accompanied by the session identity
        # from that same response. Do not combine a new AT with stale
        # session/account fields from the persisted record.
        if isinstance(source, dict) and str(
            source.get("access_token") or ""
        ).strip():
            if not all(
                str(source.get(key) or "").strip()
                for key in ("session_token", "account_id")
            ):
                return False
        merged = merged_protocol_tokens(source)
        return all(
            str(merged.get(key) or "").strip()
            for key in ("access_token", "session_token", "account_id")
        )

    def refresh_incomplete_session() -> None:
        """Fill session identity fields after an AT-only MFA operation."""
        password_for_session = str(
            fields.get("password") or existing_password or ""
        ).strip()
        if is_browser_registration or not password_for_session:
            return
        if has_session_credentials(protocol_tokens):
            return
        refreshed = registrar._read_chatgpt_session(
            email,
            password_for_session,
            index,
        )
        if not isinstance(refreshed, dict):
            raise RuntimeError("security_repair_session_empty_result")
        missing = [
            key
            for key in ("access_token", "session_token", "account_id")
            if not str(refreshed.get(key) or "").strip()
        ]
        if missing:
            raise RuntimeError(
                "security_repair_session_incomplete: " + ", ".join(missing)
            )
        protocol_tokens.update(refreshed)
        save_protocol_tokens(refreshed)

    try:
        ensure_running()
        if needs_password or is_browser_registration:
            if mailbox is None:
                mailbox = create_mailbox(
                    email,
                    register_proxy=proxy,
                    allow_used=True,
                )
            if str(mailbox.get("address") or "").strip().lower() != email.lower():
                raise RuntimeError("安全字段补全邮箱池未返回目标邮箱")
        registrar = PlatformRegistrar(proxy)
        if is_browser_registration:
            ensure_running()
            step(index, "浏览器注册完成，开始协议邮箱登录")
            protocol_tokens = registrar.login(email, mailbox, index)
            if not isinstance(protocol_tokens, dict):
                raise RuntimeError("browser_protocol_login_empty_result")
        elif not needs_password:
            stored_cookies = account.get("cookies")
            if isinstance(stored_cookies, dict):
                for name, value in stored_cookies.items():
                    if str(name or "").strip() and value is not None:
                        try:
                            registrar.session.cookies.set(str(name), str(value))
                        except Exception:
                            continue

        # The persisted flag can lag behind the account-settings service. If
        # a local TOTP secret is missing, probe the authenticated session so a
        # remotely enabled factor is replaced before the password step.
        if needs_totp and not is_browser_registration and not remote_mfa_enabled:
            probe_remote_mfa()

        # If the server still has an active factor but the local secret is
        # missing, replace that factor before attempting a password reset.
        # The reset endpoint itself requires MFA when a factor exists, so
        # doing the password step first would strand the account with neither
        # a usable local secret nor a confirmed password.  An existing AT is
        # sufficient for the ChatGPT settings API and avoids asking for the
        # missing TOTP during this bootstrap phase.
        if (
            needs_totp
            and remote_mfa_enabled
            and not is_browser_registration
        ):
            early_tokens = dict(protocol_tokens) if protocol_tokens else {
                "access_token": str(account.get("access_token") or "").strip(),
                "session_token": str(account.get("session_token") or "").strip(),
                "account_id": str(account.get("account_id") or "").strip(),
            }
            if not str(early_tokens.get("access_token") or "").strip():
                raise RuntimeError(
                    "mfa_secret_missing_remote_factor_requires_access_token"
                )
            protocol_tokens.update(early_tokens)
            ensure_running()
            step(index, "本地缺少 TOTP，先替换远端旧因子再继续密码流程")
            replacement = registrar._enroll_totp(
                early_tokens,
                index,
                replace_existing=True,
            )
            save_partial(replacement)
            needs_totp = False
            early_totp_replaced = True
            existing_totp = str(
                fields.get("totp_secret") or replacement.get("totp_secret") or ""
            ).strip()

        if needs_password:
            password_flow_baseline = {
                key: str(
                    protocol_tokens.get(key)
                    or account.get(key)
                    or ""
                ).strip()
                for key in ("access_token", "session_token", "account_id")
            }
            password = _random_password()
            add_forwarder_secret = getattr(task_event, "add_secrets", None)
            if callable(add_forwarder_secret):
                add_forwarder_secret(password)
            password_kwargs: dict[str, Any] = {
                "totp_secret": str(
                    fields.get("totp_secret") or existing_totp
                ).strip()
            }
            # The password transaction may perform a TOTP login and return a
            # fresh AT/session even when no early MFA replacement is needed.
            # Always collect those credentials for persistence.
            password_kwargs["credential_sink"] = protocol_tokens.update
            password = registrar._complete_post_registration_password(
                email,
                password,
                mailbox or {},
                index,
                **password_kwargs,
            )
            # A callback may rotate only the AT while leaving the pre-password
            # session/account identifiers in place. Drop those stale values so
            # the complete-session refresh below cannot mix credential epochs.
            callback_access = str(
                protocol_tokens.get("access_token") or ""
            ).strip()
            callback_access_changed = bool(
                callback_access
                and callback_access != password_flow_baseline["access_token"]
            )
            if callback_access_changed:
                callback_session = str(
                    protocol_tokens.get("session_token") or ""
                ).strip()
                callback_account = str(
                    protocol_tokens.get("account_id") or ""
                ).strip()
                if (
                    not callback_session
                    or not callback_account
                    or callback_session == password_flow_baseline["session_token"]
                    or callback_account == password_flow_baseline["account_id"]
                ):
                    protocol_tokens.pop("session_token", None)
                    protocol_tokens.pop("account_id", None)
            if callable(add_forwarder_secret):
                add_forwarder_secret(password)
            ensure_running()
            confirmed_fields: dict[str, Any] = {
                "password": password,
                "password_set": True,
            }
            if force_password_confirmation:
                confirmed_fields["password_verified_at"] = datetime.now(
                    timezone.utc
                ).isoformat().replace("+00:00", "Z")
            save_partial(confirmed_fields)
            if not force_password_confirmation and (
                has_session_credentials(protocol_tokens)
                or not (
                    password_flow_baseline["session_token"]
                    or password_flow_baseline["account_id"]
                )
            ):
                save_protocol_tokens()

            # A password-add callback can complete without returning OAuth
            # credentials (for example when the account already has TOTP).
            # Do not report success with a password-only record: recover the
            # authenticated session and persist its AT/session identifiers.
            if (
                not is_browser_registration
                and not needs_totp
                and not has_session_credentials(protocol_tokens)
            ):
                refreshed = registrar._read_chatgpt_session(
                    email,
                    str(fields.get("password") or password or "").strip(),
                    index,
                )
                if not isinstance(refreshed, dict) or not str(
                    refreshed.get("access_token") or ""
                ).strip():
                    raise RuntimeError("security_repair_session_missing_access_token")
                else:
                    protocol_tokens.update(refreshed)
                    save_protocol_tokens(refreshed)

        if needs_totp:
            ensure_running()
            tokens = dict(protocol_tokens) if protocol_tokens else {
                "access_token": str(account.get("access_token") or "").strip(),
                "session_token": str(account.get("session_token") or "").strip(),
                "account_id": str(account.get("account_id") or "").strip(),
            }
            password_for_session = str(
                fields.get("password") or existing_password
            ).strip()
            # When only the local TOTP secret is missing, an existing AT is
            # sufficient for the ChatGPT settings API.  Avoid starting a new
            # password+TOTP login (which would require the very secret being
            # repaired) and use that token directly.
            can_use_stored_access_token = bool(
                str(tokens.get("access_token") or "").strip()
                and not is_browser_registration
            )
            needs_session_refresh = not has_session_credentials(tokens)
            if password_for_session and (
                not can_use_stored_access_token or needs_session_refresh
            ):
                try:
                    session_tokens = registrar._read_chatgpt_session(
                        email,
                        password_for_session,
                        index,
                    )
                except RuntimeError as session_error:
                    if is_browser_registration:
                        raise RuntimeError(
                            "browser_protocol_session_lost_after_password: "
                            f"{str(session_error)[:240]}"
                        ) from session_error
                    step(
                        index,
                        "现有 ChatGPT 会话已失效，使用邮箱 OTP 重建协议会话",
                        "yellow",
                    )
                    if mailbox is None:
                        mailbox = create_mailbox(
                            email,
                            register_proxy=proxy,
                            allow_used=True,
                        )
                    else:
                        mark_mailbox_code_boundary(mailbox, grace_seconds=0)
                    if registrar is not None:
                        registrar.close()
                    registrar = PlatformRegistrar(proxy)
                    session_tokens = registrar.login(email, mailbox, index)
                if str(session_tokens.get("access_token") or "").strip():
                    tokens = session_tokens
                    protocol_tokens.update(session_tokens)
                    # This is the recovery path for accounts that have no
                    # stored AT. Persist the newly reconstructed session before
                    # enrolling MFA so a later activation error remains
                    # recoverable with the fresh credentials.
                    if has_session_credentials(session_tokens):
                        save_protocol_tokens(session_tokens)
                    # The account flag can be stale. Probe again now that the
                    # OTP/password recovery produced an authenticated AT.
                    probe_remote_mfa(session_tokens)
            # A stale/local-missing secret can still be repaired when the
            # stored access token is active: the settings API allows the old
            # in-house factor to be disabled and a new one enrolled without
            # exposing the old secret.  Keep add-only behavior for accounts
            # whose remote MFA state is disabled.
            if remote_mfa_enabled:
                mfa = registrar._enroll_totp(
                    tokens,
                    index,
                    replace_existing=True,
                )
            else:
                mfa = registrar._enroll_totp(tokens, index)
            save_partial(mfa)

        # Replacing an already-enabled remote factor can finish with only an
        # AT in the local record. Fill the remaining session identity fields
        # before returning so Account ID/session are never silently lost.
        if not needs_totp:
            refresh_incomplete_session()

        # The browser AT is only a bootstrap credential. Read the final AT from
        # the authenticated protocol session after password/TOTP setup. This
        # does not perform another password + TOTP login.
        final_password = str(fields.get("password") or existing_password).strip()
        final_totp = str(fields.get("totp_secret") or existing_totp).strip()
        if is_browser_registration:
            if not final_password or not final_totp:
                raise RuntimeError("browser_security_session_missing_credentials")
            ensure_running()
            step(index, "密码和 TOTP 已完成，从当前协议会话读取最终 AT")
            refreshed = registrar._read_chatgpt_session(email, final_password, index)
            if not isinstance(refreshed, dict):
                raise RuntimeError("browser_security_session_empty_result")
            required_tokens = (
                "access_token",
                "session_token",
                "account_id",
            )
            missing_tokens = [
                key for key in required_tokens
                if not str(refreshed.get(key) or "").strip()
            ]
            if missing_tokens:
                raise RuntimeError(
                    "browser_security_session_incomplete: "
                    + ", ".join(missing_tokens)
                )
            token_fields: dict[str, Any] = {
                "access_token": str(refreshed.get("access_token") or "").strip(),
                "session_token": str(refreshed.get("session_token") or "").strip(),
            }
            # Workspace sessions flip account_id between member id and shared
            # workspace id; keep the stored identity on mismatch.
            _new_aid = str(refreshed.get("account_id") or "").strip()
            _old_aid = str(account.get("account_id") or fields.get("account_id") or "").strip()
            if _old_aid and _new_aid and _new_aid.lower() != _old_aid.lower():
                token_fields["workspace_account_id"] = _new_aid
            else:
                token_fields["account_id"] = _new_aid
            for key in ("plan_type", "expires_at", "oauth_client_id", "user_id"):
                value = str(refreshed.get(key) or "").strip()
                if value:
                    token_fields[key] = value
            for key in ("session", "cookies", "profile"):
                value = refreshed.get(key)
                if value:
                    token_fields[key] = value
            # The session endpoint does not return OAuth refresh/id tokens;
            # retain the browser-minted renewable values when present.
            for key in ("refresh_token", "id_token"):
                value = str(refreshed.get(key) or account.get(key) or "").strip()
                if value:
                    token_fields[key] = value
            save_partial(token_fields)
            fields.setdefault("password", final_password)
            fields["password_set"] = True
            fields.setdefault("totp_secret", final_totp)
            fields["mfa_enabled"] = True
        elif force_password_confirmation:
            required_tokens = ("access_token", "session_token", "account_id")
            missing_tokens = [
                key for key in required_tokens
                if not str(protocol_tokens.get(key) or "").strip()
            ]
            if missing_tokens:
                raise RuntimeError(
                    "credential_confirmation_session_incomplete: "
                    + ", ".join(missing_tokens)
                )
            token_fields = {
                key: protocol_tokens[key]
                for key in (
                    "access_token",
                    "session_token",
                    "account_id",
                    "plan_type",
                    "expires_at",
                    "session",
                    "cookies",
                    "profile",
                    "oauth_client_id",
                    "mfa_factor_id",
                    "mfa_factor_type",
                )
                if protocol_tokens.get(key) not in (None, "", {})
                and protocol_tokens.get(key) != fields.get(key, account.get(key))
            }
            # Same workspace-flip guard for the credential-confirmation path.
            _new_aid = str(token_fields.get("account_id") or "").strip()
            _old_aid = str(account.get("account_id") or fields.get("account_id") or "").strip()
            if _old_aid and _new_aid and _new_aid.lower() != _old_aid.lower():
                token_fields.pop("account_id", None)
                token_fields["workspace_account_id"] = _new_aid
            token_fields.update(
                {
                    "totp_secret": final_totp,
                    "mfa_enabled": True,
                    "totp_verified_at": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                }
            )
            save_partial(token_fields)
        if mailbox is not None:
            mark_mailbox_result(mailbox, success=True)
        if callable(task_event):
            task_event(index, "success", email=email, step="安全字段补全完成")
        return dict(fields)
    except JobCancelled:
        if mailbox is not None:
            release_mailbox(mailbox)
        if callable(task_event):
            task_event(
                index,
                "cancelled",
                email=email,
                step="安全字段补全已停止",
            )
        raise
    except Exception as error:
        if mailbox is not None:
            mark_mailbox_result(mailbox, success=False, error=error)
        if callable(task_event):
            task_event(
                index,
                "failed",
                email=email,
                error=_redact_operation_error(
                    error,
                    existing_password,
                    existing_totp,
                ),
                step="安全字段补全失败",
            )
        raise
    finally:
        if registrar is not None:
            registrar.close()
        runtime_context.__exit__(None, None, None)
        _PROXY_CONFIG.reset(proxy_config_token)
        _MAIL_CONFIG.reset(mail_config_token)
        _PROVIDER_TYPE.reset(provider_token)
        _FIXED_EMAIL.reset(email_token)
        _STOP_CHECK.reset(stop_token)
        _TASK_EVENT.reset(event_token)




def _validate_protocol_account_result(
    result: dict[str, Any],
    *,
    require_security_fields: bool = True,
) -> None:
    required = {
        "access_token": "Access Token",
        "session_token": "ChatGPT Session",
        "account_id": "Account ID",
    }
    missing = [label for key, label in required.items() if not str(result.get(key) or "").strip()]
    if missing:
        raise RuntimeError(
            "protocol_registration_incomplete: 缺少 " + ", ".join(missing)
        )
    access_token = str(result.get("access_token") or "").strip()
    payload = parse_jwt_payload(access_token)
    if not isinstance(payload, dict):
        raise RuntimeError("protocol_registration_incomplete: Access Token 不是有效 JWT")
    client_id = str(payload.get("client_id") or "").strip()
    if client_id != chatgpt_oauth_client_id:
        raise RuntimeError(
            "protocol_registration_wrong_oauth_client: "
            f"expected={chatgpt_oauth_client_id}, actual={client_id or 'missing'}"
        )
    auth_claim = payload.get("https://api.openai.com/auth")
    claim_account_id = (
        str(auth_claim.get("chatgpt_account_id") or "").strip()
        if isinstance(auth_claim, dict)
        else ""
    )
    if not claim_account_id:
        raise RuntimeError(
            "protocol_registration_incomplete: Access Token 缺少 chatgpt_account_id"
        )
    if claim_account_id != str(result.get("account_id") or "").strip():
        raise RuntimeError("protocol_registration_incomplete: Account ID 与 Token 不一致")
    if require_security_fields:
        security_missing = []
        if not str(result.get("password") or "").strip():
            security_missing.append("账号密码")
        if result.get("password_set") is not True:
            security_missing.append("密码设置状态")
        if not str(result.get("totp_secret") or "").strip():
            security_missing.append("TOTP 密钥")
        if result.get("mfa_enabled") is not True:
            security_missing.append("2FA 启用状态")
        if security_missing:
            raise RuntimeError(
                "protocol_registration_incomplete: 缺少 "
                + ", ".join(security_missing)
            )




def _validate_protocol_login_result(result: dict[str, Any]) -> None:
    """Validate a login result before it is persisted."""
    try:
        _validate_protocol_account_result(result, require_security_fields=False)
    except RuntimeError as error:
        message = str(error)
        if message.startswith("protocol_registration_"):
            message = "protocol_login_" + message[len("protocol_registration_") :]
        raise RuntimeError(message) from error




def login_with_password_totp_one(
    index: int,
    *,
    email: str,
    password: str,
    totp_secret: str,
    proxy: str = "",
    workspace_name: str = "",
    require_team_workspace: bool = False,
    stop_check: Callable[[], bool] | None = None,
    task_event=None,
    error_sink: Callable[[str], None] | None = None,
    account_log_sink: Callable[[dict[str, Any]], None] | None = None,
    proxy_config: dict[str, Any] | None = None,
    protocol_runtime: ProtocolRuntimeSettings | None = None,
) -> dict[str, Any] | None:
    """Re-login, retrying only an empty pre-selection Team auth session."""
    start = time.time()
    stop = stop_check if callable(stop_check) else (lambda: False)
    task_event = _operation_event_forwarder(
        task_event,
        account_log_sink,
        operation="login",
        index=index,
        email=str(email or "").strip(),
        secrets=(password, totp_secret),
    )
    event_token = _TASK_EVENT.set(task_event if callable(task_event) else None)
    stop_token = _STOP_CHECK.set(stop)
    email_token = _FIXED_EMAIL.set(email)
    proxy_config_token = _PROXY_CONFIG.set(dict(proxy_config or {}))
    runtime = protocol_runtime or ProtocolRuntimeSettings()
    if runtime.deadline is None:
        runtime = runtime.with_deadline(time.monotonic() + runtime.account_timeout)
    runtime_context = activate_runtime(runtime)
    runtime_context.__enter__()
    registrar = None

    def ensure_running() -> None:
        if stop():
            raise JobCancelled("stop requested")
        if current_runtime_settings().expired():
            raise RuntimeError("protocol_account_timeout")

    try:
        if not str(password or "").strip() or not str(totp_secret or "").strip():
            raise RuntimeError("协议凭据登录要求同时具备密码和 TOTP 2FA")
        if callable(task_event):
            task_event(
                index,
                "running",
                proxy=proxy_brief(proxy),
                email=str(email or "").strip(),
                step="密码 + TOTP 协议登录启动",
            )
        attempt_limit = 3 if require_team_workspace else 1
        for attempt in range(1, attempt_limit + 1):
            ensure_running()
            registrar = PlatformRegistrar(proxy)
            try:
                tokens = registrar.login_with_password_totp(
                    str(email or "").strip(),
                    password,
                    totp_secret,
                    index,
                    workspace_name=workspace_name,
                    require_team_workspace=require_team_workspace,
                )
                break
            except RuntimeError as error:
                # This code is emitted before workspace/select or a callback
                # is consumed. All other authentication/identity failures are
                # final, including a real list that contains no matching Team.
                if (
                    not require_team_workspace
                    or attempt >= attempt_limit
                    or str(error).partition(":")[0] != "workspace_page_without_workspaces"
                    or getattr(registrar, "_selected_workspace_id", "")
                ):
                    raise
                failed_registrar, registrar = registrar, None
                failed_registrar.close()
                ensure_running()
                step(
                    index,
                    "Team 工作区列表暂未返回，关闭当前认证会话；"
                    f"准备第 {attempt + 1}/{attempt_limit} 次全新会话登录"
                    "（workspace_page_without_workspaces）",
                    "yellow",
                )
                retry_at = time.monotonic() + attempt
                while True:
                    ensure_running()
                    remaining = retry_at - time.monotonic()
                    if remaining <= 0:
                        break
                    deadline = current_runtime_settings().deadline
                    if deadline is not None:
                        remaining = min(remaining, deadline - time.monotonic())
                    if remaining > 0:
                        time.sleep(min(0.1, remaining))
        # Return the exact identity used for this session so reauthentication
        # can replace stale browser metadata from an older Chrome account.
        fp = _browser_fingerprint(registrar.fingerprint)
        tokens = dict(tokens or {})
        tokens.update(
            {
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
                "protocol_screen": str(fp.get("screen") or ""),
                "protocol_hardware_concurrency": str(fp.get("hardware_concurrency") or ""),
                "protocol_device_memory": str(fp.get("device_memory") or ""),
                "protocol_platform": str(fp.get("platform") or "Win32"),
                "protocol_webgl_vendor": str(fp.get("webgl_vendor") or ""),
                "protocol_webgl_renderer": str(fp.get("webgl_renderer") or ""),
                "device_id": registrar.device_id,
                "protocol_profile_id": str(fp.get("profile_id") or ""),
            }
        )
        cost = time.time() - start
        log(f"{email} 密码 + TOTP 协议登录成功，本次耗时{cost:.1f}s", "green")
        if callable(task_event):
            task_event(
                index,
                "success",
                email=email,
                account_id=tokens.get("account_id"),
                step="密码 + TOTP 协议登录完成",
            )
        return tokens
    except JobCancelled:
        if callable(task_event):
            task_event(
                index,
                "cancelled",
                email=str(email or "").strip(),
                step="密码 + TOTP 协议登录已停止",
            )
        raise
    except Exception as error:
        safe_error = _redact_operation_error(error, password, totp_secret)
        if callable(error_sink):
            try:
                error_sink(safe_error)
            except Exception:
                pass
        cost = time.time() - start
        transport_error = is_proxy_transport_error(error)
        if transport_error:
            reason = _redact_operation_error(error, password, totp_secret, limit=240)
            log(
                f"任务{index} 当前代理传输失败，本次耗时{cost:.1f}s，"
                f"代理={proxy_brief(proxy)}，原因: {reason}；调度器将切换代理重试",
                "yellow",
            )
        else:
            log(f"任务{index} 密码 + TOTP 协议登录失败，本次耗时{cost:.1f}s，原因: {safe_error}", "red")
        if callable(task_event):
            task_event(
                index,
                "running" if transport_error and not stop() else "failed",
                **({"step": "当前代理连接失败，等待切换下一条代理"}
                   if transport_error and not stop()
                   else {"error": safe_error[:300], "step": safe_error[:160]}),
            )
        return None
    finally:
        if registrar is not None:
            registrar.close()
        runtime_context.__exit__(None, None, None)
        _PROXY_CONFIG.reset(proxy_config_token)
        _FIXED_EMAIL.reset(email_token)
        _STOP_CHECK.reset(stop_token)
        _TASK_EVENT.reset(event_token)




def _decorate_protocol_tokens(
    registrar: PlatformRegistrar,
    tokens: dict[str, Any] | None,
) -> dict[str, Any]:
    """Attach the selected protocol identity to maintenance results."""
    fp = _browser_fingerprint(registrar.fingerprint)
    result = dict(tokens or {})
    result.update(
        {
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
            "protocol_screen": str(fp.get("screen") or ""),
            "protocol_hardware_concurrency": str(fp.get("hardware_concurrency") or ""),
            "protocol_device_memory": str(fp.get("device_memory") or ""),
            "protocol_platform": str(fp.get("platform") or "Win32"),
            "protocol_webgl_vendor": str(fp.get("webgl_vendor") or ""),
            "protocol_webgl_renderer": str(fp.get("webgl_renderer") or ""),
            "device_id": registrar.device_id,
            "protocol_profile_id": str(fp.get("profile_id") or ""),
        }
    )
    return result




def change_password_one(
    index: int,
    *,
    email: str,
    old_password: str,
    new_password: str,
    totp_secret: str,
    proxy: str = "",
    stop_check: Callable[[], bool] | None = None,
    task_event=None,
    error_sink: Callable[[str], None] | None = None,
    account_log_sink: Callable[[dict[str, Any]], None] | None = None,
    proxy_config: dict[str, Any] | None = None,
    protocol_runtime: ProtocolRuntimeSettings | None = None,
) -> dict[str, Any] | None:
    """Run one known-credential password change."""
    start = time.time()
    stop = stop_check if callable(stop_check) else (lambda: False)
    task_event = _operation_event_forwarder(
        task_event,
        account_log_sink,
        operation="password_change",
        index=index,
        email=str(email or "").strip(),
        secrets=(old_password, new_password, totp_secret),
    )
    event_token = _TASK_EVENT.set(task_event if callable(task_event) else None)
    stop_token = _STOP_CHECK.set(stop)
    email_token = _FIXED_EMAIL.set(str(email or "").strip())
    proxy_config_token = _PROXY_CONFIG.set(dict(proxy_config or {}))
    runtime_context = activate_runtime(protocol_runtime or ProtocolRuntimeSettings())
    runtime_context.__enter__()
    registrar: PlatformRegistrar | None = None
    try:
        if stop():
            raise JobCancelled("stop requested")
        registrar = PlatformRegistrar(proxy)
        if callable(task_event):
            task_event(
                index,
                "running",
                proxy=proxy_brief(proxy),
                email=str(email or "").strip(),
                step="修改密码任务启动",
            )
        result = registrar.change_password(
            str(email or "").strip(),
            old_password,
            new_password,
            totp_secret,
            index,
        )
        result = _decorate_protocol_tokens(registrar, result)
        if callable(task_event):
            task_event(
                index,
                "success",
                email=email,
                account_id=(result or {}).get("account_id") if isinstance(result, dict) else "",
                step="修改密码完成",
            )
        log(f"{email} 修改密码成功，本次耗时{time.time() - start:.1f}s", "green")
        return result
    except JobCancelled:
        if callable(task_event):
            task_event(
                index,
                "cancelled",
                email=str(email or "").strip(),
                step="修改密码已停止",
            )
        raise
    except Exception as error:
        safe_error = _redact_operation_error(
            error,
            old_password,
            new_password,
            totp_secret,
        )
        if callable(error_sink):
            try:
                error_sink(safe_error)
            except Exception:
                pass
        transport_error = is_proxy_transport_error(error)
        log(
            f"任务{index} 修改密码失败，本次耗时{time.time() - start:.1f}s，原因: {safe_error}",
            "yellow" if transport_error else "red",
        )
        if callable(task_event):
            task_event(
                index,
                "running" if transport_error and not stop() else "failed",
                **(
                    {"step": "当前代理连接失败，等待切换下一条代理"}
                    if transport_error and not stop()
                    else {"error": safe_error[:300], "step": safe_error[:160]}
                ),
            )
        return None
    finally:
        if registrar is not None:
            registrar.close()
        runtime_context.__exit__(None, None, None)
        _PROXY_CONFIG.reset(proxy_config_token)
        _FIXED_EMAIL.reset(email_token)
        _STOP_CHECK.reset(stop_token)
        _TASK_EVENT.reset(event_token)




def change_totp_one(
    index: int,
    *,
    email: str,
    password: str,
    totp_secret: str,
    proxy: str = "",
    stop_check: Callable[[], bool] | None = None,
    task_event=None,
    error_sink: Callable[[str], None] | None = None,
    account_log_sink: Callable[[dict[str, Any]], None] | None = None,
    proxy_config: dict[str, Any] | None = None,
    protocol_runtime: ProtocolRuntimeSettings | None = None,
) -> dict[str, Any] | None:
    """Run one TOTP replacement using the current password and TOTP."""
    start = time.time()
    stop = stop_check if callable(stop_check) else (lambda: False)
    task_event = _operation_event_forwarder(
        task_event,
        account_log_sink,
        operation="totp_change",
        index=index,
        email=str(email or "").strip(),
        secrets=(password, totp_secret),
    )
    event_token = _TASK_EVENT.set(task_event if callable(task_event) else None)
    stop_token = _STOP_CHECK.set(stop)
    email_token = _FIXED_EMAIL.set(str(email or "").strip())
    proxy_config_token = _PROXY_CONFIG.set(dict(proxy_config or {}))
    runtime_context = activate_runtime(protocol_runtime or ProtocolRuntimeSettings())
    runtime_context.__enter__()
    registrar: PlatformRegistrar | None = None
    try:
        if stop():
            raise JobCancelled("stop requested")
        registrar = PlatformRegistrar(proxy)
        if callable(task_event):
            task_event(
                index,
                "running",
                proxy=proxy_brief(proxy),
                email=str(email or "").strip(),
                step="修改 2FA 任务启动",
            )
        result = registrar.change_totp(
            str(email or "").strip(),
            password,
            totp_secret,
            index,
        )
        result = _decorate_protocol_tokens(registrar, result)
        if callable(task_event):
            task_event(
                index,
                "success",
                email=email,
                account_id=(result or {}).get("account_id") if isinstance(result, dict) else "",
                step="修改 2FA 完成",
            )
        log(f"{email} 修改 2FA 成功，本次耗时{time.time() - start:.1f}s", "green")
        return result
    except JobCancelled:
        if callable(task_event):
            task_event(
                index,
                "cancelled",
                email=str(email or "").strip(),
                step="修改 2FA 已停止",
            )
        raise
    except Exception as error:
        safe_error = _redact_operation_error(error, password, totp_secret)
        if callable(error_sink):
            try:
                error_sink(safe_error)
            except Exception:
                pass
        transport_error = is_proxy_transport_error(error)
        log(
            f"任务{index} 修改 2FA 失败，本次耗时{time.time() - start:.1f}s，原因: {safe_error}",
            "yellow" if transport_error else "red",
        )
        if callable(task_event):
            task_event(
                index,
                "running" if transport_error and not stop() else "failed",
                **(
                    {"step": "当前代理连接失败，等待切换下一条代理"}
                    if transport_error and not stop()
                    else {"error": safe_error[:300], "step": safe_error[:160]}
                ),
            )
        return None
    finally:
        if registrar is not None:
            registrar.close()
        runtime_context.__exit__(None, None, None)
        _PROXY_CONFIG.reset(proxy_config_token)
        _FIXED_EMAIL.reset(email_token)
        _STOP_CHECK.reset(stop_token)
        _TASK_EVENT.reset(event_token)




def login_one(
    index: int,
    *,
    mail_config: dict[str, Any],
    proxy: str = "",
    fixed_email: str | None = None,
    provider_type: str | None = None,
    stop_check: Callable[[], bool] | None = None,
    task_event=None,
    error_sink: Callable[[str], None] | None = None,
    proxy_config: dict[str, Any] | None = None,
    protocol_runtime: ProtocolRuntimeSettings | None = None,
) -> dict[str, Any] | None:
    """Run one standalone NextAuth protocol login and persist the session."""
    start = time.time()
    stop = stop_check if callable(stop_check) else (lambda: False)
    event_token = _TASK_EVENT.set(task_event if callable(task_event) else None)
    stop_token = _STOP_CHECK.set(stop)
    email_token = _FIXED_EMAIL.set(fixed_email)
    provider_token = _PROVIDER_TYPE.set(provider_type)
    mail_config_token = _MAIL_CONFIG.set(dict(mail_config))
    proxy_config_token = _PROXY_CONFIG.set(dict(proxy_config or {}))
    runtime_context = activate_runtime(
        protocol_runtime or ProtocolRuntimeSettings()
    )
    runtime_context.__enter__()
    registrar = None
    mailbox: dict[str, Any] | None = None
    try:
        mailbox = create_mailbox(
            _FIXED_EMAIL.get(),
            register_proxy=proxy,
            allow_used=True,
        )
        email = str(mailbox.get("address") or "").strip()
        if not email:
            raise RuntimeError("协议登录邮箱池未返回 address")
        registrar = PlatformRegistrar(proxy)
        if callable(task_event):
            task_event(
                index,
                "running",
                proxy=proxy_brief(proxy),
                email=email,
                step="协议登录任务启动",
            )
        tokens = registrar.login(email, mailbox, index)
        result = registrar.build_login_record(email, mailbox, tokens)
        _validate_protocol_login_result(result)
        mark_mailbox_result(mailbox, success=True)
        _append_account(result)
        cost = time.time() - start
        log(f"{email} 协议登录成功，本次耗时{cost:.1f}s", "green")
        if callable(task_event):
            task_event(index, "success", email=email, step="协议登录完成")
        return result
    except JobCancelled:
        if mailbox is not None:
            mark_mailbox_result(mailbox, success=False, error="stop requested")
        raise
    except Exception as error:
        if mailbox is not None:
            mark_mailbox_result(mailbox, success=False, error=error)
        if callable(error_sink):
            try:
                error_sink(str(error))
            except Exception:
                pass
        transport_error = is_proxy_transport_error(error)
        cost = time.time() - start
        if transport_error:
            log(
                f"任务{index} 当前代理传输失败，本次耗时{cost:.1f}s，调度器将切换代理重试",
                "yellow",
            )
        else:
            log(f"任务{index} 协议登录失败，本次耗时{cost:.1f}s，原因: {error}", "red")
        if callable(task_event):
            task_event(
                index,
                "running" if transport_error and not stop() else "failed",
                **({"step": "当前代理连接失败，等待切换下一条代理"}
                   if transport_error and not stop()
                   else {"error": str(error)[:300], "step": str(error)[:160]}),
            )
        return None
    finally:
        if registrar is not None:
            registrar.close()
        runtime_context.__exit__(None, None, None)
        _PROXY_CONFIG.reset(proxy_config_token)
        _MAIL_CONFIG.reset(mail_config_token)
        _PROVIDER_TYPE.reset(provider_token)
        _FIXED_EMAIL.reset(email_token)
        _STOP_CHECK.reset(stop_token)
        _TASK_EVENT.reset(event_token)

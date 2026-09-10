"""Security-domain sync helpers: reauth/repair/password/TOTP operations."""

from __future__ import annotations

import asyncio
import copy
import inspect
import secrets
import string
import time
from typing import Any

from common.team_workspace import requires_team_workspace, validate_session_replacement


def _state() -> Any:
    # Delayed import is intentional: web_app imports this module.
    import web_app

    return web_app


def _persist_reauthenticated_account(
    account: dict[str, Any], tokens: dict[str, Any]
) -> dict[str, Any]:
    from common.account_health import check_account
    from common.account_store import upsert_account_record

    access_token = str(tokens.get("access_token") or "").strip()
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    if not access_token:
        raise RuntimeError("重新登录未返回 AT")
    require_team = requires_team_workspace(account)
    if require_team and (
        tokens.get("workspace_selection_verified") is not True
        or not str(tokens.get("selected_workspace_id") or "").strip()
    ):
        raise RuntimeError("team_workspace_unverified: 重新登录未完成团队工作区选择，保留原凭据")
    identity = validate_session_replacement(account, tokens, require_identity=require_team)
    refreshed = {
        **account,
        "access_token": access_token,
        "refresh_token": refresh_token or str(account.get("refresh_token") or ""),
        "id_token": str(tokens.get("id_token") or account.get("id_token") or "").strip(),
        "session_token": str(tokens.get("session_token") or account.get("session_token") or "").strip(),
        "session": tokens.get("session") or account.get("session"),
        "last_reauthenticated_at": _state()._now_iso(),
        "last_token_refresh_at": _state()._now_iso(),
        "last_token_refresh_error": "",
        "last_reauth_error": "",
        "last_reauth_failed_at": "",
        "reauth_blocked_reason": "",
        "health_status": "unknown",
        "health_error": "",
    }
    for key in (
        "plan_type",
        "type",
        "browser_type",
        "tls_impersonate",
        "user_agent",
        "sec_ch_ua",
        "sec_ch_ua_full_version_list",
        "platform_version",
        "accept_language",
        "locale",
        "timezone",
        "timezone_offset_min",
        "protocol_screen",
        "protocol_hardware_concurrency",
        "protocol_device_memory",
        "protocol_platform",
        "protocol_webgl_vendor",
        "protocol_webgl_renderer",
        "device_id",
        "protocol_profile_id",
        "selected_workspace_id",
        "selected_workspace_name",
        "workspace_selection_verified",
    ):
        if key in tokens:
            refreshed[key] = tokens[key]
    if identity["user_id"]:
        refreshed["user_id"] = identity["user_id"]
    if identity["plan_type"]:
        refreshed["plan_type"] = identity["plan_type"]
    checked = check_account(refreshed, persist=False, path=_state().ACCOUNTS_FILE)
    if _state().normalized_health_status(checked) not in {"alive", "limited"}:
        detail = str(checked.get("health_error") or "重新登录后的凭证测活失败")
        raise RuntimeError(detail[:400])
    validate_session_replacement(account, checked, require_identity=require_team)
    return upsert_account_record(checked, _state().ACCOUNTS_FILE, create=False)


def _persist_reauthentication_failure(
    account: dict[str, Any],
    error: BaseException | str,
    *,
    mode: str = "",
    step: str = "",
) -> dict[str, Any]:
    detail = str(error or "重新登录失败").strip()[:400]
    lowered = detail.lower()
    previous_health = _state().normalized_health_status(account)
    now = _state()._now_iso()
    deactivated = (
        "account_deactivated" in lowered
        or str(account.get("reauth_blocked_reason") or "").strip().lower()
        == "account_deactivated"
    )
    transport_error = _state().is_proxy_transport_error(detail)

    if deactivated:
        health_status = "invalid"
        status = "已封号"
        health_error = "account_deactivated: account has been deleted or deactivated"
        blocked_reason = "account_deactivated"
    elif transport_error:
        health_status = "error"
        status = "异常"
        health_error = detail
        blocked_reason = ""
    else:
        health_status = "invalid" if previous_health == "invalid" else "reauth_required"
        status = "异常"
        health_error = detail
        blocked_reason = ""

    failed = {
        **account,
        "status": status,
        "health_status": health_status,
        "health_checked_at": now,
        "health_error": health_error,
        "last_reauth_error": detail,
        "last_reauth_failed_at": now,
        "last_reauth_mode": str(mode or "").strip().lower(),
        "last_reauth_step": str(step or "").strip()[:160],
        "reauth_blocked_reason": blocked_reason,
        "updated_at": now,
    }
    if deactivated and previous_health != "invalid":
        failed["last_invalid_at"] = now
    return _state().upsert_account_record(failed, _state().ACCOUNTS_FILE, create=False)


def _reauth_proxy(account: dict[str, Any], cfg: dict[str, Any]) -> str:
    proxy_cfg = cfg.get("proxy") if isinstance(cfg.get("proxy"), dict) else {}
    proxy = str(account.get("proxy") or "").strip()
    if not proxy and proxy_cfg.get("enabled"):
        proxy = str(proxy_cfg.get("url") or "").strip()
    return proxy


def _normalize_reauth_mode(value: Any) -> str:
    mode = str(value or "protocol").strip().lower()
    if mode not in _state()._ACCOUNT_REAUTH_MODES:
        raise ValueError("重新登录方式只支持 browser 或 protocol")
    return mode


def _account_requires_protocol_reauth(account: dict[str, Any]) -> bool:
    """Invalidated and team sessions use the credential flow with workspace selection."""
    if requires_team_workspace(account):
        return True
    state = _state().normalized_health_status(account)
    if state in {"invalid", "reauth_required"}:
        return True
    error = str(account.get("health_error") or "").strip().lower()
    return "token invalidated" in error or "session has expired" in error


def _protocol_reauth_credentials_ready(account: dict[str, Any]) -> bool:
    return bool(
        str(account.get("password") or "").strip()
        and str(account.get("totp_secret") or "").strip()
    )


def _reauthenticate_account_sync(
    account: dict[str, Any],
    mode: str = "protocol",
    stop_check=None,
) -> dict[str, Any]:
    from mail import update_mailbox_states
    from register.flow import register_one as browser_register_one
    from register.log import _LOG_SINK
    from register.protocol import (
        login_one as protocol_login_one,
        login_with_password_totp_one as protocol_credential_login_one,
    )
    from register.sessions import JobCancelled
    from proxy_util import (
        is_proxy_transport_error,
        normalize_proxy_url,
        proxy_brief,
        resolve_proxy_pool,
    )

    requested_mode = _normalize_reauth_mode(mode)
    reauth_mode = (
        "protocol"
        if _account_requires_protocol_reauth(account)
        else requested_mode
    )
    stop = stop_check if callable(stop_check) else (lambda: False)
    current_step = ""
    credential_password = ""
    credential_totp = ""

    log_token = None
    try:
        email = str(account.get("email") or "").strip()
        if not email:
            raise RuntimeError("账号缺少邮箱")
        log_token = _LOG_SINK.set(_state().JOB.append_log)
        _state().JOB.append_log(f"[重新登录] 开始 · {email}")
        _state().record_operation_event(
            account,
            "reauth",
            "running",
            message="重新登录开始",
            metadata={"mode": reauth_mode},
        )
        cfg = _state()._read_config()
        mail_cfg = copy.deepcopy(cfg.get("mail") or {})
        registration = cfg.get("registration") if isinstance(cfg.get("registration"), dict) else {}
        proxy = _reauth_proxy(account, cfg)
        proxy_cfg = cfg.get("proxy") if isinstance(cfg.get("proxy"), dict) else {}
        configured_proxies = resolve_proxy_pool(proxy_cfg)
        proxy_candidates: list[str] = []
        normalized_account_proxy = normalize_proxy_url(proxy)
        if normalized_account_proxy:
            proxy_candidates.append(normalized_account_proxy)
        for candidate in configured_proxies:
            normalized = normalize_proxy_url(candidate)
            if normalized and normalized not in proxy_candidates:
                proxy_candidates.append(normalized)
        if not proxy_candidates:
            proxy_candidates = [""]
        provider_type = _state()._mailbox_provider_map(cfg).get(
            email.lower(), str(account.get("provider_type") or "auto")
        )
        credential_password = str(account.get("password") or "").strip()
        credential_totp = str(account.get("totp_secret") or "").strip()
        account_event_sink = _account_operation_event_sink(
            account,
            "reauth",
            secrets=(credential_password, credential_totp),
        )
        if reauth_mode == "protocol" and (not credential_password or not credential_totp):
            raise RuntimeError("协议模式重新登录要求账号同时具备已保存密码和 TOTP 2FA")
        _state().JOB.append_log(
            f"[重新登录] 准备 · {email} · 方式={reauth_mode} · "
            f"密码={'已保存' if credential_password else '缺失'} · "
            f"TOTP={'已保存' if credential_totp else '缺失'} · "
            f"代理={proxy_brief(proxy) if proxy else 'direct'}"
        )
        timeout = max(90, int(registration.get("timeout") or 180))
        if reauth_mode != "protocol":
            update_mailbox_states([email], action="release")
        reauth_error = ""

        def capture_reauth_result(_index, _status=None, **fields):
            nonlocal current_step, reauth_error
            detail_step = str(fields.get("step") or "").strip()
            if detail_step:
                current_step = detail_step
                _state().JOB.append_log(f"[重新登录] 步骤 · {email} · {detail_step}")
            detail = str(fields.get("error") or "").strip()
            if reauth_mode != "protocol":
                # Keep the browser callback on the same bounded sink as the
                # protocol path.  The outer operation owns the terminal row;
                # this sink records at most the first running marker.
                account_event_sink(
                    {
                        "status": str(_status or "running"),
                        "step": detail_step,
                        "error": detail,
                    }
                )
            if detail:
                if detail != reauth_error:
                    if is_proxy_transport_error(detail):
                        _state().JOB.append_log(
                            f"[重新登录] 代理连接失败 · {email} · "
                            "当前代理不可用，准备切换下一条代理"
                        )
                    else:
                        _state().JOB.append_log(
                            f"[重新登录] 步骤失败 · {email} · "
                            f"当前步骤={current_step or '未知'} · {detail[:400]}"
                        )
                reauth_error = detail

        retryable_markers = (
            "no_email_input",
            "goto_failed",
            "stuck_on_verification",
            "stuck_on_login",
            "passwordless_switch_failed",
            "otp_not_delivered",
            "no_code",
            "cf_blocked",
            "代理出口 ip 检测失败",
            "host unreachable",
            "connection reset",
            "connection refused",
            "max retries exceeded",
            "timeout",
            "targetclosed",
            "execution context was destroyed",
            "ns_binding_aborted",
        )
        reauth_retries = max(
            0,
            min(1, int(registration.get("reauth_retries", 1) or 0)),
        )
        proxy_retry_limit = max(
            reauth_retries,
            min(5, max(0, len(set(proxy_candidates)) - 1)),
        )
        tokens = None
        failed_proxies: set[str] = set()
        for attempt in range(proxy_retry_limit + 1):
            if stop():
                raise JobCancelled("stop requested")
            if failed_proxies and len(failed_proxies) >= len(set(proxy_candidates)):
                break
            reauth_error = ""
            current_step = ""
            if failed_proxies:
                available = [item for item in proxy_candidates if item not in failed_proxies]
                proxy = available[0] if available else ""
            else:
                proxy = proxy_candidates[0]
            _state().JOB.append_log(
                f"[重新登录] 尝试 {attempt + 1}/{proxy_retry_limit + 1} · "
                f"{email} · {'协议' if reauth_mode == 'protocol' else '浏览器'}请求开始 · "
                f"代理={proxy_brief(proxy) if proxy else 'direct'}"
            )
            if reauth_mode == "protocol":
                tokens = protocol_credential_login_one(
                    1,
                    email=email,
                    password=credential_password,
                    totp_secret=credential_totp,
                    proxy=proxy,
                    stop_check=stop,
                    task_event=capture_reauth_result,
                    account_log_sink=account_event_sink,
                    error_sink=lambda detail: capture_reauth_result(
                        1, "failed", error=detail
                    ),
                    proxy_config=proxy_cfg,
                    **({
                        "workspace_name": str(account.get("workspace_name") or account.get("selected_workspace_name") or "").strip(),
                        "require_team_workspace": True,
                    } if requires_team_workspace(account) else {}),
                )
            else:
                tokens = asyncio.run(
                    browser_register_one(
                        1,
                        1,
                        None,
                        mail_config=mail_cfg,
                        proxy=proxy,
                        fixed_email=email,
                        provider_type=provider_type,
                        timeout=timeout,
                        keep_on_fail=False,
                        stop_check=stop,
                        task_event=capture_reauth_result,
                        c2a_config={},
                        group_id=str(account.get("group_id") or ""),
                        hybrid_mode=True,
                        persist_result=False,
                    )
                )
            if stop():
                raise JobCancelled("stop requested")
            if tokens and str(tokens.get("access_token") or "").strip():
                break
            lowered_error = reauth_error.lower()
            if "account_deactivated" in lowered_error:
                break
            transport_error = is_proxy_transport_error(reauth_error)
            if transport_error and proxy:
                failed_proxies.add(proxy)
            if transport_error:
                if attempt >= proxy_retry_limit or len(failed_proxies) >= len(set(proxy_candidates)):
                    break
                next_proxy = next(
                    (
                        item for item in proxy_candidates
                        if item not in failed_proxies
                    ),
                    "",
                )
                _state().JOB.append_log(
                    f"[重新登录] 代理连接失败 · {email} · "
                    f"已切换代理重试 {attempt + 2}/{proxy_retry_limit + 1} · "
                    f"{proxy_brief(next_proxy) if next_proxy else 'direct'}"
                )
                if reauth_mode != "protocol":
                    update_mailbox_states([email], action="release")
                time.sleep(0.5)
                continue
            retryable = any(marker in lowered_error for marker in retryable_markers)
            if attempt >= reauth_retries or not retryable:
                break
            _state().JOB.append_log(
                f"[重新登录] 临时页面错误，3 秒后重试 "
                f"{attempt + 2}/{proxy_retry_limit + 1} · {reauth_error or 'unknown'}"
            )
            if reauth_mode != "protocol":
                update_mailbox_states([email], action="release")
            time.sleep(3)
        if not tokens or not str(tokens.get("access_token") or "").strip():
            if "account_deactivated" in reauth_error.lower():
                raise RuntimeError("account_deactivated: 已封号")
            detail = reauth_error or "未取得新的访问凭证"
            mode_label = "协议" if reauth_mode == "protocol" else "混合"
            raise RuntimeError(f"{mode_label}模式重新登录失败: {detail[:240]}")
        saved = _state()._persist_reauthenticated_account({**account, "proxy": proxy}, tokens)
        _state().JOB.append_log(f"[重新登录] 成功 · {email} · {reauth_mode}")
        _state().record_operation_event(
            saved,
            "reauth",
            "success",
            message="重新登录成功",
            metadata={"mode": reauth_mode, "proxy": proxy_brief(proxy) if proxy else "direct"},
        )
        return saved
    except JobCancelled:
        email = str(account.get("email") or "").strip() or "未知账号"
        _state().JOB.append_log(f"[重新登录] 已停止 · {email}")
        _state().record_operation_event(
            account,
            "reauth",
            "cancelled",
            message="重新登录已停止",
            metadata={"mode": reauth_mode},
        )
        raise
    except Exception as error:
        email = str(account.get("email") or "").strip() or "未知账号"
        try:
            failed = _persist_reauthentication_failure(
                account,
                error,
                mode=reauth_mode,
                step=current_step,
            )
            if failed.get("reauth_blocked_reason") == "account_deactivated":
                _state().JOB.append_log(f"[重新登录] 已封号 · {failed.get('email') or email}")
        except Exception as persist_error:
            _state().JOB.append_log(
                f"[重新登录] 失败状态保存异常 · {email} · {str(persist_error)[:240]}"
            )
        _state().JOB.append_log(f"[重新登录] 失败 · {email} · {str(error)[:300]}")
        _state().record_operation_event(
            account,
            "reauth",
            "failed",
            message="重新登录失败",
            error=error,
            step=current_step,
            metadata={"mode": reauth_mode},
            secrets=(credential_password, credential_totp),
        )
        raise
    finally:
        if log_token is not None:
            _LOG_SINK.reset(log_token)


def _repair_account_security_sync(
    account: dict[str, Any],
    stop_check=None,
    *,
    force_password_confirmation: bool = False,
) -> dict[str, Any]:
    from mail import update_mailbox_states
    from register.log import _LOG_SINK
    from register.protocol import repair_security_one
    from register.protocol_runtime import ProtocolRuntimeSettings
    from register.sessions import JobCancelled
    from proxy_util import (
        is_proxy_transport_error,
        normalize_proxy_url,
        proxy_brief,
        resolve_proxy_pool,
    )

    stop = stop_check if callable(stop_check) else (lambda: False)
    log_token = None
    try:
        email = str(account.get("email") or "").strip()
        if not email:
            raise RuntimeError("账号缺少邮箱")
        log_token = _LOG_SINK.set(_state().JOB.append_log)
        _state().JOB.append_log(f"[安全字段补全] 开始 · {email}")
        _state().record_operation_event(
            account,
            "security_repair",
            "running",
            message="安全字段补全开始",
        )
        cfg = _state()._read_config()
        mail_cfg = copy.deepcopy(cfg.get("mail") or {})
        registration = (
            cfg.get("registration")
            if isinstance(cfg.get("registration"), dict)
            else {}
        )
        proxy = _reauth_proxy(account, cfg)
        proxy_cfg = cfg.get("proxy") if isinstance(cfg.get("proxy"), dict) else {}
        proxy_candidates: list[str] = []
        for candidate in [proxy, *resolve_proxy_pool(proxy_cfg)]:
            normalized = normalize_proxy_url(candidate)
            if normalized and normalized not in proxy_candidates:
                proxy_candidates.append(normalized)
        if not proxy_candidates:
            proxy_candidates = [""]
        # One initial route plus four transport fallbacks is enough to escape a
        # bad exit without turning a business/authentication failure into a loop.
        proxy_candidates = proxy_candidates[:5]
        provider_type = _state()._mailbox_provider_map(cfg).get(
            email.lower(),
            str(account.get("provider_type") or "auto"),
        )
        mail_cfg["wait_timeout"] = max(
            10,
            int(
                registration.get("protocol_login_otp_timeout")
                or registration.get("protocol_otp_timeout")
                or mail_cfg.get("wait_timeout")
                or 90
            ),
        )
        mail_cfg["wait_chunk"] = max(
            30,
            int(
                registration.get("protocol_login_poll_chunk")
                or registration.get("protocol_otp_poll_chunk")
                or 60
            ),
        )
        mail_cfg["wait_chunk"] = min(60, mail_cfg["wait_chunk"])
        mail_cfg["request_timeout"] = max(
            15,
            int(mail_cfg.get("request_timeout") or 30),
        )
        protocol_runtime = ProtocolRuntimeSettings.from_config(registration)
        current = dict(account)

        def capture_event(_index, _status=None, **fields):
            step_text = str(fields.get("step") or "").strip()
            if step_text:
                _state().JOB.append_log(f"[安全字段补全] {step_text}")

        def persist_partial(fields: dict[str, Any]) -> None:
            current.update(fields)
            current["updated_at"] = _state()._now_iso()
            _state().upsert_account_record(current, _state().ACCOUNTS_FILE, create=False)

        update_mailbox_states(
            [email],
            action="release",
            states=["failed", "in_use"],
        )
        result: dict[str, Any] | None = None
        last_error: Exception | None = None
        successful_proxy = proxy_candidates[0]
        account_event_sink = _account_operation_event_sink(
            current,
            "security_repair",
            secrets=(
                str(current.get("password") or "").strip(),
                str(current.get("totp_secret") or "").strip(),
            ),
        )
        for attempt, candidate in enumerate(proxy_candidates, start=1):
            if stop():
                raise JobCancelled("stop requested")
            try:
                result = repair_security_one(
                    1,
                    account=current,
                    mail_config=mail_cfg,
                    proxy=candidate,
                    provider_type=provider_type,
                    stop_check=stop,
                    task_event=capture_event,
                    account_log_sink=account_event_sink,
                    proxy_config=proxy_cfg,
                    partial_sink=persist_partial,
                    protocol_runtime=protocol_runtime,
                    force_password_confirmation=force_password_confirmation,
                )
                successful_proxy = candidate
                break
            except JobCancelled:
                raise
            except Exception as error:
                last_error = error
                if not is_proxy_transport_error(error) or attempt >= len(proxy_candidates):
                    raise
                next_proxy = proxy_candidates[attempt]
                _state().JOB.append_log(
                    f"[安全字段补全] 代理传输失败 · {email} · "
                    f"切换线路 {attempt + 1}/{len(proxy_candidates)} · "
                    f"{proxy_brief(next_proxy) if next_proxy else 'direct'}"
                )
                update_mailbox_states(
                    [email],
                    action="release",
                    states=["failed", "in_use"],
                )
                time.sleep(0.5)
        if result is None:
            raise last_error or RuntimeError("安全字段补全未返回结果")
        current.update(result)
        if successful_proxy:
            current["proxy"] = successful_proxy
        current["registration_state"] = "complete"
        current["updated_at"] = _state()._now_iso()
        saved = _state().upsert_account_record(current, _state().ACCOUNTS_FILE, create=False)
        _state().JOB.append_log(f"[安全字段补全] 成功 · {email}")
        _state().record_operation_event(
            saved,
            "security_repair",
            "success",
            message="安全字段补全成功",
            metadata={"proxy": proxy_brief(successful_proxy) if successful_proxy else "direct"},
        )
        return saved
    except JobCancelled:
        email = str(account.get("email") or "").strip() or "未知账号"
        _state().JOB.append_log(f"[安全字段补全] 已停止 · {email}")
        _state().record_operation_event(
            account,
            "security_repair",
            "cancelled",
            message="安全字段补全已停止",
        )
        raise
    except Exception as error:
        email = str(account.get("email") or "").strip() or "未知账号"
        _state().JOB.append_log(f"[安全字段补全] 失败 · {email} · {str(error)[:400]}")
        _state().record_operation_event(
            account,
            "security_repair",
            "failed",
            message="安全字段补全失败",
            error=error,
        )
        raise
    finally:
        if log_token is not None:
            _LOG_SINK.reset(log_token)


_ACCOUNT_SECURITY_OPERATIONS = frozenset(
    {"reauth", "security_repair", "password_change", "totp_change"}
)
_SECURITY_PASSWORD_MIN_LENGTH = 12
_SECURITY_PASSWORD_RANDOM_LENGTH = 16
_SECURITY_PASSWORD_MODES = frozenset({"custom", "random"})


def _normalize_security_password_mode(value: Any) -> str:
    mode = str(value or "custom").strip().lower().replace("-", "_")
    mode = {
        "generated": "random",
        "auto": "random",
        "automatic": "random",
        "manual": "custom",
    }.get(mode, mode)
    if mode not in _SECURITY_PASSWORD_MODES:
        raise ValueError("密码模式只支持 random 或 custom")
    return mode


def _generate_security_password(length: int = _SECURITY_PASSWORD_RANDOM_LENGTH) -> str:
    """Generate a strong per-account password without putting it in job state."""
    size = max(_SECURITY_PASSWORD_MIN_LENGTH, int(length or _SECURITY_PASSWORD_RANDOM_LENGTH))
    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%"),
    ]
    alphabet = string.ascii_letters + string.digits + "!@#$%"
    value = required + [secrets.choice(alphabet) for _ in range(size - len(required))]
    secrets.SystemRandom().shuffle(value)
    return "".join(value)


def _normalize_account_security_operation(value: Any) -> str:
    operation = str(value or "reauth").strip().lower().replace("-", "_")
    aliases = {
        "change_password": "password_change",
        "password_reset": "password_change",
        "password": "password_change",
        "change_totp": "totp_change",
        "change_2fa": "totp_change",
        "2fa_change": "totp_change",
        "mfa_change": "totp_change",
        "totp": "totp_change",
        "mfa": "totp_change",
        "security_repair": "security_repair",
        "repair": "security_repair",
    }
    operation = aliases.get(operation, operation)
    if operation not in _ACCOUNT_SECURITY_OPERATIONS:
        raise ValueError("不支持的账号批次操作")
    return operation


def _account_security_operation_label(operation: str) -> str:
    raw = str(operation or "").strip().lower().replace("-", "_")
    operation = {
        "change_password": "password_change",
        "password_reset": "password_change",
        "password": "password_change",
        "change_totp": "totp_change",
        "change_2fa": "totp_change",
        "2fa_change": "totp_change",
        "mfa_change": "totp_change",
        "totp": "totp_change",
        "mfa": "totp_change",
        "repair": "security_repair",
    }.get(raw, raw)
    return {
        "reauth": "重新登录",
        "security_repair": "安全字段补全",
        "password_change": "修改密码",
        "totp_change": "修改 2FA",
        "email_rebind": "邮箱换绑",
    }.get(operation, "重新登录")


def _call_security_protocol_wrapper(
    function: Any,
    *,
    index: int,
    kwargs: dict[str, Any],
) -> Any:
    """Call a protocol wrapper while tolerating older/newer signatures.

    The protocol module is used by CLI integrations too, and a few deployed
    versions expose the same operation under a different keyword set. Filter
    only unsupported keywords; values such as passwords and TOTP secrets never
    enter the batch state object or its JSON snapshots.
    """
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        signature = None
    call_kwargs = dict(kwargs)
    accepts_var_kw = bool(
        signature
        and any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
    )
    if signature is not None and not accepts_var_kw:
        call_kwargs = {
            key: value
            for key, value in call_kwargs.items()
            if key in signature.parameters
        }
    # Current one-account wrappers take an index first. Class-method fallbacks
    # place ``index`` last, so pass it by keyword in that shape.
    if signature is not None and "index" in signature.parameters:
        parameter = signature.parameters["index"]
        if parameter.kind == inspect.Parameter.KEYWORD_ONLY:
            return function(index=index, **call_kwargs)
        ordered = list(signature.parameters)
        if ordered.index("index") > 0:
            call_kwargs["index"] = index
            return function(**call_kwargs)
    if signature is not None and "index" not in signature.parameters:
        return function(**call_kwargs)
    return function(index, **call_kwargs)


def _resolve_security_protocol_wrapper(
    module: Any,
    names: tuple[str, ...],
) -> Any:
    for name in names:
        candidate = getattr(module, name, None)
        if callable(candidate):
            return candidate
    joined = ", ".join(names)
    raise RuntimeError(f"协议层未提供安全操作接口: {joined}")


def _security_proxy_candidates(account: dict[str, Any], cfg: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    from proxy_util import normalize_proxy_url, resolve_proxy_pool

    proxy_cfg = cfg.get("proxy") if isinstance(cfg.get("proxy"), dict) else {}
    configured = [
        _reauth_proxy(account, cfg),
        *resolve_proxy_pool(proxy_cfg),
    ]
    candidates: list[str] = []
    for candidate in configured:
        normalized = normalize_proxy_url(candidate)
        if normalized not in candidates:
            candidates.append(normalized)
    if not candidates:
        candidates = [""]
    return candidates[:5], proxy_cfg


def _persist_security_operation_failure(
    account: dict[str, Any],
    operation: str,
    error: BaseException | str,
) -> dict[str, Any]:
    detail = str(error or "账号安全操作失败").strip()[:400]
    now = _state()._now_iso()
    failed = {
        **account,
        "last_security_operation": operation,
        "last_security_error": detail,
        "last_security_failed_at": now,
        "updated_at": now,
    }
    return _state().upsert_account_record(failed, _state().ACCOUNTS_FILE, create=False)


def _redact_security_detail(detail: Any, account: dict[str, Any], new_password: str = "") -> str:
    text = str(detail or "账号安全操作失败")
    secrets_to_hide = [
        str(account.get("password") or "").strip(),
        str(account.get("totp_secret") or "").strip(),
        str(new_password or "").strip(),
    ]
    for secret in secrets_to_hide:
        if secret:
            text = text.replace(secret, "[已隐藏]")
    return text[:400]


def _account_operation_event_sink(
    account: dict[str, Any],
    operation: str,
    *,
    secrets: tuple[Any, ...] = (),
):
    """Build a bounded structured sink used by protocol maintenance flows.

    Protocol callbacks can emit dozens of ``running`` step updates. The live
    task stream retains those details; the durable per-account panel keeps the
    first running marker while the outer operation records its terminal result.
    """
    seen_statuses: set[str] = set()
    terminal_statuses = {
        "success",
        "failed",
        "cancelled",
        "skipped",
        "error",
        "complete",
        "completed",
    }

    def sink(event: dict[str, Any] | None) -> None:
        if not isinstance(event, dict):
            return
        status = str(event.get("status") or "info").strip().lower()
        if status in terminal_statuses:
            # The surrounding web operation writes the terminal summary with
            # its persisted result/error context. Avoid a second callback row.
            return
        if status == "running":
            if status in seen_statuses:
                return
            seen_statuses.add(status)
        else:
            # Detailed step/info callbacks remain in the transient task log.
            return
        _state().record_operation_event(
            account,
            operation,
            status,
            message=event.get("message") or "",
            step=event.get("step") or "",
            error=event.get("error") or "",
            metadata={"index": event.get("index")} if event.get("index") is not None else {},
            secrets=secrets,
            at=str(event.get("at") or "") or None,
        )

    return sink


def _run_security_protocol_operation(
    account: dict[str, Any],
    operation: str,
    *,
    new_password: str = "",
    password_mode: str = "custom",
    stop_check=None,
) -> dict[str, Any]:
    """Run one password/2FA mutation and persist only the resulting fields."""
    from register.log import _LOG_SINK
    import register.protocol as protocol
    from register.sessions import JobCancelled
    from register.protocol_runtime import ProtocolRuntimeSettings
    from proxy_util import is_proxy_transport_error, proxy_brief

    operation = _normalize_account_security_operation(operation)
    if operation not in {"password_change", "totp_change"}:
        raise ValueError("该协议操作只支持修改密码或修改 2FA")
    email = str(account.get("email") or "").strip()
    if not email:
        raise RuntimeError("账号缺少邮箱")
    _state().record_operation_event(
        account,
        operation,
        "running",
        message=f"{_account_security_operation_label(operation)}开始",
    )
    old_password = str(account.get("password") or "").strip()
    old_totp = str(account.get("totp_secret") or "").strip()
    try:
        if operation == "password_change":
            if not old_password or not old_totp:
                raise RuntimeError("修改密码要求账号已保存旧密码和 TOTP 2FA")
            password_mode = _normalize_security_password_mode(password_mode)
            if password_mode == "random":
                new_password = str(new_password or "").strip() or _generate_security_password()
            else:
                new_password = str(new_password or "").strip()
            if len(new_password) < _SECURITY_PASSWORD_MIN_LENGTH:
                raise RuntimeError(
                    f"新密码至少需要 {_SECURITY_PASSWORD_MIN_LENGTH} 个字符"
                )
            if new_password == old_password:
                raise RuntimeError("新密码不能与旧密码相同")
        elif not old_password or not old_totp:
            raise RuntimeError("修改 2FA 要求账号已保存密码和旧 TOTP 2FA")
    except Exception as error:
        _state().record_operation_event(
            account,
            operation,
            "failed",
            message=f"{_account_security_operation_label(operation)}预检失败",
            error=error,
            secrets=(old_password, old_totp, new_password),
        )
        raise

    stop = stop_check if callable(stop_check) else (lambda: False)
    log_token = _LOG_SINK.set(_state().JOB.append_log)
    try:
        cfg = _state()._read_config()
        registration = cfg.get("registration") if isinstance(cfg.get("registration"), dict) else {}
        candidates, proxy_cfg = _security_proxy_candidates(account, cfg)
        runtime = ProtocolRuntimeSettings.from_config(registration)
        event_error = ""
        current_step = ""

        def capture_event(_index, _status=None, **fields):
            nonlocal event_error, current_step
            step_text = str(fields.get("step") or "").strip()
            if step_text:
                current_step = step_text
                _state().JOB.append_log(f"[{_account_security_operation_label(operation)}] 步骤 · {email} · {step_text}")
            detail = str(fields.get("error") or "").strip()
            if detail:
                event_error = detail

        kwargs = {
            "email": email,
            "proxy_config": proxy_cfg,
            "stop_check": stop,
            "task_event": capture_event,
            "account_log_sink": _account_operation_event_sink(
                account,
                operation,
                secrets=(old_password, old_totp, new_password),
            ),
            "error_sink": lambda detail: capture_event(1, "failed", error=detail),
            "protocol_runtime": runtime,
        }
        wrapper: Any | None = None
        if operation == "password_change":
            kwargs.update(
                old_password=old_password,
                new_password=new_password,
                totp_secret=old_totp,
            )
            try:
                wrapper = _resolve_security_protocol_wrapper(
                    protocol,
                    ("change_password_one", "change_password_known_one", "change_password_known"),
                )
            except RuntimeError:
                wrapper = None
        else:
            kwargs.update(password=old_password, totp_secret=old_totp)
            try:
                wrapper = _resolve_security_protocol_wrapper(
                    protocol,
                    ("change_totp_one", "rotate_totp_one", "replace_totp_one", "change_2fa_one"),
                )
            except RuntimeError:
                wrapper = None
        if wrapper is None:
            # Compatibility with protocol builds that expose only the bound
            # PlatformRegistrar methods. The adapter keeps the same wrapper
            # contract used by the batch worker and closes each session.
            method_name = (
                "change_password"
                if operation == "password_change"
                else "change_totp"
            )

            def class_method_adapter(index: int, **call_kwargs: Any) -> dict[str, Any] | None:
                from register.protocol import (
                    PlatformRegistrar,
                    _PROXY_CONFIG,
                    _STOP_CHECK,
                    _TASK_EVENT,
                    _FIXED_EMAIL,
                    activate_runtime,
                )

                stop_callback = call_kwargs.get("stop_check")
                if callable(stop_callback) and stop_callback():
                    raise JobCancelled("stop requested")
                selected_proxy = str(call_kwargs.pop("proxy") or "")
                event_token = _TASK_EVENT.set(call_kwargs.get("task_event"))
                stop_token = _STOP_CHECK.set(stop_callback or (lambda: False))
                proxy_token = _PROXY_CONFIG.set(dict(call_kwargs.get("proxy_config") or {}))
                email_token = _FIXED_EMAIL.set(email)
                runtime_context = activate_runtime(call_kwargs.get("protocol_runtime") or runtime)
                runtime_context.__enter__()
                registrar = None
                try:
                    registrar = PlatformRegistrar(selected_proxy)
                    method = getattr(registrar, method_name, None)
                    if not callable(method) and operation == "totp_change":
                        method = getattr(registrar, "replace_totp", None)
                    if not callable(method):
                        raise RuntimeError(f"协议层未提供安全操作接口: {method_name}")
                    if operation == "password_change":
                        return method(
                            email,
                            old_password,
                            new_password,
                            old_totp,
                            index,
                        )
                    return method(email, old_password, old_totp, index)
                finally:
                    if registrar is not None:
                        registrar.close()
                    _PROXY_CONFIG.reset(proxy_token)
                    _STOP_CHECK.reset(stop_token)
                    _TASK_EVENT.reset(event_token)
                    _FIXED_EMAIL.reset(email_token)
                    runtime_context.__exit__(None, None, None)

            wrapper = class_method_adapter
        result: dict[str, Any] | None = None
        last_error: BaseException | None = None
        for attempt, proxy in enumerate(candidates):
            if stop():
                raise JobCancelled("stop requested")
            event_error = ""
            try:
                _state().JOB.append_log(
                    f"[{_account_security_operation_label(operation)}] 开始 · {email} · "
                    f"线路={proxy_brief(proxy) if proxy else 'direct'}"
                )
                result = _call_security_protocol_wrapper(
                    wrapper,
                    index=1,
                    kwargs={"proxy": proxy, **kwargs},
                )
                if isinstance(result, dict):
                    break
                raise RuntimeError(event_error or "协议安全操作未返回结果")
            except JobCancelled:
                raise
            except Exception as error:
                last_error = error
                if not is_proxy_transport_error(error) or attempt >= len(candidates) - 1:
                    raise
                _state().JOB.append_log(
                    f"[{_account_security_operation_label(operation)}] 代理传输失败 · {email} · "
                    f"切换线路 {attempt + 2}/{len(candidates)}"
                )
                time.sleep(0.5)
        if not isinstance(result, dict):
            raise last_error or RuntimeError("协议安全操作未返回结果")

        saved = dict(account)
        # Keep only non-secret protocol metadata that is useful for the next
        # session. Explicit credential fields are set below from trusted input.
        for key in (
            "access_token", "refresh_token", "id_token", "session_token",
            "account_id", "session", "cookies", "profile", "plan_type",
            "expires_at", "oauth_client_id", "mfa_factor_id", "mfa_factor_type",
            "device_id", "protocol_profile_id", "browser_type", "tls_impersonate",
            "user_agent", "sec_ch_ua", "sec_ch_ua_full_version_list", "locale",
            "timezone", "timezone_offset_min", "protocol_screen",
            "protocol_hardware_concurrency", "protocol_device_memory",
            "protocol_platform", "protocol_webgl_vendor", "protocol_webgl_renderer",
        ):
            if key in result and result[key] not in (None, "", {}):
                saved[key] = result[key]
        if operation == "password_change":
            saved.update(
                password=new_password,
                password_set=True,
                password_verified_at=_state()._now_iso(),
            )
        else:
            replacement = str(
                result.get("totp_secret")
                or result.get("new_totp_secret")
                or result.get("secret")
                or ""
            ).strip()
            if not replacement:
                raise RuntimeError("协议修改 2FA 未返回新的 TOTP 密钥")
            saved.update(
                totp_secret=replacement,
                mfa_enabled=True,
                totp_verified_at=_state()._now_iso(),
            )
        if proxy:
            saved["proxy"] = proxy
        saved.update(
            last_security_operation=operation,
            last_security_error="",
            last_security_failed_at="",
            updated_at=_state()._now_iso(),
            # A successful mutation proves the credential path, but an old
            # invalid/reauth state still needs a health probe before it can be
            # advertised as usable. Healthy rows retain their classification.
            health_status=(
                "unknown"
                if _state().normalized_health_status(account)
                in {"invalid", "reauth_required", "error"}
                else str(account.get("health_status") or "unknown")
            ),
            health_error="",
        )
        persisted = _state().upsert_account_record(saved, _state().ACCOUNTS_FILE, create=False)
        _state().JOB.append_log(f"[{_account_security_operation_label(operation)}] 成功 · {email}")
        _state().record_operation_event(
            persisted,
            operation,
            "success",
            message=f"{_account_security_operation_label(operation)}成功",
            metadata={"proxy": proxy_brief(proxy) if proxy else "direct"},
            secrets=(old_password, old_totp, new_password),
        )
        return persisted
    except JobCancelled:
        _state().record_operation_event(
            account,
            operation,
            "cancelled",
            message=f"{_account_security_operation_label(operation)}已停止",
            secrets=(old_password, old_totp, new_password),
        )
        raise
    except Exception as error:
        safe_error = _redact_security_detail(error, account, new_password)
        try:
            _persist_security_operation_failure(account, operation, safe_error)
        except Exception:
            pass
        _state().JOB.append_log(
            f"[{_account_security_operation_label(operation)}] 失败 · {email} · {safe_error[:300]}"
        )
        _state().record_operation_event(
            account,
            operation,
            "failed",
            message=f"{_account_security_operation_label(operation)}失败",
            error=safe_error,
            step=current_step,
            secrets=(old_password, old_totp, new_password),
        )
        raise
    finally:
        _LOG_SINK.reset(log_token)


def _change_account_password_sync(
    account: dict[str, Any],
    new_password: str,
    stop_check=None,
    *,
    password_mode: str = "custom",
) -> dict[str, Any]:
    return _run_security_protocol_operation(
        account,
        "password_change",
        new_password=new_password,
        password_mode=password_mode,
        stop_check=stop_check,
    )


def _change_account_totp_sync(
    account: dict[str, Any],
    stop_check=None,
) -> dict[str, Any]:
    return _run_security_protocol_operation(
        account,
        "totp_change",
        stop_check=stop_check,
    )

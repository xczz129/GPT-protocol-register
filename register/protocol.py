# -*- coding: utf-8 -*-
# ruff: noqa: F401
"""纯 HTTP 协议注册门面：任务编排 + 历史导入路径兼容。

实现拆分为 protocol_context / protocol_fingerprint / protocol_http /
protocol_registrar / protocol_ops 五个模块；本文件保留注册 / 登录 / 重登
编排入口，并 re-export 全部历史公开名，外部导入路径保持不变。
"""

from __future__ import annotations

import base64
import asyncio
import contextvars
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import string
import threading
import time
import uuid
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo

from curl_cffi import requests

from common.account_health import check_account
from common.account_store import ACCOUNTS_FILE, upsert_account_record
from common.country_identity import COUNTRY_IDENTITIES, country_code_from_proxy, country_identity
from common.proxy_clearance import clearance_error, headers_with_clearance, refresh_clearance
from common.session_export import build_account_record, parse_jwt_payload
from common.registration_trace import RegistrationTrace
from common.totp import totp_code
from register.sessions import JobCancelled
from mail import create_mailbox as allocate_mailbox
from mail import (
    mark_mailbox_code_boundary,
    mark_mailbox_result,
    release_mailbox,
    update_mailbox_states,
    wait_for_code as read_mail_code,
)
from mail.parsing import (
    CANONICAL_PROVIDER_TYPES,
    normalize_provider_type,
    parse_domain_mailboxes,
    parse_icloud_credentials,
    parse_outlook_credentials,
)
from proxy_util import (
    compute_proxy_retry_limit,
    is_proxy_transport_error,
    normalize_proxy_url,
    proxy_brief,
    resolve_proxy_pool,
)
from register.protocol_pkce import generate_pkce as _generate_pkce
from register.protocol_sentinel import (
    build_sentinel_token as _build_sentinel_token_tuple,
    build_sentinel_with_so_token,
)
from register.protocol_runtime import (
    ProtocolRuntimeSettings,
    activate_runtime,
    current_runtime_settings,
)
from register.scheduling import build_registration_waves
from register.mail_channel_options import (
    is_smsbower_selected,
    needs_dynamic_mail_coordinator,
    smsbower_child_enabled,
    validate_smsbower_job,
)
from register.smsbower_batch import run_dynamic_mail_groups
from register.registration_outcome import RegisteredEmailDetected
from register.pending_recovery import can_recover_registration, merge_registration_progress, recover_registration
from register.util import _append_account

from register.protocol_context import (    # noqa: F401 - 兼容 re-export
    PasswordResetRequired,
    _FIXED_EMAIL,
    _MAIL_CONFIG,
    _PASSWORD_AUTH_FLOW_GATE,
    _PROVIDER_TYPE,
    _PROXY_CONFIG,
    _REGISTRATION_TRACE,
    _STOP_CHECK,
    _TASK_EVENT,
    _add_task_event_secrets,
    _auth_error_has_code,
    _is_cloudflare_challenge,
    _is_wrong_email_otp_error,
    _mail_config,
    _make_trace_headers,
    _mfa_activation_error_text,
    _oauth_timeout,
    _operation_event_forwarder,
    _otp_wait_timeout_message,
    _protocol_proxy_for_task,
    _random_birthdate,
    _random_name,
    _random_password,
    _redact_operation_error,
    _response_debug_detail,
    _response_json,
    _tls_verify,
    _totp_code,
    _truthy,
    auth_base,
    chatgpt_base,
    chatgpt_oauth_client_id,
    config,
    create_mailbox,
    is_dead_slot_error,
    log,
    platform_auth0_client,
    platform_base,
    platform_oauth_audience,
    platform_oauth_client_id,
    platform_oauth_redirect_uri,
    print_lock,
    register_log_sink,
    stats,
    stats_lock,
    step,
    wait_for_code,
)

from register.protocol_fingerprint import (    # noqa: F401 - 兼容 re-export
    COUNTRY_TIMEZONE_OFFSETS,
    DEFAULT_BROWSER_FINGERPRINT,
    REGISTER_BROWSER_PROFILES,
    REGISTER_COUNTRY_DEVICE_TRAITS,
    REGISTER_COUNTRY_WEBGL_TRAITS,
    REGISTER_DEVICE_TRAITS,
    REGISTER_JAPAN_WEBGL_TRAITS,
    REGISTER_LANGUAGE_PROFILES,
    REGISTER_US_WEBGL_TRAITS,
    _browser_fingerprint,
    _claim_fingerprint_combo,
    _client_hint_headers,
    _complete_browser_fingerprint,
    _country_fingerprint_traits,
    _extract_firefox_version_from_user_agent,
    _fingerprint_pool_key,
    _fingerprint_with_user_agent,
    _firefox_sec_ch_ua,
    _firefox_sec_ch_ua_full_version_list,
    _firefox_user_agent,
    _header_fingerprint,
    _language_profiles_for_proxy,
    _make_browser_fingerprint,
    _proxy_country_code,
    _timezone_offset_for_proxy,
    _timezone_offset_for_timezone,
    _validate_browser_profiles,
    _weighted_fingerprint_profiles,
    common_headers,
    navigate_headers,
    sec_ch_ua,
    sec_ch_ua_full_version_list,
    user_agent,
)

from register.protocol_http import (    # noqa: F401 - 兼容 re-export
    _absolute_auth_url,
    _append_exchange_error,
    _auth_error_details_from_url,
    _auth_step_details,
    _auth_step_requires_password,
    _authorize_landed_page,
    _build_sentinel_headers,
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
    extract_callback_via_consent,
    extract_oauth_callback_params_from_url,
    request_platform_oauth_token,
    request_platform_oauth_token_legacy,
    request_with_local_retry,
    validate_otp,
)

from register.protocol_registrar import PlatformRegistrar  # noqa: F401 - 兼容 re-export

from register.protocol_ops import (    # noqa: F401 - 兼容 re-export
    _decorate_protocol_tokens,
    _validate_protocol_account_result,
    _validate_protocol_login_result,
    change_password_one,
    change_totp_one,
    login_one,
    login_with_password_totp_one,
    repair_security_one,
)




replace_totp_one = change_totp_one


change_password_known_one = change_password_one


change_password_known = change_password_one


rotate_totp_one = change_totp_one


change_2fa_one = change_totp_one




def register_one(
    index: int,
    *,
    mail_config: dict[str, Any],
    proxy: str = "",
    fixed_email: str | None = None,
    provider_type: str | None = None,
    stop_check: Callable[[], bool] | None = None,
    task_event=None,
    account_log_sink: Callable[[dict[str, Any]], None] | None = None,
    proxy_config: dict[str, Any] | None = None,
    group_id: str = "",
    post_register_health_check: bool = False,
    protocol_runtime: ProtocolRuntimeSettings | None = None,
    mailbox_override: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if isinstance(mailbox_override, dict):
        fixed_email = str(mailbox_override.get("address") or fixed_email or "") or None
    start = time.time()
    stop = stop_check if callable(stop_check) else (lambda: False)
    trace = RegistrationTrace(
        index,
        proxy=proxy,
        provider=provider_type or "",
        fixed_email=fixed_email or "",
    )
    # RegistrationTrace paths are rooted at ``<repo>/data/registration_logs``;
    # use the known project root from the path itself to keep this module
    # independent of config-level ROOT imports.
    trace_file_ref = str(trace.path.relative_to(trace.path.parents[2]))
    trace_token = _REGISTRATION_TRACE.set(trace)

    def traced_task_event(event_index: int, status: str | None = None, **fields: Any) -> None:
        trace.event(status, **fields)
        if callable(task_event):
            task_event(event_index, status, **fields)

    # Keep the historical callback untouched while optionally projecting the
    # same events to the durable account-operation log.
    forwarded_task_event = _operation_event_forwarder(
        traced_task_event,
        account_log_sink,
        operation="registration",
        index=index,
        email=str(fixed_email or "").strip(),
    )
    emit_account_events = callable(task_event) or callable(account_log_sink)
    event_token = _TASK_EVENT.set(forwarded_task_event)
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
    partial_account: dict[str, Any] = {}

    def persist_partial(record: dict[str, Any]) -> None:
        """Keep password/session credentials when TOTP is the remaining step."""
        if not isinstance(record, dict):
            return
        merge_registration_progress(partial_account, record)
        trace.set_secrets(
            partial_account.get("password"),
            partial_account.get("totp_secret"),
            partial_account.get("access_token"),
            partial_account.get("refresh_token"),
            partial_account.get("session_token"),
        )
        email = str(partial_account.get("email") or "").strip()
        if not email:
            return
        partial_account["group_id"] = str(group_id or "").strip()
        partial_account.setdefault("health_status", "unknown")
        partial_account.setdefault("health_checked_at", "")
        partial_account.setdefault("health_error", "")
        partial_account["registration_state"] = "security_pending"
        partial_account["updated_at"] = datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        active_mailbox = mailbox_override or (getattr(registrar, "registration_mailbox", None) if registrar is not None else None)
        if active_mailbox is not None:
            active_mailbox["_registration_partial_account"] = dict(partial_account)
        try:
            upsert_account_record(dict(partial_account), ACCOUNTS_FILE)
        except Exception as error:
            log(
                f"{email} 中间账号状态保存失败，继续执行 2FA: {str(error)[:240]}",
                "yellow",
            )

    try:
        registrar = PlatformRegistrar(proxy)
        initial_device_id = registrar.device_id
        trace.identity(registrar.fingerprint, device_id=initial_device_id)
        if emit_account_events:
            forwarded_task_event(index, "running", proxy=proxy_brief(proxy), step="纯协议任务启动")
        step(index, "任务启动")
        register_options = {"partial_sink": persist_partial}
        if mailbox_override is not None:
            register_options["mailbox_override"] = mailbox_override
            trace.set_secrets(mailbox_override.get("_smsbower_api_key"))
        result = None
        recovery_only = bool((mailbox_override or {}).get("_registration_recovery_only"))
        try:
            if recovery_only:
                mailbox_override["_registration_recovery_attempted"] = True
                merge_registration_progress(partial_account, (mailbox_override or {}).get("_registration_partial_account") or {})
                result = recover_registration(index, account=partial_account, mailbox=mailbox_override,
                    mail_config=mail_config, proxy=proxy, stop_check=stop, task_event=forwarded_task_event,
                    partial_sink=persist_partial, protocol_runtime=protocol_runtime)
            else:
                result = registrar.register(index, **register_options)
            _validate_protocol_account_result(result)
        except (RegisteredEmailDetected, PasswordResetRequired):
            raise
        except Exception:
            if recovery_only or stop():
                raise
            actual_mailbox = mailbox_override or getattr(registrar, "registration_mailbox", None)
            candidate = dict(partial_account)
            if isinstance(result, dict):
                merge_registration_progress(candidate, result)
            if not can_recover_registration(candidate, actual_mailbox):
                raise
            if actual_mailbox is not None:
                actual_mailbox["_registration_partial_account"] = candidate
            if actual_mailbox is not None and actual_mailbox.get("provider") == "smsbower_mail" and actual_mailbox.get("register_child", True) and actual_mailbox.get("_smsbower_pair_role", "primary") == "primary" and (actual_mailbox.get("_registration_code_received") or actual_mailbox.get("_otp_accepted_by_server")):
                actual_mailbox["_registration_recovery_pending"] = True
                forwarded_task_event(index, "running", email=candidate.get("email"), step="主号已收码并保存密码，先保留邮箱给子号取码，再密码登录补全")
                trace.finish("recovery_pending", email=candidate.get("email"), persist_operation=False)
                return None
            result = recover_registration(index, account=candidate, mailbox=actual_mailbox,
                mail_config=mail_config, proxy=proxy, stop_check=stop, task_event=forwarded_task_event,
                partial_sink=persist_partial, protocol_runtime=protocol_runtime)
            _validate_protocol_account_result(result)
        if mailbox_override is not None:
            mailbox_override["_registration_recovery_pending"] = False
            if recovery_only:
                mailbox_override["_registration_recovery_attempted"] = True
        if mailbox_override is not None and mailbox_override.get("provider") == "smsbower_mail":
            result["mailbox_role"] = str(mailbox_override.get("_smsbower_pair_role") or "primary")
            result["mailbox_base_address"] = str(mailbox_override.get("base_address") or mailbox_override.get("address") or "")
            result["mailbox_pair_id"] = str(mailbox_override.get("_smsbower_pair_id") or "")
        final_device_id = str(result.get("device_id") or registrar.device_id or "").strip()
        result["protocol_initial_device_id"] = initial_device_id
        result["protocol_final_device_id"] = final_device_id
        trace.device_sync(initial_device_id=initial_device_id, final_device_id=final_device_id)
        result["group_id"] = str(group_id or "").strip()
        result["registration_trace_file"] = trace_file_ref
        trace.set_secrets(
            result.get("password"),
            result.get("totp_secret"),
            result.get("access_token"),
            result.get("refresh_token"),
            result.get("session_token"),
        )
        result.setdefault("health_status", "unknown")
        result.setdefault("health_checked_at", "")
        result.setdefault("health_error", "")
        cost = time.time() - start
        _append_account(result)
        if stop():
            log(f'{result["email"]} 注册已完成，停止后跳过测活，本次耗时{cost:.1f}s', "green")
            if emit_account_events:
                forwarded_task_event(
                    index,
                    "success",
                    email=result.get("email"),
                    account_id=result.get("account_id"),
                    trace_file=result.get("registration_trace_file"),
                    step="注册完成；已停止后续测活",
                )
            trace.finish(
                "success",
                email=result.get("email"),
                result=result,
                persist_operation=not callable(account_log_sink),
                secrets=tuple(result.get(key) for key in ("password", "totp_secret", "access_token", "refresh_token", "session_token")),
            )
            return result
        if post_register_health_check:
            checked = check_account(result)
            if checked.get("health_status") == "error":
                step(index, f"账号已保存，测活暂未成功: {checked.get('health_error')}", "yellow")
            result = checked
        else:
            step(index, "账号已保存；远程测活已延后", "green")
        with stats_lock:
            stats["done"] += 1
            stats["success"] += 1
            avg = (time.time() - float(stats.get("start_time") or start)) / stats["success"]
        log(f'{result["email"]} 注册成功，本次耗时{cost:.1f}s，全局平均每个号注册耗时{avg:.1f}s', "green")
        if emit_account_events:
            completion = (
                "注册完成并已测活"
                if post_register_health_check
                else "注册完成；远程测活已延后"
            )
            forwarded_task_event(
                index,
                "success",
                email=result.get("email"),
                account_id=result.get("account_id"),
                trace_file=result.get("registration_trace_file"),
                step=completion,
            )
        trace.finish(
            "success",
            email=result.get("email"),
            result=result,
            persist_operation=not callable(account_log_sink),
            secrets=tuple(result.get(key) for key in ("password", "totp_secret", "access_token", "refresh_token", "session_token")),
        )
        return result
    except (PasswordResetRequired, RegisteredEmailDetected) as error:
        cost = time.time() - start
        with stats_lock:
            stats["done"] += 1
            stats["skipped"] += 1
        log(
            f"任务{index} 检测到邮箱已注册，已跳过，本次耗时{cost:.1f}s",
            "yellow",
        )
        if emit_account_events:
            forwarded_task_event(
                index,
                "skipped",
                email=error.email or None,
                group_id=str(group_id or "").strip(),
                skip_reason=error.code,
                trace_file=trace_file_ref,
                step="邮箱已注册，已跳过；未登录或重置密码",
            )
        trace.finish(
            "skipped",
            email=error.email,
            error=error,
            persist_operation=not callable(account_log_sink),
        )
        return None
    except Exception as e:
        cost = time.time() - start
        transport_error = is_proxy_transport_error(e)
        with stats_lock:
            if not transport_error:
                stats["done"] += 1
                stats["fail"] += 1
        if transport_error:
            log(
                f"任务{index} 当前代理传输失败，本次耗时{cost:.1f}s，调度器将切换代理重试",
                "yellow",
            )
        else:
            log(f"任务{index} 注册失败，本次耗时{cost:.1f}s，原因: {e}", "red")
        if emit_account_events:
            status = "cancelled" if stop() else ("running" if transport_error else "failed")
            fields = (
                {
                    "email": str(partial_account.get("email") or trace.email).strip(),
                    "trace_file": trace_file_ref,
                    "error": str(e)[:300],
                    "step": "当前代理连接失败，等待切换下一条代理",
                }
                if transport_error and not stop()
                else {
                    "email": str(partial_account.get("email") or trace.email).strip(),
                    "trace_file": trace_file_ref,
                    "error": str(e)[:300],
                    "step": str(e)[:160],
                }
            )
            forwarded_task_event(index, status, **fields)
        trace.finish(
            "transport_retry" if transport_error else "failed",
            email=str(partial_account.get("email") or trace.email).strip(),
            error=e,
            persist_operation=not callable(account_log_sink),
            secrets=tuple(partial_account.get(key) for key in ("password", "totp_secret", "access_token", "refresh_token", "session_token")),
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
        _REGISTRATION_TRACE.reset(trace_token)




def worker(index: int) -> dict:
    result = register_one(
        index,
        mail_config=config["mail"],
        proxy=str(config.get("proxy") or ""),
        proxy_config=config.get("proxy_config"),
    )
    return {"ok": bool(result), "index": index, "result": result}




async def run_protocol_register(
    app_config: dict[str, Any],
    *,
    total: int | None = None,
    concurrency: int | None = None,
    fixed_email: str | None = None,
    provider_type: str | None = None,
    stop_check=None,
    pause_check=None,
    cancel_event: asyncio.Event | None = None,
    task_event=None,
    account_log_sink: Callable[[dict[str, Any]], None] | None = None,
    group_id: str = "",
    group_plan: list[str] | None = None,
    proxy_config_loader=None,
) -> list[dict[str, Any]]:
    """Run the copied chatgpt2api protocol registrar with this project's pools."""
    from mail import count_pool_capacity, release_stale_in_use

    reg_cfg = app_config.get("registration") if isinstance(app_config.get("registration"), dict) else {}
    source_mail = app_config.get("mail") if isinstance(app_config.get("mail"), dict) else {}
    mail_config = dict(source_mail)
    mail_config["wait_timeout"] = max(
        10,
        int(reg_cfg.get("protocol_otp_timeout") or source_mail.get("wait_timeout") or 90),
    )
    mail_config["wait_chunk"] = max(
        30,
        min(45, int(reg_cfg.get("protocol_otp_poll_chunk") or 45)),
    )
    mail_config["request_timeout"] = max(
        15,
        int(source_mail.get("request_timeout") or 30),
    )
    proxy_cfg = app_config.get("proxy") if isinstance(app_config.get("proxy"), dict) else {}
    stop = stop_check if callable(stop_check) else (lambda: False)
    paused = pause_check if callable(pause_check) else (lambda: False)
    n = max(1, int(total if total is not None else reg_cfg.get("total") or 1))
    conc = max(1, int(concurrency if concurrency is not None else reg_cfg.get("threads") or 1))
    validate_smsbower_job(mail_config, provider_type, n, conc, fixed_email=fixed_email)
    max_retries = max(0, int(reg_cfg.get("max_transient_retries") or 0))
    post_register_health_check = _truthy(
        reg_cfg.get("post_register_health_check"), False
    )

    release_stale_in_use()
    capacity = count_pool_capacity(
        mail_config,
        provider_type=provider_type if str(provider_type or "auto").lower() not in {"", "auto", "any", "all"} else None,
        fixed_email=fixed_email,
        fixed_alias_count=1 if fixed_email else None,
    )
    available = int(capacity.get("available") or 0)
    available_emails = [
        str(email or "").strip().lower()
        for email in (capacity.get("emails") or [])
        if str(email or "").strip()
    ]
    fixed_alias_batch = bool(
        fixed_email
        and str(capacity.get("fixed_email_mode") or "") == "alias_batch"
    )
    if available <= 0:
        print(f"  [协议调度] 无可用邮箱 (池内={capacity.get('total')} used={capacity.get('used')})")
        return []
    if fixed_alias_batch:
        n = min(1, available)
        conc = 1
        print(
            f"  [协议调度] 指定主邮箱别名批次 → "
            f"单别名启动 总数={n} 并发={conc}"
        )
    elif fixed_email:
        n = conc = 1
    else:
        n = min(n, available)
        conc = min(conc, n, available)
        if is_smsbower_selected(provider_type) and smsbower_child_enabled(mail_config):
            if smsbower_child_enabled(mail_config):
                n -= n % 2
                if n < 2:
                    return []
            conc = max(2, conc - conc % 2)

    proxy_pool = resolve_proxy_pool(proxy_cfg)
    fallback_proxy = normalize_proxy_url(str(proxy_cfg.get("url") or "")) if proxy_cfg.get("enabled") else ""
    if not proxy_pool and fallback_proxy:
        proxy_pool = [fallback_proxy]
    rotate = bool(proxy_cfg.get("rotate_per_task", True))
    proxy_retry_limit = compute_proxy_retry_limit(max_retries, proxy_pool)
    proxy_start_offset = (
        random.SystemRandom().randrange(len(proxy_pool))
        if rotate and proxy_pool
        else 0
    )
    proxy_refresh_lock = threading.Lock()
    proxy_refresh_key = (
        tuple(proxy_pool),
        fallback_proxy,
        rotate,
        bool(proxy_cfg.get("enabled")),
    )
    proxy_refresh_offset = proxy_start_offset

    def current_proxy_snapshot() -> tuple[dict[str, Any], list[str], str, bool, int]:
        """Read a newer panel proxy pool between attempts when available."""
        nonlocal proxy_refresh_key, proxy_refresh_offset
        latest_cfg = proxy_cfg
        if callable(proxy_config_loader):
            try:
                loaded = proxy_config_loader()
                if isinstance(loaded, dict):
                    latest_cfg = dict(loaded)
            except Exception as error:
                print(f"  [代理调度] 刷新代理配置失败，继续使用当前池: {str(error)[:160]}")

        enabled = bool(latest_cfg.get("enabled"))
        latest_pool = resolve_proxy_pool(latest_cfg) if enabled else []
        latest_fallback = (
            normalize_proxy_url(str(latest_cfg.get("url") or ""))
            if enabled
            else ""
        )
        if not latest_pool and latest_fallback:
            latest_pool = [latest_fallback]
        # A temporarily incomplete saved pool must not turn an active batch
        # into direct connections; keep the last known working pool instead.
        if enabled and not latest_pool and proxy_pool:
            latest_cfg = proxy_cfg
            latest_pool = list(proxy_pool)
            latest_fallback = fallback_proxy
            enabled = bool(proxy_cfg.get("enabled"))
        latest_rotate = bool(latest_cfg.get("rotate_per_task", True))
        key = (tuple(latest_pool), latest_fallback, latest_rotate, enabled)
        with proxy_refresh_lock:
            changed = key != proxy_refresh_key
            if changed:
                proxy_refresh_key = key
                proxy_refresh_offset = (
                    random.SystemRandom().randrange(len(latest_pool))
                    if latest_rotate and len(latest_pool) > 1
                    else 0
                )
            latest_offset = proxy_refresh_offset
        if changed:
            print(
                f"  [代理调度] 已刷新代理配置 · mode={str(latest_cfg.get('mode') or 'single_proxy')} "
                f"可用代理={len(latest_pool)} 轮换={latest_rotate}"
            )
        return latest_cfg, latest_pool, latest_fallback, latest_rotate, latest_offset
    protocol_runtime = ProtocolRuntimeSettings.from_config(reg_cfg)
    with stats_lock:
        stats.update(
            {"done": 0, "success": 0, "fail": 0, "skipped": 0, "start_time": time.time()}
        )

    print(f"  GPT 纯协议注册  总数={n} 并发={conc} 邮箱可用={available}")
    results: list[dict[str, Any] | None] = [None] * n
    planned_emails = available_emails[:n] if (not fixed_email or fixed_alias_batch) else []
    retry_emails = iter(
        available_emails[n:] if (not fixed_email or fixed_alias_batch) else []
    )
    retry_email_lock = threading.Lock()
    batch_bad_proxies: set[str] = set()
    batch_bad_proxy_lock = threading.Lock()
    schedule_emails = planned_emails + [str(fixed_email or "")] * (n - len(planned_emails))
    task_waves = build_registration_waves(
        schedule_emails,
        conc,
        fixed_alias_batch=fixed_alias_batch,
    )
    has_alias_pairs = any(is_pair for _indices, is_pair in task_waves)
    semaphore = asyncio.Semaphore(max(conc, 2 if has_alias_pairs else 1))
    if has_alias_pairs and not fixed_alias_batch:
        print("  [协议调度] 双别名按主邮箱分波执行；每对完整注册结束后再启动下一对")
        if callable(task_event):
            first_wave = set(task_waves[0][0]) if task_waves else set()
            for task_index, email in enumerate(schedule_emails, start=1):
                if task_index in first_wave:
                    continue
                task_group_id = (
                    str(group_plan[task_index - 1] or "").strip()
                    if isinstance(group_plan, list) and task_index <= len(group_plan)
                    else str(group_id or "").strip()
                )
                task_event(
                    task_index,
                    "queued",
                    email=email,
                    group_id=task_group_id,
                    step="等待前一双别名完整注册",
                )

    def claim_retry_email(current: str | None) -> str | None:
        """Reserve a spare slot without stealing another task's planned slot."""
        with retry_email_lock:
            return next(retry_emails, current)

    def bad_proxy_snapshot(local: set[str]) -> set[str]:
        with batch_bad_proxy_lock:
            return set(batch_bad_proxies) | set(local)

    def quarantine_proxy(proxy: str) -> int:
        if not proxy:
            return 0
        with batch_bad_proxy_lock:
            batch_bad_proxies.add(proxy)
            return len(batch_bad_proxies)

    def alias_proxy_retryable(detail: str) -> bool:
        text = str(detail or "").lower()
        if any(
            marker in text
            for marker in (
                "rate_limit_exceeded",
                "invalid_auth_step",
                "invalid_state",
                "password_add_http_409",
            )
        ):
            return False
        return any(
            marker in text
            for marker in (
                "curl: (7)",
                "curl: (28)",
                "curl: (35)",
                "curl: (56)",
                "tls connect error",
                "wrong_version_number",
                "could not resolve proxy",
                "proxy connect",
                "proxyerror",
                "connection reset by peer",
                "connection closed abruptly",
            )
        )

    def stopped() -> bool:
        return bool(stop()) or (
            cancel_event is not None and cancel_event.is_set()
        )

    def persist_registration_summary(
        *,
        status: str,
        email: str = "",
        account_id: str = "",
        trace_file: str = "",
        error: str = "",
        step_text: str = "",
        group_id_value: str = "",
    ) -> None:
        """Send one terminal registration projection to the batch sink.

        ``register_one`` may emit terminal callbacks for an individual
        attempt.  The batch adapter withholds those callbacks while retries
        are pending, so this function supplies the single final status after
        the scheduler has resolved the attempt sequence.
        """
        if not callable(account_log_sink):
            return
        payload: dict[str, Any] = {
            "status": status,
            "email": str(email or "").strip(),
            "account_id": str(account_id or "").strip(),
            "trace_file": str(trace_file or "").strip(),
            "error": str(error or "").strip(),
            "step": str(step_text or "").strip(),
            "group_id": str(group_id_value or "").strip(),
        }
        try:
            account_log_sink({key: value for key, value in payload.items() if value})
        except Exception:
            pass

    async def wait_until_resumed(index: int, task_group_id: str, stop_override=None) -> bool:
        is_stopped = stop_override or stopped
        logged = False
        while paused():
            if is_stopped():
                return False
            if not logged and callable(task_event):
                task_event(
                    index,
                    "paused",
                    group_id=task_group_id,
                    step="任务派发已暂停",
                )
                logged = True
            await asyncio.sleep(0.25)
        if logged and callable(task_event):
            task_event(
                index,
                "queued",
                group_id=task_group_id,
                step="任务派发已继续",
            )
        return not is_stopped()

    async def run_task(
        index: int,
        start_barrier: asyncio.Barrier | None = None,
        paired_alias: bool = False,
        mailbox_override: dict[str, Any] | None = None,
    ) -> None:
        async with semaphore:
            cancellation = (mailbox_override or {}).get("_registration_cancel_event")
            def task_stopped() -> bool:
                return stopped() or bool(cancellation is not None and cancellation.is_set())
            task_group_id = (
                str(group_plan[index - 1] or "").strip()
                if isinstance(group_plan, list) and index <= len(group_plan)
                else str(group_id or "").strip()
            )
            task_email = (
                planned_emails[index - 1]
                if index <= len(planned_emails)
                else fixed_email
            )
            if mailbox_override is not None:
                task_email = str(mailbox_override.get("address") or "") or None

            if not await wait_until_resumed(index, task_group_id, task_stopped):
                persist_registration_summary(
                    status="cancelled",
                    email=task_email or "",
                    step_text="已停止，未执行",
                    group_id_value=task_group_id,
                )
                if callable(task_event):
                    task_event(index, "cancelled", group_id=task_group_id, step="已跳过")
                return
            if callable(task_event):
                task_event(index, "queued", group_id=task_group_id, step="等待协议任务")
            record = None
            last_error = ""
            skipped = False
            trace_file = ""
            account_id = ""
            failed_proxies: set[str] = set()
            # ``register_one`` reports terminal callbacks for each individual
            # attempt. Keep only one running marker in the durable audit stream;
            # the final status is emitted by ``persist_registration_summary``
            # after all retries have been resolved.
            attempt_running_logged = False

            def attempt_account_log_sink(event: dict[str, Any] | None) -> None:
                nonlocal attempt_running_logged
                if not callable(account_log_sink) or not isinstance(event, dict):
                    return
                status = str(event.get("status") or "").strip().lower()
                if status != "running" or attempt_running_logged:
                    # Terminal and informational/step events remain in the live
                    # task stream and registration trace.
                    return
                attempt_running_logged = True
                try:
                    payload = dict(event)
                    if not str(payload.get("email") or "").strip() and task_email:
                        payload["email"] = str(task_email).strip()
                    account_log_sink(payload)
                except Exception:
                    # Audit persistence is observational and must not affect
                    # registration retries.
                    pass
            if start_barrier is not None:
                if callable(task_event):
                    task_event(
                        index,
                        "queued",
                        group_id=task_group_id,
                        email=task_email,
                        step="双别名已就绪，等待同步启动",
                    )
                try:
                    await asyncio.wait_for(start_barrier.wait(), timeout=3.0)
                except (asyncio.TimeoutError, asyncio.BrokenBarrierError):
                    if task_stopped():
                        persist_registration_summary(
                            status="cancelled",
                            email=task_email or "",
                            step_text="同步启动前已停止",
                            group_id_value=task_group_id,
                        )
                        return
                    raise RuntimeError("dual_alias_start_barrier_timeout")
            # Dead slots (remote-registered mailboxes) are retired as used and
            # never heal on retry, so they must not consume the transient
            # retry budget: the task keeps switching slots instead, bounded by
            # this hard cap.
            dead_slot_switch_limit = 10
            dead_slot_switches = 0
            for attempt in range(proxy_retry_limit + 1 + dead_slot_switch_limit):
                if not await wait_until_resumed(index, task_group_id, task_stopped):
                    break
                attempt_error = ""
                (
                    attempt_proxy_cfg,
                    attempt_proxy_pool,
                    attempt_fallback,
                    attempt_rotate,
                    attempt_offset,
                ) = current_proxy_snapshot()

                def attempt_event(
                    event_index: int,
                    status: str | None = None,
                    **fields: Any,
                ) -> None:
                    nonlocal attempt_error, last_error, skipped, trace_file, account_id
                    detail = str(fields.get("error") or "").strip()
                    if detail:
                        attempt_error = detail
                        last_error = detail
                    if status == "skipped" or str(fields.get("skip_reason") or "").strip() == PasswordResetRequired.code:
                        skipped = True
                    if fields.get("trace_file"):
                        trace_file = str(fields.get("trace_file") or "").strip()
                    if fields.get("account_id"):
                        account_id = str(fields.get("account_id") or "").strip()
                    if callable(task_event):
                        forwarded = dict(fields)
                        if is_proxy_transport_error(detail):
                            forwarded.pop("error", None)
                            forwarded["step"] = "当前代理连接失败，正在切换下一条代理"
                            status = "running"
                        task_event(event_index, status, **forwarded)

                proxy = _protocol_proxy_for_task(
                    attempt_proxy_pool,
                    index,
                    attempt,
                    rotate=attempt_rotate or bool(failed_proxies),
                    start_offset=attempt_offset,
                    retry_stride=max(1, conc),
                    excluded=bad_proxy_snapshot(failed_proxies),
                )
                if not proxy and attempt_fallback:
                    proxy = attempt_fallback
                task_runtime = protocol_runtime.with_deadline(
                    time.monotonic() + protocol_runtime.account_timeout
                )
                register_kwargs = {
                    "mail_config": mail_config,
                    "proxy": proxy,
                    "fixed_email": task_email,
                    "provider_type": provider_type,
                    "stop_check": task_stopped,
                    "task_event": attempt_event,
                    "proxy_config": attempt_proxy_cfg,
                    "group_id": task_group_id,
                    "post_register_health_check": post_register_health_check,
                    "protocol_runtime": task_runtime,
                }
                # Do not add a new keyword for legacy integrations unless a
                # sink was explicitly requested.  This keeps monkey-patched
                # register_one implementations source-compatible.
                if callable(account_log_sink):
                    register_kwargs["account_log_sink"] = attempt_account_log_sink
                if mailbox_override is not None:
                    register_kwargs["mailbox_override"] = mailbox_override
                    register_kwargs["provider_type"] = str(mailbox_override.get("provider") or provider_type or "")
                record = await asyncio.to_thread(
                    register_one,
                    index,
                    **register_kwargs,
                )
                if skipped:
                    break
                if str(attempt_error or "").lower().find(PasswordResetRequired.code) >= 0:
                    skipped = True
                    break
                if record:
                    break
                if "registration_recovery_pending" in attempt_error.lower():
                    # Password completion has its own retry budget. Never feed
                    # this already-created identity back into signup retries.
                    break
                if mailbox_override is not None and mailbox_override.get("provider") == "smsbower_mail" and (
                    (mailbox_override.get("_registration_identity_created") and not mailbox_override.get("_registration_recovery_only"))
                    or mailbox_override.get("_smsbower_terminal_status")
                    or mailbox_override.get("_registration_outcome") in {"already_registered", "otp_timeout"}
                ):
                    # Never create/reset a partially-created identity or reuse
                    # a consumed/closed activation as a fresh registration.
                    break
                proxy_failed = is_proxy_transport_error(attempt_error)
                alias_pinned = fixed_alias_batch or paired_alias
                if alias_pinned and not alias_proxy_retryable(attempt_error):
                    break
                if proxy_failed and proxy:
                    failed_proxies.add(proxy)
                    cooled = quarantine_proxy(proxy)
                    print(
                        f"  [协议调度] 任务{index} 本批次冷却代理 "
                        f"{proxy_brief(proxy)} · 原因={attempt_error[:180]} · 冷却数={cooled}"
                    )
                if proxy_failed and attempt < proxy_retry_limit:
                    next_proxy = _protocol_proxy_for_task(
                        attempt_proxy_pool,
                        index,
                        attempt + 1,
                        rotate=True,
                        start_offset=attempt_offset,
                        retry_stride=max(1, conc),
                        excluded=bad_proxy_snapshot(failed_proxies),
                    )
                    print(
                        f"  [协议调度] 任务{index} 当前代理 TLS/连接失败，"
                        f"切换代理重试 {attempt + 2}/{proxy_retry_limit + 1}"
                    )
                    if callable(task_event):
                        task_event(
                            index,
                            "running",
                            group_id=task_group_id,
                            proxy=proxy_brief(next_proxy),
                            step=f"代理连接失败，已切换下一条代理重试 {attempt + 2}/{proxy_retry_limit + 1}",
                        )
                    await asyncio.sleep(0.5)
                    continue
                dead_slot_failure = is_dead_slot_error(attempt_error)
                if attempt >= max_retries and not (
                    dead_slot_failure
                    and dead_slot_switches < dead_slot_switch_limit
                ):
                    break
                if fixed_email is None and not proxy_failed and not paired_alias:
                    replacement = claim_retry_email(task_email)
                    if replacement and replacement != task_email:
                        if dead_slot_failure:
                            dead_slot_switches += 1
                        previous = task_email
                        task_email = replacement
                        print(
                            f"  [协议调度] 任务{index} 邮箱槽位失败，"
                            f"切换备用槽位 {task_email}"
                        )
                        if callable(task_event):
                            task_event(
                                index,
                                "running",
                                group_id=task_group_id,
                                email=task_email,
                                step=(
                                    "邮箱槽位失败，已切换备用别名"
                                    if "+" in task_email.rsplit("@", 1)[0]
                                    else "邮箱槽位失败，已切换备用邮箱"
                                ),
                                previous_email=previous,
                            )
                await asyncio.sleep(1.5)
            results[index - 1] = record
            if not record and mailbox_override is not None and mailbox_override.get("_registration_recovery_pending") and not mailbox_override.get("_registration_recovery_only"):
                return
            if skipped:
                persist_registration_summary(
                    status="skipped",
                    email=task_email or "",
                    account_id=account_id,
                    trace_file=trace_file,
                    step_text="账号已有密码，已跳过",
                    group_id_value=task_group_id,
                )
                return
            if record:
                # ``register_one`` terminal callbacks are attempt-scoped and
                # deliberately withheld from the durable sink. Emit one
                # success marker only after the retry loop has returned a
                # validated record.
                persist_registration_summary(
                    status="success",
                    email=str(record.get("email") or task_email or ""),
                    account_id=str(record.get("account_id") or account_id or ""),
                    trace_file=str(
                        record.get("registration_trace_file") or trace_file or ""
                    ),
                    step_text="注册完成",
                    group_id_value=task_group_id,
                )
                return
            if not record:
                if task_stopped():
                    persist_registration_summary(
                        status="cancelled",
                        email=task_email or "",
                        account_id=account_id,
                        trace_file=trace_file,
                        step_text="已停止",
                        group_id_value=task_group_id,
                    )
                    if callable(task_event):
                        task_event(
                            index,
                            "cancelled",
                            group_id=task_group_id,
                            step="已停止",
                        )
                    return
                if is_proxy_transport_error(last_error):
                    with stats_lock:
                        stats["done"] += 1
                        stats["fail"] += 1
                if callable(task_event):
                    task_event(
                        index,
                        "failed",
                        group_id=task_group_id,
                        error=last_error or "protocol_register_failed",
                        step=(
                            f"代理重试用尽：{last_error[:140]}"
                            if is_proxy_transport_error(last_error)
                            else (last_error[:160] or "协议注册失败")
                        ),
                    )
                persist_registration_summary(
                    status="failed",
                    email=task_email or "",
                    account_id=account_id,
                    trace_file=trace_file,
                    error=last_error or "protocol_register_failed",
                    step_text=(
                        f"代理重试用尽：{last_error[:140]}"
                        if is_proxy_transport_error(last_error)
                        else (last_error[:160] or "协议注册失败")
                    ),
                    group_id_value=task_group_id,
                )

    dynamic_indices = {
        index for index in range(1, n + 1)
        if not schedule_emails[index - 1] and needs_dynamic_mail_coordinator(mail_config, provider_type)
    }
    for wave_number, (indices, is_alias_pair) in enumerate(task_waves, start=1):
        indices = [index for index in indices if index not in dynamic_indices]
        if not indices:
            continue
        if stopped():
            break
        if is_alias_pair and wave_number > 1:
            print(
                f"  [协议调度] 上一双别名已完整结束，启动下一对 "
                f"({indices[0]}-{indices[-1]}/{n})"
            )
        barrier = asyncio.Barrier(2) if is_alias_pair and len(indices) == 2 else None
        await asyncio.gather(
            *(run_task(index, barrier, is_alias_pair) for index in indices)
        )
    if dynamic_indices and not stopped():
        async def run_allocated(index: int, mailbox: dict[str, Any]) -> None:
            await run_task(index, paired_alias=True, mailbox_override=mailbox)

        def emit_dynamic(index: int, status: str, **fields: Any) -> None:
            task_group = str(group_plan[index - 1]) if isinstance(group_plan, list) and index <= len(group_plan) else str(group_id or "")
            if callable(task_event):
                task_event(index, status, group_id=task_group, **fields)
            if status in {"failed", "skipped", "cancelled"}:
                persist_registration_summary(status=status, email=str(fields.get("email") or ""), error=str(fields.get("error") or ""), step_text=str(fields.get("step") or ""), group_id_value=task_group)

        await run_dynamic_mail_groups(sorted(dynamic_indices), concurrency=conc, mail_config=mail_config, provider_type=provider_type,
                                      run_account=run_allocated, result_at=lambda index: results[index - 1], emit=emit_dynamic,
                                      stop_check=stopped, pause_check=paused)
    return [item for item in results if item]




async def run_protocol_login(
    app_config: dict[str, Any],
    *,
    total: int | None = None,
    concurrency: int | None = None,
    fixed_email: str | None = None,
    provider_type: str | None = None,
    stop_check=None,
    cancel_event: asyncio.Event | None = None,
    task_event=None,
) -> list[dict[str, Any]]:
    """Run standalone ChatGPT NextAuth email-code logins."""
    from mail import release_stale_in_use

    reg_cfg = (
        app_config.get("registration")
        if isinstance(app_config.get("registration"), dict)
        else {}
    )
    source_mail = app_config.get("mail") if isinstance(app_config.get("mail"), dict) else {}
    mail_config = dict(source_mail)
    mail_config["wait_timeout"] = max(
        10,
        int(
            reg_cfg.get("protocol_login_otp_timeout")
            or reg_cfg.get("protocol_otp_timeout")
            or source_mail.get("wait_timeout")
            or 90
        ),
    )
    mail_config["wait_chunk"] = max(
        30,
        min(
            45,
            int(
                reg_cfg.get("protocol_login_poll_chunk")
                or reg_cfg.get("protocol_otp_poll_chunk")
                or 45
            ),
        ),
    )
    mail_config["request_timeout"] = max(
        15,
        int(source_mail.get("request_timeout") or 30),
    )
    proxy_cfg = app_config.get("proxy") if isinstance(app_config.get("proxy"), dict) else {}
    stop = stop_check if callable(stop_check) else (lambda: False)
    if not str(fixed_email or "").strip():
        raise ValueError("协议登录必须指定 fixed_email / --email，避免重复登录同一邮箱")
    n = conc = 1
    max_retries = max(0, int(reg_cfg.get("protocol_login_retries") or 1))

    release_stale_in_use()
    proxy_pool = resolve_proxy_pool(proxy_cfg)
    fallback_proxy = (
        normalize_proxy_url(str(proxy_cfg.get("url") or ""))
        if proxy_cfg.get("enabled")
        else ""
    )
    if not proxy_pool and fallback_proxy:
        proxy_pool = [fallback_proxy]
    rotate = bool(proxy_cfg.get("rotate_per_task", True))
    proxy_retry_limit = compute_proxy_retry_limit(max_retries, proxy_pool)
    protocol_runtime = ProtocolRuntimeSettings.from_config(reg_cfg)
    proxy_start_offset = (
        random.SystemRandom().randrange(len(proxy_pool))
        if rotate and proxy_pool
        else 0
    )

    print(f"  GPT 协议登录  总数={n} 并发={conc}")
    semaphore = asyncio.Semaphore(conc)
    results: list[dict[str, Any] | None] = [None] * n

    async def run_task(index: int) -> None:
        async with semaphore:
            if stop() or (cancel_event is not None and cancel_event.is_set()):
                if callable(task_event):
                    task_event(index, "cancelled", step="已跳过")
                return
            record = None
            last_error = ""
            failed_proxies: set[str] = set()

            def capture_event(_index, _status=None, **fields):
                nonlocal last_error
                detail = str(fields.get("error") or "").strip()
                if detail:
                    last_error = detail
                if callable(task_event):
                    forwarded = dict(fields)
                    transport_error = is_proxy_transport_error(detail)
                    if transport_error:
                        forwarded.pop("error", None)
                        forwarded["step"] = "当前代理连接失败，正在切换下一条代理"
                        _status = "running"
                    forwarded.setdefault("email", str(fixed_email or "").strip())
                    task_event(_index, _status, **forwarded)

            for attempt in range(proxy_retry_limit + 1):
                if stop() or (cancel_event is not None and cancel_event.is_set()):
                    break
                if failed_proxies and len(failed_proxies) >= len(set(proxy_pool)):
                    break
                proxy = _protocol_proxy_for_task(
                    proxy_pool,
                    index,
                    attempt,
                    rotate=rotate or bool(failed_proxies),
                    start_offset=proxy_start_offset,
                    excluded=failed_proxies,
                )
                task_runtime = protocol_runtime.with_deadline(
                    time.monotonic() + protocol_runtime.account_timeout
                )
                record = await asyncio.to_thread(
                    login_one,
                    index,
                    mail_config=mail_config,
                    proxy=proxy,
                    fixed_email=fixed_email,
                    provider_type=provider_type,
                    stop_check=stop,
                    task_event=capture_event,
                    error_sink=lambda detail: capture_event(index, "running", error=detail),
                    proxy_config=proxy_cfg,
                    protocol_runtime=task_runtime,
                )
                if record:
                    break
                if "account_deactivated" in last_error.lower():
                    break
                transport_error = is_proxy_transport_error(last_error)
                if transport_error and proxy:
                    failed_proxies.add(proxy)
                if transport_error and proxy and attempt < proxy_retry_limit:
                    if len(failed_proxies) >= len(set(proxy_pool)):
                        break
                    next_proxy = _protocol_proxy_for_task(
                        proxy_pool,
                        index,
                        attempt + 1,
                        rotate=True,
                        start_offset=proxy_start_offset,
                        excluded=failed_proxies,
                    )
                    print(
                        f"  [协议调度] 登录任务{index} 当前代理 TLS/连接失败，"
                        f"切换代理重试 {attempt + 2}/{proxy_retry_limit + 1}"
                    )
                    if callable(task_event):
                        task_event(
                            index,
                            "running",
                            email=str(fixed_email or "").strip(),
                            proxy=proxy_brief(next_proxy),
                            step=f"代理连接失败，已切换下一条代理重试 {attempt + 2}/{proxy_retry_limit + 1}",
                        )
                    await asyncio.sleep(0.5)
                    continue
                if attempt < max_retries:
                    await asyncio.sleep(1.5)
                else:
                    break
            results[index - 1] = record
            if not record and callable(task_event):
                task_event(
                    index,
                    "failed",
                    email=str(fixed_email or "").strip(),
                    error=last_error or "protocol_login_failed",
                    step=(
                        f"代理重试用尽：{last_error[:160]}"
                        if is_proxy_transport_error(last_error)
                        else (last_error[:180] or "协议登录失败")
                    ),
                )

    await asyncio.gather(*(run_task(i) for i in range(1, n + 1)))
    return [item for item in results if item]




def _protocol_mailbox_provider_map(mail_config: dict[str, Any]) -> dict[str, str]:
    """Index configured mailbox addresses so batch login can preflight them."""
    result: dict[str, str] = {}
    providers = mail_config.get("providers")
    if not isinstance(providers, list):
        return result
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        provider_type = normalize_provider_type(str(provider.get("type") or ""))
        if provider_type not in CANONICAL_PROVIDER_TYPES:
            continue
        lines = str(provider.get("mailboxes") or provider.get("pool") or "")
        if provider_type == "maddy_webui":
            parser = parse_domain_mailboxes
        else:
            parser = parse_icloud_credentials if provider_type == "icloud_api" else parse_outlook_credentials
        try:
            entries = parser(lines)
        except Exception:
            continue
        for entry in entries:
            email = str(entry.get("email") or "").strip().lower()
            if email:
                result.setdefault(email, provider_type)
    return result




def _protocol_relogin_candidates(
    accounts: list[dict[str, Any]],
    mailbox_providers: dict[str, str],
) -> tuple[list[tuple[dict[str, Any], str]], dict[str, int]]:
    """Select accounts with both stored credentials for direct protocol login."""
    from common.account_store import normalized_health_status

    candidates: list[tuple[dict[str, Any], str]] = []
    skipped = {
        "healthy_transient_error": 0,
        "account_deactivated": 0,
        "mailbox_missing": 0,
        "credentials_missing": 0,
        "unsupported_state": 0,
    }
    for account in accounts:
        state = normalized_health_status(account)
        account_status = str(account.get("status") or "").strip()
        email = str(account.get("email") or "").strip().lower()
        blocked = str(account.get("reauth_blocked_reason") or "").strip().lower()
        health_error = str(account.get("health_error") or "").strip().lower()
        if blocked == "account_deactivated" or "account_deactivated" in health_error:
            skipped["account_deactivated"] += 1
            continue

        needs_login = state in {"invalid", "reauth_required"}
        # A normal account with a transport error needs a health retry later,
        # not an OTP login that would rotate a valid session unnecessarily.
        if state == "error":
            needs_login = account_status == "异常"
            if not needs_login:
                skipped["healthy_transient_error"] += 1
        if not needs_login:
            skipped["unsupported_state"] += 1
            continue
        provider_type = mailbox_providers.get(
            email,
            str(account.get("provider_type") or "").strip(),
        )
        if not email or not str(account.get("password") or "").strip() or not str(account.get("totp_secret") or "").strip():
            skipped["credentials_missing"] += 1
            continue
        candidates.append((account, provider_type or ""))
    return candidates, skipped




async def run_protocol_relogin(
    app_config: dict[str, Any],
    *,
    total: int | None = None,
    concurrency: int | None = None,
    stop_check=None,
    cancel_event: asyncio.Event | None = None,
    task_event=None,
) -> dict[str, Any]:
    """Re-login all locally recorded accounts requiring a new session."""
    from common.account_store import load_accounts, upsert_account_record
    from mail import release_stale_in_use

    source_mail = app_config.get("mail") if isinstance(app_config.get("mail"), dict) else {}
    mail_config = dict(source_mail)
    reg_cfg = app_config.get("registration") if isinstance(app_config.get("registration"), dict) else {}
    mail_config["wait_timeout"] = max(
        10,
        int(
            reg_cfg.get("protocol_login_otp_timeout")
            or reg_cfg.get("protocol_otp_timeout")
            or source_mail.get("wait_timeout")
            or 90
        ),
    )
    mail_config["wait_chunk"] = max(
        30,
        min(
            45,
            int(
                reg_cfg.get("protocol_login_poll_chunk")
                or reg_cfg.get("protocol_otp_poll_chunk")
                or 45
            ),
        ),
    )
    mail_config["request_timeout"] = max(
        15,
        int(source_mail.get("request_timeout") or 30),
    )
    proxy_cfg = app_config.get("proxy") if isinstance(app_config.get("proxy"), dict) else {}
    stop = stop_check if callable(stop_check) else (lambda: False)
    release_stale_in_use()

    mailbox_providers = _protocol_mailbox_provider_map(mail_config)
    candidates, skipped = _protocol_relogin_candidates(load_accounts(), mailbox_providers)
    if total is not None:
        candidates = candidates[: max(0, int(total))]

    proxy_pool = resolve_proxy_pool(proxy_cfg)
    fallback_proxy = (
        normalize_proxy_url(str(proxy_cfg.get("url") or ""))
        if proxy_cfg.get("enabled")
        else ""
    )
    if not proxy_pool and fallback_proxy:
        proxy_pool = [fallback_proxy]
    rotate = bool(proxy_cfg.get("rotate_per_task", True))
    protocol_runtime = ProtocolRuntimeSettings.from_config(reg_cfg)
    proxy_start_offset = (
        random.SystemRandom().randrange(len(proxy_pool))
        if rotate and proxy_pool
        else 0
    )
    max_concurrency = max(1, int(concurrency if concurrency is not None else 1))
    max_retries = max(0, int(reg_cfg.get("protocol_login_retries") or 1))
    proxy_retry_limit = compute_proxy_retry_limit(max_retries, proxy_pool)
    results: dict[str, Any] = {
        "selected": len(candidates),
        "attempted": 0,
        "succeeded": 0,
        "failed": 0,
        "skipped": skipped,
        "success_emails": [],
        "failed_emails": [],
    }
    if not candidates:
        print("  GPT 协议批量重登录：没有匹配到待处理账号")
        return results

    print(
        f"  GPT 协议批量重登录  总数={len(candidates)} "
        f"并发={max_concurrency} 重试={max_retries}"
    )
    semaphore = asyncio.Semaphore(max_concurrency)
    result_lock = asyncio.Lock()

    async def run_task(index: int, account: dict[str, Any], provider_type: str) -> None:
        email = str(account.get("email") or "").strip()
        async with semaphore:
            async with result_lock:
                results["attempted"] += 1
            if stop() or (cancel_event is not None and cancel_event.is_set()):
                async with result_lock:
                    results["failed"] += 1
                    results["failed_emails"].append(email)
                if callable(task_event):
                    task_event(index, "cancelled", email=email, step="已跳过")
                return

            last_error = ""

            def capture_event(_index, _status=None, **fields):
                nonlocal last_error
                detail = str(fields.get("error") or "").strip()
                if detail and len(detail) >= len(last_error):
                    last_error = detail
                if callable(task_event):
                    forwarded = dict(fields)
                    if is_proxy_transport_error(detail):
                        forwarded.pop("error", None)
                        forwarded["step"] = "当前代理连接失败，正在切换下一条代理"
                        _status = "running"
                    forwarded.setdefault("email", email)
                    task_event(_index, _status, **forwarded)

            record = None
            failed_proxies: set[str] = set()

            for attempt in range(proxy_retry_limit + 1):
                if stop() or (cancel_event is not None and cancel_event.is_set()):
                    break
                if failed_proxies and len(failed_proxies) >= len(set(proxy_pool)):
                    break
                proxy = _protocol_proxy_for_task(
                    proxy_pool,
                    index,
                    attempt,
                    rotate=rotate or bool(failed_proxies),
                    start_offset=proxy_start_offset,
                    excluded=failed_proxies,
                )
                task_runtime = protocol_runtime.with_deadline(
                    time.monotonic() + protocol_runtime.account_timeout
                )
                record = await asyncio.to_thread(
                    login_with_password_totp_one,
                    index,
                    email=email,
                    password=str(account.get("password") or "").strip(),
                    totp_secret=str(account.get("totp_secret") or "").strip(),
                    proxy=proxy,
                    stop_check=stop,
                    task_event=capture_event,
                    error_sink=lambda detail: capture_event(index, "failed", error=detail),
                    proxy_config=proxy_cfg,
                    protocol_runtime=task_runtime,
                )
                if record:
                    break
                if "account_deactivated" in last_error.lower():
                    break
                transport_error = is_proxy_transport_error(last_error)
                if transport_error and proxy:
                    failed_proxies.add(proxy)
                if transport_error and proxy and attempt < proxy_retry_limit:
                    if len(failed_proxies) >= len(set(proxy_pool)):
                        break
                    next_proxy = _protocol_proxy_for_task(
                        proxy_pool,
                        index,
                        attempt + 1,
                        rotate=True,
                        start_offset=proxy_start_offset,
                        excluded=failed_proxies,
                    )
                    if callable(task_event):
                        task_event(
                            index,
                            "running",
                            email=email,
                            proxy=proxy_brief(next_proxy),
                            step=f"代理连接失败，已切换下一条代理重试 {attempt + 2}/{proxy_retry_limit + 1}",
                        )
                    await asyncio.sleep(0.5)
                    continue
                if attempt < max_retries:
                    await asyncio.sleep(1.5)
                else:
                    break

            async with result_lock:
                deactivated = "account_deactivated" in last_error.lower()
                if deactivated:
                    skipped["account_deactivated"] += 1
                elif record:
                    results["succeeded"] += 1
                    results["success_emails"].append(email)
                else:
                    results["failed"] += 1
                    results["failed_emails"].append(email)
            if deactivated:
                upsert_account_record(
                    {
                        **account,
                        "status": "已封号",
                        "health_status": "invalid",
                        "health_checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        "health_error": "account_deactivated: account has been deleted or deactivated",
                        "reauth_blocked_reason": "account_deactivated",
                        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    }
                )
                log(f"{email} 已标记为已封号，跳过后续重试", "yellow")
            if not record and last_error:
                if not deactivated:
                    log(f"{email} 批量重登录最终失败: {last_error[:240]}", "red")
            if not record and callable(task_event):
                task_event(
                    index,
                    "failed",
                    email=email,
                    error=last_error or "protocol_relogin_failed",
                    step=(
                        f"代理重试用尽：{last_error[:160]}"
                        if is_proxy_transport_error(last_error)
                        else (last_error[:180] or "协议重登录失败")
                    ),
                )

    await asyncio.gather(
        *(run_task(index, account, provider_type) for index, (account, provider_type) in enumerate(candidates, 1))
    )
    print(
        "  GPT 协议批量重登录完成 "
        f"selected={results['selected']} attempted={results['attempted']} "
        f"succeeded={results['succeeded']} failed={results['failed']} "
        f"deactivated={skipped['account_deactivated']} credentials_missing={skipped['credentials_missing']}"
    )
    return results

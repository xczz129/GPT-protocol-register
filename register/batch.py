# -*- coding: utf-8 -*-
"""Batch registration scheduler."""

from __future__ import annotations

import asyncio
import random
import re
import sys
import threading
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.chatgpt2api import c2a_config_from
from common.account_store import ACCOUNTS_FILE, upsert_account_record
from common.account_operation_log import record_operation_event
from mail import count_pool_capacity, release_stale_in_use
from proxy_util import (
    compute_proxy_retry_limit,
    is_proxy_transport_error,
    normalize_proxy_url,
    proxy_brief,
    resolve_proxy_pool,
)
from register.sessions import JobCancelled, _default_stop_check
from register.scheduling import build_registration_waves
from register.mail_channel_options import (
    is_smsbower_selected,
    needs_dynamic_mail_coordinator,
    smsbower_child_enabled,
    validate_smsbower_job,
)
from register.smsbower_batch import run_dynamic_mail_groups
from register.flow import register_one
from register.protocol import repair_security_one
from register.protocol_runtime import ProtocolRuntimeSettings
from register.util import REGISTER_TIMEOUT


def _truthy(value, fallback: bool = False) -> bool:
    """Robust bool parsing for config values (bool / str / int)."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", "", "none"}:
        return False
    return fallback


def _proxy_for_task(
    proxy_pool: list[str],
    fallback: str,
    task_index: int,
    attempt: int = 0,
    rotate: bool = True,
    start_offset: int = 0,
    attempt_span: int = 1,
    excluded: set[str] | None = None,
) -> str:
    if not proxy_pool:
        return normalize_proxy_url(fallback)
    if not rotate and not excluded:
        return proxy_pool[0]
    blocked = excluded or set()
    pool_size = len(proxy_pool)
    # Keep task placement stable across retries. This makes a failed proxy
    # predictable, allows the next slot to be selected deterministically, and
    # gives start_offset/attempt_span real meaning instead of leaving them as
    # dead scheduling parameters.
    task_slot = max(0, int(task_index) - 1)
    retry_slot = max(0, int(attempt))
    stride = max(1, int(attempt_span))
    start = (int(start_offset) + task_slot * stride + retry_slot) % pool_size
    for offset in range(pool_size):
        candidate = proxy_pool[(start + offset) % pool_size]
        if candidate not in blocked:
            return candidate
    # If every proxy is blocked, preserve forward progress and let the caller
    # decide whether the resulting attempt should quarantine the pool.
    return proxy_pool[start]


def _registration_operation_log_sink(event: dict[str, Any]) -> None:
    """Persist a redacted protocol-registration event.

    The protocol layer deliberately exposes only a small event projection.
    Keep the adapter equally strict: proxy details and arbitrary provider
    payloads belong in the transient task log, not in the durable account
    history.  ``record_operation_event`` is best-effort by design, so a
    logging outage never changes registration semantics.
    """
    if not isinstance(event, dict):
        return
    email = str(event.get("email") or "").strip()
    account_id = str(event.get("account_id") or "").strip()
    if not email and not account_id:
        return
    metadata: dict[str, Any] = {}
    for key in ("index", "group_id", "skip_reason", "attempt", "trace_file"):
        value = event.get(key)
        if value not in (None, ""):
            metadata[key] = value
    status = str(event.get("status") or "info").strip().lower()
    # Registration providers occasionally echo submitted credentials in an
    # exception body.  The live task callback still receives that diagnostic,
    # but the durable audit row gets only a stable, credential-free code.
    raw_detail = str(event.get("error") or event.get("step") or "").strip()
    safe_error = _registration_error_code(raw_detail) if status in {
        "failed",
        "error",
    } else ""
    safe_step = (
        "注册任务开始"
        if status == "running"
        else "注册完成"
        if status in {"success", "complete", "completed"}
        else "注册已跳过"
        if status == "skipped"
        else "注册任务已停止"
        if status == "cancelled"
        else ""
    )
    safe_message = safe_step
    record_operation_event(
        {"email": email, "account_id": account_id},
        "registration",
        str(event.get("status") or "info"),
        message=safe_message,
        step=safe_step,
        error=safe_error,
        metadata=metadata,
        at=str(event.get("at") or "") or None,
    )


_REGISTRATION_ERROR_MARKERS = (
    "password_reset_required",
    "account_deactivated",
    "rate_limit",
    "invalid_auth_step",
    "invalid_state",
    "cf_blocked",
    "cloudflare",
    "proxy",
    "timeout",
    "connection",
    "tls",
    "curl",
    "oauth",
    "otp",
    "totp",
    "mfa",
    "email",
    "mail",
    "protocol",
    "registration",
)


def _registration_error_code(detail: str) -> str:
    """Map arbitrary provider diagnostics to a bounded safe error code."""
    text = str(detail or "").strip().lower()
    for marker in _REGISTRATION_ERROR_MARKERS:
        if marker in text:
            return marker
    # Preserve a short HTTP status class without retaining response bodies.
    match = re.search(r"\b(?:http\s*)?(4\d\d|5\d\d)\b", text)
    if match:
        return f"http_{match.group(1)}"
    return "registration_failed"


def _registration_operation_log_sink_for_batch():
    """Return a batch-scoped sink that keeps only useful terminal events.

    ``register_one`` emits detailed step callbacks for the live task panel.
    Persisting every one of those callbacks makes registration I/O contend on
    the audit database.  Keep the first ``running`` marker and the first
    terminal status per account; the live log and registration trace retain
    the detailed timeline.
    """
    lock = threading.Lock()
    aliases: dict[str, str] = {}
    states: dict[str, set[str]] = {}
    terminal = {"success", "failed", "cancelled", "skipped", "error", "complete", "completed"}

    def sink(event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            return
        email = str(event.get("email") or "").strip().lower()
        account_id = str(event.get("account_id") or "").strip().lower()
        if not email and not account_id:
            return
        with lock:
            keys = [aliases.get(value, value) for value in (email, account_id) if value]
            canonical = next((value for value in keys if value), "")
            if not canonical:
                return
            # Bridge the email-only start event to the account-id completion.
            for value in keys:
                aliases[value] = canonical
            current = states.setdefault(canonical, set())
            status = str(event.get("status") or "info").strip().lower()
            if status == "running":
                if "running" in current:
                    return
                current.add("running")
            elif status in terminal:
                if "terminal" in current:
                    return
                current.add("terminal")
            else:
                # Informational/step callbacks are intentionally transient.
                return
        _registration_operation_log_sink(event)

    return sink


async def run_register(
    config: dict[str, Any],
    *,
    total: int | None = None,
    concurrency: int | None = None,
    keep_on_fail: bool = False,
    fixed_email: str | None = None,
    provider_type: str | None = None,
    stop_check=None,
    pause_check=None,
    cancel_event: asyncio.Event | None = None,
    task_event=None,
    account_log_sink=None,
    group_id: str = "",
    group_plan: list[str] | None = None,
    proxy_config_loader=None,
) -> list[dict[str, Any]]:
    """Run GPT registration batch. Returns successful account records.

    provider_type: "outlook" | "icloud" | "auto"/None — which mailbox pool to use.
    stop_check: optional callable returning True when job should abort ASAP.
    cancel_event: optional asyncio.Event set when stop is requested (wakes waits).
    """
    _stop = stop_check if callable(stop_check) else _default_stop_check
    _paused = pause_check if callable(pause_check) else (lambda: False)

    reg_cfg = (
        config.get("registration")
        if isinstance(config.get("registration"), dict)
        else {}
    )
    engine = str(reg_cfg.get("engine") or "protocol").strip().lower()
    if engine in {"protocol", "api", "http"}:
        from register.protocol import run_protocol_register

        return await run_protocol_register(
            config,
            total=total,
            concurrency=concurrency,
            fixed_email=fixed_email,
            provider_type=provider_type,
            stop_check=stop_check,
            pause_check=pause_check,
            cancel_event=cancel_event,
            task_event=task_event,
            account_log_sink=(
                account_log_sink
                if callable(account_log_sink)
                else _registration_operation_log_sink_for_batch()
            ),
            group_id=group_id,
            group_plan=group_plan,
            proxy_config_loader=proxy_config_loader,
        )
    hybrid_mode = engine in {"hybrid", "auto_hybrid"}
    post_security = engine == "browser" and _truthy(
        reg_cfg.get("browser_protocol_security_after_register")
    )
    proxy_cfg = config.get("proxy") if isinstance(config.get("proxy"), dict) else {}
    mail_config = config.get("mail") if isinstance(config.get("mail"), dict) else {}
    c2a_cfg = c2a_config_from(config)

    n = int(total if total is not None else reg_cfg.get("total") or 1)
    conc = int(
        concurrency if concurrency is not None else reg_cfg.get("threads") or 1
    )
    conc = max(1, conc)
    validate_smsbower_job(mail_config, provider_type, n, conc, fixed_email=fixed_email)
    timeout = int(reg_cfg.get("timeout") or REGISTER_TIMEOUT)
    max_retries = int(reg_cfg.get("max_transient_retries") or 2)

    proxy_enabled = bool(proxy_cfg.get("enabled"))
    proxy_pool: list[str] = []
    fallback_proxy = normalize_proxy_url(str(proxy_cfg.get("url") or ""))
    if proxy_enabled:
        proxy_pool = resolve_proxy_pool(proxy_cfg)
        if not proxy_pool:
            raise RuntimeError(
                "代理已启用，但当前模式没有可用代理；请检查 proxy.pool_file，"
                "任务已停止且不会无代理直连"
            )
    rotate = bool(proxy_cfg.get("rotate_per_task", True))
    proxy_start_offset = (
        random.SystemRandom().randrange(len(proxy_pool))
        if rotate and len(proxy_pool) > 1
        else 0
    )
    proxy_retry_limit = compute_proxy_retry_limit(max_retries, proxy_pool)
    proxy_attempt_span = max(1, proxy_retry_limit + 1)
    batch_bad_proxies: set[str] = set()

    mail_type = (provider_type or "auto").strip() or "auto"

    try:
        freed = release_stale_in_use()
        if freed:
            print(f"  [mail] 启动前释放 {freed} 个过期 in_use")
    except Exception:
        pass

    pool_info = count_pool_capacity(
        mail_config,
        provider_type=provider_type if mail_type not in {"auto", "", "any", "all"} else None,
        fixed_email=fixed_email,
        fixed_alias_count=1 if fixed_email else None,
    )
    available = int(pool_info.get("available") or 0)
    pool_total = int(pool_info.get("total") or 0)
    available_emails = [
        str(email or "").strip().lower()
        for email in (pool_info.get("emails") or [])
        if str(email or "").strip()
    ]
    fixed_alias_batch = bool(
        fixed_email
        and str(pool_info.get("fixed_email_mode") or "") == "alias_batch"
    )

    if fixed_alias_batch:
        n = min(1, available)
        conc = 1
        print(
            f"  [调度] 指定主邮箱别名批次 → "
            f"单别名启动 总数={n} 并发={conc}"
        )
    elif fixed_email:
        if available < 1 and pool_total >= 1:
            print(
                f"  [mail] 指定邮箱 {fixed_email} 当前不可用 "
                f"(used={pool_info.get('used')} in_use={pool_info.get('in_use')} "
                f"failed={pool_info.get('failed')})"
            )
        n = 1
        conc = 1
        print("  [调度] 指定邮箱模式 → 强制 总数=1 并发=1")
    elif available <= 0:
        print(
            f"  [mail] 无可用邮箱 (类型={mail_type} 池内={pool_total} "
            f"used={pool_info.get('used')} in_use={pool_info.get('in_use')} "
            f"failed={pool_info.get('failed')} invalid={pool_info.get('token_invalid')})"
        )
        print("  [调度] 可用=0 → 跳过全部任务")
        return []
    else:
        orig_n, orig_conc = n, conc
        n = max(1, min(n, available))
        conc = max(1, min(conc, available, n))
        if n != orig_n or conc != orig_conc:
            print(
                f"  [调度] 按可用邮箱自动调整: "
                f"总数 {orig_n}→{n}  并发 {orig_conc}→{conc}  "
                f"(可用邮箱={available}/{pool_total})"
            )
        else:
            print(f"  [调度] 可用邮箱={available}/{pool_total}  总数={n}  并发={conc}")

    print("=" * 50)
    if not fixed_email and is_smsbower_selected(provider_type):
        if smsbower_child_enabled(mail_config):
            n -= n % 2
            if n < 2:
                return []
        conc = max(2, conc - conc % 2)
    engine_label = "GPT 会话内混合注册" if hybrid_mode else "GPT 全程指纹浏览器注册"
    print(f"  {engine_label}  总数={n} 并发={conc}  邮箱类型={mail_type}")
    if hybrid_mode:
        print("  流程=浏览器过 CF/持有会话 → 协议提交邮箱/校验 OTP → 浏览器完成资料/OAuth")
    elif post_security:
        print("  流程=浏览器完成注册并退出 → 协议设置密码 → 协议启用并复核 TOTP 2FA")
    else:
        print("  流程=浏览器 UI 优先；跳转卡住时由当前会话接管认证；邮箱 API 用于收信")
    print(f"  代理池={len(proxy_pool)} 轮换={rotate}")
    if rotate and proxy_pool:
        print(
            f"  代理调度=起始槽 {proxy_start_offset + 1}/{len(proxy_pool)} "
            f"· 任务跨度={proxy_attempt_span}"
        )
    if c2a_cfg.get("enabled") and c2a_cfg.get("auto_upload", True):
        print(f"  chatgpt2api 自动上传: {c2a_cfg.get('url') or '(未配置 URL)'}")
    elif c2a_cfg.get("enabled"):
        print("  chatgpt2api: 已启用，自动上传关闭")
    print("=" * 50)

    results: list[dict[str, Any] | None] = [None] * n
    global_rate_limited = asyncio.Event()
    # Pin one mailbox to each task for the lifetime of the batch. A transient
    # failure releases the persistent mailbox state, so allocating afresh on
    # every task would repeatedly select the first address when concurrency=1.
    planned_emails = available_emails[:n] if (not fixed_email or fixed_alias_batch) else []
    if fixed_email and not fixed_alias_batch:
        schedule_emails = [str(fixed_email)] * n
    else:
        # Dynamic providers contribute capacity without returning addresses up
        # front. Keep one empty slot for every such task so create_mailbox()
        # can allocate it at execution time.
        schedule_emails = planned_emails + [""] * (n - len(planned_emails))
    task_waves = build_registration_waves(
        schedule_emails,
        conc,
        fixed_alias_batch=fixed_alias_batch,
    )
    has_alias_pairs = any(is_pair for _indices, is_pair in task_waves)
    sem = asyncio.Semaphore(max(conc, 2 if has_alias_pairs else 1))
    if has_alias_pairs and not fixed_alias_batch:
        print("  [调度] 双别名按主邮箱分波执行；每对完整注册结束后再启动下一对")

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

    def emit_task(index: int, status: str | None = None, **fields: Any) -> None:
        if callable(task_event):
            try:
                task_event(index, status, **fields)
            except Exception:
                pass

    if has_alias_pairs and not fixed_alias_batch:
        first_wave = set(task_waves[0][0]) if task_waves else set()
        for task_index, email in enumerate(schedule_emails, start=1):
            if task_index in first_wave:
                continue
            task_group_id = (
                str(group_plan[task_index - 1] or "").strip()
                if isinstance(group_plan, list) and task_index <= len(group_plan)
                else str(group_id or "").strip()
            )
            emit_task(
                task_index,
                "queued",
                email=email,
                group_id=task_group_id,
                step="等待前一双别名完整注册",
            )

    async def _sleep_or_stop(seconds: float) -> None:
        end = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < end:
            if _stop() or (cancel_event is not None and cancel_event.is_set()):
                raise JobCancelled("stop requested")
            await asyncio.sleep(min(0.25, end - time.monotonic()))

    async def _wait_if_paused(index: int, stop_override=None) -> None:
        logged = False
        while _paused():
            if (callable(stop_override) and stop_override()) or _stop() or (cancel_event is not None and cancel_event.is_set()):
                raise JobCancelled("stop requested")
            if not logged:
                emit_task(index, "paused", step="任务派发已暂停")
                logged = True
            await asyncio.sleep(0.25)
        if logged:
            emit_task(index, "queued", step="任务派发已继续")

    # Keep the existing scope while each registration attempt owns its own
    # Playwright driver through open_and_connect(p=None).
    async with nullcontext(None) as p:
        async def run_one(
            i: int,
            start_barrier: asyncio.Barrier | None = None,
            paired_alias: bool = False,
            mailbox_override: dict[str, Any] | None = None,
        ):
            async with sem:
                cancellation = (mailbox_override or {}).get("_registration_cancel_event")
                def task_stopped() -> bool:
                    return bool(_stop()) or bool(cancel_event is not None and cancel_event.is_set()) or bool(cancellation is not None and cancellation.is_set())
                task_group_id = (
                    str(group_plan[i - 1] or "").strip()
                    if isinstance(group_plan, list) and i <= len(group_plan)
                    else str(group_id or "").strip()
                )
                task_email = schedule_emails[i - 1]
                if mailbox_override is not None:
                    task_email = str(mailbox_override.get("address") or "")
                try:
                    await _wait_if_paused(i, task_stopped)
                except JobCancelled:
                    emit_task(i, "cancelled", step="暂停期间收到停止请求")
                    return
                if start_barrier is not None:
                    emit_task(
                        i,
                        "queued",
                        group_id=task_group_id,
                        email=task_email,
                        step="双别名已就绪，等待同步启动",
                    )
                    try:
                        await asyncio.wait_for(start_barrier.wait(), timeout=3.0)
                    except (asyncio.TimeoutError, asyncio.BrokenBarrierError):
                        if _stop() or (
                            cancel_event is not None and cancel_event.is_set()
                        ):
                            emit_task(i, "cancelled", step="同步启动前已停止")
                            return
                        raise RuntimeError("dual_alias_start_barrier_timeout")
                if global_rate_limited.is_set():
                    print(f"  #{i} 已跳过（当前单代理被认证服务限频）")
                    emit_task(i, "failed", group_id=task_group_id, step="当前代理被限频，已跳过")
                    return
                emit_task(i, "queued", group_id=task_group_id, step="等待执行")
                if task_stopped():
                    print(f"  #{i} 已跳过（已请求停止）")
                    emit_task(i, "cancelled", step="已跳过（已请求停止）")
                    return
                if i > 1 and not fixed_alias_batch and not paired_alias:
                    try:
                        await _sleep_or_stop(random.uniform(0.15, 0.5) * min(i - 1, 2))  # nosemgrep: gitlab.bandit.B311
                    except JobCancelled:
                        print(f"  #{i} 已跳过（已请求停止）")
                        emit_task(i, "cancelled", step="已跳过（已请求停止）")
                        return
                record = None
                skipped = False
                security_completion_failed = False
                for attempt in range(proxy_retry_limit + 1):
                    try:
                        await _wait_if_paused(i, task_stopped)
                    except JobCancelled:
                        emit_task(i, "cancelled", step="暂停期间收到停止请求")
                        break
                    if task_stopped():
                        print(f"  #{i} 停止于第 {attempt + 1} 次尝试前")
                        emit_task(i, "cancelled", step=f"停止于第 {attempt + 1} 次尝试前")
                        break
                    proxy = (
                        _proxy_for_task(
                            proxy_pool,
                            fallback_proxy,
                            i,
                            attempt,
                            rotate,
                            proxy_start_offset,
                            proxy_attempt_span,
                            batch_bad_proxies,
                        )
                        if proxy_enabled
                        else ""
                    )
                    attempt_error = ""

                    def attempt_event(
                        event_index: int, status: str | None = None, **fields: Any
                    ) -> None:
                        nonlocal attempt_error, skipped
                        if status == "skipped":
                            skipped = True
                        detail = str(fields.get("error") or "").strip()
                        if detail:
                            attempt_error = detail.lower()
                        transport_error = is_proxy_transport_error(detail)
                        if callable(task_event):
                            forwarded = dict(fields)
                            if transport_error:
                                forwarded.pop("error", None)
                                forwarded["step"] = "当前代理连接失败，正在切换下一条代理"
                                status = "running"
                            task_event(event_index, status, **forwarded)

                    try:
                        emit_task(
                            i,
                            "running",
                            attempt=attempt + 1,
                            group_id=task_group_id,
                            proxy=proxy_brief(proxy),
                            step=f"开始第 {attempt + 1} 次尝试",
                        )
                        record = await register_one(
                            i,
                            n,
                            # Each attempt owns an independent Playwright driver.
                            # A fatal driver crash must not poison sibling tasks.
                            None,
                            mail_config=mail_config,
                            proxy=proxy,
                            fixed_email=task_email,
                            provider_type=provider_type,
                            timeout=timeout,
                            keep_on_fail=keep_on_fail,
                            stop_check=task_stopped,
                            task_event=attempt_event,
                            c2a_config=c2a_cfg,
                            group_id=task_group_id,
                            hybrid_mode=hybrid_mode,
                            defer_success_event=post_security,
                            **({"mailbox_override": mailbox_override} if mailbox_override is not None else {}),
                        )
                        if record and post_security:
                            email_value = str(record.get("email") or "").strip()
                            record["registration_state"] = "security_pending"
                            upsert_account_record(dict(record), ACCOUNTS_FILE)
                            emit_task(
                                i,
                                "running",
                                email=email_value or None,
                                group_id=task_group_id,
                                step="浏览器已退出，开始协议设置密码和 TOTP 2FA",
                            )

                            def persist_security_partial(update: dict[str, Any]) -> None:
                                if not isinstance(update, dict):
                                    return
                                record.update(update)
                                record["registration_state"] = "security_pending"
                                record["updated_at"] = datetime.now(
                                    timezone.utc
                                ).isoformat().replace("+00:00", "Z")
                                upsert_account_record(dict(record), ACCOUNTS_FILE)

                            runtime = ProtocolRuntimeSettings.from_config(reg_cfg)
                            runtime = runtime.with_deadline(
                                time.monotonic() + runtime.account_timeout
                            )
                            try:
                                security_fields = await asyncio.to_thread(
                                    repair_security_one,
                                    i,
                                    account=dict(record),
                                    mail_config=mail_config,
                                    proxy=proxy,
                                    provider_type=(
                                        str(record.get("provider_type") or "").strip()
                                        or provider_type
                                    ),
                                    stop_check=task_stopped,
                                    task_event=attempt_event,
                                    proxy_config=proxy_cfg,
                                    partial_sink=persist_security_partial,
                                    protocol_runtime=runtime,
                                    **({"mailbox_override": mailbox_override} if mailbox_override is not None else {}),
                                )
                                record.update(security_fields)
                                security_missing = []
                                if not str(record.get("password") or "").strip():
                                    security_missing.append("密码")
                                if record.get("password_set") is not True:
                                    security_missing.append("密码设置状态")
                                if not str(record.get("totp_secret") or "").strip():
                                    security_missing.append("TOTP 密钥")
                                if record.get("mfa_enabled") is not True:
                                    security_missing.append("2FA 启用状态")
                                if security_missing:
                                    raise RuntimeError(
                                        "browser_protocol_security_incomplete: "
                                        + ", ".join(security_missing)
                                    )
                                record["registration_state"] = "complete"
                                record["security_completion_error"] = ""
                                record["security_completion_failed_at"] = ""
                                record["updated_at"] = datetime.now(
                                    timezone.utc
                                ).isoformat().replace("+00:00", "Z")
                                record = upsert_account_record(
                                    dict(record), ACCOUNTS_FILE
                                )
                            except (JobCancelled, asyncio.CancelledError):
                                raise
                            except Exception as security_error:
                                security_completion_failed = True
                                attempt_error = str(security_error).lower()
                                record["registration_state"] = "security_pending"
                                record["security_completion_error"] = str(
                                    security_error
                                )[:300]
                                record["security_completion_failed_at"] = datetime.now(
                                    timezone.utc
                                ).isoformat().replace("+00:00", "Z")
                                upsert_account_record(dict(record), ACCOUNTS_FILE)
                                print(
                                    f"  #{i} 浏览器注册成功，安全补全待重试: "
                                    f"{security_error}"
                                )
                                emit_task(
                                    i,
                                    "failed",
                                    email=email_value or None,
                                    group_id=task_group_id,
                                    step="浏览器注册成功，协议密码/2FA 补全待重试",
                                    error=str(security_error)[:200],
                                )
                                record = None
                    except (JobCancelled, asyncio.CancelledError):
                        print(f"  #{i} 已取消")
                        emit_task(i, "cancelled", step="已取消")
                        record = None
                        break
                    except Exception as e:
                        attempt_error = str(e).lower()
                        transport_error = is_proxy_transport_error(e)
                        if transport_error:
                            print(
                                f"  #{i} 当前代理传输失败，"
                                f"准备切换代理重试 {attempt + 2}/{proxy_retry_limit + 1}"
                            )
                            emit_task(
                                i,
                                "running",
                                step="当前代理连接失败，正在切换下一条代理",
                            )
                        else:
                            print(f"  #{i} 第 {attempt + 1} 次尝试异常: {e}")
                            emit_task(
                                i,
                                "failed",
                                step=f"第 {attempt + 1} 次尝试异常: {str(e)[:120]}",
                                error=str(e)[:200],
                            )
                        record = None
                        err_l = str(e).lower()
                        if "pool exhausted" in err_l or "pool is empty" in err_l:
                            print(f"  #{i} 邮箱池已空，停止重试")
                            break
                    if record:
                        if mailbox_override is not None and mailbox_override.get("provider") == "smsbower_mail":
                            record.update(mailbox_role=str(mailbox_override.get("_smsbower_pair_role") or "primary"),
                                          mailbox_base_address=str(mailbox_override.get("base_address") or mailbox_override.get("address") or ""),
                                          mailbox_pair_id=str(mailbox_override.get("_smsbower_pair_id") or ""))
                            record = upsert_account_record(record, ACCOUNTS_FILE)
                        em = ""
                        try:
                            em = str((record or {}).get("email") or "")
                        except Exception:
                            em = ""
                        emit_task(
                            i,
                            "success",
                            email=em or None,
                            group_id=task_group_id,
                            step=f"任务成功 · {em}" if em else "任务成功",
                        )
                        break
                    if security_completion_failed:
                        break
                    if skipped:
                        break
                    if mailbox_override is not None and mailbox_override.get("provider") == "smsbower_mail" and (
                        mailbox_override.get("_registration_identity_created")
                        or mailbox_override.get("_smsbower_terminal_status")
                        or mailbox_override.get("_registration_outcome") in {"already_registered", "otp_timeout"}
                    ):
                        break
                    if task_stopped():
                        break
                    alias_pinned = fixed_alias_batch or paired_alias
                    if alias_pinned and not alias_proxy_retryable(attempt_error):
                        break
                    proxy_failure_markers = (
                        "email_submit_stuck",
                        "no_email_input",
                        "cf_blocked",
                    )
                    proxy_transport_failure = bool(
                        proxy and is_proxy_transport_error(attempt_error)
                    )
                    if proxy and (
                        proxy_transport_failure
                        or any(
                        marker in attempt_error for marker in proxy_failure_markers
                        )
                    ):
                        batch_bad_proxies.add(proxy)
                        print(
                            f"  #{i} 当前代理本批次冷却: "
                            f"{proxy_brief(proxy)} · {attempt_error}"
                        )
                    if proxy_transport_failure and "rate_limit_exceeded" not in attempt_error:
                        alternate_available = any(
                            candidate not in batch_bad_proxies
                            for candidate in proxy_pool
                        )
                        if not alternate_available:
                            print(f"  #{i} 代理池已无可切换线路，停止当前账号重试")
                            break
                    no_retry_markers = (
                        "otp_not_delivered",
                        "account_deactivated",
                        "token_invalid",
                        "identity_verification_required",
                    )
                    if any(marker in attempt_error for marker in no_retry_markers):
                        print(f"  #{i} 当前邮箱不可继续，停止同邮箱重试 · {attempt_error}")
                        break
                    if "rate_limit_exceeded" in attempt_error:
                        has_alternate_proxy = bool(rotate and len(proxy_pool) > 1)
                        if not has_alternate_proxy:
                            global_rate_limited.set()
                            print(f"  #{i} 当前代理已限频，停止后续排队任务")
                            break
                    if attempt < proxy_retry_limit:
                        print(f"  #{i} 重试 {attempt + 1}/{proxy_retry_limit}…")
                        emit_task(i, "running", step=f"准备重试 {attempt + 1}/{proxy_retry_limit}")
                        try:
                            await _sleep_or_stop(random.uniform(1.5, 3.5))  # nosemgrep: gitlab.bandit.B311
                        except JobCancelled:
                            break
                results[i - 1] = record
                if not record and not skipped and not task_stopped():
                    emit_task(i, "failed", step="任务失败")

        dynamic_indices = {
            index for index in range(1, n + 1)
            if not schedule_emails[index - 1] and needs_dynamic_mail_coordinator(mail_config, provider_type)
        }
        for wave_number, (indices, is_alias_pair) in enumerate(task_waves, start=1):
            indices = [index for index in indices if index not in dynamic_indices]
            if not indices:
                continue
            if _stop() or (cancel_event is not None and cancel_event.is_set()):
                break
            if is_alias_pair and wave_number > 1:
                print(
                    f"  [调度] 上一双别名已完整结束，启动下一对 "
                    f"({indices[0]}-{indices[-1]}/{n})"
                )
            barrier = asyncio.Barrier(2) if is_alias_pair and len(indices) == 2 else None
            tasks = [
                asyncio.create_task(
                    run_one(index, barrier, is_alias_pair),
                    name=f"reg-{index}",
                )
                for index in indices
            ]

            async def _watch_and_cancel():
                while True:
                    if _stop() or (
                        cancel_event is not None and cancel_event.is_set()
                    ):
                        print("  [停止] 正在取消运行中的任务…")
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        return
                    if all(task.done() for task in tasks):
                        return
                    await asyncio.sleep(0.2)

            watcher = asyncio.create_task(
                _watch_and_cancel(),
                name=f"stop-watcher-{wave_number}",
            )
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                if not watcher.done():
                    watcher.cancel()
                    try:
                        await watcher
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        pass

        if dynamic_indices and not (_stop() or (cancel_event is not None and cancel_event.is_set())):
            async def run_allocated(index: int, mailbox: dict[str, Any]) -> None:
                await run_one(index, paired_alias=True, mailbox_override=mailbox)

            def emit_dynamic(index: int, status: str, **fields: Any) -> None:
                task_group = str(group_plan[index - 1]) if isinstance(group_plan, list) and index <= len(group_plan) else str(group_id or "")
                emit_task(index, status, group_id=task_group, **fields)

            await run_dynamic_mail_groups(sorted(dynamic_indices), concurrency=conc, mail_config=mail_config, provider_type=provider_type,
                                          run_account=run_allocated, result_at=lambda index: results[index - 1], emit=emit_dynamic,
                                          stop_check=lambda: bool(_stop()) or bool(cancel_event is not None and cancel_event.is_set()), pause_check=_paused)

    ok = [r for r in results if r]
    if _stop() or (cancel_event is not None and cancel_event.is_set()):
        print(f"\n{'=' * 50}\n  已提前停止 · 成功: {len(ok)}/{n}\n{'=' * 50}")
    else:
        print(f"\n{'=' * 50}\n  成功: {len(ok)}/{n}\n{'=' * 50}")
    return ok

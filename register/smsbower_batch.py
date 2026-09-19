"""Allocate dynamic mailboxes and run bounded main/child registration groups."""
from __future__ import annotations

import asyncio
import threading
from collections import deque
from typing import Any, Callable

from common.mail_registration_metrics import begin_attempt, finish_attempt
from mail import (
    create_mailbox,
    create_smsbower_child,
    finalize_smsbower_pair,
    prepare_smsbower_pair,
    prepare_smsbower_next_code,
    release_mailbox,
)
from mail.parsing import normalize_provider_type
from register.mail_channel_options import is_smsbower_selected, option_enabled, smsbower_child_enabled


async def _await_owned(task: asyncio.Task, on_cancel: Callable[[], None] | None = None) -> tuple[Any, bool]:
    """Keep ownership of blocking work until its thread has actually exited."""
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
            if on_cancel is not None:
                on_cancel()


def registration_outcome(mailbox: dict[str, Any], record: Any, stopped: bool) -> str:
    if record:
        return "success"
    marker = str(mailbox.get("_registration_outcome") or "")
    if marker == "already_registered":
        return marker
    if mailbox.get("_registration_rate_limited"):
        return "other_failed"
    if stopped:
        return "cancelled"
    return marker if marker in {"otp_timeout", "other_failed", "cancelled"} else "other_failed"


def primary_can_start_child(mailbox: dict[str, Any], record: Any) -> bool:
    if mailbox.get("_registration_outcome") == "already_registered":
        return False
    # A passwordless-existing conversion registers the primary identity only;
    # the +split child alias would need a second fresh registration of an
    # address that already belongs to an existing account.
    if mailbox.get("_registration_passwordless_existing"):
        return False
    return bool(record or mailbox.get("_registration_code_received") or mailbox.get("_otp_accepted_by_server"))


ONLINE_DISPOSABLEMAIL_PURCHASE_BLOCK_CODES = {41003, 41004, 41005}
PAID_ORDER_PERSISTENCE_FAILURE_MESSAGE = "已购订单记录保存失败，停止后续购买，需核对订单"


class _PaidOrderPurchasesStopped(RuntimeError):
    """A queued allocation was stopped before another paid order was purchased."""


def is_marketplace_stock_error(error: BaseException) -> bool:
    """A definitive no-purchase stock response from a dynamic mail marketplace.

    SMSBower answers ``No mails yet`` (or ``not available at this price``) and
    OnlineDisposableMail reports ``暂无库存`` when nothing can be bought at
    the configured price. No activation/order was created, so letting further
    task slots retry the purchase would only repeat the same instant failure.

    OnlineDisposableMail additionally reports deterministic purchase blockers
    with a numeric business code: 41003 (stock), 41004 (balance), 41005
    (purchase cap) — live-verified as English messages (e.g. ``Insufficient
    account balance``), so the wrapped ``(code N)`` marker is matched as well
    because mail.allocate_mailbox re-raises provider errors as a plain
    RuntimeError. 41002 (purchase timeout) is documented as retriable and
    must not stop the batch.
    """
    text = str(getattr(error, "business_error", "") or str(error)).strip().lower()
    if (
        "no mails yet" in text
        or text.startswith("not available at this price")
        or "暂无库存" in text
    ):
        return True
    if getattr(error, "business_code", 0) in ONLINE_DISPOSABLEMAIL_PURCHASE_BLOCK_CODES:
        return True
    message = str(error)
    return any(
        f"(code {code})" in message
        for code in ONLINE_DISPOSABLEMAIL_PURCHASE_BLOCK_CODES
    )


async def run_dynamic_mail_groups(
    indices: list[int],
    *,
    concurrency: int,
    mail_config: dict[str, Any],
    provider_type: str | None,
    run_account: Callable[..., Any],
    result_at: Callable[[int], Any],
    emit: Callable[..., Any],
    stop_check: Callable[[], bool],
    pause_check: Callable[[], bool] | None = None,
) -> None:
    """Each worker reserves at most one purchased mailbox before doing work.

    When child registration is possible a worker owns two logical account
    slots, so several groups run concurrently without pre-buying idle leases.
    Parent and child share a code source and therefore run in that order.
    """
    pending = deque(indices)
    claim_lock = asyncio.Lock()
    pairs_possible = is_smsbower_selected(provider_type) and smsbower_child_enabled(mail_config)
    width = max(1, int(concurrency or 1) // (2 if pairs_possible else 1))
    marketplace_stock_out = asyncio.Event()
    paid_order_purchases_halted = threading.Event()

    def paid_mailbox_requests_halt(mailbox: dict[str, Any] | None) -> bool:
        return bool(
            mailbox is not None
            and normalize_provider_type(str(mailbox.get("provider") or "")) == "online_disposablemail"
            and mailbox.get("_paid_order_purchase_halt_reason") == "cloudflare_challenge"
        )

    def purchase_mailbox(allocation_config: dict[str, Any]) -> dict[str, Any]:
        # Check in the owning thread too: other allocations may already be
        # queued when a paid order fails to persist. A registration failure
        # belongs to its own task and does not cancel independent queued work.
        if paid_order_purchases_halted.is_set():
            raise _PaidOrderPurchasesStopped
        try:
            return create_mailbox(allocation_config, provider_type=provider_type)
        except Exception as error:
            if getattr(error, "business_error", "") == "paid_order_persistence_failed":
                paid_order_purchases_halted.set()
            raise

    async def worker() -> None:
        worker_cancelled = threading.Event()

        def cancelled() -> bool:
            return stop_check() or worker_cancelled.is_set()

        def request_cancel(mailbox: dict[str, Any] | None = None) -> None:
            worker_cancelled.set()
            event = (mailbox or {}).get("_registration_cancel_event")
            if event is not None:
                event.set()

        while True:
            while callable(pause_check) and pause_check() and not cancelled() and not paid_order_purchases_halted.is_set():
                await asyncio.sleep(0.2)
            async with claim_lock:
                if not pending or cancelled():
                    return
                if marketplace_stock_out.is_set() or paid_order_purchases_halted.is_set():
                    return
                index = pending.popleft()
                reserved = pending.popleft() if pairs_possible and pending else None
            primary: dict[str, Any] | None = None
            child: dict[str, Any] | None = None
            primary_success = child_success = False
            sms = False
            try:
                allocation_config = mail_config
                if pairs_possible and reserved is None:
                    # Never buy a child-capable activation for a lone final
                    # slot; a static provider may still serve it.
                    filtered = [
                        {**entry, "enable": False}
                        if isinstance(entry, dict)
                        and normalize_provider_type(str(entry.get("type") or "")) == "smsbower_mail"
                        and option_enabled(entry.get("register_child"))
                        else entry
                        for entry in mail_config.get("providers") or []
                    ]
                    if any(
                        isinstance(entry, dict)
                        and entry.get("enable", True)
                        and normalize_provider_type(str(entry.get("type") or "")) in {
                            "outlook_token", "icloud_api", "maddy_webui", "fastmail_api",
                            "smsbower_mail", "online_disposablemail",
                        }
                        for entry in filtered
                    ):
                        allocation_config = {**mail_config, "providers": filtered}
                    else:
                        # No other provider can serve the lone slot. Keep the
                        # paired activation unpurchased, but fail with a
                        # clear reason instead of a generic provider error.
                        detail = (
                            "SMSBower 已启用子号：剩余单个任务无法配对，"
                            "请将任务总数设为偶数（每组 1 主号 + 1 子号）"
                        )
                        emit(index, "failed", error=detail, step=detail)
                        return
                if cancelled():
                    emit(index, "cancelled", step="已停止，未分配邮箱")
                    if reserved is not None:
                        emit(reserved, "cancelled", step="已停止，子号未分配")
                    return
                purchase = asyncio.create_task(asyncio.to_thread(purchase_mailbox, allocation_config))
                primary, was_cancelled = await _await_owned(purchase, request_cancel)
                if was_cancelled:
                    raise asyncio.CancelledError
                sms = normalize_provider_type(str(primary.get("provider") or "")) == "smsbower_mail"
                with_child = sms and option_enabled(primary.get("register_child"))
                if not with_child and reserved is not None:
                    async with claim_lock:
                        pending.appendleft(reserved)
                    reserved = None
                primary["_smsbower_pair_role"] = "primary"
                primary["_registration_metrics_defer_terminal"] = True
                begin_attempt(primary)
                if sms:
                    prepare_smsbower_pair(primary)
                emit(index, "running", email=primary.get("address"), mailbox_role="primary", step="开始主号注册")
                primary["_registration_cancel_event"] = threading.Event()
                _, was_cancelled = await _await_owned(asyncio.create_task(run_account(index, primary)), lambda: request_cancel(primary))
                primary_success = bool(result_at(index))
                if not primary.get("_registration_recovery_pending"):
                    finish_attempt(primary.get("_registration_metric_attempt_id"), registration_outcome(primary, result_at(index), cancelled()))
                if was_cancelled:
                    raise asyncio.CancelledError
                if with_child and reserved is not None:
                    if primary.get("_registration_passwordless_existing"):
                        # The purchase routed to an existing passwordless
                        # account and was converted into login + password add;
                        # no fresh child identity can be registered for it.
                        emit(reserved, "skipped", mailbox_role="child", skip_reason="passwordless_existing_account", step="主号为无密码已有账号，已转换登录并补设密码，不注册子号")
                        reserved = None
                        continue
                    if not primary_can_start_child(primary, result_at(index)) or cancelled():
                        emit(reserved, "skipped" if not cancelled() else "cancelled", mailbox_role="child", skip_reason="parent_not_registered", step="主号未完成，子号未启动")
                        reserved = None
                        continue
                    while callable(pause_check) and pause_check() and not cancelled():
                        await asyncio.sleep(0.2)
                    if cancelled():
                        emit(reserved, "cancelled", mailbox_role="child", step="已停止，子号未启动")
                        reserved = None
                        continue
                    child = create_smsbower_child(primary)
                    child["_registration_metrics_defer_terminal"] = True
                    begin_attempt(child)
                    if child.get("_registration_outcome") == "already_registered":
                        finish_attempt(child.get("_registration_metric_attempt_id"), "already_registered")
                        emit(reserved, "skipped", email=child.get("address"), mailbox_role="child", skip_reason="already_registered", step="子号已注册，已跳过")
                        reserved = None
                        continue
                    child["_registration_cancel_event"] = threading.Event()
                    _, was_cancelled = await _await_owned(asyncio.create_task(asyncio.to_thread(prepare_smsbower_next_code, child)), lambda: request_cancel(child))
                    if was_cancelled or cancelled():
                        raise asyncio.CancelledError
                    emit(reserved, "running", email=child.get("address"), mailbox_role="child", step="邮箱继续保留，开始子号注册和取码")
                    _, was_cancelled = await _await_owned(asyncio.create_task(run_account(reserved, child)), lambda: request_cancel(child))
                    child_success = bool(result_at(reserved))
                    finish_attempt(child.get("_registration_metric_attempt_id"), registration_outcome(child, result_at(reserved), cancelled()))
                    if was_cancelled:
                        raise asyncio.CancelledError
                    reserved = None
            except asyncio.CancelledError:
                request_cancel(child or primary)
                if primary is not None:
                    finish_attempt(primary.get("_registration_metric_attempt_id"), "cancelled")
                if child is not None:
                    finish_attempt(child.get("_registration_metric_attempt_id"), "cancelled")
                if not primary_success:
                    emit(index, "cancelled", step="主子号任务已停止")
                if reserved is not None and not child_success:
                    emit(reserved, "cancelled", step="子号未完成，任务已停止")
                raise
            except Exception as error:
                if isinstance(error, _PaidOrderPurchasesStopped):
                    emit(index, "cancelled", step=PAID_ORDER_PERSISTENCE_FAILURE_MESSAGE)
                    if reserved is not None:
                        emit(reserved, "cancelled", step=PAID_ORDER_PERSISTENCE_FAILURE_MESSAGE)
                    return
                if primary is None and getattr(error, "business_error", "") == "already_registered":
                    address = str(
                        getattr(error, "activation_address", "")
                        or getattr(error, "address", "")
                        or ""
                    )
                    provider_name = (
                        "online_disposablemail"
                        if type(error).__name__ == "OnlineDisposableMailError"
                        else "smsbower_mail"
                    )
                    known = {"provider": provider_name, "address": address, "_smsbower_pair_role": "primary"}
                    finish_attempt(begin_attempt(known), "already_registered")
                    detail = (
                        "邮箱已在本地登记，已购订单已保留并跳过"
                        if provider_name == "online_disposablemail"
                        else "邮箱已在本地登记，已取消并跳过"
                    )
                    emit(index, "skipped", email=address, skip_reason="already_registered", step=detail)
                    if reserved is not None:
                        emit(reserved, "cancelled" if cancelled() else "skipped", skip_reason="parent_not_registered", step="主号已注册，子号未启动")
                    continue
                # Provider exceptions already redact their key. Do not serialize
                # mailbox dictionaries: they contain in-memory credentials.
                detail = str(error)[:240]
                paid_order_failed = (
                    primary is None
                    and getattr(error, "business_error", "") == "paid_order_persistence_failed"
                )
                if paid_order_failed:
                    paid_order_purchases_halted.set()
                    detail = PAID_ORDER_PERSISTENCE_FAILURE_MESSAGE
                if primary is not None:
                    for secret_field in ("_smsbower_api_key", "_odm_api_key"):
                        secret = str(primary.get(secret_field) or "")
                        if secret:
                            detail = detail.replace(secret, "[REDACTED]")
                    if not primary.get("_registration_recovery_pending"):
                        finish_attempt(primary.get("_registration_metric_attempt_id"), registration_outcome(primary, result_at(index), cancelled()))
                if child is not None:
                    finish_attempt(child.get("_registration_metric_attempt_id"), registration_outcome(child, result_at(reserved) if reserved else None, cancelled()))
                stock_out = (
                    primary is None
                    and not cancelled()
                    and is_marketplace_stock_error(error)
                )
                if stock_out:
                    marketplace_stock_out.set()
                target = reserved if primary_success and reserved is not None else index
                paid_order_fields = (
                    {
                        "email": str(getattr(error, "address", "") or ""),
                        "paid_order_id": str(getattr(error, "order_id", "") or ""),
                    }
                    if paid_order_failed else {}
                )
                emit(
                    target,
                    "failed" if paid_order_failed or not cancelled() else "cancelled",
                    error=detail,
                    step="邮箱市场当前无可用邮箱，已停止后续邮箱分配" if stock_out else detail,
                    **paid_order_fields,
                )
                if reserved is not None and target != reserved:
                    emit(reserved, "cancelled" if cancelled() or paid_order_failed else "skipped", skip_reason="parent_not_registered", step=PAID_ORDER_PERSISTENCE_FAILURE_MESSAGE if paid_order_failed else "主号未完成，子号未启动")
            finally:
                try:
                    if primary is not None and primary.get("_registration_recovery_pending") and not primary.get("_registration_recovery_attempted") and not paid_mailbox_requests_halt(primary) and not cancelled():
                        # Collect the child's code before spending more of the
                        # shared activation window on the primary's token repair.
                        primary["_registration_recovery_only"] = True
                        primary["_registration_recovery_attempted"] = True
                        emit(index, "running", email=primary.get("address"), step="子号取码流程已结束，正在密码登录补全主号 AT 和 2FA")
                        _, was_cancelled = await _await_owned(asyncio.create_task(run_account(index, primary)), lambda: request_cancel(primary))
                        primary_success = bool(result_at(index))
                        if was_cancelled:
                            raise asyncio.CancelledError
                finally:
                    if primary is not None:
                        finish_attempt(primary.get("_registration_metric_attempt_id"), registration_outcome(primary, result_at(index), cancelled()))
                        if sms and primary.get("_smsbower_pair_state") is not None:
                            await _await_owned(asyncio.create_task(asyncio.to_thread(finalize_smsbower_pair, primary, primary_success=primary_success, child_success=child_success)), request_cancel)
                        elif not primary_success:
                            await _await_owned(asyncio.create_task(asyncio.to_thread(release_mailbox, primary)), request_cancel)

    tasks = [asyncio.create_task(worker()) for _ in range(min(width, len(indices)))]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        while pending:
            emit(
                pending.popleft(),
                "cancelled",
                step=(
                    PAID_ORDER_PERSISTENCE_FAILURE_MESSAGE
                    if paid_order_purchases_halted.is_set()
                    else "邮箱市场已无可用邮箱，停止分配"
                    if marketplace_stock_out.is_set()
                    else "已停止，未分配邮箱"
                ),
            )

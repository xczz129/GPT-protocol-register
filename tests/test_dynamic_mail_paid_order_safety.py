from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import mail
from mail import OnlineDisposableMailError
from register import smsbower_batch as batch


MESSAGE = "已购订单记录保存失败，停止后续购买，需核对订单"
CONFIG = {"providers": [{"type": "online_disposablemail", "enable": True}]}


def persistence_error():
    return OnlineDisposableMailError(
        MESSAGE,
        business_error="paid_order_persistence_failed",
        order_id="fixture-order",
        address="fixture@example.test",
    )


def mailbox():
    return {
        "provider": "online_disposablemail",
        "address": "fixture@example.test",
        "order_id": "fixture-order",
        "mail_id": "fixture-order",
    }


@pytest.fixture(autouse=True)
def isolate_external_effects(monkeypatch):
    monkeypatch.setattr(batch, "begin_attempt", lambda *_: None)
    monkeypatch.setattr(batch, "finish_attempt", lambda *_: None)
    monkeypatch.setattr(batch, "release_mailbox", lambda *_: pytest.fail("unexpected release"))
    monkeypatch.setattr(batch, "prepare_smsbower_pair", lambda *_: pytest.fail("unexpected SMSBower operation"))
    monkeypatch.setattr(batch, "create_mailbox", lambda *_args, **_kwargs: pytest.fail("purchase must be mocked"))


async def run_batch(*, total=5, concurrency=1, stop_check=lambda: False, observer=None, account_action=None):
    records, events = {}, []

    async def account(index, allocated):
        assert not allocated["_registration_cancel_event"].is_set()
        record = await account_action(index, allocated) if account_action else {"email": allocated["address"]}
        if record:
            records[index] = record

    def emit(index, status, **fields):
        events.append((index, status, fields))
        if observer:
            observer(index, status, fields)

    await asyncio.wait_for(
        batch.run_dynamic_mail_groups(
            list(range(1, total + 1)), concurrency=concurrency,
            mail_config=CONFIG, provider_type="online_disposablemail",
            run_account=account, result_at=records.get, emit=emit,
            stop_check=stop_check,
        ),
        timeout=5,
    )
    return records, events


def test_paid_order_write_failure_stops_pending_purchases(monkeypatch):
    purchases = []

    def allocate(*_args, **_kwargs):
        purchases.append(1)
        raise persistence_error()

    monkeypatch.setattr(batch, "create_mailbox", allocate)
    records, events = asyncio.run(run_batch())
    assert len(purchases) == 1
    assert not records
    assert events[0] == (1, "failed", {
        "error": MESSAGE, "step": MESSAGE,
        "email": "fixture@example.test", "paid_order_id": "fixture-order",
    })
    assert [(index, status) for index, status, _ in events[1:]] == [
        (index, "cancelled") for index in range(2, 6)
    ]
    assert all(fields["step"] == MESSAGE for _, _, fields in events)


def test_already_queued_executor_work_does_not_purchase_after_write_failure(monkeypatch):
    purchases = []

    def allocate(*_args, **_kwargs):
        purchases.append(1)
        raise persistence_error()

    monkeypatch.setattr(batch, "create_mailbox", allocate)

    async def scenario():
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
        return await run_batch(concurrency=4)

    records, events = asyncio.run(scenario())
    assert len(purchases) == 1
    assert not records
    assert len(events) == 5
    assert sum(status == "failed" for _, status, _ in events) == 1
    assert sum(status == "cancelled" for _, status, _ in events) == 4
    assert all(fields["step"] == MESSAGE for _, _, fields in events)


def test_in_flight_successful_purchase_finishes_without_cancel_or_repurchase(monkeypatch):
    purchase_started = threading.Event()
    failure_reported = threading.Event()
    purchase_lock = threading.Lock()
    purchases = []

    def allocate(*_args, **_kwargs):
        with purchase_lock:
            purchases.append(1)
            number = len(purchases)
        if number == 1:
            purchase_started.set()
            assert failure_reported.wait(3)
            return mailbox()
        assert number == 2, "no subsequent purchase is allowed"
        assert purchase_started.wait(3)
        raise persistence_error()

    def observe(_index, status, fields):
        if status == "failed" and fields.get("error") == MESSAGE:
            failure_reported.set()

    monkeypatch.setattr(batch, "create_mailbox", allocate)
    records, events = asyncio.run(run_batch(concurrency=2, observer=observe))
    assert len(purchases) == 2
    assert len(records) == 1
    successful_index = next(iter(records))
    assert not any(index == successful_index and status == "cancelled" for index, status, _ in events)
    assert sum(status == "failed" for _, status, _ in events) == 1
    assert sum(status == "cancelled" for _, status, _ in events) == 3


def test_user_stop_does_not_hide_paid_order_write_failure(monkeypatch):
    stopped = threading.Event()
    purchases = []

    def allocate(*_args, **_kwargs):
        purchases.append(1)
        stopped.set()
        raise persistence_error()

    monkeypatch.setattr(batch, "create_mailbox", allocate)
    records, events = asyncio.run(run_batch(stop_check=stopped.is_set))
    assert len(purchases) == 1
    assert not records
    assert events[0] == (1, "failed", {
        "error": MESSAGE, "step": MESSAGE,
        "email": "fixture@example.test", "paid_order_id": "fixture-order",
    })
    assert all(status == "cancelled" for _, status, _ in events[1:])


def test_ordinary_allocation_error_does_not_trip_paid_order_stop(monkeypatch):
    purchases = []

    def allocate(*_args, **_kwargs):
        purchases.append(1)
        if len(purchases) == 1:
            raise RuntimeError("fixture ordinary allocation error")
        return mailbox()

    monkeypatch.setattr(batch, "create_mailbox", allocate)
    records, events = asyncio.run(run_batch(total=3))
    assert len(purchases) == 3
    assert len(records) == 2
    assert events[0][1] == "failed"
    assert events[0][2]["error"] == "fixture ordinary allocation error"
    assert not any(status == "cancelled" for _, status, _ in events)


def test_paid_order_stop_is_scoped_to_its_batch(monkeypatch):
    purchases = []

    def allocate(*_args, **_kwargs):
        purchases.append(1)
        if len(purchases) == 1:
            raise persistence_error()
        return mailbox()

    monkeypatch.setattr(batch, "create_mailbox", allocate)

    async def scenario():
        first = await run_batch(total=2)
        second = await run_batch(total=2)
        return first, second

    (failed_records, _), (next_records, next_events) = asyncio.run(scenario())
    assert not failed_records
    assert len(next_records) == 2
    assert len(purchases) == 3
    assert all(status == "running" for _, status, _ in next_events)


def test_cloudflare_failure_retains_its_order_without_cancelling_other_tasks(monkeypatch, tmp_path):
    purchases, released = [], []
    monkeypatch.setattr(mail, "STATE_FILE", tmp_path / "mail-state.json")
    monkeypatch.setattr(mail, "_record_mail_registration_outcome", lambda *_: None)

    def allocate(*_args, **_kwargs):
        number = len(purchases) + 1
        allocated = {
            **mailbox(), "address": f"fixture-{number}@example.test",
            "order_id": f"fixture-order-{number}", "mail_id": f"fixture-order-{number}",
        }
        purchases.append(allocated)
        state = mail._load_state()
        state[allocated["address"]] = {**allocated, "state": "in_use", "reason": ""}
        mail._save_state(state)
        return allocated

    async def account(index, allocated):
        if index == 1:
            allocated["_paid_order_purchase_halt_reason"] = "cloudflare_challenge"
            raise RuntimeError("fixture cloudflare_challenge")
        return {"email": allocated["address"]}

    def release(allocated):
        released.append(allocated)
        mail.release_mailbox(allocated)

    monkeypatch.setattr(batch, "create_mailbox", allocate)
    monkeypatch.setattr(batch, "release_mailbox", release)
    records, events = asyncio.run(run_batch(account_action=account))
    assert len(purchases) == 5
    assert released == [purchases[0]]
    assert set(records) == {2, 3, 4, 5}
    assert {record["email"] for record in records.values()} == {
        f"fixture-{number}@example.test" for number in range(2, 6)
    }
    assert [(index, status) for index, status, _ in events if status == "failed"] == [(1, "failed")]
    assert not any(status == "cancelled" for _, status, _ in events)
    retained = mail._load_state()["fixture-1@example.test"]
    assert retained["order_id"] == "fixture-order-1"
    assert retained["state"] == "failed"
    assert retained["paid_order_review_required"] is True
    assert not mail._entry_available(retained)


def test_cloudflare_failure_does_not_interrupt_in_flight_or_later_independent_tasks(monkeypatch):
    second_purchase_started = threading.Event()
    failure_reported = threading.Event()
    purchase_lock = threading.Lock()
    purchases, released = [], []

    def allocate(*_args, **_kwargs):
        with purchase_lock:
            purchases.append(1)
            number = len(purchases)
        allocated = {
            **mailbox(), "address": f"concurrent-{number}@example.test",
            "order_id": f"concurrent-order-{number}", "mail_id": f"concurrent-order-{number}",
        }
        if number == 1:
            assert second_purchase_started.wait(3)
            return {**allocated, "reject": True}
        if number == 2:
            second_purchase_started.set()
            assert failure_reported.wait(3)
        return allocated

    async def account(_index, allocated):
        if allocated.get("reject"):
            allocated["_paid_order_purchase_halt_reason"] = "cloudflare_challenge"
            raise RuntimeError("fixture cloudflare_challenge")
        return {"email": allocated["address"]}

    def observe(_index, status, fields):
        if status == "failed" and fields.get("error") == "fixture cloudflare_challenge":
            failure_reported.set()

    monkeypatch.setattr(batch, "create_mailbox", allocate)
    monkeypatch.setattr(batch, "release_mailbox", released.append)
    records, events = asyncio.run(run_batch(concurrency=2, account_action=account, observer=observe))
    assert len(purchases) == 5
    assert len(released) == 1
    assert released[0]["address"] == "concurrent-1@example.test"
    assert len(records) == 4
    assert {record["email"] for record in records.values()} == {
        f"concurrent-{number}@example.test" for number in range(2, 6)
    }
    assert sum(status == "failed" for _, status, _ in events) == 1
    assert not any(status == "cancelled" for _, status, _ in events)


@pytest.mark.parametrize("marker", [None, "ordinary_business_403"])
def test_unmarked_or_other_denial_does_not_stop_all_paid_purchases(monkeypatch, marker):
    purchases = []

    def allocate(*_args, **_kwargs):
        purchases.append(1)
        return mailbox()

    async def rejected(_index, allocated):
        if marker:
            allocated["_paid_order_purchase_halt_reason"] = marker
        return None

    monkeypatch.setattr(batch, "create_mailbox", allocate)
    monkeypatch.setattr(batch, "release_mailbox", lambda *_: None)
    records, events = asyncio.run(run_batch(total=3, account_action=rejected))
    assert len(purchases) == 3
    assert not records
    assert not any(status == "cancelled" for _, status, _ in events)


def test_paid_order_diagnostic_preserves_only_reconciliation_fields(monkeypatch):
    def allocate(*_args, **_kwargs):
        error = persistence_error()
        error.api_key = "fixture-secret-must-not-be-in-event"
        error.mailbox = {"_odm_api_key": error.api_key}
        raise error

    monkeypatch.setattr(batch, "create_mailbox", allocate)
    _, events = asyncio.run(run_batch(total=1))
    assert events[0][2] == {
        "error": MESSAGE, "step": MESSAGE,
        "email": "fixture@example.test", "paid_order_id": "fixture-order",
    }
    assert "fixture-secret" not in repr(events)


def test_cloudflare_rejected_paid_mailbox_does_not_start_deferred_recovery(monkeypatch):
    calls, released, purchases = [], [], []

    def allocate(*_args, **_kwargs):
        number = len(purchases) + 1
        allocated = {
            **mailbox(), "address": f"rejected-{number}@example.test",
            "order_id": f"rejected-order-{number}", "mail_id": f"rejected-order-{number}",
        }
        purchases.append(allocated)
        return allocated

    monkeypatch.setattr(batch, "create_mailbox", allocate)
    monkeypatch.setattr(batch, "release_mailbox", released.append)

    async def rejected(index, allocated):
        calls.append((index, allocated["address"]))
        assert not allocated.get("_registration_recovery_only")
        allocated["_paid_order_purchase_halt_reason"] = "cloudflare_challenge"
        allocated["_registration_recovery_pending"] = True
        raise RuntimeError("fixture cloudflare_challenge")

    records, events = asyncio.run(run_batch(total=2, account_action=rejected))
    assert calls == [(1, "rejected-1@example.test"), (2, "rejected-2@example.test")]
    assert released == purchases
    assert not records
    assert [(index, status) for index, status, _ in events if status == "failed"] == [(1, "failed"), (2, "failed")]
    assert not any(status == "cancelled" for _, status, _ in events)

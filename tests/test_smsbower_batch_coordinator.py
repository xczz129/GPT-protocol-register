from __future__ import annotations

import asyncio
import threading

import pytest

from common.mail_registration_metrics import get_summary
from register import smsbower_batch as batch


@pytest.fixture
def harness(monkeypatch):
    purchases, settlements, children = [], [], []

    def allocate(config, *, provider_type=None):
        entries = [p for p in config["providers"] if p.get("enable", True)]
        if not entries:
            raise RuntimeError("no eligible mailbox for remaining account slot")
        mailbox = {"provider": "smsbower_mail", "address": f"main{len(purchases)}@gmail.com", "domain": "gmail.com",
                   "register_child": entries[0].get("register_child", True), "mail_id": str(len(purchases))}
        purchases.append(mailbox)
        return mailbox

    def prepare(mailbox):
        mailbox["_smsbower_pair_state"] = {}

    def child(parent):
        result = {"provider": "smsbower_mail", "address": parent["address"].replace("@", "+split-fixture@"),
                  "domain": "gmail.com", "mail_id": parent["mail_id"], "_smsbower_pair_role": "child"}
        children.append(result)
        return result

    monkeypatch.setattr(batch, "create_mailbox", allocate)
    monkeypatch.setattr(batch, "prepare_smsbower_pair", prepare)
    monkeypatch.setattr(batch, "create_smsbower_child", child)
    monkeypatch.setattr(batch, "finalize_smsbower_pair", lambda mailbox, **status: settlements.append((mailbox["mail_id"], status)))
    monkeypatch.setattr(batch, "release_mailbox", lambda mailbox: settlements.append((mailbox["mail_id"], {"released": True})))
    return purchases, settlements, children


def config(child=True):
    return {"providers": [{"type": "smsbower_mail", "enable": True, "register_child": child}]}


def run(total, task, *, child=True, stop=lambda: False, concurrency=2):
    records, events = {}, []

    async def account(index, mailbox):
        value = await task(index, mailbox)
        if value:
            records[index] = value

    asyncio.run(batch.run_dynamic_mail_groups(list(range(1, total + 1)), concurrency=concurrency,
                mail_config=config(child), provider_type="smsbower_mail", run_account=account,
                result_at=records.get, emit=lambda *args, **kwargs: events.append((args, kwargs)), stop_check=stop))
    return records, events


def test_one_activation_creates_main_then_child_and_settles_once(harness):
    order = []

    async def task(index, mailbox):
        order.append((index, mailbox["_smsbower_pair_role"], mailbox["mail_id"]))
        return {"email": mailbox["address"]}

    records, _ = run(2, task)
    purchases, settlements, children = harness
    assert len(purchases) == len(children) == len(settlements) == 1
    assert order == [(1, "primary", "0"), (2, "child", "0")]
    assert len(records) == 2
    assert settlements[0][1] == {"primary_success": True, "child_success": True}
    stats = get_summary()["totals"]
    assert stats["attempted"] == stats["success"] == 2


@pytest.mark.parametrize("outcome", ["already_registered", "otp_timeout", "other_failed"])
def test_failed_main_never_starts_or_counts_a_child(harness, outcome):
    async def task(index, mailbox):
        mailbox["_registration_outcome"] = outcome
        return None

    records, events = run(2, task)
    purchases, settlements, children = harness
    assert not records and not children
    assert len(purchases) == len(settlements) == 1
    assert settlements[0][1] == {"primary_success": False, "child_success": False}
    assert any(args == (2, "skipped") for args, _ in events)
    totals = get_summary()["totals"]
    assert totals["attempted"] == totals[outcome] == 1


def test_child_timeout_retains_successful_main_and_separate_statistics(harness):
    async def task(index, mailbox):
        if index == 1:
            return {"email": mailbox["address"]}
        mailbox["_registration_outcome"] = "otp_timeout"

    records, _ = run(2, task)
    assert list(records) == [1]
    assert harness[1][0][1] == {"primary_success": True, "child_success": False}
    totals = get_summary()["totals"]
    assert totals["success"] == totals["otp_timeout"] == 1
    assert totals["attempted"] == 2


def test_locally_registered_child_is_counted_and_skipped_without_authentication(harness, monkeypatch):
    original = batch.create_smsbower_child

    def existing_child(parent):
        child = original(parent)
        child["_registration_outcome"] = "already_registered"
        return child

    monkeypatch.setattr(batch, "create_smsbower_child", existing_child)

    async def task(index, mailbox):
        assert index == 1
        return {"email": mailbox["address"]}

    records, events = run(2, task)
    assert list(records) == [1]
    assert any(args == (2, "skipped") and fields.get("skip_reason") == "already_registered" for args, fields in events)
    totals = get_summary()["totals"]
    assert totals["attempted"] == 2 and totals["success"] == totals["already_registered"] == 1


def test_local_duplicate_at_allocation_is_counted_without_second_settlement(harness, monkeypatch):
    from mail import SmsBowerMailError

    def duplicate(*args, **kwargs):
        raise SmsBowerMailError("known mailbox was cancelled", business_error="already_registered",
                               activation_id="fixture-activation", activation_address="known@gmail.com")

    monkeypatch.setattr(batch, "create_mailbox", duplicate)

    async def task(*args):
        pytest.fail("an existing mailbox must never start authentication")

    records, events = run(2, task)
    assert not records and not harness[1]
    assert any(args == (1, "skipped") and fields.get("skip_reason") == "already_registered" for args, fields in events)
    assert get_summary()["totals"]["already_registered"] == 1


def test_consumed_mailbox_is_not_miscounted_as_registration_success(harness):
    async def task(index, mailbox):
        mailbox["_registration_outcome"] = "success"
        mailbox["_otp_accepted_by_server"] = True
        return None

    run(2, task)
    totals = get_summary()["totals"]
    assert totals["success"] == 0 and totals["other_failed"] == 2


def test_primary_code_keeps_activation_for_child_even_when_at_recovery_is_pending(harness):
    calls = []

    async def task(index, mailbox):
        calls.append(index)
        assert not harness[1], "the shared activation must remain open"
        mailbox["_registration_code_received"] = True
        if index == 1:
            mailbox["_registration_outcome"] = "other_failed"
            return None
        return {"email": mailbox["address"]}

    records, _ = run(2, task)
    assert calls == [1, 2]
    assert list(records) == [2]
    assert harness[1][0][1] == {"primary_success": False, "child_success": True}


def test_primary_password_recovery_waits_until_child_code_flow_has_finished(harness):
    order = []

    async def task(index, mailbox):
        assert not harness[1]
        if index == 1 and not mailbox.get("_registration_recovery_only"):
            order.append("primary-code")
            mailbox.update(_registration_code_received=True, _registration_recovery_pending=True,
                           _registration_partial_account={"email": mailbox["address"], "password": "fixture-password"})
            return None
        if index == 2:
            order.append("child-code")
            return {"email": mailbox["address"]}
        order.append("primary-password-login")
        assert get_summary()["totals"]["attempted"] == 2
        return {"email": mailbox["address"]}

    records, _ = run(2, task)
    assert order == ["primary-code", "child-code", "primary-password-login"]
    assert len(records) == 2 and len(harness[1]) == 1
    assert get_summary()["totals"]["success"] == 2


def test_child_switch_off_does_not_double_registration_or_purchase(harness):
    async def task(index, mailbox):
        return {"email": mailbox["address"]}

    records, _ = run(1, task, child=False)
    assert len(records) == len(harness[0]) == len(harness[1]) == 1
    assert not harness[2]


def test_pair_capacity_limits_groups_and_main_precedes_its_child(harness):
    active, maximum, parents_finished = set(), [0], set()
    first_groups_ready = asyncio.Event()

    async def task(index, mailbox):
        identity = mailbox["mail_id"]
        if mailbox["_smsbower_pair_role"] == "primary":
            active.add(identity)
            maximum[0] = max(maximum[0], len(active))
            if len(active) == 2:
                first_groups_ready.set()
            await asyncio.wait_for(first_groups_ready.wait(), timeout=2)
            parents_finished.add(identity)
        else:
            assert identity in parents_finished
            active.remove(identity)
        return {"email": mailbox["address"]}

    records, _ = run(8, task, concurrency=4)
    assert len(records) == 8 and len(harness[0]) == 4
    assert maximum[0] == 2
    assert not active


def test_stop_after_main_keeps_it_and_does_not_purchase_or_start_child(harness):
    stopped = [False]

    async def task(index, mailbox):
        stopped[0] = True
        return {"email": mailbox["address"]}

    records, events = run(4, task, stop=lambda: stopped[0])
    assert list(records) == [1]
    assert len(harness[0]) == 1 and not harness[2]
    assert any(args == (2, "cancelled") for args, _ in events)
    assert get_summary()["totals"]["attempted"] == 1


def test_unpaired_remaining_slot_does_not_purchase_two_account_activation(harness):
    async def task(index, mailbox):
        pytest.fail("must not start an unpaired SMSBower registration")

    records, _ = run(1, task)
    assert not records and not harness[0]


def test_cancel_during_purchase_retains_result_until_it_can_be_released(harness, monkeypatch):
    started, complete = threading.Event(), threading.Event()
    original = batch.create_mailbox

    def purchase(*args, **kwargs):
        started.set()
        assert complete.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(batch, "create_mailbox", purchase)

    async def scenario():
        task = asyncio.create_task(batch.run_dynamic_mail_groups([1, 2], concurrency=2,
            mail_config=config(), provider_type="smsbower_mail", run_account=lambda *_: pytest.fail("registration must not start"),
            result_at=lambda _: None, emit=lambda *a, **kw: None, stop_check=lambda: False))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        complete.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert len(harness[0]) == len(harness[1]) == 1
    assert harness[1][0][1] == {"released": True}


def test_cancel_waits_for_registration_thread_before_finalizing_mailbox(harness, monkeypatch):
    started, exited = threading.Event(), threading.Event()
    finalizer = batch.finalize_smsbower_pair

    def finish(*args, **kwargs):
        assert exited.is_set(), "activation must remain open until the worker has stopped"
        finalizer(*args, **kwargs)

    monkeypatch.setattr(batch, "finalize_smsbower_pair", finish)

    async def account(index, mailbox):
        def worker():
            started.set()
            assert mailbox["_registration_cancel_event"].wait(2)
            exited.set()
        await asyncio.to_thread(worker)

    async def scenario():
        task = asyncio.create_task(batch.run_dynamic_mail_groups([1, 2], concurrency=2,
            mail_config=config(), provider_type="smsbower_mail", run_account=account,
            result_at=lambda _: None, emit=lambda *a, **kw: None, stop_check=lambda: False))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert len(harness[1]) == 1 and not harness[2]
    assert get_summary()["totals"]["cancelled"] == 1


def test_cancelled_purchase_error_does_not_start_another_group(harness, monkeypatch):
    started, complete = threading.Event(), threading.Event()
    calls = []

    def purchase(*args, **kwargs):
        calls.append(True)
        started.set()
        assert complete.wait(2)
        raise RuntimeError("purchase response failed")

    monkeypatch.setattr(batch, "create_mailbox", purchase)

    async def scenario():
        task = asyncio.create_task(batch.run_dynamic_mail_groups([1, 2, 3, 4], concurrency=2,
            mail_config=config(), provider_type="smsbower_mail", run_account=lambda *_: pytest.fail("cancelled purchase must not start accounts"),
            result_at=lambda _: None, emit=lambda *a, **kw: None, stop_check=lambda: False))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        complete.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert len(calls) == 1


def test_cancel_during_settlement_stops_before_purchasing_next_group(harness, monkeypatch):
    started, complete = threading.Event(), threading.Event()
    finalizer = batch.finalize_smsbower_pair
    records = {}

    def finish(*args, **kwargs):
        started.set()
        assert complete.wait(2)
        finalizer(*args, **kwargs)

    monkeypatch.setattr(batch, "finalize_smsbower_pair", finish)

    async def account(index, mailbox):
        records[index] = {"email": mailbox["address"]}

    async def scenario():
        task = asyncio.create_task(batch.run_dynamic_mail_groups([1, 2, 3, 4], concurrency=2,
            mail_config=config(), provider_type="smsbower_mail", run_account=account,
            result_at=records.get, emit=lambda *a, **kw: None, stop_check=lambda: False))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        complete.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert list(records) == [1, 2]
    assert len(harness[0]) == len(harness[1]) == 1


def test_fallback_child_provider_is_filtered_when_only_one_slot_remains(monkeypatch):
    observed = []
    cfg = {"providers": [
        {"type": "smsbower_mail", "enable": True, "register_child": False},
        {"type": "smsbower_email", "enable": True, "register_child": True},
    ]}

    def purchase(mail_config, **kwargs):
        observed.append(mail_config)
        assert mail_config["providers"][0]["enable"] is True
        assert mail_config["providers"][1]["enable"] is False
        raise RuntimeError("first provider has no stock")

    monkeypatch.setattr(batch, "create_mailbox", purchase)
    asyncio.run(batch.run_dynamic_mail_groups([1], concurrency=2, mail_config=cfg, provider_type="smsbower_mail",
        run_account=lambda *_: pytest.fail("no registration expected"), result_at=lambda _: None,
        emit=lambda *a, **kw: None, stop_check=lambda: False))
    assert len(observed) == 1


@pytest.mark.parametrize("engine", ["protocol", "browser"])
def test_real_schedulers_forward_the_reserved_primary_and_child(harness, monkeypatch, engine):
    import register.protocol as protocol
    import register.batch as browser
    import mail
    received = []
    capacity = {"total": 1, "available": 2, "emails": []}
    settings = {"mail": config(), "registration": {"engine": engine, "threads": 2, "total": 2}, "proxy": {"enabled": False}}

    def record(index, **kwargs):
        mailbox = kwargs["mailbox_override"]
        received.append((index, mailbox["address"], mailbox["mail_id"]))
        return {"email": mailbox["address"], "provider_type": "smsbower_mail", "access_token": "fixture-access"}

    async def browser_record(index, *args, **kwargs):
        return record(index, **kwargs)

    for module in (mail, browser):
        monkeypatch.setattr(module, "count_pool_capacity", lambda *a, **kw: dict(capacity))
        monkeypatch.setattr(module, "release_stale_in_use", lambda: 0)
    monkeypatch.setattr(protocol, "register_one", record)
    monkeypatch.setattr(browser, "register_one", browser_record)
    monkeypatch.setattr(browser, "upsert_account_record", lambda value, *a: value)
    if engine == "protocol":
        records = asyncio.run(protocol.run_protocol_register(settings, total=2, concurrency=2, provider_type="smsbower_mail"))
    else:
        records = asyncio.run(browser.run_register(settings, total=2, concurrency=2, provider_type="smsbower_mail"))
    assert len(records) == len(received) == 2
    assert received[0][2] == received[1][2]
    assert "+split" in received[1][1]
    assert len(harness[0]) == len(harness[1]) == 1

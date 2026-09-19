"""Opt-in follow-ups never purchase numbers in tests; all workers are mocked."""
from copy import deepcopy
import json
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from common import registration_sms_followup as followups
from web.routes import codex_oauth as route


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    queue = followups.RegistrationSmsQueue(tmp_path / "queue.sqlite3")
    monkeypatch.setattr(followups, "_QUEUE", queue)
    config = {"sms_code": {"enabled": True, "provider": "smsbower", "api_key": "private-supplier-key", "max_price": "0.42", "max_phone_attempts": 0, "timeout_seconds": 60}}
    state = SimpleNamespace(_SERVER_SHUTDOWN_EVENT=threading.Event(), _read_config=lambda: deepcopy(config))
    monkeypatch.setattr(route, "_state", lambda: state)
    monkeypatch.setattr(route, "_STATE", {**deepcopy(route._STATE), "running": False, "batch_id": "", "options": {}, "counts": {}})
    monkeypatch.setattr(route, "_STOP", threading.Event())
    monkeypatch.setattr(route, "_BATCH_THREAD", None)
    monkeypatch.setattr(route, "_relay_context", lambda: {"configured": True, "by_email": {}, "by_file": {}})
    monkeypatch.setattr(route, "_log", Mock())
    return SimpleNamespace(queue=queue, state=state, config=config, path=queue.path)


def test_fifo_chunks_dedupe_and_early_completion_are_exactly_once(boundary):
    q = boundary.queue
    emails = [f"user{i}@example.test" for i in range(401)]
    q.enqueue("first", emails + [emails[0].upper()])
    q.enqueue("second", ["last@example.test"])
    q.enqueue("first", ["different@example.test"])
    calls = []

    def start(wanted, *, source_registration_batch_id, sms_options):
        batch = f"sms-{len(calls)}"
        calls.append((source_registration_batch_id, wanted))
        assert q.started(source_registration_batch_id, batch)
        q.finished(source_registration_batch_id, batch, "completed", completed=len(wanted))
        q.drain(start, lambda: False)  # A worker can finish before start returns.
        return {"batch_id": batch}

    q.drain(start, lambda: False)
    assert [(source, len(wanted)) for source, wanted in calls] == [("first", 200), ("first", 200), ("first", 1), ("second", 1)]
    assert set.union(*(wanted for source, wanted in calls if source == "first")) == set(emails)
    assert q.snapshot("first")["completed"] == 401
    assert q.snapshot("first")["status"] == "completed"
    q.drain(start, lambda: False)
    assert len(calls) == 4


def test_busy_slot_remains_pending_until_finish_wakes_queue(boundary, monkeypatch):
    q = boundary.queue
    q.enqueue("new-registration", ["new@example.test"])
    attempts = []

    def start(wanted, **kwargs):
        attempts.append(wanted)
        if route._STATE["running"]:
            raise HTTPException(409, "busy")
        assert kwargs["strategy"] == "auto" and kwargs["force"] is False and kwargs["concurrency"] == 1
        assert q.started(kwargs["source_registration_batch_id"], "automatic-sms")
        return {"batch_id": "automatic-sms"}

    monkeypatch.setattr(route, "_start_sms_batch", start)
    route._reserve_batch("import", 1, "manual-import")
    route.wake_registration_sms_queue()
    assert q.snapshot("new-registration")["status"] == "pending"
    assert len(attempts) == 1
    route._finish_batch("manual-import")
    assert len(attempts) == 2
    assert q.snapshot("new-registration")["batch_id"] == "automatic-sms"


def test_cancel_before_enqueue_is_durable_and_does_not_cancel_started_sms(boundary):
    q = boundary.queue
    q.cancel("late-finalizer")
    assert q.enqueue("late-finalizer", ["late@example.test"])["status"] == "cancelled"
    starter = Mock()
    q.drain(starter, lambda: False)
    starter.assert_not_called()
    q.enqueue("already-started", ["started@example.test"])
    q.drain(lambda _wanted, **kw: q.started(kw["source_registration_batch_id"], "sms-live"), lambda: False)
    q.cancel("already-started")
    assert q.snapshot("already-started")["status"] == "running"


def test_restart_interrupts_dispatched_and_preserves_unstarted(boundary):
    q = boundary.queue
    q.enqueue("started", ["a@example.test"])
    q.enqueue("pending", ["b@example.test"])
    q.drain(lambda _wanted, **kw: q.started(kw["source_registration_batch_id"], "sms-old"), lambda: False)
    restarted = followups.RegistrationSmsQueue(boundary.path)
    assert restarted.snapshot("started")["status"] == "interrupted"
    calls = []

    def start(wanted, **kw):
        calls.append(wanted)
        restarted.started(kw["source_registration_batch_id"], "sms-new")

    restarted.drain(start, lambda: False)
    assert calls == [{"b@example.test"}]
    assert restarted.snapshot("pending")["status"] == "running"


def test_failed_or_incomplete_chunk_never_advances_or_retries(boundary):
    q = boundary.queue
    q.enqueue("source", [f"u{i}@example.test" for i in range(201)])
    starts = []

    def start(wanted, **kw):
        starts.append(wanted)
        q.started("source", "sms-partial")
        q.finished("source", "sms-partial", "completed", completed=1)

    q.drain(start, lambda: False)
    q.drain(start, lambda: False)
    assert len(starts) == 1
    assert q.snapshot("source")["status"] == "failed"
    assert q.snapshot("source")["pending"] == 1


def test_preflight_failure_is_terminal_and_masks_sensitive_details(boundary):
    boundary.queue.enqueue("source", ["a@example.test"])
    start = Mock(side_effect=RuntimeError("api_key=private access_token=token"))
    boundary.queue.drain(start, lambda: False)
    boundary.queue.drain(start, lambda: False)
    assert start.call_count == 1
    snapshot = boundary.queue.snapshot("source")
    assert snapshot["status"] == "failed"
    assert "private" not in str(snapshot) and "token" not in str(snapshot)


def test_queue_capacity_rejection_is_visible(boundary, monkeypatch):
    monkeypatch.setattr(followups, "MAX_PENDING_BATCHES", 1)
    boundary.queue.enqueue("first", ["first@example.test"])
    rejected = boundary.queue.enqueue("second", ["second@example.test"])
    assert rejected["status"] == "failed" and "队列已满" in rejected["message"]


def test_default_off_does_not_touch_queue_or_sms(boundary, monkeypatch):
    launch = Mock()
    monkeypatch.setattr(route, "wake_registration_sms_queue", launch)
    assert route.enqueue_registration_sms_followup({}, [{"email": "a@test", "access_token": "at"}], [])["status"] == "disabled"
    assert not boundary.path.exists()
    launch.assert_not_called()


def test_only_current_final_successes_are_queued_with_saved_settings(boundary, monkeypatch):
    monkeypatch.setattr(route, "wake_registration_sms_queue", Mock())
    params = {"batch_id": "source", "auto_sms_after_register": True}
    results = [
        {"email": email, "access_token": "private-token", "registration_state": state}
        for email, state in [("ok@test", "complete"), ("OK@test", "complete"), ("failed@test", "complete"), ("skip@test", "complete"), ("old@test", "complete"), ("pending@test", "security_pending")]
    ]
    cards = [
        {"email": email, "status": status, "batch_id": batch}
        for email, status, batch in [("ok@test", "success", "source"), ("failed@test", "failed", "source"), ("skip@test", "skipped", "source"), ("old@test", "success", "prior"), ("pending@test", "success", "source")]
    ]
    result = route.enqueue_registration_sms_followup(params, results, cards)
    assert result["total"] == 1 and result["status"] == "pending"
    with sqlite3.connect(boundary.path) as connection:
        raw = connection.execute("SELECT payload FROM followups").fetchone()[0]
    row = json.loads(raw)
    assert row["remaining"] == ["ok@test"]
    assert row["sms_options"]["max_price"] == "0.42"
    assert row["sms_options"]["max_phone_attempts"] == 0  # Preserve configured manual semantics.
    assert "private-token" not in raw and "private-supplier-key" not in raw


@pytest.mark.parametrize("stopped", [True, False])
def test_registration_stop_and_server_shutdown_do_not_launch(boundary, monkeypatch, stopped):
    launch = Mock()
    monkeypatch.setattr(route, "wake_registration_sms_queue", launch)
    if not stopped:
        boundary.state._SERVER_SHUTDOWN_EVENT.set()
    result = route.enqueue_registration_sms_followup({"batch_id": "source", "auto_sms_after_register": True}, [{"email": "ok@test", "access_token": "at"}], [], stopped=stopped)
    assert result["status"] == "cancelled"
    launch.assert_not_called()


def test_disabled_supplier_is_actionable_in_public_state(boundary, monkeypatch):
    boundary.config["sms_code"]["enabled"] = False
    launch = Mock()
    monkeypatch.setattr(route, "wake_registration_sms_queue", launch)
    result = route.enqueue_registration_sms_followup({"batch_id": "source", "auto_sms_after_register": True}, [{"email": "ok@test", "access_token": "at"}], [])
    assert result["status"] == "failed"
    assert "短信设置" in result["message"] and "启用" in result["message"]
    launch.assert_not_called()


def test_missing_queue_row_after_registration_end_is_not_pending(boundary):
    params = {"batch_id": "interrupted-registration", "auto_sms_after_register": True}
    assert route.registration_sms_snapshot(params, registration_running=False)["status"] == "interrupted"
    assert route.registration_sms_snapshot(params, registration_running=False, registration_error="internal-secret")["status"] == "failed"


def _mock_preflight(boundary, monkeypatch, callback=lambda: None):
    def preflight(wanted, _force, cfg, **_kwargs):
        callback()
        return {"not_found": [], "settings": route._safe_sms_settings(cfg)}, [{"email": email} for email in wanted]
    monkeypatch.setattr(route, "_sms_preflight", preflight)


@pytest.mark.parametrize("shutdown_point", ["preflight", "reserved"])
def test_shutdown_during_preflight_or_after_reservation_never_launches(boundary, monkeypatch, shutdown_point):
    boundary.queue.enqueue("source", ["a@test"])
    _mock_preflight(boundary, monkeypatch, boundary.state._SERVER_SHUTDOWN_EVENT.set if shutdown_point == "preflight" else lambda: None)
    if shutdown_point == "reserved":
        reserve = route._reserve_batch
        def reserve_then_stop(*args):
            event = reserve(*args)
            boundary.state._SERVER_SHUTDOWN_EVENT.set()
            return event
        monkeypatch.setattr(route, "_reserve_batch", reserve_then_stop)
    launch = Mock()
    monkeypatch.setattr(route, "_launch_batch", launch)
    route.wake_registration_sms_queue()
    launch.assert_not_called()
    assert not route._STATE["running"]
    assert not boundary.queue.snapshot("source")["batch_id"]


def test_queue_ownership_precedes_launch_and_early_finish_is_retained(boundary, monkeypatch):
    boundary.queue.enqueue("source", ["a@test"], sms_options={"max_price": "0.29", "max_phone_attempts": 2})
    _mock_preflight(boundary, monkeypatch)
    launched = []

    def immediate_finish(_target, args, *, batch_id, **_kwargs):
        snapshot = boundary.queue.snapshot("source")
        assert snapshot["batch_id"] == batch_id and snapshot["status"] == "running"
        launched.append(args)
        route._STATE.update(done=1, success=1)
        route._finish_batch(batch_id)

    monkeypatch.setattr(route, "_launch_batch", immediate_finish)
    route.wake_registration_sms_queue()
    assert len(launched) == 1
    assert launched[0][1]["max_price"] == "0.29" and launched[0][1]["max_phone_attempts"] == 2
    assert boundary.queue.snapshot("source")["status"] == "completed"


def test_worker_honors_server_shutdown_before_any_login_or_sms(boundary, monkeypatch):
    boundary.queue.enqueue("source", ["a@test"])
    _mock_preflight(boundary, monkeypatch)
    payment = Mock(side_effect=AssertionError("no real SMS/login"))
    monkeypatch.setattr(route, "_process_one", payment)
    monkeypatch.setattr(route, "_append_rt_history", Mock())

    def stopped_worker(target, args, **_kwargs):
        boundary.state._SERVER_SHUTDOWN_EVENT.set()
        target(*args)

    monkeypatch.setattr(route, "_launch_batch", stopped_worker)
    route.wake_registration_sms_queue()
    payment.assert_not_called()
    assert boundary.queue.snapshot("source")["status"] == "cancelled"


def test_launch_exception_releases_slot_and_marks_followup_failed(boundary, monkeypatch):
    boundary.queue.enqueue("source", ["a@test"])
    _mock_preflight(boundary, monkeypatch)
    monkeypatch.setattr(route.threading, "Thread", Mock(side_effect=RuntimeError("thread unavailable")))
    route.wake_registration_sms_queue()
    assert not route._STATE["running"]
    assert boundary.queue.snapshot("source")["status"] == "failed"


def test_registration_stop_after_ownership_before_worker_start_prevents_payment(boundary, monkeypatch):
    boundary.queue.enqueue("source", ["a@test"])
    _mock_preflight(boundary, monkeypatch)
    payment = Mock(side_effect=AssertionError("no real SMS/login"))
    monkeypatch.setattr(route, "_process_one", payment)
    monkeypatch.setattr(route, "_append_rt_history", Mock())

    def stop_before_thread(target, args, **_kwargs):
        route.cancel_registration_sms_followup("source")
        target(*args)

    monkeypatch.setattr(route, "_launch_batch", stop_before_thread)
    route.wake_registration_sms_queue()
    payment.assert_not_called()
    assert boundary.queue.snapshot("source")["status"] == "cancelled"


def test_registration_stop_does_not_stop_an_unrelated_manual_sms(boundary):
    stop = route._reserve_batch("codex", 1, "manual")
    route.cancel_registration_sms_followup("registration")
    assert not stop.is_set()
    assert route._STATE["status"] == "running"

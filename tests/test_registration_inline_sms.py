"""Inline registration continuations use mock OAuth/SMS and temporary ledgers."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from common import registration_sms_inline as inline
from common import registration_sms_followup as old_queue
from web.routes import codex_oauth as route

_REAL_PROCESS_ONE = route._process_one


def test_inline_429_preserves_trigger_failure_and_cancels_inflight_and_queued(boundary):
    session = boundary.session()
    second_entered = threading.Event()

    def process(rec, _cfg, _steps, **kwargs):
        if rec["email"] == "first@example.test":
            assert second_entered.wait(3)
            raise RuntimeError("HTTP 429 rate_limit_exceeded password-secret")
        second_entered.set()
        assert kwargs["stop_event"].wait(3)
        raise route._SmsCancelled("任务已停止")

    boundary.paid.side_effect = process
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(session.run, 1, account())
        two = pool.submit(session.run, 2, account("second@example.test"))
        first = one.result(5)
        second = two.result(5)
    queued = session.run(3, account("third@example.test"))
    session.close()
    assert first["status"] == "failed" and "429" in first["message"]
    assert second["status"] == queued["status"] == "cancelled"
    assert boundary.paid.call_count == boundary.history.call_count == 2
    assert route._STATE["status"] == "failed" and not route._STATE["running"]
    assert route._STATE["rate_limited"] and "429" in route._STATE["rate_limit_error"]
    assert session.snapshot()["failed"] == 1
    assert "password-secret" not in str((first, boundary.events, route._STATE["rate_limit_error"]))
    terminals = [fields for args, fields in boundary.events if args[1] in {"failed", "cancelled"}]
    assert len(terminals) == 3 and all(row["registration_status"] == "success" for row in terminals)


@pytest.fixture
def boundary(monkeypatch, tmp_path):
    monkeypatch.setenv("REGISTRATION_SMS_INLINE_FILE", str(tmp_path / "inline.sqlite3"))
    monkeypatch.setattr(inline, "_LEDGERS", {})
    monkeypatch.setattr(old_queue, "existing_queue", lambda: None)
    config = {"sms_code": {"enabled": True, "provider": "smsbower", "api_key": "supplier-secret", "max_phone_attempts": 3, "max_price": "0.42"}, "ui_settings": {"codex_sms_workers": 2}}
    state = SimpleNamespace(_read_config=lambda: deepcopy(config), _SERVER_SHUTDOWN_EVENT=threading.Event())
    monkeypatch.setattr(route, "_state", lambda: state)
    monkeypatch.setattr(route, "_STATE", {**deepcopy(route._STATE), "running": False, "batch_id": "", "options": {}, "logs": [], "items": []})
    monkeypatch.setattr(route, "_STOP", threading.Event())
    monkeypatch.setattr(route, "_LOCK", threading.Lock())
    monkeypatch.setattr(route, "_current_sms_record", lambda rec: rec)
    monkeypatch.setattr(route, "_current_relay_context", lambda _rec, context: context)
    monkeypatch.setattr(route, "_relay_context", lambda: {"configured": True, "by_email": {}, "by_file": {}})
    monkeypatch.setattr(route, "_relay_action", lambda *_args: {"action": "reauthorize", "reason": "fixture", "relay_status": "missing", "relay_file": ""})
    history = Mock()
    monkeypatch.setattr(route, "_append_rt_history", history)
    events = []
    paid = Mock(return_value={"status": "success", "refresh_token": "new-private-rt", "authorization_status": "success", "relay_status": "synced"})
    monkeypatch.setattr(route, "_process_one", paid)
    stop = threading.Event()

    def session(source="source"):
        return inline.RegistrationInlineSmsSession(source, event_callback=lambda *args, **kwargs: events.append((args, kwargs)), stop_check=stop.is_set)

    return SimpleNamespace(config=config, state=state, history=history, events=events, paid=paid, stop=stop, session=session)


def account(email="first@example.test"):
    return {"email": email, "password": "password-secret", "totp_secret": "totp-private", "access_token": "access-private", "registration_state": "complete"}


def test_account_executes_before_session_close_and_owns_shared_slot(boundary):
    session = boundary.session()

    def process(*_args, **_kwargs):
        assert session._ledger.get("source", 1)["state"] == "started"
        assert route._STATE["running"] is True
        return {"status": "success", "refresh_token": "new-private-rt", "relay_status": "synced"}

    boundary.paid.side_effect = process
    result = session.run(1, account())
    assert result["status"] == "success" and "private" not in str(result)
    assert boundary.paid.call_count == boundary.history.call_count == 1
    assert route._STATE["running"]
    with pytest.raises(route.HTTPException) as error:
        route._reserve_batch("import", 1, "manual")
    assert error.value.status_code == 409
    session.close()
    assert not route._STATE["running"] and route._STATE["status"] == "completed"


def test_final_task_success_waits_for_history_and_close_waits_for_cleanup(boundary):
    history_entered, release_history = threading.Event(), threading.Event()
    session = boundary.session()

    def persist(*_args):
        history_entered.set()
        assert release_history.wait(3)

    boundary.history.side_effect = persist
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = pool.submit(session.run, 1, account())
        assert history_entered.wait(2)
        closing = pool.submit(session.close)
        try:
            assert not closing.done() and route._STATE["running"]
            assert not any(args[1] == "success" for args, _fields in boundary.events)
        finally:
            release_history.set()
        assert result.result(2)["status"] == "success"
        closing.result(2)
    assert boundary.events[-1][0] == (1, "success")
    assert session.snapshot()["status"] == "completed"


def test_streamed_accounts_share_batch_and_keep_callbacks_on_their_own_card(boundary):
    session = boundary.session()
    both = threading.Barrier(2)

    def process(rec, _cfg, _steps, **kwargs):
        route._set_sms_item(rec["email"], kwargs["batch_id"], stage="waiting_sms", attempt=2, wait_deadline=123.0, message="等待短信")
        both.wait(3)
        return {"status": "success"}

    boundary.paid.side_effect = process
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(session.run, 1, account())
        two = pool.submit(session.run, 2, account("second@example.test"))
        assert one.result(4)["status"] == two.result(4)["status"] == "success"
    session.close()
    assert route._STATE["total"] == route._STATE["done"] == 2
    assert len({args.kwargs["batch_id"] for args in boundary.paid.call_args_list}) == 1
    for index, email in [(1, "first@example.test"), (2, "second@example.test")]:
        progress = [fields for args, fields in boundary.events if args == (index, "running") and fields.get("stage") == "waiting_sms"]
        assert progress and progress[0]["email"] == email
        assert progress[0]["attempt"] == 2 and progress[0]["wait_deadline"] == 123.0
        assert progress[0]["phase"] == "sms" and progress[0]["registration_status"] == "success"


def test_waits_for_manual_slot_without_overwriting_it(boundary):
    route._reserve_batch("import", 1, "manual")
    session = boundary.session()
    queued = threading.Event()
    original = session._emit
    session._emit = lambda *args, **kwargs: (queued.set(), original(*args, **kwargs))[-1]
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(session.run, 1, account())
        assert queued.wait(2)
        assert route._STATE["batch_id"] == "manual" and boundary.paid.call_count == 0
        route._finish_batch("manual")
        assert future.result(3)["status"] == "success"
    session.close()


def test_waiting_manual_slot_is_cancellable_without_stopping_manual_work(boundary):
    manual_stop = route._reserve_batch("import", 1, "manual")
    session = boundary.session()
    boundary.stop.set()
    result = session.run(1, account())
    session.close()
    assert result["status"] == "cancelled" and not manual_stop.is_set()
    assert route._STATE["batch_id"] == "manual" and route._STATE["running"]
    boundary.paid.assert_not_called()


def test_duplicate_account_calls_wait_for_one_execution(boundary):
    session = boundary.session()
    entered, release = threading.Event(), threading.Event()

    def process(*_args, **_kwargs):
        entered.set()
        assert release.wait(3)
        return {"status": "success"}

    boundary.paid.side_effect = process
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(session.run, 1, account())
        assert entered.wait(2)
        duplicate = pool.submit(session.run, 1, account())
        release.set()
        assert first.result(3) == duplicate.result(3)
    assert boundary.paid.call_count == 1
    assert session.run(1, account())["status"] == "success"
    assert boundary.paid.call_count == 1
    session.close()


def test_restart_started_ledger_never_reexecutes_payment(boundary, monkeypatch):
    ledger = inline._ledger()
    ledger.reserve("source", 1, "first@example.test")
    assert ledger.start("source", 1, "old-sms")
    monkeypatch.setattr(inline, "_LEDGERS", {})  # Simulate a new process.
    session = boundary.session()
    result = session.run(1, account())
    session.close()
    assert result["status"] == "failed" and result["sms_status"] == "interrupted"
    assert session.snapshot()["status"] == "interrupted"
    assert "未自动重复" in result["message"]
    boundary.paid.assert_not_called()


def test_completed_ledger_is_idempotent_across_session_objects(boundary):
    first = boundary.session()
    expected = first.run(1, account())
    first.close()
    second = boundary.session()
    assert second.run(1, account()) == expected
    second.close()
    assert boundary.paid.call_count == 1


@pytest.mark.parametrize("stop_kind", ["global", "task", "shutdown", "manual"])
def test_active_work_receives_composite_stop_and_finishes_cleanup(boundary, stop_kind):
    session = boundary.session()
    task_stop = threading.Event()
    entered, cleanup = threading.Event(), threading.Event()

    def process(*_args, stop_event, **_kwargs):
        entered.set()
        assert stop_event.wait(3)
        cleanup.set()
        raise route._SmsCancelled("fixture cancelled after refund")

    boundary.paid.side_effect = process
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(session.run, 1, account(), task_stop.is_set)
        assert entered.wait(2)
        {"global": boundary.stop, "task": task_stop, "shutdown": boundary.state._SERVER_SHUTDOWN_EVENT, "manual": route._STOP}[stop_kind].set()
        assert future.result(4)["status"] == "cancelled"
    assert cleanup.is_set()
    session.close()
    assert boundary.history.call_count == 1


def test_configured_permits_limit_active_sms_and_waiting_task_can_cancel(boundary):
    boundary.config["ui_settings"]["codex_sms_workers"] = 1
    session = boundary.session()
    entered, release, second_stop = threading.Event(), threading.Event(), threading.Event()

    def process(*_args, **_kwargs):
        entered.set()
        assert release.wait(3)
        return {"status": "success"}

    boundary.paid.side_effect = process
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(session.run, 1, account())
        assert entered.wait(2)
        second_stop.set()
        second = pool.submit(session.run, 2, account("second@example.test"), second_stop.is_set)
        try:
            assert second.result(2)["status"] == "cancelled"
            assert boundary.paid.call_count == 1
        finally:
            release.set()
        assert first.result(2)["status"] == "success"
    session.close()


def test_payment_never_starts_when_durable_started_write_fails(boundary, monkeypatch):
    session = boundary.session()
    monkeypatch.setattr(session._ledger, "start", Mock(side_effect=OSError("disk unavailable")))
    assert session.run(1, account())["status"] == "failed"
    session.close()
    boundary.paid.assert_not_called()
    assert route._STATE["status"] == "failed"


def test_failure_messages_callbacks_and_ledger_redact_secrets(boundary):
    session = boundary.session()
    boundary.paid.side_effect = RuntimeError("password-secret supplier-secret access_token=very-private-token code=123456")
    result = session.run(1, account())
    session.close()
    combined = str(result) + str(boundary.events) + str(session._ledger.get("source", 1)) + str(route._STATE) + str(boundary.history.call_args)
    assert result["status"] == "failed"
    assert all(secret not in combined for secret in ("password-secret", "supplier-secret", "very-private-token", "123456"))


def test_ui_callback_failure_does_not_retry_successful_sms(boundary):
    session = inline.RegistrationInlineSmsSession("source", event_callback=Mock(side_effect=RuntimeError("ui unavailable")), stop_check=lambda: False)
    assert session.run(1, account())["status"] == "success"
    session.close()
    assert boundary.paid.call_count == boundary.history.call_count == 1


def test_inline_events_preserve_zero_unlimited_phone_attempt_setting(boundary):
    boundary.config["sms_code"]["max_phone_attempts"] = 0
    session = boundary.session()
    assert session.run(1, account())["status"] == "success"
    session.close()
    assert boundary.events and all(fields["max_phone_attempts"] == 0 for _args, fields in boundary.events)


def test_stop_from_acquiring_stage_callback_prevents_purchase(boundary, monkeypatch):
    from test_sms_workflow import fake_login
    calls = fake_login(route, monkeypatch)
    monkeypatch.setattr(route, "totp_from_secret", lambda *_args, **_kwargs: "fixture-code")
    stop = route._reserve_batch("codex", 1, "fixture-batch")
    route._STATE["items"] = [{"email": "first@example.test", "status": "pending"}]

    def callback(**fields):
        if fields.get("stage") == "acquiring":
            stop.set()

    token = route._SMS_EVENT_CALLBACK.set(callback)
    try:
        with pytest.raises(route._SmsCancelled):
            _REAL_PROCESS_ONE(account(), boundary.config["sms_code"], stop_event=stop, batch_id="fixture-batch")
    finally:
        route._SMS_EVENT_CALLBACK.reset(token)
    assert calls.acquire == 0


@pytest.mark.parametrize("stop_point", ["prices", "first_unavailable_tier"])
def test_provider_checks_stop_before_each_paid_number_request(boundary, monkeypatch, stop_point):
    stop = threading.Event()
    settings = route.SmsSettings.from_mapping(boundary.config["sms_code"])
    client = route._PanelSmsBowerClient(settings, [("6", ["10", "20"], "")])
    client.stop_check = stop.is_set

    def prices(*_args):
        if stop_point == "prices":
            stop.set()
        return {}

    def unavailable(*_args, **_kwargs):
        stop.set()
        raise route.SmsError("NO_NUMBERS")

    monkeypatch.setattr(route, "_smsbower_tier_prices", prices)
    client._request = Mock(side_effect=unavailable)
    with pytest.raises(route._SmsCancelled):
        client.acquire()
    assert client._request.call_count == (0 if stop_point == "prices" else 1)


def test_credentials_refreshed_from_store_are_redacted_everywhere(boundary, monkeypatch):
    session = boundary.session()
    new_secret = "updated-private-account-password"
    monkeypatch.setattr(route, "_current_sms_record", lambda rec: {**rec, "password": new_secret})
    boundary.paid.side_effect = RuntimeError(f"upstream error {new_secret}")
    result = session.run(1, account())
    session.close()
    combined = str(result) + str(boundary.events) + str(session._ledger.get("source", 1)) + str(route._STATE) + str(boundary.history.call_args)
    assert new_secret not in combined


def test_inline_authorize_backoff_keeps_card_and_ledger_running_until_success(boundary, monkeypatch):
    from test_sms_workflow import fake_login

    calls = fake_login(route, monkeypatch)
    monkeypatch.setattr(route, "_process_one", _REAL_PROCESS_ONE)
    monkeypatch.setattr(route, "totp_from_secret", lambda *_args, **_kwargs: "fixture-code")
    monkeypatch.setattr(route, "exchange_code", lambda *_args: {"refresh_token": "fixture-rt"})
    monkeypatch.setattr(route, "_persist_sms_tokens", lambda *_args: {"base_hash": None})
    monkeypatch.setattr(route, "_sync_cliproxy_auth", lambda *_args, **_kwargs: "fixture.json")
    monkeypatch.setattr(route, "_mark_relay_synced", lambda *_args: None)
    now = [1000.0]
    monkeypatch.setattr(route.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(route.time, "time", lambda: 1700000000 + now[0])
    session = boundary.session()
    submitted = []

    def submit(client):
        submitted.append((id(client), now[0]))
        if len(submitted) == 1:
            raise RuntimeError("HTTP 429 /api/accounts/authorize/continue: rate_limit_exceeded")

    def wait(stop, seconds):
        assert session._ledger.get("source", 1)["state"] == "started"
        assert boundary.history.call_count == calls.acquire == 0
        assert not any(args[1] in {"failed", "success", "cancelled"} for args, _ in boundary.events)
        assert not stop.is_set()
        now[0] += seconds
        return False

    monkeypatch.setattr(route.ProtocolClient, "submit_email", submit)
    monkeypatch.setattr(inline._CombinedStop, "wait", wait)
    result = session.run(1, account())
    session.close()
    assert result["status"] == "success" and submitted[0][0] == submitted[1][0]
    assert submitted[1][1] - submitted[0][1] == 90
    assert calls.login == calls.closed == calls.acquire == boundary.history.call_count == 1
    waiting = [fields for args, fields in boundary.events if fields.get("stage") == "rate_limit_wait"]
    assert waiting and all(fields["sms_status"] == "running" for fields in waiting)
    assert boundary.events[-1][0] == (1, "success")
    assert route._STATE["status"] == "completed"

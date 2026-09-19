"""Offline regressions for import validation, retries, cancellation and shutdown."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


SECRET = "JBSWY3DPEHPK3PXP"
EMAIL = "member@example.test"
TOKENS = {"access_token": "synthetic-access-token"}


@pytest.fixture
def boundary(monkeypatch):
    import register.protocol as protocol
    import web.routes.codex_oauth as route

    state = deepcopy(route._STATE)
    state.update(running=False, status="idle", batch_id="", logs=[], results=[], cancelled=0, done=0, success=0, failed=0)
    monkeypatch.setattr(route, "_STATE", state)
    monkeypatch.setattr(route, "_LOCK", threading.Lock())
    monkeypatch.setattr(route, "_STOP", threading.Event())
    monkeypatch.setattr(route, "_BATCH_THREAD", None)
    monkeypatch.setattr(route, "_state", lambda: SimpleNamespace(_read_config=lambda: {
        "proxy": {"enabled": False}, "registration": {"protocol_account_timeout": 31},
    }))
    login = Mock(return_value=dict(TOKENS))
    persist = Mock(return_value=dict(TOKENS))
    history = Mock()
    monkeypatch.setattr(protocol, "login_with_password_totp_one", login)
    monkeypatch.setattr(route, "_persist_imported_account", persist)
    monkeypatch.setattr(route, "_append_import_history", history)
    monkeypatch.setattr(route, "_rotate_import_history", Mock())
    stop = threading.Event()
    monkeypatch.setattr(stop, "wait", Mock(return_value=False))
    return SimpleNamespace(route=route, login=login, persist=persist, history=history, stop=stop)


def _run(boundary):
    return boundary.route._import_one(1, EMAIL, "synthetic-password", SECRET, stop_event=boundary.stop)


def _fail_login(boundary, reason):
    def fail(*_args, **kwargs):
        kwargs["error_sink"](reason)
        return None
    boundary.login.side_effect = fail


def test_parser_accepts_comments_and_normalizes_base32_without_changing_password(boundary):
    rows = boundary.route._parse_credential_lines(
        f"# note\n\n {EMAIL} ---- fixture-pass with space ---- jbsw y3dp ehpk 3pxp \n"
    )
    assert rows == [(EMAIL, "fixture-pass with space", SECRET)]


@pytest.mark.parametrize("invalid_line,description", [
    ("broken-line", "格式错误"),
    (f"not-an-email----fixture-secret-password----{SECRET}", "邮箱格式错误"),
    (f"second@example.test----fixture-secret-password----INVALID!SECRET", "Base32"),
    (f"{EMAIL.upper()}----fixture-secret-password----{SECRET}", "邮箱重复"),
])
def test_invalid_or_duplicate_row_rejects_entire_batch_without_partial_launch(boundary, monkeypatch, invalid_line, description):
    launch = Mock()
    monkeypatch.setattr(boundary.route, "_launch_batch", launch)
    body = boundary.route.CodexOauthImportRequest(text=f"{EMAIL}----fixture-secret-password----{SECRET}\n{invalid_line}")

    with pytest.raises(boundary.route.HTTPException) as raised:
        asyncio.run(boundary.route.codex_oauth_import(body))

    assert raised.value.status_code == 400
    assert "第 2 行" in raised.value.detail and description in raised.value.detail
    assert "fixture-secret-password" not in raised.value.detail and SECRET not in raised.value.detail
    launch.assert_not_called()
    boundary.login.assert_not_called()
    assert boundary.route._STATE["running"] is False


def test_transport_error_retries_once_and_keeps_single_account_deadline(boundary):
    def login(*_args, **kwargs):
        if boundary.login.call_count == 1:
            kwargs["error_sink"]("curl: (35) TLS connect error")
            return None
        return dict(TOKENS)
    boundary.login.side_effect = login

    result = _run(boundary)

    assert result["status"] == "success"
    assert boundary.login.call_count == 2
    boundary.persist.assert_called_once()
    boundary.stop.wait.assert_called_once()
    first, second = [call.kwargs["protocol_runtime"] for call in boundary.login.call_args_list]
    assert first.deadline == second.deadline and first.account_timeout == second.account_timeout == 31
    assert any("最后一次" in row["m"] for row in result["logs"])


def test_repeated_transport_failure_has_two_attempts_and_no_persistence(boundary):
    _fail_login(boundary, "curl: (28) Operation timed out")
    result = _run(boundary)
    assert result["status"] == "failed"
    assert boundary.login.call_count == 2
    boundary.stop.wait.assert_called_once()
    boundary.persist.assert_not_called()


@pytest.mark.parametrize("reason", [
    "cf_blocked: challenge rejected; curl: (35) TLS connect error",
    "chatgpt_home_http_403: forbidden",
    "chatgpt_csrf_http_403: transport timeout curl: (28)",
    "HTTP 429 rate_limit_exceeded; curl: (28) Operation timed out",
    "HTTP 401 invalid_credentials; curl: (35) TLS connect error",
    "incorrect_code: curl: (28) Operation timed out",
    "invalid_auth_step: curl: (35) TLS connect error",
    "team_workspace_id_mismatch: unexpected workspace",
])
def test_authentication_denials_and_throttling_are_not_retried(boundary, reason):
    _fail_login(boundary, reason)
    result = _run(boundary)
    assert result["status"] == "failed"
    assert result["error"] == reason
    boundary.login.assert_called_once()
    boundary.stop.wait.assert_not_called()
    boundary.persist.assert_not_called()
    assert not any("最后一次" in row["m"] for row in result["logs"])


def test_pre_cancelled_account_never_starts_login(boundary):
    boundary.stop.set()
    assert _run(boundary)["status"] == "cancelled"
    boundary.login.assert_not_called()
    boundary.persist.assert_not_called()


def test_cancellation_interrupts_retry_backoff_before_second_login(boundary):
    _fail_login(boundary, "curl: (28) Operation timed out")
    boundary.stop.wait.side_effect = lambda _delay: boundary.stop.set() or True
    assert _run(boundary)["status"] == "cancelled"
    boundary.login.assert_called_once()
    boundary.persist.assert_not_called()


def test_cancellation_after_login_but_before_persistence_does_not_save(boundary):
    def login(*_args, **_kwargs):
        boundary.stop.set()
        return dict(TOKENS)
    boundary.login.side_effect = login
    assert _run(boundary)["status"] == "cancelled"
    boundary.persist.assert_not_called()


def test_stop_racing_with_completed_persistence_does_not_relabel_success(boundary):
    def persisted(*_args):
        boundary.stop.set()
        return dict(TOKENS)
    boundary.persist.side_effect = persisted
    result = _run(boundary)
    assert result["status"] == "success" and result["error"] == ""
    boundary.persist.assert_called_once()


def test_batch_counts_cancellation_separately_and_retains_all_history(boundary, monkeypatch):
    stop = boundary.route._reserve_batch("import", 3, "incident-batch")
    outcomes = {1: "success", 2: "failed", 3: "cancelled"}
    monkeypatch.setattr(boundary.route, "_import_one", lambda index, email, *_args, **_kwargs: {
        "email": email, "status": outcomes[index], "error": "", "logs": [],
    })
    credentials = [(f"member-{index}@example.test", "fixture-password", SECRET) for index in range(1, 4)]
    boundary.route._run_import_batch(credentials, concurrency=2, batch_id="incident-batch", stop_event=stop)

    state = boundary.route._STATE
    assert (state["done"], state["success"], state["failed"], state["cancelled"]) == (3, 1, 1, 1)
    assert state["running"] is False and state["status"] == "cancelled"
    assert {call.args[0]["status"] for call in boundary.history.call_args_list} == set(outcomes.values())


def test_shutdown_signals_the_reserved_worker_and_returns_thread_for_join(boundary, monkeypatch):
    class Worker:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.start = Mock()

    stop = boundary.route._reserve_batch("import", 1, "shutdown-batch")
    monkeypatch.setattr(boundary.route, "threading", SimpleNamespace(Thread=Worker))
    target = Mock()
    boundary.route._launch_batch(target, (stop,), batch_id="shutdown-batch", stop_event=stop, name="incident-import")
    worker = boundary.route.request_batch_shutdown()

    assert worker is boundary.route._BATCH_THREAD
    worker.start.assert_called_once()
    assert worker.kwargs["target"] is target and worker.kwargs["args"] == (stop,)
    assert stop.is_set()
    assert boundary.route._STATE["status"] == "stopping"
    boundary.route._finish_batch("shutdown-batch")
    assert boundary.route._STATE["status"] == "cancelled"


def test_worker_start_failure_releases_reservation_for_later_retry(boundary, monkeypatch):
    stop = boundary.route._reserve_batch("import", 1, "failed-start")
    worker = SimpleNamespace(start=Mock(side_effect=RuntimeError("synthetic start failure")))
    monkeypatch.setattr(boundary.route, "threading", SimpleNamespace(Thread=Mock(return_value=worker), Event=threading.Event))
    with pytest.raises(RuntimeError, match="synthetic start failure"):
        boundary.route._launch_batch(Mock(), (), batch_id="failed-start", stop_event=stop, name="incident-import")
    assert stop.is_set() and boundary.route._STATE["running"] is False
    assert boundary.route._STATE["status"] == "failed"
    # API-level cleanup may finish the batch again after startup has failed.
    boundary.route._finish_batch("failed-start")
    assert boundary.route._STATE["status"] == "failed"
    assert not boundary.route._reserve_batch("import", 1, "new-batch").is_set()

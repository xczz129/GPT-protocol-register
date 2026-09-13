"""Synthetic regression coverage for stale sessions and Sentinel routes."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import codex_oauth.import_jobs as imports
import codex_oauth.oauth as oauth
from codex_oauth.sms import SmsSettings


def test_stale_phone_session_leaves_phone_loop_instead_of_waiting_again():
    manager = imports.ImportJobManager.__new__(imports.ImportJobManager)
    manager.store = SimpleNamespace(update=Mock())
    manager.skip_phone_required = False
    manager.timeout = 5
    manager._check_cancel = Mock()
    manager._sms_settings = Mock(return_value=SmsSettings())
    manager._wait_input_or_provider = Mock(return_value="+10000000000")
    manager._wait_input = Mock(side_effect=AssertionError("old session must not wait again"))
    manager._set_step = Mock()
    job = imports.ImportJob(id="job", account_id="account", email="a@example.test", batch_id="batch")
    client = SimpleNamespace(
        last_auth_response={},
        send_phone=Mock(side_effect=RuntimeError("HTTP 409: sign-in session is no longer valid")),
    )

    with pytest.raises(imports.AuthSessionRestartRequired):
        manager._phone_step(job, client)

    client.send_phone.assert_called_once()
    manager._wait_input.assert_not_called()


def test_provider_cancel_failure_does_not_hide_stale_phone_session(monkeypatch):
    manager = imports.ImportJobManager.__new__(imports.ImportJobManager)
    manager.store = SimpleNamespace(update=Mock())
    manager.skip_phone_required = False
    manager.timeout = 5
    manager._check_cancel = Mock()
    manager._sms_settings = Mock(return_value=SimpleNamespace(provider_label="Synthetic", max_phone_attempts=2))
    manager._set_step = Mock()
    activation = SimpleNamespace(phone="+10000000000", country="test", finished=False)
    provider = SimpleNamespace(acquire=Mock(return_value=activation), cancel=Mock(side_effect=RuntimeError("provider unavailable")))
    monkeypatch.setattr(imports, "_sms_client_for", lambda *_args: provider)
    job = imports.ImportJob(id="job", account_id="account", email="a@example.test", batch_id="batch")
    client = SimpleNamespace(last_auth_response={}, send_phone=Mock(side_effect=RuntimeError("sign-in session is no longer valid")))

    with pytest.raises(imports.AuthSessionRestartRequired):
        manager._phone_step(job, client)

    provider.cancel.assert_called_once_with(activation)


def test_stale_session_retry_closes_old_client_before_new_client(monkeypatch):
    manager = imports.ImportJobManager.__new__(imports.ImportJobManager)
    job = imports.ImportJob(id="job", account_id="account", email="a@example.test", batch_id="batch")
    manager._lock = threading.RLock()
    manager._jobs = {job.id: job}
    manager.timeout = 5
    manager.store = SimpleNamespace(
        get=Mock(return_value=SimpleNamespace(id="account", email=job.email, password="synthetic", totp_secret="synthetic")),
        update=Mock(),
    )
    manager._persist_jobs = Mock()
    manager._persist_jobs_locked = Mock()
    manager._acquire_worker_slot = Mock()
    manager._release_worker_slot = Mock()
    manager._set_step = Mock()
    events = []

    def client_factory(*_args, **_kwargs):
        index = sum(event.startswith("create") for event in events)
        events.append(f"create-{index}")
        error = (imports.AuthSessionRestartRequired("sign-in session is no longer valid")
                 if index == 0 else RuntimeError("invalid password"))
        return SimpleNamespace(
            bootstrap=Mock(side_effect=error),
            session=SimpleNamespace(close=lambda: events.append(f"close-{index}")),
        )

    monkeypatch.setattr(imports, "ProtocolClient", client_factory)
    monkeypatch.setattr(imports.time, "sleep", Mock())
    manager._run_job(job.id)

    assert events == ["create-0", "close-0", "create-1", "close-1"]
    assert job.network_retries == 1
    assert job.status == "failed"  # The second, explicit credential failure stops.


def _sentinel_client(monkeypatch, responses):
    client = oauth.ProtocolClient.__new__(oauth.ProtocolClient)
    client.device_id = "synthetic-device"
    client.timeout = 5
    client.session = SimpleNamespace(post=Mock(side_effect=responses))
    monkeypatch.setattr(oauth, "generate_requirements_token", lambda _device: "requirements")
    monkeypatch.setattr(oauth, "build_sentinel_request_body", lambda *_args: "body")
    monkeypatch.setattr(oauth, "generate_sentinel_token", lambda *_args: "sentinel-token")
    sleeps = Mock()
    monkeypatch.setattr(oauth.time, "sleep", sleeps)
    return client, sleeps


def test_sentinel_tls_failure_retries_with_bounded_backoff(monkeypatch):
    response = SimpleNamespace(status_code=200, json=lambda: {"challenge": "synthetic"})
    client, sleeps = _sentinel_client(monkeypatch, [
        RuntimeError("curl: (35) TLS connect error"),
        RuntimeError("connection reset by peer"),
        response,
    ])
    assert client.sentinel_header("authorize_continue") == "sentinel-token"
    assert client.session.post.call_count == 3
    assert [call.args[0] for call in sleeps.call_args_list] == [0.25, 0.5]


def test_sentinel_auth_failure_is_not_retried(monkeypatch):
    response = SimpleNamespace(status_code=401, text="invalid credential")
    client, sleeps = _sentinel_client(monkeypatch, [response])
    with pytest.raises(RuntimeError, match="Sentinel HTTP 401"):
        client.sentinel_header("password_verify")
    assert client.session.post.call_count == 1
    sleeps.assert_not_called()


def test_sentinel_token_runner_timeout_does_not_repeat_http_request(monkeypatch):
    response = SimpleNamespace(status_code=200, json=lambda: {"challenge": "synthetic"})
    client, sleeps = _sentinel_client(monkeypatch, [response])
    monkeypatch.setattr(oauth, "generate_sentinel_token", Mock(side_effect=RuntimeError("Node runner timeout")))
    with pytest.raises(RuntimeError, match="Node runner timeout"):
        client.sentinel_header("authorize_continue")
    assert client.session.post.call_count == 1
    sleeps.assert_not_called()

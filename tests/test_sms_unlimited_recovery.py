"""No real provider, OAuth, or network calls are made by these regressions."""

import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.request import Request

import pytest
from curl_cffi.requests.cookies import Cookies
from pydantic import ValidationError

from codex_oauth.oauth import ProtocolClient
from codex_oauth.sms import SmsActivation, SmsBowerClient, SmsError, SmsSettings
from test_sms_workflow import route, fake_login, account, config


def immediate_event():
    return SimpleNamespace(is_set=lambda: False, wait=lambda _seconds: False)


@pytest.mark.parametrize("value", [0, 1, 20, 130, 1000000, "0", "130", None])
def test_number_limit_keeps_zero_and_every_positive_value(route, value):
    expected = int(value or 0)
    assert SmsSettings.from_mapping({"max_phone_attempts": value}).max_phone_attempts == expected
    if value is not None:
        assert route.SmsConfigUpdate(max_phone_attempts=value).max_phone_attempts == expected
        assert route.SmsRunOptions(max_phone_attempts=value).max_phone_attempts == expected


def test_number_limit_defaults_to_unlimited_through_saved_and_run_settings(route):
    assert SmsSettings().max_phone_attempts == 0
    assert SmsSettings.from_mapping({}).max_phone_attempts == 0
    assert route.SmsConfigUpdate().max_phone_attempts == 0
    assert route._sms_config_section({})["max_phone_attempts"] == 0
    effective = route._effective_sms_config({"sms_code": {"max_phone_attempts": 130}}, {"max_phone_attempts": 0})
    assert effective["max_phone_attempts"] == 0
    assert route._safe_sms_settings(effective)["max_phone_attempts"] == 0
    assert SmsSettings.from_mapping(effective).timeout_seconds == 180


@pytest.mark.parametrize("value", [-1, "-1", "invalid", 1.5])
def test_bad_number_limits_are_rejected_instead_of_becoming_unlimited(route, value):
    with pytest.raises(ValueError):
        SmsSettings.from_mapping({"max_phone_attempts": value})
    with pytest.raises(ValueError):
        SmsSettings(max_phone_attempts=value)
    with pytest.raises(ValidationError):
        route.SmsConfigUpdate(max_phone_attempts=value)
    with pytest.raises(ValidationError):
        route.SmsRunOptions(max_phone_attempts=value)
    with pytest.raises(route.HTTPException):
        route._effective_sms_config({}, {"max_phone_attempts": value})


def test_unlimited_number_attempts_can_pass_fifty_without_hidden_loop_limit(route, monkeypatch):
    calls = fake_login(route, monkeypatch, send_failures=52)
    steps, attempts = [], []
    result = route._process_one(account(), config(0), steps, phone_attempts=attempts)
    assert result["status"] == "success"
    assert calls.acquire == calls.login == calls.closed == 53
    assert len(attempts) == 53
    assert any("53/不限" in row["m"] for row in steps)


def test_cancel_after_repeated_stale_sessions_releases_one_retained_number(route, monkeypatch):
    calls = fake_login(route, monkeypatch, send_failures=100, send_error="HTTP 409: sign-in session is no longer valid")
    stopped = {"value": False, "waits": 0}

    def wait(_seconds):
        stopped["waits"] += 1
        stopped["value"] = stopped["waits"] >= 4
        return stopped["value"]

    event = SimpleNamespace(is_set=lambda: stopped["value"], wait=wait)
    attempts = []
    with pytest.raises(route._SmsCancelled):
        route._process_one(account(), config(0), stop_event=event, phone_attempts=attempts)
    assert calls.acquire == calls.cancelled == 1
    assert calls.login == calls.closed == 4
    assert attempts[0]["result"] == "cancelled"


@pytest.mark.parametrize("limit", [1, 2])
def test_expired_retained_lease_obeys_existing_number_budget(route, monkeypatch, limit):
    calls = fake_login(route, monkeypatch, send_failures=1, send_error="sign-in session is no longer valid")
    sms = route._sms_client(None)
    sms.is_active = Mock(return_value=False)
    monkeypatch.setattr(route, "_sms_client", lambda *_: sms)
    if limit == 1:
        with pytest.raises(RuntimeError, match="取号上限"):
            route._process_one(account(), config(limit), stop_event=immediate_event())
    else:
        assert route._process_one(account(), config(limit), stop_event=immediate_event())["status"] == "success"
    assert calls.acquire == limit
    assert calls.cancelled == 1
    sms.is_active.assert_called_once()


def test_permanent_password_failure_during_recovery_stops_and_releases_lease(route, monkeypatch):
    calls = fake_login(route, monkeypatch, send_failures=1, send_error="sign-in session is no longer valid")
    original = route.ProtocolClient

    def factory(*args, **kwargs):
        client = original(*args, **kwargs)
        if calls.login > 1:
            client.verify_password = Mock(side_effect=RuntimeError("invalid password"))
        return client

    monkeypatch.setattr(route, "ProtocolClient", factory)
    with pytest.raises(RuntimeError, match="invalid password"):
        route._process_one(account(), config(0), stop_event=immediate_event())
    assert calls.acquire == calls.cancelled == 1
    assert calls.login == calls.closed == 2


def test_unavailable_supplier_does_not_loop_buying_numbers(route, monkeypatch):
    fake_login(route, monkeypatch)
    provider = SimpleNamespace(acquire=Mock(side_effect=SmsError("NO_NUMBERS")))
    monkeypatch.setattr(route, "_sms_client", lambda *_: provider)
    with pytest.raises(SmsError, match="NO_NUMBERS"):
        route._process_one(account(), config(0))
    provider.acquire.assert_called_once()


def _response(url="https://auth.openai.com/add-phone", *, status=200, headers=None, cookies=None):
    return SimpleNamespace(status_code=status, url=url, headers=headers or {}, cookies=cookies,
                           text="", json=lambda: {})


def test_server_device_cookie_rotation_reconciles_cookie_header_and_device_header():
    client = ProtocolClient("fixture@example.invalid")
    try:
        assert len([cookie for cookie in client.session.cookies.jar if cookie.name == "oai-did"]) == 1
        client.session.cookies.set("oai-did", "stale-broad-fixture", domain=".openai.com")
        client.session.cookies.set("oai-did", "stale-auth-fixture", domain=".auth.openai.com")
        response_cookies = Cookies()
        response_cookies.set("oai-did", "server-device-fixture", domain=".openai.com")
        client._update_navigation(_response(cookies=response_cookies, headers={"x-openai-document-navigation-id": "server-nav"}))
        request = Request("https://auth.openai.com/api/accounts/add-phone/send")
        client.session.cookies.jar.add_cookie_header(request)
        cookie_header = str(request.get_header("Cookie"))
        assert cookie_header.count("oai-did=") == 1
        assert "oai-did=server-device-fixture" in cookie_header
        assert client.device_id == "server-device-fixture"
        client.session.post = Mock(return_value=_response())
        client.send_phone("+15550000000")
        headers = client.session.post.call_args.kwargs["headers"]
        assert headers["oai-device-id"] == "server-device-fixture"
        assert headers["x-openai-document-navigation-id"] == "server-nav"
    finally:
        client.session.close()


def test_device_cookie_set_cookie_header_fallback_uses_server_scope():
    client = ProtocolClient("fixture@example.invalid")
    try:
        client._update_navigation(_response(headers={"set-cookie": "oai-did=rotated-fixture; Domain=.openai.com; Path=/; Secure"}))
        assert client.device_id == "rotated-fixture"
        assert len([cookie for cookie in client.session.cookies.jar if cookie.name == "oai-did"]) == 1
    finally:
        client.session.close()


def test_navigation_cookie_rotation_invalidates_only_stale_decoded_session():
    client = ProtocolClient("fixture@example.invalid")
    try:
        client.auth_session_data = {"workspaces": [{"id": "old-workspace"}]}
        client.session.cookies.set("oai-client-auth-session", "new-signed-fixture", domain="auth.openai.com")
        client._update_navigation(_response())
        assert client.auth_session_data == {}
        assert client._auth_cookie_value("oai-client-auth-session") == "new-signed-fixture"
    finally:
        client.session.close()


@pytest.mark.parametrize("final_path", ["/log-in", "/log-in/password", "/mfa-challenge"])
def test_phone_navigation_rejects_login_redirect_even_when_final_http_is_200(final_path):
    client = ProtocolClient("fixture@example.invalid")
    try:
        client.session.get = Mock(side_effect=[
            _response(status=302, headers={"location": final_path}),
            _response(url="https://auth.openai.com" + final_path),
        ])
        with pytest.raises(RuntimeError, match="signin_session_invalid"):
            client.navigate("https://auth.openai.com/add-phone")
        assert client.session.get.call_count == 2
    finally:
        client.session.close()


def test_phone_navigation_accepts_only_the_real_phone_page():
    client = ProtocolClient("fixture@example.invalid")
    try:
        client.session.get = Mock(return_value=_response())
        assert client.navigate("https://auth.openai.com/add-phone").status_code == 200
        client.session.get = Mock(return_value=_response(url="https://auth.openai.com/phone-verification"))
        with pytest.raises(RuntimeError, match="phone_navigation_unexpected_step"):
            client.navigate("https://auth.openai.com/add-phone")
    finally:
        client.session.close()


@pytest.mark.parametrize("body,active", [("STATUS_WAIT_CODE", True), ("STATUS_WAIT_RETRY", True), ("STATUS_CANCEL", False), ("STATUS_OK:123456", False)])
def test_smsbower_reuse_checks_the_existing_activation_only(body, active):
    provider = SmsBowerClient(SmsSettings(api_key="fixture-key"))
    provider._request = Mock(return_value=body)
    activation = SmsActivation("lease-fixture", "+15550000000", 6, "fixture-key")
    assert provider.is_active(activation) is active
    provider._request.assert_called_once_with("getStatus", id="lease-fixture")


def test_removed_provider_cannot_reuse_credentials_for_smsbower():
    settings = SmsSettings.from_mapping({"enabled": True, "provider": "firefox", "token": "old-token", "api_key": "old-key"})
    assert settings.enabled is False and settings.api_key == ""
    with pytest.raises(SmsError, match="仅支持 SMSBower"):
        SmsBowerClient(settings)


def _import_phone_fixture(monkeypatch, limit=0):
    import codex_oauth.import_jobs as imports

    manager = imports.ImportJobManager.__new__(imports.ImportJobManager)
    manager.store = SimpleNamespace(update=Mock())
    manager.skip_phone_required = False
    manager.timeout = 5
    manager._set_step = Mock()
    manager._sms_settings = Mock(return_value=SmsSettings(enabled=True, api_key="fixture-key", max_phone_attempts=limit))
    manager._wait_input = Mock(side_effect=AssertionError("automatic provider must not wait for manual input"))
    job = imports.ImportJob(id="job", account_id="account", email="fixture@example.invalid", batch_id="batch")
    activation = SmsActivation("lease-fixture", "+15550000000", 6, "fixture-key")
    provider = SimpleNamespace(acquire=Mock(return_value=activation), is_active=Mock(return_value=True),
                               wait_code=Mock(return_value="123456"), finish=Mock(), cancel=Mock())
    monkeypatch.setattr(imports, "_sms_client_for", lambda *_: provider)
    return imports, manager, job, provider, activation


def _import_phone_client(error=None):
    return SimpleNamespace(
        last_auth_response={"continue_url": "https://auth.openai.com/add-phone"},
        navigate=Mock(), send_phone=Mock(side_effect=error, return_value=SimpleNamespace(json=lambda: {"page": {"type": "phone_verification"}})),
        submit_phone_otp=Mock(), consent_probe=Mock(),
    )


def test_import_session_restart_keeps_unsent_activation_and_number_budget(monkeypatch):
    imports, manager, job, provider, activation = _import_phone_fixture(monkeypatch, 1)
    first = _import_phone_client(RuntimeError("sign-in session is no longer valid"))
    with pytest.raises(imports.AuthSessionRestartRequired):
        manager._phone_step(job, first)
    assert job._sms_activation is activation
    second = _import_phone_client()
    manager._phone_step(job, second)
    provider.acquire.assert_called_once()
    provider.cancel.assert_not_called()
    provider.is_active.assert_called_once_with(activation)
    assert job.phone_attempts == 1
    assert job._sms_activation is None
    first.send_phone.assert_called_once_with(activation.phone)
    second.send_phone.assert_called_once_with(activation.phone)


def test_import_unlimited_rejected_number_requires_fresh_authorization(monkeypatch):
    imports, manager, job, provider, _activation = _import_phone_fixture(monkeypatch)
    first = _import_phone_client(RuntimeError("invalid_phone_number"))
    with pytest.raises(imports.PhoneNumberRestartRequired):
        manager._phone_step(job, first)
    manager._phone_step(job, _import_phone_client())
    assert provider.acquire.call_count == 2
    assert job.phone_attempts == 2
    first.send_phone.assert_called_once()


def test_import_provider_unavailable_stops_without_waiting_for_manual_input(monkeypatch):
    _imports, manager, job, provider, _activation = _import_phone_fixture(monkeypatch)
    provider.acquire.side_effect = SmsError("NO_NUMBERS")
    with pytest.raises(SmsError, match="NO_NUMBERS"):
        manager._phone_step(job, _import_phone_client())
    manager._wait_input.assert_not_called()


def test_import_retained_activation_is_not_serialized(monkeypatch):
    _imports, _manager, job, provider, activation = _import_phone_fixture(monkeypatch)
    job._sms_activation = activation
    job._sms_client = provider
    text = json.dumps(job.as_persisted())
    assert "fixture-key" not in text
    assert "lease-fixture" not in text
    assert "_sms_activation" not in text


def _import_runner_fixture(monkeypatch):
    import codex_oauth.import_jobs as imports

    manager = imports.ImportJobManager.__new__(imports.ImportJobManager)
    job = imports.ImportJob(id="job", account_id="account", email="fixture@example.invalid", batch_id="batch")
    manager._lock = threading.RLock()
    manager._jobs = {job.id: job}
    manager.timeout = 5
    manager.store = SimpleNamespace(
        get=Mock(return_value=SimpleNamespace(id="account", email=job.email, password="fixture-password", totp_secret="fixture-totp")),
        update=Mock(),
    )
    manager._persist_jobs = Mock()
    manager._persist_jobs_locked = Mock()
    manager._acquire_worker_slot = Mock()
    manager._release_worker_slot = Mock()
    manager._set_step = Mock()
    return imports, manager, job


def test_import_supervisor_recovers_more_than_three_sessions_without_recursion(monkeypatch):
    imports, manager, job = _import_runner_fixture(monkeypatch)
    events = []
    job.cancel_event = immediate_event()

    def factory(*_args, **_kwargs):
        index = sum(value.startswith("create") for value in events)
        events.append(f"create-{index}")
        error = imports.AuthSessionRestartRequired("sign-in session is no longer valid") if index < 4 else RuntimeError("invalid password")
        return SimpleNamespace(bootstrap=Mock(side_effect=error), session=SimpleNamespace(close=lambda: events.append(f"close-{index}")))

    monkeypatch.setattr(imports, "ProtocolClient", factory)
    manager._run_job(job.id)
    assert events == [part for index in range(5) for part in (f"create-{index}", f"close-{index}")]
    assert job.session_retries == 4
    assert job.network_retries == 0
    assert job.phone_attempts == 0
    assert job.status == "failed"


@pytest.mark.parametrize("cancel_during_backoff", [False, True])
def test_import_supervisor_terminal_exit_releases_retained_lease_once(monkeypatch, cancel_during_backoff):
    imports, manager, job = _import_runner_fixture(monkeypatch)
    provider = SimpleNamespace(cancel=Mock())
    activation = SmsActivation("lease-fixture", "+15550000000", 6, "fixture-key")
    job._sms_client, job._sms_activation = provider, activation
    cancelled = {"value": False}

    def wait(_seconds):
        cancelled["value"] = True
        return True

    job.cancel_event = SimpleNamespace(is_set=lambda: cancelled["value"], wait=wait)
    error = imports.AuthSessionRestartRequired("sign-in session is no longer valid") if cancel_during_backoff else RuntimeError("invalid password")
    client = SimpleNamespace(bootstrap=Mock(side_effect=error), session=SimpleNamespace(close=Mock()))
    monkeypatch.setattr(imports, "ProtocolClient", Mock(return_value=client))
    manager._run_job(job.id)
    provider.cancel.assert_called_once_with(activation)
    client.session.close.assert_called_once()
    assert job._sms_activation is None
    assert job.status == ("cancelled" if cancel_during_backoff else "failed")


@pytest.mark.parametrize("limit", [0, 1])
def test_import_sms_timeout_cancels_once_and_obeys_zero_unlimited(monkeypatch, limit):
    imports, manager, job, provider, activation = _import_phone_fixture(monkeypatch, limit)
    provider.wait_code.side_effect = SmsError("等待短信验证码超时")
    expected = imports.PhoneNumberRestartRequired if limit == 0 else SmsError
    with pytest.raises(expected):
        manager._phone_step(job, _import_phone_client())
    provider.acquire.assert_called_once()
    provider.cancel.assert_called_once_with(activation)
    assert job.phone_attempts == 1

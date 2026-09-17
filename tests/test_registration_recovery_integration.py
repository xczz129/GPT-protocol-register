"""Offline integration coverage for saved-password completion in both schedulers.

The real schedulers, protocol facade, completion retry wrapper, mailbox lease,
and metrics store run together. Only authentication and provider HTTP responses
are replaced; the tests cannot buy an activation or contact an auth service.
"""
from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import mail
from common.mail_registration_metrics import get_summary
from register import batch as browser
from register import pending_recovery
from register import protocol
from register import smsbower_batch


def _pending(address: str) -> dict:
    return {
        "email": address, "password": "saved-fixture-password", "password_set": True,
        "registration_state": "security_pending",
    }


def _complete(address: str) -> dict:
    payload = {
        "client_id": protocol.chatgpt_oauth_client_id,
        "https://api.openai.com/auth": {"chatgpt_account_id": "fixture-account"},
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return {
        **_pending(address), "access_token": f"fixture.{encoded}.signature",
        "session_token": "fixture-session", "account_id": "fixture-account",
        "totp_secret": "JBSWY3DPEHPK3PXP", "mfa_enabled": True,
        "registration_state": "complete",
    }


@pytest.fixture
def integration(monkeypatch, tmp_path):
    observed = SimpleNamespace(order=[], writes=[], status=[], purchases=[], closed=[], stopped=False)
    monkeypatch.setattr(mail, "STATE_FILE", tmp_path / "mail-state.json")
    monkeypatch.setattr(mail, "_SMSBOWER_MAIL_ACTIVE_PAIRS", set())
    monkeypatch.setattr(mail, "_SMSBOWER_MAIL_PAIR_RETRIES", {})
    monkeypatch.setattr(mail, "_SMSBOWER_MAIL_ACTIVATION_BREAKERS", {})
    monkeypatch.setattr(mail, "_known_mailbox_bindings", lambda: {})
    monkeypatch.setattr(mail.requests.sessions.Session, "request", Mock(side_effect=AssertionError("network forbidden")))

    def persist(record, *_args, **_kwargs):
        observed.writes.append(dict(record))
        return dict(record)

    for module in (protocol, browser):
        monkeypatch.setattr(module, "upsert_account_record", persist)
    monkeypatch.setattr(protocol, "_append_account", persist)
    monkeypatch.setattr(protocol, "check_account", Mock(side_effect=AssertionError("health network forbidden")))
    monkeypatch.setattr(browser, "repair_security_one", Mock(side_effect=AssertionError("recovery already completes 2FA")))

    def trace(*_args, **_kwargs):
        value = Mock()
        value.path = tmp_path / "data" / "registration_logs" / "fixture.jsonl"
        value.email = ""
        return value

    monkeypatch.setattr(protocol, "RegistrationTrace", trace)

    capacity = {"total": 1, "available": 2, "emails": []}
    for module in (mail, browser):
        monkeypatch.setattr(module, "count_pool_capacity", lambda *a, **kw: dict(capacity))
        monkeypatch.setattr(module, "release_stale_in_use", lambda: 0)

    provider = mail.SmsBowerMailProvider({
        "type": "smsbower_mail", "provider_ref": "smsbower_mail#1",
        "api_base": "https://mail-api.example.test", "api_key": "fixture-private-key",
        "service": "dr", "domains": ["gmail.com"], "register_child": True,
    }, mail._make_config({}))
    primary = provider._settlement_mailbox("fresh@gmail.com", "fixture-activation", "gmail.com")
    primary["register_child"] = True
    observed.primary = primary
    observed.provider = provider
    codes = iter(["123456", "654321", "567890"])
    monkeypatch.setattr(mail.SmsBowerMailProvider, "get_code", lambda *_: next(codes))
    monkeypatch.setattr(mail.SmsBowerMailProvider, "set_status", lambda _self, mail_id, status: observed.status.append((mail_id, status)))
    monkeypatch.setattr(mail.SmsBowerMailProvider, "get_status", lambda *_: {"data": {"status": 1}})

    def purchase(*_args, **_kwargs):
        observed.purchases.append(primary)
        mail._set_state(primary["address"], "in_use")
        return primary

    monkeypatch.setattr(smsbower_batch, "create_mailbox", purchase)
    observed.events = []

    def run(engine, *, child=True):
        settings = {
            "mail": {"providers": [{"type": "smsbower_mail", "enable": True, "register_child": child}]},
            "registration": {"engine": engine, "total": 2, "threads": 2,
                             "max_transient_retries": 0, "browser_protocol_security_after_register": True, "post_register_health_check": False},
            "proxy": {"enabled": False},
        }
        callback = protocol.run_protocol_register if engine == "protocol" else browser.run_register
        return asyncio.run(callback(settings, total=2, concurrency=2, provider_type="smsbower_mail",
            stop_check=lambda: observed.stopped, task_event=lambda index, status, **fields: observed.events.append((index, status, fields))))

    observed.run = run
    yield observed
    provider.close()


def _install_signup(monkeypatch, integration, engine, *, stop_after="", primary_exception=False):
    def signup(index, *, mailbox_override, partial_sink, **kwargs):
        role = mailbox_override["_smsbower_pair_role"]
        assert not any(status in {2, 3} for _, status in integration.status)
        integration.order.append(f"{role}-signup")
        mailbox_override["_registration_identity_created"] = True
        assert integration.provider.wait_for_code(mailbox_override)
        mail.mark_smsbower_otp_accepted(mailbox_override)
        integration.order.append(f"{role}-code")
        if stop_after == role:
            integration.stopped = True
        if role == "primary":
            partial_sink({**_pending(mailbox_override["address"]), "totp_secret": "saved-late-secret"})
            # A later default-filled progress snapshot must not erase a secret
            # that an earlier enrollment response already revealed.
            partial_sink({"password": "", "password_set": False, "totp_secret": ""})
            mail.mark_mailbox_result(mailbox_override, success=False, error="session completion unavailable")
            if engine == "browser":
                # The browser flow reports its attempt before its caller decides
                # whether the saved password can complete the same account.
                kwargs["task_event"](index, "failed", error="no_access_token", step="session completion unavailable")
            if primary_exception:
                # Exercise the scheduler's defensive boundary when a driver or
                # wrapper propagates an error after the password was saved.
                raise RuntimeError("driver cleanup unavailable")
            return None
        mail.mark_mailbox_result(mailbox_override, success=True)
        return _complete(mailbox_override["address"])

    if engine == "protocol":
        class Registrar:
            device_id = "fixture-device"
            fingerprint = {}

            def __init__(self, *_args):
                self.registration_mailbox = None

            def register(self, index, **kwargs):
                self.registration_mailbox = kwargs["mailbox_override"]
                return signup(index, **kwargs)

            def close(self):
                integration.closed.append(self)

        monkeypatch.setattr(protocol, "PlatformRegistrar", Registrar)
    else:
        async def browser_signup(index, *_args, **kwargs):
            return signup(index, **kwargs)

        monkeypatch.setattr(browser, "register_one", browser_signup)


@pytest.mark.parametrize("engine", ["protocol", "browser"])
@pytest.mark.parametrize("primary_exception", [False, True])
def test_paired_schedulers_collect_child_code_before_password_only_completion(integration, monkeypatch, engine, primary_exception):
    _install_signup(monkeypatch, integration, engine, primary_exception=primary_exception)
    recovery_inputs = []

    def password_completion(index, **kwargs):
        assert index == 1
        assert integration.order == ["primary-signup", "primary-code", "child-signup", "child-code"]
        assert not any(status in {2, 3} for _, status in integration.status)
        assert len(integration.purchases) == 1
        mailbox = kwargs["mailbox_override"]
        assert mailbox is integration.primary
        assert mailbox["_registration_recovery_only"] is True
        recovery_inputs.append(dict(kwargs["account"]))
        assert kwargs["account"]["password"] == "saved-fixture-password"
        assert kwargs["account"]["password_set"] is True
        assert kwargs["account"]["totp_secret"] == "saved-late-secret"
        mail.prepare_smsbower_next_code(mailbox)
        integration.order.append("primary-password-login")
        kwargs["partial_sink"]({"totp_secret": "latest-recovery-secret", "mfa_enabled": True})
        mail.mark_mailbox_result(mailbox, success=True)
        kwargs["task_event"](index, "success", step="password completion succeeded")
        return {**_complete(mailbox["address"]), "totp_secret": "latest-recovery-secret"}

    monkeypatch.setattr(pending_recovery, "_recover_once", password_completion)
    records = integration.run(engine)
    assert len(records) == 2
    assert integration.order == ["primary-signup", "primary-code", "child-signup", "child-code", "primary-password-login"]
    assert len(recovery_inputs) == 1
    assert [status for _, status in integration.status if status in {2, 3}] == [3]
    assert integration.primary["_smsbower_pair_state"]["finalized"] is True
    assert get_summary()["totals"]["attempted"] == get_summary()["totals"]["success"] == 2
    assert not [event for event in integration.events if event[1] == "failed"]
    assert len([event for event in integration.events if event[:2] == (1, "success")]) == 1
    saved_primary = [record for record in integration.writes if record.get("email") == "fresh@gmail.com"][-1]
    assert saved_primary["totp_secret"] == "latest-recovery-secret"
    assert saved_primary["registration_state"] == "complete"
    if engine == "protocol":
        assert len(integration.closed) == 3


@pytest.mark.parametrize("engine", ["protocol", "browser"])
@pytest.mark.parametrize("stop_after", ["primary", "child"])
def test_stopping_pair_prevents_all_subsequent_auth_and_settles_consumed_lease_once(integration, monkeypatch, engine, stop_after):
    _install_signup(monkeypatch, integration, engine, stop_after=stop_after)
    completion = Mock(side_effect=AssertionError("stopped tasks must not authenticate"))
    monkeypatch.setattr(pending_recovery, "_recover_once", completion)
    records = integration.run(engine)
    completion.assert_not_called()
    expected = ["primary-signup", "primary-code"]
    if stop_after == "child":
        expected += ["child-signup", "child-code"]
    assert integration.order == expected
    assert len(integration.purchases) == 1
    assert [status for _, status in integration.status if status in {2, 3}] == [3]
    assert len(records) == (1 if stop_after == "child" else 0)
    assert get_summary()["totals"]["attempted"] == (2 if stop_after == "child" else 1)


def test_protocol_recovers_registrar_allocated_mailbox_without_an_override(integration, monkeypatch):
    mailbox = {"provider": "outlook", "address": "owned@example.test", "_registration_identity_created": True, "_otp_accepted_by_server": True}

    class Registrar:
        device_id = "fixture-device"
        fingerprint = {}

        def __init__(self, *_args):
            self.registration_mailbox = None

        def register(self, index, *, partial_sink):
            self.registration_mailbox = mailbox
            partial_sink(_pending(mailbox["address"]))
            raise RuntimeError("session completion unavailable")

        def close(self):
            integration.closed.append(self)

    def password_completion(index, **kwargs):
        assert kwargs["mailbox_override"] is mailbox
        assert kwargs["account"]["password"] == "saved-fixture-password"
        integration.order.append("password-login")
        return _complete(mailbox["address"])

    monkeypatch.setattr(protocol, "PlatformRegistrar", Registrar)
    monkeypatch.setattr(pending_recovery, "_recover_once", password_completion)
    record = protocol.register_one(1, mail_config={}, provider_type="outlook")
    assert record["email"] == mailbox["address"]
    assert record["registration_state"] == "complete"
    assert integration.order == ["password-login"]
    assert len(integration.closed) == 1
    assert not integration.purchases


def test_browser_recovers_mailbox_reported_with_saved_password_without_override(integration, monkeypatch):
    mailbox = {"provider": "outlook", "address": "owned@example.test", "_registration_identity_created": True, "_otp_accepted_by_server": True}
    monkeypatch.setattr(browser, "count_pool_capacity", lambda *a, **kw: {"total": 1, "available": 1, "emails": [mailbox["address"]]})

    async def signup(index, *_args, **kwargs):
        assert "mailbox_override" not in kwargs
        integration.order.append("signup")
        kwargs["partial_sink"](_pending(mailbox["address"]), mailbox)
        return None

    def password_completion(index, **kwargs):
        assert kwargs["mailbox_override"] is mailbox
        assert kwargs["account"]["password"] == "saved-fixture-password"
        integration.order.append("password-login")
        return _complete(mailbox["address"])

    monkeypatch.setattr(browser, "register_one", signup)
    monkeypatch.setattr(pending_recovery, "_recover_once", password_completion)
    records = asyncio.run(browser.run_register({"mail": {}, "registration": {"engine": "browser"}},
        total=1, concurrency=1, provider_type="outlook"))
    assert len(records) == 1 and records[0]["registration_state"] == "complete"
    assert integration.order == ["signup", "password-login"]
    assert not integration.purchases


@pytest.mark.parametrize("engine", ["protocol", "browser"])
def test_failed_password_completion_never_restarts_signup_for_owned_non_sms_identity(integration, monkeypatch, engine):
    mailbox = {"provider": "outlook", "address": "owned@example.test", "_registration_identity_created": True, "_otp_accepted_by_server": True}
    capacity = {"total": 1, "available": 1, "emails": [mailbox["address"]]}
    for module in (mail, browser):
        monkeypatch.setattr(module, "count_pool_capacity", lambda *a, **kw: dict(capacity))

    if engine == "protocol":
        class Registrar:
            device_id = "fixture-device"
            fingerprint = {}

            def __init__(self, *_args):
                self.registration_mailbox = None

            def register(self, index, *, partial_sink):
                integration.order.append("signup")
                self.registration_mailbox = mailbox
                partial_sink(_pending(mailbox["address"]))
                raise RuntimeError("session completion unavailable")

            def close(self):
                integration.closed.append(self)

        monkeypatch.setattr(protocol, "PlatformRegistrar", Registrar)
    else:
        async def signup(index, *_args, **kwargs):
            integration.order.append("signup")
            kwargs["partial_sink"](_pending(mailbox["address"]), mailbox)
            return None

        monkeypatch.setattr(browser, "register_one", signup)

    def password_completion(index, **kwargs):
        integration.order.append("password-login")
        raise RuntimeError("invalid_password")

    monkeypatch.setattr(pending_recovery, "_recover_once", password_completion)
    callback = protocol.run_protocol_register if engine == "protocol" else browser.run_register
    records = asyncio.run(callback({"mail": {}, "registration": {"engine": engine, "max_transient_retries": 1}},
        total=1, concurrency=1, provider_type="outlook"))
    assert records == []
    assert integration.order == ["signup", "password-login"], "an owned identity may only continue with its saved password"
    assert integration.writes[-1]["registration_state"] == "security_pending"


@pytest.mark.parametrize("engine", ["protocol", "browser"])
@pytest.mark.parametrize("include_credential", [False, True])
def test_unverified_email_failure_keeps_original_error_without_password_login(integration, monkeypatch, engine, include_credential):
    mailbox = {"provider": "icloud_api", "address": "unverified@example.test", "_registration_identity_created": True, "api_key": "fixture-mail-api-secret"}
    capacity = {"total": 1, "available": 1, "emails": [mailbox["address"]]}
    for module in (mail, browser):
        monkeypatch.setattr(module, "count_pool_capacity", lambda *a, **kw: dict(capacity))
    original_error = "iCloud API HTTP 401: API Key 不正确"
    if include_credential:
        original_error += " key=" + mailbox["api_key"]
    if engine == "protocol":
        class Registrar:
            device_id = "fixture-device"
            fingerprint = {}

            def __init__(self, *_args):
                self.registration_mailbox = mailbox

            def register(self, index, *, partial_sink):
                integration.order.append("registration")
                partial_sink(_pending(mailbox["address"]))
                raise RuntimeError(original_error)

            def close(self):
                integration.closed.append(self)

        monkeypatch.setattr(protocol, "PlatformRegistrar", Registrar)
    else:
        async def signup(index, *_args, **kwargs):
            integration.order.append("registration")
            kwargs["partial_sink"](_pending(mailbox["address"]), mailbox)
            kwargs["task_event"](index, "failed", error=original_error, step="邮箱读取失败")
            return None

        monkeypatch.setattr(browser, "register_one", signup)
    recover = Mock(side_effect=AssertionError("unverified email must not start password login"))
    monkeypatch.setattr(pending_recovery, "_recover_once", recover)
    callback = protocol.run_protocol_register if engine == "protocol" else browser.run_register
    records = asyncio.run(callback({"mail": {}, "registration": {"engine": engine, "max_transient_retries": 2}},
        total=1, concurrency=1, provider_type="icloud_api"))
    assert records == []
    assert integration.order == ["registration"]
    recover.assert_not_called()
    last = integration.writes[-1]
    assert last["registration_state"] == "security_pending"
    assert last["registration_recovery_state"] == "awaiting_email_verification"
    assert last["registration_initial_error"] == original_error.replace(mailbox["api_key"], "[已隐藏]")
    assert mailbox["api_key"] not in last["registration_initial_error"]
    assert last["password"] == "saved-fixture-password"


def test_typed_rate_limit_survives_event_boundary_and_stops_child(integration, monkeypatch):
    class LimitedError(RuntimeError):
        response = SimpleNamespace(status_code=429)

    class Registrar:
        device_id = "fixture-device"
        fingerprint = {}

        def __init__(self, *_args):
            pass

        def register(self, index, **kwargs):
            integration.order.append("auth-attempt")
            raise LimitedError("request was rejected")

        def close(self):
            integration.closed.append(self)

    monkeypatch.setattr(protocol, "PlatformRegistrar", Registrar)
    records = integration.run("protocol")
    assert records == []
    assert integration.order == ["auth-attempt"]
    assert len(integration.purchases) == 1
    assert any("auth_rate_limit_exceeded" in str(fields.get("error", "")) for _, _, fields in integration.events)
    assert any(index == 2 and status == "cancelled" for index, status, _ in integration.events)
    assert get_summary()["totals"]["other_failed"] == 1
    assert get_summary()["totals"]["cancelled"] == 0

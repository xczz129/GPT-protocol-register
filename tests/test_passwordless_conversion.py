"""Registration candidates cannot be converted into existing-account recovery.

All mailbox, HTTP, login, credential and persistence effects are test doubles.
Only synthetic, redacted auth response shapes are used here.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from register.registration_outcome import PasswordlessAccountDetected, RegisteredEmailDetected


SECURITY_ACTIONS = (
    "login", "_passwordless_login", "_send_otp", "_send_passwordless_otp",
    "_send_password_reset_otp", "_complete_post_registration_password",
    "_enroll_totp", "_set_registration_password", "_register_user", "_validate_otp",
    "_create_account", "_read_chatgpt_session", "_build_registration_record",
)


@pytest.fixture
def candidate(monkeypatch):
    from register import protocol_registrar as module

    mailbox = {"provider": "smsbower_mail", "address": "candidate@example.test"}
    registrar = object.__new__(module.PlatformRegistrar)
    registrar.proxy = ""
    registrar.device_id = "fixture-device"
    registrar.fingerprint = {}
    registrar._log_identity = Mock()
    registrar._diagnostic = Mock()
    registrar._navigate_auth_step = Mock()
    registrar._complete_chatgpt_session = Mock()
    registrar._authorize_continue_login = Mock()
    registrar._last_authorize_continue_diagnostic = {}
    for name in SECURITY_ACTIONS:
        setattr(registrar, name, Mock(side_effect=AssertionError(
            f"registration candidate must not call {name}",
        )))

    effects = SimpleNamespace(
        purchase=Mock(return_value=mailbox), wait=Mock(), finalize=Mock(),
        accepted=Mock(), partial=Mock(), http=Mock(side_effect=AssertionError(
            "unexpected HTTP in offline registration test",
        )),
    )
    monkeypatch.setattr(module, "create_mailbox", effects.purchase)
    monkeypatch.setattr(module, "wait_for_code", effects.wait)
    monkeypatch.setattr(module, "mark_mailbox_result", effects.finalize)
    monkeypatch.setattr(module, "mark_smsbower_otp_accepted", effects.accepted)
    monkeypatch.setattr(module, "request_with_local_retry", effects.http)
    monkeypatch.setattr(module, "mark_mailbox_code_boundary", Mock())
    monkeypatch.setattr(module, "release_mailbox", Mock())
    monkeypatch.setattr(module, "begin_attempt", Mock())
    monkeypatch.setattr(module, "step", Mock())
    monkeypatch.setattr(module, "_random_password", lambda: "fixture-password")
    return registrar, mailbox, effects


def assert_no_account_actions(registrar, mailbox, effects):
    for name in SECURITY_ACTIONS:
        getattr(registrar, name).assert_not_called()
    registrar._navigate_auth_step.assert_not_called()
    effects.wait.assert_not_called()
    effects.accepted.assert_not_called()
    effects.partial.assert_not_called()
    effects.http.assert_not_called()
    assert not mailbox.get("_registration_identity_created")


def otp_response(mode=None):
    payload = {
        "login_methods": [{"type": "passwordless_otp"}, {"type": "password"}],
    }
    if mode is not None:
        payload["email_verification_mode"] = mode
    return {
        "continue_url": "https://auth.openai.com/email-verification",
        "page": {"type": "email_otp_verification", "payload": payload},
    }


@pytest.mark.parametrize("provider", ["smsbower_mail", "online_disposablemail"])
@pytest.mark.parametrize("role", ["primary", "child"])
@pytest.mark.parametrize("mode", ["passwordless_login", "password_reset"])
@pytest.mark.parametrize("continued", [False, True])
def test_existing_otp_modes_skip_without_changing_credentials(
    candidate, provider, role, mode, continued,
):
    registrar, mailbox, effects = candidate
    mailbox.update(provider=provider, _smsbower_pair_role=role)
    auth_response = otp_response(mode)
    registrar._complete_chatgpt_session.return_value = (
        {"final_url": "https://auth.openai.com/log-in"} if continued else auth_response
    )
    registrar._authorize_continue_login.return_value = auth_response

    with pytest.raises(RegisteredEmailDetected) as caught:
        registrar.register(1, mailbox_override=mailbox, partial_sink=effects.partial)

    assert caught.value.email == mailbox["address"]
    assert mailbox["_registration_outcome"] == "already_registered"
    assert "_registration_passwordless_existing" not in mailbox
    assert "_registration_existing_takeover_detail" not in mailbox
    effects.purchase.assert_not_called()
    effects.finalize.assert_called_once_with(mailbox, success=False, error="already_registered")
    if continued:
        registrar._authorize_continue_login.assert_called_once_with(
            mailbox["address"], 1, passwordless=False, restart_invalid_state=False,
        )
    else:
        registrar._authorize_continue_login.assert_not_called()
    assert_no_account_actions(registrar, mailbox, effects)


@pytest.mark.parametrize("auth_response", [
    otp_response(),
    otp_response("passwordless_signup"),
    {"continue_url": "https://auth.openai.com/create-account/password",
     "page": {"type": "username_password_create", "payload": {
         "email_verification_mode": "passwordless_login",
         "login_methods": [{"type": "passwordless_otp"}, {"type": "password"}],
     }}},
    {"continue_url": "https://auth.openai.com/unknown-route",
     "page": {"type": "unknown_page"}},
], ids=["bare-otp", "otp-signup", "conflicting-login-and-signup", "unknown"])
@pytest.mark.parametrize("continued", [False, True])
def test_ambiguous_or_unsupported_state_stops_without_trying_account_actions(
    candidate, auth_response, continued,
):
    registrar, mailbox, effects = candidate
    registrar._complete_chatgpt_session.return_value = (
        {"final_url": "https://auth.openai.com/log-in"} if continued else auth_response
    )
    registrar._authorize_continue_login.return_value = auth_response

    with pytest.raises(RuntimeError, match="registration_password_route_unavailable"):
        registrar.register(1, mailbox_override=mailbox, partial_sink=effects.partial)

    assert "_registration_outcome" not in mailbox
    assert registrar._authorize_continue_login.call_count == int(continued)
    assert_no_account_actions(registrar, mailbox, effects)


@pytest.mark.parametrize("provider", ["smsbower_mail", "online_disposablemail"])
@pytest.mark.parametrize("role", ["primary", "child"])
@pytest.mark.parametrize("legacy_marker", [None, "existing_password_route"])
def test_legacy_conversion_entry_rejects_before_login_reset_otp_or_partial_save(
    candidate, provider, role, legacy_marker,
):
    registrar, mailbox, effects = candidate
    mailbox.update(provider=provider, _smsbower_pair_role=role)
    if legacy_marker:
        mailbox["_registration_existing_takeover_detail"] = legacy_marker

    with pytest.raises(RegisteredEmailDetected) as caught:
        registrar._register_passwordless_existing_account(
            mailbox["address"], mailbox, 1, partial_sink=effects.partial,
        )

    assert caught.value.email == mailbox["address"]
    assert mailbox["_registration_outcome"] == "already_registered"
    assert "_registration_passwordless_existing" not in mailbox
    effects.purchase.assert_not_called()
    assert_no_account_actions(registrar, mailbox, effects)


def test_legacy_passwordless_signal_cannot_dispatch_to_conversion(candidate):
    registrar, mailbox, effects = candidate
    registrar._complete_chatgpt_session.return_value = {
        "final_url": "https://auth.openai.com/log-in",
    }
    registrar._check_fresh_registration = Mock(side_effect=PasswordlessAccountDetected(
        "legacy_route_signal", email=mailbox["address"],
    ))
    registrar._register_passwordless_existing_account = Mock()

    with pytest.raises(PasswordlessAccountDetected):
        registrar.register(1, mailbox_override=mailbox, partial_sink=effects.partial)

    registrar._register_passwordless_existing_account.assert_not_called()
    assert_no_account_actions(registrar, mailbox, effects)


def test_worker_does_not_start_child_after_existing_primary_is_skipped():
    from register.smsbower_batch import primary_can_start_child

    mailbox = {
        "_registration_outcome": "already_registered",
        "_registration_code_received": True,
        "_otp_accepted_by_server": True,
    }
    assert primary_can_start_child(mailbox, None) is False


def test_legacy_passwordless_marker_still_blocks_a_child():
    from register.smsbower_batch import primary_can_start_child

    mailbox = {
        "_registration_passwordless_existing": True,
        "_registration_code_received": True,
        "_otp_accepted_by_server": True,
    }
    assert primary_can_start_child(mailbox, {"email": "fixture@example.test"}) is False

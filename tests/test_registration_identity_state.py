"""Offline regression evidence: auth choices never authorize account conversion."""
from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from register.auth_diagnostics import auth_route_diagnostic, read_auth_response
from register.registration_outcome import (
    auth_identity_evidence,
    is_duplicate_registration_error,
    registration_auth_state,
    registration_creation_confirmed,
)


OTP_URL = "https://auth.openai.com/email-verification"
SIGNUP_URL = "https://auth.openai.com/create-account/password"
LOGIN_URL = "https://auth.openai.com/log-in/password"

# Only the anonymous response fields from 判断逻辑.har entry 8 are retained.
# In the actual response login_methods is a list of objects, not strings.
HAR_LOGIN_RESPONSE = {
    "continue_url": OTP_URL,
    "page": {
        "type": "email_otp_verification",
        "payload": {
            "email_verification_mode": "passwordless_login",
            "login_methods": [{"type": "passwordless_otp"}, {"type": "password"}],
        },
    },
}


def otp_response(**fields):
    return {"continue_url": OTP_URL, "page": {"type": "email_otp_verification", "payload": fields}}


def test_har_login_methods_are_preserved_without_claiming_no_password_or_ownership():
    response = deepcopy(HAR_LOGIN_RESPONSE)
    original = deepcopy(response)

    assert registration_auth_state(OTP_URL, "email_otp_verification", auth_response=response) == "existing"
    diagnostic = auth_route_diagnostic(OTP_URL, "email_otp_verification", auth_response=response)
    assert diagnostic["email_verification_mode"] == "passwordless_login"
    assert diagnostic["login_methods"] == ["password", "passwordless_otp"]
    assert diagnostic["auth_response_valid"] is True
    assert response == original
    assert "_registration_identity_created" not in response


def test_har_reset_transaction_uses_the_same_page_without_becoming_registration():
    response = otp_response(email_verification_mode="password_reset")
    assert registration_auth_state(OTP_URL, "email_otp_verification", auth_response=response) == "existing"
    assert auth_identity_evidence(response)["email_verification_mode"] == "password_reset"
    assert not is_duplicate_registration_error(response)


@pytest.mark.parametrize("fields", [
    {},
    {"login_methods": []},
    {"login_methods": [{"type": "passwordless_otp"}]},
    {"login_methods": [{"type": "password"}]},
    {"login_methods": [{"type": "passwordless_otp"}, {"type": "password"}]},
])
def test_otp_page_and_available_methods_do_not_establish_account_state(fields):
    response = otp_response(**fields)
    assert registration_auth_state(OTP_URL, "email_otp_verification", auth_response=response) == ""


@pytest.mark.parametrize("url,page_type", [
    (OTP_URL, "email_otp_verification"),
    ("/verify-email", ""),
    ("", "email_otp"),
])
def test_legacy_callers_cannot_infer_passwordlessness_from_a_bare_otp_page(url, page_type):
    assert registration_auth_state(url, page_type) == ""


def test_passwordless_signup_mode_is_diagnostic_and_does_not_enable_an_otp_registration_flow():
    response = otp_response(email_verification_mode="passwordless_signup")
    assert registration_auth_state(OTP_URL, "email_otp_verification", auth_response=response) == ""
    assert auth_route_diagnostic(OTP_URL, "email_otp_verification", auth_response=response)["email_verification_mode"] == "passwordless_signup"


@pytest.mark.parametrize("url,page_type", [
    (SIGNUP_URL, "create_account_password"),
    ("/signup/password", "signup_password"),
    ("", "username_password_create"),
    (SIGNUP_URL + "?next=/log-in/password", ""),
])
def test_explicit_password_creation_step_remains_signup(url, page_type):
    response = {"page": {"type": page_type, "payload": {}}}
    assert registration_auth_state(url, page_type, auth_response=response) == "signup"


@pytest.mark.parametrize("url,page_type", [
    (LOGIN_URL, "password_verification"),
    ("/reset-password", "reset_password"),
    ("/mfa-challenge", "mfa_challenge"),
])
def test_existing_authentication_routes_remain_registration_stop_evidence(url, page_type):
    assert registration_auth_state(url, page_type, auth_response={"page": {"type": page_type}}) == "existing"


@pytest.mark.parametrize("url,page_type,response", [
    (SIGNUP_URL, "create_account_password", {"page": {"payload": {"email_verification_mode": "passwordless_login"}}}),
    (SIGNUP_URL, "create_account_password", {"page": {"payload": {"email_verification_mode": "password_reset"}}}),
    (LOGIN_URL, "password_verification", {"page": {"payload": {"email_verification_mode": "passwordless_signup"}}}),
    (SIGNUP_URL, "password_verification", {}),
    (LOGIN_URL, "create_account_password", {}),
    (SIGNUP_URL, "email_otp_verification", {}),
    (OTP_URL, "create_account_password", {}),
    (SIGNUP_URL, "create_account_password", {"continue_url": LOGIN_URL}),
    (SIGNUP_URL, "create_account_password", {"page": {"type": "password_verification"}}),
    (SIGNUP_URL, "create_account_password", {"email_verification_mode": "passwordless_signup", "page": {"payload": {"email_verification_mode": "passwordless_login"}}}),
])
def test_conflicting_evidence_cannot_authorize_a_registration(url, page_type, response):
    assert registration_auth_state(url, page_type, auth_response=response) == ""


@pytest.mark.parametrize("response", [
    [], False, "private-response-body",
    {"page": []}, {"page": None}, {"page": {"type": []}},
    {"page": {"payload": []}}, {"page": {"payload": None}},
    {"page": {"payload": {"email_verification_mode": None}}},
    {"page": {"payload": {"email_verification_mode": "unrecognized-private-mode"}}},
    {"page": {"payload": {"login_methods": "password"}}},
    {"page": {"payload": {"login_methods": ["password"]}}},
    {"page": {"payload": {"login_methods": [{"type": []}]}}},
    {"page": {"payload": {"login_methods": [{"type": "private-method"}]}}},
    {"login_methods": [{"type": "password"}], "page": {"payload": {"login_methods": [{"type": "passwordless_otp"}]}}},
    {"error": {"code": "username_not_found"}},
    {"continue_url": []},
])
def test_malformed_or_rejected_response_cannot_be_overridden_by_a_signup_url(response):
    assert registration_auth_state(SIGNUP_URL, "create_account_password", auth_response=response) == ""


@pytest.mark.parametrize("url", [
    "https://private-host.test/create-account/password",
    "https://auth.openai.com.private-host.test/create-account/password",
    "https://name:private-secret@auth.openai.com/create-account/password",
    "http://auth.openai.com/create-account/password",
    "//auth.openai.com/create-account/password",
    "https:///create-account/password",
    "https://auth.openai.com:8443/create-account/password",
    "javascript:/create-account/password",
    "https://auth.openai.com/create-account/password#private-fragment",
    "https://auth.openai.com/\\private-host.test/create-account/password",
    "https://auth.openai.com/%2Fprivate-host.test/create-account/password",
    "https://auth.openai.com/create-account/\tpassword",
    "https://[invalid",
])
def test_untrusted_urls_never_authorize_even_with_a_valid_signup_page_type(url):
    assert registration_auth_state(url, "create_account_password") == ""
    assert registration_auth_state(SIGNUP_URL, "create_account_password", auth_response={"continue_url": url}) == ""


def test_sensitive_or_unknown_metadata_is_never_emitted_in_diagnostics():
    response = {
        "page": {"payload": {
            "email_verification_mode": "private-mode-secret",
            "login_methods": [{"type": "password", "id": "private-account-id"}, {"type": "private-method-secret"}],
            "password": "private-password-secret",
            "otp": "874631",
            "api_key": "private-api-key-secret",
            "session": "private-session-secret",
        }},
    }
    diagnostic = auth_route_diagnostic(OTP_URL + "?code=private-code-secret", "private-page-secret", auth_response=response)
    assert diagnostic == {
        "page_type": "unrecognized", "route": "/email-verification",
        "email_verification_mode": "unrecognized", "login_methods": ["password", "unrecognized"],
        "auth_response_valid": False,
    }
    rendered = json.dumps(diagnostic)
    assert "private" not in rendered and "secret" not in rendered and "874631" not in rendered


def test_username_not_found_is_preserved_as_an_error_code_without_becoming_already_registered():
    payload = {"error": {"code": "username_not_found", "message": "private-message"}}
    response = SimpleNamespace(status_code=400, headers={"content-type": "application/json"}, json=lambda: payload)
    parsed, diagnostic = read_auth_response(response)
    assert parsed is payload
    assert diagnostic["api_error_code"] == "username_not_found"
    assert diagnostic["http_status"] == 400
    assert "private-message" not in json.dumps(diagnostic)
    assert not is_duplicate_registration_error(payload)
    assert registration_auth_state(SIGNUP_URL, "create_account_password", auth_response=payload) == ""


@pytest.mark.parametrize("response", [
    {"continue_url": "https://auth.openai.com/api/accounts/email-otp/send"},
    {"continue_url": "https://auth.openai.com/api/accounts/email-otp/send", "page": {"type": "email_otp_send"}},
    {"continue_url": OTP_URL, "page": {"type": "email_otp_verification"}},
    {"page": {"type": "email_otp_send"}},
    {"page": {"payload": {"continue_url": "/email-verification"}}},
    {"continue_url": "/about-you", "page": {"type": "about_you"}},
    {"page": {"type": "about_you"}},
    otp_response(email_verification_mode="passwordless_signup"),
])
def test_successful_creation_response_recognizes_only_existing_registration_next_steps(response):
    original = deepcopy(response)
    assert registration_creation_confirmed(response) is True
    assert response == original
    assert "_registration_identity_created" not in response


@pytest.mark.parametrize("response", [
    None, {}, [], "not-a-response", False,
    {"success": True},
    {"success": False, "continue_url": OTP_URL},
    {"continue_url": OTP_URL, "error": {"code": "username_not_found"}},
    {"continue_url": OTP_URL, "page": []},
    {"continue_url": OTP_URL, "page": {"payload": []}},
    {"continue_url": OTP_URL, "page": {"type": []}},
    {"continue_url": OTP_URL, "page": {"type": "private-unknown-page"}},
    {"continue_url": OTP_URL, "page": {"payload": {"email_verification_mode": "private-unknown-mode"}}},
    {"continue_url": OTP_URL, "page": {"payload": {"login_methods": ["password"]}}},
    {"continue_url": []},
    {"continue_url": "https://other.test/email-verification", "page": {"type": "email_otp_send"}},
    {"continue_url": OTP_URL, "page": {"payload": {"url": "https://other.test/email-verification"}}},
    {"continue_url": OTP_URL, "final_url": "https://other.test/email-verification"},
    {"continue_url": SIGNUP_URL, "page": {"type": "password_verification"}},
    {"continue_url": LOGIN_URL, "page": {"type": "create_account_password"}},
    {"continue_url": OTP_URL, "page": {"type": "password_verification"}},
    {"continue_url": OTP_URL, "page": {"type": "create_account_password"}},
    {"continue_url": "/about-you", "page": {"type": "email_otp_verification"}},
    {"continue_url": OTP_URL, "page": {"type": "about_you"}},
    {"continue_url": "/error", "page": {"type": "email_otp_verification"}},
    {"continue_url": "/some-future-step", "page": {"type": "email_otp_verification"}},
    HAR_LOGIN_RESPONSE,
    otp_response(email_verification_mode="password_reset"),
])
def test_invalid_or_conflicting_creation_response_never_grants_current_task_ownership(response):
    assert registration_creation_confirmed(response) is False


def test_otp_evidence_only_confirms_creation_in_the_acknowledged_creation_response_context():
    response = otp_response()
    assert registration_auth_state(OTP_URL, "email_otp_verification", auth_response=response) == ""
    assert registration_creation_confirmed(response) is True


@pytest.mark.parametrize("untrusted_location", ["final_url", "payload_url", "payload_continue_url"])
def test_other_untrusted_response_url_blocks_identifier_submission(monkeypatch, untrusted_location):
    from register import protocol_registrar as module

    registrar = module.PlatformRegistrar.__new__(module.PlatformRegistrar)
    registrar._authorize_continue_login = Mock()
    registrar._navigate_auth_step = Mock()
    registrar._set_registration_password = Mock()
    registrar._diagnostic = Mock()
    monkeypatch.setattr(module, "step", Mock())
    mailbox = {"provider": "smsbower_mail", "address": "fixture@example.test"}
    response = {
        "continue_url": "https://auth.openai.com/log-in",
        "page": {"type": "login", "payload": {}},
    }
    untrusted_url = "https://untrusted.test/log-in?session=private-session-secret"
    if untrusted_location == "final_url":
        response["final_url"] = untrusted_url
    else:
        response["page"]["payload"][untrusted_location.removeprefix("payload_")] = untrusted_url

    with pytest.raises(RuntimeError, match="registration_password_route_unavailable") as caught:
        registrar._check_fresh_registration(mailbox["address"], mailbox, response, 1)

    registrar._authorize_continue_login.assert_not_called()
    registrar._navigate_auth_step.assert_not_called()
    registrar._set_registration_password.assert_not_called()
    assert mailbox["_registration_guard_error"] == "registration_password_route_unavailable"
    assert not mailbox.get("_registration_identity_created")
    assert "private-session-secret" not in str(caught.value)
    assert "untrusted.test" not in str(registrar._diagnostic.call_args)
    assert auth_identity_evidence(response)["auth_response_valid"] is False


@pytest.mark.parametrize("message", [
    "username_not_found",
    'password_reset_send_otp_http_400, detail={"error":{"code":"username_not_found"}}',
    "auth_step_error_page: code=username_not_found; invalid_state",
])
@pytest.mark.parametrize("skip_existing_password", [False, True])
def test_missing_identity_cannot_restart_password_authorization_or_consume_otp(
    monkeypatch, message, skip_existing_password,
):
    from register import protocol_registrar as module

    original_error = RuntimeError(message)
    registrar = module.PlatformRegistrar.__new__(module.PlatformRegistrar)
    registrar._start_password_add_authorize = Mock(side_effect=original_error)
    registrar._reset_auth_cookies = Mock()
    registrar._sync_session_identity = Mock()
    registrar._send_password_reset_otp = Mock()
    registrar._send_passwordless_otp = Mock()
    registrar._validate_otp = Mock()
    registrar._add_password = Mock()
    registrar._enroll_totp = Mock()
    wait_for_code = Mock()
    sleep = Mock()
    monkeypatch.setattr(module, "mark_mailbox_code_boundary", Mock())
    monkeypatch.setattr(module, "wait_for_code", wait_for_code)
    monkeypatch.setattr(module, "step", Mock())
    monkeypatch.setattr(module.time, "sleep", sleep)
    mailbox = {"provider": "smsbower_mail", "address": "fixture@example.test"}

    with pytest.raises(RuntimeError, match="username_not_found") as caught:
        registrar._complete_post_registration_password(
            mailbox["address"], "fixture-saved-password", mailbox, 1,
            skip_existing_password=skip_existing_password,
        )

    assert caught.value is original_error
    registrar._start_password_add_authorize.assert_called_once_with(mailbox["address"], 1)
    sleep.assert_not_called()
    wait_for_code.assert_not_called()
    for method in (
        "_reset_auth_cookies", "_sync_session_identity", "_send_password_reset_otp",
        "_send_passwordless_otp", "_validate_otp", "_add_password", "_enroll_totp",
    ):
        getattr(registrar, method).assert_not_called()
    assert not mailbox.get("_registration_identity_created")

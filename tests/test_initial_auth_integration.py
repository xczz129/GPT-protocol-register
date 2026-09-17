"""Offline integration checks for initial authentication evidence and stopping.

The response bodies below are synthetic, reduced loader records. No HAR body,
real address, credential, purchase, JavaScript evaluation, or HTTP call is used.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import register.protocol_registrar as protocol
from register.protocol_registrar import PlatformRegistrar
from register.registration_outcome import RegisteredEmailDetected


FIXTURE_EMAIL = "fixture@example.test"
AUTH_ORIGIN = "https://auth.openai.com"
NO_ACCOUNT_ACTIONS = (
    "_navigate_auth_step", "_set_registration_password", "_register_user",
    "_send_otp", "_send_passwordless_otp", "_send_password_reset_otp",
    "_validate_otp", "_create_account", "_read_chatgpt_session", "login",
    "_passwordless_login", "_complete_post_registration_password",
    "_register_passwordless_existing_account", "_enroll_totp",
)


def json_response(payload, *, path="/email-verification"):
    text = json.dumps(payload)
    return SimpleNamespace(
        status_code=200, headers={"content-type": "application/json"},
        text=text, content=text.encode(), url=AUTH_ORIGIN + path,
        json=MagicMock(return_value=payload),
    )


def html_response(html, *, path="/email-verification"):
    return SimpleNamespace(
        status_code=200, headers={"content-type": "text/html; charset=utf-8"},
        text=html, content=html.encode(), url=AUTH_ORIGIN + path,
        json=MagicMock(side_effect=ValueError("fixture response is HTML")),
    )


def loader_html(*, mode="passwordless_login", email=FIXTURE_EMAIL):
    """Encode only the documented passive-loader path needed by these tests."""
    table = [
        {"_1": 2}, "loaderData", {"_3": 4},
        "routes/layouts/client-auth-session-layout/layout", {"_5": 6},
        "session", {"_7": 8, "_9": 10, "_11": 12},
        "email_verification_mode", mode, "email", email,
        "is_missing_session", False,
    ]
    # This string is parsed as data by the reader, never run as JavaScript.
    encoded = json.dumps(json.dumps(table) + "\n")
    return "<script>window.__reactRouterContext.streamController.enqueue(" + encoded + ");</script>"


@pytest.fixture
def initial_auth(monkeypatch):
    registrar = PlatformRegistrar.__new__(PlatformRegistrar)
    registrar.session = MagicMock()
    registrar.session.request.side_effect = AssertionError("network is prohibited")
    registrar.proxy = ""
    registrar.clearance_user_agent = ""
    registrar.device_id = "fixture-device"
    registrar.fingerprint = {}
    registrar._log_identity = MagicMock()
    registrar._diagnostic = MagicMock()
    registrar._authorize_continue_login = MagicMock(
        side_effect=AssertionError("unexpected identifier resubmission"),
    )
    for name in NO_ACCOUNT_ACTIONS:
        setattr(registrar, name, MagicMock(side_effect=AssertionError(
            f"unexpected account action: {name}",
        )))
    effects = SimpleNamespace(
        create=MagicMock(side_effect=AssertionError("mailbox purchase is prohibited")),
        wait=MagicMock(side_effect=AssertionError("OTP polling is prohibited")),
        finalize=MagicMock(), accepted=MagicMock(), release=MagicMock(),
        request=MagicMock(side_effect=AssertionError("unscripted request")),
        log=MagicMock(), partial=MagicMock(),
    )
    monkeypatch.setattr(protocol, "create_mailbox", effects.create)
    monkeypatch.setattr(protocol, "wait_for_code", effects.wait)
    monkeypatch.setattr(protocol, "mark_mailbox_result", effects.finalize)
    monkeypatch.setattr(protocol, "release_mailbox", effects.release)
    monkeypatch.setattr(protocol, "mark_smsbower_otp_accepted", effects.accepted)
    monkeypatch.setattr(protocol, "begin_attempt", MagicMock())
    monkeypatch.setattr(protocol, "mark_mailbox_code_boundary", MagicMock())
    monkeypatch.setattr(protocol, "_add_task_event_secrets", MagicMock())
    monkeypatch.setattr(protocol, "_random_password", lambda: "fixture-unused-password")
    monkeypatch.setattr(protocol, "_random_name", lambda: ("Fixture", "Example"))
    monkeypatch.setattr(protocol, "_random_birthdate", lambda: "1990-01-01")
    monkeypatch.setattr(protocol, "step", effects.log)
    monkeypatch.setattr(protocol, "request_with_local_retry", effects.request)
    monkeypatch.setattr(protocol, "_cookie_values", lambda _session: {})
    monkeypatch.setattr(protocol, "_is_cloudflare_challenge", lambda _response: False)
    monkeypatch.setattr(protocol, "_tls_verify", lambda: True)
    monkeypatch.setattr(protocol, "_browser_fingerprint", lambda _fingerprint: {
        "accept_language": "en", "user_agent": "offline-test",
    })
    monkeypatch.setattr(protocol, "_client_hint_headers", lambda _fingerprint: {})
    monkeypatch.setattr(
        protocol, "_headers_with_clearance",
        lambda headers, *_args, **_kwargs: headers,
    )
    monkeypatch.setattr(protocol, "current_runtime_settings", lambda: SimpleNamespace(
        enable_totp_after_register=False,
    ))

    def prepare(authorize_response, *, provider="online_disposablemail"):
        effects.request.reset_mock()
        effects.request.side_effect = [
            (json_response({"csrfToken": "fixture-csrf"}), ""),
            (json_response({"url": AUTH_ORIGIN + "/authorize"}), ""),
            (authorize_response, ""),
        ]
        mailbox = {"address": FIXTURE_EMAIL, "provider": provider}
        return registrar, mailbox, effects

    return prepare


def assert_no_account_actions(registrar, mailbox, effects):
    for name in NO_ACCOUNT_ACTIONS:
        getattr(registrar, name).assert_not_called()
    effects.create.assert_not_called()
    effects.wait.assert_not_called()
    effects.accepted.assert_not_called()
    effects.partial.assert_not_called()
    assert not mailbox.get("_registration_identity_created")
    assert effects.request.call_count == 3
    registrar.session.request.assert_not_called()


def diagnostic_text(registrar, effects):
    return "\n".join(
        str(argument)
        for mocked in (registrar._diagnostic, effects.log)
        for call in mocked.call_args_list
        for argument in call.args[1:]
    )


def test_start_only_uses_expected_identity_and_preserves_response_contract(
    initial_auth, monkeypatch,
):
    payload = {
        "page": {"type": "email_otp_verification", "payload": {"fixture": True}},
        "continue_url": AUTH_ORIGIN + "/email-verification",
        "email_verification_mode": "passwordless_login",
        "login_methods": [{"type": "passwordless_otp"}, {"type": "password"}],
        "error": {},
    }
    response = json_response(payload)
    registrar, _mailbox, effects = initial_auth(response)
    metadata = {
        "http_status": 200, "content_type": "application/json",
        "response_kind": "json_object", "auth_state_source": "json",
        "auth_payload_parsed": True, "initial_auth_parse_status": "json_object",
        "initial_auth_state_rejected": False,
    }
    reader = MagicMock(return_value=(payload, metadata))
    monkeypatch.setattr(protocol, "read_initial_auth_response", reader)

    result = registrar._complete_chatgpt_session(
        FIXTURE_EMAIL, "", 1, start_only=True, registration_password_flow=True,
    )

    reader.assert_called_once_with(response, expected_email=FIXTURE_EMAIL)
    for name, value in payload.items():
        assert result[name] == value
    assert result["final_url"] == AUTH_ORIGIN + "/email-verification"
    assert result["authorize_url"] == AUTH_ORIGIN + "/authorize"
    assert result["device_id"] == "fixture-device"
    for name, value in metadata.items():
        assert registrar._initial_auth_diagnostic[name] == value
    assert "auth_state_source" in diagnostic_text(registrar, effects)
    assert effects.request.call_count == 3


@pytest.mark.parametrize("provider", ["online_disposablemail", "smsbower_mail"])
def test_passive_html_login_evidence_skips_existing_identity_without_otp_or_reset(
    initial_auth, provider,
):
    registrar, mailbox, effects = initial_auth(
        html_response(loader_html()), provider=provider,
    )

    with pytest.raises(RegisteredEmailDetected) as raised:
        registrar.register(1, mailbox_override=mailbox, partial_sink=effects.partial)

    assert raised.value.email == FIXTURE_EMAIL
    assert mailbox["_registration_outcome"] == "already_registered"
    assert registrar._initial_auth_diagnostic["auth_state_source"] == "html_loader"
    assert registrar._initial_auth_diagnostic["auth_payload_parsed"] is True
    registrar._authorize_continue_login.assert_not_called()
    effects.finalize.assert_called_once_with(mailbox, success=False, error="already_registered")
    assert_no_account_actions(registrar, mailbox, effects)


def test_html_mode_survives_initialization_without_fabricating_page_type(initial_auth):
    registrar, _mailbox, _effects = initial_auth(html_response(loader_html()))

    result = registrar._complete_chatgpt_session(
        FIXTURE_EMAIL, "", 1, start_only=True, registration_password_flow=True,
    )

    assert result["email_verification_mode"] == "passwordless_login"
    assert not result.get("page", {}).get("type")
    assert "login_methods" not in result


def test_bare_verification_html_stops_with_honest_persistent_parse_diagnostic(initial_auth):
    registrar, mailbox, effects = initial_auth(html_response(
        "<!doctype html><html><body>Email verification</body></html>",
    ))

    with pytest.raises(RuntimeError, match="registration_password_route_unavailable") as raised:
        registrar.register(1, mailbox_override=mailbox, partial_sink=effects.partial)

    detail = json.loads(str(raised.value).split(": ", 1)[1])
    assert detail["response_kind"] == "not_json"
    assert detail["auth_state_source"] == "none"
    assert detail["auth_response_valid"] is False
    assert detail["auth_state_confirmed"] is False
    assert detail["http_status"] == 200
    assert detail["content_type"].startswith("text/html")
    assert detail["page_type"] == "missing"
    assert detail["route"] == "/email-verification"
    assert registrar._initial_auth_diagnostic["auth_state_source"] == "none"
    assert registrar._initial_auth_diagnostic["response_kind"] == "not_json"
    assert "not_json" in diagnostic_text(registrar, effects)
    assert '"auth_response_valid": false' in diagnostic_text(registrar, effects)
    registrar._authorize_continue_login.assert_not_called()
    assert mailbox.get("_registration_outcome") != "already_registered"
    assert_no_account_actions(registrar, mailbox, effects)


def test_empty_json_initial_state_is_not_reported_as_valid_authentication(initial_auth):
    registrar, mailbox, effects = initial_auth(json_response({}))

    with pytest.raises(RuntimeError, match="registration_password_route_unavailable") as raised:
        registrar.register(1, mailbox_override=mailbox, partial_sink=effects.partial)

    detail = json.loads(str(raised.value).split(": ", 1)[1])
    assert detail["response_kind"] == "json_empty_object"
    assert detail["auth_state_source"] == "json"
    assert detail["auth_payload_parsed"] is False
    assert detail["auth_response_valid"] is False
    assert detail["auth_state_confirmed"] is False
    registrar._authorize_continue_login.assert_not_called()
    assert_no_account_actions(registrar, mailbox, effects)


def test_non_object_json_stops_before_identifier_fallback(initial_auth):
    registrar, mailbox, effects = initial_auth(json_response([], path="/log-in"))

    with pytest.raises(RuntimeError, match="registration_password_route_unavailable") as raised:
        registrar.register(1, mailbox_override=mailbox, partial_sink=effects.partial)

    detail = json.loads(str(raised.value).split(": ", 1)[1])
    assert detail["response_kind"] == "json_non_object"
    assert detail["initial_auth_state_rejected"] is True
    assert detail["auth_response_valid"] is False
    assert detail["auth_state_confirmed"] is False
    registrar._authorize_continue_login.assert_not_called()
    assert mailbox.get("_registration_outcome") != "already_registered"
    assert_no_account_actions(registrar, mailbox, effects)


@pytest.mark.parametrize("path", ["/email-verification", "/log-in"])
def test_mismatched_html_identity_stops_before_identifier_fallback(initial_auth, path):
    registrar, mailbox, effects = initial_auth(html_response(
        loader_html(email="another-fixture@example.test"), path=path,
    ))

    with pytest.raises(RuntimeError, match="registration_password_route_unavailable") as raised:
        registrar.register(1, mailbox_override=mailbox, partial_sink=effects.partial)

    detail = json.loads(str(raised.value).split(": ", 1)[1])
    assert detail["initial_auth_state_rejected"] is True
    assert detail["initial_auth_parse_status"] == "html_identity_mismatch"
    assert detail["auth_response_valid"] is False
    assert detail["auth_state_confirmed"] is False
    assert "another-fixture" not in str(raised.value)
    assert "another-fixture" not in diagnostic_text(registrar, effects)
    registrar._authorize_continue_login.assert_not_called()
    assert mailbox.get("_registration_outcome") != "already_registered"
    assert_no_account_actions(registrar, mailbox, effects)


def test_unmarked_identifier_html_retains_one_identifier_submission(initial_auth):
    registrar, mailbox, effects = initial_auth(html_response(
        "<!doctype html><html><body>Continue</body></html>", path="/log-in",
    ))
    registrar._authorize_continue_login.side_effect = None
    registrar._authorize_continue_login.return_value = {
        "continue_url": AUTH_ORIGIN + "/log-in/password",
        "page": {"type": "password_verification"},
    }

    with pytest.raises(RegisteredEmailDetected):
        registrar.register(1, mailbox_override=mailbox, partial_sink=effects.partial)

    registrar._authorize_continue_login.assert_called_once_with(
        FIXTURE_EMAIL, 1, passwordless=False, restart_invalid_state=False,
    )
    assert registrar._initial_auth_diagnostic["response_kind"] == "not_json"
    assert registrar._initial_auth_diagnostic["auth_state_source"] == "none"
    assert mailbox["_registration_outcome"] == "already_registered"
    assert_no_account_actions(registrar, mailbox, effects)


def test_onboarding_mode_is_visible_but_never_claims_registration_or_ownership(initial_auth):
    registrar, mailbox, effects = initial_auth(html_response(loader_html(mode="onboarding")))

    with pytest.raises(RuntimeError, match="registration_password_route_unavailable") as raised:
        registrar.register(1, mailbox_override=mailbox, partial_sink=effects.partial)

    assert registrar._initial_auth_diagnostic["auth_state_source"] == "html_loader"
    assert registrar._initial_auth_diagnostic["auth_payload_parsed"] is True
    assert "onboarding" in diagnostic_text(registrar, effects)
    assert json.loads(str(raised.value).split(": ", 1)[1])["auth_state_confirmed"] is False
    assert mailbox.get("_registration_outcome") != "already_registered"
    registrar._authorize_continue_login.assert_not_called()
    assert_no_account_actions(registrar, mailbox, effects)


def test_completed_session_for_owned_identity_still_uses_existing_session_reader(initial_auth):
    registrar, _mailbox, effects = initial_auth(html_response(
        "<!doctype html><html><body>Account</body></html>", path="/log-in/password",
    ))
    owned_result = {"email": FIXTURE_EMAIL, "account_id": "fixture-account"}
    registrar._read_chatgpt_session.side_effect = None
    registrar._read_chatgpt_session.return_value = owned_result

    result = registrar._complete_chatgpt_session(FIXTURE_EMAIL, "fixture-owned-password", 1)

    assert result is owned_result
    registrar._read_chatgpt_session.assert_called_once_with(
        FIXTURE_EMAIL, "fixture-owned-password", 1,
    )
    registrar._authorize_continue_login.assert_not_called()
    effects.create.assert_not_called()
    effects.wait.assert_not_called()
    assert effects.request.call_count == 3

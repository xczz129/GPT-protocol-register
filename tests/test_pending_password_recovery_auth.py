"""Offline recovery of an existing pending identity using its saved password."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from register.protocol_registrar import PlatformRegistrar
from register.sessions import JobCancelled


AUTH = "https://auth.openai.com"
EMAIL = "pending@example.test"
PASSWORD = "saved-synthetic-password"
TOTP_SECRET = "saved-synthetic-totp"
CALLBACK = "https://chatgpt.com/api/auth/callback/openai?code=one-use-code&state=fixture"
EMAIL_PAGE = AUTH + "/email-verification"
EMAIL_SEND = AUTH + "/api/accounts/email-otp/send"


def _state(url, page=""):
    return {"continue_url": url, "page": {"type": page}}


def _response(url, data=None):
    return SimpleNamespace(
        url=url, status_code=200, headers={}, text="",
        json=Mock(return_value={} if data is None else data),
    )


def _mfa_state(*, factor_type="totp"):
    return {
        "continue_url": AUTH + "/mfa-challenge/factor-fixture",
        "page": {
            "type": "mfa_challenge",
            "payload": {
                "factor_id": "factor-fixture",
                "factors": [{
                    "id": "factor-fixture", "factor_type": factor_type,
                    "is_recovery": False,
                }],
            },
        },
    }


@pytest.fixture
def recovery(monkeypatch):
    import register.protocol_registrar as module

    registrar = PlatformRegistrar.__new__(PlatformRegistrar)
    registrar.proxy = "http://synthetic-proxy.invalid:8080"
    registrar.device_id = "device-fixture"
    registrar.clearance_user_agent = ""
    registrar.fingerprint = {}
    registrar.session = Mock()
    registrar._complete_chatgpt_session = Mock(return_value={
        "final_url": AUTH + "/log-in/password",
    })
    registrar._authorize_continue_login = Mock(return_value=_state(
        AUTH + "/log-in/password", "password_verification",
    ))
    registrar._post_auth_json = Mock(return_value=_state(CALLBACK, "external_url"))
    registrar._navigate_auth_step = Mock(return_value=_response("https://chatgpt.com/"))
    registrar._read_chatgpt_session = Mock(return_value={
        "email": EMAIL, "access_token": "fixture-access", "refresh_token": "fixture-refresh",
    })
    registrar._validate_otp = Mock(return_value=_state(CALLBACK, "external_url"))
    registrar._select_workspace = Mock(return_value=CALLBACK)
    registrar._dump_client_auth_session = Mock(return_value={})
    registrar._diagnostic = Mock()

    forbidden = []
    for name in (
        "_register_user", "_create_account", "_set_registration_password", "_add_password",
        "_send_password_reset_otp", "_complete_post_registration_password", "change_password",
        "_send_passwordless_otp", "_passwordless_login", "login", "register", "_enroll_totp",
    ):
        action = Mock(side_effect=AssertionError(f"pending recovery must not call {name}"))
        setattr(registrar, name, action)
        forbidden.append(action)
    for name in ("create_mailbox", "release_mailbox", "request_with_local_retry"):
        action = Mock(side_effect=AssertionError(f"offline recovery must not call {name}"))
        monkeypatch.setattr(module, name, action)
        forbidden.append(action)

    mailbox = {"address": EMAIL, "provider": "smsbower_mail", "provider_ref": "saved-lease"}
    wait = Mock(return_value="123456")
    boundary = Mock()
    accepted = Mock()
    totp = Mock(return_value="654321")
    monkeypatch.setattr(module, "wait_for_code", wait)
    monkeypatch.setattr(module, "mark_mailbox_code_boundary", boundary)
    monkeypatch.setattr(module, "mark_smsbower_otp_accepted", accepted)
    monkeypatch.setattr(module, "totp_code", totp)
    monkeypatch.setattr(module, "_totp_code", totp)
    monkeypatch.setattr(module, "step", Mock())

    trace = Mock()
    for name, action in (
        ("start", registrar._complete_chatgpt_session),
        ("post", registrar._post_auth_json),
        ("navigate", registrar._navigate_auth_step),
        ("read_session", registrar._read_chatgpt_session),
        ("boundary", boundary), ("wait", wait),
        ("validate", registrar._validate_otp), ("accepted", accepted),
    ):
        trace.attach_mock(action, name)
    stop_token = module._STOP_CHECK.set(lambda: False)
    try:
        yield SimpleNamespace(
            registrar=registrar, module=module, mailbox=mailbox,
            wait=wait, boundary=boundary, accepted=accepted, totp=totp, trace=trace,
        )
    finally:
        module._STOP_CHECK.reset(stop_token)
        for action in forbidden:
            action.assert_not_called()


def _recover(recovery, **kwargs):
    return recovery.registrar.login_pending_with_password(EMAIL, PASSWORD, 7, **kwargs)


def _post_operations(recovery):
    return [call.args[4] for call in recovery.registrar._post_auth_json.call_args_list]


@pytest.mark.parametrize("stored_totp", ["", TOTP_SECRET])
def test_password_callback_completes_without_unrequested_factors(recovery, stored_totp):
    registrar = recovery.registrar
    result = _recover(recovery, totp_secret=stored_totp)

    assert result["access_token"] == "fixture-access"
    registrar._complete_chatgpt_session.assert_called_once_with(
        EMAIL, PASSWORD, 7, start_only=True,
    )
    registrar._authorize_continue_login.assert_not_called()
    assert registrar._post_auth_json.call_args.args[:5] == (
        AUTH + "/api/accounts/password/verify", AUTH + "/log-in/password",
        {"password": PASSWORD}, 7, "password_verify",
    )
    assert _post_operations(recovery) == ["password_verify"]
    registrar._navigate_auth_step.assert_called_once()
    assert registrar._navigate_auth_step.call_args.args[0] == CALLBACK
    registrar._read_chatgpt_session.assert_called_once_with(EMAIL, PASSWORD, 7)
    assert [call[0] for call in recovery.trace.mock_calls] == [
        "start", "post", "navigate", "read_session",
    ]
    recovery.wait.assert_not_called()
    recovery.totp.assert_not_called()


def test_api_password_page_is_sufficient_without_reidentifying(recovery):
    recovery.registrar._complete_chatgpt_session.return_value = {
        "final_url": AUTH + "/authorize", "page": {"type": "password_verification"},
    }
    assert _recover(recovery)["access_token"] == "fixture-access"
    recovery.registrar._authorize_continue_login.assert_not_called()


def test_identifier_route_uses_password_continuation(recovery):
    recovery.registrar._complete_chatgpt_session.return_value = {"final_url": AUTH + "/log-in"}
    assert _recover(recovery)["access_token"] == "fixture-access"
    recovery.registrar._authorize_continue_login.assert_called_once_with(
        EMAIL, 7, passwordless=False, restart_invalid_state=False,
    )


def test_password_then_totp_uses_saved_factor_and_completes(recovery):
    registrar = recovery.registrar
    registrar._post_auth_json.side_effect = [
        _mfa_state(), {}, _state(CALLBACK, "external_url"),
    ]
    result = _recover(recovery, totp_secret=TOTP_SECRET)

    assert result["access_token"] == "fixture-access"
    assert _post_operations(recovery) == ["password_verify", "mfa_issue_challenge", "mfa_verify"]
    issue, verify = registrar._post_auth_json.call_args_list[1:]
    assert issue.args[0] == AUTH + "/api/accounts/mfa/issue_challenge"
    assert issue.args[2] == {"id": "factor-fixture", "type": "totp", "force_fresh_challenge": False}
    assert verify.args[0] == AUTH + "/api/accounts/mfa/verify"
    assert verify.args[2] == {"id": "factor-fixture", "type": "totp", "code": "654321"}
    recovery.totp.assert_called_once_with(TOTP_SECRET)
    recovery.wait.assert_not_called()
    registrar._navigate_auth_step.assert_called_once()
    assert registrar._navigate_auth_step.call_args.args[0] == CALLBACK


@pytest.mark.parametrize("continuation", [EMAIL_PAGE, EMAIL_SEND])
def test_email_otp_is_read_only_after_password_and_boundary(recovery, continuation):
    registrar = recovery.registrar
    registrar._post_auth_json.return_value = _state(continuation, "email_otp_verification")
    registrar._navigate_auth_step.side_effect = lambda url, *_args: _response(
        EMAIL_PAGE if url == continuation else "https://chatgpt.com/",
    )
    result = _recover(recovery, mailbox=recovery.mailbox)

    assert result["access_token"] == "fixture-access"
    recovery.wait.assert_called_once_with(recovery.mailbox, register_proxy=registrar.proxy)
    registrar._validate_otp.assert_called_once()
    assert registrar._validate_otp.call_args.args == ("123456", 7)
    recovery.accepted.assert_called_once_with(recovery.mailbox)
    recovery.boundary.assert_called_once()
    assert recovery.boundary.call_args.args[0] is recovery.mailbox
    events = [call[0] for call in recovery.trace.mock_calls]
    # Password verification may send the email itself; a later boundary could
    # exclude that fresh code before we start polling the saved mailbox.
    assert events.index("boundary") < events.index("post") < events.index("navigate") < events.index("wait")
    assert events.index("wait") < events.index("validate") < events.index("accepted") < events.index("read_session")
    assert [call.args[0] for call in registrar._navigate_auth_step.call_args_list] == [continuation, CALLBACK]
    assert _post_operations(recovery) == ["password_verify"]


@pytest.mark.parametrize("mailbox", [None, {}, {"address": "other@example.test", "provider": "smsbower_mail"}])
def test_email_challenge_requires_original_mailbox_before_consumption(recovery, mailbox):
    recovery.registrar._post_auth_json.return_value = _state(EMAIL_SEND, "email_otp_verification")
    with pytest.raises(RuntimeError, match="pending_recovery"):
        _recover(recovery, mailbox=mailbox)
    recovery.wait.assert_not_called()
    recovery.boundary.assert_not_called()
    recovery.registrar._validate_otp.assert_not_called()
    recovery.registrar._navigate_auth_step.assert_not_called()
    recovery.registrar._read_chatgpt_session.assert_not_called()


@pytest.mark.parametrize("secret,factor_type", [("", "totp"), (TOTP_SECRET, "email")])
def test_mfa_challenge_requires_saved_totp_and_totp_factor(recovery, secret, factor_type):
    recovery.registrar._post_auth_json.return_value = _mfa_state(factor_type=factor_type)
    with pytest.raises(RuntimeError, match="pending_recovery"):
        _recover(recovery, mailbox=recovery.mailbox, totp_secret=secret)
    assert _post_operations(recovery) == ["password_verify"]
    recovery.wait.assert_not_called()
    recovery.totp.assert_not_called()
    recovery.registrar._read_chatgpt_session.assert_not_called()


@pytest.mark.parametrize("route,page", [
    ("/create-account/password", "password_creation"),
    ("/reset-password/new-password", "password_reset_new_password"),
    ("/about-you", "profile"),
])
@pytest.mark.parametrize("stage", ["start", "identity", "password", "mfa", "email_otp"])
def test_signup_profile_and_reset_routes_fail_closed(recovery, route, page, stage):
    registrar = recovery.registrar
    state = _state(AUTH + route, page)
    if stage == "start":
        registrar._complete_chatgpt_session.return_value = {"final_url": AUTH + route, "page": state["page"]}
    elif stage == "identity":
        registrar._complete_chatgpt_session.return_value = {"final_url": AUTH + "/log-in"}
        registrar._authorize_continue_login.return_value = state
    elif stage == "password":
        registrar._post_auth_json.return_value = state
    elif stage == "mfa":
        registrar._post_auth_json.side_effect = [_mfa_state(), {}, state]
    else:
        registrar._post_auth_json.return_value = _state(EMAIL_PAGE, "email_otp_verification")
        registrar._navigate_auth_step.return_value = _response(EMAIL_PAGE)
        registrar._validate_otp.return_value = state

    with pytest.raises(RuntimeError, match="pending_recovery"):
        _recover(recovery, mailbox=recovery.mailbox, totp_secret=TOTP_SECRET)
    if stage in {"start", "identity"}:
        registrar._post_auth_json.assert_not_called()
    if stage != "email_otp":
        recovery.wait.assert_not_called()
        registrar._navigate_auth_step.assert_not_called()
    else:
        recovery.wait.assert_called_once()
        assert [call.args[0] for call in registrar._navigate_auth_step.call_args_list] == [EMAIL_PAGE]
    registrar._read_chatgpt_session.assert_not_called()


def test_email_route_before_password_never_polls_or_verifies(recovery):
    registrar = recovery.registrar
    registrar._complete_chatgpt_session.return_value = {"final_url": AUTH + "/log-in"}
    registrar._authorize_continue_login.return_value = _state(EMAIL_PAGE, "email_otp_verification")
    with pytest.raises(RuntimeError, match="pending_recovery"):
        _recover(recovery, mailbox=recovery.mailbox)
    registrar._post_auth_json.assert_not_called()
    recovery.wait.assert_not_called()
    recovery.accepted.assert_not_called()


def test_workspace_selection_continues_to_callback(recovery):
    registrar = recovery.registrar
    registrar._post_auth_json.return_value = _state(AUTH + "/workspace", "workspace_selection")
    assert _recover(recovery)["access_token"] == "fixture-access"
    registrar._select_workspace.assert_called_once()
    assert AUTH + "/workspace" in registrar._select_workspace.call_args.args
    registrar._navigate_auth_step.assert_called_once()
    assert registrar._navigate_auth_step.call_args.args[0] == CALLBACK


def test_session_read_failure_does_not_replay_consumed_callback(recovery):
    registrar = recovery.registrar
    registrar._navigate_auth_step.return_value = _response(CALLBACK)
    registrar._read_chatgpt_session.side_effect = RuntimeError("fixture session unavailable")
    with pytest.raises(RuntimeError):
        _recover(recovery)
    registrar._complete_chatgpt_session.assert_called_once()
    registrar._post_auth_json.assert_called_once()
    registrar._navigate_auth_step.assert_called_once()
    registrar._read_chatgpt_session.assert_called_once()


def test_repeated_email_challenge_cannot_consume_another_code(recovery):
    challenge = _state(EMAIL_PAGE, "email_otp_verification")
    recovery.registrar._post_auth_json.return_value = challenge
    recovery.registrar._navigate_auth_step.return_value = _response(EMAIL_PAGE)
    recovery.registrar._validate_otp.return_value = challenge
    with pytest.raises(RuntimeError, match="pending_recovery"):
        _recover(recovery, mailbox=recovery.mailbox)
    recovery.wait.assert_called_once()
    recovery.registrar._validate_otp.assert_called_once()
    recovery.accepted.assert_called_once()
    assert recovery.registrar._navigate_auth_step.call_count <= 2
    recovery.registrar._read_chatgpt_session.assert_not_called()


def test_rejected_otp_is_not_recorded_as_accepted(recovery):
    registrar = recovery.registrar
    registrar._post_auth_json.return_value = _state(EMAIL_PAGE, "email_otp_verification")
    registrar._navigate_auth_step.return_value = _response(EMAIL_PAGE)
    registrar._validate_otp.side_effect = RuntimeError("fixture invalid_otp")
    with pytest.raises(RuntimeError):
        _recover(recovery, mailbox=recovery.mailbox)
    recovery.wait.assert_called_once()
    registrar._validate_otp.assert_called_once()
    recovery.accepted.assert_not_called()
    registrar._read_chatgpt_session.assert_not_called()
    assert [call.args[0] for call in registrar._navigate_auth_step.call_args_list] == [EMAIL_PAGE]


@pytest.mark.parametrize("stage", ["before_start", "after_password", "during_otp_wait", "after_otp_wait"])
def test_cancellation_stops_before_next_authentication_action(recovery, stage):
    registrar = recovery.registrar
    cancelled = {"value": stage == "before_start"}
    stop_token = recovery.module._STOP_CHECK.set(lambda: cancelled["value"])

    def password_result(*_args, **_kwargs):
        if stage == "after_password":
            cancelled["value"] = True
        return _state(EMAIL_PAGE, "email_otp_verification")

    def wait_result(*_args, **_kwargs):
        if stage == "during_otp_wait":
            raise JobCancelled("fixture stop requested")
        cancelled["value"] = True
        return "123456"

    registrar._post_auth_json.side_effect = password_result
    if stage in {"during_otp_wait", "after_otp_wait"}:
        recovery.wait.side_effect = wait_result
    try:
        with pytest.raises(JobCancelled):
            _recover(recovery, mailbox=recovery.mailbox)
    finally:
        recovery.module._STOP_CHECK.reset(stop_token)

    if stage == "before_start":
        registrar._complete_chatgpt_session.assert_not_called()
        registrar._post_auth_json.assert_not_called()
    if stage in {"before_start", "after_password"}:
        recovery.wait.assert_not_called()
        registrar._navigate_auth_step.assert_not_called()
    registrar._validate_otp.assert_not_called()
    recovery.accepted.assert_not_called()
    registrar._read_chatgpt_session.assert_not_called()

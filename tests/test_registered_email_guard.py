from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest

from register.protocol_registrar import PlatformRegistrar
from register.registration_outcome import (
    RegisteredEmailDetected,
    is_duplicate_registration_error,
    registration_auth_state,
)


@pytest.fixture
def registration(monkeypatch):
    import register.protocol_registrar as module

    mailbox = {"address": "candidate@gmail.com", "provider": "smsbower_mail"}
    registrar = PlatformRegistrar.__new__(PlatformRegistrar)
    registrar.proxy = ""
    registrar.device_id = "device-fixture"
    registrar.fingerprint = {}
    for name in (
        "_log_identity", "_diagnostic", "_navigate_auth_step", "_enroll_totp",
        "_send_otp", "_send_passwordless_otp", "_send_password_reset_otp",
        "_reset_auth_cookies", "login", "_passwordless_login", "_register_user",
        "_complete_post_registration_password", "_authorize_continue_login",
        "_register_passwordless_existing_account",
    ):
        setattr(registrar, name, MagicMock())
    registrar._complete_chatgpt_session = MagicMock(return_value={
        "final_url": "https://auth.openai.com/create-account/password",
    })
    registrar._set_registration_password = MagicMock(return_value={
        "continue_url": "https://auth.openai.com/api/accounts/email-otp/send",
    })
    registrar._validate_otp = MagicMock(return_value={
        "continue_url": "https://auth.openai.com/about-you",
    })
    registrar._create_account = MagicMock(return_value={})
    registrar._read_chatgpt_session = MagicMock(return_value={"access_token": "fixture-token"})
    registrar._build_registration_record = MagicMock(return_value={"email": mailbox["address"]})
    effects = SimpleNamespace(
        create=MagicMock(return_value=mailbox),
        wait=MagicMock(return_value="123456"),
        finalize=MagicMock(),
        begin=MagicMock(wraps=module.begin_attempt),
        accepted=MagicMock(side_effect=lambda value: value.__setitem__("_otp_accepted_by_server", True)),
    )
    monkeypatch.setattr(module, "create_mailbox", effects.create)
    monkeypatch.setattr(module, "wait_for_code", effects.wait)
    monkeypatch.setattr(module, "mark_mailbox_result", effects.finalize)
    monkeypatch.setattr(module, "begin_attempt", effects.begin)
    monkeypatch.setattr(module, "mark_smsbower_otp_accepted", effects.accepted)
    monkeypatch.setattr(module, "mark_mailbox_code_boundary", MagicMock())
    monkeypatch.setattr(module, "release_mailbox", MagicMock())
    monkeypatch.setattr(module, "request_with_local_retry", MagicMock(
        side_effect=AssertionError("all registration HTTP must be mocked"),
    ))
    monkeypatch.setattr(module, "step", MagicMock())
    monkeypatch.setattr(module, "current_runtime_settings", lambda: SimpleNamespace(enable_totp_after_register=False))
    return registrar, mailbox, effects


@pytest.mark.parametrize("initial_state", [
    {"final_url": "https://auth.openai.com/log-in/password"},
    {"final_url": "https://auth.openai.com/login/password?screen_hint=signup"},
    {"final_url": "https://auth.openai.com/authorize", "page": {"type": "password_verification"}},
])
@pytest.mark.parametrize("role", ["primary", "child"])
@pytest.mark.parametrize("provider", ["smsbower_mail", "online_disposablemail"])
def test_existing_password_route_is_skipped_before_any_account_action(
    registration, initial_state, role, provider,
):
    """A purchased existing identity must never enter login or credential reset."""
    registrar, mailbox, effects = registration
    mailbox["provider"] = provider
    mailbox["_smsbower_pair_role"] = role
    registrar._complete_chatgpt_session.return_value = initial_state
    partial = MagicMock()

    with pytest.raises(RegisteredEmailDetected) as raised:
        registrar.register(1, partial_sink=partial)

    assert raised.value.email == mailbox["address"]
    assert mailbox["_registration_outcome"] == "already_registered"
    assert not mailbox.get("_registration_identity_created")
    assert "_registration_existing_takeover_detail" not in mailbox
    for name in (
        "_authorize_continue_login", "_set_registration_password", "_register_user",
        "_send_otp", "_send_passwordless_otp", "_send_password_reset_otp",
        "_validate_otp", "_create_account", "_read_chatgpt_session", "login",
        "_passwordless_login", "_complete_post_registration_password",
        "_register_passwordless_existing_account", "_enroll_totp",
    ):
        getattr(registrar, name).assert_not_called()
    effects.wait.assert_not_called()
    partial.assert_not_called()
    effects.finalize.assert_called_once_with(mailbox, success=False, error="already_registered")


def test_existing_mfa_route_still_rejects_before_any_action(registration):
    """An MFA challenge is an existing identity, so registration stops."""
    registrar, mailbox, effects = registration
    registrar._complete_chatgpt_session.return_value = {
        "final_url": "https://auth.openai.com/mfa-challenge",
    }
    with pytest.raises(RegisteredEmailDetected) as raised:
        registrar.register(1)
    assert raised.value.email == mailbox["address"]
    assert mailbox["_registration_outcome"] == "already_registered"
    effects.wait.assert_not_called()
    registrar._register_passwordless_existing_account.assert_not_called()
    effects.finalize.assert_called_once_with(mailbox, success=False, error="already_registered")


def test_identifier_continuation_checks_before_navigating_login(registration):
    registrar, mailbox, effects = registration
    registrar._complete_chatgpt_session.return_value = {"final_url": "https://auth.openai.com/log-in"}
    registrar._authorize_continue_login.return_value = {
        "continue_url": "https://auth.openai.com/log-in/password",
        "page": {"type": "password_verification"},
    }
    with pytest.raises(RegisteredEmailDetected):
        registrar.register(1)
    registrar._authorize_continue_login.assert_called_once_with(
        mailbox["address"], 1, passwordless=False, restart_invalid_state=False,
    )
    registrar._register_passwordless_existing_account.assert_not_called()
    registrar._navigate_auth_step.assert_not_called()
    registrar._set_registration_password.assert_not_called()
    effects.wait.assert_not_called()


def test_fresh_password_signup_proceeds_and_uses_preleased_mailbox(registration):
    registrar, mailbox, effects = registration
    confirmed_result = registrar._set_registration_password.return_value

    def confirm_creation(*_args, **_kwargs):
        assert not mailbox.get("_registration_identity_created")
        return confirmed_result

    registrar._set_registration_password.side_effect = confirm_creation
    result = registrar.register(1, mailbox_override=mailbox)
    assert result["email"] == mailbox["address"]
    effects.create.assert_not_called()
    registrar._complete_chatgpt_session.assert_called_once_with(
        mailbox["address"], "", 1, start_only=True, registration_password_flow=True,
    )
    registrar._set_registration_password.assert_called_once()
    registrar._create_account.assert_called_once()
    effects.wait.assert_called_once()
    effects.begin.assert_called_once_with(mailbox)
    effects.accepted.assert_called_once_with(mailbox)
    assert mailbox["_registration_identity_created"] is True
    assert "_registration_outcome" not in mailbox


UNCONFIRMED_CREATION_RESPONSES = [
    pytest.param(None, id="missing-response"),
    pytest.param({}, id="empty-object"),
    pytest.param({"success": True}, id="success-without-next-step"),
    pytest.param({
        "error": {"code": "invalid_request"},
        "continue_url": "https://auth.openai.com/api/accounts/email-otp/send",
    }, id="business-error-with-next-step"),
    pytest.param({
        "continue_url": "https://untrusted.example.test/email-verification",
        "page": {"type": "email_otp_verification"},
    }, id="untrusted-continuation"),
    pytest.param({
        "continue_url": "https://auth.openai.com/api/accounts/email-otp/send",
        "page": {"type": "about_you"},
    }, id="conflicting-route-and-page"),
    pytest.param({
        "continue_url": "https://auth.openai.com/api/accounts/email-otp/send",
        "page": {"type": "email_otp_verification", "payload": {
            "url": "https://auth.openai.com/about-you",
        }},
    }, id="conflicting-continuations"),
]


@pytest.mark.parametrize("payload", UNCONFIRMED_CREATION_RESPONSES)
def test_register_does_not_claim_creation_from_unconfirmed_password_response(registration, payload):
    registrar, mailbox, effects = registration
    registrar._set_registration_password = PlatformRegistrar._set_registration_password.__get__(registrar)
    registrar._register_user.return_value = payload
    partial = MagicMock()

    expected_error = (
        "set_password_failed: invalid_request"
        if isinstance(payload, dict) and payload.get("error")
        else "registration_identity_response_unconfirmed"
    )
    with pytest.raises(RuntimeError, match=expected_error):
        registrar.register(1, mailbox_override=mailbox, partial_sink=partial)

    registrar._register_user.assert_called_once()
    assert not mailbox.get("_registration_identity_created")
    assert mailbox.get("_registration_outcome") != "already_registered"
    partial.assert_not_called()
    effects.wait.assert_not_called()
    effects.accepted.assert_not_called()
    for name in ("_navigate_auth_step", "_validate_otp", "_create_account", "_read_chatgpt_session", "_enroll_totp"):
        getattr(registrar, name).assert_not_called()


def test_confirmed_password_response_allows_creation_marker_and_original_otp_flow(registration):
    registrar, mailbox, effects = registration
    registrar._set_registration_password = PlatformRegistrar._set_registration_password.__get__(registrar)
    registrar._register_user.return_value = {
        "continue_url": "https://auth.openai.com/api/accounts/email-otp/send",
        "page": {"type": "email_otp_send"},
    }

    result = registrar.register(1, mailbox_override=mailbox)

    assert result["email"] == mailbox["address"]
    assert mailbox["_registration_identity_created"] is True
    registrar._register_user.assert_called_once()
    effects.wait.assert_called_once()
    effects.accepted.assert_called_once_with(mailbox)


def test_other_mail_provider_keeps_original_route_and_behavior(registration):
    registrar, mailbox, effects = registration
    mailbox["provider"] = "icloud_api"
    registrar._complete_chatgpt_session.return_value = {"final_url": "https://auth.openai.com/log-in/password"}
    registrar.register(1)
    registrar._complete_chatgpt_session.assert_called_once_with(mailbox["address"], "", 1, start_only=True)
    registrar._authorize_continue_login.assert_not_called()
    registrar._set_registration_password.assert_called_once()
    effects.wait.assert_called_once()
    effects.begin.assert_not_called()
    effects.accepted.assert_called_once_with(mailbox)


def test_skipped_smsbower_registration_starts_metrics_before_auth(registration):
    registrar, mailbox, effects = registration
    registrar._complete_chatgpt_session.return_value = {"final_url": "https://auth.openai.com/log-in/password"}
    with pytest.raises(RegisteredEmailDetected):
        registrar.register(1)
    effects.create.assert_called_once()
    effects.begin.assert_called_once_with(mailbox)
    registrar._register_passwordless_existing_account.assert_not_called()
    effects.accepted.assert_not_called()


def test_preleased_metric_token_is_reused_by_registration_entry(registration):
    from common.mail_registration_metrics import ATTEMPT_FIELD, begin_attempt, get_summary

    registrar, mailbox, effects = registration
    existing_token = begin_attempt(mailbox)
    registrar.register(1, mailbox_override=mailbox)
    assert mailbox[ATTEMPT_FIELD] == existing_token
    assert get_summary()["totals"]["attempted"] == 1
    effects.begin.assert_called_once_with(mailbox)


def test_browser_entry_counts_preleased_mailbox_once(monkeypatch):
    import register.flow as flow
    from common.mail_registration_metrics import ATTEMPT_FIELD, begin_attempt, get_summary

    mailbox = {"provider": "smsbower_mail", "address": "leased@gmail.com"}
    token = begin_attempt(mailbox)
    start_metric = MagicMock(wraps=begin_attempt)
    create = MagicMock()
    monkeypatch.setattr(flow, "begin_attempt", start_metric)
    monkeypatch.setattr(flow, "create_mailbox", create)
    monkeypatch.setattr(flow, "open_and_connect", AsyncMock(side_effect=RuntimeError("fixture_browser_start_failed")))
    monkeypatch.setattr(flow, "mark_mailbox_result", MagicMock())
    monkeypatch.setattr(flow, "_write_browser_diagnostic", AsyncMock(return_value=""))
    monkeypatch.setattr(flow, "_log", MagicMock())
    monkeypatch.setattr(flow, "_ensure_print_prefix", MagicMock())

    result = asyncio.run(flow.register_one(1, 1, None, mail_config={}, mailbox_override=mailbox, persist_result=False))
    assert result is None
    create.assert_not_called()
    start_metric.assert_called_once_with(mailbox)
    assert mailbox[ATTEMPT_FIELD] == token
    assert get_summary()["totals"]["attempted"] == 1


@pytest.mark.parametrize("provider", ["smsbower_mail", "icloud_api"])
def test_incomplete_consumed_browser_mailbox_only_changes_smsbower_outcome(monkeypatch, provider):
    import register.flow as flow

    mailbox = {
        "provider": provider, "address": "consumed@gmail.com",
        "_otp_accepted_by_server": True,
    }
    finalize = MagicMock()
    monkeypatch.setattr(flow, "open_and_connect", AsyncMock(side_effect=RuntimeError("fixture_incomplete_registration")))
    monkeypatch.setattr(flow, "mark_mailbox_result", finalize)
    monkeypatch.setattr(flow, "_write_browser_diagnostic", AsyncMock(return_value=""))
    monkeypatch.setattr(flow, "_log", MagicMock())
    monkeypatch.setattr(flow, "_ensure_print_prefix", MagicMock())
    asyncio.run(flow.register_one(1, 1, None, mail_config={}, mailbox_override=mailbox, persist_result=False))
    if provider == "smsbower_mail":
        finalize.assert_called_once_with(mailbox, success=False, error="fixture_incomplete_registration")
    else:
        finalize.assert_called_once_with(mailbox, success=True)


def test_browser_otp_acceptance_is_persisted_before_navigation_cancellation(monkeypatch):
    import register.flow as flow

    mailbox = {"provider": "smsbower_mail", "address": "accepted@gmail.com"}
    handlers = {}
    page = SimpleNamespace(
        on=lambda event, handler: handlers.__setitem__(event, handler),
        remove_listener=MagicMock(),
    )
    response = SimpleNamespace(url="https://auth.openai.com/api/accounts/email-otp/validate", status=200)

    async def fill(*_args, **_kwargs):
        handlers["response"](response)
        return True

    accepted = MagicMock(side_effect=lambda value: value.__setitem__("_otp_accepted_by_server", True))
    monkeypatch.setattr(flow, "mark_smsbower_otp_accepted", accepted)
    monkeypatch.setattr(flow, "dismiss_cookie_banner", AsyncMock())
    monkeypatch.setattr(flow, "fill_otp_verified", fill)
    monkeypatch.setattr(flow, "wait_for_any", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(flow._submit_verification_code(page, "123456", mailbox=mailbox))
    accepted.assert_called_once_with(mailbox)
    assert mailbox["_otp_accepted_by_server"] is True


@pytest.mark.parametrize("url,status", [
    ("https://auth.openai.com/api/accounts/email-otp/validate", 401),
    ("https://other.example/api/accounts/email-otp/validate", 200),
    ("https://auth.openai.com/log?url=/api/accounts/email-otp/validate", 200),
])
def test_browser_otp_hook_ignores_rejected_or_unrelated_responses(monkeypatch, url, status):
    import register.flow as flow

    mailbox = {"provider": "smsbower_mail", "address": "unconfirmed@gmail.com"}
    handlers = {}
    page = SimpleNamespace(
        on=lambda event, handler: handlers.__setitem__(event, handler),
        remove_listener=MagicMock(),
    )

    async def fill(*_args, **_kwargs):
        handlers["response"](SimpleNamespace(url=url, status=status))
        return True

    accepted = MagicMock()
    monkeypatch.setattr(flow, "mark_smsbower_otp_accepted", accepted)
    monkeypatch.setattr(flow, "dismiss_cookie_banner", AsyncMock())
    monkeypatch.setattr(flow, "fill_otp_verified", fill)
    monkeypatch.setattr(flow, "wait_for_any", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(flow._submit_verification_code(page, "123456", mailbox=mailbox))
    accepted.assert_not_called()
    assert "_otp_accepted_by_server" not in mailbox


def test_explicit_duplicate_from_register_is_skip_before_otp(registration):
    registrar, mailbox, effects = registration
    registrar._set_registration_password.side_effect = RuntimeError(
        'user_register_http_409, detail={"error":{"code":"user_already_exists"}}'
    )
    with pytest.raises(RegisteredEmailDetected):
        registrar.register(1)
    assert mailbox["_registration_outcome"] == "already_registered"
    effects.wait.assert_not_called()
    registrar._create_account.assert_not_called()
    effects.finalize.assert_called_once()


def test_network_failure_does_not_claim_already_registered(registration):
    registrar, mailbox, effects = registration
    registrar._set_registration_password.side_effect = RuntimeError("curl: (35) SSL_connect failed")
    with pytest.raises(RuntimeError, match="SSL_connect"):
        registrar.register(1)
    assert "_registration_outcome" not in mailbox
    assert not mailbox.get("_registration_identity_created")
    effects.wait.assert_not_called()


def test_ambiguous_otp_route_fails_without_consuming_otp(registration):
    """A genuinely unrecognized route still fails without consuming the OTP."""
    registrar, mailbox, effects = registration
    registrar._complete_chatgpt_session.return_value = {"final_url": "https://auth.openai.com/log-in"}
    registrar._authorize_continue_login.return_value = {"page": {"type": "some_new_page"}}
    with pytest.raises(RuntimeError, match="registration_password_route_unavailable"):
        registrar.register(1)
    assert "_registration_outcome" not in mailbox
    registrar._set_registration_password.assert_not_called()
    effects.wait.assert_not_called()


def test_bare_otp_initial_route_proceeds_like_icloud_flow(registration):
    """OpenAI 新流程: 新邮箱初始认证直接进入邮箱验证路由，按 iCloud 渠道同款放行。

    回收邮箱身份仍由 user/register 的 duplicate 错误码兜底识别。"""
    registrar, mailbox, effects = registration
    registrar._complete_chatgpt_session.return_value = {"final_url": "https://auth.openai.com/email-verification"}
    registrar.register(1)
    assert "_registration_outcome" not in mailbox
    assert mailbox.get("_registration_identity_created")
    registrar._authorize_continue_login.assert_not_called()
    registrar._register_passwordless_existing_account.assert_not_called()
    registrar._set_registration_password.assert_called_once()
    effects.wait.assert_called_once()


def test_recovery_after_own_identity_creation_is_not_existing_account(registration):
    registrar, mailbox, effects = registration
    registrar._complete_chatgpt_session.side_effect = [
        {"final_url": "https://auth.openai.com/create-account/password"},
        {"final_url": "https://auth.openai.com/log-in/password"},
    ]
    registrar._validate_otp.side_effect = [
        RuntimeError("invalid_state"),
        {"continue_url": "https://auth.openai.com/about-you"},
    ]
    registrar._create_account.return_value = {"error": {"code": "user_already_exists"}}
    registrar.register(1)
    assert "_registration_outcome" not in mailbox
    assert effects.wait.call_count == 2
    registrar._read_chatgpt_session.assert_called_once()


def test_own_password_callback_does_not_repeat_password_add_or_reset(registration):
    registrar, mailbox, _effects = registration
    registrar._validate_otp.return_value = {"continue_url": "https://chatgpt.com/api/auth/callback/openai"}
    registrar.register(1)
    registrar._complete_post_registration_password.assert_not_called()
    registrar._send_password_reset_otp.assert_not_called()
    assert "_registration_outcome" not in mailbox


def test_protocol_signup_keeps_icloud_signin_shape_without_password_connection(registration, monkeypatch):
    import register.protocol_registrar as module

    registrar, mailbox, _effects = registration
    registrar.session = MagicMock()
    registrar.clearance_user_agent = ""

    def response(payload=None, url=""):
        return SimpleNamespace(status_code=200, json=lambda: payload or {}, url=url, headers={}, text="")

    request = MagicMock(side_effect=[
        (response({"csrfToken": "fixture-csrf"}), ""),
        (response({"url": "https://auth.openai.com/authorize"}), ""),
        (response({"page": {"type": "password_verification"}}, "https://auth.openai.com/log-in/password"), ""),
    ])
    monkeypatch.setattr(module, "request_with_local_retry", request)
    monkeypatch.setattr(module, "_cookie_values", lambda _session: {})
    result = PlatformRegistrar._complete_chatgpt_session(
        registrar, mailbox["address"], "", 1, start_only=True, registration_password_flow=True,
    )
    signin_query = parse_qs(urlsplit(request.call_args_list[1].args[2]).query)
    # 新邮箱注册与 iCloud 渠道保持同一 signin 形态：不带 connection=password，
    # 否则 OpenAI 新流程会把新地址直接路由到 /email-verification。
    assert "connection" not in signin_query
    assert "max_age" not in signin_query
    assert signin_query["screen_hint"] == ["login_or_signup"]
    assert "reauth" not in signin_query
    assert result["page"]["type"] == "password_verification"
    assert request.call_count == 3


@pytest.mark.parametrize("url,page_type,expected", [
    ("https://auth.openai.com/create-account/password?next=/log-in/password", "", "signup"),
    ("https://auth.openai.com/log-in/password?next=/create-account/password", "", "existing"),
    ("https://auth.openai.com/log-in?label=设置密码并登录", "", ""),
    ("https://auth.openai.com/create-account/password", "设置密码并登录", "signup"),
    ("https://example.test/log-in/password", "password_verification", ""),
    ("https://auth.openai.com/email-verification", "email_otp_verification", ""),
])
def test_auth_classification_uses_route_and_api_type(url, page_type, expected):
    assert registration_auth_state(url, page_type) == expected


@pytest.mark.parametrize("error,expected", [
    ({"error": {"code": "username_already_exists"}}, True),
    ("set_password_failed: email_already_registered", True),
    ({"error": {"code": "invalid_state", "message": "user_already_exists"}}, False),
    ("页面按钮显示设置密码并登录", False),
    ("user_already_exists in unrelated diagnostic text", False),
])
def test_duplicate_classifier_requires_explicit_error_code(error, expected):
    assert is_duplicate_registration_error(error) is expected


def browser_route(url, *, referer="", payload=None, status=200):
    response = SimpleNamespace(status=status, json=AsyncMock(return_value=payload))
    return SimpleNamespace(
        request=SimpleNamespace(url=url, headers={"referer": referer}),
        fetch=AsyncMock(return_value=response), fulfill=AsyncMock(),
        continue_=AsyncMock(), abort=AsyncMock(),
    )


def test_browser_existing_response_blocks_otp_before_ui_can_use_it():
    from register.page import check_fresh_registration_page, install_fresh_registration_guard

    async def run():
        mailbox = {"address": "existing@gmail.com", "provider": "smsbower_mail"}
        context = SimpleNamespace(route=AsyncMock())
        await install_fresh_registration_guard(context, mailbox)
        handler = context.route.await_args_list[1].args[1]
        identity = browser_route(
            "https://auth.openai.com/api/accounts/authorize/continue",
            payload={"continue_url": "https://auth.openai.com/log-in/password"},
        )
        await handler(identity)
        assert mailbox["_registration_outcome"] == "already_registered"
        for path in ("password/verify", "passwordless/send-otp", "password/reset", "email-otp/send", "email-otp/validate", "user/register", "create_account"):
            route = browser_route(f"https://auth.openai.com/api/accounts/{path}")
            await handler(route)
            route.fetch.assert_not_awaited()
            route.continue_.assert_not_awaited()
            route.abort.assert_awaited_once()
        page = SimpleNamespace(evaluate=AsyncMock(return_value="https://auth.openai.com/log-in/password"))
        with pytest.raises(RegisteredEmailDetected):
            await check_fresh_registration_page(page, mailbox)

    asyncio.run(run())


def test_browser_fresh_identity_allows_otp_and_own_recovery():
    from register.page import check_fresh_registration_page, install_fresh_registration_guard

    async def run():
        mailbox = {"address": "fresh@gmail.com", "provider": "smsbower_mail"}
        context = SimpleNamespace(route=AsyncMock())
        await install_fresh_registration_guard(context, mailbox)
        handler = context.route.await_args_list[1].args[1]
        register_route = browser_route(
            "https://auth.openai.com/api/accounts/user/register",
            referer="https://auth.openai.com/create-account/password",
            payload={"continue_url": "https://auth.openai.com/api/accounts/email-otp/send"},
        )
        await handler(register_route)
        assert mailbox["_registration_identity_created"] is True
        otp = browser_route("https://auth.openai.com/api/accounts/email-otp/send")
        await handler(otp)
        otp.continue_.assert_awaited_once()
        otp.abort.assert_not_awaited()
        page = SimpleNamespace(evaluate=AsyncMock(return_value="https://auth.openai.com/log-in/password"))
        await check_fresh_registration_page(page, mailbox)
        assert "_registration_outcome" not in mailbox

    asyncio.run(run())


@pytest.mark.parametrize("payload", UNCONFIRMED_CREATION_RESPONSES)
def test_browser_unconfirmed_create_response_cannot_enable_otp_or_recovery(payload):
    from register.page import check_fresh_registration_page, install_fresh_registration_guard

    async def run():
        mailbox = {"address": "candidate@example.test", "provider": "smsbower_mail"}
        context = SimpleNamespace(route=AsyncMock())
        await install_fresh_registration_guard(context, mailbox)
        handler = context.route.await_args_list[1].args[1]
        creation = browser_route(
            "https://auth.openai.com/api/accounts/user/register",
            referer="https://auth.openai.com/create-account/password", payload=payload,
        )
        await handler(creation)
        creation.fetch.assert_awaited_once()
        creation.continue_.assert_not_awaited()
        assert not mailbox.get("_registration_identity_created")
        assert mailbox.get("_registration_guard_error")
        assert mailbox.get("_registration_outcome") != "already_registered"

        otp = browser_route("https://auth.openai.com/api/accounts/email-otp/send")
        await handler(otp)
        otp.abort.assert_awaited_once()
        otp.fetch.assert_not_awaited()
        otp.continue_.assert_not_awaited()
        page = SimpleNamespace(evaluate=AsyncMock(return_value="https://auth.openai.com/email-verification"))
        with pytest.raises(RuntimeError, match="registration_identity_response_un"):
            await check_fresh_registration_page(page, mailbox)

    asyncio.run(run())


def test_browser_duplicate_response_is_terminal():
    from register.page import install_fresh_registration_guard

    async def run():
        mailbox = {"provider": "smsbower_mail", "address": "duplicate@icloud.com"}
        context = SimpleNamespace(route=AsyncMock())
        await install_fresh_registration_guard(context, mailbox)
        route = browser_route(
            "https://auth.openai.com/api/accounts/user/register", status=409,
            payload={"error": {"code": "user_already_exists"}},
        )
        await context.route.await_args_list[1].args[1](route)
        assert mailbox["_registration_outcome"] == "already_registered"
        assert not mailbox.get("_registration_identity_created")

    asyncio.run(run())


def test_browser_ambiguous_register_timeout_is_not_replayed_or_marked_created():
    from register.page import install_fresh_registration_guard

    async def run():
        mailbox = {"provider": "smsbower_mail", "address": "uncertain@gmail.com"}
        context = SimpleNamespace(route=AsyncMock())
        await install_fresh_registration_guard(context, mailbox)
        route = browser_route("https://auth.openai.com/api/accounts/user/register")
        route.fetch.side_effect = TimeoutError("response timeout")
        await context.route.await_args_list[1].args[1](route)
        route.fetch.assert_awaited_once()
        route.continue_.assert_not_awaited()
        route.abort.assert_awaited_once()
        assert not mailbox.get("_registration_identity_created")
        assert "_registration_outcome" not in mailbox

    asyncio.run(run())


def test_browser_other_providers_do_not_install_or_run_guard():
    from register.page import check_fresh_registration_page, install_fresh_registration_guard

    async def run():
        mailbox = {"provider": "icloud_api"}
        context = SimpleNamespace(route=AsyncMock())
        await install_fresh_registration_guard(context, mailbox)
        context.route.assert_not_awaited()
        page = SimpleNamespace(evaluate=AsyncMock())
        await check_fresh_registration_page(page, mailbox, require_signup=True)
        page.evaluate.assert_not_awaited()

    asyncio.run(run())


def test_browser_password_connection_keeps_the_same_auth_transaction():
    from register.page import password_registration_signin_url

    url = password_registration_signin_url(
        "https://chatgpt.com/api/auth/signin/openai?auth_session_logging_id=fixture&screen_hint=signup",
        "candidate+split@gmail.com",
    )
    query = parse_qs(urlsplit(url).query)
    assert query["auth_session_logging_id"] == ["fixture"]
    assert query["connection"] == ["password"]
    assert query["screen_hint"] == ["login_or_signup"]
    assert query["login_hint"] == ["candidate+split@gmail.com"]

"""Offline regressions for post-MFA workspace-session recovery.

Only synthetic HTTP responses and cookies are used. No login, callback or
workspace-selection request is sent to a live service.
"""
from __future__ import annotations

import base64
from collections import defaultdict, deque
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


AUTH = "https://auth.openai.com"
DUMP = AUTH + "/api/accounts/client_auth_session_dump"
WORKSPACE = AUTH + "/workspace?flow=synthetic-flow"
TEAM_CALLBACK = "https://chatgpt.com/api/auth/callback/openai?code=team-code&state=test-state"
PERSONAL_CALLBACK = "https://chatgpt.com/api/auth/callback/openai?code=personal-code&state=test-state"
PERSONAL = {"id": "ws-personal", "name": "Personal", "is_personal": True}
TEAM = {"id": "ws-team", "name": "Example Team", "is_personal": False}


def _encoded(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def _response(status=200, *, data=None, location="", url="", cookie=None):
    return SimpleNamespace(
        status_code=status,
        headers={"Location": location} if location else {},
        url=url,
        json=Mock(return_value={} if data is None else data),
        text="",
        workspace_cookie=cookie,
    )


@pytest.fixture
def recovery(monkeypatch):
    import register.protocol_registrar as module

    instance = module.PlatformRegistrar.__new__(module.PlatformRegistrar)
    instance.proxy = ""
    instance.clearance_user_agent = ""
    cookies = {}
    instance.session = SimpleNamespace(
        cookies=SimpleNamespace(get=lambda name, **_kwargs: cookies.get(name, "")),
    )
    instance._navigate_headers = Mock(side_effect=lambda referer: {"Referer": referer})
    instance._post_auth_json = Mock(return_value={"continue_url": TEAM_CALLBACK})
    instance._diagnostic = Mock()
    calls = []
    responses = defaultdict(deque)
    logs = Mock()

    def request(session, method, url, **kwargs):
        assert session is instance.session, "recovery must preserve the authenticated session"
        assert method.lower() == "get", "recovery must not replay a credential or selection POST"
        assert kwargs.get("allow_redirects") is False, "automatic redirects could consume a personal callback"
        calls.append((url, kwargs))
        assert len(calls) <= 8, "recovery must have a small, finite request budget"
        response = responses[url].popleft() if responses[url] else _response(url=url)
        if isinstance(response, tuple):
            return response
        if response.workspace_cookie is not None:
            cookies["oai-client-auth-session"] = _encoded({"workspaces": response.workspace_cookie}) + ".signature"
        if not response.url:
            response.url = url
        return response, ""

    monkeypatch.setattr(module, "request_with_local_retry", request)
    monkeypatch.setattr(module, "_headers_with_clearance", lambda headers, *_args: headers)
    monkeypatch.setattr(module, "step", logs)
    monkeypatch.setattr(module, "totp_code", lambda _secret: "000000")

    def queue(url, *items):
        responses[url].extend(items)

    return SimpleNamespace(
        registrar=instance, queue=queue, calls=calls, logs=logs, cookies=cookies,
        urls=lambda: [url for url, _kwargs in calls],
    )


def _select(recovery, referer=WORKSPACE):
    return recovery.registrar._select_workspace(
        1, "", referer, require_team_workspace=True,
    )


def test_non_recovery_dump_preserves_single_get_behavior(recovery):
    recovery.queue(DUMP, _response(302, location="/workspace"))
    assert recovery.registrar._dump_client_auth_session(AUTH + "/log-in/password", 1) == {}
    assert recovery.urls() == [DUMP]
    recovery.registrar._post_auth_json.assert_not_called()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_to_workspace_json_recovers_team_and_posts_once(recovery, status):
    page = AUTH + "/workspace?flow=redirected-flow"
    recovery.queue(DUMP, _response(status, location=page))
    recovery.queue(page, _response(data={"client_auth_session": {"workspaces": [PERSONAL, TEAM]}}))

    assert _select(recovery) == TEAM_CALLBACK
    assert recovery.urls() == [DUMP, page]
    post = recovery.registrar._post_auth_json
    post.assert_called_once()
    assert post.call_args.args[1] == page
    assert post.call_args.args[2] == {"workspace_id": "ws-team"}
    assert post.call_args.args[4] == "workspace_select"


def test_relative_redirect_uses_previous_response_url(recovery):
    first = AUTH + "/flows/workspace/start"
    second = AUTH + "/flows/workspace/next?flow=next-step"
    recovery.queue(DUMP, _response(302, location=first))
    recovery.queue(first, _response(302, location="next?flow=next-step"))
    recovery.queue(second, _response(data={"workspaces": [PERSONAL, TEAM]}))

    assert _select(recovery) == TEAM_CALLBACK
    assert recovery.urls() == [DUMP, first, second]
    assert recovery.registrar._post_auth_json.call_args.args[1] == second


def test_redirect_cookie_workspaces_are_read_before_following_callback(recovery):
    recovery.queue(DUMP, _response(302, location="/workspace"))
    recovery.queue(AUTH + "/workspace", _response(
        302, location=PERSONAL_CALLBACK, cookie=[PERSONAL, TEAM],
    ))

    assert _select(recovery) == TEAM_CALLBACK
    assert recovery.urls() == [DUMP, AUTH + "/workspace"]
    assert PERSONAL_CALLBACK not in recovery.urls()
    assert recovery.registrar._post_auth_json.call_args.args[2] == {"workspace_id": "ws-team"}


def test_empty_dump_loads_actual_workspace_page_cookie(recovery):
    page = AUTH + "/workspace/choose?flow=provided-flow"
    recovery.queue(DUMP, _response())
    recovery.queue(page, _response(cookie=[PERSONAL, TEAM]))

    assert _select(recovery, page) == TEAM_CALLBACK
    assert recovery.urls() == [DUMP, page]
    assert recovery.registrar._post_auth_json.call_args.args[1] == page


def test_empty_workspace_html_resyncs_dump_after_page_get(recovery):
    page = AUTH + "/workspace/choose?flow=provided-flow"
    recovery.queue(DUMP, _response(), _response(data={"workspaces": [PERSONAL, TEAM]}))
    recovery.queue(page, _response())

    assert _select(recovery, page) == TEAM_CALLBACK
    assert recovery.urls() == [DUMP, page, DUMP]
    assert recovery.calls[-1][1]["headers"]["Referer"] == page
    assert recovery.registrar._post_auth_json.call_args.args[1] == page


@pytest.mark.parametrize("target", [
    PERSONAL_CALLBACK,
    "http://localhost:1455/auth/callback?code=personal-code&state=test-state",
    AUTH + "/api/auth/callback/openai?state=test-state",
    AUTH + "/oauth/callback",
    AUTH + "/workspace?code=personal-code&state=test-state",
    AUTH + "/log-in/password",
    AUTH + "/mfa-challenge/test-factor",
    AUTH + "/api/accounts/authorize?client_id=test&response_type=code",
    AUTH + "/authorize",
    AUTH + "/oauth/authorize",
    AUTH + "/api/auth/%63allback/openai",
    "https://foreign.example.test/workspace",
    "https://auth.openai.com.foreign.example.test/workspace",
    "https://auth.openai.com@foreign.example.test/workspace",
    "https://auth.openai.com:8443/workspace",
    "//foreign.example.test/workspace",
    "http://auth.openai.com/workspace",
    "javascript:alert('synthetic')",
    "file:///workspace",
])
def test_recovery_never_requests_callback_reauthentication_or_foreign_target(recovery, target):
    recovery.queue(DUMP, _response(302, location=target))

    with pytest.raises(RuntimeError):
        _select(recovery)
    assert target not in recovery.urls()
    assert all(url in {DUMP, WORKSPACE} for url in recovery.urls())
    assert len(recovery.calls) <= 6
    recovery.registrar._post_auth_json.assert_not_called()


@pytest.mark.parametrize("referer", [
    PERSONAL_CALLBACK,
    AUTH + "/mfa-challenge/test-factor",
    AUTH + "/log-in/password",
    "https://foreign.example.test/workspace",
    AUTH + "/workspace?code=personal-code",
])
def test_missing_workspace_data_does_not_guess_or_request_unsafe_referer(recovery, referer):
    with pytest.raises(RuntimeError):
        _select(recovery, referer)
    assert recovery.urls() == [DUMP]
    recovery.registrar._post_auth_json.assert_not_called()


def test_missing_redirect_location_fails_closed(recovery):
    recovery.queue(DUMP, _response(302))
    with pytest.raises(RuntimeError):
        _select(recovery)
    assert len(recovery.calls) <= 6
    recovery.registrar._post_auth_json.assert_not_called()


def test_redirect_loop_is_bounded_and_never_selects_personal(recovery):
    recovery.queue(DUMP, *[_response(302, location="/workspace") for _ in range(6)])
    recovery.queue(AUTH + "/workspace", *[_response(302, location=DUMP) for _ in range(6)])
    with pytest.raises(RuntimeError):
        _select(recovery)
    assert len(recovery.calls) <= 6
    recovery.registrar._post_auth_json.assert_not_called()


def test_long_redirect_chain_stops_after_six_gets(recovery):
    recovery.queue(DUMP, _response(302, location="/workspace/hop-1"))
    for number in range(1, 10):
        recovery.queue(
            AUTH + f"/workspace/hop-{number}",
            _response(302, location=f"/workspace/hop-{number + 1}"),
        )
    with pytest.raises(RuntimeError):
        _select(recovery)
    assert len(recovery.calls) <= 6
    recovery.registrar._post_auth_json.assert_not_called()


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503])
def test_http_failure_does_not_select_workspace_from_error_body(recovery, status):
    recovery.queue(DUMP, _response(status, data={"workspaces": [PERSONAL, TEAM]}))
    with pytest.raises(RuntimeError):
        _select(recovery)
    assert len(recovery.calls) <= 6
    recovery.registrar._post_auth_json.assert_not_called()


def test_transport_failure_is_sanitized_and_never_selects(recovery):
    sensitive = "session-cookie-SYNTHETIC-SECRET"
    recovery.queue(DUMP, (None, "timeout at " + DUMP + "?state=" + sensitive))
    with pytest.raises(RuntimeError) as raised:
        _select(recovery)
    assert sensitive not in str(raised.value)
    assert sensitive not in repr(recovery.logs.call_args_list)
    assert sensitive not in repr(recovery.registrar._diagnostic.call_args_list)
    recovery.registrar._post_auth_json.assert_not_called()


def test_recovered_personal_only_data_does_not_select_or_consume_callback(recovery):
    recovery.queue(DUMP, _response(302, location="/workspace"))
    recovery.queue(AUTH + "/workspace", _response(data={"workspaces": [PERSONAL]}))
    with pytest.raises(RuntimeError, match="team_workspace_"):
        _select(recovery)
    recovery.registrar._post_auth_json.assert_not_called()
    assert PERSONAL_CALLBACK not in recovery.urls()


def test_redirect_diagnostics_omit_query_cookie_and_oauth_values(recovery):
    secret = "synthetic-sensitive-query-value"
    page = AUTH + "/workspace?state=" + secret + "&password=" + secret
    recovery.queue(DUMP, _response(302, location=page))
    recovery.queue(page, _response(cookie=[PERSONAL, TEAM]))
    assert _select(recovery) == TEAM_CALLBACK
    messages = repr(recovery.logs.call_args_list) + repr(recovery.registrar._diagnostic.call_args_list)
    assert secret not in messages
    assert recovery.cookies["oai-client-auth-session"] not in messages
    assert "302" in messages
    assert "auth.openai.com" in messages


def test_actual_login_recovers_team_before_any_oauth_callback(recovery):
    registrar = recovery.registrar
    trace = []
    registrar._complete_chatgpt_session = Mock(return_value={"final_url": AUTH + "/log-in/password"})
    recovery.queue(DUMP, _response(), _response(302, location="/workspace"))
    recovery.queue(AUTH + "/workspace", _response(data={"workspaces": [PERSONAL, TEAM]}))

    def post(_url, _referer, payload, _index, operation):
        trace.append((operation, dict(payload)))
        if operation == "password_verify":
            return {"continue_url": AUTH + "/mfa-challenge/test-factor", "page": {
                "type": "mfa_challenge", "payload": {
                    "factor_id": "test-factor", "factors": [{
                        "id": "test-factor", "factor_type": "totp", "is_recovery": False,
                    }],
                },
            }}
        if operation == "mfa_issue_challenge":
            return {}
        if operation == "mfa_verify":
            return {"continue_url": PERSONAL_CALLBACK, "page": {"type": "external_url"}}
        if operation == "workspace_select":
            return {"continue_url": TEAM_CALLBACK}
        raise AssertionError(f"Unexpected auth operation: {operation}")

    def navigate(url, _referer, _index):
        trace.append(("navigate", url))
        return SimpleNamespace(url="https://chatgpt.com/")

    claims = {"https://api.openai.com/auth": {
        "chatgpt_plan_type": "team", "chatgpt_account_id": "ws-team", "chatgpt_user_id": "user-test",
    }}
    registrar._post_auth_json = Mock(side_effect=post)
    registrar._navigate_auth_step = Mock(side_effect=navigate)
    registrar._read_chatgpt_session = Mock(return_value={
        "email": "workspace-test@example.test",
        "access_token": f"{_encoded({'alg': 'none'})}.{_encoded(claims)}.test",
        "plan_type": "team", "account_id": "ws-team", "user_id": "user-test",
    })

    result = registrar.login_with_password_totp(
        "workspace-test@example.test", "synthetic-password", "synthetic-totp", 1,
        require_team_workspace=True,
    )
    assert trace.index(("workspace_select", {"workspace_id": "ws-team"})) < trace.index(("navigate", TEAM_CALLBACK))
    assert ("navigate", PERSONAL_CALLBACK) not in trace
    assert PERSONAL_CALLBACK not in recovery.urls()
    assert sum(operation == "workspace_select" for operation, _payload in trace) == 1
    assert result["selected_workspace_id"] == "ws-team"
    assert result["workspace_selection_verified"] is True

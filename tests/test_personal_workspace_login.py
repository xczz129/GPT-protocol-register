"""Strict personal imports verify the terminal session; generic login stays compatible.

Execute the real password/TOTP transaction and workspace chooser with every
HTTP boundary mocked. All identities, cookies and credentials are synthetic.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import register.protocol_registrar as protocol


CALLBACK = "https://chatgpt.com/api/auth/callback/openai?code=synthetic&state=synthetic"
PERSONAL = {"id": "ws-personal", "name": "Personal", "type": "personal"}
TEAM = {"id": "ws-team", "name": "Example", "type": "team"}


def _record(plan="free", workspace="ws-personal", *, account_type="", user="user-a"):
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    auth = {"chatgpt_plan_type": plan, "chatgpt_account_id": workspace, "chatgpt_user_id": user}
    account = {"id": workspace, "planType": plan}
    if account_type:
        account["type"] = account_type
    return {
        "email": "member@example.test", "account_id": workspace, "user_id": user, "plan_type": plan,
        "access_token": f"{encode({'alg': 'none'})}.{encode({'https://api.openai.com/auth': auth})}.synthetic",
        "session": {"account": account, "user": {"id": user, "email": "member@example.test"}},
    }


@pytest.fixture
def login(monkeypatch):
    monkeypatch.setattr(protocol, "step", Mock())
    monkeypatch.setattr(protocol, "totp_code", Mock(return_value="000000"))
    monkeypatch.setattr(protocol, "request_with_local_retry", Mock(side_effect=AssertionError("HTTP forbidden")))

    def make(*, chooser=False, terminal=None, workspaces=None, list_in_mfa=False):
        registrar = protocol.PlatformRegistrar.__new__(protocol.PlatformRegistrar)
        registrar.session = SimpleNamespace(cookies=SimpleNamespace(get=Mock(return_value="")))
        registrar._complete_chatgpt_session = Mock(return_value={"final_url": "https://auth.openai.com/log-in/password"})
        registrar._totp_factor = Mock(return_value=("synthetic-factor", "totp"))
        rows = [PERSONAL, TEAM] if workspaces is None else workspaces
        registrar._dump_client_auth_session = Mock(return_value={} if list_in_mfa else {"workspaces": rows})
        trace = []

        def post(_url, _referer, payload, _index, operation):
            trace.append((operation, deepcopy(payload)))
            if operation == "password_verify":
                return {"continue_url": "https://auth.openai.com/mfa-challenge", "page": {"type": "mfa_challenge"}}
            if operation == "mfa_issue_challenge":
                return {}
            if operation == "mfa_verify":
                return {
                    "continue_url": "https://auth.openai.com/workspace" if chooser else CALLBACK,
                    "page": {"type": "workspace" if chooser else "external_url"},
                    **({"workspaces": rows} if list_in_mfa else {}),
                }
            if operation == "workspace_select":
                return {"continue_url": CALLBACK}
            raise AssertionError(f"Unexpected auth operation: {operation}")

        def navigate(url, _referer, _index):
            trace.append(("navigate", url))
            return SimpleNamespace(url="https://chatgpt.com/")

        registrar._post_auth_json = Mock(side_effect=post)
        registrar._navigate_auth_step = Mock(side_effect=navigate)
        registrar._read_chatgpt_session = Mock(return_value=deepcopy(_record() if terminal is None else terminal))
        return registrar, trace

    return make


def _strict_login(registrar, **kwargs):
    return registrar.login_with_password_totp(
        "member@example.test", "synthetic-password", "synthetic-secret", 1,
        require_personal_workspace=True, **kwargs,
    )


@pytest.mark.parametrize("plan", ["free", "plus", "pro"])
def test_personal_direct_callback_verifies_session_without_inventing_selection(login, plan):
    registrar, trace = login(terminal=_record(plan))
    result = _strict_login(registrar)
    assert result["plan_type"] == plan
    assert result["workspace_selection_mode"] == "personal"
    assert result["user_id"] == "user-a"
    assert not result.get("workspace_selection_verified")
    assert "selected_workspace_id" not in result
    assert not any(operation == "workspace_select" for operation, _ in trace)
    assert sum(operation == "navigate" for operation, _ in trace) == 1


@pytest.mark.parametrize("plan", ["team", "business", "enterprise"])
def test_personal_direct_callback_rejects_actual_team_session(login, plan):
    registrar, trace = login(terminal=_record(plan, "ws-team"))
    with pytest.raises(RuntimeError, match="personal_workspace_team_forbidden"):
        _strict_login(registrar)
    registrar._read_chatgpt_session.assert_called_once()
    assert not any(operation == "workspace_select" for operation, _ in trace)


def test_ambiguous_business_plan_is_not_accepted_as_personal_by_default(login):
    registrar, _ = login(terminal=_record("self_serve_business_prolite"))
    with pytest.raises(RuntimeError, match="personal_workspace_unverified"):
        _strict_login(registrar)


def test_direct_callback_uses_actual_personal_structure_for_ambiguous_plan(login):
    registrar, _ = login(terminal=_record("self_serve_business_prolite", account_type="personal"))
    result = _strict_login(registrar)
    assert result["plan_type"] == "self_serve_business_prolite"
    assert result["workspace_selection_mode"] == "personal"
    assert not result.get("workspace_selection_verified")


@pytest.mark.parametrize("list_in_mfa", [False, True], ids=["session-dump", "mfa-response-only"])
def test_personal_selection_binds_metadata_to_final_session_and_excludes_secrets(login, list_in_mfa):
    selected = {**PERSONAL, "access_token": "must-not-persist", "cookie": "must-not-persist"}
    registrar, trace = login(chooser=True, terminal=_record("self_serve_business_prolite"),
        workspaces=[TEAM, selected], list_in_mfa=list_in_mfa)
    result = _strict_login(registrar)
    assert trace.index(("workspace_select", {"workspace_id": "ws-personal"})) < trace.index(("navigate", CALLBACK))
    assert result["selected_workspace_id"] == "ws-personal"
    assert result["selected_workspace_name"] == "Personal"
    assert result["selected_workspace_metadata"]["type"] == "personal"
    assert "must-not-persist" not in repr(result)
    assert result["selected_workspace_evidence_source"] == "auth_workspace_selection"
    assert result["workspace_selection_verified"] is True
    assert result["workspace_selection_mode"] == "personal"


def test_personal_selection_rejects_callback_for_a_different_workspace(login):
    registrar, _ = login(chooser=True, terminal=_record("free", "ws-other"))
    with pytest.raises(RuntimeError, match="personal_workspace_id_mismatch"):
        _strict_login(registrar)


def test_personal_selection_rejects_team_terminal_even_when_selected_id_matches(login):
    registrar, _ = login(chooser=True, terminal=_record("team", "ws-personal"))
    with pytest.raises(RuntimeError, match="personal_workspace_team_forbidden"):
        _strict_login(registrar)


def test_personal_login_rejects_conflicting_terminal_users(login):
    terminal = _record()
    terminal["session"]["user"]["id"] = "other-user"
    registrar, _ = login(terminal=terminal)
    with pytest.raises(RuntimeError, match="personal_workspace_user_mismatch"):
        _strict_login(registrar)


def test_personal_chooser_with_only_team_stops_before_callback(login):
    registrar, trace = login(chooser=True, workspaces=[TEAM])
    with pytest.raises(RuntimeError, match="personal_workspace_unavailable"):
        _strict_login(registrar)
    assert not any(operation == "workspace_select" for operation, _ in trace)
    registrar._navigate_auth_step.assert_not_called()
    registrar._read_chatgpt_session.assert_not_called()


def test_personal_display_name_is_not_terminal_type_evidence(login):
    registrar, _ = login(chooser=True, terminal=_record("self_serve_business_prolite"),
        workspaces=[{"id": "ws-personal", "name": "Personal"}])
    with pytest.raises(RuntimeError, match="personal_workspace_unverified"):
        _strict_login(registrar)


def test_conflicting_requested_modes_stop_before_authentication(login):
    registrar, _ = login()
    with pytest.raises(RuntimeError, match="workspace_mode_conflict"):
        _strict_login(registrar, require_team_workspace=True)
    registrar._complete_chatgpt_session.assert_not_called()
    registrar._post_auth_json.assert_not_called()


def test_generic_login_default_keeps_shared_team_callback_compatibility(login):
    registrar, trace = login(terminal=_record("team", "ws-team"))
    result = registrar.login_with_password_totp("member@example.test", "synthetic-password", "synthetic-secret", 1)
    assert result["plan_type"] == "team"
    assert "workspace_selection_mode" not in result
    assert not any(operation == "workspace_select" for operation, _ in trace)


def test_explicit_team_mode_continues_to_select_and_validate_team(login):
    registrar, trace = login(terminal=_record("team", "ws-team"))
    result = registrar.login_with_password_totp("member@example.test", "synthetic-password", "synthetic-secret", 1,
        require_team_workspace=True)
    assert ("workspace_select", {"workspace_id": "ws-team"}) in trace
    assert result["workspace_selection_mode"] == "team"
    assert result["workspace_selection_verified"] is True


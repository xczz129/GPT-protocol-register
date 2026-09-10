# -*- coding: utf-8 -*-
"""Team imports must select and retain the real team session at every boundary.

All credentials, JWTs and responses are synthetic. HTTP, health checks and
account persistence are mocked; these regressions never contact live services.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import quote

import pytest

from common.team_workspace import (
    select_team_workspace,
    validate_team_session,
    workspace_entries,
)


PERSONAL = {"id": "ws-personal", "name": "Personal", "is_personal": True}
TEAM = {"id": "ws-team", "name": "Example Team", "is_personal": False}
OTHER_TEAM = {"id": "ws-other", "name": "Other Team", "is_personal": False}
PERSONAL_CALLBACK = "https://chatgpt.com/api/auth/callback/openai?code=personal&state=test"
TEAM_CALLBACK = "https://chatgpt.com/api/auth/callback/openai?code=team&state=test"


def _encoded(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def _record(plan="team", workspace="ws-team", user="user-a"):
    claims = {
        "chatgpt_plan_type": plan,
        "chatgpt_account_id": workspace,
        "chatgpt_user_id": user,
    }
    token = f"{_encoded({'alg': 'none'})}.{_encoded({'https://api.openai.com/auth': claims})}.test"
    return {
        "email": "team-import@example.test",
        "access_token": token,
        "plan_type": plan,
        "account_id": workspace,
        "user_id": user,
        "session": {"account": {"id": workspace, "planType": plan}},
        "selected_workspace_id": workspace,
        "workspace_selection_verified": True,
    }


def test_team_selection_skips_first_personal_workspace():
    assert select_team_workspace([PERSONAL, TEAM])["id"] == TEAM["id"]


@pytest.mark.parametrize("personal", [
    PERSONAL,
    {"id": "p", "name": "个人空间"},
    {"id": "p", "name": "My Account", "type": "personal"},
    {"id": "p", "name": "Team named personal account", "plan_type": "free"},
])
def test_personal_workspace_never_selected_even_when_requested_by_name(personal):
    with pytest.raises(RuntimeError, match="team_workspace_personal_forbidden"):
        select_team_workspace([personal, TEAM], personal["name"])


@pytest.mark.parametrize("items", [[], [PERSONAL]])
def test_team_selection_without_team_fails_closed(items):
    with pytest.raises(RuntimeError, match="team_workspace_"):
        select_team_workspace(items)


def test_multiple_teams_require_name_instead_of_arbitrary_first_match():
    with pytest.raises(RuntimeError, match="team_workspace_ambiguous"):
        select_team_workspace([PERSONAL, TEAM, OTHER_TEAM])
    assert select_team_workspace([PERSONAL, TEAM, OTHER_TEAM], " other team ")["id"] == "ws-other"


def test_same_display_name_on_personal_and_team_selects_only_nonpersonal_match():
    personal = {**PERSONAL, "name": "Example Team"}
    assert select_team_workspace([personal, TEAM], "Example Team")["id"] == "ws-team"


def test_unknown_requested_team_does_not_fall_back_to_another_workspace():
    with pytest.raises(RuntimeError, match="team_workspace_name_not_found"):
        select_team_workspace([PERSONAL, TEAM], "A different team")


@pytest.mark.parametrize("wrapper", [
    lambda rows: {"workspaces": rows},
    lambda rows: {"client_auth_session": {"workspaces": rows}},
    lambda rows: {"page": {"payload": {"workspaces": rows}}},
    lambda rows: {"oai-client-auth-session": _encoded({"workspaces": rows}) + ".signature"},
    lambda rows: quote(_encoded({"workspaces": rows}) + ".signature"),
    lambda rows: f"{_encoded({'alg': 'none'})}.{_encoded({'workspaces': rows})}.signature",
])
def test_auth_workspace_recovery_supports_dump_payloads_and_cookie_formats(wrapper):
    assert workspace_entries(wrapper([PERSONAL, TEAM])) == [PERSONAL, TEAM]


@pytest.fixture
def registrar(monkeypatch):
    import register.protocol_registrar as module

    instance = module.PlatformRegistrar.__new__(module.PlatformRegistrar)
    instance.proxy = ""
    instance.session = SimpleNamespace(cookies=SimpleNamespace(get=Mock(return_value="")))
    instance._dump_client_auth_session = Mock(return_value={})
    instance._post_auth_json = Mock(return_value={"continue_url": TEAM_CALLBACK})
    monkeypatch.setattr(module, "step", Mock())
    monkeypatch.setattr(module, "totp_code", lambda _secret: "000000")
    return instance


def test_structured_response_workspace_list_takes_precedence_over_stale_cookie(registrar):
    registrar.session.cookies.get.return_value = _encoded({"workspaces": [PERSONAL]}) + ".signature"
    assert registrar._auth_session_workspaces({"client_auth_session": {"workspaces": [TEAM]}}) == [TEAM]


def test_fresh_cookie_takes_precedence_over_personal_only_pre_mfa_dump(registrar):
    registrar.session.cookies.get.return_value = _encoded({"workspaces": [PERSONAL, TEAM]}) + ".signature"
    old_dump = {"client_auth_session": {"workspaces": [PERSONAL]}}
    recovered = registrar._auth_session_workspaces({}, old_dump)
    assert recovered == [PERSONAL, TEAM]
    assert select_team_workspace(recovered)["id"] == "ws-team"


def test_team_chooser_recovers_missing_cookie_from_session_dump(registrar):
    registrar._dump_client_auth_session.return_value = {"client_auth_session": {"workspaces": [PERSONAL, TEAM]}}
    callback = registrar._select_workspace(
        1, "", "https://auth.openai.com/workspace", require_team_workspace=True,
    )
    assert callback == TEAM_CALLBACK
    registrar._dump_client_auth_session.assert_called_once()
    assert registrar._post_auth_json.call_args.args[2] == {"workspace_id": "ws-team"}


def test_empty_workspace_dump_never_posts_personal_selection(registrar):
    with pytest.raises(RuntimeError, match="workspace.*without_workspaces"):
        registrar._select_workspace(1, "", "https://auth.openai.com/workspace", require_team_workspace=True)
    registrar._post_auth_json.assert_not_called()


def _prepare_login(registrar, *, chooser=False, terminal=None, workspaces=None):
    """Mock HTTP boundaries while executing the real login and chooser methods."""
    trace = []
    rows = [PERSONAL, TEAM] if workspaces is None else workspaces
    registrar._complete_chatgpt_session = Mock(return_value={"final_url": "https://auth.openai.com/log-in/password"})
    registrar._dump_client_auth_session.return_value = {"client_auth_session": {"workspaces": rows}}
    password_response = {
        "continue_url": "https://auth.openai.com/mfa-challenge/test",
        "page": {"type": "mfa_challenge", "payload": {
            "factor_id": "factor-test",
            "factors": [{"id": "factor-test", "factor_type": "totp", "is_recovery": False}],
        }},
    }
    verified = {
        "continue_url": "https://auth.openai.com/workspace" if chooser else PERSONAL_CALLBACK,
        "page": {"type": "workspace" if chooser else "external_url"},
    }

    def post(_url, _referer, payload, _index, operation):
        trace.append((operation, deepcopy(payload)))
        if operation == "password_verify":
            return password_response
        if operation == "mfa_verify":
            return verified
        if operation == "workspace_select":
            return {"continue_url": TEAM_CALLBACK}
        if operation == "mfa_issue_challenge":
            return {}
        raise AssertionError(f"Unexpected HTTP operation: {operation}")

    def navigate(url, _referer, _index):
        trace.append(("navigate", url))
        return SimpleNamespace(url="https://chatgpt.com/")

    registrar._post_auth_json = Mock(side_effect=post)
    registrar._navigate_auth_step = Mock(side_effect=navigate)
    registrar._read_chatgpt_session = Mock(return_value=deepcopy(terminal if terminal is not None else _record()))
    return trace


@pytest.mark.parametrize("chooser", [False, True], ids=["direct-personal-callback", "workspace-chooser"])
def test_team_login_selects_team_before_consuming_any_callback(registrar, chooser):
    trace = _prepare_login(registrar, chooser=chooser)
    result = registrar.login_with_password_totp(
        "team-import@example.test", "test-password", "test-totp", 1, require_team_workspace=True,
    )
    selection = trace.index(("workspace_select", {"workspace_id": "ws-team"}))
    navigation = trace.index(("navigate", TEAM_CALLBACK))
    assert selection < navigation
    assert ("navigate", PERSONAL_CALLBACK) not in trace
    assert result["selected_workspace_id"] == "ws-team"
    assert result["workspace_selection_verified"] is True
    assert result["user_id"] == "user-a"


def test_team_login_without_team_stops_before_callback_navigation(registrar):
    _prepare_login(registrar, workspaces=[PERSONAL])
    with pytest.raises(RuntimeError, match="team_workspace_"):
        registrar.login_with_password_totp("team-import@example.test", "test-password", "test-totp", 1, require_team_workspace=True)
    registrar._navigate_auth_step.assert_not_called()
    registrar._read_chatgpt_session.assert_not_called()


@pytest.mark.parametrize("terminal", [
    _record("free", "ws-personal"),
    _record("team", "ws-other"),
])
def test_team_login_rejects_terminal_free_or_different_workspace_session(registrar, terminal):
    _prepare_login(registrar, terminal=terminal)
    with pytest.raises(RuntimeError, match="team_workspace_"):
        registrar.login_with_password_totp("team-import@example.test", "test-password", "test-totp", 1, require_team_workspace=True)


def test_personal_login_keeps_existing_direct_callback_behavior(registrar):
    trace = _prepare_login(registrar, terminal=_record("free", "ws-personal"))
    result = registrar.login_with_password_totp("personal@example.test", "test-password", "test-totp", 1)
    assert not any(operation == "workspace_select" for operation, _payload in trace)
    assert ("navigate", PERSONAL_CALLBACK) in trace
    assert result["plan_type"] == "free"


def test_terminal_validation_returns_real_workspace_and_stable_user_identity():
    assert validate_team_session(_record(), selected_workspace_id="ws-team", expected_user_id="user-a") == {
        "plan_type": "team", "account_id": "ws-team", "user_id": "user-a",
    }


def test_team_label_cannot_override_a_free_access_token():
    record = _record("free", "ws-personal")
    record["plan_type"] = "team"
    record["session"]["account"]["planType"] = "team"
    with pytest.raises(RuntimeError, match="team_workspace_personal_forbidden"):
        validate_team_session(record)


def test_plain_team_label_without_session_evidence_is_unverified():
    with pytest.raises(RuntimeError, match="team_workspace_unverified"):
        validate_team_session({"access_token": "opaque-token", "plan_type": "team", "account_id": "ws-team"})


@pytest.mark.parametrize("options", [
    {"selected_workspace_id": "ws-other"},
    {"expected_user_id": "user-other"},
])
def test_terminal_validation_rejects_workspace_or_user_identity_changes(options):
    with pytest.raises(RuntimeError, match="team_workspace_.*mismatch"):
        validate_team_session(_record(), **options)


@pytest.fixture
def import_boundary(monkeypatch, tmp_path):
    import common.account_health as health
    import common.account_store as store
    import web.routes.codex_oauth as route

    database = tmp_path / "must-not-be-created.sqlite3"
    check = Mock(side_effect=lambda record, **_kwargs: {**record, "health_status": "alive"})
    upsert = Mock(side_effect=lambda record, _path, **_kwargs: deepcopy(record))
    monkeypatch.setattr(health, "check_account", check)
    monkeypatch.setattr(store, "upsert_account_record", upsert)
    monkeypatch.setattr(route, "_state", lambda: SimpleNamespace(
        ACCOUNTS_FILE=database,
        normalized_health_status=lambda record: record.get("health_status", "unknown"),
    ))
    monkeypatch.setattr(route, "_log", Mock())
    monkeypatch.setattr(route, "_STOP", threading.Event())
    return SimpleNamespace(route=route, check=check, upsert=upsert, database=database)


def test_team_import_rejects_free_login_before_health_check_or_persistence(import_boundary):
    boundary = import_boundary
    with pytest.raises(RuntimeError, match="team_workspace_"):
        boundary.route._persist_imported_account({"import_account_type": "team"}, _record("free", "ws-personal"), Mock())
    boundary.check.assert_not_called()
    boundary.upsert.assert_not_called()
    assert not boundary.database.exists()


@pytest.mark.parametrize("failure", ["free-label", "free-token", "other-workspace", "other-user"])
def test_post_health_team_guard_prevents_upsert_and_preserves_prior_record(import_boundary, failure):
    boundary = import_boundary
    existing = {**_record(), "import_account_type": "team", "refresh_token": "old-refresh"}
    before = deepcopy(existing)
    checked = {**_record(), "health_status": "alive"}
    if failure == "free-label":
        checked["plan_type"] = "free"
    elif failure == "free-token":
        checked.update(_record("free", "ws-personal"))
    elif failure == "other-workspace":
        checked.update(_record("team", "ws-other"))
    else:
        checked.update(_record("team", user="user-other"))
    boundary.check.return_value = checked
    boundary.check.side_effect = None
    with pytest.raises(RuntimeError, match="team_workspace_"):
        boundary.route._persist_imported_account(existing, _record(), Mock())
    assert existing == before
    boundary.check.assert_called_once()
    assert boundary.check.call_args.kwargs["persist"] is False
    boundary.upsert.assert_not_called()
    assert not boundary.database.exists()


def test_team_import_requires_selection_proof_even_for_team_token(import_boundary):
    tokens = _record()
    tokens.pop("workspace_selection_verified")
    with pytest.raises(RuntimeError, match="team_workspace_unverified"):
        import_boundary.route._persist_imported_account({"import_account_type": "team"}, tokens, Mock())
    import_boundary.check.assert_not_called()
    import_boundary.upsert.assert_not_called()


def test_valid_team_import_can_persist_after_health_verification(import_boundary):
    result = import_boundary.route._persist_imported_account({"import_account_type": "team"}, _record(), Mock())
    assert result["plan_type"] == "team"
    import_boundary.upsert.assert_called_once()
    assert import_boundary.upsert.call_args.kwargs["create"] is True


def test_personal_free_import_can_still_persist(import_boundary):
    result = import_boundary.route._persist_imported_account({}, _record("free", "ws-personal"), Mock())
    assert result["plan_type"] == "free"
    import_boundary.upsert.assert_called_once()


@pytest.mark.parametrize("account_type, strict", [("team", True), ("personal", False)])
def test_import_type_reaches_protocol_login_and_persistence_marker(import_boundary, monkeypatch, account_type, strict):
    import register.protocol as protocol

    login = Mock(return_value=_record() if strict else _record("free", "ws-personal"))
    persist = Mock(side_effect=lambda _account, tokens, _log: tokens)
    monkeypatch.setattr(protocol, "login_with_password_totp_one", login)
    monkeypatch.setattr(import_boundary.route, "_persist_imported_account", persist)
    result = import_boundary.route._import_one(1, "team-import@example.test", "test-password", "test-totp", account_type=account_type)
    assert result["status"] == "success"
    assert login.call_args.kwargs["require_team_workspace"] is strict
    assert (persist.call_args.args[0].get("import_account_type") == "team") is strict

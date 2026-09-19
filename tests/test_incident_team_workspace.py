"""Incident regressions with synthetic credentials and fully mocked HTTP."""
from __future__ import annotations

import base64
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from common.team_workspace import (
    selected_workspace_metadata,
    validate_session_replacement,
    validate_team_session,
    workspace_kind,
)


def _record(plan="self_serve_business_usage_based", workspace="ws-team", user="user-a"):
    payload = {"https://api.openai.com/auth": {
        "chatgpt_plan_type": plan, "chatgpt_account_id": workspace, "chatgpt_user_id": user,
    }}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return {
        "access_token": f"synthetic.{encoded}.signature",
        "session": {"account": {"id": workspace, "planType": plan}, "user": {"id": user}},
        "plan_type": plan, "user_id": user,
    }


def _verified(plan="self_serve_business_usage_based"):
    return {
        **_record(plan), "selected_workspace_id": "ws-team", "workspace_selection_verified": True,
        "selected_workspace_evidence_source": "auth_workspace_selection",
        "selected_workspace_metadata": {"id": "ws-team", "structure": "organization"},
    }


@pytest.mark.parametrize("plan", ["self_serve_business_usage_based", "self_serve_business_prolite"])
def test_business_plan_requires_matching_workspace_type_evidence(plan):
    assert validate_team_session(_verified(plan))["plan_type"] == plan
    with pytest.raises(RuntimeError, match="team_workspace_unverified"):
        validate_team_session(_record(plan), selected_workspace_id="ws-team")


@pytest.mark.parametrize("item,expected", [
    ({"name": "Marketing Team", "plan_type": "self_serve_business_prolite"}, "unknown"),
    ({"plan_type": "prolite", "is_personal": False}, "unknown"),
    ({"structure": "organization"}, "team"),
    ({"type": "team", "is_personal": True}, "personal"),
    ({"type": "team", "account": {"planType": "free"}}, "personal"),
])
def test_workspace_kind_does_not_infer_membership_from_business_name(item, expected):
    assert workspace_kind(item) == expected


def test_bare_prolite_and_named_workspaces_do_not_prove_team_membership():
    with pytest.raises(RuntimeError, match="team_workspace_unverified"):
        validate_team_session(
            _record("prolite"), selected_workspace_id="ws-team",
            selected_workspace_metadata={"id": "ws-team", "name": "Enterprise Team", "is_personal": False},
        )


@pytest.mark.parametrize("mutation,error", [
    (lambda r: r["session"]["account"].update(id="ws-other"), "team_workspace_id_mismatch"),
    (lambda r: r["session"]["user"].update(id="user-other"), "team_workspace_user_mismatch"),
    (lambda r: r["selected_workspace_metadata"].update(id="ws-other"), "team_workspace_id_mismatch"),
    (lambda r: r["selected_workspace_metadata"].update(type="personal"), "team_workspace_personal_forbidden"),
    (lambda r: r["session"]["account"].update(type="personal"), "team_workspace_personal_forbidden"),
    (lambda r: r.update(plan_type="free"), "team_workspace_plan_mismatch"),
])
def test_conflicting_evidence_never_overrides_current_identity(mutation, error):
    record = _verified()
    mutation(record)
    with pytest.raises(RuntimeError, match=error):
        validate_team_session(record)


@pytest.mark.parametrize("field,value", [
    ("workspace_selection_verified", False), ("selected_workspace_evidence_source", "user_input"),
])
def test_unverified_saved_workspace_type_is_not_accepted(field, value):
    record = _verified()
    record[field] = value
    with pytest.raises(RuntimeError, match="team_workspace_unverified"):
        validate_team_session(record)


def test_legacy_record_user_id_cannot_replace_missing_current_user_evidence():
    record = _record(user="")
    record["user_id"] = "old-user"
    with pytest.raises(RuntimeError, match="team_workspace_unverified"):
        validate_team_session(record)


def test_refresh_reuses_verified_type_only_for_same_user_and_workspace():
    previous = _verified()
    original = deepcopy(previous)
    assert validate_session_replacement(previous, _record())["account_id"] == "ws-team"
    for candidate in (_record(workspace="ws-other"), _record(user="user-other"), _record(plan="free")):
        with pytest.raises(RuntimeError, match="team_workspace_|session_identity_"):
            validate_session_replacement(previous, candidate)
    assert previous == original


def test_legacy_business_team_record_still_pins_actual_old_workspace():
    previous = {**_record(), "import_account_type": "team"}
    candidate = _record(workspace="ws-other")
    candidate["session"]["account"]["type"] = "team"
    with pytest.raises(RuntimeError, match="team_workspace_id_mismatch"):
        validate_session_replacement(previous, candidate)


def test_first_verified_team_session_may_replace_legacy_personal_credentials():
    previous = {**_record(plan="free", workspace="ws-personal"), "import_account_type": "team"}
    assert validate_session_replacement(previous, _record(plan="team"))["account_id"] == "ws-team"
    with pytest.raises(RuntimeError, match="session_identity_user_mismatch"):
        validate_session_replacement(previous, _record(plan="team", user="other-user"))
    previous.update(selected_workspace_id="previously-verified-team", workspace_selection_verified=True)
    with pytest.raises(RuntimeError, match="team_workspace_id_mismatch"):
        validate_session_replacement(previous, _record(plan="team"))


def test_metadata_filter_keeps_type_evidence_without_auth_secrets():
    metadata = selected_workspace_metadata({
        "id": "ws-team", "name": "Secret display name", "access_token": "secret-token",
        "structure": "organization", "account": {"id": "ws-team", "type": "team", "cookie": "secret-cookie"},
    })
    assert metadata == {"id": "ws-team", "structure": "organization", "account": {"id": "ws-team", "type": "team"}}


@pytest.mark.parametrize("plan", ["self_serve_business_usage_based", "self_serve_business_prolite"])
def test_mocked_login_passes_selected_type_to_final_session_validation(monkeypatch, plan):
    import register.protocol_registrar as module

    registrar = module.PlatformRegistrar.__new__(module.PlatformRegistrar)
    callback = "https://chatgpt.com/api/auth/callback/openai?code=synthetic&state=synthetic"
    registrar.session = SimpleNamespace(cookies=SimpleNamespace(get=Mock(return_value="")))
    registrar._complete_chatgpt_session = Mock(return_value={"final_url": "https://auth.openai.com/log-in/password"})
    registrar._dump_client_auth_session = Mock(return_value={"workspaces": [
        {"id": "ws-personal", "type": "personal"},
        {"id": "ws-team", "name": "Marketing", "structure": "organization", "token": "must-not-persist"},
    ]})
    registrar._totp_factor = Mock(return_value=("synthetic-factor", "totp"))
    registrar._post_auth_json = Mock(side_effect=[
        {"continue_url": "https://auth.openai.com/mfa-challenge", "page": {"type": "mfa_challenge"}},
        {}, {"continue_url": callback}, {"continue_url": callback},
    ])
    registrar._navigate_auth_step = Mock(return_value=SimpleNamespace(url="https://chatgpt.com/"))
    registrar._read_chatgpt_session = Mock(return_value=_record(plan))
    monkeypatch.setattr(module, "step", Mock())
    monkeypatch.setattr(module, "totp_code", Mock(return_value="000000"))

    result = registrar.login_with_password_totp("member@example.test", "synthetic", "synthetic", 1, require_team_workspace=True)

    assert result["selected_workspace_metadata"] == {"id": "ws-team", "structure": "organization"}
    assert result["selected_workspace_evidence_source"] == "auth_workspace_selection"
    assert result["workspace_selection_verified"] is True
    assert validate_team_session(result)["plan_type"] == plan


def _response(status=403, *, payload=None, text="<html>Forbidden OAuth=DO_NOT_LOG</html>"):
    response = SimpleNamespace(
        status_code=status, url="https://chatgpt.com/api/auth/csrf?code=SECRET_CODE#SECRET_FRAGMENT",
        headers={"content-type": "text/html", "cf-ray": "9c46e92173621717-HKG", "x-request-id": "req_0123456789abcdef0123456789abcdef", "set-cookie": "SECRET_COOKIE"},
        text=text,
    )
    response.json = Mock(side_effect=ValueError("not JSON")) if payload is None else Mock(return_value=payload)
    return response


def test_csrf_diagnostic_has_safe_metadata_but_no_credentials():
    from register.protocol_registrar import _csrf_failure_message

    result = _csrf_failure_message(_response(), "curl: (28) timeout https://user:SECRET_PASSWORD@proxy/?token=SECRET_TOKEN")
    assert result.startswith("chatgpt_csrf_http_403:")
    assert all(value in result for value in ("/api/auth/csrf", "text/html", "9c46e92173621717-HKG", "html_access_denied", "timeout", "req_0123456789abcdef0123456789abcdef"))
    assert not any(secret in result for secret in ("SECRET_", "DO_NOT_LOG", "set-cookie", "https://", "user:"))


def test_csrf_json_summary_drops_echoed_token_and_unsafe_header_values():
    from register.protocol_registrar import _csrf_failure_message

    response = _response(payload={"error": {"code": "access_denied", "message": "SECRET_MESSAGE"}, "csrfToken": "SECRET_TOKEN"})
    response.headers.update({"content-type": "application/json", "cf-ray": "SECRET_RAY", "x-request-id": "SECRET_REQUEST"})
    result = _csrf_failure_message(response)
    assert '"api_error_code":"access_denied"' in result
    assert '"body_summary":"json_object"' in result
    assert "SECRET" not in result


def test_final_csrf_failure_includes_metadata_and_stops_before_signin(monkeypatch):
    import register.protocol_registrar as module

    registrar = module.PlatformRegistrar.__new__(module.PlatformRegistrar)
    registrar.fingerprint = {}
    registrar.proxy = ""
    registrar.clearance_user_agent = ""
    registrar.session = Mock()
    monkeypatch.setattr(module, "step", Mock())
    monkeypatch.setattr(module, "_browser_fingerprint", Mock(return_value={"accept_language": "en", "user_agent": "synthetic"}))
    monkeypatch.setattr(module, "_client_hint_headers", Mock(return_value={}))
    monkeypatch.setattr(module, "_headers_with_clearance", lambda headers, *_args: headers)
    requests = Mock(side_effect=[(_response(status=200), ""), (_response(status=200), ""), (_response(), "")])
    monkeypatch.setattr(module, "request_with_local_retry", requests)

    with pytest.raises(RuntimeError, match="chatgpt_csrf_http_403.*html_access_denied"):
        registrar._complete_chatgpt_session("member@example.test", "synthetic", 1)
    assert requests.call_count == 3
    assert all(call.args[1] == "get" for call in requests.call_args_list)

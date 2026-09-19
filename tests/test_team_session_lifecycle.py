"""Credential lifecycle regressions; every network and persistence boundary is mocked."""
from __future__ import annotations

import base64
import asyncio
from copy import deepcopy
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from common.team_workspace import (
    requires_team_workspace,
    session_identity,
    validate_session_replacement,
)


def _jwt(plan="team", workspace="ws-team", user="user-a", email="member@example.test", *, exp=None):
    def enc(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    auth = {"chatgpt_plan_type": plan, "chatgpt_account_id": workspace}
    if user:
        auth["chatgpt_user_id"] = user
    payload = {"https://api.openai.com/auth": auth, "exp": exp or int(time.time()) + 86400}
    if email:
        payload["https://api.openai.com/profile"] = {"email": email}
    return f"{enc({'alg': 'none'})}.{enc(payload)}.synthetic"


def _tokens(plan="team", workspace="ws-team", user="user-a", email="member@example.test"):
    result = {
        "access_token": _jwt(plan, workspace, user, email, exp=int(time.time()) + 172800),
        "session_token": "new-synthetic-session",
        "plan_type": plan,
        "account_id": workspace,
        "email": email,
        "session": {"account": {"id": workspace, "planType": plan}},
    }
    if plan == "team":
        result.update(selected_workspace_id=workspace, selected_workspace_name="Example Team", workspace_selection_verified=True)
    return result


def _account(*, team=True):
    plan, workspace = ("team", "ws-team") if team else ("free", "ws-personal")
    result = {
        **_tokens(plan, workspace),
        # The stored member key intentionally differs from the JWT workspace.
        "account_id": "legacy-member-id",
        "user_id": "user-a",
        "access_token": _jwt(plan, workspace),
        "session_token": "old-synthetic-session",
        "refresh_token": "old-synthetic-refresh",
        "password": "synthetic-password",
        "totp_secret": "synthetic-totp",
        "health_status": "alive",
    }
    if team:
        result.update(import_account_type="team", workspace_name="")
    return result


@pytest.fixture(autouse=True)
def _no_http(monkeypatch):
    import requests
    monkeypatch.setattr(requests.sessions.Session, "request", Mock(side_effect=AssertionError("Live HTTP is forbidden in lifecycle tests")))


@pytest.mark.parametrize("marker", ["plan", "import", "token", "verified"])
def test_team_requirement_survives_missing_or_stale_display_metadata(marker):
    record = {}
    if marker == "plan":
        record["plan_type"] = "business"
    elif marker == "import":
        record.update(import_account_type="team", plan_type="free")
    elif marker == "token":
        record.update(access_token=_jwt(), plan_type="free")
    else:
        record.update(selected_workspace_id="ws-team", workspace_selection_verified=True)
    assert requires_team_workspace(record)
    assert not requires_team_workspace(_account(team=False))


def test_workspace_identity_comes_from_token_not_legacy_member_key():
    record = _account()
    assert record["account_id"] == "legacy-member-id"
    assert session_identity(record)["account_id"] == "ws-team"
    assert validate_session_replacement(record, _tokens())["account_id"] == "ws-team"


def test_team_workspace_falls_back_to_old_jwt_when_selection_metadata_is_missing():
    previous = _account()
    previous.pop("selected_workspace_id")
    previous.pop("workspace_selection_verified")
    with pytest.raises(RuntimeError, match="team_workspace_id_mismatch"):
        validate_session_replacement(previous, _tokens(workspace="different-team"))


@pytest.mark.parametrize("candidate", [
    _tokens("free", "ws-personal"),
    _tokens(workspace="other-team"),
    _tokens(user="user-b"),
    _tokens(email="different@example.test"),
])
def test_replacement_rejects_free_workspace_user_or_email_changes(candidate):
    with pytest.raises(RuntimeError, match="(team_workspace_|session_identity_)"):
        validate_session_replacement(_account(), candidate, require_identity=True)


def test_new_token_missing_user_cannot_borrow_previous_top_level_user_id():
    candidate = _tokens(user="")
    candidate["user_id"] = "user-a"
    with pytest.raises(RuntimeError, match="session_identity_user_mismatch"):
        validate_session_replacement(_account(), candidate)


def test_stored_user_identity_can_recover_from_an_already_mismatched_old_token():
    previous = _account()
    previous["access_token"] = _jwt(user="wrong-old-user")
    assert validate_session_replacement(previous, _tokens())["user_id"] == "user-a"


def test_current_record_email_wins_over_stale_old_token_after_rebind():
    previous = _account()
    previous["access_token"] = _jwt(email="previous-address@example.test")
    assert validate_session_replacement(previous, _tokens())["email"] == "member@example.test"


def test_existing_normalized_user_id_matches_mixed_case_jwt_identity():
    assert validate_session_replacement(_account(), _tokens(user="USER-A"), require_identity=True)["user_id"] == "USER-A"


@pytest.fixture
def reauth_boundary(monkeypatch, tmp_path):
    import common.account_health as health
    import common.account_store as store
    import register.protocol as protocol
    import web.routes.security_ops as sec

    check = Mock(side_effect=lambda record, **_kwargs: {**record, "health_status": "alive"})
    save = Mock(side_effect=lambda record, *_args, **_kwargs: deepcopy(record))
    login = Mock(return_value=_tokens())
    failure = Mock(side_effect=lambda record, *_args, **_kwargs: record)
    state = SimpleNamespace(
        _ACCOUNT_REAUTH_MODES={"protocol", "browser"},
        normalized_health_status=lambda record: record.get("health_status", "unknown"),
        JOB=SimpleNamespace(append_log=Mock()),
        record_operation_event=Mock(),
        _read_config=lambda: {"mail": {}, "registration": {}, "proxy": {}},
        _mailbox_provider_map=lambda _cfg: {},
        _now_iso=lambda: "2026-09-10T00:00:00Z",
        ACCOUNTS_FILE=tmp_path / "must-not-be-created.sqlite3",
        _persist_reauthenticated_account=sec._persist_reauthenticated_account,
    )
    monkeypatch.setattr(sec, "_state", lambda: state)
    monkeypatch.setattr(sec, "_persist_reauthentication_failure", failure)
    monkeypatch.setattr(health, "check_account", check)
    monkeypatch.setattr(store, "upsert_account_record", save)
    monkeypatch.setattr(protocol, "login_with_password_totp_one", login)
    return SimpleNamespace(module=sec, check=check, save=save, login=login, failure=failure, state=state)


@pytest.mark.parametrize("requested_mode", ["protocol", "browser"])
def test_team_reauth_uses_protocol_and_inherits_verified_workspace_name(reauth_boundary, requested_mode):
    boundary = reauth_boundary
    result = boundary.module._reauthenticate_account_sync(_account(), mode=requested_mode)
    assert boundary.login.call_args.kwargs["require_team_workspace"] is True
    assert boundary.login.call_args.kwargs["workspace_name"] == "Example Team"
    assert result["selected_workspace_id"] == "ws-team"
    assert result["user_id"] == "user-a"
    assert result["account_id"] == "legacy-member-id"
    boundary.save.assert_called_once()


def test_personal_reauth_keeps_default_login_behavior(reauth_boundary):
    boundary = reauth_boundary
    boundary.login.return_value = _tokens("free", "ws-personal")
    result = boundary.module._reauthenticate_account_sync(_account(team=False))
    assert not boundary.login.call_args.kwargs.get("require_team_workspace", False)
    assert result["plan_type"] == "free"
    boundary.save.assert_called_once()


@pytest.mark.parametrize("missing", ["password", "totp_secret"])
def test_team_browser_request_with_missing_credentials_stops_before_any_login(reauth_boundary, missing):
    previous = _account()
    previous.pop(missing)
    with pytest.raises(RuntimeError, match="TOTP"):
        reauth_boundary.module._reauthenticate_account_sync(previous, mode="browser")
    reauth_boundary.login.assert_not_called()
    reauth_boundary.save.assert_not_called()


def test_team_browser_batch_preflight_rejects_missing_totp_before_starting_job(reauth_boundary, monkeypatch):
    from fastapi import HTTPException
    import web.routes.accounts_reauth as route

    boundary = reauth_boundary
    previous = _account()
    previous.pop("totp_secret")
    state = boundary.state
    state._normalize_reauth_mode = boundary.module._normalize_reauth_mode
    state._load_accounts = lambda: [previous]
    state._account_key = lambda _record: "member-key"
    state.account_public_view = lambda _record: {"is_deactivated": False}
    state._account_requires_protocol_reauth = boundary.module._account_requires_protocol_reauth
    state._protocol_reauth_credentials_ready = boundary.module._protocol_reauth_credentials_ready
    state._account_email_relogin_ready = boundary.module._account_email_relogin_ready
    state.ACCOUNT_REAUTH_JOBS = SimpleNamespace(start=Mock())
    monkeypatch.setattr(route, "_state", lambda: state)
    with pytest.raises(HTTPException) as failure:
        asyncio.run(route.account_reauthenticate_start(route.AccountReauthBatchRequest(keys=["member-key"], mode="browser")))
    assert failure.value.status_code == 409
    assert "TOTP" in failure.value.detail
    state.ACCOUNT_REAUTH_JOBS.start.assert_not_called()


@pytest.mark.parametrize("candidate", [
    _tokens("free", "ws-personal"),
    _tokens(workspace="other-team"),
    _tokens(user="user-b"),
])
def test_reauth_rejects_bad_login_before_health_or_upsert(reauth_boundary, candidate):
    boundary = reauth_boundary
    previous = _account()
    before = deepcopy(previous)
    with pytest.raises(RuntimeError, match="(team_workspace_|session_identity_)"):
        boundary.module._persist_reauthenticated_account(previous, candidate)
    assert previous == before
    boundary.check.assert_not_called()
    boundary.save.assert_not_called()


@pytest.mark.parametrize("failure", ["plan", "workspace", "user"])
def test_post_health_reauth_validation_prevents_overwriting_prior_credentials(reauth_boundary, failure):
    boundary = reauth_boundary
    checked = {**_account(), **_tokens(), "health_status": "alive"}
    if failure == "plan":
        checked["plan_type"] = "free"
    elif failure == "workspace":
        checked["access_token"] = _jwt(workspace="other-team")
    else:
        checked["access_token"] = _jwt(user="user-b")
    boundary.check.side_effect = None
    boundary.check.return_value = checked
    with pytest.raises(RuntimeError, match="(team_workspace_|session_identity_)"):
        boundary.module._persist_reauthenticated_account(_account(), _tokens())
    boundary.save.assert_not_called()
    assert not boundary.state.ACCOUNTS_FILE.exists()


def test_personal_reauth_also_rejects_other_users_token(reauth_boundary):
    with pytest.raises(RuntimeError, match="session_identity_user_mismatch"):
        reauth_boundary.module._persist_reauthenticated_account(
            _account(team=False), _tokens("free", "ws-personal", user="user-b"),
        )
    reauth_boundary.save.assert_not_called()


def _run_refresh(monkeypatch, previous, response):
    from common import session_refresh as sr

    refresh = Mock(return_value=response)
    updates = Mock(return_value=(1, []))
    monkeypatch.setattr(sr, "refresh_session_via_service", refresh)
    monkeypatch.setattr(sr, "update_account_record_fields", updates)
    jobs = sr.SessionRefreshJobs()
    job = jobs.start([previous], concurrency=1)
    assert jobs.wait(job["job_id"], 3), "mock refresh worker did not finish"
    snapshot = jobs.snapshot(job["job_id"])
    fields = next(iter(updates.call_args.args[0].values()))
    return snapshot, fields


@pytest.mark.parametrize("candidate", [
    _tokens("free", "ws-personal"),
    _tokens(workspace="other-team"),
    _tokens(user="user-b"),
    _tokens(email="different@example.test"),
    _tokens(user=""),
])
def test_failed_refresh_records_error_without_replacing_original_credentials(monkeypatch, candidate):
    previous = _account()
    before = deepcopy(previous)
    snapshot, fields = _run_refresh(monkeypatch, previous, candidate)
    assert snapshot["failed"] == 1
    assert snapshot["items"][0]["status"] == "failed"
    assert fields["last_token_refresh_error"]
    assert not {"access_token", "session_token", "refresh_token", "plan_type", "health_status", "account_id", "workspace_account_id"} & fields.keys()
    assert previous == before


def test_valid_team_refresh_retains_member_key_and_tracks_real_workspace(monkeypatch):
    response = _tokens()
    response["account_id"] = "incorrect-flat-metadata"
    snapshot, fields = _run_refresh(monkeypatch, _account(), response)
    assert snapshot["refreshed"] == 1
    assert fields["plan_type"] == "team"
    assert fields["workspace_account_id"] == "ws-team"
    assert "account_id" not in fields
    assert fields["user_id"] == "user-a"
    assert fields["session"] == response["session"]


def test_personal_refresh_can_change_personal_plan_without_changing_user(monkeypatch):
    snapshot, fields = _run_refresh(monkeypatch, _account(team=False), _tokens("plus", "ws-personal"))
    assert snapshot["refreshed"] == 1
    assert fields["plan_type"] == "plus"


def test_personal_refresh_rejects_other_user(monkeypatch):
    snapshot, fields = _run_refresh(monkeypatch, _account(team=False), _tokens("free", "ws-personal", user="user-b"))
    assert snapshot["failed"] == 1
    assert "access_token" not in fields


def test_legacy_personal_refresh_keeps_compatibility_without_identity_claims(monkeypatch):
    previous = {"email": "legacy@example.test", "account_id": "member-legacy", "plan_type": "free", "access_token": "old-opaque", "session_token": "old-session"}
    response = {"access_token": "new-opaque", "session_token": "new-session", "plan_type": "free", "account_id": "", "email": ""}
    snapshot, fields = _run_refresh(monkeypatch, previous, response)
    assert snapshot["failed"] == 0
    assert fields["access_token"] == "new-opaque"


def test_refresh_response_preserves_current_session_identity_evidence(monkeypatch):
    from common import session_refresh as sr
    session = {"accessToken": "synthetic-access", "user": {"id": "user-a", "email": "member@example.test"}, "account": {"id": "ws-team", "planType": "team"}}
    post = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {"ok": True, "session": session}))
    monkeypatch.setattr(sr.requests, "post", post)
    result = sr.refresh_session_via_service("old-synthetic-session")
    assert session_identity(result)["user_id"] == "user-a"
    assert result["session"] == session

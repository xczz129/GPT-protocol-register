"""Offline probes with synthetic credentials and a real temporary SQLite store."""
from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from common import account_health as health
from common import account_store as store


def token(user="user-a", workspace="ws-team", plan="team", *, expires=None):
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    claims = {
        "exp": int(time.time() + 7200) if expires is None else expires,
        "https://api.openai.com/auth": {
            "chatgpt_user_id": user,
            "chatgpt_account_id": workspace,
            "chatgpt_plan_type": plan,
        },
    }
    return f"{encode({'alg': 'none'})}.{encode(claims)}.synthetic"


def account(*, expired=False, **extra):
    return {
        "email": "a@example.test", "user_id": "user-a", "account_id": "ws-team",
        "plan_type": "team", "selected_workspace_id": "ws-team",
        "import_account_type": "team", "workspace_selection_verified": True,
        "password": "synthetic-old-password", "group_id": "group-old",
        "access_token": token(expires=1 if expired else None),
        "session_token": "synthetic-old-session", "health_status": "unknown",
        **extra,
    }


def metadata(**extra):
    return {
        "email": "a@example.test", "user_id": "user-a", "account_id": "ws-team",
        "plan_type": "team", "health_status": "alive", "status": "正常",
        "health_checked_at": "2099-01-01T00:00:00Z", "health_error": "",
        **extra,
    }


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "accounts.json"
    monkeypatch.setattr(store, "ACCOUNTS_FILE", path)
    monkeypatch.setattr(store, "ACCOUNTS_DB_FILE", tmp_path / "accounts.sqlite3")
    monkeypatch.setattr(health, "record_operation_event", Mock())
    client = SimpleNamespace(
        cookies=SimpleNamespace(set=Mock()), close=Mock(),
        post=Mock(side_effect=AssertionError("unexpected OAuth request")),
    )
    monkeypatch.setattr(health, "_session", Mock(return_value=client))
    monkeypatch.setattr(health, "_protocol_session_request", Mock(side_effect=AssertionError("unexpected session request")))
    monkeypatch.setattr(health, "fetch_remote_account_info", Mock(return_value=metadata()))
    return path


@pytest.mark.parametrize("change", [
    {"workspace": "ws-personal", "plan": "free"},
    {"workspace": "ws-other", "plan": "team"},
    {"user": "user-other"},
    {"email": "other@example.test"},
])
def test_nextauth_refresh_rejects_identity_or_workspace_change_before_writing(db, monkeypatch, change):
    original = store.upsert_account_record(account(expired=True), db)
    user = change.get("user", "user-a")
    workspace = change.get("workspace", "ws-team")
    plan = change.get("plan", "team")
    response = SimpleNamespace(status_code=200, json=lambda: {
        "accessToken": token(user, workspace, plan),
        "account": {"id": workspace, "planType": plan},
        "user": {"id": user, "email": change.get("email", "a@example.test")},
    })
    monkeypatch.setattr(health, "_protocol_session_request", Mock(return_value=response))

    checked = health.check_account(original, path=db)

    saved = store.load_accounts(db)[0]
    assert saved["access_token"] == original["access_token"]
    assert saved["session_token"] == original["session_token"]
    assert saved["plan_type"] == "team"
    assert saved["account_id"] == "ws-team"
    assert checked["health_status"] == "error"
    health.fetch_remote_account_info.assert_not_called()


@pytest.mark.parametrize("change", [
    {"email": "other@example.test"},
    {"user_id": "user-other"},
    {"account_id": "ws-other"},
    {"plan_type": "free"},
])
def test_probe_metadata_cannot_override_original_identity_or_team(db, monkeypatch, change):
    original = store.upsert_account_record(account(), db)
    monkeypatch.setattr(health, "fetch_remote_account_info", Mock(return_value=metadata(**change)))

    checked = health.check_account(original, path=db)

    saved = store.load_accounts(db)[0]
    assert (saved["user_id"], saved["email"], saved["account_id"], saved["plan_type"]) == (
        "user-a", "a@example.test", "ws-team", "team",
    )
    assert saved["access_token"] == original["access_token"]
    assert checked["health_status"] == "error"


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("change", ["metadata", "credentials", "deleted"])
def test_inflight_health_results_preserve_concurrent_changes_and_callback_state(db, monkeypatch, batch, change):
    original = store.upsert_account_record(account(), db)
    key = store.account_delete_key(original)
    current_token = token(expires=int(time.time() + 12000))

    def probe(_record):
        if change == "deleted":
            store.delete_account_records([key], path=db, include_records=False)
        else:
            fields = {"password": "synthetic-new-password", "group_id": "group-new"}
            if change == "credentials":
                fields.update(access_token=current_token, session_token="synthetic-new-session")
            store.update_account_record_fields({key: fields}, path=db, include_records=False)
        return metadata()

    monkeypatch.setattr(health, "fetch_remote_account_info", Mock(side_effect=probe))
    callbacks = []
    if batch:
        result = health.check_accounts(
            [original], path=db,
            item_callback=lambda _done, _total, row: callbacks.append(row),
            progress_callback=lambda _done, _total, row: callbacks.append(row),
        )["items"][0]
        assert len(callbacks) == 2
    else:
        result = health.check_account(original, path=db)
        callbacks = [result]
    rows = store.load_accounts(db)
    if change == "deleted":
        assert rows == []
        assert all(row["health_write_skipped"] and "access_token" not in row for row in callbacks)
    else:
        assert rows[0]["password"] == "synthetic-new-password"
        assert rows[0]["group_id"] == "group-new"
        if change == "credentials":
            assert rows[0]["access_token"] == current_token
            assert rows[0]["session_token"] == "synthetic-new-session"
            assert all(row["access_token"] == current_token for row in callbacks)
        assert all(row["health_write_skipped"] for row in callbacks)


def test_group_only_change_survives_successful_health_write(db, monkeypatch):
    original = store.upsert_account_record(account(), db)
    key = store.account_delete_key(original)

    def probe(_record):
        store.update_account_record_fields({key: {"group_id": "group-new"}}, path=db, include_records=False)
        return metadata()

    monkeypatch.setattr(health, "fetch_remote_account_info", Mock(side_effect=probe))
    result = health.check_accounts([original], path=db)
    saved = store.load_accounts(db)[0]
    assert saved["group_id"] == "group-new"
    assert saved["health_status"] == "alive"
    assert result["items"][0]["group_id"] == "group-new"
    assert not result["items"][0].get("health_write_skipped")


def test_health_preserves_trial_result_written_while_probe_is_running(db, monkeypatch):
    original = store.upsert_account_record(account(trial_eligibility_status="pending"), db)
    key = store.account_delete_key(original)
    trial_fields = {
        "trial_eligibility_status": "ineligible",
        "trial_eligibility_label": "无免费试用",
        "trial_eligibility_checked_at": "2099-01-02T00:00:00Z",
    }

    def probe(_record):
        store.update_account_record_fields({key: trial_fields}, path=db, include_records=False)
        return metadata()

    monkeypatch.setattr(health, "fetch_remote_account_info", Mock(side_effect=probe))
    checked = health.check_accounts([original], path=db)["items"][0]
    saved = store.load_accounts(db)[0]

    assert checked["health_status"] == saved["health_status"] == "alive"
    assert {field: saved[field] for field in trial_fields} == trial_fields
    assert checked["trial_eligibility_status"] == "ineligible"


@pytest.mark.parametrize("explicit_health_verdict", [
    {},
    {"health_error": "account_deactivated: confirmed by official me"},
    {"status": "已封号"},
    {"reauth_blocked_reason": "account_deactivated"},
])
def test_trial_errors_do_not_derive_deactivation_but_explicit_health_still_does(db, explicit_health_verdict):
    store.upsert_account_record(account(
        health_status="alive",
        status="正常",
        trial_eligibility_status="invalid",
        trial_eligibility_label="账号已停用",
        trial_eligibility_reason="account_deactivated",
        trial_eligibility_result="banned",
        trial_eligibility_error="account_deactivated: old trial request rejected",
    ) | explicit_health_verdict, db)
    saved = store.load_accounts(db)[0]
    public = store.account_public_view(saved)
    selected, skipped = health.select_health_check_records([saved])
    deactivated = bool(explicit_health_verdict)

    assert store.account_is_deactivated(saved) is deactivated
    assert public["is_deactivated"] is deactivated
    assert public["health_status"] == ("invalid" if deactivated else "alive")
    assert public["trial_eligibility_status"] == "invalid"
    assert len(skipped) == int(deactivated)
    assert len(selected) == int(not deactivated)
    assert store.account_is_usable(saved) is not deactivated


def test_valid_team_refresh_still_saves_fresh_token_and_health(db, monkeypatch):
    original = store.upsert_account_record(account(expired=True), db)
    fresh_token = token()
    response = SimpleNamespace(status_code=200, json=lambda: {
        "accessToken": fresh_token,
        "account": {"id": "ws-team", "planType": "team"},
        "user": {"id": "user-a", "email": "a@example.test"},
    })
    monkeypatch.setattr(health, "_protocol_session_request", Mock(return_value=response))
    result = health.check_accounts([original], path=db)
    assert result["items"][0]["access_token"] == fresh_token
    assert store.load_accounts(db)[0]["health_status"] == "alive"


def test_oauth_refresh_cannot_replace_team_with_personal_token(db, monkeypatch):
    original = store.upsert_account_record(account(
        expired=True, session_token="", refresh_token="synthetic-refresh",
    ), db)
    response = SimpleNamespace(status_code=200, json=lambda: {
        "access_token": token(workspace="ws-personal", plan="free"),
        "refresh_token": "synthetic-replacement-refresh",
    })
    client = SimpleNamespace(post=Mock(return_value=response), close=Mock())
    monkeypatch.setattr(health, "_session", Mock(return_value=client))
    health.check_account(original, path=db)
    saved = store.load_accounts(db)[0]
    assert saved["access_token"] == original["access_token"]
    assert saved["refresh_token"] == original["refresh_token"]
    assert saved["plan_type"] == "team"
    health.fetch_remote_account_info.assert_not_called()


def test_health_accepts_just_registered_record_before_store_user_id_backfill(db):
    original = account()
    original.pop("user_id")
    store.upsert_account_record(original, db)
    result = health.check_account(original, path=db)
    assert result["health_status"] == "alive"
    assert not result.get("health_write_skipped")
    assert store.load_accounts(db)[0]["user_id"] == "user-a"


@pytest.mark.parametrize("status,body", [
    (408, ""), (425, ""), (429, "slow down"), (500, ""),
    (502, "bad gateway"), (503, "unavailable"), (504, "gateway timeout"),
    (403, "<html><title>Cloudflare challenge</title></html>"),
])
def test_session_refresh_transient_responses_do_not_invalidate_account(db, monkeypatch, status, body):
    response = SimpleNamespace(status_code=status, text=body, json=lambda: {})
    monkeypatch.setattr(health, "_protocol_session_request", Mock(return_value=response))
    monkeypatch.setattr(health.time, "sleep", Mock())

    with pytest.raises(health.HealthTransientError):
        health.refresh_chatgpt_session_tokens(account(expired=True), force=True)

    checked = health.check_account(
        account(expired=True), persist=False, path=db,
        proxy_candidates=["http://one.example:8080", "http://two.example:8080"],
    )
    assert checked["health_status"] == "unknown"
    assert "nextauth_session" in checked["health_probe_error"]
    health.fetch_remote_account_info.assert_not_called()


@pytest.mark.parametrize("status,payload", [
    (401, {}), (200, {}), (400, {"error": "Your session has ended. Please log in again."}),
])
def test_session_refresh_keeps_genuine_session_rejections_invalid(db, monkeypatch, status, payload):
    response = SimpleNamespace(status_code=status, text="", json=lambda: payload)
    monkeypatch.setattr(health, "_protocol_session_request", Mock(return_value=response))

    with pytest.raises(health.InvalidAccessTokenError):
        health.refresh_chatgpt_session_tokens(account(expired=True), force=True)


@pytest.mark.parametrize("status,payload", [(403, {"error": "forbidden"}), (404, {})])
def test_session_refresh_other_http_errors_are_not_credential_verdicts(db, monkeypatch, status, payload):
    response = SimpleNamespace(status_code=status, text="", json=lambda: payload)
    monkeypatch.setattr(health, "_protocol_session_request", Mock(return_value=response))

    with pytest.raises(RuntimeError) as error:
        health.refresh_chatgpt_session_tokens(account(expired=True), force=True)
    assert not isinstance(error.value, health.InvalidAccessTokenError)


def test_session_refresh_invalid_json_is_transient(db, monkeypatch):
    response = SimpleNamespace(status_code=200, text="<html>upstream error</html>", json=Mock(side_effect=ValueError("invalid JSON")))
    monkeypatch.setattr(health, "_protocol_session_request", Mock(return_value=response))

    with pytest.raises(health.HealthTransientError, match="invalid_json"):
        health.refresh_chatgpt_session_tokens(account(expired=True), force=True)


def test_session_refresh_unexpected_success_payload_is_not_a_session_rejection(db, monkeypatch):
    response = SimpleNamespace(status_code=200, text="", json=lambda: {"error": "upstream_unavailable"})
    monkeypatch.setattr(health, "_protocol_session_request", Mock(return_value=response))

    with pytest.raises(health.HealthTransientError, match="missing_access_token"):
        health.refresh_chatgpt_session_tokens(account(expired=True), force=True)


@pytest.mark.parametrize("status", [408, 425])
def test_health_retryable_http_responses_try_another_route(db, monkeypatch, status):
    probe = Mock(side_effect=[RuntimeError(f"health_http_{status} /backend-api/me"), metadata()])
    monkeypatch.setattr(health, "fetch_remote_account_info", probe)
    monkeypatch.setattr(health.time, "sleep", Mock())

    result = health.check_account(
        account(), persist=False, path=db,
        proxy_candidates=["http://one.example:8080", "http://two.example:8080"],
    )
    assert result["health_status"] == "alive"
    assert probe.call_count == 2
    assert len({call.args[0]["proxy"] for call in probe.call_args_list}) == 2

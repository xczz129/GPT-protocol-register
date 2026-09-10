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

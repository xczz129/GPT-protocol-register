"""Synthetic records only; every persistence operation uses a temporary store."""
from __future__ import annotations

import base64
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier

import pytest

from common import account_store as store


def member(name, **extra):
    return {
        "email": f"{name}@example.test",
        "user_id": f"user-{name}",
        "account_id": "shared-team",
        "plan_type": "team",
        "password": f"synthetic-{name}",
        **extra,
    }


@pytest.fixture(params=["sqlite", "json"])
def account_path(request, tmp_path, monkeypatch):
    path = tmp_path / "accounts.json"
    if request.param == "sqlite":
        monkeypatch.setattr(store, "ACCOUNTS_FILE", path)
        monkeypatch.setattr(store, "ACCOUNTS_DB_FILE", tmp_path / "accounts.sqlite3")
    return path


def test_user_key_round_trip_and_email_lookup(account_path):
    original = member("alpha")
    store.upsert_account_record(original, account_path)
    key = store.account_delete_key(original)
    assert key == "user:user-alpha"
    assert store.load_account_record_by_identity(key.upper(), account_path)["email"] == original["email"]
    assert store.load_account_record_by_identity(original["email"], account_path)["user_id"] == original["user_id"]
    assert store.load_account_record_by_identity("user:missing", account_path) is None
    assert store.load_account_record_by_identity("user:", account_path) is None


def test_legacy_same_email_update_cannot_bridge_team_members(account_path):
    store.upsert_account_records([member("alpha"), member("beta")], account_path)
    saved = store.upsert_account_record({
        "email": "alpha@example.test", "account_id": "shared-team", "health_status": "alive",
    }, account_path)
    rows = {row["user_id"]: row for row in store.load_accounts(account_path)}
    assert len(rows) == 2
    assert rows["user-alpha"]["password"] == "synthetic-alpha"
    assert rows["user-alpha"]["health_status"] == "alive"
    assert rows["user-beta"]["email"] == "beta@example.test"
    assert rows["user-beta"]["password"] == "synthetic-beta"
    assert saved["user_id"] == "user-alpha"


@pytest.mark.parametrize("incoming", [
    {"account_id": "shared-team", "health_status": "alive"},
    {"email": "unknown@example.test", "account_id": "shared-team", "password": "synthetic-unknown"},
])
def test_ambiguous_legacy_restore_is_rejected_without_changing_store(account_path, incoming):
    store.upsert_account_records([member("alpha"), member("beta")], account_path)
    before = deepcopy(store.load_accounts(account_path))
    with pytest.raises(ValueError, match="account_identity_ambiguous"):
        store.upsert_account_records([member("gamma"), incoming], account_path)
    assert store.load_accounts(account_path) == before


def test_known_user_cannot_replace_another_user_at_same_email(account_path):
    store.upsert_account_records([member("alpha"), member("beta")], account_path)
    before = deepcopy(store.load_accounts(account_path))
    with pytest.raises(ValueError, match="account_identity_conflict"):
        store.upsert_account_record(member("beta", email="alpha@example.test"), account_path)
    assert store.load_accounts(account_path) == before


def test_same_user_may_change_email_and_explicit_rebind_still_works(account_path):
    original = member("alpha")
    store.upsert_account_record(original, account_path)
    store.upsert_account_record({**original, "email": "renamed@example.test"}, account_path)
    assert len(store.load_accounts(account_path)) == 1
    renamed = store.load_account_record_by_identity("user:user-alpha", account_path)
    assert renamed["email"] == "renamed@example.test"
    rebound = store.rebind_account_record("user:user-alpha", "rebound@example.test", path=account_path)
    assert rebound["user_id"] == "user-alpha"
    assert rebound["email"] == "rebound@example.test"


def test_unknown_user_cannot_rename_another_members_email(account_path):
    store.upsert_account_record(member("alpha"), account_path)
    before = deepcopy(store.load_accounts(account_path))
    with pytest.raises(ValueError, match="account_identity_ambiguous"):
        store.upsert_account_record({"email": "beta@example.test", "account_id": "shared-team"}, account_path)
    assert store.load_accounts(account_path) == before


def test_legacy_email_and_account_only_fragments_still_bridge():
    rows = store.normalize_accounts([
        {"account_id": "personal-account", "access_token": "synthetic-old"},
        {"email": "alpha@example.test", "password": "synthetic-password"},
        {"email": "alpha@example.test", "account_id": "personal-account", "access_token": "synthetic-new"},
    ])
    assert len(rows) == 1
    assert rows[0]["password"] == "synthetic-password"
    assert rows[0]["access_token"] == "synthetic-new"


def test_normalize_same_email_partial_update_preserves_both_team_members():
    rows = store.normalize_accounts([
        member("alpha"), member("beta"),
        {"email": "alpha@example.test", "account_id": "shared-team", "health_status": "alive"},
    ])
    assert {row["user_id"] for row in rows} == {"user-alpha", "user-beta"}
    assert {row["email"]: row["password"] for row in rows} == {
        "alpha@example.test": "synthetic-alpha", "beta@example.test": "synthetic-beta",
    }


def test_conflicting_token_identity_is_rejected_before_write(account_path):
    body = {"https://api.openai.com/auth": {"chatgpt_user_id": "user-beta"}}
    encoded = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    with pytest.raises(ValueError, match="account_identity_conflict"):
        store.upsert_account_record(member("alpha", access_token=f"header.{encoded}.signature"), account_path)
    assert store.load_accounts(account_path) == []


def test_deleted_member_is_not_resurrected_by_update_only_batch(account_path):
    store.upsert_account_record(member("beta"), account_path)
    assert not store.account_record_exists(member("alpha"), account_path)
    assert store.account_record_exists(member("beta"), account_path)
    assert store.upsert_account_records([member("alpha")], account_path, create=False) == []
    assert [row["user_id"] for row in store.load_accounts(account_path)] == ["user-beta"]


def test_skip_existing_adds_distinct_team_members_without_replacing_existing(account_path):
    store.upsert_account_record(member("alpha"), account_path)
    stored = store.upsert_account_records([
        member("alpha", password="must-not-replace", email="renamed@example.test"),
        member("beta"), member("beta", password="must-not-replace-beta"), member("gamma"),
    ], account_path, skip_existing=True)
    assert [row["user_id"] for row in stored] == ["user-beta", "user-gamma"]
    rows = {row["user_id"]: row for row in store.load_accounts(account_path)}
    assert len(rows) == 3
    assert rows["user-alpha"]["email"] == "alpha@example.test"
    assert rows["user-alpha"]["password"] == "synthetic-alpha"
    assert rows["user-beta"]["password"] == "synthetic-beta"


def test_skip_existing_rolls_back_whole_batch_on_ambiguous_legacy_member(account_path):
    store.upsert_account_records([member("alpha"), member("beta")], account_path)
    before = deepcopy(store.load_accounts(account_path))
    with pytest.raises(ValueError, match="account_identity_ambiguous"):
        store.upsert_account_records([
            member("gamma"), {"account_id": "shared-team", "password": "ambiguous"},
        ], account_path, skip_existing=True)
    assert store.load_accounts(account_path) == before


def test_access_token_users_remain_distinct_when_workspace_id_changes(account_path):
    def token(user, account_id):
        body = {"https://api.openai.com/auth": {"chatgpt_user_id": user, "chatgpt_account_id": account_id}}
        encoded = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
        return f"header.{encoded}.signature"

    for name in ("alpha", "beta"):
        store.upsert_account_record({
            "email": f"{name}@example.test", "account_id": f"personal-{name}",
            "access_token": token(f"user-{name}", f"personal-{name}"),
        }, account_path)
    for name in ("alpha", "beta"):
        store.upsert_account_record({
            "email": f"{name}@example.test", "account_id": "shared-team",
            "access_token": token(f"user-{name}", "shared-team"),
        }, account_path)
    rows = store.load_accounts(account_path)
    assert {row["user_id"] for row in rows} == {"user-alpha", "user-beta"}
    assert {row["account_id"] for row in rows} == {"shared-team"}


def test_concurrent_merge_restore_preserves_both_new_members(account_path):
    # Initialize schema/migration before synchronizing the independent writers.
    store.load_accounts(account_path)
    barrier = Barrier(2)

    def restore(name):
        barrier.wait(timeout=5)
        return store.upsert_account_records([member(name)], account_path, skip_existing=True)

    with ThreadPoolExecutor(max_workers=2) as executor:
        saved = list(executor.map(restore, ("alpha", "beta")))
    assert [len(batch) for batch in saved] == [1, 1]
    assert {row["user_id"] for row in store.load_accounts(account_path)} == {"user-alpha", "user-beta"}


def test_json_ambiguous_result_resolution_does_not_write_file(tmp_path):
    path = tmp_path / "legacy.json"
    store.upsert_account_record(member("existing", account_id="existing-personal"), path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="account_identity_ambiguous"):
        store.upsert_account_records([
            {"account_id": "shared-team"}, member("alpha"), member("beta"),
        ], path)
    assert path.read_bytes() == before
    assert [row["user_id"] for row in store.load_accounts(path)] == ["user-existing"]

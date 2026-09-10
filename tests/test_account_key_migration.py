"""Temporary-store regressions for identity migration and conditional updates."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import sqlite3
from threading import Barrier
from types import SimpleNamespace

import pytest

from common import account_store as store


@pytest.fixture(params=["sqlite", "json"])
def account_store(request, tmp_path, monkeypatch):
    path = tmp_path / "accounts.json"
    database = tmp_path / "accounts.sqlite3"
    if request.param == "sqlite":
        monkeypatch.setattr(store, "ACCOUNTS_FILE", path)
        monkeypatch.setattr(store, "ACCOUNTS_DB_FILE", database)
    return SimpleNamespace(path=path, database=database, backend=request.param)


def legacy(name="legacy", **extra):
    return {
        "email": f"{name}@example.test",
        "account_id": f"old-{name}",
        "access_token": f"synthetic-at-{name}",
        "refresh_token": f"synthetic-rt-{name}",
        "password": f"synthetic-password-{name}",
        "totp_secret": f"synthetic-totp-{name}",
        "health_status": "unknown",
        **extra,
    }


def token(user):
    payload = {"https://api.openai.com/auth": {"chatgpt_user_id": user}}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.synthetic"


@pytest.mark.parametrize("via_token", [False, True])
def test_backfilled_user_migrates_legacy_key_without_duplicate_or_lost_credentials(account_store, via_token):
    initial = store.upsert_account_record(legacy(), account_store.path)
    old_key = store.account_delete_key(initial)
    fields = {"access_token": token("stable-user")} if via_token else {"user_id": "stable-user"}
    changed, records = store.update_account_record_fields({old_key: fields}, path=account_store.path)

    assert changed == 1
    assert len(records) == store.count_accounts(account_store.path) == 1
    assert store.account_delete_key(records[0]) == "user:stable-user"
    assert records[0]["user_id"] == "stable-user"
    for field in ("password", "totp_secret", "refresh_token"):
        assert records[0][field] == initial[field]
    assert store.load_account_records_by_keys([old_key], path=account_store.path) == {}
    if account_store.backend == "sqlite":
        with sqlite3.connect(account_store.database) as connection:
            rows = connection.execute("SELECT canonical_key, payload, public_payload FROM accounts").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "user:stable-user"
        assert json.loads(rows[0][1])["user_id"] == "stable-user"
        assert json.loads(rows[0][2])["delete_key"] == "user:stable-user"


def test_occupied_target_key_rolls_back_and_preserves_both_sets_of_credentials(account_store):
    originals = [legacy("old"), legacy("occupied", user_id="occupied-user")]
    store.upsert_account_records(originals, account_store.path)
    before = deepcopy(store.load_accounts(account_store.path))
    with pytest.raises(ValueError, match="account_identity_conflict"):
        store.update_account_record_fields({
            "account_id:old-old": {"user_id": "occupied-user", "health_status": "alive"},
            "user:occupied-user": {"health_status": "limited"},
        }, path=account_store.path)
    assert store.load_accounts(account_store.path) == before
    assert store.count_accounts(account_store.path) == 2


def test_two_rows_cannot_migrate_to_same_new_key(account_store):
    originals = [legacy("one"), legacy("two")]
    store.upsert_account_records(originals, account_store.path)
    before = deepcopy(store.load_accounts(account_store.path))
    with pytest.raises(ValueError, match="account_identity_conflict"):
        store.update_account_record_fields({
            store.account_delete_key(record): {"user_id": "same-user"}
            for record in originals
        }, path=account_store.path)
    assert store.load_accounts(account_store.path) == before


def test_existing_known_user_cannot_be_changed_by_field_update(account_store):
    original = legacy(user_id="known-user")
    store.upsert_account_record(original, account_store.path)
    before = deepcopy(store.load_accounts(account_store.path))
    with pytest.raises(ValueError, match="account_identity_conflict"):
        store.update_account_record_fields({
            "user:known-user": {"user_id": "different-user"},
        }, path=account_store.path)
    assert store.load_accounts(account_store.path) == before


def test_inconsistent_token_user_rejected_without_changing_store(account_store):
    original = legacy(user_id="known-user")
    store.upsert_account_record(original, account_store.path)
    before = deepcopy(store.load_accounts(account_store.path))
    with pytest.raises(ValueError, match="account_identity_conflict"):
        store.update_account_record_fields({
            "user:known-user": {"access_token": token("different-user")},
        }, path=account_store.path)
    assert store.load_accounts(account_store.path) == before


def test_sqlite_failure_after_migration_write_rolls_back_deleted_key_and_payload(account_store, monkeypatch):
    if account_store.backend != "sqlite":
        pytest.skip("SQLite transaction rollback")
    original = store.upsert_account_record(legacy(), account_store.path)
    before = deepcopy(store.load_accounts(account_store.path))
    real_store = store._store_sqlite_record

    def failing_store(connection, record):
        real_store(connection, record)
        assert connection.execute("SELECT canonical_key FROM accounts").fetchall() == [("user:new-user",)]
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(store, "_store_sqlite_record", failing_store)
    with pytest.raises(OSError, match="synthetic disk failure"):
        store.update_account_record_fields({
            store.account_delete_key(original): {"user_id": "new-user"},
        }, path=account_store.path)
    assert store.load_accounts(account_store.path) == before
    with sqlite3.connect(account_store.database) as connection:
        assert connection.execute("SELECT canonical_key FROM accounts").fetchall() == [
            (store.account_delete_key(original),),
        ]


def test_serialization_failure_is_atomic_for_both_backends(account_store):
    store.upsert_account_records([legacy("one"), legacy("two")], account_store.path)
    before = deepcopy(store.load_accounts(account_store.path))
    with pytest.raises(TypeError):
        store.update_account_record_fields({
            "account_id:old-one": {"user_id": "new-one"},
            "account_id:old-two": {"unserializable": object()},
        }, path=account_store.path)
    assert store.load_accounts(account_store.path) == before


def test_conditional_update_matches_missing_fields_as_none_and_can_migrate(account_store):
    original = store.upsert_account_record(legacy(), account_store.path)
    key = store.account_delete_key(original)
    changed, records = store.update_account_record_fields(
        {key: {"user_id": "new-user", "health_status": "alive"}},
        path=account_store.path,
        expected_fields={key: {"access_token": original["access_token"], "user_id": None}},
    )
    assert changed == 1
    assert len(records) == 1
    assert records[0]["user_id"] == "new-user"
    assert records[0]["health_status"] == "alive"


@pytest.mark.parametrize("expected_value", ["stale-token", None])
def test_stale_expected_token_skips_without_changing_timestamp_or_identity(account_store, expected_value):
    original = store.upsert_account_record(legacy(), account_store.path)
    key = store.account_delete_key(original)
    before = deepcopy(store.load_accounts(account_store.path))
    changed, records = store.update_account_record_fields(
        {key: {"user_id": "new-user", "health_status": "invalid"}},
        path=account_store.path,
        expected_fields={key: {"access_token": expected_value}},
    )
    assert changed == 0
    assert records == before
    assert store.load_accounts(account_store.path) == before


def test_multirow_cas_skips_only_stale_rows_and_preserves_unconditional_contract(account_store):
    store.upsert_account_records([legacy("one"), legacy("two"), legacy("three")], account_store.path)
    changed, records = store.update_account_record_fields(
        {f"account_id:old-{name}": {"health_status": "alive"} for name in ("one", "two", "three")},
        path=account_store.path,
        expected_fields={
            "account_id:old-one": {"access_token": "synthetic-at-one", "health_status": "unknown"},
            "account_id:old-two": {"access_token": "stale-at-two"},
        },
        include_records=False,
    )
    assert (changed, records) == (2, [])
    statuses = {row["email"]: row["health_status"] for row in store.load_accounts(account_store.path)}
    assert statuses == {
        "one@example.test": "alive", "two@example.test": "unknown", "three@example.test": "alive",
    }


def test_two_concurrent_compare_and_swap_writers_cannot_both_replace_same_token(account_store):
    original = store.upsert_account_record(legacy(), account_store.path)
    key = store.account_delete_key(original)
    barrier = Barrier(2)

    def update(number):
        barrier.wait(timeout=5)
        return store.update_account_record_fields(
            {key: {"access_token": f"synthetic-replacement-{number}"}},
            path=account_store.path,
            expected_fields={key: {"access_token": original["access_token"]}},
            include_records=False,
        )[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(update, number) for number in (1, 2)]
        outcomes = [future.result(timeout=10) for future in futures]
    assert sorted(outcomes) == [0, 1]
    assert len(store.load_accounts(account_store.path)) == 1


def test_malformed_cas_map_fails_closed_before_writes(account_store):
    original = store.upsert_account_record(legacy(), account_store.path)
    key = store.account_delete_key(original)
    with pytest.raises(TypeError, match="expected_fields"):
        store.update_account_record_fields(
            {key: {"health_status": "alive"}},
            path=account_store.path,
            expected_fields={key: None},
        )
    assert store.load_accounts(account_store.path)[0]["health_status"] == "unknown"

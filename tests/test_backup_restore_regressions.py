"""Recovery regressions with synthetic accounts and isolated databases."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace
import zipfile

import pytest

from common import account_store, panel_ops
from web.routes import accounts_trash


@pytest.fixture
def recovery_store(tmp_path, monkeypatch):
    accounts = tmp_path / "accounts.json"
    monkeypatch.setattr(account_store, "ACCOUNTS_FILE", accounts)
    monkeypatch.setattr(account_store, "ACCOUNTS_DB_FILE", tmp_path / "accounts.sqlite3")
    monkeypatch.setattr(panel_ops, "ACCOUNTS_FILE", accounts)
    monkeypatch.setattr(panel_ops, "TRASH_FILE", tmp_path / "trash.json")
    monkeypatch.setattr(panel_ops, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(accounts_trash, "_state", lambda: SimpleNamespace(ACCOUNTS_FILE=accounts))
    panel_ops._invalidate_trash_cache()
    yield accounts
    panel_ops._invalidate_trash_cache()


def member(name):
    return {
        "email": f"{name}@example.invalid",
        "user_id": f"user-{name}",
        "account_id": "shared-workspace",
        "plan_type": "team",
        "password": f"synthetic-{name}",
    }


def backup_payload(records):
    panel_ops.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    name = "gpt-accounts-backup-synthetic.json"
    (panel_ops.BACKUP_DIR / name).write_text(json.dumps({"accounts": records}), encoding="utf-8")
    return name


def trash_payload(records):
    rows = [
        {"trash_id": f"trash-{i}", "record": record, "deleted_at": panel_ops._now()}
        for i, record in enumerate(records)
    ]
    panel_ops.TRASH_FILE.write_text(json.dumps(rows), encoding="utf-8")
    return rows


def test_merge_restores_distinct_team_member_and_preserves_existing(recovery_store):
    current = {**member("alpha"), "group_id": "current", "password": "current-password"}
    account_store.upsert_account_record(current, recovery_store)
    name = backup_payload([member("alpha"), member("beta")])

    result = panel_ops.restore_account_backup(name, "merge")

    assert result == {"mode": "merge", "restored": 1, "skipped": 1, "total": 2}
    rows = {row["user_id"]: row for row in account_store.load_accounts(recovery_store)}
    assert set(rows) == {"user-alpha", "user-beta"}
    assert rows["user-alpha"]["password"] == "current-password"
    assert rows["user-alpha"]["group_id"] == "current"


def test_merge_deduplicates_one_user_within_backup(recovery_store):
    name = backup_payload([member("beta"), member("beta")])
    result = panel_ops.restore_account_backup(name, "merge")
    assert result["restored"] == 1
    assert result["skipped"] == 1
    assert len(account_store.load_accounts(recovery_store)) == 1


def test_merge_identity_conflict_rolls_back_entire_batch(recovery_store):
    account_store.upsert_account_record(member("alpha"), recovery_store)
    conflict = {**member("beta"), "email": member("alpha")["email"]}
    name = backup_payload([member("gamma"), conflict])

    with pytest.raises(ValueError):
        panel_ops.restore_account_backup(name, "merge")

    assert [row["user_id"] for row in account_store.load_accounts(recovery_store)] == ["user-alpha"]


def test_failed_database_restore_keeps_every_trash_entry(recovery_store, monkeypatch):
    before = trash_payload([member("alpha"), member("beta")])
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("simulated write failure")
    monkeypatch.setattr(panel_ops, "upsert_account_records", fail)

    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(accounts_trash.accounts_trash_restore(accounts_trash.TrashRestoreRequest(ids=["trash-0"])))

    assert json.loads(panel_ops.TRASH_FILE.read_text(encoding="utf-8")) == before
    assert account_store.load_accounts(recovery_store) == []


def test_successful_trash_restore_removes_only_committed_selection(recovery_store):
    before = trash_payload([member("alpha"), member("beta")])
    result = asyncio.run(accounts_trash.accounts_trash_restore(accounts_trash.TrashRestoreRequest(ids=["trash-0"])))
    assert result == {"ok": True, "restored": 1}
    assert json.loads(panel_ops.TRASH_FILE.read_text(encoding="utf-8")) == before[1:]
    assert [row["user_id"] for row in account_store.load_accounts(recovery_store)] == ["user-alpha"]


def test_failed_trash_removal_can_be_retried_without_duplicate_accounts(recovery_store, monkeypatch):
    before = trash_payload([member("alpha")])
    write = panel_ops._write_json
    def fail(*args, **kwargs):
        raise OSError("simulated trash rename failure")
    monkeypatch.setattr(panel_ops, "_write_json", fail)

    with pytest.raises(OSError):
        panel_ops.restore_trash(["trash-0"], recovery_store)
    assert json.loads(panel_ops.TRASH_FILE.read_text(encoding="utf-8")) == before
    assert len(account_store.load_accounts(recovery_store)) == 1

    monkeypatch.setattr(panel_ops, "_write_json", write)
    assert panel_ops.restore_trash(["trash-0"], recovery_store) == 1
    assert len(account_store.load_accounts(recovery_store)) == 1
    assert json.loads(panel_ops.TRASH_FILE.read_text(encoding="utf-8")) == []


def test_concurrent_trash_restore_commits_selection_once(recovery_store, monkeypatch):
    trash_payload([member("alpha")])
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def save(records, path):
        calls.append(records)
        entered.set()
        assert release.wait(3)
        return records
    def second():
        second_started.set()
        return panel_ops.restore_trash(["trash-0"], recovery_store)
    monkeypatch.setattr(panel_ops, "upsert_account_records", save)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(panel_ops.restore_trash, ["trash-0"], recovery_store)
        try:
            assert entered.wait(3)
            other = pool.submit(second)
            assert second_started.wait(3)
        finally:
            release.set()
        assert sorted([first.result(timeout=3), other.result(timeout=3)]) == [0, 1]
    assert len(calls) == 1


@pytest.fixture
def backup_files(tmp_path, monkeypatch):
    directory = tmp_path / "backups"
    monkeypatch.setattr(panel_ops, "BACKUP_DIR", directory)
    monkeypatch.setattr(panel_ops, "BACKUP_FILES", {})
    monkeypatch.setattr(panel_ops, "BACKUP_DB_FILES", {})
    monkeypatch.setattr(panel_ops, "BACKUP_DIR_SOURCES", {})
    return directory


def test_corrupt_database_does_not_publish_backup_or_prune_good_one(tmp_path, backup_files, monkeypatch):
    database = tmp_path / "accounts.sqlite3"
    database.write_bytes(b"deliberately invalid SQLite file")
    backup_files.mkdir()
    previous = backup_files / "gpt-register-backup-previous.zip"
    previous.write_bytes(b"previous backup")
    old = time.time() - 10 * 86400
    os.utime(previous, (old, old))
    monkeypatch.setattr(panel_ops, "BACKUP_DB_FILES", {"registered_accounts.sqlite3": database})

    with pytest.raises(RuntimeError, match="数据库快照失败"):
        panel_ops.create_backup()

    assert list(backup_files.iterdir()) == [previous]
    assert previous.read_bytes() == b"previous backup"


def test_backup_contains_committed_wal_rows(tmp_path, backup_files, monkeypatch):
    database = tmp_path / "accounts.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('synthetic')")
        connection.commit()
        monkeypatch.setattr(panel_ops, "BACKUP_DB_FILES", {"registered_accounts.sqlite3": database})
        result = panel_ops.create_backup()
    finally:
        connection.close()

    restored = tmp_path / "restored.sqlite3"
    with zipfile.ZipFile(backup_files / result["name"]) as archive:
        assert archive.namelist() == ["registered_accounts.sqlite3"]
        restored.write_bytes(archive.read("registered_accounts.sqlite3"))
    check = sqlite3.connect(restored)
    try:
        assert check.execute("SELECT value FROM sample").fetchall() == [("synthetic",)]
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        check.close()
    assert not list(backup_files.glob(".db-backup-*"))


def test_missing_optional_database_allows_config_backup(tmp_path, backup_files, monkeypatch):
    config = tmp_path / "sample.yaml"
    config.write_text("sample: value\n", encoding="utf-8")
    monkeypatch.setattr(panel_ops, "BACKUP_FILES", {"config.yaml": config})
    monkeypatch.setattr(panel_ops, "BACKUP_DB_FILES", {"optional.sqlite3": tmp_path / "absent.sqlite3"})
    result = panel_ops.create_backup()
    with zipfile.ZipFile(backup_files / result["name"]) as archive:
        assert archive.namelist() == ["config.yaml"]


def test_two_backups_do_not_overwrite_each_other(backup_files):
    first = panel_ops.create_backup()
    second = panel_ops.create_backup()
    assert first["name"] != second["name"]
    assert (backup_files / first["name"]).is_file()
    assert (backup_files / second["name"]).is_file()

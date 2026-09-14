"""Account deletion must preserve every recovery record until it expires."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from common import account_store, panel_ops
from web.routes import accounts_io
from web.routes.shared_models import BulkDeleteRequest


def account(index: int) -> dict:
    return {
        "email": f"recovery-{index}@example.invalid",
        "user_id": f"recovery-user-{index}",
        "account_id": f"recovery-account-{index}",
        "password": f"synthetic-password-{index}",
        "totp_secret": "JBSWY3DPEHPK3PXP",
        "health_status": "invalid",
    }


@pytest.fixture
def recovery_files(tmp_path, monkeypatch):
    accounts = tmp_path / "accounts.json"
    monkeypatch.setattr(account_store, "ACCOUNTS_FILE", accounts)
    monkeypatch.setattr(account_store, "ACCOUNTS_DB_FILE", tmp_path / "accounts.sqlite3")
    monkeypatch.setattr(panel_ops, "ACCOUNTS_FILE", accounts)
    monkeypatch.setattr(panel_ops, "TRASH_FILE", tmp_path / "trash.json")
    panel_ops._invalidate_trash_cache()
    yield accounts
    panel_ops._invalidate_trash_cache()


def read_archive():
    return json.loads(panel_ops.TRASH_FILE.read_text(encoding="utf-8"))


def test_large_batches_keep_all_accounts_and_restore_rows_outside_display_cache(recovery_files):
    # The incident deleted 6,914 records. Both existing history and the new
    # batch must survive; increasing the old 2,000-row cap is not a fix.
    originals = [account(index) for index in range(6914)]
    assert panel_ops.archive_accounts(originals[:1007], reason="manual") == 1007
    assert panel_ops.archive_accounts(iter(originals[1007:]), reason="manual") == 5907

    archived = read_archive()
    assert len(archived) == len(originals)
    assert [row["record"] for row in archived] == originals
    assert len({row["trash_id"] for row in archived}) == len(originals)

    recent, email_reasons = panel_ops.load_trash_snapshot()
    assert len(recent) == panel_ops.TRASH_DISPLAY_LIMIT
    assert len(panel_ops.list_trash(limit=100_000)) == panel_ops.TRASH_DISPLAY_LIMIT
    assert set(email_reasons) == {record["email"] for record in originals}
    oldest_id = archived[0]["trash_id"]
    assert oldest_id not in {row["trash_id"] for row in recent}
    assert len(read_archive()) == len(originals)

    # Recovery reads the complete archive, never the limited display cache.
    assert panel_ops.restore_trash([oldest_id], recovery_files) == 1
    assert panel_ops.restore_trash(
        (row["trash_id"] for row in archived[1:]), recovery_files
    ) == len(originals) - 1
    restored = {row["email"]: row for row in account_store.load_accounts(recovery_files)}
    assert set(restored) == {record["email"] for record in originals}
    for original in originals:
        assert all(restored[original["email"]][key] == value for key, value in original.items())
    assert read_archive() == []
    assert panel_ops.load_trash_snapshot() == ([], {})
    assert panel_ops.restore_trash([oldest_id], recovery_files) == 0


def test_retention_expires_only_unambiguously_old_rows(recovery_files, monkeypatch):
    now = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(panel_ops, "datetime", FrozenDatetime)
    cutoff = now - timedelta(days=panel_ops.TRASH_RETENTION_DAYS)
    timestamps = [
        (cutoff - timedelta(seconds=1)).isoformat(),
        cutoff.isoformat(),
        now.isoformat(),
        "unknown legacy timestamp",
        "2026-01-01T00:00:00",  # No timezone: do not guess its expiry.
        "",
    ]
    old_rows = [
        {"trash_id": f"old-{i}", "deleted_at": stamp, "record": account(i)}
        for i, stamp in enumerate(timestamps)
    ]
    panel_ops.TRASH_FILE.write_text(json.dumps(old_rows), encoding="utf-8")

    panel_ops.archive_accounts((account(i) for i in range(10, 2117)), reason="manual")

    stored = read_archive()
    assert stored[:5] == old_rows[1:]
    assert len(stored) == 5 + 2107


def deletion_state(path: Path):
    delete = Mock(wraps=account_store.delete_account_records)
    state = SimpleNamespace(
        ACCOUNTS_FILE=path,
        _load_accounts=lambda: account_store.load_accounts(path),
        archive_accounts=panel_ops.archive_accounts,
        delete_account_records=delete,
        count_accounts=account_store.count_accounts,
        normalized_health_status=account_store.normalized_health_status,
        account_is_deactivated=lambda record: False,
        _account_key=account_store.account_delete_key,
    )
    return state, delete


@pytest.mark.parametrize("route", [accounts_io.accounts_delete, accounts_io.accounts_delete_invalid])
def test_archive_publish_failure_prevents_account_deletion(recovery_files, monkeypatch, route):
    account_store.upsert_account_records([account(1), account(2)], recovery_files)
    panel_ops.archive_accounts([account(0)], reason="manual")
    before_accounts = account_store.load_accounts(recovery_files)
    before_trash = panel_ops.TRASH_FILE.read_bytes()
    state, delete = deletion_state(recovery_files)
    monkeypatch.setattr(accounts_io, "_state", lambda: state)
    replace = Path.replace

    def fail_trash_publish(self, target):
        if Path(target) == panel_ops.TRASH_FILE:
            raise OSError("simulated archive disk failure")
        return replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_trash_publish)
    with pytest.raises(OSError, match="archive disk failure"):
        asyncio.run(route(BulkDeleteRequest(delete_all=True)))

    delete.assert_not_called()
    assert account_store.load_accounts(recovery_files) == before_accounts
    assert panel_ops.TRASH_FILE.read_bytes() == before_trash
    assert not list(panel_ops.TRASH_FILE.parent.glob(".trash.json.*.tmp"))


@pytest.mark.parametrize("route", [accounts_io.accounts_delete, accounts_io.accounts_delete_invalid])
@pytest.mark.parametrize("broken", [b"{truncated", b"{}", b"[null]"])
def test_corrupt_archive_prevents_overwrite_and_account_deletion(
    recovery_files, monkeypatch, route, broken
):
    account_store.upsert_account_record(account(1), recovery_files)
    before_accounts = account_store.load_accounts(recovery_files)
    panel_ops.TRASH_FILE.write_bytes(broken)
    state, delete = deletion_state(recovery_files)
    monkeypatch.setattr(accounts_io, "_state", lambda: state)

    with pytest.raises(ValueError):
        asyncio.run(route(BulkDeleteRequest(delete_all=True)))

    delete.assert_not_called()
    assert account_store.load_accounts(recovery_files) == before_accounts
    assert panel_ops.TRASH_FILE.read_bytes() == broken


@pytest.mark.parametrize("operation", ["archive", "restore", "take"])
def test_unreadable_archive_is_preserved(recovery_files, monkeypatch, operation):
    panel_ops.archive_accounts([account(1)], reason="manual")
    before = panel_ops.TRASH_FILE.read_bytes()
    trash_id = read_archive()[0]["trash_id"]
    read_text = Path.read_text

    def fail_trash_read(self, *args, **kwargs):
        if self == panel_ops.TRASH_FILE:
            raise PermissionError("simulated archive read failure")
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_trash_read)
    with pytest.raises(PermissionError, match="archive read failure"):
        if operation == "archive":
            panel_ops.archive_accounts([account(2)], reason="manual")
        elif operation == "restore":
            panel_ops.restore_trash([trash_id], recovery_files)
        else:
            panel_ops.take_trash([trash_id])

    assert panel_ops.TRASH_FILE.read_bytes() == before
    assert account_store.load_accounts(recovery_files) == []

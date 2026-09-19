"""A stopped batch must persist finished probes and leave remaining rows intact."""
import threading
from unittest.mock import Mock

from common import account_health as health
from common import account_store as store


def test_shutdown_keeps_completed_rows_and_does_not_probe_remaining(tmp_path, monkeypatch):
    path = tmp_path / "accounts.json"
    monkeypatch.setattr(store, "ACCOUNTS_FILE", path)
    monkeypatch.setattr(store, "ACCOUNTS_DB_FILE", tmp_path / "accounts.sqlite3")
    monkeypatch.setattr(health, "record_operation_event", Mock())
    records = [store.upsert_account_record({
        "email": f"probe-{i}@example.test", "user_id": f"user-{i}",
        "account_id": f"account-{i}", "access_token": f"synthetic-{i}",
        "health_status": "limited", "health_checked_at": "old-check",
    }, path) for i in range(20)]
    stop = threading.Event()

    def probe(record, **kwargs):
        stop.set()
        return {**record, "health_status": "alive", "health_checked_at": "new-check"}

    check = Mock(side_effect=probe)
    monkeypatch.setattr(health, "check_account", check)
    persisted = Mock()
    result = health.check_accounts(records, path=path, workers=1,
                                   stop_check=stop.is_set, item_callback=persisted)
    assert check.call_count == 1
    assert result["interrupted"] is True
    assert result["checked"] == result["alive"] == 1
    assert result["stopped"] == 19
    assert persisted.call_count == 1
    rows = store.load_accounts(path)
    assert sum(row["health_checked_at"] == "new-check" for row in rows) == 1
    assert sum(row["health_checked_at"] == "old-check" for row in rows) == 19


def test_shutdown_before_start_does_not_invent_results(tmp_path, monkeypatch):
    probe = Mock(side_effect=AssertionError("stopped batch started a request"))
    monkeypatch.setattr(health, "check_account", probe)
    result = health.check_accounts(
        [{"email": "pending@example.test", "access_token": "synthetic"}],
        path=tmp_path / "accounts.json", stop_check=lambda: True,
    )
    assert result["interrupted"] is True
    assert result["checked"] == result["usable"] == result["unavailable"] == 0
    assert result["stopped"] == 1
    assert result["items"] == []
    probe.assert_not_called()

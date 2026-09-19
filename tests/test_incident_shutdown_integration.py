"""Shutdown preserves completed health results and durable resume boundaries."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from common import account_health as health
from common import task_checkpoint
from common.account_store import account_delete_key
from web.routes import trial_health


@pytest.fixture
def web_state(monkeypatch, tmp_path):
    import web_app

    monkeypatch.setenv("TASK_CHECKPOINT_DIR", str(tmp_path / "checkpoints"))
    monkeypatch.setattr(web_app, "_ACCOUNT_HEALTH_PROGRESS", {})
    monkeypatch.setattr(web_app, "_ACCOUNT_HEALTH_PROGRESS_LOCK", threading.Lock())
    monkeypatch.setattr(web_app, "_ACCOUNT_HEALTH_RUN_LOCK", threading.Lock())
    monkeypatch.setattr(web_app, "_ACCOUNT_HEALTH_CHECKPOINT_KEYS", [])
    monkeypatch.setattr(web_app, "_ACCOUNT_HEALTH_DONE_KEYS", set())
    monkeypatch.setattr(web_app, "_SERVER_SHUTDOWN_EVENT", threading.Event())
    monkeypatch.setattr(web_app, "_account_health_resource_budget", lambda workers: (workers, "fixture"))
    monkeypatch.setattr(web_app, "_refresh_account_health_resource_state", lambda: None)
    monkeypatch.setattr(web_app, "_account_health_worker_limit", lambda workers: workers)
    monkeypatch.setattr(web_app, "_current_health_proxy_pool", lambda: [])
    monkeypatch.setattr(web_app, "account_public_view", lambda row: {**row, "delete_key": account_delete_key(row)})
    # Keep checkpoint encoding/restoration real while avoiding timer threads.
    monkeypatch.setattr(web_app, "_ACCOUNT_HEALTH_CHECKPOINT_SAVER", SimpleNamespace(
        submit=lambda _callback: None,
        flush_now=lambda callback: task_checkpoint.save_checkpoint("health_check", callback()),
    ))
    monkeypatch.setattr(health, "record_operation_event", Mock())
    persisted = []

    def persist(batch, _path):
        rows = [deepcopy(checked) for _original, checked in batch]
        persisted.extend(rows)
        return rows

    monkeypatch.setattr(health, "_persist_health_results", persist)
    return SimpleNamespace(app=web_app, persisted=persisted)


def _records():
    return [
        {"email": "first@example.test", "health_status": "alive"},
        {"email": "pending@example.test", "health_status": "alive"},
        {"email": "disabled@example.test", "health_status": "invalid", "is_deactivated": True},
    ]


def _finish_interrupted_batch(boundary, monkeypatch):
    records = _records()
    probes, skipped = health.select_health_check_records(records)

    def probe(row, **_kwargs):
        boundary.app._SERVER_SHUTDOWN_EVENT.set()
        return {**row, "health_status": "unknown", "health_probe_error": "synthetic connection timeout"}

    check = Mock(side_effect=probe)
    monkeypatch.setattr(health, "check_account", check)
    result = asyncio.run(trial_health._run_account_health_batch(
        boundary.app, records=records, keys=[account_delete_key(row) for row in records],
        workers=1, probe_records=probes, skipped_records=skipped,
    ))
    check.assert_called_once()
    return result


def test_shutdown_before_start_skips_all_probes_and_keeps_them_pending(web_state, monkeypatch):
    records = _records()
    check = Mock(side_effect=AssertionError("probe must not start after shutdown"))
    monkeypatch.setattr(health, "check_account", check)
    web_state.app._SERVER_SHUTDOWN_EVENT.set()

    result = health.check_accounts(records, workers=2, stop_check=web_state.app._SERVER_SHUTDOWN_EVENT.is_set)

    check.assert_not_called()
    assert result["checked"] == result["usable"] == result["unknown"] == 0
    assert result["interrupted"] is True and result["stopped"] == 2
    assert result["skipped_known_deactivated"] == 1
    assert web_state.persisted == []


def test_shutdown_flushes_completed_unknown_result_before_releasing_health_slot(web_state, monkeypatch):
    result = _finish_interrupted_batch(web_state, monkeypatch)
    progress = web_state.app._account_health_progress_snapshot()

    assert result["interrupted"] is True and result["stopped"] == 1
    assert result["checked"] == result["unknown"] == 1 and result["usable"] == 0
    assert len(web_state.persisted) == 1
    assert web_state.persisted[0]["health_status"] == "unknown"
    assert progress["unknown"] == progress["done"] == progress["checked"] == 1
    assert progress["error"] == progress["usable"] == 0
    assert progress["pending_keys"] == ["email:pending@example.test"]
    assert progress["interrupted"] is True and progress["running"] is False
    assert not web_state.app._ACCOUNT_HEALTH_RUN_LOCK.locked()


def test_restart_keeps_only_unfinished_accounts_in_resume_queue(web_state, monkeypatch):
    _finish_interrupted_batch(web_state, monkeypatch)
    web_state.app._account_health_checkpoint_restore()
    restored = web_state.app._account_health_progress_snapshot()

    assert restored["pending_keys"] == ["email:pending@example.test"]
    assert restored["pending_count"] == 1
    assert restored["unknown"] == restored["checked"] == 1


def test_restart_preserves_graceful_shutdown_interrupted_flag(web_state, monkeypatch):
    _finish_interrupted_batch(web_state, monkeypatch)
    saved = task_checkpoint.load_checkpoint("health_check")
    assert saved["running"] is False and saved["snapshot"]["interrupted"] is True
    web_state.app._account_health_checkpoint_restore()

    restored = web_state.app._account_health_progress_snapshot()
    assert restored["interrupted"] is True and restored["running"] is False

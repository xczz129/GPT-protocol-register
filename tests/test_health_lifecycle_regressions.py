"""Health request cancellation must not overlap running probe batches."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from common import account_health as health
from web.routes import trial_health


def _state():
    progress = {"running": False}
    finishes = []

    def start(*_args, **_kwargs):
        progress["running"] = True

    def finish(**fields):
        progress["running"] = False
        finishes.append(fields)

    state = SimpleNamespace(
        _ACCOUNT_HEALTH_RUN_LOCK=threading.Lock(),
        _account_health_progress_snapshot=lambda **_kwargs: dict(progress),
        _start_account_health_progress=start,
        _finish_account_health_progress=finish,
        _account_health_checkpoint_begin=Mock(),
        _update_account_health_progress=Mock(),
        _mark_account_health_persisted=Mock(),
        _account_health_worker_limit=lambda workers: workers,
        _current_health_proxy_pool=lambda: [],
    )
    return state, progress, finishes


def _run(state):
    record = {"email": "health@example.test", "health_status": "alive"}
    return trial_health._run_account_health_batch(
        state,
        records=[record], keys=["email:health@example.test"], workers=1,
        probe_records=[record], skipped_records=[],
    )


def test_cancelled_request_keeps_progress_and_execution_slot_until_worker_finishes(monkeypatch):
    state, progress, finishes = _state()
    started = threading.Event()
    release = threading.Event()
    result = {"checked": 1}

    def check(**_kwargs):
        started.set()
        assert release.wait(timeout=5)
        return result

    monkeypatch.setattr(health, "check_accounts", check)

    async def scenario():
        request = asyncio.create_task(_run(state))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert progress["running"] is True
            assert state._ACCOUNT_HEALTH_RUN_LOCK.locked()
            assert finishes == []
            with pytest.raises(HTTPException) as rejected:
                await _run(state)
            assert rejected.value.status_code == 409
        finally:
            workers = tuple(trial_health._HEALTH_REQUEST_TASKS)
            release.set()
            if workers:
                await asyncio.wait_for(asyncio.gather(*workers), timeout=3)
        assert progress["running"] is False
        assert not state._ACCOUNT_HEALTH_RUN_LOCK.locked()
        assert finishes == [{"result": result, "message": ""}]

    asyncio.run(scenario())


def test_worker_failure_finalizes_progress_and_releases_execution_slot(monkeypatch):
    state, progress, finishes = _state()
    monkeypatch.setattr(health, "check_accounts", Mock(side_effect=RuntimeError("probe failed")))

    async def scenario():
        with pytest.raises(RuntimeError, match="probe failed"):
            await _run(state)

    asyncio.run(scenario())
    assert progress["running"] is False
    assert not state._ACCOUNT_HEALTH_RUN_LOCK.locked()
    assert finishes == [{"result": None, "message": "probe failed"}]


def test_maintenance_uses_the_same_execution_slot_and_releases_it_on_failure(monkeypatch):
    import web_app

    shared_lock = threading.Lock()
    maintenance_lock = threading.Lock()
    monkeypatch.setattr(web_app, "_ACCOUNT_HEALTH_RUN_LOCK", shared_lock)
    monkeypatch.setattr(web_app, "_MAINTENANCE_RUN_LOCK", maintenance_lock)
    implementation = Mock()
    monkeypatch.setattr(web_app, "_run_account_maintenance_sync_impl", implementation)

    shared_lock.acquire()
    try:
        result = web_app._run_account_maintenance_sync(force=True)
    finally:
        shared_lock.release()
    assert result["reason"] == "health_check_already_running"
    implementation.assert_not_called()
    assert not maintenance_lock.locked()

    def fail(**_kwargs):
        assert shared_lock.locked()
        raise RuntimeError("maintenance failed")

    implementation.side_effect = fail
    with pytest.raises(RuntimeError, match="maintenance failed"):
        web_app._run_account_maintenance_sync(force=True)
    assert not shared_lock.locked()
    assert not maintenance_lock.locked()

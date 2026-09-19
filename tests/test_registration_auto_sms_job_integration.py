"""Inline SMS owns completion; the old batch follow-up must not run again."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from web.routes import codex_oauth, jobs


@pytest.fixture
def job_boundary(monkeypatch, tmp_path):
    state = SimpleNamespace(
        MAX_LOG_LINES=100, CONFIG_FILE=tmp_path / "no-config.yaml",
        load_config=lambda *_args: {"registration": {}},
        _maddy_blocked_domains=lambda *_args: [], _find_provider=lambda *_args: {},
        _LogStream=lambda original, *_args: original,
        _maintenance_config=lambda *_args: {}, _now_iso=lambda: "fixture-time",
    )
    monkeypatch.setattr(jobs, "_state", lambda: state)
    state.JOB = jobs.JobState()
    state.JOB.running = True
    state.JOB.total = 2
    state.JOB.params = {
        "batch_id": "registration-fixture", "auto_sms_after_register": True,
        "total": 2, "concurrency": 1,
    }
    state.JOB.batch_id = "registration-fixture"
    state.records = [{
        "email": "successful@example.test", "access_token": "synthetic-token",
        "registration_state": "complete",
    }]
    state.cards = {
        1: {"status": "success", "email": "successful@example.test", "batch_id": "registration-fixture"},
        2: {"status": "failed", "email": "failed@example.test", "batch_id": "registration-fixture"},
    }
    state.JOB.task_cards = state.cards
    state.lock_checks = []

    def enqueue(*_args, **_kwargs):
        acquired = state.JOB.lock.acquire(blocking=False)
        state.lock_checks.append(acquired)
        if acquired:
            state.JOB.lock.release()
        return {"status": "pending", "message": "fixture-followup-enqueued"}

    state.enqueue = Mock(side_effect=enqueue)
    monkeypatch.setattr(codex_oauth, "enqueue_registration_sms_followup", state.enqueue)
    return state


@pytest.mark.parametrize("enabled,stopped,crashed", [
    (True, False, False), (True, True, False),
    (False, False, False), (True, False, True),
])
def test_job_terminal_paths_never_enqueue_a_second_batch_followup(
    job_boundary, enabled, stopped, crashed,
):
    state = job_boundary
    state.JOB.params["auto_sms_after_register"] = enabled

    async def register(*_args):
        state.JOB.stop_requested = stopped
        if crashed:
            raise RuntimeError("fixture-registration-failure")
        return state.records

    state._run_register_async = register
    jobs._run_job_thread(dict(state.JOB.params))

    assert state.JOB.running is False
    assert state.JOB.finished_at == "fixture-time"
    state.enqueue.assert_not_called()
    if enabled:
        assert state.JOB.params["auto_sms_mode"] == "inline"
    if crashed:
        assert state.JOB.last_error == "fixture-registration-failure"


def test_job_snapshot_reads_followup_without_holding_job_lock(job_boundary, monkeypatch):
    state = job_boundary
    checks = []

    def snapshot(params, **kwargs):
        acquired = state.JOB.lock.acquire(blocking=False)
        checks.append(acquired)
        if acquired:
            state.JOB.lock.release()
        assert params["batch_id"] == "registration-fixture"
        assert kwargs == {"registration_running": True, "registration_error": ""}
        return {"status": "pending", "batch_id": "sms-fixture"}

    monkeypatch.setattr(codex_oauth, "registration_sms_snapshot", snapshot)
    result = state.JOB.snapshot()
    assert checks == [True]
    assert result["auto_sms"] == {"status": "pending", "batch_id": "sms-fixture"}


def test_job_snapshot_exposes_followup_read_failure_instead_of_completion(job_boundary, monkeypatch):
    monkeypatch.setattr(
        codex_oauth, "registration_sms_snapshot",
        Mock(side_effect=RuntimeError("fixture-database-failure")),
    )
    assert job_boundary.JOB.snapshot()["auto_sms"]["status"] == "failed"


def test_sms_failure_is_not_counted_as_task_success_or_registration_retry(job_boundary):
    state = job_boundary
    state.cards[1].update(status="failed", phase="sms", registration_status="success", sms_status="failed")

    async def register(*_args):
        return state.records  # The registered account still exists after SMS failure.

    state._run_register_async = register
    jobs._run_job_thread(dict(state.JOB.params))
    result = state.JOB.snapshot()
    assert result["success_count"] == 0
    assert result["fail_count"] == 2
    assert result["retryable_fail_count"] == 1
    assert result["auto_sms"]["failed"] == 1
    assert result["auto_sms"]["status"] == "failed"
    assert state.JOB.results == state.records


def test_retry_failed_excludes_registered_accounts_with_failed_sms(job_boundary, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    state = job_boundary
    state.JOB.running = False
    state.cards[1].update(status="failed", phase="sms", registration_status="success", sms_status="failed")
    start = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(jobs, "job_start", start)
    asyncio.run(jobs.job_retry_failed())
    assert start.call_args.args[0].total == 1


def test_only_sms_failures_cannot_start_registration_again(job_boundary, monkeypatch):
    import asyncio
    from fastapi import HTTPException
    from unittest.mock import AsyncMock

    state = job_boundary
    state.JOB.running = False
    state.JOB.task_cards = {1: {"status": "failed", "registration_status": "success", "sms_status": "failed"}}
    start = AsyncMock()
    monkeypatch.setattr(jobs, "job_start", start)
    with pytest.raises(HTTPException) as failure:
        asyncio.run(jobs.job_retry_failed())
    assert failure.value.status_code == 400
    assert "账号页" in failure.value.detail
    start.assert_not_called()

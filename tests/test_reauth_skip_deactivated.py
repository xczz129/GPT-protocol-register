"""Batch reauthentication skips only deactivated members, with no live login.

The real route, batch executor and account-store identity helpers operate on
an isolated temporary store. Authentication is replaced at the worker boundary.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import HTTPException
import pytest

import common.account_store as store
import web.routes.accounts_reauth as routes
import web.routes.jobs as job_routes
import web.routes.security_ops as security


def _account(index: int, *, deactivated: bool = False) -> dict:
    record = {
        "email": f"member-{index}@example.test",
        "user_id": f"member-user-{index}",
        "account_id": "shared-workspace",
        "plan_type": "team",
        "health_status": "reauth_required",
        "password": f"synthetic-saved-password-{index}-END",
        "totp_secret": f"synthetic-saved-totp-{index}-END",
    }
    if deactivated:
        record.update(status="已封号", reauth_blocked_reason="account_deactivated")
    return record


@pytest.fixture
def boundary(monkeypatch, tmp_path):
    """No production store, application bootstrap or authentication is used."""
    import requests

    monkeypatch.setattr(
        requests.sessions.Session,
        "request",
        Mock(side_effect=AssertionError("Live HTTP is forbidden in batch tests")),
    )
    account_path = tmp_path / "accounts.json"
    logs: list[str] = []
    login = Mock(side_effect=lambda record, **_kwargs: {**record, "health_status": "alive"})
    state = SimpleNamespace(
        ACCOUNTS_FILE=account_path,
        _ACCOUNT_REAUTH_MODES={"protocol", "browser"},
        _load_accounts=lambda: store.load_accounts(account_path),
        _records_by_keys_with_fallback=lambda keys: store.load_account_records_by_keys(
            keys, path=account_path
        ),
        _account_key=store.account_delete_key,
        account_public_view=store.account_public_view,
        account_is_deactivated=store.account_is_deactivated,
        normalized_health_status=store.normalized_health_status,
        _normalize_reauth_mode=security._normalize_reauth_mode,
        _account_requires_protocol_reauth=security._account_requires_protocol_reauth,
        _protocol_reauth_credentials_ready=security._protocol_reauth_credentials_ready,
        _normalize_account_security_operation=security._normalize_account_security_operation,
        _account_security_operation_label=security._account_security_operation_label,
        _normalize_security_password_mode=security._normalize_security_password_mode,
        _redact_security_detail=security._redact_security_detail,
        _account_reauth_resource_budget=lambda workers: (workers, "normal"),
        _now_iso=lambda: "2026-09-15T00:00:00Z",
        ThreadPoolExecutor=ThreadPoolExecutor,
        JOB=SimpleNamespace(append_log=logs.append),
        record_operation_event=Mock(),
        _reauthenticate_account_sync=login,
        _change_account_password_sync=Mock(),
        _change_account_totp_sync=Mock(),
        _repair_account_security_sync=Mock(),
    )
    monkeypatch.setattr(routes, "_state", lambda: state)
    monkeypatch.setattr(job_routes, "_state", lambda: state)
    monkeypatch.setattr(security, "_state", lambda: state)
    # Exercise actual current-store checks rather than the legacy unit-test bypass.
    monkeypatch.setattr(job_routes, "_TEST_RUNTIME", False)
    jobs = job_routes.AccountReauthBatchJobs()
    state.ACCOUNT_REAUTH_JOBS = jobs

    def seed(records):
        store.upsert_account_records(records, account_path)
        return deepcopy(records)

    def start(records, *, concurrency=3, keys=None):
        requested = keys if keys is not None else [store.account_delete_key(row) for row in records]
        return asyncio.run(
            routes.account_reauthenticate_start(
                routes.AccountReauthBatchRequest(
                    keys=requested, mode="protocol", concurrency=concurrency
                )
            )
        )

    def finish(started):
        assert jobs.wait(started["job_id"], timeout=5), "Batch worker failed to finish"
        result = jobs.snapshot(started["job_id"])
        assert result is not None
        return result

    yield SimpleNamespace(
        state=state,
        jobs=jobs,
        login=login,
        logs=logs,
        seed=seed,
        start=start,
        finish=finish,
        path=account_path,
    )
    for job_id in list(jobs.jobs):
        jobs.stop(job_id)
        assert jobs.wait(job_id, timeout=5), "Test left a batch worker running"


def _assert_counts(snapshot, *, total, success=0, failed=0, skipped=0, cancelled=0):
    expected = {
        "total": total,
        "done": success + failed + skipped + cancelled,
        "success": success,
        "failed": failed,
        "skipped": skipped,
        "cancelled": cancelled,
    }
    for field, value in expected.items():
        assert snapshot[field] == value, (field, snapshot[field], value)
    counts = Counter(row["status"] for row in snapshot["items"])
    for status in ("success", "failed", "skipped", "cancelled"):
        assert counts[status] == expected[status]


def _assert_no_credentials(boundary, records, *snapshots):
    serialized = json.dumps(snapshots, ensure_ascii=False) + "\n".join(boundary.logs)
    for record in records:
        for field in ("password", "totp_secret"):
            value = record.get(field)
            if value:
                assert value not in serialized


@pytest.mark.parametrize("banned_index", [0, 47, 94])
def test_95_selected_accounts_login_94_and_skip_one_in_any_position(boundary, banned_index):
    records = boundary.seed([
        _account(index, deactivated=index == banned_index) for index in range(95)
    ])
    original_store = boundary.path.read_bytes()
    started = boundary.start(records, concurrency=4)
    result = boundary.finish(started)

    assert result["status"] == "completed"
    assert result["mode"] == "protocol"
    _assert_counts(result, total=95, success=94, skipped=1)
    assert [row["email"] for row in result["items"]] == [row["email"] for row in records]
    assert result["items"][banned_index]["status"] == "skipped"
    expected = {row["email"] for index, row in enumerate(records) if index != banned_index}
    assert {call.args[0]["email"] for call in boundary.login.call_args_list} == expected
    assert boundary.login.call_count == 94
    assert all(call.kwargs["mode"] == "protocol" for call in boundary.login.call_args_list)
    assert boundary.path.read_bytes() == original_store
    _assert_no_credentials(boundary, records, started, result)


def test_banned_account_missing_credentials_is_skipped_before_credentials_check(boundary):
    banned = _account(0, deactivated=True)
    banned.pop("password")
    banned.pop("totp_secret")
    records = boundary.seed([banned, _account(1)])
    result = boundary.finish(boundary.start(records))
    _assert_counts(result, total=2, success=1, skipped=1)
    boundary.login.assert_called_once()
    assert boundary.login.call_args.args[0]["email"] == records[1]["email"]


def test_shared_workspace_members_remain_distinct_but_repeated_keys_are_deduplicated(boundary):
    records = boundary.seed([_account(0, deactivated=True), _account(1), _account(2)])
    keys = [store.account_delete_key(row) for row in records]
    assert len(set(keys)) == 3
    assert len({row["account_id"] for row in records}) == 1
    result = boundary.finish(boundary.start(records, keys=[*keys, keys[0], keys[1]]))
    _assert_counts(result, total=3, success=2, skipped=1)
    assert [row["key"] for row in result["items"]] == keys
    assert {call.args[0]["user_id"] for call in boundary.login.call_args_list} == {
        "member-user-1", "member-user-2"
    }


def test_all_banned_returns_completed_snapshot_without_threads_or_executor(boundary, monkeypatch):
    records = boundary.seed([_account(index, deactivated=True) for index in range(3)])
    thread_factory = Mock(side_effect=AssertionError("All-skipped batch created a thread"))
    executor_factory = Mock(side_effect=AssertionError("All-skipped batch created an executor"))
    monkeypatch.setattr(job_routes.threading, "Thread", thread_factory)
    boundary.state.ThreadPoolExecutor = executor_factory

    started = boundary.jobs.start(records, "protocol", "reauth", 10)
    result = boundary.jobs.snapshot(started["job_id"])
    assert result["status"] == "completed"
    assert not result["running"] and not result["stopping"]
    assert result["concurrency"] == 0
    _assert_counts(result, total=3, skipped=3)
    assert boundary.jobs.active_snapshot() == result
    assert boundary.jobs.wait(started["job_id"], timeout=0)
    thread_factory.assert_not_called()
    executor_factory.assert_not_called()
    boundary.login.assert_not_called()
    _assert_no_credentials(boundary, records, started, result)


@pytest.mark.parametrize("missing", ["password", "totp_secret"])
def test_non_banned_missing_credentials_still_rejects_before_job_creation(boundary, missing):
    records = [_account(0, deactivated=True), _account(1)]
    records[1].pop(missing)
    boundary.seed(records)
    with pytest.raises(HTTPException) as failure:
        boundary.start(records)
    assert failure.value.status_code == 409
    assert "TOTP" in failure.value.detail
    assert boundary.jobs.active_snapshot() is None
    boundary.login.assert_not_called()


def test_deleted_selection_still_reports_404_without_starting_job(boundary):
    records = boundary.seed([_account(0, deactivated=True)])
    with pytest.raises(HTTPException) as failure:
        boundary.start(records, keys=[store.account_delete_key(records[0]), "user:deleted-member"])
    assert failure.value.status_code == 404
    assert boundary.jobs.active_snapshot() is None
    boundary.login.assert_not_called()


def test_account_marked_banned_after_queueing_is_not_logged_in(boundary):
    records = boundary.seed([_account(0), _account(1), _account(2)])
    first_entered = threading.Event()
    release = threading.Event()

    def login(record, **_kwargs):
        if record["user_id"] == "member-user-0":
            first_entered.set()
            assert release.wait(timeout=5), "Test did not release first worker"
        return {**record, "health_status": "alive"}

    boundary.login.side_effect = login
    started = boundary.start(records, concurrency=1)
    try:
        assert first_entered.wait(timeout=3)
        # The queued snapshot remains healthy, but the real member is now banned.
        store.upsert_account_record(_account(1, deactivated=True), boundary.path)
    finally:
        release.set()
    result = boundary.finish(started)

    _assert_counts(result, total=3, success=2, skipped=1)
    assert result["items"][1]["status"] == "skipped"
    assert [call.args[0]["user_id"] for call in boundary.login.call_args_list] == [
        "member-user-0", "member-user-2"
    ]


def test_runtime_ban_and_ordinary_failure_do_not_stop_remaining_accounts(boundary):
    records = boundary.seed([_account(index) for index in range(4)])

    def login(record, **_kwargs):
        if record["user_id"] == "member-user-0":
            raise RuntimeError(f"account_deactivated: {record['password']} {record['totp_secret']}")
        if record["user_id"] == "member-user-1":
            raise RuntimeError(f"upstream timeout: {record['password']} {record['totp_secret']}")
        return {**record, "health_status": "alive"}

    boundary.login.side_effect = login
    started = boundary.start(records, concurrency=1)
    result = boundary.finish(started)
    assert result["status"] == "completed"
    _assert_counts(result, total=4, success=2, failed=1, skipped=1)
    assert [row["status"] for row in result["items"]] == ["skipped", "failed", "success", "success"]
    assert boundary.login.call_count == 4
    _assert_no_credentials(boundary, records, started, result)


def test_invalid_or_expired_token_is_not_mistaken_for_banned_account(boundary):
    records = [_account(0), _account(1)]
    records[0].update(health_status="invalid", health_error="token invalidated")
    records[1].update(health_status="reauth_required", health_error="session has expired")
    boundary.seed(records)
    result = boundary.finish(boundary.start(records))
    _assert_counts(result, total=2, success=2)
    assert boundary.login.call_count == 2


def test_stop_preserves_skipped_item_and_counts_every_selected_account_once(boundary):
    records = boundary.seed([_account(0, deactivated=True), *[_account(index) for index in range(1, 6)]])
    first_entered = threading.Event()
    release = threading.Event()

    def login(record, **kwargs):
        first_entered.set()
        assert release.wait(timeout=5), "Test did not release cancellation worker"
        assert kwargs["stop_check"]()
        raise RuntimeError("stop requested")

    boundary.login.side_effect = login
    started = boundary.start(records, concurrency=1)
    try:
        assert first_entered.wait(timeout=3)
        before_stop = boundary.jobs.snapshot(started["job_id"])
        assert before_stop["skipped"] == 1
        assert before_stop["done"] == 1
        stopping = boundary.jobs.stop(started["job_id"])
        assert stopping["status"] == "stopping"
    finally:
        release.set()
    result = boundary.finish(started)

    assert result["status"] == "stopped"
    _assert_counts(result, total=6, skipped=1, cancelled=5)
    assert result["items"][0]["status"] == "skipped"
    assert result["current_items"] == []
    assert boundary.jobs.stop(started["job_id"]) == result
    boundary.login.assert_called_once()
    _assert_no_credentials(boundary, records, started, stopping, result)


@pytest.mark.parametrize("operation, helper", [
    ("password_change", "_change_account_password_sync"),
    ("totp_change", "_change_account_totp_sync"),
    ("security_repair", "_repair_account_security_sync"),
])
def test_other_security_operations_keep_runtime_ban_as_failure(boundary, operation, helper):
    records = boundary.seed([_account(0)])
    worker = getattr(boundary.state, helper)
    worker.side_effect = RuntimeError("account_deactivated: provider rejected account")
    started = boundary.jobs.start(
        records, "protocol", operation, 1,
        {"password_mode": "custom", "new_password": "synthetic-replacement-password"},
    )
    result = boundary.finish(started)
    assert result["status"] == "completed"
    _assert_counts(result, total=1, failed=1)
    worker.assert_called_once()
    boundary.login.assert_not_called()


@pytest.mark.parametrize("operation", ["password_change", "totp_change", "security_repair"])
def test_other_security_preflights_still_reject_banned_accounts(boundary, operation):
    records = boundary.seed([_account(0, deactivated=True)])
    with pytest.raises(HTTPException) as failure:
        asyncio.run(routes._load_security_accounts(
            [store.account_delete_key(records[0])], operation=operation
        ))
    assert failure.value.status_code == 409
    assert boundary.jobs.active_snapshot() is None
    boundary.login.assert_not_called()

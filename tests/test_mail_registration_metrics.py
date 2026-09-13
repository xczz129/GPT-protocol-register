from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from common import mail_registration_metrics as metrics
from web.routes import ops


@pytest.fixture
def metrics_db(tmp_path, monkeypatch):
    path = tmp_path / "channel.sqlite3"
    monkeypatch.setenv("MAIL_REGISTRATION_METRICS_DB", str(path))
    return path


def _mailbox(domain="gmail.com", role="primary"):
    return {"provider": "smsbower_mail", "email": f"private-address@{domain}",
            "domain": domain, "_smsbower_pair_role": role, "api_key": "do-not-store-this-key"}


def test_empty_summary_has_explicit_missing_rates_and_timings(metrics_db):
    summary = metrics.get_summary()
    assert summary["ok"] is True
    assert summary["totals"]["attempted"] == 0
    assert summary["rates"]["success"] == {"numerator": 0, "denominator": 0, "value": None}
    assert summary["otp_wait"] == {
        "received_count": 0, "timeout_count": 0,
        "average_seconds": None, "min_seconds": None, "max_seconds": None,
    }
    assert summary["updated_at"] is None
    assert {row["domain"] for row in summary["by_domain"]} == {"gmail.com", "icloud.com"}
    assert {row["role"] for row in summary["by_role"]} == {"primary", "child"}


def test_outcomes_are_disjoint_and_rates_include_actual_in_progress_attempts(metrics_db):
    for index, outcome in enumerate((*metrics.OUTCOMES, None)):
        token = metrics.begin_attempt(_mailbox("gmail.com" if index % 2 == 0 else "icloud.com"))
        assert token
        if outcome:
            assert metrics.finish_attempt(token, outcome)
    summary = metrics.get_summary()
    assert summary["totals"] == {"attempted": 6, "success": 1, "already_registered": 1,
                                 "otp_timeout": 1, "other_failed": 1, "cancelled": 1, "in_progress": 1}
    assert summary["rates"]["already_registered"] == {"numerator": 1, "denominator": 6, "value": 1 / 6}
    assert sum(row["totals"]["attempted"] for row in summary["by_domain"]) == 6
    assert sum(row["totals"]["attempted"] for row in summary["by_role"]) == 6
    assert summary["updated_at"].endswith("Z")


def test_weighted_wait_average_counts_received_only_and_splits_branches(metrics_db):
    primary = metrics.begin_attempt(_mailbox(), attempt_id="pair-primary")
    child = metrics.begin_attempt(_mailbox("gmail.com", "child"), attempt_id="pair-child")
    icloud = metrics.begin_attempt(_mailbox("icloud.com"), attempt_id="icloud-primary")
    assert metrics.record_otp_wait(primary, "send-1", 10.0, received=True)
    assert metrics.record_otp_wait(primary, "send-2", 180.0, received=False)
    assert metrics.record_otp_wait(child, "send-1", 30.0, received=True)
    assert metrics.record_otp_wait(icloud, "send-1", 50.0, received=True)
    assert metrics.record_otp_wait(icloud, "send-2", 70.0, received=True)
    summary = metrics.get_summary()
    assert summary["otp_wait"] == {"received_count": 4, "timeout_count": 1,
                                   "average_seconds": 40.0, "min_seconds": 10.0, "max_seconds": 70.0}
    domains = {row["domain"]: row for row in summary["by_domain"]}
    assert domains["gmail.com"]["otp_wait"]["average_seconds"] == 20.0
    assert domains["icloud.com"]["otp_wait"]["average_seconds"] == 60.0
    roles = {row["role"]: row for row in summary["by_role"]}
    assert roles["primary"]["otp_wait"]["average_seconds"] == pytest.approx(130 / 3)
    assert roles["child"]["otp_wait"]["received_count"] == 1
    assert summary["totals"]["otp_timeout"] == 0  # a retry wait isn't a final account failure


def test_replays_do_not_duplicate_attempt_outcome_or_timing(metrics_db):
    mailbox = _mailbox()
    token = metrics.begin_attempt(mailbox)
    assert token == mailbox[metrics.ATTEMPT_FIELD]
    for _ in range(4):
        assert metrics.begin_attempt(mailbox) == token
        assert metrics.record_otp_wait(token, "same-send", 12.0, received=True)
        assert metrics.finish_attempt(token, "success")
    assert metrics.record_otp_wait(token, "same-send", 99.0, received=True)
    assert metrics.record_otp_wait(token, "same-send", 180.0, received=False) is False
    assert metrics.finish_attempt(token, "cancelled") is False
    assert metrics.begin_attempt(mailbox) == token  # a terminal attempt stays terminal
    summary = metrics.get_summary()
    assert summary["totals"]["attempted"] == summary["totals"]["success"] == 1
    assert summary["totals"]["in_progress"] == 0
    assert summary["otp_wait"]["received_count"] == 1
    assert summary["otp_wait"]["average_seconds"] == 12.0
    assert summary["otp_wait"]["timeout_count"] == 0


def test_concurrent_workers_and_repeated_callbacks_are_atomic(metrics_db):
    # Start from a missing database, including concurrent schema initialization.
    mailbox = _mailbox()

    def replay(_):
        token = metrics.begin_attempt(mailbox)
        return (token, metrics.record_otp_wait(token, "same-send", 17, received=True),
                metrics.finish_attempt(token, "success"))

    with ThreadPoolExecutor(max_workers=12) as executor:
        rows = list(executor.map(replay, range(48)))
    assert all(token and sample_ok and finish_ok for token, sample_ok, finish_ok in rows)
    assert len({token for token, _, _ in rows}) == 1

    def independent(index):
        token = metrics.begin_attempt(_mailbox(role="child"), attempt_id=f"separate-{index}")
        return (token, metrics.record_otp_wait(token, "same-send", index, received=True),
                metrics.finish_attempt(token, "success"))

    with ThreadPoolExecutor(max_workers=12) as executor:
        rows = list(executor.map(independent, range(24)))
    assert all(token and sample_ok and finish_ok for token, sample_ok, finish_ok in rows)
    summary = metrics.get_summary()
    assert summary["totals"]["attempted"] == summary["totals"]["success"] == 25
    assert summary["otp_wait"]["received_count"] == 25
    assert summary["otp_wait"]["average_seconds"] == pytest.approx((17 + sum(range(24))) / 25)


def test_restart_preserves_identity_outcomes_and_wait_samples(metrics_db):
    token = "stable-checkpoint-attempt"
    metrics.begin_attempt(_mailbox(), attempt_id=token)
    metrics.record_otp_wait(token, "first-send", 31, received=True)
    script = (
        "import json; from common import mail_registration_metrics as m; "
        "m.begin_attempt({'provider':'smsbower_mail','domain':'gmail.com'}, "
        "attempt_id='stable-checkpoint-attempt'); "
        "m.record_otp_wait('stable-checkpoint-attempt','first-send',99,received=True); "
        "m.finish_attempt('stable-checkpoint-attempt','already_registered'); "
        "print(json.dumps(m.get_summary()))"
    )
    completed = subprocess.run([sys.executable, "-c", script], cwd=metrics.ROOT,
                               env=os.environ.copy(), capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    summary = json.loads(completed.stdout)
    assert summary["totals"]["attempted"] == summary["totals"]["already_registered"] == 1
    assert summary["otp_wait"]["average_seconds"] == 31
    assert metrics.get_summary()["totals"]["already_registered"] == 1


def test_ids_are_hashed_bounded_and_no_mailbox_secrets_are_saved(metrics_db):
    mailbox = _mailbox()
    token = metrics.begin_attempt(mailbox, attempt_id="a-sensitive-replay-identity")
    metrics.record_otp_wait(token, "a-private-wait-id", 2, received=True)
    with sqlite3.connect(metrics_db) as connection:
        attempt_key, provider, domain, role = connection.execute(
            "SELECT attempt_key,provider,domain,role FROM mail_registration_attempts"
        ).fetchone()
        wait_key = connection.execute("SELECT wait_key FROM mail_registration_otp_waits").fetchone()[0]
    assert len(attempt_key) == len(wait_key) == 64
    assert (provider, domain, role) == ("smsbower_mail", "gmail.com", "primary")
    content = metrics_db.read_bytes()
    for sensitive in (mailbox["email"], mailbox["api_key"], token, "a-private-wait-id"):
        assert sensitive.encode() not in content
    assert metrics.begin_attempt(_mailbox(), attempt_id="x" * 513) is None
    assert metrics.begin_attempt(_mailbox(), attempt_id="") is None
    assert metrics.record_otp_wait(token, "x" * 513, 2, received=True) is False


def test_copied_parent_token_cannot_relabel_or_count_a_child(metrics_db):
    parent = _mailbox()
    token = metrics.begin_attempt(parent)
    child = {**parent, "_smsbower_pair_role": "child"}
    assert metrics.begin_attempt(child) is None
    child.pop(metrics.ATTEMPT_FIELD)
    assert metrics.begin_attempt(child) != token
    roles = {row["role"]: row for row in metrics.get_summary()["by_role"]}
    assert roles["primary"]["totals"]["attempted"] == 1
    assert roles["child"]["totals"]["attempted"] == 1


@pytest.mark.parametrize("elapsed", [-1, float("nan"), float("inf"), "invalid"])
def test_invalid_timings_are_ignored(metrics_db, elapsed):
    token = metrics.begin_attempt(_mailbox())
    assert metrics.record_otp_wait(token, "send-1", elapsed, received=True) is False
    assert metrics.get_summary()["otp_wait"]["received_count"] == 0


def test_timeout_only_data_has_no_received_timing_average(metrics_db):
    token = metrics.begin_attempt(_mailbox())
    metrics.record_otp_wait(token, "send-1", 180, received=False)
    metrics.finish_attempt(token, "otp_timeout")
    wait = metrics.get_summary()["otp_wait"]
    assert wait["timeout_count"] == 1
    assert wait["received_count"] == 0
    assert wait["average_seconds"] is wait["min_seconds"] is wait["max_seconds"] is None


def test_missing_attempts_and_non_smsbower_calls_create_no_counts(metrics_db):
    assert metrics.begin_attempt({"provider": "icloud_api", "email": "private@icloud.com"}) is None
    assert metrics.finish_attempt(None, "success") is False
    assert metrics.finish_attempt("unknown-id", "success") is False
    assert metrics.record_otp_wait("unknown-id", "send-1", 5, received=True) is False
    assert metrics.get_summary()["totals"]["attempted"] == 0


def test_persistence_errors_are_nonfatal_and_do_not_leak_error_text(metrics_db, monkeypatch, caplog):
    def unavailable():
        raise OSError("private-address@gmail.com password=secret storage failure")

    monkeypatch.setattr(metrics, "_connection", unavailable)
    monkeypatch.setattr(metrics, "_LAST_WARNING_AT", float("-inf"))
    assert metrics.begin_attempt(_mailbox()) is None
    assert metrics.finish_attempt("attempt", "success") is False
    assert metrics.record_otp_wait("attempt", "send", 3, received=True) is False
    summary = metrics.get_summary()
    assert summary["ok"] is False
    assert summary["error"] == "metrics_unavailable"
    assert len(caplog.records) == 1
    assert "OSError" in caplog.text
    assert "private-address" not in caplog.text
    assert "secret" not in caplog.text
    assert "secret" not in json.dumps(summary)


def test_ops_endpoint_returns_aggregate_and_rejects_arbitrary_provider(metrics_db):
    token = metrics.begin_attempt(_mailbox())
    metrics.finish_attempt(token, "already_registered")
    response = asyncio.run(ops.mail_registration_stats())
    assert response["totals"]["already_registered"] == 1
    assert "private-address" not in json.dumps(response)
    with pytest.raises(HTTPException) as error:
        asyncio.run(ops.mail_registration_stats(provider="not-a-provider"))
    assert error.value.status_code == 400


def test_ops_cached_response_refreshes_durable_mail_counts(metrics_db, monkeypatch, tmp_path):
    stale = json.dumps({"ok": True, "mail_registration": metrics.get_summary()})
    state = SimpleNamespace(
        _read_config=lambda: {},
        _file_stamp=lambda path: "unchanged",
        CONFIG_FILE=tmp_path / "config.yaml",
        account_store_revision=lambda path: 0,
        ACCOUNTS_FILE=tmp_path / "accounts.sqlite3",
        DATA_DIR=tmp_path,
        _cache_settings=lambda cfg: {},
        _cache_ttl=lambda cfg, name, fallback: fallback,
        _current_operations_metrics=lambda: {},
        PANEL_RESPONSE_CACHE=SimpleNamespace(
            configure=lambda settings: None,
            get_or_set_json=lambda *args: (stale, "hit"),
            status=lambda: {"backend": "test"},
        ),
    )
    monkeypatch.setattr(ops, "_state", lambda: state)
    token = metrics.begin_attempt(_mailbox())
    metrics.finish_attempt(token, "success")
    response = asyncio.run(ops.operations())
    payload = json.loads(response.body)
    assert response.headers["X-Panel-Cache"] == "HIT"
    assert payload["mail_registration"]["totals"]["success"] == 1


def test_stats_route_remains_behind_panel_authentication(metrics_db, monkeypatch):
    import web_app

    monkeypatch.setattr(web_app, "_auth_ok", lambda request: False)
    response = TestClient(web_app.app).get("/api/operations/mail-registration-stats")
    assert response.status_code == 401
    assert "totals" not in response.json()

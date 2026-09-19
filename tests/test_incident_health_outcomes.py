"""Offline regressions for inconclusive probes reported as successful checks."""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from common import account_health as health
from common import account_store as store


def _token():
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return ".".join((encode({"alg": "none"}), encode({
        "exp": 4102444800,
        "https://api.openai.com/auth": {
            "chatgpt_user_id": "probe-user", "chatgpt_account_id": "probe-account",
            "chatgpt_plan_type": "free",
        },
    }), "synthetic"))


def _record(**extra):
    return {
        "email": "probe@example.test", "user_id": "probe-user",
        "account_id": "probe-account", "access_token": _token(),
        "plan_type": "free", "status": "正常", "health_status": "alive",
        "health_checked_at": "2026-09-01T12:00:00Z", **extra,
    }


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    path = tmp_path / "accounts.json"
    monkeypatch.setattr(store, "ACCOUNTS_FILE", path)
    monkeypatch.setattr(store, "ACCOUNTS_DB_FILE", tmp_path / "accounts.sqlite3")
    monkeypatch.setattr(health, "record_operation_event", Mock())
    monkeypatch.setattr(health, "_session", Mock(side_effect=AssertionError("network forbidden")))
    monkeypatch.setattr(health, "refresh_oauth_tokens", Mock(side_effect=lambda record, **kwargs: dict(record)))
    monkeypatch.setattr(health.time, "sleep", Mock())
    return path


@pytest.mark.parametrize("previous", ["alive", "limited"])
@pytest.mark.parametrize("error", [
    RuntimeError("curl: (35) TLS connect error"),
    RuntimeError("health_http_503 /backend-api/me"),
    health.HealthEdgeBlockedError("health_edge_blocked_http_403 /backend-api/me"),
])
def test_one_route_failure_persists_unknown_and_preserves_last_verdict(isolated, monkeypatch, previous, error):
    original = store.upsert_account_record(_record(health_status=previous), isolated)
    probe = Mock(side_effect=error)
    monkeypatch.setattr(health, "fetch_remote_account_info", probe)

    result = health.check_account(original, path=isolated, proxy_candidates=["http://one.example.test:8080"])

    saved = store.load_accounts(isolated)[0]
    for item in (result, saved):
        assert item["health_status"] == "unknown"
        assert item["status"] == "待复测"
        assert item["health_probe_error"] == str(error)
        assert item["health_error"] == ""
        assert item["health_last_confirmed_status"] == previous
        assert item["health_last_confirmed_at"] == original["health_checked_at"]
        assert item["health_checked_at"] != original["health_checked_at"]
        assert not store.account_is_deactivated(item)
    probe.assert_called_once()
    health.time.sleep.assert_not_called()
    event = health.record_operation_event.call_args
    assert event.args[2] == "failed"
    assert event.kwargs["metadata"]["health_status"] == "unknown"
    assert "待复测" in event.kwargs["message"]


def test_exhausted_routes_are_not_counted_as_usable(isolated, monkeypatch):
    original = store.upsert_account_record(_record(), isolated)
    probe = Mock(side_effect=RuntimeError("curl: (35) TLS connect error"))
    monkeypatch.setattr(health, "fetch_remote_account_info", probe)

    result = health.check_accounts(
        [original], path=isolated, workers=1,
        proxy_candidates=["http://one.example.test:8080", "http://two.example.test:8080"],
    )

    assert probe.call_count == 2
    assert result["checked"] == result["unknown"] == result["unavailable"] == 1
    assert result["usable"] == result["alive"] == result["limited"] == result["error"] == 0
    assert result["items"][0]["health_status"] == "unknown"
    assert store.load_accounts(isolated)[0]["health_status"] == "unknown"
    assert health.record_operation_event.call_args.args[2] == "failed"


def test_repeated_unknown_probe_retains_confirmed_history(isolated, monkeypatch):
    original = store.upsert_account_record(_record(
        health_status="unknown", health_probe_error="previous timeout",
        health_last_confirmed_status="limited", health_last_confirmed_at="2026-08-30T00:00:00Z",
    ), isolated)
    monkeypatch.setattr(health, "fetch_remote_account_info", Mock(side_effect=health.HealthTransientError("upstream temporary")))

    result = health.check_account(original, path=isolated, proxy_candidates=["http://one.example.test:8080"])

    assert result["health_last_confirmed_status"] == "limited"
    assert result["health_last_confirmed_at"] == "2026-08-30T00:00:00Z"


def test_success_clears_stale_errors_and_records_new_confirmation(isolated, monkeypatch):
    original = store.upsert_account_record(_record(
        health_status="unknown", status="待复测", health_probe_error="old timeout",
        health_error="old probe error", last_token_refresh_error="old refresh failure",
        health_last_confirmed_status="limited", health_last_confirmed_at="2026-08-30T00:00:00Z",
    ), isolated)
    monkeypatch.setattr(health, "fetch_remote_account_info", Mock(return_value={
        "health_status": "alive", "status": "正常", "health_checked_at": "2026-09-18T01:00:00Z",
    }))

    result = health.check_account(original, path=isolated, proxy_candidates=["http://one.example.test:8080"])

    for item in (result, store.load_accounts(isolated)[0]):
        assert item["health_status"] == "alive"
        assert item["health_error"] == item["health_probe_error"] == item["last_token_refresh_error"] == ""
        assert item["health_last_confirmed_status"] == "alive"
        assert item["health_last_confirmed_at"] == "2026-09-18T01:00:00Z"
    assert health.record_operation_event.call_args.args[2] == "success"
    assert not health.record_operation_event.call_args.kwargs["error"]


def test_401_preserves_only_business_code_and_is_not_deactivation(isolated, monkeypatch):
    response = SimpleNamespace(status_code=401, json=lambda: {"error": {
        "code": "token_expired", "message": "private credential synthetic-secret",
    }})

    def rejected(_record):
        return health._response_json(response, "/backend-api/me")

    monkeypatch.setattr(health, "fetch_remote_account_info", Mock(side_effect=rejected))
    original = store.upsert_account_record(_record(), isolated)
    result = health.check_account(original, path=isolated, proxy_candidates=["http://one.example.test:8080"])

    assert result["health_status"] == "invalid"
    assert "error_code=token_expired" in result["health_error"]
    assert "synthetic-secret" not in result["health_error"]
    assert "missing renewable credential" not in result["health_error"]
    assert result["status"] != "已封号"
    assert not store.account_is_deactivated(result)


def test_401_explicit_deactivation_remains_terminal():
    response = SimpleNamespace(status_code=401, json=lambda: {"error": {"code": "account_deactivated"}})
    with pytest.raises(health.AccountDeactivatedError):
        health._response_json(response, "/backend-api/me")


def test_401_does_not_copy_unstructured_code():
    response = SimpleNamespace(status_code=401, json=lambda: {"error": {"code": "token expired: private-data"}})
    with pytest.raises(health.InvalidAccessTokenError) as caught:
        health._response_json(response, "/backend-api/me")
    assert "private-data" not in str(caught.value)


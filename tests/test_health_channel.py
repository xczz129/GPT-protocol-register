"""Account-health channel regression coverage without external network calls."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from common import account_health as health
from web.routes import trial_health


class _Response:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


class _Session:
    def __init__(self, *, me_status: int = 200, init_payload=None):
        self.headers = {}
        self.me_status = me_status
        self.init_payload = init_payload if init_payload is not None else {
            "default_model_slug": "auto",
            "limits_progress": [{"feature_name": "image_gen", "remaining": 2}],
        }
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    def get(self, url: str, **_kwargs):
        self.calls.append(("GET", url))
        if url.endswith("/backend-api/me"):
            return _Response(
                self.me_status,
                {"email": "health@example.test", "id": "user-health"},
            )
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url: str, **_kwargs):
        self.calls.append(("POST", url))
        if url.endswith("/backend-api/conversation/init"):
            return _Response(
                200,
                self.init_payload,
            )
        raise AssertionError(f"unexpected POST {url}")

    def close(self):
        self.closed = True


def _record():
    return {
        "email": "health@example.test",
        "user_id": "user-health",
        "account_id": "account-health",
        "access_token": "synthetic.access.token",
        "plan_type": "free",
    }


@pytest.mark.parametrize("channel", ["v4", "nerver"])
def test_health_checks_only_official_availability_without_trial_calls(monkeypatch, channel):
    session = _Session()
    nerver = Mock(side_effect=AssertionError("health must not call trial API"))
    trial = Mock(side_effect=AssertionError("health must not call trial API"))
    monkeypatch.setattr(health, "_session", Mock(return_value=session))
    monkeypatch.setattr(
        "common.trial_eligibility.check_trial_eligibility_nerver", nerver,
    )
    monkeypatch.setattr("common.trial_eligibility.check_trial_eligibility", trial)

    record = {**_record(), "trial_eligibility_status": "ineligible"}
    result = health.fetch_remote_account_info(record, channel=channel)

    assert result["health_status"] == "alive"
    assert result["health_probe_channel"] == channel
    assert result["plan_type"] == "free"
    assert result["account_id"] == record["account_id"]
    assert not any(field.startswith("trial_") for field in result)
    assert len(session.calls) == 2
    assert any(url.endswith("/backend-api/me") for _method, url in session.calls)
    assert any(
        url.endswith("/backend-api/conversation/init")
        for _method, url in session.calls
    )
    assert not any("/backend-api/accounts/check/" in url for _method, url in session.calls)
    nerver.assert_not_called()
    trial.assert_not_called()
    assert session.closed is True


@pytest.mark.parametrize("channel", ["v4", "nerver"])
def test_official_token_rejection_remains_invalid_for_legacy_channels(monkeypatch, channel):
    session = _Session(me_status=401)
    nerver = Mock(return_value={"plan_type": "plus", "eligible": True})
    monkeypatch.setattr(health, "_session", Mock(return_value=session))
    monkeypatch.setattr(
        "common.trial_eligibility.check_trial_eligibility_nerver", nerver,
    )

    with pytest.raises(health.InvalidAccessTokenError):
        health.fetch_remote_account_info(_record(), channel=channel)

    assert any(
        url.endswith("/backend-api/conversation/init")
        for _method, url in session.calls
    )
    nerver.assert_not_called()
    assert session.closed is True


@pytest.mark.parametrize("limits", [
    [], [{"feature_name": "image_gen"}],
    [{"feature_name": "image_gen", "remaining": None}],
    [{"feature_name": "image_gen", "remaining": "unknown"}],
    [{"feature_name": "image_gen", "remaining": False}],
])
def test_missing_or_unknown_image_quota_does_not_mark_valid_account_limited(monkeypatch, limits):
    session = _Session(init_payload={"limits_progress": limits})
    monkeypatch.setattr(health, "_session", Mock(return_value=session))

    result = health.fetch_remote_account_info(_record())

    assert result["health_status"] == "alive"
    assert result["status"] == "正常"
    assert result["plan_type"] == "free"
    assert result["image_quota_unknown"] is True
    assert result["health_probe_error"] == ""


def test_explicit_exhausted_image_quota_remains_limited(monkeypatch):
    session = _Session(init_payload={
        "limits_progress": [{"feature_name": "image_gen", "remaining": 0}],
    })
    monkeypatch.setattr(health, "_session", Mock(return_value=session))

    result = health.fetch_remote_account_info(_record())

    assert result["health_status"] == "limited"
    assert result["image_quota_unknown"] is False


@pytest.mark.parametrize("metadata", [
    {},
    {"account_id": "workspace-team", "plan_type": "self_serve_business_prolite", "type": "team"},
])
def test_health_preserves_saved_workspace_and_plan_without_defaulting(monkeypatch, metadata):
    session = _Session()
    monkeypatch.setattr(health, "_session", Mock(return_value=session))
    record = {key: value for key, value in _record().items() if key not in {"account_id", "plan_type"}}
    record.update(metadata)

    result = health.fetch_remote_account_info(record)

    assert {key: result[key] for key in ("account_id", "plan_type", "type") if key in result} == metadata
    assert result["health_status"] == "alive"


@pytest.mark.parametrize("endpoint", ["me", "init"])
@pytest.mark.parametrize("payload", ["html", None, [], "not an object"])
def test_success_status_with_invalid_json_cannot_report_alive(monkeypatch, endpoint, payload):
    session = _Session()
    response = _Response(200, payload)
    if payload == "html":
        response.text = "<html>upstream error</html>"
        response.json = Mock(side_effect=ValueError("invalid JSON"))
    if endpoint == "me":
        session.get = Mock(return_value=response)
    else:
        session.post = Mock(return_value=response)
    monkeypatch.setattr(health, "_session", Mock(return_value=session))

    with pytest.raises(RuntimeError, match="health_invalid_json"):
        health.fetch_remote_account_info(_record())

    assert session.closed is True


def test_check_accounts_passes_normalized_channel_to_each_account(monkeypatch):
    checked = Mock(
        side_effect=lambda record, **kwargs: {
            **record,
            "health_status": "alive",
            "health_probe_channel": kwargs.get("channel", "v4"),
        }
    )
    monkeypatch.setattr(health, "check_account", checked)
    monkeypatch.setattr(
        health, "_persist_health_results", lambda pairs, _path: [row for _old, row in pairs],
    )

    result = health.check_accounts(
        records=[_record()], workers=1, channel="NERVER",
    )

    assert result["channel"] == "nerver"
    assert result["items"][0]["health_probe_channel"] == "nerver"
    assert checked.call_args.kwargs["channel"] == "nerver"


def test_route_worker_passes_channel_to_health_core(monkeypatch):
    from common import account_health

    check = Mock(return_value={"checked": 1, "channel": "nerver"})
    monkeypatch.setattr(account_health, "check_accounts", check)
    state = SimpleNamespace(
        _update_account_health_progress=Mock(),
        _mark_account_health_persisted=Mock(),
        _account_health_worker_limit=lambda workers: workers,
        _current_health_proxy_pool=lambda: [],
        _finish_account_health_progress=Mock(),
        _ACCOUNT_HEALTH_RUN_LOCK=Mock(release=Mock()),
    )

    result = trial_health._run_account_health_worker(
        state,
        records=[_record()],
        keys=["email:health@example.test"],
        workers=3,
        channel="nerver",
    )

    assert result["channel"] == "nerver"
    assert check.call_args.kwargs["channel"] == "nerver"
    state._finish_account_health_progress.assert_called_once()
    state._ACCOUNT_HEALTH_RUN_LOCK.release.assert_called_once()

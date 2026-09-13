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
    def __init__(self, *, me_status: int = 200):
        self.headers = {}
        self.me_status = me_status
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
                {
                    "default_model_slug": "auto",
                    "limits_progress": [
                        {"feature_name": "image_gen", "remaining": 2}
                    ],
                },
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


def test_nerver_channel_still_requires_chatgpt_me_and_init(monkeypatch):
    session = _Session()
    nerver = Mock(
        return_value={
            "plan_type": "plus",
            "eligible": False,
            "has_active_subscription": True,
        }
    )
    monkeypatch.setattr(health, "_session", Mock(return_value=session))
    monkeypatch.setattr(
        "common.trial_eligibility.check_trial_eligibility_nerver", nerver,
    )

    result = health.fetch_remote_account_info(_record(), channel="nerver")

    assert result["health_status"] == "alive"
    assert result["health_probe_channel"] == "nerver"
    assert result["plan_type"] == "plus"
    assert any(url.endswith("/backend-api/me") for _method, url in session.calls)
    assert any(
        url.endswith("/backend-api/conversation/init")
        for _method, url in session.calls
    )
    assert not any("/backend-api/accounts/check/" in url for _method, url in session.calls)
    nerver.assert_called_once_with("synthetic.access.token", timeout=8)
    assert session.closed is True


def test_nerver_success_cannot_mask_chatgpt_token_rejection(monkeypatch):
    session = _Session(me_status=401)
    nerver = Mock(return_value={"plan_type": "plus", "eligible": True})
    monkeypatch.setattr(health, "_session", Mock(return_value=session))
    monkeypatch.setattr(
        "common.trial_eligibility.check_trial_eligibility_nerver", nerver,
    )

    with pytest.raises(health.InvalidAccessTokenError):
        health.fetch_remote_account_info(_record(), channel="nerver")

    assert any(
        url.endswith("/backend-api/conversation/init")
        for _method, url in session.calls
    )
    nerver.assert_called_once()
    assert session.closed is True


def test_nerver_plan_failure_does_not_turn_valid_chatgpt_session_dead(monkeypatch):
    session = _Session()
    monkeypatch.setattr(health, "_session", Mock(return_value=session))
    monkeypatch.setattr(
        "common.trial_eligibility.check_trial_eligibility_nerver",
        Mock(side_effect=RuntimeError("nerver unavailable")),
    )

    result = health.fetch_remote_account_info(_record(), channel="nerver")

    assert result["health_status"] == "alive"
    assert result["plan_type"] == "free"
    assert result["health_probe_error"] == "nerver unavailable"


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

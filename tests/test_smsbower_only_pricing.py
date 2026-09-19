"""Removed suppliers fail closed; prices and concurrency use real input data."""
import asyncio
from contextlib import contextmanager
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from codex_oauth import import_jobs, quota, sms
from common import registration_sms_followup
from web.routes import codex_oauth as route


@pytest.mark.parametrize("provider", ["firefox", "firefox.fun", "firefox_sms", "huohuli", "火狐狸", "unknown"])
def test_removed_provider_is_unsupported_and_cannot_send_legacy_secrets(monkeypatch, provider):
    network = Mock(side_effect=AssertionError("network must not be used"))
    monkeypatch.setattr(sms, "urlopen", network)
    settings = sms.SmsSettings.from_mapping({"enabled": True, "provider": provider, "api_key": "old-private-key", "token": "old-private-token", "firefox_api_token": "old-private-token"})
    assert settings.api_key == "" and not settings.enabled and not settings.credentials_configured
    public = settings.public_mapping()
    assert public["state"] == "unsupported" and "已移除" in public["state_label"]
    assert "private" not in json.dumps(settings.as_mapping())
    for callback in [lambda: sms.SmsBowerClient(settings), lambda: sms.check_sms_balance(settings), lambda: import_jobs._sms_client_for(settings, 5), lambda: route._sms_client(settings)]:
        with pytest.raises(sms.SmsError, match="仅支持 SMSBower"):
            callback()
    network.assert_not_called()


def test_removed_provider_is_rejected_by_public_execution_and_config_schemas():
    with pytest.raises(ValidationError):
        route.SmsConfigUpdate(provider="firefox")
    with pytest.raises(ValidationError):
        route.SmsRunOptions(provider="firefox")
    with pytest.raises(route.HTTPException, match="已移除"):
        route._effective_sms_config({"sms_code": {"provider": "smsbower", "api_key": "fixture"}}, {"provider": "firefox"})


@pytest.mark.parametrize("provider", [None, "smsbower"])
def test_generic_legacy_token_is_never_used_as_smsbower_key(monkeypatch, provider):
    raw = {"enabled": True, "token": "ambiguous-private-token", "firefox_api_token": "ambiguous-private-token"}
    if provider is not None:
        raw["provider"] = provider
    settings = sms.SmsSettings.from_mapping(raw)
    assert settings.api_key == "" and not settings.credentials_configured
    network = Mock(side_effect=AssertionError("network must not be used"))
    monkeypatch.setattr(sms, "urlopen", network)
    with pytest.raises(sms.SmsError, match="未配置"):
        sms.check_sms_balance(settings)
    assert import_jobs._sms_client_for(settings, 5) is None
    network.assert_not_called()


def _boundary(monkeypatch, config):
    @contextmanager
    def edit():
        yield config
    state = SimpleNamespace(_read_config=lambda: config, _edit_config=edit, _SERVER_SHUTDOWN_EVENT=threading.Event())
    monkeypatch.setattr(route, "_state", lambda: state)
    monkeypatch.setattr(route, "_SMS_COUNTRIES_CACHE", {"at": 0, "payload": None, "version": 2})
    return state


def test_legacy_config_is_readable_but_requires_new_smsbower_key(monkeypatch):
    config = {"sms_code": {"enabled": True, "provider": "firefox", "api_key": "ambiguous-key", "token": "old-token", "firefox_api_password": "old-password"}}
    _boundary(monkeypatch, config)
    public = asyncio.run(route.sms_config_get())
    assert public["state"] == "unsupported" and not public["enabled"]
    assert "old-token" not in str(public) and "ambiguous-key" not in str(public)
    with pytest.raises(route.HTTPException, match="新的 SMSBower API Key"):
        asyncio.run(route.sms_config_update(route.SmsConfigUpdate(enabled=True)))
    asyncio.run(route.sms_config_update(route.SmsConfigUpdate(enabled=True, api_key="new-smsbower-key")))
    assert config["sms_code"]["provider"] == "smsbower"
    assert config["sms_code"]["api_key"] == "new-smsbower-key"
    assert "token" not in config["sms_code"]
    assert not any(key.startswith("firefox_") for key in config["sms_code"])


@pytest.mark.parametrize("request_provider,saved_provider", [("firefox", "smsbower"), ("smsbower", "firefox"), ("unknown", "smsbower")])
def test_countries_reject_removed_provider_before_any_http(monkeypatch, request_provider, saved_provider):
    _boundary(monkeypatch, {"sms_code": {"provider": saved_provider, "api_key": "fixture"}})
    network = Mock(side_effect=AssertionError("network must not be used"))
    monkeypatch.setattr(route, "_smsbower_request", network)
    with pytest.raises(route.HTTPException, match="已移除"):
        asyncio.run(route.sms_countries(request_provider))
    network.assert_not_called()


def test_country_minimum_uses_positive_in_stock_tiers_and_safe_fallback(monkeypatch):
    _boundary(monkeypatch, {"sms_code": {"provider": "smsbower", "api_key": "fixture"}})
    countries = {str(i): {"id": i, "eng": f"Country {i}"} for i in range(1, 8)}
    prices = {str(i): {"dr": {"count": 20, "cost": "9.99"}} for i in range(1, 8)}
    prices["4"]["dr"]["count"] = 0
    prices["5"]["dr"]["cost"] = "NaN"
    prices["6"]["dr"]["cost"] = "0"
    tiers = {
        "1": {"dr": {"zero": {"price": 0, "count": 100}, "nan": {"price": "NaN", "count": 4}, "missing": {"count": 4}, "sold": {"price": "0.01", "count": 0}, "high": {"price": "10", "count": 8}, "low": {"price": "2", "count": 2}}},
        "2": {"dr": {"sold": {"price": "0.02", "count": 0}}},
        "7": {"dr": {"negative": {"price": "-0.5", "count": 7}, "inf": {"price": "inf", "count": 8}}},
    }
    responses = {"getCountries": countries, "getPrices": prices, "getPricesV3": tiers}
    monkeypatch.setattr(route, "_smsbower_request", lambda _key, action, **_kwargs: json.dumps(responses[action]))
    result = {item["id"]: item for item in asyncio.run(route.sms_countries())["items"]}
    assert result["1"]["min_price"] == "2"  # Numeric order, no 0/NaN/sold-out minimum.
    assert result["2"]["min_price"] is None  # Known tiers sold out: do not show summary cost.
    assert result["3"]["min_price"] == "9.99"  # No tiers: valid in-stock summary fallback.
    assert all(result[key]["min_price"] is None for key in ("4", "5", "6"))
    assert result["7"]["min_price"] == "9.99"


def test_sms_concurrency_is_positive_without_fixed_ceiling():
    assert route.CodexOauthRunRequest(emails=["fixture@test"], concurrency=1250).concurrency == 1250
    with pytest.raises(ValidationError):
        route.CodexOauthRunRequest(emails=["fixture@test"], concurrency=0)
    assert import_jobs.ImportJobManager.normalize_concurrency(1250) == 1250
    with pytest.raises(ValueError):
        import_jobs.ImportJobManager.normalize_concurrency(0)


def test_import_settings_preserve_concurrency_above_old_limit(tmp_path, monkeypatch):
    if not hasattr(quota.os, "fchmod"):
        monkeypatch.setattr(quota.os, "fchmod", lambda *_args: None, raising=False)
    path = tmp_path / "quota.json"
    path.write_text(json.dumps({"import_concurrency": 83}), encoding="utf-8")
    config = quota.load_config(path)
    assert config.import_concurrency == 83
    assert quota.save_import_concurrency(config, 97) == 97
    assert quota.load_config(path).import_concurrency == 97
    with pytest.raises(quota.QuotaError):
        quota.save_import_concurrency(config, 0)


def test_existing_queue_can_grow_past_32_workers_without_restarting_jobs(tmp_path):
    manager = import_jobs.ImportJobManager(data_dir=tmp_path, concurrency=1)
    release, all_started = threading.Event(), threading.Event()
    counter_lock = threading.Lock()
    started = []
    rows = [import_jobs.ImportJob(id=str(i), account_id=str(i), email=f"fixture{i}@test", batch_id="batch") for i in range(33)]
    manager._jobs = {row.id: row for row in rows}

    def worker(row):
        manager._acquire_worker_slot(row)
        try:
            with counter_lock:
                started.append(row.id)
                if len(started) == 33:
                    all_started.set()
            release.wait(5)
        finally:
            manager._release_worker_slot()

    try:
        futures = [manager._executor.submit(worker, row) for row in rows]
        manager.set_concurrency(40)
        assert all_started.wait(4), f"Only {len(started)} workers started"
        assert len(set(started)) == 33
        manager.set_concurrency(1)
        assert manager.concurrency == 1
    finally:
        release.set()
        manager._executor.shutdown(wait=True)
    assert all(future.exception() is None for future in futures)


def test_auto_sms_snapshots_ops_concurrency_not_registration_concurrency(monkeypatch, tmp_path):
    config = {"sms_code": {"enabled": True, "provider": "smsbower", "api_key": "fixture"}, "ui_settings": {"codex_sms_workers": 79}}
    _boundary(monkeypatch, config)
    queue = registration_sms_followup.RegistrationSmsQueue(tmp_path / "followups.sqlite3")
    monkeypatch.setattr(registration_sms_followup, "_QUEUE", queue)
    calls = []

    def start(wanted, **kwargs):
        calls.append(kwargs)
        queue.started(kwargs["source_registration_batch_id"], "sms-fixture")
        return {"batch_id": "sms-fixture"}

    monkeypatch.setattr(route, "_start_sms_batch", start)
    route.enqueue_registration_sms_followup({"auto_sms_after_register": True, "batch_id": "source", "concurrency": 3}, [{"email": "ok@test", "access_token": "fixture"}], [])
    assert calls[0]["concurrency"] == 79
    assert "concurrency" not in calls[0]["sms_options"]

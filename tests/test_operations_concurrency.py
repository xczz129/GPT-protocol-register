"""Operation concurrency is a positive operator choice, bounded only by work size."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor as RealThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from web.routes import accounts_reauth, jobs, ops, refresh_at, trial_health


WORKER_FIELDS = [
    "health_workers", "reauth_workers", "trial_workers", "refresh_at_workers", "codex_sms_workers",
]
REQUESTS = [
    *[(ops.UiSettingsUpdate, field, {}) for field in WORKER_FIELDS],
    (ops.MaintenanceUpdate, "workers", {}),
    (trial_health.AccountHealthRequest, "workers", {}),
    (trial_health.AccountHealthResumeRequest, "workers", {}),
    (trial_health.AccountTrialEligibilityRequest, "concurrency", {}),
    (trial_health.TrialEligibilityResumeRequest, "concurrency", {}),
    (trial_health.TokenTrialEligibilityBatchRequest, "workers", {}),
    (refresh_at.AccountRefreshAtRequest, "concurrency", {}),
    (accounts_reauth.AccountReauthBatchRequest, "concurrency", {"keys": ["fixture"]}),
    (accounts_reauth.AccountEmailReauthRequest, "concurrency", {}),
]


@pytest.mark.parametrize("model,field,extra", REQUESTS)
def test_requests_accept_positive_concurrency_above_previous_limits(model, field, extra):
    assert getattr(model(**extra, **{field: 137}), field) == 137


@pytest.mark.parametrize("model,field,extra", REQUESTS)
@pytest.mark.parametrize("value", [0, -1, 1.5])
def test_requests_reject_non_positive_or_fractional_concurrency(model, field, extra, value):
    with pytest.raises(ValidationError):
        model(**extra, **{field: value})


@pytest.fixture
def ui_settings_client(monkeypatch):
    config = {"ui_settings": {"reauth_mode": "protocol"}}

    @contextmanager
    def edit():
        yield config

    monkeypatch.setattr(ops, "_state", lambda: SimpleNamespace(
        _read_config=lambda: config, _edit_config=edit,
    ))
    app = FastAPI()
    app.include_router(ops.router)
    with TestClient(app) as client:
        yield client, config


def test_all_five_defaults_roundtrip_without_clamping_and_keep_reauth_mode(ui_settings_client):
    client, config = ui_settings_client
    requested = {field: 101 + index for index, field in enumerate(WORKER_FIELDS)}
    requested.update(reauth_mode="browser", trial_channel="nerver")
    assert client.put("/api/ui-settings", json=requested).status_code == 200
    loaded = client.get("/api/ui-settings").json()
    for field, value in requested.items():
        assert config["ui_settings"][field] == loaded[field] == value


@pytest.mark.parametrize("field", WORKER_FIELDS)
@pytest.mark.parametrize("value", [0, -1])
def test_invalid_ui_setting_is_rejected_without_config_write(ui_settings_client, field, value):
    client, config = ui_settings_client
    before = deepcopy(config)
    response = client.put("/api/ui-settings", json={field: value})
    assert response.status_code == 422
    assert config == before


def _records(count=37):
    return [{
        "email": f"member-{index}@example.test", "account_id": f"account-{index}",
        "access_token": "synthetic-access", "session_token": "synthetic-session",
        "plan_type": "free", "password": "synthetic-password", "totp_secret": "synthetic-totp",
    } for index in range(count)]


def _record_executor(monkeypatch, module):
    counts = []

    def executor(*, max_workers, **kwargs):
        counts.append(max_workers)
        return RealThreadPoolExecutor(max_workers=max_workers, **kwargs)

    monkeypatch.setattr(module, "ThreadPoolExecutor", executor)
    return counts


@pytest.mark.parametrize("requested", [31, 100])
@pytest.mark.parametrize("kind", ["refresh", "trial"])
def test_job_managers_pass_requested_workers_to_real_executor(monkeypatch, requested, kind):
    from common import session_refresh, trial_eligibility_jobs

    records = _records()
    if kind == "refresh":
        module = session_refresh
        manager = module.SessionRefreshJobs()
        probe = Mock(side_effect=module.SessionRefreshError("synthetic-upstream-unavailable"))
        monkeypatch.setattr(module, "refresh_session_via_service", probe)
        extra = {}
    else:
        module = trial_eligibility_jobs
        manager = module.TrialEligibilityJobs()
        probe = Mock(return_value={"eligibility_status": "eligible"})
        monkeypatch.setattr(module, "check_trial_eligibility_nerver", probe)
        extra = {"channel": "nerver"}
    monkeypatch.setattr(module, "update_account_record_fields", Mock(return_value={}))
    pools = _record_executor(monkeypatch, module)

    job = manager.start(records, concurrency=requested, **extra)
    assert manager.wait(job["job_id"], 5)
    snapshot = manager.snapshot(job["job_id"])
    assert snapshot["concurrency"] == requested
    assert snapshot["done"] == len(records)
    assert pools == [min(requested, len(records))]
    assert probe.call_count == len(records)


@pytest.mark.parametrize("requested", [31, 100])
def test_health_executor_and_resource_budget_have_no_fixed_worker_cap(monkeypatch, tmp_path, requested):
    import web_app
    from common import account_health

    records = _records()
    probe = Mock(side_effect=lambda record, **_kwargs: {**record, "health_status": "alive"})
    monkeypatch.setattr(account_health, "check_account", probe)
    monkeypatch.setattr(account_health, "_persist_health_results", lambda batch, _path: [result for _, result in batch])
    pools = _record_executor(monkeypatch, account_health)
    result = account_health.check_accounts(
        records, workers=requested, path=tmp_path / "accounts.json",
        worker_limit=web_app._account_health_worker_limit,
    )
    assert web_app._maintenance_config({"maintenance": {"workers": requested}})["workers"] == requested
    assert web_app._account_health_resource_budget(requested)[0] == requested
    assert pools == [min(requested, len(records))]
    assert result["checked"] == result["alive"] == len(records)


@pytest.mark.parametrize("requested", [31, 100])
def test_reauth_executor_preserves_mode_and_only_limits_by_account_count(monkeypatch, requested):
    import web_app

    records = _records()
    monkeypatch.setattr(jobs, "_current_reauth_record", lambda record: dict(record))
    monkeypatch.setattr(web_app, "_account_requires_protocol_reauth", lambda _record: False)
    monkeypatch.setattr(web_app, "JOB", SimpleNamespace(append_log=Mock()))
    pools = _record_executor(monkeypatch, web_app)
    manager = jobs.AccountReauthBatchJobs()
    invoke = Mock(return_value=None)
    monkeypatch.setattr(manager, "_invoke_reauthenticate", invoke)

    job = manager.start(records, mode="browser", concurrency=requested)
    assert manager.wait(job["job_id"], 5)
    snapshot = manager.snapshot(job["job_id"])
    expected = min(requested, len(records))
    assert snapshot["concurrency"] == expected
    assert snapshot["mode"] == "browser"
    assert snapshot["done"] == snapshot["success"] == len(records)
    assert pools == [expected]
    assert invoke.call_count == len(records)
    assert all(call.args[1] == "browser" for call in invoke.call_args_list)


@pytest.mark.parametrize("requested", [31, 100])
def test_raw_token_trial_route_runs_above_twenty_without_exceeding_work_count(monkeypatch, requested):
    active = peak = 0

    async def probe(*_args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return {"ok": True, "eligible": True}

    monkeypatch.setattr(trial_health, "_token_trial_eligibility_core", probe)
    tokens = [f"synthetic-access-token-{index:03}" for index in range(37)]
    response = asyncio.run(trial_health.token_trial_eligibility_batch(
        trial_health.TokenTrialEligibilityBatchRequest(access_tokens=tokens, workers=requested),
    ))
    assert response["eligible"] == response["total"] == len(tokens)
    assert response["workers"] == peak == min(requested, len(tokens))

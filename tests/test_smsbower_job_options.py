from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import mail
import web_app
from config_loader import DEFAULT_CONFIG
from web.routes import jobs


def provider(**overrides):
    return {
        "type": "smsbower_mail",
        "enable": True,
        "api_base": "https://mail-api.example.test",
        "api_key": "synthetic-mail-secret",
        "service": "dr",
        "domains": ["gmail.com", "icloud.com"],
        **overrides,
    }


@pytest.fixture
def job_api(monkeypatch):
    state = web_app.JobState()
    cfg = {"registration": {"engine": "protocol"}, "mail": {"providers": [provider()]}}
    capacity = Mock(return_value={"available": 20, "total": 20})
    release = Mock(return_value=0)
    worker = Mock()
    monkeypatch.setattr(web_app, "JOB", state)
    monkeypatch.setattr(web_app, "_read_config", lambda: cfg)
    monkeypatch.setattr(web_app, "list_groups", lambda *_args: [{"id": "test-group", "name": "Test"}])
    monkeypatch.setattr(mail, "count_pool_capacity", capacity)
    monkeypatch.setattr(mail, "release_stale_in_use", release)
    monkeypatch.setattr(jobs, "_run_job_thread", worker)
    app = FastAPI()
    app.include_router(jobs.router)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, state=state, cfg=cfg, capacity=capacity, release=release, worker=worker)
    if state._thread is not None:
        state._thread.join(timeout=2)


@pytest.mark.parametrize("concurrency", [1, 3, 5])
@pytest.mark.parametrize("register_child", [True, False])
def test_http_start_rejects_invalid_channel_concurrency_before_mail_state(job_api, concurrency, register_child):
    job_api.cfg["mail"]["providers"][0]["register_child"] = register_child

    response = job_api.client.post("/api/job/start", json={"mail_type": "smsbower_mail", "total": 4, "concurrency": concurrency})

    assert response.status_code == 400
    assert "偶数" in response.json()["detail"]
    assert job_api.state.running is False
    job_api.capacity.assert_not_called()
    job_api.release.assert_not_called()
    job_api.worker.assert_not_called()


@pytest.mark.parametrize("total", [1, 3, 5])
def test_http_start_rejects_incomplete_pairs_with_legacy_default_enabled(job_api, total):
    response = job_api.client.post("/api/job/start", json={"mail_type": "smsbower_mail", "total": total, "concurrency": 2})

    assert response.status_code == 400
    assert "总账号数" in response.json()["detail"]
    assert job_api.state.running is False
    job_api.capacity.assert_not_called()
    job_api.release.assert_not_called()


def test_http_start_rejects_fixed_smsbower_email_before_any_purchase(job_api):
    response = job_api.client.post("/api/job/start", json={"mail_type": "smsbower_mail", "total": 2, "concurrency": 2, "email": "existing@gmail.com"})

    assert response.status_code == 400
    assert "固定邮箱" in response.json()["detail"]
    job_api.capacity.assert_not_called()
    job_api.release.assert_not_called()


@pytest.mark.parametrize("budget,expected", [(5, 4), (3, 2), (1, 2)])
def test_resource_budget_rounds_down_to_even_logical_slots(job_api, monkeypatch, budget, expected):
    monkeypatch.setattr(web_app, "_effective_registration_workers", lambda *_args: (budget, "test-budget"))
    response = job_api.client.post("/api/job/start", json={"mail_type": "smsbower_mail", "total": 10, "concurrency": 6})

    assert response.status_code == 200
    assert job_api.state.params["concurrency"] == expected
    assert job_api.state.params["requested_concurrency"] == 6
    assert job_api.state.params["total"] == 10


def test_child_disabled_single_main_keeps_two_logical_concurrency_slots(job_api):
    job_api.cfg["mail"]["providers"][0]["register_child"] = False
    response = job_api.client.post("/api/job/start", json={"mail_type": "smsbower_mail", "total": 1, "concurrency": 2})

    assert response.status_code == 200
    assert job_api.state.params["total"] == 1
    assert job_api.state.params["concurrency"] == 2
    assert job_api.state.params["group_plan"] == ["test-group"]


def test_inventory_truncation_keeps_a_complete_pair(job_api):
    job_api.capacity.return_value = {"available": 5, "total": 5}
    response = job_api.client.post("/api/job/start", json={"mail_type": "smsbower_mail", "total": 6, "concurrency": 6})

    assert response.status_code == 200
    assert job_api.state.params["total"] == 4
    assert job_api.state.params["concurrency"] == 4
    assert len(job_api.state.params["group_plan"]) == 4


def test_inventory_below_one_pair_rejects_without_starting_worker(job_api):
    job_api.capacity.return_value = {"available": 1, "total": 1}
    response = job_api.client.post("/api/job/start", json={"mail_type": "smsbower_mail", "total": 2, "concurrency": 2})

    assert response.status_code == 400
    assert job_api.state.running is False
    job_api.worker.assert_not_called()


def test_other_mail_channels_keep_odd_counts_and_worker_limits(job_api, monkeypatch):
    monkeypatch.setattr(web_app, "_effective_registration_workers", lambda *_args: (3, "test-budget"))
    response = job_api.client.post("/api/job/start", json={"mail_type": "icloud", "total": 5, "concurrency": 3})

    assert response.status_code == 200
    assert job_api.state.params["total"] == 5
    assert job_api.state.params["concurrency"] == 3


@pytest.mark.parametrize("saved,expected", [(None, True), (False, False), ("false", False), (True, True)])
def test_provider_default_sanitizer_and_public_outputs_agree(monkeypatch, saved, expected):
    entry = provider(enable=False)
    if saved is not None:
        entry["register_child"] = saved
    cfg = {"mail": {"providers": [entry]}}
    original = deepcopy(cfg)
    monkeypatch.setattr(web_app, "count_pool_capacity", lambda *_args: {})
    monkeypatch.setattr(web_app, "_load_state_file", lambda: {})

    redacted = web_app._redact_config_for_api(cfg)
    status = web_app._mail_stats(cfg)

    assert redacted["mail"]["providers"][0]["register_child"] is expected
    assert status["providers"][0]["register_child"] is expected
    assert "synthetic-mail-secret" not in repr(redacted)
    assert "synthetic-mail-secret" not in repr(status)
    assert cfg == original
    web_app._sanitize_smsbower_mail_provider_entry(entry)
    assert entry["register_child"] is expected


@pytest.mark.parametrize("saved,submitted,expected", [(None, None, True), (False, None, False), (True, False, False), (False, True, True)])
def test_mailbox_update_saves_child_flag_without_overwriting_existing_secrets(monkeypatch, saved, submitted, expected):
    entry = provider()
    if saved is not None:
        entry["register_child"] = saved
    cfg = {"mail": {"providers": [entry]}, "chatgpt2api": {"key": "synthetic-codex-key"}}
    write = Mock()
    monkeypatch.setattr(web_app, "_read_config", lambda: cfg)
    monkeypatch.setattr(web_app, "_write_config", write)
    monkeypatch.setattr(web_app, "_public_mail_stats", lambda *_args: {})
    body = web_app.MailboxesUpdate(provider_type="smsbower_mail", register_child=submitted)

    result = asyncio.run(web_app.put_mailboxes(body))

    assert result["ok"] is True
    assert entry["register_child"] is expected
    assert entry["api_key"] == "synthetic-mail-secret"
    assert cfg["chatgpt2api"]["key"] == "synthetic-codex-key"
    write.assert_called_once_with(cfg)


def test_generic_partial_config_preserves_disabled_child_registration():
    cfg = {"mail": {"providers": [provider(register_child=False)]}}
    web_app._merge_config_preserving_secrets(cfg, {"mail": {"providers": [{"type": "smsbower_mail", "service": "dr"}]}})
    web_app._sanitize_smsbower_mail_provider_entry(cfg["mail"]["providers"][0])
    assert cfg["mail"]["providers"][0]["register_child"] is False
    assert next(entry for entry in DEFAULT_CONFIG["mail"]["providers"] if entry["type"] == "smsbower_mail")["register_child"] is True

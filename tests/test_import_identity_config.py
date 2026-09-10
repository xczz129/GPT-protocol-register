"""Import identity and runtime boundaries, with synthetic credentials only."""
import base64
from copy import deepcopy
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def tokens(email="alice@example.test", user="user-a", team=True):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    plan, workspace = ("team", "workspace-team") if team else ("free", "workspace-personal")
    body = {
        "https://api.openai.com/auth": {"chatgpt_user_id": user, "chatgpt_account_id": workspace, "chatgpt_plan_type": plan},
        "https://api.openai.com/profile": {"email": email},
    }
    return {
        "access_token": f"{encode({'alg': 'none'})}.{encode(body)}.synthetic",
        "plan_type": plan,
        "session": {"user": {"email": email}, "account": {"id": workspace, "planType": plan}},
        "selected_workspace_id": workspace, "workspace_selection_verified": team,
    }


@pytest.fixture
def boundary(monkeypatch, tmp_path):
    import common.account_health as health
    import common.account_store as store
    import register.protocol as protocol
    import web.routes.codex_oauth as route
    cfg = {"proxy": {"enabled": False}, "registration": {"protocol_account_timeout": 27, "protocol_request_timeout": 5}}
    state = SimpleNamespace(ACCOUNTS_FILE=tmp_path / "must-not-exist.json", _read_config=lambda: deepcopy(cfg), normalized_health_status=lambda row: row.get("health_status"))
    check = Mock(side_effect=lambda row, **kwargs: {**row, "health_status": "alive"})
    persist = Mock(side_effect=lambda row, *args, **kwargs: deepcopy(row))
    login = Mock(return_value=tokens())
    monkeypatch.setattr(route, "_state", lambda: state)
    monkeypatch.setattr(route, "_log", Mock())
    monkeypatch.setattr(route, "_STOP", threading.Event())
    monkeypatch.setattr(health, "check_account", check)
    monkeypatch.setattr(store, "upsert_account_record", persist)
    monkeypatch.setattr(protocol, "login_with_password_totp_one", login)
    return SimpleNamespace(route=route, cfg=cfg, state=state, check=check, persist=persist, login=login)


@pytest.mark.parametrize("team", [False, True])
def test_import_rejects_another_email_before_health_or_persistence(boundary, team):
    account = {"email": "alice@example.test", "import_account_type": "team" if team else "personal"}
    with pytest.raises(RuntimeError, match="import_identity_email_mismatch"):
        boundary.route._persist_imported_account(account, tokens("bob@example.test", "user-b", team), Mock())
    boundary.check.assert_not_called()
    boundary.persist.assert_not_called()


def test_import_checks_session_email_even_when_access_token_email_matches(boundary):
    candidate = tokens()
    candidate["session"]["user"]["email"] = "bob@example.test"
    with pytest.raises(RuntimeError, match="import_identity_email_mismatch"):
        boundary.route._persist_imported_account({"email": "alice@example.test", "import_account_type": "team"}, candidate, Mock())
    boundary.check.assert_not_called()


def test_import_rejects_missing_email_evidence(boundary):
    with pytest.raises(RuntimeError, match="import_identity_unverified"):
        boundary.route._persist_imported_account({"email": "alice@example.test"}, {"access_token": "opaque"}, Mock())
    boundary.persist.assert_not_called()


@pytest.mark.parametrize("source", ["session", "record", "id_token"])
def test_import_rejects_mixed_user_identity_sources(boundary, source):
    candidate = tokens()
    if source == "session":
        candidate["session"]["user"]["id"] = "user-b"
    elif source == "record":
        candidate["user_id"] = "user-b"
    else:
        candidate["id_token"] = tokens(user="user-b")["access_token"]
    with pytest.raises(RuntimeError, match="(?:import_identity|team_workspace)_user_mismatch"):
        boundary.route._persist_imported_account({"email": "alice@example.test", "import_account_type": "team"}, candidate, Mock())
    boundary.check.assert_not_called()
    boundary.persist.assert_not_called()


@pytest.mark.parametrize("team", [False, True])
@pytest.mark.parametrize("change", ["email", "user"])
def test_post_health_identity_change_is_rejected(boundary, team, change):
    initial = tokens(team=team)
    changed = tokens("bob@example.test" if change == "email" else "alice@example.test", "user-b" if change == "user" else "user-a", team)
    boundary.check.side_effect = lambda row, **kwargs: {**row, **changed, "health_status": "alive"}
    with pytest.raises(RuntimeError, match="import_identity_.*mismatch"):
        boundary.route._persist_imported_account({"email": "alice@example.test", "import_account_type": "team" if team else "personal"}, initial, Mock())
    boundary.persist.assert_not_called()


@pytest.mark.parametrize("team", [False, True])
def test_matching_identity_is_persisted_with_stable_user_id(boundary, team):
    saved = boundary.route._persist_imported_account({"email": " Alice@Example.Test ", "import_account_type": "team" if team else "personal"}, tokens(team=team), Mock())
    assert saved["user_id"] == "user-a"
    boundary.persist.assert_called_once()


def test_import_uses_configured_proxy_and_shared_runtime_deadline(boundary):
    boundary.cfg["proxy"] = {"enabled": True, "url": "http://proxy.example.test:8080", "mode": "single_proxy"}
    before = time.monotonic()
    result = boundary.route._import_one(1, "alice@example.test", "synthetic-password", "synthetic-totp", account_type="team")
    after = time.monotonic()
    assert result["status"] == "success"
    kwargs = boundary.login.call_args.kwargs
    assert kwargs["proxy"] == boundary.cfg["proxy"]["url"]
    assert kwargs["proxy_config"] == boundary.cfg["proxy"]
    runtime = kwargs["protocol_runtime"]
    assert runtime.request_timeout == 5
    assert before + 27 <= runtime.deadline <= after + 27
    assert boundary.persist.call_args.args[0]["proxy"] == boundary.cfg["proxy"]["url"]


def test_import_rotates_account_slots_over_configured_pool(boundary, monkeypatch):
    import proxy_util
    pool = ["http://first.example.test:8080", "http://second.example.test:8080"]
    boundary.cfg["proxy"] = {"enabled": True, "mode": "pool"}
    monkeypatch.setattr(proxy_util, "resolve_proxy_pool", lambda cfg: pool)
    for index, expected in [(1, pool[0]), (2, pool[1]), (3, pool[0])]:
        assert boundary.route._import_one(index, "alice@example.test", "password", "totp", account_type="team")["status"] == "success"
        assert boundary.login.call_args.kwargs["proxy"] == expected


def test_enabled_empty_proxy_pool_never_falls_back_to_direct(boundary):
    boundary.cfg["proxy"] = {"enabled": True, "url": ""}
    result = boundary.route._import_one(1, "alice@example.test", "password", "totp", account_type="team")
    assert result["status"] == "failed" and "import_proxy_unavailable" in result["error"]
    boundary.login.assert_not_called()
    boundary.persist.assert_not_called()


def test_disabled_proxy_keeps_direct_mode_with_finite_account_deadline(boundary):
    assert boundary.route._import_one(1, "alice@example.test", "password", "totp", account_type="team")["status"] == "success"
    kwargs = boundary.login.call_args.kwargs
    assert kwargs["proxy"] == "" and kwargs["protocol_runtime"].deadline is not None


def test_direct_login_caller_receives_a_default_deadline(monkeypatch):
    import register.protocol_ops as module
    from register.protocol_runtime import current_runtime_settings
    seen = []
    class Registrar:
        def __init__(self, proxy): pass
        def login_with_password_totp(self, *args, **kwargs):
            seen.append(current_runtime_settings())
            raise RuntimeError("synthetic-stop")
        def close(self): pass
    monkeypatch.setattr(module, "PlatformRegistrar", Registrar)
    monkeypatch.setattr(module, "log", Mock())
    before = time.monotonic()
    assert module.login_with_password_totp_one(1, email="alice@example.test", password="password", totp_secret="totp") is None
    assert seen[0].deadline is not None and seen[0].deadline >= before + seen[0].account_timeout

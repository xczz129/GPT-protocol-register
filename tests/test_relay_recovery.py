"""Read-only preflight and temporary-file relay recovery regressions."""

import asyncio
import base64
from copy import deepcopy
import hashlib
import json
import threading
from types import SimpleNamespace

import pytest

from common import relay_recovery as recovery


def token(user="member-a", workspace="team-a", *, client=recovery.CODEX_CLIENT_ID, plan="team"):
    claims = {"exp": 1999999999, "https://api.openai.com/auth": {
        "chatgpt_user_id": user, "chatgpt_account_id": workspace, "chatgpt_plan_type": plan,
    }, "https://api.openai.com/profile": {"email": "a@example.invalid"}}
    if client:
        claims["client_id"] = client
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "synthetic." + body + ".signature"


def account(**extra):
    return {
        "email": "a@example.invalid", "user_id": "member-a", "plan_type": "team",
        "import_account_type": "team", "selected_workspace_id": "team-a",
        "access_token": token(), "refresh_token": "database-rt", "oauth_client_id": recovery.CODEX_CLIENT_ID,
        "password": "synthetic-password", "totp_secret": "synthetic-totp", **extra,
    }


@pytest.fixture
def env(monkeypatch, tmp_path):
    import web.routes.codex_oauth as route

    relay_dir = tmp_path / "relay"
    relay_dir.mkdir()
    state = deepcopy(route._STATE)
    state.update(running=False, mode="codex", logs=[], results=[], items=[], counts={})
    monkeypatch.setattr(route, "_STATE", state)
    monkeypatch.setattr(route, "_LOCK", threading.Lock())
    monkeypatch.setattr(route, "_STOP", threading.Event())
    monkeypatch.setattr(route, "CLIPROXY_AUTHS_DIR", relay_dir)
    monkeypatch.setattr(route, "_RT_HISTORY_PATH", tmp_path / "history.json")
    cache, records = {}, [account()]
    monkeypatch.setattr(route, "_relay_context", lambda: recovery.scan_relay(relay_dir, cache))
    monkeypatch.setattr(route, "_load_sms_batch_records", lambda wanted: [rec for rec in records if rec["email"] in wanted])
    monkeypatch.setattr(route, "update_account_record_fields", lambda *args, **kwargs: (1, []))
    monkeypatch.setattr(route, "_state", lambda: SimpleNamespace(
        ACCOUNTS_FILE=tmp_path / "accounts.json", _now_iso=lambda: "2026-09-11T12:00:00Z", _read_config=lambda: {},
    ))
    return SimpleNamespace(route=route, directory=relay_dir, cache=cache, records=records)


def write_relay(env, *, name=None, **changes):
    filename = name or env.route._cliproxy_auth_filename("a@example.invalid", "team-a", "team")
    path = env.directory / filename
    record = {**account(), "type": "codex", "account_id": "team-a", "refresh_token": "relay-newer-rt", **changes}
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def plan(env, strategy="auto", **kwargs):
    return env.route._sms_preflight({"a@example.invalid"}, False, {}, strategy=strategy, **kwargs)[0]


def run(env, strategy="auto"):
    route = env.route
    stop = route._reserve_batch("codex", len(env.records), "batch")
    route._STATE["options"] = {"strategy": strategy, "concurrency": 1}
    route._run_batch(env.records, {}, batch_id="batch", stop_event=stop)
    return route._STATE["results"][0]


def test_auto_with_stored_rt_but_missing_relay_plans_sync_not_skip(env, monkeypatch):
    monkeypatch.setattr(env.route, "_sms_client", lambda *args: pytest.fail("supplier accessed"))
    monkeypatch.setattr(env.route, "ProtocolClient", lambda *args, **kwargs: pytest.fail("auth accessed"))
    result = plan(env)
    assert result["sync_needed"] == result["eligible"] == 1
    assert result["skipped"] == result["relay_ready"] == result["reauthorize"] == 0
    assert result["action_items"][0]["action"] == "sync_only"
    assert result["configured"] is False and result["relay_configured"] is True


def test_auto_repairs_missing_relay_without_login_or_sms(env, monkeypatch):
    monkeypatch.setattr(env.route, "_process_one", lambda *args, **kwargs: pytest.fail("auth accessed"))
    result = run(env)
    saved = json.loads((env.directory / result["relay_file"]).read_text())
    assert saved["refresh_token"] == "database-rt"
    assert saved["account_id"] == "team-a" and saved["user_id"] == "member-a"
    assert result["outcome"] == "synced" and result["authorization_status"] == "reused"
    assert env.route._STATE["counts"]["synced"] == 1
    assert env.route._STATE["success"] == 1 and env.route._STATE["counts"]["success"] == 0


def test_healthy_relay_rotated_rt_is_preserved_even_when_database_rt_differs(env, monkeypatch):
    path = write_relay(env)
    before = path.read_bytes()
    monkeypatch.setattr(env.route, "_process_one", lambda *args, **kwargs: pytest.fail("auth accessed"))
    monkeypatch.setattr(env.route, "_sync_cliproxy_auth", lambda *args, **kwargs: pytest.fail("relay overwritten"))
    assert plan(env)["relay_ready"] == 1
    result = run(env)
    assert result["outcome"] == "skipped" and result["relay_status"] == "ready"
    assert result["refresh_token"] == "relay-newer-rt"
    assert path.read_bytes() == before


@pytest.mark.parametrize("cache", [
    {"status": "rate_limited", "error": "usage HTTP 429"},
    {"status": "error", "error": "network connection timed out"},
    {"status": "refresh_failed", "error": "token refresh HTTP 503"},
])
def test_quota_or_network_errors_are_not_rt_invalidity(env, cache):
    path = write_relay(env)
    env.cache[path.name] = cache
    assert plan(env)["relay_ready"] == 1
    assert recovery.auth_known_invalid(cache) is False


@pytest.mark.parametrize("cache", [
    {"status": "unauthorized", "error": "usage HTTP 401"},
    {"status": "refresh_failed", "error": "token refresh HTTP 400"},
    {"status": "refresh_failed", "error": "invalid_grant refresh_token_reused"},
])
def test_actual_auth_failures_plan_reauthorization(env, cache):
    path = write_relay(env)
    env.cache[path.name] = cache
    assert plan(env)["reauthorize"] == 1
    assert plan(env, "sync_only")["action_items"][0]["relay_status"] == "requires_auth"


def test_stale_invalid_cache_is_ignored_after_newer_on_disk_rotation(env):
    path = write_relay(env)
    env.cache[path.name] = {"status": "unauthorized", "error": "usage HTTP 401", "fetched_ts": path.stat().st_mtime - 5}
    assert plan(env)["relay_ready"] == 1


@pytest.mark.parametrize("changes", [
    {"access_token": token(client="other-client")},
    {"access_token": token(client=None)},
    {"access_token": ""},
    {"access_token": token(user="other-member")},
    {"access_token": token(workspace="other-team")},
])
def test_unproven_stored_bundle_requires_auth_even_when_rt_exists(env, changes):
    env.records[0].update(changes)
    assert plan(env)["reauthorize"] == 1
    assert plan(env, "sync_only")["action_items"][0]["action"] == "blocked"


def test_sync_only_never_invokes_authorization_when_bundle_is_unproven(env, monkeypatch):
    env.records[0]["access_token"] = ""
    monkeypatch.setattr(env.route, "_process_one", lambda *args, **kwargs: pytest.fail("auth accessed"))
    result = run(env, "sync_only")
    assert result["status"] == "failed" and result["relay_status"] == "requires_auth"
    assert result["retry_action"] == "reauthorize" and result["phone_attempts"] == []


def test_missing_password_does_not_block_safe_relay_sync(env):
    env.records[0].update(password="", totp_secret="")
    result = plan(env, "sync_only")
    assert result["sync_needed"] == 1 and result["missing_credentials"] == []


def test_shared_team_id_does_not_match_another_members_token(env):
    write_relay(env, access_token=token(user="another-member"))
    assert plan(env)["relay_ready"] == 0


def test_relay_type_codex_does_not_disguise_a_web_client_token(env):
    write_relay(env, access_token=token(client="chatgpt-web-client"))
    result = plan(env)
    assert result["relay_ready"] == 0
    assert result["reauthorize"] == 1


def test_pending_equal_tokens_do_not_skip_incomplete_relay_metadata(env):
    path = write_relay(env, account_id="", refresh_token="database-rt")
    rec = env.records[0]
    rec.update(relay_sync_pending=True,
               relay_sync_token_hash=hashlib.sha256(rec["refresh_token"].encode()).hexdigest(),
               relay_sync_base_hash=hashlib.sha256(path.read_bytes()).hexdigest())
    result = run(env, "sync_only")
    assert result["outcome"] == "synced"
    assert json.loads(path.read_text())["account_id"] == "team-a"


def test_pending_same_rt_with_current_invalid_cache_requires_auth(env):
    path = write_relay(env, refresh_token="database-rt")
    rec = env.records[0]
    rec.update(relay_sync_pending=True,
               relay_sync_token_hash=hashlib.sha256(rec["refresh_token"].encode()).hexdigest(),
               relay_sync_base_hash=hashlib.sha256(path.read_bytes()).hexdigest())
    env.cache[path.name] = {"status": "refresh_failed", "error": "invalid_grant", "fetched_ts": path.stat().st_mtime + 1}
    assert plan(env)["reauthorize"] == 1
    before = path.read_bytes()
    result = run(env, "sync_only")
    assert result["relay_status"] == "requires_auth"
    assert path.read_bytes() == before


def test_queued_recovery_reloads_latest_database_rt_before_writing(env):
    queued = deepcopy(env.records[0])
    env.records[0]["refresh_token"] = "newly-rotated-database-rt"
    stop = env.route._reserve_batch("codex", 1, "batch")
    env.route._STATE["options"] = {"strategy": "sync_only"}
    env.route._run_batch([queued], {}, batch_id="batch", stop_event=stop)
    result = env.route._STATE["results"][0]
    assert result["outcome"] == "synced"
    assert json.loads((env.directory / result["relay_file"]).read_text())["refresh_token"] == "newly-rotated-database-rt"


def test_queued_auto_rechecks_relay_repaired_by_another_operation(env, monkeypatch):
    path = write_relay(env)
    env.cache[path.name] = {"status": "refresh_failed", "error": "invalid_grant"}
    context = env.route._relay_context()
    write_relay(env, refresh_token="repaired-by-other-operation")
    monkeypatch.setattr(env.route, "_process_one", lambda *args, **kwargs: pytest.fail("unnecessary authorization"))
    stop = env.route._reserve_batch("codex", 1, "batch")
    env.route._STATE["options"] = {"strategy": "auto"}
    env.route._run_batch(env.records, {}, batch_id="batch", stop_event=stop, relay_context=context)
    assert env.route._STATE["results"][0]["outcome"] == "skipped"


def test_incomplete_relay_metadata_repair_preserves_its_newer_rt(env):
    path = write_relay(env, account_id="")
    result = run(env, "sync_only")
    saved = json.loads(path.read_text())
    assert result["outcome"] == "synced"
    assert saved["account_id"] == "team-a"
    assert saved["refresh_token"] == "relay-newer-rt"


def test_healthy_file_created_after_preflight_is_not_overwritten(env):
    context = env.route._relay_context()
    path = write_relay(env)
    before = path.read_bytes()
    filename, wrote = env.route._sync_existing_relay(env.records[0], context, lambda *args: None)
    assert filename == path.name and wrote is False
    assert path.read_bytes() == before


def test_pending_fresh_auth_syncs_even_if_old_relay_is_structurally_healthy(env):
    path = write_relay(env)
    rec = env.records[0]
    rec.update(relay_sync_pending=True,
               relay_sync_token_hash=hashlib.sha256(rec["refresh_token"].encode()).hexdigest(),
               relay_sync_base_hash=hashlib.sha256(path.read_bytes()).hexdigest())
    assert plan(env, "sync_only")["sync_needed"] == 1
    result = run(env, "sync_only")
    assert result["outcome"] == "synced"
    assert json.loads(path.read_text())["refresh_token"] == "database-rt"


def test_pending_fresh_auth_cannot_overwrite_concurrent_relay_rotation(env):
    path = write_relay(env)
    rec = env.records[0]
    rec.update(relay_sync_pending=True,
               relay_sync_token_hash=hashlib.sha256(rec["refresh_token"].encode()).hexdigest(),
               relay_sync_base_hash=hashlib.sha256(path.read_bytes()).hexdigest())
    write_relay(env, refresh_token="concurrent-rotated-rt")
    result = run(env, "sync_only")
    assert result["status"] == "failed" and result["retry_action"] == "sync_only"
    assert json.loads(path.read_text())["refresh_token"] == "concurrent-rotated-rt"


def test_sync_empty_return_or_failure_is_not_success(env, monkeypatch):
    monkeypatch.setattr(env.route, "_sync_cliproxy_auth", lambda *args, **kwargs: "")
    result = run(env, "sync_only")
    assert result["status"] == "failed" and result["retry_action"] == "sync_only"
    assert env.route._STATE["counts"]["synced"] == 0


def test_preflight_scans_directory_once_for_whole_selection(env, monkeypatch):
    calls = []
    original = env.route._relay_context
    monkeypatch.setattr(env.route, "_relay_context", lambda: calls.append(1) or original())
    env.records.extend({**account(), "email": f"{i}@example.invalid"} for i in range(20))
    env.route._sms_preflight({rec["email"] for rec in env.records}, False, {})
    assert len(calls) == 1


def test_run_accepts_reauthorization_with_sms_disabled_and_preserves_mixed_actions(env, monkeypatch):
    launched = []
    monkeypatch.setattr(env.route, "_launch_batch", lambda *args, **kwargs: launched.append(args))
    result = env.route._start_sms_batch({"a@example.invalid"}, strategy="auto",
                                      account_actions={"a@example.invalid": "reauthorize"})
    assert launched
    assert result["options"]["account_actions"] == {"a@example.invalid": "reauthorize"}
    assert result["options"]["strategy"] == "auto"
    assert plan(env, "reauthorize")["sms_required_if_challenged"] is True


def test_force_true_keeps_legacy_reauthorize_semantics(env):
    write_relay(env)
    result, _ = env.route._sms_preflight({"a@example.invalid"}, True, {})
    assert result["strategy"] == "reauthorize" and result["reauthorize"] == 1
    assert result["relay_ready"] == 0


def test_failed_publish_retry_override_sync_only_wins_previous_reauthorize_strategy(env, monkeypatch):
    monkeypatch.setattr(env.route, "_process_one", lambda *args, **kwargs: pytest.fail("authorization repeated"))
    stop = env.route._reserve_batch("codex", 1, "batch")
    env.route._STATE["options"] = {"strategy": "reauthorize", "force": True,
        "account_actions": {"a@example.invalid": "sync_only"}}
    env.route._run_batch(env.records, {}, batch_id="batch", stop_event=stop)
    assert env.route._STATE["results"][0]["outcome"] == "synced"


def test_sync_only_rejects_per_account_auth_override(env):
    with pytest.raises(env.route.HTTPException):
        env.route._sms_preflight({"a@example.invalid"}, False, {}, strategy="sync_only",
                                 account_actions={"a@example.invalid": "reauthorize"})


def test_authorization_success_relay_failure_keeps_db_token_and_requests_sync_retry(env, monkeypatch, tmp_path):
    from common import account_store as store

    route = env.route
    db = tmp_path / "real-accounts.json"
    monkeypatch.setattr(store, "ACCOUNTS_FILE", db)
    monkeypatch.setattr(store, "ACCOUNTS_DB_FILE", tmp_path / "real-accounts.sqlite3")
    original = store.upsert_account_record(env.records[0], db)
    env.records[:] = [original]
    monkeypatch.setattr(route, "update_account_record_fields", store.update_account_record_fields)
    monkeypatch.setattr(route, "_state", lambda: SimpleNamespace(ACCOUNTS_FILE=db, _now_iso=lambda: "2026-09-11T12:00:00Z"))
    monkeypatch.setattr(route, "totp_from_secret", lambda *args, **kwargs: "123456")
    monkeypatch.setattr(route, "exchange_code", lambda *args: {"access_token": token(), "refresh_token": "fresh-authorized-rt"})
    monkeypatch.setattr(route, "_sync_cliproxy_auth", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("synthetic write failure")))

    class Client:
        def __init__(self, *args, **kwargs):
            self.verifier = "fake"
            self.session = SimpleNamespace(close=lambda: None)

        def bootstrap(self): pass
        def submit_email(self): pass
        def verify_password(self, value): pass
        def mfa_id(self): return "fake"
        def issue_mfa_challenge(self, value): pass
        def verify_mfa(self, *args): return SimpleNamespace(json=lambda: {"page": {"type": "workspace"}})
        def select_workspace(self, *args, **kwargs): return "http://localhost/?code=fake"

    monkeypatch.setattr(route, "ProtocolClient", Client)
    result = run(env, "reauthorize")
    assert result["status"] == "failed"
    assert result["authorization_status"] == "success" and result["relay_status"] == "failed"
    assert result["retry_action"] == "sync_only"
    saved = store.load_accounts(db)[0]
    assert saved["refresh_token"] == "fresh-authorized-rt"
    assert saved["relay_sync_pending"] is True
    assert saved["relay_sync_token_hash"] == hashlib.sha256(b"fresh-authorized-rt").hexdigest()

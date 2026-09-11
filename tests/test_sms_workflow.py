"""SMS accounting and lifecycle regressions. All login/provider calls are mocked."""

import asyncio
import base64
from copy import deepcopy
import json
import threading
from types import SimpleNamespace

import pytest


@pytest.fixture
def route(monkeypatch, tmp_path):
    import web.routes.codex_oauth as module

    state = deepcopy(module._STATE)
    state.update(running=False, mode="codex", batch_id="", logs=[], results=[], items=[], counts={})
    monkeypatch.setattr(module, "_STATE", state)
    monkeypatch.setattr(module, "_LOCK", threading.Lock())
    monkeypatch.setattr(module, "_STOP", threading.Event())
    monkeypatch.setattr(module, "_RT_HISTORY_PATH", tmp_path / "history.json")
    monkeypatch.setattr(module, "_sync_cliproxy_auth", lambda *args: "")
    monkeypatch.setattr(module, "totp_from_secret", lambda *args, **kwargs: "123456")
    monkeypatch.setattr(module, "exchange_code", lambda *args: {"refresh_token": "test-rt"})
    monkeypatch.setattr(module, "update_account_record_fields", lambda *args, **kwargs: (1, []))
    monkeypatch.setattr(module, "_state", lambda: SimpleNamespace(
        ACCOUNTS_FILE=tmp_path / "accounts.json", _now_iso=lambda: "now",
        upsert_account_record=lambda *args, **kwargs: None,
    ))
    return module


def config(attempts=1):
    return {"enabled": True, "api_key": "fake-key", "provider": "smsbower",
            "max_phone_attempts": attempts, "timeout_seconds": 30}


def account(email="first@example.invalid", **extra):
    return {"email": email, "password": "fake-password", "totp_secret": "fake-totp", **extra}


def fake_login(route, monkeypatch, *, mfa_failures=0, send_failures=0, wait=None):
    calls = SimpleNamespace(acquire=0, login=0, closed=0, cancelled=0, send=0, mfa=0, workspaces=[])

    class Client:
        def __init__(self, *args, **kwargs):
            calls.login += 1
            self.verifier = "fake-verifier"
            self.session = SimpleNamespace(close=self.close)

        def close(self):
            calls.closed += 1

        def bootstrap(self): pass
        def submit_email(self): pass
        def verify_password(self, password): pass
        def mfa_id(self): return "fake-mfa"
        def issue_mfa_challenge(self, challenge): pass

        def verify_mfa(self, *args):
            calls.mfa += 1
            if calls.mfa <= mfa_failures:
                raise RuntimeError("mfa/verify incorrect_code")
            return SimpleNamespace(json=lambda: {"page": {"type": "add_phone"}})

        def send_phone(self, *args):
            calls.send += 1
            if calls.send <= send_failures:
                raise RuntimeError("synthetic number rejected")

        def submit_phone_otp(self, *args): pass
        def select_workspace(self, *args, **kwargs):
            calls.workspaces.append((args, kwargs))
            return "http://localhost/?code=fake-code"

    class Sms:
        def acquire(self):
            calls.acquire += 1
            return route.SmsActivation(str(calls.acquire), "+15550000000", 6, "fake",
                                       label="Indonesia", provider="smsbower")

        def wait_code(self, activation, *, cancel_event):
            if wait:
                return wait(cancel_event)
            return "654321"

        def finish(self, activation): pass

        def cancel(self, activation):
            calls.cancelled += 1

    monkeypatch.setattr(route, "ProtocolClient", Client)
    monkeypatch.setattr(route, "_sms_client", lambda *args: Sms())
    return calls


@pytest.mark.parametrize("limit", [1, 2, 4])
def test_number_attempt_budget_is_not_increased_by_mfa_allowance(route, monkeypatch, limit):
    calls = fake_login(route, monkeypatch, send_failures=100)
    attempts = []
    with pytest.raises(RuntimeError, match="取号"):
        route._process_one(account(), config(limit), phone_attempts=attempts)
    assert calls.acquire == limit
    assert len(attempts) == limit
    assert all(item["result"] == "failed" for item in attempts)
    assert calls.closed == calls.login


def test_mfa_retry_does_not_consume_number_budget(route, monkeypatch):
    calls = fake_login(route, monkeypatch, mfa_failures=3)
    event = SimpleNamespace(is_set=lambda: False, wait=lambda duration: False)
    result = route._process_one(account(), config(), stop_event=event)
    assert result["status"] == "success"
    assert calls.mfa == 4
    assert calls.acquire == 1
    assert calls.login == calls.closed == 4


def test_cancellation_during_mfa_wait_is_interruptible_and_buys_no_number(route, monkeypatch):
    calls = fake_login(route, monkeypatch, mfa_failures=1)
    event = SimpleNamespace(is_set=lambda: False, wait=lambda duration: True)
    with pytest.raises(route._SmsCancelled):
        route._process_one(account(), config(), stop_event=event)
    assert calls.acquire == 0
    assert calls.closed == 1


def test_waiting_status_has_deadline_and_no_otp_or_secret(route, monkeypatch):
    stop = route._reserve_batch("codex", 1, "batch")
    route._STATE["items"] = [{"email": account()["email"], "status": "pending"}]
    snapshots = []

    def wait(event):
        snapshots.append(asyncio.run(route.codex_oauth_status()))
        return "654321"

    fake_login(route, monkeypatch, wait=wait)
    route._run_batch([account()], config(), batch_id="batch", stop_event=stop)
    item = snapshots[0]["items"][0]
    assert item["stage"] == "waiting_sms"
    assert item["wait_deadline"] > 0
    assert item["attempt"] == 1
    text = json.dumps(snapshots)
    for secret in ("fake-password", "fake-totp", "654321", "123456", "fake-key"):
        assert secret not in text
    assert route._STATE["items"][0]["wait_deadline"] is None


def test_stop_reconciles_cancelled_active_and_never_started_accounts(route, monkeypatch):
    stop = route._reserve_batch("codex", 3, "batch")

    def wait(event):
        asyncio.run(route.codex_oauth_stop())
        assert route._STATE["status"] == "stopping"
        raise route.SmsError("任务已取消")

    calls = fake_login(route, monkeypatch, wait=wait)
    records = [account(f"{i}@example.invalid") for i in range(3)]
    route._run_batch(records, config(), batch_id="batch", stop_event=stop)
    assert calls.acquire == 1
    assert route._STATE["done"] == route._STATE["total"] == 3
    assert route._STATE["counts"]["cancelled"] == 3
    assert route._STATE["counts"]["active"] == route._STATE["counts"]["pending"] == 0
    assert route._STATE["failed"] == 0
    assert route._STATE["status"] == "cancelled"
    assert len(route._STATE["results"]) == 3
    history = json.loads(route._RT_HISTORY_PATH.read_text())
    assert all(rows[0]["outcome"] == "cancelled" for rows in history.values())


def test_existing_rt_counts_separately_without_login_and_persists_options(route, monkeypatch):
    stop = route._reserve_batch("codex", 1, "batch")
    route._STATE["options"] = {"concurrency": 1, "force": False, "sms_options": {"provider": "smsbower"}}
    monkeypatch.setattr(route, "_process_one", lambda *args, **kwargs: pytest.fail("unexpected login"))
    route._run_batch([account(refresh_token="existing-rt")], config(), batch_id="batch", stop_event=stop)
    assert route._STATE["success"] == 1  # Legacy contract.
    assert route._STATE["counts"]["success"] == 0
    assert route._STATE["counts"]["skipped"] == 1
    row = json.loads(route._RT_HISTORY_PATH.read_text())[account()["email"]][0]
    assert row["batch_id"] == "batch"
    assert row["options"]["sms_options"]["provider"] == "smsbower"
    assert row["outcome"] == "skipped"


def test_preflight_categorizes_without_supplier_calls_or_mutation(route, monkeypatch):
    cfg = {"sms_code": config()}
    records = [account("good@example.invalid"), account("rt@example.invalid", refresh_token="private-rt"),
               {"email": "missing@example.invalid"}, account("off@example.invalid", is_deactivated=True)]
    monkeypatch.setattr(route, "_state", lambda: SimpleNamespace(_read_config=lambda: cfg))
    monkeypatch.setattr(route, "_load_sms_batch_records", lambda wanted: records)
    monkeypatch.setattr(route, "_sms_client", lambda *args: pytest.fail("supplier accessed"))
    monkeypatch.setattr(route, "_smsbower_request", lambda *args, **kwargs: pytest.fail("supplier accessed"))
    before = deepcopy(cfg)
    body = route.CodexOauthRunRequest(
        emails=[rec["email"] for rec in records] + ["unknown@example.invalid"],
        sms_options={"max_phone_attempts": 2, "timeout_seconds": 60, "max_price": "0.15"},
    )
    result = asyncio.run(route.codex_oauth_preflight(body))
    assert result["total"] == 5
    assert result["eligible"] == result["skipped"] == 1
    assert result["deactivated"] == ["off@example.invalid"]
    assert result["not_found"] == ["unknown@example.invalid"]
    assert result["missing_credentials"] == [{"email": "missing@example.invalid", "missing": ["password", "totp_secret"]}]
    assert result["settings"]["max_phone_attempts"] == 2
    assert result["configured"] is True
    assert cfg == before
    text = json.dumps(result)
    assert "fake-key" not in text and "private-rt" not in text and "fake-password" not in text


def test_force_preflight_requires_credentials_for_existing_rt(route, monkeypatch):
    monkeypatch.setattr(route, "_load_sms_batch_records", lambda _: [{"email": "x@example.invalid", "refresh_token": "old"}])
    skip, _ = route._sms_preflight({"x@example.invalid"}, False, config())
    force, _ = route._sms_preflight({"x@example.invalid"}, True, config())
    assert skip["skipped"] == 1 and not skip["missing_credentials"]
    assert force["skipped"] == 0 and len(force["missing_credentials"]) == 1


@pytest.mark.parametrize("price", ["-1", "nan", "inf", "no-price", 0, "0"])
def test_per_run_price_cap_rejects_invalid_values(route, price):
    with pytest.raises(route.HTTPException):
        route._effective_sms_config({"sms_code": config()}, {"max_price": price})


def test_firefox_preflight_does_not_promise_unverified_cap_and_exposes_only_country(route):
    cfg = {"sms_code": {"enabled": True, "provider": "firefox", "firefox_api_token": "private-token",
                        "firefox_docking_code": "eng-1096-private", "max_price": "0.25"}}
    section = route._effective_sms_config(cfg)
    result = route._safe_sms_settings(section)
    assert result["price_cap_supported"] is False
    assert result["max_price"] == ""
    assert result["country_order"] == "eng"
    assert "private" not in json.dumps(result)
    with pytest.raises(route.HTTPException):
        route._effective_sms_config(cfg, {"country_order": "usa"})


def test_run_snapshots_overrides_without_mutating_config_or_accounts(route, monkeypatch):
    cfg = {"sms_code": config()}
    records = [account(refresh_token="old-rt")]
    before = deepcopy((cfg, records))
    monkeypatch.setattr(route, "_state", lambda: SimpleNamespace(_read_config=lambda: cfg))
    monkeypatch.setattr(route, "_load_sms_batch_records", lambda _: records)
    launches = []
    monkeypatch.setattr(route, "_launch_batch", lambda *args, **kwargs: launches.append((args, kwargs)))
    result = route._start_sms_batch({account()["email"]}, force=True, concurrency=3,
                                    sms_options={"max_phone_attempts": 2})
    assert result["batch_id"] == route._STATE["batch_id"]
    assert result["concurrency"] == 1
    assert result["options"]["sms_options"]["max_phone_attempts"] == 2
    assert launches[0][0][1][0][0]["refresh_token"] == "old-rt"
    assert result["options"]["force"] is True
    assert (cfg, records) == before


def test_stats_counts_legacy_no_tier_failure_and_repeat_phone_attempts(route):
    row = {"status": "success", "phone": "+15550000000", "logs": [
        {"m": "已分配 +15550000000(Indonesia)"},
        {"m": "号码 +15550000000 失败: rejected"},
        {"m": "已分配 +15550000000(england)"},
        {"m": "手机验证通过"},
    ]}
    route._RT_HISTORY_PATH.write_text(json.dumps({"x": [row]}))
    result = asyncio.run(route.sms_stats())
    assert result["summary"]["total"] == 2
    assert result["summary"]["success"] == result["summary"]["failed"] == 1
    assert result["summary"]["rate"] == 50
    assert result["summary"]["unpriced"] == 2


def test_structured_stats_use_all_providers_and_never_double_count_logs(route):
    row = {"phone_attempts": [
        {"provider": "smsbower", "country_label": "Indonesia", "tier": "123", "estimated_price": 0.2, "result": "failed"},
        {"provider": "firefox", "country_label": "england", "result": "success"},
        {"provider": "firefox", "country_label": "england", "result": "cancelled"},
        {"provider": "smsbower", "result": "acquire_failed"},
    ], "logs": [{"m": "已分配 +15550000000(Indonesia #123 $0.2)"}]}
    route._RT_HISTORY_PATH.write_text(json.dumps({"x": [row]}))
    result = asyncio.run(route.sms_stats())
    assert result["summary"] == {"total": 4, "success": 1, "failed": 2, "cancelled": 1,
                                  "unknown": 0, "unpriced": 3, "rate": 25.0, "structured": 4, "historical": 0}
    assert result["cost_is_estimate"] is True
    assert result["billing_source"] == "catalogue_quotes_only"


def test_country_annotated_price_never_overrides_lower_global_cap(route, monkeypatch):
    settings = route.SmsSettings.from_mapping({**config(), "max_price": "0.15"})
    client = route._PanelSmsBowerClient(settings, [("6", [], "0.8")])
    monkeypatch.setattr(route, "_smsbower_tier_prices", lambda *args: {})
    sent = []
    monkeypatch.setattr(client, "_request", lambda action, **params: sent.append(params) or "ACCESS_NUMBER:fake:15550000000")
    client.acquire()
    assert sent[0]["maxPrice"] == "0.15"


def test_supplier_finish_failure_keeps_verified_number_and_continues_oauth(route, monkeypatch):
    calls = fake_login(route, monkeypatch)
    original = route._sms_client

    def sms_client(*args):
        client = original(*args)

        def fail_finish(activation):
            raise route.SmsError("synthetic supplier acknowledgement failure")

        client.finish = fail_finish
        return client

    monkeypatch.setattr(route, "_sms_client", sms_client)
    result = route._process_one(account(), config(3))
    assert result["status"] == "success"
    assert result["refresh_token"] == "test-rt"
    assert result["phone_attempts"][0]["result"] == "success"
    assert calls.acquire == calls.login == 1
    assert calls.cancelled == 0


@pytest.mark.parametrize("failure", [False, True])
def test_balance_metadata_names_actual_saved_provider(route, monkeypatch, failure):
    monkeypatch.setattr(route, "_state", lambda: SimpleNamespace(_read_config=lambda: {"sms_code": config()}))

    def check(settings):
        if failure:
            raise route.SmsError("synthetic failure")
        return {"balance": "12.3"}

    monkeypatch.setattr(route, "check_sms_balance", check)
    result = asyncio.run(route.sms_config_test())
    assert result["provider"] == "smsbower"
    assert result["provider_label"] == "SMSBower"
    assert result["checked_at"]
    assert result["ok"] is (not failure)


def team_token(user="member-a", workspace="team-a", plan="team"):
    payload = {
        "https://api.openai.com/auth": {
            "chatgpt_user_id": user, "chatgpt_account_id": workspace, "chatgpt_plan_type": plan,
        },
        "https://api.openai.com/profile": {"email": account()["email"]},
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return "synthetic." + encoded + ".signature"


def team_account(**changes):
    return account(user_id="member-a", plan_type="team", import_account_type="team",
                   selected_workspace_id="team-a", access_token=team_token(), **changes)


@pytest.mark.parametrize("candidate", [
    {"access_token": team_token(workspace="personal", plan="free"), "refresh_token": "new"},
    {"access_token": team_token(workspace="team-other"), "refresh_token": "new"},
    {"access_token": team_token(user="member-other"), "refresh_token": "new"},
    {"refresh_token": "new"},
])
def test_team_sms_rejects_wrong_workspace_user_or_unverified_token_before_writes(route, monkeypatch, candidate):
    fake_login(route, monkeypatch)
    monkeypatch.setattr(route, "exchange_code", lambda *args: candidate)
    monkeypatch.setattr(route, "update_account_record_fields", lambda *args, **kwargs: pytest.fail("unexpected DB write"))
    monkeypatch.setattr(route, "_sync_cliproxy_auth", lambda *args: pytest.fail("unexpected credential publish"))
    with pytest.raises(RuntimeError):
        route._process_one(team_account(), config())


def test_team_sms_preserves_strict_id_and_persists_matching_fresh_evidence(route, monkeypatch):
    calls = fake_login(route, monkeypatch)
    candidate = {"access_token": team_token(), "refresh_token": "new-rt"}
    monkeypatch.setattr(route, "exchange_code", lambda *args: candidate)
    writes, syncs = [], []
    monkeypatch.setattr(route, "update_account_record_fields", lambda *args, **kwargs: writes.append((args, kwargs)) or (1, []))
    monkeypatch.setattr(route, "_sync_cliproxy_auth", lambda *args: syncs.append(args))
    result = route._process_one(team_account(), config())
    assert result["status"] == "success"
    assert calls.workspaces == [(("",), {"strict_team": True, "expected_workspace_id": "team-a"})]
    fields = next(iter(writes[0][0][0].values()))
    assert fields["plan_type"] == "team" and fields["user_id"] == "member-a"
    assert "password" not in fields and "group_id" not in fields
    assert len(syncs) == 1


def test_team_sms_does_not_treat_old_personal_token_as_expected_team_id(route, monkeypatch):
    calls = fake_login(route, monkeypatch)
    previous = account(import_account_type="team", workspace_name="Team A",
                       access_token=team_token(workspace="personal", plan="free"))
    monkeypatch.setattr(route, "exchange_code", lambda *args: {"access_token": team_token(), "refresh_token": "new"})
    route._process_one(previous, config())
    assert calls.workspaces[0] == (("Team A",), {"strict_team": True, "expected_workspace_id": ""})


def test_protocol_strict_team_without_name_never_defaults_to_first_personal():
    from codex_oauth.oauth import ProtocolClient

    client = ProtocolClient.__new__(ProtocolClient)
    client.auth_session_data = {"workspaces": [
        {"id": "personal", "name": "Personal", "type": "personal"},
        {"id": "team-a", "name": "Renamed Team", "plan_type": "team"},
    ]}
    assert client.workspace_id() == "personal"  # Legacy personal behavior stays unchanged.
    assert client.workspace_id(strict_team=True) == "team-a"
    assert client.workspace_id("Old Team Name", strict_team=True, expected_workspace_id="team-a") == "team-a"
    with pytest.raises(RuntimeError):
        client.workspace_id(strict_team=True, expected_workspace_id="personal")
    with pytest.raises(RuntimeError, match="id_not_found"):
        client.workspace_id(strict_team=True, expected_workspace_id="missing-team")
    client.auth_session_data["workspaces"].append({"id": "team-b", "name": "Team B", "plan_type": "team"})
    with pytest.raises(RuntimeError, match="ambiguous"):
        client.workspace_id(strict_team=True)
    assert client.workspace_id(strict_team=True, expected_workspace_id="team-a") == "team-a"


def test_sms_token_field_update_preserves_concurrent_password_and_group_changes(route, monkeypatch, tmp_path):
    from common import account_store as store

    db = tmp_path / "account-fields.json"
    monkeypatch.setattr(store, "ACCOUNTS_DB_FILE", tmp_path / "account-fields.sqlite3")
    monkeypatch.setattr(store, "ACCOUNTS_FILE", db)
    previous = store.upsert_account_record(team_account(group_id="old", refresh_token="old-rt"), db)
    key = store.account_delete_key(previous)
    store.update_account_record_fields({key: {"password": "changed-password", "group_id": "new"}}, path=db)
    monkeypatch.setattr(route, "update_account_record_fields", store.update_account_record_fields)
    monkeypatch.setattr(route, "_state", lambda: SimpleNamespace(ACCOUNTS_FILE=db, _now_iso=lambda: "now"))
    route._persist_sms_tokens(previous, {"access_token": team_token(), "refresh_token": "new-rt"})
    saved = store.load_accounts(db)[0]
    assert saved["password"] == "changed-password" and saved["group_id"] == "new"
    assert saved["refresh_token"] == "new-rt"


def test_sms_token_field_update_rejects_concurrent_token_replacement(route, monkeypatch, tmp_path):
    from common import account_store as store

    db = tmp_path / "account-stale.json"
    monkeypatch.setattr(store, "ACCOUNTS_DB_FILE", tmp_path / "account-stale.sqlite3")
    monkeypatch.setattr(store, "ACCOUNTS_FILE", db)
    previous = store.upsert_account_record(team_account(refresh_token="old-rt"), db)
    key = store.account_delete_key(previous)
    store.update_account_record_fields({key: {"refresh_token": "parallel-rt"}}, path=db)
    monkeypatch.setattr(route, "update_account_record_fields", store.update_account_record_fields)
    monkeypatch.setattr(route, "_state", lambda: SimpleNamespace(ACCOUNTS_FILE=db, _now_iso=lambda: "now"))
    with pytest.raises(RuntimeError, match="sms_credentials_changed"):
        route._persist_sms_tokens(previous, {"access_token": team_token(), "refresh_token": "stale-rt"})
    assert store.load_accounts(db)[0]["refresh_token"] == "parallel-rt"

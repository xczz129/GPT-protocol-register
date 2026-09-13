from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

import mail
from common import mail_registration_metrics as metrics


@pytest.fixture(autouse=True)
def isolated_pair_state(tmp_path, monkeypatch):
    monkeypatch.setattr(mail, "STATE_FILE", tmp_path / "mail-state.json")
    monkeypatch.setattr(mail, "_SMSBOWER_MAIL_ACTIVE_PAIRS", set())
    monkeypatch.setattr(mail, "_SMSBOWER_MAIL_PAIR_RETRIES", {})
    monkeypatch.setattr(mail, "_SMSBOWER_MAIL_ACTIVATION_BREAKERS", {})
    monkeypatch.setattr(mail, "_known_mailbox_bindings", lambda: {})
    monkeypatch.setenv("MAIL_REGISTRATION_METRICS_DB", str(tmp_path / "metrics.sqlite3"))
    monkeypatch.setattr(mail.requests.sessions.Session, "request", Mock(side_effect=AssertionError("network forbidden")))


@pytest.fixture
def pair(monkeypatch):
    provider = mail.SmsBowerMailProvider({
        "type": "smsbower_mail", "provider_ref": "smsbower_mail#1",
        "api_base": "https://mail-api.example", "api_key": "private-pair-api-key",
        "service": "dr", "domains": ["gmail.com"],
    }, mail._make_config({}))
    primary = provider._settlement_mailbox("fresh@gmail.com", "activation-pair", "gmail.com")
    primary["_registration_metrics_defer_terminal"] = True
    mail._set_state(primary["address"], "in_use")
    set_status = Mock()
    monkeypatch.setattr(mail.SmsBowerMailProvider, "set_status", set_status)
    monkeypatch.setattr(mail.SmsBowerMailProvider, "get_status", Mock(return_value={"data": {"status": 1}}))
    yield provider, primary, set_status
    provider.close()


def test_prepare_journals_without_secrets_and_live_lease_is_not_recovered(pair):
    provider, primary, set_status = pair
    mail.prepare_smsbower_pair(primary)
    mail.prepare_smsbower_pair(primary)
    entry = mail._load_state()[primary["address"]]
    assert entry["smsbower_pending_status"] == 2
    assert entry["smsbower_pair_id"] == primary["_smsbower_pair_id"]
    assert "private-pair-api-key" not in mail.STATE_FILE.read_text()
    assert provider._recover_pending_settlements() == 0
    provider.get_status.assert_not_called()
    set_status.assert_not_called()


def test_child_has_fresh_identity_and_shared_activation_and_is_reused_on_replay(pair):
    _, primary, _ = pair
    primary.update({"_registration_identity_created": True, "_registration_guard_error": "stale",
                    "_registration_outcome": "success", "_otp_accepted_by_server": True,
                    "_registration_metric_attempt_id": "parent-attempt", "_code_not_before": "old",
                    "_smsbower_last_code": "123456", "_smsbower_codes_delivered": 1})
    mail.prepare_smsbower_pair(primary)
    child = mail.create_smsbower_child(primary)
    assert child["address"] == mail.split_mailbox_address(primary["address"])
    assert child["base_address"] == child["login_address"] == primary["address"]
    assert child["mail_id"] == primary["mail_id"]
    assert child["_smsbower_pair_role"] == "child"
    assert child["_smsbower_pair_state"] is primary["_smsbower_pair_state"]
    assert child["_smsbower_last_code"] == "123456"
    assert child["_smsbower_codes_delivered"] == 1
    for key in ("_registration_identity_created", "_registration_guard_error", "_registration_outcome",
                "_otp_accepted_by_server", "_registration_metric_attempt_id", "_code_not_before"):
        assert key not in child
    assert mail.create_smsbower_child(primary) is child


@pytest.mark.parametrize("source", ["account", "used_state"])
def test_child_already_registered_locally_is_skipped_without_reopening_slot(pair, monkeypatch, source):
    _, primary, set_status = pair
    address = mail.split_mailbox_address(primary["address"])
    previous = {"state": "used", "reason": "existing account", "updated_at": "2026-01-01T00:00:00Z"}
    if source == "account":
        monkeypatch.setattr(mail, "_known_mailbox_bindings", lambda: {address: "registered_account"})
    else:
        state = mail._load_state()
        state[address] = dict(previous)
        mail._save_state(state)
    mail.prepare_smsbower_pair(primary)
    child = mail.create_smsbower_child(primary)
    assert child["_registration_outcome"] == "already_registered"
    assert primary["_smsbower_pair_state"]["outcomes"]["child"] == "already_registered"
    assert mail._load_state()[address]["state"] == "used"
    if source == "used_state":
        assert mail._load_state()[address] == previous
    journal = mail._load_state()[primary["address"]]
    assert journal["smsbower_pair_outcomes"]["child"] == "already_registered"
    set_status.assert_not_called()


def test_registered_primary_does_not_make_its_unregistered_exact_child_used(pair, monkeypatch):
    _, primary, _ = pair
    monkeypatch.setattr(mail, "_known_mailbox_bindings", lambda: {primary["address"]: "registered_account"})
    mail.prepare_smsbower_pair(primary)
    child = mail.create_smsbower_child(primary)
    assert "_registration_outcome" not in child
    assert mail._load_state()[child["address"]]["state"] == "in_use"


@pytest.mark.parametrize("state_name", ["in_use", "failed", "token_invalid", "cooldown"])
def test_unavailable_child_is_not_overwritten_or_adopted_by_current_lease(pair, state_name):
    _, primary, _ = pair
    address = mail.split_mailbox_address(primary["address"])
    # Even stale in_use reservations belong to their existing owner here.
    previous = {"state": state_name, "reason": "owned elsewhere", "updated_at": "2000-01-01T00:00:00Z"}
    state = mail._load_state()
    state[address] = dict(previous)
    mail._save_state(state)
    mail.prepare_smsbower_pair(primary)
    with pytest.raises(mail.SmsBowerMailError, match="unavailable or already reserved") as raised:
        mail.create_smsbower_child(primary)
    assert address not in str(raised.value)
    assert "private-pair-api-key" not in str(raised.value)
    assert mail._load_state()[address] == previous
    assert "child" not in primary["_smsbower_pair_state"]["addresses"]
    mail.finalize_smsbower_pair(primary, primary_success=True)
    assert mail._load_state()[address] == previous


def test_primary_and_child_success_defer_settlement_then_complete_exactly_once(pair):
    _, primary, set_status = pair
    mail.prepare_smsbower_pair(primary)
    mail.mark_mailbox_result(primary, success=True)
    child = mail.create_smsbower_child(primary)
    mail.mark_mailbox_result(child, success=True)
    mail.release_mailbox(primary)
    set_status.assert_not_called()
    assert mail._load_state()[primary["address"]]["smsbower_pending_status"] == 3
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: mail.finalize_smsbower_pair(primary, primary_success=True, child_success=True), range(24)))
    mail.release_mailbox(child)
    mail.mark_mailbox_result(primary, success=False, error="cancelled")
    assert set_status.call_args_list[0].args == (primary["mail_id"], 3)
    assert set_status.call_count == 1
    assert not mail._SMSBOWER_MAIL_ACTIVE_PAIRS
    assert not mail._SMSBOWER_MAIL_PAIR_RETRIES
    state = mail._load_state()
    assert state[primary["address"]]["state"] == state[child["address"]]["state"] == "used"
    assert "smsbower_pending_status" not in state[primary["address"]]


def test_registered_primary_is_retired_and_cancelled_without_child_or_consumption(pair):
    _, primary, set_status = pair
    mail.prepare_smsbower_pair(primary)
    primary["_registration_outcome"] = "already_registered"
    mail.mark_mailbox_result(primary, success=False, error="already_registered: password_verification")
    assert mail._load_state()[primary["address"]]["state"] == "used"
    set_status.assert_not_called()
    mail.finalize_smsbower_pair(primary, primary_success=False)
    set_status.assert_called_once_with(primary["mail_id"], 2)
    assert mail._load_state()[primary["address"]]["state"] == "used"


def test_otp_consumed_then_failed_or_cancelled_still_completes(pair):
    _, primary, set_status = pair
    mail.prepare_smsbower_pair(primary)
    mail.mark_smsbower_otp_accepted(primary)
    entry = mail._load_state()[primary["address"]]
    assert entry["smsbower_pending_status"] == 3
    assert entry["smsbower_pair_accepted_roles"] == ["primary"]
    mail.mark_mailbox_result(primary, success=False, error="transient registration failed")
    mail.finalize_smsbower_pair(primary, primary_success=False)
    set_status.assert_called_once_with(primary["mail_id"], 3)
    assert mail._load_state()[primary["address"]]["state"] == "used"


def test_all_unconsumed_failures_cancel_once(pair):
    _, primary, set_status = pair
    mail.prepare_smsbower_pair(primary)
    mail.mark_mailbox_result(primary, success=False, error="验证码超时")
    mail.finalize_smsbower_pair(primary, primary_success=False)
    mail.finalize_smsbower_pair(primary, primary_success=False)
    set_status.assert_called_once_with(primary["mail_id"], 2)
    assert primary["_registration_outcome"] == "otp_timeout"


def test_crash_recovery_uses_persisted_consumption_instead_of_cancelling(pair):
    provider, primary, set_status = pair
    mail.prepare_smsbower_pair(primary)
    mail.mark_smsbower_otp_accepted(primary)
    # A new process has no live-lease registry; only the durable journal remains.
    mail._SMSBOWER_MAIL_ACTIVE_PAIRS.clear()
    assert provider._recover_pending_settlements() == 1
    set_status.assert_called_once_with(primary["mail_id"], 3)
    assert "smsbower_pending_status" not in mail._load_state()[primary["address"]]
    assert mail._load_state()[primary["address"]]["state"] == "used"


def test_failed_remote_settlement_keeps_durable_retry_and_recovery_dedupes(pair):
    provider, primary, set_status = pair
    mail.prepare_smsbower_pair(primary)
    mail.mark_mailbox_result(primary, success=True)
    set_status.side_effect = mail.SmsBowerMailError("remote response lost")
    mail.finalize_smsbower_pair(primary, primary_success=True)
    assert not mail._SMSBOWER_MAIL_ACTIVE_PAIRS
    assert primary["_smsbower_pair_id"] in mail._SMSBOWER_MAIL_PAIR_RETRIES
    assert mail._load_state()[primary["address"]]["smsbower_pending_status"] == 3
    # The response was lost after the server committed status 3.
    provider.get_status.return_value = {"data": {"status": 3}}
    assert provider._recover_pending_settlements() == 1
    mail.finalize_smsbower_pair(primary, primary_success=True)
    assert set_status.call_count == 1
    assert not mail._SMSBOWER_MAIL_PAIR_RETRIES


def test_journal_failure_keeps_consumed_retry_and_blocks_buy_recovery_until_saved(pair, monkeypatch):
    provider, primary, set_status = pair
    mail.prepare_smsbower_pair(primary)
    original = mail._update_smsbower_settlement_state
    monkeypatch.setattr(mail, "_update_smsbower_settlement_state", Mock(side_effect=OSError("disk full")))
    primary["_otp_accepted_by_server"] = True
    mail.finalize_smsbower_pair(primary, primary_success=False)
    assert not mail._SMSBOWER_MAIL_ACTIVE_PAIRS
    assert primary["_smsbower_pair_id"] in mail._SMSBOWER_MAIL_PAIR_RETRIES
    set_status.assert_not_called()
    with pytest.raises(mail.SmsBowerMailError, match="journal is unavailable"):
        provider._recover_pending_settlements()
    monkeypatch.setattr(mail, "_update_smsbower_settlement_state", original)
    assert provider._recover_pending_settlements() == 1
    set_status.assert_called_once_with(primary["mail_id"], 3)


def test_stale_not_found_recovery_also_removes_in_memory_pair_retry(pair, monkeypatch):
    provider, primary, set_status = pair
    mail.prepare_smsbower_pair(primary)
    set_status.side_effect = mail.SmsBowerMailError("response lost")
    mail.finalize_smsbower_pair(primary, primary_success=False)
    provider.get_status.side_effect = mail.SmsBowerMailError("no activation found")
    monkeypatch.setattr(mail, "_smsbower_settlement_age_seconds", lambda entry: 3600)
    assert provider._recover_pending_settlements() == 1
    assert not mail._SMSBOWER_MAIL_PAIR_RETRIES
    assert primary["address"] not in mail._load_state()
    assert provider._recover_pending_settlements() == 0
    mail.finalize_smsbower_pair(primary, primary_success=False)
    assert set_status.call_count == 1


def test_child_rejects_all_parent_codes_and_requests_next_code_once(pair, monkeypatch):
    provider, primary, set_status = pair
    mail.prepare_smsbower_pair(primary)
    codes = iter(["111111", "222222", "111111", "222222", "333333"])
    monkeypatch.setattr(provider, "get_code", lambda mail_id: next(codes))
    monkeypatch.setattr(provider, "_sleep_until_next_poll", lambda *args: True)
    assert provider.wait_for_code(primary) == "111111"
    assert provider.wait_for_code(primary) == "222222"
    child = mail.create_smsbower_child(primary)
    assert provider.wait_for_code(child) == "333333"
    assert [call.args for call in set_status.call_args_list] == [(primary["mail_id"], 5)] * 2
    assert child["_smsbower_codes_delivered"] == 3
    assert child["_smsbower_seen_codes"] == ["111111", "222222", "333333"]


def test_fast_child_code_is_not_cleared_after_it_arrives(pair, monkeypatch):
    provider, primary, set_status = pair
    current = {"code": "111111"}
    mail.prepare_smsbower_pair(primary)
    monkeypatch.setattr(provider, "get_code", lambda _: current["code"])
    assert provider.wait_for_code(primary) == "111111"
    mail.mark_smsbower_otp_accepted(primary)
    assert not set_status.called
    child = mail.create_smsbower_child(primary)

    def status(mail_id, value):
        if value == 5:
            current["code"] = None

    set_status.side_effect = status
    mail.prepare_smsbower_next_code(child)
    set_status.assert_called_once_with(primary["mail_id"], 5)
    current["code"] = "222222"  # The child auth request now sends and delivers.
    assert provider.wait_for_code(child) == "222222"
    assert child["_registration_code_received"] is True
    assert set_status.call_count == 1
    mail.mark_smsbower_otp_accepted(child)
    mail.finalize_smsbower_pair(primary, primary_success=True, child_success=True)
    assert [call.args[1] for call in set_status.call_args_list] == [5, 3]


def test_lost_next_code_request_is_shared_and_never_replayed_by_child(pair, monkeypatch):
    provider, primary, set_status = pair
    primary["_smsbower_codes_delivered"] = 1
    primary["_smsbower_last_code"] = "111111"
    mail.prepare_smsbower_pair(primary)
    set_status.side_effect = mail.SmsBowerMailError("response lost")
    with pytest.raises(mail.SmsBowerMailError):
        provider.wait_for_code(primary)
    child = mail.create_smsbower_child(primary)
    monkeypatch.setattr(provider, "get_code", lambda mail_id: "222222")
    assert provider.wait_for_code(child) == "222222"
    set_status.assert_called_once_with(primary["mail_id"], 5)


def test_wait_timings_start_at_wait_entry_and_stop_is_not_timeout(pair, monkeypatch):
    provider, primary, _ = pair
    token = metrics.begin_attempt(primary)
    primary["_otp_requested_monotonic"] = -1000
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])

    def received(mail_id):
        clock[0] += 7.25
        return "456789"

    monkeypatch.setattr(provider, "get_code", received)
    assert provider.wait_for_code(primary) == "456789"
    assert provider.wait_for_code(primary, stop_check=lambda: True) is None
    summary = metrics.get_summary()
    assert summary["otp_wait"] == {"received_count": 1, "timeout_count": 0,
                                   "average_seconds": 7.25, "min_seconds": 7.25, "max_seconds": 7.25}
    # Replaying the same metrics callback has no effect.
    assert token
    assert summary["totals"]["in_progress"] == 1


def test_only_real_wait_expiry_records_timeout_and_sets_account_marker(pair, monkeypatch):
    provider, primary, _ = pair
    metrics.begin_attempt(primary)
    provider.wait_timeout = 2
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(provider, "get_code", lambda mail_id: "invalid")

    def next_poll(*args):
        clock[0] += 1
        return True

    monkeypatch.setattr(provider, "_sleep_until_next_poll", next_poll)
    assert provider.wait_for_code(primary) is None
    summary = metrics.get_summary()
    assert summary["otp_wait"]["timeout_count"] == 1
    assert summary["otp_wait"]["average_seconds"] is None
    assert primary["_registration_outcome"] == "otp_timeout"


def test_metrics_terminal_deferral_and_sticky_registered_marker(pair):
    _, primary, _ = pair
    token = metrics.begin_attempt(primary)
    mail.prepare_smsbower_pair(primary)
    mail.mark_mailbox_result(primary, success=False, error="验证码超时")
    assert primary["_registration_outcome"] == "otp_timeout"
    mail.mark_mailbox_result(primary, success=True)
    assert primary["_registration_outcome"] == "success"
    assert metrics.get_summary()["totals"]["in_progress"] == 1
    metrics.finish_attempt(token, "success")
    assert metrics.get_summary()["totals"]["success"] == 1
    primary["_registration_outcome"] = "already_registered"
    mail.mark_mailbox_result(primary, success=True)
    assert primary["_registration_outcome"] == "already_registered"


def test_standalone_marker_records_registered_and_preserves_retirement_on_cancel_failure(pair):
    _, primary, set_status = pair
    primary["_registration_metrics_defer_terminal"] = False
    primary["_registration_outcome"] = "already_registered"
    metrics.begin_attempt(primary)
    set_status.side_effect = mail.SmsBowerMailError("temporary failure")
    mail.mark_mailbox_result(primary, success=False, error="already_registered")
    assert metrics.get_summary()["totals"]["already_registered"] == 1
    assert mail._load_state()[primary["address"]]["state"] == "used"


@pytest.mark.parametrize("success", [True, False])
def test_consumed_mailbox_cleanup_with_error_does_not_count_success(pair, success):
    _, primary, set_status = pair
    primary["_registration_metrics_defer_terminal"] = False
    primary["_otp_accepted_by_server"] = True
    metrics.begin_attempt(primary)
    mail.mark_mailbox_result(primary, success=success, error="registration_incomplete")
    assert primary["_registration_outcome"] == "other_failed"
    assert metrics.get_summary()["totals"]["other_failed"] == 1
    assert metrics.get_summary()["totals"]["success"] == 0
    set_status.assert_called_once_with(primary["mail_id"], 3)
    assert mail._load_state()[primary["address"]]["state"] == "used"


@pytest.mark.parametrize("register_child,slots", [(True, 2), (False, 1), ("false", 1)])
def test_capacity_counts_physical_mailboxes_separately_from_registration_slots(monkeypatch, register_child, slots):
    entry = {"type": "smsbower_mail", "enable": True, "api_key": "fixture", "register_child": register_child}
    monkeypatch.setattr(mail, "_known_mailbox_bindings", lambda: {})
    monkeypatch.setattr(mail, "reconcile_registered_mailbox_states", lambda *args, **kwargs: 0)
    monkeypatch.setattr(mail.SmsBowerMailProvider, "availability_summary", lambda self: {"count": 4})
    stats = mail.count_pool_capacity({"providers": [entry]}, provider_type="smsbower_mail")
    assert stats["total"] == 4
    assert stats["total_slots"] == stats["available"] == 4 * slots
    assert stats["primary_available"] == 4
    assert stats["split_available"] == 4 * (slots - 1)
    provider = mail.SmsBowerMailProvider(entry, mail._make_config({}))
    try:
        assert provider._settlement_mailbox("box@gmail.com", "id", "gmail.com")["register_child"] is (slots == 2)
    finally:
        provider.close()


def test_non_sms_consumption_hook_only_marks_acceptance_without_state_write():
    mailbox = {"provider": "icloud_api", "address": "box@icloud.com"}
    mail.mark_smsbower_otp_accepted(mailbox)
    assert mailbox["_otp_accepted_by_server"] is True
    assert not mail.STATE_FILE.exists()


@pytest.mark.parametrize("source", ["known_account", "known_split", "used_state"])
def test_local_registered_primary_is_cancelled_once_and_propagates_structured_skip(pair, monkeypatch, source):
    provider, primary, set_status = pair
    address, activation_id = primary["address"], "new-local-known-activation"
    get_activation = Mock(return_value={"mail": address, "mailId": activation_id})
    monkeypatch.setattr(provider, "get_activation", get_activation)
    get_code = Mock(side_effect=AssertionError("registered mail must not read codes"))
    monkeypatch.setattr(provider, "get_code", get_code)
    if source == "used_state":
        mail._set_state(address, "used", "existing account")
    else:
        known_address = address if source == "known_account" else mail.split_mailbox_address(address)
        monkeypatch.setattr(mail, "_known_mailbox_bindings", lambda: {known_address: "registered_account"})
    before = dict(mail._load_state()[address])
    fallback = Mock()
    build = Mock(side_effect=[provider, fallback])
    monkeypatch.setattr(mail, "_build_provider", build)
    monkeypatch.setattr(mail, "reconcile_registered_mailbox_states", lambda *a, **k: 0)
    config = {"providers": [{"type": "smsbower_mail", "enable": True}, {"type": "smsbower_mail", "enable": True}]}
    with pytest.raises(mail.SmsBowerMailError) as caught:
        mail.create_mailbox(config, provider_type="smsbower_mail")
    assert caught.value.business_error == "already_registered"
    assert caught.value.activation_address == address
    assert caught.value.activation_id == activation_id
    assert caught.value.activation_outcome_uncertain is False
    assert "private-pair-api-key" not in str(caught.value)
    set_status.assert_called_once_with(activation_id, 2)
    get_activation.assert_called_once()
    get_code.assert_not_called()
    build.assert_called_once()
    fallback.create_mailbox.assert_not_called()
    assert mail._load_state()[address] == before


def test_local_registered_primary_cancel_failure_retains_pending_and_uncertain_error(pair, monkeypatch):
    provider, primary, set_status = pair
    address, activation_id = primary["address"], "new-uncertain-activation"
    mail._set_state(address, "used")
    monkeypatch.setattr(provider, "get_activation", lambda domain: {"mail": address, "mailId": activation_id})
    set_status.side_effect = mail.SmsBowerMailError("cancellation response lost")
    with pytest.raises(mail.SmsBowerMailError) as caught:
        provider.create_mailbox()
    assert caught.value.activation_outcome_uncertain is True
    assert caught.value.business_error != "already_registered"
    set_status.assert_called_once_with(activation_id, 2)
    entry = mail._load_state()[address]
    assert entry["state"] == "used"
    assert entry["smsbower_pending_status"] == 2
    assert entry["smsbower_pending_mail_id"] == activation_id
    assert "private-pair-api-key" not in mail.STATE_FILE.read_text()


@pytest.mark.parametrize("state_name", ["in_use", "failed", "token_invalid"])
def test_other_unavailable_primary_states_remain_ordinary_errors(pair, monkeypatch, state_name):
    provider, primary, set_status = pair
    address, activation_id = primary["address"], "new-unavailable-activation"
    mail._set_state(address, state_name)
    monkeypatch.setattr(provider, "get_activation", lambda domain: {"mail": address, "mailId": activation_id})
    with pytest.raises(RuntimeError) as caught:
        provider.create_mailbox()
    assert not isinstance(caught.value, mail.SmsBowerMailError)
    set_status.assert_called_once_with(activation_id, 2)
    assert mail._load_state()[address]["state"] == state_name

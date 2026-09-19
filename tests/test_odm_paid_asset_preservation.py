from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
import requests

import mail


@pytest.fixture(autouse=True)
def isolated_mail_state(tmp_path, monkeypatch):
    monkeypatch.setattr(mail, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(mail, "_known_mailbox_bindings", lambda: {})
    monkeypatch.setattr(requests.sessions.Session, "request", Mock(side_effect=AssertionError("No external HTTP")))
    monkeypatch.setattr(mail, "_record_mail_registration_outcome", Mock())


def paid_mailbox(**overrides):
    return {
        "provider": "online_disposablemail",
        "provider_ref": "online_disposablemail#1",
        "address": "paid@example.test",
        "order_id": "fixture-order-1",
        "mail_id": "fixture-order-1",
        "api_base": "https://mail.example.test",
        "service_id": "242",
        "email_type_id": "26",
        **overrides,
    }


def seed(mailbox=None, **fields):
    mailbox = mailbox or paid_mailbox()
    entry = {**mailbox, "state": "in_use", "reason": "", "updated_at": "2000-01-01T00:00:00+00:00", **fields}
    mail._save_state({mailbox["address"]: entry})
    return mailbox


def provider(monkeypatch):
    monkeypatch.setattr(mail, "_http_session", Mock(return_value=Mock()))
    result = mail.OnlineDisposableMailProvider({
        "provider_ref": "online_disposablemail#1", "api_base": "https://mail.example.test",
        "api_key": "fixture-private-key", "service_id": "242", "email_type_id": "26",
    }, {"request_timeout": 8, "wait_interval": 0.2})
    result.buy_mailbox = Mock(return_value={"address": "paid@example.test", "order_id": "fixture-order-1"})
    return result


def journal():
    path = mail.STATE_FILE.parent / "online_disposablemail_orders.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("release", [
    lambda box: mail._release_state(box["address"], any_state=True),
    mail.release_mailbox,
    lambda _: mail.release_all_in_use(),
    lambda _: mail.release_failed_mailboxes(),
    lambda _: mail.release_stale_in_use(),
    lambda box: mail.update_mailbox_states([box["address"]], action="release"),
])
def test_every_release_path_preserves_paid_binding_without_making_it_available(release):
    box = seed()
    release(box)
    saved = mail._load_state()[box["address"]]
    assert saved["order_id"] == "fixture-order-1"
    assert saved["mail_id"] == "fixture-order-1"
    assert saved["provider_ref"] == box["provider_ref"]
    assert saved["state"] == "failed"
    assert saved["paid_order_review_required"] is True
    assert not mail._entry_available(saved)


@pytest.mark.parametrize("error", [
    "chatgpt_csrf_http_403: cloudflare_challenge", "proxy connection timeout",
    "cancelled", "rate_limit_exceeded", "验证码超时", "some registration failure",
])
def test_registration_failures_retain_paid_order_for_review(error):
    box = seed()
    mail.mark_mailbox_result(box, success=False, error=error)
    mail.release_mailbox(box)
    saved = mail._load_state()[box["address"]]
    assert saved["order_id"] == box["order_id"]
    assert saved["state"] == "failed"
    assert saved["paid_order_review_required"] is True
    assert not mail._entry_available(saved)


@pytest.mark.parametrize("state,previous", [("used", ""), ("in_use", "used")])
@pytest.mark.parametrize("operation", [
    lambda box: mail._release_state(box["address"], any_state=True),
    lambda box: mail.mark_mailbox_result(box, success=False, error="rate_limit_exceeded"),
    lambda box: mail.update_mailbox_states([box["address"]], action="cooldown"),
    lambda box: mail.update_mailbox_states([box["address"]], action="release"),
])
def test_used_order_never_downgrades_on_failure_or_panel_release(state, previous, operation):
    box = seed(state=state, _prev_state=previous)
    operation(box)
    saved = mail._load_state()[box["address"]]
    assert saved["state"] == "used"
    assert saved["order_id"] == box["order_id"]
    assert not mail._entry_available(saved)


@pytest.mark.parametrize("flag", ["_otp_accepted_by_server", "_registration_identity_created"])
@pytest.mark.parametrize("operation", [
    mail.release_mailbox,
    lambda box: mail.mark_mailbox_result(box, success=False, error="cancelled"),
])
def test_confirmed_identity_or_accepted_otp_is_preserved_as_used(flag, operation):
    box = seed()
    box[flag] = True
    operation(box)
    assert mail._load_state()[box["address"]]["state"] == "used"


def test_received_code_without_remote_acceptance_stays_under_review():
    box = seed()
    box["_registration_code_received"] = True
    mail.mark_mailbox_result(box, success=False, error="timeout")
    saved = mail._load_state()[box["address"]]
    assert saved["state"] == "failed"
    assert saved["paid_order_review_required"]


def test_explicit_existing_account_recovery_can_read_retained_order_without_buying(monkeypatch):
    box = seed(state="failed", paid_order_review_required=True)
    instance = provider(monkeypatch)
    instance._latest_code_payload = Mock(return_value={"code": None})
    recovered = instance.create_mailbox(box["address"], allow_used=True)
    assert recovered["address"] == box["address"]
    assert recovered["order_id"] == box["order_id"]
    instance.buy_mailbox.assert_not_called()
    instance._latest_code_payload.assert_called_once_with(box["order_id"])
    mail.release_mailbox(recovered)
    assert not mail._entry_available(mail._load_state()[box["address"]])


def test_purchase_journal_is_written_before_duplicate_account_rejection(monkeypatch):
    instance = provider(monkeypatch)
    monkeypatch.setattr(mail, "_known_mailbox_bindings", lambda: {"paid@example.test": "existing-account"})
    with pytest.raises(mail.OnlineDisposableMailError, match="already bound"):
        instance.create_mailbox()
    row, = journal()
    assert row["address"] == "paid@example.test"
    assert row["order_id"] == "fixture-order-1"
    assert row["event"] == "purchased"
    assert not mail._load_state()


def test_purchase_journal_retains_conflicting_order_and_original_binding(monkeypatch):
    original = seed(state="used")
    instance = provider(monkeypatch)
    instance.buy_mailbox.return_value["order_id"] = "fixture-new-order"
    with pytest.raises(RuntimeError, match="unavailable"):
        instance.create_mailbox()
    assert journal()[0]["order_id"] == "fixture-new-order"
    assert mail._load_state()[original["address"]]["order_id"] == original["order_id"]


def test_journal_whitelist_excludes_keys_passwords_codes_and_url_credentials():
    mail._record_odm_paid_order(paid_mailbox(
        _odm_api_key="fixture-secret", api_key="fixture-secret", password="fixture-password",
        _odm_seen_codes=["123456"], code="123456", content="private mail body",
        api_base="https://fixture-user:fixture-secret@mail.example.test:8443/path?key=fixture-secret",
    ))
    row, = journal()
    assert set(row) == {"event", "recorded_at", "provider", "provider_ref", "api_base", "address", "order_id", "service_id", "email_type_id"}
    assert row["api_base"] == "https://mail.example.test:8443"
    serialized = json.dumps(row)
    for forbidden in ("fixture-secret", "fixture-password", "123456", "private mail body", "fixture-user"):
        assert forbidden not in serialized


def test_journal_failure_aborts_before_reservation_and_does_not_try_another_provider(monkeypatch):
    instance = provider(monkeypatch)
    instance._reserve_address = Mock()
    monkeypatch.setattr(mail, "_record_odm_paid_order", Mock(side_effect=OSError("fixture disk failure")))
    monkeypatch.setattr(mail, "reconcile_registered_mailbox_states", Mock())
    monkeypatch.setattr(mail, "_iter_provider_entries", Mock(return_value=[{}, {}]))
    build = Mock(return_value=instance)
    monkeypatch.setattr(mail, "_build_provider", build)
    with pytest.raises(mail.OnlineDisposableMailError) as caught:
        mail.create_mailbox({})
    assert caught.value.business_error == "paid_order_persistence_failed"
    assert caught.value.order_id == "fixture-order-1"
    assert caught.value.address == "paid@example.test"
    assert build.call_count == 1
    instance.buy_mailbox.assert_called_once()
    instance._reserve_address.assert_not_called()


def test_state_write_failure_preserves_journal_and_reports_paid_order_failure(monkeypatch):
    instance = provider(monkeypatch)
    monkeypatch.setattr(mail, "_save_state_unlocked", Mock(side_effect=OSError("fixture disk failure")))
    with pytest.raises(mail.OnlineDisposableMailError) as caught:
        instance.create_mailbox()
    assert caught.value.business_error == "paid_order_persistence_failed"
    assert journal()[0]["order_id"] == "fixture-order-1"


def test_new_purchase_does_not_automatically_reuse_retained_order(monkeypatch):
    instance = provider(monkeypatch)
    first = instance.create_mailbox()
    mail.mark_mailbox_result(first, success=False, error="cloudflare_challenge")
    instance.buy_mailbox.return_value = {"address": "different@example.test", "order_id": "fixture-order-2"}
    second = instance.create_mailbox()
    assert second["address"] == "different@example.test"
    assert instance.buy_mailbox.call_count == 2
    assert {row["order_id"] for row in journal()} == {"fixture-order-1", "fixture-order-2"}
    assert mail._load_state()[first["address"]]["paid_order_review_required"]


def test_concurrent_purchases_keep_complete_independent_journal_rows():
    boxes = [paid_mailbox(address=f"fixture-{i}@example.test", order_id=f"order-{i}") for i in range(24)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(mail._record_odm_paid_order, boxes))
    assert {row["order_id"] for row in journal()} == {box["order_id"] for box in boxes}


def test_release_behavior_for_unpaid_provider_is_unchanged():
    box = seed({"provider": "outlook_token", "address": "ordinary@example.test"})
    mail._release_state(box["address"])
    assert box["address"] not in mail._load_state()


@pytest.mark.parametrize("detail", [
    "chatgpt_csrf_http_403: html_access_denied", "chatgpt_csrf_http_503: cloudflare_challenge",
    "Cloudflare challenge rejected this request",
    "ChatGPT authorize clearance 重试仍被拦截，clearance 刷新失败或重试后仍失败: status=403, body=<!DOCTYPE html><title>Just a moment...</title>",
])
def test_clear_upstream_block_requests_halt_of_new_paid_purchases(detail):
    box = seed()
    mail.mark_mailbox_result(box, success=False, error=detail)
    assert box["_paid_order_purchase_halt_reason"] == "cloudflare_challenge"
    assert mail._load_state()[box["address"]]["order_id"] == box["order_id"]


@pytest.mark.parametrize("detail", [
    "OnlineDisposableMail latest code HTTP 403", "authorize_continue_http_403: permission_denied",
    "chatgpt_csrf_http_500: timeout", "registration_disallowed", "验证码超时",
    "ChatGPT authorize clearance 重试仍被拦截: status=403, body={\"error\":\"permission_denied\"}",
    "authorize_continue status=403, body=<!DOCTYPE html><title>Just a moment...</title>",
])
def test_other_business_or_provider_failures_do_not_claim_cloudflare_block(detail):
    box = seed()
    mail.mark_mailbox_result(box, success=False, error=detail)
    assert "_paid_order_purchase_halt_reason" not in box

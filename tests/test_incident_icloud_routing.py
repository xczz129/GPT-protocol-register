"""Regression coverage for parent inbox auth and split recipient isolation."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

import mail


PARENT = "parent@icloud.test"
CHILD_A = "parent+one@icloud.test"
CHILD_B = "parent+two@icloud.test"
KEY = "fixture-parent-mail-key"


def _response(payload, status=200):
    response = MagicMock(status_code=status, text="")
    response.json.return_value = payload
    return response


def _provider(lines=None):
    provider = mail.ICloudApiProvider(
        {"mailboxes": lines or f"{PARENT}----{KEY}", "api_base": "https://mail.example.test"},
        {"request_timeout": 5, "wait_timeout": 2, "wait_interval": 0.2,
         "user_agent": "test", "message_limit": 10},
    )
    provider.session.close()
    provider.session = MagicMock()
    return provider


def _message(address, code, received_at=None):
    return {
        "to": address, "alias_email": PARENT, "code": code,
        "subject": "Verification code",
        "received_at": (received_at or datetime.now(timezone.utc)).isoformat(),
    }


def test_child_allocation_keeps_registration_identity_and_parent_credential(tmp_path, monkeypatch):
    monkeypatch.setattr(mail, "STATE_FILE", tmp_path / "state.json")
    provider = _provider(f"{CHILD_A}----obsolete-child-key\n{PARENT}----{KEY}")
    mailbox = provider.create_mailbox(CHILD_A)
    assert mailbox["address"] == CHILD_A
    assert mailbox["login_address"] == PARENT
    assert mailbox["base_address"] == PARENT
    assert mailbox["api_key"] == KEY
    assert mail._load_state_unlocked()[CHILD_A]["state"] == "in_use"
    assert PARENT not in mail._load_state_unlocked()


@pytest.mark.parametrize("extra", [{}, {"login_address": PARENT}, {"base_address": PARENT}])
def test_both_endpoints_authenticate_parent_and_keep_time_boundary(extra):
    provider = _provider()
    provider.session.get.return_value = _response({"ok": True, "messages": []})
    boundary = datetime.now(timezone.utc)
    mailbox = {"address": CHILD_A, "api_key": KEY, "_code_not_before": boundary, **extra}
    provider._request_code(mailbox, timeout=5, exclude=["123456"])
    provider._request_messages(mailbox, exclude=["123456"])
    for request in provider.session.get.call_args_list:
        params = request.kwargs["params"]
        assert params["email"] == PARENT
        assert params["key"] == KEY
        assert params["after"] == int(boundary.timestamp()) - 2
        assert params["exclude"] == "123456"
    assert mailbox["address"] == CHILD_A


@pytest.mark.parametrize("method", ["_request_code", "_request_messages"])
def test_unrelated_inbox_is_rejected_before_network(method):
    provider = _provider()
    mailbox = {"address": CHILD_A, "login_address": "other@icloud.test", "api_key": KEY}
    kwargs = {"exclude": []}
    if method == "_request_code":
        kwargs["timeout"] = 5
    with pytest.raises(mail.ICloudApiError, match="does not match"):
        getattr(provider, method)(mailbox, **kwargs)
    provider.session.get.assert_not_called()


@pytest.mark.parametrize("payload", [
    {"code": "111222"},
    {"code": "111222", "email": CHILD_A},
    {"code": "111222", "mail": {"alias_email": PARENT}},
    {"code": "111222", "mail": {"to": PARENT}},
    {"code": "111222", "mail": {"to": CHILD_B}},
    {"code": "111222", "mail": {"to": f"{CHILD_A}, {CHILD_B}"}},
])
def test_split_never_trusts_parent_or_ambiguous_code(payload):
    assert mail._icloud_payload_code_candidates(payload, {"address": CHILD_A}) == []


@pytest.mark.parametrize("recipient", [CHILD_A, f"Child <{CHILD_A.upper()}>",
                                         [PARENT, CHILD_A], [{"address": CHILD_A}]])
def test_split_accepts_explicit_recipient_with_parent_storage_owner(recipient):
    message = _message(recipient, "222333")
    candidates = mail._icloud_payload_code_candidates(
        {"code": "222333", "mail": message}, {"address": CHILD_A},
    )
    assert [candidate[0] for candidate in candidates] == ["222333"]
    assert candidates[0][2] is not None


def test_parent_rejects_forwarded_child_message_even_if_parent_also_in_to():
    payload = {"code": "222333", "mail": _message([PARENT, CHILD_A], "222333")}
    assert mail._icloud_payload_code_candidates(payload, {"address": PARENT}) == []


@pytest.mark.parametrize("recipient", [
    repr([CHILD_A]),
    repr([f"Child <{CHILD_A}>"]),
    f"{[CHILD_A]!r} {[PARENT]!r}",
    f"[] {[CHILD_A]!r} []",
])
def test_legacy_python_list_recipient_fields_preserve_child_ownership(recipient):
    payload = {"code": "222333", "mail": _message(recipient, "222333")}
    assert [row[0] for row in mail._icloud_payload_code_candidates(payload, {"address": CHILD_A})] == ["222333"]
    assert mail._icloud_payload_code_candidates(payload, {"address": CHILD_B}) == []
    assert mail._icloud_payload_code_candidates(payload, {"address": PARENT}) == []


@pytest.mark.parametrize("recipient", [
    f"[{CHILD_A}]", f"[{{'email': '{CHILD_A}'}}]", f"[['{CHILD_A}']]",
    f"['{CHILD_A}'] + ['{PARENT}']", f"['{CHILD_A}'] ignored suffix",
    repr([CHILD_A] * 65), "[" + " " * 8192 + "]",
])
def test_malformed_or_unbounded_recipient_lists_do_not_establish_ownership(recipient):
    payload = {"code": "222333", "mail": _message(recipient, "222333")}
    assert mail._icloud_payload_code_candidates(payload, {"address": CHILD_A}) == []
    assert mail._icloud_payload_code_candidates(payload, {"address": PARENT}) == []


def test_body_or_echoed_email_never_supplies_missing_recipient_evidence():
    payload = {"email": CHILD_A, "code": "222333", "mail": {
        "alias_email": PARENT, "subject": "Verification code", "code": "222333",
        "body_text": f"Hello {CHILD_A}, code 222333", "preview": repr([CHILD_A]),
    }}
    assert mail._icloud_payload_code_candidates(payload, {"address": CHILD_A}) == []


def test_concurrent_parent_and_children_each_receive_only_their_code():
    codes = {PARENT: "111222", CHILD_A: "222333", CHILD_B: "333444"}
    messages = [_message(address, code) for address, code in codes.items()]

    def receive(address):
        provider = _provider()
        # The provider's fast endpoint returns its shared latest parent code.
        # Split tasks must read the parent cache and select their own recipient.
        def request(url, **kwargs):
            assert kwargs["params"]["email"] == PARENT
            assert kwargs["params"]["key"] == KEY
            if url.endswith("/code/wait"):
                return _response({"ok": True, "code": "111222", "mail": messages[0]})
            return _response({"ok": True, "messages": messages})
        provider.session.get.side_effect = request
        mailbox = {"address": address, "api_key": KEY,
                   "_code_not_before": datetime.now(timezone.utc) - timedelta(seconds=1)}
        result = provider.wait_for_code(mailbox)
        return address, result, mailbox["_seen_otp_codes"]

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(receive, codes))
    for address, result, seen in results:
        assert result == codes[address]
        assert seen == [codes[address]]


def test_filtered_cache_still_rejects_seen_and_old_codes():
    provider = _provider()
    now = datetime.now(timezone.utc)
    stale = _message(CHILD_A, "111222", now - timedelta(minutes=3))
    used = _message(CHILD_A, "222333", now)
    fresh = _message(CHILD_A, "333444", now)
    provider._request_code = MagicMock(return_value={"ok": True, "code": "111222", "mail": stale})
    provider._request_messages = MagicMock(return_value={"ok": True, "messages": [stale, used, fresh]})
    mailbox = {"address": CHILD_A, "api_key": KEY, "_code_not_before": now,
               "_seen_otp_codes": ["222333"]}
    assert provider.wait_for_code(mailbox) == "333444"
    assert mailbox["_seen_otp_codes"] == ["222333", "333444"]


@pytest.mark.parametrize("status", [401, 403])
def test_invalid_key_or_access_denied_stops_after_first_request_and_redacts(status):
    provider = _provider()
    provider.session.get.return_value = _response({"ok": False, "error": f"API Key invalid: {KEY}"}, status)
    with pytest.raises(mail.ICloudApiError) as raised:
        provider.wait_for_code({"address": CHILD_A, "api_key": KEY})
    assert KEY not in str(raised.value)
    assert str(status) in str(raised.value)
    provider.session.get.assert_called_once()


def test_cache_unauthorized_stops_instead_of_silent_polling():
    provider = _provider()
    provider.session.get.side_effect = [
        _response({"ok": True, "code": ""}),
        _response({"ok": False, "error": "API Key invalid"}, 401),
    ]
    with pytest.raises(mail.ICloudApiError, match="401"):
        provider.wait_for_code({"address": CHILD_A, "api_key": KEY})
    assert provider.session.get.call_count == 2


def test_network_errors_do_not_expose_query_key(monkeypatch):
    provider = _provider()
    monkeypatch.setattr(mail.time, "sleep", lambda _: None)
    provider.session.get.side_effect = RuntimeError(f"request failed https://mail.example.test/?key={KEY}&email={PARENT}")
    with pytest.raises(mail.ICloudApiError) as raised:
        provider._request_code({"address": CHILD_A, "api_key": KEY}, timeout=5, exclude=[])
    assert KEY not in str(raised.value)
    assert raised.value.__suppress_context__

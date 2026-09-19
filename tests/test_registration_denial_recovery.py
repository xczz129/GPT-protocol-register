"""Saved registration progress must not trigger new auth after an explicit refusal."""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from proxy_util import is_rate_limit_error, is_registration_access_denied
from test_network_retry_classification import registration_scheduler


@pytest.mark.parametrize("engine,signal", [
    ("protocol", "exception"),
    ("browser", "exception"),
    ("browser", "event"),
])
@pytest.mark.parametrize("error", [
    'chatgpt_csrf_http_403: {"body_summary":"html_access_denied"}',
    'chatgpt_csrf_http_503: {"body_summary":"cloudflare_challenge"}',
    "HTTP 429: rate_limit_exceeded",
])
def test_saved_password_and_owned_account_are_preserved_without_automatic_recovery(
    registration_scheduler, monkeypatch, engine, signal, error,
):
    observed = registration_scheduler
    module = observed.protocol if engine == "protocol" else observed.browser
    email = observed.addresses[0]
    mailbox = {
        "provider": "outlook_token", "address": email,
        "_registration_identity_created": True, "_otp_accepted_by_server": True,
    }
    saved = {
        "email": email, "account_id": "owned-account-fixture",
        "password": "saved-private-password", "password_set": True,
        "totp_secret": "saved-private-totp", "registration_state": "security_pending",
    }
    writes, attempts = [], []

    def persist(record, *_args, **_kwargs):
        writes.append(dict(record))
        return dict(record)

    monkeypatch.setattr(module, "upsert_account_record", persist)

    if engine == "protocol":
        registrar = Mock()
        registrar.device_id = "device-fixture"
        registrar.fingerprint = {}
        registrar.registration_mailbox = mailbox

        def register(index, **kwargs):
            attempts.append(index)
            kwargs["partial_sink"](dict(saved))
            raise RuntimeError(error)

        registrar.register.side_effect = register
        monkeypatch.setattr(module, "PlatformRegistrar", Mock(return_value=registrar))
    else:
        async def register(index, *_args, **kwargs):
            attempts.append(index)
            kwargs["partial_sink"](dict(saved), mailbox)
            if signal == "exception":
                raise RuntimeError(error)
            kwargs["task_event"](index, "failed", error=error)
            return None

        monkeypatch.setattr(module, "register_one", register)

    assert observed.run(engine) == []

    assert attempts == [1]
    observed.selectors[engine].assert_called_once()
    # The shared fixture guards recovery, health, providers and auth network.
    observed.blocked.assert_not_called()
    assert writes
    assert writes[-1]["email"] == email
    assert writes[-1]["account_id"] == saved["account_id"]
    assert writes[-1]["password"] == saved["password"]
    assert writes[-1]["totp_secret"] == saved["totp_secret"]
    assert writes[-1]["registration_state"] == "security_pending"
    assert any(
        status == "failed" and (
            is_registration_access_denied(fields.get("error"))
            or is_rate_limit_error(fields.get("error"))
        )
        for _, status, fields in observed.events
    )
    assert not any(
        "准备密码登录补全" in str(fields.get("step", ""))
        or "切换下一条代理" in str(fields.get("step", ""))
        for _, _, fields in observed.events
    )

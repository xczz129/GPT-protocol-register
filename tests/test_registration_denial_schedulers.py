"""Exercise real schedulers without allowing signup or mailbox network I/O."""
from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest

import mail
from register import batch, protocol


@pytest.mark.parametrize("engine", ["protocol", "browser"])
@pytest.mark.parametrize("denial", [
    "chatgpt_csrf_http_403: status=403 api_error_code=access_denied",
    "chatgpt_home_http_403: body_summary=cloudflare_challenge",
    "chatgpt_home_http_503: body_summary=cloudflare_challenge",
])
def test_explicit_denial_stops_signup_retries(monkeypatch, engine, denial):
    address = "owned@example.test"
    for module in (mail, batch):
        monkeypatch.setattr(module, "count_pool_capacity", lambda *a, **kw: {
            "total": 1, "available": 1, "emails": [address],
        })
        monkeypatch.setattr(module, "release_stale_in_use", lambda: 0)
    monkeypatch.setattr(mail.requests.sessions.Session, "request", Mock(side_effect=AssertionError("network forbidden")))
    calls, events = [], []

    def attempt(index, *args, **kwargs):
        calls.append(index)
        kwargs["task_event"](index, "failed", email=address, error=denial, step="upstream refused")
        return None

    async def browser_attempt(index, *args, **kwargs):
        return attempt(index, *args, **kwargs)

    monkeypatch.setattr(protocol, "register_one", attempt)
    monkeypatch.setattr(batch, "register_one", browser_attempt)
    settings = {
        "mail": {"providers": [{"type": "icloud", "enable": True}]},
        "registration": {"engine": engine, "total": 1, "threads": 1,
            "max_transient_retries": 3, "browser_protocol_security_after_register": False,
            "post_register_health_check": False},
        "proxy": {"enabled": False},
    }
    run = protocol.run_protocol_register if engine == "protocol" else batch.run_register
    records = asyncio.run(run(settings, total=1, concurrency=1, provider_type="icloud",
        fixed_email=address, task_event=lambda i, status, **fields: events.append((status, fields))))
    assert records == []
    assert calls == [1]
    assert any(status == "failed" and fields.get("error") == denial for status, fields in events)
    assert not any("切换" in fields.get("step", "") for _, fields in events)

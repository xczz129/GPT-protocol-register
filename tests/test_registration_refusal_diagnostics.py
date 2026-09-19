"""Offline regressions for refusal classification, bounded requests and traces."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from common import registration_trace
from proxy_util import (
    is_cloudflare_block_error,
    is_proxy_transport_error,
    is_registration_access_denied,
)


@pytest.mark.parametrize("message", [
    'chatgpt_home_http_403: json={"error":{"code":"access_denied"}}',
    'chatgpt_csrf_http_403: {"body_summary":"html_access_denied"}',
    "HTTP 403: access denied; connection closed",
    'upstream rejected: {"http_status":403}',
])
def test_upstream_403_is_not_invented_cloudflare_or_transport_failure(message):
    assert is_registration_access_denied(message)
    assert not is_cloudflare_block_error(message)
    assert not is_proxy_transport_error(message)


@pytest.mark.parametrize("message", [
    'chatgpt_csrf_http_403: {"body_summary":"cloudflare_challenge"}',
    'chatgpt_csrf_http_503: {"body_summary":"cloudflare_challenge"}',
    'HTTP 503 {"error_category":"edge_blocked"}',
    "Cloudflare clearance 重试仍被拦截: status=503",
    "被 Cloudflare 拦截: status=403",
])
def test_explicit_cloudflare_refusals_stop_without_transport_retry(message):
    assert is_cloudflare_block_error(message)
    assert is_registration_access_denied(message)
    assert not is_proxy_transport_error(message)


@pytest.mark.parametrize("message", [
    "Failed to perform, curl: (56) CONNECT tunnel failed, response 403",
    "curl: (56) Tunnel connection failed: 403 Forbidden",
    "proxy tunnel rejected, response 403",
])
def test_proxy_connect_refusal_remains_distinct_from_upstream_403(message):
    assert not is_registration_access_denied(message)
    assert is_proxy_transport_error(message)


@pytest.mark.parametrize("message", [
    "403", "attempt 403 failed", "https://proxy.example.test:403/",
    "request_id=fixture403", "HTTP 4030", "status_code=4030",
    "HTTP 503 unavailable", "cloudflare documentation",
])
def test_numeric_labels_and_generic_503_do_not_prove_access_denied(message):
    assert not is_registration_access_denied(message)


def test_wrapped_response_preserves_upstream_refusal_over_transport_wording():
    upstream = RuntimeError("request rejected")
    upstream.response = SimpleNamespace(status_code=403)
    wrapped = RuntimeError("proxy connection closed")
    wrapped.__cause__ = upstream
    assert is_registration_access_denied(wrapped)
    assert not is_proxy_transport_error(wrapped)


def _response(status, *, challenge=False):
    return SimpleNamespace(
        status_code=status,
        headers={"content-type": "text/html", "cf-ray": "9c46e92173621717-HKG"},
        url="https://chatgpt.com/api/auth/csrf?code=private-grant&state=private-state",
        text="<title>Just a moment...</title>" if challenge else "<html>Forbidden private-body</html>",
        json=Mock(side_effect=ValueError("not JSON")),
    )


@pytest.mark.parametrize("status,challenge", [(403, False), (429, False), (503, True)])
def test_http_helper_returns_refusal_before_any_retry(monkeypatch, status, challenge):
    from register import protocol_http

    reply = _response(status, challenge=challenge)
    session = SimpleNamespace(request=Mock(return_value=reply))
    sleep = Mock(side_effect=AssertionError("refusals must not be retried"))
    monkeypatch.setattr(protocol_http.time, "sleep", sleep)
    response, error = protocol_http.request_with_local_retry(
        session, "GET", reply.url, retry_attempts=5,
    )
    assert response is reply and error == ""
    session.request.assert_called_once()
    sleep.assert_not_called()


def test_http_exception_with_explicit_403_does_not_retry(monkeypatch):
    from register import protocol_http

    error = RuntimeError("request rejected")
    error.response = SimpleNamespace(status_code=403)
    session = SimpleNamespace(request=Mock(side_effect=error))
    sleep = Mock(side_effect=AssertionError("refusals must not be retried"))
    monkeypatch.setattr(protocol_http.time, "sleep", sleep)
    response, detail = protocol_http.request_with_local_retry(
        session, "GET", "https://example.test/action", retry_attempts=5,
    )
    assert response is None and is_registration_access_denied(detail)
    assert "request rejected" in detail
    session.request.assert_called_once()


@pytest.mark.parametrize("status,challenge", [(403, False), (429, False), (503, True)])
def test_first_csrf_refusal_is_preserved_without_homepage_or_clearance(monkeypatch, status, challenge):
    import register.protocol_registrar as module

    registrar = module.PlatformRegistrar.__new__(module.PlatformRegistrar)
    registrar.fingerprint = {}
    registrar.proxy = ""
    registrar.clearance_user_agent = ""
    registrar.session = Mock()
    registrar._refresh_cloudflare_clearance = Mock(side_effect=AssertionError("refusal must stop"))
    monkeypatch.setattr(module, "step", Mock())
    monkeypatch.setattr(module, "_browser_fingerprint", Mock(return_value={"accept_language": "en", "user_agent": "synthetic"}))
    monkeypatch.setattr(module, "_client_hint_headers", Mock(return_value={}))
    monkeypatch.setattr(module, "_headers_with_clearance", lambda headers, *_args: headers)
    request = Mock(return_value=(_response(status, challenge=challenge), ""))
    monkeypatch.setattr(module, "request_with_local_retry", request)

    with pytest.raises(RuntimeError, match=f"chatgpt_csrf_http_{status}") as failure:
        registrar._complete_chatgpt_session("member@example.test", "synthetic", 1)
    request.assert_called_once()
    registrar._refresh_cloudflare_clearance.assert_not_called()
    assert "private-" not in str(failure.value)
    assert "9c46e92173621717-HKG" in str(failure.value)


@pytest.mark.parametrize("code", ["access_denied", "invalid_auth_step", "rate_limit_exceeded"])
def test_trace_preserves_allowlisted_public_codes(code):
    assert registration_trace._safe({"error": {"code": code}})["error"]["code"] == code
    assert code in registration_trace._redact(f"code={code}")
    assert code in registration_trace._redact(json.dumps({"error": {"code": code}}))
    assert code in registration_trace._redact(f"api_error_code={code}")


@pytest.mark.parametrize("error", ["registration_access_denied: request rejected", "rate_limit_exceeded: request rejected"])
def test_csrf_summary_preserves_wrapped_refusal_without_echoing_private_error(error):
    from register.protocol_registrar import _csrf_failure_message
    from proxy_util import is_rate_limit_error

    message = _csrf_failure_message(None, error + " token=private-token")
    assert "private-token" not in message
    assert is_registration_access_denied(message) or is_rate_limit_error(message)


def test_trace_writer_redacts_json_query_and_nested_secrets_before_disk():
    secrets = {
        "password": "private-password", "code": "private-grant",
        "accessToken": "private-access-token", "csrfToken": "private-csrf",
        "totpSecret": "private-totp", "apiKey": "private-api-key",
    }
    diagnostic = "provider failed json=" + json.dumps(secrets)
    diagnostic += " callback=https://example.test/callback?code=access_denied&state=private-state"
    trace = registration_trace.RegistrationTrace(1, fixed_email="member@example.test")
    trace.event("failed", error=diagnostic, metadata=secrets, code="access_denied")
    trace.log_line(diagnostic)
    trace.finish("failed", error=diagnostic, persist_operation=False)
    content = trace.path.read_text(encoding="utf-8")
    assert not any(value in content for value in [*secrets.values(), "private-state"])
    rows = [json.loads(line) for line in content.splitlines()]
    event = next(row for row in rows if row["type"] == "event")
    assert event["code"] == "access_denied"
    assert set(event["metadata"].values()) == {"***"}
    assert "code=access_denied" not in content

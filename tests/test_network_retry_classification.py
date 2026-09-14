from __future__ import annotations

import pytest
from types import SimpleNamespace

from codex_oauth.import_jobs import _is_retryable_network_error
from proxy_util import is_proxy_transport_error, is_rate_limit_error


@pytest.mark.parametrize(
    "message",
    [
        "curl: (35) TLS connect error",
        (
            "Failed to perform, curl: (35) BoringSSL SSL_connect: Connection "
            "closed abruptly (SSL_ERROR_SYSCALL; error queue empty) in "
            "connection to sentinel.openai.com:443"
        ),
        "SSL 27: proxy tunnel failed",
        "certificate verify failed",
        "unexpected EOF while reading",
        "connection reset by peer",
    ],
)
def test_proxy_and_tls_failures_are_retryable(message: str) -> None:
    assert is_proxy_transport_error(message)
    assert _is_retryable_network_error(message)


@pytest.mark.parametrize("message", ["invalid password", "account_deactivated: confirmed"])
def test_credential_and_account_failures_are_not_retryable(message: str) -> None:
    assert not _is_retryable_network_error(message)


@pytest.mark.parametrize(
    "message",
    [
        "auth_step_error_page: code=rate_limit_exceeded request_id=fixture-id",
        "pending_recovery_failed: auth_rate_limit_exceeded",
        '{"error": {"code": "too_many_requests"}}',
        "HTTP 429 /api/accounts/authorize",
        "HTTP/1.1 429 Too Many Requests",
        "HTTP/2 429",
        "HTTP Error 429: Too Many Requests",
        "account_check_http_429",
        "HTTP status code: 429",
        "http_status_429",
        '{"http_status": 429}',
        '{"status_code": "429"}',
        "response status: 429",
        "status=429",
        "<Response [429]>",
        "429 Client Error: Too Many Requests for url: https://example.test/",
        "Client error '429 Too Many Requests' for url 'https://example.test/'",
        "HTTP 429: proxy connection aborted; retry denied",
    ],
)
def test_explicit_rate_limits_are_not_proxy_transport_failures(message: str) -> None:
    assert is_rate_limit_error(message)
    assert not is_proxy_transport_error(message)
    assert not _is_retryable_network_error(message)


@pytest.mark.parametrize(
    "message",
    [
        None,
        "",
        "429",
        "attempt 429 failed",
        "invalid password for account 429",
        "proxy.example.test:429",
        "https://proxy.example.test:429/path",
        "HTTP connection timeout to proxy.example.test:429",
        "HTTP 4290",
        "status_code=4290",
        "response [4291]",
        "http://429.example.test/",
        "request_id=fixture429",
        "curl: (35) connection closed abruptly to proxy.example.test:429",
    ],
)
def test_numbers_and_ports_do_not_imply_rate_limits(message: str | None) -> None:
    assert not is_rate_limit_error(message)


def test_rate_limit_status_can_be_preserved_on_wrapped_http_exception() -> None:
    class ErrorResponse:
        status_code = 429

        def __bool__(self):
            return False

    upstream = RuntimeError("request rejected")
    upstream.response = ErrorResponse()
    wrapped = RuntimeError("proxy operation failed")
    wrapped.__cause__ = upstream
    assert is_rate_limit_error(wrapped)
    assert not is_proxy_transport_error(wrapped)
    assert not _is_retryable_network_error(wrapped)


@pytest.mark.parametrize("attribute", ["status", "status_code"])
def test_http_exception_status_attribute_is_recognized(attribute: str) -> None:
    error = RuntimeError("request rejected")
    setattr(error, attribute, "429")
    assert is_rate_limit_error(error)
    assert not is_proxy_transport_error(error)
    assert not _is_retryable_network_error(error)


def test_non_429_response_does_not_hide_a_real_tls_failure() -> None:
    error = RuntimeError("curl: (35) TLS connect error")
    error.response = SimpleNamespace(status_code=502)
    assert not is_rate_limit_error(error)
    assert is_proxy_transport_error(error)


@pytest.mark.parametrize('suffix', ['SSL connection closed', 'TLS handshake', 'timeout', 'gateway timeout'])
def test_network_fallback_does_not_override_a_rate_limit(suffix: str) -> None:
    assert not _is_retryable_network_error('HTTP 429: ' + suffix)


def test_network_fallback_preserves_response_status_before_string_conversion() -> None:
    error = RuntimeError('proxy SSL connection aborted')
    error.response = SimpleNamespace(status_code=429)
    assert not _is_retryable_network_error(error)

from __future__ import annotations

import pytest

from codex_oauth.import_jobs import _is_retryable_network_error
from proxy_util import is_proxy_transport_error


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

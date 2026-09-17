from __future__ import annotations

import asyncio
import pytest
from types import SimpleNamespace
from unittest.mock import Mock

from codex_oauth.import_jobs import _is_retryable_network_error
from proxy_util import is_auth_identity_error, is_proxy_transport_error, is_rate_limit_error


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


@pytest.mark.parametrize('error', [
    'username_not_found',
    'password_reset_send_otp_http_400, detail={"error":{"code":"username_not_found"}}',
    'username_not_found: curl: (35) SSL connection closed abruptly',
    'connection reset by peer; code=username_not_found',
    {'error': {'code': 'username_not_found', 'message': 'Username not found.'}},
])
def test_explicit_missing_identity_is_not_a_proxy_transport_failure(error):
    assert is_auth_identity_error(error)
    assert not is_proxy_transport_error(error)


@pytest.mark.parametrize('error', [
    None, '', 'HTTP 400', 'password_reset_send_otp_http_400',
    'Username not found.', 'username_not_found@example.test',
    'prefix_username_not_found', 'username_not_found_suffix',
    {'error': {'code': 'invalid_request', 'message': 'Username not found.'}},
])
def test_http_400_and_similar_text_do_not_prove_missing_identity(error):
    assert not is_auth_identity_error(error)


def missing_identity_error(kind):
    if kind == 'late-code':
        return RuntimeError('upstream context: ' + 'x' * 350 + ' username_not_found')
    upstream = RuntimeError('upstream request rejected')
    upstream.response = SimpleNamespace(
        status_code=400, json=Mock(return_value={'error': {'code': 'username_not_found'}}),
    )
    if kind == 'response-json':
        return upstream
    wrapped = RuntimeError('curl: (35) SSL connection closed abruptly')
    wrapped.__cause__ = upstream
    return wrapped


@pytest.mark.parametrize('kind', ['response-json', 'wrapped-cause', 'late-code'])
def test_missing_identity_survives_typed_response_wrapping_and_long_messages(kind):
    error = missing_identity_error(kind)
    assert is_auth_identity_error(error)
    assert not is_proxy_transport_error(error)


def test_http_400_without_identity_code_does_not_hide_a_real_connection_failure():
    error = RuntimeError('curl: (35) SSL connection closed abruptly')
    error.response = SimpleNamespace(
        status_code=400, json=Mock(return_value={'error': {'code': 'invalid_request'}}),
    )
    assert not is_auth_identity_error(error)
    assert is_proxy_transport_error(error)


@pytest.fixture
def registration_scheduler(monkeypatch, tmp_path):
    """Run real scheduler boundaries with no provider/auth/browser operations."""
    import mail
    from register import batch as browser
    from register import protocol, protocol_registrar, smsbower_batch

    addresses = ['candidate@example.test', 'spare-one@example.test', 'spare-two@example.test']
    proxies = ['http://proxy-one.example.test:8080', 'http://proxy-two.example.test:8080']
    blocked = Mock(side_effect=AssertionError('external side effect forbidden in scheduler test'))
    for module in (mail, browser):
        monkeypatch.setattr(module, 'count_pool_capacity', lambda *a, **kw: {
            'total': len(addresses), 'available': len(addresses), 'emails': addresses,
        })
        monkeypatch.setattr(module, 'release_stale_in_use', lambda: 0)
    selectors = {}
    for engine, module, selector in (
        ('protocol', protocol, '_protocol_proxy_for_task'),
        ('browser', browser, '_proxy_for_task'),
    ):
        monkeypatch.setattr(module, 'resolve_proxy_pool', lambda *a, **kw: list(proxies))
        selectors[engine] = Mock(wraps=getattr(module, selector))
        monkeypatch.setattr(module, selector, selectors[engine])
        monkeypatch.setattr(module, 'upsert_account_record', blocked)
        monkeypatch.setattr(module, 'recover_registration', blocked)
    monkeypatch.setattr(protocol, '_append_account', blocked)
    monkeypatch.setattr(protocol, 'check_account', blocked)
    monkeypatch.setattr(protocol, 'PlatformRegistrar', blocked)
    monkeypatch.setattr(browser, 'repair_security_one', blocked)
    monkeypatch.setattr(mail.requests.sessions.Session, 'request', blocked)
    for module in (mail, protocol_registrar, smsbower_batch):
        monkeypatch.setattr(module, 'create_mailbox', blocked)
    trace = Mock()
    trace.path = tmp_path / 'data' / 'registration_logs' / 'fixture.jsonl'
    trace.email = addresses[0]
    monkeypatch.setattr(protocol, 'RegistrationTrace', Mock(return_value=trace))
    events = []
    observed = SimpleNamespace(
        protocol=protocol, browser=browser, selectors=selectors, events=events,
        addresses=addresses, proxies=proxies, blocked=blocked,
    )

    def run(engine):
        config = {
            'mail': {},
            'registration': {'engine': engine, 'max_transient_retries': 3},
            'proxy': {'enabled': True, 'mode': 'pool', 'rotate_per_task': False},
        }
        runner = protocol.run_protocol_register if engine == 'protocol' else browser.run_register
        return asyncio.run(runner(
            config, total=1, concurrency=1, provider_type='outlook_token',
            task_event=lambda index, status=None, **fields: events.append((index, status, fields)),
            account_log_sink=Mock(),
        ))

    observed.run = run
    return observed


def assert_scheduler_did_not_rotate_or_replace(observed, engine, calls, capsys):
    assert len(calls) == 1
    assert calls[0] == (1, observed.proxies[0], observed.addresses[0])
    observed.selectors[engine].assert_called_once()
    observed.blocked.assert_not_called()
    assert any(status == 'failed' for _index, status, _fields in observed.events)
    assert not any(
        '切换' in str(fields.get('step', '')) or '代理重试' in str(fields.get('step', ''))
        for _index, _status, fields in observed.events
    )
    output = capsys.readouterr().out
    assert '冷却代理' not in output and '当前代理本批次冷却' not in output


@pytest.mark.parametrize('engine', ['protocol', 'browser'])
@pytest.mark.parametrize('message', [
    'username_not_found: curl: (35) SSL connection closed abruptly',
    'registration_password_route_unavailable: {"page_type":"email_otp_verification"}',
])
def test_terminal_identity_event_stops_scheduler_without_proxy_or_mailbox_retry(
    registration_scheduler, monkeypatch, capsys, engine, message,
):
    observed = registration_scheduler
    calls = []

    def failed(index, **kwargs):
        calls.append((index, kwargs.get('proxy'), kwargs.get('fixed_email')))
        kwargs['task_event'](index, 'failed', error=message)
        return None

    async def browser_failed(index, *_args, **kwargs):
        return failed(index, **kwargs)

    monkeypatch.setattr(observed.protocol, 'register_one', failed)
    monkeypatch.setattr(observed.browser, 'register_one', browser_failed)
    assert observed.run(engine) == []
    assert_scheduler_did_not_rotate_or_replace(observed, engine, calls, capsys)


@pytest.mark.parametrize('kind', ['response-json', 'wrapped-cause', 'late-code'])
def test_protocol_facade_preserves_identity_code_before_terminal_event_truncation(
    registration_scheduler, monkeypatch, capsys, kind,
):
    observed = registration_scheduler
    registrar = Mock()
    registrar.device_id = 'fixture-device'
    registrar.fingerprint = {}
    registrar.registration_mailbox = {'provider': 'outlook_token', 'address': observed.addresses[0]}
    registrar.register.side_effect = missing_identity_error(kind)
    monkeypatch.setattr(observed.protocol, 'PlatformRegistrar', Mock(return_value=registrar))
    calls = []
    original_register_one = observed.protocol.register_one

    def captured_register_one(index, **kwargs):
        calls.append((index, kwargs.get('proxy'), kwargs.get('fixed_email')))
        return original_register_one(index, **kwargs)

    monkeypatch.setattr(observed.protocol, 'register_one', captured_register_one)
    assert observed.run('protocol') == []
    registrar.register.assert_called_once()
    registrar.close.assert_called_once()
    assert registrar.registration_mailbox['_registration_identity_error'] == 'username_not_found'
    errors = [fields['error'] for _index, status, fields in observed.events if status == 'failed' and fields.get('error')]
    assert errors and all('username_not_found' in error for error in errors)
    assert all(len(error) <= 300 for error in errors)
    assert_scheduler_did_not_rotate_or_replace(observed, 'protocol', calls, capsys)

"""Offline HTTP metadata and per-request cooldown/stop guard boundaries."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codex_oauth import oauth


AUTHORIZE_PATH = '/api/accounts/authorize/continue'


def reply(status=200, *, headers=None, data=None, text=''):
    return SimpleNamespace(status_code=status, headers=headers or {},
                           json=Mock(return_value=data or {}), text=text)


@pytest.fixture
def client(monkeypatch):
    instance = oauth.ProtocolClient.__new__(oauth.ProtocolClient)
    instance.email = 'fixture@example.test'
    instance.timeout = 30
    instance.state = 'fixture-state'
    instance.challenge = 'fixture-challenge'
    instance.device_id = 'fixture-device'
    instance.navigation_id = 'fixture-navigation'
    instance.request_guard = None
    instance.auth_session_data = {}
    instance.last_auth_response = {}
    instance.session = SimpleNamespace(get=Mock(return_value=reply()), post=Mock(return_value=reply()))
    instance.sentinel_header = Mock(return_value='fixture-sentinel')
    instance._update_navigation = Mock()
    instance._sync_auth_cookies = Mock()
    monkeypatch.setattr(oauth, 'generate_requirements_token', Mock(return_value='fixture-requirement'))
    monkeypatch.setattr(oauth, 'build_sentinel_request_body', Mock(return_value='fixture-body'))
    monkeypatch.setattr(oauth, 'generate_sentinel_token', Mock(return_value='fixture-sentinel'))
    return instance


@pytest.mark.parametrize('status,path,retry_after', [
    (429, AUTHORIZE_PATH, '120'),
    (429, AUTHORIZE_PATH, 'Fri, 18 Sep 2026 11:00:00 GMT'),
    (429, AUTHORIZE_PATH, ''),
    (403, AUTHORIZE_PATH, ''),
    (400, '/api/accounts/password/verify', ''),
])
def test_post_error_keeps_original_runtime_text_and_only_scheduling_metadata(client, status, path, retry_after):
    client.session.post.return_value = reply(
        status, headers={'Retry-After': retry_after, 'Set-Cookie': 'private-cookie', 'Authorization': 'private-token'},
        text='synthetic refusal',
    )
    with pytest.raises(RuntimeError) as caught:
        client.post_json(path, {'password': 'private-password'})
    error = caught.value
    assert isinstance(error, oauth.OAuthHttpError)
    assert str(error) == f'HTTP {status} {path}: synthetic refusal'
    assert vars(error) == {'status_code': status, 'path': path, 'retry_after': retry_after}
    assert not hasattr(error, 'response') and not hasattr(error, 'headers')
    assert 'private' not in repr(vars(error))
    client.session.post.assert_called_once()


def test_lowercase_retry_after_header_is_preserved(client):
    client.session.post.return_value = reply(429, headers={'retry-after': '180'})
    with pytest.raises(oauth.OAuthHttpError) as caught:
        client.submit_email()
    assert caught.value.retry_after == '180'


def test_no_guard_preserves_single_account_post_and_response(client):
    response = client.session.post.return_value
    assert client.submit_email() is response
    client.sentinel_header.assert_called_once_with('authorize_continue')
    client.session.post.assert_called_once()


def test_constructor_accepts_optional_guard_without_invoking_it(monkeypatch):
    session = SimpleNamespace(cookies=Mock())
    monkeypatch.setattr(oauth, 'Session', Mock(return_value=session))
    guard = Mock()
    instance = oauth.ProtocolClient('fixture@example.test', request_guard=guard)
    assert instance.request_guard is guard and instance.session is session
    guard.assert_not_called()


@pytest.mark.parametrize('operation', ['submit', 'bootstrap', 'navigate', 'sentinel', 'consent', 'workspace'])
def test_stopped_guard_prevents_the_next_http_request(client, operation):
    client.request_guard = Mock(side_effect=RuntimeError('fixture stopped'))
    if operation == 'workspace':
        client.workspace_id = Mock(return_value='fixture-workspace')
        client.post_json = Mock(return_value=reply(data={'continue_url': '/fixture-redirect'}))
    actions = {
        'submit': client.submit_email,
        'bootstrap': client.bootstrap,
        'navigate': lambda: client.navigate('https://auth.openai.com/fixture'),
        'sentinel': lambda: oauth.ProtocolClient.sentinel_header(client, 'authorize_continue'),
        'consent': client.consent_probe,
        'workspace': client.select_workspace,
    }
    with pytest.raises(RuntimeError, match='fixture stopped'):
        actions[operation]()
    client.session.get.assert_not_called()
    client.session.post.assert_not_called()
    if operation == 'submit':
        client.sentinel_header.assert_not_called()


def test_guard_checks_again_after_sentinel_before_account_post(client):
    client.request_guard = Mock(side_effect=[None, RuntimeError('cooldown started during sentinel')])
    with pytest.raises(RuntimeError, match='cooldown started'):
        client.submit_email()
    client.sentinel_header.assert_called_once()
    client.session.post.assert_not_called()


@pytest.mark.parametrize('operation', ['navigate', 'bootstrap', 'workspace'])
def test_every_redirect_request_rechecks_guard(client, operation):
    calls = []
    client.request_guard = lambda: calls.append('guard')
    responses = [reply(302, headers={'location': '/fixture-next'}), reply()]
    if operation == 'bootstrap':
        client.consent_probe = Mock()
    if operation == 'workspace':
        client.workspace_id = Mock(return_value='fixture-workspace')
        client.post_json = Mock(return_value=reply(data={'continue_url': '/fixture-start'}))
        responses[-1] = reply(302, headers={'location': 'http://localhost:1455/auth/callback?code=fixture-code'})

    def get(*_args, **_kwargs):
        assert calls[-1] == 'guard'
        calls.append('http')
        return responses.pop(0)

    client.session.get.side_effect = get
    if operation == 'navigate':
        client.navigate('https://auth.openai.com/fixture')
    elif operation == 'bootstrap':
        client.bootstrap()
    else:
        assert 'fixture-code' in client.select_workspace()
    assert client.session.get.call_count == 2


def test_sentinel_retry_checks_guard_outside_transport_retry_handler(client, monkeypatch):
    client.request_guard = Mock(side_effect=[None, RuntimeError('stop during timeout wait')])
    client.session.post.side_effect = RuntimeError('curl: (35) SSL connect error')
    monkeypatch.setattr(oauth.time, 'sleep', Mock())
    with pytest.raises(RuntimeError, match='stop during timeout wait'):
        oauth.ProtocolClient.sentinel_header(client, 'authorize_continue')
    client.session.post.assert_called_once()
    assert client.request_guard.call_count == 2

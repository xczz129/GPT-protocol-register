from itertools import count
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from register import pending_recovery as recovery
from register.sessions import JobCancelled


def pending():
    return {'email':'owned@example.test','password':'fixture-password','password_set':True,'registration_state':'security_pending'}


def complete():
    return {**pending(),'access_token':'fixture-access','account_id':'fixture-id','totp_secret':'fixture-totp','mfa_enabled':True}


def test_blank_progress_preserves_credentials_and_confirmed_security():
    saved=complete()
    recovery.merge_registration_progress(saved,{'password':'','access_token':None,'totp_secret':'','mfa_enabled':False})
    assert saved==complete()
    recovery.merge_registration_progress(saved,{'access_token':'replacement-access'})
    assert saved['access_token']=='replacement-access'


def test_fresh_password_without_verified_email_is_not_automatic_login_eligible():
    mailbox={'_registration_identity_created':True}
    assert not recovery.can_recover_registration(pending(),mailbox)
    mailbox['_otp_accepted_by_server']=True
    assert recovery.can_recover_registration(pending(),mailbox)
    assert recovery.can_recover_registration(pending(),None)
    saved={**pending(),'registration_recovery_state':'awaiting_email_verification'}
    assert not recovery.can_recover_registration(saved,None)
    assert recovery.can_recover_registration(saved,mailbox)


def test_retry_uses_new_secret_saved_before_failure_and_does_not_emit_terminal_events(monkeypatch):
    calls, persisted, events=[],[],[]
    ticks=count(10,0.25)
    monkeypatch.setattr(recovery.time,'monotonic',lambda:next(ticks))
    monkeypatch.setattr(recovery.time,'sleep',lambda _:None)

    def attempt(index,**kwargs):
        calls.append(kwargs['account'])
        assert kwargs['max_attempts']==1
        if len(calls)==1:
            kwargs['partial_sink']({'totp_secret':'new-secret','mfa_enabled':True,'access_token':'new-access'})
            kwargs['task_event'](index,'failed',error='temporary connection failure')
            raise RuntimeError('connection reset by peer')
        assert kwargs['account']['totp_secret']=='new-secret'
        assert kwargs['account']['access_token']=='new-access'
        kwargs['task_event'](index,'success')
        return {**complete(),'totp_secret':'new-secret','access_token':'new-access'}

    monkeypatch.setattr(recovery,'_recover_once',attempt)
    result=recovery.recover_registration(1,account=pending(),mailbox=None,mail_config={},
        partial_sink=persisted.append,task_event=lambda index,status=None,**kw:events.append(status))
    assert result['registration_state']=='complete' and result['registration_recovery_attempts']==2
    assert len(calls)==2 and persisted
    assert set(events)=={'running'}


@pytest.mark.parametrize('message', ['invalid_password', 'invalid_username_or_password', 'pending_recovery_remote_totp_secret_missing'])
def test_permanent_password_failure_stops_without_erasing_credentials(monkeypatch, message):
    callback=Mock(side_effect=RuntimeError(message))
    monkeypatch.setattr(recovery,'_recover_once',callback)
    sleep = Mock()
    monkeypatch.setattr(recovery.time, 'sleep', sleep)
    account=pending()
    with pytest.raises(RuntimeError,match='registration_recovery_pending'):
        recovery.recover_registration(1,account=account,mailbox=None,mail_config={})
    callback.assert_called_once()
    sleep.assert_not_called()
    assert account==pending()


@pytest.mark.parametrize('message', [
    'username_not_found',
    'password_reset_send_otp_http_400, detail={"error":{"code":"username_not_found"}}',
    'pending_recovery_failed: username_not_found; connection reset by peer',
    pytest.param('upstream context: ' + 'x' * 350 + ' username_not_found', id='late-error-code'),
])
def test_unknown_username_stops_after_one_attempt_and_preserves_saved_credentials(
    monkeypatch, message,
):
    account = {
        **pending(), 'totp_secret': 'fixture-secret', 'mfa_enabled': True,
        'refresh_token': 'fixture-refresh',
    }
    original = dict(account)
    persisted = []
    callback = Mock(side_effect=RuntimeError(message))
    sleep = Mock()
    monkeypatch.setattr(recovery, '_recover_once', callback)
    monkeypatch.setattr(recovery.time, 'sleep', sleep)

    with pytest.raises(RuntimeError, match='username_not_found') as caught:
        recovery.recover_registration(
            1, account=account, mailbox=None, mail_config={},
            proxy='http://fixture-proxy:8080', partial_sink=persisted.append,
        )

    callback.assert_called_once()
    assert callback.call_args.kwargs['max_attempts'] == 1
    assert callback.call_args.kwargs['proxy'] == 'http://fixture-proxy:8080'
    sleep.assert_not_called()
    assert caught.value.__cause__ is callback.side_effect
    assert account == original
    saved = persisted[-1]
    assert saved['registration_recovery_attempts'] == 1
    assert saved['registration_state'] == 'security_pending'
    assert saved['registration_recovery_state'] == 'identity_unconfirmed'
    assert 'username_not_found' in saved['registration_recovery_error']
    assert not recovery.can_recover_registration(saved, None)
    for field in ('password', 'refresh_token', 'totp_secret', 'mfa_enabled'):
        assert saved[field] == original[field]

def test_password_route_unavailable_marks_recycled_identity_as_already_registered(monkeypatch):
    """密码路由不存在 => 回收邮箱身份，按已注册跳过（原为失败挂起）。"""
    from register.registration_outcome import RegisteredEmailDetected

    callback = Mock(side_effect=RuntimeError('pending_recovery_password_route_unavailable'))
    monkeypatch.setattr(recovery, '_recover_once', callback)
    sleep = Mock()
    monkeypatch.setattr(recovery.time, 'sleep', sleep)
    account = pending()
    mailbox = {'address': 'owned@example.test', 'provider': 'online_disposablemail'}
    with pytest.raises(RegisteredEmailDetected):
        recovery.recover_registration(1, account=account, mailbox=mailbox, mail_config={})
    callback.assert_called_once()
    sleep.assert_not_called()
    assert account == pending()
    assert mailbox['_registration_outcome'] == 'already_registered'



@pytest.mark.parametrize('message', [
    'auth_step_error_page: code=rate_limit_exceeded',
    'pending_recovery_failed: account_check_http_429',
    'HTTP 429: debug password=fixture-password',
    '{"http_status": 429, "error": "request rejected"}',
    'HTTP 429: proxy connection aborted',
])
def test_rate_limit_stops_auth_and_backoff_and_preserves_partial_credentials(monkeypatch, message):
    persisted, events = [], []
    account = {
        **pending(), 'totp_secret': 'fixture-totp', 'mfa_enabled': True,
        'refresh_token': 'fixture-refresh',
    }
    original = dict(account)
    sleep = Mock()
    monkeypatch.setattr(recovery.time, 'sleep', sleep)

    def limited(index, **kwargs):
        kwargs['partial_sink']({
            'password': '', 'totp_secret': '', 'mfa_enabled': False,
            'session_token': 'fixture-session-before-limit',
        })
        raise RuntimeError(message)

    callback = Mock(side_effect=limited)
    monkeypatch.setattr(recovery, '_recover_once', callback)
    with pytest.raises(RuntimeError, match='registration_recovery_pending') as caught:
        recovery.recover_registration(
            1, account=account, mailbox=None, mail_config={}, proxy='http://fixture-proxy:8080',
            partial_sink=persisted.append,
            task_event=lambda index, status=None, **fields: events.append((status, fields)),
        )
    callback.assert_called_once()
    assert callback.call_args.kwargs['max_attempts'] == 1
    assert callback.call_args.kwargs['proxy'] == 'http://fixture-proxy:8080'
    sleep.assert_not_called()
    saved = persisted[-1]
    assert saved['registration_state'] == 'security_pending'
    assert saved['registration_recovery_state'] == 'rate_limited'
    assert saved['registration_recovery_attempts'] == 1
    assert saved['password'] == account['password']
    assert saved['totp_secret'] == account['totp_secret']
    assert saved['refresh_token'] == account['refresh_token']
    assert saved['mfa_enabled'] is True
    assert saved['session_token'] == 'fixture-session-before-limit'
    assert 'fixture-password' not in saved['registration_recovery_error']
    assert 'fixture-password' not in str(caught.value)
    assert recovery.is_rate_limit_error(caught.value)
    assert events[-1][0] == 'running'
    assert events[-1][1]['error'] == 'auth_rate_limit_exceeded'
    assert '限流' in events[-1][1]['step']
    assert account == original


def test_rate_limit_http_status_survives_message_only_outer_layers(monkeypatch):
    error = RuntimeError('request rejected')
    error.response = SimpleNamespace(status_code=429)
    callback = Mock(side_effect=error)
    sleep = Mock()
    monkeypatch.setattr(recovery, '_recover_once', callback)
    monkeypatch.setattr(recovery.time, 'sleep', sleep)
    with pytest.raises(RuntimeError, match='rate_limit_exceeded') as caught:
        recovery.recover_registration(1, account=pending(), mailbox=None, mail_config={})
    callback.assert_called_once()
    sleep.assert_not_called()
    assert recovery.is_rate_limit_error(str(caught.value))


@pytest.mark.parametrize('account,mailbox',[
    ({'email':'other@example.test','password':'random-candidate'},{}),
    (pending(),{'_registration_outcome':'already_registered','_registration_identity_created':True}),
    ({**pending(),'registration_recovery_state':'identity_unconfirmed'},None),
    ({**pending(),'registration_recovery_error':'username_not_found'},None),
    ({**pending(),'registration_initial_error':'HTTP 400: username_not_found'},None),
    (pending(),{'_registration_identity_error':'username_not_found',
                '_registration_identity_created':True,'_otp_accepted_by_server':True}),
])
def test_ineligible_identity_guard_cannot_be_bypassed(monkeypatch,account,mailbox):
    callback=Mock()
    monkeypatch.setattr(recovery,'_recover_once',callback)
    with pytest.raises(RuntimeError,match='not_eligible'):
        recovery.recover_registration(1,account=account,mailbox=mailbox,mail_config={})
    callback.assert_not_called()


def test_stopped_recovery_never_starts_authentication(monkeypatch):
    callback=Mock()
    monkeypatch.setattr(recovery,'_recover_once',callback)
    with pytest.raises(JobCancelled):
        recovery.recover_registration(1,account=pending(),mailbox=None,mail_config={},stop_check=lambda:True)
    callback.assert_not_called()

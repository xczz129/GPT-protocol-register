from itertools import count
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


def test_permanent_password_failure_stops_without_erasing_credentials(monkeypatch):
    callback=Mock(side_effect=RuntimeError('invalid_password'))
    monkeypatch.setattr(recovery,'_recover_once',callback)
    account=pending()
    with pytest.raises(RuntimeError,match='registration_recovery_pending'):
        recovery.recover_registration(1,account=account,mailbox=None,mail_config={})
    callback.assert_called_once()
    assert account==pending()


@pytest.mark.parametrize('account,mailbox',[
    ({'email':'other@example.test','password':'random-candidate'},{}),
    (pending(),{'_registration_outcome':'already_registered','_registration_identity_created':True}),
])
def test_registered_skip_guard_cannot_be_bypassed(monkeypatch,account,mailbox):
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

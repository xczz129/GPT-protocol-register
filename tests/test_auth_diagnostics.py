"""Offline response diagnostics; no auth request, mailbox, or code acquisition."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from register.auth_diagnostics import auth_route_diagnostic, read_auth_response
from register.protocol_registrar import PlatformRegistrar
from register.registration_outcome import RegisteredEmailDetected


def response(value=None, *, error=None, content_type='application/json'):
    return SimpleNamespace(status_code=200, headers={'content-type':content_type},
                           json=Mock(side_effect=error, return_value=value))


@pytest.mark.parametrize('value,kind',[(None,'json_non_object'),([], 'json_non_object'),
    ('private-body','json_non_object'),(False,'json_non_object'),({},'json_empty_object')])
def test_non_object_responses_keep_legacy_empty_dict_semantics(value,kind):
    reply=response(value)
    data, diagnostic=read_auth_response(reply)
    assert data=={}
    assert diagnostic['response_kind']==kind
    reply.json.assert_called_once_with()
    assert 'private-body' not in json.dumps(diagnostic)


def test_html_parse_failure_logs_its_type_without_body_or_exception():
    reply=response(error=ValueError('secret-html-body'),content_type='text/html; charset=utf-8')
    data, diagnostic=read_auth_response(reply)
    assert data=={}
    assert diagnostic=={'http_status':200,'content_type':'text/html','response_kind':'not_json'}
    assert 'secret' not in json.dumps(diagnostic)


def test_error_metadata_does_not_copy_secret_fields_or_untrusted_labels():
    data={'error':{'code':'unknown-private-code','type':'private-type','message':'secret-message'},
          'password':'private-password','otp':'123456','access_token':'private-token',
          'cookie':'private-cookie'}
    reply=response(data,content_type='private-content-type')
    parsed, diagnostic=read_auth_response(reply)
    assert parsed is data
    assert diagnostic['has_api_error'] is True
    assert diagnostic['api_error_code']==diagnostic['api_error_type']=='unrecognized'
    rendered=json.dumps(diagnostic)
    assert 'private' not in rendered and 'secret-message' not in rendered and '123456' not in rendered


@pytest.mark.parametrize('url,expected',[
    ('https://auth.openai.com/email-verification?code=secret-code','/email-verification'),
    ('https://auth.openai.com/secret-path','unrecognized'),
    ('https://private-host.test/log-in','untrusted_or_opaque'),
    ('https://name:secret@auth.openai.com/log-in','untrusted_or_opaque'),
    ('https://auth.openai.com/log-in#secret','untrusted_or_opaque'),
    ('https://[invalid','malformed'),
])
def test_route_diagnostics_omit_urls_and_opaque_path_data(url,expected):
    diagnostic=auth_route_diagnostic(url,'private-page-type')
    assert diagnostic=={'page_type':'unrecognized','route':expected}
    assert 'secret' not in json.dumps(diagnostic) and 'private' not in json.dumps(diagnostic)


@pytest.mark.parametrize('mode', ['passwordless_login', 'password_reset', 'passwordless_signup'])
@pytest.mark.parametrize('full_response', [False, True])
def test_identity_diagnostics_keep_only_fixed_modes_and_login_method_types(mode, full_response):
    payload = {
        'email_verification_mode': mode,
        'login_methods': [
            {'type': 'passwordless_otp', 'email': 'private@example.test'},
            {'type': 'password', 'credential': 'private-credential'},
        ],
        'otp': '123456', 'access_token': 'private-token',
    }
    data = {'page': {'type': 'email_otp_verification', 'payload': payload}} if full_response else payload
    diagnostic = auth_route_diagnostic(
        'https://auth.openai.com/email-verification?code=private-code',
        'email_otp_verification', auth_response=data,
    )
    assert diagnostic['email_verification_mode'] == mode
    assert set(diagnostic['login_methods']) == {'passwordless_otp', 'password'}
    assert diagnostic['auth_response_valid'] is True
    rendered = json.dumps(diagnostic)
    assert 'private' not in rendered and '123456' not in rendered


def test_identity_diagnostics_redact_unrecognized_mode_and_method_labels():
    diagnostic = auth_route_diagnostic(
        'https://auth.openai.com/email-verification', 'email_otp_verification',
        auth_response={'page': {'payload': {
            'email_verification_mode': 'private-mode',
            'login_methods': [{'type': 'private-method', 'token': 'private-token'}],
        }}},
    )
    assert diagnostic['email_verification_mode'] == 'unrecognized'
    assert diagnostic['login_methods'] == ['unrecognized']
    assert diagnostic['auth_response_valid'] is False
    assert 'private' not in json.dumps(diagnostic)


@pytest.fixture
def guard(monkeypatch):
    import register.protocol_registrar as module
    registrar=PlatformRegistrar.__new__(PlatformRegistrar)
    registrar.session=object()
    registrar.device_id='fixture-device'
    registrar.fingerprint={}
    registrar.proxy=''
    registrar.clearance_user_agent=''
    registrar._json_headers=Mock(return_value={})
    registrar._diagnostic=Mock()
    registrar._navigate_auth_step=Mock()
    registrar._reset_auth_cookies=Mock()
    registrar._platform_authorize=Mock()
    monkeypatch.setattr(module,'step',Mock())
    monkeypatch.setattr(module,'build_sentinel_token',Mock(return_value='fixture-value'))
    monkeypatch.setattr(module,'_headers_with_clearance',lambda headers,*args:headers)
    request=Mock()
    monkeypatch.setattr(module,'request_with_local_retry',request)
    mailbox={'provider':'smsbower_mail','address':'fixture@example.test'}
    return registrar,request,mailbox


@pytest.mark.parametrize('reply,kind,page',[
    (response(error=ValueError('private-html'),content_type='text/html'),'not_json','missing'),
    (response([]),'json_non_object','missing'),
    (response({'page':{'type':'email_otp_verification'},
        'continue_url':'https://auth.openai.com/email-verification'}),'json_object','email_otp_verification'),
    (response({'error':{'code':'invalid_state','message':'private-error'}}),'json_object','missing'),
    (response({'error':{'code':'invalid_auth_step'}}),'json_object','missing'),
    (response({'error':{'code':'invalid_state','type':'user_already_exists'}}),'json_object','missing'),
    (response({'error':{'code':'user-already-exists'}}),'json_object','missing'),
    (response({'page':{'type':'private-page'},'continue_url':'https://auth.openai.com/private-path'}),'json_object','unrecognized'),
    (response({'page':['private-page']}),'json_object','missing'),
    (response({'page':{'payload':['private-payload']}}),'json_object','missing'),
])
def test_ambiguous_responses_still_stop_with_safe_context(guard,reply,kind,page):
    registrar,request,mailbox=guard
    request.return_value=(reply,'')
    with pytest.raises(RuntimeError,match='registration_password_route_unavailable') as raised:
        registrar._check_fresh_registration(mailbox['address'],mailbox,{'final_url':'https://auth.openai.com/log-in'},1)
    context=json.loads(str(raised.value).split(': ',1)[1])
    assert context['response_kind']==kind and context['page_type']==page
    assert 'private' not in str(raised.value) and 'invalid_state' not in str(raised.value)
    assert 'private' not in str(registrar._diagnostic.call_args)
    from mail import _mail_registration_outcome
    assert _mail_registration_outcome(mailbox,False,str(raised.value))=='other_failed'
    assert not any(marker in str(raised.value) for marker in ('invalid_auth_step','user_already_exists','user-already-exists'))
    assert '_registration_identity_created' not in mailbox and '_registration_outcome' not in mailbox
    registrar._navigate_auth_step.assert_not_called()
    registrar._reset_auth_cookies.assert_not_called()
    assert request.call_count==1


@pytest.mark.parametrize('payload',[
    {'error':{'code':'user_already_exists'}},
])
def test_explicit_existing_identity_is_still_rejected(guard,payload):
    registrar,request,mailbox=guard
    request.return_value=(response(payload),'')
    with pytest.raises(RegisteredEmailDetected):
        registrar._check_fresh_registration(mailbox['address'],mailbox,
            {'final_url':'https://auth.openai.com/log-in'},1)


def test_existing_password_route_is_rejected_before_navigation(guard):
    """A password login page is an existing candidate, never a reset trigger."""
    registrar,request,mailbox=guard
    request.return_value=(response({'continue_url':'https://auth.openai.com/log-in/password',
        'page':{'type':'password_verification'}}),'')
    with pytest.raises(RegisteredEmailDetected) as raised:
        registrar._check_fresh_registration(mailbox['address'],mailbox,
            {'final_url':'https://auth.openai.com/log-in'},1)
    assert raised.value.email == mailbox['address']
    assert mailbox['_registration_outcome']=='already_registered'
    assert '_registration_existing_takeover_detail' not in mailbox
    assert not mailbox.get('_registration_identity_created')
    registrar._navigate_auth_step.assert_not_called()
    assert request.call_count==1


def test_existing_mfa_route_is_rejected_not_converted(guard):
    """Registration candidates at an MFA challenge are skipped."""
    registrar,request,mailbox=guard
    request.return_value=(response({'continue_url':'https://auth.openai.com/mfa-challenge',
        'page':{'type':'mfa_challenge'}}),'')
    with pytest.raises(RegisteredEmailDetected):
        registrar._check_fresh_registration(mailbox['address'],mailbox,
            {'final_url':'https://auth.openai.com/log-in'},1)


def test_otp_route_without_identity_mode_stops_without_repeating_auth(guard):
    """A verification page alone is not evidence that an account has no password."""
    registrar,request,mailbox=guard
    request.return_value=(response({'page':{'type':'email_otp_verification'},
        'continue_url':'https://auth.openai.com/email-verification?code=private-code'}),'')
    with pytest.raises(RuntimeError,match='registration_password_route_unavailable') as caught:
        registrar._check_fresh_registration(mailbox['address'],mailbox,
            {'final_url':'https://auth.openai.com/log-in'},1)
    assert '_registration_identity_created' not in mailbox
    assert '_registration_outcome' not in mailbox
    registrar._navigate_auth_step.assert_not_called()
    assert 'private-code' not in str(caught.value)
    assert request.call_count==1

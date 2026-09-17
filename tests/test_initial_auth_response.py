"""Pure offline parsing tests, using reduced anonymous HAR-shaped fixtures."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import register.initial_auth_response as module
from register.initial_auth_response import read_initial_auth_response


EMAIL = 'fixture+alias@example.test'
LAYOUT = 'routes/layouts/client-auth-session-layout/layout'


def table_for(value):
    """Encode the observed reference-table shape, with no original HAR data."""
    table = []

    def add(item):
        index = len(table)
        table.append(None)
        if isinstance(item, dict):
            encoded = {}
            for key, child in item.items():
                encoded[f'_{add(key)}'] = add(child)
            table[index] = encoded
        elif isinstance(item, list):
            table[index] = [add(child) for child in item]
        else:
            table[index] = item
        return index

    add(value)
    return table


def session(mode='passwordless_login', *, email=EMAIL):
    return {'email_verification_mode': mode, 'email': email,
            'username': {'kind': 'email', 'value': email},
            'is_missing_session': False, 'session_id': 'private-session',
            'access_token': 'private-token', 'password': 'private-password'}


def html_for(state=None, *, seed=None, root=None, table=None):
    if table is None:
        if root is None:
            layout = {'session': session() if state is None else state}
            if seed is not None:
                layout['seedCacheEntry'] = {'session': seed, 'checksum': 'private-checksum'}
            root = {'loaderData': {LAYOUT: layout}, 'actionData': None, 'errors': None}
        table = table_for(root)
    encoded = json.dumps(json.dumps(table) + '\n')
    return f'<html><script>window.__reactRouterContext.streamController.enqueue({encoded});</script></html>'


def reply(body='', *, data=None, content_type='text/html', status=200,
          url='https://auth.openai.com/email-verification?state=private-state'):
    decoder = Mock(return_value=data) if content_type.startswith('application/json') else Mock(side_effect=ValueError('private-response'))
    return SimpleNamespace(text=body, status_code=status, url=url,
                           headers={'content-type': content_type}, json=decoder)


def parse(body, **kwargs):
    return read_initial_auth_response(reply(body, **kwargs), expected_email=EMAIL)


@pytest.mark.parametrize('mode', ['passwordless_login', 'password_reset', 'passwordless_signup', 'onboarding'])
def test_har_shaped_loader_is_read_without_fabricating_page_type(mode):
    state = session(mode)
    data, meta = parse(html_for(state, seed=state))
    assert data == {'email_verification_mode': mode}
    assert meta == {'http_status': 200, 'content_type': 'text/html', 'response_kind': 'not_json',
                    'auth_state_source': 'html_loader', 'auth_payload_parsed': True,
                    'initial_auth_parse_status': 'html_loader_parsed', 'initial_auth_state_rejected': False}
    rendered = json.dumps([data, meta])
    assert 'private' not in rendered and EMAIL not in rendered and 'page' not in data


def test_only_explicit_login_methods_are_returned_with_private_fields_removed():
    state = session()
    state['login_methods'] = [{'type': 'passwordless_otp', 'token': 'private-method'}, {'type': 'password'}]
    data, _ = parse(html_for(state))
    assert data['login_methods'] == [{'type': 'password'}, {'type': 'passwordless_otp'}]
    assert 'private' not in json.dumps(data)


def test_json_object_identity_and_diagnostics_are_preserved():
    value = {'page': {'type': 'email_otp_verification', 'payload': {'email_verification_mode': 'passwordless_login'}}}
    response = reply(data=value, content_type='application/json; charset=utf-8')
    data, meta = read_initial_auth_response(response, expected_email=EMAIL)
    assert data is value
    assert meta['response_kind'] == 'json_object' and meta['auth_state_source'] == 'json'
    assert meta['auth_payload_parsed'] is True
    response.json.assert_called_once_with()


@pytest.mark.parametrize('value,kind', [({}, 'json_empty_object'), ([], 'json_non_object'), (None, 'json_non_object')])
def test_empty_or_nonobject_json_is_not_a_parsed_auth_payload(value, kind):
    data, meta = read_initial_auth_response(reply(data=value, content_type='application/json'), expected_email=EMAIL)
    assert data == {} and meta['response_kind'] == kind
    assert meta['auth_payload_parsed'] is False


@pytest.mark.parametrize('url,label', [
    ('http://auth.openai.com/email-verification', 'html_untrusted_url'),
    ('https://auth.openai.com:444/email-verification', 'html_untrusted_url'),
    ('https://user:private@auth.openai.com/email-verification', 'html_untrusted_url'),
    ('https://untrusted.example/email-verification', 'html_untrusted_url'),
    ('/email-verification', 'html_untrusted_url'),
    ('https://auth.openai.com/email-verification#private', 'html_untrusted_url'),
    ('https://auth.openai.com/other', 'html_unsupported_route'),
])
def test_html_is_never_inferred_on_an_untrusted_or_unobserved_url(url, label):
    data, meta = parse(html_for(), url=url)
    assert data == {} and meta['initial_auth_parse_status'] == label
    assert meta['initial_auth_state_rejected'] is False


@pytest.mark.parametrize('status', [400, 429, 500])
def test_http_error_body_is_not_parsed_as_successful_loader(status):
    data, meta = parse(html_for(), status=status)
    assert data == {} and meta['initial_auth_parse_status'] == 'http_not_successful'
    assert meta['http_status'] == status


def test_initial_identifier_loader_may_lack_identity_and_mode():
    data, meta = parse(html_for({'is_missing_session': False, 'original_screen_hint': 'signup'}),
                       url='https://auth.openai.com/log-in')
    assert data == {} and meta['auth_payload_parsed'] is False
    assert meta['initial_auth_parse_status'] == 'html_identifier_state'
    assert meta['initial_auth_state_rejected'] is False


@pytest.mark.parametrize('body', [
    '<script>const modes = {email_verification_mode:"passwordless_login"};</script>',
    '<script type="application/json">{"email_verification_mode":"passwordless_login"}</script>',
    '<div data-email_verification_mode="passwordless_login">signup</div>',
    '<script>const text = "window.__reactRouterContext.streamController.enqueue";</script>',
    '<script src="/fixture.js">window.__reactRouterContext.streamController.enqueue("[]");</script>',
])
def test_static_dictionaries_and_unrelated_script_content_are_not_auth_evidence(body):
    data, meta = parse(body)
    assert data == {} and meta['initial_auth_parse_status'] == 'html_loader_missing'
    assert meta['initial_auth_state_rejected'] is False


@pytest.mark.parametrize('tag', ['template', 'noscript', 'textarea', 'title', 'xmp'])
def test_inert_html_contents_are_not_current_loader_state(tag):
    data, meta = parse(f'<{tag}>{html_for()}</{tag}>')
    assert data == {} and meta['initial_auth_parse_status'] == 'html_loader_missing'


@pytest.mark.parametrize('mutate,label', [
    (lambda s: s.update(email='different@example.test', username={'kind': 'email', 'value': 'different@example.test'}), 'html_identity_mismatch'),
    (lambda s: s.update(email='fixture@example.test', username={'kind': 'email', 'value': 'fixture@example.test'}), 'html_identity_mismatch'),
    (lambda s: s.update(email='different@example.test'), 'html_identity_conflict'),
    (lambda s: (s.pop('email'), s.pop('username')), 'html_identity_missing'),
    (lambda s: s.update(email={'secret': 'private'}), 'html_identity_malformed'),
    (lambda s: s.update(username={'kind': 'phone', 'value': EMAIL}), 'html_identity_malformed'),
    (lambda s: s.update(email_verification_mode='private-mode'), 'html_mode_unsupported'),
    (lambda s: s.pop('email_verification_mode'), 'html_mode_missing'),
    (lambda s: s.update(is_missing_session=True), 'html_session_missing'),
    (lambda s: s.pop('is_missing_session'), 'html_session_missing'),
    (lambda s: s.update(login_methods=[{'type': 'private-method'}]), 'html_methods_unsupported'),
])
def test_bad_or_ambiguous_loader_states_are_rejected_without_secret_diagnostics(mutate, label):
    state = session()
    mutate(state)
    data, meta = parse(html_for(state))
    assert data == {} and meta['initial_auth_state_rejected'] is True
    assert meta['initial_auth_parse_status'] == label
    rendered = json.dumps(meta)
    assert 'private' not in rendered and EMAIL not in rendered and 'different' not in rendered


def test_identity_mismatch_on_identifier_route_is_still_rejected():
    data, meta = parse(html_for(session(email='different@example.test')), url='https://auth.openai.com/log-in')
    assert data == {} and meta['initial_auth_parse_status'] == 'html_identity_mismatch'
    assert meta['initial_auth_state_rejected'] is True


def test_identity_case_and_outer_whitespace_can_match_without_alias_removal():
    data, meta = parse(html_for(session(email=f' {EMAIL.upper()} ')))
    assert data['email_verification_mode'] == 'passwordless_login'
    assert meta['initial_auth_state_rejected'] is False


@pytest.mark.parametrize('seed', [session('onboarding'), session(email='different@example.test'), {'is_missing_session': False}])
def test_seed_cache_cannot_override_or_disagree_with_the_primary_state(seed):
    data, meta = parse(html_for(seed=seed))
    assert data == {} and meta['initial_auth_parse_status'] == 'html_seed_conflict'


def test_seed_without_primary_session_is_rejected():
    root = {'loaderData': {LAYOUT: {'seedCacheEntry': {'session': session()}}}}
    data, meta = parse(html_for(root=root))
    assert data == {} and meta['initial_auth_parse_status'] == 'html_session_missing'


def test_route_error_is_not_hidden_by_an_otherwise_valid_loader():
    root = {'loaderData': {LAYOUT: {'session': session()}}, 'errors': {'private': 'private-error'}}
    data, meta = parse(html_for(root=root))
    assert data == {} and meta['initial_auth_parse_status'] == 'html_route_error'
    assert 'private' not in json.dumps(meta)


def test_known_null_sentinel_is_accepted_only_on_nullable_fields():
    table = table_for({'loaderData': {LAYOUT: {'session': session(), 'seedCacheEntry': None}}, 'errors': None})
    for item in table:
        if isinstance(item, dict):
            for key in item:
                if table[int(key[1:])] in {'seedCacheEntry', 'errors'}:
                    item[key] = -5
    data, meta = parse(html_for(table=table))
    assert data == {'email_verification_mode': 'passwordless_login'}
    assert meta['initial_auth_state_rejected'] is False


def test_multiple_equal_states_are_accepted_but_different_states_are_rejected():
    good = html_for()
    data, _ = parse(good + good)
    assert data['email_verification_mode'] == 'passwordless_login'
    data, meta = parse(good + html_for(session('onboarding')))
    assert data == {} and meta['initial_auth_parse_status'] == 'html_state_conflict'


@pytest.mark.parametrize('reference,label', [(-1, 'html_reference_invalid'), (-5, 'html_reference_invalid'),
                                         (9999, 'html_reference_invalid'), (True, 'html_reference_invalid'),
                                         (0, 'html_reference_cycle')])
def test_invalid_loader_references_never_use_negative_python_indexing(reference, label):
    data, meta = parse(html_for(table=[{'_1': reference}, 'loaderData', session()]))
    assert data == {} and meta['initial_auth_parse_status'] == label
    assert meta['initial_auth_state_rejected'] is True


@pytest.mark.parametrize('change', [
    lambda text: text[:text.index('</script>')],
    lambda text: text.replace(');</script>', '); privateAction();</script>'),
    lambda text: text.replace('enqueue(', 'enqueue(unsafe('),
])
def test_truncated_or_executable_expressions_are_not_executed_or_partially_parsed(change):
    data, meta = parse(change(html_for()))
    assert data == {} and meta['initial_auth_parse_status'] == 'html_stream_malformed'


def test_duplicate_json_keys_and_duplicate_decoded_keys_are_rejected():
    chunk = '[{"_1":2,"_1":3},"loaderData",{},{}]'
    body = f'<script>{module._PREFIX}({json.dumps(chunk)});</script>'
    assert parse(body)[1]['initial_auth_parse_status'] == 'html_stream_malformed'
    table = [{'_1': 3, '_2': 3}, 'loaderData', 'loaderData', {}]
    assert parse(html_for(table=table))[1]['initial_auth_parse_status'] == 'html_reference_invalid'


@pytest.mark.parametrize('name,limit', [('_MAX_HTML_CHARS', 10), ('_MAX_CHUNK_CHARS', 10),
                                    ('_MAX_TABLE_ITEMS', 2), ('_MAX_VISITS', 2),
                                    ('_MAX_DEPTH', 2), ('_MAX_SCRIPTS', 0), ('_MAX_CHUNKS', 0)])
def test_parser_resource_limits_fail_closed(monkeypatch, name, limit):
    monkeypatch.setattr(module, name, limit)
    data, meta = parse(html_for())
    assert data == {} and meta['initial_auth_parse_status'] == 'html_limit_exceeded'
    assert meta['initial_auth_state_rejected'] is True


def test_opaque_unrelated_values_are_not_dereferenced():
    table = table_for({'loaderData': {LAYOUT: {'session': session()}}})
    for item in table:
        if isinstance(item, dict):
            for key in item:
                if table[int(key[1:])] == 'access_token':
                    item[key] = -99999
    data, _ = parse(html_for(table=table))
    assert data == {'email_verification_mode': 'passwordless_login'}

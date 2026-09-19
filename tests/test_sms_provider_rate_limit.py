"""Supplier 429 responses stop further payment and preserve confirmed work."""
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

from test_sms_workflow import account, config, fake_login, route


def typed_limit():
    error = RuntimeError('synthetic supplier refusal')
    error.response = SimpleNamespace(status_code=429)
    return error


def prepare_batch(route, monkeypatch):
    from common import registration_sms_followup

    monkeypatch.setattr(registration_sms_followup, 'existing_queue', lambda: None)
    monkeypatch.setattr(route, '_current_sms_record', lambda value: value)
    monkeypatch.setattr(route, '_current_relay_context', lambda _record, context: context)
    monkeypatch.setattr(route, '_relay_action', lambda *_args: {
        'action': 'reauthorize', 'reason': 'fixture', 'relay_status': 'missing', 'relay_file': '',
    })
    history = Mock()
    monkeypatch.setattr(route, '_append_rt_history', history)
    stop = route._reserve_batch('codex', 2, 'supplier-429-fixture')
    route._STATE['items'] = [
        {'email': account()['email'], 'status': 'pending'},
        {'email': 'next@example.invalid', 'status': 'pending'},
    ]

    def execute(email=account()['email']):
        return route._run_sms_record(account(email), config(3), 'supplier-429-fixture', stop, {}, {'strategy': 'auto'})

    return stop, history, execute


@pytest.mark.parametrize('error', [
    RuntimeError('HTTP 429: too_many_requests'),
    HTTPError('https://supplier.example.test', 429, 'Too Many Requests', {}, None),
])
def test_display_price_query_propagates_explicit_429(route, monkeypatch, error):
    monkeypatch.setattr(route, '_SMSBOWER_PRICE_CACHE', {'at': 0, 'map': {'6': {'1': '0.1'}}})
    query = Mock(side_effect=error)
    monkeypatch.setattr(route, '_smsbower_request', query)
    with pytest.raises(type(error)):
        route._smsbower_tier_prices('fixture-key', 'dr')
    query.assert_called_once()


def test_ordinary_display_price_error_still_uses_cached_quote(route, monkeypatch):
    cached = {'6': {'1': '0.1'}}
    monkeypatch.setattr(route, '_SMSBOWER_PRICE_CACHE', {'at': 0, 'map': cached})
    monkeypatch.setattr(route, '_smsbower_request', Mock(side_effect=RuntimeError('temporary price failure')))
    assert route._smsbower_tier_prices('fixture-key', 'dr') == cached


@pytest.mark.parametrize('response', ['exception', 'body'])
def test_first_number_429_never_tries_another_country_or_tier(route, monkeypatch, response):
    settings = route.SmsSettings.from_mapping(config(3))
    client = route._PanelSmsBowerClient(settings, [('6', ['1', '2'], ''), ('4', [], '')])
    monkeypatch.setattr(route, '_smsbower_tier_prices', lambda *_args: {})
    error = 'SMSBower HTTP 429: too_many_requests'
    request = Mock(side_effect=[route.SmsError(error) if response == 'exception' else error,
                               'ACCESS_NUMBER:should-not-buy:+15550000000'])
    monkeypatch.setattr(client, '_request', request)
    with pytest.raises(route.SmsError, match='429'):
        client.acquire()
    request.assert_called_once()


def test_ordinary_no_stock_can_try_next_configured_country(route, monkeypatch):
    client = route._PanelSmsBowerClient(route.SmsSettings.from_mapping(config()), [('6', [], ''), ('4', [], '')])
    monkeypatch.setattr(route, '_smsbower_tier_prices', lambda *_args: {})
    request = Mock(side_effect=[route.SmsError('SMSBower NO_NUMBERS'), 'ACCESS_NUMBER:fixture:+15550000000'])
    monkeypatch.setattr(client, '_request', request)
    assert client.acquire().country == 4
    assert request.call_count == 2


@pytest.mark.parametrize('step', ['prices', 'number'])
def test_supplier_429_is_first_failure_and_stops_next_account(route, monkeypatch, step):
    calls = fake_login(route, monkeypatch)
    stop, history, execute = prepare_batch(route, monkeypatch)
    client = route._PanelSmsBowerClient(route.SmsSettings.from_mapping(config(3)), [('6', [], ''), ('4', [], '')])
    number = Mock(side_effect=route.SmsError('SMSBower HTTP 429: too_many_requests'))
    monkeypatch.setattr(client, '_request', number)
    monkeypatch.setattr(route, '_sms_client', lambda *_args: client)
    monkeypatch.setattr(route, '_SMSBOWER_PRICE_CACHE', {'at': 0, 'map': {}})
    monkeypatch.setattr(route, '_smsbower_request', Mock(
        side_effect=typed_limit() if step == 'prices' else None, return_value='{}',
    ))
    first, second = execute(), execute('next@example.invalid')
    assert first['outcome'] == 'failed' and '429' in first['error']
    assert second['outcome'] == 'cancelled'
    assert stop.is_set() and calls.login == calls.closed == 1
    assert number.call_count == (0 if step == 'prices' else 1)
    assert calls.acquire == calls.send == 0
    assert history.call_count == 2
    route._finish_batch('supplier-429-fixture')
    assert route._STATE['status'] == 'failed'


@pytest.mark.parametrize('error', [RuntimeError('SMSBower HTTP 429'), typed_limit()])
def test_finish_429_keeps_verified_phone_but_stops_authorization(route, monkeypatch, error):
    calls = fake_login(route, monkeypatch)
    original_client = route._sms_client

    def client(*args):
        sms = original_client(*args)
        sms.finish = Mock(side_effect=error)
        return sms

    monkeypatch.setattr(route, '_sms_client', client)
    exchange = Mock(side_effect=AssertionError('no further authorization after supplier 429'))
    monkeypatch.setattr(route, 'exchange_code', exchange)
    stop, history, execute = prepare_batch(route, monkeypatch)
    first, second = execute(), execute('next@example.invalid')
    assert first['status'] == 'failed' and '429' in first['error']
    assert first['phone'] == '+15550000000'
    assert first['phone_attempts'][0]['result'] == 'success'
    assert second['status'] == 'cancelled'
    assert calls.acquire == calls.login == calls.otp == calls.closed == 1
    assert calls.cancelled == 0 and calls.workspaces == []
    exchange.assert_not_called()
    assert stop.is_set()
    assert history.call_args_list[0].args[1]['phone'] == '+15550000000'
    assert history.call_args_list[0].args[1]['phone_attempts'][0]['result'] == 'success'


def test_typed_relay_429_preserves_tokens_and_stops_batch(route, monkeypatch):
    calls = fake_login(route, monkeypatch)
    stop, _history, execute = prepare_batch(route, monkeypatch)
    monkeypatch.setattr(route, '_sync_cliproxy_auth', Mock(side_effect=typed_limit()))
    first, second = execute(), execute('next@example.invalid')
    assert first['status'] == 'failed' and '429' in first['error']
    assert first['authorization_status'] == 'success'
    assert first['refresh_token'] == 'test-rt'
    assert first['retry_action'] == 'sync_only'
    assert first['phone_attempts'][0]['result'] == 'success'
    assert second['status'] == 'cancelled' and stop.is_set()
    assert calls.login == calls.acquire == 1 and calls.cancelled == 0

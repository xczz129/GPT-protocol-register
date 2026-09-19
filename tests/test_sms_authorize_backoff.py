"""Authorization backoff uses virtual time and mocked login/payment boundaries."""
from email.utils import formatdate
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_sms_workflow import account, config, fake_login, route


def limited(retry_after=None):
    error = RuntimeError('HTTP 429 /api/accounts/authorize/continue: rate_limit_exceeded')
    if retry_after is not None:
        error.retry_after = retry_after
    return error


@pytest.fixture
def clock(route, monkeypatch):
    clock = SimpleNamespace(now=1000.0, waits=[], on_wait=None)
    stop = route._reserve_batch('codex', 1, 'backoff')
    route._STATE['items'] = [{'email': account()['email'], 'status': 'pending'}]
    monkeypatch.setattr(route.time, 'monotonic', lambda: clock.now)
    monkeypatch.setattr(route.time, 'time', lambda: clock.now + 1700000000)

    def wait(seconds):
        if clock.on_wait:
            clock.on_wait(seconds)
        clock.waits.append(seconds)
        clock.now += seconds
        return stop.is_set()

    monkeypatch.setattr(stop, 'wait', wait)
    clock.stop = stop
    return clock


@pytest.mark.parametrize('retry_after,expected', [(None, 90), ('2', 90), ('120', 120), ('invalid', 90), ('-1', 90)])
def test_auth_429_waits_then_retries_same_session_without_extra_number(route, monkeypatch, clock, retry_after, expected):
    calls = fake_login(route, monkeypatch)
    submissions, waiting = [], []

    def submit(client):
        submissions.append((id(client), clock.now))
        if len(submissions) == 1:
            raise limited(retry_after)

    def during_wait(_seconds):
        assert calls.acquire == 0 and calls.login == 1
        assert not clock.stop.is_set() and route._STATE['status'] == 'running'
        waiting.append(dict(route._STATE['items'][0]))

    clock.on_wait = during_wait
    monkeypatch.setattr(route.ProtocolClient, 'submit_email', submit)
    result = route._process_one(account(), config(0), batch_id='backoff', stop_event=clock.stop)
    assert result['status'] == 'success'
    assert submissions == [(submissions[0][0], 1000), (submissions[0][0], 1000 + expected)]
    assert calls.login == calls.closed == calls.acquire == 1
    assert len(result['phone_attempts']) == 1 and not clock.stop.is_set()
    assert waiting and all(item['stage'] == 'rate_limit_wait' and item['attempt'] == 0 for item in waiting)
    assert all('本账号提交邮箱返回 HTTP 429' in item['message'] for item in waiting)
    assert waiting[0]['wait_deadline'] == 1700001000 + expected
    assert route._STATE['items'][0]['wait_deadline'] is None


def test_repeated_429_starts_a_new_90_second_wait_each_time(route, monkeypatch, clock):
    calls = fake_login(route, monkeypatch)
    submitted = []

    def submit(client):
        submitted.append((id(client), clock.now))
        if len(submitted) <= 2:
            raise limited()

    monkeypatch.setattr(route.ProtocolClient, 'submit_email', submit)
    assert route._process_one(account(), config(), batch_id='backoff', stop_event=clock.stop)['status'] == 'success'
    assert [at for _, at in submitted] == [1000, 1090, 1180]
    assert len({identity for identity, _ in submitted}) == 1
    assert calls.acquire == calls.login == 1 and route._STATE['auth_retry_count'] == 2


def test_backoff_keeps_previously_acquired_unused_number(route, monkeypatch, clock):
    calls = fake_login(route, monkeypatch, send_failures=1, send_error='sign-in session is no longer valid')
    submissions = []

    def submit(client):
        submissions.append(id(client))
        if len(submissions) == 2:
            assert calls.acquire == 1
            raise limited()

    monkeypatch.setattr(route.ProtocolClient, 'submit_email', submit)
    result = route._process_one(account(), config(1), batch_id='backoff', stop_event=clock.stop)
    assert result['status'] == 'success' and len(result['phone_attempts']) == 1
    assert submissions[1] == submissions[2]
    assert calls.login == calls.closed == 2
    assert calls.acquire == 1 and calls.cancelled == 0


def test_stop_during_backoff_cancels_without_buying_or_retrying(route, monkeypatch, clock):
    calls = fake_login(route, monkeypatch)
    submissions = Mock(side_effect=limited())
    monkeypatch.setattr(route.ProtocolClient, 'submit_email', submissions)
    clock.on_wait = lambda _seconds: clock.stop.set()
    with pytest.raises(route._SmsCancelled):
        route._process_one(account(), config(), batch_id='backoff', stop_event=clock.stop)
    assert submissions.call_count == calls.login == calls.closed == 1
    assert calls.acquire == calls.cancelled == 0
    assert not route._STATE['rate_limited']


def test_batch_peer_waits_before_starting_auth_or_buying_number(route, monkeypatch, clock):
    calls = fake_login(route, monkeypatch)
    original = route.ProtocolClient.bootstrap
    starts = []
    monkeypatch.setattr(route.ProtocolClient, 'bootstrap', lambda client: (starts.append(clock.now), original(client))[-1])
    route._defer_authorize_retry('backoff', limited())
    clock.on_wait = lambda _seconds: (pytest.fail('number bought during cooldown') if calls.acquire else None)
    assert route._process_one(account(), config(), batch_id='backoff', stop_event=clock.stop)['status'] == 'success'
    assert starts == [1090] and calls.acquire == 1


def test_another_429_extends_shared_cooldown_for_waiting_peer(route, monkeypatch, clock):
    calls = fake_login(route, monkeypatch)
    route._defer_authorize_retry('backoff', limited())
    extended = []

    def during_wait(_seconds):
        if clock.now >= 1040 and not extended:
            extended.append(route._defer_authorize_retry('backoff', limited()))
        assert calls.acquire == 0

    clock.on_wait = during_wait
    assert route._process_one(account(), config(), batch_id='backoff', stop_event=clock.stop)['status'] == 'success'
    assert clock.now == 1130 and calls.acquire == 1


def test_late_old_batch_cannot_delay_new_batch(route, clock):
    route._defer_authorize_retry('old-batch', limited('180'))
    assert route._shared_authorize_retry_deadline('backoff') == 0
    route._defer_authorize_retry('backoff', limited())
    assert route._shared_authorize_retry_deadline('backoff') == 1090
    route._finish_batch('backoff')
    route._reserve_batch('codex', 1, 'new-batch')
    assert route._shared_authorize_retry_deadline('new-batch') == 0


def test_retry_after_http_date_and_expired_dates(route, clock):
    assert route._authorize_retry_delay(limited(formatdate(route.time.time() + 180, usegmt=True))) == 180
    assert route._authorize_retry_delay(limited(formatdate(route.time.time() - 1, usegmt=True))) == 90


@pytest.mark.parametrize('error', [
    RuntimeError('HTTP 429 /api/accounts/password/verify: /api/accounts/authorize/continue'),
    RuntimeError('HTTP 403 /api/accounts/authorize/continue: forbidden'),
    RuntimeError('HTTP 429 /api/accounts/authorize/continue-other: rate_limit_exceeded'),
    RuntimeError('supplier refused: HTTP 429 /api/accounts/authorize/continue'),
])
def test_only_exact_authorize_endpoint_429_gets_delayed_retry(route, error):
    assert not route._authorize_continue_rate_limit(error)


def test_current_http_metadata_outranks_body_codes_and_old_exception_context(route):
    from codex_oauth.oauth import OAuthHttpError

    forbidden = OAuthHttpError('rate_limit_exceeded', status_code=403, path='/api/accounts/authorize/continue')
    forbidden.__cause__ = limited()
    assert not route._authorize_continue_rate_limit(forbidden)
    other = OAuthHttpError('old request failed', status_code=429, path='/api/accounts/password/verify')
    other.__cause__ = limited()
    assert not route._authorize_continue_rate_limit(other)
    current = OAuthHttpError('sparse response', status_code=429, path='/api/accounts/authorize/continue')
    assert route._authorize_continue_rate_limit(current)
    wrapper = RuntimeError('wrapped')
    wrapper.__cause__ = current
    assert route._authorize_continue_rate_limit(wrapper)


def test_number_purchase_guard_runs_after_prices_and_before_each_tier(route, monkeypatch):
    client = route._PanelSmsBowerClient(route.SmsSettings.from_mapping(config()), [('6', ['1', '2'], '')])
    calls = []
    client.request_guard = lambda: calls.append('guard')
    monkeypatch.setattr(route, '_smsbower_tier_prices', lambda *_: calls.append('prices') or {})
    responses = iter(['NO_NUMBERS', 'ACCESS_NUMBER:test:15550000000'])
    monkeypatch.setattr(client, '_request', lambda *_args, **_kwargs: calls.append('purchase') or next(responses))
    client.acquire()
    assert calls == ['guard', 'prices', 'guard', 'purchase', 'guard', 'purchase']


def test_local_deadline_does_not_inherit_another_accounts_longer_wait(route, clock):
    assert route._defer_authorize_retry('backoff', limited('180')) == 1180
    assert route._defer_authorize_retry('backoff', limited()) == 1090
    assert route._shared_authorize_retry_deadline('backoff') == 1180


@pytest.mark.parametrize('extend', [False, True])
def test_received_sms_is_logged_before_peer_cooldown_and_original_code_submitted_once(route, monkeypatch, clock, extend):
    def received(_stop):
        route._defer_authorize_retry('backoff', limited())
        clock.now += 3  # Reproduce the reported remaining 87 seconds.
        return '654321'

    calls = fake_login(route, monkeypatch, wait=received)
    original_submit = route.ProtocolClient.submit_phone_otp
    codes, steps, waiting = [], [], []
    monkeypatch.setattr(route.ProtocolClient, 'submit_phone_otp', lambda client, code: (codes.append(code), original_submit(client, code))[-1])
    extended = []

    def paused(_seconds):
        assert any('已收到短信验证码' in item['m'] for item in steps)
        assert calls.acquire == calls.login == calls.send == 1 and calls.otp == 0
        item = dict(route._STATE['items'][0])
        assert item['stage'] == 'batch_rate_limit_wait'
        assert '同批其他账号' in item['message'] and '正在验证短信' in item['message']
        assert '本账号提交邮箱返回' not in item['message'] and '重试授权提交' not in item['message']
        waiting.append(item)
        if extend and clock.now >= 1040 and not extended:
            extended.append(route._defer_authorize_retry('backoff', limited()))

    clock.on_wait = paused
    result = route._process_one(account(), config(1), steps, batch_id='backoff', stop_event=clock.stop)
    assert result['status'] == 'success'
    assert '87 秒' in waiting[0]['message']
    assert codes == ['654321'] and calls.acquire == calls.otp == calls.closed == 1
    assert calls.cancelled == 0 and len(result['phone_attempts']) == 1
    assert clock.now == (1130 if extend else 1090)
    assert '654321' not in str(steps) and '654321' not in str(waiting)


def test_stop_after_received_sms_during_peer_cooldown_releases_without_new_number(route, monkeypatch, clock):
    def received(_stop):
        route._defer_authorize_retry('backoff', limited())
        return '654321'

    calls = fake_login(route, monkeypatch, wait=received)
    clock.on_wait = lambda _seconds: clock.stop.set()
    with pytest.raises(route._SmsCancelled):
        route._process_one(account(), config(1), batch_id='backoff', stop_event=clock.stop)
    assert calls.acquire == calls.cancelled == calls.login == calls.closed == 1
    assert calls.otp == 0


def test_peer_cooldown_after_phone_send_does_not_delay_receiving_paid_sms(route, monkeypatch, clock):
    polled = []

    def received(_stop):
        polled.append(clock.now)
        return '654321'

    calls = fake_login(route, monkeypatch, wait=received)
    original_send = route.ProtocolClient.send_phone

    def send(client, phone):
        original_send(client, phone)
        route._defer_authorize_retry('backoff', limited())

    def paused(_seconds):
        assert polled == [1000]
        assert calls.acquire == 1 and calls.otp == 0
        assert route._STATE['items'][0]['stage'] == 'batch_rate_limit_wait'

    monkeypatch.setattr(route.ProtocolClient, 'send_phone', send)
    clock.on_wait = paused
    result = route._process_one(account(), config(1), batch_id='backoff', stop_event=clock.stop)
    assert result['status'] == 'success'
    assert calls.acquire == calls.send == calls.otp == 1 and calls.cancelled == 0
    assert clock.now == 1090

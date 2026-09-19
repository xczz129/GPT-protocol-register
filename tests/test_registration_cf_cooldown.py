"""Batch cooldown gates future work; CSRF fast-path challenges retry via proxy rotation."""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from register import batch, protocol
from test_network_retry_classification import registration_scheduler


CF_DENIAL = 'chatgpt_csrf_http_403: body_summary=cloudflare_challenge'
HOME_CF_DENIAL = 'chatgpt_home_http_403: body_summary=cloudflare_challenge'
JSON_DENIAL = 'chatgpt_csrf_http_403: api_error_code=access_denied'


@pytest.fixture
def clock(monkeypatch):
    value = SimpleNamespace(now=100.0)
    monkeypatch.setattr(protocol, 'time', SimpleNamespace(
        monotonic=lambda: value.now, time=time.time,
    ))
    real_sleep = asyncio.sleep

    async def advance(seconds):
        # Advance only cooldown waits, not the browser stop-watcher polling.
        if seconds == 0.25:
            value.now += seconds
        await real_sleep(0)

    monkeypatch.setattr(asyncio, 'sleep', advance)
    # Browser dispatch jitter is unrelated to batch cooldown.
    monkeypatch.setattr(batch.random, 'uniform', lambda *_: 0)
    return value


def settings(engine):
    return {
        'mail': {},
        'registration': {
            'engine': engine, 'max_transient_retries': 3,
            'browser_protocol_security_after_register': False,
            'post_register_health_check': False,
        },
        'proxy': {'enabled': True, 'mode': 'pool', 'rotate_per_task': False},
    }


def run_batch(observed, engine, *, total=3, concurrency=1, event=None, stop=None):
    runner = protocol.run_protocol_register if engine == 'protocol' else batch.run_register

    def capture(index, status=None, **fields):
        observed.events.append((index, status, fields))
        if event:
            event(index, status, fields)

    return asyncio.run(runner(
        settings(engine), total=total, concurrency=concurrency,
        provider_type='outlook_token', task_event=capture,
        stop_check=stop, account_log_sink=Mock(),
    ))


def test_existing_strike_threshold_window_and_cooldown_are_preserved(clock):
    assert (protocol._CF_STRIKE_WINDOW_SEC, protocol._CF_STRIKE_THRESHOLD,
            protocol._CF_COOLDOWN_SEC) == (180.0, 8, 300.0)
    state = protocol._new_cf_block_state()
    for _ in range(7):
        assert protocol._note_cf_block(state) == 0
    assert protocol._cf_cooldown_remaining(state) == 0
    clock.now += 181
    assert protocol._note_cf_block(state) == 0  # Old strikes expired.
    for _ in range(6):
        assert protocol._note_cf_block(state) == 0
    assert protocol._note_cf_block(state) == 300
    assert protocol._cf_cooldown_remaining(state) == 300
    clock.now += 299
    assert protocol._cf_cooldown_remaining(state) == 1
    clock.now += 1
    assert protocol._cf_cooldown_remaining(state) == 0
    assert protocol._cf_cooldown_remaining(protocol._new_cf_block_state()) == 0


@pytest.mark.parametrize('engine', ['protocol', 'browser'])
@pytest.mark.parametrize('denials,cools', [
    ([HOME_CF_DENIAL, JSON_DENIAL], False),
    ([JSON_DENIAL, JSON_DENIAL], False),
    ([HOME_CF_DENIAL, HOME_CF_DENIAL], True),
])
def test_only_cf_burst_delays_next_account_without_retry_or_route_change(
    registration_scheduler, monkeypatch, clock, engine, denials, cools,
):
    observed = registration_scheduler
    monkeypatch.setattr(protocol, '_CF_STRIKE_THRESHOLD', 2)
    calls = []
    record = {'email': observed.addresses[2], 'registration_state': 'complete'}

    def attempt(index, **kwargs):
        calls.append((index, clock.now, kwargs.get('proxy'), kwargs.get('fixed_email')))
        if index < 3:
            kwargs['task_event'](index, 'failed', error=denials[index - 1])
            return None
        return record

    async def browser_attempt(index, *_args, **kwargs):
        return attempt(index, **kwargs)

    monkeypatch.setattr(protocol, 'register_one', attempt)
    monkeypatch.setattr(batch, 'register_one', browser_attempt)
    assert run_batch(observed, engine) == [record]
    assert [entry[0] for entry in calls] == [1, 2, 3]
    assert calls[2][1] - calls[1][1] == (300 if cools else 0)
    assert all(entry[2] == observed.proxies[0] for entry in calls)
    assert [entry[3] for entry in calls] == observed.addresses
    observed.blocked.assert_not_called()
    paused = [(index, fields) for index, status, fields in observed.events if status == 'paused']
    assert bool(paused) is cools
    assert all(index == 3 and '冷却' in fields['step'] for index, fields in paused)
    for index in (1, 2):
        statuses = [status for task, status, _ in observed.events if task == index]
        assert statuses[-1] == 'failed'


@pytest.mark.parametrize('engine', ['protocol', 'browser'])
def test_stop_during_shared_cooldown_does_not_dispatch_next_account(
    registration_scheduler, monkeypatch, clock, engine,
):
    observed = registration_scheduler
    monkeypatch.setattr(protocol, '_CF_STRIKE_THRESHOLD', 2)
    calls = []
    stopped = threading.Event()

    def attempt(index, **kwargs):
        calls.append(index)
        kwargs['task_event'](index, 'failed', error=HOME_CF_DENIAL)

    async def browser_attempt(index, *_args, **kwargs):
        return attempt(index, **kwargs)

    def stop_when_paused(index, status, _fields):
        if status == 'paused':
            stopped.set()

    monkeypatch.setattr(protocol, 'register_one', attempt)
    monkeypatch.setattr(batch, 'register_one', browser_attempt)
    assert run_batch(observed, engine, event=stop_when_paused, stop=stopped.is_set) == []
    assert calls == [1, 2]
    assert clock.now < 400
    assert any(index == 3 and status == 'cancelled' for index, status, _ in observed.events)
    observed.blocked.assert_not_called()


@pytest.mark.parametrize('engine', ['protocol', 'browser'])
def test_cooldown_allows_inflight_success_to_finish(
    registration_scheduler, monkeypatch, clock, engine,
):
    observed = registration_scheduler
    observed.addresses.append('fourth@example.test')
    monkeypatch.setattr(protocol, '_CF_STRIKE_THRESHOLD', 1)
    monkeypatch.setattr(protocol, '_CF_COOLDOWN_SEC', 1)
    started = threading.Barrier(3)
    release_success = threading.Event()
    calls = []
    record = {'email': observed.addresses[2], 'registration_state': 'complete'}

    def attempt(index, **kwargs):
        calls.append(index)
        if index <= 3:
            started.wait(timeout=5)
        if index == 3:
            assert release_success.wait(timeout=5)
            return record
        kwargs['task_event'](index, 'failed', error=HOME_CF_DENIAL)

    async def browser_attempt(index, *_args, **kwargs):
        return await asyncio.to_thread(attempt, index, **kwargs)

    def release_on_cooldown(index, status, _fields):
        if index == 4 and status == 'paused':
            release_success.set()

    monkeypatch.setattr(protocol, 'register_one', attempt)
    monkeypatch.setattr(batch, 'register_one', browser_attempt)
    assert run_batch(observed, engine, total=4, concurrency=3, event=release_on_cooldown) == [record]
    assert sorted(calls) == [1, 2, 3, 4]
    assert release_success.is_set()
    assert not any(index == 3 and status in {'failed', 'cancelled'} for index, status, _ in observed.events)
    observed.blocked.assert_not_called()


def test_long_cf_diagnostics_keep_classification_in_bounded_failure_event(
    registration_scheduler, monkeypatch,
):
    observed = registration_scheduler
    registrar = Mock()
    registrar.device_id = 'fixture-device'
    registrar.fingerprint = {}
    registrar.registration_mailbox = {'provider': 'outlook_token', 'address': observed.addresses[0]}
    registrar.register.side_effect = RuntimeError(
        'chatgpt_home_http_403: ' + ('request_id=public-request ' * 30)
        + 'body_summary=cloudflare_challenge'
    )
    monkeypatch.setattr(protocol, 'PlatformRegistrar', Mock(return_value=registrar))
    assert observed.run('protocol') == []
    errors = [fields['error'] for _, status, fields in observed.events if status == 'failed' and fields.get('error')]
    assert errors and all(protocol.is_cloudflare_block_error(error) for error in errors)
    assert all(len(error) <= 300 for error in errors)
    registrar.register.assert_called_once()
    observed.blocked.assert_not_called()


@pytest.mark.parametrize('engine', ['protocol', 'browser'])
def test_dynamic_mailbox_allocation_waits_for_batch_cooldown(
    registration_scheduler, monkeypatch, clock, engine,
):
    import mail
    from register import smsbower_batch

    observed = registration_scheduler
    monkeypatch.setattr(protocol, '_CF_STRIKE_THRESHOLD', 2)
    # Dynamic coordinator polls at 0.2s; no static-task watcher runs here.
    fast_sleep = asyncio.sleep

    async def advance_dynamic(seconds):
        if seconds == 0.2:
            clock.now += seconds
        await fast_sleep(seconds)

    monkeypatch.setattr(asyncio, 'sleep', advance_dynamic)
    for module in (mail, batch):
        monkeypatch.setattr(module, 'count_pool_capacity', lambda *a, **kw: {
            'total': 3, 'available': 3, 'emails': [],
        })
    purchases, calls = [], []

    def allocate(*_args, **_kwargs):
        purchases.append(clock.now)
        return {'provider': 'online_disposablemail', 'address': f'fixture{len(purchases)}@example.test'}

    def attempt(index, **kwargs):
        calls.append(index)
        kwargs['task_event'](index, 'failed', error=HOME_CF_DENIAL)

    async def browser_attempt(index, *_args, **kwargs):
        return attempt(index, **kwargs)

    monkeypatch.setattr(smsbower_batch, 'create_mailbox', allocate)
    monkeypatch.setattr(smsbower_batch, 'release_mailbox', Mock())
    monkeypatch.setattr(protocol, 'register_one', attempt)
    monkeypatch.setattr(batch, 'register_one', browser_attempt)
    config = settings(engine)
    config['mail'] = {'providers': [{'type': 'online_disposablemail', 'enable': True}]}
    runner = protocol.run_protocol_register if engine == 'protocol' else batch.run_register
    assert asyncio.run(runner(
        config, total=3, concurrency=1, provider_type='online_disposablemail',
        task_event=Mock(), account_log_sink=Mock(),
    )) == []
    assert calls == [1, 2, 3]
    assert purchases[:2] == [100, 100]
    assert 400 <= purchases[2] < 400.3
    observed.blocked.assert_not_called()


def test_csrf_cloudflare_challenge_rotates_proxy_and_recovers(
    registration_scheduler, monkeypatch, clock,
):
    observed = registration_scheduler
    monkeypatch.setattr(protocol, '_CF_STRIKE_THRESHOLD', 8)
    calls = []
    record = {'email': observed.addresses[0], 'registration_state': 'complete'}

    def attempt(index, **kwargs):
        calls.append(kwargs.get('proxy'))
        if len(calls) == 1:
            kwargs['task_event'](index, 'failed', error=CF_DENIAL)
            return None
        return record

    monkeypatch.setattr(protocol, 'register_one', attempt)
    assert run_batch(observed, 'protocol', total=1) == [record]
    assert len(calls) == 2
    assert calls[0] == observed.proxies[0]
    assert calls[1] == observed.proxies[1]
    switch_steps = [
        fields.get('step', '')
        for _, status, fields in observed.events
        if status == 'running' and '切换代理' in fields.get('step', '')
    ]
    assert switch_steps

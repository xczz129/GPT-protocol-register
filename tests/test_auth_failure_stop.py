"""Failure paths preserve pending data and do not issue extra authentication."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import mail
from register import batch as browser
from register import protocol
from register import protocol_http


def test_http_429_is_returned_without_local_retry_or_sleep(monkeypatch):
    reply=SimpleNamespace(status_code=429,headers={'retry-after':'120'})
    session=SimpleNamespace(request=Mock(return_value=reply))
    sleep=Mock(side_effect=AssertionError('rate limits must be propagated'))
    monkeypatch.setattr(protocol_http.time,'sleep',sleep)
    response,error=protocol_http.request_with_local_retry(session,'POST','https://service.example.test/action',retry_attempts=5)
    assert response is reply and not error
    session.request.assert_called_once()
    sleep.assert_not_called()


def test_stop_after_response_prevents_another_local_request(monkeypatch):
    import threading
    stopped=threading.Event()
    def reply(*args,**kwargs):
        stopped.set()
        return SimpleNamespace(status_code=503,headers={})
    session=SimpleNamespace(request=Mock(side_effect=reply))
    sleep=Mock(side_effect=AssertionError('a stopped task must not start a backoff'))
    monkeypatch.setattr(protocol_http.time,'sleep',sleep)
    token=protocol_http._STOP_CHECK.set(stopped.is_set)
    try:
        response,error=protocol_http.request_with_local_retry(session,'GET','https://service.example.test/action')
    finally:
        protocol_http._STOP_CHECK.reset(token)
    assert response is None and error=='stop requested'
    session.request.assert_called_once()
    sleep.assert_not_called()


@pytest.mark.parametrize('engine',['protocol','browser'])
def test_rate_limit_does_not_rotate_proxies_or_start_queued_accounts(monkeypatch,engine):
    addresses=['first@example.test','second@example.test']
    for module in (mail,browser):
        monkeypatch.setattr(module,'count_pool_capacity',lambda *a,**kw:{'total':2,'available':2,'emails':addresses})
        monkeypatch.setattr(module,'release_stale_in_use',lambda:0)
    for module in (protocol,browser):
        monkeypatch.setattr(module,'resolve_proxy_pool',lambda *a,**kw:['http://proxy-one.example.test:8080','http://proxy-two.example.test:8080'])
    calls,events=[],[]
    def limited(index,**kwargs):
        calls.append((index,kwargs.get('proxy')))
        kwargs['task_event'](index,'failed',error='auth_rate_limit_exceeded',step='认证服务限流')
        return None
    async def browser_limited(index,*args,**kwargs):
        return limited(index,**kwargs)
    monkeypatch.setattr(protocol,'register_one',limited)
    monkeypatch.setattr(browser,'register_one',browser_limited)
    config={'mail':{},'registration':{'engine':engine,'max_transient_retries':3},
            'proxy':{'enabled':True,'mode':'pool','rotate_per_task':True}}
    run=protocol.run_protocol_register if engine=='protocol' else browser.run_register
    result=asyncio.run(run(config,total=2,concurrency=1,provider_type='outlook_token',
        task_event=lambda index,status=None,**fields:events.append((index,status,fields))))
    assert not result
    assert len(calls)==1 and calls[0][0]==1
    assert any(index==2 and status in {'cancelled','skipped','failed'} for index,status,_ in events)
    assert not any('代理连接失败' in str(fields.get('step','')) or '切换下一条代理' in str(fields.get('step','')) for _,_,fields in events)

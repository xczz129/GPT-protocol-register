"""Registration cards own per-account SMS without holding mailbox leases."""
import asyncio
import threading
from unittest.mock import Mock

import pytest

from common.registration_pipeline import RegistrationPipeline


def test_web_job_waits_for_inline_sms_and_returns_refreshed_credentials(monkeypatch):
    import register_gpt
    import web_app
    from common import registration_pipeline as module
    from web.routes.jobs import JobState

    entered, release = threading.Event(), threading.Event()
    class Session:
        concurrency = 1
        def __init__(self, _source, *, event_callback, stop_check):
            self.emit = event_callback
        def run(self, index, record, **kwargs):
            entered.set()
            assert release.wait(3)
            self.emit(index, "success", phase="sms", registration_status="success", sms_status="success")
        def close(self): pass

    def factory(*args, **kwargs):
        return RegistrationPipeline(*args, **kwargs, session_factory=Session)

    async def register(_cfg, **kwargs):
        kwargs["task_event"](1, "success", email=account()["email"], step="注册完成并已测活")
        await kwargs["account_ready"](1, account(), stop_check=lambda: False)
        return [account()]

    job = JobState()
    job.running = True
    job.batch_id = "fixture-job"
    monkeypatch.setattr(web_app, "JOB", job)
    monkeypatch.setattr(web_app, "_SERVER_SHUTDOWN_EVENT", threading.Event())
    monkeypatch.setattr(web_app, "_io_worker_count", lambda: 2)
    monkeypatch.setattr(web_app, "_account_key", lambda record: record["email"])
    monkeypatch.setattr(web_app, "_records_by_keys_with_fallback", lambda keys: {account()["email"]: {**account(), "access_token": "refreshed-synthetic-token"}})
    monkeypatch.setattr(register_gpt, "run_register", register)
    monkeypatch.setattr(module, "RegistrationPipeline", factory)

    async def run():
        worker = asyncio.create_task(web_app._run_register_async({"registration": {}}, {"total": 1, "concurrency": 1, "batch_id": "fixture-job", "auto_sms_after_register": True}))
        assert await asyncio.to_thread(entered.wait, 3)
        assert not worker.done()
        assert job.task_cards[1]["status"] == "running"
        release.set()
        results = await worker
        assert job.task_cards[1]["status"] == "success"
        assert results[0]["access_token"] == "refreshed-synthetic-token"
        assert job._registration_task is None
    try:
        asyncio.run(run())
    finally:
        release.set()


def account():
    return {"email": "fixture@example.test", "access_token": "synthetic-token", "registration_state": "complete", "health_checked_at": "fixture-time"}


def test_sms_starts_before_batch_end_and_only_sms_emits_final_success():
    async def run():
        entered, release = threading.Event(), threading.Event()
        events = []

        class Session:
            def __init__(self, _source, *, event_callback, stop_check):
                self.emit = event_callback
                self.close = Mock()
            def run(self, index, record, task_stop_check=None):
                entered.set()
                assert release.wait(3)
                self.emit(index, "success", phase="sms", registration_status="success", sms_status="success", step="注册及接码完成")

        pipeline = RegistrationPipeline("fixture", event_callback=lambda i, st, **kw: events.append((i, st, kw)), stop_check=lambda: False, session_factory=Session)
        pipeline.registration_event(1, "success", step="注册完成并已测活")
        assert events[-1][1] == "running"
        await pipeline.account_ready(1, account())
        assert await asyncio.to_thread(entered.wait, 3)
        # account_ready already returned, so child mailbox work is free to run.
        assert not any(status == "success" for _, status, _ in events)
        finish = asyncio.create_task(pipeline.finish())
        await asyncio.sleep(0)
        assert not finish.done()
        release.set()
        await finish
        await pipeline.finish()
        pipeline._session.close.assert_called_once()
        assert events[-1][1] == "success"
    asyncio.run(run())


def test_cancellation_waits_for_sms_cleanup_before_finishing():
    async def run():
        entered, cleanup_release, cleaned = threading.Event(), threading.Event(), threading.Event()
        class Session:
            def __init__(self, _source, *, event_callback, stop_check):
                self.stop = stop_check
            def run(self, *args, **kwargs):
                entered.set()
                assert cleanup_release.wait(3)
                assert self.stop()
                cleaned.set()
            def close(self):
                assert cleaned.is_set()
        pipeline = RegistrationPipeline("fixture", event_callback=Mock(), stop_check=lambda: False, session_factory=Session)
        await pipeline.account_ready(1, account())
        assert await asyncio.to_thread(entered.wait, 3)
        finish = asyncio.create_task(pipeline.finish())
        await asyncio.sleep(0)
        finish.cancel()
        await asyncio.sleep(0)
        assert not finish.done()
        cleanup_release.set()
        await finish
        assert cleaned.is_set()
    asyncio.run(run())


def test_duplicate_ready_does_not_purchase_twice_and_failure_preserves_registration():
    async def run():
        events, called = [], []
        class Session:
            def __init__(self, *args, **kwargs): pass
            def run(self, index, record, **kwargs):
                called.append(index)
                raise RuntimeError("password=private token=private")
            def close(self): pass
        pipeline = RegistrationPipeline("fixture", event_callback=lambda i, st, **kw: events.append((st, kw)), stop_check=lambda: False, session_factory=Session)
        record = account()
        await pipeline.account_ready(1, record)
        await pipeline.account_ready(1, record)
        await pipeline.finish()
        assert called == [1]
        assert record == account()
        assert events[-1][0] == "failed"
        assert events[-1][1]["registration_status"] == "success"
        assert "private" not in str(events)
    asyncio.run(run())


@pytest.mark.parametrize("checked", [True, False])
def test_missing_health_probe_runs_before_sms_but_completed_probe_is_not_repeated(checked):
    async def run():
        calls = []
        class Session:
            def __init__(self, *args, **kwargs): pass
            def run(self, index, record, **kwargs): calls.append("sms")
            def close(self): pass
        def health(record):
            calls.append("health")
            return {**record, "health_checked_at": "now"}
        pipeline = RegistrationPipeline("fixture", event_callback=Mock(), stop_check=lambda: False, session_factory=Session, health_check=health)
        record = account()
        if not checked: record.pop("health_checked_at")
        await pipeline.account_ready(1, record)
        await pipeline.finish()
        assert calls == (["sms"] if checked else ["health", "sms"])
    asyncio.run(run())


def test_incomplete_identity_does_not_start_sms():
    async def run():
        events = []
        class Session:
            def __init__(self, *args, **kwargs): self.run = Mock()
            def close(self): pass
        pipeline = RegistrationPipeline("fixture", event_callback=lambda i, st, **kw: events.append(kw), stop_check=lambda: False, session_factory=Session)
        await pipeline.account_ready(1, {**account(), "registration_state": "security_pending"})
        await pipeline.finish()
        pipeline._session.run.assert_not_called()
        assert events[-1]["registration_status"] == "incomplete"
    asyncio.run(run())


def test_waiting_sms_does_not_exhaust_registration_and_child_mailbox_executor():
    from concurrent.futures import ThreadPoolExecutor

    async def run():
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=2))
        entered = [threading.Event(), threading.Event()]
        release = threading.Event()

        class Session:
            concurrency = 2

            def __init__(self, *args, **kwargs):
                pass

            def run(self, index, record, **kwargs):
                entered[index - 1].set()
                assert release.wait(3)

            def close(self):
                pass

        pipeline = RegistrationPipeline(
            "fixture-executor", event_callback=Mock(), stop_check=lambda: False,
            session_factory=Session,
        )
        try:
            for index in (1, 2):
                await pipeline.account_ready(index, {**account(), "email": f"fixture{index}@example.test"})
            for _ in range(1000):
                if all(event.is_set() for event in entered):
                    break
                await asyncio.sleep(0.001)
            assert all(event.is_set() for event in entered), "both SMS executions must be blocked before checking mailbox progress"
            # Registration and parent/child mailbox operations use to_thread.
            # They must still run while all SMS workers wait for a code/slot.
            child = await asyncio.wait_for(
                asyncio.to_thread(lambda: "child mailbox registration ran"), timeout=0.5,
            )
            assert child == "child mailbox registration ran"
            assert not release.is_set(), "mailbox progress must not depend on SMS completion"
        finally:
            release.set()
            await pipeline.finish()

    asyncio.run(run())


@pytest.mark.parametrize("engine", ["protocol", "browser"])
def test_real_scheduler_dispatches_per_account_before_whole_registration_batch_finishes(monkeypatch, engine):
    import mail
    from register import batch, protocol

    for module in (mail, batch):
        monkeypatch.setattr(module, "count_pool_capacity", lambda *a, **kw: {
            "total": 2, "available": 2, "emails": ["one@example.test", "two@example.test"],
        })
        monkeypatch.setattr(module, "release_stale_in_use", lambda: 0)
    monkeypatch.setattr(mail.requests.sessions.Session, "request", Mock(side_effect=AssertionError("network forbidden")))
    first_sms, release_sms = threading.Event(), threading.Event()
    seen = []

    def attempt(index, *args, **kwargs):
        if index == 2:
            assert first_sms.wait(3), "first account SMS starts before the second registration ends"
        result = {**account(), "email": f"{index}@example.test"}
        kwargs["task_event"](index, "success", email=result["email"], step="注册完成")
        return result

    async def browser_attempt(index, *args, **kwargs):
        return await asyncio.to_thread(attempt, index, *args, **kwargs)

    monkeypatch.setattr(protocol, "register_one", attempt)
    monkeypatch.setattr(batch, "register_one", browser_attempt)

    class Session:
        def __init__(self, _source, *, event_callback, stop_check):
            self.emit = event_callback
        def run(self, index, record, **kwargs):
            if index == 1: first_sms.set()
            assert release_sms.wait(3)
            self.emit(index, "success", phase="sms", registration_status="success", sms_status="success")
        def close(self): pass

    async def run():
        pipeline = RegistrationPipeline("fixture", event_callback=lambda i, st, **kw: seen.append((i, st, kw)), stop_check=lambda: False, session_factory=Session)
        config = {
            "mail": {"providers": [{"type": "icloud", "enable": True}]},
            "registration": {"engine": engine, "total": 2, "threads": 1, "max_transient_retries": 0,
                             "browser_protocol_security_after_register": False, "post_register_health_check": False},
            "proxy": {"enabled": False},
        }
        records = await batch.run_register(config, total=2, concurrency=1, provider_type="icloud",
                                           task_event=pipeline.registration_event, account_ready=pipeline.account_ready,
                                           account_log_sink=lambda *_args: None)
        assert len(records) == 2
        assert not any(status == "success" for _, status, _ in seen)
        release_sms.set()
        await pipeline.finish()
        assert sorted(index for index, status, _ in seen if status == "success") == [1, 2]
    try:
        asyncio.run(run())
    finally:
        release_sms.set()

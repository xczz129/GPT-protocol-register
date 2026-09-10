"""Deterministic, offline regressions for batch ownership and checkpoints."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading
from types import SimpleNamespace

import pytest

from common import task_checkpoint


class ManualTimer:
    created = []

    def __init__(self, interval, callback, args=()):
        self.callback = callback
        self.args = args
        self.cancelled = False
        self.created.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        # A cancelled timer may already have entered its callback.
        self.callback(*self.args)


@pytest.fixture
def checkpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("TASK_CHECKPOINT_DIR", str(tmp_path / "checkpoints"))
    monkeypatch.setattr(ManualTimer, "created", [])
    monkeypatch.setattr(task_checkpoint, "threading", SimpleNamespace(
        Lock=threading.Lock, Timer=ManualTimer,
    ))
    return task_checkpoint.DebouncedCheckpointSaver("batch", interval=60)


def test_timer_saves_latest_submitted_callback(checkpoint):
    checkpoint.submit(lambda: {"done": 1})
    checkpoint.submit(lambda: {"done": 2})
    assert len(ManualTimer.created) == 1
    ManualTimer.created[0].fire()
    assert task_checkpoint.load_checkpoint("batch") == {"done": 2}


@pytest.mark.parametrize("action", ["flush", "clear", "new_batch"])
def test_inflight_timer_cannot_overwrite_final_or_new_checkpoint(checkpoint, action):
    entered, release = threading.Event(), threading.Event()

    def old_payload():
        entered.set()
        assert release.wait(5)
        return {"generation": "old", "running": True}

    checkpoint.submit(old_payload)
    worker = threading.Thread(target=ManualTimer.created[-1].fire)
    worker.start()
    try:
        assert entered.wait(5)
        if action == "flush":
            checkpoint.flush_now(lambda: {"generation": "final", "running": False})
        elif action == "clear":
            task_checkpoint.clear_checkpoint("batch")
        else:
            task_checkpoint.save_checkpoint("batch", {"generation": "new", "running": True})
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    expected = {
        "flush": {"generation": "final", "running": False},
        "clear": None,
        "new_batch": {"generation": "new", "running": True},
    }[action]
    assert task_checkpoint.load_checkpoint("batch") == expected


def test_clear_and_new_submit_ignore_old_timer(checkpoint):
    checkpoint.submit(lambda: {"batch": "old"})
    old_timer = ManualTimer.created[-1]
    task_checkpoint.clear_checkpoint("batch")
    task_checkpoint.save_checkpoint("batch", {"batch": "new", "done": 0})
    checkpoint.submit(lambda: {"batch": "new", "done": 1})
    new_timer = ManualTimer.created[-1]
    assert new_timer is not old_timer
    assert old_timer.cancelled
    old_timer.fire()
    assert task_checkpoint.load_checkpoint("batch") == {"batch": "new", "done": 0}
    new_timer.fire()
    assert task_checkpoint.load_checkpoint("batch") == {"batch": "new", "done": 1}


def test_flush_now_invalidates_other_saver_timer(checkpoint):
    other = task_checkpoint.DebouncedCheckpointSaver("batch", interval=60)
    other.submit(lambda: {"running": True})
    stale = ManualTimer.created[-1]
    checkpoint.flush_now(lambda: {"running": False})
    stale.fire()
    assert task_checkpoint.load_checkpoint("batch") == {"running": False}


def test_payload_failure_does_not_disable_future_timer(checkpoint):
    def failed():
        raise RuntimeError("synthetic failure")

    checkpoint.submit(failed)
    ManualTimer.created[-1].fire()
    checkpoint.submit(lambda: {"done": 3})
    ManualTimer.created[-1].fire()
    assert task_checkpoint.load_checkpoint("batch") == {"done": 3}


@pytest.fixture
def route(monkeypatch):
    import web.routes.codex_oauth as module

    state = deepcopy(module._STATE)
    state.update(running=False, batch_id="", logs=[], results=[], done=0, success=0, failed=0)
    monkeypatch.setattr(module, "_STATE", state)
    monkeypatch.setattr(module, "_LOCK", threading.Lock())
    monkeypatch.setattr(module, "_STOP", threading.Event())
    monkeypatch.setattr(module, "_rotate_import_history", lambda: None)
    monkeypatch.setattr(module, "_append_import_history", lambda *_: None)
    monkeypatch.setattr(module, "_append_rt_history", lambda *_: None)
    monkeypatch.setattr(module, "_state", lambda: SimpleNamespace(_read_config=lambda: {}))
    monkeypatch.setattr(module, "_load_sms_batch_records", lambda _: [{"email": "test@example.invalid"}])
    monkeypatch.setattr(module, "_sms_config_section", lambda _: {})
    monkeypatch.setattr(module, "SmsSettings", SimpleNamespace(from_mapping=lambda _: SimpleNamespace(
        enabled=True, credentials_configured=True, provider_label="test", provider="test",
    )))
    return module


def _body(route):
    return route.CodexOauthImportRequest(text="test@example.invalid----dummy-password----dummy-totp")


def test_two_import_requests_atomically_claim_one_batch(route, monkeypatch):
    barrier = threading.Barrier(2)
    launched = []

    def parse(_):
        barrier.wait(5)
        return [("test@example.invalid", "dummy", "dummy")]

    monkeypatch.setattr(route, "_parse_credential_lines", parse)
    monkeypatch.setattr(route, "_launch_batch", lambda *args, **kwargs: launched.append(kwargs))

    async def requests():
        return await asyncio.gather(
            route.codex_oauth_import(_body(route)),
            route.codex_oauth_import(_body(route)), return_exceptions=True,
        )

    results = asyncio.run(requests())
    assert sum(isinstance(result, dict) and result["ok"] for result in results) == 1
    assert sum(isinstance(result, route.HTTPException) and result.status_code == 409 for result in results) == 1
    assert len(launched) == 1
    assert route._STATE["running"] is True


@pytest.mark.parametrize("second_mode", ["import", "codex"])
def test_import_and_sms_share_one_atomic_slot(route, monkeypatch, second_mode):
    barrier = threading.Barrier(2)
    original_reserve = route._reserve_batch
    launched = []

    def reserve(*args):
        barrier.wait(5)
        return original_reserve(*args)

    monkeypatch.setattr(route, "_reserve_batch", reserve)
    monkeypatch.setattr(route, "_launch_batch", lambda *args, **kwargs: launched.append(kwargs))
    monkeypatch.setattr(route, "_parse_credential_lines", lambda _: [("test@example.invalid", "dummy", "dummy")])

    def invoke(mode):
        try:
            if mode == "import":
                return asyncio.run(route.codex_oauth_import(_body(route)))
            return route._start_sms_batch({"test@example.invalid"})
        except route.HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(invoke, "codex")
        second = executor.submit(invoke, second_mode)
        results = [first.result(5), second.result(5)]
    assert results.count(409) == 1
    assert len(launched) == 1


@pytest.mark.parametrize("mode", ["import", "codex"])
def test_thread_start_failure_releases_batch_slot(route, monkeypatch, mode):
    class FailedThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            raise RuntimeError("thread start failed")

    monkeypatch.setattr(route, "threading", SimpleNamespace(Thread=FailedThread, Event=threading.Event))
    monkeypatch.setattr(route, "_parse_credential_lines", lambda _: [("test@example.invalid", "dummy", "dummy")])
    with pytest.raises(RuntimeError, match="thread start failed"):
        if mode == "import":
            asyncio.run(route.codex_oauth_import(_body(route)))
        else:
            route._start_sms_batch({"test@example.invalid"})
    assert route._STATE["running"] is False
    assert route._STOP.is_set()
    event = route._reserve_batch("import", 1, "retry")
    assert not event.is_set()


def test_history_rotation_failure_releases_import_slot(route, monkeypatch):
    monkeypatch.setattr(route, "_parse_credential_lines", lambda _: [("test@example.invalid", "dummy", "dummy")])

    def fail():
        raise OSError("synthetic history failure")

    monkeypatch.setattr(route, "_rotate_import_history", fail)
    with pytest.raises(OSError, match="synthetic history failure"):
        asyncio.run(route.codex_oauth_import(_body(route)))
    assert route._STATE["running"] is False
    assert route._STOP.is_set()


def test_stop_event_belongs_to_reserved_batch_and_old_finish_is_ignored(route):
    old_stop = route._reserve_batch("import", 1, "old")
    asyncio.run(route.codex_oauth_stop())
    assert old_stop.is_set()
    route._finish_batch("old")
    new_stop = route._reserve_batch("codex", 1, "new")
    route._finish_batch("old")
    assert route._STATE["running"] is True
    assert route._STATE["batch_id"] == "new"
    assert old_stop.is_set()
    assert not new_stop.is_set()


@pytest.mark.parametrize("mode", ["import", "codex"])
def test_stale_batch_completion_does_not_modify_new_state(route, monkeypatch, mode):
    old_stop = route._reserve_batch(mode, 1, "old")
    route._finish_batch("old")
    route._reserve_batch("import", 2, "new")
    monkeypatch.setattr(route, "_import_one", lambda *args, **kwargs: {"status": "success", "email": "test@example.invalid"})
    if mode == "import":
        route._run_import_batch([("test@example.invalid", "dummy", "dummy")], batch_id="old", stop_event=old_stop)
    else:
        route._run_batch([{"email": "test@example.invalid", "refresh_token": "dummy"}], {}, batch_id="old", stop_event=old_stop)
    assert route._STATE["running"] is True
    assert route._STATE["batch_id"] == "new"
    assert route._STATE["done"] == route._STATE["success"] == route._STATE["failed"] == 0
    assert route._STATE["results"] == route._STATE["logs"] == []

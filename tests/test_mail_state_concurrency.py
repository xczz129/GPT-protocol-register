# -*- coding: utf-8 -*-
"""Concurrency regression tests for the shared mailbox state machine.

The browser engine (asyncio) and the protocol engine (thread pool) share the
mailbox state JSON through a cross-process file lock plus an in-process mutex.
These tests hammer that path with parallel threads to make sure concurrent
acquire/release cycles never lose updates or resurrect released mailboxes.
"""
from __future__ import annotations

import json
import threading

import mail


def _run_parallel(fn, items):
    errors = []

    def guard(item):
        try:
            fn(item)
        except Exception as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=guard, args=(item,)) for item in items]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"worker errors: {[repr(e) for e in errors[:3]]}"


class TestMailboxStateConcurrency:
    def test_parallel_set_state_loses_no_updates(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mail, "STATE_FILE", tmp_path / "state.json")
        addrs = [f"user{i}@example.com" for i in range(24)]
        # Each address is acquired several times from different threads.
        _run_parallel(lambda a: mail._set_state(a, "in_use", "reg"), addrs * 3)
        state = json.loads(mail.STATE_FILE.read_text(encoding="utf-8"))
        assert len(state) == len(addrs)
        for a in addrs:
            assert state[a]["state"] == "in_use", a

    def test_parallel_set_and_release_keeps_survivors(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mail, "STATE_FILE", tmp_path / "state.json")
        addrs = [f"user{i}@example.com" for i in range(16)]
        for a in addrs:
            mail._set_state(a, "in_use", "seed")
        keep = set(addrs[::2])
        drop = [a for a in addrs if a not in keep]
        _run_parallel(mail._release_state, drop)
        state = json.loads(mail.STATE_FILE.read_text(encoding="utf-8"))
        for a in drop:
            assert a not in state, f"released mailbox still present: {a}"
        for a in keep:
            assert state[a]["state"] == "in_use", f"survivor lost: {a}"

    def test_interleaved_acquire_release_never_loses_entry(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(mail, "STATE_FILE", tmp_path / "state.json")
        addrs = [f"user{i}@example.com" for i in range(12)]

        def churn(rank):
            addr = addrs[rank % len(addrs)]
            for _ in range(10):
                mail._set_state(addr, "in_use", f"churn-{rank}")
                mail._release_state(addr)

        _run_parallel(churn, range(24))
        state = (
            json.loads(mail.STATE_FILE.read_text(encoding="utf-8"))
            if mail.STATE_FILE.exists()
            else {}
        )
        # Every address ended released -> no stale in_use entries may remain.
        stale = [a for a, e in state.items() if e.get("state") == "in_use"]
        assert not stale, f"stale in_use entries after churn: {stale}"

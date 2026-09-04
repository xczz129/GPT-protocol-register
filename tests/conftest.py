# -*- coding: utf-8 -*-
"""Test-wide isolation for the default operation-log database.

Worker code records operation events through
``common.account_operation_log.record_operation_event`` without an explicit
``path``.  Before this fixture existed, any test that exercised the reauth or
security flows without mocking the logger appended rows straight into the
production ``account_operation_logs.sqlite3`` (which is also how the
``expired@example.com`` / ``missing-security@example.com`` fixtures kept
reappearing).  Every default-path write now lands in a per-test temporary
database; tests that pass an explicit ``path`` keep full control.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_default_operation_log(tmp_path, monkeypatch):
    from common import account_operation_log as oplog

    original = oplog.append_operation_event
    isolated = tmp_path / "operation_logs.sqlite3"

    def append_isolated(account, operation, status, **kwargs):
        kwargs.setdefault("path", isolated)
        return original(account, operation, status, **kwargs)

    monkeypatch.setattr(oplog, "append_operation_event", append_isolated)
    yield

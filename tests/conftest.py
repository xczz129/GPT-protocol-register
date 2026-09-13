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

import os
import tempfile

import pytest

# 任务检查点(测活/检测试用/接码的重启恢复文件)必须与生产数据隔离:
# 在任何被测模块导入 task_checkpoint 之前重定向到临时目录。
os.environ.setdefault("TASK_CHECKPOINT_DIR", tempfile.mkdtemp(prefix="task-ckpt-"))
os.environ.setdefault(
    "MAIL_REGISTRATION_METRICS_DB",
    os.path.join(tempfile.mkdtemp(prefix="mail-metrics-"), "metrics.sqlite3"),
)


@pytest.fixture(autouse=True)
def _isolate_default_operation_log(tmp_path, monkeypatch):
    monkeypatch.setenv("MAIL_REGISTRATION_METRICS_DB", str(tmp_path / "mail_metrics.sqlite3"))
    from common import account_operation_log as oplog

    original = oplog.append_operation_event
    isolated = tmp_path / "operation_logs.sqlite3"

    def append_isolated(account, operation, status, **kwargs):
        kwargs.setdefault("path", isolated)
        return original(account, operation, status, **kwargs)

    monkeypatch.setattr(oplog, "append_operation_event", append_isolated)
    yield

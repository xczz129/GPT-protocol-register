"""Keep each registration card open while its own SMS follow-up runs."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import threading
from typing import Any

from register.smsbower_batch import _await_owned


class RegistrationPipeline:
    def __init__(self, source_id: str, *, event_callback, stop_check,
                 session_factory=None, health_check=None, persist=None) -> None:
        if session_factory is None:
            from common.registration_sms_inline import RegistrationInlineSmsSession
            session_factory = RegistrationInlineSmsSession
        self._emit = event_callback
        self._stop_check = stop_check
        self._health_check = health_check
        self._persist = persist
        self._cancel = threading.Event()
        self._tasks: dict[int, asyncio.Task] = {}
        self._finished = False
        self._session = session_factory(source_id, event_callback=self._sms_event, stop_check=self.stopped)
        # Waiting SMS must not occupy the registration executor and delay the
        # next mailbox/child registration. The queue grows only with this job.
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(getattr(self._session, "concurrency", 2))),
            thread_name_prefix="registration-sms",
        )

    def stopped(self) -> bool:
        return self._cancel.is_set() or bool(self._stop_check())

    def registration_event(self, index: int, status: str | None = None, **fields: Any) -> None:
        # The registrar's success is a stage result, not the final card result.
        # SMS starts only from account_ready, after the retry loop has resolved.
        if status == "success":
            status = "running"
            fields.update(registration_status="success", phase="registration", sms_status="pending")
        self._emit(index, status, **fields)

    def _sms_event(self, index: int, status: str | None = None, **fields: Any) -> None:
        self._emit(index, status, **fields)
        if callable(self._persist):
            self._persist()

    async def account_ready(self, index: int, record: dict, *, stop_check=None) -> None:
        if index in self._tasks:
            return
        # Do not hold the mailbox lease or registration worker while SMS waits.
        # In particular, a main mailbox's child can start receiving mail now.
        self._tasks[index] = asyncio.create_task(
            self._run(index, dict(record), stop_check), name=f"registration-sms-{index}",
        )

    async def _run(self, index: int, record: dict, task_stop_check) -> None:
        def stopped():
            return self.stopped() or bool(callable(task_stop_check) and task_stop_check())

        try:
            if str(record.get("registration_state") or "complete") != "complete" or not record.get("access_token"):
                self._sms_event(index, "failed", phase="registration", registration_status="incomplete",
                                sms_status="skipped", stage="security_pending", message="注册凭据未补全，未启动接码",
                                step="注册凭据未补全，已保存现有进度；未启动接码")
                return
            if callable(self._health_check) and not record.get("health_checked_at") and not stopped():
                self._sms_event(index, "running", phase="health", registration_status="success", sms_status="pending",
                                stage="health_check", message="注册完成，正在测活", step="注册完成，开始接码前测活")
                checked, _ = await _await_owned(
                    asyncio.create_task(asyncio.to_thread(self._health_check, record)), self._cancel.set,
                )
                if isinstance(checked, dict):
                    record = checked
            await _await_owned(
                asyncio.get_running_loop().run_in_executor(
                    self._executor, partial(self._session.run, index, record, task_stop_check=stopped),
                ),
                self._cancel.set,
            )
        except Exception:
            # Keep existing credentials and never send a follow-up failure back
            # through the signup retry loop. Exceptions may contain secrets.
            cancelled = stopped()
            self._sms_event(index, "cancelled" if cancelled else "failed", phase="sms", registration_status="success",
                            sms_status="cancelled" if cancelled else "failed", stage="cancelled" if cancelled else "failed",
                            message="注册已保存，自动接码已停止" if cancelled else "注册已保存，自动接码未完成，请从账号页核对",
                            step="注册已保存，自动接码已停止" if cancelled else "注册已保存，自动接码未完成，请从账号页核对")

    def cancel(self) -> None:
        self._cancel.set()

    async def finish(self) -> None:
        if self._finished:
            return
        try:
            if self._tasks:
                await _await_owned(asyncio.gather(*self._tasks.values()), self._cancel.set)
        finally:
            await _await_owned(asyncio.create_task(asyncio.to_thread(self._session.close)), self._cancel.set)
            self._executor.shutdown(wait=True)
            self._finished = True

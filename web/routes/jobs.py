"""Job domain: JobState, AccountReauthBatchJobs and registration job routes."""

from __future__ import annotations

import asyncio
import copy
import os
import secrets
import sys
import threading
import time
import traceback
from collections import deque
from concurrent.futures import FIRST_COMPLETED, wait
from datetime import datetime, timezone
from typing import Any

import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_DUP_TS_RE = re.compile(r"^\d{1,2}:\d{2}:\d{2}\s+")

_TEST_RUNTIME = "unittest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _account_still_exists(account: dict[str, Any]) -> bool:
    """Whether an account queued in a batch is still present in the store.

    Users may delete rows while a long reauth/security batch is running. The
    worker checks each row before spending minutes on it, so deleted accounts
    are skipped instead of being processed and written back.
    """
    if _TEST_RUNTIME:
        return True
    from common.account_store import account_record_exists

    try:
        return account_record_exists(account, _state().ACCOUNTS_FILE)
    except Exception:
        return True


def _state() -> Any:
    # Delayed import is intentional: web_app includes this router.
    import web_app

    return web_app


class StartJobRequest(BaseModel):
    total: int = Field(1, ge=1, le=500)
    concurrency: int = Field(1, ge=1)
    keep_on_fail: bool = False
    email: str | None = None
    mail_type: str | None = "auto"
    maddy_domain: str | None = None
    engine: str | None = None
    group_id: str | None = None
    auto_group: bool = False
    auto_group_size: int = Field(35, ge=1, le=500)
    group_plan: list[str] | None = None


class JobState:
    def __init__(
        self,
        *,
        restore: bool = False,
        persist: bool = False,
        persist_delay: float = 0.25,
    ) -> None:
        self.lock = threading.Lock()
        self._cond = threading.Condition(self.lock)
        self.running = False
        self.stop_requested = False
        self.paused = False
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.params: dict[str, Any] = {}
        self.success_count = 0
        self.fail_count = 0
        self.skipped_count = 0
        self.total = 0
        self.last_error = ""
        self.results: list[dict[str, Any]] = []
        self.task_cards: dict[int, dict[str, Any]] = {}
        self._task_revision = 0
        # SSE clients ask for the task snapshot every second. Keep the
        # expensive ordering/copy work tied to the mutation revision instead
        # of repeating it for every heartbeat and every connected client.
        self._task_stream_cache_revision = -1
        self._task_stream_cache: list[dict[str, Any]] | None = None
        self.batch_id = ""
        self.logs: deque[str] = deque(maxlen=_state().MAX_LOG_LINES)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cancel_event: asyncio.Event | None = None
        self._registration_task: asyncio.Task | None = None
        self._persistence_enabled = bool(persist)
        self._persist_delay = max(0.01, float(persist_delay))
        self._persist_timer_lock = threading.Lock()
        self._persist_timer: threading.Timer | None = None
        self._persistence_write_lock = threading.Lock()
        if restore:
            self._restore_runtime()

    def _restore_runtime(self) -> None:
        saved = _state().load_task_runtime()
        last = saved.get("last_job") if isinstance(saved.get("last_job"), dict) else {}
        if not last:
            return
        self.batch_id = str(last.get("batch_id") or "")
        self.started_at = str(last.get("started_at") or "") or None
        self.finished_at = str(last.get("finished_at") or "") or None
        self.params = dict(last.get("params") or {})
        self.total = max(0, int(last.get("total") or 0))
        self.last_error = str(last.get("last_error") or "")
        interrupted = False
        for item in saved.get("cards") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("batch_id") or "") != self.batch_id:
                continue
            try:
                key = int(item.get("index"))
            except Exception:
                continue
            card = dict(item)
            if str(card.get("status") or "") in {"queued", "running"}:
                card["status"] = "cancelled"
                card["error"] = card.get("error") or "service_restarted"
                card["finished_at_epoch"] = card.get("updated_at_epoch") or time.time()
                interrupted = True
            self.task_cards[key] = card
        if interrupted and not self.finished_at:
            self.finished_at = datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
            self.last_error = self.last_error or "service_restarted"
        if self.task_cards:
            self._task_revision = 1
        self._recompute_counts_locked()
        if not self.task_cards:
            self.success_count = max(0, int(last.get("success_count") or 0))
            self.fail_count = max(0, int(last.get("fail_count") or 0))
            self.skipped_count = max(0, int(last.get("skipped_count") or 0))

    def append_log(self, line: str) -> None:
        # protocol.log() prints ANSI-colored lines with their own HH:MM:SS
        # prefix; the stdout Tee forwards them here, so strip both to keep
        # the panel log readable with a single timestamp.
        text = _ANSI_RE.sub("", str(line)).rstrip()
        if not text:
            return
        text = _DUP_TS_RE.sub("", text, count=1)
        ts = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.logs.append(f"[{ts}] {text}")

    def recent_logs(self, limit: int = 300) -> list[str]:
        with self.lock:
            items = list(self.logs)
        return items[-max(1, min(limit, _state().MAX_LOG_LINES)) :]

    def task_event(self, index: int, status: str | None = None, **fields: Any) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        now_epoch = time.time()
        try:
            key = int(index)
        except Exception:
            return
        with self.lock:
            card = self.task_cards.setdefault(
                key,
                {
                    "index": key,
                    "batch_id": self.batch_id or "legacy",
                    "card_id": f"{self.batch_id or 'legacy'}:{key}",
                    "batch_started_at": self.started_at or "",
                    "status": "queued",
                    "email": "",
                    "provider": "",
                    "proxy": "",
                    "pid": "",
                    "started_at": now,
                    "started_at_epoch": now_epoch,
                    "updated_at": now,
                    "updated_at_epoch": now_epoch,
                    "steps": [],
                },
            )
            prev_status = str(card.get("status") or "")
            if status:
                card["status"] = status
                if status in {"running", "success", "skipped"}:
                    card.pop("error", None)
                if status in {"success", "failed", "cancelled", "skipped"}:
                    card.setdefault("finished_at_epoch", now_epoch)
                    started_epoch = float(card.get("started_at_epoch") or now_epoch)
                    finished_epoch = float(card.get("finished_at_epoch") or now_epoch)
                    card["elapsed_sec"] = round(
                        max(0.0, finished_epoch - started_epoch), 1
                    )
                else:
                    card.pop("finished_at_epoch", None)
                    card.pop("elapsed_sec", None)
            for k, v in fields.items():
                if k == "step":
                    continue
                if v is not None:
                    card[k] = v
            step = str(fields.get("step") or "").strip()
            if step and status == "success" and card.get("elapsed_sec") is not None:
                step = f"{step} ({float(card['elapsed_sec']):.0f}s)"
            if step:
                steps = card.setdefault("steps", [])
                if isinstance(steps, list):
                    steps.append({"time": now, "text": step, "status": status or card.get("status")})
                    del steps[:-18]
            card["updated_at"] = now
            card["updated_at_epoch"] = now_epoch
            self._task_revision += 1
            if status:
                self._adjust_task_counts_locked(prev_status, card.get("status"))
            persist = status in {"success", "failed", "cancelled", "skipped"}
        if persist:
            self.schedule_persist()

    def _recompute_counts_locked(self) -> None:
        ok = fail = skipped = 0
        for card in self.task_cards.values():
            st = str(card.get("status") or "").lower()
            if st == "success":
                ok += 1
            elif st in {"failed", "cancelled"}:
                fail += 1
            elif st == "skipped":
                skipped += 1
        self.success_count = ok
        self.fail_count = fail
        self.skipped_count = skipped

    @staticmethod
    def _task_count_bucket(status: Any) -> str:
        value = str(status or "").strip().lower()
        if value == "success":
            return "success"
        if value in {"failed", "cancelled"}:
            return "failed"
        if value == "skipped":
            return "skipped"
        return ""

    def _adjust_task_counts_locked(self, previous: Any, current: Any) -> None:
        """Update terminal counters without rescanning the whole task map."""
        old_bucket = self._task_count_bucket(previous)
        new_bucket = self._task_count_bucket(current)
        if old_bucket == new_bucket:
            return
        if old_bucket == "success":
            self.success_count = max(0, self.success_count - 1)
        elif old_bucket == "failed":
            self.fail_count = max(0, self.fail_count - 1)
        elif old_bucket == "skipped":
            self.skipped_count = max(0, self.skipped_count - 1)
        if new_bucket == "success":
            self.success_count += 1
        elif new_bucket == "failed":
            self.fail_count += 1
        elif new_bucket == "skipped":
            self.skipped_count += 1

    def tasks_snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            cards = [dict(v) for _, v in sorted(self.task_cards.items())]
        now_epoch = time.time()
        for card in cards:
            if card.get("elapsed_sec") is not None:
                continue
            started_epoch = float(card.get("started_at_epoch") or 0)
            if started_epoch:
                card["elapsed_sec"] = round(max(0.0, now_epoch - started_epoch), 1)
        return cards

    def task_stream_snapshot(self) -> tuple[int, list[dict[str, Any]]]:
        """Return a consistent revision and current-batch task snapshot."""
        with self.lock:
            revision = self._task_revision
            cached = self._task_stream_cache
            if cached is None or self._task_stream_cache_revision != revision:
                cached = []
                for _, value in sorted(self.task_cards.items()):
                    card = dict(value)
                    # ``steps`` is the only mutable nested task field. Copy
                    # it so JSON serialization outside the lock cannot race
                    # with task_event appending a new step.
                    steps = card.get("steps")
                    if isinstance(steps, list):
                        card["steps"] = [
                            dict(step) if isinstance(step, dict) else step
                            for step in steps
                        ]
                    cached.append(card)
                for card in cached:
                    if card.get("elapsed_sec") is not None:
                        continue
                    started_epoch = float(card.get("started_at_epoch") or 0)
                    if started_epoch:
                        card["elapsed_sec"] = round(max(0.0, time.time() - started_epoch), 1)
                self._task_stream_cache = cached
                self._task_stream_cache_revision = revision
            # Callers serialize this snapshot immediately; treat the cached
            # list as read-only so subsequent heartbeats can reuse it.
            return revision, cached

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "running": self.running,
                "batch_id": self.batch_id,
                "stop_requested": self.stop_requested,
                "paused": self.paused,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "params": dict(self.params),
                "success_count": self.success_count,
                "fail_count": self.fail_count,
                "skipped_count": self.skipped_count,
                "total": self.total,
                "last_error": self.last_error,
                "result_count": len(self.results),
                "task_count": len(self.task_cards),
                "task_revision": self._task_revision,
            }

    def all_tasks_snapshot(self, limit: int | None = None) -> list[dict[str, Any]]:
        saved = _state().load_task_runtime(limit=limit)
        merged: dict[str, dict[str, Any]] = {}
        for item in [*(saved.get("cards") or []), *self.tasks_snapshot()]:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            card_id = str(
                row.get("card_id")
                or f"{row.get('batch_id') or 'legacy'}:{row.get('index') or 0}"
            )
            row["card_id"] = card_id
            merged[card_id] = row
        items = sorted(
            merged.values(),
            key=lambda item: float(
                item.get("updated_at_epoch") or item.get("started_at_epoch") or 0
            ),
            reverse=True,
        )
        return items if limit is None else items[: max(0, int(limit))]

    def schedule_persist(self) -> None:
        """Coalesce bursts of terminal task events into one durable write."""
        if not self._persistence_enabled:
            return
        with self._persist_timer_lock:
            if self._persist_timer is not None:
                return
            timer = threading.Timer(self._persist_delay, self._flush_scheduled_persist)
            timer.daemon = True
            self._persist_timer = timer
            timer.start()

    def _flush_scheduled_persist(self) -> None:
        with self._persist_timer_lock:
            self._persist_timer = None
        self.persist_runtime()

    def persist_runtime(self) -> None:
        if not self._persistence_enabled:
            return
        # Snapshot inside the write lock so a delayed flush cannot overwrite a
        # newer explicit job-finalization write with stale state.
        with self._persistence_write_lock:
            _state().save_task_runtime(self.tasks_snapshot(), self.snapshot())


class AccountReauthBatchJobs:
    """Server-side concurrent reauthentication batches.

    A browser login can take several minutes.  Keeping the whole batch inside
    one HTTP request made duplicate tabs and reverse-proxy timeouts race each
    other. This state object owns one background batch and lets every UI
    observe the same authoritative progress instead.
    """

    def __init__(self, *, max_records: int = 20) -> None:
        self.lock = threading.RLock()
        self.jobs: dict[str, dict[str, Any]] = {}
        # Sensitive operation inputs stay outside public job snapshots.
        self.operation_options: dict[str, dict[str, Any]] = {}
        self.stop_events: dict[str, threading.Event] = {}
        self.threads: dict[str, threading.Thread] = {}
        self.active_job_id = ""
        self.max_records = max(5, int(max_records))

    def _trim_locked(self) -> None:
        if len(self.jobs) <= self.max_records:
            return
        finished = sorted(
            (
                item
                for item in self.jobs.values()
                if item.get("status") not in {"queued", "running", "stopping"}
            ),
            key=lambda item: float(item.get("finished_epoch") or 0),
        )
        for item in finished[: max(0, len(self.jobs) - self.max_records)]:
            job_id = str(item.get("job_id") or "")
            self.jobs.pop(job_id, None)
            self.operation_options.pop(job_id, None)
            self.stop_events.pop(job_id, None)
            thread = self.threads.get(job_id)
            if thread is None or not thread.is_alive():
                self.threads.pop(job_id, None)

    @staticmethod
    def _operation_label(operation: str) -> str:
        return _state()._account_security_operation_label(operation)

    @classmethod
    def _account_item(
        cls,
        record: dict[str, Any],
        operation: str = "reauth",
    ) -> dict[str, Any]:
        return {
            "key": _state()._account_key(record),
            "email": str(record.get("email") or ""),
            "account_id": str(record.get("account_id") or ""),
            "status": "queued",
            "message": f"等待{cls._operation_label(operation)}",
        }

    def start(
        self,
        records: list[dict[str, Any]],
        mode: str = "protocol",
        operation: str = "reauth",
        concurrency: int = 3,
        operation_options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not records:
            raise ValueError("没有可处理的账号")
        operation = _state()._normalize_account_security_operation(operation)
        requested_mode = _state()._normalize_reauth_mode(mode)
        reauth_mode = (
            "protocol"
            if any(_state()._account_requires_protocol_reauth(record) for record in records)
            else requested_mode
        )
        operation_label = self._operation_label(operation)
        requested_workers = min(len(records), max(1, int(concurrency or 3)))
        budget_workers, resource_mode = _state()._account_reauth_resource_budget(
            requested_workers
        )
        worker_count = min(len(records), budget_workers)
        with self.lock:
            if self.active_job_id:
                active = self.jobs.get(self.active_job_id)
                if active and active.get("status") in {"queued", "running", "stopping"}:
                    active_label = self._operation_label(
                        str(active.get("operation") or "reauth")
                    )
                    raise RuntimeError(
                        f"已有{active_label}批次正在运行，可直接查看当前进度"
                    )
            job_id = secrets.token_hex(16)
            item = {
                "job_id": job_id,
                "status": "queued",
                "running": True,
                "stopping": False,
                "stop_requested": False,
                "mode": reauth_mode,
                "operation": operation,
                "requested_concurrency": requested_workers,
                "concurrency": worker_count,
                "throttled": worker_count < requested_workers,
                "resource_mode": resource_mode,
                "total": len(records),
                "done": 0,
                "current": f"正在创建 {worker_count} 路{operation_label}队列",
                "current_items": [],
                "success": 0,
                "failed": 0,
                "cancelled": 0,
                "last": "",
                "last_status": "",
                "last_message": "",
                "message": "",
                "started_at": "",
                "finished_at": "",
                "finished_epoch": 0.0,
                "items": [
                    self._account_item(record, operation)
                    for record in records
                ],
            }
            self.jobs[job_id] = item
            raw_options = operation_options if isinstance(operation_options, dict) else {}
            self.operation_options[job_id] = {
                key: raw_options[key]
                for key in (
                    "new_password",
                    "password_mode",
                    "force_password_confirmation",
                )
                if key in raw_options
            }
            self.stop_events[job_id] = threading.Event()
            self.active_job_id = job_id
            self._trim_locked()
        thread = threading.Thread(
            target=self._run,
            args=(
                job_id,
                [dict(record) for record in records],
                reauth_mode,
                operation,
                worker_count,
            ),
            name=f"account-{operation}-{job_id[:8]}",
            daemon=True,
        )
        with self.lock:
            self.threads[job_id] = thread
        try:
            thread.start()
        except Exception:
            with self.lock:
                self.operation_options.pop(job_id, None)
                self.stop_events.pop(job_id, None)
                self.threads.pop(job_id, None)
                failed_job = self.jobs.get(job_id)
                if failed_job is not None:
                    failed_job.update(
                        status="failed",
                        running=False,
                        stopping=False,
                        message="批次线程启动失败",
                        finished_at=_state()._now_iso(),
                        finished_epoch=time.time(),
                    )
            raise
        return self.snapshot(job_id) or dict(item)

    def _set_item(self, job_id: str, index: int, **fields: Any) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            items = job.get("items")
            if isinstance(items, list) and 0 <= index < len(items):
                items[index].update(fields)

    def stop(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            job = self.jobs.get(str(job_id or ""))
            if not job:
                return None
            if job.get("status") not in {"queued", "running", "stopping"}:
                return copy.deepcopy(job)
            event = self.stop_events.get(str(job_id))
            if event is not None:
                event.set()
            label = self._operation_label(str(job.get("operation") or "reauth"))
            job.update(
                status="stopping",
                stopping=True,
                stop_requested=True,
                message="正在停止当前账号并清理剩余队列",
            )
        _state().JOB.append_log(f"[{label}批次] 已请求停止")
        return self.snapshot(job_id)

    def _stop_event(self, job_id: str) -> threading.Event | None:
        with self.lock:
            return self.stop_events.get(job_id)

    def _record_terminal(
        self,
        job_id: str,
        index: int,
        label: str,
        status: str,
        message: str,
        **result_fields: Any,
    ) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            items = job.get("items")
            if isinstance(items, list) and 0 <= index < len(items):
                items[index].update(
                    status=status,
                    message=message,
                    finished_at=_state()._now_iso(),
                    **result_fields,
                )
            counter = "cancelled" if status == "cancelled" else status
            job[counter] = int(job.get(counter) or 0) + 1
            job["done"] = min(
                int(job.get("total") or 0),
                int(job.get("success") or 0)
                + int(job.get("failed") or 0)
                + int(job.get("cancelled") or 0),
            )
            current_items = job.get("current_items")
            if isinstance(current_items, list):
                try:
                    current_items.remove(label)
                except ValueError:
                    pass
                job["current"] = "；".join(str(value) for value in current_items)
            job["last"] = label
            job["last_status"] = status
            job["last_message"] = message

    def _cancel_remaining(self, job_id: str, start_index: int) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            items = job.get("items")
            cancelled = 0
            if isinstance(items, list):
                for item in items[start_index:]:
                    if str(item.get("status") or "") in {"queued", "running"}:
                        item.update(
                            status="cancelled",
                            message="已停止，未执行",
                            finished_at=_state()._now_iso(),
                        )
                        cancelled += 1
            job["cancelled"] = int(job.get("cancelled") or 0) + cancelled
            job["done"] = int(job.get("total") or 0)

    def _finish_stopped(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            success = int(job.get("success") or 0)
            failed = int(job.get("failed") or 0)
            cancelled = int(job.get("cancelled") or 0)
            label = self._operation_label(str(job.get("operation") or "reauth"))
            job.update(
                status="stopped",
                running=False,
                stopping=False,
                current="",
                done=int(job.get("total") or 0),
                message=(
                    f"{label}已停止：成功 {success}，失败 {failed}，"
                    f"已停止 {cancelled}"
                ),
                finished_at=_state()._now_iso(),
                finished_epoch=time.time(),
            )
            self.operation_options.pop(job_id, None)

    @staticmethod
    def _invoke_reauthenticate(
        account: dict[str, Any],
        mode: str,
        stop_event: threading.Event,
        operation: str = "reauth",
        operation_options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        operation = _state()._normalize_account_security_operation(operation)
        options = operation_options if isinstance(operation_options, dict) else {}
        if operation == "password_change":
            password_mode = _state()._normalize_security_password_mode(
                options.get("password_mode")
            )
            new_password = str(options.get("new_password") or "")
            if password_mode == "random":
                # Generate at the account boundary so every account receives
                # a distinct credential while the job snapshot stays secret-free.
                new_password = _state()._generate_security_password()
                # The generated value is now explicit; the operation layer
                # should validate and persist it without generating twice.
                password_mode = "custom"
            try:
                return _state()._change_account_password_sync(
                    account,
                    new_password,
                    stop_check=stop_event.is_set,
                    password_mode=password_mode,
                )
            except Exception as error:
                # A generated password is intentionally absent from the job
                # options; sanitize any provider echo before it reaches the
                # public per-account progress snapshot.
                raise RuntimeError(
                    _state()._redact_security_detail(error, account, new_password)
                ) from error
        if operation == "totp_change":
            return _state()._change_account_totp_sync(
                account,
                stop_check=stop_event.is_set,
            )
        if operation == "security_repair":
            return _state()._repair_account_security_sync(
                account,
                stop_check=stop_event.is_set,
                force_password_confirmation=bool(
                    options.get("force_password_confirmation")
                ),
            )
        try:
            return _state()._reauthenticate_account_sync(
                account,
                mode=mode,
                stop_check=stop_event.is_set,
            )
        except TypeError as error:
            # Keep compatibility with older integrations that monkey-patched
            # the one-argument helper before mode selection was added.
            if "unexpected keyword argument" not in str(error):
                raise
            return _state()._reauthenticate_account_sync(account)

    def _run(
        self,
        job_id: str,
        records: list[dict[str, Any]],
        mode: str,
        operation: str = "reauth",
        concurrency: int = 3,
    ) -> None:
        stop_event = self._stop_event(job_id)
        if stop_event is None:
            with self.lock:
                self.operation_options.pop(job_id, None)
            return
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                self.operation_options.pop(job_id, None)
                return
            job.update(
                status="stopping" if stop_event.is_set() else "running",
                running=True,
                stopping=stop_event.is_set(),
                started_at=_state()._now_iso(),
                current=f"准备启动 {concurrency} 路{self._operation_label(operation)}",
            )
        operation_label = self._operation_label(operation)
        _state().JOB.append_log(
            f"[{operation_label}批次] 开始 · 共 {len(records)} 个账号 · "
            f"方式={mode} · 并发={concurrency}"
        )
        try:
            def run_one(index: int, account: dict[str, Any]) -> None:
                label = str(account.get("email") or account.get("account_id") or f"账号 {index + 1}")
                if stop_event.is_set():
                    self._record_terminal(
                        job_id,
                        index,
                        label,
                        "cancelled",
                        "已停止，未执行",
                    )
                    _state().JOB.append_log(
                        f"[{operation_label}批次] {index + 1}/{len(records)} · "
                        f"已停止，未执行 · {label}"
                    )
                    return
                if not _account_still_exists(account):
                    self._record_terminal(
                        job_id,
                        index,
                        label,
                        "cancelled",
                        "账号已删除，跳过",
                    )
                    _state().JOB.append_log(
                        f"[{operation_label}批次] {index + 1}/{len(records)} · "
                        f"账号已删除，跳过 · {label}"
                    )
                    return
                with self.lock:
                    job = self.jobs.get(job_id)
                    if not job:
                        return
                    items = job.get("items")
                    if isinstance(items, list) and 0 <= index < len(items):
                        items[index].update(
                            status="running",
                            message=f"正在{operation_label}",
                            started_at=_state()._now_iso(),
                        )
                    current_items = job.setdefault("current_items", [])
                    if isinstance(current_items, list):
                        current_items.append(label)
                        job["current"] = "；".join(
                            str(value) for value in current_items
                        )
                _state().JOB.append_log(
                    f"[{operation_label}批次] {index + 1}/{len(records)} · "
                    f"开始 · {label}"
                )
                result: dict[str, Any] | None = None
                operation_options: dict[str, Any] = {}
                try:
                    with self.lock:
                        operation_options = copy.deepcopy(
                            self.operation_options.get(job_id) or {}
                        )
                    if operation == "reauth" and not operation_options:
                        # Preserve the four-argument call shape used by older
                        # integrations that monkey-patch this dispatcher.
                        result = self._invoke_reauthenticate(
                            account,
                            mode,
                            stop_event,
                            operation,
                        )
                    else:
                        result = self._invoke_reauthenticate(
                            account,
                            mode,
                            stop_event,
                            operation,
                            operation_options,
                        )
                except Exception as error:
                    if stop_event.is_set():
                        message = "已停止"
                        terminal_status = "cancelled"
                    else:
                        message = _state()._redact_security_detail(
                            error,
                            account,
                            str(operation_options.get("new_password") or ""),
                        )
                        terminal_status = "failed"
                else:
                    terminal_status = "success"
                    message = f"{operation_label}成功"
                result_fields: dict[str, Any] = {}
                if terminal_status == "success" and isinstance(result, dict):
                    public = _state().account_public_view(result)
                    result_fields = {
                        "account_status": str(public.get("status") or ""),
                        "health_status": str(public.get("health_status") or "unknown"),
                        "plan_type": str(public.get("plan_type") or "free").lower(),
                        "has_password": bool(public.get("has_password")),
                        "has_totp": bool(public.get("has_totp")),
                    }
                self._record_terminal(
                    job_id,
                    index,
                    label,
                    terminal_status,
                    message,
                    **result_fields,
                )
                if terminal_status == "success":
                    _state().JOB.append_log(
                        f"[{operation_label}批次] {index + 1}/{len(records)} · "
                        f"成功 · {label}"
                    )
                elif terminal_status == "cancelled":
                    _state().JOB.append_log(
                        f"[{operation_label}批次] {index + 1}/{len(records)} · "
                        f"已停止 · {label}"
                    )
                else:
                    _state().JOB.append_log(
                        f"[{operation_label}批次] {index + 1}/{len(records)} · "
                        f"失败 · {label} · {message}"
                    )

            worker_count = max(1, min(int(concurrency or 1), len(records)))
            # Keep the executor queue bounded.  Submitting a large account
            # batch all at once retains one Future (and its captured account)
            # per row, even though only ``worker_count`` calls can run.  A
            # small look-ahead keeps workers fed without retaining an
            # unbounded queue; this also lets a stop request avoid starting
            # the tail of a large batch.
            max_in_flight = max(worker_count, worker_count * 2)
            with _state().ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix=f"account-{operation}-{job_id[:6]}",
            ) as executor:
                in_flight: dict[Any, int] = {}
                next_index = 0

                def submit_next() -> bool:
                    nonlocal next_index
                    if next_index >= len(records) or stop_event.is_set():
                        return False
                    index = next_index
                    next_index += 1
                    in_flight[executor.submit(run_one, index, records[index])] = index
                    return True

                for _ in range(min(len(records), max_in_flight)):
                    if not submit_next():
                        break

                while in_flight:
                    completed, _ = wait(
                        tuple(in_flight),
                        return_when=FIRST_COMPLETED,
                    )
                    for future in completed:
                        in_flight.pop(future, None)
                        # Preserve the existing fail-fast behavior for an
                        # unexpected worker exception.  Normal operation
                        # errors are converted to terminal item states by
                        # ``run_one`` before reaching this point.
                        future.result()
                        submit_next()
                    if stop_event.is_set():
                        # Do not refill the window after a stop request.
                        break

                if stop_event.is_set():
                    # The executor context waits for the submitted window on
                    # exit. Observe those futures too, preserving the former
                    # fail-fast behavior for an unexpected worker exception
                    # instead of silently discarding it during shutdown.
                    for future in tuple(in_flight):
                        future.result()
                    self._cancel_remaining(job_id, next_index)

            with self.lock:
                job = self.jobs.get(job_id)
                if not job:
                    return
                success = int(job.get("success") or 0)
                failed = int(job.get("failed") or 0)
                cancelled = int(job.get("cancelled") or 0)
                stopped = stop_event.is_set()
                job.update(
                    status="stopped" if stopped else "completed",
                    running=False,
                    stopping=False,
                    current="",
                    current_items=[],
                    message=(
                        f"{operation_label}已停止：成功 {success}，失败 {failed}，"
                        f"已停止 {cancelled}"
                        if stopped
                        else f"{operation_label}完成：成功 {success}，失败 {failed}"
                    ),
                    finished_at=_state()._now_iso(),
                    finished_epoch=time.time(),
                )
                self.operation_options.pop(job_id, None)
            _state().JOB.append_log(
                f"[{operation_label}批次] {'已停止' if stopped else '完成'} · "
                f"方式={mode} · 并发={concurrency} · 成功 {success} · "
                f"失败 {failed} · 已停止 {cancelled}"
            )
        except Exception as error:
            message = str(error or "重新登录批次异常")[:400]
            if stop_event.is_set():
                self._cancel_remaining(job_id, 0)
                self._finish_stopped(job_id)
                _state().JOB.append_log(
                    f"[{operation_label}批次] 已停止，剩余账号未执行"
                )
                return
            with self.lock:
                job = self.jobs.get(job_id)
                if job:
                    job.update(
                        status="failed",
                        running=False,
                        stopping=False,
                        current="",
                        message=message,
                        finished_at=_state()._now_iso(),
                        finished_epoch=time.time(),
                    )
                self.operation_options.pop(job_id, None)
            _state().JOB.append_log(f"[{operation_label}批次] 异常结束 · {message}")

    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            item = self.jobs.get(str(job_id or ""))
            return copy.deepcopy(item) if item else None

    def active_snapshot(self) -> dict[str, Any] | None:
        with self.lock:
            job_id = self.active_job_id
        return self.snapshot(job_id) if job_id else None

    def wait(self, job_id: str, timeout: float = 15.0) -> bool:
        with self.lock:
            thread = self.threads.get(str(job_id or ""))
        if thread is None:
            return True
        thread.join(timeout=max(0.0, float(timeout)))
        return not thread.is_alive()


def _run_job_thread(params: dict[str, Any]) -> None:
    avail = params.get("_pool_available")
    pool_t = params.get("_pool_total")
    extra = f" 可用邮箱={avail}/{pool_t}" if avail is not None else ""
    domain_extra = (
        f" Maddy域名={params.get('maddy_domain')}"
        if str(params.get("maddy_domain") or "").strip()
        else ""
    )
    _state().JOB.append_log(
        f"任务启动 总数={params.get('total')} 并发={params.get('concurrency')} "
        f"(请求={params.get('requested_concurrency') or params.get('_requested_concurrency') or params.get('concurrency')}) "
        f"邮箱类型={params.get('mail_type') or params.get('provider_type') or 'auto'} "
        f"引擎={params.get('engine') or 'protocol'}"
        f"{domain_extra}{extra} 分组={params.get('group_name') or params.get('group_id') or '-'}"
    )
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = _state()._LogStream(old_out, _state().JOB)  # type: ignore[assignment]
    sys.stderr = _state()._LogStream(old_err, _state().JOB)  # type: ignore[assignment]
    try:
        cfg = _state().load_config(_state().CONFIG_FILE if _state().CONFIG_FILE.is_file() else None)
        maddy_domain = str(params.get("maddy_domain") or "").strip().lower()
        blocked_domains = _state()._maddy_blocked_domains(_state()._find_provider(cfg, "maddy_webui") or {})
        if maddy_domain and maddy_domain in blocked_domains:
            raise RuntimeError(f"Maddy 域名已屏蔽: {maddy_domain}")
        if maddy_domain:
            _state()._apply_maddy_domain_to_config(cfg, maddy_domain)
        reg = cfg.setdefault("registration", {})
        reg["total"] = int(params.get("total") or 1)
        reg["threads"] = int(params.get("concurrency") or 1)
        if params.get("engine"):
            reg["engine"] = str(params["engine"])

        results = asyncio.run(_state()._run_register_async(cfg, params))
        with _state().JOB.lock:
            _state().JOB.results = list(results or [])
            _state().JOB._recompute_counts_locked()
            if not _state().JOB.task_cards:
                _state().JOB.success_count = len(results or [])
                _state().JOB.fail_count = max(0, _state().JOB.total - _state().JOB.success_count)
                _state().JOB.skipped_count = 0
            else:
                n_ok = len(results or [])
                if n_ok > _state().JOB.success_count:
                    _state().JOB.success_count = n_ok
                unfinished = max(
                    0,
                    _state().JOB.total
                    - _state().JOB.success_count
                    - _state().JOB.fail_count
                    - _state().JOB.skipped_count,
                )
                if unfinished and not _state().JOB.stop_requested:
                    _state().JOB.fail_count += unfinished
            stopped = _state().JOB.stop_requested
            sc, fc, sk, tot = (
                _state().JOB.success_count,
                _state().JOB.fail_count,
                _state().JOB.skipped_count,
                _state().JOB.total,
            )
        if not maddy_domain:
            mail_type = str(params.get("mail_type") or params.get("provider_type") or "").strip().lower()
            if mail_type in {"maddy", "maddy_webui"}:
                provider = _state()._find_provider(cfg, "maddy_webui") or {}
                maddy_domain = str(provider.get("default_domain") or provider.get("domain") or "").strip().lower()
        if maddy_domain:
            _state()._maddy_auto_mark_domain_run(
                maddy_domain,
                result={
                    "ok": not stopped and fc == 0,
                    "success": sc,
                    "failed": fc,
                    "skipped": sk,
                    "total": tot,
                    "batch_id": _state().JOB.batch_id or "",
                    "finished_at": _state()._now_iso(),
                },
            )
        if stopped:
            _state().JOB.append_log(f"任务已停止 · 成功={sc} 失败={fc} 跳过={sk} 总数={tot}")
        else:
            _state().JOB.append_log(f"任务完成 · 成功={sc} 失败={fc} 跳过={sk} 总数={tot}")
        try:
            maintenance = _state()._maintenance_config(cfg)
            webhook = str(maintenance.get("webhook_url") or "").strip()
            if webhook and maintenance.get("notify_on_complete"):
                ok_notice, notice_message = _state().send_webhook(
                    webhook,
                    {
                        "event": "registration_completed",
                        "stopped": stopped,
                        "success": sc,
                        "failed": fc,
                        "skipped": sk,
                        "total": tot,
                        "finished_at": _state()._now_iso(),
                    },
                )
                _state().JOB.append_log(
                    f"Webhook {'发送成功' if ok_notice else '发送失败'} · {notice_message}"
                )
        except Exception as exc:
            _state().JOB.append_log(f"Webhook 通知异常: {str(exc)[:120]}")

    except Exception as e:
        _state().JOB.last_error = str(e)[:500]
        _state().JOB.append_log(f"任务出错: {e}")
        _state().JOB.append_log(traceback.format_exc()[-1500:])
    finally:
        sys.stdout = old_out
        sys.stderr = old_err
        with _state().JOB.lock:
            _state().JOB.running = False
            _state().JOB.finished_at = _state()._now_iso()
            _state().JOB.stop_requested = False
            _state().JOB.paused = False
            _state().JOB._loop = None
            _state().JOB._cancel_event = None
            _state().JOB._registration_task = None
            _state().JOB._thread = None
            _state().JOB._cond.notify_all()
        _state().JOB.persist_runtime()


@router.post("/api/job/start")
async def job_start(body: StartJobRequest):
    params = body.model_dump()

    # Archive the previous batch before reserving and replacing current state.
    _state().JOB.persist_runtime()

    # Reserve the single job slot before touching mailbox state.  Otherwise a
    # second start request can release addresses owned by the active job before
    # it eventually notices JOB.running.
    with _state().JOB.lock:
        if _state().JOB.running:
            raise HTTPException(409, "job already running")
        _state().JOB.running = True
        _state().JOB.stop_requested = False
        _state().JOB.paused = False

    def _prepare() -> dict[str, Any]:
        from mail import count_pool_capacity, release_stale_in_use

        out = dict(params)
        try:
            groups = _state().list_groups(_state().GROUPS_FILE)
            group_id = str(out.get("group_id") or "").strip() or groups[0]["id"]
            group = next((item for item in groups if item["id"] == group_id), None)
            if group is None:
                raise HTTPException(400, "目标分组不存在，请刷新分组列表")
            out["group_id"] = group_id
            out["group_name"] = group["name"]
            release_stale_in_use()
            cfg = _state()._read_config()
            reg_cfg = cfg.get("registration") if isinstance(cfg.get("registration"), dict) else {}
            engine = str(out.get("engine") or reg_cfg.get("engine") or "protocol").strip().lower()
            if engine not in _state().SUPPORTED_REGISTRATION_ENGINES:
                raise HTTPException(400, f"不支持的注册引擎: {engine}")
            out["engine"] = engine
            maddy_domain = str(out.get("maddy_domain") or "").strip().lower()
            blocked_domains = _state()._maddy_blocked_domains(_state()._find_provider(cfg, "maddy_webui") or {})
            if maddy_domain and maddy_domain in blocked_domains:
                raise HTTPException(400, f"Maddy 域名已屏蔽: {maddy_domain}")
            if maddy_domain:
                out["maddy_domain"] = maddy_domain
                out["mail_type"] = "maddy_webui"
                _state()._apply_maddy_domain_to_config(cfg, maddy_domain)
            mail_cfg = cfg.get("mail") if isinstance(cfg.get("mail"), dict) else {}
            mail_type = (out.get("mail_type") or "auto") or "auto"
            want_j = max(1, int(out.get("concurrency") or 1))
            cap = count_pool_capacity(
                mail_cfg,
                provider_type=None
                if mail_type in {"auto", "", "any", "all"}
                else mail_type,
                fixed_email=out.get("email") or None,
                fixed_alias_count=1 if out.get("email") else None,
            )
            available = int(cap.get("available") or 0)
            if out.get("email"):
                alias_batch = str(cap.get("fixed_email_mode") or "") == "alias_batch"
                fixed_tasks = min(1, available) if alias_batch else 1
                if fixed_tasks <= 0:
                    raise HTTPException(
                        400,
                        f"指定邮箱没有可用注册槽位（used={cap.get('used')} "
                        f"split_used={cap.get('split_used')} in_use={cap.get('in_use')}）",
                    )
                out["total"] = fixed_tasks
                out["concurrency"] = fixed_tasks
                out["_requested_concurrency"] = 1 if alias_batch else want_j
                out["requested_concurrency"] = 1 if alias_batch else want_j
                out["_fixed_alias_batch"] = alias_batch
                out["_pool_available"] = available
                out["_pool_total"] = int(cap.get("total") or 0)
            elif available <= 0:
                raise HTTPException(
                    400,
                    f"无可用邮箱（类型={mail_type} 池={cap.get('total')} "
                    f"used={cap.get('used')} in_use={cap.get('in_use')}）"
                    f" — 请添加邮箱或点「释放 failed/in_use」",
                )
            else:
                want_n = int(out.get("total") or 1)
                out["total"] = max(1, min(want_n, available))
                out["_requested_concurrency"] = want_j
                out["requested_concurrency"] = want_j
                effective, resource_mode = _state()._effective_registration_workers(
                    engine,
                    want_j,
                    out["total"],
                )
                out["concurrency"] = effective
                out["_resource_mode"] = resource_mode
                out["_pool_available"] = available
                out["_pool_total"] = int(cap.get("total") or 0)

            # Create the complete plan before workers start. Each task keeps
            # its slot across retries, so concurrency cannot move a boundary.
            requested_plan = out.get("group_plan")
            if isinstance(requested_plan, list) and requested_plan:
                plan = [str(item or "").strip() for item in requested_plan]
                if len(plan) != int(out["total"]):
                    raise HTTPException(400, "任务分组计划与注册数量不一致")
                available_groups = {str(item["id"]) for item in groups}
                if any(item not in available_groups for item in plan):
                    raise HTTPException(400, "任务分组计划包含不存在的分组")
                out["group_plan"] = plan
            elif bool(out.get("auto_group")):
                group_size = max(1, int(out.get("auto_group_size") or 35))
                total = int(out["total"])
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                known_names = {
                    str(item.get("name") or "").casefold() for item in groups
                }
                auto_groups: list[dict[str, Any]] = []
                group_count = (total + group_size - 1) // group_size
                for number in range(1, group_count + 1):
                    base_name = f"注册组 {stamp}-{number:02d}"
                    candidate = base_name
                    suffix = 2
                    while candidate.casefold() in known_names:
                        candidate = f"{base_name}-{suffix}"
                        suffix += 1
                    created = _state().create_group(candidate, _state().GROUPS_FILE)
                    auto_groups.append(created)
                    known_names.add(candidate.casefold())
                out["group_plan"] = [
                    auto_groups[min(index // group_size, len(auto_groups) - 1)]["id"]
                    for index in range(total)
                ]
                out["auto_group_size"] = group_size
                out["auto_group_ids"] = [item["id"] for item in auto_groups]
                out["auto_group_names"] = [item["name"] for item in auto_groups]
                out["group_name"] = f"自动分组（每组 {group_size} 个）"
            else:
                out["group_plan"] = [str(out["group_id"])] * int(out["total"])
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(400, f"自动分组创建失败: {str(e)[:160]}") from e
        except Exception as e:
            out["_cap_error"] = str(e)[:200]
        return out

    try:
        params = await asyncio.to_thread(_prepare)
    except Exception:
        with _state().JOB.lock:
            _state().JOB.running = False
            _state().JOB.stop_requested = False
            _state().JOB.paused = False
            _state().JOB._cond.notify_all()
        raise

    with _state().JOB.lock:
        _state().JOB.batch_id = f"{int(time.time())}-{secrets.token_hex(4)}"
        _state().JOB.started_at = _state()._now_iso()
        _state().JOB.finished_at = None
        params["batch_id"] = _state().JOB.batch_id
        _state().JOB.params = params
        _state().JOB.success_count = 0
        _state().JOB.fail_count = 0
        _state().JOB.skipped_count = 0
        _state().JOB.total = int(params.get("total") or 1)
        _state().JOB.last_error = ""
        _state().JOB.results = []
        _state().JOB.task_cards.clear()
        _state().JOB._task_revision += 1
    _state().JOB.persist_runtime()

    t = threading.Thread(
        target=_run_job_thread,
        args=(params,),
        name="gpt-register-job",
        daemon=True,
    )
    with _state().JOB.lock:
        _state().JOB._thread = t
    t.start()
    return {"ok": True, "job": _state().JOB.snapshot()}


@router.post("/api/job/stop")
async def job_stop():
    not_running = False
    with _state().JOB.lock:
        if not _state().JOB.running:
            not_running = True
            loop = cancel_event = registration_task = None
            engine = "protocol"
        else:
            _state().JOB.stop_requested = True
            _state().JOB.paused = False
            loop = _state().JOB._loop
            cancel_event = _state().JOB._cancel_event
            registration_task = _state().JOB._registration_task
            engine = str(_state().JOB.params.get("engine") or "protocol").strip().lower()

    if not_running:
        return {"ok": True, "message": "not running", "job": _state().JOB.snapshot()}

    _state().JOB.append_log("已请求立即停止 - 正在取消任务并清理资源")

    if loop is not None and cancel_event is not None:
        try:
            def _cancel_registration():
                cancel_event.set()
                # asyncio.to_thread() cannot kill its worker. Detaching the
                # protocol root would make the panel report idle while the
                # synchronous registrar is still running in the background.
                if (
                    engine in {"browser"}
                    and registration_task is not None
                    and not registration_task.done()
                ):
                    registration_task.cancel()

            loop.call_soon_threadsafe(_cancel_registration)
        except Exception as e:
            _state().JOB.append_log(f"停止信号发送失败: {e}")

    if loop is not None:
        try:
            from register_gpt import force_close_all_sessions

            asyncio.run_coroutine_threadsafe(force_close_all_sessions(), loop)
        except Exception as e:
            _state().JOB.append_log(f"安排强制关闭失败: {e}")

    # Do not clear active reservations here. Browser/protocol workers own the
    # mailbox until their finalizer runs; clearing them early lets a new batch
    # allocate an address that the stopping worker still uses. Abandoned
    # reservations are recovered by release_stale_in_use on the next start.
    try:
        released = await asyncio.to_thread(_state().release_stale_in_use)
        if released:
            _state().JOB.append_log(f"停止任务回收过期邮箱占用 {released} 个")
    except Exception as e:
        _state().JOB.append_log(f"停止时回收过期邮箱失败: {e}")

    return {"ok": True, "message": "stopping", "job": _state().JOB.snapshot()}


@router.post("/api/job/pause")
async def job_pause():
    with _state().JOB.lock:
        if not _state().JOB.running:
            raise HTTPException(409, "当前没有运行中的任务")
        _state().JOB.paused = True
    _state().JOB.append_log("已暂停新任务和下一次重试的派发")
    return {"ok": True, "message": "paused", "job": _state().JOB.snapshot()}


@router.post("/api/job/resume")
async def job_resume():
    with _state().JOB.lock:
        if not _state().JOB.running:
            raise HTTPException(409, "当前没有运行中的任务")
        _state().JOB.paused = False
    _state().JOB.append_log("任务派发已继续")
    return {"ok": True, "message": "resumed", "job": _state().JOB.snapshot()}


@router.post("/api/job/retry-failed")
async def job_retry_failed():
    with _state().JOB.lock:
        if _state().JOB.running:
            raise HTTPException(409, "已有任务正在运行")
        failed_items = [
            item
            for item in _state().JOB.task_cards.values()
            if str(item.get("status") or "") in {"failed", "cancelled"}
        ]
        failed = len(failed_items)
        params = dict(_state().JOB.params)
    if failed <= 0:
        raise HTTPException(400, "没有可重试的失败任务")
    old_plan = params.get("group_plan")
    retry_plan: list[str] | None = None
    if isinstance(old_plan, list):
        candidate: list[str] = []
        for item in sorted(failed_items, key=lambda row: int(row.get("index") or 0)):
            try:
                old_index = int(item.get("index") or 0) - 1
            except (TypeError, ValueError):
                candidate = []
                break
            if old_index < 0 or old_index >= len(old_plan):
                candidate = []
                break
            candidate.append(str(old_plan[old_index] or "").strip())
        if len(candidate) == failed and all(candidate):
            retry_plan = candidate
    body = StartJobRequest(
        total=failed,
        concurrency=max(1, int(params.get("concurrency") or 1)),
        keep_on_fail=bool(params.get("keep_on_fail")),
        mail_type=str(params.get("mail_type") or params.get("provider_type") or "auto"),
        engine=str(params.get("engine") or "hybrid"),
        group_id=str(params.get("group_id") or "") or None,
        group_plan=retry_plan,
    )
    return await job_start(body)

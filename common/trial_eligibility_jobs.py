# -*- coding: utf-8 -*-
"""In-memory JP Plus/free-trial eligibility batch jobs."""

from __future__ import annotations

import copy
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any

from common.account_health import InvalidAccessTokenError
from common.account_store import (
    account_delete_key,
    account_is_deactivated,
    update_account_record_fields,
)
from common.trial_eligibility import (
    _status_fields,
    account_access_token,
    check_trial_eligibility,
    check_trial_eligibility_nerver,
    is_trial_probe_retryable,
)
from common import task_checkpoint
from common.trial_proxy_pool import selected_trial_proxies


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _public_item(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": account_delete_key(record),
        "email": str(record.get("email") or ""),
        "account_id": str(record.get("account_id") or ""),
        "status": "queued",
        "result": None,
        "error": "",
        "attempt": 0,
    }


class TrialEligibilityJobs:
    """Run one bounded eligibility batch without persistent legacy job state."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.threads: dict[str, threading.Thread] = {}
        self.stop_events: dict[str, threading.Event] = {}
        self.active_job_id = ""
        # 检查点:任务进度落盘,服务重启后恢复并支持续跑
        self._checkpoint_task = "trial_eligibility"
        self._checkpoint_done: set[str] = set()
        self._checkpoint_saver = task_checkpoint.DebouncedCheckpointSaver(
            self._checkpoint_task, interval=2.0
        )

    def _checkpoint_payload(
        self, job_id: str, running: bool, *, completed: bool = False,
    ) -> dict[str, Any]:
        with self.lock:
            job = self.jobs.get(job_id) or {}
            items = job.get("items") or []
            snapshot = {key: value for key, value in job.items() if key != "items"}
        return {
            "version": 1,
            "task_type": self._checkpoint_task,
            "running": bool(running),
            # A stopped job is not complete: its unfinished keys must remain
            # restorable after a service restart. Successful jobs clear the
            # checkpoint instead of writing a completed payload.
            "completed": bool(completed),
            "keys": [str(item.get("key") or "") for item in items],
            "emails": [str(item.get("email") or "") for item in items],
            "done_keys": sorted(self._checkpoint_done),
            "snapshot": snapshot,
            "saved_at": _now(),
        }

    def start(
        self,
        records: list[dict[str, Any]],
        *,
        concurrency: int = 20,
        attempts: int = 8,
        channel: str = "v4",
    ) -> dict[str, Any]:
        selected = [
            dict(record)
            for record in records
            if isinstance(record, dict)
            and str(record.get("plan_type") or "free").strip().lower() == "free"
            and account_access_token(record)
            and not account_is_deactivated(record)
        ]
        if not selected:
            raise ValueError("所选 Free 账号没有可用的 Access Token")
        with self.lock:
            active = self.jobs.get(self.active_job_id)
            if active and active.get("status") in {"queued", "running", "stopping"}:
                raise RuntimeError("已有优惠检测批次正在运行")
            job_id = uuid.uuid4().hex
            job = {
                "job_id": job_id,
                "provider": "trial_eligibility",
                "status": "queued",
                "stage": "优惠检测排队中",
                "total": len(selected),
                "done": 0,
                "eligible": 0,
                "ineligible": 0,
                "failed": 0,
                "stopped": 0,
                "current": "",
                "concurrency": max(1, min(20, int(concurrency or 1))),
                "attempts": max(1, min(20, int(attempts or 1))),
                "channel": "nerver" if str(channel or "").strip().lower() == "nerver" else "v4",
                "created_at": _now(),
                "started_at": "",
                "finished_at": "",
                "items": [_public_item(record) for record in selected],
            }
            self.jobs[job_id] = job
            self.active_job_id = job_id
            self._checkpoint_done.clear()
            task_checkpoint.save_checkpoint(
                self._checkpoint_task,
                self._checkpoint_payload(job_id, running=True),
            )
            stop_event = threading.Event()
            self.stop_events[job_id] = stop_event
            thread = threading.Thread(
                target=self._run,
                args=(job_id, selected, stop_event),
                daemon=True,
                name=f"trial-check-{job_id[:8]}",
            )
            self.threads[job_id] = thread
            thread.start()
            return copy.deepcopy(job)

    def _run(
        self,
        job_id: str,
        records: list[dict[str, Any]],
        stop_event: threading.Event,
    ) -> None:
        with self.lock:
            job = self.jobs[job_id]
            job.update(status="running", stage="JP 优惠检测运行中", started_at=_now())
            concurrency = int(job["concurrency"])
            max_attempts = int(job["attempts"])

        def run_one(index: int, record: dict[str, Any]) -> tuple[str, dict[str, Any], str, int]:
            if stop_event.is_set():
                return "stopped", {}, "任务已停止", 0
            email = str(record.get("email") or record.get("account_id") or f"账号 {index + 1}")
            with self.lock:
                item = self.jobs[job_id]["items"][index]
                item.update(status="running", started_at=_now())
                self.jobs[job_id]["current"] = email

            channel = "nerver" if str(self.jobs[job_id].get("channel") or "v4") == "nerver" else "v4"
            if channel == "nerver":
                last_error: BaseException | None = None
                for attempt in range(1, 3):
                    if stop_event.is_set():
                        return "stopped", {}, "任务已停止", attempt - 1
                    try:
                        result = check_trial_eligibility_nerver(record.get("access_token"))
                        fields = _status_fields(result)
                        fields["trial_eligibility_method"] = "nerver_check"
                        fields["trial_eligibility_proxy_mode"] = "nerver"
                        update_account_record_fields(
                            {account_delete_key(record): fields}, include_records=False
                        )
                        if result.get("eligibility_status") == "invalid":
                            return "failed", result, str(result.get("label") or "AT 已过期"), attempt
                        eligibility_status = str(result.get("eligibility_status") or "").lower()
                        if eligibility_status in {"eligible", "active"}:
                            state = "eligible"
                        else:
                            state = "ineligible"
                        return state, result, "", attempt
                    except Exception as error:
                        last_error = error
                        if attempt >= 2:
                            break
                        time.sleep(min(2.0, 0.25 * attempt))
                fields = {
                    "trial_eligibility_status": "error",
                    "trial_eligibility_label": "检测失败",
                    "trial_eligibility_checked_at": _now(),
                    "trial_eligibility_reason": "request_error",
                    "trial_eligibility_method": "nerver_check",
                    "trial_eligibility_proxy_mode": "nerver",
                    "trial_eligibility_error": str(last_error or "nerver_check_failed")[:500],
                }
                update_account_record_fields(
                    {account_delete_key(record): fields}, include_records=False
                )
                return "failed", {}, str(last_error or "nerver_check_failed"), 2

            country, proxies = selected_trial_proxies(limit=max_attempts)
            routes = proxies if proxies else [""]
            route_mode = f"{country} proxy" if proxies and country else "direct"
            last_error = None
            for attempt, proxy in enumerate(routes, 1):
                if stop_event.is_set():
                    return "stopped", {}, "任务已停止", attempt - 1
                try:
                    result = check_trial_eligibility(
                        record,
                        proxy=proxy,
                        direct=not bool(proxy),
                    )
                    fields = _status_fields(result)
                    fields["trial_eligibility_proxy_mode"] = route_mode
                    update_account_record_fields(
                        {account_delete_key(record): fields}, include_records=False
                    )
                    eligibility_status = str(result.get("eligibility_status") or "").lower()
                    if result.get("is_deactivated"):
                        state = "failed"
                    elif eligibility_status in {"eligible", "active"}:
                        state = "eligible"
                    else:
                        state = "ineligible"
                    return state, result, "", attempt
                except InvalidAccessTokenError as error:
                    fields = {
                        "trial_eligibility_status": "invalid",
                        "trial_eligibility_label": "AT 已失效",
                        "trial_eligibility_checked_at": _now(),
                        "trial_eligibility_reason": "token_invalidated",
                        "trial_eligibility_method": "accounts_check_v4",
                        "trial_eligibility_proxy_mode": route_mode,
                        "trial_eligibility_error": str(error)[:500],
                        "health_status": "invalid",
                        "health_error": str(error)[:500],
                        "last_invalid_at": _now(),
                    }
                    update_account_record_fields(
                        {account_delete_key(record): fields}, include_records=False
                    )
                    return "failed", {}, str(error), attempt
                except Exception as error:
                    last_error = error
                    if not is_trial_probe_retryable(error) or attempt >= len(routes):
                        break
                    time.sleep(min(2.0, 0.25 * attempt))

            retryable = is_trial_probe_retryable(last_error)
            fields = {
                "trial_eligibility_status": "error",
                "trial_eligibility_label": (
                    f"待复测（{country or '网络'}线路异常）" if retryable else "检测失败"
                ),
                "trial_eligibility_checked_at": "" if retryable else _now(),
                "trial_eligibility_reason": "route_retry_pending" if retryable else "request_error",
                "trial_eligibility_method": "accounts_check_v4",
                "trial_eligibility_proxy_mode": route_mode,
                "trial_eligibility_error": str(last_error or "account_check_failed")[:500],
            }
            update_account_record_fields(
                {account_delete_key(record): fields}, include_records=False
            )
            return "failed", {}, str(last_error or "account_check_failed"), len(routes)

        try:
            worker_count = max(1, min(int(concurrency or 1), len(records)))
            # Avoid queueing one Future for every account.  Eligibility jobs
            # can cover the entire account store, while only ``worker_count``
            # probes can run concurrently.  A two-wave look-ahead keeps the
            # workers fed and bounds retained record/Future references.
            max_in_flight = max(worker_count, worker_count * 2)
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix=f"trial-{job_id[:6]}",
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

                def mark_unsubmitted_stopped(start_index: int) -> None:
                    """Account for records never submitted after a stop."""
                    with self.lock:
                        job = self.jobs[job_id]
                        for index in range(start_index, len(records)):
                            item = job["items"][index]
                            if item.get("status") not in {"queued", "running"}:
                                continue
                            item.update(
                                status="stopped",
                                result=None,
                                error="任务已停止",
                                attempt=0,
                                finished_at=_now(),
                            )
                            job["stopped"] = int(job.get("stopped") or 0) + 1
                            job["done"] = int(job.get("done") or 0) + 1
                        job["stage"] = f"{job['done']} / {job['total']} 已完成"

                while in_flight:
                    completed, _ = wait(
                        tuple(in_flight),
                        return_when=FIRST_COMPLETED,
                    )
                    for future in completed:
                        index = in_flight.pop(future)
                        try:
                            state, result, error, attempt = future.result()
                        except Exception as exc:
                            state, result, error, attempt = "failed", {}, str(exc), 0
                        with self.lock:
                            job = self.jobs[job_id]
                            item = job["items"][index]
                            item.update(
                                status="success" if state in {"eligible", "ineligible"} else state,
                                result=result or None,
                                error=error[:500],
                                attempt=attempt,
                                finished_at=_now(),
                            )
                            if state == "discount":
                                state = "ineligible"
                            job[state] = int(job.get(state) or 0) + 1
                            job["done"] = int(job.get("done") or 0) + 1
                            job["stage"] = f"{job['done']} / {job['total']} 已完成"
                        item_key = str(item.get("key") or "")
                        if item_key:
                            self._checkpoint_done.add(item_key)
                            self._checkpoint_saver.submit(
                                lambda: self._checkpoint_payload(job_id, running=True)
                            )
                        if not stop_event.is_set():
                            submit_next()
                if stop_event.is_set():
                    mark_unsubmitted_stopped(next_index)
        finally:
            with self.lock:
                job = self.jobs[job_id]
                stopped = stop_event.is_set()
                job.update(
                    status=("partial" if stopped or job["failed"] else "success"),
                    stage="已停止" if stopped else "优惠检测完成",
                    current="",
                    finished_at=_now(),
                )
                if self.active_job_id == job_id:
                    self.active_job_id = ""
            # 检查点收尾:全部跑完(含失败)清除;停止保留供续跑
            if not stopped:
                task_checkpoint.clear_checkpoint(self._checkpoint_task)
            else:
                self._checkpoint_saver.flush_now(
                    lambda: self._checkpoint_payload(job_id, running=False)
                )

    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            job = self.jobs.get(str(job_id or ""))
            return copy.deepcopy(job) if job else None

    def active(self) -> dict[str, Any] | None:
        with self.lock:
            job = self.jobs.get(self.active_job_id)
            return copy.deepcopy(job) if job else None

    def stop(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            job = self.jobs.get(str(job_id or ""))
            if not job:
                return None
            event = self.stop_events.get(str(job_id or ""))
            if event:
                event.set()
            if job.get("status") in {"queued", "running"}:
                job.update(status="stopping", stage="正在停止")
            return copy.deepcopy(job)

    def wait(self, job_id: str, timeout: float) -> bool:
        with self.lock:
            thread = self.threads.get(str(job_id or ""))
        if not thread:
            return True
        thread.join(max(0.0, float(timeout)))
        return not thread.is_alive()


TRIAL_ELIGIBILITY_JOBS = TrialEligibilityJobs()


def _restore_trial_eligibility_checkpoint() -> None:
    """服务启动时恢复未完成的优惠检测任务(interrupted + 剩余账号)。"""
    checkpoint = task_checkpoint.load_checkpoint(
        TRIAL_ELIGIBILITY_JOBS._checkpoint_task
    )
    if not checkpoint or checkpoint.get("completed"):
        return
    keys = [str(key) for key in (checkpoint.get("keys") or []) if str(key)]
    emails = [str(email) for email in (checkpoint.get("emails") or [])]
    emails_by_key = dict(zip(keys, emails))
    done = {str(key) for key in (checkpoint.get("done_keys") or []) if str(key)}
    pending = [key for key in keys if key not in done]
    snapshot = dict(checkpoint.get("snapshot") or {})
    interrupted = bool(checkpoint.get("running"))
    job = dict(snapshot)
    job.update(
        {
            "job_id": "restored-" + uuid.uuid4().hex[:8],
            "status": "interrupted",
            "stage": (
                f"服务重启中断：已完成 {len(done)} / {len(keys)}，剩余 {len(pending)} 个可续跑"
                if interrupted
                else f"任务已停止：剩余 {len(pending)} 个可续跑"
            ),
            "current": "",
            "finished_at": _now(),
            "interrupted": interrupted,
            "pending_keys": pending,
            "items": [
                {
                    "key": key,
                    "email": emails_by_key.get(key, ""),
                    "account_id": "",
                    "status": "queued",
                    "result": None,
                    "error": "",
                    "attempt": 0,
                }
                for key in pending
            ],
        }
    )
    with TRIAL_ELIGIBILITY_JOBS.lock:
        TRIAL_ELIGIBILITY_JOBS.jobs[job["job_id"]] = job
        TRIAL_ELIGIBILITY_JOBS.active_job_id = job["job_id"]


try:
    _restore_trial_eligibility_checkpoint()
except Exception:
    pass

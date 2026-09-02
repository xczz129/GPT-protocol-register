"""Small in-memory coordinator for email-rebinding batches.

The coordinator deliberately keeps credentials and mailbox provider details in
worker-only closures. Public snapshots contain identities, progress and masked
tokens only.
"""

from __future__ import annotations

import copy
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable

from common.account_store import account_delete_key


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class EmailRebindJobs:
    """Own one bounded rebind batch and expose immutable progress snapshots."""

    def __init__(self, *, max_records: int = 20) -> None:
        self.lock = threading.RLock()
        self.jobs: dict[str, dict[str, Any]] = {}
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
            self.stop_events.pop(job_id, None)
            thread = self.threads.get(job_id)
            if thread is None or not thread.is_alive():
                self.threads.pop(job_id, None)

    @staticmethod
    def _record_key(record: dict[str, Any]) -> str:
        """Resolve the same stable identity used by the account store."""
        explicit = str(record.get("delete_key") or record.get("account_key") or "").strip()
        return explicit or account_delete_key(record)

    @classmethod
    def _item(cls, record: dict[str, Any], target: str = "") -> dict[str, Any]:
        return {
            "key": cls._record_key(record),
            "email": str(record.get("email") or ""),
            "old_email": str(record.get("email") or ""),
            "new_email": str(target or ""),
            "account_id": str(record.get("account_id") or ""),
            "status": "queued",
            "message": "等待换绑",
            "started_at": "",
            "finished_at": "",
        }

    def start(
        self,
        records: list[dict[str, Any]],
        reservations: dict[str, dict[str, Any]],
        *,
        concurrency: int,
        worker: Callable[..., dict[str, Any]],
        skipped: list[str] | None = None,
    ) -> dict[str, Any]:
        if not records:
            raise ValueError("没有可处理的账号")
        workers = min(len(records), max(1, int(concurrency or 1)))
        with self.lock:
            active = self.jobs.get(self.active_job_id) if self.active_job_id else None
            if active and active.get("status") in {"queued", "running", "stopping"}:
                raise RuntimeError("已有换绑批次正在运行")
            job_id = secrets.token_hex(16)
            items = []
            for record in records:
                key = self._record_key(record)
                reservation = reservations.get(key) or {}
                target = str(reservation.get("email") or reservation.get("address") or "")
                items.append(self._item(record, target))
            job = {
                "job_id": job_id,
                "status": "queued",
                "running": True,
                "stopping": False,
                "stop_requested": False,
                "mode": "protocol",
                "operation": "email_rebind",
                "requested_concurrency": workers,
                "concurrency": workers,
                "total": len(records),
                "done": 0,
                "current": f"正在创建 {workers} 路换绑队列",
                "success": 0,
                "failed": 0,
                "cancelled": 0,
                "last": "",
                "last_status": "",
                "last_message": "",
                "skipped": [str(item)[:400] for item in (skipped or []) if str(item).strip()],
                "started_at": "",
                "finished_at": "",
                "finished_epoch": 0.0,
                "items": items,
            }
            self.jobs[job_id] = job
            stop_event = threading.Event()
            self.stop_events[job_id] = stop_event
            self.active_job_id = job_id
            self._trim_locked()
        thread = threading.Thread(
            target=self._run,
            args=(job_id, [dict(item) for item in records], copy.deepcopy(reservations), workers, worker),
            daemon=True,
            name=f"email-rebind-{job_id[:8]}",
        )
        with self.lock:
            self.threads[job_id] = thread
        thread.start()
        return self.snapshot(job_id) or copy.deepcopy(job)

    def _set_item(self, job_id: str, index: int, **fields: Any) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            items = job.get("items")
            if isinstance(items, list) and 0 <= index < len(items):
                items[index].update(fields)

    def _event_sink(self, job_id: str, index: int) -> Callable[..., None]:
        def sink(event_index: int, status: str | None = None, **fields: Any) -> None:
            # Each sink is bound to one worker item. Protocol callbacks use a
            # one-based task index while the batch stores zero-based indexes;
            # using the closure index avoids an off-by-one update (and also
            # protects against wrappers that emit a stale index on retry).
            item_index = index
            value = str(status or "running").lower()
            message = str(fields.get("error") or fields.get("step") or fields.get("message") or "").strip()
            update: dict[str, Any] = {"message": message[:400]}
            if fields.get("new_email"):
                update["new_email"] = str(fields["new_email"])[:320]
            if fields.get("account_id"):
                update["account_id"] = str(fields["account_id"])[:320]
            if fields.get("access_token_masked"):
                update["access_token_masked"] = str(fields["access_token_masked"])[:80]
            if value in {"running", "queued"}:
                update["status"] = "running"
                update.setdefault("started_at", _now())
            elif value in {"success", "completed", "complete"}:
                update["status"] = "success"
                update["finished_at"] = _now()
            elif value in {"cancelled", "canceled", "stopped"}:
                update["status"] = "cancelled"
                update["finished_at"] = _now()
            elif value in {"failed", "error"}:
                update["status"] = "failed"
                update["finished_at"] = _now()
            self._set_item(job_id, item_index, **update)
            with self.lock:
                job = self.jobs.get(job_id)
                if job:
                    if message:
                        job["current"] = message[:400]
                        job["last_message"] = message[:400]
                    job["last"] = str(job.get("items", [])[item_index].get("email") or "") if isinstance(job.get("items"), list) and item_index < len(job["items"]) else ""
                    job["last_status"] = value

        return sink

    def _terminal(self, job_id: str, index: int, status: str, message: str, result: dict[str, Any] | None = None) -> None:
        result = result if isinstance(result, dict) else {}
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            items = job.get("items")
            if isinstance(items, list) and 0 <= index < len(items):
                item = items[index]
                item.update(
                    status=status,
                    message=str(message or "")[:400],
                    finished_at=_now(),
                )
                for key in ("new_email", "account_id", "access_token_masked"):
                    if result.get(key):
                        item[key] = str(result[key])[:320]
            counter = "cancelled" if status == "cancelled" else status
            if counter in {"success", "failed", "cancelled"}:
                job[counter] = int(job.get(counter) or 0) + 1
            job["done"] = min(int(job.get("total") or 0), int(job.get("success") or 0) + int(job.get("failed") or 0) + int(job.get("cancelled") or 0))
            job["last"] = str((items[index] if isinstance(items, list) and index < len(items) else {}).get("email") or "")
            job["last_status"] = status
            job["last_message"] = str(message or "")[:400]

    def _run(self, job_id: str, records: list[dict[str, Any]], reservations: dict[str, dict[str, Any]], workers: int, worker: Callable[..., dict[str, Any]]) -> None:
        stop_event = self.stop_events.get(job_id) or threading.Event()
        with self.lock:
            job = self.jobs.get(job_id)
            if job:
                job["status"] = "running"
                job["started_at"] = _now()
                job["current"] = "换绑任务运行中"
        def one(index: int, record: dict[str, Any]) -> tuple[int, dict[str, Any] | None, Exception | None]:
            key = self._record_key(record)
            reservation = reservations.get(key) or {}
            # Always enter the worker, even when stop was requested before a
            # queued future started.  The worker owns mailbox cleanup; an
            # early return here would strand its ``in_use`` reservation.
            try:
                result = worker(
                    index=index + 1,
                    record=record,
                    reservation=reservation,
                    stop_check=stop_event.is_set,
                    task_event=self._event_sink(job_id, index),
                )
                return index, result if isinstance(result, dict) else {}, None
            except Exception as error:  # terminal classification happens below
                return index, None, error
        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"rebind-{job_id[:6]}") as pool:
                futures = [pool.submit(one, index, record) for index, record in enumerate(records)]
                for future in as_completed(futures):
                    index, result, error = future.result()
                    if error is None:
                        self._terminal(job_id, index, "success", "换绑成功", result)
                    elif stop_event.is_set() or str(error).lower() == "cancelled":
                        self._terminal(job_id, index, "cancelled", "已停止，未完成", {})
                    else:
                        self._terminal(job_id, index, "failed", str(error), {})
            with self.lock:
                job = self.jobs.get(job_id)
                if not job:
                    return
                stopped = stop_event.is_set()
                # Any queued items that never reached a worker become cancelled.
                if stopped and isinstance(job.get("items"), list):
                    for item in job["items"]:
                        if item.get("status") in {"queued", "running"}:
                            item.update(status="cancelled", message="已停止，未执行", finished_at=_now())
                            job["cancelled"] = int(job.get("cancelled") or 0) + 1
                job["done"] = int(job.get("total") or 0)
                job.update(
                    status="stopped" if stopped else "completed",
                    running=False,
                    stopping=False,
                    current="",
                    finished_at=_now(),
                    finished_epoch=time.time(),
                    message=(
                        f"换绑已停止：成功 {job.get('success', 0)}，失败 {job.get('failed', 0)}"
                        if stopped
                        else f"换绑完成：成功 {job.get('success', 0)}，失败 {job.get('failed', 0)}"
                    ),
                )
                self._trim_locked()
        except Exception as error:
            with self.lock:
                job = self.jobs.get(job_id)
                if job:
                    job.update(status="failed", running=False, stopping=False, current="", message=str(error)[:400], finished_at=_now(), finished_epoch=time.time())

    def stop(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            job = self.jobs.get(str(job_id or ""))
            if not job:
                return None
            if job.get("status") not in {"queued", "running", "stopping"}:
                return copy.deepcopy(job)
            event = self.stop_events.get(str(job_id))
            if event:
                event.set()
            job.update(status="stopping", stopping=True, stop_requested=True, message="正在停止当前换绑并清理剩余队列")
            return copy.deepcopy(job)

    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            item = self.jobs.get(str(job_id or ""))
            return copy.deepcopy(item) if item else None

    def active(self) -> dict[str, Any] | None:
        with self.lock:
            job_id = self.active_job_id
        return self.snapshot(job_id) if job_id else None


__all__ = ["EmailRebindJobs"]

"""Durable, bounded follow-ups for explicitly opted-in registration batches.

Only batch IDs, email addresses and non-secret run settings are stored here.
The existing SMS worker owns all authentication, supplier calls and retries.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Callable

from config import ROOT

MAX_PENDING_BATCHES = 100
MAX_EMAILS_PER_BATCH = 500
SMS_CHUNK_SIZE = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RegistrationSmsQueue:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else Path(
            os.environ.get("REGISTRATION_SMS_QUEUE_FILE") or (
                Path(os.environ["TASK_CHECKPOINT_DIR"]) / "registration_sms_queue.sqlite3"
                if os.environ.get("TASK_CHECKPOINT_DIR") else ROOT / "data" / "registration_sms_queue.sqlite3"
            )
        )
        self._lock = threading.RLock()
        self._initialized = False
        self._draining = False
        self._wake_requested = False

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("CREATE TABLE IF NOT EXISTS followups (source_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL)")
        if not self._initialized:
            # A process may have exited after buying a number. Never replay a
            # previously dispatched chunk automatically after a restart.
            rows = connection.execute("SELECT source_id,payload FROM followups WHERE status='running'").fetchall()
            for source_id, payload in rows:
                row = json.loads(payload)
                row.update(status="interrupted", message="服务重启，已启动的接码结果需人工核对，未自动重复执行", updated_at=_now())
                connection.execute("UPDATE followups SET status=?,payload=? WHERE source_id=?", (row["status"], json.dumps(row, ensure_ascii=False), source_id))
            connection.commit()
            self._initialized = True
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _read(connection, source_id: str) -> dict[str, Any] | None:
        found = connection.execute("SELECT payload FROM followups WHERE source_id=?", (source_id,)).fetchone()
        return json.loads(found[0]) if found else None

    @staticmethod
    def _write(connection, row: dict[str, Any]) -> None:
        row["updated_at"] = _now()
        connection.execute(
            "INSERT INTO followups(source_id,created_at,status,payload) VALUES(?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET status=excluded.status,payload=excluded.payload",
            (row["source_batch_id"], row["created_at"], row["status"], json.dumps(row, ensure_ascii=False)),
        )
        connection.commit()

    @staticmethod
    def public(row: dict[str, Any] | None, source_id: str = "") -> dict[str, Any]:
        if not row:
            return {"status": "pending", "total": 0, "message": "等待本批注册完成", "batch_id": "", "source_batch_id": source_id, "completed": 0, "pending": 0, "failed": 0}
        return {key: row.get(key) for key in (
            "status", "total", "message", "batch_id", "source_batch_id", "completed", "failed", "created_at", "updated_at",
        )} | {"pending": len(row.get("remaining") or []) + len(row.get("active_emails") or [])}

    def snapshot(self, source_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            return self.public(self._read(connection, source_id), source_id)

    def enqueue(self, source_id: str, emails: list[str], *, sms_options: dict[str, Any] | None = None,
                cancelled: bool = False, error: str = "") -> dict[str, Any]:
        if not source_id:
            raise ValueError("注册批次 ID 缺失，未启动自动接码")
        unique = list(dict.fromkeys(str(email).strip().lower() for email in emails if str(email).strip()))
        if len(unique) > MAX_EMAILS_PER_BATCH:
            raise ValueError("自动接码单个注册批次最多 500 个账号")
        safe_options = {key: value for key, value in dict(sms_options or {}).items() if key in {
            "provider", "country_order", "max_price", "max_phone_attempts", "timeout_seconds", "concurrency",
        }}
        with self._lock, self._connect() as connection:
            existing = self._read(connection, source_id)
            if existing:
                return self.public(existing)
            queued = connection.execute("SELECT COUNT(*) FROM followups WHERE status IN ('pending','running')").fetchone()[0]
            if queued >= MAX_PENDING_BATCHES:
                error = "自动接码待处理队列已满，请从账号页手动处理本批成功账号"
            status = "cancelled" if cancelled else "failed" if error else "pending" if unique else "completed"
            row = {
                "source_batch_id": source_id, "batch_id": "", "status": status,
                "total": len(unique), "completed": 0, "failed": 0,
                "remaining": unique, "active_emails": [], "sms_options": safe_options,
                "message": "注册已停止，未自动接码" if cancelled else error or ("等待接码空闲，已加入队列" if unique else "本批没有可自动接码的注册成功账号"),
                "created_at": _now(), "updated_at": _now(),
            }
            self._write(connection, row)
            return self.public(row)

    def cancel(self, source_id: str = "", *, message: str = "已取消排队，未再启动自动接码") -> None:
        with self._lock, self._connect() as connection:
            if source_id and not self._read(connection, source_id):
                # A stop can race the registration finalizer before enqueue.
                # Persist a tombstone so that finalizer cannot start payments.
                self._write(connection, {
                    "source_batch_id": source_id, "batch_id": "", "status": "cancelled",
                    "total": 0, "completed": 0, "failed": 0, "remaining": [], "active_emails": [],
                    "sms_options": {}, "message": message, "created_at": _now(),
                })
            query = "SELECT source_id,payload FROM followups WHERE status='pending' OR (status='running' AND json_extract(payload,'$.batch_id')='')"
            for key, payload in connection.execute(query).fetchall():
                if source_id and key != source_id:
                    continue
                row = json.loads(payload)
                row.update(status="cancelled", message=message)
                self._write(connection, row)

    def may_start(self, source_id: str) -> bool:
        with self._lock, self._connect() as connection:
            row = self._read(connection, source_id)
            return bool(row and row["status"] == "running" and not row.get("batch_id"))

    def started(self, source_id: str, batch_id: str) -> bool:
        with self._lock, self._connect() as connection:
            row = self._read(connection, source_id)
            if not row or row["status"] != "running" or row.get("batch_id"):
                return False
            row.update(batch_id=batch_id, message="正在执行注册后自动接码/Codex 授权")
            self._write(connection, row)
            return True

    def finished(self, source_id: str, batch_id: str, status: str, *, completed: int = 0, failed: int = 0) -> None:
        with self._lock, self._connect() as connection:
            row = self._read(connection, source_id) if source_id else None
            if row and row["status"] == "running" and row.get("batch_id") == batch_id:
                chunk_size = len(row.get("active_emails") or [])
                row["completed"] += min(chunk_size, max(0, int(completed)))
                row["failed"] += min(chunk_size, max(0, int(failed)))
                row["active_emails"] = []
                if status == "completed" and int(completed) < chunk_size:
                    status = "failed"
                if status in {"failed", "cancelled"}:
                    row.update(status=status, message="接码已停止，剩余账号未自动执行" if status == "cancelled" else "接码任务异常结束，请在接码记录中核对")
                elif row["remaining"]:
                    row.update(status="pending", batch_id="", message="本段已结束，剩余成功账号继续排队")
                else:
                    row.update(status="failed" if row["failed"] else "completed", message="自动接码已结束，部分账号失败，请查看接码记录" if row["failed"] else "本批注册后自动接码已完成")
                self._write(connection, row)
            self._wake_requested = True

    def drain(self, start_batch: Callable[..., dict], stop_check: Callable[[], bool]) -> None:
        with self._lock:
            self._wake_requested = True
            if self._draining:
                return
            self._draining = True
        try:
            while True:
                with self._lock:
                    self._wake_requested = False
                    if stop_check():
                        return
                    with self._connect() as connection:
                        if connection.execute("SELECT 1 FROM followups WHERE status='running' LIMIT 1").fetchone():
                            return
                        found = connection.execute("SELECT payload FROM followups WHERE status='pending' ORDER BY created_at,rowid LIMIT 1").fetchone()
                        if not found:
                            return
                        row = json.loads(found[0])
                        row["active_emails"], row["remaining"] = row["remaining"][:SMS_CHUNK_SIZE], row["remaining"][SMS_CHUNK_SIZE:]
                        row.update(status="running", batch_id="", message="正在检查接码配置与账号状态")
                        self._write(connection, row)
                try:
                    if stop_check():
                        self.cancel(row["source_batch_id"], message="服务退出，尚未启动自动接码")
                        return
                    start_batch(set(row["active_emails"]), sms_options=row["sms_options"], source_registration_batch_id=row["source_batch_id"])
                except Exception as exc:
                    busy = getattr(exc, "status_code", None) == 409
                    with self._lock, self._connect() as connection:
                        current = self._read(connection, row["source_batch_id"])
                        if current and current["status"] == "running" and not current.get("batch_id"):
                            current["remaining"] = current["active_emails"] + current["remaining"]
                            current["active_emails"] = []
                            safe_errors = {
                                "选中账号在库中不存在": "本批账号已不在账号库，未自动接码",
                                "自动接码需要已启用且配置完整的短信渠道": "自动接码未启动，请在短信设置中启用接码并配置渠道凭据",
                                "自动接码已停止": "自动接码已停止，未继续执行",
                            }
                            message = safe_errors.get(getattr(exc, "detail", None), "自动接码预检或启动失败，请检查接码配置与任务日志") if isinstance(getattr(exc, "detail", None), str) else "自动接码预检或启动失败，请检查接码配置与任务日志"
                            current.update(status="pending" if busy else "failed", message="其他导入/接码任务运行中，等待空闲" if busy else message)
                            self._write(connection, current)
                        awakened = self._wake_requested
                    if busy and not awakened:
                        return
                # finished() can run before start_batch returns. Re-read the
                # durable state instead of overwriting that early completion.
        finally:
            with self._lock:
                self._draining = False
                retry_wakeup = self._wake_requested
                self._wake_requested = False
            if retry_wakeup and not stop_check():
                self.drain(start_batch, stop_check)


_QUEUE: RegistrationSmsQueue | None = None
_QUEUE_LOCK = threading.Lock()


def queue() -> RegistrationSmsQueue:
    global _QUEUE
    with _QUEUE_LOCK:
        if _QUEUE is None:
            _QUEUE = RegistrationSmsQueue()
        return _QUEUE


def existing_queue() -> RegistrationSmsQueue | None:
    global _QUEUE
    with _QUEUE_LOCK:
        if _QUEUE is not None:
            return _QUEUE
        candidate = RegistrationSmsQueue()
        if candidate.path.is_file():
            _QUEUE = candidate
        return _QUEUE

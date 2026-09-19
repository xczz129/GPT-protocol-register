"""Per-account registration SMS continuations with durable payment ownership."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

from config import ROOT
from common.account_operation_log import _redact_text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _InlineLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.initialized = False

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS accounts (source TEXT NOT NULL, task_index INTEGER NOT NULL, email TEXT NOT NULL, status TEXT NOT NULL, batch_id TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL, PRIMARY KEY(source,task_index))")
            if not self.initialized:
                # A prior process might have bought a number. Explicit user
                # review is required before any further execution of that row.
                connection.execute("UPDATE accounts SET status='interrupted',result=?,updated_at=? WHERE status='started'", (
                    json.dumps({"status": "failed", "sms_status": "interrupted", "message": "服务重启中断接码，需核对历史记录；未自动重复执行"}, ensure_ascii=False), _now(),
                ))
                connection.commit()
                self.initialized = True
            yield connection
        finally:
            connection.close()

    @staticmethod
    def read(connection, source: str, index: int) -> dict | None:
        row = connection.execute("SELECT email,status,batch_id,result FROM accounts WHERE source=? AND task_index=?", (source, index)).fetchone()
        if not row:
            return None
        return {"email": row[0], "state": row[1], "batch_id": row[2], **json.loads(row[3])}

    def reserve(self, source: str, index: int, email: str) -> dict:
        with self.lock, self.connection() as connection:
            connection.execute("INSERT OR IGNORE INTO accounts(source,task_index,email,status,updated_at) VALUES(?,?,?,'pending',?)", (source, index, email, _now()))
            connection.commit()
            return self.read(connection, source, index)

    def start(self, source: str, index: int, batch_id: str) -> bool:
        with self.lock, self.connection() as connection:
            cursor = connection.execute("UPDATE accounts SET status='started',batch_id=?,updated_at=? WHERE source=? AND task_index=? AND status='pending'", (batch_id, _now(), source, index))
            connection.commit()
            return cursor.rowcount == 1

    def finish(self, source: str, index: int, result: dict) -> None:
        with self.lock, self.connection() as connection:
            connection.execute("UPDATE accounts SET status=?,result=?,updated_at=? WHERE source=? AND task_index=? AND status IN ('pending','started')", (result.get("sms_status") or result["status"], json.dumps(result, ensure_ascii=False), _now(), source, index))
            connection.commit()

    def get(self, source: str, index: int) -> dict | None:
        with self.lock, self.connection() as connection:
            return self.read(connection, source, index)

    def snapshot(self, source: str) -> dict:
        with self.lock, self.connection() as connection:
            rows = connection.execute("SELECT status,batch_id FROM accounts WHERE source=? ORDER BY updated_at,task_index", (source,)).fetchall()
        states = [row[0] for row in rows]
        status = "running" if "started" in states else "pending" if "pending" in states else "interrupted" if "interrupted" in states else "failed" if "failed" in states else "cancelled" if "cancelled" in states else "completed"
        return {"status": status, "source_batch_id": source, "batch_id": next((row[1] for row in reversed(rows) if row[1]), ""),
                "total": len(rows), "completed": sum(state in {"success", "failed", "cancelled", "interrupted"} for state in states),
                "failed": sum(state in {"failed", "interrupted"} for state in states), "pending": sum(state in {"pending", "started"} for state in states),
                "message": "每个账号在注册后接续接码/Codex 授权"}


_LEDGERS: dict[Path, _InlineLedger] = {}
_LEDGERS_LOCK = threading.Lock()


def _ledger() -> _InlineLedger:
    override = os.environ.get("REGISTRATION_SMS_INLINE_FILE")
    directory = Path(os.environ.get("TASK_CHECKPOINT_DIR") or ROOT / "data")
    path = Path(override) if override else directory / "registration_sms_inline.sqlite3"
    path = path.resolve()
    with _LEDGERS_LOCK:
        return _LEDGERS.setdefault(path, _InlineLedger(path))


def registration_inline_sms_snapshot(source_id: str) -> dict:
    return _ledger().snapshot(source_id)


class _CombinedStop:
    def __init__(self, session, task_stop_check=None):
        self.session = session
        self.task_stop_check = task_stop_check

    def is_set(self) -> bool:
        from web.routes import codex_oauth as route
        return bool(self.session._stop.is_set() or self.session.stop_check() or route._automatic_sms_stopping()
                    or (callable(self.task_stop_check) and self.task_stop_check()))

    def wait(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0, timeout)
        while not self.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self.session._stop.wait(min(0.1, remaining))
        return True


@dataclass
class _AccountRun:
    email: str
    finished: threading.Event = field(default_factory=threading.Event)
    result: dict = field(default_factory=dict)


class RegistrationInlineSmsSession:
    """One streaming SMS slot; run() owns each account until cleanup completes."""
    def __init__(self, source_id: str, *, event_callback, stop_check) -> None:
        from web.routes import codex_oauth as route
        if not str(source_id or "").strip():
            raise ValueError("注册批次 ID 缺失")
        self.source_id = str(source_id)
        self.event_callback = event_callback
        self.stop_check = stop_check if callable(stop_check) else lambda: False
        self._stop = threading.Event()
        self._condition = threading.Condition(threading.RLock())
        self._slot_lock = threading.Lock()
        self._runs: dict[int, _AccountRun] = {}
        self._inflight = 0
        self._closed = False
        self._closing = False
        self._batch_id = ""
        self._failed = False
        self._ledger = _ledger()
        self._config_error = ""
        self._secrets: tuple = ()
        try:
            cfg = route._state()._read_config()
            self.sms_cfg = route._effective_sms_config(cfg)
            safe = route._safe_sms_settings(self.sms_cfg)
            if not safe["enabled"] or not safe["configured"]:
                raise ValueError("请在短信设置中启用 SMSBower 并配置 API Key")
            self.concurrency = max(1, int((cfg.get("ui_settings") or {}).get("codex_sms_workers") or 2))
            self._secrets = (self.sms_cfg.get("api_key"),)
            self.options = {"strategy": "auto", "force": False, "concurrency": self.concurrency,
                            "source_registration_batch_id": self.source_id, "inline_registration": True,
                            "sms_options": {key: safe[key] for key in ("provider", "country_order", "max_price", "max_phone_attempts", "timeout_seconds")}}
        except Exception:
            self.sms_cfg, self.options, self.concurrency = {}, {}, 1
            self._config_error = "自动接码未启动，请在短信设置中启用 SMSBower 并检查 API Key、价格和换号设置"
        self._permits = threading.Semaphore(self.concurrency)

    def _emit(self, index: int, status: str = "running", *, secrets=(), **fields) -> None:
        if not callable(self.event_callback):
            return
        clean = {}
        for key, value in fields.items():
            if key in {"step", "message", "error", "stage", "email", "sms_status", "retry_action", "authorization_status", "relay_status"}:
                clean[key] = _redact_text(value, (*self._secrets, *secrets))[:500]
            elif key in {"attempt", "wait_deadline", "max_phone_attempts"}:
                clean[key] = value
        clean.setdefault("max_phone_attempts", (self.options.get("sms_options") or {}).get("max_phone_attempts", 0))
        clean.setdefault("sms_status", status)
        clean.setdefault("message", clean.get("step", ""))
        clean.setdefault("step", clean.get("message", ""))
        clean.update(phase="sms", registration_status="success", sms_batch_id=self._batch_id)
        try:
            self.event_callback(index, status, **clean)
        except Exception:
            # UI/audit callbacks must not change the payment/retry outcome.
            pass

    def _ensure_slot(self, stop: _CombinedStop) -> None:
        from web.routes import codex_oauth as route
        while not self._slot_lock.acquire(timeout=0.1):
            if stop.is_set():
                raise route._SmsCancelled("任务已停止")
        try:
            if self._batch_id:
                return
            while True:
                if stop.is_set():
                    raise route._SmsCancelled("任务已停止")
                batch_id = "codex-inline-" + hashlib.sha256(f"{self.source_id}:{time.time_ns()}".encode()).hexdigest()[:16]
                try:
                    event = route._reserve_batch("codex", 0, batch_id)
                except route.HTTPException as exc:
                    if exc.status_code != 409:
                        raise
                    stop.wait(0.1)
                    continue
                self._stop = event
                self._batch_id = batch_id
                with route._LOCK:
                    route._STATE.update(options=dict(self.options), concurrency=self.concurrency, items=[])
                return
        finally:
            self._slot_lock.release()

    def run(self, index: int, record: dict, task_stop_check=None) -> dict:
        from web.routes import codex_oauth as route
        index = int(index)
        email = str(record.get("email") or "").strip().lower()
        with self._condition:
            existing = self._runs.get(index)
            if existing:
                owner = False
                run = existing
            elif self._closing or self._closed:
                return {"status": "cancelled", "sms_status": "cancelled", "message": "接码任务已结束，未再次执行"}
            else:
                owner = True
                run = self._runs[index] = _AccountRun(email)
                self._inflight += 1
        if not owner:
            if run.email != email:
                return {"status": "failed", "sms_status": "failed", "message": "注册任务索引与邮箱不一致，未重复接码"}
            run.finished.wait()
            return dict(run.result)
        stop = _CombinedStop(self, task_stop_check)
        secrets = tuple(record.get(key) for key in ("password", "totp_secret", "access_token", "refresh_token", "session_token"))
        result = {"status": "failed", "sms_status": "failed", "message": "自动接码执行异常，请核对接码记录"}
        permit = False
        owns_row = False
        callback_token = None
        try:
            if not email:
                raise ValueError("注册结果缺少邮箱")
            previous = self._ledger.reserve(self.source_id, index, email)
            if previous["email"] != email:
                raise ValueError("注册任务索引与邮箱不一致")
            if previous["state"] != "pending":
                result = {key: previous[key] for key in ("status", "sms_status", "message", "batch_id", "retry_action", "authorization_status", "relay_status") if key in previous}
                result.setdefault("status", "failed")
                result.setdefault("sms_status", "interrupted")
                result.setdefault("message", "本账号接码已执行或中断，未自动重复执行")
                return result
            owns_row = True
            if self._config_error:
                raise ValueError(self._config_error)
            self._emit(index, email=email, stage="sms_waiting", sms_status="pending", message="注册和测活完成，等待接码执行位")
            while not self._permits.acquire(timeout=0.1):
                if stop.is_set():
                    raise route._SmsCancelled("任务已停止，未启动接码")
            permit = True
            if stop.is_set():
                raise route._SmsCancelled("任务已停止，未启动接码")
            self._ensure_slot(stop)
            if stop.is_set():
                raise route._SmsCancelled("任务已停止，未启动接码")
            with route._LOCK:
                route._STATE["items"].append({"email": email, "status": "pending", "stage": "queued", "message": "等待执行", "attempt": 0, "wait_deadline": None, "max_phone_attempts": self.options["sms_options"]["max_phone_attempts"]})
                route._STATE["total"] = len(route._STATE["items"])
                route._sms_counts_locked()
            if not self._ledger.start(self.source_id, index, self._batch_id):
                owns_row = False
                raise RuntimeError("该账号已有接码执行记录，未重复执行")

            def on_sms_event(**updates):
                # The shared executor reports its terminal item before writing
                # history. Keep the task running until all finalizers finish.
                updates = dict(updates)
                updates.pop("status", None)
                self._emit(index, "running", secrets=secrets, email=email, sms_status="running", **updates)

            callback_token = route._SMS_EVENT_CALLBACK.set(on_sms_event)
            self._emit(index, email=email, stage="sms_running", message="开始短信验证和 Codex 授权")
            full = route._run_sms_record(dict(record), self.sms_cfg, self._batch_id, stop, route._relay_context(), self.options)
            outcome = full.get("outcome") or full.get("status")
            status = "success" if outcome in {"success", "synced", "skipped"} else "cancelled" if outcome == "cancelled" else "failed"
            result = {"status": status, "sms_status": status, "message": _redact_text(full.get("message") or full.get("error") or "接码流程已结束", (*self._secrets, *secrets))[:500],
                      "batch_id": self._batch_id, **{key: str(full.get(key) or "") for key in ("retry_action", "authorization_status", "relay_status")}}
        except Exception as exc:
            cancelled = stop.is_set() or isinstance(exc, route._SmsCancelled)
            status = "cancelled" if cancelled else "failed"
            result = {"status": status, "sms_status": status, "message": _redact_text(str(exc), (*self._secrets, *secrets))[:500] or "自动接码执行异常", "batch_id": self._batch_id}
        finally:
            if callback_token is not None:
                route._SMS_EVENT_CALLBACK.reset(callback_token)
            if owns_row:
                try:
                    self._ledger.finish(self.source_id, index, result)
                except Exception:
                    result = {"status": "failed", "sms_status": "interrupted", "message": "接码结果保存失败，请核对历史记录；未安排自动重试", "batch_id": self._batch_id}
            self._emit(index, result["status"], secrets=secrets, email=email, stage="sms_" + result["sms_status"], **{key: result[key] for key in ("sms_status", "message", "retry_action", "authorization_status", "relay_status") if key in result})
            if permit:
                self._permits.release()
            with self._condition:
                self._failed |= result["status"] == "failed"
                run.result = dict(result)
                run.finished.set()
                self._inflight -= 1
                self._condition.notify_all()
        return dict(result)

    def snapshot(self) -> dict:
        return self._ledger.snapshot(self.source_id)

    def close(self) -> None:
        from web.routes import codex_oauth as route
        with self._condition:
            if self._closed:
                return
            self._closing = True
            while self._inflight:
                self._condition.wait(timeout=0.1)
            self._closed = True
            batch_id = self._batch_id
        if batch_id:
            route._finish_batch(batch_id, failed=self._failed)

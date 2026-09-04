# -*- coding: utf-8 -*-
"""Session-based access-token refresh through the nerver session service."""
from __future__ import annotations

import base64
import copy
import json
import os
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any

import requests

from common.account_store import (
    account_delete_key,
    account_is_deactivated,
    update_account_record_fields,
)

NERVER_SESSION_REFRESH_URL = os.environ.get(
    "NERVER_SESSION_REFRESH_URL",
    "https://cha.nerver.cc/api/v1/session/refresh",
)
DEFAULT_REQUEST_TIMEOUT = 45.0
# AT 视为需要刷新的窗口：已过期或将在 7 天内过期。
NEAR_EXPIRY_SECONDS = 7 * 86400
# 新 AT 必须比旧 AT 新这么多秒才算“真的刷新了”。
REFRESH_MARGIN_SECONDS = 300


class SessionRefreshError(RuntimeError):
    """Raised when the nerver session refresh call fails."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def jwt_expires_at(token: Any) -> int:
    """Best-effort JWT exp extraction; 0 when unparsable."""
    text = str(token or "").strip()
    if not text:
        return 0
    try:
        part = text.split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
        return int(float(payload.get("exp") or 0))
    except Exception:
        return 0


def access_token_needs_refresh(record: dict[str, Any], *, window: int = NEAR_EXPIRY_SECONDS) -> bool:
    """AT 已过期 / 即将过期 / 缺失 / 无法解析 → 需要刷新。"""
    token = str(record.get("access_token") or "").strip()
    if not token:
        return True
    expires_at = jwt_expires_at(token)
    if expires_at <= 0:
        return True
    return expires_at <= time.time() + max(0, int(window))


def refresh_session_via_service(
    session_token: str,
    *,
    timeout: float = DEFAULT_REQUEST_TIMEOUT,
) -> dict[str, Any]:
    """Call nerver POST /api/v1/session/refresh with a sessionToken cookie value.

    Returns a normalized payload:
    ``{access_token, session_token, plan_type, account_id, email, rotated}``.
    Raises :class:`SessionRefreshError` when the service rejects the session.
    """
    token = str(session_token or "").strip()
    if not token:
        raise SessionRefreshError("session_refresh_empty_token: 没有可用的 Session Token")
    try:
        response = requests.post(
            NERVER_SESSION_REFRESH_URL,
            json={"sessionToken": token},
            headers={"content-type": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException as error:
        raise SessionRefreshError(f"session_refresh_request_failed: {str(error)[:300]}") from error
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict) or not data.get("ok"):
        reason = str(data.get("reason") or "") if isinstance(data, dict) else ""
        message = str(data.get("message") or "") if isinstance(data, dict) else ""
        detail = " ".join(part for part in (reason, message, response.text[:160]) if part).strip()
        raise SessionRefreshError(
            f"session_refresh_http_{response.status_code}: {detail or 'refresh failed'}"
        )
    session = data.get("session") if isinstance(data.get("session"), dict) else {}
    access_token = str(data.get("accessToken") or session.get("accessToken") or "").strip()
    if not access_token:
        raise SessionRefreshError("session_refresh_empty_access_token: 服务未返回 accessToken")
    account = session.get("account") if isinstance(session.get("account"), dict) else {}
    user = session.get("user") if isinstance(session.get("user"), dict) else {}
    return {
        "access_token": access_token,
        "session_token": str(
            data.get("sessionToken") or session.get("sessionToken") or token
        ).strip(),
        "plan_type": str(
            data.get("planType") or account.get("planType") or ""
        ).strip(),
        "account_id": str(data.get("account_id") or account.get("id") or "").strip(),
        "email": str(data.get("email") or user.get("email") or "").strip(),
        "rotated": bool(data.get("session_rotated")),
    }


def _public_item(record: dict[str, Any]) -> dict[str, Any]:
    old_exp = jwt_expires_at(record.get("access_token"))
    return {
        "key": account_delete_key(record),
        "email": str(record.get("email") or ""),
        "account_id": str(record.get("account_id") or ""),
        "status": "queued",
        "message": "等待刷新",
        "old_expires_at": old_exp,
        "new_expires_at": 0,
    }


class SessionRefreshJobs:
    """Run one bounded session-refresh batch without persistent job state."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.threads: dict[str, threading.Thread] = {}
        self.stop_events: dict[str, threading.Event] = {}
        self.active_job_id = ""

    def start(
        self,
        records: list[dict[str, Any]],
        *,
        concurrency: int = 4,
    ) -> dict[str, Any]:
        selected = [
            dict(record)
            for record in records
            if isinstance(record, dict)
            and str(record.get("session_token") or "").strip()
            and not account_is_deactivated(record)
        ]
        if not selected:
            raise ValueError("所选账号没有可用的 Session Token")
        with self.lock:
            active = self.jobs.get(self.active_job_id)
            if active and active.get("status") in {"queued", "running", "stopping"}:
                raise RuntimeError("已有刷新 AT 批次正在运行")
            job_id = uuid.uuid4().hex
            job = {
                "job_id": job_id,
                "provider": "session_refresh",
                "status": "queued",
                "stage": "刷新 AT 排队中",
                "total": len(selected),
                "done": 0,
                "refreshed": 0,
                "unchanged": 0,
                "failed": 0,
                "stopped": 0,
                "current": "",
                "concurrency": max(1, min(10, int(concurrency or 1))),
                "created_at": _now(),
                "started_at": "",
                "finished_at": "",
                "items": [_public_item(record) for record in selected],
            }
            self.jobs[job_id] = job
            self.active_job_id = job_id
            stop_event = threading.Event()
            self.stop_events[job_id] = stop_event
            thread = threading.Thread(
                target=self._run,
                args=(job_id, selected, stop_event),
                daemon=True,
                name=f"refresh-at-{job_id[:8]}",
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
            job.update(status="running", stage="刷新 AT 运行中", started_at=_now())
            concurrency = int(job["concurrency"])

        def run_one(index: int, record: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
            if stop_event.is_set():
                return "stopped", {}, "任务已停止"
            email = str(record.get("email") or record.get("account_id") or f"账号 {index + 1}")
            key = account_delete_key(record)
            with self.lock:
                item = self.jobs[job_id]["items"][index]
                item.update(status="running", started_at=_now())
                self.jobs[job_id]["current"] = email
                old_exp = int(item.get("old_expires_at") or 0)
            try:
                result = refresh_session_via_service(str(record.get("session_token") or ""))
            except SessionRefreshError as error:
                message = str(error)
                fields = {
                    "last_token_refresh_at": _now(),
                    "last_token_refresh_error": message[:500],
                }
                if key:
                    update_account_record_fields({key: fields}, include_records=False)
                return "failed", {}, message
            new_exp = jwt_expires_at(result["access_token"])
            fields: dict[str, Any] = {
                "access_token": result["access_token"],
                "session_token": result["session_token"],
                "last_token_refresh_at": _now(),
                "last_token_refresh_error": "",
                # 刷出有效 AT 说明账号活着：把测活失败状态归位，
                # 让该账号退出"测活失败/待重登"池（与重新登录口径对齐）。
                "health_status": "alive",
                "health_error": "",
            }
            if result.get("plan_type"):
                fields["plan_type"] = result["plan_type"]
            if result.get("account_id"):
                fields["account_id"] = result["account_id"]
            if key:
                update_account_record_fields({key: fields}, include_records=False)
            if new_exp > old_exp + REFRESH_MARGIN_SECONDS:
                return (
                    "refreshed",
                    {"new_expires_at": new_exp, "rotated": bool(result.get("rotated"))},
                    "",
                )
            return (
                "unchanged",
                {"new_expires_at": new_exp, "rotated": bool(result.get("rotated"))},
                "上游返回的 AT 未变化（可能仍在缓存期内）",
            )

        try:
            worker_count = max(1, min(int(concurrency or 1), len(records)))
            max_in_flight = max(worker_count, worker_count * 2)
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix=f"refresh-at-{job_id[:6]}",
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
                    with self.lock:
                        job = self.jobs[job_id]
                        for index in range(start_index, len(records)):
                            item = job["items"][index]
                            if item.get("status") not in {"queued", "running"}:
                                continue
                            item.update(
                                status="stopped",
                                message="任务已停止",
                                finished_at=_now(),
                            )
                            job["stopped"] = int(job.get("stopped") or 0) + 1
                            job["done"] = int(job.get("done") or 0) + 1
                        job["stage"] = f"{job['done']} / {job['total']} 已完成"

                while in_flight:
                    completed, _ = wait(tuple(in_flight), return_when=FIRST_COMPLETED)
                    for future in completed:
                        index = in_flight.pop(future)
                        try:
                            state, result, message = future.result()
                        except Exception as exc:
                            state, result, message = "failed", {}, str(exc)
                        with self.lock:
                            job = self.jobs[job_id]
                            item = job["items"][index]
                            item.update(
                                status=state,
                                message=str(message or "")[:500],
                                new_expires_at=int(result.get("new_expires_at") or 0),
                                rotated=bool(result.get("rotated")),
                                finished_at=_now(),
                            )
                            job[state] = int(job.get(state) or 0) + 1
                            job["done"] = int(job.get("done") or 0) + 1
                            job["stage"] = f"{job['done']} / {job['total']} 已完成"
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
                    stage="已停止" if stopped else "刷新 AT 完成",
                    current="",
                    finished_at=_now(),
                )
                if self.active_job_id == job_id:
                    self.active_job_id = ""

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


SESSION_REFRESH_JOBS = SessionRefreshJobs()

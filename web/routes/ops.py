"""Ops panel: job logs, task runtime, operations metrics, maintenance, backups."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from common.mail_registration_metrics import get_summary as mail_registration_summary

router = APIRouter()


def _state() -> Any:
    # Delayed import is intentional: web_app includes this router.
    import web_app

    return web_app


_ACCOUNT_LOG_CACHE_LOCK = threading.RLock()
_ACCOUNT_LOG_CACHE_COND = threading.Condition(_ACCOUNT_LOG_CACHE_LOCK)
_ACCOUNT_LOG_CACHE: dict[
    tuple[Any, ...],
    tuple[float, dict[str, Any], tuple[tuple[str, int, int], ...]],
] = {}
_ACCOUNT_LOG_CACHE_INFLIGHT: set[tuple[Any, ...]] = set()
_ACCOUNT_LOG_CACHE_INFLIGHT_AT: dict[tuple[Any, ...], float] = {}
_ACCOUNT_LOG_CACHE_TTL = 2.0
_ACCOUNT_LOG_CACHE_MAX = 128


class TaskDeleteRequest(BaseModel):
    indexes: list[str | int] = Field(default_factory=list)
    delete_all: bool = False


class MaintenanceUpdate(BaseModel):
    enabled: bool = False
    interval_hours: int = Field(12, ge=1, le=168)
    workers: int = Field(10, ge=1, le=20)
    auto_delete_invalid: bool = False
    webhook_url: str = ""
    notify_on_complete: bool = False
    notify_on_pool_low: bool = False
    pool_low_threshold: int = Field(3, ge=0, le=10000)


class CliproxyQuotaUpdate(BaseModel):
    enabled: bool = True
    interval_seconds: int = Field(60, ge=30, le=3600)


class BackupRestoreRequest(BaseModel):
    name: str


def _log_stream_delta(previous: list[str], current: list[str]) -> list[str]:
    """Return newly appended log lines even after the bounded deque rotates.

    JOB.logs is a maxlen deque. Once it is full, ``len(current)`` stops
    increasing, so a plain last-length cursor misses every future line.  Logs
    are append-only, therefore the previous suffix overlaps the current prefix
    after rotation.
    """
    if not previous:
        return list(current)
    if not current:
        return []
    max_overlap = min(len(previous), len(current))
    for overlap in range(max_overlap, 0, -1):
        if previous[-overlap:] == current[:overlap]:
            return current[overlap:]
    if previous == current:
        return []
    return list(current)


def _account_log_event_identity(item: dict[str, Any]) -> str:
    """Use one stable identity when trace rows omit account_id (or email)."""
    email = str(item.get("email") or "").strip().lower()
    if email:
        return f"email:{email}"
    account_id = str(item.get("account_id") or "").strip().lower()
    if account_id:
        return f"account_id:{account_id}"
    account_key = str(item.get("account_key") or "").strip().lower()
    return account_key if account_key else ""


def _account_log_event_key(item: dict[str, Any]) -> tuple[Any, ...]:
    stamp = str(item.get("created_at") or item.get("at") or "").strip()
    try:
        bucket: Any = int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp())
    except Exception:
        bucket = stamp

    def _norm(value: Any) -> str:
        return " ".join(str(value or "").split()).casefold()

    return (
        _account_log_event_identity(item),
        _norm(item.get("operation")),
        _norm(item.get("status")),
        _norm(item.get("message")),
        _norm(item.get("step")),
        _norm(item.get("error")),
        bucket,
    )


def _merge_account_log_events(
    items: list[dict[str, Any]],
    trace_items: list[dict[str, Any]],
    *,
    limit: int,
    newest_first: bool,
) -> tuple[list[dict[str, Any]], int]:
    """Sanitize, de-duplicate and cap account log rows consistently.

    Registration traces and durable operation rows intentionally overlap. A
    one-second semantic key removes exact/replayed rows while keeping distinct
    steps, statuses, and later operations. ``total`` describes the returned
    page, so it cannot disagree with ``items`` after the cap is applied.
    """
    cap = max(1, min(int(limit or 200), 1000))
    merged: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    trace_seen: set[tuple[Any, ...]] = set()
    for raw in [*items, *trace_items]:
        event = _state().redact_operation_event(raw)
        if not event:
            continue
        if event.get("source") == "registration_trace":
            # A trace writer emits a human log row and one or more structured
            # event rows for the same step.  They differ in status/message vs
            # step, so the general key below cannot collapse them. Normalize
            # the task prefix and use one semantic marker for trace rows only.
            stamp = str(event.get("created_at") or event.get("at") or "").strip()
            try:
                bucket: Any = int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp())
            except Exception:
                bucket = stamp
            text = str(event.get("message") or event.get("step") or event.get("error") or "")
            text = re.sub(r"^\s*\[[^\]]+\]\s*", "", text)
            text = " ".join(text.split()).casefold()
            trace_marker = (
                _account_log_event_identity(event),
                str(event.get("operation") or "").casefold(),
                bucket,
                text,
            )
            if text and trace_marker in trace_seen:
                continue
            if text:
                trace_seen.add(trace_marker)
        marker = _account_log_event_key(event)
        if marker in seen:
            continue
        seen.add(marker)
        merged.append(event)
    merged.sort(
        key=lambda item: str(item.get("created_at") or item.get("at") or ""),
        reverse=newest_first,
    )
    page = merged[:cap]
    return page, len(page)


def _account_log_file_stamp(path: Any) -> tuple[str, int, int]:
    """Return a cheap content revision for SQLite/trace cache invalidation."""
    try:
        target = Path(path)
        if not target.is_absolute():
            target = _state().ROOT / target
        target = target.resolve()
        stat = target.stat()
        return str(target), int(stat.st_mtime_ns), int(stat.st_size)
    except (OSError, TypeError, ValueError):
        return str(path or ""), 0, 0


def _account_log_store_stamp() -> tuple[tuple[str, int, int], ...]:
    db_path = Path(_state().OPERATION_LOG_DB_FILE)
    account_db = Path(_state().ACCOUNTS_DB_FILE)
    return tuple(
        _account_log_file_stamp(path)
        for path in (
            db_path,
            Path(f"{db_path}-wal"),
            account_db,
            Path(f"{account_db}-wal"),
        )
    )


def _account_log_trace_stamp(
    payload: dict[str, Any],
    trace_files: Any = (),
) -> tuple[tuple[str, int, int], ...]:
    files = {str(path or "").strip() for path in (trace_files or ()) if str(path or "").strip()}
    # Keep this fallback for callers/tests that only provide the final payload.
    for event in payload.get("items") or []:
        if not isinstance(event, dict):
            continue
        metadata = event.get("metadata")
        if isinstance(metadata, dict):
            trace_file = str(metadata.get("trace_file") or "").strip()
            if trace_file:
                files.add(trace_file)
    return tuple(
        sorted((_account_log_file_stamp(path) for path in files), key=lambda item: item[0])
    )


def _account_log_cache_get(cache_key: tuple[Any, ...]) -> dict[str, Any] | None:
    """Return an isolated fresh snapshot; callers may safely mutate it."""
    now = time.monotonic()
    current_store_stamp = _state()._account_log_store_stamp()
    with _ACCOUNT_LOG_CACHE_COND:
        cached = _ACCOUNT_LOG_CACHE.get(cache_key)
        if cached is None or now - cached[0] >= _ACCOUNT_LOG_CACHE_TTL:
            return None
        _created_at, payload, source_stamp = cached
        if source_stamp[: len(current_store_stamp)] != current_store_stamp:
            _ACCOUNT_LOG_CACHE.pop(cache_key, None)
            return None
        trace_stamp = source_stamp[len(current_store_stamp) :]
        for stamp in trace_stamp:
            if not stamp or _account_log_file_stamp(stamp[0]) != stamp:
                _ACCOUNT_LOG_CACHE.pop(cache_key, None)
                return None
        return copy.deepcopy(payload)


def _account_log_cache_store(
    cache_key: tuple[Any, ...],
    payload: dict[str, Any],
    trace_files: Any = (),
) -> dict[str, Any]:
    stamp = (*_state()._account_log_store_stamp(), *_account_log_trace_stamp(payload, trace_files))
    now = time.monotonic()
    with _ACCOUNT_LOG_CACHE_COND:
        _ACCOUNT_LOG_CACHE[cache_key] = (now, copy.deepcopy(payload), stamp)
        expired = [
            key
            for key, (created_at, _payload, _stamp) in _ACCOUNT_LOG_CACHE.items()
            if now - created_at >= _ACCOUNT_LOG_CACHE_TTL
        ]
        for key in expired:
            _ACCOUNT_LOG_CACHE.pop(key, None)
        while len(_ACCOUNT_LOG_CACHE) > _ACCOUNT_LOG_CACHE_MAX:
            oldest = min(_ACCOUNT_LOG_CACHE, key=lambda key: _ACCOUNT_LOG_CACHE[key][0])
            _ACCOUNT_LOG_CACHE.pop(oldest, None)
        _ACCOUNT_LOG_CACHE_INFLIGHT.discard(cache_key)
        _ACCOUNT_LOG_CACHE_INFLIGHT_AT.pop(cache_key, None)
        _ACCOUNT_LOG_CACHE_COND.notify_all()
    return copy.deepcopy(payload)


def _account_log_cache_begin(cache_key: tuple[Any, ...]) -> dict[str, Any] | None:
    """Coalesce concurrent misses for the same account log page."""
    with _ACCOUNT_LOG_CACHE_COND:
        while cache_key in _ACCOUNT_LOG_CACHE_INFLIGHT:
            started = _ACCOUNT_LOG_CACHE_INFLIGHT_AT.get(cache_key, time.monotonic())
            wait_limit = max(10.0, _ACCOUNT_LOG_CACHE_TTL * 5.0)
            _ACCOUNT_LOG_CACHE_COND.wait(timeout=min(1.0, wait_limit))
            cached = _account_log_cache_get(cache_key)
            if cached is not None:
                return cached
            # A worker that failed before publishing can leave an in-flight
            # marker behind. Reclaim only after a generous bound; ordinary
            # slow trace reads remain single-flight instead of duplicating IO.
            if time.monotonic() - started >= wait_limit:
                _ACCOUNT_LOG_CACHE_INFLIGHT.discard(cache_key)
                _ACCOUNT_LOG_CACHE_INFLIGHT_AT.pop(cache_key, None)
                break
        cached = _account_log_cache_get(cache_key)
        if cached is not None:
            return cached
        _ACCOUNT_LOG_CACHE_INFLIGHT.add(cache_key)
        _ACCOUNT_LOG_CACHE_INFLIGHT_AT[cache_key] = time.monotonic()
        return None


def _account_log_cache_abort(cache_key: tuple[Any, ...]) -> None:
    with _ACCOUNT_LOG_CACHE_COND:
        _ACCOUNT_LOG_CACHE_INFLIGHT.discard(cache_key)
        _ACCOUNT_LOG_CACHE_INFLIGHT_AT.pop(cache_key, None)
        _ACCOUNT_LOG_CACHE_COND.notify_all()


@router.get("/api/accounts/logs/{delete_key:path}")
async def account_operation_logs(
    delete_key: str,
    limit: int = 200,
    order: str = "desc",
):
    """Return durable, redacted operation history for one account.

    ``delete_key`` is accepted as a path value because the panel already uses
    the same stable ``email:...``/``account_id:...`` identity in row actions.
    The log store resolves historical aliases, so an email key remains useful
    after a later token response adds an account id or the account row is gone.
    """
    key = str(delete_key or "").strip()
    if not key or len(key) > 512:
        raise HTTPException(400, "账号标识无效")
    direction = str(order or "desc").strip().lower()
    if direction not in {"asc", "desc"}:
        raise HTTPException(400, "日志排序只支持 asc 或 desc")
    try:
        cap = max(1, min(int(limit or 200), 1000))
    except (TypeError, ValueError) as error:
        raise HTTPException(400, "日志条数参数无效") from error
    # Include the durable store revision in the key so a write naturally
    # bypasses old pages before the short TTL expires. Trace files are checked
    # again against the cached source stamp because they are discovered after
    # the first SQLite read.
    cache_key = (key.casefold(), cap, direction, _state()._account_log_store_stamp())
    cached = await asyncio.to_thread(_account_log_cache_begin, cache_key)
    if cached is not None:
        cached["account_key"] = key
        return cached
    try:
        items, total = await asyncio.to_thread(
            _state().list_operation_events,
            key,
            limit=cap,
            newest_first=direction != "asc",
        )
    except (TypeError, ValueError) as error:
        await asyncio.to_thread(_account_log_cache_abort, cache_key)
        raise HTTPException(400, "日志条数参数无效") from error
    except Exception:
        await asyncio.to_thread(_account_log_cache_abort, cache_key)
        raise
    # Protocol registration already has durable JSONL traces. Merge those
    # records into the same account dialog so old registrations are visible
    # alongside newer maintenance events. A row can carry one trace path, but
    # a deleted account may leave several historical paths in its event data.
    trace_files: set[str] = set()
    record: dict[str, Any] | None = None
    try:
        matched = await asyncio.to_thread(_state()._records_by_keys_with_fallback, [key])
        record = matched.get(key)
        if record is None:
            # account ids and email aliases may differ in case/prefix; use the
            # first identity match returned by the keyed store.
            record = next(iter(matched.values()), None)
        record_trace = str((record or {}).get("registration_trace_file") or "").strip()
        if record_trace:
            trace_files.add(record_trace)
    except Exception:
        pass
    # Deleted accounts no longer have a row to resolve, but durable
    # registration events retain the trace path in sanitized metadata.
    for event in items:
        metadata = event.get("metadata") if isinstance(event, dict) else {}
        if isinstance(metadata, dict):
            trace_file = str(metadata.get("trace_file") or "").strip()
            if trace_file:
                trace_files.add(trace_file)

    try:
        trace_groups = await asyncio.gather(
            *(
                asyncio.to_thread(
                    _state().list_registration_trace_events,
                    trace_file,
                    limit=cap,
                    newest_first=direction != "asc",
                )
                for trace_file in sorted(trace_files)
            )
        )
    except Exception:
        await asyncio.to_thread(_account_log_cache_abort, cache_key)
        raise
    trace_items = [item for group, _trace_total in trace_groups for item in group]
    # Early registration trace rows often precede email/account-id discovery.
    # Bind those rows to the requested account key so persistence/trace
    # duplicates can still be recognized without exposing new data.
    known_email = str((record or {}).get("email") or "").strip()
    known_account_id = str((record or {}).get("account_id") or "").strip()
    if not known_email and key.lower().startswith("email:"):
        known_email = key.split(":", 1)[1]
    if not known_account_id and key.lower().startswith("account_id:"):
        known_account_id = key.split(":", 1)[1]
    for trace_item in trace_items:
        if isinstance(trace_item, dict) and not (
            str(trace_item.get("email") or "").strip()
            or str(trace_item.get("account_id") or "").strip()
        ):
            if known_email:
                trace_item["email"] = known_email
            if known_account_id:
                trace_item["account_id"] = known_account_id
            if not known_email and not known_account_id and key:
                trace_item["account_key"] = key

    try:
        items, total = _merge_account_log_events(
            items,
            trace_items,
            limit=cap,
            newest_first=direction != "asc",
        )
        payload = {
            "ok": True,
            "account_key": key,
            "items": items,
            "total": total,
            "order": direction,
        }
        return await asyncio.to_thread(
            _account_log_cache_store,
            cache_key,
            payload,
            trace_files,
        )
    except Exception:
        await asyncio.to_thread(_account_log_cache_abort, cache_key)
        raise


@router.get("/api/logs")
async def logs(limit: int = 300, q: str = "", level: str = "all"):
    lines = _state().JOB.recent_logs(limit)
    needle = str(q or "").strip().lower()
    kind = str(level or "all").strip().lower()
    if needle:
        lines = [line for line in lines if needle in line.lower()]
    if kind == "error":
        lines = [line for line in lines if any(marker in line.lower() for marker in ("错误", "失败", "error", "timeout", "http 4", "http 5"))]
    elif kind == "success":
        lines = [line for line in lines if any(marker in line.lower() for marker in ("成功", "完成", "http 200"))]
    return {"lines": lines}


@router.get("/api/logs/download")
async def logs_download():
    payload = ("\n".join(_state().JOB.recent_logs(_state().MAX_LOG_LINES)) + "\n").encode("utf-8")
    return StreamingResponse(
        iter([payload]),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="gpt-register.log"'},
    )


@router.get("/api/tasks")
async def tasks(limit: int = 500):
    def _response():
        cap = max(1, min(int(limit or 500), 2000))
        cfg = _state()._read_config()
        snap = _state().JOB.snapshot()
        live_bucket = str(int(time.time() // 2)) if snap.get("running") else "idle"
        task_db = _state().DATA_DIR / "task_history.sqlite3"
        key = ":".join(
            (
                "tasks:v2",
                str(cap),
                _state()._cache_digest(json.dumps(snap, sort_keys=True, default=str)),
                live_bucket,
                str(_state()._file_stamp(task_db)),
                str(_state()._file_stamp(Path(f"{task_db}-wal"))),
            )
        )
        return _state()._cached_json_response(
            cfg,
            key,
            "tasks_ttl",
            60,
            lambda: {"items": _state().JOB.all_tasks_snapshot(limit=cap), "job": snap},
        )

    return await asyncio.to_thread(_response)


@router.post("/api/tasks/delete")
async def tasks_delete(body: TaskDeleteRequest):
    before = len(_state().JOB.all_tasks_snapshot())
    wanted = {str(value).strip() for value in body.indexes}
    with _state().JOB.lock:
        if body.delete_all:
            _state().JOB.task_cards.clear()
        else:
            for key, card in list(_state().JOB.task_cards.items()):
                card_id = str(card.get("card_id") or key)
                if card_id in wanted or str(key) in wanted:
                    _state().JOB.task_cards.pop(key, None)
        _state().JOB._task_revision += 1
        _state().JOB._recompute_counts_locked()
        if body.delete_all:
            _state().JOB.total = 0
    _state().delete_task_runtime(wanted, delete_all=body.delete_all)
    _state().JOB.persist_runtime()
    items = _state().JOB.all_tasks_snapshot(limit=500)
    return {"ok": True, "removed": max(0, before - len(items)), "items": items}


@router.get("/api/logs/stream")
async def logs_stream(request: Request, events: str = "status,log,tasks"):
    # Clients can subscribe only to the event classes needed by the active
    # view. Keep the historical all-events default for older clients.
    requested_events = {
        part.strip().lower()
        for part in str(events or "").split(",")
        if part.strip().lower() in {"status", "log", "tasks"}
    }
    if not requested_events:
        requested_events = {"status", "log", "tasks"}
    requested_events.add("status")

    async def event_gen():
        previous_lines: list[str] = []
        previous_status = ""
        previous_task_revision = -1
        last_heartbeat = 0.0
        idle_ticks = 0
        # Cap stream lifetime so abandoned tabs cannot pin connections forever
        max_ticks = 15 * 60  # ~15 min at 1s interval
        yield "retry: 1500\n\n"
        while idle_ticks < max_ticks and not _state()._SERVER_SHUTDOWN_EVENT.is_set():
            if await request.is_disconnected():
                break
            sent_any = False
            if "log" in requested_events:
                with _state().JOB.lock:
                    lines = list(_state().JOB.logs)
                chunk = _log_stream_delta(previous_lines, lines)
                previous_lines = lines
                if chunk:
                    payload = json.dumps({"lines": chunk}, ensure_ascii=False)
                    yield f"event: log\ndata: {payload}\n\n"
                    sent_any = True

            if "tasks" in requested_events:
                task_revision, task_items = _state().JOB.task_stream_snapshot()
                if task_revision != previous_task_revision:
                    payload = json.dumps(
                        {"revision": task_revision, "items": task_items},
                        ensure_ascii=False,
                    )
                    yield f"event: tasks\ndata: {payload}\n\n"
                    previous_task_revision = task_revision
                    sent_any = True

            if "status" in requested_events:
                snap = _state().JOB.snapshot()
                status_payload = json.dumps(snap, ensure_ascii=False, sort_keys=True)
                if status_payload != previous_status:
                    yield f"event: status\ndata: {status_payload}\n\n"
                    previous_status = status_payload
                    sent_any = True

            now = time.monotonic()
            if now - last_heartbeat >= 5.0:
                yield "event: heartbeat\ndata: {}\n\n"
                last_heartbeat = now
            # Only count genuinely idle ticks so active streams are not cut
            # off by the lifetime cap.
            if sent_any:
                idle_ticks = 0
            else:
                idle_ticks += 1
            await asyncio.sleep(1.0)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/api/operations")
async def operations():
    def _response():
        cfg = _state()._read_config()
        key = ":".join(
            (
                "operations:v2",
                str(_state()._file_stamp(_state().CONFIG_FILE)),
                str(_state().account_store_revision(_state().ACCOUNTS_FILE)),
                str(_state()._file_stamp(_state().DATA_DIR / "panel_operations.json")),
                str(_state()._file_stamp(_state().DATA_DIR / "account_trash.json")),
            )
        )

        def _build_payload() -> dict[str, Any]:
            settings = _state()._maintenance_config(cfg)
            webhook = settings.pop("webhook_url", "")
            settings["has_webhook"] = bool(webhook)
            settings["webhook_masked"] = _state()._mask_secret(webhook) if webhook else ""
            state = _state().load_ops_state()
            health_counts = _state().account_health_summary(_state().ACCOUNTS_FILE)
            account_total = health_counts["total"]
            return {
                "ok": True,
                "metrics": _state()._current_operations_metrics(),
                "mail": _state()._public_mail_stats(cfg),
                "accounts": {"total": account_total, **health_counts},
                "maintenance": settings,
                "cliproxy_quota": _state()._cliproxy_quota_config(cfg),
                "state": state,
                "backups": _state().list_backups(),
                "backup_schedule": _state()._backup_schedule_config(cfg),
                "account_backups": _state().list_account_backups(),
                "trash_count": len(_state().list_trash(limit=1000)),
            }

        _state().PANEL_RESPONSE_CACHE.configure(_state()._cache_settings(cfg))
        cached, source = _state().PANEL_RESPONSE_CACHE.get_or_set_json(
            key,
            _state()._cache_ttl(cfg, "operations_ttl", 5),
            _build_payload,
        )
        try:
            payload = json.loads(cached)
        except (TypeError, ValueError):
            payload = _build_payload()
        if not isinstance(payload, dict):
            payload = {}
        payload["metrics"] = _state()._current_operations_metrics()
        # Independent durable counters must advance while the broader ops
        # response is cached, just like the live worker metrics above.
        payload["mail_registration"] = mail_registration_summary()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        cache_status = _state().PANEL_RESPONSE_CACHE.status()
        return Response(
            content=encoded,
            media_type="application/json",
            headers={
                "X-Panel-Cache": source.upper(),
                "X-Panel-Cache-Backend": str(cache_status.get("backend") or "unknown"),
                "Cache-Control": "private, no-cache",
            },
        )

    return await asyncio.to_thread(_response)


@router.get("/api/operations/mail-registration-stats")
async def mail_registration_stats(provider: str = "smsbower_mail"):
    if provider not in ("smsbower_mail", "online_disposablemail"):
        raise HTTPException(400, "邮箱注册统计仅支持 SMSBower / OnlineDisposableMail 渠道")
    return await asyncio.to_thread(mail_registration_summary, provider)


class UiSettingsUpdate(BaseModel):
    health_workers: int = Field(default=10, ge=1, le=20)
    reauth_workers: int = Field(default=3, ge=1, le=20)
    trial_workers: int = Field(default=20, ge=1, le=20)
    refresh_at_workers: int = Field(default=10, ge=1, le=10)
    # 接码批量任务默认并发(保存的默认值,保存上限 64;单次任务并发不受此限制)
    codex_sms_workers: int = Field(default=2, ge=1, le=64)
    reauth_mode: str = Field(default="protocol")
    trial_channel: str = Field(default="v4")


@router.get("/api/ui-settings")
async def ui_settings_get():
    def _do():
        cfg = _state()._read_config()
        raw = cfg.get("ui_settings") if isinstance(cfg.get("ui_settings"), dict) else {}

        def _int(value, default):
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        return {
            "ok": True,
            "health_workers": max(1, min(_int(raw.get("health_workers"), 10), 20)),
            "reauth_workers": max(1, min(_int(raw.get("reauth_workers"), 3), 20)),
            "trial_workers": max(1, min(_int(raw.get("trial_workers"), 20), 20)),
            "trial_channel": "nerver" if str(raw.get("trial_channel") or "").strip().lower() == "nerver" else "v4",
            "refresh_at_workers": max(1, min(_int(raw.get("refresh_at_workers"), 10), 10)),
            "codex_sms_workers": max(1, min(_int(raw.get("codex_sms_workers"), 2), 64)),
            "reauth_mode": "browser" if str(raw.get("reauth_mode") or "").strip().lower() == "browser" else "protocol",
        }

    return await asyncio.to_thread(_do)


@router.put("/api/ui-settings")
async def ui_settings_update(body: UiSettingsUpdate):
    def _do():
        with _state()._edit_config() as cfg:
            current = cfg.setdefault("ui_settings", {})
            current.update(
                {
                    "health_workers": max(1, min(int(body.health_workers or 10), 20)),
                    "reauth_workers": max(1, min(int(body.reauth_workers or 3), 20)),
                    "trial_workers": max(1, min(int(body.trial_workers or 20), 20)),
                    "trial_channel": "nerver" if str(body.trial_channel or "").strip().lower() == "nerver" else "v4",
                    "refresh_at_workers": max(1, min(int(body.refresh_at_workers or 10), 10)),
                    "codex_sms_workers": max(1, min(int(body.codex_sms_workers or 2), 64)),
                    "reauth_mode": "browser" if str(body.reauth_mode or "").strip().lower() == "browser" else "protocol",
                }
            )
        return {"ok": True}

    return await asyncio.to_thread(_do)


@router.get("/api/cache/status")
async def cache_status():
    def _status():
        cfg = _state()._read_config()
        _state().PANEL_RESPONSE_CACHE.configure(_state()._cache_settings(cfg))
        return {"ok": True, "cache": _state().PANEL_RESPONSE_CACHE.status()}

    return await asyncio.to_thread(_status)


@router.put("/api/maintenance")
async def maintenance_update(body: MaintenanceUpdate):
    with _state()._edit_config() as cfg:
        current = cfg.setdefault("maintenance", {})
        payload = body.model_dump()
        webhook = str(payload.pop("webhook_url") or "").strip()
        current.update(payload)
        if webhook:
            current["webhook_url"] = webhook
    public = _state()._maintenance_config(cfg)
    saved_webhook = public.pop("webhook_url", "")
    public["has_webhook"] = bool(saved_webhook)
    public["webhook_masked"] = _state()._mask_secret(saved_webhook) if saved_webhook else ""
    return {"ok": True, "maintenance": public}


@router.put("/api/cliproxy-quota")
async def cliproxy_quota_update(body: CliproxyQuotaUpdate):
    with _state()._edit_config() as cfg:
        current = cfg.setdefault("cliproxy_quota", {})
        current.update(
            {
                "enabled": bool(body.enabled),
                "interval_seconds": max(30, min(int(body.interval_seconds or 60), 3600)),
            }
        )
    return {"ok": True, "cliproxy_quota": _state()._cliproxy_quota_config(cfg)}


@router.post("/api/maintenance/run")
async def maintenance_run():
    return await asyncio.to_thread(_state()._run_account_maintenance_sync, force=True)


@router.post("/api/maintenance/test-webhook")
async def maintenance_test_webhook(body: MaintenanceUpdate):
    cfg = _state()._read_config()
    saved = _state()._maintenance_config(cfg).get("webhook_url") or ""
    target = str(body.webhook_url or "").strip() or str(saved)
    ok, message = await asyncio.to_thread(
        _state().send_webhook,
        target,
        {"event": "webhook_test", "message": "GPT Register FP test", "time": _state()._now_iso()},
    )
    return {"ok": ok, "message": message}


class BackupScheduleUpdate(BaseModel):
    enabled: bool = False
    time: str = Field(default="04:00", max_length=8)
    retention_days: int = Field(default=7, ge=1, le=90)


@router.put("/api/backup-schedule")
async def backup_schedule_update(body: BackupScheduleUpdate):
    with _state()._edit_config() as cfg:
        current = cfg.setdefault("backup_schedule", {})
        current.update(
            {
                "enabled": bool(body.enabled),
                "time": str(body.time or "04:00"),
                "retention_days": max(1, min(int(body.retention_days or 7), 90)),
            }
        )
    return {"ok": True, "backup_schedule": _state()._backup_schedule_config(cfg)}


@router.get("/api/backups")
async def backups_list():
    return {"ok": True, "items": await asyncio.to_thread(_state().list_backups)}


@router.post("/api/backups")
async def backups_create():
    retention = _state()._backup_schedule_config(_state()._read_config())["retention_days"]
    _state().PROGRESS.start("backup_create", "创建数据备份", stage="压缩数据库与配置文件")
    try:
        item = await asyncio.to_thread(_state().create_backup, retention_days=retention)
    except Exception as exc:
        _state().PROGRESS.finish("backup_create", ok=False, message=str(exc))
        raise
    _state().PROGRESS.finish("backup_create", message=item["name"])
    return {"ok": True, "item": item}


@router.get("/api/backups/{name}")
async def backups_download(name: str):
    try:
        path = _state().backup_path(name)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(404, "备份不存在") from exc
    return FileResponse(path, filename=path.name, media_type="application/zip")


@router.post("/api/backups/restore")
async def backups_restore(body: BackupRestoreRequest):
    if _state().JOB.running:
        raise HTTPException(409, "任务运行时不能恢复备份")
    _state().PROGRESS.start("backup_restore", "恢复数据备份", stage="校验并覆盖数据文件")
    try:
        restored = await asyncio.to_thread(_state().restore_backup, body.name)
    except (ValueError, FileNotFoundError) as exc:
        _state().PROGRESS.finish("backup_restore", ok=False, message="备份不存在")
        raise HTTPException(404, "备份不存在") from exc
    _state()._WEB_CREDS_CACHE = None
    _state()._WEB_CREDS_CACHE_MTIME = None
    _state().PROGRESS.finish("backup_restore", message=f"已恢复 {len(restored)} 个文件")
    return {"ok": True, "restored": restored, "message": "备份已恢复，请刷新页面"}

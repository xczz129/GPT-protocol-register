# -*- coding: utf-8 -*-
"""
Local web panel for GPT fingerprint registration.

  python web_app.py
  python cli.py web --host 127.0.0.1 --port 8787
"""

from __future__ import annotations

import asyncio
import copy
import logging
import hashlib
import importlib.util
import io
import inspect
import json
import os
import re
import secrets
import sys
import string
import threading
import time
import traceback
import tempfile
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.gzip import GZipMiddleware

from common.account_groups import (
    create_group,
    delete_group,
    get_group,
    list_groups,
    rename_group,
)
from common.account_store import (
    ACCOUNTS_DB_FILE,
    account_email_set,
    account_group_counts,
    account_health_counts,
    account_health_summary,
    account_is_deactivated,
    account_is_usable,
    account_public_view,
    account_store_revision,
    assign_account_group,
    count_accounts,
    delete_account_records,
    load_account_record_by_identity,
    load_account_records_by_keys,
    load_account_public_views,
    load_accounts,
    normalize_accounts,
    normalized_health_status,
    rebind_account_record,
    reassign_account_group,
    update_account_records,
    upsert_account_record,
    upsert_account_records,
)
from common.account_operation_log import (
    OPERATION_LOG_DB_FILE,
    list_operation_events,
    list_registration_trace_events,
    redact_operation_event,
    record_operation_event,
)
from common.redis_cache import PANEL_RESPONSE_CACHE
from common.cdk_store import (
    cdk_summary,
    create_cdks,
    ensure_plain_codes,
    get_cdk,
    list_cdks,
    lookup_cdk,
    redeem_cdk,
    replace_available_bindings,
    set_cdk_status,
)
from common.account_health import (
    InvalidAccessTokenError,
    select_health_check_records,
)
from common.trial_eligibility import (
    TrialEligibilityError,
    account_access_token as _stored_access_token,
    check_trial_eligibility,
    is_trial_probe_retryable,
    schedule_unchecked_trial_checks,
)
from common.trial_eligibility_jobs import TRIAL_ELIGIBILITY_JOBS
from common.email_rebind_jobs import EmailRebindJobs
from common.trial_proxy_pool import (
    read_trial_proxy_config,
    selected_trial_proxy,
    selected_trial_proxies,
    write_trial_proxy_config,
)
from common.registration_proxy_pool import (
    read_registration_proxy_pools,
    write_registration_proxy_pools,
)
from common.web_auth import (
    sign_redeem_session as _sign_redeem_session_value,
    sign_session as _sign_session_value,
    verify_redeem_session as _verify_redeem_session_value,
    verify_session as _verify_session_value,
)
from common.panel_ops import (
    archive_accounts,
    backup_path,
    create_backup,
    delete_task_runtime,
    list_backups,
    list_trash,
    load_ops_state,
    load_task_runtime,
    probe_proxy,
    restore_backup,
    send_webhook,
    save_task_runtime,
    take_trash,
    task_metrics,
    update_ops_state,
)
from common.redaction import mask_secret as _mask_secret, merge_config_preserving_secrets as _merge_config_preserving_secrets, redact_config_for_api as _redact_config_for_api
from common.totp import totp_code
from config import ROOT as CONFIG_ROOT
from config_loader import DEFAULT_CONFIG, apply_fingerprint_env, load_config
from proxy_util import is_proxy_transport_error, resolve_proxy_pool
from web.routes.auth import (
    BatchRedeemRequest,
    LoginRequest,
    MailMessagesRequest,
    PasswordChangeRequest,
    RedeemMessageRequest,
    RedeemMessagesRequest,
    RedeemRequest,
    auth_change_password,
    auth_login,
    auth_logout,
    auth_revoke_sessions,
    auth_status,
    cdk_query,
    login_page,
    redeem_account_json,
    redeem_access_token,
    redeem_batch_exchange,
    redeem_exchange,
    redeem_logout,
    redeem_message,
    redeem_messages,
    redeem_page,
    redeem_session,
    auth_router,
)
from mail import (
    CANONICAL_PROVIDER_TYPES,
    DOMAIN_API_DEFAULT_BASE,
    DOMAIN_API_DEFAULT_CAPACITY,
    DOMAIN_API_DEFAULT_LOCAL_LENGTH,
    DomainApiProvider,
    FASTMAIL_API_DEFAULT_BASE,
    FASTMAIL_API_DEFAULT_CAPACITY,
    FastmailApiProvider,
    MADDY_WEBUI_DEFAULT_BASE,
    MADDY_WEBUI_DEFAULT_CAPACITY,
    MADDY_WEBUI_DEFAULT_BLOCKED_DOMAINS,
    MADDY_WEBUI_DEFAULT_LOCAL_LENGTH,
    MaddyWebuiProvider,
    ICloudApiProvider,
    OutlookTokenProvider,
    _entry_available as _mailbox_entry_available,
    _extract_code,
    _known_mailbox_bindings,
    _make_config,
    base_mailbox_address,
    count_pool_capacity,
    create_mailbox,
    reconcile_registered_mailbox_states,
    normalize_provider_type,
    parse_domain_mailboxes,
    parse_icloud_credentials,
    parse_outlook_credentials,
    release_failed_mailboxes,
    release_mailbox,
    mark_mailbox_result,
    release_stale_in_use,
    update_mailbox_states,
)
from register.rebind import RebindError, run_rebind
from register.protocol_runtime import ProtocolRuntimeSettings

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None  # type: ignore


logger = logging.getLogger(__name__)

WEB_DIR = ROOT / "web"
STATIC_DIR = WEB_DIR / "static"
DATA_DIR = CONFIG_ROOT / "data"
ACCOUNTS_FILE = DATA_DIR / "registered_accounts.json"
GROUPS_FILE = DATA_DIR / "account_groups.json"
CDKS_FILE = DATA_DIR / "cdks.json"
STATE_FILE = DATA_DIR / "outlook_token_state.json"
CONFIG_FILE = ROOT / "config.yaml"
MAX_LOG_LINES = 3000
SUPPORTED_REGISTRATION_ENGINES = frozenset({"browser", "hybrid", "protocol"})

_CONFIG_CACHE_LOCK = threading.Lock()
_CONFIG_CACHE_COND = threading.Condition(_CONFIG_CACHE_LOCK)
_CONFIG_CACHE_KEY: tuple[str, int, int] | None = None
_CONFIG_CACHE_VALUE: dict[str, Any] | None = None
_CONFIG_LOAD_INFLIGHT: set[tuple[str, int, int]] = set()
_CONFIG_CACHE_GENERATION = 0
_CONFIG_MUTATION_LOCK = threading.RLock()
_ICLOUD_INBOX_SYNC_LOCK = threading.Lock()
_ICLOUD_INBOX_SYNC_AT = 0.0
_ICLOUD_INBOX_SYNC_INTERVAL = 5.0
_TRIAL_BACKFILL_LOCK = threading.Lock()
_TRIAL_BACKFILL_RUNNING = False
_ACCOUNTS_API_CACHE_LOCK = threading.Lock()
_ACCOUNTS_API_CACHE: dict[tuple[int, int, int, int, int], tuple[float, dict[str, Any]]] = {}
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
_OPERATIONS_METRICS_LOCK = threading.Lock()
_OPERATIONS_METRICS_REVISION = -1
_OPERATIONS_METRICS_AT = 0.0
_OPERATIONS_METRICS_VALUE: dict[str, Any] | None = None
_MAIL_STATS_CACHE_LOCK = threading.Lock()
_MAIL_STATS_BUILD_LOCK = threading.Lock()
_MAIL_STATS_CACHE_KEY: tuple[Any, ...] | None = None
_MAIL_STATS_CACHE_AT = 0.0
_MAIL_STATS_CACHE_VALUE: dict[str, Any] | None = None
# Domain mailbox listing is an external read used by the mailbox panel. Keep
# the last successful snapshot briefly so opening the panel does not block on
# the provider for every request; per-key locks coalesce concurrent refreshes.
_DOMAIN_MAILBOX_REMOTE_CACHE_LOCK = threading.RLock()
_DOMAIN_MAILBOX_REMOTE_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_DOMAIN_MAILBOX_REMOTE_FETCH_LOCKS: dict[str, threading.Lock] = {}
_DOMAIN_MAILBOX_REMOTE_CACHE_TTL = 30.0
_PANEL_CACHE_WARM_TASK: asyncio.Task | None = None
_SERVER_SHUTDOWN_EVENT = threading.Event()

# Email-rebind reservations are intentionally short-lived.  The mailbox
# provider marks each candidate ``in_use`` in its shared state file; this
# process-local index binds that reservation to the source account and stops a
# second browser tab from starting it twice.  Stale provider reservations are
# reclaimed by the existing mailbox maintenance path after its normal timeout.
_REBIND_RESERVATION_LOCK = threading.RLock()
_REBIND_RESERVATIONS: dict[str, dict[str, Any]] = {}
_REBIND_RESERVATION_TTL = 10 * 60.0
ACCOUNT_REBIND_JOBS = EmailRebindJobs()


def _file_stamp(path: Path) -> int:
    try:
        return int(path.stat().st_mtime_ns)
    except OSError:
        return 0


def _account_data_stamp() -> str:
    db_path = Path(ACCOUNTS_DB_FILE)
    return ":".join(
        str(value)
        for value in (
            _file_stamp(db_path),
            _file_stamp(Path(f"{db_path}-wal")),
        )
    )


def _current_operations_metrics() -> dict[str, Any]:
    """Refresh task metrics only when the in-memory task revision changes."""
    global _OPERATIONS_METRICS_REVISION, _OPERATIONS_METRICS_AT, _OPERATIONS_METRICS_VALUE
    revision = int(JOB.snapshot().get("task_revision") or 0)
    now = time.monotonic()
    with _OPERATIONS_METRICS_LOCK:
        if (
            _OPERATIONS_METRICS_VALUE is not None
            and (
                _OPERATIONS_METRICS_REVISION == revision
                or now - _OPERATIONS_METRICS_AT < 2.0
            )
        ):
            return dict(_OPERATIONS_METRICS_VALUE)
    metrics = task_metrics(JOB.all_tasks_snapshot(limit=2000))
    with _OPERATIONS_METRICS_LOCK:
        _OPERATIONS_METRICS_REVISION = revision
        _OPERATIONS_METRICS_AT = time.monotonic()
        _OPERATIONS_METRICS_VALUE = dict(metrics)
    return metrics


def _cache_settings(cfg: dict[str, Any]) -> dict[str, Any]:
    value = cfg.get("cache") if isinstance(cfg.get("cache"), dict) else {}
    return dict(value) if isinstance(value, dict) else {}


def _cache_ttl(cfg: dict[str, Any], name: str, default: int) -> int:
    settings = _cache_settings(cfg)
    try:
        return max(1, min(3600, int(settings.get(name) or default)))
    except (TypeError, ValueError):
        return default


def _cache_digest(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def _cached_json_response(
    cfg: dict[str, Any],
    key: str,
    ttl_name: str,
    default_ttl: int,
    builder,
) -> Response:
    PANEL_RESPONSE_CACHE.configure(_cache_settings(cfg))
    payload, source = PANEL_RESPONSE_CACHE.get_or_set_json(
        key,
        _cache_ttl(cfg, ttl_name, default_ttl),
        builder,
    )
    cache_status = PANEL_RESPONSE_CACHE.status()
    return Response(
        content=payload,
        media_type="application/json",
        headers={
            "X-Panel-Cache": source.upper(),
            "X-Panel-Cache-Backend": str(cache_status.get("backend") or "unknown"),
            "Cache-Control": "private, no-cache",
        },
    )


def _io_worker_count() -> int:
    """Choose a small I/O pool for a low-core panel process."""
    return max(4, min(8, (os.cpu_count() or 2) * 3))


def _reauth_job_running() -> bool:
    manager = globals().get("ACCOUNT_REAUTH_JOBS")
    if manager is None:
        return False
    try:
        snapshot = manager.active_snapshot()
    except Exception:
        return False
    return bool(snapshot and snapshot.get("status") in {"queued", "running", "stopping"})


def _effective_registration_workers(
    engine: str,
    requested: int,
    total: int,
) -> tuple[int, str]:
    """Run the requested number of tasks; only the mailbox count is a real cap."""
    requested = max(1, int(requested or 1))
    total = max(1, int(total or 1))
    return max(1, min(requested, total)), "independent"


def _queue_trial_eligibility_backfill(
    records: list[dict[str, Any]],
    *,
    force: bool = False,
) -> int:
    """Run account eligibility queueing away from the list-request path."""
    global _TRIAL_BACKFILL_RUNNING
    job = globals().get("JOB")
    if job is not None:
        with job.lock:
            if job.running:
                return 0
    health_lock = globals().get("_ACCOUNT_HEALTH_PROGRESS_LOCK")
    health_progress = globals().get("_ACCOUNT_HEALTH_PROGRESS")
    if health_lock is not None and isinstance(health_progress, dict):
        with health_lock:
            if health_progress.get("running"):
                return 0
    with _TRIAL_BACKFILL_LOCK:
        if _TRIAL_BACKFILL_RUNNING:
            return 0
        _TRIAL_BACKFILL_RUNNING = True

    snapshot = [dict(item) for item in records if isinstance(item, dict)]

    def worker() -> None:
        global _TRIAL_BACKFILL_RUNNING
        try:
            schedule_unchecked_trial_checks(snapshot, force=force, path=ACCOUNTS_FILE)
        finally:
            with _TRIAL_BACKFILL_LOCK:
                _TRIAL_BACKFILL_RUNNING = False

    threading.Thread(target=worker, daemon=True, name="trial-eligibility-backfill").start()
    return len(snapshot)


def _is_expected_browser_navigation_error(context: dict[str, Any]) -> bool:
    """Classify Playwright futures invalidated by an in-progress navigation."""
    exc = context.get("exception")
    message = " ".join(
        part for part in (str(exc or ""), str(context.get("message") or "")) if part
    ).lower()
    return any(
        marker in message
        for marker in (
            "targetclosederror",
            "target page, context or browser has been closed",
            "execution context was destroyed",
            "cannot find context with specified id",
            "inspected target navigated or closed",
            "waiting for navigation to finish",
            "event loop is closed",
            "base subprocess transport",
            "exception ignored in",
        )
    )


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
        self.logs: deque[str] = deque(maxlen=MAX_LOG_LINES)
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
        saved = load_task_runtime()
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
        ts = datetime.now().strftime("%H:%M:%S")
        text = str(line).rstrip()
        if not text:
            return
        with self.lock:
            self.logs.append(f"[{ts}] {text}")

    def recent_logs(self, limit: int = 300) -> list[str]:
        with self.lock:
            items = list(self.logs)
        return items[-max(1, min(limit, MAX_LOG_LINES)) :]

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
        saved = load_task_runtime(limit=limit)
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
            save_task_runtime(self.tasks_snapshot(), self.snapshot())


_TEST_RUNTIME = "unittest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))
JOB = JobState(restore=not _TEST_RUNTIME, persist=not _TEST_RUNTIME)


class OperationProgressState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.items: dict[str, dict[str, Any]] = {}

    def start(self, key: str, label: str, *, total: int = 0, stage: str = "准备中") -> None:
        now = time.time()
        with self.lock:
            self.items[key] = {
                "key": key,
                "label": label,
                "status": "running",
                "stage": stage,
                "done": 0,
                "total": max(0, int(total or 0)),
                "percent": 0,
                "message": "",
                "started_at": _now_iso(),
                "started_epoch": now,
                "updated_epoch": now,
            }

    def update(
        self,
        key: str,
        *,
        stage: str | None = None,
        done: int | None = None,
        total: int | None = None,
        percent: float | None = None,
        message: str | None = None,
    ) -> None:
        with self.lock:
            item = self.items.get(key)
            if not item:
                return
            if stage is not None:
                item["stage"] = str(stage)
            if done is not None:
                item["done"] = max(0, int(done))
            if total is not None:
                item["total"] = max(0, int(total))
            if percent is None and int(item.get("total") or 0) > 0:
                percent = int(item.get("done") or 0) * 100 / int(item["total"])
            if percent is not None:
                item["percent"] = round(max(0.0, min(float(percent), 99.0)), 1)
            if message is not None:
                item["message"] = str(message)[:240]
            item["updated_epoch"] = time.time()

    def finish(self, key: str, *, ok: bool = True, message: str = "") -> None:
        with self.lock:
            item = self.items.get(key)
            if not item:
                return
            item["status"] = "success" if ok else "failed"
            item["stage"] = "已完成" if ok else "失败"
            item["percent"] = 100
            item["message"] = str(message)[:240]
            item["finished_at"] = _now_iso()
            item["updated_epoch"] = time.time()

    def snapshot(self) -> list[dict[str, Any]]:
        now = time.time()
        with self.lock:
            self.items = {
                key: value
                for key, value in self.items.items()
                if value.get("status") == "running" or now - float(value.get("updated_epoch") or now) < 900
            }
            rows = [dict(value) for value in self.items.values()]
        for row in rows:
            started = float(row.pop("started_epoch", 0) or 0)
            row.pop("updated_epoch", None)
            row["elapsed_sec"] = round(max(0.0, now - started), 1) if started else 0
        return sorted(rows, key=lambda item: str(item.get("started_at") or ""), reverse=True)


PROGRESS = OperationProgressState()


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


class ConfigUpdate(BaseModel):
    config: dict[str, Any]


class MailboxesUpdate(BaseModel):
    mailboxes: str = ""
    append: bool = True
    mode: str | None = None
    provider_type: str | None = None
    api_base: str | None = None
    api_key: str | None = None
    api_token: str | None = None
    admin_key: str | None = None
    accounts: str | None = None
    username: str | None = None
    password: str | None = None
    default_domain: str | None = None
    domains: list[str] | None = None
    domain_index: int | None = None
    local_length: int | None = None
    capacity: int | None = None
    enable: bool | None = None
    label: str | None = None
    mailbox_password: str | None = None


class DomainMailDomainsRequest(BaseModel):
    api_base: str | None = None
    username: str | None = None
    password: str | None = None


class MaddyWebuiDomainsRequest(BaseModel):
    api_base: str | None = None
    api_key: str | None = None
    api_token: str | None = None


class FastmailAccountsRequest(BaseModel):
    api_base: str | None = None
    admin_key: str | None = None


class ProxyUpdate(BaseModel):
    enabled: bool = False
    mode: str = "single_proxy"
    url: str = ""
    resource_url: str = ""
    pool_file: str = ""
    pool_text: str | None = None
    selected_country: str = ""
    country_pools: list[dict[str, Any]] | None = None
    rotate_per_task: bool = True
    clearance: dict[str, Any] | None = None


class ChatGPT2APIUpdate(BaseModel):
    enabled: bool = False
    url: str = ""
    key: str = ""
    auto_upload: bool = True


class ChatGPT2APITestRequest(BaseModel):
    url: str | None = None
    key: str | None = None


class ChatGPT2APIUploadRequest(ChatGPT2APITestRequest):
    group_id: str | None = None


class CDKGenerateRequest(BaseModel):
    group_id: str = ""
    count: int = Field(1, ge=1, le=500)
    expires_days: int = Field(0, ge=0, le=3650)
    account_key: str = Field(default="", max_length=256)


class CDKStatusRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)
    status: str


from web.routes.shared_models import AccountTokenRequest  # noqa: E402


class AccountCredentialsRequest(AccountTokenRequest):
    field: str = Field(default="totp_code", min_length=1, max_length=32)


class BulkDeleteRequest(BaseModel):
    keys: list[str] = Field(default_factory=list)
    delete_all: bool = False


class AccountEmailExportRequest(BaseModel):
    """Email filters used by the credential export workspace."""

    emails: list[str] = Field(default_factory=list, max_length=5000)
    text: str = Field(default="", max_length=2_000_000)


class AccountHealthRequest(BulkDeleteRequest):
    workers: int = Field(10, ge=1, le=20)


class AccountTrialEligibilityRequest(BaseModel):
    keys: list[str] = Field(default_factory=list, max_length=1000)
    all_accounts: bool = False
    force: bool = False
    concurrency: int = Field(default=5, ge=1, le=5)


class TokenTrialEligibilityRequest(BaseModel):
    access_token: str = ""
    use_configured_proxy: bool = True


class TokenTrialEligibilityBatchRequest(BaseModel):
    access_tokens: list[str] = Field(default_factory=list)
    use_configured_proxy: bool = True
    workers: int = Field(default=4, ge=1, le=8)


class TrialProxyCountryItem(BaseModel):
    country: str = Field(min_length=2, max_length=2)
    label: str = Field(default="", max_length=40)
    text: str = Field(default="", max_length=2_000_000)


class TrialProxyConfigRequest(BaseModel):
    selected_country: str = Field(default="", max_length=2)
    countries: list[TrialProxyCountryItem] = Field(default_factory=list, max_length=100)


class GroupCreateRequest(BaseModel):
    name: str


class GroupUpdateRequest(BaseModel):
    name: str


class AccountGroupAssignRequest(BaseModel):
    keys: list[str] = Field(default_factory=list)
    delete_all: bool = False
    group_id: str


class TaskDeleteRequest(BaseModel):
    indexes: list[str | int] = Field(default_factory=list)
    delete_all: bool = False


class MailboxDeleteRequest(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)
    delete_all: bool = False
    provider_type: str | None = "auto"


class MailboxStateRequest(BaseModel):
    emails: list[str] = Field(default_factory=list)
    action: str = "release"
    states: list[str] = Field(default_factory=list)


class ProxyTestRequest(BaseModel):
    proxies: list[str] = Field(default_factory=list)


class MaintenanceUpdate(BaseModel):
    enabled: bool = False
    interval_hours: int = Field(12, ge=1, le=168)
    workers: int = Field(10, ge=1, le=20)
    auto_delete_invalid: bool = False
    webhook_url: str = ""
    notify_on_complete: bool = False
    notify_on_pool_low: bool = False
    pool_low_threshold: int = Field(3, ge=0, le=10000)


class MaddyAutoRegisterUpdate(BaseModel):
    enabled: bool = False
    interval_hours: int = Field(1, ge=1, le=168)
    per_domain: int = Field(20, ge=1, le=500)
    concurrency: int = Field(3, ge=1, le=20)
    engine: str = Field(default="protocol", min_length=1, max_length=16)
    keep_on_fail: bool = False
    refresh_domains: bool = True
    group_id: str = Field(default="", max_length=256)


class BackupRestoreRequest(BaseModel):
    name: str


class TrashRestoreRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ensure_config_file() -> Path:
    if CONFIG_FILE.is_file():
        return CONFIG_FILE
    example = ROOT / "config.example.yaml"
    if example.is_file():
        CONFIG_FILE.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    return CONFIG_FILE


def _config_file_key() -> tuple[Path | None, tuple[str, int, int]]:
    path = CONFIG_FILE if CONFIG_FILE.is_file() else None
    try:
        stat = path.stat() if path is not None else None
        key = (
            str(path.resolve()),
            int(stat.st_mtime_ns),
            int(stat.st_size),
        ) if path is not None and stat is not None else ("", 0, 0)
    except OSError:
        key = (str(path or ""), 0, 0)
    return path, key


def _read_config() -> dict[str, Any]:
    global _CONFIG_CACHE_KEY, _CONFIG_CACHE_VALUE
    path, key = _config_file_key()
    # Coalesce cold reads for the same file version.  Parsing YAML is the
    # expensive part of this path, and several panel requests commonly arrive
    # together after startup or a config write.
    while True:
        with _CONFIG_CACHE_COND:
            if _CONFIG_CACHE_KEY == key and _CONFIG_CACHE_VALUE is not None:
                return copy.deepcopy(_CONFIG_CACHE_VALUE)
            if key not in _CONFIG_LOAD_INFLIGHT:
                _CONFIG_LOAD_INFLIGHT.add(key)
                load_generation = _CONFIG_CACHE_GENERATION
                break
            _CONFIG_CACHE_COND.wait()
            path, key = _config_file_key()

    try:
        loaded = load_config(path)
    except BaseException:
        with _CONFIG_CACHE_COND:
            _CONFIG_LOAD_INFLIGHT.discard(key)
            _CONFIG_CACHE_COND.notify_all()
        raise

    with _CONFIG_CACHE_COND:
        # A write or another file replacement may have happened while parsing.
        # Only publish a snapshot if this stamp is still current; otherwise a
        # later read must parse the newer file instead of seeing stale data.
        _, current_key = _config_file_key()
        if current_key == key and load_generation == _CONFIG_CACHE_GENERATION:
            _CONFIG_CACHE_KEY = key
            _CONFIG_CACHE_VALUE = copy.deepcopy(loaded)
        _CONFIG_LOAD_INFLIGHT.discard(key)
        _CONFIG_CACHE_COND.notify_all()
    return copy.deepcopy(loaded)


def _write_config(cfg: dict[str, Any]) -> None:
    global _WEB_CREDS_CACHE, _WEB_CREDS_CACHE_MTIME
    global _CONFIG_CACHE_KEY, _CONFIG_CACHE_VALUE, _CONFIG_CACHE_GENERATION
    with _CONFIG_MUTATION_LOCK:
        data = {k: v for k, v in cfg.items() if not str(k).startswith("_")}
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if yaml is not None:
            c_safe_dumper = getattr(yaml, "CSafeDumper", None)
            if c_safe_dumper is not None:
                text = yaml.dump(
                    data,
                    Dumper=c_safe_dumper,
                    allow_unicode=True,
                    sort_keys=False,
                )
            else:
                text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        else:
            text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{CONFIG_FILE.name}.",
            suffix=".tmp",
            dir=str(CONFIG_FILE.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
                try:
                    os.fchmod(handle.fileno(), 0o600)
                except OSError:
                    pass
            Path(tmp_name).replace(CONFIG_FILE)
        finally:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass
        with _CONFIG_CACHE_COND:
            _CONFIG_CACHE_KEY = None
            _CONFIG_CACHE_VALUE = None
            _CONFIG_CACHE_GENERATION += 1
            _CONFIG_CACHE_COND.notify_all()
        _WEB_CREDS_CACHE = None
        _WEB_CREDS_CACHE_MTIME = None
        try:
            apply_fingerprint_env(cfg)
        except Exception:
            pass
        fp = cfg.get("fingerprint") if isinstance(cfg.get("fingerprint"), dict) else {}
        try:
            browser = str((fp or {}).get("browser") or "local").strip().lower()
            os.environ["FINGERPRINT_BROWSER"] = browser
        except Exception:
            pass
        try:
            os.chmod(CONFIG_FILE, 0o600)
        except Exception:
            pass


@contextmanager
def _edit_config():
    """Serialize a complete config read-modify-write transaction."""
    with _CONFIG_MUTATION_LOCK:
        cfg = _read_config()
        yield cfg
        _write_config(cfg)


def _providers(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    providers = (
        cfg.get("mail", {}).get("providers")
        if isinstance(cfg.get("mail"), dict)
        else []
    )
    if not isinstance(providers, list):
        return []
    return [item for item in providers if isinstance(item, dict)]


def _find_provider(cfg: dict[str, Any], ptype: str) -> dict[str, Any] | None:
    want = normalize_provider_type(ptype)
    for item in _providers(cfg):
        if normalize_provider_type(str(item.get("type") or "")) == want:
            return item
    return None


def _ensure_provider(cfg: dict[str, Any], ptype: str) -> dict[str, Any]:
    want = normalize_provider_type(ptype)
    found = _find_provider(cfg, want)
    if found is not None:
        return found
    mail = cfg.setdefault("mail", {})
    providers = mail.setdefault("providers", [])
    if not isinstance(providers, list):
        providers = []
        mail["providers"] = providers
    if want == "icloud_api":
        entry = {
            "type": "icloud_api",
            "enable": True,
            "label": "iCloud Pool",
            "api_base": "https://icloud.xbovo.online",
            "mailboxes": "",
        }
    elif want == "domain_api":
        entry = {
            "type": "domain_api",
            "enable": True,
            "label": "域名邮箱",
            "api_base": DOMAIN_API_DEFAULT_BASE,
            "username": "",
            "password": "",
            "default_domain": "",
            "domain_index": 0,
            "local_length": DOMAIN_API_DEFAULT_LOCAL_LENGTH,
            "capacity": DOMAIN_API_DEFAULT_CAPACITY,
            "mailboxes": "",
        }
    elif want == "maddy_webui":
        entry = {
            "type": "maddy_webui",
            "enable": True,
            "label": "Maddy 邮箱",
            "api_base": MADDY_WEBUI_DEFAULT_BASE,
            "api_key": "",
            "default_domain": "unjome.com",
            "domains": ["unjome.com"],
            "blocked_domains": list(MADDY_WEBUI_DEFAULT_BLOCKED_DOMAINS),
            "local_length": MADDY_WEBUI_DEFAULT_LOCAL_LENGTH,
            "capacity": MADDY_WEBUI_DEFAULT_CAPACITY,
            "mailboxes": "",
        }
    elif want == "fastmail_api":
        entry = {
            "type": "fastmail_api",
            "enable": True,
            "label": "Fastmail Pool",
            "api_base": FASTMAIL_API_DEFAULT_BASE,
            "admin_key": "",
            "accounts": "",
            "capacity": FASTMAIL_API_DEFAULT_CAPACITY,
            "mailboxes": "",
        }
    else:
        entry = copy.deepcopy(DEFAULT_CONFIG["mail"]["providers"][0])
        entry["type"] = "outlook_token"
    providers.append(entry)
    return entry


def _pool_stats_for(pool: list[dict[str, str]], state: dict[str, Any]) -> dict[str, int]:
    available = used = failed = in_use = invalid = cooldown = 0
    for item in pool:
        email = item["email"].strip().lower()
        entry = state.get(email) if isinstance(state.get(email), dict) else None
        st = str((entry or {}).get("state") or "").strip().lower()
        if st in {"used"}:
            used += 1
        elif st in {"failed"}:
            failed += 1
        elif st in {"token_invalid"}:
            invalid += 1
        elif st in {"in_use"}:
            in_use += 1
        elif st == "cooldown":
            if _mailbox_entry_available(entry):
                available += 1
            else:
                cooldown += 1
        else:
            available += 1
    return {
        "total": len(pool),
        "available": available,
        "used": used,
        "failed": failed,
        "in_use": in_use,
        "token_invalid": invalid,
        "cooldown": cooldown,
    }


def _load_state_file() -> dict[str, Any]:
    if not STATE_FILE.is_file():
        return {}
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _mail_stats(cfg: dict[str, Any]) -> dict[str, Any]:
    mail_cfg = cfg.get("mail") if isinstance(cfg.get("mail"), dict) else {}
    capacity = count_pool_capacity(mail_cfg)
    state = _load_state_file()
    providers_out: list[dict[str, Any]] = []
    totals = {
        "total": 0,
        "available": 0,
        "used": 0,
        "failed": 0,
        "in_use": 0,
        "token_invalid": 0,
        "cooldown": 0,
    }
    for item in _providers(cfg):
        ptype = normalize_provider_type(str(item.get("type") or ""))
        if ptype not in CANONICAL_PROVIDER_TYPES:
            continue
        lines = str(item.get("mailboxes") or "")
        if ptype == "icloud_api":
            pool = parse_icloud_credentials(lines)
            stats = _pool_stats_for(pool, state)
        elif ptype == "domain_api":
            try:
                dynamic_capacity = int(
                    item.get("capacity")
                    or item.get("auto_create_limit")
                    or DOMAIN_API_DEFAULT_CAPACITY
                )
            except (TypeError, ValueError):
                dynamic_capacity = DOMAIN_API_DEFAULT_CAPACITY
            dynamic_capacity = max(0, min(500, dynamic_capacity))
            pool = parse_domain_mailboxes(lines)
            stats = _pool_stats_for(pool, state) if pool else {
                "total": dynamic_capacity,
                "available": dynamic_capacity,
                "used": 0,
                "failed": 0,
                "in_use": 0,
                "token_invalid": 0,
                "cooldown": 0,
            }
        elif ptype == "maddy_webui":
            try:
                dynamic_capacity = int(
                    item.get("capacity")
                    or item.get("auto_create_limit")
                    or MADDY_WEBUI_DEFAULT_CAPACITY
                )
            except (TypeError, ValueError):
                dynamic_capacity = MADDY_WEBUI_DEFAULT_CAPACITY
            dynamic_capacity = max(0, min(500, dynamic_capacity))
            pool = parse_domain_mailboxes(lines)
            stats = _pool_stats_for(pool, state) if pool else {
                "total": dynamic_capacity,
                "available": dynamic_capacity,
                "used": 0,
                "failed": 0,
                "in_use": 0,
                "token_invalid": 0,
                "cooldown": 0,
            }
        elif ptype == "fastmail_api":
            # Remote alias pool (short shared cache inside the mail module)
            # plus local state rows created by acquire. Disabled providers
            # skip the remote call entirely — /api/status must stay fast.
            try:
                fallback_capacity = max(
                    0,
                    min(1000, int(item.get("capacity") or FASTMAIL_API_DEFAULT_CAPACITY)),
                )
            except (TypeError, ValueError):
                fallback_capacity = FASTMAIL_API_DEFAULT_CAPACITY
            if bool(item.get("enable", True)):
                conf = _make_config(mail_cfg)
                conf["request_timeout"] = min(float(conf.get("request_timeout") or 30), 6.0)
                fastmail_provider = FastmailApiProvider(dict(item), conf)
                try:
                    summary = fastmail_provider.pool_summary()
                finally:
                    fastmail_provider.close()
                if summary.get("error"):
                    available = fallback_capacity
                    total = fallback_capacity
                else:
                    available = max(0, int(summary.get("available") or 0))
                    total = max(available, int(summary.get("total") or 0))
            else:
                available = fallback_capacity
                total = fallback_capacity
            local_rows = [
                {"email": email}
                for email, entry in state.items()
                if isinstance(entry, dict)
                and normalize_provider_type(str(entry.get("provider") or "")) == "fastmail_api"
            ]
            local_stats = _pool_stats_for(local_rows, state)
            stats = {
                "total": total,
                "available": available,
                "used": local_stats["used"],
                "failed": local_stats["failed"],
                "in_use": local_stats["in_use"],
                "token_invalid": local_stats["token_invalid"],
                "cooldown": local_stats["cooldown"],
            }
        else:
            pool = parse_outlook_credentials(lines)
            stats = _pool_stats_for(pool, state)
        for k in totals:
            totals[k] += stats[k]
        api_secret = str(item.get("api_key") or item.get("api_token") or "")
        providers_out.append(
            {
                "type": ptype,
                "enable": bool(item.get("enable", True)),
                "label": str(item.get("label") or ptype),
                "mode": str(
                    item.get("mode")
                    or (
                        "api"
                        if ptype in {"icloud_api", "domain_api", "maddy_webui", "fastmail_api"}
                        else "imap"
                    )
                ),
                "api_base": str(item.get("api_base") or ""),
                "default_domain": str(item.get("default_domain") or item.get("domain") or ""),
                "api_key": _mask_secret(api_secret) if api_secret else "",
                "api_token": _mask_secret(api_secret) if api_secret else "",
                "mailboxes": lines,
                **stats,
            }
        )
    primary = next(
        (p for p in providers_out if p["type"] == "outlook_token"),
        providers_out[0] if providers_out else {},
    )
    return {
        **totals,
        "split_total": int(capacity.get("split_total") or 0),
        "split_available": int(
            capacity.get("split_slot_available")
            if capacity.get("split_slot_available") is not None
            else capacity.get("split_available") or 0
        ),
        "split_usable": int(capacity.get("split_available") or 0),
        "split_used": int(capacity.get("split_used") or 0),
        "registration_available": int(capacity.get("available") or 0),
        "total_slots": int(capacity.get("total_slots") or totals["total"]),
        "mode": str(primary.get("mode") or "imap"),
        "label": " + ".join(p["label"] for p in providers_out) or "Mail Pool",
        "providers": providers_out,
    }


def _load_accounts() -> list[dict[str, Any]]:
    return load_accounts(ACCOUNTS_FILE)


_DEFAULT_LOAD_ACCOUNTS = _load_accounts


def _groups_with_counts() -> list[dict[str, Any]]:
    groups = list_groups(GROUPS_FILE)
    default_id = groups[0]["id"]
    known = {item["id"] for item in groups}
    stored = account_group_counts(ACCOUNTS_FILE)
    counts = {group_id: stored.get(group_id, 0) for group_id in known}
    counts[default_id] += sum(
        count for group_id, count in stored.items() if group_id not in known
    )
    if any(group_id not in known and count for group_id, count in stored.items()):
        missing_keys = [
            str(item.get("delete_key") or "")
            for item in load_account_public_views(path=ACCOUNTS_FILE)
            if str(item.get("group_id") or "").strip() not in known
        ]
        if missing_keys:
            assign_account_group(
                missing_keys,
                default_id,
                include_records=False,
                path=ACCOUNTS_FILE,
            )
    return [{**item, "account_count": counts[item["id"]]} for item in groups]


def _groups_with_counts_for_accounts(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build group counts from an existing account snapshot.

    The accounts endpoint already loads the full account file. Reusing that
    snapshot avoids a second multi-megabyte JSON parse on every page refresh.
    """
    groups = list_groups(GROUPS_FILE)
    default_id = groups[0]["id"]
    counts = {item["id"]: 0 for item in groups}
    missing_keys: list[str] = []
    for account in accounts:
        group_id = str(account.get("group_id") or "").strip()
        if group_id not in counts:
            group_id = default_id
            missing_keys.append(_account_key(account))
        counts[group_id] += 1
    if missing_keys:
        assign_account_group(
            missing_keys,
            default_id,
            include_records=False,
            path=ACCOUNTS_FILE,
        )
    return [{**item, "account_count": counts[item["id"]]} for item in groups]


def _credential_lines_for_group(group_id: str) -> tuple[list[str], int]:
    group = get_group(group_id, GROUPS_FILE)
    if group is None:
        raise KeyError("分组不存在")
    wanted = {
        str(item.get("email") or "").strip().lower()
        for item in load_account_public_views(path=ACCOUNTS_FILE)
        if str(item.get("group_id") or "").strip() == group_id
        and str(item.get("email") or "").strip()
    }
    credentials: dict[str, str] = {}
    cfg = _read_config()
    for provider in _providers(cfg):
        ptype = normalize_provider_type(str(provider.get("type") or ""))
        raw = str(provider.get("mailboxes") or "")
        if ptype == "outlook_token":
            for item in parse_outlook_credentials(raw):
                email = item["email"].strip().lower()
                credentials.setdefault(
                    email,
                    "----".join(
                        [
                            item["email"],
                            item["password"],
                            item["client_id"],
                            item["refresh_token"],
                        ]
                    ),
                )
        elif ptype == "icloud_api":
            for item in parse_icloud_credentials(raw):
                email = item["email"].strip().lower()
                credentials.setdefault(
                    email, "----".join([item["email"], item["api_key"]])
                )
    lines = [credentials[email] for email in sorted(wanted) if email in credentials]
    return lines, len(wanted) - len(lines)


def _domain_mailbox_remote_key(provider: dict[str, Any], provider_index: int) -> str:
    """Build a stable cache key for one configured domain provider."""
    try:
        payload = json.dumps(
            {
                "provider_index": int(provider_index),
                "provider": provider,
                # Tests and hot-reload deployments can replace the provider
                # class; avoid reusing a snapshot produced by the old class.
                "provider_impl": id(DomainApiProvider),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
    except Exception:
        payload = f"{provider_index}:{repr(provider)}"
    return _cache_digest(payload)


def _copy_mailbox_pool(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Return a detached pool so callers cannot mutate the shared snapshot."""
    return [dict(item) for item in rows or [] if isinstance(item, dict)]


def _domain_mailbox_pool(
    cfg: dict[str, Any],
    provider: dict[str, Any],
    provider_index: int,
    lines: str,
) -> list[dict[str, Any]]:
    """Read a domain provider mailbox pool with a short stale-on-error cache.

    The cache is deliberately local to the panel listing path. Registration
    allocation still constructs its provider and reads live state directly.
    """
    key = _domain_mailbox_remote_key(provider, provider_index)
    now = time.monotonic()
    with _DOMAIN_MAILBOX_REMOTE_CACHE_LOCK:
        cached = _DOMAIN_MAILBOX_REMOTE_CACHE.get(key)
        if cached is not None and now - cached[0] < _DOMAIN_MAILBOX_REMOTE_CACHE_TTL:
            return _copy_mailbox_pool(cached[1])
        refresh_lock = _DOMAIN_MAILBOX_REMOTE_FETCH_LOCKS.get(key)
        if refresh_lock is None:
            refresh_lock = threading.Lock()
            _DOMAIN_MAILBOX_REMOTE_FETCH_LOCKS[key] = refresh_lock
            if len(_DOMAIN_MAILBOX_REMOTE_FETCH_LOCKS) > 128:
                # Keep the lock map bounded; active locks remain referenced by
                # their callers even if they are evicted from this registry.
                oldest = next(iter(_DOMAIN_MAILBOX_REMOTE_FETCH_LOCKS))
                if oldest != key:
                    _DOMAIN_MAILBOX_REMOTE_FETCH_LOCKS.pop(oldest, None)
        stale = _copy_mailbox_pool(cached[1]) if cached is not None else None

    with refresh_lock:
        now = time.monotonic()
        with _DOMAIN_MAILBOX_REMOTE_CACHE_LOCK:
            cached = _DOMAIN_MAILBOX_REMOTE_CACHE.get(key)
            if cached is not None and now - cached[0] < _DOMAIN_MAILBOX_REMOTE_CACHE_TTL:
                return _copy_mailbox_pool(cached[1])
            stale = _copy_mailbox_pool(cached[1]) if cached is not None else stale

        username = str(provider.get("username") or "").strip()
        password = str(provider.get("password") or "").strip()
        if not bool(provider.get("enable", True)) or not username or not password:
            return parse_domain_mailboxes(lines)

        domain_provider = None
        try:
            mail_cfg = cfg.get("mail") if isinstance(cfg.get("mail"), dict) else {}
            conf = _make_config(mail_cfg)
            conf["request_timeout"] = min(
                float(conf.get("request_timeout") or 30), 8.0
            )
            domain_provider = DomainApiProvider(provider, conf)
            pool: list[dict[str, Any]] = []
            for remote in domain_provider.list_mailboxes(limit=2000):
                if not isinstance(remote, dict):
                    continue
                address = str(
                    remote.get("address") or remote.get("email") or ""
                ).strip()
                if address:
                    pool.append(
                        {"email": address, "id": str(remote.get("id") or "")}
                    )
            # Do not retain arbitrary provider payloads or more rows than the
            # endpoint can expose.  This also bounds memory if a provider
            # ignores its requested page size.
            pool = pool[:2000]
            with _DOMAIN_MAILBOX_REMOTE_CACHE_LOCK:
                _DOMAIN_MAILBOX_REMOTE_CACHE[key] = (time.monotonic(), _copy_mailbox_pool(pool))
                while len(_DOMAIN_MAILBOX_REMOTE_CACHE) > 64:
                    _DOMAIN_MAILBOX_REMOTE_CACHE.pop(
                        next(iter(_DOMAIN_MAILBOX_REMOTE_CACHE))
                    )
            return _copy_mailbox_pool(pool)
        except Exception:
            if stale is not None:
                # A failed refresh must not make an otherwise usable panel
                # blank.  Advance the timestamp to avoid retrying the same
                # unavailable upstream for every concurrent request.
                with _DOMAIN_MAILBOX_REMOTE_CACHE_LOCK:
                    _DOMAIN_MAILBOX_REMOTE_CACHE[key] = (
                        time.monotonic(),
                        _copy_mailbox_pool(stale),
                    )
                return _copy_mailbox_pool(stale)
            return parse_domain_mailboxes(lines)
        finally:
            if domain_provider is not None:
                try:
                    domain_provider.close()
                except Exception:
                    pass


def _mailbox_items(
    cfg: dict[str, Any], *, provider_type: str = "auto", q: str = ""
) -> list[dict[str, Any]]:
    want = normalize_provider_type(provider_type)
    if want in {"outlook", "ms", "microsoft"}:
        want = "outlook_token"
    elif want in {"icloud", "apple", "hme", "hide_my_email"}:
        want = "icloud_api"
    elif want in {"", "auto", "any", "all"}:
        want = ""

    state = _load_state_file()
    registered = account_email_set(ACCOUNTS_FILE)
    query = str(q or "").strip().lower()
    out: list[dict[str, Any]] = []

    for pi, provider in enumerate(_providers(cfg), start=1):
        ptype = normalize_provider_type(str(provider.get("type") or ""))
        if ptype not in CANONICAL_PROVIDER_TYPES:
            continue
        if want and ptype != want:
            continue
        lines = str(provider.get("mailboxes") or "")
        if ptype == "domain_api":
            pool = _domain_mailbox_pool(cfg, provider, pi, lines)
        elif ptype == "maddy_webui":
            pool = parse_domain_mailboxes(lines)
            # Maddy creates dynamic mailboxes through the automation API and
            # does not expose a mailbox-pool listing endpoint. The shared
            # state file is the local index for mailboxes created by this
            # provider, including their terminal registration state.
            provider_ref = f"{ptype}#{pi}"
            configured_api_base = str(provider.get("api_base") or "").strip().rstrip("/").lower()
            configured_domains = {
                str(value or "").strip().lower()
                for value in (
                    provider.get("default_domain"),
                    provider.get("domain"),
                )
                if str(value or "").strip()
            }
            extra_domains = provider.get("domains")
            if isinstance(extra_domains, list):
                configured_domains.update(
                    str(value or "").strip().lower()
                    for value in extra_domains
                    if str(value or "").strip()
                )
            elif isinstance(extra_domains, str):
                configured_domains.update(
                    value.strip().lower()
                    for value in re.split(r"[,;\s]+", extra_domains)
                    if value.strip()
                )
            known = {str(item.get("email") or "").strip().lower() for item in pool}
            for address, state_entry in state.items():
                if not isinstance(state_entry, dict) or "@" not in str(address):
                    continue
                entry_provider = normalize_provider_type(str(state_entry.get("provider") or ""))
                entry_ref = str(state_entry.get("provider_ref") or "").strip()
                entry_api_base = str(state_entry.get("api_base") or "").strip().rstrip("/").lower()
                address_domain = str(address).rsplit("@", 1)[1].lower()
                if entry_ref and entry_ref != provider_ref:
                    continue
                if entry_provider not in {ptype, ""} and entry_ref != provider_ref:
                    continue
                if entry_provider == "" and not (
                    configured_api_base
                    and entry_api_base
                    and entry_api_base == configured_api_base
                ):
                    continue
                if configured_domains and address_domain not in configured_domains:
                    continue
                if not entry_ref and configured_api_base and entry_api_base and entry_api_base != configured_api_base:
                    continue
                email = str(address).strip().lower()
                if email not in known:
                    pool.append({"email": email})
                    known.add(email)
        elif ptype == "fastmail_api":
            pool = parse_domain_mailboxes(lines)
            # mailcode-api owns the alias pool; locally we only track the
            # aliases this provider acquired (state file metadata).
            provider_ref = f"{ptype}#{pi}"
            configured_api_base = str(provider.get("api_base") or "").strip().rstrip("/").lower()
            known = {str(item.get("email") or "").strip().lower() for item in pool}
            for address, state_entry in state.items():
                if not isinstance(state_entry, dict) or "@" not in str(address):
                    continue
                entry_provider = normalize_provider_type(str(state_entry.get("provider") or ""))
                entry_ref = str(state_entry.get("provider_ref") or "").strip()
                entry_api_base = str(state_entry.get("api_base") or "").strip().rstrip("/").lower()
                if entry_ref and entry_ref != provider_ref:
                    continue
                if entry_provider not in {ptype, ""}:
                    continue
                if entry_provider == "" and not (
                    configured_api_base
                    and entry_api_base
                    and entry_api_base == configured_api_base
                ):
                    continue
                email = str(address).strip().lower()
                if email not in known:
                    pool.append({"email": email})
                    known.add(email)
        elif ptype == "icloud_api":
            pool = parse_icloud_credentials(lines)
        else:
            pool = parse_outlook_credentials(lines)
        for idx, item in enumerate(pool, start=1):
            email = str(item.get("email") or "").strip()
            email_l = email.lower()
            st_entry = (
                state.get(email_l) if isinstance(state.get(email_l), dict) else {}
            )
            st = str((st_entry or {}).get("state") or "available").strip() or "available"
            row = {
                "id": f"{ptype}:{email_l}",
                "index": idx,
                "provider_index": pi,
                "provider_type": ptype,
                "provider_label": str(provider.get("label") or ptype),
                "enabled": bool(provider.get("enable", True)),
                "email": email,
                "login_address": base_mailbox_address(email),
                "state": st,
                "reason": str((st_entry or {}).get("reason") or ""),
                "updated_at": str((st_entry or {}).get("updated_at") or ""),
                "registered": email_l in registered,
                "mode": str(
                    provider.get("mode")
                    or (
                        "api"
                        if ptype in {"icloud_api", "domain_api", "maddy_webui", "fastmail_api"}
                        else "imap"
                    )
                ),
                "api_base": str(provider.get("api_base") or ""),
                "client_id": _mask_secret(str(item.get("client_id") or "")),
                "secret": _mask_secret(
                    str(item.get("api_key") or item.get("refresh_token") or "")
                ),
            }
            hay = " ".join(
                str(row.get(k) or "")
                for k in ("email", "provider_type", "provider_label", "state", "mode")
            ).lower()
            if query and query not in hay:
                continue
            out.append(row)
    return out


def _find_mailbox_credential(
    cfg: dict[str, Any], provider_type: str | None, email: str
) -> tuple[dict[str, Any], dict[str, str]]:
    target = str(email or "").strip().lower()
    want = normalize_provider_type(str(provider_type or ""))
    if want in {"outlook", "ms", "microsoft"}:
        want = "outlook_token"
    elif want in {"icloud", "apple", "hme", "hide_my_email"}:
        want = "icloud_api"
    elif want in {"", "auto", "any", "all"}:
        want = ""
    for provider in _providers(cfg):
        ptype = normalize_provider_type(str(provider.get("type") or ""))
        if ptype not in CANONICAL_PROVIDER_TYPES or (want and ptype != want):
            continue
        if ptype == "fastmail_api":
            if any(
                str(item.get("email") or "").strip().lower() == target
                for item in parse_domain_mailboxes(str(provider.get("mailboxes") or ""))
            ):
                return dict(provider, type=ptype), {"email": target}
            state_entry = _load_state_file().get(target)
            if isinstance(state_entry, dict) and (
                normalize_provider_type(str(state_entry.get("provider") or "")) == "fastmail_api"
            ):
                return dict(provider, type=ptype), {
                    "email": target,
                    "account": str(state_entry.get("account") or ""),
                    "key": str(state_entry.get("key") or ""),
                }
            # Configured pool acts as a fallback owner; the message fetch
            # resolves the alias key through the mailcode-api admin API.
            if str(provider.get("accounts") or "").strip() or str(
                provider.get("admin_key") or provider.get("api_key") or ""
            ).strip():
                return dict(provider, type=ptype), {"email": target}
            continue
        if ptype in {"domain_api", "maddy_webui"}:
            target_domain = target.rsplit("@", 1)[1] if "@" in target else ""
            configured_domains = {
                str(value or "").strip().lower()
                for value in (
                    provider.get("default_domain"),
                    provider.get("domain"),
                )
                if str(value or "").strip()
            }
            extra_domains = provider.get("domains")
            if isinstance(extra_domains, list):
                configured_domains.update(
                    str(value or "").strip().lower()
                    for value in extra_domains
                    if str(value or "").strip()
                )
            elif isinstance(extra_domains, str):
                configured_domains.update(
                    value.strip().lower()
                    for value in re.split(r"[,;\s]+", extra_domains)
                    if value.strip()
                )
            if not configured_domains or target_domain in configured_domains:
                lines = str(provider.get("mailboxes") or "")
                if any(
                    str(item.get("email") or "").strip().lower() == target
                    for item in parse_domain_mailboxes(lines)
                ):
                    return dict(provider, type=ptype), {"email": target}
                state_entry = _load_state_file().get(target)
                if isinstance(state_entry, dict):
                    entry_provider = normalize_provider_type(str(state_entry.get("provider") or ""))
                    entry_api_base = str(state_entry.get("api_base") or "").strip().rstrip("/").lower()
                    provider_api_base = str(provider.get("api_base") or "").strip().rstrip("/").lower()
                    if entry_provider == ptype or (
                        ptype == "maddy_webui"
                        and entry_api_base
                        and provider_api_base
                        and entry_api_base == provider_api_base
                    ):
                        return dict(provider, type=ptype), {"email": target}
                if ptype == "domain_api":
                    return dict(provider, type=ptype), {"email": target}
            continue
        pool = (
            parse_icloud_credentials(str(provider.get("mailboxes") or ""))
            if ptype == "icloud_api"
            else parse_outlook_credentials(str(provider.get("mailboxes") or ""))
        )
        for item in pool:
            if str(item.get("email") or "").strip().lower() == target:
                return dict(provider, type=ptype), item
    raise HTTPException(404, f"邮箱不在当前邮箱池中: {email}")


def _message_preview(message: dict[str, Any]) -> dict[str, Any]:
    received = message.get("received_at")
    if hasattr(received, "isoformat"):
        received_at = received.isoformat()
    else:
        received_at = str(received or "")
    text = str(message.get("text_content") or "")
    html_body = str(message.get("html_content") or "")
    body = text or re.sub(r"<[^>]+>", " ", html_body)
    body = re.sub(r"\s+", " ", body).strip()
    safe_html = re.sub(r"(?is)<script\b[^>]*>.*?</script>", "", html_body)
    safe_html = re.sub(r"(?is)<iframe\b[^>]*>.*?</iframe>", "", safe_html)
    return {
        "subject": str(message.get("subject") or ""),
        "sender": str(message.get("sender") or ""),
        "recipients": [str(x) for x in (message.get("recipients") or [])][:8],
        "received_at": received_at,
        "code": _extract_code(message) or "",
        "preview": body[:260],
        "text": text[:200000],
        "html": safe_html[:300000],
    }


def _refresh_icloud_inbox_cache(
    provider: ICloudApiProvider,
    base: str,
    credential: dict[str, str],
    conf: dict[str, Any],
) -> None:
    """Coalesce the upstream iCloud sync shared by all inbox viewers."""
    global _ICLOUD_INBOX_SYNC_AT
    now = time.monotonic()
    if now - _ICLOUD_INBOX_SYNC_AT < _ICLOUD_INBOX_SYNC_INTERVAL:
        return
    if not _ICLOUD_INBOX_SYNC_LOCK.acquire(blocking=False):
        return
    try:
        now = time.monotonic()
        if now - _ICLOUD_INBOX_SYNC_AT < _ICLOUD_INBOX_SYNC_INTERVAL:
            return
        try:
            provider.session.get(
                f"{base}/api/v1/code",
                params={"key": credential["api_key"], "email": credential["email"]},
                headers={"Accept": "application/json", "User-Agent": conf["user_agent"]},
                timeout=6.0,
            )
        finally:
            # Cool down failed upstream attempts too, otherwise many open inboxes
            # immediately recreate the same timeout storm.
            _ICLOUD_INBOX_SYNC_AT = time.monotonic()
    finally:
        _ICLOUD_INBOX_SYNC_LOCK.release()


def _fetch_mailbox_messages_sync(
    cfg: dict[str, Any], body: MailMessagesRequest
) -> dict[str, Any]:
    provider_entry, cred = _find_mailbox_credential(cfg, body.provider_type, body.email)
    ptype = normalize_provider_type(str(provider_entry.get("type") or ""))
    conf = _make_config(cfg.get("mail") if isinstance(cfg.get("mail"), dict) else {})
    conf["request_timeout"] = min(float(conf.get("request_timeout") or 30), 8.0)
    if ptype == "outlook_token":
        provider_entry = dict(
            provider_entry, message_limit=max(1, min(int(body.limit or 10), 30))
        )
        provider = OutlookTokenProvider(provider_entry, conf)
        mailbox = {
            "provider": "outlook_token",
            "provider_ref": "preview",
            "address": cred["email"],
            "login_address": base_mailbox_address(cred["email"]),
            "base_address": base_mailbox_address(cred["email"]),
            "label": str(provider_entry.get("label") or "Outlook Pool"),
            "password": cred.get("password", ""),
            "client_id": cred["client_id"],
            "refresh_token": cred["refresh_token"],
        }
        try:
            messages = provider.fetch_recent_messages(mailbox)[: body.limit]
            return {
                "ok": True,
                "provider_type": ptype,
                "email": cred["email"],
                "mode": provider.mode,
                "items": [_message_preview(m) for m in messages],
            }
        finally:
            provider.close()

    if ptype == "domain_api":
        provider_entry = dict(
            provider_entry, message_limit=max(1, min(int(body.limit or 10), 30))
        )
        provider = DomainApiProvider(provider_entry, conf)
        mailbox = {
            "provider": "domain_api",
            "provider_ref": "preview",
            "address": cred["email"],
            "login_address": cred["email"],
            "base_address": base_mailbox_address(cred["email"]),
            "label": str(provider_entry.get("label") or "域名邮箱"),
            "api_base": str(provider_entry.get("api_base") or ""),
        }
        try:
            messages = provider.fetch_recent_messages(mailbox)[: body.limit]
            return {
                "ok": True,
                "provider_type": ptype,
                "email": cred["email"],
                "mode": "api",
                "items": [_message_preview(m) for m in messages],
            }
        finally:
            provider.close()

    if ptype == "maddy_webui":
        provider_entry = dict(
            provider_entry, message_limit=max(1, min(int(body.limit or 10), 30))
        )
        provider = MaddyWebuiProvider(provider_entry, conf)
        mailbox = {
            "provider": "maddy_webui",
            "provider_ref": "preview",
            "address": cred["email"],
            "login_address": cred["email"],
            "base_address": base_mailbox_address(cred["email"]),
            "label": str(provider_entry.get("label") or "Maddy 邮箱"),
            "api_base": str(provider_entry.get("api_base") or ""),
        }
        try:
            messages = provider.fetch_recent_messages(mailbox)[: body.limit]
            return {
                "ok": True,
                "provider_type": ptype,
                "email": cred["email"],
                "mode": "api",
                "items": [_message_preview(m) for m in messages],
            }
        finally:
            provider.close()

    if ptype == "fastmail_api":
        provider_entry = dict(
            provider_entry, message_limit=max(1, min(int(body.limit or 10), 30))
        )
        provider = FastmailApiProvider(provider_entry, conf)
        mailbox = {
            "provider": "fastmail_api",
            "provider_ref": str(provider_entry.get("provider_ref") or "preview"),
            "address": cred["email"],
            "login_address": cred["email"],
            "base_address": base_mailbox_address(cred["email"]),
            "label": str(provider_entry.get("label") or "Fastmail Pool"),
            "api_base": str(provider_entry.get("api_base") or ""),
            "account": str(cred.get("account") or ""),
            "alias_key": str(cred.get("key") or ""),
        }
        try:
            messages = provider.fetch_recent_messages(mailbox)[: body.limit]
            return {
                "ok": True,
                "provider_type": ptype,
                "email": cred["email"],
                "mode": "api",
                "items": [_message_preview(m) for m in messages],
            }
        finally:
            provider.close()

    provider = ICloudApiProvider(provider_entry, conf)
    mailbox = {
        "provider": "icloud_api",
        "provider_ref": "preview",
        "address": cred["email"],
        "login_address": base_mailbox_address(cred["email"]),
        "base_address": base_mailbox_address(cred["email"]),
        "label": str(provider_entry.get("label") or "iCloud Pool"),
        "api_key": cred["api_key"],
        "api_base": str(provider_entry.get("api_base") or ""),
    }
    try:
        base = str(mailbox.get("api_base") or provider.api_base).rstrip("/")
        # /messages only lists the API server's local cache. Coalesce the
        # upstream sync so many inbox tabs do not each start an iCloud request.
        try:
            _refresh_icloud_inbox_cache(provider, base, cred, conf)
        except Exception:
            pass
        resp = provider.session.get(
            f"{base}/api/v1/messages",
            params={
                "key": cred["api_key"],
                "email": cred["email"],
                "limit": max(1, min(int(body.limit or 10), 30)),
            },
            headers={"Accept": "application/json", "User-Agent": conf["user_agent"]},
            timeout=8.0,
        )
        try:
            data = resp.json()
        except Exception:
            data = {"ok": False, "error": resp.text[:300]}
        if resp.status_code != 200:
            data = {
                "ok": False,
                "error": data.get("error")
                or data.get("message")
                or f"HTTP {resp.status_code}",
            }
        if not isinstance(data, dict):
            data = {"ok": False, "error": "invalid iCloud API response"}
        if data.get("ok") is False:
            return {
                "ok": False,
                "provider_type": ptype,
                "email": cred["email"],
                "mode": "api",
                "items": [],
                "error": str(
                    data.get("error")
                    or data.get("message")
                    or data.get("sync_error")
                    or "iCloud API 暂无可读邮件"
                ),
            }
        messages = data.get("messages") if isinstance(data.get("messages"), list) else []
        items = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            recipient = message.get("to") or message.get("alias_email") or cred["email"]
            recipients = recipient if isinstance(recipient, list) else [str(recipient or "")]
            preview = str(message.get("preview") or "")
            subject = str(message.get("subject") or "")
            subject_lower = subject.lower()
            code = str(message.get("code") or "").strip()
            if not re.fullmatch(r"\d{6}", code) or any(
                marker in subject_lower
                for marker in ("new sign-in", "new login", "security alert")
            ):
                code = ""
            items.append(
                {
                    "id": str(message.get("id") or ""),
                    "subject": subject,
                    "sender": str(message.get("from") or message.get("sender") or ""),
                    "recipients": [str(value) for value in recipients if str(value)],
                    "received_at": str(message.get("received_at") or ""),
                    "code": code,
                    "preview": preview[:260],
                    "text": preview[:200000],
                    "html": "",
                    "has_raw": bool(message.get("id")),
                }
            )
        return {
            "ok": True,
            "provider_type": ptype,
            "email": cred["email"],
            "mode": "api",
            "items": items,
        }
    finally:
        provider.close()


def _fetch_icloud_message_sync(
    cfg: dict[str, Any], email: str, provider_type: str, message_id: str
) -> dict[str, Any]:
    provider_entry, cred = _find_mailbox_credential(cfg, provider_type, email)
    ptype = normalize_provider_type(str(provider_entry.get("type") or ""))
    if ptype != "icloud_api":
        raise HTTPException(400, "该邮箱类型不支持远程正文查询")
    conf = _make_config(cfg.get("mail") if isinstance(cfg.get("mail"), dict) else {})
    provider = ICloudApiProvider(provider_entry, conf)
    try:
        base = str(provider_entry.get("api_base") or provider.api_base).rstrip("/")
        resp = provider.session.get(
            f"{base}/api/v1/message/raw",
            params={
                "key": cred["api_key"],
                "email": cred["email"],
                "id": str(message_id),
            },
            headers={"Accept": "application/json", "User-Agent": conf["user_agent"]},
            timeout=12.0,
        )
        try:
            data = resp.json()
        except Exception:
            data = {"ok": False, "error": resp.text[:300]}
        if resp.status_code != 200 or not isinstance(data, dict) or data.get("ok") is False:
            detail = (data.get("error") if isinstance(data, dict) else "") or f"HTTP {resp.status_code}"
            raise HTTPException(502, f"邮件正文读取失败: {detail}")
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        if not message:
            raise HTTPException(404, "邮件不存在")
        recipient = message.get("to") or cred["email"]
        recipients = recipient if isinstance(recipient, list) else [str(recipient or "")]
        text = str(message.get("body_text") or "")
        subject = str(message.get("subject") or "")
        code = str(message.get("code") or "").strip()
        if not re.fullmatch(r"\d{6}", code) or any(
            marker in subject.lower()
            for marker in ("new sign-in", "new login", "security alert")
        ):
            code = ""
        return {
            "ok": True,
            "item": {
                "id": str(message.get("id") or message_id),
                "subject": subject,
                "sender": str(message.get("from") or ""),
                "recipients": [str(value) for value in recipients if str(value)],
                "received_at": str(message.get("received_at") or ""),
                "code": code,
                "preview": text[:260],
                "text": text[:200000],
                "html": str(message.get("body_html") or "")[:300000],
            },
        }
    finally:
        provider.close()


def _account_key(record: dict[str, Any]) -> str:
    return str(account_public_view(record).get("delete_key") or "")


def _records_by_keys_with_fallback(
    keys: list[str] | set[str] | tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    """Load selected full records, preserving compatibility with legacy stores.

    The SQLite path is the normal fast path.  A fallback to ``_load_accounts``
    is only needed for records that are absent from the keyed store (including
    test doubles and older JSON-only snapshots), so it does not reintroduce a
    full decode for ordinary panel requests.
    """
    wanted = list(dict.fromkeys(str(key or "").strip() for key in keys if str(key or "").strip()))
    if not wanted:
        return {}
    found = load_account_records_by_keys(wanted, path=ACCOUNTS_FILE)
    missing = [key for key in wanted if key not in found]
    # A partial miss is a normal stale-selection case; report it as missing
    # rather than decoding the entire account store.  The fallback is reserved
    # for legacy/mock stores where none of the requested keys are available.
    # A production SQLite miss is a normal stale-selection/404 case. Only
    # decode the legacy snapshot when SQLite is absent or a test/legacy caller
    # replaced the loader explicitly.
    compatibility_loader = _load_accounts is not _DEFAULT_LOAD_ACCOUNTS
    standard_json = (ROOT / "data" / "registered_accounts.json").resolve()
    legacy_store = Path(ACCOUNTS_FILE).resolve() != standard_json or not Path(ACCOUNTS_DB_FILE).is_file()
    if missing and not found and (compatibility_loader or legacy_store):
        legacy = {_account_key(item): item for item in _load_accounts()}
        for key in missing:
            item = legacy.get(key)
            if item is not None:
                found[key] = item
    return found


def _account_cdk_security_ready(record: dict[str, Any]) -> bool:
    """Only expose accounts with a usable password-backed TOTP setup to CDK."""
    if account_is_deactivated(record):
        return False
    password = str(record.get("password") or "").strip()
    totp_secret = str(record.get("totp_secret") or "").strip()
    mfa_enabled = bool(record.get("mfa_enabled")) or bool(totp_secret)
    return bool(password and totp_secret and mfa_enabled)


def _cdk_account_marks() -> dict[str, dict[str, Any]]:
    """Index CDK binding/redeem state by every stable account identity."""
    marks: dict[str, dict[str, Any]] = {}
    priority = {"redeemed": 3, "available": 2, "disabled": 1}
    for record in list_cdks(CDKS_FILE):
        status = str(record.get("status") or "available").strip().lower()
        mark = {
            "cdk_bound": True,
            "cdk_status": status,
            "cdk_redeemed": status == "redeemed",
            "cdk_redeemed_at": str(record.get("redeemed_at") or ""),
            "cdk_last_access_at": str(record.get("last_access_at") or ""),
        }
        aliases = {
            str(record.get("account_key") or "").strip(),
            f"email:{str(record.get('email') or '').strip().lower()}" if str(record.get("email") or "").strip() else "",
            f"account_id:{str(record.get('account_id') or '').strip().lower()}" if str(record.get("account_id") or "").strip() else "",
        }
        for alias in aliases:
            if not alias:
                continue
            current = marks.get(alias)
            if current is None or priority.get(status, 0) >= priority.get(str(current.get("cdk_status") or ""), 0):
                marks[alias] = dict(mark)
    return marks


def _mailbox_provider_map(cfg: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for provider in _providers(cfg):
        ptype = normalize_provider_type(str(provider.get("type") or ""))
        if ptype not in CANONICAL_PROVIDER_TYPES:
            continue
        if ptype in {"domain_api", "maddy_webui", "fastmail_api"}:
            parser = parse_domain_mailboxes
        else:
            parser = parse_icloud_credentials if ptype == "icloud_api" else parse_outlook_credentials
        for item in parser(str(provider.get("mailboxes") or "")):
            email = str(item.get("email") or "").strip().lower()
            if email:
                result.setdefault(email, ptype)
    # Dynamic fastmail aliases live in the state file, not in mailboxes text.
    for email, entry in _load_state_file().items():
        if not isinstance(entry, dict):
            continue
        if normalize_provider_type(str(entry.get("provider") or "")) == "fastmail_api":
            address = str(email or "").strip().lower()
            if address:
                result.setdefault(address, "fastmail_api")
    return result


def _eligible_cdk_bindings(group_id: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    group = get_group(group_id, GROUPS_FILE)
    if group is None:
        raise KeyError("分组不存在")
    provider_map = _mailbox_provider_map(_read_config())
    existing = list_cdks(CDKS_FILE)
    bound_keys = {str(item.get("account_key") or "") for item in existing}
    bindings: list[dict[str, Any]] = []
    stats = {
        "group_accounts": 0,
        "without_mailbox": 0,
        "invalid": 0,
        "already_bound": 0,
        "security_incomplete": 0,
    }
    for account in _load_accounts():
        if str(account.get("group_id") or "").strip() != group_id:
            continue
        stats["group_accounts"] += 1
        email = str(account.get("email") or "").strip()
        email_l = email.lower()
        provider_type = provider_map.get(email_l, str(account.get("provider_type") or "").strip())
        if not email or not provider_type:
            stats["without_mailbox"] += 1
            continue
        if normalized_health_status(account) == "invalid":
            stats["invalid"] += 1
            continue
        account_key = _account_key(account) or f"email:{email_l}"
        if account_key in bound_keys:
            stats["already_bound"] += 1
            continue
        if not _account_cdk_security_ready(account):
            stats["security_incomplete"] += 1
            continue
        bindings.append(
            {
                "account_key": account_key,
                "account_id": str(account.get("account_id") or ""),
                "email": email,
                "provider_type": provider_type,
                "group_id": group_id,
                "group_name": str(group.get("name") or ""),
                "registered_at": str(account.get("registered_at") or ""),
            }
        )
    bindings.sort(key=lambda item: (item.get("registered_at") or "", item["email"]))
    stats["eligible"] = len(bindings)
    return bindings, stats


def _replace_invalid_unredeemed_cdks(
    checked_records: list[dict[str, Any]],
    *,
    accounts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    # Health callers already hold a complete account snapshot. Reusing it
    # avoids decoding the full payload store a second time at the end of a
    # large run.
    accounts = accounts if accounts is not None else _load_accounts()
    account_keys = {_account_key(item) for item in accounts if _account_key(item)}
    account_emails = {
        str(item.get("email") or "").strip().lower()
        for item in accounts
        if str(item.get("email") or "").strip()
    }
    account_ids = {
        str(item.get("account_id") or "").strip().lower()
        for item in accounts
        if str(item.get("account_id") or "").strip()
    }
    invalid_accounts: list[dict[str, Any]] = []
    invalid_identities: set[tuple[str, str, str]] = set()

    def add_invalid(account: dict[str, Any]) -> None:
        item = {
            "account_key": str(account.get("account_key") or _account_key(account) or ""),
            "account_id": str(account.get("account_id") or ""),
            "email": str(account.get("email") or ""),
        }
        identity = (
            item["account_key"].strip(),
            item["account_id"].strip().lower(),
            item["email"].strip().lower(),
        )
        if identity not in invalid_identities:
            invalid_identities.add(identity)
            invalid_accounts.append(item)

    for account in [*accounts, *checked_records]:
        if normalized_health_status(account) == "invalid":
            add_invalid(account)

    # Keep unresolved CDKs repairable after their invalid account was archived.
    for record in list_cdks(CDKS_FILE):
        key = str(record.get("account_key") or "").strip()
        email = str(record.get("email") or "").strip().lower()
        account_id = str(record.get("account_id") or "").strip().lower()
        exists = (
            (key and key in account_keys)
            or (email and email in account_emails)
            or (account_id and account_id in account_ids)
        )
        if not exists:
            add_invalid(record)

    if not invalid_accounts:
        return {"replaced": 0, "unresolved": 0, "items": []}

    provider_map = _mailbox_provider_map(_read_config())
    group_names = {item["id"]: item["name"] for item in _groups_with_counts()}
    candidates: list[dict[str, Any]] = []
    for account in accounts:
        health = normalized_health_status(account)
        if health not in {"alive", "limited"}:
            continue
        if not _account_cdk_security_ready(account):
            continue
        email = str(account.get("email") or "").strip()
        provider_type = provider_map.get(email.lower(), str(account.get("provider_type") or "").strip())
        group_id = str(account.get("group_id") or "").strip()
        if not email or not provider_type or not group_id:
            continue
        candidates.append(
            {
                "account_key": _account_key(account) or f"email:{email.lower()}",
                "account_id": str(account.get("account_id") or ""),
                "email": email,
                "provider_type": provider_type,
                "group_id": group_id,
                "group_name": group_names.get(group_id, ""),
                "health_status": health,
                "registered_at": str(account.get("registered_at") or ""),
            }
        )
    candidates.sort(
        key=lambda item: (
            0 if item["health_status"] == "alive" else 1,
            item["registered_at"],
            item["email"].lower(),
        )
    )
    return replace_available_bindings(invalid_accounts, candidates, CDKS_FILE)


def _merge_health_snapshot(
    accounts: list[dict[str, Any]],
    checked_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Overlay this run's health results without decoding the store again."""
    if not checked_records:
        return accounts
    return normalize_accounts([*accounts, *checked_records])


def _replace_cdks_after_health(
    accounts: list[dict[str, Any]],
    checked_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Merge health results and repair bindings in one worker-thread call."""
    refreshed_records = _merge_health_snapshot(accounts, checked_records)
    return _replace_invalid_unredeemed_cdks(
        checked_records,
        accounts=refreshed_records,
    )


_ACCOUNT_REAUTH_MODES = frozenset({"browser", "protocol"})
_ACCOUNT_HEALTH_PROGRESS_LOCK = threading.Lock()
_MAINTENANCE_RUN_LOCK = threading.Lock()
# Inline row updates are useful for a small selection, but sending and merging
# hundreds of public rows on every progress poll makes an all-account check
# needlessly expensive. Large jobs refresh the account list once at completion.
_ACCOUNT_HEALTH_INLINE_UPDATE_LIMIT = 200
_ACCOUNT_HEALTH_PROGRESS: dict[str, Any] = {
    "running": False,
    "requested": 0,
    "actual_total": 0,
    "total": 0,
    "done": 0,
    "checked": 0,
    "usable": 0,
    "unavailable": 0,
    "skipped_known_deactivated": 0,
    "deactivated": 0,
    "invalid_other": 0,
    "current": "",
    "alive": 0,
    "limited": 0,
    "invalid": 0,
    "reauth_required": 0,
    "error": 0,
    "cdk_replaced": 0,
    "cdk_unresolved": 0,
    "requested_workers": 0,
    "workers": 0,
    "throttled": False,
    "resource_mode": "normal",
    "updates": {},
    "updates_enabled": True,
}


def _account_health_resource_budget(requested_workers: int) -> tuple[int, str]:
    requested = max(1, int(requested_workers or 1))
    job = globals().get("JOB")
    if job is not None:
        with job.lock:
            if job.running:
                return 1, "registration_priority"
    return requested, "normal"


def _account_reauth_resource_budget(requested_workers: int) -> tuple[int, str]:
    requested = max(1, int(requested_workers or 1))
    return requested, "independent"


def _trial_eligibility_resource_budget(requested_workers: int) -> tuple[int, str]:
    requested = max(1, int(requested_workers or 1))
    return requested, "independent"


def _account_health_worker_limit(requested_workers: int) -> int:
    return _account_health_resource_budget(requested_workers)[0]


def _refresh_account_health_resource_state() -> None:
    with _ACCOUNT_HEALTH_PROGRESS_LOCK:
        if not _ACCOUNT_HEALTH_PROGRESS.get("running"):
            return
        requested = max(
            1,
            int(
                _ACCOUNT_HEALTH_PROGRESS.get("requested_workers")
                or _ACCOUNT_HEALTH_PROGRESS.get("workers")
                or 1
            ),
        )
    workers, mode = _account_health_resource_budget(requested)
    with _ACCOUNT_HEALTH_PROGRESS_LOCK:
        if not _ACCOUNT_HEALTH_PROGRESS.get("running"):
            return
        _ACCOUNT_HEALTH_PROGRESS["workers"] = workers
        _ACCOUNT_HEALTH_PROGRESS["throttled"] = workers < requested
        _ACCOUNT_HEALTH_PROGRESS["resource_mode"] = mode


def _start_account_health_progress(
    total: int,
    workers: int,
    *,
    requested: int | None = None,
    skipped_known_deactivated: int = 0,
) -> None:
    actual_total = max(0, int(total or 0))
    skipped = max(0, int(skipped_known_deactivated or 0))
    requested_total = max(
        actual_total + skipped,
        int(requested if requested is not None else actual_total + skipped),
    )
    requested_workers = max(1, int(workers))
    actual_workers, resource_mode = _account_health_resource_budget(requested_workers)
    updates_enabled = actual_total <= _ACCOUNT_HEALTH_INLINE_UPDATE_LIMIT
    with _ACCOUNT_HEALTH_PROGRESS_LOCK:
        _ACCOUNT_HEALTH_PROGRESS.clear()
        _ACCOUNT_HEALTH_PROGRESS.update(
            running=True,
            requested=requested_total,
            actual_total=actual_total,
            total=actual_total,
            done=0,
            current="正在分配检测任务",
            checked=0,
            usable=0,
            unavailable=skipped,
            skipped_known_deactivated=skipped,
            deactivated=skipped,
            invalid_other=0,
            alive=0,
            limited=0,
            invalid=0,
            reauth_required=0,
            error=0,
            cdk_replaced=0,
            cdk_unresolved=0,
            requested_workers=requested_workers,
            workers=actual_workers,
            throttled=actual_workers < requested_workers,
            resource_mode=resource_mode,
            updates={},
            updates_enabled=updates_enabled,
            started_at=_now_iso(),
            finished_at="",
            message="",
        )


def _update_account_health_progress(
    done: int, total: int, item: dict[str, Any]
) -> None:
    state = normalized_health_status(item) or "error"
    if state not in {"alive", "limited", "invalid", "reauth_required", "error"}:
        state = "error"
    with _ACCOUNT_HEALTH_PROGRESS_LOCK:
        updates_enabled = bool(
            _ACCOUNT_HEALTH_PROGRESS.get("updates_enabled", True)
        )
        _ACCOUNT_HEALTH_PROGRESS["done"] = max(0, int(done))
        actual_total = max(0, int(total))
        _ACCOUNT_HEALTH_PROGRESS["actual_total"] = actual_total
        _ACCOUNT_HEALTH_PROGRESS["total"] = actual_total
        _ACCOUNT_HEALTH_PROGRESS["current"] = str(
            item.get("email") or item.get("account_id") or ""
        )
        _ACCOUNT_HEALTH_PROGRESS[state] = int(
            _ACCOUNT_HEALTH_PROGRESS.get(state) or 0
        ) + 1
        _ACCOUNT_HEALTH_PROGRESS["checked"] = int(
            _ACCOUNT_HEALTH_PROGRESS.get("checked") or 0
        ) + 1
        _ACCOUNT_HEALTH_PROGRESS["usable"] = int(
            _ACCOUNT_HEALTH_PROGRESS.get("alive") or 0
        ) + int(_ACCOUNT_HEALTH_PROGRESS.get("limited") or 0)
        _ACCOUNT_HEALTH_PROGRESS["unavailable"] = (
            int(_ACCOUNT_HEALTH_PROGRESS.get("skipped_known_deactivated") or 0)
            + int(_ACCOUNT_HEALTH_PROGRESS.get("invalid") or 0)
            + int(_ACCOUNT_HEALTH_PROGRESS.get("reauth_required") or 0)
            + int(_ACCOUNT_HEALTH_PROGRESS.get("error") or 0)
        )
        if account_is_deactivated(item):
            _ACCOUNT_HEALTH_PROGRESS["deactivated"] = int(
                _ACCOUNT_HEALTH_PROGRESS.get("deactivated") or 0
            ) + 1
        _ACCOUNT_HEALTH_PROGRESS["invalid_other"] = max(
            0,
            int(_ACCOUNT_HEALTH_PROGRESS.get("invalid") or 0)
            - max(
                0,
                int(_ACCOUNT_HEALTH_PROGRESS.get("deactivated") or 0)
                - int(_ACCOUNT_HEALTH_PROGRESS.get("skipped_known_deactivated") or 0),
            ),
        )
        if updates_enabled:
            public_item = account_public_view(item)
            delete_key = str(public_item.get("delete_key") or "").strip()
            if delete_key:
                updates = _ACCOUNT_HEALTH_PROGRESS.setdefault("updates", {})
                if isinstance(updates, dict):
                    updates[delete_key] = public_item
    _refresh_account_health_resource_state()


def _finish_account_health_progress(
    *,
    message: str = "",
    cdk_replaced: int | None = None,
    cdk_unresolved: int | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    with _ACCOUNT_HEALTH_PROGRESS_LOCK:
        _ACCOUNT_HEALTH_PROGRESS["running"] = False
        _ACCOUNT_HEALTH_PROGRESS["finished_at"] = _now_iso()
        _ACCOUNT_HEALTH_PROGRESS["message"] = str(message or "")[:300]
        if isinstance(result, dict):
            for key in (
                "requested",
                "actual_total",
                "checked",
                "usable",
                "unavailable",
                "skipped_known_deactivated",
                "deactivated",
                "invalid_other",
                "alive",
                "limited",
                "invalid",
                "reauth_required",
                "error",
            ):
                if key in result:
                    _ACCOUNT_HEALTH_PROGRESS[key] = max(0, int(result.get(key) or 0))
            _ACCOUNT_HEALTH_PROGRESS["total"] = max(
                0, int(result.get("actual_total") or result.get("checked") or 0)
            )
            _ACCOUNT_HEALTH_PROGRESS["done"] = max(
                0, int(result.get("checked") or 0)
            )
        if cdk_replaced is not None:
            _ACCOUNT_HEALTH_PROGRESS["cdk_replaced"] = max(0, int(cdk_replaced))
        if cdk_unresolved is not None:
            _ACCOUNT_HEALTH_PROGRESS["cdk_unresolved"] = max(0, int(cdk_unresolved))


def _account_health_progress_snapshot(*, include_updates: bool = True) -> dict[str, Any]:
    _refresh_account_health_resource_state()
    with _ACCOUNT_HEALTH_PROGRESS_LOCK:
        snapshot = dict(_ACCOUNT_HEALTH_PROGRESS)
        updates = snapshot.get("updates")
        if include_updates and isinstance(updates, dict):
            # Copy while holding the lock so JSON serialization never races a
            # worker adding the next completed row.
            snapshot["updates"] = dict(updates)
        else:
            snapshot["updates"] = {}
        return snapshot


def _persist_reauthenticated_account(
    account: dict[str, Any], tokens: dict[str, Any]
) -> dict[str, Any]:
    from common.account_health import check_account
    from common.account_store import upsert_account_record

    access_token = str(tokens.get("access_token") or "").strip()
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    if not access_token:
        raise RuntimeError("重新登录未返回 AT")
    refreshed = {
        **account,
        "access_token": access_token,
        "refresh_token": refresh_token or str(account.get("refresh_token") or ""),
        "id_token": str(tokens.get("id_token") or account.get("id_token") or "").strip(),
        "session_token": str(tokens.get("session_token") or account.get("session_token") or "").strip(),
        "session": tokens.get("session") or account.get("session"),
        "last_reauthenticated_at": _now_iso(),
        "last_token_refresh_at": _now_iso(),
        "last_token_refresh_error": "",
        "last_reauth_error": "",
        "last_reauth_failed_at": "",
        "reauth_blocked_reason": "",
        "health_status": "unknown",
        "health_error": "",
    }
    for key in (
        "plan_type",
        "type",
        "browser_type",
        "tls_impersonate",
        "user_agent",
        "sec_ch_ua",
        "sec_ch_ua_full_version_list",
        "platform_version",
        "accept_language",
        "locale",
        "timezone",
        "timezone_offset_min",
        "protocol_screen",
        "protocol_hardware_concurrency",
        "protocol_device_memory",
        "protocol_platform",
        "protocol_webgl_vendor",
        "protocol_webgl_renderer",
        "device_id",
        "protocol_profile_id",
    ):
        if key in tokens:
            refreshed[key] = tokens[key]
    checked = check_account(refreshed, persist=False, path=ACCOUNTS_FILE)
    if normalized_health_status(checked) not in {"alive", "limited"}:
        detail = str(checked.get("health_error") or "重新登录后的凭证测活失败")
        raise RuntimeError(detail[:400])
    return upsert_account_record(checked, ACCOUNTS_FILE)


def _persist_reauthentication_failure(
    account: dict[str, Any],
    error: BaseException | str,
    *,
    mode: str = "",
    step: str = "",
) -> dict[str, Any]:
    detail = str(error or "重新登录失败").strip()[:400]
    lowered = detail.lower()
    previous_health = normalized_health_status(account)
    now = _now_iso()
    deactivated = (
        "account_deactivated" in lowered
        or str(account.get("reauth_blocked_reason") or "").strip().lower()
        == "account_deactivated"
    )
    transport_error = is_proxy_transport_error(detail)

    if deactivated:
        health_status = "invalid"
        status = "已封号"
        health_error = "account_deactivated: account has been deleted or deactivated"
        blocked_reason = "account_deactivated"
    elif transport_error:
        health_status = "error"
        status = "异常"
        health_error = detail
        blocked_reason = ""
    else:
        health_status = "invalid" if previous_health == "invalid" else "reauth_required"
        status = "异常"
        health_error = detail
        blocked_reason = ""

    failed = {
        **account,
        "status": status,
        "health_status": health_status,
        "health_checked_at": now,
        "health_error": health_error,
        "last_reauth_error": detail,
        "last_reauth_failed_at": now,
        "last_reauth_mode": str(mode or "").strip().lower(),
        "last_reauth_step": str(step or "").strip()[:160],
        "reauth_blocked_reason": blocked_reason,
        "updated_at": now,
    }
    if deactivated and previous_health != "invalid":
        failed["last_invalid_at"] = now
    return upsert_account_record(failed, ACCOUNTS_FILE)


def _reauth_proxy(account: dict[str, Any], cfg: dict[str, Any]) -> str:
    proxy_cfg = cfg.get("proxy") if isinstance(cfg.get("proxy"), dict) else {}
    proxy = str(account.get("proxy") or "").strip()
    if not proxy and proxy_cfg.get("enabled"):
        proxy = str(proxy_cfg.get("url") or "").strip()
    return proxy


def _normalize_reauth_mode(value: Any) -> str:
    mode = str(value or "protocol").strip().lower()
    if mode not in _ACCOUNT_REAUTH_MODES:
        raise ValueError("重新登录方式只支持 browser 或 protocol")
    return mode


def _account_requires_protocol_reauth(account: dict[str, Any]) -> bool:
    """Invalidated sessions must recover through stored credentials."""
    state = normalized_health_status(account)
    if state in {"invalid", "reauth_required"}:
        return True
    error = str(account.get("health_error") or "").strip().lower()
    return "token invalidated" in error or "session has expired" in error


def _protocol_reauth_credentials_ready(account: dict[str, Any]) -> bool:
    return bool(
        str(account.get("password") or "").strip()
        and str(account.get("totp_secret") or "").strip()
    )


def _reauthenticate_account_sync(
    account: dict[str, Any],
    mode: str = "protocol",
    stop_check=None,
) -> dict[str, Any]:
    from mail import update_mailbox_states
    from register.flow import register_one as browser_register_one
    from register.log import _LOG_SINK
    from register.protocol import (
        login_one as protocol_login_one,
        login_with_password_totp_one as protocol_credential_login_one,
    )
    from register.sessions import JobCancelled
    from proxy_util import (
        is_proxy_transport_error,
        normalize_proxy_url,
        proxy_brief,
        resolve_proxy_pool,
    )

    requested_mode = _normalize_reauth_mode(mode)
    reauth_mode = (
        "protocol"
        if _account_requires_protocol_reauth(account)
        else requested_mode
    )
    stop = stop_check if callable(stop_check) else (lambda: False)
    current_step = ""
    credential_password = ""
    credential_totp = ""

    log_token = None
    try:
        email = str(account.get("email") or "").strip()
        if not email:
            raise RuntimeError("账号缺少邮箱")
        log_token = _LOG_SINK.set(JOB.append_log)
        JOB.append_log(f"[重新登录] 开始 · {email}")
        record_operation_event(
            account,
            "reauth",
            "running",
            message="重新登录开始",
            metadata={"mode": reauth_mode},
        )
        cfg = _read_config()
        mail_cfg = copy.deepcopy(cfg.get("mail") or {})
        registration = cfg.get("registration") if isinstance(cfg.get("registration"), dict) else {}
        proxy = _reauth_proxy(account, cfg)
        proxy_cfg = cfg.get("proxy") if isinstance(cfg.get("proxy"), dict) else {}
        configured_proxies = resolve_proxy_pool(proxy_cfg)
        proxy_candidates: list[str] = []
        normalized_account_proxy = normalize_proxy_url(proxy)
        if normalized_account_proxy:
            proxy_candidates.append(normalized_account_proxy)
        for candidate in configured_proxies:
            normalized = normalize_proxy_url(candidate)
            if normalized and normalized not in proxy_candidates:
                proxy_candidates.append(normalized)
        if not proxy_candidates:
            proxy_candidates = [""]
        provider_type = _mailbox_provider_map(cfg).get(
            email.lower(), str(account.get("provider_type") or "auto")
        )
        credential_password = str(account.get("password") or "").strip()
        credential_totp = str(account.get("totp_secret") or "").strip()
        account_event_sink = _account_operation_event_sink(
            account,
            "reauth",
            secrets=(credential_password, credential_totp),
        )
        if reauth_mode == "protocol" and (not credential_password or not credential_totp):
            raise RuntimeError("协议模式重新登录要求账号同时具备已保存密码和 TOTP 2FA")
        JOB.append_log(
            f"[重新登录] 准备 · {email} · 方式={reauth_mode} · "
            f"密码={'已保存' if credential_password else '缺失'} · "
            f"TOTP={'已保存' if credential_totp else '缺失'} · "
            f"代理={proxy_brief(proxy) if proxy else 'direct'}"
        )
        timeout = max(90, int(registration.get("timeout") or 180))
        if reauth_mode != "protocol":
            update_mailbox_states([email], action="release")
        reauth_error = ""

        def capture_reauth_result(_index, _status=None, **fields):
            nonlocal current_step, reauth_error
            detail_step = str(fields.get("step") or "").strip()
            if detail_step:
                current_step = detail_step
                JOB.append_log(f"[重新登录] 步骤 · {email} · {detail_step}")
            detail = str(fields.get("error") or "").strip()
            if reauth_mode != "protocol":
                # Keep the browser callback on the same bounded sink as the
                # protocol path.  The outer operation owns the terminal row;
                # this sink records at most the first running marker.
                account_event_sink(
                    {
                        "status": str(_status or "running"),
                        "step": detail_step,
                        "error": detail,
                    }
                )
            if detail:
                if detail != reauth_error:
                    if is_proxy_transport_error(detail):
                        JOB.append_log(
                            f"[重新登录] 代理连接失败 · {email} · "
                            "当前代理不可用，准备切换下一条代理"
                        )
                    else:
                        JOB.append_log(
                            f"[重新登录] 步骤失败 · {email} · "
                            f"当前步骤={current_step or '未知'} · {detail[:400]}"
                        )
                reauth_error = detail

        retryable_markers = (
            "no_email_input",
            "goto_failed",
            "stuck_on_verification",
            "stuck_on_login",
            "passwordless_switch_failed",
            "otp_not_delivered",
            "no_code",
            "cf_blocked",
            "代理出口 ip 检测失败",
            "host unreachable",
            "connection reset",
            "connection refused",
            "max retries exceeded",
            "timeout",
            "targetclosed",
            "execution context was destroyed",
            "ns_binding_aborted",
        )
        reauth_retries = max(
            0,
            min(1, int(registration.get("reauth_retries", 1) or 0)),
        )
        proxy_retry_limit = max(
            reauth_retries,
            min(5, max(0, len(set(proxy_candidates)) - 1)),
        )
        tokens = None
        failed_proxies: set[str] = set()
        for attempt in range(proxy_retry_limit + 1):
            if stop():
                raise JobCancelled("stop requested")
            if failed_proxies and len(failed_proxies) >= len(set(proxy_candidates)):
                break
            reauth_error = ""
            current_step = ""
            if failed_proxies:
                available = [item for item in proxy_candidates if item not in failed_proxies]
                proxy = available[0] if available else ""
            else:
                proxy = proxy_candidates[0]
            JOB.append_log(
                f"[重新登录] 尝试 {attempt + 1}/{proxy_retry_limit + 1} · "
                f"{email} · {'协议' if reauth_mode == 'protocol' else '浏览器'}请求开始 · "
                f"代理={proxy_brief(proxy) if proxy else 'direct'}"
            )
            if reauth_mode == "protocol":
                tokens = protocol_credential_login_one(
                    1,
                    email=email,
                    password=credential_password,
                    totp_secret=credential_totp,
                    proxy=proxy,
                    stop_check=stop,
                    task_event=capture_reauth_result,
                    account_log_sink=account_event_sink,
                    error_sink=lambda detail: capture_reauth_result(
                        1, "failed", error=detail
                    ),
                    proxy_config=proxy_cfg,
                )
            else:
                tokens = asyncio.run(
                    browser_register_one(
                        1,
                        1,
                        None,
                        mail_config=mail_cfg,
                        proxy=proxy,
                        fixed_email=email,
                        provider_type=provider_type,
                        timeout=timeout,
                        keep_on_fail=False,
                        stop_check=stop,
                        task_event=capture_reauth_result,
                        c2a_config={},
                        group_id=str(account.get("group_id") or ""),
                        hybrid_mode=True,
                        persist_result=False,
                    )
                )
            if stop():
                raise JobCancelled("stop requested")
            if tokens and str(tokens.get("access_token") or "").strip():
                break
            lowered_error = reauth_error.lower()
            if "account_deactivated" in lowered_error:
                break
            transport_error = is_proxy_transport_error(reauth_error)
            if transport_error and proxy:
                failed_proxies.add(proxy)
            if transport_error:
                if attempt >= proxy_retry_limit or len(failed_proxies) >= len(set(proxy_candidates)):
                    break
                next_proxy = next(
                    (
                        item for item in proxy_candidates
                        if item not in failed_proxies
                    ),
                    "",
                )
                JOB.append_log(
                    f"[重新登录] 代理连接失败 · {email} · "
                    f"已切换代理重试 {attempt + 2}/{proxy_retry_limit + 1} · "
                    f"{proxy_brief(next_proxy) if next_proxy else 'direct'}"
                )
                if reauth_mode != "protocol":
                    update_mailbox_states([email], action="release")
                time.sleep(0.5)
                continue
            retryable = any(marker in lowered_error for marker in retryable_markers)
            if attempt >= reauth_retries or not retryable:
                break
            JOB.append_log(
                f"[重新登录] 临时页面错误，3 秒后重试 "
                f"{attempt + 2}/{proxy_retry_limit + 1} · {reauth_error or 'unknown'}"
            )
            if reauth_mode != "protocol":
                update_mailbox_states([email], action="release")
            time.sleep(3)
        if not tokens or not str(tokens.get("access_token") or "").strip():
            if "account_deactivated" in reauth_error.lower():
                raise RuntimeError("account_deactivated: 已封号")
            detail = reauth_error or "未取得新的访问凭证"
            mode_label = "协议" if reauth_mode == "protocol" else "混合"
            raise RuntimeError(f"{mode_label}模式重新登录失败: {detail[:240]}")
        saved = _persist_reauthenticated_account({**account, "proxy": proxy}, tokens)
        JOB.append_log(f"[重新登录] 成功 · {email} · {reauth_mode}")
        record_operation_event(
            saved,
            "reauth",
            "success",
            message="重新登录成功",
            metadata={"mode": reauth_mode, "proxy": proxy_brief(proxy) if proxy else "direct"},
        )
        return saved
    except JobCancelled:
        email = str(account.get("email") or "").strip() or "未知账号"
        JOB.append_log(f"[重新登录] 已停止 · {email}")
        record_operation_event(
            account,
            "reauth",
            "cancelled",
            message="重新登录已停止",
            metadata={"mode": reauth_mode},
        )
        raise
    except Exception as error:
        email = str(account.get("email") or "").strip() or "未知账号"
        try:
            failed = _persist_reauthentication_failure(
                account,
                error,
                mode=reauth_mode,
                step=current_step,
            )
            if failed.get("reauth_blocked_reason") == "account_deactivated":
                JOB.append_log(f"[重新登录] 已封号 · {failed.get('email') or email}")
        except Exception as persist_error:
            JOB.append_log(
                f"[重新登录] 失败状态保存异常 · {email} · {str(persist_error)[:240]}"
            )
        JOB.append_log(f"[重新登录] 失败 · {email} · {str(error)[:300]}")
        record_operation_event(
            account,
            "reauth",
            "failed",
            message="重新登录失败",
            error=error,
            step=current_step,
            metadata={"mode": reauth_mode},
            secrets=(credential_password, credential_totp),
        )
        raise
    finally:
        if log_token is not None:
            _LOG_SINK.reset(log_token)


def _repair_account_security_sync(
    account: dict[str, Any],
    stop_check=None,
    *,
    force_password_confirmation: bool = False,
) -> dict[str, Any]:
    from mail import update_mailbox_states
    from register.log import _LOG_SINK
    from register.protocol import repair_security_one
    from register.protocol_runtime import ProtocolRuntimeSettings
    from register.sessions import JobCancelled
    from proxy_util import (
        is_proxy_transport_error,
        normalize_proxy_url,
        proxy_brief,
        resolve_proxy_pool,
    )

    stop = stop_check if callable(stop_check) else (lambda: False)
    log_token = None
    try:
        email = str(account.get("email") or "").strip()
        if not email:
            raise RuntimeError("账号缺少邮箱")
        log_token = _LOG_SINK.set(JOB.append_log)
        JOB.append_log(f"[安全字段补全] 开始 · {email}")
        record_operation_event(
            account,
            "security_repair",
            "running",
            message="安全字段补全开始",
        )
        cfg = _read_config()
        mail_cfg = copy.deepcopy(cfg.get("mail") or {})
        registration = (
            cfg.get("registration")
            if isinstance(cfg.get("registration"), dict)
            else {}
        )
        proxy = _reauth_proxy(account, cfg)
        proxy_cfg = cfg.get("proxy") if isinstance(cfg.get("proxy"), dict) else {}
        proxy_candidates: list[str] = []
        for candidate in [proxy, *resolve_proxy_pool(proxy_cfg)]:
            normalized = normalize_proxy_url(candidate)
            if normalized and normalized not in proxy_candidates:
                proxy_candidates.append(normalized)
        if not proxy_candidates:
            proxy_candidates = [""]
        # One initial route plus four transport fallbacks is enough to escape a
        # bad exit without turning a business/authentication failure into a loop.
        proxy_candidates = proxy_candidates[:5]
        provider_type = _mailbox_provider_map(cfg).get(
            email.lower(),
            str(account.get("provider_type") or "auto"),
        )
        mail_cfg["wait_timeout"] = max(
            10,
            int(
                registration.get("protocol_login_otp_timeout")
                or registration.get("protocol_otp_timeout")
                or mail_cfg.get("wait_timeout")
                or 90
            ),
        )
        mail_cfg["wait_chunk"] = max(
            30,
            int(
                registration.get("protocol_login_poll_chunk")
                or registration.get("protocol_otp_poll_chunk")
                or 60
            ),
        )
        mail_cfg["wait_chunk"] = min(60, mail_cfg["wait_chunk"])
        mail_cfg["request_timeout"] = max(
            15,
            int(mail_cfg.get("request_timeout") or 30),
        )
        protocol_runtime = ProtocolRuntimeSettings.from_config(registration)
        current = dict(account)

        def capture_event(_index, _status=None, **fields):
            step_text = str(fields.get("step") or "").strip()
            if step_text:
                JOB.append_log(f"[安全字段补全] {step_text}")

        def persist_partial(fields: dict[str, Any]) -> None:
            current.update(fields)
            current["updated_at"] = _now_iso()
            upsert_account_record(current, ACCOUNTS_FILE)

        update_mailbox_states(
            [email],
            action="release",
            states=["failed", "in_use"],
        )
        result: dict[str, Any] | None = None
        last_error: Exception | None = None
        successful_proxy = proxy_candidates[0]
        account_event_sink = _account_operation_event_sink(
            current,
            "security_repair",
            secrets=(
                str(current.get("password") or "").strip(),
                str(current.get("totp_secret") or "").strip(),
            ),
        )
        for attempt, candidate in enumerate(proxy_candidates, start=1):
            if stop():
                raise JobCancelled("stop requested")
            try:
                result = repair_security_one(
                    1,
                    account=current,
                    mail_config=mail_cfg,
                    proxy=candidate,
                    provider_type=provider_type,
                    stop_check=stop,
                    task_event=capture_event,
                    account_log_sink=account_event_sink,
                    proxy_config=proxy_cfg,
                    partial_sink=persist_partial,
                    protocol_runtime=protocol_runtime,
                    force_password_confirmation=force_password_confirmation,
                )
                successful_proxy = candidate
                break
            except JobCancelled:
                raise
            except Exception as error:
                last_error = error
                if not is_proxy_transport_error(error) or attempt >= len(proxy_candidates):
                    raise
                next_proxy = proxy_candidates[attempt]
                JOB.append_log(
                    f"[安全字段补全] 代理传输失败 · {email} · "
                    f"切换线路 {attempt + 1}/{len(proxy_candidates)} · "
                    f"{proxy_brief(next_proxy) if next_proxy else 'direct'}"
                )
                update_mailbox_states(
                    [email],
                    action="release",
                    states=["failed", "in_use"],
                )
                time.sleep(0.5)
        if result is None:
            raise last_error or RuntimeError("安全字段补全未返回结果")
        current.update(result)
        if successful_proxy:
            current["proxy"] = successful_proxy
        current["registration_state"] = "complete"
        current["updated_at"] = _now_iso()
        saved = upsert_account_record(current, ACCOUNTS_FILE)
        JOB.append_log(f"[安全字段补全] 成功 · {email}")
        record_operation_event(
            saved,
            "security_repair",
            "success",
            message="安全字段补全成功",
            metadata={"proxy": proxy_brief(successful_proxy) if successful_proxy else "direct"},
        )
        return saved
    except JobCancelled:
        email = str(account.get("email") or "").strip() or "未知账号"
        JOB.append_log(f"[安全字段补全] 已停止 · {email}")
        record_operation_event(
            account,
            "security_repair",
            "cancelled",
            message="安全字段补全已停止",
        )
        raise
    except Exception as error:
        email = str(account.get("email") or "").strip() or "未知账号"
        JOB.append_log(f"[安全字段补全] 失败 · {email} · {str(error)[:400]}")
        record_operation_event(
            account,
            "security_repair",
            "failed",
            message="安全字段补全失败",
            error=error,
        )
        raise
    finally:
        if log_token is not None:
            _LOG_SINK.reset(log_token)


_ACCOUNT_SECURITY_OPERATIONS = frozenset(
    {"reauth", "security_repair", "password_change", "totp_change"}
)
_SECURITY_PASSWORD_MIN_LENGTH = 12
_SECURITY_PASSWORD_RANDOM_LENGTH = 16
_SECURITY_PASSWORD_MODES = frozenset({"custom", "random"})


def _normalize_security_password_mode(value: Any) -> str:
    mode = str(value or "custom").strip().lower().replace("-", "_")
    mode = {
        "generated": "random",
        "auto": "random",
        "automatic": "random",
        "manual": "custom",
    }.get(mode, mode)
    if mode not in _SECURITY_PASSWORD_MODES:
        raise ValueError("密码模式只支持 random 或 custom")
    return mode


def _generate_security_password(length: int = _SECURITY_PASSWORD_RANDOM_LENGTH) -> str:
    """Generate a strong per-account password without putting it in job state."""
    size = max(_SECURITY_PASSWORD_MIN_LENGTH, int(length or _SECURITY_PASSWORD_RANDOM_LENGTH))
    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%"),
    ]
    alphabet = string.ascii_letters + string.digits + "!@#$%"
    value = required + [secrets.choice(alphabet) for _ in range(size - len(required))]
    secrets.SystemRandom().shuffle(value)
    return "".join(value)


def _normalize_account_security_operation(value: Any) -> str:
    operation = str(value or "reauth").strip().lower().replace("-", "_")
    aliases = {
        "change_password": "password_change",
        "password_reset": "password_change",
        "password": "password_change",
        "change_totp": "totp_change",
        "change_2fa": "totp_change",
        "2fa_change": "totp_change",
        "mfa_change": "totp_change",
        "totp": "totp_change",
        "mfa": "totp_change",
        "security_repair": "security_repair",
        "repair": "security_repair",
    }
    operation = aliases.get(operation, operation)
    if operation not in _ACCOUNT_SECURITY_OPERATIONS:
        raise ValueError("不支持的账号批次操作")
    return operation


def _account_security_operation_label(operation: str) -> str:
    raw = str(operation or "").strip().lower().replace("-", "_")
    operation = {
        "change_password": "password_change",
        "password_reset": "password_change",
        "password": "password_change",
        "change_totp": "totp_change",
        "change_2fa": "totp_change",
        "2fa_change": "totp_change",
        "mfa_change": "totp_change",
        "totp": "totp_change",
        "mfa": "totp_change",
        "repair": "security_repair",
    }.get(raw, raw)
    return {
        "reauth": "重新登录",
        "security_repair": "安全字段补全",
        "password_change": "修改密码",
        "totp_change": "修改 2FA",
        "email_rebind": "邮箱换绑",
    }.get(operation, "重新登录")


def _call_security_protocol_wrapper(
    function: Any,
    *,
    index: int,
    kwargs: dict[str, Any],
) -> Any:
    """Call a protocol wrapper while tolerating older/newer signatures.

    The protocol module is used by CLI integrations too, and a few deployed
    versions expose the same operation under a different keyword set. Filter
    only unsupported keywords; values such as passwords and TOTP secrets never
    enter the batch state object or its JSON snapshots.
    """
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        signature = None
    call_kwargs = dict(kwargs)
    accepts_var_kw = bool(
        signature
        and any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
    )
    if signature is not None and not accepts_var_kw:
        call_kwargs = {
            key: value
            for key, value in call_kwargs.items()
            if key in signature.parameters
        }
    # Current one-account wrappers take an index first. Class-method fallbacks
    # place ``index`` last, so pass it by keyword in that shape.
    if signature is not None and "index" in signature.parameters:
        parameter = signature.parameters["index"]
        if parameter.kind == inspect.Parameter.KEYWORD_ONLY:
            return function(index=index, **call_kwargs)
        ordered = list(signature.parameters)
        if ordered.index("index") > 0:
            call_kwargs["index"] = index
            return function(**call_kwargs)
    if signature is not None and "index" not in signature.parameters:
        return function(**call_kwargs)
    return function(index, **call_kwargs)


def _resolve_security_protocol_wrapper(
    module: Any,
    names: tuple[str, ...],
) -> Any:
    for name in names:
        candidate = getattr(module, name, None)
        if callable(candidate):
            return candidate
    joined = ", ".join(names)
    raise RuntimeError(f"协议层未提供安全操作接口: {joined}")


def _security_proxy_candidates(account: dict[str, Any], cfg: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    from proxy_util import normalize_proxy_url, resolve_proxy_pool

    proxy_cfg = cfg.get("proxy") if isinstance(cfg.get("proxy"), dict) else {}
    configured = [
        _reauth_proxy(account, cfg),
        *resolve_proxy_pool(proxy_cfg),
    ]
    candidates: list[str] = []
    for candidate in configured:
        normalized = normalize_proxy_url(candidate)
        if normalized not in candidates:
            candidates.append(normalized)
    if not candidates:
        candidates = [""]
    return candidates[:5], proxy_cfg


def _persist_security_operation_failure(
    account: dict[str, Any],
    operation: str,
    error: BaseException | str,
) -> dict[str, Any]:
    detail = str(error or "账号安全操作失败").strip()[:400]
    now = _now_iso()
    failed = {
        **account,
        "last_security_operation": operation,
        "last_security_error": detail,
        "last_security_failed_at": now,
        "updated_at": now,
    }
    return upsert_account_record(failed, ACCOUNTS_FILE)


def _redact_security_detail(detail: Any, account: dict[str, Any], new_password: str = "") -> str:
    text = str(detail or "账号安全操作失败")
    secrets_to_hide = [
        str(account.get("password") or "").strip(),
        str(account.get("totp_secret") or "").strip(),
        str(new_password or "").strip(),
    ]
    for secret in secrets_to_hide:
        if secret:
            text = text.replace(secret, "[已隐藏]")
    return text[:400]


def _account_operation_event_sink(
    account: dict[str, Any],
    operation: str,
    *,
    secrets: tuple[Any, ...] = (),
):
    """Build a bounded structured sink used by protocol maintenance flows.

    Protocol callbacks can emit dozens of ``running`` step updates. The live
    task stream retains those details; the durable per-account panel keeps the
    first running marker while the outer operation records its terminal result.
    """
    seen_statuses: set[str] = set()
    terminal_statuses = {
        "success",
        "failed",
        "cancelled",
        "skipped",
        "error",
        "complete",
        "completed",
    }

    def sink(event: dict[str, Any] | None) -> None:
        if not isinstance(event, dict):
            return
        status = str(event.get("status") or "info").strip().lower()
        if status in terminal_statuses:
            # The surrounding web operation writes the terminal summary with
            # its persisted result/error context. Avoid a second callback row.
            return
        if status == "running":
            if status in seen_statuses:
                return
            seen_statuses.add(status)
        else:
            # Detailed step/info callbacks remain in the transient task log.
            return
        record_operation_event(
            account,
            operation,
            status,
            message=event.get("message") or "",
            step=event.get("step") or "",
            error=event.get("error") or "",
            metadata={"index": event.get("index")} if event.get("index") is not None else {},
            secrets=secrets,
            at=str(event.get("at") or "") or None,
        )

    return sink


def _run_security_protocol_operation(
    account: dict[str, Any],
    operation: str,
    *,
    new_password: str = "",
    password_mode: str = "custom",
    stop_check=None,
) -> dict[str, Any]:
    """Run one password/2FA mutation and persist only the resulting fields."""
    from register.log import _LOG_SINK
    import register.protocol as protocol
    from register.sessions import JobCancelled
    from register.protocol_runtime import ProtocolRuntimeSettings
    from proxy_util import is_proxy_transport_error, proxy_brief

    operation = _normalize_account_security_operation(operation)
    if operation not in {"password_change", "totp_change"}:
        raise ValueError("该协议操作只支持修改密码或修改 2FA")
    email = str(account.get("email") or "").strip()
    if not email:
        raise RuntimeError("账号缺少邮箱")
    record_operation_event(
        account,
        operation,
        "running",
        message=f"{_account_security_operation_label(operation)}开始",
    )
    old_password = str(account.get("password") or "").strip()
    old_totp = str(account.get("totp_secret") or "").strip()
    try:
        if operation == "password_change":
            if not old_password or not old_totp:
                raise RuntimeError("修改密码要求账号已保存旧密码和 TOTP 2FA")
            password_mode = _normalize_security_password_mode(password_mode)
            if password_mode == "random":
                new_password = str(new_password or "").strip() or _generate_security_password()
            else:
                new_password = str(new_password or "").strip()
            if len(new_password) < _SECURITY_PASSWORD_MIN_LENGTH:
                raise RuntimeError(
                    f"新密码至少需要 {_SECURITY_PASSWORD_MIN_LENGTH} 个字符"
                )
            if new_password == old_password:
                raise RuntimeError("新密码不能与旧密码相同")
        elif not old_password or not old_totp:
            raise RuntimeError("修改 2FA 要求账号已保存密码和旧 TOTP 2FA")
    except Exception as error:
        record_operation_event(
            account,
            operation,
            "failed",
            message=f"{_account_security_operation_label(operation)}预检失败",
            error=error,
            secrets=(old_password, old_totp, new_password),
        )
        raise

    stop = stop_check if callable(stop_check) else (lambda: False)
    log_token = _LOG_SINK.set(JOB.append_log)
    try:
        cfg = _read_config()
        registration = cfg.get("registration") if isinstance(cfg.get("registration"), dict) else {}
        candidates, proxy_cfg = _security_proxy_candidates(account, cfg)
        runtime = ProtocolRuntimeSettings.from_config(registration)
        event_error = ""
        current_step = ""

        def capture_event(_index, _status=None, **fields):
            nonlocal event_error, current_step
            step_text = str(fields.get("step") or "").strip()
            if step_text:
                current_step = step_text
                JOB.append_log(f"[{_account_security_operation_label(operation)}] 步骤 · {email} · {step_text}")
            detail = str(fields.get("error") or "").strip()
            if detail:
                event_error = detail

        kwargs = {
            "email": email,
            "proxy_config": proxy_cfg,
            "stop_check": stop,
            "task_event": capture_event,
            "account_log_sink": _account_operation_event_sink(
                account,
                operation,
                secrets=(old_password, old_totp, new_password),
            ),
            "error_sink": lambda detail: capture_event(1, "failed", error=detail),
            "protocol_runtime": runtime,
        }
        wrapper: Any | None = None
        if operation == "password_change":
            kwargs.update(
                old_password=old_password,
                new_password=new_password,
                totp_secret=old_totp,
            )
            try:
                wrapper = _resolve_security_protocol_wrapper(
                    protocol,
                    ("change_password_one", "change_password_known_one", "change_password_known"),
                )
            except RuntimeError:
                wrapper = None
        else:
            kwargs.update(password=old_password, totp_secret=old_totp)
            try:
                wrapper = _resolve_security_protocol_wrapper(
                    protocol,
                    ("change_totp_one", "rotate_totp_one", "replace_totp_one", "change_2fa_one"),
                )
            except RuntimeError:
                wrapper = None
        if wrapper is None:
            # Compatibility with protocol builds that expose only the bound
            # PlatformRegistrar methods. The adapter keeps the same wrapper
            # contract used by the batch worker and closes each session.
            method_name = (
                "change_password"
                if operation == "password_change"
                else "change_totp"
            )

            def class_method_adapter(index: int, **call_kwargs: Any) -> dict[str, Any] | None:
                from register.protocol import (
                    PlatformRegistrar,
                    _PROXY_CONFIG,
                    _STOP_CHECK,
                    _TASK_EVENT,
                    _FIXED_EMAIL,
                    activate_runtime,
                )

                stop_callback = call_kwargs.get("stop_check")
                if callable(stop_callback) and stop_callback():
                    raise JobCancelled("stop requested")
                selected_proxy = str(call_kwargs.pop("proxy") or "")
                event_token = _TASK_EVENT.set(call_kwargs.get("task_event"))
                stop_token = _STOP_CHECK.set(stop_callback or (lambda: False))
                proxy_token = _PROXY_CONFIG.set(dict(call_kwargs.get("proxy_config") or {}))
                email_token = _FIXED_EMAIL.set(email)
                runtime_context = activate_runtime(call_kwargs.get("protocol_runtime") or runtime)
                runtime_context.__enter__()
                registrar = None
                try:
                    registrar = PlatformRegistrar(selected_proxy)
                    method = getattr(registrar, method_name, None)
                    if not callable(method) and operation == "totp_change":
                        method = getattr(registrar, "replace_totp", None)
                    if not callable(method):
                        raise RuntimeError(f"协议层未提供安全操作接口: {method_name}")
                    if operation == "password_change":
                        return method(
                            email,
                            old_password,
                            new_password,
                            old_totp,
                            index,
                        )
                    return method(email, old_password, old_totp, index)
                finally:
                    if registrar is not None:
                        registrar.close()
                    _PROXY_CONFIG.reset(proxy_token)
                    _STOP_CHECK.reset(stop_token)
                    _TASK_EVENT.reset(event_token)
                    _FIXED_EMAIL.reset(email_token)
                    runtime_context.__exit__(None, None, None)

            wrapper = class_method_adapter
        result: dict[str, Any] | None = None
        last_error: BaseException | None = None
        for attempt, proxy in enumerate(candidates):
            if stop():
                raise JobCancelled("stop requested")
            event_error = ""
            try:
                JOB.append_log(
                    f"[{_account_security_operation_label(operation)}] 开始 · {email} · "
                    f"线路={proxy_brief(proxy) if proxy else 'direct'}"
                )
                result = _call_security_protocol_wrapper(
                    wrapper,
                    index=1,
                    kwargs={"proxy": proxy, **kwargs},
                )
                if isinstance(result, dict):
                    break
                raise RuntimeError(event_error or "协议安全操作未返回结果")
            except JobCancelled:
                raise
            except Exception as error:
                last_error = error
                if not is_proxy_transport_error(error) or attempt >= len(candidates) - 1:
                    raise
                JOB.append_log(
                    f"[{_account_security_operation_label(operation)}] 代理传输失败 · {email} · "
                    f"切换线路 {attempt + 2}/{len(candidates)}"
                )
                time.sleep(0.5)
        if not isinstance(result, dict):
            raise last_error or RuntimeError("协议安全操作未返回结果")

        saved = dict(account)
        # Keep only non-secret protocol metadata that is useful for the next
        # session. Explicit credential fields are set below from trusted input.
        for key in (
            "access_token", "refresh_token", "id_token", "session_token",
            "account_id", "session", "cookies", "profile", "plan_type",
            "expires_at", "oauth_client_id", "mfa_factor_id", "mfa_factor_type",
            "device_id", "protocol_profile_id", "browser_type", "tls_impersonate",
            "user_agent", "sec_ch_ua", "sec_ch_ua_full_version_list", "locale",
            "timezone", "timezone_offset_min", "protocol_screen",
            "protocol_hardware_concurrency", "protocol_device_memory",
            "protocol_platform", "protocol_webgl_vendor", "protocol_webgl_renderer",
        ):
            if key in result and result[key] not in (None, "", {}):
                saved[key] = result[key]
        if operation == "password_change":
            saved.update(
                password=new_password,
                password_set=True,
                password_verified_at=_now_iso(),
            )
        else:
            replacement = str(
                result.get("totp_secret")
                or result.get("new_totp_secret")
                or result.get("secret")
                or ""
            ).strip()
            if not replacement:
                raise RuntimeError("协议修改 2FA 未返回新的 TOTP 密钥")
            saved.update(
                totp_secret=replacement,
                mfa_enabled=True,
                totp_verified_at=_now_iso(),
            )
        if proxy:
            saved["proxy"] = proxy
        saved.update(
            last_security_operation=operation,
            last_security_error="",
            last_security_failed_at="",
            updated_at=_now_iso(),
            # A successful mutation proves the credential path, but an old
            # invalid/reauth state still needs a health probe before it can be
            # advertised as usable. Healthy rows retain their classification.
            health_status=(
                "unknown"
                if normalized_health_status(account)
                in {"invalid", "reauth_required", "error"}
                else str(account.get("health_status") or "unknown")
            ),
            health_error="",
        )
        persisted = upsert_account_record(saved, ACCOUNTS_FILE)
        JOB.append_log(f"[{_account_security_operation_label(operation)}] 成功 · {email}")
        record_operation_event(
            persisted,
            operation,
            "success",
            message=f"{_account_security_operation_label(operation)}成功",
            metadata={"proxy": proxy_brief(proxy) if proxy else "direct"},
            secrets=(old_password, old_totp, new_password),
        )
        return persisted
    except JobCancelled:
        record_operation_event(
            account,
            operation,
            "cancelled",
            message=f"{_account_security_operation_label(operation)}已停止",
            secrets=(old_password, old_totp, new_password),
        )
        raise
    except Exception as error:
        safe_error = _redact_security_detail(error, account, new_password)
        try:
            _persist_security_operation_failure(account, operation, safe_error)
        except Exception:
            pass
        JOB.append_log(
            f"[{_account_security_operation_label(operation)}] 失败 · {email} · {safe_error[:300]}"
        )
        record_operation_event(
            account,
            operation,
            "failed",
            message=f"{_account_security_operation_label(operation)}失败",
            error=safe_error,
            step=current_step,
            secrets=(old_password, old_totp, new_password),
        )
        raise
    finally:
        _LOG_SINK.reset(log_token)


def _change_account_password_sync(
    account: dict[str, Any],
    new_password: str,
    stop_check=None,
    *,
    password_mode: str = "custom",
) -> dict[str, Any]:
    return _run_security_protocol_operation(
        account,
        "password_change",
        new_password=new_password,
        password_mode=password_mode,
        stop_check=stop_check,
    )


def _change_account_totp_sync(
    account: dict[str, Any],
    stop_check=None,
) -> dict[str, Any]:
    return _run_security_protocol_operation(
        account,
        "totp_change",
        stop_check=stop_check,
    )


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
        return _account_security_operation_label(operation)

    @classmethod
    def _account_item(
        cls,
        record: dict[str, Any],
        operation: str = "reauth",
    ) -> dict[str, Any]:
        return {
            "key": _account_key(record),
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
        operation = _normalize_account_security_operation(operation)
        requested_mode = _normalize_reauth_mode(mode)
        reauth_mode = (
            "protocol"
            if any(_account_requires_protocol_reauth(record) for record in records)
            else requested_mode
        )
        operation_label = self._operation_label(operation)
        requested_workers = min(len(records), max(1, int(concurrency or 3)))
        budget_workers, resource_mode = _account_reauth_resource_budget(
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
                        finished_at=_now_iso(),
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
        JOB.append_log(f"[{label}批次] 已请求停止")
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
                    finished_at=_now_iso(),
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
                            finished_at=_now_iso(),
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
                finished_at=_now_iso(),
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
        operation = _normalize_account_security_operation(operation)
        options = operation_options if isinstance(operation_options, dict) else {}
        if operation == "password_change":
            password_mode = _normalize_security_password_mode(
                options.get("password_mode")
            )
            new_password = str(options.get("new_password") or "")
            if password_mode == "random":
                # Generate at the account boundary so every account receives
                # a distinct credential while the job snapshot stays secret-free.
                new_password = _generate_security_password()
                # The generated value is now explicit; the operation layer
                # should validate and persist it without generating twice.
                password_mode = "custom"
            try:
                return _change_account_password_sync(
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
                    _redact_security_detail(error, account, new_password)
                ) from error
        if operation == "totp_change":
            return _change_account_totp_sync(
                account,
                stop_check=stop_event.is_set,
            )
        if operation == "security_repair":
            return _repair_account_security_sync(
                account,
                stop_check=stop_event.is_set,
                force_password_confirmation=bool(
                    options.get("force_password_confirmation")
                ),
            )
        try:
            return _reauthenticate_account_sync(
                account,
                mode=mode,
                stop_check=stop_event.is_set,
            )
        except TypeError as error:
            # Keep compatibility with older integrations that monkey-patched
            # the one-argument helper before mode selection was added.
            if "unexpected keyword argument" not in str(error):
                raise
            return _reauthenticate_account_sync(account)

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
                started_at=_now_iso(),
                current=f"准备启动 {concurrency} 路{self._operation_label(operation)}",
            )
        operation_label = self._operation_label(operation)
        JOB.append_log(
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
                    JOB.append_log(
                        f"[{operation_label}批次] {index + 1}/{len(records)} · "
                        f"已停止，未执行 · {label}"
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
                            started_at=_now_iso(),
                        )
                    current_items = job.setdefault("current_items", [])
                    if isinstance(current_items, list):
                        current_items.append(label)
                        job["current"] = "；".join(
                            str(value) for value in current_items
                        )
                JOB.append_log(
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
                        message = _redact_security_detail(
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
                    public = account_public_view(result)
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
                    JOB.append_log(
                        f"[{operation_label}批次] {index + 1}/{len(records)} · "
                        f"成功 · {label}"
                    )
                elif terminal_status == "cancelled":
                    JOB.append_log(
                        f"[{operation_label}批次] {index + 1}/{len(records)} · "
                        f"已停止 · {label}"
                    )
                else:
                    JOB.append_log(
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
            with ThreadPoolExecutor(
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
                    finished_at=_now_iso(),
                    finished_epoch=time.time(),
                )
                self.operation_options.pop(job_id, None)
            JOB.append_log(
                f"[{operation_label}批次] {'已停止' if stopped else '完成'} · "
                f"方式={mode} · 并发={concurrency} · 成功 {success} · "
                f"失败 {failed} · 已停止 {cancelled}"
            )
        except Exception as error:
            message = str(error or "重新登录批次异常")[:400]
            if stop_event.is_set():
                self._cancel_remaining(job_id, 0)
                self._finish_stopped(job_id)
                JOB.append_log(
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
                        finished_at=_now_iso(),
                        finished_epoch=time.time(),
                    )
                self.operation_options.pop(job_id, None)
            JOB.append_log(f"[{operation_label}批次] 异常结束 · {message}")

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


ACCOUNT_REAUTH_JOBS = AccountReauthBatchJobs()


def _cdk_admin_row(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: record.get(key)
        for key in (
            "id",
            "code",
            "code_hint",
            "email",
            "account_id",
            "provider_type",
            "group_id",
            "group_name",
            "status",
            "created_at",
            "updated_at",
            "redeemed_at",
            "last_access_at",
            "expires_at",
            "replacement_count",
            "last_replaced_at",
        )
    }


def _cdk_admin_payload(group_id: str = "") -> dict[str, Any]:
    rows = ensure_plain_codes(CDKS_FILE)
    groups = _groups_with_counts()
    selected = str(group_id or "").strip()
    eligible: dict[str, Any] = {}
    if selected:
        try:
            _bindings, eligible = _eligible_cdk_bindings(selected)
        except KeyError:
            eligible = {}
    items = [_cdk_admin_row(item) for item in rows]
    items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {
        "ok": True,
        "items": items,
        "summary": cdk_summary(rows),
        "groups": groups,
        "eligibility": eligible,
    }


def _filter_mailbox_text(
    raw: str, ptype: str, remove_emails: set[str]
) -> tuple[str, int]:
    kept: list[str] = []
    removed = 0
    if ptype in {"domain_api", "maddy_webui", "fastmail_api"}:
        parser = parse_domain_mailboxes
    else:
        parser = (
            parse_icloud_credentials if ptype == "icloud_api" else parse_outlook_credentials
        )
    valid = {item["email"].strip().lower() for item in parser(raw)}
    for line in str(raw or "").splitlines():
        if ptype in {"domain_api", "maddy_webui", "fastmail_api"}:
            email = line.split("----", 1)[0].strip().lower()
        else:
            email = line.split("----", 1)[0].strip().lower() if "----" in line else ""
        if email and email in remove_emails and email in valid:
            removed += 1
            continue
        kept.append(line)
    return (
        "\n".join(kept).strip()
        + ("\n" if kept and any(x.strip() for x in kept) else ""),
        removed,
    )


def _serialize_mailbox_credential(item: dict[str, str], ptype: str) -> str:
    if ptype == "icloud_api":
        return f"{item['email']}----{item['api_key']}"
    if ptype in {"domain_api", "maddy_webui", "fastmail_api"}:
        remote_id = str(item.get("id") or "").strip()
        return f"{item['email']}----{remote_id}" if remote_id else item["email"]
    return "----".join(
        (
            item["email"],
            item.get("password", ""),
            item["client_id"],
            item["refresh_token"],
        )
    )


def _merge_mailbox_credentials(
    existing: str, incoming: str, ptype: str, *, append: bool = True
) -> tuple[str, dict[str, int]]:
    if ptype in {"domain_api", "maddy_webui", "fastmail_api"}:
        parser = parse_domain_mailboxes
    else:
        parser = (
            parse_icloud_credentials if ptype == "icloud_api" else parse_outlook_credentials
        )
    old_items = parser(existing) if append or not str(incoming or "").strip() else []
    new_items = parser(incoming)
    merged: dict[str, dict[str, str]] = {
        item["email"].strip().lower(): item for item in old_items
    }
    added = 0
    updated = 0
    unchanged = 0
    for item in new_items:
        key = item["email"].strip().lower()
        previous = merged.get(key)
        if previous is None:
            added += 1
        elif _serialize_mailbox_credential(
            previous, ptype
        ) == _serialize_mailbox_credential(item, ptype):
            unchanged += 1
        else:
            updated += 1
        merged[key] = item
    lines = [_serialize_mailbox_credential(item, ptype) for item in merged.values()]
    text = "\n".join(lines) + ("\n" if lines else "")
    return text, {
        "imported": len(new_items),
        "added": added,
        "updated": updated,
        "unchanged": unchanged,
        "total": len(merged),
    }


def _delete_mailboxes_from_config(
    cfg: dict[str, Any], body: MailboxDeleteRequest
) -> int:
    want = normalize_provider_type(str(body.provider_type or "auto"))
    if want in {"outlook", "ms", "microsoft"}:
        want = "outlook_token"
    elif want in {"icloud", "apple", "hme", "hide_my_email"}:
        want = "icloud_api"
    elif want in {"", "auto", "any", "all"}:
        want = ""
    selected: dict[str, set[str]] = {
        "outlook_token": set(),
        "icloud_api": set(),
        "domain_api": set(),
        "maddy_webui": set(),
        "fastmail_api": set(),
    }
    for item in body.items or []:
        email = str(item.get("email") or "").strip().lower()
        ptype = normalize_provider_type(str(item.get("provider_type") or want or ""))
        if ptype in {"outlook", "ms", "microsoft"}:
            ptype = "outlook_token"
        elif ptype in {"icloud", "apple", "hme", "hide_my_email"}:
            ptype = "icloud_api"
        elif ptype in {"domain", "domain_mail", "custom_domain", "freemail", "xbovo_mail", "iding_mail"}:
            ptype = "domain_api"
        elif ptype in {"maddy", "maddy_webui_api", "maddy_api", "maddy_mail"}:
            ptype = "maddy_webui"
        elif ptype in {"fastmail", "fastmail_mail", "mailcode", "mailcode_api"}:
            ptype = "fastmail_api"
        if email and ptype in selected:
            selected[ptype].add(email)

    removed = 0
    removed_emails: set[str] = set()
    for provider in _providers(cfg):
        ptype = normalize_provider_type(str(provider.get("type") or ""))
        if ptype not in CANONICAL_PROVIDER_TYPES:
            continue
        if want and ptype != want:
            continue
        raw = str(provider.get("mailboxes") or "")
        if ptype in {"domain_api", "maddy_webui", "fastmail_api"}:
            pool = parse_domain_mailboxes(raw)
            targets = selected.get(ptype, set())
            if body.delete_all:
                targets = {
                    str(item.get("email") or "").strip().lower()
                    for item in pool
                    if str(item.get("email") or "").strip()
                }
            remote_removed: set[str] = set()
            if ptype == "domain_api" and targets and str(provider.get("username") or "").strip() and str(provider.get("password") or "").strip():
                conf = _make_config(cfg.get("mail") if isinstance(cfg.get("mail"), dict) else {})
                conf["request_timeout"] = min(float(conf.get("request_timeout") or 30), 8.0)
                domain_provider = DomainApiProvider(provider, conf)
                try:
                    for email in sorted(targets):
                        try:
                            if domain_provider.delete_mailbox(email):
                                removed += 1
                                remote_removed.add(email)
                                removed_emails.add(email)
                        except Exception:
                            continue
                finally:
                    domain_provider.close()
            if body.delete_all:
                provider["mailboxes"] = ""
            elif pool:
                new_text, _n = _filter_mailbox_text(raw, ptype, targets)
                provider["mailboxes"] = new_text
                valid = {item["email"].strip().lower() for item in pool}
                local_removed = {email for email in targets if email in valid}
                removed += len(local_removed - remote_removed)
                removed_emails.update(local_removed)
            if ptype in {"maddy_webui", "fastmail_api"} and targets:
                state = _load_state_file()
                state_targets = {
                    email
                    for email in targets
                    if isinstance(state.get(email), dict)
                }
                removed += len(state_targets - removed_emails)
                removed_emails.update(state_targets)
            continue
        pool = (
            parse_icloud_credentials(raw)
            if ptype == "icloud_api"
            else parse_outlook_credentials(raw)
        )
        if body.delete_all:
            removed += len(pool)
            removed_emails.update(item["email"].strip().lower() for item in pool)
            provider["mailboxes"] = ""
        else:
            targets = selected.get(ptype, set())
            new_text, n = _filter_mailbox_text(raw, ptype, targets)
            provider["mailboxes"] = new_text
            removed += n
            valid = {item["email"].strip().lower() for item in pool}
            removed_emails.update(email for email in targets if email in valid)

    if removed_emails:
        # Keep mailbox credentials and reservation state consistent. The mail
        # module owns the shared lock and atomic state-file replacement.
        update_mailbox_states(removed_emails, action="release")
    return removed


def _proxy_pool_preview(cfg: dict[str, Any]) -> dict[str, Any]:
    proxy = cfg.get("proxy") if isinstance(cfg.get("proxy"), dict) else {}
    pool_file = str(proxy.get("pool_file") or "").strip()
    lines: list[str] = []
    if pool_file:
        path = Path(pool_file).expanduser()
        if not path.is_absolute():
            path = (ROOT / path).resolve()
        if path.is_file():
            try:
                lines = [
                    ln.strip()
                    for ln in path.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                    if ln.strip() and not ln.strip().startswith("#")
                ]
            except Exception:
                lines = []
    country_pools = read_registration_proxy_pools()
    selected_country = str(proxy.get("selected_country") or "").upper().strip()
    selected_pool = next(
        (item for item in country_pools["items"] if item["country"] == selected_country),
        None,
    )
    selected_count = int((selected_pool or {}).get("count") or 0)
    mode = str(proxy.get("mode") or "single_proxy")
    return {
        "enabled": bool(proxy.get("enabled")),
        "mode": mode,
        "url": str(proxy.get("url") or ""),
        "resource_url": str(proxy.get("resource_url") or ""),
        "clearance": dict(proxy.get("clearance") or {}) if isinstance(proxy.get("clearance"), dict) else {},
        "pool_file": pool_file,
        "pool_count": selected_count if mode == "country_pool" else len(lines),
        "pool_unique_count": selected_count if mode == "country_pool" else len(set(lines)),
        "rotate_per_task": bool(proxy.get("rotate_per_task", True)),
        "pool_preview": lines[:20],
        "pool_text": "\n".join(lines),
        "selected_country": selected_country,
        "country_pools": country_pools,
    }


def _c2a_public(cfg: dict[str, Any]) -> dict[str, Any]:
    from common.chatgpt2api import c2a_config_from

    c = c2a_config_from(cfg)
    key = str(c.get("key") or "")
    return {
        "enabled": bool(c.get("enabled")),
        "url": str(c.get("url") or ""),
        "key": "",
        "key_masked": _mask_secret(key) if key else "",
        "has_key": bool(key),
        "auto_upload": bool(c.get("auto_upload", True)),
    }


def _public_mail_stats(cfg: dict[str, Any]) -> dict[str, Any]:
    global _MAIL_STATS_CACHE_KEY, _MAIL_STATS_CACHE_AT, _MAIL_STATS_CACHE_VALUE
    mail_cfg = cfg.get("mail") if isinstance(cfg.get("mail"), dict) else {}
    try:
        mail_digest = hashlib.sha256(
            json.dumps(mail_cfg, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
    except Exception:
        mail_digest = str(id(mail_cfg))
    cache_key = (
        mail_digest,
        _file_stamp(CONFIG_FILE),
        _file_stamp(STATE_FILE),
    )
    now = time.monotonic()
    with _MAIL_STATS_CACHE_LOCK:
        if (
            _MAIL_STATS_CACHE_VALUE is not None
            and _MAIL_STATS_CACHE_KEY == cache_key
            and now - _MAIL_STATS_CACHE_AT < 3.0
        ):
            return copy.deepcopy(_MAIL_STATS_CACHE_VALUE)

    # Status and operations can miss their independent response caches at the
    # same time. Allow only one expensive mailbox scan to run, then re-check
    # the short-lived cache before doing any work.
    with _MAIL_STATS_BUILD_LOCK:
        now = time.monotonic()
        with _MAIL_STATS_CACHE_LOCK:
            if (
                _MAIL_STATS_CACHE_VALUE is not None
                and _MAIL_STATS_CACHE_KEY == cache_key
                and now - _MAIL_STATS_CACHE_AT < 3.0
            ):
                return copy.deepcopy(_MAIL_STATS_CACHE_VALUE)
        stats = _mail_stats(cfg)
        providers = stats.get("providers")
        if isinstance(providers, list):
            slim = []
            for p in providers:
                if not isinstance(p, dict):
                    continue
                item = dict(p)
                item.pop("mailboxes", None)
                slim.append(item)
            stats["providers"] = slim
        built_at = time.monotonic()
        with _MAIL_STATS_CACHE_LOCK:
            _MAIL_STATS_CACHE_KEY = cache_key
            _MAIL_STATS_CACHE_AT = built_at
            _MAIL_STATS_CACHE_VALUE = copy.deepcopy(stats)
        return stats


def _warm_panel_caches() -> None:
    """Prime the expensive mailbox/status snapshot after a service restart."""
    try:
        _public_mail_stats(_read_config())
    except Exception as error:
        # Cache warming is opportunistic; the first request can still build a
        # fresh snapshot if a provider or a partially-written data file fails.
        logger.debug("panel cache warm-up skipped: %s", error)


class _LogStream(io.TextIOBase):
    def __init__(self, original, job: JobState):
        self._original = original
        self._job = job
        self._buf = ""
        self._in_transport_cleanup_trace = False

    def write(self, s):
        if not isinstance(s, str):
            s = str(s)
        try:
            self._original.write(s)
        except Exception:
            pass
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            s = line.strip()
            if not s:
                continue
            if s.startswith(
                "Exception ignored in: <function BaseSubprocessTransport.__del__"
            ):
                self._in_transport_cleanup_trace = True
                continue
            if self._in_transport_cleanup_trace:
                transport_trace_line = (
                    s == "Traceback (most recent call last):"
                    or "asyncio/base_subprocess.py" in s
                    or "asyncio/unix_events.py" in s
                    or "asyncio/base_events.py" in s
                    or s in {
                        "self.close()",
                        "proto.pipe.close()",
                        "self.write_eof()",
                        "self._loop.call_soon(self._call_connection_lost, None)",
                        "self._check_closed()",
                        "raise RuntimeError('Event loop is closed')",
                        "RuntimeError: Event loop is closed",
                    }
                )
                if transport_trace_line:
                    if s == "RuntimeError: Event loop is closed":
                        self._in_transport_cleanup_trace = False
                    continue
            if "Future exception was never retrieved" in s:
                continue
            if "Execution context was destroyed" in s:
                continue
            if "Cannot find context with specified id" in s:
                continue
            if "Inspected target navigated or closed" in s:
                continue
            if "TargetClosedError" in s or "Target page, context or browser has been closed" in s:
                continue
            if "LeakWarning: Disabling Cross-Origin-Opener-Policy" in s:
                continue
            if "LeakWarning: When using a proxy" in s:
                continue
            if "i_know_what_im_doing=True" in s:
                continue
            if s.startswith("result = self.fn("):
                continue
            self._job.append_log(line)
        return len(s)

    def flush(self):
        try:
            self._original.flush()
        except Exception:
            pass
        if self._buf.strip():
            self._job.append_log(self._buf)
            self._buf = ""




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

def _job_should_stop() -> bool:
    with JOB.lock:
        return bool(JOB.stop_requested)


def _job_should_pause() -> bool:
    with JOB.lock:
        return bool(JOB.paused)


def _maintenance_config(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    source = cfg if isinstance(cfg, dict) else _read_config()
    raw = source.get("maintenance") if isinstance(source.get("maintenance"), dict) else {}
    return {
        "enabled": bool(raw.get("enabled")),
        "interval_hours": max(1, min(int(raw.get("interval_hours") or 12), 168)),
        "workers": max(1, min(int(raw.get("workers") or 10), 20)),
        "auto_delete_invalid": bool(raw.get("auto_delete_invalid")),
        "webhook_url": str(raw.get("webhook_url") or "").strip(),
        "notify_on_complete": bool(raw.get("notify_on_complete")),
        "notify_on_pool_low": bool(raw.get("notify_on_pool_low")),
        "pool_low_threshold": max(0, int(raw.get("pool_low_threshold") or 3)),
    }


def _maddy_auto_config(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    source = cfg if isinstance(cfg, dict) else _read_config()
    raw = source.get("maddy_auto_register") if isinstance(source.get("maddy_auto_register"), dict) else {}
    reg_cfg = source.get("registration") if isinstance(source.get("registration"), dict) else {}
    engine = str(raw.get("engine") or reg_cfg.get("engine") or "protocol").strip().lower() or "protocol"
    return {
        "enabled": bool(raw.get("enabled")),
        "interval_hours": max(1, min(int(raw.get("interval_hours") or 1), 168)),
        "per_domain": max(1, min(int(raw.get("per_domain") or 20), 500)),
        "concurrency": max(1, min(int(raw.get("concurrency") or reg_cfg.get("threads") or 3), 20)),
        "engine": engine,
        "keep_on_fail": bool(raw.get("keep_on_fail")),
        "refresh_domains": raw.get("refresh_domains", True) is not False,
        "group_id": str(raw.get("group_id") or "").strip(),
    }


def _maddy_auto_provider_entry(cfg: dict[str, Any]) -> dict[str, Any]:
    return dict(_find_provider(cfg, "maddy_webui") or {}, type="maddy_webui")


def _sanitize_maddy_provider_entry(provider: dict[str, Any]) -> bool:
    if not isinstance(provider, dict):
        return False
    blocked = _maddy_blocked_domains(provider)
    blocked_list = _normalize_domain_list(
        [*MADDY_WEBUI_DEFAULT_BLOCKED_DOMAINS, *_normalize_domain_list(provider.get("blocked_domains"))]
    )
    changed = False
    if _normalize_domain_list(provider.get("blocked_domains")) != blocked_list:
        provider["blocked_domains"] = blocked_list
        changed = True
    domains = _normalize_domain_list(provider.get("domains"))
    filtered_domains = [domain for domain in domains if domain not in blocked]
    if filtered_domains != domains:
        provider["domains"] = filtered_domains
        changed = True
    replacement = filtered_domains[0] if filtered_domains else ""
    for key in ("default_domain", "domain"):
        current = str(provider.get(key) or "").strip().lower()
        if current in blocked and provider.get(key) != replacement:
            provider[key] = replacement
            changed = True
    return changed


def _maddy_secret_input(value: Any) -> str:
    """Return a usable Maddy credential, ignoring values redacted for display.

    The config endpoint intentionally returns masked credentials (for example
    ``maddy_…acf3``).  The UI may send that value back when refreshing or
    saving the provider.  Treating it as a new key would overwrite the real
    credential and make every subsequent API request fail authentication.
    """
    text = str(value or "").strip()
    if not text or any(marker in text for marker in ("…", "•", "***")):
        return ""
    return text


def _normalize_domain_list(values: Any) -> list[str]:
    items: list[str] = []
    if isinstance(values, list):
        items = [str(item or "").strip().lower() for item in values]
    elif isinstance(values, str):
        items = [item.strip().lower() for item in re.split(r"[,;\s]+", values) if item.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _maddy_blocked_domains(entry: dict[str, Any] | None = None) -> set[str]:
    blocked = set(_normalize_domain_list(list(MADDY_WEBUI_DEFAULT_BLOCKED_DOMAINS)))
    if isinstance(entry, dict):
        blocked.update(_normalize_domain_list(entry.get("blocked_domains")))
    return blocked


def _maddy_allowed_domains(entry: dict[str, Any] | None, values: Any) -> list[str]:
    blocked = _maddy_blocked_domains(entry)
    return [domain for domain in _normalize_domain_list(values) if domain not in blocked]


def _maddy_auto_domains(cfg: dict[str, Any], *, refresh: bool = True) -> list[str]:
    entry = _maddy_auto_provider_entry(cfg)
    if not entry:
        return []
    conf = _make_config(cfg.get("mail") if isinstance(cfg.get("mail"), dict) else {})
    conf["request_timeout"] = min(float(conf.get("request_timeout") or 30), 10.0)
    provider = MaddyWebuiProvider(entry, conf)
    try:
        if refresh:
            try:
                info = provider.fetch_domain_info(force=True)
                domains = _maddy_allowed_domains(entry, info.get("domains") or [])
                if domains:
                    return domains
            except Exception as exc:
                logger.warning("Maddy 自动注册拉取域名失败，回退本地配置: %s", str(exc)[:200])
        domains = provider._configured_domains()
        return _maddy_allowed_domains(entry, domains)
    finally:
        provider.close()


def _maddy_auto_domain_last_runs(state: dict[str, Any]) -> dict[str, float]:
    raw = state.get("maddy_auto_domain_last_epoch")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in raw.items():
        try:
            out[str(key).strip().lower()] = float(value or 0)
        except (TypeError, ValueError):
            continue
    return out


def _parse_iso_epoch(value: str) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _maddy_auto_mark_domain_run(domain: str, *, result: dict[str, Any]) -> dict[str, Any]:
    domain_l = str(domain or "").strip().lower()
    if not domain_l:
        return load_ops_state()
    state = load_ops_state()
    domain_runs = _maddy_auto_domain_last_runs(state)
    domain_runs[domain_l] = time.time()
    state.update(
        {
            "maddy_auto_current_domain": "",
            "maddy_auto_current_started_at": "",
            "maddy_auto_last_epoch": time.time(),
            "maddy_auto_last_at": _now_iso(),
            "maddy_auto_last_result": dict(result),
            "maddy_auto_domain_last_epoch": domain_runs,
            "maddy_auto_last_domain": domain_l,
        }
    )
    update_ops_state(**state)
    return state


def _maddy_auto_mark_domain_start(domain: str, settings: dict[str, Any]) -> dict[str, Any]:
    domain_l = str(domain or "").strip().lower()
    state = load_ops_state()
    domain_runs = _maddy_auto_domain_last_runs(state)
    domain_runs[domain_l] = time.time()
    state.update(
        {
            "maddy_auto_current_domain": domain_l,
            "maddy_auto_current_started_at": _now_iso(),
            "maddy_auto_current_interval_hours": int(settings.get("interval_hours") or 1),
            "maddy_auto_current_per_domain": int(settings.get("per_domain") or 20),
            "maddy_auto_domain_last_epoch": domain_runs,
        }
    )
    update_ops_state(**state)
    return state


def _apply_maddy_domain_to_config(cfg: dict[str, Any], domain: str) -> bool:
    domain_l = str(domain or "").strip().lower()
    if not domain_l:
        return False
    mail = cfg.get("mail") if isinstance(cfg.get("mail"), dict) else {}
    providers = mail.get("providers") if isinstance(mail.get("providers"), list) else []
    changed = False
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        if normalize_provider_type(str(provider.get("type") or "")) != "maddy_webui":
            continue
        if domain_l in _maddy_blocked_domains(provider):
            return False
        provider["default_domain"] = domain_l
        provider["domain"] = domain_l
        provider_domains = _normalize_domain_list(provider.get("domains"))
        provider["domains"] = [
            domain_l,
            *[
                item
                for item in provider_domains
                if item != domain_l and item not in _maddy_blocked_domains(provider)
            ],
        ]
        _sanitize_maddy_provider_entry(provider)
        changed = True
    if changed and isinstance(mail, dict):
        cfg["mail"] = mail
    return changed


def _current_health_proxy_pool(cfg: dict[str, Any] | None = None) -> list[str]:
    source = cfg if isinstance(cfg, dict) else _read_config()
    proxy_cfg = source.get("proxy") if isinstance(source.get("proxy"), dict) else {}
    return resolve_proxy_pool(proxy_cfg) if proxy_cfg.get("enabled") else []


def _run_account_maintenance_sync(*, force: bool = False) -> dict[str, Any]:
    if not _MAINTENANCE_RUN_LOCK.acquire(blocking=False):
        return {
            "ok": True,
            "skipped": True,
            "reason": "maintenance_already_running",
        }
    try:
        return _run_account_maintenance_sync_impl(force=force)
    finally:
        _MAINTENANCE_RUN_LOCK.release()


def _run_account_maintenance_sync_impl(*, force: bool = False) -> dict[str, Any]:
    from common.account_health import check_accounts

    cfg = _read_config()
    settings = _maintenance_config(cfg)
    state = load_ops_state()
    now_epoch = time.time()
    last_epoch = float(state.get("last_health_epoch") or 0)
    due = now_epoch - last_epoch >= settings["interval_hours"] * 3600
    if not force and (not settings["enabled"] or not due):
        return {"ok": True, "skipped": True, "due": due, "settings": settings}
    if _account_health_progress_snapshot(include_updates=False).get("running"):
        return {
            "ok": True,
            "skipped": True,
            "reason": "health_check_already_running",
            "settings": settings,
        }
    all_records = _load_accounts()
    probe_records, skipped_records = select_health_check_records(all_records)
    requested_accounts = len(probe_records) + len(skipped_records)
    actual_total = len(probe_records)
    for account in [*probe_records, *skipped_records]:
        record_operation_event(
            account,
            "maintenance",
            "running",
            message="自动维护测活开始",
            metadata={"force": bool(force)},
        )
    PROGRESS.start(
        "account_maintenance",
        "账号批量测活",
        total=actual_total,
        stage=(
            "刷新 OAuth 并检测账号"
            + (f" · 跳过已封号 {len(skipped_records)}" if skipped_records else "")
        ),
    )

    def _health_progress(done: int, total: int, _item: dict[str, Any]) -> None:
        actual_workers, resource_mode = _account_health_resource_budget(
            settings["workers"]
        )
        stage = f"已检测 {done} / {total}"
        if skipped_records:
            stage += f" · 跳过已封号 {len(skipped_records)}"
        if resource_mode != "normal":
            stage += f" · 注册优先，测活并发 {actual_workers}"
        PROGRESS.update(
            "account_maintenance",
            done=done,
            total=total,
            stage=stage,
        )

    try:
        result = check_accounts(
            records=all_records,
            workers=settings["workers"],
            item_callback=_health_progress,
            worker_limit=_account_health_worker_limit,
            proxy_candidates=_current_health_proxy_pool(cfg),
        )
    except Exception as exc:
        for account in [*probe_records, *skipped_records]:
            record_operation_event(
                account,
                "maintenance",
                "failed",
                message="自动维护测活失败",
                error=exc,
            )
        PROGRESS.finish("account_maintenance", ok=False, message=str(exc))
        raise
    checked_by_key = {
        _account_key(item): item
        for item in result.get("items", [])
        if isinstance(item, dict) and _account_key(item)
    }
    for account in probe_records:
        checked = checked_by_key.get(_account_key(account), account)
        state = normalized_health_status(checked)
        record_operation_event(
            checked,
            "maintenance",
            "success" if state in {"alive", "limited"} else "failed",
            message=("自动维护测活完成" if state in {"alive", "limited"} else "自动维护测活失败"),
            error=checked.get("health_error") or checked.get("health_probe_error"),
            metadata={"health_status": state},
        )
    for account in skipped_records:
        record_operation_event(
            account,
            "maintenance",
            "skipped",
            message="自动维护跳过已封号账号",
            error=account.get("health_error"),
        )
    replacement = _replace_cdks_after_health(all_records, result["items"])
    deleted = 0
    if settings["auto_delete_invalid"]:
        records = _load_accounts()
        invalid = [
            item
            for item in records
            if normalized_health_status(item) == "invalid"
            and not account_is_deactivated(item)
        ]
        if invalid:
            archive_accounts(invalid, reason="scheduled-invalid")
            keys = [_account_key(item) for item in invalid]
            deleted, _ = delete_account_records(
                keys,
                include_records=False,
                path=ACCOUNTS_FILE,
            )
    update_ops_state(
        last_health_epoch=now_epoch,
        last_health_at=_now_iso(),
        last_health_result={
            key: result.get(key, 0)
            for key in (
                "requested",
                "actual_total",
                "checked",
                "usable",
                "unavailable",
                "skipped_known_deactivated",
                "deactivated",
                "invalid_other",
                "alive",
                "limited",
                "invalid",
                "reauth_required",
                "error",
            )
        } | {
            "cdk_replaced": replacement["replaced"],
            "cdk_unresolved": replacement["unresolved"],
        },
        last_auto_deleted=deleted,
    )
    PROGRESS.finish(
        "account_maintenance",
        message=(
            f"正常 {result.get('alive', 0)}，待续期 {result.get('reauth_required', 0)}，"
            f"其它失效 {result.get('invalid_other', result.get('invalid', 0))}，"
            f"已封号跳过 {result.get('skipped_known_deactivated', 0)}，"
            f"CDK 替换 {replacement['replaced']}，待补位 {replacement['unresolved']}"
        ),
    )
    mail = _public_mail_stats(cfg)
    webhook = settings["webhook_url"]
    if (
        webhook
        and settings["notify_on_pool_low"]
        and int(mail.get("registration_available") or 0)
        <= settings["pool_low_threshold"]
    ):
        send_webhook(
            webhook,
            {
                "event": "mail_pool_low",
                "available": int(mail.get("registration_available") or 0),
                "total": int(mail.get("total_slots") or 0),
                "checked_at": _now_iso(),
            },
        )
    return {
        "ok": True,
        "skipped": False,
        "deleted": deleted,
        "cdk_replaced": replacement["replaced"],
        "cdk_unresolved": replacement["unresolved"],
        **result,
    }


async def _run_register_async(cfg: dict[str, Any], params: dict[str, Any]):
    from register_gpt import force_close_all_sessions, run_register

    loop = asyncio.get_running_loop()
    requested_workers = max(1, int(params.get("concurrency") or 1))
    # Registration gets its own I/O pool sized from the requested concurrency.
    # Health checks use a separate pool and do not borrow this capacity.
    loop.set_default_executor(
        ThreadPoolExecutor(
            max_workers=max(_io_worker_count(), requested_workers),
            thread_name_prefix="registration-io",
        )
    )

    def _silence_expected_browser_races(loop_, context):
        if _is_expected_browser_navigation_error(context):
            return
        try:
            loop_.default_exception_handler(context)
        except Exception:
            pass

    try:
        loop.set_exception_handler(_silence_expected_browser_races)
    except Exception:
        pass

    cancel_event = asyncio.Event()
    with JOB.lock:
        JOB._loop = loop
        JOB._cancel_event = cancel_event
        if JOB.stop_requested:
            cancel_event.set()

    def latest_proxy_config() -> dict[str, Any]:
        latest = _read_config()
        value = latest.get("proxy")
        return dict(value) if isinstance(value, dict) else {}

    registration_task = asyncio.create_task(
        run_register(
            cfg,
            total=int(params.get("total") or 1),
            concurrency=int(params.get("concurrency") or 1),
            keep_on_fail=bool(params.get("keep_on_fail")),
            fixed_email=(params.get("email") or None) or None,
            provider_type=(
                params.get("mail_type") or params.get("provider_type") or "auto"
            ),
            stop_check=_job_should_stop,
            pause_check=_job_should_pause,
            cancel_event=cancel_event,
            task_event=JOB.task_event,
            group_id=str(params.get("group_id") or ""),
            group_plan=params.get("group_plan"),
            # The worker owns the active request, but reads the latest proxy
            # pool before each task/retry so a panel update takes effect
            # without restarting the service.
            proxy_config_loader=latest_proxy_config,
        ),
        name="registration-root",
    )
    with JOB.lock:
        JOB._registration_task = registration_task

    try:
        return await registration_task
    except asyncio.CancelledError:
        return []
    finally:
        if _job_should_stop():
            try:
                n = await force_close_all_sessions()
                if n:
                    JOB.append_log(f"已强制关闭 {n} 个浏览器会话")
            except Exception as e:
                JOB.append_log(f"强制关闭出错: {e}")
        with JOB.lock:
            if JOB._registration_task is registration_task:
                JOB._registration_task = None
            JOB._loop = None
            JOB._cancel_event = None


def _run_job_thread(params: dict[str, Any]) -> None:
    avail = params.get("_pool_available")
    pool_t = params.get("_pool_total")
    extra = f" 可用邮箱={avail}/{pool_t}" if avail is not None else ""
    domain_extra = (
        f" Maddy域名={params.get('maddy_domain')}"
        if str(params.get("maddy_domain") or "").strip()
        else ""
    )
    JOB.append_log(
        f"任务启动 总数={params.get('total')} 并发={params.get('concurrency')} "
        f"(请求={params.get('requested_concurrency') or params.get('_requested_concurrency') or params.get('concurrency')}) "
        f"邮箱类型={params.get('mail_type') or params.get('provider_type') or 'auto'} "
        f"引擎={params.get('engine') or 'protocol'}"
        f"{domain_extra}{extra} 分组={params.get('group_name') or params.get('group_id') or '-'}"
    )
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = _LogStream(old_out, JOB)  # type: ignore[assignment]
    sys.stderr = _LogStream(old_err, JOB)  # type: ignore[assignment]
    try:
        cfg = load_config(CONFIG_FILE if CONFIG_FILE.is_file() else None)
        maddy_domain = str(params.get("maddy_domain") or "").strip().lower()
        blocked_domains = _maddy_blocked_domains(_find_provider(cfg, "maddy_webui") or {})
        if maddy_domain and maddy_domain in blocked_domains:
            raise RuntimeError(f"Maddy 域名已屏蔽: {maddy_domain}")
        if maddy_domain:
            _apply_maddy_domain_to_config(cfg, maddy_domain)
        reg = cfg.setdefault("registration", {})
        reg["total"] = int(params.get("total") or 1)
        reg["threads"] = int(params.get("concurrency") or 1)
        if params.get("engine"):
            reg["engine"] = str(params["engine"])

        results = asyncio.run(_run_register_async(cfg, params))
        with JOB.lock:
            JOB.results = list(results or [])
            JOB._recompute_counts_locked()
            if not JOB.task_cards:
                JOB.success_count = len(results or [])
                JOB.fail_count = max(0, JOB.total - JOB.success_count)
                JOB.skipped_count = 0
            else:
                n_ok = len(results or [])
                if n_ok > JOB.success_count:
                    JOB.success_count = n_ok
                unfinished = max(
                    0,
                    JOB.total
                    - JOB.success_count
                    - JOB.fail_count
                    - JOB.skipped_count,
                )
                if unfinished and not JOB.stop_requested:
                    JOB.fail_count += unfinished
            stopped = JOB.stop_requested
            sc, fc, sk, tot = (
                JOB.success_count,
                JOB.fail_count,
                JOB.skipped_count,
                JOB.total,
            )
        if not maddy_domain:
            mail_type = str(params.get("mail_type") or params.get("provider_type") or "").strip().lower()
            if mail_type in {"maddy", "maddy_webui"}:
                provider = _find_provider(cfg, "maddy_webui") or {}
                maddy_domain = str(provider.get("default_domain") or provider.get("domain") or "").strip().lower()
        if maddy_domain:
            _maddy_auto_mark_domain_run(
                maddy_domain,
                result={
                    "ok": not stopped and fc == 0,
                    "success": sc,
                    "failed": fc,
                    "skipped": sk,
                    "total": tot,
                    "batch_id": JOB.batch_id or "",
                    "finished_at": _now_iso(),
                },
            )
        if stopped:
            JOB.append_log(f"任务已停止 · 成功={sc} 失败={fc} 跳过={sk} 总数={tot}")
        else:
            JOB.append_log(f"任务完成 · 成功={sc} 失败={fc} 跳过={sk} 总数={tot}")
        try:
            maintenance = _maintenance_config(cfg)
            webhook = str(maintenance.get("webhook_url") or "").strip()
            if webhook and maintenance.get("notify_on_complete"):
                ok_notice, notice_message = send_webhook(
                    webhook,
                    {
                        "event": "registration_completed",
                        "stopped": stopped,
                        "success": sc,
                        "failed": fc,
                        "skipped": sk,
                        "total": tot,
                        "finished_at": _now_iso(),
                    },
                )
                JOB.append_log(
                    f"Webhook {'发送成功' if ok_notice else '发送失败'} · {notice_message}"
                )
        except Exception as exc:
            JOB.append_log(f"Webhook 通知异常: {str(exc)[:120]}")

    except Exception as e:
        JOB.last_error = str(e)[:500]
        JOB.append_log(f"任务出错: {e}")
        JOB.append_log(traceback.format_exc()[-1500:])
    finally:
        sys.stdout = old_out
        sys.stderr = old_err
        with JOB.lock:
            JOB.running = False
            JOB.finished_at = _now_iso()
            JOB.stop_requested = False
            JOB.paused = False
            JOB._loop = None
            JOB._cancel_event = None
            JOB._registration_task = None
            JOB._thread = None
            JOB._cond.notify_all()
        JOB.persist_runtime()


_MAINTENANCE_TASK: asyncio.Task | None = None
_MADDY_AUTO_TASK: asyncio.Task | None = None
_MADDY_AUTO_RUN_LOCK = threading.Lock()


async def _wait_for_job_completion(poll_sec: float = 3.0) -> None:
    while True:
        with JOB.lock:
            running = bool(JOB.running)
        if not running or _SERVER_SHUTDOWN_EVENT.is_set():
            return
        await asyncio.sleep(max(0.5, poll_sec))


def _maddy_auto_domain_plan(cfg: dict[str, Any], settings: dict[str, Any]) -> list[str]:
    domains = _maddy_auto_domains(cfg, refresh=bool(settings.get("refresh_domains")))
    return _normalize_domain_list(domains)


async def _run_maddy_auto_register_once(*, force: bool = False) -> dict[str, Any]:
    if not _MADDY_AUTO_RUN_LOCK.acquire(blocking=False):
        return {"ok": True, "skipped": True, "reason": "maddy_auto_already_running"}
    try:
        cfg = _read_config()
        settings = _maddy_auto_config(cfg)
        if not settings["enabled"] and not force:
            # Skip before the domain fetch so a disabled feature never hits
            # the Maddy API (and never logs remote errors) from the loop.
            return {"ok": True, "skipped": True, "reason": "disabled", "settings": settings}
        state = load_ops_state()
        now_epoch = time.time()
        last_runs = _maddy_auto_domain_last_runs(state)
        domains = _maddy_auto_domain_plan(cfg, settings)
        if not domains:
            update_ops_state(
                maddy_auto_last_epoch=now_epoch,
                maddy_auto_last_at=_now_iso(),
                maddy_auto_last_error="no_domains",
                maddy_auto_current_domain="",
                maddy_auto_current_started_at="",
            )
            return {"ok": True, "skipped": True, "reason": "no_domains", "settings": settings}
        due: list[str] = []
        interval_sec = max(1, int(settings["interval_hours"])) * 3600
        for domain in domains:
            if force or now_epoch - float(last_runs.get(domain) or 0) >= interval_sec:
                due.append(domain)
        if not due:
            return {
                "ok": True,
                "skipped": True,
                "reason": "not_due",
                "domains": domains,
                "settings": settings,
            }

        results: list[dict[str, Any]] = []
        for domain in due:
            if _SERVER_SHUTDOWN_EVENT.is_set():
                break
            with JOB.lock:
                busy = bool(JOB.running)
            if busy:
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": "job_running",
                    "domain": domain,
                    "results": results,
                    "settings": settings,
                }
            _maddy_auto_mark_domain_start(domain, settings)
            try:
                await job_start(
                    StartJobRequest(
                        total=int(settings["per_domain"]),
                        concurrency=int(settings["concurrency"]),
                        keep_on_fail=bool(settings["keep_on_fail"]),
                        mail_type="maddy_webui",
                        maddy_domain=domain,
                        engine=str(settings["engine"] or "protocol"),
                        group_id=str(settings["group_id"] or "") or None,
                    )
                )
            except HTTPException as exc:
                update_ops_state(
                    maddy_auto_last_error=str(exc.detail)[:200],
                    maddy_auto_last_domain=domain,
                    maddy_auto_current_domain="",
                    maddy_auto_current_started_at="",
                )
                results.append({"domain": domain, "ok": False, "error": str(exc.detail)})
                continue
            except Exception as exc:
                update_ops_state(
                    maddy_auto_last_error=str(exc)[:200],
                    maddy_auto_last_domain=domain,
                    maddy_auto_current_domain="",
                    maddy_auto_current_started_at="",
                )
                results.append({"domain": domain, "ok": False, "error": str(exc)[:200]})
                continue

            await _wait_for_job_completion()
            job_snapshot = JOB.snapshot()
            ok = int(job_snapshot.get("success_count") or 0)
            fail = int(job_snapshot.get("fail_count") or 0)
            skipped = int(job_snapshot.get("skipped_count") or 0)
            result = {
                "domain": domain,
                "ok": True,
                "success": ok,
                "failed": fail,
                "skipped": skipped,
                "total": int(job_snapshot.get("total") or int(settings["per_domain"])),
                "batch_id": job_snapshot.get("batch_id") or "",
                "finished_at": job_snapshot.get("finished_at") or _now_iso(),
            }
            _maddy_auto_mark_domain_run(domain, result=result)
            results.append(result)

        update_ops_state(
            maddy_auto_last_epoch=time.time(),
            maddy_auto_last_at=_now_iso(),
            maddy_auto_last_result=results[-1] if results else {},
            maddy_auto_last_error="",
            maddy_auto_current_domain="",
            maddy_auto_current_started_at="",
        )
        return {"ok": True, "skipped": False, "results": results, "settings": settings, "domains": domains}
    finally:
        _MADDY_AUTO_RUN_LOCK.release()


async def _maddy_auto_loop() -> None:
    while not _SERVER_SHUTDOWN_EVENT.is_set():
        try:
            await _run_maddy_auto_register_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            JOB.append_log(f"Maddy 自动注册异常: {str(exc)[:160]}")
        await asyncio.sleep(60)


async def _maintenance_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(_run_account_maintenance_sync)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            JOB.append_log(f"自动维护异常: {str(exc)[:160]}")
        await asyncio.sleep(60)


async def _shutdown_active_background_jobs() -> None:
    """Request a coordinated stop and wait briefly before the loop closes."""
    waits: list[tuple[str, threading.Thread | None, Any, str]] = []

    with JOB.lock:
        if JOB.running:
            JOB.stop_requested = True
            JOB.paused = False
        registration_loop = JOB._loop
        cancel_event = JOB._cancel_event
        registration_task = JOB._registration_task
        registration_thread = JOB._thread
        registration_engine = str(JOB.params.get("engine") or "protocol").lower()
    if registration_loop is not None and cancel_event is not None:
        try:
            def request_registration_stop() -> None:
                cancel_event.set()
                if (
                    registration_engine == "browser"
                    and registration_task is not None
                    and not registration_task.done()
                ):
                    registration_task.cancel()

            registration_loop.call_soon_threadsafe(request_registration_stop)
            from register_gpt import force_close_all_sessions

            asyncio.run_coroutine_threadsafe(
                force_close_all_sessions(),
                registration_loop,
            )
        except Exception as error:
            JOB.append_log(f"服务退出时停止注册任务失败: {str(error)[:160]}")
    if registration_thread is not None and registration_thread.is_alive():
        waits.append(("registration", registration_thread, None, ""))

    reauth = ACCOUNT_REAUTH_JOBS.active_snapshot()
    if reauth and reauth.get("status") in {"queued", "running", "stopping"}:
        reauth_id = str(reauth.get("job_id") or "")
        ACCOUNT_REAUTH_JOBS.stop(reauth_id)
        waits.append(("reauth", None, ACCOUNT_REAUTH_JOBS, reauth_id))

    trial = TRIAL_ELIGIBILITY_JOBS.active()
    if trial and trial.get("status") in {"queued", "running", "stopping"}:
        trial_id = str(trial.get("job_id") or "")
        TRIAL_ELIGIBILITY_JOBS.stop(trial_id)
        waits.append(("trial", None, TRIAL_ELIGIBILITY_JOBS, trial_id))

    async def wait_one(
        label: str,
        thread: threading.Thread | None,
        manager: Any,
        job_id: str,
    ) -> None:
        if thread is not None:
            await asyncio.to_thread(thread.join, 15.0)
            done = not thread.is_alive()
        else:
            done = await asyncio.to_thread(manager.wait, job_id, 15.0)
        if not done:
            JOB.append_log(f"服务退出等待超时 · {label}")

    if waits:
        await asyncio.gather(*(wait_one(*item) for item in waits))


@asynccontextmanager
async def _app_lifespan(_app: FastAPI):
    global _MAINTENANCE_TASK, _MADDY_AUTO_TASK, _PANEL_CACHE_WARM_TASK
    loop = asyncio.get_running_loop()
    _SERVER_SHUTDOWN_EVENT.clear()
    web_executor = ThreadPoolExecutor(
        max_workers=_io_worker_count(),
        thread_name_prefix="web-io",
    )
    loop.set_default_executor(web_executor)
    # Warm the mailbox snapshot in the background so the first authenticated
    # status request does not pay the large trash-file parse cost.
    _PANEL_CACHE_WARM_TASK = asyncio.create_task(
        asyncio.to_thread(_warm_panel_caches),
        name="panel-cache-warm",
    )
    if _MAINTENANCE_TASK is None or _MAINTENANCE_TASK.done():
        _MAINTENANCE_TASK = asyncio.create_task(_maintenance_loop(), name="account-maintenance")
    if _MADDY_AUTO_TASK is None or _MADDY_AUTO_TASK.done():
        _MADDY_AUTO_TASK = asyncio.create_task(_maddy_auto_loop(), name="maddy-auto-register")
    try:
        yield
    finally:
        _SERVER_SHUTDOWN_EVENT.set()
        if _PANEL_CACHE_WARM_TASK is not None:
            _PANEL_CACHE_WARM_TASK.cancel()
            try:
                await _PANEL_CACHE_WARM_TASK
            except asyncio.CancelledError:
                pass
            _PANEL_CACHE_WARM_TASK = None
        if _MAINTENANCE_TASK is not None:
            _MAINTENANCE_TASK.cancel()
            try:
                await _MAINTENANCE_TASK
            except asyncio.CancelledError:
                pass
            _MAINTENANCE_TASK = None
        if _MADDY_AUTO_TASK is not None:
            _MADDY_AUTO_TASK.cancel()
            try:
                await _MADDY_AUTO_TASK
            except asyncio.CancelledError:
                pass
            _MADDY_AUTO_TASK = None
        await _shutdown_active_background_jobs()
        web_executor.shutdown(wait=True, cancel_futures=True)


app = FastAPI(title="GPT Register FP", version="1.0.0", lifespan=_app_lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
app.include_router(auth_router)

from web.routes.account_groups import router as account_groups_router
from web.routes.accounts_trash import router as accounts_trash_router
app.include_router(account_groups_router)
app.include_router(accounts_trash_router)

from web.routes.account_groups import (  # noqa: E402,F401
    account_group_export_credentials,
    account_groups,
    account_groups_create,
    account_groups_delete,
    account_groups_rename,
    accounts_assign_group,
)
from web.routes.accounts_trash import (  # noqa: E402,F401
    accounts_trash,
    accounts_trash_restore,
)


from web.routes.accounts_rebind import router as accounts_rebind_router
app.include_router(accounts_rebind_router)

from web.routes.accounts_rebind import (  # noqa: E402,F401
    AccountRebindPreflightRequest,
    AccountRebindStartRequest,
    AccountRebindStopRequest,
    DEFAULT_REBIND_CLIENT_BUILD,
    DEFAULT_REBIND_CLIENT_VERSION,
    _rebind_discard_reservation,
    _rebind_identity_value,
    _rebind_job_worker_factory,
    _rebind_load_record,
    _rebind_mail_cfg,
    _rebind_parse_sources,
    _rebind_parse_target_specs,
    _rebind_proxy_candidates,
    _rebind_public_source,
    _rebind_record_for_source,
    _rebind_register_reservation,
    _rebind_release_reservation_item,
    _rebind_release_source_reservations,
    _rebind_reservation_cleanup,
    _rebind_reservation_items,
    _rebind_reservation_payload,
    _rebind_reserve_target,
    _rebind_target_conflict,
    _rebind_worker,
    account_rebind_job,
    account_rebind_preflight,
    account_rebind_progress,
    account_rebind_reservation_cancel,
    account_rebind_start,
    account_rebind_stop,
)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Panel login (username / password) + signed session cookie
# ---------------------------------------------------------------------------

SESSION_COOKIE = "gpt_reg_session"
SESSION_MAX_AGE = 7 * 24 * 3600  # 7 days
REDEEM_COOKIE = "gpt_redeem_session"
REDEEM_SESSION_MAX_AGE = 30 * 24 * 3600

PUBLIC_PATHS = {
    "/login",
    "/redeem",
    "/cdk-query",
    "/api/health",
    "/api/auth/login",
    "/api/auth/status",
    "/api/cdk-query",
    "/favicon.ico",
}
PUBLIC_PREFIXES = ("/static/", "/api/redeem")


def _web_cfg() -> dict[str, Any]:
    cfg = _read_config()
    web = cfg.get("web") if isinstance(cfg.get("web"), dict) else {}
    return web if isinstance(web, dict) else {}


_WEB_CREDS_CACHE: dict[str, str] | None = None
_WEB_CREDS_CACHE_MTIME: float | None = None
_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
_LOGIN_ATTEMPTS_LOCK = threading.Lock()
_REDEEM_ATTEMPTS: dict[str, list[float]] = {}
_REDEEM_ATTEMPTS_LOCK = threading.Lock()


def _ensure_web_credentials() -> dict[str, str]:
    """Ensure username/password/session-secret exist; persist if generated.

    Cached by config mtime so every request does not re-parse YAML (which
    previously piled up under 3s panel polling and made /api/health hang).
    """
    global _WEB_CREDS_CACHE, _WEB_CREDS_CACHE_MTIME
    try:
        mtime = CONFIG_FILE.stat().st_mtime if CONFIG_FILE.is_file() else 0.0
    except Exception:
        mtime = 0.0
    if (
        _WEB_CREDS_CACHE is not None
        and _WEB_CREDS_CACHE_MTIME is not None
        and mtime == _WEB_CREDS_CACHE_MTIME
    ):
        return dict(_WEB_CREDS_CACHE)

    with _CONFIG_MUTATION_LOCK:
        cfg = _read_config()
        web = cfg.setdefault("web", {})
        if not isinstance(web, dict):
            web = {}
            cfg["web"] = web
        changed = False
        username = str(web.get("username") or "").strip() or "admin"
        if str(web.get("username") or "").strip() != username:
            web["username"] = username
            changed = True
        else:
            web.setdefault("username", username)
        password = str(web.get("password") or "").strip()
        if not password:
            password = secrets.token_urlsafe(10)
            web["password"] = password
            changed = True
        secret = str(web.get("token") or "").strip()
        if not secret:
            secret = secrets.token_urlsafe(24)
            web["token"] = secret
            changed = True
        web.setdefault("host", "127.0.0.1")
        web.setdefault("port", 8787)
        if changed:
            try:
                _write_config(cfg)
                try:
                    mtime = CONFIG_FILE.stat().st_mtime
                except Exception:
                    pass
            except Exception:
                pass
    out = {"username": username, "password": password, "secret": secret}
    _WEB_CREDS_CACHE = dict(out)
    _WEB_CREDS_CACHE_MTIME = mtime
    return out



def _ensure_web_token() -> str:
    """Session signing secret (kept for backward compatibility)."""
    return _ensure_web_credentials()["secret"]


def _b64url(data: bytes) -> str:
    from common.web_auth import _b64url as encode

    return encode(data)


def _b64url_decode(text: str) -> bytes:
    from common.web_auth import _b64url_decode as decode

    return decode(text)


def _sign_session(username: str, *, max_age: int = SESSION_MAX_AGE) -> str:
    creds = _ensure_web_credentials()
    return _sign_session_value(username, creds["secret"], max_age=max_age)


def _verify_session(token: str) -> str | None:
    creds = _ensure_web_credentials()
    return _verify_session_value(token, creds["secret"], creds["username"])


def _sign_redeem_session(cdk_id: str, *, max_age: int = REDEEM_SESSION_MAX_AGE) -> str:
    return _sign_redeem_session_value(
        cdk_id,
        _ensure_web_credentials()["secret"],
        max_age=max_age,
        now=int(time.time()),
    )


def _verify_redeem_session(token: str) -> str | None:
    return _verify_redeem_session_value(
        token,
        _ensure_web_credentials()["secret"],
    )


def _redeem_record_from_request(request: Request, *, touch: bool = False) -> dict[str, Any] | None:
    cdk_id = _verify_redeem_session(str(request.cookies.get(REDEEM_COOKIE) or ""))
    return get_cdk(cdk_id, CDKS_FILE, touch=touch) if cdk_id else None


def _redeem_bound_account(record: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve the one account bound to a redeemed CDK."""
    key = str(record.get("account_key") or "").strip()
    account_id = str(record.get("account_id") or "").strip().lower()
    email = str(record.get("email") or "").strip().lower()

    # The normal production store is indexed SQLite. Resolve by canonical
    # identity first so a redeemed-session request does not decode every large
    # session/cookie payload in the account database. A full scan remains a
    # compatibility fallback for legacy CDKs whose stored account key no
    # longer matches the account's current canonical identity.
    lookup_keys: list[str] = []
    for candidate in (
        key,
        key.lower(),
        f"account_id:{account_id}" if account_id else "",
        f"email:{email}" if email else "",
    ):
        if candidate and candidate not in lookup_keys:
            lookup_keys.append(candidate)

    def matches(account: dict[str, Any]) -> bool:
        return bool(
            (key and _account_key(account) == key)
            or (
                account_id
                and str(account.get("account_id") or "").strip().lower()
                == account_id
            )
            or (
                email
                and str(account.get("email") or "").strip().lower() == email
            )
        )

    if lookup_keys:
        found = load_account_records_by_keys(lookup_keys, path=ACCOUNTS_FILE)
        # Prefer the same identity order as the legacy scan: explicit CDK
        # account_key, then account_id, then email.
        for lookup_key in lookup_keys:
            account = found.get(lookup_key)
            if isinstance(account, dict) and matches(account):
                return account
        # A keyed lookup can return a canonical account under a different key
        # when an alias happens to bridge two identities; retain the match as
        # a last fast-path check before falling back to the legacy full scan.
        for account in found.values():
            if isinstance(account, dict) and matches(account):
                return account

    # Preserve legacy alias matching when a CDK predates account_id/email
    # canonicalization or a custom JSON store is in use.
    for account in _load_accounts():
        if matches(account):
            return account
    return None


def _redeem_security_payload(account: dict[str, Any]) -> dict[str, Any]:
    password = str(account.get("password") or "")
    totp_secret = str(account.get("totp_secret") or "")
    return {
        "password": password,
        "password_set": bool(password.strip()),
        "mfa_enabled": bool(account.get("mfa_enabled")) or bool(totp_secret.strip()),
        "mfa_factor_type": str(account.get("mfa_factor_type") or ("totp" if totp_secret.strip() else "")),
        "totp_secret": totp_secret,
    }


def _redeem_account_export(account: dict[str, Any]) -> dict[str, Any]:
    """Build the account JSON delivered to the holder of its redeemed CDK."""
    return {
        "email": str(account.get("email") or ""),
        "account_id": str(account.get("account_id") or ""),
        **_redeem_security_payload(account),
        "access_token": _stored_access_token(account),
        "session_token": str(account.get("session_token") or ""),
        "refresh_token": str(account.get("refresh_token") or ""),
        "plan_type": str(account.get("plan_type") or ""),
        "expires_at": str(account.get("expires_at") or ""),
        "registered_at": str(account.get("registered_at") or ""),
    }


def _require_redeemed_account(request: Request) -> dict[str, Any]:
    record = _redeem_record_from_request(request, touch=True)
    if record is None:
        raise HTTPException(401, "兑换会话已失效")
    if str(record.get("status") or "").lower() != "redeemed":
        raise HTTPException(403, "CDK 尚未兑换")
    account = _redeem_bound_account(record)
    if account is None:
        raise HTTPException(404, "绑定账号不存在")
    return account


def _set_redeem_cookie(response: Response, cdk_id: str) -> None:
    response.set_cookie(
        key=REDEEM_COOKIE,
        value=_sign_redeem_session(cdk_id),
        max_age=REDEEM_SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def _clear_redeem_cookie(response: Response) -> None:
    response.delete_cookie(key=REDEEM_COOKIE, path="/", secure=True, samesite="lax")


def _trusted_proxy_hosts() -> set[str]:
    configured = _web_cfg().get("trusted_proxies")
    if isinstance(configured, str):
        values = configured.split(",")
    elif isinstance(configured, list):
        values = configured
    else:
        values = ["127.0.0.1", "::1"]
    return {str(value or "").strip().strip("[]").lower() for value in values if str(value or "").strip()}


def _request_client_ip(request: Request) -> str:
    client_host = str(getattr(request.client, "host", "") or "unknown").strip()
    if client_host.lower() not in _trusted_proxy_hosts():
        return client_host or "unknown"
    forwarded = str(request.headers.get("cf-connecting-ip") or "").strip()
    if not forwarded:
        forwarded = str(request.headers.get("x-forwarded-for") or "").split(",", 1)[0].strip()
    return forwarded or client_host or "unknown"


def _extract_session(request: Request) -> str:
    cookie = str(request.cookies.get(SESSION_COOKIE) or "").strip()
    if cookie:
        return cookie
    auth = (
        request.headers.get("authorization")
        or request.headers.get("Authorization")
        or ""
    )
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _auth_user(request: Request) -> str | None:
    return _verify_session(_extract_session(request))


def _auth_ok(request: Request) -> bool:
    return _auth_user(request) is not None


def _set_session_cookie(response: Response, session: str, *, secure: bool = False) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=session,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def _request_is_secure(request: Request) -> bool:
    forwarded = str(request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    return request.url.scheme.lower() == "https" or forwarded == "https"


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE, path="/")


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    path = request.url.path or "/"
    if path.startswith("/static/"):
        response = await call_next(request)
        # HTML entry points and the legacy client are intentionally kept
        # revalidation-free so an old bookmarked page cannot stay on the
        # single-page 1,000-row implementation after an upgrade.
        if path.endswith(".html") or path == "/static/app.js":
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response
    if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
        return await call_next(request)
    if path.startswith("/api/"):
        if not _auth_ok(request):
            return JSONResponse(
                {"detail": "unauthorized"},
                status_code=401,
            )
        response = await call_next(request)
        response.headers.setdefault(
            "Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"
        )
        response.headers.setdefault("Pragma", "no-cache")
        return response
    # HTML pages: redirect unauthenticated users to login
    if path in {"/", "/index.html", "/cdk"} and not _auth_ok(request):
        return RedirectResponse(
            url="/login",
            status_code=302,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )
    return await call_next(request)


@app.get("/", include_in_schema=False)
async def public_home(request: Request = None):  # type: ignore[assignment]
    # URL fragments are client-only. The authenticated root must always return
    # the SPA entry so refreshing a hash-routed view keeps its page.
    vue_index = STATIC_DIR / "vue" / "index.html"
    index_path = vue_index if vue_index.is_file() else STATIC_DIR / "index.html"
    if not index_path.is_file():
        return HTMLResponse("<h1>web panel index missing</h1>", status_code=500)
    return FileResponse(
        index_path,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/cdk", response_class=HTMLResponse)
async def index():
    vue_index = STATIC_DIR / "vue" / "index.html"
    index_path = vue_index if vue_index.is_file() else STATIC_DIR / "index.html"
    if not index_path.is_file():
        return HTMLResponse(
            "<h1>web panel index missing</h1>", status_code=500
        )
    return FileResponse(
        index_path,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "time": _now_iso(),
        "auth_required": True,
        "auth_mode": "password",
    }


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@app.get("/api/status")
async def status():
    # Heavy config/mail stats and Redis I/O stay off the event loop.
    def _response():
        cfg = _read_config()
        key = ":".join(
            (
                "status:v2",
                str(_file_stamp(CONFIG_FILE)),
                str(_file_stamp(STATE_FILE)),
                str(account_store_revision(ACCOUNTS_FILE)),
                _account_data_stamp(),
            )
        )
        # Cache the expensive summary independently from the rapidly changing
        # task snapshot, then merge the current job state per request.
        PANEL_RESPONSE_CACHE.configure(_cache_settings(cfg))
        cached, source = PANEL_RESPONSE_CACHE.get_or_set_json(
            key,
            _cache_ttl(cfg, "status_ttl", 3),
            lambda: _status_payload(cfg=cfg, snap={}),
        )
        try:
            payload = json.loads(cached)
        except (TypeError, ValueError):
            payload = _status_payload(cfg=cfg, snap={})
        if not isinstance(payload, dict):
            payload = {}
        payload["job"] = JOB.snapshot()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        cache_status = PANEL_RESPONSE_CACHE.status()
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


@app.get("/api/progress")
async def operation_progress():
    items = PROGRESS.snapshot()
    return {
        "ok": True,
        "running": sum(1 for item in items if item.get("status") == "running"),
        "items": items,
    }


def _status_payload(
    *, cfg: dict[str, Any] | None = None, snap: dict[str, Any] | None = None
) -> dict[str, Any]:
    cfg = cfg if isinstance(cfg, dict) else _read_config()
    snap = snap if isinstance(snap, dict) else JOB.snapshot()
    mail_stats = _public_mail_stats(cfg)
    available = int(
        mail_stats.get("registration_available")
        if mail_stats.get("registration_available") is not None
        else mail_stats.get("available") or 0
    )
    reg_cfg = cfg.get("registration") if isinstance(cfg.get("registration"), dict) else {}
    engine = str(reg_cfg.get("engine") or "protocol").strip().lower()
    configured_threads = max(1, int(reg_cfg.get("threads") or (3 if engine == "protocol" else 1)))
    suggested = min(available, configured_threads) if available else 0
    fp = dict(cfg.get("fingerprint") or {})
    account_summary = account_health_summary(ACCOUNTS_FILE)
    return {
        "job": snap,
        "mail": mail_stats,
        "suggested_concurrency": suggested,
        "suggested_total": available,
        "proxy": _proxy_pool_preview(cfg),
        "fingerprint": fp,
        "registration": cfg.get("registration") or {},
        "chatgpt2api": _c2a_public(cfg),
        "accounts_count": account_summary["total"],
        "accounts": account_summary,
        "auth_required": True,
    }


def _browser_capabilities(cfg: dict[str, Any]) -> dict[str, Any]:
    """Return runtime-backed availability for every supported browser provider."""
    from local_browser import (
        CamoufoxBrowser,
        ObscuraBrowser,
        chrome_executable_candidates,
        resolve_chrome_executable,
        resolve_obscura_binary,
    )

    fp = cfg.get("fingerprint") if isinstance(cfg.get("fingerprint"), dict) else {}
    selected = str(fp.get("browser") or "local").strip().lower()
    playwright_ready = importlib.util.find_spec("playwright") is not None
    # The capability endpoint is a status probe and is called while the panel
    # is opening.  A full headless smoke test can block for 8 seconds on a
    # stale/misconfigured Chrome binary; actual browser startup performs its
    # own launch/error handling, so only resolve the local executable here.
    chrome_path = resolve_chrome_executable(verify_launch=False)
    chrome_candidates = [chrome_path] if chrome_path else chrome_executable_candidates()
    obscura_path = resolve_obscura_binary()

    try:
        camoufox_module = importlib.util.find_spec("camoufox") is not None
        camoufox_ready = camoufox_module and CamoufoxBrowser.is_available()
    except Exception:
        camoufox_module = False
        camoufox_ready = False

    items = [
        {
            "id": "local",
            "name": "Local Chrome",
            "kind": "免费 / 本机",
            "available": bool(playwright_ready and chrome_path),
            "selected": selected in {"local", "chrome", "stealth", "playwright", "chromium", "free"},
            "detail": (
                f"Playwright + {chrome_path}"
                if playwright_ready and chrome_path
                else (
                    "检测到 Chrome/Chromium，但无法通过 headless 启动探测"
                    if playwright_ready and chrome_candidates
                    else "缺少 Playwright 或 Chrome/Chromium 可执行文件"
                )
            ),
        },
        {
            "id": "camoufox",
            "name": "Camoufox",
            "kind": "免费 / 反检测 Firefox",
            "available": bool(playwright_ready and camoufox_ready),
            "selected": selected == "camoufox",
            "detail": (
                "Python 模块与浏览器内核均已安装"
                if playwright_ready and camoufox_ready
                else ("已安装模块，但浏览器内核未下载" if camoufox_module else "Camoufox 模块未安装")
            ),
        },
        {
            "id": "obscura",
            "name": "Obscura",
            "kind": "免费 / 轻量 CDP",
            "available": bool(playwright_ready and ObscuraBrowser.is_available()),
            "selected": selected == "obscura",
            "detail": f"CDP 二进制：{obscura_path}" if obscura_path else "未找到可执行的 Obscura 二进制",
        },
    ]
    return {
        "selected": selected,
        "available_count": sum(1 for item in items if item["available"]),
        "items": items,
    }


@app.get("/api/config")
async def get_config():
    def _response():
        cfg = _read_config()
        return _cached_json_response(
            cfg,
            f"config:v2:{_file_stamp(CONFIG_FILE)}",
            "config_ttl",
            60,
            lambda: {"config": _redact_config_for_api(cfg)},
        )

    return await asyncio.to_thread(_response)


@app.get("/api/browsers/status")
async def browsers_status():
    def _response():
        cfg = _read_config()
        return _cached_json_response(
            cfg,
            f"browsers:v2:{_file_stamp(CONFIG_FILE)}",
            "browsers_ttl",
            300,
            lambda: _browser_capabilities(cfg),
        )

    return await asyncio.to_thread(_response)


@app.put("/api/config")
async def put_config(body: ConfigUpdate):
    if JOB.running:
        raise HTTPException(409, "job is running; stop it before saving config")
    if not isinstance(body.config, dict):
        raise HTTPException(400, "config must be an object")

    def _do():
        with _edit_config() as current:
            _merge_config_preserving_secrets(current, body.config)
            mail = current.get("mail") if isinstance(current.get("mail"), dict) else {}
            providers = mail.get("providers") if isinstance(mail.get("providers"), list) else []
            for provider in providers:
                if isinstance(provider, dict) and normalize_provider_type(str(provider.get("type") or "")) == "maddy_webui":
                    _sanitize_maddy_provider_entry(provider)
        return {"ok": True, "config": _redact_config_for_api(current)}

    return await asyncio.to_thread(_do)


@app.put("/api/mailboxes")
async def put_mailboxes(body: MailboxesUpdate):
    # Mailbox imports are additive configuration writes.  Registration and
    # reauthentication keep their own mailbox/config snapshots, while the
    # edit transaction serializes concurrent writers.  Do not block importing
    # new credentials just because an unrelated batch is active.
    def _do():
        with _edit_config() as cfg:
            ptype = normalize_provider_type(body.provider_type or "outlook_token")
            if ptype not in CANONICAL_PROVIDER_TYPES:
                raise HTTPException(400, f"unsupported provider_type: {body.provider_type}")
            prov = _ensure_provider(cfg, ptype)
            prov["type"] = ptype
            merged_text, import_stats = _merge_mailbox_credentials(
                str(prov.get("mailboxes") or ""),
                body.mailboxes,
                ptype,
                append=body.append,
            )
            if body.mailboxes.strip() and import_stats["imported"] == 0:
                raise HTTPException(400, "未识别到有效邮箱凭证，请检查每行格式")
            progress_key = f"mail_import_{ptype}"
            total_lines = len(
                [line for line in body.mailboxes.splitlines() if line.strip()]
            )
            PROGRESS.start(
                progress_key,
                (
                    "iCloud 邮箱导入"
                    if ptype == "icloud_api"
                    else "Maddy WebUI 配置"
                    if ptype == "maddy_webui"
                    else "域名邮箱配置"
                    if ptype == "domain_api"
                    else "Fastmail 配置"
                    if ptype == "fastmail_api"
                    else "Outlook 邮箱导入"
                ),
                total=total_lines,
                stage="校验并合并凭证",
            )
            PROGRESS.update(
                progress_key, percent=65, done=import_stats.get("imported", 0)
            )
            prov["mailboxes"] = merged_text
            if body.enable is not None:
                prov["enable"] = bool(body.enable)
            else:
                prov.setdefault("enable", True)
            if body.label:
                prov["label"] = body.label
            if ptype == "outlook_token":
                if body.mode:
                    prov["mode"] = body.mode
            elif ptype == "domain_api":
                prov["mode"] = "api"
                if body.api_base is not None:
                    prov["api_base"] = (
                        (body.api_base or "").strip() or DOMAIN_API_DEFAULT_BASE
                    )
                else:
                    prov.setdefault("api_base", DOMAIN_API_DEFAULT_BASE)
                if body.username is not None:
                    prov["username"] = str(body.username or "").strip()
                if body.password is not None and str(body.password or "").strip():
                    prov["password"] = str(body.password or "").strip()
                if body.default_domain is not None:
                    prov["default_domain"] = str(body.default_domain or "").strip()
                    prov["domain"] = prov["default_domain"]
                if body.domains is not None:
                    seen_domains: set[str] = set()
                    cleaned_domains: list[str] = []
                    for value in body.domains:
                        domain = str(value or "").strip()
                        key = domain.lower()
                        if not domain or key in seen_domains:
                            continue
                        seen_domains.add(key)
                        cleaned_domains.append(domain)
                    prov["domains"] = cleaned_domains
                if body.domain_index is not None:
                    prov["domain_index"] = max(0, int(body.domain_index or 0))
                if body.local_length is not None:
                    prov["local_length"] = max(8, min(30, int(body.local_length or 12)))
                if body.capacity is not None:
                    prov["capacity"] = max(1, min(500, int(body.capacity or 1)))
                prov.setdefault("local_length", DOMAIN_API_DEFAULT_LOCAL_LENGTH)
                prov.setdefault("capacity", DOMAIN_API_DEFAULT_CAPACITY)
            elif ptype == "maddy_webui":
                prov["mode"] = "api"
                if body.api_base is not None:
                    prov["api_base"] = (
                        (body.api_base or "").strip() or MADDY_WEBUI_DEFAULT_BASE
                    )
                else:
                    prov.setdefault("api_base", MADDY_WEBUI_DEFAULT_BASE)
                api_key = _maddy_secret_input(body.api_key)
                api_token = _maddy_secret_input(body.api_token)
                if api_key:
                    prov["api_key"] = api_key
                    prov.pop("api_token", None)
                elif api_token:
                    prov["api_token"] = api_token
                if body.default_domain is not None:
                    prov["default_domain"] = str(body.default_domain or "").strip()
                    prov["domain"] = prov["default_domain"]
                if body.domains is not None:
                    seen_domains: set[str] = set()
                    cleaned_domains: list[str] = []
                    blocked_domains = _maddy_blocked_domains(prov)
                    for value in body.domains:
                        domain = str(value or "").strip()
                        key = domain.lower()
                        if not domain or key in seen_domains or key in blocked_domains:
                            continue
                        seen_domains.add(key)
                        cleaned_domains.append(domain)
                    prov["domains"] = cleaned_domains
                if body.local_length is not None:
                    prov["local_length"] = max(8, min(30, int(body.local_length or 12)))
                if body.capacity is not None:
                    prov["capacity"] = max(1, min(500, int(body.capacity or 1)))
                if body.mailbox_password is not None and str(body.mailbox_password or "").strip():
                    prov["mailbox_password"] = str(body.mailbox_password or "").strip()
                prov.setdefault("local_length", MADDY_WEBUI_DEFAULT_LOCAL_LENGTH)
                prov.setdefault("capacity", MADDY_WEBUI_DEFAULT_CAPACITY)
                _sanitize_maddy_provider_entry(prov)
            elif ptype == "fastmail_api":
                prov["mode"] = "api"
                if body.api_base is not None:
                    prov["api_base"] = (
                        (body.api_base or "").strip() or FASTMAIL_API_DEFAULT_BASE
                    )
                else:
                    prov.setdefault("api_base", FASTMAIL_API_DEFAULT_BASE)
                if body.admin_key is not None and str(body.admin_key or "").strip():
                    prov["admin_key"] = str(body.admin_key or "").strip()
                    prov.pop("api_key", None)
                elif body.api_key is not None and str(body.api_key or "").strip():
                    prov["api_key"] = str(body.api_key or "").strip()
                if body.accounts is not None:
                    prov["accounts"] = str(body.accounts or "")
                if body.capacity is not None:
                    prov["capacity"] = max(1, min(1000, int(body.capacity or 1)))
                prov.setdefault("capacity", FASTMAIL_API_DEFAULT_CAPACITY)
                prov.setdefault("accounts", "")
            elif body.api_base is not None:
                prov["api_base"] = (
                    (body.api_base or "").strip() or "https://icloud.xbovo.online"
                )
            else:
                prov.setdefault("api_base", "https://icloud.xbovo.online")
        PROGRESS.finish(
            progress_key,
            message=(
                f"新增 {import_stats.get('added', 0)}，"
                f"更新 {import_stats.get('updated', 0)}"
            ),
        )
        return {
            "ok": True,
            **import_stats,
            "mail": _public_mail_stats(cfg),
        }

    return await asyncio.to_thread(_do)


@app.post("/api/domain-mail/domains")
async def domain_mail_domains(body: DomainMailDomainsRequest):
    def _do():
        cfg = _read_config()
        saved = _find_provider(cfg, "domain_api") or {}
        entry = dict(saved, type="domain_api")
        if body.api_base is not None:
            entry["api_base"] = (body.api_base or "").strip() or DOMAIN_API_DEFAULT_BASE
        if body.username is not None:
            entry["username"] = str(body.username or "").strip()
        if body.password is not None and str(body.password or "").strip():
            entry["password"] = str(body.password or "").strip()
        conf = _make_config(cfg.get("mail") if isinstance(cfg.get("mail"), dict) else {})
        conf["request_timeout"] = min(float(conf.get("request_timeout") or 30), 10.0)
        provider = DomainApiProvider(entry, conf)
        try:
            items = provider.fetch_domains(force=True)
        finally:
            provider.close()
        default_domain = str(entry.get("default_domain") or entry.get("domain") or "")
        return {"ok": True, "items": items, "default_domain": default_domain}

    return await asyncio.to_thread(_do)


def _maddy_webui_domains_payload(cfg: dict[str, Any], body: MaddyWebuiDomainsRequest) -> dict[str, Any]:
    saved = _find_provider(cfg, "maddy_webui") or {}
    entry = dict(saved, type="maddy_webui")
    if body.api_base is not None:
        entry["api_base"] = (body.api_base or "").strip() or MADDY_WEBUI_DEFAULT_BASE
    api_key = _maddy_secret_input(body.api_key)
    api_token = _maddy_secret_input(body.api_token)
    if api_key:
        entry["api_key"] = api_key
        entry.pop("api_token", None)
    elif api_token:
        entry["api_token"] = api_token
    conf = _make_config(cfg.get("mail") if isinstance(cfg.get("mail"), dict) else {})
    conf["request_timeout"] = min(float(conf.get("request_timeout") or 30), 10.0)
    provider = MaddyWebuiProvider(entry, conf)
    try:
        info = provider.fetch_domain_info(force=True)
    finally:
        provider.close()
    items = _maddy_allowed_domains(entry, info.get("domains") or [])
    primary_domain = str(info.get("primary_domain") or "").strip().lower()
    blocked_domains = _maddy_blocked_domains(entry)
    if primary_domain in blocked_domains:
        primary_domain = ""
    if not primary_domain and items:
        primary_domain = items[0]
    default_domain = primary_domain or str(entry.get("default_domain") or entry.get("domain") or "").strip().lower()
    if default_domain in blocked_domains:
        default_domain = items[0] if items else ""
    if not default_domain and items:
        default_domain = items[0]
    return {
        "ok": True,
        "items": items,
        "default_domain": default_domain,
        "primary_domain": primary_domain,
        "count": len(items),
    }


@app.post("/api/maddy-webui/domains")
async def maddy_webui_domains(body: MaddyWebuiDomainsRequest):
    def _do():
        return _maddy_webui_domains_payload(_read_config(), body)

    return await asyncio.to_thread(_do)


@app.post("/api/fastmail/accounts")
async def fastmail_accounts(body: FastmailAccountsRequest):
    """List mailcode-api master accounts and their alias pool status."""

    def _do():
        cfg = _read_config()
        saved = _find_provider(cfg, "fastmail_api") or {}
        entry = dict(saved, type="fastmail_api")
        if body.api_base is not None:
            entry["api_base"] = (body.api_base or "").strip() or FASTMAIL_API_DEFAULT_BASE
        if body.admin_key is not None and str(body.admin_key or "").strip():
            entry["admin_key"] = str(body.admin_key or "").strip()
        conf = _make_config(cfg.get("mail") if isinstance(cfg.get("mail"), dict) else {})
        conf["request_timeout"] = min(float(conf.get("request_timeout") or 30), 10.0)
        provider = FastmailApiProvider(entry, conf)
        try:
            rows = provider.fetch_accounts(force=True)
        finally:
            provider.close()
        items = [
            {
                "email": str(row.get("email") or ""),
                "enabled": bool(row.get("enabled", True)),
                "refill_enabled": bool(row.get("refillEnabled", True)),
                "pool_size": int(row.get("poolSize") or 0),
                "alias_count": int(row.get("aliasCount") or 0),
                "available_count": int(row.get("availableCount") or 0),
                "expired_count": int(row.get("expiredCount") or 0),
                "last_check_at": str(row.get("lastCheckAt") or ""),
            }
            for row in rows
            if isinstance(row, dict)
        ]
        return {
            "ok": True,
            "items": items,
            "available": sum(item["available_count"] for item in items),
            "total": sum(item["alias_count"] for item in items),
        }

    return await asyncio.to_thread(_do)


@app.put("/api/proxy")
async def put_proxy(body: ProxyUpdate):
    # Proxy settings are persisted independently from an active batch. A
    # request already in flight keeps its selected proxy; protocol workers
    # refresh this saved pool before the next task or retry.
    country_pools = read_registration_proxy_pools()
    if body.country_pools is not None:
        try:
            country_pools = write_registration_proxy_pools(body.country_pools)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    selected_country = str(body.selected_country or "").upper().strip()
    known_countries = {item["country"]: item for item in country_pools["items"]}
    if selected_country and selected_country not in known_countries:
        raise HTTPException(400, "当前国家未在主注册代理池中")
    mode = body.mode if body.mode in {"single_proxy", "pool", "country_pool"} else "single_proxy"
    if mode == "country_pool":
        if not selected_country:
            raise HTTPException(400, "请选择主注册代理国家")
        if not int(known_countries[selected_country].get("count") or 0):
            raise HTTPException(400, "所选国家没有可用代理")

    with _edit_config() as cfg:
        proxy = cfg.setdefault("proxy", {})
        proxy["enabled"] = body.enabled
        proxy["mode"] = mode
        proxy["url"] = body.url or ""
        proxy["resource_url"] = body.resource_url or ""
        proxy["selected_country"] = selected_country
        proxy["rotate_per_task"] = body.rotate_per_task
        if body.clearance is not None:
            raw = body.clearance
            mode = str(raw.get("mode") or "none").strip().lower()
            proxy["clearance"] = {
                "enabled": bool(raw.get("enabled")),
                "mode": mode if mode in {"none", "flaresolverr"} else "none",
                "flaresolverr_url": str(raw.get("flaresolverr_url") or "").strip(),
                "proxy_url": str(raw.get("proxy_url") or "").strip(),
                "timeout_sec": max(1, min(300, int(raw.get("timeout_sec") or 60))),
                "refresh_interval": max(0, int(raw.get("refresh_interval") or 3600)),
            }
        pool_file = (body.pool_file or "").strip()
        if body.pool_text is not None:
            if not pool_file:
                pool_file = str(DATA_DIR / "proxy_pool.txt")
            path = Path(pool_file).expanduser()
            if not path.is_absolute():
                path = (ROOT / path).resolve()
            else:
                path = path.resolve()
            try:
                path.relative_to(DATA_DIR.resolve())
            except ValueError:
                raise HTTPException(400, "代理池文件必须位于项目 data 目录")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body.pool_text.rstrip() + "\n", encoding="utf-8")
            try:
                rel = path.relative_to(ROOT)
                pool_file = str(rel)
            except ValueError:
                pool_file = str(path)
        proxy["pool_file"] = pool_file
    return {"ok": True, "proxy": _proxy_pool_preview(cfg)}


@app.post("/api/proxy/test")
async def proxy_test(body: ProxyTestRequest):
    from register.batch import resolve_proxy_pool

    cfg = _read_config()
    proxy_cfg = cfg.get("proxy") if isinstance(cfg.get("proxy"), dict) else {}
    proxies = [str(value or "").strip() for value in body.proxies if str(value or "").strip()]
    if not proxies:
        proxies = resolve_proxy_pool(proxy_cfg)
        single = str(proxy_cfg.get("url") or "").strip()
        if not proxies and single:
            proxies = [single]
    if not proxies:
        raise HTTPException(400, "没有可测试的代理")
    targets = proxies[:100]
    PROGRESS.start("proxy_test", "代理批量检测", total=len(targets), stage="检测出口连通性")
    items: list[dict[str, Any]] = []
    try:
        tasks = [asyncio.create_task(asyncio.to_thread(probe_proxy, proxy)) for proxy in targets]
        for future in asyncio.as_completed(tasks):
            items.append(await future)
            PROGRESS.update(
                "proxy_test",
                done=len(items),
                total=len(targets),
                stage=f"已检测 {len(items)} / {len(targets)}",
            )
    except Exception as exc:
        PROGRESS.finish("proxy_test", ok=False, message=str(exc))
        raise
    alive = sum(1 for item in items if item.get("ok"))
    PROGRESS.finish("proxy_test", message=f"可用 {alive} / {len(items)}")
    return {
        "ok": True,
        "count": len(items),
        "alive": alive,
        "items": items,
    }


@app.get("/api/chatgpt2api")
async def get_chatgpt2api():
    return {"chatgpt2api": _c2a_public(_read_config())}


@app.put("/api/chatgpt2api")
async def put_chatgpt2api(body: ChatGPT2APIUpdate):
    if JOB.running:
        raise HTTPException(409, "job is running; stop it before saving config")
    from common.chatgpt2api import normalize_c2a_url

    with _edit_config() as cfg:
        c2a = cfg.setdefault("chatgpt2api", {})
        c2a["enabled"] = bool(body.enabled)
        c2a["url"] = normalize_c2a_url(body.url) or "https://chatgpt2api.xbovo.online"
        new_key = str(body.key or "").strip()
        if new_key and "…" not in new_key and "***" not in new_key:
            c2a["key"] = new_key
        c2a["auto_upload"] = bool(body.auto_upload)
    return {"ok": True, "chatgpt2api": _c2a_public(cfg)}


@app.post("/api/chatgpt2api/test")
async def test_chatgpt2api(body: ChatGPT2APITestRequest):
    from common.chatgpt2api import c2a_config_from, test_connection

    cfg = _read_config()
    c = c2a_config_from(cfg)
    url = (body.url or "").strip() or c.get("url") or ""
    key = (body.key or "").strip() or c.get("key") or ""
    ok, msg = await asyncio.to_thread(test_connection, url, key)
    return {"ok": ok, "message": msg, "url": url}


@app.post("/api/chatgpt2api/upload-all")
async def upload_all_chatgpt2api(body: ChatGPT2APIUploadRequest | None = None):
    from common.chatgpt2api import (
        c2a_config_from,
        collect_local_accounts,
        import_accounts,
        normalize_c2a_url,
    )

    cfg = _read_config()
    c = c2a_config_from(cfg)
    body = body or ChatGPT2APIUploadRequest()
    url = normalize_c2a_url((body.url or "").strip() or c.get("url") or "")
    key = (body.key or "").strip() or c.get("key") or ""
    if not url or not key:
        raise HTTPException(400, "请先配置 chatgpt2api 网址和 admin key")
    group_id = str(body.group_id or "").strip()
    groups = _groups_with_counts()
    default_group_id = str(groups[0]["id"])
    group = next((item for item in groups if item["id"] == group_id), None) if group_id else None
    if group_id and group is None:
        raise HTTPException(404, "分组不存在")
    group_count = (group or {}).get("account_count")
    if group is not None and group_count is not None:
        try:
            expected = max(0, int(group_count))
        except (TypeError, ValueError):
            expected = await asyncio.to_thread(count_accounts, ACCOUNTS_FILE)
    else:
        expected = await asyncio.to_thread(count_accounts, ACCOUNTS_FILE)
    PROGRESS.start(
        "c2a_upload",
        "chatgpt2api 账号上传",
        total=expected,
        stage="筛选分组与有效账号",
    )
    accounts = await asyncio.to_thread(
        collect_local_accounts,
        group_id=group_id,
        default_group_id=default_group_id,
        accounts_path=ACCOUNTS_FILE,
    )
    if not accounts:
        scope = f"分组“{group['name']}”" if group else "本地"
        PROGRESS.finish("c2a_upload", ok=False, message=f"{scope}没有有效账号")
        return {
            "ok": False,
            "message": f"{scope}没有可上传的有效账号",
            "count": 0,
            "group_id": group_id,
            "group_name": str((group or {}).get("name") or ""),
        }
    PROGRESS.update(
        "c2a_upload",
        done=0,
        total=len(accounts),
        percent=30,
        stage=f"正在提交 {len(accounts)} 个账号到远端",
    )
    try:
        upload_result = await asyncio.to_thread(import_accounts, url, key, accounts)
    except Exception as exc:
        PROGRESS.finish("c2a_upload", ok=False, message=str(exc))
        raise
    ok, msg, data = upload_result
    PROGRESS.update(
        "c2a_upload",
        done=len(accounts),
        total=len(accounts),
        percent=95,
        stage="记录上传结果",
    )
    uploaded_emails = {
        str(item.get("email") or "").strip().lower()
        for item in accounts
        if isinstance(item, dict) and str(item.get("email") or "").strip()
    }
    upload_keys = [
        _account_key(item)
        for item in accounts
        if str(item.get("email") or "").strip().lower() in uploaded_emails
        and _account_key(item)
    ]
    if upload_keys:
        await asyncio.to_thread(
            update_account_records,
            upload_keys,
            {
                "c2a_upload_status": "success" if ok else "failed",
                "c2a_uploaded_at": _now_iso(),
                "c2a_upload_error": "" if ok else msg[:300],
            },
            path=ACCOUNTS_FILE,
        )
    PROGRESS.finish("c2a_upload", ok=ok, message=msg)
    return {
        "ok": ok,
        "message": msg,
        "count": len(accounts),
        "result": data,
        "url": url,
        "group_id": group_id,
        "group_name": str((group or {}).get("name") or ""),
    }


@app.get("/api/accounts")
async def accounts(limit: int = 200, offset: int = 0):
    def _do():
        cap = max(1, min(limit, 1000))
        start = max(0, offset)

        def _stamp(path: Path) -> int:
            try:
                return path.stat().st_mtime_ns
            except OSError:
                return 0

        cfg = _read_config()

        class _RevisionChanged(RuntimeError):
            pass

        # Count, group metadata and the requested rows are read through
        # separate short-lived SQLite connections. Pin the response cache key
        # to one store revision and only publish a page if no writer committed
        # while those reads were being assembled. The browser uses this same
        # revision to reject mixed-version page batches.
        for _attempt in range(5):
            revision = account_store_revision(ACCOUNTS_FILE)
            groups_stamp = _stamp(GROUPS_FILE)
            cdks_stamp = _stamp(CDKS_FILE)
            cache_key = (cap, start, revision, groups_stamp, cdks_stamp)
            redis_key = ":".join(
                (
                    "accounts:v3",
                    str(cap),
                    str(start),
                    str(revision),
                    _account_data_stamp(),
                    str(groups_stamp),
                    str(cdks_stamp),
                )
            )

            def _build_payload(expected_revision: int = revision) -> dict[str, Any]:
                now = time.monotonic()
                with _ACCOUNTS_API_CACHE_LOCK:
                    cached = _ACCOUNTS_API_CACHE.get(cache_key)
                    if cached and now - cached[0] < 5.0:
                        return cached[1]

                if account_store_revision(ACCOUNTS_FILE) != expected_revision:
                    raise _RevisionChanged
                total = count_accounts(ACCOUNTS_FILE)
                groups = _groups_with_counts()
                names = {item["id"]: item["name"] for item in groups}
                default_id = groups[0]["id"]
                cdk_marks = _cdk_account_marks()
                items = load_account_public_views(cap, ACCOUNTS_FILE, offset=start)
                if account_store_revision(ACCOUNTS_FILE) != expected_revision:
                    raise _RevisionChanged

                slim = []
                for a in items:
                    if not isinstance(a, dict):
                        continue
                    item = dict(a)
                    group_id = str(item.get("group_id") or "").strip()
                    if group_id not in names:
                        group_id = default_id
                    item["group_id"] = group_id
                    item["group_name"] = names[group_id]
                    item.update(
                        cdk_marks.get(
                            str(item.get("delete_key") or ""),
                            {
                                "cdk_bound": False,
                                "cdk_status": "",
                                "cdk_redeemed": False,
                                "cdk_redeemed_at": "",
                                "cdk_last_access_at": "",
                            },
                        )
                    )
                    slim.append(item)
                if account_store_revision(ACCOUNTS_FILE) != expected_revision:
                    raise _RevisionChanged
                payload = {
                    "count": total,
                    "total": total,
                    "revision": expected_revision,
                    "offset": start,
                    "limit": cap,
                    "returned": len(slim),
                    "items": slim,
                }
                with _ACCOUNTS_API_CACHE_LOCK:
                    expired = [
                        key for key, value in _ACCOUNTS_API_CACHE.items()
                        if now - value[0] >= 5.0
                    ]
                    for key in expired:
                        _ACCOUNTS_API_CACHE.pop(key, None)
                    _ACCOUNTS_API_CACHE[cache_key] = (now, payload)
                    while len(_ACCOUNTS_API_CACHE) > 8:
                        _ACCOUNTS_API_CACHE.pop(next(iter(_ACCOUNTS_API_CACHE)))
                return payload

            try:
                # The builder validates the revision immediately before
                # publishing a newly assembled payload. A cache hit is already
                # keyed by this revision; do not open another SQLite
                # connection here, since every page refresh would pay that
                # extra round-trip.
                return _cached_json_response(
                    cfg,
                    redis_key,
                    "accounts_ttl",
                    300,
                    _build_payload,
                )
            except _RevisionChanged:
                continue
        raise RuntimeError("账号列表在持续更新，请稍后重试")

    return await asyncio.to_thread(_do)


@app.post("/api/accounts/trial-eligibility")
async def accounts_trial_eligibility(body: AccountTrialEligibilityRequest):
    """Queue direct Plus/free-trial checks for selected or all accounts."""
    records = await asyncio.to_thread(_load_accounts)
    wanted = {str(key or "").strip() for key in body.keys if str(key or "").strip()}
    selected = [
        record for record in records
        if (body.all_accounts or _account_key(record) in wanted)
        and str(record.get("plan_type") or "free").strip().lower() == "free"
        and not account_is_deactivated(record)
    ]
    if not selected:
        raise HTTPException(400, "没有可检测的 Free 账号")
    effective_concurrency, resource_mode = _trial_eligibility_resource_budget(
        body.concurrency
    )
    try:
        job = await asyncio.to_thread(
            TRIAL_ELIGIBILITY_JOBS.start,
            selected,
            concurrency=effective_concurrency,
            attempts=8,
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    return {
        "ok": True,
        **job,
        "requested": len(
            [
                record
                for record in records
                if (body.all_accounts or _account_key(record) in wanted)
                and str(record.get("plan_type") or "free").strip().lower() == "free"
            ]
        ),
        "skipped_known_deactivated": max(
            0,
            len(
                [
                    record
                    for record in records
                    if (body.all_accounts or _account_key(record) in wanted)
                    and str(record.get("plan_type") or "free").strip().lower() == "free"
                    and account_is_deactivated(record)
                ]
            ),
        ),
        "requested_concurrency": body.concurrency,
        "effective_concurrency": effective_concurrency,
        "resource_mode": resource_mode,
    }


@app.get("/api/accounts/trial-eligibility/progress")
async def accounts_trial_eligibility_progress():
    return {"ok": True, "job": await asyncio.to_thread(TRIAL_ELIGIBILITY_JOBS.active)}


@app.get("/api/accounts/trial-eligibility/{job_id}")
async def accounts_trial_eligibility_job(job_id: str):
    job = await asyncio.to_thread(TRIAL_ELIGIBILITY_JOBS.snapshot, job_id)
    if job is None:
        raise HTTPException(404, "优惠检测任务不存在或已清理")
    return {"ok": True, **job}


@app.post("/api/accounts/trial-eligibility/{job_id}/stop")
async def accounts_trial_eligibility_stop(job_id: str):
    job = await asyncio.to_thread(TRIAL_ELIGIBILITY_JOBS.stop, job_id)
    if job is None:
        raise HTTPException(404, "优惠检测任务不存在或已清理")
    return {"ok": True, **job}


async def _token_trial_eligibility_core(token: str, use_configured_proxy: bool) -> dict[str, Any]:
    """Single-token check shared by the single and batch endpoints."""
    country = ""
    if use_configured_proxy:
        country, first_proxy = await asyncio.to_thread(selected_trial_proxy)
        if first_proxy:
            _, remaining_proxies = await asyncio.to_thread(
                selected_trial_proxies,
                limit=4,
                exclude={first_proxy},
            )
            attempts = [first_proxy, *remaining_proxies]
        else:
            attempts = [""]
    else:
        attempts = [""]

    result: dict[str, Any] | None = None
    last_error: Exception | None = None
    for attempt, proxy in enumerate(attempts, 1):
        try:
            result = await asyncio.to_thread(
                check_trial_eligibility,
                {"access_token": token},
                proxy=proxy,
                direct=not bool(proxy),
            )
            break
        except InvalidAccessTokenError as error:
            raise HTTPException(400, "Access Token 已失效或无效") from error
        except Exception as error:
            last_error = error
            if not is_trial_probe_retryable(error) or attempt >= len(attempts):
                break
    if result is None:
        error = last_error or TrialEligibilityError("资格检测失败")
        if isinstance(error, TrialEligibilityError):
            raise HTTPException(502, f"资格接口检测失败: {str(error)[:240]}") from error
        raise HTTPException(502, f"资格检测请求失败: {str(error)[:240]}") from error

    campaigns = result.get("eligible_promo_campaigns")
    campaigns = campaigns if isinstance(campaigns, dict) else {}
    active_campaigns = {
        str(name): value
        for name, value in campaigns.items()
        if value
    }
    return {
        "ok": True,
        **result,
        "eligible_promo_campaigns": active_campaigns,
        "has_promotion": bool(active_campaigns),
        "promotion_count": len(active_campaigns),
        "promotion_keys": list(active_campaigns),
        "proxy_country": country if attempts and attempts[0] else "",
    }


def _normalize_access_token(raw: str) -> str:
    token = str(raw or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


@app.post("/api/trial-eligibility/check-token")
async def token_trial_eligibility(body: TokenTrialEligibilityRequest):
    """Check one supplied AT without persisting or returning the credential."""
    token = _normalize_access_token(body.access_token)
    if len(token) < 20 or len(token) > 20_000:
        raise HTTPException(400, "请输入完整的 Access Token")
    return await _token_trial_eligibility_core(token, body.use_configured_proxy)


@app.post("/api/trial-eligibility/check-tokens")
async def token_trial_eligibility_batch(body: TokenTrialEligibilityBatchRequest):
    """Batch-check supplied ATs with bounded concurrency."""
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in body.access_tokens:
        token = _normalize_access_token(raw)
        if len(token) < 20 or len(token) > 20_000 or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    if not tokens:
        raise HTTPException(400, "请输入至少一个有效的 Access Token")
    if len(tokens) > 200:
        raise HTTPException(400, "单次最多批量检测 200 个 Access Token")

    workers = max(1, min(int(body.workers), 8))
    semaphore = asyncio.Semaphore(workers)

    async def run_one(index: int, token: str) -> dict[str, Any]:
        tail = token[-10:]
        async with semaphore:
            try:
                payload = await _token_trial_eligibility_core(token, body.use_configured_proxy)
            except HTTPException as error:
                detail = str(error.detail) if error.detail is not None else "检测失败"
                return {"index": index, "token_tail": tail, "ok": False, "error": detail[:240]}
            except Exception as error:
                return {"index": index, "token_tail": tail, "ok": False, "error": str(error)[:240]}
            return {"index": index, "token_tail": tail, **payload}

    results = list(await asyncio.gather(*(run_one(i, token) for i, token in enumerate(tokens))))
    eligible = sum(1 for item in results if item.get("ok") and item.get("eligible"))
    plus_active = sum(1 for item in results if item.get("ok") and item.get("has_active_subscription"))
    banned = sum(1 for item in results if item.get("ok") and item.get("is_deactivated"))
    failed = sum(1 for item in results if not item.get("ok"))
    return {
        "ok": True,
        "total": len(results),
        "eligible": eligible,
        "plus_active": plus_active,
        "banned": banned,
        "failed": failed,
        "workers": workers,
        "results": results,
    }


@app.get("/api/trial-proxy-pool")
async def trial_proxy_pool():
    return {"ok": True, **await asyncio.to_thread(read_trial_proxy_config)}


@app.put("/api/trial-proxy-pool")
async def trial_proxy_pool_save(body: TrialProxyConfigRequest):
    try:
        config = await asyncio.to_thread(
            write_trial_proxy_config,
            body.selected_country,
            [item.model_dump() for item in body.countries],
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return {"ok": True, **config}


@app.post("/api/accounts/access-token")
async def account_access_token(body: AccountTokenRequest):
    """Return one explicitly requested stored AT without exposing it in the list."""
    target = str(body.key or "").strip()
    records = await asyncio.to_thread(_records_by_keys_with_fallback, [target])
    account = records.get(target)
    if account is None:
        raise HTTPException(404, "账号不存在")
    token = _stored_access_token(account)
    if not token:
        raise HTTPException(409, "该账号没有可复制的 AT")
    return JSONResponse(
        {"ok": True, "access_token": token},
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def _selected_access_tokens(keys: list[str]) -> tuple[list[str], int]:
    """Return stored AT values in requested account order."""
    wanted = list(dict.fromkeys(str(key or "").strip() for key in keys))
    wanted = [key for key in wanted if key]
    records_by_key = load_account_records_by_keys(wanted, path=ACCOUNTS_FILE)
    tokens: list[str] = []
    missing = 0
    for key in wanted:
        account = records_by_key.get(key)
        token = _stored_access_token(account) if account is not None else ""
        if not token or "\n" in token or "\r" in token:
            missing += 1
            continue
        tokens.append(token)
    return tokens, missing


@app.post("/api/accounts/access-tokens")
async def accounts_access_tokens(body: BulkDeleteRequest):
    """Return selected stored AT values for an explicit clipboard action."""
    if not any(str(key or "").strip() for key in body.keys):
        raise HTTPException(400, "请选择要复制 AT 的账号")
    tokens, missing = await asyncio.to_thread(_selected_access_tokens, body.keys)
    return JSONResponse(
        {
            "ok": True,
            "tokens": tokens,
            "exported": len(tokens),
            "missing": missing,
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@app.post("/api/accounts/export-access-tokens")
async def accounts_export_access_tokens(body: BulkDeleteRequest):
    """Download selected stored AT values, one token per line."""
    if not any(str(key or "").strip() for key in body.keys):
        raise HTTPException(400, "请选择要导出 AT 的账号")
    tokens, missing = await asyncio.to_thread(_selected_access_tokens, body.keys)
    payload = ("\n".join(tokens) + ("\n" if tokens else "")).encode("utf-8")
    headers = {
        "Content-Disposition": 'attachment; filename="accounts.access-tokens.selected.txt"',
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "X-Exported-Count": str(len(tokens)),
        "X-Missing-Count": str(missing),
    }
    return StreamingResponse(
        iter([payload]),
        media_type="text/plain; charset=utf-8",
        headers=headers,
    )


@app.post("/api/accounts/credentials")
async def account_credentials(body: AccountCredentialsRequest):
    """Return one explicitly requested password, TOTP secret, or current code."""
    field = str(body.field or "").strip().lower()
    if field not in {"password", "totp_secret", "totp_code"}:
        raise HTTPException(400, "不支持的凭据字段")
    target = str(body.key or "").strip()
    records = await asyncio.to_thread(_records_by_keys_with_fallback, [target])
    account = records.get(target)
    if account is None:
        raise HTTPException(404, "账号不存在")

    if field == "password":
        value = str(account.get("password") or "").strip()
        if not value:
            raise HTTPException(409, "该账号没有已保存的密码")
        payload = {"ok": True, "field": field, "value": value}
    elif field == "totp_secret":
        value = str(account.get("totp_secret") or "").strip()
        if not value:
            raise HTTPException(409, "该账号没有已保存的 TOTP 密钥")
        payload = {"ok": True, "field": field, "value": value}
    else:
        secret = str(account.get("totp_secret") or "").strip()
        if not secret:
            raise HTTPException(409, "该账号没有已保存的 TOTP 密钥")
        now = time.time()
        try:
            code = totp_code(secret, now)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        period = 30
        expires_at = (int(now) // period + 1) * period
        payload = {
            "ok": True,
            "field": field,
            "value": code,
            "code": code,
            "expires_at": expires_at,
            "remaining": max(0, expires_at - int(now)),
        }
    return JSONResponse(
        payload,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@app.get("/api/cdks")
async def cdks(group_id: str = ""):
    return await asyncio.to_thread(_cdk_admin_payload, group_id)


@app.post("/api/cdks/generate")
async def cdks_generate(body: CDKGenerateRequest):
    account_key = str(body.account_key or "").strip()
    group_id = str(body.group_id or "").strip()
    if account_key:
        account = await asyncio.to_thread(
            lambda: _records_by_keys_with_fallback([account_key]).get(account_key)
        )
        if account is None:
            raise HTTPException(404, "指定账号不存在")
        group_id = str(account.get("group_id") or "").strip()
        if not group_id:
            raise HTTPException(400, "指定账号没有有效分组")
    if not group_id:
        raise HTTPException(400, "请选择账号分组")
    try:
        bindings, eligibility = await asyncio.to_thread(
            _eligible_cdk_bindings, group_id
        )
    except KeyError as error:
        raise HTTPException(404, str(error).strip("'")) from error
    selected = (
        [item for item in bindings if str(item.get("account_key") or "") == account_key]
        if account_key
        else bindings[: body.count]
    )
    if not selected:
        message = "指定账号无法生成 CDK：可能未在邮箱池、已失效或已绑定 CDK" if account_key else "该分组没有可生成 CDK 的账号"
        raise HTTPException(400, message)
    created = await asyncio.to_thread(
        create_cdks,
        selected,
        expires_days=body.expires_days,
        path=CDKS_FILE,
    )
    if not created:
        raise HTTPException(409, "所选账号已绑定 CDK，请刷新后重试")
    codes = [
        {
            "id": str(item.get("id") or ""),
            "code": str(item.get("code") or ""),
            "email": str(item.get("email") or ""),
            "expires_at": str(item.get("expires_at") or ""),
        }
        for item in created
    ]
    for item in created:
        record_operation_event(
            {
                "email": item.get("email"),
                "account_id": item.get("account_id"),
            },
            "cdk_generate",
            "success",
            message="CDK 生成成功",
            metadata={
                "cdk_id": str(item.get("id") or ""),
                "group_id": str(item.get("group_id") or group_id),
                "expires_at": str(item.get("expires_at") or ""),
            },
        )
    return {
        "ok": True,
        "created": len(codes),
        "codes": codes,
        "eligibility": eligibility,
        "group_id": group_id,
        "message": f"已为指定账号生成 CDK" if account_key else f"已生成 {len(codes)} 个 CDK",
    }


@app.post("/api/cdks/status")
async def cdks_status(body: CDKStatusRequest):
    try:
        changed, rows = await asyncio.to_thread(
            set_cdk_status, body.ids, body.status, CDKS_FILE
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    wanted_ids = {str(value or "").strip() for value in body.ids if str(value or "").strip()}
    for item in rows:
        if str(item.get("id") or "") not in wanted_ids:
            continue
        record_operation_event(
            {
                "email": item.get("email"),
                "account_id": item.get("account_id"),
            },
            "cdk_status",
            "success",
            message="CDK 状态已更新",
            metadata={
                "cdk_id": str(item.get("id") or ""),
                "status": str(item.get("status") or body.status),
            },
        )
    return {
        "ok": True,
        "changed": changed,
        "summary": cdk_summary(rows),
    }





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
        event = redact_operation_event(raw)
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
            target = ROOT / target
        target = target.resolve()
        stat = target.stat()
        return str(target), int(stat.st_mtime_ns), int(stat.st_size)
    except (OSError, TypeError, ValueError):
        return str(path or ""), 0, 0


def _account_log_store_stamp() -> tuple[tuple[str, int, int], ...]:
    db_path = Path(OPERATION_LOG_DB_FILE)
    account_db = Path(ACCOUNTS_DB_FILE)
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
    current_store_stamp = _account_log_store_stamp()
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
    stamp = (*_account_log_store_stamp(), *_account_log_trace_stamp(payload, trace_files))
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


@app.get("/api/accounts/logs/{delete_key:path}")
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
    cache_key = (key.casefold(), cap, direction, _account_log_store_stamp())
    cached = await asyncio.to_thread(_account_log_cache_begin, cache_key)
    if cached is not None:
        cached["account_key"] = key
        return cached
    try:
        items, total = await asyncio.to_thread(
            list_operation_events,
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
        matched = await asyncio.to_thread(_records_by_keys_with_fallback, [key])
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
                    list_registration_trace_events,
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





def _security_credential_lines(records: list[dict[str, Any]]) -> tuple[list[str], int]:
    lines: list[str] = []
    missing = 0
    for account in records:
        email = str(account.get("email") or "").strip()
        password = str(account.get("password") or "").strip()
        totp_secret = str(account.get("totp_secret") or "").strip()
        if not email or not password or not totp_secret:
            missing += 1
            continue
        lines.append("----".join((email, password, totp_secret)))
    return lines, missing


def _normalize_credential_export_emails(body: AccountEmailExportRequest) -> list[str]:
    values = [str(value or "") for value in body.emails]
    if body.text:
        values.append(str(body.text))
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        for candidate in re.split(r"[\s,，;；]+", value):
            email = candidate.strip().strip("`'\"").lower()
            if not email or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
                continue
            if email not in seen:
                seen.add(email)
                result.append(email)
    return result


@app.post("/api/accounts/export-security-by-email")
async def accounts_export_security_by_email(body: AccountEmailExportRequest):
    """Download credentials for accounts matched by an explicit email list."""
    requested = _normalize_credential_export_emails(body)
    if not requested:
        raise HTTPException(400, "请输入至少一个有效邮箱")
    # Public rows contain the stable delete key without decoding the large
    # session/cookie payload. Decode full records only for requested emails.
    public_rows = await asyncio.to_thread(load_account_public_views, None, ACCOUNTS_FILE)
    key_by_email = {
        str(record.get("email") or "").strip().lower(): str(record.get("delete_key") or "")
        for record in public_rows
        if str(record.get("email") or "").strip()
        and str(record.get("delete_key") or "").strip()
    }
    requested_keys = [key_by_email[email] for email in requested if email in key_by_email]
    records_by_key = await asyncio.to_thread(_records_by_keys_with_fallback, requested_keys)
    legacy_by_email: dict[str, dict[str, Any]] = {}
    # An unknown address alongside valid matches is already represented by the
    # missing count; do not re-read the full store just for that address.
    compatibility_loader = _load_accounts is not _DEFAULT_LOAD_ACCOUNTS
    legacy_store = (
        Path(ACCOUNTS_FILE).resolve() != (ROOT / "data" / "registered_accounts.json").resolve()
        or not Path(ACCOUNTS_DB_FILE).is_file()
    )
    if not records_by_key and (compatibility_loader or legacy_store):
        legacy_records = await asyncio.to_thread(_load_accounts)
        legacy_by_email = {
            str(record.get("email") or "").strip().lower(): record
            for record in legacy_records
            if str(record.get("email") or "").strip()
        }
    selected = []
    for email in requested:
        key = key_by_email.get(email)
        record = records_by_key.get(key) if key else None
        if record is None:
            record = legacy_by_email.get(email)
        if record is not None:
            selected.append(record)
    lines, missing = _security_credential_lines(selected)
    missing += len(requested) - len(selected)
    payload = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    headers = {
        "Content-Disposition": 'attachment; filename="accounts.security.by-email.txt"',
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "X-Requested-Count": str(len(requested)),
        "X-Matched-Count": str(len(selected)),
        "X-Exported-Count": str(len(lines)),
        "X-Missing-Count": str(missing),
    }
    return StreamingResponse(
        iter([payload]),
        media_type="text/plain; charset=utf-8",
        headers=headers,
    )


@app.post("/api/accounts/health")
async def accounts_health(body: AccountHealthRequest):
    from common.account_health import check_accounts

    if not body.delete_all and not body.keys:
        raise HTTPException(400, "请选择要测活的账号，或明确使用全部测活")
    records = await asyncio.to_thread(_load_accounts)
    keys = [] if body.delete_all else body.keys
    probe_records, skipped_records = select_health_check_records(records, keys=keys)
    requested = len(probe_records) + len(skipped_records)
    actual_total = len(probe_records)
    if not requested:
        raise HTTPException(404, "没有匹配到可测活的账号记录")
    if _account_health_progress_snapshot(include_updates=False).get("running"):
        raise HTTPException(409, "账号批量测活正在运行，请等待当前任务完成")
    _start_account_health_progress(
        actual_total,
        body.workers,
        requested=requested,
        skipped_known_deactivated=len(skipped_records),
    )
    try:
        result = await asyncio.to_thread(
            check_accounts,
            records=records,
            keys=keys,
            workers=body.workers,
            item_callback=_update_account_health_progress,
            worker_limit=_account_health_worker_limit,
            proxy_candidates=_current_health_proxy_pool(),
        )
    except asyncio.CancelledError:
        _finish_account_health_progress(message="测活请求已取消")
        raise
    except Exception as error:
        _finish_account_health_progress(message=str(error))
        raise
    try:
        replacement = await asyncio.to_thread(
            _replace_cdks_after_health,
            records,
            result["items"],
        )
    except Exception as error:
        _finish_account_health_progress(message=str(error))
        raise
    _finish_account_health_progress(
        cdk_replaced=replacement["replaced"],
        cdk_unresolved=replacement["unresolved"],
        result=result,
    )
    # The UI refreshes its paginated account view after completion. Returning
    # every checked row here duplicates that response and can be several MB for
    # an all-account run, so keep row payloads for small targeted checks only.
    include_rows = requested <= _ACCOUNT_HEALTH_INLINE_UPDATE_LIMIT
    response = {
        "ok": True,
        "requested": result["requested"],
        "actual_total": result["actual_total"],
        "checked": result["checked"],
        "usable": result["usable"],
        "unavailable": result["unavailable"],
        "skipped_known_deactivated": result["skipped_known_deactivated"],
        "deactivated": result["deactivated"],
        "invalid_other": result["invalid_other"],
        "alive": result["alive"],
        "limited": result["limited"],
        "invalid": result["invalid"],
        "reauth_required": result["reauth_required"],
        "error": result["error"],
        "cdk_replaced": replacement["replaced"],
        "cdk_unresolved": replacement["unresolved"],
        "items": (
            [account_public_view(item) for item in result["items"]]
            if include_rows
            else []
        ),
        "skipped_items": (
            [
                account_public_view(item)
                for item in result.get("skipped_items", [])
            ]
            if include_rows
            else []
        ),
        "items_omitted": not include_rows,
    }
    return response


@app.get("/api/accounts/health/progress")
async def accounts_health_progress(include_updates: bool = True):
    return {
        "ok": True,
        **_account_health_progress_snapshot(include_updates=include_updates),
    }


from web.routes.accounts_reauth import router as accounts_reauth_router
app.include_router(accounts_reauth_router)

from web.routes.accounts_reauth import (  # noqa: E402,F401
    AccountEmailReauthRequest,
    AccountPasswordChangeBatchRequest,
    AccountPasswordChangeRequest,
    AccountReauthBatchRequest,
    AccountReauthRequest,
    AccountSecurityOperationRequest,
    AccountTotpChangeBatchRequest,
    AccountTotpChangeRequest,
    _load_security_accounts,
    _security_batch_password,
    _start_security_batch,
    account_password_change,
    account_password_change_start,
    account_reauthenticate,
    account_reauthenticate_by_email_start,
    account_reauthenticate_progress,
    account_reauthenticate_start,
    account_reauthenticate_status,
    account_reauthenticate_stop,
    account_security_operation_start,
    account_security_repair,
    account_security_repair_start,
    account_totp_change,
    account_totp_change_start,
)


@app.post("/api/accounts/delete")
async def accounts_delete(body: BulkDeleteRequest):
    wanted = {str(key or "").strip() for key in body.keys if str(key or "").strip()}
    if body.delete_all:
        removed_records = await asyncio.to_thread(_load_accounts)
    elif wanted:
        records_by_key = await asyncio.to_thread(
            _records_by_keys_with_fallback,
            wanted,
        )
        removed_records = [
            records_by_key[key]
            for key in dict.fromkeys(
                str(raw_key or "").strip() for raw_key in body.keys
            )
            if key in records_by_key
        ]
    else:
        removed_records = []
    await asyncio.to_thread(archive_accounts, removed_records, reason="manual")
    removed, _ = await asyncio.to_thread(
        delete_account_records,
        body.keys,
        delete_all=body.delete_all,
        include_records=False,
        path=ACCOUNTS_FILE,
    )
    return {
        "ok": True,
        "removed": removed,
        "count": await asyncio.to_thread(count_accounts, ACCOUNTS_FILE),
    }


@app.post("/api/accounts/delete-invalid")
async def accounts_delete_invalid(body: BulkDeleteRequest):
    records = await asyncio.to_thread(_load_accounts)
    wanted = {str(key or "").strip() for key in body.keys if str(key or "").strip()}
    if not body.delete_all and not wanted:
        raise HTTPException(400, "请选择要删除的失效账号")
    keys = [
        _account_key(item)
        for item in records
        if normalized_health_status(item) == "invalid"
        and not account_is_deactivated(item)
        and (body.delete_all or _account_key(item) in wanted)
    ]
    if not keys:
        return {"ok": True, "removed": 0, "count": len(records)}
    await asyncio.to_thread(
        archive_accounts,
        [item for item in records if _account_key(item) in set(keys)],
        reason="invalid",
    )
    removed, _ = await asyncio.to_thread(
        delete_account_records,
        keys,
        include_records=False,
        path=ACCOUNTS_FILE,
    )
    return {
        "ok": True,
        "removed": removed,
        "count": await asyncio.to_thread(count_accounts, ACCOUNTS_FILE),
    }


@app.post("/api/accounts/download")
async def accounts_download(body: BulkDeleteRequest):
    wanted = {str(k or "").strip() for k in body.keys if str(k or "").strip()}
    if body.delete_all:
        records = await asyncio.to_thread(_load_accounts)
    elif wanted:
        records_by_key = await asyncio.to_thread(_records_by_keys_with_fallback, wanted)
        records = []
        for raw_key in body.keys:
            key = str(raw_key or "").strip()
            if key in records_by_key:
                records.append(records_by_key[key])
    else:
        records = []
    payload = await asyncio.to_thread(
        lambda: (json.dumps(records, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    headers = {
        "Content-Disposition": 'attachment; filename="registered_accounts.selected.json"'
    }
    return StreamingResponse(
        iter([payload]), media_type="application/json", headers=headers
    )


@app.post("/api/accounts/export-security")
async def accounts_export_security(body: BulkDeleteRequest):
    """Export selected account email, password, and TOTP secret records."""
    wanted = list(dict.fromkeys(str(key or "").strip() for key in body.keys))
    wanted = [key for key in wanted if key]
    if not wanted:
        raise HTTPException(400, "请选择要导出的账号")
    records_by_key = await asyncio.to_thread(_records_by_keys_with_fallback, wanted)
    selected = [records_by_key[key] for key in wanted if key in records_by_key]
    lines, missing = _security_credential_lines(selected)
    payload = await asyncio.to_thread(
        lambda: ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    )
    headers = {
        "Content-Disposition": 'attachment; filename="accounts.security.selected.txt"',
        "X-Exported-Count": str(len(lines)),
        "X-Missing-Count": str(missing + len(wanted) - len(selected)),
    }
    return StreamingResponse(
        iter([payload]), media_type="text/plain; charset=utf-8", headers=headers
    )


@app.get("/api/accounts/download-all")
async def accounts_download_all():
    records = await asyncio.to_thread(_load_accounts)
    payload = await asyncio.to_thread(
        lambda: (json.dumps(records, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    headers = {
        "Content-Disposition": 'attachment; filename="registered_accounts.all.json"'
    }
    return StreamingResponse(
        iter([payload]), media_type="application/json", headers=headers
    )


@app.get("/api/logs")
async def logs(limit: int = 300, q: str = "", level: str = "all"):
    lines = JOB.recent_logs(limit)
    needle = str(q or "").strip().lower()
    kind = str(level or "all").strip().lower()
    if needle:
        lines = [line for line in lines if needle in line.lower()]
    if kind == "error":
        lines = [line for line in lines if any(marker in line.lower() for marker in ("错误", "失败", "error", "timeout", "http 4", "http 5"))]
    elif kind == "success":
        lines = [line for line in lines if any(marker in line.lower() for marker in ("成功", "完成", "http 200"))]
    return {"lines": lines}


@app.get("/api/logs/download")
async def logs_download():
    payload = ("\n".join(JOB.recent_logs(MAX_LOG_LINES)) + "\n").encode("utf-8")
    return StreamingResponse(
        iter([payload]),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="gpt-register.log"'},
    )


@app.get("/api/tasks")
async def tasks(limit: int = 500):
    def _response():
        cap = max(1, min(int(limit or 500), 2000))
        cfg = _read_config()
        snap = JOB.snapshot()
        live_bucket = str(int(time.time() // 2)) if snap.get("running") else "idle"
        task_db = DATA_DIR / "task_history.sqlite3"
        key = ":".join(
            (
                "tasks:v2",
                str(cap),
                _cache_digest(json.dumps(snap, sort_keys=True, default=str)),
                live_bucket,
                str(_file_stamp(task_db)),
                str(_file_stamp(Path(f"{task_db}-wal"))),
            )
        )
        return _cached_json_response(
            cfg,
            key,
            "tasks_ttl",
            60,
            lambda: {"items": JOB.all_tasks_snapshot(limit=cap), "job": snap},
        )

    return await asyncio.to_thread(_response)


@app.post("/api/tasks/delete")
async def tasks_delete(body: TaskDeleteRequest):
    before = len(JOB.all_tasks_snapshot())
    wanted = {str(value).strip() for value in body.indexes}
    with JOB.lock:
        if body.delete_all:
            JOB.task_cards.clear()
        else:
            for key, card in list(JOB.task_cards.items()):
                card_id = str(card.get("card_id") or key)
                if card_id in wanted or str(key) in wanted:
                    JOB.task_cards.pop(key, None)
        JOB._task_revision += 1
        JOB._recompute_counts_locked()
        if body.delete_all:
            JOB.total = 0
    delete_task_runtime(wanted, delete_all=body.delete_all)
    JOB.persist_runtime()
    items = JOB.all_tasks_snapshot(limit=500)
    return {"ok": True, "removed": max(0, before - len(items)), "items": items}


@app.get("/api/mailboxes/list")
async def mailboxes_list(
    provider_type: str = "auto", q: str = "", limit: int = 500
):
    def _do():
        cfg = _read_config()
        cap = max(1, min(int(limit or 500), 2000))
        provider_key = normalize_provider_type(provider_type) or "auto"
        query_key = _cache_digest(str(q or "").strip().lower())
        key = ":".join(
            (
                "mailboxes:v2",
                provider_key,
                query_key,
                str(cap),
                str(_file_stamp(CONFIG_FILE)),
                str(_file_stamp(STATE_FILE)),
                str(account_store_revision(ACCOUNTS_FILE)),
                _account_data_stamp(),
            )
        )

        def _build_payload() -> dict[str, Any]:
            items = _mailbox_items(cfg, provider_type=provider_type, q=q)
            return {"count": len(items), "items": items[:cap]}

        return _cached_json_response(
            cfg,
            key,
            "mailboxes_ttl",
            120,
            _build_payload,
        )

    return await asyncio.to_thread(_do)


@app.post("/api/mailboxes/delete")
async def mailboxes_delete(body: MailboxDeleteRequest):
    if JOB.running:
        raise HTTPException(409, "job is running")

    def _do():
        with _edit_config() as cfg:
            removed = _delete_mailboxes_from_config(cfg, body)
        return {
            "ok": True,
            "removed": removed,
            "mail": _public_mail_stats(cfg),
        }

    return await asyncio.to_thread(_do)


@app.post("/api/mailboxes/state")
async def mailboxes_state(body: MailboxStateRequest):
    if JOB.running:
        raise HTTPException(409, "任务运行时不能修改邮箱状态")
    from mail import update_mailbox_states

    changed = await asyncio.to_thread(
        update_mailbox_states,
        body.emails,
        action=body.action,
        states=body.states,
    )

    def _snapshot():
        cfg = _read_config()
        return {
            "ok": True,
            "changed": changed,
            "mail": _public_mail_stats(cfg),
        }

    return await asyncio.to_thread(_snapshot)


@app.post("/api/mailbox/messages")
async def mailbox_messages(body: MailMessagesRequest):
    cfg = _read_config()
    try:
        return await asyncio.to_thread(_fetch_mailbox_messages_sync, cfg, body)
    except HTTPException as e:
        return {
            "ok": False,
            "provider_type": body.provider_type or "auto",
            "email": body.email,
            "items": [],
            "error": str(e.detail),
        }
    except Exception as e:
        return {
            "ok": False,
            "provider_type": body.provider_type or "auto",
            "email": body.email,
            "items": [],
            "error": f"读取邮箱失败: {str(e)[:500]}",
        }


@app.get("/api/logs/stream")
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
        while idle_ticks < max_ticks and not _SERVER_SHUTDOWN_EVENT.is_set():
            if await request.is_disconnected():
                break
            if "log" in requested_events:
                with JOB.lock:
                    lines = list(JOB.logs)
                chunk = _log_stream_delta(previous_lines, lines)
                previous_lines = lines
                if chunk:
                    payload = json.dumps({"lines": chunk}, ensure_ascii=False)
                    yield f"event: log\ndata: {payload}\n\n"

            if "tasks" in requested_events:
                task_revision, task_items = JOB.task_stream_snapshot()
                if task_revision != previous_task_revision:
                    payload = json.dumps(
                        {"revision": task_revision, "items": task_items},
                        ensure_ascii=False,
                    )
                    yield f"event: tasks\ndata: {payload}\n\n"
                    previous_task_revision = task_revision

            if "status" in requested_events:
                snap = JOB.snapshot()
                status_payload = json.dumps(snap, ensure_ascii=False, sort_keys=True)
                if status_payload != previous_status:
                    yield f"event: status\ndata: {status_payload}\n\n"
                    previous_status = status_payload

            now = time.monotonic()
            if now - last_heartbeat >= 5.0:
                yield "event: heartbeat\ndata: {}\n\n"
                last_heartbeat = now
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


@app.post("/api/job/start")
async def job_start(body: StartJobRequest):
    params = body.model_dump()

    # Archive the previous batch before reserving and replacing current state.
    JOB.persist_runtime()

    # Reserve the single job slot before touching mailbox state.  Otherwise a
    # second start request can release addresses owned by the active job before
    # it eventually notices JOB.running.
    with JOB.lock:
        if JOB.running:
            raise HTTPException(409, "job already running")
        JOB.running = True
        JOB.stop_requested = False
        JOB.paused = False

    def _prepare() -> dict[str, Any]:
        from mail import count_pool_capacity, release_stale_in_use

        out = dict(params)
        try:
            groups = list_groups(GROUPS_FILE)
            group_id = str(out.get("group_id") or "").strip() or groups[0]["id"]
            group = next((item for item in groups if item["id"] == group_id), None)
            if group is None:
                raise HTTPException(400, "目标分组不存在，请刷新分组列表")
            out["group_id"] = group_id
            out["group_name"] = group["name"]
            release_stale_in_use()
            cfg = _read_config()
            reg_cfg = cfg.get("registration") if isinstance(cfg.get("registration"), dict) else {}
            engine = str(out.get("engine") or reg_cfg.get("engine") or "protocol").strip().lower()
            if engine not in SUPPORTED_REGISTRATION_ENGINES:
                raise HTTPException(400, f"不支持的注册引擎: {engine}")
            out["engine"] = engine
            maddy_domain = str(out.get("maddy_domain") or "").strip().lower()
            blocked_domains = _maddy_blocked_domains(_find_provider(cfg, "maddy_webui") or {})
            if maddy_domain and maddy_domain in blocked_domains:
                raise HTTPException(400, f"Maddy 域名已屏蔽: {maddy_domain}")
            if maddy_domain:
                out["maddy_domain"] = maddy_domain
                out["mail_type"] = "maddy_webui"
                _apply_maddy_domain_to_config(cfg, maddy_domain)
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
                effective, resource_mode = _effective_registration_workers(
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
                    created = create_group(candidate, GROUPS_FILE)
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
        with JOB.lock:
            JOB.running = False
            JOB.stop_requested = False
            JOB.paused = False
            JOB._cond.notify_all()
        raise

    with JOB.lock:
        JOB.batch_id = f"{int(time.time())}-{secrets.token_hex(4)}"
        JOB.started_at = _now_iso()
        JOB.finished_at = None
        params["batch_id"] = JOB.batch_id
        JOB.params = params
        JOB.success_count = 0
        JOB.fail_count = 0
        JOB.skipped_count = 0
        JOB.total = int(params.get("total") or 1)
        JOB.last_error = ""
        JOB.results = []
        JOB.task_cards.clear()
        JOB._task_revision += 1
    JOB.persist_runtime()

    t = threading.Thread(
        target=_run_job_thread,
        args=(params,),
        name="gpt-register-job",
        daemon=True,
    )
    with JOB.lock:
        JOB._thread = t
    t.start()
    return {"ok": True, "job": JOB.snapshot()}


@app.post("/api/job/stop")
async def job_stop():
    not_running = False
    with JOB.lock:
        if not JOB.running:
            not_running = True
            loop = cancel_event = registration_task = None
            engine = "protocol"
        else:
            JOB.stop_requested = True
            JOB.paused = False
            loop = JOB._loop
            cancel_event = JOB._cancel_event
            registration_task = JOB._registration_task
            engine = str(JOB.params.get("engine") or "protocol").strip().lower()

    if not_running:
        return {"ok": True, "message": "not running", "job": JOB.snapshot()}

    JOB.append_log("已请求立即停止 - 正在取消任务并清理资源")

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
            JOB.append_log(f"停止信号发送失败: {e}")

    if loop is not None:
        try:
            from register_gpt import force_close_all_sessions

            asyncio.run_coroutine_threadsafe(force_close_all_sessions(), loop)
        except Exception as e:
            JOB.append_log(f"安排强制关闭失败: {e}")

    # Do not clear active reservations here. Browser/protocol workers own the
    # mailbox until their finalizer runs; clearing them early lets a new batch
    # allocate an address that the stopping worker still uses. Abandoned
    # reservations are recovered by release_stale_in_use on the next start.
    try:
        released = await asyncio.to_thread(release_stale_in_use)
        if released:
            JOB.append_log(f"停止任务回收过期邮箱占用 {released} 个")
    except Exception as e:
        JOB.append_log(f"停止时回收过期邮箱失败: {e}")

    return {"ok": True, "message": "stopping", "job": JOB.snapshot()}


@app.post("/api/job/pause")
async def job_pause():
    with JOB.lock:
        if not JOB.running:
            raise HTTPException(409, "当前没有运行中的任务")
        JOB.paused = True
    JOB.append_log("已暂停新任务和下一次重试的派发")
    return {"ok": True, "message": "paused", "job": JOB.snapshot()}


@app.post("/api/job/resume")
async def job_resume():
    with JOB.lock:
        if not JOB.running:
            raise HTTPException(409, "当前没有运行中的任务")
        JOB.paused = False
    JOB.append_log("任务派发已继续")
    return {"ok": True, "message": "resumed", "job": JOB.snapshot()}


@app.post("/api/job/retry-failed")
async def job_retry_failed():
    with JOB.lock:
        if JOB.running:
            raise HTTPException(409, "已有任务正在运行")
        failed_items = [
            item
            for item in JOB.task_cards.values()
            if str(item.get("status") or "") in {"failed", "cancelled"}
        ]
        failed = len(failed_items)
        params = dict(JOB.params)
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


@app.post("/api/mail/reset-failed")
async def reset_failed_mail():
    with JOB.lock:
        if JOB.running:
            raise HTTPException(409, "任务运行时不能释放邮箱状态")
    reset = await asyncio.to_thread(release_failed_mailboxes, include_stale_in_use=True)
    return {
        "ok": True,
        "reset": reset,
        "mail": _public_mail_stats(_read_config()),
    }


@app.get("/api/operations")
async def operations():
    def _response():
        cfg = _read_config()
        key = ":".join(
            (
                "operations:v2",
                str(_file_stamp(CONFIG_FILE)),
                str(account_store_revision(ACCOUNTS_FILE)),
                str(_file_stamp(DATA_DIR / "panel_operations.json")),
                str(_file_stamp(DATA_DIR / "account_trash.json")),
            )
        )

        def _build_payload() -> dict[str, Any]:
            settings = _maintenance_config(cfg)
            webhook = settings.pop("webhook_url", "")
            settings["has_webhook"] = bool(webhook)
            settings["webhook_masked"] = _mask_secret(webhook) if webhook else ""
            state = load_ops_state()
            maddy_auto = _maddy_auto_config(cfg)
            try:
                maddy_auto["domains"] = _maddy_auto_domains(cfg, refresh=False)
            except Exception:
                maddy_auto["domains"] = []
            maddy_auto.update(
                {
                    "current_domain": str(state.get("maddy_auto_current_domain") or ""),
                    "current_started_at": str(state.get("maddy_auto_current_started_at") or ""),
                    "last_at": str(state.get("maddy_auto_last_at") or ""),
                    "last_domain": str(state.get("maddy_auto_last_domain") or ""),
                    "last_result": state.get("maddy_auto_last_result") or {},
                    "last_error": str(state.get("maddy_auto_last_error") or ""),
                    "domain_last_epoch": state.get("maddy_auto_domain_last_epoch") or {},
                    "running": bool(state.get("maddy_auto_current_domain")),
                    "trigger_locked": _MADDY_AUTO_RUN_LOCK.locked(),
                }
            )
            health_counts = account_health_summary(ACCOUNTS_FILE)
            account_total = health_counts["total"]
            return {
                "ok": True,
                "metrics": _current_operations_metrics(),
                "mail": _public_mail_stats(cfg),
                "accounts": {"total": account_total, **health_counts},
                "maintenance": settings,
                "maddy_auto_register": maddy_auto,
                "state": state,
                "backups": list_backups(),
                "trash_count": len(list_trash(limit=1000)),
            }

        PANEL_RESPONSE_CACHE.configure(_cache_settings(cfg))
        cached, source = PANEL_RESPONSE_CACHE.get_or_set_json(
            key,
            _cache_ttl(cfg, "operations_ttl", 5),
            _build_payload,
        )
        try:
            payload = json.loads(cached)
        except (TypeError, ValueError):
            payload = _build_payload()
        if not isinstance(payload, dict):
            payload = {}
        payload["metrics"] = _current_operations_metrics()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        cache_status = PANEL_RESPONSE_CACHE.status()
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


@app.get("/api/cache/status")
async def cache_status():
    def _status():
        cfg = _read_config()
        PANEL_RESPONSE_CACHE.configure(_cache_settings(cfg))
        return {"ok": True, "cache": PANEL_RESPONSE_CACHE.status()}

    return await asyncio.to_thread(_status)


@app.put("/api/maintenance")
async def maintenance_update(body: MaintenanceUpdate):
    with _edit_config() as cfg:
        current = cfg.setdefault("maintenance", {})
        payload = body.model_dump()
        webhook = str(payload.pop("webhook_url") or "").strip()
        current.update(payload)
        if webhook:
            current["webhook_url"] = webhook
    public = _maintenance_config(cfg)
    saved_webhook = public.pop("webhook_url", "")
    public["has_webhook"] = bool(saved_webhook)
    public["webhook_masked"] = _mask_secret(saved_webhook) if saved_webhook else ""
    return {"ok": True, "maintenance": public}


@app.post("/api/maintenance/run")
async def maintenance_run():
    return await asyncio.to_thread(_run_account_maintenance_sync, force=True)


@app.put("/api/maddy-auto-register")
async def maddy_auto_register_update(body: MaddyAutoRegisterUpdate):
    engine = str(body.engine or "protocol").strip().lower()
    if engine not in SUPPORTED_REGISTRATION_ENGINES:
        raise HTTPException(400, f"不支持的注册引擎: {engine}")
    group_id = str(body.group_id or "").strip()
    if group_id:
        groups = list_groups(GROUPS_FILE)
        if not any(str(item.get("id") or "") == group_id for item in groups):
            raise HTTPException(400, "目标分组不存在，请刷新分组列表")
    with _edit_config() as cfg:
        current = cfg.setdefault("maddy_auto_register", {})
        current.update(
            {
                "enabled": bool(body.enabled),
                "interval_hours": max(1, min(int(body.interval_hours or 1), 168)),
                "per_domain": max(1, min(int(body.per_domain or 20), 500)),
                "concurrency": max(1, min(int(body.concurrency or 3), 20)),
                "engine": engine,
                "keep_on_fail": bool(body.keep_on_fail),
                "refresh_domains": bool(body.refresh_domains),
                "group_id": group_id,
            }
        )
    return {"ok": True, "maddy_auto_register": _maddy_auto_config(cfg)}


@app.post("/api/maddy-auto-register/run")
async def maddy_auto_register_run():
    if _MADDY_AUTO_RUN_LOCK.locked():
        return {"ok": True, "skipped": True, "reason": "maddy_auto_already_running"}
    asyncio.create_task(_run_maddy_auto_register_once(force=True))
    return {"ok": True, "message": "Maddy 自动注册已触发"}


@app.post("/api/maintenance/test-webhook")
async def maintenance_test_webhook(body: MaintenanceUpdate):
    cfg = _read_config()
    saved = _maintenance_config(cfg).get("webhook_url") or ""
    target = str(body.webhook_url or "").strip() or str(saved)
    ok, message = await asyncio.to_thread(
        send_webhook,
        target,
        {"event": "webhook_test", "message": "GPT Register FP test", "time": _now_iso()},
    )
    return {"ok": ok, "message": message}


@app.get("/api/backups")
async def backups_list():
    return {"ok": True, "items": await asyncio.to_thread(list_backups)}


@app.post("/api/backups")
async def backups_create():
    PROGRESS.start("backup_create", "创建数据备份", stage="压缩配置与数据文件")
    try:
        item = await asyncio.to_thread(create_backup)
    except Exception as exc:
        PROGRESS.finish("backup_create", ok=False, message=str(exc))
        raise
    PROGRESS.finish("backup_create", message=item["name"])
    return {"ok": True, "item": item}


@app.get("/api/backups/{name}")
async def backups_download(name: str):
    try:
        path = backup_path(name)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(404, "备份不存在") from exc
    return FileResponse(path, filename=path.name, media_type="application/zip")


@app.post("/api/backups/restore")
async def backups_restore(body: BackupRestoreRequest):
    if JOB.running:
        raise HTTPException(409, "任务运行时不能恢复备份")
    PROGRESS.start("backup_restore", "恢复数据备份", stage="校验并覆盖数据文件")
    try:
        restored = await asyncio.to_thread(restore_backup, body.name)
    except (ValueError, FileNotFoundError) as exc:
        PROGRESS.finish("backup_restore", ok=False, message="备份不存在")
        raise HTTPException(404, "备份不存在") from exc
    global _WEB_CREDS_CACHE, _WEB_CREDS_CACHE_MTIME
    _WEB_CREDS_CACHE = None
    _WEB_CREDS_CACHE_MTIME = None
    PROGRESS.finish("backup_restore", message=f"已恢复 {len(restored)} 个文件")
    return {"ok": True, "restored": restored, "message": "备份已恢复，请刷新页面"}





def create_app() -> FastAPI:
    return app


def main(argv: list[str] | None = None) -> None:
    import argparse

    import uvicorn

    _ensure_config_file()
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    web = _web_cfg()
    default_host = (
        os.environ.get("WEB_HOST")
        or str(web.get("host") or "127.0.0.1").strip()
        or "127.0.0.1"
    )
    default_port = int(os.environ.get("WEB_PORT") or web.get("port") or 8787)

    parser = argparse.ArgumentParser(description="GPT Register FP Web Panel")
    parser.add_argument("--host", default=default_host)
    parser.add_argument("--port", type=int, default=default_port)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)

    _ensure_web_token()
    creds = _ensure_web_credentials()
    print(f"  GPT Register FP Web → http://{args.host}:{args.port}")
    if args.host in {"0.0.0.0", "::", "[::]"}:
        print("  警告: 面板绑定了所有网卡，请确保仅可信网络可访问")
    print(f"  登录: http://{args.host}:{args.port}/login")
    print(f"  用户: {creds['username']}")
    server_options = {
        "host": args.host,
        "port": args.port,
        "log_level": "info",
        # Keep panel responsive under concurrent register jobs / SSE clients.
        "timeout_keep_alive": 15,
        "timeout_graceful_shutdown": 20,
        "limit_concurrency": 80,
        "backlog": 128,
    }
    if args.reload:
        uvicorn.run("web_app:app", reload=True, **server_options)
        return

    class _PanelServer(uvicorn.Server):
        def handle_exit(self, sig, frame) -> None:
            _SERVER_SHUTDOWN_EVENT.set()
            super().handle_exit(sig, frame)

    _PanelServer(uvicorn.Config("web_app:app", **server_options)).run()


if __name__ == "__main__":
    main()

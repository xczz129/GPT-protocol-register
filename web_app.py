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
    account_delete_key,
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


from common.account_health import (
    InvalidAccessTokenError,
    select_health_check_records,
)
from common.trial_eligibility import (
    TrialEligibilityError,
    account_access_token as _stored_access_token,
    check_trial_eligibility,
    check_trial_eligibility_nerver,
    is_trial_probe_retryable,
    schedule_unchecked_trial_checks,
)
from common.trial_eligibility_jobs import TRIAL_ELIGIBILITY_JOBS
from common import task_checkpoint
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
    sign_session as _sign_session_value,
    verify_session as _verify_session_value,
)
from common.panel_ops import (
    maybe_run_scheduled_backup,
    archive_accounts,
    backup_path,
    create_backup,
    delete_task_runtime,
    list_account_backups,
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
    LoginRequest,
    MailMessagesRequest,
    PasswordChangeRequest,
    auth_change_password,
    auth_login,
    auth_logout,
    auth_revoke_sessions,
    auth_status,
    login_page,
    auth_router,
)
from mail import (
    CANONICAL_PROVIDER_TYPES,
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
_TRIAL_BACKFILL_LOCK = threading.Lock()
_TRIAL_BACKFILL_RUNNING = False
_ACCOUNTS_API_CACHE_LOCK = threading.Lock()
_ACCOUNTS_API_CACHE: dict[tuple[int, int, int, int, int], tuple[float, dict[str, Any]]] = {}
_OPERATIONS_METRICS_LOCK = threading.Lock()
_OPERATIONS_METRICS_REVISION = -1
_OPERATIONS_METRICS_AT = 0.0
_OPERATIONS_METRICS_VALUE: dict[str, Any] | None = None
_PANEL_CACHE_WARM_TASK: asyncio.Task | None = None
_SERVER_SHUTDOWN_EVENT = threading.Event()
_BACKUP_AUTO_TASK = None

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


from web.routes.jobs import (  # noqa: E402,F401
    AccountReauthBatchJobs,
    JobState,
    StartJobRequest,
    _run_job_thread,
    job_pause,
    job_resume,
    job_retry_failed,
    job_start,
    job_stop,
)


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


class ConfigUpdate(BaseModel):
    config: dict[str, Any]


from web.routes.shared_models import AccountTokenRequest  # noqa: E402


class AccountCredentialsRequest(AccountTokenRequest):
    field: str = Field(default="totp_code", min_length=1, max_length=32)


from web.routes.shared_models import BulkDeleteRequest  # noqa: E402,F401


class GroupCreateRequest(BaseModel):
    name: str


class GroupUpdateRequest(BaseModel):
    name: str


class AccountGroupAssignRequest(BaseModel):
    keys: list[str] = Field(default_factory=list)
    delete_all: bool = False
    group_id: str


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
    "requested_workers": 0,
    "workers": 0,
    "throttled": False,
    "resource_mode": "normal",
    "updates": {},
    "updates_enabled": True,
}

# ---------------------------------------------------------------------------
# 测活检查点:任务进度落盘,服务重启后恢复显示并支持续跑剩余账号。
_ACCOUNT_HEALTH_CHECKPOINT_TASK = "health_check"
_ACCOUNT_HEALTH_CHECKPOINT_KEYS: list[str] = []
_ACCOUNT_HEALTH_DONE_KEYS: set[str] = set()
_ACCOUNT_HEALTH_CHECKPOINT_SAVER = task_checkpoint.DebouncedCheckpointSaver(
    _ACCOUNT_HEALTH_CHECKPOINT_TASK, interval=2.0
)


def _account_health_resource_budget(requested_workers: int) -> tuple[int, str]:
    # Operator choice: health checks are no longer throttled to a single
    # worker while a registration job is running. The configured value is
    # respected unconditionally.
    requested = max(1, int(requested_workers or 1))
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
        actual_total = max(
            1,
            int(
                _ACCOUNT_HEALTH_PROGRESS.get("actual_total")
                or _ACCOUNT_HEALTH_PROGRESS.get("total")
                or 1
            ),
        )
        budget_workers = workers
        workers = max(1, min(workers, actual_total))
        _ACCOUNT_HEALTH_PROGRESS["workers"] = workers
        _ACCOUNT_HEALTH_PROGRESS["throttled"] = budget_workers < requested
        _ACCOUNT_HEALTH_PROGRESS["resource_mode"] = mode


def _account_health_checkpoint_payload(
    running: bool, *, completed: bool = False
) -> dict[str, Any]:
    with _ACCOUNT_HEALTH_PROGRESS_LOCK:
        snapshot = {
            key: value
            for key, value in _ACCOUNT_HEALTH_PROGRESS.items()
            if key != "updates"
        }
    return {
        "version": 1,
        "task_type": _ACCOUNT_HEALTH_CHECKPOINT_TASK,
        "running": bool(running),
        "completed": bool(completed),
        "params": {
            "keys": list(_ACCOUNT_HEALTH_CHECKPOINT_KEYS),
            "workers": max(1, int(snapshot.get("requested_workers") or 1)),
        },
        "done_keys": sorted(_ACCOUNT_HEALTH_DONE_KEYS),
        "snapshot": snapshot,
        "saved_at": _now_iso(),
    }


def _account_health_checkpoint_begin(keys: list[Any], *, workers: int) -> None:
    global _ACCOUNT_HEALTH_CHECKPOINT_KEYS
    _ACCOUNT_HEALTH_CHECKPOINT_KEYS = [
        str(key).strip() for key in keys if str(key).strip()
    ]
    _ACCOUNT_HEALTH_DONE_KEYS.clear()
    task_checkpoint.save_checkpoint(
        _ACCOUNT_HEALTH_CHECKPOINT_TASK,
        _account_health_checkpoint_payload(running=True),
    )


def _account_health_checkpoint_restore() -> None:
    """服务启动时恢复未完成的测活进度(interrupted/已停止 + 剩余账号)。"""
    global _ACCOUNT_HEALTH_CHECKPOINT_KEYS
    checkpoint = task_checkpoint.load_checkpoint(_ACCOUNT_HEALTH_CHECKPOINT_TASK)
    if not checkpoint or checkpoint.get("completed"):
        return
    params = checkpoint.get("params") or {}
    keys = [str(key) for key in (params.get("keys") or []) if str(key)]
    done_keys = {str(key) for key in (checkpoint.get("done_keys") or []) if str(key)}
    pending = [key for key in keys if key not in done_keys]
    snapshot = dict(checkpoint.get("snapshot") or {})
    interrupted = bool(checkpoint.get("running"))
    if interrupted:
        message = (
            f"服务重启，测活任务中断：已完成 {snapshot.get('done', 0)} / "
            f"{snapshot.get('total', 0)}，剩余 {len(pending)} 个账号可续跑"
        )
    else:
        message = str(snapshot.get("message") or "测活任务已停止")
        if pending:
            message += f"，剩余 {len(pending)} 个账号可续跑"
    _ACCOUNT_HEALTH_CHECKPOINT_KEYS = keys
    _ACCOUNT_HEALTH_DONE_KEYS.clear()
    _ACCOUNT_HEALTH_DONE_KEYS.update(done_keys)
    with _ACCOUNT_HEALTH_PROGRESS_LOCK:
        _ACCOUNT_HEALTH_PROGRESS.clear()
        _ACCOUNT_HEALTH_PROGRESS.update(snapshot)
        _ACCOUNT_HEALTH_PROGRESS.update(
            running=False,
            interrupted=interrupted,
            pending_keys=pending,
            pending_count=len(pending),
            message=message,
            finished_at=_now_iso(),
            updates={},
        )


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
    budget_workers, resource_mode = _account_health_resource_budget(requested_workers)
    # 线程池实际按 min(workers, 账号数) 开线程，进度展示与实际一致；
    # 账号数不足导致的截断不算资源压并发（throttled 仍按预算口径）。
    actual_workers = max(1, min(budget_workers, actual_total or 1))
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
            requested_workers=requested_workers,
            workers=actual_workers,
            throttled=budget_workers < requested_workers,
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
    # 检查点:记录已完成账号并防抖落盘(服务重启可恢复进度)。
    done_key = account_delete_key(item)
    if done_key:
        _ACCOUNT_HEALTH_DONE_KEYS.add(done_key)
        _ACCOUNT_HEALTH_CHECKPOINT_SAVER.submit(
            lambda: _account_health_checkpoint_payload(running=True)
        )


def _finish_account_health_progress(
    *,
    message: str = "",
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
    # 检查点收尾:正常完成清除;取消/异常保留(running=false)供续跑。
    # 正常完成时 pending 恒为空:被跳过的封号账号不会触发 item 回调,
    # 不能把它们算进"剩余可续跑"。
    global _ACCOUNT_HEALTH_CHECKPOINT_KEYS
    pending = (
        []
        if result is not None
        else [
            key
            for key in _ACCOUNT_HEALTH_CHECKPOINT_KEYS
            if key not in _ACCOUNT_HEALTH_DONE_KEYS
        ]
    )
    with _ACCOUNT_HEALTH_PROGRESS_LOCK:
        _ACCOUNT_HEALTH_PROGRESS["pending_keys"] = pending
        _ACCOUNT_HEALTH_PROGRESS["pending_count"] = len(pending)
        if result is not None:
            _ACCOUNT_HEALTH_PROGRESS["interrupted"] = False
    if result is not None:
        _ACCOUNT_HEALTH_CHECKPOINT_KEYS = []
        _ACCOUNT_HEALTH_DONE_KEYS.clear()
        task_checkpoint.clear_checkpoint(_ACCOUNT_HEALTH_CHECKPOINT_TASK)
    else:
        _ACCOUNT_HEALTH_CHECKPOINT_SAVER.flush_now(
            lambda: _account_health_checkpoint_payload(running=False)
        )


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

try:  # 服务重启后恢复被打断的测活进度(检查点不存在时为空操作)
    _account_health_checkpoint_restore()
except Exception:
    pass
from web.routes.security_ops import (  # noqa: E402,F401
    _SECURITY_PASSWORD_MIN_LENGTH,
    _account_requires_protocol_reauth,
    _account_security_operation_label,
    _change_account_password_sync,
    _change_account_totp_sync,
    _generate_security_password,
    _normalize_account_security_operation,
    _normalize_reauth_mode,
    _normalize_security_password_mode,
    _persist_reauthenticated_account,
    _persist_reauthentication_failure,
    _protocol_reauth_credentials_ready,
    _reauthenticate_account_sync,
    _redact_security_detail,
    _repair_account_security_sync,
    _security_proxy_candidates,
)
ACCOUNT_REAUTH_JOBS = AccountReauthBatchJobs()


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



def _job_should_stop() -> bool:
    with JOB.lock:
        return bool(JOB.stop_requested)


def _job_should_pause() -> bool:
    with JOB.lock:
        return bool(JOB.paused)


def _backup_schedule_config(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    source = cfg if isinstance(cfg, dict) else _read_config()
    raw = source.get("backup_schedule") if isinstance(source.get("backup_schedule"), dict) else {}
    time_text = str(raw.get("time") or "04:00").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", time_text)
    if match:
        hour = min(23, max(0, int(match.group(1))))
        minute = min(59, max(0, int(match.group(2))))
        time_text = f"{hour:02d}:{minute:02d}"
    else:
        time_text = "04:00"
    return {
        "enabled": bool(raw.get("enabled")),
        "time": time_text,
        "retention_days": max(1, min(int(raw.get("retention_days") or 7), 90)),
    }


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


def _parse_iso_epoch(value: str) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


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
        },
        last_auto_deleted=deleted,
    )
    PROGRESS.finish(
        "account_maintenance",
        message=(
            f"正常 {result.get('alive', 0)}，待续期 {result.get('reauth_required', 0)}，"
            f"其它失效 {result.get('invalid_other', result.get('invalid', 0))}，"
            f"已封号跳过 {result.get('skipped_known_deactivated', 0)}，"
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

    from common.session_refresh import SESSION_REFRESH_JOBS

    refresh_at = SESSION_REFRESH_JOBS.active()
    if refresh_at and refresh_at.get("status") in {"queued", "running", "stopping"}:
        refresh_at_id = str(refresh_at.get("job_id") or "")
        SESSION_REFRESH_JOBS.stop(refresh_at_id)
        waits.append(("refresh_at", None, SESSION_REFRESH_JOBS, refresh_at_id))

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


async def _backup_auto_loop() -> None:
    'Daily scheduled database backup driven by the backup_schedule config.'
    while not _SERVER_SHUTDOWN_EVENT.is_set():
        try:
            cfg = await asyncio.to_thread(_read_config)
            schedule = _backup_schedule_config(cfg)
        except asyncio.CancelledError:
            raise
        except Exception:
            schedule = {"enabled": False, "time": "04:00", "retention_days": 7}
        if schedule["enabled"]:
            try:
                await asyncio.to_thread(maybe_run_scheduled_backup, schedule)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                try:
                    JOB.append_log(f"定时备份异常: {str(exc)[:160]}")
                except Exception:
                    pass
        await asyncio.sleep(60)


@asynccontextmanager
async def _app_lifespan(_app: FastAPI):
    global _MAINTENANCE_TASK, _MADDY_AUTO_TASK, _PANEL_CACHE_WARM_TASK, _BACKUP_AUTO_TASK
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
    if _BACKUP_AUTO_TASK is None or _BACKUP_AUTO_TASK.done():
        _BACKUP_AUTO_TASK = asyncio.create_task(_backup_auto_loop(), name="backup-auto-scheduler")
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
        if _BACKUP_AUTO_TASK is not None:
            _BACKUP_AUTO_TASK.cancel()
            try:
                await _BACKUP_AUTO_TASK
            except asyncio.CancelledError:
                pass
            _BACKUP_AUTO_TASK = None
        await _shutdown_active_background_jobs()
        web_executor.shutdown(wait=True, cancel_futures=True)


app = FastAPI(title="GPT Register FP", version="1.0.0", lifespan=_app_lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
app.include_router(auth_router)

from web.routes.codex_oauth import router as codex_oauth_router  # noqa: E402
app.include_router(codex_oauth_router)

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
PUBLIC_PATHS = {
    "/login",
    "/api/health",
    "/api/auth/login",
    "/api/auth/status",
    "/favicon.ico",
}
PUBLIC_PREFIXES = ("/static/",)


def _web_cfg() -> dict[str, Any]:
    cfg = _read_config()
    web = cfg.get("web") if isinstance(cfg.get("web"), dict) else {}
    return web if isinstance(web, dict) else {}


_WEB_CREDS_CACHE: dict[str, str] | None = None
_WEB_CREDS_CACHE_MTIME: float | None = None
_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
_LOGIN_ATTEMPTS_LOCK = threading.Lock()

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
    if path in {"/", "/index.html"} and not _auth_ok(request):
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


from web.routes.mailbox import router as mailbox_router  # noqa: E402

app.include_router(mailbox_router)

from web.routes.mailbox import (  # noqa: E402,F401
    FastmailAccountsRequest,
    MaddyAutoRegisterUpdate,
    MailboxesUpdate,
    MailboxDeleteRequest,
    MailboxStateRequest,
    MaddyWebuiDomainsRequest,
    _apply_maddy_domain_to_config,
    _fetch_icloud_message_sync,
    _fetch_mailbox_messages_sync,
    _delete_mailboxes_from_config,
    _find_mailbox_credential,
    _mail_stats,
    _merge_mailbox_credentials,
    _mailbox_items,
    _mailbox_provider_map,
    _maddy_auto_config,
    _maddy_auto_domains,
    _maddy_auto_loop,
    _maddy_auto_mark_domain_run,
    _maddy_blocked_domains,
    _maddy_webui_domains_payload,
    _public_mail_stats,
    _sanitize_maddy_provider_entry,
    fastmail_accounts,
    mailbox_messages,
    mailboxes_delete,
    mailboxes_list,
    mailboxes_state,
    maddy_auto_register_run,
    maddy_auto_register_update,
    put_mailboxes,
    reset_failed_mail,
)


from web.routes.proxy_admin import router as proxy_admin_router  # noqa: E402

app.include_router(proxy_admin_router)

from web.routes.proxy_admin import (  # noqa: E402,F401
    ProxyTestRequest,
    ProxyUpdate,
    _proxy_pool_preview,
    _warm_panel_caches,
    proxy_test,
    put_proxy,
)



from web.routes.chatgpt2api import router as chatgpt2api_router  # noqa: E402

app.include_router(chatgpt2api_router)

from web.routes.chatgpt2api import (  # noqa: E402,F401
    ChatGPT2APITestRequest,
    ChatGPT2APIUpdate,
    ChatGPT2APIUploadRequest,
    _c2a_public,
    get_chatgpt2api,
    put_chatgpt2api,
    test_chatgpt2api,
    upload_all_chatgpt2api,
)



@app.get("/api/accounts")
async def accounts(limit: int = 200, offset: int = 0):
    def _do():
        cap = max(1, min(limit, 20000))
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
        for _attempt in range(8):
            revision = account_store_revision(ACCOUNTS_FILE)
            groups_stamp = _stamp(GROUPS_FILE)
            cache_key = (cap, start, revision, groups_stamp)
            redis_key = ":".join(
                (
                    "accounts:v3",
                    str(cap),
                    str(start),
                    str(revision),
                    _account_data_stamp(),
                    str(groups_stamp),
                )
            )

            def _build_payload(expected_revision: int = revision) -> dict[str, Any]:
                now = time.monotonic()
                with _ACCOUNTS_API_CACHE_LOCK:
                    cached = _ACCOUNTS_API_CACHE.get(cache_key)
                    if cached and now - cached[0] < 5.0:
                        return cached[1]

                total = count_accounts(ACCOUNTS_FILE)
                groups = _groups_with_counts()
                names = {item["id"]: item["name"] for item in groups}
                default_id = groups[0]["id"]
                items = load_account_public_views(cap, ACCOUNTS_FILE, offset=start)

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
                    slim.append(item)
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


from web.routes.trial_health import router as trial_health_router  # noqa: E402

app.include_router(trial_health_router)


from web.routes.trial_health import (  # noqa: E402,F401
    AccountHealthRequest,
    AccountTrialEligibilityRequest,
    TokenTrialEligibilityBatchRequest,
    TokenTrialEligibilityRequest,
    TrialProxyConfigRequest,
    TrialProxyCountryItem,
    _normalize_access_token,
    _token_trial_eligibility_core,
    accounts_trial_eligibility,
    accounts_trial_eligibility_job,
    accounts_trial_eligibility_progress,
    accounts_trial_eligibility_stop,
    token_trial_eligibility,
    token_trial_eligibility_batch,
    trial_proxy_pool,
    trial_proxy_pool_save,
)


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
    if field not in {"password", "totp_secret", "totp_code", "refresh_token", "session_token"}:
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
    elif field == "session_token":
        value = str(account.get("session_token") or "").strip()
        if not value:
            raise HTTPException(409, "该账号没有已保存的 Session Token")
        payload = {"ok": True, "field": field, "value": value}
    elif field == "refresh_token":
        value = str(account.get("refresh_token") or "").strip()
        if not value:
            raise HTTPException(409, "该账号没有已保存的 RT(未接码或未导入)")
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


@app.post("/api/accounts/device-info")
async def account_device_info(body: AccountCredentialsRequest):
    """Return the registration fingerprint snapshot for one account."""
    target = str(body.key or "").strip()
    records = await asyncio.to_thread(_records_by_keys_with_fallback, [target])
    account = records.get(target)
    if account is None:
        raise HTTPException(404, "账号不存在")

    fingerprint: dict[str, Any] = {}
    raw_fp = account.get("register_fingerprint")
    if isinstance(raw_fp, str) and raw_fp.strip().startswith("{"):
        try:
            loaded = json.loads(raw_fp)
        except ValueError:
            loaded = None
        if isinstance(loaded, dict):
            fingerprint = loaded
    if not fingerprint:
        # 老账号没有完整存档：从平铺字段尽量还原注册环境。
        flat_map = {
            "browser_family": "browser_family",
            "impersonate": "tls_impersonate",
            "user_agent": "user_agent",
            "sec_ch_ua": "sec_ch_ua",
            "sec_ch_ua_full_version_list": "sec_ch_ua_full_version_list",
            "accept_language": "accept_language",
            "timezone": "timezone",
            "timezone_offset_min": "timezone_offset_min",
            "platform": "protocol_platform",
            "screen": "protocol_screen",
            "hardware_concurrency": "protocol_hardware_concurrency",
            "device_memory": "protocol_device_memory",
            "device_pixel_ratio": "protocol_device_pixel_ratio",
            "navigator_vendor": "protocol_navigator_vendor",
            "max_touch_points": "protocol_max_touch_points",
            "webgl_vendor": "protocol_webgl_vendor",
            "webgl_renderer": "protocol_webgl_renderer",
            "profile_id": "protocol_profile_id",
        }
        fingerprint = {
            new_key: str(account.get(old_key) or "")
            for new_key, old_key in flat_map.items()
            if str(account.get(old_key) or "").strip()
        }
        if not fingerprint.get("impersonate"):
            browser_type = str(account.get("browser_type") or "")
            if browser_type.startswith("curl_cffi/"):
                fingerprint["impersonate"] = browser_type.split("/", 1)[1]

    payload = {
        "ok": True,
        "email": str(account.get("email") or ""),
        "fingerprint": fingerprint,
        "proxy": str(account.get("proxy") or ""),
        "registered_at": str(account.get("registered_at") or ""),
        "registration_method": str(account.get("registration_method") or ""),
        "device_id": str(account.get("device_id") or fingerprint.get("device_id") or ""),
        "has_snapshot": bool(raw_fp),
    }
    return JSONResponse(
        payload,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


from web.routes.accounts_io import router as accounts_io_router  # noqa: E402

app.include_router(accounts_io_router)


from web.routes.accounts_io import (  # noqa: E402,F401
    AccountEmailExportRequest,
    _normalize_credential_export_emails,
    _security_credential_lines,
    accounts_export_security_by_email,
)


from web.routes.trial_health import router_health as account_health_router  # noqa: E402

app.include_router(account_health_router)


from web.routes.trial_health import accounts_health, accounts_health_progress  # noqa: E402,F401


from web.routes.accounts_reauth import router as accounts_reauth_router
app.include_router(accounts_reauth_router)

from web.routes.refresh_at import router as refresh_at_router  # noqa: E402

app.include_router(refresh_at_router)

from web.routes.refresh_at import (  # noqa: E402,F401
    AccountRefreshAtRequest,
    accounts_refresh_at,
    accounts_refresh_at_job,
    accounts_refresh_at_progress,
    accounts_refresh_at_stop,
)

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


from web.routes.accounts_io import router_bulk as accounts_io_bulk_router  # noqa: E402

app.include_router(accounts_io_bulk_router)


from web.routes.accounts_io import (  # noqa: E402,F401
    accounts_delete,
    accounts_delete_invalid,
    accounts_download,
    accounts_download_all,
    accounts_export_security,
)


from web.routes.ops import router as ops_router  # noqa: E402

app.include_router(ops_router)

from web.routes.ops import (  # noqa: E402,F401
    BackupRestoreRequest,
    MaintenanceUpdate,
    TaskDeleteRequest,
    _ACCOUNT_LOG_CACHE,
    _ACCOUNT_LOG_CACHE_COND,
    _ACCOUNT_LOG_CACHE_INFLIGHT,
    _ACCOUNT_LOG_CACHE_INFLIGHT_AT,
    _ACCOUNT_LOG_CACHE_LOCK,
    _ACCOUNT_LOG_CACHE_MAX,
    _ACCOUNT_LOG_CACHE_TTL,
    _account_log_cache_abort,
    _account_log_cache_begin,
    _account_log_cache_get,
    _account_log_cache_store,
    _account_log_event_identity,
    _account_log_event_key,
    _account_log_file_stamp,
    _account_log_store_stamp,
    _account_log_trace_stamp,
    _log_stream_delta,
    _merge_account_log_events,
    account_operation_logs,
    backups_create,
    backups_download,
    backups_list,
    backups_restore,
    cache_status,
    logs,
    logs_download,
    logs_stream,
    maintenance_run,
    maintenance_test_webhook,
    maintenance_update,
    operations,
    tasks,
    tasks_delete,
)



from web.routes.jobs import router as jobs_router  # noqa: E402

app.include_router(jobs_router)


from web.routes.account_backups import router as account_backups_router  # noqa: E402

app.include_router(account_backups_router)


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

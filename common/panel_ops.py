# -*- coding: utf-8 -*-
"""Operational helpers for the web panel."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from config import ROOT
from proxy_util import normalize_proxy_url, proxy_brief
from common.account_store import export_account_records, replace_account_records
from common.task_store import (
    delete_runtime as _delete_task_runtime,
    export_runtime as _export_task_runtime,
    import_runtime as _import_task_runtime,
    load_runtime as _load_task_runtime,
    save_runtime as _save_task_runtime,
)

DATA_DIR = ROOT / "data"
TRASH_FILE = DATA_DIR / "account_trash.json"
OPS_STATE_FILE = DATA_DIR / "panel_operations.json"
TASK_HISTORY_FILE = DATA_DIR / "task_history.json"
_TASK_HISTORY_LOCK = threading.Lock()
_TRASH_LOCK = threading.RLock()
_TRASH_CACHE_LOCK = threading.RLock()
_TRASH_CACHE_PATH: Path | None = None
_TRASH_CACHE_STAMP: tuple[int, int, int] = (0, 0, 0)
_TRASH_CACHE_ROWS: list[dict[str, Any]] | None = None
_TRASH_CACHE_EMAIL_REASONS: dict[str, str] | None = None
BACKUP_DIR = ROOT / "backups"
BACKUP_FILES = {
    "config.yaml": ROOT / "config.yaml",
    "registered_accounts.json": DATA_DIR / "registered_accounts.json",
    "account_groups.json": DATA_DIR / "account_groups.json",
    "outlook_token_state.json": DATA_DIR / "outlook_token_state.json",
    "task_history.json": TASK_HISTORY_FILE,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path, default: Any) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value
    except Exception:
        return default


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        Path(tmp_name).replace(path)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
    finally:
        Path(tmp_name).unlink(missing_ok=True)


def _invalidate_trash_cache() -> None:
    global _TRASH_CACHE_PATH, _TRASH_CACHE_STAMP, _TRASH_CACHE_ROWS, _TRASH_CACHE_EMAIL_REASONS
    with _TRASH_CACHE_LOCK:
        _TRASH_CACHE_PATH = None
        _TRASH_CACHE_STAMP = (0, 0, 0)
        _TRASH_CACHE_ROWS = None
        _TRASH_CACHE_EMAIL_REASONS = None


def load_trash_snapshot(path: Path | None = None) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Load recent trash rows and its email index from one parsed snapshot.

    The raw trash file can be large, so retain only the panel-sized recent rows
    and the small email/reason index.  Callers receive detached containers and
    the cache is invalidated by the source stamp or an explicit write.
    """
    global _TRASH_CACHE_PATH, _TRASH_CACHE_STAMP, _TRASH_CACHE_ROWS, _TRASH_CACHE_EMAIL_REASONS
    target = Path(path) if path is not None else TRASH_FILE
    stamp = _trash_stamp_for(target)
    with _TRASH_CACHE_LOCK:
        if (
            _TRASH_CACHE_ROWS is not None
            and _TRASH_CACHE_EMAIL_REASONS is not None
            and _TRASH_CACHE_PATH == target
            and _TRASH_CACHE_STAMP == stamp
        ):
            return list(_TRASH_CACHE_ROWS), dict(_TRASH_CACHE_EMAIL_REASONS)

    # Parse outside the write lock. A large historical file can take close to
    # a second on a cold read and must not pause mailbox allocation/recovery.
    def derive(rows: Any) -> tuple[list[dict[str, Any]], dict[str, str]]:
        source = rows if isinstance(rows, list) else []
        recent = [row for row in reversed(source) if isinstance(row, dict)][:1000]
        email_reasons: dict[str, str] = {}
        for row in source:
            if not isinstance(row, dict):
                continue
            record = row.get("record")
            if not isinstance(record, dict):
                continue
            email = str(record.get("email") or "").strip().lower()
            if email:
                email_reasons[email] = "known_deactivated"
        return recent, email_reasons

    recent, email_reasons = derive(_read_json(target, []))
    current_stamp = _trash_stamp_for(target)
    if current_stamp != stamp:
        # The writer uses atomic replacement. If it won the race with this
        # read, retry once so old-inode contents are not cached under the new
        # path stamp.
        recent, email_reasons = derive(_read_json(target, []))
        retry_stamp = _trash_stamp_for(target)
        if retry_stamp != current_stamp:
            return list(recent), dict(email_reasons)
        current_stamp = retry_stamp
    with _TRASH_CACHE_LOCK:
        # Prefer a snapshot another reader published while this parse ran.
        if (
            _TRASH_CACHE_ROWS is not None
            and _TRASH_CACHE_EMAIL_REASONS is not None
            and _TRASH_CACHE_PATH == target
            and _TRASH_CACHE_STAMP == current_stamp
        ):
            return list(_TRASH_CACHE_ROWS), dict(_TRASH_CACHE_EMAIL_REASONS)
        _TRASH_CACHE_PATH = target
        _TRASH_CACHE_STAMP = current_stamp
        _TRASH_CACHE_ROWS = recent
        _TRASH_CACHE_EMAIL_REASONS = email_reasons
        return list(recent), dict(email_reasons)


def _trash_stamp_for(path: Path) -> tuple[int, int, int]:
    try:
        stat = path.stat()
        return stat.st_ino, stat.st_mtime_ns, stat.st_size
    except OSError:
        return 0, 0, 0


def trash_email_reasons(path: Path | None = None) -> dict[str, str]:
    """Return the cached/parsed email index used by mailbox reconciliation."""
    _rows, email_reasons = load_trash_snapshot(path)
    return email_reasons


def _recent_trash_rows() -> list[dict[str, Any]]:
    """Read and cache only the rows exposed by the panel trash list."""
    rows, _email_reasons = load_trash_snapshot(TRASH_FILE)
    return rows


def load_ops_state() -> dict[str, Any]:
    value = _read_json(OPS_STATE_FILE, {})
    return value if isinstance(value, dict) else {}


def update_ops_state(**fields: Any) -> dict[str, Any]:
    state = load_ops_state()
    state.update(fields)
    _write_json(OPS_STATE_FILE, state)
    return state


def load_task_runtime(*, limit: int | None = None) -> dict[str, Any]:
    return _load_task_runtime(TASK_HISTORY_FILE, limit=limit)


def save_task_runtime(
    cards: Iterable[dict[str, Any]],
    last_job: dict[str, Any],
) -> dict[str, Any]:
    """Merge task cards by stable card_id into the SQLite runtime store."""
    with _TASK_HISTORY_LOCK:
        return _save_task_runtime(cards, last_job, TASK_HISTORY_FILE)


def delete_task_runtime(keys: Iterable[str] = (), *, delete_all: bool = False) -> int:
    with _TASK_HISTORY_LOCK:
        return _delete_task_runtime(keys, delete_all=delete_all, legacy_path=TASK_HISTORY_FILE)


def archive_accounts(records: Iterable[dict[str, Any]], *, reason: str) -> int:
    with _TRASH_LOCK:
        rows = _read_json(TRASH_FILE, [])
        if not isinstance(rows, list):
            rows = []
        count = 0
        for record in records:
            if not isinstance(record, dict):
                continue
            rows.append(
                {
                    "trash_id": uuid.uuid4().hex,
                    "deleted_at": _now(),
                    "reason": str(reason or "manual")[:80],
                    "record": dict(record),
                }
            )
            count += 1
        if count:
            _write_json(TRASH_FILE, rows[-5000:])
            _invalidate_trash_cache()
        return count


def list_trash(*, limit: int = 200) -> list[dict[str, Any]]:
    with _TRASH_LOCK:
        rows = _recent_trash_rows()
        return rows[: max(1, min(limit, 1000))]


def take_trash(ids: Iterable[str]) -> list[dict[str, Any]]:
    wanted = {str(value or "").strip() for value in ids if str(value or "").strip()}
    if not wanted:
        return []
    with _TRASH_LOCK:
        rows = _read_json(TRASH_FILE, [])
        if not isinstance(rows, list):
            return []
        taken: list[dict[str, Any]] = []
        kept: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict) and str(row.get("trash_id") or "") in wanted:
                record = row.get("record")
                if isinstance(record, dict):
                    taken.append(record)
            else:
                kept.append(row)
        if taken:
            _write_json(TRASH_FILE, kept)
            _invalidate_trash_cache()
        return taken


def create_backup() -> dict[str, Any]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"gpt-register-backup-{stamp}.zip"
    path = BACKUP_DIR / name
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for arcname, source in BACKUP_FILES.items():
            if arcname == "registered_accounts.json":
                archive.writestr(arcname, export_account_records(source))
                continue
            if arcname == "task_history.json":
                archive.writestr(arcname, _export_task_runtime(TASK_HISTORY_FILE))
                continue
            if source.is_file():
                archive.write(source, arcname=arcname)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return {"name": name, "size": path.stat().st_size, "created_at": _now()}


def list_backups() -> list[dict[str, Any]]:
    if not BACKUP_DIR.is_dir():
        return []
    out = []
    for path in sorted(BACKUP_DIR.glob("gpt-register-backup-*.zip"), reverse=True):
        stat = path.stat()
        out.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            }
        )
    return out[:50]


def backup_path(name: str) -> Path:
    clean = Path(str(name or "")).name
    if not clean.startswith("gpt-register-backup-") or not clean.endswith(".zip"):
        raise ValueError("invalid backup name")
    path = BACKUP_DIR / clean
    if not path.is_file():
        raise FileNotFoundError(clean)
    return path


def restore_backup(name: str) -> list[str]:
    path = backup_path(name)
    restored: list[str] = []
    with zipfile.ZipFile(path) as archive:
        members = set(archive.namelist())
        for arcname, target in BACKUP_FILES.items():
            if arcname not in members:
                continue
            data = archive.read(arcname)
            if arcname == "registered_accounts.json":
                try:
                    records = json.loads(data.decode("utf-8"))
                except Exception as exc:
                    raise ValueError("invalid account backup payload") from exc
                if not isinstance(records, list):
                    raise ValueError("invalid account backup payload")
                replace_account_records(records, target)
                restored.append(arcname)
                continue
            if arcname == "task_history.json":
                _import_task_runtime(data, TASK_HISTORY_FILE)
                restored.append(arcname)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            try:
                os.chmod(target, 0o600)
            except Exception:
                pass
            restored.append(arcname)
    return restored


def probe_proxy(proxy: str, *, timeout: float = 15.0) -> dict[str, Any]:
    from curl_cffi import requests

    normalized = normalize_proxy_url(proxy)
    started = time.monotonic()
    kwargs: dict[str, Any] = {"impersonate": "firefox147", "verify": False}
    if normalized:
        kwargs["proxies"] = {"http": normalized, "https": normalized}
    session = requests.Session(**kwargs)
    try:
        response = session.get("https://chatgpt.com/cdn-cgi/trace", timeout=timeout)
        elapsed = round((time.monotonic() - started) * 1000)
        fields: dict[str, str] = {}
        for line in str(response.text or "").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                fields[key.strip()] = value.strip()
        return {
            "proxy": proxy_brief(normalized),
            "ok": response.status_code < 400,
            "status": int(response.status_code),
            "latency_ms": elapsed,
            "ip": fields.get("ip", ""),
            "country": fields.get("loc", ""),
            "error": "",
        }
    except Exception as exc:
        return {
            "proxy": proxy_brief(normalized),
            "ok": False,
            "status": 0,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "ip": "",
            "country": "",
            "error": str(exc)[:180],
        }
    finally:
        session.close()


def task_metrics(tasks: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = [item for item in tasks if isinstance(item, dict)]
    terminal = [item for item in rows if str(item.get("status") or "") in {"success", "failed", "cancelled"}]
    successes = [item for item in terminal if item.get("status") == "success"]
    failures = [item for item in terminal if item.get("status") in {"failed", "cancelled"}]
    elapsed = [float(item.get("elapsed_sec") or 0) for item in terminal if float(item.get("elapsed_sec") or 0) > 0]
    reasons: dict[str, int] = {}
    for item in failures:
        error = str(item.get("error") or "").strip()
        if not error:
            steps = item.get("steps") if isinstance(item.get("steps"), list) else []
            error = str((steps[-1] if steps else {}).get("text") or "unknown")
        key = error[:80] or "unknown"
        reasons[key] = reasons.get(key, 0) + 1
    return {
        "total": len(terminal),
        "success": len(successes),
        "failed": len(failures),
        "success_rate": round(len(successes) * 100 / len(terminal), 1) if terminal else 0,
        "average_seconds": round(sum(elapsed) / len(elapsed), 1) if elapsed else 0,
        "failure_reasons": [
            {"reason": reason, "count": count}
            for reason, count in sorted(reasons.items(), key=lambda pair: pair[1], reverse=True)[:8]
        ],
    }


def send_webhook(url: str, payload: dict[str, Any]) -> tuple[bool, str]:
    import requests

    target = str(url or "").strip()
    if not target.startswith(("http://", "https://")):
        return False, "Webhook 地址无效"
    try:
        response = requests.post(target, json=payload, timeout=15)
        if response.status_code >= 400:
            return False, f"HTTP {response.status_code}: {(response.text or '')[:120]}"
        return True, f"HTTP {response.status_code}"
    except Exception as exc:
        return False, str(exc)[:160]

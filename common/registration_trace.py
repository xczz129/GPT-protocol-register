from __future__ import annotations

"""Per-account registration trace files.

Each task gets one append-only JSONL file.  Traces contain timings, stage
events, sanitized fingerprint metadata and proxy summaries, never credentials.
"""

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import ROOT
from proxy_util import proxy_brief

TRACE_DIR = ROOT / "data" / "registration_logs"
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


_SENSITIVE_KEY_NAMES = frozenset(
    {
        "password",
        "passwd",
        "passphrase",
        "new_password",
        "old_password",
        "password_confirm",
        "confirm_password",
        "totp",
        "totp_secret",
        "mfa_secret",
        "access_token",
        "refresh_token",
        "id_token",
        "session_token",
        "authorization",
        "cookie",
        "cookies",
        "api_key",
        "client_secret",
        "token",
        "code",
        "otp",
        "mfa_code",
        "verification_code",
        "secret",
    }
)
_SENSITIVE_KEY_COMPACT = frozenset(
    item.replace("_", "") for item in _SENSITIVE_KEY_NAMES
)


def _sensitive_key(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(key or "").lower()).strip("_")
    return normalized in _SENSITIVE_KEY_NAMES or normalized.replace("_", "") in _SENSITIVE_KEY_COMPACT


def _safe(value: Any, limit: int = 800, *, key: str = "") -> Any:
    if _sensitive_key(key):
        return "***" if value not in (None, "", [], {}) else ""
    if isinstance(value, (str, int, float, bool)) or value is None:
        text = str(value) if isinstance(value, str) else value
        return _redact(text)[:limit] if isinstance(text, str) else text
    if isinstance(value, dict):
        return {str(k): _safe(v, limit, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v, limit) for v in value[:50]]
    return _redact(str(value))[:limit]


def _redact(text: Any, secrets: tuple[Any, ...] = ()) -> str:
    value = str(text or "")
    for secret in sorted(
        (str(item or "").strip() for item in secrets),
        key=len,
        reverse=True,
    ):
        if secret:
            value = value.replace(secret, "***")
    value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~-]+", r"\1***", value)
    value = re.sub(
        r"(?i)(password|passwd|passphrase|new[_-]?password|old[_-]?password|"
        r"totp(?:[_-]?secret)?|mfa[_-]?secret|token|access[_-]?token|"
        r"refresh[_-]?token|id[_-]?token|session[_-]?token|authorization|"
        r"cookie|secret|otp|code)"
        r"\s*[:=]\s*[^\s,;]+",
        r"\1=***",
        value,
    )
    value = re.sub(
        r"(?i)([?&](?:access[_-]?token|refresh[_-]?token|id[_-]?token|"
        r"token|code|secret|password|totp|totp[_-]?secret|otp)=)[^&\s]+",
        r"\1***",
        value,
    )
    value = re.sub(r"(?i)(https?://)([^:/\s@]+):([^@/\s]+)@", r"\1***:***@", value)
    return value[:1200]


def _fingerprint_snapshot(fp: dict[str, Any] | None, device_id: str = "") -> dict[str, Any]:
    fp = fp if isinstance(fp, dict) else {}
    return {
        "impersonate": fp.get("impersonate"),
        "major": fp.get("major"),
        "full_version": fp.get("full_version"),
        "platform": "Windows",
        "platform_version": fp.get("platform_version"),
        "locale": fp.get("locale"),
        "accept_language": fp.get("accept_language"),
        "screen": fp.get("screen"),
        "hardware_concurrency": fp.get("hardware_concurrency"),
        "device_memory": fp.get("device_memory"),
        "timezone": fp.get("timezone"),
        "timezone_offset_min": fp.get("timezone_offset_min"),
        "webgl_vendor": fp.get("webgl_vendor"),
        "webgl_renderer": fp.get("webgl_renderer"),
        "profile_id": fp.get("profile_id"),
        "sentinel_sid": fp.get("sentinel_sid"),
        "device_id": device_id,
        "user_agent": fp.get("user_agent"),
        "sec_ch_ua": fp.get("sec_ch_ua"),
    }


def _trace_path(value: Any) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text)
    if not path.is_absolute():
        path = ROOT / path
    try:
        resolved = path.resolve()
        trace_root = TRACE_DIR.resolve()
        if trace_root not in resolved.parents and resolved != trace_root:
            return None
        return resolved
    except Exception:
        return None


def append_trace_file(trace_file: Any, payload: dict[str, Any]) -> bool:
    """Append a sanitized event to an existing registration trace JSONL file."""
    path = _trace_path(trace_file)
    if path is None:
        return False
    event = {
        str(k): _safe(v, key=str(k))
        for k, v in dict(payload or {}).items()
    }
    if "at" not in event:
        event["at"] = _now()
    for key in ("error", "step", "message", "reason", "proxy"):
        if key in event:
            event[key] = _redact(event[key])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        return True
    except Exception:
        return False


class RegistrationTrace:
    def __init__(
        self,
        index: int,
        *,
        proxy: str = "",
        provider: str = "",
        fixed_email: str = "",
        secrets: tuple[Any, ...] = (),
    ) -> None:
        self.index = int(index)
        self.started_epoch = time.time()
        self.trace_id = uuid.uuid4().hex
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        self.path = TRACE_DIR / f"task-{self.index:04d}-{self.trace_id[:12]}.jsonl"
        self.email = str(fixed_email or "").strip()
        self._secrets = tuple(secrets or ())
        self._write({
            "type": "trace_started",
            "trace_id": self.trace_id,
            "task_index": self.index,
            "started_at": _now(),
            "provider": str(provider or ""),
            "proxy": proxy_brief(proxy) if proxy else "direct",
            "email": self.email,
        })

    def _write(self, payload: dict[str, Any]) -> None:
        payload = {k: _safe(v, key=str(k)) for k, v in payload.items()}
        with _LOCK:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def set_secrets(self, *values: Any) -> None:
        """Add credential values used to redact free-form trace diagnostics."""
        self._secrets = tuple(
            dict.fromkeys(
                [*self._secrets, *(value for value in values if str(value or "").strip())]
            )
        )

    def identity(self, fingerprint: dict[str, Any] | None, *, device_id: str = "") -> None:
        self._write({"type": "fingerprint", "at": _now(), "fingerprint": _fingerprint_snapshot(fingerprint, device_id)})

    def device_sync(self, *, initial_device_id: str = "", final_device_id: str = "") -> None:
        initial = str(initial_device_id or "").strip()
        final = str(final_device_id or "").strip()
        self._write(
            {
                "type": "device_sync",
                "at": _now(),
                "elapsed_sec": round(max(0.0, time.time() - self.started_epoch), 3),
                "initial_device_id": initial,
                "final_device_id": final,
                "device_id_changed": bool(initial and final and initial != final),
            }
        )

    def event(self, status: str | None = None, **fields: Any) -> None:
        email = str(fields.get("email") or "").strip()
        if email:
            self.email = email
        clean = {}
        for key, value in fields.items():
            if key in {"error", "step", "message", "reason", "skip_reason", "proxy"}:
                clean[key] = _redact(value, self._secrets)
            elif not _sensitive_key(key):
                clean[key] = _safe(value, key=str(key))
        self._write({
            "type": "event",
            "at": _now(),
            "elapsed_sec": round(max(0.0, time.time() - self.started_epoch), 3),
            "status": status or "",
            "email": self.email,
            **clean,
        })

    def log_line(self, text: Any, color: str = "") -> None:
        self._write({
            "type": "log",
            "at": _now(),
            "elapsed_sec": round(max(0.0, time.time() - self.started_epoch), 3),
            "color": str(color or ""),
            "email": self.email,
            "message": _redact(text, self._secrets),
        })

    def finish(
        self,
        status: str,
        *,
        email: str = "",
        error: Any = "",
        result: dict[str, Any] | None = None,
        persist_operation: bool = True,
        secrets: tuple[Any, ...] = (),
    ) -> None:
        if email:
            self.email = str(email).strip()
        summary = {
            "type": "trace_finished",
            "at": _now(),
            "elapsed_sec": round(max(0.0, time.time() - self.started_epoch), 3),
            "status": status,
            "email": self.email,
        }
        if error:
            summary["error"] = _redact(error, (*self._secrets, *secrets))
        if isinstance(result, dict):
            summary["account_id"] = str(result.get("account_id") or "")[:80]
            summary["plan_type"] = str(result.get("plan_type") or "")
            summary["registration_state"] = str(result.get("registration_state") or "")
        self._write(summary)
        # Keep the durable account history in sync when no outer operation
        # coordinator is present.  Batch/web callers already persist one
        # bounded running marker and one terminal summary; writing another row
        # here would make every registration appear twice in the account log.
        if not persist_operation:
            return
        # Import lazily so this trace module remains usable by tooling that only
        # reads JSONL files and so a logging failure never changes registration
        # behaviour.
        try:
            from common.account_operation_log import record_operation_event

            account = dict(result or {})
            if email and not account.get("email"):
                account["email"] = email
            mapped_status = {
                "success": "success",
                "failed": "failed",
                "transport_retry": "retry",
                "skipped": "skipped",
                "cancelled": "cancelled",
            }.get(str(status or "").strip().lower(), "info")
            record_operation_event(
                account,
                "registration",
                mapped_status,
                message=(
                    "注册成功"
                    if mapped_status == "success"
                    else "注册跳过"
                    if mapped_status == "skipped"
                    else "注册失败"
                    if mapped_status in {"failed", "retry"}
                    else "注册任务结束"
                ),
                error=error,
                metadata={
                    "trace_file": str(self.path.relative_to(ROOT))
                    if self.path.is_relative_to(ROOT)
                    else str(self.path),
                    "trace_status": str(status or ""),
                    "provider": str(summary.get("provider") or ""),
                },
                secrets=(account.get("password"), account.get("totp_secret"), account.get("access_token"), account.get("refresh_token")),
            )
        except Exception:
            pass

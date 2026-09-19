"""Web-driven account import and pure HTTP OAuth job queue.

The queue keeps credentials on the server-side private store, exposes only
email/status/log metadata to the browser, and lets each account pause for a
phone or SMS value when an automatic SMS provider is not available.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import parse_qs, urlparse, urljoin

from .oauth import (
    ProtocolClient,
    exchange_code,
    save_auth_record,
    totp_from_secret,
)
from .sms import (
    SmsActivation,
    SmsBowerClient,
    SmsError,
    SmsSettings,
    load_sms_settings,
)
from .token_files import TokenFileError, move_token_files_to_trash
from proxy_util import is_proxy_transport_error, is_rate_limit_error


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PHONE_RE = re.compile(r"\+?\d[\d ()\-]{6,24}")
SECRET_KEYS = re.compile(
    r"(?i)(access[_ -]?token|refresh[_ -]?token|id[_ -]?token|password|secret|api[_ -]?key|sms[_ -]?code|totp)[\"']?\s*[:=]\s*[\"']?[^,;\s}\"']+"
)
LONG_TOKEN_RE = re.compile(r"\b(?:eyJ|ac_)[A-Za-z0-9_\-.]{24,}\b")


def _flag(value: Any, default: bool = False) -> bool:
    """Parse persisted boolean flags without treating ``"false"`` as true."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)

# The configured positive worker count controls concurrency. The slot gate
# applies decreases without interrupting sessions that are already running.
DEFAULT_IMPORT_CONCURRENCY = 2
# 登录会话重建(stale sign-in session)的次数上限:无上限时,持续失效的
# 账号会在队列中永久循环。超过后按失败收敛。
_SESSION_RESTART_LIMIT = 5
MAX_ACCOUNT_EXPORTS = 500
MAX_ACCOUNT_EXPORT_BYTES = 4 * 1024 * 1024
MAX_PERSISTED_JOBS = 300
MAX_JOB_LOGS = 240
ACTIVE_JOB_STATUSES = frozenset({"queued", "running", "waiting_phone", "waiting_sms"})
TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled", "skipped_phone"})
JOB_HISTORY_VERSION = 1


def _sms_client_for(settings: SmsSettings, timeout: float) -> Any | None:
    """Build the selected SMS provider client while keeping old patches/API stable."""
    if settings.provider != "smsbower":
        raise SmsError("此接码渠道已移除，仅支持 SMSBower")
    if not settings.enabled or not settings.credentials_configured:
        return None
    return SmsBowerClient(settings, timeout=timeout)


class ImportErrorBase(RuntimeError):
    """Import queue error suitable for a JSON response."""


class JobCancelled(ImportErrorBase):
    pass


class PhoneRequiredSkipped(ImportErrorBase):
    """The operator chose to skip an account that needs phone verification."""

    pass


class AuthSessionRestartRequired(ImportErrorBase):
    """The auth flow lost its server-side sign-in session.

    This is a session lifecycle failure rather than a bad password, phone
    number, or OTP.  The current ProtocolClient must be closed and the whole
    login flow restarted with a fresh client and PKCE state.
    """

    pass


class PhoneNumberRestartRequired(AuthSessionRestartRequired):
    """A different number must begin with a fresh authorization transaction."""


def _is_stale_signin_session_error(value: Any) -> bool:
    """Recognize the auth edge's stale sign-in-session response."""
    text = str(value or "").lower().replace("_", "-")
    if "http 409" in text and "/api/accounts/add-phone/send" in text and "invalid-state" in text:
        return True
    return any(
        marker in text
        for marker in (
            "sign-in session is no longer valid",
            "sign in session is no longer valid",
            "signin session is no longer valid",
            "sign-in-session-is-no-longer-valid",
            "signin-session-invalid",
        )
    )


def _auth_step_info(value: Any) -> tuple[str, str]:
    """Return the next URL and page type from an auth JSON response.

    The auth frontend has returned the navigation hint in a few equivalent
    shapes over time.  Keeping the extraction in one place prevents an empty
    or wrapped ``continue_url`` from being mistaken for a phone requirement.
    """
    if not isinstance(value, Mapping):
        return "", ""
    page = value.get("page") if isinstance(value.get("page"), Mapping) else {}
    payload = page.get("payload") if isinstance(page.get("payload"), Mapping) else {}
    candidates = (
        value.get("continue_url"),
        payload.get("url"),
        value.get("redirect_uri"),
        value.get("redirect_url"),
        value.get("url"),
    )
    next_url = next((str(item).strip() for item in candidates if isinstance(item, str) and item.strip()), "")
    page_type = str(page.get("type") or value.get("next_step") or value.get("step") or "").strip()
    return next_url, page_type


def _phone_requirement(value: Any) -> tuple[bool | None, str]:
    """Classify the MFA continuation as phone-required or phone-free.

    ``add_phone`` is the explicit signal observed in the HAR captures.  The
    extra aliases cover the frontend's phone-OTP naming without treating a
    normal ``phone_number`` field inside the session metadata as a request for
    verification.
    """
    next_url, page_type = _auth_step_info(value)
    url_marker = next_url.lower().replace("_", "-")
    type_marker = page_type.lower().replace("_", "-")
    phone_markers = ("/add-phone", "add-phone", "phone-otp", "phone-verification")
    phone_types = {"add-phone", "phone-otp-verification", "phone-verification", "verify-phone"}
    required = type_marker in phone_types or any(marker in url_marker for marker in phone_markers)
    if required:
        detail = page_type or next_url or "服务端步骤"
        return True, detail
    if not next_url and not page_type:
        return None, "未返回下一步导航"
    detail = page_type or next_url or "未返回手机号步骤"
    return False, detail


def _looks_like_phone_step_error(value: Any) -> bool:
    """Recognize a workspace error that means the server wants add-phone."""
    text = str(value or "").lower().replace("_", "-")
    return "invalid-auth-step" in text and any(
        marker in text for marker in ("add-phone", "phone-verification", "phone-otp")
    )


def _session_has_phone(value: Any) -> bool:
    """Detect an already-associated phone in auth-session metadata.

    This is deliberately conservative: only fields whose names explicitly
    describe a phone are inspected, and a value must contain a plausible
    number (or a non-empty phone record) before it can suppress the add-phone
    step.  Country hints alone never count as a verified phone.
    """
    if not isinstance(value, Mapping):
        return False
    phone_keys = {
        "phone", "phone_number", "phone_numbers", "verified_phone",
        "verified_phone_number", "verified_phone_numbers",
    }
    for key, raw in value.items():
        normalized = str(key).strip().lower().replace("-", "_")
        if normalized not in phone_keys:
            continue
        if isinstance(raw, str):
            digits = re.sub(r"\D", "", raw)
            if len(digits) >= 6:
                return True
        elif isinstance(raw, Mapping):
            if any(str(item or "").strip() for item in raw.values()):
                return True
        elif isinstance(raw, (list, tuple, set)) and raw:
            return True
    return False


def _phone_otp_expected(value: Any) -> bool | None:
    """Classify an ``add-phone/send`` response.

    ``True`` means the provider returned the normal phone-OTP page, ``False``
    means it advanced directly to consent/callback, and ``None`` means the
    response did not contain a usable navigation hint.  The unknown case is
    intentionally handled as OTP-required by the caller so a missing JSON
    field never causes an unverified account to be treated as complete.
    """
    next_url, page_type = _auth_step_info(value)
    if not next_url and not page_type:
        return None
    marker = (page_type or "").lower().replace("_", "-")
    url_marker = next_url.lower().replace("_", "-")
    if marker in {"phone-otp-verification", "phone-verification", "verify-phone"}:
        return True
    if any(item in url_marker for item in ("/phone-verification", "phone-otp")):
        return True
    return False


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _safe_filename(email: str) -> str:
    value = email.strip()
    return "".join("_" if char in '/\\:*?"<>|' else char for char in value)


def _mask_phone(value: str) -> str:
    raw = str(value or "").strip()
    if len(raw) <= 4:
        return "••••" if raw else ""
    return "•" * max(2, len(raw) - 4) + raw[-4:]


def redact_log(value: Any) -> str:
    """Remove credential-like fields before a message reaches the UI."""
    text = str(value or "")
    text = SECRET_KEYS.sub(lambda match: match.group(1) + ": [已隐藏]", text)
    text = LONG_TOKEN_RE.sub("[令牌已隐藏]", text)
    # Preserve the last four digits so an operator can distinguish a phone
    # attempt while avoiding a complete number in the browser log.
    text = PHONE_RE.sub(lambda match: _mask_phone(match.group(0)), text)
    return text[:600]


def _is_retryable_network_error(error: BaseException | str) -> bool:
    """Return true for route/TLS failures that are safe to retry.

    OAuth credentials and workspace errors are terminal; a proxy tunnel or
    TLS handshake failure only says that this network route failed.  Retrying
    the whole HTTP session is necessary because the auth flow is stateful.
    """
    if is_rate_limit_error(error):
        return False
    text = str(error or "").lower()
    if is_proxy_transport_error(text):
        return True
    return any(marker in text for marker in (
        "ssl", "tls", "handshake", "certificate verify failed",
        "connection reset", "connection aborted", "connection closed",
        "timed out", "timeout", "unexpected eof", "gateway timeout",
        "http 502", "http 503", "http 504", "curl: (27)",
    ))


def _atomic_write(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def parse_account_lines(text: str) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Parse ``email----password----base32`` lines without echoing secrets."""
    records: list[dict[str, str]] = []
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_no, raw in enumerate(str(text or "").splitlines(), 1):
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        parts = line.split("----", 2)
        if len(parts) < 3:
            errors.append({"line": line_no, "message": "格式应为：邮箱----密码----2FA_BASE32密钥"})
            continue
        email, password, secret = (part.strip() for part in parts)
        lowered = email.lower()
        if not EMAIL_RE.match(email):
            errors.append({"line": line_no, "message": "邮箱格式不正确"})
            continue
        if not password:
            errors.append({"line": line_no, "email": email, "message": "密码为空"})
            continue
        if not secret:
            errors.append({"line": line_no, "email": email, "message": "2FA Base32 密钥为空"})
            continue
        if lowered in seen:
            errors.append({"line": line_no, "email": email, "message": "文本中重复的邮箱"})
            continue
        try:
            # Validate early, but never return the generated code.
            totp_from_secret(secret)
        except ValueError as exc:
            errors.append({"line": line_no, "email": email, "message": f"2FA 密钥无效：{exc}"})
            continue
        seen.add(lowered)
        records.append({"email": email, "password": password, "totp_secret": secret})
    return records, errors


@dataclass
class AccountCredential:
    id: str
    email: str
    password: str
    totp_secret: str
    status: str = "ready"
    last_error: str = ""
    last_job_id: str = ""
    token_path: str = ""
    # Kept separately from ``status`` so a later successful import or a manual
    # retry can clear the marker without losing the account's last job state.
    phone_required: bool = False
    phone_required_at: int = 0
    created_at: int = field(default_factory=lambda: int(time.time()))
    updated_at: int = field(default_factory=lambda: int(time.time()))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AccountCredential | None":
        email = str(value.get("email") or "").strip()
        password = str(value.get("password") or "")
        secret = str(value.get("totp_secret") or value.get("mfa_secret") or "").strip()
        if not email or not password or not secret:
            return None
        return cls(
            id=str(value.get("id") or uuid.uuid4().hex[:16]),
            email=email,
            password=password,
            totp_secret=secret,
            status=str(value.get("status") or "ready"),
            last_error=str(value.get("last_error") or ""),
            last_job_id=str(value.get("last_job_id") or ""),
            token_path=str(value.get("token_path") or ""),
            phone_required=_flag(value.get("phone_required", False)),
            phone_required_at=int(value.get("phone_required_at") or 0),
            created_at=int(value.get("created_at") or time.time()),
            updated_at=int(value.get("updated_at") or time.time()),
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "password": self.password,
            "totp_secret": self.totp_secret,
            "status": self.status,
            "last_error": self.last_error,
            "last_job_id": self.last_job_id,
            "token_path": self.token_path,
            "phone_required": self.phone_required,
            "phone_required_at": self.phone_required_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "status": self.status,
            "last_error": redact_log(self.last_error),
            "last_job_id": self.last_job_id,
            "token_path": self.token_path,
            "phone_required": self.phone_required,
            "phone_required_at": self.phone_required_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class AccountStore:
    """Private on-disk credential store for imported accounts."""

    def __init__(self, path: Path | None = None):
        self.path = path or (_root() / "data" / "import-accounts.json")
        self._lock = threading.RLock()
        self._records: dict[str, AccountCredential] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        try:
            value = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except (OSError, json.JSONDecodeError):
            value = {}
        rows = value.get("accounts", []) if isinstance(value, dict) else []
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, Mapping):
                    account = AccountCredential.from_mapping(row)
                    if account:
                        self._records[account.id] = account

    def _persist(self) -> None:
        _atomic_write({"version": 1, "accounts": [item.as_mapping() for item in self._records.values()]}, self.path)

    def add_text(self, text: str) -> dict[str, Any]:
        parsed, errors = parse_account_lines(text)
        added: list[AccountCredential] = []
        updated: list[AccountCredential] = []
        with self._lock:
            by_email = {item.email.lower(): item for item in self._records.values()}
            for item in parsed:
                old = by_email.get(item["email"].lower())
                if old:
                    old.password = item["password"]
                    old.totp_secret = item["totp_secret"]
                    old.status = "ready"
                    old.last_error = ""
                    old.token_path = ""
                    old.updated_at = int(time.time())
                    updated.append(old)
                else:
                    account = AccountCredential(id=uuid.uuid4().hex[:16], **item)
                    self._records[account.id] = account
                    by_email[account.email.lower()] = account
                    added.append(account)
            if added or updated:
                self._persist()
        return {
            "added": [item.public() for item in added],
            "updated": [item.public() for item in updated],
            "errors": errors,
            "total": len(self._records),
        }

    def get(self, account_id: str) -> AccountCredential | None:
        with self._lock:
            return self._records.get(str(account_id))

    def all(self) -> list[AccountCredential]:
        with self._lock:
            return list(self._records.values())

    def public_all(self) -> list[dict[str, Any]]:
        return [item.public() for item in self.all()]

    def export_text(self, account_ids: Iterable[str] | None = None) -> tuple[bytes, int]:
        """Return the account pool in the same TXT format accepted by ``add_text``.

        Credentials are deliberately only returned through the authenticated
        download route.  The browser never receives them as JSON metadata.
        """
        with self._lock:
            if account_ids is None:
                accounts = list(self._records.values())
            else:
                requested: list[str] = []
                seen: set[str] = set()
                for raw in account_ids:
                    value = str(raw or "").strip()
                    if value and value not in seen:
                        seen.add(value)
                        requested.append(value)
                if len(requested) > MAX_ACCOUNT_EXPORTS:
                    raise ImportErrorBase(f"一次最多导出 {MAX_ACCOUNT_EXPORTS} 个账号")
                missing = [value for value in requested if value not in self._records]
                if missing:
                    raise ImportErrorBase("账号已不存在，请刷新账号池后重试")
                accounts = [self._records[value] for value in requested]
        if not accounts:
            raise ImportErrorBase("账号池为空，没有可导出的账号")
        if len(accounts) > MAX_ACCOUNT_EXPORTS:
            raise ImportErrorBase(f"一次最多导出 {MAX_ACCOUNT_EXPORTS} 个账号")
        lines = [f"{item.email}----{item.password}----{item.totp_secret}" for item in accounts]
        body = ("\n".join(lines) + "\n").encode("utf-8")
        if len(body) > MAX_ACCOUNT_EXPORT_BYTES:
            raise ImportErrorBase("账号导出文件超过大小限制")
        return body, len(accounts)

    def remove_many(self, account_ids: Iterable[str]) -> dict[str, Any]:
        """Delete private account-pool records and return public summaries."""
        requested: list[str] = []
        seen: set[str] = set()
        for raw in account_ids:
            value = str(raw or "").strip()
            if value and value not in seen:
                seen.add(value)
                requested.append(value)
        if not requested:
            raise ImportErrorBase("请至少选择一个账号")
        if len(requested) > MAX_ACCOUNT_EXPORTS:
            raise ImportErrorBase(f"一次最多删除 {MAX_ACCOUNT_EXPORTS} 个账号")
        deleted: list[AccountCredential] = []
        missing: list[str] = []
        with self._lock:
            for account_id in requested:
                account = self._records.pop(account_id, None)
                if account is None:
                    missing.append(account_id)
                else:
                    deleted.append(account)
            if deleted:
                self._persist()
            total = len(self._records)
        return {
            "deleted": [item.public() for item in deleted],
            "missing": missing,
            "total": total,
        }

    def update(self, account_id: str, **changes: Any) -> AccountCredential | None:
        with self._lock:
            item = self._records.get(str(account_id))
            if not item:
                return None
            for key in ("status", "last_error", "last_job_id", "token_path"):
                if key in changes and changes[key] is not None:
                    setattr(item, key, str(changes[key]))
            if "phone_required" in changes and changes["phone_required"] is not None:
                value = changes["phone_required"]
                if isinstance(value, str):
                    item.phone_required = value.strip().lower() in {"1", "true", "yes", "on"}
                else:
                    item.phone_required = bool(value)
            if "phone_required_at" in changes and changes["phone_required_at"] is not None:
                item.phone_required_at = int(changes["phone_required_at"] or 0)
            item.updated_at = int(time.time())
            self._persist()
            return item


STEP_PROGRESS = {
    "queued": 0,
    "bootstrap": 8,
    "email": 18,
    "password": 30,
    "mfa": 48,
    "phone": 65,
    "sms": 76,
    "workspace": 88,
    "token": 96,
    "saved": 100,
    "skipped_phone": 100,
}


@dataclass
class ImportJob:
    id: str
    account_id: str
    email: str
    batch_id: str
    status: str = "queued"
    step: str = "queued"
    progress: int = 0
    waiting_for: str = ""
    current_phone: str = ""
    error: str = ""
    token_path: str = ""
    started_at: int = 0
    finished_at: int = 0
    logs: list[dict[str, Any]] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    pending_input: dict[str, str] | None = field(default=None, repr=False)
    sms_provider: str = ""
    phone_required: bool = False
    phone_required_at: int = 0
    phone_masked: str = ""
    # Monotonically increasing attempt number, shown in the single per-email
    # card while preserving every attempt's logs in the persisted history.
    attempt: int = 1
    # Number of automatic route retries for this attempt.  It is persisted so
    # a worker restart cannot loop forever on the same broken proxy.
    network_retries: int = 0
    session_retries: int = 0
    phone_attempts: int = 0
    # 在租号码台账：acquire 成功即写入，正常释放时清空；持久化用于
    # 服务重启后补 cancel，避免已购号码只能等供应商侧过期。
    sms_activation_id: str = ""
    _sms_activation: SmsActivation | None = field(default=None, repr=False, compare=False)
    _sms_client: Any = field(default=None, repr=False, compare=False)
    _sms_sent: bool = field(default=False, repr=False, compare=False)
    _sms_keep_for_session_retry: bool = field(default=False, repr=False, compare=False)
    _pending_phone: str = field(default="", repr=False, compare=False)
    skip_phone_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _on_change: Callable[[], None] | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ImportJob | None":
        """Restore public/audit state without restoring a live OAuth session."""
        job_id = str(value.get("id") or "").strip()
        account_id = str(value.get("account_id") or "").strip()
        email = str(value.get("email") or "").strip()
        if not job_id or not account_id or not email:
            return None
        raw_logs = value.get("logs") if isinstance(value.get("logs"), list) else []
        logs: list[dict[str, Any]] = []
        for raw in raw_logs[-MAX_JOB_LOGS:]:
            if not isinstance(raw, Mapping):
                continue
            message = redact_log(raw.get("message") or "")
            if not message:
                continue
            try:
                at = max(0, int(raw.get("at") or 0))
            except (TypeError, ValueError):
                at = 0
            level = str(raw.get("level") or "info").strip().lower()
            if level not in {"info", "warning", "error"}:
                level = "info"
            logs.append({"at": at, "level": level, "message": message})
        try:
            progress = max(0, min(100, int(value.get("progress") or 0)))
            phone_required_at = max(0, int(value.get("phone_required_at") or 0))
            started_at = max(0, int(value.get("started_at") or 0))
            finished_at = max(0, int(value.get("finished_at") or 0))
            attempt = max(1, int(value.get("attempt") or 1))
        except (TypeError, ValueError):
            return None
        return cls(
            id=job_id,
            account_id=account_id,
            email=email,
            batch_id=str(value.get("batch_id") or ""),
            status=(str(value.get("status") or "failed") if str(value.get("status") or "failed") in (ACTIVE_JOB_STATUSES | TERMINAL_JOB_STATUSES) else "failed"),
            step=str(value.get("step") or "failed"),
            progress=progress,
            waiting_for=str(value.get("waiting_for") or ""),
            error=redact_log(value.get("error") or ""),
            token_path=str(value.get("token_path") or ""),
            started_at=started_at,
            finished_at=finished_at,
            logs=logs,
            sms_provider=str(value.get("sms_provider") or ""),
            phone_required=_flag(value.get("phone_required", False)),
            phone_required_at=phone_required_at,
            phone_masked=str(value.get("phone_masked") or ""),
            attempt=attempt,
            network_retries=max(0, min(3, int(value.get("network_retries") or 0))),
            session_retries=max(0, int(value.get("session_retries") or 0)),
            phone_attempts=max(0, int(value.get("phone_attempts") or 0)),
            sms_activation_id=str(value.get("sms_activation_id") or ""),
        )

    def as_persisted(self) -> dict[str, Any]:
        """Return only restart-safe, redacted task metadata."""
        return {
            "id": self.id,
            "account_id": self.account_id,
            "email": self.email,
            "batch_id": self.batch_id,
            "status": self.status,
            "step": self.step,
            "progress": max(0, min(100, int(self.progress))),
            "waiting_for": self.waiting_for,
            "error": redact_log(self.error),
            "token_path": self.token_path,
            "started_at": max(0, int(self.started_at)),
            "finished_at": max(0, int(self.finished_at)),
            "sms_provider": self.sms_provider,
            "phone_required": bool(self.phone_required),
            "phone_required_at": max(0, int(self.phone_required_at)),
            "phone_masked": _mask_phone(self.current_phone) or self.phone_masked,
            "attempt": max(1, int(self.attempt)),
            "network_retries": max(0, min(3, int(self.network_retries))),
            "session_retries": max(0, int(self.session_retries)),
            "phone_attempts": max(0, int(self.phone_attempts)),
            "sms_activation_id": str(self.sms_activation_id or ""),
            "logs": [
                {
                    "at": max(0, int(item.get("at") or 0)),
                    "level": str(item.get("level") or "info"),
                    "message": redact_log(item.get("message") or ""),
                }
                for item in self.logs[-MAX_JOB_LOGS:]
                if isinstance(item, Mapping) and str(item.get("message") or "").strip()
            ],
        }

    def add_log(self, message: str, level: str = "info") -> None:
        self.logs.append({"at": int(time.time()), "level": level, "message": redact_log(message)})
        if len(self.logs) > MAX_JOB_LOGS:
            del self.logs[:-MAX_JOB_LOGS]
        callback = self._on_change
        if callback is not None:
            try:
                callback()
            except Exception:
                # A transient history-file error must not abort OAuth work.
                pass


class ImportJobManager:
    """Threaded batch runner used by the local web dashboard."""

    def __init__(
        self,
        *,
        store: AccountStore | None = None,
        data_dir: Path | None = None,
        output_dir: Path | None = None,
        sms_settings_path: Path | None = None,
        concurrency: int = DEFAULT_IMPORT_CONCURRENCY,
        skip_phone_required: bool = False,
        timeout: float = 30.0,
    ):
        self.data_dir = data_dir or (_root() / "data")
        self.output_dir = output_dir or self.data_dir
        external_dir = Path("/opt/codex-services/data/cliproxy/auths")
        self.mirror_dir: Path | None = external_dir if external_dir.is_dir() else None
        self.sms_settings_path = sms_settings_path or (self.data_dir / "smsbower.json")
        self.store = store or AccountStore(self.data_dir / "import-accounts.json")
        self.concurrency = self.normalize_concurrency(concurrency)
        self.skip_phone_required = _flag(skip_phone_required)
        self.timeout = max(5.0, float(timeout))
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._jobs: dict[str, ImportJob] = {}
        self._batches: dict[str, dict[str, Any]] = {}
        # 重启孤儿号码台账（job_id -> activation_id）：首个可用 SMS client 补 cancel。
        self._orphan_sms_activation_ids: dict[str, str] = {}
        self._active_workers = 0
        self.jobs_path = self.data_dir / "import-jobs.json"
        # Load audit history before creating workers.  A process restart cannot
        # resume the old HTTP session, so any in-flight entry is explicitly
        # closed and made retryable below.
        self._load_jobs()
        self._executor = ThreadPoolExecutor(
            max_workers=self.concurrency,
            thread_name_prefix="oauth-import",
        )

    def _bind_job(self, job: ImportJob) -> ImportJob:
        """Attach manager ownership to a restored/new job when needed."""
        return job

    def _load_jobs(self) -> None:
        """Load durable task cards and mark interrupted sessions as failed."""
        path = self.jobs_path
        if path.exists():
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        try:
            payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, list):
            rows = payload
            batches: Any = {}
        elif isinstance(payload, Mapping):
            rows = payload.get("jobs", [])
            batches = payload.get("batches", {})
        else:
            rows, batches = [], {}
        if isinstance(rows, list):
            for row in rows[-MAX_PERSISTED_JOBS:]:
                if not isinstance(row, Mapping):
                    continue
                job = ImportJob.from_mapping(row)
                if job is None:
                    continue
                # No event/session/pending input is restored by design.
                if job.status in ACTIVE_JOB_STATUSES:
                    if job.sms_activation_id:
                        # 重启丢上下文：记录在租号码，等首个可用 client 补 cancel。
                        self._orphan_sms_activation_ids[job.id] = job.sms_activation_id
                        job.sms_activation_id = ""
                    job.status = "failed"
                    job.step = "failed"
                    job.waiting_for = ""
                    job.error = "服务重启，OAuth 会话已中断，可点击重新执行"
                    job.finished_at = int(time.time())
                    # Keep the callback detached while repairing the row.  A
                    # callback here would persist a partially loaded job set
                    # before this loop has added the remaining rows.
                    job.add_log(job.error, "warning")
                    try:
                        self.store.update(job.account_id, status="failed", last_error=job.error)
                    except Exception:
                        pass
                self._jobs[job.id] = job
                job._on_change = self._persist_jobs
        if isinstance(batches, Mapping):
            for key, value in batches.items():
                if not isinstance(value, Mapping):
                    continue
                batch_id = str(key or value.get("id") or "").strip()
                if not batch_id:
                    continue
                job_ids = value.get("job_ids") if isinstance(value.get("job_ids"), list) else []
                self._batches[batch_id] = {
                    "id": batch_id,
                    "created_at": int(value.get("created_at") or 0),
                    "job_ids": [str(item) for item in job_ids if str(item).strip()],
                }
        # Keep only references to jobs that survived the bounded load/prune.
        for meta in self._batches.values():
            meta["job_ids"] = [item for item in meta.get("job_ids", []) if item in self._jobs]
        if self._jobs:
            self._prune_jobs_locked()
        # Materialize the private history file even before the first job.  It
        # makes the persistence contract and file permissions visible at
        # startup, and subsequent log changes always replace this file
        # atomically.
        self._persist_jobs_locked()

    def _persist_jobs_locked(self) -> None:
        """Atomically persist task metadata; callers hold ``_lock``."""
        rows = list(self._jobs.values())
        # Keep terminal history bounded, but never discard a currently active
        # task merely to satisfy the cap.
        if len(rows) > MAX_PERSISTED_JOBS:
            active = [item for item in rows if item.status in ACTIVE_JOB_STATUSES]
            terminal = [item for item in rows if item.status not in ACTIVE_JOB_STATUSES]
            terminal.sort(key=lambda item: (item.finished_at, item.started_at, item.attempt, item.id), reverse=True)
            rows = active + terminal[: max(0, MAX_PERSISTED_JOBS - len(active))]
            keep = {item.id for item in rows}
            self._jobs = {key: item for key, item in self._jobs.items() if key in keep}
            for meta in self._batches.values():
                meta["job_ids"] = [item for item in meta.get("job_ids", []) if item in keep]
        payload = {
            "version": JOB_HISTORY_VERSION,
            "updated_at": int(time.time()),
            "jobs": [item.as_persisted() for item in rows],
            "batches": {
                key: {
                    "id": str(value.get("id") or key),
                    "created_at": int(value.get("created_at") or 0),
                    "job_ids": [str(item) for item in value.get("job_ids", []) if str(item) in self._jobs],
                }
                for key, value in self._batches.items()
            },
        }
        try:
            _atomic_write(payload, self.jobs_path)
        except OSError:
            # A transient disk/permission issue must not abort an OAuth task.
            pass

    def _persist_jobs(self) -> None:
        with self._lock:
            self._persist_jobs_locked()

    @staticmethod
    def normalize_concurrency(value: Any) -> int:
        """Normalize a user/config value to the supported worker range."""
        if isinstance(value, bool):
            raise ValueError("并发数必须是整数")
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("并发数必须是整数") from exc
        if number < 1:
            raise ValueError("并发数必须是正整数")
        return number

    def set_concurrency(self, value: Any) -> int:
        """Set the live import concurrency and wake waiting workers."""
        number = self.normalize_concurrency(value)
        with self._condition:
            self.concurrency = number
            # Grow the physical pool when the new limit exceeds it: queued
            # submissions already live in this executor, so only its worker
            # cap can unlock them. The CPython-internal hook is guarded;
            # if a future CPython removes it, rebuild the pool instead of
            # 500-ing this settings endpoint.
            executor = self._executor
            if number > getattr(executor, "_max_workers", 0):
                try:
                    with executor._shutdown_lock:
                        if number > executor._max_workers:
                            executor._max_workers = number
                            queued = sum(job.status == "queued" for job in self._jobs.values())
                            for _ in range(max(0, min(number, queued + self._active_workers) - len(executor._threads))):
                                executor._adjust_thread_count()
                except AttributeError:
                    self._executor = ThreadPoolExecutor(
                        max_workers=number,
                        thread_name_prefix="oauth-import",
                    )
            self._condition.notify_all()
            self._persist_jobs_locked()
        return number

    def set_skip_phone_required(self, value: Any) -> bool:
        """Set auto-skip behavior and wake tasks already at the phone step."""
        if isinstance(value, str):
            enabled = value.strip().lower() in {"1", "true", "yes", "on"}
        else:
            enabled = bool(value)
        with self._condition:
            self.skip_phone_required = enabled
            if enabled:
                for job in self._jobs.values():
                    if (
                        job.phone_required
                        and job.status in {"running", "waiting_phone", "waiting_sms"}
                        and job.step in {"phone", "sms"}
                    ):
                        job.skip_phone_event.set()
                        job.cancel_event.set()
            self._condition.notify_all()
            self._persist_jobs_locked()
        return enabled

    def runtime_status(self) -> dict[str, Any]:
        """Return live worker/queue counts for the dashboard."""
        with self._lock:
            active = sum(
                1
                for job in self._jobs.values()
                if job.status in {"running", "waiting_phone", "waiting_sms"}
            )
            queued = sum(1 for job in self._jobs.values() if job.status == "queued")
            return {
                "concurrency": self.concurrency,
                "active": active,
                "queued": queued,
                "slots_used": self._active_workers,
                "slots_available": max(0, self.concurrency - self._active_workers),
                "skip_phone_required": self.skip_phone_required,
            }

    def _acquire_worker_slot(self, job: ImportJob) -> None:
        """Wait until this job is allowed to own one live OAuth slot."""
        with self._condition:
            while True:
                self._check_cancel(job)
                if self._active_workers < self.concurrency:
                    self._active_workers += 1
                    return
                self._condition.wait(timeout=1.0)

    def _release_worker_slot(self) -> None:
        with self._condition:
            self._active_workers = max(0, self._active_workers - 1)
            self._condition.notify_all()

    def preview(self, text: str) -> dict[str, Any]:
        records, errors = parse_account_lines(text)
        return {
            "accounts": [{"email": item["email"], "valid": True} for item in records],
            "errors": errors,
            "count": len(records),
        }

    def _latest_job_for_email_locked(self, email: str) -> ImportJob | None:
        """Return the newest attempt for ``email``; caller holds ``_lock``."""
        key = str(email or "").strip().casefold()
        if not key:
            return None
        matches = [
            item for item in self._jobs.values()
            if str(item.email or "").strip().casefold() == key
        ]
        if not matches:
            return None
        return max(
            matches,
            key=lambda item: (
                max(1, int(item.attempt or 1)),
                int(item.started_at or 0),
                int(item.finished_at or 0),
                str(item.id),
            ),
        )

    def start(self, account_ids: Iterable[str], *, all_accounts: bool = False) -> dict[str, Any]:
        selected = self.store.all() if all_accounts else [self.store.get(item) for item in account_ids]
        accounts = [item for item in selected if item is not None]
        if not accounts:
            raise ImportErrorBase("没有可启动的账号")
        with self._lock:
            active_ids = {
                job.account_id
                for job in self._jobs.values()
                if job.status in ACTIVE_JOB_STATUSES
            }
            active_emails = {
                str(job.email or "").strip().casefold()
                for job in self._jobs.values()
                if job.status in ACTIVE_JOB_STATUSES and str(job.email or "").strip()
            }
            jobs: list[ImportJob] = []
            batch_id = "batch_" + secrets.token_urlsafe(8)
            for account in accounts:
                if account.id in active_ids or account.email.casefold() in active_emails:
                    continue
                # A mailbox may have several terminal attempts in the audit
                # store.  Start the next run with the previous attempt's
                # number and log history so the dashboard can keep exactly one
                # continuous card per email, regardless of whether the run was
                # launched from the import page or from quota re-authorize.
                previous = self._latest_job_for_email_locked(account.email)
                previous_attempt = max(0, int(previous.attempt)) if previous else 0
                previous_logs = list(previous.logs[-MAX_JOB_LOGS:]) if previous else []
                job = ImportJob(
                    id="job_" + secrets.token_urlsafe(9),
                    account_id=account.id,
                    email=account.email,
                    batch_id=batch_id,
                    attempt=max(1, previous_attempt + 1),
                    logs=previous_logs,
                )
                self._jobs[job.id] = job
                job._on_change = self._persist_jobs
                if previous:
                    job.add_log(f"重新执行第 {job.attempt} 次")
                job.add_log("任务已排队")
                self.store.update(
                    account.id,
                    status="queued",
                    last_error="",
                    last_job_id=job.id,
                )
                jobs.append(job)
            if not jobs:
                raise ImportErrorBase("选中的账号都正在处理")
            self._batches[batch_id] = {
                "id": batch_id,
                "created_at": int(time.time()),
                "job_ids": [job.id for job in jobs],
            }
            for job in jobs:
                self._executor.submit(self._run_job, job.id)
            self._prune_jobs_locked()
            self._persist_jobs_locked()
            return self.batch_view(batch_id)

    def start_for_emails(self, emails: Iterable[str]) -> dict[str, Any]:
        """Queue fresh OAuth attempts for accounts selected from the quota view.

        The quota report only contains public email metadata, while the OAuth
        worker needs the private password/TOTP record in ``AccountStore``.
        Resolve that join server-side and return missing addresses without
        exposing which credential fields are absent.
        """
        requested: list[str] = []
        seen: set[str] = set()
        for raw in emails:
            email = str(raw or "").strip()
            key = email.casefold()
            if not email or key in seen:
                continue
            seen.add(key)
            requested.append(email)
        if not requested:
            raise ImportErrorBase("请至少选择一个账号")
        by_email = {item.email.casefold(): item for item in self.store.all()}
        account_ids: list[str] = []
        missing: list[str] = []
        for email in requested:
            account = by_email.get(email.casefold())
            if account is None:
                missing.append(email)
            else:
                account_ids.append(account.id)
        if not account_ids:
            raise ImportErrorBase("选中的账号不在账号池中，请先导入邮箱、密码和 2FA")
        result = self.start(account_ids)
        if missing:
            result["missing_emails"] = missing
        return result

    def account_ids_for_emails(self, emails: Iterable[str]) -> tuple[list[str], list[str]]:
        """Join quota-view email addresses to private account-pool IDs."""
        requested: list[str] = []
        seen: set[str] = set()
        for raw in emails:
            email = str(raw or "").strip()
            key = email.casefold()
            if email and key not in seen:
                seen.add(key)
                requested.append(email)
        by_email = {item.email.casefold(): item.id for item in self.store.all()}
        ids = [by_email[email.casefold()] for email in requested if email.casefold() in by_email]
        missing = [email for email in requested if email.casefold() not in by_email]
        return ids, missing

    def export_for_emails(self, emails: Iterable[str]) -> tuple[bytes, int]:
        ids, missing = self.account_ids_for_emails(emails)
        if missing:
            raise ImportErrorBase("以下账号不在账号池中：" + ", ".join(missing[:3]))
        if not ids:
            raise ImportErrorBase("请至少选择一个账号")
        return self.store.export_text(ids)

    def delete_for_emails(self, emails: Iterable[str]) -> dict[str, Any]:
        ids, missing = self.account_ids_for_emails(emails)
        if not ids:
            raise ImportErrorBase("选中的账号不在账号池中，请先导入邮箱、密码和 2FA")
        result = self.delete_accounts(ids)
        if missing:
            result["missing_emails"] = missing
        return result

    def active_emails(self) -> set[str]:
        """Return emails with a live OAuth task, folded for safe joins."""
        with self._lock:
            return {
                str(job.email or "").strip().casefold()
                for job in self._jobs.values()
                if job.status in {"queued", "running", "waiting_phone", "waiting_sms"}
                and str(job.email or "").strip()
            }

    def delete_assets_for_emails(
        self,
        emails: Iterable[str],
        *,
        token_directories: Iterable[str | Path] = (),
        trash_directory: str | Path | None = None,
    ) -> dict[str, Any]:
        """Remove quota inventory assets while protecting live OAuth jobs.

        This is the quota-page deletion operation.  It removes matching
        credentials from the private import pool and moves every matching
        Token JSON (project and mirror copies, including opaque filenames) to
        a recoverable trash directory.  Accounts with a live job are returned
        in ``skipped_active`` and are left untouched.
        """
        requested: list[str] = []
        seen: set[str] = set()
        for raw in emails:
            email = str(raw or "").strip()
            key = email.casefold()
            if email and key not in seen:
                seen.add(key)
                requested.append(email)
        if not requested:
            raise ImportErrorBase("请至少选择一个账号")
        if len(requested) > MAX_ACCOUNT_EXPORTS:
            raise ImportErrorBase(f"一次最多删除 {MAX_ACCOUNT_EXPORTS} 个账号")

        with self._lock:
            active_by_email = {
                str(job.email or "").strip().casefold(): job
                for job in self._jobs.values()
                if job.status in {"queued", "running", "waiting_phone", "waiting_sms"}
                and str(job.email or "").strip()
            }
            safe_emails = [email for email in requested if email.casefold() not in active_by_email]
            skipped_active = [
                {
                    "email": email,
                    "status": active_by_email[email.casefold()].status,
                    "id": active_by_email[email.casefold()].id,
                }
                for email in requested
                if email.casefold() in active_by_email
            ]

            directories = tuple(Path(item) for item in token_directories if str(item).strip())
            try:
                token_result = move_token_files_to_trash(
                    self.output_dir,
                    safe_emails,
                    directories=directories,
                    trash_directory=Path(trash_directory) if trash_directory else self.data_dir / ".codex-token-trash",
                ) if safe_emails else {
                    "deleted": [],
                    "errors": [],
                    "deleted_count": 0,
                    "requested_count": 0,
                    "trash_directory": str(Path(trash_directory) if trash_directory else self.data_dir / ".codex-token-trash"),
                }
            except TokenFileError as exc:
                token_result = {
                    "deleted": [],
                    "errors": [{"error": str(exc)}],
                    "deleted_count": 0,
                    "requested_count": len(safe_emails),
                    "trash_directory": str(Path(trash_directory) if trash_directory else self.data_dir / ".codex-token-trash"),
                }

            token_errors = token_result.get("errors", [])
            global_token_error = any(not str(item.get("email") or "").strip() for item in token_errors)
            token_error_emails = {
                str(item.get("email") or "").strip().casefold()
                for item in token_errors
                if str(item.get("email") or "").strip()
            }
            # If a duplicate Token copy failed to move, keep the private login
            # record as well.  The operator can retry without having to import
            # credentials again.  A missing Token is treated as already gone,
            # which also lets an old persisted quota row be dismissed.
            deleted_emails = [
                email for email in safe_emails
                if not global_token_error and email.casefold() not in token_error_emails
            ]
            by_email = {item.email.casefold(): item.id for item in self.store.all()}
            safe_ids = [
                by_email[email.casefold()]
                for email in deleted_emails
                if email.casefold() in by_email
            ]
            missing_pool = [email for email in deleted_emails if email.casefold() not in by_email]
            pool_result = self.delete_accounts(safe_ids) if safe_ids else {
                "deleted": [],
                "skipped_active": [],
                "missing": [],
                "total": len(self.store.all()),
            }
            return {
                "deleted": pool_result.get("deleted", []),
                "deleted_emails": deleted_emails,
                "deleted_tokens": token_result.get("deleted", []),
                "deleted_token_count": int(token_result.get("deleted_count", 0) or 0),
                "token_errors": token_errors,
                "trash_directory": token_result.get("trash_directory", ""),
                "skipped_active": skipped_active,
                "missing_emails": missing_pool,
                "total": len(self.store.all()),
            }

    def delete_accounts(self, account_ids: Iterable[str]) -> dict[str, Any]:
        """Delete idle account-pool entries while keeping active OAuth jobs safe."""
        requested: list[str] = []
        seen: set[str] = set()
        for raw in account_ids:
            value = str(raw or "").strip()
            if value and value not in seen:
                seen.add(value)
                requested.append(value)
        if not requested:
            raise ImportErrorBase("请至少选择一个账号")
        if len(requested) > MAX_ACCOUNT_EXPORTS:
            raise ImportErrorBase(f"一次最多删除 {MAX_ACCOUNT_EXPORTS} 个账号")
        with self._lock:
            active = {
                job.account_id: job
                for job in self._jobs.values()
                if job.status in {"queued", "running", "waiting_phone", "waiting_sms"}
            }
            deletable = [account_id for account_id in requested if account_id not in active]
            skipped_active = [
                {
                    "id": account_id,
                    "email": active[account_id].email,
                    "status": active[account_id].status,
                }
                for account_id in requested
                if account_id in active
            ]
            result = self.store.remove_many(deletable) if deletable else {
                "deleted": [],
                "missing": [account_id for account_id in deletable],
                "total": len(self.store.all()),
            }
            result["skipped_active"] = skipped_active
            return result

    def _prune_jobs_locked(self) -> None:
        """Bound in-memory history while retaining recent job cards."""
        if len(self._jobs) <= 300:
            return
        completed = [item for item in self._jobs.values() if item.status in {"succeeded", "failed", "cancelled", "skipped_phone"}]
        completed.sort(key=lambda item: (item.finished_at, item.id))
        for item in completed[: max(0, len(self._jobs) - 240)]:
            self._jobs.pop(item.id, None)

    def cancel(self, job_id: str) -> bool:
        with self._condition:
            job = self._jobs.get(job_id)
            if not job:
                return False
            job.cancel_event.set()
            job.pending_input = {"kind": "cancel", "value": ""}
            self._condition.notify_all()
            if job.status in {"queued", "waiting_phone", "waiting_sms"}:
                job.status = "cancelled"
                job.step = "cancelled"
                job.finished_at = int(time.time())
                job.add_log("任务已取消", "warning")
                self.store.update(job.account_id, status="cancelled", last_error="任务已取消")
            self._persist_jobs_locked()
            return True

    def skip_phone(self, job_id: str) -> bool:
        """Stop a phone-required task and retain the account as needs_phone."""
        with self._condition:
            job = self._jobs.get(str(job_id))
            if not job:
                return False
            if not job.phone_required:
                raise ImportErrorBase("该任务尚未检测到需要接码")
            if job.status not in {"running", "waiting_phone", "waiting_sms"}:
                raise ImportErrorBase("该任务当前不在接码步骤")
            job.skip_phone_event.set()
            job.cancel_event.set()
            job.pending_input = {"kind": "skip_phone", "value": ""}
            job.add_log("已请求跳过接码，正在结束当前会话", "warning")
            self._condition.notify_all()
            self._persist_jobs_locked()
            return True

    def retry(self, job_id: str) -> dict[str, Any]:
        """Queue a fresh attempt for a finished/failed job.

        A retry gets a new job id and batch id, so the original card remains
        as an audit trail while the new attempt receives a clean HTTP session
        and fresh PKCE/MFA state.
        """
        with self._lock:
            job = self._jobs.get(str(job_id))
            if not job:
                raise ImportErrorBase("任务不存在")
            if job.status in ACTIVE_JOB_STATUSES:
                raise ImportErrorBase("当前任务仍在处理中")
            account_id = job.account_id
        # ``start`` performs the same history/attempt inheritance for retries
        # initiated by either the job card or the quota page.  Keeping one
        # implementation also prevents a worker from starting before the new
        # attempt metadata has been installed.
        return self.start([account_id])

    def submit_input(self, job_id: str, kind: str, value: str) -> dict[str, Any]:
        kind = str(kind or "").strip().lower()
        value = str(value or "").strip()
        if kind not in {"phone", "sms_code"}:
            raise ImportErrorBase("输入类型应为 phone 或 sms_code")
        if not value:
            raise ImportErrorBase("输入内容为空")
        with self._condition:
            job = self._jobs.get(job_id)
            if not job:
                raise ImportErrorBase("任务不存在")
            expected = "phone" if job.waiting_for == "phone" else "sms_code"
            if expected != kind:
                raise ImportErrorBase(f"当前任务正在等待：{expected}")
            job.pending_input = {"kind": kind, "value": value}
            job.add_log("已收到网页输入" + ("手机号" if kind == "phone" else "短信验证码"))
            self._condition.notify_all()
            self._persist_jobs_locked()
            return self.job_view(job_id) or {}

    def list_jobs(self, batch_id: str = "") -> list[dict[str, Any]]:
        with self._lock:
            jobs = list(self._jobs.values())
            if batch_id:
                jobs = [item for item in jobs if item.batch_id == batch_id]
            jobs.sort(key=lambda item: (item.started_at, item.id), reverse=True)
            # Defensive server-side collapse: retries remain in ``_jobs`` and
            # their logs remain persisted, but the dashboard receives one card
            # per case-folded email.  Prefer an active/latest attempt.
            unique: dict[str, ImportJob] = {}
            histories: dict[str, list[dict[str, Any]]] = {}
            for item in jobs:
                key = str(item.email or "").strip().casefold() or item.id
                current = unique.get(key)
                if current is None:
                    unique[key] = item
                else:
                    current_active = current.status in ACTIVE_JOB_STATUSES
                    item_active = item.status in ACTIVE_JOB_STATUSES
                    if item_active and not current_active:
                        unique[key] = item
                    elif item_active == current_active and (item.attempt, item.started_at, item.id) > (current.attempt, current.started_at, current.id):
                        unique[key] = item
                bucket = histories.setdefault(key, [])
                bucket.extend(item.logs[-MAX_JOB_LOGS:])
            views: list[dict[str, Any]] = []
            for key, item in unique.items():
                view = self._job_view(item)
                # Older history files may predate attempt inheritance.  Merge
                # logs at read time as a compatibility bridge without
                # mutating live jobs or exposing credentials.
                merged: dict[tuple[int, str, str], dict[str, Any]] = {}
                for log in histories.get(key, []):
                    if not isinstance(log, Mapping):
                        continue
                    marker = (int(log.get("at") or 0), str(log.get("level") or "info"), str(log.get("message") or ""))
                    if marker[2]:
                        merged[marker] = dict(log)
                view["logs"] = sorted(merged.values(), key=lambda value: int(value.get("at") or 0))[-MAX_JOB_LOGS:]
                views.append(view)
            return views

    def job_view(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._jobs.get(job_id)
            return self._job_view(item) if item else None

    def batch_view(self, batch_id: str) -> dict[str, Any]:
        with self._lock:
            meta = dict(self._batches.get(batch_id) or {"id": batch_id, "job_ids": []})
            jobs = [self._jobs[item] for item in meta.get("job_ids", []) if item in self._jobs]
            counts: dict[str, int] = {}
            for job in jobs:
                counts[job.status] = counts.get(job.status, 0) + 1
            meta["summary"] = counts
            total = len(jobs)
            finished = sum(1 for job in jobs if job.status in {"succeeded", "failed", "cancelled", "skipped_phone"})
            meta["progress"] = {
                "total": total,
                "finished": finished,
                "active": max(0, total - finished),
                "percent": round(sum(max(0, min(100, int(job.progress))) for job in jobs) / total)
                if total else 0,
            }
            meta["jobs"] = [self._job_view(job) for job in jobs]
            return meta

    def _job_view(self, job: ImportJob | None) -> dict[str, Any]:
        if job is None:
            return {}
        return {
            "id": job.id,
            "account_id": job.account_id,
            "email": job.email,
            "batch_id": job.batch_id,
            "status": job.status,
            "step": job.step,
            "progress": job.progress,
            "waiting_for": job.waiting_for,
            "phone_required": job.phone_required,
            "phone_required_at": job.phone_required_at,
            "sms_provider": job.sms_provider,
            "phone_masked": _mask_phone(job.current_phone) or job.phone_masked,
            "error": redact_log(job.error),
            "token_path": job.token_path,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "attempt": job.attempt,
            "logs": list(job.logs),
        }

    def _set_step(self, job: ImportJob, step: str, message: str = "", *, status: str | None = None) -> None:
        with self._lock:
            job.step = step
            job.progress = STEP_PROGRESS.get(step, job.progress)
            if status:
                job.status = status
            if message:
                job.add_log(message)
            if status in {"running", "waiting_phone", "waiting_sms"}:
                self.store.update(job.account_id, status=status)
            self._persist_jobs_locked()

    def _check_cancel(self, job: ImportJob) -> None:
        if job.skip_phone_event.is_set():
            raise PhoneRequiredSkipped("账号需要接码，已跳过")
        if job.cancel_event.is_set():
            raise JobCancelled("任务已取消")

    def _wait_input(self, job: ImportJob, kind: str) -> str:
        value = self._wait_input_or_provider(job, kind, watch_provider=False)
        # The non-provider mode always returns a string; keeping the guard
        # here makes the contract explicit for callers and type checkers.
        if value is None:
            raise ImportErrorBase("等待输入时接码配置发生变化，请重试任务")
        return value

    def _wait_input_or_provider(self, job: ImportJob, kind: str, *, watch_provider: bool) -> str | None:
        """Wait for a web value, optionally noticing a newly enabled provider.

        Operators often enable SMSBower after a task has already reached the
        phone page.  In that case the worker can switch to automatic mode
        without throwing away the authenticated HTTP/PKCE session.  A return
        value of ``None`` is reserved for that provider switch.
        """
        with self._condition:
            job.waiting_for = kind
            job.status = "waiting_phone" if kind == "phone" else "waiting_sms"
            job.step = "phone" if kind == "phone" else "sms"
            job.progress = STEP_PROGRESS[job.step]
            job.add_log("等待网页输入" + ("手机号" if kind == "phone" else "短信验证码"))
            next_provider_check = time.monotonic() + 1.0
            while True:
                self._check_cancel(job)
                if job.pending_input and job.pending_input.get("kind") == kind:
                    value = job.pending_input.get("value", "")
                    job.pending_input = None
                    job.waiting_for = ""
                    job.status = "running"
                    return value
                if watch_provider and kind == "phone" and time.monotonic() >= next_provider_check:
                    next_provider_check = time.monotonic() + 2.0
                    settings = self._sms_settings()
                    if settings.enabled and settings.credentials_configured:
                        job.waiting_for = ""
                        job.status = "running"
                        job.add_log(f"检测到 {settings.provider_label} 已启用，切换自动接码")
                        return None
                self._condition.wait(timeout=1.0)

    def _sms_settings(self) -> SmsSettings:
        try:
            return load_sms_settings(self.sms_settings_path)
        except SmsError:
            # Treat a broken optional provider configuration like disabled
            # automatic SMS; the task will fall back to webpage input.
            return SmsSettings()

    @staticmethod
    def _cancel_activation(sms_client: Any | None, activation: SmsActivation | None) -> None:
        """Best-effort release of a number when a job leaves the phone step.

        A skip/cancel request can arrive between ``acquire`` and the first
        provider call, or between ``wait_code`` and OAuth submission.  Those
        paths used to raise immediately and leave the provider activation
        reserved.  Keep cleanup idempotent and deliberately swallow provider
        errors: the account job's terminal state is more important than a
        secondary acknowledgement failure.
        """
        if sms_client is None or activation is None:
            return
        if bool(getattr(activation, "finished", False)):
            return
        try:
            sms_client.cancel(activation)
        except Exception as error:
            # 取消未被供应商确认。activation 未置 finished,下次清理会重试;
            # 此处记告警,避免号码在供应商侧静默占用计费。
            print(f"[sms] number release unconfirmed: {str(error)[:200]}", flush=True)

    def _phone_step(self, job: ImportJob, client: ProtocolClient) -> None:
        try:
            self._phone_step_impl(job, client)
        except AuthSessionRestartRequired:
            # Only an explicitly rejected, unsent phone submission may keep
            # its lease while a fresh authorization transaction is started.
            if not job._sms_keep_for_session_retry or job._sms_sent:
                self._release_retained_activation(job)
            raise
        except BaseException:
            # Provider adapters, task persistence, or UI-step callbacks may
            # raise exceptions other than SmsError. All terminal paths own
            # the same cleanup, including cancellation after acquisition.
            self._release_retained_activation(job)
            raise

    def _phone_step_impl(self, job: ImportJob, client: ProtocolClient) -> None:
        now = int(time.time())
        job.phone_required = True
        job.phone_required_at = now
        self.store.update(job.account_id, status="needs_phone", phone_required=True, phone_required_at=now)
        job.add_log("已标记：账号需要接码")
        if self.skip_phone_required or job.skip_phone_event.is_set():
            job.skip_phone_event.set()
            job.cancel_event.set()
            raise PhoneRequiredSkipped("账号需要接码，自动跳过")
        settings = self._sms_settings()
        job.sms_provider = settings.provider_label
        sms_client = job._sms_client or _sms_client_for(settings, self.timeout)
        if sms_client is not None and getattr(self, "_orphan_sms_activation_ids", None):
            self._cancel_orphan_activations(sms_client)
        # OpenAI 2026-09 起校验页面导航:先进入 add-phone 页面,
        # 否则 add-phone/send 直接 409 session invalid
        nav_data = getattr(client, "last_auth_response", None)
        nav_url = urljoin("https://auth.openai.com/", _auth_step_info(nav_data)[0] or "/add-phone")
        parsed_navigation = urlparse(nav_url)
        if parsed_navigation.scheme != "https" or parsed_navigation.netloc != "auth.openai.com" or parsed_navigation.path.rstrip("/") != "/add-phone":
            raise ImportErrorBase("phone_navigation_unexpected_step")
        try:
            client.navigate(nav_url)
            job.add_log("已进入手机验证页面")
        except Exception as exc:
            if _is_stale_signin_session_error(exc):
                raise AuthSessionRestartRequired(
                    "sign-in session is no longer valid; restarting authorization session"
                ) from exc
            raise ImportErrorBase(f"phone_navigation_failed: {exc}") from exc
        if sms_client is not None:
            job.add_log(
                f"服务端要求手机号；{settings.provider_label} 自动接码已启用，将自动申请号码并轮询短信"
            )
        elif settings.credentials_configured:
            job.add_log(
                f"服务端要求手机号；{settings.provider_label} 已配置但当前未启用，等待网页输入手机号",
                "warning",
            )
        else:
            job.add_log(
                f"服务端要求手机号；未配置 {settings.provider_label} 凭证，等待网页输入手机号",
                "warning",
            )
        attempts = job.phone_attempts
        provider_disabled_by_failure = False
        # A manually supplied replacement number must be consumed by the next
        # iteration.  Keeping it outside the loop prevents the old state
        # machine from asking for the same input twice after a rejected number.
        pending_phone = job._pending_phone
        job._pending_phone = ""

        def cancel_number(activation: SmsActivation | None) -> None:
            self._cancel_activation(sms_client, activation)
            if job._sms_activation is activation:
                job._sms_activation = None
                job.sms_activation_id = ""
                job._sms_client = None
                job._sms_sent = False
                job._sms_keep_for_session_retry = False

        def may_acquire_again() -> bool:
            return settings.max_phone_attempts == 0 or attempts < settings.max_phone_attempts

        while True:
            self._check_cancel(job)
            activation: SmsActivation | None = None
            automatic = sms_client is not None and not pending_phone
            if job._sms_activation is not None:
                activation = job._sms_activation
                check_active = getattr(sms_client, "is_active", None)
                active = not activation.finished and (check_active(activation) if callable(check_active) else True)
                if not active:
                    cancel_number(activation)
                    raise PhoneNumberRestartRequired("原号码租约已结束，重新授权后按配置换号")
                phone = activation.phone
                automatic = True
                job.add_log("原号码尚未发出短信，授权已重建，继续使用同一号码")
            elif pending_phone:
                phone = pending_phone
                pending_phone = ""
                automatic = False
                job.current_phone = phone
            elif sms_client:
                if not may_acquire_again():
                    raise ImportErrorBase(f"已达到最多 {settings.max_phone_attempts} 次取号上限")
                attempts += 1
                job.phone_attempts = attempts
                denominator = settings.max_phone_attempts if settings.max_phone_attempts > 0 else "不限"
                self._set_step(job, "phone", f"正在从 {settings.provider_label} 获取手机号（第 {attempts}/{denominator} 次）", status="running")
                try:
                    activation = sms_client.acquire()
                    job._sms_activation = activation
                    job.sms_activation_id = str(getattr(activation, "activation_id", "") or "")
                    job._sms_client = sms_client
                    job._sms_sent = False
                    job._sms_keep_for_session_retry = False
                    phone = activation.phone
                    job.current_phone = phone
                    job.add_log(f"已分配 {phone}（国家 {activation.country}）")
                except SmsError as exc:
                    job.add_log(f"自动接码分配失败：{exc}", "warning")
                    raise
            else:
                if not provider_disabled_by_failure:
                    phone = self._wait_input_or_provider(job, "phone", watch_provider=True)
                    if phone is None:
                        settings = self._sms_settings()
                        sms_client = _sms_client_for(settings, self.timeout)
                        if sms_client is not None:
                            continue
                        phone = self._wait_input(job, "phone")
                else:
                    phone = self._wait_input(job, "phone")
                job.current_phone = phone
            try:
                self._check_cancel(job)
            except (PhoneRequiredSkipped, JobCancelled):
                # The activation may have been acquired just before the
                # operator clicked skip/cancel.  Release it before bubbling
                # the terminal signal to ``_run_job``.
                cancel_number(activation)
                raise
            self._set_step(job, "phone", f"向 {_mask_phone(phone)} 请求短信验证码")
            try:
                response = client.send_phone(phone)
                if activation is not None:
                    job._sms_sent = True
                    job._sms_keep_for_session_retry = False
                try:
                    phone_data = response.json()
                except Exception:
                    phone_data = dict(getattr(client, "last_auth_response", {}) or {})
                otp_expected = _phone_otp_expected(phone_data)
                if otp_expected is False:
                    # Check the operator's skip/cancel request before
                    # settling a provider activation.  Settling first would
                    # charge a number even though this task is being skipped.
                    try:
                        self._check_cancel(job)
                    except (PhoneRequiredSkipped, JobCancelled):
                        cancel_number(activation)
                        raise
                    if activation and sms_client and not activation.finished:
                        sms_client.finish(activation)
                    job._sms_activation = None
                    job.sms_activation_id = ""
                    job._sms_client = None
                    job._sms_sent = False
                    job._sms_keep_for_session_retry = False
                    job.add_log("服务端已完成手机号步骤，无需短信验证码，继续授权")
                    job.phone_required = False
                    job.phone_required_at = 0
                    self.store.update(job.account_id, phone_required=False, phone_required_at=0)
                    return
                if activation is None:
                    # The response itself is useful for diagnosing a server
                    # step transition, but never copy response JSON to logs.
                    job.add_log("手机号已接受，等待验证码")
                else:
                    job.add_log("手机号已接受，开始轮询短信")
            except Exception as exc:
                message = str(exc)
                if _is_stale_signin_session_error(message):
                    # The add-phone endpoint is stateful.  Once the auth edge
                    # invalidates the sign-in session, retrying a number or
                    # OTP against this client can never recover it.  Bubble a
                    # dedicated signal so _run_job closes this client and
                    # starts a completely fresh ProtocolClient flow.
                    if activation is not None and not activation.finished and not job._sms_sent and not job.cancel_event.is_set() and not job.skip_phone_event.is_set():
                        job._sms_activation = activation
                        job._sms_client = sms_client
                        job._sms_keep_for_session_retry = True
                        job.add_log("发送前会话失效，保留原号码重新授权", "warning")
                    else:
                        cancel_number(activation)
                    self._check_cancel(job)
                    raise AuthSessionRestartRequired(
                        "sign-in session is no longer valid; restarting authorization session"
                    ) from exc
                cancel_number(activation)
                self._check_cancel(job)
                job.add_log(f"手机号被拒绝：{message}", "warning")
                if automatic:
                    if may_acquire_again():
                        raise PhoneNumberRestartRequired("号码被拒绝，重新授权后换号") from exc
                    raise ImportErrorBase(f"取号 {attempts}/{settings.max_phone_attempts} 次均未完成") from exc
                # Manual mode lets the operator replace a rejected number.
                sms_client = None
                provider_disabled_by_failure = True
                job._pending_phone = self._wait_input(job, "phone")
                raise PhoneNumberRestartRequired("手工号码已更换，重新授权后提交") from exc
            if activation and sms_client:
                self._set_step(job, "sms", f"等待 {settings.provider_label} 返回验证码", status="waiting_sms")
                try:
                    code = sms_client.wait_code(activation, cancel_event=job.cancel_event)
                    job.add_log("已收到短信验证码（内容已隐藏）")
                except SmsError as exc:
                    cancel_number(activation)
                    self._check_cancel(job)
                    job.add_log(f"轮询短信失败：{exc}", "warning")
                    if may_acquire_again() and any(marker in str(exc).lower() for marker in ("超时", "timeout", "激活已结束", "status_cancel")):
                        raise PhoneNumberRestartRequired("号码未收到验证码，重新授权后换号") from exc
                    raise
            else:
                code = self._wait_input(job, "sms_code")
            try:
                self._check_cancel(job)
            except (PhoneRequiredSkipped, JobCancelled):
                # ``wait_code`` returns the code before OAuth accepts it.  A
                # concurrent skip/cancel must still release that activation.
                cancel_number(activation)
                raise
            self._set_step(job, "sms", "提交短信验证码")
            try:
                client.submit_phone_otp(code)
            except Exception as exc:
                cancel_number(activation)
                self._check_cancel(job)
                if _is_stale_signin_session_error(exc):
                    raise AuthSessionRestartRequired(
                        "sign-in session is no longer valid; restarting authorization session"
                    ) from exc
                job.add_log(f"短信验证码验证失败：{exc}", "warning")
                if automatic and may_acquire_again():
                    raise PhoneNumberRestartRequired("号码验证码验证失败，重新授权后换号") from exc
                if automatic:
                    # The automatic activation was released above.  Do not
                    # ask for a second code against that cancelled number;
                    # switch to a fresh manual number instead.
                    raise ImportErrorBase(f"取号 {attempts}/{settings.max_phone_attempts} 次均未完成") from exc
                # A bad manual code should request a fresh code on the same
                # number before requiring a new phone.
                code = self._wait_input(job, "sms_code")
                try:
                    client.submit_phone_otp(code)
                except Exception as exc:
                    if _is_stale_signin_session_error(exc):
                        raise AuthSessionRestartRequired(
                            "sign-in session is no longer valid; restarting authorization session"
                        ) from exc
                    sms_client = None
                    job._pending_phone = self._wait_input(job, "phone")
                    raise PhoneNumberRestartRequired("手工号码已更换，重新授权后提交") from exc
            # A skip request can race with the upstream OTP response.  Honor
            # it before marking the activation successful and returning.
            try:
                self._check_cancel(job)
            except (PhoneRequiredSkipped, JobCancelled):
                cancel_number(activation)
                raise
            if activation and sms_client and not activation.finished:
                sms_client.finish(activation)
            job._sms_activation = None
            job.sms_activation_id = ""
            job._sms_client = None
            job._sms_sent = False
            job._sms_keep_for_session_retry = False
            job.add_log("手机验证成功")
            job.phone_required = False
            job.phone_required_at = 0
            self.store.update(job.account_id, phone_required=False, phone_required_at=0)
            try:
                client.consent_probe()
                job.add_log("已刷新 consent 会话")
            except Exception as exc:
                # The probe is a navigation hint; the workspace endpoint is
                # still authoritative, so keep going if it is unavailable.
                job.add_log(f"consent 刷新提示：{exc}", "warning")
            return

    def _cancel_orphan_activations(self, sms_client: Any) -> None:
        """Best-effort cancel of SMS leases orphaned by a process restart."""
        with self._lock:
            orphans = self._orphan_sms_activation_ids
            if not orphans:
                return
            self._orphan_sms_activation_ids = {}
        for job_id, activation_id in orphans.items():
            try:
                sms_client.cancel(SmsActivation(
                    activation_id=activation_id,
                    phone="",
                    country="",
                    api_key="",
                ))
            except Exception:
                # 供应商侧取消未确认：保留台账，等下一个可用 client 重试。
                with self._lock:
                    self._orphan_sms_activation_ids[job_id] = activation_id

    def _release_retained_activation(self, job: ImportJob) -> None:
        sms_client, activation = job._sms_client, job._sms_activation
        job._sms_client = None
        job._sms_activation = None
        job.sms_activation_id = ""
        job._sms_sent = False
        job._sms_keep_for_session_retry = False
        self._cancel_activation(sms_client, activation)

    def _run_job(self, job_id: str) -> None:
        # Session recovery can be unlimited. Keep it iterative so repeated
        # stale sessions never accumulate recursive worker calls.
        while True:
            delay = self._run_job_once(job_id)
            if delay is None:
                return
            with self._lock:
                job = self._jobs.get(job_id)
            if job is None:
                return
            # The next iteration records cancellation and releases any lease.
            job.cancel_event.wait(delay)

    def _run_job_once(self, job_id: str) -> float | None:
        with self._lock:
            job = self._jobs.get(job_id)
        if not job:
            return
        # A queued task can be cancelled just before its worker is scheduled.
        # Check the event before creating a network session so cancellation
        # never resurrects a job that the UI already marked as stopped.
        if job.cancel_event.is_set():
            self._release_retained_activation(job)
            with self._lock:
                job.status = "cancelled"
                job.step = "cancelled"
                job.finished_at = int(time.time())
                job.add_log("任务已取消", "warning")
                self._persist_jobs_locked()
            self.store.update(job.account_id, status="cancelled", last_error="任务已取消")
            return
        account = self.store.get(job.account_id)
        if not account:
            self._release_retained_activation(job)
            self._fail_job(job, "导入账号已不存在")
            return
        with self._lock:
            job.started_at = int(time.time())
            job._on_change = self._persist_jobs
            self._persist_jobs_locked()
        client: ProtocolClient | None = None
        slot_acquired = False
        retry_requested = False
        retry_delay = 0.0

        def request_session_restart(error: Exception) -> None:
            nonlocal retry_requested, retry_delay
            job.session_retries += 1
            if job.session_retries > _SESSION_RESTART_LIMIT:
                # 连续重建会话仍失效:停止无限重排,按失败收敛,避免 job
                # 在队列中永久循环占用并发槽位。
                job.status = "failed"
                job.step = "failed"
                job.progress = 100
                job.error = redact_log(str(error))
                job.waiting_for = ""
                job.finished_at = int(time.time())
                job.add_log(
                    f"登录会话连续失效 {job.session_retries - 1} 次，停止重建会话",
                    "error",
                )
                self.store.update(job.account_id, status="failed", last_error=job.error)
                with self._lock:
                    self._persist_jobs_locked()
                return
            job.status = "queued"
            job.step = "queued"
            job.progress = 0
            job.error = redact_log(str(error))
            job.waiting_for = ""
            retry_delay = min(30.0, 2.0 ** min(job.session_retries - 1, 5))
            reason = "号码验证未完成" if isinstance(error, PhoneNumberRestartRequired) else "登录会话已失效"
            job.add_log(
                f"{reason}，关闭旧会话后重新授权（第 {job.session_retries} 次，{retry_delay:g}s 后）"
                + ("；保留原号码" if job._sms_activation is not None else ""),
                "warning",
            )
            self.store.update(job.account_id, status="queued", last_error=job.error)
            with self._lock:
                self._persist_jobs_locked()
            retry_requested = True
        try:
            # The executor has a fixed upper bound; this gate is the live,
            # operator-configurable limit for network/OAuth work.
            self._acquire_worker_slot(job)
            slot_acquired = True
            self._set_step(job, "bootstrap", "建立 HTTP 会话并初始化 PKCE", status="running")
            client = ProtocolClient(account.email, timeout=self.timeout)
            client.bootstrap()
            self._set_step(job, "email", "提交邮箱")
            client.submit_email()
            job.add_log("邮箱提交成功")
            self._set_step(job, "password", "提交密码")
            client.verify_password(account.password)
            job.add_log("密码验证成功")
            self._set_step(job, "mfa", "解析 MFA 因子并申请 challenge")
            mfa_id = client.mfa_id()
            client.issue_mfa_challenge(mfa_id)
            job.add_log("MFA challenge 已申请")
            mfa_response = None
            for attempt in range(2):
                self._check_cancel(job)
                code = totp_from_secret(account.totp_secret)
                try:
                    mfa_response = client.verify_mfa(mfa_id, code)
                    job.add_log("2FA 验证成功")
                    break
                except Exception as exc:
                    if _is_stale_signin_session_error(exc):
                        raise AuthSessionRestartRequired(str(exc)) from exc
                    if attempt >= 1:
                        raise
                    job.add_log(f"2FA 第一次验证失败，重新申请 challenge：{exc}", "warning")
                    mfa_id = client.mfa_id()
                    client.issue_mfa_challenge(mfa_id)
            try:
                mfa_data = mfa_response.json() if mfa_response is not None else {}
            except Exception:
                mfa_data = {}
            if not isinstance(mfa_data, Mapping) or not mfa_data:
                # ``post_json`` keeps the decoded response even for response
                # wrappers whose ``.json()`` method is unavailable.
                mfa_data = dict(client.last_auth_response or {})
            phone_required, phone_reason = _phone_requirement(mfa_data)
            session_data = mfa_data.get("oai-client-auth-session") if isinstance(mfa_data, Mapping) else {}
            if not isinstance(session_data, Mapping) or not session_data:
                session_data = client.auth_session_data
            if phone_required is True:
                # A few existing-phone sessions have briefly returned the
                # add-phone navigation hint even though workspace selection
                # is already allowed.  If the session exposes a verified
                # number, try the direct workspace route before asking for a
                # new number.  A failed probe leaves the normal phone flow
                # untouched.
                if _session_has_phone(session_data):
                    job.add_log("认证会话已有手机号标记，先尝试直接进入 workspace")
                    try:
                        self._set_step(job, "workspace", "选择 workspace（尝试跳过手机号）")
                        callback = client.select_workspace()
                        job.add_log("服务端允许跳过手机号步骤")
                    except Exception as exc:
                        callback = None
                        job.add_log(f"直接进入 workspace 未通过（{type(exc).__name__}），继续手机号步骤")
                else:
                    callback = None
                if callback is None:
                    job.add_log(f"服务端下一步为手机号验证（{phone_reason}）")
                    self._phone_step(job, client)
            elif phone_required is False:
                job.add_log(f"当前账号无需手机号，跳过接码（{phone_reason}）")
                job.phone_required = False
                job.phone_required_at = 0
                self.store.update(account.id, phone_required=False, phone_required_at=0)
                callback = None
            else:
                # Unknown response shape: try the normal workspace route
                # first.  If the server explicitly says that the auth step
                # is add-phone, fall back to the phone flow and retry.
                job.add_log("MFA 未返回明确手机号步骤，先尝试直接进入 workspace")
                callback = None
            if callback is None:
                self._set_step(job, "workspace", "选择 workspace")
                try:
                    callback = client.select_workspace()
                except Exception as exc:
                    if phone_required is None and _looks_like_phone_step_error(exc):
                        job.add_log("workspace 返回手机号步骤，切换到手机号验证")
                        self._phone_step(job, client)
                        callback = client.select_workspace()
                    else:
                        raise
            query = parse_qs(urlparse(callback).query)
            state = query.get("state", [""])[0]
            if state and state != client.state:
                raise ImportErrorBase("OAuth state 校验失败")
            oauth_code = query.get("code", [""])[0]
            if not oauth_code:
                raise ImportErrorBase("workspace 回调缺少授权 code")
            self._set_step(job, "token", "交换 OAuth Token")
            token = exchange_code(oauth_code, client.verifier)
            output_path = self.output_dir / f"{_safe_filename(account.email)}.json"
            record = save_auth_record(token, account.email, output_path)
            mirror_path = ""
            if self.mirror_dir is not None and self.mirror_dir.resolve() != self.output_dir.resolve():
                # Keep the operator-facing filename stable and readable.  The
                # external service accepts arbitrary auth-file names and the
                # email is the same stem used by the project data directory.
                mirror_name = f"{_safe_filename(account.email)}.json"
                mirror_path_obj = self.mirror_dir / mirror_name
                try:
                    save_auth_record(token, account.email, mirror_path_obj)
                    mirror_path = str(mirror_path_obj)
                except (OSError, ValueError) as exc:
                    job.add_log(f"同步服务目录失败（主 Token 已保存）：{exc}", "warning")
            job.token_path = mirror_path or str(output_path)
            job.status = "succeeded"
            job.step = "saved"
            job.progress = 100
            job.finished_at = int(time.time())
            job.add_log(f"Token 已保存：{output_path}" + (f"，已同步本地服务目录" if mirror_path else ""))
            self.store.update(
                account.id,
                status="succeeded",
                last_error="",
                token_path=job.token_path,
                phone_required=False,
                phone_required_at=0,
            )
            with self._lock:
                self._persist_jobs_locked()
        except AuthSessionRestartRequired as exc:
            # A stale sign-in session cannot be repaired by retrying the
            # current phone/OTP step.  Let the finally block close the old
            # client, then iteratively run the complete flow with a fresh
            # ProtocolClient (new cookies, device id, PKCE state).
            request_session_restart(exc)
        except PhoneRequiredSkipped as exc:
            with self._lock:
                job.status = "skipped_phone"
                job.step = "skipped_phone"
                job.progress = 100
                job.error = ""
                job.waiting_for = ""
                job.finished_at = int(time.time())
                job.add_log(str(exc), "warning")
                self._persist_jobs_locked()
            self.store.update(
                account.id,
                status="needs_phone",
                last_error=str(exc),
                phone_required=True,
                phone_required_at=job.phone_required_at or int(time.time()),
            )
        except JobCancelled as exc:
            with self._lock:
                job.status = "cancelled"
                job.step = "cancelled"
                job.error = str(exc)
                job.finished_at = int(time.time())
                job.add_log(str(exc), "warning")
                self._persist_jobs_locked()
            self.store.update(account.id, status="cancelled", last_error=str(exc))
        except Exception as exc:
            message = str(exc)
            if _is_stale_signin_session_error(exc):
                request_session_restart(exc)
            elif _is_retryable_network_error(message) and job.network_retries < 3:
                # Do not reuse the partially authenticated client.  The
                # finally block closes it and releases the worker slot before
                # the fresh session is queued again.
                job.network_retries += 1
                job.status = "queued"
                job.step = "queued"
                job.progress = 0
                job.error = redact_log(message)
                job.waiting_for = ""
                job.add_log(
                    f"网络/代理暂时不可用，{min(3 - job.network_retries, 3)} 次自动重试后仍失败将停止：{message[:180]}",
                    "warning",
                )
                self.store.update(job.account_id, status="queued", last_error=job.error)
                with self._lock:
                    self._persist_jobs_locked()
                retry_requested = True
                retry_delay = min(8.0, 2.0 ** (job.network_retries - 1))
            else:
                self._fail_job(job, message)
        finally:
            if client is not None:
                try:
                    client.session.close()
                except Exception:
                    pass
            if slot_acquired:
                self._release_worker_slot()
            if not retry_requested or not job._sms_keep_for_session_retry or job._sms_sent:
                self._release_retained_activation(job)
        if retry_requested:
            # Backoff happens in the iterative owner after releasing the slot.
            return retry_delay
        return None

    def _fail_job(self, job: ImportJob, message: str) -> None:
        with self._lock:
            job.status = "failed"
            job.step = "failed"
            job.error = redact_log(message)
            job.finished_at = int(time.time())
            job.add_log(job.error, "error")
            self._persist_jobs_locked()
        self.store.update(job.account_id, status="failed", last_error=job.error)


def default_import_manager() -> ImportJobManager:
    root = _root()
    data = root / "data"
    return ImportJobManager(data_dir=data, output_dir=data, sms_settings_path=data / "smsbower.json")

"""Local Codex quota client and response normalisation.

The CPA management API deliberately stays behind this module.  The browser UI
receives the small, token-free report objects produced here; access tokens and
the management key never leave the server process.
"""

from __future__ import annotations

import base64
import binascii
import concurrent.futures
import json
import os
import re
import secrets
import tempfile
import threading
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping


DEFAULT_CPA_BASE_URL = "http://127.0.0.1:8317"  # optional legacy mode only
DEFAULT_AUTH_DIR = ""  # resolved to this project's data/ directory at runtime
DEFAULT_EXTERNAL_AUTH_DIR = "/opt/codex-services/data/cliproxy/auths"
DEFAULT_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
DEFAULT_RESET_CREDITS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
DEFAULT_RESET_CONSUME_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume"
DEFAULT_CONFIG_NAME = "quota-config.json"
DEFAULT_REPORT_NAME = "quota-report.json"
DEFAULT_ACTION_KEY_NAME = "quota-action-key"
DEFAULT_TIMEOUT = 30.0
DEFAULT_CONCURRENCY = 6
DEFAULT_IMPORT_CONCURRENCY = 2
MAX_IMPORT_CONCURRENCY = 32
DEFAULT_CACHE_SECONDS = 30

_USER_AGENT = "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal"
_EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)


class QuotaError(RuntimeError):
    """A request or payload error suitable for showing in the dashboard."""


def _first(mapping: Mapping[str, Any] | None, *keys: str) -> Any:
    if not isinstance(mapping, Mapping):
        return None
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1", "on"}:
            return True
        if lowered in {"false", "no", "0", "off"}:
            return False
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _count(value: Any) -> int | None:
    """Return a non-negative integer count while tolerating API strings."""
    number = _number(value)
    if number is None or number < 0:
        return None
    return int(number)


def _decode_jwt_payload(value: Any) -> dict[str, Any] | None:
    """Decode a JWT payload without verifying it (claims are display hints)."""
    if not isinstance(value, str):
        return None
    parts = value.split(".")
    if len(parts) < 2:
        return None
    try:
        raw = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = base64.urlsafe_b64decode(raw.encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except (ValueError, UnicodeError, binascii.Error, json.JSONDecodeError):
        return None


def _claim_sources(record: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    """Yield normalised claim dictionaries found in an auth-files record."""
    seen: set[int] = set()

    def visit(value: Any) -> Iterable[Mapping[str, Any]]:
        if isinstance(value, Mapping):
            marker = id(value)
            if marker in seen:
                return
            seen.add(marker)
            yield value
            for nested in (value.get("metadata"), value.get("attributes"), value.get("claims")):
                yield from visit(nested)
            for token_key in ("id_token", "access_token"):
                token = value.get(token_key)
                decoded = _decode_jwt_payload(token)
                if decoded:
                    yield from visit(decoded)
        elif isinstance(value, str):
            decoded = _decode_jwt_payload(value)
            if decoded:
                yield from visit(decoded)

    for key in ("id_token", "metadata", "attributes", "claims", "access_token"):
        yield from visit(record.get(key))


def extract_account_id(record: Mapping[str, Any]) -> str:
    for source in (record, *_claim_sources(record)):
        value = _first(source, "account_id", "accountId", "chatgpt_account_id")
        if value:
            return _text(value)
        auth = source.get("https://api.openai.com/auth") if isinstance(source, Mapping) else None
        if isinstance(auth, Mapping):
            value = _first(auth, "chatgpt_account_id", "account_id", "accountId")
            if value:
                return _text(value)
    return ""


def extract_plan(record: Mapping[str, Any]) -> str:
    for source in (record, *_claim_sources(record)):
        value = _first(source, "plan_type", "planType", "chatgpt_plan_type")
        if value:
            return _text(value).lower()
        auth = source.get("https://api.openai.com/auth") if isinstance(source, Mapping) else None
        if isinstance(auth, Mapping):
            value = _first(auth, "chatgpt_plan_type", "plan_type", "planType")
            if value:
                return _text(value).lower()
    return "unknown"


def extract_email(record: Mapping[str, Any]) -> str:
    for source in (record, *_claim_sources(record)):
        for key in ("email", "account", "label", "name", "id"):
            value = _text(source.get(key))
            match = _EMAIL_RE.search(value)
            if match:
                return match.group(0)
    return "unknown"


def _safe_name(record: Mapping[str, Any], email: str) -> str:
    # CPA's ``name`` is usually a filename.  A label/account value is more
    # useful in the dashboard, with the email as the stable final fallback.
    name = _text(_first(record, "label", "account", "email", "name"))
    return name or email


@dataclass(frozen=True)
class AccountRef:
    email: str
    name: str
    auth_index: str
    account_id: str = ""
    plan: str = "unknown"
    disabled: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


def account_from_record(record: Mapping[str, Any]) -> AccountRef:
    email = extract_email(record)
    return AccountRef(
        email=email,
        name=_safe_name(record, email),
        auth_index=_text(_first(record, "auth_index", "authIndex", "index")),
        account_id=extract_account_id(record),
        plan=extract_plan(record),
        disabled=bool(_boolean(_first(record, "disabled", "is_disabled"))),
        raw=record,
    )


def _window_label(seconds: float | None, fallback: str) -> str:
    if seconds is None or seconds <= 0:
        return fallback
    if seconds <= 6 * 60 * 60:
        return "5 小时"
    if seconds <= 9 * 24 * 60 * 60:
        return "7 天"
    if seconds <= 40 * 24 * 60 * 60:
        return "月度"
    days = round(seconds / 86400)
    return f"{days} 天"


def _window_id(label: str, ordinal: int) -> str:
    lowered = label.lower()
    if "5" in lowered or "hour" in lowered or "小时" in lowered:
        return "5h"
    if "7" in lowered or "week" in lowered or "天" in lowered and ordinal == 1:
        return "7d"
    if "月" in lowered or "month" in lowered:
        return "month"
    return f"window-{ordinal}"


@dataclass
class QuotaWindow:
    id: str
    label: str
    remaining_percent: float | None = None
    used_percent: float | None = None
    reset_at: int | None = None
    reset_after_seconds: int | None = None
    exhausted: bool = False
    source_name: str = ""

    def as_dict(self, now: int | None = None) -> dict[str, Any]:
        now = int(time.time()) if now is None else now
        reset_in = None
        if self.reset_at:
            reset_in = max(0, int(self.reset_at - now))
        elif self.reset_after_seconds:
            reset_in = max(0, int(self.reset_after_seconds))
        return {
            "id": self.id,
            "label": self.label,
            "remaining_percent": self.remaining_percent,
            "used_percent": self.used_percent,
            "reset_at": self.reset_at,
            "reset_in_seconds": reset_in,
            "exhausted": self.exhausted,
        }


def _reset_at(window: Mapping[str, Any], now: int) -> tuple[int | None, int | None]:
    absolute = _number(_first(window, "reset_at", "resetAt"))
    relative = _number(_first(window, "reset_after_seconds", "resetAfterSeconds"))
    if absolute and absolute > 0:
        return int(absolute), int(max(0, relative or 0)) or None
    if relative and relative > 0:
        return now + int(relative), int(relative)
    return None, None


def build_window(
    raw: Mapping[str, Any] | None,
    *,
    ordinal: int,
    fallback_label: str,
    parent_limit_reached: Any = None,
    parent_allowed: Any = None,
) -> QuotaWindow | None:
    if not isinstance(raw, Mapping) or not raw:
        return None
    now = int(time.time())
    duration = _number(_first(raw, "limit_window_seconds", "limitWindowSeconds"))
    label = _window_label(duration, fallback_label)
    used = _number(_first(raw, "used_percent", "usedPercent"))
    if used is not None:
        used = max(0.0, min(100.0, used))
    reached = _boolean(parent_limit_reached)
    allowed = _boolean(parent_allowed)
    exhausted = bool(reached is True or allowed is False or (used is not None and used >= 100))
    reset_at, relative = _reset_at(raw, now)
    if used is None and exhausted:
        used = 100.0
    remaining = None if used is None else max(0.0, min(100.0, 100.0 - used))
    if remaining is None and exhausted:
        remaining = 0.0
    return QuotaWindow(
        id=_window_id(label, ordinal),
        label=label,
        remaining_percent=remaining,
        used_percent=used,
        reset_at=reset_at,
        reset_after_seconds=relative,
        exhausted=exhausted,
        source_name=fallback_label,
    )


def _window_candidates(rate_limit: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    candidates: list[tuple[str, Mapping[str, Any]]] = []
    for key, value in rate_limit.items():
        if not isinstance(value, Mapping):
            continue
        lowered = key.lower()
        if "window" in lowered and key not in {"primary_window", "primaryWindow", "secondary_window", "secondaryWindow"}:
            candidates.append((key, value))
    primary = _first(rate_limit, "primary_window", "primaryWindow")
    secondary = _first(rate_limit, "secondary_window", "secondaryWindow")
    ordered: list[tuple[str, Mapping[str, Any]]] = []
    if isinstance(primary, Mapping):
        ordered.append(("primary", primary))
    if isinstance(secondary, Mapping):
        ordered.append(("secondary", secondary))
    ordered.extend(candidates)
    return ordered


def parse_windows(rate_limit: Mapping[str, Any] | None) -> list[QuotaWindow]:
    if not isinstance(rate_limit, Mapping):
        return []
    reached = _first(rate_limit, "limit_reached", "limitReached")
    allowed = _first(rate_limit, "allowed")
    result: list[QuotaWindow] = []
    for ordinal, (key, raw) in enumerate(_window_candidates(rate_limit), 1):
        fallback = "5 小时" if ordinal == 1 else ("7 天" if ordinal == 2 else key)
        item = build_window(raw, ordinal=ordinal, fallback_label=fallback,
                            parent_limit_reached=reached, parent_allowed=allowed)
        if item is None:
            continue
        # A duration is more reliable than JSON key ordering when CPA changes
        # which window is primary after an account is exhausted.
        if item.id == "window-1" and ordinal == 1:
            item.id = "5h"
        result.append(item)
    # Keep the conventional order: 5h, 7d, month, then provider extras.
    rank = {"5h": 0, "7d": 1, "month": 2}
    result.sort(key=lambda item: (rank.get(item.id, 10), item.label))
    # Avoid duplicate IDs when an upstream payload contains aliases.
    seen: set[str] = set()
    unique: list[QuotaWindow] = []
    for item in result:
        if item.id in seen:
            item.id = f"{item.id}-{len(unique) + 1}"
        seen.add(item.id)
        unique.append(item)
    return unique


def parse_additional_windows(payload: Mapping[str, Any]) -> list[QuotaWindow]:
    result: list[QuotaWindow] = []
    raw_items = _first(payload, "additional_rate_limits", "additionalRateLimits")
    if isinstance(raw_items, list):
        for index, entry in enumerate(raw_items, 1):
            if not isinstance(entry, Mapping):
                continue
            rate = _first(entry, "rate_limit", "rateLimit")
            if not isinstance(rate, Mapping):
                continue
            name = _text(_first(entry, "limit_name", "limitName", "metered_feature", "meteredFeature")) or f"附加额度 {index}"
            windows = parse_windows(rate)
            for window in windows:
                window.id = f"additional-{index}-{window.id}"
                window.label = f"{name} · {window.label}"
                result.append(window)
    code_review = _first(payload, "code_review_rate_limit", "codeReviewRateLimit")
    if isinstance(code_review, Mapping):
        for window in parse_windows(code_review):
            window.id = f"code-review-{window.id}"
            window.label = f"代码审查 · {window.label}"
            result.append(window)
    return result


def _status(windows: list[QuotaWindow], error: str = "") -> str:
    if error:
        return "error"
    values = [w.remaining_percent for w in windows if w.remaining_percent is not None]
    if not values:
        return "unknown"
    remaining = min(values)
    if remaining <= 0:
        return "exhausted"
    if remaining <= 30:
        return "low"
    if remaining <= 70:
        return "medium"
    if remaining < 100:
        return "high"
    return "full"


def _normalise_reset_credit(value: Any) -> dict[str, Any] | None:
    """Keep the useful, non-sensitive fields from a reset-credit object."""
    if not isinstance(value, Mapping):
        return None
    item: dict[str, Any] = {}
    for output, keys in (
        ("id", ("id", "credit_id", "creditId")),
        ("status", ("status",)),
        ("granted_at", ("granted_at", "grantedAt")),
        ("expires_at", ("expires_at", "expiresAt")),
        ("redeem_started_at", ("redeem_started_at", "redeemStartedAt")),
        ("redeemed_at", ("redeemed_at", "redeemedAt")),
        ("title", ("title",)),
        ("description", ("description",)),
    ):
        value_found = _first(value, *keys)
        if value_found is not None:
            item[output] = _text(value_found)
    supported = _boolean(_first(value, "is_supported_by_plan", "isSupportedByPlan"))
    if supported is not None:
        item["supported_by_plan"] = supported
    return item or None


def parse_reset_credits(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalise the standalone ``rate-limit-reset-credits`` response.

    CPA and the upstream endpoint have used both camelCase and snake_case over
    time.  The dashboard receives one stable shape and never receives a token.
    """
    if not isinstance(payload, Mapping):
        return {}
    raw_credits = _first(payload, "credits", "items")
    credits: list[dict[str, Any]] = []
    if isinstance(raw_credits, list):
        for raw in raw_credits:
            item = _normalise_reset_credit(raw)
            if item is not None:
                credits.append(item)

    available = _count(_first(payload, "available_count", "availableCount"))
    if available is None:
        available = sum(1 for item in credits if item.get("status") == "available")
    applicable = _count(_first(payload, "applicable_available_count", "applicableAvailableCount"))
    if applicable is None:
        applicable = sum(
            1 for item in credits
            if item.get("status") == "available" and item.get("supported_by_plan") is not False
        )

    result: dict[str, Any] = {
        "available_count": available,
        "applicable_available_count": applicable,
        "credits": credits,
    }
    for output, keys in (
        ("total_earned_count", ("total_earned_count", "totalEarnedCount")),
        ("immediate_reset_purchase_eligible", ("immediate_reset_purchase_eligible", "immediateResetPurchaseEligible")),
    ):
        value = _first(payload, *keys)
        if value is not None:
            result[output] = _count(value) if output == "total_earned_count" else bool(_boolean(value))
    return result


def parse_usage(
    payload: Mapping[str, Any],
    account: AccountRef,
    *,
    source: str = "local",
    reset_credits_payload: Mapping[str, Any] | None = None,
    reset_credits_error: str = "",
) -> dict[str, Any]:
    rate_limit = _first(payload, "rate_limit", "rateLimit")
    windows = parse_windows(rate_limit if isinstance(rate_limit, Mapping) else None)
    additional = parse_additional_windows(payload)
    plan = _text(_first(payload, "plan_type", "planType")) or account.plan
    credits = _first(payload, "rate_limit_reset_credits", "rateLimitResetCredits")
    reset_credits: dict[str, Any] = {}
    if isinstance(credits, Mapping):
        for key in ("available_count", "availableCount", "applicable_available_count", "applicableAvailableCount"):
            if key in credits:
                reset_credits[key] = credits[key]
    if reset_credits_payload is not None:
        # The standalone endpoint contains expiry/status details.  Match the
        # Codex client precedence: its available count wins, while an
        # explicit applicable count from usage (including zero) is retained.
        details = parse_reset_credits(reset_credits_payload)
        if details:
            usage_available = _count(_first(reset_credits, "available_count", "availableCount"))
            usage_applicable = _count(_first(reset_credits, "applicable_available_count", "applicableAvailableCount"))
            if details.get("available_count") is not None:
                reset_credits["available_count"] = details["available_count"]
            elif usage_available is not None:
                reset_credits["available_count"] = usage_available
            if usage_applicable is not None:
                reset_credits["applicable_available_count"] = usage_applicable
            elif details.get("applicable_available_count") is not None:
                reset_credits["applicable_available_count"] = details["applicable_available_count"]
            for key in ("credits", "total_earned_count", "immediate_reset_purchase_eligible"):
                if key in details:
                    reset_credits[key] = details[key]
    if reset_credits_error:
        reset_credits["error"] = reset_credits_error[:300]
    checked = int(time.time())
    return {
        "email": account.email,
        "name": account.name,
        "auth_index": account.auth_index,
        "account_id": account.account_id,
        "plan": plan.lower() if plan else "unknown",
        "status": _status(windows + additional),
        "disabled": account.disabled,
        "source": source,
        "windows": [window.as_dict(checked) for window in windows],
        "additional_windows": [window.as_dict(checked) for window in additional],
        "reset_credits": reset_credits,
        "error": "",
        "checked_at": checked,
    }


def error_account(account: AccountRef, message: str, *, source: str = "local") -> dict[str, Any]:
    return {
        "email": account.email,
        "name": account.name,
        "auth_index": account.auth_index,
        "account_id": account.account_id,
        "plan": account.plan,
        "status": "disabled" if account.disabled else "error",
        "disabled": account.disabled,
        "source": source,
        "windows": [],
        "additional_windows": [],
        "reset_credits": {},
        "error": message[:500],
        "checked_at": int(time.time()),
    }


@dataclass
class QuotaConfig:
    # local: read credential JSON files and query the usage endpoint directly.
    # cpa: use the CPA management API (kept as an explicit opt-in for legacy
    # installations; it is never selected by the default configuration).
    mode: str = "local"
    base_url: str = DEFAULT_CPA_BASE_URL
    management_key: str = ""
    auth_dir: str = DEFAULT_AUTH_DIR
    auth_dirs: tuple[str, ...] = ()
    timeout: float = DEFAULT_TIMEOUT
    concurrency: int = DEFAULT_CONCURRENCY
    cache_seconds: int = DEFAULT_CACHE_SECONDS
    report_path: Path | None = None
    # Mutating dashboard actions (currently reset-credit consumption) require
    # this separate key.  When omitted, QuotaClient creates a 600-permission
    # key file on first use instead of exposing an unauthenticated public POST.
    action_key: str = ""
    action_key_path: Path | None = None
    # The loaded path is retained so a dashboard setting can persist back to
    # the same file, including when --config points at a custom location.
    config_path: Path | None = None
    # OAuth import workers are independent from quota-query workers.  Kept at
    # the end of the dataclass to preserve positional compatibility for older
    # callers that constructed QuotaConfig directly.
    import_concurrency: int = DEFAULT_IMPORT_CONCURRENCY
    skip_phone_required: bool = False


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_config_path() -> Path:
    return _project_root() / "data" / DEFAULT_CONFIG_NAME


def load_config(path: str | Path | None = None) -> QuotaConfig:
    config_path = Path(path) if path else default_config_path()
    data: dict[str, Any] = {}
    if config_path.exists():
        try:
            parsed = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                data = parsed
        except (OSError, json.JSONDecodeError) as exc:
            raise QuotaError(f"读取配额配置失败: {exc}") from exc
    mode = _text(data.get("mode") or os.getenv("QUOTA_MODE") or "local").lower()
    if mode not in {"local", "cpa", "auto"}:
        mode = "local"
    base_url = _text(data.get("base_url") or data.get("cpa_base_url") or os.getenv("CPA_BASE_URL")) or DEFAULT_CPA_BASE_URL
    key = _text(data.get("management_key") or data.get("cpa_management_key") or os.getenv("CPA_MANAGEMENT_KEY"))
    auth_dir = _text(data.get("auth_dir") or os.getenv("QUOTA_AUTH_DIR"))
    if not auth_dir:
        auth_dir = str(_project_root() / "data")
    raw_dirs = data.get("auth_dirs")
    auth_dirs: list[str] = []
    if isinstance(raw_dirs, list):
        auth_dirs.extend(_text(item) for item in raw_dirs if _text(item))
    # Keep the project's data directory first, then allow a separately mounted
    # local credential directory.  Neither path implies a CPA HTTP dependency.
    project_data = str(_project_root() / "data")
    if project_data not in auth_dirs and auth_dir != project_data:
        auth_dirs.insert(0, project_data)
    if auth_dir not in auth_dirs:
        auth_dirs.insert(0, auth_dir)
    try:
        timeout = max(3.0, float(data.get("timeout", os.getenv("CPA_TIMEOUT", DEFAULT_TIMEOUT))))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    try:
        concurrency = max(1, min(32, int(data.get("concurrency", os.getenv("CPA_CONCURRENCY", DEFAULT_CONCURRENCY)))))
    except (TypeError, ValueError):
        concurrency = DEFAULT_CONCURRENCY
    try:
        import_concurrency = max(
            1,
            min(
                MAX_IMPORT_CONCURRENCY,
                int(data.get("import_concurrency", os.getenv("OAUTH_IMPORT_CONCURRENCY", DEFAULT_IMPORT_CONCURRENCY))),
            ),
        )
    except (TypeError, ValueError):
        import_concurrency = DEFAULT_IMPORT_CONCURRENCY
    skip_phone_required = _boolean(
        data.get("skip_phone_required", os.getenv("OAUTH_SKIP_PHONE_REQUIRED", False))
    ) is True
    try:
        cache_seconds = max(0, int(data.get("cache_seconds", DEFAULT_CACHE_SECONDS)))
    except (TypeError, ValueError):
        cache_seconds = DEFAULT_CACHE_SECONDS
    report_value = data.get("report_path")
    report_path = Path(report_value) if report_value else config_path.parent / DEFAULT_REPORT_NAME
    if not report_path.is_absolute():
        report_path = _project_root() / report_path
    action_key = _text(data.get("action_key") or os.getenv("QUOTA_ACTION_KEY"))
    action_key_value = data.get("action_key_path")
    action_key_path = Path(action_key_value) if action_key_value else config_path.parent / DEFAULT_ACTION_KEY_NAME
    if not action_key_path.is_absolute():
        action_key_path = _project_root() / action_key_path
    return QuotaConfig(mode=mode, base_url=base_url.rstrip("/"), management_key=key,
                       auth_dir=auth_dir, auth_dirs=tuple(dict.fromkeys(auth_dirs)),
                       timeout=timeout, concurrency=concurrency,
                       import_concurrency=import_concurrency,
                       skip_phone_required=skip_phone_required,
                       cache_seconds=cache_seconds, report_path=report_path,
                       action_key=action_key, action_key_path=action_key_path,
                       config_path=config_path)


def save_import_concurrency(config: QuotaConfig, value: Any) -> int:
    return int(save_import_settings(config, concurrency=value)["concurrency"])


def save_import_settings(
    config: QuotaConfig,
    *,
    concurrency: Any | None = None,
    skip_phone_required: Any | None = None,
) -> dict[str, Any]:
    """Persist OAuth import concurrency and phone-skip behavior atomically."""
    if concurrency is None:
        normalized = int(config.import_concurrency)
    else:
        if isinstance(concurrency, bool):
            raise QuotaError("导入并发数必须是整数")
        try:
            normalized = max(1, min(MAX_IMPORT_CONCURRENCY, int(str(concurrency).strip())))
        except (TypeError, ValueError) as exc:
            raise QuotaError(f"导入并发数必须是 1-{MAX_IMPORT_CONCURRENCY} 的整数") from exc
    path = config.config_path or default_config_path()
    data: dict[str, Any] = {}
    if path.exists():
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                data = parsed
        except (OSError, json.JSONDecodeError) as exc:
            raise QuotaError(f"读取配额配置失败: {exc}") from exc
    if skip_phone_required is None:
        # Preserve an existing key for the legacy concurrency-only setter,
        # while still honoring a freshly loaded/explicit runtime config when
        # the key has not existed before.
        existing_skip = data.get("skip_phone_required", config.skip_phone_required)
        if isinstance(existing_skip, str):
            skip_value = existing_skip.strip().lower() in {"1", "true", "yes", "on"}
        else:
            skip_value = bool(existing_skip)
    elif isinstance(skip_phone_required, str):
        skip_value = skip_phone_required.strip().lower() in {"1", "true", "yes", "on"}
    else:
        skip_value = bool(skip_phone_required)
    data["import_concurrency"] = normalized
    data["skip_phone_required"] = skip_value
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
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
    except OSError as exc:
        raise QuotaError(f"保存配额配置失败: {exc}") from exc
    config.import_concurrency = normalized
    config.skip_phone_required = skip_value
    config.config_path = path
    return {"concurrency": normalized, "skip_phone_required": skip_value}


def save_quota_settings(config: QuotaConfig, *, concurrency: Any | None = None,
                        cache_seconds: Any | None = None) -> dict[str, int]:
    """Persist live quota-query tuning without discarding other settings.

    The dashboard can change these values while it is running.  A currently
    active sweep keeps its existing worker pool; the next sweep uses the new
    values.  Keeping the write atomic means a browser refresh or service
    restart can never observe a half-written JSON config.
    """
    if concurrency is None:
        normalized_concurrency = int(config.concurrency)
    else:
        if isinstance(concurrency, bool):
            raise QuotaError("配额查询并发数必须是整数")
        try:
            normalized_concurrency = int(str(concurrency).strip())
        except (TypeError, ValueError) as exc:
            raise QuotaError("配额查询并发数必须是 1-32 的整数") from exc
        if not 1 <= normalized_concurrency <= 32:
            raise QuotaError("配额查询并发数必须是 1-32 的整数")
    if cache_seconds is None:
        normalized_cache = int(config.cache_seconds)
    else:
        if isinstance(cache_seconds, bool):
            raise QuotaError("配额缓存时间必须是整数")
        try:
            normalized_cache = int(str(cache_seconds).strip())
        except (TypeError, ValueError) as exc:
            raise QuotaError("配额缓存时间必须是 0-3600 秒的整数") from exc
        if not 0 <= normalized_cache <= 3600:
            raise QuotaError("配额缓存时间必须是 0-3600 秒的整数")

    path = config.config_path or default_config_path()
    data: dict[str, Any] = {}
    if path.exists():
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                data = parsed
        except (OSError, json.JSONDecodeError) as exc:
            raise QuotaError(f"读取配额配置失败: {exc}") from exc
    data["concurrency"] = normalized_concurrency
    data["cache_seconds"] = normalized_cache
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
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
    except OSError as exc:
        raise QuotaError(f"保存配额配置失败: {exc}") from exc
    config.concurrency = normalized_concurrency
    config.cache_seconds = normalized_cache
    config.config_path = path
    return {"concurrency": normalized_concurrency, "cache_seconds": normalized_cache}


def _json_request(config: QuotaConfig, method: str, url: str, payload: Any = None) -> Any:
    body = None
    headers = {"Accept": "application/json", "User-Agent": "codex-quota/1.0"}
    if config.management_key and url.startswith(config.base_url):
        headers["Authorization"] = f"Bearer {config.management_key}"
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=config.timeout) as response:
            raw = response.read()
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise QuotaError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise QuotaError(f"连接失败: {exc.reason}") from exc
    except TimeoutError as exc:
        raise QuotaError("请求超时") from exc
    except json.JSONDecodeError as exc:
        raise QuotaError("上游返回了非 JSON 数据") from exc


class QuotaClient:
    """Fetch and mutate Codex quota data without proxying tokens to the UI."""

    def __init__(self, config: QuotaConfig):
        self.config = config
        self._reset_lock = threading.Lock()
        self._action_key_lock = threading.Lock()
        # Local mode used to re-read every auth JSON for every account and for
        # both usage requests.  With a large pool that turns one refresh into
        # an O(accounts²) disk scan.  Keep a short-lived immutable index so a
        # refresh parses each file once; fetch_report invalidates it at the
        # start of a new sweep after imports have written new files.
        self._local_cache_lock = threading.RLock()
        self._local_cache_at = 0.0
        self._local_cache_dirs: tuple[str, ...] = ()
        self._local_cache_entries: tuple[tuple[Path, Mapping[str, Any], str, str], ...] = ()
        # The cache is explicitly invalidated at the beginning of each full
        # report and before account mutations, so it can safely span a slow
        # upstream sweep without rebuilding halfway through it.
        self._local_cache_ttl = 60.0

    def list_accounts(self) -> list[AccountRef]:
        if not self.config.management_key:
            raise QuotaError("未配置 CPA 管理密码（可设置 CPA_MANAGEMENT_KEY）")
        payload = _json_request(self.config, "GET", self.config.base_url + "/v0/management/auth-files")
        files = payload.get("files") if isinstance(payload, Mapping) else None
        if not isinstance(files, list):
            raise QuotaError("CPA auth-files 响应格式异常")
        accounts: list[AccountRef] = []
        for item in files:
            if not isinstance(item, Mapping):
                continue
            provider = _text(_first(item, "provider", "type")).lower()
            if provider != "codex":
                continue
            account = account_from_record(item)
            if account.auth_index:
                accounts.append(account)
        return accounts

    def _management_usage(self, account: AccountRef) -> Mapping[str, Any]:
        header: dict[str, str] = {
            "Authorization": "Bearer $TOKEN$",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        }
        if account.account_id:
            header["Chatgpt-Account-Id"] = account.account_id
        request_payload = {
            "auth_index": account.auth_index,
            "method": "GET",
            "url": DEFAULT_USAGE_URL,
            "header": header,
        }
        wrapper = _json_request(self.config, "POST", self.config.base_url + "/v0/management/api-call", request_payload)
        status_code = int(_number(_first(wrapper, "status_code", "statusCode")) or 0)
        body = wrapper.get("body") if isinstance(wrapper, Mapping) else None
        if status_code < 200 or status_code >= 300:
            detail = _text(body)[:300] or f"上游 HTTP {status_code}"
            raise QuotaError(detail)
        if isinstance(body, Mapping):
            return body
        if isinstance(body, str):
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError as exc:
                raise QuotaError("usage 响应不是 JSON") from exc
            if isinstance(parsed, Mapping):
                return parsed
        raise QuotaError("usage 响应为空")

    def _local_records(self, account: AccountRef) -> list[Mapping[str, Any]]:
        """Find credential records matching one dashboard account.

        The same email can occur in a project data directory and the mounted
        CPA auth directory.  Returning all matches lets callers try a fresh
        token if one duplicate has expired.
        """
        candidates: list[Mapping[str, Any]] = []
        wanted_email = account.email.lower()
        for file_path, record, record_email, record_index in self._local_record_entries():
            if (wanted_email == record_email
                    or record_index == account.auth_index
                    or file_path.stem == account.auth_index):
                candidates.append(record)
        return candidates

    def _invalidate_local_cache(self) -> None:
        with self._local_cache_lock:
            self._local_cache_at = 0.0

    def _local_record_entries(self) -> tuple[tuple[Path, Mapping[str, Any], str, str], ...]:
        """Read valid local auth records once and reuse them during a refresh."""
        directories = self._local_directories()
        now = time.monotonic()
        with self._local_cache_lock:
            if (
                self._local_cache_entries
                and self._local_cache_dirs == directories
                and now - self._local_cache_at < self._local_cache_ttl
            ):
                return self._local_cache_entries

            entries: list[tuple[Path, Mapping[str, Any], str, str]] = []
            for directory in directories:
                path = Path(directory)
                if not path.is_dir():
                    continue
                try:
                    files = sorted(path.glob("*.json"), key=lambda item: item.name.lower())
                except OSError:
                    continue
                for file_path in files:
                    try:
                        record = json.loads(file_path.read_text(encoding="utf-8"))
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(record, Mapping) or not _text(record.get("access_token")):
                        continue
                    email = extract_email(record).lower()
                    record_index = _text(record.get("auth_index"))
                    entries.append((file_path, record, email, record_index))
            self._local_cache_dirs = directories
            self._local_cache_entries = tuple(entries)
            self._local_cache_at = now
            return self._local_cache_entries

    def _local_request(
        self,
        account: AccountRef,
        *,
        url: str,
        method: str = "GET",
        payload: Any = None,
        accept: str = "application/json",
    ) -> Mapping[str, Any]:
        """Send one direct upstream request using a local credential."""
        candidates = self._local_records(account)
        if not candidates:
            raise QuotaError("本地 auth 文件未找到对应账号")
        last_error: Exception | None = None
        for record in candidates:
            token = _text(record.get("access_token"))
            if not token:
                last_error = QuotaError("本地 auth 文件缺少 access_token")
                continue
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": accept,
                "Content-Type": "application/json",
                "User-Agent": _USER_AGENT,
                "OpenAI-Beta": "codex-1",
                "Originator": "Codex Desktop",
            }
            account_id = account.account_id or _text(record.get("account_id")) or extract_account_id(record)
            if account_id:
                headers["Chatgpt-Account-Id"] = account_id
            body = None
            if payload is not None:
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            request = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                    raw = response.read()
                    if not raw:
                        return {}
                    try:
                        parsed = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        # The consume endpoint's successful body is not part
                        # of its contract.  A 2xx POST is sufficient and must
                        # never be retried merely because its body is empty or
                        # plain text (which could consume a second card).
                        if method.upper() != "GET":
                            return {}
                        raise
                    if isinstance(parsed, Mapping):
                        return parsed
                    if method.upper() != "GET":
                        return {}
                    last_error = QuotaError("本地直连返回格式异常")
            except urllib.error.HTTPError as exc:
                # A duplicate local record may be stale while another copy is
                # still valid.  Try the remaining matching files before
                # surfacing the status to the dashboard.
                detail = exc.read().decode("utf-8", "replace")[:300]
                last_error = QuotaError(f"本地直连 HTTP {exc.code}{': ' + detail if detail else ''}")
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = QuotaError(f"本地直连失败: {exc}")
        raise last_error or QuotaError("本地直连返回格式异常")

    def _local_usage(self, account: AccountRef) -> Mapping[str, Any]:
        return self._local_request(account, url=DEFAULT_USAGE_URL)

    def _local_reset_credits(self, account: AccountRef) -> Mapping[str, Any]:
        return self._local_request(account, url=DEFAULT_RESET_CREDITS_URL)

    def consume_reset_credit(self, account: AccountRef) -> Mapping[str, Any]:
        """Consume one upstream Codex reset credit and return its fresh state.

        The request mirrors the Codex desktop client: a unique
        ``redeem_request_id`` is sent with the POST.  A lock prevents two
        dashboard clicks from redeeming concurrently for the same process.
        """
        if self.config.mode != "local":
            raise QuotaError("重置卡操作只支持 local 本地凭证模式")
        with self._reset_lock:
            before = parse_reset_credits(self._local_reset_credits(account))
            # CPA's own ``canResetQuota`` gate uses available_count.  The
            # applicable count is displayed as an informational hint but must
            # not hide a card that the upstream consume endpoint accepts.
            available = _count(before.get("available_count"))
            if available is None:
                available = _count(before.get("applicable_available_count"))
            if available is not None and available <= 0:
                raise QuotaError("该账号当前没有可用重置卡")
            self._local_request(
                account,
                url=DEFAULT_RESET_CONSUME_URL,
                method="POST",
                payload={"redeem_request_id": str(uuid.uuid4())},
            )
            try:
                return self._local_reset_credits(account)
            except Exception:
                # The POST may have succeeded even if the follow-up read is
                # interrupted.  QuotaService performs a full refresh next;
                # do not make the operator click again and risk a second
                # redemption.
                return {}

    def query_account(self, account: AccountRef) -> dict[str, Any]:
        if account.disabled:
            return error_account(account, "账号已禁用")
        # Local mode is intentionally direct: it never contacts CPA's
        # management port.  The other branches are explicit compatibility
        # modes selected in quota-config.json.
        if self.config.mode == "local":
            try:
                payload = self._local_usage(account)
                reset_payload: Mapping[str, Any] | None = None
                reset_error = ""
                try:
                    reset_payload = self._local_reset_credits(account)
                except Exception as exc:
                    # A transient reset-card request must not hide otherwise
                    # valid usage windows.  Keep the detail next to the card.
                    reset_error = str(exc)
                return parse_usage(payload, account, source="local",
                                   reset_credits_payload=reset_payload,
                                   reset_credits_error=reset_error)
            except Exception as exc:
                return error_account(account, str(exc), source="local")
        try:
            payload = self._management_usage(account)
            return parse_usage(payload, account, source="cpa")
        except Exception as first_error:  # auto mode can fall back to local files
            if self.config.mode != "auto":
                return error_account(account, str(first_error), source="cpa")
            try:
                payload = self._local_usage(account)
                return parse_usage(payload, account, source="local")
            except Exception as fallback_error:
                return error_account(account, f"{first_error}; 本地回退失败: {fallback_error}", source="local")

    def fetch_report(self) -> dict[str, Any]:
        source = self.config.mode
        if self.config.mode == "local":
            self._invalidate_local_cache()
            accounts = self._list_local_accounts()
        else:
            try:
                accounts = self.list_accounts()
            except Exception as exc:
                if self.config.mode != "auto":
                    raise
                source = "local"
                accounts = self._list_local_accounts()
                if not accounts:
                    raise QuotaError(str(exc)) from exc
        if not accounts:
            raise QuotaError("本地凭证目录中没有可用的 Codex JSON 文件")
        results: list[dict[str, Any]] = []
        workers = min(self.config.concurrency, max(1, len(accounts)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="quota") as pool:
            futures = [pool.submit(self.query_account, account) for account in accounts]
            for future in futures:
                results.append(future.result())
        if any(item.get("source") == "local" for item in results) and source == "auto":
            source = "mixed"
        report = build_report(results, source=source)
        if self.config.report_path:
            write_report(report, self.config.report_path)
        return report

    def _list_local_accounts(self) -> list[AccountRef]:
        accounts: list[AccountRef] = []
        seen: set[str] = set()
        for file_path, record, _, _ in self._local_record_entries():
            provider = _text(_first(record, "type", "provider")).lower()
            if provider and provider != "codex":
                continue
            auth = account_from_record(record)
            if not auth.auth_index:
                # Project token files do not have CPA's auth_index.  The
                # filename is a stable local selector for matching.
                auth = AccountRef(auth.email, auth.name, file_path.stem, auth.account_id, auth.plan, auth.disabled, record)
            identity = auth.email.lower() if auth.email and auth.email != "unknown" else str(file_path.resolve())
            if identity in seen:
                continue
            seen.add(identity)
            accounts.append(auth)
        return accounts

    def _local_directories(self) -> tuple[str, ...]:
        configured = self.config.auth_dirs or (self.config.auth_dir,)
        return tuple(dict.fromkeys(str(Path(item).expanduser()) for item in configured if _text(item)))

    def find_account(
        self,
        *,
        email: str = "",
        account_id: str = "",
        auth_index: str = "",
    ) -> AccountRef:
        """Resolve a dashboard selector to one local account."""
        requested_email = _text(email).lower()
        requested_id = _text(account_id)
        requested_index = _text(auth_index)
        if self.config.mode == "local":
            self._invalidate_local_cache()
            accounts = self._list_local_accounts()
        else:
            accounts = self.list_accounts()
        matches = [
            account for account in accounts
            if (requested_id and account.account_id == requested_id)
            or (requested_index and account.auth_index == requested_index)
            or (requested_email and account.email.lower() == requested_email)
        ]
        if not matches:
            raise QuotaError("找不到对应的本地 Codex 账号")
        if len(matches) > 1 and requested_id:
            exact = [item for item in matches if item.account_id == requested_id]
            if exact:
                return exact[0]
        return matches[0]

    def action_key(self) -> str:
        """Read or create the key protecting mutating dashboard actions."""
        if self.config.action_key:
            return self.config.action_key
        path = self.config.action_key_path
        if path is None:
            path = _project_root() / "data" / DEFAULT_ACTION_KEY_NAME
        try:
            with self._action_key_lock:
                existing = _text(path.read_text(encoding="utf-8")) if path.exists() else ""
                if existing:
                    try:
                        os.chmod(path, 0o600)
                    except OSError:
                        pass
                    return existing
                path.parent.mkdir(parents=True, exist_ok=True)
                generated = secrets.token_urlsafe(32)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(generated + "\n")
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
                return generated
        except OSError as exc:
            raise QuotaError(f"创建配额操作密钥失败: {exc}") from exc


def _summary(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = {key: sum(1 for item in accounts if item.get("status") == key)
                for key in ("full", "high", "medium", "low", "exhausted", "error", "unknown", "disabled")}
    available = sum(statuses.get(key, 0) for key in ("full", "high", "medium"))
    low = statuses.get("low", 0) + statuses.get("medium", 0)
    return {
        "total": len(accounts),
        "available": available,
        "low": low,
        "exhausted": statuses.get("exhausted", 0),
        "errors": statuses.get("error", 0),
        "disabled": statuses.get("disabled", 0),
        "statuses": statuses,
    }


def full_quota_groups(accounts: Iterable[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Group accounts whose known quota windows are all exactly 100%.

    The dashboard only needs email addresses for this view.  A reset card is
    considered present when the provider reports a positive available count;
    legacy camelCase fields and a raw credit list are accepted as fallbacks.
    """
    with_reset: list[str] = []
    without_reset: list[str] = []
    seen_with: set[str] = set()
    seen_without: set[str] = set()
    for account in accounts:
        if not isinstance(account, Mapping) or account.get("error"):
            continue
        windows = [
            item
            for key in ("windows", "additional_windows")
            for item in (account.get(key) or [])
            if isinstance(item, Mapping)
        ]
        values: list[float] = []
        for window in windows:
            value = _number(window.get("remaining_percent"))
            if value is not None:
                values.append(value)
        if not values or any(abs(value - 100.0) > 1e-9 for value in values):
            continue
        email = _text(account.get("email"))
        if not email:
            continue
        credits = account.get("reset_credits")
        available: int | None = None
        if isinstance(credits, Mapping):
            for key in ("available_count", "availableCount", "applicable_available_count", "applicableAvailableCount"):
                if key in credits:
                    available = _count(credits.get(key))
                    if available is not None:
                        break
            if available is None and isinstance(credits.get("credits"), list):
                available = sum(
                    1
                    for item in credits["credits"]
                    if isinstance(item, Mapping)
                    and str(item.get("status") or "").lower() == "available"
                    and _boolean(item.get("supported_by_plan", item.get("is_supported_by_plan", True))) is not False
                )
        target, seen = ((with_reset, seen_with) if (available or 0) > 0 else (without_reset, seen_without))
        if email not in seen:
            seen.add(email)
            target.append(email)
    return {"with_reset_card": with_reset, "without_reset_card": without_reset}


def build_report(accounts: list[dict[str, Any]], *, source: str) -> dict[str, Any]:
    return {
        "generated_at": int(time.time()),
        "source": source,
        "summary": _summary(accounts),
        "full_quota_accounts": full_quota_groups(accounts),
        "accounts": accounts,
    }


def write_report(report: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".quota-report-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
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


class QuotaService:
    """Small cache around QuotaClient for the local dashboard."""

    def __init__(self, client: QuotaClient):
        self.client = client
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._report: dict[str, Any] | None = self._load_cached_report()
        # A persisted report is a safe, token-free snapshot, but it must not
        # be mistaken for a freshly checked result after a process restart.
        # Mark it stale so the first normal read returns it immediately while
        # queuing a background refresh.  This gives the UI both instant data
        # and a truthful “后台同步中” indicator.
        self._fetched_at = 0.0
        self._refreshing = False
        self._last_error = ""
        self._refresh_started_at = 0.0
        self._last_refresh_duration = 0.0
        # Local token deletion can race with a quota sweep that already read
        # the old file.  Track which deletion generation occurred after a
        # refresh began so that stale result cannot resurrect a removed row.
        self._mutation_generation = 0
        self._deleted_email_generation: dict[str, int] = {}

    def _load_cached_report(self) -> dict[str, Any] | None:
        path = self.client.config.report_path
        if path is None:
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict) or not isinstance(value.get("accounts"), list):
            return None
        return value

    def snapshot(self) -> dict[str, Any] | None:
        """Return the latest in-memory report without starting a refresh."""
        with self._lock:
            return dict(self._report) if self._report is not None else None

    @staticmethod
    def _without_accounts(report: Mapping[str, Any], emails: set[str]) -> dict[str, Any]:
        """Return a report with selected emails removed and summaries rebuilt."""
        updated = dict(report)
        accounts = [
            dict(item)
            for item in (report.get("accounts") or [])
            if isinstance(item, Mapping)
            and _text(item.get("email")).casefold() not in emails
        ]
        updated["accounts"] = accounts
        updated["summary"] = _summary(accounts)
        updated["full_quota_accounts"] = full_quota_groups(accounts)
        return updated

    def remove_accounts_from_snapshot(self, emails: Iterable[str]) -> dict[str, Any] | None:
        """Hide deleted local accounts immediately and protect against refresh races."""
        removed = {
            _text(email).casefold()
            for email in emails
            if _text(email)
        }
        if not removed:
            return self.snapshot()
        with self._lock:
            self._mutation_generation += 1
            generation = self._mutation_generation
            for email in removed:
                self._deleted_email_generation[email] = generation
            if self._report is None:
                return None
            self._report = self._without_accounts(self._report, removed)
            self._fetched_at = time.time()
            updated = dict(self._report)
            path = self.client.config.report_path
        if path is not None:
            try:
                write_report(updated, path)
            except OSError:
                # The in-memory inventory is authoritative for the running UI;
                # a disk snapshot failure must not undo the token deletion.
                pass
        return updated

    def _stale_report_locked(self) -> dict[str, Any]:
        if self._report is None:
            return {
                "generated_at": int(time.time()),
                "source": "refreshing",
                "summary": _summary([]),
                "accounts": [],
                "refreshing": True,
            }
        stale = dict(self._report)
        stale["stale"] = True
        stale["refreshing"] = True
        return stale

    def _fetch_and_store(self) -> dict[str, Any]:
        """Fetch one report and publish it, for sync or background callers."""
        started = time.monotonic()
        with self._lock:
            started_generation = self._mutation_generation
        try:
            report = self.client.fetch_report()
            report_path = self.client.config.report_path
            with self._lock:
                deleted_during_fetch = {
                    email
                    for email, generation in self._deleted_email_generation.items()
                    if generation > started_generation
                }
                if deleted_during_fetch:
                    report = self._without_accounts(report, deleted_during_fetch)
                # When a fetch started after the latest deletion, its source
                # scan already reflects the moved files and old race markers
                # can be dropped.
                if started_generation == self._mutation_generation:
                    self._deleted_email_generation = {
                        email: generation
                        for email, generation in self._deleted_email_generation.items()
                        if generation > started_generation
                    }
                self._report = report
                self._fetched_at = time.time()
                self._last_error = ""
            # ``QuotaClient.fetch_report`` may have written its pre-filter
            # result before a concurrent deletion was observed.  Persist the
            # final filtered snapshot as well so a service restart does not
            # briefly resurrect removed rows from disk.
            if report_path is not None:
                try:
                    write_report(report, report_path)
                except OSError:
                    pass
            return report
        except Exception as exc:
            message = str(exc)[:500]
            with self._lock:
                self._last_error = message
                if self._report is not None:
                    stale = dict(self._report)
                    stale["stale"] = True
                    stale["error"] = message
                    stale["refreshing"] = False
                    # Treat a failed refresh as a short-lived snapshot.  This
                    # prevents every browser poll from immediately launching
                    # another slow sweep while the upstream is unavailable.
                    self._report = stale
                    self._fetched_at = time.time()
                    return stale
            error_report = {
                "generated_at": int(time.time()),
                "source": "error",
                "summary": _summary([]),
                "accounts": [],
                "error": message,
                "refreshing": False,
            }
            # Keep the error response available for the normal cache window;
            # an explicit refresh can still start a new attempt immediately.
            with self._lock:
                self._report = error_report
                self._fetched_at = time.time()
            return error_report
        finally:
            with self._lock:
                self._refreshing = False
                self._last_refresh_duration = max(0.0, time.monotonic() - started)
                self._refresh_started_at = 0.0
                self._condition.notify_all()

    def refresh_async(self) -> dict[str, Any]:
        """Start a refresh once and return the current snapshot immediately."""
        with self._lock:
            if self._refreshing:
                return self._stale_report_locked()
            self._refreshing = True
            self._refresh_started_at = time.time()
            self._fetched_at = 0.0
            snapshot = self._stale_report_locked()
        threading.Thread(
            target=self._fetch_and_store,
            daemon=True,
            name="quota-background-refresh",
        ).start()
        return snapshot

    @property
    def refreshing(self) -> bool:
        with self._lock:
            return self._refreshing

    def report(self, force: bool = False) -> dict[str, Any]:
        with self._lock:
            fresh = self._report is not None and (time.time() - self._fetched_at) < self.client.config.cache_seconds
            if not force and fresh:
                return self._report or {}
            if not force:
                # A normal dashboard read must never wait for a full upstream
                # sweep.  Start (or join) the background refresh and return a
                # token-free snapshot/placeholder immediately.  Explicit
                # callers such as ``--once`` retain the synchronous ``force``
                # behaviour below.
                return self.refresh_async()
            if self._refreshing:
                while self._refreshing:
                    self._condition.wait(timeout=max(1.0, self.client.config.timeout + 2.0))
                    if not self._refreshing:
                        break
                if self._report is not None:
                    return self._report
            self._refreshing = True
            self._refresh_started_at = time.time()
        return self._fetch_and_store()

    def status(self) -> dict[str, Any]:
        with self._lock:
            generated_at = int(self._report.get("generated_at") or 0) if self._report else 0
            report_age = max(0, int(time.time() - generated_at)) if generated_at else None
            return {
                "refreshing": self._refreshing,
                "has_report": self._report is not None,
                "last_error": self._last_error,
                "last_checked": generated_at or (int(self._fetched_at) if self._fetched_at else None),
                "report_generated_at": generated_at or None,
                "report_age_seconds": report_age,
                "cache_seconds": self.client.config.cache_seconds,
                "account_count": len(self._report.get("accounts") or []) if self._report else 0,
                "refresh_started_at": int(self._refresh_started_at) if self._refresh_started_at else None,
                "last_refresh_duration_ms": int(self._last_refresh_duration * 1000) if self._last_refresh_duration else None,
                "mode": self.client.config.mode,
                "auth_dirs": list(self.client.config.auth_dirs),
            }

    def consume_reset_credit(
        self,
        *,
        email: str = "",
        account_id: str = "",
        auth_index: str = "",
    ) -> dict[str, Any]:
        """Redeem one reset card, then return a fresh token-free report."""
        account = self.client.find_account(email=email, account_id=account_id, auth_index=auth_index)
        self.client.consume_reset_credit(account)
        # The reset changes both the credit list and the rate-limit windows.
        # Publish the current snapshot immediately and refresh in the
        # background so a public browser/proxy does not wait for every account.
        report = self.refresh_async()
        target = next(
            (
                item for item in report.get("accounts", [])
                if (account.account_id and item.get("account_id") == account.account_id)
                or (not account.account_id and item.get("email", "").lower() == account.email.lower())
            ),
            None,
        )
        return {"account": target or {}, "report": report}

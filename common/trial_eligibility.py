# -*- coding: utf-8 -*-
"""Access Token based Plus/free-trial eligibility probe.

This is the lightweight account check used by the Outlook project:
``GET /backend-api/accounts/check/v4-2023-04-27`` and inspect
``eligible_promo_campaigns.plus.id``.  It deliberately stops at the
eligibility response; it does not create a checkout or payment method.
"""

from __future__ import annotations

import os
import time
import uuid
import threading
import queue
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from common.account_health import (
    CHATGPT_BASE,
    InvalidAccessTokenError,
    _identity_headers,
    _request_identity,
    _session,
)
from common.registration_trace import append_trace_file
from proxy_util import is_proxy_transport_error


PROMO_CAMPAIGN_ID = "plus-1-month-free"
ACCOUNT_CHECK_PATH = "/backend-api/accounts/check/v4-2023-04-27"
NERVER_CHECK_URL = os.environ.get(
    "NERVER_CHECK_URL", "https://cha.nerver.cc/api/v1/check"
)

# Registration must finish and persist the account before the network probe is
# started.  Keep automatic backfill deliberately small: explicit eligibility
# batches use the separate job scheduler below, while list rendering should
# never create a second burst of network and file activity.
_AUTO_CHECK_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="trial-eligibility")
_AUTO_CHECK_LOCK = threading.RLock()
_AUTO_CHECK_PENDING: set[str] = set()
_AUTO_CHECK_RESULT_QUEUE: queue.Queue[tuple[str, dict[str, Any], Any]] = queue.Queue()
_AUTO_CHECK_WRITER_LOCK = threading.Lock()
_AUTO_CHECK_WRITER: threading.Thread | None = None
_AUTO_CHECK_BATCH_SIZE = 16
_AUTO_CHECK_FLUSH_SECONDS = 1.5
_AUTO_CHECK_PROXY_ATTEMPTS = 8

_COUNTRY_TIMEZONE_OFFSETS: dict[str, tuple[int, ...]] = {
    "JP": (-540,),
    "KR": (540,),
    "AU": (-600, -660),
    "US": (300, 360, 420, 480),
    "CA": (240, 300, 360, 420),
    "GB": (0, -60),
    "DE": (-60,),
    "FR": (-60,),
    "SG": (-480,),
    "BR": (180, 240),
}


class TrialEligibilityError(RuntimeError):
    """Eligibility probe failed before a reliable account result was returned."""


def is_trial_probe_retryable(error: BaseException | str | None) -> bool:
    """Return whether another JP route may turn this probe into a real result."""
    if is_proxy_transport_error(error):
        return True
    message = str(error or "").lower()
    return any(
        marker in message
        for marker in (
            "trial_edge_blocked_http_403",
            "account_check_http_403",
            "account_check_http_408",
            "account_check_http_425",
            "account_check_http_429",
            "account_check_http_500",
            "account_check_http_502",
            "account_check_http_503",
            "account_check_http_504",
            "account_check_http_520",
            "account_check_http_521",
            "account_check_http_522",
            "account_check_http_523",
            "account_check_http_524",
            "account_check_invalid_json",
            "account_check_missing_accounts",
        )
    )


def _record_key(record: dict[str, Any]) -> str:
    from common.account_store import account_delete_key

    return account_delete_key(record)


def _status_fields(result: dict[str, Any]) -> dict[str, Any]:
    """Map the probe payload to fields persisted with the account row."""
    fields = {
        "trial_eligibility_status": str(result.get("eligibility_status") or "error"),
        "trial_eligibility_label": str(result.get("label") or "检测失败"),
        "trial_eligibility_checked_at": str(result.get("checked_at") or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")),
        "trial_eligibility_reason": str(result.get("reason") or ""),
        "trial_eligibility_campaign_id": str(result.get("promo_campaign_id") or ""),
        "trial_eligibility_method": "accounts_check_v4",
        "trial_eligibility_proxy_mode": "direct",
        "trial_eligibility_result": str(result.get("status") or ""),
        "trial_eligibility_error": "",
        "trial_eligibility_has_promotion": bool(result.get("has_promotion")),
        "trial_eligibility_promotion_count": int(result.get("promotion_count") or 0),
    }
    if result.get("is_deactivated"):
        fields.update(
            {
                "status": "已封号",
                "health_status": "invalid",
                "health_error": "account_deactivated: eligibility probe confirmed deactivation",
                "reauth_blocked_reason": "account_deactivated",
                "last_invalid_at": fields["trial_eligibility_checked_at"],
            }
        )
    return fields


def _trace_trial_result(record: dict[str, Any], result: dict[str, Any], *, route_mode: str = "") -> None:
    if not isinstance(record, dict):
        return
    append_trace_file(
        record.get("registration_trace_file"),
        {
            "type": "trial_eligibility",
            "email": record.get("email"),
            "account_id": record.get("account_id"),
            "status": result.get("status"),
            "eligibility_status": result.get("eligibility_status"),
            "label": result.get("label"),
            "reason": result.get("reason"),
            "campaign_id": result.get("promo_campaign_id"),
            "promotion_count": result.get("promotion_count"),
            "promotion_keys": result.get("promotion_keys"),
            "proxy_mode": route_mode or result.get("proxy_mode"),
            "checked_at": result.get("checked_at"),
            "elapsed_seconds": result.get("elapsed_seconds"),
        },
    )
    try:
        from common.account_operation_log import record_operation_event

        eligibility_status = str(result.get("eligibility_status") or "").strip().lower()
        record_operation_event(
            record,
            "trial_eligibility",
            "success" if eligibility_status not in {"error", "invalid"} else "failed",
            message=(
                "优惠资格检测完成"
                if eligibility_status not in {"error", "invalid"}
                else "优惠资格检测失败"
            ),
            error=result.get("error") or result.get("reason"),
            metadata={
                "eligibility_status": eligibility_status,
                "campaign_id": result.get("promo_campaign_id"),
                "proxy_mode": route_mode or result.get("proxy_mode"),
            },
        )
    except Exception:
        pass


def _trace_trial_error(
    record: dict[str, Any],
    error: BaseException | str,
    *,
    route_mode: str = "",
    retryable: bool = False,
    status: str = "error",
) -> None:
    if not isinstance(record, dict):
        return
    append_trace_file(
        record.get("registration_trace_file"),
        {
            "type": "trial_eligibility",
            "email": record.get("email"),
            "account_id": record.get("account_id"),
            "status": status,
            "eligibility_status": status,
            "label": "AT 已失效" if status == "invalid" else ("待复测" if retryable else "检测失败"),
            "reason": "token_invalidated" if status == "invalid" else ("route_retry_pending" if retryable else "request_error"),
            "proxy_mode": route_mode,
            "retryable": retryable,
            "error": str(error)[:500],
        },
    )
    try:
        from common.account_operation_log import record_operation_event

        record_operation_event(
            record,
            "trial_eligibility",
            "failed" if status != "invalid" else "invalid",
            message=("AT 已失效" if status == "invalid" else "优惠资格检测失败"),
            error=error,
            metadata={
                "route_mode": route_mode,
                "retryable": bool(retryable),
                "status": status,
            },
        )
    except Exception:
        pass


def _discard_pending(key: str) -> None:
    with _AUTO_CHECK_LOCK:
        _AUTO_CHECK_PENDING.discard(key)


def _eligibility_writer() -> None:
    """Persist automatic probe results in short, bounded batches.

    The account store is JSON-backed for compatibility.  Coalescing results
    here keeps that compatibility while reducing full-file rewrites from one
    per account to one per short time window.
    """
    pending: dict[tuple[str, str], tuple[str, dict[str, Any], Any]] = {}
    deadline = 0.0

    def flush() -> None:
        nonlocal deadline
        if not pending:
            deadline = 0.0
            return
        from common.account_store import update_account_record_fields

        groups: dict[Any, dict[str, dict[str, Any]]] = {}
        for path_key, (key, fields, path) in pending.items():
            groups.setdefault(path, {})[key] = fields
        completed: list[str] = [key for key, _fields, _path in pending.values()]
        try:
            for path, updates in groups.items():
                update_account_record_fields(
                    updates, include_records=False, path=path
                )
        except Exception as error:
            # Leave failed rows available for a later explicit check instead
            # of keeping them permanently marked as pending.
            print(f"[trial-eligibility] batch persist failed: {str(error)[:180]}")
        finally:
            for key in completed:
                _discard_pending(key)
            pending.clear()
            deadline = 0.0

    while True:
        timeout = max(0.05, deadline - time.monotonic()) if deadline else 1.0
        try:
            key, fields, path = _AUTO_CHECK_RESULT_QUEUE.get(timeout=timeout)
            path_key = str(getattr(path, "absolute", lambda: path)())
            pending[(path_key, key)] = (key, fields, path)
            if not deadline:
                deadline = time.monotonic() + _AUTO_CHECK_FLUSH_SECONDS
            if len(pending) >= _AUTO_CHECK_BATCH_SIZE:
                flush()
        except queue.Empty:
            flush()


def _ensure_eligibility_writer() -> None:
    global _AUTO_CHECK_WRITER
    with _AUTO_CHECK_WRITER_LOCK:
        if _AUTO_CHECK_WRITER is not None and _AUTO_CHECK_WRITER.is_alive():
            return
        _AUTO_CHECK_WRITER = threading.Thread(
            target=_eligibility_writer,
            daemon=True,
            name="trial-eligibility-writer",
        )
        _AUTO_CHECK_WRITER.start()


def _queue_result(key: str, fields: dict[str, Any], path: Any) -> None:
    _ensure_eligibility_writer()
    _AUTO_CHECK_RESULT_QUEUE.put((key, fields, path))


def _run_scheduled_check(record: dict[str, Any], path, key: str) -> None:
    from common.trial_proxy_pool import selected_trial_proxies

    queued = False
    country = ""
    route_mode = "direct"
    last_error: Exception | None = None
    try:
        from common.account_operation_log import record_operation_event

        record_operation_event(
            record,
            "trial_eligibility",
            "running",
            message="优惠资格检测开始",
        )
    except Exception:
        pass
    try:
        country, proxies = selected_trial_proxies(limit=_AUTO_CHECK_PROXY_ATTEMPTS)
        attempts = proxies if proxies else [""]
        route_mode = f"{country} proxy" if proxies and country else "direct"
        result: dict[str, Any] | None = None
        for attempt, proxy in enumerate(attempts, 1):
            try:
                result = check_trial_eligibility(record, proxy=proxy, direct=not bool(proxy))
                break
            except InvalidAccessTokenError:
                raise
            except Exception as error:
                last_error = error
                if is_trial_probe_retryable(error) and attempt < len(attempts):
                    time.sleep(0.6 * attempt)
                    continue
                break
        if result is None:
            raise TrialEligibilityError(str(last_error or "account_check_failed"))
        fields = _status_fields(result)
        fields["trial_eligibility_proxy_mode"] = route_mode
        _trace_trial_result(record, result, route_mode=route_mode)
        _queue_result(key, fields, path)
        queued = True
    except InvalidAccessTokenError as error:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _trace_trial_error(record, error, route_mode=route_mode, status="invalid")
        _queue_result(
            key,
            {
                "trial_eligibility_status": "invalid",
                "trial_eligibility_label": "AT 已失效",
                "trial_eligibility_checked_at": now,
                "trial_eligibility_reason": "token_invalidated",
                "trial_eligibility_method": "accounts_check_v4",
                "trial_eligibility_proxy_mode": route_mode,
                "trial_eligibility_error": str(error)[:500],
                "health_status": "invalid",
                "health_error": str(error)[:500],
                "last_invalid_at": now,
            },
            path=path,
        )
        queued = True
    except Exception as error:
        retryable = is_trial_probe_retryable(error) or is_trial_probe_retryable(last_error)
        _trace_trial_error(record, error, route_mode=route_mode, retryable=retryable)
        _queue_result(
            key,
            {
                "trial_eligibility_status": "error",
                "trial_eligibility_label": (
                    f"待复测（{country or '网络'}线路异常）"
                    if retryable
                    else "检测失败"
                ),
                # A route failure is not a completed eligibility result. Keep
                # it eligible for the next explicit/automatic retry.
                "trial_eligibility_checked_at": "" if retryable else datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "trial_eligibility_reason": "route_retry_pending" if retryable else "request_error",
                "trial_eligibility_method": "accounts_check_v4",
                "trial_eligibility_proxy_mode": route_mode,
                "trial_eligibility_error": str(error)[:500],
            },
            path=path,
        )
        queued = True
    finally:
        if not queued:
            _discard_pending(key)


def schedule_trial_eligibility_check(
    record: dict[str, Any],
    *,
    force: bool = False,
    path=None,
) -> bool:
    """Queue a direct eligibility check and mark the row while it runs.

    Returns ``True`` when a new worker was queued.  Existing checks are
    coalesced by account key so refreshing the accounts page never creates a
    request storm.
    """
    from common.account_store import (
        ACCOUNTS_FILE,
        account_is_deactivated,
        update_account_records,
    )

    if (
        not isinstance(record, dict)
        or account_is_deactivated(record)
        or not _free_account(record)
        or not _access_token(record)
    ):
        return False
    path = path or ACCOUNTS_FILE
    key = _record_key(record)
    if not key:
        return False
    with _AUTO_CHECK_LOCK:
        if key in _AUTO_CHECK_PENDING:
            record.update({
                "trial_eligibility_status": "pending",
                "trial_eligibility_label": "检测中",
                "trial_eligibility_proxy_mode": "direct",
            })
            return False
        if not force and str(record.get("trial_eligibility_checked_at") or "").strip():
            return False
        _AUTO_CHECK_PENDING.add(key)
    pending = {
        "trial_eligibility_status": "pending",
        "trial_eligibility_label": "检测中",
        "trial_eligibility_checked_at": "",
        "trial_eligibility_reason": "queued",
        "trial_eligibility_campaign_id": "",
        "trial_eligibility_method": "accounts_check_v4",
        "trial_eligibility_proxy_mode": "direct",
        "trial_eligibility_error": "",
    }
    record.update(pending)
    try:
        update_account_records([key], pending, include_records=False, path=path)
        _AUTO_CHECK_EXECUTOR.submit(_run_scheduled_check, dict(record), path, key)
        return True
    except Exception:
        with _AUTO_CHECK_LOCK:
            _AUTO_CHECK_PENDING.discard(key)
        return False


def schedule_unchecked_trial_checks(records: list[dict[str, Any]], *, force: bool = False, path=None) -> int:
    """Queue unchecked rows with one store write for a whole account batch."""
    from common.account_store import (
        ACCOUNTS_FILE,
        account_is_deactivated,
        update_account_records,
    )

    path = path or ACCOUNTS_FILE
    pending = {
        "trial_eligibility_status": "pending",
        "trial_eligibility_label": "检测中",
        "trial_eligibility_checked_at": "",
        "trial_eligibility_reason": "queued",
        "trial_eligibility_campaign_id": "",
        "trial_eligibility_method": "accounts_check_v4",
        "trial_eligibility_proxy_mode": "direct",
        "trial_eligibility_error": "",
    }
    selected: list[tuple[str, dict[str, Any]]] = []
    with _AUTO_CHECK_LOCK:
        for record in records or []:
            if (
                not isinstance(record, dict)
                or account_is_deactivated(record)
                or not _free_account(record)
                or not _access_token(record)
            ):
                continue
            key = _record_key(record)
            if not key or key in _AUTO_CHECK_PENDING:
                continue
            if not force and str(record.get("trial_eligibility_checked_at") or "").strip():
                continue
            _AUTO_CHECK_PENDING.add(key)
            record.update(pending)
            selected.append((key, dict(record)))
    if not selected:
        return 0
    try:
        update_account_records(
            [key for key, _ in selected],
            pending,
            include_records=False,
            path=path,
        )
        for key, record in selected:
            _AUTO_CHECK_EXECUTOR.submit(_run_scheduled_check, record, path, key)
        return len(selected)
    except Exception:
        with _AUTO_CHECK_LOCK:
            for key, _ in selected:
                _AUTO_CHECK_PENDING.discard(key)
        return 0


def _emit(callback: Callable[[str], None] | None, message: str) -> None:
    if not callable(callback):
        return
    try:
        callback(message)
    except Exception:
        pass


def _access_token(record: dict[str, Any]) -> str:
    token = str(record.get("access_token") or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if token:
        return token
    session = record.get("session")
    if isinstance(session, dict):
        token = str(session.get("accessToken") or session.get("access_token") or "").strip()
    return token[7:].strip() if token.lower().startswith("bearer ") else token


def _proxy_country(proxy: str) -> str:
    match = re.search(r"(?:region-|[_-]g-)([A-Z]{2})(?:[_-]|$)", str(proxy or ""), re.I)
    return match.group(1).upper() if match else ""


def _timezone_offset(record: dict[str, Any], proxy: str) -> int:
    country = _proxy_country(proxy)
    raw = record.get("timezone_offset_min")
    try:
        if raw is not None and str(raw).strip() != "":
            parsed = int(raw)
            # Older protocol rows were persisted with a hard-coded zero even
            # when their selected route was JP/AU/US. Derive a coherent
            # regional offset for those legacy rows during the next probe.
            if parsed != 0 or country in {"", "GB"}:
                return parsed
    except (TypeError, ValueError):
        pass
    choices = _COUNTRY_TIMEZONE_OFFSETS.get(country)
    return int(choices[0]) if choices else 0


def account_access_token(record: dict[str, Any]) -> str:
    """Return the stored access token without its optional Bearer prefix."""
    return _access_token(record)


def _free_account(record: dict[str, Any]) -> bool:
    """Plus accounts never enter the free-trial eligibility queue."""
    return str(record.get("plan_type") or "free").strip().lower() == "free"


def _account_info(payload: dict[str, Any]) -> dict[str, Any]:
    accounts = payload.get("accounts")
    if not isinstance(accounts, dict) or not accounts:
        return {}
    default = accounts.get("default")
    if isinstance(default, dict):
        return default
    first = next(iter(accounts.values()), {})
    return first if isinstance(first, dict) else {}


def _campaign_id(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("id") or value.get("campaign_id") or "").strip()
    if isinstance(value, str):
        return value.strip()
    return ""


def check_trial_eligibility(
    record: dict[str, Any],
    *,
    proxy: str = "",
    direct: bool = True,
    timeout: float = 15,
    line_callback: Callable[[str], None] | None = None,
    stop_event: Any = None,
) -> dict[str, Any]:
    """Query account metadata and return the ``plus-1-month-free`` status.

    ``direct=True`` explicitly creates a session without proxy, even when the
    account record contains an old proxy setting.  The returned payload is
    shaped for the account eligibility UI and contains no credential value.
    """
    token = _access_token(record)
    if not token:
        raise TrialEligibilityError("missing access_token")
    if stop_event is not None and callable(getattr(stop_event, "is_set", None)) and stop_event.is_set():
        raise TrialEligibilityError("任务已停止")

    mode = "direct" if direct else (str(proxy or record.get("proxy") or "").strip() or "direct")
    _emit(line_callback, "校验 ChatGPT Access Token")
    _emit(line_callback, "读取账号 Plus/免费试用资格")

    identity = _request_identity(record)
    session = _session("" if direct else mode, impersonate=identity["impersonate"])
    request_id = str(uuid.uuid4())
    headers = _identity_headers(identity)
    headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Origin": CHATGPT_BASE,
            "Referer": CHATGPT_BASE + "/",
            "Cache-Control": "no-cache",
            "OAI-Session-Id": request_id,
            "OAI-Language": identity["locale"],
        }
    )
    account_id = str(record.get("account_id") or "").strip()
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id
    timezone_offset = _timezone_offset(record, mode)
    started = time.monotonic()
    try:
        response = session.get(
            CHATGPT_BASE + ACCOUNT_CHECK_PATH + f"?timezone_offset_min={timezone_offset}",
            headers=headers,
            timeout=max(3.0, float(timeout)),
        )
        status_code = int(getattr(response, "status_code", 0) or 0)
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if status_code == 401:
            raise InvalidAccessTokenError(
                f"token_invalidated: accounts/check HTTP 401 {(response.text or '')[:240]}"
            )
        body = str(getattr(response, "text", "") or "")
        if status_code == 403 and any(
            marker in body.lower()
            for marker in ("<html", "<!doctype", "cf-ray", "/cdn-cgi/")
        ):
            raise TrialEligibilityError("trial_edge_blocked_http_403")
        if status_code != 200:
            raise TrialEligibilityError(
                f"account_check_http_{status_code}: {body[:300]}"
            )
        if not isinstance(payload, dict):
            raise TrialEligibilityError("account_check_invalid_json")
    finally:
        session.close()

    info = _account_info(payload)
    if not info:
        raise TrialEligibilityError("account_check_missing_accounts")
    account = info.get("account") if isinstance(info.get("account"), dict) else {}
    entitlement = info.get("entitlement") if isinstance(info.get("entitlement"), dict) else {}
    campaigns = info.get("eligible_promo_campaigns")
    campaigns = campaigns if isinstance(campaigns, dict) else {}
    plus_campaign = campaigns.get("plus")
    campaign_id = _campaign_id(plus_campaign)
    plan = str(
        account.get("plan_type")
        or record.get("plan_type")
        or "free"
    ).strip().lower()
    active_subscription = bool(entitlement.get("has_active_subscription"))
    if active_subscription:
        plan = "plus"
    from common.account_store import account_is_deactivated

    deactivated = (
        account_is_deactivated(account)
        or account_is_deactivated(info)
        or account_is_deactivated(payload)
    )

    if deactivated:
        status = "banned"
        label = "账号已停用"
        eligibility_status = "invalid"
        reason = "account_deactivated"
    elif plan == "plus" or active_subscription:
        status = "plus_active"
        label = "Plus 生效中"
        eligibility_status = "active"
        reason = "active_subscription"
    elif campaign_id == PROMO_CAMPAIGN_ID:
        status = "plus_eligible"
        label = "可领 Plus 免费试用"
        eligibility_status = "eligible"
        reason = "eligible_promo_campaign"
    else:
        status = "free"
        label = "无免费试用"
        eligibility_status = "ineligible"
        reason = "no_free_trial"

    checked_epoch = time.time()
    _emit(line_callback, f"资格结果：{label} · campaign={campaign_id or '-'}")
    return {
        "provider": "trial_eligibility",
        "eligible": status == "plus_eligible",
        "eligibility_status": eligibility_status,
        "status": status,
        "label": label,
        "reason": reason,
        "plan_type": plan,
        "has_active_subscription": active_subscription,
        "is_deactivated": deactivated,
        "promo_campaign_id": campaign_id,
        "eligible_promo_campaigns": campaigns,
        "has_promotion": bool(campaigns),
        "promotion_count": len(campaigns),
        "promotion_keys": [str(name) for name, value in campaigns.items() if value],
        "amount": "unknown",
        "currency": "",
        "payment_methods": [],
        "checked_at": datetime.fromtimestamp(checked_epoch, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "checked_epoch": round(checked_epoch, 3),
        "proxy_mode": mode,
        "elapsed_seconds": round(time.monotonic() - started, 1),
    }


def check_trial_eligibility_nerver(
    access_token: Any,
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Check Plus free-trial eligibility through the nerver AC checker API.

    POSTs the access token to ``https://cha.nerver.cc/api/v1/check`` and maps
    the response onto the same result shape as :func:`check_trial_eligibility`.
    Only the ``plus-1-month-free`` campaign counts as eligible; discount
    campaigns are reported as ineligible.
    """
    from curl_cffi import requests as _requests

    token = str(access_token or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise TrialEligibilityError("missing access_token")
    started = time.monotonic()
    checked_epoch = time.time()
    try:
        response = _requests.post(
            NERVER_CHECK_URL,
            json={"token": token},
            timeout=max(5.0, float(timeout)),
        )
    except Exception as error:
        raise TrialEligibilityError(
            f"nerver_check_request_failed: {str(error)[:300]}"
        ) from error
    try:
        payload = response.json()
    except Exception:
        payload = None
    status_code = int(getattr(response, "status_code", 0) or 0)
    if not isinstance(payload, dict):
        raise TrialEligibilityError(
            f"nerver_check_http_{status_code}: invalid json response"
        )
    base = {
        "provider": "nerver_check",
        "plan_type": str(payload.get("plan_type") or "free").strip().lower() or "free",
        "is_deactivated": False,
        "promo_campaign_id": "",
        "checked_at": datetime.fromtimestamp(checked_epoch, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "checked_epoch": round(checked_epoch, 3),
        "proxy_mode": "nerver",
        "elapsed_seconds": round(time.monotonic() - started, 1),
    }
    reason = str(payload.get("reason") or "")
    if not payload.get("token_ok"):
        if reason == "jwt-expired":
            # Token parses but has expired; the account itself may still be
            # alive, so report a dedicated state instead of marking invalid.
            return {
                **base,
                "eligible": False,
                "eligibility_status": "invalid",
                "status": "at_expired",
                "label": "AT 已过期",
                "reason": reason,
                "has_active_subscription": False,
                "jwt_expired": True,
            }
        detail = " ".join(
            part
            for part in (
                reason,
                str(payload.get("message") or ""),
                str(payload.get("error") or ""),
            )
            if part
        )
        raise TrialEligibilityError(
            f"nerver_check_http_{status_code}: {detail[:300] or 'check failed'}"
        )
    eligible = bool(payload.get("eligible"))
    plus_active = base["plan_type"] == "plus"
    if eligible:
        result_status, label, eligibility_status = (
            "plus_eligible",
            "可领 Plus 免费试用",
            "eligible",
        )
        result_reason = reason or "eligible"
        base["promo_campaign_id"] = PROMO_CAMPAIGN_ID
    elif plus_active:
        result_status, label, eligibility_status = (
            "plus_active",
            "Plus 生效中",
            "active",
        )
        result_reason = "active_subscription"
    else:
        result_status, label, eligibility_status = ("free", "无免费试用", "ineligible")
        result_reason = reason or "not-eligible"
    return {
        **base,
        "eligible": eligible,
        "eligibility_status": eligibility_status,
        "status": result_status,
        "label": label,
        "reason": result_reason,
        "has_active_subscription": plus_active,
        "jwt_expired": False,
    }

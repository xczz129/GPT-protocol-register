# -*- coding: utf-8 -*-
"""ChatGPT web account token refresh and remote health checks."""

from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from curl_cffi import requests

from common.account_store import (
    ACCOUNTS_FILE,
    account_deactivation_reason,
    account_delete_key,
    account_is_deactivated,
    load_account_records_by_keys,
    load_accounts,
    normalized_health_status,
    update_account_record_fields,
)
from common.account_operation_log import record_operation_event
from common.team_workspace import requires_team_workspace, session_identity, validate_session_replacement
from proxy_util import is_proxy_transport_error, normalize_proxy_url

AUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
OAUTH_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"
CHATGPT_BASE = "https://chatgpt.com"
DEFAULT_REFRESH_SKEW_SECONDS = 5 * 60
_SUPPORTED_BROWSER_PROFILES = frozenset(
    {
        "chrome131", "chrome136", "chrome142", "chrome145", "chrome146",
        "firefox133", "firefox135", "firefox144", "firefox147",
    }
)
_DEFAULT_FIREFOX_PROFILE = "firefox147"
_DEFAULT_JAPAN_LOCALE = "ja-JP"
_DEFAULT_JAPAN_LANGUAGE = "ja-JP,ja;q=0.9,en-US;q=0.7,en;q=0.6"


# 测活路径的 token 刷新快速失败参数：慢代理不应把单账号测活拖过 1 分钟，
# 完整刷新交给「刷新AT/重新登录」批量任务。
_PROBE_REFRESH_TIMEOUT = 8.0
_PROBE_REFRESH_ATTEMPTS = 1


class _HealthWorkerGate:
    """Coordinate optional caller-provided limits without imposing a default cap."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active = 0

    def run(
        self,
        callback: Callable[[], dict[str, Any]],
        *,
        requested: int,
        limit_provider: Callable[[int], int],
    ) -> dict[str, Any]:
        with self._condition:
            while self._active >= max(1, int(limit_provider(requested))):
                self._condition.wait(timeout=0.25)
            self._active += 1
        try:
            return callback()
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()


_HEALTH_WORKER_GATE = _HealthWorkerGate()


class InvalidAccessTokenError(RuntimeError):
    pass


class AccountDeactivatedError(RuntimeError):
    """The account API confirmed that the account is deactivated."""


class HealthTransientError(RuntimeError):
    """The network or upstream failed before a credential verdict was known."""


class HealthEdgeBlockedError(HealthTransientError):
    """The edge rejected this network path without evaluating the token."""


def _deactivation_reason(value: Any, *, prefix: str = "") -> str:
    """Inspect bounded account-shaped API data for explicit terminal markers."""
    if not isinstance(value, dict):
        return ""

    direct = account_deactivation_reason(value)
    if direct:
        return f"{prefix}{direct}" if prefix else direct

    for key in ("error", "detail"):
        nested = value.get(key)
        if isinstance(nested, dict):
            reason = account_deactivation_reason(nested)
            if reason:
                return f"{prefix}{key}.{reason}"

    account = value.get("account")
    if isinstance(account, dict):
        reason = account_deactivation_reason(account)
        if reason:
            return f"{prefix}account.{reason}"

    accounts = value.get("accounts")
    if isinstance(accounts, dict):
        ordered = []
        default = accounts.get("default")
        if isinstance(default, dict):
            ordered.append(("default", default))
        ordered.extend(
            (str(name), item)
            for name, item in accounts.items()
            if name != "default" and isinstance(item, dict)
        )
        for name, item in ordered:
            reason = _deactivation_reason(item, prefix=f"accounts.{name}.")
            if reason:
                return f"{prefix}{reason}"
    return ""


def _raise_if_deactivated(value: Any, *, source: str) -> None:
    reason = _deactivation_reason(value)
    if reason:
        raise AccountDeactivatedError(
            f"account_deactivated: confirmed by {source} ({reason[:240]})"
        )


def _is_transient_health_probe_error(error: BaseException) -> bool:
    message = str(error or "").lower()
    return isinstance(error, HealthTransientError) or any(
        marker in message
        for marker in (
            "health_http_408",
            "health_http_425",
            "health_http_429",
            "health_http_500",
            "health_http_502",
            "health_http_503",
            "health_http_504",
        )
    )


def _is_transient_tls_connect_error(error: BaseException) -> bool:
    message = str(error or "").lower()
    return "curl: (35)" in message and "tls connect error" in message


def _is_health_transport_error(error: BaseException) -> bool:
    return _is_transient_tls_connect_error(error) or is_proxy_transport_error(error)


def _normalized_proxy_candidates(values: Iterable[str] | None) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        proxy = normalize_proxy_url(str(raw or ""))
        if proxy and proxy not in seen:
            seen.add(proxy)
            candidates.append(proxy)
    return candidates


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_health_probe_channel(value: Any) -> str:
    """Normalize legacy client metadata; health no longer selects a trial API."""
    return "nerver" if str(value or "").strip().lower() == "nerver" else "v4"


def _jwt_payload(token: str) -> dict[str, Any]:
    try:
        segment = str(token).split(".")[1]
        segment += "=" * (-len(segment) % 4)
        data = json.loads(base64.urlsafe_b64decode(segment.encode("ascii")))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _needs_refresh(
    token: str,
    skew_seconds: int = DEFAULT_REFRESH_SKEW_SECONDS,
) -> bool:
    try:
        expires_at = int(_jwt_payload(token).get("exp") or 0)
        return expires_at > 0 and expires_at <= int(time.time()) + max(
            int(skew_seconds), 0
        )
    except (TypeError, ValueError):
        return False


def _token_expired(token: str) -> bool:
    try:
        expires_at = int(_jwt_payload(token).get("exp") or 0)
        return expires_at > 0 and expires_at <= int(time.time())
    except (TypeError, ValueError):
        return False


def _session(proxy: str = "", *, impersonate: str = _DEFAULT_FIREFOX_PROFILE):
    kwargs: dict[str, Any] = {"impersonate": impersonate, "verify": False}
    normalized = normalize_proxy_url(proxy)
    if normalized:
        kwargs["proxies"] = {"http": normalized, "https": normalized}
    return requests.Session(**kwargs)


def _chrome_user_agent(major: str) -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )


def _firefox_user_agent(major: str) -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        f"rv:{major}.0 Gecko/20100101 Firefox/{major}.0"
    )


def _request_identity(record: dict[str, Any]) -> dict[str, str]:
    """Build one coherent identity for health and OAuth refresh requests."""
    browser_type = str(record.get("browser_type") or "").strip().lower()
    impersonate = str(record.get("tls_impersonate") or "").strip().lower()
    if not impersonate and browser_type.startswith("curl_cffi/"):
        impersonate = browser_type.split("/", 1)[1]
    profile_supported = impersonate in _SUPPORTED_BROWSER_PROFILES
    if not profile_supported:
        impersonate = _DEFAULT_FIREFOX_PROFILE
    major = (
        impersonate.removeprefix("chrome").removeprefix("firefox")
        or "147"
    )
    default_user_agent = (
        _chrome_user_agent(major)
        if impersonate.startswith("chrome")
        else _firefox_user_agent(major)
    )
    user_agent = str(record.get("user_agent") or "").strip()
    if not user_agent or not profile_supported:
        user_agent = default_user_agent
    locale = str(record.get("locale") or "").strip()
    accept_language = str(record.get("accept_language") or "").strip()
    if not locale:
        locale = (accept_language.split(",", 1)[0] if accept_language else _DEFAULT_JAPAN_LOCALE)
    if not accept_language:
        accept_language = (
            _DEFAULT_JAPAN_LANGUAGE
            if locale == _DEFAULT_JAPAN_LOCALE
            else f"{locale},{locale.split('-', 1)[0]};q=0.9"
        )
    device_id = str(record.get("device_id") or "").strip()
    if not device_id:
        stable_key = str(record.get("email") or record.get("account_id") or "anonymous").strip().lower()
        device_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"gpt-register-fp:{stable_key}"))
    sec_ch_ua = str(record.get("sec_ch_ua") or "").strip()
    if not sec_ch_ua and impersonate.startswith("chrome"):
        sec_ch_ua = (
            f'"Chromium";v="{major}", "Google Chrome";v="{major}", '
            '"Not_A Brand";v="99"'
        )
    platform_version = str(record.get("platform_version") or "10.0.0").strip()
    timezone_name = str(record.get("timezone") or "").strip()
    try:
        timezone_offset = int(record.get("timezone_offset_min"))
    except (TypeError, ValueError):
        timezone_offset = -540 if locale == _DEFAULT_JAPAN_LOCALE else 0
    if locale == _DEFAULT_JAPAN_LOCALE:
        timezone_name = "Asia/Tokyo"
        if timezone_offset == 0:
            timezone_offset = -540
    return {
        "impersonate": impersonate,
        "major": major,
        "user_agent": user_agent,
        "sec_ch_ua": sec_ch_ua,
        "platform_version": platform_version,
        "accept_language": accept_language,
        "locale": locale,
        "timezone": timezone_name or ("Asia/Tokyo" if locale == _DEFAULT_JAPAN_LOCALE else "UTC"),
        "timezone_offset_min": str(timezone_offset),
        "device_id": device_id,
    }


def _identity_headers(identity: dict[str, str]) -> dict[str, str]:
    headers = {
        "User-Agent": identity["user_agent"],
        "Accept-Language": identity["accept_language"],
        "OAI-Device-Id": identity["device_id"],
    }
    if identity["sec_ch_ua"]:
        headers.update(
            {
                "Sec-CH-UA": identity["sec_ch_ua"],
                "Sec-CH-UA-Mobile": "?0",
                "Sec-CH-UA-Platform": '"Windows"',
                "Sec-CH-UA-Platform-Version": f'"{identity["platform_version"]}"',
            }
        )
    return headers


def refresh_oauth_tokens(
    record: dict[str, Any], *, force: bool = False, timeout: float = 60.0
) -> dict[str, Any]:
    access_token = str(record.get("access_token") or "").strip()
    refresh_token = str(record.get("refresh_token") or "").strip()
    if not refresh_token or (access_token and not force and not _needs_refresh(access_token)):
        return dict(record)

    identity = _request_identity(record)
    session = _session(
        str(record.get("proxy") or ""), impersonate=identity["impersonate"]
    )
    try:
        headers = _identity_headers(identity)
        headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            }
        )
        response = session.post(
            AUTH_TOKEN_URL,
            headers=headers,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": str(record.get("oauth_client_id") or "").strip() or OAUTH_CLIENT_ID,
            },
            timeout=timeout,
        )
        try:
            data = response.json()
        except Exception:
            data = {}
        if response.status_code != 200 or not isinstance(data, dict) or not data.get("access_token"):
            detail = str(
                (data or {}).get("error_description")
                or (data or {}).get("error")
                or (data or {}).get("message")
                or response.text[:300]
            )
            error = f"oauth_refresh_http_{response.status_code}: {detail}"
            if response.status_code == 401:
                raise InvalidAccessTokenError(error)
            raise RuntimeError(error)
        fresh = {
            "access_token": str(data.get("access_token") or access_token).strip(),
            "refresh_token": str(data.get("refresh_token") or refresh_token).strip(),
            "id_token": str(data.get("id_token") or record.get("id_token") or "").strip(),
            "last_token_refresh_at": _now(),
            "last_token_refresh_error": "",
        }
        validate_session_replacement(record, fresh, require_identity=requires_team_workspace(record))
        return {**record, **fresh}
    finally:
        session.close()


def _proxy_clearance_config() -> dict[str, Any] | None:
    """Read the shared proxy/clearance settings used by the protocol engine."""
    try:
        from config_loader import load_config

        value = load_config().get("proxy_config")
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _protocol_session_request(
    session: requests.Session,
    url: str,
    headers: dict[str, str],
    proxy: str,
    *,
    attempts: int = 3,
    timeout: float = 30.0,
) -> requests.Response:
    """Protocol-channel GET: CF clearance injection plus bounded retry.

    Mirrors register.protocol.request_with_local_retry semantics for the
    session endpoint: transport errors bubble up (the probe loop switches
    proxies), throttling/gateway statuses retry with backoff, and clearance
    cookies/UA are injected per attempt when a bundle is cached.
    """
    from common.proxy_clearance import headers_with_clearance

    proxy_config = _proxy_clearance_config()
    retryable = {408, 425, 429, 500, 502, 503, 504}
    attempts = max(1, attempts)
    last_error = ""
    for attempt in range(attempts):
        request_headers = headers_with_clearance(
            proxy_config, headers, url, request_proxy=proxy
        )
        try:
            response = session.get(url, headers=request_headers, timeout=timeout)
        except Exception as exc:
            message = str(exc)
            if is_proxy_transport_error(message):
                raise
            last_error = message
            if attempt + 1 >= attempts:
                raise
            time.sleep(min(4.0, 0.5 * (2 ** attempt)))
            continue
        status = int(getattr(response, "status_code", 0) or 0)
        if status in retryable and attempt + 1 < attempts:
            retry_after = 0.0
            raw = str(getattr(response, "headers", {}).get("retry-after") or "").strip()
            if raw.isdigit():
                retry_after = float(raw)
            time.sleep(min(8.0, max(0.25, retry_after or (0.5 * (2 ** attempt)))))
            continue
        return response
    raise RuntimeError(f"session_refresh_no_response: {last_error}")


def refresh_chatgpt_session_tokens(
    record: dict[str, Any],
    *,
    force: bool = False,
    timeout: float = 30.0,
    attempts: int = 3,
) -> dict[str, Any]:
    """Refresh the access token through the protocol channel.

    Uses the stored NextAuth session cookie to call /api/auth/session with
    the same clearance/retry machinery as the registration protocol engine.
    """
    access_token = str(record.get("access_token") or "").strip()
    session_token = str(record.get("session_token") or "").strip()
    if not session_token or (
        access_token and not force and not _needs_refresh(access_token)
    ):
        return dict(record)

    proxy = str(record.get("proxy") or "")
    identity = _request_identity(record)
    session = _session(proxy, impersonate=identity["impersonate"])
    try:
        session.cookies.set(
            "__Secure-next-auth.session-token",
            session_token,
            domain=".chatgpt.com",
            path="/",
        )
        device_id = str(
            record.get("device_id") or record.get("protocol_final_device_id") or ""
        ).strip()
        if device_id:
            session.cookies.set("oai-did", device_id, domain=".chatgpt.com", path="/")
        headers = _identity_headers(identity)
        headers.update(
            {
                "Accept": "application/json",
                "Referer": CHATGPT_BASE + "/",
                "Cache-Control": "no-cache",
            }
        )
        session_url = f"{CHATGPT_BASE}/api/auth/session?update={int(time.time() * 1000)}"
        response = _protocol_session_request(
            session, session_url, headers, proxy,
            attempts=attempts, timeout=timeout,
        )
        try:
            data = response.json()
        except Exception:
            data = None
        status_code = int(getattr(response, "status_code", 0) or 0)
        body = str(getattr(response, "text", "") or "")
        if status_code in {408, 425, 429} or 500 <= status_code < 600:
            raise HealthTransientError(
                f"nextauth_session_http_{status_code}: temporary upstream response"
            )
        if status_code == 403 and any(
            marker in body.lower()
            for marker in ("<html", "<!doctype", "cf-ray", "/cdn-cgi/")
        ):
            raise HealthEdgeBlockedError("nextauth_session_edge_blocked_http_403")
        if status_code == 200 and not isinstance(data, dict):
            raise HealthTransientError("nextauth_session_invalid_json")
        data = data if isinstance(data, dict) else {}
        fresh_access = str(data.get("accessToken") or "").strip()
        if status_code != 200 or not fresh_access:
            detail = str(
                data.get("error")
                or data.get("message")
                or body[:300]
                or ("session ended" if status_code == 200 else "empty response")
            )
            error = f"nextauth_session_http_{status_code}: {detail}"
            rejected_session = any(
                marker in detail.lower()
                for marker in ("session ended", "session has ended", "session expired", "session has expired", "invalid session")
            )
            if status_code == 401 or (status_code == 200 and not data) or rejected_session:
                raise InvalidAccessTokenError(error)
            if status_code == 200:
                raise HealthTransientError("nextauth_session_missing_access_token")
            # A generic non-success response is not proof that the stored
            # session is invalid. Keep it recoverable instead of marking the
            # account invalid or rejecting an otherwise valid import.
            raise RuntimeError(error)
        auth = _jwt_payload(fresh_access).get("https://api.openai.com/auth")
        auth = auth if isinstance(auth, dict) else {}
        account = data.get("account") if isinstance(data.get("account"), dict) else {}
        account_id = str(
            account.get("id")
            or auth.get("chatgpt_account_id")
            or ""
        ).strip()
        plan_type = str(
            account.get("planType")
            or account.get("plan_type")
            or auth.get("chatgpt_plan_type")
            or ""
        ).strip()
        user_id = str(
            auth.get("chatgpt_user_id")
            or auth.get("user_id")
            or ""
        ).strip()
        fresh = {
            "access_token": fresh_access,
            "account_id": account_id,
            "user_id": user_id,
            "plan_type": plan_type,
            "session": data,
            "last_token_refresh_at": _now(),
            "last_token_refresh_error": "",
        }
        validate_session_replacement(record, fresh, require_identity=requires_team_workspace(record))
        return {
            **record, **fresh,
            "account_id": account_id or record.get("account_id") or "",
            "user_id": user_id or record.get("user_id") or "",
            "plan_type": plan_type or record.get("plan_type") or "free",
        }
    finally:
        session.close()


def _response_json(response, path: str) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:
        data = None
    if response.status_code == 401:
        _raise_if_deactivated(data, source=f"HTTP 401 {path}")
        raise InvalidAccessTokenError(f"token invalidated ({path})")
    if response.status_code != 200:
        detail = (response.text or "")[:400]
        lowered = detail.lower()
        if response.status_code == 403 and any(
            marker in lowered
            for marker in ("<html", "<!doctype", "cf-ray", "/cdn-cgi/")
        ):
            raise HealthEdgeBlockedError(f"health_edge_blocked_http_403 {path}")
        _raise_if_deactivated(data, source=f"HTTP {response.status_code} {path}")
        raise RuntimeError(f"health_http_{response.status_code} {path}: {detail}")
    if not isinstance(data, dict):
        raise RuntimeError(f"health_invalid_json {path}")
    _raise_if_deactivated(data, source=path)
    return data


def fetch_remote_account_info(
    record: dict[str, Any], channel: str = "v4",
) -> dict[str, Any]:
    """Check account/session availability through the official me/init APIs.

    ``channel`` is retained for older clients and checkpoints. Both legacy
    values use the same health checks; plan and trial checks are separate jobs.
    """
    token = str(record.get("access_token") or "").strip()
    if not token:
        raise InvalidAccessTokenError("missing access_token")

    probe_channel = normalize_health_probe_channel(channel)
    identity = _request_identity(record)
    device_id = identity["device_id"]
    session_id = str(uuid.uuid4())
    locale = identity["locale"]
    timezone_offset = int(identity["timezone_offset_min"])
    session = _session(
        str(record.get("proxy") or ""), impersonate=identity["impersonate"]
    )
    identity_headers = _identity_headers(identity)
    identity_headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Origin": CHATGPT_BASE,
            "Referer": CHATGPT_BASE + "/",
            "Accept": "application/json",
            "OAI-Session-Id": session_id,
            "OAI-Language": locale,
        }
    )
    session.headers.update(identity_headers)
    me_path = "/backend-api/me"
    init_path = "/backend-api/conversation/init"
    try:
        requests_to_run = [
            (
                "me",
                lambda: _response_json(
                    session.get(CHATGPT_BASE + me_path, timeout=8),
                    me_path,
                ),
            ),
            (
                "init",
                lambda: _response_json(
                    session.post(
                        CHATGPT_BASE + init_path,
                        json={
                            "gizmo_id": None,
                            "requested_default_model": None,
                            "conversation_id": None,
                            "timezone_offset_min": timezone_offset,
                        },
                        timeout=8,
                    ),
                    init_path,
                ),
            ),
        ]
        payloads: dict[str, dict[str, Any]] = {}
        errors: dict[str, BaseException] = {}
        # 测活只并行请求 me/init；资格与套餐探测不参与可用性判断。
        # curl_cffi Session 默认 use_thread_local_curl=True，工作线程各持
        # 独立 Curl 句柄，同一 Session 并发请求是安全的。
        with ThreadPoolExecutor(max_workers=len(requests_to_run)) as probe_executor:
            probe_futures = {
                probe_executor.submit(request): name
                for name, request in requests_to_run
            }
            for future, name in probe_futures.items():
                try:
                    payloads[name] = future.result()
                except (KeyboardInterrupt, SystemExit):
                    raise
                except BaseException as exc:
                    errors[name] = exc
        for name, payload in payloads.items():
            _raise_if_deactivated(payload, source=name)
        if errors:
            invalid_token = next(
                (
                    exc
                    for exc in errors.values()
                    if isinstance(exc, InvalidAccessTokenError)
                ),
                None,
            )
            raise invalid_token or next(iter(errors.values()))
        me = payloads["me"]
        init = payloads["init"]
    finally:
        session.close()

    limits = init.get("limits_progress")
    limits = limits if isinstance(limits, list) else []
    image_limit = next(
        (item for item in limits if isinstance(item, dict) and item.get("feature_name") == "image_gen"),
        None,
    )
    raw_quota = (image_limit or {}).get("remaining")
    quota_unknown = raw_quota is None or isinstance(raw_quota, bool)
    try:
        quota = 0 if quota_unknown else max(0, int(raw_quota))
    except (TypeError, ValueError, OverflowError):
        quota, quota_unknown = 0, True
    status = "限流" if not quota_unknown and quota == 0 else "正常"
    return {
        # Availability probes do not change the selected workspace or plan.
        **{field: record[field] for field in ("account_id", "plan_type", "type") if field in record},
        "email": me.get("email") or record.get("email"),
        "user_id": me.get("id") or record.get("user_id"),
        "quota": quota,
        "image_quota_unknown": quota_unknown,
        "limits_progress": limits,
        "default_model_slug": init.get("default_model_slug"),
        "restore_at": (image_limit or {}).get("reset_after"),
        "status": status,
        "health_status": "limited" if status == "限流" else "alive",
        "health_checked_at": _now(),
        "health_error": "",
        "health_probe_channel": probe_channel,
        "health_probe_error": "",
        "device_id": device_id,
    }


_HEALTH_OWNED_FIELDS = frozenset({
    "access_token", "refresh_token", "id_token", "session_token", "session",
    "account_id", "workspace_account_id", "user_id", "email", "plan_type",
    "last_token_refresh_at", "last_token_refresh_error", "last_invalid_at",
    "status", "health_status", "health_checked_at", "health_error",
    "health_probe_error", "health_probe_proxy_mode", "health_probe_channel",
    "reauth_blocked_reason",
    "type", "quota", "image_quota_unknown", "limits_progress",
    "default_model_slug", "restore_at", "device_id",
})
_HEALTH_EXPECTED_FIELDS = (
    "access_token", "refresh_token", "session_token", "user_id", "email",
    "account_id", "password", "totp_secret", "import_account_type",
    "selected_workspace_id", "workspace_selection_verified",
)


def _persist_health_results(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]], path: Path,
) -> list[dict[str, Any]]:
    """Write only probe-owned changes, conditional on the credentials inspected.

    Read back the actual rows for callbacks: a concurrent credential replacement
    can invalidate a probe while it is in flight, and deletion must stay deleted.
    """
    updates = {}
    expected = {}
    lookup_keys = set()
    for original, checked in pairs:
        key = account_delete_key(original)
        updates[key] = {
            field: checked[field] for field in _HEALTH_OWNED_FIELDS
            if field in checked and checked[field] != original.get(field)
        }
        expected[key] = {field: original.get(field) for field in _HEALTH_EXPECTED_FIELDS}
        # The store backfills this field from JWT claims on insertion. A newly
        # registered caller may still hold the pre-normalization dictionary.
        if not expected[key]["user_id"]:
            claims = _jwt_payload(str(original.get("access_token") or "")).get("https://api.openai.com/auth")
            if isinstance(claims, dict):
                known_user = str(claims.get("chatgpt_user_id") or claims.get("user_id") or "").strip()
                if known_user:
                    expected[key]["user_id"] = known_user
        lookup_keys.update((key, account_delete_key(checked)))
    update_account_record_fields(
        updates, expected_fields=expected, include_records=False, path=path,
    )
    stored = load_account_records_by_keys(lookup_keys, path)
    results = []
    for original, checked in pairs:
        key = account_delete_key(original)
        current = stored.get(key) or stored.get(account_delete_key(checked))
        if current is None:
            results.append({
                **{field: original[field] for field in ("email", "user_id", "account_id") if field in original},
                "health_status": "skipped",
                "health_error": "账号已删除或身份已更新，测活结果未写入",
                "health_write_skipped": True,
            })
            continue
        current = dict(current)
        if any(current.get(field) != value for field, value in updates[key].items()):
            current["health_write_skipped"] = True
            current["health_write_skip_reason"] = "账号在测活期间发生变化，已保留最新记录"
        results.append(current)
    return results


def _validate_health_metadata(
    original: dict[str, Any], current: dict[str, Any], remote: dict[str, Any],
) -> None:
    """Check probe identity before token claims can mask conflicting /me data."""
    previous = session_identity(original)
    expected_user = str(original.get("user_id") or previous["user_id"] or "").strip().casefold()
    expected_email = str(original.get("email") or previous["email"] or "").strip().casefold()
    remote_user = str(remote.get("user_id") or "").strip().casefold()
    remote_email = str(remote.get("email") or "").strip().casefold()
    if expected_user and remote_user and remote_user != expected_user:
        raise RuntimeError("session_identity_user_mismatch: 测活返回其他用户，保留原账号")
    if expected_email and remote_email and remote_email != expected_email:
        raise RuntimeError("session_identity_email_mismatch: 测活邮箱与原账号不一致，保留原账号")
    if requires_team_workspace(original):
        expected_workspace = str(original.get("selected_workspace_id") or previous["account_id"] or "").strip().casefold()
        remote_workspace = str(remote.get("account_id") or "").strip().casefold()
        if expected_workspace and remote_workspace and remote_workspace != expected_workspace:
            raise RuntimeError("team_workspace_id_mismatch: 测活返回其他工作区，保留原团队会话")
    validate_session_replacement(
        original, {**current, **remote}, require_identity=requires_team_workspace(original),
    )


def check_account(
    record: dict[str, Any],
    *,
    persist: bool = True,
    path: Path = ACCOUNTS_FILE,
    proxy_candidates: Iterable[str] | None = None,
    channel: str = "v4",
) -> dict[str, Any]:
    current = dict(record)
    probe_channel = normalize_health_probe_channel(channel)
    operation_log_path = Path(path or ACCOUNTS_FILE).with_name(
        "account_operation_logs.sqlite3"
    )
    record_operation_event(
        current,
        "health_check",
        "running",
        message="账号测活开始",
        path=operation_log_path,
    )
    current.pop("plan_type_override", None)
    current["health_probe_channel"] = probe_channel
    stored_proxy = str(current.get("proxy") or "").strip()
    configured_proxies = _normalized_proxy_candidates(proxy_candidates)
    if configured_proxies:
        seed = sum(str(current.get("email") or current.get("account_id") or "").encode("utf-8"))
        offset = seed % len(configured_proxies)
        probe_proxies = configured_proxies[offset:] + configured_proxies[:offset]
        probe_proxy_mode = "current_registration_pool"
        max_attempts = min(3, len(probe_proxies))
    else:
        probe_proxies = [stored_proxy, stored_proxy] if stored_proxy else ["", ""]
        probe_proxy_mode = "account_proxy" if stored_proxy else "direct"
        max_attempts = 2
    probe_proxy = probe_proxies[0]
    last_transport_error: BaseException | None = None

    def apply_refreshed(refreshed: dict[str, Any]) -> None:
        # The saved registration proxy is historical metadata. Network calls
        # use the current pool without replacing that field in the account.
        refreshed = dict(refreshed)
        validate_session_replacement(
            record, refreshed, require_identity=requires_team_workspace(record),
        )
        refreshed.pop("proxy", None)
        # Workspace sessions flip account_id between the member id and the
        # shared workspace id. A health-check refresh must never rewrite the
        # account identity — merging multiple members into one shared
        # workspace id previously collapsed whole account batches.
        new_user = str(refreshed.get("user_id") or "").strip().lower()
        old_user = str(current.get("user_id") or "").strip().lower()
        if new_user and old_user and new_user != old_user:
            raise InvalidAccessTokenError(
                "session_user_mismatch: 刷新返回了其他用户的会话，已拒绝写入"
            )
        new_account_id = str(refreshed.get("account_id") or "").strip()
        old_account_id = str(current.get("account_id") or "").strip()
        if (
            new_account_id
            and old_account_id
            and new_account_id.lower() != old_account_id.lower()
        ):
            refreshed.pop("account_id", None)
            refreshed.pop("email", None)
            refreshed["workspace_account_id"] = new_account_id
        current.update(refreshed)

    def refresh_for_probe(*, force: bool = False) -> None:
        source = {**current, "proxy": probe_proxy}
        # 快速失败：测活只保证拿到可用 AT 探测，慢刷新交给批量刷新任务。
        refreshed = (
            refresh_chatgpt_session_tokens(
                source,
                force=force,
                timeout=_PROBE_REFRESH_TIMEOUT,
                attempts=_PROBE_REFRESH_ATTEMPTS,
            )
            if current.get("session_token")
            else refresh_oauth_tokens(source, force=force, timeout=_PROBE_REFRESH_TIMEOUT)
        )
        apply_refreshed(refreshed)

    def mark_invalid_transition() -> None:
        if normalized_health_status(current) != "invalid":
            current["last_invalid_at"] = _now()

    if account_is_deactivated(current):
        mark_invalid_transition()
        current.update(
            status="已封号",
            health_status="invalid",
            health_checked_at=_now(),
            health_error="account_deactivated: account has been deleted or deactivated",
            updated_at=_now(),
        )
        if persist:
            current = _persist_health_results([(record, current)], path)[0]
        record_operation_event(
            current,
            "health_check",
            "failed",
            message="账号已封号，测活跳过",
            error=current.get("health_error"),
            path=operation_log_path,
        )
        return current
    try:
        for attempt in range(max_attempts):
            probe_proxy = probe_proxies[attempt]
            try:
                refresh_for_probe()
                try:
                    probe_record = {**current, "proxy": probe_proxy}
                    remote = (
                        fetch_remote_account_info(
                            probe_record, channel=probe_channel,
                        )
                        if probe_channel == "nerver"
                        else fetch_remote_account_info(probe_record)
                    )
                except InvalidAccessTokenError:
                    session_error: InvalidAccessTokenError | None = None
                    if current.get("session_token"):
                        try:
                            refreshed = refresh_chatgpt_session_tokens(
                                {**current, "proxy": probe_proxy},
                                force=True,
                                timeout=_PROBE_REFRESH_TIMEOUT,
                                attempts=_PROBE_REFRESH_ATTEMPTS,
                            )
                            apply_refreshed(refreshed)
                        except InvalidAccessTokenError as exc:
                            session_error = exc
                    if session_error is not None or not current.get("session_token"):
                        if not current.get("refresh_token"):
                            raise session_error or InvalidAccessTokenError(
                                "missing renewable credential"
                            )
                        refreshed = refresh_oauth_tokens(
                            {**current, "proxy": probe_proxy},
                            force=True,
                            timeout=_PROBE_REFRESH_TIMEOUT,
                        )
                        apply_refreshed(refreshed)
                    probe_record = {**current, "proxy": probe_proxy}
                    remote = (
                        fetch_remote_account_info(
                            probe_record, channel=probe_channel,
                        )
                        if probe_channel == "nerver"
                        else fetch_remote_account_info(probe_record)
                    )
                _validate_health_metadata(record, current, remote)
                remote["health_probe_proxy_mode"] = probe_proxy_mode
                remote["health_probe_channel"] = probe_channel
                remote.setdefault("health_probe_error", "")
                current.update(remote)
                break
            except Exception as exc:
                if _is_health_transport_error(exc):
                    last_transport_error = exc
                    if attempt + 1 < max_attempts:
                        # Imported OAuth records usually have no historical
                        # registration proxy. Give the next probe a real
                        # fallback route instead of retrying the same direct
                        # socket and marking a healthy token as failed.
                        if not configured_proxies and attempt == 0:
                            try:
                                from common.trial_proxy_pool import selected_trial_proxies

                                country, fallback = selected_trial_proxies(
                                    limit=2,
                                    exclude={stored_proxy} if stored_proxy else set(),
                                )
                            except Exception:
                                country, fallback = "", []
                            if fallback:
                                probe_proxies[attempt + 1] = fallback[0]
                                probe_proxy_mode = f"{country or 'fallback'}_pool"
                        time.sleep(0.25)
                        continue
                    raise
                if attempt == 0 and _is_transient_health_probe_error(exc):
                    if not configured_proxies:
                        try:
                            from common.trial_proxy_pool import selected_trial_proxies

                            country, proxies = selected_trial_proxies(
                                limit=2,
                                exclude={stored_proxy} if stored_proxy else set(),
                            )
                        except Exception:
                            country, proxies = "", []
                        if proxies:
                            probe_proxies[attempt + 1] = proxies[0]
                            probe_proxy_mode = f"{country or 'fallback'}_pool"
                    delay_seed = sum(
                        str(current.get("email") or "").encode("utf-8")
                    ) % 700
                    time.sleep(0.35 + delay_seed / 1000)
                    continue
                raise
    except AccountDeactivatedError as exc:
        mark_invalid_transition()
        current.update(
            status="已封号",
            health_status="invalid",
            health_checked_at=_now(),
            health_error=str(exc)[:400],
            reauth_blocked_reason="account_deactivated",
        )
    except InvalidAccessTokenError as exc:
        state = (
            "reauth_required"
            if _token_expired(str(current.get("access_token") or ""))
            else "invalid"
        )
        if state == "invalid":
            mark_invalid_transition()
        current.update(
            status="异常",
            health_status=state,
            health_checked_at=_now(),
            health_error=str(exc)[:400],
        )
    except Exception as exc:
        if last_transport_error is not None and _is_health_transport_error(exc):
            previous = str(record.get("health_status") or "").strip().lower()
            preserved = previous if previous in {"alive", "limited"} else "unknown"
            current.update(
                status=(record.get("status") if preserved != "unknown" else "待复测"),
                health_status=preserved,
                health_checked_at=_now(),
                health_error="",
                health_probe_error=str(exc)[:400],
                health_probe_proxy_mode=probe_proxy_mode,
                health_probe_channel=probe_channel,
            )
        else:
            current.update(
                status="异常",
                health_status="error",
                health_checked_at=_now(),
                health_error=str(exc)[:400],
                health_probe_channel=probe_channel,
            )
    current["updated_at"] = _now()
    final_health = str(current.get("health_status") or "error").strip().lower()
    record_operation_event(
        current,
        "health_check",
        "success" if final_health in {"alive", "limited"} else "failed",
        message=(
            "账号测活成功"
            if final_health in {"alive", "limited"}
            else "账号测活失败"
        ),
        error=current.get("health_error") or current.get("health_probe_error"),
        metadata={
            "health_status": final_health,
            "health_probe_channel": probe_channel,
        },
        path=operation_log_path,
    )
    if persist:
        current = _persist_health_results([(record, current)], path)[0]
    return current


def select_health_check_records(
    records: Iterable[dict[str, Any]],
    *,
    keys: Iterable[str] | None = None,
    skip_known_deactivated: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split selected account rows into probe targets and terminal skips.

    The split is kept in one helper so the API's progress denominator and the
    worker's actual input cannot drift apart when a caller requests all rows.
    """
    items = [item for item in records if isinstance(item, dict)]
    wanted = {str(key or "").strip() for key in (keys or []) if str(key or "").strip()}
    if wanted:
        items = [item for item in items if account_delete_key(item) in wanted]
    if not skip_known_deactivated:
        return items, []
    pending: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in items:
        (skipped if account_is_deactivated(item) else pending).append(item)
    return pending, skipped


def check_accounts(
    records: Iterable[dict[str, Any]] | None = None,
    *,
    keys: Iterable[str] | None = None,
    workers: int = 1,
    path: Path = ACCOUNTS_FILE,
    progress_callback=None,
    item_callback=None,
    completion_callback=None,
    worker_limit: Callable[[int], int] | None = None,
    proxy_candidates: Iterable[str] | None = None,
    skip_known_deactivated: bool = True,
    channel: str = "v4",
) -> dict[str, Any]:
    probe_channel = normalize_health_probe_channel(channel)
    source_items = list(records if records is not None else load_accounts(path))
    operation_log_path = Path(path or ACCOUNTS_FILE).with_name(
        "account_operation_logs.sqlite3"
    )
    items, skipped_items = select_health_check_records(
        source_items,
        keys=keys,
        skip_known_deactivated=skip_known_deactivated,
    )
    requested = len(items) + len(skipped_items)
    actual_total = len(items)
    results: list[dict[str, Any]] = []
    pending: list[tuple[dict[str, Any], dict[str, Any]]] = []
    completed = 0
    # A single SQLite transaction per worker completion is expensive for a
    # multi-thousand-account run. Keep progress durable in modest chunks while
    # avoiding thousands of full transaction/JSON cycles.
    batch_size = max(1, min(25, len(items) or 1))

    def persist_pending() -> None:
        if not pending:
            return
        batch = list(pending)
        pending.clear()
        for persisted in _persist_health_results(batch, path):
            results.append(persisted)
            if callable(item_callback):
                try:
                    item_callback(len(results), len(items), persisted)
                except Exception:
                    pass
            if callable(progress_callback):
                try:
                    progress_callback(len(results), len(items), persisted)
                except Exception:
                    pass

    requested_workers = max(1, min(int(workers or 1), len(items) or 1))
    executor_workers = requested_workers
    if callable(worker_limit):
        try:
            executor_workers = max(
                1,
                min(requested_workers, int(worker_limit(requested_workers))),
            )
        except Exception:
            executor_workers = requested_workers

    def run_one(item: dict[str, Any]) -> dict[str, Any]:
        check_kwargs = {
            "persist": False,
            "path": path,
            "proxy_candidates": proxy_candidates,
        }
        if probe_channel == "nerver":
            check_kwargs["channel"] = probe_channel
        if not callable(worker_limit):
            return check_account(item, **check_kwargs)
        return _HEALTH_WORKER_GATE.run(
            lambda: check_account(item, **check_kwargs),
            requested=requested_workers,
            limit_provider=worker_limit,
        )

    # Keep only a small number of futures queued.  Submitting every account at
    # once retains the complete payload of thousands of accounts while a
    # registration-priority gate may be allowing only one health probe.
    item_iterator = iter(items)
    with ThreadPoolExecutor(max_workers=executor_workers) as executor:
        in_flight: dict[Any, dict[str, Any]] = {}

        def submit_next() -> bool:
            try:
                item = next(item_iterator)
            except StopIteration:
                return False
            in_flight[executor.submit(run_one, item)] = item
            return True

        for _ in range(min(len(items), max(1, executor_workers * 2))):
            submit_next()

        while in_flight:
            completed_futures, _ = wait(
                tuple(in_flight), return_when=FIRST_COMPLETED
            )
            for future in completed_futures:
                original = in_flight.pop(future)
                checked = future.result()
                completed += 1
                pending.append((original, checked))
                # ``completion_callback`` is deliberately invoked before the
                # persistence batch is flushed.  It is for live progress only;
                # callers that need a persisted row should keep using
                # ``item_callback``/``progress_callback`` below.  Separating
                # these callbacks prevents the UI from jumping once per
                # 25-row SQLite transaction while retaining batched writes.
                if callable(completion_callback):
                    try:
                        completion_callback(completed, len(items), checked)
                    except Exception:
                        pass
                if len(pending) >= batch_size:
                    persist_pending()
                submit_next()
    persist_pending()
    for skipped in skipped_items:
        record_operation_event(
            skipped,
            "health_check",
            "skipped",
            message="账号已封号，测活跳过",
            error=skipped.get("health_error"),
            path=operation_log_path,
        )
    counts = {"alive": 0, "limited": 0, "invalid": 0, "reauth_required": 0, "error": 0}
    for item in results:
        state = str(item.get("health_status") or "error")
        counts[state if state in counts else "error"] += 1
    usable = counts["alive"] + counts["limited"]
    detected_deactivated = sum(1 for item in results if account_is_deactivated(item))
    deactivated = len(skipped_items) + detected_deactivated
    invalid_other = max(0, counts["invalid"] - detected_deactivated)
    return {
        "channel": probe_channel,
        "requested": requested,
        "actual_total": actual_total,
        "checked": len(results),
        "usable": usable,
        "unavailable": (
            len(skipped_items)
            + counts["invalid"]
            + counts["reauth_required"]
            + counts["error"]
        ),
        "skipped_known_deactivated": len(skipped_items),
        "deactivated": deactivated,
        "invalid_other": invalid_other,
        **counts,
        "items": results,
        "skipped_items": skipped_items,
    }

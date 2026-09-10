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
    load_accounts,
    normalized_health_status,
    upsert_account_record,
    upsert_account_records,
)
from common.account_operation_log import record_operation_event
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


class HealthEdgeBlockedError(RuntimeError):
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
    return isinstance(error, HealthEdgeBlockedError) or any(
        marker in message
        for marker in (
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
        return {
            **record,
            "access_token": str(data.get("access_token") or access_token).strip(),
            "refresh_token": str(data.get("refresh_token") or refresh_token).strip(),
            "id_token": str(data.get("id_token") or record.get("id_token") or "").strip(),
            "last_token_refresh_at": _now(),
            "last_token_refresh_error": "",
        }
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
            data = {}
        fresh_access = str((data or {}).get("accessToken") or "").strip()
        if response.status_code != 200 or not fresh_access:
            detail = str(
                (data or {}).get("error")
                or (data or {}).get("message")
                or response.text[:300]
                or "session ended"
            )
            raise InvalidAccessTokenError(
                f"nextauth_session_http_{response.status_code}: {detail}"
            )
        auth = _jwt_payload(fresh_access).get("https://api.openai.com/auth")
        auth = auth if isinstance(auth, dict) else {}
        account = data.get("account") if isinstance(data.get("account"), dict) else {}
        account_id = str(
            account.get("id")
            or auth.get("chatgpt_account_id")
            or record.get("account_id")
            or ""
        ).strip()
        plan_type = str(
            account.get("planType")
            or account.get("plan_type")
            or auth.get("chatgpt_plan_type")
            or record.get("plan_type")
            or "free"
        ).strip()
        user_id = str(
            auth.get("chatgpt_user_id")
            or auth.get("user_id")
            or record.get("user_id")
            or ""
        ).strip()
        return {
            **record,
            "access_token": fresh_access,
            "account_id": account_id,
            "user_id": user_id,
            "plan_type": plan_type,
            "session": data,
            "last_token_refresh_at": _now(),
            "last_token_refresh_error": "",
        }
    finally:
        session.close()


def _response_json(response, path: str) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:
        data = {}
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


def fetch_remote_account_info(record: dict[str, Any]) -> dict[str, Any]:
    token = str(record.get("access_token") or "").strip()
    if not token:
        raise InvalidAccessTokenError("missing access_token")

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
    account_path = "/backend-api/accounts/check/v4-2023-04-27"
    try:
        requests_to_run = (
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
                (
                    "accounts",
                    lambda: _response_json(
                        session.get(
                            CHATGPT_BASE
                            + account_path
                            + f"?timezone_offset_min={timezone_offset}",
                            timeout=8,
                        ),
                        account_path,
                    ),
                ),
            )
        payloads: dict[str, dict[str, Any]] = {}
        errors: list[BaseException] = []
        # 并行发送 me/init/accounts 三个请求（对齐 chatgpt2api v3.2.2
        # get_user_info 的并行探测）：单账号测活耗时由 3 个串行 RTT 降为
        # 1 个并行 RTT；错误判定保持"多请求错误中优先采信 401"。
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
                    errors.append(exc)
        for name, payload in payloads.items():
            _raise_if_deactivated(payload, source=name)
        accounts = payloads.get("accounts") or {}
        account_entries = accounts.get("accounts") or {}
        account_entries = account_entries if isinstance(account_entries, dict) else {}
        # 多工作区账号(Team): AT 的 chatgpt_account_id 指向本次会话实际
        # 登录的工作区, 优先取它的套餐/账号ID; 否则退回 default(个人)工作区,
        # 避免 Team 账号被 default 个人工作区覆盖成 free。
        token_account_id = ""
        try:
            auth_claims = _jwt_payload(token).get("https://api.openai.com/auth")
            token_account_id = str(
                (auth_claims if isinstance(auth_claims, dict) else {}).get(
                    "chatgpt_account_id"
                )
                or ""
            ).strip()
        except Exception:
            token_account_id = ""
        selected = (
            account_entries.get(token_account_id)
            if token_account_id and isinstance(
                account_entries.get(token_account_id), dict
            )
            else None
        )
        default = selected or account_entries.get("default") or next(
            (item for item in account_entries.values() if isinstance(item, dict)),
            {},
        )
        account = default.get("account") if isinstance(default, dict) else {}
        account = account if isinstance(account, dict) else {}
        if errors:
            invalid_token = next(
                (exc for exc in errors if isinstance(exc, InvalidAccessTokenError)),
                None,
            )
            raise invalid_token or errors[0]
        me = payloads["me"]
        init = payloads["init"]
        accounts = payloads["accounts"]
    finally:
        session.close()

    limits = init.get("limits_progress")
    limits = limits if isinstance(limits, list) else []
    image_limit = next(
        (item for item in limits if isinstance(item, dict) and item.get("feature_name") == "image_gen"),
        None,
    )
    plan = str(
        account.get("plan_type")
        or record.get("plan_type")
        or "free"
    )
    quota_unknown = image_limit is None
    quota = int((image_limit or {}).get("remaining") or 0)
    status = "正常" if quota_unknown and plan.lower() != "free" else ("限流" if quota == 0 else "正常")
    return {
        "email": me.get("email") or record.get("email"),
        "user_id": me.get("id") or record.get("user_id"),
        "account_id": account.get("account_id") or record.get("account_id"),
        "plan_type": plan,
        "type": plan,
        "quota": quota,
        "image_quota_unknown": quota_unknown,
        "limits_progress": limits,
        "default_model_slug": init.get("default_model_slug"),
        "restore_at": (image_limit or {}).get("reset_after"),
        "status": status,
        "health_status": "limited" if status == "限流" else "alive",
        "health_checked_at": _now(),
        "health_error": "",
        "device_id": device_id,
    }


def check_account(
    record: dict[str, Any],
    *,
    persist: bool = True,
    path: Path = ACCOUNTS_FILE,
    proxy_candidates: Iterable[str] | None = None,
) -> dict[str, Any]:
    current = dict(record)
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
            upsert_account_record(current, path)
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
                    remote = fetch_remote_account_info({**current, "proxy": probe_proxy})
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
                    remote = fetch_remote_account_info({**current, "proxy": probe_proxy})
                remote["health_probe_proxy_mode"] = probe_proxy_mode
                remote["health_probe_error"] = ""
                current.update(remote)
                break
            except Exception as exc:
                if _is_health_transport_error(exc):
                    last_transport_error = exc
                    if attempt + 1 < max_attempts:
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
            )
        else:
            current.update(
                status="异常",
                health_status="error",
                health_checked_at=_now(),
                health_error=str(exc)[:400],
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
        metadata={"health_status": final_health},
        path=operation_log_path,
    )
    if persist:
        upsert_account_record(current, path)
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
    worker_limit: Callable[[int], int] | None = None,
    proxy_candidates: Iterable[str] | None = None,
    skip_known_deactivated: bool = True,
) -> dict[str, Any]:
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
    pending: list[dict[str, Any]] = []
    # A single SQLite transaction per worker completion is expensive for a
    # multi-thousand-account run. Keep progress durable in modest chunks while
    # avoiding thousands of full transaction/JSON cycles.
    batch_size = max(1, min(25, len(items) or 1))

    def persist_pending() -> None:
        if not pending:
            return
        batch = list(pending)
        pending.clear()
        stored = upsert_account_records(batch, path, create=False)
        by_key = {account_delete_key(item): item for item in stored}
        by_account_id = {
            str(item.get("account_id") or "").strip().lower(): item
            for item in stored
            if str(item.get("account_id") or "").strip()
        }
        by_email = {
            str(item.get("email") or "").strip().lower(): item
            for item in stored
            if str(item.get("email") or "").strip()
        }
        for checked in batch:
            persisted = by_key.get(account_delete_key(checked))
            if persisted is None:
                account_id = str(checked.get("account_id") or "").strip().lower()
                email = str(checked.get("email") or "").strip().lower()
                persisted = by_account_id.get(account_id) or by_email.get(email) or checked
            results.append(persisted)
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
        if not callable(worker_limit):
            return check_account(
                item,
                persist=False,
                path=path,
                proxy_candidates=proxy_candidates,
            )
        return _HEALTH_WORKER_GATE.run(
            lambda: check_account(
                item,
                persist=False,
                path=path,
                proxy_candidates=proxy_candidates,
            ),
            requested=requested_workers,
            limit_provider=worker_limit,
        )

    completed = 0
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
                in_flight.pop(future, None)
                checked = future.result()
                pending.append(checked)
                completed += 1
                if callable(item_callback):
                    try:
                        item_callback(completed, len(items), checked)
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

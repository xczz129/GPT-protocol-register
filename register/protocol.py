from __future__ import annotations

import base64
import asyncio
import contextvars
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import string
import threading
import time
import uuid
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo

from curl_cffi import requests

from common.account_health import check_account
from common.account_store import ACCOUNTS_FILE, upsert_account_record
from common.country_identity import COUNTRY_IDENTITIES, country_code_from_proxy, country_identity
from common.proxy_clearance import clearance_error, headers_with_clearance, refresh_clearance
from common.session_export import build_account_record, parse_jwt_payload
from common.registration_trace import RegistrationTrace
from common.totp import totp_code
from register.sessions import JobCancelled
from mail import create_mailbox as allocate_mailbox
from mail import (
    mark_mailbox_code_boundary,
    mark_mailbox_result,
    release_mailbox,
    update_mailbox_states,
    wait_for_code as read_mail_code,
)
from mail.parsing import (
    CANONICAL_PROVIDER_TYPES,
    normalize_provider_type,
    parse_domain_mailboxes,
    parse_icloud_credentials,
    parse_outlook_credentials,
)
from proxy_util import (
    is_proxy_transport_error,
    normalize_proxy_url,
    proxy_brief,
    resolve_proxy_pool,
)
from register.protocol_pkce import generate_pkce as _generate_pkce
from register.protocol_sentinel import (
    build_sentinel_token as _build_sentinel_token_tuple,
    build_sentinel_with_so_token,
)
from register.protocol_runtime import (
    ProtocolRuntimeSettings,
    activate_runtime,
    current_runtime_settings,
)
from register.scheduling import build_registration_waves
from register.util import _append_account

config = {
    "mail": {
        "request_timeout": 30,
        "wait_timeout": 90,
        "wait_interval": 2,
        "api_use_register_proxy": True,
        "providers": [],
    },
    "proxy": "",
    "total": 10,
    "threads": 3,
    "proxy_config": {},
}

_TASK_EVENT: contextvars.ContextVar[Callable[..., Any] | None] = contextvars.ContextVar(
    "protocol_task_event", default=None
)
_STOP_CHECK: contextvars.ContextVar[Callable[[], bool]] = contextvars.ContextVar(
    "protocol_stop_check", default=lambda: False
)
_FIXED_EMAIL: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "protocol_fixed_email", default=None
)
_PROVIDER_TYPE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "protocol_provider_type", default=None
)
_MAIL_CONFIG: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "protocol_mail_config", default=None
)
_PROXY_CONFIG: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "protocol_proxy_config", default=None
)
_REGISTRATION_TRACE: contextvars.ContextVar[RegistrationTrace | None] = contextvars.ContextVar(
    "protocol_registration_trace", default=None
)

auth_base = "https://auth.openai.com"
chatgpt_base = "https://chatgpt.com"
platform_base = "https://platform.openai.com"
platform_oauth_client_id = "app_2SKx67EdpoN0G6j64rFvigXD"
chatgpt_oauth_client_id = "app_X8zY6vW2pQ9tR3dE7nK1jL5gH"
platform_oauth_redirect_uri = f"{platform_base}/auth/callback"
platform_oauth_audience = "https://api.openai.com/v1"
platform_auth0_client = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"
REGISTER_BROWSER_PROFILES: tuple[dict[str, str], ...] = (
    {
        "impersonate": "firefox147",
        "major": "147",
        "full_version": "147.0",
        "weight": "5",
    },
    {
        "impersonate": "firefox144",
        "major": "144",
        "full_version": "144.0",
        "weight": "4",
    },
    {
        "impersonate": "firefox135",
        "major": "135",
        "full_version": "135.0",
        "weight": "3",
    },
    {
        "impersonate": "firefox133",
        "major": "133",
        "full_version": "133.0",
        "weight": "1",
    },
)


def is_dead_slot_error(detail: str) -> bool:
    """Slot-quality failures that retire the mailbox as used.

    These never heal by retrying, so they must not consume the transient
    retry budget: the task should keep switching to other slots instead.
    """
    text = str(detail or "").lower()
    return any(
        marker in text
        for marker in (
            "invalid_auth_step",
            "user_already_exists",
            "username_already_exists",
            "account_deactivated",
        )
    )



def _validate_browser_profiles(
    profiles: tuple[dict[str, str], ...] = REGISTER_BROWSER_PROFILES,
) -> None:
    """Fail early when curl_cffi cannot impersonate a configured profile."""
    try:
        from curl_cffi.requests.impersonate import BrowserType
    except Exception as exc:  # pragma: no cover - environment-specific import failure
        raise RuntimeError("curl_cffi impersonation profiles are unavailable") from exc
    supported = {str(item.value) for item in BrowserType}
    missing = sorted(
        {
            str(profile.get("impersonate") or "").strip()
            for profile in profiles
            if str(profile.get("impersonate") or "").strip() not in supported
        }
    )
    if missing:
        raise RuntimeError(
            "Unsupported curl_cffi impersonation profile(s): " + ", ".join(missing)
        )


_validate_browser_profiles()

REGISTER_DEVICE_TRAITS: tuple[dict[str, str], ...] = (
    {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "10.0.0"},
    {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    {"screen": "1600x900", "hardware_concurrency": "8", "device_memory": "16", "device_pixel_ratio": "1.2", "platform_version": "10.0.0"},
    {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "16", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    {"screen": "1920x1200", "hardware_concurrency": "12", "device_memory": "16", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    {"screen": "1280x720", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1.5", "platform_version": "10.0.0"},
    {"screen": "1440x900", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    {"screen": "1680x1050", "hardware_concurrency": "8", "device_memory": "16", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    {"screen": "2560x1600", "hardware_concurrency": "16", "device_memory": "16", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
)

REGISTER_JAPAN_WEBGL_TRAITS: tuple[dict[str, str], ...] = (
    {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
)

REGISTER_US_WEBGL_TRAITS: tuple[dict[str, str], ...] = (
    {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
)

REGISTER_COUNTRY_DEVICE_TRAITS: dict[str, tuple[dict[str, str], ...]] = {
    "PH": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "10.0.0"},
        {"screen": "1600x900", "hardware_concurrency": "8", "device_memory": "16", "device_pixel_ratio": "1.2", "platform_version": "10.0.0"},
    ),
    "TH": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1600x900", "hardware_concurrency": "8", "device_memory": "16", "device_pixel_ratio": "1.2", "platform_version": "10.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "12", "device_memory": "16", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
    "TR": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1440x900", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "12", "device_memory": "16", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
    "VN": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "10.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "16", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
    "KR": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1600x900", "hardware_concurrency": "8", "device_memory": "16", "device_pixel_ratio": "1.2", "platform_version": "10.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "12", "device_memory": "16", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "16", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
}

REGISTER_COUNTRY_WEBGL_TRAITS: dict[str, tuple[dict[str, str], ...]] = {
    "PH": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 560 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "TH": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "TR": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "VN": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "KR": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 2060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
}

REGISTER_LANGUAGE_PROFILES: tuple[str, ...] = (
    "en-US,en;q=0.9",
    "en-US,en;q=0.9",
    "en-US,en;q=0.9",
    "en-GB,en;q=0.9,en-US;q=0.8",
    "en-US,en;q=0.8",
    "en-GB,en;q=0.9",
)

COUNTRY_TIMEZONE_OFFSETS: dict[str, tuple[int, ...]] = {
    country: identity.timezone_offsets
    for country, identity in COUNTRY_IDENTITIES.items()
}


def _firefox_user_agent(major: str, full_version: str) -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        f"rv:{full_version} Gecko/20100101 Firefox/{full_version}"
    )


def _firefox_sec_ch_ua(major: str) -> str:
    # Firefox does not send Chromium client-hint headers.  Keep this empty so
    # callers can remove legacy sec-ch-* fields from request headers.
    return ""


def _firefox_sec_ch_ua_full_version_list(major: str, full_version: str) -> str:
    return ""


def _complete_browser_fingerprint(profile: dict[str, str]) -> dict[str, str]:
    major = str(profile.get("major") or "147").strip()
    full_version = str(profile.get("full_version") or f"{major}.0.0.0").strip()
    return {
        **profile,
        "major": major,
        "full_version": full_version,
        "user_agent": str(profile.get("user_agent") or _firefox_user_agent(major, full_version)),
        "sec_ch_ua": str(profile.get("sec_ch_ua") or _firefox_sec_ch_ua(major)),
        "sec_ch_ua_full_version_list": str(
            profile.get("sec_ch_ua_full_version_list") or _firefox_sec_ch_ua_full_version_list(major, full_version)
        ),
        "accept_language": str(profile.get("accept_language") or "en-US,en;q=0.9"),
        "platform_version": str(profile.get("platform_version") or "10.0.0"),
        "impersonate": str(profile.get("impersonate") or "firefox147"),
    }


DEFAULT_BROWSER_FINGERPRINT = _complete_browser_fingerprint(REGISTER_BROWSER_PROFILES[0])
user_agent = DEFAULT_BROWSER_FINGERPRINT["user_agent"]
sec_ch_ua = DEFAULT_BROWSER_FINGERPRINT["sec_ch_ua"]
sec_ch_ua_full_version_list = DEFAULT_BROWSER_FINGERPRINT["sec_ch_ua_full_version_list"]
print_lock = threading.Lock()
stats_lock = threading.Lock()
_PASSWORD_AUTH_FLOW_GATE = threading.BoundedSemaphore(value=3)
stats = {"done": 0, "success": 0, "fail": 0, "skipped": 0, "start_time": 0.0}
register_log_sink = None


class PasswordResetRequired(RuntimeError):
    """Signal that a registration candidate already has a password.

    Registration must not rotate or reset credentials belonging to an existing
    account.  The batch scheduler handles this as a terminal skip so the
    mailbox is not retried through another proxy.
    """

    code = "password_reset_required"

    def __init__(self, detail: str = "", *, email: str = "") -> None:
        self.email = str(email or "").strip()
        suffix = f": {detail.strip()[:180]}" if str(detail or "").strip() else ""
        super().__init__(f"{self.code}{suffix}")

common_headers = {
    "accept": "application/json",
    "accept-encoding": "gzip, deflate, br",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "no-cache",
    "connection": "keep-alive",
    "content-type": "application/json",
    "dnt": "1",
    "origin": auth_base,
    "priority": "u=1, i",
    "sec-gpc": "1",
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": user_agent,
}

navigate_headers = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-encoding": "gzip, deflate, br",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "max-age=0",
    "connection": "keep-alive",
    "dnt": "1",
    "sec-gpc": "1",
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": user_agent,
}


def _browser_fingerprint(fingerprint: dict[str, str] | None = None) -> dict[str, str]:
    return _complete_browser_fingerprint(fingerprint or DEFAULT_BROWSER_FINGERPRINT)


def _header_fingerprint(headers: dict[str, str], fingerprint: dict[str, str] | None = None) -> dict[str, str]:
    fp = _browser_fingerprint(fingerprint)
    next_headers = dict(headers)
    next_headers["user-agent"] = fp["user_agent"]
    if fp["sec_ch_ua"]:
        next_headers["sec-ch-ua"] = fp["sec_ch_ua"]
    else:
        for key in tuple(next_headers):
            if key.lower().startswith("sec-ch-"):
                next_headers.pop(key, None)
    if "sec-ch-ua-full-version-list" in next_headers:
        if fp["sec_ch_ua_full_version_list"]:
            next_headers["sec-ch-ua-full-version-list"] = fp["sec_ch_ua_full_version_list"]
        else:
            next_headers.pop("sec-ch-ua-full-version-list", None)
    if "sec-ch-ua-platform-version" in next_headers:
        next_headers["sec-ch-ua-platform-version"] = f'"{fp["platform_version"]}"'
    if "accept-language" in next_headers:
        next_headers["accept-language"] = fp["accept_language"]
    return next_headers


def _extract_firefox_version_from_user_agent(value: str) -> tuple[str, str]:
    ua = str(value or "")
    for marker in ("Firefox/",):
        if marker not in ua:
            continue
        tail = ua.split(marker, 1)[1]
        version = tail.split(" ", 1)[0].strip()
        major = version.split(".", 1)[0].strip()
        if major.isdigit():
            return major, version or f"{major}.0.0.0"
    return "", ""


def _fingerprint_with_user_agent(fingerprint: dict[str, str] | None, value: str) -> dict[str, str]:
    ua = str(value or "").strip()
    if not ua:
        return _browser_fingerprint(fingerprint)
    fp = _browser_fingerprint(fingerprint)
    major, full_version = _extract_firefox_version_from_user_agent(ua)
    impersonate_by_major = {
        str(item.get("major") or ""): str(item.get("impersonate") or "")
        for item in REGISTER_BROWSER_PROFILES
    }
    # Clearance endpoints occasionally report a Chromium UA.  Keep the
    # protocol session's Firefox identity instead of mixing TLS and HTTP
    # fingerprints, and reject Firefox versions absent from curl_cffi.
    if not major or major not in impersonate_by_major:
        return fp
    full_version = full_version or f"{major}.0"
    return {
        **fp,
        "major": major,
        "full_version": full_version,
        "impersonate": impersonate_by_major.get(major) or fp["impersonate"],
        "user_agent": ua,
        "sec_ch_ua": _firefox_sec_ch_ua(major),
        "sec_ch_ua_full_version_list": _firefox_sec_ch_ua_full_version_list(major, full_version),
    }


def _client_hint_headers(fp: dict[str, str]) -> dict[str, str]:
    """Return Chromium client hints only when the selected profile uses them."""
    if not fp.get("sec_ch_ua"):
        return {}
    return {
        "sec-ch-ua": fp["sec_ch_ua"],
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }


def _proxy_country_code(proxy: str = "") -> str:
    # A country embedded in proxy credentials describes the actual pool and
    # takes precedence over the panel selection exported by config_loader.
    return (
        country_code_from_proxy(proxy)
        or str(os.environ.get("REGISTER_PROXY_COUNTRY") or "").strip().upper()
        or "JP"
    )


def _language_profiles_for_proxy(proxy: str = "") -> tuple[str, ...]:
    identity = country_identity(_proxy_country_code(proxy))
    if identity:
        return (identity.accept_language,)
    return REGISTER_LANGUAGE_PROFILES


def _country_fingerprint_traits(proxy: str = "") -> dict[str, str]:
    """Return country-coherent browser traits for the selected egress route."""
    country = _proxy_country_code(proxy)
    if country == "JP":
        webgl = secrets.choice(REGISTER_JAPAN_WEBGL_TRAITS)
        return {
            "locale": "ja-JP",
            "timezone": "Asia/Tokyo",
            "timezone_offset_min": "-540",
            "platform": "Win32",
            "navigator_vendor": "",
            "max_touch_points": "0",
            "webgl_vendor": webgl["vendor"],
            "webgl_renderer": webgl["renderer"],
        }
    if country == "US":
        webgl = secrets.choice(REGISTER_US_WEBGL_TRAITS)
        return {
            "locale": "en-US",
            "timezone": "America/New_York",
            "timezone_offset_min": str(
                _timezone_offset_for_timezone("America/New_York", fallback=300)
            ),
            "platform": "Win32",
            "navigator_vendor": "",
            "max_touch_points": "0",
            "webgl_vendor": webgl["vendor"],
            "webgl_renderer": webgl["renderer"],
        }
    if country in REGISTER_COUNTRY_WEBGL_TRAITS:
        identity = country_identity(country)
        webgl = secrets.choice(REGISTER_COUNTRY_WEBGL_TRAITS[country])
        device = secrets.choice(REGISTER_COUNTRY_DEVICE_TRAITS[country])
        return {
            **device,
            "locale": identity.locale if identity else "en-US",
            "timezone": identity.timezone if identity else "UTC",
            "timezone_offset_min": str(
                _timezone_offset_for_timezone(
                    identity.timezone if identity else "UTC",
                    fallback=(identity.timezone_offsets[0] if identity else 0),
                )
            ),
            "platform": "Win32",
            "navigator_vendor": "",
            "max_touch_points": "0",
            "webgl_vendor": webgl["vendor"],
            "webgl_renderer": webgl["renderer"],
        }
    identity = country_identity(country)
    return {
        "locale": identity.locale if identity else "en-US",
        "timezone": identity.timezone if identity else "UTC",
        "timezone_offset_min": str(_timezone_offset_for_proxy(proxy)),
        "platform": "Win32",
        "navigator_vendor": "",
        "max_touch_points": "0",
        "webgl_vendor": "Intel Inc.",
        "webgl_renderer": "ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
    }


def _timezone_offset_for_timezone(timezone_name: str, *, fallback: int = 0) -> int:
    """Return JavaScript-style timezone offset minutes for a concrete zone."""
    try:
        offset = datetime.now(ZoneInfo(str(timezone_name))).utcoffset()
    except Exception:
        offset = None
    if offset is None:
        return int(fallback)
    return -int(offset.total_seconds() / 60)


def _timezone_offset_for_proxy(proxy: str = "") -> int:
    country = _proxy_country_code(proxy)
    choices = COUNTRY_TIMEZONE_OFFSETS.get(country)
    if not choices:
        return 0
    return int(secrets.choice(choices))


def _weighted_fingerprint_profiles() -> list[dict[str, str]]:
    profiles: list[dict[str, str]] = []
    for profile in REGISTER_BROWSER_PROFILES:
        weight = max(1, int(profile.get("weight") or 1))
        profiles.extend([dict(profile) for _ in range(weight)])
    return profiles or [dict(DEFAULT_BROWSER_FINGERPRINT)]


def _fingerprint_pool_key(proxy: str = "") -> str:
    country = _proxy_country_code(proxy) or "LOCAL"
    return f"register-fingerprint:{country}"


def _claim_fingerprint_combo(proxy: str = "") -> dict[str, str] | None:
    """Return an under-used browser/profile tuple for the current process.

    Random pure sampling can accidentally repeat the same high-frequency tuple
    for a short batch.  This small per-process shuffled pool keeps variation
    high without changing the public concurrency model or touching backend
    queues.
    """
    state = getattr(_claim_fingerprint_combo, "_state", None)
    if not isinstance(state, dict):
        state = {}
        setattr(_claim_fingerprint_combo, "_state", state)
    key = _fingerprint_pool_key(proxy)
    pool = state.get(key)
    if not pool:
        combos: list[dict[str, str]] = []
        for profile in _weighted_fingerprint_profiles():
            for trait in REGISTER_DEVICE_TRAITS:
                for language in _language_profiles_for_proxy(proxy):
                    combos.append({**profile, **trait, "accept_language": language})
        random.SystemRandom().shuffle(combos)
        state[key] = combos
        pool = combos
    try:
        return dict(pool.pop())
    except Exception:
        return None


def _make_browser_fingerprint(proxy: str = "") -> dict[str, str]:
    profile = _claim_fingerprint_combo(proxy)
    if not profile:
        chooser = random.SystemRandom()
        selected = chooser.choices(
            REGISTER_BROWSER_PROFILES,
            weights=[max(1, int(item.get("weight") or 1)) for item in REGISTER_BROWSER_PROFILES],
            k=1,
        )[0]
        traits = secrets.choice(REGISTER_DEVICE_TRAITS)
        profile = {
            **selected,
            **traits,
            "accept_language": secrets.choice(_language_profiles_for_proxy(proxy)),
        }
    country_traits = _country_fingerprint_traits(proxy)
    return _complete_browser_fingerprint(
        {
            **profile,
            **country_traits,
            "profile_id": secrets.token_hex(6),
            "sentinel_sid": str(uuid.uuid4()),
        }
    )


def log(text: str, color: str = "") -> None:
    colors = {"red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m"}
    if register_log_sink:
        try:
            register_log_sink(text, color)
        except Exception:
            pass
    with print_lock:
        trace = _REGISTRATION_TRACE.get()
        if trace is not None:
            try:
                trace.log_line(text, color)
            except Exception:
                pass
        prefix = colors.get(color, "")
        suffix = "\033[0m" if prefix else ""
        print(f"{prefix}{time.strftime('%H:%M:%S')} {text}{suffix}")


def step(index: int, text: str, color: str = "") -> None:
    log(f"[任务{index}] {text}", color)
    event = _TASK_EVENT.get()
    if callable(event):
        try:
            event(index, "running", step=text)
        except Exception:
            pass
    else:
        # Registration tasks install ``traced_task_event`` as ``_TASK_EVENT``;
        # that wrapper records the trace event before forwarding the live
        # callback.  Only retain the direct path for low-level callers that
        # use a trace without installing a task callback, avoiding duplicate
        # JSONL writes on the normal registration hot path.
        trace = _REGISTRATION_TRACE.get()
        if trace is not None:
            try:
                trace.event("running", step=text)
            except Exception:
                pass


def _make_trace_headers() -> dict[str, str]:
    trace_id = str(random.getrandbits(64))
    parent_id = str(random.getrandbits(64))
    return {
        "traceparent": f"00-{uuid.uuid4().hex}-{format(int(parent_id), '016x')}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


def _random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    value = list(
        secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + secrets.choice("!@#$%")
        + "".join(secrets.choice(chars) for _ in range(max(0, length - 4)))
    )
    random.shuffle(value)
    return "".join(value)


def _totp_code(secret: str, timestamp: float | None = None) -> str:
    return totp_code(secret, timestamp)


def _random_name() -> tuple[str, str]:
    return random.choice(["James", "Robert", "John", "Michael", "David", "Mary", "Emma", "Olivia"]), random.choice(
        ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller"]
    )


def _random_birthdate() -> str:
    return f"{random.randint(1996, 2006):04d}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"


def _response_json(resp) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _is_wrong_email_otp_error(error: BaseException | str) -> bool:
    text = str(error or "").strip().lower()
    return "wrong_email_otp_code" in text or "wrong code" in text


def _auth_error_has_code(error: BaseException | str, code: str) -> bool:
    return str(code or "").strip().lower() in str(error or "").strip().lower()


def _mfa_activation_error_text(payload: Any) -> str:
    """Flatten activation error fields, including JSON-in-string details."""
    parts: list[str] = []

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 3 or value is None:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in {
                    "error",
                    "detail",
                    "message",
                    "code",
                    "type",
                }:
                    visit(item, depth + 1)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                visit(item, depth + 1)
            return
        text = str(value).strip()
        if not text:
            return
        parts.append(text)
        if text[:1] in "[{":
            try:
                visit(json.loads(text), depth + 1)
            except Exception:
                pass

    visit(payload)
    return " ".join(parts).lower()


def _otp_wait_timeout_message(mailbox: dict[str, Any], stage: str) -> str:
    message = f"等待{stage}验证码超时"
    provider = str(mailbox.get("provider") or "unknown").strip()
    attempts = max(0, int(mailbox.get("_code_wait_attempts") or 0))
    last_error = str(mailbox.get("_last_code_wait_error") or "").strip()
    details = [f"provider={provider}", f"polls={attempts}"]
    if last_error:
        details.append(f"last_error={last_error[:240]}")
    else:
        details.append("接口未返回新验证码")
    return f"{message} ({'; '.join(details)})"


def _response_debug_detail(resp, limit: int = 800) -> str:
    if resp is None:
        return ""
    data = _response_json(resp)
    parts = [
        f"url={str(getattr(resp, 'url', '') or '')[:300]}",
        f"content_type={str(getattr(resp, 'headers', {}).get('content-type') or '')}",
    ]
    for key in ("cf-ray", "x-request-id", "openai-processing-ms"):
        value = str(getattr(resp, "headers", {}).get(key) or "").strip()
        if value:
            parts.append(f"{key}={value}")
    if data:
        parts.append(f"json={json.dumps(data, ensure_ascii=False)[:limit]}")
    else:
        parts.append(f"body={str(getattr(resp, 'text', '') or '')[:limit]}")
    return ", ".join(parts)


def _is_cloudflare_challenge(resp) -> bool:
    if resp is None:
        return False
    try:
        status_code = int(getattr(resp, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status_code = 0
    if status_code not in (403, 503):
        return False
    text = str(getattr(resp, "text", "") or "").lower()
    return (
        "<title>just a moment" in text
        or "<title>attention required! | cloudflare" in text
        or "cf-chl-" in text
        or "__cf_chl_" in text
        or "cf-browser-verification" in text
    )


def _truthy(value: object, fallback: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return fallback


def _redact_operation_error(
    error: BaseException | str,
    *secrets: object,
    limit: int = 400,
) -> str:
    """Keep credential values out of maintenance logs and task events."""
    text = str(error or "").replace("\r", " ").replace("\n", " ")
    values = sorted(
        {
            str(value).strip()
            for value in secrets
            if str(value or "").strip()
        },
        key=len,
        reverse=True,
    )
    for value in values:
        text = text.replace(value, "[已隐藏]")
    return text[:limit]


def _operation_event_forwarder(
    task_event: Callable[..., Any] | None,
    account_log_sink: Callable[[dict[str, Any]], None] | None,
    *,
    operation: str,
    index: int,
    email: str = "",
    secrets: tuple[object, ...] = (),
) -> Callable[..., Any] | None:
    """Forward protocol task events to an optional per-account audit sink.

    Existing callers consume the historical callback signature
    ``(index, status, **fields)``.  The account log is deliberately a small,
    structured projection of that callback so credentials, proxy details and
    arbitrary provider payloads never enter the durable audit store.
    """
    if not callable(task_event) and not callable(account_log_sink):
        return task_event if callable(task_event) else None

    safe_operation = str(operation or "account_operation").strip()[:64]
    safe_email = str(email or "").strip()[:320]
    # Keep this mutable because security repair creates a password only after
    # the forwarder has been installed.  The operation can add that value
    # before invoking a provider so an echoed credential is still redacted.
    safe_secrets: list[object] = []
    for value in secrets or ():
        if str(value or "").strip() and not any(
            str(value) == str(existing) for existing in safe_secrets
        ):
            safe_secrets.append(value)

    def add_secrets(*values: object) -> None:
        for value in values:
            if not str(value or "").strip() or any(
                str(value) == str(existing) for existing in safe_secrets
            ):
                continue
            safe_secrets.append(value)

    def forward(event_index: int, status: str | None = None, **fields: Any) -> None:
        nonlocal safe_email
        # Keep the original callback behavior (including its exception
        # semantics), while guaranteeing a sink event when the callback is
        # well-behaved.  Sink failures are observational and must not change
        # the protocol result.
        try:
            if callable(task_event):
                task_event(event_index, status, **fields)
        finally:
            if not callable(account_log_sink):
                return
            event: dict[str, Any] = {
                "status": str(status or ""),
                "operation": safe_operation,
                "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "index": int(event_index) if str(event_index).lstrip("-").isdigit() else int(index),
            }
            event_email = str(fields.get("email") or safe_email).strip()
            if event_email:
                event["email"] = event_email[:320]
                safe_email = event_email[:320]
            # Registration completion is the first point where an Account ID
            # is guaranteed to be available.  Keep it in the small structured
            # projection so the durable log can bridge email-only attempts to
            # the later account-id identity.
            account_id = str(fields.get("account_id") or "").strip()
            if account_id:
                event["account_id"] = account_id[:320]
            trace_file = str(fields.get("trace_file") or "").strip()
            if trace_file:
                # Registration traces are resolved against the configured
                # trace directory by the API; retain only a bounded relative
                # path and never arbitrary provider payloads.
                event["trace_file"] = trace_file[:512]
            for key in (
                "step",
                "message",
                "error",
                "group_id",
                "skip_reason",
                "attempt",
            ):
                value = fields.get(key)
                if value is None or value == "":
                    continue
                event[key] = _redact_operation_error(value, *safe_secrets, limit=800)
            try:
                account_log_sink(event)
            except Exception:
                pass

    # This is intentionally an attribute rather than a second callback
    # argument, preserving the historical task-event callable signature.
    setattr(forward, "add_secrets", add_secrets)
    return forward


def _add_task_event_secrets(*values: object) -> None:
    """Extend the active event forwarder's redaction set when credentials appear."""
    callback = getattr(_TASK_EVENT.get(), "add_secrets", None)
    if callable(callback):
        try:
            callback(*values)
        except Exception:
            pass


def _tls_verify() -> bool | str:
    return current_runtime_settings().tls_verify


def _oauth_timeout() -> float:
    runtime = current_runtime_settings()
    return runtime.timeout_for(runtime.oauth_timeout)


def _protocol_proxy_for_task(
    proxy_pool: list[str],
    task_index: int,
    attempt: int,
    *,
    rotate: bool,
    start_offset: int = 0,
    retry_stride: int = 1,
    excluded: set[str] | None = None,
) -> str:
    """Choose a stable proxy slot for a task and advance it on retry."""
    if not proxy_pool:
        return ""
    blocked = excluded or set()
    if not rotate and not blocked:
        return proxy_pool[0]
    slot = (
        int(start_offset)
        + max(0, int(task_index) - 1)
        + max(0, int(attempt)) * max(1, int(retry_stride))
    ) % len(proxy_pool)
    for offset in range(len(proxy_pool)):
        candidate = proxy_pool[(slot + offset) % len(proxy_pool)]
        if candidate not in blocked:
            return candidate
    return proxy_pool[slot]


def _mail_config(register_proxy: str = "") -> dict:
    scoped_mail = _MAIL_CONFIG.get()
    mail = scoped_mail if isinstance(scoped_mail, dict) else (
        config["mail"] if isinstance(config.get("mail"), dict) else {}
    )
    use_register_proxy = _truthy(mail.get("api_use_register_proxy"), True)
    proxy = str(register_proxy or "").strip() if use_register_proxy else ""
    return {**mail, "api_use_register_proxy": use_register_proxy, "proxy": proxy}


def _authorize_landed_page(resp) -> str:
    """诊断用：粗判 authorize 之后落在哪个页面。返回 signup / login / "" 仅供日志。

    注意：email-verification / email_otp_verification 在注册和登录流程里都会出现，
    无法据此可靠区分，所以这里只用于打日志，绝不据此中断注册流程。
    """
    if resp is None:
        return ""
    final_url = str(getattr(resp, "url", "") or "").lower()
    data = _response_json(resp)
    page_type = ""
    page = data.get("page") if isinstance(data, dict) else None
    if isinstance(page, dict):
        page_type = str(page.get("type") or "").lower()
    if "create-account" in final_url or "signup" in final_url or "create_account" in page_type:
        return "signup"
    if "/log-in" in final_url or "/login" in final_url or page_type in {"login", "password_verification"}:
        return "login"
    return ""


def _auth_step_details(payload: dict[str, Any] | None) -> tuple[str, str]:
    data = payload if isinstance(payload, dict) else {}
    page = data.get("page") if isinstance(data.get("page"), dict) else {}
    page_payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
    continue_url = str(
        data.get("continue_url")
        or page_payload.get("url")
        or page_payload.get("continue_url")
        or ""
    ).strip()
    return continue_url, str(page.get("type") or "").strip()


def _auth_step_requires_password(continue_url: str, page_type: str) -> bool:
    return "password" in f"{continue_url or ''} {page_type or ''}".lower()


def _is_chatgpt_callback_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return False
    return parsed.netloc.lower() == "chatgpt.com" or "/api/auth/callback" in parsed.path.lower()


def create_mailbox(
    username: str | None = None,
    register_proxy: str = "",
    *,
    allow_used: bool = False,
) -> dict:
    return allocate_mailbox(
        _mail_config(register_proxy),
        username,
        provider_type=_PROVIDER_TYPE.get(),
        allow_used=allow_used,
    )


def wait_for_code(mailbox: dict, register_proxy: str = "") -> str | None:
    mail_config = _mail_config(register_proxy)
    fallback_code = str(
        mailbox.get("_post_registration_otp_fallback") or ""
    ).strip()
    if re.fullmatch(r"\d{6}", fallback_code):
        mail_config = dict(mail_config)
        mail_config["wait_timeout"] = min(
            60.0,
            float(mail_config.get("wait_timeout") or 90),
        )
    code = read_mail_code(
        mail_config,
        mailbox,
        stop_check=_STOP_CHECK.get(),
    )
    if code:
        mailbox.pop("_post_registration_otp_fallback", None)
        return code
    if re.fullmatch(r"\d{6}", fallback_code):
        mailbox.pop("_post_registration_otp_fallback", None)
        mailbox["_post_registration_otp_fallback_used"] = True
        return fallback_code
    return None


def build_sentinel_token(
    session: requests.Session,
    device_id: str,
    flow: str,
    fingerprint: dict[str, str] | None = None,
    diagnostic: Callable[[str], None] | None = None,
) -> str:
    """请求 sentinel token，返回 sentinel header 字符串（兼容旧接口）。"""
    fp = _browser_fingerprint(fingerprint)
    sentinel_val, oai_sc_val = _build_sentinel_token_tuple(
        session,
        device_id,
        flow,
        user_agent=fp["user_agent"],
        sec_ch_ua=fp["sec_ch_ua"],
        diagnostic=diagnostic,
        sid=str(fp.get("sentinel_sid") or ""),
        device_profile=fp,
    )
    _set_oai_sc_cookie(session, oai_sc_val)
    return sentinel_val


def _set_oai_sc_cookie(session: Any, value: str) -> None:
    value = str(value or "").strip()
    if not value:
        return
    try:
        session.cookies.set("oai-sc", value, domain=".openai.com", path="/")
        session.cookies.set("oai-sc", value, domain="auth.openai.com", path="/")
    except Exception:
        # Test doubles and alternate cookie jars may not support domain/path.
        try:
            session.cookies.set("oai-sc", value)
        except Exception:
            pass


def _build_sentinel_headers(
    session: requests.Session,
    device_id: str,
    flow: str,
    fingerprint: dict[str, str] | None = None,
    diagnostic: Callable[[str], None] | None = None,
) -> dict[str, str]:
    """Build the Sentinel headers observed on auth JSON requests."""
    fp = _browser_fingerprint(fingerprint)
    token, so_token, oai_sc = build_sentinel_with_so_token(
        session,
        device_id,
        flow,
        user_agent=fp["user_agent"],
        sec_ch_ua=fp["sec_ch_ua"],
        diagnostic=diagnostic,
        sid=str(fp.get("sentinel_sid") or ""),
        device_profile=fp,
    )
    _set_oai_sc_cookie(session, oai_sc)
    headers = {"openai-sentinel-token": token}
    if so_token:
        headers["openai-sentinel-so-token"] = so_token
    return headers


def create_session(proxy: str = "", fingerprint: dict[str, str] | None = None) -> Any:
    fp = _browser_fingerprint(fingerprint)
    runtime = current_runtime_settings()
    kwargs: dict[str, Any] = {
        "impersonate": fp["impersonate"],
        "verify": runtime.tls_verify,
    }
    normalized = normalize_proxy_url(proxy)
    if normalized:
        kwargs["proxies"] = {"http": normalized, "https": normalized}
    session = requests.Session(**kwargs)
    session.headers.update({"user-agent": fp["user_agent"]})
    return session


def _headers_with_clearance(
    headers: dict[str, str],
    target_url: str,
    proxy: str = "",
    user_agent_override: str = "",
) -> dict[str, str]:
    scoped_proxy_config = _PROXY_CONFIG.get()
    proxy_config = scoped_proxy_config if isinstance(scoped_proxy_config, dict) else config.get("proxy_config")
    return headers_with_clearance(
        proxy_config,
        headers,
        target_url,
        user_agent_override,
        request_proxy=proxy,
    )


def _cloudflare_block_message(resp, prefix: str = "被 Cloudflare 拦截", reason: str = "") -> str:
    status = getattr(resp, "status_code", "unknown")
    debug = _response_debug_detail(resp)
    reason = reason or "clearance 刷新失败或重试后仍失败，请更换 IP/代理重试"
    return f"{prefix}，{reason}: status={status}, {debug}"


def request_with_local_retry(session: requests.Session, method: str, url: str, retry_attempts: int = 3, **kwargs):
    """Request with bounded transport/temporary-status retry.

    POST requests are retried only for throttling and gateway-unavailable
    responses. This keeps the protocol resilient without blindly replaying
    every application-level failure.
    """
    last_error = ""
    method_name = str(method or "GET").upper()
    retryable_statuses = {408, 425, 429, 500, 502, 503, 504}
    if method_name not in {"GET", "HEAD", "OPTIONS"}:
        retryable_statuses.discard(500)
    attempts = max(1, retry_attempts)
    runtime = current_runtime_settings()
    request_kwargs = dict(kwargs)
    request_kwargs.setdefault("timeout", runtime.timeout_for(runtime.request_timeout))
    for attempt in range(attempts):
        if runtime.expired():
            return None, "protocol_account_timeout"
        try:
            response = session.request(method_name, url, **request_kwargs)
            status = int(getattr(response, "status_code", 0) or 0)
            should_retry = status in retryable_statuses and attempt + 1 < attempts
            if not should_retry:
                return response, ""
            retry_after = 0.0
            try:
                raw_retry_after = str(getattr(response, "headers", {}).get("retry-after") or "").strip()
                if raw_retry_after.isdigit():
                    retry_after = float(raw_retry_after)
                elif raw_retry_after:
                    retry_after = max(0.0, (parsedate_to_datetime(raw_retry_after).timestamp() - time.time()))
            except Exception:
                retry_after = 0.0
            time.sleep(min(8.0, max(0.25, retry_after or (0.5 * (2 ** attempt)))))
        except Exception as error:
            last_error = str(error)
            if is_proxy_transport_error(last_error):
                return None, last_error
            if attempt + 1 >= attempts:
                break
            time.sleep(min(4.0, 0.5 * (2 ** attempt)))
    return None, last_error


def validate_otp(
    session: requests.Session,
    device_id: str,
    code: str,
    fingerprint: dict[str, str] | None = None,
    diagnostic: Callable[[str], None] | None = None,
    allow_sentinel_retry: bool = True,
):
    headers = _header_fingerprint(common_headers, fingerprint)
    headers["referer"] = f"{auth_base}/email-verification"
    headers["oai-device-id"] = device_id
    headers["x-access-flow-invocation-id"] = str(uuid.uuid4())
    headers.update(_make_trace_headers())
    sentinel_error = ""
    if allow_sentinel_retry:
        try:
            headers.update(
                _build_sentinel_headers(
                    session,
                    device_id,
                    "email_otp_validate",
                    fingerprint,
                    diagnostic,
                )
            )
        except Exception as error:
            sentinel_error = f"sentinel_prepare_failed: {str(error)[:200]}"
            if diagnostic:
                diagnostic(sentinel_error)
    resp, error = request_with_local_retry(session, "post", f"{auth_base}/api/accounts/email-otp/validate", json={"code": code}, headers=headers, verify=_tls_verify())
    if not allow_sentinel_retry:
        return resp, error
    if resp is not None and resp.status_code == 200:
        return resp, ""
    try:
        headers.update(
            _build_sentinel_headers(
                session,
                device_id,
                "email_otp_validate",
                fingerprint,
                diagnostic,
            )
        )
    except Exception as retry_error:
        return resp, error or sentinel_error or f"sentinel_retry_failed: {str(retry_error)[:200]}"
    resp, error = request_with_local_retry(session, "post", f"{auth_base}/api/accounts/email-otp/validate", json={"code": code}, headers=headers, verify=_tls_verify())
    return resp, error


def extract_oauth_callback_params_from_url(url: str) -> dict[str, str] | None:
    if not url:
        return None
    try:
        params = parse_qs(urlparse(url).query)
    except Exception:
        return None
    code = str((params.get("code") or [""])[0]).strip()
    if not code:
        return None
    return {"code": code, "state": str((params.get("state") or [""])[0]).strip(), "scope": str((params.get("scope") or [""])[0]).strip()}


def _callback_state_matches(callback: dict[str, str] | None, expected_state: str = "") -> bool:
    """Validate OAuth state when both sides provide it.

    Some consent redirects omit state, so absence remains compatible while an
    explicit mismatch is rejected before token exchange.
    """
    expected = str(expected_state or "").strip()
    actual = str((callback or {}).get("state") or "").strip()
    return not expected or not actual or secrets.compare_digest(expected, actual)


def _absolute_auth_url(url: str) -> str:
    value = str(url or "").strip()
    if value.startswith("/"):
        return f"{auth_base}{value}"
    return value


def _safe_url_for_log(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        return "-"
    try:
        parsed = urlparse(value)
    except Exception:
        return value[:160]
    if parsed.scheme and parsed.netloc:
        return f"{parsed.netloc}{parsed.path}"[:160]
    return parsed.path[:160] if parsed.path else value[:160]


def _auth_error_details_from_url(url: str) -> dict[str, str]:
    """Decode the error code embedded in Auth API redirect URLs."""
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return {}
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/").lower()
    if not (
        (host == "auth.openai.com" and path == "/error")
        or (host == "chatgpt.com" and path == "/auth/error")
    ):
        return {}
    query = parse_qs(parsed.query, keep_blank_values=True)
    direct = str((query.get("error") or [""])[0]).strip().lower()
    details = {
        "code": re.sub(r"[^a-z0-9_]+", "_", direct).strip("_") or "unknown",
        "request_id": "",
    }
    payload = str((query.get("payload") or [""])[0]).strip()
    if not payload:
        return details
    try:
        payload += "=" * (-len(payload) % 4)
        decoded = json.loads(base64.b64decode(payload).decode("utf-8"))
    except Exception:
        return details
    if not isinstance(decoded, dict):
        return details
    code = str(decoded.get("errorCode") or decoded.get("error_code") or "").strip().lower()
    request_id = str(decoded.get("requestId") or decoded.get("request_id") or "").strip()
    if code:
        details["code"] = re.sub(r"[^a-z0-9_]+", "_", code).strip("_") or "unknown"
    details["request_id"] = request_id[:120]
    return details


def _url_path(url: str) -> str:
    value = _absolute_auth_url(url)
    try:
        return urlparse(value).path.rstrip("/") or "/"
    except Exception:
        return ""


def _cookie_values(session) -> dict[str, str]:
    try:
        jar = getattr(session.cookies, "jar", session.cookies)
        return {
            str(getattr(cookie, "name", "") or ""): str(
                getattr(cookie, "value", "") or ""
            )
            for cookie in list(jar)
            if str(getattr(cookie, "name", "") or "")
        }
    except Exception:
        try:
            return {
                str(key): str(value)
                for key, value in session.cookies.get_dict().items()
            }
        except Exception:
            return {}


def _nextauth_session_cookie(session) -> str:
    cookies = _cookie_values(session)
    base = "__Secure-next-auth.session-token"
    if cookies.get(base):
        return cookies[base]
    chunks: list[tuple[int, str]] = []
    for name, value in cookies.items():
        if not name.startswith(base + "."):
            continue
        try:
            index = int(name.rsplit(".", 1)[1])
        except (TypeError, ValueError):
            continue
        chunks.append((index, value))
    return "".join(value for _, value in sorted(chunks))


def _append_exchange_error(errors: list[str] | None, message: str) -> None:
    if errors is not None and message:
        errors.append(message)


def request_platform_oauth_token(
    session: requests.Session,
    code: str,
    code_verifier: str,
    errors: list[str] | None = None,
    fingerprint: dict[str, str] | None = None,
) -> dict | None:
    fp = _browser_fingerprint(fingerprint)
    headers = {
        "accept": "*/*",
        "accept-language": fp["accept_language"],
        "auth0-client": platform_auth0_client,
        "cache-control": "no-cache",
        "content-type": "application/json",
        "origin": platform_base,
        "pragma": "no-cache",
        "priority": "u=1, i",
        "referer": f"{platform_base}/",
        **_client_hint_headers(fp),
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": fp["user_agent"],
    }
    try:
        resp = session.post(
            f"{auth_base}/api/accounts/oauth/token",
            headers=headers,
            json={
                "client_id": platform_oauth_client_id,
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": platform_oauth_redirect_uri,
            },
            verify=_tls_verify(),
            timeout=_oauth_timeout(),
        )
    except Exception as error:
        _append_exchange_error(errors, f"api token 请求异常: {str(error)[:300]}")
        return None
    if resp.status_code != 200:
        _append_exchange_error(errors, f"api token 接口拒绝: status={resp.status_code}, {_response_debug_detail(resp, 500)}")
        return None
    data = _response_json(resp)
    missing = [key for key in ("access_token", "refresh_token") if not data.get(key)]
    if missing:
        _append_exchange_error(errors, f"api token 返回缺少字段: {', '.join(missing)}")
        return None
    return data


def request_platform_oauth_token_legacy(
    session: requests.Session,
    code: str,
    code_verifier: str,
    proxy: str = "",
    errors: list[str] | None = None,
    fresh_session: bool = False,
    fingerprint: dict[str, str] | None = None,
) -> dict | None:
    token_session = create_session(proxy, fingerprint) if fresh_session else session
    resp = None
    try:
        resp = token_session.post(
            f"{auth_base}/oauth/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": platform_oauth_redirect_uri,
                "client_id": platform_oauth_client_id,
                "code_verifier": code_verifier,
            },
            verify=_tls_verify(),
            timeout=_oauth_timeout(),
        )
    except Exception as error:
        _append_exchange_error(errors, f"legacy token 请求异常: {str(error)[:300]}")
        return None
    finally:
        if fresh_session:
            try:
                token_session.close()
            except Exception:
                pass
    if resp is None:
        _append_exchange_error(errors, "legacy token 未返回响应")
        return None
    data = _response_json(resp)
    if resp.status_code != 200:
        _append_exchange_error(errors, f"legacy token 接口拒绝: status={resp.status_code}, {_response_debug_detail(resp, 500)}")
        return None
    missing = [key for key in ("access_token", "refresh_token", "id_token") if not data.get(key)]
    if missing:
        _append_exchange_error(errors, f"legacy token 返回缺少字段: {', '.join(missing)}")
        return None
    return data


def extract_callback_via_consent(
    session: requests.Session,
    consent_url: str,
    device_id: str,
    proxy: str = "",
    user_agent_override: str = "",
    fingerprint: dict[str, str] | None = None,
) -> dict[str, str] | None:
    current = _absolute_auth_url(consent_url)
    if not current:
        return None
    fp = _fingerprint_with_user_agent(fingerprint, user_agent_override) if user_agent_override else _browser_fingerprint(fingerprint)
    for _ in range(10):
        headers = _headers_with_clearance(_header_fingerprint(navigate_headers, fp), current, proxy, user_agent_override)
        resp, _error = request_with_local_retry(session, "get", current, headers=headers, verify=_tls_verify(), allow_redirects=False)
        if resp is None:
            return None
        callback = extract_oauth_callback_params_from_url(str(getattr(resp, "url", "") or ""))
        callback = callback or extract_oauth_callback_params_from_url(str(getattr(resp, "headers", {}).get("Location") or ""))
        if callback:
            return callback
        location = str(getattr(resp, "headers", {}).get("Location") or "").strip()
        if getattr(resp, "status_code", 0) not in (301, 302, 303, 307, 308) or not location:
            break
        current = _absolute_auth_url(location)

    raw = session.cookies.get("oai-client-auth-session", domain=".auth.openai.com") or session.cookies.get("oai-client-auth-session")
    if not raw:
        return None
    try:
        first = raw.split(".")[0]
        pad = 4 - len(first) % 4
        if pad != 4:
            first += "=" * pad
        payload = json.loads(base64.urlsafe_b64decode(first))
        workspace_id = payload["workspaces"][0]["id"]
    except Exception:
        return None

    url = f"{auth_base}/api/accounts/workspace/select"
    headers = _header_fingerprint(common_headers, fp)
    headers["referer"] = current
    headers["oai-device-id"] = device_id
    headers.update(_make_trace_headers())
    headers = _headers_with_clearance(headers, url, proxy, user_agent_override)
    ws_resp, _error = request_with_local_retry(session, "post", url, json={"workspace_id": workspace_id}, headers=headers, verify=_tls_verify(), allow_redirects=False)
    if ws_resp is None:
        return None
    callback = extract_oauth_callback_params_from_url(str(getattr(ws_resp, "headers", {}).get("Location") or ""))
    if callback:
        return callback

    ws_data = _response_json(ws_resp)
    orgs = ((ws_data.get("data") or {}).get("orgs") or []) if isinstance(ws_data, dict) else []
    if not orgs:
        return None
    org_id = str((orgs[0] or {}).get("id") or "").strip()
    project_id = str(((orgs[0] or {}).get("projects") or [{}])[0].get("id") or "").strip()
    if not org_id:
        return None
    org_url = f"{auth_base}/api/accounts/organization/select"
    org_headers = _header_fingerprint(common_headers, fp)
    org_headers["referer"] = str(ws_data.get("continue_url") or current)
    org_headers["oai-device-id"] = device_id
    org_headers.update(_make_trace_headers())
    org_headers = _headers_with_clearance(org_headers, org_url, proxy, user_agent_override)
    body = {"org_id": org_id}
    if project_id:
        body["project_id"] = project_id
    org_resp, _error = request_with_local_retry(session, "post", org_url, json=body, headers=org_headers, verify=_tls_verify(), allow_redirects=False)
    if org_resp is None:
        return None
    return extract_oauth_callback_params_from_url(str(getattr(org_resp, "headers", {}).get("Location") or ""))


def exchange_tokens_from_continue_url(
    session: requests.Session,
    device_id: str,
    code_verifier: str,
    continue_url: str,
    proxy: str = "",
    user_agent_override: str = "",
    errors: list[str] | None = None,
    fingerprint: dict[str, str] | None = None,
    expected_state: str = "",
) -> dict | None:
    callback = extract_oauth_callback_params_from_url(continue_url)
    if callback and not _callback_state_matches(callback, expected_state):
        _append_exchange_error(errors, "OAuth callback state 不匹配")
        return None
    fp = _fingerprint_with_user_agent(fingerprint, user_agent_override) if user_agent_override else _browser_fingerprint(fingerprint)
    callback = callback or extract_callback_via_consent(
        session,
        continue_url,
        device_id,
        proxy,
        user_agent_override,
        fp,
    )
    if callback and not _callback_state_matches(callback, expected_state):
        _append_exchange_error(errors, "OAuth consent callback state 不匹配")
        return None
    if not callback:
        url = _absolute_auth_url(continue_url)
        try:
            headers = _headers_with_clearance(_header_fingerprint(navigate_headers, fp), url, proxy, user_agent_override)
            runtime = current_runtime_settings()
            resp = session.get(
                url,
                headers=headers,
                allow_redirects=True,
                verify=_tls_verify(),
                timeout=runtime.timeout_for(runtime.request_timeout),
            )
            callback = extract_oauth_callback_params_from_url(str(getattr(resp, "url", "") or ""))
            if not callback:
                for history in getattr(resp, "history", []) or []:
                    callback = extract_oauth_callback_params_from_url(str(history.headers.get("Location") or ""))
                    if callback:
                        break
            if callback and not _callback_state_matches(callback, expected_state):
                _append_exchange_error(errors, "OAuth redirect callback state 不匹配")
                callback = None
            if not callback:
                _append_exchange_error(
                    errors,
                    f"跟随 continue_url 后仍未拿到 callback: status={getattr(resp, 'status_code', 'unknown')}, final={_safe_url_for_log(str(getattr(resp, 'url', '') or ''))}",
                )
        except Exception as error:
            _append_exchange_error(errors, f"跟随 continue_url 异常: {str(error)[:300]}")
            callback = None
    code = str((callback or {}).get("code") or "").strip()
    if not code:
        _append_exchange_error(errors, f"未拿到 OAuth callback code: continue={_safe_url_for_log(continue_url)}")
        return None
    return request_platform_oauth_token_legacy(session, code, code_verifier, proxy, errors, fresh_session=True, fingerprint=fp)


class PlatformRegistrar:
    def __init__(self, proxy: str = "") -> None:
        self.proxy = str(proxy or "").strip()
        self.fingerprint = _make_browser_fingerprint(self.proxy)
        self.session = create_session(self.proxy, self.fingerprint)
        self.clearance_user_agent = ""
        self.clearance_failure_reason = ""
        self.device_id = str(uuid.uuid4())
        self.code_verifier = ""
        self.platform_auth_code = ""
        self.authorization_state = ""
        self.authorization_nonce = ""
        self._sync_session_identity()

    def close(self) -> None:
        self.session.close()

    def _sync_session_identity(self) -> None:
        """Keep the HTTP session identity aligned with the current profile."""
        fp = _browser_fingerprint(self.fingerprint)
        try:
            if hasattr(self.session, "impersonate"):
                self.session.impersonate = fp["impersonate"]
            self.session.headers.update(
                {
                    "user-agent": fp["user_agent"],
                    "oai-device-id": self.device_id,
                }
            )
            for key in ("sec-ch-ua", "sec-ch-ua-full-version-list", "sec-ch-ua-mobile", "sec-ch-ua-platform"):
                self.session.headers.pop(key, None)
            self.session.headers.update(_client_hint_headers(fp))
        except Exception:
            pass

    @staticmethod
    def _diagnostic(index: int, message: str) -> None:
        step(index, f"[协议诊断] {message}")

    def _log_identity(self, index: int) -> None:
        fp = _browser_fingerprint(self.fingerprint)
        self._diagnostic(
            index,
            "TLS/HTTP 指纹 "
            f"impersonate={fp['impersonate']} firefox={fp['full_version']} "
            f"platform=Windows/{fp['platform_version']} language={fp['accept_language']}",
        )
        self._diagnostic(index, f"User-Agent={fp['user_agent']}")
        self._diagnostic(index, f"sec-ch-ua={fp['sec_ch_ua']}")
        self._diagnostic(index, f"设备标识 oai-did={self.device_id}")
        self._diagnostic(
            index,
            f"设备画像 profile={fp.get('profile_id')} screen={fp.get('screen')} "
            f"cpu={fp.get('hardware_concurrency')} memory={fp.get('device_memory')} "
            f"locale={fp.get('locale')} tz={fp.get('timezone')} "
            f"offset={fp.get('timezone_offset_min')} webgl={fp.get('webgl_vendor')} "
            f"sentinel_sid={fp.get('sentinel_sid')}",
        )
        self._diagnostic(
            index,
            f"网络出口 country={_proxy_country_code(self.proxy)} "
            f"proxy={proxy_brief(self.proxy) if self.proxy else 'direct'}",
        )

    def _navigate_headers(self, referer: str = "") -> dict[str, str]:
        headers = _header_fingerprint(navigate_headers, self.fingerprint)
        if referer:
            headers["referer"] = referer
        return headers

    def _json_headers(self, referer: str) -> dict[str, str]:
        headers = _header_fingerprint(common_headers, self.fingerprint)
        headers["referer"] = referer
        headers["oai-device-id"] = self.device_id
        headers.update(_make_trace_headers())
        return headers

    def _refresh_cloudflare_clearance(self, target_url: str, index: int):
        self.clearance_failure_reason = ""
        step(index, "检测到 Cloudflare 拦截，正在通过 FlareSolverr 清障", "yellow")
        scoped_proxy_config = _PROXY_CONFIG.get()
        proxy_config = (
            scoped_proxy_config
            if isinstance(scoped_proxy_config, dict)
            else config.get("proxy_config")
        )
        bundle = refresh_clearance(
            proxy_config,
            target_url,
            request_proxy=self.proxy,
        )
        if bundle is None:
            detail = clearance_error()
            self.clearance_failure_reason = detail or "FlareSolverr 未返回可用 clearance，请检查服务、代理和出口 IP"
            step(index, self.clearance_failure_reason, "yellow")
            return None
        # Keep the protocol identity stable.  FlareSolverr may report its own
        # browser UA (often Chromium); it is only used for clearance cookies,
        # never as the account's Firefox fingerprint.
        self.clearance_user_agent = _browser_fingerprint(self.fingerprint)["user_agent"]
        self._sync_session_identity()
        for name, value in bundle.cookies.items():
            try:
                self.session.cookies.set(name, value, domain=".openai.com")
                self.session.cookies.set(name, value, domain="auth.openai.com")
            except Exception:
                pass
        step(index, "FlareSolverr clearance 已缓存，重试当前请求", "yellow")
        return bundle

    def _platform_authorize(self, email: str, index: int, screen_hint: str = "signup") -> str:
        step(index, "开始 platform authorize")
        self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
        self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")
        self.code_verifier, code_challenge = _generate_pkce()
        params = {
            "issuer": auth_base,
            "client_id": platform_oauth_client_id,
            "audience": platform_oauth_audience,
            "redirect_uri": platform_oauth_redirect_uri,
            "device_id": self.device_id,
            # 注册流程显式声明 signup：throwaway 域名 OpenAI 会自动当新账号走注册，
            # 但 @outlook.com/@hotmail.com 这类真实消费邮箱会被 login_or_signup 路由到登录分支，
            # 后续 user/register 落在错误的 auth step 上报 invalid_auth_step。
            "screen_hint": screen_hint,
            "max_age": "0",
            "login_hint": email,
            "scope": "openid profile email offline_access",
            "response_type": "code",
            "response_mode": "query",
            "state": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(32),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "auth0Client": platform_auth0_client,
        }
        self.authorization_state = str(params["state"])
        self.authorization_nonce = str(params["nonce"])
        self._sync_session_identity()
        self._diagnostic(
            index,
            "OAuth/PKCE "
            f"screen_hint={screen_hint} state={self.authorization_state[:12]}... "
            f"nonce={self.authorization_nonce[:12]}... verifier_len={len(self.code_verifier)} "
            f"challenge={code_challenge[:16]}... method=S256",
        )
        target_url = f"{auth_base}/api/accounts/authorize?{urlencode(params)}"
        headers = self._navigate_headers(f"{platform_base}/")
        headers = _headers_with_clearance(headers, target_url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "get", target_url, headers=headers, allow_redirects=True, verify=_tls_verify())
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(target_url, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            retry_headers = _headers_with_clearance(self._navigate_headers(f"{platform_base}/"), target_url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, "get", target_url, headers=retry_headers, allow_redirects=True, verify=_tls_verify())
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code != 200:
            err = _response_json(resp).get("error", {}) if resp is not None else {}
            detail = f": {err.get('code', '')} - {err.get('message', '')}".strip(" -") if err else ""
            debug = _response_debug_detail(resp)
            status = getattr(resp, "status_code", "unknown")
            raise RuntimeError(error or f"platform_authorize_http_{status}{detail}, {debug}")
        landed = _authorize_landed_page(resp)
        response_headers = getattr(resp, "headers", {}) or {}
        trace = " ".join(
            f"{name}={response_headers.get(name)}"
            for name in ("x-request-id", "cf-ray", "openai-processing-ms")
            if response_headers.get(name)
        )
        step(
            index,
            f"platform authorize 完成[{landed or '?'}] http={resp.status_code} "
            f"url={str(getattr(resp, 'url', '') or '')[:160]}{(' ' + trace) if trace else ''}",
        )
        return landed

    def _complete_chatgpt_session(
        self,
        email: str,
        password: str,
        index: int,
        *,
        start_only: bool = False,
        post_login_action: str = "",
    ) -> dict[str, Any]:
        """Finish the registered identity through ChatGPT NextAuth.

        Platform OAuth can return syntactically valid tokens before a ChatGPT
        account/workspace exists.  The NextAuth session and /backend-api/me are
        the completion boundary used by the actual ChatGPT web application.
        """
        step(index, "开始建立 ChatGPT 会话")
        fp = _browser_fingerprint(self.fingerprint)
        common = {
            "accept": "application/json",
            "accept-language": fp["accept_language"],
            "user-agent": fp["user_agent"],
        **_client_hint_headers(fp),
        }

        csrf_url = chatgpt_base + "/api/auth/csrf"

        def fetch_csrf() -> tuple[str, Any, str]:
            csrf_headers = _headers_with_clearance(
                {**common, "referer": chatgpt_base + "/"},
                csrf_url,
                self.proxy,
                self.clearance_user_agent,
            )
            csrf_response, csrf_error = request_with_local_retry(
                self.session,
                "get",
                csrf_url,
                headers=csrf_headers,
                verify=_tls_verify(),
            )
            csrf_payload = _response_json(csrf_response)
            csrf_token = str(csrf_payload.get("csrfToken") or "").strip()
            return csrf_token, csrf_response, csrf_error

        # Fast-path: /api/auth/csrf usually creates the required NextAuth CSRF
        # cookie by itself.  Only fetch the heavy homepage HTML when direct CSRF
        # fails or lands on a challenge.  This removes one high-latency request
        # from the common protocol path.
        csrf_token, csrf_response, csrf_error = fetch_csrf()
        if _is_cloudflare_challenge(csrf_response):
            bundle = self._refresh_cloudflare_clearance(csrf_url, index)
            if bundle is None:
                raise RuntimeError(
                    _cloudflare_block_message(
                        csrf_response,
                        reason=self.clearance_failure_reason,
                    )
                )
            csrf_token, csrf_response, csrf_error = fetch_csrf()
        if (
            csrf_response is None
            or csrf_response.status_code != 200
            or not csrf_token
        ):
            step(index, "CSRF fast-path 未命中，回退 ChatGPT 首页预热", "yellow")
            home_headers = {**common, "accept": "text/html,application/xhtml+xml"}
            home_headers = _headers_with_clearance(
                home_headers,
                chatgpt_base + "/",
                self.proxy,
                self.clearance_user_agent,
            )
            home, home_error = request_with_local_retry(
                self.session,
                "get",
                chatgpt_base + "/",
                retry_attempts=4,
                headers=home_headers,
                allow_redirects=True,
                verify=_tls_verify(),
            )
            if _is_cloudflare_challenge(home):
                bundle = self._refresh_cloudflare_clearance(chatgpt_base + "/", index)
                if bundle is None:
                    raise RuntimeError(
                        _cloudflare_block_message(
                            home,
                            reason=self.clearance_failure_reason,
                        )
                    )
                home_headers = _headers_with_clearance(
                    {**common, "accept": "text/html,application/xhtml+xml"},
                    chatgpt_base + "/",
                    self.proxy,
                    self.clearance_user_agent,
                )
                home, home_error = request_with_local_retry(
                    self.session,
                    "get",
                    chatgpt_base + "/",
                    retry_attempts=4,
                    headers=home_headers,
                    allow_redirects=True,
                    verify=_tls_verify(),
                )
                if _is_cloudflare_challenge(home):
                    raise RuntimeError(
                        _cloudflare_block_message(
                            home,
                            "ChatGPT 首页 clearance 重试仍被拦截",
                        )
                    )
            if home is None or int(getattr(home, "status_code", 0) or 0) >= 400:
                raise RuntimeError(
                    home_error
                    or f"chatgpt_home_http_{getattr(home, 'status_code', 'unknown')}: "
                    f"{_response_debug_detail(home)}"
                )
            csrf_token, csrf_response, csrf_error = fetch_csrf()
            if (
                csrf_response is None
                or csrf_response.status_code != 200
                or not csrf_token
            ):
                raise RuntimeError(
                    csrf_error
                    or f"chatgpt_csrf_http_{getattr(csrf_response, 'status_code', 'unknown')}"
                )

        signin_params = {
            "prompt": "login",
            "ext-passkey-client-capabilities": "11111",
            "ext-oai-did": self.device_id,
            "auth_session_logging_id": str(uuid.uuid4()),
            "screen_hint": "login_or_signup",
            "login_hint": email,
        }
        if str(password or "").strip():
            # A login_hint identifies the account but does not require the
            # password connection.  Without this hint some existing accounts
            # are routed into email OTP and password/verify then fails with
            # invalid_state even though a password is stored locally.
            signin_params.update({"connection": "password", "max_age": "0"})
            # ChatGPT uses the same NextAuth transaction for account
            # maintenance actions.  Keep this opt-in so registration and
            # ordinary credential login retain their existing query shape.
            action = str(post_login_action or "").strip().lower().replace("-", "_")
            if action in {"password_reset", "reset_password"}:
                signin_params.update(
                    {
                        "reauth": "password",
                        "post_login_password_reset": "true",
                    }
                )
            elif action in {"password_add", "add_password"}:
                signin_params.update(
                    {
                        "reauth": "password",
                        "post_login_add_password": "true",
                    }
                )
        query = urlencode(signin_params)
        signin_url = f"{chatgpt_base}/api/auth/signin/openai?{query}"
        signin_headers = _headers_with_clearance(
            {
                **common,
                "content-type": "application/x-www-form-urlencoded",
                "origin": chatgpt_base,
                "referer": chatgpt_base + "/",
            },
            signin_url,
            self.proxy,
            self.clearance_user_agent,
        )
        signin_response, signin_error = request_with_local_retry(
            self.session,
            "post",
            signin_url,
            headers=signin_headers,
            data={
                "callbackUrl": chatgpt_base + "/",
                "csrfToken": csrf_token,
                "json": "true",
            },
            allow_redirects=False,
            verify=_tls_verify(),
        )
        signin_payload = _response_json(signin_response)
        authorize_url = str(
            signin_payload.get("url")
            or getattr(signin_response, "headers", {}).get("Location")
            or ""
        ).strip()
        if (
            signin_response is None
            or signin_response.status_code >= 400
            or not authorize_url
        ):
            raise RuntimeError(
                signin_error
                or f"chatgpt_signin_http_{getattr(signin_response, 'status_code', 'unknown')}"
            )

        authorize_headers = _headers_with_clearance(
            {**common, "accept": "text/html,application/xhtml+xml", "referer": chatgpt_base + "/"},
            authorize_url,
            self.proxy,
            self.clearance_user_agent,
        )
        authorize_response, authorize_error = request_with_local_retry(
            self.session,
            "get",
            authorize_url,
            headers=authorize_headers,
            allow_redirects=True,
            verify=_tls_verify(),
        )
        # The authorize URL is served by auth.openai.com and can be challenged
        # independently from the ChatGPT CSRF/home requests above.  In the HAR
        # flow this appears as a 403 Cloudflare page followed by a successful
        # redirect after clearance is refreshed.  Treat it like the other
        # authenticated navigation steps instead of surfacing a transient
        # challenge as a permanent login failure.
        if _is_cloudflare_challenge(authorize_response):
            bundle = self._refresh_cloudflare_clearance(authorize_url, index)
            if bundle is None:
                raise RuntimeError(
                    _cloudflare_block_message(
                        authorize_response,
                        reason=self.clearance_failure_reason,
                    )
                )
            authorize_headers = _headers_with_clearance(
                {**common, "accept": "text/html,application/xhtml+xml", "referer": chatgpt_base + "/"},
                authorize_url,
                self.proxy,
                self.clearance_user_agent,
            )
            authorize_response, authorize_error = request_with_local_retry(
                self.session,
                "get",
                authorize_url,
                retry_attempts=5,
                headers=authorize_headers,
                allow_redirects=True,
                verify=_tls_verify(),
            )
            if _is_cloudflare_challenge(authorize_response):
                raise RuntimeError(
                    _cloudflare_block_message(
                        authorize_response,
                        "ChatGPT authorize clearance 重试仍被拦截",
                    )
                )
        final_url = str(getattr(authorize_response, "url", "") or "")
        auth_error = _auth_error_details_from_url(final_url)
        if (
            authorize_response is None
            or authorize_response.status_code >= 400
            or auth_error
        ):
            if auth_error:
                code = auth_error.get("code") or "unknown"
                request_id = auth_error.get("request_id") or ""
                request_hint = f" request_id={request_id}" if request_id else ""
                step(
                    index,
                    f"初始认证进入错误页 · code={code}{request_hint}",
                    "yellow",
                )
                raise RuntimeError(
                    f"auth_step_error_page: code={code}{request_hint}"
                )
            raise RuntimeError(
                authorize_error
                or f"chatgpt_authorize_http_{getattr(authorize_response, 'status_code', 'unknown')}"
            )

        device_cookie = str(_cookie_values(self.session).get("oai-did") or "").strip()
        if device_cookie:
            self.device_id = device_cookie
            self._sync_session_identity()
        if start_only:
            step(
                index,
                (
                    "ChatGPT NextAuth 凭据认证事务已建立（邮箱仅作账号标识）"
                    if str(password or "").strip()
                    else "ChatGPT NextAuth 初始化完成，等待邮箱验证码"
                ),
            )
            return {
                "authorize_url": authorize_url,
                "final_url": final_url,
                "device_id": self.device_id,
            }

        return self._read_chatgpt_session(email, password, index)

    def _navigate_auth_step(self, url: str, referer: str, index: int) -> Any:
        target = _absolute_auth_url(url)
        if not target:
            return None
        headers = _headers_with_clearance(
            self._navigate_headers(referer),
            target,
            self.proxy,
            self.clearance_user_agent,
        )
        response, error = request_with_local_retry(
            self.session,
            "get",
            target,
            retry_attempts=5,
            headers=headers,
            allow_redirects=True,
            verify=_tls_verify(),
        )
        if _is_cloudflare_challenge(response):
            bundle = self._refresh_cloudflare_clearance(target, index)
            if bundle is None:
                raise RuntimeError(
                    _cloudflare_block_message(
                        response,
                        reason=self.clearance_failure_reason,
                    )
                )
            headers = _headers_with_clearance(
                self._navigate_headers(referer),
                target,
                self.proxy,
                self.clearance_user_agent,
            )
            response, error = request_with_local_retry(
                self.session,
                "get",
                target,
                retry_attempts=5,
                headers=headers,
                allow_redirects=True,
                verify=_tls_verify(),
            )
            if _is_cloudflare_challenge(response):
                raise RuntimeError(
                    _cloudflare_block_message(
                        response,
                        "认证跳转 clearance 重试仍被拦截",
                    )
                )
        if response is None or int(getattr(response, "status_code", 0) or 0) >= 400:
            raise RuntimeError(
                error
                or f"auth_step_http_{getattr(response, 'status_code', 'unknown')}: "
                f"{_safe_url_for_log(target)}, {_response_debug_detail(response)}"
            )
        final_url = str(getattr(response, "url", "") or target)
        auth_error = _auth_error_details_from_url(final_url)
        if auth_error:
            code = auth_error.get("code") or "unknown"
            request_id = auth_error.get("request_id") or ""
            request_hint = f" request_id={request_id}" if request_id else ""
            step(
                index,
                f"认证步骤进入错误页 · code={code}{request_hint}",
                "yellow",
            )
            raise RuntimeError(
                f"auth_step_error_page: code={code}{request_hint}"
            )
        step(
            index,
            f"认证步骤已跟随 · {getattr(response, 'status_code', 0)} "
            f"{_safe_url_for_log(final_url)}",
        )
        return response

    def _post_auth_json(
        self,
        url: str,
        referer: str,
        payload: dict[str, Any],
        index: int,
        operation: str,
        *,
        flow_invocation: bool = False,
    ) -> dict[str, Any]:
        headers = self._json_headers(referer)
        if operation == "password_verify":
            headers["openai-sentinel-token"] = build_sentinel_token(
                self.session,
                self.device_id,
                "password_verify",
                self.fingerprint,
                lambda message: self._diagnostic(index, message),
            )
        if operation in {"password_verify", "mfa_verify"} or flow_invocation:
            headers["x-access-flow-invocation-id"] = str(uuid.uuid4())
        headers = _headers_with_clearance(
            headers, url, self.proxy, self.clearance_user_agent
        )
        response, error = request_with_local_retry(
            self.session,
            "post",
            url,
            json=payload,
            headers=headers,
            allow_redirects=False,
            verify=_tls_verify(),
        )
        data = _response_json(response)
        if response is None or response.status_code != 200:
            error_data = data.get("error") if isinstance(data, dict) else {}
            error_code = (
                str(error_data.get("code") or "").strip()
                if isinstance(error_data, dict)
                else str(error_data or "").strip()
            )
            detail = f": {error_code}" if error_code else ""
            raise RuntimeError(
                error
                or f"{operation}_http_{getattr(response, 'status_code', 'unknown')}{detail}"
            )
        if isinstance(data.get("error"), dict):
            error_data = data["error"]
            raise RuntimeError(
                f"{operation}_failed: {str(error_data.get('code') or error_data)[:160]}"
            )
        return data

    def _dump_client_auth_session(self, referer: str, index: int) -> dict[str, Any]:
        """Refresh the auth-session checksum observed during the MFA page load."""
        url = f"{auth_base}/api/accounts/client_auth_session_dump"
        headers = _headers_with_clearance(
            self._navigate_headers(referer),
            url,
            self.proxy,
            self.clearance_user_agent,
        )
        response, error = request_with_local_retry(
            self.session,
            "get",
            url,
            headers=headers,
            allow_redirects=False,
            verify=_tls_verify(),
        )
        if response is None or response.status_code != 200:
            step(
                index,
                f"认证会话同步跳过 · {error or getattr(response, 'status_code', 'unknown')}",
                "yellow",
            )
            return {}
        return _response_json(response)

    @staticmethod
    def _totp_factor(data: dict[str, Any]) -> tuple[str, str]:
        page = data.get("page") if isinstance(data.get("page"), dict) else {}
        payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
        factors = payload.get("factors") if isinstance(payload.get("factors"), list) else []
        factor = next(
            (
                item
                for item in factors
                if isinstance(item, dict)
                and str(item.get("factor_type") or "").strip().lower() == "totp"
                and not _truthy(item.get("is_recovery"))
            ),
            None,
        )
        factor_id = str(payload.get("factor_id") or "").strip()
        if not factor_id and isinstance(factor, dict):
            factor_id = str(factor.get("id") or "").strip()
        factor_type = str((factor or {}).get("factor_type") or "").strip().lower()
        return factor_id, factor_type

    def login_with_password_totp(
        self,
        email: str,
        password: str,
        totp_secret: str,
        index: int,
    ) -> dict[str, Any]:
        """Complete the HAR-observed password plus TOTP login transaction."""
        email = str(email or "").strip()
        password = str(password or "").strip()
        totp_secret = str(totp_secret or "").strip()
        if not email:
            raise RuntimeError("协议凭据登录缺少邮箱")
        if not password or not totp_secret:
            raise RuntimeError("协议凭据登录要求同时具备密码和 TOTP 2FA")

        auth_start = self._complete_chatgpt_session(
            email,
            password,
            index,
            start_only=True,
        )
        landed_url = str(auth_start.get("final_url") or "").strip()
        if _url_path(landed_url) != "/log-in/password":
            identity_result = self._authorize_continue_login(
                email,
                index,
                passwordless=False,
                restart_invalid_state=False,
            )
            identity_url, identity_page = _auth_step_details(identity_result)
            if not _auth_step_requires_password(identity_url, identity_page):
                raise RuntimeError(
                    "credential_login_password_route_unavailable: "
                    f"page={identity_page or '?'} "
                    f"continue={_safe_url_for_log(identity_url)}; "
                    "服务端将该账号导向邮箱验证，未进入密码认证"
                )
        step(
            index,
            "协议凭据登录[1/6] 邮箱账号已识别（未读取验证码），准备密码验证",
        )
        password_url = f"{auth_base}/api/accounts/password/verify"
        password_page = f"{auth_base}/log-in/password"
        password_result = self._post_auth_json(
            password_url,
            password_page,
            {"password": password},
            index,
            "password_verify",
        )
        factor_id, factor_type = self._totp_factor(password_result)
        continue_url, page_type = _auth_step_details(password_result)
        step(
            index,
            "协议凭据登录[2/6] 密码验证返回 · "
            f"page={page_type or '?'} · "
            f"TOTP因子={'已返回' if factor_id and factor_type == 'totp' else '缺失'}",
        )
        if page_type != "mfa_challenge" or not factor_id or factor_type != "totp":
            raise RuntimeError(
                f"password_verify_missing_totp_factor: page={page_type or '?'}"
            )
        mfa_referer = _absolute_auth_url(continue_url) or f"{auth_base}/mfa-challenge"
        session_dump = self._dump_client_auth_session(password_page, index)
        step(
            index,
            "协议凭据登录[3/6] "
            + (
                "认证会话同步完成，准备签发 TOTP challenge"
                if session_dump
                else "认证会话同步未返回数据，继续签发 TOTP challenge"
            ),
        )

        issue_url = f"{auth_base}/api/accounts/mfa/issue_challenge"
        self._post_auth_json(
            issue_url,
            password_page,
            {"id": factor_id, "type": "totp", "force_fresh_challenge": False},
            index,
            "mfa_issue_challenge",
        )
        step(index, "协议凭据登录[4/6] TOTP challenge 已签发 · factor_type=totp")
        code = totp_code(totp_secret)
        step(index, "协议凭据登录[5/6] TOTP 验证码已生成，提交 MFA 校验（验证码不写入日志）")
        verify_url = f"{auth_base}/api/accounts/mfa/verify"
        verified = self._post_auth_json(
            verify_url,
            mfa_referer,
            {"id": factor_id, "type": "totp", "code": code},
            index,
            "mfa_verify",
        )
        step(index, "协议凭据登录[6/6] MFA 校验通过，准备跟随 OAuth callback")
        callback_url, callback_page = _auth_step_details(verified)
        if not _is_chatgpt_callback_url(callback_url):
            raise RuntimeError(
                "mfa_verify_missing_callback: "
                f"page={callback_page or '?'} continue={_safe_url_for_log(callback_url)}"
            )
        callback_response = self._navigate_auth_step(
            callback_url,
            mfa_referer,
            index,
        )
        step(index, "OAuth callback 跟随完成，开始读取 ChatGPT 会话")
        final_url = str(getattr(callback_response, "url", "") or "")
        if not (_is_chatgpt_callback_url(callback_url) or _is_chatgpt_callback_url(final_url)):
            raise RuntimeError(
                "协议凭据登录未完成 OAuth callback: "
                f"continue={_safe_url_for_log(callback_url)} "
                f"final={_safe_url_for_log(final_url)}"
            )
        tokens = self._read_chatgpt_session(email, password, index)
        step(index, "ChatGPT 会话读取完成，AT/Refresh 会话校验通过")
        tokens["mfa_factor_id"] = factor_id
        tokens["mfa_factor_type"] = "totp"
        return tokens

    def change_password(
        self,
        email: str,
        old_password: str,
        new_password: str,
        totp_secret: str,
        index: int,
    ) -> dict[str, Any]:
        """Change an existing password through one re-auth transaction.

        The password-reset flag is attached to the initial NextAuth authorize
        request.  After password and TOTP verification, the reset POST is
        issued before following the OAuth callback, matching the browser HAR.
        """
        email = str(email or "").strip()
        old_password = str(old_password or "").strip()
        new_password = str(new_password or "").strip()
        totp_secret = str(totp_secret or "").strip()
        if not email:
            raise RuntimeError("change_password_missing_email")
        if not old_password or not new_password:
            raise RuntimeError("change_password_requires_old_and_new_password")
        if not totp_secret:
            raise RuntimeError("change_password_requires_totp_secret")
        if len(new_password) < 12:
            raise RuntimeError("change_password_requires_minimum_12_characters")
        if old_password == new_password:
            raise RuntimeError("change_password_new_password_matches_old")

        auth_start = self._complete_chatgpt_session(
            email,
            old_password,
            index,
            start_only=True,
            post_login_action="password_reset",
        )
        landed_url = str(auth_start.get("final_url") or "").strip()
        if _url_path(landed_url) != "/log-in/password":
            raise RuntimeError(
                "change_password_password_route_unavailable: "
                f"final={_safe_url_for_log(landed_url)}"
            )
        step(index, "改密[1/6] 已建立密码重置认证事务，提交旧密码")

        password_result = self._post_auth_json(
            f"{auth_base}/api/accounts/password/verify",
            f"{auth_base}/log-in/password",
            {"password": old_password},
            index,
            "password_verify",
        )
        factor_id, factor_type = self._totp_factor(password_result)
        continue_url, page_type = _auth_step_details(password_result)
        if page_type != "mfa_challenge" or not factor_id or factor_type != "totp":
            raise RuntimeError(
                f"change_password_missing_totp_factor: page={page_type or '?'}"
            )
        mfa_referer = _absolute_auth_url(continue_url) or f"{auth_base}/mfa-challenge"
        self._post_auth_json(
            f"{auth_base}/api/accounts/mfa/issue_challenge",
            f"{auth_base}/log-in/password",
            {"id": factor_id, "type": "totp", "force_fresh_challenge": False},
            index,
            "mfa_issue_challenge",
            flow_invocation=True,
        )
        code = _totp_code(totp_secret)
        verified = self._post_auth_json(
            f"{auth_base}/api/accounts/mfa/verify",
            mfa_referer,
            {"id": factor_id, "type": "totp", "code": code},
            index,
            "mfa_verify",
        )
        verify_url, verify_page = _auth_step_details(verified)
        step(
            index,
            "改密[2/6] MFA 校验通过，" 
            + ("进入新密码页面" if _url_path(verify_url) == "/reset-password/new-password" else "准备提交密码重置"),
        )

        # A successful MFA response normally points to the reset form.  Some
        # edge responses expose the callback first; in that case there is no
        # second reset POST to issue and we simply continue the callback.
        callback_url = ""
        if _url_path(verify_url) == "/reset-password/new-password" or verify_page in {
            "reset_password_new_password",
            "password_reset_new_password",
        }:
            reset_result = self._add_password(
                new_password,
                index,
                reset_existing=True,
            )
            callback_url, reset_page = _auth_step_details(reset_result)
            if not callback_url and reset_page == "reset_password_success":
                # The success page may require one explicit navigation before
                # the callback becomes available in the session redirect.
                success_url = f"{auth_base}/reset-password/success"
                success_response = self._navigate_auth_step(
                    success_url,
                    f"{auth_base}/reset-password/new-password",
                    index,
                )
                success_final = str(getattr(success_response, "url", "") or "")
                if _is_chatgpt_callback_url(success_final):
                    callback_url = success_final
        elif _is_chatgpt_callback_url(verify_url):
            callback_url = verify_url
        else:
            raise RuntimeError(
                "change_password_unexpected_mfa_step: "
                f"page={verify_page or '?'} continue={_safe_url_for_log(verify_url)}"
            )

        if callback_url:
            callback_response = self._navigate_auth_step(
                callback_url,
                f"{auth_base}/reset-password/new-password",
                index,
            )
            final_url = str(getattr(callback_response, "url", "") or "")
            if not (_is_chatgpt_callback_url(callback_url) or _is_chatgpt_callback_url(final_url)):
                raise RuntimeError(
                    "change_password_callback_not_followed: "
                    f"continue={_safe_url_for_log(callback_url)}"
                )
        tokens = self._read_chatgpt_session(email, new_password, index)
        tokens = dict(tokens or {})
        tokens.update(
            {
                "password": new_password,
                "password_set": True,
                "mfa_factor_id": factor_id,
                "mfa_factor_type": "totp",
                "totp_secret": totp_secret,
                "mfa_enabled": True,
            }
        )
        step(index, "改密完成，已读取新密码对应的 ChatGPT 会话")
        return tokens

    # Explicit aliases keep callers that use the UI terminology independent
    # of the internal password-reset endpoint name.
    reset_password_with_totp = change_password
    change_password_with_totp = change_password

    def _read_chatgpt_session(
        self,
        email: str,
        password: str,
        index: int,
    ) -> dict[str, Any]:
        fp = _browser_fingerprint(self.fingerprint)
        common = {
            "accept": "application/json",
            "accept-language": fp["accept_language"],
            "user-agent": fp["user_agent"],
            **_client_hint_headers(fp),
        }
        last_error = ""
        for attempt in range(3):
            session_url = f"{chatgpt_base}/api/auth/session?update={int(time.time() * 1000)}"
            session_headers = _headers_with_clearance(
                {**common, "referer": chatgpt_base + "/"},
                session_url,
                self.proxy,
                self.clearance_user_agent,
            )
            session_response, session_error = request_with_local_retry(
                self.session,
                "get",
                session_url,
                headers=session_headers,
                verify=_tls_verify(),
            )
            payload = _response_json(session_response)
            session_cookie = _nextauth_session_cookie(self.session)
            record = build_account_record(
                payload,
                email=email,
                password=password,
                proxy=self.proxy,
                session_cookie=session_cookie,
                browser_type=f"curl_cffi/{fp['impersonate']}",
            )
            access_token = str(record.get("access_token") or "").strip()
            account_id = str(record.get("account_id") or "").strip()
            if (
                session_response is not None
                and session_response.status_code == 200
                and access_token
                and session_cookie
                and account_id
            ):
                token_payload = parse_jwt_payload(access_token) or {}
                if current_runtime_settings().verify_chatgpt_me:
                    me_url = chatgpt_base + "/backend-api/me"
                    me_headers = _headers_with_clearance(
                        {
                            **common,
                            "authorization": f"Bearer {access_token}",
                            "origin": chatgpt_base,
                            "referer": chatgpt_base + "/",
                            "oai-device-id": self.device_id,
                        },
                        me_url,
                        self.proxy,
                        self.clearance_user_agent,
                    )
                    me_response, me_error = request_with_local_retry(
                        self.session,
                        "get",
                        me_url,
                        headers=me_headers,
                        verify=_tls_verify(),
                    )
                    if me_response is None or me_response.status_code != 200:
                        last_error = me_error or f"chatgpt_me_http_{getattr(me_response, 'status_code', 'unknown')}"
                        if attempt < 2:
                            time.sleep(1.0)
                            continue
                        break
                    step(
                        index,
                        "ChatGPT 会话验证完成 · "
                        f"Account ID {account_id[:12]}… client_id="
                        f"{str(token_payload.get('client_id') or '?')}",
                    )
                else:
                    step(
                        index,
                        "ChatGPT 会话已保存 · 跳过 /backend-api/me 快速验证",
                        "yellow",
                    )
                return {
                    "access_token": access_token,
                    "session_token": session_cookie,
                    "account_id": account_id,
                    "plan_type": str(record.get("plan_type") or "free"),
                    "expires_at": str(record.get("expires_at") or ""),
                    "session": payload,
                    "cookies": _cookie_values(self.session),
                    "profile": payload.get("account")
                    if isinstance(payload.get("account"), dict)
                    else {},
                    "oauth_client_id": str(token_payload.get("client_id") or ""),
                }
            else:
                missing = []
                if not access_token:
                    missing.append("access_token")
                if not session_cookie:
                    missing.append("session_token")
                if not account_id:
                    missing.append("account_id")
                last_error = session_error or "ChatGPT session 缺少 " + ", ".join(missing)
            if attempt < 2:
                time.sleep(1.0)
        raise RuntimeError(f"chatgpt_session_incomplete: {last_error[:300]}")

    def _chatgpt_mfa_request(
        self,
        method: str,
        path: str,
        access_token: str,
        index: int,
        payload: dict[str, Any] | None = None,
    ) -> tuple[Any, str]:
        token = str(access_token or "").strip()
        if not token:
            raise RuntimeError("mfa_missing_access_token")
        fp = _browser_fingerprint(self.fingerprint)
        headers = _header_fingerprint(
            {
                "accept": "application/json",
                "authorization": f"Bearer {token}",
                "referer": f"{chatgpt_base}/",
                "oai-device-id": self.device_id,
            },
            fp,
        )
        if payload is not None:
            headers["content-type"] = "application/json"
            headers["origin"] = chatgpt_base
        url = f"{chatgpt_base}{path}"
        headers = _headers_with_clearance(
            headers,
            url,
            self.proxy,
            str(getattr(self, "clearance_user_agent", "") or ""),
        )
        kwargs: dict[str, Any] = {
            "headers": headers,
            "verify": _tls_verify(),
        }
        if payload is not None:
            kwargs["json"] = payload
        return request_with_local_retry(self.session, method, url, **kwargs)

    def _enroll_totp(
        self,
        tokens: dict[str, Any],
        index: int,
        replace_existing: bool = False,
    ) -> dict[str, Any]:
        """Enroll and activate a TOTP factor.

        ``replace_existing`` follows the account-settings flow: disable the
        currently active in-house factor, then enroll and activate a fresh
        secret in the same authenticated ChatGPT session.  The default keeps
        the registration path's original add-only behavior.
        """
        access_token = str(tokens.get("access_token") or "").strip()
        replacing_active_factor = False
        info_response, info_error = self._chatgpt_mfa_request(
            "get",
            "/backend-api/accounts/mfa_info",
            access_token,
            index,
        )
        if info_response is None or info_response.status_code != 200:
            raise RuntimeError(
                info_error
                or f"mfa_info_http_{getattr(info_response, 'status_code', 'unknown')}"
            )
        initial_info = _response_json(info_response)
        if _truthy(initial_info.get("mfa_enabled")) or _truthy(
            initial_info.get("mfa_enabled_v2")
        ):
            if not replace_existing:
                raise RuntimeError("mfa_already_enabled_without_new_totp_secret")
            factors_payload = initial_info.get("factors")
            totp_factors = (
                factors_payload.get("totp")
                if isinstance(factors_payload, dict)
                else factors_payload
            )
            if not isinstance(totp_factors, list):
                totp_factors = []
            # ``factors.totp`` is an explicit TOTP collection, and some API
            # responses omit ``factor_type`` on its elements.  A bare/list
            # ``factors`` payload still requires an explicit type so a
            # passkey or another provider can never be selected implicitly.
            factors_are_explicit_totp = isinstance(factors_payload, dict) and isinstance(
                factors_payload.get("totp"), list
            )
            old_factor_id = ""
            for item in totp_factors:
                if not isinstance(item, dict) or _truthy(item.get("is_recovery")):
                    continue
                factor_type = str(
                    item.get("factor_type") or item.get("type") or ""
                ).strip().lower()
                if factor_type and factor_type != "totp":
                    continue
                if not factors_are_explicit_totp and factor_type != "totp":
                    continue
                old_factor_id = str(item.get("id") or "").strip()
                if old_factor_id:
                    break
            if not old_factor_id:
                raise RuntimeError("mfa_enabled_totp_factor_id_missing")
            if _STOP_CHECK.get()():
                raise JobCancelled("stop requested")
            disable_response, disable_error = self._chatgpt_mfa_request(
                "post",
                "/backend-api/accounts/mfa/user/disable_in_house",
                access_token,
                index,
                {"factor_id": old_factor_id},
            )
            if disable_response is None or disable_response.status_code != 200:
                detail = _response_json(disable_response)
                raise RuntimeError(
                    disable_error
                    or f"mfa_disable_http_{getattr(disable_response, 'status_code', 'unknown')}: "
                    f"{json.dumps(detail, ensure_ascii=False)[:240]}"
                )
            replacing_active_factor = True
            step(index, f"旧 TOTP 因子已停用 · factor_id={old_factor_id[:12]}…")
            # The settings UI refreshes mfa_info after disabling.  Confirm the
            # server has observed the transition before creating a replacement.
            disabled_response = None
            disabled_error = ""
            disabled_info: dict[str, Any] = {}
            # The disable endpoint is eventually consistent.  Match the UI's
            # observed confirmation window with 1s, 2s and 4s backoff (7s
            # total) while checking cancellation between short sleep chunks.
            disable_confirm_delays = (1.0, 2.0, 4.0)
            for disable_attempt in range(len(disable_confirm_delays) + 1):
                if _STOP_CHECK.get()():
                    raise JobCancelled("stop requested")
                disabled_response, disabled_error = self._chatgpt_mfa_request(
                    "get",
                    "/backend-api/accounts/mfa_info",
                    access_token,
                    index,
                )
                disabled_info = _response_json(disabled_response)
                if (
                    disabled_response is not None
                    and disabled_response.status_code == 200
                    and not (
                        _truthy(disabled_info.get("mfa_enabled"))
                        or _truthy(disabled_info.get("mfa_enabled_v2"))
                    )
                ):
                    break
                if disable_attempt < len(disable_confirm_delays):
                    delay = disable_confirm_delays[disable_attempt]
                    step(
                        index,
                        f"等待旧 TOTP 停用确认 · {delay:g}s 后重试",
                        "yellow",
                    )
                    remaining = delay
                    while remaining > 0:
                        if _STOP_CHECK.get()():
                            raise JobCancelled("stop requested")
                        chunk = min(0.25, remaining)
                        time.sleep(chunk)
                        remaining -= chunk
            if (
                disabled_response is None
                or disabled_response.status_code != 200
                or _truthy(disabled_info.get("mfa_enabled"))
                or _truthy(disabled_info.get("mfa_enabled_v2"))
            ):
                raise RuntimeError(disabled_error or "mfa_disable_not_confirmed")

        security_response, security_error = self._chatgpt_mfa_request(
            "get",
            "/backend-api/accounts/security_settings/info",
            access_token,
            index,
        )
        if security_response is None or security_response.status_code != 200:
            raise RuntimeError(
                security_error
                or "security_settings_info_http_"
                f"{getattr(security_response, 'status_code', 'unknown')}"
            )
        security_info = _response_json(security_response)
        if security_info.get("aas_eligible") is False:
            raise RuntimeError("mfa_enrollment_not_eligible")

        enroll_response, enroll_error = self._chatgpt_mfa_request(
            "post",
            "/backend-api/accounts/mfa/enroll",
            access_token,
            index,
            {"factor_type": "totp"},
        )
        if enroll_response is None or enroll_response.status_code != 200:
            raise RuntimeError(
                enroll_error
                or f"mfa_enroll_http_{getattr(enroll_response, 'status_code', 'unknown')}"
            )
        enrollment = _response_json(enroll_response)
        secret = str(enrollment.get("secret") or "").strip()
        session_id = str(enrollment.get("session_id") or "").strip()
        factor = enrollment.get("factor") if isinstance(enrollment.get("factor"), dict) else {}
        factor_id = str(factor.get("id") or "").strip()
        if not secret or not session_id or not factor_id:
            raise RuntimeError("mfa_enroll_incomplete_response")
        registration_trace = _REGISTRATION_TRACE.get()
        if registration_trace is not None:
            registration_trace.set_secrets(secret)
        _add_task_event_secrets(secret)

        activate_response = None
        activate_error = ""
        activate_data: dict[str, Any] = {}
        previous_code = ""
        for activate_attempt in range(2):
            code = _totp_code(secret)
            if activate_attempt and code == previous_code:
                for _ in range(32):
                    # After disabling the old factor this becomes a critical
                    # section: finish activating the replacement even if the
                    # batch receives a stop request, otherwise the account is
                    # left with MFA disabled and the stored secret invalid.
                    if _STOP_CHECK.get()() and not replacing_active_factor:
                        raise JobCancelled("stop requested")
                    time.sleep(1.0)
                    code = _totp_code(secret)
                    if code != previous_code:
                        break
            activate_response, activate_error = self._chatgpt_mfa_request(
                "post",
                "/backend-api/accounts/mfa/user/activate_enrollment",
                access_token,
                index,
                {
                    "code": code,
                    "factor_type": "totp",
                    "session_id": session_id,
                },
            )
            activate_data = _response_json(activate_response)
            activation_error_payload: Any = activate_data
            if not activation_error_payload and activate_response is not None:
                raw_activation_body = str(
                    getattr(activate_response, "text", "") or ""
                ).strip()
                if raw_activation_body:
                    try:
                        activation_error_payload = json.loads(raw_activation_body)
                    except Exception:
                        activation_error_payload = raw_activation_body
            if (
                activate_response is not None
                and activate_response.status_code == 200
                and activate_data.get("success") is True
            ):
                break
            # Depending on the edge and the current TOTP window, the API may
            # return HTTP 200 with ``success=false`` or HTTP 403 with an
            # ``invalid_code`` detail (sometimes encoded as JSON text).  Both
            # represent a retryable stale code; other 403 responses are hard
            # authorization failures.
            activation_error_text = _mfa_activation_error_text(
                activation_error_payload
            )
            retryable_invalid_code = (
                "invalid_code" in activation_error_text
                or "invalid code" in activation_error_text
            )
            if (
                activate_attempt == 0
                and activate_response is not None
                and (
                    activate_response.status_code == 200
                    or (
                        activate_response.status_code == 403
                        and retryable_invalid_code
                    )
                )
            ):
                previous_code = code
                step(
                    index,
                    "TOTP 激活码被拒绝，等待下一个验证码时间窗后重试",
                    "yellow",
                )
                continue
            break
        if (
            activate_response is None
            or activate_response.status_code != 200
            or activate_data.get("success") is not True
        ):
            detail = str(
                activate_data.get("error")
                or activate_data.get("detail")
                or "activation rejected"
            )
            raise RuntimeError(
                activate_error
                or f"mfa_activate_failed: {detail[:200]}"
            )

        final_response, final_error = self._chatgpt_mfa_request(
            "get",
            "/backend-api/accounts/mfa_info",
            access_token,
            index,
        )
        final_info = _response_json(final_response)
        final_factors = final_info.get("factors")
        final_factors_are_explicit_totp = isinstance(final_factors, dict) and isinstance(
            final_factors.get("totp"), list
        )
        totp_factors = (
            final_factors.get("totp", [])
            if isinstance(final_factors, dict)
            else final_factors
        )
        if not isinstance(totp_factors, list):
            totp_factors = []
        factor_confirmed = any(
            isinstance(item, dict)
            and str(item.get("id") or "") == factor_id
            and not _truthy(item.get("is_recovery"))
            and (
                final_factors_are_explicit_totp
                or str(item.get("factor_type") or item.get("type") or "")
                .strip()
                .lower()
                == "totp"
            )
            for item in totp_factors
        )
        if (
            final_response is None
            or final_response.status_code != 200
            or not (
                _truthy(final_info.get("mfa_enabled"))
                or _truthy(final_info.get("mfa_enabled_v2"))
            )
            or not factor_confirmed
        ):
            raise RuntimeError(
                final_error or "mfa_activation_not_confirmed"
            )
        step(index, "TOTP 2FA 已启用并完成复核")
        return {
            "mfa_enabled": True,
            "mfa_factor_type": "totp",
            "mfa_factor_id": factor_id,
            "totp_secret": secret,
        }

    def change_totp(
        self,
        email: str,
        password: str,
        current_totp_secret: str,
        index: int,
    ) -> dict[str, Any]:
        """Replace the account's TOTP factor after credential login."""
        email = str(email or "").strip()
        password = str(password or "").strip()
        current_totp_secret = str(current_totp_secret or "").strip()
        if not email or not password or not current_totp_secret:
            raise RuntimeError("change_totp_requires_email_password_and_current_totp")
        tokens = self.login_with_password_totp(
            email,
            password,
            current_totp_secret,
            index,
        )
        replacement = self._enroll_totp(
            tokens,
            index,
            replace_existing=True,
        )
        result = dict(tokens or {})
        result.update(replacement)
        result.update(
            {
                "email": email,
                "password": password,
                "password_set": True,
                "mfa_enabled": True,
            }
        )
        step(index, "TOTP 2FA 替换完成，已保存新密钥")
        return result

    # Alias used by a few older task payloads.
    replace_totp = change_totp
    rotate_totp = change_totp
    change_2fa = change_totp

    def _reset_auth_cookies(self) -> None:
        jar = getattr(self.session.cookies, "jar", self.session.cookies)
        for cookie in list(jar):
            domain = str(getattr(cookie, "domain", "") or "")
            if "auth.openai.com" not in domain and "chatgpt.com" not in domain:
                continue
            try:
                self.session.cookies.delete(
                    str(getattr(cookie, "name", "") or ""),
                    domain=domain,
                    path=str(getattr(cookie, "path", "/") or "/"),
                )
            except Exception:
                continue
        self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
        self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")

    def _authorize_continue_login(
        self,
        email: str,
        index: int,
        *,
        passwordless: bool = True,
        restart_invalid_state: bool = True,
    ) -> dict:
        step(
            index,
            (
                "提交邮箱进入 passwordless 登录验证"
                if passwordless
                else "提交邮箱作为登录账号标识（不读取邮箱验证码）"
            ),
        )
        url = f"{auth_base}/api/accounts/authorize/continue"

        def send():
            headers = self._json_headers(f"{auth_base}/log-in?usernameKind=email")
            headers["openai-sentinel-token"] = build_sentinel_token(
                self.session,
                self.device_id,
                "authorize_continue",
                self.fingerprint,
                lambda message: self._diagnostic(index, message),
            )
            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
            return request_with_local_retry(
                self.session,
                "post",
                url,
                json={"username": {"kind": "email", "value": email}},
                headers=headers,
                allow_redirects=False,
                verify=_tls_verify(),
            )

        resp, error = send()
        if (
            restart_invalid_state
            and resp is not None
            and resp.status_code == 409
        ):
            step(index, "登录会话过期，重新发起登录授权", "yellow")
            self._reset_auth_cookies()
            self._platform_authorize(email, index, screen_hint="login_or_signup")
            resp, error = send()
        if resp is None or resp.status_code != 200:
            detail = _response_json(resp) if resp is not None else {}
            raise RuntimeError(error or f"login_continue_http_{getattr(resp, 'status_code', 'unknown')}, detail={json.dumps(detail, ensure_ascii=False)[:300]}")
        data = _response_json(resp)
        if (
            passwordless
            and ((data.get("page") or {}).get("payload") or {}).get("passwordless_disabled")
        ):
            raise RuntimeError("当前账号不支持 passwordless 登录，请换邮箱或使用已有密码重登")
        return data

    def _send_passwordless_otp(self, index: int) -> None:
        step(index, "发送 passwordless 登录验证码")
        url = f"{auth_base}/api/accounts/passwordless/send-otp"
        headers = self._json_headers(f"{auth_base}/log-in/password")
        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "post", url, json={}, headers=headers, allow_redirects=False, verify=_tls_verify())
        if resp is None or resp.status_code not in (200, 201, 204):
            detail = _response_json(resp) if resp is not None else {}
            raise RuntimeError(error or f"passwordless_send_otp_http_{getattr(resp, 'status_code', 'unknown')}, detail={json.dumps(detail, ensure_ascii=False)[:300]}")

    @staticmethod
    def _is_passwordless_invalid_state(resp) -> bool:
        if resp is None or getattr(resp, "status_code", None) != 409:
            return False
        data = _response_json(resp)
        error = data.get("error") if isinstance(data, dict) else None
        if not isinstance(error, dict):
            return False
        code = str(error.get("code") or "").strip().lower()
        message = str(error.get("message") or "").strip().lower()
        return code == "invalid_state" or "sign-in session is no longer valid" in message

    def _complete_passwordless_login(self, code: str, index: int) -> dict:
        """Validate one passwordless OTP and exchange the resulting OAuth code."""
        resp, error = validate_otp(
            self.session,
            self.device_id,
            str(code or "").strip(),
            self.fingerprint,
            lambda message: self._diagnostic(index, message),
        )
        if resp is None or resp.status_code != 200:
            body = ""
            try:
                body = (resp.text or "")[:500] if resp is not None else ""
            except Exception:
                pass
            raise RuntimeError(
                error
                or f"passwordless_validate_otp_http_"
                f"{getattr(resp, 'status_code', 'unknown')}_body={body}"
            )
        data = _response_json(resp)
        continue_url = (
            str(data.get("continue_url") or "").strip()
            or f"{auth_base}/sign-in-with-chatgpt/platform/consent"
        )
        if _url_path(continue_url) == "/about-you":
            first_name, last_name = _random_name()
            step(index, "passwordless 登录验证完成，需要完善账号资料")
            self._create_account(
                f"{first_name} {last_name}",
                _random_birthdate(),
                index,
                require_platform_callback=True,
            )
            return self._exchange_registered_tokens(index)
        exchange_errors: list[str] = []
        tokens = exchange_tokens_from_continue_url(
            self.session,
            self.device_id,
            self.code_verifier,
            continue_url,
            self.proxy,
            self.clearance_user_agent,
            exchange_errors,
            self.fingerprint,
            getattr(self, "authorization_state", ""),
        )
        if not tokens:
            detail = (
                "；".join(exchange_errors[-4:])
                if exchange_errors
                else "未返回 token"
            )
            raise RuntimeError(f"passwordless token 换取失败: {detail}")
        return tokens

    def _passwordless_login(self, email: str, mailbox: dict, index: int) -> dict:
        provider = str(mailbox.get("provider") or "").strip().lower()
        if normalize_provider_type(provider) not in CANONICAL_PROVIDER_TYPES:
            raise RuntimeError(f"OpenAI 返回登录流，但邮箱来源不支持读取登录验证码: {provider or 'unknown'}")
        step(index, f"OpenAI 返回登录流，转入 passwordless 登录[{provider}]", "yellow")
        for attempt in range(2):
            if attempt:
                step(index, "登录会话失效，重新发起 passwordless 登录", "yellow")
                self._reset_auth_cookies()
                self._platform_authorize(email, index, screen_hint="login_or_signup")
            try:
                self._authorize_continue_login(email, index)
                mark_mailbox_code_boundary(mailbox)
                self._send_passwordless_otp(index)
            except RuntimeError as error:
                if attempt == 0 and "invalid_state" in str(error).lower():
                    continue
                raise
            step(index, "开始等待 passwordless 登录验证码")
            code = wait_for_code(mailbox, register_proxy=self.proxy)
            if not code:
                raise RuntimeError("等待 passwordless 登录验证码超时")
            step(index, "收到 passwordless 登录验证码")
            try:
                tokens = self._complete_passwordless_login(code, index)
                break
            except RuntimeError as error:
                if attempt == 0 and "invalid_state" in str(error).lower():
                    continue
                raise
        step(index, "passwordless token 换取完成")
        return tokens

    def login(self, email: str, mailbox: dict, index: int) -> dict[str, Any]:
        """Log in an existing account through the HAR-observed NextAuth flow."""
        step(index, "开始协议登录")
        self._complete_chatgpt_session(
            email,
            "",
            index,
            start_only=True,
        )
        mark_mailbox_code_boundary(mailbox)
        code = wait_for_code(mailbox, register_proxy=self.proxy)
        if not code:
            raise RuntimeError("协议登录验证码获取超时")

        validated = self._validate_otp(code, index)
        continue_url, _page_type = _auth_step_details(validated)
        if not continue_url:
            raise RuntimeError("协议登录 OTP 响应缺少 continue_url")

        response = self._navigate_auth_step(
            continue_url,
            f"{auth_base}/email-verification",
            index,
        )
        final_url = str(getattr(response, "url", "") or "")
        if not (_is_chatgpt_callback_url(continue_url) or _is_chatgpt_callback_url(final_url)):
            raise RuntimeError(
                "协议登录未完成 OAuth callback: "
                f"continue={_safe_url_for_log(continue_url)} "
                f"final={_safe_url_for_log(final_url)}"
            )
        tokens = self._read_chatgpt_session(email, "", index)
        step(index, "协议登录完成")
        return tokens

    def build_login_record(
        self,
        email: str,
        mailbox: dict[str, Any],
        tokens: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the same account-store shape as registration without a password."""
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        fp = _browser_fingerprint(self.fingerprint)
        return {
            "email": email,
            "password": "",
            "access_token": str(tokens.get("access_token") or "").strip(),
            "refresh_token": str(tokens.get("refresh_token") or "").strip(),
            "id_token": str(tokens.get("id_token") or "").strip(),
            "session_token": str(tokens.get("session_token") or "").strip(),
            "account_id": str(tokens.get("account_id") or "").strip(),
            "plan_type": str(tokens.get("plan_type") or "free").strip(),
            "expires_at": str(tokens.get("expires_at") or "").strip(),
            "session": tokens.get("session"),
            "cookies": tokens.get("cookies"),
            "profile": tokens.get("profile") or {},
            "oauth_client_id": str(tokens.get("oauth_client_id") or "").strip(),
            "auth_flow": "chatgpt_nextauth_login",
            "password_set": False,
            "status": "正常",
            "health_status": "alive",
            "health_checked_at": now,
            "health_error": "",
            "last_reauthenticated_at": now,
            "last_token_refresh_error": "",
            "source_type": str(mailbox.get("provider") or ""),
            "created_at": now,
            "registered_at": now,
            "login_at": now,
            "proxy": self.proxy,
            "registration_method": "protocol_login",
            "browser_type": f"curl_cffi/{fp['impersonate']}",
            "tls_impersonate": fp["impersonate"],
            "user_agent": fp["user_agent"],
            "sec_ch_ua": fp["sec_ch_ua"],
            "sec_ch_ua_full_version_list": fp["sec_ch_ua_full_version_list"],
            "platform_version": fp["platform_version"],
            "accept_language": fp["accept_language"],
            "locale": fp["accept_language"].split(",", 1)[0],
            "timezone": str(fp.get("timezone") or "Asia/Tokyo"),
            "timezone_offset_min": int(fp.get("timezone_offset_min") or 0),
            "device_id": self.device_id,
            "protocol_profile_id": str(fp.get("profile_id") or ""),
            "protocol_screen": str(fp.get("screen") or ""),
            "protocol_hardware_concurrency": str(
                fp.get("hardware_concurrency") or ""
            ),
            "protocol_device_memory": str(fp.get("device_memory") or ""),
            "protocol_platform": str(fp.get("platform") or "Win32"),
            "protocol_webgl_vendor": str(fp.get("webgl_vendor") or ""),
            "protocol_webgl_renderer": str(fp.get("webgl_renderer") or ""),
            "provider_type": str(mailbox.get("provider") or ""),
            "mailbox_provider_ref": str(mailbox.get("provider_ref") or ""),
        }

    def _register_user(self, email: str, password: str, index: int) -> dict[str, Any]:
        step(index, "开始提交注册密码")
        url = f"{auth_base}/api/accounts/user/register"
        headers = self._json_headers(f"{auth_base}/create-account/password")
        headers["openai-sentinel-token"] = build_sentinel_token(
            self.session,
            self.device_id,
            "username_password_create",
            self.fingerprint,
            lambda message: self._diagnostic(index, message),
        )
        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "post", url, json={"username": email, "password": password}, headers=headers, verify=_tls_verify())
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(url, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            headers = self._json_headers(f"{auth_base}/create-account/password")
            headers["openai-sentinel-token"] = build_sentinel_token(
                self.session,
                self.device_id,
                "username_password_create",
                self.fingerprint,
                lambda message: self._diagnostic(index, message),
            )
            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, "post", url, json={"username": email, "password": password}, headers=headers, verify=_tls_verify())
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code != 200:
            data = _response_json(resp) if resp is not None else {}
            if data.get("message") == "Failed to create account. Please try again.":
                step(index, "注册失败提示: 邮箱域名很可能因滥用被封禁，请更换邮箱域名", "yellow")
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(error or f"user_register_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        step(index, "提交注册密码完成")
        return _response_json(resp)

    def _set_registration_password(
        self,
        email: str,
        password: str,
        index: int,
    ) -> dict[str, Any]:
        """Submit the password and reject an application-level error response."""
        if not str(password or "").strip():
            raise RuntimeError("set_password_failed: generated password is empty")
        result = self._register_user(email, password, index)
        error_data = result.get("error")
        if error_data:
            code_value = (
                str(error_data.get("code") or "")
                if isinstance(error_data, dict)
                else str(error_data)
            )
            raise RuntimeError(f"set_password_failed: {code_value or error_data}")
        return result

    def _start_password_add_authorize(self, email: str, index: int) -> str:
        """Start the post-registration password-add authentication transaction."""
        fp = _browser_fingerprint(self.fingerprint)
        common = {
            "accept": "application/json",
            "accept-language": fp["accept_language"],
            "user-agent": fp["user_agent"],
            **_client_hint_headers(fp),
        }
        home_url = f"{chatgpt_base}/"
        home_headers = _headers_with_clearance(
            {**common, "accept": "text/html,application/xhtml+xml"},
            home_url,
            self.proxy,
            self.clearance_user_agent,
        )
        home, home_error = request_with_local_retry(
            self.session,
            "get",
            home_url,
            headers=home_headers,
            allow_redirects=True,
            verify=_tls_verify(),
        )
        if home is None or int(getattr(home, "status_code", 0) or 0) >= 400:
            raise RuntimeError(
                home_error
                or f"password_add_chatgpt_home_http_{getattr(home, 'status_code', 'unknown')}"
            )

        csrf_url = f"{chatgpt_base}/api/auth/csrf"
        csrf_headers = _headers_with_clearance(
            {**common, "referer": home_url},
            csrf_url,
            self.proxy,
            self.clearance_user_agent,
        )
        csrf_response, csrf_error = request_with_local_retry(
            self.session,
            "get",
            csrf_url,
            headers=csrf_headers,
            verify=_tls_verify(),
        )
        csrf_token = str(_response_json(csrf_response).get("csrfToken") or "").strip()
        if (
            csrf_response is None
            or csrf_response.status_code != 200
            or not csrf_token
        ):
            raise RuntimeError(
                csrf_error
                or f"password_add_chatgpt_csrf_http_{getattr(csrf_response, 'status_code', 'unknown')}"
            )

        def signin(params: dict[str, str]) -> Any:
            query = urlencode(params)
            signin_url = f"{chatgpt_base}/api/auth/signin/openai?{query}"
            signin_headers = _headers_with_clearance(
                {
                    **common,
                    "content-type": "application/x-www-form-urlencoded",
                    "origin": chatgpt_base,
                    "referer": home_url,
                },
                signin_url,
                self.proxy,
                self.clearance_user_agent,
            )
            with _PASSWORD_AUTH_FLOW_GATE:
                response, error = request_with_local_retry(
                    self.session,
                    "post",
                    signin_url,
                    headers=signin_headers,
                    data={
                        "callbackUrl": home_url,
                        "csrfToken": csrf_token,
                        "json": "true",
                    },
                    allow_redirects=False,
                    verify=_tls_verify(),
                )
                payload = _response_json(response)
                authorize_url = str(
                    payload.get("url")
                    or getattr(response, "headers", {}).get("Location")
                    or ""
                ).strip()
                if (
                    response is None
                    or response.status_code >= 400
                    or not authorize_url
                ):
                    raise RuntimeError(
                        error
                        or f"password_add_signin_http_{getattr(response, 'status_code', 'unknown')}"
                    )
                return self._navigate_auth_step(authorize_url, home_url, index)

        self._diagnostic(
            index,
            "启动注册后补密码授权 "
            "reauth=password post_login_add_password=true",
        )
        response = signin(
            {
                "connection": "password",
                "ext-oai-did": self.device_id,
                "login_hint": email,
                "max_age": "0",
                "post_login_add_password": "true",
                "reauth": "password",
            }
        )
        return str(getattr(response, "url", "") or "")

    def _send_password_reset_otp(
        self,
        index: int,
        *,
        referer_path: str = "/log-in/password",
    ) -> dict[str, Any]:
        """Request the email OTP used by the existing-password reset route.

        The auth service uses the page that initiated the transaction as the
        request referer. Keep the historical login-page default for callers
        that start there, while the HAR-observed forgot-password flow passes
        ``/reset-password`` explicitly.
        """
        step(index, "已有密码未知，开始发送密码重置验证码", "yellow")
        url = f"{auth_base}/api/accounts/password/send-otp"
        normalized_referer_path = str(referer_path or "/log-in/password").strip()
        if not normalized_referer_path.startswith("/"):
            normalized_referer_path = "/" + normalized_referer_path
        referer = f"{auth_base}{normalized_referer_path}"
        headers = _headers_with_clearance(
            self._json_headers(referer),
            url,
            self.proxy,
            self.clearance_user_agent,
        )
        headers["x-access-flow-invocation-id"] = str(uuid.uuid4())
        with _PASSWORD_AUTH_FLOW_GATE:
            resp, error = request_with_local_retry(
                self.session,
                "post",
                url,
                headers=headers,
                verify=_tls_verify(),
            )
        if resp is None or resp.status_code != 200:
            data = _response_json(resp) if resp is not None else {}
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(
                error
                or f"password_reset_send_otp_http_{getattr(resp, 'status_code', 'unknown')}{detail}"
            )
        step(index, "密码重置验证码已发送")
        return _response_json(resp)

    def _add_password(
        self,
        password: str,
        index: int,
        *,
        reset_existing: bool = False,
    ) -> dict[str, Any]:
        """Add a new password or reset an existing password after email OTP."""
        if not str(password or "").strip():
            raise RuntimeError("set_password_failed: generated password is empty")
        action = "重置" if reset_existing else "补充"
        step(index, f"开始{action}账号密码")
        endpoint = "reset" if reset_existing else "add"
        url = f"{auth_base}/api/accounts/password/{endpoint}"
        referer = f"{auth_base}/reset-password/new-password"

        def send():
            headers = self._json_headers(referer)
            headers["x-access-flow-invocation-id"] = str(uuid.uuid4())
            headers["openai-sentinel-token"] = build_sentinel_token(
                self.session,
                self.device_id,
                "password_reset",
                self.fingerprint,
                lambda message: self._diagnostic(index, message),
            )
            headers = _headers_with_clearance(
                headers, url, self.proxy, self.clearance_user_agent
            )
            with _PASSWORD_AUTH_FLOW_GATE:
                return request_with_local_retry(
                    self.session,
                    "post",
                    url,
                    json={"password": password},
                    headers=headers,
                    verify=_tls_verify(),
                )

        resp, error = send()
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(url, index)
            if bundle is None:
                raise RuntimeError(
                    _cloudflare_block_message(
                        resp, reason=self.clearance_failure_reason
                    )
                )
            resp, error = send()
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(
                    _cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截")
                )
        if resp is None or resp.status_code != 200:
            data = _response_json(resp) if resp is not None else {}
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            operation = "password_reset" if reset_existing else "password_add"
            raise RuntimeError(
                error
                or f"{operation}_http_{getattr(resp, 'status_code', 'unknown')}{detail}"
            )
        step(index, f"{action}账号密码完成")
        return _response_json(resp)

    def _complete_post_registration_password(
        self,
        email: str,
        password: str,
        mailbox: dict[str, Any],
        index: int,
        *,
        totp_secret: str = "",
        skip_existing_password: bool = False,
        credential_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> str:
        """Run the second OTP transaction used by the password-add HAR flow."""
        def reset_transaction_identity() -> None:
            self.device_id = str(uuid.uuid4())
            self._reset_auth_cookies()
            self._sync_session_identity()

        def start_with_retry(*, fresh: bool) -> str:
            if fresh:
                reset_transaction_identity()
            # A new password transaction may legitimately reuse the same
            # six-digit value. The timestamp boundary, rather than value
            # deduplication, separates it from the registration transaction.
            mark_mailbox_code_boundary(
                mailbox,
                grace_seconds=0,
                reset_seen_codes=True,
            )
            for authorize_attempt in range(2):
                try:
                    return self._start_password_add_authorize(email, index)
                except RuntimeError as error:
                    if authorize_attempt >= 1:
                        raise
                    if _auth_error_has_code(error, "rate_limit_exceeded"):
                        # Let the outer transaction apply the longer backoff;
                        # retrying here would issue six auth starts per account.
                        raise
                    reset_transaction_identity()
                    mark_mailbox_code_boundary(
                        mailbox,
                        grace_seconds=0,
                        reset_seen_codes=True,
                    )
                    delay = (
                        random.uniform(2.0, 4.0)
                        if _auth_error_has_code(error, "rate_limit_exceeded")
                        else 1.0
                    )
                    step(
                        index,
                        f"补密码授权启动失败，{delay:.1f}s 后重建认证事务",
                        "yellow",
                    )
                    time.sleep(delay)
            raise RuntimeError("password_authorize_start_exhausted")

        def run_transaction(
            *,
            fresh: bool,
            force_password_reset: bool,
        ) -> tuple[dict[str, Any], bool]:
            start_url = start_with_retry(fresh=fresh)
            reset_existing = (
                force_password_reset
                or _url_path(start_url) == "/log-in/password"
            )
            if reset_existing:
                self._diagnostic(
                    index,
                    (
                        "补密码授权识别到已有密码，注册任务直接跳过，"
                        "不发送密码重置验证码"
                        if skip_existing_password
                        else "补密码授权识别到已有密码，切换邮箱 OTP 密码重置协议"
                    ),
                )
                if skip_existing_password:
                    raise PasswordResetRequired(
                        f"existing_password_route={_url_path(start_url) or '/log-in/password'}",
                        email=email,
                    )
                self._send_password_reset_otp(
                    index,
                    referer_path="/reset-password",
                )

            validated: dict[str, Any] | None = None
            max_otp_attempts = 3
            for otp_attempt in range(max_otp_attempts):
                step(index, f"开始等待补密码验证码 ({otp_attempt + 1}/{max_otp_attempts})")
                code = wait_for_code(mailbox, register_proxy=self.proxy)
                if not code:
                    raise RuntimeError(
                        _otp_wait_timeout_message(mailbox, "补密码")
                    )
                if mailbox.pop("_post_registration_otp_fallback_used", False):
                    step(
                        index,
                        "补密码邮件未产生新验证码，复用本轮已验证的注册验证码",
                        "yellow",
                    )
                step(index, "收到补密码验证码")
                try:
                    validated = self._validate_otp(
                        code,
                        index,
                        allow_sentinel_retry=True,
                    )
                    break
                except RuntimeError as error:
                    if (
                        _is_wrong_email_otp_error(error)
                        and otp_attempt + 1 < max_otp_attempts
                    ):
                        step(
                            index,
                            "补密码验证码被服务端判定错误，已排除本次候选并继续等待新码",
                            "yellow",
                        )
                        continue
                    raise
            if not isinstance(validated, dict):
                raise RuntimeError("password_add_otp_validate_empty_response")
            continue_url, page_type = _auth_step_details(validated)
            self._diagnostic(
                index,
                "补密码 OTP 后认证分支 "
                f"page_type={page_type or '?'} continue={_safe_url_for_log(continue_url)}",
            )
            if _url_path(continue_url) != "/reset-password/new-password":
                raise RuntimeError(
                    "password_add_unexpected_auth_step: "
                    f"{_safe_url_for_log(continue_url)}"
                )
            # The browser HAR submits password/add directly after OTP
            # validation. Loading this page again can rotate the auth-session
            # cookies and make the immediately following POST fail invalid_state.
            password_result = (
                self._add_password(password, index, reset_existing=True)
                if reset_existing
                else self._add_password(password, index)
            )
            return password_result, reset_existing

        force_password_reset = False
        password_result: dict[str, Any] | None = None
        reset_existing = False
        for transaction_attempt in range(3):
            try:
                password_result, reset_existing = run_transaction(
                    fresh=transaction_attempt > 0,
                    force_password_reset=force_password_reset,
                )
                break
            except RuntimeError as error:
                invalid_state = _auth_error_has_code(error, "invalid_state")
                password_already_set = _auth_error_has_code(
                    error,
                    "password_already_set",
                )
                auth_rate_limited = _auth_error_has_code(
                    error,
                    "rate_limit_exceeded",
                )
                auth_error_page = _auth_error_has_code(
                    error,
                    "auth_step_error_page",
                )
                if transaction_attempt >= 2 or not (
                    invalid_state
                    or password_already_set
                    or auth_rate_limited
                    or auth_error_page
                ):
                    raise
                if password_already_set:
                    if skip_existing_password:
                        raise PasswordResetRequired(
                            "password_already_set", email=email
                        ) from error
                    force_password_reset = True
                    step(
                        index,
                        "账号密码已存在，正在重建认证事务并切换密码重置",
                        "yellow",
                    )
                elif auth_rate_limited:
                    delay = random.uniform(10.0, 14.0) * (2 ** transaction_attempt)
                    step(
                        index,
                        f"补密码授权触发频率限制，{delay:.1f}s 后重建认证事务",
                        "yellow",
                    )
                    time.sleep(delay)
                elif auth_error_page:
                    step(
                        index,
                        "补密码授权进入错误页，正在重建整段认证事务",
                        "yellow",
                    )
                else:
                    step(
                        index,
                        "补密码认证状态已失效，正在重建整段认证事务",
                        "yellow",
                    )
        if not isinstance(password_result, dict):
            raise RuntimeError("password_transaction_empty_response")
        error_data = password_result.get("error")
        if error_data:
            code_value = (
                str(error_data.get("code") or "")
                if isinstance(error_data, dict)
                else str(error_data)
            )
            raise RuntimeError(f"set_password_failed: {code_value or error_data}")
        if reset_existing:
            if str(totp_secret or "").strip():
                step(
                    index,
                    "密码重置已提交，正在用保存的 TOTP 验证新密码",
                    "yellow",
                )
                credential_tokens = self.login_with_password_totp(
                    email,
                    password,
                    totp_secret,
                    index,
                )
                if callable(credential_sink):
                    credential_sink(dict(credential_tokens or {}))
                return password
            next_url, next_page_type = _auth_step_details(password_result)
            normalized_next_page = (
                str(next_page_type or "").strip().lower().replace("-", "_")
            )
            if (
                normalized_next_page == "mfa_challenge"
                or "/mfa-challenge" in str(next_url or "").lower()
            ):
                raise RuntimeError(
                    "password_reset_existing_mfa_secret_missing: "
                    f"{_safe_url_for_log(next_url)}"
                )
            if next_url:
                self._navigate_auth_step(
                    next_url,
                    f"{auth_base}/reset-password/new-password",
                    index,
                )
            step(index, "密码重置完成，继续建立会话并创建 TOTP")
            return password
        next_url, next_page_type = _auth_step_details(password_result)
        normalized_next_page = str(next_page_type or "").strip().lower().replace("-", "_")
        if (
            str(totp_secret or "").strip()
            and (
                normalized_next_page == "mfa_challenge"
                or "/mfa-challenge" in str(next_url or "").lower()
            )
        ):
            step(
                index,
                "密码已提交，认证进入现有 MFA challenge，正在用保存的 TOTP 完成确认",
                "yellow",
            )
            credential_tokens = self.login_with_password_totp(
                email,
                password,
                totp_secret,
                index,
            )
            if callable(credential_sink):
                credential_sink(dict(credential_tokens or {}))
            return password
        if not next_url or not _is_chatgpt_callback_url(next_url):
            raise RuntimeError(
                "password_add_missing_callback: "
                f"{_safe_url_for_log(next_url)}"
            )
        self._navigate_auth_step(
            next_url,
            f"{auth_base}/reset-password/new-password",
            index,
        )
        return password

    def _send_otp(self, index: int) -> None:
        step(index, "开始发送验证码")
        url = f"{auth_base}/api/accounts/email-otp/send"
        headers = _headers_with_clearance(self._navigate_headers(f"{auth_base}/create-account/password"), url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "get", url, headers=headers, allow_redirects=True, verify=_tls_verify())
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(url, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            headers = _headers_with_clearance(self._navigate_headers(f"{auth_base}/create-account/password"), url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, "get", url, headers=headers, allow_redirects=True, verify=_tls_verify())
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code not in (200, 302):
            raise RuntimeError(error or f"send_otp_http_{getattr(resp, 'status_code', 'unknown')}")
        step(index, "发送验证码完成")

    def _validate_otp(
        self,
        code: str,
        index: int,
        *,
        allow_sentinel_retry: bool = True,
    ) -> dict[str, Any]:
        step(index, "开始校验验证码")
        resp, error = validate_otp(
            self.session,
            self.device_id,
            code,
            self.fingerprint,
            lambda message: self._diagnostic(index, message),
            allow_sentinel_retry=allow_sentinel_retry,
        )
        if resp is None or resp.status_code != 200:
            body = ""
            try:
                body = (resp.text or "")[:500] if resp is not None else ""
            except Exception:
                pass
            raise RuntimeError(error or f"validate_otp_http_{getattr(resp, 'status_code', 'unknown')}_body={body}")
        step(index, "验证码校验完成")
        return _response_json(resp)

    def _create_account(
        self,
        name: str,
        birthdate: str,
        index: int,
        *,
        require_platform_callback: bool = False,
    ) -> dict[str, Any]:
        step(index, "开始创建账号资料")
        url = f"{auth_base}/api/accounts/create_account"
        headers = self._json_headers(f"{auth_base}/about-you")

        # 使用新的 Sentinel 函数，同时获取 Sentinel Token 和 SO Token
        fp = _browser_fingerprint(self.fingerprint)
        sentinel_token, so_token, _oai_sc = build_sentinel_with_so_token(
            self.session,
            self.device_id,
            "oauth_create_account",
            user_agent=fp["user_agent"],
            sec_ch_ua=fp["sec_ch_ua"],
            diagnostic=lambda message: self._diagnostic(index, message),
            sid=str(fp.get("sentinel_sid") or ""),
            device_profile=fp,
        )
        _set_oai_sc_cookie(self.session, _oai_sc)

        headers["openai-sentinel-token"] = sentinel_token
        if so_token:
            headers["openai-sentinel-so-token"] = so_token

        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "post", url, json={"name": name, "birthdate": birthdate}, headers=headers, verify=_tls_verify())
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(url, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            headers = self._json_headers(f"{auth_base}/about-you")

            # 重新生成 Sentinel Token 和 SO Token
            sentinel_token, so_token, _oai_sc = build_sentinel_with_so_token(
                self.session,
                self.device_id,
                "oauth_create_account",
                user_agent=fp["user_agent"],
                sec_ch_ua=fp["sec_ch_ua"],
                diagnostic=lambda message: self._diagnostic(index, message),
                sid=str(fp.get("sentinel_sid") or ""),
                device_profile=fp,
            )
            _set_oai_sc_cookie(self.session, _oai_sc)

            headers["openai-sentinel-token"] = sentinel_token
            if so_token:
                headers["openai-sentinel-so-token"] = so_token

            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, "post", url, json={"name": name, "birthdate": birthdate}, headers=headers, verify=_tls_verify())
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code not in (200, 302):
            data = _response_json(resp) if resp is not None else {}
            create_error = data.get("error") if isinstance(data, dict) else None
            error_code = (
                str(create_error.get("code") or "").strip()
                if isinstance(create_error, dict)
                else str(create_error or "").strip()
            )
            if error_code in {
                "invalid_auth_step",
                "user_already_exists",
                "username_already_exists",
            }:
                step(
                    index,
                    f"创建账号资料返回可恢复状态: {error_code}",
                    "yellow",
                )
                return data
            if data.get("message") == "Failed to create account. Please try again.":
                step(index, "创建账号失败提示: 邮箱域名很可能因滥用被封禁，请更换邮箱域名", "yellow")
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(error or f"create_account_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        data = _response_json(resp)
        callback_params = (
            extract_oauth_callback_params_from_url(str(data.get("continue_url") or "").strip())
            or extract_oauth_callback_params_from_url(str(getattr(resp, "headers", {}).get("Location") or "").strip())
            or extract_oauth_callback_params_from_url(str(getattr(resp, "url", "") or "").strip())
        )
        if callback_params and not _callback_state_matches(
            callback_params, getattr(self, "authorization_state", "")
        ):
            raise RuntimeError("create_account_callback_state_mismatch")
        self.platform_auth_code = str((callback_params or {}).get("code") or "").strip()
        if require_platform_callback and not self.platform_auth_code:
            continue_hint = str(data.get("continue_url") or getattr(resp, "headers", {}).get("Location") or getattr(resp, "url", "") or "")
            raise RuntimeError(f"create_account_missing_callback: continue={_safe_url_for_log(continue_hint)}")
        step(index, "创建账号资料完成")
        return data

    def _exchange_registered_tokens(self, index: int) -> dict:
        step(index, "开始换 token")
        if not self.platform_auth_code:
            raise RuntimeError("token换取失败: 缺少 OAuth callback code")
        exchange_errors: list[str] = []
        tokens = request_platform_oauth_token(self.session, self.platform_auth_code, self.code_verifier, exchange_errors, self.fingerprint)
        if not tokens:
            detail = "；".join(exchange_errors[-3:]) if exchange_errors else "未返回 token"
            raise RuntimeError(f"token换取失败: {detail}")
        step(index, "token 换取完成")
        return tokens

    def _build_registration_record(
        self,
        email: str,
        password: str,
        tokens: dict[str, Any],
        mfa_result: dict[str, Any],
        source_type: str,
        mailbox: dict[str, Any],
        *,
        registration_state: str = "complete",
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        fp = _browser_fingerprint(self.fingerprint)
        return {
            "email": email,
            "password": password,
            "access_token": str(tokens.get("access_token") or "").strip(),
            "refresh_token": str(tokens.get("refresh_token") or "").strip(),
            "id_token": str(tokens.get("id_token") or "").strip(),
            "session_token": str(tokens.get("session_token") or "").strip(),
            "account_id": str(tokens.get("account_id") or "").strip(),
            "plan_type": str(tokens.get("plan_type") or "free").strip(),
            "expires_at": str(tokens.get("expires_at") or "").strip(),
            "session": tokens.get("session"),
            "cookies": tokens.get("cookies"),
            "profile": tokens.get("profile") or {},
            "oauth_client_id": str(tokens.get("oauth_client_id") or "").strip(),
            "auth_flow": "chatgpt_nextauth",
            "password_set": bool(password),
            "mfa_enabled": bool(mfa_result.get("mfa_enabled")),
            "mfa_factor_type": str(mfa_result.get("mfa_factor_type") or ""),
            "mfa_factor_id": str(mfa_result.get("mfa_factor_id") or ""),
            "totp_secret": str(mfa_result.get("totp_secret") or ""),
            "registration_state": registration_state,
            "source_type": source_type,
            "created_at": now,
            "registered_at": now,
            "proxy": self.proxy,
            "registration_method": "protocol",
            "browser_type": f"curl_cffi/{fp['impersonate']}",
            "tls_impersonate": fp["impersonate"],
            "user_agent": fp["user_agent"],
            "sec_ch_ua": fp["sec_ch_ua"],
            "sec_ch_ua_full_version_list": fp["sec_ch_ua_full_version_list"],
            "platform_version": fp["platform_version"],
            "accept_language": fp["accept_language"],
            "locale": fp["accept_language"].split(",", 1)[0],
            "timezone": str(fp.get("timezone") or "Asia/Tokyo"),
            "timezone_offset_min": int(fp.get("timezone_offset_min") or 0),
            "device_id": self.device_id,
            "protocol_profile_id": str(fp.get("profile_id") or ""),
            "protocol_screen": str(fp.get("screen") or ""),
            "protocol_hardware_concurrency": str(fp.get("hardware_concurrency") or ""),
            "protocol_device_memory": str(fp.get("device_memory") or ""),
            "protocol_platform": str(fp.get("platform") or "Win32"),
            "protocol_webgl_vendor": str(fp.get("webgl_vendor") or ""),
            "protocol_webgl_renderer": str(fp.get("webgl_renderer") or ""),
            "provider_type": str(mailbox.get("provider") or ""),
            "mailbox_provider_ref": str(mailbox.get("provider_ref") or ""),
        }

    def register(
        self,
        index: int,
        *,
        partial_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict:
        def ensure_running() -> None:
            if _STOP_CHECK.get()():
                raise JobCancelled("stop requested")

        ensure_running()
        self._log_identity(index)
        step(index, "开始创建邮箱")
        mailbox = create_mailbox(_FIXED_EMAIL.get(), register_proxy=self.proxy)
        email = str(mailbox.get("address") or "").strip()
        if not email:
            release_mailbox(mailbox)
            raise RuntimeError("邮箱服务未返回 address")
        label = str(mailbox.get("label") or "")
        step(index, f"邮箱创建完成[{label}]: {email}")
        try:
            password_candidate = _random_password()
            registration_trace = _REGISTRATION_TRACE.get()
            if registration_trace is not None:
                registration_trace.set_secrets(password_candidate)
            _add_task_event_secrets(password_candidate)
            password = ""
            mfa_result: dict[str, Any] = {}
            first_name, last_name = _random_name()
            birthdate = _random_birthdate()
            source_type = (
                "microsoft"
                if str(mailbox.get("provider") or "") == "outlook_token"
                else "web"
            )

            def persist_security_pending(
                snapshot_tokens: dict[str, Any] | None = None,
                *,
                announce: bool = False,
            ) -> None:
                if not callable(partial_sink) or not password:
                    return
                partial_sink(
                    self._build_registration_record(
                        email,
                        password,
                        snapshot_tokens or {},
                        {},
                        source_type,
                        mailbox,
                        registration_state="security_pending",
                    )
                )
                if announce:
                    step(
                        index,
                        "注册密码已保存，等待会话和 TOTP 2FA；中断后可继续补全安全字段",
                        "yellow",
                    )

            mark_mailbox_code_boundary(mailbox)
            mailbox["_registration_auth_started"] = True
            ensure_running()
            self._complete_chatgpt_session(
                email,
                "",
                index,
                start_only=True,
            )
            ensure_running()
            registration_password_result = self._set_registration_password(
                email,
                password_candidate,
                index,
            )
            # user/register returns the next auth step rather than the
            # password itself. Keep the generated credential locally, then
            # follow email-otp/send in the same session before validating OTP.
            # Test doubles from older callers may still return a string.
            password = (
                str(registration_password_result)
                if isinstance(registration_password_result, str)
                else password_candidate
            )
            registration_continue, registration_page_type = _auth_step_details(
                registration_password_result
                if isinstance(registration_password_result, dict)
                else {}
            )
            if registration_continue:
                self._diagnostic(
                    index,
                    "注册密码后认证步骤 "
                    f"page_type={registration_page_type or '?'} "
                    f"continue={_safe_url_for_log(registration_continue)}",
                )
                self._navigate_auth_step(
                    registration_continue,
                    f"{auth_base}/create-account/password",
                    index,
                )
            ensure_running()
            persist_security_pending(announce=True)
            validated: dict[str, Any] | None = None
            max_otp_attempts = 3
            for otp_attempt in range(max_otp_attempts):
                step(index, f"开始等待注册验证码 ({otp_attempt + 1}/{max_otp_attempts})")
                code = wait_for_code(mailbox, register_proxy=self.proxy)
                if not code:
                    ensure_running()
                    raise RuntimeError(
                        _otp_wait_timeout_message(mailbox, "注册")
                    )
                step(index, "收到注册验证码")
                try:
                    validated = self._validate_otp(
                        code,
                        index,
                        allow_sentinel_retry=True,
                    )
                    break
                except RuntimeError as error:
                    if _is_wrong_email_otp_error(error) and otp_attempt + 1 < max_otp_attempts:
                        step(
                            index,
                            "注册验证码被服务端判定错误，已排除本次候选并继续等待新码",
                            "yellow",
                        )
                        ensure_running()
                        continue
                    if "invalid_state" not in str(error).lower() or otp_attempt + 1 >= max_otp_attempts:
                        raise
                    step(index, "NextAuth 登录状态已失效，清理会话并重新发送验证码", "yellow")
                    self._reset_auth_cookies()
                    mark_mailbox_code_boundary(mailbox, grace_seconds=0)
                    self._complete_chatgpt_session(
                        email,
                        "",
                        index,
                        start_only=True,
                    )
                    ensure_running()
            if not isinstance(validated, dict):
                raise RuntimeError("email_otp_validate_empty_response")
            ensure_running()
            continue_url, page_type = _auth_step_details(validated)
            self._diagnostic(
                index,
                "OTP 后认证分支 "
                f"page_type={page_type or '?'} continue={_safe_url_for_log(continue_url)}",
            )
            normalized_page_type = str(page_type or "").strip().lower().replace("-", "_")
            if normalized_page_type == "mfa_challenge" or "/mfa-challenge" in str(
                continue_url or ""
            ).lower():
                step(
                    index,
                    "该邮箱已是启用 MFA 的现有账号，缺少原密码和 TOTP 密钥，已退出注册候选池",
                    "yellow",
                )
                raise RuntimeError(
                    "existing_account_mfa_credentials_missing: "
                    f"page={page_type or '?'} continue={_safe_url_for_log(continue_url)}"
                )

            # Some auth sessions expose the legacy password route before
            # about-you. The happy path now submits the registration password
            # before OTP; keep the older callback fallback for unusual states.
            if _auth_step_requires_password(continue_url, page_type):
                if (
                    _url_path(continue_url) == "/log-in/password"
                    or str(page_type or "").strip().lower() == "password_verification"
                ):
                    step(
                        index,
                        "验证码后进入已有密码认证，已跳过注册（未发送密码重置验证码）",
                        "yellow",
                    )
                    raise PasswordResetRequired("otp_password_route", email=email)
                if continue_url:
                    self._navigate_auth_step(
                        continue_url,
                        f"{auth_base}/email-verification",
                        index,
                    )
                ensure_running()
            elif _is_chatgpt_callback_url(continue_url):
                self._diagnostic(
                    index,
                    "OTP 已直接进入 ChatGPT 回调，沿用旧补密码兜底",
                )
                self._navigate_auth_step(
                    continue_url,
                    f"{auth_base}/email-verification",
                    index,
                )
                ensure_running()
            else:
                self._diagnostic(
                    index,
                    "OTP 已直接进入资料步骤，继续创建账号并读取会话",
                )
                if continue_url:
                    self._navigate_auth_step(
                        continue_url,
                        f"{auth_base}/email-verification",
                        index,
                    )
                    ensure_running()

            if _is_chatgpt_callback_url(continue_url):
                tokens = self._read_chatgpt_session(email, password, index)
                ensure_running()
                mailbox["_post_registration_otp_fallback"] = code
                password = self._complete_post_registration_password(
                    email,
                    password_candidate,
                    mailbox,
                    index,
                    skip_existing_password=True,
                )
                if registration_trace is not None:
                    registration_trace.set_secrets(password)
                _add_task_event_secrets(password)
                ensure_running()
                persist_security_pending(tokens, announce=True)
                tokens = self._read_chatgpt_session(email, password, index)
            else:
                create_result = self._create_account(
                    f"{first_name} {last_name}",
                    birthdate,
                    index,
                )
                ensure_running()
                create_error = create_result.get("error")
                if create_error:
                    error_code = (
                        str(create_error.get("code") or "")
                        if isinstance(create_error, dict)
                        else str(create_error)
                    )
                    if error_code not in {
                        "invalid_auth_step",
                        "user_already_exists",
                        "username_already_exists",
                    }:
                        raise RuntimeError(
                            f"create_account_failed: {error_code or create_error}"
                        )
                create_continue, _create_page_type = _auth_step_details(create_result)
                if create_continue:
                    self._navigate_auth_step(
                        create_continue,
                        f"{auth_base}/about-you",
                        index,
                    )
                ensure_running()
                tokens = self._read_chatgpt_session(email, password, index)
            persist_security_pending(tokens)
            if current_runtime_settings().enable_totp_after_register:
                mfa_result = self._enroll_totp(tokens, index)
            else:
                step(
                    index,
                    "协议注册已跳过即时 TOTP 2FA，账号以 security_pending 保存，后续可批量补全",
                    "yellow",
                )
            tokens["refresh_token"] = ""
            tokens["id_token"] = ""
        except JobCancelled:
            if mailbox.get("_registration_auth_started"):
                mark_mailbox_result(
                    mailbox,
                    success=False,
                    error="partial_registration_otp_already_sent",
                )
            else:
                release_mailbox(mailbox)
            raise
        except PasswordResetRequired as error:
            mark_mailbox_result(
                mailbox,
                success=False,
                error=PasswordResetRequired.code,
            )
            step(
                index,
                "检测到账号已有密码，已跳过注册（未发送密码重置验证码）",
                "yellow",
            )
            raise
        except Exception as error:
            mark_mailbox_result(mailbox, success=False, error=error)
            raise
        mark_mailbox_result(mailbox, success=True)
        final_state = (
            "complete"
            if current_runtime_settings().enable_totp_after_register
            else "security_pending"
        )
        return self._build_registration_record(
            email,
            password,
            tokens,
            mfa_result,
            source_type,
            mailbox,
            registration_state=final_state,
        )


def repair_security_one(
    index: int,
    *,
    account: dict[str, Any],
    mail_config: dict[str, Any],
    proxy: str = "",
    provider_type: str | None = None,
    stop_check: Callable[[], bool] | None = None,
    task_event=None,
    account_log_sink: Callable[[dict[str, Any]], None] | None = None,
    proxy_config: dict[str, Any] | None = None,
    partial_sink: Callable[[dict[str, Any]], None] | None = None,
    protocol_runtime: ProtocolRuntimeSettings | None = None,
    force_password_confirmation: bool = False,
) -> dict[str, Any]:
    """Complete browser security in one protocol session.

    Browser registration is followed by one email-code protocol login. The
    resulting session is used to set the password and enroll TOTP; the final
    AT is then read from that same session without a second password/TOTP
    login.
    """
    email = str(account.get("email") or "").strip()
    if not email:
        raise RuntimeError("安全字段补全缺少邮箱")

    existing_password = str(account.get("password") or "").strip()
    existing_totp = str(account.get("totp_secret") or "").strip()
    is_browser_registration = (
        str(account.get("registration_method") or "").strip().lower()
        == "browser"
    )
    # A browser registration may contain a generated candidate that was never
    # submitted. Only an explicit server-confirmed flag makes it reusable.
    needs_password = (
        bool(force_password_confirmation)
        or not existing_password
        or account.get("password_set") is not True
    )
    needs_totp = not existing_totp
    if not needs_password and not needs_totp:
        return {
            "password": existing_password,
            "password_set": True,
            "totp_secret": existing_totp,
            "mfa_enabled": bool(account.get("mfa_enabled")) or bool(existing_totp),
        }
    remote_mfa_enabled = _truthy(account.get("mfa_enabled"))

    stop = stop_check if callable(stop_check) else (lambda: False)
    task_event = _operation_event_forwarder(
        task_event,
        account_log_sink,
        operation="security_repair",
        index=index,
        email=email,
        secrets=(existing_password, existing_totp),
    )
    event_token = _TASK_EVENT.set(task_event if callable(task_event) else None)
    stop_token = _STOP_CHECK.set(stop)
    email_token = _FIXED_EMAIL.set(email)
    provider_token = _PROVIDER_TYPE.set(provider_type)
    mail_config_token = _MAIL_CONFIG.set(dict(mail_config))
    proxy_config_token = _PROXY_CONFIG.set(dict(proxy_config or {}))
    runtime_context = activate_runtime(protocol_runtime or ProtocolRuntimeSettings())
    runtime_context.__enter__()
    registrar: PlatformRegistrar | None = None
    mailbox: dict[str, Any] | None = None
    fields: dict[str, Any] = {}
    protocol_tokens: dict[str, Any] = {}
    early_totp_replaced = False

    def ensure_running() -> None:
        if stop():
            raise JobCancelled("stop requested")

    def save_partial(update: dict[str, Any]) -> None:
        fields.update(update)
        if callable(partial_sink):
            partial_sink(dict(update))

    # Password and MFA flows can refresh the authenticated protocol session.
    # Keep all credential/session fields in the same incremental persistence
    # path so a successful repair never leaves the account with stale tokens.
    protocol_token_keys = (
        "access_token",
        "refresh_token",
        "id_token",
        "session_token",
        "account_id",
        "plan_type",
        "expires_at",
        "session",
        "cookies",
        "profile",
        "oauth_client_id",
        "mfa_factor_id",
        "mfa_factor_type",
    )

    def save_protocol_tokens(tokens: dict[str, Any] | None = None) -> None:
        source = tokens if isinstance(tokens, dict) else protocol_tokens
        snapshot = {
            key: source[key]
            for key in protocol_token_keys
            if source.get(key) not in (None, "", {})
            and source.get(key) != fields.get(key, account.get(key))
        }
        if snapshot:
            save_partial(snapshot)

    def probe_remote_mfa(token_source: dict[str, Any] | None = None) -> None:
        """Refresh the remote MFA flag once an authenticated AT is available."""
        nonlocal remote_mfa_enabled
        if remote_mfa_enabled or is_browser_registration:
            return
        source = token_source if isinstance(token_source, dict) else protocol_tokens
        probe_token = str(
            source.get("access_token")
            or account.get("access_token")
            or ""
        ).strip()
        if not probe_token:
            return
        try:
            probe_response, _probe_error = registrar._chatgpt_mfa_request(
                "get",
                "/backend-api/accounts/mfa_info",
                probe_token,
                index,
            )
            probe_info = _response_json(probe_response)
            if (
                probe_response is not None
                and getattr(probe_response, "status_code", 0) == 200
                and isinstance(probe_info, dict)
            ):
                remote_mfa_enabled = _truthy(
                    probe_info.get("mfa_enabled")
                ) or _truthy(probe_info.get("mfa_enabled_v2"))
        except Exception:
            # Keep the normal enrollment path as the fallback when a probe is
            # unavailable or the endpoint is temporarily inconsistent.
            pass

    def merged_protocol_tokens(source: dict[str, Any] | None = None) -> dict[str, Any]:
        """Overlay freshly obtained credentials on the persisted account data."""
        merged = {
            key: account.get(key)
            for key in protocol_token_keys
            if account.get(key) not in (None, "", {})
        }
        if isinstance(source, dict):
            merged.update(
                {
                    key: value
                    for key, value in source.items()
                    if value not in (None, "", {})
                }
            )
        return merged

    def has_session_credentials(source: dict[str, Any] | None = None) -> bool:
        # A freshly returned AT must be accompanied by the session identity
        # from that same response. Do not combine a new AT with stale
        # session/account fields from the persisted record.
        if isinstance(source, dict) and str(
            source.get("access_token") or ""
        ).strip():
            if not all(
                str(source.get(key) or "").strip()
                for key in ("session_token", "account_id")
            ):
                return False
        merged = merged_protocol_tokens(source)
        return all(
            str(merged.get(key) or "").strip()
            for key in ("access_token", "session_token", "account_id")
        )

    def refresh_incomplete_session() -> None:
        """Fill session identity fields after an AT-only MFA operation."""
        password_for_session = str(
            fields.get("password") or existing_password or ""
        ).strip()
        if is_browser_registration or not password_for_session:
            return
        if has_session_credentials(protocol_tokens):
            return
        refreshed = registrar._read_chatgpt_session(
            email,
            password_for_session,
            index,
        )
        if not isinstance(refreshed, dict):
            raise RuntimeError("security_repair_session_empty_result")
        missing = [
            key
            for key in ("access_token", "session_token", "account_id")
            if not str(refreshed.get(key) or "").strip()
        ]
        if missing:
            raise RuntimeError(
                "security_repair_session_incomplete: " + ", ".join(missing)
            )
        protocol_tokens.update(refreshed)
        save_protocol_tokens(refreshed)

    try:
        ensure_running()
        if needs_password or is_browser_registration:
            mailbox = create_mailbox(
                email,
                register_proxy=proxy,
                allow_used=True,
            )
            if str(mailbox.get("address") or "").strip().lower() != email.lower():
                raise RuntimeError("安全字段补全邮箱池未返回目标邮箱")
        registrar = PlatformRegistrar(proxy)
        if is_browser_registration:
            ensure_running()
            step(index, "浏览器注册完成，开始协议邮箱登录")
            protocol_tokens = registrar.login(email, mailbox, index)
            if not isinstance(protocol_tokens, dict):
                raise RuntimeError("browser_protocol_login_empty_result")
        elif not needs_password:
            stored_cookies = account.get("cookies")
            if isinstance(stored_cookies, dict):
                for name, value in stored_cookies.items():
                    if str(name or "").strip() and value is not None:
                        try:
                            registrar.session.cookies.set(str(name), str(value))
                        except Exception:
                            continue

        # The persisted flag can lag behind the account-settings service. If
        # a local TOTP secret is missing, probe the authenticated session so a
        # remotely enabled factor is replaced before the password step.
        if needs_totp and not is_browser_registration and not remote_mfa_enabled:
            probe_remote_mfa()

        # If the server still has an active factor but the local secret is
        # missing, replace that factor before attempting a password reset.
        # The reset endpoint itself requires MFA when a factor exists, so
        # doing the password step first would strand the account with neither
        # a usable local secret nor a confirmed password.  An existing AT is
        # sufficient for the ChatGPT settings API and avoids asking for the
        # missing TOTP during this bootstrap phase.
        if (
            needs_totp
            and remote_mfa_enabled
            and not is_browser_registration
        ):
            early_tokens = dict(protocol_tokens) if protocol_tokens else {
                "access_token": str(account.get("access_token") or "").strip(),
                "session_token": str(account.get("session_token") or "").strip(),
                "account_id": str(account.get("account_id") or "").strip(),
            }
            if not str(early_tokens.get("access_token") or "").strip():
                raise RuntimeError(
                    "mfa_secret_missing_remote_factor_requires_access_token"
                )
            protocol_tokens.update(early_tokens)
            ensure_running()
            step(index, "本地缺少 TOTP，先替换远端旧因子再继续密码流程")
            replacement = registrar._enroll_totp(
                early_tokens,
                index,
                replace_existing=True,
            )
            save_partial(replacement)
            needs_totp = False
            early_totp_replaced = True
            existing_totp = str(
                fields.get("totp_secret") or replacement.get("totp_secret") or ""
            ).strip()

        if needs_password:
            password_flow_baseline = {
                key: str(
                    protocol_tokens.get(key)
                    or account.get(key)
                    or ""
                ).strip()
                for key in ("access_token", "session_token", "account_id")
            }
            password = _random_password()
            add_forwarder_secret = getattr(task_event, "add_secrets", None)
            if callable(add_forwarder_secret):
                add_forwarder_secret(password)
            password_kwargs: dict[str, Any] = {
                "totp_secret": str(
                    fields.get("totp_secret") or existing_totp
                ).strip()
            }
            # The password transaction may perform a TOTP login and return a
            # fresh AT/session even when no early MFA replacement is needed.
            # Always collect those credentials for persistence.
            password_kwargs["credential_sink"] = protocol_tokens.update
            password = registrar._complete_post_registration_password(
                email,
                password,
                mailbox or {},
                index,
                **password_kwargs,
            )
            # A callback may rotate only the AT while leaving the pre-password
            # session/account identifiers in place. Drop those stale values so
            # the complete-session refresh below cannot mix credential epochs.
            callback_access = str(
                protocol_tokens.get("access_token") or ""
            ).strip()
            callback_access_changed = bool(
                callback_access
                and callback_access != password_flow_baseline["access_token"]
            )
            if callback_access_changed:
                callback_session = str(
                    protocol_tokens.get("session_token") or ""
                ).strip()
                callback_account = str(
                    protocol_tokens.get("account_id") or ""
                ).strip()
                if (
                    not callback_session
                    or not callback_account
                    or callback_session == password_flow_baseline["session_token"]
                    or callback_account == password_flow_baseline["account_id"]
                ):
                    protocol_tokens.pop("session_token", None)
                    protocol_tokens.pop("account_id", None)
            if callable(add_forwarder_secret):
                add_forwarder_secret(password)
            ensure_running()
            confirmed_fields: dict[str, Any] = {
                "password": password,
                "password_set": True,
            }
            if force_password_confirmation:
                confirmed_fields["password_verified_at"] = datetime.now(
                    timezone.utc
                ).isoformat().replace("+00:00", "Z")
            save_partial(confirmed_fields)
            if not force_password_confirmation and (
                has_session_credentials(protocol_tokens)
                or not (
                    password_flow_baseline["session_token"]
                    or password_flow_baseline["account_id"]
                )
            ):
                save_protocol_tokens()

            # A password-add callback can complete without returning OAuth
            # credentials (for example when the account already has TOTP).
            # Do not report success with a password-only record: recover the
            # authenticated session and persist its AT/session identifiers.
            if (
                not is_browser_registration
                and not needs_totp
                and not has_session_credentials(protocol_tokens)
            ):
                refreshed = registrar._read_chatgpt_session(
                    email,
                    str(fields.get("password") or password or "").strip(),
                    index,
                )
                if not isinstance(refreshed, dict) or not str(
                    refreshed.get("access_token") or ""
                ).strip():
                    raise RuntimeError("security_repair_session_missing_access_token")
                else:
                    protocol_tokens.update(refreshed)
                    save_protocol_tokens(refreshed)

        if needs_totp:
            ensure_running()
            tokens = dict(protocol_tokens) if protocol_tokens else {
                "access_token": str(account.get("access_token") or "").strip(),
                "session_token": str(account.get("session_token") or "").strip(),
                "account_id": str(account.get("account_id") or "").strip(),
            }
            password_for_session = str(
                fields.get("password") or existing_password
            ).strip()
            # When only the local TOTP secret is missing, an existing AT is
            # sufficient for the ChatGPT settings API.  Avoid starting a new
            # password+TOTP login (which would require the very secret being
            # repaired) and use that token directly.
            can_use_stored_access_token = bool(
                str(tokens.get("access_token") or "").strip()
                and not is_browser_registration
            )
            needs_session_refresh = not has_session_credentials(tokens)
            if password_for_session and (
                not can_use_stored_access_token or needs_session_refresh
            ):
                try:
                    session_tokens = registrar._read_chatgpt_session(
                        email,
                        password_for_session,
                        index,
                    )
                except RuntimeError as session_error:
                    if is_browser_registration:
                        raise RuntimeError(
                            "browser_protocol_session_lost_after_password: "
                            f"{str(session_error)[:240]}"
                        ) from session_error
                    step(
                        index,
                        "现有 ChatGPT 会话已失效，使用邮箱 OTP 重建协议会话",
                        "yellow",
                    )
                    if mailbox is None:
                        mailbox = create_mailbox(
                            email,
                            register_proxy=proxy,
                            allow_used=True,
                        )
                    else:
                        mark_mailbox_code_boundary(mailbox, grace_seconds=0)
                    if registrar is not None:
                        registrar.close()
                    registrar = PlatformRegistrar(proxy)
                    session_tokens = registrar.login(email, mailbox, index)
                if str(session_tokens.get("access_token") or "").strip():
                    tokens = session_tokens
                    protocol_tokens.update(session_tokens)
                    # This is the recovery path for accounts that have no
                    # stored AT. Persist the newly reconstructed session before
                    # enrolling MFA so a later activation error remains
                    # recoverable with the fresh credentials.
                    if has_session_credentials(session_tokens):
                        save_protocol_tokens(session_tokens)
                    # The account flag can be stale. Probe again now that the
                    # OTP/password recovery produced an authenticated AT.
                    probe_remote_mfa(session_tokens)
            # A stale/local-missing secret can still be repaired when the
            # stored access token is active: the settings API allows the old
            # in-house factor to be disabled and a new one enrolled without
            # exposing the old secret.  Keep add-only behavior for accounts
            # whose remote MFA state is disabled.
            if remote_mfa_enabled:
                mfa = registrar._enroll_totp(
                    tokens,
                    index,
                    replace_existing=True,
                )
            else:
                mfa = registrar._enroll_totp(tokens, index)
            save_partial(mfa)

        # Replacing an already-enabled remote factor can finish with only an
        # AT in the local record. Fill the remaining session identity fields
        # before returning so Account ID/session are never silently lost.
        if not needs_totp:
            refresh_incomplete_session()

        # The browser AT is only a bootstrap credential. Read the final AT from
        # the authenticated protocol session after password/TOTP setup. This
        # does not perform another password + TOTP login.
        final_password = str(fields.get("password") or existing_password).strip()
        final_totp = str(fields.get("totp_secret") or existing_totp).strip()
        if is_browser_registration:
            if not final_password or not final_totp:
                raise RuntimeError("browser_security_session_missing_credentials")
            ensure_running()
            step(index, "密码和 TOTP 已完成，从当前协议会话读取最终 AT")
            refreshed = registrar._read_chatgpt_session(email, final_password, index)
            if not isinstance(refreshed, dict):
                raise RuntimeError("browser_security_session_empty_result")
            required_tokens = (
                "access_token",
                "session_token",
                "account_id",
            )
            missing_tokens = [
                key for key in required_tokens
                if not str(refreshed.get(key) or "").strip()
            ]
            if missing_tokens:
                raise RuntimeError(
                    "browser_security_session_incomplete: "
                    + ", ".join(missing_tokens)
                )
            token_fields: dict[str, Any] = {
                "access_token": str(refreshed.get("access_token") or "").strip(),
                "session_token": str(refreshed.get("session_token") or "").strip(),
                "account_id": str(refreshed.get("account_id") or "").strip(),
            }
            for key in ("plan_type", "expires_at", "oauth_client_id"):
                value = str(refreshed.get(key) or "").strip()
                if value:
                    token_fields[key] = value
            for key in ("session", "cookies", "profile"):
                value = refreshed.get(key)
                if value:
                    token_fields[key] = value
            # The session endpoint does not return OAuth refresh/id tokens;
            # retain the browser-minted renewable values when present.
            for key in ("refresh_token", "id_token"):
                value = str(refreshed.get(key) or account.get(key) or "").strip()
                if value:
                    token_fields[key] = value
            save_partial(token_fields)
            fields.setdefault("password", final_password)
            fields["password_set"] = True
            fields.setdefault("totp_secret", final_totp)
            fields["mfa_enabled"] = True
        elif force_password_confirmation:
            required_tokens = ("access_token", "session_token", "account_id")
            missing_tokens = [
                key for key in required_tokens
                if not str(protocol_tokens.get(key) or "").strip()
            ]
            if missing_tokens:
                raise RuntimeError(
                    "credential_confirmation_session_incomplete: "
                    + ", ".join(missing_tokens)
                )
            token_fields = {
                key: protocol_tokens[key]
                for key in (
                    "access_token",
                    "session_token",
                    "account_id",
                    "plan_type",
                    "expires_at",
                    "session",
                    "cookies",
                    "profile",
                    "oauth_client_id",
                    "mfa_factor_id",
                    "mfa_factor_type",
                )
                if protocol_tokens.get(key) not in (None, "", {})
                and protocol_tokens.get(key) != fields.get(key, account.get(key))
            }
            token_fields.update(
                {
                    "totp_secret": final_totp,
                    "mfa_enabled": True,
                    "totp_verified_at": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                }
            )
            save_partial(token_fields)
        if mailbox is not None:
            mark_mailbox_result(mailbox, success=True)
        if callable(task_event):
            task_event(index, "success", email=email, step="安全字段补全完成")
        return dict(fields)
    except JobCancelled:
        if mailbox is not None:
            release_mailbox(mailbox)
        if callable(task_event):
            task_event(
                index,
                "cancelled",
                email=email,
                step="安全字段补全已停止",
            )
        raise
    except Exception as error:
        if mailbox is not None:
            mark_mailbox_result(mailbox, success=False, error=error)
        if callable(task_event):
            task_event(
                index,
                "failed",
                email=email,
                error=_redact_operation_error(
                    error,
                    existing_password,
                    existing_totp,
                ),
                step="安全字段补全失败",
            )
        raise
    finally:
        if registrar is not None:
            registrar.close()
        runtime_context.__exit__(None, None, None)
        _PROXY_CONFIG.reset(proxy_config_token)
        _MAIL_CONFIG.reset(mail_config_token)
        _PROVIDER_TYPE.reset(provider_token)
        _FIXED_EMAIL.reset(email_token)
        _STOP_CHECK.reset(stop_token)
        _TASK_EVENT.reset(event_token)


def _validate_protocol_account_result(
    result: dict[str, Any],
    *,
    require_security_fields: bool = True,
) -> None:
    required = {
        "access_token": "Access Token",
        "session_token": "ChatGPT Session",
        "account_id": "Account ID",
    }
    missing = [label for key, label in required.items() if not str(result.get(key) or "").strip()]
    if missing:
        raise RuntimeError(
            "protocol_registration_incomplete: 缺少 " + ", ".join(missing)
        )
    access_token = str(result.get("access_token") or "").strip()
    payload = parse_jwt_payload(access_token)
    if not isinstance(payload, dict):
        raise RuntimeError("protocol_registration_incomplete: Access Token 不是有效 JWT")
    client_id = str(payload.get("client_id") or "").strip()
    if client_id != chatgpt_oauth_client_id:
        raise RuntimeError(
            "protocol_registration_wrong_oauth_client: "
            f"expected={chatgpt_oauth_client_id}, actual={client_id or 'missing'}"
        )
    auth_claim = payload.get("https://api.openai.com/auth")
    claim_account_id = (
        str(auth_claim.get("chatgpt_account_id") or "").strip()
        if isinstance(auth_claim, dict)
        else ""
    )
    if not claim_account_id:
        raise RuntimeError(
            "protocol_registration_incomplete: Access Token 缺少 chatgpt_account_id"
        )
    if claim_account_id != str(result.get("account_id") or "").strip():
        raise RuntimeError("protocol_registration_incomplete: Account ID 与 Token 不一致")
    if require_security_fields:
        security_missing = []
        if not str(result.get("password") or "").strip():
            security_missing.append("账号密码")
        if result.get("password_set") is not True:
            security_missing.append("密码设置状态")
        if not str(result.get("totp_secret") or "").strip():
            security_missing.append("TOTP 密钥")
        if result.get("mfa_enabled") is not True:
            security_missing.append("2FA 启用状态")
        if security_missing:
            raise RuntimeError(
                "protocol_registration_incomplete: 缺少 "
                + ", ".join(security_missing)
            )


def _validate_protocol_login_result(result: dict[str, Any]) -> None:
    """Validate a login result before it is persisted."""
    try:
        _validate_protocol_account_result(result, require_security_fields=False)
    except RuntimeError as error:
        message = str(error)
        if message.startswith("protocol_registration_"):
            message = "protocol_login_" + message[len("protocol_registration_") :]
        raise RuntimeError(message) from error


def login_with_password_totp_one(
    index: int,
    *,
    email: str,
    password: str,
    totp_secret: str,
    proxy: str = "",
    stop_check: Callable[[], bool] | None = None,
    task_event=None,
    error_sink: Callable[[str], None] | None = None,
    account_log_sink: Callable[[dict[str, Any]], None] | None = None,
    proxy_config: dict[str, Any] | None = None,
    protocol_runtime: ProtocolRuntimeSettings | None = None,
) -> dict[str, Any] | None:
    """Run one re-login using the stored password and TOTP secret."""
    start = time.time()
    stop = stop_check if callable(stop_check) else (lambda: False)
    task_event = _operation_event_forwarder(
        task_event,
        account_log_sink,
        operation="login",
        index=index,
        email=str(email or "").strip(),
        secrets=(password, totp_secret),
    )
    event_token = _TASK_EVENT.set(task_event if callable(task_event) else None)
    stop_token = _STOP_CHECK.set(stop)
    email_token = _FIXED_EMAIL.set(email)
    proxy_config_token = _PROXY_CONFIG.set(dict(proxy_config or {}))
    runtime_context = activate_runtime(
        protocol_runtime or ProtocolRuntimeSettings()
    )
    runtime_context.__enter__()
    registrar = None

    try:
        if not str(password or "").strip() or not str(totp_secret or "").strip():
            raise RuntimeError("协议凭据登录要求同时具备密码和 TOTP 2FA")
        registrar = PlatformRegistrar(proxy)
        if callable(task_event):
            task_event(
                index,
                "running",
                proxy=proxy_brief(proxy),
                email=str(email or "").strip(),
                step="密码 + TOTP 协议登录启动",
            )
        tokens = registrar.login_with_password_totp(
            str(email or "").strip(),
            password,
            totp_secret,
            index,
        )
        # Return the exact identity used for this session so reauthentication
        # can replace stale browser metadata from an older Chrome account.
        fp = _browser_fingerprint(registrar.fingerprint)
        tokens = dict(tokens or {})
        tokens.update(
            {
                "browser_type": f"curl_cffi/{fp['impersonate']}",
                "tls_impersonate": fp["impersonate"],
                "user_agent": fp["user_agent"],
                "sec_ch_ua": fp["sec_ch_ua"],
                "sec_ch_ua_full_version_list": fp["sec_ch_ua_full_version_list"],
                "platform_version": fp["platform_version"],
                "accept_language": fp["accept_language"],
                "locale": fp["accept_language"].split(",", 1)[0],
                "timezone": str(fp.get("timezone") or "Asia/Tokyo"),
                "timezone_offset_min": int(fp.get("timezone_offset_min") or 0),
                "protocol_screen": str(fp.get("screen") or ""),
                "protocol_hardware_concurrency": str(fp.get("hardware_concurrency") or ""),
                "protocol_device_memory": str(fp.get("device_memory") or ""),
                "protocol_platform": str(fp.get("platform") or "Win32"),
                "protocol_webgl_vendor": str(fp.get("webgl_vendor") or ""),
                "protocol_webgl_renderer": str(fp.get("webgl_renderer") or ""),
                "device_id": registrar.device_id,
                "protocol_profile_id": str(fp.get("profile_id") or ""),
            }
        )
        cost = time.time() - start
        log(f"{email} 密码 + TOTP 协议登录成功，本次耗时{cost:.1f}s", "green")
        if callable(task_event):
            task_event(
                index,
                "success",
                email=email,
                account_id=tokens.get("account_id"),
                step="密码 + TOTP 协议登录完成",
            )
        return tokens
    except JobCancelled:
        if callable(task_event):
            task_event(
                index,
                "cancelled",
                email=str(email or "").strip(),
                step="密码 + TOTP 协议登录已停止",
            )
        raise
    except Exception as error:
        safe_error = _redact_operation_error(error, password, totp_secret)
        if callable(error_sink):
            try:
                error_sink(safe_error)
            except Exception:
                pass
        cost = time.time() - start
        transport_error = is_proxy_transport_error(error)
        if transport_error:
            reason = _redact_operation_error(error, password, totp_secret, limit=240)
            log(
                f"任务{index} 当前代理传输失败，本次耗时{cost:.1f}s，"
                f"代理={proxy_brief(proxy)}，原因: {reason}；调度器将切换代理重试",
                "yellow",
            )
        else:
            log(f"任务{index} 密码 + TOTP 协议登录失败，本次耗时{cost:.1f}s，原因: {safe_error}", "red")
        if callable(task_event):
            task_event(
                index,
                "running" if transport_error and not stop() else "failed",
                **({"step": "当前代理连接失败，等待切换下一条代理"}
                   if transport_error and not stop()
                   else {"error": safe_error[:300], "step": safe_error[:160]}),
            )
        return None
    finally:
        if registrar is not None:
            registrar.close()
        runtime_context.__exit__(None, None, None)
        _PROXY_CONFIG.reset(proxy_config_token)
        _FIXED_EMAIL.reset(email_token)
        _STOP_CHECK.reset(stop_token)
        _TASK_EVENT.reset(event_token)


def _decorate_protocol_tokens(
    registrar: PlatformRegistrar,
    tokens: dict[str, Any] | None,
) -> dict[str, Any]:
    """Attach the selected protocol identity to maintenance results."""
    fp = _browser_fingerprint(registrar.fingerprint)
    result = dict(tokens or {})
    result.update(
        {
            "browser_type": f"curl_cffi/{fp['impersonate']}",
            "tls_impersonate": fp["impersonate"],
            "user_agent": fp["user_agent"],
            "sec_ch_ua": fp["sec_ch_ua"],
            "sec_ch_ua_full_version_list": fp["sec_ch_ua_full_version_list"],
            "platform_version": fp["platform_version"],
            "accept_language": fp["accept_language"],
            "locale": fp["accept_language"].split(",", 1)[0],
            "timezone": str(fp.get("timezone") or "Asia/Tokyo"),
            "timezone_offset_min": int(fp.get("timezone_offset_min") or 0),
            "protocol_screen": str(fp.get("screen") or ""),
            "protocol_hardware_concurrency": str(fp.get("hardware_concurrency") or ""),
            "protocol_device_memory": str(fp.get("device_memory") or ""),
            "protocol_platform": str(fp.get("platform") or "Win32"),
            "protocol_webgl_vendor": str(fp.get("webgl_vendor") or ""),
            "protocol_webgl_renderer": str(fp.get("webgl_renderer") or ""),
            "device_id": registrar.device_id,
            "protocol_profile_id": str(fp.get("profile_id") or ""),
        }
    )
    return result


def change_password_one(
    index: int,
    *,
    email: str,
    old_password: str,
    new_password: str,
    totp_secret: str,
    proxy: str = "",
    stop_check: Callable[[], bool] | None = None,
    task_event=None,
    error_sink: Callable[[str], None] | None = None,
    account_log_sink: Callable[[dict[str, Any]], None] | None = None,
    proxy_config: dict[str, Any] | None = None,
    protocol_runtime: ProtocolRuntimeSettings | None = None,
) -> dict[str, Any] | None:
    """Run one known-credential password change."""
    start = time.time()
    stop = stop_check if callable(stop_check) else (lambda: False)
    task_event = _operation_event_forwarder(
        task_event,
        account_log_sink,
        operation="password_change",
        index=index,
        email=str(email or "").strip(),
        secrets=(old_password, new_password, totp_secret),
    )
    event_token = _TASK_EVENT.set(task_event if callable(task_event) else None)
    stop_token = _STOP_CHECK.set(stop)
    email_token = _FIXED_EMAIL.set(str(email or "").strip())
    proxy_config_token = _PROXY_CONFIG.set(dict(proxy_config or {}))
    runtime_context = activate_runtime(protocol_runtime or ProtocolRuntimeSettings())
    runtime_context.__enter__()
    registrar: PlatformRegistrar | None = None
    try:
        if stop():
            raise JobCancelled("stop requested")
        registrar = PlatformRegistrar(proxy)
        if callable(task_event):
            task_event(
                index,
                "running",
                proxy=proxy_brief(proxy),
                email=str(email or "").strip(),
                step="修改密码任务启动",
            )
        result = registrar.change_password(
            str(email or "").strip(),
            old_password,
            new_password,
            totp_secret,
            index,
        )
        result = _decorate_protocol_tokens(registrar, result)
        if callable(task_event):
            task_event(
                index,
                "success",
                email=email,
                account_id=(result or {}).get("account_id") if isinstance(result, dict) else "",
                step="修改密码完成",
            )
        log(f"{email} 修改密码成功，本次耗时{time.time() - start:.1f}s", "green")
        return result
    except JobCancelled:
        if callable(task_event):
            task_event(
                index,
                "cancelled",
                email=str(email or "").strip(),
                step="修改密码已停止",
            )
        raise
    except Exception as error:
        safe_error = _redact_operation_error(
            error,
            old_password,
            new_password,
            totp_secret,
        )
        if callable(error_sink):
            try:
                error_sink(safe_error)
            except Exception:
                pass
        transport_error = is_proxy_transport_error(error)
        log(
            f"任务{index} 修改密码失败，本次耗时{time.time() - start:.1f}s，原因: {safe_error}",
            "yellow" if transport_error else "red",
        )
        if callable(task_event):
            task_event(
                index,
                "running" if transport_error and not stop() else "failed",
                **(
                    {"step": "当前代理连接失败，等待切换下一条代理"}
                    if transport_error and not stop()
                    else {"error": safe_error[:300], "step": safe_error[:160]}
                ),
            )
        return None
    finally:
        if registrar is not None:
            registrar.close()
        runtime_context.__exit__(None, None, None)
        _PROXY_CONFIG.reset(proxy_config_token)
        _FIXED_EMAIL.reset(email_token)
        _STOP_CHECK.reset(stop_token)
        _TASK_EVENT.reset(event_token)


def change_totp_one(
    index: int,
    *,
    email: str,
    password: str,
    totp_secret: str,
    proxy: str = "",
    stop_check: Callable[[], bool] | None = None,
    task_event=None,
    error_sink: Callable[[str], None] | None = None,
    account_log_sink: Callable[[dict[str, Any]], None] | None = None,
    proxy_config: dict[str, Any] | None = None,
    protocol_runtime: ProtocolRuntimeSettings | None = None,
) -> dict[str, Any] | None:
    """Run one TOTP replacement using the current password and TOTP."""
    start = time.time()
    stop = stop_check if callable(stop_check) else (lambda: False)
    task_event = _operation_event_forwarder(
        task_event,
        account_log_sink,
        operation="totp_change",
        index=index,
        email=str(email or "").strip(),
        secrets=(password, totp_secret),
    )
    event_token = _TASK_EVENT.set(task_event if callable(task_event) else None)
    stop_token = _STOP_CHECK.set(stop)
    email_token = _FIXED_EMAIL.set(str(email or "").strip())
    proxy_config_token = _PROXY_CONFIG.set(dict(proxy_config or {}))
    runtime_context = activate_runtime(protocol_runtime or ProtocolRuntimeSettings())
    runtime_context.__enter__()
    registrar: PlatformRegistrar | None = None
    try:
        if stop():
            raise JobCancelled("stop requested")
        registrar = PlatformRegistrar(proxy)
        if callable(task_event):
            task_event(
                index,
                "running",
                proxy=proxy_brief(proxy),
                email=str(email or "").strip(),
                step="修改 2FA 任务启动",
            )
        result = registrar.change_totp(
            str(email or "").strip(),
            password,
            totp_secret,
            index,
        )
        result = _decorate_protocol_tokens(registrar, result)
        if callable(task_event):
            task_event(
                index,
                "success",
                email=email,
                account_id=(result or {}).get("account_id") if isinstance(result, dict) else "",
                step="修改 2FA 完成",
            )
        log(f"{email} 修改 2FA 成功，本次耗时{time.time() - start:.1f}s", "green")
        return result
    except JobCancelled:
        if callable(task_event):
            task_event(
                index,
                "cancelled",
                email=str(email or "").strip(),
                step="修改 2FA 已停止",
            )
        raise
    except Exception as error:
        safe_error = _redact_operation_error(error, password, totp_secret)
        if callable(error_sink):
            try:
                error_sink(safe_error)
            except Exception:
                pass
        transport_error = is_proxy_transport_error(error)
        log(
            f"任务{index} 修改 2FA 失败，本次耗时{time.time() - start:.1f}s，原因: {safe_error}",
            "yellow" if transport_error else "red",
        )
        if callable(task_event):
            task_event(
                index,
                "running" if transport_error and not stop() else "failed",
                **(
                    {"step": "当前代理连接失败，等待切换下一条代理"}
                    if transport_error and not stop()
                    else {"error": safe_error[:300], "step": safe_error[:160]}
                ),
            )
        return None
    finally:
        if registrar is not None:
            registrar.close()
        runtime_context.__exit__(None, None, None)
        _PROXY_CONFIG.reset(proxy_config_token)
        _FIXED_EMAIL.reset(email_token)
        _STOP_CHECK.reset(stop_token)
        _TASK_EVENT.reset(event_token)


replace_totp_one = change_totp_one
change_password_known_one = change_password_one
change_password_known = change_password_one
rotate_totp_one = change_totp_one
change_2fa_one = change_totp_one


def login_one(
    index: int,
    *,
    mail_config: dict[str, Any],
    proxy: str = "",
    fixed_email: str | None = None,
    provider_type: str | None = None,
    stop_check: Callable[[], bool] | None = None,
    task_event=None,
    error_sink: Callable[[str], None] | None = None,
    proxy_config: dict[str, Any] | None = None,
    protocol_runtime: ProtocolRuntimeSettings | None = None,
) -> dict[str, Any] | None:
    """Run one standalone NextAuth protocol login and persist the session."""
    start = time.time()
    stop = stop_check if callable(stop_check) else (lambda: False)
    event_token = _TASK_EVENT.set(task_event if callable(task_event) else None)
    stop_token = _STOP_CHECK.set(stop)
    email_token = _FIXED_EMAIL.set(fixed_email)
    provider_token = _PROVIDER_TYPE.set(provider_type)
    mail_config_token = _MAIL_CONFIG.set(dict(mail_config))
    proxy_config_token = _PROXY_CONFIG.set(dict(proxy_config or {}))
    runtime_context = activate_runtime(
        protocol_runtime or ProtocolRuntimeSettings()
    )
    runtime_context.__enter__()
    registrar = None
    mailbox: dict[str, Any] | None = None
    try:
        mailbox = create_mailbox(
            _FIXED_EMAIL.get(),
            register_proxy=proxy,
            allow_used=True,
        )
        email = str(mailbox.get("address") or "").strip()
        if not email:
            raise RuntimeError("协议登录邮箱池未返回 address")
        registrar = PlatformRegistrar(proxy)
        if callable(task_event):
            task_event(
                index,
                "running",
                proxy=proxy_brief(proxy),
                email=email,
                step="协议登录任务启动",
            )
        tokens = registrar.login(email, mailbox, index)
        result = registrar.build_login_record(email, mailbox, tokens)
        _validate_protocol_login_result(result)
        mark_mailbox_result(mailbox, success=True)
        _append_account(result)
        cost = time.time() - start
        log(f"{email} 协议登录成功，本次耗时{cost:.1f}s", "green")
        if callable(task_event):
            task_event(index, "success", email=email, step="协议登录完成")
        return result
    except JobCancelled:
        if mailbox is not None:
            mark_mailbox_result(mailbox, success=False, error="stop requested")
        raise
    except Exception as error:
        if mailbox is not None:
            mark_mailbox_result(mailbox, success=False, error=error)
        if callable(error_sink):
            try:
                error_sink(str(error))
            except Exception:
                pass
        transport_error = is_proxy_transport_error(error)
        cost = time.time() - start
        if transport_error:
            log(
                f"任务{index} 当前代理传输失败，本次耗时{cost:.1f}s，调度器将切换代理重试",
                "yellow",
            )
        else:
            log(f"任务{index} 协议登录失败，本次耗时{cost:.1f}s，原因: {error}", "red")
        if callable(task_event):
            task_event(
                index,
                "running" if transport_error and not stop() else "failed",
                **({"step": "当前代理连接失败，等待切换下一条代理"}
                   if transport_error and not stop()
                   else {"error": str(error)[:300], "step": str(error)[:160]}),
            )
        return None
    finally:
        if registrar is not None:
            registrar.close()
        runtime_context.__exit__(None, None, None)
        _PROXY_CONFIG.reset(proxy_config_token)
        _MAIL_CONFIG.reset(mail_config_token)
        _PROVIDER_TYPE.reset(provider_token)
        _FIXED_EMAIL.reset(email_token)
        _STOP_CHECK.reset(stop_token)
        _TASK_EVENT.reset(event_token)


def register_one(
    index: int,
    *,
    mail_config: dict[str, Any],
    proxy: str = "",
    fixed_email: str | None = None,
    provider_type: str | None = None,
    stop_check: Callable[[], bool] | None = None,
    task_event=None,
    account_log_sink: Callable[[dict[str, Any]], None] | None = None,
    proxy_config: dict[str, Any] | None = None,
    group_id: str = "",
    post_register_health_check: bool = False,
    protocol_runtime: ProtocolRuntimeSettings | None = None,
) -> dict[str, Any] | None:
    start = time.time()
    stop = stop_check if callable(stop_check) else (lambda: False)
    trace = RegistrationTrace(
        index,
        proxy=proxy,
        provider=provider_type or "",
        fixed_email=fixed_email or "",
    )
    # RegistrationTrace paths are rooted at ``<repo>/data/registration_logs``;
    # use the known project root from the path itself to keep this module
    # independent of config-level ROOT imports.
    trace_file_ref = str(trace.path.relative_to(trace.path.parents[2]))
    trace_token = _REGISTRATION_TRACE.set(trace)

    def traced_task_event(event_index: int, status: str | None = None, **fields: Any) -> None:
        trace.event(status, **fields)
        if callable(task_event):
            task_event(event_index, status, **fields)

    # Keep the historical callback untouched while optionally projecting the
    # same events to the durable account-operation log.
    forwarded_task_event = _operation_event_forwarder(
        traced_task_event,
        account_log_sink,
        operation="registration",
        index=index,
        email=str(fixed_email or "").strip(),
    )
    emit_account_events = callable(task_event) or callable(account_log_sink)
    event_token = _TASK_EVENT.set(forwarded_task_event)
    stop_token = _STOP_CHECK.set(stop)
    email_token = _FIXED_EMAIL.set(fixed_email)
    provider_token = _PROVIDER_TYPE.set(provider_type)
    mail_config_token = _MAIL_CONFIG.set(dict(mail_config))
    proxy_config_token = _PROXY_CONFIG.set(dict(proxy_config or {}))
    runtime_context = activate_runtime(
        protocol_runtime or ProtocolRuntimeSettings()
    )
    runtime_context.__enter__()
    registrar = None
    partial_account: dict[str, Any] = {}

    def persist_partial(record: dict[str, Any]) -> None:
        """Keep password/session credentials when TOTP is the remaining step."""
        if not isinstance(record, dict):
            return
        partial_account.update(record)
        trace.set_secrets(
            partial_account.get("password"),
            partial_account.get("totp_secret"),
            partial_account.get("access_token"),
            partial_account.get("refresh_token"),
            partial_account.get("session_token"),
        )
        email = str(partial_account.get("email") or "").strip()
        if not email:
            return
        partial_account["group_id"] = str(group_id or "").strip()
        partial_account.setdefault("health_status", "unknown")
        partial_account.setdefault("health_checked_at", "")
        partial_account.setdefault("health_error", "")
        partial_account["registration_state"] = "security_pending"
        partial_account["updated_at"] = datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        try:
            upsert_account_record(dict(partial_account), ACCOUNTS_FILE)
        except Exception as error:
            log(
                f"{email} 中间账号状态保存失败，继续执行 2FA: {str(error)[:240]}",
                "yellow",
            )

    try:
        registrar = PlatformRegistrar(proxy)
        initial_device_id = registrar.device_id
        trace.identity(registrar.fingerprint, device_id=initial_device_id)
        if emit_account_events:
            forwarded_task_event(index, "running", proxy=proxy_brief(proxy), step="纯协议任务启动")
        step(index, "任务启动")
        result = registrar.register(index, partial_sink=persist_partial)
        _validate_protocol_account_result(result)
        final_device_id = str(result.get("device_id") or registrar.device_id or "").strip()
        result["protocol_initial_device_id"] = initial_device_id
        result["protocol_final_device_id"] = final_device_id
        trace.device_sync(initial_device_id=initial_device_id, final_device_id=final_device_id)
        result["group_id"] = str(group_id or "").strip()
        result["registration_trace_file"] = trace_file_ref
        trace.set_secrets(
            result.get("password"),
            result.get("totp_secret"),
            result.get("access_token"),
            result.get("refresh_token"),
            result.get("session_token"),
        )
        result.setdefault("health_status", "unknown")
        result.setdefault("health_checked_at", "")
        result.setdefault("health_error", "")
        cost = time.time() - start
        _append_account(result)
        if stop():
            log(f'{result["email"]} 注册已完成，停止后跳过测活，本次耗时{cost:.1f}s', "green")
            if emit_account_events:
                forwarded_task_event(
                    index,
                    "success",
                    email=result.get("email"),
                    account_id=result.get("account_id"),
                    trace_file=result.get("registration_trace_file"),
                    step="注册完成；已停止后续测活",
                )
            trace.finish(
                "success",
                email=result.get("email"),
                result=result,
                persist_operation=not callable(account_log_sink),
                secrets=tuple(result.get(key) for key in ("password", "totp_secret", "access_token", "refresh_token", "session_token")),
            )
            return result
        if post_register_health_check:
            checked = check_account(result)
            if checked.get("health_status") == "error":
                step(index, f"账号已保存，测活暂未成功: {checked.get('health_error')}", "yellow")
            result = checked
        else:
            step(index, "账号已保存；远程测活已延后", "green")
        with stats_lock:
            stats["done"] += 1
            stats["success"] += 1
            avg = (time.time() - float(stats.get("start_time") or start)) / stats["success"]
        log(f'{result["email"]} 注册成功，本次耗时{cost:.1f}s，全局平均每个号注册耗时{avg:.1f}s', "green")
        if emit_account_events:
            completion = (
                "注册完成并已测活"
                if post_register_health_check
                else "注册完成；远程测活已延后"
            )
            forwarded_task_event(
                index,
                "success",
                email=result.get("email"),
                account_id=result.get("account_id"),
                trace_file=result.get("registration_trace_file"),
                step=completion,
            )
        trace.finish(
            "success",
            email=result.get("email"),
            result=result,
            persist_operation=not callable(account_log_sink),
            secrets=tuple(result.get(key) for key in ("password", "totp_secret", "access_token", "refresh_token", "session_token")),
        )
        return result
    except PasswordResetRequired as error:
        cost = time.time() - start
        with stats_lock:
            stats["done"] += 1
            stats["skipped"] += 1
        log(
            f"任务{index} 检测到已有密码，已跳过注册，本次耗时{cost:.1f}s",
            "yellow",
        )
        if emit_account_events:
            forwarded_task_event(
                index,
                "skipped",
                email=error.email or None,
                group_id=str(group_id or "").strip(),
                skip_reason=PasswordResetRequired.code,
                trace_file=trace_file_ref,
                step="账号已有密码，已跳过；未执行密码重置",
            )
        trace.finish(
            "skipped",
            email=error.email,
            error=error,
            persist_operation=not callable(account_log_sink),
        )
        return None
    except Exception as e:
        cost = time.time() - start
        transport_error = is_proxy_transport_error(e)
        with stats_lock:
            if not transport_error:
                stats["done"] += 1
                stats["fail"] += 1
        if transport_error:
            log(
                f"任务{index} 当前代理传输失败，本次耗时{cost:.1f}s，调度器将切换代理重试",
                "yellow",
            )
        else:
            log(f"任务{index} 注册失败，本次耗时{cost:.1f}s，原因: {e}", "red")
        if emit_account_events:
            status = "cancelled" if stop() else ("running" if transport_error else "failed")
            fields = (
                {
                    "email": str(partial_account.get("email") or trace.email).strip(),
                    "trace_file": trace_file_ref,
                    "error": str(e)[:300],
                    "step": "当前代理连接失败，等待切换下一条代理",
                }
                if transport_error and not stop()
                else {
                    "email": str(partial_account.get("email") or trace.email).strip(),
                    "trace_file": trace_file_ref,
                    "error": str(e)[:300],
                    "step": str(e)[:160],
                }
            )
            forwarded_task_event(index, status, **fields)
        trace.finish(
            "transport_retry" if transport_error else "failed",
            email=str(partial_account.get("email") or trace.email).strip(),
            error=e,
            persist_operation=not callable(account_log_sink),
            secrets=tuple(partial_account.get(key) for key in ("password", "totp_secret", "access_token", "refresh_token", "session_token")),
        )
        return None
    finally:
        if registrar is not None:
            registrar.close()
        runtime_context.__exit__(None, None, None)
        _PROXY_CONFIG.reset(proxy_config_token)
        _MAIL_CONFIG.reset(mail_config_token)
        _PROVIDER_TYPE.reset(provider_token)
        _FIXED_EMAIL.reset(email_token)
        _STOP_CHECK.reset(stop_token)
        _TASK_EVENT.reset(event_token)
        _REGISTRATION_TRACE.reset(trace_token)


def worker(index: int) -> dict:
    result = register_one(
        index,
        mail_config=config["mail"],
        proxy=str(config.get("proxy") or ""),
        proxy_config=config.get("proxy_config"),
    )
    return {"ok": bool(result), "index": index, "result": result}


async def run_protocol_register(
    app_config: dict[str, Any],
    *,
    total: int | None = None,
    concurrency: int | None = None,
    fixed_email: str | None = None,
    provider_type: str | None = None,
    stop_check=None,
    pause_check=None,
    cancel_event: asyncio.Event | None = None,
    task_event=None,
    account_log_sink: Callable[[dict[str, Any]], None] | None = None,
    group_id: str = "",
    group_plan: list[str] | None = None,
    proxy_config_loader=None,
) -> list[dict[str, Any]]:
    """Run the copied chatgpt2api protocol registrar with this project's pools."""
    from mail import count_pool_capacity, release_stale_in_use

    reg_cfg = app_config.get("registration") if isinstance(app_config.get("registration"), dict) else {}
    source_mail = app_config.get("mail") if isinstance(app_config.get("mail"), dict) else {}
    mail_config = dict(source_mail)
    mail_config["wait_timeout"] = max(
        10,
        int(reg_cfg.get("protocol_otp_timeout") or source_mail.get("wait_timeout") or 90),
    )
    mail_config["wait_chunk"] = max(
        30,
        min(45, int(reg_cfg.get("protocol_otp_poll_chunk") or 45)),
    )
    mail_config["request_timeout"] = max(
        15,
        int(source_mail.get("request_timeout") or 30),
    )
    proxy_cfg = app_config.get("proxy") if isinstance(app_config.get("proxy"), dict) else {}
    stop = stop_check if callable(stop_check) else (lambda: False)
    paused = pause_check if callable(pause_check) else (lambda: False)
    n = max(1, int(total if total is not None else reg_cfg.get("total") or 1))
    conc = max(1, int(concurrency if concurrency is not None else reg_cfg.get("threads") or 1))
    max_retries = max(0, int(reg_cfg.get("max_transient_retries") or 0))
    post_register_health_check = _truthy(
        reg_cfg.get("post_register_health_check"), False
    )

    release_stale_in_use()
    capacity = count_pool_capacity(
        mail_config,
        provider_type=provider_type if str(provider_type or "auto").lower() not in {"", "auto", "any", "all"} else None,
        fixed_email=fixed_email,
        fixed_alias_count=1 if fixed_email else None,
    )
    available = int(capacity.get("available") or 0)
    available_emails = [
        str(email or "").strip().lower()
        for email in (capacity.get("emails") or [])
        if str(email or "").strip()
    ]
    fixed_alias_batch = bool(
        fixed_email
        and str(capacity.get("fixed_email_mode") or "") == "alias_batch"
    )
    if available <= 0:
        print(f"  [协议调度] 无可用邮箱 (池内={capacity.get('total')} used={capacity.get('used')})")
        return []
    if fixed_alias_batch:
        n = min(1, available)
        conc = 1
        print(
            f"  [协议调度] 指定主邮箱别名批次 → "
            f"单别名启动 总数={n} 并发={conc}"
        )
    elif fixed_email:
        n = conc = 1
    else:
        n = min(n, available)
        conc = min(conc, n, available)

    proxy_pool = resolve_proxy_pool(proxy_cfg)
    fallback_proxy = normalize_proxy_url(str(proxy_cfg.get("url") or "")) if proxy_cfg.get("enabled") else ""
    if not proxy_pool and fallback_proxy:
        proxy_pool = [fallback_proxy]
    rotate = bool(proxy_cfg.get("rotate_per_task", True))
    proxy_retry_limit = max(
        max_retries,
        min(5, max(0, len(proxy_pool) - 1)),
    )
    proxy_start_offset = (
        random.SystemRandom().randrange(len(proxy_pool))
        if rotate and proxy_pool
        else 0
    )
    proxy_refresh_lock = threading.Lock()
    proxy_refresh_key = (
        tuple(proxy_pool),
        fallback_proxy,
        rotate,
        bool(proxy_cfg.get("enabled")),
    )
    proxy_refresh_offset = proxy_start_offset

    def current_proxy_snapshot() -> tuple[dict[str, Any], list[str], str, bool, int]:
        """Read a newer panel proxy pool between attempts when available."""
        nonlocal proxy_refresh_key, proxy_refresh_offset
        latest_cfg = proxy_cfg
        if callable(proxy_config_loader):
            try:
                loaded = proxy_config_loader()
                if isinstance(loaded, dict):
                    latest_cfg = dict(loaded)
            except Exception as error:
                print(f"  [代理调度] 刷新代理配置失败，继续使用当前池: {str(error)[:160]}")

        enabled = bool(latest_cfg.get("enabled"))
        latest_pool = resolve_proxy_pool(latest_cfg) if enabled else []
        latest_fallback = (
            normalize_proxy_url(str(latest_cfg.get("url") or ""))
            if enabled
            else ""
        )
        if not latest_pool and latest_fallback:
            latest_pool = [latest_fallback]
        # A temporarily incomplete saved pool must not turn an active batch
        # into direct connections; keep the last known working pool instead.
        if enabled and not latest_pool and proxy_pool:
            latest_cfg = proxy_cfg
            latest_pool = list(proxy_pool)
            latest_fallback = fallback_proxy
            enabled = bool(proxy_cfg.get("enabled"))
        latest_rotate = bool(latest_cfg.get("rotate_per_task", True))
        key = (tuple(latest_pool), latest_fallback, latest_rotate, enabled)
        with proxy_refresh_lock:
            changed = key != proxy_refresh_key
            if changed:
                proxy_refresh_key = key
                proxy_refresh_offset = (
                    random.SystemRandom().randrange(len(latest_pool))
                    if latest_rotate and len(latest_pool) > 1
                    else 0
                )
            latest_offset = proxy_refresh_offset
        if changed:
            print(
                f"  [代理调度] 已刷新代理配置 · mode={str(latest_cfg.get('mode') or 'single_proxy')} "
                f"可用代理={len(latest_pool)} 轮换={latest_rotate}"
            )
        return latest_cfg, latest_pool, latest_fallback, latest_rotate, latest_offset
    protocol_runtime = ProtocolRuntimeSettings.from_config(reg_cfg)
    with stats_lock:
        stats.update(
            {"done": 0, "success": 0, "fail": 0, "skipped": 0, "start_time": time.time()}
        )

    print(f"  GPT 纯协议注册  总数={n} 并发={conc} 邮箱可用={available}")
    results: list[dict[str, Any] | None] = [None] * n
    planned_emails = available_emails[:n] if (not fixed_email or fixed_alias_batch) else []
    retry_emails = iter(
        available_emails[n:] if (not fixed_email or fixed_alias_batch) else []
    )
    retry_email_lock = threading.Lock()
    batch_bad_proxies: set[str] = set()
    batch_bad_proxy_lock = threading.Lock()
    schedule_emails = planned_emails or [str(fixed_email or "")] * n
    task_waves = build_registration_waves(
        schedule_emails,
        conc,
        fixed_alias_batch=fixed_alias_batch,
    )
    has_alias_pairs = any(is_pair for _indices, is_pair in task_waves)
    semaphore = asyncio.Semaphore(max(conc, 2 if has_alias_pairs else 1))
    if has_alias_pairs and not fixed_alias_batch:
        print("  [协议调度] 双别名按主邮箱分波执行；每对完整注册结束后再启动下一对")
        if callable(task_event):
            first_wave = set(task_waves[0][0]) if task_waves else set()
            for task_index, email in enumerate(schedule_emails, start=1):
                if task_index in first_wave:
                    continue
                task_group_id = (
                    str(group_plan[task_index - 1] or "").strip()
                    if isinstance(group_plan, list) and task_index <= len(group_plan)
                    else str(group_id or "").strip()
                )
                task_event(
                    task_index,
                    "queued",
                    email=email,
                    group_id=task_group_id,
                    step="等待前一双别名完整注册",
                )

    def claim_retry_email(current: str | None) -> str | None:
        """Reserve a spare slot without stealing another task's planned slot."""
        with retry_email_lock:
            return next(retry_emails, current)

    def bad_proxy_snapshot(local: set[str]) -> set[str]:
        with batch_bad_proxy_lock:
            return set(batch_bad_proxies) | set(local)

    def quarantine_proxy(proxy: str) -> int:
        if not proxy:
            return 0
        with batch_bad_proxy_lock:
            batch_bad_proxies.add(proxy)
            return len(batch_bad_proxies)

    def alias_proxy_retryable(detail: str) -> bool:
        text = str(detail or "").lower()
        if any(
            marker in text
            for marker in (
                "rate_limit_exceeded",
                "invalid_auth_step",
                "invalid_state",
                "password_add_http_409",
            )
        ):
            return False
        return any(
            marker in text
            for marker in (
                "curl: (7)",
                "curl: (28)",
                "curl: (35)",
                "curl: (56)",
                "tls connect error",
                "wrong_version_number",
                "could not resolve proxy",
                "proxy connect",
                "proxyerror",
                "connection reset by peer",
                "connection closed abruptly",
            )
        )

    def stopped() -> bool:
        return bool(stop()) or (
            cancel_event is not None and cancel_event.is_set()
        )

    def persist_registration_summary(
        *,
        status: str,
        email: str = "",
        account_id: str = "",
        trace_file: str = "",
        error: str = "",
        step_text: str = "",
        group_id_value: str = "",
    ) -> None:
        """Send one terminal registration projection to the batch sink.

        ``register_one`` may emit terminal callbacks for an individual
        attempt.  The batch adapter withholds those callbacks while retries
        are pending, so this function supplies the single final status after
        the scheduler has resolved the attempt sequence.
        """
        if not callable(account_log_sink):
            return
        payload: dict[str, Any] = {
            "status": status,
            "email": str(email or "").strip(),
            "account_id": str(account_id or "").strip(),
            "trace_file": str(trace_file or "").strip(),
            "error": str(error or "").strip(),
            "step": str(step_text or "").strip(),
            "group_id": str(group_id_value or "").strip(),
        }
        try:
            account_log_sink({key: value for key, value in payload.items() if value})
        except Exception:
            pass

    async def wait_until_resumed(index: int, task_group_id: str) -> bool:
        logged = False
        while paused():
            if stopped():
                return False
            if not logged and callable(task_event):
                task_event(
                    index,
                    "paused",
                    group_id=task_group_id,
                    step="任务派发已暂停",
                )
                logged = True
            await asyncio.sleep(0.25)
        if logged and callable(task_event):
            task_event(
                index,
                "queued",
                group_id=task_group_id,
                step="任务派发已继续",
            )
        return not stopped()

    async def run_task(
        index: int,
        start_barrier: asyncio.Barrier | None = None,
        paired_alias: bool = False,
    ) -> None:
        async with semaphore:
            task_group_id = (
                str(group_plan[index - 1] or "").strip()
                if isinstance(group_plan, list) and index <= len(group_plan)
                else str(group_id or "").strip()
            )
            task_email = (
                planned_emails[index - 1]
                if index <= len(planned_emails)
                else fixed_email
            )

            if not await wait_until_resumed(index, task_group_id):
                persist_registration_summary(
                    status="cancelled",
                    email=task_email or "",
                    step_text="已停止，未执行",
                    group_id_value=task_group_id,
                )
                if callable(task_event):
                    task_event(index, "cancelled", group_id=task_group_id, step="已跳过")
                return
            if callable(task_event):
                task_event(index, "queued", group_id=task_group_id, step="等待协议任务")
            record = None
            last_error = ""
            skipped = False
            trace_file = ""
            account_id = ""
            failed_proxies: set[str] = set()
            # ``register_one`` reports terminal callbacks for each individual
            # attempt. Keep only one running marker in the durable audit stream;
            # the final status is emitted by ``persist_registration_summary``
            # after all retries have been resolved.
            attempt_running_logged = False

            def attempt_account_log_sink(event: dict[str, Any] | None) -> None:
                nonlocal attempt_running_logged
                if not callable(account_log_sink) or not isinstance(event, dict):
                    return
                status = str(event.get("status") or "").strip().lower()
                if status != "running" or attempt_running_logged:
                    # Terminal and informational/step events remain in the live
                    # task stream and registration trace.
                    return
                attempt_running_logged = True
                try:
                    payload = dict(event)
                    if not str(payload.get("email") or "").strip() and task_email:
                        payload["email"] = str(task_email).strip()
                    account_log_sink(payload)
                except Exception:
                    # Audit persistence is observational and must not affect
                    # registration retries.
                    pass
            if start_barrier is not None:
                if callable(task_event):
                    task_event(
                        index,
                        "queued",
                        group_id=task_group_id,
                        email=task_email,
                        step="双别名已就绪，等待同步启动",
                    )
                try:
                    await asyncio.wait_for(start_barrier.wait(), timeout=3.0)
                except (asyncio.TimeoutError, asyncio.BrokenBarrierError):
                    if stopped():
                        persist_registration_summary(
                            status="cancelled",
                            email=task_email or "",
                            step_text="同步启动前已停止",
                            group_id_value=task_group_id,
                        )
                        return
                    raise RuntimeError("dual_alias_start_barrier_timeout")
            # Dead slots (remote-registered mailboxes) are retired as used and
            # never heal on retry, so they must not consume the transient
            # retry budget: the task keeps switching slots instead, bounded by
            # this hard cap.
            dead_slot_switch_limit = 10
            dead_slot_switches = 0
            for attempt in range(proxy_retry_limit + 1 + dead_slot_switch_limit):
                if not await wait_until_resumed(index, task_group_id):
                    break
                attempt_error = ""
                (
                    attempt_proxy_cfg,
                    attempt_proxy_pool,
                    attempt_fallback,
                    attempt_rotate,
                    attempt_offset,
                ) = current_proxy_snapshot()

                def attempt_event(
                    event_index: int,
                    status: str | None = None,
                    **fields: Any,
                ) -> None:
                    nonlocal attempt_error, last_error, skipped, trace_file, account_id
                    detail = str(fields.get("error") or "").strip()
                    if detail:
                        attempt_error = detail
                        last_error = detail
                    if status == "skipped" or str(fields.get("skip_reason") or "").strip() == PasswordResetRequired.code:
                        skipped = True
                    if fields.get("trace_file"):
                        trace_file = str(fields.get("trace_file") or "").strip()
                    if fields.get("account_id"):
                        account_id = str(fields.get("account_id") or "").strip()
                    if callable(task_event):
                        forwarded = dict(fields)
                        if is_proxy_transport_error(detail):
                            forwarded.pop("error", None)
                            forwarded["step"] = "当前代理连接失败，正在切换下一条代理"
                            status = "running"
                        task_event(event_index, status, **forwarded)

                proxy = _protocol_proxy_for_task(
                    attempt_proxy_pool,
                    index,
                    attempt,
                    rotate=attempt_rotate or bool(failed_proxies),
                    start_offset=attempt_offset,
                    retry_stride=max(1, conc),
                    excluded=bad_proxy_snapshot(failed_proxies),
                )
                if not proxy and attempt_fallback:
                    proxy = attempt_fallback
                task_runtime = protocol_runtime.with_deadline(
                    time.monotonic() + protocol_runtime.account_timeout
                )
                register_kwargs = {
                    "mail_config": mail_config,
                    "proxy": proxy,
                    "fixed_email": task_email,
                    "provider_type": provider_type,
                    "stop_check": stop,
                    "task_event": attempt_event,
                    "proxy_config": attempt_proxy_cfg,
                    "group_id": task_group_id,
                    "post_register_health_check": post_register_health_check,
                    "protocol_runtime": task_runtime,
                }
                # Do not add a new keyword for legacy integrations unless a
                # sink was explicitly requested.  This keeps monkey-patched
                # register_one implementations source-compatible.
                if callable(account_log_sink):
                    register_kwargs["account_log_sink"] = attempt_account_log_sink
                record = await asyncio.to_thread(
                    register_one,
                    index,
                    **register_kwargs,
                )
                if skipped:
                    break
                if str(attempt_error or "").lower().find(PasswordResetRequired.code) >= 0:
                    skipped = True
                    break
                if record:
                    break
                proxy_failed = is_proxy_transport_error(attempt_error)
                alias_pinned = fixed_alias_batch or paired_alias
                if alias_pinned and not alias_proxy_retryable(attempt_error):
                    break
                if proxy_failed and proxy:
                    failed_proxies.add(proxy)
                    cooled = quarantine_proxy(proxy)
                    print(
                        f"  [协议调度] 任务{index} 本批次冷却代理 "
                        f"{proxy_brief(proxy)} · 原因={attempt_error[:180]} · 冷却数={cooled}"
                    )
                if proxy_failed and attempt < proxy_retry_limit:
                    next_proxy = _protocol_proxy_for_task(
                        attempt_proxy_pool,
                        index,
                        attempt + 1,
                        rotate=True,
                        start_offset=attempt_offset,
                        retry_stride=max(1, conc),
                        excluded=bad_proxy_snapshot(failed_proxies),
                    )
                    print(
                        f"  [协议调度] 任务{index} 当前代理 TLS/连接失败，"
                        f"切换代理重试 {attempt + 2}/{proxy_retry_limit + 1}"
                    )
                    if callable(task_event):
                        task_event(
                            index,
                            "running",
                            group_id=task_group_id,
                            proxy=proxy_brief(next_proxy),
                            step=f"代理连接失败，已切换下一条代理重试 {attempt + 2}/{proxy_retry_limit + 1}",
                        )
                    await asyncio.sleep(0.5)
                    continue
                dead_slot_failure = is_dead_slot_error(attempt_error)
                if attempt >= max_retries and not (
                    dead_slot_failure
                    and dead_slot_switches < dead_slot_switch_limit
                ):
                    break
                if fixed_email is None and not proxy_failed and not paired_alias:
                    replacement = claim_retry_email(task_email)
                    if replacement and replacement != task_email:
                        if dead_slot_failure:
                            dead_slot_switches += 1
                        previous = task_email
                        task_email = replacement
                        print(
                            f"  [协议调度] 任务{index} 邮箱槽位失败，"
                            f"切换备用槽位 {task_email}"
                        )
                        if callable(task_event):
                            task_event(
                                index,
                                "running",
                                group_id=task_group_id,
                                email=task_email,
                                step=(
                                    "邮箱槽位失败，已切换备用别名"
                                    if "+" in task_email.rsplit("@", 1)[0]
                                    else "邮箱槽位失败，已切换备用邮箱"
                                ),
                                previous_email=previous,
                            )
                await asyncio.sleep(1.5)
            results[index - 1] = record
            if skipped:
                persist_registration_summary(
                    status="skipped",
                    email=task_email or "",
                    account_id=account_id,
                    trace_file=trace_file,
                    step_text="账号已有密码，已跳过",
                    group_id_value=task_group_id,
                )
                return
            if record:
                # ``register_one`` terminal callbacks are attempt-scoped and
                # deliberately withheld from the durable sink. Emit one
                # success marker only after the retry loop has returned a
                # validated record.
                persist_registration_summary(
                    status="success",
                    email=str(record.get("email") or task_email or ""),
                    account_id=str(record.get("account_id") or account_id or ""),
                    trace_file=str(
                        record.get("registration_trace_file") or trace_file or ""
                    ),
                    step_text="注册完成",
                    group_id_value=task_group_id,
                )
                return
            if not record:
                if stopped():
                    persist_registration_summary(
                        status="cancelled",
                        email=task_email or "",
                        account_id=account_id,
                        trace_file=trace_file,
                        step_text="已停止",
                        group_id_value=task_group_id,
                    )
                    if callable(task_event):
                        task_event(
                            index,
                            "cancelled",
                            group_id=task_group_id,
                            step="已停止",
                        )
                    return
                if is_proxy_transport_error(last_error):
                    with stats_lock:
                        stats["done"] += 1
                        stats["fail"] += 1
                if callable(task_event):
                    task_event(
                        index,
                        "failed",
                        group_id=task_group_id,
                        error=last_error or "protocol_register_failed",
                        step=(
                            f"代理重试用尽：{last_error[:140]}"
                            if is_proxy_transport_error(last_error)
                            else (last_error[:160] or "协议注册失败")
                        ),
                    )
                persist_registration_summary(
                    status="failed",
                    email=task_email or "",
                    account_id=account_id,
                    trace_file=trace_file,
                    error=last_error or "protocol_register_failed",
                    step_text=(
                        f"代理重试用尽：{last_error[:140]}"
                        if is_proxy_transport_error(last_error)
                        else (last_error[:160] or "协议注册失败")
                    ),
                    group_id_value=task_group_id,
                )

    for wave_number, (indices, is_alias_pair) in enumerate(task_waves, start=1):
        if stopped():
            break
        if is_alias_pair and wave_number > 1:
            print(
                f"  [协议调度] 上一双别名已完整结束，启动下一对 "
                f"({indices[0]}-{indices[-1]}/{n})"
            )
        barrier = asyncio.Barrier(2) if is_alias_pair and len(indices) == 2 else None
        await asyncio.gather(
            *(run_task(index, barrier, is_alias_pair) for index in indices)
        )
    return [item for item in results if item]


async def run_protocol_login(
    app_config: dict[str, Any],
    *,
    total: int | None = None,
    concurrency: int | None = None,
    fixed_email: str | None = None,
    provider_type: str | None = None,
    stop_check=None,
    cancel_event: asyncio.Event | None = None,
    task_event=None,
) -> list[dict[str, Any]]:
    """Run standalone ChatGPT NextAuth email-code logins."""
    from mail import release_stale_in_use

    reg_cfg = (
        app_config.get("registration")
        if isinstance(app_config.get("registration"), dict)
        else {}
    )
    source_mail = app_config.get("mail") if isinstance(app_config.get("mail"), dict) else {}
    mail_config = dict(source_mail)
    mail_config["wait_timeout"] = max(
        10,
        int(
            reg_cfg.get("protocol_login_otp_timeout")
            or reg_cfg.get("protocol_otp_timeout")
            or source_mail.get("wait_timeout")
            or 90
        ),
    )
    mail_config["wait_chunk"] = max(
        30,
        min(
            45,
            int(
                reg_cfg.get("protocol_login_poll_chunk")
                or reg_cfg.get("protocol_otp_poll_chunk")
                or 45
            ),
        ),
    )
    mail_config["request_timeout"] = max(
        15,
        int(source_mail.get("request_timeout") or 30),
    )
    proxy_cfg = app_config.get("proxy") if isinstance(app_config.get("proxy"), dict) else {}
    stop = stop_check if callable(stop_check) else (lambda: False)
    if not str(fixed_email or "").strip():
        raise ValueError("协议登录必须指定 fixed_email / --email，避免重复登录同一邮箱")
    n = conc = 1
    max_retries = max(0, int(reg_cfg.get("protocol_login_retries") or 1))

    release_stale_in_use()
    proxy_pool = resolve_proxy_pool(proxy_cfg)
    fallback_proxy = (
        normalize_proxy_url(str(proxy_cfg.get("url") or ""))
        if proxy_cfg.get("enabled")
        else ""
    )
    if not proxy_pool and fallback_proxy:
        proxy_pool = [fallback_proxy]
    rotate = bool(proxy_cfg.get("rotate_per_task", True))
    proxy_retry_limit = max(
        max_retries,
        min(5, max(0, len(set(proxy_pool)) - 1)),
    )
    protocol_runtime = ProtocolRuntimeSettings.from_config(reg_cfg)
    proxy_start_offset = (
        random.SystemRandom().randrange(len(proxy_pool))
        if rotate and proxy_pool
        else 0
    )

    print(f"  GPT 协议登录  总数={n} 并发={conc}")
    semaphore = asyncio.Semaphore(conc)
    results: list[dict[str, Any] | None] = [None] * n

    async def run_task(index: int) -> None:
        async with semaphore:
            if stop() or (cancel_event is not None and cancel_event.is_set()):
                if callable(task_event):
                    task_event(index, "cancelled", step="已跳过")
                return
            record = None
            last_error = ""
            failed_proxies: set[str] = set()

            def capture_event(_index, _status=None, **fields):
                nonlocal last_error
                detail = str(fields.get("error") or "").strip()
                if detail:
                    last_error = detail
                if callable(task_event):
                    forwarded = dict(fields)
                    transport_error = is_proxy_transport_error(detail)
                    if transport_error:
                        forwarded.pop("error", None)
                        forwarded["step"] = "当前代理连接失败，正在切换下一条代理"
                        _status = "running"
                    forwarded.setdefault("email", str(fixed_email or "").strip())
                    task_event(_index, _status, **forwarded)

            for attempt in range(proxy_retry_limit + 1):
                if stop() or (cancel_event is not None and cancel_event.is_set()):
                    break
                if failed_proxies and len(failed_proxies) >= len(set(proxy_pool)):
                    break
                proxy = _protocol_proxy_for_task(
                    proxy_pool,
                    index,
                    attempt,
                    rotate=rotate or bool(failed_proxies),
                    start_offset=proxy_start_offset,
                    excluded=failed_proxies,
                )
                task_runtime = protocol_runtime.with_deadline(
                    time.monotonic() + protocol_runtime.account_timeout
                )
                record = await asyncio.to_thread(
                    login_one,
                    index,
                    mail_config=mail_config,
                    proxy=proxy,
                    fixed_email=fixed_email,
                    provider_type=provider_type,
                    stop_check=stop,
                    task_event=capture_event,
                    error_sink=lambda detail: capture_event(index, "running", error=detail),
                    proxy_config=proxy_cfg,
                    protocol_runtime=task_runtime,
                )
                if record:
                    break
                if "account_deactivated" in last_error.lower():
                    break
                transport_error = is_proxy_transport_error(last_error)
                if transport_error and proxy:
                    failed_proxies.add(proxy)
                if transport_error and proxy and attempt < proxy_retry_limit:
                    if len(failed_proxies) >= len(set(proxy_pool)):
                        break
                    next_proxy = _protocol_proxy_for_task(
                        proxy_pool,
                        index,
                        attempt + 1,
                        rotate=True,
                        start_offset=proxy_start_offset,
                        excluded=failed_proxies,
                    )
                    print(
                        f"  [协议调度] 登录任务{index} 当前代理 TLS/连接失败，"
                        f"切换代理重试 {attempt + 2}/{proxy_retry_limit + 1}"
                    )
                    if callable(task_event):
                        task_event(
                            index,
                            "running",
                            email=str(fixed_email or "").strip(),
                            proxy=proxy_brief(next_proxy),
                            step=f"代理连接失败，已切换下一条代理重试 {attempt + 2}/{proxy_retry_limit + 1}",
                        )
                    await asyncio.sleep(0.5)
                    continue
                if attempt < max_retries:
                    await asyncio.sleep(1.5)
                else:
                    break
            results[index - 1] = record
            if not record and callable(task_event):
                task_event(
                    index,
                    "failed",
                    email=str(fixed_email or "").strip(),
                    error=last_error or "protocol_login_failed",
                    step=(
                        f"代理重试用尽：{last_error[:160]}"
                        if is_proxy_transport_error(last_error)
                        else (last_error[:180] or "协议登录失败")
                    ),
                )

    await asyncio.gather(*(run_task(i) for i in range(1, n + 1)))
    return [item for item in results if item]


def _protocol_mailbox_provider_map(mail_config: dict[str, Any]) -> dict[str, str]:
    """Index configured mailbox addresses so batch login can preflight them."""
    result: dict[str, str] = {}
    providers = mail_config.get("providers")
    if not isinstance(providers, list):
        return result
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        provider_type = normalize_provider_type(str(provider.get("type") or ""))
        if provider_type not in CANONICAL_PROVIDER_TYPES:
            continue
        lines = str(provider.get("mailboxes") or provider.get("pool") or "")
        if provider_type == "maddy_webui":
            parser = parse_domain_mailboxes
        else:
            parser = parse_icloud_credentials if provider_type == "icloud_api" else parse_outlook_credentials
        try:
            entries = parser(lines)
        except Exception:
            continue
        for entry in entries:
            email = str(entry.get("email") or "").strip().lower()
            if email:
                result.setdefault(email, provider_type)
    return result


def _protocol_relogin_candidates(
    accounts: list[dict[str, Any]],
    mailbox_providers: dict[str, str],
) -> tuple[list[tuple[dict[str, Any], str]], dict[str, int]]:
    """Select accounts with both stored credentials for direct protocol login."""
    from common.account_store import normalized_health_status

    candidates: list[tuple[dict[str, Any], str]] = []
    skipped = {
        "healthy_transient_error": 0,
        "account_deactivated": 0,
        "mailbox_missing": 0,
        "credentials_missing": 0,
        "unsupported_state": 0,
    }
    for account in accounts:
        state = normalized_health_status(account)
        account_status = str(account.get("status") or "").strip()
        email = str(account.get("email") or "").strip().lower()
        blocked = str(account.get("reauth_blocked_reason") or "").strip().lower()
        health_error = str(account.get("health_error") or "").strip().lower()
        if blocked == "account_deactivated" or "account_deactivated" in health_error:
            skipped["account_deactivated"] += 1
            continue

        needs_login = state in {"invalid", "reauth_required"}
        # A normal account with a transport error needs a health retry later,
        # not an OTP login that would rotate a valid session unnecessarily.
        if state == "error":
            needs_login = account_status == "异常"
            if not needs_login:
                skipped["healthy_transient_error"] += 1
        if not needs_login:
            skipped["unsupported_state"] += 1
            continue
        provider_type = mailbox_providers.get(
            email,
            str(account.get("provider_type") or "").strip(),
        )
        if not email or not str(account.get("password") or "").strip() or not str(account.get("totp_secret") or "").strip():
            skipped["credentials_missing"] += 1
            continue
        candidates.append((account, provider_type or ""))
    return candidates, skipped


async def run_protocol_relogin(
    app_config: dict[str, Any],
    *,
    total: int | None = None,
    concurrency: int | None = None,
    stop_check=None,
    cancel_event: asyncio.Event | None = None,
    task_event=None,
) -> dict[str, Any]:
    """Re-login all locally recorded accounts requiring a new session."""
    from common.account_store import load_accounts, upsert_account_record
    from mail import release_stale_in_use

    source_mail = app_config.get("mail") if isinstance(app_config.get("mail"), dict) else {}
    mail_config = dict(source_mail)
    reg_cfg = app_config.get("registration") if isinstance(app_config.get("registration"), dict) else {}
    mail_config["wait_timeout"] = max(
        10,
        int(
            reg_cfg.get("protocol_login_otp_timeout")
            or reg_cfg.get("protocol_otp_timeout")
            or source_mail.get("wait_timeout")
            or 90
        ),
    )
    mail_config["wait_chunk"] = max(
        30,
        min(
            45,
            int(
                reg_cfg.get("protocol_login_poll_chunk")
                or reg_cfg.get("protocol_otp_poll_chunk")
                or 45
            ),
        ),
    )
    mail_config["request_timeout"] = max(
        15,
        int(source_mail.get("request_timeout") or 30),
    )
    proxy_cfg = app_config.get("proxy") if isinstance(app_config.get("proxy"), dict) else {}
    stop = stop_check if callable(stop_check) else (lambda: False)
    release_stale_in_use()

    mailbox_providers = _protocol_mailbox_provider_map(mail_config)
    candidates, skipped = _protocol_relogin_candidates(load_accounts(), mailbox_providers)
    if total is not None:
        candidates = candidates[: max(0, int(total))]

    proxy_pool = resolve_proxy_pool(proxy_cfg)
    fallback_proxy = (
        normalize_proxy_url(str(proxy_cfg.get("url") or ""))
        if proxy_cfg.get("enabled")
        else ""
    )
    if not proxy_pool and fallback_proxy:
        proxy_pool = [fallback_proxy]
    rotate = bool(proxy_cfg.get("rotate_per_task", True))
    protocol_runtime = ProtocolRuntimeSettings.from_config(reg_cfg)
    proxy_start_offset = (
        random.SystemRandom().randrange(len(proxy_pool))
        if rotate and proxy_pool
        else 0
    )
    max_concurrency = max(1, int(concurrency if concurrency is not None else 1))
    max_retries = max(0, int(reg_cfg.get("protocol_login_retries") or 1))
    proxy_retry_limit = max(
        max_retries,
        min(5, max(0, len(set(proxy_pool)) - 1)),
    )
    results: dict[str, Any] = {
        "selected": len(candidates),
        "attempted": 0,
        "succeeded": 0,
        "failed": 0,
        "skipped": skipped,
        "success_emails": [],
        "failed_emails": [],
    }
    if not candidates:
        print("  GPT 协议批量重登录：没有匹配到待处理账号")
        return results

    print(
        f"  GPT 协议批量重登录  总数={len(candidates)} "
        f"并发={max_concurrency} 重试={max_retries}"
    )
    semaphore = asyncio.Semaphore(max_concurrency)
    result_lock = asyncio.Lock()

    async def run_task(index: int, account: dict[str, Any], provider_type: str) -> None:
        email = str(account.get("email") or "").strip()
        async with semaphore:
            async with result_lock:
                results["attempted"] += 1
            if stop() or (cancel_event is not None and cancel_event.is_set()):
                async with result_lock:
                    results["failed"] += 1
                    results["failed_emails"].append(email)
                if callable(task_event):
                    task_event(index, "cancelled", email=email, step="已跳过")
                return

            last_error = ""

            def capture_event(_index, _status=None, **fields):
                nonlocal last_error
                detail = str(fields.get("error") or "").strip()
                if detail and len(detail) >= len(last_error):
                    last_error = detail
                if callable(task_event):
                    forwarded = dict(fields)
                    if is_proxy_transport_error(detail):
                        forwarded.pop("error", None)
                        forwarded["step"] = "当前代理连接失败，正在切换下一条代理"
                        _status = "running"
                    forwarded.setdefault("email", email)
                    task_event(_index, _status, **forwarded)

            record = None
            failed_proxies: set[str] = set()

            for attempt in range(proxy_retry_limit + 1):
                if stop() or (cancel_event is not None and cancel_event.is_set()):
                    break
                if failed_proxies and len(failed_proxies) >= len(set(proxy_pool)):
                    break
                proxy = _protocol_proxy_for_task(
                    proxy_pool,
                    index,
                    attempt,
                    rotate=rotate or bool(failed_proxies),
                    start_offset=proxy_start_offset,
                    excluded=failed_proxies,
                )
                task_runtime = protocol_runtime.with_deadline(
                    time.monotonic() + protocol_runtime.account_timeout
                )
                record = await asyncio.to_thread(
                    login_with_password_totp_one,
                    index,
                    email=email,
                    password=str(account.get("password") or "").strip(),
                    totp_secret=str(account.get("totp_secret") or "").strip(),
                    proxy=proxy,
                    stop_check=stop,
                    task_event=capture_event,
                    error_sink=lambda detail: capture_event(index, "failed", error=detail),
                    proxy_config=proxy_cfg,
                    protocol_runtime=task_runtime,
                )
                if record:
                    break
                if "account_deactivated" in last_error.lower():
                    break
                transport_error = is_proxy_transport_error(last_error)
                if transport_error and proxy:
                    failed_proxies.add(proxy)
                if transport_error and proxy and attempt < proxy_retry_limit:
                    if len(failed_proxies) >= len(set(proxy_pool)):
                        break
                    next_proxy = _protocol_proxy_for_task(
                        proxy_pool,
                        index,
                        attempt + 1,
                        rotate=True,
                        start_offset=proxy_start_offset,
                        excluded=failed_proxies,
                    )
                    if callable(task_event):
                        task_event(
                            index,
                            "running",
                            email=email,
                            proxy=proxy_brief(next_proxy),
                            step=f"代理连接失败，已切换下一条代理重试 {attempt + 2}/{proxy_retry_limit + 1}",
                        )
                    await asyncio.sleep(0.5)
                    continue
                if attempt < max_retries:
                    await asyncio.sleep(1.5)
                else:
                    break

            async with result_lock:
                deactivated = "account_deactivated" in last_error.lower()
                if deactivated:
                    skipped["account_deactivated"] += 1
                elif record:
                    results["succeeded"] += 1
                    results["success_emails"].append(email)
                else:
                    results["failed"] += 1
                    results["failed_emails"].append(email)
            if deactivated:
                upsert_account_record(
                    {
                        **account,
                        "status": "已封号",
                        "health_status": "invalid",
                        "health_checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        "health_error": "account_deactivated: account has been deleted or deactivated",
                        "reauth_blocked_reason": "account_deactivated",
                        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    }
                )
                log(f"{email} 已标记为已封号，跳过后续重试", "yellow")
            if not record and last_error:
                if not deactivated:
                    log(f"{email} 批量重登录最终失败: {last_error[:240]}", "red")
            if not record and callable(task_event):
                task_event(
                    index,
                    "failed",
                    email=email,
                    error=last_error or "protocol_relogin_failed",
                    step=(
                        f"代理重试用尽：{last_error[:160]}"
                        if is_proxy_transport_error(last_error)
                        else (last_error[:180] or "协议重登录失败")
                    ),
                )

    await asyncio.gather(
        *(run_task(index, account, provider_type) for index, (account, provider_type) in enumerate(candidates, 1))
    )
    print(
        "  GPT 协议批量重登录完成 "
        f"selected={results['selected']} attempted={results['attempted']} "
        f"succeeded={results['succeeded']} failed={results['failed']} "
        f"deactivated={skipped['account_deactivated']} credentials_missing={skipped['credentials_missing']}"
    )
    return results

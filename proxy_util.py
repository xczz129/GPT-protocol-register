# -*- coding: utf-8 -*-
"""Proxy helpers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def normalize_proxy_url(url: str | None) -> str:
    url = (url or "").strip().strip("\ufeff").strip(",;")
    if not url:
        return ""
    if "://" not in url:
        if "##" in url:
            bits = [x.strip() for x in url.split("##")]
            if len(bits) >= 3 and ":" in bits[0]:
                host, port = bits[0].rsplit(":", 1)
                username, password = bits[1], bits[2]
                url = f"http://{username}:{password}@{host}:{port}"
        elif "@" in url:
            left, right = url.split("@", 1)
            if ":" in left and ":" in right:
                host, port = left.rsplit(":", 1)
                username, password = right.split(":", 1)
                if port.isdigit():
                    url = f"http://{username}:{password}@{host}:{port}"
            if "://" not in url:
                url = "http://" + url
        else:
            parts = url.split(":")
            if len(parts) == 4 and parts[1].isdigit():
                host, port, username, password = parts
                url = f"http://{username}:{password}@{host}:{port}"
            elif len(parts) == 4 and parts[3].isdigit():
                username, password, host, port = parts
                url = f"http://{username}:{password}@{host}:{port}"
            else:
                url = "http://" + url
    if re.match(r"^socks://", url, re.IGNORECASE):
        url = "socks5h" + url[5:]
    elif re.match(r"^socks4a?://", url, re.IGNORECASE):
        url = re.sub(r"^socks4a?://", "socks4://", url, count=1, flags=re.IGNORECASE)
    return url


def load_proxy_pool(proxy_cfg: dict[str, Any]) -> list[str]:
    pool_file = str(proxy_cfg.get("pool_file") or "").strip()
    if not pool_file:
        return []
    path = Path(pool_file).expanduser()
    try:
        raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        print(f"  [proxy] pool file unavailable: {path} ({exc})")
        return []
    proxies: list[str] = []
    seen: set[str] = set()
    for line in raw_lines:
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        proxy = normalize_proxy_url(value)
        if proxy and proxy not in seen:
            seen.add(proxy)
            proxies.append(proxy)
    return proxies


def resolve_proxy_pool(proxy_cfg: dict[str, Any]) -> list[str]:
    """Resolve the configured outbound mode into the proxies tasks may use."""
    if not isinstance(proxy_cfg, dict) or not proxy_cfg.get("enabled"):
        return []
    fallback = normalize_proxy_url(str(proxy_cfg.get("url") or ""))
    mode = str(proxy_cfg.get("mode") or "single_proxy").strip().lower()
    if mode == "country_pool":
        from common.registration_proxy_pool import proxies_for_country

        proxies = proxies_for_country(str(proxy_cfg.get("selected_country") or ""))
        return proxies or ([fallback] if fallback else [])
    if mode != "pool":
        return [fallback] if fallback else []
    proxies = load_proxy_pool(proxy_cfg)
    return proxies or ([fallback] if fallback else [])


def proxy_brief(proxy: str) -> str:
    value = str(proxy or "").strip()
    if not value:
        return "none"
    without_scheme = value.split("://", 1)[-1]
    if "@" in without_scheme:
        auth, host = without_scheme.rsplit("@", 1)
        username = auth.split(":", 1)[0] if auth else ""
        if len(username) > 22:
            username = f"{username[:14]}...{username[-6:]}"
        return f"{host} user={username}"
    return without_scheme


def is_proxy_transport_error(error: BaseException | str | None) -> bool:
    """Identify failures where retrying through another proxy is useful."""
    message = str(error or "").lower()
    if not message:
        return False
    exact_markers = (
        "curl: (28)",
        "curl: (35)",
        "tls connect error",
        "curl: (56) connection closed abruptly",
        "curl: (56) connect tunnel failed",
        "curl: (7)",
        "wrong_version_number",
        "err_no_supported_proxies",
        "could not resolve proxy",
        "proxy connect",
        "proxyerror",
        "response 407",
        "chatgpt_home_http_403",
        "cf_blocked",
        "rate_limit_exceeded",
    )
    if any(marker in message for marker in exact_markers):
        return True
    return any(
        marker in message
        for marker in (
            "curl: (56) recv failure",
            "connection reset by peer",
            "connection closed abruptly",
            "broken pipe",
        )
    ) and ("proxy" in message or "curl" in message)

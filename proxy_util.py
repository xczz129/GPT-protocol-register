# -*- coding: utf-8 -*-
"""Proxy helpers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote


def _encode_proxy_credential(value: object) -> str:
    """Percent-encode proxy credentials for the URL userinfo component.

    代理商的地域参数会带空格（如 ``city-Bernau bei Berlin``），直接拼进
    ``http://user:pass@host`` 会让 curl 直接拒绝整个请求（错误 5:
    Unsupported proxy syntax）。先 unquote 再 quote：已编码的输入保持稳定
    （幂等），原始空格/非 ASCII 字符则被正确编码。
    """
    text = str(value or "")
    try:
        text = unquote(text)
    except Exception:
        pass
    return quote(text, safe="")


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
                url = f"http://{_encode_proxy_credential(username)}:{_encode_proxy_credential(password)}@{host}:{port}"
        elif "@" in url:
            left, right = url.split("@", 1)
            if ":" in left and ":" in right:
                # 标准格式 user:pass@host:port 时右侧必须是合法 host:port；
                # 先按标准顺序解释，避免纯数字密码（如 user:123456@host:8080）
                # 被反向重组为 host:port@user:pass
                right_host, _, right_port = right.rpartition(":")
                if right_host and right_port.isdigit() and ":" not in right_host:
                    url = f"http://{left}@{right}"
                else:
                    host, port = left.rsplit(":", 1)
                    username, password = right.split(":", 1)
                    if port.isdigit():
                        url = f"http://{_encode_proxy_credential(username)}:{_encode_proxy_credential(password)}@{host}:{port}"
            if "://" not in url:
                url = "http://" + url
        else:
            parts = url.split(":")
            if len(parts) == 4 and parts[1].isdigit():
                host, port, username, password = parts
                url = f"http://{_encode_proxy_credential(username)}:{_encode_proxy_credential(password)}@{host}:{port}"
            elif len(parts) == 4 and parts[3].isdigit():
                username, password, host, port = parts
                url = f"http://{_encode_proxy_credential(username)}:{_encode_proxy_credential(password)}@{host}:{port}"
            else:
                url = "http://" + url
    if re.match(r"^socks://", url, re.IGNORECASE):
        url = "socks5h" + url[5:]
    elif re.match(r"^socks4a?://", url, re.IGNORECASE):
        url = re.sub(r"^socks4a?://", "socks4://", url, count=1, flags=re.IGNORECASE)
    # 直贴的完整 URL 若 userinfo 含原始空格/非 ASCII 同样会让 curl 报错 5，
    # 按相同规则修复。
    if "://" in url:
        scheme, rest = url.split("://", 1)
        userinfo, at, hostpart = rest.rpartition("@")
        if at and (" " in userinfo or any(ord(ch) > 127 for ch in userinfo)):
            user, _, password = userinfo.partition(":")
            url = (
                f"{scheme}://{_encode_proxy_credential(user)}:"
                f"{_encode_proxy_credential(password)}@{hostpart}"
            )
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


def compute_proxy_retry_limit(max_retries: int, proxy_pool: list) -> int:
    """Shared retry budget across distinct proxies (used by all schedulers).

    Always retries at least ``max_retries`` times, and up to 5 extra attempts
    when the pool has distinct proxies to rotate through.
    """
    distinct = len({str(p) for p in proxy_pool if str(p or "").strip()})
    return max(int(max_retries or 0), min(5, max(0, distinct - 1)))


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
        # curl-cffi/OpenSSL and proxy bridges use several equivalent labels
        # for a transient TLS tunnel failure.  These errors do not say
        # anything about the account credential, so a different route should
        # be tried before marking the account failed.
        "curl: (27)",
        "ssl error",
        "sslerror",
        "ssl connect",
        "ssl handshake",
        "tls handshake",
        "tlsv1_alert",
        "certificate verify failed",
        "unexpected eof while reading",
        "eof occurred in violation of protocol",
        "proxy tunnel",
        "gateway timeout",
    )
    if any(marker in message for marker in exact_markers):
        return True
    return any(
        marker in message
        for marker in (
            "curl: (56) recv failure",
            "connection reset by peer",
            "connection closed abruptly",
            "connection aborted",
            "broken pipe",
        )
    )

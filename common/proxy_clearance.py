# -*- coding: utf-8 -*-
"""Small FlareSolverr clearance cache shared by the protocol registrar."""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib import request as urllib_request
from urllib.parse import unquote, urlparse


@dataclass(frozen=True)
class ClearanceBundle:
    target_host: str
    cookies: dict[str, str]
    user_agent: str
    expires_at: float


_CACHE: dict[tuple[str, str], ClearanceBundle] = {}
_LOCK = threading.Lock()
_ERROR_STATE = threading.local()


def _set_error(message: str = "") -> None:
    _ERROR_STATE.message = str(message or "").strip()


def clearance_error() -> str:
    """Return the latest refresh failure for the current registration thread."""
    return str(getattr(_ERROR_STATE, "message", "") or "")


def _flaresolverr_proxy(value: str) -> dict[str, str]:
    """Convert an authenticated URL to FlareSolverr's proxy object schema."""
    proxy_url = str(value or "").strip()
    if not proxy_url:
        return {}
    # Chromium accepts socks5:// but does not recognize requests/curl's
    # socks5h:// alias. FlareSolverr passes this URL to Chromium directly.
    if proxy_url.lower().startswith("socks5h://"):
        proxy_url = "socks5://" + proxy_url[len("socks5h://") :]
    parsed = urlparse(proxy_url)
    if not parsed.hostname:
        return {"url": proxy_url}
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    port = f":{parsed.port}" if parsed.port else ""
    result = {"url": f"{parsed.scheme}://{host}{port}"}
    if parsed.username is not None:
        result["username"] = unquote(parsed.username)
    if parsed.password is not None:
        result["password"] = unquote(parsed.password)
    return result


def _solution_error(solution: dict[str, Any]) -> str:
    body = str(solution.get("response") or "")
    error_codes = sorted(set(re.findall(r"ERR_[A-Z0-9_]+", body)))
    if error_codes:
        return "FlareSolverr Chromium 代理页: " + ", ".join(error_codes[:4])
    if "chrome-error://chromewebdata" in body.lower():
        return "FlareSolverr 返回 Chromium 网络错误页"
    try:
        status = int(solution.get("status") or 0)
    except (TypeError, ValueError):
        status = 0
    if status >= 400:
        return f"FlareSolverr 页面状态异常: HTTP {status}"
    return ""


def _runtime_proxy_is_reachable_from_flaresolverr(value: str) -> bool:
    proxy = str(value or "").strip()
    if not proxy:
        return False
    try:
        host = str(urlparse(proxy).hostname or "").lower()
    except Exception:
        return False
    return bool(host) and host not in {"127.0.0.1", "localhost", "::1"}


def _settings(
    proxy_config: dict[str, Any] | None,
    request_proxy: str = "",
) -> dict[str, Any]:
    cfg = proxy_config if isinstance(proxy_config, dict) else {}
    clearance = cfg.get("clearance") if isinstance(cfg.get("clearance"), dict) else {}
    configured_proxy = str(clearance.get("proxy_url") or cfg.get("url") or "").strip()
    runtime_proxy = str(request_proxy or "").strip()
    # An external task proxy is directly reachable from the FlareSolverr
    # container and must match the protocol request's exit IP. Loopback task
    # proxies remain mapped to the configured Docker-side proxy (e.g. warp).
    clearance_proxy = (
        runtime_proxy
        if _runtime_proxy_is_reachable_from_flaresolverr(runtime_proxy)
        else configured_proxy or runtime_proxy
    )
    return {
        "enabled": bool(cfg.get("enabled")) and bool(clearance.get("enabled")),
        "mode": str(clearance.get("mode") or "none").strip().lower(),
        "url": str(clearance.get("flaresolverr_url") or "").strip().rstrip("/"),
        "proxy": clearance_proxy,
        "timeout": max(1, int(clearance.get("timeout_sec") or 60)),
        "refresh_interval": max(0, int(clearance.get("refresh_interval") or 3600)),
    }


def _domain_matches(host: str, domain: str) -> bool:
    host = str(host or "").strip().lower().lstrip(".")
    domain = str(domain or "").strip().lower().lstrip(".")
    return not domain or host == domain or host.endswith("." + domain)


def _bundle_from_solution(solution: dict[str, Any], target_url: str, ttl: int) -> ClearanceBundle | None:
    host = str(urlparse(target_url).hostname or "").lower()
    cookies: dict[str, str] = {}
    for item in solution.get("cookies") if isinstance(solution.get("cookies"), list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        domain = str(item.get("domain") or "").strip()
        if name and _domain_matches(host, domain):
            cookies[name] = str(item.get("value") or "")
    user_agent = str(solution.get("userAgent") or "").strip()
    # A User-Agent or __cf_bm alone cannot replay a managed challenge in the
    # protocol client. Treat it as clearance only when the actual token exists.
    if not cookies.get("cf_clearance"):
        return None
    return ClearanceBundle(host, cookies, user_agent, time.time() + ttl)


def cached_clearance(
    proxy_config: dict[str, Any] | None,
    target_url: str,
    request_proxy: str = "",
) -> ClearanceBundle | None:
    settings = _settings(proxy_config, request_proxy)
    host = str(urlparse(target_url).hostname or "").lower()
    key = (settings["proxy"], host)
    with _LOCK:
        bundle = _CACHE.get(key)
        if bundle and bundle.expires_at > time.time():
            return bundle
        _CACHE.pop(key, None)
    return None


def refresh_clearance(
    proxy_config: dict[str, Any] | None,
    target_url: str,
    request_proxy: str = "",
) -> ClearanceBundle | None:
    _set_error()
    settings = _settings(proxy_config, request_proxy)
    if not settings["enabled"] or settings["mode"] != "flaresolverr" or not settings["url"]:
        _set_error("FlareSolverr 清障未启用或地址为空")
        return None
    payload: dict[str, Any] = {
        "cmd": "request.get",
        "url": target_url,
        "maxTimeout": settings["timeout"] * 1000,
    }
    if settings["proxy"]:
        payload["proxy"] = _flaresolverr_proxy(settings["proxy"])
    endpoint = settings["url"] + "/v1"
    req = urllib_request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=settings["timeout"] + 5) as response:
            data = json.loads(response.read())
    except Exception as error:
        _set_error(f"FlareSolverr 请求失败: {str(error)[:240]}")
        return None
    solution = data.get("solution") if isinstance(data, dict) and str(data.get("status") or "").lower() == "ok" else None
    if not isinstance(solution, dict):
        message = str(data.get("message") or "") if isinstance(data, dict) else ""
        _set_error(f"FlareSolverr 未返回 solution: {message[:200]}".rstrip(": "))
        return None
    solution_error = _solution_error(solution)
    if solution_error:
        _set_error(solution_error)
        return None
    bundle = _bundle_from_solution(solution, target_url, settings["refresh_interval"])
    if bundle is None:
        names = [
            str(item.get("name") or "")
            for item in solution.get("cookies", [])
            if isinstance(item, dict) and item.get("name")
        ]
        suffix = f"（仅返回: {', '.join(names[:6])}）" if names else ""
        _set_error(f"FlareSolverr 未返回 cf_clearance cookie{suffix}")
    if bundle:
        key = (settings["proxy"], bundle.target_host)
        with _LOCK:
            _CACHE[key] = bundle
    return bundle


def headers_with_clearance(
    proxy_config: dict[str, Any] | None,
    headers: dict[str, str],
    target_url: str,
    user_agent_override: str = "",
    request_proxy: str = "",
) -> dict[str, str]:
    output = {str(key): str(value) for key, value in headers.items()}
    bundle = cached_clearance(proxy_config, target_url, request_proxy)
    user_agent = user_agent_override or (bundle.user_agent if bundle else "")
    if user_agent:
        key = next((key for key in output if key.lower() == "user-agent"), "user-agent")
        output[key] = user_agent
    if bundle and bundle.cookies:
        key = next((key for key in output if key.lower() == "cookie"), "cookie")
        existing = str(output.get(key) or "").strip().rstrip(";")
        additions = "; ".join(f"{name}={value}" for name, value in bundle.cookies.items())
        output[key] = f"{existing}; {additions}" if existing else additions
    return output

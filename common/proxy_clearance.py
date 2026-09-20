# -*- coding: utf-8 -*-
"""Small FlareSolverr clearance cache shared by the protocol registrar."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
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

# 内嵌 FlareSolverr(项目 third_party/)自动拉起支持。
_SPAWN_LOCK = threading.Lock()
_SPAWN_STATE: dict[str, float] = {"last_try": 0.0}
_EMBEDDED_ENTRY = (
    Path(__file__).resolve().parent.parent / "third_party" / "flaresolverr" / "src" / "flaresolverr.py"
)


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
        # 清障链独立于代理开关：proxy.enabled=false（直连）时同样可能撞 CF
        # 质询，clearance.enabled 单独控制 FlareSolverr 是否参与。
        "enabled": bool(clearance.get("enabled")),
        "mode": str(clearance.get("mode") or "none").strip().lower(),
        "url": str(clearance.get("flaresolverr_url") or "").strip().rstrip("/"),
        "proxy": clearance_proxy,
        "timeout": max(1, int(clearance.get("timeout_sec") or 60)),
        "refresh_interval": max(0, int(clearance.get("refresh_interval") or 3600)),
        "autostart": bool(clearance.get("autostart", True)),
    }


def _flaresolverr_alive(url: str) -> bool:
    """探测 FlareSolverr 根端点(Docker 版/已有实例均适用)。"""
    try:
        with urllib_request.urlopen(url + "/", timeout=1.5) as response:
            return 200 <= response.status < 500
    except Exception:
        return False


def _spawn_embedded_flaresolverr(url: str) -> bool:
    """从项目 third_party/ 拉起内嵌 FlareSolverr 常驻子进程。

    崩溃由下一次 ensure 的"探测失败→重新拉起"自愈;日志写入
    data/logs/flaresolverr_embedded.log。
    """
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8191
    if not _EMBEDDED_ENTRY.exists():
        _set_error(f"内嵌 FlareSolverr 缺失: {_EMBEDDED_ENTRY}")
        return False
    log_dir = _EMBEDDED_ENTRY.parents[3] / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "flaresolverr_embedded.log"
    try:
        with open(log_path, "ab") as log_file:
            subprocess.Popen(
                [sys.executable, str(_EMBEDDED_ENTRY), "--host", host, "--port", str(port)],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                cwd=str(_EMBEDDED_ENTRY.parent),
            )
    except Exception as error:
        _set_error(f"内嵌 FlareSolverr 拉起失败: {str(error)[:200]}")
        return False
    # 首次拉起含 chromedriver 下载与 Chrome 冷启动,给足 60s
    deadline = time.time() + 60.0
    while time.time() < deadline:
        if _flaresolverr_alive(url):
            return True
        time.sleep(0.5)
    _set_error("内嵌 FlareSolverr 启动超时(60s),详见 data/logs/flaresolverr_embedded.log")
    return False


def _ensure_flaresolverr(settings: dict[str, Any]) -> None:
    """清障请求前确保服务在线:已有实例(如 Docker 版)直接复用,否则自动拉起内嵌副本。"""
    url = str(settings.get("url") or "")
    if not url or _flaresolverr_alive(url):
        return
    if not settings.get("autostart"):
        return
    with _SPAWN_LOCK:
        # 节流:60s 内不重复尝试,避免 CF 高频期反复拉起拖慢请求
        if time.time() - _SPAWN_STATE["last_try"] < 60.0:
            return
        _SPAWN_STATE["last_try"] = time.time()
        _spawn_embedded_flaresolverr(url)


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
    # 先查缓存：批量任务撞同一出口的 CF 拦截时，后来者直接复用已解出的
    # clearance，避免把 FlareSolverr 打爆。
    cached = cached_clearance(proxy_config, target_url, request_proxy)
    if cached is not None:
        return cached
    _ensure_flaresolverr(settings)
    # 全局串行化求解：FlareSolverr(v3) 单浏览器实例并发求解会互相超时，
    # 批量高并发下表现为"clearance 刷新失败"。排队 + 双检缓存后，同键
    # 请求等前排解完直接命中缓存，异键请求顺序求解。
    with _REFRESH_LOCK:
        cached = cached_clearance(proxy_config, target_url, request_proxy)
        if cached is not None:
            return cached
        return _solve_clearance_locked(settings, target_url)


_REFRESH_LOCK = threading.Lock()


def _solve_clearance_locked(settings: dict[str, Any], target_url: str) -> ClearanceBundle | None:
    """Actual FlareSolverr round-trip; caller must hold _REFRESH_LOCK."""
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
    # 仅在调用方显式提供当前会话 UA 时才覆盖；不回退 bundle 的 FlareSolverr UA，
    # 否则 Firefox TLS 会话会配上 Chrome UA，形成 UA/TLS 指纹矛盾
    if user_agent_override:
        key = next((key for key in output if key.lower() == "user-agent"), "user-agent")
        output[key] = user_agent_override
    if bundle and bundle.cookies:
        key = next((key for key in output if key.lower() == "cookie"), "cookie")
        existing = str(output.get(key) or "").strip().rstrip(";")
        additions = "; ".join(f"{name}={value}" for name, value in bundle.cookies.items())
        output[key] = f"{existing}; {additions}" if existing else additions
    return output

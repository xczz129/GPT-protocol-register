# -*- coding: utf-8 -*-
"""Fingerprint browser connect + stealth inject for Playwright CDP / free local."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import random
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from playwright.async_api import async_playwright

from local_browser import get_free_browser_provider

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BROWSER_DIAGNOSTICS_DIR = PROJECT_ROOT / "data" / "browser_diagnostics"


def _utc_stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _env_float(name: str, default: float, *, min_value: float | None = None, max_value: float | None = None) -> float:
    raw = os.environ.get(name)
    try:
        value = float(raw) if raw not in {None, ""} else float(default)
    except (TypeError, ValueError):
        value = float(default)
    if min_value is not None:
        value = max(float(min_value), value)
    if max_value is not None:
        value = min(float(max_value), value)
    return value


def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


def _redact_url_credentials(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return raw
    try:
        parsed = urlparse(raw)
        if parsed.username is None:
            return raw
        host = parsed.hostname or ""
        netloc = host
        if parsed.port:
            netloc += f":{parsed.port}"
        return parsed._replace(netloc=f"***:***@{netloc}").geturl()
    except Exception:
        if "@" in raw:
            return "***:***@" + raw.rsplit("@", 1)[-1]
        return raw


def _redact_proxy_config(value):
    if not value:
        return value
    if isinstance(value, str):
        return _redact_url_credentials(value)
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            lk = str(key).lower()
            if lk in {"username", "password", "token", "auth", "proxy-authorization"}:
                out[str(key)] = "***" if item else ""
            elif lk == "server":
                out[str(key)] = _redact_url_credentials(str(item or ""))
            else:
                out[str(key)] = _json_safe(item)
        return out
    return _json_safe(value)


def _diagnostic_launch_kwargs(kwargs: dict | None) -> dict:
    data = dict(kwargs or {})
    if "proxy" in data:
        data["proxy"] = _redact_proxy_config(data.get("proxy"))
    return _json_safe(data)


async def _write_browser_diagnostic(
    event: str,
    *,
    provider: str = "",
    profile_id: str = "",
    proxy=None,
    launch_kwargs: dict | None = None,
    elapsed: float | None = None,
    error: BaseException | str | None = None,
    page=None,
    extra: dict | None = None,
    screenshot: bool = False,
) -> Path | None:
    """Write redacted browser diagnostics without depending on the web app.

    The files are JSON so panel / shell tooling can diff failures across proxy,
    headless, geoip and lifecycle paths.  Credentials and page bodies stay out;
    screenshots are opt-in and stored next to the JSON file.
    """
    try:
        BROWSER_DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = _utc_stamp()
        safe_profile = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(profile_id or "na"))[:80]
        path = BROWSER_DIAGNOSTICS_DIR / f"{stamp}_{event}_{safe_profile}.json"
        payload = {
            "ts": stamp,
            "event": event,
            "provider": provider,
            "profile_id": str(profile_id or ""),
            "proxy": _redact_proxy_config(proxy),
            "elapsed_sec": round(float(elapsed), 3) if elapsed is not None else None,
            "launch": _diagnostic_launch_kwargs(launch_kwargs),
            "env": {
                "FINGERPRINT_BROWSER": os.environ.get("FINGERPRINT_BROWSER", ""),
                "CAMOUFOX_HEADLESS": os.environ.get("CAMOUFOX_HEADLESS", ""),
                "CAMOUFOX_OS": os.environ.get("CAMOUFOX_OS", ""),
                "CAMOUFOX_LOCALE": os.environ.get("CAMOUFOX_LOCALE", ""),
                "CAMOUFOX_GEOIP": os.environ.get("CAMOUFOX_GEOIP", ""),
                "CAMOUFOX_HUMANIZE": os.environ.get("CAMOUFOX_HUMANIZE", ""),
                "CAMOUFOX_LAUNCH_TIMEOUT": os.environ.get("CAMOUFOX_LAUNCH_TIMEOUT", ""),
            },
            "error_type": type(error).__name__ if isinstance(error, BaseException) else ("str" if error else ""),
            "error": str(error)[:1200] if error else "",
            "extra": _json_safe(extra or {}),
        }
        if page is not None:
            try:
                payload["page_url"] = str(getattr(page, "url", "") or "")[:1000]
            except Exception:
                payload["page_url"] = ""
            if screenshot:
                shot = path.with_suffix(".png")
                try:
                    await asyncio.wait_for(page.screenshot(path=str(shot), full_page=False), timeout=5.0)
                    payload["screenshot"] = str(shot)
                except Exception as shot_error:
                    payload["screenshot_error"] = str(shot_error)[:300]
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path
    except Exception:
        return None

STEALTH_JS = r"""
    try {
        Object.defineProperty(Navigator.prototype, 'webdriver', {
            get: () => undefined,
            configurable: true,
        });
    } catch(e) {}

    if (!window.chrome) {
        window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};
    }

    const origQuery = window.navigator.permissions?.query;
    if (origQuery) {
        window.navigator.permissions.query = (params) => (
            params.name === 'notifications' ?
                Promise.resolve({state: Notification.permission}) :
                origQuery.call(window.navigator.permissions, params)
        );
    }

    const cdcProps = Object.getOwnPropertyNames(window).filter(p =>
        p.match(/^cdc_|^__cdc|^_cdp|^__cdp|^chrome_devtools/i)
    );
    cdcProps.forEach(p => { try { delete window[p]; } catch(e) {} });

    if (window.outerWidth === 0) {
        Object.defineProperty(window, 'outerWidth', {get: () => window.innerWidth + 16});
    }
    if (window.outerHeight === 0) {
        Object.defineProperty(window, 'outerHeight', {get: () => window.innerHeight + 88});
    }

    if (Notification.permission === 'denied') {
        Object.defineProperty(Notification, 'permission', {get: () => 'default'});
    }

    const origGetter = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow');
    if (origGetter) {
        Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
            get: function() {
                const w = origGetter.get.call(this);
                if (w) {
                    try { Object.defineProperty(w.navigator, 'webdriver', {get: () => undefined}); } catch(e) {}
                }
                return w;
            }
        });
    }
"""


async def inject_stealth(context, page):
    try:
        cdp = await context.new_cdp_session(page)
        await cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": STEALTH_JS})
    except Exception as e:
        print(f"  [隐身] CDP 注入跳过（非 Chromium 正常）: {str(e)[:60]}")
    try:
        await page.evaluate(f"() => {{{STEALTH_JS}}}")
    except Exception:
        pass
    try:
        await context.add_init_script(f"() => {{{STEALTH_JS}}}")
    except Exception:
        pass
    print("  [隐身] 脚本已注入")


def create_browser_with_retry(bb, name, proxy=None, retries=3):
    for attempt in range(retries):
        try:
            return bb.create_browser(name=name, proxy=proxy)
        except Exception as e:
            msg = str(e)
            msg_l = msg.lower()
            if any(
                k in msg_l
                for k in (
                    "tls",
                    "socket",
                    "econnreset",
                    "network",
                    "timeout",
                    "connection refused",
                    "max retries",
                    "failed to establish",
                )
            ):
                print(
                    f"  create browser network error (retry {attempt+1}/{retries}): {msg[:120]}"
                )
                time.sleep(2)
                continue
            raise
    return None


def _is_local_provider(bb) -> bool:
    name = (getattr(bb, "provider_name", "") or "").lower()
    return name in {"local", "camoufox", "chrome", "stealth", "obscura"}


def _local_http_url(url: str) -> str:
    """Validate a local CDP HTTP URL before fetching it.

    Obscura exposes a DevTools endpoint on loopback.  Rejecting non-http
    schemes prevents accidental urllib-style file:// handling, and rejecting
    non-loopback hosts keeps this helper from becoming a generic SSRF gadget.
    """
    value = str(url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"unsupported CDP URL scheme: {parsed.scheme or '<empty>'}")
    host = (parsed.hostname or "").lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(f"CDP URL host must be loopback: {host or '<empty>'}")
    return value


def _http_json(url: str, timeout: float = 3.0):
    resp = requests.get(_local_http_url(url), timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _obscura_list_pages(endpoint: str) -> list[dict]:
    endpoint = (endpoint or "").rstrip("/")
    try:
        data = _http_json(f"{endpoint}/json/list", timeout=3.0)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _close_local_target(endpoint: str, target_id: str) -> None:
    url = f"{endpoint.rstrip('/')}/json/close/{quote(str(target_id), safe='')}"
    requests.get(_local_http_url(url), timeout=2).raise_for_status()


async def _obscura_close_default_pages(endpoint: str) -> None:
    """Close Obscura seed pages so Playwright does not hit ``Duplicate target page-1``.

    Obscura pre-creates a target id like ``page-1``. Playwright asserts unique
    target ids and aborts the CDP pipe when it sees that seed page. Close all
    page targets via browser WebSocket (and HTTP fallback) before connect.
    """
    endpoint = (endpoint or "").rstrip("/")
    if not endpoint:
        return

    # Collect target ids from /json/list (more reliable than Target.getTargets here)
    page_ids: list[str] = []
    for item in await asyncio.to_thread(_obscura_list_pages, endpoint):
        tid = item.get("id")
        if tid and (item.get("type") or "page") == "page":
            page_ids.append(str(tid))

    ws_url = ""
    try:
        ver = _http_json(f"{endpoint}/json/version", timeout=3.0)
        ws_url = str(ver.get("webSocketDebuggerUrl") or "")
    except Exception as e:
        print(f"  Obscura json/version: {str(e)[:80]}")

    closed = 0

    # 1) HTTP /json/close/{id} if supported
    for tid in list(page_ids):
        try:
            await asyncio.to_thread(
                _close_local_target,
                endpoint,
                tid,
            )
            closed += 1
            page_ids.remove(tid)
        except Exception:
            pass

    # 2) Browser-level CDP Target.closeTarget via websockets
    if page_ids or True:
        try:
            import websockets  # type: ignore
        except ImportError:
            websockets = None  # type: ignore

        if websockets and ws_url:
            try:
                async with websockets.connect(
                    ws_url, max_size=8_000_000, open_timeout=5
                ) as ws:
                    mid = 0

                    async def call(method: str, params: dict | None = None) -> dict:
                        nonlocal mid
                        mid += 1
                        payload: dict = {"id": mid, "method": method}
                        if params:
                            payload["params"] = params
                        await ws.send(json.dumps(payload))
                        while True:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5)
                            data = json.loads(raw)
                            if data.get("id") == mid:
                                return data

                    # Prefer Target.getTargets; also close ids from /json/list
                    ids: set[str] = set(page_ids)
                    try:
                        result = await call("Target.getTargets")
                        for t in (result.get("result") or {}).get("targetInfos") or []:
                            if (t.get("type") or "") == "page" and t.get("targetId"):
                                ids.add(str(t["targetId"]))
                    except Exception:
                        pass

                    # Known seed id used by Obscura even when getTargets is empty
                    ids.add("page-1")

                    for tid in sorted(ids):
                        try:
                            await call("Target.closeTarget", {"targetId": tid})
                            closed += 1
                        except Exception:
                            pass
            except Exception as e:
                print(f"  Obscura WS 关闭默认页: {str(e)[:100]}")

    # Brief settle + verify
    await asyncio.sleep(0.2)
    left = await asyncio.to_thread(_obscura_list_pages, endpoint)
    if left:
        # last resort: close remaining via HTTP again
        for item in left:
            tid = item.get("id")
            if not tid:
                continue
            try:
                await asyncio.to_thread(_close_local_target, endpoint, tid)
                closed += 1
            except Exception:
                pass
        await asyncio.sleep(0.15)
        left = await asyncio.to_thread(_obscura_list_pages, endpoint)

    print(
        f"  Obscura 预清理 page 目标 closed≈{closed}, remaining={len(left)}"
        + (f" { [x.get('id') for x in left] }" if left else "")
    )


async def _launch_local_chrome(p, bb, profile_id, proxy=None):
    """Launch free local Chromium/Chrome with stealth."""
    from local_browser import (
        _proxy_for_playwright,
        chrome_executable_candidates,
        probe_chrome_executable,
    )

    candidates = chrome_executable_candidates()
    bundled = str(getattr(p.chromium, "executable_path", "") or "")
    if bundled and Path(bundled).is_file() and bundled not in candidates:
        candidates.append(bundled)
    checks = await asyncio.gather(
        *(asyncio.to_thread(probe_chrome_executable, candidate) for candidate in candidates)
    )
    probe_results = [
        (candidate, ok, detail)
        for candidate, (ok, detail) in zip(candidates, checks)
    ]
    exe = next((candidate for candidate, ok, _detail in probe_results if ok), None)
    if not exe:
        detail = "; ".join(f"{path}: {reason}" for path, _ok, reason in probe_results)
        raise RuntimeError(f"未找到可启动的 Local Chrome/Chromium: {detail or '无候选可执行文件'}")
    proxy_cfg = _proxy_for_playwright(proxy)
    headless = os.environ.get("LOCAL_HEADLESS", "1").strip() not in {"0", "false", "no"}
    # Prefer headed under Xvfb if DISPLAY is set and LOCAL_HEADLESS not forced
    if os.environ.get("DISPLAY") and os.environ.get("LOCAL_HEADLESS") is None:
        # headed under virtual display looks more real
        headless = False

    viewport = random.choice(  # nosemgrep: gitlab.bandit.B311
        [
            {"width": 1366, "height": 768},
            {"width": 1440, "height": 900},
            {"width": 1536, "height": 864},
            {"width": 1920, "height": 1080},
        ]
    )
    launch_kwargs = {
        "headless": headless,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-infobars",
            f"--window-size={viewport['width']},{viewport['height']}",
        ],
        "ignore_default_args": ["--enable-automation"],
    }
    launch_kwargs["executable_path"] = exe
    print(f"  本地 Chrome: {exe} headless={headless}")
    if proxy_cfg:
        launch_kwargs["proxy"] = proxy_cfg
        print(f"  proxy: {proxy_cfg.get('server')}")

    browser = await p.chromium.launch(**launch_kwargs)
    if hasattr(bb, "register_runtime"):
        bb.register_runtime(profile_id, browser=browser, owns_browser=True)
    context = await browser.new_context(
        locale=os.environ.get("LOCAL_LOCALE", "en-US"),
        timezone_id=os.environ.get("LOCAL_TZ", "UTC"),
        viewport=viewport,
        screen=viewport,
        color_scheme="light",
    )
    page = await context.new_page()
    if hasattr(bb, "register_runtime"):
        bb._runtime[profile_id].update(context=context)
    await inject_stealth(context, page)
    return browser, context, page


# Camoufox (Firefox) quirk: FormData.entries() returns an iterator without
# Symbol.iterator, so `[...fd.entries()]` throws. ChatGPT login uses FormData
# for submit — without this polyfill Continue spins / shows "Email is required".
CAMOUFOX_FORMDATA_POLYFILL = """
(() => {
  const patch = (instance, proto, method) => {
    if (!proto || typeof proto[method] !== 'function') return;
    const orig = proto[method];
    // Modern Camoufox already exposes standards-compliant iterable methods.
    // Replacing those native functions breaks NextAuth/Remix form detection.
    try {
      const probe = orig.call(instance);
      if (probe && typeof probe[Symbol.iterator] === 'function') return;
    } catch (e) {}
    try {
      // already patched
      if (orig.__camoufoxIterable) return;
    } catch (e) {}
    const wrapped = function (...args) {
      const it = orig.apply(this, args);
      if (it && typeof it[Symbol.iterator] !== 'function' && typeof it.next === 'function') {
        try {
          it[Symbol.iterator] = function () { return this; };
        } catch (e) {}
      }
      return it;
    };
    try { wrapped.__camoufoxIterable = true; } catch (e) {}
    try {
      proto[method] = wrapped;
    } catch (e) {
      try {
        Object.defineProperty(proto, method, { value: wrapped, configurable: true });
      } catch (e2) {}
    }
  };
  try {
    const formData = new FormData();
    patch(formData, FormData.prototype, 'entries');
    patch(formData, FormData.prototype, 'keys');
    patch(formData, FormData.prototype, 'values');
  } catch (e) {}
  try {
    patch(new URLSearchParams(), URLSearchParams.prototype, 'entries');
  } catch (e) {}
  try { patch(new Headers(), Headers.prototype, 'entries'); } catch (e) {}
})();
"""


def _env_enabled(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if not value:
        return default
    return value not in {"0", "false", "no", "off"}


def _camoufox_launch_config(proxy=None) -> dict:
    """Build a consistent Camoufox fingerprint policy from panel settings."""
    from local_browser import _proxy_for_playwright

    proxy_cfg = _proxy_for_playwright(proxy)
    os_values = [
        item.strip().lower()
        for item in os.environ.get("CAMOUFOX_OS", "windows").split(",")
        if item.strip().lower() in {"windows", "macos", "linux"}
    ] or ["windows"]
    headless = _env_enabled(
        "CAMOUFOX_HEADLESS", _env_enabled("LOCAL_HEADLESS", True)
    )
    default_humanize = 0.0 if headless else 0.15
    humanize = _env_float(
        "CAMOUFOX_HUMANIZE",
        default_humanize,
        min_value=0.0,
        max_value=1.5,
    )

    kwargs = {
        "headless": headless,
        "os": tuple(os_values),
        "disable_coop": True,
        "i_know_what_im_doing": True,
    }
    if humanize > 0:
        kwargs["humanize"] = humanize
    if proxy_cfg:
        kwargs["proxy"] = proxy_cfg

    locale = os.environ.get("CAMOUFOX_LOCALE", "").strip()
    use_geoip = _env_enabled("CAMOUFOX_GEOIP", True)
    kwargs["block_webrtc"] = not use_geoip
    if use_geoip:
        kwargs["geoip"] = True
        if locale:
            # Keep a stable UI language while GeoIP supplies matching timezone
            # and geolocation for the proxy exit.
            kwargs["locale"] = locale
    elif locale:
        kwargs["locale"] = locale
    else:
        kwargs["locale"] = "en-US"
    return kwargs


def _secure_public_ip(proxy_cfg: dict | None = None) -> str:
    """Resolve the browser's exit IP over verified HTTPS."""
    session = requests.Session()
    session.trust_env = False
    if proxy_cfg and proxy_cfg.get("server"):
        server = str(proxy_cfg["server"])
        username = str(proxy_cfg.get("username") or "")
        password = str(proxy_cfg.get("password") or "")
        if username:
            parsed = urlparse(server)
            auth = quote(username, safe="")
            if password:
                auth += ":" + quote(password, safe="")
            server = parsed._replace(netloc=f"{auth}@{parsed.netloc}").geturl()
        session.proxies = {"http": server, "https": server}

    last_error = "no response"
    try:
        for url in (
            "https://api.ipify.org",
            "https://checkip.amazonaws.com",
            "https://icanhazip.com",
        ):
            try:
                response = session.get(url, timeout=5)
                response.raise_for_status()
                return str(ipaddress.ip_address(response.text.strip()))
            except Exception as error:
                last_error = str(error)
    finally:
        session.close()
    raise RuntimeError(f"无法通过 HTTPS 获取出口 IP: {last_error}")


async def _launch_camoufox(p, bb, profile_id, proxy=None):
    """Launch free Camoufox anti-detect browser with bounded startup diagnostics."""
    from camoufox.async_api import AsyncNewBrowser

    kwargs = _camoufox_launch_config(proxy)
    proxy_cfg = kwargs.get("proxy") or {}
    if proxy_cfg:
        redacted = _redact_proxy_config(proxy_cfg)
        print(f"  proxy: {redacted.get('server') if isinstance(redacted, dict) else redacted}")
    region = "geoip" if kwargs.get("geoip") else kwargs.get("locale")
    launch_timeout = _env_float("CAMOUFOX_LAUNCH_TIMEOUT", 60.0, min_value=15.0, max_value=180.0)
    print(
        f"  Camoufox 启动中 headless={kwargs['headless']} "
        f"os={','.join(kwargs['os'])} region={region} "
        f"humanize={kwargs.get('humanize', 0)} timeout={launch_timeout:.0f}s"
    )
    t0 = time.time()
    browser = context = page = None

    async def _cleanup_partial() -> None:
        for target in (context, browser):
            if target is None:
                continue
            try:
                await asyncio.wait_for(target.close(), timeout=3.0)
            except Exception:
                pass
        try:
            bb.delete_browser(profile_id)
        except Exception:
            pass

    async def _new_browser(active_kwargs: dict):
        return await asyncio.wait_for(
            AsyncNewBrowser(p, **active_kwargs),
            timeout=launch_timeout,
        )

    try:
        if kwargs.get("geoip") is True:
            try:
                kwargs["geoip"] = await asyncio.wait_for(
                    asyncio.to_thread(_secure_public_ip, proxy_cfg or None),
                    timeout=min(12.0, max(4.0, launch_timeout / 3)),
                )
            except Exception as error:
                await _write_browser_diagnostic(
                    "camoufox_geoip_fallback",
                    provider="camoufox",
                    profile_id=profile_id,
                    proxy=proxy_cfg,
                    launch_kwargs=kwargs,
                    elapsed=time.time() - t0,
                    error=error,
                    extra={"fallback_locale": os.environ.get("CAMOUFOX_LOCALE", "").strip() or "en-US"},
                )
                kwargs.pop("geoip", None)
                kwargs["locale"] = os.environ.get("CAMOUFOX_LOCALE", "").strip() or "en-US"
                kwargs["block_webrtc"] = True
                if proxy_cfg:
                    print(
                        "  [Camoufox] 代理出口 IP 服务暂时不可达，"
                        f"保留代理并降级 locale={kwargs['locale']}: {str(error)[:120]}"
                    )
                else:
                    print(f"  [Camoufox] GeoIP 不可用，降级 locale={kwargs['locale']}")
        try:
            browser = await _new_browser(kwargs)
        except Exception as error:
            # GeoIP lookup is an optional consistency enhancement.  A blocked
            # IP service must not prevent the browser itself from starting.
            message = str(error).lower()
            error_name = type(error).__name__.lower()
            if kwargs.get("geoip") and (
                error_name == "invalidip" or "failed to get ip" in message
            ):
                await _write_browser_diagnostic(
                    "camoufox_launch_geoip_retry",
                    provider="camoufox",
                    profile_id=profile_id,
                    proxy=proxy_cfg,
                    launch_kwargs=kwargs,
                    elapsed=time.time() - t0,
                    error=error,
                )
                kwargs.pop("geoip", None)
                kwargs["locale"] = os.environ.get("CAMOUFOX_LOCALE", "").strip() or "en-US"
                kwargs["block_webrtc"] = True
                print(f"  [Camoufox] GeoIP 不可用，降级 locale={kwargs['locale']}")
                browser = await _new_browser(kwargs)
            else:
                raise

        # Camoufox may return Browser or BrowserContext. Do not override its
        # generated viewport/locale with a second, inconsistent fingerprint.
        context_timeout = min(30.0, max(10.0, launch_timeout / 2))
        page_timeout = min(30.0, max(12.0, launch_timeout / 3))
        if hasattr(browser, "new_context"):
            context = await asyncio.wait_for(browser.new_context(), timeout=context_timeout)
            page = await asyncio.wait_for(context.new_page(), timeout=page_timeout)
        else:
            context = browser
            pages = list(getattr(context, "pages", []) or [])
            page = pages[0] if pages else await asyncio.wait_for(context.new_page(), timeout=page_timeout)
            browser = context  # persistent-context style; close() owns the process
    except Exception as error:
        await _write_browser_diagnostic(
            "camoufox_launch_failed",
            provider="camoufox",
            profile_id=profile_id,
            proxy=proxy_cfg,
            launch_kwargs=kwargs,
            elapsed=time.time() - t0,
            error=error,
            page=page,
            screenshot=page is not None,
        )
        await _cleanup_partial()
        raise

    if hasattr(bb, "register_runtime"):
        bb.register_runtime(
            profile_id,
            browser=browser,
            context=context,
            owns_browser=True,
            launch_kwargs=_diagnostic_launch_kwargs(kwargs),
            provider="camoufox",
        )

    # Current ChatGPT/NextAuth requires the native FormData methods. The old
    # iterator wrapper is retained only for explicitly pinned legacy builds.
    legacy_formdata_patch = _env_enabled("CAMOUFOX_LEGACY_FORMDATA_PATCH", False)
    if legacy_formdata_patch:
        try:
            await asyncio.wait_for(context.add_init_script(CAMOUFOX_FORMDATA_POLYFILL), timeout=3.0)
        except Exception as e:
            print(f"  [Camoufox] legacy FormData 补丁注入失败: {str(e)[:80]}")
        try:
            await asyncio.wait_for(page.evaluate(CAMOUFOX_FORMDATA_POLYFILL), timeout=3.0)
        except Exception:
            pass

    formdata_mode = "legacy FormData patch" if legacy_formdata_patch else "native FormData"
    elapsed = time.time() - t0
    print(f"  Camoufox 就绪 {elapsed:.1f}s ({formdata_mode})")
    if elapsed > launch_timeout * 0.8:
        await _write_browser_diagnostic(
            "camoufox_launch_slow",
            provider="camoufox",
            profile_id=profile_id,
            proxy=proxy_cfg,
            launch_kwargs=kwargs,
            elapsed=elapsed,
            page=page,
            extra={"threshold_sec": round(launch_timeout * 0.8, 2)},
        )
    return browser, context, page


async def _launch_obscura(p, bb, profile_id, proxy=None):
    """Start obscura serve process and connect Playwright over CDP."""
    meta = getattr(bb, "_profiles", {}).get(profile_id) or {}
    proxy = proxy if proxy is not None else (meta.get("proxy") or None)

    # start_server is sync (subprocess + poll); run in thread to avoid blocking loop
    info = await asyncio.to_thread(bb.start_server, profile_id, proxy)

    endpoint = info.get("endpoint") or f"http://{info.get('host')}:{info.get('port')}"
    # Drop seed page BEFORE Playwright attaches (avoids Duplicate target page-1)
    await _obscura_close_default_pages(endpoint)

    print(f"  Playwright connect_over_cdp → {endpoint}")
    browser = await p.chromium.connect_over_cdp(endpoint)

    if browser.contexts:
        context = browser.contexts[0]
    else:
        context = await browser.new_context()

    # Drop any pages Playwright already bound; create a fresh one
    for old in list(context.pages):
        try:
            await old.close()
        except Exception:
            pass
    page = await context.new_page()

    # Merge browser handles into runtime (start_server already stored proc)
    rt = getattr(bb, "_runtime", {}).get(profile_id) or {}
    rt.update(
        {
            "browser": browser,
            "context": context,
            "owns_browser": True,
            "cdp_disconnect_only": True,
        }
    )
    if not hasattr(bb, "_runtime"):
        bb._runtime = {}
    bb._runtime[profile_id] = rt

    # Light stealth only — full CDP session often flaky on Obscura
    try:
        await context.add_init_script(f"() => {{{STEALTH_JS}}}")
    except Exception:
        pass
    try:
        await page.evaluate(f"() => {{{STEALTH_JS}}}")
    except Exception:
        pass
    print("  [隐身] Obscura 轻量脚本已注入")
    return browser, context, page


async def open_and_connect(name, p=None, proxy=None):
    """Create fingerprint browser, connect Playwright, inject stealth.

    Returns (bb, profile_id, browser, context, page).
    """
    owns_playwright = p is None
    if owns_playwright:
        p = await async_playwright().start()

    provider_name = os.environ.get("FINGERPRINT_BROWSER", "local").strip().lower()
    bb = get_free_browser_provider(provider_name)
    if bb is None:
        if owns_playwright:
            await p.stop()
        raise RuntimeError(
            f"unsupported fingerprint browser: {provider_name}; "
            "choose local, camoufox, or obscura"
        )
    pid = create_browser_with_retry(bb, name, proxy=proxy)
    if not pid:
        if owns_playwright:
            await p.stop()
        raise RuntimeError("create browser failed after retries")

    provider = getattr(bb, "provider_name", "local")
    proxy_bridge = None
    t0 = time.time()
    try:
        if provider == "camoufox":
            from common.proxy_bridge import AuthenticatedProxyBridge

            proxy_bridge = await AuthenticatedProxyBridge.start_for(proxy)
            launch_proxy = proxy_bridge.url if proxy_bridge is not None else proxy
            if proxy_bridge is not None:
                print("  Camoufox 代理认证桥已启用 (loopback -> upstream)")
            browser, context, page = await _launch_camoufox(
                p, bb, pid, proxy=launch_proxy
            )
        elif provider == "obscura":
            browser, context, page = await _launch_obscura(p, bb, pid, proxy=proxy)
        else:
            browser, context, page = await _launch_local_chrome(p, bb, pid, proxy=proxy)
        if hasattr(bb, "_runtime"):
            runtime = bb._runtime.get(pid)
            if isinstance(runtime, dict):
                runtime.setdefault("provider", provider)
                runtime.setdefault("original_proxy", _redact_proxy_config(proxy))
                if proxy_bridge is not None:
                    runtime["proxy_bridge"] = proxy_bridge
                    runtime["proxy_bridge_url"] = proxy_bridge.url
                if owns_playwright:
                    runtime["playwright"] = p
        if provider != "camoufox":
            try:
                await context.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
            except Exception as e:
                print(f"  设置 Accept-Language 失败: {e}")
        return bb, pid, browser, context, page
    except Exception as error:
        await _write_browser_diagnostic(
            "browser_open_failed",
            provider=provider,
            profile_id=pid,
            proxy=proxy,
            elapsed=time.time() - t0,
            error=error,
            extra={"name": name, "owns_playwright": owns_playwright},
        )
        if proxy_bridge is not None:
            try:
                await asyncio.wait_for(proxy_bridge.close(), timeout=3.0)
            except Exception:
                pass
        try:
            await teardown(bb, pid, delete=True)
        except Exception:
            pass
        if owns_playwright:
            try:
                await asyncio.wait_for(p.stop(), timeout=3.0)
            except Exception:
                pass
        raise


async def teardown(bb, profile_id, delete=True):
    # Close the in-process local browser runtime.
    if _is_local_provider(bb) and hasattr(bb, "_runtime"):
        provider = (getattr(bb, "provider_name", "") or "").lower()
        rt = bb._runtime.get(profile_id)
        if not isinstance(rt, dict):
            profiles = getattr(bb, "_profiles", {})
            if delete and profile_id in profiles:
                try:
                    await asyncio.to_thread(bb.delete_browser, profile_id)
                except Exception:
                    pass
            return
        # delete=False means debug retention; do not close the browser first.
        if not delete:
            rt["retained"] = True
            return
        # Event-loop atomic before the first await: concurrent stop/finally calls
        # share one cleanup owner.
        if rt.get("closing") or rt.get("closed"):
            return
        rt["closing"] = True
        browser = rt.get("browser")
        context = rt.get("context")
        playwright = rt.get("playwright")
        proxy_bridge = rt.get("proxy_bridge")
        close_errors: list[str] = []

        async def bounded_close(label: str, target, timeout=3.0):
            if target is None:
                return
            try:
                await asyncio.wait_for(target.close(), timeout=timeout)
            except Exception as error:
                close_errors.append(f"{label}: {type(error).__name__}: {str(error)[:160]}")

        # Close the loopback bridge early so hung CONNECT tunnels stop feeding the
        # browser while context.close() is waiting on network drains.
        if proxy_bridge is not None:
            try:
                await asyncio.wait_for(proxy_bridge.close(), timeout=2.0)
            except asyncio.TimeoutError:
                # close() has already stopped the loopback listener. Existing
                # CONNECT handlers may outlive wait_closed briefly during browser exit.
                pass
            except Exception as error:
                close_errors.append(f"proxy_bridge: {type(error).__name__}: {str(error)[:160]}")
            finally:
                rt["proxy_bridge"] = None

        if context and not rt.get("cdp_disconnect_only"):
            await bounded_close("context", context, timeout=4.0 if provider == "camoufox" else 3.0)
        if browser and browser is not context:
            # CDP attach: close() disconnects; obscura process killed below.
            await bounded_close("browser", browser, timeout=4.0 if provider == "camoufox" else 3.0)
        if provider == "camoufox":
            # Let Playwright deliver subprocess-exit callbacks before the batch
            # asyncio.run() closes its event loop.
            await asyncio.sleep(0.1)
        if playwright is not None:
            try:
                await asyncio.wait_for(playwright.stop(), timeout=3.0)
            except Exception as error:
                close_errors.append(f"playwright: {type(error).__name__}: {str(error)[:160]}")
        try:
            if provider == "obscura" and hasattr(bb, "stop_server"):
                await asyncio.to_thread(bb.delete_browser, profile_id)
            else:
                bb.delete_browser(profile_id)
        except Exception as error:
            close_errors.append(f"delete_browser: {type(error).__name__}: {str(error)[:160]}")
        rt["closed"] = True
        if close_errors:
            await _write_browser_diagnostic(
                "browser_teardown_warning",
                provider=provider,
                profile_id=profile_id,
                proxy=rt.get("original_proxy") or getattr(bb, "_profiles", {}).get(profile_id, {}).get("proxy"),
                launch_kwargs=rt.get("launch_kwargs") if isinstance(rt.get("launch_kwargs"), dict) else None,
                error="; ".join(close_errors),
                extra={"delete": delete, "close_errors": close_errors},
            )
        return



async def human_type(page, selector, text, delay_range=(0.02, 0.06)):
    el = page.locator(selector).first
    await el.click()
    for ch in text:
        await el.type(ch, delay=random.uniform(*delay_range) * 1000)  # nosemgrep: gitlab.bandit.B311
    await asyncio.sleep(random.uniform(0.05, 0.15))  # nosemgrep: gitlab.bandit.B311


async def js_force_value(page, selector: str, text: str) -> bool:
    """Set input value via native value setter + React fiber onChange (no click hang).

    Works on engines where Playwright click/fill actionability hangs (e.g. Obscura).
    Critical: ChatGPT login is React-controlled — DOM value alone is not enough;
    the framework state must update or form submits empty email= params.
    """
    try:
        ok = await page.evaluate(
            """([sel, v]) => {
                // support comma-separated selectors
                let el = null;
                for (const s of String(sel).split(',')) {
                    el = document.querySelector(s.trim());
                    if (el) break;
                }
                if (!el) return {ok:false, reason:'no_el'};

                const setNative = (node, val) => {
                    try {
                        const tracker = node._valueTracker;
                        if (tracker) tracker.setValue(val === '' ? ' ' : '');
                    } catch (e) {}
                    const proto = node.tagName === 'TEXTAREA'
                        ? window.HTMLTextAreaElement.prototype
                        : window.HTMLInputElement.prototype;
                    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                    if (desc && desc.set) desc.set.call(node, val);
                    else node.value = val;
                };

                // Find React fiber props (React 16–19)
                const findReactProps = (node) => {
                    const keys = Object.keys(node);
                    for (const k of keys) {
                        if (k.startsWith('__reactProps$') || k.startsWith('__reactEventHandlers$')) {
                            return node[k];
                        }
                    }
                    // fiber via __reactFiber$
                    for (const k of keys) {
                        if (k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$')) {
                            let fiber = node[k];
                            for (let i = 0; i < 12 && fiber; i++) {
                                const p = fiber.memoizedProps || fiber.pendingProps;
                                if (p && (typeof p.onChange === 'function' || typeof p.onInput === 'function')) {
                                    return p;
                                }
                                fiber = fiber.return;
                            }
                        }
                    }
                    return null;
                };

                try { el.focus(); } catch (e) {}
                try { el.click(); } catch (e) {}

                // Clear first so React sees a change
                setNative(el, '');
                try {
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                } catch (e) {}

                setNative(el, v);

                // Prefer InputEvent (React 17+ synthetic system)
                try {
                    el.dispatchEvent(new InputEvent('input', {
                        bubbles: true, cancelable: true, data: v, inputType: 'insertText'
                    }));
                } catch (e) {
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                }
                el.dispatchEvent(new Event('change', {bubbles: true}));

                // Directly invoke React onChange / onInput with synthetic-like event
                const props = findReactProps(el);
                let fiberCalled = false;
                if (props) {
                    const fakeEvent = {
                        target: el,
                        currentTarget: el,
                        type: 'change',
                        bubbles: true,
                        cancelable: true,
                        defaultPrevented: false,
                        isTrusted: false,
                        nativeEvent: {target: el, type: 'input'},
                        preventDefault() { this.defaultPrevented = true; },
                        stopPropagation() {},
                        persist() {},
                        isDefaultPrevented() { return !!this.defaultPrevented; },
                        isPropagationStopped() { return false; },
                    };
                    try {
                        if (typeof props.onChange === 'function') {
                            props.onChange(fakeEvent);
                            fiberCalled = true;
                        }
                    } catch (e) {}
                    try {
                        if (typeof props.onInput === 'function') {
                            props.onInput({...fakeEvent, type: 'input'});
                            fiberCalled = true;
                        }
                    } catch (e) {}
                }

                // Keyboard-style events some forms listen for
                try {
                    el.dispatchEvent(new KeyboardEvent('keydown', {key: 'a', bubbles: true}));
                    el.dispatchEvent(new KeyboardEvent('keyup', {key: 'a', bubbles: true}));
                } catch (e) {}

                // execCommand path
                try {
                    if ((el.value || '') !== v) {
                        el.focus();
                        document.execCommand('selectAll', false, null);
                        document.execCommand('insertText', false, v);
                    }
                } catch (e) {}

                // blur to trigger validation
                try {
                    el.dispatchEvent(new Event('blur', {bubbles: true}));
                    el.dispatchEvent(new FocusEvent('focusout', {bubbles: true}));
                } catch (e) {}

                return {
                    ok: (el.value || '') === v,
                    value: el.value || '',
                    fiber: fiberCalled,
                    hasProps: !!props,
                };
            }""",
            [selector, text],
        )
        if isinstance(ok, dict):
            return bool(ok.get("ok"))
        return bool(ok)
    except Exception:
        return False


async def js_click(page, selector: str) -> bool:
    """Click via DOM (avoids Playwright actionability hang on Obscura)."""
    try:
        return bool(
            await page.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    el.click();
                    return true;
                }""",
                selector,
            )
        )
    except Exception:
        return False


async def react_fill(page, selector, text, tries=2, delay=6, verbose=True, settle=0.06):
    """Fill React controlled inputs so framework state updates.

    Prefer pure JS setter first — Playwright click/fill hangs on some CDP
    engines (Obscura). Falls back to fill() / keyboard typing.
    """
    el = page.locator(selector).first
    try:
        if await el.count() == 0:
            return False
    except Exception:
        return False

    async def _readback():
        try:
            # Prefer DOM read — more reliable when Playwright input_value stalls
            got = await page.evaluate(
                """(sel) => {
                    let el = null;
                    for (const s of String(sel).split(',')) {
                        el = document.querySelector(s.trim());
                        if (el) break;
                    }
                    return el ? (el.value || '') : '';
                }""",
                selector,
            )
            return (got or "").strip()
        except Exception:
            try:
                return (await el.input_value()).strip()
            except Exception:
                return ""

    for i in range(tries):
        prefer_js = os.environ.get("FINGERPRINT_BROWSER", "").lower() == "obscura"
        if not prefer_js:
            try:
                await el.fill(text, timeout=1500)
            except Exception as e:
                if verbose:
                    print(f"  [填写] fill 路径跳过: {str(e)[:50]}")
            await asyncio.sleep(settle)
            if await _readback() == text:
                return True

        # Obscura needs the DOM/React fallback before Playwright actionability.
        if await js_force_value(page, selector, text):
            await asyncio.sleep(settle)
            if await _readback() == text:
                return True

        # 3) click + keyboard (last resort; may hang on Obscura)
        try:
            await el.click(timeout=1000)
            await el.press("Control+A", timeout=500)
            await el.press("Delete", timeout=500)
            await page.keyboard.type(text, delay=delay)
        except Exception as e:
            if verbose:
                print(f"  [填写] 键盘输入路径跳过: {str(e)[:50]}")
        await asyncio.sleep(settle)
        if await _readback() == text:
            return True
        if verbose:
            print(f"  [填写] 未写入成功（当前='{await _readback()}'），重试 {i+1}/{tries}")
    return False

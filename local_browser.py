# -*- coding: utf-8 -*-
"""Free local fingerprint browsers.

Providers:
  - local / chrome / stealth : Playwright Chromium/Chrome + stealth (always free)
  - camoufox                 : open-source anti-detect Firefox (free, needs fetch)
  - obscura                  : lightweight Rust headless browser via CDP (low RAM)
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess  # nosemgrep: gitlab.bandit.B404
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
ROOT = Path(__file__).resolve().parent

_CHROME_PROBE_LOCK = threading.Lock()
_CHROME_PROBE_CACHE: dict[tuple[str, int], tuple[float, bool, str]] = {}
_CHROME_PROBE_TTL = 300.0


def parse_proxy(proxy_str: str | None) -> dict | None:
    """Parse an HTTP/SOCKS proxy URL for local browser launchers."""
    if not proxy_str:
        return None
    raw = str(proxy_str).strip()
    if not raw:
        return None
    proxy_type = "http"
    lower = raw.lower()
    for prefix in ("socks5h://", "socks5://", "socks://"):
        if lower.startswith(prefix):
            proxy_type = "socks5"
            raw = raw[len(prefix) :]
            break
    else:
        for prefix in ("http://", "https://"):
            if lower.startswith(prefix):
                raw = raw[len(prefix) :]
                break
    raw = raw.replace(",", "@", 1) if "@" not in raw and "," in raw else raw
    authenticated = re.match(r"^(.+):(.+)@(.+):(\d+)$", raw)
    if authenticated:
        return {
            "type": proxy_type,
            "username": authenticated.group(1),
            "password": authenticated.group(2),
            "host": authenticated.group(3),
            "port": authenticated.group(4),
        }
    plain = re.match(r"^(.+):(\d+)$", raw)
    if plain:
        return {
            "type": proxy_type,
            "host": plain.group(1),
            "port": plain.group(2),
            "username": "",
            "password": "",
        }
    return None


def _proxy_for_playwright(proxy: str | None) -> dict | None:
    """Convert proxy string to Playwright proxy dict."""
    parsed = parse_proxy(proxy)
    if not parsed:
        return None
    scheme = parsed.get("type") or "http"
    host = parsed["host"]
    port = parsed["port"]
    server = f"{scheme}://{host}:{port}"
    out: dict[str, str] = {"server": server}
    if parsed.get("username"):
        out["username"] = parsed["username"]
    if parsed.get("password"):
        out["password"] = parsed["password"]
    return out


def _proxy_for_obscura_cli(proxy: str | None) -> str | None:
    """Build a single URL string for `obscura --proxy`."""
    parsed = parse_proxy(proxy)
    if not parsed:
        return None
    scheme = parsed.get("type") or "http"
    host = parsed["host"]
    port = parsed["port"]
    user = parsed.get("username") or ""
    password = parsed.get("password") or ""
    if user:
        from urllib.parse import quote

        return f"{scheme}://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
    return f"{scheme}://{host}:{port}"


def _loopback_host(value: str) -> str:
    host = str(value or "").strip() or "127.0.0.1"
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(f"OBSCURA_HOST must be loopback, got: {host}")
    return host


def _local_http_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"unsupported local CDP URL scheme: {parsed.scheme or '<empty>'}")
    host = (parsed.hostname or "").lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(f"local CDP host must be loopback: {host or '<empty>'}")
    return str(url)


class LocalChrome:
    """Launch local Chromium/Chrome with remote debugging + stealth.

    Provides the profile interface used by open_and_connect / teardown.
    Actual browser launch happens in open_and_connect (async); this class
    only tracks profile metadata for cleanup.
    """

    provider_name = "local"

    def __init__(self, api_base=None):
        self.api_base = api_base or "local://chrome"
        self._profiles: dict[str, dict[str, Any]] = {}
        # filled by open_and_connect after launch
        self._runtime: dict[str, Any] = {}

    def create_browser(self, name="gpt_register", proxy: str | None = None, **kwargs):
        profile_id = f"local_{uuid.uuid4().hex[:12]}"
        self._profiles[profile_id] = {
            "name": name,
            "proxy": proxy or "",
            "created": time.time(),
        }
        print(f"  新本地窗口已创建: {name} (ID: {profile_id}) [free local chrome]")
        return profile_id

    def open_browser(self, profile_id):
        """Return placeholder; real launch is done in common.browser.open_and_connect."""
        meta = self._profiles.get(profile_id) or {}
        return {
            "ws": "",
            "http": "",
            "proxy": meta.get("proxy") or "",
            "name": meta.get("name") or profile_id,
            "local": True,
        }

    def close_browser(self, profile_id):
        rt = self._runtime.pop(profile_id, None)
        if not rt:
            return {"success": True}
        # browser/context closed async in teardown
        return {"success": True, "runtime": rt}

    def delete_browser(self, profile_id):
        self._profiles.pop(profile_id, None)
        self._runtime.pop(profile_id, None)
        print(f"  本地窗口已删除: {profile_id}")
        return {"success": True}

    def list_browsers(self, page=0, page_size=100):
        items = [
            {"id": k, "name": v.get("name", ""), "seq": int(v.get("created", 0))}
            for k, v in self._profiles.items()
        ]
        return {"success": True, "data": {"list": items[:page_size]}}

    def cleanup_browsers(self, keep=0):
        ids = list(self._profiles.keys())
        for pid in ids[keep:]:
            self.delete_browser(pid)
        return max(0, len(ids) - keep)

    def register_runtime(self, profile_id: str, **kwargs):
        self._runtime[profile_id] = kwargs


class CamoufoxBrowser(LocalChrome):
    """Camoufox free anti-detect browser (Firefox-based)."""

    provider_name = "camoufox"

    def __init__(self, api_base=None):
        super().__init__(api_base=api_base or "local://camoufox")
        self._ensure_installed()

    @staticmethod
    def is_available() -> bool:
        """Check the installed kernel without triggering Camoufox downloads."""
        try:
            from camoufox.pkgman import INSTALL_DIR, LAUNCH_FILE, OS_NAME, camoufox_path

            install_dir = Path(str(camoufox_path(download_if_missing=False)))
            launch_file = LAUNCH_FILE.get(OS_NAME) if isinstance(LAUNCH_FILE, dict) else LAUNCH_FILE
            if not launch_file:
                return False
            executable = (install_dir / str(launch_file)).resolve()
            return executable.is_file() and os.access(executable, os.X_OK)
        except Exception:
            return False

    def _ensure_installed(self) -> None:
        if self.is_available():
            return
        raise RuntimeError(
            "Camoufox 浏览器内核未安装。请执行: python3 -m camoufox fetch\n"
            "或改用免费方案 fingerprint.browser: local（本机 Chrome + stealth）"
        )

    def create_browser(self, name="gpt_register", proxy: str | None = None, **kwargs):
        profile_id = f"cfx_{uuid.uuid4().hex[:12]}"
        self._profiles[profile_id] = {
            "name": name,
            "proxy": proxy or "",
            "created": time.time(),
        }
        print(f"  新 Camoufox 窗口已创建: {name} (ID: {profile_id}) [free]")
        return profile_id


class ObscuraBrowser(LocalChrome):
    """Obscura lightweight Rust headless browser (CDP + Playwright).

    Each profile starts its own `obscura serve` process on a free local port
    so concurrent tasks stay isolated (proxy / crash / teardown).
    Binary: bin/obscura (project), or PATH / OBSCURA_BIN.
    """

    provider_name = "obscura"

    def __init__(self, api_base=None):
        super().__init__(api_base=api_base or "local://obscura")
        self._binary = resolve_obscura_binary()
        if not self._binary:
            raise RuntimeError(
                "Obscura 二进制未找到。请放到 "
                f"{ROOT / 'bin' / 'obscura'} 或设置环境变量 OBSCURA_BIN。\n"
                "下载: https://github.com/h4ckf0r0day/obscura/releases "
                "(obscura-x86_64-linux.tar.gz)"
            )
        print(f"  Obscura binary: {self._binary}")

    @staticmethod
    def is_available() -> bool:
        return bool(resolve_obscura_binary())

    def create_browser(self, name="gpt_register", proxy: str | None = None, **kwargs):
        profile_id = f"obs_{uuid.uuid4().hex[:12]}"
        self._profiles[profile_id] = {
            "name": name,
            "proxy": proxy or "",
            "created": time.time(),
        }
        print(f"  新 Obscura 会话已创建: {name} (ID: {profile_id}) [low-mem CDP]")
        return profile_id

    def start_server(self, profile_id: str, proxy: str | None = None) -> dict[str, Any]:
        """Spawn `obscura serve --stealth` and wait until CDP is ready."""
        meta = self._profiles.get(profile_id) or {}
        proxy = proxy if proxy is not None else (meta.get("proxy") or None)
        port = _pick_free_port()
        host = _loopback_host(os.environ.get("OBSCURA_HOST", "127.0.0.1"))
        stealth = os.environ.get("OBSCURA_STEALTH", "1").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }

        cmd = [
            self._binary,
            "serve",
            "--host",
            host,
            "--port",
            str(port),
        ]
        if stealth:
            cmd.append("--stealth")

        proxy_url = _proxy_for_obscura_cli(proxy)
        if proxy_url:
            # Global --proxy must come before subcommand for some builds;
            # serve also accepts --proxy after. Use both-safe form: flag after serve.
            cmd.extend(["--proxy", proxy_url])
            print(f"  Obscura proxy: {proxy_url.split('@')[-1]}")

        log_dir = ROOT / "data" / "obscura_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{profile_id}.log"
        log_fh = open(log_path, "ab", buffering=0)

        env = dict(os.environ)
        # Keep worker binary next to main binary when present
        bin_dir = str(Path(self._binary).resolve().parent)
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")

        print(f"  Obscura 启动 CDP: {host}:{port} stealth={stealth}")
        try:
            proc = subprocess.Popen(
                cmd,  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=bin_dir,
                start_new_session=True,
            )
        except Exception:
            log_fh.close()
            raise

        # Local-only CDP control plane; host is restricted by _loopback_host().
        endpoint = f"http://{host}:{port}"  # nosemgrep: python.lang.security.audit.insecure-transport.requests.request-with-http.request-with-http
        info = {
            "proc": proc,
            "port": port,
            "host": host,
            "endpoint": endpoint,
            "ws": f"ws://{host}:{port}/devtools/browser",  # nosemgrep: javascript.lang.security.detect-insecure-websocket.detect-insecure-websocket
            "log_path": str(log_path),
            "log_fh": log_fh,
            "proxy": proxy or "",
        }
        # Register immediately so every readiness failure has one cleanup owner.
        self._runtime[profile_id] = info
        deadline = time.time() + float(os.environ.get("OBSCURA_READY_TIMEOUT", "20") or 20)
        last_err = ""
        while time.time() < deadline:
            if proc.poll() is not None:
                tail = _tail_file(log_path, 800)
                self.stop_server(profile_id)
                raise RuntimeError(
                    f"Obscura 进程退出 code={proc.returncode}。"
                    f" 日志: {log_path}\n{tail}"
                )
            try:
                resp = requests.get(
                    _local_http_url(f"{endpoint}/json/version"),  # nosemgrep: python.lang.security.audit.insecure-transport.requests.request-with-http.request-with-http
                    timeout=1.0,
                )
                if resp.status_code == 200:
                    break
            except Exception as e:
                last_err = str(e)
            time.sleep(0.15)
        else:
            self.stop_server(profile_id)
            raise RuntimeError(
                f"Obscura CDP 未就绪 ({endpoint}): {last_err or 'timeout'}。"
                f" 日志: {log_path}"
            )

        print(f"  Obscura CDP ready: {endpoint} (pid={proc.pid})")
        return info

    def stop_server(self, profile_id: str) -> None:
        rt = self._runtime.pop(profile_id, None) or {}
        proc = rt.get("proc")
        log_fh = rt.get("log_fh")
        if proc is not None:
            try:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=3)
                    except Exception:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if log_fh is not None:
            try:
                log_fh.close()
            except Exception:
                pass

    def delete_browser(self, profile_id):
        self.stop_server(profile_id)
        self._profiles.pop(profile_id, None)
        print(f"  Obscura 会话已删除: {profile_id}")
        return {"success": True}

    def close_browser(self, profile_id):
        self.stop_server(profile_id)
        return {"success": True}


def resolve_obscura_binary() -> str | None:
    """Locate obscura executable."""
    candidates = [
        os.environ.get("OBSCURA_BIN"),
        str(ROOT / "bin" / "obscura"),
        str(Path.home() / ".local" / "bin" / "obscura"),
        "/usr/local/bin/obscura",
        shutil.which("obscura"),
    ]
    for cand in candidates:
        if not cand:
            continue
        p = Path(cand).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p.resolve())
    return None


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _tail_file(path: Path, max_bytes: int = 800) -> str:
    try:
        data = path.read_bytes()
        if len(data) > max_bytes:
            data = data[-max_bytes:]
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


def get_free_browser_provider(name: str | None = None):
    """Factory for free providers."""
    n = (name or os.environ.get("FINGERPRINT_BROWSER") or "local").strip().lower()
    if n in {"camoufox", "cfx", "fox"}:
        return CamoufoxBrowser()
    if n in {"obscura", "obs", "obscura-browser"}:
        return ObscuraBrowser()
    if n in {"local", "chrome", "stealth", "playwright", "chromium", "free"}:
        return LocalChrome()
    return None


def chrome_executable_candidates() -> list[str]:
    """Return unique Chrome candidates without claiming they can launch."""
    candidates = [
        os.environ.get("CHROME_PATH"),
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            candidates.append(p.chromium.executable_path)
    except Exception:
        pass

    result: list[str] = []
    seen: set[str] = set()
    for cand in candidates:
        if cand and Path(cand).exists():
            resolved = str(Path(cand).resolve())
            if Path(resolved).name == "snap":
                continue
            if resolved not in seen:
                seen.add(resolved)
                result.append(resolved)
    return result


def probe_chrome_executable(path: str, *, timeout: float = 8.0) -> tuple[bool, str]:
    """Run a cached, local-only headless smoke test for one executable."""
    executable = Path(str(path or "")).expanduser()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        return False, "executable missing"
    try:
        stamp = executable.stat().st_mtime_ns
    except OSError as error:
        return False, str(error)
    key = (str(executable.resolve()), stamp)
    now = time.monotonic()
    with _CHROME_PROBE_LOCK:
        cached = _CHROME_PROBE_CACHE.get(key)
        if cached and now - cached[0] < _CHROME_PROBE_TTL:
            return cached[1], cached[2]

        try:
            with tempfile.TemporaryDirectory(prefix="gpt-reg-chrome-probe-") as profile:
                completed = subprocess.run(  # nosemgrep: python.lang.security.audit.subprocess-shell-true.subprocess-shell-true
                    [
                        str(executable),
                        "--headless=new",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-crash-reporter",
                        "--disable-crashpad",
                        f"--user-data-dir={profile}",
                        "--dump-dom",
                        "data:text/html,<p>gpt-register-browser-probe</p>",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=max(2.0, float(timeout)),
                    check=False,
                )
            output = (completed.stdout + completed.stderr).decode("utf-8", errors="replace")
            ok = completed.returncode == 0 and "gpt-register-browser-probe" in output
            detail = "launch probe passed" if ok else f"exit={completed.returncode}: {output[-240:].strip()}"
        except subprocess.TimeoutExpired:
            ok, detail = False, f"launch probe timed out after {timeout:.1f}s"
        except Exception as error:
            ok, detail = False, str(error)[:240]
        _CHROME_PROBE_CACHE[key] = (time.monotonic(), ok, detail)
        return ok, detail


def resolve_chrome_executable(*, verify_launch: bool = False) -> str | None:
    """Return the first existing, optionally launch-verified Chrome candidate."""
    for candidate in chrome_executable_candidates():
        if not verify_launch or probe_chrome_executable(candidate)[0]:
            return candidate
    return None

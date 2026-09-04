"""OpenAI Sentinel Token (PoW) 生成与请求工具函数。

用于密码登录、注册等需要 sentinel token 的流程。
"""
from __future__ import annotations

import base64
import json
import random
import re
import os
import select
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from zoneinfo import ZoneInfo

from register.protocol_turnstile import solve_turnstile_token
from register.protocol_runtime import current_runtime_settings

if TYPE_CHECKING:
    from curl_cffi.requests import Session


SENTINEL_BOOTSTRAP_URL = "https://sentinel.openai.com/backend-api/sentinel/sdk.js"
SENTINEL_FRAME_URL = "https://sentinel.openai.com/backend-api/sentinel/frame.html"
SENTINEL_REQ_URL = "https://sentinel.openai.com/backend-api/sentinel/req"
DEFAULT_SENTINEL_SDK_URL = "https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js"
# Windows 本地化时区显示名（Firefox Date.toString() 括号里的部分）。
# 必须与 timezone_offset_min 保持一致，否则构成自相矛盾的指纹信号。
_WINDOWS_TZ_STANDARD = {
    "Asia/Tokyo": "Japan Standard Time",
    "Asia/Seoul": "Korea Standard Time",
    "Asia/Manila": "Philippine Standard Time",
    "Asia/Singapore": "Singapore Standard Time",
    "Asia/Bangkok": "Indochina Time",
    "Asia/Ho_Chi_Minh": "SE Asia Standard Time",
    "Australia/Sydney": "AUS Eastern Standard Time",
    "America/Sao_Paulo": "E. South America Standard Time",
    "America/New_York": "Eastern Standard Time",
    "America/Toronto": "Eastern Standard Time",
    "Europe/London": "GMT Standard Time",
    "Europe/Berlin": "W. Europe Standard Time",
    "Europe/Paris": "Romance Standard Time",
    "Europe/Istanbul": "Turkey Time",
    "Asia/Kolkata": "India Standard Time",
}
_WINDOWS_TZ_DAYLIGHT = {
    "Australia/Sydney": "AUS Eastern Daylight Time",
    "America/New_York": "Eastern Daylight Time",
    "America/Toronto": "Eastern Daylight Time",
    "Europe/London": "GMT Daylight Time",
    "Europe/Berlin": "W. Europe Daylight Time",
    "Europe/Paris": "Romance Daylight Time",
}


def _windows_timezone_display(timezone_name: str, *, offset_minutes: int) -> str:
    """Return the Windows zone label a real Firefox would render for this zone."""
    name = str(timezone_name or "").strip()
    standard = _WINDOWS_TZ_STANDARD.get(name)
    if standard is None:
        # 未知 IANA 时区：保留旧行为，避免臆造更假的名称。
        return "Japan Standard Time" if offset_minutes == -540 else "Coordinated Universal Time"
    daylight = _WINDOWS_TZ_DAYLIGHT.get(name)
    if daylight:
        try:
            now = datetime.now(ZoneInfo(name))
            dst = now.dst()
            if dst is not None and dst.total_seconds() > 0:
                return daylight
        except Exception:
            pass
    return standard


_SENTINEL_WORKER = Path(__file__).resolve().parent / "sentinel_vm" / "runtime_worker.js"
# Each worker reserves a full Node/V8 runtime and the live SDK can create several
# timers and VM contexts. Keep this local compute gate bounded without reducing
# the registration task pool itself.
_SENTINEL_WORKER_CONCURRENCY = 4
_SENTINEL_WORKER_GATE = threading.BoundedSemaphore(_SENTINEL_WORKER_CONCURRENCY)
_SDK_CACHE_LOCK = threading.Lock()
_SDK_LOAD_LOCK = threading.Lock()
_SDK_URL_CACHE: tuple[float, str] | None = None
_SDK_SOURCE_CACHE: dict[str, tuple[float, str]] = {}

_PERSISTENT_WORKER_POOL_LOCK = threading.Lock()
_PERSISTENT_WORKERS: list["_PersistentSentinelWorker"] = []
_PERSISTENT_WORKER_CURSOR = 0


class _SentinelPersistentTransportError(RuntimeError):
    pass


class _PersistentSentinelWorker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._proc: subprocess.Popen | None = None

    def _start_locked(self) -> subprocess.Popen:
        if not _SENTINEL_WORKER.exists():
            raise RuntimeError(f"sentinel_runtime_worker_missing: {_SENTINEL_WORKER}")
        proc = self._proc
        if proc is not None and proc.poll() is None and proc.stdin and proc.stdout:
            return proc
        self.close()
        try:
            proc = subprocess.Popen(
                ["node", str(_SENTINEL_WORKER), "--server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as error:
            raise _SentinelPersistentTransportError(
                f"sentinel_persistent_worker_spawn_failed: {error}"
            ) from error
        self._proc = proc
        return proc

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.5)
                except Exception:
                    proc.kill()
                    proc.wait(timeout=1.0)
        except Exception:
            pass

    def run(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        with self._lock:
            proc = self._start_locked()
            if not proc.stdin or not proc.stdout:
                self.close()
                raise _SentinelPersistentTransportError("sentinel_persistent_worker_pipe_missing")
            self._seq += 1
            request_id = self._seq
            try:
                proc.stdin.write(
                    json.dumps(
                        {"id": request_id, "payload": payload},
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                proc.stdin.flush()
            except Exception as error:
                self.close()
                raise _SentinelPersistentTransportError(
                    f"sentinel_persistent_worker_write_failed: {error}"
                ) from error

            fd = proc.stdout.fileno()
            deadline = time.monotonic() + max(0.1, float(timeout))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.close()
                    raise _SentinelPersistentTransportError(
                        f"sentinel_persistent_worker_timeout: timeout={timeout:.1f}s flow={payload.get('flow') or '?'}"
                    )
                if proc.poll() is not None:
                    stderr = ""
                    try:
                        stderr = (proc.stderr.read() if proc.stderr else "") or ""
                    except Exception:
                        pass
                    self.close()
                    raise _SentinelPersistentTransportError(
                        f"sentinel_persistent_worker_exited: code={proc.returncode} stderr={stderr[:800]}"
                    )
                ready, _w, _x = select.select([fd], [], [], min(remaining, 0.5))
                if not ready:
                    continue
                line = proc.stdout.readline()
                if not line:
                    self.close()
                    raise _SentinelPersistentTransportError("sentinel_persistent_worker_eof")
                try:
                    message = json.loads(line)
                except Exception as error:
                    self.close()
                    raise _SentinelPersistentTransportError(
                        f"sentinel_persistent_worker_invalid_json: {error}: {line[:300]}"
                    ) from error
                if message.get("id") != request_id:
                    continue
                if not message.get("ok"):
                    raise RuntimeError(
                        "sentinel_sdk_vm_failed: persistent stderr="
                        + str(message.get("error") or "unknown error")[:1200]
                    )
                result = message.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError("sentinel_sdk_vm_non_object")
                return result


def _get_persistent_worker() -> _PersistentSentinelWorker:
    global _PERSISTENT_WORKER_CURSOR
    runtime = current_runtime_settings()
    size = max(1, min(8, int(getattr(runtime, "sentinel_worker_pool_size", 4) or 4)))
    with _PERSISTENT_WORKER_POOL_LOCK:
        while len(_PERSISTENT_WORKERS) < size:
            _PERSISTENT_WORKERS.append(_PersistentSentinelWorker())
        if len(_PERSISTENT_WORKERS) > size:
            extra = _PERSISTENT_WORKERS[size:]
            del _PERSISTENT_WORKERS[size:]
            for worker in extra:
                worker.close()
        worker = _PERSISTENT_WORKERS[_PERSISTENT_WORKER_CURSOR % len(_PERSISTENT_WORKERS)]
        _PERSISTENT_WORKER_CURSOR += 1
        return worker


def close_persistent_sentinel_workers() -> None:
    with _PERSISTENT_WORKER_POOL_LOCK:
        workers = list(_PERSISTENT_WORKERS)
        _PERSISTENT_WORKERS.clear()
    for worker in workers:
        worker.close()



def clear_sentinel_sdk_cache() -> None:
    """Clear cached SDK source, mainly for tests and explicit refreshes."""
    global _SDK_URL_CACHE
    with _SDK_CACHE_LOCK:
        _SDK_URL_CACHE = None
        _SDK_SOURCE_CACHE.clear()


class SentinelTokenGenerator:
    """Sentinel Token 生成器（PoW - Proof of Work）。"""
    MAX_ATTEMPTS = 500_000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(
        self,
        device_id: str,
        ua: str,
        *,
        sid: str = "",
        device_profile: dict[str, str] | None = None,
    ):
        self.device_id = device_id
        self.user_agent = ua
        self.sid = str(sid or uuid.uuid4())
        self.device_profile = dict(device_profile or {})

    @staticmethod
    def _fnv1a_32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= h >> 16
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= h >> 16
        return format(h & 0xFFFFFFFF, "08x")

    def _get_config(self) -> list:
        perf_now = random.uniform(1000, 50000)
        screen = str(self.device_profile.get("screen") or "1920x1080")
        language = str(self.device_profile.get("accept_language") or "en-US").split(",", 1)[0]
        try:
            offset_minutes = int(self.device_profile.get("timezone_offset_min") or 0)
        except (TypeError, ValueError):
            offset_minutes = 0
        sign = "+" if offset_minutes <= 0 else "-"
        absolute = abs(offset_minutes)
        hours, minutes = divmod(absolute, 60)
        zone_name = _windows_timezone_display(
            str(self.device_profile.get("timezone") or ""),
            offset_minutes=offset_minutes,
        )
        local_stamp = time.strftime(
            f"%a %b %d %Y %H:%M:%S GMT{sign}{hours:02d}{minutes:02d} ({zone_name})",
            time.gmtime(time.time() - offset_minutes * 60),
        )
        try:
            hardware_concurrency = max(
                1, int(self.device_profile.get("hardware_concurrency") or 8)
            )
        except (TypeError, ValueError):
            hardware_concurrency = 8
        return [
            screen,
            local_stamp,
            4294705152,
            random.random(),
            self.user_agent,
            "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js",
            None,
            None,
            language,
            random.random(),
            random.choice(["vendorSub-undefined", "plugins-undefined", "mimeTypes-undefined", "hardwareConcurrency-undefined"]),
            random.choice(["location", "implementation", "URL", "documentURI", "compatMode"]),
            random.choice(["Object", "Function", "Array", "Number", "parseFloat", "undefined"]),
            perf_now,
            self.sid,
            "",
            hardware_concurrency,
            time.time() * 1000 - perf_now,
        ]

    @staticmethod
    def _b64(data) -> str:
        return base64.b64encode(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).decode("ascii")

    def generate_requirements_token(self) -> str:
        data = self._get_config()
        data[3] = 1
        data[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._b64(data)

    def generate_token(self, seed: str, difficulty: str) -> str:
        start = time.time()
        data = self._get_config()
        difficulty = str(difficulty or "0")
        for i in range(self.MAX_ATTEMPTS):
            data[3] = i
            data[9] = round((time.time() - start) * 1000)
            payload = self._b64(data)
            if self._fnv1a_32(seed + payload)[: len(difficulty)] <= difficulty:
                return "gAAAAAB" + payload + "~S"
        return "gAAAAAB" + self.ERROR_PREFIX + self._b64(str(None))


def _diagnostic_emit(sink: Callable[[str], None] | None, message: str) -> None:
    if not callable(sink):
        return
    try:
        sink(message)
    except Exception:
        pass


# ── 默认 Firefox User-Agent；Firefox 不发送 Chromium client hints ──
DEFAULT_SENTINEL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "rv:147.0 Gecko/20100101 Firefox/147.0"
)
DEFAULT_SENTINEL_SEC_CH_UA = ""


def _build_legacy_sentinel_token(
    session: "Session",
    device_id: str,
    flow: str,
    *,
    user_agent: str = "",
    sec_ch_ua: str = "",
    diagnostic: Callable[[str], None] | None = None,
    sid: str = "",
    device_profile: dict[str, str] | None = None,
) -> tuple[str, str]:
    """请求 sentinel token 并返回 (sentinel_header_value, oai_sc_cookie_value)。

    Args:
        session: curl_cffi Session 实例
        device_id: 设备 ID
        flow: 流程标识（如 "password_verify", "username_password_create" 等）
        user_agent: 可选的 User-Agent 覆盖
        sec_ch_ua: 可选的 sec-ch-ua 覆盖

    Returns:
        (openai-sentinel-token header value, oai-sc cookie value) 元组

    Raises:
        RuntimeError: sentinel 请求失败
    """
    ua = user_agent or DEFAULT_SENTINEL_USER_AGENT
    ch_ua = sec_ch_ua or DEFAULT_SENTINEL_SEC_CH_UA
    generator = SentinelTokenGenerator(
        device_id,
        ua,
        sid=sid,
        device_profile=device_profile,
    )
    requirements_token = generator.generate_requirements_token()
    _diagnostic_emit(
        diagnostic,
        f"Sentinel 请求 flow={flow} sid={generator.sid} did={device_id} ua={ua}",
    )
    req_started = time.monotonic()
    sentinel_headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html",
        "Origin": "https://sentinel.openai.com",
        "User-Agent": ua,
    }
    if ch_ua:
        sentinel_headers["sec-ch-ua"] = ch_ua
        sentinel_headers["sec-ch-ua-mobile"] = "?0"
        sentinel_headers["sec-ch-ua-platform"] = '"Windows"'
    resp = session.post(
        "https://sentinel.openai.com/backend-api/sentinel/req",
        data=json.dumps({"p": requirements_token, "id": device_id, "flow": flow}),
        headers=sentinel_headers,
        timeout=current_runtime_settings().timeout_for(
            current_runtime_settings().sentinel_http_timeout
        ),
        verify=current_runtime_settings().tls_verify,
    )

    try:
        data = resp.json() if resp.text else {}
    except Exception:
        fallback = json.dumps(
            {"p": generator.generate_requirements_token(), "t": "", "c": "", "id": device_id, "flow": flow},
            separators=(",", ":"),
        )
        return fallback, ""

    token = str(data.get("token") or "").strip()
    if resp.status_code != 200 or not token:
        raise RuntimeError(f"sentinel_req_failed_{resp.status_code}")
    pow_data = data.get("proofofwork") or {}
    turnstile_data = data.get("turnstile") or {}
    _diagnostic_emit(
        diagnostic,
        "Sentinel 响应 "
        f"http={resp.status_code} elapsed={time.monotonic() - req_started:.2f}s "
        f"pow_required={bool(pow_data.get('required'))} "
        f"difficulty={str(pow_data.get('difficulty') or '-')} "
        f"turnstile_required={bool(turnstile_data.get('required'))}",
    )
    pow_started = time.monotonic()
    p_value = (
        generator.generate_token(str(pow_data.get("seed") or ""), str(pow_data.get("difficulty") or "0"))
        if pow_data.get("required") and pow_data.get("seed")
        else generator.generate_requirements_token()
    )
    if pow_data.get("required"):
        _diagnostic_emit(
            diagnostic,
            f"Sentinel PoW 完成 elapsed={time.monotonic() - pow_started:.2f}s token_len={len(p_value)}",
        )
    turnstile_token = ""
    if turnstile_data.get("required") and turnstile_data.get("dx"):
        # 对齐 sentinel sdk.js 的 token(flow) 行为：
        # t 字段由 req 阶段返回的 turnstile.dx 与本次 requirements_token 计算得到。
        turnstile_token = solve_turnstile_token(
            str(turnstile_data.get("dx") or ""), requirements_token
        ) or ""
        if not turnstile_token:
            raise RuntimeError("sentinel_turnstile_token_failed")
        _diagnostic_emit(
            diagnostic,
            f"Sentinel Turnstile Token 完成 token_len={len(turnstile_token)}",
        )
    sentinel_value = json.dumps({"p": p_value, "t": turnstile_token, "c": token, "id": device_id, "flow": flow}, separators=(",", ":"))
    # oai-sc cookie = "0" + sentinel token "c" value (the challenge token from the server)
    oai_sc_value = "0" + token
    return sentinel_value, oai_sc_value


def _build_legacy_sentinel_with_so_token(
    session: "Session",
    device_id: str,
    flow: str,
    *,
    user_agent: str = "",
    sec_ch_ua: str = "",
    diagnostic: Callable[[str], None] | None = None,
    sid: str = "",
    device_profile: dict[str, str] | None = None,
) -> tuple[str, str, str]:
    """请求 sentinel token 并返回 (sentinel_header_value, so_token_header_value, oai_sc_cookie_value)。

    Args:
        session: curl_cffi Session 实例
        device_id: 设备 ID
        flow: 流程标识（如 "oauth_create_account" 等）
        user_agent: 可选的 User-Agent 覆盖
        sec_ch_ua: 可选的 sec-ch-ua 覆盖

    Returns:
        (openai-sentinel-token, openai-sentinel-so-token, oai-sc cookie) 元组

    Raises:
        RuntimeError: sentinel 请求失败
    """
    ua = user_agent or DEFAULT_SENTINEL_USER_AGENT
    ch_ua = sec_ch_ua or DEFAULT_SENTINEL_SEC_CH_UA
    generator = SentinelTokenGenerator(
        device_id,
        ua,
        sid=sid,
        device_profile=device_profile,
    )
    requirements_token = generator.generate_requirements_token()
    _diagnostic_emit(
        diagnostic,
        f"Sentinel 请求 flow={flow} sid={generator.sid} did={device_id} ua={ua}",
    )
    req_started = time.monotonic()
    sentinel_headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html",
        "Origin": "https://sentinel.openai.com",
        "User-Agent": ua,
    }
    if ch_ua:
        sentinel_headers["sec-ch-ua"] = ch_ua
        sentinel_headers["sec-ch-ua-mobile"] = "?0"
        sentinel_headers["sec-ch-ua-platform"] = '"Windows"'
    resp = session.post(
        "https://sentinel.openai.com/backend-api/sentinel/req",
        data=json.dumps({"p": requirements_token, "id": device_id, "flow": flow}),
        headers=sentinel_headers,
        timeout=current_runtime_settings().timeout_for(
            current_runtime_settings().sentinel_http_timeout
        ),
        verify=current_runtime_settings().tls_verify,
    )

    try:
        data = resp.json() if resp.text else {}
    except Exception:
        fallback = json.dumps(
            {"p": generator.generate_requirements_token(), "t": "", "c": "", "id": device_id, "flow": flow},
            separators=(",", ":"),
        )
        return fallback, "", ""

    token = str(data.get("token") or "").strip()
    if resp.status_code != 200 or not token:
        raise RuntimeError(f"sentinel_req_failed_{resp.status_code}")

    pow_data = data.get("proofofwork") or {}
    turnstile_data = data.get("turnstile") or {}
    _diagnostic_emit(
        diagnostic,
        "Sentinel 响应 "
        f"http={resp.status_code} elapsed={time.monotonic() - req_started:.2f}s "
        f"pow_required={bool(pow_data.get('required'))} "
        f"difficulty={str(pow_data.get('difficulty') or '-')} "
        f"turnstile_required={bool(turnstile_data.get('required'))}",
    )
    pow_started = time.monotonic()
    p_value = (
        generator.generate_token(str(pow_data.get("seed") or ""), str(pow_data.get("difficulty") or "0"))
        if pow_data.get("required") and pow_data.get("seed")
        else generator.generate_requirements_token()
    )
    if pow_data.get("required"):
        _diagnostic_emit(
            diagnostic,
            f"Sentinel PoW 完成 elapsed={time.monotonic() - pow_started:.2f}s token_len={len(p_value)}",
        )

    turnstile_token = ""
    if turnstile_data.get("required") and turnstile_data.get("dx"):
        turnstile_token = solve_turnstile_token(
            str(turnstile_data.get("dx") or ""), requirements_token
        ) or ""
        if not turnstile_token:
            raise RuntimeError("sentinel_turnstile_token_failed")
        _diagnostic_emit(
            diagnostic,
            f"Sentinel Turnstile Token 完成 token_len={len(turnstile_token)}",
        )

    # turnstile.dx 只生成 Sentinel 内部的 t。observer SO 需要独立的
    # collector_dx/snapshot_dx 计算结果，不能复用 t；当前响应缺少该结果时省略 SO 头。
    sentinel_value = json.dumps({"p": p_value, "t": turnstile_token, "c": token, "id": device_id, "flow": flow}, separators=(",", ":"))
    observer_header = ""
    # oai-sc cookie = "0" + sentinel token "c" value
    oai_sc_value = "0" + token

    return sentinel_value, observer_header, oai_sc_value


def extract_sentinel_sdk_url(source: str) -> str:
    match = re.search(
        r"https://sentinel\.openai\.com/sentinel/[^'\"<>]+/sdk\.js",
        str(source or ""),
    )
    return match.group(0) if match else ""


def _sentinel_version(url: str) -> str:
    match = re.search(r"/sentinel/([^/]+)/sdk\.js", str(url or ""))
    return match.group(1) if match else ""


def _cookie_header(session: Any, device_id: str) -> str:
    values: dict[str, str] = {}
    try:
        jar = getattr(session.cookies, "jar", session.cookies)
        for cookie in list(jar):
            name = str(getattr(cookie, "name", "") or "")
            value = str(getattr(cookie, "value", "") or "")
            if name:
                values[name] = value
    except Exception:
        try:
            values.update(
                {
                    str(name): str(value)
                    for name, value in session.cookies.get_dict().items()
                }
            )
        except Exception:
            pass
    values["oai-did"] = device_id
    return "; ".join(f"{name}={value}" for name, value in values.items() if value)


def _screen_profile(device_profile: dict[str, str] | None) -> tuple[int, int]:
    raw = str((device_profile or {}).get("screen") or "1920x1080")
    try:
        width, height = (int(part) for part in raw.lower().split("x", 1))
        return max(800, width), max(600, height)
    except (TypeError, ValueError):
        return 1920, 1080


def _sdk_profile(
    *,
    user_agent: str,
    device_profile: dict[str, str] | None,
    page_url: str,
    referer: str,
) -> dict[str, Any]:
    profile = dict(device_profile or {})
    width, height = _screen_profile(profile)
    accept_language = str(profile.get("accept_language") or "en-US")
    language = accept_language.split(",", 1)[0]
    languages: list[str] = []
    for item in accept_language.split(","):
        value = item.split(";", 1)[0].strip()
        if value and value not in languages:
            languages.append(value)
    try:
        hardware_concurrency = max(1, int(profile.get("hardware_concurrency") or 8))
    except (TypeError, ValueError):
        hardware_concurrency = 8
    try:
        device_memory = max(1, int(profile.get("device_memory") or 8))
    except (TypeError, ValueError):
        device_memory = 8
    try:
        max_touch_points = max(0, int(profile.get("max_touch_points") or 0))
    except (TypeError, ValueError):
        max_touch_points = 0
    timezone_offset = str(profile.get("timezone_offset_min") or "0")
    try:
        timezone_offset_minutes = int(timezone_offset)
    except (TypeError, ValueError):
        timezone_offset_minutes = 0
    return {
        "url": page_url,
        "referer": referer,
        "timezone": str(profile.get("timezone") or "UTC"),
        "timezoneOffsetMinutes": timezone_offset_minutes,
        "navigator": {
            "userAgent": user_agent,
            "language": language,
            "languages": languages or [language],
            "platform": str(profile.get("platform") or "Win32"),
            "vendor": str(profile.get("navigator_vendor") or ""),
            "hardwareConcurrency": hardware_concurrency,
            "deviceMemory": device_memory,
            "maxTouchPoints": max_touch_points,
        },
        "screen": {
            "width": width,
            "height": height,
            "availWidth": width,
            "availHeight": max(600, height - 40),
            "innerWidth": max(800, width - 17),
            "innerHeight": max(600, height - 111),
            "outerWidth": width,
            "outerHeight": height,
            "colorDepth": 24,
            "pixelDepth": 24,
            "devicePixelRatio": float(profile.get("device_pixel_ratio") or 1),
        },
        "webgl": {
            "vendor": str(profile.get("webgl_vendor") or "Intel Inc."),
            "renderer": str(
                profile.get("webgl_renderer")
                or "ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"
            ),
        },
        "historyLength": 3,
    }



def _run_sentinel_sdk(payload: dict[str, Any]) -> dict[str, Any]:
    runtime = current_runtime_settings()
    if getattr(runtime, "sentinel_worker_persistent", True):
        worker_timeout = runtime.timeout_for(runtime.sentinel_worker_timeout)
        try:
            return _get_persistent_worker().run(payload, worker_timeout)
        except _SentinelPersistentTransportError:
            # Keep protocol registration alive if the resident process was
            # killed by memory pressure or a pipe race; one-shot execution is
            # slower but matches the old behavior.
            return _run_sentinel_sdk_oneshot(payload)
    return _run_sentinel_sdk_oneshot(payload)


def _run_sentinel_sdk_oneshot(payload: dict[str, Any]) -> dict[str, Any]:
    if not _SENTINEL_WORKER.exists():
        raise RuntimeError(f"sentinel_runtime_worker_missing: {_SENTINEL_WORKER}")
    runtime = current_runtime_settings()
    worker_timeout = runtime.timeout_for(runtime.sentinel_worker_timeout)
    completed = None
    for worker_attempt in range(2):
        acquired = _SENTINEL_WORKER_GATE.acquire(timeout=worker_timeout)
        if not acquired:
            raise RuntimeError(
                "sentinel_sdk_vm_capacity_timeout: "
                f"active_limit={_SENTINEL_WORKER_CONCURRENCY}"
            )
        try:
            try:
                completed = subprocess.run(
                    ["node", str(_SENTINEL_WORKER)],
                    input=json.dumps(payload, separators=(",", ":")),
                    text=True,
                    capture_output=True,
                    timeout=worker_timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise RuntimeError(
                    "sentinel_sdk_vm_timeout: "
                    f"timeout={worker_timeout:.1f}s flow={payload.get('flow') or '?'}"
                ) from error
            except OSError as error:
                raise RuntimeError(f"sentinel_sdk_vm_spawn_failed: {error}") from error
        finally:
            _SENTINEL_WORKER_GATE.release()

        if completed.returncode == 0:
            break

        signal_name = ""
        if completed.returncode < 0:
            try:
                signal_name = signal.Signals(-completed.returncode).name
            except ValueError:
                signal_name = f"SIG{-completed.returncode}"
        elif completed.returncode >= 128:
            signal_number = completed.returncode - 128
            try:
                signal_name = signal.Signals(signal_number).name
            except ValueError:
                signal_name = f"SIG{signal_number}"
        detail = (
            f"returncode={completed.returncode}"
            + (f" signal={signal_name}" if signal_name else "")
            + f" flow={payload.get('flow') or '?'} attempt={worker_attempt + 1}/2"
        )
        # A native V8 abort is generally transient under process pressure. A
        # fresh process is cheap and avoids carrying a damaged VM into the
        # next protocol request; ordinary SDK exit codes stay fail-fast.
        if signal_name and worker_attempt == 0:
            time.sleep(min(0.25, runtime.timeout_for(0.25)))
            continue
        raise RuntimeError(
            "sentinel_sdk_vm_failed: "
            + detail
            + " stderr="
            + str(completed.stderr or completed.stdout or "unknown error")[:1200]
        )
    if completed is None or completed.returncode != 0:
        raise RuntimeError("sentinel_sdk_vm_failed: worker did not return a result")
    try:
        data = json.loads(completed.stdout)
    except Exception as error:
        raise RuntimeError(f"sentinel_sdk_vm_invalid_json: {error}") from error
    if not isinstance(data, dict):
        raise RuntimeError("sentinel_sdk_vm_non_object")
    return data


def _sentinel_headers(
    *,
    user_agent: str,
    sec_ch_ua: str,
    accept_language: str,
    referer: str,
    content_type: str = "",
) -> dict[str, str]:
    headers = {
        "accept": "*/*",
        "accept-language": accept_language,
        "referer": referer,
        "user-agent": user_agent,
        "sec-fetch-site": "same-origin" if "sentinel.openai.com" in referer else "cross-site",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }
    if sec_ch_ua:
        headers.update(
            {
                "sec-ch-ua": sec_ch_ua,
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            }
        )
    if content_type:
        headers["content-type"] = content_type
    return headers


def _load_live_sdk(
    session: Any,
    *,
    user_agent: str,
    sec_ch_ua: str,
    accept_language: str,
) -> tuple[str, str]:
    global _SDK_URL_CACHE
    runtime = current_runtime_settings()
    cache_ttl = max(0.0, float(runtime.sentinel_sdk_cache_ttl))
    now = time.monotonic()
    sdk_url = ""
    if cache_ttl:
        with _SDK_CACHE_LOCK:
            if _SDK_URL_CACHE is not None and _SDK_URL_CACHE[0] > now:
                sdk_url = _SDK_URL_CACHE[1]

    headers = _sentinel_headers(
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        accept_language=accept_language,
        referer="https://auth.openai.com/",
    )
    if cache_ttl and sdk_url:
        with _SDK_CACHE_LOCK:
            cached_source = _SDK_SOURCE_CACHE.get(sdk_url)
            if cached_source is not None and cached_source[0] > now:
                return sdk_url, cached_source[1]

    # Only one registration task performs a cold bootstrap/download. Waiting
    # tasks re-check the shared cache after acquiring this single-flight lock.
    with _SDK_LOAD_LOCK:
        now = time.monotonic()
        if cache_ttl:
            with _SDK_CACHE_LOCK:
                if _SDK_URL_CACHE is not None and _SDK_URL_CACHE[0] > now:
                    sdk_url = _SDK_URL_CACHE[1]
                cached_source = _SDK_SOURCE_CACHE.get(sdk_url) if sdk_url else None
                if cached_source is not None and cached_source[0] > now:
                    return sdk_url, cached_source[1]

        if not sdk_url:
            bootstrap = session.get(
                SENTINEL_BOOTSTRAP_URL,
                headers=headers,
                timeout=runtime.timeout_for(runtime.sentinel_http_timeout),
                verify=runtime.tls_verify,
            )
            if int(getattr(bootstrap, "status_code", 0) or 0) >= 400:
                raise RuntimeError(
                    f"sentinel_sdk_bootstrap_http_{getattr(bootstrap, 'status_code', 'unknown')}"
                )
            sdk_url = extract_sentinel_sdk_url(
                str(getattr(bootstrap, "text", "") or "")
            ) or DEFAULT_SENTINEL_SDK_URL
            if cache_ttl:
                with _SDK_CACHE_LOCK:
                    _SDK_URL_CACHE = (time.monotonic() + cache_ttl, sdk_url)

        sdk_response = session.get(
            sdk_url,
            headers=headers,
            timeout=runtime.timeout_for(runtime.sentinel_http_timeout),
            verify=runtime.tls_verify,
        )
        sdk_source = str(getattr(sdk_response, "text", "") or "")
        if int(getattr(sdk_response, "status_code", 0) or 0) >= 400 or not sdk_source:
            raise RuntimeError(
                f"sentinel_sdk_http_{getattr(sdk_response, 'status_code', 'unknown')}"
            )
        if cache_ttl:
            with _SDK_CACHE_LOCK:
                _SDK_SOURCE_CACHE[sdk_url] = (
                    time.monotonic() + cache_ttl,
                    sdk_source,
                )
        return sdk_url, sdk_source


def _decoded_vm_error(value: str) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    try:
        decoded = base64.b64decode(
            token + "=" * (-len(token) % 4), validate=True
        ).decode("utf-8")
    except Exception:
        return ""
    return decoded[:300] if re.match(r"^\d+:\s*(?:TypeError|ReferenceError|Error):", decoded) else ""


def _flow_page(flow: str) -> tuple[str, str]:
    if flow == "username_password_create":
        return (
            "https://auth.openai.com/create-account/password",
            "https://auth.openai.com/email-verification",
        )
    if flow == "authorize_continue":
        return (
            "https://auth.openai.com/log-in",
            "https://chatgpt.com/",
        )
    if flow == "email_otp_validate":
        return (
            "https://auth.openai.com/email-verification",
            "https://auth.openai.com/email-verification",
        )
    if flow == "password_reset":
        return (
            "https://auth.openai.com/reset-password/new-password",
            "https://auth.openai.com/email-verification",
        )
    return (
        "https://auth.openai.com/about-you",
        "https://auth.openai.com/email-verification",
    )


def _build_live_sentinel(
    session: Any,
    device_id: str,
    flow: str,
    *,
    user_agent: str,
    sec_ch_ua: str,
    diagnostic: Callable[[str], None] | None,
    device_profile: dict[str, str] | None,
) -> tuple[str, str, str]:
    sdk_url, sdk_source = _load_live_sdk(
        session,
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        accept_language=str(
            (device_profile or {}).get("accept_language") or "en-US,en;q=0.9"
        ),
    )
    page_url, referer = _flow_page(flow)
    document_cookie = _cookie_header(session, device_id)
    profile = _sdk_profile(
        user_agent=user_agent,
        device_profile=device_profile,
        page_url=page_url,
        referer=referer,
    )
    proof_result = _run_sentinel_sdk(
        {
            "mode": "proof",
            "sdk": sdk_source,
            "sdkUrl": sdk_url,
            "flow": flow,
            "userAgent": user_agent,
            "language": profile["navigator"]["language"],
            "documentCookie": document_cookie,
            "profile": profile,
        }
    )
    proof = str(proof_result.get("proof") or "")
    if not proof:
        raise RuntimeError("sentinel_sdk_empty_requirements_proof")
    version = _sentinel_version(sdk_url)
    _diagnostic_emit(
        diagnostic,
        f"Sentinel 实时 SDK flow={flow} version={version or '?'} proof_len={len(proof)}",
    )
    response = session.post(
        SENTINEL_REQ_URL,
        data=json.dumps(
            {"p": proof, "id": device_id, "flow": flow},
            separators=(",", ":"),
        ),
        headers={
            **_sentinel_headers(
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                accept_language=str(
                    (device_profile or {}).get("accept_language")
                    or "en-US,en;q=0.9"
                ),
                referer=f"{SENTINEL_FRAME_URL}?sv={version}",
                content_type="text/plain;charset=UTF-8",
            ),
            "origin": "https://sentinel.openai.com",
        },
        timeout=current_runtime_settings().timeout_for(
            current_runtime_settings().sentinel_http_timeout
        ),
        verify=current_runtime_settings().tls_verify,
    )
    try:
        challenge = response.json()
    except Exception as error:
        raise RuntimeError(f"sentinel_req_invalid_json: {error}") from error
    if int(getattr(response, "status_code", 0) or 0) != 200 or not isinstance(challenge, dict):
        raise RuntimeError(
            f"sentinel_req_failed_{getattr(response, 'status_code', 'unknown')}"
        )
    solved = _run_sentinel_sdk(
        {
            "mode": "token",
            "sdk": sdk_source,
            "sdkUrl": sdk_url,
            "flow": flow,
            "userAgent": user_agent,
            "language": profile["navigator"]["language"],
            "documentCookie": document_cookie,
            "cachedChatReq": challenge,
            "cachedProof": proof,
            "behaviorDurationMs": current_runtime_settings().sentinel_behavior_duration_ms,
            "profile": profile,
        }
    )
    token = str(solved.get("token") or "")
    so_token = str(solved.get("soToken") or "")
    try:
        token_payload = json.loads(token)
    except Exception as error:
        raise RuntimeError(f"sentinel_sdk_invalid_token: {error}") from error
    expected = {
        "c": str(challenge.get("token") or ""),
        "id": device_id,
        "flow": flow,
    }
    for key, value in expected.items():
        if value and str(token_payload.get(key) or "") != value:
            raise RuntimeError(f"sentinel_sdk_{key}_binding_mismatch")
    if not str(token_payload.get("p") or ""):
        raise RuntimeError("sentinel_sdk_missing_enforcement_proof")
    t_value = str(token_payload.get("t") or "")
    if (challenge.get("turnstile") or {}).get("required"):
        if not t_value:
            raise RuntimeError("sentinel_sdk_missing_turnstile_proof")
        vm_error = _decoded_vm_error(t_value)
        if vm_error:
            raise RuntimeError(f"sentinel_turnstile_vm_error: {vm_error}")
    if (challenge.get("so") or {}).get("required"):
        try:
            so_payload = json.loads(so_token)
        except Exception as error:
            raise RuntimeError(f"sentinel_sdk_invalid_so_token: {error}") from error
        if not str(so_payload.get("so") or ""):
            raise RuntimeError("sentinel_sdk_missing_so_proof")
        for key, value in expected.items():
            if value and str(so_payload.get(key) or "") != value:
                raise RuntimeError(f"sentinel_so_{key}_binding_mismatch")
    _diagnostic_emit(
        diagnostic,
        "Sentinel SDK 完成 "
        f"t_len={len(t_value)} so_len={len(so_token)} "
        f"turnstile_required={bool((challenge.get('turnstile') or {}).get('required'))}",
    )
    return token, so_token, "0" + str(challenge.get("token") or "")


def build_sentinel_token(
    session: "Session",
    device_id: str,
    flow: str,
    *,
    user_agent: str = "",
    sec_ch_ua: str = "",
    diagnostic: Callable[[str], None] | None = None,
    sid: str = "",
    device_profile: dict[str, str] | None = None,
) -> tuple[str, str]:
    del sid
    token, _so_token, oai_sc = _build_live_sentinel(
        session,
        device_id,
        flow,
        user_agent=user_agent or DEFAULT_SENTINEL_USER_AGENT,
        sec_ch_ua=sec_ch_ua or DEFAULT_SENTINEL_SEC_CH_UA,
        diagnostic=diagnostic,
        device_profile=device_profile,
    )
    return token, oai_sc


def build_sentinel_with_so_token(
    session: "Session",
    device_id: str,
    flow: str,
    *,
    user_agent: str = "",
    sec_ch_ua: str = "",
    diagnostic: Callable[[str], None] | None = None,
    sid: str = "",
    device_profile: dict[str, str] | None = None,
) -> tuple[str, str, str]:
    del sid
    return _build_live_sentinel(
        session,
        device_id,
        flow,
        user_agent=user_agent or DEFAULT_SENTINEL_USER_AGENT,
        sec_ch_ua=sec_ch_ua or DEFAULT_SENTINEL_SEC_CH_UA,
        diagnostic=diagnostic,
        device_profile=device_profile,
    )

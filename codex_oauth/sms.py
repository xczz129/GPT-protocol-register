"""Pure-HTTP SMS provider clients used by the local import queue.

The provider deliberately lives behind this module so the OAuth worker only
needs three operations: reserve a number, wait for a code, and finish/cancel
the activation. SMSBower provides this small interface without a browser
or provider SDK.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SMSBOWER_API = "https://smsbower.page/stubs/handler_api.php"
SMSBOWER_UA = "codex-oauth-local/1.0"
SMS_PROVIDERS = ("smsbower",)


def parse_phone_attempt_limit(value: Any = None) -> int:
    """Zero means unlimited; retain every explicit nonnegative integer."""
    if value is None or value == "":
        return 0
    if isinstance(value, bool) or isinstance(value, float) and not value.is_integer():
        raise ValueError("max_phone_attempts must be a nonnegative integer")
    try:
        attempts = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("max_phone_attempts must be a nonnegative integer") from error
    if attempts < 0:
        raise ValueError("max_phone_attempts must be a nonnegative integer")
    return attempts

def _mask_secret(value: str) -> str:
    value = str(value or "")
    if not value:
        return ""
    return value[:3] + "…" + value[-4:] if len(value) > 8 else "••••"


class SmsError(RuntimeError):
    """Provider error suitable for a job log."""


@dataclass(frozen=True)
class SmsSettings:
    enabled: bool = False
    api_key: str = ""
    service: str = "dr"
    country_order: tuple[int, ...] = (6, 4, 151)
    poll_interval_ms: int = 5000
    timeout_seconds: int = 180
    max_price: str = ""
    max_phone_attempts: int = 0
    provider: str = "smsbower"

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_phone_attempts", parse_phone_attempt_limit(self.max_phone_attempts))
        if self.provider != "smsbower":
            # Legacy or unknown channels must never route their credentials to
            # SMSBower. Retain the provider name only for an actionable status.
            object.__setattr__(self, "enabled", False)
            object.__setattr__(self, "api_key", "")

    @classmethod
    def from_mapping(cls, value: Any) -> "SmsSettings":
        if not isinstance(value, dict):
            value = {}
        provider = str(value.get("provider") or "smsbower").strip().lower()
        # Generic legacy tokens have ambiguous supplier ownership. Only an
        # explicitly saved SMSBower API key can authorize supplier requests.
        raw_key = value.get("api_key") if provider == "smsbower" else ""
        raw_countries = value.get("country_order", (6, 4, 151))
        if isinstance(raw_countries, str):
            raw_countries = re.split(r"[,，\s]+", raw_countries.strip())
        countries: list[int] = []
        if isinstance(raw_countries, (list, tuple)):
            for item in raw_countries:
                text = re.split(r"[=|:@]", str(item).strip(), 1)[0].strip()
                try:
                    number = int(text)
                except (TypeError, ValueError):
                    continue
                if number >= 0 and number not in countries:
                    countries.append(number)
        try:
            poll = max(1000, min(60000, int(value.get("poll_interval_ms", 5000))))
        except (TypeError, ValueError):
            poll = 5000
        try:
            timeout = max(30, min(1800, int(value.get("timeout_seconds", 180))))
        except (TypeError, ValueError):
            timeout = 180
        raw_enabled = value.get("enabled", False)
        enabled = raw_enabled.strip().lower() in {"1", "true", "yes", "on", "启用"} if isinstance(raw_enabled, str) else bool(raw_enabled)
        return cls(
            enabled=enabled, api_key=str(raw_key or "").strip(),
            service=str(value.get("service") or "dr").strip() or "dr",
            country_order=tuple(countries or [6, 4, 151]),
            poll_interval_ms=poll, timeout_seconds=timeout,
            max_price=str(value.get("max_price") or "").strip(),
            max_phone_attempts=parse_phone_attempt_limit(value.get("max_phone_attempts")),
            provider=provider,
        )

    @property
    def credentials_configured(self) -> bool:
        return self.provider == "smsbower" and bool(self.api_key)

    @property
    def provider_label(self) -> str:
        return "SMSBower" if self.provider == "smsbower" else "已停用的旧接码渠道"

    def as_mapping(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled, "api_key": self.api_key,
            "service": self.service, "country_order": list(self.country_order),
            "poll_interval_ms": self.poll_interval_ms, "timeout_seconds": self.timeout_seconds,
            "max_price": self.max_price, "max_phone_attempts": self.max_phone_attempts,
            "provider": self.provider,
        }

    def public_mapping(self) -> dict[str, Any]:
        configured = self.credentials_configured
        if self.provider != "smsbower":
            state, label = "unsupported", "此接码渠道已移除，请重新配置 SMSBower API Key"
        elif self.enabled and configured:
            state, label = "active", "SMSBower 自动接码已启用"
        elif configured:
            state, label = "disabled", "SMSBower 已配置但未启用"
        else:
            state, label = "unconfigured", "未配置 SMSBower 凭证"
        public = self.as_mapping()
        public.pop("api_key")
        return {**public, "configured": configured, "state": state, "state_label": label,
                "provider_label": self.provider_label, "api_key_masked": _mask_secret(self.api_key),
                "has_api_key": bool(self.api_key)}


@dataclass
class SmsActivation:
    activation_id: str
    phone: str
    country: int | str
    api_key: str
    finished: bool = False
    # 面板日志展示用:国家名称/档位价格(如 "印度尼西亚 #1170 $0.107")。
    label: str = ""
    # Optional telemetry supplied by the panel. Catalogue prices are estimates,
    # never a claim that the provider charged/refunded this amount.
    provider: str = ""
    tier: str = ""
    estimated_price: float | None = None


class SmsBowerClient:
    """SMSBower ``handler_api.php`` protocol wrapper."""

    def __init__(self, settings: SmsSettings, *, timeout: float = 30.0):
        if settings.provider != "smsbower":
            raise SmsError("此接码渠道已移除，仅支持 SMSBower")
        self.settings = settings
        self.timeout = max(3.0, float(timeout))

    def _request(self, action: str, **params: Any) -> str:
        if not self.settings.api_key:
            raise SmsError("未配置 SMSBower API key")
        query: dict[str, str] = {
            "api_key": self.settings.api_key,
            "action": action,
        }
        query.update({key: str(value) for key, value in params.items() if value is not None and str(value) != ""})
        request = Request(
            SMSBOWER_API + "?" + urlencode(query),
            headers={"User-Agent": SMSBOWER_UA, "Accept": "text/plain, */*"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", "replace").strip()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:220]
            raise SmsError(f"SMSBower HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError) as exc:
            raise SmsError(f"SMSBower 连接失败: {getattr(exc, 'reason', exc)}") from exc
        if not body:
            raise SmsError("SMSBower 返回空响应")
        if body.startswith(("BAD_", "NO_", "ERROR", "WRONG_", "EXPIRED")):
            raise SmsError(f"SMSBower {body[:180]}")
        return body

    def balance(self) -> str:
        return self._request("getBalance")

    def acquire(self) -> SmsActivation:
        last_error = "没有可用号码"
        for country in self.settings.country_order:
            params: dict[str, Any] = {
                "service": self.settings.service,
                "country": country,
            }
            if self.settings.max_price:
                params["maxPrice"] = self.settings.max_price
            try:
                body = self._request("getNumber", **params)
            except SmsError as exc:
                last_error = str(exc)
                continue
            if not body.startswith("ACCESS_NUMBER:"):
                last_error = f"SMSBower 未分配号码: {body[:120]}"
                continue
            parts = body.split(":", 2)
            if len(parts) != 3 or not parts[1] or not parts[2]:
                last_error = f"SMSBower 响应格式异常: {body[:120]}"
                continue
            return SmsActivation(parts[1], parts[2], int(country), self.settings.api_key)
        raise SmsError(last_error)

    def is_active(self, activation: SmsActivation) -> bool:
        """Check a retained, unsent activation without reserving another number."""
        if activation.finished:
            return False
        try:
            body = self._request("getStatus", id=activation.activation_id)
        except SmsError as error:
            if any(marker in str(error).upper() for marker in ("NO_ACTIVATION", "EXPIRED")):
                return False
            raise
        if body.startswith(("STATUS_WAIT_CODE", "STATUS_WAIT_RETRY", "STATUS_WAIT_RESEND")):
            return True
        if body.startswith(("STATUS_CANCEL", "STATUS_FINISH", "STATUS_OK:")):
            return False
        raise SmsError("SMSBower 无法确认原号码激活状态")

    def wait_code(self, activation: SmsActivation, *, cancel_event: Any = None) -> str:
        """Wait for a code without closing the activation.

        The caller must invoke :meth:`finish` only after the upstream service
        accepts the code; this leaves a failed OAuth validation eligible for a
        retry and lets timeout/cancellation use :meth:`cancel`.
        """
        deadline = time.monotonic() + self.settings.timeout_seconds
        interval = self.settings.poll_interval_ms / 1000.0
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                try:
                    self.cancel(activation)
                except SmsError as cancel_error:
                    # 取消未被供应商确认:错误带出,上层可重试取消或告警。
                    raise SmsError(f"任务已取消（号码取消失败: {cancel_error}）") from cancel_error
                raise SmsError("任务已取消")
            body = self._request("getStatus", id=activation.activation_id)
            if body.startswith("STATUS_OK:"):
                code = body.split(":", 1)[1].strip()
                if not code:
                    raise SmsError("SMSBower 返回空验证码")
                # Do not finish the activation yet.  The upstream OAuth
                # endpoint still has to accept the code.  Marking it as
                # finished here makes a bad/expired code impossible to retry
                # and can consume a number before it was actually used.
                return code
            if body.startswith("STATUS_CANCEL") or body.startswith("STATUS_FINISH"):
                raise SmsError(f"SMSBower 激活已结束: {body[:120]}")
            # STATUS_WAIT_CODE / STATUS_WAIT_RETRY are normal polling states.
            if cancel_event is not None:
                cancel_event.wait(interval)
            else:
                time.sleep(interval)
        try:
            self.cancel(activation)
        except SmsError as cancel_error:
            raise SmsError(f"等待短信验证码超时（号码取消失败: {cancel_error}）") from cancel_error
        raise SmsError("等待短信验证码超时")

    def finish(self, activation: SmsActivation) -> None:
        if activation.finished:
            return
        try:
            self._request("setStatus", id=activation.activation_id, status="6")
        except SmsError:
            # The code has already been obtained; a provider acknowledgement
            # failure should not make the OAuth result look unsuccessful.
            pass
        finally:
            activation.finished = True

    def cancel(self, activation: SmsActivation) -> None:
        if activation.finished:
            return
        try:
            self._request("setStatus", id=activation.activation_id, status="8")
        except SmsError:
            # 取消未被供应商确认:不要置 finished。置位会让上层
            # release_activation 的"not finished"守卫跳过重试、告警
            # 永远不触发,号码在供应商侧继续占用、短信到达即计费。
            # 保留未完成状态,交由上层重试取消或记录告警。
            raise
        activation.finished = True


def parse_balance_response(provider: str, body: Any) -> dict[str, str]:
    """Normalize SMSBower balance responses without exposing credentials."""
    if provider != "smsbower":
        raise SmsError("此接码渠道已移除，仅支持 SMSBower")
    raw = str(body or "").strip()
    prefix = "ACCESS_BALANCE:"
    if raw.startswith(prefix) and raw[len(prefix):].strip():
        return {"balance": raw[len(prefix):].strip()}
    raise SmsError(f"SMSBower 余额查询失败：{raw[:120] or '响应为空'}")


def check_sms_balance(settings: SmsSettings, *, timeout: float = 30.0) -> dict[str, str]:
    """Check the selected provider using the server-side private settings."""
    if settings.provider != "smsbower":
        raise SmsError("此接码渠道已移除，仅支持 SMSBower")
    if not settings.credentials_configured:
        raise SmsError("未配置 SMSBower 凭证")
    client = SmsBowerClient(settings, timeout=timeout)
    return parse_balance_response(settings.provider, client.balance())


def load_sms_settings(path: str | Any) -> SmsSettings:
    from pathlib import Path

    target = Path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise SmsError(f"读取接码配置失败: {exc}") from exc
    return SmsSettings.from_mapping(data)


def save_sms_settings(path: str | Any, settings: SmsSettings) -> None:
    from pathlib import Path
    import os
    import tempfile

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".smsbower-", suffix=".tmp", dir=str(target.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(settings.as_mapping(), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass

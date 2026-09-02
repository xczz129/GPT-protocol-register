"""Per-task runtime settings for the synchronous protocol registrar."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
import time
from typing import Iterator


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _as_positive(value: object, default: float, *, minimum: float = 0.1) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)


@dataclass(frozen=True)
class ProtocolRuntimeSettings:
    """Immutable settings carried by one protocol registration task."""

    tls_verify: bool | str = True
    request_timeout: float = 30.0
    oauth_timeout: float = 60.0
    sentinel_http_timeout: float = 30.0
    sentinel_worker_timeout: float = 90.0
    sentinel_sdk_cache_ttl: float = 300.0
    sentinel_behavior_duration_ms: int = 4200
    sentinel_worker_persistent: bool = True
    sentinel_worker_pool_size: int = 4
    verify_chatgpt_me: bool = True
    enable_totp_after_register: bool = True
    account_timeout: float = 480.0
    deadline: float | None = None

    @classmethod
    def from_config(cls, registration: dict | None = None) -> "ProtocolRuntimeSettings":
        cfg = registration if isinstance(registration, dict) else {}
        ca_bundle = str(cfg.get("protocol_ca_bundle") or "").strip()
        tls_verify: bool | str = ca_bundle or _as_bool(
            cfg.get("protocol_tls_verify"), True
        )
        return cls(
            tls_verify=tls_verify,
            request_timeout=_as_positive(
                cfg.get("protocol_request_timeout"), 30.0
            ),
            oauth_timeout=_as_positive(cfg.get("protocol_oauth_timeout"), 60.0),
            sentinel_http_timeout=_as_positive(
                cfg.get("protocol_sentinel_timeout"), 30.0
            ),
            sentinel_worker_timeout=_as_positive(
                cfg.get("protocol_sentinel_worker_timeout"), 90.0
            ),
            sentinel_sdk_cache_ttl=max(
                0.0,
                _as_positive(
                    cfg.get("protocol_sentinel_sdk_cache_ttl"),
                    300.0,
                    minimum=0.0,
                ),
            ),
            sentinel_behavior_duration_ms=int(
                _as_positive(
                    cfg.get("protocol_sentinel_behavior_duration_ms"),
                    4200.0,
                    minimum=500.0,
                )
            ),
            sentinel_worker_persistent=_as_bool(
                cfg.get("protocol_sentinel_worker_persistent"), True
            ),
            sentinel_worker_pool_size=int(
                _as_positive(
                    cfg.get("protocol_sentinel_worker_pool_size"),
                    4.0,
                    minimum=1.0,
                )
            ),
            verify_chatgpt_me=_as_bool(cfg.get("protocol_verify_me"), True),
            enable_totp_after_register=_as_bool(
                cfg.get("protocol_enable_totp_after_register"), True
            ),
            account_timeout=_as_positive(
                cfg.get("protocol_account_timeout") or cfg.get("timeout"),
                480.0,
            ),
        )

    def with_deadline(self, deadline: float) -> "ProtocolRuntimeSettings":
        return replace(self, deadline=float(deadline))

    def expired(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline

    def timeout_for(self, requested: float) -> float:
        value = max(0.1, float(requested))
        if self.deadline is None:
            return value
        return max(0.1, min(value, self.deadline - time.monotonic()))


_DEFAULT_RUNTIME = ProtocolRuntimeSettings()
_CURRENT_RUNTIME: ContextVar[ProtocolRuntimeSettings] = ContextVar(
    "protocol_runtime_settings", default=_DEFAULT_RUNTIME
)


def current_runtime_settings() -> ProtocolRuntimeSettings:
    return _CURRENT_RUNTIME.get()


@contextmanager
def activate_runtime(settings: ProtocolRuntimeSettings) -> Iterator[None]:
    token = _CURRENT_RUNTIME.set(settings)
    try:
        yield
    finally:
        _CURRENT_RUNTIME.reset(token)

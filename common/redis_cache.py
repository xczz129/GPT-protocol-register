# -*- coding: utf-8 -*-
"""Small two-level response cache for the authenticated web panel."""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from typing import Any, Callable


class HybridJSONCache:
    """Use process memory as L1 and Redis as L2, with a local failure fallback."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._settings_key: tuple[Any, ...] | None = None
        self._enabled = False
        self._redis_url = ""
        self._prefix = "gpt-register-fp"
        self._socket_timeout = 0.35
        self._local_max_entries = 32
        self._local_max_bytes = 64 * 1024 * 1024
        self._local: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._local_bytes = 0
        self._client: Any = None
        self._redis_retry_at = 0.0
        self._last_error = ""
        self._redis_ready = False
        self._key_locks: dict[str, threading.Lock] = {}
        self._stats = {
            "local_hits": 0,
            "redis_hits": 0,
            "misses": 0,
            "sets": 0,
            "errors": 0,
        }

    def configure(self, settings: dict[str, Any] | None) -> None:
        value = settings if isinstance(settings, dict) else {}
        enabled = bool(value.get("enabled", True))
        redis_url = str(value.get("redis_url") or "redis://127.0.0.1:6379/0").strip()
        prefix = str(value.get("prefix") or "gpt-register-fp").strip() or "gpt-register-fp"
        socket_timeout = max(0.05, min(5.0, float(value.get("socket_timeout") or 0.35)))
        local_max_entries = max(4, min(256, int(value.get("local_max_entries") or 32)))
        local_max_bytes = max(
            4 * 1024 * 1024,
            min(512 * 1024 * 1024, int(value.get("local_max_bytes") or 64 * 1024 * 1024)),
        )
        settings_key = (
            enabled,
            redis_url,
            prefix,
            socket_timeout,
            local_max_entries,
            local_max_bytes,
        )
        with self._lock:
            if self._settings_key == settings_key:
                return
            self._settings_key = settings_key
            self._enabled = enabled
            self._redis_url = redis_url
            self._prefix = prefix
            self._socket_timeout = socket_timeout
            self._local_max_entries = local_max_entries
            self._local_max_bytes = local_max_bytes
            self._client = None
            self._redis_retry_at = 0.0
            self._last_error = ""
            self._redis_ready = False
            self._clear_local_locked()

    def _qualified(self, key: str) -> str:
        return f"{self._prefix}:{str(key).strip()}"

    def _clear_local_locked(self) -> None:
        self._local.clear()
        self._local_bytes = 0

    def clear_local(self) -> None:
        with self._lock:
            self._clear_local_locked()

    def _local_get_locked(self, key: str, now: float) -> bytes | None:
        cached = self._local.get(key)
        if cached is None:
            return None
        expires_at, payload = cached
        if expires_at <= now:
            self._local.pop(key, None)
            self._local_bytes = max(0, self._local_bytes - len(payload))
            return None
        self._local.move_to_end(key)
        return payload

    def _local_set_locked(self, key: str, payload: bytes, ttl: int) -> None:
        previous = self._local.pop(key, None)
        if previous is not None:
            self._local_bytes = max(0, self._local_bytes - len(previous[1]))
        self._local[key] = (time.monotonic() + max(1, ttl), payload)
        self._local_bytes += len(payload)
        while (
            len(self._local) > self._local_max_entries
            or self._local_bytes > self._local_max_bytes
        ):
            _, (_, removed) = self._local.popitem(last=False)
            self._local_bytes = max(0, self._local_bytes - len(removed))

    def _redis_client(self) -> Any:
        if not self._enabled or not self._redis_url:
            return None
        now = time.monotonic()
        with self._lock:
            if self._client is not None:
                return self._client
            if now < self._redis_retry_at:
                return None
        try:
            import redis  # type: ignore

            client = redis.Redis.from_url(
                self._redis_url,
                socket_connect_timeout=self._socket_timeout,
                socket_timeout=self._socket_timeout,
                health_check_interval=30,
                decode_responses=False,
            )
            client.ping()
        except Exception as error:
            with self._lock:
                self._client = None
                self._redis_ready = False
                self._redis_retry_at = time.monotonic() + 10.0
                self._last_error = str(error)[:240]
                self._stats["errors"] += 1
            return None
        with self._lock:
            self._client = client
            self._redis_ready = True
            self._redis_retry_at = 0.0
            self._last_error = ""
        return client

    def get(self, key: str) -> tuple[bytes | None, str]:
        if not self._enabled:
            return None, "disabled"
        qualified = self._qualified(key)
        now = time.monotonic()
        with self._lock:
            local = self._local_get_locked(qualified, now)
            if local is not None:
                self._stats["local_hits"] += 1
                return local, "local"
        client = self._redis_client()
        if client is not None:
            try:
                payload = client.get(qualified)
                if payload is not None:
                    value = bytes(payload)
                    ttl = int(client.ttl(qualified) or 1)
                    with self._lock:
                        self._local_set_locked(qualified, value, max(1, ttl))
                        self._stats["redis_hits"] += 1
                    return value, "redis"
            except Exception as error:
                with self._lock:
                    self._client = None
                    self._redis_ready = False
                    self._redis_retry_at = time.monotonic() + 10.0
                    self._last_error = str(error)[:240]
                    self._stats["errors"] += 1
        with self._lock:
            self._stats["misses"] += 1
        return None, "miss"

    def set(self, key: str, payload: bytes, ttl: int) -> None:
        if not self._enabled:
            return
        value = bytes(payload)
        lifetime = max(1, int(ttl))
        qualified = self._qualified(key)
        with self._lock:
            self._local_set_locked(qualified, value, lifetime)
            self._stats["sets"] += 1
        client = self._redis_client()
        if client is None:
            return
        try:
            client.setex(qualified, lifetime, value)
            with self._lock:
                self._redis_ready = True
                self._last_error = ""
        except Exception as error:
            with self._lock:
                self._client = None
                self._redis_ready = False
                self._redis_retry_at = time.monotonic() + 10.0
                self._last_error = str(error)[:240]
                self._stats["errors"] += 1

    def _key_lock(self, qualified: str) -> threading.Lock:
        with self._lock:
            lock = self._key_locks.get(qualified)
            if lock is None:
                if len(self._key_locks) >= 128:
                    self._key_locks.clear()
                lock = threading.Lock()
                self._key_locks[qualified] = lock
            return lock

    def get_or_set_json(
        self,
        key: str,
        ttl: int,
        builder: Callable[[], Any],
    ) -> tuple[bytes, str]:
        cached, source = self.get(key)
        if cached is not None:
            return cached, source
        qualified = self._qualified(key)
        lock = self._key_lock(qualified)
        with lock:
            cached, source = self.get(key)
            if cached is not None:
                return cached, source
            payload = json.dumps(
                builder(),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self.set(key, payload, ttl)
            return payload, "miss"

    def status(self) -> dict[str, Any]:
        with self._lock:
            if not self._enabled:
                backend = "disabled"
            elif self._redis_ready:
                backend = "redis"
            else:
                backend = "local-fallback"
            return {
                "enabled": self._enabled,
                "backend": backend,
                "redis_ready": self._redis_ready,
                "local_entries": len(self._local),
                "local_bytes": self._local_bytes,
                "last_error": self._last_error,
                **self._stats,
            }


PANEL_RESPONSE_CACHE = HybridJSONCache()

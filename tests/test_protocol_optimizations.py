from __future__ import annotations

import asyncio
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import register.protocol as protocol
import register.protocol_sentinel as protocol_sentinel
from register.protocol_runtime import (
    ProtocolRuntimeSettings,
    activate_runtime,
    current_runtime_settings,
)


class ProtocolRuntimeTests(unittest.TestCase):
    def test_runtime_settings_are_scoped_and_restored(self):
        original = current_runtime_settings()
        scoped = ProtocolRuntimeSettings(
            tls_verify="/tmp/fixture-ca.pem",
            request_timeout=7,
        )
        with activate_runtime(scoped):
            self.assertIs(current_runtime_settings(), scoped)
        self.assertIs(current_runtime_settings(), original)

    def test_request_retry_uses_scoped_timeout(self):
        session = MagicMock()
        session.request.return_value = SimpleNamespace(status_code=200, headers={})
        scoped = ProtocolRuntimeSettings(request_timeout=7)

        with activate_runtime(scoped):
            response, error = protocol.request_with_local_retry(
                session,
                "get",
                "https://auth.openai.com/fixture",
                retry_attempts=1,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(error, "")
        self.assertEqual(session.request.call_args.kwargs["timeout"], 7)

    def test_request_retry_stops_after_task_deadline(self):
        session = MagicMock()
        runtime = ProtocolRuntimeSettings(deadline=0)

        with activate_runtime(runtime):
            response, error = protocol.request_with_local_retry(
                session,
                "get",
                "https://auth.openai.com/fixture",
                retry_attempts=1,
            )

        self.assertIsNone(response)
        self.assertEqual(error, "protocol_account_timeout")
        session.request.assert_not_called()

    def test_protocol_proxy_slot_is_sticky_per_task_and_advances_on_retry(self):
        pool = ["proxy-a", "proxy-b", "proxy-c"]
        first = protocol._protocol_proxy_for_task(
            pool, 2, 0, rotate=True, start_offset=1
        )
        retry = protocol._protocol_proxy_for_task(
            pool, 2, 1, rotate=True, start_offset=1
        )
        self.assertEqual(first, "proxy-c")
        self.assertEqual(retry, "proxy-a")

    def test_protocol_proxy_slot_skips_failed_proxy(self):
        selected = protocol._protocol_proxy_for_task(
            ["proxy-a", "proxy-b", "proxy-c"],
            1,
            0,
            rotate=True,
            excluded={"proxy-a"},
        )
        self.assertEqual(selected, "proxy-b")

    def test_protocol_proxy_retry_stride_avoids_active_task_slots(self):
        pool = [f"proxy-{index}" for index in range(12)]
        first_wave = {
            protocol._protocol_proxy_for_task(
                pool, task, 0, rotate=True, start_offset=0, retry_stride=4
            )
            for task in range(1, 5)
        }
        retry_wave = {
            protocol._protocol_proxy_for_task(
                pool, task, 1, rotate=True, start_offset=0, retry_stride=4
            )
            for task in range(1, 5)
        }

        self.assertEqual(first_wave, {"proxy-0", "proxy-1", "proxy-2", "proxy-3"})
        self.assertEqual(retry_wave, {"proxy-4", "proxy-5", "proxy-6", "proxy-7"})
        self.assertTrue(first_wave.isdisjoint(retry_wave))

    def test_create_session_uses_scoped_tls_setting(self):
        with patch("register.protocol.requests.Session") as session_factory:
            with activate_runtime(
                ProtocolRuntimeSettings(tls_verify="/tmp/fixture-ca.pem")
            ):
                protocol.create_session()

        self.assertEqual(
            session_factory.call_args.kwargs["verify"],
            "/tmp/fixture-ca.pem",
        )


class SentinelSdkCacheTests(unittest.TestCase):
    def test_sdk_source_is_cached_for_the_configured_ttl(self):
        protocol_sentinel.clear_sentinel_sdk_cache()
        session = MagicMock()
        session.get.side_effect = [
            SimpleNamespace(
                status_code=200,
                text="https://sentinel.openai.com/sentinel/fixture/sdk.js",
            ),
            SimpleNamespace(status_code=200, text="fixture sdk source"),
        ]
        runtime = ProtocolRuntimeSettings(sentinel_sdk_cache_ttl=60)

        try:
            with activate_runtime(runtime):
                first = protocol_sentinel._load_live_sdk(
                    session,
                    user_agent="fixture-agent",
                    sec_ch_ua='"Chromium";v="142"',
                    accept_language="en-US,en;q=0.9",
                )
                second = protocol_sentinel._load_live_sdk(
                    session,
                    user_agent="fixture-agent",
                    sec_ch_ua='"Chromium";v="142"',
                    accept_language="en-US,en;q=0.9",
                )
        finally:
            protocol_sentinel.clear_sentinel_sdk_cache()

        self.assertEqual(first, second)
        self.assertEqual(session.get.call_count, 2)

    def test_parallel_cold_load_downloads_sdk_once(self):
        protocol_sentinel.clear_sentinel_sdk_cache()
        session = MagicMock()
        calls: list[str] = []
        call_lock = threading.Lock()

        def get(url, **_kwargs):
            with call_lock:
                calls.append(url)
            if url == protocol_sentinel.SENTINEL_BOOTSTRAP_URL:
                time.sleep(0.05)
                return SimpleNamespace(
                    status_code=200,
                    text="https://sentinel.openai.com/sentinel/fixture/sdk.js",
                )
            return SimpleNamespace(status_code=200, text="fixture sdk source")

        session.get.side_effect = get
        try:
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [
                    executor.submit(
                        protocol_sentinel._load_live_sdk,
                        session,
                        user_agent="fixture-agent",
                        sec_ch_ua='"Chromium";v="142"',
                        accept_language="en-US,en;q=0.9",
                    )
                    for _ in range(4)
                ]
                results = [future.result() for future in futures]
        finally:
            protocol_sentinel.clear_sentinel_sdk_cache()

        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(len(calls), 2)

    def test_behavior_duration_is_configurable(self):
        runtime = ProtocolRuntimeSettings.from_config(
            {"protocol_sentinel_behavior_duration_ms": 1800}
        )

        self.assertEqual(runtime.sentinel_behavior_duration_ms, 1800)


class ProtocolLoginTests(unittest.TestCase):
    def test_login_rotates_after_tls_proxy_error_without_intermediate_failure(self):
        proxies: list[str] = []
        events: list[tuple[str | None, dict]] = []

        def fake_login(index, *, proxy, task_event, error_sink, **_kwargs):
            proxies.append(proxy)
            if len(proxies) == 1:
                detail = "Failed to perform, curl: (35) TLS connect error"
                error_sink(detail)
                task_event(index, "running", step="等待调度器切换代理")
                return None
            task_event(index, "success", email="fixture@example.com")
            return {"email": "fixture@example.com"}

        with (
            patch("mail.release_stale_in_use", return_value=0),
            patch("register.protocol.resolve_proxy_pool", return_value=["proxy-a", "proxy-b"]),
            patch("register.protocol.login_one", side_effect=fake_login),
            patch("register.protocol.asyncio.sleep", return_value=None),
        ):
            result = asyncio.run(
                protocol.run_protocol_login(
                    {
                        "registration": {"protocol_login_retries": 1},
                        "mail": {},
                        "proxy": {"enabled": True, "rotate_per_task": False},
                    },
                    fixed_email="fixture@example.com",
                    task_event=lambda _index, status=None, **fields: events.append(
                        (status, fields)
                    ),
                )
            )

        self.assertEqual(proxies, ["proxy-a", "proxy-b"])
        self.assertEqual(result, [{"email": "fixture@example.com"}])
        self.assertNotIn("failed", [status for status, _fields in events])

    def test_batch_relogin_rotates_after_tls_proxy_error(self):
        proxies: list[str] = []
        account = {
            "email": "fixture@example.com",
            "password": "Password-fixture-1!",
            "totp_secret": "TOTP_FIXTURE",
            "health_status": "invalid",
        }

        def fake_relogin(index, *, proxy, task_event, error_sink, **_kwargs):
            proxies.append(proxy)
            if len(proxies) == 1:
                error_sink("Failed to perform, curl: (35) TLS connect error")
                task_event(index, "running", step="等待调度器切换代理")
                return None
            task_event(index, "success", email=account["email"])
            return {"access_token": "AT_FIXTURE"}

        with (
            patch("common.account_store.load_accounts", return_value=[account]),
            patch("mail.release_stale_in_use", return_value=0),
            patch("register.protocol.resolve_proxy_pool", return_value=["proxy-a", "proxy-b"]),
            patch("register.protocol.login_with_password_totp_one", side_effect=fake_relogin),
            patch("register.protocol.asyncio.sleep", return_value=None),
        ):
            result = asyncio.run(
                protocol.run_protocol_relogin(
                    {
                        "registration": {"protocol_login_retries": 1},
                        "mail": {},
                        "proxy": {"enabled": True, "rotate_per_task": False},
                    },
                    concurrency=1,
                )
            )

        self.assertEqual(proxies, ["proxy-a", "proxy-b"])
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 0)

    def test_validate_otp_attaches_both_sentinel_headers_on_first_request(self):
        session = MagicMock()
        response = SimpleNamespace(status_code=200)
        sentinel_headers = {
            "openai-sentinel-token": "SENTINEL",
            "openai-sentinel-so-token": "SO_TOKEN",
        }
        with (
            patch("register.protocol._build_sentinel_headers", return_value=sentinel_headers) as build,
            patch(
                "register.protocol.request_with_local_retry",
                return_value=(response, ""),
            ) as request,
        ):
            result, error = protocol.validate_otp(
                session,
                "device-fixture",
                "123456",
                fingerprint={"sentinel_sid": "sid-fixture"},
            )

        self.assertIs(result, response)
        self.assertEqual(error, "")
        build.assert_called_once()
        headers = request.call_args.kwargs["headers"]
        self.assertEqual(headers["openai-sentinel-token"], "SENTINEL")
        self.assertEqual(headers["openai-sentinel-so-token"], "SO_TOKEN")
        self.assertIn("x-access-flow-invocation-id", headers)

    def test_local_retry_returns_curl_28_without_reusing_same_proxy(self):
        session = MagicMock()
        session.request.side_effect = RuntimeError(
            "Failed to perform, curl: (28) Connection timed out "
            "after 30000 milliseconds"
        )

        response, error = protocol.request_with_local_retry(
            session,
            "GET",
            "https://auth.openai.com/fixture",
            retry_attempts=3,
        )

        self.assertIsNone(response)
        self.assertIn("curl: (28)", error)
        session.request.assert_called_once()


    def test_runtime_speed_flags_are_loaded_from_config(self):
        runtime = ProtocolRuntimeSettings.from_config(
            {
                "protocol_verify_me": False,
                "protocol_enable_totp_after_register": False,
                "protocol_sentinel_worker_persistent": False,
                "protocol_sentinel_worker_pool_size": 2,
            }
        )

        self.assertFalse(runtime.verify_chatgpt_me)
        self.assertFalse(runtime.enable_totp_after_register)
        self.assertFalse(runtime.sentinel_worker_persistent)
        self.assertEqual(runtime.sentinel_worker_pool_size, 2)

    def test_read_chatgpt_session_can_skip_me_verification(self):
        registrar = protocol.PlatformRegistrar.__new__(protocol.PlatformRegistrar)
        registrar.proxy = ""
        registrar.clearance_user_agent = ""
        registrar.device_id = "device-id"
        registrar.fingerprint = protocol._browser_fingerprint({})
        registrar.session = MagicMock()
        registrar.session.cookies = MagicMock()
        payload = {
            "accessToken": "header.payload.sig",
            "expires": "fixture-expiry",
            "account": {"id": "account-id"},
        }

        with (
            activate_runtime(ProtocolRuntimeSettings(verify_chatgpt_me=False)),
            patch("register.protocol.request_with_local_retry", return_value=(SimpleNamespace(status_code=200), "")) as request,
            patch("register.protocol._response_json", return_value=payload),
            patch("register.protocol._nextauth_session_cookie", return_value="SESSION"),
            patch("register.protocol.build_account_record", return_value={
                "access_token": "AT",
                "account_id": "acc_1234567890",
                "plan_type": "free",
                "expires_at": "",
            }),
            patch("register.protocol._cookie_values", return_value={}),
        ):
            result = registrar._read_chatgpt_session("fixture@example.com", "pw", 1)

        self.assertEqual(result["access_token"], "AT")
        self.assertEqual(request.call_count, 1)
        self.assertIn("/api/auth/session", request.call_args.args[2])

    def test_persistent_sentinel_worker_reuses_one_node_process(self):
        protocol_sentinel.close_persistent_sentinel_workers()
        sdk = """
          globalThis.SentinelSDK = {
            init: async () => { window.document.body.appendChild(window.document.createElement('iframe')); },
            token: async (flow) => JSON.stringify({p:'p', t:'', c:'c', id:'id', flow}),
            sessionObserverToken: async () => ''
          };
        """
        payload = {
            "mode": "proof",
            "sdk": sdk,
            "sdkUrl": "https://sentinel.openai.com/sentinel/x/sdk.js",
            "flow": "fixture",
            "userAgent": "ua",
            "language": "en-US",
            "documentCookie": "",
            "profile": {},
        }
        try:
            with activate_runtime(
                ProtocolRuntimeSettings(
                    sentinel_worker_persistent=True,
                    sentinel_worker_pool_size=1,
                    sentinel_worker_timeout=5,
                )
            ):
                first = protocol_sentinel._run_sentinel_sdk(payload)
                second = protocol_sentinel._run_sentinel_sdk(payload)
                workers = list(protocol_sentinel._PERSISTENT_WORKERS)
                pid = workers[0]._proc.pid if workers and workers[0]._proc else None
        finally:
            protocol_sentinel.close_persistent_sentinel_workers()

        self.assertIn("proof", first)
        self.assertIn("proof", second)
        self.assertIsNotNone(pid)

    def test_login_command_requires_an_explicit_email(self):
        from cli import build_parser

        with self.assertRaises(SystemExit):
            build_parser().parse_args(["login"])


if __name__ == "__main__":
    unittest.main()

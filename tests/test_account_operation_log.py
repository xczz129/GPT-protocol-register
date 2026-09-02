from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


class AccountOperationLogTests(unittest.TestCase):
    @staticmethod
    def _clear_web_account_log_cache():
        import web_app

        with web_app._ACCOUNT_LOG_CACHE_COND:
            web_app._ACCOUNT_LOG_CACHE.clear()
            web_app._ACCOUNT_LOG_CACHE_INFLIGHT.clear()
            web_app._ACCOUNT_LOG_CACHE_COND.notify_all()

    def test_read_boundary_redacts_legacy_camel_case_values(self):
        from common.account_operation_log import append_operation_event, list_operation_events

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.sqlite3"
            append_operation_event(
                "legacy@example.com",
                "maintenance",
                "failed",
                message="initial",
                path=path,
            )
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE operation_events SET message = ?, metadata = ?",
                    (
                        'provider response {"accessToken":"AT-LEAK","newPassword":"PW-LEAK","totpSecret":"TOTP-LEAK","csrfToken":"CSRF-LEAK"}',
                        '{"accessToken":"AT-LEAK","nested":{"newPassword":"PW-LEAK","totpSecret":"TOTP-LEAK","code":"CODE-LEAK"}}',
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            items, total = list_operation_events("legacy@example.com", path=path)
            self.assertEqual(total, 1)
            self.assertEqual(len(items), 1)
            serialized = json.dumps(items, ensure_ascii=False)
            for secret in ("AT-LEAK", "PW-LEAK", "TOTP-LEAK", "CSRF-LEAK", "CODE-LEAK"):
                self.assertNotIn(secret, serialized)
            self.assertEqual(items[0]["metadata"]["accessToken"], "***")
            self.assertEqual(items[0]["metadata"]["nested"]["newPassword"], "***")

    def test_rebind_alias_keeps_history_visible_from_both_emails(self):
        """A successful rebind must not strand events under the old address."""
        from common.account_operation_log import append_operation_event, list_operation_events

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.sqlite3"
            append_operation_event(
                {"email": "old@example.com"},
                "email_rebind",
                "running",
                message="换绑开始",
                path=path,
            )
            append_operation_event(
                {"email": "new@example.com", "account_id": "acct-rebind"},
                "email_rebind",
                "success",
                message="换绑成功",
                aliases=("email:old@example.com",),
                path=path,
            )

            old_rows, old_total = list_operation_events("email:old@example.com", path=path)
            new_rows, new_total = list_operation_events("email:new@example.com", path=path)

            self.assertEqual(old_total, 2)
            self.assertEqual(new_total, 2)
            self.assertEqual(
                {row["id"] for row in old_rows},
                {row["id"] for row in new_rows},
            )
            self.assertEqual(
                {row["email"] for row in new_rows},
                {"old@example.com", "new@example.com"},
            )

    def test_account_log_merge_collapses_trace_log_event_pairs_and_caps_total(self):
        from web_app import _merge_account_log_events

        trace_rows = [
            {
                "id": "trace:1",
                "source": "registration_trace",
                "email": "trace@example.com",
                "operation": "registration",
                "status": "running",
                "step": "任务启动",
                "created_at": "2026-01-01T00:00:00.100Z",
            },
            {
                "id": "trace:2",
                "source": "registration_trace",
                "email": "trace@example.com",
                "operation": "registration",
                "status": "info",
                "message": "[任务1] 任务启动",
                "created_at": "2026-01-01T00:00:00.100Z",
            },
            {
                "id": "trace:3",
                "source": "registration_trace",
                "email": "trace@example.com",
                "operation": "registration",
                "status": "running",
                "step": "任务启动",
                "created_at": "2026-01-01T00:00:00.100Z",
            },
        ]
        rows, total = _merge_account_log_events(
            [
                {
                    "id": "persist:1",
                    "email": "trace@example.com",
                    "operation": "registration",
                    "status": "running",
                    "step": "任务启动",
                    "created_at": "2026-01-01T00:00:00.100Z",
                }
            ],
            trace_rows,
            limit=2,
            newest_first=True,
        )
        self.assertEqual(total, len(rows))
        self.assertEqual(len(rows), 1)

    def test_account_log_api_cache_coalesces_concurrent_reads(self):
        import web_app

        self._clear_web_account_log_cache()
        entered = threading.Event()
        release = threading.Event()
        calls = 0

        def read_events(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            entered.set()
            release.wait(timeout=2)
            return (
                [
                    {
                        "id": 1,
                        "email": "cache@example.com",
                        "operation": "reauth",
                        "status": "success",
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ],
                1,
            )

        async def scenario():
            first = asyncio.create_task(
                web_app.account_operation_logs("email:cache@example.com")
            )
            await asyncio.to_thread(entered.wait, 1)
            second = asyncio.create_task(
                web_app.account_operation_logs("email:cache@example.com")
            )
            await asyncio.sleep(0.05)
            release.set()
            return await asyncio.gather(first, second)

        with (
            patch("web_app.list_operation_events", side_effect=read_events),
            patch("web_app._records_by_keys_with_fallback", return_value={}),
        ):
            first, second = asyncio.run(scenario())

        self.assertEqual(calls, 1)
        self.assertEqual(first, second)
        self.assertEqual(first["total"], 1)
        self._clear_web_account_log_cache()

    def test_account_log_api_cache_invalidates_on_store_revision(self):
        import web_app

        self._clear_web_account_log_cache()
        revision = {"value": 1}
        calls = 0

        def store_stamp():
            return (("events-wal", revision["value"], revision["value"]),)

        def read_events(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return ([], 0)

        async def scenario():
            first = await web_app.account_operation_logs("email:revision@example.com")
            second = await web_app.account_operation_logs("email:revision@example.com")
            revision["value"] = 2
            third = await web_app.account_operation_logs("email:revision@example.com")
            return first, second, third

        with (
            patch("web_app._account_log_store_stamp", side_effect=store_stamp),
            patch("web_app.list_operation_events", side_effect=read_events),
            patch("web_app._records_by_keys_with_fallback", return_value={}),
        ):
            first, second, third = asyncio.run(scenario())

        self.assertEqual(calls, 2)
        self.assertEqual(first, second)
        self.assertEqual(second, third)
        self._clear_web_account_log_cache()

    def test_account_log_api_cache_invalidates_when_trace_changes(self):
        import web_app

        self._clear_web_account_log_cache()
        database_calls = 0
        trace_calls = 0
        with tempfile.TemporaryDirectory() as tmp:
            trace_path = Path(tmp) / "trace.jsonl"
            trace_path.write_text("first\n", encoding="utf-8")

            def read_events(*_args, **_kwargs):
                nonlocal database_calls
                database_calls += 1
                return (
                    [
                        {
                            "id": 1,
                            "email": "trace-cache@example.com",
                            "operation": "registration",
                            "status": "success",
                            "metadata": {"trace_file": str(trace_path)},
                            "created_at": "2026-01-01T00:00:00Z",
                        }
                    ],
                    1,
                )

            def read_trace(*_args, **_kwargs):
                nonlocal trace_calls
                trace_calls += 1
                return ([], 0)

            async def scenario():
                await web_app.account_operation_logs("email:trace-cache@example.com")
                await web_app.account_operation_logs("email:trace-cache@example.com")
                trace_path.write_text("second revision\n", encoding="utf-8")
                await web_app.account_operation_logs("email:trace-cache@example.com")

            with (
                patch("web_app.list_operation_events", side_effect=read_events),
                patch("web_app.list_registration_trace_events", side_effect=read_trace),
                patch("web_app._records_by_keys_with_fallback", return_value={}),
            ):
                asyncio.run(scenario())

        self.assertEqual(database_calls, 2)
        self.assertEqual(trace_calls, 2)
        self._clear_web_account_log_cache()

    def test_protocol_event_forwarder_keeps_account_id_and_redacts_fields(self):
        from register.protocol import _operation_event_forwarder

        task_events = []
        account_events = []
        forward = _operation_event_forwarder(
            lambda index, status=None, **fields: task_events.append(
                (index, status, fields)
            ),
            account_events.append,
            operation="registration",
            index=2,
            email="audit@example.com",
            secrets=("secret-password",),
        )

        forward(
            2,
            "success",
            email="audit@example.com",
            account_id="acct-123",
            step="password=secret-password",
            password="secret-password",
        )

        self.assertEqual(len(task_events), 1)
        self.assertEqual(len(account_events), 1)
        event = account_events[0]
        self.assertEqual(event["account_id"], "acct-123")
        self.assertNotIn("password", event)
        self.assertNotIn("secret-password", json.dumps(event))

    def test_protocol_event_forwarder_can_redact_late_generated_secret(self):
        from register.protocol import _operation_event_forwarder

        account_events = []
        forward = _operation_event_forwarder(
            None,
            account_events.append,
            operation="security_repair",
            index=3,
            email="repair@example.com",
            secrets=("old-password",),
        )
        add_secrets = getattr(forward, "add_secrets", None)
        self.assertTrue(callable(add_secrets))
        add_secrets("generated-password")
        forward(
            3,
            "failed",
            error="provider echoed generated-password and old-password",
        )

        self.assertEqual(len(account_events), 1)
        serialized = json.dumps(account_events, ensure_ascii=False)
        self.assertNotIn("generated-password", serialized)
        self.assertNotIn("old-password", serialized)

    def test_protocol_step_uses_trace_wrapper_once(self):
        from register.protocol import _REGISTRATION_TRACE, _TASK_EVENT, step

        trace = unittest.mock.Mock()
        callback_events = []
        trace_token = _REGISTRATION_TRACE.set(trace)
        event_token = _TASK_EVENT.set(
            lambda index, status=None, **fields: callback_events.append(
                (index, status, fields)
            )
        )
        try:
            with patch("register.protocol.log"):
                step(7, "single trace step")
        finally:
            _TASK_EVENT.reset(event_token)
            _REGISTRATION_TRACE.reset(trace_token)

        trace.event.assert_not_called()
        self.assertEqual(callback_events, [(7, "running", {"step": "single trace step"})])

    def test_protocol_batch_passes_registration_sink_to_one_account_worker(self):
        from register.protocol import run_protocol_register

        seen = []
        account_events = []

        def fake_register(index, **kwargs):
            kwargs["task_event"](
                index,
                "success",
                email=kwargs.get("fixed_email"),
                account_id="acct-123",
                step="注册完成",
            )
            seen.append(kwargs.get("account_log_sink"))
            return {
                "email": kwargs.get("fixed_email"),
                "account_id": "acct-123",
                "access_token": "token",
            }

        async def scenario():
            return await run_protocol_register(
                {"registration": {"max_transient_retries": 0}, "mail": {}, "proxy": {}},
                total=1,
                concurrency=1,
                account_log_sink=account_events.append,
            )

        with (
            patch("mail.release_stale_in_use", return_value=0),
            patch(
                "mail.count_pool_capacity",
                return_value={
                    "available": 1,
                    "total": 1,
                    "emails": ["audit@example.com"],
                },
            ),
            patch("register.protocol.resolve_proxy_pool", return_value=[]),
            patch("register.protocol.register_one", side_effect=fake_register),
        ):
            result = asyncio.run(scenario())

        self.assertEqual(result[0]["account_id"], "acct-123")
        self.assertEqual(len(seen), 1)
        self.assertTrue(callable(seen[0]))

    def test_protocol_batch_holds_attempt_failure_until_retry_terminal(self):
        """A retry must not leave a durable failed row after later success."""
        from register.protocol import run_protocol_register

        events = []
        calls = []

        def fake_register(index, **kwargs):
            calls.append(index)
            sink = kwargs["account_log_sink"]
            task_event = kwargs["task_event"]
            email = kwargs.get("fixed_email") or "retry@example.com"
            sink({"status": "running", "email": email})
            if len(calls) == 1:
                task_event(index, "failed", email=email, error="temporary timeout")
                sink({
                    "status": "failed",
                    "email": email,
                    "error": "temporary timeout",
                })
                return None
            sink({"status": "success", "email": email, "account_id": "acct-retry"})
            task_event(index, "success", email=email, account_id="acct-retry")
            return {
                "email": email,
                "account_id": "acct-retry",
                "access_token": "token-retry",
            }

        async def scenario():
            return await run_protocol_register(
                {
                    "registration": {"max_transient_retries": 1},
                    "mail": {},
                    "proxy": {},
                },
                total=1,
                concurrency=1,
                account_log_sink=events.append,
            )

        with (
            patch("mail.release_stale_in_use", return_value=0),
            patch(
                "mail.count_pool_capacity",
                return_value={
                    "available": 1,
                    "total": 1,
                    "emails": ["retry@example.com"],
                },
            ),
            patch("register.protocol.resolve_proxy_pool", return_value=[]),
            patch("register.protocol.register_one", side_effect=fake_register),
        ):
            result = asyncio.run(scenario())

        self.assertEqual(len(calls), 2)
        self.assertEqual(result[0]["account_id"], "acct-retry")
        self.assertEqual([item["status"] for item in events], ["running", "success"])

    def test_protocol_batch_keeps_immediate_running_marker_and_terminal_replacement(self):
        """The start row is immediate; the terminal row follows the replacement slot."""
        from register.protocol import run_protocol_register

        events = []
        calls = []

        def fake_register(index, **kwargs):
            calls.append(kwargs.get("fixed_email"))
            email = kwargs.get("fixed_email") or ""
            kwargs["account_log_sink"]({"status": "running", "email": email})
            if len(calls) == 1:
                kwargs["task_event"](
                    index,
                    "failed",
                    email=email,
                    error="temporary application failure",
                )
                return None
            kwargs["task_event"](
                index,
                "success",
                email=email,
                account_id="acct-replacement",
            )
            return {"email": email, "account_id": "acct-replacement"}

        async def scenario():
            return await run_protocol_register(
                {
                    "registration": {"max_transient_retries": 1},
                    "mail": {},
                    "proxy": {},
                },
                total=1,
                concurrency=1,
                account_log_sink=events.append,
            )

        with (
            patch("mail.release_stale_in_use", return_value=0),
            patch(
                "mail.count_pool_capacity",
                return_value={
                    "available": 2,
                    "total": 2,
                    "emails": ["first@example.com", "second@example.com"],
                },
            ),
            patch("register.protocol.resolve_proxy_pool", return_value=[]),
            patch("register.protocol.register_one", side_effect=fake_register),
        ):
            result = asyncio.run(scenario())

        self.assertEqual(calls, ["first@example.com", "second@example.com"])
        self.assertEqual(result[0]["email"], "second@example.com")
        self.assertEqual([item["status"] for item in events], ["running", "success"])
        self.assertEqual(events[0]["email"], "first@example.com")
        self.assertEqual(events[1]["email"], "second@example.com")

    def test_registration_default_sink_persists_only_running_and_terminal(self):
        from register.batch import _registration_operation_log_sink_for_batch

        with patch("register.batch._registration_operation_log_sink") as persist:
            sink = _registration_operation_log_sink_for_batch()
            sink({"status": "running", "email": "audit@example.com"})
            sink({"status": "running", "email": "audit@example.com", "step": "step 2"})
            sink({"status": "success", "email": "audit@example.com", "account_id": "acct-1"})
            sink({"status": "success", "email": "audit@example.com", "account_id": "acct-1"})
            sink({"status": "failed", "email": "audit@example.com", "account_id": "acct-1"})

        self.assertEqual(persist.call_count, 2)
        self.assertEqual(
            [call.args[0]["status"] for call in persist.call_args_list],
            ["running", "success"],
        )

    def test_registration_sink_drops_provider_credential_echoes(self):
        from register.batch import _registration_operation_log_sink

        with patch("register.batch.record_operation_event") as persist:
            _registration_operation_log_sink(
                {
                    "status": "failed",
                    "email": "audit@example.com",
                    "error": "provider echoed password=VerySecret! and token=abc",
                    "step": "VerySecret!",
                }
            )

        payload = persist.call_args.kwargs
        self.assertNotIn("VerySecret!", json.dumps(payload, ensure_ascii=False))
        self.assertNotIn("abc", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(payload["error"], "registration_failed")


if __name__ == "__main__":
    unittest.main()

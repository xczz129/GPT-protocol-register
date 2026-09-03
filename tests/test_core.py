# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

from fastapi import HTTPException, Response

import web.routes.mailbox as mailbox_routes

from common.account_store import (
    account_email_set,
    account_public_view,
    assign_account_group,
    load_accounts,
    normalized_health_status,
    upsert_account_record,
    upsert_account_records,
)
from common.redaction import mask_secret, merge_config_preserving_secrets, redact_config_for_api
from common.session_export import build_chatgpt2api_account, parse_jwt_payload
from mail import (
    _extract_code,
    base_mailbox_address,
    normalize_provider_type,
    parse_domain_mailboxes,
    parse_icloud_credentials,
    parse_outlook_credentials,
)
from proxy_util import normalize_proxy_url, proxy_brief


def _jwt(payload: dict) -> str:
    def enc(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{enc({'alg':'none','typ':'JWT'})}.{enc(payload)}.sig"


class MailParsingTests(unittest.TestCase):
    def test_icloud_wait_endpoint_uses_sync_age_and_boundary_parameters(self):
        from mail import ICloudApiProvider

        provider = ICloudApiProvider(
            {
                "type": "icloud_api",
                "api_base": "https://icloud.example",
                "mailboxes": "user@icloud.com----test-key",
            },
            {
                "request_timeout": 15,
                "wait_timeout": 45,
                "wait_interval": 2,
                "wait_chunk": 10,
                "user_agent": "test-agent",
            },
        )
        response = MagicMock(status_code=200, text="")
        response.json.return_value = {"ok": True, "code": "123456"}
        session = MagicMock()
        session.get.return_value = response
        provider.session = session

        boundary = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)
        result = provider._request_code(
            {
                "address": "user@icloud.com",
                "api_key": "test-key",
                "api_base": "https://icloud.example",
                "_code_not_before": boundary,
            },
            timeout=10,
            exclude=["654321"],
        )

        self.assertEqual(result["code"], "123456")
        session.get.assert_called_once()
        self.assertTrue(session.get.call_args.args[0].endswith("/api/v1/code/wait"))
        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["email"], "user@icloud.com")
        self.assertEqual(params["timeout"], 10)
        self.assertEqual(params["interval"], 2)
        self.assertEqual(params["sync"], 1)
        self.assertEqual(params["max_age_seconds"], 600)
        self.assertEqual(params["exclude"], "654321")
        self.assertEqual(params["after"], int(boundary.timestamp()) - 2)

        provider._request_code(
            {
                "address": "user@icloud.com",
                "api_key": "test-key",
                "api_base": "https://icloud.example",
            },
            timeout=60,
            exclude=[],
        )
        self.assertEqual(session.get.call_args.kwargs["params"]["timeout"], 60)

    def test_mailbox_boundary_preserves_seen_codes_unless_explicitly_reset(self):
        from mail import mark_mailbox_code_boundary

        mailbox = {"_seen_otp_codes": ["111222", "333444"]}

        mark_mailbox_code_boundary(mailbox, grace_seconds=0)

        self.assertEqual(mailbox["_seen_otp_codes"], ["111222", "333444"])
        self.assertIn("_code_not_before", mailbox)
        self.assertIn("_received_after", mailbox)

        mark_mailbox_code_boundary(
            mailbox,
            grace_seconds=0,
            reset_seen_codes=True,
        )
        self.assertEqual(mailbox["_seen_otp_codes"], [])

    def test_icloud_wait_endpoint_resyncs_periodically_during_wait_cycle(self):
        from mail import ICloudApiProvider

        provider = ICloudApiProvider(
            {
                "type": "icloud_api",
                "api_base": "https://icloud.example",
                "mailboxes": "user@icloud.com----test-key",
            },
            {
                "request_timeout": 15,
                "wait_timeout": 45,
                "wait_interval": 2,
                "wait_chunk": 10,
                "user_agent": "test-agent",
            },
        )
        response = MagicMock(status_code=200, text="")
        response.json.return_value = {"ok": True}
        session = MagicMock()
        session.get.return_value = response
        provider.session = session
        mailbox = {
            "address": "user@icloud.com",
            "api_key": "test-key",
            "api_base": "https://icloud.example",
        }

        provider._request_code(mailbox, timeout=10, exclude=[])
        provider._request_code(mailbox, timeout=10, exclude=[])
        mailbox["_icloud_wait_last_sync_monotonic"] -= 16
        provider._request_code(mailbox, timeout=10, exclude=[])

        calls = session.get.call_args_list
        self.assertEqual(
            [call.kwargs["params"]["sync"] for call in calls],
            [1, 0, 1],
        )

    def test_no_code_quarantines_mailbox_instead_of_retrying_same_address(self):
        from mail import _is_transient_registration_error

        self.assertFalse(_is_transient_registration_error("no_code"))
        self.assertFalse(_is_transient_registration_error("otp_not_delivered"))
        self.assertTrue(_is_transient_registration_error("stale_signup_otp_session"))
        self.assertTrue(
            _is_transient_registration_error(
                "Failed to perform, curl: (28) Operation timed out after 30000 milliseconds"
            )
        )
        self.assertFalse(_is_transient_registration_error("account_deactivated"))
        self.assertTrue(_is_transient_registration_error("stuck_on_password"))
        self.assertTrue(_is_transient_registration_error("no_email_input"))
        self.assertTrue(_is_transient_registration_error("auth_undefined"))
        self.assertTrue(_is_transient_registration_error("auth_oauthcallback"))
        self.assertTrue(
            _is_transient_registration_error("chatgpt_home_http_403: blocked")
        )

    def test_icloud_gateway_timeout_releases_mailbox_for_retry(self):
        import mail

        mailbox = {
            "provider": "icloud_api",
            "address": "no-fresh-code@icloud.com",
            "_icloud_messages_cache_reads": 3,
        }
        with tempfile.TemporaryDirectory() as td:
            old_state = mail.STATE_FILE
            mail.STATE_FILE = Path(td) / "state.json"
            try:
                mail.mark_mailbox_result(
                    mailbox,
                    success=False,
                    error=(
                        "等待注册验证码超时 "
                        "(provider=icloud_api; last_error=iCloud API HTTP 504)"
                    ),
                )
                state = (
                    json.loads(mail.STATE_FILE.read_text(encoding="utf-8"))
                    if mail.STATE_FILE.exists()
                    else {}
                )
            finally:
                mail.STATE_FILE = old_state

        self.assertNotIn("no-fresh-code@icloud.com", state)

    def test_proxy_transport_error_releases_mailbox_for_retry(self):
        import mail

        mailbox = {
            "provider": "icloud_api",
            "address": "proxy-retry@icloud.com",
        }
        with tempfile.TemporaryDirectory() as td:
            old_state = mail.STATE_FILE
            mail.STATE_FILE = Path(td) / "state.json"
            try:
                mail.STATE_FILE.write_text(
                    json.dumps(
                        {
                            "proxy-retry@icloud.com": {
                                "state": "in_use",
                                "updated_at": "2026-08-11T00:00:00Z",
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                mail.mark_mailbox_result(
                    mailbox,
                    success=False,
                    error="curl: (56) CONNECT tunnel failed, response 403",
                )
                state = json.loads(mail.STATE_FILE.read_text(encoding="utf-8"))
            finally:
                mail.STATE_FILE = old_state

        self.assertNotIn("proxy-retry@icloud.com", state)

    def test_password_add_invalid_state_releases_mailbox_for_fresh_session(self):
        import mail

        mailbox = {
            "provider": "icloud_api",
            "address": "passwordless-retry@icloud.com",
        }
        with tempfile.TemporaryDirectory() as td:
            old_state = mail.STATE_FILE
            mail.STATE_FILE = Path(td) / "state.json"
            try:
                mail.STATE_FILE.write_text(
                    json.dumps(
                        {
                            "passwordless-retry@icloud.com": {
                                "state": "in_use",
                                "updated_at": "2026-08-11T00:00:00Z",
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                mail.mark_mailbox_result(
                    mailbox,
                    success=False,
                    error=(
                        'password_add_http_409, detail={"error": '
                        '{"code": "invalid_state"}}'
                    ),
                )
                state = json.loads(mail.STATE_FILE.read_text(encoding="utf-8"))
            finally:
                mail.STATE_FILE = old_state

        self.assertNotIn("passwordless-retry@icloud.com", state)

    def test_remote_registered_error_retires_mailbox_as_used(self):
        import mail

        cases = (
            (
                "remote-owned@icloud.com",
                'user_register_http_400, detail={"error": {"code": "invalid_auth_step"}}',
            ),
            (
                "taken@icloud.com",
                "create_account_failed: user_already_exists",
            ),
        )
        with tempfile.TemporaryDirectory() as td:
            old_state = mail.STATE_FILE
            mail.STATE_FILE = Path(td) / "state.json"
            try:
                for address, error in cases:
                    mail.mark_mailbox_result(
                        {"provider": "icloud_api", "address": address},
                        success=False,
                        error=error,
                    )
                state = json.loads(mail.STATE_FILE.read_text(encoding="utf-8"))
            finally:
                mail.STATE_FILE = old_state

        self.assertEqual(state["remote-owned@icloud.com"]["state"], "used")
        self.assertIn("invalid_auth_step", state["remote-owned@icloud.com"]["reason"])
        self.assertEqual(state["taken@icloud.com"]["state"], "used")

    def test_icloud_wait_retries_gateway_504_before_returning_code(self):
        from mail import ICloudApiProvider

        provider = object.__new__(ICloudApiProvider)
        provider.conf = {
            "request_timeout": 15,
            "wait_timeout": 10,
            "wait_interval": 1,
            "user_agent": "test-agent",
        }
        provider.api_base = "https://icloud.example"
        first = MagicMock(status_code=504, text="gateway timeout")
        first.json.return_value = {"error": "gateway timeout"}
        second = MagicMock(status_code=200, text="")
        second.json.return_value = {"ok": True, "code": "123456"}
        provider.session = MagicMock()
        provider.session.get.side_effect = [first, second]
        mailbox = {"address": "alias@icloud.com", "api_key": "test-key"}

        with patch("mail.time.sleep") as sleep:
            result = provider._request_code(mailbox, timeout=10, exclude=[])

        self.assertEqual(result["code"], "123456")
        self.assertEqual(provider.session.get.call_count, 2)
        sleep.assert_called_once_with(0.75)

    def test_rate_limited_mailbox_enters_cooldown(self):
        import mail

        config = {
            "providers": [
                {
                    "type": "icloud_api",
                    "mailboxes": "limited@icloud.com----api-key\n",
                }
            ]
        }
        mailbox = {"provider": "icloud_api", "address": "limited@icloud.com"}
        with tempfile.TemporaryDirectory() as td:
            old_state = mail.STATE_FILE
            mail.STATE_FILE = Path(td) / "state.json"
            try:
                mail.mark_mailbox_result(
                    mailbox,
                    success=False,
                    error="auth_rate_limit_exceeded",
                )
                state = json.loads(mail.STATE_FILE.read_text(encoding="utf-8"))
                capacity = mail.count_pool_capacity(config)
            finally:
                mail.STATE_FILE = old_state

        self.assertEqual(state["limited@icloud.com"]["state"], "cooldown")
        self.assertEqual(capacity["available"], 0)
        self.assertEqual(capacity["cooldown"], 1)
        self.assertEqual(capacity["split_available"], 0)
        self.assertEqual(capacity["split_slot_available"], 1)

    def test_capacity_restores_used_state_for_registered_pool_email(self):
        import mail

        config = {
            "providers": [
                {
                    "type": "icloud_api",
                    "mailboxes": (
                        "known@icloud.com----known-key\n"
                        "fresh@icloud.com----fresh-key\n"
                    ),
                }
            ]
        }
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            with (
                patch.object(mail, "STATE_FILE", state_path),
                patch(
                    "common.account_store.account_email_set",
                    return_value={"known@icloud.com"},
                ),
            ):
                capacity = mail.count_pool_capacity(config)
                state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(capacity["used"], 1)
        self.assertEqual(capacity["primary_available"], 1)
        self.assertEqual(capacity["split_available"], 2)
        self.assertEqual(capacity["available"], 3)
        self.assertEqual(
            capacity["emails"],
            [
                "fresh@icloud.com",
                mail.split_mailbox_address("known@icloud.com"),
                mail.split_mailbox_address("fresh@icloud.com"),
            ],
        )
        self.assertEqual(state["known@icloud.com"]["state"], "used")

    def test_capacity_keeps_one_remaining_slot_after_one_historical_alias(self):
        import mail

        config = {
            "providers": [
                {
                    "type": "icloud_api",
                    "mailboxes": "known@icloud.com----known-key\n",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            with (
                patch.object(mail, "STATE_FILE", state_path),
                patch(
                    "common.account_store.account_email_set",
                    return_value={
                        "known@icloud.com",
                        "known+older-split@icloud.com",
                    },
                ),
            ):
                capacity = mail.count_pool_capacity(config)

        self.assertEqual(capacity["total"], 1)
        self.assertEqual(capacity["total_slots"], 2)
        self.assertEqual(capacity["split_total"], 1)
        self.assertEqual(capacity["split_used"], 1)
        self.assertEqual(capacity["split_available"], 0)
        self.assertEqual(capacity["split_slot_available"], 0)
        self.assertEqual(capacity["available"], 0)
        self.assertEqual(len(capacity["emails"]), 0)

    def test_registered_primary_fixed_batch_returns_single_alias_slot(self):
        import mail

        base = "double@icloud.com"
        config = {
            "providers": [
                {
                    "type": "icloud_api",
                    "mailboxes": f"{base}----api-key\n",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as td:
            with (
                patch.object(mail, "STATE_FILE", Path(td) / "state.json"),
                patch(
                    "common.account_store.account_email_set",
                    return_value={base},
                ),
            ):
                capacity = mail.count_pool_capacity(
                    config,
                    fixed_email=base,
                    fixed_alias_count=8,
                )

        self.assertEqual(capacity["fixed_email_mode"], "alias_batch")
        # Hard 1:1: a mailbox yields at most one split slot regardless of the
        # requested alias count.
        self.assertEqual(capacity["available"], 1)
        self.assertEqual(capacity["emails"], [mail.split_mailbox_address(base)])

    def test_custom_alias_batch_never_mints_slots_past_lifetime_cap(self):
        import mail

        base = "repeat@icloud.com"
        previous = set(mail.split_mailbox_addresses(base, 2))
        config = {
            "providers": [
                {
                    "type": "icloud_api",
                    "mailboxes": f"{base}----api-key\n",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as td:
            with (
                patch.object(mail, "STATE_FILE", Path(td) / "state.json"),
                patch(
                    "common.account_store.account_email_set",
                    return_value={base, *previous},
                ),
            ):
                capacity = mail.count_pool_capacity(
                    config,
                    fixed_email=base,
                    fixed_alias_count=10,
                )

        # Hard 1:1: both lifetime slots are spent, so the batch returns
        # nothing instead of minting fresh slots past history.
        self.assertEqual(capacity["available"], 0)
        self.assertEqual(capacity["split_used"], 2)
        self.assertEqual(capacity["emails"], [])

    def test_is_dead_slot_error_classifies_remote_owned_mailboxes(self):
        from register.protocol import is_dead_slot_error

        self.assertTrue(
            is_dead_slot_error(
                'user_register_http_400, detail={"error": {"code": "invalid_auth_step"}}'
            )
        )
        self.assertTrue(
            is_dead_slot_error("create_account_failed: user_already_exists")
        )
        self.assertTrue(is_dead_slot_error("login_http_403 account_deactivated"))
        self.assertFalse(is_dead_slot_error("network timeout"))
        self.assertFalse(is_dead_slot_error(""))
        self.assertFalse(is_dead_slot_error("rate_limit_exceeded"))

    def test_capacity_schedules_single_alias_slots_by_pool_order(self):
        import mail

        first = "single@icloud.com"
        second = "paired@icloud.com"
        historical = "single+historical@icloud.com"
        config = {
            "providers": [
                {
                    "type": "icloud_api",
                    "mailboxes": f"{first}----key-1\n{second}----key-2\n",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as td:
            with (
                patch.object(mail, "STATE_FILE", Path(td) / "state.json"),
                patch(
                    "common.account_store.account_email_set",
                    return_value={first, second, historical},
                ),
            ):
                capacity = mail.count_pool_capacity(config)

        self.assertEqual(
            capacity["emails"],
            [mail.split_mailbox_address(second)],
        )

    def test_registration_wave_planner_isolates_complete_alias_pairs(self):
        from register.scheduling import build_registration_waves

        emails = [
            "primary@icloud.com",
            "first+splitone@icloud.com",
            "first+splittwo@icloud.com",
            "second+splitone@icloud.com",
            "second+splittwo@icloud.com",
            "remainder+splitone@icloud.com",
        ]

        self.assertEqual(
            build_registration_waves(emails, 10),
            [
                ([1], False),
                ([2, 3], True),
                ([4, 5], True),
                ([6], False),
            ],
        )

    def test_icloud_split_slot_uses_base_credential_and_tracks_alias_state(self):
        import mail

        base = "box@icloud.com"
        split = mail.split_mailbox_address(base)
        entry = {
            "type": "icloud_api",
            "mailboxes": f"{base}----api-key\n",
        }
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            state_path.write_text(
                json.dumps({base: {"state": "used"}}), encoding="utf-8"
            )
            with patch.object(mail, "STATE_FILE", state_path):
                provider = mail.ICloudApiProvider(entry, {})
                try:
                    mailbox = provider.create_mailbox(split)
                finally:
                    provider.close()
                state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(mailbox["address"], split)
        self.assertEqual(mailbox["login_address"], base)
        self.assertEqual(mailbox["api_key"], "api-key")
        self.assertEqual(state[base]["state"], "used")
        self.assertEqual(state[split]["state"], "in_use")

    def test_requested_icloud_slot_does_not_fallback_to_outlook_error(self):
        import mail

        base = "target@icloud.com"
        split = mail.split_mailbox_address(base)
        config = {
            "providers": [
                {
                    "type": "outlook_token",
                    "mailboxes": "outlook@example.com----pw----client----refresh\n",
                },
                {
                    "type": "icloud_api",
                    "mailboxes": f"{base}----api-key\n",
                },
            ]
        }
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            state_path.write_text(
                json.dumps({split: {"state": "failed"}}), encoding="utf-8"
            )
            with patch.object(mail, "STATE_FILE", state_path):
                with self.assertRaisesRegex(RuntimeError, "iCloud mailbox slot unavailable"):
                    mail.create_mailbox(config, split)

    def test_capacity_quarantines_trash_and_cdk_pool_addresses(self):
        import mail

        config = {
            "providers": [
                {
                    "type": "icloud_api",
                    "mailboxes": (
                        "trash@icloud.com----trash-key\n"
                        "cdk@icloud.com----cdk-key\n"
                        "fresh@icloud.com----fresh-key\n"
                    ),
                }
            ]
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_path = root / "state.json"
            trash_path = root / "account_trash.json"
            cdk_path = root / "cdks.json"
            trash_path.write_text(
                json.dumps([{"record": {"email": "trash@icloud.com"}}]),
                encoding="utf-8",
            )
            cdk_path.write_text(
                json.dumps([{"email": "cdk@icloud.com"}]),
                encoding="utf-8",
            )
            with (
                patch.object(mail, "STATE_FILE", state_path),
                patch.object(mail, "ACCOUNT_TRASH_FILE", trash_path),
                patch.object(mail, "CDKS_FILE", cdk_path),
                patch("common.account_store.account_email_set", return_value=set()),
            ):
                capacity = mail.count_pool_capacity(config)
                state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(capacity["used"], 2)
        self.assertEqual(capacity["primary_available"], 1)
        self.assertEqual(capacity["split_available"], 3)
        self.assertEqual(capacity["available"], 4)
        self.assertEqual(state["trash@icloud.com"]["reason"], "known_deactivated")
        self.assertEqual(state["cdk@icloud.com"]["reason"], "cdk_bound")

    def test_outlook_parser_dedupes_and_skips_bad_lines(self):
        rows = parse_outlook_credentials(
            """
            # comment
            bad line
            User@outlook.com----pw----cid----rt
            user@outlook.com----pw2----cid2----rt2
            no-token@outlook.com----pw----cid----
            """
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], "User@outlook.com")
        self.assertEqual(rows[0]["refresh_token"], "rt")

    def test_icloud_parser_and_provider_aliases(self):
        rows = parse_icloud_credentials(
            "a@icloud.com----alias_key\n"
            "outlook@x.com----pw----" + "c" * 21 + "----" + "r" * 21 + "\n"
        )
        self.assertEqual(rows, [{"email": "a@icloud.com", "api_key": "alias_key"}])
        self.assertEqual(normalize_provider_type("icloud"), "icloud_api")
        self.assertEqual(normalize_provider_type("outlook_token"), "outlook_token")
        self.assertEqual(base_mailbox_address("abc+tag@icloud.com"), "abc@icloud.com")

    def test_domain_mailbox_parser(self):
        rows = parse_domain_mailboxes(
            "box@xb1219.com----6338\n"
            "box@xb1219.com----dup\n"
            "local-only\n"
            "other@xb1219.com\n"
        )
        self.assertEqual(
            rows,
            [
                {"email": "box@xb1219.com", "id": "6338"},
                {"email": "other@xb1219.com"},
            ],
        )
        self.assertEqual(normalize_provider_type("maddy"), "maddy_webui")

    def test_extract_code_prefers_expected_otp_patterns(self):
        self.assertEqual(_extract_code({"subject": "Verification code: 123456"}), "123456")
        self.assertEqual(_extract_code({"html_content": "<p style='background-color:#F3F3F3'> 654321 </p>"}), "654321")
        self.assertIsNone(_extract_code({"text_content": "ref 177010"}))

    def test_icloud_wait_ignores_year_from_sign_in_notice(self):
        from mail import ICloudApiProvider

        provider = object.__new__(ICloudApiProvider)
        provider.name = "icloud_api"
        provider.conf = {
            "wait_timeout": 1,
            "wait_chunk": 1,
            "wait_interval": 0.2,
        }
        provider._request_code = MagicMock(
            side_effect=[
                {"code": "2026", "mail": {"subject": "New sign-in to your OpenAI account"}},
                {"code": "981015", "mail": {"subject": "Your temporary ChatGPT login code"}},
            ]
        )
        mailbox = {"address": "alias@icloud.com"}

        code = provider.wait_for_code(mailbox)

        self.assertEqual(code, "981015")
        self.assertEqual(mailbox["_seen_otp_codes"], ["2026", "981015"])
        self.assertIn(
            "2026",
            provider._request_code.call_args_list[1].kwargs["exclude"],
        )

    def test_icloud_wait_extracts_code_from_nested_mail_preview(self):
        from mail import ICloudApiProvider

        provider = object.__new__(ICloudApiProvider)
        provider.name = "icloud_api"
        provider.conf = {
            "wait_timeout": 2,
            "wait_chunk": 1,
            "wait_interval": 0.2,
        }
        provider._request_code = MagicMock(
            return_value={
                "ok": True,
                "code": "",
                "mail": {
                    "subject": "Your temporary ChatGPT verification code",
                    "preview": "Your code is 654321",
                    "received_at": "2026-07-31T00:01:00Z",
                },
            }
        )
        mailbox = {"address": "alias@icloud.com"}

        self.assertEqual(provider.wait_for_code(mailbox), "654321")

    def test_icloud_wait_falls_back_to_messages_cache(self):
        from mail import ICloudApiProvider

        provider = object.__new__(ICloudApiProvider)
        provider.name = "icloud_api"
        provider.conf = {
            "wait_timeout": 2,
            "wait_chunk": 1,
            "wait_interval": 0.2,
        }
        provider._request_code = MagicMock(return_value={"ok": True})
        provider._request_messages = MagicMock(
            return_value={
                "ok": True,
                "messages": [
                    {
                        "subject": "Your temporary ChatGPT verification code",
                        "preview": "Your code is 333444",
                        "received_at": "2026-07-31T00:01:00Z",
                    }
                ],
            }
        )
        mailbox = {"address": "alias@icloud.com"}

        self.assertEqual(provider.wait_for_code(mailbox), "333444")
        provider._request_messages.assert_called_once()

    def test_icloud_wait_falls_back_when_wait_repeats_excluded_code(self):
        from mail import ICloudApiProvider

        provider = object.__new__(ICloudApiProvider)
        provider.name = "icloud_api"
        provider.conf = {
            "wait_timeout": 2,
            "wait_chunk": 1,
            "wait_interval": 0.2,
        }
        provider._request_code = MagicMock(
            return_value={
                "ok": True,
                "code": "111222",
                "mail": {"subject": "Your temporary ChatGPT verification code"},
            }
        )
        provider._request_messages = MagicMock(
            return_value={
                "ok": True,
                "messages": [
                    {
                        "code": "333444",
                        "subject": "Your temporary ChatGPT login code",
                        "received_at": "2026-08-04T22:45:35+08:00",
                    }
                ],
            }
        )
        mailbox = {
            "address": "alias@icloud.com",
            "_seen_otp_codes": ["111222"],
            "_received_after": "2026-08-04T22:45:32+08:00",
        }

        self.assertEqual(provider.wait_for_code(mailbox), "333444")
        provider._request_messages.assert_called_once_with(
            mailbox,
            exclude=["111222"],
        )
        self.assertEqual(mailbox["_seen_otp_codes"], ["111222", "333444"])

    def test_icloud_wait_reads_messages_cache_after_wait_timeout(self):
        from mail import ICloudApiError, ICloudApiProvider

        provider = object.__new__(ICloudApiProvider)
        provider.name = "icloud_api"
        provider.conf = {
            "wait_timeout": 2,
            "wait_chunk": 1,
            "wait_interval": 0.2,
        }
        provider._request_code = MagicMock(
            side_effect=ICloudApiError("iCloud wait API request failed: timed out")
        )
        provider._request_messages = MagicMock(
            return_value={
                "ok": True,
                "messages": [
                    {
                        "code": "555666",
                        "subject": "Your temporary ChatGPT login code",
                        "received_at": "2026-08-04T23:21:55+08:00",
                    }
                ],
            }
        )
        mailbox = {
            "address": "alias@icloud.com",
            "_received_after": "2026-08-04T23:21:50+08:00",
        }

        self.assertEqual(provider.wait_for_code(mailbox), "555666")
        provider._request_messages.assert_called_once_with(mailbox, exclude=[])
        self.assertEqual(mailbox["_seen_otp_codes"], ["555666"])

    def test_icloud_wait_applies_received_after_to_nested_mail(self):
        from mail import ICloudApiProvider

        provider = object.__new__(ICloudApiProvider)
        provider.name = "icloud_api"
        provider.conf = {
            "wait_timeout": 3,
            "wait_chunk": 1,
            "wait_interval": 0.2,
        }
        provider._request_code = MagicMock(
            side_effect=[
                {
                    "ok": True,
                    "mail": {
                        "subject": "Your temporary ChatGPT verification code",
                        "preview": "Your code is 111222",
                        "received_at": "2026-07-31T00:00:00Z",
                    },
                },
                {
                    "ok": True,
                    "mail": {
                        "subject": "Your temporary ChatGPT verification code",
                        "preview": "Your code is 222333",
                        "received_at": "2026-07-31T00:01:00Z",
                    },
                },
            ]
        )
        provider._request_messages = MagicMock(return_value={"ok": True, "messages": []})
        mailbox = {
            "address": "alias@icloud.com",
            "_received_after": "2026-07-31T00:00:30Z",
        }

        self.assertEqual(provider.wait_for_code(mailbox), "222333")

    def test_active_mailbox_reservation_does_not_expire_mid_registration(self):
        from mail import _entry_available

        now = datetime.now(timezone.utc)
        active = {
            "state": "in_use",
            "updated_at": (now - timedelta(minutes=8)).isoformat(),
        }
        stale = {
            "state": "in_use",
            "updated_at": (now - timedelta(minutes=16)).isoformat(),
        }
        self.assertFalse(_entry_available(active))
        self.assertTrue(_entry_available(stale))


class ProxyTests(unittest.TestCase):
    def test_batch_proxy_assignment_uses_stride_and_skips_failed_proxy(self):
        from register.batch import _proxy_for_task

        pool = [f"http://proxy-{index}.example:8080" for index in range(6)]
        self.assertEqual(
            _proxy_for_task(
                pool, "", task_index=1, attempt=0, rotate=True,
                start_offset=2, attempt_span=3,
            ),
            pool[2],
        )
        self.assertEqual(
            _proxy_for_task(
                pool, "", task_index=2, attempt=0, rotate=True,
                start_offset=2, attempt_span=3,
            ),
            pool[5],
        )
        self.assertEqual(
            _proxy_for_task(
                pool, "", task_index=2, attempt=0, rotate=True,
                start_offset=2, attempt_span=3, excluded={pool[5]},
            ),
            pool[0],
        )

    def test_authenticated_proxy_bridge_injects_auth_and_relays_connect(self):
        from common.proxy_bridge import AuthenticatedProxyBridge

        async def scenario():
            captured = {"head": ""}

            async def upstream(reader, writer):
                head = await reader.readuntil(b"\r\n\r\n")
                captured["head"] = head.decode("latin-1")
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
                data = await reader.readexactly(4)
                writer.write(data)
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            try:
                server = await asyncio.start_server(upstream, "127.0.0.1", 0)
            except OSError as error:
                raise unittest.SkipTest(
                    f"loopback bind unavailable in test sandbox: {error}"
                ) from error
            upstream_port = server.sockets[0].getsockname()[1]
            bridge = await AuthenticatedProxyBridge.start_for(
                f"http://bridge-user:bridge-pass@127.0.0.1:{upstream_port}"
            )
            self.assertIsNotNone(bridge)
            bridge_port = int(bridge.url.rsplit(":", 1)[1])
            reader, writer = await asyncio.open_connection("127.0.0.1", bridge_port)
            writer.write(
                b"CONNECT example.com:443 HTTP/1.1\r\n"
                b"Host: example.com:443\r\n\r\n"
            )
            await writer.drain()
            response = await reader.readuntil(b"\r\n\r\n")
            self.assertIn(b" 200 ", response.split(b"\r\n", 1)[0])
            writer.write(b"ping")
            await writer.drain()
            self.assertEqual(await reader.readexactly(4), b"ping")
            writer.close()
            await writer.wait_closed()
            await bridge.close()
            server.close()
            await server.wait_closed()
            return captured["head"]

        head = asyncio.run(scenario())
        expected = base64.b64encode(b"bridge-user:bridge-pass").decode("ascii")
        self.assertIn(f"Proxy-Authorization: Basic {expected}", head)

    def test_proxy_mode_selects_single_url_without_reading_pool(self):
        from proxy_util import resolve_proxy_pool

        cfg = {
            "enabled": True,
            "mode": "single_proxy",
            "url": "socks5://127.0.0.1:1080",
            "pool_file": "/does/not/matter.txt",
        }
        self.assertEqual(resolve_proxy_pool(cfg), ["socks5://127.0.0.1:1080"])

    def test_proxy_mode_pool_reads_pool_file(self):
        from proxy_util import resolve_proxy_pool

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "proxies.txt"
            path.write_text("http://pool.example:8080\n", encoding="utf-8")
            cfg = {
                "enabled": True,
                "mode": "pool",
                "url": "socks5://127.0.0.1:1080",
                "pool_file": str(path),
            }
            self.assertEqual(resolve_proxy_pool(cfg), ["http://pool.example:8080"])

    def test_flaresolverr_proxy_splits_auth_from_url(self):
        from common.proxy_clearance import _flaresolverr_proxy

        self.assertEqual(
            _flaresolverr_proxy("http://user%40mail:p%40ss@gate.example:1000"),
            {
                "url": "http://gate.example:1000",
                "username": "user@mail",
                "password": "p@ss",
            },
        )

    def test_flaresolverr_keeps_chromium_supported_socks5_scheme(self):
        from common.proxy_clearance import _flaresolverr_proxy

        self.assertEqual(
            _flaresolverr_proxy("socks5h://warp:1080"),
            {"url": "socks5://warp:1080"},
        )

    def test_flaresolverr_rejects_chromium_proxy_error_page(self):
        from common.proxy_clearance import _solution_error

        error = _solution_error({
            "status": 200,
            "response": "<html>chrome-error://chromewebdata ERR_NO_SUPPORTED_PROXIES</html>",
        })
        self.assertIn("ERR_NO_SUPPORTED_PROXIES", error)

    def test_flaresolverr_requires_real_clearance_cookie(self):
        from common.proxy_clearance import _bundle_from_solution

        self.assertIsNone(_bundle_from_solution(
            {"userAgent": "Chrome/Test", "cookies": []},
            "https://auth.openai.com/api/accounts/authorize",
            3600,
        ))

    def test_clearance_uses_task_proxy_for_external_pool_but_keeps_docker_proxy_for_loopback(self):
        from common.proxy_clearance import _settings

        cfg = {
            "enabled": True,
            "url": "socks5://127.0.0.1:1080",
            "clearance": {
                "enabled": True,
                "mode": "flaresolverr",
                "flaresolverr_url": "http://172.21.0.3:8191",
                "proxy_url": "socks5://warp:1080",
            },
        }
        external = "http://user:pass@gate.example:1000"
        self.assertEqual(_settings(cfg, external)["proxy"], external)
        self.assertEqual(
            _settings(cfg, "socks5://127.0.0.1:1080")["proxy"],
            "socks5://warp:1080",
        )


class CDKStoreTests(unittest.TestCase):
    def test_icloud_mailbox_preview_returns_full_message_list(self):
        import web_app

        sync_response = MagicMock(status_code=200, text="")
        sync_response.json.return_value = {"ok": True, "code": "123456"}
        messages_response = MagicMock(status_code=200, text="")
        messages_response.json.return_value = {
            "ok": True,
            "messages": [
                {"id": 11, "from": "sender@example.com", "to": "box@icloud.com", "subject": "First", "preview": "hello", "received_at": "2026-07-17T00:00:00Z", "code": "123456"},
                {"id": 12, "from": "other@example.com", "to": "box@icloud.com", "subject": "Second", "preview": "world", "received_at": "2026-07-17T00:01:00Z", "code": ""},
            ],
        }
        provider = MagicMock(api_base="https://icloud.example")
        provider.session.get.side_effect = [sync_response, messages_response]
        entry = {"type": "icloud_api", "api_base": "https://icloud.example"}
        credential = {"email": "box@icloud.com", "api_key": "alias_key"}
        with (
            patch.object(web_app, "_find_mailbox_credential", return_value=(entry, credential)),
            patch.object(web_app, "ICloudApiProvider", return_value=provider),
            patch.object(mailbox_routes, "_ICLOUD_INBOX_SYNC_INTERVAL", 0.0),
        ):
            result = web_app._fetch_mailbox_messages_sync(
                {}, web_app.MailMessagesRequest(email="box@icloud.com", provider_type="icloud_api", limit=30)
            )

        self.assertEqual([item["id"] for item in result["items"]], ["11", "12"])
        self.assertTrue(result["items"][0]["has_raw"])
        self.assertEqual(result["items"][0]["code"], "123456")
        self.assertEqual(provider.session.get.call_count, 2)
        sync_call, messages_call = provider.session.get.call_args_list
        self.assertTrue(sync_call.args[0].endswith("/api/v1/code"))
        self.assertTrue(messages_call.args[0].endswith("/api/v1/messages"))
        self.assertEqual(messages_call.kwargs["params"]["email"], "box@icloud.com")

    def test_icloud_mailbox_preview_uses_cached_list_when_sync_fails(self):
        import web_app

        messages_response = MagicMock(status_code=200, text="")
        messages_response.json.return_value = {"ok": True, "messages": []}
        provider = MagicMock(api_base="https://icloud.example")
        provider.session.get.side_effect = [TimeoutError("sync timeout"), messages_response]
        entry = {"type": "icloud_api", "api_base": "https://icloud.example"}
        credential = {"email": "box@icloud.com", "api_key": "alias_key"}
        with (
            patch.object(web_app, "_find_mailbox_credential", return_value=(entry, credential)),
            patch.object(web_app, "ICloudApiProvider", return_value=provider),
            patch.object(mailbox_routes, "_ICLOUD_INBOX_SYNC_INTERVAL", 0.0),
        ):
            result = web_app._fetch_mailbox_messages_sync(
                {}, web_app.MailMessagesRequest(email="box@icloud.com", provider_type="icloud_api")
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["items"], [])
        self.assertEqual(provider.session.get.call_count, 2)

    def test_icloud_raw_message_maps_full_text(self):
        import web_app

        response = MagicMock(status_code=200, text="")
        response.json.return_value = {
            "ok": True,
            "message": {"id": 11, "from": "sender@example.com", "to": "box@icloud.com", "subject": "Full", "body_text": "full body", "body_html": "<p>full body</p>", "received_at": "2026-07-17T00:00:00Z", "code": "123456"},
        }
        provider = MagicMock(api_base="https://icloud.example")
        provider.session.get.return_value = response
        entry = {"type": "icloud_api", "api_base": "https://icloud.example"}
        credential = {"email": "box@icloud.com", "api_key": "alias_key"}
        with (
            patch.object(web_app, "_find_mailbox_credential", return_value=(entry, credential)),
            patch.object(web_app, "ICloudApiProvider", return_value=provider),
        ):
            result = web_app._fetch_icloud_message_sync({}, "box@icloud.com", "icloud_api", "11")

        self.assertEqual(result["item"]["text"], "full body")
        self.assertEqual(result["item"]["code"], "123456")
        self.assertEqual(provider.session.get.call_args.kwargs["params"]["id"], "11")

class ProxyFormatTests(unittest.TestCase):
    def test_proxy_pool_status_keeps_short_preview_and_full_editor_text(self):
        import web_app

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proxy_pool.txt"
            lines = [f"http://proxy-{index}.example:8080" for index in range(25)]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            result = web_app._proxy_pool_preview(
                {
                    "proxy": {
                        "enabled": True,
                        "mode": "pool",
                        "pool_file": str(path),
                    }
                }
            )

        self.assertEqual(result["pool_count"], 25)
        self.assertEqual(result["pool_unique_count"], 25)
        self.assertEqual(len(result["pool_preview"]), 20)
        self.assertEqual(result["pool_text"].splitlines(), lines)

    def test_local_browser_proxy_parser_survives_commercial_provider_removal(self):
        from local_browser import parse_proxy

        self.assertEqual(
            parse_proxy("socks5://user:pass@proxy.example:1080"),
            {
                "type": "socks5", "username": "user", "password": "pass",
                "host": "proxy.example", "port": "1080",
            },
        )

    def test_normalize_proxy_formats(self):
        self.assertEqual(normalize_proxy_url("127.0.0.1:8080"), "http://127.0.0.1:8080")
        self.assertEqual(
            normalize_proxy_url("host:1234:user:pass"),
            "http://user:pass@host:1234",
        )
        self.assertEqual(normalize_proxy_url("socks://host:1080"), "socks5h://host:1080")
        self.assertEqual(proxy_brief("http://verylongusername123456789:pw@host:8080"), "host:8080 user=verylonguserna...456789")

    def test_flaresolverr_solution_filters_foreign_cookies(self):
        from common.proxy_clearance import _bundle_from_solution

        bundle = _bundle_from_solution(
            {
                "userAgent": "Chrome/Test",
                "cookies": [
                    {"name": "cf_clearance", "value": "ok", "domain": ".openai.com"},
                    {"name": "foreign", "value": "no", "domain": ".example.com"},
                ],
            },
            "https://auth.openai.com/api/accounts/authorize",
            3600,
        )
        self.assertIsNotNone(bundle)
        self.assertEqual(bundle.cookies, {"cf_clearance": "ok"})
        self.assertEqual(bundle.user_agent, "Chrome/Test")


class CamoufoxConfigTests(unittest.TestCase):
    def test_japan_country_selection_sets_browser_locale_and_timezone(self):
        from config_loader import apply_fingerprint_env

        cfg = {
            "proxy": {"selected_country": "JP"},
            "fingerprint": {
                "browser": "camoufox",
                "camoufox": {"locale": "auto", "geoip": True},
            },
        }
        with patch.dict(os.environ, {"FINGERPRINT_BROWSER_FORCE": ""}, clear=False):
            apply_fingerprint_env(cfg)
            self.assertEqual(os.environ["REGISTER_PROXY_COUNTRY"], "JP")
            self.assertEqual(os.environ["REGISTER_ACCEPT_LANGUAGE"], "ja-JP,ja;q=0.9,en-US;q=0.7,en;q=0.6")
            self.assertEqual(os.environ["CAMOUFOX_LOCALE"], "ja-JP")
            self.assertEqual(os.environ["LOCAL_LOCALE"], "ja-JP")
            self.assertEqual(os.environ["LOCAL_TZ"], "Asia/Tokyo")

    def test_explicit_camoufox_locale_overrides_country_auto_locale(self):
        from config_loader import apply_fingerprint_env

        cfg = {
            "proxy": {"selected_country": "JP"},
            "fingerprint": {
                "browser": "camoufox",
                "camoufox": {"locale": "en-US", "geoip": True},
            },
        }
        with patch.dict(os.environ, {"FINGERPRINT_BROWSER_FORCE": ""}, clear=False):
            apply_fingerprint_env(cfg)
            self.assertEqual(os.environ["CAMOUFOX_LOCALE"], "en-US")
            self.assertEqual(os.environ["LOCAL_LOCALE"], "en-US")
            self.assertEqual(os.environ["LOCAL_TZ"], "Asia/Tokyo")

    def test_forced_browser_provider_still_refreshes_country_identity(self):
        from config_loader import apply_fingerprint_env

        cfg = {
            "proxy": {"selected_country": "JP"},
            "fingerprint": {
                "browser": "camoufox",
                "camoufox": {"locale": "auto", "geoip": True},
            },
        }
        env = {
            "FINGERPRINT_BROWSER_FORCE": "1",
            "FINGERPRINT_BROWSER": "local",
        }
        with patch.dict(os.environ, env, clear=False):
            apply_fingerprint_env(cfg)
            self.assertEqual(os.environ["FINGERPRINT_BROWSER"], "local")
            self.assertEqual(os.environ["CAMOUFOX_LOCALE"], "ja-JP")
            self.assertEqual(os.environ["LOCAL_TZ"], "Asia/Tokyo")

    def test_availability_check_never_calls_downloading_launch_path(self):
        from local_browser import CamoufoxBrowser

        with (
            patch("camoufox.pkgman.camoufox_path", side_effect=FileNotFoundError("missing")),
            patch("camoufox.pkgman.launch_path") as launch_path,
        ):
            self.assertFalse(CamoufoxBrowser.is_available())
        launch_path.assert_not_called()

    def test_formdata_compatibility_patch_keeps_native_iterators(self):
        from common.browser import CAMOUFOX_FORMDATA_POLYFILL, _env_enabled

        self.assertIn(
            "typeof probe[Symbol.iterator] === 'function') return",
            CAMOUFOX_FORMDATA_POLYFILL,
        )
        with patch.dict(os.environ, {"CAMOUFOX_LEGACY_FORMDATA_PATCH": ""}):
            self.assertFalse(_env_enabled("CAMOUFOX_LEGACY_FORMDATA_PATCH", False))

    def test_exit_ip_lookup_uses_verified_https_and_proxy_credentials(self):
        from common.browser import _secure_public_ip

        response = type("Response", (), {
            "text": "203.0.113.7\n",
            "raise_for_status": lambda self: None,
        })()
        session = type("Session", (), {
            "trust_env": True,
            "proxies": {},
            "get": lambda self, url, **kwargs: response,
            "close": lambda self: None,
        })()
        with patch("common.browser.requests.Session", return_value=session):
            value = _secure_public_ip({
                "server": "http://proxy.example:8080",
                "username": "user@example.com",
                "password": "p@ss",
            })
        self.assertEqual(value, "203.0.113.7")
        self.assertFalse(session.trust_env)
        self.assertEqual(
            session.proxies["https"],
            "http://user%40example.com:p%40ss@proxy.example:8080",
        )
    def test_proxy_geoip_does_not_get_overridden_by_fixed_locale(self):
        from common.browser import _camoufox_launch_config

        env = {
            "CAMOUFOX_HEADLESS": "1",
            "CAMOUFOX_OS": "windows,invalid,macos",
            "CAMOUFOX_LOCALE": "",
            "CAMOUFOX_GEOIP": "1",
            "CAMOUFOX_HUMANIZE": "9",
        }
        with patch.dict(os.environ, env):
            cfg = _camoufox_launch_config("http://user:pass@127.0.0.1:8080")
        self.assertEqual(cfg["os"], ("windows", "macos"))
        self.assertEqual(cfg["humanize"], 1.5)
        self.assertTrue(cfg["geoip"])
        self.assertFalse(cfg["block_webrtc"])
        self.assertNotIn("locale", cfg)

    def test_direct_connection_uses_geoip_for_locale_and_timezone_consistency(self):
        from common.browser import _camoufox_launch_config

        with patch.dict(os.environ, {"CAMOUFOX_LOCALE": "", "CAMOUFOX_GEOIP": "1"}):
            cfg = _camoufox_launch_config(None)
        self.assertTrue(cfg["geoip"])
        self.assertFalse(cfg["block_webrtc"])
        self.assertNotIn("locale", cfg)

    def test_geoip_can_keep_explicit_ui_locale(self):
        from common.browser import _camoufox_launch_config

        with patch.dict(
            os.environ,
            {"CAMOUFOX_LOCALE": "en-US", "CAMOUFOX_GEOIP": "1"},
        ):
            cfg = _camoufox_launch_config("socks5://127.0.0.1:1080")
        self.assertTrue(cfg["geoip"])
        self.assertEqual(cfg["locale"], "en-US")
        self.assertFalse(cfg["block_webrtc"])

    def test_direct_connection_without_geoip_falls_back_to_stable_locale(self):
        from common.browser import _camoufox_launch_config

        with patch.dict(os.environ, {"CAMOUFOX_LOCALE": "", "CAMOUFOX_GEOIP": "0"}):
            cfg = _camoufox_launch_config(None)
        self.assertEqual(cfg["locale"], "en-US")
        self.assertTrue(cfg["block_webrtc"])
        self.assertNotIn("geoip", cfg)

    def test_explicit_locale_is_used_when_proxy_geoip_is_disabled(self):
        from common.browser import _camoufox_launch_config

        env = {"CAMOUFOX_LOCALE": "de-DE", "CAMOUFOX_GEOIP": "0"}
        with patch.dict(os.environ, env):
            cfg = _camoufox_launch_config("http://127.0.0.1:8080")
        self.assertEqual(cfg["locale"], "de-DE")
        self.assertNotIn("geoip", cfg)

    def test_headless_camoufox_defaults_to_zero_humanize(self):
        from common.browser import _camoufox_launch_config

        env = {
            "CAMOUFOX_HEADLESS": "1",
            "CAMOUFOX_HUMANIZE": "",
            "CAMOUFOX_GEOIP": "0",
        }
        with patch.dict(os.environ, env):
            cfg = _camoufox_launch_config(None)
        self.assertTrue(cfg["headless"])
        self.assertNotIn("humanize", cfg)

    def test_browser_diagnostic_redacts_proxy_credentials(self):
        from common import browser as browser_mod

        with tempfile.TemporaryDirectory() as td:
            async def exercise():
                with patch.object(browser_mod, "BROWSER_DIAGNOSTICS_DIR", Path(td)):
                    return await browser_mod._write_browser_diagnostic(
                        "unit",
                        provider="camoufox",
                        profile_id="profile",
                        proxy="http://user:secret@proxy.example:8080",
                        launch_kwargs={
                            "proxy": {
                                "server": "http://user:secret@proxy.example:8080",
                                "username": "user",
                                "password": "secret",
                            }
                        },
                        error="boom",
                    )

            path = asyncio.run(exercise())
            payload = path.read_text(encoding="utf-8")
        self.assertIn("***:***@proxy.example:8080", payload)
        self.assertNotIn("secret", payload)
        self.assertNotIn("user:secret", payload)


class BrowserLifecycleTests(unittest.TestCase):
    def test_open_without_shared_playwright_owns_and_stops_driver(self):
        from common.browser import open_and_connect, teardown
        from local_browser import LocalChrome

        provider = LocalChrome()
        browser = AsyncMock()
        context = AsyncMock()
        page = object()
        playwright = type("Playwright", (), {"stop": AsyncMock()})()
        starter = type("Starter", (), {"start": AsyncMock(return_value=playwright)})()

        async def launch(_playwright, bb, profile_id, proxy=None):
            bb.register_runtime(
                profile_id,
                browser=browser,
                context=context,
                owns_browser=True,
            )
            return browser, context, page

        async def exercise():
            with (
                patch.dict(os.environ, {"FINGERPRINT_BROWSER": "local"}),
                patch("common.browser.async_playwright", return_value=starter),
                patch("common.browser.get_free_browser_provider", return_value=provider),
                patch("common.browser._launch_local_chrome", side_effect=launch),
            ):
                result = await open_and_connect("owned-driver", p=None)
            self.assertIs(result[0], provider)
            self.assertIs(provider._runtime[result[1]]["playwright"], playwright)
            await teardown(provider, result[1], delete=True)

        asyncio.run(exercise())
        starter.start.assert_awaited_once()
        playwright.stop.assert_awaited_once()

    def test_teardown_delete_false_retains_live_runtime(self):
        from common.browser import teardown

        context = AsyncMock()
        browser = AsyncMock()

        class Provider:
            provider_name = "local"

            def __init__(self):
                self._profiles = {"profile": {"name": "debug"}}
                self._runtime = {
                    "profile": {"context": context, "browser": browser}
                }

            def delete_browser(self, profile_id):
                self._profiles.pop(profile_id, None)
                self._runtime.pop(profile_id, None)

        provider = Provider()
        asyncio.run(teardown(provider, "profile", delete=False))

        context.close.assert_not_awaited()
        browser.close.assert_not_awaited()
        self.assertTrue(provider._runtime["profile"]["retained"])

    def test_concurrent_teardown_closes_runtime_once(self):
        from common.browser import teardown

        context = AsyncMock()
        browser = AsyncMock()

        class Provider:
            provider_name = "local"

            def __init__(self):
                self._profiles = {"profile": {"name": "task"}}
                self._runtime = {
                    "profile": {"context": context, "browser": browser}
                }

            def delete_browser(self, profile_id):
                self._profiles.pop(profile_id, None)
                self._runtime.pop(profile_id, None)

        provider = Provider()

        async def close_twice():
            await asyncio.gather(
                teardown(provider, "profile", delete=True),
                teardown(provider, "profile", delete=True),
            )

        asyncio.run(close_twice())
        self.assertEqual(context.close.await_count, 1)
        self.assertEqual(browser.close.await_count, 1)
        self.assertNotIn("profile", provider._runtime)

    def test_teardown_closes_proxy_bridge_before_browser_context(self):
        from common.browser import teardown

        order = []

        class Bridge:
            async def close(self):
                order.append("bridge")

        class Target:
            async def close(self):
                order.append("context")

        class Provider:
            provider_name = "camoufox"

            def __init__(self):
                self._profiles = {"profile": {"name": "task"}}
                self._runtime = {
                    "profile": {
                        "context": Target(),
                        "browser": Target(),
                        "proxy_bridge": Bridge(),
                    }
                }

            def delete_browser(self, profile_id):
                self._profiles.pop(profile_id, None)
                self._runtime.pop(profile_id, None)

        asyncio.run(teardown(Provider(), "profile", delete=True))
        self.assertEqual(order[0], "bridge")
        self.assertIn("context", order)

    def test_teardown_does_not_report_proxy_bridge_timeout(self):
        from common.browser import teardown

        class Bridge:
            async def close(self):
                raise asyncio.TimeoutError()

        class Target:
            async def close(self):
                return None

        class Provider:
            provider_name = "camoufox"

            def __init__(self):
                self._profiles = {"profile": {"name": "task"}}
                self._runtime = {
                    "profile": {
                        "context": Target(),
                        "browser": Target(),
                        "proxy_bridge": Bridge(),
                    }
                }

            def delete_browser(self, profile_id):
                self._profiles.pop(profile_id, None)
                self._runtime.pop(profile_id, None)

        with patch(
            "common.browser._write_browser_diagnostic", new=AsyncMock()
        ) as diagnostic:
            asyncio.run(teardown(Provider(), "profile", delete=True))

        diagnostic.assert_not_awaited()

    def test_verified_chrome_resolution_skips_failed_candidate(self):
        from local_browser import resolve_chrome_executable

        with (
            patch(
                "local_browser.chrome_executable_candidates",
                return_value=["/bad/chrome", "/good/chrome"],
            ),
            patch(
                "local_browser.probe_chrome_executable",
                side_effect=[(False, "crashed"), (True, "ok")],
            ),
        ):
            self.assertEqual(
                resolve_chrome_executable(verify_launch=True), "/good/chrome"
            )


class AccountStoreTests(unittest.TestCase):
    def test_account_email_set_uses_sqlite_index_and_json_fallback(self):
        import common.account_store as store

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "accounts.json"
            path.write_text(
                json.dumps(
                    [
                        {"email": "one@example.com", "password": "secret"},
                        {"email": "two@example.com", "access_token": "token"},
                    ]
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                account_email_set(path), {"one@example.com", "two@example.com"}
            )

            db_path = Path(td) / "accounts.sqlite3"
            old_json, old_db = store.ACCOUNTS_FILE, store.ACCOUNTS_DB_FILE
            try:
                store.ACCOUNTS_FILE = path
                store.ACCOUNTS_DB_FILE = db_path
                self.assertEqual(
                    account_email_set(path), {"one@example.com", "two@example.com"}
                )
            finally:
                store.ACCOUNTS_FILE, store.ACCOUNTS_DB_FILE = old_json, old_db

    def test_public_view_ignores_legacy_manual_plan_override(self):
        view = account_public_view(
            {
                "email": "plan@example.com",
                "plan_type": "free",
                "plan_type_override": "plus",
            }
        )

        self.assertEqual(view["plan_type"], "free")
        self.assertNotIn("plan_type_override", view)

    def test_account_store_drops_legacy_manual_plan_override(self):
        from common.account_store import normalize_accounts

        rows = normalize_accounts(
            [{"email": "plan@example.com", "plan_type": "plus", "plan_type_override": "free"}]
        )

        self.assertEqual(rows[0]["plan_type"], "plus")
        self.assertNotIn("plan_type_override", rows[0])

    def test_public_view_allows_reauth_for_recoverable_states_only(self):
        recoverable = [
            {"health_status": "reauth_required", "health_error": "token expired"},
            {"health_status": "invalid", "health_error": "oauth_refresh_http_401"},
            {"health_status": "error", "health_error": "health_http_403"},
        ]
        for fields in recoverable:
            view = account_public_view({"email": "recover@example.com", **fields})
            self.assertTrue(view["reauth_available"], fields)
            self.assertEqual(view["status"], "异常", fields)

        deactivated = account_public_view(
            {
                "email": "dead@example.com",
                "health_status": "invalid",
                "health_error": "account_deactivated: account has been deleted",
            }
        )
        alive = account_public_view(
            {"email": "alive@example.com", "health_status": "alive"}
        )
        self.assertFalse(deactivated["reauth_available"])
        self.assertTrue(deactivated["is_deactivated"])
        self.assertEqual(deactivated["status"], "已封号")
        self.assertFalse(alive["is_deactivated"])
        self.assertFalse(alive["reauth_available"])

    def test_public_view_marks_unconfirmed_browser_password_for_security_repair(self):
        # Browser registration may persist a candidate password before the
        # server acknowledges it.  The raw value alone must not hide the
        # repair action from the panel.
        pending = account_public_view(
            {
                "email": "pending@example.com",
                "password": "candidate-password",
                "password_set": False,
                "totp_secret": "JBSWY3DPEHPK3PXP",
                "mfa_enabled": True,
                "registration_method": "browser",
            }
        )
        self.assertTrue(pending["security_repair_available"])
        self.assertFalse(pending["password_confirmed"])

        # Missing marker remains compatible with legacy rows that already had
        # both security fields persisted.
        legacy = account_public_view(
            {
                "email": "legacy@example.com",
                "password": "saved-password",
                "totp_secret": "JBSWY3DPEHPK3PXP",
                "mfa_enabled": True,
            }
        )
        self.assertFalse(legacy["security_repair_available"])
        self.assertTrue(legacy["password_confirmed"])

    def test_upsert_dedupe_and_public_view_redacts_password(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "accounts.json"
            upsert_account_record(
                {
                    "email": "a@example.com",
                    "password": "Secret123",
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                    "mfa_enabled": True,
                    "mfa_factor_type": "totp",
                    "mfa_factor_id": "factor-1",
                    "registered_at": "2026-01-01T00:00:00Z",
                },
                path=path,
            )
            upsert_account_record({"email": "a@example.com", "account_id": "acc_1", "access_token": "tok", "registration_method": "protocol", "browser_type": "", "registered_at": "2026-01-02T00:00:00Z"}, path=path)
            rows = load_accounts(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["account_id"], "acc_1")
            self.assertEqual(rows[0]["totp_secret"], "JBSWY3DPEHPK3PXP")
            self.assertTrue(rows[0]["mfa_enabled"])
            public = account_public_view(rows[0])
            self.assertTrue(public["has_password"])
            self.assertNotIn("password", public)
            self.assertNotIn("access_token", public)
            self.assertIn("password_masked", public)
            self.assertTrue(public["has_totp"])
            self.assertTrue(public["mfa_enabled"])
            self.assertEqual(public["totp_secret_masked"], "JB••••XP")
            self.assertNotIn("totp_secret", public)
            self.assertEqual(public["registration_method"], "protocol")
            self.assertEqual(public["browser_type"], "")

    def test_browser_account_record_includes_registration_metadata(self):
        from common.session_export import build_account_record

        record = build_account_record(
            {
                "accessToken": "token",
                "sessionToken": "complete-session-token",
                "user": {"email": "browser@example.com"},
            },
            session_cookie="partial-cookie-chunk",
            browser_type="camoufox",
        )
        self.assertEqual(record["registration_method"], "browser")
        self.assertEqual(record["browser_type"], "camoufox")
        self.assertEqual(record["session_token"], "complete-session-token")

    def test_platform_oauth_callback_is_exchanged_for_refresh_token(self):
        from common.session_export import fetch_platform_refresh_tokens

        handlers = {}
        page = MagicMock()
        page.url = "https://platform.openai.com/auth/callback?code=oauth-code&state=test"
        page.on.side_effect = lambda event, callback: handlers.__setitem__(event, callback)

        async def goto(_url, **_kwargs):
            handlers["request"](MagicMock(url=page.url))

        page.goto = AsyncMock(side_effect=goto)
        page.evaluate = AsyncMock(return_value=False)
        page.close = AsyncMock()
        response = MagicMock(ok=True)
        response.json = AsyncMock(
            return_value={
                "access_token": "renewable-access",
                "refresh_token": "renewable-refresh",
                "id_token": "identity-token",
            }
        )
        request_context = MagicMock()
        request_context.post = AsyncMock(return_value=response)
        context = MagicMock(request=request_context)
        context.new_page = AsyncMock(return_value=page)

        result = asyncio.run(
            fetch_platform_refresh_tokens(
                context,
                email="browser@example.com",
                device_id="device-id",
                timeout=1,
            )
        )

        self.assertEqual(result["refresh_token"], "renewable-refresh")
        request_context.post.assert_awaited_once()
        payload = request_context.post.await_args.kwargs["data"]
        self.assertEqual(payload["code"], "oauth-code")
        self.assertEqual(payload["grant_type"], "authorization_code")
        page.close.assert_awaited_once()

    def test_chunked_nextauth_cookie_is_reconstructed(self):
        import common.cookies as cookie_store

        context = MagicMock()
        context.cookies = AsyncMock(
            return_value=[
                {"name": "__Secure-next-auth.session-token.1", "value": "tail"},
                {"name": "__Secure-next-auth.session-token.0", "value": "head"},
            ]
        )
        with tempfile.TemporaryDirectory() as td, patch.object(
            cookie_store, "COOKIE_OUTPUT_DIR", td
        ):
            value, cookie_file = asyncio.run(
                cookie_store.save_platform_cookies(
                    context,
                    "chatgpt",
                    "profile",
                    email="browser@example.com",
                    key_cookie_names=[
                        "__Secure-next-auth.session-token",
                        "__Secure-next-auth.session-token.0",
                    ],
                )
            )

        self.assertEqual(value, "headtail")
        self.assertTrue(cookie_file.endswith(".json"))

    def test_batch_upsert_merges_all_records_with_one_store_result(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "accounts.json"
            upsert_account_record(
                {"email": "a@example.com", "password": "pw"}, path=path
            )
            rows = upsert_account_records(
                [
                    {"email": "a@example.com", "health_status": "alive"},
                    {"email": "b@example.com", "health_status": "limited"},
                ],
                path=path,
            )

            self.assertEqual(len(rows), 2)
            saved = {item["email"]: item for item in load_accounts(path)}
            self.assertEqual(saved["a@example.com"]["password"], "pw")
            self.assertEqual(saved["a@example.com"]["health_status"], "alive")
            self.assertEqual(saved["b@example.com"]["health_status"], "limited")

    def test_batch_upsert_returns_only_touched_rows(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "accounts.json"
            upsert_account_records(
                [
                    {"email": "a@example.com", "health_status": "alive"},
                    {"email": "b@example.com", "health_status": "alive"},
                    {"email": "c@example.com", "health_status": "alive"},
                ],
                path=path,
            )
            rows = upsert_account_records(
                [{"email": "b@example.com", "health_status": "limited"}],
                path=path,
            )
            saved = load_accounts(path)

        self.assertEqual([item["email"] for item in rows], ["b@example.com"])
        self.assertEqual(
            next(item for item in saved if item["email"] == "b@example.com")["health_status"],
            "limited",
        )

    def test_health_summary_cache_is_revision_scoped_and_returns_copies(self):
        import common.account_store as store

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "accounts.json"
            db_path = root / "accounts.sqlite3"
            old_json, old_db = store.ACCOUNTS_FILE, store.ACCOUNTS_DB_FILE
            try:
                # Route this temporary path through the SQLite backend, while
                # keeping it isolated from the production account database.
                store.ACCOUNTS_FILE = path
                store.ACCOUNTS_DB_FILE = db_path
                store.upsert_account_records(
                    [
                        {"email": "summary-alive@example.com", "health_status": "alive"},
                        {"email": "summary-unknown@example.com"},
                    ],
                    path=path,
                )
                first = store.account_health_summary(path)
                cached = store.account_health_summary(path)
                self.assertEqual(first, cached)

                # The API composes/augments returned dictionaries, so a cache
                # hit must never expose the internal mutable value.
                cached["alive"] = 999
                self.assertEqual(store.account_health_summary(path)["alive"], 1)

                # Every SQLite writer bumps account_meta.revision; the next
                # summary must therefore reflect the changed health state.
                store.upsert_account_record(
                    {
                        "email": "summary-alive@example.com",
                        "health_status": "invalid",
                    },
                    path=path,
                )
                refreshed = store.account_health_summary(path)
                self.assertEqual(refreshed["alive"], 0)
                self.assertEqual(refreshed["invalid"], 1)
            finally:
                store.ACCOUNTS_FILE, store.ACCOUNTS_DB_FILE = old_json, old_db

    def test_stale_false_flags_do_not_overwrite_confirmed_security_state(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "accounts.json"
            upsert_account_record(
                {
                    "email": "secured@example.com",
                    "password": "Aa1!confirmed",
                    "password_set": True,
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                    "mfa_enabled": True,
                },
                path=path,
            )
            upsert_account_record(
                {
                    "email": "secured@example.com",
                    "password_set": False,
                    "mfa_enabled": False,
                    "health_status": "alive",
                },
                path=path,
            )

            saved = load_accounts(path)[0]

        self.assertTrue(saved["password_set"])
        self.assertTrue(saved["mfa_enabled"])
        self.assertEqual(saved["health_status"], "alive")

    def test_public_view_infers_legacy_registration_method(self):
        browser = account_public_view({"email": "old-browser@example.com", "source_type": "fingerprint_browser"})
        protocol = account_public_view({"email": "old-protocol@example.com", "source_type": "web"})
        unknown = account_public_view({"email": "imported@example.com", "source_type": "other"})
        self.assertEqual(browser["registration_method"], "browser")
        self.assertEqual(browser["browser_type"], "")
        self.assertEqual(protocol["registration_method"], "protocol")
        self.assertEqual(unknown["registration_method"], "")

    def test_normalize_bridged_email_and_account_id_groups(self):
        from common.account_store import normalize_accounts

        rows = normalize_accounts([
            {"account_id": "acc_bridge", "access_token": "tok1", "registered_at": "2026-01-01T00:00:00Z"},
            {"email": "bridge@example.com", "password": "pw", "registered_at": "2026-01-02T00:00:00Z"},
            {"email": "bridge@example.com", "account_id": "acc_bridge", "access_token": "tok2", "registered_at": "2026-01-03T00:00:00Z"},
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], "bridge@example.com")
        self.assertEqual(rows[0]["account_id"], "acc_bridge")
        self.assertEqual(rows[0]["access_token"], "tok2")
        self.assertEqual(rows[0]["password"], "pw")

    def test_successful_health_merge_clears_previous_error(self):
        from common.account_store import normalize_accounts

        rows = normalize_accounts(
            [
                {
                    "email": "recovered@example.com",
                    "health_status": "error",
                    "health_error": "Could not resolve host",
                    "last_reauth_error": "invalid_auth_step",
                    "last_reauth_failed_at": "2026-08-09T00:00:00Z",
                },
                {
                    "email": "recovered@example.com",
                    "health_status": "alive",
                    "health_error": "",
                    "last_reauth_error": "",
                    "last_reauth_failed_at": "",
                },
            ]
        )

        self.assertEqual(rows[0]["health_status"], "alive")
        self.assertEqual(rows[0]["health_error"], "")
        self.assertEqual(rows[0]["last_reauth_error"], "")
        self.assertEqual(rows[0]["last_reauth_failed_at"], "")
        self.assertEqual(account_public_view(rows[0])["health_error"], "")


class AccountGroupTests(unittest.TestCase):
    def test_account_snapshot_read_does_not_wait_for_writer_lock(self):
        import common.account_store as account_store

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "accounts.json"
            upsert_account_record({"email": "snapshot@example.com"}, path=path)
            with patch.object(account_store, "_file_lock", side_effect=AssertionError("read attempted to lock")):
                rows = account_store.load_accounts(path)
            self.assertEqual(rows[0]["email"], "snapshot@example.com")

    def test_account_snapshot_cache_reuses_unchanged_file(self):
        import common.account_store as account_store

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "accounts.json"
            upsert_account_record({"email": "cached@example.com"}, path=path)
            with account_store._ACCOUNT_SNAPSHOT_LOCK:
                account_store._ACCOUNT_SNAPSHOT_CACHE.pop(path.absolute(), None)
            with patch.object(account_store, "_read_unlocked", wraps=account_store._read_unlocked) as read:
                first = account_store.load_accounts(path)
                second = account_store.load_accounts(path)
            self.assertEqual(first, second)
            self.assertEqual(read.call_count, 1)

    def test_group_lifecycle_and_last_group_guard(self):
        from common.account_groups import create_group, delete_group, list_groups, rename_group

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "groups.json"
            default = list_groups(path)[0]
            self.assertEqual(default["name"], "默认分组")
            extra = create_group("  新   分组  ", path)
            self.assertEqual(extra["name"], "新 分组")
            with self.assertRaises(ValueError):
                create_group("新 分组", path)
            renamed = rename_group(extra["id"], "归档", path)
            self.assertEqual(renamed["name"], "归档")
            removed, remaining = delete_group(default["id"], path)
            self.assertEqual(removed["id"], default["id"])
            self.assertEqual(len(remaining), 1)
            with self.assertRaises(ValueError):
                delete_group(extra["id"], path)

    def test_assign_group_and_public_view(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "accounts.json"
            upsert_account_record({"email": "one@example.com"}, path=path)
            upsert_account_record({"email": "two@example.com"}, path=path)
            key = account_public_view(load_accounts(path)[0])["delete_key"]
            changed, rows = assign_account_group([key], "grp_target", path=path)
            self.assertEqual(changed, 1)
            assigned = next(row for row in rows if account_public_view(row)["delete_key"] == key)
            self.assertEqual(account_public_view(assigned)["group_id"], "grp_target")

    def test_group_credential_export_is_scoped_and_preserves_formats(self):
        import web_app
        from common.account_groups import create_group, list_groups

        with tempfile.TemporaryDirectory() as td:
            accounts_path = Path(td) / "accounts.json"
            groups_path = Path(td) / "groups.json"
            first = list_groups(groups_path)[0]
            second = create_group("第二组", groups_path)
            upsert_account_record({"email": "outlook@example.com", "group_id": first["id"]}, path=accounts_path)
            upsert_account_record({"email": "alias@icloud.com", "group_id": first["id"]}, path=accounts_path)
            upsert_account_record({"email": "other@icloud.com", "group_id": second["id"]}, path=accounts_path)
            cfg = {"mail": {"providers": [
                {"type": "outlook_token", "mailboxes": "outlook@example.com----pw----client----refresh"},
                {"type": "icloud_api", "mailboxes": "alias@icloud.com----api-key\nother@icloud.com----other-key"},
            ]}}
            with (
                patch.object(web_app, "ACCOUNTS_FILE", accounts_path),
                patch.object(web_app, "GROUPS_FILE", groups_path),
                patch.object(web_app, "_read_config", return_value=cfg),
                patch.object(
                    web_app,
                    "_load_accounts",
                    side_effect=AssertionError("credential export decoded full store"),
                ),
            ):
                lines, missing = web_app._credential_lines_for_group(first["id"])
            self.assertEqual(missing, 0)
            self.assertEqual(lines, [
                "alias@icloud.com----api-key",
                "outlook@example.com----pw----client----refresh",
            ])
            self.assertFalse(any("other-key" in line for line in lines))


class SessionExportTests(unittest.TestCase):
    def test_chatgpt_session_fetch_bypasses_transient_cache(self):
        from common.session_export import fetch_chatgpt_session

        page = MagicMock()
        page.context = None
        page.evaluate = AsyncMock(
            return_value={"accessToken": "access-token", "sessionToken": "session"}
        )

        result = asyncio.run(fetch_chatgpt_session(page))

        self.assertEqual(result["accessToken"], "access-token")
        script = page.evaluate.await_args.args[0]
        self.assertIn("cache: 'no-store'", script)
        self.assertIn("Cache-Control", script)
        self.assertIsInstance(page.evaluate.await_args.args[1], int)

    def test_chatgpt_session_without_access_token_is_not_accepted(self):
        from common.session_export import fetch_chatgpt_session

        page = MagicMock()
        page.context = None
        page.evaluate = AsyncMock(return_value={"sessionToken": "session-only"})

        self.assertIsNone(asyncio.run(fetch_chatgpt_session(page)))

    def test_chatgpt_session_context_request_can_win_race(self):
        from common.session_export import fetch_chatgpt_session

        response = MagicMock(ok=True)
        response.json = AsyncMock(
            return_value={"accessToken": "context-token", "sessionToken": "session"}
        )
        request_context = MagicMock()
        request_context.get = AsyncMock(return_value=response)
        context = MagicMock()
        context.request = request_context
        page = MagicMock()
        page.context = context

        async def slow_page(*_args):
            await asyncio.sleep(30)

        page.evaluate = AsyncMock(side_effect=slow_page)

        result = asyncio.run(fetch_chatgpt_session(page, timeout=1.0))

        self.assertEqual(result["accessToken"], "context-token")
        request_context.get.assert_awaited_once()

    def test_jwt_parse_and_c2a_payload(self):
        token = _jwt({
            "email": "jwt@example.com",
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acc_jwt",
                "chatgpt_plan_type": "free",
            },
        })
        self.assertEqual(parse_jwt_payload(token)["email"], "jwt@example.com")
        account = build_chatgpt2api_account({"accessToken": token}, email="fallback@example.com")
        self.assertEqual(account["email"], "fallback@example.com")
        self.assertEqual(account["account_id"], "acc_jwt")
        self.assertEqual(account["type"], "free")
        self.assertEqual(account["source_type"], "web")


class ChatGPT2APIGroupTests(unittest.TestCase):
    def test_collect_local_accounts_filters_group_and_invalid_records(self):
        from common.chatgpt2api import collect_local_accounts

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "accounts.json"
            upsert_account_record(
                {"email": "one@example.com", "access_token": "tok-one", "group_id": "grp_one", "health_status": "alive"},
                path=path,
            )
            upsert_account_record(
                {"email": "invalid@example.com", "access_token": "tok-invalid", "group_id": "grp_one", "health_status": "invalid"},
                path=path,
            )
            upsert_account_record(
                {
                    "email": "refresh-401@example.com",
                    "access_token": "tok-refresh-401",
                    "group_id": "grp_one",
                    "health_status": "error",
                    "health_error": "oauth_refresh_http_401: Your session has expired",
                },
                path=path,
            )
            upsert_account_record(
                {"email": "two@example.com", "access_token": "tok-two", "group_id": "grp_two"},
                path=path,
            )
            upsert_account_record(
                {"email": "legacy@example.com", "access_token": "tok-legacy"},
                path=path,
            )

            first = collect_local_accounts(
                group_id="grp_one", default_group_id="grp_default", accounts_path=path
            )
            default = collect_local_accounts(
                group_id="grp_default", default_group_id="grp_default", accounts_path=path
            )
            all_valid = collect_local_accounts(accounts_path=path)

        self.assertEqual([item["email"] for item in first], ["one@example.com"])
        self.assertEqual([item["email"] for item in default], ["legacy@example.com"])
        self.assertEqual(
            {item["email"] for item in all_valid},
            {"one@example.com", "two@example.com", "legacy@example.com"},
        )

    def test_upload_endpoint_passes_selected_group(self):
        import web_app

        body = web_app.ChatGPT2APIUploadRequest(
            url="https://c2a.example",
            key="admin-key",
            group_id="grp_one",
        )
        groups = [
            {"id": "grp_default", "name": "默认分组", "account_count": 1},
            {"id": "grp_one", "name": "第一组", "account_count": 1},
        ]
        account = {"email": "one@example.com", "access_token": "token"}

        async def inline_thread(function, *args, **kwargs):
            return function(*args, **kwargs)

        with (
            patch.object(web_app, "_read_config", return_value={}),
            patch.object(web_app, "_groups_with_counts", return_value=groups),
            patch.object(web_app.asyncio, "to_thread", side_effect=inline_thread),
            patch("common.chatgpt2api.collect_local_accounts", return_value=[account]) as collect,
            patch("common.chatgpt2api.import_accounts", return_value=(True, "added=1", {"added": 1})),
        ):
            result = asyncio.run(web_app.upload_all_chatgpt2api(body))

        self.assertTrue(result["ok"])
        self.assertEqual(result["group_id"], "grp_one")
        self.assertEqual(result["group_name"], "第一组")
        self.assertEqual(collect.call_args.kwargs["group_id"], "grp_one")

    def test_upload_endpoint_counts_all_accounts_without_decoding_payloads(self):
        import web_app

        body = web_app.ChatGPT2APIUploadRequest(
            url="https://c2a.example",
            key="admin-key",
        )
        groups = [{"id": "grp_default", "name": "默认分组", "account_count": 3}]

        async def inline_thread(function, *args, **kwargs):
            return function(*args, **kwargs)

        with (
            patch.object(web_app, "_read_config", return_value={}),
            patch.object(web_app, "_groups_with_counts", return_value=groups),
            patch.object(web_app, "count_accounts", return_value=123) as count,
            patch.object(
                web_app,
                "_load_accounts",
                side_effect=AssertionError("upload-all decoded full store for count"),
            ),
            patch.object(web_app.asyncio, "to_thread", side_effect=inline_thread),
            patch(
                "common.chatgpt2api.collect_local_accounts",
                return_value=[],
            ),
        ):
            result = asyncio.run(web_app.upload_all_chatgpt2api(body))

        self.assertFalse(result["ok"])
        count.assert_called_once_with(web_app.ACCOUNTS_FILE)


class RedactionTests(unittest.TestCase):
    def test_cache_redaction_masks_redis_password_and_preserves_it(self):
        cfg = {"cache": {"redis_url": "redis://panel:redis-secret@127.0.0.1:6379/0"}}

        redacted = redact_config_for_api(cfg)

        self.assertEqual(redacted["cache"]["redis_url"], "")
        self.assertTrue(redacted["cache"]["has_redis_password"])
        self.assertNotIn("redis-secret", json.dumps(redacted))
        merge_config_preserving_secrets(cfg, redacted)
        self.assertEqual(
            cfg["cache"]["redis_url"],
            "redis://panel:redis-secret@127.0.0.1:6379/0",
        )

    def test_web_config_reads_are_cached_and_return_independent_values(self):
        import web_app

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.yaml"
            path.write_text("web: {}\n", encoding="utf-8")
            old_file = web_app.CONFIG_FILE
            old_key = web_app._CONFIG_CACHE_KEY
            old_value = web_app._CONFIG_CACHE_VALUE
            try:
                web_app.CONFIG_FILE = path
                web_app._CONFIG_CACHE_KEY = None
                web_app._CONFIG_CACHE_VALUE = None
                with patch.object(
                    web_app,
                    "load_config",
                    return_value={"nested": {"value": 1}},
                ) as loader:
                    first = web_app._read_config()
                    first["nested"]["value"] = 9
                    second = web_app._read_config()

                self.assertEqual(second["nested"]["value"], 1)
                loader.assert_called_once_with(path)
            finally:
                web_app.CONFIG_FILE = old_file
                web_app._CONFIG_CACHE_KEY = old_key
                web_app._CONFIG_CACHE_VALUE = old_value

    def test_web_config_cold_reads_singleflight_same_file_stamp(self):
        import web_app

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.yaml"
            path.write_text("web: {}\n", encoding="utf-8")
            old_file = web_app.CONFIG_FILE
            old_key = web_app._CONFIG_CACHE_KEY
            old_value = web_app._CONFIG_CACHE_VALUE
            old_inflight = set(web_app._CONFIG_LOAD_INFLIGHT)
            try:
                web_app.CONFIG_FILE = path
                web_app._CONFIG_CACHE_KEY = None
                web_app._CONFIG_CACHE_VALUE = None
                web_app._CONFIG_LOAD_INFLIGHT.clear()
                started = threading.Event()
                release = threading.Event()

                def slow_load(config_path):
                    started.set()
                    self.assertTrue(release.wait(timeout=2))
                    return {"nested": {"value": 1}}

                with patch.object(web_app, "load_config", side_effect=slow_load) as loader:
                    with ThreadPoolExecutor(max_workers=6) as pool:
                        futures = [pool.submit(web_app._read_config) for _ in range(6)]
                        self.assertTrue(started.wait(timeout=2))
                        release.set()
                        values = [future.result(timeout=2) for future in futures]

                self.assertEqual(loader.call_count, 1)
                self.assertEqual(values, [{"nested": {"value": 1}}] * 6)
                values[0]["nested"]["value"] = 9
                self.assertEqual(web_app._read_config()["nested"]["value"], 1)
            finally:
                web_app.CONFIG_FILE = old_file
                web_app._CONFIG_CACHE_KEY = old_key
                web_app._CONFIG_CACHE_VALUE = old_value
                web_app._CONFIG_LOAD_INFLIGHT.clear()
                web_app._CONFIG_LOAD_INFLIGHT.update(old_inflight)

    def test_config_redaction_and_secret_preserving_merge(self):
        cfg = {
            "chatgpt2api": {"key": "c2a-secret-value"},
            "web": {"username": "admin", "password": "panel-pass", "token": "session-secret"},
        }
        red = redact_config_for_api(cfg)
        self.assertEqual(red["chatgpt2api"]["key"], "")
        self.assertEqual(red["web"]["password"], "")
        self.assertEqual(mask_secret("abcdefghijk"), "ab…")

        merge_config_preserving_secrets(cfg, {"chatgpt2api": {"key": ""}, "web": {"password": "••••••••"}})
        self.assertEqual(cfg["chatgpt2api"]["key"], "c2a-secret-value")
        self.assertEqual(cfg["web"]["password"], "panel-pass")

    def test_config_redaction_hides_mailbox_credentials_and_reports_count(self):
        cfg = {
            "mail": {
                "providers": [
                    {
                        "type": "outlook_token",
                        "mailboxes": "one@example.com----password----client----refresh-token\n",
                    },
                    {
                        "type": "icloud_api",
                        "mailboxes": "alias@icloud.com----private-api-key\n",
                    },
                ]
            }
        }

        red = redact_config_for_api(cfg)

        providers = red["mail"]["providers"]
        self.assertEqual([item["mailbox_count"] for item in providers], [1, 1])
        self.assertTrue(all(item["mailboxes"] == "" for item in providers))
        payload = json.dumps(red)
        self.assertNotIn("password", payload)
        self.assertNotIn("refresh-token", payload)
        self.assertNotIn("private-api-key", payload)

    def test_config_redaction_hides_maintenance_webhook(self):
        cfg = {"maintenance": {"webhook_url": "https://hooks.example/secret-token"}}

        redacted = redact_config_for_api(cfg)

        self.assertEqual(redacted["maintenance"]["webhook_url"], "")
        self.assertTrue(redacted["maintenance"]["has_webhook"])
        self.assertNotIn("secret-token", json.dumps(redacted))


class ResponseCacheTests(unittest.TestCase):
    def test_local_hit_expiry_and_singleflight(self):
        from common.redis_cache import HybridJSONCache

        cache = HybridJSONCache()
        cache.configure(
            {
                "enabled": True,
                "redis_url": "redis://127.0.0.1:1/0",
                "local_max_entries": 8,
                "local_max_bytes": 4 * 1024 * 1024,
            }
        )
        calls = 0
        call_lock = threading.Lock()

        def builder():
            nonlocal calls
            with call_lock:
                calls += 1
            time.sleep(0.03)
            return {"value": calls}

        with patch.object(cache, "_redis_client", return_value=None):
            with ThreadPoolExecutor(max_workers=6) as pool:
                payloads = list(
                    pool.map(lambda _: cache.get_or_set_json("same", 1, builder), range(6))
                )
            self.assertEqual(calls, 1)
            self.assertTrue(all(item[0] == payloads[0][0] for item in payloads))
            self.assertEqual(cache.get("same")[1], "local")

            time.sleep(1.05)
            cache.get_or_set_json("same", 1, builder)
            self.assertEqual(calls, 2)


class MailboxImportLogicTests(unittest.TestCase):
    def test_outlook_import_appends_without_overwriting_existing_pool(self):
        import web_app

        existing = "old@example.com----pw1----client1----refresh1\n"
        incoming = "new@example.com----pw2----client2----refresh2\n"

        merged, stats = web_app._merge_mailbox_credentials(
            existing, incoming, "outlook_token"
        )

        rows = parse_outlook_credentials(merged)
        self.assertEqual([item["email"] for item in rows], ["old@example.com", "new@example.com"])
        self.assertEqual(stats, {"imported": 1, "added": 1, "updated": 0, "unchanged": 0, "total": 2})

    def test_icloud_import_updates_same_email_and_keeps_other_entries(self):
        import web_app

        existing = "same@icloud.com----old-key\nkeep@icloud.com----keep-key\n"
        incoming = "same@icloud.com----new-key\nadded@icloud.com----added-key\n"

        merged, stats = web_app._merge_mailbox_credentials(
            existing, incoming, "icloud_api"
        )

        rows = {item["email"]: item["api_key"] for item in parse_icloud_credentials(merged)}
        self.assertEqual(rows, {
            "same@icloud.com": "new-key",
            "keep@icloud.com": "keep-key",
            "added@icloud.com": "added-key",
        })
        self.assertEqual(stats, {"imported": 2, "added": 1, "updated": 1, "unchanged": 0, "total": 3})

    def test_empty_import_never_clears_existing_credentials(self):
        import web_app

        existing = "keep@icloud.com----keep-key\n"
        merged, stats = web_app._merge_mailbox_credentials(
            existing, "   \n", "icloud_api", append=False
        )

        self.assertEqual(merged, existing)
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["imported"], 0)

    def test_mailbox_api_returns_import_counts_and_persists_merged_pool(self):
        import web_app

        cfg = {"mail": {"providers": [{
            "type": "icloud_api",
            "mailboxes": "old@icloud.com----old-key\n",
        }]}}
        body = web_app.MailboxesUpdate(
            provider_type="icloud_api",
            mailboxes="new@icloud.com----new-key\n",
            append=True,
        )
        with web_app.JOB.lock:
            old_running = web_app.JOB.running
            web_app.JOB.running = True
        try:
            with (
                patch.object(web_app, "_read_config", return_value=cfg),
                patch.object(web_app, "_write_config") as write_config,
            ):
                result = asyncio.run(web_app.put_mailboxes(body))
            self.assertEqual(result["added"], 1)
            self.assertEqual(result["updated"], 0)
            self.assertEqual(result["total"], 2)
            saved = cfg["mail"]["providers"][0]["mailboxes"]
            self.assertIn("old@icloud.com----old-key", saved)
            self.assertIn("new@icloud.com----new-key", saved)
            write_config.assert_called_once_with(cfg)
        finally:
            with web_app.JOB.lock:
                web_app.JOB.running = old_running


class WebPanelLogicTests(unittest.TestCase):
    def test_delete_invalid_accounts_preserves_other_health_states(self):
        import web_app

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "accounts.json"
            for email, health in (
                ("alive@example.com", "alive"),
                ("invalid@example.com", "invalid"),
                ("error@example.com", "error"),
            ):
                upsert_account_record(
                    {"email": email, "access_token": f"token-{health}", "health_status": health},
                    path=path,
                )
            upsert_account_record(
                {
                    "email": "refresh-401@example.com",
                    "access_token": "token-refresh-401",
                    "health_status": "error",
                    "health_error": "oauth_refresh_http_401: Your session is invalid",
                },
                path=path,
            )
            async def inline_thread(function, *args, **kwargs):
                return function(*args, **kwargs)

            with (
                patch.object(web_app, "ACCOUNTS_FILE", path),
                patch.object(web_app.asyncio, "to_thread", side_effect=inline_thread),
                patch.object(web_app, "archive_accounts", return_value=2),
            ):
                invalid_keys = [
                    account_public_view(item)["delete_key"]
                    for item in load_accounts(path)
                    if account_public_view(item)["health_status"] == "invalid"
                ]
                result = asyncio.run(
                    web_app.accounts_delete_invalid(
                        web_app.BulkDeleteRequest(keys=invalid_keys)
                    )
                )
            remaining = load_accounts(path)

        self.assertEqual(result["removed"], 2)
        self.assertEqual(
            {item["email"] for item in remaining},
            {"alive@example.com", "error@example.com"},
        )

    def test_legacy_refresh_401_is_publicly_invalid(self):
        record = {
            "email": "refresh-401@example.com",
            "health_status": "error",
            "health_error": "oauth_refresh_http_401: Your session has expired",
        }

        self.assertEqual(normalized_health_status(record), "invalid")
        self.assertEqual(account_public_view(record)["health_status"], "invalid")

    def test_expired_access_token_401_requires_reauthentication(self):
        record = {
            "email": "expired@example.com",
            "access_token": _jwt({"exp": 1}),
            "health_status": "invalid",
            "health_error": "token invalidated (/backend-api/me)",
        }
        self.assertEqual(normalized_health_status(record), "reauth_required")
        public = account_public_view(record)
        self.assertEqual(public["health_status"], "reauth_required")
        self.assertIn("token invalidated", public["health_error"])

    def test_oauth_refresh_401_raises_invalid_token_error(self):
        from common.account_health import InvalidAccessTokenError, refresh_oauth_tokens

        response = MagicMock(status_code=401, text="")
        response.json.return_value = {"message": "Your session has expired"}
        session = MagicMock()
        session.post.return_value = response

        with patch("common.account_health._session", return_value=session):
            with self.assertRaisesRegex(InvalidAccessTokenError, "oauth_refresh_http_401"):
                refresh_oauth_tokens({"refresh_token": "expired-refresh-token"}, force=True)

        session.close.assert_called_once_with()

    def test_account_health_identity_reuses_protocol_fingerprint(self):
        from common.account_health import _request_identity

        record = {
            "email": "protocol@example.com",
            "browser_type": "curl_cffi/chrome145",
            "tls_impersonate": "chrome145",
            "user_agent": "Mozilla/5.0 Chrome/145.0.0.0 Safari/537.36",
            "sec_ch_ua": '"Chromium";v="145", "Google Chrome";v="145"',
            "platform_version": "15.0.0",
            "accept_language": "en-GB,en;q=0.9",
            "device_id": "device-145",
        }
        identity = _request_identity(record)

        self.assertEqual(identity["impersonate"], "chrome145")
        self.assertIn("Chrome/145", identity["user_agent"])
        self.assertEqual(identity["device_id"], "device-145")
        self.assertEqual(identity["platform_version"], "15.0.0")

    def test_account_health_identity_fallback_device_is_stable(self):
        from common.account_health import _request_identity

        first = _request_identity({"email": "legacy@example.com"})
        second = _request_identity({"email": "legacy@example.com"})

        self.assertEqual(first["device_id"], second["device_id"])
        self.assertEqual(first["impersonate"], "firefox147")
        self.assertIn("Firefox/147.0", first["user_agent"])
        self.assertEqual(first["sec_ch_ua"], "")
        self.assertEqual(first["locale"], "ja-JP")
        self.assertEqual(first["timezone"], "Asia/Tokyo")
        self.assertEqual(first["timezone_offset_min"], "-540")

    def test_oauth_refresh_uses_matching_persisted_identity(self):
        from common.account_health import refresh_oauth_tokens

        response = MagicMock(status_code=200, text="")
        response.json.return_value = {
            "access_token": "fresh-access",
            "refresh_token": "fresh-refresh",
        }
        session = MagicMock()
        session.post.return_value = response
        record = {
            "email": "protocol@example.com",
            "refresh_token": "old-refresh",
            "browser_type": "curl_cffi/chrome145",
            "device_id": "device-145",
        }

        with patch("common.account_health._session", return_value=session) as factory:
            refreshed = refresh_oauth_tokens(record, force=True)

        factory.assert_called_once_with("", impersonate="chrome145")
        headers = session.post.call_args.kwargs["headers"]
        self.assertIn("Chrome/145.0.0.0", headers["User-Agent"])
        self.assertEqual(headers["OAI-Device-Id"], "device-145")
        self.assertEqual(refreshed["refresh_token"], "fresh-refresh")
        session.close.assert_called_once_with()

    def test_fresh_oauth_token_is_not_refreshed_immediately(self):
        from common.account_health import refresh_oauth_tokens

        record = {
            "access_token": _jwt({"exp": int(time.time()) + 3600}),
            "refresh_token": "refresh-token",
        }
        with patch("common.account_health._session") as factory:
            refreshed = refresh_oauth_tokens(record)

        self.assertEqual(refreshed, record)
        factory.assert_not_called()

    def test_oauth_token_near_expiry_is_refreshed(self):
        from common.account_health import refresh_oauth_tokens

        response = MagicMock(status_code=200, text="")
        response.json.return_value = {
            "access_token": "fresh-access",
            "refresh_token": "fresh-refresh",
        }
        session = MagicMock()
        session.post.return_value = response
        record = {
            "access_token": _jwt({"exp": int(time.time()) + 120}),
            "refresh_token": "old-refresh",
        }

        with patch("common.account_health._session", return_value=session):
            refreshed = refresh_oauth_tokens(record)

        self.assertEqual(refreshed["access_token"], "fresh-access")
        self.assertEqual(refreshed["refresh_token"], "fresh-refresh")

    def test_nextauth_session_refreshes_chatgpt_access_before_oauth(self):
        from common.account_health import refresh_chatgpt_session_tokens

        fresh_access = _jwt(
            {
                "exp": int(time.time()) + 3600,
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "account-session",
                    "chatgpt_plan_type": "free",
                },
            }
        )
        response = MagicMock(status_code=200, text="")
        response.json.return_value = {
            "accessToken": fresh_access,
            "account": {"id": "account-session", "planType": "free"},
        }
        session = MagicMock()
        session.get.return_value = response
        source = {
            "email": "session@example.com",
            "access_token": _jwt({"exp": 1}),
            "session_token": "nextauth-session",
            "refresh_token": "platform-refresh",
        }

        with patch("common.account_health._session", return_value=session):
            refreshed = refresh_chatgpt_session_tokens(source)

        self.assertEqual(refreshed["access_token"], fresh_access)
        self.assertEqual(refreshed["account_id"], "account-session")
        session.cookies.set.assert_called_once_with(
            "__Secure-next-auth.session-token",
            "nextauth-session",
            domain=".chatgpt.com",
            path="/",
        )
        session.close.assert_called_once_with()

    def test_health_401_prefers_nextauth_session_over_platform_refresh(self):
        from common.account_health import InvalidAccessTokenError, check_account

        source = {
            "email": "session@example.com",
            "access_token": _jwt({"exp": int(time.time()) + 3600}),
            "session_token": "nextauth-session",
            "refresh_token": "platform-refresh",
        }
        refreshed = {
            **source,
            "access_token": _jwt({"exp": int(time.time()) + 7200}),
            "account_id": "account-session",
        }
        alive = {
            **refreshed,
            "health_status": "alive",
            "health_error": "",
        }
        with (
            patch(
                "common.account_health.refresh_chatgpt_session_tokens",
                side_effect=[dict(source), refreshed],
            ) as nextauth,
            patch("common.account_health.refresh_oauth_tokens") as oauth,
            patch(
                "common.account_health.fetch_remote_account_info",
                side_effect=[
                    InvalidAccessTokenError("token invalidated (/backend-api/me)"),
                    alive,
                ],
            ),
            patch("common.account_health.upsert_account_record"),
        ):
            result = check_account(source)

        self.assertEqual(result["health_status"], "alive")
        self.assertEqual(nextauth.call_args_list[-1].kwargs, {"force": True})
        oauth.assert_not_called()

    def test_mail_stats_do_not_count_active_cooldown_as_available(self):
        import web_app

        pool = [{"email": "cooldown@icloud.com"}, {"email": "free@icloud.com"}]
        state = {
            "cooldown@icloud.com": {
                "state": "cooldown",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        }

        stats = web_app._pool_stats_for(pool, state)

        self.assertEqual(stats["available"], 1)
        self.assertEqual(stats["cooldown"], 1)

    def test_browser_capabilities_only_exposes_free_local_providers(self):
        from web_app import _browser_capabilities

        with (
            patch("web_app.importlib.util.find_spec", return_value=object()),
            patch("local_browser.resolve_chrome_executable", return_value="/usr/bin/google-chrome"),
            patch("local_browser.resolve_obscura_binary", return_value="/app/bin/obscura"),
            patch("local_browser.CamoufoxBrowser.is_available", return_value=True),
            patch("local_browser.ObscuraBrowser.is_available", return_value=True),
        ):
            result = _browser_capabilities({"fingerprint": {"browser": "camoufox"}})
        self.assertEqual([item["id"] for item in result["items"]], ["local", "camoufox", "obscura"])
        self.assertEqual(result["available_count"], 3)

    def test_log_stream_delta_handles_deque_rotation(self):
        from web_app import _log_stream_delta

        prev = ["1", "2", "3"]
        cur = ["2", "3", "4"]
        self.assertEqual(_log_stream_delta(prev, cur), ["4"])
        self.assertEqual(_log_stream_delta(cur, cur), [])
        self.assertEqual(_log_stream_delta([], ["a", "b"]), ["a", "b"])

    def test_log_stream_delta_recovers_after_gap(self):
        from web_app import _log_stream_delta

        self.assertEqual(_log_stream_delta(["old"], ["new1", "new2"]), ["new1", "new2"])

    def test_task_card_records_and_freezes_elapsed_time(self):
        import web_app

        state = web_app.JobState()
        with patch("web_app.time.time", side_effect=[1000.0, 1012.9, 9999.0]):
            state.task_event(1, "running", step="开始")
            state.task_event(1, "success", step="完成")
            card = state.tasks_snapshot()[0]
        self.assertEqual(card["started_at_epoch"], 1000.0)
        self.assertEqual(card["finished_at_epoch"], 1012.9)
        self.assertEqual(card["elapsed_sec"], 12.9)
        self.assertEqual(card["steps"][-1]["text"], "完成 (13s)")

    def test_task_card_clears_failed_attempt_error_after_retry_success(self):
        import web_app

        state = web_app.JobState()
        state.task_event(1, "running", step="第 1 次尝试")
        state.task_event(1, "failed", error="password_add_http_409")
        state.task_event(1, "running", step="第 2 次尝试")
        self.assertNotIn("error", state.tasks_snapshot()[0])

        state.task_event(1, "success", step="注册完成")
        card = state.tasks_snapshot()[0]
        self.assertEqual(card["status"], "success")
        self.assertNotIn("error", card)

    def test_task_card_tracks_password_reset_skip_without_counting_failure(self):
        import web_app

        state = web_app.JobState()
        state.task_event(1, "running", step="开始")
        state.task_event(
            1,
            "skipped",
            skip_reason="password_reset_required",
            step="账号已有密码，已跳过",
        )

        snapshot = state.snapshot()
        card = state.tasks_snapshot()[0]
        self.assertEqual(snapshot["success_count"], 0)
        self.assertEqual(snapshot["fail_count"], 0)
        self.assertEqual(snapshot["skipped_count"], 1)
        self.assertEqual(card["status"], "skipped")
        self.assertEqual(card["skip_reason"], "password_reset_required")

    def test_task_stream_revision_changes_with_task_updates(self):
        import web_app

        state = web_app.JobState()
        initial_revision, initial_items = state.task_stream_snapshot()
        state.task_event(1, "running", step="开始")
        running_revision, running_items = state.task_stream_snapshot()
        state.task_event(1, "success", step="完成")
        finished_revision, finished_items = state.task_stream_snapshot()

        self.assertEqual(initial_items, [])
        self.assertGreater(running_revision, initial_revision)
        self.assertGreater(finished_revision, running_revision)
        self.assertEqual(running_items[0]["status"], "running")
        self.assertEqual(finished_items[0]["status"], "success")
        self.assertEqual(state.snapshot()["task_revision"], finished_revision)

    def test_terminal_task_persistence_is_coalesced(self):
        import web_app

        timers = []

        class FakeTimer:
            def __init__(self, delay, callback):
                self.delay = delay
                self.callback = callback
                self.daemon = False
                timers.append(self)

            def start(self):
                return None

        with (
            patch("web_app.threading.Timer", FakeTimer),
            patch("web_app.save_task_runtime") as save_runtime,
        ):
            state = web_app.JobState(persist=True)
            state.task_event(1, "success", step="完成")
            state.task_event(2, "failed", step="失败")
            self.assertEqual(len(timers), 1)
            save_runtime.assert_not_called()

            state._flush_scheduled_persist()

        save_runtime.assert_called_once()
        saved_cards = save_runtime.call_args.args[0]
        self.assertEqual([item["status"] for item in saved_cards], ["success", "failed"])

    def test_log_stream_only_repeats_heartbeat_when_state_is_unchanged(self):
        import web_app

        async def scenario():
            request = MagicMock()
            request.is_disconnected = AsyncMock(side_effect=[False, False, True])
            state = web_app.JobState()
            state.task_event(1, "running", step="开始")
            with (
                patch.object(web_app, "JOB", state),
                patch("web_app.asyncio.sleep", new=AsyncMock()),
                patch("web_app.time.monotonic", side_effect=[100.0, 101.0]),
            ):
                response = await web_app.logs_stream(request)
                chunks = []
                async for chunk in response.body_iterator:
                    chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
                return "".join(chunks)

        stream = asyncio.run(scenario())
        self.assertEqual(stream.count("event: tasks"), 1)
        self.assertEqual(stream.count("event: status"), 1)
        self.assertEqual(stream.count("event: heartbeat"), 1)

    def test_log_stream_exits_when_server_shutdown_starts(self):
        import web_app

        async def scenario():
            request = MagicMock()
            request.is_disconnected = AsyncMock(return_value=False)
            web_app._SERVER_SHUTDOWN_EVENT.set()
            try:
                response = await web_app.logs_stream(request)
                chunks = []
                async for chunk in response.body_iterator:
                    chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
                return "".join(chunks)
            finally:
                web_app._SERVER_SHUTDOWN_EVENT.clear()

        stream = asyncio.run(scenario())
        self.assertEqual(stream, "retry: 1500\n\n")

    def test_stop_idle_job_returns_without_deadlock(self):
        import web_app

        with web_app.JOB.lock:
            web_app.JOB.running = False
        result = asyncio.run(asyncio.wait_for(web_app.job_stop(), timeout=0.5))
        self.assertEqual(result["message"], "not running")
        self.assertFalse(result["job"]["running"])

    def test_stop_running_browser_job_cancels_root_without_releasing_active_mailboxes(self):
        import web_app

        async def scenario():
            cancel_event = asyncio.Event()
            registration_task = asyncio.create_task(asyncio.sleep(60))

            def discard_force_close(coro, _loop):
                coro.close()
                return None

            async def inline_to_thread(func, *args, **kwargs):
                return func(*args, **kwargs)

            with web_app.JOB.lock:
                old = (
                    web_app.JOB.running,
                    web_app.JOB.stop_requested,
                    web_app.JOB.params,
                    web_app.JOB._loop,
                    web_app.JOB._cancel_event,
                    web_app.JOB._registration_task,
                )
                web_app.JOB.running = True
                web_app.JOB.stop_requested = False
                web_app.JOB.params = {"engine": "browser"}
                web_app.JOB._loop = asyncio.get_running_loop()
                web_app.JOB._cancel_event = cancel_event
                web_app.JOB._registration_task = registration_task
            try:
                with (
                    patch(
                        "register_gpt.force_close_all_sessions",
                        new=AsyncMock(return_value=1),
                    ) as close_all,
                    patch(
                        "web_app.asyncio.run_coroutine_threadsafe",
                        side_effect=discard_force_close,
                    ),
                    patch("web_app.asyncio.to_thread", new=inline_to_thread),
                    patch("web_app.release_stale_in_use", return_value=0) as release_stale,
                ):
                    result = await web_app.job_stop()
                    await asyncio.sleep(0)
                    self.assertEqual(result["message"], "stopping")
                    self.assertTrue(cancel_event.is_set())
                    self.assertGreater(registration_task.cancelling(), 0)
                    try:
                        await registration_task
                    except asyncio.CancelledError:
                        pass
                    self.assertTrue(registration_task.cancelled())
                    release_stale.assert_called_once_with()
                    close_all.assert_called_once()
            finally:
                if not registration_task.done():
                    registration_task.cancel()
                with web_app.JOB.lock:
                    (
                        web_app.JOB.running,
                        web_app.JOB.stop_requested,
                        web_app.JOB.params,
                        web_app.JOB._loop,
                        web_app.JOB._cancel_event,
                        web_app.JOB._registration_task,
                    ) = old

        asyncio.run(scenario())

    def test_reset_failed_mail_rejects_running_job(self):
        import web_app

        with web_app.JOB.lock:
            old_running = web_app.JOB.running
            web_app.JOB.running = True
        try:
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(web_app.reset_failed_mail())
            self.assertEqual(caught.exception.status_code, 409)
        finally:
            with web_app.JOB.lock:
                web_app.JOB.running = old_running

    def test_session_query_parameter_is_not_an_auth_credential(self):
        import web_app

        request = SimpleNamespace(
            cookies={},
            headers={},
            query_params={"token": "signed-session"},
        )
        self.assertEqual(web_app._extract_session(request), "")

    def test_forwarded_client_ip_requires_trusted_proxy(self):
        import web_app

        direct = SimpleNamespace(
            client=SimpleNamespace(host="203.0.113.20"),
            headers={"X-Forwarded-For": "198.51.100.8"},
        )
        proxied = SimpleNamespace(
            client=SimpleNamespace(host="127.0.0.1"),
            headers={"x-forwarded-for": "198.51.100.8"},
        )
        with patch.object(web_app, "_web_cfg", return_value={"trusted_proxies": ["127.0.0.1"]}):
            self.assertEqual(web_app._request_client_ip(direct), "203.0.113.20")
            self.assertEqual(web_app._request_client_ip(proxied), "198.51.100.8")

    def test_start_rejects_running_job_before_releasing_mailboxes(self):
        import web_app

        with web_app.JOB.lock:
            old_running = web_app.JOB.running
            web_app.JOB.running = True
        try:
            with patch("mail.release_stale_in_use") as release:
                with self.assertRaises(HTTPException) as caught:
                    asyncio.run(web_app.job_start(web_app.StartJobRequest()))
            self.assertEqual(caught.exception.status_code, 409)
            release.assert_not_called()
        finally:
            with web_app.JOB.lock:
                web_app.JOB.running = old_running

    def test_start_accepts_protocol_engine_without_runtime_worker_cap(self):
        import web_app

        state = web_app.JobState()
        worker = MagicMock()
        config = {
            "registration": {
                "engine": "browser",
                "max_concurrency": 1,
                "protocol_max_concurrency": 4,
            },
            "mail": {},
        }

        async def inline_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        with (
            patch.object(web_app, "JOB", state),
            patch("web_app.asyncio.to_thread", new=inline_to_thread),
            patch("web_app._read_config", return_value=config),
            patch(
                "web_app.list_groups",
                return_value=[{"id": "grp_protocol", "name": "Protocol"}],
            ),
            patch("mail.release_stale_in_use", return_value=0),
            patch(
                "mail.count_pool_capacity",
                return_value={"available": 20, "total": 20},
            ),
            patch("web_app.threading.Thread", return_value=worker),
        ):
            result = asyncio.run(
                web_app.job_start(
                    web_app.StartJobRequest(
                        engine="protocol",
                        total=12,
                        concurrency=9,
                        group_id="grp_protocol",
                    )
                )
            )

        self.assertTrue(result["ok"])
        self.assertEqual(state.params["engine"], "protocol")
        self.assertEqual(state.params["total"], 12)
        self.assertEqual(state.params["concurrency"], 9)
        self.assertEqual(state.params["_requested_concurrency"], 9)
        self.assertEqual(state.params["requested_concurrency"], 9)
        self.assertEqual(state.params["_resource_mode"], "independent")
        worker.start.assert_called_once_with()

    def test_start_fixed_registered_primary_uses_one_alias_concurrency(self):
        import web_app

        state = web_app.JobState()
        worker = MagicMock()
        aliases = ["main+splitone@icloud.com"]

        async def inline_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        with (
            patch.object(web_app, "JOB", state),
            patch("web_app.asyncio.to_thread", new=inline_to_thread),
            patch(
                "web_app._read_config",
                return_value={"registration": {"engine": "protocol"}, "mail": {}},
            ),
            patch(
                "web_app.list_groups",
                return_value=[{"id": "grp_alias", "name": "Alias"}],
            ),
            patch("mail.release_stale_in_use", return_value=0),
            patch(
                "mail.count_pool_capacity",
                return_value={
                    "available": 1,
                    "total": 1,
                    "fixed_email_mode": "alias_batch",
                    "emails": aliases,
                },
            ) as capacity,
            patch("web_app.threading.Thread", return_value=worker),
        ):
            result = asyncio.run(
                web_app.job_start(
                    web_app.StartJobRequest(
                        engine="protocol",
                        total=1,
                        concurrency=10,
                        email="main@icloud.com",
                        group_id="grp_alias",
                    )
                )
            )

        self.assertTrue(result["ok"])
        self.assertEqual(state.params["total"], 1)
        self.assertEqual(state.params["concurrency"], 1)
        self.assertEqual(state.params["requested_concurrency"], 1)
        self.assertTrue(state.params["_fixed_alias_batch"])
        self.assertEqual(state.params["group_plan"], ["grp_alias"] * 1)
        self.assertEqual(capacity.call_args.kwargs["fixed_alias_count"], 1)
        worker.start.assert_called_once_with()

    def test_registration_and_reauth_models_accept_large_concurrency_values(self):
        import web_app

        registration = web_app.StartJobRequest(concurrency=10_000)
        reauth = web_app.AccountReauthBatchRequest(
            keys=["account-key"],
            concurrency=10_000,
        )

        self.assertEqual(registration.concurrency, 10_000)
        self.assertEqual(reauth.concurrency, 10_000)

    def test_proxy_pool_write_rejects_path_outside_data_directory(self):
        import web_app

        with web_app.JOB.lock:
            old_running = web_app.JOB.running
            web_app.JOB.running = False
        try:
            body = web_app.ProxyUpdate(
                pool_file="/tmp/outside-proxy-pool.txt",
                pool_text="http://127.0.0.1:8080",
            )
            with patch.object(web_app, "_read_config", return_value={}):
                with self.assertRaises(HTTPException) as caught:
                    asyncio.run(web_app.put_proxy(body))
            self.assertEqual(caught.exception.status_code, 400)
        finally:
            with web_app.JOB.lock:
                web_app.JOB.running = old_running

    def test_proxy_update_is_allowed_while_job_is_running(self):
        import web_app

        cfg = {"proxy": {"enabled": False}}
        body = web_app.ProxyUpdate(
            enabled=True,
            url="http://proxy.example:8080",
            rotate_per_task=True,
        )
        with web_app.JOB.lock:
            old_running = web_app.JOB.running
            web_app.JOB.running = True
        try:
            with (
                patch.object(web_app, "_read_config", return_value=cfg),
                patch.object(web_app, "_write_config") as write_config,
            ):
                result = asyncio.run(web_app.put_proxy(body))
            self.assertTrue(result["ok"])
            self.assertTrue(cfg["proxy"]["enabled"])
            self.assertEqual(cfg["proxy"]["url"], "http://proxy.example:8080")
            write_config.assert_called_once_with(cfg)
        finally:
            with web_app.JOB.lock:
                web_app.JOB.running = old_running


class RegistrationPageStateTests(unittest.TestCase):
    def test_empty_hydrating_page_is_not_misclassified_as_cloudflare(self):
        from register.page import _is_cf_blocked

        with (
            patch("register.page.safe_count", new=AsyncMock(return_value=0)),
            patch("register.page.safe_body_text", new=AsyncMock(return_value="")),
        ):
            blocked = asyncio.run(_is_cf_blocked(MagicMock()))

        self.assertFalse(blocked)

    def test_explicit_turnstile_dom_is_classified_as_cloudflare(self):
        from register.page import _is_cf_blocked

        counts = iter([0, 1])

        async def safe_count(*_args, **_kwargs):
            return next(counts)

        with (
            patch("register.page.safe_count", side_effect=safe_count),
            patch("register.page.safe_body_text", new=AsyncMock(return_value="")),
        ):
            blocked = asyncio.run(_is_cf_blocked(MagicMock()))

        self.assertTrue(blocked)

    def test_page_url_preserves_case_sensitive_error_payload(self):
        from register.page import _page_url, auth_error_code_from_url

        payload = base64.b64encode(
            json.dumps(
                {
                    "kind": "AuthApiFailure",
                    "errorCode": "rate_limit_exceeded",
                }
            ).encode()
        ).decode()
        url = f"https://auth.openai.com/error?payload={payload}&session_id=test"
        page = MagicMock()
        page.evaluate = AsyncMock(return_value=url)

        current = asyncio.run(_page_url(page))

        self.assertEqual(current, url)
        self.assertEqual(auth_error_code_from_url(current), "rate_limit_exceeded")

        page.evaluate = AsyncMock(side_effect=RuntimeError("navigation race"))
        page.url = url
        self.assertEqual(asyncio.run(_page_url(page)), url)

    def test_auth_error_payload_extracts_rate_limit_code(self):
        from register.page import auth_error_code_from_url

        payload = base64.b64encode(
            json.dumps(
                {
                    "kind": "AuthApiFailure",
                    "errorCode": "rate_limit_exceeded",
                }
            ).encode()
        ).decode()

        self.assertEqual(
            auth_error_code_from_url(
                f"https://auth.openai.com/error?payload={payload}&session_id=test"
            ),
            "rate_limit_exceeded",
        )
        self.assertEqual(auth_error_code_from_url("https://auth.openai.com/about-you"), "")

    def test_direct_nextauth_error_url_is_classified(self):
        from register.page import _is_auth_error_url, auth_error_code_from_url

        url = "https://chatgpt.com/auth/error?error=undefined"
        self.assertTrue(_is_auth_error_url(url))
        self.assertEqual(auth_error_code_from_url(url), "undefined")
        self.assertEqual(
            auth_error_code_from_url(
                "https://chatgpt.com/auth/error?error=OAuthCallback"
            ),
            "oauthcallback",
        )

    def test_existing_account_password_route_is_distinct_from_signup_password(self):
        from register.page import _is_existing_account_password_url

        self.assertTrue(
            _is_existing_account_password_url(
                "https://auth.openai.com/log-in/password"
            )
        )
        self.assertFalse(
            _is_existing_account_password_url(
                "https://auth.openai.com/create-account/password"
            )
        )

    def test_password_page_can_request_email_code_with_current_browser_session(self):
        from register.page import switch_password_to_email_code

        empty = MagicMock()
        empty.count = AsyncMock(return_value=0)
        page = MagicMock()
        page.get_by_role.return_value = empty
        page.locator.return_value = empty
        page.evaluate = AsyncMock(
            side_effect=[False] * 11 + [{"ok": True, "status": 200}]
        )
        page.goto = AsyncMock()

        switched = asyncio.run(switch_password_to_email_code(page))

        self.assertTrue(switched)
        page.goto.assert_awaited_once_with(
            "https://auth.openai.com/email-verification",
            timeout=10000,
            wait_until="commit",
        )
        request_script = page.evaluate.await_args_list[-1].args[0]
        self.assertIn("/api/accounts/passwordless/send-otp", request_script)
        self.assertIn("credentials: 'include'", request_script)

    def test_resend_otp_falls_back_to_verified_same_origin_request(self):
        from register.page import _click_resend_code

        empty = MagicMock()
        empty.count = AsyncMock(return_value=0)
        page = MagicMock()
        page.get_by_role.return_value = empty
        page.locator.return_value = empty
        page.evaluate = AsyncMock(return_value={"status": 200, "ok": True})

        status = asyncio.run(_click_resend_code(page))

        self.assertEqual(status, 200)
        script = page.evaluate.await_args.args[0]
        self.assertIn("/api/accounts/email-otp/resend", script)
        self.assertIn("credentials: 'include'", script)

    def test_browser_resend_never_uses_http_fallback(self):
        from register.page import _click_resend_code

        empty = MagicMock()
        empty.count = AsyncMock(return_value=0)
        page = MagicMock()
        page.get_by_role.return_value = empty
        page.locator.return_value = empty
        page.evaluate = AsyncMock(return_value={"status": 200, "ok": True})

        status = asyncio.run(
            _click_resend_code(page, allow_http_fallback=False)
        )

        self.assertEqual(status, 0)
        page.evaluate.assert_not_awaited()

    def test_browser_email_submit_requires_real_signin_request(self):
        from register.page import EMAIL_INPUT_SELECTOR, submit_email_browser_ui

        handlers = {}
        email_input = MagicMock()
        email_input.wait_for = AsyncMock(return_value=None)
        email_input.click = AsyncMock(return_value=None)
        email_input.fill = AsyncMock(return_value=None)
        email_input.press = AsyncMock(return_value=None)
        email_input.input_value = AsyncMock(return_value="browser@icloud.com")
        email_input.focus = AsyncMock(return_value=None)
        email_input.press_sequentially = AsyncMock(return_value=None)
        input_query = MagicMock()
        input_query.first = email_input

        request = MagicMock()
        request.url = "https://chatgpt.com/api/auth/signin/openai"
        request.method = "POST"
        button = MagicMock()
        button.count = AsyncMock(return_value=1)

        async def click_button(**_kwargs):
            handlers["request"](request)

        button.click = AsyncMock(side_effect=click_button)
        button_query = MagicMock()
        button_query.count = AsyncMock(return_value=1)
        button_query.first = button
        absent_button = MagicMock()
        absent_button.count = AsyncMock(return_value=0)
        absent_query = MagicMock()
        absent_query.first = absent_button

        page = MagicMock()
        page.url = "https://chatgpt.com/auth/login"
        page.locator.return_value = input_query
        page.get_by_role.side_effect = lambda _role, name, **_kwargs: (
            button_query if name == "Continue" else absent_query
        )
        page.wait_for_function = AsyncMock(return_value=MagicMock())
        page.on.side_effect = lambda event, callback: handlers.__setitem__(event, callback)

        with (
            patch(
                "register.page._page_url",
                new=AsyncMock(return_value="https://chatgpt.com/auth/login"),
            ),
            patch("register.page.safe_count", new=AsyncMock(return_value=0)),
            patch("register.page.fill_email_verified", new=AsyncMock(return_value=True)),
        ):
            status = asyncio.run(
                submit_email_browser_ui(page, "browser@icloud.com")
            )

        self.assertEqual(status, "submitted")
        button.click.assert_awaited_once()
        self.assertEqual(
            page.wait_for_function.await_args.kwargs.get("arg"),
            EMAIL_INPUT_SELECTOR,
        )
        page.evaluate.assert_not_called()
        page.remove_listener.assert_called_once()

    def test_browser_email_submit_accepts_japanese_continue_button(self):
        from register.page import submit_email_browser_ui

        handlers = {}
        email_input = MagicMock()
        email_input.wait_for = AsyncMock(return_value=None)
        email_input.input_value = AsyncMock(return_value="jp@icloud.com")
        input_query = MagicMock()
        input_query.first = email_input

        absent = MagicMock()
        absent.count = AsyncMock(return_value=0)
        japanese_button = MagicMock()
        japanese_button.count = AsyncMock(return_value=1)
        request = MagicMock(
            url="https://chatgpt.com/api/auth/signin/openai",
            method="POST",
        )

        async def click_button(**_kwargs):
            handlers["request"](request)

        japanese_button.first.click = AsyncMock(side_effect=click_button)
        page = MagicMock()
        page.url = "https://chatgpt.com/auth/login"
        page.locator.return_value = input_query
        page.wait_for_function = AsyncMock(return_value=MagicMock())
        page.get_by_role.side_effect = lambda _role, name, **_kwargs: (
            japanese_button if name == "続行" else absent
        )
        page.on.side_effect = lambda event, callback: handlers.__setitem__(event, callback)

        with (
            patch(
                "register.page._page_url",
                new=AsyncMock(return_value="https://chatgpt.com/auth/login"),
            ),
            patch("register.page.safe_count", new=AsyncMock(return_value=0)),
            patch("register.page.fill_email_verified", new=AsyncMock(return_value=True)),
        ):
            status = asyncio.run(submit_email_browser_ui(page, "jp@icloud.com"))

        self.assertEqual(status, "submitted")
        japanese_button.first.click.assert_awaited_once()
        self.assertEqual(page.wait_for_function.await_args.kwargs["timeout"], 6000)

    def test_browser_email_submit_consumes_playwright_stability_timeout(self):
        from register.page import submit_email_browser_ui

        page = MagicMock()
        page.url = "https://chatgpt.com/auth/login"
        page.wait_for_function = AsyncMock(
            side_effect=RuntimeError("Timeout 6000ms exceeded.")
        )

        with patch(
            "register.page._page_url",
            new=AsyncMock(return_value="https://chatgpt.com/auth/login"),
        ):
            status = asyncio.run(
                submit_email_browser_ui(page, "browser@icloud.com")
            )

        self.assertEqual(status, "form_unstable")
        page.wait_for_function.assert_awaited_once()
        page.evaluate.assert_not_called()

    def test_login_email_query_preserves_blank_value(self):
        from register.page import login_email_query

        self.assertIsNone(login_email_query("https://chatgpt.com/auth/login"))
        self.assertEqual(
            login_email_query("https://chatgpt.com/auth/login?email="), ""
        )
        self.assertEqual(
            login_email_query(
                "https://chatgpt.com/auth/login?email=user%40example.com"
            ),
            "user@example.com",
        )

    def test_nextauth_fallback_uses_browser_session_and_authorize_url(self):
        from register.page import start_nextauth_email_flow

        context = type("Context", (), {})()
        context.cookies = AsyncMock(
            return_value=[{"name": "oai-did", "value": "device-id"}]
        )
        page = type("Page", (), {})()
        page.context = context
        page.url = "https://chatgpt.com/auth/login"

        async def evaluate(script, arg=None):
            if "fetch('/api/auth/csrf'" in script:
                return {
                "ok": True,
                "stage": "signin",
                "status": 200,
                "url": "https://auth.openai.com/api/accounts/authorize?state=test",
                }
            return page.url

        page.evaluate = AsyncMock(side_effect=evaluate)

        async def goto(url, **_kwargs):
            page.url = url

        page.goto = AsyncMock(side_effect=goto)

        status = asyncio.run(
            start_nextauth_email_flow(page, "user@icloud.com")
        )

        self.assertEqual(status, "advanced")
        call = next(
            item for item in page.evaluate.call_args_list if len(item.args) > 1
        )
        args = call.args[1]
        self.assertEqual(args["email"], "user@icloud.com")
        self.assertEqual(args["deviceId"], "device-id")
        page.goto.assert_awaited_once()
        self.assertEqual(
            page.goto.await_args.args[0],
            "https://auth.openai.com/api/accounts/authorize?state=test",
        )

    def test_nextauth_prefers_browser_context_request_channel(self):
        from register.page import start_nextauth_email_flow

        csrf_response = MagicMock(status=200, ok=True)
        csrf_response.json = AsyncMock(return_value={"csrfToken": "csrf-token"})
        signin_response = MagicMock(status=200, ok=True)
        signin_response.json = AsyncMock(
            return_value={
                "url": "https://auth.openai.com/api/accounts/authorize?state=context"
            }
        )
        request_context = MagicMock()
        request_context.get = AsyncMock(return_value=csrf_response)
        request_context.post = AsyncMock(return_value=signin_response)
        context = type("Context", (), {})()
        context.cookies = AsyncMock(
            return_value=[{"name": "oai-did", "value": "device-id"}]
        )
        context.request = request_context
        page = type("Page", (), {"url": "https://chatgpt.com/auth/login"})()
        page.context = context

        async def evaluate(script, arg=None):
            return page.url

        page.evaluate = AsyncMock(side_effect=evaluate)

        async def goto(url, **_kwargs):
            page.url = url

        page.goto = AsyncMock(side_effect=goto)

        status = asyncio.run(start_nextauth_email_flow(page, "context@icloud.com"))

        self.assertEqual(status, "advanced")
        request_context.get.assert_awaited_once()
        request_context.post.assert_awaited_once()
        post_kwargs = request_context.post.await_args.kwargs
        self.assertEqual(post_kwargs["form"]["csrfToken"], "csrf-token")
        self.assertIn(
            "login_hint=context%40icloud.com",
            request_context.post.await_args.args[0],
        )
        page.goto.assert_awaited_once()
        self.assertFalse(
            any(
                "fetch('/api/auth/csrf'" in str(call)
                for call in page.evaluate.call_args_list
            )
        )

    def test_nextauth_navigation_race_accepts_completed_authorize_navigation(self):
        from register.page import start_nextauth_email_flow

        csrf_response = MagicMock(status=200, ok=True)
        csrf_response.json = AsyncMock(return_value={"csrfToken": "csrf-token"})
        signin_response = MagicMock(status=200, ok=True)
        signin_response.json = AsyncMock(
            return_value={
                "url": "https://auth.openai.com/api/accounts/authorize?state=race"
            }
        )
        request_context = MagicMock()
        request_context.get = AsyncMock(return_value=csrf_response)
        request_context.post = AsyncMock(return_value=signin_response)
        context = type("Context", (), {})()
        context.cookies = AsyncMock(return_value=[])
        context.request = request_context
        page = type("Page", (), {"url": "https://chatgpt.com/auth/login"})()
        page.context = context

        async def evaluate(script, _arg=None):
            return page.url

        page.evaluate = AsyncMock(side_effect=evaluate)

        async def goto(_url, **_kwargs):
            page.url = "https://auth.openai.com/email-verification"
            raise RuntimeError("Page.goto: Timeout 8000ms exceeded")

        page.goto = AsyncMock(side_effect=goto)

        status = asyncio.run(start_nextauth_email_flow(page, "race@icloud.com"))

        self.assertEqual(status, "advanced")
        page.goto.assert_awaited_once()

    def test_auto_sent_otp_skips_resend(self):
        from register.flow import _poll_auto_sent_code_before_resend

        mailbox = {"address": "first@icloud.com"}
        loop = MagicMock()

        async def run_executor(_executor, callback):
            return callback()

        loop.run_in_executor = AsyncMock(side_effect=run_executor)
        with (
            patch("register.flow.asyncio.get_running_loop", return_value=loop),
            patch("register.flow.wait_for_code", return_value="123456") as wait_code,
            patch(
                "register.flow._click_resend_code", new=AsyncMock(return_value=200)
            ) as resend,
        ):
            code, status = asyncio.run(
                _poll_auto_sent_code_before_resend(
                    MagicMock(),
                    {"wait_timeout": 90, "wait_chunk": 30, "request_timeout": 15},
                    mailbox,
                    lambda: False,
                )
            )

        self.assertEqual((code, status), ("123456", 200))
        resend.assert_not_awaited()
        wait_config = wait_code.call_args.args[0]
        self.assertEqual(wait_config["wait_timeout"], 60.0)
        self.assertEqual(wait_config["wait_chunk"], 30.0)
        self.assertEqual(wait_config["request_timeout"], 15.0)

    def test_missing_auto_sent_otp_requests_one_resend(self):
        from register.flow import _poll_auto_sent_code_before_resend

        mailbox = {"address": "resend@icloud.com"}
        loop = MagicMock()

        async def run_executor(_executor, callback):
            return callback()

        loop.run_in_executor = AsyncMock(side_effect=run_executor)
        with (
            patch("register.flow.asyncio.get_running_loop", return_value=loop),
            patch("register.flow.wait_for_code", return_value=None),
            patch(
                "register.flow._click_resend_code", new=AsyncMock(return_value=202)
            ) as resend,
        ):
            code, status = asyncio.run(
                _poll_auto_sent_code_before_resend(
                    MagicMock(), {}, mailbox, lambda: False
                )
            )

        self.assertIsNone(code)
        self.assertEqual(status, 202)
        self.assertIn("_code_not_before", mailbox)
        resend.assert_awaited_once()

    def test_otp_resend_only_accepts_success_status(self):
        from register.flow import _otp_resend_accepted

        self.assertTrue(_otp_resend_accepted(200))
        self.assertTrue(_otp_resend_accepted(202))
        self.assertFalse(_otp_resend_accepted(0))
        self.assertFalse(_otp_resend_accepted(409))
        self.assertFalse(_otp_resend_accepted(429))

    def test_browser_otp_http_200_recovers_when_spa_stays_on_verification(self):
        from register.flow import _submit_verification_code

        handlers = {}

        class OtpResponse:
            url = "https://auth.openai.com/api/accounts/email-otp/validate"
            status = 200

            async def json(self):
                return {
                    "transaction": {
                        "continueUrl": "/about-you?from=email-otp",
                    }
                }

        class Page:
            url = "https://auth.openai.com/email-verification"

            def on(self, event, callback):
                handlers[event] = callback

            def remove_listener(self, event, callback):
                self.removed = (event, callback)

            async def evaluate(self, _script, *_args):
                return self.url

            async def goto(self, url, **_kwargs):
                self.url = url

        page = Page()

        async def submit_button(*_args, **_kwargs):
            handlers["response"](OtpResponse())
            return True

        with (
            patch("register.flow.dismiss_cookie_banner", new=AsyncMock()),
            patch("register.flow.fill_otp_verified", new=AsyncMock(return_value=True)),
            patch("register.flow.wait_for_any", new=AsyncMock(return_value=False)),
            patch("register.flow.click_any_exact", new=AsyncMock(side_effect=submit_button)),
            patch("register.flow.safe_count", new=AsyncMock(return_value=0)),
            patch("register.flow.is_chatgpt_main", new=AsyncMock(return_value=False)),
            patch("register.flow.safe_body_text", new=AsyncMock(return_value="")),
            patch("register.flow._click_resend_code", new=AsyncMock()) as resend,
        ):
            result = asyncio.run(_submit_verification_code(page, "123456"))

        self.assertTrue(result.accepted)
        self.assertTrue(result.advanced)
        self.assertEqual(result.status, 200)
        self.assertIn("/about-you?from=email-otp", result.continue_url)
        self.assertIn("/about-you?from=email-otp", page.url)
        self.assertFalse(result.should_resend)
        resend.assert_not_awaited()

    def test_browser_otp_recognizes_japanese_already_verified_page(self):
        from register.flow import _otp_confirmation_present

        self.assertTrue(
            _otp_confirmation_present(
                "メールが確認されました\nお使いのメールアドレスはすでに確認済みです。"
            )
        )

    def test_accepted_otp_without_continue_url_recovers_about_you(self):
        from register.flow import _recover_after_accepted_otp

        page = type("Page", (), {})()
        page.url = "https://auth.openai.com/email-verification"

        async def goto(url, **_kwargs):
            page.url = url

        page.goto = AsyncMock(side_effect=goto)
        page.evaluate = AsyncMock(side_effect=lambda *_args: page.url)
        with (
            patch("register.flow.wait_for_any", new=AsyncMock(return_value=True)),
            patch("register.flow.safe_count", new=AsyncMock(return_value=1)),
            patch("register.flow.is_chatgpt_main", new=AsyncMock(return_value=False)),
        ):
            advanced = asyncio.run(_recover_after_accepted_otp(page))

        self.assertTrue(advanced)
        page.goto.assert_awaited_once_with(
            "https://auth.openai.com/about-you",
            timeout=20000,
            wait_until="commit",
        )

    def test_hybrid_otp_validation_uses_browser_origin_http_then_navigates(self):
        from register.page import validate_otp_via_browser_session

        page = type("Page", (), {})()
        page.url = "https://auth.openai.com/email-verification"
        page.evaluate = AsyncMock(
            side_effect=[
                page.url,
                {
                    "ok": True,
                    "status": 200,
                    "continueUrl": "https://auth.openai.com/about-you",
                },
                "https://auth.openai.com/about-you",
            ]
        )
        page.goto = AsyncMock(return_value=None)

        with patch("register.page.wait_for_any", new=AsyncMock(return_value=True)):
            result = asyncio.run(validate_otp_via_browser_session(page, "123456"))

        self.assertTrue(result)
        protocol_call = next(
            call for call in page.evaluate.call_args_list if len(call.args) > 1
        )
        self.assertIn("/api/accounts/email-otp/validate", protocol_call.args[0])
        self.assertEqual(protocol_call.args[1], "123456")
        page.goto.assert_awaited_once_with(
            "https://auth.openai.com/about-you",
            timeout=20000,
            wait_until="commit",
        )

    def test_hybrid_otp_validation_falls_back_when_success_has_no_continue_url(self):
        from register.page import validate_otp_via_browser_session

        page = type("Page", (), {})()
        page.url = "https://auth.openai.com/email-verification"
        page.evaluate = AsyncMock(
            side_effect=[
                page.url,
                {"ok": True, "status": 200, "continueUrl": ""},
                "https://auth.openai.com/about-you",
            ]
        )
        page.goto = AsyncMock(return_value=None)

        with patch("register.page.wait_for_any", new=AsyncMock(return_value=True)):
            result = asyncio.run(validate_otp_via_browser_session(page, "981015"))

        self.assertTrue(result)
        page.goto.assert_awaited_once_with(
            "https://auth.openai.com/about-you",
            timeout=20000,
            wait_until="commit",
        )

    def test_otp_fill_uses_trusted_keyboard_input(self):
        from register.page import fill_otp_verified

        state = {"value": ""}

        class Input:
            async def is_visible(self):
                return True

            async def click(self, **_kwargs):
                return None

            async def press(self, key, **_kwargs):
                if key in {"Backspace", "Delete"}:
                    state["value"] = ""

            async def input_value(self, **_kwargs):
                return state["value"]

        item = Input()

        class Inputs:
            async def count(self):
                return 1

            def nth(self, _index):
                return item

        class Keyboard:
            async def type(self, value, **_kwargs):
                state["value"] = value

        page = type("Page", (), {})()
        page.locator = lambda _selector: Inputs()
        page.keyboard = Keyboard()

        filled = asyncio.run(fill_otp_verified(page, "400004"))
        self.assertTrue(filled)
        self.assertEqual(state["value"], "400004")

    def test_create_account_response_follows_protocol_continue_url(self):
        from register.page import follow_create_account_response

        response = type("Response", (), {})()
        response.json = AsyncMock(return_value={
            "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=test",
        })
        page = type("Page", (), {})()
        page.goto = AsyncMock(return_value=None)
        page.url = "https://auth.openai.com/about-you"
        with (
            patch("register.page.wait_for_any", new=AsyncMock(return_value=True)),
            patch("register.page._about_you_left", new=AsyncMock(return_value=True)),
        ):
            followed = asyncio.run(follow_create_account_response(page, response))
        self.assertTrue(followed)
        page.goto.assert_not_awaited()

    def test_create_account_response_accepts_nested_continue_url(self):
        from register.page import follow_create_account_response

        response = type("Response", (), {"status": 200, "headers": {}})()
        response.json = AsyncMock(return_value={
            "page": {
                "payload": {
                    "continueUrl": "https://chatgpt.com/api/auth/callback/openai?code=nested",
                }
            }
        })
        page = type("Page", (), {})()
        page.goto = AsyncMock(return_value=None)
        page.url = "https://auth.openai.com/about-you"
        with (
            patch("register.page.wait_for_any", new=AsyncMock(return_value=True)),
            patch("register.page._about_you_left", new=AsyncMock(return_value=True)),
        ):
            followed = asyncio.run(follow_create_account_response(page, response))

        self.assertTrue(followed)
        page.goto.assert_not_awaited()

    def test_create_account_response_logs_identity_route_without_query(self):
        from register.page import follow_create_account_response

        response = type("Response", (), {"status": 200, "headers": {}})()
        response.json = AsyncMock(return_value={
            "page": {
                "type": "verify_identity",
                "continue_url": (
                    "https://auth.openai.com/verify-your-identity?code=secret"
                ),
            }
        })
        page = type("Page", (), {})()
        page.url = "https://auth.openai.com/verify-your-identity"
        page.goto = AsyncMock(return_value=None)
        with (
            patch("register.page._log") as log,
            patch("register.page.wait_for_any", new=AsyncMock(return_value=False)),
            patch("register.page._about_you_left", new=AsyncMock(return_value=True)),
        ):
            followed = asyncio.run(follow_create_account_response(page, response))

        self.assertTrue(followed)
        messages = "\n".join(str(item.args[0]) for item in log.call_args_list)
        self.assertIn("page=verify_identity", messages)
        self.assertIn(
            "continue=auth.openai.com/verify-your-identity", messages
        )
        self.assertIn("服务端分流到 Persona 身份核验", messages)
        self.assertNotIn("secret", messages)

    def test_create_account_without_continue_url_recovers_existing_session(self):
        from register.page import follow_create_account_response

        response = type("Response", (), {"status": 200, "headers": {}})()
        response.json = AsyncMock(return_value={"page": {"type": "done"}})
        page = object()
        with patch(
            "register.page._recover_nextauth_session",
            new=AsyncMock(return_value=True),
        ) as recover:
            followed = asyncio.run(follow_create_account_response(page, response))

        self.assertTrue(followed)
        recover.assert_awaited_once_with(page)

    def test_finish_button_schedules_one_click_and_never_force_resubmits(self):
        import inspect

        from register.page import click_finish_button

        source = inspect.getsource(click_finish_button)
        self.assertIn("setTimeout", source)
        self.assertIn("timeout=30.0", source)
        self.assertIn('return "pending"', source)
        self.assertNotIn("requestSubmit", source)
        self.assertNotIn("f.submit()", source)

    def test_japanese_birthdate_confirmation_clicks_ok(self):
        from register.page import confirm_birthdate_dialog

        body = (
            "生年月日を2000年8月13日に設定しています\n"
            "これはあくまで記録用です。誰とも共有されることはありません。\n"
            "OK\nキャンセルする"
        )
        with (
            patch("register.page.safe_body_text", new=AsyncMock(return_value=body)),
            patch("register.page.click_any_exact", new=AsyncMock(return_value=True)) as click,
            patch("register.page.asyncio.sleep", new=AsyncMock()),
        ):
            confirmed = asyncio.run(confirm_birthdate_dialog(MagicMock()))

        self.assertTrue(confirmed)
        labels = click.await_args.args[1]
        self.assertIn("OK", labels)
        self.assertIn("確認", labels)

    def test_generic_ok_button_is_not_treated_as_birthdate_confirmation(self):
        from register.page import confirm_birthdate_dialog

        response_future = MagicMock()
        response_future.done.side_effect = [False, True]
        with (
            patch(
                "register.page.safe_body_text",
                new=AsyncMock(return_value="Something went wrong\nOK"),
            ),
            patch("register.page.click_any_exact", new=AsyncMock()) as click,
            patch("register.page.asyncio.sleep", new=AsyncMock()),
        ):
            confirmed = asyncio.run(
                confirm_birthdate_dialog(
                    MagicMock(),
                    response_future=response_future,
                )
            )

        self.assertFalse(confirmed)
        click.assert_not_awaited()

    def test_create_account_does_not_replay_observed_oauth_callback(self):
        from register.page import follow_create_account_response

        response = type("Response", (), {})()
        response.json = AsyncMock(return_value={
            "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=one-time",
        })
        callback_response = type("CallbackResponse", (), {"status": 302})()

        async def scenario():
            callback_future = asyncio.get_running_loop().create_future()
            callback_future.set_result(callback_response)
            page = type("Page", (), {})()
            page.goto = AsyncMock(return_value=None)
            page.url = "https://auth.openai.com/about-you"
            with (
                patch("register.page.wait_for_any", new=AsyncMock(return_value=True)),
                patch("register.page.is_chatgpt_main", new=AsyncMock(return_value=False)),
                patch(
                    "register.page._recover_nextauth_session",
                    new=AsyncMock(return_value=True),
                ) as recover,
            ):
                followed = await follow_create_account_response(
                    page, response, callback_future
                )
            self.assertTrue(followed)
            recover.assert_awaited_once_with(page)
            page.goto.assert_not_awaited()

        asyncio.run(scenario())

    def test_create_account_response_falls_back_after_spa_wait(self):
        from register.page import follow_create_account_response

        response = type("Response", (), {})()
        response.json = AsyncMock(return_value={
            "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=test",
        })
        page = type("Page", (), {})()
        page.url = "https://auth.openai.com/about-you"
        page.goto = AsyncMock(return_value=None)
        with (
            patch("register.page.wait_for_any", new=AsyncMock(return_value=False)),
            patch("register.page._about_you_left", new=AsyncMock(side_effect=[False, True])),
            patch("register.page._page_url", new=AsyncMock(return_value=page.url)),
        ):
            followed = asyncio.run(follow_create_account_response(page, response))
        self.assertTrue(followed)
        page.goto.assert_awaited_once_with(
            "https://chatgpt.com/api/auth/callback/openai?code=test",
            timeout=15000,
            wait_until="commit",
        )

    def test_oauth_callback_error_is_not_successful_onboarding_exit(self):
        from register.page import _about_you_left

        page = type("Page", (), {
            "url": "https://chatgpt.com/auth/error?error=OAuthCallback",
        })()
        with patch("register.page.is_chatgpt_main", new=AsyncMock(return_value=False)):
            self.assertFalse(asyncio.run(_about_you_left(page)))

    def test_onboarding_does_not_resubmit_after_create_account_response(self):
        from register.onboarding import handle_onboarding

        page = type("Page", (), {"url": "https://auth.openai.com/about-you"})()
        page.locator = lambda _selector: object()
        with (
            patch("register.onboarding.is_chatgpt_main", new=AsyncMock(return_value=False)),
            patch("register.onboarding._bounded_count", new=AsyncMock(return_value=0)),
            patch("register.onboarding.click_finish_button", new=AsyncMock(return_value="submitted")) as finish,
            patch("register.onboarding.wait_for_any", new=AsyncMock(return_value=False)),
            patch("register.onboarding._about_you_left", new=AsyncMock(return_value=False)),
            patch("register.onboarding.dump_state", new=AsyncMock()),
            patch("register.onboarding.asyncio.sleep", new=AsyncMock()),
        ):
            result = asyncio.run(handle_onboarding(page, 1))
        self.assertFalse(result)
        finish.assert_awaited_once()

    def test_navigation_race_is_classified_without_masking_other_errors(self):
        from register.page import _is_navigation_race, safe_count
        from web_app import _is_expected_browser_navigation_error

        error = RuntimeError(
            "Execution context was destroyed, most likely because of a navigation"
        )
        self.assertTrue(_is_navigation_race(error))
        self.assertTrue(_is_expected_browser_navigation_error({
            "message": "Future exception was never retrieved",
            "exception": error,
        }))
        navigation_timeout = RuntimeError(
            "Timeout 5000ms exceeded - waiting for navigation to finish"
        )
        self.assertTrue(_is_navigation_race(navigation_timeout))
        self.assertTrue(_is_expected_browser_navigation_error({
            "exception": navigation_timeout,
        }))
        self.assertFalse(_is_navigation_race(RuntimeError("proxy timeout")))
        self.assertFalse(_is_expected_browser_navigation_error({
            "exception": RuntimeError("proxy timeout"),
        }))

        page = type("Page", (), {})()
        page.evaluate = AsyncMock(side_effect=error)
        page.locator = lambda _selector: self.fail(
            "navigation race must not start a locator fallback"
        )
        self.assertEqual(asyncio.run(safe_count(page, "input", timeout=0.1)), 0)

    def test_email_selector_matches_new_text_placeholder_variant(self):
        from register.page import EMAIL_INPUT_SELECTOR, safe_count

        self.assertIn('input[placeholder*="email" i]', EMAIL_INPUT_SELECTOR)
        self.assertIn('input[autocomplete="username"]', EMAIL_INPUT_SELECTOR)
        page = type("Page", (), {})()

        async def evaluate(_script, selector):
            return int('input[placeholder*="email" i]' in selector)

        page.evaluate = evaluate
        self.assertEqual(asyncio.run(safe_count(page, EMAIL_INPUT_SELECTOR)), 1)

    def test_safe_count_fallback_is_also_bounded(self):
        from register.page import safe_count

        class Locator:
            async def count(self):
                await asyncio.sleep(1)
                return 1

        page = type("Page", (), {})()
        page.evaluate = AsyncMock(side_effect=RuntimeError("driver busy"))
        page.locator = lambda selector: Locator()
        result = asyncio.run(asyncio.wait_for(
            safe_count(page, "input", timeout=0.05),
            timeout=0.5,
        ))
        self.assertEqual(result, 0)

    def test_wait_for_any_checks_all_selectors_in_one_browser_call(self):
        from register.page import wait_for_any

        page = type("Page", (), {"url": "https://example.com"})()
        with patch("register.page.safe_count", new=AsyncMock(return_value=1)) as count:
            found = asyncio.run(wait_for_any(
                page,
                selectors=['input[name="code"]', 'input[name="age"]'],
                timeout=0.3,
            ))
        self.assertTrue(found)
        count.assert_awaited_once_with(
            page,
            'input[name="code"], input[name="age"]',
            timeout=0.6,
        )

    def test_main_app_dom_wins_over_stale_about_you_url(self):
        from register.page import is_chatgpt_main

        page = type("Page", (), {})()
        page.url = "https://chatgpt.com/auth/onboarding/about-you"
        page.evaluate = AsyncMock(return_value=page.url)
        with (
            patch("register.page.safe_count", new=AsyncMock(return_value=1)),
            patch("register.page.safe_body_text", new=AsyncMock(return_value="")),
        ):
            self.assertTrue(asyncio.run(is_chatgpt_main(page)))

    def test_about_you_is_left_when_main_app_dom_is_present(self):
        from register.page import _about_you_left

        page = type("Page", (), {"url": "https://chatgpt.com/auth/onboarding/about-you"})()
        with patch("register.page.is_chatgpt_main", new=AsyncMock(return_value=True)):
            self.assertTrue(asyncio.run(_about_you_left(page)))

class WebMailboxDeleteLogicTests(unittest.TestCase):
    def test_delete_selected_mailbox_removes_credential_and_state(self):
        import web_app

        cfg = {
            "mail": {
                "providers": [
                    {
                        "type": "icloud_api",
                        "enable": True,
                        "mailboxes": "remove@icloud.com----k1\nkeep@icloud.com----k2\n",
                    }
                ]
            }
        }
        body = web_app.MailboxDeleteRequest(
            items=[{"email": "REMOVE@icloud.com", "provider_type": "icloud_api"}],
            provider_type="auto",
        )
        with patch.object(web_app, "update_mailbox_states", return_value=1) as update_state:
            removed = web_app._delete_mailboxes_from_config(cfg, body)

        self.assertEqual(removed, 1)
        self.assertNotIn("remove@icloud.com", cfg["mail"]["providers"][0]["mailboxes"])
        self.assertIn("keep@icloud.com----k2", cfg["mail"]["providers"][0]["mailboxes"])
        update_state.assert_called_once_with({"remove@icloud.com"}, action="release")

    def test_delete_selected_maddy_mailbox_removes_plain_cached_row(self):
        import web_app

        cfg = {
            "mail": {
                "providers": [
                    {
                        "type": "maddy_webui",
                        "enable": True,
                        "mailboxes": "remove@xb1219.com\nkeep@xb1219.com----42\n",
                    }
                ]
            }
        }
        body = web_app.MailboxDeleteRequest(
            items=[{"email": "REMOVE@xb1219.com", "provider_type": "maddy_webui"}],
            provider_type="auto",
        )
        with patch.object(web_app, "update_mailbox_states", return_value=1) as update_state:
            removed = web_app._delete_mailboxes_from_config(cfg, body)

        self.assertEqual(removed, 1)
        self.assertNotIn("remove@xb1219.com", cfg["mail"]["providers"][0]["mailboxes"])
        self.assertIn("keep@xb1219.com----42", cfg["mail"]["providers"][0]["mailboxes"])
        update_state.assert_called_once_with({"remove@xb1219.com"}, action="release")

    def test_concurrent_config_update_does_not_restore_deleted_mailbox(self):
        import web_app

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.yaml"
            path.write_text(
                "mail:\n"
                "  providers:\n"
                "  - type: icloud_api\n"
                "    enable: true\n"
                "    mailboxes: 'remove@icloud.com----secret-key\\n'\n"
                "proxy:\n"
                "  enabled: false\n",
                encoding="utf-8",
            )
            old_file = web_app.CONFIG_FILE
            old_key = web_app._CONFIG_CACHE_KEY
            old_value = web_app._CONFIG_CACHE_VALUE
            original_write = web_app._write_config
            write_entered = threading.Event()
            release_write = threading.Event()
            first_write = True
            errors: list[BaseException] = []

            def delayed_write(cfg):
                nonlocal first_write
                if first_write:
                    first_write = False
                    write_entered.set()
                    self.assertTrue(release_write.wait(2))
                return original_write(cfg)

            def run_proxy_update():
                try:
                    asyncio.run(web_app.put_proxy(web_app.ProxyUpdate(enabled=True)))
                except BaseException as error:
                    errors.append(error)

            def run_delete():
                try:
                    asyncio.run(
                        web_app.mailboxes_delete(
                            web_app.MailboxDeleteRequest(
                                items=[
                                    {
                                        "email": "remove@icloud.com",
                                        "provider_type": "icloud_api",
                                    }
                                ]
                            )
                        )
                    )
                except BaseException as error:
                    errors.append(error)

            with web_app.JOB.lock:
                old_running = web_app.JOB.running
                web_app.JOB.running = False
            try:
                web_app.CONFIG_FILE = path
                web_app._CONFIG_CACHE_KEY = None
                web_app._CONFIG_CACHE_VALUE = None
                with (
                    patch.object(web_app, "_write_config", side_effect=delayed_write),
                    patch.object(web_app, "update_mailbox_states", return_value=1),
                ):
                    proxy_thread = threading.Thread(target=run_proxy_update)
                    proxy_thread.start()
                    self.assertTrue(write_entered.wait(2))
                    delete_thread = threading.Thread(target=run_delete)
                    delete_thread.start()
                    time.sleep(0.05)
                    release_write.set()
                    proxy_thread.join(3)
                    delete_thread.join(3)

                final = web_app.load_config(path)
                pool = final["mail"]["providers"][0]["mailboxes"]
                self.assertEqual(errors, [])
                self.assertFalse(proxy_thread.is_alive())
                self.assertFalse(delete_thread.is_alive())
                self.assertNotIn("remove@icloud.com", pool)
                self.assertTrue(final["proxy"]["enabled"])
            finally:
                release_write.set()
                web_app.CONFIG_FILE = old_file
                web_app._CONFIG_CACHE_KEY = old_key
                web_app._CONFIG_CACHE_VALUE = old_value
                with web_app.JOB.lock:
                    web_app.JOB.running = old_running

    def test_delete_all_mailboxes_also_clears_state(self):
        import json
        import tempfile
        import web_app

        cfg = {
            "mail": {
                "providers": [
                    {
                        "type": "icloud_api",
                        "enable": True,
                        "mailboxes": "a@icloud.com----k1\nb@icloud.com----k2\n",
                    }
                ]
            }
        }
        with tempfile.TemporaryDirectory() as td:
            import mail

            old_state = web_app.STATE_FILE
            old_mail_state = mail.STATE_FILE
            web_app.STATE_FILE = Path(td) / "state.json"
            mail.STATE_FILE = web_app.STATE_FILE
            web_app.STATE_FILE.write_text(json.dumps({
                "a@icloud.com": {"state": "used"},
                "b@icloud.com": {"state": "failed"},
                "keep@icloud.com": {"state": "used"},
            }), encoding="utf-8")
            try:
                removed = web_app._delete_mailboxes_from_config(
                    cfg,
                    web_app.MailboxDeleteRequest(delete_all=True, provider_type="icloud_api"),
                )
                self.assertEqual(removed, 2)
                self.assertEqual(cfg["mail"]["providers"][0]["mailboxes"], "")
                state = json.loads(web_app.STATE_FILE.read_text(encoding="utf-8"))
                self.assertNotIn("a@icloud.com", state)
                self.assertNotIn("b@icloud.com", state)
                self.assertIn("keep@icloud.com", state)
            finally:
                web_app.STATE_FILE = old_state
                mail.STATE_FILE = old_mail_state


class ProtocolAndHealthTests(unittest.TestCase):
    def test_protocol_wait_reuses_registration_code_when_second_mail_is_absent(self):
        import register.protocol as protocol

        mailbox = {
            "address": "fallback@example.com",
            "_post_registration_otp_fallback": "437183",
        }
        scoped_config = {
            "wait_timeout": 90,
            "providers": [],
        }
        config_token = protocol._MAIL_CONFIG.set(scoped_config)
        try:
            with patch(
                "register.protocol.read_mail_code",
                return_value=None,
            ) as read_code:
                result = protocol.wait_for_code(mailbox)
        finally:
            protocol._MAIL_CONFIG.reset(config_token)

        self.assertEqual(result, "437183")
        self.assertEqual(read_code.call_args.args[0]["wait_timeout"], 60.0)
        self.assertTrue(mailbox["_post_registration_otp_fallback_used"])
        self.assertNotIn("_post_registration_otp_fallback", mailbox)

    def test_protocol_registration_retries_next_code_after_wrong_otp(self):
        from register.protocol import PlatformRegistrar

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.proxy = ""
        registrar.device_id = "device-fixture"
        registrar.fingerprint = {}
        registrar._log_identity = MagicMock()
        registrar._complete_chatgpt_session = MagicMock()
        registrar._validate_otp = MagicMock(
            side_effect=[
                RuntimeError(
                    'validate_otp_http_401_body={"error":{"code":"wrong_email_otp_code"}}'
                ),
                {"continue_url": "https://auth.openai.com/about-you"},
            ]
        )
        registrar._diagnostic = MagicMock()
        registrar._navigate_auth_step = MagicMock()
        registrar._set_registration_password = MagicMock(
            return_value="Strong-password-1"
        )
        registrar._create_account = MagicMock(
            return_value={"continue_url": "https://chatgpt.com/api/auth/callback/openai"}
        )
        registrar._read_chatgpt_session = MagicMock(
            return_value={"access_token": "access-token", "account_id": "account-id"}
        )
        registrar._enroll_totp = MagicMock(
            return_value={"mfa_enabled": True, "totp_secret": "JBSWY3DPEHPK3PXP"}
        )
        mailbox = {
            "address": "retry@example.com",
            "provider": "icloud_api",
            "label": "fixture",
            "_seen_otp_codes": [],
        }
        candidates = iter(["111222", "333444"])

        def read_code(active_mailbox, register_proxy=""):
            code = next(candidates)
            active_mailbox["_seen_otp_codes"].append(code)
            return code

        with (
            patch("register.protocol.create_mailbox", return_value=mailbox),
            patch("register.protocol.wait_for_code", side_effect=read_code) as wait_code,
            patch("register.protocol.mark_mailbox_result") as mailbox_result,
            patch("register.protocol.step") as protocol_step,
        ):
            result = registrar.register(1)

        self.assertEqual(result["email"], "retry@example.com")
        registrar._set_registration_password.assert_called_once_with(
            "retry@example.com",
            unittest.mock.ANY,
            1,
        )
        registrar._create_account.assert_called_once()
        self.assertEqual(wait_code.call_count, 2)
        self.assertEqual(mailbox["_seen_otp_codes"], ["111222", "333444"])
        self.assertTrue(
            any(
                "已排除本次候选" in str(call.args[1])
                for call in protocol_step.call_args_list
                if len(call.args) > 1
            )
        )
        mailbox_result.assert_called_once_with(mailbox, success=True)
        self.assertNotIn("_post_registration_otp_fallback", mailbox)

    def test_post_registration_password_retries_next_code_after_wrong_otp(self):
        from register.protocol import PlatformRegistrar

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.proxy = ""
        registrar.device_id = "device-fixture"
        registrar._start_password_add_authorize = MagicMock(
            return_value="https://auth.openai.com/email-verification"
        )
        registrar._reset_auth_cookies = MagicMock()
        registrar._sync_session_identity = MagicMock()
        registrar._validate_otp = MagicMock(
            side_effect=[
                RuntimeError(
                    'validate_otp_http_401_body={"error":{"code":"wrong_email_otp_code"}}'
                ),
                {
                    "continue_url": (
                        "https://auth.openai.com/reset-password/new-password"
                    )
                },
            ]
        )
        registrar._diagnostic = MagicMock()
        registrar._navigate_auth_step = MagicMock()
        registrar._add_password = MagicMock(
            return_value={"continue_url": "https://chatgpt.com/api/auth/callback/openai"}
        )
        mailbox = {
            "address": "retry@example.com",
            "_seen_otp_codes": ["999000"],
        }
        candidates = iter(["111222", "333444"])

        def read_code(active_mailbox, register_proxy=""):
            code = next(candidates)
            active_mailbox["_seen_otp_codes"].append(code)
            return code

        with (
            patch("register.protocol.wait_for_code", side_effect=read_code) as wait_code,
            patch("register.protocol.step") as protocol_step,
        ):
            result = registrar._complete_post_registration_password(
                "retry@example.com",
                "Strong-password-1",
                mailbox,
                1,
            )

        self.assertEqual(result, "Strong-password-1")
        self.assertEqual(wait_code.call_count, 2)
        self.assertEqual(
            mailbox["_seen_otp_codes"],
            ["111222", "333444"],
        )
        self.assertTrue(
            any(
                "已排除本次候选" in str(call.args[1])
                for call in protocol_step.call_args_list
                if len(call.args) > 1
            )
        )
        registrar._start_password_add_authorize.assert_called_once()
        registrar._add_password.assert_called_once_with(
            "Strong-password-1",
            1,
        )

    def test_post_registration_password_accepts_reused_code_in_new_transaction(self):
        from register.protocol import PlatformRegistrar

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.proxy = ""
        registrar.device_id = "device-fixture"
        registrar._start_password_add_authorize = MagicMock(
            return_value="https://auth.openai.com/email-verification"
        )
        registrar._validate_otp = MagicMock(
            return_value={
                "continue_url": (
                    "https://auth.openai.com/reset-password/new-password"
                )
            }
        )
        registrar._diagnostic = MagicMock()
        registrar._navigate_auth_step = MagicMock()
        registrar._add_password = MagicMock(
            return_value={
                "continue_url": "https://chatgpt.com/api/auth/callback/openai"
            }
        )
        mailbox = {
            "address": "reused-code@example.com",
            "_seen_otp_codes": ["437183"],
        }

        def read_reused_code(active_mailbox, register_proxy=""):
            self.assertEqual(active_mailbox["_seen_otp_codes"], [])
            active_mailbox["_seen_otp_codes"].append("437183")
            return "437183"

        with (
            patch(
                "register.protocol.wait_for_code",
                side_effect=read_reused_code,
            ),
            patch("register.protocol.step"),
        ):
            result = registrar._complete_post_registration_password(
                "reused-code@example.com",
                "Strong-password-1",
                mailbox,
                1,
            )

        self.assertEqual(result, "Strong-password-1")
        self.assertEqual(mailbox["_seen_otp_codes"], ["437183"])
        registrar._validate_otp.assert_called_once_with(
            "437183",
            1,
            allow_sentinel_retry=True,
        )

    def test_post_registration_password_rebuilds_transaction_after_add_invalid_state(self):
        from register.protocol import PlatformRegistrar

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.proxy = ""
        registrar.device_id = "device-fixture"
        registrar._start_password_add_authorize = MagicMock(
            side_effect=[
                "https://auth.openai.com/email-verification",
                "https://auth.openai.com/email-verification",
            ]
        )
        registrar._reset_auth_cookies = MagicMock()
        registrar._sync_session_identity = MagicMock()
        registrar._validate_otp = MagicMock(
            return_value={
                "continue_url": "https://auth.openai.com/reset-password/new-password"
            }
        )
        registrar._diagnostic = MagicMock()
        registrar._navigate_auth_step = MagicMock()
        registrar._add_password = MagicMock(
            side_effect=[
                RuntimeError(
                    'password_add_http_409, detail={"error":{"code":"invalid_state"}}'
                ),
                {
                    "continue_url": (
                        "https://chatgpt.com/api/auth/callback/openai"
                    )
                },
            ]
        )

        with (
            patch(
                "register.protocol.wait_for_code",
                side_effect=["111222", "333444"],
            ),
            patch("register.protocol.step") as protocol_step,
        ):
            result = registrar._complete_post_registration_password(
                "invalid-state@example.com",
                "Strong-password-1",
                {"address": "invalid-state@example.com"},
                1,
            )

        self.assertEqual(result, "Strong-password-1")
        self.assertEqual(registrar._start_password_add_authorize.call_count, 2)
        self.assertEqual(registrar._add_password.call_count, 2)
        registrar._reset_auth_cookies.assert_called_once()
        registrar._sync_session_identity.assert_called_once()
        self.assertTrue(
            any(
                "重建整段认证事务" in str(call.args[1])
                for call in protocol_step.call_args_list
                if len(call.args) > 1
            )
        )

    def test_post_registration_password_retries_auth_rate_limit_before_waiting_otp(self):
        from register.protocol import PlatformRegistrar

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.proxy = ""
        registrar.device_id = "device-fixture"
        registrar._start_password_add_authorize = MagicMock(
            side_effect=[
                RuntimeError(
                    "auth_step_error_page: code=rate_limit_exceeded"
                ),
                RuntimeError(
                    "auth_step_error_page: code=rate_limit_exceeded"
                ),
                "https://auth.openai.com/email-verification",
            ]
        )
        registrar._reset_auth_cookies = MagicMock()
        registrar._sync_session_identity = MagicMock()
        registrar._validate_otp = MagicMock(
            return_value={
                "continue_url": "https://auth.openai.com/reset-password/new-password"
            }
        )
        registrar._diagnostic = MagicMock()
        registrar._navigate_auth_step = MagicMock()
        registrar._add_password = MagicMock(
            return_value={
                "continue_url": "https://chatgpt.com/api/auth/callback/openai"
            }
        )

        with (
            patch("register.protocol.wait_for_code", return_value="123456"),
            patch("register.protocol.random.uniform", return_value=2.0),
            patch("register.protocol.time.sleep") as sleep,
            patch("register.protocol.step") as protocol_step,
        ):
            result = registrar._complete_post_registration_password(
                "rate-limited@example.com",
                "Strong-password-1",
                {"address": "rate-limited@example.com"},
                1,
            )

        self.assertEqual(result, "Strong-password-1")
        self.assertEqual(registrar._start_password_add_authorize.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue(
            any(
                "频率限制" in str(call.args[1])
                for call in protocol_step.call_args_list
                if len(call.args) > 1
            )
        )

    def test_post_registration_password_skips_when_already_set(self):
        from register.protocol import PlatformRegistrar

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.proxy = ""
        registrar.device_id = "device-fixture"
        registrar._start_password_add_authorize = MagicMock(
            side_effect=[
                "https://auth.openai.com/email-verification",
                "https://auth.openai.com/log-in/password",
            ]
        )
        registrar._reset_auth_cookies = MagicMock()
        registrar._sync_session_identity = MagicMock()
        registrar._send_password_reset_otp = MagicMock(return_value={})
        registrar._validate_otp = MagicMock(
            return_value={
                "continue_url": "https://auth.openai.com/reset-password/new-password"
            }
        )
        registrar._diagnostic = MagicMock()
        registrar._navigate_auth_step = MagicMock()
        registrar.login_with_password_totp = MagicMock()
        registrar._add_password = MagicMock(
            side_effect=[
                RuntimeError(
                    'password_add_http_400, detail={"error":{"code":"password_already_set"}}'
                ),
                {
                    "continue_url": "https://auth.openai.com/reset-password/success",
                    "page": {"type": "reset_password_success"},
                },
            ]
        )

        with (
            patch(
                "register.protocol.wait_for_code",
                side_effect=["111222", "333444"],
            ),
            patch("register.protocol.step"),
        ):
            with self.assertRaisesRegex(RuntimeError, "password_reset_required"):
                registrar._complete_post_registration_password(
                    "already-set@example.com",
                    "Strong-password-1",
                    {"address": "already-set@example.com"},
                    1,
                    skip_existing_password=True,
                )

        registrar._send_password_reset_otp.assert_not_called()
        registrar._add_password.assert_called_once_with("Strong-password-1", 1)
        registrar.login_with_password_totp.assert_not_called()

    def test_otp_timeout_message_includes_provider_poll_and_last_error(self):
        from register.protocol import _otp_wait_timeout_message

        message = _otp_wait_timeout_message(
            {
                "provider": "icloud_api",
                "_code_wait_attempts": 7,
                "_last_code_wait_error": "iCloud API HTTP 502",
            },
            "注册",
        )

        self.assertIn("等待注册验证码超时", message)
        self.assertIn("provider=icloud_api", message)
        self.assertIn("polls=7", message)
        self.assertIn("iCloud API HTTP 502", message)

    def test_account_access_token_endpoint_returns_requested_at_without_cache(self):
        import web_app

        account = {
            "email": "copy-at@example.com",
            "account_id": "copy-at-id",
            "access_token": "Bearer copied-access-token",
        }
        key = web_app._account_key(account)
        with patch.object(
            web_app,
            "load_account_records_by_keys",
            return_value={key: account},
        ):
            response = asyncio.run(
                web_app.account_access_token(web_app.AccountTokenRequest(key=key))
            )

        payload = json.loads(response.body)
        self.assertEqual(payload["access_token"], "copied-access-token")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["pragma"], "no-cache")

    def test_selected_access_token_copy_and_export_preserve_order(self):
        import web_app

        first = {
            "email": "first@example.com",
            "account_id": "first-id",
            "access_token": "Bearer token-first",
        }
        second = {
            "email": "second@example.com",
            "account_id": "second-id",
            "access_token": "token-second",
        }
        missing = {
            "email": "missing@example.com",
            "account_id": "missing-id",
            "access_token": "",
        }
        keys = [
            web_app._account_key(second),
            web_app._account_key(missing),
            "email:unknown@example.com",
            web_app._account_key(first),
            web_app._account_key(second),
        ]

        async def read_body(response):
            return b"".join([chunk async for chunk in response.body_iterator])

        records_by_key = {
            web_app._account_key(first): first,
            web_app._account_key(second): second,
            web_app._account_key(missing): missing,
        }
        with patch.object(
            web_app,
            "load_account_records_by_keys",
            return_value=records_by_key,
        ):
            copied = asyncio.run(
                web_app.accounts_access_tokens(
                    web_app.BulkDeleteRequest(keys=keys, delete_all=False)
                )
            )
            exported = asyncio.run(
                web_app.accounts_export_access_tokens(
                    web_app.BulkDeleteRequest(keys=keys, delete_all=False)
                )
            )
            payload = asyncio.run(read_body(exported)).decode("utf-8")

        copied_payload = json.loads(copied.body)
        self.assertEqual(copied_payload["tokens"], ["token-second", "token-first"])
        self.assertEqual(copied_payload["exported"], 2)
        self.assertEqual(copied_payload["missing"], 2)
        self.assertEqual(copied.headers["cache-control"], "no-store")
        self.assertEqual(payload, "token-second\ntoken-first\n")
        self.assertEqual(exported.headers["x-exported-count"], "2")
        self.assertEqual(exported.headers["x-missing-count"], "2")
        self.assertEqual(exported.headers["cache-control"], "no-store")

    def test_account_credentials_endpoint_returns_requested_secret_or_current_totp(self):
        import web_app

        account = {
            "email": "copy-credentials@example.com",
            "account_id": "copy-credentials-id",
            "password": "Secret123!",
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "mfa_enabled": True,
        }
        key = web_app._account_key(account)
        with patch.object(web_app, "_load_accounts", return_value=[account]):
            password = asyncio.run(
                web_app.account_credentials(
                    web_app.AccountCredentialsRequest(key=key, field="password")
                )
            )
            secret = asyncio.run(
                web_app.account_credentials(
                    web_app.AccountCredentialsRequest(key=key, field="totp_secret")
                )
            )
            with patch("web_app.time.time", return_value=59.0):
                code = asyncio.run(
                    web_app.account_credentials(
                        web_app.AccountCredentialsRequest(key=key, field="totp_code")
                    )
                )

        self.assertEqual(json.loads(password.body)["value"], "Secret123!")
        self.assertEqual(json.loads(secret.body)["value"], "JBSWY3DPEHPK3PXP")
        code_payload = json.loads(code.body)
        self.assertEqual(code_payload["value"], "996554")
        self.assertEqual(code_payload["expires_at"], 60)
        self.assertEqual(code.headers["cache-control"], "no-store")

    def test_account_credentials_endpoint_rejects_unknown_field(self):
        import web_app

        with self.assertRaises(HTTPException) as context:
            asyncio.run(
                web_app.account_credentials(
                    web_app.AccountCredentialsRequest(key="email:x", field="access_token")
                )
            )
        self.assertEqual(context.exception.status_code, 400)

    def test_selected_security_export_returns_email_password_and_totp(self):
        import web_app

        complete = {
            "email": "complete@example.com",
            "password": "Secret123!",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }
        missing = {
            "email": "missing@example.com",
            "password": "",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }
        keys = [web_app._account_key(complete), web_app._account_key(missing)]

        async def read_body(response):
            return b"".join([chunk async for chunk in response.body_iterator])

        with patch.object(web_app, "_load_accounts", return_value=[complete, missing]):
            response = asyncio.run(
                web_app.accounts_export_security(
                    web_app.BulkDeleteRequest(keys=keys, delete_all=False)
                )
            )
            payload = asyncio.run(read_body(response)).decode("utf-8")

        self.assertEqual(payload, "complete@example.com----Secret123!----JBSWY3DPEHPK3PXP\n")
        self.assertEqual(response.headers["x-exported-count"], "1")
        self.assertEqual(response.headers["x-missing-count"], "1")

    def test_email_security_export_matches_requested_addresses_in_order(self):
        import web_app

        complete = {
            "email": "first@example.com",
            "password": "Secret123!",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }
        second = {
            "email": "second@example.com",
            "password": "Secret456!",
            "totp_secret": "KRSXG5A7N5ZGK3XQ",
        }
        missing = {"email": "missing@example.com", "password": "only-password"}

        async def read_body(response):
            return b"".join([chunk async for chunk in response.body_iterator])

        with patch.object(
            web_app,
            "_load_accounts",
            return_value=[complete, second, missing],
        ):
            response = asyncio.run(
                web_app.accounts_export_security_by_email(
                    web_app.AccountEmailExportRequest(
                        text="second@example.com, unknown@example.com\nfirst@example.com\nsecond@example.com"
                    )
                )
            )
            payload = asyncio.run(read_body(response)).decode("utf-8")

        self.assertEqual(
            payload,
            "second@example.com----Secret456!----KRSXG5A7N5ZGK3XQ\n"
            "first@example.com----Secret123!----JBSWY3DPEHPK3PXP\n",
        )
        self.assertEqual(response.headers["x-requested-count"], "3")
        self.assertEqual(response.headers["x-matched-count"], "2")
        self.assertEqual(response.headers["x-exported-count"], "2")
        self.assertEqual(response.headers["x-missing-count"], "1")

    def test_email_security_export_rejects_empty_filter(self):
        import web_app

        with self.assertRaises(HTTPException) as context:
            asyncio.run(
                web_app.accounts_export_security_by_email(
                    web_app.AccountEmailExportRequest(text="not-an-email")
                )
            )

        self.assertEqual(context.exception.status_code, 400)

    def test_register_log_sink_receives_rendered_lines(self):
        from register.log import _LOG_SINK, _TASK_EMAIL, _TASK_IDX, _log

        captured: list[str] = []
        sink_token = _LOG_SINK.set(captured.append)
        index_token = _TASK_IDX.set(3)
        email_token = _TASK_EMAIL.set("renew@example.com")
        try:
            _log("[2] 浏览器登录步骤")
        finally:
            _TASK_EMAIL.reset(email_token)
            _TASK_IDX.reset(index_token)
            _LOG_SINK.reset(sink_token)

        self.assertEqual(captured, ["#3 renew@example.com [2] 浏览器登录步骤"])

    def test_account_reauthentication_uses_browser_and_preserves_account(self):
        import web_app

        account = {
            "email": "expired@example.com",
            "account_id": "account-id",
            "group_id": "sale",
            "registered_at": "2026-07-01T00:00:00Z",
            "access_token": "expired-access",
            "health_status": "alive",
            "provider_type": "icloud_api",
        }
        browser_tokens = {
            "access_token": "fresh-access",
            "refresh_token": "fresh-refresh",
            "id_token": "fresh-id",
        }
        checked = {
            **account,
            "access_token": "fresh-access",
            "refresh_token": "fresh-refresh",
            "id_token": "fresh-id",
            "health_status": "alive",
            "health_error": "",
        }
        with (
            patch.object(web_app, "_read_config", return_value={"mail": {}, "proxy": {}, "registration": {}}),
            patch("register.flow.register_one", new=AsyncMock(return_value=browser_tokens)) as browser_login,
            patch("mail.update_mailbox_states") as mailbox_state,
            patch("common.account_health.check_account", return_value=checked),
            patch("common.account_store.upsert_account_record", return_value=checked) as persist,
        ):
            result = web_app._reauthenticate_account_sync(account, mode="browser")

        self.assertEqual(result["refresh_token"], "fresh-refresh")
        self.assertEqual(result["group_id"], "sale")
        browser_login.assert_awaited_once()
        self.assertEqual(browser_login.await_args.args[:3], (1, 1, None))
        self.assertEqual(browser_login.await_args.kwargs["fixed_email"], account["email"])
        self.assertEqual(browser_login.await_args.kwargs["provider_type"], "icloud_api")
        self.assertTrue(browser_login.await_args.kwargs["hybrid_mode"])
        self.assertFalse(browser_login.await_args.kwargs["persist_result"])
        mailbox_state.assert_called_once_with([account["email"]], action="release")
        persist.assert_called_once()
        logs = "\n".join(web_app.JOB.recent_logs(20))
        self.assertIn("[重新登录] 开始 · expired@example.com", logs)
        self.assertIn("[重新登录] 成功 · expired@example.com", logs)

    def test_security_repair_rotates_proxy_after_transport_error(self):
        import web_app

        account = {
            "email": "repair@example.com",
            "proxy": "http://first.example:1001",
            "provider_type": "icloud_api",
        }
        repaired = {
            "password": "Confirmed123!",
            "password_set": True,
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "mfa_enabled": True,
        }

        def save(record, _path):
            return dict(record)

        with (
            patch.object(
                web_app,
                "_read_config",
                return_value={
                    "mail": {},
                    "registration": {},
                    "proxy": {
                        "enabled": True,
                        "mode": "single_proxy",
                        "url": "http://second.example:1002",
                    },
                },
            ),
            patch("mail.update_mailbox_states") as mailbox_state,
            patch(
                "register.protocol.repair_security_one",
                side_effect=[
                    RuntimeError("curl: (35) connection closed abruptly"),
                    repaired,
                ],
            ) as repair,
            patch.object(web_app, "upsert_account_record", side_effect=save),
            patch.object(web_app.time, "sleep"),
        ):
            result = web_app._repair_account_security_sync(account)

        self.assertEqual(repair.call_count, 2)
        self.assertEqual(
            repair.call_args_list[0].kwargs["proxy"],
            "http://first.example:1001",
        )
        self.assertEqual(
            repair.call_args_list[1].kwargs["proxy"],
            "http://second.example:1002",
        )
        self.assertEqual(result["proxy"], "http://second.example:1002")
        self.assertEqual(result["registration_state"], "complete")
        self.assertGreaterEqual(mailbox_state.call_count, 2)

    def test_security_repair_does_not_rotate_on_business_error(self):
        import web_app

        account = {
            "email": "repair@example.com",
            "proxy": "http://first.example:1001",
            "provider_type": "icloud_api",
        }
        with (
            patch.object(
                web_app,
                "_read_config",
                return_value={
                    "mail": {},
                    "registration": {},
                    "proxy": {
                        "enabled": True,
                        "mode": "single_proxy",
                        "url": "http://second.example:1002",
                    },
                },
            ),
            patch("mail.update_mailbox_states"),
            patch(
                "register.protocol.repair_security_one",
                side_effect=RuntimeError("wrong_email_otp_code"),
            ) as repair,
        ):
            with self.assertRaisesRegex(RuntimeError, "wrong_email_otp_code"):
                web_app._repair_account_security_sync(account)

        repair.assert_called_once()

    def test_security_repair_preflight_does_not_require_access_token(self):
        import web_app

        # A browser account can have no AT while still being recoverable via
        # the mailbox-backed protocol repair flow.  Preflight should only
        # validate the missing security fields, not reject this row early.
        account = {
            "email": "no-at-pending@example.com",
            "password": "candidate-password",
            "password_set": False,
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "mfa_enabled": True,
            "registration_method": "browser",
            "access_token": "",
        }
        key = web_app._account_key(account)
        with (
            patch.object(web_app, "_load_accounts", return_value=[account]),
            patch.object(
                web_app.ACCOUNT_REAUTH_JOBS,
                "start",
                return_value={"job_id": "repair-no-at", "total": 1},
            ) as start,
        ):
            result = asyncio.run(
                web_app.account_security_repair_start(
                    web_app.AccountReauthBatchRequest(
                        keys=[key],
                        mode="protocol",
                        concurrency=1,
                    )
                )
            )

        self.assertTrue(result["ok"])
        start.assert_called_once_with([account], "protocol", "security_repair", 1)

    def test_single_security_repair_redacts_credentials_from_error(self):
        import web_app

        account = {
            "email": "repair-error@example.com",
            "password": "old-secret-password",
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "mfa_enabled": True,
        }
        with (
            patch("web.routes.accounts_reauth._load_security_accounts", new=AsyncMock(return_value=[account])),
            patch.object(
                web_app,
                "_repair_account_security_sync",
                side_effect=RuntimeError(
                    "provider echoed old-secret-password and JBSWY3DPEHPK3PXP"
                ),
            ),
        ):
            with self.assertRaises(HTTPException) as context:
                asyncio.run(
                    web_app.account_security_repair(
                        web_app.AccountTokenRequest(key="email:repair-error@example.com")
                    )
                )

        self.assertEqual(context.exception.status_code, 409)
        detail = str(context.exception.detail)
        self.assertNotIn("old-secret-password", detail)
        self.assertNotIn("JBSWY3DPEHPK3PXP", detail)
        self.assertIn("[已隐藏]", detail)

    def test_account_reauthentication_reports_browser_failure(self):
        import web_app

        account = {"email": "expired@example.com", "access_token": "expired"}
        with (
            patch.object(web_app, "_read_config", return_value={"mail": {}, "proxy": {}, "registration": {}}),
            patch("register.flow.register_one", new=AsyncMock(return_value=None)),
        ):
            with self.assertRaisesRegex(RuntimeError, "混合模式重新登录失败"):
                web_app._reauthenticate_account_sync(account, mode="browser")

        logs = "\n".join(web_app.JOB.recent_logs(20))
        self.assertIn("[重新登录] 开始 · expired@example.com", logs)
        self.assertIn("[重新登录] 失败 · expired@example.com", logs)

    def test_account_reauthentication_uses_protocol_mode(self):
        import web_app

        account = {
            "email": "protocol@example.com",
            "account_id": "protocol-id",
            "access_token": "expired-access",
            "health_status": "reauth_required",
            "provider_type": "icloud_api",
            "password": "Strong-password-1",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }
        protocol_tokens = {
            "access_token": "protocol-access",
            "refresh_token": "protocol-refresh",
            "id_token": "protocol-id-token",
        }
        checked = {
            **account,
            **protocol_tokens,
            "health_status": "alive",
            "health_error": "",
        }
        with (
            patch.object(
                web_app,
                "_read_config",
                return_value={"mail": {}, "proxy": {}, "registration": {}},
            ),
            patch(
                "register.protocol.login_with_password_totp_one",
                return_value=protocol_tokens,
            ) as protocol_login,
            patch("register.flow.register_one", new=AsyncMock()) as browser_login,
            patch("mail.update_mailbox_states"),
            patch("common.account_health.check_account", return_value=checked),
            patch("common.account_store.upsert_account_record", return_value=checked),
        ):
            result = web_app._reauthenticate_account_sync(account, mode="protocol")

        self.assertEqual(result["access_token"], "protocol-access")
        protocol_login.assert_called_once()
        self.assertEqual(protocol_login.call_args.kwargs["email"], account["email"])
        self.assertEqual(protocol_login.call_args.kwargs["password"], account["password"])
        self.assertEqual(protocol_login.call_args.kwargs["totp_secret"], account["totp_secret"])
        self.assertFalse(browser_login.await_count)

    def test_invalid_account_forces_protocol_mode_even_when_browser_requested(self):
        import web_app

        account = {
            "email": "invalid-browser-request@example.com",
            "account_id": "invalid-account-id",
            "access_token": "expired-access",
            "health_status": "invalid",
            "password": "Strong-password-1",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }
        protocol_tokens = {"access_token": "fresh-protocol-access"}
        checked = {**account, **protocol_tokens, "health_status": "alive", "health_error": ""}
        with (
            patch.object(
                web_app,
                "_read_config",
                return_value={"mail": {}, "proxy": {}, "registration": {}},
            ),
            patch(
                "register.protocol.login_with_password_totp_one",
                return_value=protocol_tokens,
            ) as protocol_login,
            patch("register.flow.register_one", new=AsyncMock()) as browser_login,
            patch("common.account_health.check_account", return_value=checked),
            patch("common.account_store.upsert_account_record", return_value=checked),
        ):
            result = web_app._reauthenticate_account_sync(account, mode="browser")

        self.assertEqual(result["access_token"], "fresh-protocol-access")
        protocol_login.assert_called_once()
        self.assertFalse(browser_login.await_count)

    def test_account_reauthentication_protocol_requires_password_and_totp(self):
        import web_app

        account = {
            "email": "missing-security@example.com",
            "access_token": "expired-access",
            "health_status": "reauth_required",
        }
        with patch.object(
            web_app,
            "_read_config",
            return_value={"mail": {}, "proxy": {}, "registration": {}},
        ):
            with self.assertRaisesRegex(RuntimeError, "同时具备已保存密码和 TOTP 2FA"):
                web_app._reauthenticate_account_sync(account, mode="protocol")

    def test_reauth_protocol_endpoints_reject_missing_credentials_before_queue(self):
        import web_app

        account = {
            "email": "missing-security@example.com",
            "access_token": "expired-access",
            "health_status": "reauth_required",
            "password": "",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }
        key = web_app._account_key(account)
        with (
            patch.object(web_app, "_load_accounts", return_value=[account]),
            patch.object(web_app.ACCOUNT_REAUTH_JOBS, "start") as start,
        ):
            with self.assertRaises(HTTPException) as batch_error:
                asyncio.run(
                    web_app.account_reauthenticate_start(
                        web_app.AccountReauthBatchRequest(keys=[key], mode="protocol")
                    )
                )
            with self.assertRaises(HTTPException) as single_error:
                asyncio.run(
                    web_app.account_reauthenticate(
                        web_app.AccountReauthRequest(key=key, mode="protocol")
                    )
                )

        self.assertEqual(batch_error.exception.status_code, 409)
        self.assertIn("密码和 TOTP 2FA", str(batch_error.exception.detail))
        self.assertEqual(single_error.exception.status_code, 409)
        start.assert_not_called()

    def test_reauth_endpoint_passes_custom_concurrency_to_batch(self):
        import web_app

        account = {
            "email": "ready@example.com",
            "health_status": "reauth_required",
            "password": "saved-password",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }
        key = web_app._account_key(account)
        with (
            patch.object(web_app, "_load_accounts", return_value=[account]),
            patch.object(
                web_app.ACCOUNT_REAUTH_JOBS,
                "start",
                return_value={"job_id": "job-1", "concurrency": 37},
            ) as start,
        ):
            result = asyncio.run(
                web_app.account_reauthenticate_start(
                    web_app.AccountReauthBatchRequest(
                        keys=[key],
                        mode="protocol",
                        concurrency=37,
                    )
                )
            )

        self.assertTrue(result["ok"])
        start.assert_called_once_with([account], "protocol", "reauth", 37)

    def test_reauth_by_email_matches_deduplicates_and_reports_skipped_accounts(self):
        import web_app

        ready = {
            "email": "ready@example.com",
            "health_status": "alive",
            "password": "saved-password",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }
        missing = {
            "email": "missing@example.com",
            "health_status": "invalid",
            "password": "",
            "totp_secret": "",
        }
        with (
            patch.object(web_app, "_load_accounts", return_value=[ready, missing]),
            patch.object(
                web_app.ACCOUNT_REAUTH_JOBS,
                "start",
                return_value={"job_id": "email-job", "total": 1},
            ) as start,
        ):
            result = asyncio.run(
                web_app.account_reauthenticate_by_email_start(
                    web_app.AccountEmailReauthRequest(
                        emails=[
                            "READY@example.com",
                            "ready@example.com",
                            "missing@example.com",
                            "unknown@example.com",
                        ],
                        concurrency=7,
                    )
                )
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["requested"], 3)
        self.assertEqual(result["matched"], 2)
        self.assertEqual(result["submitted"], 1)
        self.assertEqual(result["not_found"], ["unknown@example.com"])
        self.assertEqual(
            result["missing_credentials"],
            [{"email": "missing@example.com", "missing": ["password", "totp"]}],
        )
        start.assert_called_once_with([ready], "protocol", "reauth", 7)

    def test_reauth_by_email_reports_batch_without_ready_credentials(self):
        import web_app

        account = {
            "email": "missing@example.com",
            "password": "saved-password",
            "totp_secret": "",
        }
        with (
            patch.object(web_app, "_load_accounts", return_value=[account]),
            patch.object(web_app.ACCOUNT_REAUTH_JOBS, "start") as start,
        ):
            result = asyncio.run(
                web_app.account_reauthenticate_by_email_start(
                    web_app.AccountEmailReauthRequest(
                        text="missing@example.com unknown@example.com"
                    )
                )
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "preflight")
        self.assertEqual(result["submitted"], 0)
        self.assertEqual(result["not_found"], ["unknown@example.com"])
        self.assertEqual(
            result["missing_credentials"],
            [{"email": "missing@example.com", "missing": ["totp"]}],
        )
        start.assert_not_called()

    def test_account_reauth_batch_can_stop_remaining_accounts(self):
        import web_app

        jobs = web_app.AccountReauthBatchJobs()
        entered = threading.Event()
        release = threading.Event()

        def reauthenticate(_account, **kwargs):
            entered.set()
            release.wait(timeout=3)
            if kwargs.get("stop_check", lambda: False)():
                raise RuntimeError("stop requested")
            return _account

        records = [
            {"email": "stop-first@example.com", "account_id": "stop-first"},
            {"email": "stop-second@example.com", "account_id": "stop-second"},
        ]
        with patch.object(web_app, "_reauthenticate_account_sync", side_effect=reauthenticate):
            started = jobs.start(records, mode="protocol")
            self.assertTrue(entered.wait(timeout=1))
            stopping = jobs.stop(started["job_id"])
            self.assertEqual(stopping["status"], "stopping")
            release.set()
            deadline = time.time() + 3
            result = jobs.snapshot(started["job_id"])
            while result["status"] in {"queued", "running", "stopping"} and time.time() < deadline:
                time.sleep(0.01)
                result = jobs.snapshot(started["job_id"])

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["mode"], "protocol")
        self.assertEqual(result["success"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["cancelled"], 2)
        self.assertEqual([item["status"] for item in result["items"]], ["cancelled", "cancelled"])

    def test_account_reauthentication_retries_transient_browser_state_once(self):
        import web_app

        account = {
            "email": "retry@example.com",
            "access_token": "expired",
            "group_id": "sale",
        }
        fresh = {**account, "access_token": "fresh", "health_status": "alive"}
        calls = 0

        async def browser_login(*_args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                kwargs["task_event"](1, "failed", error="no_email_input")
                return None
            return {"access_token": "fresh"}

        with (
            patch.object(
                web_app,
                "_read_config",
                return_value={
                    "mail": {},
                    "proxy": {},
                    "registration": {"reauth_retries": 1},
                },
            ),
            patch("register.flow.register_one", new=browser_login),
            patch("mail.update_mailbox_states") as mailbox_state,
            patch.object(web_app, "_persist_reauthenticated_account", return_value=fresh),
            patch("web_app.time.sleep"),
        ):
            result = web_app._reauthenticate_account_sync(account, mode="browser")

        self.assertEqual(result["access_token"], "fresh")
        self.assertEqual(calls, 2)
        self.assertEqual(mailbox_state.call_count, 2)
        logs = "\n".join(web_app.JOB.recent_logs(30))
        self.assertIn("临时页面错误", logs)

    def test_account_reauthentication_persists_deactivated_terminal_state(self):
        import web_app

        account = {
            "email": "deactivated@example.com",
            "access_token": "expired",
            "health_status": "invalid",
            "password": "Strong-password-1",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }

        def deactivated(*_args, **kwargs):
            kwargs["task_event"](
                1,
                "failed",
                error="account_deactivated",
                step="账号已删除或停用",
            )
            return None

        persisted = {**account, "reauth_blocked_reason": "account_deactivated"}
        with (
            patch.object(
                web_app,
                "_read_config",
                return_value={"mail": {}, "proxy": {}, "registration": {}},
            ),
            patch(
                "register.protocol.login_with_password_totp_one",
                side_effect=deactivated,
            ) as protocol_login,
            patch("web_app.upsert_account_record", return_value=persisted) as save,
        ):
            with self.assertRaisesRegex(RuntimeError, "account_deactivated"):
                web_app._reauthenticate_account_sync(account)

        protocol_login.assert_called_once()

        saved = save.call_args.args[0]
        self.assertEqual(saved["status"], "已封号")
        self.assertEqual(saved["health_status"], "invalid")
        self.assertEqual(saved["reauth_blocked_reason"], "account_deactivated")
        self.assertIn("account_deactivated", saved["health_error"])

    def test_protocol_reauthentication_failure_replaces_stale_alive_state(self):
        import web_app

        account = {
            "email": "stale-alive@example.com",
            "access_token": "expired",
            "health_status": "alive",
            "password": "saved-password",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }

        def invalid_auth_step(*_args, **kwargs):
            kwargs["task_event"](
                1,
                "failed",
                error="password_verify_http_400: invalid_auth_step",
                step="验证密码",
            )
            return None

        with (
            patch.object(
                web_app,
                "_read_config",
                return_value={
                    "mail": {},
                    "proxy": {},
                    "registration": {"reauth_retries": 0},
                },
            ),
            patch(
                "register.protocol.login_with_password_totp_one",
                side_effect=invalid_auth_step,
            ),
            patch(
                "web_app.upsert_account_record",
                side_effect=lambda record, _path: dict(record),
            ) as save,
        ):
            with self.assertRaisesRegex(RuntimeError, "invalid_auth_step"):
                web_app._reauthenticate_account_sync(account, mode="protocol")

        saved = save.call_args.args[0]
        self.assertEqual(saved["health_status"], "reauth_required")
        self.assertEqual(saved["status"], "异常")
        self.assertIn("invalid_auth_step", saved["health_error"])
        self.assertEqual(saved["last_reauth_step"], "验证密码")
        self.assertTrue(saved["last_reauth_failed_at"])

    def test_reauthentication_transport_failure_is_persisted_as_error(self):
        import web_app

        account = {
            "email": "transport@example.com",
            "access_token": "expired",
            "health_status": "alive",
        }
        detail = "Failed to perform, curl: (35) TLS connect error"
        with patch(
            "web_app.upsert_account_record",
            side_effect=lambda record, _path: dict(record),
        ):
            saved = web_app._persist_reauthentication_failure(
                account,
                detail,
                mode="protocol",
                step="验证密码",
            )

        self.assertEqual(saved["health_status"], "error")
        self.assertEqual(saved["health_error"], detail)
        self.assertEqual(saved["last_reauth_error"], detail)

    def test_successful_reauthentication_clears_previous_failure_fields(self):
        import web_app

        account = {
            "email": "recovered@example.com",
            "access_token": "expired",
            "health_status": "reauth_required",
            "health_error": "password_verify_http_400: invalid_auth_step",
            "last_reauth_error": "password_verify_http_400: invalid_auth_step",
            "last_reauth_failed_at": "2026-08-09T00:00:00Z",
        }

        def healthy(record, **_kwargs):
            self.assertEqual(record["last_reauth_error"], "")
            self.assertEqual(record["last_reauth_failed_at"], "")
            self.assertEqual(record["reauth_blocked_reason"], "")
            return {**record, "health_status": "alive", "health_error": ""}

        with (
            patch("common.account_health.check_account", side_effect=healthy),
            patch(
                "common.account_store.upsert_account_record",
                side_effect=lambda record, _path: dict(record),
            ),
        ):
            saved = web_app._persist_reauthenticated_account(
                account,
                {"access_token": "fresh", "refresh_token": "fresh-refresh"},
            )

        self.assertEqual(saved["health_status"], "alive")
        self.assertEqual(saved["last_reauth_error"], "")
        self.assertEqual(saved["last_reauth_failed_at"], "")

    def test_account_reauth_batch_defaults_to_three_concurrent_workers(self):
        import web_app

        jobs = web_app.AccountReauthBatchJobs()
        records = [
            {
                "email": f"account-{index}@example.com",
                "account_id": f"account-{index}",
                "access_token": f"secret-{index}",
            }
            for index in range(5)
        ]
        state_lock = threading.Lock()
        first_wave_ready = threading.Event()
        release = threading.Event()
        active = 0
        max_active = 0

        def reauthenticate(account, **_kwargs):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
                if active == 3:
                    first_wave_ready.set()
            try:
                release.wait(timeout=3)
                if account["email"] == "account-1@example.com":
                    raise RuntimeError("account failed")
                return {**account, "health_status": "alive"}
            finally:
                with state_lock:
                    active -= 1

        with patch.object(web_app, "_reauthenticate_account_sync", side_effect=reauthenticate):
            started = jobs.start(records)
            self.assertTrue(first_wave_ready.wait(timeout=1))
            release.set()
            deadline = time.time() + 3
            result = started
            while result["status"] in {"queued", "running"} and time.time() < deadline:
                time.sleep(0.01)
                result = jobs.snapshot(started["job_id"])

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["concurrency"], 3)
        self.assertEqual(max_active, 3)
        self.assertEqual(result["done"], 5)
        self.assertEqual(result["success"], 4)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(
            [item["status"] for item in result["items"]],
            ["success", "failed", "success", "success", "success"],
        )
        for index in range(5):
            self.assertNotIn(f"secret-{index}", json.dumps(result))

    def test_account_reauth_batch_bounds_in_flight_futures(self):
        import web_app

        jobs = web_app.AccountReauthBatchJobs()
        records = [
            {"email": f"bounded-{index}@example.com", "account_id": f"bounded-{index}"}
            for index in range(25)
        ]
        release = threading.Event()
        window_ready = threading.Event()
        tracker_lock = threading.Lock()
        outstanding = 0
        peak = 0
        submitted = 0

        real_executor = web_app.ThreadPoolExecutor

        class TrackingExecutor(real_executor):
            def submit(self, *args, **kwargs):
                nonlocal outstanding, peak, submitted
                with tracker_lock:
                    outstanding += 1
                    submitted += 1
                    peak = max(peak, outstanding)
                    if submitted >= 4:
                        window_ready.set()
                future = super().submit(*args, **kwargs)

                def finished(_future):
                    nonlocal outstanding
                    with tracker_lock:
                        outstanding -= 1

                future.add_done_callback(finished)
                return future

        def reauthenticate(account, **_kwargs):
            release.wait(timeout=3)
            return {**account, "health_status": "alive"}

        try:
            with (
                patch.object(web_app, "ThreadPoolExecutor", TrackingExecutor),
                patch.object(web_app, "_reauthenticate_account_sync", side_effect=reauthenticate),
            ):
                started = jobs.start(records, concurrency=2)
                self.assertTrue(window_ready.wait(timeout=3))
                time.sleep(0.1)
                with tracker_lock:
                    peak_before_release = peak
                    submitted_before_release = submitted
                release.set()
                self.assertTrue(jobs.wait(started["job_id"], timeout=5))
                result = jobs.snapshot(started["job_id"])
        finally:
            release.set()

        self.assertLessEqual(peak_before_release, 4)
        self.assertLessEqual(submitted_before_release, 4)
        self.assertEqual(submitted, len(records))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["done"], len(records))
        self.assertEqual(result["success"], len(records))

    def test_account_reauth_batch_exposes_post_login_status_and_plan(self):
        import web_app

        jobs = web_app.AccountReauthBatchJobs()
        saved = {
            "email": "plus@example.com",
            "account_id": "account-plus",
            "health_status": "alive",
            "status": "正常",
            "plan_type": "plus",
            "password": "must-not-leak",
            "totp_secret": "must-not-leak-either",
        }
        with patch.object(
            web_app,
            "_reauthenticate_account_sync",
            return_value=saved,
        ):
            started = jobs.start(
                [{"email": "plus@example.com", "account_id": "account-plus"}],
                mode="protocol",
            )
            jobs.wait(started["job_id"], timeout=3)
            result = jobs.snapshot(started["job_id"])

        item = result["items"][0]
        self.assertEqual(item["status"], "success")
        self.assertEqual(item["health_status"], "alive")
        self.assertEqual(item["account_status"], "正常")
        self.assertEqual(item["plan_type"], "plus")
        self.assertTrue(item["has_password"])
        self.assertTrue(item["has_totp"])
        self.assertNotIn("must-not-leak", json.dumps(result))

    def test_account_reauth_batch_rejects_second_active_batch_once(self):
        import web_app

        jobs = web_app.AccountReauthBatchJobs()
        entered = threading.Event()
        release = threading.Event()

        def reauthenticate(account):
            entered.set()
            release.wait(timeout=3)
            return account

        with patch.object(web_app, "_reauthenticate_account_sync", side_effect=reauthenticate):
            started = jobs.start([{"email": "first@example.com"}])
            self.assertTrue(entered.wait(timeout=1))
            try:
                with self.assertRaisesRegex(RuntimeError, "已有重新登录批次"):
                    jobs.start([{"email": "second@example.com"}])
            finally:
                release.set()
            deadline = time.time() + 3
            result = jobs.snapshot(started["job_id"])
            while result["status"] in {"queued", "running"} and time.time() < deadline:
                time.sleep(0.01)
                result = jobs.snapshot(started["job_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["success"], 1)

    def test_expired_access_token_401_is_not_treated_as_dead_account(self):
        from common.account_health import InvalidAccessTokenError, check_account

        source = {
            "email": "expired@example.com",
            "access_token": _jwt({"exp": 1}),
        }
        with (
            patch("common.account_health.refresh_oauth_tokens", return_value=dict(source)),
            patch(
                "common.account_health.fetch_remote_account_info",
                side_effect=InvalidAccessTokenError("token invalidated (/backend-api/me)"),
            ),
            patch("common.account_health.upsert_account_record"),
        ):
            result = check_account(source)

        self.assertEqual(result["health_status"], "reauth_required")

    def test_icloud_mailbox_can_complete_passwordless_login_flow(self):
        from register.protocol import PlatformRegistrar

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.proxy = "http://proxy.example:8080"
        registrar.session = object()
        registrar.device_id = "device"
        registrar.code_verifier = "verifier"
        registrar.clearance_user_agent = ""
        registrar.fingerprint = {}
        registrar._authorize_continue_login = lambda *_args, **_kwargs: {}
        registrar._send_passwordless_otp = lambda *_args, **_kwargs: None

        response = type(
            "Response",
            (),
            {
                "status_code": 200,
                "text": "",
                "json": lambda self: {
                    "continue_url": "https://auth.openai.com/sign-in-with-chatgpt/platform/consent"
                },
            },
        )()
        with (
            patch("register.protocol.step"),
            patch("register.protocol.wait_for_code", return_value="123456"),
            patch("register.protocol.validate_otp", return_value=(response, "")),
            patch(
                "register.protocol.exchange_tokens_from_continue_url",
                return_value={"access_token": "token"},
            ),
        ):
            tokens = registrar._passwordless_login(
                "alias@icloud.com",
                {"provider": "icloud_api", "address": "alias@icloud.com"},
                1,
            )
        self.assertEqual(tokens["access_token"], "token")

    def test_protocol_credential_login_follows_password_totp_har_sequence(self):
        from register.protocol import PlatformRegistrar

        def response(payload, status=200, url="https://auth.openai.com/"):
            return SimpleNamespace(
                status_code=status,
                headers={},
                text=json.dumps(payload),
                url=url,
                json=lambda: payload,
            )

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.proxy = ""
        registrar.device_id = "device-id"
        registrar.clearance_user_agent = ""
        registrar.fingerprint = {
            "impersonate": "chrome142",
            "major": "142",
            "full_version": "142.0.0.0",
            "user_agent": "Mozilla/5.0 Chrome/142.0.0.0",
            "sec_ch_ua": '"Chromium";v="142"',
            "sec_ch_ua_full_version_list": '"Chromium";v="142.0.0.0"',
            "platform_version": "10.0.0",
            "accept_language": "en-US,en;q=0.9",
        }
        registrar.session = MagicMock()
        registrar._complete_chatgpt_session = MagicMock(
            return_value={
                "final_url": "https://auth.openai.com/log-in/password",
            }
        )
        registrar._authorize_continue_login = MagicMock()
        registrar._navigate_auth_step = MagicMock(
            return_value=response({}, 302, "https://chatgpt.com/")
        )
        registrar._read_chatgpt_session = MagicMock(
            return_value={"access_token": "fresh-access", "account_id": "account-id"}
        )
        password_result = {
            "continue_url": "https://auth.openai.com/mfa-challenge/challenge-id",
            "page": {
                "type": "mfa_challenge",
                "payload": {
                    "factor_id": "factor-id",
                    "factors": [
                        {"id": "factor-id", "factor_type": "totp", "is_recovery": False},
                        {"id": "recovery-id", "factor_type": "email", "is_recovery": True},
                    ],
                },
            },
        }
        verified_result = {
            "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=one&state=state",
            "page": {
                "type": "external_url",
                "payload": {
                    "url": "https://chatgpt.com/api/auth/callback/openai?code=one&state=state"
                },
            },
        }
        responses = [
            response(password_result),
            response({"client_auth_session": {}, "checksum": "checksum"}),
            response({"oai-client-auth-session": {}}),
            response(verified_result),
        ]
        with (
            patch("register.protocol.build_sentinel_token", return_value="sentinel"),
            patch("register.protocol.totp_code", return_value="123456"),
            patch(
                "register.protocol.request_with_local_retry",
                side_effect=[(item, "") for item in responses],
            ) as request,
            patch("register.protocol.step") as protocol_step,
        ):
            tokens = registrar.login_with_password_totp(
                "user@example.com",
                "Strong-password-1",
                "JBSWY3DPEHPK3PXP",
                1,
            )

        registrar._complete_chatgpt_session.assert_called_once_with(
            "user@example.com", "Strong-password-1", 1, start_only=True
        )
        registrar._authorize_continue_login.assert_not_called()
        self.assertEqual(tokens["access_token"], "fresh-access")
        self.assertEqual(tokens["mfa_factor_id"], "factor-id")
        self.assertEqual(tokens["mfa_factor_type"], "totp")
        step_text = "\n".join(str(call.args[1]) for call in protocol_step.call_args_list)
        self.assertIn("协议凭据登录[1/6]", step_text)
        self.assertIn("协议凭据登录[5/6] TOTP 验证码已生成", step_text)
        self.assertIn("ChatGPT 会话读取完成", step_text)
        self.assertEqual(
            [call.args[1:3] for call in request.call_args_list],
            [
                ("post", "https://auth.openai.com/api/accounts/password/verify"),
                ("get", "https://auth.openai.com/api/accounts/client_auth_session_dump"),
                ("post", "https://auth.openai.com/api/accounts/mfa/issue_challenge"),
                ("post", "https://auth.openai.com/api/accounts/mfa/verify"),
            ],
        )
        self.assertEqual(request.call_args_list[0].kwargs["json"], {"password": "Strong-password-1"})
        self.assertEqual(
            request.call_args_list[2].kwargs["json"],
            {"id": "factor-id", "type": "totp", "force_fresh_challenge": False},
        )
        self.assertEqual(
            request.call_args_list[3].kwargs["json"],
            {"id": "factor-id", "type": "totp", "code": "123456"},
        )
        self.assertEqual(
            request.call_args_list[0].kwargs["headers"]["openai-sentinel-token"],
            "sentinel",
        )
        self.assertIn("x-access-flow-invocation-id", request.call_args_list[0].kwargs["headers"])
        self.assertIn("x-access-flow-invocation-id", request.call_args_list[3].kwargs["headers"])
        self.assertNotIn("x-access-flow-invocation-id", request.call_args_list[2].kwargs["headers"])

    def test_protocol_credential_login_submits_email_identity_before_password(self):
        from register.protocol import PlatformRegistrar

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar._complete_chatgpt_session = MagicMock(
            return_value={
                "final_url": "https://auth.openai.com/log-in?usernameKind=email",
            }
        )
        registrar._authorize_continue_login = MagicMock(
            return_value={
                "continue_url": "https://auth.openai.com/log-in/password",
                "page": {"type": "password_verification"},
            }
        )
        registrar._post_auth_json = MagicMock(
            side_effect=RuntimeError("password verification reached")
        )

        with self.assertRaisesRegex(RuntimeError, "password verification reached"):
            registrar.login_with_password_totp(
                "user@example.com",
                "Strong-password-1",
                "JBSWY3DPEHPK3PXP",
                1,
            )

        registrar._authorize_continue_login.assert_called_once_with(
            "user@example.com",
            1,
            passwordless=False,
            restart_invalid_state=False,
        )
        registrar._post_auth_json.assert_called_once()

    def test_protocol_credential_login_rejects_email_otp_route(self):
        from register.protocol import PlatformRegistrar

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar._complete_chatgpt_session = MagicMock(
            return_value={
                "final_url": "https://auth.openai.com/log-in?usernameKind=email",
            }
        )
        registrar._authorize_continue_login = MagicMock(
            return_value={
                "continue_url": "https://auth.openai.com/email-verification",
                "page": {"type": "email_otp_verification"},
            }
        )
        registrar._post_auth_json = MagicMock()

        with self.assertRaisesRegex(
            RuntimeError,
            "服务端将该账号导向邮箱验证",
        ):
            registrar.login_with_password_totp(
                "user@example.com",
                "Strong-password-1",
                "JBSWY3DPEHPK3PXP",
                1,
            )

        registrar._post_auth_json.assert_not_called()

    def test_protocol_credential_login_rejects_missing_credentials_before_session(self):
        from register.protocol import PlatformRegistrar

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar._complete_chatgpt_session = MagicMock()
        with self.assertRaisesRegex(RuntimeError, "同时具备密码和 TOTP 2FA"):
            registrar.login_with_password_totp("user@example.com", "", "secret", 1)
        registrar._complete_chatgpt_session.assert_not_called()

    def test_protocol_credential_login_preserves_proxy_error_detail(self):
        from register.protocol import login_with_password_totp_one

        registrar = MagicMock()
        registrar.login_with_password_totp.side_effect = RuntimeError(
            "Failed to perform, curl: (28) Connection timed out after 30000 milliseconds"
        )
        errors = []
        with patch("register.protocol.PlatformRegistrar", return_value=registrar):
            result = login_with_password_totp_one(
                1,
                email="proxy-timeout@example.com",
                password="Strong-password-1",
                totp_secret="JBSWY3DPEHPK3PXP",
                error_sink=errors.append,
            )

        self.assertIsNone(result)
        self.assertEqual(len(errors), 1)
        self.assertIn("curl: (28)", errors[0])
        self.assertNotIn("name 'e' is not defined", errors[0])
        registrar.close.assert_called_once()

    def test_protocol_login_account_log_sink_gets_redacted_structured_events(self):
        from register.protocol import login_with_password_totp_one

        registrar = MagicMock()
        registrar.login_with_password_totp.side_effect = RuntimeError(
            "provider echoed Strong-password-1 JBSWY3DPEHPK3PXP"
        )
        task_events = []
        account_events = []
        with patch("register.protocol.PlatformRegistrar", return_value=registrar):
            result = login_with_password_totp_one(
                4,
                email="audit@example.com",
                password="Strong-password-1",
                totp_secret="JBSWY3DPEHPK3PXP",
                task_event=lambda index, status=None, **fields: task_events.append(
                    (index, status, fields)
                ),
                account_log_sink=account_events.append,
            )

        self.assertIsNone(result)
        self.assertTrue(account_events)
        self.assertEqual({item["operation"] for item in account_events}, {"login"})
        self.assertEqual(account_events[0]["status"], "running")
        self.assertEqual(account_events[-1]["status"], "failed")
        self.assertTrue(all(item["email"] == "audit@example.com" for item in account_events))
        serialized = json.dumps(account_events, ensure_ascii=False)
        self.assertNotIn("Strong-password-1", serialized)
        self.assertNotIn("JBSWY3DPEHPK3PXP", serialized)
        self.assertTrue(task_events)
        self.assertTrue(all(set(item).issubset({"status", "operation", "at", "index", "email", "step", "message", "error"}) for item in account_events))

    def test_protocol_password_change_account_log_sink_receives_success(self):
        from register.protocol import change_password_one

        registrar = MagicMock()
        registrar.fingerprint = {}
        registrar.device_id = "device"
        registrar.change_password.return_value = {
            "access_token": "access-token",
            "session_token": "session-token",
            "account_id": "account-id",
        }
        account_events = []
        with patch("register.protocol.PlatformRegistrar", return_value=registrar):
            result = change_password_one(
                2,
                email="change@example.com",
                old_password="Old-password-1",
                new_password="New-password-2",
                totp_secret="JBSWY3DPEHPK3PXP",
                account_log_sink=account_events.append,
            )

        self.assertEqual(result["account_id"], "account-id")
        self.assertEqual({item["operation"] for item in account_events}, {"password_change"})
        self.assertEqual([item["status"] for item in account_events], ["running", "success"])
        self.assertEqual(account_events[-1]["step"], "修改密码完成")

    def test_passwordless_login_restarts_when_otp_send_session_is_invalid(self):
        from register.protocol import PlatformRegistrar

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.proxy = ""
        registrar._reset_auth_cookies = MagicMock()
        registrar._platform_authorize = MagicMock()
        registrar._authorize_continue_login = MagicMock(return_value={})
        registrar._send_passwordless_otp = MagicMock(
            side_effect=[RuntimeError("invalid_state"), None]
        )
        registrar._complete_passwordless_login = MagicMock(
            return_value={"access_token": "fresh", "refresh_token": "refresh"}
        )
        mailbox = {"provider": "icloud_api"}
        with patch("register.protocol.wait_for_code", return_value="123456"):
            tokens = registrar._passwordless_login("user@icloud.com", mailbox, 1)

        self.assertEqual(tokens["access_token"], "fresh")
        self.assertEqual(registrar._send_passwordless_otp.call_count, 2)
        registrar._reset_auth_cookies.assert_called_once()
        registrar._platform_authorize.assert_called_once_with(
            "user@icloud.com", 1, screen_hint="login_or_signup"
        )

    def test_protocol_oauth_callback_rejects_explicit_state_mismatch(self):
        from register.protocol import _callback_state_matches

        self.assertTrue(_callback_state_matches({"state": "expected"}, "expected"))
        self.assertTrue(_callback_state_matches({"state": ""}, "expected"))
        self.assertFalse(_callback_state_matches({"state": "different"}, "expected"))

    def test_protocol_request_retries_temporary_get_status(self):
        from register.protocol import request_with_local_retry

        responses = [
            type("Response", (), {"status_code": 503, "headers": {}})(),
            type("Response", (), {"status_code": 200, "headers": {}})(),
        ]
        session = MagicMock()
        session.request.side_effect = responses
        with patch("register.protocol.time.sleep") as sleep:
            response, error = request_with_local_retry(
                session, "get", "https://auth.openai.com/example", retry_attempts=2
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(error, "")
        self.assertEqual(session.request.call_count, 2)
        sleep.assert_called_once()

    def test_protocol_auth_navigation_refreshes_cloudflare_clearance(self):
        from register.protocol import PlatformRegistrar

        challenge = SimpleNamespace(
            status_code=403,
            headers={"cf-ray": "fixture-ray"},
            text="<title>Just a moment...</title>",
            url="https://chatgpt.com/api/auth/callback/openai",
        )
        success = SimpleNamespace(
            status_code=200,
            headers={},
            text="ok",
            url="https://chatgpt.com/",
        )
        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.proxy = ""
        registrar.session = MagicMock()
        registrar.fingerprint = {}
        registrar.clearance_user_agent = ""
        registrar.clearance_failure_reason = ""
        registrar._refresh_cloudflare_clearance = MagicMock(return_value=object())

        with (
            patch(
                "register.protocol.request_with_local_retry",
                side_effect=[(challenge, ""), (success, "")],
            ) as request,
            patch(
                "register.protocol._headers_with_clearance",
                side_effect=lambda headers, *_args, **_kwargs: headers,
            ),
            patch("register.protocol.step"),
        ):
            response = registrar._navigate_auth_step(
                "https://chatgpt.com/api/auth/callback/openai",
                "https://auth.openai.com/email-verification",
                1,
            )

        self.assertIs(response, success)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(
            [call.kwargs["retry_attempts"] for call in request.call_args_list],
            [5, 5],
        )
        registrar._refresh_cloudflare_clearance.assert_called_once()

    def test_protocol_clearance_refresh_uses_current_task_proxy_config(self):
        import register.protocol as protocol
        from register.protocol import PlatformRegistrar

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.proxy = "http://proxy.example:8080"
        registrar.session = MagicMock()
        registrar.fingerprint = {}
        registrar.clearance_user_agent = ""
        task_proxy_config = {
            "clearance": {
                "enabled": True,
                "mode": "flaresolverr",
                "flaresolverr_url": "http://flaresolverr.example:8191",
            }
        }
        token = protocol._PROXY_CONFIG.set(task_proxy_config)
        try:
            with (
                patch("register.protocol.refresh_clearance", return_value=None) as refresh,
                patch("register.protocol.clearance_error", return_value="fixture error"),
                patch("register.protocol.step"),
            ):
                result = registrar._refresh_cloudflare_clearance(
                    "https://chatgpt.com/",
                    1,
                )
        finally:
            protocol._PROXY_CONFIG.reset(token)

        self.assertIsNone(result)
        self.assertIs(refresh.call_args.args[0], task_proxy_config)
        self.assertEqual(
            refresh.call_args.kwargs["request_proxy"],
            "http://proxy.example:8080",
        )

    def test_protocol_fingerprint_variants_remain_internally_consistent(self):
        from register.protocol import _make_browser_fingerprint

        samples = [_make_browser_fingerprint() for _ in range(40)]
        for fingerprint in samples:
            self.assertEqual(
                fingerprint["impersonate"], f"firefox{fingerprint['major']}"
            )
            self.assertIn(
                f"Firefox/{fingerprint['full_version']}", fingerprint["user_agent"]
            )
            self.assertEqual(fingerprint["sec_ch_ua"], "")
            self.assertNotIn("Chrome", fingerprint["user_agent"])
            self.assertNotIn("Google", fingerprint["user_agent"])
            self.assertIn(fingerprint["screen"], {
                "1920x1080", "1536x864", "1366x768",
                "1600x900", "2560x1440", "1920x1200",
                "1280x720", "1440x900", "1680x1050", "2560x1600",
            })
        self.assertEqual(len({item["profile_id"] for item in samples}), len(samples))

    def test_japan_proxy_uses_japanese_language_and_tokyo_clock(self):
        from register.protocol import _make_browser_fingerprint

        with patch.dict(os.environ, {"REGISTER_PROXY_COUNTRY": "US"}):
            fingerprint = _make_browser_fingerprint(
                "http://user-region-JP-session-1:pass@proxy.example:8080"
            )
        self.assertEqual(
            fingerprint["accept_language"],
            "ja-JP,ja;q=0.9,en-US;q=0.7,en;q=0.6",
        )
        self.assertEqual(fingerprint["locale"], "ja-JP")
        self.assertEqual(fingerprint["timezone"], "Asia/Tokyo")
        self.assertEqual(fingerprint["timezone_offset_min"], "-540")

    def test_us_proxy_uses_stable_us_language_and_new_york_clock(self):
        from register.protocol import _make_browser_fingerprint

        with patch.dict(os.environ, {"REGISTER_PROXY_COUNTRY": "JP"}):
            fingerprint = _make_browser_fingerprint(
                "http://user_g-US_f-1_sid-one_l-10:pass@proxy.example:8080"
            )
        self.assertEqual(fingerprint["accept_language"], "en-US,en;q=0.9")
        self.assertEqual(fingerprint["locale"], "en-US")
        self.assertEqual(fingerprint["timezone"], "America/New_York")
        self.assertIn(fingerprint["timezone_offset_min"], {"240", "300"})
        self.assertIn(fingerprint["webgl_vendor"], {"Intel Inc.", "NVIDIA Corporation", "AMD"})
        self.assertIn("ANGLE (", fingerprint["webgl_renderer"])

    def test_country_proxy_traits_cover_ph_th_tr_vn_kr(self):
        from register.protocol import _make_browser_fingerprint

        expected = {
            "PH": ("en-PH", "Asia/Manila", "-480"),
            "TH": ("th-TH", "Asia/Bangkok", "-420"),
            "TR": ("tr-TR", "Europe/Istanbul", "-180"),
            "VN": ("vi-VN", "Asia/Ho_Chi_Minh", "-420"),
            "KR": ("ko-KR", "Asia/Seoul", "-540"),
        }
        allowed_screens = {
            "1920x1080",
            "1366x768",
            "1536x864",
            "1600x900",
            "1920x1200",
            "1440x900",
            "2560x1440",
        }
        with patch.dict(os.environ, {"REGISTER_PROXY_COUNTRY": "US"}):
            for country, (locale, timezone_name, offset) in expected.items():
                fingerprint = _make_browser_fingerprint(
                    f"http://user_g-{country}_f-1_sid-one_l-10:pass@proxy.example:8080"
                )
                self.assertEqual(fingerprint["locale"], locale)
                self.assertEqual(fingerprint["accept_language"].split(",", 1)[0], locale)
                self.assertEqual(fingerprint["timezone"], timezone_name)
                self.assertEqual(fingerprint["timezone_offset_min"], offset)
                self.assertEqual(fingerprint["platform"], "Win32")
                self.assertEqual(fingerprint["max_touch_points"], "0")
                self.assertIn(fingerprint["screen"], allowed_screens)
                self.assertIn(fingerprint["webgl_vendor"], {"Intel Inc.", "NVIDIA Corporation", "AMD"})
                self.assertIn("ANGLE (", fingerprint["webgl_renderer"])

    def test_selected_country_is_used_when_proxy_has_no_region_label(self):
        from register.protocol import _make_browser_fingerprint

        with patch.dict(os.environ, {"REGISTER_PROXY_COUNTRY": "JP"}):
            fingerprint = _make_browser_fingerprint("http://proxy.example:8080")
        self.assertTrue(fingerprint["accept_language"].startswith("ja-JP,"))
        self.assertEqual(fingerprint["timezone_offset_min"], "-540")

    def test_sentinel_reuses_session_device_traits(self):
        from register.protocol_sentinel import SentinelTokenGenerator

        generator = SentinelTokenGenerator(
            "device-id",
            "user-agent",
            sid="stable-sid",
            device_profile={
                "screen": "2560x1440",
                "hardware_concurrency": "16",
                "accept_language": "en-GB,en;q=0.9",
            },
        )
        first = generator._get_config()
        second = generator._get_config()

        self.assertEqual(generator.sid, "stable-sid")
        self.assertEqual((first[0], first[8], first[16]), ("2560x1440", "en-GB", 16))
        self.assertEqual((second[0], second[8], second[16]), ("2560x1440", "en-GB", 16))

    def test_sentinel_builder_returns_independent_live_sdk_observer_header(self):
        from register.protocol_sentinel import build_sentinel_with_so_token

        session = MagicMock()
        with patch(
            "register.protocol_sentinel._build_live_sentinel",
            return_value=("sentinel-token", "observer-token", "0challenge-token"),
        ):
            sentinel, observer, cookie = build_sentinel_with_so_token(
                session, "device-id", "oauth_create_account"
            )

        self.assertEqual(sentinel, "sentinel-token")
        self.assertEqual(observer, "observer-token")
        self.assertEqual(cookie, "0challenge-token")

    def test_live_sentinel_sdk_uses_the_account_fingerprint_for_both_vm_steps(self):
        from register.protocol_sentinel import build_sentinel_with_so_token

        challenge = {
            "token": "challenge-token",
            "turnstile": {"required": True},
            "so": {"required": True},
        }
        response = MagicMock(status_code=200)
        response.json.return_value = challenge
        session = MagicMock()
        session.cookies.jar = []
        session.post.return_value = response
        main_token = json.dumps(
            {
                "p": "enforcement-proof",
                "t": "turnstile-proof",
                "c": "challenge-token",
                "id": "device-id",
                "flow": "oauth_create_account",
            }
        )
        so_token = json.dumps(
            {
                "so": "observer-proof",
                "c": "challenge-token",
                "id": "device-id",
                "flow": "oauth_create_account",
            }
        )
        with (
            patch(
                "register.protocol_sentinel._load_live_sdk",
                return_value=(
                    "https://sentinel.openai.com/sentinel/live/sdk.js",
                    "sdk-source",
                ),
            ),
            patch(
                "register.protocol_sentinel._run_sentinel_sdk",
                side_effect=[
                    {"proof": "requirements-proof"},
                    {"token": main_token, "soToken": so_token},
                ],
            ) as run_sdk,
        ):
            sentinel, observer, cookie = build_sentinel_with_so_token(
                session,
                "device-id",
                "oauth_create_account",
                user_agent="Mozilla/5.0 Chrome/142.0.0.0",
                sec_ch_ua='"Chromium";v="142"',
                device_profile={
                    "screen": "2560x1440",
                    "hardware_concurrency": "16",
                    "accept_language": "ja-JP,ja;q=0.9,en-US;q=0.7,en;q=0.6",
                    "timezone": "Asia/Tokyo",
                    "timezone_offset_min": "-540",
                },
            )

        self.assertEqual(sentinel, main_token)
        self.assertEqual(observer, so_token)
        self.assertEqual(cookie, "0challenge-token")
        for call in run_sdk.call_args_list:
            vm_profile = call.args[0]["profile"]
            self.assertEqual(vm_profile["screen"]["width"], 2560)
            self.assertEqual(vm_profile["navigator"]["hardwareConcurrency"], 16)
            self.assertEqual(vm_profile["navigator"]["language"], "ja-JP")
            self.assertEqual(vm_profile["navigator"]["languages"], ["ja-JP", "ja", "en-US", "en"])
            self.assertEqual(vm_profile["timezone"], "Asia/Tokyo")
            self.assertEqual(vm_profile["timezoneOffsetMinutes"], -540)

    def test_totp_matches_rfc6238_sha1_vectors(self):
        from register.protocol import _totp_code

        secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
        vectors = {
            59: "287082",
            1_111_111_109: "081804",
            1_111_111_111: "050471",
            1_234_567_890: "005924",
            2_000_000_000: "279037",
            20_000_000_000: "353130",
        }
        for timestamp, expected in vectors.items():
            with self.subTest(timestamp=timestamp):
                self.assertEqual(_totp_code(secret, timestamp), expected)

    def test_protocol_mfa_requests_follow_har_and_save_totp_secret(self):
        from register.protocol import PlatformRegistrar

        def response(payload, status=200):
            return SimpleNamespace(
                status_code=status,
                headers={},
                text=json.dumps(payload),
                url="https://chatgpt.com/",
                json=lambda: payload,
            )

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.proxy = ""
        registrar.session = MagicMock()
        registrar.device_id = "device-fixture"
        registrar.fingerprint = {}
        registrar.clearance_user_agent = ""
        responses = [
            response({"mfa_enabled": False, "factors": {"totp": []}}),
            response({"aas_eligible": True}),
            response(
                {
                    "secret": "JBSWY3DPEHPK3PXP",
                    "session_id": "temporary-session-id",
                    "factor": {"id": "factor-fixture", "factor_type": "totp"},
                }
            ),
            response({"success": True}),
            response(
                {
                    "mfa_enabled": True,
                    "factors": {
                        "totp": [{"id": "factor-fixture", "factor_type": "totp"}]
                    },
                }
            ),
        ]
        with (
            patch(
                "register.protocol.request_with_local_retry",
                side_effect=[(item, "") for item in responses],
            ) as request,
            patch("register.protocol._totp_code", return_value="575850") as totp,
            patch("register.protocol.step"),
        ):
            result = registrar._enroll_totp({"access_token": "access-token"}, 7)

        self.assertEqual(result["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertTrue(result["mfa_enabled"])
        self.assertEqual(result["mfa_factor_type"], "totp")
        self.assertEqual(result["mfa_factor_id"], "factor-fixture")
        totp.assert_called_once_with("JBSWY3DPEHPK3PXP")
        self.assertEqual(
            [call.args[1:3] for call in request.call_args_list],
            [
                ("get", "https://chatgpt.com/backend-api/accounts/mfa_info"),
                ("get", "https://chatgpt.com/backend-api/accounts/security_settings/info"),
                ("post", "https://chatgpt.com/backend-api/accounts/mfa/enroll"),
                ("post", "https://chatgpt.com/backend-api/accounts/mfa/user/activate_enrollment"),
                ("get", "https://chatgpt.com/backend-api/accounts/mfa_info"),
            ],
        )
        self.assertEqual(
            request.call_args_list[2].kwargs["json"],
            {"factor_type": "totp"},
        )
        self.assertEqual(
            request.call_args_list[3].kwargs["json"],
            {
                "code": "575850",
                "factor_type": "totp",
                "session_id": "temporary-session-id",
            },
        )
        self.assertEqual(
            request.call_args_list[3].kwargs["headers"]["origin"],
            "https://chatgpt.com",
        )

    def test_protocol_mfa_activation_retries_with_next_totp_window(self):
        from register.protocol import PlatformRegistrar

        def response(payload, status=200):
            return SimpleNamespace(
                status_code=status,
                headers={},
                text=json.dumps(payload),
                url="https://chatgpt.com/",
                json=lambda: payload,
            )

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar._chatgpt_mfa_request = MagicMock(
            side_effect=[
                (response({"mfa_enabled": False}), ""),
                (response({"aas_eligible": True}), ""),
                (
                    response(
                        {
                            "secret": "JBSWY3DPEHPK3PXP",
                            "session_id": "temporary-session-id",
                            "factor": {"id": "factor-fixture"},
                        }
                    ),
                    "",
                ),
                (response({"success": False, "error": "activation rejected"}), ""),
                (response({"success": True}), ""),
                (
                    response(
                        {
                            "mfa_enabled": True,
                            "factors": {"totp": [{"id": "factor-fixture"}]},
                        }
                    ),
                    "",
                ),
            ]
        )

        with (
            patch(
                "register.protocol._totp_code",
                side_effect=["111111", "111111", "222222"],
            ),
            patch("register.protocol.time.sleep") as sleep,
            patch("register.protocol.step") as protocol_step,
        ):
            result = registrar._enroll_totp(
                {"access_token": "access-token"},
                1,
            )

        self.assertTrue(result["mfa_enabled"])
        activation_calls = [
            item
            for item in registrar._chatgpt_mfa_request.call_args_list
            if item.args[1].endswith("/activate_enrollment")
        ]
        self.assertEqual(
            [item.args[4]["code"] for item in activation_calls],
            ["111111", "222222"],
        )
        sleep.assert_called_once_with(1.0)
        self.assertTrue(
            any(
                "下一个验证码时间窗" in str(item.args[1])
                for item in protocol_step.call_args_list
                if len(item.args) > 1
            )
        )

    def test_protocol_mfa_replace_accepts_totp_item_without_factor_type(self):
        """The explicit factors.totp collection is authoritative."""
        from register.protocol import PlatformRegistrar

        def response(payload, status=200):
            return SimpleNamespace(
                status_code=status,
                headers={},
                text=json.dumps(payload),
                url="https://chatgpt.com/",
                json=lambda: payload,
            )

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar._chatgpt_mfa_request = MagicMock(
            side_effect=[
                (
                    response(
                        {
                            "mfa_enabled": True,
                            "factors": {
                                "totp": [
                                    {"id": "recovery-totp", "is_recovery": "true"},
                                    {"id": "old-totp", "is_recovery": "false"},
                                ]
                            },
                        }
                    ),
                    "",
                ),
                (response({"ok": True}), ""),  # disable old factor
                (response({"mfa_enabled": False}), ""),  # disable confirmed
                (response({"aas_eligible": True}), ""),
                (
                    response(
                        {
                            "secret": "JBSWY3DPEHPK3PXP",
                            "session_id": "temporary-session-id",
                            "factor": {"id": "new-totp"},
                        }
                    ),
                    "",
                ),
                (response({"success": True}), ""),
                (
                    response(
                        {
                            "mfa_enabled": True,
                            "factors": {"totp": [{"id": "new-totp"}]},
                        }
                    ),
                    "",
                ),
            ]
        )

        with (
            patch("register.protocol._totp_code", return_value="575850"),
            patch("register.protocol.step"),
        ):
            result = registrar._enroll_totp(
                {"access_token": "access-token"},
                1,
                replace_existing=True,
            )

        self.assertEqual(result["mfa_factor_id"], "new-totp")
        disable_calls = [
            item
            for item in registrar._chatgpt_mfa_request.call_args_list
            if item.args[1].endswith("/disable_in_house")
        ]
        self.assertEqual(len(disable_calls), 1)
        self.assertEqual(disable_calls[0].args[4], {"factor_id": "old-totp"})

    def test_protocol_mfa_replace_does_not_fallback_to_native_passkey_factor(self):
        from register.protocol import PlatformRegistrar

        def response(payload, status=200):
            return SimpleNamespace(
                status_code=status,
                headers={},
                text=json.dumps(payload),
                url="https://chatgpt.com/",
                json=lambda: payload,
            )

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar._chatgpt_mfa_request = MagicMock(
            return_value=(
                response(
                    {
                        "mfa_enabled": True,
                        "native_default_factor_id": "passkey-only",
                        "factors": {
                            "passkey": [{"id": "passkey-only", "factor_type": "passkey"}]
                        },
                    }
                ),
                "",
            )
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "mfa_enabled_totp_factor_id_missing",
        ):
            registrar._enroll_totp(
                {"access_token": "access-token"},
                1,
                replace_existing=True,
            )

        registrar._chatgpt_mfa_request.assert_called_once()

    def test_protocol_mfa_replace_waits_for_disable_confirmation_with_bounded_backoff(self):
        from register.protocol import PlatformRegistrar

        def response(payload, status=200):
            return SimpleNamespace(
                status_code=status,
                headers={},
                text=json.dumps(payload),
                url="https://chatgpt.com/",
                json=lambda: payload,
            )

        enabled = response({"mfa_enabled": True})
        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar._chatgpt_mfa_request = MagicMock(
            side_effect=[
                (response({"mfa_enabled": True, "factors": {"totp": [{"id": "old"}]}}), ""),
                (response({"ok": True}), ""),
                (enabled, ""),
                (enabled, ""),
                (enabled, ""),
                (response({"mfa_enabled": False}), ""),
                (response({"aas_eligible": True}), ""),
                (
                    response(
                        {
                            "secret": "JBSWY3DPEHPK3PXP",
                            "session_id": "temporary-session-id",
                            "factor": {"id": "new"},
                        }
                    ),
                    "",
                ),
                (response({"success": True}), ""),
                (response({"mfa_enabled": True, "factors": {"totp": [{"id": "new"}]}}), ""),
            ]
        )
        sleeps = []

        with (
            patch("register.protocol._totp_code", return_value="575850"),
            patch("register.protocol.time.sleep", side_effect=sleeps.append),
            patch("register.protocol.step"),
        ):
            registrar._enroll_totp(
                {"access_token": "access-token"},
                1,
                replace_existing=True,
            )

        self.assertAlmostEqual(sum(sleeps), 7.0, places=6)
        self.assertTrue(all(0 < delay <= 0.25 for delay in sleeps))
        self.assertEqual(len(sleeps), 28)

    def test_protocol_mfa_replace_stop_during_disable_confirmation_aborts_before_enroll(self):
        from register.protocol import PlatformRegistrar
        from register.sessions import JobCancelled
        import register.protocol as protocol

        def response(payload, status=200):
            return SimpleNamespace(
                status_code=status,
                headers={},
                text=json.dumps(payload),
                url="https://chatgpt.com/",
                json=lambda: payload,
            )

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar._chatgpt_mfa_request = MagicMock(
            side_effect=[
                (response({"mfa_enabled": True, "factors": {"totp": [{"id": "old"}]}}), ""),
                (response({"ok": True}), ""),
                (response({"mfa_enabled": True}), ""),
            ]
        )
        stopped = {"value": False}
        stop_token = protocol._STOP_CHECK.set(lambda: stopped["value"])
        try:
            def stop_after_sleep(delay):
                self.assertEqual(delay, 0.25)
                stopped["value"] = True

            with (
                patch("register.protocol.time.sleep", side_effect=stop_after_sleep),
                patch("register.protocol.step"),
            ):
                with self.assertRaises(JobCancelled):
                    registrar._enroll_totp(
                        {"access_token": "access-token"},
                        1,
                        replace_existing=True,
                    )
        finally:
            protocol._STOP_CHECK.reset(stop_token)

        self.assertEqual(registrar._chatgpt_mfa_request.call_count, 3)
        self.assertFalse(
            any(
                item.args[1].endswith("/mfa/enroll")
                for item in registrar._chatgpt_mfa_request.call_args_list
            )
        )

    def test_protocol_security_repair_saves_password_and_totp_incrementally(self):
        from register.protocol import repair_security_one

        registrar = MagicMock()
        registrar._complete_post_registration_password.return_value = "Aa1!repair-test"
        registrar._read_chatgpt_session.return_value = {
            "access_token": "access-token",
            "session_token": "",
            "account_id": "account-id",
        }
        registrar._enroll_totp.return_value = {
            "mfa_enabled": True,
            "mfa_factor_type": "totp",
            "mfa_factor_id": "factor-repair",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }
        mailbox = {
            "address": "repair@example.com",
            "provider": "icloud_api",
            "provider_ref": "icloud_api#1",
        }
        account = {
            "email": "repair@example.com",
            "access_token": "access-token",
            "account_id": "account-id",
            "password": "",
            "totp_secret": "",
        }
        partials = []
        with (
            patch("register.protocol.PlatformRegistrar", return_value=registrar),
            patch("register.protocol.create_mailbox", return_value=mailbox),
            patch("register.protocol._random_password", return_value="generated"),
            patch("register.protocol.mark_mailbox_result") as mailbox_result,
        ):
            result = repair_security_one(
                1,
                account=account,
                mail_config={"providers": []},
                provider_type="icloud_api",
                partial_sink=partials.append,
            )

        self.assertEqual(result["password"], "Aa1!repair-test")
        self.assertTrue(result["password_set"])
        self.assertEqual(result["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(partials[0], {"password": "Aa1!repair-test", "password_set": True})
        self.assertEqual(partials[1]["mfa_factor_id"], "factor-repair")
        mailbox_result.assert_called_once_with(mailbox, success=True)
        registrar._complete_post_registration_password.assert_called_once()
        registrar._enroll_totp.assert_called_once_with(
            {
                "access_token": "access-token",
                "session_token": "",
                "account_id": "account-id",
            },
            1,
        )

    def test_protocol_security_repair_keeps_password_when_totp_fails(self):
        from register.protocol import repair_security_one

        registrar = MagicMock()
        registrar._complete_post_registration_password.return_value = "Aa1!repair-test"
        registrar._read_chatgpt_session.return_value = {
            "access_token": "access-token",
            "session_token": "",
            "account_id": "account-id",
        }
        registrar._enroll_totp.side_effect = RuntimeError("mfa enrollment failed")
        mailbox = {
            "address": "repair@example.com",
            "provider": "icloud_api",
            "provider_ref": "icloud_api#1",
        }
        partials = []
        with (
            patch("register.protocol.PlatformRegistrar", return_value=registrar),
            patch("register.protocol.create_mailbox", return_value=mailbox),
            patch("register.protocol._random_password", return_value="generated"),
            patch("register.protocol.mark_mailbox_result") as mailbox_result,
        ):
            with self.assertRaisesRegex(RuntimeError, "mfa enrollment failed"):
                repair_security_one(
                    1,
                    account={
                        "email": "repair@example.com",
                        "access_token": "access-token",
                        "password": "",
                        "totp_secret": "",
                    },
                    mail_config={"providers": []},
                    provider_type="icloud_api",
                    partial_sink=partials.append,
                )

        self.assertEqual(partials, [{"password": "Aa1!repair-test", "password_set": True}])
        mailbox_result.assert_called_once_with(
            mailbox,
            success=False,
            error=unittest.mock.ANY,
        )

    def test_protocol_security_repair_replaces_unconfirmed_browser_password(self):
        from register.protocol import repair_security_one

        registrar = MagicMock()
        registrar._complete_post_registration_password.return_value = (
            "Aa1!server-confirmed"
        )
        registrar._enroll_totp.return_value = {
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "mfa_enabled": True,
        }
        mailbox = {
            "address": "browser-pending@example.com",
            "provider": "icloud_api",
        }
        with (
            patch("register.protocol.PlatformRegistrar", return_value=registrar),
            patch("register.protocol.create_mailbox", return_value=mailbox),
            patch("register.protocol._random_password", return_value="Aa1!generated"),
            patch("register.protocol.mark_mailbox_result"),
        ):
            result = repair_security_one(
                1,
                account={
                    "email": "browser-pending@example.com",
                    "password": "Aa1!browser-candidate",
                    "password_set": False,
                    "access_token": "access-token",
                    "totp_secret": "",
                },
                mail_config={"providers": []},
            )

        self.assertEqual(result["password"], "Aa1!server-confirmed")
        registrar._complete_post_registration_password.assert_called_once()

    def test_protocol_security_repair_passes_existing_totp_to_password_flow(self):
        from register.protocol import repair_security_one

        registrar = MagicMock()
        final_tokens = {
            "access_token": "fresh-access-token",
            "refresh_token": "fresh-refresh-token",
            "session_token": "fresh-session-token",
            "account_id": "fresh-account-id",
            "plan_type": "free",
        }

        def complete_password(_email, password, _mailbox, _index, **kwargs):
            self.assertTrue(callable(kwargs.get("credential_sink")))
            kwargs["credential_sink"](final_tokens)
            return password

        registrar._complete_post_registration_password.side_effect = complete_password
        mailbox = {
            "address": "existing-mfa@example.com",
            "provider": "icloud_api",
            "provider_ref": "icloud_api#1",
        }
        partials = []
        with (
            patch("register.protocol.PlatformRegistrar", return_value=registrar),
            patch("register.protocol.create_mailbox", return_value=mailbox),
            patch(
                "register.protocol._random_password",
                return_value="Aa1!confirmed-password",
            ),
            patch("register.protocol.mark_mailbox_result") as mailbox_result,
            patch("register.protocol.step"),
        ):
            result = repair_security_one(
                1,
                account={
                    "email": "existing-mfa@example.com",
                    "access_token": "old-access-token",
                    "password": "",
                    "mfa_enabled": True,
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                },
                mail_config={"providers": []},
                provider_type="icloud_api",
                partial_sink=partials.append,
            )

        registrar._complete_post_registration_password.assert_called_once()
        password_call = registrar._complete_post_registration_password.call_args
        self.assertEqual(
            password_call.args[:4],
            (
                "existing-mfa@example.com",
                "Aa1!confirmed-password",
                mailbox,
                1,
            ),
        )
        self.assertEqual(
            password_call.kwargs["totp_secret"],
            "JBSWY3DPEHPK3PXP",
        )
        self.assertTrue(callable(password_call.kwargs["credential_sink"]))
        registrar.login_with_password_totp.assert_not_called()
        registrar._enroll_totp.assert_not_called()
        self.assertEqual(result["password"], "Aa1!confirmed-password")
        self.assertTrue(result["password_set"])
        self.assertEqual(
            partials,
            [
                {"password": "Aa1!confirmed-password", "password_set": True},
                {
                    "access_token": "fresh-access-token",
                    "refresh_token": "fresh-refresh-token",
                    "session_token": "fresh-session-token",
                    "account_id": "fresh-account-id",
                    "plan_type": "free",
                },
            ],
        )
        self.assertEqual(result["access_token"], "fresh-access-token")
        self.assertEqual(result["session_token"], "fresh-session-token")
        self.assertEqual(result["account_id"], "fresh-account-id")
        self.assertFalse(
            any(item.get("access_token") == "new-access-only" for item in partials)
        )
        mailbox_result.assert_called_once_with(mailbox, success=True)

    def test_protocol_security_repair_does_not_mix_new_at_with_stale_session(self):
        """An AT-only password callback must refresh its matching session tuple."""
        from register.protocol import repair_security_one

        registrar = MagicMock()
        fresh_tokens = {
            "access_token": "fresh-access-token",
            "session_token": "fresh-session-token",
            "account_id": "fresh-account-id",
        }

        def complete_password(_email, password, _mailbox, _index, **kwargs):
            kwargs["credential_sink"]({"access_token": "new-access-only"})
            return password

        registrar._complete_post_registration_password.side_effect = complete_password
        registrar._read_chatgpt_session.return_value = fresh_tokens
        partials = []
        mailbox = {
            "address": "new-at-only@example.com",
            "provider": "icloud_api",
        }
        with (
            patch("register.protocol.PlatformRegistrar", return_value=registrar),
            patch("register.protocol.create_mailbox", return_value=mailbox),
            patch("register.protocol._random_password", return_value="generated"),
            patch("register.protocol.mark_mailbox_result"),
            patch("register.protocol.step"),
        ):
            result = repair_security_one(
                1,
                account={
                    "email": "new-at-only@example.com",
                    "password": "",
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                    "mfa_enabled": True,
                    "access_token": "old-access-token",
                    "session_token": "old-session-token",
                    "account_id": "old-account-id",
                },
                mail_config={"providers": []},
                partial_sink=partials.append,
            )

        registrar._read_chatgpt_session.assert_called_once_with(
            "new-at-only@example.com",
            "generated",
            1,
        )
        self.assertEqual(result["access_token"], "fresh-access-token")
        self.assertEqual(result["session_token"], "fresh-session-token")
        self.assertEqual(result["account_id"], "fresh-account-id")

    def test_protocol_security_repair_persists_session_after_password_when_at_missing(self):
        """A password repair with no stored AT must retain the rebuilt session."""
        from register.protocol import repair_security_one

        registrar = MagicMock()
        final_tokens = {
            "access_token": "recovered-access-token",
            "session_token": "recovered-session-token",
            "account_id": "recovered-account-id",
            "plan_type": "free",
        }

        def complete_password(_email, password, _mailbox, _index, **kwargs):
            self.assertTrue(callable(kwargs.get("credential_sink")))
            kwargs["credential_sink"]({"session_token": "password-step-session"})
            return "Aa1!recovered-password"

        registrar._complete_post_registration_password.side_effect = complete_password
        registrar._read_chatgpt_session.return_value = final_tokens
        registrar._enroll_totp.return_value = {
            "mfa_enabled": True,
            "mfa_factor_type": "totp",
            "mfa_factor_id": "recovered-factor-id",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }
        mailbox = {
            "address": "missing-at@example.com",
            "provider": "icloud_api",
        }
        partials = []
        with (
            patch("register.protocol.PlatformRegistrar", return_value=registrar),
            patch("register.protocol.create_mailbox", return_value=mailbox),
            patch("register.protocol._random_password", return_value="generated"),
            patch("register.protocol.mark_mailbox_result"),
            patch("register.protocol.step"),
        ):
            result = repair_security_one(
                1,
                account={
                    "email": "missing-at@example.com",
                    "password": "",
                    "totp_secret": "",
                    "mfa_enabled": False,
                },
                mail_config={"providers": []},
                partial_sink=partials.append,
            )

        self.assertEqual(result["password"], "Aa1!recovered-password")
        self.assertEqual(result["access_token"], "recovered-access-token")
        self.assertEqual(result["session_token"], "recovered-session-token")
        self.assertEqual(result["account_id"], "recovered-account-id")
        self.assertEqual(partials[1]["session_token"], "password-step-session")
        self.assertEqual(
            partials[2],
            {
                "access_token": "recovered-access-token",
                "session_token": "recovered-session-token",
                "account_id": "recovered-account-id",
                "plan_type": "free",
            },
        )
        registrar._read_chatgpt_session.assert_called_once_with(
            "missing-at@example.com",
            "Aa1!recovered-password",
            1,
        )
        registrar._enroll_totp.assert_called_once_with(final_tokens, 1)

    def test_protocol_security_repair_rebuilds_missing_at_for_existing_password(self):
        """When only AT/TOTP are missing, fall back to the mailbox login path."""
        from register.protocol import repair_security_one

        registrar = MagicMock()
        registrar._read_chatgpt_session.side_effect = RuntimeError("session expired")
        recovered_tokens = {
            "access_token": "otp-login-access",
            "session_token": "otp-login-session",
            "account_id": "otp-login-account",
        }
        registrar.login.return_value = recovered_tokens
        registrar._enroll_totp.return_value = {
            "mfa_enabled": True,
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }
        mailbox = {
            "address": "missing-at-login@example.com",
            "provider": "icloud_api",
        }
        partials = []
        with (
            patch("register.protocol.PlatformRegistrar", return_value=registrar),
            patch("register.protocol.create_mailbox", return_value=mailbox),
            patch("register.protocol.mark_mailbox_result"),
            patch("register.protocol.step"),
        ):
            result = repair_security_one(
                1,
                account={
                    "email": "missing-at-login@example.com",
                    "password": "Aa1!existing-password",
                    "password_set": True,
                    "totp_secret": "",
                    "mfa_enabled": False,
                },
                mail_config={"providers": []},
                partial_sink=partials.append,
            )

        registrar.login.assert_called_once_with(
            "missing-at-login@example.com", mailbox, 1
        )
        self.assertEqual(result["access_token"], "otp-login-access")
        self.assertEqual(result["session_token"], "otp-login-session")
        self.assertEqual(partials[0], recovered_tokens)
        registrar._enroll_totp.assert_called_once_with(recovered_tokens, 1)

    def test_protocol_security_repair_refreshes_incomplete_session_before_totp(self):
        """An AT without Account ID/session must be refreshed before MFA enrollment."""
        from register.protocol import repair_security_one

        registrar = MagicMock()
        refreshed_tokens = {
            "access_token": "complete-access-token",
            "session_token": "complete-session-token",
            "account_id": "complete-account-id",
        }
        registrar._read_chatgpt_session.return_value = refreshed_tokens
        registrar._enroll_totp.return_value = {
            "mfa_enabled": True,
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }
        partials = []
        with (
            patch("register.protocol.PlatformRegistrar", return_value=registrar),
            patch("register.protocol.step"),
            patch("register.protocol.mark_mailbox_result"),
        ):
            result = repair_security_one(
                1,
                account={
                    "email": "incomplete-session@example.com",
                    "password": "Aa1!existing-password",
                    "password_set": True,
                    "access_token": "stale-access-token",
                    "session_token": "",
                    "account_id": "",
                    "totp_secret": "",
                    "mfa_enabled": False,
                },
                mail_config={"providers": []},
                partial_sink=partials.append,
            )

        registrar._read_chatgpt_session.assert_called_once_with(
            "incomplete-session@example.com",
            "Aa1!existing-password",
            1,
        )
        registrar._enroll_totp.assert_called_once_with(refreshed_tokens, 1)
        self.assertEqual(result["account_id"], "complete-account-id")
        self.assertEqual(result["session_token"], "complete-session-token")
        self.assertEqual(partials[0], refreshed_tokens)

    def test_protocol_security_repair_refreshes_session_after_remote_mfa_replacement(self):
        """The early remote-factor replacement also persists Account ID/session."""
        from register.protocol import repair_security_one

        registrar = MagicMock()
        registrar._chatgpt_mfa_request.return_value = (
            SimpleNamespace(status_code=200, json=lambda: {"mfa_enabled": True}),
            "",
        )
        registrar._enroll_totp.return_value = {
            "mfa_enabled": True,
            "mfa_factor_id": "replacement-factor",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }
        refreshed_tokens = {
            "access_token": "complete-access-token",
            "session_token": "complete-session-token",
            "account_id": "complete-account-id",
        }
        registrar._read_chatgpt_session.return_value = refreshed_tokens
        partials = []
        with (
            patch("register.protocol.PlatformRegistrar", return_value=registrar),
            patch("register.protocol.step"),
            patch("register.protocol.mark_mailbox_result"),
        ):
            result = repair_security_one(
                1,
                account={
                    "email": "remote-mfa-replacement@example.com",
                    "password": "Aa1!existing-password",
                    "password_set": True,
                    "access_token": "old-access-token",
                    "session_token": "",
                    "account_id": "",
                    "totp_secret": "",
                    "mfa_enabled": True,
                },
                mail_config={"providers": []},
                partial_sink=partials.append,
            )

        registrar._enroll_totp.assert_called_once_with(
            {
                "access_token": "old-access-token",
                "session_token": "",
                "account_id": "",
            },
            1,
            replace_existing=True,
        )
        registrar._read_chatgpt_session.assert_called_once_with(
            "remote-mfa-replacement@example.com",
            "Aa1!existing-password",
            1,
        )
        self.assertEqual(result["account_id"], "complete-account-id")
        self.assertEqual(result["session_token"], "complete-session-token")
        self.assertIn(refreshed_tokens, partials)

    def test_protocol_security_repair_drops_stale_session_after_early_mfa_and_at_rotation(self):
        """An early factor replacement must not pair a rotated AT with old IDs."""
        from register.protocol import repair_security_one

        registrar = MagicMock()
        registrar._enroll_totp.return_value = {
            "mfa_enabled": True,
            "mfa_factor_id": "replacement-factor",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }
        fresh_tokens = {
            "access_token": "rotated-complete-access",
            "session_token": "rotated-complete-session",
            "account_id": "rotated-complete-account",
        }

        def complete_password(_email, password, _mailbox, _index, **kwargs):
            kwargs["credential_sink"]({"access_token": "rotated-at-only"})
            return "Aa1!rotated-password"

        registrar._complete_post_registration_password.side_effect = complete_password
        registrar._read_chatgpt_session.return_value = fresh_tokens
        mailbox = {
            "address": "early-mfa-at-rotation@example.com",
            "provider": "icloud_api",
        }
        partials = []
        with (
            patch("register.protocol.PlatformRegistrar", return_value=registrar),
            patch("register.protocol.create_mailbox", return_value=mailbox),
            patch("register.protocol._random_password", return_value="generated"),
            patch("register.protocol.mark_mailbox_result"),
            patch("register.protocol.step"),
        ):
            result = repair_security_one(
                1,
                account={
                    "email": "early-mfa-at-rotation@example.com",
                    "password": "",
                    "totp_secret": "",
                    "mfa_enabled": True,
                    "access_token": "old-access-token",
                    "session_token": "old-session-token",
                    "account_id": "old-account-id",
                },
                mail_config={"providers": []},
                partial_sink=partials.append,
            )

        registrar._read_chatgpt_session.assert_called_once_with(
            "early-mfa-at-rotation@example.com",
            "Aa1!rotated-password",
            1,
        )
        self.assertEqual(result["access_token"], "rotated-complete-access")
        self.assertEqual(result["session_token"], "rotated-complete-session")
        self.assertEqual(result["account_id"], "rotated-complete-account")
        self.assertIn(fresh_tokens, partials)
        self.assertFalse(
            any(item.get("access_token") == "rotated-at-only" for item in partials)
        )

    def test_protocol_security_repair_reads_session_when_password_callback_has_no_at(self):
        """Password callback completion must not leave an existing-TOTP account tokenless."""
        from register.protocol import repair_security_one

        registrar = MagicMock()
        registrar._complete_post_registration_password.return_value = (
            "Aa1!callback-password"
        )
        callback_tokens = {
            "access_token": "callback-access-token",
            "session_token": "callback-session-token",
            "account_id": "callback-account-id",
        }
        registrar._read_chatgpt_session.return_value = callback_tokens
        mailbox = {
            "address": "callback-no-at@example.com",
            "provider": "icloud_api",
        }
        partials = []
        with (
            patch("register.protocol.PlatformRegistrar", return_value=registrar),
            patch("register.protocol.create_mailbox", return_value=mailbox),
            patch("register.protocol._random_password", return_value="generated"),
            patch("register.protocol.mark_mailbox_result"),
            patch("register.protocol.step"),
        ):
            result = repair_security_one(
                1,
                account={
                    "email": "callback-no-at@example.com",
                    "password": "",
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                    "mfa_enabled": True,
                },
                mail_config={"providers": []},
                partial_sink=partials.append,
            )

        registrar._read_chatgpt_session.assert_called_once_with(
            "callback-no-at@example.com",
            "Aa1!callback-password",
            1,
        )
        self.assertEqual(result["access_token"], "callback-access-token")
        self.assertEqual(result["session_token"], "callback-session-token")
        self.assertEqual(result["account_id"], "callback-account-id")
        self.assertIn(callback_tokens, partials)

    def test_protocol_security_repair_force_confirms_password_totp_and_session(self):
        from register.protocol import repair_security_one

        registrar = MagicMock()
        mailbox = {
            "address": "force-confirm@example.com",
            "provider": "icloud_api",
        }
        final_tokens = {
            "access_token": "fresh-access",
            "session_token": "fresh-session",
            "account_id": "fresh-account",
            "plan_type": "plus",
            "mfa_factor_id": "factor-confirmed",
            "mfa_factor_type": "totp",
        }

        def confirm_password(_email, password, _mailbox, _index, **kwargs):
            kwargs["credential_sink"](final_tokens)
            return password

        registrar._complete_post_registration_password.side_effect = confirm_password
        partials = []
        with (
            patch("register.protocol.PlatformRegistrar", return_value=registrar),
            patch("register.protocol.create_mailbox", return_value=mailbox),
            patch("register.protocol._random_password", return_value="Aa1!new-confirmed"),
            patch("register.protocol.mark_mailbox_result") as mailbox_result,
        ):
            result = repair_security_one(
                1,
                account={
                    "email": "force-confirm@example.com",
                    "password": "Aa1!old-local",
                    "password_set": True,
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                    "mfa_enabled": True,
                },
                mail_config={"providers": []},
                force_password_confirmation=True,
                partial_sink=partials.append,
            )

        self.assertEqual(result["password"], "Aa1!new-confirmed")
        self.assertEqual(result["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(result["access_token"], "fresh-access")
        self.assertEqual(result["plan_type"], "plus")
        self.assertTrue(result["password_verified_at"])
        self.assertTrue(result["totp_verified_at"])
        registrar._complete_post_registration_password.assert_called_once()
        call_kwargs = registrar._complete_post_registration_password.call_args.kwargs
        self.assertEqual(call_kwargs["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertTrue(callable(call_kwargs["credential_sink"]))
        registrar._enroll_totp.assert_not_called()
        mailbox_result.assert_called_once_with(mailbox, success=True)

    def test_browser_security_repair_logs_in_once_and_reads_final_session(self):
        from register.protocol import repair_security_one

        final_access = _jwt(
            {
                "client_id": "app_X8zY6vW2pQ9tR3dE7nK1jL5gH",
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "final-account-id"
                },
            }
        )
        registrar = MagicMock()
        registrar.login.return_value = {
            "access_token": "protocol-login-access",
            "session_token": "protocol-login-session",
            "account_id": "protocol-login-account",
        }
        registrar._complete_post_registration_password.return_value = (
            "Aa1!browser-password"
        )
        registrar._read_chatgpt_session.return_value = {
            "access_token": final_access,
            "session_token": "final-session",
            "account_id": "final-account-id",
            "plan_type": "free",
            "expires_at": "2026-09-01T00:00:00Z",
            "session": {"user": {"email": "browser-repair@example.com"}},
            "cookies": {"next-auth.session-token": "final-cookie"},
            "profile": {"email": "browser-repair@example.com"},
            "oauth_client_id": "app_X8zY6vW2pQ9tR3dE7nK1jL5gH",
        }
        registrar._enroll_totp.return_value = {
            "mfa_enabled": True,
            "mfa_factor_type": "totp",
            "mfa_factor_id": "factor-browser",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }
        mailbox = {
            "address": "browser-repair@example.com",
            "provider": "icloud_api",
            "provider_ref": "icloud_api#browser-repair",
        }
        partials = []
        with (
            patch("register.protocol.PlatformRegistrar", return_value=registrar),
            patch("register.protocol.create_mailbox", return_value=mailbox),
            patch("register.protocol._random_password", return_value="generated"),
            patch("register.protocol.mark_mailbox_result"),
        ):
            result = repair_security_one(
                1,
                account={
                    "email": "browser-repair@example.com",
                    "registration_method": "browser",
                    "browser_type": "camoufox",
                    "access_token": "browser-bootstrap-access",
                    "session_token": "browser-bootstrap-session",
                    "account_id": "browser-bootstrap-account",
                    "password": "",
                    "password_set": False,
                    "totp_secret": "",
                },
                mail_config={"providers": []},
                partial_sink=partials.append,
            )

        registrar.login.assert_called_once_with(
            "browser-repair@example.com", mailbox, 1
        )
        self.assertEqual(registrar._read_chatgpt_session.call_count, 2)
        self.assertEqual(
            registrar._read_chatgpt_session.call_args_list,
            [
                call("browser-repair@example.com", "Aa1!browser-password", 1),
                call("browser-repair@example.com", "Aa1!browser-password", 1),
            ],
        )
        registrar.login_with_password_totp.assert_not_called()
        self.assertEqual(result["access_token"], final_access)
        self.assertEqual(result["session_token"], "final-session")
        self.assertEqual(result["account_id"], "final-account-id")
        self.assertEqual(partials[-1]["access_token"], final_access)

    def test_post_registration_password_skips_existing_password_route(self):
        from register.protocol import PlatformRegistrar

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.proxy = ""
        registrar._start_password_add_authorize = MagicMock(
            return_value="https://auth.openai.com/log-in/password"
        )
        registrar._send_password_reset_otp = MagicMock(return_value={})
        registrar._validate_otp = MagicMock(
            return_value={
                "continue_url": "https://auth.openai.com/reset-password/new-password",
                "page": {"type": "reset_password_new_password"},
            }
        )
        registrar._diagnostic = MagicMock()
        registrar._navigate_auth_step = MagicMock()
        registrar._add_password = MagicMock(
            return_value={
                "continue_url": "https://auth.openai.com/reset-password/success",
                "page": {"type": "reset_password_success"},
            }
        )
        registrar.login_with_password_totp = MagicMock(return_value={})
        mailbox = {"address": "existing-mfa@example.com"}

        with (
            patch("register.protocol.wait_for_code", return_value="123456"),
            patch("register.protocol.mark_mailbox_code_boundary"),
            patch("register.protocol.step"),
        ):
            with self.assertRaisesRegex(RuntimeError, "password_reset_required"):
                registrar._complete_post_registration_password(
                    "existing-mfa@example.com",
                    "Aa1!confirmed-password",
                    mailbox,
                    1,
                    totp_secret="JBSWY3DPEHPK3PXP",
                    skip_existing_password=True,
                )

        registrar._send_password_reset_otp.assert_not_called()
        registrar._validate_otp.assert_not_called()
        registrar._add_password.assert_not_called()
        registrar.login_with_password_totp.assert_not_called()

    def test_protocol_registrar_uses_nextauth_first_without_platform_oauth(self):
        from register.protocol import PlatformRegistrar

        access_token = _jwt(
            {
                "client_id": "app_X8zY6vW2pQ9tR3dE7nK1jL5gH",
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "nextauth-account"
                },
            }
        )
        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.proxy = ""
        registrar.device_id = "device-id"
        registrar.clearance_user_agent = ""
        registrar.session = MagicMock()
        registrar.fingerprint = {
            "impersonate": "chrome142",
            "major": "142",
            "full_version": "142.0.0.0",
            "user_agent": "Mozilla/5.0 Chrome/142.0.0.0",
            "sec_ch_ua": '"Chromium";v="142"',
            "sec_ch_ua_full_version_list": '"Chromium";v="142.0.0.0"',
            "platform_version": "10.0.0",
            "accept_language": "en-US,en;q=0.9",
            "profile_id": "profile-id",
            "screen": "1920x1080",
            "hardware_concurrency": "8",
        }
        registrar._log_identity = MagicMock()
        registrar._platform_authorize = MagicMock(
            side_effect=AssertionError("Platform OAuth must not start registration")
        )
        registrar._complete_chatgpt_session = MagicMock(return_value={})
        registrar._validate_otp = MagicMock(
            return_value={
                "continue_url": "https://auth.openai.com/about-you",
                "page": {"type": "about_you"},
            }
        )
        registrar._navigate_auth_step = MagicMock()
        registrar._start_password_add_authorize = MagicMock()
        registrar._set_registration_password = MagicMock(
            return_value="Aa1!protocol-test"
        )
        registrar._create_account = MagicMock(
            return_value={
                "continue_url": "https://chatgpt.com/api/auth/callback/openai"
            }
        )
        registrar._read_chatgpt_session = MagicMock(
            return_value={
                "access_token": access_token,
                "session_token": "nextauth-session",
                "account_id": "nextauth-account",
                "oauth_client_id": "app_X8zY6vW2pQ9tR3dE7nK1jL5gH",
            }
        )
        registrar._enroll_totp = MagicMock(
            return_value={
                "mfa_enabled": True,
                "mfa_factor_type": "totp",
                "mfa_factor_id": "factor-nextauth",
                "totp_secret": "JBSWY3DPEHPK3PXP",
            }
        )
        mailbox = {
            "address": "fresh@example.com",
            "label": "fixture",
            "provider": "icloud_api",
        }
        with (
            patch("register.protocol.create_mailbox", return_value=mailbox),
            patch(
                "register.protocol.wait_for_code",
                side_effect=["123456", "654321"],
            ),
            patch("register.protocol.mark_mailbox_result"),
            patch("register.protocol.step"),
            patch(
                "register.protocol._random_password",
                return_value="Aa1!protocol-test",
            ),
        ):
            result = registrar.register(1)

        registrar._platform_authorize.assert_not_called()
        registrar._complete_chatgpt_session.assert_called_once_with(
            "fresh@example.com", "", 1, start_only=True
        )
        registrar._set_registration_password.assert_called_once_with(
            "fresh@example.com",
            "Aa1!protocol-test",
            1,
        )
        registrar._create_account.assert_called_once()
        self.assertEqual(
            registrar._read_chatgpt_session.call_args.args[:2],
            ("fresh@example.com", "Aa1!protocol-test"),
        )
        self.assertEqual(
            registrar._navigate_auth_step.call_args_list[-1].args[0],
            "https://chatgpt.com/api/auth/callback/openai",
        )
        self.assertEqual(result["auth_flow"], "chatgpt_nextauth")
        self.assertEqual(result["password"], "Aa1!protocol-test")
        self.assertTrue(result["password_set"])
        self.assertEqual(result["account_id"], "nextauth-account")
        self.assertTrue(result["mfa_enabled"])
        self.assertEqual(result["mfa_factor_id"], "factor-nextauth")
        self.assertEqual(result["totp_secret"], "JBSWY3DPEHPK3PXP")
        registrar._enroll_totp.assert_called_once_with(
            registrar._read_chatgpt_session.return_value,
            1,
        )

    def test_protocol_registration_follows_password_continue_step_before_otp(self):
        from register.protocol import PlatformRegistrar

        access_token = _jwt(
            {
                "client_id": "app_X8zY6vW2pQ9tR3dE7nK1jL5gH",
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "nextauth-account"
                },
            }
        )
        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.proxy = ""
        registrar.device_id = "device-fixture"
        registrar.fingerprint = {}
        registrar._log_identity = MagicMock()
        registrar._complete_chatgpt_session = MagicMock(return_value={})
        registrar._set_registration_password = MagicMock(
            return_value={
                "continue_url": "https://auth.openai.com/api/accounts/email-otp/send",
                "page": {"type": "email_otp_send"},
            }
        )
        registrar._validate_otp = MagicMock(
            return_value={
                "continue_url": "https://auth.openai.com/about-you",
                "page": {"type": "about_you"},
            }
        )
        registrar._diagnostic = MagicMock()
        registrar._navigate_auth_step = MagicMock()
        registrar._create_account = MagicMock(
            return_value={
                "continue_url": "https://chatgpt.com/api/auth/callback/openai"
            }
        )
        registrar._read_chatgpt_session = MagicMock(
            return_value={
                "access_token": access_token,
                "session_token": "nextauth-session",
                "account_id": "nextauth-account",
            }
        )
        registrar._complete_post_registration_password = MagicMock()
        registrar._enroll_totp = MagicMock(
            return_value={
                "mfa_enabled": True,
                "mfa_factor_id": "factor-nextauth",
                "totp_secret": "JBSWY3DPEHPK3PXP",
            }
        )
        mailbox = {
            "address": "continue@example.com",
            "label": "fixture",
            "provider": "icloud_api",
        }

        with (
            patch("register.protocol.create_mailbox", return_value=mailbox),
            patch("register.protocol.wait_for_code", return_value="123456"),
            patch("register.protocol.mark_mailbox_result"),
            patch("register.protocol.step"),
            patch("register.protocol._random_password", return_value="generated"),
        ):
            result = registrar.register(1)

        self.assertEqual(result["password"], "generated")
        self.assertEqual(
            registrar._navigate_auth_step.call_args_list[0].args[0],
            "https://auth.openai.com/api/accounts/email-otp/send",
        )
        registrar._create_account.assert_called_once()
        registrar._complete_post_registration_password.assert_not_called()

    def test_protocol_registration_rejects_existing_mfa_account_before_create(self):
        from register.protocol import PlatformRegistrar

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.proxy = ""
        registrar.device_id = "device-fixture"
        registrar.fingerprint = {}
        registrar._log_identity = MagicMock()
        registrar._complete_chatgpt_session = MagicMock(return_value={})
        registrar._validate_otp = MagicMock(
            return_value={
                "continue_url": "https://auth.openai.com/mfa-challenge/challenge-id",
                "page": {"type": "mfa_challenge"},
            }
        )
        registrar._diagnostic = MagicMock()
        registrar._navigate_auth_step = MagicMock()
        registrar._create_account = MagicMock()
        registrar._set_registration_password = MagicMock(
            return_value="Aa1!existing-test"
        )
        registrar._complete_post_registration_password = MagicMock()
        registrar._read_chatgpt_session = MagicMock()
        registrar._enroll_totp = MagicMock()
        mailbox = {
            "address": "existing@example.com",
            "label": "fixture",
            "provider": "icloud_api",
        }

        with (
            patch("register.protocol.create_mailbox", return_value=mailbox),
            patch("register.protocol.wait_for_code", return_value="123456"),
            patch("register.protocol.mark_mailbox_result") as mailbox_result,
            patch("register.protocol.step"),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "existing_account_mfa_credentials_missing"
            ):
                registrar.register(1)

        registrar._navigate_auth_step.assert_not_called()
        registrar._set_registration_password.assert_called_once_with(
            "existing@example.com",
            unittest.mock.ANY,
            1,
        )
        registrar._create_account.assert_not_called()
        registrar._complete_post_registration_password.assert_not_called()
        registrar._read_chatgpt_session.assert_not_called()
        registrar._enroll_totp.assert_not_called()
        mailbox_result.assert_called_once_with(
            mailbox,
            success=False,
            error=unittest.mock.ANY,
        )

    def test_protocol_registration_callback_still_adds_password(self):
        from register.protocol import PlatformRegistrar

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.proxy = ""
        registrar.device_id = "device-fixture"
        registrar.fingerprint = {}
        registrar._log_identity = MagicMock()
        registrar._complete_chatgpt_session = MagicMock(return_value={})
        registrar._validate_otp = MagicMock(
            return_value={
                "continue_url": "https://chatgpt.com/api/auth/callback/openai",
                "page": {"type": "external_url"},
            }
        )
        registrar._diagnostic = MagicMock()
        registrar._navigate_auth_step = MagicMock()
        registrar._create_account = MagicMock()
        registrar._set_registration_password = MagicMock(
            return_value="generated"
        )
        registrar._complete_post_registration_password = MagicMock(
            return_value="Aa1!callback-test"
        )
        initial_tokens = {
            "access_token": "initial-access-token",
            "account_id": "account-id",
        }
        final_tokens = {
            "access_token": "final-access-token",
            "account_id": "account-id",
        }
        registrar._read_chatgpt_session = MagicMock(
            side_effect=[initial_tokens, final_tokens]
        )
        registrar._enroll_totp = MagicMock(
            return_value={
                "mfa_enabled": True,
                "totp_secret": "JBSWY3DPEHPK3PXP",
            }
        )
        mailbox = {
            "address": "callback@example.com",
            "label": "fixture",
            "provider": "icloud_api",
        }

        with (
            patch("register.protocol.create_mailbox", return_value=mailbox),
            patch("register.protocol.wait_for_code", return_value="123456"),
            patch("register.protocol.mark_mailbox_result"),
            patch("register.protocol._random_password", return_value="generated"),
            patch("register.protocol.step"),
        ):
            result = registrar.register(1)

        registrar._create_account.assert_not_called()
        registrar._set_registration_password.assert_called_once_with(
            "callback@example.com",
            "generated",
            1,
        )
        registrar._complete_post_registration_password.assert_called_once_with(
            "callback@example.com",
            "generated",
            mailbox,
            1,
            skip_existing_password=True,
        )
        self.assertEqual(registrar._read_chatgpt_session.call_count, 2)
        self.assertEqual(
            registrar._read_chatgpt_session.call_args_list[0].args[:2],
            ("callback@example.com", "generated"),
        )
        self.assertEqual(
            registrar._read_chatgpt_session.call_args_list[1].args[:2],
            ("callback@example.com", "Aa1!callback-test"),
        )
        registrar._enroll_totp.assert_called_once_with(final_tokens, 1)
        self.assertEqual(result["password"], "Aa1!callback-test")
        self.assertTrue(result["password_set"])

    def test_protocol_registration_emits_recoverable_security_pending_snapshot(self):
        from register.protocol import PlatformRegistrar

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.proxy = ""
        registrar.device_id = "device-fixture"
        registrar.fingerprint = {}
        registrar._log_identity = MagicMock()
        registrar._complete_chatgpt_session = MagicMock(return_value={})
        registrar._validate_otp = MagicMock(
            return_value={
                "continue_url": "https://auth.openai.com/about-you",
                "page": {"type": "about_you"},
            }
        )
        registrar._diagnostic = MagicMock()
        registrar._navigate_auth_step = MagicMock()
        registrar._create_account = MagicMock(
            return_value={"continue_url": "https://auth.openai.com/about-you"}
        )
        registrar._set_registration_password = MagicMock(
            return_value="Aa1!partial-test"
        )
        registrar._read_chatgpt_session = MagicMock(
            return_value={
                "access_token": _jwt(
                    {
                        "client_id": "app_X8zY6vW2pQ9tR3dE7nK1jL5gH",
                        "https://api.openai.com/auth": {
                            "chatgpt_account_id": "partial-account"
                        },
                    }
                ),
                "session_token": "session-token",
                "account_id": "partial-account",
                "plan_type": "free",
                "cookies": {"session": "cookie"},
            }
        )
        registrar._enroll_totp = MagicMock(
            side_effect=RuntimeError("mfa enrollment interrupted")
        )
        mailbox = {
            "address": "partial@example.com",
            "label": "fixture",
            "provider": "icloud_api",
            "provider_ref": "icloud_api#partial",
        }
        partials = []

        with (
            patch("register.protocol.create_mailbox", return_value=mailbox),
            patch("register.protocol.wait_for_code", return_value="123456"),
            patch("register.protocol.mark_mailbox_result"),
            patch("register.protocol._random_password", return_value="generated"),
            patch("register.protocol.step"),
        ):
            with self.assertRaisesRegex(RuntimeError, "mfa enrollment interrupted"):
                registrar.register(1, partial_sink=partials.append)

        registrar._set_registration_password.assert_called_once_with(
            "partial@example.com",
            "generated",
            1,
        )
        self.assertEqual(len(partials), 2)
        self.assertEqual(partials[0]["email"], "partial@example.com")
        self.assertEqual(partials[0]["password"], "Aa1!partial-test")
        self.assertTrue(partials[0]["password_set"])
        self.assertEqual(partials[0]["account_id"], "")
        self.assertEqual(partials[0]["registration_state"], "security_pending")
        self.assertFalse(partials[0]["mfa_enabled"])
        self.assertEqual(partials[0]["totp_secret"], "")
        self.assertEqual(partials[1]["account_id"], "partial-account")
        self.assertEqual(partials[1]["access_token"], registrar._read_chatgpt_session.return_value["access_token"])

    def test_protocol_registration_saves_password_before_final_session_read(self):
        from register.protocol import PlatformRegistrar

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.proxy = "http://proxy.example:3000"
        registrar.device_id = "device-fixture"
        registrar.fingerprint = {}
        registrar._log_identity = MagicMock()
        registrar._complete_chatgpt_session = MagicMock(return_value={})
        registrar._validate_otp = MagicMock(
            return_value={
                "continue_url": "https://auth.openai.com/about-you",
                "page": {"type": "about_you"},
            }
        )
        registrar._diagnostic = MagicMock()
        registrar._navigate_auth_step = MagicMock()
        registrar._create_account = MagicMock(
            return_value={"continue_url": "https://auth.openai.com/about-you"}
        )
        registrar._set_registration_password = MagicMock(
            return_value="Aa1!saved-before-session"
        )
        registrar._read_chatgpt_session = MagicMock(
            side_effect=RuntimeError(
                "curl: (56) CONNECT tunnel failed, response 403"
            )
        )
        registrar._enroll_totp = MagicMock()
        mailbox = {
            "address": "late-proxy-failure@example.com",
            "label": "fixture",
            "provider": "icloud_api",
            "provider_ref": "icloud_api#partial",
        }
        partials = []

        with (
            patch("register.protocol.create_mailbox", return_value=mailbox),
            patch("register.protocol.wait_for_code", return_value="123456"),
            patch("register.protocol.mark_mailbox_result") as mailbox_result,
            patch("register.protocol._random_password", return_value="generated"),
            patch("register.protocol.step"),
        ):
            with self.assertRaisesRegex(RuntimeError, "CONNECT tunnel failed"):
                registrar.register(1, partial_sink=partials.append)

        registrar._set_registration_password.assert_called_once_with(
            "late-proxy-failure@example.com",
            "generated",
            1,
        )
        self.assertEqual(len(partials), 1)
        self.assertEqual(partials[0]["email"], "late-proxy-failure@example.com")
        self.assertEqual(partials[0]["password"], "Aa1!saved-before-session")
        self.assertTrue(partials[0]["password_set"])
        self.assertEqual(partials[0]["registration_state"], "security_pending")
        registrar._enroll_totp.assert_not_called()
        mailbox_result.assert_called_once_with(
            mailbox,
            success=False,
            error=unittest.mock.ANY,
        )

    def test_create_account_returns_recoverable_auth_step_error(self):
        from register.protocol import PlatformRegistrar

        payload = {
            "error": {
                "message": "Invalid authorization step.",
                "code": "invalid_auth_step",
            }
        }
        response = SimpleNamespace(
            status_code=400,
            headers={},
            url="https://auth.openai.com/api/accounts/create_account",
            text=json.dumps(payload),
            json=lambda: payload,
        )
        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.proxy = ""
        registrar.session = MagicMock()
        registrar.device_id = "device-fixture"
        registrar.fingerprint = {}
        registrar.clearance_user_agent = ""

        with (
            patch(
                "register.protocol.build_sentinel_with_so_token",
                return_value=("sentinel", "so-token", "oai-sc"),
            ),
            patch(
                "register.protocol.request_with_local_retry",
                return_value=(response, ""),
            ),
            patch("register.protocol._set_oai_sc_cookie"),
            patch("register.protocol.step"),
        ):
            result = registrar._create_account("Fixture User", "1990-01-01", 1)

        self.assertEqual(result, payload)

    def test_protocol_password_add_uses_har_request_shape(self):
        from register.protocol import PlatformRegistrar

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.proxy = ""
        registrar.session = MagicMock()
        registrar.device_id = "device-id"
        registrar.fingerprint = {}
        registrar.clearance_user_agent = ""
        response = SimpleNamespace(
            status_code=200,
            headers={},
            json=lambda: {
                "continue_url": "https://chatgpt.com/api/auth/callback/openai"
            },
        )
        with (
            patch("register.protocol.build_sentinel_token", return_value="sentinel"),
            patch(
                "register.protocol.request_with_local_retry",
                return_value=(response, ""),
            ) as request,
            patch("register.protocol.step"),
        ):
            result = registrar._add_password("Aa1!protocol-test", 1)

        self.assertIn("continue_url", result)
        request.assert_called_once()
        self.assertEqual(request.call_args.args[1], "post")
        self.assertEqual(
            request.call_args.args[2],
            "https://auth.openai.com/api/accounts/password/add",
        )
        self.assertEqual(request.call_args.kwargs["json"], {"password": "Aa1!protocol-test"})
        self.assertEqual(
            len(request.call_args.kwargs["headers"]["x-access-flow-invocation-id"]),
            36,
        )
        self.assertEqual(
            request.call_args.kwargs["headers"]["referer"],
            "https://auth.openai.com/reset-password/new-password",
        )
        self.assertEqual(
            request.call_args.kwargs["headers"]["openai-sentinel-token"],
            "sentinel",
        )

    def test_protocol_password_reset_uses_har_request_shape(self):
        from register.protocol import PlatformRegistrar

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.proxy = ""
        registrar.session = MagicMock()
        registrar.device_id = "device-id"
        registrar.fingerprint = {}
        registrar.clearance_user_agent = ""
        response = SimpleNamespace(
            status_code=200,
            headers={},
            json=lambda: {
                "continue_url": "https://auth.openai.com/reset-password/success"
            },
        )
        with (
            patch("register.protocol.build_sentinel_token", return_value="sentinel"),
            patch(
                "register.protocol.request_with_local_retry",
                return_value=(response, ""),
            ) as request,
            patch("register.protocol.step"),
        ):
            result = registrar._add_password(
                "Aa1!protocol-test",
                1,
                reset_existing=True,
            )

        self.assertIn("continue_url", result)
        self.assertEqual(
            request.call_args.args[2],
            "https://auth.openai.com/api/accounts/password/reset",
        )
        self.assertEqual(
            request.call_args.kwargs["json"],
            {"password": "Aa1!protocol-test"},
        )

    def test_protocol_password_reset_otp_uses_har_request_shape(self):
        from register.protocol import PlatformRegistrar

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.proxy = ""
        registrar.session = MagicMock()
        registrar.device_id = "device-id"
        registrar.fingerprint = {}
        registrar.clearance_user_agent = ""
        response = SimpleNamespace(
            status_code=200,
            headers={},
            json=lambda: {
                "continue_url": "https://auth.openai.com/reset-password"
            },
        )
        with (
            patch(
                "register.protocol.request_with_local_retry",
                return_value=(response, ""),
            ) as request,
            patch("register.protocol.step"),
        ):
            result = registrar._send_password_reset_otp(1)

        self.assertIn("continue_url", result)
        request.assert_called_once()
        self.assertEqual(request.call_args.args[1], "post")
        self.assertEqual(
            request.call_args.args[2],
            "https://auth.openai.com/api/accounts/password/send-otp",
        )
        self.assertNotIn("json", request.call_args.kwargs)
        self.assertEqual(
            request.call_args.kwargs["headers"]["referer"],
            "https://auth.openai.com/log-in/password",
        )
        self.assertEqual(
            len(request.call_args.kwargs["headers"]["x-access-flow-invocation-id"]),
            36,
        )

    def test_protocol_password_reset_otp_accepts_forgot_password_referer(self):
        from register.protocol import PlatformRegistrar

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.proxy = ""
        registrar.session = MagicMock()
        registrar.device_id = "device-id"
        registrar.fingerprint = {}
        registrar.clearance_user_agent = ""
        response = SimpleNamespace(
            status_code=200,
            headers={},
            json=lambda: {"continue_url": "https://auth.openai.com/reset-password"},
        )
        with (
            patch(
                "register.protocol.request_with_local_retry",
                return_value=(response, ""),
            ) as request,
            patch("register.protocol.step"),
        ):
            registrar._send_password_reset_otp(1, referer_path="/reset-password")

        self.assertEqual(
            request.call_args.kwargs["headers"]["referer"],
            "https://auth.openai.com/reset-password",
        )

    def test_protocol_password_add_authorize_uses_nextauth_har_sequence(self):
        from register.protocol import PlatformRegistrar

        def response(payload, status=200, url="https://chatgpt.com/"):
            return SimpleNamespace(
                status_code=status,
                headers={},
                url=url,
                json=lambda: payload,
            )

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.proxy = ""
        registrar.session = MagicMock()
        registrar.device_id = "device-id"
        registrar.fingerprint = {}
        registrar.clearance_user_agent = ""
        navigate = MagicMock(
            return_value=response(
                {}, url="https://auth.openai.com/email-verification"
            )
        )
        registrar._navigate_auth_step = navigate
        responses = [
            (response({}), ""),
            (response({"csrfToken": "csrf-fixture"}), ""),
            (response({"url": "https://auth.openai.com/authorize?password=1"}), ""),
        ]
        with (
            patch(
                "register.protocol.request_with_local_retry",
                side_effect=responses,
            ) as request,
            patch("register.protocol.step"),
        ):
            registrar._start_password_add_authorize("user@example.com", 1)

        self.assertEqual(
            [call.args[1] for call in request.call_args_list],
            ["get", "get", "post"],
        )
        self.assertEqual(
            [call.args[2].split("?", 1)[0] for call in request.call_args_list],
            [
                "https://chatgpt.com/",
                "https://chatgpt.com/api/auth/csrf",
                "https://chatgpt.com/api/auth/signin/openai",
            ],
        )
        query = request.call_args_list[2].args[2].split("?", 1)[1]
        self.assertNotIn("prompt=login", query)
        self.assertNotIn("screen_hint=login_or_signup", query)
        self.assertIn("login_hint=user%40example.com", query)
        self.assertIn("connection=password", query)
        self.assertIn("post_login_add_password=true", query)
        self.assertIn("reauth=password", query)
        self.assertEqual(navigate.call_count, 1)

    def test_protocol_auth_error_url_decodes_rate_limit_details(self):
        from register.protocol import _auth_error_details_from_url

        payload = base64.b64encode(
            json.dumps(
                {
                    "kind": "AuthApiFailure",
                    "errorCode": "rate_limit_exceeded",
                    "requestId": "request-fixture",
                }
            ).encode()
        ).decode()
        payload = (
            payload.replace("+", "%2B")
            .replace("/", "%2F")
            .replace("=", "%3D")
        )

        details = _auth_error_details_from_url(
            f"https://auth.openai.com/error?payload={payload}"
        )

        self.assertEqual(details["code"], "rate_limit_exceeded")
        self.assertEqual(details["request_id"], "request-fixture")

    def test_protocol_auth_navigation_rejects_http_200_error_page(self):
        from register.protocol import PlatformRegistrar

        payload = base64.b64encode(
            json.dumps({"errorCode": "rate_limit_exceeded"}).encode()
        ).decode()
        payload = (
            payload.replace("+", "%2B")
            .replace("/", "%2F")
            .replace("=", "%3D")
        )
        response = SimpleNamespace(
            status_code=200,
            headers={},
            url=f"https://auth.openai.com/error?payload={payload}",
            json=lambda: {},
        )
        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.proxy = ""
        registrar.session = MagicMock()
        registrar.clearance_user_agent = ""
        registrar._navigate_headers = MagicMock(return_value={})

        with (
            patch(
                "register.protocol.request_with_local_retry",
                return_value=(response, ""),
            ),
            patch("register.protocol._is_cloudflare_challenge", return_value=False),
            patch("register.protocol.step") as protocol_step,
        ):
            with self.assertRaisesRegex(RuntimeError, "rate_limit_exceeded"):
                registrar._navigate_auth_step(
                    "https://auth.openai.com/authorize",
                    "https://chatgpt.com/",
                    1,
                )

        self.assertTrue(
            any(
                "认证步骤进入错误页" in str(call.args[1])
                for call in protocol_step.call_args_list
                if len(call.args) > 1
            )
        )

    def test_protocol_registration_password_response_error_fails_before_account_record(self):
        from register.protocol import PlatformRegistrar

        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar._register_user = MagicMock(
            return_value={"error": {"code": "invalid_password"}}
        )

        with self.assertRaisesRegex(RuntimeError, "set_password_failed: invalid_password"):
            registrar._set_registration_password(
                "fresh@example.com",
                "Aa1!protocol-test",
                1,
            )

        registrar._register_user.assert_called_once_with(
            "fresh@example.com",
            "Aa1!protocol-test",
            1,
        )

    def test_protocol_result_rejects_platform_oauth_identity(self):
        from register.protocol import _validate_protocol_account_result

        platform_token = _jwt(
            {
                "client_id": "app_2SKx67EdpoN0G6j64rFvigXD",
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "account-id"
                },
            }
        )
        with self.assertRaisesRegex(RuntimeError, "wrong_oauth_client"):
            _validate_protocol_account_result(
                {
                    "access_token": platform_token,
                    "session_token": "session-token",
                    "account_id": "account-id",
                }
            )

    def test_protocol_result_requires_password_and_totp(self):
        from register.protocol import _validate_protocol_account_result

        access_token = _jwt(
            {
                "client_id": "app_X8zY6vW2pQ9tR3dE7nK1jL5gH",
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "account-id"
                },
            }
        )
        base = {
            "access_token": access_token,
            "session_token": "session-token",
            "account_id": "account-id",
        }
        incomplete_variants = [
            {
                **base,
                "password_set": True,
                "mfa_enabled": True,
                "totp_secret": "JBSWY3DPEHPK3PXP",
            },
            {
                **base,
                "password": "Aa1!fixture",
                "password_set": True,
                "mfa_enabled": True,
            },
            {
                **base,
                "password": "Aa1!fixture",
                "password_set": False,
                "mfa_enabled": True,
                "totp_secret": "JBSWY3DPEHPK3PXP",
            },
            {
                **base,
                "password": "Aa1!fixture",
                "password_set": True,
                "mfa_enabled": False,
                "totp_secret": "JBSWY3DPEHPK3PXP",
            },
        ]
        for result in incomplete_variants:
            with self.subTest(result=result):
                with self.assertRaisesRegex(
                    RuntimeError, "protocol_registration_incomplete"
                ):
                    _validate_protocol_account_result(result)

    def test_protocol_registration_defers_remote_health_check_by_default(self):
        from register.protocol import register_one

        registrar = MagicMock()
        registrar.register.return_value = {
            "email": "new@example.com",
            "password": "Aa1!fixture",
            "password_set": True,
            "mfa_enabled": True,
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "access_token": _jwt(
                {
                    "client_id": "app_X8zY6vW2pQ9tR3dE7nK1jL5gH",
                    "https://api.openai.com/auth": {
                        "chatgpt_account_id": "account-id"
                    },
                }
            ),
            "refresh_token": "refresh-token",
            "session_token": "session-token",
            "account_id": "account-id",
        }
        with (
            patch("register.protocol.PlatformRegistrar", return_value=registrar),
            patch("register.protocol._append_account") as persist,
            patch("register.protocol.check_account") as health,
        ):
            result = register_one(1, mail_config={})

        self.assertEqual(result["health_status"], "unknown")
        persist.assert_called_once()
        health.assert_not_called()
        registrar.close.assert_called_once_with()

    def test_protocol_registration_rejects_incomplete_identity_before_persisting(self):
        from register.protocol import register_one

        registrar = MagicMock()
        registrar.register.return_value = {
            "email": "incomplete@example.com",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
        }
        with (
            patch("register.protocol.PlatformRegistrar", return_value=registrar),
            patch("register.protocol._append_account") as persist,
            patch("register.protocol.check_account") as health,
        ):
            result = register_one(1, mail_config={})

        self.assertIsNone(result)
        persist.assert_not_called()
        health.assert_not_called()
        registrar.close.assert_called_once_with()

    def test_protocol_completes_nextauth_session_and_requires_account_id(self):
        from register.protocol import PlatformRegistrar

        access_token = _jwt(
            {
                "exp": int(time.time()) + 3600,
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "account-nextauth",
                    "chatgpt_plan_type": "free",
                },
            }
        )

        def response(status=200, payload=None, url="", headers=None):
            item = MagicMock()
            item.status_code = status
            item.url = url
            item.headers = headers or {}
            item.text = ""
            item.json.return_value = payload or {}
            return item

        session = MagicMock()
        session.cookies.jar = [
            type(
                "Cookie",
                (),
                {
                    "name": "__Secure-next-auth.session-token",
                    "value": "nextauth-session-token",
                },
            )()
        ]
        session.request.side_effect = [
            response(payload={"csrfToken": "csrf-token"}),
            response(payload={"url": "https://auth.openai.com/authorize"}),
            response(url="https://chatgpt.com/"),
            response(
                payload={
                    "accessToken": access_token,
                    "account": {"id": "account-nextauth", "planType": "free"},
                    "user": {"email": "nextauth@example.com"},
                }
            ),
            response(payload={"email": "nextauth@example.com"}),
        ]
        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.session = session
        registrar.proxy = ""
        registrar.clearance_user_agent = ""
        registrar.device_id = "device-id"
        registrar.fingerprint = {
            "impersonate": "chrome142",
            "major": "142",
            "full_version": "142.0.0.0",
            "user_agent": "Mozilla/5.0 Chrome/142.0.0.0",
            "sec_ch_ua": '"Chromium";v="142"',
            "sec_ch_ua_full_version_list": '"Chromium";v="142.0.0.0"',
            "platform_version": "10.0.0",
            "accept_language": "en-US,en;q=0.9",
        }

        with patch("register.protocol.step"):
            result = registrar._complete_chatgpt_session(
                "nextauth@example.com", "Password1!", 1
            )

        self.assertEqual(result["account_id"], "account-nextauth")
        self.assertEqual(result["session_token"], "nextauth-session-token")
        self.assertEqual(result["access_token"], access_token)
        signin_url = session.request.call_args_list[1].args[1]
        self.assertIn("connection=password", signin_url)
        self.assertIn("max_age=0", signin_url)
        me_request = session.request.call_args_list[-1]
        self.assertEqual(me_request.args[:2], ("GET", "https://chatgpt.com/backend-api/me"))
        self.assertEqual(
            me_request.kwargs["headers"]["authorization"],
            f"Bearer {access_token}",
        )

    def test_protocol_nextauth_authorize_refreshes_cloudflare_clearance(self):
        from register.protocol import PlatformRegistrar

        def response(status=200, payload=None, url="", headers=None, text=""):
            item = MagicMock()
            item.status_code = status
            item.url = url
            item.headers = headers or {}
            item.text = text
            item.json.return_value = payload or {}
            return item

        challenge = response(
            status=403,
            url="https://auth.openai.com/api/accounts/authorize",
            text="<title>Just a moment...</title>",
        )
        success = response(
            status=302,
            url="https://auth.openai.com/log-in/password",
        )
        session = MagicMock()
        session.request.side_effect = [
            response(payload={"csrfToken": "csrf-token"}),
            response(payload={"url": "https://auth.openai.com/api/accounts/authorize"}),
            challenge,
            success,
        ]
        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.session = session
        registrar.proxy = ""
        registrar.clearance_user_agent = ""
        registrar.clearance_failure_reason = ""
        registrar.device_id = "device-id"
        registrar.fingerprint = {
            "impersonate": "chrome142",
            "major": "142",
            "full_version": "142.0.0.0",
            "user_agent": "Mozilla/5.0 Chrome/142.0.0.0",
            "sec_ch_ua": '"Chromium";v="142"',
            "sec_ch_ua_full_version_list": '"Chromium";v="142.0.0.0"',
            "platform_version": "10.0.0",
            "accept_language": "en-US,en;q=0.9",
        }
        registrar._refresh_cloudflare_clearance = MagicMock(return_value=object())

        with (
            patch(
                "register.protocol._headers_with_clearance",
                side_effect=lambda headers, *_args, **_kwargs: headers,
            ),
            patch("register.protocol.step"),
        ):
            result = registrar._complete_chatgpt_session(
                "existing@example.com",
                "Password1!",
                1,
                start_only=True,
            )

        self.assertEqual(result["final_url"], "https://auth.openai.com/log-in/password")
        registrar._refresh_cloudflare_clearance.assert_called_once()
        self.assertEqual(session.request.call_count, 4)
        self.assertEqual(
            session.request.call_args_list[3].kwargs["allow_redirects"],
            True,
        )

    def test_protocol_nextauth_registration_keeps_email_otp_route(self):
        from register.protocol import PlatformRegistrar

        def response(status=200, payload=None, url="", headers=None):
            item = MagicMock()
            item.status_code = status
            item.url = url
            item.headers = headers or {}
            item.text = ""
            item.json.return_value = payload or {}
            return item

        session = MagicMock()
        session.request.side_effect = [
            response(payload={"csrfToken": "csrf-token"}),
            response(payload={"url": "https://auth.openai.com/authorize"}),
            response(url="https://auth.openai.com/email-verification"),
        ]
        registrar = PlatformRegistrar.__new__(PlatformRegistrar)
        registrar.session = session
        registrar.proxy = ""
        registrar.clearance_user_agent = ""
        registrar.device_id = "device-id"
        registrar.fingerprint = {
            "impersonate": "chrome142",
            "major": "142",
            "full_version": "142.0.0.0",
            "user_agent": "Mozilla/5.0 Chrome/142.0.0.0",
            "sec_ch_ua": '"Chromium";v="142"',
            "sec_ch_ua_full_version_list": '"Chromium";v="142.0.0.0"',
            "platform_version": "10.0.0",
            "accept_language": "en-US,en;q=0.9",
        }

        with patch("register.protocol.step"):
            result = registrar._complete_chatgpt_session(
                "new@example.com", "", 1, start_only=True
            )

        signin_url = session.request.call_args_list[1].args[1]
        self.assertNotIn("connection=password", signin_url)
        self.assertNotIn("max_age=0", signin_url)
        self.assertEqual(
            result["final_url"],
            "https://auth.openai.com/email-verification",
        )

    def test_protocol_engine_routes_without_starting_playwright(self):
        from register.batch import run_register
        import register.protocol

        expected = [{"email": "protocol@example.com", "access_token": "token"}]
        pause_check = lambda: False
        with patch(
            "register.protocol.run_protocol_register",
            new=AsyncMock(return_value=expected),
        ) as run_protocol:
            result = asyncio.run(
                run_register(
                    {"registration": {"engine": "protocol"}},
                    group_id="grp_test",
                    pause_check=pause_check,
                )
            )
        self.assertEqual(result, expected)
        run_protocol.assert_awaited_once()
        self.assertEqual(run_protocol.await_args.kwargs["group_id"], "grp_test")
        self.assertIs(run_protocol.await_args.kwargs["pause_check"], pause_check)

    def test_protocol_pause_blocks_worker_until_resumed(self):
        from register.protocol import run_protocol_register

        state = {"paused": True}
        events = []
        register = MagicMock(
            return_value={"email": "resumed@example.com", "access_token": "token"}
        )

        async def scenario():
            task = asyncio.create_task(
                run_protocol_register(
                    {
                        "registration": {
                            "engine": "protocol",
                            "max_transient_retries": 0,
                        },
                        "mail": {},
                        "proxy": {},
                    },
                    total=1,
                    concurrency=1,
                    pause_check=lambda: state["paused"],
                    task_event=lambda index, status, **fields: events.append(
                        (index, status, fields.get("step"))
                    ),
                )
            )
            await asyncio.sleep(0.05)
            self.assertFalse(register.called)
            state["paused"] = False
            return await asyncio.wait_for(task, timeout=2)

        with (
            patch("mail.release_stale_in_use", return_value=0),
            patch(
                "mail.count_pool_capacity",
                return_value={"available": 1, "total": 1},
            ),
            patch("register.protocol.resolve_proxy_pool", return_value=[]),
            patch("register.protocol.register_one", register),
        ):
            result = asyncio.run(scenario())

        self.assertEqual(result[0]["email"], "resumed@example.com")
        self.assertTrue(any(status == "paused" for _, status, _ in events))

    def test_protocol_batch_does_not_retry_password_reset_skips(self):
        from register.protocol import run_protocol_register

        events = []

        def skip_register(index, **kwargs):
            kwargs["task_event"](
                index,
                "skipped",
                skip_reason="password_reset_required",
                step="账号已有密码，已跳过",
            )
            return None

        async def scenario():
            return await run_protocol_register(
                {
                    "registration": {
                        "engine": "protocol",
                        "max_transient_retries": 3,
                    },
                    "mail": {},
                    "proxy": {},
                },
                total=1,
                concurrency=1,
                task_event=lambda index, status, **fields: events.append(
                    (index, status, fields.get("skip_reason"))
                ),
            )

        with (
            patch("mail.release_stale_in_use", return_value=0),
            patch("mail.count_pool_capacity", return_value={"available": 1, "total": 1}),
            patch("register.protocol.resolve_proxy_pool", return_value=[]),
            patch("register.protocol.register_one", side_effect=skip_register) as register,
        ):
            result = asyncio.run(scenario())

        self.assertEqual(result, [])
        register.assert_called_once()
        self.assertIn((1, "skipped", "password_reset_required"), events)

    def test_protocol_batch_dead_slots_do_not_consume_retry_budget(self):
        from register.protocol import run_protocol_register

        emails = [
            "dead1@icloud.com",
            "dead2@icloud.com",
            "dead3@icloud.com",
            "healthy+splitabc@icloud.com",
        ]
        calls = []

        def registered(index, **kwargs):
            fixed = kwargs["fixed_email"]
            calls.append(fixed)
            if fixed.startswith("dead"):
                kwargs["task_event"](
                    index,
                    "failed",
                    error=(
                        'user_register_http_400, detail={"error": '
                        '{"code": "invalid_auth_step"}}'
                    ),
                )
                return None
            return {"email": fixed, "access_token": "token"}

        with (
            patch("mail.release_stale_in_use", return_value=0),
            patch(
                "mail.count_pool_capacity",
                return_value={
                    "available": len(emails),
                    "total": 4,
                    "total_slots": 8,
                    "emails": emails,
                },
            ),
            patch("register.protocol.resolve_proxy_pool", return_value=[]),
            patch("register.protocol.register_one", side_effect=registered) as register,
        ):
            result = asyncio.run(
                run_protocol_register(
                    {
                        "registration": {
                            "engine": "protocol",
                            "max_transient_retries": 0,
                        },
                        "mail": {},
                        "proxy": {},
                    },
                    total=1,
                    concurrency=1,
                )
            )

        # max_transient_retries=0 would normally stop after the first failure,
        # but remote-owned slots retire as used and the task keeps switching
        # until it lands on a healthy slot.
        self.assertEqual(
            calls,
            [
                "dead1@icloud.com",
                "dead2@icloud.com",
                "dead3@icloud.com",
                "healthy+splitabc@icloud.com",
            ],
        )
        register.assert_called()
        self.assertEqual(result[0]["email"], "healthy+splitabc@icloud.com")

    def test_protocol_batch_transient_errors_still_consume_retry_budget(self):
        from register.protocol import run_protocol_register

        def registered(index, **kwargs):
            kwargs["task_event"](index, "failed", error="otp_wait_timeout")
            return None

        with (
            patch("mail.release_stale_in_use", return_value=0),
            patch(
                "mail.count_pool_capacity",
                return_value={
                    "available": 1,
                    "total": 1,
                    "total_slots": 2,
                    "emails": ["only@icloud.com"],
                },
            ),
            patch("register.protocol.resolve_proxy_pool", return_value=[]),
            patch("register.protocol.register_one", side_effect=registered) as register,
        ):
            result = asyncio.run(
                run_protocol_register(
                    {
                        "registration": {
                            "engine": "protocol",
                            "max_transient_retries": 0,
                        },
                        "mail": {},
                        "proxy": {},
                    },
                    total=1,
                    concurrency=1,
                )
            )

        register.assert_called_once()
        self.assertEqual(result, [])

    def test_protocol_batch_pins_distinct_primary_and_split_slots(self):
        from register.protocol import run_protocol_register

        emails = [
            "first@icloud.com",
            "second@icloud.com",
            "first+splitabc@icloud.com",
        ]

        def registered(index, **kwargs):
            return {
                "email": kwargs["fixed_email"],
                "access_token": f"token-{index}",
            }

        with (
            patch("mail.release_stale_in_use", return_value=0),
            patch(
                "mail.count_pool_capacity",
                return_value={
                    "available": len(emails),
                    "total": 2,
                    "total_slots": 4,
                    "emails": emails,
                },
            ),
            patch("register.protocol.resolve_proxy_pool", return_value=[]),
            patch("register.protocol.register_one", side_effect=registered) as register,
        ):
            result = asyncio.run(
                run_protocol_register(
                    {
                        "registration": {
                            "engine": "protocol",
                            "max_transient_retries": 0,
                        },
                        "mail": {},
                        "proxy": {},
                    },
                    total=3,
                    concurrency=3,
                )
            )

        assigned = {
            int(call.args[0]): call.kwargs["fixed_email"]
            for call in register.call_args_list
        }
        self.assertEqual([assigned[index] for index in sorted(assigned)], emails)
        self.assertEqual([item["email"] for item in result], emails)

    def test_protocol_alias_pairs_wait_for_both_complete_before_next_pair(self):
        from register.protocol import run_protocol_register

        emails = [
            "first+splitone@icloud.com",
            "first+splittwo@icloud.com",
            "second+splitone@icloud.com",
            "second+splittwo@icloud.com",
        ]
        started: dict[str, float] = {}
        finished: dict[str, float] = {}
        lock = threading.Lock()

        def registered(index, **kwargs):
            email = kwargs["fixed_email"]
            with lock:
                started[email] = time.monotonic()
            if email.startswith("first+"):
                time.sleep(0.04 if email.endswith("one@icloud.com") else 0.08)
            with lock:
                finished[email] = time.monotonic()
            return {"email": email, "access_token": f"token-{index}"}

        with (
            patch("mail.release_stale_in_use", return_value=0),
            patch(
                "mail.count_pool_capacity",
                return_value={
                    "available": len(emails),
                    "total": 2,
                    "emails": emails,
                },
            ),
            patch("register.protocol.resolve_proxy_pool", return_value=[]),
            patch("register.protocol.register_one", side_effect=registered),
        ):
            result = asyncio.run(
                run_protocol_register(
                    {
                        "registration": {
                            "engine": "protocol",
                            "max_transient_retries": 0,
                        },
                        "mail": {},
                        "proxy": {},
                    },
                    total=4,
                    concurrency=10,
                )
            )

        first_pair = emails[:2]
        second_pair = emails[2:]
        self.assertEqual({item["email"] for item in result}, set(emails))
        self.assertLess(abs(started[first_pair[0]] - started[first_pair[1]]), 0.1)
        self.assertLess(abs(started[second_pair[0]] - started[second_pair[1]]), 0.1)
        self.assertGreaterEqual(
            min(started[email] for email in second_pair),
            max(finished[email] for email in first_pair),
        )

    def test_protocol_fixed_primary_starts_one_alias(self):
        from register.protocol import run_protocol_register

        aliases = ["double+splitone@icloud.com"]
        entered: list[tuple[str, float]] = []
        all_entered = threading.Event()
        entered_lock = threading.Lock()

        def registered(index, **kwargs):
            with entered_lock:
                entered.append((kwargs["fixed_email"], time.monotonic()))
                if len(entered) == len(aliases):
                    all_entered.set()
            return {"email": kwargs["fixed_email"], "access_token": f"token-{index}"}

        with (
            patch("mail.release_stale_in_use", return_value=0),
            patch(
                "mail.count_pool_capacity",
                return_value={
                    "available": len(aliases),
                    "total": 1,
                    "fixed_email_mode": "alias_batch",
                    "emails": aliases,
                },
            ) as capacity,
            patch("register.protocol.resolve_proxy_pool", return_value=[]),
            patch("register.protocol.register_one", side_effect=registered),
        ):
            result = asyncio.run(
                run_protocol_register(
                    {
                        "registration": {
                            "engine": "protocol",
                            "max_transient_retries": 0,
                        },
                        "mail": {},
                        "proxy": {},
                    },
                    total=1,
                    concurrency=8,
                    fixed_email="double@icloud.com",
                )
            )

        self.assertEqual({item["email"] for item in result}, set(aliases))
        self.assertEqual(capacity.call_args.kwargs["fixed_alias_count"], 1)
        self.assertTrue(all_entered.is_set())
        self.assertEqual(len(entered), 1)

    def test_protocol_fixed_alias_batch_does_not_repeat_auth_state_failure(self):
        from register.protocol import run_protocol_register

        aliases = [
            "double+splitone@icloud.com",
        ]

        def failed(index, **kwargs):
            kwargs["task_event"](
                index,
                "failed",
                error="validate_otp_http_400: invalid_auth_step",
                step="OTP transaction expired",
            )
            return None

        with (
            patch("mail.release_stale_in_use", return_value=0),
            patch(
                "mail.count_pool_capacity",
                return_value={
                    "available": 1,
                    "total": 1,
                    "fixed_email_mode": "alias_batch",
                    "emails": aliases,
                },
            ),
            patch("register.protocol.resolve_proxy_pool", return_value=[]),
            patch("register.protocol.register_one", side_effect=failed) as register,
        ):
            result = asyncio.run(
                run_protocol_register(
                    {
                        "registration": {
                            "engine": "protocol",
                            "max_transient_retries": 3,
                        },
                        "mail": {},
                        "proxy": {},
                    },
                    total=2,
                    concurrency=2,
                    fixed_email="double@icloud.com",
                    task_event=lambda *_args, **_kwargs: None,
                )
            )

        self.assertEqual(result, [])
        self.assertEqual(register.call_count, 1)
        self.assertEqual(
            {call.kwargs["fixed_email"] for call in register.call_args_list},
            set(aliases),
        )

    def test_protocol_fixed_alias_batch_retries_curl_28_with_new_proxies(self):
        from register.protocol import run_protocol_register

        aliases = [
            "double+splitone@icloud.com",
        ]
        calls: dict[int, list[tuple[str, str]]] = {1: []}

        def register(index, **kwargs):
            calls[index].append((kwargs["fixed_email"], kwargs.get("proxy") or ""))
            if len(calls[index]) == 1:
                kwargs["task_event"](
                    index,
                    "running",
                    error=(
                        "Failed to perform, curl: (28) Operation timed out "
                        "after 30000 milliseconds with 0 bytes received"
                    ),
                    step="代理响应超时",
                )
                return None
            return {"email": kwargs["fixed_email"], "access_token": f"token-{index}"}

        with (
            patch("mail.release_stale_in_use", return_value=0),
            patch(
                "mail.count_pool_capacity",
                return_value={
                    "available": 1,
                    "total": 1,
                    "fixed_email_mode": "alias_batch",
                    "emails": aliases,
                },
            ),
            patch(
                "register.protocol.resolve_proxy_pool",
                return_value=["proxy-a", "proxy-b", "proxy-c"],
            ),
            patch("register.protocol.register_one", side_effect=register),
            patch("register.protocol.asyncio.sleep", return_value=None),
        ):
            result = asyncio.run(
                run_protocol_register(
                    {
                        "registration": {
                            "engine": "protocol",
                            "max_transient_retries": 0,
                        },
                        "mail": {},
                        "proxy": {"enabled": True, "rotate_per_task": True},
                    },
                    total=2,
                    concurrency=2,
                    fixed_email="double@icloud.com",
                    task_event=lambda *_args, **_kwargs: None,
                )
            )

        self.assertEqual({item["email"] for item in result}, set(aliases))
        for index, alias in enumerate(aliases, start=1):
            self.assertEqual([item[0] for item in calls[index]], [alias, alias])
            self.assertNotEqual(calls[index][0][1], calls[index][1][1])

    def test_protocol_retry_allocates_fresh_slot_after_mailbox_failure(self):
        from register.protocol import run_protocol_register

        emails = ["first+splitabc@icloud.com", "second+splitdef@icloud.com"]
        calls = []

        def register(index, **kwargs):
            calls.append(kwargs.get("fixed_email"))
            if len(calls) == 1:
                kwargs["task_event"](
                    index,
                    "failed",
                    error="icloud api request failed: timeout",
                    step="邮箱网关超时",
                )
                return None
            return {"email": kwargs["fixed_email"], "access_token": "token"}

        with (
            patch("mail.release_stale_in_use", return_value=0),
            patch(
                "mail.count_pool_capacity",
                return_value={
                    "available": len(emails),
                    "total": 2,
                    "emails": emails,
                },
            ),
            patch("register.protocol.resolve_proxy_pool", return_value=[]),
            patch("register.protocol.register_one", side_effect=register),
        ):
            result = asyncio.run(
                run_protocol_register(
                    {
                        "registration": {
                            "engine": "protocol",
                            "max_transient_retries": 1,
                        },
                        "mail": {},
                        "proxy": {},
                    },
                    total=1,
                    concurrency=1,
                )
            )

        self.assertEqual(calls, emails)
        self.assertEqual(result[0]["email"], emails[1])

    def test_protocol_proxy_retry_keeps_the_same_alias_slot(self):
        from register.protocol import run_protocol_register

        emails = [
            "first+splitabc@icloud.com",
            "spare+splitdef@icloud.com",
        ]
        calls = []

        def register(index, **kwargs):
            calls.append(kwargs.get("fixed_email"))
            if len(calls) == 1:
                kwargs["task_event"](
                    index,
                    "running",
                    error="Failed to perform, curl: (56) CONNECT tunnel failed",
                    step="代理连接失败",
                )
                return None
            return {"email": kwargs["fixed_email"], "access_token": "token"}

        with (
            patch("mail.release_stale_in_use", return_value=0),
            patch(
                "mail.count_pool_capacity",
                return_value={
                    "available": len(emails),
                    "total": 1,
                    "emails": emails,
                },
            ),
            patch(
                "register.protocol.resolve_proxy_pool",
                return_value=["proxy-a", "proxy-b"],
            ),
            patch("register.protocol.register_one", side_effect=register),
            patch("register.protocol.asyncio.sleep", return_value=None),
        ):
            result = asyncio.run(
                run_protocol_register(
                    {
                        "registration": {
                            "engine": "protocol",
                            "max_transient_retries": 1,
                        },
                        "mail": {},
                        "proxy": {"enabled": True, "rotate_per_task": True},
                    },
                    total=1,
                    concurrency=1,
                    task_event=lambda *_args, **_kwargs: None,
                )
            )

        self.assertEqual(calls, [emails[0], emails[0]])
        self.assertEqual(result[0]["email"], emails[0])

    def test_protocol_curl_28_retries_same_alias_with_next_proxy(self):
        from register.protocol import run_protocol_register

        alias = "timeout+splitabc@icloud.com"
        calls = []

        def register(index, **kwargs):
            calls.append((kwargs.get("fixed_email"), kwargs.get("proxy")))
            if len(calls) == 1:
                kwargs["task_event"](
                    index,
                    "running",
                    error=(
                        "Failed to perform, curl: (28) Connection timed out "
                        "after 30000 milliseconds"
                    ),
                    step="代理连接超时",
                )
                return None
            return {"email": kwargs["fixed_email"], "access_token": "token"}

        with (
            patch("mail.release_stale_in_use", return_value=0),
            patch(
                "mail.count_pool_capacity",
                return_value={"available": 1, "total": 1, "emails": [alias]},
            ),
            patch(
                "register.protocol.resolve_proxy_pool",
                return_value=["proxy-a", "proxy-b"],
            ),
            patch("register.protocol.register_one", side_effect=register),
            patch("register.protocol.asyncio.sleep", return_value=None),
        ):
            result = asyncio.run(
                run_protocol_register(
                    {
                        "registration": {
                            "engine": "protocol",
                            "max_transient_retries": 1,
                        },
                        "mail": {},
                        "proxy": {"enabled": True, "rotate_per_task": True},
                    },
                    total=1,
                    concurrency=1,
                    task_event=lambda *_args, **_kwargs: None,
                )
            )

        self.assertEqual([item[0] for item in calls], [alias, alias])
        self.assertNotEqual(calls[0][1], calls[1][1])
        self.assertEqual(result[0]["email"], alias)

    def test_hybrid_engine_runs_one_browser_session_with_protocol_steps_enabled(self):
        from register.batch import run_register

        class FakePlaywright:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *_args):
                return False

        expected = {"email": "hybrid@example.com", "access_token": "token"}
        with (
            patch(
                "playwright.async_api.async_playwright",
                return_value=FakePlaywright(),
            ),
            patch(
                "register.batch.count_pool_capacity",
                return_value={"available": 1, "total": 1},
            ),
            patch("register.batch.release_stale_in_use", return_value=0),
            patch(
                "register.batch.register_one",
                new=AsyncMock(return_value=expected),
            ) as browser_register,
            patch(
                "register.protocol.run_protocol_register",
                new=AsyncMock(),
            ) as full_protocol_register,
        ):
            result = asyncio.run(
                run_register(
                    {
                        "registration": {
                            "engine": "hybrid",
                            "max_transient_retries": 0,
                        }
                    },
                    total=1,
                    concurrency=1,
                )
            )

        self.assertEqual(result, [expected])
        browser_register.assert_awaited_once()
        self.assertIsNone(browser_register.await_args.args[2])
        self.assertTrue(browser_register.await_args.kwargs["hybrid_mode"])
        full_protocol_register.assert_not_awaited()

    def test_browser_engine_disables_protocol_steps(self):
        from register.batch import run_register

        class FakePlaywright:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *_args):
                return False

        expected = {"email": "browser@example.com", "access_token": "token"}
        with (
            patch(
                "playwright.async_api.async_playwright",
                return_value=FakePlaywright(),
            ),
            patch(
                "register.batch.count_pool_capacity",
                return_value={"available": 1, "total": 1},
            ),
            patch("register.batch.release_stale_in_use", return_value=0),
            patch(
                "register.batch.register_one",
                new=AsyncMock(return_value=expected),
            ) as browser_register,
            patch(
                "register.protocol.run_protocol_register",
                new=AsyncMock(),
            ) as full_protocol_register,
        ):
            result = asyncio.run(
                run_register(
                    {
                        "registration": {
                            "engine": "browser",
                            "max_transient_retries": 0,
                        }
                    },
                    total=1,
                    concurrency=1,
                )
            )

        self.assertEqual(result, [expected])
        browser_register.assert_awaited_once()
        self.assertFalse(browser_register.await_args.kwargs["hybrid_mode"])
        full_protocol_register.assert_not_awaited()

    def test_browser_engine_completes_password_and_totp_after_browser_exit(self):
        from register.batch import run_register

        browser_record = {
            "email": "browser-secure@example.com",
            "access_token": "access-token",
            "session_token": "session-token",
            "account_id": "account-id",
            "password": "",
            "password_set": False,
            "totp_secret": "",
            "mfa_enabled": False,
            "registration_method": "browser",
            "browser_type": "camoufox",
        }

        def repair(_index, **kwargs):
            self.assertEqual(kwargs["account"]["registration_state"], "security_pending")
            kwargs["partial_sink"](
                {"password": "Aa1!confirmed", "password_set": True}
            )
            kwargs["partial_sink"](
                {
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                    "mfa_enabled": True,
                    "mfa_factor_type": "totp",
                }
            )
            return {
                "password": "Aa1!confirmed",
                "password_set": True,
                "totp_secret": "JBSWY3DPEHPK3PXP",
                "mfa_enabled": True,
                "mfa_factor_type": "totp",
            }

        saved = []
        with (
            patch(
                "register.batch.count_pool_capacity",
                return_value={
                    "available": 1,
                    "total": 1,
                    "emails": [browser_record["email"]],
                },
            ),
            patch("register.batch.release_stale_in_use", return_value=0),
            patch(
                "register.batch.register_one",
                new=AsyncMock(return_value=dict(browser_record)),
            ) as browser_register,
            patch("register.batch.repair_security_one", side_effect=repair) as security,
            patch(
                "register.batch.upsert_account_record",
                side_effect=lambda record, _path: saved.append(dict(record)) or dict(record),
            ),
        ):
            result = asyncio.run(
                run_register(
                    {
                        "registration": {
                            "engine": "browser",
                            "browser_protocol_security_after_register": True,
                            "max_transient_retries": 0,
                        },
                        "mail": {},
                    },
                    total=1,
                    concurrency=1,
                )
            )

        browser_register.assert_awaited_once()
        self.assertTrue(browser_register.await_args.kwargs["defer_success_event"])
        security.assert_called_once()
        self.assertEqual(result[0]["registration_method"], "browser")
        self.assertEqual(result[0]["browser_type"], "camoufox")
        self.assertEqual(result[0]["registration_state"], "complete")
        self.assertTrue(result[0]["password_set"])
        self.assertTrue(result[0]["mfa_enabled"])
        self.assertEqual(saved[-1]["registration_state"], "complete")

    def test_browser_security_failure_does_not_repeat_browser_registration(self):
        from register.batch import run_register

        browser_record = {
            "email": "browser-pending@example.com",
            "access_token": "access-token",
            "password": "",
            "password_set": False,
            "registration_method": "browser",
            "browser_type": "camoufox",
        }
        saved = []
        with (
            patch(
                "register.batch.count_pool_capacity",
                return_value={
                    "available": 1,
                    "total": 1,
                    "emails": [browser_record["email"]],
                },
            ),
            patch("register.batch.release_stale_in_use", return_value=0),
            patch(
                "register.batch.register_one",
                new=AsyncMock(return_value=dict(browser_record)),
            ) as browser_register,
            patch(
                "register.batch.repair_security_one",
                side_effect=RuntimeError("mfa enrollment failed"),
            ),
            patch(
                "register.batch.upsert_account_record",
                side_effect=lambda record, _path: saved.append(dict(record)) or dict(record),
            ),
        ):
            result = asyncio.run(
                run_register(
                    {
                        "registration": {
                            "engine": "browser",
                            "browser_protocol_security_after_register": True,
                            "max_transient_retries": 2,
                        },
                        "mail": {},
                    },
                    total=1,
                    concurrency=1,
                )
            )

        self.assertEqual(result, [])
        browser_register.assert_awaited_once()
        self.assertEqual(saved[-1]["registration_state"], "security_pending")
        self.assertIn("mfa enrollment failed", saved[-1]["security_completion_error"])

    def test_browser_batch_pins_a_distinct_mailbox_to_each_task(self):
        from register.batch import run_register

        emails = ["first@icloud.com", "second@icloud.com", "third@icloud.com"]

        async def registered(index, _total, _playwright, **kwargs):
            return {
                "email": kwargs["fixed_email"],
                "access_token": f"token-{index}",
            }

        with (
            patch(
                "register.batch.count_pool_capacity",
                return_value={
                    "available": 3,
                    "total": 3,
                    "emails": emails,
                },
            ),
            patch("register.batch.release_stale_in_use", return_value=0),
            patch(
                "register.batch.register_one",
                new=AsyncMock(side_effect=registered),
            ) as browser_register,
        ):
            result = asyncio.run(
                run_register(
                    {
                        "registration": {
                            "engine": "browser",
                            "max_transient_retries": 0,
                        }
                    },
                    total=3,
                    concurrency=1,
                )
            )

        assigned = [
            call.kwargs["fixed_email"]
            for call in browser_register.await_args_list
        ]
        self.assertEqual(assigned, emails)
        self.assertEqual([item["email"] for item in result], emails)

    def test_browser_alias_pairs_wait_for_both_complete_before_next_pair(self):
        from register.batch import run_register

        emails = [
            "first+splitone@icloud.com",
            "first+splittwo@icloud.com",
            "second+splitone@icloud.com",
            "second+splittwo@icloud.com",
        ]
        started: dict[str, float] = {}
        finished: dict[str, float] = {}

        async def registered(index, _total, _playwright, **kwargs):
            email = kwargs["fixed_email"]
            started[email] = time.monotonic()
            if email.startswith("first+"):
                await asyncio.sleep(
                    0.04 if email.endswith("one@icloud.com") else 0.08
                )
            finished[email] = time.monotonic()
            return {"email": email, "access_token": f"token-{index}"}

        with (
            patch(
                "register.batch.count_pool_capacity",
                return_value={
                    "available": len(emails),
                    "total": 2,
                    "emails": emails,
                },
            ),
            patch("register.batch.release_stale_in_use", return_value=0),
            patch(
                "register.batch.register_one",
                new=AsyncMock(side_effect=registered),
            ),
        ):
            result = asyncio.run(
                run_register(
                    {
                        "registration": {
                            "engine": "browser",
                            "max_transient_retries": 0,
                        }
                    },
                    total=4,
                    concurrency=10,
                )
            )

        first_pair = emails[:2]
        second_pair = emails[2:]
        self.assertEqual({item["email"] for item in result}, set(emails))
        self.assertLess(abs(started[first_pair[0]] - started[first_pair[1]]), 0.1)
        self.assertLess(abs(started[second_pair[0]] - started[second_pair[1]]), 0.1)
        self.assertGreaterEqual(
            min(started[email] for email in second_pair),
            max(finished[email] for email in first_pair),
        )

    def test_browser_fixed_primary_uses_one_alias_concurrency(self):
        from register.batch import run_register

        aliases = ["browser+splitone@icloud.com"]
        entered: list[tuple[str, float]] = []

        async def registered(index, _total, _playwright, **kwargs):
            entered.append((kwargs["fixed_email"], time.monotonic()))
            return {
                "email": kwargs["fixed_email"],
                "access_token": f"token-{index}",
            }

        with (
            patch(
                "register.batch.count_pool_capacity",
                return_value={
                    "available": len(aliases),
                    "total": 1,
                    "fixed_email_mode": "alias_batch",
                    "emails": aliases,
                },
            ) as capacity,
            patch("register.batch.release_stale_in_use", return_value=0),
            patch(
                "register.batch.register_one",
                new=AsyncMock(side_effect=registered),
            ) as browser_register,
        ):
            result = asyncio.run(
                run_register(
                    {
                        "registration": {
                            "engine": "browser",
                            "max_transient_retries": 0,
                        }
                    },
                    total=1,
                    concurrency=8,
                    fixed_email="browser@icloud.com",
                )
            )

        self.assertEqual(capacity.call_args.kwargs["fixed_alias_count"], 1)
        self.assertEqual(browser_register.await_count, 1)
        self.assertEqual({item["email"] for item in result}, set(aliases))
        self.assertEqual(len(entered), 1)

    def test_single_proxy_rate_limit_stops_retries_and_queued_tasks(self):
        from register.batch import run_register

        events = []

        class FakePlaywright:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *_args):
                return False

        async def rate_limited(*_args, **kwargs):
            kwargs["task_event"](
                _args[0],
                "failed",
                error="auth_rate_limit_exceeded",
                step="rate limited",
            )
            return None

        with (
            patch(
                "playwright.async_api.async_playwright",
                return_value=FakePlaywright(),
            ),
            patch(
                "register.batch.count_pool_capacity",
                return_value={"available": 2, "total": 2},
            ),
            patch("register.batch.release_stale_in_use", return_value=0),
            patch(
                "register.batch.resolve_proxy_pool",
                return_value=["socks5://127.0.0.1:1080"],
            ),
            patch(
                "register.batch.register_one",
                new=AsyncMock(side_effect=rate_limited),
            ) as browser_register,
        ):
            result = asyncio.run(
                run_register(
                    {
                        "registration": {
                            "engine": "hybrid",
                            "max_transient_retries": 2,
                        },
                        "proxy": {
                            "enabled": True,
                            "rotate_per_task": False,
                            "url": "socks5://127.0.0.1:1080",
                        },
                    },
                    total=2,
                    concurrency=1,
                    task_event=lambda index, status, **fields: events.append(
                        (index, status, fields.get("step"))
                    ),
                )
            )

        self.assertEqual(result, [])
        browser_register.assert_awaited_once()
        self.assertTrue(
            any(index == 2 and "代理被限频" in str(step) for index, _, step in events)
        )

    def test_account_health_updates_record_without_exposing_token(self):
        from common.account_health import check_account

        source = {
            "email": "health@example.com",
            "access_token": "secret-access-token",
            "refresh_token": "secret-refresh-token",
            "registered_at": "2026-07-14T00:00:00Z",
        }
        remote = {
            "email": source["email"],
            "plan_type": "free",
            "status": "正常",
            "health_status": "alive",
            "health_checked_at": "2026-07-14T01:00:00Z",
            "health_error": "",
            "quota": 12,
            "image_quota_unknown": False,
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "accounts.json"
            with (
                patch("common.account_health.refresh_oauth_tokens", return_value=dict(source)),
                patch("common.account_health.fetch_remote_account_info", return_value=remote),
            ):
                checked = check_account(source, path=path)
            saved = load_accounts(path)[0]
        self.assertEqual(checked["health_status"], "alive")
        self.assertEqual(saved["quota"], 12)
        public = account_public_view(saved)
        self.assertTrue(public["has_access_token"])
        self.assertNotIn("access_token", public)
        self.assertEqual(public["health_status"], "alive")

    def test_account_health_retries_once_on_tls_connect_error(self):
        from common.account_health import check_account
        from common.account_store import load_accounts, upsert_account_record

        source = {
            "email": "tls-retry@example.com",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "health_probe_error": "Failed to perform, curl: (56) CONNECT tunnel failed",
        }
        remote = {
            "email": source["email"],
            "health_status": "alive",
            "health_error": "",
        }
        tls_error = RuntimeError(
            "Failed to perform, curl: (35) TLS connect error:"
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "accounts.json"
            upsert_account_record(source, path)
            with (
                patch("common.account_health.refresh_oauth_tokens", return_value=dict(source)) as refresh,
                patch(
                    "common.account_health.fetch_remote_account_info",
                    side_effect=[tls_error, remote],
                ) as fetch,
                patch("common.account_health.time.sleep") as sleep,
            ):
                checked = check_account(source, path=path)
            saved = load_accounts(path)[0]

        self.assertEqual(checked["health_status"], "alive")
        self.assertEqual(checked["health_probe_error"], "")
        self.assertEqual(saved["health_probe_error"], "")
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(refresh.call_count, 2)
        sleep.assert_called_once_with(0.25)

    def test_account_health_transport_error_preserves_last_known_status(self):
        from common.account_health import check_account

        source = {
            "email": "tls-failed@example.com",
            "access_token": "access-token",
            "health_status": "alive",
            "status": "正常",
        }
        tls_error = RuntimeError("Failed to perform, curl: (35) TLS connect error")
        with (
            patch("common.account_health.refresh_oauth_tokens", return_value=dict(source)),
            patch(
                "common.account_health.fetch_remote_account_info",
                side_effect=[tls_error, tls_error],
            ),
            patch("common.account_health.time.sleep"),
        ):
            checked = check_account(source, persist=False)

        self.assertEqual(checked["health_status"], "alive")
        self.assertEqual(checked["status"], "正常")
        self.assertEqual(checked["health_error"], "")
        self.assertIn("curl: (35)", checked["health_probe_error"])

    def test_account_health_uses_current_proxy_pool_and_rotates_transport_error(self):
        from common.account_health import check_account

        source = {
            "email": "new-proxy@example.com",
            "access_token": "access-token",
            "proxy": "http://old-proxy.example:8080",
        }
        remote = {
            "email": source["email"],
            "health_status": "alive",
            "status": "正常",
            "health_error": "",
        }
        transport_error = RuntimeError(
            "Failed to perform, curl: (56) CONNECT tunnel failed, response 403"
        )
        with (
            patch("common.account_health.refresh_oauth_tokens", side_effect=lambda record, **_: dict(record)),
            patch(
                "common.account_health.fetch_remote_account_info",
                side_effect=[transport_error, remote],
            ) as fetch,
            patch("common.account_health.time.sleep"),
        ):
            checked = check_account(
                source,
                persist=False,
                proxy_candidates=[
                    "http://new-one.example:8080",
                    "http://new-two.example:8080",
                ],
            )

        used = [call.args[0]["proxy"] for call in fetch.call_args_list]
        self.assertEqual(len(set(used)), 2)
        self.assertNotIn(source["proxy"], used)
        self.assertEqual(checked["proxy"], source["proxy"])
        self.assertEqual(checked["health_status"], "alive")
        self.assertEqual(checked["health_probe_proxy_mode"], "current_registration_pool")

    def test_account_health_retries_edge_block_with_fallback_proxy(self):
        from common.account_health import HealthEdgeBlockedError, check_account

        source = {
            "email": "edge-retry@example.com",
            "access_token": "access-token",
            "proxy": "http://primary.example:8080",
        }
        remote = {
            "email": source["email"],
            "health_status": "alive",
            "health_error": "",
        }
        with (
            patch("common.account_health.refresh_oauth_tokens", return_value=dict(source)),
            patch(
                "common.account_health.fetch_remote_account_info",
                side_effect=[
                    HealthEdgeBlockedError(
                        "health_edge_blocked_http_403 /backend-api/me"
                    ),
                    remote,
                ],
            ) as fetch,
            patch(
                "common.trial_proxy_pool.selected_trial_proxies",
                return_value=("JP", ["http://fallback.example:8080"]),
            ),
            patch("common.account_health.time.sleep") as sleep,
        ):
            checked = check_account(source, persist=False)

        self.assertEqual(checked["health_status"], "alive")
        self.assertEqual(checked["proxy"], source["proxy"])
        self.assertEqual(checked["health_probe_proxy_mode"], "JP_pool")
        self.assertEqual(
            fetch.call_args_list[1].args[0]["proxy"],
            "http://fallback.example:8080",
        )
        sleep.assert_called_once()

    def test_health_html_403_is_reported_without_html_body(self):
        from common.account_health import HealthEdgeBlockedError, _response_json

        response = MagicMock(
            status_code=403,
            text="<html><head><style>large edge block page</style></head></html>",
        )
        with self.assertRaises(HealthEdgeBlockedError) as context:
            _response_json(response, "/backend-api/me")

        self.assertEqual(
            str(context.exception),
            "health_edge_blocked_http_403 /backend-api/me",
        )

    def test_account_health_keeps_deactivated_account_as_terminal_invalid(self):
        from common.account_health import check_account

        source = {
            "email": "deactivated@example.com",
            "access_token": "stale-access",
            "refresh_token": "stale-refresh",
            "health_status": "invalid",
            "health_error": "oauth_refresh_http_401: refresh_token_invalidated",
            "reauth_blocked_reason": "account_deactivated",
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "accounts.json"
            with (
                patch("common.account_health.refresh_oauth_tokens") as refresh,
                patch("common.account_health.fetch_remote_account_info") as fetch,
            ):
                checked = check_account(source, path=path)

        self.assertEqual(checked["health_status"], "invalid")
        self.assertIn("account_deactivated", checked["health_error"])
        refresh.assert_not_called()
        fetch.assert_not_called()

    def test_bulk_health_skips_known_deactivated_rows_and_reports_separate_totals(self):
        from common.account_health import check_accounts

        records = [
            {
                "email": "already-dead@example.com",
                "health_status": "invalid",
                "status": "已封号",
                "reauth_blocked_reason": "account_deactivated",
            },
            {"email": "probe@example.com", "health_status": "alive"},
            {"email": "not-checked@example.com", "health_status": "unknown"},
        ]

        def checked(record, **kwargs):
            self.assertFalse(kwargs["persist"])
            return {**record, "health_status": "alive"}

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "accounts.json"
            with (
                patch("common.account_health.check_account", side_effect=checked) as probe,
                patch(
                    "common.account_health.upsert_account_records",
                    side_effect=lambda batch, _path: [dict(item) for item in batch],
                ),
            ):
                result = check_accounts(records=records, workers=2, path=path)

        self.assertEqual(result["requested"], 3)
        self.assertEqual(result["actual_total"], 2)
        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["skipped_known_deactivated"], 1)
        self.assertEqual(result["deactivated"], 1)
        self.assertEqual(result["usable"], 2)
        self.assertEqual(result["unavailable"], 1)
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(
            [item["email"] for item in result["skipped_items"]],
            ["already-dead@example.com"],
        )

    def test_bulk_health_with_only_known_deactivated_rows_does_not_probe(self):
        from common.account_health import check_accounts

        records = [
            {
                "email": "dead-only@example.com",
                "health_status": "invalid",
                "health_error": "account_deactivated: account has been deleted",
            }
        ]
        with (
            patch("common.account_health.check_account") as probe,
            patch("common.account_health.upsert_account_records") as persist,
        ):
            result = check_accounts(records=records, workers=3)

        self.assertEqual(result["requested"], 1)
        self.assertEqual(result["actual_total"], 0)
        self.assertEqual(result["checked"], 0)
        self.assertEqual(result["skipped_known_deactivated"], 1)
        self.assertEqual(result["unavailable"], 1)
        probe.assert_not_called()
        persist.assert_not_called()

    def test_bulk_health_batches_results_before_progress(self):
        from common.account_health import check_accounts

        records = [
            {"email": "one@example.com", "health_status": "alive"},
            {"email": "two@example.com", "health_status": "limited"},
        ]

        def checked(record, **kwargs):
            self.assertFalse(kwargs["persist"])
            return dict(record)

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "accounts.json"
            persisted = []

            def persist_batch(batch, _path):
                persisted.extend(record["email"] for record in batch)
                return [dict(record) for record in batch]

            progress = []
            with (
                patch("common.account_health.check_account", side_effect=checked) as check_one,
                patch("common.account_health.upsert_account_records", side_effect=persist_batch) as persist,
            ):
                result = check_accounts(
                    records=records,
                    workers=2,
                    path=path,
                    progress_callback=lambda done, _total, item: progress.append(
                        (done, item["email"], list(persisted))
                    ),
                )

        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["alive"], 1)
        self.assertEqual(result["limited"], 1)
        self.assertEqual(check_one.call_count, 2)
        self.assertEqual(persist.call_count, 1)
        self.assertEqual(len(persist.call_args.args[0]), 2)
        self.assertEqual(len(progress), 2)
        for _done, email, visible_at_callback in progress:
            self.assertIn(email, visible_at_callback)

    def test_account_health_progress_updates_realtime_counts(self):
        import web_app

        web_app._start_account_health_progress(3, 8)
        try:
            web_app._update_account_health_progress(
                1,
                3,
                {
                    "email": "one@example.com",
                    "health_status": "alive",
                    "plan_type": "plus",
                },
            )
            web_app._update_account_health_progress(
                2, 3, {"email": "two@example.com", "health_status": "error"}
            )
            progress = web_app._account_health_progress_snapshot()
            self.assertTrue(progress["running"])
            self.assertEqual(progress["done"], 2)
            self.assertEqual(progress["total"], 3)
            self.assertEqual(progress["requested_workers"], 8)
            self.assertEqual(progress["workers"], 8)
            self.assertFalse(progress["throttled"])
            self.assertEqual(progress["resource_mode"], "normal")
            self.assertEqual(progress["requested"], 3)
            self.assertEqual(progress["actual_total"], 3)
            self.assertEqual(progress["checked"], 2)
            self.assertEqual(progress["current"], "two@example.com")
            self.assertEqual(progress["alive"], 1)
            self.assertEqual(progress["error"], 1)
            first_update = next(
                item
                for item in progress["updates"].values()
                if item["email"] == "one@example.com"
            )
            self.assertEqual(first_update["health_status"], "alive")
            self.assertEqual(first_update["plan_type"], "plus")
        finally:
            web_app._finish_account_health_progress()
        finished = web_app._account_health_progress_snapshot()
        self.assertFalse(finished["running"])
        self.assertEqual(finished["done"], 2)

    def test_account_health_progress_exposes_skipped_terminal_rows(self):
        import web_app

        web_app._start_account_health_progress(
            2,
            4,
            requested=5,
            skipped_known_deactivated=3,
        )
        try:
            progress = web_app._account_health_progress_snapshot()
            self.assertTrue(progress["running"])
            self.assertEqual(progress["requested"], 5)
            self.assertEqual(progress["actual_total"], 2)
            self.assertEqual(progress["total"], 2)
            self.assertEqual(progress["skipped_known_deactivated"], 3)
            self.assertEqual(progress["deactivated"], 3)
            self.assertEqual(progress["done"], 0)
        finally:
            web_app._finish_account_health_progress()

    def test_large_account_health_progress_omits_inline_row_updates(self):
        import web_app

        web_app._start_account_health_progress(201, 8)
        try:
            web_app._update_account_health_progress(
                1,
                201,
                {"email": "large-job@example.com", "health_status": "alive"},
            )
            progress = web_app._account_health_progress_snapshot()
            self.assertFalse(progress["updates_enabled"])
            self.assertEqual(progress["updates"], {})
            hidden = web_app._account_health_progress_snapshot(include_updates=False)
            self.assertEqual(hidden["updates"], {})
        finally:
            web_app._finish_account_health_progress()

    def test_health_worker_gate_applies_dynamic_process_wide_limit(self):
        from common.account_health import check_accounts

        records = [
            {"email": f"limited-{index}@example.com", "health_status": "alive"}
            for index in range(4)
        ]
        active = 0
        maximum_active = 0
        active_lock = threading.Lock()

        def checked(record, **_kwargs):
            nonlocal active, maximum_active
            with active_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.03)
            with active_lock:
                active -= 1
            return dict(record)

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "accounts.json"
            with patch("common.account_health.check_account", side_effect=checked):
                result = check_accounts(
                    records=records,
                    workers=4,
                    worker_limit=lambda _requested: 1,
                    path=path,
                )

        self.assertEqual(result["checked"], 4)
        self.assertEqual(maximum_active, 1)

    def test_health_resource_budget_prioritizes_registration(self):
        import web_app

        state = web_app.JobState()
        with state.lock:
            state.running = True
            state.params = {"concurrency": 3}
        with patch.object(web_app, "JOB", state):
            web_app._start_account_health_progress(4, 10)
            throttled = web_app._account_health_progress_snapshot()
            self.assertEqual(throttled["requested_workers"], 10)
            self.assertEqual(throttled["workers"], 1)
            self.assertTrue(throttled["throttled"])
            self.assertEqual(throttled["resource_mode"], "registration_priority")

            with state.lock:
                state.running = False
            restored = web_app._account_health_progress_snapshot()

        self.assertEqual(restored["workers"], 10)
        self.assertFalse(restored["throttled"])
        self.assertEqual(restored["resource_mode"], "normal")
        web_app._finish_account_health_progress()

    def test_trial_backfill_waits_for_registration_to_finish(self):
        import web_app

        state = web_app.JobState()
        with state.lock:
            state.running = True
        with (
            patch.object(web_app, "JOB", state),
            patch("web_app.threading.Thread") as worker,
        ):
            queued = web_app._queue_trial_eligibility_backfill(
                [{"email": "busy@example.com"}]
            )

        self.assertEqual(queued, 0)
        worker.assert_not_called()

    def test_health_batches_can_run_concurrently(self):
        from concurrent.futures import ThreadPoolExecutor
        import common.account_health as health

        barrier = threading.Barrier(2)

        def checked(record, **_kwargs):
            barrier.wait(timeout=3)
            return dict(record)

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "accounts.json"
            with patch("common.account_health.check_account", side_effect=checked):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [
                        executor.submit(
                            health.check_accounts,
                            records=[{"email": f"batch-{index}@example.com", "health_status": "alive"}],
                            path=path,
                        )
                        for index in range(2)
                    ]
                    results = [future.result(timeout=5) for future in futures]
                stored = load_accounts(path)

        self.assertEqual([result["checked"] for result in results], [1, 1])
        self.assertEqual(len(stored), 2)

    def test_health_check_reuses_registration_browser_identity(self):
        from common.account_health import fetch_remote_account_info

        def response(data):
            item = MagicMock(status_code=200, text="")
            item.json.return_value = data
            return item

        def get_response(url, **_kwargs):
            if url.endswith("/backend-api/me"):
                return response({"email": "identity@example.com", "id": "user-1"})
            return response(
                {
                    "accounts": {
                        "default": {
                            "account": {"account_id": "acct-1", "plan_type": "plus"}
                        }
                    }
                }
            )

        def post_response(_url, **_kwargs):
            return response(
                {
                    "limits_progress": [
                        {"feature_name": "image_gen", "remaining": 25}
                    ]
                }
            )

        session = MagicMock()
        session.headers = {}
        session.get.side_effect = get_response
        session.post.side_effect = post_response
        record = {
            "email": "identity@example.com",
            "access_token": "token",
            "plan_type": "free",
            "device_id": "stable-device-id",
            "locale": "en-US",
            "timezone_offset_min": 420,
        }

        with patch("common.account_health._session", return_value=session):
            result = fetch_remote_account_info(record)

        self.assertEqual(result["health_status"], "alive")
        self.assertEqual(result["plan_type"], "plus")
        self.assertEqual(session.headers["OAI-Device-Id"], "stable-device-id")
        self.assertEqual(session.headers["OAI-Language"], "en-US")
        self.assertEqual(
            session.post.call_args.kwargs["json"]["timezone_offset_min"], 420
        )
        get_urls = [item.args[0] for item in session.get.call_args_list]
        self.assertTrue(any("timezone_offset_min=420" in url for url in get_urls))
        session.close.assert_called_once_with()

    def test_health_check_marks_http_200_deactivated_account_invalid(self):
        from common.account_health import check_account

        def response(data):
            item = MagicMock(status_code=200, text="")
            item.json.return_value = data
            return item

        session = MagicMock()
        session.headers = {}
        session.get.side_effect = lambda url, **_kwargs: (
            response({"email": "deactivated-200@example.com", "id": "user-1"})
            if url.endswith("/backend-api/me")
            else response(
                {
                    "accounts": {
                        "default": {
                            "account": {
                                "account_id": "acct-1",
                                "plan_type": "free",
                                "is_deactivated": True,
                            }
                        }
                    }
                }
            )
        )
        session.post.return_value = response({"limits_progress": []})
        source = {
            "email": "deactivated-200@example.com",
            "access_token": "token",
            "health_status": "alive",
        }

        with patch("common.account_health._session", return_value=session):
            checked = check_account(source, persist=False)

        self.assertEqual(checked["status"], "已封号")
        self.assertEqual(checked["health_status"], "invalid")
        self.assertEqual(checked["reauth_blocked_reason"], "account_deactivated")
        self.assertIn("account_deactivated", checked["health_error"])
        self.assertTrue(checked["last_invalid_at"])
        session.close.assert_called_once_with()

    def test_parallel_health_check_prioritizes_invalid_token_error(self):
        from common.account_health import InvalidAccessTokenError, fetch_remote_account_info

        def response(status_code, data=None, text=""):
            item = MagicMock(status_code=status_code, text=text)
            item.json.return_value = data or {}
            return item

        session = MagicMock()
        session.headers = {}
        session.get.side_effect = lambda url, **_kwargs: (
            response(500, text="temporary upstream error")
            if url.endswith("/backend-api/me")
            else response(200, {"accounts": {"default": {"account": {}}}})
        )
        session.post.return_value = response(401, text="expired")

        with (
            patch("common.account_health._session", return_value=session),
            self.assertRaises(InvalidAccessTokenError),
        ):
            fetch_remote_account_info(
                {"email": "expired@example.com", "access_token": "expired-token"}
            )

        session.close.assert_called_once_with()


class PanelOperationsTests(unittest.TestCase):
    def test_task_runtime_migrates_legacy_json_to_sqlite(self):
        import common.panel_ops as panel_ops

        with tempfile.TemporaryDirectory() as td:
            history_path = Path(td) / "task_history.json"
            history_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "updated_at": "2026-07-30T00:00:00Z",
                        "cards": [
                            {
                                "card_id": "legacy:1",
                                "batch_id": "legacy",
                                "index": 1,
                                "status": "success",
                                "updated_at_epoch": 10,
                            }
                        ],
                        "last_job": {"batch_id": "legacy"},
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(panel_ops, "TASK_HISTORY_FILE", history_path):
                saved = panel_ops.load_task_runtime()
                self.assertEqual(saved["cards"][0]["card_id"], "legacy:1")
                self.assertEqual(saved["last_job"]["batch_id"], "legacy")
                self.assertTrue(history_path.with_suffix(".sqlite3").is_file())

    def test_task_runtime_persists_and_merges_batches(self):
        import common.panel_ops as panel_ops

        with tempfile.TemporaryDirectory() as td:
            history_path = Path(td) / "task_history.json"
            with patch.object(panel_ops, "TASK_HISTORY_FILE", history_path):
                panel_ops.save_task_runtime(
                    [
                        {
                            "card_id": "batch-a:1",
                            "batch_id": "batch-a",
                            "index": 1,
                            "status": "success",
                            "updated_at_epoch": 10,
                        }
                    ],
                    {"batch_id": "batch-a", "total": 1, "success_count": 1},
                )
                panel_ops.save_task_runtime(
                    [
                        {
                            "card_id": "batch-b:1",
                            "batch_id": "batch-b",
                            "index": 1,
                            "status": "failed",
                            "updated_at_epoch": 20,
                        }
                    ],
                    {"batch_id": "batch-b", "total": 1, "fail_count": 1},
                )
                saved = panel_ops.load_task_runtime()

                self.assertEqual(len(saved["cards"]), 2)
                self.assertEqual(saved["cards"][0]["card_id"], "batch-b:1")
                self.assertEqual(saved["last_job"]["batch_id"], "batch-b")

                removed = panel_ops.delete_task_runtime(["batch-a:1"])
                remaining = panel_ops.load_task_runtime()["cards"]

            self.assertEqual(removed, 1)
            self.assertEqual([item["card_id"] for item in remaining], ["batch-b:1"])

    def test_job_state_restores_last_persisted_batch(self):
        import web_app

        saved = {
            "cards": [
                {
                    "card_id": "batch-z:1",
                    "batch_id": "batch-z",
                    "index": 1,
                    "status": "success",
                    "started_at_epoch": 100,
                    "finished_at_epoch": 112,
                    "elapsed_sec": 12,
                }
            ],
            "last_job": {
                "batch_id": "batch-z",
                "started_at": "2026-07-15T01:00:00Z",
                "finished_at": "2026-07-15T01:01:00Z",
                "total": 1,
                "success_count": 1,
                "fail_count": 0,
            },
        }
        with patch("web_app.load_task_runtime", return_value=saved):
            state = web_app.JobState(restore=True)

        self.assertFalse(state.running)
        self.assertEqual(state.batch_id, "batch-z")
        self.assertEqual(state.total, 1)
        self.assertEqual(state.success_count, 1)
        self.assertEqual(state.tasks_snapshot()[0]["card_id"], "batch-z:1")

    def test_job_state_closes_interrupted_batch_after_service_restart(self):
        import web_app

        saved = {
            "cards": [
                {
                    "card_id": "batch-interrupted:1",
                    "batch_id": "batch-interrupted",
                    "index": 1,
                    "status": "running",
                    "updated_at_epoch": 112,
                }
            ],
            "last_job": {
                "batch_id": "batch-interrupted",
                "started_at": "2026-08-07T05:40:00Z",
                "finished_at": None,
                "total": 1,
            },
        }
        with patch("web_app.load_task_runtime", return_value=saved):
            state = web_app.JobState(restore=True)

        restored = state.tasks_snapshot()[0]
        self.assertEqual(restored["status"], "cancelled")
        self.assertEqual(restored["error"], "service_restarted")
        self.assertEqual(state.last_error, "service_restarted")
        self.assertTrue(state.finished_at)

    def test_operation_progress_tracks_stage_counts_and_completion(self):
        import web_app

        state = web_app.OperationProgressState()
        state.start("upload", "账号上传", total=10, stage="准备")
        state.update("upload", done=4, stage="上传中")
        running = state.snapshot()[0]
        state.finish("upload", message="完成")
        finished = state.snapshot()[0]

        self.assertEqual(running["percent"], 40.0)
        self.assertEqual(running["stage"], "上传中")
        self.assertEqual(finished["status"], "success")
        self.assertEqual(finished["percent"], 100)

    def test_task_metrics_summarizes_results_and_failures(self):
        from common.panel_ops import task_metrics

        result = task_metrics(
            [
                {"status": "success", "elapsed_sec": 10},
                {"status": "failed", "elapsed_sec": 20, "error": "otp timeout"},
                {"status": "running", "elapsed_sec": 5},
            ]
        )

        self.assertEqual(result["total"], 2)
        self.assertEqual(result["success_rate"], 50.0)
        self.assertEqual(result["average_seconds"], 15.0)
        self.assertEqual(result["failure_reasons"][0]["reason"], "otp timeout")

    def test_account_trash_round_trip(self):
        import common.panel_ops as panel_ops

        with tempfile.TemporaryDirectory() as td:
            trash_path = Path(td) / "trash.json"
            with patch.object(panel_ops, "TRASH_FILE", trash_path):
                self.assertEqual(
                    panel_ops.archive_accounts(
                        [{"email": "trash@example.com", "access_token": "secret"}],
                        reason="manual",
                    ),
                    1,
                )
                row = panel_ops.list_trash()[0]
                restored = panel_ops.take_trash([row["trash_id"]])
                remaining = panel_ops.list_trash()

        self.assertEqual(restored[0]["email"], "trash@example.com")
        self.assertEqual(remaining, [])

    def test_backup_create_and_restore(self):
        import common.panel_ops as panel_ops

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "config.yaml"
            source.write_text("value: original\n", encoding="utf-8")
            with (
                patch.object(panel_ops, "BACKUP_DIR", root / "backups"),
                patch.object(panel_ops, "BACKUP_FILES", {"config.yaml": source}),
                patch.object(panel_ops, "BACKUP_DB_FILES", {}),
                patch.object(panel_ops, "BACKUP_DIR_SOURCES", {}),
            ):
                item = panel_ops.create_backup()
                source.write_text("value: changed\n", encoding="utf-8")
                restored = panel_ops.restore_backup(item["name"])

            content = source.read_text(encoding="utf-8")

        self.assertEqual(restored, ["config.yaml"])
        self.assertEqual(content, "value: original\n")

    def test_mailbox_state_release_selected(self):
        import mail

        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "one@example.com": {"state": "failed"},
                        "two@example.com": {"state": "used"},
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(mail, "STATE_FILE", state_path):
                changed = mail.update_mailbox_states(
                    ["one@example.com"], action="release"
                )
                state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(changed, 1)
        self.assertNotIn("one@example.com", state)
        self.assertIn("two@example.com", state)

    def test_job_pause_and_resume(self):
        import web_app

        with web_app.JOB.lock:
            old_running = web_app.JOB.running
            old_paused = web_app.JOB.paused
            web_app.JOB.running = True
            web_app.JOB.paused = False
        try:
            paused = asyncio.run(web_app.job_pause())
            self.assertTrue(paused["job"]["paused"])
            resumed = asyncio.run(web_app.job_resume())
            self.assertFalse(resumed["job"]["paused"])
        finally:
            with web_app.JOB.lock:
                web_app.JOB.running = old_running
                web_app.JOB.paused = old_paused

    def test_proxy_test_reports_probe_results(self):
        import web_app

        expected = {
            "proxy": "127.0.0.1:1080",
            "ok": True,
            "status": 200,
            "latency_ms": 42,
            "ip": "203.0.113.1",
            "country": "US",
            "error": "",
        }
        async def inline_thread(function, *args, **kwargs):
            return function(*args, **kwargs)

        with (
            patch.object(web_app, "_read_config", return_value={"proxy": {}}),
            patch("register.batch.resolve_proxy_pool", return_value=["socks5://127.0.0.1:1080"]),
            patch.object(web_app, "probe_proxy", return_value=expected),
            patch.object(web_app.asyncio, "to_thread", side_effect=inline_thread),
        ):
            result = asyncio.run(web_app.proxy_test(web_app.ProxyTestRequest()))

        self.assertEqual(result["alive"], 1)
        self.assertEqual(result["items"], [expected])


class MailboxSnapshotCacheTests(unittest.TestCase):
    def test_known_bindings_cache_reuses_snapshot_and_invalidates_on_source_change(self):
        import common.account_store as account_store
        import mail

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trash = root / "account_trash.json"
            cdks = root / "cdks.json"
            trash.write_text(
                json.dumps([{"record": {"email": "trash@example.com"}}]),
                encoding="utf-8",
            )
            cdks.write_text(
                json.dumps([{"email": "cdk@example.com"}]),
                encoding="utf-8",
            )
            old_key = mail._KNOWN_MAILBOX_BINDINGS_CACHE_KEY
            old_value = mail._KNOWN_MAILBOX_BINDINGS_CACHE_VALUE
            calls = 0

            def emails():
                nonlocal calls
                calls += 1
                return {"account@example.com"}

            try:
                mail._KNOWN_MAILBOX_BINDINGS_CACHE_KEY = None
                mail._KNOWN_MAILBOX_BINDINGS_CACHE_VALUE = None
                with (
                    patch.object(mail, "ACCOUNT_TRASH_FILE", trash),
                    patch.object(mail, "CDKS_FILE", cdks),
                    patch.object(account_store, "account_email_set", side_effect=emails),
                    patch.object(account_store, "account_store_revision", return_value=7),
                ):
                    first = mail._known_mailbox_bindings()
                    second = mail._known_mailbox_bindings()
                    self.assertEqual(first, second)
                    self.assertEqual(calls, 1)

                    # A size change is sufficient to model the atomic replace
                    # used by production writers and must invalidate the read.
                    trash.write_text(
                        json.dumps(
                            [
                                {"record": {"email": "trash@example.com"}},
                                {"record": {"email": "new@example.com"}},
                            ]
                        ),
                        encoding="utf-8",
                    )
                    third = mail._known_mailbox_bindings()
                    self.assertIn("new@example.com", third)
                    self.assertEqual(calls, 2)
            finally:
                mail._KNOWN_MAILBOX_BINDINGS_CACHE_KEY = old_key
                mail._KNOWN_MAILBOX_BINDINGS_CACHE_VALUE = old_value


class MaddyWebuiMailTests(unittest.TestCase):
    def test_maddy_alias_normalization_and_dynamic_capacity(self):
        import mail

        self.assertEqual(normalize_provider_type("maddy"), "maddy_webui")
        self.assertEqual(normalize_provider_type("maddy_api"), "maddy_webui")

        config = {
            "providers": [
                {
                    "type": "maddy_webui",
                    "enable": True,
                    "api_base": "https://admin.example/api",
                    "api_key": "key-fixture",
                    "default_domain": "unjome.com",
                    "domains": ["unjome.com", "guimingpian.com"],
                    "blocked_domains": ["xb1219.com"],
                    "capacity": 17,
                    "mailboxes": "",
                }
            ]
        }
        with (
            patch.object(mail, "_known_mailbox_bindings", return_value={}),
            patch.object(mail, "reconcile_registered_mailbox_states", return_value=0),
            patch.object(mail, "_load_state", return_value={}),
        ):
            stats = mail.count_pool_capacity(config, provider_type="maddy")

        self.assertEqual(stats["total"], 17)
        self.assertEqual(stats["available"], 17)

    def test_maddy_create_mailbox_uses_api_key_headers_without_login(self):
        import mail

        response = MagicMock(status_code=201, text="")
        response.json.return_value = {"email": "fixture@unjome.com"}
        session = MagicMock()
        session.request.return_value = response
        with tempfile.TemporaryDirectory() as td, patch.object(mail, "STATE_FILE", Path(td) / "state.json"):
            provider = mail.MaddyWebuiProvider(
                {
                    "type": "maddy_webui",
                    "api_base": "https://admin.example",
                    "api_key": "key-fixture",
                    "default_domain": "unjome.com",
                    "domains": ["unjome.com"],
                    "mailbox_password": "Mailbox-pass-123",
                },
                {"request_timeout": 8, "user_agent": "test-agent"},
            )
            provider.session = session
            try:
                mailbox = provider.create_mailbox("fixture")
            finally:
                provider.close()
            state = json.loads(mail.STATE_FILE.read_text(encoding="utf-8"))

        self.assertEqual(mailbox["provider"], "maddy_webui")
        self.assertEqual(mailbox["address"], "fixture@unjome.com")
        call_args = session.request.call_args
        self.assertEqual(call_args.args[:2], ("POST", "https://admin.example/api/automation/mailboxes"))
        self.assertEqual(call_args.kwargs["headers"]["X-API-Key"], "key-fixture")
        self.assertEqual(call_args.kwargs["headers"]["Authorization"], "Bearer key-fixture")
        self.assertEqual(
            call_args.kwargs["json"],
            {"email": "fixture@unjome.com", "password": "Mailbox-pass-123"},
        )
        self.assertEqual(state["fixture@unjome.com"]["provider"], "maddy_webui")

    def test_maddy_fetch_domains_uses_api_key_headers(self):
        import mail

        response = MagicMock(status_code=200, text="")
        response.json.return_value = {
            "primaryDomain": "xb1219.com",
            "domains": ["xb1219.com", "unjome.com", "guimingpian.com"],
            "count": 3,
        }
        session = MagicMock()
        session.request.return_value = response
        provider = mail.MaddyWebuiProvider(
            {
                "type": "maddy_webui",
                "api_base": "https://admin.example",
                "api_key": "key-fixture",
                "blocked_domains": ["xb1219.com"],
            },
            {"request_timeout": 8, "user_agent": "test-agent"},
        )
        provider.session = session
        try:
            info = provider.fetch_domain_info(force=True)
        finally:
            provider.close()

        self.assertEqual(info["primary_domain"], "unjome.com")
        self.assertEqual(info["domains"], ["unjome.com", "guimingpian.com"])
        self.assertEqual(info["count"], 2)
        call_args = session.request.call_args
        self.assertEqual(call_args.args[:2], ("GET", "https://admin.example/api/automation/domains"))
        self.assertEqual(call_args.kwargs["headers"]["X-API-Key"], "key-fixture")
        self.assertEqual(call_args.kwargs["headers"]["Authorization"], "Bearer key-fixture")

    def test_maddy_create_mailbox_rejects_blocked_domain(self):
        import mail

        session = MagicMock()
        with tempfile.TemporaryDirectory() as td, patch.object(mail, "STATE_FILE", Path(td) / "state.json"):
            provider = mail.MaddyWebuiProvider(
                {
                    "type": "maddy_webui",
                    "api_base": "https://admin.example",
                    "api_key": "key-fixture",
                    "default_domain": "xb1219.com",
                },
                {"request_timeout": 8, "user_agent": "test-agent"},
            )
            provider.session = session
            try:
                with self.assertRaises(mail.MaddyWebuiError):
                    provider.create_mailbox("fixture@xb1219.com")
            finally:
                provider.close()

        session.request.assert_not_called()

    def test_maddy_create_mailbox_uses_env_key_when_config_key_missing(self):
        import mail

        response = MagicMock(status_code=201, text="")
        response.json.return_value = {"email": "fixture@unjome.com"}
        session = MagicMock()
        session.request.return_value = response
        with (
            tempfile.TemporaryDirectory() as td,
            patch.dict(os.environ, {"MADDY_WEBUI_API_KEY": "env-key"}, clear=False),
            patch.object(mail, "STATE_FILE", Path(td) / "state.json"),
        ):
            provider = mail.MaddyWebuiProvider(
                {
                    "type": "maddy_webui",
                    "api_base": "https://admin.example",
                    "default_domain": "unjome.com",
                    "domains": ["unjome.com"],
                    "mailbox_password": "Mailbox-pass-123",
                },
                {"request_timeout": 8, "user_agent": "test-agent"},
            )
            provider.session = session
            try:
                mailbox = provider.create_mailbox("fixture")
            finally:
                provider.close()

        self.assertEqual(mailbox["address"], "fixture@unjome.com")
        call_args = session.request.call_args
        self.assertEqual(call_args.kwargs["headers"]["X-API-Key"], "env-key")
        self.assertEqual(call_args.kwargs["headers"]["Authorization"], "Bearer env-key")

    def test_maddy_default_ip_base_uses_admin_host_header(self):
        import mail

        response = MagicMock(status_code=201, text="")
        response.json.return_value = {"email": "fixture@unjome.com"}
        session = MagicMock()
        session.request.return_value = response
        with tempfile.TemporaryDirectory() as td, patch.object(mail, "STATE_FILE", Path(td) / "state.json"):
            provider = mail.MaddyWebuiProvider(
                {
                    "type": "maddy_webui",
                    "api_key": "key-fixture",
                    "default_domain": "unjome.com",
                    "mailbox_password": "Mailbox-pass-123",
                },
                {"request_timeout": 8, "user_agent": "test-agent"},
            )
            provider.session = session
            try:
                mailbox = provider.create_mailbox("fixture")
            finally:
                provider.close()

        self.assertEqual(mailbox["address"], "fixture@unjome.com")
        self.assertEqual(provider.api_base, "https://120.53.232.195/api")
        self.assertEqual(provider.api_host, "admin.xb1219.com")
        self.assertFalse(provider.verify_tls)
        call_args = session.request.call_args
        self.assertEqual(call_args.args[:2], ("POST", "https://120.53.232.195/api/automation/mailboxes"))
        self.assertEqual(call_args.kwargs["headers"]["Host"], "admin.xb1219.com")
        self.assertEqual(call_args.kwargs["headers"]["X-API-Key"], "key-fixture")
        self.assertFalse(call_args.kwargs["verify"])

    def test_maddy_default_ip_base_suppresses_insecure_request_warning(self):
        import mail
        import warnings
        from urllib3.exceptions import InsecureRequestWarning

        response = MagicMock(status_code=201, text="")
        response.json.return_value = {"email": "fixture@unjome.com"}

        def request(*args, **kwargs):
            warnings.warn("unverified https request", InsecureRequestWarning)
            return response

        session = MagicMock()
        session.request.side_effect = request
        with tempfile.TemporaryDirectory() as td, patch.object(mail, "STATE_FILE", Path(td) / "state.json"):
            provider = mail.MaddyWebuiProvider(
                {
                    "type": "maddy_webui",
                    "api_key": "key-fixture",
                    "default_domain": "unjome.com",
                    "mailbox_password": "Mailbox-pass-123",
                },
                {"request_timeout": 8, "user_agent": "test-agent"},
            )
            provider.session = session
            try:
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    mailbox = provider.create_mailbox("fixture")
            finally:
                provider.close()

        self.assertEqual(mailbox["address"], "fixture@unjome.com")
        self.assertFalse(caught)

    def test_panel_lists_maddy_dynamic_state_mailboxes(self):
        import web_app

        state = {
            "box@xb1219.com": {
                "state": "used",
                "provider": "maddy_webui",
                "api_base": "https://admin.example/api",
                "updated_at": "2026-08-19 10:00:00+0800",
            }
        }
        cfg = {
            "mail": {
                "providers": [
                    {
                        "type": "maddy_webui",
                        "enable": True,
                        "label": "Maddy",
                        "api_base": "https://admin.example/api",
                        "api_key": "key-fixture",
                        "default_domain": "xb1219.com",
                        "domains": ["xb1219.com"],
                        "mailboxes": "",
                    }
                ]
            }
        }
        with (
            patch.object(web_app, "_load_state_file", return_value=state),
            patch.object(web_app, "account_email_set", return_value={"box@xb1219.com"}),
        ):
            items = web_app._mailbox_items(cfg, provider_type="maddy")

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["provider_type"], "maddy_webui")
        self.assertEqual(items[0]["email"], "box@xb1219.com")
        self.assertTrue(items[0]["registered"])

    def test_panel_maddy_mailbox_preview_uses_provider_messages(self):
        import web_app

        provider = MagicMock()
        provider.fetch_recent_messages.return_value = [
            {
                "subject": "Your temporary ChatGPT code",
                "sender": "OpenAI <noreply@tm.openai.com>",
                "recipients": ["box@xb1219.com"],
                "text_content": "Verification code: 123456",
                "html_content": "",
                "received_at": "2026-08-19T01:02:03Z",
            }
        ]
        entry = {
            "type": "maddy_webui",
            "api_base": "https://admin.example/api",
            "api_key": "key-fixture",
            "default_domain": "xb1219.com",
            "domains": ["xb1219.com"],
        }
        with (
            patch.object(web_app, "_find_mailbox_credential", return_value=(entry, {"email": "box@xb1219.com"})),
            patch.object(web_app, "MaddyWebuiProvider", return_value=provider),
        ):
            result = web_app._fetch_mailbox_messages_sync(
                {"mail": {}},
                web_app.MailMessagesRequest(
                    email="box@xb1219.com",
                    provider_type="maddy_webui",
                    limit=10,
                ),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["provider_type"], "maddy_webui")
        self.assertEqual(result["items"][0]["code"], "123456")
        provider.close.assert_called_once()

    def test_panel_maddy_domains_endpoint_uses_saved_key(self):
        import web_app

        provider = MagicMock()
        provider.fetch_domain_info.return_value = {
            "primary_domain": "xb1219.com",
            "domains": ["xb1219.com", "guimingpian.com"],
            "count": 2,
        }
        cfg = {
            "mail": {
                "providers": [
                    {
                        "type": "maddy_webui",
                        "api_base": "https://admin.example/api",
                        "api_key": "saved-key",
                        "default_domain": "xb1219.com",
                    }
                ]
            }
        }
        with (
            patch.object(web_app, "MaddyWebuiProvider", return_value=provider) as ctor,
        ):
            result = web_app._maddy_webui_domains_payload(
                cfg,
                web_app.MaddyWebuiDomainsRequest(api_base="https://override.example/api"),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["items"], ["guimingpian.com"])
        self.assertEqual(result["default_domain"], "guimingpian.com")
        self.assertEqual(result["primary_domain"], "guimingpian.com")
        entry = ctor.call_args.args[0]
        self.assertEqual(entry["api_key"], "saved-key")
        self.assertEqual(entry["api_base"], "https://override.example/api")
        provider.fetch_domain_info.assert_called_once_with(force=True)
        provider.close.assert_called_once()

    def test_panel_maddy_domains_endpoint_ignores_redacted_keys(self):
        import web_app

        cfg = {
            "mail": {
                "providers": [
                    {
                        "type": "maddy_webui",
                        "api_base": "https://admin.example/api",
                        "api_key": "saved-key",
                        "default_domain": "guimingpian.com",
                    }
                ]
            }
        }
        for redacted in ("maddy_…acf3", "••••••••", "maddy_***_acf3"):
            with self.subTest(redacted=redacted):
                provider = MagicMock()
                provider.fetch_domain_info.return_value = {
                    "primary_domain": "guimingpian.com",
                    "domains": ["guimingpian.com"],
                    "count": 1,
                }
                with patch.object(
                    web_app, "MaddyWebuiProvider", return_value=provider
                ) as ctor:
                    result = web_app._maddy_webui_domains_payload(
                        cfg,
                        web_app.MaddyWebuiDomainsRequest(api_key=redacted),
                    )

                self.assertTrue(result["ok"])
                self.assertEqual(ctor.call_args.args[0]["api_key"], "saved-key")

    def test_panel_maddy_save_ignores_redacted_keys(self):
        import web_app

        for redacted in ("maddy_…acf3", "••••••••", "maddy_***_acf3"):
            with self.subTest(redacted=redacted):
                cfg = {
                    "mail": {
                        "providers": [
                            {
                                "type": "maddy_webui",
                                "enable": True,
                                "api_base": "https://admin.example/api",
                                "api_key": "saved-key",
                                "default_domain": "guimingpian.com",
                                "domains": ["guimingpian.com"],
                                "mailboxes": "",
                            }
                        ]
                    }
                }
                body = web_app.MailboxesUpdate(
                    provider_type="maddy_webui",
                    api_key=redacted,
                    mailboxes="",
                )
                with (
                    patch.object(web_app, "_read_config", return_value=cfg),
                    patch.object(web_app, "_write_config") as write_config,
                    patch.object(web_app, "_public_mail_stats", return_value={}),
                ):
                    result = asyncio.run(web_app.put_mailboxes(body))

                self.assertTrue(result["ok"])
                self.assertEqual(cfg["mail"]["providers"][0]["api_key"], "saved-key")
                write_config.assert_called_once_with(cfg)

    def test_maddy_auto_config_clamps_schedule_settings(self):
        import web_app

        settings = web_app._maddy_auto_config(
            {
                "registration": {"threads": 7, "engine": "protocol"},
                "maddy_auto_register": {
                    "enabled": True,
                    "interval_hours": 0,
                    "per_domain": 999,
                    "concurrency": 99,
                    "engine": "hybrid",
                    "keep_on_fail": True,
                    "refresh_domains": False,
                    "group_id": "group-fixture",
                },
            }
        )

        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["interval_hours"], 1)
        self.assertEqual(settings["per_domain"], 500)
        self.assertEqual(settings["concurrency"], 20)
        self.assertEqual(settings["engine"], "hybrid")
        self.assertTrue(settings["keep_on_fail"])
        self.assertFalse(settings["refresh_domains"])
        self.assertEqual(settings["group_id"], "group-fixture")

    def test_maddy_auto_domain_override_targets_single_domain(self):
        import web_app

        cfg = {
            "mail": {
                "providers": [
                    {
                        "type": "maddy_webui",
                        "default_domain": "xb1219.com",
                        "domain": "xb1219.com",
                        "domains": ["xb1219.com", "unjome.com"],
                    }
                ]
            }
        }

        web_app._apply_maddy_domain_to_config(cfg, "guimingpian.com")

        provider = cfg["mail"]["providers"][0]
        self.assertEqual(provider["default_domain"], "guimingpian.com")
        self.assertEqual(provider["domain"], "guimingpian.com")
        self.assertEqual(provider["domains"][0], "guimingpian.com")
        self.assertNotIn("xb1219.com", provider["domains"])
        self.assertIn("xb1219.com", provider["blocked_domains"])


class FastmailApiMailTests(unittest.TestCase):
    def _config(self, **overrides):
        entry = {
            "type": "fastmail_api",
            "enable": True,
            "api_base": "http://mailcode.example",
            "admin_key": "admin-fixture",
            "accounts": "master@fastmail.com",
            "capacity": 23,
            "mailboxes": "",
        }
        entry.update(overrides)
        return {"providers": [entry]}

    def _provider(self, entry_overrides=None):
        import mail

        entry = self._config(**(entry_overrides or {}))["providers"][0]
        entry["provider_ref"] = "fastmail_api#1"
        provider = mail.FastmailApiProvider(
            entry,
            {
                "request_timeout": 8,
                "wait_timeout": 10,
                "wait_interval": 0.2,
                "wait_chunk": 5,
                "user_agent": "test-agent",
            },
        )
        return provider

    def test_fastmail_alias_normalization(self):
        import mail

        self.assertEqual(normalize_provider_type("fastmail"), "fastmail_api")
        self.assertEqual(normalize_provider_type("mailcode"), "fastmail_api")
        self.assertEqual(normalize_provider_type("mailcode_api"), "fastmail_api")
        self.assertIn("fastmail_api", mail.CANONICAL_PROVIDER_TYPES)

    def test_fastmail_parse_accounts_tolerates_password_suffix(self):
        from mail.parsing import parse_fastmail_accounts

        accounts = parse_fastmail_accounts(
            "# comment\n\nmaster@fastmail.com----secret\nother@fastmail.jp\nmaster@fastmail.com\n"
        )
        self.assertEqual(accounts, ["master@fastmail.com", "other@fastmail.jp"])

    def test_fastmail_create_mailbox_acquires_alias_and_persists_metadata(self):
        import mail

        accounts_response = MagicMock(status_code=200, text="")
        accounts_response.json.return_value = {
            "ok": True,
            "accounts": [
                {
                    "email": "master@fastmail.com",
                    "enabled": True,
                    "aliasCount": 10,
                    "availableCount": 5,
                }
            ],
        }
        acquire_response = MagicMock(status_code=200, text="")
        acquire_response.json.return_value = {
            "ok": True,
            "alias": {
                "address": "q77nf1zwxl43pf@fastmail.jp",
                "key": "alias_fixture",
            },
        }
        session = MagicMock()
        session.request.side_effect = [accounts_response, acquire_response]
        with tempfile.TemporaryDirectory() as td, patch.object(
            mail, "STATE_FILE", Path(td) / "state.json"
        ):
            provider = self._provider()
            provider.session = session
            try:
                mailbox = provider.create_mailbox()
            finally:
                provider.close()
            state = json.loads(mail.STATE_FILE.read_text(encoding="utf-8"))

        self.assertEqual(mailbox["provider"], "fastmail_api")
        self.assertEqual(mailbox["address"], "q77nf1zwxl43pf@fastmail.jp")
        self.assertEqual(mailbox["account"], "master@fastmail.com")
        self.assertEqual(mailbox["alias_key"], "alias_fixture")
        entry = state["q77nf1zwxl43pf@fastmail.jp"]
        self.assertEqual(entry["state"], "in_use")
        self.assertEqual(entry["provider"], "fastmail_api")
        self.assertEqual(entry["account"], "master@fastmail.com")
        self.assertEqual(entry["key"], "alias_fixture")
        calls = session.request.call_args_list
        self.assertEqual(
            calls[0].args[:2], ("GET", "http://mailcode.example/api/v1/accounts")
        )
        self.assertEqual(calls[0].kwargs["headers"]["X-API-Key"], "admin-fixture")
        self.assertEqual(
            calls[1].args[:2],
            ("POST", "http://mailcode.example/api/v1/accounts/master%40fastmail.com/pool/acquire"),
        )

    def test_fastmail_pool_exhausted_reports_remote_summary(self):
        import mail

        accounts_response = MagicMock(status_code=200, text="")
        accounts_response.json.return_value = {
            "ok": True,
            "accounts": [
                {
                    "email": "master@fastmail.com",
                    "enabled": True,
                    "aliasCount": 10,
                    "availableCount": 0,
                }
            ],
        }
        session = MagicMock()
        session.request.return_value = accounts_response
        provider = self._provider()
        provider.session = session
        try:
            with self.assertRaises(RuntimeError) as ctx:
                provider.create_mailbox()
        finally:
            provider.close()
        self.assertIn("pool exhausted", str(ctx.exception))
        self.assertIn("available=0/10", str(ctx.exception))

    def test_fastmail_wait_for_code_filters_alerts_and_stale_codes(self):
        import mail

        provider = self._provider()
        mailbox = {
            "provider": "fastmail_api",
            "address": "alias@fastmail.jp",
            "account": "master@fastmail.com",
            "alias_key": "alias_fixture",
        }
        mail.mark_mailbox_code_boundary(mailbox, grace_seconds=0)
        now = datetime.now(timezone.utc)
        stale = {
            "ok": True,
            "code": "111111",
            "mail": {
                "subject": "Your verification code",
                "received_at": (now - timedelta(hours=1)).isoformat(),
            },
        }
        alert = {
            "ok": True,
            "code": "222222",
            "mail": {
                "subject": "New sign-in detected",
                "received_at": now.isoformat(),
            },
        }
        fresh = {
            "ok": True,
            "code": "654321",
            "mail": {
                "subject": "Verification code",
                "received_at": now.isoformat(),
            },
        }
        responses = []
        for payload in (stale, alert, fresh):
            resp = MagicMock(status_code=200, text="")
            resp.json.return_value = payload
            responses.append(resp)
        session = MagicMock()
        session.get.side_effect = responses
        provider.session = session
        try:
            code = provider.wait_for_code(mailbox)
        finally:
            provider.close()
        self.assertEqual(code, "654321")
        self.assertEqual(mailbox["_seen_otp_codes"], ["111111", "222222"])
        self.assertEqual(session.get.call_count, 3)
        params = session.get.call_args_list[0].kwargs["params"]
        self.assertEqual(params["account"], "master@fastmail.com")
        self.assertEqual(params["email"], "alias@fastmail.jp")
        self.assertEqual(params["key"], "alias_fixture")

    def test_fastmail_wait_for_code_raises_on_invalid_key(self):
        import mail

        resp = MagicMock(status_code=404, text="invalid email or key")
        resp.json.return_value = {"ok": False, "error": "invalid email or key"}
        session = MagicMock()
        session.get.return_value = resp
        provider = self._provider()
        provider.session = session
        mailbox = {
            "provider": "fastmail_api",
            "address": "alias@fastmail.jp",
            "account": "master@fastmail.com",
            "alias_key": "alias_fixture",
        }
        try:
            with self.assertRaises(mail.FastmailApiError):
                provider.wait_for_code(mailbox)
        finally:
            provider.close()
        self.assertGreaterEqual(session.get.call_count, 2)

    def test_fastmail_capacity_uses_remote_summary(self):
        import mail

        with (
            patch.object(mail, "_known_mailbox_bindings", return_value={}),
            patch.object(mail, "reconcile_registered_mailbox_states", return_value=0),
            patch.object(mail, "_load_state", return_value={}),
            patch.object(mail.FastmailApiProvider, "pool_summary") as summary,
        ):
            summary.return_value = {
                "available": 7,
                "total": 9,
                "accounts": 1,
                "error": "",
            }
            stats = mail.count_pool_capacity(self._config(), provider_type="fastmail")
        self.assertEqual(stats["total"], 9)
        self.assertEqual(stats["available"], 7)

    def test_fastmail_capacity_falls_back_when_service_unreachable(self):
        import mail

        with (
            patch.object(mail, "_known_mailbox_bindings", return_value={}),
            patch.object(mail, "reconcile_registered_mailbox_states", return_value=0),
            patch.object(mail, "_load_state", return_value={}),
            patch.object(mail.FastmailApiProvider, "pool_summary") as summary,
        ):
            summary.return_value = {
                "available": 0,
                "total": 0,
                "accounts": 0,
                "error": "connection refused",
            }
            stats = mail.count_pool_capacity(self._config(), provider_type="fastmail")
        self.assertEqual(stats["total"], 23)
        self.assertEqual(stats["available"], 23)

    def test_fastmail_otp_timeout_releases_local_reservation(self):
        import mail

        with tempfile.TemporaryDirectory() as td, patch.object(
            mail, "STATE_FILE", Path(td) / "state.json"
        ):
            mail._set_state("alias@fastmail.jp", "in_use")
            mailbox = {"provider": "fastmail_api", "address": "alias@fastmail.jp"}
            mail.mark_mailbox_result(
                mailbox, success=False, error="等待注册验证码超时"
            )
            state = json.loads(mail.STATE_FILE.read_text(encoding="utf-8"))
        self.assertNotIn("alias@fastmail.jp", state)

    def test_fastmail_invalid_key_error_marks_token_invalid(self):
        import mail

        with tempfile.TemporaryDirectory() as td, patch.object(
            mail, "STATE_FILE", Path(td) / "state.json"
        ):
            mail._set_state("alias2@fastmail.jp", "in_use")
            mailbox = {"provider": "fastmail_api", "address": "alias2@fastmail.jp"}
            mail.mark_mailbox_result(
                mailbox,
                success=False,
                error=mail.FastmailApiError("Fastmail API invalid email or key: bad"),
            )
            state = json.loads(mail.STATE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(state["alias2@fastmail.jp"]["state"], "token_invalid")

    def test_fastmail_state_metadata_survives_state_updates(self):
        import mail

        with tempfile.TemporaryDirectory() as td, patch.object(
            mail, "STATE_FILE", Path(td) / "state.json"
        ):
            with mail._mailbox_state_lock, mail._with_state_file_lock():
                store = mail._load_state_unlocked()
                store["alias3@fastmail.jp"] = {
                    "state": "in_use",
                    "reason": "",
                    "updated_at": "2026-01-01 00:00:00+0800",
                    "provider": "fastmail_api",
                    "account": "master@fastmail.com",
                    "key": "alias_fixture",
                }
                mail._save_state_unlocked(store)
            mail._set_state("alias3@fastmail.jp", "used", "registered_account")
            state = json.loads(mail.STATE_FILE.read_text(encoding="utf-8"))
        entry = state["alias3@fastmail.jp"]
        self.assertEqual(entry["state"], "used")
        self.assertEqual(entry["provider"], "fastmail_api")
        self.assertEqual(entry["key"], "alias_fixture")

    def test_fastmail_fetch_recent_messages_normalizes_graph_items(self):
        import mail

        resp = MagicMock(status_code=200, text="")
        resp.json.return_value = {
            "success": True,
            "data": {
                "items": [
                    {
                        "id": "m1",
                        "subject": "Verification code",
                        "from": {
                            "emailAddress": {
                                "name": "OpenAI",
                                "address": "noreply@tm.openai.com",
                            }
                        },
                        "receivedDateTime": "2026-01-01T00:00:00.000Z",
                        "bodyPreview": "Your code is 654321",
                    }
                ]
            },
        }
        session = MagicMock()
        session.get.return_value = resp
        provider = self._provider()
        provider.session = session
        mailbox = {
            "provider": "fastmail_api",
            "address": "alias@fastmail.jp",
            "account": "master@fastmail.com",
            "alias_key": "alias_fixture",
        }
        try:
            messages = provider.fetch_recent_messages(mailbox)
        finally:
            provider.close()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["message_id"], "m1")
        self.assertEqual(
            messages[0]["sender"], "OpenAI <noreply@tm.openai.com>"
        )
        self.assertEqual(messages[0]["recipients"], ["alias@fastmail.jp"])
        self.assertEqual(messages[0]["text_content"], "Your code is 654321")
        self.assertEqual(
            _extract_code(messages[0]), "654321"
        )


if __name__ == "__main__":
    unittest.main()

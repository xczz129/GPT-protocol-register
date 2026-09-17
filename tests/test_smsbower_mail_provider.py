from __future__ import annotations

import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

import mail
from mail.parsing import normalize_provider_type


def response(payload: dict, status_code: int = 200) -> MagicMock:
    item = MagicMock(status_code=status_code, text=json.dumps(payload))
    item.json.return_value = payload
    return item


class SmsBowerMailProviderTests(unittest.TestCase):
    def setUp(self):
        mail._SMSBOWER_MAIL_ACTIVATION_BREAKERS.clear()

    def _entry(self, **overrides):
        entry = {
            "type": "smsbower_mail",
            "enable": True,
            "provider_ref": "smsbower_mail#1",
            "api_base": "https://mail-api.example",
            "api_key": "mail-secret-fixture",
            "service": "dr",
            "domains": ["gmail.com", "icloud.com"],
            "max_price": 0.25,
        }
        entry.update(overrides)
        return entry

    def _provider(self, **overrides):
        return mail.SmsBowerMailProvider(
            self._entry(**overrides),
            {
                "request_timeout": 8,
                "wait_timeout": 2,
                "wait_interval": 0.2,
                "wait_chunk": 1,
                "user_agent": "test-agent",
            },
        )

    def test_provider_aliases_and_default_api_origin(self):
        self.assertEqual(normalize_provider_type("smsbower_email"), "smsbower_mail")
        self.assertEqual(normalize_provider_type("sms_bower_mail"), "smsbower_mail")
        self.assertIn("smsbower_mail", mail.CANONICAL_PROVIDER_TYPES)

        provider = mail.SmsBowerMailProvider(
            {"type": "smsbower_mail", "api_key": "fixture"},
            mail._make_config({}),
        )
        try:
            self.assertEqual(provider.api_base, "https://smsbower.page")
            self.assertEqual(provider.service, "dr")
            self.assertEqual(provider.domains, ["gmail.com"])
        finally:
            provider.close()

    def test_api_base_and_ordered_domains_are_normalized(self):
        provider = self._provider(
            api_base="smsbower.example/proxy/api/mail/getActivation",
            domains=["icloud", "google", "icloud.com"],
        )
        try:
            self.assertEqual(provider.api_base, "https://smsbower.example/proxy")
            self.assertEqual(provider.domains, ["icloud.com", "gmail.com"])
        finally:
            provider.close()

    def test_others_domain_adopts_returned_domain(self):
        """domain=others 买到随机域名邮箱时直接采用返回的域名。"""
        provider = self._provider(domains=["others"])
        session = MagicMock()
        session.get.side_effect = [
            response(
                {"status": 1, "mail": "picked@mailburstx.com", "mailId": "oth-1"}
            ),
        ]
        provider.session = session
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            mail, "STATE_FILE", Path(temp_dir) / "state.json"
        ):
            try:
                mailbox = provider.create_mailbox()
            finally:
                provider.close()

        self.assertEqual(mailbox["domain"], "mailburstx.com")
        self.assertEqual(
            session.get.call_args_list[0].kwargs["params"]["domain"], "others"
        )

    def test_non_finite_max_price_is_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf"), "Infinity"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "positive number"):
                    self._provider(max_price=value)

    def test_smsbower_wait_timeout_has_three_minute_floor(self):
        provider = self._provider()
        try:
            self.assertEqual(provider.wait_timeout, 180.0)
        finally:
            provider.close()

        conf = {
            "request_timeout": 8,
            "wait_timeout": 2,
            "smsbower_wait_timeout": 240,
            "wait_interval": 0.2,
            "wait_chunk": 1,
            "user_agent": "test-agent",
        }
        provider = mail.SmsBowerMailProvider(self._entry(), conf)
        try:
            self.assertEqual(provider.wait_timeout, 240.0)
        finally:
            provider.close()

    def test_services_and_price_rests_use_documented_endpoints(self):
        provider = self._provider(domains=["gmail.com"])
        session = MagicMock()
        session.get.side_effect = [
            response(
                {
                    "status": "success",
                    "services": [{"code": "dr", "name": "OpenAI"}],
                }
            ),
            response(
                {
                    "status": 1,
                    "data": {"dr": {"gmail.com": {"price": 0.2, "count": 7}}},
                }
            ),
        ]
        provider.session = session
        try:
            services = provider.get_mail_services_list()
            availability = provider.availability("gmail.com")
        finally:
            provider.close()

        self.assertEqual(services[0]["code"], "dr")
        self.assertEqual(availability["count"], 7)
        self.assertEqual(availability["price"], 0.2)
        self.assertEqual(
            session.get.call_args_list[0].args[0],
            "https://mail-api.example/stubs/handler_api.php",
        )
        self.assertEqual(
            session.get.call_args_list[0].kwargs["params"]["action"],
            "getMailServicesList",
        )
        self.assertEqual(
            session.get.call_args_list[1].args[0],
            "https://mail-api.example/api/mail/getPriceRests",
        )

    def test_create_mailbox_records_mail_id_without_persisting_api_key(self):
        provider = self._provider(domains=["gmail.com"])
        session = MagicMock()
        session.get.return_value = response(
            {"status": 1, "mail": "Fresh.Box@Gmail.com", "mailId": 41}
        )
        provider.session = session
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            mail, "STATE_FILE", Path(temp_dir) / "state.json"
        ):
            try:
                mailbox = provider.create_mailbox()
            finally:
                provider.close()
            state_text = mail.STATE_FILE.read_text(encoding="utf-8")
            state = json.loads(state_text)

        self.assertEqual(mailbox["provider"], "smsbower_mail")
        self.assertEqual(mailbox["address"], "fresh.box@gmail.com")
        self.assertEqual(mailbox["mail_id"], "41")
        self.assertEqual(mailbox["domain"], "gmail.com")
        self.assertEqual(mailbox["_smsbower_api_key"], "mail-secret-fixture")
        self.assertEqual(state["fresh.box@gmail.com"]["mail_id"], "41")
        self.assertNotIn("mail-secret-fixture", state_text)
        self.assertNotIn("api_key", state["fresh.box@gmail.com"])
        call = session.get.call_args
        self.assertEqual(
            call.args[0], "https://mail-api.example/api/mail/getActivation"
        )
        self.assertEqual(
            call.kwargs["params"],
            {
                "api_key": "mail-secret-fixture",
                "service": "dr",
                "domain": "gmail.com",
                "maxPrice": "0.25",
            },
        )

    def test_create_tries_next_domain_only_for_explicit_no_stock(self):
        provider = self._provider(domains=["gmail.com", "icloud.com"])
        session = MagicMock()
        session.get.side_effect = [
            response({"status": 0, "error": "No mails yet"}),
            response(
                {"status": 1, "mail": "picked@icloud.com", "mailId": "icloud-9"}
            ),
        ]
        provider.session = session
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            mail, "STATE_FILE", Path(temp_dir) / "state.json"
        ):
            try:
                mailbox = provider.create_mailbox()
            finally:
                provider.close()

        self.assertEqual(mailbox["domain"], "icloud.com")
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(
            [call.kwargs["params"]["domain"] for call in session.get.call_args_list],
            ["gmail.com", "icloud.com"],
        )

    def test_create_tries_next_domain_for_explicit_price_rejection(self):
        provider = self._provider(domains=["gmail.com", "icloud.com"])
        session = MagicMock()
        session.get.side_effect = [
            response(
                {
                    "status": 0,
                    "error": "Not available at this price. Try another",
                }
            ),
            response(
                {"status": 1, "mail": "picked@icloud.com", "mailId": "icloud-10"}
            ),
        ]
        provider.session = session
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            mail, "STATE_FILE", Path(temp_dir) / "state.json"
        ):
            try:
                mailbox = provider.create_mailbox()
            finally:
                provider.close()

        self.assertEqual(mailbox["domain"], "icloud.com")
        self.assertEqual(
            [call.kwargs["params"]["domain"] for call in session.get.call_args_list],
            ["gmail.com", "icloud.com"],
        )

    def test_create_does_not_try_another_domain_after_ambiguous_failure(self):
        provider = self._provider(domains=["gmail.com", "icloud.com"])
        secret = provider.api_key
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError(
            f"failed for https://mail-api.example/api/mail/getActivation?api_key={secret}&domain=gmail.com"
        )
        provider.session = session
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            mail, "STATE_FILE", Path(temp_dir) / "state.json"
        ):
            try:
                with self.assertRaises(mail.SmsBowerMailError) as raised:
                    provider.create_mailbox()
            finally:
                provider.close()

        self.assertEqual(session.get.call_count, 1)
        self.assertNotIn(secret, str(raised.exception))
        self.assertIn("api_key=***", str(raised.exception))

    def test_local_reservation_failure_cancels_activation_once(self):
        provider = self._provider(domains=["gmail.com"])
        session = MagicMock()
        session.get.side_effect = [
            response({"status": 1, "mail": "box@gmail.com", "mailId": "mail-2"}),
            response({"status": 1, "message": "Success"}),
        ]
        provider.session = session
        with patch.object(
            provider, "_reserve_address", side_effect=OSError("state write failed")
        ):
            try:
                with self.assertRaisesRegex(OSError, "state write failed"):
                    provider.create_mailbox()
            finally:
                provider.close()

        self.assertEqual(session.get.call_count, 2)
        cancel_call = session.get.call_args_list[1]
        self.assertTrue(cancel_call.args[0].endswith("/setStatus"))
        self.assertEqual(cancel_call.kwargs["params"]["id"], "mail-2")
        self.assertEqual(cancel_call.kwargs["params"]["status"], 2)

    def test_partial_activation_id_is_canceled_without_domain_failover(self):
        provider = self._provider(domains=["gmail.com", "icloud.com"])
        session = MagicMock()
        session.get.side_effect = [
            response({"status": 1, "mail": "invalid-address", "mailId": "mail-partial"}),
            response({"status": 1, "message": "Success"}),
        ]
        provider.session = session
        try:
            with self.assertRaises(mail.SmsBowerMailError) as raised:
                provider.create_mailbox()
        finally:
            provider.close()

        self.assertFalse(raised.exception.activation_outcome_uncertain)
        self.assertEqual(session.get.call_count, 2)
        cancel_call = session.get.call_args_list[1]
        self.assertTrue(cancel_call.args[0].endswith("/setStatus"))
        self.assertEqual(cancel_call.kwargs["params"]["id"], "mail-partial")
        self.assertEqual(cancel_call.kwargs["params"]["status"], 2)

    def test_partial_activation_cancel_failure_uses_synthetic_journal_key(self):
        provider = self._provider(domains=["gmail.com"])
        session = MagicMock()
        session.get.side_effect = [
            response({"status": 1, "mail": "invalid-address", "mailId": "mail-partial"}),
            requests.ConnectionError("cancel response lost"),
        ]
        provider.session = session
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            mail, "STATE_FILE", Path(temp_dir) / "state.json"
        ):
            try:
                with self.assertRaises(mail.SmsBowerMailError) as raised:
                    provider.create_mailbox()
            finally:
                provider.close()
            state_text = mail.STATE_FILE.read_text(encoding="utf-8")
            state = json.loads(state_text)

        self.assertTrue(raised.exception.activation_outcome_uncertain)
        self.assertEqual(len(state), 1)
        state_key, entry = next(iter(state.items()))
        self.assertTrue(state_key.startswith("__smsbower_pending__:"))
        self.assertEqual(entry["smsbower_pending_mail_id"], "mail-partial")
        self.assertEqual(entry["smsbower_pending_status"], 2)
        self.assertNotIn("mail-secret-fixture", state_text)

    def test_cancel_failure_before_reservation_creates_recovery_record(self):
        provider = self._provider(domains=["gmail.com"])
        session = MagicMock()
        session.get.side_effect = [
            response({"status": 1, "mail": "journal@gmail.com", "mailId": "mail-journal"}),
            requests.ConnectionError("cancel response lost"),
        ]
        provider.session = session
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            mail, "STATE_FILE", Path(temp_dir) / "state.json"
        ), patch.object(
            provider, "_reserve_address", side_effect=OSError("state write failed")
        ):
            try:
                with self.assertRaises(mail.SmsBowerMailError) as raised:
                    provider.create_mailbox()
            finally:
                provider.close()
            state_text = mail.STATE_FILE.read_text(encoding="utf-8")
            state = json.loads(state_text)["journal@gmail.com"]

        self.assertTrue(raised.exception.activation_outcome_uncertain)
        self.assertEqual(state["smsbower_pending_status"], 2)
        self.assertEqual(state["smsbower_pending_mail_id"], "mail-journal")
        self.assertTrue(state["smsbower_pending_key_hash"])
        self.assertNotIn("mail-secret-fixture", state_text)

    def test_known_account_address_is_canceled_instead_of_reserved(self):
        provider = self._provider(domains=["gmail.com"])
        session = MagicMock()
        session.get.side_effect = [
            response({"status": 1, "mail": "known@gmail.com", "mailId": "mail-known"}),
            response({"status": 1, "message": "Success"}),
        ]
        provider.session = session
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            mail, "STATE_FILE", Path(temp_dir) / "state.json"
        ), patch.object(
            mail,
            "_known_mailbox_bindings",
            return_value={"known+split@gmail.com": "registered_account"},
        ):
            try:
                with self.assertRaisesRegex(RuntimeError, "already bound"):
                    provider.create_mailbox()
            finally:
                provider.close()

        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(session.get.call_args_list[1].kwargs["params"]["status"], 2)

    def test_wrong_domain_response_is_canceled_and_not_reused(self):
        provider = self._provider(domains=["gmail.com", "icloud.com"])
        session = MagicMock()
        session.get.side_effect = [
            response(
                {"status": 1, "mail": "unexpected@icloud.com", "mailId": "mail-3"}
            ),
            response({"status": 1, "message": "Success"}),
        ]
        provider.session = session
        try:
            with self.assertRaisesRegex(
                mail.SmsBowerMailError, "icloud.com instead of gmail.com"
            ):
                provider.create_mailbox(allow_used=True)
        finally:
            provider.close()

        self.assertEqual(session.get.call_count, 2)
        self.assertTrue(session.get.call_args_list[1].args[0].endswith("/setStatus"))
        self.assertEqual(session.get.call_args_list[1].kwargs["params"]["status"], 2)

    def test_business_failure_and_set_status_are_never_retried(self):
        provider = self._provider(domains=["gmail.com"])
        session = MagicMock()
        session.get.return_value = response(
            {"status": 0, "error": "Insufficient balance"}
        )
        provider.session = session
        try:
            with self.assertRaisesRegex(
                mail.SmsBowerMailError, "Insufficient balance"
            ):
                provider.get_activation()
            self.assertEqual(session.get.call_count, 1)

            session.reset_mock()
            session.get.side_effect = requests.ConnectionError("connection reset")
            with self.assertRaises(mail.SmsBowerMailError):
                provider.set_status("mail-1", 2)
            self.assertEqual(session.get.call_count, 1)
        finally:
            provider.close()

    def test_wait_for_code_uses_jitter_and_requests_next_code_once(self):
        provider = self._provider(domains=["gmail.com"])
        mailbox = {
            "provider": "smsbower_mail",
            "address": "box@gmail.com",
            "mail_id": "mail-7",
        }
        session = MagicMock()
        session.get.side_effect = [
            response(
                {
                    "status": 0,
                    "error": "Code has not been received yet, please try again later",
                }
            ),
            response({"status": 1, "code": "123456"}),
        ]
        provider.session = session
        with (
            patch.object(mail.random, "uniform", return_value=0.1) as jitter,
            patch.object(mail.time, "sleep") as sleep,
        ):
            first_code = provider.wait_for_code(mailbox)
        self.assertEqual(first_code, "123456")
        jitter.assert_called()
        sleep.assert_called()
        self.assertEqual(mailbox["_smsbower_codes_delivered"], 1)

        session.get.reset_mock()
        session.get.side_effect = [
            response({"status": 1, "message": "Success"}),
            response({"status": 1, "code": "654321"}),
        ]
        with patch.object(mail.random, "uniform", return_value=0), patch.object(
            mail.time, "sleep"
        ):
            second_code = provider.wait_for_code(mailbox)
        provider.close()

        self.assertEqual(second_code, "654321")
        self.assertEqual(session.get.call_count, 2)
        self.assertTrue(session.get.call_args_list[0].args[0].endswith("/setStatus"))
        self.assertEqual(
            session.get.call_args_list[0].kwargs["params"]["status"], 5
        )
        self.assertTrue(session.get.call_args_list[1].args[0].endswith("/getCode"))
        self.assertEqual(mailbox["_smsbower_next_code_requests"], 1)

    def test_lost_next_code_status_response_is_not_replayed(self):
        provider = self._provider(domains=["gmail.com"])
        mailbox = {
            "provider": "smsbower_mail",
            "address": "box@gmail.com",
            "mail_id": "mail-next",
            "_smsbower_codes_delivered": 1,
        }
        with patch.object(
            provider,
            "set_status",
            side_effect=mail.SmsBowerMailError("response lost"),
        ) as set_status, patch.object(
            provider, "get_code", return_value="654321"
        ):
            with self.assertRaisesRegex(mail.SmsBowerMailError, "response lost"):
                provider.wait_for_code(mailbox)
            second_code = provider.wait_for_code(mailbox)
        provider.close()

        self.assertEqual(second_code, "654321")
        self.assertEqual(set_status.call_count, 1)
        self.assertEqual(mailbox["_smsbower_next_code_requests"], 1)

    def test_get_status_uses_mail_id(self):
        provider = self._provider()
        session = MagicMock()
        session.get.return_value = response(
            {
                "status": 1,
                "data": {
                    "status": 3,
                    "status_description": "Mail has been activated",
                    "available_to_get_next_code": False,
                    "last_code": "123456",
                },
            }
        )
        provider.session = session
        try:
            result = provider.get_status("mail-12")
        finally:
            provider.close()
        self.assertEqual(result["data"]["last_code"], "123456")
        self.assertEqual(session.get.call_args.kwargs["params"]["id"], "mail-12")

    def test_result_lifecycle_maps_to_independent_mail_statuses(self):
        base_mailbox = {
            "provider": "smsbower_mail",
            "provider_ref": "smsbower_mail#1",
            "api_base": "https://mail-api.example",
            "service": "dr",
            "domain": "gmail.com",
            "mail_id": "mail-99",
            "_smsbower_api_key": "mail-secret-fixture",
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            mail, "STATE_FILE", Path(temp_dir) / "state.json"
        ), patch.object(mail.SmsBowerMailProvider, "set_status") as set_status:
            successful = dict(base_mailbox, address="success@gmail.com")
            mail._set_state(successful["address"], "in_use")
            mail.mark_mailbox_result(successful, success=True)

            failed = dict(base_mailbox, address="failed@gmail.com", mail_id="mail-100")
            mail._set_state(failed["address"], "in_use")
            mail.mark_mailbox_result(failed, success=False, error="registration failed")

            released = dict(
                base_mailbox, address="released@gmail.com", mail_id="mail-101"
            )
            mail._set_state(released["address"], "in_use")
            mail.release_mailbox(released)
            state = json.loads(mail.STATE_FILE.read_text(encoding="utf-8"))

        self.assertEqual(
            [call.args for call in set_status.call_args_list],
            [("mail-99", 3), ("mail-100", 2), ("mail-101", 2)],
        )
        self.assertEqual(state["success@gmail.com"]["state"], "used")
        self.assertEqual(state["failed@gmail.com"]["state"], "failed")
        self.assertNotIn("released@gmail.com", state)

    def test_failed_terminal_status_is_persisted_without_api_key(self):
        mailbox = {
            "provider": "smsbower_mail",
            "provider_ref": "smsbower_mail#1",
            "address": "pending@gmail.com",
            "api_base": "https://mail-api.example",
            "service": "dr",
            "domain": "gmail.com",
            "mail_id": "mail-pending",
            "_smsbower_api_key": "mail-secret-fixture",
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            mail, "STATE_FILE", Path(temp_dir) / "state.json"
        ), patch.object(
            mail.SmsBowerMailProvider,
            "set_status",
            side_effect=mail.SmsBowerMailError("connection reset"),
        ):
            mail._set_state(mailbox["address"], "in_use")
            mail.release_mailbox(mailbox)
            state_text = mail.STATE_FILE.read_text(encoding="utf-8")
            state = json.loads(state_text)[mailbox["address"]]

        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["smsbower_pending_status"], 2)
        self.assertIn("connection reset", state["smsbower_settlement_error"])
        self.assertNotIn("mail-secret-fixture", state_text)

    def test_mark_result_keeps_pending_settlement_on_transient_failure(self):
        mailbox = {
            "provider": "smsbower_mail",
            "provider_ref": "smsbower_mail#1",
            "address": "transient@gmail.com",
            "api_base": "https://mail-api.example",
            "service": "dr",
            "domain": "gmail.com",
            "mail_id": "mail-transient",
            "_smsbower_api_key": "mail-secret-fixture",
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            mail, "STATE_FILE", Path(temp_dir) / "state.json"
        ), patch.object(
            mail.SmsBowerMailProvider,
            "set_status",
            side_effect=mail.SmsBowerMailError("curl: (35) connection closed"),
        ):
            mail._set_state(mailbox["address"], "in_use")
            mail.mark_mailbox_result(
                mailbox,
                success=False,
                error="curl: (35) connection closed",
            )
            state = json.loads(mail.STATE_FILE.read_text(encoding="utf-8"))[
                mailbox["address"]
            ]

        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["smsbower_pending_status"], 2)

    def test_generic_release_helpers_preserve_pending_settlements(self):
        failed = "pending-failed@gmail.com"
        stale = "pending-stale@gmail.com"
        pending_state = {
            failed: {
                "provider": "smsbower_mail",
                "state": "failed",
                "smsbower_pending_status": 2,
                "updated_at": "2000-01-01T00:00:00+00:00",
            },
            stale: {
                "provider": "smsbower_mail",
                "state": "in_use",
                "smsbower_pending_status": 3,
                "updated_at": "2000-01-01T00:00:00+00:00",
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            mail, "STATE_FILE", Path(temp_dir) / "state.json"
        ):
            mail._save_state(pending_state)
            self.assertEqual(mail.release_all_in_use(), 0)
            self.assertEqual(mail.release_failed_mailboxes(), 0)
            self.assertEqual(
                mail.update_mailbox_states([], action="release"),
                0,
            )
            self.assertEqual(mail.release_stale_in_use(), 0)
            remaining = mail._load_state()

        self.assertEqual(set(remaining), {failed, stale})

    def test_pending_settlement_is_reconciled_before_next_purchase(self):
        provider = self._provider(domains=["gmail.com"])
        pending = "pending-recovery@gmail.com"
        session = MagicMock()
        session.get.side_effect = [
            response({"status": 1, "data": {"status": 1}}),
            response({"status": 1, "message": "Success"}),
        ]
        provider.session = session
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            mail, "STATE_FILE", Path(temp_dir) / "state.json"
        ):
            mail._save_state(
                {
                    pending: {
                        "provider": "smsbower_mail",
                        "provider_ref": "smsbower_mail#1",
                        "state": "failed",
                        "mail_id": "mail-recovery",
                        "smsbower_pending_status": 2,
                    }
                }
            )
            recovered = provider._recover_pending_settlements()
            state = mail._load_state()[pending]
        provider.close()

        self.assertEqual(recovered, 1)
        self.assertNotIn("smsbower_pending_status", state)
        self.assertTrue(session.get.call_args_list[0].args[0].endswith("/getStatus"))
        self.assertTrue(session.get.call_args_list[1].args[0].endswith("/setStatus"))
        self.assertEqual(session.get.call_args_list[1].kwargs["params"]["status"], 2)

    def test_other_remote_terminal_status_is_not_changed(self):
        provider = self._provider(domains=["gmail.com"])
        pending = "already-terminal@gmail.com"
        mailbox = provider._settlement_mailbox(pending, "mail-terminal", "gmail.com")
        session = MagicMock()
        session.get.return_value = response({"status": 1, "data": {"status": 3}})
        provider.session = session
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            mail, "STATE_FILE", Path(temp_dir) / "state.json"
        ):
            mail._update_smsbower_settlement_state(mailbox, pending_status=2)
            recovered = provider._recover_pending_settlements()
            state = mail._load_state()[pending]
        provider.close()

        self.assertEqual(recovered, 1)
        self.assertNotIn("smsbower_pending_status", state)
        self.assertEqual(session.get.call_count, 1)
        self.assertTrue(session.get.call_args.args[0].endswith("/getStatus"))

    def test_provider_closed_status_6_is_terminal(self):
        provider = self._provider(domains=["gmail.com"])
        pending = "provider-closed@gmail.com"
        mailbox = provider._settlement_mailbox(pending, "mail-closed", "gmail.com")
        session = MagicMock()
        session.get.return_value = response({"status": 1, "data": {"status": 6}})
        provider.session = session
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            mail, "STATE_FILE", Path(temp_dir) / "state.json"
        ):
            mail._update_smsbower_settlement_state(mailbox, pending_status=2)
            recovered = provider._recover_pending_settlements()
            state = mail._load_state()[pending]
        provider.close()

        self.assertEqual(recovered, 1)
        self.assertNotIn("smsbower_pending_status", state)
        self.assertEqual(session.get.call_count, 1)
        self.assertTrue(session.get.call_args.args[0].endswith("/getStatus"))

    def test_unsupported_remote_status_stops_blocking_after_window(self):
        provider = self._provider(domains=["gmail.com"])
        pending = "stale-unsupported@gmail.com"
        mailbox = provider._settlement_mailbox(pending, "mail-unsupported", "gmail.com")
        session = MagicMock()
        session.get.return_value = response({"status": 1, "data": {"status": 7}})
        provider.session = session
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            mail, "STATE_FILE", Path(temp_dir) / "state.json"
        ):
            mail._update_smsbower_settlement_state(mailbox, pending_status=2)
            with patch.object(
                mail, "_smsbower_settlement_age_seconds", return_value=600.0
            ):
                recovered = provider._recover_pending_settlements()
            state = mail._load_state()[pending]
        provider.close()

        self.assertEqual(recovered, 1)
        self.assertNotIn("smsbower_pending_status", state)

    def test_provider_reordering_uses_stable_hashed_identity(self):
        old_provider = self._provider(provider_ref="smsbower_mail#9")
        current_provider = self._provider(provider_ref="smsbower_mail#1")
        pending = "reordered@gmail.com"
        mailbox = old_provider._settlement_mailbox(pending, "mail-reordered", "gmail.com")
        session = MagicMock()
        session.get.return_value = response({"status": 1, "data": {"status": 2}})
        current_provider.session = session
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            mail, "STATE_FILE", Path(temp_dir) / "state.json"
        ):
            mail._update_smsbower_settlement_state(mailbox, pending_status=2)
            recovered = current_provider._recover_pending_settlements()
            state = mail._load_state()[pending]
        old_provider.close()
        current_provider.close()

        self.assertEqual(recovered, 1)
        self.assertNotIn("smsbower_pending_status", state)
        self.assertEqual(session.get.call_count, 1)

    def test_concurrent_recovery_sends_terminal_action_once(self):
        first = self._provider()
        second = self._provider()
        pending = "concurrent@gmail.com"
        mailbox = first._settlement_mailbox(pending, "mail-concurrent", "gmail.com")
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            mail, "STATE_FILE", Path(temp_dir) / "state.json"
        ), patch.object(
            mail.SmsBowerMailProvider,
            "get_status",
            return_value={"status": 1, "data": {"status": 1}},
        ) as get_status, patch.object(
            mail.SmsBowerMailProvider,
            "set_status",
            return_value={"status": 1},
        ) as set_status:
            mail._update_smsbower_settlement_state(mailbox, pending_status=2)
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(
                    executor.map(
                        lambda provider: provider._recover_pending_settlements(),
                        (first, second),
                    )
                )
        first.close()
        second.close()

        self.assertEqual(sorted(results), [0, 1])
        self.assertEqual(get_status.call_count, 1)
        self.assertEqual(set_status.call_count, 1)

    def test_concurrent_finalizers_send_only_one_terminal_action(self):
        mailbox = {
            "provider": "smsbower_mail",
            "provider_ref": "smsbower_mail#1",
            "address": "terminal-race@gmail.com",
            "api_base": "https://mail-api.example",
            "service": "dr",
            "domain": "gmail.com",
            "mail_id": "mail-terminal-race",
            "_smsbower_api_key": "mail-secret-fixture",
        }

        def slow_set_status(*_args, **_kwargs):
            time.sleep(0.05)
            return {"status": 1}

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            mail, "STATE_FILE", Path(temp_dir) / "state.json"
        ), patch.object(
            mail.SmsBowerMailProvider,
            "set_status",
            side_effect=slow_set_status,
        ) as set_status:
            mail._set_state(mailbox["address"], "in_use")
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(
                    executor.map(
                        lambda status: mail._set_smsbower_mailbox_status(
                            mailbox, status
                        ),
                        (2, 3),
                    )
                )
            state = mail._load_state()[mailbox["address"]]

        self.assertEqual(results, [True, True])
        self.assertEqual(set_status.call_count, 1)
        self.assertIn(mailbox["_smsbower_terminal_status"], {2, 3})
        self.assertNotIn("smsbower_pending_status", state)

    def test_uncertain_activation_journal_survives_memory_reset(self):
        first = self._provider(domains=["gmail.com"])
        first_session = MagicMock()
        first_session.get.side_effect = requests.ConnectionError("activation response lost")
        first.session = first_session
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            mail, "STATE_FILE", Path(temp_dir) / "state.json"
        ):
            with self.assertRaises(mail.SmsBowerMailError):
                first.create_mailbox()
            first.close()
            state_text = mail.STATE_FILE.read_text(encoding="utf-8")
            mail._SMSBOWER_MAIL_ACTIVATION_BREAKERS.clear()

            second = self._provider(domains=["gmail.com"])
            second_session = MagicMock()
            second.session = second_session
            with self.assertRaises(mail.SmsBowerMailError) as raised:
                second.create_mailbox()
            second.close()

        self.assertTrue(raised.exception.activation_outcome_uncertain)
        second_session.get.assert_not_called()
        self.assertIn("smsbower_activation_outcome_unknown", state_text)
        self.assertNotIn("mail-secret-fixture", state_text)

    def test_uncertain_activation_journal_failure_never_fails_over(self):
        first = self._provider(domains=["gmail.com"])
        first.session = MagicMock()
        first.session.get.side_effect = requests.ConnectionError("activation response lost")
        second = MagicMock()
        config = {
            "providers": [
                self._entry(),
                self._entry(label="SMSBower fallback"),
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            mail, "STATE_FILE", Path(temp_dir) / "state.json"
        ), patch.object(
            mail, "reconcile_registered_mailbox_states", return_value=0
        ), patch.object(
            mail, "_build_provider", side_effect=[first, second]
        ) as build_provider, patch.object(
            mail,
            "_record_smsbower_activation_breaker",
            side_effect=OSError("journal unavailable"),
        ):
            with self.assertRaises(mail.SmsBowerMailError) as raised:
                mail.create_mailbox(config)

        self.assertTrue(raised.exception.activation_outcome_uncertain)
        build_provider.assert_called_once()
        second.create_mailbox.assert_not_called()
        self.assertTrue(mail._SMSBOWER_MAIL_ACTIVATION_BREAKERS)

    def test_stale_terminal_not_found_record_is_audit_cleared(self):
        provider = self._provider(domains=["gmail.com"])
        pending = "stale-not-found@gmail.com"
        mailbox = provider._settlement_mailbox(
            pending, "mail-not-found", "gmail.com"
        )
        session = MagicMock()
        session.get.return_value = response(
            {"status": 0, "error": "No activation found"}
        )
        provider.session = session
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            mail, "STATE_FILE", Path(temp_dir) / "state.json"
        ):
            mail._update_smsbower_settlement_state(mailbox, pending_status=2)
            state = mail._load_state()
            state[pending]["smsbower_settlement_started_at"] = (
                "2000-01-01T00:00:00+00:00"
            )
            mail._save_state(state)
            recovered = provider._recover_pending_settlements()
            state = mail._load_state()[pending]
        provider.close()

        self.assertEqual(recovered, 1)
        self.assertEqual(session.get.call_count, 1)
        self.assertNotIn("smsbower_pending_status", state)
        self.assertNotIn("smsbower_settlement_error", state)

    def test_unresolved_pending_settlement_blocks_new_purchase(self):
        provider = self._provider(domains=["gmail.com"])
        pending = "pending-block@gmail.com"
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("status lookup failed")
        provider.session = session
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            mail, "STATE_FILE", Path(temp_dir) / "state.json"
        ):
            mail._save_state(
                {
                    pending: {
                        "provider": "smsbower_mail",
                        "provider_ref": "smsbower_mail#1",
                        "state": "failed",
                        "mail_id": "mail-block",
                        "smsbower_pending_status": 3,
                    }
                }
            )
            with self.assertRaises(mail.SmsBowerMailError) as raised:
                provider.create_mailbox()
            state = mail._load_state()[pending]
        provider.close()

        self.assertTrue(raised.exception.activation_outcome_uncertain)
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(state["smsbower_pending_status"], 3)
        self.assertIn("status lookup failed", state["smsbower_settlement_error"])

    def test_pool_capacity_sums_configured_domains(self):
        config = {"providers": [self._entry(capacity=3)]}
        with (
            patch.object(mail, "_known_mailbox_bindings", return_value={}),
            patch.object(mail, "reconcile_registered_mailbox_states", return_value=0),
            patch.object(mail, "_load_state", return_value={}),
            patch.object(
                mail.SmsBowerMailProvider,
                "availability_summary",
                return_value={"count": 10},
            ) as summary,
        ):
            stats = mail.count_pool_capacity(
                config, provider_type="smsbower_email"
            )

        self.assertEqual(stats["total"], 10)
        self.assertEqual(stats["available"], 20)
        self.assertEqual(stats["total_slots"], 20)
        self.assertEqual(stats["primary_available"], 10)
        self.assertEqual(stats["split_available"], 10)
        summary.assert_called_once_with()

    def test_capacity_excludes_stock_above_configured_max_price(self):
        provider = self._provider(domains=["gmail.com"], max_price=0.25)
        session = MagicMock()
        session.get.return_value = response(
            {
                "status": 1,
                "data": {"dr": {"gmail.com": {"price": 0.3, "count": 7}}},
            }
        )
        provider.session = session
        try:
            availability = provider.availability("gmail.com")
        finally:
            provider.close()

        self.assertEqual(availability["count"], 0)
        self.assertEqual(availability["raw_count"], 7)
        self.assertTrue(availability["blocked_by_price"])
        self.assertEqual(availability["price"], 0.3)

    def test_capacity_ignores_non_finite_remote_values(self):
        provider = self._provider(domains=["gmail.com"], max_price=0.25)
        session = MagicMock()
        session.get.return_value = response(
            {
                "status": 1,
                "data": {
                    "dr": {
                        "gmail.com": {"price": "NaN", "count": "Infinity"}
                    }
                },
            }
        )
        provider.session = session
        try:
            availability = provider.availability("gmail.com")
        finally:
            provider.close()

        self.assertEqual(availability["count"], 0)
        self.assertEqual(availability["raw_count"], 0)
        self.assertIsNone(availability["price"])
        self.assertFalse(availability["blocked_by_price"])

    def test_availability_summary_cache_hashes_key_and_avoids_repeat_calls(self):
        provider = self._provider()
        with patch.object(
            provider,
            "availability",
            side_effect=[
                {"domain": "gmail.com", "count": 4, "price": 0.2},
                {"domain": "icloud.com", "count": 6, "price": 0.3},
            ],
        ) as availability:
            first = provider.availability_summary(force=True)
            second = provider.availability_summary()
        provider.close()

        self.assertEqual(first["count"], 10)
        self.assertEqual(second["count"], 10)
        self.assertEqual(availability.call_count, 2)
        cache_keys = list(mail._SMSBOWER_MAIL_CAPACITY_CACHE)
        self.assertTrue(cache_keys)
        self.assertNotIn("mail-secret-fixture", repr(cache_keys))

    def test_top_level_mail_pool_builds_and_closes_provider(self):
        config = {"providers": [self._entry(domains=["icloud.com"])]}
        session = MagicMock()
        session.get.return_value = response(
            {"status": 1, "mail": "pool@icloud.com", "mailId": "pool-1"}
        )
        with tempfile.TemporaryDirectory() as temp_dir, (
            patch.object(mail, "STATE_FILE", Path(temp_dir) / "state.json")
        ), patch.object(mail, "_http_session", return_value=session), patch.object(
            mail, "reconcile_registered_mailbox_states", return_value=0
        ):
            mailbox = mail.create_mailbox(
                config, provider_type="smsbower_email"
            )

        self.assertEqual(mailbox["provider"], "smsbower_mail")
        self.assertEqual(mailbox["domain"], "icloud.com")
        session.close.assert_called_once()

    def test_uncertain_activation_stops_top_level_provider_failover(self):
        uncertain = mail.SmsBowerMailError(
            "activation response lost",
            activation_outcome_uncertain=True,
        )
        for provider_type in (None, "smsbower_mail"):
            with self.subTest(provider_type=provider_type or "auto"):
                first = MagicMock()
                first.create_mailbox.side_effect = uncertain
                second = MagicMock()
                config = {
                    "providers": [
                        self._entry(),
                        self._entry(label="SMSBower Mail fallback"),
                    ]
                }
                with patch.object(
                    mail, "reconcile_registered_mailbox_states", return_value=0
                ), patch.object(
                    mail, "_build_provider", side_effect=[first, second]
                ) as build_provider:
                    with self.assertRaises(mail.SmsBowerMailError) as raised:
                        mail.create_mailbox(config, provider_type=provider_type)

                self.assertIs(raised.exception, uncertain)
                build_provider.assert_called_once()
                first.close.assert_called_once()
                second.create_mailbox.assert_not_called()


if __name__ == "__main__":
    unittest.main()

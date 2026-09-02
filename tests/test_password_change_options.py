# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import patch

from fastapi import HTTPException


class PasswordChangeOptionTests(unittest.TestCase):
    def test_custom_password_requires_twelve_characters(self):
        import web_app

        short = web_app.AccountPasswordChangeBatchRequest(
            keys=["account_id:fixture"],
            password_mode="custom",
            new_password="Aa1!short",
            confirm_password="Aa1!short",
        )
        with self.assertRaises(HTTPException) as raised:
            web_app._security_batch_password(short)
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("12", str(raised.exception.detail))

    def test_random_mode_does_not_accept_password_fields(self):
        import web_app

        request = web_app.AccountPasswordChangeBatchRequest(
            keys=["account_id:fixture"],
            password_mode="random",
        )
        self.assertEqual(web_app._security_batch_password(request), ("", ""))

        request.new_password = "Aa1!this-should-not-be-sent"
        with self.assertRaises(HTTPException) as raised:
            web_app._security_batch_password(request)
        self.assertEqual(raised.exception.status_code, 400)

    def test_generated_security_password_has_required_shape(self):
        import web_app

        value = web_app._generate_security_password()
        self.assertGreaterEqual(len(value), 12)
        self.assertRegex(value, r"[A-Z]")
        self.assertRegex(value, r"[a-z]")
        self.assertRegex(value, r"[0-9]")
        self.assertRegex(value, r"[!@#$%^&*]")

    def test_random_batch_generates_a_distinct_password_for_each_account(self):
        import web_app

        captured: list[str] = []

        def change(account, new_password, **kwargs):
            self.assertEqual(kwargs["password_mode"], "custom")
            captured.append(new_password)
            self.assertGreaterEqual(len(new_password), 12)
            return {**account, "password": new_password}

        accounts = [
            {"email": "one@example.com"},
            {"email": "two@example.com"},
        ]
        with patch.object(web_app, "_change_account_password_sync", side_effect=change):
            for account in accounts:
                result = web_app.AccountReauthBatchJobs._invoke_reauthenticate(
                    account,
                    "protocol",
                    threading.Event(),
                    "password_change",
                    {"password_mode": "random"},
                )
                self.assertEqual(result["password"], captured[-1])

        self.assertEqual(len(captured), 2)
        self.assertNotEqual(captured[0], captured[1])
        self.assertTrue(all(len(value) >= 12 for value in captured))

    def test_random_password_is_not_retained_in_job_snapshot(self):
        import web_app

        jobs = web_app.AccountReauthBatchJobs()
        account = {"email": "snapshot@example.com", "account_id": "snapshot"}
        generated = "Aa1!generated-secret"
        with patch.object(
            web_app,
            "_change_account_password_sync",
            return_value={**account, "password": generated},
        ):
            started = jobs.start(
                [account],
                mode="protocol",
                operation="password_change",
                operation_options={"password_mode": "random"},
            )
            self.assertTrue(jobs.wait(started["job_id"], timeout=3))
            snapshot = jobs.snapshot(started["job_id"])

        self.assertNotIn(generated, json.dumps(snapshot, ensure_ascii=False))
        self.assertNotIn("new_password", snapshot)


if __name__ == "__main__":
    unittest.main()

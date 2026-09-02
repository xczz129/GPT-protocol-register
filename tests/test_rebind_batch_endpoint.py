from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import patch

import web.routes.accounts_rebind as rebind_routes


class RebindBatchEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        import web_app

        self.web_app = web_app
        with web_app._REBIND_RESERVATION_LOCK:
            web_app._REBIND_RESERVATIONS.clear()

    def tearDown(self) -> None:
        with self.web_app._REBIND_RESERVATION_LOCK:
            self.web_app._REBIND_RESERVATIONS.clear()

    @staticmethod
    def _record(email: str, account_id: str) -> dict:
        return {
            "email": email,
            "account_id": account_id,
            "password": "saved-password",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }

    def test_preflight_reserves_each_batch_target_and_reports_items(self):
        web_app = self.web_app
        records = {
            "one@example.com": self._record("one@example.com", "one"),
            "two@example.com": self._record("two@example.com", "two"),
        }
        allocated: list[str] = []

        def reserve(_cfg, *, target_email, provider_type):
            allocated.append(target_email)
            address = target_email or f"auto-{len(allocated)}@example.net"
            return {"provider": provider_type, "address": address}

        body = web_app.AccountRebindPreflightRequest(
            text="one@example.com----one-target@example.net\ntwo@example.com----two-target@example.net",
            provider_type="domain_api",
        )
        with (
            patch.object(web_app, "_read_config", return_value={"mail": {}}),
            patch.object(rebind_routes, "_rebind_load_record", side_effect=lambda value: records.get(value.lower())),
            patch.object(rebind_routes, "_rebind_reserve_target", side_effect=reserve),
            patch.object(web_app, "account_is_deactivated", return_value=False),
        ):
            result = asyncio.run(web_app.account_rebind_preflight(body))

        self.assertTrue(result["eligible"])
        self.assertEqual(result["requested"], 2)
        self.assertEqual(result["matched"], 2)
        self.assertEqual(result["eligible_count"], 2)
        self.assertEqual(allocated, ["one-target@example.net", "two-target@example.net"])
        self.assertEqual(
            {item["target"]["email"] for item in result["items"]},
            {"one-target@example.net", "two-target@example.net"},
        )
        reservation_id = result["reservation_id"]
        self.assertTrue(reservation_id)
        with web_app._REBIND_RESERVATION_LOCK:
            reservation = web_app._REBIND_RESERVATIONS[reservation_id]
            self.assertTrue(reservation.get("batch"))
            self.assertEqual(len(reservation["items"]), 2)

    def test_start_claims_batch_and_passes_matching_reservations_to_job(self):
        web_app = self.web_app
        first = self._record("one@example.com", "one")
        second = self._record("two@example.com", "two")
        reservation_id = "batch-start-fixture"
        batch = {
            "batch": True,
            "created_monotonic": time.monotonic(),
            "config": {"mail": {}, "registration": {}, "proxy": {}},
            "items": [
                {
                    "record_key": web_app._account_key(first),
                    "source": web_app._rebind_public_source(first),
                    "mailbox": {"provider": "domain_api", "address": "one-target@example.net"},
                    "email": "one-target@example.net",
                },
                {
                    "record_key": web_app._account_key(second),
                    "source": web_app._rebind_public_source(second),
                    "mailbox": {"provider": "domain_api", "address": "two-target@example.net"},
                    "email": "two-target@example.net",
                },
            ],
        }
        with web_app._REBIND_RESERVATION_LOCK:
            web_app._REBIND_RESERVATIONS[reservation_id] = batch
        captured = {}

        def fake_start(records, reservations, *, concurrency, worker):
            captured.update(records=records, reservations=reservations, concurrency=concurrency)
            return {"job_id": "job-fixture", "status": "queued", "total": len(records), "items": []}

        with (
            patch.object(rebind_routes, "_rebind_load_record", side_effect=lambda value: {web_app._account_key(first): first, web_app._account_key(second): second}.get(value)),
            patch.object(rebind_routes, "_rebind_target_conflict", return_value=""),
            patch.object(web_app.ACCOUNT_REBIND_JOBS, "start", side_effect=fake_start),
        ):
            result = asyncio.run(
                web_app.account_rebind_start(
                    web_app.AccountRebindStartRequest(
                        reservation_id=reservation_id,
                        concurrency=2,
                    )
                )
            )

        self.assertEqual(result["job_id"], "job-fixture")
        self.assertEqual(result["skipped"], [])
        self.assertEqual(captured["concurrency"], 2)
        self.assertEqual({record["email"] for record in captured["records"]}, {first["email"], second["email"]})
        self.assertEqual(
            {reservation["email"] for reservation in captured["reservations"].values()},
            {"one-target@example.net", "two-target@example.net"},
        )
        with web_app._REBIND_RESERVATION_LOCK:
            self.assertNotIn(reservation_id, web_app._REBIND_RESERVATIONS)

    def test_preflight_rejects_duplicate_addresses_returned_by_auto_pool(self):
        """A provider race must not put the same mailbox in two batch rows."""
        web_app = self.web_app
        records = {
            "one@example.com": self._record("one@example.com", "one"),
            "two@example.com": self._record("two@example.com", "two"),
        }
        duplicate = {"provider": "domain_api", "address": "same-target@example.net"}
        with (
            patch.object(web_app, "_read_config", return_value={"mail": {}}),
            patch.object(rebind_routes, "_rebind_load_record", side_effect=lambda value: records.get(value.lower())),
            patch.object(rebind_routes, "_rebind_reserve_target", return_value=dict(duplicate)),
            patch.object(web_app, "account_is_deactivated", return_value=False),
            patch.object(web_app, "release_mailbox") as release,
        ):
            result = asyncio.run(
                web_app.account_rebind_preflight(
                    web_app.AccountRebindPreflightRequest(
                        text="one@example.com\ntwo@example.com",
                    )
                )
            )

        self.assertEqual(result["eligible_count"], 1)
        self.assertEqual(len(result["items"]), 2)
        statuses = {item["input"]: item["status"] for item in result["items"]}
        self.assertEqual(statuses["one@example.com"], "eligible")
        self.assertEqual(statuses["two@example.com"], "target_conflict")
        # The duplicate candidate is returned immediately; the first remains
        # reserved for the batch and is released by tearDown.
        release.assert_called_once_with(duplicate)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import common.account_store as account_store


class SQLiteAccountStoreTests(unittest.TestCase):
    def test_production_path_migrates_and_uses_indexed_views(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "registered_accounts.json"
            database = root / "registered_accounts.sqlite3"
            legacy.write_text(
                json.dumps(
                    [
                        {
                            "email": "first@example.com",
                            "password": "secret",
                            "access_token": "token",
                            "cookies": [{"name": "session", "value": "large-secret"}],
                            "group_id": "group-a",
                            "registered_at": "2026-01-01T00:00:00Z",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            with (
                patch.object(account_store, "ACCOUNTS_FILE", legacy),
                patch.object(account_store, "ACCOUNTS_DB_FILE", database),
            ):
                migrated = account_store.load_accounts(legacy)
                saved = account_store.upsert_account_record(
                    {
                        "email": "first@example.com",
                        "account_id": "account-1",
                        "health_status": "alive",
                    },
                    legacy,
                )
                public = account_store.load_account_public_views(10, legacy)

                self.assertEqual(len(migrated), 1)
                self.assertEqual(saved["password"], "secret")
                self.assertEqual(account_store.count_accounts(legacy), 1)
                self.assertEqual(account_store.account_group_counts(legacy), {"group-a": 1})
                self.assertEqual(public[0]["delete_key"], "account_id:account-1")
                self.assertNotIn("access_token", public[0])
                self.assertNotIn("cookies", public[0])
                self.assertTrue(database.is_file())

    def test_replace_import_updates_sqlite_without_touching_json_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "registered_accounts.json"
            database = root / "registered_accounts.sqlite3"
            legacy.write_text("[]\n", encoding="utf-8")

            with (
                patch.object(account_store, "ACCOUNTS_FILE", legacy),
                patch.object(account_store, "ACCOUNTS_DB_FILE", database),
            ):
                account_store.load_accounts(legacy)
                account_store.replace_account_records(
                    [{"email": "restored@example.com", "group_id": "restored"}],
                    legacy,
                )
                rows = account_store.load_accounts(legacy)

            self.assertEqual([row["email"] for row in rows], ["restored@example.com"])
            self.assertEqual(legacy.read_text(encoding="utf-8"), "[]\n")

    def test_public_views_support_stable_offset_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "registered_accounts.json"
            database = root / "registered_accounts.sqlite3"
            records = [
                {
                    "email": f"account-{index}@example.com",
                    "registered_at": f"2026-01-0{index + 1}T00:00:00Z",
                }
                for index in range(5)
            ]
            legacy.write_text(json.dumps(records), encoding="utf-8")

            with (
                patch.object(account_store, "ACCOUNTS_FILE", legacy),
                patch.object(account_store, "ACCOUNTS_DB_FILE", database),
            ):
                complete = account_store.load_account_public_views(path=legacy)
                first = account_store.load_account_public_views(2, legacy)
                second = account_store.load_account_public_views(2, legacy, offset=2)
                last = account_store.load_account_public_views(2, legacy, offset=4)

            paged = first + second + last
            self.assertEqual(
                [item["delete_key"] for item in paged],
                [item["delete_key"] for item in complete],
            )

    def test_reassign_group_updates_index_and_payloads_in_one_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "registered_accounts.json"
            database = root / "registered_accounts.sqlite3"
            legacy.write_text(
                json.dumps(
                    [
                        {"email": "one@example.com", "group_id": "source"},
                        {"email": "two@example.com", "group_id": "source"},
                        {"email": "other@example.com", "group_id": "other"},
                    ]
                ),
                encoding="utf-8",
            )

            with (
                patch.object(account_store, "ACCOUNTS_FILE", legacy),
                patch.object(account_store, "ACCOUNTS_DB_FILE", database),
            ):
                account_store.load_accounts(legacy)
                changed = account_store.reassign_account_group(
                    "source", "target", path=legacy
                )
                complete = account_store.load_accounts(legacy)
                public = account_store.load_account_public_views(path=legacy)
                counts = account_store.account_group_counts(legacy)

            self.assertEqual(changed, 2)
            self.assertEqual(counts, {"other": 1, "target": 2})
            self.assertEqual(
                {item["group_id"] for item in complete if item["email"] != "other@example.com"},
                {"target"},
            )
            self.assertEqual(
                {item["group_id"] for item in public if item["email"] != "other@example.com"},
                {"target"},
            )

    def test_mutations_can_skip_full_snapshot_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "registered_accounts.json"
            database = root / "registered_accounts.sqlite3"
            legacy.write_text(
                json.dumps(
                    [
                        {"email": "one@example.com", "health_status": "unknown"},
                        {"email": "two@example.com", "health_status": "unknown"},
                    ]
                ),
                encoding="utf-8",
            )
            with (
                patch.object(account_store, "ACCOUNTS_FILE", legacy),
                patch.object(account_store, "ACCOUNTS_DB_FILE", database),
            ):
                account_store.load_accounts(legacy)
                changed, rows = account_store.update_account_records(
                    ["email:one@example.com"],
                    {"health_status": "alive"},
                    include_records=False,
                    path=legacy,
                )
                self.assertEqual((changed, rows), (1, []))
                changed, rows = account_store.update_account_record_fields(
                    {"email:two@example.com": {"health_status": "limited"}},
                    include_records=False,
                    path=legacy,
                )
                self.assertEqual((changed, rows), (1, []))
                removed, rows = account_store.delete_account_records(
                    ["email:one@example.com"],
                    include_records=False,
                    path=legacy,
                )
                self.assertEqual((removed, rows), (1, []))
                remaining = account_store.load_accounts(legacy)

            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["health_status"], "limited")

    def test_load_one_record_by_identity_uses_email_and_account_id_indexes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "registered_accounts.json"
            database = root / "registered_accounts.sqlite3"
            legacy.write_text(
                json.dumps(
                    [
                        {
                            "email": "first@example.com",
                            "password": "first-secret",
                        },
                        {
                            "email": "second@example.com",
                            "account_id": "acct-2",
                            "password": "second-secret",
                        },
                    ]
                ),
                encoding="utf-8",
            )

            with (
                patch.object(account_store, "ACCOUNTS_FILE", legacy),
                patch.object(account_store, "ACCOUNTS_DB_FILE", database),
            ):
                account_store.load_accounts(legacy)

                by_email = account_store.load_account_record_by_identity(
                    "FIRST@EXAMPLE.COM", path=legacy
                )
                by_email_prefix = account_store.load_account_record_by_identity(
                    "email:first@example.com", path=legacy
                )
                by_account_id = account_store.load_account_record_by_identity(
                    "acct-2", path=legacy
                )
                by_account_id_prefix = account_store.load_account_record_by_identity(
                    "account_id:ACCT-2", path=legacy
                )
                missing = account_store.load_account_record_by_identity(
                    "missing@example.com", path=legacy
                )

            self.assertEqual(by_email["password"], "first-secret")
            self.assertEqual(by_email_prefix["email"], "first@example.com")
            self.assertEqual(by_account_id["email"], "second@example.com")
            self.assertEqual(by_account_id_prefix["account_id"], "acct-2")
            self.assertIsNone(missing)

    def test_load_one_record_by_identity_keeps_json_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "accounts.json"
            path.write_text(
                json.dumps(
                    [
                        {"email": "legacy@example.com", "password": "legacy-secret"},
                    ]
                ),
                encoding="utf-8",
            )

            record = account_store.load_account_record_by_identity(
                "email:legacy@example.com", path=path
            )

            self.assertEqual(record["email"], "legacy@example.com")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import time
import unittest

from common.email_rebind_jobs import EmailRebindJobs


class EmailRebindJobsBatchTests(unittest.TestCase):
    @staticmethod
    def _wait_for_terminal(jobs: EmailRebindJobs, job_id: str, timeout: float = 3.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = jobs.snapshot(job_id)
            if snapshot and snapshot.get("status") not in {"queued", "running", "stopping"}:
                return snapshot
            time.sleep(0.01)
        raise AssertionError(f"rebind job did not finish: {jobs.snapshot(job_id)!r}")

    def test_start_runs_each_record_with_its_matching_reservation(self):
        jobs = EmailRebindJobs()
        records = [
            {"delete_key": "email:one@example.com", "email": "one@example.com", "account_id": "one"},
            {"delete_key": "email:two@example.com", "email": "two@example.com", "account_id": "two"},
        ]
        reservations = {
            "email:one@example.com": {"email": "new-one@example.com", "secret": "worker-only-one"},
            "email:two@example.com": {"email": "new-two@example.com", "secret": "worker-only-two"},
        }
        calls: list[tuple[int, str, str, str]] = []

        def worker(*, index, record, reservation, stop_check, task_event):
            self.assertFalse(stop_check())
            calls.append((index, record["email"], reservation["email"], reservation["secret"]))
            task_event(index, "running", step="processing")
            task_event(index, "success", new_email=reservation["email"])
            return {"new_email": reservation["email"], "account_id": record["account_id"]}

        started = jobs.start(records, reservations, concurrency=2, worker=worker)
        finished = self._wait_for_terminal(jobs, started["job_id"])

        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["total"], 2)
        self.assertEqual(finished["done"], 2)
        self.assertEqual(finished["success"], 2)
        self.assertEqual(finished["failed"], 0)
        self.assertEqual(
            {(email, target) for _, email, target, _ in calls},
            {
                ("one@example.com", "new-one@example.com"),
                ("two@example.com", "new-two@example.com"),
            },
        )
        self.assertEqual(
            [item["status"] for item in sorted(finished["items"], key=lambda item: item["email"])],
            ["success", "success"],
        )
        self.assertEqual(
            {item["new_email"] for item in finished["items"]},
            {"new-one@example.com", "new-two@example.com"},
        )
        # Provider credentials are passed to workers but never exposed in a
        # public progress snapshot.
        self.assertNotIn("worker-only-one", repr(finished))
        self.assertNotIn("worker-only-two", repr(finished))

    def test_worker_exception_marks_only_that_item_failed(self):
        jobs = EmailRebindJobs()
        records = [
            {"delete_key": "account_id:good", "email": "good@example.com", "account_id": "good"},
            {"delete_key": "account_id:bad", "email": "bad@example.com", "account_id": "bad"},
        ]
        reservations = {
            "account_id:good": {"email": "good-target@example.com"},
            "account_id:bad": {"email": "bad-target@example.com"},
        }

        def worker(*, record, reservation, **kwargs):
            if record["account_id"] == "bad":
                raise RuntimeError("fixture failure")
            return {"new_email": reservation["email"]}

        started = jobs.start(records, reservations, concurrency=1, worker=worker)
        finished = self._wait_for_terminal(jobs, started["job_id"])

        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["success"], 1)
        self.assertEqual(finished["failed"], 1)
        by_email = {item["email"]: item for item in finished["items"]}
        self.assertEqual(by_email["good@example.com"]["status"], "success")
        self.assertEqual(by_email["bad@example.com"]["status"], "failed")
        self.assertIn("fixture failure", by_email["bad@example.com"]["message"])


if __name__ == "__main__":
    unittest.main()

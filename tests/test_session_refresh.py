from __future__ import annotations

import base64
import json
import time
import unittest
from unittest.mock import patch

from common import session_refresh as sr


def _jwt(exp: float) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp}).encode()
    ).decode().rstrip("=")
    return f"{header}.{payload}.sig"


class _Response:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class SessionRefreshHelpersTest(unittest.TestCase):
    def test_jwt_expires_at_parses_exp(self):
        exp = time.time() + 3600
        self.assertEqual(sr.jwt_expires_at(_jwt(exp)), int(exp))
        self.assertEqual(sr.jwt_expires_at(""), 0)
        self.assertEqual(sr.jwt_expires_at("not-a-jwt"), 0)

    def test_needs_refresh_covers_missing_expired_and_near_expiry(self):
        self.assertTrue(sr.access_token_needs_refresh({}))
        self.assertTrue(sr.access_token_needs_refresh({"access_token": "junk"}))
        self.assertTrue(
            sr.access_token_needs_refresh({"access_token": _jwt(time.time() - 10)})
        )
        self.assertTrue(
            sr.access_token_needs_refresh({"access_token": _jwt(time.time() + 86400)})
        )
        self.assertFalse(
            sr.access_token_needs_refresh({"access_token": _jwt(time.time() + 30 * 86400)})
        )

    def test_refresh_session_via_service_normalizes_payload(self):
        new_at = _jwt(time.time() + 10 * 86400)
        payload = {
            "ok": True,
            "session": {
                "accessToken": new_at,
                "sessionToken": "rotated-session",
                "account": {"id": "acc-1", "planType": "plus"},
                "user": {"email": "a@b.c"},
            },
            "session_rotated": True,
        }
        with patch.object(sr.requests, "post", return_value=_Response(200, payload)) as post:
            result = sr.refresh_session_via_service("old-session")
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["json"], {"sessionToken": "old-session"})
        self.assertEqual(result["access_token"], new_at)
        self.assertEqual(result["session_token"], "rotated-session")
        self.assertEqual(result["plan_type"], "plus")
        self.assertEqual(result["account_id"], "acc-1")
        self.assertEqual(result["email"], "a@b.c")
        self.assertTrue(result["rotated"])

    def test_refresh_session_via_service_raises_on_rejection(self):
        payload = {"ok": False, "reason": "session-expired", "message": "session ended"}
        with patch.object(sr.requests, "post", return_value=_Response(401, payload)):
            with self.assertRaises(sr.SessionRefreshError) as ctx:
                sr.refresh_session_via_service("bad")
        self.assertIn("session_refresh_http_401", str(ctx.exception))
        self.assertIn("session-expired", str(ctx.exception))

    def test_refresh_session_via_service_raises_without_access_token(self):
        payload = {"ok": True, "session": {}}
        with patch.object(sr.requests, "post", return_value=_Response(200, payload)):
            with self.assertRaises(sr.SessionRefreshError):
                sr.refresh_session_via_service("s")

    def test_refresh_session_via_service_keeps_session_when_not_rotated(self):
        payload = {
            "ok": True,
            "session": {"accessToken": _jwt(time.time() + 3600)},
        }
        with patch.object(sr.requests, "post", return_value=_Response(200, payload)):
            result = sr.refresh_session_via_service("same-session")
        self.assertEqual(result["session_token"], "same-session")
        self.assertFalse(result["rotated"])


class SessionRefreshJobsTest(unittest.TestCase):
    def _record(self, email: str, *, old_exp: float, session: str = "sess") -> dict:
        return {
            "email": email,
            "account_id": f"acc-{email}",
            "access_token": _jwt(old_exp),
            "session_token": session,
            "plan_type": "free",
        }

    def _wait_job(self, jobs: sr.SessionRefreshJobs, job: dict, timeout: float = 10.0) -> dict:
        job_id = job["job_id"]
        self.assertTrue(jobs.wait(job_id, timeout))
        return jobs.snapshot(job_id)

    def test_job_marks_refreshed_unchanged_and_failed(self):
        jobs = sr.SessionRefreshJobs()
        now = time.time()
        records = [
            self._record("fresh@example.com", old_exp=now - 100, session="sess-fresh"),
            self._record("stale@example.com", old_exp=now + 5 * 86400, session="sess-stale"),
            self._record("dead@example.com", old_exp=now - 100, session="sess-dead"),
        ]

        def fake_refresh(session_token: str, **_kwargs):
            if session_token == "sess-dead":
                raise sr.SessionRefreshError("session_refresh_http_401: session ended")
            if session_token == "sess-stale":
                return {
                    "access_token": _jwt(now + 5 * 86400),
                    "session_token": session_token,
                    "plan_type": "",
                    "account_id": "",
                    "email": "",
                    "rotated": False,
                }
            return {
                "access_token": _jwt(now + 10 * 86400),
                "session_token": session_token,
                "plan_type": "plus",
                "account_id": "acc-new",
                "email": "",
                "rotated": False,
            }

        fields_by_key: dict[str, dict] = {}

        with (
            patch.object(sr, "refresh_session_via_service", side_effect=fake_refresh),
            patch.object(
                sr,
                "update_account_record_fields",
                side_effect=lambda updates, **_k: fields_by_key.update(updates) or (1, []),
            ),
        ):
            job = jobs.start(records, concurrency=2)
            self.assertEqual(job["total"], 3)
            finished = self._wait_job(jobs, job)

        self.assertEqual(finished["status"], "partial")  # one failure
        self.assertEqual(finished["refreshed"], 1)
        self.assertEqual(finished["unchanged"], 1)
        self.assertEqual(finished["failed"], 1)
        self.assertEqual(finished["done"], 3)
        self.assertEqual(len(fields_by_key), 3)
        # Failed account records only the error, no AT overwrite.
        dead_key = next(
            k for k, v in fields_by_key.items() if v.get("last_token_refresh_error")
        )
        self.assertNotIn("access_token", fields_by_key[dead_key])
        # Successful refresh persists the new token + session.
        fresh_key = next(k for k, v in fields_by_key.items() if v.get("plan_type") == "plus")
        self.assertEqual(fields_by_key[fresh_key]["session_token"], "sess-fresh")

    def test_start_filters_deactivated_and_sessionless_records(self):
        jobs = sr.SessionRefreshJobs()
        records = [
            self._record("ok@example.com", old_exp=time.time() - 10),
            {**self._record("banned@example.com", old_exp=time.time() - 10), "is_deactivated": True},
            self._record("nosession@example.com", old_exp=time.time() - 10, session=""),
        ]
        with patch.object(
            sr,
            "update_account_record_fields",
            side_effect=lambda updates, **_k: (1, []),
        ):
            job = jobs.start(records, concurrency=1)
        self.assertEqual(job["total"], 1)
        self.assertEqual(job["items"][0]["email"], "ok@example.com")
        jobs.wait(job["job_id"], 5)

    def test_start_rejects_empty_selection(self):
        jobs = sr.SessionRefreshJobs()
        with self.assertRaises(ValueError):
            jobs.start([], concurrency=1)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
import threading
import time
import unittest
from unittest.mock import patch

from common import trial_eligibility
from common.account_health import InvalidAccessTokenError
from common.trial_eligibility_jobs import TrialEligibilityJobs


class _Response:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _Session:
    def __init__(self, response: _Response):
        self.response = response
        self.closed = False
        self.calls = []

    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response

    def close(self):
        self.closed = True


class TrialEligibilityTests(unittest.TestCase):
    @staticmethod
    def _wait_for_job(manager: TrialEligibilityJobs, job_id: str, timeout: float = 3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = manager.snapshot(job_id)
            if job and job["status"] not in {"queued", "running", "stopping"}:
                return job
            time.sleep(0.01)
        raise AssertionError("优惠检测任务未在预期时间内结束")

    def test_edge_and_transient_http_failures_are_retryable(self):
        for message in (
            "trial_edge_blocked_http_403",
            "account_check_http_429: rate limited",
            "account_check_http_504: gateway timeout",
            "Failed to perform, curl: (35) TLS connect error",
        ):
            with self.subTest(message=message):
                self.assertTrue(trial_eligibility.is_trial_probe_retryable(message))
        self.assertFalse(
            trial_eligibility.is_trial_probe_retryable("account_check_http_400: bad request")
        )

    def test_direct_probe_reads_campaign_id(self):
        response = _Response(
            200,
            {
                "accounts": {
                    "default": {
                        "account": {"plan_type": "free", "is_deactivated": False},
                        "entitlement": {"has_active_subscription": False},
                        "eligible_promo_campaigns": {
                            "plus": {"id": "plus-1-month-free"}
                        },
                    }
                }
            },
        )
        session = _Session(response)
        with patch.object(trial_eligibility, "_session", return_value=session) as make_session:
            result = trial_eligibility.check_trial_eligibility(
                {"email": "fixture@example.com", "access_token": "AT", "proxy": "PROXY"},
                direct=True,
            )
        self.assertEqual(result["provider"], "trial_eligibility")
        self.assertTrue(result["eligible"])
        self.assertEqual(result["status"], "plus_eligible")
        self.assertEqual(result["promo_campaign_id"], "plus-1-month-free")
        self.assertEqual(result["proxy_mode"], "direct")
        make_session.assert_called_once_with("", impersonate="firefox147")
        self.assertTrue(session.closed)

    def test_discount_campaign_is_not_free_trial(self):
        response = _Response(
            200,
            {
                "accounts": {
                    "default": {
                        "account": {"plan_type": "free", "is_deactivated": False},
                        "entitlement": {"has_active_subscription": False},
                        "eligible_promo_campaigns": {
                            "plus": {"id": "plus-2-months-50-pct-off"}
                        },
                    }
                }
            },
        )
        with patch.object(trial_eligibility, "_session", return_value=_Session(response)):
            result = trial_eligibility.check_trial_eligibility(
                {"email": "fixture@example.com", "access_token": "AT"}
            )
        self.assertFalse(result["eligible"])
        self.assertEqual(result["eligibility_status"], "ineligible")
        self.assertEqual(result["label"], "无免费试用")
        self.assertTrue(result["has_promotion"])
        self.assertEqual(result["promotion_count"], 1)

    def test_probe_sends_account_and_device_identity_headers(self):
        response = _Response(
            200,
            {
                "accounts": {
                    "default": {
                        "account": {"plan_type": "free"},
                        "eligible_promo_campaigns": {},
                    }
                }
            },
        )
        session = _Session(response)
        with patch.object(trial_eligibility, "_session", return_value=session):
            trial_eligibility.check_trial_eligibility(
                {
                    "email": "fixture@example.com",
                    "access_token": "ACCESS_TOKEN",
                    "account_id": "ACCOUNT_ID",
                    "device_id": "DEVICE_UUID",
                }
            )

        request_url = session.calls[0][0][0]
        headers = session.calls[0][1]["headers"]
        self.assertIn(trial_eligibility.ACCOUNT_CHECK_PATH, request_url)
        self.assertEqual(headers["Authorization"], "Bearer ACCESS_TOKEN")
        self.assertEqual(headers["ChatGPT-Account-ID"], "ACCOUNT_ID")
        self.assertEqual(headers["OAI-Device-Id"], "DEVICE_UUID")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertEqual(headers["Origin"], "https://chatgpt.com")
        self.assertEqual(headers["Referer"], "https://chatgpt.com/")

    def test_active_subscription_is_not_trial_eligible(self):
        response = _Response(
            200,
            {
                "accounts": {
                    "default": {
                        "account": {"plan_type": "plus"},
                        "entitlement": {"has_active_subscription": True},
                        "eligible_promo_campaigns": {},
                    }
                }
            },
        )
        with patch.object(trial_eligibility, "_session", return_value=_Session(response)):
            result = trial_eligibility.check_trial_eligibility(
                {"email": "fixture@example.com", "access_token": "AT"}
            )
        self.assertFalse(result["eligible"])
        self.assertEqual(result["status"], "plus_active")
        self.assertEqual(result["eligibility_status"], "active")

    def test_401_marks_token_invalidated(self):
        with patch.object(
            trial_eligibility,
            "_session",
            return_value=_Session(_Response(401, text="expired")),
        ):
            with self.assertRaises(InvalidAccessTokenError):
                trial_eligibility.check_trial_eligibility(
                    {"email": "fixture@example.com", "access_token": "AT"}
                )

    def test_html_403_is_reported_as_retryable_edge_failure(self):
        with patch.object(
            trial_eligibility,
            "_session",
            return_value=_Session(_Response(403, text="<html>Cloudflare</html>")),
        ):
            with self.assertRaisesRegex(
                trial_eligibility.TrialEligibilityError,
                "trial_edge_blocked_http_403",
            ):
                trial_eligibility.check_trial_eligibility(
                    {"email": "fixture@example.com", "access_token": "AT"}
                )

    def test_scheduled_probe_rotates_edge_failure_and_records_jp_route(self):
        checked = {
            "eligibility_status": "eligible",
            "label": "可领 Plus 试用",
            "status": "plus_eligible",
            "promo_campaign_id": "plus-1-month-free",
        }
        with (
            patch.object(trial_eligibility, "_auto_check_channel", return_value="v4"),
            patch(
                "common.trial_proxy_pool.selected_trial_proxies",
                return_value=("JP", ["http://proxy-a", "http://proxy-b"]),
            ),
            patch.object(
                trial_eligibility,
                "check_trial_eligibility",
                side_effect=[
                    trial_eligibility.TrialEligibilityError("trial_edge_blocked_http_403"),
                    checked,
                ],
            ) as probe,
            patch.object(trial_eligibility, "_queue_result") as queued,
            patch.object(trial_eligibility.time, "sleep"),
        ):
            trial_eligibility._run_scheduled_check(
                {"email": "fixture@example.com", "access_token": "AT"},
                "PATH",
                "KEY",
            )

        self.assertEqual([call.kwargs["proxy"] for call in probe.call_args_list], ["http://proxy-a", "http://proxy-b"])
        fields = queued.call_args.args[1]
        self.assertEqual(fields["trial_eligibility_status"], "eligible")
        self.assertEqual(fields["trial_eligibility_proxy_mode"], "JP proxy")

    def test_exhausted_jp_routes_remain_recheckable(self):
        failure = trial_eligibility.TrialEligibilityError("account_check_http_429: busy")
        with (
            patch.object(trial_eligibility, "_auto_check_channel", return_value="v4"),
            patch(
                "common.trial_proxy_pool.selected_trial_proxies",
                return_value=("JP", ["http://proxy-a", "http://proxy-b"]),
            ),
            patch.object(trial_eligibility, "check_trial_eligibility", side_effect=failure),
            patch.object(trial_eligibility, "_queue_result") as queued,
            patch.object(trial_eligibility.time, "sleep"),
        ):
            trial_eligibility._run_scheduled_check(
                {"email": "fixture@example.com", "access_token": "AT"},
                "PATH",
                "KEY",
            )

        fields = queued.call_args.args[1]
        self.assertEqual(fields["trial_eligibility_status"], "error")
        self.assertEqual(fields["trial_eligibility_checked_at"], "")
        self.assertEqual(fields["trial_eligibility_reason"], "route_retry_pending")
        self.assertEqual(fields["trial_eligibility_proxy_mode"], "JP proxy")

    def test_scheduled_probe_uses_nerver_channel_when_configured(self):
        checked = {
            "eligibility_status": "eligible",
            "label": "可领 Plus 免费试用",
            "status": "plus_eligible",
            "promo_campaign_id": "plus-1-month-free",
        }
        with (
            patch.object(trial_eligibility, "_auto_check_channel", return_value="nerver"),
            patch.object(
                trial_eligibility,
                "check_trial_eligibility_nerver",
                return_value=checked,
            ) as nerver_probe,
            patch.object(trial_eligibility, "check_trial_eligibility") as v4_probe,
            patch.object(trial_eligibility, "_queue_result") as queued,
        ):
            trial_eligibility._run_scheduled_check(
                {"email": "fixture@example.com", "access_token": "AT"},
                "PATH",
                "KEY",
            )

        v4_probe.assert_not_called()
        nerver_probe.assert_called_once_with("AT")
        fields = queued.call_args.args[1]
        self.assertEqual(fields["trial_eligibility_status"], "eligible")
        self.assertEqual(fields["trial_eligibility_method"], "nerver_check")
        self.assertEqual(fields["trial_eligibility_proxy_mode"], "nerver")

    def test_scheduled_probe_nerver_error_marks_failed_without_v4_fallback(self):
        failure = trial_eligibility.TrialEligibilityError("nerver_check_http_500: busy")
        with (
            patch.object(trial_eligibility, "_auto_check_channel", return_value="nerver"),
            patch.object(
                trial_eligibility,
                "check_trial_eligibility_nerver",
                side_effect=failure,
            ),
            patch.object(trial_eligibility, "check_trial_eligibility") as v4_probe,
            patch.object(trial_eligibility, "_queue_result") as queued,
        ):
            trial_eligibility._run_scheduled_check(
                {"email": "fixture@example.com", "access_token": "AT"},
                "PATH",
                "KEY",
            )

        v4_probe.assert_not_called()
        fields = queued.call_args.args[1]
        self.assertEqual(fields["trial_eligibility_status"], "error")
        self.assertEqual(fields["trial_eligibility_reason"], "request_error")
        self.assertEqual(fields["trial_eligibility_method"], "nerver_check")
        self.assertEqual(fields["trial_eligibility_proxy_mode"], "nerver")

    def test_auto_check_channel_reads_ui_settings(self):
        trial_eligibility._AUTO_CHANNEL_CACHE = (0.0, "v4")
        with patch(
            "config_loader.load_config",
            return_value={"ui_settings": {"trial_channel": "nerver"}},
        ):
            self.assertEqual(trial_eligibility._auto_check_channel(), "nerver")
        trial_eligibility._AUTO_CHANNEL_CACHE = (0.0, "v4")
        with patch(
            "config_loader.load_config",
            return_value={"ui_settings": {"trial_channel": "v4"}},
        ):
            self.assertEqual(trial_eligibility._auto_check_channel(), "v4")
        trial_eligibility._AUTO_CHANNEL_CACHE = (0.0, "v4")
        with patch("config_loader.load_config", side_effect=RuntimeError("boom")):
            self.assertEqual(trial_eligibility._auto_check_channel(), "v4")
        trial_eligibility._AUTO_CHANNEL_CACHE = (0.0, "v4")

    def test_batch_job_uses_jp_proxy_and_persists_result(self):
        manager = TrialEligibilityJobs()
        checked = {
            "eligibility_status": "eligible",
            "label": "可领 Plus 试用",
            "status": "plus_eligible",
            "promo_campaign_id": "campaign-1",
        }
        record = {
            "email": "fixture@example.com",
            "account_id": "acct-1",
            "plan_type": "free",
            "access_token": "ACCESS_TOKEN",
        }
        with (
            patch(
                "common.trial_eligibility_jobs.selected_trial_proxies",
                return_value=("JP", ["http://jp-proxy"]),
            ),
            patch(
                "common.trial_eligibility_jobs.check_trial_eligibility",
                return_value=checked,
            ) as probe,
            patch(
                "common.trial_eligibility_jobs.update_account_record_fields"
            ) as persist,
        ):
            started = manager.start([record], concurrency=1, attempts=8)
            job = self._wait_for_job(manager, started["job_id"])

        probe.assert_called_once_with(record, proxy="http://jp-proxy", direct=False)
        fields = persist.call_args.args[0]["account_id:acct-1"]
        self.assertEqual(fields["trial_eligibility_status"], "eligible")
        self.assertEqual(fields["trial_eligibility_proxy_mode"], "JP proxy")
        self.assertEqual(job["status"], "success")
        self.assertEqual(job["eligible"], 1)

    def test_batch_job_enforces_worker_limit(self):
        manager = TrialEligibilityJobs()
        lock = threading.Lock()
        active = 0
        peak = 0

        def probe(*args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.12)
            with lock:
                active -= 1
            return {
                "eligibility_status": "ineligible",
                "label": "暂无优惠",
                "status": "not_eligible",
            }

        records = [
            {
                "email": f"worker-{index}@example.com",
                "account_id": f"acct-{index}",
                "plan_type": "free",
                "access_token": f"TOKEN-{index}",
            }
            for index in range(7)
        ]
        with (
            patch(
                "common.trial_eligibility_jobs.selected_trial_proxies",
                return_value=("JP", ["http://jp-proxy"]),
            ),
            patch(
                "common.trial_eligibility_jobs.check_trial_eligibility",
                side_effect=probe,
            ),
            patch("common.trial_eligibility_jobs.update_account_record_fields"),
        ):
            started = manager.start(records, concurrency=20)
            job = self._wait_for_job(manager, started["job_id"])

        self.assertEqual(started["concurrency"], 20)
        self.assertEqual(peak, 7)
        self.assertEqual(job["done"], 7)
        self.assertEqual(job["ineligible"], 7)

    def test_batch_job_bounds_in_flight_futures(self):
        manager = TrialEligibilityJobs()
        records = [
            {
                "email": f"bounded-{index}@example.com",
                "account_id": f"bounded-{index}",
                "plan_type": "free",
                "access_token": f"TOKEN-{index}",
            }
            for index in range(25)
        ]
        release = threading.Event()
        window_ready = threading.Event()
        tracker_lock = threading.Lock()
        outstanding = 0
        peak = 0
        submitted = 0

        import common.trial_eligibility_jobs as jobs_module

        real_executor = jobs_module.ThreadPoolExecutor

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

        def probe(*_args, **_kwargs):
            release.wait(timeout=3)
            return {"eligibility_status": "ineligible", "label": "暂无优惠"}

        try:
            with (
                patch.object(jobs_module, "ThreadPoolExecutor", TrackingExecutor),
                patch(
                    "common.trial_eligibility_jobs.selected_trial_proxies",
                    return_value=("JP", ["http://jp-proxy"]),
                ),
                patch(
                    "common.trial_eligibility_jobs.check_trial_eligibility",
                    side_effect=probe,
                ),
                patch("common.trial_eligibility_jobs.update_account_record_fields"),
            ):
                started = manager.start(records, concurrency=2)
                self.assertTrue(window_ready.wait(timeout=3))
                time.sleep(0.1)
                with tracker_lock:
                    peak_before_release = peak
                    submitted_before_release = submitted
                release.set()
                self.assertTrue(manager.wait(started["job_id"], timeout=5))
                result = manager.snapshot(started["job_id"])
        finally:
            release.set()

        self.assertLessEqual(peak_before_release, 4)
        self.assertLessEqual(submitted_before_release, 4)
        self.assertEqual(submitted, len(records))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["done"], len(records))
        self.assertEqual(result["ineligible"], len(records))

    def test_batch_job_stop_drains_submitted_window_and_marks_tail(self):
        manager = TrialEligibilityJobs()
        records = [
            {
                "email": f"stop-bounded-{index}@example.com",
                "account_id": f"stop-bounded-{index}",
                "plan_type": "free",
                "access_token": f"TOKEN-{index}",
            }
            for index in range(12)
        ]
        entered = threading.Event()
        release = threading.Event()

        def probe(*_args, **_kwargs):
            entered.set()
            release.wait(timeout=3)
            return {"eligibility_status": "ineligible", "label": "暂无优惠"}

        try:
            with (
                patch(
                    "common.trial_eligibility_jobs.selected_trial_proxies",
                    return_value=("JP", ["http://jp-proxy"]),
                ),
                patch(
                    "common.trial_eligibility_jobs.check_trial_eligibility",
                    side_effect=probe,
                ),
                patch("common.trial_eligibility_jobs.update_account_record_fields"),
            ):
                started = manager.start(records, concurrency=2)
                self.assertTrue(entered.wait(timeout=3))
                stopping = manager.stop(started["job_id"])
                self.assertEqual(stopping["status"], "stopping")
                release.set()
                self.assertTrue(manager.wait(started["job_id"], timeout=5))
                result = manager.snapshot(started["job_id"])
        finally:
            release.set()

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["done"], len(records))
        self.assertGreaterEqual(result["stopped"], 1)
        self.assertTrue(
            all(item["status"] in {"success", "stopped"} for item in result["items"])
        )

    def test_batch_lock_is_current_manager_only_and_releases_after_completion(self):
        first = TrialEligibilityJobs()
        second = TrialEligibilityJobs()
        entered = threading.Event()
        release = threading.Event()
        record = {
            "email": "fixture@example.com",
            "account_id": "acct-1",
            "plan_type": "free",
            "access_token": "ACCESS_TOKEN",
        }

        def blocking_probe(*args, **kwargs):
            entered.set()
            release.wait(2)
            return {
                "eligibility_status": "ineligible",
                "label": "暂无优惠",
                "status": "not_eligible",
            }

        with (
            patch(
                "common.trial_eligibility_jobs.selected_trial_proxies",
                return_value=("JP", ["http://jp-proxy"]),
            ),
            patch(
                "common.trial_eligibility_jobs.check_trial_eligibility",
                side_effect=blocking_probe,
            ),
            patch("common.trial_eligibility_jobs.update_account_record_fields"),
        ):
            first_job = first.start([record])
            self.assertTrue(entered.wait(1))
            with self.assertRaisesRegex(RuntimeError, "已有优惠检测批次正在运行"):
                first.start([record])

            # A fresh manager has no persisted legacy lock or shared active job.
            second_job = second.start([record])
            release.set()
            self._wait_for_job(first, first_job["job_id"])
            self._wait_for_job(second, second_job["job_id"])

            next_job = first.start([record])
            self._wait_for_job(first, next_job["job_id"])

    def test_token_endpoint_uses_configured_proxy_without_returning_token(self):
        import web_app

        token = "AT-" + "x" * 40
        checked = {
            "provider": "trial_eligibility",
            "eligible": True,
            "eligible_promo_campaigns": {
                "plus": {"id": "plus-1-month-free"},
            },
        }
        with (
            patch.object(web_app, "selected_trial_proxy", return_value=("KR", "http://proxy")),
            patch.object(web_app, "selected_trial_proxies", return_value=("KR", [])),
            patch.object(web_app, "check_trial_eligibility", return_value=checked) as probe,
        ):
            result = asyncio.run(
                web_app.token_trial_eligibility(
                    web_app.TokenTrialEligibilityRequest(
                        access_token=token,
                        use_configured_proxy=True,
                    )
                )
            )

        probe.assert_called_once_with(
            {"access_token": token},
            proxy="http://proxy",
            direct=False,
        )
        self.assertTrue(result["has_promotion"])
        self.assertEqual(result["promotion_count"], 1)
        self.assertEqual(result["proxy_country"], "KR")
        self.assertNotIn("access_token", result)

    def test_token_endpoint_rotates_proxy_after_tls_error(self):
        import web_app

        token = "AT-" + "x" * 40
        checked = {
            "provider": "trial_eligibility",
            "eligible": False,
            "eligible_promo_campaigns": {},
        }
        tls_error = RuntimeError(
            "Failed to perform, curl: (35) TLS connect error"
        )
        with (
            patch.object(web_app, "selected_trial_proxy", return_value=("KR", "http://proxy-a")),
            patch.object(
                web_app,
                "selected_trial_proxies",
                return_value=("KR", ["http://proxy-b"]),
            ) as remaining,
            patch.object(
                web_app,
                "check_trial_eligibility",
                side_effect=[tls_error, checked],
            ) as probe,
        ):
            result = asyncio.run(
                web_app.token_trial_eligibility(
                    web_app.TokenTrialEligibilityRequest(access_token=token)
                )
            )

        remaining.assert_called_once_with(limit=4, exclude={"http://proxy-a"})
        self.assertEqual(
            [call.kwargs["proxy"] for call in probe.call_args_list],
            ["http://proxy-a", "http://proxy-b"],
        )
        self.assertFalse(result["has_promotion"])
        self.assertEqual(result["proxy_country"], "KR")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import requests

import mail
from mail.parsing import normalize_provider_type


def response(payload: dict, status_code: int = 200) -> MagicMock:
    item = MagicMock(status_code=status_code, text=json.dumps(payload))
    item.json.return_value = payload
    return item


class OnlineDisposableMailProviderTests(unittest.TestCase):
    def setUp(self):
        mail._ONLINE_DISPOSABLEMAIL_CAPACITY_CACHE.clear()
        mail._ONLINE_DISPOSABLEMAIL_TYPES_CACHE.clear()
        self.public_sessions = []
        self.public_calls = []
        self.sales_response = response({"code": 200, "data": [{
            "emailTypeId": "26", "emailTypeName": "Gmail_1",
            "availableNum": 165000, "price": 0.05,
        }]})
        self.sales_by_service = {}
        public_factory = patch.object(mail, "_http_session", side_effect=self._public_session)
        public_factory.start()
        self.addCleanup(public_factory.stop)
        forbid_network = patch.object(
            requests.sessions.Session, "request",
            side_effect=AssertionError("External HTTP is forbidden in provider tests"),
        )
        self.network = forbid_network.start()
        self.addCleanup(forbid_network.stop)
        self.addCleanup(self.network.assert_not_called)

    def _public_session(self):
        session = MagicMock()

        def get(url, **kwargs):
            self.public_calls.append((url, kwargs))
            self.assertEqual(urlsplit(url).path, "/api/guest/type/selects")
            self.assertEqual(kwargs["params"]["pageNum"], 1)
            self.assertEqual(kwargs["params"]["pageSize"], 10000)
            self.assertNotIn("apiKey", kwargs["params"])
            result = self.sales_by_service.get(kwargs["params"]["serviceId"], self.sales_response)
            if isinstance(result, BaseException):
                raise result
            return result

        session.get.side_effect = get
        self.public_sessions.append(session)
        return session

    def _catalog_response(self, provider, method_name, result):
        mail._ONLINE_DISPOSABLEMAIL_TYPES_CACHE.clear()
        if method_name == "get_types":
            self.sales_response = result
        else:
            provider.session.get.return_value = result

    def _entry(self, **overrides):
        entry = {
            "type": "online_disposablemail",
            "enable": True,
            "provider_ref": "online_disposablemail#1",
            "api_base": "https://mail-api.example",
            "api_key": "odm-secret-fixture",
            "service_id": "242",
            "email_type_id": "26",
            "capacity": 50,
        }
        entry.update(overrides)
        return entry

    def _provider(self, **overrides):
        with patch.object(mail, "_http_session", return_value=MagicMock()):
            provider = mail.OnlineDisposableMailProvider(
                self._entry(**overrides),
                {
                    "request_timeout": 8,
                    "wait_timeout": 2,
                    "wait_interval": 0.2,
                    "wait_chunk": 1,
                    "user_agent": "test-agent",
                },
            )
        self.addCleanup(provider.close)
        return provider

    def _mock_provider(self, **overrides):
        return self._provider(**overrides)

    def test_aliases_and_canonical_types(self):
        self.assertEqual(
            normalize_provider_type("online_dispo_mail"), "online_disposablemail"
        )
        self.assertEqual(
            normalize_provider_type("onlinedispomail"), "online_disposablemail"
        )
        self.assertIn("online_disposablemail", mail.CANONICAL_PROVIDER_TYPES)

    def test_api_base_is_normalized_to_origin(self):
        provider = self._provider(
            api_base="https://mail-api.example/api/mailbox/getActivation"
        )
        try:
            self.assertEqual(provider.api_base, "https://mail-api.example")
            self.assertEqual(provider.service_id, "242")
            self.assertEqual(provider.email_type_id, "26")
        finally:
            provider.close()

    def test_non_numeric_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            self._provider(service_id="dr")

    def test_default_is_current_gmail_type_but_explicit_legacy_id_is_not_rewritten(self):
        self.assertEqual(self._provider(email_type_id=None).email_type_id, "26")
        self.assertEqual(self._provider(email_type_id="3").email_type_id, "3")

    def test_business_error_raises_with_code(self):
        provider = self._provider()
        provider.session = MagicMock()
        provider.session.get.return_value = response(
            {"code": 41004, "msg": "账户余额不足"}
        )
        with self.assertRaises(mail.OnlineDisposableMailError) as caught:
            provider.get_balance()
        provider.close()
        self.assertEqual(caught.exception.business_code, 41004)

    def test_public_catalogs_work_without_an_api_key(self):
        provider = self._mock_provider(api_key="")
        provider.api_key = ""
        provider.session.get.return_value = response({"code": 200, "data": [{"id": 242, "name": "Service fixture"}]})

        services = provider.get_services()
        types = provider.get_types()

        self.assertEqual([(item["id"], item["name"]) for item in services], [("242", "Service fixture")])
        self.assertEqual([(item["id"], item["name"]) for item in types], [("26", "Gmail_1")])
        self.assertEqual(types[0]["availableNum"], 165000)
        calls = provider.session.get.call_args_list
        self.assertEqual([call.args[0] for call in calls], [
            "https://mail-api.example/api/services",
        ])
        for call in calls:
            self.assertNotIn("apiKey", call.kwargs["params"])
        self.assertEqual(self.public_calls[0][0], "https://mail-api.example/api/guest/type/selects")
        self.public_sessions[0].close.assert_called_once()

    def test_public_catalogs_do_not_transmit_a_configured_key(self):
        provider = self._mock_provider()
        provider.session.get.return_value = response({"code": 200, "data": [{"id": "242", "name": "Service fixture"}]})

        provider.get_services()
        provider.get_types()

        for call in provider.session.get.call_args_list:
            self.assertNotIn("apiKey", call.kwargs["params"])
            self.assertNotIn("odm-secret-fixture", repr(call))
        self.assertNotIn("odm-secret-fixture", repr(self.public_calls))

    def test_balance_keeps_default_authentication(self):
        provider = self._mock_provider()
        provider.session.get.return_value = response({"code": 200, "data": 12.5})

        self.assertEqual(provider.get_balance(), 12.5)
        provider.session.get.assert_called_once()
        self.assertEqual(provider.session.get.call_args.kwargs["params"], {"apiKey": "odm-secret-fixture"})

    def test_legacy_catalog_fields_are_normalized(self):
        cases = [
            ("get_services", {"serviceId": 242, "serviceName": "Service fixture"}, "242", "Service fixture"),
            ("get_types", {"emailTypeId": 26, "emailTypeName": "Mailbox fixture", "availableNum": 2}, "26", "Mailbox fixture"),
            ("get_services", {"id": "242", "serviceId": 242, "name": "Service fixture", "serviceName": "Service fixture"}, "242", "Service fixture"),
            ("get_types", {"id": "26", "emailTypeId": 26, "name": "Mailbox fixture", "emailTypeName": "Mailbox fixture", "availableNum": "2"}, "26", "Mailbox fixture"),
        ]
        for method_name, item, expected_id, expected_name in cases:
            with self.subTest(method=method_name, item=item):
                provider = self._mock_provider()
                self._catalog_response(provider, method_name, response({"code": 200, "data": [item]}))

                catalog = getattr(provider, method_name)()

                self.assertEqual(len(catalog), 1)
                self.assertEqual(catalog[0]["id"], expected_id)
                self.assertEqual(catalog[0]["name"], expected_name)

    def test_invalid_catalog_entries_are_not_silently_discarded(self):
        invalid_payloads = [
            None,
            {},
            "not-a-list",
            [None],
            ["not-an-object"],
            [{"name": "Missing ID"}],
            [{"id": "", "name": "Empty ID"}],
            [{"id": "not-a-number", "name": "Invalid ID"}],
            [{"id": True, "name": "Boolean ID"}],
            [{"id": 3}],
            [{"id": 3, "name": "   "}],
            [{"id": 3, "name": "First"}, {"id": "3", "name": "Duplicate"}],
            [{"id": 242, "name": "Valid first row"}, {"name": "Invalid later row"}],
        ]
        for method_name in ("get_services", "get_types"):
            for payload in invalid_payloads:
                with self.subTest(method=method_name, payload=payload):
                    provider = self._mock_provider()
                    self._catalog_response(provider, method_name, response({"code": 200, "data": payload}))

                    with self.assertRaises(mail.OnlineDisposableMailError):
                        getattr(provider, method_name)()

                    if method_name == "get_types":
                        self.public_sessions[-1].get.assert_called_once()
                        self.public_sessions[-1].close.assert_called_once()
                    else:
                        provider.session.get.assert_called_once()

    def test_catalog_rejects_conflicting_id_aliases(self):
        cases = [
            ("get_services", {"id": 242, "serviceId": 243, "name": "Conflict"}),
            ("get_types", {"id": 3, "emailTypeId": 4, "name": "Conflict"}),
        ]
        for method_name, item in cases:
            with self.subTest(method=method_name):
                provider = self._mock_provider()
                self._catalog_response(provider, method_name, response({"code": 200, "data": [item]}))
                with self.assertRaises(mail.OnlineDisposableMailError):
                    getattr(provider, method_name)()

    def test_service_child_id_does_not_replace_missing_parent_id(self):
        provider = self._mock_provider()
        provider.session.get.return_value = response({
            "code": 200,
            "data": [{"serviceItemId": 242, "serviceItemName": "Child fixture"}],
        })

        with self.assertRaises(mail.OnlineDisposableMailError):
            provider.get_services()

    def test_validate_selection_uses_catalogs_without_purchasing(self):
        provider = self._mock_provider()
        provider.session.get.return_value = response({"code": 200, "data": [{"id": 242, "name": "Service fixture"}]})
        with patch.object(provider, "buy_mailbox") as buy:
            provider.validate_selection()

        buy.assert_not_called()
        self.assertEqual(provider.service_id, "242")
        self.assertEqual(provider.email_type_id, "26")
        self.assertEqual([call.args[0] for call in provider.session.get.call_args_list], [
            "https://mail-api.example/api/services",
        ])
        self.assertEqual(self.public_calls[0][0], "https://mail-api.example/api/guest/type/selects")

    def test_validate_selection_reports_the_missing_field(self):
        for field in ("service_id", "email_type_id"):
            with self.subTest(field=field):
                provider = self._mock_provider(**{field: "909090"})
                provider.session.get.return_value = response({"code": 200, "data": [{"id": 242, "name": "Service fixture"}]})
                with patch.object(provider, "buy_mailbox") as buy:
                    with self.assertRaisesRegex(ValueError, field):
                        provider.validate_selection()

                buy.assert_not_called()
                self.assertEqual(getattr(provider, field), "909090")
                self.assertEqual(provider.session.get.call_count, 1)

    def test_validate_selection_does_not_accept_child_id_as_service(self):
        provider = self._mock_provider()
        provider.session.get.return_value = response({"code": 200, "data": [{
                "id": 909090,
                "name": "Parent fixture",
                "items": [{"serviceItemId": 242, "serviceItemName": "Child fixture"}],
            }]})

        with self.assertRaisesRegex(ValueError, "service_id"):
            provider.validate_selection()

    def test_validate_selection_preserves_transport_and_catalog_failures(self):
        malformed_json = response({})
        malformed_json.json.side_effect = ValueError("not JSON")
        cases = [
            requests.Timeout("directory unavailable"),
            response({"msg": "directory unavailable"}, status_code=503),
            malformed_json,
            response({"code": 200, "data": [{"name": "Missing ID"}]}),
            response({"code": 41001, "msg": "Catalog request rejected"}),
        ]
        for failed_response in cases:
            with self.subTest(failure=type(failed_response).__name__):
                provider = self._mock_provider()
                if isinstance(failed_response, Exception):
                    provider.session.get.side_effect = failed_response
                else:
                    provider.session.get.return_value = failed_response
                with patch.object(provider, "buy_mailbox") as buy:
                    with self.assertRaises(mail.OnlineDisposableMailError):
                        provider.validate_selection()

                buy.assert_not_called()
                provider.session.get.assert_called_once()

    def test_purchase_parameter_error_keeps_code_and_is_not_replayed(self):
        provider = self._mock_provider()
        provider.session.get.return_value = response({
            "code": 41001,
            "msg": "Invalid parameter, please check odm-secret-fixture",
        })

        with self.assertRaises(mail.OnlineDisposableMailError) as caught:
            provider.buy_mailbox()

        self.assertEqual(caught.exception.business_code, 41001)
        self.assertIn("purchase", str(caught.exception))
        self.assertIn("41001", str(caught.exception))
        self.assertNotIn("odm-secret-fixture", str(caught.exception))
        provider.session.get.assert_called_once()
        self.assertEqual(provider.session.get.call_args.args[0], "https://mail-api.example/api/mailbox")
        self.assertEqual(provider.session.get.call_args.kwargs["params"], {
            "apiKey": "odm-secret-fixture",
            "serviceId": "242",
            "emailTypeId": "26",
            "quantity": 1,
            "buyMode": 0,
        })
        self.assertEqual((provider.service_id, provider.email_type_id), ("242", "26"))
        self.assertEqual(len(self.public_calls), 1)
        self.assertNotIn((provider.public_catalog_base, "242"), mail._ONLINE_DISPOSABLEMAIL_TYPES_CACHE)

    def test_uncertain_purchase_transport_failure_is_not_replayed(self):
        provider = self._mock_provider()
        provider.session.get.side_effect = requests.Timeout("purchase response timed out")

        with self.assertRaises(mail.OnlineDisposableMailError) as caught:
            provider.buy_mailbox()

        self.assertNotEqual(caught.exception.business_code, 41001)
        provider.session.get.assert_called_once()

    def test_legacy_type_dictionary_does_not_allow_an_unsold_type(self):
        provider = self._provider(email_type_id="3")

        def paid_api(url, **_kwargs):
            if urlsplit(url).path == "/api/services":
                return response({"code": 200, "data": [{"id": 242, "name": "OpenAi"}]})
            if urlsplit(url).path == "/api/types":
                return response({"code": 200, "data": [{"id": 3, "name": "Gmail"}]})
            self.fail(f"A rejected selection must not call {urlsplit(url).path}")

        provider.session.get.side_effect = paid_api
        with self.assertRaisesRegex(ValueError, "email_type_id=3"):
            provider.validate_selection()
        with self.assertRaisesRegex(mail.OnlineDisposableMailError, "email_type_id=3"):
            provider.buy_mailbox()
        summary = provider.availability_summary()

        self.assertEqual(summary["capacity"], 0)
        self.assertIn("email_type_id=3", summary["error"])
        self.assertEqual(provider.email_type_id, "3")
        self.assertEqual([urlsplit(call.args[0]).path for call in provider.session.get.call_args_list], ["/api/services"])
        self.assertEqual(len(self.public_calls), 1)

    def test_sales_preflight_allows_exactly_one_purchase_for_selected_type(self):
        provider = self._provider()
        provider.session.get.return_value = response({"code": 200, "data": {
            "orders": [{"orderId": "fixture-order", "email": "fixture@example.test"}],
        }})

        self.assertEqual(provider.buy_mailbox(), {"address": "fixture@example.test", "order_id": "fixture-order"})

        provider.session.get.assert_called_once()
        self.assertEqual(provider.session.get.call_args.kwargs["params"], {
            "apiKey": "odm-secret-fixture", "serviceId": "242", "emailTypeId": "26",
            "buyMode": 0, "quantity": 1,
        })
        self.assertIs(provider.session.get.call_args.kwargs["allow_redirects"], False)
        self.assertEqual(len(self.public_calls), 1)
        self.assertEqual(self.public_calls[0][1]["params"], {"pageSize": 10000, "pageNum": 1, "serviceId": "242"})
        self.public_sessions[0].close.assert_called_once()

    def test_purchase_redirect_is_not_followed_or_replayed(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                provider = self._provider()
                redirected = response({"msg": "Purchase endpoint redirected"}, status_code=status)
                redirected.headers = {"Location": "https://other.example.test/api/mailbox"}
                provider.session.get.return_value = redirected

                with self.assertRaisesRegex(mail.OnlineDisposableMailError, f"HTTP {status}"):
                    provider.buy_mailbox()

                provider.session.get.assert_called_once()
                self.assertEqual(provider.session.get.call_args.args[0], "https://mail-api.example/api/mailbox")
                self.assertIs(provider.session.get.call_args.kwargs["allow_redirects"], False)

    def test_preflight_failure_or_zero_stock_never_calls_purchase(self):
        malformed_json = response({})
        malformed_json.json.side_effect = ValueError("not JSON")
        cases = [
            requests.Timeout("sales unavailable"),
            response({"msg": "unavailable"}, status_code=503),
            response({"code": 41001, "msg": "Invalid parameter"}),
            malformed_json,
            response({"code": 200, "data": {"unexpected": []}}),
            response({"code": 200, "data": []}),
            response({"code": 200, "data": [{"emailTypeId": "26", "emailTypeName": "Gmail_1", "availableNum": 0}]}),
        ]
        for failed in cases:
            with self.subTest(failure=repr(failed)):
                mail._ONLINE_DISPOSABLEMAIL_TYPES_CACHE.clear()
                self.sales_response = failed
                provider = self._provider()
                with self.assertRaises(mail.OnlineDisposableMailError):
                    provider.buy_mailbox()
                provider.session.get.assert_not_called()
                self.public_sessions[-1].close.assert_called_once()
                summary = provider.availability_summary()
                self.assertEqual(summary["capacity"], 0)
                self.assertTrue(summary["error"])
                provider.session.get.assert_not_called()

    def test_invalid_sales_stock_is_rejected_and_not_cached(self):
        for stock in (None, True, False, -1, "-1", "", "many", "1.5", 1.5, float("nan"), float("inf")):
            with self.subTest(stock=stock):
                self.sales_response = response({"code": 200, "data": [{
                    "emailTypeId": "26", "emailTypeName": "Gmail_1", "availableNum": stock,
                }]})
                provider = self._provider()
                with self.assertRaisesRegex(mail.OnlineDisposableMailError, "availableNum"):
                    provider.buy_mailbox()
                provider.session.get.assert_not_called()
                self.assertFalse(mail._ONLINE_DISPOSABLEMAIL_TYPES_CACHE)
                self.public_sessions[-1].close.assert_called_once()

    def test_sales_cache_is_separate_for_each_service_and_public_origin(self):
        first = self._provider()
        other_service = self._provider(service_id="909")
        other_origin = self._provider(api_base="https://other.example.test")
        self.sales_by_service["909"] = response({"code": 200, "data": [{
            "emailTypeId": "44", "emailTypeName": "Other mailbox", "availableNum": 4,
        }]})

        self.assertEqual(first.get_types()[0]["id"], "26")
        self.assertEqual(other_service.get_types()[0]["id"], "44")
        self.assertEqual(other_origin.get_types()[0]["id"], "26")
        first.get_types()

        self.assertEqual(len(self.public_calls), 3)
        self.assertEqual(len(mail._ONLINE_DISPOSABLEMAIL_TYPES_CACHE), 3)

    def test_expired_sales_cache_failure_is_not_reused(self):
        clock = [100.0]
        provider = self._provider()
        with patch.object(mail.time, "monotonic", side_effect=lambda: clock[0]):
            catalog = provider.get_types()
            catalog[0]["availableNum"] = 0
            clock[0] = 120.0
            self.assertEqual(provider.get_types()[0]["availableNum"], 165000)
            self.assertEqual(len(self.public_calls), 1)
            clock[0] = 131.0
            self.sales_response = requests.Timeout("catalog refresh failed")
            for _ in range(2):
                with self.assertRaises(mail.OnlineDisposableMailError):
                    provider.buy_mailbox()
        self.assertEqual(len(self.public_calls), 3)
        self.assertFalse(mail._ONLINE_DISPOSABLEMAIL_TYPES_CACHE)
        provider.session.get.assert_not_called()

    def test_failed_forced_refresh_removes_valid_cache(self):
        provider = self._provider()
        provider.get_types()
        self.sales_response = requests.Timeout("forced refresh failed")
        with self.assertRaises(mail.OnlineDisposableMailError):
            provider.get_types(force=True)
        with self.assertRaises(mail.OnlineDisposableMailError):
            provider.buy_mailbox()
        self.assertFalse(mail._ONLINE_DISPOSABLEMAIL_TYPES_CACHE)
        provider.session.get.assert_not_called()

    def test_public_origin_mapping_does_not_redirect_custom_providers(self):
        cases = [
            (mail.ONLINE_DISPOSABLEMAIL_DEFAULT_BASE, None, "https://online-disposablemail.com"),
            ("https://custom.example.test", None, "https://custom.example.test"),
            ("https://api.online-disposablemail.com.example.test", None, "https://api.online-disposablemail.com.example.test"),
            ("https://custom.example.test", "https://catalog.example.test/public", "https://catalog.example.test"),
        ]
        for api_base, configured, expected in cases:
            with self.subTest(api_base=api_base, configured=configured):
                provider = self._provider(api_base=api_base, public_catalog_base=configured)
                self.assertEqual(provider.public_catalog_base, expected)
                provider.get_types(force=True)
                self.assertEqual(self.public_calls[-1][0], expected + "/api/guest/type/selects")

    def test_public_origin_rejects_url_credentials(self):
        with self.assertRaisesRegex(ValueError, "must not contain credentials"):
            self._provider(public_catalog_base="https://fixture:secret@catalog.example.test")
        self.assertFalse(self.public_calls)

    def test_anonymous_session_does_not_inherit_paid_session_credentials(self):
        provider = self._provider(api_base=mail.ONLINE_DISPOSABLEMAIL_DEFAULT_BASE)
        provider.session = requests.Session()
        provider.session.headers["Authorization"] = "Bearer paid-session-fixture"
        provider.session.cookies.set("paid_cookie", "fixture", domain=".online-disposablemail.com")
        provider.session.auth = ("fixture-user", "fixture-password")
        anonymous = requests.Session()
        anonymous.trust_env = False
        prepared = []

        def inspect_get(url, **kwargs):
            prepared.append(anonymous.prepare_request(requests.Request(
                "GET", url, params=kwargs["params"], headers=kwargs["headers"],
            )))
            return self.sales_response

        with patch.object(mail, "_http_session", return_value=anonymous), patch.object(
            anonymous, "get", side_effect=inspect_get
        ), patch.object(anonymous, "close", wraps=anonymous.close) as closed:
            provider.get_types()
            closed.assert_called_once()

        request = prepared[0]
        self.assertEqual(urlsplit(request.url).netloc, "online-disposablemail.com")
        self.assertEqual(parse_qs(urlsplit(request.url).query), {"pageSize": ["10000"], "pageNum": ["1"], "serviceId": ["242"]})
        self.assertNotIn("Authorization", request.headers)
        self.assertNotIn("Cookie", request.headers)
        self.assertNotIn("odm-secret-fixture", request.url)

    def test_no_stock_clears_cached_scheduling_capacity(self):
        provider = self._provider()
        provider.session.get.return_value = response({"code": 200, "data": 12.0})
        self.assertEqual(provider.availability_summary()["capacity"], 50)
        self.sales_response = response({"code": 200, "data": [{
            "emailTypeId": "26", "emailTypeName": "Gmail_1", "availableNum": 0,
        }]})
        provider.get_types(force=True)
        summary = provider.availability_summary()
        self.assertEqual(summary["capacity"], 0)
        self.assertTrue(summary["error"])
        provider.session.get.assert_called_once()

    def test_availability_rejects_catalog_failure_despite_cached_positive_balance(self):
        provider = self._provider()
        provider.session.get.return_value = response({"code": 200, "data": 12.0})
        self.assertEqual(provider.availability_summary()["capacity"], 50)
        self.sales_response = requests.Timeout("directory failure")
        summary = provider.availability_summary(force=True)
        self.assertEqual(summary["capacity"], 0)
        self.assertIn("directory failure", summary["error"])
        provider.session.get.assert_called_once()

    def test_availability_respects_stock_and_configured_capacity(self):
        provider = self._provider()
        provider.session.get.return_value = response({"code": 200, "data": 12.0})
        self.sales_response = response({"code": 200, "data": [{
            "emailTypeId": "26", "emailTypeName": "Gmail_1", "availableNum": 4,
        }]})
        self.assertEqual(provider.availability_summary()["capacity"], 4)
        provider.conf["capacity"] = 0
        self.assertEqual(provider.availability_summary()["capacity"], 0)

    def test_availability_fails_closed_when_balance_request_fails(self):
        provider = self._provider()
        provider.session.get.side_effect = requests.Timeout("balance unavailable")
        summary = provider.availability_summary()
        self.assertEqual(summary["capacity"], 0)
        self.assertIn("balance unavailable", summary["error"])

    def test_failed_balance_refresh_cannot_restore_cached_capacity(self):
        provider = self._provider()
        provider.session.get.return_value = response({"code": 200, "data": 12.0})
        self.assertEqual(provider.availability_summary()["capacity"], 50)
        provider.session.get.side_effect = requests.Timeout("balance refresh failed")
        self.assertEqual(provider.availability_summary(force=True)["capacity"], 0)
        self.assertEqual(provider.availability_summary()["capacity"], 0)
        self.assertFalse(mail._ONLINE_DISPOSABLEMAIL_CAPACITY_CACHE)

    def test_upstream_stock_rejection_invalidates_catalog_without_second_purchase(self):
        provider = self._provider()
        provider.session.get.return_value = response({"code": 41003, "msg": "No stock"})
        with self.assertRaises(mail.OnlineDisposableMailError) as raised:
            provider.buy_mailbox()
        self.assertEqual(raised.exception.business_code, 41003)
        provider.session.get.assert_called_once()
        self.assertEqual(len(self.public_calls), 1)
        self.assertFalse(mail._ONLINE_DISPOSABLEMAIL_TYPES_CACHE)

    def test_pool_capacity_does_not_fall_back_after_availability_exception(self):
        provider = self._provider()
        provider.availability_summary = MagicMock(side_effect=RuntimeError("availability unavailable"))
        with patch.object(mail, "OnlineDisposableMailProvider", return_value=provider), patch.object(
            mail, "_known_mailbox_bindings", return_value={}
        ), patch.object(mail, "_load_state", return_value={}):
            summary = mail.count_pool_capacity({"providers": [self._entry()]})
        self.assertEqual(summary["primary_available"], 0)
        self.assertEqual(summary["total"], 0)

    def test_purchase_reserves_state_and_returns_order(self):
        provider = self._provider()
        provider.session = MagicMock()
        provider.session.get.return_value = response(
            {
                "code": 200,
                "data": {
                    "orders": [
                        {"orderId": "1874732167711084545", "email": "Candice216442@gmail.com"}
                    ]
                },
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            mail, "STATE_FILE", Path(temp_dir) / "state.json"
        ), patch.object(
            mail, "_known_mailbox_bindings", return_value={}
        ):
            mailbox = provider.create_mailbox()
            state = mail._load_state()["candice216442@gmail.com"]
        provider.close()

        self.assertEqual(mailbox["provider"], "online_disposablemail")
        self.assertEqual(mailbox["order_id"], "1874732167711084545")
        self.assertEqual(mailbox["_odm_api_key"], "odm-secret-fixture")
        self.assertEqual(state["state"], "in_use")
        self.assertEqual(state["order_id"], "1874732167711084545")
        self.assertEqual(
            provider.session.get.call_args.kwargs["params"]["serviceId"], "242"
        )
        self.assertEqual(
            provider.session.get.call_args.kwargs["params"]["emailTypeId"], "26"
        )
        self.assertEqual(
            provider.session.get.call_args.kwargs["params"]["buyMode"], 0
        )

    def test_purchase_bound_address_is_rejected(self):
        provider = self._provider()
        provider.session = MagicMock()
        provider.session.get.return_value = response(
            {
                "code": 200,
                "data": {
                    "orders": [
                        {"orderId": "1", "email": "taken@gmail.com"}
                    ]
                },
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            mail, "STATE_FILE", Path(temp_dir) / "state.json"
        ), patch.object(
            mail,
            "_known_mailbox_bindings",
            return_value={"taken@gmail.com": "registered_account"},
        ):
            with self.assertRaises(mail.OnlineDisposableMailError) as caught:
                provider.create_mailbox()
        provider.close()
        self.assertEqual(caught.exception.business_error, "already_registered")

    def test_wait_for_code_returns_code_payload(self):
        provider = self._provider()
        provider.session = MagicMock()
        provider.session.get.return_value = response(
            {"code": 200, "data": {"code": "869550", "content": None}}
        )
        mailbox = {"address": "a@gmail.com", "order_id": "9"}
        provider.wait_timeout = 3.0
        code = provider.wait_for_code(mailbox)
        provider.close()
        self.assertEqual(code, "869550")
        self.assertTrue(mailbox["_registration_code_received"])

    def test_wait_for_code_extracts_from_html_content(self):
        provider = self._provider()
        provider.session = MagicMock()
        # OpenAI's own mail template is decoded by the shared extract_code
        # (background-color #F3F3F3 code box).
        provider.session.get.return_value = response(
            {
                "code": 200,
                "data": {
                    "code": None,
                    "content": (
                        '<td style="background-color: #F3F3F3; border-radius: 8px;">'
                        " 869550 </td>"
                    ),
                },
            }
        )
        mailbox = {"address": "a@gmail.com", "order_id": "9"}
        code = provider.wait_for_code(mailbox)
        provider.close()
        self.assertEqual(code, "869550")

    def test_wait_for_code_generic_html_fallback(self):
        provider = self._provider()
        provider.session = MagicMock()
        # Other services' undecoded mail falls back to the first standalone
        # 4-8 digit number once tags are stripped.
        provider.session.get.return_value = response(
            {
                "code": 200,
                "data": {
                    "code": None,
                    "content": "Use <b>869550</b> to finish signing in",
                },
            }
        )
        mailbox = {"address": "a@gmail.com", "order_id": "9"}
        code = provider.wait_for_code(mailbox)
        provider.close()
        self.assertEqual(code, "869550")

    def test_wait_for_code_pending_then_terminal(self):
        provider = self._provider()
        provider.session = MagicMock()
        provider.session.get.side_effect = [
            response({"code": 42001, "msg": "等待接取验证码中..."}),
            response({"code": 42002, "msg": "订单已关闭"}),
        ]
        mailbox = {"address": "a@gmail.com", "order_id": "9"}
        with self.assertRaises(mail.OnlineDisposableMailError) as caught:
            provider.wait_for_code(mailbox)
        provider.close()
        self.assertEqual(caught.exception.business_code, 42002)
        self.assertIn("42002", mailbox.get("_last_code_wait_error", ""))

    def test_wait_for_code_timeout_marks_outcome(self):
        provider = self._provider()
        provider.session = MagicMock()
        provider.session.get.return_value = response(
            {"code": 42001, "msg": "等待接取验证码中..."}
        )
        mailbox = {"address": "a@gmail.com", "order_id": "9"}
        provider.wait_timeout = 0.5
        code = provider.wait_for_code(mailbox)
        provider.close()
        self.assertIsNone(code)
        self.assertTrue(mailbox["_odm_wait_timed_out"])
        self.assertEqual(mailbox["_registration_outcome"], "otp_timeout")

    def test_wait_for_code_skips_seen_code(self):
        provider = self._provider()
        provider.session = MagicMock()
        provider.session.get.return_value = response(
            {"code": 200, "data": {"code": "111222", "content": None}}
        )
        mailbox = {
            "address": "a@gmail.com",
            "order_id": "9",
            "_odm_seen_codes": ["111222"],
        }
        provider.wait_timeout = 0.5
        code = provider.wait_for_code(mailbox)
        provider.close()
        self.assertIsNone(code)
        self.assertIn(
            "previous code", mailbox.get("_last_code_wait_error", "")
        )

    def test_availability_summary_gates_on_balance(self):
        provider = self._provider()
        provider.session = MagicMock()
        provider.session.get.return_value = response(
            {"code": 200, "data": 46.9}
        )
        summary = provider.availability_summary(force=True)
        provider.close()
        self.assertEqual(summary["balance"], 46.9)
        self.assertEqual(summary["capacity"], 50)

        provider = self._provider()
        provider.session = MagicMock()
        provider.session.get.return_value = response(
            {"code": 200, "data": 0.0}
        )
        summary = provider.availability_summary(force=True)
        provider.close()
        self.assertEqual(summary["capacity"], 0)

    def test_module_create_mailbox_routes_to_provider(self):
        provider = self._provider()
        provider.session = MagicMock()
        provider.session.get.return_value = response(
            {
                "code": 200,
                "data": {
                    "orders": [{"orderId": "77", "email": "routed@gmail.com"}]
                },
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            mail, "STATE_FILE", Path(temp_dir) / "state.json"
        ), patch.object(
            mail, "_known_mailbox_bindings", return_value={}
        ), patch.object(
            mail, "_build_provider", return_value=provider
        ):
            mailbox = mail.create_mailbox(
                {"providers": [self._entry()], "wait_interval": 0.2},
                provider_type="online_disposablemail",
            )
        self.assertEqual(mailbox["provider"], "online_disposablemail")
        self.assertEqual(mailbox["order_id"], "77")

    def test_mark_mailbox_result_records_odm_outcome(self):
        mailbox = {
            "provider": "online_disposablemail",
            "address": "marked@gmail.com",
            "_registration_metrics_defer_terminal": False,
            "_registration_metric_attempt_id": "odm-attempt-1",
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            mail, "STATE_FILE", Path(temp_dir) / "state.json"
        ), patch.object(
            mail, "_record_mail_registration_outcome"
        ) as record:
            mail.mark_mailbox_result(mailbox, success=False, error="失败")
            state_failed = dict(
                mail._load_state().get("marked@gmail.com") or {}
            )
            mail.mark_mailbox_result(mailbox, success=True)
            state_used = dict(
                mail._load_state().get("marked@gmail.com") or {}
            )
        record.assert_any_call(mailbox, "other_failed")
        record.assert_any_call(mailbox, "success")
        self.assertEqual(state_failed.get("state"), "failed")
        self.assertEqual(state_used.get("state"), "used")

    def test_release_mailbox_releases_reservation(self):
        mailbox = {
            "provider": "online_disposablemail",
            "address": "rel@gmail.com",
            "_registration_metrics_defer_terminal": False,
            "_registration_metric_attempt_id": "odm-attempt-2",
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            mail, "STATE_FILE", Path(temp_dir) / "state.json"
        ):
            mail._set_state("rel@gmail.com", "in_use")
            mail.release_mailbox(mailbox)
        self.assertNotIn("rel@gmail.com", mail._load_state())


class OnlineDisposableMailMetricsTests(unittest.TestCase):
    def test_begin_attempt_accepts_odm_provider(self):
        import os
        with tempfile.TemporaryDirectory() as temp_dir:
            os.environ["MAIL_REGISTRATION_METRICS_DB"] = str(
                Path(temp_dir) / "metrics.sqlite3"
            )
            try:
                from common import mail_registration_metrics as metrics

                mailbox = {
                    "provider": "online_disposablemail",
                    "domain": "gmail.com",
                    "_smsbower_pair_role": "primary",
                }
                token = metrics.begin_attempt(mailbox)
                self.assertIsNotNone(token)
                self.assertTrue(
                    metrics.finish_attempt(token, "otp_timeout")
                )
                summary = metrics.get_summary("online_disposablemail")
                self.assertTrue(summary["ok"])
                self.assertEqual(
                    summary["totals"]["otp_timeout"], 1
                )
            finally:
                os.environ.pop("MAIL_REGISTRATION_METRICS_DB", None)

    def test_begin_attempt_rejects_unknown_provider(self):
        import os
        with tempfile.TemporaryDirectory() as temp_dir:
            os.environ["MAIL_REGISTRATION_METRICS_DB"] = str(
                Path(temp_dir) / "metrics.sqlite3"
            )
            try:
                from common import mail_registration_metrics as metrics

                self.assertIsNone(
                    metrics.begin_attempt({"provider": "outlook_token"})
                )
            finally:
                os.environ.pop("MAIL_REGISTRATION_METRICS_DB", None)

    def test_legacy_schema_migrates_to_multi_provider(self):
        import os
        import sqlite3
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "metrics.sqlite3"
            os.environ["MAIL_REGISTRATION_METRICS_DB"] = str(db_path)
            try:
                # Build a legacy single-provider database first.
                connection = sqlite3.connect(str(db_path))
                connection.executescript(
                    """
                    CREATE TABLE mail_registration_attempts (
                        attempt_key TEXT PRIMARY KEY CHECK(length(attempt_key) = 64),
                        provider TEXT NOT NULL CHECK(provider = 'smsbower_mail'),
                        domain TEXT NOT NULL CHECK(domain IN ('gmail.com', 'icloud.com', 'unknown')),
                        role TEXT NOT NULL CHECK(role IN ('primary', 'child')),
                        outcome TEXT CHECK(outcome IN ('success', 'already_registered',
                            'otp_timeout', 'other_failed', 'cancelled')),
                        started_at REAL NOT NULL,
                        finished_at REAL
                    );
                    CREATE TABLE mail_registration_otp_waits (
                        attempt_key TEXT NOT NULL REFERENCES mail_registration_attempts(attempt_key),
                        wait_key TEXT NOT NULL CHECK(length(wait_key) = 64),
                        elapsed_seconds REAL NOT NULL CHECK(elapsed_seconds >= 0),
                        received INTEGER NOT NULL CHECK(received IN (0, 1)),
                        recorded_at REAL NOT NULL,
                        PRIMARY KEY (attempt_key, wait_key)
                    );
                    """
                )
                connection.execute(
                    "INSERT INTO mail_registration_attempts "
                    "(attempt_key, provider, domain, role, started_at) "
                    "VALUES (?, 'smsbower_mail', 'gmail.com', 'primary', ?)",
                    ("a" * 64, time.time()),
                )
                connection.commit()
                connection.close()

                from common import mail_registration_metrics as metrics

                mailbox = {
                    "provider": "online_disposablemail",
                    "domain": "gmail.com",
                }
                token = metrics.begin_attempt(mailbox)
                self.assertIsNotNone(token)

                connection = sqlite3.connect(str(db_path))
                rows = connection.execute(
                    "SELECT provider FROM mail_registration_attempts"
                ).fetchall()
                connection.close()
                providers = {row[0] for row in rows}
                self.assertIn("online_disposablemail", providers)
                self.assertIn("smsbower_mail", providers)
            finally:
                os.environ.pop("MAIL_REGISTRATION_METRICS_DB", None)


class DynamicMailOptionsTests(unittest.TestCase):
    def test_needs_dynamic_coordinator_for_odm(self):
        from register.mail_channel_options import needs_dynamic_mail_coordinator

        config = {"providers": [self._odm_entry()]}
        self.assertTrue(
            needs_dynamic_mail_coordinator(config, "online_disposablemail")
        )
        self.assertTrue(needs_dynamic_mail_coordinator(config, "auto"))
        self.assertFalse(needs_dynamic_mail_coordinator(config, "outlook"))

    def test_validate_dynamic_mail_job_rejects_fixed_email(self):
        from register.mail_channel_options import validate_dynamic_mail_job

        with self.assertRaises(ValueError):
            validate_dynamic_mail_job(
                {"providers": [self._odm_entry()]},
                "online_disposablemail",
                2,
                2,
                fixed_email="a@gmail.com",
            )

    def _odm_entry(self):
        return {
            "type": "online_disposablemail",
            "enable": True,
            "api_key": "k",
            "service_id": "242",
            "email_type_id": "3",
        }


if __name__ == "__main__":
    unittest.main()

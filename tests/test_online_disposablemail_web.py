from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import urlsplit

import pytest
import requests
from fastapi import HTTPException

import mail
import web_app
from config_loader import DEFAULT_CONFIG
from web.routes import mailbox as mailbox_routes


SALES_PATH = "/api/guest/type/selects"
CATALOG_PATHS = ["/api/services", SALES_PATH]


def _provider(**overrides):
    entry = {
        "type": "online_disposablemail",
        "enable": True,
        "label": "Mailbox fixture",
        "api_base": "https://mail-api.example.test",
        "api_key": "saved-odm-secret",
        "service_id": "242",
        "email_type_id": "26",
        "capacity": 50,
        "mailboxes": "",
    }
    entry.update(overrides)
    return entry


def _config(**provider_overrides):
    return {
        "mail": {"providers": [
            _provider(**provider_overrides),
            {"type": "outlook_token", "mailboxes": "preserved-mailbox-fixture", "enable": False},
        ]},
        "unrelated_setting": {"value": "preserve this"},
    }


def _response(payload, status=200):
    result = MagicMock(status_code=status, text=json.dumps(payload))
    result.json.return_value = payload
    return result


def _patch_config(monkeypatch, cfg):
    read = MagicMock(return_value=cfg)
    write = MagicMock()
    monkeypatch.setattr(web_app, "_read_config", read)
    monkeypatch.setattr(web_app, "_write_config", write)
    monkeypatch.setattr(web_app, "_public_mail_stats", MagicMock(return_value={}))
    monkeypatch.setattr(web_app, "PROGRESS", MagicMock())
    return SimpleNamespace(read=read, write=write)


def _save(**kwargs):
    body = mailbox_routes.MailboxesUpdate(
        provider_type="online_disposablemail", mailboxes="", append=True, **kwargs
    )
    return asyncio.run(mailbox_routes.put_mailboxes(body))


@pytest.fixture(autouse=True)
def _forbid_external_io(monkeypatch):
    network = MagicMock(side_effect=AssertionError("External HTTP is forbidden in these tests"))
    purchase = MagicMock(side_effect=AssertionError("Configuration must not purchase a mailbox"))
    monkeypatch.setattr(requests.sessions.Session, "request", network)
    monkeypatch.setattr(mail.OnlineDisposableMailProvider, "buy_mailbox", purchase)
    with mail._ONLINE_DISPOSABLEMAIL_TYPES_CACHE_LOCK:
        mail._ONLINE_DISPOSABLEMAIL_TYPES_CACHE.clear()
    yield
    network.assert_not_called()
    purchase.assert_not_called()


@pytest.fixture
def odm_http(monkeypatch):
    http = SimpleNamespace(
        calls=[],
        sessions=[],
        request_sessions=[],
        before_request=lambda _path: None,
        responses={
            "/api/services": _response({"code": 200, "data": [{"id": 242, "name": "Service fixture"}]}),
            # The old dictionary can still contain IDs that cannot be bought.
            "/api/types": _response({"code": 200, "data": [{"id": 3, "name": "Gmail"}]}),
        },
        sales_by_service={
            "242": _response({"code": 200, "data": [{
                "emailTypeId": "26", "emailTypeName": "Gmail_1",
                "availableNum": 165000, "price": 0.05,
            }]}),
            "243": _response({"code": 200, "data": [{
                "emailTypeId": "27", "emailTypeName": "Other service mailbox",
                "availableNum": 200, "price": 0.08,
            }]}),
        },
    )

    def get(session, url, **kwargs):
        http.calls.append((url, kwargs))
        path = urlsplit(url).path
        http.request_sessions.append((path, session))
        assert path in http.responses or path == SALES_PATH, f"Unexpected endpoint: {path}"
        http.before_request(path)
        if path == SALES_PATH and path not in http.responses:
            service_id = str(kwargs["params"].get("serviceId"))
            result = http.sales_by_service.get(service_id, _response({"code": 200, "data": []}))
        else:
            result = http.responses[path]
        if isinstance(result, Exception):
            raise result
        return result

    def make_session():
        session = MagicMock()
        session.headers = {}
        session.params = {}
        session.cookies = requests.cookies.RequestsCookieJar()
        session.get.side_effect = lambda url, **kwargs: get(session, url, **kwargs)
        http.sessions.append(session)
        return session

    monkeypatch.setattr(mail, "_http_session", make_session)
    return http


def _assert_closed(http):
    assert http.sessions
    for session in http.sessions:
        session.close.assert_called_once()


def test_new_odm_config_defaults_to_current_gmail_sales_id():
    entry = next(item for item in DEFAULT_CONFIG["mail"]["providers"] if item["type"] == "online_disposablemail")
    assert entry["enable"] is False
    assert (entry["service_id"], entry["email_type_id"]) == ("242", "26")


@pytest.mark.parametrize("legacy_fields", [False, True])
def test_catalog_route_preserves_the_public_ui_contract(monkeypatch, odm_http, legacy_fields):
    cfg = _config()
    before = deepcopy(cfg)
    env = _patch_config(monkeypatch, cfg)
    if legacy_fields:
        odm_http.responses["/api/services"] = _response({"code": 200, "data": [
            {"serviceId": 242, "serviceName": "Service fixture"}
        ]})

    result = asyncio.run(mailbox_routes.online_disposablemail_services(
        mailbox_routes.OnlineDisposableMailInfoRequest()
    ))

    assert result["ok"] is True
    assert [(item["service_id"], item["name"]) for item in result["services"]] == [("242", "Service fixture")]
    assert [(item["email_type_id"], item["name"]) for item in result["types"]] == [("26", "Gmail_1")]
    assert result["types"][0]["available_num"] == 165000
    assert [urlsplit(url).path for url, _ in odm_http.calls] == CATALOG_PATHS
    assert odm_http.calls[-1][1]["params"] == {"pageSize": 10000, "pageNum": 1, "serviceId": "242"}
    for _, kwargs in odm_http.calls:
        assert "apiKey" not in kwargs["params"]
    assert odm_http.request_sessions[0][1] is not odm_http.request_sessions[1][1]
    env.write.assert_not_called()
    assert cfg == before
    _assert_closed(odm_http)


def test_catalog_route_uses_requested_service_instead_of_saved_selection(monkeypatch, odm_http):
    cfg = _config()
    before = deepcopy(cfg)
    env = _patch_config(monkeypatch, cfg)
    odm_http.responses["/api/services"] = _response({"code": 200, "data": [
        {"id": 242, "name": "First service"},
        {"id": 243, "name": "Second service"},
    ]})

    result = asyncio.run(mailbox_routes.online_disposablemail_services(
        mailbox_routes.OnlineDisposableMailInfoRequest(service_id="243")
    ))

    assert result["ok"] is True
    assert [(item["email_type_id"], item["name"]) for item in result["types"]] == [
        ("27", "Other service mailbox")
    ]
    assert odm_http.calls[-1][1]["params"]["serviceId"] == "243"
    assert cfg == before
    env.write.assert_not_called()
    _assert_closed(odm_http)


def test_manual_catalog_reload_refreshes_sales_even_while_cache_is_fresh(monkeypatch, odm_http):
    env = _patch_config(monkeypatch, _config())

    first = asyncio.run(mailbox_routes.online_disposablemail_services(
        mailbox_routes.OnlineDisposableMailInfoRequest()
    ))
    odm_http.sales_by_service["242"] = _response({"code": 200, "data": [{
        "emailTypeId": "28", "emailTypeName": "Updated Gmail listing",
        "availableNum": 15, "price": 0.06,
    }]})
    second = asyncio.run(mailbox_routes.online_disposablemail_services(
        mailbox_routes.OnlineDisposableMailInfoRequest()
    ))

    assert [item["email_type_id"] for item in first["types"]] == ["26"]
    assert [item["email_type_id"] for item in second["types"]] == ["28"]
    assert [urlsplit(url).path for url, _ in odm_http.calls] == CATALOG_PATHS * 2
    env.write.assert_not_called()
    _assert_closed(odm_http)


def test_sales_catalog_does_not_share_provider_session_credentials(monkeypatch, odm_http):
    _patch_config(monkeypatch, _config())

    def set_provider_credentials(path):
        if path == "/api/services":
            session = odm_http.request_sessions[-1][1]
            session.headers["Authorization"] = "saved-provider-authorization"
            session.params["apiKey"] = "saved-odm-secret"
            session.cookies.set("provider_login", "saved-provider-cookie")

    odm_http.before_request = set_provider_credentials

    result = asyncio.run(mailbox_routes.online_disposablemail_services(
        mailbox_routes.OnlineDisposableMailInfoRequest()
    ))

    assert result["ok"] is True
    services_session = odm_http.request_sessions[0][1]
    sales_session = odm_http.request_sessions[1][1]
    assert sales_session is not services_session
    assert "Authorization" not in sales_session.headers
    assert "apiKey" not in sales_session.params
    assert "provider_login" not in sales_session.cookies
    assert "apiKey" not in odm_http.calls[-1][1]["params"]
    _assert_closed(odm_http)


@pytest.mark.parametrize("path", CATALOG_PATHS)
@pytest.mark.parametrize("failure_kind", ["timeout", "http", "json", "catalog", "business"])
def test_catalog_route_reports_upstream_failure_and_closes(monkeypatch, odm_http, path, failure_kind):
    cfg = _config()
    before = deepcopy(cfg)
    env = _patch_config(monkeypatch, cfg)
    if failure_kind == "timeout":
        odm_http.responses[path] = requests.Timeout("directory request timed out")
    elif failure_kind == "http":
        odm_http.responses[path] = _response({"msg": "directory unavailable"}, status=503)
    elif failure_kind == "json":
        odm_http.responses[path] = _response({})
        odm_http.responses[path].json.side_effect = ValueError("invalid JSON")
    elif failure_kind == "business":
        odm_http.responses[path] = _response({"code": 41001, "msg": "Invalid parameter"})
    else:
        odm_http.responses[path] = _response({"code": 200, "data": [{"name": "Missing ID"}]})

    with pytest.raises(HTTPException) as raised:
        asyncio.run(mailbox_routes.online_disposablemail_services(
            mailbox_routes.OnlineDisposableMailInfoRequest()
        ))

    assert raised.value.status_code == 503
    assert "saved-odm-secret" not in str(raised.value.detail)
    env.write.assert_not_called()
    assert cfg == before
    _assert_closed(odm_http)


@pytest.mark.parametrize("submitted_key", [None, "", "saved-…cret", "********"])
def test_save_validates_ids_preserves_existing_key_and_other_config(monkeypatch, odm_http, submitted_key):
    cfg = _config(enable=False)
    other_provider = deepcopy(cfg["mail"]["providers"][1])
    env = _patch_config(monkeypatch, cfg)
    kwargs = {
        "api_base": "mail-api.example.test/api/mailbox",
        "service_id": "242",
        "email_type_id": "26",
        "enable": True,
    }
    if submitted_key is not None:
        kwargs["api_key"] = submitted_key

    result = _save(**kwargs)

    assert result["ok"] is True
    saved = env.write.call_args.args[0]
    entry = saved["mail"]["providers"][0]
    assert entry["api_key"] == "saved-odm-secret"
    assert entry["api_base"] == "https://mail-api.example.test"
    assert (entry["service_id"], entry["email_type_id"]) == ("242", "26")
    assert entry["mode"] == "api"
    assert saved["mail"]["providers"][1] == other_provider
    assert saved["unrelated_setting"] == {"value": "preserve this"}
    env.write.assert_called_once()
    assert [urlsplit(url).path for url, _ in odm_http.calls] == CATALOG_PATHS
    _assert_closed(odm_http)


def test_catalog_validation_finishes_before_entering_the_write_transaction(monkeypatch, odm_http):
    cfg = _config()
    _patch_config(monkeypatch, cfg)
    events = []
    real_edit = web_app._edit_config

    @contextmanager
    def observed_edit():
        events.append("write transaction")
        with real_edit() as current:
            yield current

    monkeypatch.setattr(web_app, "_edit_config", observed_edit)
    odm_http.before_request = events.append

    _save(api_base="https://selected-mail-api.example.test")

    assert events == [*CATALOG_PATHS, "write transaction"]


@pytest.mark.parametrize("field,value", [
    ("service_id", "not-a-number"),
    ("email_type_id", "not-a-number"),
    ("api_base", "ftp://mail-api.example.test"),
])
def test_invalid_local_settings_are_rejected_without_network_or_commit(monkeypatch, odm_http, field, value):
    cfg = _config()
    before = deepcopy(cfg)
    env = _patch_config(monkeypatch, cfg)

    with pytest.raises(HTTPException) as raised:
        _save(**{field: value})

    assert raised.value.status_code == 400
    assert not odm_http.calls
    env.write.assert_not_called()
    assert cfg == before


@pytest.mark.parametrize("field", ["service_id", "email_type_id"])
def test_unknown_selection_is_a_field_error_without_config_commit(monkeypatch, odm_http, field):
    cfg = _config()
    before = deepcopy(cfg)
    env = _patch_config(monkeypatch, cfg)

    with pytest.raises(HTTPException) as raised:
        _save(**{field: "909090", "api_key": "replacement-key-that-must-not-be-saved"})

    assert raised.value.status_code == 400
    assert field in str(raised.value.detail)
    assert "saved-odm-secret" not in str(raised.value.detail)
    env.write.assert_not_called()
    assert cfg == before
    _assert_closed(odm_http)


@pytest.mark.parametrize("was_enabled", [False, True])
def test_dictionary_id_without_service_sales_listing_cannot_be_saved(monkeypatch, odm_http, was_enabled):
    cfg = _config(enable=was_enabled)
    before = deepcopy(cfg)
    env = _patch_config(monkeypatch, cfg)
    # The obsolete global dictionary claims Gmail is ID 3; the actual service's
    # sales catalog contains only ID 26, so it cannot validate a purchase choice.
    assert odm_http.responses["/api/types"].json()["data"][0]["id"] == 3

    with pytest.raises(HTTPException) as raised:
        _save(enable=True, service_id="242", email_type_id="3")

    assert raised.value.status_code == 400
    assert "email_type_id" in str(raised.value.detail)
    assert [urlsplit(url).path for url, _ in odm_http.calls] == CATALOG_PATHS
    assert cfg == before
    env.write.assert_not_called()
    _assert_closed(odm_http)


def test_type_sold_for_another_service_does_not_validate_selected_pair(monkeypatch, odm_http):
    cfg = _config()
    before = deepcopy(cfg)
    env = _patch_config(monkeypatch, cfg)
    odm_http.responses["/api/services"] = _response({"code": 200, "data": [
        {"id": 242, "name": "First service"},
        {"id": 243, "name": "Second service"},
    ]})

    with pytest.raises(HTTPException) as raised:
        _save(service_id="243", email_type_id="26")

    assert raised.value.status_code == 400
    assert "email_type_id" in str(raised.value.detail)
    assert odm_http.calls[-1][1]["params"]["serviceId"] == "243"
    assert cfg == before
    env.write.assert_not_called()
    _assert_closed(odm_http)


@pytest.mark.parametrize("failure_kind", ["timeout", "http", "catalog"])
def test_validation_unavailability_is_not_reported_as_an_invalid_id(monkeypatch, odm_http, failure_kind):
    cfg = _config(enable=False)
    before = deepcopy(cfg)
    env = _patch_config(monkeypatch, cfg)
    if failure_kind == "timeout":
        odm_http.responses[SALES_PATH] = requests.Timeout("catalog timed out")
    elif failure_kind == "http":
        odm_http.responses[SALES_PATH] = _response({"msg": "catalog unavailable"}, status=503)
    else:
        odm_http.responses[SALES_PATH] = _response({"code": 200, "data": [{"emailTypeId": "26"}]})

    with pytest.raises(HTTPException) as raised:
        _save(enable=True, service_id="242", email_type_id="26", api_key="unsaved-new-key")

    assert raised.value.status_code == 503
    env.write.assert_not_called()
    assert cfg == before
    _assert_closed(odm_http)


@pytest.mark.parametrize("current_enabled,kwargs", [
    (True, {"enable": False, "service_id": "242", "email_type_id": "26"}),
    (False, {"service_id": "242", "email_type_id": "26"}),
    (True, {"capacity": 75}),
    (True, {"label": "Updated label"}),
    (True, {"api_key": "replacement-key"}),
    (True, {
        "enable": True,
        "api_base": "mail-api.example.test/api/mailbox",
        "service_id": "242",
        "email_type_id": "26",
        "api_key": "replacement-key",
        "capacity": 75,
    }),
])
def test_disabled_or_ordinary_updates_do_not_depend_on_catalog_availability(monkeypatch, odm_http, current_enabled, kwargs):
    cfg = _config(enable=current_enabled)
    env = _patch_config(monkeypatch, cfg)
    odm_http.responses["/api/services"] = requests.Timeout("must never be requested")

    result = _save(**kwargs)

    assert result["ok"] is True
    env.write.assert_called_once()
    assert not odm_http.calls
    entry = env.write.call_args.args[0]["mail"]["providers"][0]
    assert (entry["service_id"], entry["email_type_id"]) == ("242", "26")
    if kwargs.get("enable") is False:
        assert entry["enable"] is False
    if "api_key" in kwargs:
        assert entry["api_key"] == kwargs["api_key"]


@pytest.mark.parametrize("kwargs", [
    {"enable": False},
    {"capacity": 75},
    {"api_key": "replacement-key"},
    {"enable": True, "service_id": "242", "email_type_id": "3"},
])
def test_maintenance_preserves_saved_legacy_id_without_network_or_silent_migration(monkeypatch, odm_http, kwargs):
    cfg = _config(email_type_id="3")
    env = _patch_config(monkeypatch, cfg)
    odm_http.responses["/api/services"] = requests.Timeout("must never be requested")

    result = _save(**kwargs)

    assert result["ok"] is True
    env.write.assert_called_once()
    assert not odm_http.calls
    assert env.write.call_args.args[0]["mail"]["providers"][0]["email_type_id"] == "3"


@pytest.mark.parametrize("current_enabled", [False, True])
def test_reenabling_validates_but_unchanged_enabled_form_skips_lookup(monkeypatch, odm_http, current_enabled):
    cfg = _config(enable=current_enabled)
    env = _patch_config(monkeypatch, cfg)

    result = _save(enable=True)

    assert result["ok"] is True
    env.write.assert_called_once()
    if current_enabled:
        assert not odm_http.calls
    else:
        assert [urlsplit(url).path for url, _ in odm_http.calls] == CATALOG_PATHS
        _assert_closed(odm_http)


def test_new_enabled_provider_is_validated_before_being_saved(monkeypatch, odm_http):
    cfg = _config()
    cfg["mail"]["providers"] = cfg["mail"]["providers"][1:]
    env = _patch_config(monkeypatch, cfg)

    result = _save(
        enable=True,
        api_base="https://mail-api.example.test",
        service_id="242",
        email_type_id="26",
        api_key="new-provider-key",
    )

    assert result["ok"] is True
    env.write.assert_called_once()
    saved = env.write.call_args.args[0]
    entry = next(item for item in saved["mail"]["providers"] if item["type"] == "online_disposablemail")
    assert (entry["service_id"], entry["email_type_id"]) == ("242", "26")
    assert entry["api_key"] == "new-provider-key"
    assert [urlsplit(url).path for url, _ in odm_http.calls] == CATALOG_PATHS
    _assert_closed(odm_http)


@pytest.mark.parametrize("field,value,submitted", [
    ("api_base", "https://concurrent-mail-api.example.test", {"service_id": "243", "email_type_id": "27"}),
    ("service_id", "909090", {"api_base": "https://selected-mail-api.example.test"}),
    ("email_type_id", "909090", {"api_base": "https://selected-mail-api.example.test"}),
    ("enable", False, {"api_base": "https://selected-mail-api.example.test"}),
])
def test_changed_selection_during_preflight_returns_conflict_without_commit(monkeypatch, odm_http, field, value, submitted):
    cfg = _config()
    env = _patch_config(monkeypatch, cfg)
    env.read.side_effect = lambda: deepcopy(cfg)
    odm_http.responses["/api/services"] = _response({"code": 200, "data": [
        {"id": 242, "name": "First service fixture"},
        {"id": 243, "name": "Second service fixture"},
    ]})
    after_concurrent_change = deepcopy(cfg)
    after_concurrent_change["mail"]["providers"][0][field] = value

    def concurrent_change(path):
        if path == SALES_PATH:
            cfg["mail"]["providers"][0][field] = value

    odm_http.before_request = concurrent_change

    with pytest.raises(HTTPException) as raised:
        _save(**submitted)

    assert raised.value.status_code == 409
    env.write.assert_not_called()
    assert cfg == after_concurrent_change
    _assert_closed(odm_http)


def test_unrelated_concurrent_changes_and_new_key_are_preserved(monkeypatch, odm_http):
    cfg = _config()
    env = _patch_config(monkeypatch, cfg)
    env.read.side_effect = lambda: deepcopy(cfg)

    def concurrent_change(path):
        if path == SALES_PATH:
            cfg["mail"]["providers"][0]["api_key"] = "concurrently-updated-secret"
            cfg["unrelated_setting"]["value"] = "concurrent update"

    odm_http.before_request = concurrent_change

    result = _save(api_base="https://selected-mail-api.example.test")

    assert result["ok"] is True
    env.write.assert_called_once()
    saved = env.write.call_args.args[0]
    assert saved["mail"]["providers"][0]["api_key"] == "concurrently-updated-secret"
    assert saved["unrelated_setting"]["value"] == "concurrent update"
    _assert_closed(odm_http)

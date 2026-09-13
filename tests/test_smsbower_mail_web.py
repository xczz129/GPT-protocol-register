from __future__ import annotations

import asyncio
import os
from copy import deepcopy
from unittest.mock import patch

import pytest
from fastapi import HTTPException

import mail
import web_app
from config_loader import DEFAULT_CONFIG


def _provider(**overrides):
    entry = {
        "type": "smsbower_mail",
        "enable": True,
        "label": "SMSBower Mail",
        "api_base": "https://mail-api.example.test",
        "api_key": "saved-mail-secret",
        "service": "dr",
        "domains": ["gmail.com", "icloud.com"],
        "max_price": "0.25",
        "capacity": 0,
        "mailboxes": "",
    }
    entry.update(overrides)
    return entry


def test_default_config_contains_disabled_smsbower_mail_provider():
    entry = next(
        item
        for item in DEFAULT_CONFIG["mail"]["providers"]
        if item["type"] == "smsbower_mail"
    )

    assert entry["enable"] is False
    assert entry["api_base"] == "https://smsbower.page"
    assert entry["service"] == "dr"
    assert entry["domains"] == ["gmail.com", "icloud.com"]
    assert entry["max_price"] == ""


def test_config_redaction_exposes_only_smsbower_key_metadata():
    cfg = {"mail": {"providers": [_provider()]}}

    redacted = web_app._redact_config_for_api(cfg)
    entry = redacted["mail"]["providers"][0]

    assert "api_key" not in entry
    assert "key" not in entry
    assert entry["has_api_key"] is True
    assert entry["api_key_masked"]
    assert "saved-mail-secret" not in repr(redacted)
    assert cfg["mail"]["providers"][0]["api_key"] == "saved-mail-secret"


def test_environment_key_is_reported_without_exposing_its_value():
    secret = "environment-mail-secret"
    cfg = {"mail": {"providers": [_provider(enable=False, api_key="")]}}

    with patch.dict(os.environ, {"SMSBOWER_MAIL_API_KEY": secret}, clear=True):
        redacted = web_app._redact_config_for_api(cfg)
        with (
            patch.object(web_app, "count_pool_capacity", return_value={}),
            patch.object(web_app, "_load_state_file", return_value={}),
        ):
            stats = web_app._mail_stats(cfg)

    config_entry = redacted["mail"]["providers"][0]
    stats_entry = stats["providers"][0]
    assert config_entry["has_api_key"] is True
    assert config_entry["api_key_masked"] == ""
    assert stats_entry["has_api_key"] is True
    assert stats_entry["api_key_masked"] == ""
    assert secret not in repr(redacted)
    assert secret not in repr(stats)


@pytest.mark.parametrize("submitted_key", [None, "", "saved-…cret", "********"])
def test_mailbox_update_preserves_key_and_normalizes_settings(submitted_key):
    cfg = {"mail": {"providers": [_provider()]}}
    body_kwargs = {
        "provider_type": "smsbower_mail",
        "mailboxes": "",
        "append": True,
        "api_base": "mail-api.example.test/api/mail/getActivation",
        "service": " DR ",
        "domains": ["icloud.com", "gmail.com", "icloud.com"],
        "max_price": "0.40",
        "enable": True,
    }
    if submitted_key is not None:
        body_kwargs["api_key"] = submitted_key
    body = web_app.MailboxesUpdate(**body_kwargs)

    with (
        patch.object(web_app, "_read_config", return_value=cfg),
        patch.object(web_app, "_write_config") as write_config,
        patch.object(web_app, "_public_mail_stats", return_value={}),
    ):
        result = asyncio.run(web_app.put_mailboxes(body))

    saved = cfg["mail"]["providers"][0]
    assert result["ok"] is True
    assert saved["api_key"] == "saved-mail-secret"
    assert saved["api_base"] == "https://mail-api.example.test"
    assert saved["service"] == "dr"
    assert saved["domains"] == ["icloud.com", "gmail.com"]
    assert saved["max_price"] == "0.4"
    assert saved["mode"] == "api"
    write_config.assert_called_once_with(cfg)


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_price", "0"),
        ("max_price", "-1"),
        ("max_price", "not-a-number"),
        ("domains", []),
        ("domains", ["outlook.com"]),
        ("api_base", "ftp://mail-api.example.test"),
    ],
)
def test_mailbox_update_rejects_invalid_smsbower_settings(field, value):
    cfg = {"mail": {"providers": [_provider()]}}
    kwargs = {
        "provider_type": "smsbower_mail",
        "mailboxes": "",
        "domains": ["gmail.com"],
        "max_price": "0.25",
    }
    kwargs[field] = value
    body = web_app.MailboxesUpdate(**kwargs)

    with (
        patch.object(web_app, "_read_config", return_value=cfg),
        patch.object(web_app, "_write_config") as write_config,
    ):
        with pytest.raises(HTTPException) as raised:
            asyncio.run(web_app.put_mailboxes(body))

    assert raised.value.status_code == 400
    write_config.assert_not_called()


def test_generic_config_merge_preserves_smsbower_key_and_normalizes_provider():
    cfg = {"mail": {"providers": [_provider()]}}
    incoming = deepcopy(cfg)
    incoming["mail"]["providers"][0].update(
        {
            "api_key": "",
            "api_key_masked": "saved-…cret",
            "has_api_key": True,
            "domains": ["icloud", "google", "icloud.com"],
            "max_price": "0.30",
        }
    )

    web_app._merge_config_preserving_secrets(cfg, incoming)
    web_app._sanitize_smsbower_mail_provider_entry(
        cfg["mail"]["providers"][0]
    )

    saved = cfg["mail"]["providers"][0]
    assert saved["api_key"] == "saved-mail-secret"
    assert saved["domains"] == ["icloud.com", "gmail.com"]
    assert saved["max_price"] == "0.3"
    assert "api_key_masked" not in saved
    assert "has_api_key" not in saved


def test_mail_stats_reuses_smsbower_shared_inventory_cache():
    cfg = {"mail": {"providers": [_provider()]}}
    mail._SMSBOWER_MAIL_CAPACITY_CACHE.clear()

    def availability(_self, domain=None):
        return {
            "service": "dr",
            "domain": domain,
            "count": 4 if domain == "gmail.com" else 6,
            "price": 0.2 if domain == "gmail.com" else 0.3,
        }

    try:
        with (
            patch.object(mail, "_known_mailbox_bindings", return_value={}),
            patch.object(mail, "reconcile_registered_mailbox_states", return_value=0),
            patch.object(mail, "_load_state", return_value={}),
            patch.object(web_app, "_load_state_file", return_value={}),
            patch.object(
                mail.SmsBowerMailProvider,
                "availability",
                autospec=True,
                side_effect=availability,
            ) as inventory,
        ):
            stats = web_app._mail_stats(cfg)
    finally:
        mail._SMSBOWER_MAIL_CAPACITY_CACHE.clear()

    provider = stats["providers"][0]
    assert provider["available"] == 10
    assert provider["total"] == 10
    assert provider["domains"] == ["gmail.com", "icloud.com"]
    assert len(provider["inventory"]) == 2
    assert inventory.call_count == 2
    assert provider["has_api_key"] is True
    assert "api_key" not in provider


def test_purchased_smsbower_address_is_listed_from_mailbox_state():
    cfg = {"mail": {"providers": [_provider()]}}
    state = {
        "fresh@gmail.com": {
            "provider": "smsbower_mail",
            "provider_ref": "smsbower_mail#1",
            "api_base": "https://mail-api.example.test",
            "mail_id": "mail-7",
            "state": "used",
            "updated_at": "2026-09-13 03:00:00+0800",
        }
    }

    with (
        patch.object(web_app, "_load_state_file", return_value=state),
        patch.object(web_app, "account_email_set", return_value=set()),
    ):
        rows = web_app._mailbox_items(cfg, provider_type="smsbower_mail")

    assert len(rows) == 1
    assert rows[0]["email"] == "fresh@gmail.com"
    assert rows[0]["provider_type"] == "smsbower_mail"
    assert rows[0]["state"] == "used"
    assert rows[0]["mode"] == "api"


def test_panel_delete_does_not_discard_pending_smsbower_settlement():
    cfg = {"mail": {"providers": [_provider()]}}
    state = {
        "pending@gmail.com": {
            "provider": "smsbower_mail",
            "provider_ref": "smsbower_mail#1",
            "state": "failed",
            "mail_id": "mail-pending",
            "smsbower_pending_status": 2,
        }
    }
    body = web_app.MailboxDeleteRequest(
        provider_type="smsbower_mail",
        items=[
            {
                "provider_type": "smsbower_mail",
                "email": "pending@gmail.com",
            }
        ],
    )

    with (
        patch.object(web_app, "_load_state_file", return_value=state),
        patch.object(web_app, "update_mailbox_states") as update_states,
    ):
        removed = web_app._delete_mailboxes_from_config(cfg, body)

    assert removed == 0
    update_states.assert_not_called()

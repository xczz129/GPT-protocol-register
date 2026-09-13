from __future__ import annotations

import json

from common.redaction import merge_config_preserving_secrets, redact_config_for_api


SMSBOWER_SECRET = "fixture-smsbower-shared-secret"
FIREFOX_PASSWORD = "fixture-firefox-password"
FIREFOX_TOKEN = "fixture-firefox-token"


def _config() -> dict:
    return {
        "sms_code": {
            "enabled": True,
            "api_key": SMSBOWER_SECRET,
            "firefox_api_password": FIREFOX_PASSWORD,
            "firefox_api_token": FIREFOX_TOKEN,
            "firefox_docking_code": "eng-1096-FIXTURE",
            "firefox_docking_code_history": ["eng-1096-OLD"],
            "country_order": "US,GB",
        },
        "mail": {
            "providers": [
                {
                    "type": "smsbower_mail",
                    "enable": False,
                    "api_key": SMSBOWER_SECRET,
                    "domains": ["gmail.com", "icloud.com"],
                    "mailboxes": "",
                }
            ]
        },
        "chatgpt2api": {"key": "fixture-c2a-secret", "enabled": True},
    }


def test_whole_config_redaction_hides_shared_sms_and_mail_secret():
    source = _config()

    public = redact_config_for_api(source)
    serialized = json.dumps(public, ensure_ascii=False)

    assert SMSBOWER_SECRET not in serialized
    assert FIREFOX_PASSWORD not in serialized
    assert FIREFOX_TOKEN not in serialized
    assert public["sms_code"]["api_key"] == ""
    assert public["sms_code"]["firefox_api_password"] == ""
    assert public["sms_code"]["firefox_api_token"] == ""
    assert public["sms_code"]["has_api_key"] is True
    assert public["sms_code"]["has_firefox_api_password"] is True
    assert public["sms_code"]["has_firefox_api_token"] is True
    assert public["mail"]["providers"][0]["api_key"] == ""
    assert source["sms_code"]["api_key"] == SMSBOWER_SECRET


def test_get_put_roundtrip_preserves_three_secrets_without_metadata_pollution():
    stored = _config()
    public = redact_config_for_api(stored)
    public["sms_code"]["enabled"] = False
    public["sms_code"]["firefox_docking_code"] = "eng-1096-UPDATED"

    merge_config_preserving_secrets(stored, public)

    assert stored["sms_code"]["api_key"] == SMSBOWER_SECRET
    assert stored["sms_code"]["firefox_api_password"] == FIREFOX_PASSWORD
    assert stored["sms_code"]["firefox_api_token"] == FIREFOX_TOKEN
    assert stored["sms_code"]["enabled"] is False
    assert stored["sms_code"]["firefox_docking_code"] == "eng-1096-UPDATED"
    assert stored["sms_code"]["firefox_docking_code_history"] == ["eng-1096-OLD"]
    assert not any(
        key.startswith("has_") or key.endswith("_masked")
        for key in stored["sms_code"]
    )


def test_explicit_new_sms_secrets_replace_saved_values():
    stored = _config()
    overlay = {
        "sms_code": {
            "api_key": "replacement-sms-key",
            "firefox_api_password": "replacement-firefox-password",
            "firefox_api_token": "replacement-firefox-token",
            "api_key_masked": "stale-display-value",
            "has_api_key": True,
            "has_firefox_token": True,
        }
    }

    merge_config_preserving_secrets(stored, overlay)

    assert stored["sms_code"]["api_key"] == "replacement-sms-key"
    assert stored["sms_code"]["firefox_api_password"] == "replacement-firefox-password"
    assert stored["sms_code"]["firefox_api_token"] == "replacement-firefox-token"
    assert stored["sms_code"]["country_order"] == "US,GB"
    assert not any(
        key.startswith("has_") or key.endswith("_masked")
        for key in stored["sms_code"]
    )

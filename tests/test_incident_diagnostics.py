"""Public diagnostics retain reasons while hiding authorization/OTP secrets."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from common import account_operation_log as oplog


@pytest.mark.parametrize("code", [
    "invalid_workspace_selected", "incorrect_code", "rate_limit_exceeded", "account_deactivated",
])
def test_known_error_code_survives_structured_and_text_log_roundtrip(tmp_path, code):
    path = tmp_path / "diagnostics.sqlite3"
    original = f'auth failed: {{"code":"{code}","otp":"123456","csrfToken":"csrf-fixture"}}'
    event = oplog.append_operation_event(
        "member@example.test", "import", "failed", error=original,
        metadata={"upstream": {"code": code, "verificationCode": "654321", "accessToken": "token-fixture"}},
        path=path,
    )
    rows, count = oplog.list_operation_events("member@example.test", path=path)
    assert count == 1
    assert event["metadata"]["upstream"]["code"] == rows[0]["metadata"]["upstream"]["code"] == code
    assert code in rows[0]["error"]
    encoded = json.dumps([event, rows])
    for secret in ("123456", "654321", "csrf-fixture", "token-fixture"):
        assert secret not in encoded


@pytest.mark.parametrize("value", ["123456", "oauth-grant-secret", "future_unknown_error", "sk-private-secret"])
def test_unknown_code_values_remain_private_even_when_they_look_like_error_codes(value):
    event = oplog.redact_operation_event({
        "message": f"code={value}", "error": json.dumps({"code": value}),
        "metadata": {"code": value, "nested": [{"code": value}]},
    })
    assert value not in json.dumps(event)
    assert event["metadata"]["code"] == "***"


@pytest.mark.parametrize("field", ["otp", "mfa_code", "verificationCode", "csrfToken", "challenge_token"])
def test_secret_fields_cannot_use_the_business_code_exception(field):
    event = oplog.redact_operation_event({
        "message": f"{field}=incorrect_code", "error": json.dumps({field: "incorrect_code"}),
        "metadata": {field: "incorrect_code"},
    })
    assert "incorrect_code" not in json.dumps(event)
    assert event["metadata"][field] == "***"


def test_oauth_callback_url_query_is_hidden_even_for_allowlisted_value():
    event = oplog.redact_operation_event({
        "error": "callback=https://example.test/callback?code=access_denied&state=state-fixture&access_token=token-fixture",
    })
    assert "example.test/callback" in event["error"]
    assert not any(secret in event["error"] for secret in ("access_denied", "state-fixture", "token-fixture"))


def _fresh_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_trace_writer_and_reader_respect_isolated_environment_directory(monkeypatch, tmp_path):
    from common import registration_trace

    isolated = tmp_path / "isolated-traces"
    monkeypatch.setenv("REGISTRATION_TRACE_DIR", str(isolated))
    writer = _fresh_module("incident_isolated_trace", Path(registration_trace.__file__))
    reader = _fresh_module("incident_isolated_trace_reader", Path(oplog.__file__))
    assert writer.TRACE_DIR == reader.TRACE_DIR == isolated

    trace = writer.RegistrationTrace(1, fixed_email="member@example.test", secrets=("synthetic-secret",))
    trace.event("failed", error="provider failed token=synthetic-secret code=123456")
    trace.finish("failed", persist_operation=False)
    rows, count = reader.list_registration_trace_events(trace.path)

    assert trace.path.parent == isolated and trace.path.is_file()
    assert count == 3
    assert "synthetic-secret" not in trace.path.read_text(encoding="utf-8")
    assert "123456" not in json.dumps(rows)
    outside = tmp_path / "outside.jsonl"
    outside.write_text('{"type":"event","error":"private outside trace"}\n', encoding="utf-8")
    assert reader.list_registration_trace_events(outside) == ([], 0)

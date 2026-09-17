from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

import mail
import register.protocol_ops as ops
import register.protocol_registrar as reg
from register.protocol_runtime import ProtocolRuntimeSettings, current_runtime_settings
from register.sessions import JobCancelled


EMAIL = "owned-pending@example.test"
PASSWORD = "owned-saved-password-fixture"
SECRET = "OWNEDSECRET234567"


def token(**overrides):
    payload = {"client_id": ops.chatgpt_oauth_client_id, "email": EMAIL, "exp": int(time.time()) + 3600,
               "https://api.openai.com/auth": {"chatgpt_account_id": "owned-id"}}
    payload.update(overrides)
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return f"{encode({'alg': 'none'})}.{encode(payload)}.fixture"


def response(payload, status=200):
    return SimpleNamespace(status_code=status, json=lambda: payload, text=json.dumps(payload))


@pytest.fixture
def recovery(monkeypatch):
    account = {"email": EMAIL, "password": PASSWORD, "registration_state": "security_pending"}
    mailbox = {"address": EMAIL, "provider": "smsbower_mail", "_registration_identity_created": True}
    tokens = {"access_token": token(), "session_token": "owned-session", "account_id": "owned-id"}
    registrar = Mock()
    registrar.login_pending_with_password.return_value = dict(tokens)
    registrar._chatgpt_mfa_request.return_value = (response({"mfa_enabled": False}), "")

    def enroll(tokens, index, *, credential_sink=None, **_kwargs):
        if credential_sink:
            credential_sink({"totp_secret": SECRET, "mfa_factor_id": "new-factor", "mfa_enabled": False})
            credential_sink({"mfa_enabled": True})
        return {"totp_secret": SECRET, "mfa_factor_id": "new-factor", "mfa_factor_type": "totp", "mfa_enabled": True}

    registrar._enroll_totp.side_effect = enroll
    factory = Mock(return_value=registrar)
    prepare = Mock()
    monkeypatch.setattr(ops, "PlatformRegistrar", factory)
    monkeypatch.setattr(ops, "_decorate_protocol_tokens", lambda registrar, tokens: dict(tokens))
    monkeypatch.setattr(mail, "prepare_smsbower_next_code", prepare, raising=False)
    monkeypatch.setattr(ops, "step", Mock())
    monkeypatch.setattr(ops, "create_mailbox", Mock(side_effect=AssertionError("no allocation")))
    monkeypatch.setattr(ops, "mark_mailbox_result", Mock(side_effect=AssertionError("caller owns lease")))
    monkeypatch.setattr(ops, "release_mailbox", Mock(side_effect=AssertionError("caller owns lease")))
    monkeypatch.setattr(requests.sessions.Session, "request", Mock(side_effect=AssertionError("network forbidden")))
    return SimpleNamespace(account=account, mailbox=mailbox, tokens=tokens, registrar=registrar, factory=factory, prepare=prepare)


def run(recovery, **overrides):
    options = dict(account=recovery.account, mail_config={}, mailbox_override=recovery.mailbox)
    options.update(overrides)
    return ops.recover_pending_registration_one(1, **options)


def test_recycled_passwordless_identity_is_skipped_as_already_registered(recovery):
    """恢复时密码路由不存在 => 回收邮箱身份按已注册跳过，不挂起失败。"""
    from register.pending_recovery import recover_registration
    from register.registration_outcome import RegisteredEmailDetected

    recovery.mailbox["_otp_accepted_by_server"] = True
    recovery.registrar.login_pending_with_password.side_effect = RuntimeError(
        "pending_recovery_password_route_unavailable"
    )
    with pytest.raises(RegisteredEmailDetected):
        recover_registration(
            1,
            account=recovery.account,
            mailbox=recovery.mailbox,
            mail_config={},
        )
    assert recovery.mailbox["_registration_outcome"] == "already_registered"


def test_historical_pending_without_password_set_restores_tokens_before_mfa(recovery):
    snapshots = []
    effects = Mock()
    effects.attach_mock(recovery.prepare, "next_code")
    effects.attach_mock(recovery.registrar.login_pending_with_password, "login")
    result = run(recovery, partial_sink=lambda fields: snapshots.append(dict(fields)))
    assert result["password"] == PASSWORD and result["password_set"] is True
    assert result["access_token"] == recovery.tokens["access_token"]
    assert result["registration_state"] == "complete" and result["mfa_enabled"] is True
    assert result["totp_secret"] == SECRET and result["totp_verified_at"]
    at_position = next(i for i, fields in enumerate(snapshots) if fields.get("access_token"))
    secret_position = next(i for i, fields in enumerate(snapshots) if fields.get("totp_secret"))
    assert at_position < secret_position
    assert all(fields.get("registration_state") != "complete" for fields in snapshots[:-1])
    assert effects.mock_calls[0][0] == "next_code" and effects.mock_calls[1][0] == "login"
    recovery.registrar.close.assert_called_once()


@pytest.mark.parametrize("change", ["candidate_only", "registered_marker", "different_mailbox", "missing_password"])
def test_unowned_or_registered_candidates_never_authenticate(recovery, change):
    if change == "candidate_only":
        recovery.account.pop("registration_state")
        recovery.mailbox.pop("_registration_identity_created")
    elif change == "registered_marker":
        recovery.mailbox["_registration_outcome"] = "already_registered"
    elif change == "different_mailbox":
        recovery.mailbox["address"] = "someone-else@example.test"
    else:
        recovery.account["password"] = ""
    with pytest.raises(RuntimeError, match="owned_identity"):
        run(recovery, owned_new_identity=True)
    recovery.factory.assert_not_called()
    recovery.prepare.assert_not_called()


def test_owned_current_identity_can_recover_before_pending_flag_is_persisted(recovery):
    recovery.account.pop("registration_state")
    assert not ops.can_recover_pending_registration(recovery.account, mailbox_override=recovery.mailbox)
    assert run(recovery, owned_new_identity=True)["registration_state"] == "complete"


def test_existing_totp_is_verified_without_rotation(recovery):
    recovery.account["totp_secret"] = SECRET
    recovery.registrar.login_pending_with_password.return_value = {
        **recovery.tokens, "_pending_totp_verified": True, "mfa_factor_id": "existing-factor",
    }
    recovery.registrar._chatgpt_mfa_request.return_value = (
        response({"mfa_enabled": True, "factors": {"totp": [{"id": "existing-factor"}]}}), "",
    )
    result = run(recovery)
    assert result["totp_secret"] == SECRET and result["mfa_factor_id"] == "existing-factor"
    recovery.registrar._enroll_totp.assert_not_called()
    recovery.registrar.login_with_password_totp.assert_not_called()


def test_unknown_active_factor_preserves_at_and_pending_without_disabling_it(recovery):
    recovery.registrar._chatgpt_mfa_request.return_value = (response({"mfa_enabled": True}), "")
    saved = dict(recovery.account)
    with pytest.raises(RuntimeError, match="remote_totp_secret_missing"):
        run(recovery, partial_sink=saved.update)
    assert saved["access_token"] == recovery.tokens["access_token"]
    assert saved["registration_state"] == "security_pending"
    recovery.registrar._enroll_totp.assert_not_called()
    assert all(call.args[0] == "get" for call in recovery.registrar._chatgpt_mfa_request.call_args_list)


def test_post_activation_failure_keeps_secret_and_at_without_blank_snapshot_erasure(recovery):
    def failed_enrollment(tokens, index, *, credential_sink):
        credential_sink({"totp_secret": SECRET, "mfa_enabled": True})
        credential_sink({"totp_secret": "", "access_token": "", "session_token": "", "mfa_enabled": False})
        raise RuntimeError(f"confirmation failed with key {SECRET}")

    recovery.registrar._enroll_totp.side_effect = failed_enrollment
    saved = dict(recovery.account)
    with pytest.raises(RuntimeError) as caught:
        run(recovery, partial_sink=saved.update)
    assert saved["registration_state"] == "security_pending"
    assert saved["access_token"] == recovery.tokens["access_token"]
    assert saved["totp_secret"] == SECRET and saved["mfa_enabled"] is True
    assert SECRET not in str(caught.value) and SECRET not in saved["last_security_error"]


@pytest.mark.parametrize("invalid", ["expired", "foreign_email", "foreign_account", "invalid_jwt"])
def test_invalid_or_foreign_tokens_are_not_saved(recovery, invalid):
    tokens = dict(recovery.tokens)
    if invalid == "expired":
        tokens["access_token"] = token(exp=1)
    elif invalid == "foreign_email":
        tokens["access_token"] = token(email="not-owned@example.test")
    elif invalid == "foreign_account":
        tokens["account_id"] = "another-id"
    else:
        tokens["access_token"] = "not-a-jwt"
    recovery.registrar.login_pending_with_password.return_value = tokens
    saved = dict(recovery.account)
    with pytest.raises(RuntimeError):
        run(recovery, partial_sink=saved.update)
    assert not saved.get("access_token") and not saved.get("session_token")
    recovery.registrar._enroll_totp.assert_not_called()


def test_transient_login_retry_preserves_password_and_never_registers(recovery, monkeypatch):
    recovery.registrar.login_pending_with_password.side_effect = [RuntimeError("curl: (35) connection reset"), dict(recovery.tokens)]
    monkeypatch.setattr(ops.time, "sleep", lambda seconds: None)
    assert run(recovery)["registration_state"] == "complete"
    assert recovery.registrar.login_pending_with_password.call_count == 2
    assert recovery.registrar._enroll_totp.call_count == 1
    assert recovery.registrar.close.call_count == 2
    recovery.registrar.register.assert_not_called()
    recovery.registrar.change_password.assert_not_called()


@pytest.mark.parametrize("suffix", ["invalid_state", "http_503", "invalid_state; curl: (35) connection reset"])
def test_missing_identity_overrides_session_and_transport_retries(recovery, monkeypatch, suffix):
    recovery.account.update(totp_secret=SECRET, mfa_enabled=True, refresh_token="saved-refresh-fixture")
    original = dict(recovery.account)
    saved = dict(original)
    error = RuntimeError(f"username_not_found: {suffix}; password={PASSWORD}")
    recovery.registrar.login_pending_with_password.side_effect = error
    sleep = Mock()
    monkeypatch.setattr(ops.time, "sleep", sleep)

    with pytest.raises(RuntimeError, match="username_not_found") as caught:
        run(recovery, proxy="http://fixture-proxy.example.test:8080", max_attempts=2, partial_sink=saved.update)

    recovery.factory.assert_called_once_with("http://fixture-proxy.example.test:8080")
    recovery.registrar.login_pending_with_password.assert_called_once_with(
        EMAIL, PASSWORD, 1, mailbox=recovery.mailbox, totp_secret=SECRET,
    )
    recovery.registrar.close.assert_called_once()
    recovery.prepare.assert_called_once_with(recovery.mailbox)
    sleep.assert_not_called()
    assert caught.value.__cause__ is error
    assert recovery.account == original
    assert saved["registration_state"] == "security_pending"
    assert "username_not_found" in saved["last_security_error"]
    assert PASSWORD not in str(caught.value) and PASSWORD not in saved["last_security_error"]
    for field in ("password", "totp_secret", "mfa_enabled", "refresh_token"):
        assert saved[field] == original[field]
    for name in ("register", "change_password", "_send_password_reset_otp", "_enroll_totp", "_chatgpt_mfa_request"):
        getattr(recovery.registrar, name).assert_not_called()


def test_stop_before_recovery_performs_no_authentication(recovery):
    with pytest.raises(JobCancelled):
        run(recovery, stop_check=lambda: True)
    recovery.factory.assert_not_called()
    recovery.prepare.assert_not_called()


def test_recovery_forces_session_and_mfa_verification_and_restores_runtime(recovery):
    before = current_runtime_settings()
    original_login = recovery.registrar.login_pending_with_password.return_value
    def login(*args, **kwargs):
        runtime = current_runtime_settings()
        assert runtime.verify_chatgpt_me and runtime.enable_totp_after_register
        return original_login
    recovery.registrar.login_pending_with_password.side_effect = login
    run(recovery, protocol_runtime=ProtocolRuntimeSettings(verify_chatgpt_me=False, enable_totp_after_register=False))
    assert current_runtime_settings() is before


def test_sync_identity_removes_overlapping_did_cookies_and_preserves_other_cookies(monkeypatch):
    registrar = reg.PlatformRegistrar.__new__(reg.PlatformRegistrar)
    registrar.fingerprint = {}
    registrar.device_id = "current-device-fixture"
    registrar.session = requests.Session()
    registrar.session.headers["oai-device-id"] = "stale-device"
    for domain in (".openai.com", ".auth.openai.com", "auth.openai.com", ".chatgpt.com"):
        registrar.session.cookies.set("oai-did", f"stale-{domain}", domain=domain)
    registrar.session.cookies.set("cf_clearance", "preserved-clearance", domain=".openai.com")
    monkeypatch.setattr(reg, "_browser_fingerprint", lambda fp: {"impersonate": "fixture", "user_agent": "fixture-ua"})
    monkeypatch.setattr(reg, "_client_hint_headers", lambda fp: {})
    registrar._sync_session_identity()
    assert registrar.session.headers["oai-device-id"] == registrar.device_id
    cookies = [cookie for cookie in registrar.session.cookies if cookie.name == "oai-did"]
    assert {(cookie.domain, cookie.value) for cookie in cookies} == {
        ("auth.openai.com", registrar.device_id), ("chatgpt.com", registrar.device_id),
    }
    assert registrar.session.cookies.get("cf_clearance", domain=".openai.com") == "preserved-clearance"


def test_real_enrollment_persists_secret_before_activation_and_retains_it_on_confirm_failure(monkeypatch):
    registrar = reg.PlatformRegistrar.__new__(reg.PlatformRegistrar)
    saved = {}

    def request(method, path, access, index, payload=None):
        if path.endswith("mfa_info"):
            if method == "get" and saved.get("mfa_enabled"):
                return response({}, status=503), "verification connection lost"
            return response({"mfa_enabled": False}), ""
        if path.endswith("security_settings/info"):
            return response({"aas_eligible": True}), ""
        if path.endswith("/mfa/enroll"):
            return response({"secret": SECRET, "session_id": "enrollment-session", "factor": {"id": "new-factor"}}), ""
        assert path.endswith("activate_enrollment")
        assert saved["totp_secret"] == SECRET
        assert saved["mfa_enabled"] is False
        return response({"success": True}), ""

    registrar._chatgpt_mfa_request = request
    monkeypatch.setattr(reg, "_totp_code", lambda secret: "123456")
    monkeypatch.setattr(reg, "step", Mock())
    with pytest.raises(RuntimeError, match="verification connection lost"):
        registrar._enroll_totp({"access_token": "fixture-at"}, 1, credential_sink=saved.update)
    assert saved["totp_secret"] == SECRET and saved["mfa_enabled"] is True
    assert saved["totp_setup_pending"] is False


def registration_fixture(monkeypatch, password_result, otp_result):
    registrar = reg.PlatformRegistrar.__new__(reg.PlatformRegistrar)
    registrar.proxy = ""
    registrar._log_identity = Mock()
    registrar._diagnostic = Mock()
    registrar._complete_chatgpt_session = Mock(return_value={"final_url": "https://auth.openai.com/create-account/password"})
    registrar._check_fresh_registration = Mock()
    registrar._set_registration_password = Mock(return_value=password_result)
    registrar._navigate_auth_step = Mock()
    registrar._validate_otp = Mock(return_value=otp_result)
    registrar._read_chatgpt_session = Mock(return_value={"access_token": "saved-at", "session_token": "saved-session"})
    registrar._build_registration_record = lambda email, password, tokens, mfa, source, mailbox, **kw: {
        "email": email, "password": password, "password_set": bool(password), **tokens, **mfa, **kw,
    }
    mailbox = {"address": EMAIL, "provider": "smsbower_mail"}
    monkeypatch.setattr(reg, "_random_password", lambda: PASSWORD)
    monkeypatch.setattr(reg, "step", Mock())
    monkeypatch.setattr(reg, "mark_mailbox_code_boundary", Mock())
    monkeypatch.setattr(reg, "wait_for_code", Mock(return_value="123456"))
    monkeypatch.setattr(reg, "mark_smsbower_otp_accepted", lambda box: box.update(_otp_accepted_by_server=True))
    monkeypatch.setattr(reg, "mark_mailbox_result", Mock())
    return registrar, mailbox


def test_signup_password_is_saved_before_navigation_can_fail_and_mailbox_is_exposed(monkeypatch):
    registrar, mailbox = registration_fixture(monkeypatch,
        {"continue_url": "https://auth.openai.com/api/accounts/email-otp/send"}, {})
    registrar._navigate_auth_step.side_effect = RuntimeError("navigation failed")
    snapshots = []
    with pytest.raises(RuntimeError, match="navigation failed"):
        registrar.register(1, mailbox_override=mailbox, partial_sink=snapshots.append)
    assert registrar.registration_mailbox is mailbox
    assert snapshots[0]["password"] == PASSWORD and snapshots[0]["password_set"] is True
    assert mailbox["_registration_identity_created"] is True


def test_owned_identity_password_route_after_otp_requests_login_recovery_not_registered_skip(monkeypatch):
    registrar, mailbox = registration_fixture(monkeypatch, {},
        {"continue_url": "https://auth.openai.com/log-in/password", "page": {"type": "password_verification"}})
    with pytest.raises(RuntimeError, match="pending_recovery_password_login_required_after_otp") as caught:
        registrar.register(1, mailbox_override=mailbox, partial_sink=Mock())
    assert not isinstance(caught.value, reg.PasswordResetRequired)
    assert mailbox.get("_registration_outcome") != "already_registered"


def test_normal_signup_mfa_progress_is_in_pending_snapshots_on_late_failure(monkeypatch):
    registrar, mailbox = registration_fixture(monkeypatch, {},
        {"continue_url": "https://chatgpt.com/api/auth/callback/openai?code=fixture"})
    snapshots = []
    def enroll(tokens, index, *, credential_sink, **_kwargs):
        credential_sink({"totp_secret": SECRET, "mfa_enabled": True, "mfa_factor_id": "new-factor"})
        raise RuntimeError("late mfa confirmation failed")
    registrar._enroll_totp = enroll
    with pytest.raises(RuntimeError, match="late mfa confirmation failed"):
        registrar.register(1, mailbox_override=mailbox, partial_sink=snapshots.append)
    assert snapshots[-1]["totp_secret"] == SECRET
    assert snapshots[-1]["mfa_enabled"] is True
    assert snapshots[-1]["access_token"] == "saved-at"
    assert snapshots[-1]["registration_state"] == "security_pending"

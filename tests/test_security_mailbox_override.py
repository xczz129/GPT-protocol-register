from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import register.protocol_ops as operations


@pytest.fixture
def security_operation(monkeypatch):
    mailbox = {
        "address": "new+split@gmail.com",
        "provider": "smsbower_mail",
        "mail_id": "shared-fixture-activation",
        "_smsbower_pair_role": "child",
        "_registration_identity_created": True,
    }
    tokens = {
        "access_token": "fixture-access",
        "session_token": "fixture-session",
        "account_id": "fixture-account",
    }
    registrar = MagicMock()
    registrar.login.return_value = dict(tokens)
    registrar._read_chatgpt_session.return_value = dict(tokens)
    registrar._enroll_totp.return_value = {
        "totp_secret": "fixture-totp",
        "mfa_enabled": True,
    }
    effects = SimpleNamespace(
        create=MagicMock(return_value=mailbox),
        factory=MagicMock(return_value=registrar),
        finalize=MagicMock(),
        release=MagicMock(),
        boundary=MagicMock(),
    )
    monkeypatch.setattr(operations, "PlatformRegistrar", effects.factory)
    monkeypatch.setattr(operations, "create_mailbox", effects.create)
    monkeypatch.setattr(operations, "mark_mailbox_result", effects.finalize)
    monkeypatch.setattr(operations, "release_mailbox", effects.release)
    monkeypatch.setattr(operations, "mark_mailbox_code_boundary", effects.boundary)
    monkeypatch.setattr(operations, "step", MagicMock())
    return registrar, mailbox, effects


@pytest.mark.parametrize("use_override", [True, False])
def test_browser_security_uses_the_provided_activation_or_default_mailbox(security_operation, use_override):
    registrar, mailbox, effects = security_operation
    kwargs = {"mailbox_override": mailbox} if use_override else {}
    result = operations.repair_security_one(
        1,
        account={
            "email": mailbox["address"], "registration_method": "browser",
            "password": "known-new-password", "password_set": True,
        },
        mail_config={},
        **kwargs,
    )
    if use_override:
        effects.create.assert_not_called()
    else:
        effects.create.assert_called_once_with(mailbox["address"], register_proxy="", allow_used=True)
    assert registrar.login.call_args.args[1] is mailbox
    assert mailbox["mail_id"] == "shared-fixture-activation"
    assert result["totp_secret"] == "fixture-totp"
    assert result["access_token"] == "fixture-access"
    registrar._check_fresh_registration.assert_not_called()
    registrar._complete_post_registration_password.assert_not_called()
    effects.finalize.assert_called_once_with(mailbox, success=True)


@pytest.mark.parametrize("override", [
    {},
    {"address": "new@gmail.com"},
    {"address": "new+other@gmail.com"},
    {"address": "unrelated@icloud.com"},
])
def test_mismatched_override_is_rejected_before_any_account_or_lease_action(security_operation, override):
    registrar, _mailbox, effects = security_operation
    with pytest.raises(RuntimeError, match="预分配邮箱与目标账号不一致"):
        operations.repair_security_one(
            1,
            account={
                "email": "new+split@gmail.com", "password": "already-complete",
                "password_set": True, "totp_secret": "already-complete-totp",
            },
            mail_config={}, mailbox_override=override,
        )
    effects.factory.assert_not_called()
    effects.create.assert_not_called()
    effects.finalize.assert_not_called()
    effects.release.assert_not_called()
    registrar.login.assert_not_called()


def test_override_email_comparison_preserves_alias_and_ignores_case(security_operation):
    _registrar, mailbox, effects = security_operation
    mailbox["address"] = "  NEW+SPLIT@GMAIL.COM  "
    result = operations.repair_security_one(
        1,
        account={
            "email": "new+split@gmail.com", "password": "already-complete",
            "password_set": True, "totp_secret": "already-complete-totp",
        },
        mail_config={}, mailbox_override=mailbox,
    )
    assert result["password"] == "already-complete"
    effects.factory.assert_not_called()
    effects.create.assert_not_called()


def test_expired_session_recovery_reuses_activation_instead_of_buying_again(security_operation):
    registrar, mailbox, effects = security_operation
    registrar._read_chatgpt_session.side_effect = RuntimeError("session expired")
    result = operations.repair_security_one(
        1,
        account={
            "email": mailbox["address"], "password": "known-new-password",
            "password_set": True, "totp_secret": "", "mfa_enabled": False,
        },
        mail_config={}, mailbox_override=mailbox,
    )
    effects.create.assert_not_called()
    effects.boundary.assert_called_once_with(mailbox, grace_seconds=0)
    assert registrar.login.call_args.args[1] is mailbox
    assert result["access_token"] == "fixture-access"
    assert result["totp_secret"] == "fixture-totp"
    registrar._check_fresh_registration.assert_not_called()


def test_missing_password_completion_keeps_the_same_mailbox(security_operation):
    registrar, mailbox, effects = security_operation

    def complete(_email, _password, leased_mailbox, _index, **kwargs):
        assert leased_mailbox is mailbox
        kwargs["credential_sink"](dict(registrar.login.return_value))
        return "confirmed-new-password"

    registrar._complete_post_registration_password.side_effect = complete
    result = operations.repair_security_one(
        1,
        account={"email": mailbox["address"], "totp_secret": "existing-totp"},
        mail_config={}, mailbox_override=mailbox,
    )
    effects.create.assert_not_called()
    assert result["password"] == "confirmed-new-password"
    registrar._complete_post_registration_password.assert_called_once()
    registrar._check_fresh_registration.assert_not_called()

from __future__ import annotations

"""Login OTP wait: two 90s rounds, one 45s segment each resend."""
from unittest.mock import Mock

import pytest

import register.protocol_registrar as module
from register.protocol_registrar import PlatformRegistrar


def _registrar():
    registrar = PlatformRegistrar.__new__(PlatformRegistrar)
    registrar.proxy = ""
    registrar.device_id = "fixture-device"
    registrar.fingerprint = {"impersonate": "chrome131", "user_agent": "ua"}
    return registrar


def test_first_segment_hit_skips_resend(monkeypatch):
    registrar = _registrar()
    steps = []
    monkeypatch.setattr(module, "step", lambda *a, **k: steps.append(str(a[1])))
    monkeypatch.setattr(
        module, "wait_for_code", lambda *a, **k: "654321"
    )
    resend = Mock()
    monkeypatch.setattr(module.PlatformRegistrar, "_send_passwordless_otp", resend)
    monkeypatch.setattr(module, "mark_mailbox_code_boundary", Mock())

    code = registrar._wait_login_otp_rounds({"address": "a@gmail.com"}, 1)

    assert code == "654321"
    resend.assert_not_called()
    assert steps[0].startswith("开始等待 passwordless 登录验证码（1/2")


def test_segments_use_45s_pacing(monkeypatch):
    registrar = _registrar()
    waits = []

    def wait(mailbox, register_proxy="", wait_timeout=None):
        waits.append(wait_timeout)
        return None

    monkeypatch.setattr(module, "step", Mock())
    monkeypatch.setattr(module, "wait_for_code", wait)
    monkeypatch.setattr(module, "mark_mailbox_code_boundary", Mock())
    monkeypatch.setattr(
        module.PlatformRegistrar, "_send_passwordless_otp", Mock()
    )

    registrar._wait_login_otp_rounds({"address": "a@gmail.com"}, 1)

    assert waits == [45.0, 45.0, 45.0, 45.0]


def test_code_in_third_segment_resends_twice(monkeypatch):
    registrar = _registrar()
    calls = {"reads": 0}

    def wait(mailbox, register_proxy="", wait_timeout=None):
        calls["reads"] += 1
        return None if calls["reads"] < 3 else "987654"

    monkeypatch.setattr(module, "step", Mock())
    monkeypatch.setattr(module, "wait_for_code", wait)
    boundaries = Mock()
    monkeypatch.setattr(module, "mark_mailbox_code_boundary", boundaries)
    resend = Mock()
    monkeypatch.setattr(module.PlatformRegistrar, "_send_passwordless_otp", resend)

    code = registrar._wait_login_otp_rounds({"address": "a@gmail.com"}, 1)

    assert code == "987654"
    assert calls["reads"] == 3  # code arrives in the 90-135s segment
    assert resend.call_count == 2  # resends at 45s and 90s
    assert boundaries.call_count == 2


def test_all_expired_resends_three_times(monkeypatch):
    registrar = _registrar()
    monkeypatch.setattr(module, "step", Mock())
    monkeypatch.setattr(module, "wait_for_code", lambda *a, **k: None)
    boundaries = Mock()
    monkeypatch.setattr(module, "mark_mailbox_code_boundary", boundaries)
    resend = Mock()
    monkeypatch.setattr(module.PlatformRegistrar, "_send_passwordless_otp", resend)

    code = registrar._wait_login_otp_rounds({"address": "a@gmail.com"}, 1)

    assert code is None  # both rounds exhausted
    assert resend.call_count == 3  # resends at 45s / 90s / 135s
    assert boundaries.call_count == 3


def test_resend_409_invalid_state_is_tolerated(monkeypatch):
    registrar = _registrar()
    monkeypatch.setattr(module, "step", Mock())
    monkeypatch.setattr(module, "wait_for_code", lambda *a, **k: None)

    def resend(self, index):
        raise RuntimeError("passwordless_send_otp_http_409, detail=invalid_state")

    monkeypatch.setattr(module.PlatformRegistrar, "_send_passwordless_otp", resend)
    boundaries = Mock()
    monkeypatch.setattr(module, "mark_mailbox_code_boundary", boundaries)

    code = registrar._wait_login_otp_rounds({"address": "a@gmail.com"}, 1)

    assert code is None  # both rounds exhausted
    assert boundaries.call_count == 3


def test_resend_hard_failure_propagates(monkeypatch):
    registrar = _registrar()
    monkeypatch.setattr(module, "step", Mock())
    monkeypatch.setattr(module, "wait_for_code", lambda *a, **k: None)

    def resend(self, index):
        raise RuntimeError("passwordless_send_otp_http_500")

    monkeypatch.setattr(module.PlatformRegistrar, "_send_passwordless_otp", resend)

    with pytest.raises(RuntimeError, match="http_500"):
        registrar._wait_login_otp_rounds({"address": "a@gmail.com"}, 1)


def test_passwordless_login_uses_rounds_and_reports_total_timeout(monkeypatch):
    registrar = _registrar()
    monkeypatch.setattr(module, "step", Mock())
    monkeypatch.setattr(module, "mark_mailbox_code_boundary", Mock())
    authorize = Mock()
    monkeypatch.setattr(module.PlatformRegistrar, "_authorize_continue_login", authorize)
    monkeypatch.setattr(module.PlatformRegistrar, "_send_passwordless_otp", Mock())
    rounds = Mock(return_value=None)
    monkeypatch.setattr(module.PlatformRegistrar, "_wait_login_otp_rounds", rounds)

    with pytest.raises(RuntimeError, match="两轮共180秒"):
        registrar._passwordless_login("a@gmail.com", {"provider": "smsbower_mail"}, 1)

    rounds.assert_called_once()

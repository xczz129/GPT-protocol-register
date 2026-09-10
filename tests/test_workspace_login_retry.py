"""Offline regressions for fresh-session retries before Team selection."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from register.protocol_runtime import ProtocolRuntimeSettings, current_runtime_settings
from register.sessions import JobCancelled


EMPTY_WORKSPACES = "workspace_page_without_workspaces: synthetic empty auth session"


@pytest.fixture
def login_harness(monkeypatch):
    import register.protocol_context as context
    import register.protocol_ops as module
    import register.protocol_runtime as runtime

    clock = SimpleNamespace(now=100.0, sleeps=[], on_sleep=None)

    def sleep(seconds):
        assert 0 < seconds <= 0.1
        clock.sleeps.append(seconds)
        clock.now += seconds
        if clock.on_sleep:
            clock.on_sleep()

    fake_time = SimpleNamespace(
        time=lambda: clock.now,
        monotonic=lambda: clock.now,
        sleep=sleep,
    )
    monkeypatch.setattr(module, "time", fake_time)
    monkeypatch.setattr(runtime, "time", fake_time)
    monkeypatch.setattr(module, "log", Mock())
    monkeypatch.setattr(context, "log", Mock())
    monkeypatch.setattr(module, "_browser_fingerprint", lambda value: dict(value))
    harness = SimpleNamespace(
        module=module, clock=clock, outcomes=[], registrars=[], events=[],
        account_events=[], errors=[], cancelled=False,
    )

    class Registrar:
        def __init__(self, proxy):
            assert all(item.close.call_count == 1 for item in harness.registrars)
            self.attempt = len(harness.registrars) + 1
            self.proxy = proxy
            self.runtime = current_runtime_settings()
            self.proxy_config = dict(context._PROXY_CONFIG.get())
            self.session = object()
            self.device_id = f"device-{self.attempt}"
            self.fingerprint = {
                "impersonate": f"browser-{self.attempt}",
                "user_agent": f"agent-{self.attempt}",
                "sec_ch_ua": "",
                "sec_ch_ua_full_version_list": "",
                "platform_version": "10.0.0",
                "accept_language": "en-US,en;q=0.9",
                "profile_id": f"profile-{self.attempt}",
            }
            self._selected_workspace_id = ""
            self.close = Mock()
            self.calls = []
            harness.registrars.append(self)

        def login_with_password_totp(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            outcome = harness.outcomes.pop(0)
            if callable(outcome):
                outcome = outcome(self)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    monkeypatch.setattr(module, "PlatformRegistrar", Registrar)

    def run(**overrides):
        options = dict(
            email="retry@example.test",
            password="synthetic-password",
            totp_secret="synthetic-totp",
            proxy="socks5://127.0.0.1:19000",
            workspace_name="Synthetic Team",
            require_team_workspace=True,
            stop_check=lambda: harness.cancelled,
            task_event=lambda index, status=None, **fields: harness.events.append(
                (index, status, fields)
            ),
            account_log_sink=harness.account_events.append,
            error_sink=harness.errors.append,
            proxy_config={"marker": "same-config"},
        )
        options.update(overrides)
        return module.login_with_password_totp_one(7, **options)

    harness.run = run
    return harness


@pytest.mark.parametrize("failures", [1, 2])
def test_empty_team_session_retries_with_fresh_registrar_and_keeps_success_metadata(
    login_harness, failures,
):
    harness = login_harness
    harness.outcomes = [RuntimeError(EMPTY_WORKSPACES) for _ in range(failures)] + [
        {"access_token": "synthetic-fresh-token", "account_id": "team-id"},
    ]
    settings = ProtocolRuntimeSettings().with_deadline(200.0)
    previous_runtime = current_runtime_settings()
    result = harness.run(protocol_runtime=settings)

    assert len(harness.registrars) == failures + 1
    assert len({id(item.session) for item in harness.registrars}) == failures + 1
    assert all(item.close.call_count == 1 for item in harness.registrars)
    assert all(item.runtime is settings for item in harness.registrars)
    assert all(item.proxy_config == {"marker": "same-config"} for item in harness.registrars)
    assert len({item.proxy for item in harness.registrars}) == 1
    assert all(item.calls == harness.registrars[0].calls for item in harness.registrars)
    assert current_runtime_settings() is previous_runtime
    assert result["access_token"] == "synthetic-fresh-token"
    assert result["device_id"] == f"device-{failures + 1}"
    assert result["user_agent"] == f"agent-{failures + 1}"
    assert result["protocol_profile_id"] == f"profile-{failures + 1}"
    assert sum(harness.clock.sleeps) == pytest.approx(sum(range(1, failures + 1)))
    assert harness.errors == []
    assert [status for _, status, _ in harness.events] == ["running"] * (failures + 1) + ["success"]
    assert all(item["status"] != "failed" and not item.get("error") for item in harness.account_events)
    retry_steps = [fields["step"] for _, _, fields in harness.events if "workspace_page_without_workspaces" in fields["step"]]
    assert len(retry_steps) == failures
    assert f"{failures + 1}/3" in retry_steps[-1]
    serialized = json.dumps(harness.account_events)
    assert "synthetic-password" not in serialized
    assert "synthetic-totp" not in serialized


def test_empty_team_session_exhausts_exactly_three_attempts(login_harness):
    harness = login_harness
    harness.outcomes = [RuntimeError(EMPTY_WORKSPACES) for _ in range(3)] + [{}]
    assert harness.run() is None
    assert len(harness.registrars) == 3
    assert len(harness.outcomes) == 1
    assert all(item.close.call_count == 1 for item in harness.registrars)
    assert harness.errors == [EMPTY_WORKSPACES]
    assert [status for _, status, _ in harness.events].count("failed") == 1
    assert harness.events[-1][1] == "failed"


@pytest.mark.parametrize("error", [
    "team_workspace_not_found: no team",
    "team_workspace_name_not_found: no matching name",
    "team_workspace_ambiguous: choose a team",
    "team_workspace_identity_mismatch: wrong account",
    "workspace_select_missing_continue: missing callback",
    "password_verify_failed: wrong password",
    "mfa_verify_failed: incorrect OTP",
    "workspace_page_without_workspaces_extra: unrelated failure",
])
def test_other_authentication_and_selection_errors_never_retry(login_harness, error):
    harness = login_harness
    harness.outcomes = [RuntimeError(error)]
    assert harness.run() is None
    assert len(harness.registrars) == 1
    assert harness.registrars[0].close.call_count == 1
    assert harness.errors == [error]
    assert harness.clock.sleeps == []


def test_personal_login_never_retries_empty_workspaces(login_harness):
    harness = login_harness
    harness.outcomes = [RuntimeError(EMPTY_WORKSPACES)]
    assert harness.run(require_team_workspace=False) is None
    assert len(harness.registrars) == 1
    assert harness.registrars[0].close.call_count == 1
    assert harness.clock.sleeps == []


def test_post_selection_failure_never_starts_a_new_session(login_harness):
    harness = login_harness

    def selected_before_error(registrar):
        registrar._selected_workspace_id = "selected-team"
        return RuntimeError(EMPTY_WORKSPACES)

    harness.outcomes = [selected_before_error]
    assert harness.run() is None
    assert len(harness.registrars) == 1
    assert harness.registrars[0].close.call_count == 1
    assert harness.clock.sleeps == []


def test_non_runtime_exception_with_matching_text_never_retries(login_harness):
    harness = login_harness
    harness.outcomes = [ValueError(EMPTY_WORKSPACES)]
    assert harness.run() is None
    assert len(harness.registrars) == 1
    assert harness.registrars[0].close.call_count == 1


@pytest.mark.parametrize("phase", ["before_start", "during_login", "during_backoff"])
def test_cancellation_closes_active_session_and_skips_retry(login_harness, phase):
    harness = login_harness
    if phase == "before_start":
        harness.cancelled = True
    elif phase == "during_login":
        harness.outcomes = [JobCancelled("stop requested")]
    else:
        harness.outcomes = [RuntimeError(EMPTY_WORKSPACES)]
        harness.clock.on_sleep = lambda: setattr(harness, "cancelled", True)
    previous_runtime = current_runtime_settings()
    with pytest.raises(JobCancelled):
        harness.run()
    assert len(harness.registrars) == (0 if phase == "before_start" else 1)
    assert all(item.close.call_count == 1 for item in harness.registrars)
    assert harness.errors == []
    assert harness.events[-1][1] == "cancelled"
    assert current_runtime_settings() is previous_runtime
    assert sum(harness.clock.sleeps) <= 0.1


@pytest.mark.parametrize("deadline", [99.0, 100.25])
def test_original_deadline_prevents_start_or_retry_without_new_budget(login_harness, deadline):
    harness = login_harness
    harness.outcomes = [RuntimeError(EMPTY_WORKSPACES)]
    settings = ProtocolRuntimeSettings().with_deadline(deadline)
    assert harness.run(protocol_runtime=settings) is None
    assert len(harness.registrars) == (0 if deadline < 100 else 1)
    assert all(item.close.call_count == 1 for item in harness.registrars)
    assert harness.errors == ["protocol_account_timeout"]
    assert harness.events[-1][1] == "failed"
    assert harness.clock.now <= max(100, deadline)


def test_deadline_expiring_in_login_skips_backoff_and_retry(login_harness):
    harness = login_harness

    def exhaust_budget(_registrar):
        harness.clock.now = 102.0
        return RuntimeError(EMPTY_WORKSPACES)

    harness.outcomes = [exhaust_budget]
    assert harness.run(protocol_runtime=ProtocolRuntimeSettings().with_deadline(101.0)) is None
    assert len(harness.registrars) == 1
    assert harness.registrars[0].close.call_count == 1
    assert harness.errors == ["protocol_account_timeout"]
    assert harness.clock.sleeps == []

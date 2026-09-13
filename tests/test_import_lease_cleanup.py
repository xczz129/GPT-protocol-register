"""Lease ownership survives every phone-step exit, without real SMS calls."""

import pytest

from test_sms_unlimited_recovery import _import_phone_client, _import_phone_fixture


def test_non_sms_provider_exception_releases_the_newly_acquired_lease(monkeypatch):
    _imports, manager, job, provider, activation = _import_phone_fixture(monkeypatch)
    provider.wait_code.side_effect = RuntimeError("fixture provider adapter failure")
    with pytest.raises(RuntimeError, match="adapter failure"):
        manager._phone_step(job, _import_phone_client())
    provider.cancel.assert_called_once_with(activation)
    assert job._sms_activation is None
    manager._release_retained_activation(job)
    provider.cancel.assert_called_once()


def test_step_callback_exception_after_acquisition_releases_before_send(monkeypatch):
    _imports, manager, job, provider, activation = _import_phone_fixture(monkeypatch)
    manager._set_step.side_effect = [None, RuntimeError("fixture task persistence failure")]
    client = _import_phone_client()
    with pytest.raises(RuntimeError, match="persistence failure"):
        manager._phone_step(job, client)
    client.send_phone.assert_not_called()
    provider.cancel.assert_called_once_with(activation)
    assert job._sms_activation is None


def test_explicit_unsent_session_recovery_is_the_only_retained_exit(monkeypatch):
    imports, manager, job, provider, activation = _import_phone_fixture(monkeypatch)
    with pytest.raises(imports.AuthSessionRestartRequired):
        manager._phone_step(job, _import_phone_client(RuntimeError("sign-in session is no longer valid")))
    provider.cancel.assert_not_called()
    assert job._sms_activation is activation
    assert job._sms_keep_for_session_retry is True
    assert job._sms_sent is False
    provider.is_active.side_effect = RuntimeError("fixture lease status unavailable")
    with pytest.raises(RuntimeError, match="lease status unavailable"):
        manager._phone_step(job, _import_phone_client())
    provider.acquire.assert_called_once()
    provider.cancel.assert_called_once_with(activation)
    assert job._sms_activation is None


def test_cancel_racing_with_number_acquisition_releases_exactly_once(monkeypatch):
    imports, manager, job, provider, activation = _import_phone_fixture(monkeypatch)

    def acquire():
        job.cancel_event.set()
        return activation

    provider.acquire.side_effect = acquire
    with pytest.raises(imports.JobCancelled):
        manager._phone_step(job, _import_phone_client())
    manager._release_retained_activation(job)
    provider.cancel.assert_called_once_with(activation)
    assert job._sms_activation is None


def test_non_sms_cancellation_during_wait_also_releases_once(monkeypatch):
    imports, manager, job, provider, activation = _import_phone_fixture(monkeypatch)
    provider.wait_code.side_effect = imports.JobCancelled("fixture cancelled")
    with pytest.raises(imports.JobCancelled):
        manager._phone_step(job, _import_phone_client())
    manager._release_retained_activation(job)
    provider.cancel.assert_called_once_with(activation)
    assert job._sms_activation is None


def test_cancel_adapter_exception_preserves_original_error_and_drops_ownership(monkeypatch):
    _imports, manager, job, provider, activation = _import_phone_fixture(monkeypatch)
    provider.wait_code.side_effect = RuntimeError("fixture original wait failure")
    provider.cancel.side_effect = RuntimeError("fixture cancellation acknowledgement failed")
    with pytest.raises(RuntimeError, match="original wait failure"):
        manager._phone_step(job, _import_phone_client())
    manager._release_retained_activation(job)
    provider.cancel.assert_called_once_with(activation)
    assert job._sms_activation is None


def test_failure_after_successful_send_never_retains_the_sent_activation(monkeypatch):
    _imports, manager, job, provider, activation = _import_phone_fixture(monkeypatch)
    provider.wait_code.side_effect = RuntimeError("sign-in session is no longer valid")
    with pytest.raises(RuntimeError, match="sign-in session"):
        manager._phone_step(job, _import_phone_client())
    provider.cancel.assert_called_once_with(activation)
    assert job._sms_activation is None
    assert job._sms_keep_for_session_retry is False

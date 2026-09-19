"""Personal imports must reject the wrong workspace before any database write."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from test_import_identity_config import boundary, tokens


@pytest.mark.parametrize("plan", ["free", "plus", "pro"])
def test_personal_import_accepts_real_personal_session(boundary, plan):
    candidate = tokens(team=False)
    # Session-only plan changes must agree with the actual token, so generate
    # the signed-claim-shaped fixture for each real personal subscription.
    import base64, json
    body = json.loads(base64.urlsafe_b64decode(candidate["access_token"].split('.')[1] + '=='))
    body["https://api.openai.com/auth"]["chatgpt_plan_type"] = plan
    payload = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip('=')
    candidate["access_token"] = 'synthetic.' + payload + '.signature'
    candidate["session"]["account"]["planType"] = candidate["plan_type"] = plan
    saved = boundary.route._persist_imported_account(
        {"email": "alice@example.test", "import_account_type": "personal"}, candidate, Mock(),
    )
    assert saved["plan_type"] == plan
    boundary.persist.assert_called_once()


def test_team_direct_callback_never_reaches_health_or_database_in_personal_mode(boundary):
    with pytest.raises(RuntimeError, match="personal_workspace_"):
        boundary.route._persist_imported_account(
            {"email": "alice@example.test", "import_account_type": "personal"}, tokens(team=True), Mock(),
        )
    boundary.check.assert_not_called()
    boundary.persist.assert_not_called()
    assert not boundary.state.ACCOUNTS_FILE.exists()


@pytest.mark.parametrize("change", ["team", "workspace", "user", "team-label"])
def test_post_health_changes_cannot_overwrite_an_existing_personal_account(boundary, change):
    previous = {"email": "alice@example.test", "import_account_type": "personal", "refresh_token": "prior-synthetic"}
    before = deepcopy(previous)
    initial = tokens(team=False)
    initial.pop("selected_workspace_id")  # A direct callback must also be pinned.
    checked = tokens(team=change == "team", user="other-user" if change == "user" else "user-a")
    if change == "workspace":
        import base64, json
        body = json.loads(base64.urlsafe_b64decode(checked["access_token"].split('.')[1] + '=='))
        body["https://api.openai.com/auth"]["chatgpt_account_id"] = "other-personal"
        payload = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip('=')
        checked["access_token"] = 'synthetic.' + payload + '.signature'
        checked["session"]["account"]["id"] = checked["selected_workspace_id"] = "other-personal"
    if change == "team-label":
        checked["plan_type"] = "team"
    checked["health_status"] = "alive"
    boundary.check.side_effect = None
    boundary.check.return_value = checked
    with pytest.raises(RuntimeError, match="personal_workspace_|import_identity_user_mismatch"):
        boundary.route._persist_imported_account(previous, initial, Mock())
    boundary.persist.assert_not_called()
    assert previous == before


def test_unknown_type_is_not_accepted_based_on_personal_import_mode(boundary):
    candidate = tokens(team=False)
    candidate["access_token"] = "opaque-synthetic"
    candidate["session"]["account"] = {"id": "workspace-personal", "name": "Personal"}
    candidate["session"]["user"]["id"] = "user-a"
    candidate["plan_type"] = "free"  # A stale display label is not proof.
    with pytest.raises(RuntimeError, match="personal_workspace_unverified"):
        boundary.route._persist_imported_account({"email": "alice@example.test"}, candidate, Mock())
    boundary.check.assert_not_called()
    boundary.persist.assert_not_called()


def test_import_explicitly_requests_personal_validation_from_shared_login(boundary, monkeypatch):
    boundary.login.return_value = tokens(team=False)
    result = boundary.route._import_one(1, "alice@example.test", "synthetic", "JBSWY3DPEHPK3PXP", account_type="personal")
    assert result["status"] == "success"
    assert boundary.login.call_args.kwargs["require_personal_workspace"] is True
    assert boundary.login.call_args.kwargs["require_team_workspace"] is False

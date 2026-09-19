"""Personal terminal verification uses synthetic sessions and no external calls."""
from __future__ import annotations

import base64
from copy import deepcopy
import json

import pytest

from common.team_workspace import selected_workspace_metadata, validate_personal_session, validate_team_session


def _record(plan="free", *, workspace="ws-personal", user="user-a", session_plan=None):
    body = {"https://api.openai.com/auth": {
        "chatgpt_plan_type": plan, "chatgpt_account_id": workspace, "chatgpt_user_id": user,
    }}
    encoded = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    return {
        "access_token": f"synthetic.{encoded}.signature",
        "session": {
            "account": {"id": workspace, "planType": plan if session_plan is None else session_plan},
            "user": {"id": user},
        },
        "user_id": user,
        "plan_type": plan,
    }


@pytest.mark.parametrize("plan", ["free", "plus", "pro"])
def test_real_personal_plan_and_identity_are_accepted_without_chooser(plan):
    record = _record(plan)
    original = deepcopy(record)
    assert validate_personal_session(record) == {"plan_type": plan, "account_id": "ws-personal", "user_id": "user-a"}
    assert record == original


@pytest.mark.parametrize("plan", ["team", "business", "enterprise"])
@pytest.mark.parametrize("source", ["token", "session", "declared", "selected"])
def test_team_plan_from_any_relevant_source_cannot_be_masked_as_personal(plan, source):
    record = _record(plan if source == "token" else "free", session_plan=plan if source == "session" else "free")
    if source == "declared":
        record["plan_type"] = plan
    options = {}
    if source == "selected":
        options = {"selected_workspace_id": "ws-personal", "selected_workspace_metadata": {
            "id": "ws-personal", "type": "personal", "plan_type": plan,
        }}
    with pytest.raises(RuntimeError, match="personal_workspace_team_forbidden"):
        validate_personal_session(record, **options)


@pytest.mark.parametrize("kind", ["team", "organization", "org", "business", "enterprise"])
@pytest.mark.parametrize("source", ["session", "selected"])
def test_organization_type_overrides_free_plan_or_personal_flag(kind, source):
    record = _record()
    options = {}
    if source == "session":
        record["session"]["account"].update(type=kind, is_personal=True)
    else:
        options = {"selected_workspace_id": "ws-personal", "selected_workspace_metadata": {
            "id": "ws-personal", "account": {"structure": kind}, "is_personal": True,
        }}
    with pytest.raises(RuntimeError, match="personal_workspace_team_forbidden"):
        validate_personal_session(record, **options)


@pytest.mark.parametrize("plan", ["", "prolite", "self_serve_business_prolite", "self_serve_business_usage_based", "future_plan"])
def test_ambiguous_plan_requires_real_personal_structure(plan):
    record = _record(plan)
    record.update(import_account_type="personal", workspace_selection_mode="personal", plan_type="free", type="personal")
    record["session"]["account"]["name"] = "Personal"
    with pytest.raises(RuntimeError, match="personal_workspace_unverified"):
        validate_personal_session(record)
    record["session"]["account"]["structure"] = "personal"
    assert validate_personal_session(record)["plan_type"] == plan


@pytest.mark.parametrize("evidence", [{"structure": "personal"}, {"account_type": "individual"}, {"is_personal": True}, {"account": {"isPersonal": "true"}}])
def test_trusted_personal_selection_can_resolve_ambiguous_token(evidence):
    record = _record("prolite")
    metadata = selected_workspace_metadata({"id": "ws-personal", **evidence})
    expected = {"plan_type": "prolite", "account_id": "ws-personal", "user_id": "user-a"}
    assert validate_personal_session(record, selected_workspace_id="ws-personal", selected_workspace_metadata=metadata) == expected
    record.update(
        selected_workspace_id="ws-personal", selected_workspace_metadata=metadata,
        selected_workspace_evidence_source="auth_workspace_selection", workspace_selection_verified=True,
    )
    assert validate_personal_session(record) == expected


@pytest.mark.parametrize("use_saved", [False, True])
def test_display_name_must_not_turn_into_personal_type_evidence(use_saved):
    record = _record("prolite")
    metadata = selected_workspace_metadata({"id": "ws-personal", "name": "Personal"})
    options = {"selected_workspace_id": "ws-personal", "selected_workspace_metadata": metadata}
    if use_saved:
        record.update(**options, selected_workspace_evidence_source="auth_workspace_selection", workspace_selection_verified=True)
        options = {}
    with pytest.raises(RuntimeError, match="personal_workspace_unverified"):
        validate_personal_session(record, **options)


@pytest.mark.parametrize("evidence", [
    {"name": "Personal", "is_personal": False},
    {"name": "Personal", "is_personal": 0},
    {"plan_type": "free", "is_personal": False},
    {"type": "personal", "is_personal": False},
    {"isPersonal": True, "is_personal": False},
    {"is_personal": True, "personal": False},
    {"is_personal": True, "account": {"is_personal": False}},
])
@pytest.mark.parametrize("use_saved", [False, True])
def test_metadata_normalization_preserves_explicit_nonpersonal_counterevidence(evidence, use_saved):
    record = _record("free")
    metadata = selected_workspace_metadata({"id": "ws-personal", **evidence})
    if "is_personal" in evidence and not evidence["is_personal"]:
        assert metadata["is_personal"] is False
    options = {"selected_workspace_id": "ws-personal", "selected_workspace_metadata": metadata}
    if use_saved:
        record.update(**options, selected_workspace_evidence_source="auth_workspace_selection", workspace_selection_verified=True)
        options = {}
    with pytest.raises(RuntimeError, match="personal_workspace_plan_mismatch"):
        validate_personal_session(record, **options)


def test_preserving_false_does_not_weaken_existing_team_rejection_of_personal_label():
    metadata = selected_workspace_metadata({"id": "ws-personal", "name": "Personal", "is_personal": False})
    with pytest.raises(RuntimeError, match="team_workspace_personal_forbidden"):
        validate_team_session(_record("team"), selected_workspace_id="ws-personal", selected_workspace_metadata=metadata)


@pytest.mark.parametrize("plan", ["prolite", "self_serve_business_prolite", "self_serve_business_usage_based"])
def test_ambiguous_token_can_use_real_current_session_plan_but_not_legacy_row_label(plan):
    assert validate_personal_session(_record(plan, session_plan="free"))["plan_type"] == plan
    record = _record(plan)
    record["plan_type"] = "free"
    with pytest.raises(RuntimeError, match="personal_workspace_unverified"):
        validate_personal_session(record)


@pytest.mark.parametrize("mutation,error", [
    (lambda row: row["session"]["account"].update(id="other-workspace"), "personal_workspace_id_mismatch"),
    (lambda row: row["session"]["account"].update(account_id="other-workspace"), "personal_workspace_id_mismatch"),
    (lambda row: row["session"]["user"].update(id="other-user"), "personal_workspace_user_mismatch"),
    (lambda row: row.update(user_id="other-user"), "personal_workspace_user_mismatch"),
    (lambda row: row.update(selected_workspace_id="other-workspace"), "personal_workspace_id_mismatch"),
    (lambda row: row["session"]["account"].update(is_personal=False), "personal_workspace_plan_mismatch"),
    (lambda row: row["session"]["account"].update(is_personal=0), "personal_workspace_plan_mismatch"),
])
def test_conflicting_identity_or_personal_flag_is_rejected(mutation, error):
    record = _record()
    mutation(record)
    with pytest.raises(RuntimeError, match=error):
        validate_personal_session(record)


@pytest.mark.parametrize("options,error", [
    ({"expected_user_id": "other-user"}, "personal_workspace_user_mismatch"),
    ({"selected_workspace_id": "other-workspace"}, "personal_workspace_id_mismatch"),
    ({"selected_workspace_id": "ws-personal", "selected_workspace_metadata": {"type": "personal"}}, "personal_workspace_unverified"),
    ({"selected_workspace_metadata": {"id": "ws-personal", "type": "personal"}}, "personal_workspace_unverified"),
    ({"selected_workspace_id": "ws-personal", "selected_workspace_metadata": {"id": "other-workspace", "type": "personal"}}, "personal_workspace_id_mismatch"),
])
def test_selected_metadata_cannot_be_borrowed_from_another_identity(options, error):
    with pytest.raises(RuntimeError, match=error):
        validate_personal_session(_record(), **options)


@pytest.mark.parametrize("missing", ["token", "account", "user"])
def test_legacy_row_labels_cannot_supply_missing_live_identity(missing):
    record = _record(workspace="" if missing == "account" else "ws-personal", user="" if missing == "user" else "user-a")
    if missing == "token":
        record["access_token"] = ""
    record.update(account_id="legacy-personal", user_id="user-a", plan_type="free", type="personal")
    with pytest.raises(RuntimeError, match="personal_workspace_unverified"):
        validate_personal_session(record)


def test_opaque_access_token_requires_real_session_identity_and_type():
    record = {"access_token": "opaque-fixture", "plan_type": "free", "account_id": "legacy", "user_id": "user-a"}
    with pytest.raises(RuntimeError, match="personal_workspace_unverified"):
        validate_personal_session(record)
    record["session"] = {"account": {"id": "ws-personal", "type": "personal"}, "user": {"id": "user-a"}}
    assert validate_personal_session(record) == {"plan_type": "", "account_id": "ws-personal", "user_id": "user-a"}


def test_user_and_workspace_comparison_is_case_insensitive_and_preserves_actual_values():
    result = validate_personal_session(_record(workspace="WS-PERSONAL", user="USER-A"), selected_workspace_id="ws-personal", expected_user_id="user-a")
    assert result["account_id"] == "WS-PERSONAL" and result["user_id"] == "USER-A"

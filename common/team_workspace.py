"""Resolve and verify an explicitly requested team session without changing user identity."""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import unquote


_WORKSPACE_TYPE_KEYS = ("type", "kind", "account_type", "workspace_type", "structure")
_PERSONAL_FLAG_KEYS = ("is_personal", "isPersonal", "personal")
_PLAN_KEYS = ("plan_type", "planType", "chatgpt_plan_type")
_PERSONAL_PLANS = {"free", "plus", "pro"}
_BUSINESS_PLAN_HINTS = {"self_serve_business_usage_based", "self_serve_business_prolite"}
_SELECTION_EVIDENCE_SOURCE = "auth_workspace_selection"
_DISPLAY_PERSONAL_MARKER = "_personal_from_display_name"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _team_plan(value: Any) -> bool:
    plan = _text(value).lower()
    # Plan labels such as ``self_serve_business_prolite`` are subscription
    # identifiers, not proof of the selected workspace type. Workspace
    # metadata/ID is the authority for personal vs Team selection.
    return plan in {"team", "business", "enterprise"}


def _explicit_team_workspace(item: dict[str, Any]) -> bool:
    if not isinstance(item, dict):
        return False
    account = item.get("account") if isinstance(item.get("account"), dict) else {}
    for source in (item, account):
        for key in ("type", "kind", "account_type", "workspace_type", "structure"):
            if _text(source.get(key)).lower() in {"team", "business", "enterprise", "organization", "org", "workspace"}:
                return True
    return False


def _explicit_personal_workspace(item: dict[str, Any]) -> bool:
    """Read structural personal evidence, excluding display-name inference."""
    if not isinstance(item, dict):
        return False
    account = item.get("account") if isinstance(item.get("account"), dict) else {}
    for source in (item, account):
        if not source.get(_DISPLAY_PERSONAL_MARKER):
            if any(source.get(key) is True or _text(source.get(key)).lower() in {"true", "1"}
                   for key in _PERSONAL_FLAG_KEYS):
                return True
        if any(_text(source.get(key)).lower() in {"personal", "individual", "default", "user"}
               for key in _WORKSPACE_TYPE_KEYS):
            return True
    return False


def selected_workspace_metadata(item: dict[str, Any]) -> dict[str, Any]:
    """Keep only identity/type evidence from the just-selected auth workspace.

    Display names never prove team membership. Do not persist the entire auth
    response, which can contain credentials or unrelated workspace information.
    """
    if not isinstance(item, dict):
        return {}
    result: dict[str, Any] = {}
    if item.get(_DISPLAY_PERSONAL_MARKER) is True:
        result[_DISPLAY_PERSONAL_MARKER] = True
    for key in ("id", "account_id", *_WORKSPACE_TYPE_KEYS, *_PLAN_KEYS):
        if isinstance(item.get(key), str) and item[key].strip():
            result[key] = item[key].strip()[:160]
    for key in _PERSONAL_FLAG_KEYS:
        value = item.get(key)
        if type(value) is bool:
            result[key] = value
        elif str(value).strip().lower() in {"true", "1", "false", "0"}:
            result[key] = str(value).strip().lower() in {"true", "1"}
    if isinstance(item.get("account"), dict):
        nested = {key: item["account"][key] for key in (
            "id", "account_id", *_WORKSPACE_TYPE_KEYS, *_PLAN_KEYS, *_PERSONAL_FLAG_KEYS,
            _DISPLAY_PERSONAL_MARKER,
        ) if key in item["account"]}
        result["account"] = selected_workspace_metadata(nested)
    if _personal_workspace(item):
        # Never overwrite an explicit non-personal flag with an inference from
        # a plan, another flag or a display name. Conflicts must remain visible
        # to the terminal validator after this metadata has been persisted.
        result.setdefault("is_personal", True)
        account = item.get("account") if isinstance(item.get("account"), dict) else {}
        if not _explicit_personal_workspace(item) and not any(
            _text(source.get(key)).lower() in _PERSONAL_PLANS
            for source in (item, account) for key in _PLAN_KEYS
        ):
            # Preserve the existing Team-side rejection of a Personal-labelled
            # selection without promoting its name into positive personal proof.
            result[_DISPLAY_PERSONAL_MARKER] = True
    return result


def _saved_workspace_metadata(record: dict[str, Any]) -> dict[str, Any]:
    if (record.get("workspace_selection_verified") is True
            and record.get("selected_workspace_evidence_source") == _SELECTION_EVIDENCE_SOURCE):
        return selected_workspace_metadata(record.get("selected_workspace_metadata"))
    return {}


def workspace_kind(item: dict[str, Any]) -> str:
    """Distinguish unknown types from explicit personal/team workspace data."""
    if not isinstance(item, dict):
        return "unknown"
    if _personal_workspace(item):
        return "personal"
    if _explicit_team_workspace(item):
        return "team"
    account = item.get("account") if isinstance(item.get("account"), dict) else {}
    if any(_team_plan(source.get(key)) for source in (item, account) for key in _PLAN_KEYS):
        return "team"
    return "unknown"


def workspace_entries(payload: Any) -> list[dict[str, Any]]:
    """Read the known auth response wrappers and signed auth-session cookie formats."""
    def collect(value: Any, depth: int = 0) -> list[dict[str, Any]]:
        if depth > 6:
            return []
        if isinstance(value, str):
            raw = unquote(value)
            candidates = [raw]
            for part in raw.split(".")[:2]:
                try:
                    candidates.append(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)).decode())
                except (ValueError, UnicodeError):
                    continue
            for candidate in candidates:
                try:
                    decoded = json.loads(candidate)
                except (ValueError, TypeError):
                    continue
                if isinstance(decoded, dict):
                    result = collect(decoded, depth + 1)
                    if result:
                        return result
            return []
        if not isinstance(value, dict):
            return []
        result = []
        items = value.get("workspaces")
        if isinstance(items, list):
            result.extend(item for item in items if isinstance(item, dict) and item.get("id"))
        elif isinstance(items, dict):
            result.extend({"id": key, **item} for key, item in items.items() if isinstance(item, dict))
        for key in ("client_auth_session", "oai-client-auth-session", "page", "payload", "data"):
            result.extend(collect(value.get(key), depth + 1))
        return result

    unique = {}
    for item in collect(payload):
        key = _text(item.get("id"))
        if key:
            unique[key] = {**unique.get(key, {}), **item}
    return list(unique.values())


def _personal_workspace(item: dict[str, Any]) -> bool:
    account = item.get("account") if isinstance(item.get("account"), dict) else {}
    for source in (item, account):
        # A Personal display label remains conservative rejection evidence for
        # Team selection, even when a conflicting explicit False is preserved.
        # validate_personal_session never uses this marker as positive proof.
        if source.get(_DISPLAY_PERSONAL_MARKER) is True:
            return True
        for key in ("is_personal", "isPersonal", "personal"):
            if source.get(key) is True or _text(source.get(key)).lower() in {"true", "1"}:
                return True
        for key in ("type", "kind", "account_type", "workspace_type", "structure"):
            if _text(source.get(key)).lower() in {"personal", "individual", "default", "user"}:
                return True
        for key in ("plan_type", "planType", "chatgpt_plan_type"):
            if _text(source.get(key)).lower() in {"free", "plus", "pro"}:
                return True
    label = _text(item.get("name") or item.get("title") or item.get("label")).lower()
    return label in {"personal", "personal workspace", "personal space", "个人", "个人空间", "个人工作区"}


def select_team_workspace(workspaces: list[dict[str, Any]], workspace_name: str = "") -> dict[str, Any]:
    wanted = _text(workspace_name).casefold()
    items = [item for item in workspaces if isinstance(item, dict) and item.get("id")]
    if not items:
        raise RuntimeError("team_workspace_unavailable: 认证会话未提供工作区列表，未选择个人空间")
    if wanted:
        # Accept either a displayed workspace name or its stable ID.
        named = [item for item in items if _text(item.get("id")).casefold() == wanted]
        if not named:
            named = [item for item in items if _text(item.get("name") or item.get("title") or item.get("label")).casefold() == wanted]
        matches = [item for item in named if not _personal_workspace(item)]
        if named and not matches:
            raise RuntimeError("team_workspace_personal_forbidden: 指定的是个人空间，Team 导入已停止")
        if not named:
            raise RuntimeError(f"team_workspace_name_not_found: 未找到指定团队工作区「{workspace_name}」")
    else:
        matches = [item for item in items if not _personal_workspace(item)]
    if len(matches) != 1:
        reason = "没有可用团队工作区" if not matches else "存在多个候选工作区，请指定团队工作区名称"
        raise RuntimeError("team_workspace_ambiguous: " + reason)
    return matches[0]


def select_personal_workspace(workspaces: list[dict[str, Any]], workspace_name: str = "") -> dict[str, Any]:
    """Select a real personal workspace without inferring it from plan_type."""
    items = [item for item in workspaces if isinstance(item, dict) and item.get("id")]
    wanted = _text(workspace_name).casefold()
    personal = [item for item in items if _personal_workspace(item)]
    if wanted:
        matches = [item for item in personal if (
            _text(item.get("id")).casefold() == wanted
            or _text(item.get("name") or item.get("title") or item.get("label")).casefold() == wanted
        )]
        if not matches:
            raise RuntimeError(f"personal_workspace_not_found: 未找到指定个人工作区「{workspace_name}」")
        return matches[0]
    if len(personal) == 1:
        return personal[0]
    if not personal:
        raise RuntimeError("personal_workspace_unavailable: 当前会话没有可识别的个人工作区；若账号只有 Team/Business，请选择 Team")
    raise RuntimeError("personal_workspace_ambiguous: 检测到多个个人工作区，请指定 Workspace ID")


def validate_team_session(
    record: dict[str, Any], *, selected_workspace_id: str = "", expected_user_id: str = "",
    selected_workspace_metadata: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Validate session/token evidence, never turn a personal token into a team label."""
    token = _text(record.get("access_token"))
    auth = {}
    try:
        part = token.split(".")[1]
        body = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        auth = body.get("https://api.openai.com/auth") or {}
    except (IndexError, ValueError, UnicodeError, AttributeError):
        pass
    if not isinstance(auth, dict):
        auth = {}
    session = record.get("session") if isinstance(record.get("session"), dict) else {}
    account = session.get("account") if isinstance(session.get("account"), dict) else {}
    token_plan = _text(auth.get("chatgpt_plan_type"))
    session_plan = _text(account.get("planType") or account.get("plan_type"))
    plan = token_plan or session_plan
    if not token or not plan:
        raise RuntimeError("team_workspace_unverified: 缺少真实工作区套餐信息，未按 Team 导入成功")
    token_account = _text(auth.get("chatgpt_account_id"))
    session_account = _text(account.get("id") or account.get("account_id"))
    account_id = token_account or session_account
    if not account_id:
        raise RuntimeError("team_workspace_unverified: 缺少实际工作区 ID")
    if token_account and session_account and token_account.casefold() != session_account.casefold():
        raise RuntimeError("team_workspace_id_mismatch: Token 与会话的工作区 ID 不一致")
    selected_id = _text(selected_workspace_id or record.get("selected_workspace_id"))
    if selected_id and account_id.casefold() != selected_id.casefold():
        raise RuntimeError("team_workspace_id_mismatch: 实际会话不属于所选团队工作区")
    session_user = session.get("user") if isinstance(session.get("user"), dict) else {}
    token_user = _text(auth.get("chatgpt_user_id") or auth.get("user_id"))
    session_user_id = _text(session_user.get("id"))
    user_id = token_user or session_user_id
    recorded_user = _text(record.get("user_id"))
    user_ids = {value.casefold() for value in (token_user, session_user_id, recorded_user) if value}
    if len(user_ids) > 1:
        raise RuntimeError("team_workspace_user_mismatch: 会话用户身份不一致，未覆盖已有账号")
    if expected_user_id and (not user_id or user_id.casefold() != _text(expected_user_id).casefold()):
        raise RuntimeError("team_workspace_user_mismatch: 测活改变了用户身份，未覆盖已有账号")
    if not user_id:
        raise RuntimeError("team_workspace_unverified: 缺少实际会话用户标识")

    # An explicit argument comes only from the current auth transaction. Saved
    # evidence must carry our successful-selection marker and still match the
    # fresh session's workspace; a stale row's type/name cannot label a new token.
    metadata = (selected_workspace_metadata if isinstance(selected_workspace_metadata, dict)
                else _saved_workspace_metadata(record))
    if metadata:
        evidence_id = _text(metadata.get("id") or metadata.get("account_id"))
        nested = metadata.get("account") if isinstance(metadata.get("account"), dict) else {}
        nested_id = _text(nested.get("id") or nested.get("account_id"))
        if not evidence_id or not selected_id:
            raise RuntimeError("team_workspace_unverified: 工作区类型证据缺少所选工作区 ID")
        if any(value.casefold() != account_id.casefold() for value in (evidence_id, nested_id) if value):
            raise RuntimeError("team_workspace_id_mismatch: 工作区类型证据不属于实际会话")

    declared_plan = _text(record.get("plan_type"))
    if token_plan.lower() in _PERSONAL_PLANS or _personal_workspace(account) or _personal_workspace(metadata):
        raise RuntimeError(f"team_workspace_personal_forbidden: 实际会话套餐为 {plan}，未接受个人空间")
    if declared_plan.lower() in _PERSONAL_PLANS:
        raise RuntimeError("team_workspace_plan_mismatch: 登录或测活结果回到了个人套餐，未覆盖已有账号")
    has_team_type = _explicit_team_workspace(account) or _explicit_team_workspace(metadata)
    if not _team_plan(plan) and not has_team_type:
        reason = "业务套餐仍缺少明确工作区类型" if plan.lower() in _BUSINESS_PLAN_HINTS else "工作区类型未确认"
        raise RuntimeError(f"team_workspace_unverified: {reason}，未按 Team 导入成功")
    return {"plan_type": plan, "account_id": account_id, "user_id": user_id}


def validate_personal_session(
    record: dict[str, Any], *, selected_workspace_id: str = "", expected_user_id: str = "",
    selected_workspace_metadata: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Verify a personal terminal session from its actual identity and type.

    A personal import mode, display name or stored plan label is not proof.
    Ambiguous slugs, including ``prolite``, require structural personal evidence.
    """
    token = _text(record.get("access_token"))
    auth: dict[str, Any] = {}
    try:
        part = token.split(".")[1]
        body = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        value = body.get("https://api.openai.com/auth") if isinstance(body, dict) else None
        if isinstance(value, dict):
            auth = value
    except (IndexError, ValueError, UnicodeError):
        pass
    session = record.get("session") if isinstance(record.get("session"), dict) else {}
    account = session.get("account") if isinstance(session.get("account"), dict) else {}
    session_user = session.get("user") if isinstance(session.get("user"), dict) else {}
    token_plan = _text(auth.get("chatgpt_plan_type"))
    session_plan = _text(account.get("planType") or account.get("plan_type"))
    plan = token_plan or session_plan
    token_account = _text(auth.get("chatgpt_account_id"))
    account_ids = [_text(account.get(key)) for key in ("id", "account_id")]
    actual_ids = [value for value in (token_account, *account_ids) if value]
    if not token or not actual_ids:
        raise RuntimeError("personal_workspace_unverified: 缺少真实会话或实际工作区 ID，未按个人导入成功")
    if len({value.casefold() for value in actual_ids}) > 1:
        raise RuntimeError("personal_workspace_id_mismatch: Token 与会话的工作区 ID 不一致")
    account_id = actual_ids[0]
    selected_id = _text(selected_workspace_id or record.get("selected_workspace_id"))
    if selected_id and selected_id.casefold() != account_id.casefold():
        raise RuntimeError("personal_workspace_id_mismatch: 实际会话不属于所选个人工作区")

    token_user = _text(auth.get("chatgpt_user_id") or auth.get("user_id"))
    session_user_id = _text(session_user.get("id"))
    user_id = token_user or session_user_id
    recorded_user = _text(record.get("user_id"))
    if len({value.casefold() for value in (token_user, session_user_id, recorded_user) if value}) > 1:
        raise RuntimeError("personal_workspace_user_mismatch: 会话用户身份不一致，未覆盖已有账号")
    if expected_user_id and (not user_id or user_id.casefold() != _text(expected_user_id).casefold()):
        raise RuntimeError("personal_workspace_user_mismatch: 测活改变了用户身份，未覆盖已有账号")
    if not user_id:
        raise RuntimeError("personal_workspace_unverified: 缺少实际会话用户标识")

    metadata = (selected_workspace_metadata if isinstance(selected_workspace_metadata, dict)
                else _saved_workspace_metadata(record))
    nested = metadata.get("account") if isinstance(metadata.get("account"), dict) else {}
    if metadata:
        evidence_ids = [_text(metadata.get(key)) for key in ("id", "account_id")]
        if not any(evidence_ids) or not selected_id:
            raise RuntimeError("personal_workspace_unverified: 工作区类型证据缺少所选工作区 ID")
        evidence_ids.extend(_text(nested.get(key)) for key in ("id", "account_id"))
        if any(value.casefold() != account_id.casefold() for value in evidence_ids if value):
            raise RuntimeError("personal_workspace_id_mismatch: 工作区类型证据不属于实际会话")

    # A conflicting measured plan/type must never be masked by a free token,
    # an older selection or a caller-supplied personal label.
    sources = (auth, account, metadata, nested)
    plans = [token_plan, session_plan, _text(record.get("plan_type"))]
    plans.extend(_text(source.get(key)) for source in sources for key in _PLAN_KEYS)
    if any(_team_plan(value) for value in plans) or any(_explicit_team_workspace(source) for source in sources):
        raise RuntimeError("personal_workspace_team_forbidden: 实际会话属于 Team/Business/组织工作区，未按个人导入")
    if any(
        source.get(key) is False or (source.get(key) is not None and str(source.get(key)).strip().lower() in {"false", "0"})
        for source in sources for key in _PERSONAL_FLAG_KEYS
    ):
        raise RuntimeError("personal_workspace_plan_mismatch: 工作区明确标记为非个人，未按个人导入")

    actual_personal_plan = any(value.lower() in _PERSONAL_PLANS for value in (token_plan, session_plan))
    explicit_personal = any(_explicit_personal_workspace(source) for source in sources)
    if not actual_personal_plan and not explicit_personal:
        raise RuntimeError("personal_workspace_unverified: 缺少明确个人工作区证据，未按个人导入成功")
    return {"plan_type": plan, "account_id": account_id, "user_id": user_id}


def session_identity(record: dict[str, Any]) -> dict[str, str]:
    """Read current token/session evidence, excluding the legacy account row key.

    ``record.account_id`` may deliberately retain a member's old personal ID.
    It is never evidence of the workspace to which the current token belongs.
    """
    body = {}
    try:
        part = _text(record.get("access_token")).split(".")[1]
        decoded = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        if isinstance(decoded, dict):
            body = decoded
    except (IndexError, ValueError, UnicodeError):
        pass
    auth = body.get("https://api.openai.com/auth")
    auth = auth if isinstance(auth, dict) else {}
    profile = body.get("https://api.openai.com/profile")
    profile = profile if isinstance(profile, dict) else {}
    session = record.get("session")
    session = session if isinstance(session, dict) else {}
    account = session.get("account")
    account = account if isinstance(account, dict) else {}
    user = session.get("user")
    user = user if isinstance(user, dict) else {}
    return {
        "user_id": _text(auth.get("chatgpt_user_id") or auth.get("user_id") or user.get("id")),
        "account_id": _text(auth.get("chatgpt_account_id") or account.get("id") or account.get("account_id")),
        "plan_type": _text(auth.get("chatgpt_plan_type") or account.get("planType") or account.get("plan_type")),
        "email": _text(profile.get("email") or body.get("email") or user.get("email") or record.get("email")),
    }


def requires_team_workspace(record: dict[str, Any]) -> bool:
    """Keep an imported/verified team constraint across later credential changes."""
    mode = _text(record.get("workspace_selection_mode")).lower()
    import_type = _text(record.get("import_account_type")).lower()
    if mode == "personal" or import_type == "personal":
        return False
    if mode == "team" or import_type == "team":
        return True
    return (
        import_type == "team"
        or _team_plan(record.get("plan_type"))
        or _team_plan(session_identity(record)["plan_type"])
        or bool(record.get("workspace_selection_verified") is True and _text(record.get("selected_workspace_id")))
    )


def validate_session_replacement(
    previous: dict[str, Any], candidate: dict[str, Any], *, require_identity: bool = False,
) -> dict[str, str]:
    """Validate fresh credentials BEFORE merging in the previous row's metadata.

    Existing stable user IDs must remain verifiable. Legacy personal records
    without a user ID retain their existing behavior, while any returned email
    mismatch is rejected. Team callers can additionally require shared identity
    evidence when only the old email is available.
    """
    old = session_identity(previous)
    new = session_identity(candidate)
    # The row's stable user/email identify the account being maintained. Its
    # old token may already be stale (for example immediately after a rebind).
    expected_user = _text(previous.get("user_id")) or old["user_id"]
    expected_email = _text(previous.get("email")) or old["email"]
    users_match = bool(expected_user and new["user_id"] and new["user_id"].casefold() == expected_user.casefold())
    if expected_user and not users_match:
        raise RuntimeError("session_identity_user_mismatch: 新凭据不属于原用户或缺少可验证的用户标识，保留原凭据")
    if expected_email and new["email"] and new["email"].casefold() != expected_email.casefold():
        raise RuntimeError("session_identity_email_mismatch: 新会话邮箱与原账号不一致，保留原凭据")
    if require_identity and not (
        users_match
        or (expected_email and new["email"] and new["email"].casefold() == expected_email.casefold())
    ):
        raise RuntimeError("session_identity_unverified: 缺少原用户与新会话的一致性证据，保留原凭据")
    if requires_team_workspace(previous):
        selected_id = _text(previous.get("selected_workspace_id"))
        old_session = previous.get("session") if isinstance(previous.get("session"), dict) else {}
        old_account = old_session.get("account") if isinstance(old_session.get("account"), dict) else {}
        # An operator may request Team for a legacy account whose only saved
        # credentials still belong to its personal workspace. Until a Team
        # selection was verified, that personal ID is not a Team constraint.
        # Business/unknown legacy IDs remain pinned; only explicit personal
        # evidence permits the initial transition to a verified Team session.
        old_is_personal = old["plan_type"].lower() in _PERSONAL_PLANS or _personal_workspace(old_account)
        if not selected_id and not old_is_personal:
            selected_id = old["account_id"]
        verified = validate_team_session(
            candidate, selected_workspace_id=selected_id, expected_user_id=expected_user,
            selected_workspace_metadata=(
                _saved_workspace_metadata(candidate) or _saved_workspace_metadata(previous) or None
            ),
        )
        new.update(verified)
    return new

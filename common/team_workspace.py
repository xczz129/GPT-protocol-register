"""Resolve and verify an explicitly requested team session without changing user identity."""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import unquote


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
            if _text(source.get(key)).lower() in {"team", "business", "enterprise", "organization", "org"}:
                return True
    return False


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


def validate_team_session(record: dict[str, Any], *, selected_workspace_id: str = "", expected_user_id: str = "") -> dict[str, str]:
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
    if _personal_workspace(account) or (not _team_plan(plan) and not _explicit_team_workspace(account)):
        raise RuntimeError(f"team_workspace_personal_forbidden: 实际会话套餐为 {plan}，未接受个人空间")
    declared_plan = _text(record.get("plan_type"))
    if ((session_plan and not _team_plan(session_plan) and not _explicit_team_workspace(account))
            or (declared_plan and not _team_plan(declared_plan) and not _explicit_team_workspace(account))):
        raise RuntimeError("team_workspace_plan_mismatch: 登录或测活结果回到了个人套餐，未覆盖已有账号")
    account_id = _text(auth.get("chatgpt_account_id") or account.get("id") or account.get("account_id"))
    if not account_id:
        raise RuntimeError("team_workspace_unverified: 缺少实际工作区 ID")
    if selected_workspace_id and account_id.casefold() != _text(selected_workspace_id).casefold():
        raise RuntimeError("team_workspace_id_mismatch: 实际会话不属于所选团队工作区")
    user_id = _text(auth.get("chatgpt_user_id") or auth.get("user_id") or record.get("user_id"))
    recorded_user = _text(record.get("user_id"))
    if user_id and recorded_user and user_id.casefold() != recorded_user.casefold():
        raise RuntimeError("team_workspace_user_mismatch: 会话用户身份不一致，未覆盖已有账号")
    if expected_user_id and (not user_id or user_id.casefold() != _text(expected_user_id).casefold()):
        raise RuntimeError("team_workspace_user_mismatch: 测活改变了用户身份，未覆盖已有账号")
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
        if not selected_id and _team_plan(old["plan_type"]):
            selected_id = old["account_id"]
        verified = validate_team_session(
            candidate, selected_workspace_id=selected_id, expected_user_id=expected_user,
        )
        new.update(verified)
    return new

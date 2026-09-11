"""Read-only relay planning and strict validation of reusable Codex credentials."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from common.team_workspace import requires_team_workspace, session_identity, validate_session_replacement

CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _claims(token: Any) -> dict:
    try:
        part = _text(token).split(".")[1]
        result = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return result if isinstance(result, dict) else {}
    except (ValueError, IndexError, UnicodeError):
        return {}


def auth_known_invalid(record: dict) -> bool:
    """Only explicit authentication failures count; quota/network failures do not."""
    text = " ".join(_text(record.get(key)).lower() for key in (
        "error", "health_error", "last_token_refresh_error", "quota_error",
    ))
    if any(marker in text for marker in (
        "invalid_grant", "refresh_token_reused", "refresh token reused", "refresh_token_invalid",
        "token has been revoked", "token revoked", "token invalidated", "invalid_refresh_token",
        "refresh token expired", "refresh_token_expired", "invalid credentials",
    )):
        return True
    if re.search(r"(?:token refresh|oauth_refresh)[ _-]*(?:http[ _-]*)?(?:400|401|403)\b", text):
        return True
    if "usage http 401" in text or "oauth_refresh_http_401" in text:
        return True
    # A bare refresh_failed can mean a proxy/429/5xx error, so never infer
    # invalid credentials from that broad cache classification alone.
    return _text(record.get("status")).lower() == "unauthorized" and not any(
        marker in text for marker in ("429", "timeout", "timed out", "network", "connection", "502", "503", "504")
    )


def safe_stored_bundle(record: dict) -> dict:
    """A saved RT alone is insufficient proof that its AT belongs to Codex."""
    if not _text(record.get("access_token")) or not _text(record.get("refresh_token")):
        raise RuntimeError("保存的 AT/RT 不完整，需要重新授权")
    access_claims = _claims(record.get("access_token"))
    id_claims = _claims(record.get("id_token"))
    access_clients = [_text(access_claims.get(key)) for key in ("client_id", "azp") if access_claims.get(key)]
    auth = access_claims.get("https://api.openai.com/auth")
    if isinstance(auth, dict) and auth.get("client_id"):
        access_clients.append(_text(auth["client_id"]))
    if access_clients and any(client != CODEX_CLIENT_ID for client in access_clients):
        raise RuntimeError("保存的 AT 不属于 Codex 客户端，需要重新授权")
    audience = id_claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience] if audience else []
    if audiences and CODEX_CLIENT_ID not in audiences:
        raise RuntimeError("保存的 ID Token 不属于 Codex 客户端，需要重新授权")
    if not access_clients and not (
        CODEX_CLIENT_ID in audiences and _text(record.get("oauth_client_id")) == CODEX_CLIENT_ID
    ):
        raise RuntimeError("无法确认保存凭据的 Codex 客户端，需要重新授权")
    fresh = {key: _text(record.get(key)) for key in ("access_token", "refresh_token", "id_token")}
    identity = validate_session_replacement(record, fresh, require_identity=True)
    if not identity["user_id"] or not identity["account_id"] or not identity["plan_type"]:
        raise RuntimeError("保存的 Token 缺少用户、工作区或套餐证据，需要重新授权")
    if id_claims:
        identity_from_id = session_identity({"access_token": fresh["id_token"]})
        for key in ("user_id", "account_id", "email"):
            if identity_from_id[key] and identity[key] and identity_from_id[key].casefold() != identity[key].casefold():
                raise RuntimeError("保存的 AT 与 ID Token 身份不一致，需要重新授权")
    return fresh


def read_relay_entry(path: Path, cache: dict | None = None) -> dict:
    """Read one exact candidate for a final local recheck before writing."""
    if path.is_symlink():
        return {"file": path.name, "record": {}, "sha256": "", "mtime": 0.0, "unsafe": True, "cache": {}}
    try:
        raw = path.read_bytes()
        record = json.loads(raw)
        if not isinstance(record, dict):
            record = {}
        stat = path.stat()
    except (OSError, ValueError, UnicodeError):
        return {"file": path.name, "record": {}, "sha256": "", "mtime": 0.0, "cache": {}}
    metadata = dict(cache or {})
    fetched = metadata.get("fetched_ts")
    if isinstance(fetched, (int, float)) and fetched < stat.st_mtime:
        metadata = {}  # A newer on-disk rotation invalidates the old quota error.
    return {
        "file": path.name, "record": record, "sha256": hashlib.sha256(raw).hexdigest(),
        "mtime": stat.st_mtime, "cache": metadata,
    }


def scan_relay(directory: Path, cache: dict | None = None) -> dict:
    """Read every relay file once and index by email for an entire batch."""
    context: dict = {"configured": directory.is_dir(), "by_email": {}, "by_file": {}}
    if not context["configured"]:
        return context
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        context["configured"] = False
        return context
    for path in paths:
        if not path.is_file():
            continue
        entry = read_relay_entry(path, (cache or {}).get(path.name))
        record = entry["record"]
        email = _text(record.get("email") or session_identity(record)["email"]).casefold()
        context["by_file"][path.name] = entry
        if email:
            context["by_email"].setdefault(email, []).append(entry)
    return context


def relay_entry_status(previous: dict, entry: dict) -> str:
    """A shared Team workspace ID is never sufficient to match a member."""
    record = entry.get("record") or {}
    email = _text(previous.get("email")).casefold()
    if _text(record.get("email")).casefold() != email:
        return "mismatch"
    if entry.get("unsafe"):
        return "unverified"
    if not _text(record.get("access_token")) or not _text(record.get("refresh_token")):
        return "incomplete"
    try:
        identity = validate_session_replacement(
            previous, {"access_token": record["access_token"]}, require_identity=True,
        )
    except RuntimeError:
        return "mismatch"
    expected_id = _text(previous.get("selected_workspace_id")) or session_identity(previous)["account_id"]
    if not identity["user_id"] or not identity["account_id"] or not identity["plan_type"]:
        return "unverified"
    if expected_id and identity["account_id"].casefold() != expected_id.casefold():
        return "mismatch"
    try:
        safe_stored_bundle({**previous, **record})
    except RuntimeError:
        return "unverified"
    if not _text(record.get("account_id")) or _text(record.get("account_id")).casefold() != identity["account_id"].casefold():
        return "incomplete"
    if _text(record.get("type") or record.get("provider")).lower() != "codex":
        return "unverified"
    if record.get("disabled") is True or auth_known_invalid(record) or auth_known_invalid(entry.get("cache") or {}):
        return "invalid"
    return "ready"


def relay_state(previous: dict, context: dict) -> dict:
    entries = context.get("by_email", {}).get(_text(previous.get("email")).casefold(), [])
    candidates = []
    for entry in entries:
        status = relay_entry_status(previous, entry)
        if status == "ready":
            return {"status": "ready", "file": entry["file"]}
        candidates.append((status, entry["file"]))
    for preferred in ("invalid", "incomplete", "unverified", "mismatch"):
        for status, filename in candidates:
            if status == preferred:
                return {"status": preferred, "file": filename}
    return {"status": "missing", "file": ""}

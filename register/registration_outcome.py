"""Registration-only outcomes shared by the HTTP and browser engines."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlsplit

from mail.parsing import normalize_provider_type


class RegisteredEmailDetected(RuntimeError):
    """The candidate belongs to an existing identity, so registration must stop."""

    code = "already_registered"

    def __init__(self, detail: str = "", *, email: str = "") -> None:
        self.email = str(email or "").strip()
        suffix = f": {str(detail).strip()[:160]}" if detail else ""
        super().__init__(f"{self.code}{suffix}")


def requires_fresh_registration(mailbox: dict[str, Any] | None) -> bool:
    return normalize_provider_type(str((mailbox or {}).get("provider") or "")) == "smsbower_mail"


def registration_auth_state(url: str = "", page_type: str = "") -> str:
    """Classify explicit auth routes/types, never copy or arbitrary URL queries."""
    try:
        parsed = urlsplit(str(url or ""))
        if parsed.netloc and (
            parsed.hostname != "auth.openai.com"
            or parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
        ):
            return ""
        path = unquote(parsed.path).rstrip("/").lower()
    except (TypeError, ValueError):
        return ""
    normalized_page = str(page_type or "").strip().lower().replace("-", "_")
    if path in {
        "/log-in/password", "/login/password", "/sign-in/password",
        "/reset-password", "/reset-password/new-password", "/mfa-challenge",
    } or normalized_page in {
        "password_verification", "login_password", "mfa_challenge",
        "reset_password", "reset_password_new_password",
    }:
        return "existing"
    if path in {
        "/create-account/password", "/signup/password", "/sign-up/password",
        "/auth/username_password",
    } or normalized_page in {
        "create_account_password", "signup_password", "username_password_create",
    }:
        return "signup"
    return ""


_DUPLICATE_CODES = frozenset({
    "user_already_exists", "username_already_exists", "email_already_exists",
    "account_already_exists", "email_already_registered", "account_already_registered",
})


def is_duplicate_registration_error(error: Any) -> bool:
    """Accept an exact API error code; transient errors and page copy are not proof."""
    if isinstance(error, dict):
        value = error.get("error", error)
        if isinstance(value, dict):
            return str(value.get("code") or "").strip().lower() in _DUPLICATE_CODES
        return str(value or "").strip().lower() in _DUPLICATE_CODES
    text = str(error or "").strip().lower()
    if text in _DUPLICATE_CODES:
        return True
    codes = re.findall(r'["\']code["\']\s*:\s*["\']([a-z_]+)["\']', text)
    codes.extend(re.findall(r"(?:set_password_failed|create_account_failed):\s*([a-z_]+)(?:\s|$)", text))
    return any(code in _DUPLICATE_CODES for code in codes)


def reject_registered_mailbox(mailbox: dict[str, Any], detail: str = "") -> None:
    """Mark the terminal outcome before the existing owner finalizes the lease."""
    mailbox["_registration_outcome"] = RegisteredEmailDetected.code
    raise RegisteredEmailDetected(detail, email=str(mailbox.get("address") or ""))

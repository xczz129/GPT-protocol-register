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


class PasswordlessAccountDetected(RuntimeError):
    """Legacy outcome kept for import compatibility, never permission to convert.

    An OTP route does not prove that an identity lacks a password or belongs
    to this task. Registration callers must stop instead of resetting it.
    """

    code = "passwordless_existing_account"

    def __init__(self, detail: str = "", *, email: str = "") -> None:
        self.email = str(email or "").strip()
        suffix = f": {str(detail).strip()[:160]}" if detail else ""
        super().__init__(f"{self.code}{suffix}")


def requires_fresh_registration(mailbox: dict[str, Any] | None) -> bool:
    return normalize_provider_type(str((mailbox or {}).get("provider") or "")) in {
        "smsbower_mail",
        "online_disposablemail",
    }


def is_password_login_route(url: str = "", page_type: str = "") -> bool:
    """True when the auth step is the existing-account password login page.

    This describes a requested authentication step, not account ownership.
    MFA and forced-reset routes are separate authentication steps.
    """
    normalized_page = str(page_type or "").strip().lower().replace("-", "_")
    if normalized_page in {"password_verification", "login_password"}:
        return True
    try:
        path = unquote(urlsplit(str(url or "")).path).rstrip("/").lower()
    except (TypeError, ValueError):
        return False
    return path in {"/log-in/password", "/login/password", "/sign-in/password"}


_EXISTING_PATHS = frozenset({
    "/log-in/password", "/login/password", "/sign-in/password",
    "/reset-password", "/reset-password/new-password", "/mfa-challenge",
})
_EXISTING_PAGE_TYPES = frozenset({
    "password_verification", "login_password", "mfa_challenge",
    "reset_password", "reset_password_new_password",
})
_SIGNUP_PATHS = frozenset({
    "/create-account/password", "/signup/password", "/sign-up/password",
    "/auth/username_password",
})
_SIGNUP_PAGE_TYPES = frozenset({
    "create_account_password", "signup_password", "username_password_create",
})
_OTP_PATHS = frozenset({"/email-verification", "/verify-email", "/email-otp"})
_OTP_PAGE_TYPES = frozenset({"email_otp_verification", "email_verification", "email_otp"})
_EMAIL_VERIFICATION_MODES = frozenset({
    "passwordless_login", "password_reset", "passwordless_signup", "onboarding",
})
_LOGIN_METHODS = frozenset({"password", "passwordless_otp"})


def auth_identity_evidence(auth_response: Any) -> dict[str, Any]:
    """Read identity-step metadata using only fixed labels safe for diagnostics.

    Login methods describe available authentication choices. They never prove
    ownership or authorize a registration, password reset, or OTP consumption.
    """
    result: dict[str, Any] = {
        "email_verification_mode": "missing",
        "login_methods": [],
        "auth_response_valid": True,
    }
    if auth_response is None:
        return result
    if not isinstance(auth_response, dict):
        result["auth_response_valid"] = False
        return result
    page = auth_response.get("page", {})
    if not isinstance(page, dict):
        result["auth_response_valid"] = False
        return result
    payload = page.get("payload", {})
    if not isinstance(payload, dict):
        result["auth_response_valid"] = False
        return result
    if auth_response.get("error") or page.get("error") or payload.get("error"):
        result["auth_response_valid"] = False
    if "type" in page and not isinstance(page["type"], str):
        result["auth_response_valid"] = False
    for source, keys in ((auth_response, ("continue_url", "final_url")),
                         (payload, ("url", "continue_url"))):
        if any(_trusted_auth_path(source[key]) is None for key in keys if key in source):
            result["auth_response_valid"] = False

    modes: set[str] = set()
    method_sets: list[set[str]] = []
    for source in (auth_response, payload):
        if "email_verification_mode" in source:
            value = source["email_verification_mode"]
            mode = value.strip().lower() if isinstance(value, str) else ""
            if mode not in _EMAIL_VERIFICATION_MODES:
                mode = "unrecognized"
                result["auth_response_valid"] = False
            modes.add(mode)
        if "login_methods" in source:
            values = source["login_methods"]
            methods: set[str] = set()
            if not isinstance(values, list):
                methods.add("unrecognized")
                result["auth_response_valid"] = False
            else:
                for item in values:
                    value = item.get("type") if isinstance(item, dict) else None
                    method = value.strip().lower() if isinstance(value, str) else ""
                    if method not in _LOGIN_METHODS:
                        method = "unrecognized"
                        result["auth_response_valid"] = False
                    methods.add(method)
            method_sets.append(methods)
    if len(modes) > 1:
        result["email_verification_mode"] = "conflicting"
        result["auth_response_valid"] = False
    elif modes:
        result["email_verification_mode"] = next(iter(modes))
    if method_sets:
        result["login_methods"] = sorted(set.union(*method_sets))
        if any(methods != method_sets[0] for methods in method_sets[1:]):
            result["auth_response_valid"] = False
    return result


def _trusted_auth_path(url: Any) -> str | None:
    """Return only an auth-origin path; None means the supplied URL is unsafe."""
    if not isinstance(url, str):
        return None
    if "\\" in url or any(ord(character) < 32 for character in url):
        return None
    try:
        value = url.strip()
        parsed = urlsplit(value)
        if parsed.fragment or parsed.username is not None or parsed.password is not None:
            return None
        if parsed.netloc:
            if parsed.hostname != "auth.openai.com" or parsed.scheme != "https" or parsed.port not in (None, 443):
                return None
        elif parsed.scheme or (value and not value.startswith("/")):
            return None
        path = unquote(parsed.path).rstrip("/").lower()
        if path.startswith("//") or "\\" in path or any(ord(character) < 32 for character in path):
            return None
        return path
    except (TypeError, ValueError):
        return None


def registration_auth_state(
    url: str = "", page_type: str = "", *, auth_response: Any = None,
) -> str:
    """Classify registration eligibility without inferring credentials or ownership.

    Existing-account authentication stops registration. An email OTP page is
    ambiguous without an explicit mode, and never grants permission to convert
    an existing account. Contradictory or malformed evidence fails closed.
    """
    evidence = auth_identity_evidence(auth_response)
    if not evidence["auth_response_valid"] or not isinstance(page_type, str):
        return ""
    urls: list[Any] = [url]
    page_types = [page_type]
    if isinstance(auth_response, dict):
        page = auth_response.get("page", {})
        payload = page.get("payload", {})
        if "type" in page:
            page_types.append(page["type"])
        for source, keys in ((auth_response, ("continue_url", "final_url")),
                             (payload, ("url", "continue_url"))):
            urls.extend(source[key] for key in keys if key in source)
    paths = [_trusted_auth_path(value) for value in urls]
    if any(path is None for path in paths):
        return ""
    pages = {value.strip().lower().replace("-", "_") for value in page_types}
    existing = any(path in _EXISTING_PATHS for path in paths) or bool(pages & _EXISTING_PAGE_TYPES)
    signup = any(path in _SIGNUP_PATHS for path in paths) or bool(pages & _SIGNUP_PAGE_TYPES)
    otp = any(path in _OTP_PATHS for path in paths) or bool(pages & _OTP_PAGE_TYPES)
    mode = evidence["email_verification_mode"]
    if (existing and signup) or (otp and (existing or signup)):
        return ""
    if mode in {"passwordless_login", "password_reset"}:
        return "" if signup else "existing"
    if mode == "passwordless_signup" and existing:
        return ""
    if existing:
        return "existing"
    return "signup" if signup else ""


def registration_creation_confirmed(auth_response: Any) -> bool:
    """Check the successful user/register response before claiming ownership.

    The caller must already have verified the HTTP success status. A known
    next registration step is required; a login intent, contradictory step,
    arbitrary object, or unsafe continuation must not create an ownership flag.
    This is a response check, not permission to execute any of these steps.
    """
    if not isinstance(auth_response, dict) or not auth_response:
        return False
    evidence = auth_identity_evidence(auth_response)
    if not evidence["auth_response_valid"] or evidence["email_verification_mode"] in {
        "passwordless_login", "password_reset",
    }:
        return False
    if "success" in auth_response and auth_response["success"] is not True:
        return False
    page = auth_response.get("page", {})
    payload = page.get("payload", {})
    urls = [
        source[key]
        for source, keys in ((auth_response, ("continue_url", "final_url")),
                             (payload, ("url", "continue_url")))
        for key in keys if key in source
    ]
    paths = [_trusted_auth_path(value) for value in urls]
    if any(path is None for path in paths):
        return False
    page_type = str(page.get("type") or "").strip().lower().replace("-", "_")
    steps: set[str] = set()
    for path in paths:
        if not path:
            continue
        if path in _OTP_PATHS or path == "/api/accounts/email-otp/send":
            steps.add("email_verification")
        elif path == "/about-you":
            steps.add("profile")
        else:
            return False
    if page_type:
        if page_type in _OTP_PAGE_TYPES or page_type == "email_otp_send":
            steps.add("email_verification")
        elif page_type == "about_you":
            steps.add("profile")
        else:
            return False
    return len(steps) == 1


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

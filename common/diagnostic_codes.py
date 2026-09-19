"""Public symbolic error names, never OAuth authorization or verification codes."""

PUBLIC_ERROR_CODES = frozenset({
    "invalid_workspace_selected", "invalid_auth_step", "rate_limit_exceeded",
    "max_check_attempts", "incorrect_code", "account_deactivated", "account_disabled",
    "token_expired", "token_invalidated", "session_expired", "session_invalidated",
    "invalid_token", "invalid_grant", "access_denied", "unauthorized",
    "invalid_request_error", "invalid_credentials", "incorrect_password",
})


def public_error_code(key, value) -> bool:
    return str(key).lower() == "code" and isinstance(value, str) and value in PUBLIC_ERROR_CODES

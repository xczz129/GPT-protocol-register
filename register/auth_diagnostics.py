"""Describe authentication failures without logging response bodies or credentials.

The summaries are diagnostic only. They never select or allow an auth step.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


_PAGE_TYPES = frozenset({
    'create_account_password', 'signup_password', 'username_password_create',
    'password_verification', 'login_password', 'mfa_challenge', 'reset_password',
    'reset_password_new_password', 'email_otp_verification', 'email_verification',
    'email_otp', 'login', 'signup', 'consent', 'about_you', 'add_phone',
    'captcha', 'turnstile', 'challenge', 'error',
})
_PATHS = frozenset({
    '/create-account/password', '/signup/password', '/sign-up/password',
    '/auth/username_password', '/log-in/password', '/login/password',
    '/sign-in/password', '/reset-password', '/reset-password/new-password',
    '/mfa-challenge', '/email-verification', '/email-otp', '/verify-email',
    '/log-in', '/login', '/signup', '/sign-up', '/create-account',
    '/about-you', '/add-phone', '/consent', '/error',
})
_ERROR_CODES = frozenset({
    'invalid_state', 'invalid_auth_step', 'signin_session_invalid',
    'invalid_request_error', 'invalid_request', 'access_denied',
    'rate_limit_exceeded', 'unsupported_country', 'captcha_required',
    'challenge_required', 'verification_required', 'account_deactivated',
    'user_already_exists', 'username_already_exists', 'email_already_exists',
    'account_already_exists', 'email_already_registered', 'account_already_registered',
})


def _label(value: Any, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        return 'missing'
    normalized = value.strip().lower().replace('-', '_')
    return normalized if normalized in allowed else 'unrecognized'


def read_auth_response(response: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Preserve the old object-or-empty-dict parsing while recording its reason."""
    status = getattr(response, 'status_code', None)
    metadata: dict[str, Any] = {'http_status': status if type(status) is int and 100 <= status <= 599 else 'unknown'}
    try:
        content_type = response.headers.get('content-type', '')
        media_type = content_type.split(';', 1)[0].strip().lower() if isinstance(content_type, str) else ''
    except Exception:
        media_type = ''
    metadata['content_type'] = media_type if media_type in {'application/json', 'application/problem+json', 'text/html', 'text/plain'} else ('other' if media_type else 'missing')
    try:
        value = response.json()
    except Exception:
        metadata['response_kind'] = 'not_json'
        return {}, metadata
    if not isinstance(value, dict):
        metadata['response_kind'] = 'json_non_object'
        return {}, metadata
    metadata['response_kind'] = 'json_object' if value else 'json_empty_object'
    error = value.get('error')
    metadata['has_api_error'] = bool(error)
    if isinstance(error, dict):
        metadata['api_error_code'] = _label(error.get('code'), _ERROR_CODES)
        metadata['api_error_type'] = _label(error.get('type'), _ERROR_CODES)
    elif error:
        metadata['api_error_code'] = _label(error, _ERROR_CODES)
    return value, metadata


def auth_route_diagnostic(url: Any, page_type: Any) -> dict[str, str]:
    """Only fixed route/type labels can be emitted; queries and bodies are omitted."""
    page = _label(page_type, _PAGE_TYPES)
    route = 'missing'
    if isinstance(url, str) and url.strip():
        try:
            parsed = urlsplit(url)
            if parsed.username or parsed.password or parsed.fragment or (parsed.netloc and (parsed.scheme != 'https' or parsed.hostname != 'auth.openai.com')):
                route = 'untrusted_or_opaque'
            else:
                path = parsed.path.rstrip('/').lower()
                route = path if path in _PATHS else 'unrecognized'
        except ValueError:
            route = 'malformed'
    return {'page_type': page, 'route': route}

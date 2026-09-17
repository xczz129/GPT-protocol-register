"""Passively read initial auth JSON or the observed server-rendered loader.

Only a fixed inline JSON string and selected reference paths are read. No
JavaScript is executed, no request is made, and private identity fields never
leave the comparison scope. This module does not authorize any auth action.
"""
from __future__ import annotations

from html.parser import HTMLParser
import json
import re
from typing import Any
from urllib.parse import urlsplit

from register.auth_diagnostics import read_auth_response


_LAYOUT = 'routes/layouts/client-auth-session-layout/layout'
_PATHS = frozenset({'/email-verification', '/log-in'})
_MODES = frozenset({'passwordless_login', 'password_reset', 'passwordless_signup', 'onboarding'})
_METHODS = frozenset({'password', 'passwordless_otp'})
_MAX_HTML_CHARS = 2 * 1024 * 1024
_MAX_CHUNK_CHARS = 256 * 1024
_MAX_TABLE_ITEMS = 4096
_MAX_VISITS = 512
_MAX_DEPTH = 32
_MAX_SCRIPTS = 256
_MAX_CHUNKS = 32
_PREFIX = 'window.__reactRouterContext.streamController.enqueue'
_CALL_START = re.compile(r'\s*window\.__reactRouterContext\.streamController\.enqueue\s*\(\s*')
_KEY = re.compile(r'_(?:0|[1-9][0-9]{0,5})\Z')
_MISSING = object()


class _Rejected(ValueError):
    """Contains only a fixed diagnostic label, never source text."""


def _reject(label: str) -> None:
    raise _Rejected(label)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _reject('html_stream_malformed')
        result[key] = value
    return result


class _InlineStreams(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.chunks: list[str] = []
        self.script_count = 0
        self.current: list[str] | None = None
        self.eligible = False
        self.inert_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {'template', 'noscript', 'textarea', 'title', 'xmp'}:
            self.inert_depth += 1
        if tag != 'script':
            return
        self.script_count += 1
        if self.script_count > _MAX_SCRIPTS:
            _reject('html_limit_exceeded')
        values = dict(attrs)
        self.eligible = self.inert_depth == 0 and not any(key == 'src' for key, _ in attrs) and (values.get('type') or '').strip().lower() in {'', 'text/javascript', 'application/javascript'}
        self.current = []

    def handle_data(self, data: str) -> None:
        if self.current is not None and self.eligible:
            self.current.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {'template', 'noscript', 'textarea', 'title', 'xmp'}:
            self.inert_depth = max(0, self.inert_depth - 1)
        if tag != 'script' or self.current is None:
            return
        if self.eligible:
            self._collect(''.join(self.current))
        self.current = None
        self.eligible = False

    def _collect(self, script: str) -> None:
        if not script.lstrip().startswith(_PREFIX):
            return
        if len(script) > _MAX_CHUNK_CHARS:
            _reject('html_limit_exceeded')
        match = _CALL_START.match(script)
        if match is None:
            _reject('html_stream_malformed')
        try:
            chunk, end = json.JSONDecoder().raw_decode(script, match.end())
        except (ValueError, RecursionError):
            _reject('html_stream_malformed')
        # Decode the literal once, then check the suffix separately. Avoid
        # overlapping regex repetitions on potentially malformed page text.
        if not isinstance(chunk, str) or re.fullmatch(r'\)\s*;?', script[end:].strip()) is None:
            _reject('html_stream_malformed')
        if len(chunk) > _MAX_CHUNK_CHARS or len(self.chunks) >= _MAX_CHUNKS:
            _reject('html_limit_exceeded')
        self.chunks.append(chunk)

    def finish(self) -> None:
        self.close()
        if self.current is not None and self.eligible:
            pending = ''.join(self.current) + self.rawdata
            if pending.lstrip().startswith(_PREFIX):
                _reject('html_stream_malformed')


class _References:
    """Resolve only explicit target fields, not unrelated session contents."""

    def __init__(self, table: Any) -> None:
        if not isinstance(table, list) or not table:
            _reject('html_stream_malformed')
        if len(table) > _MAX_TABLE_ITEMS:
            _reject('html_limit_exceeded')
        self.table = table
        self.visits = 0

    def node(self, ref: Any, ancestors: tuple[int, ...] = ()) -> '_Node':
        self.visits += 1
        if self.visits > _MAX_VISITS or len(ancestors) >= _MAX_DEPTH:
            _reject('html_limit_exceeded')
        if type(ref) is not int or ref < 0 or ref >= len(self.table):
            _reject('html_reference_invalid')
        if ref in ancestors:
            _reject('html_reference_cycle')
        return _Node(self, self.table[ref], (*ancestors, ref))


class _Node:
    def __init__(self, references: _References, value: Any, path: tuple[int, ...]) -> None:
        self.references = references
        self.value = value
        self.path = path

    def mapping(self) -> dict[str, Any]:
        if not isinstance(self.value, dict):
            _reject('html_state_malformed')
        result: dict[str, Any] = {}
        for key, ref in self.value.items():
            if not isinstance(key, str) or _KEY.fullmatch(key) is None:
                _reject('html_reference_invalid')
            name = self.references.node(int(key[1:]), self.path).value
            if not isinstance(name, str) or name in result:
                _reject('html_reference_invalid')
            result[name] = ref
        return result

    def child(self, key: str, *, optional: bool = False, nullable: bool = False) -> '_Node | None':
        mapping = self.mapping()
        if key not in mapping:
            if optional:
                return None
            _reject('html_session_missing')
        # The observed stream uses -5 for null. It is permitted only at
        # explicitly nullable fields, never as a Python array subscript.
        if nullable and type(mapping[key]) is int and mapping[key] == -5:
            return None
        node = self.references.node(mapping[key], self.path)
        return None if nullable and node.value is None else node

    def children(self) -> list['_Node']:
        if not isinstance(self.value, list) or len(self.value) > 16:
            _reject('html_state_malformed')
        return [self.references.node(ref, self.path) for ref in self.value]


def _email(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 320:
        _reject('html_identity_malformed')
    normalized = value.strip().casefold()
    if normalized.count('@') != 1 or any(char.isspace() or ord(char) < 32 for char in normalized):
        _reject('html_identity_malformed')
    local, domain = normalized.split('@')
    if not local or not domain:
        _reject('html_identity_malformed')
    # In particular, do not strip +aliases or Gmail dots.
    return normalized


def _snapshot(session: _Node) -> dict[str, Any]:
    session.mapping()
    mode_node = session.child('email_verification_mode', optional=True)
    mode = _MISSING if mode_node is None else mode_node.value
    if mode is not _MISSING and (not isinstance(mode, str) or mode not in _MODES):
        _reject('html_mode_unsupported')
    identities: dict[str, str] = {}
    email_node = session.child('email', optional=True)
    if email_node is not None:
        identities['email'] = _email(email_node.value)
    username = session.child('username', optional=True)
    if username is not None:
        if isinstance(username.value, str):
            identities['username'] = _email(username.value)
        else:
            kind = username.child('kind')
            value = username.child('value')
            if kind.value != 'email':
                _reject('html_identity_malformed')
            identities['username'] = _email(value.value)
    if len(set(identities.values())) > 1:
        _reject('html_identity_conflict')
    methods_node = session.child('login_methods', optional=True)
    methods: Any = _MISSING
    if methods_node is not None:
        method_values = []
        for item in methods_node.children():
            method = item.child('type').value
            if not isinstance(method, str) or method not in _METHODS:
                _reject('html_methods_unsupported')
            method_values.append(method)
        methods = tuple(sorted(set(method_values)))
    return {'mode': mode, 'identities': identities, 'methods': methods}


def _stream_state(chunk: str) -> dict[str, Any] | None:
    try:
        table = json.loads(chunk, object_pairs_hook=_unique_object)
    except _Rejected:
        raise
    except (ValueError, RecursionError):
        _reject('html_stream_malformed')
    root = _References(table).node(0)
    loader = root.child('loaderData', optional=True)
    if loader is None:
        return None
    layout = loader.child(_LAYOUT, optional=True)
    if layout is None:
        return None
    if root.child('errors', optional=True, nullable=True) is not None:
        _reject('html_route_error')
    session = layout.child('session')
    missing = session.child('is_missing_session', optional=True)
    if missing is not None and missing.value is not False:
        _reject('html_session_missing')
    state = _snapshot(session)
    if state['mode'] is not _MISSING and missing is None:
        _reject('html_session_missing')
    seed = layout.child('seedCacheEntry', optional=True, nullable=True)
    if seed is not None:
        seed_session = seed.child('session', optional=True, nullable=True)
        if seed_session is not None:
            other = _snapshot(seed_session)
            if state['mode'] != other['mode'] or state['identities'] != other['identities']:
                _reject('html_seed_conflict')
            if state['methods'] is not _MISSING and other['methods'] is not _MISSING and state['methods'] != other['methods']:
                _reject('html_seed_conflict')
    return state


def _trusted_path(url: Any) -> str | None:
    if not isinstance(url, str) or '\\' in url or any(ord(char) < 32 for char in url):
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname != 'auth.openai.com' or parsed.port not in (None, 443):
            return None
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            return None
        return parsed.path
    except ValueError:
        return None


def read_initial_auth_response(response: Any, *, expected_email: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Preserve JSON responses or extract a narrowly validated HTML auth mode."""
    data, metadata = read_auth_response(response)
    kind = metadata['response_kind']
    metadata.update(auth_state_source='none', auth_payload_parsed=False,
                    initial_auth_parse_status=kind, initial_auth_state_rejected=False)
    if kind != 'not_json':
        if kind in {'json_object', 'json_empty_object'}:
            metadata['auth_state_source'] = 'json'
            metadata['auth_payload_parsed'] = kind == 'json_object'
        elif kind == 'json_non_object':
            metadata['initial_auth_state_rejected'] = True
        return data, metadata
    if metadata['content_type'] != 'text/html':
        metadata['initial_auth_parse_status'] = 'unsupported_content_type'
        return {}, metadata
    status = metadata['http_status']
    if type(status) is not int or not 200 <= status < 300:
        metadata['initial_auth_parse_status'] = 'http_not_successful'
        return {}, metadata
    path = _trusted_path(getattr(response, 'url', None))
    if path not in _PATHS:
        metadata['initial_auth_parse_status'] = 'html_untrusted_url' if path is None else 'html_unsupported_route'
        return {}, metadata
    try:
        body = response.text
    except Exception:
        body = None
    if not isinstance(body, str):
        metadata['initial_auth_parse_status'] = 'html_body_unavailable'
        return {}, metadata
    try:
        if len(body) > _MAX_HTML_CHARS:
            _reject('html_limit_exceeded')
        parser = _InlineStreams()
        parser.feed(body)
        parser.finish()
        states = [state for chunk in parser.chunks if (state := _stream_state(chunk)) is not None]
        if not states:
            metadata['initial_auth_parse_status'] = 'html_loader_missing'
            return {}, metadata
        state = states[0]
        if any(other != state for other in states[1:]):
            _reject('html_state_conflict')
        if state['mode'] is _MISSING:
            if path == '/log-in' and not state['identities']:
                metadata.update(auth_state_source='html_loader', initial_auth_parse_status='html_identifier_state')
                return {}, metadata
            _reject('html_mode_missing')
        if not state['identities']:
            _reject('html_identity_missing')
        if not isinstance(expected_email, str) or not expected_email.strip():
            _reject('html_expected_identity_missing')
        expected = _email(expected_email)
        if any(value != expected for value in state['identities'].values()):
            _reject('html_identity_mismatch')
        result: dict[str, Any] = {'email_verification_mode': state['mode']}
        if state['methods'] is not _MISSING:
            result['login_methods'] = [{'type': value} for value in state['methods']]
        metadata.update(auth_state_source='html_loader', auth_payload_parsed=True,
                        initial_auth_parse_status='html_loader_parsed')
        return result, metadata
    except _Rejected as exc:
        metadata.update(initial_auth_parse_status=str(exc), initial_auth_state_rejected=True)
        return {}, metadata
    except (ValueError, RecursionError):
        metadata.update(initial_auth_parse_status='html_stream_malformed', initial_auth_state_rejected=True)
        return {}, metadata

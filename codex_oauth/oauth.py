from __future__ import annotations

import base64, binascii, hashlib, json, os, re, secrets, tempfile, threading, time, uuid
import hmac, struct
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse, parse_qs, parse_qsl, urljoin, unquote
from datetime import datetime, timezone

from curl_cffi.requests import Session
from .sentinel import generate_requirements_token, build_sentinel_request_body
from .sentinel_runner import generate_sentinel_token
from .sentinel_config import USER_AGENT

AUTH_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPE = "openid email profile offline_access"

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def create_pkce() -> tuple[str, str]:
    verifier = _b64(secrets.token_bytes(64))
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge

def totp_from_secret(secret: str, at: int | None = None) -> str:
    """按 RFC 6238 从 Base32 2FA 密钥生成 6 位 TOTP。"""
    normalized = re.sub(r"[\s-]+", "", str(secret or "")).upper()
    if not normalized:
        raise ValueError("2FA secret is empty")
    try:
        key = base64.b32decode(normalized + "=" * (-len(normalized) % 8), casefold=True)
    except Exception as exc:
        raise ValueError("2FA secret is not valid Base32") from exc
    counter = int((at if at is not None else time.time()) // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"

def authorization_url(state: str, challenge: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": CLIENT_ID, "response_type": "code",
        "redirect_uri": REDIRECT_URI, "scope": SCOPE, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "prompt": "login", "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    })

class _Callback(BaseHTTPRequestHandler):
    result: dict[str, str] = {}
    expected_state = ""
    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        if q.get("state", [""])[0] != self.expected_state:
            self.result["error"] = "state_mismatch"
        elif q.get("error", [""])[0]:
            self.result["error"] = q["error"][0]
        else:
            self.result["code"] = q.get("code", [""])[0]
        body = b"Authorization received. You can close this window."
        self.send_response(200); self.send_header("Content-Type", "text/plain"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *_): pass

def wait_for_callback(state: str, timeout: int = 300) -> str:
    _Callback.result = {}; _Callback.expected_state = state
    server = HTTPServer(("127.0.0.1", 1455), _Callback)
    thread = threading.Thread(target=server.handle_request, daemon=True); thread.start()
    thread.join(timeout); server.server_close()
    if thread.is_alive(): raise TimeoutError("OAuth callback timed out")
    if _Callback.result.get("error"): raise RuntimeError(_Callback.result["error"])
    code = _Callback.result.get("code")
    if not code: raise RuntimeError("callback did not contain an authorization code")
    return code

def exchange_code(code: str, verifier: str, proxy: str = "") -> dict:
    session = Session(impersonate="chrome")
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    try:
        response = session.post(TOKEN_URL, data={
            "grant_type": "authorization_code", "client_id": CLIENT_ID,
            "code": code, "redirect_uri": REDIRECT_URI, "code_verifier": verifier,
        }, timeout=30)
        response.raise_for_status()
        return response.json()
    finally:
        session.close()


def decode_jwt_payload(value: str) -> dict:
    """Decode an OAuth JWT payload for non-sensitive account metadata.

    Signature verification is intentionally left to the upstream token
    endpoint; this helper only extracts the account id/email used for naming a
    local credential file.
    """
    if not isinstance(value, str) or value.count(".") < 1:
        return {}
    try:
        part = value.split(".", 2)[1]
        raw = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError, binascii.Error):
        return {}


def _claim_value(claims: dict, *keys: str) -> str:
    for key in keys:
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    auth = claims.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        for key in keys:
            value = auth.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def token_metadata(token: dict, email: str = "") -> dict:
    """Build the local CPA-compatible auth record from an OAuth response."""
    if not isinstance(token, dict):
        raise ValueError("OAuth token response is not an object")
    id_token = str(token.get("id_token") or "")
    access_token = str(token.get("access_token") or "")
    claims = decode_jwt_payload(id_token) or decode_jwt_payload(access_token)
    auth = claims.get("https://api.openai.com/auth") if isinstance(claims, dict) else {}
    account_id = _claim_value(claims, "chatgpt_account_id", "account_id", "accountId")
    if not account_id and isinstance(auth, dict):
        account_id = _claim_value(auth, "chatgpt_account_id", "account_id", "accountId")
    if not account_id:
        organizations = claims.get("organizations") if isinstance(claims, dict) else None
        if isinstance(organizations, list) and organizations and isinstance(organizations[0], dict):
            account_id = str(organizations[0].get("id") or "").strip()
    claim_email = _claim_value(claims, "email", "preferred_username", "username")
    try:
        expires_in = int(float(token.get("expires_in", 0) or 0))
    except (TypeError, ValueError):
        expires_in = 0
    raw_expiry = token.get("expires_at")
    try:
        supplied_expiry = int(float(raw_expiry)) if raw_expiry else 0
    except (TypeError, ValueError):
        supplied_expiry = 0
    if expires_in > 0:
        expires_at = int(time.time()) + expires_in
    elif supplied_expiry:
        expires_at = supplied_expiry
    elif isinstance(claims.get("exp"), (int, float)):
        expires_at = int(claims.get("exp") or 0)
    else:
        expires_at = 0
    return {
        "type": "codex",
        "provider": "codex",
        "email": (email or claim_email).strip(),
        "account_id": account_id,
        "access_token": access_token,
        "refresh_token": str(token.get("refresh_token") or ""),
        "id_token": id_token,
        "token_type": str(token.get("token_type") or "Bearer"),
        "scope": str(token.get("scope") or SCOPE),
        "expires_in": expires_in,
        "expires_at": expires_at,
        "expired": datetime.fromtimestamp(expires_at, timezone.utc).isoformat() if expires_at else "",
        "last_refresh": datetime.now(timezone.utc).isoformat(),
        "plan_type": _claim_value(claims, "chatgpt_plan_type", "plan_type", "planType") or "unknown",
        "disabled": False,
        "created_at": int(time.time()),
    }


def _atomic_private_json(payload: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def save_auth_record(token: dict, email: str, path: Path) -> dict:
    """Persist a token in the local auth-file shape consumed by the dashboard."""
    record = token_metadata(token, email)
    if not record.get("access_token"):
        raise ValueError("OAuth token response did not contain access_token")
    _atomic_private_json(record, path)
    return record

class ProtocolClient:
    """纯 HTTP OAuth 流程客户端；不依赖浏览器自动化。"""
    def __init__(self, email: str, timeout: int = 30, proxy: str = ""):
        self.email, self.timeout = email, timeout
        self.session = Session(impersonate="chrome")
        self.proxy = str(proxy or "").strip()
        if self.proxy:
            self.session.proxies = {"http": self.proxy, "https": self.proxy}
        self.state = secrets.token_urlsafe(32)
        self.verifier, self.challenge = create_pkce()
        # The auth frontend uses a UUID-shaped oai-did and carries a stable
        # navigation id through the JSON request chain.  Keeping both in the
        # same session makes the pure-HTTP flow match the HAR transaction and
        # avoids losing the auth step after a redirect or phone retry.
        self.device_id = str(uuid.uuid4())
        self.navigation_id = str(uuid.uuid4())
        self.auth_session_data: dict = {}
        self.last_auth_response: dict = {}
        try:
            for domain in (".openai.com", "openai.com", ".auth.openai.com", "auth.openai.com"):
                self.session.cookies.set("oai-did", self.device_id, domain=domain)
        except Exception:
            # curl-cffi versions with a restricted cookie jar still accept the
            # device id through the explicit request header below.
            pass

    def _update_navigation(self, response: Any) -> None:
        try:
            value = response.headers.get("x-openai-document-navigation-id")
        except Exception:
            value = ""
        if value:
            self.navigation_id = str(value)

    def navigate(self, url: str, referer: str = "https://auth.openai.com/") -> Any:
        """模拟浏览器页面导航(GET)。

        真实浏览器在 mfa 通过后会先导航到 continue_url(如 add-phone 页面),
        该请求会刷新会话 cookie 与服务端状态机;2026-09 起 add-phone/send
        会校验这一步,缺少导航直接报 409 session invalid。
        """
        current = str(url or "").strip()
        if not current:
            return None
        for _ in range(6):
            response = self.session.get(
                current,
                allow_redirects=False,
                timeout=self.timeout,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "Referer": referer,
                    "Upgrade-Insecure-Requests": "1",
                    "sec-fetch-site": "same-origin",
                    "sec-fetch-mode": "navigate",
                    "sec-fetch-user": "?1",
                    "sec-fetch-dest": "document",
                    "x-openai-document-navigation-id": self.navigation_id,
                    "oai-device-id": self.device_id,
                },
            )
            self._update_navigation(response)
            status = int(response.status_code)
            if 300 <= status < 400:
                location = response.headers.get("location", "")
                if not location:
                    break
                referer, current = current, urljoin(current, location)
                continue
            if status in (403, 503) and self._is_edge_challenge(response):
                # 与 bootstrap 相同的 Cloudflare 兜底:按表单 POST 重放
                form = dict(parse_qsl(urlparse(current).query, keep_blank_values=True))
                response = self.session.post(
                    current,
                    data=form,
                    allow_redirects=False,
                    timeout=self.timeout,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Origin": "https://auth.openai.com",
                        "Referer": current,
                        "x-openai-document-navigation-id": self.navigation_id,
                        "oai-device-id": self.device_id,
                    },
                )
                self._update_navigation(response)
                status = int(response.status_code)
            if status >= 400:
                raise RuntimeError(f"HTTP {status} navigate {urlparse(current).path}: {self._response_detail(response)}")
            break
        return response

    @staticmethod
    def _response_detail(response: Any) -> str:
        try:
            return (response.text or "").replace("\n", " ")[:300]
        except Exception:
            return ""

    @classmethod
    def _is_edge_challenge(cls, response: Any) -> bool:
        if response is None or int(getattr(response, "status_code", 0) or 0) not in (403, 503):
            return False
        detail = cls._response_detail(response).lower()
        return any(marker in detail for marker in ("just a moment", "cloudflare", "cf-chl-"))

    def bootstrap(self):
        try:
            self.consent_probe()
        except Exception:
            # The probe is a navigation hint; the authorize request remains
            # authoritative and can establish the same cookies itself.
            pass
        current = authorization_url(self.state, self.challenge)
        referer = "https://chatgpt.com/"
        post_fallback_used = False
        for _ in range(12):
            headers = {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": referer,
                "Upgrade-Insecure-Requests": "1",
                "sec-fetch-site": (
                    "same-origin"
                    if referer and urlparse(referer).netloc == urlparse(current).netloc
                    else "none"
                ),
                "sec-fetch-mode": "navigate",
                "sec-fetch-user": "?1",
                "sec-fetch-dest": "document",
                "x-openai-document-navigation-id": self.navigation_id,
                "oai-device-id": self.device_id,
            }
            response = self.session.get(current, allow_redirects=False,
                                        timeout=self.timeout, headers=headers)
            self._update_navigation(response)
            status = int(response.status_code)
            if self._is_edge_challenge(response) and not post_fallback_used:
                # The auth edge occasionally answers the first navigation
                # with a Cloudflare interstitial.  The HAR shows the browser
                # replaying the same query as an x-www-form-urlencoded POST;
                # doing that here keeps the flow browser-free while allowing
                # the edge to set its clearance cookie.
                post_fallback_used = True
                form = dict(parse_qsl(urlparse(current).query, keep_blank_values=True))
                post_headers = dict(headers)
                post_headers.update({
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://auth.openai.com",
                    "Referer": current,
                })
                response = self.session.post(current, data=form, allow_redirects=False,
                                             timeout=self.timeout, headers=post_headers)
                self._update_navigation(response)
                status = int(response.status_code)
            if 300 <= status < 400:
                location = response.headers.get("location", "")
                if not location:
                    raise RuntimeError(f"OAuth authorize redirect missing: HTTP {status}")
                referer, current = current, urljoin(current, location)
                continue
            if status >= 400:
                raise RuntimeError(f"HTTP {status} /oauth/authorize: {self._response_detail(response)}")
            break
        else:
            raise RuntimeError("OAuth authorize redirect chain exceeded limit")
        try:
            self.device_id = self.session.cookies.get("oai-did", self.device_id) or self.device_id
        except Exception:
            pass

    def post_json(self, path: str, payload: dict):
        referers = {
            "/api/accounts/authorize/continue": "https://auth.openai.com/log-in-or-create-account",
            "/api/accounts/password/verify": "https://auth.openai.com/log-in/password",
            "/api/accounts/mfa/issue_challenge": "https://auth.openai.com/log-in/password",
            "/api/accounts/mfa/verify": "https://auth.openai.com/mfa-challenge",
            "/api/accounts/add-phone/send": "https://auth.openai.com/add-phone",
            "/api/accounts/phone-otp/validate": "https://auth.openai.com/phone-verification",
            "/api/accounts/workspace/select": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
        }
        headers = {"Referer": referers.get(path, "https://auth.openai.com/"),
                   "Accept": "application/json, text/plain, */*",
                   "Accept-Language": "zh-CN,zh;q=0.9",
                   "Origin": "https://auth.openai.com",
                   "User-Agent": USER_AGENT,
                   "x-access-flow-invocation-id": str(uuid.uuid4()),
                   "x-openai-document-navigation-id": self.navigation_id,
                   "oai-device-id": self.device_id,
                   "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors",
                   "sec-fetch-dest": "empty"}
        if path == "/api/accounts/authorize/continue":
            headers["openai-sentinel-token"] = self.sentinel_header("authorize_continue")
        elif path == "/api/accounts/password/verify":
            headers["openai-sentinel-token"] = self.sentinel_header("password_verify")
        elif path == "/api/accounts/phone-otp/validate":
            headers["openai-sentinel-token"] = self.sentinel_header("verify_phone_otp")
        # Do not let a previous step's navigation hint leak into a response
        # that did not contain JSON.  Callers use ``last_auth_response`` as a
        # compatibility fallback, so stale data here could make a phone-send
        # response look like a completed OTP step.
        self.last_auth_response = {}
        r = self.session.post("https://auth.openai.com" + path, json=payload,
                              allow_redirects=False, timeout=self.timeout,
                              headers=headers)
        self._update_navigation(r)
        if r.status_code >= 400:
            detail = self._response_detail(r)
            raise RuntimeError(f"HTTP {r.status_code} {path}: {detail}")
        # Recent auth responses carry a fresh, minimized session object in
        # JSON even when the cookie jar is not updated on every step.  Keep a
        # private copy so workspace/MFA resolution remains consistent across
        # phone retries and redirect transitions.
        try:
            data = r.json()
        except Exception:
            data = None
        if isinstance(data, dict):
            self.last_auth_response = data
            session_data = data.get("oai-client-auth-session")
            if isinstance(session_data, dict):
                self.auth_session_data = session_data
            error = data.get("error")
            if isinstance(error, dict):
                message = error.get("message") or error.get("code") or "上游返回认证错误"
                raise RuntimeError(f"{path}: {message}")
        return r

    def sentinel_header(self, flow: str) -> str:
        p = generate_requirements_token(self.device_id)
        body = build_sentinel_request_body(p, self.device_id, flow)
        r = self.session.post(
            "https://sentinel.openai.com/backend-api/sentinel/req",
            data=body,
            headers={
                "Content-Type": "text/plain;charset=UTF-8",
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Origin": "https://sentinel.openai.com",
                "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html",
                "User-Agent": USER_AGENT,
                "sec-fetch-site": "same-origin",
                "sec-fetch-mode": "cors",
                "sec-fetch-dest": "empty",
            },
            timeout=self.timeout,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Sentinel HTTP {r.status_code}: {(r.text or '')[:250]}")
        challenge = r.json()
        return generate_sentinel_token(challenge, flow, self.device_id)

    def submit_email(self):
        return self.post_json("/api/accounts/authorize/continue", {
            "username": {"kind": "email", "value": self.email},
            "screen_hint": "login_or_signup",
        })

    def verify_password(self, password: str):
        self.post_json("/api/accounts/password/verify", {"password": password})

    def issue_mfa_challenge(self, challenge_id: str):
        return self.post_json("/api/accounts/mfa/issue_challenge", {
            "id": challenge_id, "type": "totp", "force_fresh_challenge": False,
        })

    def verify_mfa(self, challenge_id: str, code: str):
        return self.post_json("/api/accounts/mfa/verify", {
            "id": challenge_id, "type": "totp", "code": code,
        })

    def send_phone(self, phone: str):
        value = "".join((phone or "").split())
        if value and not value.startswith("+"):
            value = "+" + value
        if not value or len(value) < 8:
            raise ValueError("手机号格式看起来不完整，应包含国家码")
        return self.post_json("/api/accounts/add-phone/send", {"phone_number": value, "channel": "sms"})

    def submit_phone_otp(self, code: str):
        self.post_json("/api/accounts/phone-otp/validate", {"code": code})

    def consent_probe(self) -> None:
        """Prime the consent route used by the current auth redirect chain."""
        response = self.session.get(
            "https://auth.openai.com/sign-in-with-chatgpt/codex/consent.data"
            "?_routes=SIGN_IN_WITH_CHATGPT_CODEX_CONSENT",
            allow_redirects=False,
            timeout=self.timeout,
            headers={
                "Accept": "text/x-script; charset=utf-8, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
                "User-Agent": USER_AGENT,
                "x-openai-document-navigation-id": self.navigation_id,
                "oai-device-id": self.device_id,
                "sec-fetch-site": "same-origin",
                "sec-fetch-mode": "navigate",
                "sec-fetch-dest": "document",
            },
        )
        self._update_navigation(response)
        if response.status_code >= 500:
            raise RuntimeError(f"consent probe HTTP {response.status_code}")

    @staticmethod
    def _workspace_label(item: dict) -> str:
        return str(
            item.get("name") or item.get("title") or item.get("label") or item.get("id") or ""
        )

    def _auth_workspaces(self) -> list[dict]:
        """Workspace entries from the auth session (payload first, cookie fallback)."""
        data = self.auth_session_data
        if not data:
            raw = self.session.cookies.get("oai-client-auth-session", "")
            if not raw: raise RuntimeError("oai-client-auth-session cookie missing")
            part = unquote(raw.split(".", 1)[0])
            data = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        workspaces = data.get("workspaces") or []
        if not workspaces:
            # No workspaces means the account has not finished a pending
            # step (typically add-phone verification) yet.
            raise RuntimeError("no workspaces in auth session (phone verification pending?)")
        return [item for item in workspaces if isinstance(item, dict) and item.get("id")]

    def workspace_id(self, workspace_name: str = "") -> str:
        """Resolve the workspace id, matching by name when one is given.

        Team accounts carry multiple workspaces; picking the first one
        silently lands the token on the wrong (personal) workspace.
        """
        workspaces = self._auth_workspaces()
        wanted = str(workspace_name or "").strip().lower()
        if wanted:
            for item in workspaces:
                if self._workspace_label(item).strip().lower() == wanted:
                    return str(item["id"])
            available = "、".join(self._workspace_label(item) for item in workspaces)
            raise RuntimeError(
                f"workspace_name_not_found: 未找到工作区「{workspace_name}」，可用工作区: {available[:200]}"
            )
        return str(workspaces[0]["id"])

    def mfa_id(self) -> str:
        page = self.last_auth_response.get("page") if isinstance(self.last_auth_response, dict) else None
        payload = page.get("payload") if isinstance(page, dict) else None
        if isinstance(payload, dict) and payload.get("factor_id"):
            return str(payload["factor_id"])
        if self.auth_session_data:
            factors = self.auth_session_data.get("mfa_challenge_factors") or self.auth_session_data.get("mfa_factors") or []
            for factor in factors:
                if factor.get("factor_type") == "totp" and factor.get("id"):
                    return str(factor["id"])
        raw = self.session.cookies.get("oai-client-auth-session", "")
        if not raw: raise RuntimeError("oai-client-auth-session cookie missing")
        part = unquote(raw.split(".", 1)[0])
        data = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        factors = data.get("mfa_challenge_factors") or data.get("mfa_factors") or []
        for factor in factors:
            if factor.get("factor_type") == "totp" and factor.get("id"):
                return factor["id"]
        raise RuntimeError("TOTP MFA factor not found in auth session")

    def select_workspace(self, workspace_name: str = "") -> str:
        r = self.post_json("/api/accounts/workspace/select", {"workspace_id": self.workspace_id(workspace_name)})
        location = r.headers.get("location", "")
        # 当前接口通常返回 JSON continue_url，而不是 Location 头。
        try:
            data = r.json()
        except Exception:
            data = {}
        if not location and isinstance(data, dict):
            location = data.get("continue_url", "")
            if not location:
                page = data.get("page") or {}
                payload = page.get("payload") if isinstance(page, dict) else {}
                location = payload.get("url", "") if isinstance(payload, dict) else ""
            if isinstance(data.get("oai-client-auth-session"), dict):
                self.auth_session_data = data["oai-client-auth-session"]
            # A few responses wrap the external URL one level deeper.
            if not location:
                for key in ("redirect_uri", "redirect_url", "url"):
                    candidate = data.get(key)
                    if isinstance(candidate, str) and candidate:
                        location = candidate
                        break
        for _ in range(15):
            location = unquote(str(location or "")).strip()
            parsed = urlparse(location)
            if parsed.scheme and parsed.netloc == urlparse(REDIRECT_URI).netloc and parsed.path == urlparse(REDIRECT_URI).path:
                return location
            if location.startswith("/"): location = "https://auth.openai.com" + location
            if not location: raise RuntimeError("workspace redirect missing")
            nxt = self.session.get(location, allow_redirects=False, timeout=self.timeout)
            self._update_navigation(nxt)
            location = nxt.headers.get("location", "")
            if not location:
                # curl-cffi exposes the effective URL even for a 200 response;
                # accepting it covers callback redirects returned through a
                # meta refresh without following an external page.
                effective = getattr(nxt, "url", "") or ""
                parsed_effective = urlparse(str(effective))
                if parsed_effective.path == urlparse(REDIRECT_URI).path:
                    location = str(effective)
        raise RuntimeError("redirect chain exceeded limit")

    def run(self, password: str, mfa_code: str, phone: str, phone_code: str) -> dict:
        self.bootstrap(); self.submit_email()
        self.verify_password(password)
        mfa_id = self.mfa_id()
        self.issue_mfa_challenge(mfa_id); self.verify_mfa(mfa_id, mfa_code)
        self.send_phone(phone); self.submit_phone_otp(phone_code)
        callback = self.select_workspace(); query = parse_qs(urlparse(callback).query)
        if query.get("state", [""])[0] != self.state: raise RuntimeError("state mismatch")
        return exchange_code(query["code"][0], self.verifier, proxy=self.proxy)

def save_token(token: dict, path: Path) -> None:
    # Keep the legacy CLI output shape, but apply the same private-file
    # permissions used by imported auth records.
    _atomic_private_json(token, path)

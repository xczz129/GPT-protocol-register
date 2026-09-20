# -*- coding: utf-8 -*-
"""Proxy helpers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote


# ---------------------------------------------------------------------------
# 错误信息中文化：面板/操作日志展示层统一把英文错误码翻译成中文。
# 规则按特异性从上到下匹配，首个命中生效；未命中原样返回。
# ---------------------------------------------------------------------------
_ERROR_ZH_RULES: tuple[tuple[str, str], ...] = (
    ("mfa_verify_http_403: account_deactivated", "账号已被停用（封号）"),
    ("account_deactivated", "账号已被停用（封号）"),
    ("invalid_username_or_password", "密码错误：存储密码与账号实际密码不符"),
    ("password_verify_http_401", "密码验证被拒：密码与账号不符"),
    ("mfa_verify_http_403", "TOTP验证码被拒：2FA密钥与账号不符"),
    ("incorrect_code", "验证码错误"),
    ("mfa_email_otp_login_unavailable", "已开启2FA：无邮箱验证码登录通道，无法自动恢复"),
    ("email_address_changed", "账号邮箱已被变更"),
    ("credential_login_password_route_unavailable", "服务端未提供密码登录入口（导向邮箱验证）"),
    ("mfa_verify_missing_callback", "TOTP校验后未取得登录回调"),
    ("chatgpt_csrf_http_403", "Cloudflare拦截：CSRF请求被质询"),
    ("clearance 重试仍被拦截", "Cloudflare拦截：清障后仍被拦截，需更换IP/代理"),
    ("cloudflare_challenge", "Cloudflare质询拦截"),
    ("team_workspace_unavailable", "团队工作区不可用：认证会话未提供工作区列表"),
    ("team_workspace_id_mismatch", "工作区ID不匹配：已保留原团队会话"),
    ("team_workspace_unverified", "团队工作区选择未完成"),
    ("重新登录后的凭证测活失败", "登录成功但凭证测活失败"),
    ("等待登录验证码超时", "邮箱验证码未到达（180秒超时）"),
    ("otp_not_delivered", "邮箱验证码未送达"),
    ("rate_limit", "触发服务端限流"),
    ("限流", "触发服务端限流"),
    ("no_email_input", "页面未找到邮箱输入框"),
    ("goto_failed", "页面加载失败"),
    ("stuck_on_verification", "卡在验证页"),
    ("stuck_on_login", "卡在登录页"),
    ("invalid_state", "认证状态已失效"),
    ("curl: (7)", "代理连接失败"),
    ("curl: (28)", "连接超时"),
    ("curl: (35)", "SSL/TLS连接失败"),
    ("curl: (56)", "连接被重置"),
    ("timed out", "请求超时"),
    ("timeout", "请求超时"),
)


def error_zh(error_text: object) -> str:
    """把常见英文错误码翻译成中文说明（附原始码便于排查）。

    面板与操作日志的展示层调用；日志/数据库中的完整原始错误不受影响。
    幂等：错误信息会经过 task_event → 调度器 → 持久化多层传递，已含
    中文翻译的文本直接原样返回，避免嵌套包装。
    """
    text = str(error_text or "").strip()
    if not text:
        return text
    # 已含任一中文译文的文本视为已翻译，不再二次包装
    for _needle, zh in _ERROR_ZH_RULES:
        if zh in text:
            return text
    lowered = text.lower()
    for needle, zh in _ERROR_ZH_RULES:
        if needle in text or needle.lower() in lowered:
            return f"{zh}（{text[:72]}）" if len(text) > 72 else f"{zh}（{text}）"
    return text


def _encode_proxy_credential(value: object) -> str:
    """Percent-encode proxy credentials for the URL userinfo component.

    代理商的地域参数会带空格（如 ``city-Bernau bei Berlin``），直接拼进
    ``http://user:pass@host`` 会让 curl 直接拒绝整个请求（错误 5:
    Unsupported proxy syntax）。先 unquote 再 quote：已编码的输入保持稳定
    （幂等），原始空格/非 ASCII 字符则被正确编码。
    """
    text = str(value or "")
    try:
        text = unquote(text)
    except Exception:
        pass
    return quote(text, safe="")


def normalize_proxy_url(url: str | None) -> str:
    url = (url or "").strip().strip("\ufeff").strip(",;")
    if not url:
        return ""
    if "://" not in url:
        if "##" in url:
            bits = [x.strip() for x in url.split("##")]
            if len(bits) >= 3 and ":" in bits[0]:
                host, port = bits[0].rsplit(":", 1)
                username, password = bits[1], bits[2]
                url = f"http://{_encode_proxy_credential(username)}:{_encode_proxy_credential(password)}@{host}:{port}"
        elif "@" in url:
            left, right = url.split("@", 1)
            if ":" in left and ":" in right:
                # 标准格式 user:pass@host:port 时右侧必须是合法 host:port；
                # 先按标准顺序解释，避免纯数字密码（如 user:123456@host:8080）
                # 被反向重组为 host:port@user:pass
                right_host, _, right_port = right.rpartition(":")
                if right_host and right_port.isdigit() and ":" not in right_host:
                    url = f"http://{left}@{right}"
                else:
                    host, port = left.rsplit(":", 1)
                    username, password = right.split(":", 1)
                    if port.isdigit():
                        url = f"http://{_encode_proxy_credential(username)}:{_encode_proxy_credential(password)}@{host}:{port}"
            if "://" not in url:
                url = "http://" + url
        else:
            parts = url.split(":")
            if len(parts) == 4 and parts[1].isdigit():
                host, port, username, password = parts
                url = f"http://{_encode_proxy_credential(username)}:{_encode_proxy_credential(password)}@{host}:{port}"
            elif len(parts) == 4 and parts[3].isdigit():
                username, password, host, port = parts
                url = f"http://{_encode_proxy_credential(username)}:{_encode_proxy_credential(password)}@{host}:{port}"
            else:
                url = "http://" + url
    if re.match(r"^socks://", url, re.IGNORECASE):
        url = "socks5h" + url[5:]
    elif re.match(r"^socks4a?://", url, re.IGNORECASE):
        url = re.sub(r"^socks4a?://", "socks4://", url, count=1, flags=re.IGNORECASE)
    # 直贴的完整 URL 若 userinfo 含原始空格/非 ASCII 同样会让 curl 报错 5，
    # 按相同规则修复。
    if "://" in url:
        scheme, rest = url.split("://", 1)
        userinfo, at, hostpart = rest.rpartition("@")
        if at and (" " in userinfo or any(ord(ch) > 127 for ch in userinfo)):
            user, _, password = userinfo.partition(":")
            url = (
                f"{scheme}://{_encode_proxy_credential(user)}:"
                f"{_encode_proxy_credential(password)}@{hostpart}"
            )
    return url


def load_proxy_pool(proxy_cfg: dict[str, Any]) -> list[str]:
    pool_file = str(proxy_cfg.get("pool_file") or "").strip()
    if not pool_file:
        return []
    path = Path(pool_file).expanduser()
    try:
        raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        print(f"  [proxy] pool file unavailable: {path} ({exc})")
        return []
    proxies: list[str] = []
    seen: set[str] = set()
    for line in raw_lines:
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        proxy = normalize_proxy_url(value)
        if proxy and proxy not in seen:
            seen.add(proxy)
            proxies.append(proxy)
    return proxies


def resolve_proxy_pool(proxy_cfg: dict[str, Any]) -> list[str]:
    """Resolve the configured outbound mode into the proxies tasks may use."""
    if not isinstance(proxy_cfg, dict) or not proxy_cfg.get("enabled"):
        return []
    fallback = normalize_proxy_url(str(proxy_cfg.get("url") or ""))
    mode = str(proxy_cfg.get("mode") or "single_proxy").strip().lower()
    if mode == "country_pool":
        from common.registration_proxy_pool import proxies_for_country

        proxies = proxies_for_country(str(proxy_cfg.get("selected_country") or ""))
        return proxies or ([fallback] if fallback else [])
    if mode != "pool":
        return [fallback] if fallback else []
    proxies = load_proxy_pool(proxy_cfg)
    return proxies or ([fallback] if fallback else [])


def compute_proxy_retry_limit(max_retries: int, proxy_pool: list) -> int:
    """Shared retry budget across distinct proxies (used by all schedulers).

    Always retries at least ``max_retries`` times, and up to 5 extra attempts
    when the pool has distinct proxies to rotate through.
    """
    distinct = len({str(p) for p in proxy_pool if str(p or "").strip()})
    return max(int(max_retries or 0), min(5, max(0, distinct - 1)))


def proxy_brief(proxy: str) -> str:
    value = str(proxy or "").strip()
    if not value:
        return "none"
    without_scheme = value.split("://", 1)[-1]
    if "@" in without_scheme:
        auth, host = without_scheme.rsplit("@", 1)
        username = auth.split(":", 1)[0] if auth else ""
        if len(username) > 22:
            username = f"{username[:14]}...{username[-6:]}"
        return f"{host} user={username}"
    return without_scheme


_RATE_LIMIT_ERROR_CODE = re.compile(
    r"(?<![a-z0-9])(?:rate_limit_exceeded|too_many_requests)(?![a-z0-9])",
    re.IGNORECASE,
)
_RATE_LIMIT_HTTP_STATUS = re.compile(
    r"(?<![a-z0-9])(?:"
    r"http(?:/\d+(?:\.\d+)?)?(?:[\s_-]+(?:error|status)(?:[\s_-]+code)?)?"
    r"|(?:response[\s_-]+)?status(?:[\s_-]+code)?"
    r"|response"
    r")[\s'\":=_([{<\-]*429(?![a-z0-9])"
    r"|(?<![a-z0-9])429[\s_:=-]+(?:client[\s_-]+error:?[\s_-]+)?"
    r"too[\s_-]+many[\s_-]+requests(?![a-z])",
    re.IGNORECASE,
)


def is_rate_limit_error(error: BaseException | str | None) -> bool:
    """Recognize an explicit rate-limit code or HTTP 429 response.

    A bare ``429`` is not sufficient: it can be a port, attempt count or ID.
    Preserve the classification when an HTTP client exposes the status only on
    its exception/response, or another layer wraps that exception.
    """
    seen: set[int] = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current)
        if _RATE_LIMIT_ERROR_CODE.search(message) or _RATE_LIMIT_HTTP_STATUS.search(message):
            return True
        if not isinstance(current, BaseException):
            break
        for source in (current, getattr(current, "response", None)):
            if source is None:
                continue
            for attribute in ("status_code", "status"):
                status = getattr(source, attribute, None)
                if isinstance(status, (int, str)) and str(status).strip() == "429":
                    return True
        current = current.__cause__ or (
            current.__context__ if not current.__suppress_context__ else None
        )
    return False


_AUTH_IDENTITY_ERROR_CODE = re.compile(
    r"(?:[\"']code[\"']\s*:\s*[\"']|(?:^|[\s:])(?:code\s*=\s*)?)"
    r"username_not_found(?=$|[\s\"',:;}])",
    re.IGNORECASE,
)


def is_auth_identity_error(error: BaseException | str | dict | None) -> bool:
    """Recognize an explicit missing-identity code, including wrapped responses."""
    seen: set[int] = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, dict):
            payload = current.get("error", current)
            if isinstance(payload, dict) and payload.get("code") == "username_not_found":
                return True
        if _AUTH_IDENTITY_ERROR_CODE.search(str(current)):
            return True
        if not isinstance(current, BaseException):
            break
        response = getattr(current, "response", None)
        if response is not None and callable(getattr(response, "json", None)):
            try:
                data = response.json()
            except Exception:
                data = None
            if isinstance(data, dict) and isinstance(data.get("error"), dict):
                if data["error"].get("code") == "username_not_found":
                    return True
        current = current.__cause__ or (
            current.__context__ if not current.__suppress_context__ else None
        )
    return False


def is_cloudflare_block_error(error: BaseException | str | None) -> bool:
    """Recognize a Cloudflare challenge block (managed/interstitial page).

    Used by schedulers for batch-level circuit breaking: a burst of these
    within a short window means the whole proxy pool's exit IPs are being
    rate-flagged, and hammering further only deepens the block.
    """
    message = str(error or "").lower()
    if not message:
        return False
    return any(
        marker in message
        for marker in (
            "被 cloudflare 拦截",
            "cf_blocked",
            "clearance 重试仍被拦截",
            "error_category=edge_blocked",
        )
    ) or bool(re.search(
        r"(?:[\"']?body_summary[\"']?\s*[:=]\s*[\"']?cloudflare_challenge"
        r"|[\"']?error_category[\"']?\s*[:=]\s*[\"']?edge_blocked)(?![a-z0-9_])",
        message,
    ))


_FORBIDDEN_HTTP_STATUS = re.compile(
    r"(?<![a-z0-9])(?:"
    r"http(?:/\d+(?:\.\d+)?)?(?:[\s_-]+(?:error|status)(?:[\s_-]+code)?)?"
    r"|(?:response[\s_-]+)?status(?:[\s_-]+code)?"
    r"|response"
    r")[\s'\":=_([{<\-]*403(?![a-z0-9])"
    r"|(?<![a-z0-9])403[\s_:=-]+(?:client[\s_-]+error:?[\s_-]+)?"
    r"forbidden(?![a-z])",
    re.IGNORECASE,
)
_PROXY_TUNNEL_REFUSAL = re.compile(
    r"connect tunnel failed|tunnel connection failed|proxy tunnel (?:failed|refused|rejected)",
    re.IGNORECASE,
)


def is_http_forbidden_error(error: BaseException | str | None) -> bool:
    """An explicit HTTP refusal is not a broken transport or a proxy failure."""
    seen: set[int] = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current)
        proxy_tunnel = bool(_PROXY_TUNNEL_REFUSAL.search(message))
        if not proxy_tunnel and _FORBIDDEN_HTTP_STATUS.search(message):
            return True
        if not isinstance(current, BaseException):
            break
        for source in (current, getattr(current, "response", None)):
            if source is None:
                continue
            for attribute in ("status_code", "status"):
                status = getattr(source, attribute, None)
                if not proxy_tunnel and isinstance(status, (int, str)) and str(status).strip() == "403":
                    return True
        current = current.__cause__ or (
            current.__context__ if not current.__suppress_context__ else None
        )
    return False


def is_registration_access_denied(error: BaseException | str | None) -> bool:
    """Stop upstream refusals without confusing a CONNECT proxy refusal with one."""
    if is_http_forbidden_error(error):
        return True
    seen: set[int] = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if is_cloudflare_block_error(current) or re.search(
            r"(?<![a-z0-9_])registration_access_denied(?![a-z0-9_])",
            str(current).lower(),
        ):
            return True
        if not isinstance(current, BaseException):
            break
        current = current.__cause__ or (
            current.__context__ if not current.__suppress_context__ else None
        )
    return False


def is_tls_transient_error(error: BaseException | str | None) -> bool:
    """Link-level TLS handshake transients that recover on the same session.

    代理链路偶发 curl: (35)/TLS connect error 属链路级瞬断,与指纹和凭据
    无关,原 session 立即重试几乎必恢复(参考项目实测 8/8 一次即恢复)。
    与代理不可达(connect 类)不同,后者才需要换代理。
    """
    message = str(error or "").lower()
    return any(
        marker in message
        for marker in (
            "curl: (35)",
            "tls connect error",
            "ssl error",
            "sslerror",
            "ssl connect",
            "ssl handshake",
            "tls handshake",
            "tlsv1_alert",
            "wrong_version_number",
            "unexpected eof while reading",
            "eof occurred in violation of protocol",
        )
    )


def is_proxy_transport_error(error: BaseException | str | None) -> bool:
    """Identify failures where retrying through another proxy is useful."""
    if (
        is_rate_limit_error(error) or is_auth_identity_error(error)
        or is_registration_access_denied(error)
    ):
        return False
    message = str(error or "").lower()
    if not message:
        return False
    exact_markers = (
        "curl: (28)",
        "curl: (35)",
        "tls connect error",
        # curl(56)=接收网络数据失败，一律按传输层瞬断处理：
        # 覆盖 connection closed abruptly / connect tunnel failed /
        # BoringSSL SSL_read SSLV3_ALERT_BAD_RECORD_MAC 等所有变体
        "curl: (56)",
        "curl: (7)",
        "wrong_version_number",
        "err_no_supported_proxies",
        "could not resolve proxy",
        "proxy connect",
        "proxyerror",
        "response 407",
        # curl-cffi/OpenSSL and proxy bridges use several equivalent labels
        # for a transient TLS tunnel failure.  These errors do not say
        # anything about the account credential, so a different route should
        # be tried before marking the account failed.
        "curl: (27)",
        "ssl error",
        "sslerror",
        "ssl connect",
        "ssl handshake",
        "tls handshake",
        "tlsv1_alert",
        "certificate verify failed",
        "unexpected eof while reading",
        "eof occurred in violation of protocol",
        "proxy tunnel",
        "gateway timeout",
    )
    if any(marker in message for marker in exact_markers):
        return True
    return any(
        marker in message
        for marker in (
            "curl: (56) recv failure",
            "connection reset by peer",
            "connection closed abruptly",
            "connection aborted",
            "broken pipe",
        )
    )

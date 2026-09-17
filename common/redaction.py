# -*- coding: utf-8 -*-
"""Redaction helpers shared by the local web panel and tests."""

from __future__ import annotations

import copy
from typing import Any
from urllib.parse import urlsplit, urlunsplit


_SMS_CODE_SECRET_FIELDS = (
    ("api_key", "api_key_masked", "has_api_key"),
    (
        "firefox_api_password",
        "firefox_api_password_masked",
        "has_firefox_api_password",
    ),
    (
        "firefox_api_token",
        "firefox_api_token_masked",
        "has_firefox_api_token",
    ),
)
_SMS_CODE_DISPLAY_FIELDS = frozenset(
    {metadata for _, masked, flag in _SMS_CODE_SECRET_FIELDS for metadata in (masked, flag)}
    | {"has_firefox_password", "has_firefox_token"}
)


def mask_secret(value: str, *, head: int = 6, tail: int = 4) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= head + tail + 3:
        return f"…({len(text)})"
    return f"{text[:head]}…{text[-tail:]}"


def _mask_url_password(value: str) -> str:
    """Keep a Redis endpoint usable for display without exposing its password."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
        if parsed.password is None:
            return text
        username = parsed.username or ""
        auth = f"{username}:{mask_secret(parsed.password)}@"
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, auth + host, parsed.path, parsed.query, parsed.fragment))
    except Exception:
        return mask_secret(text)


def redact_config_for_api(cfg: dict[str, Any]) -> dict[str, Any]:
    data = copy.deepcopy(cfg)
    data.pop("_loaded_from", None)
    sms_code = data.get("sms_code") if isinstance(data.get("sms_code"), dict) else {}
    if isinstance(sms_code, dict):
        for secret_key, masked_key, flag_key in _SMS_CODE_SECRET_FIELDS:
            raw_secret = str(sms_code.get(secret_key) or "").strip()
            sms_code[secret_key] = ""
            sms_code[masked_key] = mask_secret(raw_secret) if raw_secret else ""
            sms_code[flag_key] = bool(raw_secret)
    mail = data.get("mail") if isinstance(data.get("mail"), dict) else {}
    providers = mail.get("providers") if isinstance(mail, dict) else []
    if isinstance(providers, list):
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            raw = str(provider.get("mailboxes") or "")
            provider["mailbox_count"] = len(
                {
                    line.split("----", 1)[0].strip().lower()
                    for line in raw.splitlines()
                    if "@" in line.split("----", 1)[0]
                }
            )
            provider["mailboxes"] = ""
            for secret_key, masked_key, flag_key in (
                ("password", "password_masked", "has_password"),
                ("api_key", "api_key_masked", "has_api_key"),
                ("api_token", "api_token_masked", "has_api_token"),
                ("admin_key", "admin_key_masked", "has_admin_key"),
                ("mailbox_password", "mailbox_password_masked", "has_mailbox_password"),
                ("token", "token_masked", "has_token"),
            ):
                if provider.get(secret_key):
                    provider[masked_key] = mask_secret(str(provider.get(secret_key) or ""))
                    provider[flag_key] = True
                    provider[secret_key] = ""
    c2a = data.get("chatgpt2api") if isinstance(data.get("chatgpt2api"), dict) else {}
    if isinstance(c2a, dict) and c2a.get("key"):
        c2a["key_masked"] = mask_secret(str(c2a.get("key") or ""))
        c2a["has_key"] = True
        c2a["key"] = ""
    web = data.get("web") if isinstance(data.get("web"), dict) else {}
    if isinstance(web, dict):
        if web.get("token"):
            web["token_masked"] = mask_secret(str(web.get("token") or ""))
            web["has_token"] = True
            web["token"] = ""
        if web.get("password"):
            web["password_masked"] = "••••••••"
            web["has_password"] = True
            web["password"] = ""
        web.setdefault("username", "admin")
    maintenance = data.get("maintenance") if isinstance(data.get("maintenance"), dict) else {}
    if isinstance(maintenance, dict) and maintenance.get("webhook_url"):
        maintenance["webhook_masked"] = mask_secret(str(maintenance.get("webhook_url") or ""))
        maintenance["has_webhook"] = True
        maintenance["webhook_url"] = ""
    cache = data.get("cache") if isinstance(data.get("cache"), dict) else {}
    if isinstance(cache, dict) and cache.get("redis_url"):
        raw_url = str(cache.get("redis_url") or "")
        try:
            has_password = urlsplit(raw_url).password is not None
        except Exception:
            has_password = False
        if has_password:
            cache["redis_url_masked"] = _mask_url_password(raw_url)
            cache["has_redis_password"] = True
            cache["redis_url"] = ""
    return data


def merge_config_preserving_secrets(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    """Deep-merge panel config updates without erasing masked/blank secrets."""
    if not isinstance(overlay, dict):
        return
    for key, value in overlay.items():
        if str(key).startswith("_"):
            continue
        if key == "sms_code" and isinstance(value, dict):
            sms_code = base.setdefault("sms_code", {})
            if not isinstance(sms_code, dict):
                sms_code = {}
                base["sms_code"] = sms_code
            secret_keys = {item[0] for item in _SMS_CODE_SECRET_FIELDS}
            for sk, sv in value.items():
                if sk in _SMS_CODE_DISPLAY_FIELDS:
                    continue
                secret_text = str(sv or "")
                if sk in secret_keys and (
                    not secret_text.strip()
                    or "***" in secret_text
                    or "…" in secret_text
                    or secret_text.startswith("•")
                ):
                    continue
                sms_code[sk] = sv
            for display_key in _SMS_CODE_DISPLAY_FIELDS:
                sms_code.pop(display_key, None)
            continue
        if key == "mail" and isinstance(value, dict) and isinstance(base.get("mail"), dict):
            b_mail = base["mail"]
            if isinstance(value.get("providers"), list):
                old_provs = b_mail.get("providers") if isinstance(b_mail.get("providers"), list) else []
                old_by_type = {str(p.get("type") or ""): p for p in old_provs if isinstance(p, dict)}
                new_provs = []
                for prov in value["providers"]:
                    if not isinstance(prov, dict):
                        continue
                    ptype = str(prov.get("type") or "")
                    old = old_by_type.get(ptype) or {}
                    merged = dict(old)
                    merged.update(prov)
                    merged.pop("mailbox_count", None)
                    mb = str(prov.get("mailboxes") if "mailboxes" in prov else "")
                    if (not mb.strip() or "***" in mb) and old.get("mailboxes"):
                        merged["mailboxes"] = old.get("mailboxes")
                    for secret_key in ("password", "api_key", "api_token", "admin_key", "mailbox_password", "token"):
                        if secret_key not in prov:
                            continue
                        raw_secret = prov.get(secret_key)
                        secret_text = str(raw_secret or "")
                        if (
                            (not secret_text.strip() or "***" in secret_text or "…" in secret_text or secret_text.startswith("•"))
                            and old.get(secret_key)
                        ):
                            merged[secret_key] = old.get(secret_key)
                    for masked_key in (
                        "password_masked",
                        "has_password",
                        "api_key_masked",
                        "has_api_key",
                        "api_token_masked",
                        "has_api_token",
                        "admin_key_masked",
                        "has_admin_key",
                        "mailbox_password_masked",
                        "has_mailbox_password",
                        "token_masked",
                        "has_token",
                    ):
                        merged.pop(masked_key, None)
                    new_provs.append(merged)
                b_mail["providers"] = new_provs
            for mk, mv in value.items():
                if mk == "providers":
                    continue
                b_mail[mk] = mv
            continue
        if key == "chatgpt2api" and isinstance(value, dict):
            c2a = base.setdefault("chatgpt2api", {})
            if not isinstance(c2a, dict):
                c2a = {}
                base["chatgpt2api"] = c2a
            for ck, cv in value.items():
                if ck == "key" and (not str(cv or "").strip() or "***" in str(cv) or "…" in str(cv)):
                    continue
                c2a[ck] = cv
            continue
        if key == "web" and isinstance(value, dict):
            web = base.setdefault("web", {})
            if not isinstance(web, dict):
                web = {}
                base["web"] = web
            for wk, wv in value.items():
                if wk in {"token", "password"} and (
                    not str(wv or "").strip()
                    or "…" in str(wv)
                    or str(wv).startswith("•")
                    or "***" in str(wv)
                ):
                    continue
                web[wk] = wv
            continue
        if key == "maintenance" and isinstance(value, dict):
            maintenance = base.setdefault("maintenance", {})
            if not isinstance(maintenance, dict):
                maintenance = {}
                base["maintenance"] = maintenance
            for mk, mv in value.items():
                if mk == "webhook_url" and (
                    not str(mv or "").strip() or "…" in str(mv) or "***" in str(mv)
                ):
                    continue
                maintenance[mk] = mv
            continue
        if key == "cache" and isinstance(value, dict):
            cache = base.setdefault("cache", {})
            if not isinstance(cache, dict):
                cache = {}
                base["cache"] = cache
            for ck, cv in value.items():
                if ck in {"redis_url_masked", "has_redis_password"}:
                    continue
                if ck == "redis_url" and not str(cv or "").strip():
                    continue
                cache[ck] = cv
            continue
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge_config_preserving_secrets(base[key], value)
        else:
            base[key] = value

"""Mailbox domain: mail stats, mailbox pool, maddy auto-register and mail routes."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import threading
import time
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Any
from web.routes.auth import MailMessagesRequest

router = APIRouter()


def _state() -> Any:
    # Delayed import is intentional: web_app includes this router.
    import web_app

    return web_app


_ICLOUD_INBOX_SYNC_LOCK = threading.Lock()
_ICLOUD_INBOX_SYNC_AT = 0.0
_ICLOUD_INBOX_SYNC_INTERVAL = 5.0


_MAIL_STATS_CACHE_LOCK = threading.Lock()
_MAIL_STATS_BUILD_LOCK = threading.Lock()
_MAIL_STATS_CACHE_KEY: tuple[Any, ...] | None = None
_MAIL_STATS_CACHE_AT = 0.0
_MAIL_STATS_CACHE_VALUE: dict[str, Any] | None = None
class MailboxesUpdate(BaseModel):
    mailboxes: str = ""
    append: bool = True
    mode: str | None = None
    provider_type: str | None = None
    api_base: str | None = None
    api_key: str | None = None
    api_token: str | None = None
    admin_key: str | None = None
    accounts: str | None = None
    default_domain: str | None = None
    domains: list[str] | None = None
    local_length: int | None = None
    capacity: int | None = None
    enable: bool | None = None
    label: str | None = None
    mailbox_password: str | None = None


class MaddyWebuiDomainsRequest(BaseModel):
    api_base: str | None = None
    api_key: str | None = None
    api_token: str | None = None


class FastmailAccountsRequest(BaseModel):
    api_base: str | None = None
    admin_key: str | None = None


class MailboxDeleteRequest(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)
    delete_all: bool = False
    provider_type: str | None = "auto"


class MailboxStateRequest(BaseModel):
    emails: list[str] = Field(default_factory=list)
    action: str = "release"
    states: list[str] = Field(default_factory=list)


class MaddyAutoRegisterUpdate(BaseModel):
    enabled: bool = False
    interval_hours: int = Field(1, ge=1, le=168)
    per_domain: int = Field(20, ge=1, le=500)
    concurrency: int = Field(3, ge=1, le=20)
    engine: str = Field(default="protocol", min_length=1, max_length=16)
    keep_on_fail: bool = False
    refresh_domains: bool = True
    group_id: str = Field(default="", max_length=256)


def _mail_stats(cfg: dict[str, Any]) -> dict[str, Any]:
    mail_cfg = cfg.get("mail") if isinstance(cfg.get("mail"), dict) else {}
    capacity = _state().count_pool_capacity(mail_cfg)
    state = _state()._load_state_file()
    providers_out: list[dict[str, Any]] = []
    totals = {
        "total": 0,
        "available": 0,
        "used": 0,
        "failed": 0,
        "in_use": 0,
        "token_invalid": 0,
        "cooldown": 0,
    }
    for item in _state()._providers(cfg):
        ptype = _state().normalize_provider_type(str(item.get("type") or ""))
        if ptype not in _state().CANONICAL_PROVIDER_TYPES:
            continue
        lines = str(item.get("mailboxes") or "")
        if ptype == "icloud_api":
            pool = _state().parse_icloud_credentials(lines)
            stats = _state()._pool_stats_for(pool, state)
        elif ptype == "maddy_webui":
            try:
                dynamic_capacity = int(
                    item.get("capacity")
                    or item.get("auto_create_limit")
                    or _state().MADDY_WEBUI_DEFAULT_CAPACITY
                )
            except (TypeError, ValueError):
                dynamic_capacity = _state().MADDY_WEBUI_DEFAULT_CAPACITY
            dynamic_capacity = max(0, min(500, dynamic_capacity))
            pool = _state().parse_domain_mailboxes(lines)
            stats = _state()._pool_stats_for(pool, state) if pool else {
                "total": dynamic_capacity,
                "available": dynamic_capacity,
                "used": 0,
                "failed": 0,
                "in_use": 0,
                "token_invalid": 0,
                "cooldown": 0,
            }
        elif ptype == "fastmail_api":
            # Remote alias pool (short shared cache inside the mail module)
            # plus local state rows created by acquire. Disabled providers
            # skip the remote call entirely — /api/status must stay fast.
            try:
                fallback_capacity = max(
                    0,
                    min(1000, int(item.get("capacity") or _state().FASTMAIL_API_DEFAULT_CAPACITY)),
                )
            except (TypeError, ValueError):
                fallback_capacity = _state().FASTMAIL_API_DEFAULT_CAPACITY
            if bool(item.get("enable", True)):
                conf = _state()._make_config(mail_cfg)
                conf["request_timeout"] = min(float(conf.get("request_timeout") or 30), 6.0)
                fastmail_provider = _state().FastmailApiProvider(dict(item), conf)
                try:
                    summary = fastmail_provider.pool_summary()
                finally:
                    fastmail_provider.close()
                if summary.get("error"):
                    available = fallback_capacity
                    total = fallback_capacity
                else:
                    available = max(0, int(summary.get("available") or 0))
                    total = max(available, int(summary.get("total") or 0))
            else:
                available = fallback_capacity
                total = fallback_capacity
            local_rows = [
                {"email": email}
                for email, entry in state.items()
                if isinstance(entry, dict)
                and _state().normalize_provider_type(str(entry.get("provider") or "")) == "fastmail_api"
            ]
            local_stats = _state()._pool_stats_for(local_rows, state)
            stats = {
                "total": total,
                "available": available,
                "used": local_stats["used"],
                "failed": local_stats["failed"],
                "in_use": local_stats["in_use"],
                "token_invalid": local_stats["token_invalid"],
                "cooldown": local_stats["cooldown"],
            }
        else:
            pool = _state().parse_outlook_credentials(lines)
            stats = _state()._pool_stats_for(pool, state)
        for k in totals:
            totals[k] += stats[k]
        api_secret = str(item.get("api_key") or item.get("api_token") or "")
        providers_out.append(
            {
                "type": ptype,
                "enable": bool(item.get("enable", True)),
                "label": str(item.get("label") or ptype),
                "mode": str(
                    item.get("mode")
                    or (
                        "api"
                        if ptype in {"icloud_api", "maddy_webui", "fastmail_api"}
                        else "imap"
                    )
                ),
                "api_base": str(item.get("api_base") or ""),
                "default_domain": str(item.get("default_domain") or item.get("domain") or ""),
                "api_key": _state()._mask_secret(api_secret) if api_secret else "",
                "api_token": _state()._mask_secret(api_secret) if api_secret else "",
                "mailboxes": lines,
                **stats,
            }
        )
    primary = next(
        (p for p in providers_out if p["type"] == "outlook_token"),
        providers_out[0] if providers_out else {},
    )
    return {
        **totals,
        "split_total": int(capacity.get("split_total") or 0),
        "split_available": int(
            capacity.get("split_slot_available")
            if capacity.get("split_slot_available") is not None
            else capacity.get("split_available") or 0
        ),
        "split_usable": int(capacity.get("split_available") or 0),
        "split_used": int(capacity.get("split_used") or 0),
        "registration_available": int(capacity.get("available") or 0),
        "total_slots": int(capacity.get("total_slots") or totals["total"]),
        "mode": str(primary.get("mode") or "imap"),
        "label": " + ".join(p["label"] for p in providers_out) or "Mail Pool",
        "providers": providers_out,
    }


def _mailbox_items(
    cfg: dict[str, Any], *, provider_type: str = "auto", q: str = ""
) -> list[dict[str, Any]]:
    want = _state().normalize_provider_type(provider_type)
    if want in {"outlook", "ms", "microsoft"}:
        want = "outlook_token"
    elif want in {"icloud", "apple", "hme", "hide_my_email"}:
        want = "icloud_api"
    elif want in {"", "auto", "any", "all"}:
        want = ""

    state = _state()._load_state_file()
    registered = _state().account_email_set(_state().ACCOUNTS_FILE)
    query = str(q or "").strip().lower()
    out: list[dict[str, Any]] = []

    for pi, provider in enumerate(_state()._providers(cfg), start=1):
        ptype = _state().normalize_provider_type(str(provider.get("type") or ""))
        if ptype not in _state().CANONICAL_PROVIDER_TYPES:
            continue
        if want and ptype != want:
            continue
        lines = str(provider.get("mailboxes") or "")
        if ptype == "maddy_webui":
            pool = _state().parse_domain_mailboxes(lines)
            # Maddy creates dynamic mailboxes through the automation API and
            # does not expose a mailbox-pool listing endpoint. The shared
            # state file is the local index for mailboxes created by this
            # provider, including their terminal registration state.
            provider_ref = f"{ptype}#{pi}"
            configured_api_base = str(provider.get("api_base") or "").strip().rstrip("/").lower()
            configured_domains = {
                str(value or "").strip().lower()
                for value in (
                    provider.get("default_domain"),
                    provider.get("domain"),
                )
                if str(value or "").strip()
            }
            extra_domains = provider.get("domains")
            if isinstance(extra_domains, list):
                configured_domains.update(
                    str(value or "").strip().lower()
                    for value in extra_domains
                    if str(value or "").strip()
                )
            elif isinstance(extra_domains, str):
                configured_domains.update(
                    value.strip().lower()
                    for value in re.split(r"[,;\s]+", extra_domains)
                    if value.strip()
                )
            known = {str(item.get("email") or "").strip().lower() for item in pool}
            for address, state_entry in state.items():
                if not isinstance(state_entry, dict) or "@" not in str(address):
                    continue
                entry_provider = _state().normalize_provider_type(str(state_entry.get("provider") or ""))
                entry_ref = str(state_entry.get("provider_ref") or "").strip()
                entry_api_base = str(state_entry.get("api_base") or "").strip().rstrip("/").lower()
                address_domain = str(address).rsplit("@", 1)[1].lower()
                if entry_ref and entry_ref != provider_ref:
                    continue
                if entry_provider not in {ptype, ""} and entry_ref != provider_ref:
                    continue
                if entry_provider == "" and not (
                    configured_api_base
                    and entry_api_base
                    and entry_api_base == configured_api_base
                ):
                    continue
                if configured_domains and address_domain not in configured_domains:
                    continue
                if not entry_ref and configured_api_base and entry_api_base and entry_api_base != configured_api_base:
                    continue
                email = str(address).strip().lower()
                if email not in known:
                    pool.append({"email": email})
                    known.add(email)
        elif ptype == "fastmail_api":
            pool = _state().parse_domain_mailboxes(lines)
            # mailcode-api owns the alias pool; locally we only track the
            # aliases this provider acquired (state file metadata).
            provider_ref = f"{ptype}#{pi}"
            configured_api_base = str(provider.get("api_base") or "").strip().rstrip("/").lower()
            known = {str(item.get("email") or "").strip().lower() for item in pool}
            for address, state_entry in state.items():
                if not isinstance(state_entry, dict) or "@" not in str(address):
                    continue
                entry_provider = _state().normalize_provider_type(str(state_entry.get("provider") or ""))
                entry_ref = str(state_entry.get("provider_ref") or "").strip()
                entry_api_base = str(state_entry.get("api_base") or "").strip().rstrip("/").lower()
                if entry_ref and entry_ref != provider_ref:
                    continue
                if entry_provider not in {ptype, ""}:
                    continue
                if entry_provider == "" and not (
                    configured_api_base
                    and entry_api_base
                    and entry_api_base == configured_api_base
                ):
                    continue
                email = str(address).strip().lower()
                if email not in known:
                    pool.append({"email": email})
                    known.add(email)
        elif ptype == "icloud_api":
            pool = _state().parse_icloud_credentials(lines)
        else:
            pool = _state().parse_outlook_credentials(lines)
        for idx, item in enumerate(pool, start=1):
            email = str(item.get("email") or "").strip()
            email_l = email.lower()
            st_entry = (
                state.get(email_l) if isinstance(state.get(email_l), dict) else {}
            )
            st = str((st_entry or {}).get("state") or "available").strip() or "available"
            row = {
                "id": f"{ptype}:{email_l}",
                "index": idx,
                "provider_index": pi,
                "provider_type": ptype,
                "provider_label": str(provider.get("label") or ptype),
                "enabled": bool(provider.get("enable", True)),
                "email": email,
                "login_address": _state().base_mailbox_address(email),
                "state": st,
                "reason": str((st_entry or {}).get("reason") or ""),
                "updated_at": str((st_entry or {}).get("updated_at") or ""),
                "registered": email_l in registered,
                "mode": str(
                    provider.get("mode")
                    or (
                        "api"
                        if ptype in {"icloud_api", "maddy_webui", "fastmail_api"}
                        else "imap"
                    )
                ),
                "api_base": str(provider.get("api_base") or ""),
                "client_id": _state()._mask_secret(str(item.get("client_id") or "")),
                "secret": _state()._mask_secret(
                    str(item.get("api_key") or item.get("refresh_token") or "")
                ),
            }
            hay = " ".join(
                str(row.get(k) or "")
                for k in ("email", "provider_type", "provider_label", "state", "mode")
            ).lower()
            if query and query not in hay:
                continue
            out.append(row)
    return out


def _find_mailbox_credential(
    cfg: dict[str, Any], provider_type: str | None, email: str
) -> tuple[dict[str, Any], dict[str, str]]:
    target = str(email or "").strip().lower()
    want = _state().normalize_provider_type(str(provider_type or ""))
    if want in {"outlook", "ms", "microsoft"}:
        want = "outlook_token"
    elif want in {"icloud", "apple", "hme", "hide_my_email"}:
        want = "icloud_api"
    elif want in {"", "auto", "any", "all"}:
        want = ""
    for provider in _state()._providers(cfg):
        ptype = _state().normalize_provider_type(str(provider.get("type") or ""))
        if ptype not in _state().CANONICAL_PROVIDER_TYPES or (want and ptype != want):
            continue
        if ptype == "fastmail_api":
            if any(
                str(item.get("email") or "").strip().lower() == target
                for item in _state().parse_domain_mailboxes(str(provider.get("mailboxes") or ""))
            ):
                return dict(provider, type=ptype), {"email": target}
            state_entry = _state()._load_state_file().get(target)
            if isinstance(state_entry, dict) and (
                _state().normalize_provider_type(str(state_entry.get("provider") or "")) == "fastmail_api"
            ):
                return dict(provider, type=ptype), {
                    "email": target,
                    "account": str(state_entry.get("account") or ""),
                    "key": str(state_entry.get("key") or ""),
                }
            # Configured pool acts as a fallback owner; the message fetch
            # resolves the alias key through the mailcode-api admin API.
            if str(provider.get("accounts") or "").strip() or str(
                provider.get("admin_key") or provider.get("api_key") or ""
            ).strip():
                return dict(provider, type=ptype), {"email": target}
            continue
        if ptype == "maddy_webui":
            target_domain = target.rsplit("@", 1)[1] if "@" in target else ""
            configured_domains = {
                str(value or "").strip().lower()
                for value in (
                    provider.get("default_domain"),
                    provider.get("domain"),
                )
                if str(value or "").strip()
            }
            extra_domains = provider.get("domains")
            if isinstance(extra_domains, list):
                configured_domains.update(
                    str(value or "").strip().lower()
                    for value in extra_domains
                    if str(value or "").strip()
                )
            elif isinstance(extra_domains, str):
                configured_domains.update(
                    value.strip().lower()
                    for value in re.split(r"[,;\s]+", extra_domains)
                    if value.strip()
                )
            if not configured_domains or target_domain in configured_domains:
                lines = str(provider.get("mailboxes") or "")
                if any(
                    str(item.get("email") or "").strip().lower() == target
                    for item in _state().parse_domain_mailboxes(lines)
                ):
                    return dict(provider, type=ptype), {"email": target}
                state_entry = _state()._load_state_file().get(target)
                if isinstance(state_entry, dict):
                    entry_provider = _state().normalize_provider_type(str(state_entry.get("provider") or ""))
                    entry_api_base = str(state_entry.get("api_base") or "").strip().rstrip("/").lower()
                    provider_api_base = str(provider.get("api_base") or "").strip().rstrip("/").lower()
                    if entry_provider == ptype or (
                        ptype == "maddy_webui"
                        and entry_api_base
                        and provider_api_base
                        and entry_api_base == provider_api_base
                    ):
                        return dict(provider, type=ptype), {"email": target}
            continue
        pool = (
            _state().parse_icloud_credentials(str(provider.get("mailboxes") or ""))
            if ptype == "icloud_api"
            else _state().parse_outlook_credentials(str(provider.get("mailboxes") or ""))
        )
        for item in pool:
            if str(item.get("email") or "").strip().lower() == target:
                return dict(provider, type=ptype), item
    raise HTTPException(404, f"邮箱不在当前邮箱池中: {email}")


def _refresh_icloud_inbox_cache(
    provider: _state().ICloudApiProvider,
    base: str,
    credential: dict[str, str],
    conf: dict[str, Any],
) -> None:
    """Coalesce the upstream iCloud sync shared by all inbox viewers."""
    global _ICLOUD_INBOX_SYNC_AT
    now = time.monotonic()
    if now - _ICLOUD_INBOX_SYNC_AT < _ICLOUD_INBOX_SYNC_INTERVAL:
        return
    if not _ICLOUD_INBOX_SYNC_LOCK.acquire(blocking=False):
        return
    try:
        now = time.monotonic()
        if now - _ICLOUD_INBOX_SYNC_AT < _ICLOUD_INBOX_SYNC_INTERVAL:
            return
        try:
            provider.session.get(
                f"{base}/api/v1/code",
                params={"key": credential["api_key"], "email": credential["email"]},
                headers={"Accept": "application/json", "User-Agent": conf["user_agent"]},
                timeout=6.0,
            )
        finally:
            # Cool down failed upstream attempts too, otherwise many open inboxes
            # immediately recreate the same timeout storm.
            _ICLOUD_INBOX_SYNC_AT = time.monotonic()
    finally:
        _ICLOUD_INBOX_SYNC_LOCK.release()


def _fetch_mailbox_messages_sync(
    cfg: dict[str, Any], body: MailMessagesRequest
) -> dict[str, Any]:
    provider_entry, cred = _state()._find_mailbox_credential(cfg, body.provider_type, body.email)
    ptype = _state().normalize_provider_type(str(provider_entry.get("type") or ""))
    conf = _state()._make_config(cfg.get("mail") if isinstance(cfg.get("mail"), dict) else {})
    conf["request_timeout"] = min(float(conf.get("request_timeout") or 30), 8.0)
    if ptype == "outlook_token":
        provider_entry = dict(
            provider_entry, message_limit=max(1, min(int(body.limit or 10), 30))
        )
        provider = _state().OutlookTokenProvider(provider_entry, conf)
        mailbox = {
            "provider": "outlook_token",
            "provider_ref": "preview",
            "address": cred["email"],
            "login_address": _state().base_mailbox_address(cred["email"]),
            "base_address": _state().base_mailbox_address(cred["email"]),
            "label": str(provider_entry.get("label") or "Outlook Pool"),
            "password": cred.get("password", ""),
            "client_id": cred["client_id"],
            "refresh_token": cred["refresh_token"],
        }
        try:
            messages = provider.fetch_recent_messages(mailbox)[: body.limit]
            return {
                "ok": True,
                "provider_type": ptype,
                "email": cred["email"],
                "mode": provider.mode,
                "items": [_state()._message_preview(m) for m in messages],
            }
        finally:
            provider.close()

    if ptype == "maddy_webui":
        provider_entry = dict(
            provider_entry, message_limit=max(1, min(int(body.limit or 10), 30))
        )
        provider = _state().MaddyWebuiProvider(provider_entry, conf)
        mailbox = {
            "provider": "maddy_webui",
            "provider_ref": "preview",
            "address": cred["email"],
            "login_address": cred["email"],
            "base_address": _state().base_mailbox_address(cred["email"]),
            "label": str(provider_entry.get("label") or "Maddy 邮箱"),
            "api_base": str(provider_entry.get("api_base") or ""),
        }
        try:
            messages = provider.fetch_recent_messages(mailbox)[: body.limit]
            return {
                "ok": True,
                "provider_type": ptype,
                "email": cred["email"],
                "mode": "api",
                "items": [_state()._message_preview(m) for m in messages],
            }
        finally:
            provider.close()

    if ptype == "fastmail_api":
        provider_entry = dict(
            provider_entry, message_limit=max(1, min(int(body.limit or 10), 30))
        )
        provider = _state().FastmailApiProvider(provider_entry, conf)
        mailbox = {
            "provider": "fastmail_api",
            "provider_ref": str(provider_entry.get("provider_ref") or "preview"),
            "address": cred["email"],
            "login_address": cred["email"],
            "base_address": _state().base_mailbox_address(cred["email"]),
            "label": str(provider_entry.get("label") or "Fastmail Pool"),
            "api_base": str(provider_entry.get("api_base") or ""),
            "account": str(cred.get("account") or ""),
            "alias_key": str(cred.get("key") or ""),
        }
        try:
            messages = provider.fetch_recent_messages(mailbox)[: body.limit]
            return {
                "ok": True,
                "provider_type": ptype,
                "email": cred["email"],
                "mode": "api",
                "items": [_state()._message_preview(m) for m in messages],
            }
        finally:
            provider.close()

    provider = _state().ICloudApiProvider(provider_entry, conf)
    mailbox = {
        "provider": "icloud_api",
        "provider_ref": "preview",
        "address": cred["email"],
        "login_address": _state().base_mailbox_address(cred["email"]),
        "base_address": _state().base_mailbox_address(cred["email"]),
        "label": str(provider_entry.get("label") or "iCloud Pool"),
        "api_key": cred["api_key"],
        "api_base": str(provider_entry.get("api_base") or ""),
    }
    try:
        base = str(mailbox.get("api_base") or provider.api_base).rstrip("/")
        # /messages only lists the API server's local cache. Coalesce the
        # upstream sync so many inbox tabs do not each start an iCloud request.
        try:
            _refresh_icloud_inbox_cache(provider, base, cred, conf)
        except Exception:
            pass
        resp = provider.session.get(
            f"{base}/api/v1/messages",
            params={
                "key": cred["api_key"],
                "email": cred["email"],
                "limit": max(1, min(int(body.limit or 10), 30)),
            },
            headers={"Accept": "application/json", "User-Agent": conf["user_agent"]},
            timeout=8.0,
        )
        try:
            data = resp.json()
        except Exception:
            data = {"ok": False, "error": resp.text[:300]}
        if resp.status_code != 200:
            data = {
                "ok": False,
                "error": data.get("error")
                or data.get("message")
                or f"HTTP {resp.status_code}",
            }
        if not isinstance(data, dict):
            data = {"ok": False, "error": "invalid iCloud API response"}
        if data.get("ok") is False:
            return {
                "ok": False,
                "provider_type": ptype,
                "email": cred["email"],
                "mode": "api",
                "items": [],
                "error": str(
                    data.get("error")
                    or data.get("message")
                    or data.get("sync_error")
                    or "iCloud API 暂无可读邮件"
                ),
            }
        messages = data.get("messages") if isinstance(data.get("messages"), list) else []
        items = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            recipient = message.get("to") or message.get("alias_email") or cred["email"]
            recipients = recipient if isinstance(recipient, list) else [str(recipient or "")]
            preview = str(message.get("preview") or "")
            subject = str(message.get("subject") or "")
            subject_lower = subject.lower()
            code = str(message.get("code") or "").strip()
            if not re.fullmatch(r"\d{6}", code) or any(
                marker in subject_lower
                for marker in ("new sign-in", "new login", "security alert")
            ):
                code = ""
            items.append(
                {
                    "id": str(message.get("id") or ""),
                    "subject": subject,
                    "sender": str(message.get("from") or message.get("sender") or ""),
                    "recipients": [str(value) for value in recipients if str(value)],
                    "received_at": str(message.get("received_at") or ""),
                    "code": code,
                    "preview": preview[:260],
                    "text": preview[:200000],
                    "html": "",
                    "has_raw": bool(message.get("id")),
                }
            )
        return {
            "ok": True,
            "provider_type": ptype,
            "email": cred["email"],
            "mode": "api",
            "items": items,
        }
    finally:
        provider.close()


def _fetch_icloud_message_sync(
    cfg: dict[str, Any], email: str, provider_type: str, message_id: str
) -> dict[str, Any]:
    provider_entry, cred = _state()._find_mailbox_credential(cfg, provider_type, email)
    ptype = _state().normalize_provider_type(str(provider_entry.get("type") or ""))
    if ptype != "icloud_api":
        raise HTTPException(400, "该邮箱类型不支持远程正文查询")
    conf = _state()._make_config(cfg.get("mail") if isinstance(cfg.get("mail"), dict) else {})
    provider = _state().ICloudApiProvider(provider_entry, conf)
    try:
        base = str(provider_entry.get("api_base") or provider.api_base).rstrip("/")
        resp = provider.session.get(
            f"{base}/api/v1/message/raw",
            params={
                "key": cred["api_key"],
                "email": cred["email"],
                "id": str(message_id),
            },
            headers={"Accept": "application/json", "User-Agent": conf["user_agent"]},
            timeout=12.0,
        )
        try:
            data = resp.json()
        except Exception:
            data = {"ok": False, "error": resp.text[:300]}
        if resp.status_code != 200 or not isinstance(data, dict) or data.get("ok") is False:
            detail = (data.get("error") if isinstance(data, dict) else "") or f"HTTP {resp.status_code}"
            raise HTTPException(502, f"邮件正文读取失败: {detail}")
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        if not message:
            raise HTTPException(404, "邮件不存在")
        recipient = message.get("to") or cred["email"]
        recipients = recipient if isinstance(recipient, list) else [str(recipient or "")]
        text = str(message.get("body_text") or "")
        subject = str(message.get("subject") or "")
        code = str(message.get("code") or "").strip()
        if not re.fullmatch(r"\d{6}", code) or any(
            marker in subject.lower()
            for marker in ("new sign-in", "new login", "security alert")
        ):
            code = ""
        return {
            "ok": True,
            "item": {
                "id": str(message.get("id") or message_id),
                "subject": subject,
                "sender": str(message.get("from") or ""),
                "recipients": [str(value) for value in recipients if str(value)],
                "received_at": str(message.get("received_at") or ""),
                "code": code,
                "preview": text[:260],
                "text": text[:200000],
                "html": str(message.get("body_html") or "")[:300000],
            },
        }
    finally:
        provider.close()


def _mailbox_provider_map(cfg: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for provider in _state()._providers(cfg):
        ptype = _state().normalize_provider_type(str(provider.get("type") or ""))
        if ptype not in _state().CANONICAL_PROVIDER_TYPES:
            continue
        if ptype in {"maddy_webui", "fastmail_api"}:
            parser = _state().parse_domain_mailboxes
        else:
            parser = _state().parse_icloud_credentials if ptype == "icloud_api" else _state().parse_outlook_credentials
        for item in parser(str(provider.get("mailboxes") or "")):
            email = str(item.get("email") or "").strip().lower()
            if email:
                result.setdefault(email, ptype)
    # Dynamic fastmail aliases live in the state file, not in mailboxes text.
    for email, entry in _state()._load_state_file().items():
        if not isinstance(entry, dict):
            continue
        if _state().normalize_provider_type(str(entry.get("provider") or "")) == "fastmail_api":
            address = str(email or "").strip().lower()
            if address:
                result.setdefault(address, "fastmail_api")
    return result


def _filter_mailbox_text(
    raw: str, ptype: str, remove_emails: set[str]
) -> tuple[str, int]:
    kept: list[str] = []
    removed = 0
    if ptype in {"maddy_webui", "fastmail_api"}:
        parser = _state().parse_domain_mailboxes
    else:
        parser = (
            _state().parse_icloud_credentials if ptype == "icloud_api" else _state().parse_outlook_credentials
        )
    valid = {item["email"].strip().lower() for item in parser(raw)}
    for line in str(raw or "").splitlines():
        if ptype in {"maddy_webui", "fastmail_api"}:
            email = line.split("----", 1)[0].strip().lower()
        else:
            email = line.split("----", 1)[0].strip().lower() if "----" in line else ""
        if email and email in remove_emails and email in valid:
            removed += 1
            continue
        kept.append(line)
    return (
        "\n".join(kept).strip()
        + ("\n" if kept and any(x.strip() for x in kept) else ""),
        removed,
    )


def _serialize_mailbox_credential(item: dict[str, str], ptype: str) -> str:
    if ptype == "icloud_api":
        return f"{item['email']}----{item['api_key']}"
    if ptype in {"maddy_webui", "fastmail_api"}:
        remote_id = str(item.get("id") or "").strip()
        return f"{item['email']}----{remote_id}" if remote_id else item["email"]
    return "----".join(
        (
            item["email"],
            item.get("password", ""),
            item["client_id"],
            item["refresh_token"],
        )
    )


def _merge_mailbox_credentials(
    existing: str, incoming: str, ptype: str, *, append: bool = True
) -> tuple[str, dict[str, int]]:
    if ptype in {"maddy_webui", "fastmail_api"}:
        parser = _state().parse_domain_mailboxes
    else:
        parser = (
            _state().parse_icloud_credentials if ptype == "icloud_api" else _state().parse_outlook_credentials
        )
    old_items = parser(existing) if append or not str(incoming or "").strip() else []
    new_items = parser(incoming)
    merged: dict[str, dict[str, str]] = {
        item["email"].strip().lower(): item for item in old_items
    }
    added = 0
    updated = 0
    unchanged = 0
    for item in new_items:
        key = item["email"].strip().lower()
        previous = merged.get(key)
        if previous is None:
            added += 1
        elif _serialize_mailbox_credential(
            previous, ptype
        ) == _serialize_mailbox_credential(item, ptype):
            unchanged += 1
        else:
            updated += 1
        merged[key] = item
    lines = [_serialize_mailbox_credential(item, ptype) for item in merged.values()]
    text = "\n".join(lines) + ("\n" if lines else "")
    return text, {
        "imported": len(new_items),
        "added": added,
        "updated": updated,
        "unchanged": unchanged,
        "total": len(merged),
    }


def _delete_mailboxes_from_config(
    cfg: dict[str, Any], body: MailboxDeleteRequest
) -> int:
    want = _state().normalize_provider_type(str(body.provider_type or "auto"))
    if want in {"outlook", "ms", "microsoft"}:
        want = "outlook_token"
    elif want in {"icloud", "apple", "hme", "hide_my_email"}:
        want = "icloud_api"
    elif want in {"", "auto", "any", "all"}:
        want = ""
    selected: dict[str, set[str]] = {
        "outlook_token": set(),
        "icloud_api": set(),
        "maddy_webui": set(),
        "fastmail_api": set(),
    }
    for item in body.items or []:
        email = str(item.get("email") or "").strip().lower()
        ptype = _state().normalize_provider_type(str(item.get("provider_type") or want or ""))
        if ptype in {"outlook", "ms", "microsoft"}:
            ptype = "outlook_token"
        elif ptype in {"icloud", "apple", "hme", "hide_my_email"}:
            ptype = "icloud_api"
        elif ptype in {"maddy", "maddy_webui_api", "maddy_api", "maddy_mail"}:
            ptype = "maddy_webui"
        elif ptype in {"fastmail", "fastmail_mail", "mailcode", "mailcode_api"}:
            ptype = "fastmail_api"
        if email and ptype in selected:
            selected[ptype].add(email)

    removed = 0
    removed_emails: set[str] = set()
    for provider in _state()._providers(cfg):
        ptype = _state().normalize_provider_type(str(provider.get("type") or ""))
        if ptype not in _state().CANONICAL_PROVIDER_TYPES:
            continue
        if want and ptype != want:
            continue
        raw = str(provider.get("mailboxes") or "")
        if ptype in {"maddy_webui", "fastmail_api"}:
            pool = _state().parse_domain_mailboxes(raw)
            targets = selected.get(ptype, set())
            if body.delete_all:
                targets = {
                    str(item.get("email") or "").strip().lower()
                    for item in pool
                    if str(item.get("email") or "").strip()
                }
            remote_removed: set[str] = set()
            if body.delete_all:
                provider["mailboxes"] = ""
            elif pool:
                new_text, _n = _filter_mailbox_text(raw, ptype, targets)
                provider["mailboxes"] = new_text
                valid = {item["email"].strip().lower() for item in pool}
                local_removed = {email for email in targets if email in valid}
                removed += len(local_removed - remote_removed)
                removed_emails.update(local_removed)
            if ptype in {"maddy_webui", "fastmail_api"} and targets:
                state = _state()._load_state_file()
                state_targets = {
                    email
                    for email in targets
                    if isinstance(state.get(email), dict)
                }
                removed += len(state_targets - removed_emails)
                removed_emails.update(state_targets)
            continue
        pool = (
            _state().parse_icloud_credentials(raw)
            if ptype == "icloud_api"
            else _state().parse_outlook_credentials(raw)
        )
        if body.delete_all:
            removed += len(pool)
            removed_emails.update(item["email"].strip().lower() for item in pool)
            provider["mailboxes"] = ""
        else:
            targets = selected.get(ptype, set())
            new_text, n = _filter_mailbox_text(raw, ptype, targets)
            provider["mailboxes"] = new_text
            removed += n
            valid = {item["email"].strip().lower() for item in pool}
            removed_emails.update(email for email in targets if email in valid)

    if removed_emails:
        # Keep mailbox credentials and reservation state consistent. The mail
        # module owns the shared lock and atomic state-file replacement.
        _state().update_mailbox_states(removed_emails, action="release")
    return removed


def _public_mail_stats(cfg: dict[str, Any]) -> dict[str, Any]:
    global _MAIL_STATS_CACHE_KEY, _MAIL_STATS_CACHE_AT, _MAIL_STATS_CACHE_VALUE
    mail_cfg = cfg.get("mail") if isinstance(cfg.get("mail"), dict) else {}
    try:
        mail_digest = hashlib.sha256(
            json.dumps(mail_cfg, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
    except Exception:
        mail_digest = str(id(mail_cfg))
    cache_key = (
        mail_digest,
        _state()._file_stamp(_state().CONFIG_FILE),
        _state()._file_stamp(_state().STATE_FILE),
    )
    now = time.monotonic()
    with _MAIL_STATS_CACHE_LOCK:
        if (
            _MAIL_STATS_CACHE_VALUE is not None
            and _MAIL_STATS_CACHE_KEY == cache_key
            and now - _MAIL_STATS_CACHE_AT < 3.0
        ):
            return copy.deepcopy(_MAIL_STATS_CACHE_VALUE)

    # Status and operations can miss their independent response caches at the
    # same time. Allow only one expensive mailbox scan to run, then re-check
    # the short-lived cache before doing any work.
    with _MAIL_STATS_BUILD_LOCK:
        now = time.monotonic()
        with _MAIL_STATS_CACHE_LOCK:
            if (
                _MAIL_STATS_CACHE_VALUE is not None
                and _MAIL_STATS_CACHE_KEY == cache_key
                and now - _MAIL_STATS_CACHE_AT < 3.0
            ):
                return copy.deepcopy(_MAIL_STATS_CACHE_VALUE)
        stats = _state()._mail_stats(cfg)
        providers = stats.get("providers")
        if isinstance(providers, list):
            slim = []
            for p in providers:
                if not isinstance(p, dict):
                    continue
                item = dict(p)
                item.pop("mailboxes", None)
                slim.append(item)
            stats["providers"] = slim
        built_at = time.monotonic()
        with _MAIL_STATS_CACHE_LOCK:
            _MAIL_STATS_CACHE_KEY = cache_key
            _MAIL_STATS_CACHE_AT = built_at
            _MAIL_STATS_CACHE_VALUE = copy.deepcopy(stats)
        return stats


def _maddy_auto_config(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    source = cfg if isinstance(cfg, dict) else _state()._read_config()
    raw = source.get("maddy_auto_register") if isinstance(source.get("maddy_auto_register"), dict) else {}
    reg_cfg = source.get("registration") if isinstance(source.get("registration"), dict) else {}
    engine = str(raw.get("engine") or reg_cfg.get("engine") or "protocol").strip().lower() or "protocol"
    return {
        "enabled": bool(raw.get("enabled")),
        "interval_hours": max(1, min(int(raw.get("interval_hours") or 1), 168)),
        "per_domain": max(1, min(int(raw.get("per_domain") or 20), 500)),
        "concurrency": max(1, min(int(raw.get("concurrency") or reg_cfg.get("threads") or 3), 20)),
        "engine": engine,
        "keep_on_fail": bool(raw.get("keep_on_fail")),
        "refresh_domains": raw.get("refresh_domains", True) is not False,
        "group_id": str(raw.get("group_id") or "").strip(),
    }


def _maddy_auto_provider_entry(cfg: dict[str, Any]) -> dict[str, Any]:
    return dict(_state()._find_provider(cfg, "maddy_webui") or {}, type="maddy_webui")


def _sanitize_maddy_provider_entry(provider: dict[str, Any]) -> bool:
    if not isinstance(provider, dict):
        return False
    blocked = _state()._maddy_blocked_domains(provider)
    blocked_list = _state()._normalize_domain_list(
        [*_state().MADDY_WEBUI_DEFAULT_BLOCKED_DOMAINS, *_state()._normalize_domain_list(provider.get("blocked_domains"))]
    )
    changed = False
    if _state()._normalize_domain_list(provider.get("blocked_domains")) != blocked_list:
        provider["blocked_domains"] = blocked_list
        changed = True
    domains = _state()._normalize_domain_list(provider.get("domains"))
    filtered_domains = [domain for domain in domains if domain not in blocked]
    if filtered_domains != domains:
        provider["domains"] = filtered_domains
        changed = True
    replacement = filtered_domains[0] if filtered_domains else ""
    for key in ("default_domain", "domain"):
        current = str(provider.get(key) or "").strip().lower()
        if current in blocked and provider.get(key) != replacement:
            provider[key] = replacement
            changed = True
    return changed


def _maddy_secret_input(value: Any) -> str:
    """Return a usable Maddy credential, ignoring values redacted for display.

    The config endpoint intentionally returns masked credentials (for example
    ``maddy_…acf3``).  The UI may send that value back when refreshing or
    saving the provider.  Treating it as a new key would overwrite the real
    credential and make every subsequent API request fail authentication.
    """
    text = str(value or "").strip()
    if not text or any(marker in text for marker in ("…", "•", "***")):
        return ""
    return text


def _maddy_blocked_domains(entry: dict[str, Any] | None = None) -> set[str]:
    blocked = set(_state()._normalize_domain_list(list(_state().MADDY_WEBUI_DEFAULT_BLOCKED_DOMAINS)))
    if isinstance(entry, dict):
        blocked.update(_state()._normalize_domain_list(entry.get("blocked_domains")))
    return blocked


def _maddy_allowed_domains(entry: dict[str, Any] | None, values: Any) -> list[str]:
    blocked = _state()._maddy_blocked_domains(entry)
    return [domain for domain in _state()._normalize_domain_list(values) if domain not in blocked]


def _maddy_auto_domains(cfg: dict[str, Any], *, refresh: bool = True) -> list[str]:
    entry = _maddy_auto_provider_entry(cfg)
    if not entry:
        return []
    conf = _state()._make_config(cfg.get("mail") if isinstance(cfg.get("mail"), dict) else {})
    conf["request_timeout"] = min(float(conf.get("request_timeout") or 30), 10.0)
    provider = _state().MaddyWebuiProvider(entry, conf)
    try:
        if refresh:
            try:
                info = provider.fetch_domain_info(force=True)
                domains = _maddy_allowed_domains(entry, info.get("domains") or [])
                if domains:
                    return domains
            except Exception as exc:
                _state().logger.warning("Maddy 自动注册拉取域名失败，回退本地配置: %s", str(exc)[:200])
        domains = provider._configured_domains()
        return _maddy_allowed_domains(entry, domains)
    finally:
        provider.close()


def _maddy_auto_domain_last_runs(state: dict[str, Any]) -> dict[str, float]:
    raw = state.get("maddy_auto_domain_last_epoch")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in raw.items():
        try:
            out[str(key).strip().lower()] = float(value or 0)
        except (TypeError, ValueError):
            continue
    return out


def _maddy_auto_mark_domain_run(domain: str, *, result: dict[str, Any]) -> dict[str, Any]:
    domain_l = str(domain or "").strip().lower()
    if not domain_l:
        return _state().load_ops_state()
    state = _state().load_ops_state()
    domain_runs = _maddy_auto_domain_last_runs(state)
    domain_runs[domain_l] = time.time()
    state.update(
        {
            "maddy_auto_current_domain": "",
            "maddy_auto_current_started_at": "",
            "maddy_auto_last_epoch": time.time(),
            "maddy_auto_last_at": _state()._now_iso(),
            "maddy_auto_last_result": dict(result),
            "maddy_auto_domain_last_epoch": domain_runs,
            "maddy_auto_last_domain": domain_l,
        }
    )
    _state().update_ops_state(**state)
    return state


def _maddy_auto_mark_domain_start(domain: str, settings: dict[str, Any]) -> dict[str, Any]:
    domain_l = str(domain or "").strip().lower()
    state = _state().load_ops_state()
    domain_runs = _maddy_auto_domain_last_runs(state)
    domain_runs[domain_l] = time.time()
    state.update(
        {
            "maddy_auto_current_domain": domain_l,
            "maddy_auto_current_started_at": _state()._now_iso(),
            "maddy_auto_current_interval_hours": int(settings.get("interval_hours") or 1),
            "maddy_auto_current_per_domain": int(settings.get("per_domain") or 20),
            "maddy_auto_domain_last_epoch": domain_runs,
        }
    )
    _state().update_ops_state(**state)
    return state


def _apply_maddy_domain_to_config(cfg: dict[str, Any], domain: str) -> bool:
    domain_l = str(domain or "").strip().lower()
    if not domain_l:
        return False
    mail = cfg.get("mail") if isinstance(cfg.get("mail"), dict) else {}
    providers = mail.get("providers") if isinstance(mail.get("providers"), list) else []
    changed = False
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        if _state().normalize_provider_type(str(provider.get("type") or "")) != "maddy_webui":
            continue
        if domain_l in _state()._maddy_blocked_domains(provider):
            return False
        provider["default_domain"] = domain_l
        provider["domain"] = domain_l
        provider_domains = _state()._normalize_domain_list(provider.get("domains"))
        provider["domains"] = [
            domain_l,
            *[
                item
                for item in provider_domains
                if item != domain_l and item not in _state()._maddy_blocked_domains(provider)
            ],
        ]
        _state()._sanitize_maddy_provider_entry(provider)
        changed = True
    if changed and isinstance(mail, dict):
        cfg["mail"] = mail
    return changed


def _maddy_auto_domain_plan(cfg: dict[str, Any], settings: dict[str, Any]) -> list[str]:
    domains = _state()._maddy_auto_domains(cfg, refresh=bool(settings.get("refresh_domains")))
    return _state()._normalize_domain_list(domains)


async def _run_maddy_auto_register_once(*, force: bool = False) -> dict[str, Any]:
    if not _state()._MADDY_AUTO_RUN_LOCK.acquire(blocking=False):
        return {"ok": True, "skipped": True, "reason": "maddy_auto_already_running"}
    try:
        cfg = _state()._read_config()
        settings = _state()._maddy_auto_config(cfg)
        if not settings["enabled"] and not force:
            # Skip before the domain fetch so a disabled feature never hits
            # the Maddy API (and never logs remote errors) from the loop.
            return {"ok": True, "skipped": True, "reason": "disabled", "settings": settings}
        state = _state().load_ops_state()
        now_epoch = time.time()
        last_runs = _maddy_auto_domain_last_runs(state)
        domains = _maddy_auto_domain_plan(cfg, settings)
        if not domains:
            _state().update_ops_state(
                maddy_auto_last_epoch=now_epoch,
                maddy_auto_last_at=_state()._now_iso(),
                maddy_auto_last_error="no_domains",
                maddy_auto_current_domain="",
                maddy_auto_current_started_at="",
            )
            return {"ok": True, "skipped": True, "reason": "no_domains", "settings": settings}
        due: list[str] = []
        interval_sec = max(1, int(settings["interval_hours"])) * 3600
        for domain in domains:
            if force or now_epoch - float(last_runs.get(domain) or 0) >= interval_sec:
                due.append(domain)
        if not due:
            return {
                "ok": True,
                "skipped": True,
                "reason": "not_due",
                "domains": domains,
                "settings": settings,
            }

        results: list[dict[str, Any]] = []
        for domain in due:
            if _state()._SERVER_SHUTDOWN_EVENT.is_set():
                break
            with _state().JOB.lock:
                busy = bool(_state().JOB.running)
            if busy:
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": "job_running",
                    "domain": domain,
                    "results": results,
                    "settings": settings,
                }
            _maddy_auto_mark_domain_start(domain, settings)
            try:
                await _state().job_start(
                    _state().StartJobRequest(
                        total=int(settings["per_domain"]),
                        concurrency=int(settings["concurrency"]),
                        keep_on_fail=bool(settings["keep_on_fail"]),
                        mail_type="maddy_webui",
                        maddy_domain=domain,
                        engine=str(settings["engine"] or "protocol"),
                        group_id=str(settings["group_id"] or "") or None,
                    )
                )
            except HTTPException as exc:
                _state().update_ops_state(
                    maddy_auto_last_error=str(exc.detail)[:200],
                    maddy_auto_last_domain=domain,
                    maddy_auto_current_domain="",
                    maddy_auto_current_started_at="",
                )
                results.append({"domain": domain, "ok": False, "error": str(exc.detail)})
                continue
            except Exception as exc:
                _state().update_ops_state(
                    maddy_auto_last_error=str(exc)[:200],
                    maddy_auto_last_domain=domain,
                    maddy_auto_current_domain="",
                    maddy_auto_current_started_at="",
                )
                results.append({"domain": domain, "ok": False, "error": str(exc)[:200]})
                continue

            await _state()._wait_for_job_completion()
            job_snapshot = _state().JOB.snapshot()
            ok = int(job_snapshot.get("success_count") or 0)
            fail = int(job_snapshot.get("fail_count") or 0)
            skipped = int(job_snapshot.get("skipped_count") or 0)
            result = {
                "domain": domain,
                "ok": True,
                "success": ok,
                "failed": fail,
                "skipped": skipped,
                "total": int(job_snapshot.get("total") or int(settings["per_domain"])),
                "batch_id": job_snapshot.get("batch_id") or "",
                "finished_at": job_snapshot.get("finished_at") or _state()._now_iso(),
            }
            _state()._maddy_auto_mark_domain_run(domain, result=result)
            results.append(result)

        _state().update_ops_state(
            maddy_auto_last_epoch=time.time(),
            maddy_auto_last_at=_state()._now_iso(),
            maddy_auto_last_result=results[-1] if results else {},
            maddy_auto_last_error="",
            maddy_auto_current_domain="",
            maddy_auto_current_started_at="",
        )
        return {"ok": True, "skipped": False, "results": results, "settings": settings, "domains": domains}
    finally:
        _state()._MADDY_AUTO_RUN_LOCK.release()


async def _maddy_auto_loop() -> None:
    while not _state()._SERVER_SHUTDOWN_EVENT.is_set():
        try:
            await _run_maddy_auto_register_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _state().JOB.append_log(f"Maddy 自动注册异常: {str(exc)[:160]}")
        await asyncio.sleep(60)


@router.put("/api/mailboxes")
async def put_mailboxes(body: MailboxesUpdate):
    # Mailbox imports are additive configuration writes.  Registration and
    # reauthentication keep their own mailbox/config snapshots, while the
    # edit transaction serializes concurrent writers.  Do not block importing
    # new credentials just because an unrelated batch is active.
    def _do():
        with _state()._edit_config() as cfg:
            ptype = _state().normalize_provider_type(body.provider_type or "outlook_token")
            if ptype not in _state().CANONICAL_PROVIDER_TYPES:
                raise HTTPException(400, f"unsupported provider_type: {body.provider_type}")
            prov = _state()._ensure_provider(cfg, ptype)
            prov["type"] = ptype
            merged_text, import_stats = _merge_mailbox_credentials(
                str(prov.get("mailboxes") or ""),
                body.mailboxes,
                ptype,
                append=body.append,
            )
            if body.mailboxes.strip() and import_stats["imported"] == 0:
                raise HTTPException(400, "未识别到有效邮箱凭证，请检查每行格式")
            progress_key = f"mail_import_{ptype}"
            total_lines = len(
                [line for line in body.mailboxes.splitlines() if line.strip()]
            )
            _state().PROGRESS.start(
                progress_key,
                (
                    "iCloud 邮箱导入"
                    if ptype == "icloud_api"
                    else "Maddy WebUI 配置"
                    if ptype == "maddy_webui"
                    else "Fastmail 配置"
                    if ptype == "fastmail_api"
                    else "Outlook 邮箱导入"
                ),
                total=total_lines,
                stage="校验并合并凭证",
            )
            _state().PROGRESS.update(
                progress_key, percent=65, done=import_stats.get("imported", 0)
            )
            prov["mailboxes"] = merged_text
            if body.enable is not None:
                prov["enable"] = bool(body.enable)
            else:
                prov.setdefault("enable", True)
            if body.label:
                prov["label"] = body.label
            if ptype == "outlook_token":
                if body.mode:
                    prov["mode"] = body.mode
            elif ptype == "maddy_webui":
                prov["mode"] = "api"
                if body.api_base is not None:
                    prov["api_base"] = (
                        (body.api_base or "").strip() or _state().MADDY_WEBUI_DEFAULT_BASE
                    )
                else:
                    prov.setdefault("api_base", _state().MADDY_WEBUI_DEFAULT_BASE)
                api_key = _maddy_secret_input(body.api_key)
                api_token = _maddy_secret_input(body.api_token)
                if api_key:
                    prov["api_key"] = api_key
                    prov.pop("api_token", None)
                elif api_token:
                    prov["api_token"] = api_token
                if body.default_domain is not None:
                    prov["default_domain"] = str(body.default_domain or "").strip()
                    prov["domain"] = prov["default_domain"]
                if body.domains is not None:
                    seen_domains: set[str] = set()
                    cleaned_domains: list[str] = []
                    blocked_domains = _state()._maddy_blocked_domains(prov)
                    for value in body.domains:
                        domain = str(value or "").strip()
                        key = domain.lower()
                        if not domain or key in seen_domains or key in blocked_domains:
                            continue
                        seen_domains.add(key)
                        cleaned_domains.append(domain)
                    prov["domains"] = cleaned_domains
                if body.local_length is not None:
                    prov["local_length"] = max(8, min(30, int(body.local_length or 12)))
                if body.capacity is not None:
                    prov["capacity"] = max(1, min(500, int(body.capacity or 1)))
                if body.mailbox_password is not None and str(body.mailbox_password or "").strip():
                    prov["mailbox_password"] = str(body.mailbox_password or "").strip()
                prov.setdefault("local_length", _state().MADDY_WEBUI_DEFAULT_LOCAL_LENGTH)
                prov.setdefault("capacity", _state().MADDY_WEBUI_DEFAULT_CAPACITY)
                _state()._sanitize_maddy_provider_entry(prov)
            elif ptype == "fastmail_api":
                prov["mode"] = "api"
                if body.api_base is not None:
                    prov["api_base"] = (
                        (body.api_base or "").strip() or _state().FASTMAIL_API_DEFAULT_BASE
                    )
                else:
                    prov.setdefault("api_base", _state().FASTMAIL_API_DEFAULT_BASE)
                if body.admin_key is not None and str(body.admin_key or "").strip():
                    prov["admin_key"] = str(body.admin_key or "").strip()
                    prov.pop("api_key", None)
                elif body.api_key is not None and str(body.api_key or "").strip():
                    prov["api_key"] = str(body.api_key or "").strip()
                if body.accounts is not None:
                    prov["accounts"] = str(body.accounts or "")
                if body.capacity is not None:
                    prov["capacity"] = max(1, min(1000, int(body.capacity or 1)))
                prov.setdefault("capacity", _state().FASTMAIL_API_DEFAULT_CAPACITY)
                prov.setdefault("accounts", "")
            elif body.api_base is not None:
                prov["api_base"] = (
                    (body.api_base or "").strip() or "https://icloud.xbovo.online"
                )
            else:
                prov.setdefault("api_base", "https://icloud.xbovo.online")
        _state().PROGRESS.finish(
            progress_key,
            message=(
                f"新增 {import_stats.get('added', 0)}，"
                f"更新 {import_stats.get('updated', 0)}"
            ),
        )
        return {
            "ok": True,
            **import_stats,
            "mail": _state()._public_mail_stats(cfg),
        }

    return await asyncio.to_thread(_do)


def _maddy_webui_domains_payload(cfg: dict[str, Any], body: MaddyWebuiDomainsRequest) -> dict[str, Any]:
    saved = _state()._find_provider(cfg, "maddy_webui") or {}
    entry = dict(saved, type="maddy_webui")
    if body.api_base is not None:
        entry["api_base"] = (body.api_base or "").strip() or _state().MADDY_WEBUI_DEFAULT_BASE
    api_key = _maddy_secret_input(body.api_key)
    api_token = _maddy_secret_input(body.api_token)
    if api_key:
        entry["api_key"] = api_key
        entry.pop("api_token", None)
    elif api_token:
        entry["api_token"] = api_token
    conf = _state()._make_config(cfg.get("mail") if isinstance(cfg.get("mail"), dict) else {})
    conf["request_timeout"] = min(float(conf.get("request_timeout") or 30), 10.0)
    provider = _state().MaddyWebuiProvider(entry, conf)
    try:
        info = provider.fetch_domain_info(force=True)
    finally:
        provider.close()
    items = _maddy_allowed_domains(entry, info.get("domains") or [])
    primary_domain = str(info.get("primary_domain") or "").strip().lower()
    blocked_domains = _state()._maddy_blocked_domains(entry)
    if primary_domain in blocked_domains:
        primary_domain = ""
    if not primary_domain and items:
        primary_domain = items[0]
    default_domain = primary_domain or str(entry.get("default_domain") or entry.get("domain") or "").strip().lower()
    if default_domain in blocked_domains:
        default_domain = items[0] if items else ""
    if not default_domain and items:
        default_domain = items[0]
    return {
        "ok": True,
        "items": items,
        "default_domain": default_domain,
        "primary_domain": primary_domain,
        "count": len(items),
    }


@router.post("/api/maddy-webui/domains")
async def maddy_webui_domains(body: MaddyWebuiDomainsRequest):
    def _do():
        return _state()._maddy_webui_domains_payload(_state()._read_config(), body)

    return await asyncio.to_thread(_do)


@router.post("/api/fastmail/accounts")
async def fastmail_accounts(body: FastmailAccountsRequest):
    """List mailcode-api master accounts and their alias pool status."""

    def _do():
        cfg = _state()._read_config()
        saved = _state()._find_provider(cfg, "fastmail_api") or {}
        entry = dict(saved, type="fastmail_api")
        if body.api_base is not None:
            entry["api_base"] = (body.api_base or "").strip() or _state().FASTMAIL_API_DEFAULT_BASE
        if body.admin_key is not None and str(body.admin_key or "").strip():
            entry["admin_key"] = str(body.admin_key or "").strip()
        conf = _state()._make_config(cfg.get("mail") if isinstance(cfg.get("mail"), dict) else {})
        conf["request_timeout"] = min(float(conf.get("request_timeout") or 30), 10.0)
        provider = _state().FastmailApiProvider(entry, conf)
        try:
            rows = provider.fetch_accounts(force=True)
        finally:
            provider.close()
        items = [
            {
                "email": str(row.get("email") or ""),
                "enabled": bool(row.get("enabled", True)),
                "refill_enabled": bool(row.get("refillEnabled", True)),
                "pool_size": int(row.get("poolSize") or 0),
                "alias_count": int(row.get("aliasCount") or 0),
                "available_count": int(row.get("availableCount") or 0),
                "expired_count": int(row.get("expiredCount") or 0),
                "last_check_at": str(row.get("lastCheckAt") or ""),
            }
            for row in rows
            if isinstance(row, dict)
        ]
        return {
            "ok": True,
            "items": items,
            "available": sum(item["available_count"] for item in items),
            "total": sum(item["alias_count"] for item in items),
        }

    return await asyncio.to_thread(_do)


@router.get("/api/mailboxes/list")
async def mailboxes_list(
    provider_type: str = "auto", q: str = "", limit: int = 500
):
    def _do():
        cfg = _state()._read_config()
        cap = max(1, min(int(limit or 500), 2000))
        provider_key = _state().normalize_provider_type(provider_type) or "auto"
        query_key = _state()._cache_digest(str(q or "").strip().lower())
        key = ":".join(
            (
                "mailboxes:v2",
                provider_key,
                query_key,
                str(cap),
                str(_state()._file_stamp(_state().CONFIG_FILE)),
                str(_state()._file_stamp(_state().STATE_FILE)),
                str(_state().account_store_revision(_state().ACCOUNTS_FILE)),
                _state()._account_data_stamp(),
            )
        )

        def _build_payload() -> dict[str, Any]:
            items = _state()._mailbox_items(cfg, provider_type=provider_type, q=q)
            return {"count": len(items), "items": items[:cap]}

        return _state()._cached_json_response(
            cfg,
            key,
            "mailboxes_ttl",
            120,
            _build_payload,
        )

    return await asyncio.to_thread(_do)


@router.post("/api/mailboxes/delete")
async def mailboxes_delete(body: MailboxDeleteRequest):
    if _state().JOB.running:
        raise HTTPException(409, "job is running")

    def _do():
        with _state()._edit_config() as cfg:
            removed = _delete_mailboxes_from_config(cfg, body)
        return {
            "ok": True,
            "removed": removed,
            "mail": _state()._public_mail_stats(cfg),
        }

    return await asyncio.to_thread(_do)


@router.post("/api/mailboxes/state")
async def mailboxes_state(body: MailboxStateRequest):
    if _state().JOB.running:
        raise HTTPException(409, "任务运行时不能修改邮箱状态")
    from mail import update_mailbox_states

    changed = await asyncio.to_thread(
        update_mailbox_states,
        body.emails,
        action=body.action,
        states=body.states,
    )

    def _snapshot():
        cfg = _state()._read_config()
        return {
            "ok": True,
            "changed": changed,
            "mail": _state()._public_mail_stats(cfg),
        }

    return await asyncio.to_thread(_snapshot)


@router.post("/api/mailbox/messages")
async def mailbox_messages(body: MailMessagesRequest):
    cfg = _state()._read_config()
    try:
        return await asyncio.to_thread(_state()._fetch_mailbox_messages_sync, cfg, body)
    except HTTPException as e:
        return {
            "ok": False,
            "provider_type": body.provider_type or "auto",
            "email": body.email,
            "items": [],
            "error": str(e.detail),
        }
    except Exception as e:
        return {
            "ok": False,
            "provider_type": body.provider_type or "auto",
            "email": body.email,
            "items": [],
            "error": f"读取邮箱失败: {str(e)[:500]}",
        }


@router.post("/api/mail/reset-failed")
async def reset_failed_mail():
    with _state().JOB.lock:
        if _state().JOB.running:
            raise HTTPException(409, "任务运行时不能释放邮箱状态")
    reset = await asyncio.to_thread(_state().release_failed_mailboxes, include_stale_in_use=True)
    return {
        "ok": True,
        "reset": reset,
        "mail": _state()._public_mail_stats(_state()._read_config()),
    }


@router.put("/api/maddy-auto-register")
async def maddy_auto_register_update(body: MaddyAutoRegisterUpdate):
    engine = str(body.engine or "protocol").strip().lower()
    if engine not in _state().SUPPORTED_REGISTRATION_ENGINES:
        raise HTTPException(400, f"不支持的注册引擎: {engine}")
    group_id = str(body.group_id or "").strip()
    if group_id:
        groups = _state().list_groups(_state().GROUPS_FILE)
        if not any(str(item.get("id") or "") == group_id for item in groups):
            raise HTTPException(400, "目标分组不存在，请刷新分组列表")
    with _state()._edit_config() as cfg:
        current = cfg.setdefault("maddy_auto_register", {})
        current.update(
            {
                "enabled": bool(body.enabled),
                "interval_hours": max(1, min(int(body.interval_hours or 1), 168)),
                "per_domain": max(1, min(int(body.per_domain or 20), 500)),
                "concurrency": max(1, min(int(body.concurrency or 3), 20)),
                "engine": engine,
                "keep_on_fail": bool(body.keep_on_fail),
                "refresh_domains": bool(body.refresh_domains),
                "group_id": group_id,
            }
        )
    return {"ok": True, "maddy_auto_register": _state()._maddy_auto_config(cfg)}


@router.post("/api/maddy-auto-register/run")
async def maddy_auto_register_run():
    if _state()._MADDY_AUTO_RUN_LOCK.locked():
        return {"ok": True, "skipped": True, "reason": "maddy_auto_already_running"}
    asyncio.create_task(_run_maddy_auto_register_once(force=True))
    return {"ok": True, "message": "Maddy 自动注册已触发"}

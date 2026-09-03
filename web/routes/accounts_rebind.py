"""Account email rebind (邮箱换绑) routes.

Helpers, request models and handlers moved from web_app.py. Runtime state and
shared helpers resolve lazily through web_app to avoid import cycles with the
application bootstrap.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field


router = APIRouter()


def _state() -> Any:
    # Delayed import is intentional: web_app includes this router.
    import web_app

    return web_app


class AccountRebindPreflightRequest(BaseModel):
    """Source account emails and optional target selection for email rebind."""

    keys: list[str] = Field(default_factory=list, max_length=500)
    emails: list[str] = Field(default_factory=list, max_length=500)
    text: str = Field(default="", max_length=200_000)
    # ``new_email`` remains the single-target compatibility field.  The UI
    # also sends a newline/comma separated list here for batch rebinds.
    new_email: str = Field(default="", max_length=200_000)
    new_emails: list[str] = Field(default_factory=list, max_length=500)
    provider_type: str = Field(default="maddy_webui", max_length=32)
    reservation_id: str = Field(default="", max_length=128)

class AccountRebindStartRequest(AccountRebindPreflightRequest):
    concurrency: int = Field(default=1, ge=1, le=20)
    reservation_id: str = Field(default="", max_length=128)

class AccountRebindStopRequest(BaseModel):
    job_id: str = Field(min_length=1, max_length=128)


def _rebind_parse_sources(body: AccountRebindPreflightRequest) -> tuple[list[str], list[str]]:
    """Return de-duplicated source keys/emails and format errors."""
    raw: list[str] = [*body.keys, *body.emails]
    # A line may optionally be pasted as ``old@example.com----new@example``
    # (or with an arrow).  Keep only the source side here; target extraction is
    # handled separately so the normal one-column format remains compatible.
    for line in re.split(r"[\r\n]+", body.text or ""):
        value = str(line or "").strip()
        if not value:
            continue
        if "----" in value:
            value = value.split("----", 1)[0].strip()
        elif "->" in value:
            value = value.split("->", 1)[0].strip()
        raw.extend(re.split(r"[\s,，;；]+", value))
    values: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for value in raw:
        text = str(value or "").strip().strip("'\"`").lower()
        if not text or text in seen:
            continue
        seen.add(text)
        # ``keys`` may contain the stable account-id identity used by the
        # paginated panel, not only an email address.
        if (
            not text.startswith("account_id:")
            and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", text)
        ):
            invalid.append(text)
        else:
            values.append(text)
    return values, invalid


def _rebind_parse_target_specs(
    body: AccountRebindPreflightRequest,
) -> tuple[list[str], dict[str, str], list[str]]:
    """Parse optional target addresses and source----target pair mappings.

    ``new_email`` is intentionally accepted as a large text field because it
    is the legacy API name used by the single-account page.  Batch callers can
    paste one target per line, or paste pairs in the source input as
    ``source----target`` / ``source->target``.
    """
    values: list[str] = []
    pairs: dict[str, str] = {}
    invalid: list[str] = []
    raw: list[str] = [str(item or "").strip() for item in body.new_emails]
    raw.extend(
        item.strip()
        for item in re.split(r"[\r\n,，;；\s]+", body.new_email or "")
        if item.strip()
    )
    for line in re.split(r"[\r\n]+", body.text or ""):
        text = str(line or "").strip()
        delimiter = "----" if "----" in text else "->" if "->" in text else ""
        if not delimiter:
            continue
        left, right = (part.strip() for part in text.split(delimiter, 1))
        if left and right:
            target = right.lower()
            if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", target):
                invalid.append(target)
            else:
                pairs[left.lower()] = target
    for value in raw:
        text = str(value or "").strip().strip("'\"`").lower()
        if not text:
            continue
        if "----" in text or "->" in text:
            delimiter = "----" if "----" in text else "->"
            left, right = (part.strip() for part in text.split(delimiter, 1))
            if left and right:
                if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", right):
                    invalid.append(right)
                else:
                    pairs[left] = right
                continue
        if "@" not in text or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", text):
            invalid.append(text)
            continue
        if text not in values:
            values.append(text)
    return values, pairs, invalid


def _rebind_reservation_items(item: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return child reservation rows for both batch and legacy reservations."""
    if not isinstance(item, dict):
        return []
    children = item.get("items")
    if isinstance(children, list):
        return [child for child in children if isinstance(child, dict)]
    return [item]


def _rebind_release_reservation_item(item: dict[str, Any] | None) -> None:
    """Release every mailbox owned by a reservation, without double release."""
    seen: set[str] = set()
    for child in _rebind_reservation_items(item):
        mailbox = child.get("mailbox")
        if not isinstance(mailbox, dict):
            continue
        address = str(mailbox.get("address") or "").strip().lower()
        marker = address or f"object:{id(mailbox)}"
        if marker in seen:
            continue
        seen.add(marker)
        try:
            _state().release_mailbox(mailbox)
        except Exception:
            pass


def _rebind_reservation_cleanup() -> None:
    now = time.monotonic()
    expired: list[dict[str, Any]] = []
    with _state()._REBIND_RESERVATION_LOCK:
        for reservation_id, item in list(_state()._REBIND_RESERVATIONS.items()):
            if now - float(item.get("created_monotonic") or 0) >= _state()._REBIND_RESERVATION_TTL:
                expired.append(item)
                _state()._REBIND_RESERVATIONS.pop(reservation_id, None)
    for item in expired:
        _rebind_release_reservation_item(item)


def _rebind_release_source_reservations(source_key: str) -> None:
    """Release unclaimed candidates from an earlier preflight for this row."""
    key = str(source_key or "").strip().lower()
    if not key:
        return
    stale: list[dict[str, Any]] = []
    with _state()._REBIND_RESERVATION_LOCK:
        for reservation_id, item in list(_state()._REBIND_RESERVATIONS.items()):
            matched = False
            for child in _rebind_reservation_items(item):
                item_key = str(child.get("record_key") or "").strip().lower()
                source = child.get("source") if isinstance(child.get("source"), dict) else {}
                source_email = str(source.get("email") or "").strip().lower()
                source_account_id = str(source.get("account_id") or "").strip().lower()
                aliases = {
                    item_key,
                    source_email,
                    f"email:{source_email}" if source_email else "",
                    source_account_id,
                    f"account_id:{source_account_id}" if source_account_id else "",
                }
                if key in aliases:
                    matched = True
                    break
            if matched:
                stale.append(item)
                _state()._REBIND_RESERVATIONS.pop(reservation_id, None)
    for item in stale:
        _rebind_release_reservation_item(item)


def _rebind_discard_reservation(reservation_id: str) -> dict[str, Any] | None:
    """Atomically remove one unclaimed reservation and release its mailbox."""
    with _state()._REBIND_RESERVATION_LOCK:
        item = _state()._REBIND_RESERVATIONS.pop(str(reservation_id or "").strip(), None)
    if isinstance(item, dict):
        _rebind_release_reservation_item(item)
    return item if isinstance(item, dict) else None


def _rebind_register_reservation(reservation_id: str, item: dict[str, Any]) -> None:
    """Publish a candidate while superseding another preflight for the row."""
    keys = {
        str(child.get("record_key") or "").strip().lower()
        for child in _rebind_reservation_items(item)
        if str(child.get("record_key") or "").strip()
    }
    replaced: list[dict[str, Any]] = []
    with _state()._REBIND_RESERVATION_LOCK:
        target_keys = {
            str(child.get("email") or (child.get("mailbox") or {}).get("address") or "").strip().lower()
            for child in _rebind_reservation_items(item)
            if str(child.get("email") or (child.get("mailbox") or {}).get("address") or "").strip()
        }
        if keys:
            replacements: list[tuple[str, dict[str, Any]]] = []
            for old_id, old_item in list(_state()._REBIND_RESERVATIONS.items()):
                if old_id == reservation_id:
                    continue
                old_keys = {
                    str(child.get("record_key") or "").strip().lower()
                    for child in _rebind_reservation_items(old_item)
                }
                old_targets = {
                    str(child.get("email") or (child.get("mailbox") or {}).get("address") or "").strip().lower()
                    for child in _rebind_reservation_items(old_item)
                    if str(child.get("email") or (child.get("mailbox") or {}).get("address") or "").strip()
                }
                # A repeated source supersedes its old candidates. A target
                # owned by a different source is a hard conflict, otherwise
                # two tabs could run the same verification inbox.
                if target_keys.intersection(old_targets) and not keys.intersection(old_keys):
                    raise ValueError("目标邮箱已被其它换绑预检占用")
                if not keys.intersection(old_keys):
                    continue
                replacements.append((old_id, old_item))
            for old_id, old_item in replacements:
                _state()._REBIND_RESERVATIONS.pop(old_id, None)
                replaced.append(old_item)
        _state()._REBIND_RESERVATIONS[reservation_id] = item
    for old_item in replaced:
        _rebind_release_reservation_item(old_item)


def _rebind_record_for_source(value: str, records: list[dict[str, Any]]) -> dict[str, Any] | None:
    needle = str(value or "").strip().lower()
    for record in records:
        if _state()._account_key(record).lower() == needle:
            return record
        if str(record.get("email") or "").strip().lower() == needle:
            return record
        if needle.startswith("email:") and str(record.get("email") or "").strip().lower() == needle[6:]:
            return record
        if needle.startswith("account_id:") and str(record.get("account_id") or "").strip().lower() == needle[11:]:
            return record
    return None


def _rebind_load_record(value: str) -> dict[str, Any] | None:
    """Resolve one rebind source without decoding the complete account table."""
    try:
        record = _state().load_account_record_by_identity(value, path=_state().ACCOUNTS_FILE)
    except Exception:
        record = None
    if record is not None:
        return record
    # Keep compatibility with test doubles and legacy stores that expose a
    # custom delete_key not represented by the SQLite identity indexes.
    if _state()._load_accounts is not _state()._DEFAULT_LOAD_ACCOUNTS:
        return _rebind_record_for_source(value, _state()._load_accounts())
    return None


def _rebind_public_source(record: dict[str, Any]) -> dict[str, Any]:
    public = _state().account_public_view(record)
    return {
        "delete_key": str(public.get("delete_key") or _state()._account_key(record)),
        "email": str(record.get("email") or ""),
        "account_id": str(record.get("account_id") or ""),
        "plan_type": str(record.get("plan_type") or "free"),
        "health_status": str(public.get("health_status") or record.get("health_status") or "unknown"),
        "has_password": bool(public.get("has_password") or record.get("password")),
        "password_confirmed": bool(
            public.get("password_confirmed")
            if "password_confirmed" in public
            else record.get("password")
        ),
        "has_totp": bool(public.get("has_totp") or record.get("totp_secret")),
    }


def _rebind_mail_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    mail_cfg = cfg.get("mail") if isinstance(cfg.get("mail"), dict) else {}
    return dict(mail_cfg)


def _rebind_target_conflict(target_email: str) -> str:
    """Return a stable conflict reason when a target became occupied.

    Preflight reserves a mailbox in the provider state file, but an account,
    trash row, or CDK binding can still be created by another worker before
    ``/start`` (or while a long protocol transaction is running).  Keep this
    check cheap and side-effect free so callers can repeat it immediately
    before claiming and before committing the account row.
    """
    target = str(target_email or "").strip().lower()
    if not target:
        return "目标邮箱为空"
    try:
        if target in _state().account_email_set(_state().ACCOUNTS_FILE):
            return "目标邮箱已在账号库中"
    except Exception:
        # The atomic account-store update remains the final authority when a
        # transient read of the index fails.
        pass
    try:
        reason = str(_state()._known_mailbox_bindings().get(target) or "").strip()
    except Exception:
        reason = ""
    if reason:
        labels = {
            "registered_account": "账号库中",
            "known_deactivated": "回收站中",
            "cdk_bound": "CDK 已绑定",
        }
        return f"目标邮箱已占用（{labels.get(reason, reason)}）"
    return ""


def _rebind_reserve_target(
    cfg: dict[str, Any],
    *,
    target_email: str,
    provider_type: str,
) -> dict[str, Any]:
    """Reserve one rebind mailbox after checking local account occupancy."""
    target = str(target_email or "").strip().lower()
    ptype = _state().normalize_provider_type(provider_type or "maddy_webui")
    if ptype not in {"maddy_webui", "fastmail_api"}:
        raise ValueError("换绑目标只支持 maddy_webui 或 fastmail_api")
    if target and target in _state().account_email_set(_state().ACCOUNTS_FILE):
        raise ValueError("目标邮箱已在账号库中")
    known_bindings = _state()._known_mailbox_bindings()
    if target and target in known_bindings:
        raise ValueError("目标邮箱曾绑定过账号或兑换码")
    # Remote mailbox pools can include addresses which may have been used by a
    # deleted account. The provider's state reconciliation additionally checks
    # trash/CDK bindings before selecting an automatic candidate.
    mail_cfg = _rebind_mail_cfg(cfg)
    # ``create_mailbox`` reconciles historical account/trash/CDK bindings only
    # for automatic allocation. Rebind often receives an explicit target, so
    # run the same quarantine pass first to prevent reuse of an address that
    # was deleted from the current account table but has been used before.
    _state().reconcile_registered_mailbox_states(mail_cfg, provider_type=ptype)
    mailbox = _state().create_mailbox(
        mail_cfg,
        username=target or None,
        provider_type=ptype,
        allow_used=False,
    )
    address = str(mailbox.get("address") or mailbox.get("login_address") or "").strip().lower()
    if not address:
        try:
            _state().release_mailbox(mailbox)
        except Exception:
            pass
        raise RuntimeError("邮箱池没有返回目标地址")
    if target and address != target:
        # A provider must not silently canonicalize an explicitly requested
        # address: the verification inbox and the account-store email would
        # otherwise refer to different identities.
        _state().release_mailbox(mailbox)
        raise RuntimeError(f"邮箱服务返回了不同地址: {address}")
    if address in _state().account_email_set(_state().ACCOUNTS_FILE):
        _state().release_mailbox(mailbox)
        raise ValueError("候选邮箱已被账号库占用")
    if address in known_bindings:
        _state().release_mailbox(mailbox)
        raise ValueError("候选邮箱曾绑定过账号或兑换码")
    mailbox["address"] = address
    mailbox["login_address"] = str(mailbox.get("login_address") or address)
    return mailbox


def _rebind_proxy_candidates(account: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    try:
        candidates, _proxy_cfg = _state()._security_proxy_candidates(account, cfg)
        return candidates or [""]
    except Exception:
        return [str(account.get("proxy") or "").strip()]


def _rebind_identity_value(
    record: dict[str, Any],
    tokens: dict[str, Any],
    key: str,
) -> Any:
    """Keep an account's stored protocol fingerprint stable across rebind.

    The verification login uses a fresh HTTP session, which necessarily has a
    temporary registrar identity.  That identity must not overwrite the
    account's persisted fingerprint/profile; only missing legacy fields are
    filled from the fresh session.
    """
    existing = record.get(key)
    if existing is not None and str(existing).strip():
        return existing
    return tokens.get(key)


def _rebind_worker(
    *,
    index: int,
    record: dict[str, Any],
    reservation: dict[str, Any],
    stop_check: Callable[[], bool],
    task_event: Callable[..., Any],
) -> dict[str, Any]:
    """Run one rebind and commit only after the new session is verified."""
    old_email = str(record.get("email") or "").strip().lower()
    old_key = _state()._account_key(record)
    target = str(reservation.get("email") or reservation.get("address") or "").strip().lower()
    mailbox = reservation.get("mailbox") if isinstance(reservation.get("mailbox"), dict) else {}
    password = str(record.get("password") or "").strip()
    totp_secret = str(record.get("totp_secret") or "").strip()
    last_error: Exception | None = None
    began = False
    # Keep setup failures inside the same cleanup boundary as protocol
    # failures. A malformed runtime/configuration must not strand the
    # provider mailbox reservation or leave the account without an error log.
    try:
        cfg = reservation.get("config") if isinstance(reservation.get("config"), dict) else _state()._read_config()
        mail_cfg = _rebind_mail_cfg(cfg)
        registration = cfg.get("registration") if isinstance(cfg.get("registration"), dict) else {}
        runtime = _state().ProtocolRuntimeSettings.from_config(registration)
        proxy_cfg = cfg.get("proxy") if isinstance(cfg.get("proxy"), dict) else {}
        version = str(registration.get("rebind_client_version") or DEFAULT_REBIND_CLIENT_VERSION)
        build = str(registration.get("rebind_client_build") or DEFAULT_REBIND_CLIENT_BUILD)
        _state().record_operation_event(
            record,
            "email_rebind",
            "running",
            message="邮箱换绑开始",
            metadata={"target": target, "provider": str(mailbox.get("provider") or "")},
        )
        proxy_candidates = _rebind_proxy_candidates(record, cfg)
    except Exception as error:
        detail = str(error or "换绑初始化失败")[:500]
        try:
            _state().release_mailbox(mailbox)
        except Exception:
            pass
        try:
            _state().update_account_records(
                [old_key],
                {
                    "last_rebind_at": _state()._now_iso(),
                    "last_rebind_error": detail,
                    "last_rebind_old_email": old_email,
                    "last_rebind_new_email": target,
                },
                include_records=False,
                path=_state().ACCOUNTS_FILE,
            )
        except Exception:
            pass
        try:
            _state().record_operation_event(
                record,
                "email_rebind",
                "failed",
                message="邮箱换绑失败",
                error=detail,
                metadata={"target": target, "provider": str(mailbox.get("provider") or "")},
                secrets=(password, totp_secret),
            )
        except Exception:
            pass
        raise
    try:
        for attempt, proxy in enumerate(proxy_candidates):
            if stop_check():
                raise _state().RebindError("CANCELLED", "换绑任务已停止")
            try:
                target_conflict = _rebind_target_conflict(target)
                if target_conflict:
                    raise _state().RebindError("TARGET_CONFLICT", target_conflict)
                task_event(index, "running", email=old_email, new_email=target, step=f"使用线路 {attempt + 1} 开始换绑")
                result = _state().run_rebind(
                    old_email=old_email,
                    password=password,
                    totp_secret=totp_secret,
                    new_email=target,
                    mailbox=mailbox,
                    mail_config=mail_cfg,
                    proxy=proxy,
                    proxy_config=proxy_cfg,
                    runtime=runtime,
                    stop_check=stop_check,
                    event=task_event,
                    index=index,
                    client_version=version,
                    client_build=build,
                    mail_timeout=float(mail_cfg.get("wait_timeout") or 120),
                )
                began = bool(result.get("began"))
                tokens = result.get("tokens") if isinstance(result.get("tokens"), dict) else {}
                # A different worker may have claimed the address while the
                # remote OTP transaction was in flight.  Do not overwrite
                # that account row; the durable account-store transaction
                # below performs the same conflict check as a final guard.
                target_conflict = _rebind_target_conflict(target)
                if target_conflict:
                    raise _state().RebindError("TARGET_CONFLICT", target_conflict)
                fields = {
                    "email": target,
                    "access_token": tokens.get("access_token") or record.get("access_token"),
                    "refresh_token": tokens.get("refresh_token") or record.get("refresh_token"),
                    "id_token": tokens.get("id_token") or record.get("id_token"),
                    "session_token": tokens.get("session_token") or record.get("session_token"),
                    "account_id": tokens.get("account_id") or record.get("account_id"),
                    "session": tokens.get("session") or record.get("session"),
                    "cookies": tokens.get("cookies") or record.get("cookies"),
                    "profile": tokens.get("profile") or record.get("profile"),
                    "plan_type": tokens.get("plan_type") or record.get("plan_type") or "free",
                    "expires_at": tokens.get("expires_at") or record.get("expires_at"),
                    "mfa_factor_id": tokens.get("mfa_factor_id") or record.get("mfa_factor_id"),
                    "mfa_factor_type": tokens.get("mfa_factor_type") or record.get("mfa_factor_type"),
                    "last_reauthenticated_at": _state()._now_iso(),
                    "last_token_refresh_error": "",
                    # The target mailbox can come from a different provider
                    # than the source account. Keep the registration channel
                    # (``source_type``) intact, but point future OTP/relogin
                    # lookups at the provider that owns the new address.
                    "provider_type": str(mailbox.get("provider") or record.get("provider_type") or ""),
                    "mailbox_provider_ref": str(mailbox.get("provider_ref") or record.get("mailbox_provider_ref") or ""),
                    "oauth_client_id": _rebind_identity_value(record, tokens, "oauth_client_id"),
                    "device_id": _rebind_identity_value(record, tokens, "device_id"),
                    "protocol_profile_id": _rebind_identity_value(record, tokens, "protocol_profile_id"),
                    "browser_type": _rebind_identity_value(record, tokens, "browser_type"),
                    "tls_impersonate": _rebind_identity_value(record, tokens, "tls_impersonate"),
                    "user_agent": _rebind_identity_value(record, tokens, "user_agent"),
                    "accept_language": _rebind_identity_value(record, tokens, "accept_language"),
                    "locale": _rebind_identity_value(record, tokens, "locale"),
                    "timezone": _rebind_identity_value(record, tokens, "timezone"),
                    "timezone_offset_min": _rebind_identity_value(record, tokens, "timezone_offset_min"),
                    "protocol_screen": _rebind_identity_value(record, tokens, "protocol_screen"),
                    "protocol_hardware_concurrency": _rebind_identity_value(record, tokens, "protocol_hardware_concurrency"),
                    "protocol_device_memory": _rebind_identity_value(record, tokens, "protocol_device_memory"),
                    "protocol_platform": _rebind_identity_value(record, tokens, "protocol_platform"),
                    "protocol_webgl_vendor": _rebind_identity_value(record, tokens, "protocol_webgl_vendor"),
                    "protocol_webgl_renderer": _rebind_identity_value(record, tokens, "protocol_webgl_renderer"),
                    "status": "正常",
                    "health_status": "alive",
                    "health_error": "",
                    "registration_state": "",
                    "last_rebind_at": _state()._now_iso(),
                    "last_rebind_error": "",
                    "last_rebind_old_email": old_email,
                    "last_rebind_new_email": target,
                    "rebind_count": int(record.get("rebind_count") or 0) + 1,
                }
                try:
                    persisted = _state().rebind_account_record(old_key, target, fields, path=_state().ACCOUNTS_FILE)
                except ValueError as error:
                    # Keep the public task reason stable and prevent a local
                    # target conflict from being mistaken for a proxy retry.
                    raise _state().RebindError("TARGET_CONFLICT", str(error)[:300]) from error
                _state().mark_mailbox_result(mailbox, success=True)
                _state().record_operation_event(
                    persisted,
                    "email_rebind",
                    "success",
                    message="邮箱换绑成功",
                    metadata={"old_email": old_email, "new_email": target, "provider": str(mailbox.get("provider") or "")},
                    secrets=(password, totp_secret, tokens.get("access_token")),
                    aliases=(f"email:{old_email}",),
                )
                task_event(
                    index,
                    "success",
                    email=target,
                    new_email=target,
                    account_id=str(persisted.get("account_id") or ""),
                    access_token_masked=_state()._mask_secret(str(persisted.get("access_token") or "")),
                    step="换绑成功，账号库已同步新邮箱和会话",
                )
                return {
                    "new_email": target,
                    "account_id": str(persisted.get("account_id") or ""),
                    "access_token_masked": _state()._mask_secret(str(persisted.get("access_token") or "")),
                }
            except _state().RebindError as error:
                last_error = error
                began = bool(began or getattr(error, "began", False))
                if error.code == "CANCELLED":
                    raise
                # ``run_rebind`` annotates classified errors with whether the
                # remote begin request was accepted.  Once accepted, replaying
                # the whole flow through another proxy can send a second OTP
                # and race the first transaction, so stop retrying here.
                if began:
                    raise
                if not _state().is_proxy_transport_error(error) or attempt >= len(proxy_candidates) - 1:
                    raise
                task_event(index, "running", step="当前线路传输失败，切换下一条线路")
            except Exception as error:
                last_error = error
                began = bool(began or getattr(error, "began", False))
                if began:
                    raise
                if not _state().is_proxy_transport_error(error) or attempt >= len(proxy_candidates) - 1:
                    raise
        raise last_error or RuntimeError("换绑失败")
    except Exception as error:
        detail = str(error or "换绑失败")[:500]
        try:
            # Before ``begin`` the address is safe to return to the pool.  Once
            # the service accepted it, retain a failed/used quarantine because
            # a delayed OTP can still arrive and be consumed by a later task.
            if began and isinstance(error, _state().RebindError) and error.code == "TARGET_CONFLICT":
                # The remote service may already have accepted begin/verify,
                # so this address must never return to the available pool
                # even though the local account row was not committed.
                _state().mark_mailbox_result(mailbox, success=True)
            elif began:
                # Use a stable non-transient reason here.  Passing the raw
                # cancellation/proxy text would make the generic mailbox
                # classifier release the address, even though ``begin`` has
                # already made it unsafe to reuse.
                _state().mark_mailbox_result(
                    mailbox,
                    success=False,
                    error="rebind_begin_accepted",
                )
            else:
                _state().release_mailbox(mailbox)
        except Exception:
            pass
        failed_fields = {
            "last_rebind_at": _state()._now_iso(),
            "last_rebind_error": detail,
            "last_rebind_old_email": old_email,
            "last_rebind_new_email": target,
        }
        try:
            _state().update_account_records([old_key], failed_fields, include_records=False, path=_state().ACCOUNTS_FILE)
        except Exception:
            pass
        _state().record_operation_event(
            record,
            "email_rebind",
            "cancelled" if isinstance(error, _state().RebindError) and error.code == "CANCELLED" else "failed",
            message="邮箱换绑已停止" if isinstance(error, _state().RebindError) and error.code == "CANCELLED" else "邮箱换绑失败",
            error=detail,
            metadata={"target": target, "provider": str(mailbox.get("provider") or "")},
            secrets=(password, totp_secret),
        )
        raise


# Keep these defaults in one place; deployments can override them under
# registration.rebind_client_version/rebind_client_build without touching the
# protocol implementation.
DEFAULT_REBIND_CLIENT_VERSION = "prod-1e268a33279bcedafc2fe5526bfe230880444b77"
DEFAULT_REBIND_CLIENT_BUILD = "9748354"


def _rebind_job_worker_factory(cfg: dict[str, Any]):
    def worker(**kwargs: Any) -> dict[str, Any]:
        reservation = kwargs.get("reservation") if isinstance(kwargs.get("reservation"), dict) else {}
        reservation.setdefault("config", cfg)
        return _rebind_worker(**{**kwargs, "reservation": reservation})
    return worker


def _rebind_reservation_payload(item: dict[str, Any], reservation_id: str) -> dict[str, Any]:
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    target = item.get("mailbox") if isinstance(item.get("mailbox"), dict) else {}
    return {
        "reservation_id": reservation_id,
        "source": source,
        "target": {
            "email": str(target.get("address") or target.get("email") or item.get("email") or ""),
            "provider": str(target.get("provider") or ""),
            "provider_ref": str(target.get("provider_ref") or ""),
            "domain": str(target.get("domain") or ""),
            "reserved_at": str(item.get("created_at") or ""),
        },
    }


@router.post("/api/accounts/rebind/preflight")
async def account_rebind_preflight(body: AccountRebindPreflightRequest):
    await asyncio.to_thread(_rebind_reservation_cleanup)
    # A browser may submit a second preflight while an earlier candidate is
    # still displayed. Consume that candidate before validating the new input
    # so an invalid edit cannot strand a mailbox reservation.
    previous_reservation = str(body.reservation_id or "").strip()
    if previous_reservation:
        await asyncio.to_thread(_rebind_discard_reservation, previous_reservation)
    values, invalid = _rebind_parse_sources(body)
    if not values:
        raise HTTPException(400, "请输入当前 GPT 账号邮箱")
    if len(values) + len(invalid) > 500:
        raise HTTPException(400, "单批最多输入 500 个账号")
    target_values, target_pairs, invalid_targets = _rebind_parse_target_specs(body)
    # A list of explicit targets is positional.  Pair syntax can be used when
    # only a subset needs a fixed address; it is resolved by source identity.
    if target_values and len(target_values) != len(values):
        raise HTTPException(400, "批量指定新邮箱时，目标邮箱数量必须与账号数量一致")
    if invalid_targets:
        raise HTTPException(400, "新邮箱格式无效")

    requested = len(values) + len(invalid)
    provider_type = _state().normalize_provider_type(body.provider_type or "maddy_webui")
    cfg = await asyncio.to_thread(_state()._read_config)
    created_at = _state()._now_iso()
    created_monotonic = time.monotonic()
    internal_items: list[dict[str, Any]] = []
    reserved_targets: set[str] = set()
    resolved_record_keys: set[str] = set()

    def target_for(index: int, source_value: str, record: dict[str, Any]) -> str:
        candidates = [
            source_value,
            str(record.get("email") or "").strip().lower(),
            _state()._account_key(record).strip().lower(),
        ]
        for candidate in candidates:
            target = target_pairs.get(candidate)
            if target:
                return target
            if candidate.startswith("email:"):
                target = target_pairs.get(candidate[6:])
                if target:
                    return target
            if candidate.startswith("account_id:"):
                target = target_pairs.get(candidate[11:])
                if target:
                    return target
        if target_values:
            return target_values[index] if len(target_values) == len(values) else target_values[0]
        return ""

    for index, source_value in enumerate(values):
        try:
            record = await asyncio.to_thread(_rebind_load_record, source_value)
        except Exception as error:
            internal_items.append({
                "input": source_value,
                "record_key": "",
                "status": "not_found",
                "message": f"账号读取失败: {str(error)[:180]}",
                "source": None,
            })
            continue
        if record is None:
            await asyncio.to_thread(_rebind_release_source_reservations, source_value)
            internal_items.append({
                "input": source_value,
                "record_key": "",
                "status": "not_found",
                "message": "未找到当前账号",
                "source": None,
            })
            continue
        # Release candidates from an earlier tab before any eligibility branch.
        record_key = _state()._account_key(record)
        if record_key in resolved_record_keys:
            internal_items.append({
                "input": source_value,
                "source": _rebind_public_source(record),
                "record_key": record_key,
                "status": "duplicate",
                "message": "本批次中已输入同一个账号",
            })
            continue
        resolved_record_keys.add(record_key)
        # Release candidates from an earlier tab before any eligibility branch.
        await asyncio.to_thread(_rebind_release_source_reservations, record_key)
        public = _rebind_public_source(record)
        child: dict[str, Any] = {
            "input": source_value,
            "source": public,
            "record_key": record_key,
            "created_at": created_at,
            "created_monotonic": created_monotonic,
            "provider_type": provider_type,
            "config": cfg,
        }
        missing: list[str] = []
        if not bool(public.get("password_confirmed")):
            missing.append("password")
        if not str(record.get("totp_secret") or "").strip():
            missing.append("totp")
        if missing:
            child.update(status="missing_credentials", missing=missing, message="当前账号缺少密码或 TOTP 2FA")
            internal_items.append(child)
            continue
        if _state().account_is_deactivated(record):
            child.update(status="deactivated", message="已封禁账号不参与换绑")
            internal_items.append(child)
            continue
        target_email = target_for(index, source_value, record).strip().lower()
        if target_email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", target_email):
            child.update(status="target_invalid", target_email=target_email, message="新邮箱格式无效")
            internal_items.append(child)
            continue
        if target_email and target_email == str(record.get("email") or "").strip().lower():
            child.update(status="target_conflict", target_email=target_email, message="新邮箱不能与当前邮箱相同")
            internal_items.append(child)
            continue
        if target_email and target_email in reserved_targets:
            child.update(status="target_conflict", target_email=target_email, message="目标邮箱在本批次中重复")
            internal_items.append(child)
            continue
        try:
            mailbox = await asyncio.to_thread(
                _rebind_reserve_target,
                cfg,
                target_email=target_email,
                provider_type=provider_type,
            )
        except ValueError as error:
            child.update(status="target_conflict", target_email=target_email, message=str(error)[:300])
            internal_items.append(child)
            continue
        except Exception as error:
            child.update(status="target_unavailable", target_email=target_email, message=f"目标邮箱池暂不可用: {str(error)[:260]}")
            internal_items.append(child)
            continue
        if not isinstance(mailbox, dict):
            child.update(status="target_unavailable", target_email=target_email, message="邮箱池返回了无效候选")
            internal_items.append(child)
            continue
        address = str(mailbox.get("address") or "").strip().lower()
        if address in reserved_targets:
            # A provider may rotate back to the same candidate under load.
            # Never put two workers behind one mailbox; return this candidate
            # immediately and keep the first reservation authoritative.
            await asyncio.to_thread(_state().release_mailbox, mailbox)
            child.update(status="target_conflict", target_email=address, message="邮箱池返回了本批次已预留的目标邮箱")
            internal_items.append(child)
            continue
        reserved_targets.add(address)
        child.update(
            status="eligible",
            mailbox=mailbox,
            email=address,
            target_email=address,
            message="已匹配未占用邮箱",
        )
        internal_items.append(child)

    eligible_items = [item for item in internal_items if item.get("status") == "eligible"]
    reservation_id = ""
    if eligible_items:
        reservation_id = secrets.token_urlsafe(24)
        batch = {
            "batch": True,
            "items": eligible_items,
            "created_at": created_at,
            "created_monotonic": created_monotonic,
            "config": cfg,
            # Compatibility fields make diagnostics and old integrations able
            # to inspect a one-item batch without special casing it.
            "record_key": eligible_items[0].get("record_key") if len(eligible_items) == 1 else "",
        }
        if len(eligible_items) == 1:
            # Keep the old single-row reservation fields for integrations
            # that inspect the process-local registry while the new batch
            # shape remains the source of truth for cleanup and start.
            single = eligible_items[0]
            batch.update(
                source=single.get("source"),
                mailbox=single.get("mailbox"),
                email=single.get("email"),
                provider_type=single.get("provider_type"),
                created_at=single.get("created_at"),
                created_monotonic=single.get("created_monotonic"),
            )
        try:
            _rebind_register_reservation(reservation_id, batch)
        except ValueError as error:
            _rebind_release_reservation_item(batch)
            raise HTTPException(409, str(error)) from error
        except Exception:
            _rebind_release_reservation_item(batch)
            raise

    def public_item(item: dict[str, Any]) -> dict[str, Any]:
        source = item.get("source") if isinstance(item.get("source"), dict) else None
        result = {
            "input": str(item.get("input") or ""),
            "status": str(item.get("status") or ""),
            "message": str(item.get("message") or ""),
            "source": source,
            "record_key": str(item.get("record_key") or ""),
        }
        if item.get("missing"):
            result["missing"] = list(item.get("missing") or [])
        if item.get("target_email"):
            result["target_email"] = str(item.get("target_email"))
        if item.get("mailbox"):
            result["target"] = _rebind_reservation_payload(item, reservation_id)["target"]
        return result

    public_items = [public_item(item) for item in internal_items]
    matched = sum(1 for item in internal_items if item.get("source"))
    missing_credentials = [
        {"email": str(item.get("source", {}).get("email") or item.get("input") or ""), "missing": item.get("missing") or []}
        for item in internal_items
        if item.get("status") == "missing_credentials"
    ]
    not_found = [str(item.get("input") or "") for item in internal_items if item.get("status") == "not_found"]
    conflicts = [
        f"{item.get('input')}: {item.get('message')}"
        for item in internal_items
        if item.get("status") in {"target_conflict", "target_invalid", "target_unavailable"}
    ]
    first_source = next((item.get("source") for item in internal_items if item.get("source")), None)
    first_target = next((item.get("target") for item in public_items if item.get("target")), None)
    eligible_count = len(eligible_items)
    if eligible_count:
        message = (
            f"已匹配 {eligible_count} 个可用目标邮箱，可开始批量换绑"
            if eligible_count > 1
            else "已匹配未占用邮箱，可开始换绑"
        )
    elif not_found and matched == 0:
        message = "未找到当前账号"
    else:
        message = "没有可执行的换绑账号"
    return {
        "ok": True,
        "requested": requested,
        "matched": matched,
        "eligible_count": eligible_count,
        "eligible": bool(eligible_count),
        "source": first_source,
        "sources": [item.get("source") for item in internal_items if item.get("source")],
        "target": first_target,
        "items": public_items,
        "reservation_id": reservation_id,
        "invalid_format": invalid,
        "invalid_targets": invalid_targets,
        "not_found": not_found,
        "missing_credentials": missing_credentials,
        "conflicts": conflicts,
        "message": message,
    }


@router.post("/api/accounts/rebind/preflight/{reservation_id}/cancel")
async def account_rebind_reservation_cancel(reservation_id: str):
    """Release an unclaimed preflight mailbox immediately and idempotently."""
    value = str(reservation_id or "").strip()
    if not value or len(value) > 128:
        raise HTTPException(400, "换绑预检标识无效")
    released = await asyncio.to_thread(_rebind_discard_reservation, value)
    return {
        "ok": True,
        "reservation_id": value,
        "released": bool(released),
    }


@router.post("/api/accounts/rebind/start")
async def account_rebind_start(body: AccountRebindStartRequest):
    await asyncio.to_thread(_rebind_reservation_cleanup)
    reservation_id = str(body.reservation_id or "").strip()
    if not reservation_id:
        raise HTTPException(400, "请先完成换绑预检")
    with _state()._REBIND_RESERVATION_LOCK:
        item = _state()._REBIND_RESERVATIONS.get(reservation_id)
        if item is None:
            raise HTTPException(409, "换绑预检已过期，请重新检查邮箱")
        if time.monotonic() - float(item.get("created_monotonic") or 0) >= _state()._REBIND_RESERVATION_TTL:
            expired_item = _state()._REBIND_RESERVATIONS.pop(reservation_id, None)
            item = None
        else:
            expired_item = None
    if isinstance(expired_item, dict):
        await asyncio.to_thread(_rebind_release_reservation_item, expired_item)
    if item is None:
        raise HTTPException(409, "换绑预检已过期，请重新检查邮箱")
    # Claim the whole batch exactly once before doing the second identity
    # checks.  A second browser tab then receives a deterministic conflict
    # instead of racing individual mailbox candidates.
    with _state()._REBIND_RESERVATION_LOCK:
        claimed = _state()._REBIND_RESERVATIONS.pop(reservation_id, None)
    if claimed is None:
        raise HTTPException(409, "该换绑候选已被其它任务领取")

    children = _rebind_reservation_items(claimed)
    valid_records: list[dict[str, Any]] = []
    valid_reservations: dict[str, dict[str, Any]] = {}
    rejected: list[str] = []
    released_child_ids: set[int] = set()

    async def release_child(child: dict[str, Any]) -> None:
        marker = id(child)
        if marker in released_child_ids:
            return
        released_child_ids.add(marker)
        await asyncio.to_thread(_rebind_release_reservation_item, child)

    try:
        for child in children:
            source_key = str(child.get("record_key") or "")
            try:
                record = await asyncio.to_thread(_rebind_load_record, source_key)
            except Exception as error:
                rejected.append(f"{source_key}: 账号读取失败（{str(error)[:160]}）")
                await release_child(child)
                continue
            if record is None:
                rejected.append(f"{source_key}: 当前账号已不存在")
                await release_child(child)
                continue
            source_snapshot = child.get("source") if isinstance(child.get("source"), dict) else {}
            snapshot_email = str(source_snapshot.get("email") or "").strip().lower()
            current_email = str(record.get("email") or "").strip().lower()
            snapshot_account_id = str(source_snapshot.get("account_id") or "").strip().lower()
            current_account_id = str(record.get("account_id") or "").strip().lower()
            if current_email != snapshot_email or (
                snapshot_account_id and current_account_id != snapshot_account_id
            ):
                rejected.append(f"{snapshot_email or source_key}: 当前账号身份已变化")
                await release_child(child)
                continue
            mailbox_snapshot = child.get("mailbox") if isinstance(child.get("mailbox"), dict) else {}
            target_email = str(child.get("email") or mailbox_snapshot.get("address") or "").strip().lower()
            try:
                target_conflict = await asyncio.to_thread(_rebind_target_conflict, target_email)
            except Exception as error:
                target_conflict = f"目标占用检查失败（{str(error)[:160]}）"
            if target_conflict:
                rejected.append(f"{target_email}: {target_conflict}")
                await release_child(child)
                continue
            # The coordinator indexes reservations by the same stable key as the
            # account store. Keep both legacy aliases for integrations that inspect
            # the claimed payload directly.
            claimed_child = {
                **child,
                "email": target_email,
                "address": target_email,
            }
            current_key = _state()._account_key(record)
            if current_key in valid_reservations:
                rejected.append(f"{snapshot_email or source_key}: 本批次中账号身份重复")
                await release_child(child)
                continue
            valid_records.append(record)
            valid_reservations[current_key] = claimed_child
            if source_key and source_key != current_key:
                valid_reservations[source_key] = claimed_child
    except Exception:
        # The reservation was already claimed from the registry. If an
        # unexpected malformed row or store failure escapes the per-item
        # checks, release every child before surfacing the error.
        for child in children:
            await release_child(child)
        raise
    if not valid_records:
        detail = "；".join(rejected[:5]) or "没有可执行的换绑账号"
        raise HTTPException(409, f"批量换绑启动前校验失败：{detail}")
    try:
        start_kwargs: dict[str, Any] = {
            "concurrency": body.concurrency,
            "worker": _rebind_job_worker_factory(
                claimed.get("config")
                if isinstance(claimed.get("config"), dict)
                else await asyncio.to_thread(_state()._read_config)
            ),
        }
        if rejected:
            start_kwargs["skipped"] = rejected
        job = await asyncio.to_thread(
            _state().ACCOUNT_REBIND_JOBS.start,
            valid_records,
            valid_reservations,
            **start_kwargs,
        )
    except Exception:
        await asyncio.to_thread(_rebind_release_reservation_item, {"items": list(valid_reservations.values())})
        raise
    return {"ok": True, **job, "skipped": rejected}


@router.get("/api/accounts/rebind/progress")
async def account_rebind_progress():
    job = await asyncio.to_thread(_state().ACCOUNT_REBIND_JOBS.active)
    return {"ok": True, "job": job}


@router.get("/api/accounts/rebind/{job_id}")
async def account_rebind_job(job_id: str):
    job = await asyncio.to_thread(_state().ACCOUNT_REBIND_JOBS.snapshot, job_id)
    if job is None:
        raise HTTPException(404, "换绑任务不存在")
    return {"ok": True, "job": job}


@router.post("/api/accounts/rebind/{job_id}/stop")
async def account_rebind_stop(job_id: str):
    job = await asyncio.to_thread(_state().ACCOUNT_REBIND_JOBS.stop, job_id)
    if job is None:
        raise HTTPException(404, "换绑任务不存在")
    return {"ok": True, "job": job}



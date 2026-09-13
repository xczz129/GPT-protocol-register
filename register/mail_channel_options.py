"""Shared SMSBower registration options for API and worker entry points."""
from __future__ import annotations

from typing import Any

from mail.parsing import normalize_provider_type


def option_enabled(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def smsbower_entry(mail_config: dict[str, Any]) -> dict[str, Any] | None:
    for entry in mail_config.get("providers") or []:
        if isinstance(entry, dict) and normalize_provider_type(str(entry.get("type") or "")) == "smsbower_mail" and option_enabled(entry.get("enable")):
            return entry
    return None


def smsbower_child_enabled(mail_config: dict[str, Any]) -> bool:
    return any(
        isinstance(entry, dict)
        and normalize_provider_type(str(entry.get("type") or "")) == "smsbower_mail"
        and option_enabled(entry.get("enable"))
        and option_enabled(entry.get("register_child"))
        for entry in mail_config.get("providers") or []
    )


def is_smsbower_selected(provider_type: str | None) -> bool:
    return normalize_provider_type(str(provider_type or "")) == "smsbower_mail"


def needs_dynamic_mail_coordinator(mail_config: dict[str, Any], provider_type: str | None) -> bool:
    selected = str(provider_type or "auto").strip().lower()
    return is_smsbower_selected(selected) or (
        selected in {"auto", "", "all", "any"} and smsbower_entry(mail_config) is not None
    )


def validate_smsbower_job(mail_config: dict[str, Any], provider_type: str | None, total: int, concurrency: int, *, fixed_email: str | None = None) -> None:
    if not is_smsbower_selected(provider_type):
        return
    if fixed_email:
        raise ValueError("SMSBower 不支持指定固定邮箱，请从邮箱池分配")
    if concurrency < 2 or concurrency % 2:
        raise ValueError("SMSBower 并发数必须至少为 2，且必须是偶数")
    if smsbower_child_enabled(mail_config) and (total < 2 or total % 2):
        raise ValueError("启用子号时，总账号数必须至少为 2，且必须是偶数（每组一个主号和一个子号）")

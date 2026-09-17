# -*- coding: utf-8 -*-
"""打野协议：注册流撞到已有账号时，用买到的邮箱接管该账号。

邮箱在我们手里 = 登录验证码在我们手里。复用安全补全引擎
（repair_security_one）的「邮箱验证码登录 → 设置密码 → 登记 TOTP 2FA →
读取 AT」链路，把号商之前用该邮箱注册的账号回收成我们自己的号
（email + password + totp_secret）。失败时返回 None，调用方回退到
原本的「记为已注册并跳过」行为。

配置开关：registration.harvest_registered: true
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Callable

from register.protocol_runtime import ProtocolRuntimeSettings


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def harvest_registered_account(
    index: int,
    *,
    mailbox: dict[str, Any] | None,
    reg_cfg: dict[str, Any] | None,
    mail_config: dict[str, Any] | None,
    proxy: str,
    proxy_cfg: dict[str, Any] | None = None,
    stop_check: Callable[[], bool] | None = None,
    task_event=None,
) -> dict[str, Any] | None:
    """接管一个已注册邮箱对应的账号；成功返回完整账号记录，失败返回 None。

    注册流在 authorize/continue 判定「已有账号」时调用；此时邮箱租约
    仍在当前任务手上（ODM 订单有效、可收码），直接复用安全补全引擎做
    邮箱验证码登录，会话建立后设置我们的密码并登记 TOTP 2FA。
    """
    email = str((mailbox or {}).get("address") or "").strip()
    if not email:
        return None
    reg = reg_cfg if isinstance(reg_cfg, dict) else {}
    mail = dict(mail_config if isinstance(mail_config, dict) else {})
    provider_type = str((mailbox or {}).get("provider") or "") or None

    from register.protocol_ops import repair_security_one

    runtime = ProtocolRuntimeSettings.from_config(reg)
    runtime = runtime.with_deadline(time.monotonic() + runtime.account_timeout)

    def _run() -> dict[str, Any]:
        return repair_security_one(
            index,
            account={
                "email": email,
                "provider_type": provider_type or "",
            },
            mail_config=mail,
            proxy=proxy,
            provider_type=provider_type,
            stop_check=stop_check,
            task_event=task_event,
            proxy_config=proxy_cfg,
            protocol_runtime=runtime,
            mailbox_override=mailbox,
        )

    try:
        security = await asyncio.to_thread(_run)
    except Exception as error:
        if callable(task_event):
            task_event(index, "running", step=f"打野失败: {str(error)[:140]}")
        return None
    if not isinstance(security, dict) or not str(security.get("email") or "").strip():
        return None

    record = dict(security)
    record.setdefault("email", email)
    record["source_type"] = "harvest"
    record["registration_state"] = "complete"
    record["harvested_at"] = _now_iso()
    if callable(task_event):
        task_event(index, "running", step="打野成功：已接管已有账号（新密码 + 2FA 已设置）")
    return record

#!/usr/bin/env python
"""跳过账号回收：把注册历史里 skipped 的存量账号收成可用账号。

对 already_registered / password_reset_required 且邮箱仍可收码的跳过账号，
按 2026-09-17 手动打野抓包（D:\\Downloads\\最新.csv）的流程回收：
  1. 邮箱验证码登录（signin 无特殊参数 → email-otp → callback）
  2. 登录态重置密码（reauth=password + post_login_password_reset 事务
     → 事务内 OTP → password/reset → callback）
  3. 设置 2FA（mfa/enroll → activate_enrollment）→ 入库
面板注册状态：正常=注册成功，回收=打野成功。

用法:
  .venv/bin/python scripts/recover_skipped_accounts.py --dry-run
  .venv/bin/python scripts/recover_skipped_accounts.py --dry-run --domain icloud.com
  .venv/bin/python scripts/recover_skipped_accounts.py --email some@icloud.com
  .venv/bin/python scripts/recover_skipped_accounts.py --domain icloud.com \
      --reason password_reset_required --limit 5 --concurrency 2

结果输出: data/recover_skipped_results_<时间戳>.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sqlite3
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RECOVERABLE_REASONS = ("already_registered", "password_reset_required")

PRINT_LOCK = threading.Lock()
RESULTS_LOCK = threading.Lock()
RESULTS: list[dict] = []


def log(message: str) -> None:
    with PRINT_LOCK:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def _load_yaml_config() -> dict:
    import yaml

    with open(ROOT / "config.yaml", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _resolve_proxies(cfg: dict) -> list[str]:
    from proxy_util import resolve_proxy_pool

    proxy_cfg = cfg.get("proxy") if isinstance(cfg.get("proxy"), dict) else {}
    return resolve_proxy_pool(proxy_cfg) if proxy_cfg.get("enabled") else []


def load_candidates(db_path: Path, *, domain: str = "", reason: str = "") -> list[dict]:
    """从 task_history 读 skipped 卡片，按邮箱去重（保留最新一条）。"""
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT payload, updated_at_epoch FROM task_cards ORDER BY updated_at_epoch"
        ).fetchall()
    finally:
        con.close()
    latest: dict[str, dict] = {}
    for payload_text, updated in rows:
        try:
            payload = json.loads(payload_text)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("status") or "").lower() != "skipped":
            continue
        email = str(payload.get("email") or "").strip().lower()
        if not email or "@" not in email:
            continue
        skip_reason = str(payload.get("skip_reason") or "").strip()
        if domain and not email.endswith("@" + domain.lstrip("@").lower()):
            continue
        if reason:
            if reason.lower() not in skip_reason.lower():
                continue
        elif skip_reason not in RECOVERABLE_REASONS:
            continue
        card = {
            "email": email,
            "skip_reason": skip_reason,
            "batch_id": str(payload.get("batch_id") or ""),
            "updated_at_epoch": float(updated or 0),
        }
        prev = latest.get(email)
        if prev is None or card["updated_at_epoch"] >= prev["updated_at_epoch"]:
            latest[email] = card
    return sorted(latest.values(), key=lambda c: -c["updated_at_epoch"])


def _is_rate_limited(error: BaseException) -> bool:
    text = str(error)
    return (
        "429" in text
        or "too many requests" in text.lower()
        or "rate_limit_exceeded" in text
        or "rate limit" in text.lower()
    )


def _is_connection_error(error: BaseException) -> bool:
    text = str(error).lower()
    markers = (
        "curl: (7)", "curl: (28)", "curl: (35)", "curl: (56)",
        "tls connect error", "wrong_version_number", "ssl_connect",
        "connection closed", "connection reset", "connection aborted",
        "timeout", "timed out", "connection error",
    )
    return any(marker in text for marker in markers)


def _is_login_route_retryable(error: BaseException) -> bool:
    """登录路由/会话层临时错误：换事务重试通常可恢复（密码已重置时尤其值得）。"""
    text = str(error)
    return any(
        marker in text
        for marker in (
            "password_route_unavailable",
            "chatgpt_session_incomplete",
            "pending_recovery_unexpected_login_step",
            "pending_recovery_auth_step_loop",
            "invalid_state",
            "pending_recovery_email_otp_timeout",
            "验证码超时",
            "验证码获取超时",
            "unexpected_auth_step",
            "未完成 OAuth callback",
        )
    )


def _save_partial_record(
    email: str, password: str, provider: str, group_id: str, error_text: str
) -> None:
    """密码已重置但会话/2FA 步骤失败时落一个 security_pending 记录，防止新密码丢失。"""
    from common.account_store import (
        ACCOUNTS_FILE,
        load_account_records_by_emails,
        upsert_account_record,
    )

    record = {
        "email": email,
        "password": password,
        "provider_type": provider,
        "registration_state": "security_pending",
        "last_security_error": error_text[:300],
        "group_id": group_id,
    }
    existing = load_account_records_by_emails({email}, ACCOUNTS_FILE)
    if existing:
        record = {
            **existing[0],
            **{k: v for k, v in record.items() if v not in ("", None)},
        }
    upsert_account_record(record, ACCOUNTS_FILE, create=True)


def recover_one(
    seq: int, cand: dict, cfg: dict, mail_cfg: dict, proxies: list[str], group_id: str
) -> dict:
    from register.protocol_context import (
        _FIXED_EMAIL,
        _MAIL_CONFIG,
        _PROVIDER_TYPE,
        _PROXY_CONFIG,
        _STOP_CHECK,
        _TASK_EVENT,
        PasswordResetRequired,
    )
    from register.protocol_runtime import ProtocolRuntimeSettings, activate_runtime
    from register.protocol_registrar import PlatformRegistrar
    from register.protocol import _random_password, create_mailbox
    from mail import mark_mailbox_result
    from common.account_store import (
        ACCOUNTS_FILE,
        load_account_records_by_emails,
        upsert_account_record,
    )

    email = cand["email"]
    index = seq + 1
    proxy = proxies[seq % len(proxies)] if proxies else ""
    provider = ""
    base = {
        "email": email,
        "result": "failed",
        "reason": "",
        "provider": "",
        "group": group_id,
    }
    time.sleep(random.uniform(1.0, 3.0))

    # contextvar 不跨线程传播，工作线程内自建上下文
    context_tokens = (
        (_TASK_EVENT, _TASK_EVENT.set(None)),
        (_STOP_CHECK, _STOP_CHECK.set(lambda: False)),
        (_FIXED_EMAIL, _FIXED_EMAIL.set(email)),
        (_PROVIDER_TYPE, _PROVIDER_TYPE.set(None)),
        (_MAIL_CONFIG, _MAIL_CONFIG.set(dict(mail_cfg))),
        (_PROXY_CONFIG, _PROXY_CONFIG.set({})),
    )
    runtime = replace(
        ProtocolRuntimeSettings.from_config(cfg.get("registration")),
        verify_chatgpt_me=True,
        enable_totp_after_register=True,
    )
    if runtime.deadline is None:
        runtime = runtime.with_deadline(time.monotonic() + runtime.account_timeout)
    runtime_context = activate_runtime(runtime)
    runtime_context.__enter__()

    registrar = None
    mailbox = None
    last_known_password = ""
    try:
        # split 子号(parent+split@)的验证码落主号收件箱：按主号分配邮箱，
        # 用主号 address+api_key 查询(HME 别名共用收件箱)
        from mail.parsing import base_mailbox_address

        receive_address = base_mailbox_address(email)
        mailbox = create_mailbox(
            receive_address, register_proxy=proxy, allow_used=True
        )
        if (
            str(mailbox.get("address") or "").strip().lower()
            != receive_address
        ):
            raise RuntimeError("mailbox_address_mismatch")
        provider = str(mailbox.get("provider") or "")
        base["provider"] = provider
        proxy_label = proxy.split("@", 1)[-1].split(":")[0] if proxy else "直连"

        registrar = PlatformRegistrar(proxy)
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                if not last_known_password:
                    # 上一轮已重置过密码(security_pending)的账号直接复用，
                    # 不再消耗一轮验证码重置
                    for saved in load_account_records_by_emails(
                        {email}, ACCOUNTS_FILE
                    ):
                        if (
                            str(saved.get("password") or "").strip()
                            and str(saved.get("registration_state") or "")
                            == "security_pending"
                        ):
                            last_known_password = str(saved["password"]).strip()
                            log(
                                f"REUSE {email} 沿用已重置密码，"
                                f"跳过重置只补会话和 2FA"
                            )
                            break
                # 手动抓包流程（2026-09-17）：先用邮箱验证码登录上去
                tokens = dict(registrar.login(email, mailbox, index))
                if not last_known_password:
                    # 登录态重置密码：reauth=password 事务内再验一次码
                    password = registrar._complete_loggedin_password_reset(
                        email, _random_password(), mailbox, index
                    )
                    last_known_password = password
                    # 密码重置 callback 后刷新会话令牌
                    tokens = registrar._read_chatgpt_session(
                        email, last_known_password, index
                    )
                security = registrar._enroll_totp(
                    tokens, index, replace_existing=True
                )
                source_type = (
                    "microsoft" if provider == "outlook_token" else "web"
                )
                record = registrar._build_registration_record(
                    email,
                    last_known_password,
                    tokens,
                    security,
                    source_type,
                    mailbox,
                    registration_state="complete",
                )
                # 库里已有该账号时合并，保留既有字段
                existing = load_account_records_by_emails({email}, ACCOUNTS_FILE)
                if existing:
                    record = {
                        **existing[0],
                        **{
                            k: v
                            for k, v in record.items()
                            if v not in ("", None)
                        },
                    }
                record["email"] = email
                record["group_id"] = group_id
                record["recovered_from_skipped"] = True
                upsert_account_record(record, ACCOUNTS_FILE, create=True)
                # 面板可见的注册状态：正常注册=注册成功，回收=打野成功
                try:
                    from common.account_operation_log import record_operation_event

                    record_operation_event(
                        record,
                        "registration",
                        "success",
                        message="打野成功",
                        metadata={
                            "recovered_from_skipped": True,
                            "provider": provider,
                        },
                        secrets=(
                            last_known_password,
                            str(record.get("totp_secret") or ""),
                            str(record.get("access_token") or ""),
                            str(record.get("refresh_token") or ""),
                        ),
                    )
                except Exception:
                    pass
                mark_mailbox_result(mailbox, success=True)
                log(
                    f"OK   {email} 打野成功（密码已重置+2FA 已设置）"
                    f"provider={provider} via={proxy_label}"
                )
                return {**base, "result": "success", "reason": ""}
            except PasswordResetRequired as error:
                mark_mailbox_result(
                    mailbox,
                    success=bool(mailbox.get("_otp_accepted_by_server")),
                    error=str(error),
                )
                log(f"SKIP  {email} 密码路线仍被要求: {str(error)[:100]}")
                return {
                    **base,
                    "result": "skipped",
                    "reason": f"password_reset_required: {str(error)[:200]}",
                }
            except Exception as error:
                safe = str(error)
                retryable = (
                    _is_rate_limited(error)
                    or _is_connection_error(error)
                    or _is_login_route_retryable(error)
                ) and attempt < max_attempts - 1
                if retryable:
                    if proxies:
                        proxy = proxies[(seq + attempt + 1) % len(proxies)]
                    registrar.close()
                    registrar = PlatformRegistrar(proxy)
                    wait = (
                        random.uniform(30, 60)
                        if _is_rate_limited(error)
                        else random.uniform(2, 5)
                    )
                    log(
                        f"RETRY {email} 第{attempt + 1}次失败({safe[:60]})，"
                        f"换线等 {wait:.0f}s 重试"
                    )
                    time.sleep(wait)
                    continue
                if mailbox is not None:
                    mark_mailbox_result(
                        mailbox,
                        success=bool(mailbox.get("_otp_accepted_by_server")),
                        error=safe[:300],
                    )
                # 密码已重置但会话/2FA 失败：落 security_pending 防止新密码丢失
                if last_known_password:
                    try:
                        _save_partial_record(
                            email, last_known_password, provider, group_id, safe
                        )
                        safe = f"密码已重置但后续步骤失败(已存 pending): {safe}"
                    except Exception as save_error:
                        safe = (
                            f"密码已重置但入库失败"
                            f"({str(save_error)[:80]}): {safe}"
                        )
                log(f"FAIL  {email} {safe[:140]}")
                return {**base, "reason": safe[:300]}
        return {**base, "reason": "重试后仍失败"}
    except Exception as error:
        safe = str(error)
        if mailbox is not None:
            try:
                mark_mailbox_result(mailbox, success=False, error=safe[:300])
            except Exception:
                pass
        log(f"FAIL  {email} 邮箱分配/初始化失败: {safe[:140]}")
        return {**base, "reason": f"mailbox: {safe[:280]}"}
    finally:
        if registrar is not None:
            try:
                registrar.close()
            except Exception:
                pass
        runtime_context.__exit__(None, None, None)
        for variable, token in reversed(context_tokens):
            variable.reset(token)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db", default=str(ROOT / "data" / "task_history.sqlite3"),
        help="任务历史库路径",
    )
    parser.add_argument("--dry-run", action="store_true", help="只列候选不执行")
    parser.add_argument("--email", default="", help="直接指定单个邮箱(绕过历史库)")
    parser.add_argument("--domain", default="", help="只处理该域名, 如 icloud.com")
    parser.add_argument(
        "--reason", default="",
        help="只处理该 skip_reason; 默认 already_registered+password_reset_required",
    )
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条")
    parser.add_argument("--concurrency", type=int, default=2, help="并发数")
    parser.add_argument("--group", default="回收跳过账号", help="目标分组名")
    parser.add_argument("--direct", action="store_true", help="不走代理池直连")
    args = parser.parse_args()

    if args.email:
        candidates = [
            {
                "email": args.email.strip().lower(),
                "skip_reason": "manual",
                "batch_id": "",
                "updated_at_epoch": 0.0,
            }
        ]
    else:
        candidates = load_candidates(
            Path(args.db), domain=args.domain, reason=args.reason
        )
    if not candidates:
        raise SystemExit("没有匹配的候选账号")

    if args.limit > 0:
        candidates = candidates[: args.limit]

    # 跳过账号库中已完整可用（密码+TOTP+AT）的账号，避免重置已回收账号
    try:
        from common.account_store import (
            ACCOUNTS_FILE,
            load_account_records_by_emails,
        )

        existing = load_account_records_by_emails(
            {c["email"] for c in candidates}, ACCOUNTS_FILE
        )
        complete = {
            str(r.get("email") or "").strip().lower()
            for r in existing
            if str(r.get("password") or "").strip()
            and str(r.get("totp_secret") or "").strip()
            and str(r.get("access_token") or "").strip()
        }
        before = len(candidates)
        candidates = [c for c in candidates if c["email"] not in complete]
        if before > len(candidates):
            log(f"跳过账号库中已完整可用的 {before - len(candidates)} 个")
    except Exception as error:
        log(f"账号库检查失败(继续全量): {error}")
    if not candidates:
        log("没有需要回收的账号（全部已完整可用）")
        return

    domain_counts = Counter(c["email"].split("@")[-1] for c in candidates)
    reason_counts = Counter(c["skip_reason"] for c in candidates)
    log(
        f"候选 {len(candidates)} 个: "
        + ", ".join(f"@{d}={n}" for d, n in domain_counts.most_common())
    )
    log(
        "skip_reason: "
        + ", ".join(f"{r}={n}" for r, n in reason_counts.most_common())
    )
    if args.dry_run:
        for card in candidates[:80]:
            log(f"  {card['email']:<40} {card['skip_reason']}")
        if len(candidates) > 80:
            log(f"  ... 其余 {len(candidates) - 80} 个省略")
        return

    cfg = _load_yaml_config()
    mail_cfg = cfg.get("mail") if isinstance(cfg.get("mail"), dict) else {}
    if not (mail_cfg.get("providers") or []):
        raise SystemExit("config.yaml mail.providers 为空, 无法收码")
    proxies = [] if args.direct else _resolve_proxies(cfg)
    log(
        f"代理线路 {len(proxies)} 条" + ("(直连模式)" if args.direct else "")
        + f", 并发 {args.concurrency}"
    )

    from common.account_groups import create_group, list_groups

    group = next(
        (
            g
            for g in list_groups()
            if str(g.get("name") or "").strip() == args.group
        ),
        None,
    ) or create_group(args.group)
    group_id = group.get("id") or ""
    log(f"分组「{args.group}」 id={group_id}, 开始回收 {len(candidates)} 个账号")

    started = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = [
            pool.submit(recover_one, seq, cand, cfg, mail_cfg, proxies, group_id)
            for seq, cand in enumerate(candidates)
        ]
        for future in as_completed(futures):
            result = future.result()
            with RESULTS_LOCK:
                RESULTS.append(result)

    success = sum(1 for r in RESULTS if r["result"] == "success")
    skipped = sum(1 for r in RESULTS if r["result"] == "skipped")
    failed = sum(1 for r in RESULTS if r["result"] == "failed")
    out_path = ROOT / "data" / (
        f"recover_skipped_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    )
    with open(out_path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["email", "result", "reason", "provider", "group"],
        )
        writer.writeheader()
        order = {"success": 0, "skipped": 1, "failed": 2}
        for row in sorted(
            RESULTS, key=lambda r: (order[r["result"]], r["email"])
        ):
            writer.writerow(row)
    minutes = (time.time() - started) / 60
    log(
        f"完成: 成功 {success} / 跳过 {skipped} / 失败 {failed},"
        f"耗时 {minutes:.1f} 分钟, 结果已保存 {out_path}"
    )


if __name__ == "__main__":
    main()

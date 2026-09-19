#!/usr/bin/env python
"""Recover failed ODM paid mailboxes whose remote orders are still alive.

Reads data/odm_failed_probe.csv (probe result bc_401 means the order is
still waiting and can receive mail), then re-runs a full protocol
registration on each mailbox through the already-paid order.
"""
from __future__ import annotations

import argparse
import csv
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PRINT_LOCK = threading.Lock()
RESULTS: list[dict] = []


def log(message: str) -> None:
    with PRINT_LOCK:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def load_candidates(probe_csv: Path, reason_filter: str) -> list[dict]:
    import json
    state = json.load(open(ROOT / "data" / "outlook_token_state.json", encoding="utf-8"))
    rows = list(csv.DictReader(open(probe_csv, encoding="utf-8")))
    out = []
    for r in rows:
        if str(r.get("probe") or "") != "bc_401":
            continue
        entry = state.get(str(r.get("email") or "").strip().lower())
        if not isinstance(entry, dict) or str(entry.get("state") or "") != "failed":
            continue
        if reason_filter and reason_filter.lower() not in str(r.get("local_reason") or "").lower():
            continue
        out.append({
            "email": r["email"],
            "order_id": r["order_id"],
            "local_reason": str(r.get("local_reason") or "")[:90],
        })
    return out


def _load_yaml_config() -> dict:
    import yaml
    with open(ROOT / "config.yaml", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _resolve_proxies(cfg: dict) -> list[str]:
    from proxy_util import normalize_proxy_url, resolve_proxy_pool
    proxy_cfg = cfg.get("proxy") if isinstance(cfg.get("proxy"), dict) else {}
    if not proxy_cfg.get("enabled"):
        return []
    return [p for p in (normalize_proxy_url(v) for v in resolve_proxy_pool(proxy_cfg)) if p]


def recover_one(seq, cand, cfg, mail_cfg, proxies, group_id):
    from register.protocol_context import (
        _FIXED_EMAIL, _MAIL_CONFIG, _PROVIDER_TYPE, _PROXY_CONFIG,
        _STOP_CHECK, _TASK_EVENT,
    )
    from register.protocol_runtime import ProtocolRuntimeSettings, activate_runtime
    from register.protocol import create_mailbox, register_one
    from mail import mark_mailbox_result

    email = cand["email"]
    index = seq + 1
    proxy = proxies[seq % len(proxies)] if proxies else ""
    result = {"email": email, "order_id": cand["order_id"], "result": "failed", "reason": ""}

    context_tokens = (
        (_TASK_EVENT, _TASK_EVENT.set(None)),
        (_STOP_CHECK, _STOP_CHECK.set(lambda: False)),
        (_FIXED_EMAIL, _FIXED_EMAIL.set(email)),
        (_PROVIDER_TYPE, _PROVIDER_TYPE.set("online_disposablemail")),
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
    mailbox = None
    try:
        time.sleep(random.uniform(0.5, 2.0))
        mailbox = create_mailbox(email, register_proxy=proxy, allow_used=True)
        mailbox.pop("_registration_outcome", None)
        record = register_one(
            index,
            mail_config=dict(mail_cfg),
            proxy=proxy,
            provider_type="online_disposablemail",
            mailbox_override=mailbox,
            protocol_runtime=runtime,
            group_id=group_id,
        )
        if record:
            try:
                mark_mailbox_result(mailbox, success=True)
            except Exception:
                pass
            log(f"OK   {email} re-registered successfully")
            return {**result, "result": "success"}
        log(f"FAIL {email} register_one returned no record")
        return {**result, "reason": "register_one returned no record"}
    except Exception as error:
        safe = str(error)
        if mailbox is not None:
            try:
                mark_mailbox_result(
                    mailbox,
                    success=bool(mailbox.get("_otp_accepted_by_server")),
                    error=safe[:300],
                )
            except Exception:
                pass
        log(f"FAIL {email} {safe[:140]}")
        return {**result, "reason": safe[:300]}
    finally:
        try:
            runtime_context.__exit__(None, None, None)
        except Exception:
            pass
        for variable, token in reversed(context_tokens):
            variable.reset(token)


def main() -> None:
    parser = argparse.ArgumentParser(description="recover failed ODM paid mailboxes")
    parser.add_argument("--probe-csv", default=str(ROOT / "data" / "odm_failed_probe.csv"))
    parser.add_argument("--reason", default="", help="substring filter on local failure reason")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--group", default="odm-recovered")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = _load_yaml_config()
    mail_cfg = dict(cfg.get("mail") or {})
    mail_cfg.setdefault("wait_timeout", 300)
    mail_cfg.setdefault("wait_chunk", 4)
    mail_cfg.setdefault("request_timeout", 20)
    proxies = _resolve_proxies(cfg)
    candidates = load_candidates(Path(args.probe_csv), args.reason)
    if args.limit:
        candidates = candidates[:args.limit]
    print(f"candidates={len(candidates)} proxies={len(proxies)} concurrency={args.concurrency} dry_run={args.dry_run}")
    for c in candidates[:10]:
        print("  cand:", c["email"], "|", c["local_reason"][:60])
    if args.dry_run or not candidates:
        return

    started = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(recover_one, i, c, cfg, mail_cfg, proxies, args.group): c
            for i, c in enumerate(candidates)
        }
        for fut in as_completed(futures):
            RESULTS.append(fut.result())
    ok = sum(1 for r in RESULTS if r["result"] == "success")
    print(f"=== done in {time.time() - started:.0f}s: {ok}/{len(RESULTS)} recovered ===")
    out = ROOT / "data" / f"odm_recovery_results_{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["email", "order_id", "result", "reason"])
        w.writeheader()
        for r in RESULTS:
            w.writerow(r)
    print("results saved:", out)


if __name__ == "__main__":
    main()

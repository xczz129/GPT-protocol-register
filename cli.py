# -*- coding: utf-8 -*-
"""CLI for fingerprint-browser ChatGPT registration."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config_loader import load_config, write_example_config
from register_gpt import run_register


def cmd_init(args) -> int:
    dest = Path(args.output).expanduser() if getattr(args, "output", None) else (ROOT / "config.yaml")
    if dest.exists() and not getattr(args, "force", False):
        print(f"already exists: {dest} (use --force to overwrite)")
        return 1
    example = ROOT / "config.example.yaml"
    if example.is_file():
        dest.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        write_example_config(dest)
    print(f"wrote {dest}")
    print("Edit mail.providers[].mailboxes with:")
    print("  email----password----client_id----refresh_token")
    return 0


def cmd_register(args) -> int:
    config = load_config(getattr(args, "config", None))
    if getattr(args, "engine", None):
        config.setdefault("registration", {})["engine"] = args.engine
    total = args.count if getattr(args, "count", None) is not None else None
    conc = args.concurrency if getattr(args, "concurrency", None) is not None else None
    results = asyncio.run(
        run_register(
            config,
            total=total,
            concurrency=conc,
            keep_on_fail=bool(getattr(args, "keep_on_fail", False)),
            fixed_email=getattr(args, "email", None),
            provider_type=getattr(args, "mail_type", None) or getattr(args, "provider", None),
        )
    )
    return 0 if results else 1


def cmd_login(args) -> int:
    config = load_config(getattr(args, "config", None))
    from register.protocol import run_protocol_login, run_protocol_relogin

    if getattr(args, "all_needed", False):
        summary = asyncio.run(
            run_protocol_relogin(
                config,
                total=getattr(args, "count", None),
                concurrency=getattr(args, "concurrency", None),
            )
        )
        return 0 if summary["failed"] == 0 else 1

    if not str(getattr(args, "email", None) or "").strip():
        print("login 需要 --email，或使用 --all-needed 批量处理")
        return 2

    results = asyncio.run(
        run_protocol_login(
            config,
            total=getattr(args, "count", None),
            concurrency=getattr(args, "concurrency", None),
            fixed_email=getattr(args, "email", None),
            provider_type=getattr(args, "mail_type", None),
        )
    )
    return 0 if results else 1


def cmd_health(args) -> int:
    from common.account_health import check_accounts

    result = check_accounts(workers=max(1, int(getattr(args, "concurrency", None) or 5)))
    print(
        "测活完成 "
        f"requested={result['requested']} checked={result['checked']} "
        f"skipped_deactivated={result['skipped_known_deactivated']} "
        f"alive={result['alive']} limited={result['limited']} "
        f"invalid={result['invalid']} error={result['error']}"
    )
    return 0 if result["requested"] and not result["error"] else 1


def cmd_web(args) -> int:
    from web_app import main as web_main

    argv = []
    if getattr(args, "host", None):
        argv.extend(["--host", str(args.host)])
    if getattr(args, "port", None):
        argv.extend(["--port", str(args.port)])
    if getattr(args, "reload", False):
        argv.append("--reload")
    web_main(argv)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gpt-register-fp",
        description="ChatGPT registration via protocol or fingerprint browser",
    )
    parser.add_argument(
        "-c",
        "--config",
        default=None,
        help="config.yaml path (default: ./config.yaml)",
    )
    # top-level flags so `python cli.py -n 1` works without subcommand
    parser.add_argument("-n", "--count", type=int, default=None, help="How many accounts")
    parser.add_argument("-j", "--concurrency", type=int, default=None, help="Parallel browsers")
    parser.add_argument("--email", default=None, help="Force a specific mailbox address")
    parser.add_argument("--keep-on-fail", action="store_true", help="Keep browser window on failure")
    parser.add_argument("--engine", choices=["browser", "protocol"], default=None)
    parser.add_argument(
        "--mail-type",
        "--provider",
        dest="mail_type",
        default=None,
        choices=["auto", "outlook", "icloud", "maddy", "fastmail", "outlook_token", "icloud_api", "maddy_webui", "fastmail_api"],
        help="Mailbox pool: auto / outlook / icloud / maddy / fastmail",
    )

    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="Write config.yaml from example")
    p_init.add_argument("-o", "--output", default=None, help="Output path")
    p_init.add_argument("-f", "--force", action="store_true")
    p_init.set_defaults(func=cmd_init)

    p_reg = sub.add_parser("register", help="Run GPT registration")
    p_reg.add_argument("-n", "--count", type=int, default=None, help="How many accounts")
    p_reg.add_argument("-j", "--concurrency", type=int, default=None, help="Parallel browsers")
    p_reg.add_argument("--email", default=None, help="Force a specific mailbox address")
    p_reg.add_argument("--keep-on-fail", action="store_true", help="Keep browser window on failure")
    p_reg.add_argument("--engine", choices=["browser", "protocol"], default=None)
    p_reg.add_argument(
        "--mail-type",
        "--provider",
        dest="mail_type",
        default=None,
        choices=["auto", "outlook", "icloud", "maddy", "fastmail", "outlook_token", "icloud_api", "maddy_webui", "fastmail_api"],
        help="Mailbox pool: auto / outlook / icloud / maddy / fastmail",
    )
    p_reg.set_defaults(func=cmd_register)

    p_login = sub.add_parser("login", help="Run ChatGPT protocol login")
    login_mode = p_login.add_mutually_exclusive_group(required=True)
    login_mode.add_argument("--email", help="Existing account email")
    login_mode.add_argument(
        "--all-needed",
        action="store_true",
        help="Re-login recorded accounts in invalid/reauth-required state",
    )
    p_login.add_argument("-n", "--count", type=int, default=None)
    p_login.add_argument("-j", "--concurrency", type=int, default=3)
    p_login.add_argument(
        "--mail-type",
        "--provider",
        dest="mail_type",
        default=None,
        choices=["auto", "outlook", "icloud", "maddy", "fastmail", "outlook_token", "icloud_api", "maddy_webui", "fastmail_api"],
        help="Mailbox pool: auto / outlook / icloud / maddy / fastmail",
    )
    p_login.set_defaults(func=cmd_login)

    p_health = sub.add_parser("health", help="Refresh and health-check local accounts")
    p_health.add_argument("-j", "--concurrency", type=int, default=5)
    p_health.set_defaults(func=cmd_health)

    p_web = sub.add_parser("web", help="Start local web panel")
    p_web.add_argument("--host", default=None, help="Bind host (default from config web.host / 127.0.0.1)")
    p_web.add_argument("--port", type=int, default=None, help="Bind port (default from config web.port / 8787)")
    p_web.add_argument("--reload", action="store_true", help="Auto-reload (dev)")
    p_web.set_defaults(func=cmd_web)

    parser.set_defaults(func=cmd_register)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    code = args.func(args)
    raise SystemExit(code if isinstance(code, int) else 0)


if __name__ == "__main__":
    main()

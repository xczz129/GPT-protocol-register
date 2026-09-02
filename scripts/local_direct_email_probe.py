#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Probe the Camoufox email UI over local direct traffic only."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.browser import open_and_connect, teardown
from register.page import EMAIL_INPUT_SELECTOR, submit_email_browser_ui, wait_for_any
from register.util import SIGNUP_URL


async def run(timeout: float) -> dict:
    os.environ.update(
        FINGERPRINT_BROWSER="camoufox",
        CAMOUFOX_HEADLESS="1",
        CAMOUFOX_OS="windows",
        CAMOUFOX_LOCALE="en-US",
        CAMOUFOX_GEOIP="0",
        CAMOUFOX_HUMANIZE="0.15",
    )
    browser_manager = profile_id = None
    auth_requests: list[tuple[str, str]] = []
    try:
        browser_manager, profile_id, _browser, _context, page = (
            await open_and_connect("local_direct_email_probe", proxy=None)
        )

        def capture(request) -> None:
            url = str(request.url or "")
            if "/api/auth/" in url or "auth.openai.com" in url:
                auth_requests.append((str(request.method or "GET"), url))

        page.on("request", capture)
        response = await page.goto(SIGNUP_URL, timeout=30000, wait_until="commit")
        input_found = await wait_for_any(
            page, selectors=[EMAIL_INPUT_SELECTOR], timeout=15, poll=0.25
        )
        email = f"probe-{uuid.uuid4().hex[:12]}@icloud.com"
        attempts: list[str] = []
        if input_found:
            for _ in range(2):
                try:
                    status = await asyncio.wait_for(
                        submit_email_browser_ui(page, email), timeout=timeout
                    )
                except asyncio.TimeoutError:
                    status = "timeout"
                attempts.append(status)
                if status not in {"routed", "form_unstable"}:
                    break
        for _ in range(40):
            if any(
                "/api/auth/signin/" in url
                or "api/accounts/authorize" in url
                for _, url in auth_requests
            ) or "auth.openai.com" in page.url:
                break
            await asyncio.sleep(0.25)
        return {
            "traffic": "local-direct",
            "proxy_used": False,
            "document_status": response.status if response else None,
            "input_found": input_found,
            "submit_attempts": attempts,
            "signin_or_authorize_seen": any(
                "/api/auth/signin/" in url
                or "api/accounts/authorize" in url
                for _, url in auth_requests
            ),
            "auth_requests": [
                [method, url.split("?", 1)[0]]
                for method, url in auth_requests[-8:]
            ],
            "final_host": urlparse(page.url).hostname,
            "final_path": urlparse(page.url).path,
        }
    finally:
        if browser_manager is not None and profile_id is not None:
            await teardown(browser_manager, profile_id, delete=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.timeout)), ensure_ascii=False))


if __name__ == "__main__":
    main()

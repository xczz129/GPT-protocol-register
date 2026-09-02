# -*- coding: utf-8 -*-
"""chatgpt2api (basketikun) client — POST /api/accounts with Bearer admin key."""

from __future__ import annotations

import glob
import json
import os
import time
from pathlib import Path
from typing import Any

from config import TOKEN_OUTPUT_DIR


def normalize_c2a_url(url: str = "") -> str:
    u = str(url or "").strip().rstrip("/")
    if not u:
        return ""
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u.rstrip("/")


def c2a_config_from(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize chatgpt2api section from full app config."""
    raw = (cfg or {}).get("chatgpt2api") if isinstance(cfg, dict) else None
    if not isinstance(raw, dict):
        raw = {}
    return {
        "enabled": bool(raw.get("enabled")),
        "url": normalize_c2a_url(str(raw.get("url") or "")),
        "key": str(raw.get("key") or "").strip(),
        "auto_upload": bool(raw.get("auto_upload", True)),
    }


def import_accounts(
    host: str,
    key: str,
    accounts: list[dict[str, Any]],
    *,
    retries: int = 4,
) -> tuple[bool, str, dict[str, Any] | None]:
    """POST {accounts:[...]} to <host>/api/accounts.

    Returns (ok, summary, response_json_or_none).
    Connection errors are retried; HTTP errors are not fatal for callers that
    only log (registration must not fail because upload failed).
    """
    import requests

    host = normalize_c2a_url(host)
    key = str(key or "").strip()
    if not host or not key:
        return False, "chatgpt2api URL/KEY 未配置", None
    if not accounts:
        return False, "无 account 可导入", None

    url = host + "/api/accounts"
    last_err = ""
    resp = None
    for attempt in range(max(1, retries)):
        try:
            resp = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json={"accounts": accounts},
                timeout=120,
            )
            break
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = str(e)[:120]
            if attempt < retries - 1:
                wait = 2 + attempt * 2
                time.sleep(wait)
                continue
            return False, f"请求失败(重试用尽): {last_err}", None
        except Exception as e:
            return False, f"请求失败: {str(e)[:120]}", None

    if resp is None:
        return False, f"请求失败: {last_err or 'no response'}", None

    data: dict[str, Any] | None = None
    try:
        parsed = resp.json()
        if isinstance(parsed, dict):
            data = parsed
    except Exception:
        data = None

    if resp.status_code >= 400:
        body = (resp.text or "")[:200]
        if data:
            body = str(data.get("detail") or data.get("error") or data.get("message") or body)[:200]
        return False, f"HTTP {resp.status_code}: {body}", data

    if data:
        summary = (
            f"added={data.get('added')} skipped={data.get('skipped')} "
            f"refreshed={data.get('refreshed')}"
        )
        if data.get("errors"):
            summary += f" errors={data['errors']}"
        return True, summary, data
    return True, f"HTTP {resp.status_code}: {(resp.text or '')[:120]}", None


def test_connection(host: str, key: str) -> tuple[bool, str]:
    """Probe chatgpt2api with admin key (GET /api/accounts preferred)."""
    import requests

    host = normalize_c2a_url(host)
    key = str(key or "").strip()
    if not host:
        return False, "请填写 chatgpt2api 网址"
    if not key:
        return False, "请填写 admin key"

    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    # Prefer accounts list (auth required); fall back to health/root
    for path in ("/api/accounts", "/api/health", "/health", "/"):
        url = host + path
        try:
            resp = requests.get(url, headers=headers, timeout=20)
        except (requests.ConnectionError, requests.Timeout) as e:
            return False, f"连接失败: {str(e)[:100]}"
        except Exception as e:
            return False, f"请求失败: {str(e)[:100]}"

        if path == "/api/accounts":
            if resp.status_code == 401 or resp.status_code == 403:
                return False, f"鉴权失败 HTTP {resp.status_code}（检查 admin key）"
            if resp.status_code >= 400:
                # some builds may not expose GET list — still try next
                continue
            try:
                data = resp.json()
            except Exception:
                data = None
            n = None
            if isinstance(data, list):
                n = len(data)
            elif isinstance(data, dict):
                for k in ("total", "count", "accounts"):
                    if k == "accounts" and isinstance(data.get(k), list):
                        n = len(data[k])
                        break
                    if k in data and isinstance(data[k], (int, float)):
                        n = int(data[k])
                        break
            msg = f"连接成功 · {host}"
            if n is not None:
                msg += f" · 号池约 {n} 个"
            return True, msg

        if resp.status_code < 500:
            # reachable; key not validated on public health
            if resp.status_code in (401, 403):
                return False, f"鉴权失败 HTTP {resp.status_code}"
            return True, f"服务可达 · {host} (HTTP {resp.status_code}) · 建议保存后点「上传已有账号」验证 key"

    return False, f"无法访问 {host}"


def collect_local_accounts(
    *,
    group_id: str = "",
    default_group_id: str = "",
    accounts_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Build import objects from the account store, with token files as fallback."""
    from common.account_store import (
        ACCOUNTS_FILE,
        account_is_deactivated,
        load_accounts,
        normalized_health_status,
    )
    from common.session_export import build_chatgpt2api_account

    selected_group = str(group_id or "").strip()
    records = load_accounts(accounts_path or ACCOUNTS_FILE)
    if records:
        selected: list[dict[str, Any]] = []
        for record in records:
            record_group = str(record.get("group_id") or "").strip() or str(
                default_group_id or ""
            ).strip()
            if selected_group and record_group != selected_group:
                continue
            if account_is_deactivated(record) or normalized_health_status(record) == "invalid":
                continue
            try:
                selected.append(
                    build_chatgpt2api_account(
                        record,
                        email=str(record.get("email") or "").strip(),
                    )
                )
            except Exception:
                continue
        return selected

    # Legacy fallback for installations that have token exports but no account store.
    if selected_group:
        return []
    cdir = os.path.join(TOKEN_OUTPUT_DIR, "chatgpt")
    by_token: dict[str, dict[str, Any]] = {}

    for path in sorted(glob.glob(os.path.join(cdir, "c2a-*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                item = json.load(f)
            if not isinstance(item, dict):
                continue
            tok = str(item.get("access_token") or "").strip()
            if tok:
                by_token[tok] = item
        except Exception:
            continue

    for path in sorted(glob.glob(os.path.join(cdir, "*.session.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                sess = json.load(f)
            name = Path(path).name[: -len(".session.json")]
            item = build_chatgpt2api_account(sess, email=name)
            tok = str(item.get("access_token") or "").strip()
            if tok and tok not in by_token:
                by_token[tok] = item
        except Exception:
            continue

    return list(by_token.values())


def upload_session(
    session: dict[str, Any] | None,
    email: str = "",
    *,
    host: str = "",
    key: str = "",
    cfg: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Build one c2a account from browser session and POST it."""
    if cfg is not None:
        c = c2a_config_from(cfg)
        host = host or c["url"]
        key = key or c["key"]
    try:
        from common.session_export import build_chatgpt2api_account

        account = build_chatgpt2api_account(session or {}, email=email)
    except Exception as e:
        return False, f"构建导入对象失败: {e}"
    ok, msg, _ = import_accounts(host, key, [account])
    return ok, msg

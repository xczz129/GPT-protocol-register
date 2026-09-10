# -*- coding: utf-8 -*-
# CLIProxyAPI 中转账号管理:直接读取本机 cliproxy 容器挂载的 auths 目录,
# 额度由面板直连 OpenAI usage 端点查询;全程不经过 cliproxy 的任何接口。

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import re
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

router = APIRouter()

# cli-proxy-api 容器把 auths 挂载在宿主机 /opt/codex-services/data/cliproxy/auths
AUTHS_DIR = Path("/opt/codex-services/data/cliproxy/auths")
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"
RESET_CREDITS_CONSUME_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume"
TOKEN_URL = "https://auth.openai.com/oauth/token"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
IMPERSONATE = "firefox147"
QUOTA_WORKERS = 6

_QUOTA_CACHE: dict[str, dict] = {}
_QUOTA_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _auth_files() -> list[Path]:
    if not AUTHS_DIR.is_dir():
        return []
    return sorted((p for p in AUTHS_DIR.glob("*.json") if p.is_file()), key=lambda p: p.name)


def _read_auth(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _jwt_payload(token: str) -> dict:
    if not isinstance(token, str) or token.count(".") < 2:
        return {}
    try:
        part = token.split(".", 2)[1]
        raw = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _claim(claims: dict, *keys: str) -> str:
    for key in keys:
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    auth = claims.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        for key in keys:
            value = auth.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _plan_from_record(record: dict) -> str:
    claims = _jwt_payload(str(record.get("id_token") or "")) or _jwt_payload(str(record.get("access_token") or ""))
    if claims:
        plan = _claim(claims, "chatgpt_plan_type", "plan_type", "planType")
        if plan:
            return plan
    return str(record.get("plan_type") or "").strip()


def _plan_from_filename(name: str) -> str:
    stem = name[:-5] if name.endswith(".json") else name
    if not stem.startswith("codex-") or "@" not in stem:
        return ""
    tail = stem.split("@", 1)[1]
    # 文件名形如 codex-{hash}-{email}-{plan};@ 之后的最后一个 '-' 后是 plan
    plan = tail.rsplit("-", 1)[-1] if "-" in tail else ""
    return re.sub(r"[^a-z0-9]+", "-", plan.lower()).strip("-") if plan else ""


def _fetch_usage(access_token: str, account_id: str, email: str) -> dict:
    from curl_cffi.requests import Session

    session = Session(impersonate=IMPERSONATE)
    device_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"cliproxy-quota:{email}"))
    try:
        session.cookies.set("oai-did", device_id, domain=".chatgpt.com", path="/")
    except Exception:
        pass
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "OAI-Device-Id": device_id,
    }
    if account_id:
        headers["Chatgpt-Account-Id"] = str(account_id)
    try:
        response = session.get(CODEX_USAGE_URL, headers=headers, timeout=30)
        if response.status_code != 200:
            raise RuntimeError(f"usage HTTP {response.status_code}")
        try:
            data = response.json()
        except Exception as exc:
            raise RuntimeError(f"usage 响应不是 JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError("usage 响应格式异常")
        return data
    finally:
        session.close()


def _consume_reset_credit(path: Path, credit_id: str = "") -> dict[str, Any]:
    """Consume one upstream Codex reset credit for a local CPA auth file."""
    from curl_cffi.requests import Session

    record = _read_auth(path)
    access_token = str(record.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("auth 文件缺少 access_token")
    account_id = str(record.get("account_id") or "").strip()
    email = str(record.get("email") or path.name).strip()
    device_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"cliproxy-quota:{email}"))
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "OAI-Device-Id": device_id,
        "OAI-Product-SKU": "CODEX",
        "Originator": "Codex Desktop",
    }
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id
    request_id = str(uuid.uuid4())
    payload: dict[str, str] = {"redeem_request_id": request_id}
    if str(credit_id or "").strip():
        payload["credit_id"] = str(credit_id).strip()
    session = Session(impersonate=IMPERSONATE)
    try:
        response = session.post(RESET_CREDITS_CONSUME_URL, headers=headers, json=payload, timeout=30)
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(f"reset credit HTTP {response.status_code}")
        try:
            data = response.json()
        except Exception as exc:
            raise RuntimeError(f"reset credit 响应不是 JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError("reset credit 响应格式异常")
        credit = data.get("credit") if isinstance(data.get("credit"), dict) else {}
        return {
            "ok": True,
            "code": str(data.get("code") or "reset"),
            "redeem_request_id": request_id,
            "credit": {
                key: credit.get(key)
                for key in ("id", "status", "title", "description", "expires_at")
                if credit.get(key) is not None
            },
        }
    finally:
        session.close()


def _refresh_tokens(refresh_token: str) -> dict:
    from curl_cffi.requests import Session

    session = Session(impersonate=IMPERSONATE)
    try:
        response = session.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": OAUTH_CLIENT_ID,
            },
            headers={"Accept": "application/json"},
            timeout=30,
        )
        if response.status_code != 200:
            raise RuntimeError(f"token refresh HTTP {response.status_code}")
        data = response.json()
        if not isinstance(data, dict) or not data.get("access_token"):
            raise RuntimeError("token refresh 响应缺少 access_token")
        return data
    finally:
        session.close()


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True)
    try:
        with open(fd, "w", encoding="utf-8", closefd=True) as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
        Path(tmp_name).replace(path)
    finally:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except OSError:
            pass
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _apply_token_refresh(path: Path, record: dict, token: dict) -> dict:
    """把刷新后的 token 回写 auth 文件(cliproxy watcher 会自动重载)。"""
    try:
        expires_in = int(float(token.get("expires_in") or 0))
    except (TypeError, ValueError):
        expires_in = 0
    updates = {
        "access_token": str(token.get("access_token") or ""),
        "refresh_token": str(token.get("refresh_token") or record.get("refresh_token") or ""),
    }
    if token.get("id_token"):
        updates["id_token"] = str(token.get("id_token"))
    if expires_in > 0:
        updates["expired"] = datetime.fromtimestamp(time.time() + expires_in, timezone.utc).isoformat()
    updates["last_refresh"] = _now_iso()
    merged = {**record, **updates}
    _atomic_write_json(path, merged)
    return merged


def _window_label(seconds: Any) -> str:
    try:
        value = int(float(seconds or 0))
    except (TypeError, ValueError):
        value = 0
    if value == 5 * 3600:
        return "5 小时窗口"
    if value == 7 * 86400:
        return "周额度"
    if value and value % 86400 == 0:
        return f"{value // 86400} 天窗口"
    if value and value % 3600 == 0:
        return f"{value // 3600} 小时窗口"
    if value and value % 60 == 0:
        return f"{value // 60} 分钟窗口"
    return "额度窗口"


def _parse_usage(data: dict) -> dict:
    rate_limit = data.get("rate_limit") if isinstance(data.get("rate_limit"), dict) else {}

    def window(value: Any) -> dict | None:
        if not isinstance(value, dict):
            return None
        seconds = value.get("limit_window_seconds")
        return {
            "used_percent": value.get("used_percent"),
            "window_seconds": seconds,
            "window_label": _window_label(seconds),
            "reset_after_seconds": value.get("reset_after_seconds"),
            "reset_at": value.get("reset_at"),
        }

    credits = data.get("credits") if isinstance(data.get("credits"), dict) else {}
    reset_credits = data.get("rate_limit_reset_credits") if isinstance(data.get("rate_limit_reset_credits"), dict) else {}
    if rate_limit.get("limit_reached"):
        status = "limit_reached"
    elif rate_limit.get("allowed") is False:
        status = "blocked"
    else:
        status = "ok"
    primary = window(rate_limit.get("primary_window"))
    secondary = window(rate_limit.get("secondary_window"))
    windows = [item for item in (primary, secondary) if item]
    weekly = next(
        (item for item in windows if int(item.get("window_seconds") or 0) >= 6 * 86400),
        None,
    )
    five_hour = next(
        (
            item
            for item in windows
            if 4 * 3600 <= int(item.get("window_seconds") or 0) <= 6 * 3600
        ),
        None,
    )
    for item in windows:
        seconds = int(item.get("window_seconds") or 0)
        item["kind"] = "weekly" if item is weekly else "five_hour" if item is five_hour else "other"
    return {
        "plan_type": str(data.get("plan_type") or ""),
        "allowed": rate_limit.get("allowed"),
        "limit_reached": rate_limit.get("limit_reached"),
        "primary": primary,
        "secondary": secondary,
        "weekly": weekly,
        "five_hour": five_hour,
        "windows": windows,
        "credits": {
            "has_credits": credits.get("has_credits"),
            "balance": credits.get("balance"),
        },
        "reset_credits": {
            "available": reset_credits.get("available_count"),
            "applicable": reset_credits.get("applicable_available_count"),
        },
        "status": status,
        "fetched_at": _now_iso(),
    }


def _classify_quota_error(error: str) -> str:
    """把额度查询失败归类为账号健康状态。"""
    text = str(error)
    if "usage HTTP 401" in text:
        return "unauthorized"
    if "usage HTTP 403" in text:
        return "forbidden"
    if "usage HTTP 429" in text:
        return "rate_limited"
    if "token refresh HTTP" in text:
        return "refresh_failed"
    return "error"


def _quota_for_file(path: Path) -> dict:
    """查询单个账号额度;AT 失效时刷新 RT(回写文件)后重试一次。"""
    record = _read_auth(path)
    access_token = str(record.get("access_token") or "")
    if not access_token:
        raise RuntimeError("auth 文件缺少 access_token")
    try:
        data = _fetch_usage(access_token, str(record.get("account_id") or ""), str(record.get("email") or path.name))
    except RuntimeError as exc:
        refresh_token = str(record.get("refresh_token") or "")
        retryable = "HTTP 401" in str(exc) or "HTTP 403" in str(exc)
        if not (retryable and refresh_token):
            raise
        token = _refresh_tokens(refresh_token)
        record = _apply_token_refresh(path, record, token)
        data = _fetch_usage(str(record.get("access_token") or ""), str(record.get("account_id") or ""), str(record.get("email") or path.name))
    return _parse_usage(data)


class QuotaRefreshRequest(BaseModel):
    files: list[str] = Field(default_factory=list)


class Sub2ImportRequest(BaseModel):
    filename: str = Field(default="sub2.json", max_length=256)
    content: str = Field(min_length=2, max_length=20_000_000)


class ResetCreditConsumeRequest(BaseModel):
    file: str = Field(min_length=1, max_length=512)
    credit_id: str = Field(default="", max_length=256)


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _sub2_entries(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        accounts = payload.get("data")
    if isinstance(accounts, list):
        return [item for item in accounts if isinstance(item, dict)]
    token_keys = {"access_token", "accessToken", "refresh_token", "refreshToken", "credentials", "tokens"}
    return [payload] if token_keys.intersection(payload) else []


def _normalize_sub2_entry(entry: dict[str, Any]) -> dict[str, str] | None:
    credentials = entry.get("credentials") if isinstance(entry.get("credentials"), dict) else {}
    tokens = entry.get("tokens") if isinstance(entry.get("tokens"), dict) else {}
    bags = (credentials, tokens, entry)
    def pick(*keys: str) -> str:
        return _first_text(*(bag.get(key) for bag in bags for key in keys))

    access_token = pick("access_token", "accessToken")
    refresh_token = pick("refresh_token", "refreshToken")
    id_token = pick("id_token", "idToken")
    account_id = pick("chatgpt_account_id", "account_id", "accountId")
    email = pick("email", "user_email", "userEmail")
    plan_type = pick("plan_type", "planType", "plan")
    expired = pick("expires_at", "expired", "expiresAt")
    claims = _jwt_payload(id_token) or _jwt_payload(access_token)
    auth = claims.get("https://api.openai.com/auth") if isinstance(claims, dict) else {}
    if isinstance(auth, dict):
        account_id = account_id or _first_text(auth.get("chatgpt_account_id"))
        plan_type = plan_type or _first_text(auth.get("chatgpt_plan_type"))
    if not email and isinstance(entry.get("name"), str) and "@" in entry["name"]:
        email = entry["name"].strip()
    if not account_id and not email:
        account_id = _first_text(entry.get("id"))
    if not access_token and not refresh_token:
        return None
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "account_id": account_id,
        "email": email,
        "plan_type": plan_type or "free",
        "expired": expired,
    }


def _sub2_auth_filename(item: dict[str, str], used: set[str]) -> str:
    email = re.sub(r"[^a-zA-Z0-9._-]+", "_", item.get("email") or "").strip("._-")
    account_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", item.get("account_id") or "").strip("_-")
    stem = email or account_id[:24] or "account"
    plan = re.sub(r"[^a-zA-Z0-9_-]+", "-", item.get("plan_type") or "free").strip("-") or "free"
    digest = hashlib.sha256(
        "|".join(item.get(key, "") for key in ("email", "account_id", "access_token", "refresh_token")).encode("utf-8")
    ).hexdigest()[:8]
    base = f"codex-{digest}-{stem}-{plan}.json"
    candidate = base
    index = 2
    while candidate in used or (AUTHS_DIR / candidate).exists():
        candidate = f"codex-{digest}-{stem}-{plan}-{index}.json"
        index += 1
    used.add(candidate)
    return candidate


def _sub2_to_auth(item: dict[str, str]) -> dict[str, Any]:
    credentials = {
        "access_token": item.get("access_token") or "",
        "refresh_token": item.get("refresh_token") or "",
        "id_token": item.get("id_token") or "",
        "chatgpt_account_id": item.get("account_id") or "",
        "email": item.get("email") or "",
        "plan_type": item.get("plan_type") or "free",
    }
    return {
        "access_token": credentials["access_token"],
        "refresh_token": credentials["refresh_token"],
        "id_token": credentials["id_token"],
        "account_id": credentials["chatgpt_account_id"],
        "email": credentials["email"],
        "type": "codex",
        "plan_type": item.get("plan_type") or "free",
        "disabled": False,
        "last_refresh": "",
        "expired": item.get("expired") or "",
    }


def _sub2_export_record(record: dict[str, Any]) -> dict[str, Any]:
    email = str(record.get("email") or "").strip()
    account_id = str(record.get("account_id") or "").strip()
    credentials = {
        "_token_version": int(time.time() * 1000),
        "access_token": str(record.get("access_token") or ""),
        "refresh_token": str(record.get("refresh_token") or ""),
        "id_token": str(record.get("id_token") or ""),
        "chatgpt_account_id": account_id,
        "email": email,
        "expires_at": str(record.get("expired") or ""),
        "plan_type": _plan_from_record(record) or "free",
    }
    return {
        "name": email or account_id or "account",
        "platform": "openai",
        "type": "oauth",
        "credentials": credentials,
        "extra": {"email": email},
        "concurrency": 10,
        "priority": 1,
        "rate_multiplier": 1,
        "auto_pause_on_expired": True,
    }


@router.get("/api/cliproxy/accounts")
async def list_accounts() -> dict:
    with _QUOTA_LOCK:
        cache = {k: dict(v) for k, v in _QUOTA_CACHE.items()}
    items: list[dict] = []
    for path in _auth_files():
        record = _read_auth(path)
        stat = path.stat()
        item = {
            "file": path.name,
            "email": str(record.get("email") or ""),
            "account_id": str(record.get("account_id") or ""),
            "plan_type": _plan_from_record(record) or _plan_from_filename(path.name),
            "expired": str(record.get("expired") or ""),
            "last_refresh": str(record.get("last_refresh") or ""),
            "size_bytes": stat.st_size,
            "mtime": int(stat.st_mtime),
        }
        cached = cache.get(path.name) or {}
        item["quota"] = cached.get("quota")
        item["quota_error"] = str(cached.get("error") or "")
        item["status"] = str(cached.get("status") or "")
        items.append(item)
    return {
        "auths_dir": str(AUTHS_DIR),
        "configured": AUTHS_DIR.is_dir(),
        "items": items,
    }


def _safe_auth_paths(names: list[str] | None = None) -> list[Path]:
    available = {path.name: path for path in _auth_files()}
    if not names:
        return list(available.values())
    selected: list[Path] = []
    for raw_name in names:
        name = str(raw_name or "").strip()
        if not name or "/" in name or "\\" in name or ".." in name:
            continue
        path = available.get(name)
        if path is not None:
            selected.append(path)
    return selected


def _import_sub2_sync(content: str) -> dict[str, Any]:
    try:
        payload = json.loads(str(content or "").lstrip("\ufeff"))
    except Exception as exc:
        raise ValueError(f"账号 JSON 格式无效: {exc}") from exc
    entries = _sub2_entries(payload)
    if not entries:
        raise ValueError("JSON 中没有 accounts 数组或账号对象")

    existing: set[str] = set()
    for path in _auth_files():
        record = _read_auth(path)
        for key in ("email", "account_id", "access_token", "refresh_token"):
            value = str(record.get(key) or "").strip().lower()
            if value:
                existing.add(f"{key}:{value}")

    imported: list[str] = []
    skipped = 0
    errors: list[str] = []
    used: set[str] = set()
    AUTHS_DIR.mkdir(parents=True, exist_ok=True)
    for index, entry in enumerate(entries, start=1):
        item = _normalize_sub2_entry(entry)
        if item is None:
            skipped += 1
            errors.append(f"第 {index} 条缺少 access_token/refresh_token")
            continue
        identities = {
            f"{key}:{item[key].lower()}"
            for key in ("email", "account_id", "access_token", "refresh_token")
            if item.get(key)
        }
        if identities & existing:
            skipped += 1
            continue
        filename = _sub2_auth_filename(item, used)
        _atomic_write_json(AUTHS_DIR / filename, _sub2_to_auth(item))
        imported.append(filename)
        existing.update(identities)
    return {
        "ok": True,
        "imported": len(imported),
        "skipped": skipped,
        "files": imported,
        "errors": errors[:50],
    }


def _import_cpa_sync(content: str) -> dict[str, Any]:
    """Import CPA/codex auth JSON (single object, array, or accounts wrapper)."""
    return _import_sub2_sync(content)


@router.post("/api/cliproxy/import-sub2")
async def import_sub2(body: Sub2ImportRequest) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(_import_sub2_sync, body.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@router.post("/api/cliproxy/import-cpa")
async def import_cpa(body: Sub2ImportRequest) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(_import_cpa_sync, body.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


def _auth_records(paths: list[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        record = _read_auth(path)
        if isinstance(record, dict) and record:
            records.append(record)
    return records


@router.get("/api/cliproxy/export-cpa")
async def export_cpa(files: str = Query(default="")) -> Any:
    names = [name.strip() for name in str(files or "").split(",") if name.strip()]
    records = _auth_records(_safe_auth_paths(names or None))
    if not records:
        raise HTTPException(status_code=404, detail="未找到可导出的 CPA auth 文件")
    data = json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="cliproxy-cpa-{stamp}.json"'},
    )


@router.get("/api/cliproxy/export-sub2")
async def export_sub2(files: str = Query(default="")) -> Any:
    names = [name.strip() for name in str(files or "").split(",") if name.strip()]
    selected = _safe_auth_paths(names or None)
    if not selected:
        raise HTTPException(status_code=404, detail="未找到可导出的 auth 文件")
    accounts = []
    for path in selected:
        record = _read_auth(path)
        if record:
            accounts.append(_sub2_export_record(record))
    if not accounts:
        raise HTTPException(status_code=422, detail="选中的 auth 文件均无法解析")
    payload = {
        "exported_at": _now_iso(),
        "proxies": [],
        "accounts": accounts,
    }
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="cliproxy-sub2-{stamp}.json"'},
    )


@router.post("/api/cliproxy/delete")
async def delete_accounts(body: QuotaRefreshRequest) -> dict[str, Any]:
    """删除中转账号:auth 文件移出 auths 目录(cliproxy 自动卸载),
    移入同级 auths-trash 回收目录,可手动找回。"""
    selected = _safe_auth_paths([name for name in body.files if str(name or "").strip()])
    if not selected:
        raise HTTPException(status_code=404, detail="未找到指定的 auth 文件")
    trash_dir = AUTHS_DIR.parent / "auths-trash"
    trash_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    deleted: list[str] = []
    failed: list[dict[str, str]] = []
    for path in selected:
        try:
            path.replace(trash_dir / f"{stamp}-{path.name}")
            deleted.append(path.name)
        except OSError as exc:
            failed.append({"file": path.name, "error": str(exc)[:120]})
    if deleted:
        with _QUOTA_LOCK:
            for name in deleted:
                _QUOTA_CACHE.pop(name, None)
    return {"ok": not failed, "deleted": deleted, "failed": failed}


@router.post("/api/cliproxy/reset-credits/consume")
async def consume_reset_credit(body: ResetCreditConsumeRequest) -> dict[str, Any]:
    selected = _safe_auth_paths([body.file])
    if len(selected) != 1:
        raise HTTPException(status_code=404, detail="未找到指定的 auth 文件")
    try:
        result = await asyncio.to_thread(_consume_reset_credit, selected[0], body.credit_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:300]) from exc
    try:
        result["quota"] = await asyncio.to_thread(_quota_for_file, selected[0])
    except Exception as exc:
        result["quota_error"] = str(exc)[:200]
    result["file"] = selected[0].name
    return result


def refresh_quota_files(names: list[str] | None = None) -> dict:
    """Refresh the quota cache for all (or selected) auth files.

    Blocking worker shared by the HTTP route and the background auto-refresh
    loop. Safe to call from a threadpool; updates _QUOTA_CACHE in place.
    """
    paths = _auth_files()
    if names:
        wanted = {name for name in names if name}
        paths = [p for p in paths if p.name in wanted]
    if not paths:
        return {"items": {}}

    def work(path: Path) -> tuple[str, dict]:
        try:
            quota = _quota_for_file(path)
            return path.name, {"quota": quota, "error": "", "status": str(quota.get("status") or "ok")}
        except Exception as exc:
            message = str(exc)[:200]
            return path.name, {"quota": None, "error": message, "status": _classify_quota_error(message)}

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(QUOTA_WORKERS, len(paths))) as pool:
        futures = [pool.submit(work, p) for p in paths]
        for future in as_completed(futures):
            name, payload = future.result()
            results[name] = payload

    now = time.time()
    with _QUOTA_LOCK:
        for name, payload in results.items():
            _QUOTA_CACHE[name] = {
                "quota": payload.get("quota"),
                "error": payload.get("error") or "",
                "status": payload.get("status") or "",
                "fetched_ts": now,
            }
    return {"items": results}


@router.post("/api/cliproxy/refresh-quota")
async def refresh_quota(body: QuotaRefreshRequest) -> dict:
    return await run_in_threadpool(refresh_quota_files, body.files or None)


@router.get("/api/cliproxy/download")
async def download_auths(files: str = Query(default=""), mode: str = Query(default="individual")) -> Any:
    names = [name.strip() for name in files.split(",") if name.strip()]
    selected = _safe_auth_paths(names)
    if not selected:
        raise HTTPException(status_code=404, detail="未找到可下载的 auth 文件")
    if mode == "merged":
        records = _auth_records(selected)
        if not records:
            raise HTTPException(status_code=422, detail="选中的 auth 文件均无法解析")
        data = json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        return StreamingResponse(
            io.BytesIO(data),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="cliproxy-cpa-merged.json"'},
        )
    if mode != "individual":
        raise HTTPException(status_code=400, detail="下载模式必须是 merged 或 individual")
    if len(selected) == 1:
        return FileResponse(selected[0], filename=selected[0].name, media_type="application/json")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in selected:
            archive.write(path, arcname=path.name)
    buffer.seek(0)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="cliproxy-auths-{stamp}.zip"'},
    )

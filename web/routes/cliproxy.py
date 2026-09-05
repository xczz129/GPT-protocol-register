# -*- coding: utf-8 -*-
# CLIProxyAPI 中转账号管理:直接读取本机 cliproxy 容器挂载的 auths 目录,
# 额度由面板直连 OpenAI usage 端点查询;全程不经过 cliproxy 的任何接口。

from __future__ import annotations

import base64
import io
import json
import re
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

router = APIRouter()

# cli-proxy-api 容器把 auths 挂载在宿主机 /opt/codex-services/data/cliproxy/auths
AUTHS_DIR = Path("/opt/codex-services/data/cliproxy/auths")
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"
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


def _refresh_tokens(refresh_token: str) -> dict:
    from curl_cffi.requests import Session

    session = Session(impersonate=IMPERSONATE)
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


def _atomic_write_json(path: Path, payload: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
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


def _parse_usage(data: dict) -> dict:
    rate_limit = data.get("rate_limit") if isinstance(data.get("rate_limit"), dict) else {}

    def window(value: Any) -> dict | None:
        if not isinstance(value, dict):
            return None
        return {
            "used_percent": value.get("used_percent"),
            "window_seconds": value.get("limit_window_seconds"),
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
    return {
        "plan_type": str(data.get("plan_type") or ""),
        "allowed": rate_limit.get("allowed"),
        "limit_reached": rate_limit.get("limit_reached"),
        "primary": window(rate_limit.get("primary_window")),
        "secondary": window(rate_limit.get("secondary_window")),
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


@router.post("/api/cliproxy/refresh-quota")
async def refresh_quota(body: QuotaRefreshRequest) -> dict:
    paths = _auth_files()
    if body.files:
        wanted = {name for name in body.files if name}
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


@router.get("/api/cliproxy/download")
async def download_auths(files: str = Query(default="")) -> Any:
    names = [name.strip() for name in files.split(",") if name.strip()]
    selected: list[Path] = []
    for name in names:
        if "/" in name or "\\" in name or ".." in name:
            continue
        path = AUTHS_DIR / name
        try:
            path.resolve().relative_to(AUTHS_DIR.resolve())
        except ValueError:
            continue
        if path.is_file():
            selected.append(path)
    if not selected:
        raise HTTPException(status_code=404, detail="未找到可下载的 auth 文件")
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

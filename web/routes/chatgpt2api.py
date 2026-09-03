"""chatgpt2api panel config, connectivity test and bulk upload routes."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()


def _state() -> Any:
    # Delayed import is intentional: web_app includes this router.
    import web_app

    return web_app


class ChatGPT2APIUpdate(BaseModel):
    enabled: bool = False
    url: str = ""
    key: str = ""
    auto_upload: bool = True


class ChatGPT2APITestRequest(BaseModel):
    url: str | None = None
    key: str | None = None


class ChatGPT2APIUploadRequest(ChatGPT2APITestRequest):
    group_id: str | None = None


def _c2a_public(cfg: dict[str, Any]) -> dict[str, Any]:
    from common.chatgpt2api import c2a_config_from

    c = c2a_config_from(cfg)
    key = str(c.get("key") or "")
    return {
        "enabled": bool(c.get("enabled")),
        "url": str(c.get("url") or ""),
        "key": "",
        "key_masked": _state()._mask_secret(key) if key else "",
        "has_key": bool(key),
        "auto_upload": bool(c.get("auto_upload", True)),
    }


@router.get("/api/chatgpt2api")
async def get_chatgpt2api():
    return {"chatgpt2api": _c2a_public(_state()._read_config())}


@router.put("/api/chatgpt2api")
async def put_chatgpt2api(body: ChatGPT2APIUpdate):
    if _state().JOB.running:
        raise HTTPException(409, "job is running; stop it before saving config")
    from common.chatgpt2api import normalize_c2a_url

    with _state()._edit_config() as cfg:
        c2a = cfg.setdefault("chatgpt2api", {})
        c2a["enabled"] = bool(body.enabled)
        c2a["url"] = normalize_c2a_url(body.url) or "https://chatgpt2api.xbovo.online"
        new_key = str(body.key or "").strip()
        if new_key and "…" not in new_key and "***" not in new_key:
            c2a["key"] = new_key
        c2a["auto_upload"] = bool(body.auto_upload)
    return {"ok": True, "chatgpt2api": _c2a_public(cfg)}


@router.post("/api/chatgpt2api/test")
async def test_chatgpt2api(body: ChatGPT2APITestRequest):
    from common.chatgpt2api import c2a_config_from, test_connection

    cfg = _state()._read_config()
    c = c2a_config_from(cfg)
    url = (body.url or "").strip() or c.get("url") or ""
    key = (body.key or "").strip() or c.get("key") or ""
    ok, msg = await asyncio.to_thread(test_connection, url, key)
    return {"ok": ok, "message": msg, "url": url}


@router.post("/api/chatgpt2api/upload-all")
async def upload_all_chatgpt2api(body: ChatGPT2APIUploadRequest | None = None):
    from common.chatgpt2api import (
        c2a_config_from,
        collect_local_accounts,
        import_accounts,
        normalize_c2a_url,
    )

    cfg = _state()._read_config()
    c = c2a_config_from(cfg)
    body = body or ChatGPT2APIUploadRequest()
    url = normalize_c2a_url((body.url or "").strip() or c.get("url") or "")
    key = (body.key or "").strip() or c.get("key") or ""
    if not url or not key:
        raise HTTPException(400, "请先配置 chatgpt2api 网址和 admin key")
    group_id = str(body.group_id or "").strip()
    groups = _state()._groups_with_counts()
    default_group_id = str(groups[0]["id"])
    group = next((item for item in groups if item["id"] == group_id), None) if group_id else None
    if group_id and group is None:
        raise HTTPException(404, "分组不存在")
    group_count = (group or {}).get("account_count")
    if group is not None and group_count is not None:
        try:
            expected = max(0, int(group_count))
        except (TypeError, ValueError):
            expected = await asyncio.to_thread(_state().count_accounts, _state().ACCOUNTS_FILE)
    else:
        expected = await asyncio.to_thread(_state().count_accounts, _state().ACCOUNTS_FILE)
    _state().PROGRESS.start(
        "c2a_upload",
        "chatgpt2api 账号上传",
        total=expected,
        stage="筛选分组与有效账号",
    )
    accounts = await asyncio.to_thread(
        collect_local_accounts,
        group_id=group_id,
        default_group_id=default_group_id,
        accounts_path=_state().ACCOUNTS_FILE,
    )
    if not accounts:
        scope = f"分组“{group['name']}”" if group else "本地"
        _state().PROGRESS.finish("c2a_upload", ok=False, message=f"{scope}没有有效账号")
        return {
            "ok": False,
            "message": f"{scope}没有可上传的有效账号",
            "count": 0,
            "group_id": group_id,
            "group_name": str((group or {}).get("name") or ""),
        }
    _state().PROGRESS.update(
        "c2a_upload",
        done=0,
        total=len(accounts),
        percent=30,
        stage=f"正在提交 {len(accounts)} 个账号到远端",
    )
    try:
        upload_result = await asyncio.to_thread(import_accounts, url, key, accounts)
    except Exception as exc:
        _state().PROGRESS.finish("c2a_upload", ok=False, message=str(exc))
        raise
    ok, msg, data = upload_result
    _state().PROGRESS.update(
        "c2a_upload",
        done=len(accounts),
        total=len(accounts),
        percent=95,
        stage="记录上传结果",
    )
    uploaded_emails = {
        str(item.get("email") or "").strip().lower()
        for item in accounts
        if isinstance(item, dict) and str(item.get("email") or "").strip()
    }
    upload_keys = [
        _state()._account_key(item)
        for item in accounts
        if str(item.get("email") or "").strip().lower() in uploaded_emails
        and _state()._account_key(item)
    ]
    if upload_keys:
        await asyncio.to_thread(
            _state().update_account_records,
            upload_keys,
            {
                "c2a_upload_status": "success" if ok else "failed",
                "c2a_uploaded_at": _state()._now_iso(),
                "c2a_upload_error": "" if ok else msg[:300],
            },
            path=_state().ACCOUNTS_FILE,
        )
    _state().PROGRESS.finish("c2a_upload", ok=ok, message=msg)
    return {
        "ok": ok,
        "message": msg,
        "count": len(accounts),
        "result": data,
        "url": url,
        "group_id": group_id,
        "group_name": str((group or {}).get("name") or ""),
    }

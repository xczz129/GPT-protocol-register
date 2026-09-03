# -*- coding: utf-8 -*-
"""Account-only backup routes: standalone JSON snapshots of account records."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from common.panel_ops import (
    account_backup_path,
    create_account_backup,
    delete_account_backup,
    list_account_backups,
    restore_account_backup,
)

router = APIRouter()


class AccountBackupCreateRequest(BaseModel):
    group_id: str = ""
    status: str = ""


class AccountBackupRestoreRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    mode: str = "merge"


def _state() -> Any:
    # Delayed import is intentional: web_app includes this router.
    import web_app

    return web_app


@router.get("/api/account-backups")
async def account_backups_list():
    return {"ok": True, "items": await asyncio.to_thread(list_account_backups)}


@router.post("/api/account-backups")
async def account_backups_create(body: AccountBackupCreateRequest):
    try:
        item = await asyncio.to_thread(create_account_backup, body.group_id, body.status)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return {"ok": True, "item": item}


@router.get("/api/account-backups/{name}")
async def account_backups_download(name: str):
    try:
        path = account_backup_path(name)
    except (ValueError, FileNotFoundError) as error:
        raise HTTPException(404, "备份不存在") from error
    return FileResponse(path, filename=path.name, media_type="application/json")


@router.post("/api/account-backups/restore")
async def account_backups_restore(body: AccountBackupRestoreRequest):
    if _state().JOB.running:
        raise HTTPException(409, "任务运行时不能恢复账号备份")
    mode = "replace" if str(body.mode or "").strip().lower() == "replace" else "merge"
    try:
        result = await asyncio.to_thread(restore_account_backup, body.name, mode)
    except FileNotFoundError as error:
        raise HTTPException(404, "备份不存在") from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return {"ok": True, **result}


@router.delete("/api/account-backups/{name}")
async def account_backups_delete(name: str):
    try:
        removed = await asyncio.to_thread(delete_account_backup, name)
    except (ValueError, FileNotFoundError) as error:
        raise HTTPException(404, "备份不存在") from error
    return {"ok": True, "name": removed}

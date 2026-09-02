"""Account group management routes (CRUD, assignment, credential export).

Handlers resolve runtime state from :mod:`web_app` lazily per request, which
keeps this module free of import cycles with the application bootstrap.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field


router = APIRouter()


class GroupCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class GroupUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class AccountGroupAssignRequest(BaseModel):
    group_id: str = Field(min_length=1, max_length=64)
    keys: list[str] = Field(default_factory=list)
    delete_all: bool = False


def _state() -> Any:
    # Delayed import is intentional: web_app includes this router.
    import web_app

    return web_app


@router.get("/api/account-groups")
async def account_groups():
    state = _state()

    def _response():
        cfg = state._read_config()
        key = (
            f"account-groups:v2:{state.account_store_revision(state.ACCOUNTS_FILE)}:"
            f"{state._file_stamp(state.GROUPS_FILE)}:{state._account_data_stamp()}"
        )
        return state._cached_json_response(
            cfg,
            key,
            "accounts_ttl",
            300,
            lambda: {"ok": True, "items": state._groups_with_counts()},
        )

    return await asyncio.to_thread(_response)


@router.post("/api/account-groups")
async def account_groups_create(body: GroupCreateRequest):
    state = _state()
    try:
        group = await asyncio.to_thread(state.create_group, body.name, state.GROUPS_FILE)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return {"ok": True, "group": group}


@router.put("/api/account-groups/{group_id}")
async def account_groups_rename(group_id: str, body: GroupUpdateRequest):
    state = _state()
    try:
        group = await asyncio.to_thread(state.rename_group, group_id, body.name, state.GROUPS_FILE)
    except KeyError as error:
        raise HTTPException(404, str(error).strip("'")) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return {"ok": True, "group": group}


@router.delete("/api/account-groups/{group_id}")
async def account_groups_delete(group_id: str):
    state = _state()
    try:
        removed, remaining = await asyncio.to_thread(state.delete_group, group_id, state.GROUPS_FILE)
    except KeyError as error:
        raise HTTPException(404, str(error).strip("'")) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    fallback_id = remaining[0]["id"]
    moved = await asyncio.to_thread(
        state.reassign_account_group,
        group_id,
        fallback_id,
        path=state.ACCOUNTS_FILE,
    )
    return {
        "ok": True,
        "removed": removed,
        "fallback_group_id": fallback_id,
        "moved": moved,
    }


@router.post("/api/accounts/group")
async def accounts_assign_group(body: AccountGroupAssignRequest):
    state = _state()
    if state.get_group(body.group_id, state.GROUPS_FILE) is None:
        raise HTTPException(404, "分组不存在")
    if not body.delete_all and not body.keys:
        raise HTTPException(400, "请选择要移动的账号")
    changed, _ = await asyncio.to_thread(
        state.assign_account_group,
        body.keys,
        body.group_id,
        delete_all=body.delete_all,
        include_records=False,
        path=state.ACCOUNTS_FILE,
    )
    return {
        "ok": True,
        "changed": changed,
        "count": await asyncio.to_thread(state.count_accounts, state.ACCOUNTS_FILE),
    }


@router.get("/api/account-groups/{group_id}/export-credentials")
async def account_group_export_credentials(group_id: str):
    state = _state()
    group = state.get_group(group_id, state.GROUPS_FILE)
    if group is None:
        raise HTTPException(404, "分组不存在")
    lines, missing = await asyncio.to_thread(state._credential_lines_for_group, group_id)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", group["name"]).strip("-") or "group"
    payload = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    headers = {
        "Content-Disposition": f'attachment; filename="{safe_name}.mail-credentials.txt"',
        "X-Exported-Count": str(len(lines)),
        "X-Missing-Count": str(missing),
    }
    return StreamingResponse(iter([payload]), media_type="text/plain; charset=utf-8", headers=headers)

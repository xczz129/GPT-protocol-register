"""Session-based access-token refresh routes (刷新AT)."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from common.session_refresh import (
    SESSION_REFRESH_JOBS,
    access_token_needs_refresh,
)

router = APIRouter()


def _state() -> Any:
    # Delayed import is intentional: web_app includes this router.
    import web_app

    return web_app


class AccountRefreshAtRequest(BaseModel):
    keys: list[str] = Field(default_factory=list, max_length=2000)
    all_accounts: bool = False
    concurrency: int = Field(default=10, ge=1, le=10)


@router.post("/api/accounts/refresh-at")
async def accounts_refresh_at(body: AccountRefreshAtRequest):
    """Refresh stored ATs through the nerver session service.

    Explicit keys are always attempted (single-account or hand-picked rows).
    The all-accounts mode only covers records whose AT already expired or
    expires soon.  Deactivated records and records without a session token
    are skipped in both modes.
    """
    wanted = {str(key or "").strip() for key in body.keys if str(key or "").strip()}
    if not body.all_accounts and not wanted:
        raise HTTPException(400, "请选择要刷新 AT 的账号，或明确使用全部临期刷新")
    records = await asyncio.to_thread(_state()._load_accounts)
    selected: list[dict[str, Any]] = []
    skipped_deactivated = 0
    skipped_no_session = 0
    skipped_fresh = 0
    for record in records:
        key = _state()._account_key(record)
        if not (body.all_accounts or key in wanted):
            continue
        if _state().account_is_deactivated(record):
            skipped_deactivated += 1
            continue
        if not str(record.get("session_token") or "").strip():
            skipped_no_session += 1
            continue
        if body.all_accounts and not access_token_needs_refresh(record):
            skipped_fresh += 1
            continue
        selected.append(record)
    if not selected:
        raise HTTPException(
            400,
            "没有可刷新 AT 的账号（需要有 Session Token 且未封号）",
        )
    try:
        job = await asyncio.to_thread(
            SESSION_REFRESH_JOBS.start,
            selected,
            concurrency=body.concurrency,
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    return {
        "ok": True,
        **job,
        "skipped_known_deactivated": skipped_deactivated,
        "skipped_no_session": skipped_no_session,
        "skipped_fresh_at": skipped_fresh,
    }


@router.get("/api/accounts/refresh-at/progress")
async def accounts_refresh_at_progress():
    return {"ok": True, "job": await asyncio.to_thread(SESSION_REFRESH_JOBS.active)}


@router.get("/api/accounts/refresh-at/{job_id}")
async def accounts_refresh_at_job(job_id: str):
    job = await asyncio.to_thread(SESSION_REFRESH_JOBS.snapshot, job_id)
    if job is None:
        raise HTTPException(404, "刷新 AT 任务不存在或已清理")
    return {"ok": True, **job}


@router.post("/api/accounts/refresh-at/{job_id}/stop")
async def accounts_refresh_at_stop(job_id: str):
    job = await asyncio.to_thread(SESSION_REFRESH_JOBS.stop, job_id)
    if job is None:
        raise HTTPException(404, "刷新 AT 任务不存在或已清理")
    return {"ok": True, **job}

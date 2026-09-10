"""Account trash routes (recycle bin list / restore).

Handlers resolve runtime state from :mod:`web_app` lazily per request, which
keeps this module free of import cycles with the application bootstrap.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from common.panel_ops import restore_trash


router = APIRouter()


class TrashRestoreRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)


def _state() -> Any:
    # Delayed import is intentional: web_app includes this router.
    import web_app

    return web_app


@router.get("/api/accounts/trash")
async def accounts_trash():
    state = _state()

    def _load_public_trash() -> list[dict[str, Any]]:
        rows = state.list_trash(limit=500)
        items: list[dict[str, Any]] = []
        for row in rows:
            record = row.get("record") if isinstance(row.get("record"), dict) else {}
            items.append(
                {
                    "trash_id": row.get("trash_id"),
                    "deleted_at": row.get("deleted_at"),
                    "reason": row.get("reason"),
                    "account": state.account_public_view(record),
                }
            )
        return items

    items = await asyncio.to_thread(_load_public_trash)
    return {"ok": True, "count": len(items), "items": items}


@router.post("/api/accounts/trash/restore")
async def accounts_trash_restore(body: TrashRestoreRequest):
    state = _state()
    if not any(str(value or "").strip() for value in body.ids):
        return {"ok": True, "restored": 0}
    try:
        restored = await asyncio.to_thread(restore_trash, body.ids, state.ACCOUNTS_FILE)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return {"ok": True, "restored": restored}

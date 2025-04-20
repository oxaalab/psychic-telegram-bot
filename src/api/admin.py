from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from welcome.repository import (
    bulk_import_history,
    fetch_history_by_user_id,
    fetch_history_by_username,
    upsert_user,
)

router = APIRouter()


class SnapshotItem(BaseModel):
    first_name: str | None = ""
    last_name: str | None = ""
    username: str | None = ""
    seen_at: str | None = ""

    @field_validator("first_name", "last_name", "username", "seen_at", mode="before")
    @classmethod
    def _nz(cls, v):
        return (v or "").strip()


class ImportPayload(BaseModel):
    user_id: int
    is_bot: bool | None = False
    language_code: str | None = None
    items: list[SnapshotItem]


@router.get("/user/{user_id}/history")
async def get_history_by_user_id(
    user_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    history = await fetch_history_by_user_id(session, user_id)
    if history is None:
        raise HTTPException(status_code=404, detail="User not found")
    return {"user_id": user_id, "history": history}


@router.get("/username/{username}/history")
async def get_history_by_username(
    username: str,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    res = await fetch_history_by_username(session, username.lstrip("@"))
    if res is None:
        raise HTTPException(status_code=404, detail="No user found for username")
    user_id, history = res
    return {"user_id": user_id, "history": history}


@router.post("/import-history")
async def import_history(
    payload: ImportPayload,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    await upsert_user(
        session,
        payload.user_id,
        bool(payload.is_bot),
        payload.language_code,
    )
    items = [i.model_dump() for i in payload.items]
    count = await bulk_import_history(
        session,
        payload.user_id,
        items,
    )
    await session.commit()
    return {"ok": True, "user_id": payload.user_id, "imported": count}

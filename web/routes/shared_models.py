"""Pydantic request models shared between web_app and route modules."""

from pydantic import BaseModel, Field


class AccountTokenRequest(BaseModel):
    key: str = Field(min_length=1, max_length=256)


class BulkDeleteRequest(BaseModel):
    keys: list[str] = Field(default_factory=list)
    delete_all: bool = False

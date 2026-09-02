"""Pydantic request models shared between web_app and route modules."""

from pydantic import BaseModel, Field


class AccountTokenRequest(BaseModel):
    key: str = Field(min_length=1, max_length=256)
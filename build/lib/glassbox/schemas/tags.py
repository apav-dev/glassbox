from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class TagCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    description: str | None = None


class TagResponse(BaseModel):
    model_config = {"from_attributes": True}

    name: str
    color: str | None
    description: str | None


class TickerTagResponse(BaseModel):
    model_config = {"from_attributes": True}

    symbol: str
    tag_name: str
    tagged_by: str
    tagged_at: datetime


class TagTickerRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=16, pattern=r"^[A-Z][A-Z0-9.\-]{0,15}$")
    tag_name: str = Field(..., min_length=1, max_length=64)
    tagged_by: str = Field(default="agent", min_length=1, max_length=64)


class BulkTagRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=16, pattern=r"^[A-Z][A-Z0-9.\-]{0,15}$")
    tag_names: list[str] = Field(..., min_length=1)
    tagged_by: str = Field(default="agent", min_length=1, max_length=64)


class ThematicExposureBucket(BaseModel):
    tag: str
    total_market_value: float
    weight: float
    position_count: int
    symbols: list[str]

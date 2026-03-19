from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ThesisCreate(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=16, pattern=r"^[A-Z][A-Z0-9.\-]{0,15}$")
    thesis: str = Field(..., min_length=1)
    catalyst: str | None = None
    edge: str | None = None
    status: str = Field(default="active", pattern=r"^(active|closed|watching)$")
    created_by: str = Field(default="agent", min_length=1, max_length=64)


class ThesisUpdate(BaseModel):
    thesis: str | None = Field(default=None, min_length=1)
    catalyst: str | None = None
    edge: str | None = None
    status: str | None = Field(default=None, pattern=r"^(active|closed|watching)$")


class ThesisResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    symbol: str
    version: int
    thesis: str
    catalyst: str | None
    edge: str | None
    status: str
    created_at: datetime
    created_by: str


class ThesisWithFundamentals(ThesisResponse):
    """Thesis enriched with current position weight and key fundamentals."""

    portfolio_weight: float | None = None
    trailing_pe: float | None = None
    target_mean_price: float | None = None
    recommendation_key: str | None = None

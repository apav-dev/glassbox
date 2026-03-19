from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class TickerFundamentalsResponse(BaseModel):
    symbol: str
    updated_at: datetime | None
    market_cap: float | None = None
    trailing_pe: float | None = None
    forward_pe: float | None = None
    price_to_book: float | None = None
    ev_to_ebitda: float | None = None
    ev_to_revenue: float | None = None
    peg_ratio: float | None = None
    price_to_sales: float | None = None
    profit_margin: float | None = None
    gross_margin: float | None = None
    operating_margin: float | None = None
    ebitda_margin: float | None = None
    return_on_equity: float | None = None
    return_on_assets: float | None = None
    target_high_price: float | None = None
    target_low_price: float | None = None
    target_mean_price: float | None = None
    target_median_price: float | None = None
    analyst_count: int | None = None
    recommendation_key: str | None = None
    recommendation_mean: float | None = None


class FundamentalsRefreshResult(BaseModel):
    refreshed: dict[str, bool]
    total: int
    succeeded: int
    failed: int

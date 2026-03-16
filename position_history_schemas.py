from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class DailyPositionSnapshot(BaseModel):
    date: date
    positions: dict[str, float]
    cash: float
    total_tickers: int


class TickerDailyUnits(BaseModel):
    date: date
    shares: float


class PositionAtDateResponse(BaseModel):
    symbol: str
    date: date
    shares: float

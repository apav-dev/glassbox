from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class LotDetail(BaseModel):
    qty: float
    price: float


class CostBasisResponse(BaseModel):
    symbol: str
    total_qty: float
    avg_cost: float
    total_cost_basis: float
    realized_pnl: float
    lots: list[LotDetail]


class RealizedPnlRecord(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    ticker: str
    sell_trade_id: str
    qty: float
    buy_price: float
    sell_price: float
    pnl: float
    closed_at: datetime


class TickerRealizedPnl(BaseModel):
    symbol: str
    realized_pnl: float
    trade_count: int


class RealizedPnlSummary(BaseModel):
    total_realized_pnl: float
    by_ticker: list[TickerRealizedPnl]

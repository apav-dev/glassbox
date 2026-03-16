from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


SymbolPattern = r"^[A-Z][A-Z0-9.\-]{0,15}$"


class TradeRequest(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=16, pattern=SymbolPattern)
    side: Literal["buy", "sell"]
    qty: float = Field(..., gt=0)
    thesis: str = Field(..., min_length=1)
    strategy_tag: str | None = Field(default=None, min_length=1, max_length=128)
    order_type: str = Field(default="market", min_length=1, max_length=32, pattern=r"^[a-z_]+$")
    time_in_force: str = Field(default="day", min_length=1, max_length=32, pattern=r"^[a-z_]+$")


class TradeResponse(BaseModel):
    order_id: str
    client_order_id: str
    status: str
    symbol: str
    side: str
    qty: float
    filled_qty: float
    filled_avg_price: float
    submitted_at: datetime | None
    local_trade_id: str
    strategy_tag: str | None
    thesis_recorded: bool


class PortfolioSummaryResponse(BaseModel):
    nav: float
    equity: float
    cash: float
    buying_power: float
    day_pnl: float
    day_pnl_pct: float
    long_market_value: float


class PositionResponse(BaseModel):
    symbol: str
    side: str
    qty: float
    avg_entry_price: float
    market_value: float
    cost_basis: float
    unrealized_pl: float
    unrealized_plpc: float
    company_name: str | None = None
    sector: str | None = None
    industry: str | None = None
    trailing_pe: float | None = None
    forward_pe: float | None = None
    price_to_book: float | None = None
    target_mean_price: float | None = None
    recommendation_key: str | None = None


class TradeRecord(BaseModel):
    model_config = {"from_attributes": True}

    trade_id: str
    timestamp: datetime
    ticker: str
    action: str
    qty: Decimal
    price: Decimal | None
    commission: Decimal
    strategy_tag: str | None
    investment_thesis: str


class SyncResponse(BaseModel):
    snapshot_date: date
    equity_value: float
    cash_balance: float
    long_market_value: float
    total_drawdown: float
    positions_count: int
    new_tickers_added: list[str]


class HealthResponse(BaseModel):
    status: str
    db_connected: bool
    alpaca_connected: bool

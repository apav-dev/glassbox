from __future__ import annotations

from datetime import date

from pydantic import BaseModel

from glassbox.schemas.trading import PositionResponse


class EquityCurvePoint(BaseModel):
    date: date
    cash: float
    invested_value: float
    nav: float
    daily_return: float
    cumulative_return: float
    drawdown: float


class ReturnPoint(BaseModel):
    date: date
    value: float


class RiskMetricsResponse(BaseModel):
    total_return: float | None
    annualized_return: float | None
    annualized_volatility: float | None
    sharpe_ratio: float | None
    sortino_ratio: float | None
    var_95: float | None
    var_99: float | None
    cvar_95: float | None
    max_drawdown: float | None
    max_drawdown_date: date | None
    best_day: ReturnPoint | None
    worst_day: ReturnPoint | None
    win_rate: float | None
    current_drawdown: float | None
    beta_to_spy: float | None = None
    snapshot_count: int


class PositionWithWeight(PositionResponse):
    portfolio_weight: float


class ExposureBucket(BaseModel):
    name: str
    total_market_value: float
    weight: float
    position_count: int


class PortfolioRiskReport(BaseModel):
    risk_metrics: RiskMetricsResponse
    sector_exposure: list[ExposureBucket]
    geographic_exposure: list[ExposureBucket]


class CorrelationMatrixResponse(BaseModel):
    symbols: list[str]
    matrix: list[list[float | None]]
    lookback_days: int
    data_points_used: int


class StressScenario(BaseModel):
    scenario: str
    market_move: float
    beta_used: float
    beta_estimated: bool
    portfolio_move: float
    dollar_impact: float
    portfolio_value_after: float


class StressTestResponse(BaseModel):
    current_portfolio_value: float
    scenarios: list[StressScenario]

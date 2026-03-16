from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Annotated
from uuid import uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from alpaca_client import AlpacaClient
from models import DailyPrice, DailySnapshot, SessionLocal, Ticker, Trade, TradeAction, get_db, init_db
from price_service import backfill_all_tickers, backfill_ticker_prices
from risk_schemas import (
    CorrelationMatrixResponse,
    EquityCurvePoint,
    ExposureBucket,
    PortfolioRiskReport,
    PositionWithWeight,
    RiskMetricsResponse,
    StressTestResponse,
)
from risk_service import (
    compute_correlation_matrix,
    compute_daily_returns,
    compute_geographic_exposure,
    compute_position_weights,
    compute_stress_scenarios,
    compute_risk_metrics,
    compute_sector_exposure,
)
from schemas import (
    HealthResponse,
    PortfolioSummaryResponse,
    PositionResponse,
    SyncResponse,
    TradeRecord,
    TradeRequest,
    TradeResponse,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Glass Box Trading API",
    description=(
        "Backend for an AI day-trading agent. Combines Alpaca execution with "
        "local analytics persistence."
    ),
    lifespan=lifespan,
)

DbSession = Annotated[Session, Depends(get_db)]
logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_alpaca_client() -> AlpacaClient:
    return AlpacaClient()


def _alpaca_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=502, detail=f"Alpaca error: {exc}")


def _database_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=500, detail=f"Database error: {exc}")


def _load_ticker_map(db: Session, symbols: list[str]) -> dict[str, Ticker]:
    if not symbols:
        return {}

    ticker_rows = db.scalars(select(Ticker).where(Ticker.symbol.in_(symbols))).all()
    return {ticker.symbol.upper(): ticker for ticker in ticker_rows}


def _enrich_positions(
    positions: list[dict[str, float | str]],
    ticker_map: dict[str, Ticker],
) -> list[dict[str, float | str | None]]:
    enriched_positions: list[dict[str, float | str | None]] = []
    for position in positions:
        ticker_record = ticker_map.get(str(position["symbol"]).upper())
        enriched_positions.append(
            {
                **position,
                "company_name": ticker_record.company_name if ticker_record else None,
                "sector": ticker_record.sector if ticker_record else None,
                "industry": ticker_record.industry if ticker_record else None,
            }
        )

    return enriched_positions


def _load_price_history(
    db: Session,
    symbols: list[str],
    start_date: date | None = None,
) -> list[DailyPrice]:
    if not symbols:
        return []

    statement = select(DailyPrice).where(DailyPrice.symbol.in_([symbol.upper() for symbol in symbols]))
    if start_date is not None:
        statement = statement.where(DailyPrice.date >= start_date)

    return db.scalars(statement.order_by(DailyPrice.symbol.asc(), DailyPrice.date.asc())).all()


def _backfill_symbol_prices_in_background(symbol: str) -> None:
    db = SessionLocal()
    try:
        price_exists = db.scalar(
            select(DailyPrice.symbol).where(DailyPrice.symbol == symbol.upper()).limit(1)
        )
        if price_exists is None:
            backfill_ticker_prices(db, symbol)
    except Exception:
        logger.exception("Failed to backfill daily prices for %s after trade execution", symbol.upper())
        db.rollback()
    finally:
        db.close()


@app.get("/health", response_model=HealthResponse)
def health_check(db: DbSession) -> HealthResponse:
    """Report local database and Alpaca connectivity."""
    db_connected = False
    alpaca_connected = False

    try:
        db.execute(text("SELECT 1"))
        db_connected = True
    except Exception:
        db_connected = False

    try:
        get_alpaca_client().get_account()
        alpaca_connected = True
    except Exception:
        alpaca_connected = False

    status = "ok" if db_connected and alpaca_connected else "degraded"
    return HealthResponse(
        status=status,
        db_connected=db_connected,
        alpaca_connected=alpaca_connected,
    )


@app.get("/portfolio/summary", response_model=PortfolioSummaryResponse)
def get_portfolio_summary() -> PortfolioSummaryResponse:
    """Return the current account summary from Alpaca."""
    try:
        summary = get_alpaca_client().get_account_summary()
    except Exception as exc:
        raise _alpaca_error(exc) from exc

    return PortfolioSummaryResponse(**summary)


@app.get("/portfolio/positions", response_model=list[PositionResponse])
def list_portfolio_positions(db: DbSession) -> list[PositionResponse]:
    """Return Alpaca positions enriched with local ticker metadata."""
    try:
        positions = get_alpaca_client().list_positions()
    except Exception as exc:
        raise _alpaca_error(exc) from exc

    symbols = sorted({str(position["symbol"]).upper() for position in positions})
    if symbols:
        try:
            ticker_map = _load_ticker_map(db, symbols)
        except SQLAlchemyError as exc:
            raise _database_error(exc) from exc
    else:
        ticker_map = {}

    return [PositionResponse(**position) for position in _enrich_positions(positions, ticker_map)]


@app.get("/portfolio/equity-curve", response_model=list[EquityCurvePoint])
def get_portfolio_equity_curve(db: DbSession) -> list[EquityCurvePoint]:
    """Return the local equity curve derived from stored daily snapshots."""
    try:
        snapshots = db.scalars(select(DailySnapshot).order_by(DailySnapshot.date.asc())).all()
    except SQLAlchemyError as exc:
        raise _database_error(exc) from exc

    return [EquityCurvePoint(**point) for point in compute_daily_returns(snapshots)]


@app.get("/portfolio/risk", response_model=RiskMetricsResponse)
def get_portfolio_risk(
    db: DbSession,
    risk_free_rate: float = Query(default=0.045),
) -> RiskMetricsResponse:
    """Return portfolio-level return and drawdown metrics derived from stored daily snapshots."""
    try:
        snapshots = db.scalars(select(DailySnapshot).order_by(DailySnapshot.date.asc())).all()
        spy_prices = _load_price_history(db, ["SPY"])
    except SQLAlchemyError as exc:
        raise _database_error(exc) from exc

    return RiskMetricsResponse(
        **compute_risk_metrics(
            snapshots,
            risk_free_rate=risk_free_rate,
            benchmark_prices=spy_prices,
        )
    )


@app.get("/portfolio/positions/weighted", response_model=list[PositionWithWeight])
def list_weighted_portfolio_positions(db: DbSession) -> list[PositionWithWeight]:
    """Return live positions with current portfolio weights and local ticker metadata."""
    try:
        positions = get_alpaca_client().list_positions()
        summary = get_alpaca_client().get_account_summary()
    except Exception as exc:
        raise _alpaca_error(exc) from exc

    symbols = sorted({str(position["symbol"]).upper() for position in positions})
    try:
        ticker_map = _load_ticker_map(db, symbols)
    except SQLAlchemyError as exc:
        raise _database_error(exc) from exc

    weighted_positions = compute_position_weights(positions, nav=float(summary["nav"]))
    enriched_positions = _enrich_positions(weighted_positions, ticker_map)
    return [PositionWithWeight(**position) for position in enriched_positions]


@app.get("/portfolio/exposure/sector", response_model=list[ExposureBucket])
def get_sector_exposure(db: DbSession) -> list[ExposureBucket]:
    """Return live portfolio exposure grouped by sector using local ticker metadata."""
    try:
        positions = get_alpaca_client().list_positions()
        summary = get_alpaca_client().get_account_summary()
    except Exception as exc:
        raise _alpaca_error(exc) from exc

    symbols = sorted({str(position["symbol"]).upper() for position in positions})
    try:
        ticker_map = _load_ticker_map(db, symbols)
    except SQLAlchemyError as exc:
        raise _database_error(exc) from exc

    buckets = compute_sector_exposure(positions, ticker_map, nav=float(summary["nav"]))
    return [ExposureBucket(**bucket) for bucket in buckets]


@app.get("/portfolio/exposure/geographic", response_model=list[ExposureBucket])
def get_geographic_exposure(db: DbSession) -> list[ExposureBucket]:
    """Return live portfolio exposure grouped by country using local ticker metadata."""
    try:
        positions = get_alpaca_client().list_positions()
        summary = get_alpaca_client().get_account_summary()
    except Exception as exc:
        raise _alpaca_error(exc) from exc

    symbols = sorted({str(position["symbol"]).upper() for position in positions})
    try:
        ticker_map = _load_ticker_map(db, symbols)
    except SQLAlchemyError as exc:
        raise _database_error(exc) from exc

    buckets = compute_geographic_exposure(positions, ticker_map, nav=float(summary["nav"]))
    return [ExposureBucket(**bucket) for bucket in buckets]


@app.get("/portfolio/risk/report", response_model=PortfolioRiskReport)
def get_portfolio_risk_report(
    db: DbSession,
    risk_free_rate: float = Query(default=0.045),
) -> PortfolioRiskReport:
    """Return a combined portfolio risk report covering history, sector exposure, and geographic exposure."""
    try:
        snapshots = db.scalars(select(DailySnapshot).order_by(DailySnapshot.date.asc())).all()
        spy_prices = _load_price_history(db, ["SPY"])
        positions = get_alpaca_client().list_positions()
        summary = get_alpaca_client().get_account_summary()
        ticker_map = _load_ticker_map(
            db,
            sorted({str(position["symbol"]).upper() for position in positions}),
        )
    except SQLAlchemyError as exc:
        raise _database_error(exc) from exc
    except Exception as exc:
        raise _alpaca_error(exc) from exc

    nav = float(summary["nav"])
    return PortfolioRiskReport(
        risk_metrics=RiskMetricsResponse(
            **compute_risk_metrics(
                snapshots,
                risk_free_rate=risk_free_rate,
                benchmark_prices=spy_prices,
            )
        ),
        sector_exposure=[
            ExposureBucket(**bucket)
            for bucket in compute_sector_exposure(positions, ticker_map, nav=nav)
        ],
        geographic_exposure=[
            ExposureBucket(**bucket)
            for bucket in compute_geographic_exposure(positions, ticker_map, nav=nav)
        ],
    )


@app.get("/portfolio/correlation", response_model=CorrelationMatrixResponse)
def get_portfolio_correlation(
    db: DbSession,
    lookback_days: int = Query(default=252, ge=20, le=2520),
) -> CorrelationMatrixResponse:
    """Return a correlation matrix for currently held symbols using local daily close history."""
    try:
        positions = get_alpaca_client().list_positions()
        symbols = sorted({str(position["symbol"]).upper() for position in positions})
        start_date = datetime.now(timezone.utc).date() - timedelta(days=max(lookback_days * 2, lookback_days + 30))
        prices = _load_price_history(db, symbols, start_date=start_date)
    except SQLAlchemyError as exc:
        raise _database_error(exc) from exc
    except Exception as exc:
        raise _alpaca_error(exc) from exc

    return CorrelationMatrixResponse(
        **compute_correlation_matrix(prices, symbols=symbols, lookback_days=lookback_days)
    )


@app.get("/portfolio/stress-test", response_model=StressTestResponse)
def get_portfolio_stress_test(db: DbSession) -> StressTestResponse:
    """Estimate portfolio sensitivity under standard market move scenarios."""
    try:
        summary = get_alpaca_client().get_account_summary()
        snapshots = db.scalars(select(DailySnapshot).order_by(DailySnapshot.date.asc())).all()
        spy_prices = _load_price_history(db, ["SPY"])
    except SQLAlchemyError as exc:
        raise _database_error(exc) from exc
    except Exception as exc:
        raise _alpaca_error(exc) from exc

    portfolio_value = float(summary["nav"])
    beta = compute_risk_metrics(snapshots, benchmark_prices=spy_prices)["beta_to_spy"]
    return StressTestResponse(
        current_portfolio_value=portfolio_value,
        scenarios=compute_stress_scenarios(portfolio_value=portfolio_value, beta=beta),
    )


@app.get("/trades", response_model=list[TradeRecord])
def list_trades(
    db: DbSession,
    ticker: str | None = Query(default=None, min_length=1, max_length=16, pattern=r"^[A-Za-z0-9.\-]+$"),
    strategy_tag: str | None = Query(default=None, min_length=1, max_length=128),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[TradeRecord]:
    """Return locally persisted trade history."""
    statement = select(Trade).order_by(Trade.timestamp.desc()).limit(limit).offset(offset)

    if ticker:
        statement = statement.where(Trade.ticker == ticker.upper())
    if strategy_tag:
        statement = statement.where(Trade.strategy_tag == strategy_tag)

    try:
        trades = db.scalars(statement).all()
    except SQLAlchemyError as exc:
        raise _database_error(exc) from exc

    return [TradeRecord.model_validate(trade) for trade in trades]


@app.post("/trade/execute", response_model=TradeResponse)
def execute_trade(
    payload: TradeRequest,
    background_tasks: BackgroundTasks,
    db: DbSession,
) -> TradeResponse:
    """Submit an Alpaca order and persist the local trade record."""
    try:
        order = get_alpaca_client().submit_order(
            symbol=payload.ticker,
            side=payload.side,
            qty=payload.qty,
            order_type=payload.order_type,
            time_in_force=payload.time_in_force,
        )
    except Exception as exc:
        raise _alpaca_error(exc) from exc

    local_trade_id = order.get("id") or f"glassbox-{uuid4()}"
    trade = Trade(
        trade_id=local_trade_id,
        ticker=payload.ticker.upper(),
        action=TradeAction.BUY if payload.side == "buy" else TradeAction.SELL,
        qty=payload.qty,
        price=order.get("filled_avg_price") or None,
        strategy_tag=payload.strategy_tag,
        investment_thesis=payload.thesis,
    )

    try:
        db.add(trade)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise _database_error(exc) from exc

    response = TradeResponse(
        order_id=order.get("id", ""),
        client_order_id=order.get("client_order_id", ""),
        status=order.get("status", ""),
        symbol=order.get("symbol", payload.ticker.upper()),
        side=order.get("side", payload.side),
        qty=order.get("qty", payload.qty),
        filled_qty=order.get("filled_qty", 0.0),
        filled_avg_price=order.get("filled_avg_price", 0.0),
        submitted_at=order.get("submitted_at"),
        local_trade_id=local_trade_id,
        strategy_tag=payload.strategy_tag,
        thesis_recorded=True,
    )
    background_tasks.add_task(_backfill_symbol_prices_in_background, payload.ticker.upper())
    return response


@app.post("/sync", response_model=SyncResponse)
def sync_portfolio(db: DbSession) -> SyncResponse:
    """Refresh the local snapshot and ticker cache from Alpaca."""
    try:
        snapshot = get_alpaca_client().build_daily_snapshot()
        positions = get_alpaca_client().list_positions()
    except Exception as exc:
        raise _alpaca_error(exc) from exc

    snapshot_date = snapshot["date"]

    try:
        existing_snapshot = db.get(DailySnapshot, snapshot_date)
        if existing_snapshot is None:
            existing_snapshot = DailySnapshot(
                date=snapshot_date,
                equity_value=snapshot["equity_value"],
                cash_balance=snapshot["cash_balance"],
                long_market_value=snapshot["long_market_value"],
                total_drawdown=snapshot["total_drawdown"],
            )
            db.add(existing_snapshot)
        else:
            existing_snapshot.equity_value = snapshot["equity_value"]
            existing_snapshot.cash_balance = snapshot["cash_balance"]
            existing_snapshot.long_market_value = snapshot["long_market_value"]
            existing_snapshot.total_drawdown = snapshot["total_drawdown"]

        new_tickers_added: list[str] = []
        for position in positions:
            symbol = str(position["symbol"]).upper()
            if db.get(Ticker, symbol) is None:
                db.add(Ticker(symbol=symbol))
                new_tickers_added.append(symbol)

        db.commit()
        backfill_all_tickers(db)
    except SQLAlchemyError as exc:
        db.rollback()
        raise _database_error(exc) from exc
    except Exception as exc:
        db.rollback()
        raise _alpaca_error(exc) from exc

    return SyncResponse(
        snapshot_date=snapshot_date,
        equity_value=float(snapshot["equity_value"]),
        cash_balance=float(snapshot["cash_balance"]),
        long_market_value=float(snapshot["long_market_value"]),
        total_drawdown=float(snapshot["total_drawdown"]),
        positions_count=len(positions),
        new_tickers_added=new_tickers_added,
    )

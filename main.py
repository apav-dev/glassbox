from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Annotated
from uuid import uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from alpaca_client import AlpacaClient
from cost_basis_schemas import CostBasisResponse, RealizedPnlRecord, RealizedPnlSummary, TickerRealizedPnl
from cost_basis_service import compute_cost_basis, rebuild_realized_pnl, record_realized_pnl_for_trade
from fundamentals_schemas import FundamentalsRefreshResult, TickerFundamentalsResponse
from fundamentals_service import get_stale_symbols, refresh_all_fundamentals, refresh_ticker_fundamentals
from models import (
    DailyPrice,
    DailySnapshot,
    RealizedPnl,
    SessionLocal,
    Ticker,
    TickerFundamentals,
    Trade,
    TradeAction,
    get_db,
    init_db,
)
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


def _load_fundamentals_map(db: Session, symbols: list[str]) -> dict[str, TickerFundamentals]:
    if not symbols:
        return {}

    rows = db.scalars(select(TickerFundamentals).where(TickerFundamentals.symbol.in_(symbols))).all()
    return {row.symbol.upper(): row for row in rows}


def _enrich_positions(
    positions: list[dict[str, float | str]],
    ticker_map: dict[str, Ticker],
    fundamentals_map: dict[str, TickerFundamentals],
) -> list[dict[str, float | str | None]]:
    enriched_positions: list[dict[str, float | str | None]] = []
    for position in positions:
        symbol = str(position["symbol"]).upper()
        ticker_record = ticker_map.get(symbol)
        fundamentals_record = fundamentals_map.get(symbol)
        enriched_positions.append(
            {
                **position,
                "company_name": ticker_record.company_name if ticker_record else None,
                "sector": ticker_record.sector if ticker_record else None,
                "industry": ticker_record.industry if ticker_record else None,
                "trailing_pe": _optional_float(
                    fundamentals_record.trailing_pe if fundamentals_record else None
                ),
                "forward_pe": _optional_float(
                    fundamentals_record.forward_pe if fundamentals_record else None
                ),
                "price_to_book": _optional_float(
                    fundamentals_record.price_to_book if fundamentals_record else None
                ),
                "target_mean_price": _optional_float(
                    fundamentals_record.target_mean_price if fundamentals_record else None
                ),
                "recommendation_key": fundamentals_record.recommendation_key if fundamentals_record else None,
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


def _refresh_symbol_fundamentals_in_background(symbol: str) -> None:
    db = SessionLocal()
    try:
        if db.get(TickerFundamentals, symbol.upper()) is None:
            refresh_ticker_fundamentals(db, symbol)
    except Exception:
        logger.exception("Failed to refresh fundamentals for %s after trade execution", symbol.upper())
        db.rollback()
    finally:
        db.close()


def _optional_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _fundamentals_response_from_record(
    symbol: str,
    record: TickerFundamentals | None,
) -> TickerFundamentalsResponse:
    if record is None:
        return TickerFundamentalsResponse(symbol=symbol.upper(), updated_at=None)

    return TickerFundamentalsResponse(
        symbol=record.symbol,
        updated_at=record.updated_at,
        market_cap=_optional_float(record.market_cap),
        trailing_pe=_optional_float(record.trailing_pe),
        forward_pe=_optional_float(record.forward_pe),
        price_to_book=_optional_float(record.price_to_book),
        ev_to_ebitda=_optional_float(record.ev_to_ebitda),
        ev_to_revenue=_optional_float(record.ev_to_revenue),
        peg_ratio=_optional_float(record.peg_ratio),
        price_to_sales=_optional_float(record.price_to_sales),
        profit_margin=_optional_float(record.profit_margin),
        gross_margin=_optional_float(record.gross_margin),
        operating_margin=_optional_float(record.operating_margin),
        ebitda_margin=_optional_float(record.ebitda_margin),
        return_on_equity=_optional_float(record.return_on_equity),
        return_on_assets=_optional_float(record.return_on_assets),
        target_high_price=_optional_float(record.target_high_price),
        target_low_price=_optional_float(record.target_low_price),
        target_mean_price=_optional_float(record.target_mean_price),
        target_median_price=_optional_float(record.target_median_price),
        analyst_count=record.analyst_count,
        recommendation_key=record.recommendation_key,
        recommendation_mean=_optional_float(record.recommendation_mean),
    )


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
            fundamentals_map = _load_fundamentals_map(db, symbols)
        except SQLAlchemyError as exc:
            raise _database_error(exc) from exc
    else:
        ticker_map = {}
        fundamentals_map = {}

    return [
        PositionResponse(**position)
        for position in _enrich_positions(positions, ticker_map, fundamentals_map)
    ]


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
        fundamentals_map = _load_fundamentals_map(db, symbols)
    except SQLAlchemyError as exc:
        raise _database_error(exc) from exc

    weighted_positions = compute_position_weights(positions, nav=float(summary["nav"]))
    enriched_positions = _enrich_positions(weighted_positions, ticker_map, fundamentals_map)
    return [PositionWithWeight(**position) for position in enriched_positions]


@app.get("/portfolio/fundamentals", response_model=list[TickerFundamentalsResponse])
def list_portfolio_fundamentals(db: DbSession) -> list[TickerFundamentalsResponse]:
    """Return fundamentals for symbols currently held in Alpaca positions."""
    try:
        positions = get_alpaca_client().list_positions()
    except Exception as exc:
        raise _alpaca_error(exc) from exc

    symbols = sorted({str(position["symbol"]).upper() for position in positions})
    try:
        fundamentals_map = _load_fundamentals_map(db, symbols)
    except SQLAlchemyError as exc:
        raise _database_error(exc) from exc

    return [_fundamentals_response_from_record(symbol, fundamentals_map.get(symbol)) for symbol in symbols]


@app.post("/portfolio/fundamentals/refresh", response_model=FundamentalsRefreshResult)
def refresh_portfolio_fundamentals(
    db: DbSession,
    symbol: str | None = Query(default=None, min_length=1, max_length=16, pattern=r"^[A-Za-z0-9.\-]+$"),
    stale_only: bool = Query(default=True),
) -> FundamentalsRefreshResult:
    """Refresh fundamentals for one ticker or the full local ticker universe."""
    try:
        if symbol:
            normalized_symbol = symbol.upper()
            if db.get(Ticker, normalized_symbol) is None:
                raise HTTPException(status_code=404, detail=f"Ticker {normalized_symbol} not found")
            symbols_to_refresh = [normalized_symbol]
        else:
            symbols_to_refresh = sorted({ticker.upper() for ticker in db.scalars(select(Ticker.symbol)).all()})

        if stale_only:
            stale_symbols = set(get_stale_symbols(db, max_age_hours=24))
            symbols_to_refresh = [item for item in symbols_to_refresh if item in stale_symbols]
    except SQLAlchemyError as exc:
        raise _database_error(exc) from exc

    refreshed: dict[str, bool] = {}
    if symbol is None and not stale_only:
        refreshed = refresh_all_fundamentals(db)
    else:
        total = len(symbols_to_refresh)
        for index, refresh_symbol in enumerate(symbols_to_refresh, start=1):
            logger.info("Refreshing fundamentals for %s (%s/%s)...", refresh_symbol, index, total)
            try:
                refreshed[refresh_symbol] = refresh_ticker_fundamentals(db, refresh_symbol)
            except Exception:
                db.rollback()
                logger.exception("Failed to refresh fundamentals for %s", refresh_symbol)
                refreshed[refresh_symbol] = False

    succeeded = sum(1 for success in refreshed.values() if success)
    failed = sum(1 for success in refreshed.values() if not success)
    return FundamentalsRefreshResult(
        refreshed=refreshed,
        total=len(refreshed),
        succeeded=succeeded,
        failed=failed,
    )


@app.get("/portfolio/fundamentals/{symbol}", response_model=TickerFundamentalsResponse)
def get_ticker_fundamentals(symbol: str, db: DbSession) -> TickerFundamentalsResponse:
    """Return stored fundamentals for a single ticker."""
    normalized_symbol = symbol.upper()
    try:
        ticker = db.get(Ticker, normalized_symbol)
        record = db.get(TickerFundamentals, normalized_symbol)
    except SQLAlchemyError as exc:
        raise _database_error(exc) from exc

    if ticker is None:
        raise HTTPException(status_code=404, detail=f"Ticker {normalized_symbol} not found")

    return _fundamentals_response_from_record(normalized_symbol, record)


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


@app.get("/portfolio/cost-basis", response_model=list[CostBasisResponse])
def get_portfolio_cost_basis(db: DbSession) -> list[CostBasisResponse]:
    """Return FIFO-based local cost basis and realized P&L by ticker."""
    try:
        trades = db.scalars(select(Trade).order_by(Trade.timestamp.asc(), Trade.trade_id.asc())).all()
    except SQLAlchemyError as exc:
        raise _database_error(exc) from exc

    cost_basis = compute_cost_basis(trades)
    return [
        CostBasisResponse(
            symbol=record.symbol,
            total_qty=record.total_qty,
            avg_cost=record.avg_cost,
            total_cost_basis=record.total_cost_basis,
            realized_pnl=record.realized_pnl,
            lots=record.lots,
        )
        for _, record in sorted(cost_basis.items(), key=lambda item: item[0])
    ]


@app.get("/portfolio/realized-pnl", response_model=RealizedPnlSummary)
def get_realized_pnl_summary(
    db: DbSession,
    ticker: str | None = Query(default=None, min_length=1, max_length=16, pattern=r"^[A-Za-z0-9.\-]+$"),
    since: datetime | None = Query(default=None),
) -> RealizedPnlSummary:
    """Return aggregate realized P&L totals from locally matched sell lots."""
    filters = []
    if ticker:
        filters.append(RealizedPnl.ticker == ticker.upper())
    if since:
        filters.append(RealizedPnl.closed_at >= since)

    try:
        by_ticker_rows = db.execute(
            select(
                RealizedPnl.ticker,
                func.sum(RealizedPnl.pnl),
                func.count(RealizedPnl.id),
            )
            .where(*filters)
            .group_by(RealizedPnl.ticker)
            .order_by(RealizedPnl.ticker.asc())
        ).all()
        total_realized_pnl = db.scalar(select(func.coalesce(func.sum(RealizedPnl.pnl), 0)).where(*filters)) or 0
    except SQLAlchemyError as exc:
        raise _database_error(exc) from exc

    return RealizedPnlSummary(
        total_realized_pnl=float(total_realized_pnl),
        by_ticker=[
            TickerRealizedPnl(
                symbol=row[0],
                realized_pnl=float(row[1] or 0),
                trade_count=int(row[2] or 0),
            )
            for row in by_ticker_rows
        ],
    )


@app.get("/portfolio/realized-pnl/details", response_model=list[RealizedPnlRecord])
def get_realized_pnl_details(
    db: DbSession,
    ticker: str | None = Query(default=None, min_length=1, max_length=16, pattern=r"^[A-Za-z0-9.\-]+$"),
    since: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[RealizedPnlRecord]:
    """Return lot-level realized P&L rows from local FIFO matching."""
    statement = select(RealizedPnl).order_by(RealizedPnl.closed_at.desc(), RealizedPnl.id.desc()).limit(limit).offset(offset)
    if ticker:
        statement = statement.where(RealizedPnl.ticker == ticker.upper())
    if since:
        statement = statement.where(RealizedPnl.closed_at >= since)

    try:
        rows = db.scalars(statement).all()
    except SQLAlchemyError as exc:
        raise _database_error(exc) from exc

    return [RealizedPnlRecord.model_validate(row) for row in rows]


@app.post("/portfolio/realized-pnl/rebuild")
def rebuild_realized_pnl_records(db: DbSession) -> dict[str, int]:
    """Rebuild the realized P&L table from the local trade log."""
    try:
        rows_rebuilt = rebuild_realized_pnl(db)
    except SQLAlchemyError as exc:
        db.rollback()
        raise _database_error(exc) from exc

    return {"rows_rebuilt": rows_rebuilt}


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
        if db.get(Ticker, payload.ticker.upper()) is None:
            db.add(Ticker(symbol=payload.ticker.upper()))
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise _database_error(exc) from exc

    if trade.action == TradeAction.SELL:
        try:
            record_realized_pnl_for_trade(db, trade)
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Failed to persist realized P&L rows for sell trade %s", trade.trade_id)

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
    background_tasks.add_task(_refresh_symbol_fundamentals_in_background, payload.ticker.upper())
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
        stale_symbols = set(get_stale_symbols(db, max_age_hours=24))
        symbols_to_refresh = sorted(set(new_tickers_added) | stale_symbols)
        total = len(symbols_to_refresh)
        for index, symbol in enumerate(symbols_to_refresh, start=1):
            logger.info("Refreshing fundamentals for %s (%s/%s)...", symbol, index, total)
            try:
                refresh_ticker_fundamentals(db, symbol)
            except Exception:
                db.rollback()
                logger.exception("Failed to refresh fundamentals for %s during sync", symbol)
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

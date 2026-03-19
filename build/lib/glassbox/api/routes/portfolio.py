from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from glassbox.api.dependencies import DbSession, get_alpaca_client
from glassbox.api.errors import alpaca_error, database_error
from glassbox.api.helpers import (
    enrich_positions,
    fundamentals_response_from_record,
    load_fundamentals_map,
    load_price_history,
    load_ticker_map,
    load_ticker_tag_map,
)
from glassbox.db.models import DailySnapshot, Ticker, TickerFundamentals
from glassbox.schemas.fundamentals import FundamentalsRefreshResult, TickerFundamentalsResponse
from glassbox.schemas.risk import (
    CorrelationMatrixResponse,
    EquityCurvePoint,
    ExposureBucket,
    PortfolioRiskReport,
    PositionWithWeight,
    RiskMetricsResponse,
    StressTestResponse,
)
from glassbox.schemas.tags import ThematicExposureBucket
from glassbox.schemas.trading import PortfolioSummaryResponse, PositionResponse, SyncResponse
from glassbox.services.fundamentals import get_stale_symbols, refresh_all_fundamentals, refresh_ticker_fundamentals
from glassbox.services.price import backfill_all_tickers
from glassbox.services.risk import (
    compute_correlation_matrix,
    compute_daily_returns,
    compute_geographic_exposure,
    compute_position_weights,
    compute_risk_metrics,
    compute_sector_exposure,
    compute_stress_scenarios,
    compute_thematic_exposure,
)


router = APIRouter(tags=["portfolio"])
logger = logging.getLogger(__name__)


@router.get("/portfolio/summary", response_model=PortfolioSummaryResponse)
def get_portfolio_summary() -> PortfolioSummaryResponse:
    """Return the current account summary from Alpaca."""
    try:
        summary = get_alpaca_client().get_account_summary()
    except Exception as exc:
        raise alpaca_error(exc) from exc

    return PortfolioSummaryResponse(**summary)


@router.get("/portfolio/positions", response_model=list[PositionResponse])
def list_portfolio_positions(db: DbSession) -> list[PositionResponse]:
    """Return Alpaca positions enriched with local ticker metadata."""
    try:
        positions = get_alpaca_client().list_positions()
    except Exception as exc:
        raise alpaca_error(exc) from exc

    symbols = sorted({str(position["symbol"]).upper() for position in positions})
    if symbols:
        try:
            ticker_map = load_ticker_map(db, symbols)
            fundamentals_map = load_fundamentals_map(db, symbols)
        except SQLAlchemyError as exc:
            raise database_error(exc) from exc
    else:
        ticker_map = {}
        fundamentals_map = {}

    return [
        PositionResponse(**position)
        for position in enrich_positions(positions, ticker_map, fundamentals_map)
    ]


@router.get("/portfolio/equity-curve", response_model=list[EquityCurvePoint])
def get_portfolio_equity_curve(db: DbSession) -> list[EquityCurvePoint]:
    """Return the local equity curve derived from stored daily snapshots."""
    try:
        snapshots = db.scalars(select(DailySnapshot).order_by(DailySnapshot.date.asc())).all()
    except SQLAlchemyError as exc:
        raise database_error(exc) from exc

    return [EquityCurvePoint(**point) for point in compute_daily_returns(snapshots)]


@router.get("/portfolio/risk", response_model=RiskMetricsResponse)
def get_portfolio_risk(
    db: DbSession,
    risk_free_rate: float = Query(default=0.045),
) -> RiskMetricsResponse:
    """Return portfolio-level return and drawdown metrics derived from stored daily snapshots."""
    try:
        snapshots = db.scalars(select(DailySnapshot).order_by(DailySnapshot.date.asc())).all()
        spy_prices = load_price_history(db, ["SPY"])
    except SQLAlchemyError as exc:
        raise database_error(exc) from exc

    return RiskMetricsResponse(
        **compute_risk_metrics(
            snapshots,
            risk_free_rate=risk_free_rate,
            benchmark_prices=spy_prices,
        )
    )


@router.get("/portfolio/positions/weighted", response_model=list[PositionWithWeight])
def list_weighted_portfolio_positions(db: DbSession) -> list[PositionWithWeight]:
    """Return live positions with current portfolio weights and local ticker metadata."""
    try:
        positions = get_alpaca_client().list_positions()
        summary = get_alpaca_client().get_account_summary()
    except Exception as exc:
        raise alpaca_error(exc) from exc

    symbols = sorted({str(position["symbol"]).upper() for position in positions})
    try:
        ticker_map = load_ticker_map(db, symbols)
        fundamentals_map = load_fundamentals_map(db, symbols)
    except SQLAlchemyError as exc:
        raise database_error(exc) from exc

    weighted_positions = compute_position_weights(positions, nav=float(summary["nav"]))
    enriched_positions = enrich_positions(weighted_positions, ticker_map, fundamentals_map)
    return [PositionWithWeight(**position) for position in enriched_positions]


@router.get("/portfolio/fundamentals", response_model=list[TickerFundamentalsResponse])
def list_portfolio_fundamentals(db: DbSession) -> list[TickerFundamentalsResponse]:
    """Return fundamentals for symbols currently held in Alpaca positions."""
    try:
        positions = get_alpaca_client().list_positions()
    except Exception as exc:
        raise alpaca_error(exc) from exc

    symbols = sorted({str(position["symbol"]).upper() for position in positions})
    try:
        fundamentals_map = load_fundamentals_map(db, symbols)
    except SQLAlchemyError as exc:
        raise database_error(exc) from exc

    return [fundamentals_response_from_record(symbol, fundamentals_map.get(symbol)) for symbol in symbols]


@router.post("/portfolio/fundamentals/refresh", response_model=FundamentalsRefreshResult)
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
        raise database_error(exc) from exc

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


@router.get("/portfolio/fundamentals/{symbol}", response_model=TickerFundamentalsResponse)
def get_ticker_fundamentals(symbol: str, db: DbSession) -> TickerFundamentalsResponse:
    """Return stored fundamentals for a single ticker."""
    normalized_symbol = symbol.upper()
    try:
        ticker = db.get(Ticker, normalized_symbol)
        record = db.get(TickerFundamentals, normalized_symbol)
    except SQLAlchemyError as exc:
        raise database_error(exc) from exc

    if ticker is None:
        raise HTTPException(status_code=404, detail=f"Ticker {normalized_symbol} not found")

    return fundamentals_response_from_record(normalized_symbol, record)


@router.get("/portfolio/exposure/sector", response_model=list[ExposureBucket])
def get_sector_exposure(db: DbSession) -> list[ExposureBucket]:
    """Return live portfolio exposure grouped by sector using local ticker metadata."""
    try:
        positions = get_alpaca_client().list_positions()
        summary = get_alpaca_client().get_account_summary()
    except Exception as exc:
        raise alpaca_error(exc) from exc

    symbols = sorted({str(position["symbol"]).upper() for position in positions})
    try:
        ticker_map = load_ticker_map(db, symbols)
    except SQLAlchemyError as exc:
        raise database_error(exc) from exc

    buckets = compute_sector_exposure(positions, ticker_map, nav=float(summary["nav"]))
    return [ExposureBucket(**bucket) for bucket in buckets]


@router.get("/portfolio/exposure/geographic", response_model=list[ExposureBucket])
def get_geographic_exposure(db: DbSession) -> list[ExposureBucket]:
    """Return live portfolio exposure grouped by country using local ticker metadata."""
    try:
        positions = get_alpaca_client().list_positions()
        summary = get_alpaca_client().get_account_summary()
    except Exception as exc:
        raise alpaca_error(exc) from exc

    symbols = sorted({str(position["symbol"]).upper() for position in positions})
    try:
        ticker_map = load_ticker_map(db, symbols)
    except SQLAlchemyError as exc:
        raise database_error(exc) from exc

    buckets = compute_geographic_exposure(positions, ticker_map, nav=float(summary["nav"]))
    return [ExposureBucket(**bucket) for bucket in buckets]


@router.get("/portfolio/exposure/thematic", response_model=list[ThematicExposureBucket])
def get_thematic_exposure(db: DbSession) -> list[ThematicExposureBucket]:
    """Return live portfolio exposure grouped by local thematic tags."""
    try:
        positions = get_alpaca_client().list_positions()
        summary = get_alpaca_client().get_account_summary()
    except Exception as exc:
        raise alpaca_error(exc) from exc

    symbols = sorted({str(position["symbol"]).upper() for position in positions})
    try:
        ticker_tag_map = load_ticker_tag_map(db, symbols)
    except SQLAlchemyError as exc:
        raise database_error(exc) from exc

    buckets = compute_thematic_exposure(positions, ticker_tag_map, nav=float(summary["nav"]))
    return [ThematicExposureBucket(**bucket) for bucket in buckets]


@router.get("/portfolio/risk/report", response_model=PortfolioRiskReport)
def get_portfolio_risk_report(
    db: DbSession,
    risk_free_rate: float = Query(default=0.045),
) -> PortfolioRiskReport:
    """Return a combined portfolio risk report covering history, sector exposure, and geographic exposure."""
    try:
        snapshots = db.scalars(select(DailySnapshot).order_by(DailySnapshot.date.asc())).all()
        spy_prices = load_price_history(db, ["SPY"])
        positions = get_alpaca_client().list_positions()
        summary = get_alpaca_client().get_account_summary()
        ticker_map = load_ticker_map(
            db,
            sorted({str(position["symbol"]).upper() for position in positions}),
        )
    except SQLAlchemyError as exc:
        raise database_error(exc) from exc
    except Exception as exc:
        raise alpaca_error(exc) from exc

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


@router.get("/portfolio/correlation", response_model=CorrelationMatrixResponse)
def get_portfolio_correlation(
    db: DbSession,
    lookback_days: int = Query(default=252, ge=20, le=2520),
) -> CorrelationMatrixResponse:
    """Return a correlation matrix for currently held symbols using local daily close history."""
    try:
        positions = get_alpaca_client().list_positions()
        symbols = sorted({str(position["symbol"]).upper() for position in positions})
        start_date = datetime.now(timezone.utc).date() - timedelta(days=max(lookback_days * 2, lookback_days + 30))
        prices = load_price_history(db, symbols, start_date=start_date)
    except SQLAlchemyError as exc:
        raise database_error(exc) from exc
    except Exception as exc:
        raise alpaca_error(exc) from exc

    return CorrelationMatrixResponse(
        **compute_correlation_matrix(prices, symbols=symbols, lookback_days=lookback_days)
    )


@router.get("/portfolio/stress-test", response_model=StressTestResponse)
def get_portfolio_stress_test(db: DbSession) -> StressTestResponse:
    """Estimate portfolio sensitivity under standard market move scenarios."""
    try:
        summary = get_alpaca_client().get_account_summary()
        snapshots = db.scalars(select(DailySnapshot).order_by(DailySnapshot.date.asc())).all()
        spy_prices = load_price_history(db, ["SPY"])
    except SQLAlchemyError as exc:
        raise database_error(exc) from exc
    except Exception as exc:
        raise alpaca_error(exc) from exc

    portfolio_value = float(summary["nav"])
    beta = compute_risk_metrics(snapshots, benchmark_prices=spy_prices)["beta_to_spy"]
    return StressTestResponse(
        current_portfolio_value=portfolio_value,
        scenarios=compute_stress_scenarios(portfolio_value=portfolio_value, beta=beta),
    )


@router.post("/sync", response_model=SyncResponse)
def sync_portfolio(db: DbSession) -> SyncResponse:
    """Refresh the local snapshot and ticker cache from Alpaca."""
    try:
        snapshot = get_alpaca_client().build_daily_snapshot()
        positions = get_alpaca_client().list_positions()
    except Exception as exc:
        raise alpaca_error(exc) from exc

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
        raise database_error(exc) from exc
    except Exception as exc:
        db.rollback()
        raise alpaca_error(exc) from exc

    return SyncResponse(
        snapshot_date=snapshot_date,
        equity_value=float(snapshot["equity_value"]),
        cash_balance=float(snapshot["cash_balance"]),
        long_market_value=float(snapshot["long_market_value"]),
        total_drawdown=float(snapshot["total_drawdown"]),
        positions_count=len(positions),
        new_tickers_added=new_tickers_added,
    )


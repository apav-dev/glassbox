from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Callable

from fastapi import HTTPException
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel
from sqlalchemy import func, select, text

from glassbox.api.dependencies import get_alpaca_client
from glassbox.api.helpers import (
    backfill_symbol_prices_in_background,
    enrich_positions,
    fundamentals_response_from_record,
    load_fundamentals_map,
    load_price_history,
    load_ticker_map,
    load_ticker_tag_map,
    load_trades_for_date_range,
    optional_float,
    refresh_symbol_fundamentals_in_background,
    validate_history_range,
)
from glassbox.db.models import (
    DailySnapshot,
    RealizedPnl,
    SessionLocal,
    Ticker,
    TickerFundamentals,
    Trade,
    init_db,
)
from glassbox.schemas.cost_basis import CostBasisResponse, RealizedPnlRecord, RealizedPnlSummary, TickerRealizedPnl
from glassbox.schemas.fundamentals import FundamentalsRefreshResult
from glassbox.schemas.position_history import DailyPositionSnapshot, PositionAtDateResponse, TickerDailyUnits
from glassbox.schemas.risk import (
    CorrelationMatrixResponse,
    EquityCurvePoint,
    ExposureBucket,
    PortfolioRiskReport,
    PositionWithWeight,
    RiskMetricsResponse,
    StressTestResponse,
)
from glassbox.schemas.tags import BulkTagRequest, TagCreate, TagResponse, TagTickerRequest, ThematicExposureBucket, TickerTagResponse
from glassbox.schemas.theses import ThesisCreate, ThesisResponse, ThesisWithFundamentals
from glassbox.schemas.trading import (
    HealthResponse,
    PortfolioSummaryResponse,
    PositionResponse,
    SyncResponse,
    TradeRecord,
    TradeRequest,
    TradeResponse,
)
from glassbox.services.cost_basis import (
    compute_cost_basis,
    rebuild_realized_pnl as rebuild_realized_pnl_service,
)
from glassbox.services.fundamentals import get_stale_symbols, refresh_all_fundamentals, refresh_ticker_fundamentals
from glassbox.services.position_history import (
    get_position_at_date,
    reconstruct_daily_positions,
    reconstruct_daily_positions_for_ticker,
)
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
from glassbox.services.tags import (
    bulk_tag_ticker as bulk_tag_ticker_service,
    create_tag as create_tag_service,
    get_tag_tickers,
    get_ticker_tags,
    list_tags as list_tags_service,
    tag_ticker as tag_ticker_service,
    untag_ticker as untag_ticker_service,
)
from glassbox.services.theses import (
    create_thesis as create_thesis_service,
    get_current_theses,
    get_current_thesis,
    get_thesis_history,
    update_thesis_status as update_thesis_status_service,
)
from glassbox.services.trading import execute_trade as execute_trade_service


logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "MCP server for the Glassbox day-trading backend. Provides portfolio analytics, "
    "trade execution, risk metrics, and thesis management."
)

init_db()

mcp = FastMCP(
    name="Glassbox Trading",
    instructions=INSTRUCTIONS,
)


@contextmanager
def _db():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _run(operation: Callable[[], dict | list]) -> dict | list:
    try:
        return operation()
    except Exception as exc:
        return {"error": True, "detail": str(exc)}


def _dump(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _dump(value.model_dump(mode="python"))
    if isinstance(value, list):
        return [_dump(item) for item in value]
    if isinstance(value, tuple):
        return [_dump(item) for item in value]
    if isinstance(value, dict):
        return {key: _dump(item) for key, item in value.items()}
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _parse_datetime(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _require_ticker(db: Any, symbol: str) -> str:
    normalized_symbol = symbol.upper()
    if db.get(Ticker, normalized_symbol) is None:
        raise HTTPException(status_code=404, detail=f"Ticker {normalized_symbol} not found")
    return normalized_symbol


def _validate_int_range(value: int, *, name: str, minimum: int, maximum: int | None = None) -> None:
    if value < minimum:
        if maximum is None:
            raise HTTPException(status_code=400, detail=f"{name} must be greater than or equal to {minimum}")
        raise HTTPException(status_code=400, detail=f"{name} must be between {minimum} and {maximum}")
    if maximum is not None and value > maximum:
        raise HTTPException(status_code=400, detail=f"{name} must be between {minimum} and {maximum}")


@mcp.tool()
def health_check() -> dict | list:
    """Returns local database and Alpaca connectivity status. Use this to confirm the glassbox backend is reachable and healthy."""
    def operation() -> dict:
        db_connected = False
        alpaca_connected = False

        with _db() as db:
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
        return _dump(
            HealthResponse(
                status=status,
                db_connected=db_connected,
                alpaca_connected=alpaca_connected,
            )
        )

    return _run(operation)


@mcp.tool()
def sync_portfolio() -> dict | list:
    """Refreshes the local daily snapshot and ticker cache from Alpaca. Use this before analytics when you need the latest synced portfolio state."""
    def operation() -> dict:
        alpaca_client = get_alpaca_client()
        snapshot = alpaca_client.build_daily_snapshot()
        positions = alpaca_client.list_positions()
        snapshot_date = snapshot["date"]

        with _db() as db:
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

        return _dump(
            SyncResponse(
                snapshot_date=snapshot_date,
                equity_value=float(snapshot["equity_value"]),
                cash_balance=float(snapshot["cash_balance"]),
                long_market_value=float(snapshot["long_market_value"]),
                total_drawdown=float(snapshot["total_drawdown"]),
                positions_count=len(positions),
                new_tickers_added=new_tickers_added,
            )
        )

    return _run(operation)


@mcp.tool()
def get_portfolio_summary() -> dict | list:
    """Returns current account NAV, equity, cash, buying power, and daily P&L from Alpaca. Use this to check the overall portfolio state."""
    return _run(lambda: _dump(PortfolioSummaryResponse(**get_alpaca_client().get_account_summary())))


@mcp.tool()
def list_positions() -> dict | list:
    """Returns current Alpaca positions enriched with local company and fundamentals data. Use this to review the live holdings in detail."""
    def operation() -> list[dict]:
        positions = get_alpaca_client().list_positions()
        symbols = sorted({str(position["symbol"]).upper() for position in positions})

        with _db() as db:
            ticker_map = load_ticker_map(db, symbols) if symbols else {}
            fundamentals_map = load_fundamentals_map(db, symbols) if symbols else {}

        return _dump(
            [PositionResponse(**position) for position in enrich_positions(positions, ticker_map, fundamentals_map)]
        )

    return _run(operation)


@mcp.tool()
def list_weighted_positions() -> dict | list:
    """Returns current positions with portfolio weights plus local enrichment fields. Use this to see how concentrated the portfolio is by position."""
    def operation() -> list[dict]:
        alpaca_client = get_alpaca_client()
        positions = alpaca_client.list_positions()
        summary = alpaca_client.get_account_summary()
        symbols = sorted({str(position["symbol"]).upper() for position in positions})

        with _db() as db:
            ticker_map = load_ticker_map(db, symbols)
            fundamentals_map = load_fundamentals_map(db, symbols)

        weighted_positions = compute_position_weights(positions, nav=float(summary["nav"]))
        enriched_positions = enrich_positions(weighted_positions, ticker_map, fundamentals_map)
        return _dump([PositionWithWeight(**position) for position in enriched_positions])

    return _run(operation)


@mcp.tool()
def execute_trade(
    ticker: str,
    side: str,
    qty: float,
    thesis: str,
    strategy_tag: str | None = None,
    order_type: str = "market",
    time_in_force: str = "day",
) -> dict | list:
    """Submits an Alpaca order, records the trade ledger, syncs buy theses, and closes theses on full exits."""
    def operation() -> dict:
        payload = TradeRequest(
            ticker=ticker,
            side=side,
            qty=qty,
            thesis=thesis,
            strategy_tag=strategy_tag,
            order_type=order_type,
            time_in_force=time_in_force,
        )
        alpaca_client = get_alpaca_client()

        with _db() as db:
            result = execute_trade_service(db, alpaca_client, payload)

        response = TradeResponse(
            order_id=result.order.get("id", ""),
            client_order_id=result.order.get("client_order_id", ""),
            status=result.order.get("status", ""),
            symbol=result.order.get("symbol", payload.ticker.upper()),
            side=result.order.get("side", payload.side),
            qty=result.order.get("qty", payload.qty),
            filled_qty=result.order.get("filled_qty", 0.0),
            filled_avg_price=result.order.get("filled_avg_price", 0.0),
            submitted_at=result.order.get("submitted_at"),
            local_trade_id=result.local_trade_id,
            strategy_tag=payload.strategy_tag,
            thesis_recorded=result.thesis_recorded,
            thesis_action=result.thesis_action,
        )
        backfill_symbol_prices_in_background(payload.ticker.upper())
        refresh_symbol_fundamentals_in_background(payload.ticker.upper())
        return _dump(response)

    return _run(operation)


@mcp.tool()
def list_trades(
    ticker: str | None = None,
    strategy_tag: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict | list:
    """Returns locally persisted trade history with optional symbol and strategy filters. Use this to review past executions and metadata."""
    def operation() -> list[dict]:
        _validate_int_range(limit, name="limit", minimum=1, maximum=500)
        _validate_int_range(offset, name="offset", minimum=0)
        statement = select(Trade).order_by(Trade.timestamp.desc()).limit(limit).offset(offset)
        if ticker:
            statement = statement.where(Trade.ticker == ticker.upper())
        if strategy_tag:
            statement = statement.where(Trade.strategy_tag == strategy_tag)

        with _db() as db:
            trades = db.scalars(statement).all()

        return _dump([TradeRecord.model_validate(trade) for trade in trades])

    return _run(operation)


@mcp.tool()
def get_risk_metrics(risk_free_rate: float = 0.045) -> dict | list:
    """Returns portfolio-level risk analytics including Sharpe ratio, Sortino ratio, VaR, max drawdown, and beta to SPY. Use this to evaluate portfolio risk and performance quality."""
    def operation() -> dict:
        with _db() as db:
            snapshots = db.scalars(select(DailySnapshot).order_by(DailySnapshot.date.asc())).all()
            spy_prices = load_price_history(db, ["SPY"])

        return _dump(
            RiskMetricsResponse(
                **compute_risk_metrics(
                    snapshots,
                    risk_free_rate=risk_free_rate,
                    benchmark_prices=spy_prices,
                )
            )
        )

    return _run(operation)


@mcp.tool()
def get_equity_curve() -> dict | list:
    """Returns the stored equity curve with daily returns and drawdown. Use this to inspect portfolio performance over time."""
    def operation() -> list[dict]:
        with _db() as db:
            snapshots = db.scalars(select(DailySnapshot).order_by(DailySnapshot.date.asc())).all()

        return _dump([EquityCurvePoint(**point) for point in compute_daily_returns(snapshots)])

    return _run(operation)


@mcp.tool()
def get_correlation_matrix(lookback_days: int = 252) -> dict | list:
    """Returns a correlation matrix for currently held symbols using local price history. Use this to check diversification and co-movement across positions."""
    def operation() -> dict:
        _validate_int_range(lookback_days, name="lookback_days", minimum=20, maximum=2520)
        alpaca_client = get_alpaca_client()
        positions = alpaca_client.list_positions()
        symbols = sorted({str(position["symbol"]).upper() for position in positions})
        start_date = datetime.now(timezone.utc).date() - timedelta(days=max(lookback_days * 2, lookback_days + 30))

        with _db() as db:
            prices = load_price_history(db, symbols, start_date=start_date)

        return _dump(
            CorrelationMatrixResponse(
                **compute_correlation_matrix(prices, symbols=symbols, lookback_days=lookback_days)
            )
        )

    return _run(operation)


@mcp.tool()
def get_stress_test() -> dict | list:
    """Returns portfolio stress scenarios for standard market moves. Use this to estimate potential portfolio impact under broad market shocks."""
    def operation() -> dict:
        alpaca_client = get_alpaca_client()
        summary = alpaca_client.get_account_summary()

        with _db() as db:
            snapshots = db.scalars(select(DailySnapshot).order_by(DailySnapshot.date.asc())).all()
            spy_prices = load_price_history(db, ["SPY"])

        portfolio_value = float(summary["nav"])
        beta = compute_risk_metrics(snapshots, benchmark_prices=spy_prices)["beta_to_spy"]
        return _dump(
            StressTestResponse(
                current_portfolio_value=portfolio_value,
                scenarios=compute_stress_scenarios(portfolio_value=portfolio_value, beta=beta),
            )
        )

    return _run(operation)


@mcp.tool()
def get_risk_report(risk_free_rate: float = 0.045) -> dict | list:
    """Returns a combined risk report with portfolio metrics, sector exposure, and geographic exposure. Use this for a broader portfolio risk review."""
    def operation() -> dict:
        alpaca_client = get_alpaca_client()
        positions = alpaca_client.list_positions()
        summary = alpaca_client.get_account_summary()
        symbols = sorted({str(position["symbol"]).upper() for position in positions})

        with _db() as db:
            snapshots = db.scalars(select(DailySnapshot).order_by(DailySnapshot.date.asc())).all()
            spy_prices = load_price_history(db, ["SPY"])
            ticker_map = load_ticker_map(db, symbols)

        nav = float(summary["nav"])
        return _dump(
            PortfolioRiskReport(
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
        )

    return _run(operation)


@mcp.tool()
def get_sector_exposure() -> dict | list:
    """Returns live portfolio exposure grouped by sector. Use this to understand concentration by industry sector."""
    def operation() -> list[dict]:
        alpaca_client = get_alpaca_client()
        positions = alpaca_client.list_positions()
        summary = alpaca_client.get_account_summary()
        symbols = sorted({str(position["symbol"]).upper() for position in positions})

        with _db() as db:
            ticker_map = load_ticker_map(db, symbols)

        buckets = compute_sector_exposure(positions, ticker_map, nav=float(summary["nav"]))
        return _dump([ExposureBucket(**bucket) for bucket in buckets])

    return _run(operation)


@mcp.tool()
def get_geographic_exposure() -> dict | list:
    """Returns live portfolio exposure grouped by geography. Use this to understand country-level concentration in the portfolio."""
    def operation() -> list[dict]:
        alpaca_client = get_alpaca_client()
        positions = alpaca_client.list_positions()
        summary = alpaca_client.get_account_summary()
        symbols = sorted({str(position["symbol"]).upper() for position in positions})

        with _db() as db:
            ticker_map = load_ticker_map(db, symbols)

        buckets = compute_geographic_exposure(positions, ticker_map, nav=float(summary["nav"]))
        return _dump([ExposureBucket(**bucket) for bucket in buckets])

    return _run(operation)


@mcp.tool()
def get_thematic_exposure() -> dict | list:
    """Returns live portfolio exposure grouped by thematic tags. Use this to review how portfolio value is distributed across your custom themes."""
    def operation() -> list[dict]:
        alpaca_client = get_alpaca_client()
        positions = alpaca_client.list_positions()
        summary = alpaca_client.get_account_summary()
        symbols = sorted({str(position["symbol"]).upper() for position in positions})

        with _db() as db:
            ticker_tag_map = load_ticker_tag_map(db, symbols)

        buckets = compute_thematic_exposure(positions, ticker_tag_map, nav=float(summary["nav"]))
        return _dump([ThematicExposureBucket(**bucket) for bucket in buckets])

    return _run(operation)


@mcp.tool()
def get_cost_basis() -> dict | list:
    """Returns FIFO cost basis, lot data, and realized P&L by ticker from the local trade ledger. Use this to inspect entry prices and tax-lot state."""
    def operation() -> list[dict]:
        with _db() as db:
            trades = db.scalars(select(Trade).order_by(Trade.timestamp.asc(), Trade.trade_id.asc())).all()

        cost_basis = compute_cost_basis(trades)
        return _dump(
            [
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
        )

    return _run(operation)


@mcp.tool()
def get_realized_pnl(
    ticker: str | None = None,
    since: str | None = None,
) -> dict | list:
    """Returns aggregate realized P&L totals with an optional ticker or start time filter. Use this to summarize closed gains and losses."""
    def operation() -> dict:
        filters = []
        if ticker:
            filters.append(RealizedPnl.ticker == ticker.upper())
        if since:
            filters.append(RealizedPnl.closed_at >= _parse_datetime(since))

        with _db() as db:
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

        return _dump(
            RealizedPnlSummary(
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
        )

    return _run(operation)


@mcp.tool()
def get_realized_pnl_details(
    ticker: str | None = None,
    since: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict | list:
    """Returns lot-level realized P&L records with optional filters and pagination. Use this when you need the underlying sell-lot detail."""
    def operation() -> list[dict]:
        _validate_int_range(limit, name="limit", minimum=1, maximum=500)
        _validate_int_range(offset, name="offset", minimum=0)
        statement = select(RealizedPnl).order_by(RealizedPnl.closed_at.desc(), RealizedPnl.id.desc()).limit(limit).offset(offset)
        if ticker:
            statement = statement.where(RealizedPnl.ticker == ticker.upper())
        if since:
            statement = statement.where(RealizedPnl.closed_at >= _parse_datetime(since))

        with _db() as db:
            rows = db.scalars(statement).all()

        return _dump([RealizedPnlRecord.model_validate(row) for row in rows])

    return _run(operation)


@mcp.tool()
def rebuild_realized_pnl() -> dict | list:
    """Rebuilds the realized P&L table from the local trade log. Use this after ledger changes when you need to recompute realized gains and losses."""
    def operation() -> dict:
        alpaca_client = get_alpaca_client()
        with _db() as db:
            rows_rebuilt = rebuild_realized_pnl_service(
                db,
                price_resolver=lambda trade_id: alpaca_client.get_order(trade_id).get("filled_avg_price"),
            )
        return {"rows_rebuilt": rows_rebuilt}

    return _run(operation)


@mcp.tool()
def list_fundamentals() -> dict | list:
    """Returns stored fundamentals for currently held symbols. Use this to review valuation and analyst data across active positions."""
    def operation() -> list[dict]:
        positions = get_alpaca_client().list_positions()
        symbols = sorted({str(position["symbol"]).upper() for position in positions})

        with _db() as db:
            fundamentals_map = load_fundamentals_map(db, symbols)

        return _dump([fundamentals_response_from_record(symbol, fundamentals_map.get(symbol)) for symbol in symbols])

    return _run(operation)


@mcp.tool()
def get_ticker_fundamentals(symbol: str) -> dict | list:
    """Returns stored fundamentals for one ticker. Use this to inspect valuation and analyst fields for a specific symbol."""
    def operation() -> dict:
        normalized_symbol = symbol.upper()
        with _db() as db:
            _require_ticker(db, normalized_symbol)
            record = db.get(TickerFundamentals, normalized_symbol)
        return _dump(fundamentals_response_from_record(normalized_symbol, record))

    return _run(operation)


@mcp.tool()
def refresh_fundamentals(
    symbol: str | None = None,
    stale_only: bool = True,
) -> dict | list:
    """Refreshes fundamentals for one ticker or the full local ticker universe. Use this when fundamentals may be stale or missing."""
    def operation() -> dict:
        with _db() as db:
            if symbol:
                normalized_symbol = _require_ticker(db, symbol)
                symbols_to_refresh = [normalized_symbol]
            else:
                symbols_to_refresh = sorted({ticker.upper() for ticker in db.scalars(select(Ticker.symbol)).all()})

            if stale_only:
                stale_symbols = set(get_stale_symbols(db, max_age_hours=24))
                symbols_to_refresh = [item for item in symbols_to_refresh if item in stale_symbols]

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
        return _dump(
            FundamentalsRefreshResult(
                refreshed=refreshed,
                total=len(refreshed),
                succeeded=succeeded,
                failed=failed,
            )
        )

    return _run(operation)


@mcp.tool()
def list_theses(
    status: str | None = None,
    symbol: str | None = None,
) -> dict | list:
    """Returns current thesis records with optional status or symbol filters. Use this to review the active investment narrative across names."""
    def operation() -> list[dict]:
        with _db() as db:
            records = get_current_theses(db, symbols=[symbol] if symbol else None, status=status)
        return _dump([ThesisResponse.model_validate(record) for record in records])

    return _run(operation)


@mcp.tool()
def get_thesis(symbol: str) -> dict | list:
    """Returns the current thesis record for one symbol. Use this when you need the latest thesis for a specific position or watchlist name."""
    def operation() -> dict:
        with _db() as db:
            record = get_current_thesis(db, symbol)
        if record is None:
            raise HTTPException(status_code=404, detail=f"No thesis found for {symbol.upper()}")
        return _dump(ThesisResponse.model_validate(record))

    return _run(operation)


@mcp.tool()
def create_thesis(
    symbol: str,
    thesis: str,
    catalyst: str | None = None,
    edge: str | None = None,
    status: str = "active",
    created_by: str = "agent",
) -> dict | list:
    """Creates a new thesis version for a symbol. Use this to record or update the investment rationale behind a name."""
    def operation() -> dict:
        payload = ThesisCreate(
            symbol=symbol,
            thesis=thesis,
            catalyst=catalyst,
            edge=edge,
            status=status,
            created_by=created_by,
        )
        with _db() as db:
            record = create_thesis_service(
                db,
                symbol=payload.symbol,
                thesis=payload.thesis,
                catalyst=payload.catalyst,
                edge=payload.edge,
                status=payload.status,
                created_by=payload.created_by,
            )
        return _dump(ThesisResponse.model_validate(record))

    return _run(operation)


@mcp.tool()
def update_thesis_status(symbol: str, status: str) -> dict | list:
    """Updates the status of the current thesis for a symbol. Use this to move a name between active, closed, and watching states."""
    def operation() -> dict:
        with _db() as db:
            record = update_thesis_status_service(db, symbol, status)
        if record is None:
            raise HTTPException(status_code=404, detail=f"No thesis found for {symbol.upper()}")
        return _dump(ThesisResponse.model_validate(record))

    return _run(operation)


@mcp.tool()
def list_thesis_history(symbol: str) -> dict | list:
    """Returns all thesis versions for a symbol. Use this to review how the thesis evolved over time."""
    def operation() -> list[dict]:
        with _db() as db:
            records = get_thesis_history(db, symbol)
        return _dump([ThesisResponse.model_validate(record) for record in records])

    return _run(operation)


@mcp.tool()
def list_enriched_theses() -> dict | list:
    """Returns current active theses enriched with portfolio weight and key fundamentals. Use this for a portfolio-level view of thesis quality and exposure."""
    def operation() -> list[dict]:
        with _db() as db:
            thesis_rows = get_current_theses(db, status="active")
            if not thesis_rows:
                return []
            symbols = [row.symbol for row in thesis_rows]
            fundamentals_map = load_fundamentals_map(db, symbols)

        alpaca_client = get_alpaca_client()
        positions = alpaca_client.list_positions()
        summary = alpaca_client.get_account_summary()
        weighted_positions = compute_position_weights(positions, nav=float(summary["nav"]))
        portfolio_weight_map = {
            str(position["symbol"]).upper(): float(position["portfolio_weight"])
            for position in weighted_positions
        }

        enriched_rows = [
            ThesisWithFundamentals(
                id=row.id,
                symbol=row.symbol,
                version=row.version,
                thesis=row.thesis,
                catalyst=row.catalyst,
                edge=row.edge,
                status=row.status,
                created_at=row.created_at,
                created_by=row.created_by,
                portfolio_weight=portfolio_weight_map.get(row.symbol, 0.0),
                trailing_pe=optional_float(fundamentals_map.get(row.symbol).trailing_pe) if fundamentals_map.get(row.symbol) else None,
                target_mean_price=optional_float(fundamentals_map.get(row.symbol).target_mean_price) if fundamentals_map.get(row.symbol) else None,
                recommendation_key=fundamentals_map.get(row.symbol).recommendation_key if fundamentals_map.get(row.symbol) else None,
            )
            for row in thesis_rows
        ]
        return _dump(enriched_rows)

    return _run(operation)


@mcp.tool()
def list_tags() -> dict | list:
    """Returns all local thematic tags. Use this to inspect the available tagging taxonomy."""
    def operation() -> list[dict]:
        with _db() as db:
            rows = list_tags_service(db)
        return _dump([TagResponse.model_validate(row) for row in rows])

    return _run(operation)


@mcp.tool()
def create_tag(
    name: str,
    color: str | None = None,
    description: str | None = None,
) -> dict | list:
    """Creates a thematic tag if it does not already exist. Use this before assigning a new theme to one or more tickers."""
    def operation() -> dict:
        payload = TagCreate(name=name, color=color, description=description)
        with _db() as db:
            row = create_tag_service(db, name=payload.name, color=payload.color, description=payload.description)
        return _dump(TagResponse.model_validate(row))

    return _run(operation)


@mcp.tool()
def list_tagged_tickers(tag_name: str) -> dict | list:
    """Returns all symbols assigned to a thematic tag. Use this to inspect the membership of one theme."""
    def operation() -> list[str]:
        with _db() as db:
            return _dump(get_tag_tickers(db, tag_name))

    return _run(operation)


@mcp.tool()
def list_ticker_tags(symbol: str) -> dict | list:
    """Returns all thematic tags assigned to a ticker. Use this to review how a symbol is classified."""
    def operation() -> list[dict]:
        with _db() as db:
            rows = get_ticker_tags(db, symbol)
        return _dump([TagResponse.model_validate(row) for row in rows])

    return _run(operation)


@mcp.tool()
def tag_ticker(
    symbol: str,
    tag_name: str,
    tagged_by: str = "agent",
) -> dict | list:
    """Applies one thematic tag to a ticker. Use this to associate a symbol with a strategy or theme."""
    def operation() -> dict:
        payload = TagTickerRequest(symbol=symbol, tag_name=tag_name, tagged_by=tagged_by)
        if payload.symbol.upper() != symbol.upper():
            raise HTTPException(status_code=400, detail="Path symbol must match payload symbol")
        with _db() as db:
            row = tag_ticker_service(db, symbol=symbol, tag_name=payload.tag_name, tagged_by=payload.tagged_by)
        return _dump(TickerTagResponse.model_validate(row))

    return _run(operation)


@mcp.tool()
def bulk_tag_ticker(
    symbol: str,
    tag_names: list[str],
    tagged_by: str = "agent",
) -> dict | list:
    """Applies multiple thematic tags to a ticker in one request. Use this when classifying a symbol across several themes at once."""
    def operation() -> list[dict]:
        payload = BulkTagRequest(symbol=symbol, tag_names=tag_names, tagged_by=tagged_by)
        if payload.symbol.upper() != symbol.upper():
            raise HTTPException(status_code=400, detail="Path symbol must match payload symbol")
        with _db() as db:
            rows = bulk_tag_ticker_service(db, symbol=symbol, tag_names=payload.tag_names, tagged_by=payload.tagged_by)
        return _dump([TickerTagResponse.model_validate(row) for row in rows])

    return _run(operation)


@mcp.tool()
def untag_ticker(symbol: str, tag_name: str) -> dict | list:
    """Removes a thematic tag from a ticker. Use this when a symbol no longer belongs in a theme or strategy bucket."""
    def operation() -> dict:
        with _db() as db:
            removed = untag_ticker_service(db, symbol=symbol, tag_name=tag_name)
        return {"removed": removed}

    return _run(operation)


@mcp.tool()
def get_position_history(
    start_date: str,
    end_date: str,
    starting_cash: float,
) -> dict | list:
    """Returns reconstructed daily positions and cash over a date range. Use this to review how the portfolio evolved through time."""
    def operation() -> list[dict]:
        parsed_start_date = _parse_date(start_date)
        parsed_end_date = _parse_date(end_date)
        validate_history_range(parsed_start_date, parsed_end_date)

        with _db() as db:
            trades = load_trades_for_date_range(db, start_date=parsed_start_date, end_date=parsed_end_date)

        return _dump(
            [
                DailyPositionSnapshot(**snapshot)
                for snapshot in reconstruct_daily_positions(
                    trades,
                    start_date=parsed_start_date,
                    end_date=parsed_end_date,
                    starting_cash=starting_cash,
                )
            ]
        )

    return _run(operation)


@mcp.tool()
def get_symbol_position_history(
    symbol: str,
    start_date: str,
    end_date: str,
) -> dict | list:
    """Returns reconstructed daily share counts for one ticker over a date range. Use this to inspect position sizing history for a symbol."""
    def operation() -> list[dict]:
        normalized_symbol = symbol.upper()
        parsed_start_date = _parse_date(start_date)
        parsed_end_date = _parse_date(end_date)
        validate_history_range(parsed_start_date, parsed_end_date)

        with _db() as db:
            trades = load_trades_for_date_range(
                db,
                start_date=parsed_start_date,
                end_date=parsed_end_date,
                symbol=normalized_symbol,
            )

        return _dump(
            [
                TickerDailyUnits(**snapshot)
                for snapshot in reconstruct_daily_positions_for_ticker(
                    trades,
                    symbol=normalized_symbol,
                    start_date=parsed_start_date,
                    end_date=parsed_end_date,
                )
            ]
        )

    return _run(operation)


@mcp.tool()
def get_symbol_position_at_date(symbol: str, date: str) -> dict | list:
    """Returns the reconstructed share count for one ticker on a specific date. Use this to answer point-in-time position questions."""
    def operation() -> dict:
        normalized_symbol = symbol.upper()
        parsed_date = _parse_date(date)

        with _db() as db:
            trades = load_trades_for_date_range(db, start_date=parsed_date, end_date=parsed_date, symbol=normalized_symbol)

        return _dump(
            PositionAtDateResponse(
                symbol=normalized_symbol,
                date=parsed_date,
                shares=get_position_at_date(trades, normalized_symbol, target_date=parsed_date),
            )
        )

    return _run(operation)


def main() -> None:
    mcp.run(transport="stdio")

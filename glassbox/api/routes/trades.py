from __future__ import annotations

import logging
from datetime import date, datetime
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Query
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from glassbox.api.dependencies import DbSession, get_alpaca_client
from glassbox.api.errors import alpaca_error, database_error
from glassbox.api.helpers import (
    backfill_symbol_prices_in_background,
    load_trades_for_date_range,
    refresh_symbol_fundamentals_in_background,
    validate_history_range,
)
from glassbox.db.models import RealizedPnl, Ticker, Trade, TradeAction
from glassbox.schemas.cost_basis import CostBasisResponse, RealizedPnlRecord, RealizedPnlSummary, TickerRealizedPnl
from glassbox.schemas.position_history import DailyPositionSnapshot, PositionAtDateResponse, TickerDailyUnits
from glassbox.schemas.trading import TradeRecord, TradeRequest, TradeResponse
from glassbox.services.cost_basis import compute_cost_basis, rebuild_realized_pnl, record_realized_pnl_for_trade
from glassbox.services.position_history import (
    get_position_at_date,
    reconstruct_daily_positions,
    reconstruct_daily_positions_for_ticker,
)
from glassbox.services.theses import close_thesis


router = APIRouter(tags=["trades"])
logger = logging.getLogger(__name__)


@router.get("/portfolio/cost-basis", response_model=list[CostBasisResponse])
def get_portfolio_cost_basis(db: DbSession) -> list[CostBasisResponse]:
    """Return FIFO-based local cost basis and realized P&L by ticker."""
    try:
        trades = db.scalars(select(Trade).order_by(Trade.timestamp.asc(), Trade.trade_id.asc())).all()
    except SQLAlchemyError as exc:
        raise database_error(exc) from exc

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


@router.get("/portfolio/realized-pnl", response_model=RealizedPnlSummary)
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
        raise database_error(exc) from exc

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


@router.get("/portfolio/realized-pnl/details", response_model=list[RealizedPnlRecord])
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
        raise database_error(exc) from exc

    return [RealizedPnlRecord.model_validate(row) for row in rows]


@router.post("/portfolio/realized-pnl/rebuild")
def rebuild_realized_pnl_records(db: DbSession) -> dict[str, int]:
    """Rebuild the realized P&L table from the local trade log."""
    try:
        rows_rebuilt = rebuild_realized_pnl(db)
    except SQLAlchemyError as exc:
        db.rollback()
        raise database_error(exc) from exc

    return {"rows_rebuilt": rows_rebuilt}


@router.get("/portfolio/history/positions", response_model=list[DailyPositionSnapshot])
def get_portfolio_position_history(
    db: DbSession,
    start_date: date = Query(...),
    end_date: date = Query(...),
    starting_cash: float = Query(...),
) -> list[DailyPositionSnapshot]:
    """Reconstruct end-of-day positions and cash from the local trade ledger."""
    validate_history_range(start_date, end_date)

    try:
        trades = load_trades_for_date_range(db, start_date=start_date, end_date=end_date)
    except SQLAlchemyError as exc:
        raise database_error(exc) from exc

    return [
        DailyPositionSnapshot(**snapshot)
        for snapshot in reconstruct_daily_positions(
            trades,
            start_date=start_date,
            end_date=end_date,
            starting_cash=starting_cash,
        )
    ]


@router.get("/portfolio/history/positions/{symbol}/at", response_model=PositionAtDateResponse)
def get_symbol_position_at_date(
    symbol: str,
    db: DbSession,
    date: date = Query(...),
) -> PositionAtDateResponse:
    """Return the reconstructed share count for one ticker on a specific date."""
    normalized_symbol = symbol.upper()
    try:
        trades = load_trades_for_date_range(db, start_date=date, end_date=date, symbol=normalized_symbol)
    except SQLAlchemyError as exc:
        raise database_error(exc) from exc

    return PositionAtDateResponse(
        symbol=normalized_symbol,
        date=date,
        shares=get_position_at_date(trades, normalized_symbol, target_date=date),
    )


@router.get("/portfolio/history/positions/{symbol}", response_model=list[TickerDailyUnits])
def get_symbol_position_history(
    symbol: str,
    db: DbSession,
    start_date: date = Query(...),
    end_date: date = Query(...),
) -> list[TickerDailyUnits]:
    """Return reconstructed daily share counts for one ticker."""
    normalized_symbol = symbol.upper()
    validate_history_range(start_date, end_date)

    try:
        trades = load_trades_for_date_range(
            db,
            start_date=start_date,
            end_date=end_date,
            symbol=normalized_symbol,
        )
    except SQLAlchemyError as exc:
        raise database_error(exc) from exc

    return [
        TickerDailyUnits(**snapshot)
        for snapshot in reconstruct_daily_positions_for_ticker(
            trades,
            symbol=normalized_symbol,
            start_date=start_date,
            end_date=end_date,
        )
    ]


@router.get("/trades", response_model=list[TradeRecord])
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
        raise database_error(exc) from exc

    return [TradeRecord.model_validate(trade) for trade in trades]


@router.post("/trade/execute", response_model=TradeResponse)
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
        raise alpaca_error(exc) from exc

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
        raise database_error(exc) from exc

    if trade.action == TradeAction.SELL:
        try:
            record_realized_pnl_for_trade(db, trade)
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Failed to persist realized P&L rows for sell trade %s", trade.trade_id)

        try:
            positions = get_alpaca_client().list_positions()
            open_symbols = {str(position["symbol"]).upper() for position in positions}
            if payload.ticker.upper() not in open_symbols:
                close_thesis(db, payload.ticker.upper())
        except Exception:
            db.rollback()
            logger.exception(
                "Failed to auto-close thesis for %s after sell trade %s",
                payload.ticker.upper(),
                trade.trade_id,
            )

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
    background_tasks.add_task(backfill_symbol_prices_in_background, payload.ticker.upper())
    background_tasks.add_task(refresh_symbol_fundamentals_in_background, payload.ticker.upper())
    return response


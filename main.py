from __future__ import annotations

from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from alpaca_client import AlpacaClient
from models import DailySnapshot, Ticker, Trade, TradeAction, get_db, init_db
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


@lru_cache(maxsize=1)
def get_alpaca_client() -> AlpacaClient:
    return AlpacaClient()


def _alpaca_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=502, detail=f"Alpaca error: {exc}")


def _database_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=500, detail=f"Database error: {exc}")


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
    ticker_map: dict[str, Ticker] = {}

    if symbols:
        try:
            ticker_rows = db.scalars(select(Ticker).where(Ticker.symbol.in_(symbols))).all()
        except SQLAlchemyError as exc:
            raise _database_error(exc) from exc
        ticker_map = {ticker.symbol.upper(): ticker for ticker in ticker_rows}

    enriched_positions: list[PositionResponse] = []
    for position in positions:
        ticker_record = ticker_map.get(str(position["symbol"]).upper())
        enriched_positions.append(
            PositionResponse(
                **position,
                company_name=ticker_record.company_name if ticker_record else None,
                sector=ticker_record.sector if ticker_record else None,
                industry=ticker_record.industry if ticker_record else None,
            )
        )

    return enriched_positions


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
def execute_trade(payload: TradeRequest, db: DbSession) -> TradeResponse:
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

    return TradeResponse(
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
    except SQLAlchemyError as exc:
        db.rollback()
        raise _database_error(exc) from exc

    return SyncResponse(
        snapshot_date=snapshot_date,
        equity_value=float(snapshot["equity_value"]),
        cash_balance=float(snapshot["cash_balance"]),
        long_market_value=float(snapshot["long_market_value"]),
        total_drawdown=float(snapshot["total_drawdown"]),
        positions_count=len(positions),
        new_tickers_added=new_tickers_added,
    )

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from glassbox.db.models import Ticker, Trade, TradeAction
from glassbox.schemas.trading import ThesisAction, TradeRequest
from glassbox.services.cost_basis import record_realized_pnl_for_trade
from glassbox.services.theses import close_thesis, sync_buy_trade_thesis


logger = logging.getLogger(__name__)

THESIS_MUTATION_ACTIONS = {"created", "versioned", "closed"}


@dataclass(slots=True)
class TradeExecutionResult:
    order: dict[str, Any]
    local_trade_id: str
    thesis_action: ThesisAction

    @property
    def thesis_recorded(self) -> bool:
        return self.thesis_action in THESIS_MUTATION_ACTIONS


def execute_trade(db: Session, alpaca_client: Any, payload: TradeRequest) -> TradeExecutionResult:
    order = alpaca_client.submit_order(
        symbol=payload.ticker,
        side=payload.side,
        qty=payload.qty,
        order_type=payload.order_type,
        time_in_force=payload.time_in_force,
    )

    local_trade_id = order.get("id") or f"glassbox-{uuid4()}"
    symbol = payload.ticker.upper()
    trade = Trade(
        trade_id=local_trade_id,
        ticker=symbol,
        action=TradeAction.BUY if payload.side == "buy" else TradeAction.SELL,
        qty=payload.qty,
        price=order.get("filled_avg_price") or None,
        strategy_tag=payload.strategy_tag,
        investment_thesis=payload.thesis,
    )

    thesis_action: ThesisAction = "not_written"
    try:
        db.add(trade)
        if db.get(Ticker, symbol) is None:
            db.add(Ticker(symbol=symbol))
            db.flush()
        if trade.action == TradeAction.BUY:
            thesis_action = sync_buy_trade_thesis(db, symbol, payload.thesis)
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise

    if trade.action == TradeAction.SELL:
        _persist_sell_side_effects(db, trade)
        if _close_thesis_if_position_exited(db, alpaca_client, symbol, trade.trade_id):
            thesis_action = "closed"

    return TradeExecutionResult(
        order=order,
        local_trade_id=local_trade_id,
        thesis_action=thesis_action,
    )


def _persist_sell_side_effects(db: Session, trade: Trade) -> None:
    try:
        record_realized_pnl_for_trade(db, trade)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to persist realized P&L rows for sell trade %s", trade.trade_id)


def _close_thesis_if_position_exited(
    db: Session,
    alpaca_client: Any,
    symbol: str,
    trade_id: str,
) -> bool:
    try:
        positions = alpaca_client.list_positions()
        open_symbols = {str(position["symbol"]).upper() for position in positions}
        if symbol in open_symbols:
            return False

        closed_record = close_thesis(db, symbol)
        return closed_record is not None
    except Exception:
        db.rollback()
        logger.exception("Failed to auto-close thesis for %s after sell trade %s", symbol, trade_id)
        return False

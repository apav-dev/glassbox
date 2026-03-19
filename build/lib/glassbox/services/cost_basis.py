from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from glassbox.db.models import RealizedPnl, Trade, TradeAction


logger = logging.getLogger(__name__)
ZERO = Decimal("0")


@dataclass
class CostBasisRecord:
    symbol: str
    total_qty: float
    avg_cost: float
    total_cost_basis: float
    realized_pnl: float
    lots: list[dict[str, float]]


@dataclass
class _Lot:
    qty: Decimal
    price: Decimal


def _to_decimal(value: Any) -> Decimal:
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _effective_price(price: Any, commission: Any, qty: Any, action: TradeAction) -> Decimal | None:
    if price is None:
        return None

    quantity = _to_decimal(qty)
    if quantity <= ZERO:
        return None

    base_price = _to_decimal(price)
    per_share_commission = _to_decimal(commission) / quantity
    if action == TradeAction.BUY:
        return base_price + per_share_commission
    return base_price - per_share_commission


def compute_cost_basis(trades: list[Trade]) -> dict[str, CostBasisRecord]:
    grouped_lots: dict[str, deque[_Lot]] = defaultdict(deque)
    realized_by_symbol: dict[str, Decimal] = defaultdict(lambda: ZERO)
    seen_symbols: set[str] = set()

    for trade in trades:
        symbol = trade.ticker.upper()
        seen_symbols.add(symbol)

        effective_price = _effective_price(trade.price, trade.commission, trade.qty, trade.action)
        quantity = _to_decimal(trade.qty)
        if effective_price is None or quantity <= ZERO:
            continue

        lots = grouped_lots[symbol]
        if trade.action == TradeAction.BUY:
            lots.append(_Lot(qty=quantity, price=effective_price))
            continue

        remaining_qty = quantity
        while remaining_qty > ZERO and lots:
            open_lot = lots[0]
            matched_qty = min(remaining_qty, open_lot.qty)
            realized_by_symbol[symbol] += (effective_price - open_lot.price) * matched_qty
            remaining_qty -= matched_qty
            open_lot.qty -= matched_qty
            if open_lot.qty <= ZERO:
                lots.popleft()

        if remaining_qty > ZERO:
            logger.warning(
                "Sell trade %s for %s exceeded available quantity by %s shares; skipped excess.",
                trade.trade_id,
                symbol,
                float(remaining_qty),
            )

    results: dict[str, CostBasisRecord] = {}
    for symbol in sorted(seen_symbols):
        lots = grouped_lots[symbol]
        total_qty = sum((lot.qty for lot in lots), start=ZERO)
        total_cost = sum((lot.qty * lot.price for lot in lots), start=ZERO)
        avg_cost = total_cost / total_qty if total_qty > ZERO else ZERO
        results[symbol] = CostBasisRecord(
            symbol=symbol,
            total_qty=float(total_qty),
            avg_cost=float(avg_cost),
            total_cost_basis=float(total_cost),
            realized_pnl=float(realized_by_symbol[symbol]),
            lots=[{"qty": float(lot.qty), "price": float(lot.price)} for lot in lots],
        )

    return results


def _match_sell_to_lots(
    symbol: str,
    sell_trade_id: str,
    sell_qty: Decimal,
    sell_price: Decimal | None,
    closed_at: datetime,
    lots: deque[_Lot],
) -> tuple[list[RealizedPnl], Decimal]:
    if sell_price is None or sell_qty <= ZERO:
        return [], ZERO

    remaining_qty = sell_qty
    rows: list[RealizedPnl] = []
    realized_total = ZERO

    while remaining_qty > ZERO and lots:
        open_lot = lots[0]
        matched_qty = min(remaining_qty, open_lot.qty)
        pnl = (sell_price - open_lot.price) * matched_qty
        rows.append(
            RealizedPnl(
                ticker=symbol,
                sell_trade_id=sell_trade_id,
                qty=float(matched_qty),
                buy_price=float(open_lot.price),
                sell_price=float(sell_price),
                pnl=float(pnl),
                closed_at=closed_at,
            )
        )
        realized_total += pnl
        remaining_qty -= matched_qty
        open_lot.qty -= matched_qty
        if open_lot.qty <= ZERO:
            lots.popleft()

    if remaining_qty > ZERO:
        logger.warning(
            "Sell trade %s for %s exceeded available quantity by %s shares; skipped excess.",
            sell_trade_id,
            symbol,
            float(remaining_qty),
        )

    return rows, realized_total


def rebuild_realized_pnl(db: Session) -> int:
    trades = db.scalars(select(Trade).order_by(Trade.timestamp.asc(), Trade.trade_id.asc())).all()
    grouped_lots: dict[str, deque[_Lot]] = defaultdict(deque)
    rows_to_insert: list[RealizedPnl] = []

    db.execute(delete(RealizedPnl))

    for trade in trades:
        symbol = trade.ticker.upper()
        quantity = _to_decimal(trade.qty)
        effective_price = _effective_price(trade.price, trade.commission, trade.qty, trade.action)
        if effective_price is None or quantity <= ZERO:
            continue

        lots = grouped_lots[symbol]
        if trade.action == TradeAction.BUY:
            lots.append(_Lot(qty=quantity, price=effective_price))
            continue

        matched_rows, _ = _match_sell_to_lots(
            symbol=symbol,
            sell_trade_id=trade.trade_id,
            sell_qty=quantity,
            sell_price=effective_price,
            closed_at=trade.timestamp,
            lots=lots,
        )
        rows_to_insert.extend(matched_rows)

    if rows_to_insert:
        db.add_all(rows_to_insert)
    db.commit()
    return len(rows_to_insert)


def record_realized_pnl_for_trade(db: Session, sell_trade: Trade) -> list[RealizedPnl]:
    if sell_trade.action != TradeAction.SELL:
        return []

    symbol = sell_trade.ticker.upper()
    trades = db.scalars(
        select(Trade)
        .where(Trade.ticker == symbol)
        .order_by(Trade.timestamp.asc(), Trade.trade_id.asc())
    ).all()

    lots: deque[_Lot] = deque()
    inserted_rows: list[RealizedPnl] = []
    sell_found = False

    db.execute(delete(RealizedPnl).where(RealizedPnl.sell_trade_id == sell_trade.trade_id))

    for trade in trades:
        quantity = _to_decimal(trade.qty)
        effective_price = _effective_price(trade.price, trade.commission, trade.qty, trade.action)
        if effective_price is None or quantity <= ZERO:
            if trade.trade_id == sell_trade.trade_id:
                sell_found = True
            continue

        if trade.action == TradeAction.BUY:
            lots.append(_Lot(qty=quantity, price=effective_price))
        else:
            matched_rows, _ = _match_sell_to_lots(
                symbol=symbol,
                sell_trade_id=trade.trade_id,
                sell_qty=quantity,
                sell_price=effective_price,
                closed_at=trade.timestamp,
                lots=lots,
            )
            if trade.trade_id == sell_trade.trade_id:
                inserted_rows = matched_rows
                sell_found = True
                break

        if trade.trade_id == sell_trade.trade_id:
            sell_found = True
            break

    if not sell_found:
        logger.warning("Sell trade %s was not found in local history for %s.", sell_trade.trade_id, symbol)
        db.rollback()
        return []

    if inserted_rows:
        db.add_all(inserted_rows)
        db.flush()

    return inserted_rows

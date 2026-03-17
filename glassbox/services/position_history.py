from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from glassbox.db.models import Trade, TradeAction


ZERO = Decimal("0")


def _to_decimal(value: Any) -> Decimal:
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _day_trade_effects(trade: Trade) -> tuple[str, Decimal, Decimal] | None:
    if trade.price is None:
        return None

    symbol = trade.ticker.upper()
    quantity = _to_decimal(trade.qty)
    price = _to_decimal(trade.price)
    commission = _to_decimal(trade.commission)
    gross_cash = quantity * price

    if trade.action == TradeAction.BUY:
        return symbol, quantity, -(gross_cash + commission)
    return symbol, -quantity, gross_cash - commission


def reconstruct_daily_positions(
    trades: list[Trade],
    start_date: date,
    end_date: date,
    starting_cash: float = 0.0,
) -> list[dict[str, Any]]:
    holdings: dict[str, Decimal] = defaultdict(lambda: ZERO)
    cash = _to_decimal(starting_cash)
    trade_index = 0
    snapshots: list[dict[str, Any]] = []
    current_date = start_date

    while current_date <= end_date:
        while trade_index < len(trades) and trades[trade_index].timestamp.date() <= current_date:
            trade = trades[trade_index]
            effects = _day_trade_effects(trade)
            if effects is not None:
                symbol, share_delta, cash_delta = effects
                holdings[symbol] += share_delta
                if holdings[symbol] == ZERO:
                    del holdings[symbol]
                cash += cash_delta
            trade_index += 1

        positions = {symbol: float(shares) for symbol, shares in sorted(holdings.items()) if shares != ZERO}
        snapshots.append(
            {
                "date": current_date,
                "positions": positions,
                "cash": float(cash),
                "total_tickers": len(positions),
            }
        )
        current_date += timedelta(days=1)

    return snapshots


def reconstruct_daily_positions_for_ticker(
    trades: list[Trade],
    symbol: str,
    start_date: date,
    end_date: date,
) -> list[dict[str, Any]]:
    normalized_symbol = symbol.upper()
    shares = ZERO
    trade_index = 0
    snapshots: list[dict[str, Any]] = []
    current_date = start_date

    while current_date <= end_date:
        while trade_index < len(trades) and trades[trade_index].timestamp.date() <= current_date:
            trade = trades[trade_index]
            if trade.ticker.upper() == normalized_symbol:
                effects = _day_trade_effects(trade)
                if effects is not None:
                    _, share_delta, _ = effects
                    shares += share_delta
            trade_index += 1

        snapshots.append({"date": current_date, "shares": float(shares)})
        current_date += timedelta(days=1)

    return snapshots


def get_position_at_date(trades: list[Trade], symbol: str, target_date: date) -> float:
    normalized_symbol = symbol.upper()
    shares = ZERO

    for trade in trades:
        if trade.timestamp.date() > target_date:
            break
        if trade.ticker.upper() != normalized_symbol:
            continue

        effects = _day_trade_effects(trade)
        if effects is None:
            continue

        _, share_delta, _ = effects
        shares += share_delta

    return float(shares)

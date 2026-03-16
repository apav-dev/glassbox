from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from alpaca_client import AlpacaClient
from models import DailyPrice, Ticker


def fetch_daily_prices(symbol: str, start_date: date, end_date: date) -> list[dict[str, Any]]:
    client = AlpacaClient()
    bars = client.get_daily_bars(symbol=symbol, start_date=start_date, end_date=end_date)

    prices: list[dict[str, Any]] = []
    for bar in bars:
        close_value = getattr(bar, "c", None)
        if close_value is None:
            close_value = getattr(bar, "close")
        prices.append(
            {
                "symbol": symbol.upper(),
                "date": _bar_date(bar),
                "close": float(close_value),
                "volume": _optional_float(getattr(bar, "v", getattr(bar, "volume", None))),
            }
        )

    return prices


def backfill_ticker_prices(db: Session, symbol: str, lookback_days: int = 365) -> int:
    normalized_symbol = symbol.upper()
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    if yesterday <= date.min:
        return 0

    latest_price_date = db.scalar(
        select(DailyPrice.date)
        .where(DailyPrice.symbol == normalized_symbol)
        .order_by(DailyPrice.date.desc())
        .limit(1)
    )

    start_date = latest_price_date + timedelta(days=1) if latest_price_date else yesterday - timedelta(days=lookback_days)
    if start_date > yesterday:
        return 0

    prices = fetch_daily_prices(normalized_symbol, start_date, yesterday)
    if not prices:
        return 0

    for price in prices:
        existing = db.get(DailyPrice, (normalized_symbol, price["date"]))
        if existing is None:
            db.add(
                DailyPrice(
                    symbol=normalized_symbol,
                    date=price["date"],
                    close=price["close"],
                    volume=price.get("volume"),
                )
            )
            continue

        existing.close = price["close"]
        existing.volume = price.get("volume")

    db.commit()
    return len(prices)


def backfill_all_tickers(db: Session, lookback_days: int = 365) -> dict[str, int]:
    symbols = {symbol.upper() for symbol in db.scalars(select(Ticker.symbol)).all()}
    symbols.add("SPY")

    results: dict[str, int] = {}
    for symbol in sorted(symbols):
        results[symbol] = backfill_ticker_prices(db, symbol, lookback_days=lookback_days)

    return results


def replace_symbol_prices(db: Session, symbol: str, prices: list[dict[str, Any]]) -> None:
    normalized_symbol = symbol.upper()
    if not prices:
        return

    price_dates = [price["date"] for price in prices]
    db.execute(
        delete(DailyPrice).where(
            DailyPrice.symbol == normalized_symbol,
            DailyPrice.date.in_(price_dates),
        )
    )
    db.add_all(
        [
            DailyPrice(
                symbol=normalized_symbol,
                date=price["date"],
                close=price["close"],
                volume=price.get("volume"),
            )
            for price in prices
        ]
    )
    db.commit()


def _bar_date(bar: Any) -> date:
    raw_timestamp = getattr(bar, "t", None)
    if raw_timestamp is None:
        raw_timestamp = getattr(bar, "timestamp", None)
    if isinstance(raw_timestamp, datetime):
        return raw_timestamp.date()
    if hasattr(raw_timestamp, "to_pydatetime"):
        return raw_timestamp.to_pydatetime().date()
    raise ValueError("Unable to extract date from Alpaca bar")


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)

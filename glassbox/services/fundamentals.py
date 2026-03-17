from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from time import sleep

from sqlalchemy import select
from sqlalchemy.orm import Session

from glassbox.providers.fundamentals import get_fundamentals_provider
from glassbox.db.models import Ticker, TickerFundamentals


logger = logging.getLogger(__name__)

_TICKER_BACKFILL_FIELDS = ("company_name", "sector", "industry", "country", "beta")
_FUNDAMENTALS_FIELDS = (
    "market_cap",
    "trailing_pe",
    "forward_pe",
    "price_to_book",
    "ev_to_ebitda",
    "ev_to_revenue",
    "peg_ratio",
    "price_to_sales",
    "profit_margin",
    "gross_margin",
    "operating_margin",
    "ebitda_margin",
    "return_on_equity",
    "return_on_assets",
    "target_high_price",
    "target_low_price",
    "target_mean_price",
    "target_median_price",
    "analyst_count",
    "recommendation_key",
    "recommendation_mean",
)


def refresh_ticker_fundamentals(db: Session, symbol: str) -> bool:
    normalized_symbol = symbol.upper()
    fundamentals = get_fundamentals_provider().fetch_fundamentals(normalized_symbol)
    if fundamentals is None:
        return False

    ticker = db.get(Ticker, normalized_symbol)
    if ticker is None:
        ticker = Ticker(symbol=normalized_symbol)
        db.add(ticker)

    record = db.get(TickerFundamentals, normalized_symbol)
    if record is None:
        record = TickerFundamentals(symbol=normalized_symbol)
        db.add(record)

    record.updated_at = _utc_now()
    for field in _FUNDAMENTALS_FIELDS:
        setattr(record, field, fundamentals.get(field))

    for field in _TICKER_BACKFILL_FIELDS:
        current_value = getattr(ticker, field)
        incoming_value = fundamentals.get(field)
        if current_value is None and incoming_value is not None:
            setattr(ticker, field, incoming_value)

    db.commit()
    return True


def refresh_all_fundamentals(db: Session) -> dict[str, bool]:
    symbols = sorted({symbol.upper() for symbol in db.scalars(select(Ticker.symbol)).all()})

    results: dict[str, bool] = {}
    total = len(symbols)
    for index, symbol in enumerate(symbols, start=1):
        logger.info("Refreshing fundamentals for %s (%s/%s)...", symbol, index, total)
        try:
            results[symbol] = refresh_ticker_fundamentals(db, symbol)
        except Exception:
            db.rollback()
            logger.exception("Failed to refresh fundamentals for %s", symbol)
            results[symbol] = False
        if index < total:
            sleep(0.5)

    return results


def get_stale_symbols(db: Session, max_age_hours: int = 24) -> list[str]:
    cutoff = _utc_now() - timedelta(hours=max_age_hours)
    symbols = sorted({symbol.upper() for symbol in db.scalars(select(Ticker.symbol)).all()})

    stale_symbols: list[str] = []
    for symbol in symbols:
        record = db.get(TickerFundamentals, symbol)
        updated_at = _normalize_timestamp(record.updated_at) if record is not None else None
        if record is None or updated_at is None or updated_at < cutoff:
            stale_symbols.append(symbol)

    return stale_symbols


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_timestamp(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

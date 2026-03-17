from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from glassbox.db.models import DailyPrice, SessionLocal, Ticker, TickerFundamentals, TickerTag, Trade
from glassbox.schemas.fundamentals import TickerFundamentalsResponse
from glassbox.services.fundamentals import refresh_ticker_fundamentals
from glassbox.services.price import backfill_ticker_prices


logger = logging.getLogger(__name__)


def load_ticker_map(db: Session, symbols: list[str]) -> dict[str, Ticker]:
    if not symbols:
        return {}

    ticker_rows = db.scalars(select(Ticker).where(Ticker.symbol.in_(symbols))).all()
    return {ticker.symbol.upper(): ticker for ticker in ticker_rows}


def load_fundamentals_map(db: Session, symbols: list[str]) -> dict[str, TickerFundamentals]:
    if not symbols:
        return {}

    rows = db.scalars(select(TickerFundamentals).where(TickerFundamentals.symbol.in_(symbols))).all()
    return {row.symbol.upper(): row for row in rows}


def enrich_positions(
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
                "trailing_pe": optional_float(
                    fundamentals_record.trailing_pe if fundamentals_record else None
                ),
                "forward_pe": optional_float(
                    fundamentals_record.forward_pe if fundamentals_record else None
                ),
                "price_to_book": optional_float(
                    fundamentals_record.price_to_book if fundamentals_record else None
                ),
                "target_mean_price": optional_float(
                    fundamentals_record.target_mean_price if fundamentals_record else None
                ),
                "recommendation_key": fundamentals_record.recommendation_key if fundamentals_record else None,
            }
        )

    return enriched_positions


def load_price_history(
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


def load_ticker_tag_map(db: Session, symbols: list[str]) -> dict[str, list[str]]:
    if not symbols:
        return {}

    rows = db.scalars(
        select(TickerTag)
        .where(TickerTag.symbol.in_([symbol.upper() for symbol in symbols]))
        .order_by(TickerTag.symbol.asc(), TickerTag.tag_name.asc())
    ).all()
    tag_map: dict[str, list[str]] = {}
    for row in rows:
        tag_map.setdefault(row.symbol.upper(), []).append(row.tag_name)
    return tag_map


def validate_history_range(start_date: date, end_date: date) -> None:
    if end_date < start_date:
        raise HTTPException(status_code=400, detail="end_date must be on or after start_date")
    if (end_date - start_date).days > 365:
        raise HTTPException(status_code=400, detail="Date range exceeds 365 days; request a narrower range")


def load_trades_for_date_range(
    db: Session,
    start_date: date,
    end_date: date,
    symbol: str | None = None,
) -> list[Trade]:
    end_exclusive = datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    statement = (
        select(Trade)
        .where(Trade.timestamp < end_exclusive)
        .order_by(Trade.timestamp.asc(), Trade.trade_id.asc())
    )
    if symbol:
        statement = statement.where(Trade.ticker == symbol.upper())
    return db.scalars(statement).all()


def backfill_symbol_prices_in_background(symbol: str) -> None:
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


def refresh_symbol_fundamentals_in_background(symbol: str) -> None:
    db = SessionLocal()
    try:
        if db.get(TickerFundamentals, symbol.upper()) is None:
            refresh_ticker_fundamentals(db, symbol)
    except Exception:
        logger.exception("Failed to refresh fundamentals for %s after trade execution", symbol.upper())
        db.rollback()
    finally:
        db.close()


def optional_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def fundamentals_response_from_record(
    symbol: str,
    record: TickerFundamentals | None,
) -> TickerFundamentalsResponse:
    if record is None:
        return TickerFundamentalsResponse(symbol=symbol.upper(), updated_at=None)

    return TickerFundamentalsResponse(
        symbol=record.symbol,
        updated_at=record.updated_at,
        market_cap=optional_float(record.market_cap),
        trailing_pe=optional_float(record.trailing_pe),
        forward_pe=optional_float(record.forward_pe),
        price_to_book=optional_float(record.price_to_book),
        ev_to_ebitda=optional_float(record.ev_to_ebitda),
        ev_to_revenue=optional_float(record.ev_to_revenue),
        peg_ratio=optional_float(record.peg_ratio),
        price_to_sales=optional_float(record.price_to_sales),
        profit_margin=optional_float(record.profit_margin),
        gross_margin=optional_float(record.gross_margin),
        operating_margin=optional_float(record.operating_margin),
        ebitda_margin=optional_float(record.ebitda_margin),
        return_on_equity=optional_float(record.return_on_equity),
        return_on_assets=optional_float(record.return_on_assets),
        target_high_price=optional_float(record.target_high_price),
        target_low_price=optional_float(record.target_low_price),
        target_mean_price=optional_float(record.target_mean_price),
        target_median_price=optional_float(record.target_median_price),
        analyst_count=record.analyst_count,
        recommendation_key=record.recommendation_key,
        recommendation_mean=optional_float(record.recommendation_mean),
    )


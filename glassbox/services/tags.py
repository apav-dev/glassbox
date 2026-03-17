from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from glassbox.db.models import Tag, Ticker, TickerTag


def _normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def _normalize_tag_name(tag_name: str) -> str:
    words = tag_name.strip().split()
    normalized_words: list[str] = []
    for word in words:
        if word.isupper() and len(word) <= 5:
            normalized_words.append(word)
        else:
            normalized_words.append(word.capitalize())
    return " ".join(normalized_words)


def create_tag(
    db: Session,
    name: str,
    color: str | None = None,
    description: str | None = None,
) -> Tag:
    normalized_name = _normalize_tag_name(name)
    existing = db.get(Tag, normalized_name)
    if existing is not None:
        return existing

    row = Tag(name=normalized_name, color=color, description=description)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_tags(db: Session) -> list[Tag]:
    return db.scalars(select(Tag).order_by(Tag.name.asc())).all()


def tag_ticker(db: Session, symbol: str, tag_name: str, tagged_by: str = "agent") -> TickerTag:
    normalized_symbol = _normalize_symbol(symbol)
    normalized_tag_name = _normalize_tag_name(tag_name)

    if db.get(Ticker, normalized_symbol) is None:
        db.add(Ticker(symbol=normalized_symbol))
        db.flush()

    create_tag(db, normalized_tag_name)
    existing = db.get(TickerTag, (normalized_symbol, normalized_tag_name))
    if existing is not None:
        return existing

    row = TickerTag(symbol=normalized_symbol, tag_name=normalized_tag_name, tagged_by=tagged_by)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def untag_ticker(db: Session, symbol: str, tag_name: str) -> bool:
    normalized_symbol = _normalize_symbol(symbol)
    normalized_tag_name = _normalize_tag_name(tag_name)
    existing = db.get(TickerTag, (normalized_symbol, normalized_tag_name))
    if existing is None:
        return False

    db.delete(existing)
    db.commit()
    return True


def get_ticker_tags(db: Session, symbol: str) -> list[Tag]:
    normalized_symbol = _normalize_symbol(symbol)
    return db.scalars(
        select(Tag)
        .join(TickerTag, TickerTag.tag_name == Tag.name)
        .where(TickerTag.symbol == normalized_symbol)
        .order_by(Tag.name.asc())
    ).all()


def get_tag_tickers(db: Session, tag_name: str) -> list[str]:
    normalized_tag_name = _normalize_tag_name(tag_name)
    return list(
        db.scalars(
            select(TickerTag.symbol)
            .where(TickerTag.tag_name == normalized_tag_name)
            .order_by(TickerTag.symbol.asc())
        ).all()
    )


def bulk_tag_ticker(
    db: Session,
    symbol: str,
    tag_names: list[str],
    tagged_by: str = "agent",
) -> list[TickerTag]:
    tagged_rows: list[TickerTag] = []
    for tag_name in tag_names:
        tagged_rows.append(tag_ticker(db, symbol=symbol, tag_name=tag_name, tagged_by=tagged_by))
    return tagged_rows

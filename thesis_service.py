from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import InvestmentThesis, Ticker


VALID_THESIS_STATUSES = {"active", "closed", "watching"}


def _normalize_symbol(symbol: str) -> str:
    return symbol.upper()


def _validate_status(status: str) -> str:
    normalized_status = status.lower()
    if normalized_status not in VALID_THESIS_STATUSES:
        raise ValueError(f"Invalid thesis status: {status}")
    return normalized_status


def create_thesis(
    db: Session,
    symbol: str,
    thesis: str,
    catalyst: str | None,
    edge: str | None,
    status: str = "active",
    created_by: str = "agent",
) -> InvestmentThesis:
    normalized_symbol = _normalize_symbol(symbol)
    normalized_status = _validate_status(status)

    if db.get(Ticker, normalized_symbol) is None:
        db.add(Ticker(symbol=normalized_symbol))
        db.flush()

    current_max_version = db.scalar(
        select(func.max(InvestmentThesis.version)).where(InvestmentThesis.symbol == normalized_symbol)
    )
    next_version = int(current_max_version or 0) + 1

    row = InvestmentThesis(
        symbol=normalized_symbol,
        version=next_version,
        thesis=thesis,
        catalyst=catalyst,
        edge=edge,
        status=normalized_status,
        created_by=created_by,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_thesis_status(db: Session, symbol: str, status: str) -> InvestmentThesis | None:
    current = get_current_thesis(db, symbol)
    if current is None:
        return None

    current.status = _validate_status(status)
    db.commit()
    db.refresh(current)
    return current


def get_current_thesis(db: Session, symbol: str) -> InvestmentThesis | None:
    normalized_symbol = _normalize_symbol(symbol)
    return db.scalar(
        select(InvestmentThesis)
        .where(InvestmentThesis.symbol == normalized_symbol)
        .order_by(InvestmentThesis.version.desc())
        .limit(1)
    )


def get_current_theses(
    db: Session,
    symbols: list[str] | None = None,
    status: str | None = None,
) -> list[InvestmentThesis]:
    normalized_symbols = [_normalize_symbol(symbol) for symbol in symbols] if symbols else None
    normalized_status = _validate_status(status) if status is not None else None

    latest_version_subquery = (
        select(
            InvestmentThesis.symbol.label("symbol"),
            func.max(InvestmentThesis.version).label("max_version"),
        )
        .group_by(InvestmentThesis.symbol)
        .subquery()
    )

    statement = (
        select(InvestmentThesis)
        .join(
            latest_version_subquery,
            (InvestmentThesis.symbol == latest_version_subquery.c.symbol)
            & (InvestmentThesis.version == latest_version_subquery.c.max_version),
        )
        .order_by(InvestmentThesis.symbol.asc())
    )

    if normalized_symbols:
        statement = statement.where(InvestmentThesis.symbol.in_(normalized_symbols))
    if normalized_status is not None:
        statement = statement.where(InvestmentThesis.status == normalized_status)

    return db.scalars(statement).all()


def get_thesis_history(db: Session, symbol: str) -> list[InvestmentThesis]:
    normalized_symbol = _normalize_symbol(symbol)
    return db.scalars(
        select(InvestmentThesis)
        .where(InvestmentThesis.symbol == normalized_symbol)
        .order_by(InvestmentThesis.version.asc())
    ).all()


def close_thesis(db: Session, symbol: str) -> InvestmentThesis | None:
    return update_thesis_status(db, symbol, "closed")

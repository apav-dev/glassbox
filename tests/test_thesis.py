from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from glassbox.db.models import Base, Ticker
from glassbox.services.theses import (
    close_thesis,
    create_thesis,
    get_current_theses,
    get_current_thesis,
    get_thesis_history,
    update_thesis_status,
)


@pytest.fixture
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


def test_create_first_thesis(db_session: Session) -> None:
    record = create_thesis(
        db_session,
        symbol="AAPL",
        thesis="Services mix expansion supports multiple re-rating.",
        catalyst="WWDC monetization updates.",
        edge="Street underestimates recurring revenue durability.",
    )

    assert record.symbol == "AAPL"
    assert record.version == 1
    assert record.status == "active"
    assert record.thesis == "Services mix expansion supports multiple re-rating."
    assert record.catalyst == "WWDC monetization updates."
    assert record.edge == "Street underestimates recurring revenue durability."
    assert record.created_by == "agent"
    assert record.created_at is not None


def test_create_second_version(db_session: Session) -> None:
    create_thesis(db_session, "AAPL", "Initial thesis.", None, None)
    second = create_thesis(db_session, "AAPL", "Updated thesis.", "Product cycle", "Estimate reset")

    current = get_current_thesis(db_session, "AAPL")

    assert second.version == 2
    assert current is not None
    assert current.version == 2
    assert current.thesis == "Updated thesis."


def test_version_isolation_across_tickers(db_session: Session) -> None:
    aapl = create_thesis(db_session, "AAPL", "Apple thesis.", None, None)
    msft = create_thesis(db_session, "MSFT", "Microsoft thesis.", None, None)

    assert aapl.version == 1
    assert msft.version == 1


def test_get_current_theses_returns_only_latest_versions(db_session: Session) -> None:
    create_thesis(db_session, "AAPL", "AAPL v1", None, None)
    create_thesis(db_session, "AAPL", "AAPL v2", None, None)
    create_thesis(db_session, "MSFT", "MSFT v1", None, None)

    records = get_current_theses(db_session)

    assert [(record.symbol, record.version) for record in records] == [("AAPL", 2), ("MSFT", 1)]


def test_status_filter(db_session: Session) -> None:
    create_thesis(db_session, "AAPL", "Active thesis", None, None, status="active")
    create_thesis(db_session, "MSFT", "Watching thesis", None, None, status="watching")
    create_thesis(db_session, "GOOGL", "Closed thesis", None, None, status="closed")

    records = get_current_theses(db_session, status="active")

    assert [record.symbol for record in records] == ["AAPL"]


def test_update_thesis_status(db_session: Session) -> None:
    create_thesis(db_session, "AAPL", "Active thesis", None, None, status="active")

    updated = update_thesis_status(db_session, "AAPL", "closed")

    assert updated is not None
    assert updated.status == "closed"


def test_get_thesis_history(db_session: Session) -> None:
    create_thesis(db_session, "AAPL", "v1", None, None)
    create_thesis(db_session, "AAPL", "v2", None, None)
    create_thesis(db_session, "AAPL", "v3", None, None)

    history = get_thesis_history(db_session, "AAPL")

    assert [record.version for record in history] == [1, 2, 3]
    assert [record.thesis for record in history] == ["v1", "v2", "v3"]


def test_close_thesis(db_session: Session) -> None:
    create_thesis(db_session, "AAPL", "Open thesis", None, None, status="active")

    closed = close_thesis(db_session, "AAPL")

    assert closed is not None
    assert closed.status == "closed"


def test_close_thesis_returns_none_for_missing_symbol(db_session: Session) -> None:
    assert close_thesis(db_session, "ZZZZ") is None


def test_create_thesis_auto_creates_ticker_row(db_session: Session) -> None:
    assert db_session.get(Ticker, "NVDA") is None

    create_thesis(db_session, "NVDA", "GPU demand remains supply constrained.", None, None)

    assert db_session.get(Ticker, "NVDA") is not None

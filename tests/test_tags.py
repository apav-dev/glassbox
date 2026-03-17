from __future__ import annotations

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from glassbox.db.models import Base, Tag, Ticker, TickerTag
from glassbox.services.risk import compute_thematic_exposure
from glassbox.services.tags import (
    bulk_tag_ticker,
    create_tag,
    get_tag_tickers,
    get_ticker_tags,
    tag_ticker,
    untag_ticker,
)


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()


def test_create_tag() -> None:
    session = _make_session()
    try:
        tag = create_tag(session, "AI", color="#4A90D9", description="Artificial intelligence exposure")

        assert tag.name == "AI"
        assert tag.color == "#4A90D9"
        assert tag.description == "Artificial intelligence exposure"
    finally:
        session.close()


def test_create_tag_is_idempotent() -> None:
    session = _make_session()
    try:
        first = create_tag(session, "Cloud")
        second = create_tag(session, " cloud ")

        count = session.scalar(select(func.count()).select_from(TickerTag))
        tag_rows = session.scalars(select(Tag)).all()

        assert first.name == "Cloud"
        assert second.name == "Cloud"
        assert len(tag_rows) == 1
        assert count == 0
    finally:
        session.close()


def test_tag_a_ticker() -> None:
    session = _make_session()
    try:
        row = tag_ticker(session, "AAPL", "AI")

        assert row.symbol == "AAPL"
        assert row.tag_name == "AI"
        assert session.get(TickerTag, ("AAPL", "AI")) is not None
    finally:
        session.close()


def test_tag_a_ticker_is_idempotent() -> None:
    session = _make_session()
    try:
        first = tag_ticker(session, "AAPL", "AI")
        second = tag_ticker(session, "AAPL", "AI")

        rows = session.scalars(select(TickerTag)).all()

        assert first.symbol == second.symbol
        assert first.tag_name == second.tag_name
        assert len(rows) == 1
    finally:
        session.close()


def test_tag_auto_creates_ticker() -> None:
    session = _make_session()
    try:
        assert session.get(Ticker, "NVDA") is None

        tag_ticker(session, "NVDA", "AI")

        assert session.get(Ticker, "NVDA") is not None
    finally:
        session.close()


def test_untag_a_ticker() -> None:
    session = _make_session()
    try:
        tag_ticker(session, "AAPL", "AI")

        removed = untag_ticker(session, "AAPL", "AI")

        assert removed is True
        assert session.get(TickerTag, ("AAPL", "AI")) is None
    finally:
        session.close()


def test_untag_returns_false_for_non_existent_pairing() -> None:
    session = _make_session()
    try:
        assert untag_ticker(session, "AAPL", "AI") is False
    finally:
        session.close()


def test_get_ticker_tags() -> None:
    session = _make_session()
    try:
        tag_ticker(session, "AAPL", "AI")
        tag_ticker(session, "AAPL", "Cloud")

        tags = get_ticker_tags(session, "AAPL")

        assert [tag.name for tag in tags] == ["AI", "Cloud"]
    finally:
        session.close()


def test_get_tag_tickers() -> None:
    session = _make_session()
    try:
        tag_ticker(session, "MSFT", "Cloud")
        tag_ticker(session, "AAPL", "Cloud")

        symbols = get_tag_tickers(session, "Cloud")

        assert symbols == ["AAPL", "MSFT"]
    finally:
        session.close()


def test_bulk_tag() -> None:
    session = _make_session()
    try:
        rows = bulk_tag_ticker(session, "AAPL", ["AI", "Cloud", "Consumer Hardware"])

        persisted = session.scalars(
            select(TickerTag).where(TickerTag.symbol == "AAPL").order_by(TickerTag.tag_name.asc())
        ).all()

        assert len(rows) == 3
        assert [row.tag_name for row in persisted] == ["AI", "Cloud", "Consumer Hardware"]
    finally:
        session.close()


def test_thematic_exposure_computation() -> None:
    positions = [
        {"symbol": "AAPL", "market_value": 25000.0},
        {"symbol": "MSFT", "market_value": 40000.0},
        {"symbol": "AMZN", "market_value": 15000.0},
    ]
    ticker_tag_map = {
        "AAPL": ["AI", "Consumer Hardware"],
        "MSFT": ["AI", "Cloud"],
        "AMZN": ["Cloud"],
    }

    buckets = compute_thematic_exposure(positions, ticker_tag_map, nav=100000.0)

    assert [bucket["tag"] for bucket in buckets] == ["AI", "Cloud", "Consumer Hardware"]
    assert buckets[0]["total_market_value"] == 65000.0
    assert buckets[0]["weight"] == 0.65
    assert buckets[0]["position_count"] == 2
    assert buckets[0]["symbols"] == ["AAPL", "MSFT"]
    assert buckets[1]["total_market_value"] == 55000.0
    assert buckets[1]["weight"] == 0.55
    assert buckets[1]["symbols"] == ["AMZN", "MSFT"]

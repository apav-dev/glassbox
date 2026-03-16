from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import fundamentals_provider
import fundamentals_service
from fundamentals_provider import YFinanceFundamentalsProvider
from models import Base, Ticker, TickerFundamentals


@pytest.fixture
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


def test_yfinance_provider_fetch_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeTicker:
        def __init__(self, symbol: str) -> None:
            self.info = {
                "shortName": "Apple Inc.",
                "sector": "Technology",
                "industry": "Consumer Electronics",
                "country": "United States",
                "beta": 1.24,
                "marketCap": 3000000000000,
                "trailingPE": 28.1,
                "forwardPE": 24.6,
                "priceToBook": 42.0,
                "enterpriseToEbitda": 21.3,
                "enterpriseToRevenue": 7.8,
                "pegRatio": 2.1,
                "priceToSalesTrailing12Months": 7.2,
                "profitMargins": 0.25,
                "grossMargins": 0.44,
                "operatingMargins": 0.31,
                "ebitdaMargins": 0.34,
                "returnOnEquity": 1.45,
                "returnOnAssets": 0.22,
                "targetHighPrice": 260,
                "targetLowPrice": 180,
                "targetMeanPrice": 220,
                "targetMedianPrice": 225,
                "numberOfAnalystOpinions": 38,
                "recommendationKey": "buy",
                "recommendationMean": 1.9,
            }

    monkeypatch.setattr(fundamentals_provider, "yf", type("FakeYF", (), {"Ticker": FakeTicker}))

    result = YFinanceFundamentalsProvider().fetch_fundamentals("AAPL")

    assert result is not None
    assert result["company_name"] == "Apple Inc."
    assert result["sector"] == "Technology"
    assert result["market_cap"] == pytest.approx(3000000000000.0)
    assert result["trailing_pe"] == pytest.approx(28.1)
    assert result["analyst_count"] == 38
    assert result["recommendation_key"] == "buy"
    assert result["recommendation_mean"] == pytest.approx(1.9)


def test_yfinance_provider_missing_keys_become_none(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeTicker:
        def __init__(self, symbol: str) -> None:
            self.info = {
                "shortName": "Microsoft Corporation",
                "sector": "Technology",
                "trailingPE": 31.4,
            }

    monkeypatch.setattr(fundamentals_provider, "yf", type("FakeYF", (), {"Ticker": FakeTicker}))

    result = YFinanceFundamentalsProvider().fetch_fundamentals("MSFT")

    assert result is not None
    assert result["company_name"] == "Microsoft Corporation"
    assert result["sector"] == "Technology"
    assert result["trailing_pe"] == pytest.approx(31.4)
    assert result["target_mean_price"] is None
    assert result["analyst_count"] is None
    assert result["recommendation_key"] is None


def test_yfinance_provider_returns_none_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeTicker:
        def __init__(self, symbol: str) -> None:
            raise RuntimeError("provider blew up")

    monkeypatch.setattr(fundamentals_provider, "yf", type("FakeYF", (), {"Ticker": FakeTicker}))

    result = YFinanceFundamentalsProvider().fetch_fundamentals("BAD")

    assert result is None


def test_yfinance_provider_bad_numeric_values_become_none(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeTicker:
        def __init__(self, symbol: str) -> None:
            self.info = {
                "shortName": "Example Co.",
                "trailingPE": "N/A",
                "forwardPE": True,
                "numberOfAnalystOpinions": "unknown",
                "recommendationKey": "hold",
            }

    monkeypatch.setattr(fundamentals_provider, "yf", type("FakeYF", (), {"Ticker": FakeTicker}))

    result = YFinanceFundamentalsProvider().fetch_fundamentals("EXMPL")

    assert result is not None
    assert result["trailing_pe"] is None
    assert result["forward_pe"] is None
    assert result["analyst_count"] is None
    assert result["recommendation_key"] == "hold"


def test_refresh_ticker_fundamentals_inserts_new_row(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_session.add(Ticker(symbol="AAPL"))
    db_session.commit()

    monkeypatch.setattr(
        fundamentals_service,
        "get_fundamentals_provider",
        lambda: _FakeProvider(
            {
                "company_name": "Apple Inc.",
                "sector": "Technology",
                "industry": "Consumer Electronics",
                "country": "United States",
                "beta": 1.24,
                "market_cap": 3000000000000.0,
                "trailing_pe": 28.1,
                "forward_pe": 24.6,
                "price_to_book": 42.0,
                "ev_to_ebitda": 21.3,
                "ev_to_revenue": 7.8,
                "peg_ratio": 2.1,
                "price_to_sales": 7.2,
                "profit_margin": 0.25,
                "gross_margin": 0.44,
                "operating_margin": 0.31,
                "ebitda_margin": 0.34,
                "return_on_equity": 1.45,
                "return_on_assets": 0.22,
                "target_high_price": 260.0,
                "target_low_price": 180.0,
                "target_mean_price": 220.0,
                "target_median_price": 225.0,
                "analyst_count": 38,
                "recommendation_key": "buy",
                "recommendation_mean": 1.9,
            }
        ),
    )

    refreshed = fundamentals_service.refresh_ticker_fundamentals(db_session, "AAPL")
    record = db_session.get(TickerFundamentals, "AAPL")

    assert refreshed is True
    assert record is not None
    assert float(record.market_cap) == pytest.approx(3000000000000.0)
    assert record.trailing_pe == pytest.approx(28.1)
    assert record.analyst_count == 38
    assert record.recommendation_key == "buy"
    assert record.updated_at is not None


def test_refresh_ticker_fundamentals_updates_existing_row(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_updated_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    db_session.add(Ticker(symbol="MSFT"))
    db_session.add(
        TickerFundamentals(
            symbol="MSFT",
            updated_at=old_updated_at,
            trailing_pe=30.0,
            recommendation_key="hold",
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        fundamentals_service,
        "get_fundamentals_provider",
        lambda: _FakeProvider(
            {
                "company_name": None,
                "sector": None,
                "industry": None,
                "country": None,
                "beta": None,
                "market_cap": 2500000000000.0,
                "trailing_pe": 33.0,
                "forward_pe": 29.0,
                "price_to_book": 11.0,
                "ev_to_ebitda": 18.0,
                "ev_to_revenue": 12.0,
                "peg_ratio": 2.0,
                "price_to_sales": 13.0,
                "profit_margin": 0.36,
                "gross_margin": 0.69,
                "operating_margin": 0.45,
                "ebitda_margin": 0.5,
                "return_on_equity": 0.33,
                "return_on_assets": 0.16,
                "target_high_price": 520.0,
                "target_low_price": 410.0,
                "target_mean_price": 465.0,
                "target_median_price": 470.0,
                "analyst_count": 45,
                "recommendation_key": "buy",
                "recommendation_mean": 2.0,
            }
        ),
    )

    refreshed = fundamentals_service.refresh_ticker_fundamentals(db_session, "MSFT")
    record = db_session.get(TickerFundamentals, "MSFT")

    assert refreshed is True
    assert record is not None
    assert record.trailing_pe == pytest.approx(33.0)
    assert record.recommendation_key == "buy"
    assert record.updated_at is not None
    assert _as_utc(record.updated_at) > old_updated_at


def test_refresh_ticker_fundamentals_backfills_ticker_metadata(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_session.add(Ticker(symbol="NVDA", sector=None))
    db_session.commit()

    monkeypatch.setattr(
        fundamentals_service,
        "get_fundamentals_provider",
        lambda: _FakeProvider(
            {
                "company_name": None,
                "sector": "Technology",
                "industry": None,
                "country": None,
                "beta": None,
            }
        ),
    )

    fundamentals_service.refresh_ticker_fundamentals(db_session, "NVDA")
    ticker = db_session.get(Ticker, "NVDA")

    assert ticker is not None
    assert ticker.sector == "Technology"


def test_refresh_ticker_fundamentals_does_not_overwrite_existing_ticker_metadata(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_session.add(Ticker(symbol="JPM", sector="Financials"))
    db_session.commit()

    monkeypatch.setattr(
        fundamentals_service,
        "get_fundamentals_provider",
        lambda: _FakeProvider(
            {
                "company_name": None,
                "sector": "Technology",
                "industry": None,
                "country": None,
                "beta": None,
            }
        ),
    )

    fundamentals_service.refresh_ticker_fundamentals(db_session, "JPM")
    ticker = db_session.get(Ticker, "JPM")

    assert ticker is not None
    assert ticker.sector == "Financials"


def test_refresh_ticker_fundamentals_returns_false_on_provider_failure(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_session.add(Ticker(symbol="FAIL"))
    db_session.commit()

    monkeypatch.setattr(
        fundamentals_service,
        "get_fundamentals_provider",
        lambda: _FakeProvider(None),
    )

    refreshed = fundamentals_service.refresh_ticker_fundamentals(db_session, "FAIL")
    record = db_session.get(TickerFundamentals, "FAIL")

    assert refreshed is False
    assert record is None


def test_get_stale_symbols(db_session: Session) -> None:
    fresh_time = datetime.now(timezone.utc) - timedelta(hours=2)
    stale_time = datetime.now(timezone.utc) - timedelta(hours=30)

    db_session.add_all(
        [
            Ticker(symbol="AAPL"),
            Ticker(symbol="MSFT"),
            Ticker(symbol="TSLA"),
        ]
    )
    db_session.add_all(
        [
            TickerFundamentals(symbol="AAPL", updated_at=fresh_time),
            TickerFundamentals(symbol="MSFT", updated_at=stale_time),
        ]
    )
    db_session.commit()

    stale_symbols = fundamentals_service.get_stale_symbols(db_session, max_age_hours=24)

    assert stale_symbols == ["MSFT", "TSLA"]


class _FakeProvider:
    def __init__(self, payload: dict[str, object] | None) -> None:
        self.payload = payload

    def fetch_fundamentals(self, symbol: str) -> dict[str, object] | None:
        return self.payload


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

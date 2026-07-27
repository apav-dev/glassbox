from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import glassbox.services.price as price_service
from glassbox.db.models import Base, DailyPrice, Ticker
from glassbox.services.risk import compute_correlation_matrix, compute_portfolio_beta, compute_stress_scenarios


@dataclass
class Snapshot:
    date: date
    equity_value: float
    cash_balance: float = 0.0
    long_market_value: float = 0.0


@dataclass
class PricePoint:
    symbol: str
    date: date
    close: float
    volume: float | None = None


def build_snapshots(daily_returns: list[float], start_nav: float = 100.0) -> list[Snapshot]:
    snapshots = [Snapshot(date=date(2024, 1, 1), equity_value=start_nav, long_market_value=start_nav)]
    nav = start_nav

    for offset, daily_return in enumerate(daily_returns, start=1):
        nav *= 1 + daily_return
        snapshots.append(
            Snapshot(
                date=date(2024, 1, 1) + timedelta(days=offset),
                equity_value=nav,
                long_market_value=nav,
            )
        )

    return snapshots


def build_prices(symbol: str, daily_returns: list[float], start_price: float = 100.0) -> list[PricePoint]:
    prices = [PricePoint(symbol=symbol, date=date(2024, 1, 1), close=start_price)]
    close = start_price

    for offset, daily_return in enumerate(daily_returns, start=1):
        close *= 1 + daily_return
        prices.append(PricePoint(symbol=symbol, date=date(2024, 1, 1) + timedelta(days=offset), close=close))

    return prices


@pytest.fixture
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


def test_compute_portfolio_beta_matches_known_linear_series() -> None:
    spy_returns = [
        0.01,
        -0.02,
        0.015,
        -0.01,
        0.005,
        0.012,
        -0.008,
        0.009,
        -0.011,
        0.02,
        -0.005,
        0.007,
        -0.013,
        0.014,
        -0.006,
        0.01,
        -0.009,
        0.011,
        -0.004,
        0.013,
        -0.007,
        0.008,
        -0.01,
        0.016,
    ]
    portfolio_returns = [daily_return * 1.5 for daily_return in spy_returns]

    beta = compute_portfolio_beta(
        build_prices("SPY", spy_returns),
        build_snapshots(portfolio_returns),
    )

    assert beta == pytest.approx(1.5)


def test_compute_portfolio_beta_returns_none_with_insufficient_overlap() -> None:
    spy_returns = [0.01, -0.02, 0.015, -0.01, 0.005] * 3
    portfolio_returns = [daily_return * 1.2 for daily_return in spy_returns]

    beta = compute_portfolio_beta(
        build_prices("SPY", spy_returns),
        build_snapshots(portfolio_returns),
    )

    assert beta is None


def test_compute_correlation_matrix_handles_known_relationships() -> None:
    base_returns = [
        0.01,
        -0.02,
        0.015,
        -0.01,
        0.005,
        0.012,
        -0.008,
        0.009,
        -0.011,
        0.02,
        -0.005,
        0.007,
        -0.013,
        0.014,
        -0.006,
        0.01,
        -0.009,
        0.011,
        -0.004,
        0.013,
        -0.007,
        0.008,
        -0.01,
        0.016,
    ]
    inverse_returns = [-value for value in base_returns]
    short_returns = base_returns[:10]
    prices = [
        *build_prices("AAPL", base_returns),
        *build_prices("MSFT", base_returns),
        *build_prices("TSLA", inverse_returns),
        *build_prices("NFLX", short_returns),
    ]

    result = compute_correlation_matrix(
        prices,
        symbols=["AAPL", "MSFT", "TSLA", "NFLX"],
        lookback_days=252,
    )

    symbol_index = {symbol: index for index, symbol in enumerate(result["symbols"])}
    aapl = symbol_index["AAPL"]
    msft = symbol_index["MSFT"]
    tsla = symbol_index["TSLA"]
    nflx = symbol_index["NFLX"]

    assert result["matrix"][aapl][aapl] == pytest.approx(1.0)
    assert result["matrix"][msft][msft] == pytest.approx(1.0)
    assert result["matrix"][tsla][tsla] == pytest.approx(1.0)
    assert result["matrix"][aapl][msft] == pytest.approx(1.0)
    assert result["matrix"][aapl][tsla] == pytest.approx(-1.0)
    assert result["matrix"][aapl][msft] == result["matrix"][msft][aapl]
    assert result["matrix"][aapl][tsla] == result["matrix"][tsla][aapl]
    assert result["matrix"][aapl][nflx] is None


def test_compute_stress_scenarios_uses_beta_and_falls_back_when_missing() -> None:
    scenarios = compute_stress_scenarios(portfolio_value=100000.0, beta=1.3)
    market_crash = next(item for item in scenarios if item["scenario"] == "Market Crash")
    rally = next(item for item in scenarios if item["scenario"] == "Rally")

    assert market_crash["portfolio_move"] == pytest.approx(-0.26)
    assert market_crash["dollar_impact"] == pytest.approx(-26000.0)
    assert market_crash["portfolio_value_after"] == pytest.approx(74000.0)
    assert market_crash["beta_estimated"] is False
    assert rally["dollar_impact"] > 0

    fallback = compute_stress_scenarios(
        portfolio_value=50000.0,
        beta=None,
        scenarios=[{"name": "Correction", "market_move": -0.10}],
    )[0]
    assert fallback["beta_used"] == pytest.approx(1.0)
    assert fallback["beta_estimated"] is True
    assert fallback["dollar_impact"] == pytest.approx(-5000.0)


def test_backfill_ticker_prices_inserts_rows_and_only_fetches_gap(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch_calls: list[tuple[str, date, date]] = []
    first_start = date(2024, 1, 1)
    first_end = date(2024, 1, 3)
    second_start = date(2024, 1, 4)
    second_end = date(2024, 1, 5)

    def fake_fetch_daily_prices(symbol: str, start_date: date, end_date: date) -> list[dict[str, object]]:
        fetch_calls.append((symbol, start_date, end_date))
        if (start_date, end_date) == (first_start, first_end):
            return [
                {"symbol": symbol, "date": first_start, "close": 100.0, "volume": 1000.0},
                {"symbol": symbol, "date": first_start + timedelta(days=1), "close": 101.0, "volume": 1100.0},
                {"symbol": symbol, "date": first_end, "close": 102.0, "volume": 1200.0},
            ]
        if (start_date, end_date) == (second_start, second_end):
            return [
                {"symbol": symbol, "date": second_start, "close": 103.0, "volume": 1300.0},
                {"symbol": symbol, "date": second_end, "close": 104.0, "volume": 1400.0},
            ]
        return []

    class FirstNow:
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            return _FrozenMoment(first_end + timedelta(days=1))

    class SecondNow:
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            return _FrozenMoment(second_end + timedelta(days=1))

    monkeypatch.setattr(price_service, "fetch_daily_prices", fake_fetch_daily_prices)
    monkeypatch.setattr(price_service, "datetime", FirstNow)

    inserted = price_service.backfill_ticker_prices(db_session, "AAPL", lookback_days=2)
    rows = db_session.query(DailyPrice).filter(DailyPrice.symbol == "AAPL").order_by(DailyPrice.date.asc()).all()

    assert inserted == 3
    assert [row.date for row in rows] == [first_start, first_start + timedelta(days=1), first_end]
    assert fetch_calls[0] == ("AAPL", first_start, first_end)

    monkeypatch.setattr(price_service, "datetime", SecondNow)
    inserted = price_service.backfill_ticker_prices(db_session, "AAPL", lookback_days=2)
    rows = db_session.query(DailyPrice).filter(DailyPrice.symbol == "AAPL").order_by(DailyPrice.date.asc()).all()

    assert inserted == 2
    assert [row.date for row in rows] == [
        first_start,
        first_start + timedelta(days=1),
        first_end,
        second_start,
        second_end,
    ]
    assert fetch_calls[1] == ("AAPL", second_start, second_end)


def test_backfill_all_tickers_always_includes_spy(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_session.add_all([Ticker(symbol="AAPL"), Ticker(symbol="MSFT")])
    db_session.commit()

    called_symbols: list[str] = []

    def fake_backfill_ticker_prices(db: Session, symbol: str, lookback_days: int = 365) -> int:
        called_symbols.append(symbol)
        return 0

    monkeypatch.setattr(price_service, "backfill_ticker_prices", fake_backfill_ticker_prices)

    price_service.backfill_all_tickers(db_session)

    assert set(called_symbols) == {"AAPL", "MSFT", "SPY"}


class _FrozenMoment:
    def __init__(self, current_date: date) -> None:
        self._current_date = current_date

    def date(self) -> date:
        return self._current_date

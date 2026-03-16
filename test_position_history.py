from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

import pytest

from models import TradeAction
from position_history_service import (
    get_position_at_date,
    reconstruct_daily_positions,
    reconstruct_daily_positions_for_ticker,
)


@dataclass
class TradeStub:
    trade_id: str
    timestamp: datetime
    ticker: str
    action: TradeAction
    qty: float
    price: float | None
    commission: float = 0.0


def make_stub(
    trade_id: str,
    trade_date: date,
    action: TradeAction,
    qty: float,
    price: float | None,
    *,
    ticker: str = "AAPL",
    hour: int = 9,
    minute: int = 30,
    commission: float = 0.0,
) -> TradeStub:
    return TradeStub(
        trade_id=trade_id,
        timestamp=datetime(
            trade_date.year,
            trade_date.month,
            trade_date.day,
            hour,
            minute,
            tzinfo=timezone.utc,
        ),
        ticker=ticker,
        action=action,
        qty=qty,
        price=price,
        commission=commission,
    )


def test_basic_reconstruction() -> None:
    trades = [
        make_stub("buy-aapl", date(2024, 1, 1), TradeAction.BUY, 100, 50.0),
        make_stub("buy-msft", date(2024, 1, 2), TradeAction.BUY, 50, 200.0, ticker="MSFT"),
    ]

    snapshots = reconstruct_daily_positions(
        trades,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 2),
        starting_cash=100000.0,
    )

    assert snapshots[0]["positions"] == {"AAPL": 100.0}
    assert snapshots[0]["cash"] == pytest.approx(95000.0)
    assert snapshots[1]["positions"] == {"AAPL": 100.0, "MSFT": 50.0}
    assert snapshots[1]["cash"] == pytest.approx(85000.0)


def test_buy_and_sell() -> None:
    trades = [
        make_stub("buy-aapl", date(2024, 1, 1), TradeAction.BUY, 100, 50.0),
        make_stub("sell-aapl", date(2024, 1, 3), TradeAction.SELL, 50, 60.0),
    ]

    snapshots = reconstruct_daily_positions(
        trades,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 3),
        starting_cash=100000.0,
    )

    assert snapshots[1]["positions"] == {"AAPL": 100.0}
    assert snapshots[2]["positions"] == {"AAPL": 50.0}


def test_full_exit_excludes_zero_holdings() -> None:
    trades = [
        make_stub("buy-aapl", date(2024, 1, 1), TradeAction.BUY, 100, 50.0),
        make_stub("sell-aapl", date(2024, 1, 3), TradeAction.SELL, 100, 55.0),
    ]

    snapshots = reconstruct_daily_positions(
        trades,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 3),
        starting_cash=100000.0,
    )

    assert snapshots[2]["positions"] == {}


def test_multiple_trades_same_day() -> None:
    trades = [
        make_stub("buy-aapl", date(2024, 1, 2), TradeAction.BUY, 50, 100.0),
        make_stub("buy-msft", date(2024, 1, 2), TradeAction.BUY, 30, 200.0, ticker="MSFT", minute=45),
    ]

    snapshots = reconstruct_daily_positions(
        trades,
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 2),
        starting_cash=100000.0,
    )

    assert snapshots[0]["positions"] == {"AAPL": 50.0, "MSFT": 30.0}


def test_cash_tracking() -> None:
    trades = [
        make_stub("buy-aapl", date(2024, 1, 1), TradeAction.BUY, 100, 50.0, commission=5.0),
        make_stub("sell-aapl", date(2024, 1, 2), TradeAction.SELL, 50, 60.0, commission=3.0),
    ]

    snapshots = reconstruct_daily_positions(
        trades,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 2),
        starting_cash=100000.0,
    )

    assert snapshots[0]["cash"] == pytest.approx(94995.0)
    assert snapshots[1]["cash"] == pytest.approx(97992.0)


def test_trades_with_none_price_are_skipped() -> None:
    trades = [
        make_stub("ignored", date(2024, 1, 1), TradeAction.BUY, 100, None),
        make_stub("buy-aapl", date(2024, 1, 2), TradeAction.BUY, 25, 10.0),
    ]

    snapshots = reconstruct_daily_positions(
        trades,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 2),
        starting_cash=1000.0,
    )

    assert snapshots[0]["positions"] == {}
    assert snapshots[0]["cash"] == pytest.approx(1000.0)
    assert snapshots[1]["positions"] == {"AAPL": 25.0}
    assert snapshots[1]["cash"] == pytest.approx(750.0)


def test_pre_range_trades_establish_starting_state() -> None:
    trades = [
        make_stub("buy-aapl", date(2024, 1, 1), TradeAction.BUY, 100, 50.0),
    ]

    snapshots = reconstruct_daily_positions(
        trades,
        start_date=date(2024, 1, 10),
        end_date=date(2024, 1, 10),
        starting_cash=100000.0,
    )

    assert snapshots[0]["positions"] == {"AAPL": 100.0}
    assert snapshots[0]["cash"] == pytest.approx(95000.0)


def test_single_ticker_reconstruction() -> None:
    trades = [
        make_stub("buy-aapl", date(2024, 1, 1), TradeAction.BUY, 100, 50.0),
        make_stub("buy-msft", date(2024, 1, 2), TradeAction.BUY, 50, 200.0, ticker="MSFT"),
        make_stub("sell-aapl", date(2024, 1, 3), TradeAction.SELL, 40, 55.0),
    ]

    snapshots = reconstruct_daily_positions_for_ticker(
        trades,
        symbol="AAPL",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 3),
    )

    assert snapshots == [
        {"date": date(2024, 1, 1), "shares": 100.0},
        {"date": date(2024, 1, 2), "shares": 100.0},
        {"date": date(2024, 1, 3), "shares": 60.0},
    ]


def test_get_position_at_date() -> None:
    trades = [
        make_stub("buy-aapl", date(2024, 1, 1), TradeAction.BUY, 100, 50.0),
        make_stub("sell-aapl", date(2024, 1, 5), TradeAction.SELL, 50, 60.0),
    ]

    assert get_position_at_date(trades, "AAPL", date(2024, 1, 3)) == pytest.approx(100.0)
    assert get_position_at_date(trades, "AAPL", date(2024, 1, 5)) == pytest.approx(50.0)
    assert get_position_at_date(trades, "AAPL", date(2024, 1, 10)) == pytest.approx(50.0)


def test_empty_trade_history() -> None:
    snapshots = reconstruct_daily_positions(
        [],
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 3),
        starting_cash=100000.0,
    )

    assert snapshots == [
        {"date": date(2024, 1, 1), "positions": {}, "cash": 100000.0, "total_tickers": 0},
        {"date": date(2024, 1, 2), "positions": {}, "cash": 100000.0, "total_tickers": 0},
        {"date": date(2024, 1, 3), "positions": {}, "cash": 100000.0, "total_tickers": 0},
    ]

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from glassbox.services.cost_basis import compute_cost_basis, rebuild_realized_pnl, record_realized_pnl_for_trade
from glassbox.db.models import Base, RealizedPnl, Trade, TradeAction


@dataclass
class TradeStub:
    trade_id: str
    timestamp: datetime
    ticker: str
    action: TradeAction
    qty: float
    price: float | None
    commission: float = 0.0


@pytest.fixture
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


def make_stub(
    trade_id: str,
    action: TradeAction,
    qty: float,
    price: float | None,
    *,
    ticker: str = "AAPL",
    minute: int = 0,
    commission: float = 0.0,
) -> TradeStub:
    return TradeStub(
        trade_id=trade_id,
        timestamp=datetime(2024, 1, 1, 9, minute, tzinfo=timezone.utc),
        ticker=ticker,
        action=action,
        qty=qty,
        price=price,
        commission=commission,
    )


def make_trade(
    trade_id: str,
    action: TradeAction,
    qty: float,
    price: float | None,
    *,
    ticker: str = "AAPL",
    minute: int = 0,
    commission: float = 0.0,
) -> Trade:
    return Trade(
        trade_id=trade_id,
        timestamp=datetime(2024, 1, 1, 9, minute, tzinfo=timezone.utc),
        ticker=ticker,
        action=action,
        qty=qty,
        price=price,
        commission=commission,
        strategy_tag=None,
        investment_thesis="test",
    )


def test_compute_cost_basis_fifo_lot_matching() -> None:
    trades = [
        make_stub("buy-1", TradeAction.BUY, 100, 10.0, minute=1),
        make_stub("buy-2", TradeAction.BUY, 50, 12.0, minute=2),
        make_stub("sell-1", TradeAction.SELL, 120, 15.0, minute=3),
    ]

    result = compute_cost_basis(trades)["AAPL"]

    assert result.total_qty == pytest.approx(30.0)
    assert result.avg_cost == pytest.approx(12.0)
    assert result.total_cost_basis == pytest.approx(360.0)
    assert result.realized_pnl == pytest.approx(560.0)
    assert len(result.lots) == 1
    assert result.lots[0]["qty"] == pytest.approx(30.0)
    assert result.lots[0]["price"] == pytest.approx(12.0)


def test_compute_cost_basis_full_close_then_reopen() -> None:
    trades = [
        make_stub("buy-1", TradeAction.BUY, 50, 20.0, minute=1),
        make_stub("sell-1", TradeAction.SELL, 50, 25.0, minute=2),
        make_stub("buy-2", TradeAction.BUY, 100, 30.0, minute=3),
    ]

    result = compute_cost_basis(trades)["AAPL"]

    assert result.total_qty == pytest.approx(100.0)
    assert result.avg_cost == pytest.approx(30.0)
    assert result.total_cost_basis == pytest.approx(3000.0)
    assert result.realized_pnl == pytest.approx(250.0)


def test_compute_cost_basis_multiple_partial_sells() -> None:
    trades = [
        make_stub("buy-1", TradeAction.BUY, 200, 10.0, minute=1),
        make_stub("sell-1", TradeAction.SELL, 50, 12.0, minute=2),
        make_stub("sell-2", TradeAction.SELL, 50, 8.0, minute=3),
        make_stub("sell-3", TradeAction.SELL, 100, 15.0, minute=4),
    ]

    result = compute_cost_basis(trades)["AAPL"]

    assert result.total_qty == pytest.approx(0.0)
    assert result.avg_cost == pytest.approx(0.0)
    assert result.total_cost_basis == pytest.approx(0.0)
    assert result.realized_pnl == pytest.approx(500.0)
    assert result.lots == []


def test_compute_cost_basis_warns_and_skips_excess_sell(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    trades = [
        make_stub("buy-1", TradeAction.BUY, 50, 10.0, minute=1),
        make_stub("sell-1", TradeAction.SELL, 80, 15.0, minute=2),
    ]

    result = compute_cost_basis(trades)["AAPL"]

    assert result.total_qty == pytest.approx(0.0)
    assert result.realized_pnl == pytest.approx(250.0)
    assert "exceeded available quantity" in caplog.text


def test_compute_cost_basis_skips_none_price_trades() -> None:
    trades = [
        make_stub("buy-ignored", TradeAction.BUY, 100, None, minute=1),
        make_stub("buy-1", TradeAction.BUY, 25, 10.0, minute=2),
    ]

    result = compute_cost_basis(trades)["AAPL"]

    assert result.total_qty == pytest.approx(25.0)
    assert result.avg_cost == pytest.approx(10.0)
    assert result.realized_pnl == pytest.approx(0.0)


def test_rebuild_realized_pnl_is_idempotent(db_session: Session) -> None:
    db_session.add_all(
        [
            make_trade("buy-1", TradeAction.BUY, 100, 10.0, minute=1),
            make_trade("buy-2", TradeAction.BUY, 50, 12.0, minute=2),
            make_trade("sell-1", TradeAction.SELL, 120, 15.0, minute=3),
        ]
    )
    db_session.commit()

    first_count = rebuild_realized_pnl(db_session)
    first_rows = db_session.scalars(select(RealizedPnl).order_by(RealizedPnl.id.asc())).all()
    second_count = rebuild_realized_pnl(db_session)
    second_rows = db_session.scalars(select(RealizedPnl).order_by(RealizedPnl.id.asc())).all()

    assert first_count == 2
    assert second_count == 2
    assert len(first_rows) == 2
    assert len(second_rows) == 2
    assert [float(row.pnl) for row in second_rows] == pytest.approx([500.0, 60.0])


def test_record_realized_pnl_for_trade_is_incremental(db_session: Session) -> None:
    buy = make_trade("buy-1", TradeAction.BUY, 100, 10.0, minute=1)
    first_sell = make_trade("sell-1", TradeAction.SELL, 40, 12.0, minute=2)
    second_sell = make_trade("sell-2", TradeAction.SELL, 30, 14.0, minute=3)

    db_session.add_all([buy, first_sell])
    db_session.commit()

    first_rows = record_realized_pnl_for_trade(db_session, first_sell)
    db_session.commit()
    first_persisted = db_session.scalars(
        select(RealizedPnl).where(RealizedPnl.sell_trade_id == "sell-1")
    ).all()

    assert len(first_rows) == 1
    assert float(first_rows[0].qty) == pytest.approx(40.0)
    assert float(first_rows[0].pnl) == pytest.approx(80.0)
    assert len(first_persisted) == 1

    db_session.add(second_sell)
    db_session.commit()

    second_rows = record_realized_pnl_for_trade(db_session, second_sell)
    db_session.commit()
    persisted_rows = db_session.scalars(select(RealizedPnl).order_by(RealizedPnl.id.asc())).all()

    assert len(second_rows) == 1
    assert float(second_rows[0].qty) == pytest.approx(30.0)
    assert float(second_rows[0].pnl) == pytest.approx(120.0)
    assert len(persisted_rows) == 2
    assert [row.sell_trade_id for row in persisted_rows] == ["sell-1", "sell-2"]
    assert [float(row.pnl) for row in persisted_rows] == pytest.approx([80.0, 120.0])


def test_rebuild_realized_pnl_refreshes_missing_trade_prices(db_session: Session) -> None:
    db_session.add_all(
        [
            make_trade("buy-1", TradeAction.BUY, 10, None, minute=1),
            make_trade("sell-1", TradeAction.SELL, 10, None, minute=2),
        ]
    )
    db_session.commit()

    fill_prices = {"buy-1": 10.0, "sell-1": 12.0}
    rows_rebuilt = rebuild_realized_pnl(db_session, price_resolver=lambda trade_id: fill_prices.get(trade_id))
    rows = db_session.scalars(select(RealizedPnl).order_by(RealizedPnl.id.asc())).all()
    trades = db_session.scalars(select(Trade).order_by(Trade.trade_id.asc())).all()

    assert rows_rebuilt == 1
    assert len(rows) == 1
    assert float(rows[0].pnl) == pytest.approx(20.0)
    assert [float(trade.price or 0) for trade in trades] == pytest.approx([10.0, 12.0])


def test_record_realized_pnl_for_trade_refreshes_missing_trade_prices(db_session: Session) -> None:
    buy = make_trade("buy-1", TradeAction.BUY, 10, None, minute=1)
    sell = make_trade("sell-1", TradeAction.SELL, 10, None, minute=2)
    db_session.add_all([buy, sell])
    db_session.commit()

    fill_prices = {"buy-1": 10.0, "sell-1": 13.0}
    rows = record_realized_pnl_for_trade(
        db_session,
        sell,
        price_resolver=lambda trade_id: fill_prices.get(trade_id),
    )
    db_session.commit()

    persisted_rows = db_session.scalars(select(RealizedPnl).order_by(RealizedPnl.id.asc())).all()
    refreshed_trades = db_session.scalars(select(Trade).order_by(Trade.trade_id.asc())).all()

    assert len(rows) == 1
    assert len(persisted_rows) == 1
    assert float(persisted_rows[0].pnl) == pytest.approx(30.0)
    assert [float(trade.price or 0) for trade in refreshed_trades] == pytest.approx([10.0, 13.0])


def test_compute_cost_basis_includes_commission_in_lot_prices() -> None:
    trades = [
        make_stub("buy-1", TradeAction.BUY, 10, 10.0, minute=1, commission=5.0),
        make_stub("sell-1", TradeAction.SELL, 10, 12.0, minute=2, commission=2.0),
    ]

    result = compute_cost_basis(trades)["AAPL"]

    expected_buy_price = Decimal("10.5")
    expected_sell_price = Decimal("11.8")
    expected_pnl = (expected_sell_price - expected_buy_price) * Decimal("10")
    assert result.total_qty == pytest.approx(0.0)
    assert result.realized_pnl == pytest.approx(float(expected_pnl))

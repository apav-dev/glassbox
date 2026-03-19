from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from glassbox.api.app import app
from glassbox.api.routes import trades as trade_routes
from glassbox.db.models import Base, Trade, get_db
from glassbox.schemas.trading import TradeRequest
from glassbox.services.theses import create_thesis, get_current_thesis, get_thesis_history
from glassbox.services.trading import execute_trade


class FakeAlpacaClient:
    def __init__(self) -> None:
        self.positions: list[dict[str, Any]] = []
        self.order_count = 0

    def submit_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        *,
        order_type: str = "market",
        time_in_force: str = "day",
    ) -> dict[str, Any]:
        self.order_count += 1
        return {
            "id": f"order-{self.order_count}",
            "client_order_id": f"client-{self.order_count}",
            "status": "accepted",
            "symbol": symbol.upper(),
            "side": side,
            "qty": qty,
            "filled_qty": qty,
            "filled_avg_price": 101.25,
            "submitted_at": None,
            "order_type": order_type,
            "time_in_force": time_in_force,
        }

    def list_positions(self) -> list[dict[str, Any]]:
        return self.positions

    def get_order(self, order_id: str) -> dict[str, Any]:
        return {
            "id": order_id,
            "filled_avg_price": 101.25,
        }


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


def test_execute_trade_buy_creates_trade_and_thesis(db_session: Session) -> None:
    alpaca = FakeAlpacaClient()

    result = execute_trade(
        db_session,
        alpaca,
        TradeRequest(ticker="AAPL", side="buy", qty=5, thesis="Services growth remains durable."),
    )

    current = get_current_thesis(db_session, "AAPL")
    trades = db_session.query(Trade).all()

    assert result.thesis_action == "created"
    assert result.thesis_recorded is True
    assert len(trades) == 1
    assert trades[0].investment_thesis == "Services growth remains durable."
    assert current is not None
    assert current.version == 1
    assert current.status == "active"


def test_execute_trade_buy_same_thesis_does_not_append_version(db_session: Session) -> None:
    alpaca = FakeAlpacaClient()

    first = execute_trade(
        db_session,
        alpaca,
        TradeRequest(ticker="AAPL", side="buy", qty=1, thesis="Installed base monetization is compounding."),
    )
    second = execute_trade(
        db_session,
        alpaca,
        TradeRequest(ticker="AAPL", side="buy", qty=1, thesis="Installed base monetization is compounding."),
    )

    history = get_thesis_history(db_session, "AAPL")

    assert first.thesis_action == "created"
    assert second.thesis_action == "unchanged"
    assert second.thesis_recorded is False
    assert [row.version for row in history] == [1]


def test_execute_trade_buy_changed_thesis_appends_version(db_session: Session) -> None:
    alpaca = FakeAlpacaClient()

    execute_trade(
        db_session,
        alpaca,
        TradeRequest(ticker="AAPL", side="buy", qty=1, thesis="Cycle recovery is underway."),
    )
    result = execute_trade(
        db_session,
        alpaca,
        TradeRequest(ticker="AAPL", side="buy", qty=1, thesis="Services and margins are both inflecting."),
    )

    history = get_thesis_history(db_session, "AAPL")

    assert result.thesis_action == "versioned"
    assert result.thesis_recorded is True
    assert [row.thesis for row in history] == [
        "Cycle recovery is underway.",
        "Services and margins are both inflecting.",
    ]


def test_execute_trade_buy_after_closed_thesis_reopens_with_new_active_version(db_session: Session) -> None:
    alpaca = FakeAlpacaClient()
    create_thesis(
        db_session,
        symbol="AAPL",
        thesis="Original thesis",
        catalyst="WWDC",
        edge="Street is too low",
        status="closed",
    )

    result = execute_trade(
        db_session,
        alpaca,
        TradeRequest(ticker="AAPL", side="buy", qty=2, thesis="Re-entry thesis after reset."),
    )

    current = get_current_thesis(db_session, "AAPL")
    history = get_thesis_history(db_session, "AAPL")

    assert result.thesis_action == "versioned"
    assert current is not None
    assert current.version == 2
    assert current.status == "active"
    assert current.catalyst == "WWDC"
    assert current.edge == "Street is too low"
    assert [row.status for row in history] == ["closed", "active"]


def test_execute_trade_buy_after_watching_thesis_reopens_with_new_active_version(db_session: Session) -> None:
    alpaca = FakeAlpacaClient()
    create_thesis(
        db_session,
        symbol="AAPL",
        thesis="Watchlist thesis",
        catalyst="New product cycle",
        edge="Checks remain strong",
        status="watching",
    )

    result = execute_trade(
        db_session,
        alpaca,
        TradeRequest(ticker="AAPL", side="buy", qty=2, thesis="Position thesis after confirmation."),
    )

    current = get_current_thesis(db_session, "AAPL")

    assert result.thesis_action == "versioned"
    assert current is not None
    assert current.version == 2
    assert current.status == "active"
    assert current.catalyst == "New product cycle"
    assert current.edge == "Checks remain strong"


def test_execute_trade_partial_sell_does_not_append_thesis_version(db_session: Session) -> None:
    alpaca = FakeAlpacaClient()
    execute_trade(
        db_session,
        alpaca,
        TradeRequest(ticker="AAPL", side="buy", qty=4, thesis="Demand remains stronger than feared."),
    )
    alpaca.positions = [{"symbol": "AAPL", "qty": 2}]

    result = execute_trade(
        db_session,
        alpaca,
        TradeRequest(ticker="AAPL", side="sell", qty=2, thesis="Trim into strength."),
    )

    history = get_thesis_history(db_session, "AAPL")
    current = get_current_thesis(db_session, "AAPL")

    assert result.thesis_action == "not_written"
    assert result.thesis_recorded is False
    assert [row.version for row in history] == [1]
    assert current is not None
    assert current.status == "active"


def test_execute_trade_full_sell_closes_current_thesis(db_session: Session) -> None:
    alpaca = FakeAlpacaClient()
    execute_trade(
        db_session,
        alpaca,
        TradeRequest(ticker="AAPL", side="buy", qty=4, thesis="Installed base expansion thesis."),
    )
    alpaca.positions = []

    result = execute_trade(
        db_session,
        alpaca,
        TradeRequest(ticker="AAPL", side="sell", qty=4, thesis="Exit on thesis completion."),
    )

    current = get_current_thesis(db_session, "AAPL")

    assert result.thesis_action == "closed"
    assert result.thesis_recorded is True
    assert current is not None
    assert current.status == "closed"


def test_http_execute_trade_creates_thesis_and_returns_precise_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'http.db'}", future=True)
    Base.metadata.create_all(engine)
    testing_session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    alpaca = FakeAlpacaClient()

    def override_get_db() -> Iterator[Session]:
        session = testing_session_local()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("glassbox.api.app.init_db", lambda: None)
    monkeypatch.setattr(trade_routes, "get_alpaca_client", lambda: alpaca)
    monkeypatch.setattr(trade_routes, "backfill_symbol_prices_in_background", lambda symbol: None)
    monkeypatch.setattr(trade_routes, "refresh_symbol_fundamentals_in_background", lambda symbol: None)
    app.dependency_overrides[get_db] = override_get_db

    try:
        with TestClient(app) as client:
            response = client.post(
                "/trade/execute",
                json={"ticker": "AAPL", "side": "buy", "qty": 3, "thesis": "Portfolio services thesis."},
            )
            thesis_response = client.get("/theses/AAPL")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["thesis_recorded"] is True
    assert response.json()["thesis_action"] == "created"
    assert thesis_response.status_code == 200
    assert thesis_response.json()["thesis"] == "Portfolio services thesis."


def test_stdio_execute_trade_serializes_thesis_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import glassbox.api.mcp_stdio as mcp_stdio

    engine = create_engine(f"sqlite:///{tmp_path / 'stdio.db'}", future=True)
    Base.metadata.create_all(engine)
    testing_session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    alpaca = FakeAlpacaClient()

    monkeypatch.setattr(mcp_stdio, "SessionLocal", testing_session_local)
    monkeypatch.setattr(mcp_stdio, "get_alpaca_client", lambda: alpaca)
    monkeypatch.setattr(mcp_stdio, "backfill_symbol_prices_in_background", lambda symbol: None)
    monkeypatch.setattr(mcp_stdio, "refresh_symbol_fundamentals_in_background", lambda symbol: None)

    first = mcp_stdio.execute_trade("AAPL", "buy", 1, "Cash flow durability thesis.")
    second = mcp_stdio.execute_trade("AAPL", "buy", 1, "Cash flow durability thesis.")

    session = testing_session_local()
    try:
        current = get_current_thesis(session, "AAPL")
    finally:
        session.close()

    assert isinstance(first, dict)
    assert isinstance(second, dict)
    assert first["thesis_recorded"] is True
    assert first["thesis_action"] == "created"
    assert second["thesis_recorded"] is False
    assert second["thesis_action"] == "unchanged"
    assert current is not None
    assert current.thesis == "Cash flow durability thesis."

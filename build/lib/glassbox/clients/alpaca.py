from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

try:
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import OrderSide, OrderType, QueryOrderStatus, TimeInForce
    from alpaca.trading.requests import GetOrdersRequest, GetPortfolioHistoryRequest, OrderRequest
except ImportError as exc:  # pragma: no cover - import guard for local dev bootstrapping
    StockHistoricalDataClient = None
    StockBarsRequest = None
    TimeFrame = None
    TradingClient = None
    OrderSide = None
    OrderType = None
    QueryOrderStatus = None
    TimeInForce = None
    GetOrdersRequest = None
    GetPortfolioHistoryRequest = None
    OrderRequest = None
    _alpaca_import_error = exc
else:
    _alpaca_import_error = None


class AlpacaConfigurationError(RuntimeError):
    pass


@dataclass(slots=True)
class AlpacaSettings:
    api_key: str
    api_secret: str
    paper: bool = True
    trading_base_url: str | None = None
    data_base_url: str | None = None

    @classmethod
    def from_env(cls) -> "AlpacaSettings":
        api_key = os.getenv("ALPACA_API_KEY")
        api_secret = os.getenv("ALPACA_API_SECRET") or os.getenv("ALPACA_SECRET_KEY")

        if not api_key or not api_secret:
            raise AlpacaConfigurationError(
                "Missing Alpaca credentials. Set ALPACA_API_KEY and ALPACA_API_SECRET or ALPACA_SECRET_KEY."
            )

        trading_base_url = os.getenv("ALPACA_BASE_URL")
        explicit_paper = os.getenv("ALPACA_PAPER")
        if explicit_paper is not None:
            paper = _env_bool(explicit_paper)
        elif trading_base_url:
            paper = "paper-api" in trading_base_url
        else:
            paper = True

        return cls(
            api_key=api_key,
            api_secret=api_secret,
            paper=paper,
            trading_base_url=trading_base_url,
            data_base_url=os.getenv("ALPACA_DATA_BASE_URL"),
        )


class AlpacaClient:
    def __init__(self, settings: AlpacaSettings | None = None) -> None:
        if TradingClient is None or StockHistoricalDataClient is None:
            raise ImportError(
                "alpaca-py is required. Install it with `pip install alpaca-py`."
            ) from _alpaca_import_error

        self.settings = settings or AlpacaSettings.from_env()
        self.trading_client = TradingClient(
            api_key=self.settings.api_key,
            secret_key=self.settings.api_secret,
            paper=self.settings.paper,
            raw_data=False,
            url_override=self.settings.trading_base_url,
        )
        self.data_client = StockHistoricalDataClient(
            api_key=self.settings.api_key,
            secret_key=self.settings.api_secret,
            raw_data=False,
            url_override=self.settings.data_base_url,
        )

    def get_account(self) -> Any:
        return self.trading_client.get_account()

    def get_account_summary(self) -> dict[str, float]:
        account = self.get_account()
        equity = self._to_float(account.equity)
        last_equity = self._to_float(getattr(account, "last_equity", 0))

        return {
            "nav": self._to_float(getattr(account, "portfolio_value", account.equity)),
            "equity": equity,
            "cash": self._to_float(account.cash),
            "buying_power": self._to_float(account.buying_power),
            "day_pnl": equity - last_equity,
            "day_pnl_pct": ((equity - last_equity) / last_equity) if last_equity else 0.0,
            "long_market_value": self._to_float(getattr(account, "long_market_value", 0)),
        }

    def list_positions(self) -> list[dict[str, float | str]]:
        positions = self.trading_client.get_all_positions()
        return [
            {
                "symbol": position.symbol,
                "side": position.side.value,
                "qty": self._to_float(position.qty),
                "avg_entry_price": self._to_float(position.avg_entry_price),
                "market_value": self._to_float(position.market_value),
                "cost_basis": self._to_float(position.cost_basis),
                "unrealized_pl": self._to_float(position.unrealized_pl),
                "unrealized_plpc": self._to_float(position.unrealized_plpc),
            }
            for position in positions
        ]

    def list_orders(self, status: str = "all", limit: int = 100) -> list[Any]:
        request = GetOrdersRequest(
            status=QueryOrderStatus(status.lower()),
            limit=limit,
            nested=True,
        )
        return self.trading_client.get_orders(filter=request)

    def submit_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        *,
        order_type: str = "market",
        time_in_force: str = "day",
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        order = self.trading_client.submit_order(
            order_data=OrderRequest(
                symbol=symbol.upper(),
                side=OrderSide(side.lower()),
                qty=qty,
                type=OrderType(order_type.lower()),
                time_in_force=TimeInForce(time_in_force.lower()),
                client_order_id=client_order_id or f"glassbox-{uuid.uuid4()}",
            )
        )
        return {
            "id": str(order.id),
            "client_order_id": order.client_order_id,
            "status": order.status.value,
            "symbol": order.symbol,
            "side": order.side.value if order.side is not None else side.lower(),
            "qty": self._to_float(order.qty),
            "filled_qty": self._to_float(getattr(order, "filled_qty", 0)),
            "filled_avg_price": self._to_float(getattr(order, "filled_avg_price", 0)),
            "submitted_at": getattr(order, "submitted_at", None),
        }

    def get_portfolio_history(
        self,
        *,
        period: str = "1M",
        timeframe: str = "1D",
        extended_hours: bool = False,
    ) -> Any:
        return self.trading_client.get_portfolio_history(
            history_filter=GetPortfolioHistoryRequest(
                period=period,
                timeframe=timeframe,
                extended_hours=extended_hours,
            )
        )

    def get_daily_bars(self, *, symbol: str, start_date: date, end_date: date) -> list[Any]:
        bar_set = self.data_client.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=symbol.upper(),
                timeframe=TimeFrame.Day,
                start=datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc),
                end=datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc),
            )
        )
        return list(bar_set.data.get(symbol.upper(), []))

    def calculate_max_drawdown(self, *, period: str = "1M", timeframe: str = "1D") -> float:
        history = self.get_portfolio_history(period=period, timeframe=timeframe)
        equities = [self._to_float(equity) for equity in getattr(history, "equity", [])]

        peak = 0.0
        max_drawdown = 0.0
        for equity in equities:
            peak = max(peak, equity)
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak - equity) / peak)

        return max_drawdown

    def build_daily_snapshot(self) -> dict[str, float | date]:
        account = self.get_account_summary()
        return {
            "date": datetime.now(timezone.utc).date(),
            "equity_value": account["equity"],
            "cash_balance": account["cash"],
            "long_market_value": account["long_market_value"],
            "total_drawdown": self.calculate_max_drawdown(),
        }

    @staticmethod
    def _to_float(value: Any) -> float:
        if value in (None, ""):
            return 0.0
        return float(value)


def _env_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}

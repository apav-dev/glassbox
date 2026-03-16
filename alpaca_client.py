from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

try:
    import alpaca_trade_api as tradeapi
except ImportError as exc:  # pragma: no cover - import guard for local dev bootstrapping
    tradeapi = None
    _alpaca_import_error = exc
else:
    _alpaca_import_error = None


class AlpacaConfigurationError(RuntimeError):
    pass


@dataclass(slots=True)
class AlpacaSettings:
    api_key: str
    api_secret: str
    base_url: str = "https://paper-api.alpaca.markets"
    api_version: str = "v2"

    @classmethod
    def from_env(cls) -> "AlpacaSettings":
        api_key = os.getenv("ALPACA_API_KEY")
        api_secret = os.getenv("ALPACA_API_SECRET")

        if not api_key or not api_secret:
            raise AlpacaConfigurationError(
                "Missing Alpaca credentials. Set ALPACA_API_KEY and ALPACA_API_SECRET."
            )

        return cls(
            api_key=api_key,
            api_secret=api_secret,
            base_url=os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
            api_version=os.getenv("ALPACA_API_VERSION", "v2"),
        )


class AlpacaClient:
    def __init__(self, settings: AlpacaSettings | None = None) -> None:
        if tradeapi is None:
            raise ImportError(
                "alpaca-trade-api is required. Install it with `pip install alpaca-trade-api`."
            ) from _alpaca_import_error

        self.settings = settings or AlpacaSettings.from_env()
        self.client = tradeapi.REST(
            key_id=self.settings.api_key,
            secret_key=self.settings.api_secret,
            base_url=self.settings.base_url,
            api_version=self.settings.api_version,
        )

    def get_account(self) -> Any:
        return self.client.get_account()

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
        positions = self.client.list_positions()
        return [
            {
                "symbol": position.symbol,
                "side": position.side,
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
        return self.client.list_orders(status=status, limit=limit, nested=True)

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
        order = self.client.submit_order(
            symbol=symbol.upper(),
            side=side.lower(),
            qty=qty,
            type=order_type,
            time_in_force=time_in_force,
            client_order_id=client_order_id or f"glassbox-{uuid.uuid4()}",
        )
        return {
            "id": order.id,
            "client_order_id": order.client_order_id,
            "status": order.status,
            "symbol": order.symbol,
            "side": order.side,
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
        return self.client.get_portfolio_history(
            period=period,
            timeframe=timeframe,
            extended_hours=extended_hours,
        )

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

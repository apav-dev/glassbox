import json
import os
from typing import Any
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP


GLASSBOX_BASE_URL = os.getenv("GLASSBOX_BASE_URL", "http://localhost:8000")
MCP_PORT = int(os.getenv("MCP_PORT", "8100"))
HTTP_TIMEOUT = float(os.getenv("GLASSBOX_TIMEOUT_SECONDS", "30"))

mcp = FastMCP(
    "Glassbox Trading",
    instructions=(
        "MCP server for the Glassbox day-trading backend. Provides portfolio analytics, "
        "trade execution, risk metrics, and thesis management."
    ),
    host="0.0.0.0",
    port=MCP_PORT,
)


def _strip_none(data: dict[str, Any] | None) -> dict[str, Any] | None:
    if data is None:
        return None
    return {key: value for key, value in data.items() if value is not None}


def _extract_error_detail(response: httpx.Response) -> Any:
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return response.text

    if isinstance(payload, dict) and "detail" in payload:
        return payload["detail"]
    return payload


async def _glassbox_request(
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict | list:
    """Make an HTTP request to the glassbox FastAPI service and return parsed JSON."""
    async with httpx.AsyncClient(base_url=GLASSBOX_BASE_URL, timeout=HTTP_TIMEOUT) as client:
        try:
            response = await client.request(
                method,
                path,
                params=_strip_none(params),
                json=_strip_none(json_body),
            )
        except httpx.HTTPError as exc:
            return {
                "error": True,
                "status_code": 503,
                "detail": str(exc),
            }

    if response.status_code >= 400:
        return {
            "error": True,
            "status_code": response.status_code,
            "detail": _extract_error_detail(response),
        }

    return response.json()


@mcp.tool()
async def health_check() -> dict | list:
    """Returns local database and Alpaca connectivity status. Use this to confirm the glassbox backend is reachable and healthy."""
    return await _glassbox_request("GET", "/health")


@mcp.tool()
async def sync_portfolio() -> dict | list:
    """Refreshes the local daily snapshot and ticker cache from Alpaca. Use this before analytics when you need the latest synced portfolio state."""
    return await _glassbox_request("POST", "/sync")


@mcp.tool()
async def get_portfolio_summary() -> dict | list:
    """Returns current account NAV, equity, cash, buying power, and daily P&L from Alpaca. Use this to check the overall portfolio state."""
    return await _glassbox_request("GET", "/portfolio/summary")


@mcp.tool()
async def list_positions() -> dict | list:
    """Returns current Alpaca positions enriched with local company and fundamentals data. Use this to review the live holdings in detail."""
    return await _glassbox_request("GET", "/portfolio/positions")


@mcp.tool()
async def list_weighted_positions() -> dict | list:
    """Returns current positions with portfolio weights plus local enrichment fields. Use this to see how concentrated the portfolio is by position."""
    return await _glassbox_request("GET", "/portfolio/positions/weighted")


@mcp.tool()
async def execute_trade(
    ticker: str,
    side: str,
    qty: float,
    thesis: str,
    strategy_tag: str | None = None,
    order_type: str = "market",
    time_in_force: str = "day",
) -> dict | list:
    """Submits an Alpaca order and logs trade metadata locally. Requires a ticker, side, quantity, and investment thesis."""
    return await _glassbox_request(
        "POST",
        "/trade/execute",
        json_body={
            "ticker": ticker,
            "side": side,
            "qty": qty,
            "thesis": thesis,
            "strategy_tag": strategy_tag,
            "order_type": order_type,
            "time_in_force": time_in_force,
        },
    )


@mcp.tool()
async def list_trades(
    ticker: str | None = None,
    strategy_tag: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict | list:
    """Returns locally persisted trade history with optional symbol and strategy filters. Use this to review past executions and metadata."""
    return await _glassbox_request(
        "GET",
        "/trades",
        params={
            "ticker": ticker,
            "strategy_tag": strategy_tag,
            "limit": limit,
            "offset": offset,
        },
    )


@mcp.tool()
async def get_risk_metrics(risk_free_rate: float = 0.045) -> dict | list:
    """Returns portfolio-level risk analytics including Sharpe ratio, Sortino ratio, VaR, max drawdown, and beta to SPY. Use this to evaluate portfolio risk and performance quality."""
    return await _glassbox_request(
        "GET",
        "/portfolio/risk",
        params={"risk_free_rate": risk_free_rate},
    )


@mcp.tool()
async def get_equity_curve() -> dict | list:
    """Returns the stored equity curve with daily returns and drawdown. Use this to inspect portfolio performance over time."""
    return await _glassbox_request("GET", "/portfolio/equity-curve")


@mcp.tool()
async def get_correlation_matrix(lookback_days: int = 252) -> dict | list:
    """Returns a correlation matrix for currently held symbols using local price history. Use this to check diversification and co-movement across positions."""
    return await _glassbox_request(
        "GET",
        "/portfolio/correlation",
        params={"lookback_days": lookback_days},
    )


@mcp.tool()
async def get_stress_test() -> dict | list:
    """Returns portfolio stress scenarios for standard market moves. Use this to estimate potential portfolio impact under broad market shocks."""
    return await _glassbox_request("GET", "/portfolio/stress-test")


@mcp.tool()
async def get_risk_report(risk_free_rate: float = 0.045) -> dict | list:
    """Returns a combined risk report with portfolio metrics, sector exposure, and geographic exposure. Use this for a broader portfolio risk review."""
    return await _glassbox_request(
        "GET",
        "/portfolio/risk/report",
        params={"risk_free_rate": risk_free_rate},
    )


@mcp.tool()
async def get_sector_exposure() -> dict | list:
    """Returns live portfolio exposure grouped by sector. Use this to understand concentration by industry sector."""
    return await _glassbox_request("GET", "/portfolio/exposure/sector")


@mcp.tool()
async def get_geographic_exposure() -> dict | list:
    """Returns live portfolio exposure grouped by geography. Use this to understand country-level concentration in the portfolio."""
    return await _glassbox_request("GET", "/portfolio/exposure/geographic")


@mcp.tool()
async def get_thematic_exposure() -> dict | list:
    """Returns live portfolio exposure grouped by thematic tags. Use this to review how portfolio value is distributed across your custom themes."""
    return await _glassbox_request("GET", "/portfolio/exposure/thematic")


@mcp.tool()
async def get_cost_basis() -> dict | list:
    """Returns FIFO cost basis, lot data, and realized P&L by ticker from the local trade ledger. Use this to inspect entry prices and tax-lot state."""
    return await _glassbox_request("GET", "/portfolio/cost-basis")


@mcp.tool()
async def get_realized_pnl(
    ticker: str | None = None,
    since: str | None = None,
) -> dict | list:
    """Returns aggregate realized P&L totals with an optional ticker or start time filter. Use this to summarize closed gains and losses."""
    return await _glassbox_request(
        "GET",
        "/portfolio/realized-pnl",
        params={"ticker": ticker, "since": since},
    )


@mcp.tool()
async def get_realized_pnl_details(
    ticker: str | None = None,
    since: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict | list:
    """Returns lot-level realized P&L records with optional filters and pagination. Use this when you need the underlying sell-lot detail."""
    return await _glassbox_request(
        "GET",
        "/portfolio/realized-pnl/details",
        params={
            "ticker": ticker,
            "since": since,
            "limit": limit,
            "offset": offset,
        },
    )


@mcp.tool()
async def rebuild_realized_pnl() -> dict | list:
    """Rebuilds the realized P&L table from the local trade log. Use this after ledger changes when you need to recompute realized gains and losses."""
    return await _glassbox_request("POST", "/portfolio/realized-pnl/rebuild")


@mcp.tool()
async def list_fundamentals() -> dict | list:
    """Returns stored fundamentals for currently held symbols. Use this to review valuation and analyst data across active positions."""
    return await _glassbox_request("GET", "/portfolio/fundamentals")


@mcp.tool()
async def get_ticker_fundamentals(symbol: str) -> dict | list:
    """Returns stored fundamentals for one ticker. Use this to inspect valuation and analyst fields for a specific symbol."""
    return await _glassbox_request("GET", f"/portfolio/fundamentals/{quote(symbol, safe='')}")


@mcp.tool()
async def refresh_fundamentals(
    symbol: str | None = None,
    stale_only: bool = True,
) -> dict | list:
    """Refreshes fundamentals for one ticker or the full local ticker universe. Use this when fundamentals may be stale or missing."""
    return await _glassbox_request(
        "POST",
        "/portfolio/fundamentals/refresh",
        params={"symbol": symbol, "stale_only": stale_only},
    )


@mcp.tool()
async def list_theses(
    status: str | None = None,
    symbol: str | None = None,
) -> dict | list:
    """Returns current thesis records with optional status or symbol filters. Use this to review the active investment narrative across names."""
    return await _glassbox_request(
        "GET",
        "/theses",
        params={"status": status, "symbol": symbol},
    )


@mcp.tool()
async def get_thesis(symbol: str) -> dict | list:
    """Returns the current thesis record for one symbol. Use this when you need the latest thesis for a specific position or watchlist name."""
    return await _glassbox_request("GET", f"/theses/{quote(symbol, safe='')}")


@mcp.tool()
async def create_thesis(
    symbol: str,
    thesis: str,
    catalyst: str | None = None,
    edge: str | None = None,
    status: str = "active",
    created_by: str = "agent",
) -> dict | list:
    """Creates a new thesis version for a symbol. Use this to record or update the investment rationale behind a name."""
    return await _glassbox_request(
        "POST",
        "/theses",
        json_body={
            "symbol": symbol,
            "thesis": thesis,
            "catalyst": catalyst,
            "edge": edge,
            "status": status,
            "created_by": created_by,
        },
    )


@mcp.tool()
async def update_thesis_status(symbol: str, status: str) -> dict | list:
    """Updates the status of the current thesis for a symbol. Use this to move a name between active, closed, and watching states."""
    return await _glassbox_request(
        "PATCH",
        f"/theses/{quote(symbol, safe='')}/status",
        json_body={"status": status},
    )


@mcp.tool()
async def list_thesis_history(symbol: str) -> dict | list:
    """Returns all thesis versions for a symbol. Use this to review how the thesis evolved over time."""
    return await _glassbox_request("GET", f"/theses/{quote(symbol, safe='')}/history")


@mcp.tool()
async def list_enriched_theses() -> dict | list:
    """Returns current active theses enriched with portfolio weight and key fundamentals. Use this for a portfolio-level view of thesis quality and exposure."""
    return await _glassbox_request("GET", "/theses/enriched")


@mcp.tool()
async def list_tags() -> dict | list:
    """Returns all local thematic tags. Use this to inspect the available tagging taxonomy."""
    return await _glassbox_request("GET", "/tags")


@mcp.tool()
async def create_tag(
    name: str,
    color: str | None = None,
    description: str | None = None,
) -> dict | list:
    """Creates a thematic tag if it does not already exist. Use this before assigning a new theme to one or more tickers."""
    return await _glassbox_request(
        "POST",
        "/tags",
        json_body={"name": name, "color": color, "description": description},
    )


@mcp.tool()
async def list_tagged_tickers(tag_name: str) -> dict | list:
    """Returns all symbols assigned to a thematic tag. Use this to inspect the membership of one theme."""
    return await _glassbox_request("GET", f"/tags/{quote(tag_name, safe='')}/tickers")


@mcp.tool()
async def list_ticker_tags(symbol: str) -> dict | list:
    """Returns all thematic tags assigned to a ticker. Use this to review how a symbol is classified."""
    return await _glassbox_request("GET", f"/tickers/{quote(symbol, safe='')}/tags")


@mcp.tool()
async def tag_ticker(
    symbol: str,
    tag_name: str,
    tagged_by: str = "agent",
) -> dict | list:
    """Applies one thematic tag to a ticker. Use this to associate a symbol with a strategy or theme."""
    return await _glassbox_request(
        "POST",
        f"/tickers/{quote(symbol, safe='')}/tags",
        json_body={"symbol": symbol, "tag_name": tag_name, "tagged_by": tagged_by},
    )


@mcp.tool()
async def bulk_tag_ticker(
    symbol: str,
    tag_names: list[str],
    tagged_by: str = "agent",
) -> dict | list:
    """Applies multiple thematic tags to a ticker in one request. Use this when classifying a symbol across several themes at once."""
    return await _glassbox_request(
        "POST",
        f"/tickers/{quote(symbol, safe='')}/tags/bulk",
        json_body={"symbol": symbol, "tag_names": tag_names, "tagged_by": tagged_by},
    )


@mcp.tool()
async def untag_ticker(symbol: str, tag_name: str) -> dict | list:
    """Removes a thematic tag from a ticker. Use this when a symbol no longer belongs in a theme or strategy bucket."""
    return await _glassbox_request(
        "DELETE",
        f"/tickers/{quote(symbol, safe='')}/tags/{quote(tag_name, safe='')}",
    )


@mcp.tool()
async def get_position_history(
    start_date: str,
    end_date: str,
    starting_cash: float,
) -> dict | list:
    """Returns reconstructed daily positions and cash over a date range. Use this to review how the portfolio evolved through time."""
    return await _glassbox_request(
        "GET",
        "/portfolio/history/positions",
        params={
            "start_date": start_date,
            "end_date": end_date,
            "starting_cash": starting_cash,
        },
    )


@mcp.tool()
async def get_symbol_position_history(
    symbol: str,
    start_date: str,
    end_date: str,
) -> dict | list:
    """Returns reconstructed daily share counts for one ticker over a date range. Use this to inspect position sizing history for a symbol."""
    return await _glassbox_request(
        "GET",
        f"/portfolio/history/positions/{quote(symbol, safe='')}",
        params={"start_date": start_date, "end_date": end_date},
    )


@mcp.tool()
async def get_symbol_position_at_date(symbol: str, date: str) -> dict | list:
    """Returns the reconstructed share count for one ticker on a specific date. Use this to answer point-in-time position questions."""
    return await _glassbox_request(
        "GET",
        f"/portfolio/history/positions/{quote(symbol, safe='')}/at",
        params={"date": date},
    )


if __name__ == "__main__":
    mcp.run(transport="streamable-http")

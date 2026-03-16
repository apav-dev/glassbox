from __future__ import annotations

import math
import statistics
from datetime import date
from typing import Any, Protocol


class SnapshotLike(Protocol):
    date: date
    equity_value: float
    cash_balance: float
    long_market_value: float


class TickerLike(Protocol):
    sector: str | None
    country: str | None


class PriceLike(Protocol):
    symbol: str
    date: date
    close: float


def _to_float(value: Any) -> float:
    return float(value) if value is not None else 0.0


def compute_daily_returns(snapshots: list[SnapshotLike]) -> list[dict[str, Any]]:
    """Build an equity curve that shows daily performance, cumulative return, and drawdown over time."""
    if not snapshots:
        return []

    first_nav = _to_float(snapshots[0].equity_value)
    peak_equity = first_nav
    results: list[dict[str, Any]] = []

    for index, snapshot in enumerate(snapshots):
        nav = _to_float(snapshot.equity_value)
        cash = _to_float(snapshot.cash_balance)
        invested_value = _to_float(snapshot.long_market_value)

        peak_equity = max(peak_equity, nav)
        daily_return = 0.0
        if index > 0:
            previous_nav = _to_float(snapshots[index - 1].equity_value)
            daily_return = ((nav - previous_nav) / previous_nav) if previous_nav else 0.0

        cumulative_return = ((nav / first_nav) - 1.0) if first_nav else 0.0
        drawdown = ((nav - peak_equity) / peak_equity) if peak_equity else 0.0

        results.append(
            {
                "date": snapshot.date,
                "cash": cash,
                "invested_value": invested_value,
                "nav": nav,
                "daily_return": daily_return,
                "cumulative_return": cumulative_return,
                "drawdown": drawdown,
            }
        )

    return results


def compute_risk_metrics(
    snapshots: list[SnapshotLike],
    risk_free_rate: float = 0.045,
    benchmark_prices: list[PriceLike] | None = None,
) -> dict[str, Any]:
    """Summarize return, volatility, downside risk, and drawdown statistics for the full snapshot history."""
    empty_metrics = {
        "total_return": None,
        "annualized_return": None,
        "annualized_volatility": None,
        "sharpe_ratio": None,
        "sortino_ratio": None,
        "var_95": None,
        "var_99": None,
        "cvar_95": None,
        "max_drawdown": None,
        "max_drawdown_date": None,
        "best_day": None,
        "worst_day": None,
        "win_rate": None,
        "current_drawdown": None,
        "beta_to_spy": None,
        "snapshot_count": len(snapshots),
    }
    if len(snapshots) < 2:
        return empty_metrics

    curve = compute_daily_returns(snapshots)
    daily_returns = [point["daily_return"] for point in curve[1:]]
    negative_returns = [value for value in daily_returns if value < 0]
    var_95 = None
    var_99 = None
    cvar_95 = None

    if len(daily_returns) >= 20:
        sorted_returns = sorted(daily_returns)
        var_95_cutoff = sorted_returns[int(len(sorted_returns) * 0.05)]
        var_99_cutoff = sorted_returns[int(len(sorted_returns) * 0.01)]
        cvar_95_tail = [value for value in sorted_returns if value <= var_95_cutoff]

        var_95 = abs(var_95_cutoff)
        var_99 = abs(var_99_cutoff)
        cvar_95 = abs(statistics.mean(cvar_95_tail))

    first_nav = _to_float(snapshots[0].equity_value)
    last_nav = _to_float(snapshots[-1].equity_value)
    day_span = (snapshots[-1].date - snapshots[0].date).days

    total_return = ((last_nav / first_nav) - 1.0) if first_nav else None
    annualized_return = None
    if total_return is not None and day_span > 0 and first_nav > 0 and last_nav > 0:
        annualized_return = math.pow(last_nav / first_nav, 365 / day_span) - 1.0

    annualized_volatility = None
    if len(daily_returns) > 1:
        annualized_volatility = statistics.stdev(daily_returns) * math.sqrt(252)
    elif len(daily_returns) == 1:
        annualized_volatility = 0.0

    sharpe_ratio = None
    if annualized_return is not None and annualized_volatility not in (None, 0.0):
        sharpe_ratio = (annualized_return - risk_free_rate) / annualized_volatility

    sortino_ratio = None
    if annualized_return is not None and negative_returns:
        downside_deviation = math.sqrt(sum(value * value for value in negative_returns) / len(negative_returns))
        downside_deviation *= math.sqrt(252)
        if downside_deviation != 0.0:
            sortino_ratio = (annualized_return - risk_free_rate) / downside_deviation

    max_drawdown_point = min(curve, key=lambda point: point["drawdown"])
    best_day_point = max(curve[1:], key=lambda point: point["daily_return"])
    worst_day_point = min(curve[1:], key=lambda point: point["daily_return"])
    win_rate = sum(1 for value in daily_returns if value > 0) / len(daily_returns) if daily_returns else None

    return {
        "total_return": total_return,
        "annualized_return": annualized_return,
        "annualized_volatility": annualized_volatility,
        "sharpe_ratio": sharpe_ratio,
        "sortino_ratio": sortino_ratio,
        "var_95": var_95,
        "var_99": var_99,
        "cvar_95": cvar_95,
        "max_drawdown": abs(max_drawdown_point["drawdown"]),
        "max_drawdown_date": max_drawdown_point["date"],
        "best_day": {
            "date": best_day_point["date"],
            "value": best_day_point["daily_return"],
        },
        "worst_day": {
            "date": worst_day_point["date"],
            "value": worst_day_point["daily_return"],
        },
        "win_rate": win_rate,
        "current_drawdown": abs(curve[-1]["drawdown"]),
        "beta_to_spy": compute_portfolio_beta(benchmark_prices or [], snapshots),
        "snapshot_count": len(snapshots),
    }


def compute_portfolio_beta(
    daily_prices: list[PriceLike],
    portfolio_snapshots: list[SnapshotLike],
    benchmark_symbol: str = "SPY",
) -> float | None:
    benchmark_returns = _build_price_return_map(
        [
            price
            for price in daily_prices
            if str(getattr(price, "symbol", "")).upper() == benchmark_symbol.upper()
        ]
    )
    portfolio_returns = {
        point["date"]: point["daily_return"]
        for point in compute_daily_returns(portfolio_snapshots)[1:]
    }

    overlapping_dates = sorted(set(benchmark_returns) & set(portfolio_returns))
    if len(overlapping_dates) < 20:
        return None

    benchmark_values = [benchmark_returns[day] for day in overlapping_dates]
    portfolio_values = [portfolio_returns[day] for day in overlapping_dates]

    benchmark_mean = statistics.mean(benchmark_values)
    portfolio_mean = statistics.mean(portfolio_values)
    benchmark_variance = statistics.mean(
        (value - benchmark_mean) ** 2 for value in benchmark_values
    )
    if benchmark_variance == 0.0:
        return None

    covariance = statistics.mean(
        (portfolio_value - portfolio_mean) * (benchmark_value - benchmark_mean)
        for portfolio_value, benchmark_value in zip(portfolio_values, benchmark_values, strict=True)
    )
    return covariance / benchmark_variance


def compute_correlation_matrix(
    daily_prices: list[PriceLike],
    symbols: list[str],
    lookback_days: int = 252,
) -> dict[str, Any]:
    normalized_symbols = [symbol.upper() for symbol in symbols]
    return_maps = {
        symbol: _build_price_return_map(
            [price for price in daily_prices if str(getattr(price, "symbol", "")).upper() == symbol],
            lookback_days=lookback_days,
        )
        for symbol in normalized_symbols
    }

    matrix: list[list[float | None]] = []
    min_data_points: int | None = None
    for row_symbol in normalized_symbols:
        row: list[float | None] = []
        for column_symbol in normalized_symbols:
            if row_symbol == column_symbol and return_maps[row_symbol]:
                overlap_count = len(return_maps[row_symbol])
                min_data_points = overlap_count if min_data_points is None else min(min_data_points, overlap_count)
                row.append(1.0)
                continue

            correlation, overlap_count = _correlation_for_pair(
                return_maps[row_symbol],
                return_maps[column_symbol],
            )
            if correlation is not None:
                min_data_points = overlap_count if min_data_points is None else min(min_data_points, overlap_count)
            row.append(correlation)
        matrix.append(row)

    return {
        "symbols": normalized_symbols,
        "matrix": matrix,
        "lookback_days": lookback_days,
        "data_points_used": min_data_points or 0,
    }


def compute_stress_scenarios(
    portfolio_value: float,
    beta: float | None,
    scenarios: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    scenario_set = scenarios or [
        {"name": "Market Crash", "market_move": -0.20},
        {"name": "Correction", "market_move": -0.10},
        {"name": "Flash Crash", "market_move": -0.05},
        {"name": "Rally", "market_move": 0.10},
    ]
    beta_used = beta if beta is not None else 1.0
    beta_estimated = beta is None

    results: list[dict[str, Any]] = []
    for scenario in scenario_set:
        market_move = _to_float(scenario.get("market_move"))
        portfolio_move = market_move * beta_used
        dollar_impact = portfolio_value * portfolio_move
        results.append(
            {
                "scenario": str(scenario.get("name", "Scenario")),
                "market_move": market_move,
                "beta_used": beta_used,
                "beta_estimated": beta_estimated,
                "portfolio_move": portfolio_move,
                "dollar_impact": dollar_impact,
                "portfolio_value_after": portfolio_value + dollar_impact,
            }
        )

    return results


def compute_position_weights(positions: list[dict[str, Any]], nav: float) -> list[dict[str, Any]]:
    """Show how much of total portfolio value each live position contributes based on current NAV."""
    weighted_positions: list[dict[str, Any]] = []
    for position in positions:
        market_value = _to_float(position.get("market_value"))
        weighted_positions.append(
            {
                **position,
                "portfolio_weight": (market_value / nav) if nav else 0.0,
            }
        )

    return sorted(weighted_positions, key=lambda position: position["portfolio_weight"], reverse=True)


def compute_sector_exposure(
    positions: list[dict[str, Any]],
    ticker_map: dict[str, TickerLike],
    nav: float,
) -> list[dict[str, Any]]:
    """Aggregate live market value by sector so concentration risk can be measured across the portfolio."""
    return _compute_exposure(positions, ticker_map, nav, attribute="sector")


def compute_geographic_exposure(
    positions: list[dict[str, Any]],
    ticker_map: dict[str, TickerLike],
    nav: float,
) -> list[dict[str, Any]]:
    """Aggregate live market value by country so geographic concentration can be measured across the portfolio."""
    return _compute_exposure(positions, ticker_map, nav, attribute="country")


def _compute_exposure(
    positions: list[dict[str, Any]],
    ticker_map: dict[str, TickerLike],
    nav: float,
    *,
    attribute: str,
) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}

    for position in positions:
        symbol = str(position.get("symbol", "")).upper()
        ticker = ticker_map.get(symbol)
        bucket_name = getattr(ticker, attribute, None) if ticker else None
        bucket_name = bucket_name or "Unknown"
        market_value = _to_float(position.get("market_value"))

        bucket = buckets.setdefault(
            bucket_name,
            {
                "name": bucket_name,
                "total_market_value": 0.0,
                "weight": 0.0,
                "position_count": 0,
            },
        )
        bucket["total_market_value"] += market_value
        bucket["position_count"] += 1

    for bucket in buckets.values():
        bucket["weight"] = (bucket["total_market_value"] / nav) if nav else 0.0

    return sorted(buckets.values(), key=lambda bucket: bucket["weight"], reverse=True)


def _build_price_return_map(
    prices: list[PriceLike],
    lookback_days: int | None = None,
) -> dict[date, float]:
    ordered_prices = sorted(prices, key=lambda price: price.date)
    if lookback_days is not None:
        ordered_prices = ordered_prices[-(lookback_days + 1) :]

    returns: dict[date, float] = {}
    for previous_price, current_price in zip(ordered_prices, ordered_prices[1:], strict=False):
        previous_close = _to_float(previous_price.close)
        current_close = _to_float(current_price.close)
        if previous_close == 0.0:
            continue
        returns[current_price.date] = (current_close - previous_close) / previous_close

    return returns


def _correlation_for_pair(
    left_returns: dict[date, float],
    right_returns: dict[date, float],
) -> tuple[float | None, int]:
    overlapping_dates = sorted(set(left_returns) & set(right_returns))
    overlap_count = len(overlapping_dates)
    if overlap_count < 20:
        return None, overlap_count

    left_values = [left_returns[day] for day in overlapping_dates]
    right_values = [right_returns[day] for day in overlapping_dates]

    if len(set(left_values)) == 1 or len(set(right_values)) == 1:
        return None, overlap_count

    try:
        return statistics.correlation(left_values, right_values), overlap_count
    except statistics.StatisticsError:
        return None, overlap_count

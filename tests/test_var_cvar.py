from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pytest

from glassbox.services.risk import compute_risk_metrics


@dataclass
class Snapshot:
    date: date
    equity_value: float
    cash_balance: float = 0.0
    long_market_value: float = 0.0


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


def test_var_and_cvar_match_known_series() -> None:
    daily_returns = [
        -0.10,
        -0.08,
        -0.06,
        -0.04,
        -0.03,
        -0.02,
        -0.01,
        -0.005,
        0.0,
        0.005,
        0.01,
        0.012,
        0.014,
        0.016,
        0.018,
        0.02,
        0.022,
        0.024,
        0.026,
        0.028,
    ]

    metrics = compute_risk_metrics(build_snapshots(daily_returns))

    assert metrics["var_95"] == pytest.approx(0.08)
    assert metrics["var_99"] == pytest.approx(0.10)
    assert metrics["cvar_95"] == pytest.approx(0.09)


def test_var_and_cvar_are_none_with_fewer_than_two_snapshots() -> None:
    metrics = compute_risk_metrics([Snapshot(date=date(2024, 1, 1), equity_value=100.0)])

    assert metrics["var_95"] is None
    assert metrics["var_99"] is None
    assert metrics["cvar_95"] is None


def test_var_and_cvar_are_none_with_fewer_than_twenty_returns() -> None:
    daily_returns = [-0.02, 0.01, -0.01, 0.015, 0.0] * 3
    metrics = compute_risk_metrics(build_snapshots(daily_returns))

    assert len(daily_returns) == 15
    assert metrics["var_95"] is None
    assert metrics["var_99"] is None
    assert metrics["cvar_95"] is None


def test_cvar_is_greater_than_or_equal_to_var() -> None:
    daily_returns = [
        -0.12,
        -0.09,
        -0.07,
        -0.05,
        -0.04,
        -0.03,
        -0.02,
        -0.01,
        -0.005,
        0.0,
        0.004,
        0.006,
        0.008,
        0.01,
        0.012,
        0.014,
        0.016,
        0.018,
        0.02,
        0.022,
    ]

    metrics = compute_risk_metrics(build_snapshots(daily_returns))

    assert metrics["cvar_95"] is not None
    assert metrics["var_95"] is not None
    assert metrics["cvar_95"] >= metrics["var_95"]

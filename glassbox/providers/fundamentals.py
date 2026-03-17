from __future__ import annotations

import logging
from typing import Any, Protocol

try:
    import yfinance as yf
except ImportError as exc:  # pragma: no cover - import guard for local dev bootstrapping
    yf = None
    _yfinance_import_error = exc
else:
    _yfinance_import_error = None


logger = logging.getLogger(__name__)

_CANONICAL_KEYS = [
    "company_name",
    "sector",
    "industry",
    "country",
    "beta",
    "market_cap",
    "trailing_pe",
    "forward_pe",
    "price_to_book",
    "ev_to_ebitda",
    "ev_to_revenue",
    "peg_ratio",
    "price_to_sales",
    "profit_margin",
    "gross_margin",
    "operating_margin",
    "ebitda_margin",
    "return_on_equity",
    "return_on_assets",
    "target_high_price",
    "target_low_price",
    "target_mean_price",
    "target_median_price",
    "analyst_count",
    "recommendation_key",
    "recommendation_mean",
]

_YFINANCE_KEY_MAP = {
    "company_name": "shortName",
    "sector": "sector",
    "industry": "industry",
    "country": "country",
    "beta": "beta",
    "market_cap": "marketCap",
    "trailing_pe": "trailingPE",
    "forward_pe": "forwardPE",
    "price_to_book": "priceToBook",
    "ev_to_ebitda": "enterpriseToEbitda",
    "ev_to_revenue": "enterpriseToRevenue",
    "peg_ratio": "pegRatio",
    "price_to_sales": "priceToSalesTrailing12Months",
    "profit_margin": "profitMargins",
    "gross_margin": "grossMargins",
    "operating_margin": "operatingMargins",
    "ebitda_margin": "ebitdaMargins",
    "return_on_equity": "returnOnEquity",
    "return_on_assets": "returnOnAssets",
    "target_high_price": "targetHighPrice",
    "target_low_price": "targetLowPrice",
    "target_mean_price": "targetMeanPrice",
    "target_median_price": "targetMedianPrice",
    "analyst_count": "numberOfAnalystOpinions",
    "recommendation_key": "recommendationKey",
    "recommendation_mean": "recommendationMean",
}

_STRING_KEYS = {"company_name", "sector", "industry", "country", "recommendation_key"}
_INTEGER_KEYS = {"analyst_count"}


class FundamentalsProvider(Protocol):
    def fetch_fundamentals(self, symbol: str) -> dict[str, Any] | None:
        """Fetch fundamentals for a single ticker. Returns None if the symbol is not found or the provider errors."""


class YFinanceFundamentalsProvider:
    def fetch_fundamentals(self, symbol: str) -> dict[str, Any] | None:
        if yf is None:
            logger.warning(
                "yfinance is not installed; fundamentals fetch for %s is unavailable",
                symbol.upper(),
            )
            return None

        try:
            info = yf.Ticker(symbol.upper()).info
        except Exception as exc:
            logger.warning("Failed to fetch fundamentals for %s: %s", symbol.upper(), exc)
            return None

        if not isinstance(info, dict) or not info:
            return None

        fundamentals: dict[str, Any] = {key: None for key in _CANONICAL_KEYS}
        for canonical_key, provider_key in _YFINANCE_KEY_MAP.items():
            raw_value = info.get(provider_key)
            if canonical_key in _STRING_KEYS:
                fundamentals[canonical_key] = raw_value if isinstance(raw_value, str) else None
            elif canonical_key in _INTEGER_KEYS:
                fundamentals[canonical_key] = _to_int(raw_value)
            else:
                fundamentals[canonical_key] = _to_float(raw_value)

        return fundamentals


def get_fundamentals_provider() -> FundamentalsProvider:
    return YFinanceFundamentalsProvider()


def _to_float(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

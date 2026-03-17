from __future__ import annotations

from fastapi import HTTPException


def alpaca_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=502, detail=f"Alpaca error: {exc}")


def database_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=500, detail=f"Database error: {exc}")


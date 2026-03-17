from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from glassbox.api.dependencies import DbSession, get_alpaca_client
from glassbox.schemas.trading import HealthResponse


router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health_check(db: DbSession) -> HealthResponse:
    """Report local database and Alpaca connectivity."""
    db_connected = False
    alpaca_connected = False

    try:
        db.execute(text("SELECT 1"))
        db_connected = True
    except Exception:
        db_connected = False

    try:
        get_alpaca_client().get_account()
        alpaca_connected = True
    except Exception:
        alpaca_connected = False

    status = "ok" if db_connected and alpaca_connected else "degraded"
    return HealthResponse(
        status=status,
        db_connected=db_connected,
        alpaca_connected=alpaca_connected,
    )


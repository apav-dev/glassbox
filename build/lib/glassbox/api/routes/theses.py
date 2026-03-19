from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError

from glassbox.api.dependencies import DbSession, get_alpaca_client
from glassbox.api.errors import alpaca_error, database_error
from glassbox.api.helpers import load_fundamentals_map, optional_float
from glassbox.schemas.theses import ThesisCreate, ThesisResponse, ThesisWithFundamentals
from glassbox.services.risk import compute_position_weights
from glassbox.services.theses import (
    create_thesis,
    get_current_theses,
    get_current_thesis,
    get_thesis_history,
    update_thesis_status,
)


router = APIRouter(tags=["theses"])


@router.get("/theses", response_model=list[ThesisResponse])
def list_theses(
    db: DbSession,
    status: str | None = Query(default=None, pattern=r"^(active|closed|watching)$"),
    symbol: str | None = Query(default=None, min_length=1, max_length=16, pattern=r"^[A-Z][A-Z0-9.\-]{0,15}$"),
) -> list[ThesisResponse]:
    """Return the current thesis rows, optionally filtered by status or symbol."""
    try:
        records = get_current_theses(db, symbols=[symbol] if symbol else None, status=status)
    except SQLAlchemyError as exc:
        raise database_error(exc) from exc

    return [ThesisResponse.model_validate(record) for record in records]


@router.post("/theses", response_model=ThesisResponse)
def create_thesis_record(payload: ThesisCreate, db: DbSession) -> ThesisResponse:
    """Create a new thesis version for a symbol."""
    try:
        record = create_thesis(
            db,
            symbol=payload.symbol,
            thesis=payload.thesis,
            catalyst=payload.catalyst,
            edge=payload.edge,
            status=payload.status,
            created_by=payload.created_by,
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise database_error(exc) from exc

    return ThesisResponse.model_validate(record)


@router.get("/theses/enriched", response_model=list[ThesisWithFundamentals])
def list_enriched_theses(db: DbSession) -> list[ThesisWithFundamentals]:
    """Return current active theses with live portfolio weights and stored fundamentals."""
    try:
        thesis_rows = get_current_theses(db, status="active")
    except SQLAlchemyError as exc:
        raise database_error(exc) from exc

    if not thesis_rows:
        return []

    symbols = [row.symbol for row in thesis_rows]
    try:
        fundamentals_map = load_fundamentals_map(db, symbols)
        positions = get_alpaca_client().list_positions()
        summary = get_alpaca_client().get_account_summary()
    except SQLAlchemyError as exc:
        raise database_error(exc) from exc
    except Exception as exc:
        raise alpaca_error(exc) from exc

    weighted_positions = compute_position_weights(positions, nav=float(summary["nav"]))
    portfolio_weight_map = {
        str(position["symbol"]).upper(): float(position["portfolio_weight"])
        for position in weighted_positions
    }

    enriched_rows: list[ThesisWithFundamentals] = []
    for row in thesis_rows:
        fundamentals = fundamentals_map.get(row.symbol)
        enriched_rows.append(
            ThesisWithFundamentals(
                id=row.id,
                symbol=row.symbol,
                version=row.version,
                thesis=row.thesis,
                catalyst=row.catalyst,
                edge=row.edge,
                status=row.status,
                created_at=row.created_at,
                created_by=row.created_by,
                portfolio_weight=portfolio_weight_map.get(row.symbol, 0.0),
                trailing_pe=optional_float(fundamentals.trailing_pe if fundamentals else None),
                target_mean_price=optional_float(
                    fundamentals.target_mean_price if fundamentals else None
                ),
                recommendation_key=fundamentals.recommendation_key if fundamentals else None,
            )
        )

    return enriched_rows


@router.get("/theses/{symbol}/history", response_model=list[ThesisResponse])
def get_thesis_versions(symbol: str, db: DbSession) -> list[ThesisResponse]:
    """Return all thesis versions for a symbol in ascending version order."""
    try:
        records = get_thesis_history(db, symbol)
    except SQLAlchemyError as exc:
        raise database_error(exc) from exc

    return [ThesisResponse.model_validate(record) for record in records]


@router.get("/theses/{symbol}", response_model=ThesisResponse)
def get_symbol_thesis(symbol: str, db: DbSession) -> ThesisResponse:
    """Return the current thesis for a symbol."""
    try:
        record = get_current_thesis(db, symbol)
    except SQLAlchemyError as exc:
        raise database_error(exc) from exc

    if record is None:
        raise HTTPException(status_code=404, detail=f"No thesis found for {symbol.upper()}")

    return ThesisResponse.model_validate(record)


@router.patch("/theses/{symbol}/status", response_model=ThesisResponse)
def patch_thesis_status(
    symbol: str,
    db: DbSession,
    status: str = Body(..., embed=True, pattern=r"^(active|closed|watching)$"),
) -> ThesisResponse:
    """Update the status of the current thesis version for a symbol."""
    try:
        record = update_thesis_status(db, symbol, status)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise database_error(exc) from exc

    if record is None:
        raise HTTPException(status_code=404, detail=f"No thesis found for {symbol.upper()}")

    return ThesisResponse.model_validate(record)


from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import SQLAlchemyError

from glassbox.api.dependencies import DbSession
from glassbox.api.errors import database_error
from glassbox.schemas.tags import BulkTagRequest, TagCreate, TagResponse, TagTickerRequest, TickerTagResponse
from glassbox.services.tags import (
    bulk_tag_ticker,
    create_tag,
    get_tag_tickers,
    get_ticker_tags,
    list_tags,
    tag_ticker,
    untag_ticker,
)


router = APIRouter(tags=["tags"])


@router.get("/tags", response_model=list[TagResponse])
def get_tags(db: DbSession) -> list[TagResponse]:
    """List all local thematic tags."""
    try:
        rows = list_tags(db)
    except SQLAlchemyError as exc:
        raise database_error(exc) from exc

    return [TagResponse.model_validate(row) for row in rows]


@router.post("/tags", response_model=TagResponse)
def create_tag_record(payload: TagCreate, db: DbSession) -> TagResponse:
    """Create a thematic tag if it does not already exist."""
    try:
        row = create_tag(db, name=payload.name, color=payload.color, description=payload.description)
    except SQLAlchemyError as exc:
        db.rollback()
        raise database_error(exc) from exc

    return TagResponse.model_validate(row)


@router.get("/tags/{tag_name}/tickers", response_model=list[str])
def list_tagged_tickers(tag_name: str, db: DbSession) -> list[str]:
    """Return all symbols assigned to a thematic tag."""
    try:
        return get_tag_tickers(db, tag_name)
    except SQLAlchemyError as exc:
        raise database_error(exc) from exc


@router.get("/tickers/{symbol}/tags", response_model=list[TagResponse])
def list_ticker_tags(symbol: str, db: DbSession) -> list[TagResponse]:
    """Return all thematic tags assigned to a ticker."""
    try:
        rows = get_ticker_tags(db, symbol)
    except SQLAlchemyError as exc:
        raise database_error(exc) from exc

    return [TagResponse.model_validate(row) for row in rows]


@router.post("/tickers/{symbol}/tags", response_model=TickerTagResponse)
def create_ticker_tag(symbol: str, payload: TagTickerRequest, db: DbSession) -> TickerTagResponse:
    """Apply a thematic tag to a ticker."""
    if payload.symbol.upper() != symbol.upper():
        raise HTTPException(status_code=400, detail="Path symbol must match payload symbol")

    try:
        row = tag_ticker(db, symbol=symbol, tag_name=payload.tag_name, tagged_by=payload.tagged_by)
    except SQLAlchemyError as exc:
        db.rollback()
        raise database_error(exc) from exc

    return TickerTagResponse.model_validate(row)


@router.post("/tickers/{symbol}/tags/bulk", response_model=list[TickerTagResponse])
def create_ticker_tags_bulk(
    symbol: str,
    payload: BulkTagRequest,
    db: DbSession,
) -> list[TickerTagResponse]:
    """Apply multiple thematic tags to a ticker."""
    if payload.symbol.upper() != symbol.upper():
        raise HTTPException(status_code=400, detail="Path symbol must match payload symbol")

    try:
        rows = bulk_tag_ticker(db, symbol=symbol, tag_names=payload.tag_names, tagged_by=payload.tagged_by)
    except SQLAlchemyError as exc:
        db.rollback()
        raise database_error(exc) from exc

    return [TickerTagResponse.model_validate(row) for row in rows]


@router.delete("/tickers/{symbol}/tags/{tag_name}")
def delete_ticker_tag(symbol: str, tag_name: str, db: DbSession) -> dict[str, bool]:
    """Remove a thematic tag from a ticker."""
    try:
        removed = untag_ticker(db, symbol=symbol, tag_name=tag_name)
    except SQLAlchemyError as exc:
        db.rollback()
        raise database_error(exc) from exc

    return {"removed": removed}


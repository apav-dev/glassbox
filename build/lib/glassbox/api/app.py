from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from glassbox.api.routes.health import router as health_router
from glassbox.api.routes.portfolio import router as portfolio_router
from glassbox.api.routes.tags import router as tags_router
from glassbox.api.routes.theses import router as theses_router
from glassbox.api.routes.trades import router as trades_router
from glassbox.db.models import init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Glass Box Trading API",
    description=(
        "Backend for an AI day-trading agent. Combines Alpaca execution with "
        "local analytics persistence."
    ),
    lifespan=lifespan,
)

app.include_router(health_router)
app.include_router(portfolio_router)
app.include_router(trades_router)
app.include_router(tags_router)
app.include_router(theses_router)


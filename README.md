# Glass Box Trading Backend

`glassbox` is a FastAPI backend and MCP wrapper for an AI-assisted trading workflow. It uses Alpaca as the live brokerage source of truth, stores agent-specific metadata in a local SQLAlchemy database, and layers on portfolio analytics, fundamentals, tagging, thesis management, cost basis tracking, and reconstructed position history.

This repo currently includes:

- A packaged Python codebase under `glassbox/`
- Thin root entrypoints in `main.py` and `mcp_server.py`
- SQLAlchemy models and database bootstrap logic under `glassbox/db/`
- Alpaca integration under `glassbox/clients/`
- Local services for risk, prices, fundamentals, tags, theses, cost basis, and position history under `glassbox/services/`
- A focused pytest suite under `tests/`

## What Lives Where

- `main.py`: thin compatibility wrapper that exposes `app`
- `mcp_server.py`: thin compatibility wrapper that runs the MCP server
- `glassbox/api/app.py`: FastAPI app shell, lifespan wiring, and router registration
- `glassbox/api/http.py`: compatibility shim that re-exports `app`
- `glassbox/api/mcp.py`: MCP tool layer that wraps the FastAPI service over HTTP
- `glassbox/api/routes/`: feature routers for health, portfolio, trades, tags, and theses
- `glassbox/db/models.py`: ORM models, DB URL resolution, engine/session creation, schema bootstrap
- `glassbox/clients/alpaca.py`: account/positions/orders/snapshots/daily bars via Alpaca
- `glassbox/providers/fundamentals.py`: fundamentals provider abstraction, currently backed by `yfinance`
- `glassbox/services/`: business logic modules for prices, risk, fundamentals, tags, theses, cost basis, and position history
- `glassbox/schemas/`: Pydantic request/response models grouped by domain
- `tests/`: unit tests by feature area

Current top-level layout:

```text
.
├── glassbox/
│   ├── api/
│   │   └── routes/
│   ├── clients/
│   ├── db/
│   ├── providers/
│   ├── schemas/
│   └── services/
├── tests/
├── main.py
├── mcp_server.py
├── README.md
└── requirements.txt
```

## Architecture

There are three data domains in this service:

- Alpaca: live account state, live positions, order execution, and historical daily bars used for local price backfill
- Local database: trades, daily snapshots, daily prices, tags, theses, fundamentals cache, and realized P&L
- yfinance: ticker fundamentals enrichment only

In practice:

- Live portfolio reads like `/portfolio/summary` and `/portfolio/positions` hit Alpaca directly.
- Local analytics like `/portfolio/equity-curve`, `/portfolio/cost-basis`, and `/portfolio/history/positions` read the local database.
- Hybrid analytics like `/portfolio/risk/report` combine local history with live Alpaca state.
- Metadata flows like tags and theses are fully local.

## Source Of Truth Rules

These are the important rules for new devs and agents:

- Alpaca is the source of truth for current account state and order execution.
- The local `trades` table is the source of truth for FIFO cost basis, realized P&L, and reconstructed position history.
- `/sync` does not import historical fills into `trades`; it only updates snapshots, tickers, prices, and fundamentals inputs.
- If trades are executed outside this service, local ledger-based analytics can become incomplete or wrong until that trade history is written locally.
- Local fundamentals are a cache. They may be missing or stale until refreshed.

## Database Model

The app creates tables automatically on startup via `init_db()` in `glassbox/db/models.py`. There is no Alembic migration stack in this repo right now.

Current tables:

- `trades`: local trade ledger with thesis text and optional strategy tag
- `daily_snapshots`: one row per sync day for account-level NAV/cash/drawdown history
- `daily_prices`: local daily closes and volume by symbol, used for beta/correlation analytics
- `tickers`: local reference metadata like company name, sector, industry, country, beta
- `tags`: local thematic tags
- `ticker_tags`: many-to-many ticker/tag assignments
- `ticker_fundamentals`: cached fundamentals and analyst fields
- `investment_theses`: versioned thesis history per symbol
- `realized_pnl`: lot-matched realized P&L rows derived from the local trade ledger

Two schema notes matter:

- Missing tables are created automatically at startup.
- `init_db()` includes a narrow additive compatibility patch for `tickers.country`, but this is not a general migration system.

## Configuration

The app reads environment variables directly. There is no built-in `.env` loader, so if you use a `.env` file you need to source it yourself or have your process manager do it.

### Alpaca

Required for live account access, sync, order execution, and price backfills:

```bash
export ALPACA_API_KEY="your-key"
export ALPACA_API_SECRET="your-secret"
```

Optional:

```bash
export ALPACA_BASE_URL="https://paper-api.alpaca.markets"
export ALPACA_API_VERSION="v2"
```

### Database

Resolution order in `glassbox/db/models.py`:

1. `DATABASE_URL`
2. `SQLALCHEMY_DATABASE_URL`
3. `SQLALCHEMY_DATABASE_URI`
4. `POSTGRES_HOST` + `POSTGRES_PORT` + `POSTGRES_DB` + `POSTGRES_USER` + `POSTGRES_PASSWORD`
5. SQLite fallback at `./glassbox.db`

Examples:

```bash
# Default local SQLite file
export SQLITE_PATH="./glassbox.db"
```

```bash
# Postgres
export DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:5432/glassbox"
```

`postgres://...` and plain `postgresql://...` URLs are normalized automatically to `postgresql+psycopg://...`.

### MCP Server

Used by `mcp_server.py`:

```bash
export GLASSBOX_BASE_URL="http://localhost:8000"
export MCP_PORT="8100"
export GLASSBOX_TIMEOUT_SECONDS="30"
```

## Local Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest
```

`pytest` is used by the test suite but is not currently listed in `requirements.txt`, so install it separately for local development.

## Running The Services

### FastAPI

```bash
uvicorn main:app --reload
```

Equivalent direct package path:

```bash
uvicorn glassbox.api.http:app --reload
```

Once running:

- OpenAPI docs: `http://localhost:8000/docs`
- Alternate docs: `http://localhost:8000/redoc`

The app will start without immediately validating Alpaca credentials, but any route that touches Alpaca will fail until those env vars are set. `/health` will report `degraded` if either the DB or Alpaca is unavailable.

### MCP

Run in a second terminal after the FastAPI service is up:

```bash
python mcp_server.py
```

The MCP server uses the streamable HTTP transport and forwards requests to the FastAPI backend defined by `GLASSBOX_BASE_URL`.

## Core Runtime Flows

### `/sync`

`POST /sync` does the following:

1. Pulls the latest account snapshot and live positions from Alpaca
2. Upserts the current day's `daily_snapshots` row
3. Ensures every live position symbol exists in `tickers`
4. Backfills local `daily_prices` for all tracked symbols plus `SPY`
5. Refreshes fundamentals for newly seen or stale symbols

Use `/sync` before portfolio analytics if you want fresh local history and enrichment data.

### `/trade/execute`

`POST /trade/execute`:

1. Submits the order to Alpaca
2. Writes a local `trades` row with thesis text and optional strategy tag
3. Auto-creates a bare `tickers` row if needed
4. For sells, writes lot-matched realized P&L rows
5. For sells, auto-closes the current thesis if the symbol is no longer in live Alpaca positions
6. Kicks off background price and fundamentals refresh for the traded symbol

Important: ledger-based analytics depend on the local `trades` row, not Alpaca order history.

## API Overview

The complete contract lives in `/docs`, but this is the practical route map.

### Health, Account, And Trading

- `GET /health`: database and Alpaca connectivity status
- `GET /portfolio/summary`: live account NAV, equity, cash, buying power, day P&L
- `GET /portfolio/positions`: live Alpaca positions enriched with local ticker and fundamentals data
- `GET /portfolio/positions/weighted`: same positions plus portfolio weights
- `POST /trade/execute`: submit an order and write the local trade ledger entry
- `POST /sync`: refresh local snapshots, tickers, prices, and fundamentals inputs
- `GET /trades`: local trade ledger, filterable by `ticker` and `strategy_tag`

### Portfolio Analytics

- `GET /portfolio/equity-curve`: local NAV history derived from `daily_snapshots`
- `GET /portfolio/risk`: return/risk metrics from `daily_snapshots`, optional `risk_free_rate`
- `GET /portfolio/risk/report`: combined risk metrics, sector exposure, geographic exposure
- `GET /portfolio/correlation`: correlation matrix from local `daily_prices`, optional `lookback_days`
- `GET /portfolio/stress-test`: scenario analysis using current NAV and estimated beta
- `GET /portfolio/exposure/sector`: live position exposure grouped by sector
- `GET /portfolio/exposure/geographic`: live position exposure grouped by country
- `GET /portfolio/exposure/thematic`: live position exposure grouped by local tags

### Fundamentals

- `GET /portfolio/fundamentals`: stored fundamentals for currently held symbols
- `GET /portfolio/fundamentals/{symbol}`: stored fundamentals for one symbol
- `POST /portfolio/fundamentals/refresh`: refresh one symbol or the full ticker universe, optional `symbol` and `stale_only`

### Cost Basis And Realized P&L

- `GET /portfolio/cost-basis`: FIFO lots, average cost, total cost basis, realized P&L by symbol
- `GET /portfolio/realized-pnl`: realized P&L summary, optional `ticker` and `since`
- `GET /portfolio/realized-pnl/details`: lot-level realized P&L records, optional filters plus `limit` and `offset`
- `POST /portfolio/realized-pnl/rebuild`: rebuild the `realized_pnl` table from `trades`

### Position History Reconstruction

- `GET /portfolio/history/positions`: reconstructed daily end-of-day positions and cash for a date range
- `GET /portfolio/history/positions/{symbol}`: reconstructed daily share count for one symbol
- `GET /portfolio/history/positions/{symbol}/at`: reconstructed share count for one symbol on one date

The history endpoints use the local trade ledger and currently reject ranges longer than 365 days.

### Tags

- `GET /tags`: list all local tags
- `POST /tags`: create a tag
- `GET /tags/{tag_name}/tickers`: list all symbols assigned to a tag
- `GET /tickers/{symbol}/tags`: list all tags on a symbol
- `POST /tickers/{symbol}/tags`: apply one tag to a symbol
- `POST /tickers/{symbol}/tags/bulk`: apply multiple tags to a symbol
- `DELETE /tickers/{symbol}/tags/{tag_name}`: remove a tag from a symbol

Tagging is idempotent and will auto-create the ticker row if it does not exist yet.

### Theses

- `GET /theses`: current thesis rows, filterable by `status` and `symbol`
- `POST /theses`: create a new thesis version for a symbol
- `GET /theses/enriched`: current active theses enriched with live portfolio weights and stored fundamentals
- `GET /theses/{symbol}`: current thesis for a symbol
- `GET /theses/{symbol}/history`: full thesis version history for a symbol
- `PATCH /theses/{symbol}/status`: update the current thesis status

Thesis versions are append-only per symbol and statuses are limited to `active`, `closed`, and `watching`.

## MCP Tooling

`mcp_server.py` exposes the backend through MCP tools that mirror the REST API closely. The tool set covers:

- health and sync
- account summary and positions
- trade execution and trade history
- risk, exposures, correlation, stress testing
- cost basis and realized P&L
- fundamentals refresh and lookup
- tags and ticker classification
- theses and thesis history
- reconstructed position history

The MCP server does not implement separate business logic. It is a thin HTTP wrapper around the FastAPI service and returns backend JSON directly. On backend or transport errors it returns structured error payloads with `error`, `status_code`, and `detail`.

## Testing

Run the full suite with:

```bash
pytest -q
```

Current test coverage focuses on:

- risk metrics, VaR/CVaR, beta, correlation, and stress scenarios
- price backfills and SPY inclusion
- fundamentals fetch/refresh behavior
- FIFO cost basis and realized P&L matching
- thematic tags and exposure aggregation
- thesis versioning and status flows
- reconstructed historical positions and cash

Most tests use in-memory SQLite and monkeypatched providers, so they do not require live Alpaca credentials.

## Operational Notes And Limitations

- There is no auth layer in this repo right now.
- There is no migration framework; schema management is still lightweight and code-driven.
- There is no Docker or Compose config checked in right now.
- A number of analytics depend on local history being populated first. If `daily_snapshots` or `daily_prices` are sparse, risk/beta/correlation outputs will also be sparse.
- `SPY` price history is backfilled automatically because it is the benchmark for beta calculations.
- Fundamentals refresh uses `yfinance`, so failures there degrade enrichment only; they do not block the rest of the app.
- Alpaca upstream failures are surfaced as HTTP 502s from the API layer. Database failures are surfaced as HTTP 500s.

## Recommended First Steps For A New Dev Or Agent

1. Install dependencies and start the FastAPI app.
2. Open `/docs` and inspect the live schema.
3. Set Alpaca paper credentials and call `POST /sync`.
4. Use `/portfolio/summary`, `/portfolio/positions`, and `/portfolio/risk/report` to confirm the live-plus-local flow works.
5. If you need MCP integration, start `mcp_server.py` and point your client at it.
6. If you are working on analytics, remember which features depend on `daily_snapshots`, `daily_prices`, or local `trades` rather than Alpaca directly.

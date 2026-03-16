# Glass Box Trading Backend

`glassbox` is a FastAPI-based backend for a day-trading AI agent. It replaces an Excel portfolio tracker with a container-ready Python service that can place paper trades through Alpaca, persist local trading metadata, and calculate portfolio risk metrics on demand.

This initial scaffold provides:

- `models.py` for the SQLAlchemy ORM models and dual-mode database bootstrap
- `alpaca_client.py` for Alpaca account, position, order, and snapshot access
- `README.md` for the system-level setup and API contract

## System Architecture

The service is designed around two sources of truth:

- **Alpaca** is the live execution source of truth for cash, buying power, open positions, orders, and portfolio history.
- **The local database** is the historical analytics source of truth for agent-specific metadata that Alpaca does not store cleanly, such as strategy tags, investment thesis text, ticker fundamentals, and daily NAV snapshots.

Planned runtime flow:

1. A FastAPI endpoint receives an agent request.
2. For reads like `/portfolio/summary`, the app queries Alpaca directly.
3. For writes like `/trade/execute`, the app submits the order to Alpaca and stores trade metadata locally.
4. For analytics like `/portfolio/risk`, the app combines Alpaca state with local tables such as `daily_snapshots` and `tickers`.
5. A `/sync` endpoint reconciles Alpaca positions and account state back into the local database.

## Database Schema

### `trades`

Stores agent-generated trade metadata and execution identifiers.

- `trade_id`: unique trade or order identifier
- `timestamp`: trade timestamp
- `ticker`: asset symbol
- `action`: `BUY` or `SELL`
- `qty`: share quantity
- `price`: executed or intended price
- `commission`: execution cost
- `strategy_tag`: optional strategy label
- `investment_thesis`: free-text reasoning from the agent

### `daily_snapshots`

Stores account-level portfolio snapshots for local NAV and drawdown tracking.

- `date`: snapshot date
- `equity_value`: total account equity
- `cash_balance`: available cash
- `long_market_value`: gross long exposure
- `total_drawdown`: max drawdown fraction for the measured lookback window

### `tickers`

Stores reference metadata used for enrichment and sector-risk calculations.

- `symbol`: ticker symbol
- `company_name`: company name
- `sector`: sector label
- `industry`: industry label
- `beta`: optional beta value

## Environment Setup

### Required Alpaca variables

Set these before calling the Alpaca client:

```bash
export ALPACA_API_KEY="your-key"
export ALPACA_API_SECRET="your-secret"
export ALPACA_BASE_URL="https://paper-api.alpaca.markets"
```

Optional:

```bash
export ALPACA_API_VERSION="v2"
```

### Database switching: SQLite vs Postgres

The ORM bootstrap in `models.py` follows this order:

1. `DATABASE_URL`
2. `SQLALCHEMY_DATABASE_URL` or `SQLALCHEMY_DATABASE_URI`
3. `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`
4. Fallback to SQLite at `./glassbox.db`

Examples:

```bash
# Local development with SQLite
export SQLITE_PATH="./glassbox.db"
```

```bash
# Cloud or container deployment with Postgres
export DATABASE_URL="postgresql+psycopg://postgres:postgres@db:5432/glassbox"
```

## How To Run

The FastAPI entrypoint and container files are the next pieces to add. Once they exist, the intended run modes are:

### Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install fastapi uvicorn sqlalchemy psycopg alpaca-trade-api
uvicorn main:app --reload
```

### Docker Compose

```bash
docker-compose up --build
```

The expected Compose setup is:

- `api` service for the FastAPI app
- `db` service for PostgreSQL in cloud-like deployments
- environment variables passed into the API container for both DB and Alpaca access

## API Reference

These are the core agent-facing endpoints the service is being built around:

### `GET /portfolio/summary`

Returns current account NAV, P&L, cash, and buying power from Alpaca.

### `GET /portfolio/risk`

Returns risk metrics such as:

- Sharpe ratio
- max drawdown
- sector exposure

This endpoint will combine Alpaca positions with local snapshot and ticker metadata.

### `POST /trade/execute`

Accepts:

- `ticker`
- `side`
- `qty`
- `thesis`

Behavior:

1. validates the request
2. submits the order to Alpaca
3. logs agent metadata into `trades`
4. returns order status and stored trade metadata

### `POST /sync`

Maintenance endpoint that:

- refreshes account state from Alpaca
- reconciles open positions
- writes an updated `daily_snapshots` row
- prepares the local database for downstream analytics

## Next Build Steps

The next implementation pass should add:

- `main.py` with the FastAPI app and route handlers
- request and response schemas
- risk calculation utilities for Sharpe ratio and sector exposure
- `Dockerfile` and `docker-compose.yml`
- migrations, ideally with Alembic

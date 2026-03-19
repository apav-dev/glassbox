# Glassbox MCP Server — End-to-End Test Plan

This plan validates the Glassbox MCP server connected to Cursor using your Paper trading account. Paper accounts can be reset at any time, so live trade execution is acceptable.

---

## Prerequisites

- [ ] **Environment**: `.env` with `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` (Paper keys recommended)
- [ ] **Paper mode**: Default is `paper=true`. Explicitly set `ALPACA_PAPER=true` if desired.
- [ ] **Database**: SQLite at `./glassbox.db` (created automatically) or Postgres via `DATABASE_URL`
- [ ] **MCP connection**: Glassbox MCP server added to Cursor (stdio transport via `glassbox-mcp-server serve`)
- [ ] **Dependencies**: `pip install -e .` from repo root

---

## Test Phases

### Phase 1: Connectivity & Health

| Step | Tool | Expected Result | Notes |
|------|------|-----------------|-------|
| 1.1 | `health_check` | `status: "ok"`, `db_connected: true`, `alpaca_connected: true` | If degraded, check `.env` and DB path |
| 1.2 | `get_portfolio_summary` | Object with `nav`, `equity`, `cash`, `buying_power`, `day_pnl` | Confirms Alpaca account is reachable |

**Failure signals**: DB not writable, Alpaca credentials invalid, or network issues.

---

### Phase 2: Sync & Portfolio

| Step | Tool | Expected Result | Notes |
|------|------|-----------------|-------|
| 2.1 | `sync_portfolio` | `snapshot_date`, `equity_value`, `positions_count`, `new_tickers_added` | May take a while if many positions; backfills prices and fundamentals |
| 2.2 | `get_portfolio_summary` | Same as 1.2; values should match sync | |
| 2.3 | `list_positions` | Array of positions (or empty) | Each has `symbol`, `qty`, `market_value`, etc. |
| 2.4 | `list_weighted_positions` | Same positions with `portfolio_weight` | Weights sum to ~1.0 for long-only |

**Failure signals**: Alpaca `build_daily_snapshot` or `list_positions` failing; DB write errors during sync.

---

### Phase 3: Trade Execution (Critical Path)

Use a liquid ticker (e.g. AAPL, SPY) and small size (1 share) to minimize impact.

| Step | Tool | Arguments | Expected Result | Notes |
|------|------|-----------|-----------------|-------|
| 3.1 | `execute_trade` | `ticker="AAPL"`, `side="buy"`, `qty=1`, `thesis="E2E test: cash flow durability thesis"` | `status` accepted/filled, `order_id`, `local_trade_id`, `thesis_recorded: true`, `thesis_action: "created"` | Paper orders often fill immediately |
| 3.2 | `list_positions` | — | AAPL appears with `qty: 1` | May need 1–2 seconds for Alpaca to reflect |
| 3.3 | `list_trades` | `ticker="AAPL"` | One trade with matching thesis | |
| 3.4 | `get_thesis` | `symbol="AAPL"` | Thesis with `status: "active"`, matching text | |
| 3.5 | `get_cost_basis` | — | AAPL entry with `total_qty: 1`, `avg_cost` > 0 | |
| 3.6 | `execute_trade` | `ticker="AAPL"`, `side="sell"`, `qty=1`, `thesis="E2E test: closing position"` | Order accepted/filled | Full exit |
| 3.7 | `list_positions` | — | AAPL no longer present | |
| 3.8 | `get_realized_pnl` | — | `by_ticker` includes AAPL with `realized_pnl` (may be small) | |
| 3.9 | `list_thesis_history` | `symbol="AAPL"` | Multiple versions; latest may be closed | |

**Failure signals**: Order rejection (insufficient buying power, invalid symbol), DB rollback during trade record, thesis not created/closed correctly.

---

### Phase 4: Risk & Analytics

Requires some portfolio history. If account is fresh, run `sync_portfolio` first; risk metrics depend on `daily_snapshots` and `daily_prices`.

| Step | Tool | Arguments | Expected Result | Notes |
|------|------|-----------|-----------------|-------|
| 4.1 | `get_risk_metrics` | `risk_free_rate=0.045` (optional) | `sharpe_ratio`, `sortino_ratio`, `var_95`, `max_drawdown`, `beta_to_spy` | Sparse if little history |
| 4.2 | `get_equity_curve` | — | Array of `{date, equity, daily_return, drawdown}` | |
| 4.3 | `get_correlation_matrix` | `lookback_days=252` (optional) | Matrix for current positions | Empty if no positions |
| 4.4 | `get_stress_test` | — | `current_portfolio_value`, `scenarios` with market move impacts | |
| 4.5 | `get_risk_report` | — | Combined `risk_metrics`, `sector_exposure`, `geographic_exposure` | |
| 4.6 | `get_sector_exposure` | — | Buckets by sector | Depends on Yahoo fundamentals |
| 4.7 | `get_geographic_exposure` | — | Buckets by country | Same |

**Failure signals**: Missing SPY prices (auto-backfilled), insufficient `daily_snapshots`, or Yahoo fundamentals fetch failures (non-fatal; enrichment degrades).

---

### Phase 5: Theses (Standalone)

| Step | Tool | Arguments | Expected Result | Notes |
|------|------|-----------|-----------------|-------|
| 5.1 | `create_thesis` | `symbol="MSFT"`, `thesis="Cloud growth thesis"`, `catalyst="Azure"`, `status="active"` | New thesis with `version: 1` | MSFT need not be in portfolio |
| 5.2 | `get_thesis` | `symbol="MSFT"` | Matching thesis | |
| 5.3 | `list_theses` | `status="active"` | Includes MSFT | |
| 5.4 | `update_thesis_status` | `symbol="MSFT"`, `status="watching"` | Thesis with `status: "watching"` | |
| 5.5 | `list_thesis_history` | `symbol="MSFT"` | Single version (no new version on status change) | |
| 5.6 | `list_enriched_theses` | — | Active theses with `portfolio_weight`, `trailing_pe`, etc. | |

**Edge case**: `get_thesis` for unknown symbol should return 404-style error.

---

### Phase 6: Tags

| Step | Tool | Arguments | Expected Result | Notes |
|------|------|-----------|-----------------|-------|
| 6.1 | `create_tag` | `name="quality-growth"`, `color="#00ff00"`, `description="Quality growth names"` | Tag created | Idempotent if exists |
| 6.2 | `list_tags` | — | Includes `quality-growth` | |
| 6.3 | `tag_ticker` | `symbol="AAPL"`, `tag_name="quality-growth"` | Ticker-tag link created | AAPL must exist in DB (from sync or trade) |
| 6.4 | `list_ticker_tags` | `symbol="AAPL"` | Includes `quality-growth` | |
| 6.5 | `list_tagged_tickers` | `tag_name="quality-growth"` | Includes `AAPL` | |
| 6.6 | `get_thematic_exposure` | — | Buckets include `quality-growth` if positions tagged | |
| 6.7 | `untag_ticker` | `symbol="AAPL"`, `tag_name="quality-growth"` | `removed: true` | |
| 6.8 | `bulk_tag_ticker` | `symbol="AAPL"`, `tag_names=["quality-growth","large-cap"]` | Multiple tags applied | Create `large-cap` first if needed |

**Failure signals**: Tagging a symbol not in `tickers` table (404).

---

### Phase 7: Fundamentals

| Step | Tool | Arguments | Expected Result | Notes |
|------|------|-----------|-----------------|-------|
| 7.1 | `get_ticker_fundamentals` | `symbol="AAPL"` | `trailing_pe`, `target_mean_price`, `recommendation_key`, etc. | Symbol must exist in DB |
| 7.2 | `refresh_fundamentals` | `symbol="AAPL"`, `stale_only=True` | `succeeded`, `failed` counts | Yahoo Finance backend |
| 7.3 | `list_fundamentals` | — | Fundamentals for all current positions | |

**Failure signals**: Yahoo Finance rate limits or missing data; errors are logged but do not block other tools.

---

### Phase 8: Position History

Needs trades in the date range. After Phase 3 you should have AAPL buys/sells.

| Step | Tool | Arguments | Expected Result | Notes |
|------|------|-----------|-----------------|-------|
| 8.1 | `get_position_history` | `start_date="2025-01-01"`, `end_date="2025-03-19"`, `starting_cash=100000` | Daily snapshots with positions and cash | Use real dates that include your trades |
| 8.2 | `get_symbol_position_history` | `symbol="AAPL"`, `start_date`, `end_date` | Daily share counts for AAPL | |
| 8.3 | `get_symbol_position_at_date` | `symbol="AAPL"`, `date="2025-03-18"` | Point-in-time shares for that date | |

**Failure signals**: Invalid date range (e.g. `end_date` before `start_date`), or validation errors from `validate_history_range`.

---

### Phase 9: Cost Basis & Realized P&L

| Step | Tool | Arguments | Expected Result | Notes |
|------|------|-----------|-----------------|-------|
| 9.1 | `get_realized_pnl_details` | `ticker="AAPL"`, `limit=10` | Lot-level sell records | |
| 9.2 | `rebuild_realized_pnl` | — | `rows_rebuilt` count | Safe to run; recomputes from trade ledger |

---

## Quick Smoke Test (5 minutes)

If time is limited, run this minimal sequence:

1. `health_check` → expect `status: "ok"`
2. `sync_portfolio` → expect `snapshot_date`, no errors
3. `execute_trade` buy 1 share AAPL with a test thesis
4. `list_positions` → AAPL appears
5. `list_trades` → trade recorded with thesis
6. `execute_trade` sell 1 share AAPL
7. `list_positions` → AAPL gone
8. `get_realized_pnl` → AAPL in `by_ticker`

---

## Execution Options

### Option A: Manual in Cursor Chat

Use natural language to invoke tools, e.g.:

- *"Run the Glassbox health check"*
- *"Buy 1 share of AAPL with thesis 'E2E test thesis'"*
- *"List my current positions"*

### Option B: MCP Tool Calls

If you have a way to call MCP tools directly (e.g. script or IDE), call tools by name with the arguments above.

### Option C: Automated Script (Future)

A `tests/test_mcp_e2e.py` could use the MCP client SDK to invoke tools programmatically against a running stdio server. Not implemented in this plan.

---

## Rollback / Reset

- **Paper account**: Reset via Alpaca dashboard to clear positions and history.
- **Local DB**: Delete `glassbox.db` and re-run; `init_db()` recreates schema. Sync and trades will repopulate.
- **Cursor MCP**: Restart Cursor or reconnect the Glassbox MCP server if tools stop responding.

---

## Known Limitations

- Fundamentals use Yahoo Finance; rate limits or missing data can cause refresh failures.
- Risk metrics need sufficient `daily_snapshots` and `daily_prices`; fresh accounts will have sparse outputs.
- SPY is auto-backfilled for beta calculations.
- No auth layer; keep `.env` and DB file secure.

---

## Checklist Summary

- [ ] Phase 1: Connectivity
- [ ] Phase 2: Sync & Portfolio
- [ ] Phase 3: Trade Execution (buy → verify → sell → verify)
- [ ] Phase 4: Risk & Analytics
- [ ] Phase 5: Theses
- [ ] Phase 6: Tags
- [ ] Phase 7: Fundamentals
- [ ] Phase 8: Position History
- [ ] Phase 9: Cost Basis & P&L

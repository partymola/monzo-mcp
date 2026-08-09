# monzo-mcp - agent guide

`CLAUDE.md` symlinks to this file. It orients AI agents and contributors working *in* the code, and deliberately does not repeat the user-facing docs:

- **What it is, install, auth, tools, config, CLI, usage** -> [README.md](README.md)
- **Dev environment, running tests, pre-commit hook, PR & security process** -> [CONTRIBUTING.md](CONTRIBUTING.md)

**This is a public open-source repository handling financial data.** Read the Data Safety Rules before committing.

## Data Safety Rules

Every commit, PR, and file is visible to anyone. Before committing ANY change:

- **No real financial data** in code, tests, or docs - no real transaction amounts, balances, account IDs, or merchant names that could identify a user
- **No personal identifiers** - no real names, addresses, boroughs, postcodes, email addresses, or phone numbers
- **No credentials** - no OAuth tokens, client secrets, API keys, or session data
- **Test fixtures must use fictional data** - obviously fake merchants ("Acme Housing", "Coffee Shop"), round amounts (-15000, -2500), generic descriptions ("Childcare", "Top-up")
- **Error messages must not leak secrets** - Monzo API error responses may contain account-specific data; never put raw response bodies in exceptions or logs
- **`config/` and `*.db` are gitignored for a reason** - never override this

The `scripts/check-no-data.sh` pre-commit hook enforces most of this - install it per [CONTRIBUTING.md](CONTRIBUTING.md).

## Architecture

- **Entry point**: `src/monzo_mcp/cli.py` - routes the `auth` subcommand or starts the MCP stdio server. **Keep it inside the package.** As `src/main.py` with a `main:main` console script, the wheel installed a top-level `main` module into site-packages, where any other package doing the same overwrites it - installing a sibling MCP server made `monzo-mcp` start that server instead
- **MCP server**: `mcp_instance.py` creates the shared `MCPServer("monzo-server")` instance
- **Auth**: `auth.py` - OAuth setup CLI + token refresh (5-min expiry buffer). Redirect `http://localhost:6600/callback`; SCA window is 5 min after app approval for full history, then 90 days only
- **API**: `api.py` - GET-only wrapper with auto-refresh and typed exceptions (read-only by design; no write path exists). Reads and parses in separate steps: a body that will not decode or is not JSON raises `ValueError`, which no transport handler catches, so combining them let an intermediary's HTML page escape the sync loop unrecorded.
- **Failure classification**: `refresh_token` is a boundary over `_refresh_token` and raises exactly two types. `TokenRefused` only where the server or the credential files judged the credentials unusable; `RefreshNetworkError` for everything else, via a catch-all, so an unanticipated failure lands there by construction. `api.get` maps the first to `MonzoAuthError` and the second to `MonzoAPIError`. **Never widen `TokenRefused` to a condition that can clear on its own** (a rate limit, a 403 from a WAF, an unreadable response): Monzo rotates the refresh token on use and the token file is shared, so re-authorising in answer to a transient fault causes the outage it was meant to diagnose. Pinned by `TestTheRefreshBoundary` in `tests/test_api.py`.
- **DB**: `db.py` - SQLite schema, `get_db()`, `migrate()` (ALTER TABLE for columns added after a DB was created), balance/sync helpers
- **Tools**: `tools/account_tools.py`, `tools/transaction_tools.py`, `tools/analysis_tools.py`
- **Helpers**: `helpers.py` - `require_auth` (auth-gate decorator wrapping every tool), `format_response`, `pence_to_pounds`, `validate_account_type`
- **Config**: env vars `MONZO_MCP_CONFIG_DIR`, `MONZO_MCP_DB_PATH`, falling back to package-relative paths

## Database schema

SQLite at `monzo.db` (gitignored). Amounts stored in pence (integers), converted to pounds only in tool responses.

- `monzo_transactions` - id, account_id, account_type, created, amount (pence), currency, description, merchant_name, category, notes, settled, counterparty_name, counterparty_sort_code, counterparty_account_number, counterparty_user_id
- `balances` - time-series snapshots (account_type, name, balance in pence, captured_at)
- `sync_log` - sync history with timestamps, record counts and a status. `get_last_sync_attempt` reads every row and is what throttles the automatic sync to once a day. **Throttle on attempts, never on successes** - a sync that keeps failing never advances a success timestamp, so gating on one starts a fresh full sync on every tool call, which answers a rate limit by retrying continuously. The consequence is deliberate and worth knowing: a failure suppresses automatic syncing for the rest of the day even once the cause clears, so `monzo_sync` is the way to force one. Every exit that reaches the API writes a row, including a trailing catch-all, and the database is opened before the first API call so that the exit which fails on `/accounts` has a connection to write on. Two exits deliberately write nothing, because no sync was attempted: an invalid `since`, rejected before the database is opened, and a failure to open the database at all. Pinned by `TestTheAutoSyncThrottleCountsAttempts` and `TestAnUnnamedFailureStillLeavesASyncLogRow` in `tests/test_transactions.py`

## Key invariants

- **Auth-hold dedup**: after syncing, a duplicate unsettled transaction is removed when a matching settled one exists (same merchant, amount, account, within ~15 min). Both-settled pairs are kept as genuine separate charges
- **`since` backfill** on `monzo_sync`/`run_sync`: RFC3339-coerced via `_coerce_since`, overrides the last-sync resume cursor, and reaching >90 days back only works inside the SCA window (90-day fallback otherwise)
- **Counterparty**: the `/transactions` list endpoint already returns a `counterparty` object for bank transfers (`payport_faster_payments`, `p2p_payment`, `bacs` schemes) - no expand param or per-transaction fetch needed. Sync persists name/sort_code/account_number/user_id; list/search emit a `counterparty` object only when a name is present (card transactions are unchanged), and search matches `counterparty_name`. Rows cached before these columns existed stay NULL until re-fetched (i.e. the next sync that reaches them - beyond 90 days that means an SCA-window backfill)
- All tools are `async def` with `@mcp.tool()` + `@require_auth`; sync HTTP calls are wrapped in `anyio.to_thread.run_sync()` to avoid blocking
- **Comparison**: `monzo_spending`'s `vs_previous` applies the same `category` and `account_type` filters as the month it is comparing. A filtered month against an unfiltered one reports the difference between two different questions as a percentage change. Pinned by `test_vs_previous_applies_the_same_filters_as_the_month_it_compares` and `test_vs_previous_applies_a_category_filter_too` in `tests/test_spending_tool.py`
- Cache-reading tools call `auto_sync_if_stale()` before querying - an incremental sync if not synced today **in UTC**. Both sides of that comparison must be UTC: `log_sync` stores a UTC timestamp, so never compare it against a local date - that reopens the retry storm east of Greenwich, and a "today" earlier than the UTC date suppresses syncing west of it. Pinned by `test_a_local_date_ahead_of_utc_does_not_defeat_the_throttle` and `test_a_local_evening_west_of_greenwich_counts_towards_the_next_utc_day` in `tests/test_transactions.py`

## Running tests

```bash
.venv/bin/python -m pytest tests/ -v
```

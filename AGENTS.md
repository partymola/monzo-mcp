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

- **Entry point**: `src/main.py` - routes the `auth` subcommand or starts the MCP stdio server
- **FastMCP**: `mcp_instance.py` creates the shared `FastMCP("monzo-server")` instance
- **Auth**: `auth.py` - OAuth setup CLI + token refresh (5-min expiry buffer). Redirect `http://localhost:6600/callback`; SCA window is 5 min after app approval for full history, then 90 days only
- **API**: `api.py` - GET-only wrapper with auto-refresh and typed exceptions (read-only by design; no write path exists)
- **DB**: `db.py` - SQLite schema, `get_db()`, balance/sync helpers
- **Tools**: `tools/account_tools.py`, `tools/transaction_tools.py`, `tools/analysis_tools.py`
- **Config**: env vars `MONZO_MCP_CONFIG_DIR`, `MONZO_MCP_DB_PATH`, falling back to package-relative paths

## Database schema

SQLite at `monzo.db` (gitignored). Amounts stored in pence (integers), converted to pounds only in tool responses.

- `monzo_transactions` - id, account_id, account_type, created, amount (pence), currency, description, merchant_name, category, notes, settled
- `balances` - time-series snapshots (account_type, name, balance in pence, captured_at)
- `sync_log` - sync history with timestamps and record counts

## Key invariants

- **Auth-hold dedup**: after syncing, a duplicate unsettled transaction is removed when a matching settled one exists (same merchant, amount, account, within ~15 min). Both-settled pairs are kept as genuine separate charges
- **`since` backfill** on `monzo_sync`/`run_sync`: RFC3339-coerced via `_coerce_since`, overrides the last-sync resume cursor, and reaching >90 days back only works inside the SCA window (90-day fallback otherwise)
- All tools are `async def` with `@mcp.tool()` + `@require_auth`; sync HTTP calls are wrapped in `anyio.to_thread.run_sync()` to avoid blocking
- Cache-reading tools call `auto_sync_if_stale()` before querying (incremental sync if not synced today)

## Running tests

```bash
.venv/bin/python -m pytest tests/ -v
```

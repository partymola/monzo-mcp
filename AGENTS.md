# Monzo MCP Server

**This is a public open-source repository.** Do not commit personal or financial data.

MCP server for Monzo banking API. Read-only tools with OAuth auto-refresh and local transaction cache.

## Data Safety Rules

This repo is public. Every commit, PR, and file is visible to anyone. Before committing ANY change:

- **No real financial data** in code, tests, or docs - no real transaction amounts, balances, account IDs, or merchant names that could identify a user
- **No personal identifiers** - no real names, addresses, boroughs, postcodes, email addresses, or phone numbers
- **No credentials** - no OAuth tokens, client secrets, API keys, or session data
- **Test fixtures must use fictional data** - use obviously fake merchants ("Acme Housing", "Coffee Shop"), round amounts (-15000, -2500), and generic descriptions ("Childcare", "Top-up")
- **Error messages must not leak secrets** - API error responses from Monzo may contain account-specific data; never include raw response bodies in exceptions or logs that could reach committed code
- **The `config/` directory and `*.db` files are gitignored for a reason** - never override this

## Quick Reference

```
monzo-mcp auth          # Interactive OAuth setup (browser + Monzo app approval)
monzo-mcp               # Start MCP server (stdio transport, used by Claude Code)
```

## Tools

| Tool | Source | Purpose |
|------|--------|---------|
| `monzo_list_accounts` | Live API | Account IDs, types, status |
| `monzo_get_balance` | Live API | Balance + spend_today (also saves snapshot) |
| `monzo_list_pots` | Live API | Pot names and balances (also saves snapshots) |
| `monzo_sync` | Live API -> SQLite | Sync transactions with pagination, SCA fallback, dedup |
| `monzo_list_transactions` | Cache (auto-sync) | Filter by date, category, merchant, account |
| `monzo_search_transactions` | Cache (auto-sync) | Full-text search across merchant, description, notes |
| `monzo_spending` | Cache (auto-sync) | Category breakdown, top merchants, month-over-month |

## Architecture

- **Entry point**: `src/main.py` - routes `auth` subcommand or starts MCP stdio server
- **FastMCP**: `mcp_instance.py` creates the shared `FastMCP("monzo-server")` instance
- **Auth**: `auth.py` - OAuth setup CLI + token refresh (5-min expiry buffer)
- **API**: `api.py` - GET wrapper with auto-refresh, typed exceptions
- **DB**: `db.py` - SQLite schema, `get_db()`, balance/sync helpers
- **Tools**: `tools/account_tools.py`, `tools/transaction_tools.py`, `tools/analysis_tools.py`

## Auth & Credentials

- OAuth client registered at https://developers.monzo.com
- Redirect URL: `http://localhost:6600/callback`
- Credentials in `config/monzo_client.json` and `config/monzo_tokens.json` (gitignored)
- Token auto-refreshes with 5-minute buffer before expiry
- SCA window: 5 min after app approval for full history, then 90 days only

## Database

SQLite at `monzo.db` (gitignored). Tables:
- `monzo_transactions` - id, account_id, account_type, created, amount (pence), currency, description, merchant_name, category, notes, settled
- `balances` - time-series snapshots (account_type, name, balance in pence, captured_at)
- `sync_log` - sync history with timestamps and record counts

Amounts stored in pence (integers), converted to pounds only in tool responses.

## Auth-hold dedup

After syncing, duplicate unsettled transactions are removed when a matching settled transaction exists (same merchant, amount, account, within ~15 min). Both-settled pairs are kept as genuine separate charges.

## Key patterns

- All tools are `async def` with `@mcp.tool()` + `@require_auth` decorators
- Sync HTTP calls wrapped in `anyio.to_thread.run_sync()` to avoid blocking
- Account ID resolution: tools look up account_id from a live `/accounts` call
- Config paths from env vars (`MONZO_MCP_CONFIG_DIR`, `MONZO_MCP_DB_PATH`) with fallback to package-relative paths

## Running tests

```bash
cd monzo-mcp
.venv/bin/python -m pytest tests/ -v
```

## Troubleshooting

- **"Monzo not configured"**: Run `monzo-mcp auth`
- **"Token refresh failed"**: Re-run `monzo-mcp auth` (refresh token may have expired)
- **"SCA required"**: Open Monzo app, approve the login, then re-sync
- **Empty transaction list**: Run `monzo_sync` first to populate the cache
- **Python 3.13+ required**. Python 3.14 now works (pydantic-core 2.45.0+ has 3.14 wheels).

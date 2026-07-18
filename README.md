# monzo-mcp

[![CI](https://github.com/partymola/monzo-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/partymola/monzo-mcp/actions/workflows/ci.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)
[![Glama MCP Server](https://glama.ai/mcp/servers/partymola/monzo-mcp/badges/score.svg)](https://glama.ai/mcp/servers/partymola/monzo-mcp)

MCP server for the [Monzo](https://monzo.com) banking API. Read-only access to accounts, balances, pots, transactions, and spending analysis - all through Claude Code or any MCP client.

Unlike other Monzo MCP implementations that use raw bearer tokens (which expire in 6 hours), this server handles **full OAuth with automatic token refresh**.

## Features

- **7 read-only tools** - no write operations, no money movement
- **OAuth with auto-refresh** - tokens refresh automatically, no manual regeneration
- **Local transaction cache** - SQLite database survives Monzo's 90-day SCA window
- **Auto-sync on demand** - the cache-reading tools run an incremental sync automatically if the cache wasn't synced today, so you rarely need to call `monzo_sync` by hand
- **Spending analysis** - category breakdowns, top merchants, month-over-month comparison
- **Transaction search** - search by merchant, payee (counterparty), description, or notes across cached history
- **Counterparty details** - bank transfers (faster payments, p2p, Bacs) are cached with the payee's name, sort code, and account number

## Tools

| Tool | Description | Data source |
|------|-------------|-------------|
| `monzo_list_accounts` | List accounts with types and IDs | Live API |
| `monzo_get_balance` | Current balance and spend today | Live API |
| `monzo_list_pots` | Savings pots and balances | Live API |
| `monzo_sync` | Sync transactions to local cache | Live API -> SQLite |
| `monzo_list_transactions` | List/filter cached transactions | Local cache (auto-syncs if stale) |
| `monzo_search_transactions` | Search by merchant/payee/description/notes | Local cache (auto-syncs if stale) |
| `monzo_spending` | Spending analysis with category breakdown | Local cache (auto-syncs if stale) |

## Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- A Monzo account with an OAuth client registered at [developers.monzo.com](https://developers.monzo.com)

## Installation

```bash
git clone https://github.com/partymola/monzo-mcp.git
cd monzo-mcp
uv venv --python 3.13 .venv
uv pip install -e .
```

## Setup

### 1. Register a Monzo OAuth client

Go to [developers.monzo.com](https://developers.monzo.com) and create an OAuth client:
- Set the redirect URL to `http://localhost:6600/callback`
- Note your **Client ID** and **Client Secret**

### 2. Authenticate

```bash
monzo-mcp auth
```

This opens your browser for Monzo OAuth. After authorizing, approve the login in your **Monzo app** within 5 minutes for full transaction history access (Monzo's SCA window).

### 3. Register with Claude Code

```bash
claude mcp add -s user monzo -- /path/to/monzo-mcp/.venv/bin/monzo-mcp
```

### 4. First sync

In Claude Code, run `monzo_sync` to populate the local transaction cache. Do this immediately after auth to take advantage of the SCA window (up to 11 months of history).

## CLI

```
monzo-mcp              Start the MCP server (stdio transport)
monzo-mcp auth         Interactive OAuth setup (opens the browser)
monzo-mcp --version    Print the installed package version
```

## Configuration

All configuration is via environment variables (optional):

| Variable | Default | Description |
|----------|---------|-------------|
| `MONZO_MCP_CONFIG_DIR` | `<package>/config/` | Directory for OAuth credentials and tokens |
| `MONZO_MCP_DB_PATH` | `<package>/monzo.db` | Path to SQLite transaction cache |

Credential files (created by `monzo-mcp auth`):
- `config/monzo_client.json` - OAuth client ID and secret
- `config/monzo_tokens.json` - Access and refresh tokens (auto-refreshed)

## Monzo SCA window

Monzo's Strong Customer Authentication (SCA) limits transaction history access:
- **Within 5 minutes of app approval**: up to ~11 months of history
- **After the window expires**: only the last 90 days

The local SQLite cache preserves all synced transactions permanently, so run `monzo_sync` promptly after `monzo-mcp auth`.

To backfill a specific range, pass `since` to `monzo_sync` - an ISO date (`2026-01-01`) or datetime (`2026-01-01T14:30:00Z`). It overrides the usual last-sync resumption and is passed straight to the Monzo API. Reaching back more than ~90 days only works inside the SCA window; outside it, the 90-day fallback applies.

Transactions cached by a version of this server that predates a schema addition (e.g. counterparty details on bank transfers) gain the new fields only when re-fetched from the API. The next post-auth full sync - typically the periodic `monzo-mcp auth` re-authentication followed by a sync inside the SCA window - backfills them for the history it re-pulls.

## Security

- **Zero write tools** - cannot send money, move funds between pots, or modify transactions
- **Monzo API itself** cannot send money to external accounts
- Tokens stored as JSON files in the `config/` directory (gitignored)
- All API calls are GET requests with Bearer token auth

## Troubleshooting

- **"SCA required" or only 90 days of history** - re-run `monzo-mcp auth` and approve the login in the Monzo app within 5 minutes, then sync straight away (see the SCA window above).
- **Token expired / no refresh** - re-run `monzo-mcp auth` to re-authorise.
- **"No transaction data available"** - the cache is empty; call `monzo_sync` (or any cache-reading tool, which auto-syncs) once after authenticating.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, the test workflow, and the pre-commit hook. Changes are tracked in [CHANGELOG.md](CHANGELOG.md).

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

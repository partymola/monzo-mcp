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

This package is not on PyPI - the name belongs to an unrelated project. A container image is published instead; see [Docker](#docker).

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

On Windows the console script is at `.venv\Scripts\monzo-mcp.exe`.

### 4. First sync

In Claude Code, run `monzo_sync` to populate the local transaction cache. Do this immediately after auth to take advantage of the SCA window (up to 11 months of history).

## Docker

Images are published to `ghcr.io/partymola/monzo-mcp`. Tags carry a `v` prefix (`:vX.Y.Z`), and `:latest` follows the most recent release.

**The container needs a volume.** Credentials and the transaction cache live under `/data`; with nothing mounted there, the container still starts and every tool reports that it is not configured, and anything you authorise is lost as soon as the container is replaced.

First register an OAuth client as described in [Setup step 1](#1-register-a-monzo-oauth-client) - `auth` prompts for the Client ID and secret, and there is no way to supply them later. The redirect URL is `http://localhost:6600/callback`, the same as a source install.

Authenticate once, into a named volume. Decide before you run this whether you want a bind mount instead - switching afterwards means authorising again:

```bash
docker volume create monzo-mcp-data
docker run --rm -it \
  -v monzo-mcp-data:/data \
  -p 127.0.0.1:6600:6600 \
  ghcr.io/partymola/monzo-mcp:latest auth
```

The published port is needed only for this step, so the OAuth redirect can reach the container. Binding it to `127.0.0.1` keeps the callback listener off your network. **No browser opens** - the container has none - so copy the URL it prints. Then approve the login in your Monzo app within 5 minutes (see [Monzo SCA window](#monzo-sca-window)).

Then register the server, reusing the same volume:

```bash
claude mcp add -s user monzo -- \
  docker run --rm -i -v monzo-mcp-data:/data ghcr.io/partymola/monzo-mcp:latest
```

`-i` is required - the server speaks JSON-RPC over stdin and stdout.

**Sync straight away.** The 11-month backfill closes 5 minutes after you approve in the Monzo app (see [Monzo SCA window](#monzo-sca-window)), and this route is longer than a source install - so call `monzo_sync` as soon as the server is registered. Miss it and you silently get 90 days instead, with no error.

To keep the files somewhere you can read them, use a bind mount instead of a named volume. The container runs as root and writes credentials owner-only, so without `--user` they end up root-owned:

```bash
mkdir -p ~/monzo-mcp-data/config
docker run --rm -it \
  -v ~/monzo-mcp-data:/data \
  --user $(id -u):$(id -g) \
  -p 127.0.0.1:6600:6600 \
  ghcr.io/partymola/monzo-mcp:latest auth
```

Pass the same `-v` and `--user` to the server command. Create the directory first - `--user` against a named volume fails, because the volume initialises root-owned from the image.

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
| `MONZO_MCP_CALLBACK_HOST` | `localhost` | Interface the `auth` callback server binds to. The redirect URI is unaffected |

The container image sets the first two under `/data`, because the package-relative defaults resolve into the interpreter's lib directory there, which cannot be mounted. It sets the third to `0.0.0.0`, because a published port arrives on the container's bridge interface and a `localhost` bind refuses it.

Credential files (created by `monzo-mcp auth`):
- `config/monzo_client.json` - OAuth client ID and secret
- `config/monzo_tokens.json` - Access and refresh tokens (auto-refreshed)

## Monzo SCA window

Monzo's Strong Customer Authentication (SCA) limits transaction history access:
- **Within 5 minutes of app approval**: up to ~11 months of history
- **After the window expires**: only the last 90 days

The local SQLite cache preserves all synced transactions permanently, so run `monzo_sync` promptly after `monzo-mcp auth`.

To backfill a specific range, pass `since` to `monzo_sync` - an ISO date (`2026-01-01`) or datetime (`2026-01-01T14:30:00Z`). Reaching back more than ~90 days only works inside the SCA window; outside it, only the last 90 days are returned.

Older cached transactions gain fields added in newer versions (e.g. counterparty/payee details on bank transfers) only when re-fetched, which a post-auth full sync does for the history it re-pulls.

## Security

- **Zero write tools** - cannot send money, move funds between pots, or modify transactions
- **Monzo API itself** cannot send money to external accounts
- Tokens stored as JSON files in the `config/` directory (gitignored)
- All API calls are GET requests with Bearer token auth

## Troubleshooting

- **"SCA required" or only 90 days of history** - re-run `monzo-mcp auth` and approve the login in the Monzo app within 5 minutes, then sync straight away (see the SCA window above).
- **Token expired / no refresh** - re-run `monzo-mcp auth` to re-authorise.
- **"No transaction data available"** - the cache is empty; call `monzo_sync` (or any cache-reading tool, which auto-syncs) once after authenticating.
- **Every tool reports "not configured" under Docker, or authentication does not survive a restart** - nothing is mounted at `/data`. See [Docker](#docker); the same volume must be passed to the `auth` run and to the server.
- **`auth` sits on "Waiting for callback..." after you approve** - the callback was not delivered. Press Ctrl-C and run it again. Under Docker, check the port is published (`-p 127.0.0.1:6600:6600`).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, the test workflow, and the pre-commit hook. Changes are tracked in [CHANGELOG.md](CHANGELOG.md).

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

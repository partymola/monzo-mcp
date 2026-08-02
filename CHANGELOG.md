# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0] - 2026-08-03

### Changed

- Ported to the `mcp` 2.x server API. 2.0.0 renamed `mcp.server.fastmcp` to `mcp.server.mcpserver` and the `FastMCP` class to `MCPServer`, with no compatibility alias. The tool contract is unchanged: every tool keeps its name, description, and input and output schemas.
- The `mcp<2` cap added in 0.4.0 is lifted, now that the server runs on 2.x. It was a holding action to keep installs working, not the destination.
- Every dependency is pinned to an exact version instead of a lower bound: `mcp` 2.0.0, and for development `pytest` 9.1.1 and `ruff` 0.16.1.

### Packaging

- The build toolchain is pinned alongside the dependencies: `setuptools` to an exact version, the `python:3.13-slim` base image by digest, and every GitHub Action to a full commit SHA rather than a moving major tag. A floating tag can change what a build produces with nobody deciding, which is the same failure the dependency pins address.

## [0.4.0] - 2026-08-02

### Changed

- **Breaking:** every account-scoped tool now names the account in its response, under one key. `monzo_list_pots`, `monzo_list_transactions` and `monzo_search_transactions` return `{"account_type": ..., "pots"/"transactions": [...]}` instead of a bare array; `monzo_spending` gains `account_type` on all four result shapes; and `monzo_sync`'s per-account details rename `account` to `account_type`. An empty array previously carried no trace of which account produced it, so "nothing there" and "wrong account" were indistinguishable - and the sync response was the one place calling this field `account` while every tool argument is `account_type`, which makes passing the wrong argument name easy. Where no account filter is given the value is `null`, keeping "all accounts" distinct from a missing key.

### Fixed

- `mcp` is now capped below 2.0. The dependency was declared `>=1.6.0` with no upper bound, so once `mcp` 2.0.0 was published a clean install pulled it in and the server failed to import - 2.0.0 no longer ships `mcp.server.fastmcp`, which this server is built on.

## [0.3.0] - 2026-07-18

### Added

- Counterparty support for bank transfers (faster payments, p2p, Bacs): sync now persists the payee's name, sort code, account number, and user id from the `counterparty` object the transactions endpoint already returns. `monzo_list_transactions` and `monzo_search_transactions` include a `counterparty` object for transactions that have one, and search also matches the counterparty name - so transfers can be found by payee. Existing databases are migrated automatically (new nullable columns); previously cached rows gain counterparty data when re-fetched by a later sync.

## [0.2.1] - 2026-07-11

### Packaging

- Listed in the official MCP registry (`io.github.partymola/monzo-mcp`) via a GHCR container image; a release workflow builds/pushes the image and publishes to the registry.

## [0.2.0] - 2026-06-09

### Added

- `monzo_sync` (and the underlying `run_sync`) accept an optional `since` parameter - an ISO date (`2026-01-01`) or datetime (`2026-01-01T14:30:00Z`) - to backfill from an explicit start, overriding last-sync resumption. The value is coerced to RFC3339 and passed straight to the Monzo API; reaching beyond ~90 days only works inside the post-auth SCA window, otherwise the existing 90-day fallback applies.
- `monzo-mcp --version` prints the installed package version.
- Continuous integration now runs the test suite on Python 3.14 in addition to 3.13.

## [0.1.0] - 2026-04-26

### Added

- Initial release.
- OAuth 2.0 authentication against the Monzo Developer API with automatic token refresh.
- Local SQLite cache for transactions, balance snapshots, and pot snapshots, with auto-sync on stale data.
- Pagination, SCA fallback handling, and auth-hold dedup for transaction sync.
- MCP tools (read-only): `monzo_list_accounts`, `monzo_get_balance`, `monzo_list_pots`, `monzo_sync`, `monzo_list_transactions`, `monzo_search_transactions`, `monzo_spending`.
- Spending analysis with category breakdown, top merchants, and month-over-month comparison.
- Transaction search across merchant name, description, and notes.
- Pre-commit hook (`scripts/check-no-data.sh`) blocking commit of databases, tokens, and other secrets.

[Unreleased]: https://github.com/partymola/monzo-mcp/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/partymola/monzo-mcp/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/partymola/monzo-mcp/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/partymola/monzo-mcp/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/partymola/monzo-mcp/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/partymola/monzo-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/partymola/monzo-mcp/releases/tag/v0.1.0

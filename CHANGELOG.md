# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/partymola/monzo-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/partymola/monzo-mcp/releases/tag/v0.1.0

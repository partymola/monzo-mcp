# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.0] - 2026-08-09

### Fixed

- Installing this package no longer puts a module called `main` in the Python environment, where it could overwrite, or be overwritten by, any other package that does the same. Installed alongside another MCP server built the same way, whichever was installed second won, and the `monzo-mcp` command silently started the other server. The command name and everything a user types are unchanged.
- On POSIX, credential files are created with owner-only permissions, and existing ones are tightened when rewritten on a best-effort basis - tightening needs ownership the writer may not have, and the file has already been emptied for rewriting by that point, so a permissions failure must not take the token with it. They were written with no mode set at all, so they took whatever the umask gave them - world-readable on a default umask, permanently, for a file holding a refresh token. Windows ignores the mode and governs access by inherited ACLs, so nothing changes there. A file created by an earlier version is narrowed the next time a token is saved; to check now, `ls -l config/`.
- A data request that gets an unreadable answer is reported rather than escaping. A proxy or captive portal replying to an API call with HTML, with a body that will not decode, or with JSON that is not an object, raised past every handler in `api.get` and past the sync loop, which catches only the Monzo types. A read timeout or a truncated response did the same: `urlopen` wraps only connect-phase failures, so those arrive unwrapped. A sync that ends with a transaction error is now logged with status `error` rather than `ok`, whichever page of the history failed - a failure after the first page was not recorded at all, so a partial history was reported as a complete sync. Only a genuine SCA refusal produces the "approve in Monzo app" note; an unreadable response used to produce it too. An SCA refusal is still logged as a success, deliberately and on every page: it is answered by approving in the Monzo app, and nothing this client does before then changes the outcome. Reading and parsing are now separate steps.
- Every sync that reaches the API now leaves a row in the sync log. The account lookup that opens a sync sits before the point where a token failure surfaces, and the log was opened after it, so the commonest failure of all left no trace; finding no accounts left none either. The log is opened first now. Recording is best-effort - a database that cannot be written to cannot take the row either - but a failure to record no longer replaces the error that caused it. A request rejected before any call is made, such as an unparseable `since` date, still writes nothing: no sync was attempted.
- The comparison against the previous month applies the same filters as the month it is compared with. Asking for one category, or one account, compared that filtered month against every transaction in the month before it, and reported the difference as a percentage change - so a month with identical spending on the account asked about could be reported as ninety per cent down.
- The once-a-day automatic sync compares two timestamps in the same timezone. The sync log records UTC and the comparison used the local date, so east of Greenwich there was a window each night in which a sync that had just finished counted as yesterday's, and every tool call started another one - the retry storm the throttle exists to prevent. "Today" now means the UTC day everywhere: west of Greenwich a sync late in the local evening counts towards the next day, so there can be one extra sync per local day.
- A failure while reading a balance or a pot reports the kind of failure rather than repeating its message. Both were caught by a bare `except`, whose message set cannot be known in advance, and the result is returned to the model.
- The automatic sync decides it has already run today by counting attempts rather than successes. It counted successes, so a sync that kept failing never advanced the timestamp gating it, and every tool call started a fresh full sync - answering a rate limit or a captive portal by retrying continuously, and writing a log row each time. The trade is deliberate and worth knowing: a failed sync now suppresses automatic syncing for the rest of the day even after the cause clears. `monzo_sync` forces one at any time.

## [0.5.1] - 2026-08-09

### Fixed

- Every way of failing to obtain an access token is now classified, and reaches callers as one of two outcomes rather than escaping. Only a refusal is an authentication failure: HTTP 400 or 401, a response carrying no token, or credentials that are missing, unreadable or malformed. Everything else is a network error - an unreachable server, a read timeout, a reset connection, a truncated or non-HTTP response, a body that will not decode, a 403, a rate limit, and a 5xx.

  The classification is made where the token is obtained, with a catch-all at that boundary, so an unanticipated failure is a network error by construction rather than by listing exception types. The distinction matters because an authentication failure tells the user to re-authorise, which rewrites the shared token file and spends the refresh token the syncing host owns - the wrong answer to a rate limit or a dropped connection, both of which clear on their own. 403 is treated as a network condition: it is what a WAF returns, and this client already reads 403 on a data request as a Strong Customer Authentication prompt.
- The message reported when a token cannot be obtained is fixed text. It previously carried the underlying exception, which for a credential-file failure is an absolute path, into a string that reaches the MCP client.

### Packaging

- The container image is built on Python 3.14 instead of 3.13, and 3.14 joins the supported-version classifiers. `requires-python` is unchanged at `>=3.13`: the package still supports both, and only the published image moves. Installing from PyPI is unaffected - that uses whichever Python the user already has.
- Dependency updates are automated. Every dependency, the base image and the CI actions are pinned to exact versions, so nothing changes without a deliberate bump; Dependabot now proposes those bumps rather than leaving the pins to rot.

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

[Unreleased]: https://github.com/partymola/monzo-mcp/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/partymola/monzo-mcp/compare/v0.5.1...v0.6.0
[0.5.1]: https://github.com/partymola/monzo-mcp/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/partymola/monzo-mcp/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/partymola/monzo-mcp/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/partymola/monzo-mcp/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/partymola/monzo-mcp/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/partymola/monzo-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/partymola/monzo-mcp/releases/tag/v0.1.0

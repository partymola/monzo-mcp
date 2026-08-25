# Contributing to monzo-mcp

Thanks for your interest in contributing. This is a community MCP server for the Monzo banking API.

## Getting started

### Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- A Monzo account with an OAuth client registered at [developers.monzo.com](https://developers.monzo.com)

### Set up the dev environment

```bash
git clone https://github.com/partymola/monzo-mcp
cd monzo-mcp
uv venv --python 3.13 .venv
uv pip install -e ".[dev]"
```

### Install the pre-commit hook

The repo ships with `scripts/check-no-data.sh`, which blocks commits that contain databases, tokens, or other secrets:

```bash
ln -sf ../../scripts/check-no-data.sh .git/hooks/pre-commit
```

Please install it before your first commit. On Windows, Git Bash copies rather than symlinks unless Developer Mode is on, and a copied hook keeps running whatever the script said when you installed it - re-copy it after any change to `scripts/check-no-data.sh`.

### Run the test suite

```bash
.venv/bin/python -m pytest tests/ -v      # .venv\Scripts\python on Windows
```

CI runs this on Linux, macOS and Windows. Tests are fully offline - no real API calls, no real tokens, and no reads of your own cache. Autouse fixtures in `tests/conftest.py` enforce that rather than trusting each test to: your credential files and `monzo.db` are replaced with paths in a temporary directory, and a test that reaches `urllib.request.urlopen` fails. Fixtures use fictional merchants and round amounts; never paste real transaction data into tests.

### Run lint checks

```bash
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format --check src tests
```

## Making changes

- **Open an issue first** for non-trivial changes (new tools, schema migrations, new endpoints, breaking changes). Small fixes (typos, bug fixes, docs) can go straight to a PR.
- Keep PRs small and focused.
- Add or update tests for any behaviour change.
- This server is intentionally read-only - PRs that add write tools (sending money, moving pots, modifying transactions) will not be accepted.
- Run `ruff check src tests` and `ruff format --check src tests` before opening a PR.
- Run `pytest tests/ -v` before opening a PR.

## Releases (maintainers)

1. Bump `version` in `pyproject.toml`, run `uv lock` so the tracked lockfile records the new version, and turn the `[Unreleased]` CHANGELOG heading into `## [X.Y.Z] - YYYY-MM-DD`, adding the compare link at the foot of the file.
2. Push to `main` and wait for CI to pass on that commit.
3. Tag it `vX.Y.Z` and push the tag by name.
4. Create the GitHub Release.

Step 4 is what publishes: `publish-registry.yml` runs on `release: published`, not on the tag push, so the tag on its own ships nothing. It builds the `Dockerfile`, pushes `ghcr.io/partymola/monzo-mcp:vX.Y.Z` and `:latest`, and publishes to the MCP registry. Because the Release event is what builds the image, do not create it until CI is green on the tagged commit.

**Do not hand-edit `server.json`'s `version` or `packages[0].identifier`.** The workflow rewrites both from the tag before publishing, so the values committed to the repo are deliberately left behind and are not a bug. To see what actually published, query the registry rather than reading the file:

```bash
curl -s "https://registry.modelcontextprotocol.io/v0/servers?search=io.github.partymola/monzo-mcp"
```

`--version` reads the installed package metadata, so it follows `pyproject.toml`.

## Pull requests

- Branch off `main`.
- Reference any related issue.
- Maintainer aims to reply within ~7 days. Feel free to bump if you don't hear back.

## Reporting issues

Helpful details to include:

- Python version (`python --version`)
- MCP client (Claude Desktop, Claude Code, other)
- Steps to reproduce
- Relevant log output, with any tokens, account IDs, or transaction details redacted

## Security

Please do not open a public issue for credential, OAuth-flow, or token-leak issues. Use [GitHub's private vulnerability reporting](https://github.com/partymola/monzo-mcp/security/advisories/new) instead.

## License

By contributing, you agree that your contributions are licensed under GPL-3.0-or-later, the project's license.

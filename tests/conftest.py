"""Shared fixtures for the monzo-mcp test suite.

What these hold and why is in AGENTS.md, "Running tests".
"""

import urllib.request

import pytest

from monzo_mcp import auth, config, db, helpers

#: The modules that bind a path at import and so keep their own copy of it -
#: patching `config` alone reaches none of them. Pinned by
#: `test_only_the_expected_modules_hold_a_config_path`.
_CREDENTIAL_HOLDERS = (config, auth, helpers)
_CONFIG_DIR_HOLDERS = (config, auth)
_DATABASE_HOLDERS = (config, db)


@pytest.fixture(autouse=True)
def _no_real_credentials(tmp_path_factory, monkeypatch):
    """Point every credential, config-directory and database path at a temp one."""
    empty = tmp_path_factory.mktemp("isolated")
    # The credential files sit *inside* the isolated config directory, because
    # `config.py` derives them from it: pointing them at unrelated directories
    # runs the suite against a layout production cannot produce, and a test
    # asserting that `auth` writes the client file into the config directory
    # would then pass while meaning nothing.
    isolated_config = empty / "config"
    for module in _CREDENTIAL_HOLDERS:
        monkeypatch.setattr(module, "MONZO_TOKENS_PATH", isolated_config / "monzo_tokens.json")
        monkeypatch.setattr(module, "MONZO_CLIENT_PATH", isolated_config / "monzo_client.json")
    for module in _CONFIG_DIR_HOLDERS:
        monkeypatch.setattr(module, "CONFIG_DIR", isolated_config)
    for module in _DATABASE_HOLDERS:
        monkeypatch.setattr(module, "DB_PATH", empty / "monzo.db")

    # The token cache outlives the files: left populated by one test it hands
    # the next a working access token with no credential file anywhere, which
    # is the same "every test remembers" the fixtures exist to replace.
    monkeypatch.setattr(auth, "_cached_tokens", None)
    monkeypatch.setattr(auth, "_cached_creds", None)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Refuse `urllib.request.urlopen`, as a backstop to the fixture above.

    `pytest.fail` rather than `assert`: `auto_sync_if_stale` ends in `except
    Exception: pass`, which absorbs an `AssertionError` and reports nothing.
    """

    def refuse(*_args, **_kwargs):
        pytest.fail("this test reached the network; patch urllib.request.urlopen to serve it")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)

"""Writing credential files from a test without risking the real ones."""

import json

import pytest

from monzo_mcp import auth

#: The directory conftest isolates this test's paths into, set by its fixture.
#: None outside a test, which refuses every write.
ROOT = None


def write_isolated(path, data):
    """Write `data` to `path`, refusing any path outside this test's isolated directory.

    Checked here rather than trusted to the patched paths: if conftest's
    isolation slips, `auth`'s paths are the real credentials, and the harness
    pin that would notice runs after the tests that write. The directory is the
    one created for this test, not the system temp directory, which can contain
    a checkout and its default `config/`.
    """
    if ROOT is None or not path.resolve().is_relative_to(ROOT.resolve()):
        pytest.fail(f"refusing to write {path.name} outside the isolated directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))


def write_credentials(tokens=None, creds=None):
    """Write the token and client files at the paths `auth` reads."""
    if tokens is not None:
        write_isolated(auth.MONZO_TOKENS_PATH, tokens)
    if creds is not None:
        write_isolated(auth.MONZO_CLIENT_PATH, creds)

"""What the suite's own isolation guarantees, and what it does not.

Nothing you can feed the server exercises these: they are claims about the
fixtures in `conftest.py`, whose failure mode is silence. Delete a fixture and
every other test still passes - on a machine with no credentials because there
is nothing to see, and on one with credentials because the tests that would
notice patch their own paths anyway.
"""

import ast
import urllib.request
from pathlib import Path

import pytest

import monzo_mcp
from monzo_mcp import auth, config, db, helpers


def test_the_suite_cannot_reach_the_network():
    """Nothing else fails if the refusal is dropped, on any machine.

    The exception type is part of the claim: `Failed` derives from
    `BaseException`, which is what carries it out through
    `auto_sync_if_stale`'s `except Exception: pass`. An `AssertionError` here
    is absorbed by the code under test and reported as an ordinary sync
    failure - the silence the fixture exists to break.

    A loopback port nothing listens on, rather than a name: `.invalid` is
    reserved but still goes to the resolver, and a wildcard DNS answer turns
    the failure path of the offline guarantee into real egress.
    """
    with pytest.raises(pytest.fail.Exception, match="reached the network"):
        urllib.request.urlopen("http://127.0.0.1:1/")


def test_the_suite_cannot_see_real_credentials_or_the_real_database(tmp_path_factory):
    """Every module holding a copy of a path must hold an isolated one.

    Asserted against pytest's own temp root rather than against
    `config.CONFIG_DIR`, which the fixture also patches - comparing two
    patched values would pass whatever they were. This fires on any machine,
    including CI, which is the point: without it, deleting the fixture is
    invisible wherever no credential happens to exist.

    `db` is included because `auto_sync_if_stale` opens `DB_PATH` before any
    network call, so the refusal above does not stand between a tool test and
    the real cache.
    """
    base = tmp_path_factory.getbasetemp()
    for module in (config, auth, helpers):
        assert module.MONZO_TOKENS_PATH.is_relative_to(base), module.__name__
        assert module.MONZO_CLIENT_PATH.is_relative_to(base), module.__name__
    for module in (config, auth):
        assert module.CONFIG_DIR.is_relative_to(base), module.__name__
    for module in (config, db):
        assert module.DB_PATH.is_relative_to(base), module.__name__
    # `config.py` derives the credential paths from the config directory, so a
    # fixture that isolates them into unrelated directories would run the
    # suite against a layout production cannot produce.
    assert config.MONZO_TOKENS_PATH.parent == config.CONFIG_DIR
    assert not helpers.MONZO_TOKENS_PATH.exists()
    assert not db.DB_PATH.exists()


def test_only_the_expected_modules_hold_a_config_path():
    """A module binding one of these by value escapes the fixture.

    It keeps its own copy, so patching `config` afterwards does not move it -
    which is how `auth` and `helpers` came to need patching by name.

    This reads `from ... import` forms only, and that is its limit: a module
    reaching the value through the `config` object at import time
    (`from . import config` then `LEAK = config.MONZO_TOKENS_PATH` at module
    level) binds just as hard and is not visible here. The forms below are the
    ones anything in this package has ever used.
    """
    expected = {
        "auth.py": ["CONFIG_DIR", "MONZO_CLIENT_PATH", "MONZO_TOKENS_PATH"],
        "db.py": ["DB_PATH"],
        "helpers.py": ["MONZO_CLIENT_PATH", "MONZO_TOKENS_PATH"],
    }
    # `CONFIG_DIR` is watched because a path built from it at import - which
    # `auth` already imports - reaches the real directory with nothing else
    # complaining.
    watched = {"CONFIG_DIR", "DB_PATH", "MONZO_CLIENT_PATH", "MONZO_TOKENS_PATH"}
    # Both spellings of the same import, and keyed by path rather than by
    # stem, so a future `tools/db.py` cannot merge into `db`'s entry.
    modules = ("config", f"{monzo_mcp.__name__}.config")
    package = Path(monzo_mcp.__file__).parent

    holders = {}
    for source in sorted(package.rglob("*.py")):
        for node in ast.walk(ast.parse(source.read_text())):
            if not isinstance(node, ast.ImportFrom) or node.module not in modules:
                continue
            bound = sorted(a.name for a in node.names if a.name in watched)
            if bound:
                holders.setdefault(source.relative_to(package).as_posix(), []).extend(bound)
    holders = {name: sorted(set(names)) for name, names in holders.items()}

    assert holders == expected, (
        f"a module binds a config path at import that the harness does not patch: {holders}"
    )


def test_the_fixture_clears_the_token_cache():
    """`auth` caches tokens in module state, which outlives the files.

    Left populated by one test it hands the next a working access token with
    no credential file anywhere. The property is only observable across two
    tests, and a pair of ordered tests asserting it passes vacuously when
    either is run alone and breaks under any run-order randomiser - so the
    fixture is read instead, the shape this suite already uses for claims
    nothing can execute.
    """
    source = (Path(__file__).parent / "conftest.py").read_text()
    cleared = {
        node.args[1].value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) == "setattr"
        and len(node.args) == 3
        and isinstance(node.args[1], ast.Constant)
    }
    assert {"_cached_tokens", "_cached_creds"} <= cleared, (
        f"the token cache is no longer cleared between tests: {sorted(cleared)}"
    )

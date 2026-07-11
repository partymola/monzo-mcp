"""Tests for config path resolution.

`config` computes its paths at import time from environment variables, so
these reload the module under a patched environment and assert on the reloaded
attributes. A cleanup reload restores the module to its default state so other
tests are unaffected.
"""

import importlib
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from monzo_mcp import config


def _reload_with_env(env):
    """Reload config with os.environ replaced by `env`; return the module."""
    with patch.dict(os.environ, env, clear=True):
        return importlib.reload(config)


class TestConfigOverrides(unittest.TestCase):
    def tearDown(self):
        # Restore the module to whatever the ambient environment produces so a
        # patched CONFIG_DIR/DB_PATH does not leak into later tests.
        importlib.reload(config)

    def test_config_dir_override(self):
        cfg = _reload_with_env({"MONZO_MCP_CONFIG_DIR": "/custom/cfg"})
        self.assertEqual(cfg.CONFIG_DIR, Path("/custom/cfg"))
        # Credential paths are derived from CONFIG_DIR, so they must follow it.
        self.assertEqual(cfg.MONZO_CLIENT_PATH, Path("/custom/cfg/monzo_client.json"))
        self.assertEqual(cfg.MONZO_TOKENS_PATH, Path("/custom/cfg/monzo_tokens.json"))

    def test_db_path_override(self):
        cfg = _reload_with_env({"MONZO_MCP_DB_PATH": "/custom/data/monzo.db"})
        self.assertEqual(cfg.DB_PATH, Path("/custom/data/monzo.db"))

    def test_overrides_are_independent(self):
        # Overriding the DB path must not move CONFIG_DIR off its package default.
        cfg = _reload_with_env({"MONZO_MCP_DB_PATH": "/only/db.sqlite"})
        self.assertEqual(cfg.DB_PATH, Path("/only/db.sqlite"))
        self.assertEqual(cfg.CONFIG_DIR, cfg._PACKAGE_ROOT / "config")

    def test_defaults_without_env(self):
        cfg = _reload_with_env({})
        self.assertEqual(cfg.CONFIG_DIR, cfg._PACKAGE_ROOT / "config")
        self.assertEqual(cfg.DB_PATH, cfg._PACKAGE_ROOT / "monzo.db")
        self.assertEqual(cfg.MONZO_CLIENT_PATH, cfg._PACKAGE_ROOT / "config" / "monzo_client.json")
        self.assertEqual(cfg.MONZO_TOKENS_PATH, cfg._PACKAGE_ROOT / "config" / "monzo_tokens.json")

    def test_package_root_is_repo_root(self):
        # _PACKAGE_ROOT is three levels up from src/monzo_mcp/config.py, i.e. the
        # repo root that holds pyproject.toml.
        cfg = _reload_with_env({})
        self.assertTrue((cfg._PACKAGE_ROOT / "pyproject.toml").exists())


class TestConfigConstants(unittest.TestCase):
    def test_api_constants(self):
        self.assertEqual(config.MONZO_API_BASE, "https://api.monzo.com")
        self.assertEqual(config.MONZO_AUTH_URL, "https://auth.monzo.com/")
        self.assertEqual(config.MONZO_TOKEN_URL, "https://api.monzo.com/oauth2/token")
        self.assertEqual(config.MONZO_CALLBACK_PORT, 6600)


if __name__ == "__main__":
    unittest.main()

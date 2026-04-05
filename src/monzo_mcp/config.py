"""Path resolution and constants for the Monzo MCP server.

All paths are derived from environment variables or the package location.
No hardcoded personal paths.
"""

import os
from pathlib import Path

# Package root: monzo-mcp/ (three levels up from this file)
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent

# Config directory: stores OAuth credentials and tokens (gitignored)
CONFIG_DIR = Path(os.environ.get("MONZO_MCP_CONFIG_DIR", _PACKAGE_ROOT / "config"))

# SQLite database path
DB_PATH = Path(os.environ.get("MONZO_MCP_DB_PATH", _PACKAGE_ROOT / "monzo.db"))

# Credential file paths
MONZO_CLIENT_PATH = CONFIG_DIR / "monzo_client.json"
MONZO_TOKENS_PATH = CONFIG_DIR / "monzo_tokens.json"

# Monzo API
MONZO_API_BASE = "https://api.monzo.com"
MONZO_AUTH_URL = "https://auth.monzo.com/"
MONZO_TOKEN_URL = "https://api.monzo.com/oauth2/token"
MONZO_CALLBACK_PORT = 6600

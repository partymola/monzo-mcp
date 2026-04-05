"""Monzo API client with automatic token refresh."""

import json
import logging
import urllib.error
import urllib.request

from .auth import refresh_token
from .config import MONZO_API_BASE

logger = logging.getLogger(__name__)


class MonzoAuthError(Exception):
    """Token expired or invalid, re-auth needed."""


class MonzoSCAError(Exception):
    """Strong Customer Authentication required (outside SCA window)."""


class MonzoAPIError(Exception):
    """General API error."""


def get(path: str) -> dict:
    """Make an authenticated GET request to the Monzo API.

    Automatically refreshes the access token if expired.
    """
    token = refresh_token()
    url = f"{MONZO_API_BASE}{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "User-Agent": "monzo-mcp/0.1",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise MonzoAuthError("Access token invalid. Run: monzo-mcp auth") from e
        if e.code == 403:
            raise MonzoSCAError("SCA required - approve in Monzo app") from e
        raise MonzoAPIError(f"Monzo API error {e.code}") from e
    except urllib.error.URLError as e:
        raise MonzoAPIError(f"Network error: {e}") from e

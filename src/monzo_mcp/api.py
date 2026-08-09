"""Monzo API client with automatic token refresh."""

import http.client
import json
import logging
import urllib.error
import urllib.request

from .auth import RefreshNetworkError, TokenRefused, refresh_token
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
    # refresh_token classifies its own failures, so there is no exception tuple
    # here to get wrong. The messages are fixed rather than built from the
    # original, which can carry an absolute config path into a response the MCP
    # client sees.
    try:
        token = refresh_token()
    except TokenRefused as e:
        raise MonzoAuthError("Could not obtain an access token. Run: monzo-mcp auth") from e
    except RefreshNetworkError as e:
        raise MonzoAPIError("Network error. Check your connection.") from e

    url = f"{MONZO_API_BASE}{path}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "monzo-mcp/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise MonzoAuthError("Access token invalid. Run: monzo-mcp auth") from e
        if e.code == 403:
            raise MonzoSCAError("SCA required - approve in Monzo app") from e
        raise MonzoAPIError(f"Monzo API error {e.code}") from e
    except (OSError, http.client.HTTPException) as e:
        # Wider than URLError, for the reason auth.py already documents:
        # urlopen wraps only connect-phase failures in it, so a read timeout
        # or a reset connection arrives bare, and a truncated response raises
        # from http.client, which is not an OSError at all.
        raise MonzoAPIError("Network error. Check your connection.") from e

    # Parsing is its own failure cause, not a transport one, and it is left to
    # json.loads on the raw bytes so an undecodable body and one that is not
    # JSON land in the same place: both raise ValueError, which none of the
    # handlers above catch. Either used to escape the sync loop, whose handlers
    # are for the Monzo types, leaving no record that syncing had stopped.
    try:
        body = json.loads(raw)
    except ValueError as e:
        raise MonzoAPIError("Monzo returned an unreadable response.") from e

    if not isinstance(body, dict):
        raise MonzoAPIError("Monzo returned an unexpected response shape.")

    return body

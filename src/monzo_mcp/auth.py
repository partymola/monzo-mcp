"""Monzo OAuth setup and token management."""

import json
import logging
import sys
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode

from .config import (
    CONFIG_DIR, MONZO_CLIENT_PATH, MONZO_TOKENS_PATH,
    MONZO_AUTH_URL, MONZO_TOKEN_URL, MONZO_CALLBACK_PORT,
)

logger = logging.getLogger(__name__)

# In-memory token cache to avoid re-reading JSON files on every API call
_cached_tokens = None
_cached_creds = None


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _load_json(path):
    return json.loads(path.read_text())


def refresh_token() -> str:
    """Return a valid access token, refreshing if expired.

    Checks expiry with a 5-minute buffer. If expired, uses the refresh_token
    grant to obtain new tokens and updates the token file.
    """
    global _cached_tokens, _cached_creds

    if _cached_tokens is None:
        _cached_tokens = _load_json(MONZO_TOKENS_PATH)
    if _cached_creds is None:
        _cached_creds = _load_json(MONZO_CLIENT_PATH)

    if datetime.now(timezone.utc).timestamp() < _cached_tokens.get("expiry", 0) - 300:
        return _cached_tokens["access_token"]

    if not _cached_tokens.get("refresh_token"):
        logger.error("Token expired and no refresh token. Run: monzo-mcp auth")
        raise RuntimeError("Token expired and no refresh token. Run: monzo-mcp auth")

    data = urlencode({
        "grant_type": "refresh_token",
        "client_id": _cached_creds["client_id"],
        "client_secret": _cached_creds["client_secret"],
        "refresh_token": _cached_tokens["refresh_token"],
    }).encode()

    req = urllib.request.Request(MONZO_TOKEN_URL, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            new_tokens = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        logger.error("Token refresh failed: %s", e)
        raise RuntimeError(f"Token refresh failed: {e}. Run: monzo-mcp auth") from e

    _cached_tokens = {
        "access_token": new_tokens["access_token"],
        "refresh_token": new_tokens.get("refresh_token", _cached_tokens["refresh_token"]),
        "token_type": new_tokens.get("token_type", "Bearer"),
        "expiry": datetime.now(timezone.utc).timestamp() + new_tokens.get("expires_in", 86400),
    }
    _save_json(MONZO_TOKENS_PATH, _cached_tokens)
    return _cached_tokens["access_token"]


def setup_auth():
    """Interactive OAuth setup. Prompts for credentials, opens browser, exchanges code."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    creds = None
    if MONZO_CLIENT_PATH.exists():
        creds = _load_json(MONZO_CLIENT_PATH)
        print(f"Existing client_id: {creds['client_id'][:12]}...")
        resp = input("Re-use existing credentials? [Y/n] ").strip().lower()
        if resp in ("n", "no"):
            creds = None

    if not creds:
        print("Register an OAuth client at https://developers.monzo.com/")
        print(f"Set redirect URL to: http://localhost:{MONZO_CALLBACK_PORT}/callback")
        client_id = input("Client ID: ").strip()
        client_secret = input("Client secret: ").strip()
        if not client_id or not client_secret:
            print("Error: both client_id and client_secret required.", file=sys.stderr)
            sys.exit(1)
        creds = {"client_id": client_id, "client_secret": client_secret}
        _save_json(MONZO_CLIENT_PATH, creds)
        print("Credentials saved.")

    state = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    redirect_uri = f"http://localhost:{MONZO_CALLBACK_PORT}/callback"
    auth_url = MONZO_AUTH_URL + "?" + urlencode({
        "client_id": creds["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
    })

    auth_code = None

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal auth_code
            qs = parse_qs(urlparse(self.path).query)
            if "code" in qs:
                auth_code = qs["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h1>Auth complete - you can close this tab.</h1>")
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Missing code parameter")

        def log_message(self, format, *a):
            pass

    print(f"\nOpening browser for Monzo auth...")
    print(f"URL: {auth_url}\n")
    webbrowser.open(auth_url)

    server = HTTPServer(("localhost", MONZO_CALLBACK_PORT), CallbackHandler)
    print("Waiting for callback... (approve in Monzo app)")
    server.handle_request()

    if not auth_code:
        print("Error: no auth code received.", file=sys.stderr)
        sys.exit(1)

    token_data = urlencode({
        "grant_type": "authorization_code",
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "redirect_uri": redirect_uri,
        "code": auth_code,
    }).encode()

    req = urllib.request.Request(MONZO_TOKEN_URL, data=token_data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            tokens = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        print(f"Error exchanging code: {e}", file=sys.stderr)
        sys.exit(1)

    token_store = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token", ""),
        "token_type": tokens.get("token_type", "Bearer"),
        "expiry": datetime.now(timezone.utc).timestamp() + tokens.get("expires_in", 86400),
    }
    _save_json(MONZO_TOKENS_PATH, token_store)
    print("Monzo auth complete. Tokens saved.")
    print("IMPORTANT: Approve the login in your Monzo app within 5 minutes for full access.")
    print("\nAfter approving, use monzo_sync to fetch full transaction history.")
    print("(The 5-minute SCA window allows access to ALL transactions, not just the last 90 days.)")

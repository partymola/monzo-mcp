"""Monzo OAuth setup and token management."""

import http.client
import json
import logging
import os
import socket
import sys
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from .config import (
    CONFIG_DIR,
    MONZO_AUTH_URL,
    MONZO_CALLBACK_HOST,
    MONZO_CALLBACK_PORT,
    MONZO_CLIENT_PATH,
    MONZO_TOKEN_URL,
    MONZO_TOKENS_PATH,
)

logger = logging.getLogger(__name__)


# RFC 6749 defines the token endpoint's refusals as 400, with 401 for a bad
# client. 403 is deliberately absent: a WAF or bot-protection block returns it
# with no opinion about the grant, and this client already reads 403 on a data
# request as an SCA prompt.
_REFUSAL_CODES = frozenset({400, 401})


class _CallbackServer(HTTPServer):
    """Refuse to share the port the authorisation code arrives on.

    Deliberate, and not what `HTTPServer` does by default: it asks for address
    reuse, which on Windows is what lets another process bind over this
    listener and take the code. Both halves below are load-bearing here, and
    which one carries the weight depends on `MONZO_MCP_CALLBACK_HOST` - the
    reasoning is in AGENTS.md. Pinned by TestTheCallbackPortIsNotShared, which
    drives this method's Windows branch on a POSIX runner.
    """

    allow_reuse_address = sys.platform != "win32"
    # SO_REUSEPORT is the POSIX-side version of the same hazard; nothing sets
    # this, and nothing should.
    allow_reuse_port = False

    def server_bind(self):
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class TokenRefused(RuntimeError):
    """The server judged the credentials and rejected them.

    The only failure that warrants telling the user to re-authorise, which
    rewrites the token file the syncing host owns.
    """


class RefreshNetworkError(RuntimeError):
    """The refresh request never got an answer.

    Subclasses RuntimeError so existing callers are unaffected, but is
    distinguishable: an unreachable server says nothing about whether the
    credentials are still good, and telling the user to re-authorise would
    rewrite a token file the syncing host owns.
    """


# In-memory token cache to avoid re-reading JSON files on every API call
_cached_tokens = None
_cached_creds = None


def _save_json(path, data):
    """Write a credential file at owner-only permissions from the outset.

    The mode is set by os.open at creation rather than by a chmod afterwards:
    a chmod leaves a window in which the token sits in a world-readable file,
    and no chmod at all leaves it there permanently.

    The mode applies on POSIX. Windows ignores it and governs access by
    inherited ACLs, so this narrows nothing there - it is not a claim about
    every platform.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        # Best-effort: O_TRUNC has already emptied the file, so a
        # permissions failure must not take the token with it. Skipped where
        # there is no fchmod - warning on every save on Windows, which has
        # nothing to narrow, would be noise rather than information.
        if hasattr(os, "fchmod"):
            try:
                os.fchmod(handle.fileno(), 0o600)
            except OSError:
                logger.warning("Could not tighten permissions on %s", path.name)
        handle.write(json.dumps(data, indent=2))


def _load_json(path):
    """Read a credential file as a dict, or say why the credentials are unusable.

    Classified here rather than left to the caller: a file that is absent,
    unreadable or not a JSON object means there are no usable credentials,
    which is a refusal - unlike a transport failure it will not clear on its
    own, and the user does have to re-authorise.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise TokenRefused(f"{path.name} is missing or unreadable. Run: monzo-mcp auth") from e
    if not isinstance(data, dict):
        raise TokenRefused(f"{path.name} is malformed. Run: monzo-mcp auth")
    return data


def _refresh_token() -> str:
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
        raise TokenRefused("Token expired and no refresh token. Run: monzo-mcp auth")

    data = urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": _cached_creds["client_id"],
            "client_secret": _cached_creds["client_secret"],
            "refresh_token": _cached_tokens["refresh_token"],
        }
    ).encode()

    req = urllib.request.Request(MONZO_TOKEN_URL, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        # Checked before OSError, which it subclasses. Listed by what the code
        # says about the credentials rather than by range: a bad grant is
        # refused with 400/401/403, while 429 carries no judgement at all.
        if e.code in _REFUSAL_CODES:
            logger.error("Token refresh refused with HTTP %s", e.code)
            raise TokenRefused("Token refresh failed. Run: monzo-mcp auth") from e
        logger.error("Token refresh got HTTP %s from the server", e.code)
        raise RefreshNetworkError("Monzo could not answer the refresh request.") from e
    except OSError as e:
        # Not just URLError: urlopen wraps only connect-phase failures in it,
        # so a read timeout or a reset connection arrives bare.
        logger.error("Token refresh could not reach the server")
        raise RefreshNetworkError("Could not reach Monzo to refresh the token.") from e

    try:
        new_tokens = json.loads(raw)
    except ValueError as e:
        logger.error("Token refresh got a response that is not JSON")
        raise RefreshNetworkError("Monzo returned an unreadable response.") from e

    if not isinstance(new_tokens, dict) or "access_token" not in new_tokens:
        logger.error("Token refresh returned an unexpected response shape")
        raise TokenRefused("Token refresh failed. Run: monzo-mcp auth")

    _cached_tokens = {
        "access_token": new_tokens["access_token"],
        "refresh_token": new_tokens.get("refresh_token", _cached_tokens["refresh_token"]),
        "token_type": new_tokens.get("token_type", "Bearer"),
        "expiry": datetime.now(timezone.utc).timestamp() + new_tokens.get("expires_in", 86400),
    }
    _save_json(MONZO_TOKENS_PATH, _cached_tokens)
    return _cached_tokens["access_token"]


def refresh_token() -> str:
    """Return a valid access token, refreshing if expired.

    The boundary that classifies every way obtaining a token can fail. Only
    TokenRefused means the credentials were rejected; everything else becomes
    RefreshNetworkError, by construction rather than by listing the exception
    types that happen to occur. Enumerating them is what went wrong before:
    each round of fixes found another type nobody had thought of - a bare
    OSError, an http.client exception that is not an OSError at all, a decode
    failure that is a ValueError - and each was graded a dead credential.

    A bug inside the refresh therefore reports as a network failure rather
    than a refusal. That is the safe direction: it is still recorded and still
    visible, and it does not tell anyone to rotate a shared token file.
    """
    try:
        return _refresh_token()
    except (TokenRefused, RefreshNetworkError):
        raise
    except Exception as e:
        logger.error("Token refresh failed: %s", type(e).__name__)
        raise RefreshNetworkError("Could not obtain a token from Monzo.") from e


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
    auth_url = (
        MONZO_AUTH_URL
        + "?"
        + urlencode(
            {
                "client_id": creds["client_id"],
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "state": state,
            }
        )
    )

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

    # Bind before opening the browser: the listener no longer asks to share the
    # port, so a busy one is now a real failure, and sending the user to an
    # authorisation page whose redirect has nowhere to land wastes the attempt.
    try:
        server = _CallbackServer((MONZO_CALLBACK_HOST, MONZO_CALLBACK_PORT), CallbackHandler)
    except OSError:
        print(
            f"Port {MONZO_CALLBACK_PORT} on {MONZO_CALLBACK_HOST} is in use, so the callback "
            "cannot be received. It must match the redirect URL registered with Monzo and so "
            "cannot be changed. On Windows a socket from a recent `monzo-mcp auth` may still "
            "be closing; retry once it has.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("\nOpening browser for Monzo auth...")
    print(f"URL: {auth_url}\n")
    webbrowser.open(auth_url)

    print("Waiting for callback... (approve in Monzo app)")
    server.handle_request()

    if not auth_code:
        print("Error: no auth code received.", file=sys.stderr)
        sys.exit(1)

    token_data = urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "redirect_uri": redirect_uri,
            "code": auth_code,
        }
    ).encode()

    req = urllib.request.Request(MONZO_TOKEN_URL, data=token_data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
    except (OSError, http.client.HTTPException) as e:
        # Same widening as the refresh path: urlopen wraps only connect-phase
        # failures, so a read timeout arrives bare and a truncated response
        # raises from http.client, which is not an OSError at all.
        print(f"Error exchanging code: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        tokens = json.loads(raw)
    except ValueError:
        print("Error exchanging code: the response was not readable.", file=sys.stderr)
        sys.exit(1)

    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        # Checked before indexing: the auth code is single-use, so a raw
        # traceback here costs the user the whole browser flow again.
        print("Error exchanging code: no token in the response.", file=sys.stderr)
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

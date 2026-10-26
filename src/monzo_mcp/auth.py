"""Monzo OAuth setup and token management."""

import contextlib
import http.client
import json
import logging
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
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
from .errors import MonzoError

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


class TokenRefused(MonzoError, RuntimeError):
    """The server judged the credentials and rejected them.

    The only failure that warrants telling the user to re-authorise, which
    rewrites the token file the syncing host owns.
    """


class RefreshNetworkError(MonzoError, RuntimeError):
    """The refresh request never got an answer.

    Keeps RuntimeError so existing callers are unaffected, but is
    distinguishable: an unreachable server says nothing about whether the
    credentials are still good, and telling the user to re-authorise would
    rewrite a token file the syncing host owns.
    """


# Tool calls run in worker threads. Without this, one can read the token file
# while another is half-way through saving it, and two can spend the same
# refresh token.
_token_lock = threading.Lock()

# A refreshed grant whose save failed: (the refresh token the file held when
# the save failed, the grant). Monzo rotates the refresh token, so the file's
# may already be spent and this the only live copy. It is used only while the
# file still holds that token or cannot be read.
_unsaved = None


def _save_json(path, data):
    """Replace a credential file in one step, owner-only from the outset.

    Written to a new file beside it, synced, and renamed over it: `os.replace`
    is atomic within a directory on POSIX, so a reader sees the old file or
    the new one and never part of either, and a failed write leaves the old
    one in place. mkstemp creates the file 0600 before anything is written,
    under a name no other writer shares. A symlink is followed, so the file it
    points at is the one replaced. A kill between the write and the rename can
    leave a temp file, at 0600. Windows ignores the mode.
    """
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(data, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        _replace(tmp, path)
    except BaseException:
        # Whatever stops the cleanup, the error to report is the one above.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# Windows refuses to replace a file another process has open, and every
# server sharing the token file opens it on each request. Each read is brief,
# so a short wait clears it; anywhere else the refusal is a real permission
# fault and is raised at once.
_REPLACE_ATTEMPTS = 5
_REPLACE_BACKOFF_SECONDS = 0.05


def _replace(src, dst):
    for attempt in range(1, _REPLACE_ATTEMPTS + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if sys.platform != "win32" or attempt == _REPLACE_ATTEMPTS:
                raise
            time.sleep(_REPLACE_BACKOFF_SECONDS * attempt)


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


def _reread_tokens():
    """The token file as it is now, or None if it cannot be read."""
    try:
        return _load_json(MONZO_TOKENS_PATH)
    except TokenRefused:
        return None


def _expiry(tokens):
    """The stored expiry, or 0 when it is unusable."""
    value = tokens.get("expiry", 0)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return 0
    return value


def _usable(tokens):
    """Whether the stored access token can be sent, with the 5-minute buffer."""
    now = datetime.now(timezone.utc).timestamp()
    return bool(tokens.get("access_token")) and now < _expiry(tokens) - 300


def _replaced_by_a_later_grant(on_disk, tokens):
    """Whether `on_disk` is a grant saved since `tokens` was read, and usable.

    Later rather than merely different, and usable rather than merely later,
    because Monzo rotates the refresh token: the one just issued is known to
    be live and another copy's usually is not, so saving over that copy is
    what keeps it.
    """
    return _expiry(on_disk) > _expiry(tokens) and _usable(on_disk)


def _current_tokens():
    """The token file, or the grant held after a failed save while it still applies."""
    global _unsaved
    if _unsaved is not None:
        file_token, held = _unsaved
        on_disk = _reread_tokens()
        if on_disk is None or on_disk.get("refresh_token") == file_token:
            _save(held, file_token)
            return held
        _unsaved = None
    return _load_json(MONZO_TOKENS_PATH)


def _save(refreshed, file_token):
    """Save a refreshed grant, or hold it if the save fails.

    `file_token` is the refresh token the file holds, which is what a held
    grant is matched against on the next call.
    """
    global _unsaved
    try:
        _save_json(MONZO_TOKENS_PATH, refreshed)
    except OSError:
        # Once, when the grant starts being held: the retry runs on every call.
        if _unsaved is None:
            logger.warning("Could not save the refreshed token; holding it until a save succeeds")
        _unsaved = (file_token, refreshed)
    else:
        _unsaved = None


def _refresh_token() -> str:
    """Return a valid access token, refreshing if expired.

    Checks expiry with a 5-minute buffer. If expired, uses the refresh_token
    grant, and saves the result unless the token file now holds a later usable
    grant.

    The token file is read on every call and the client file on every refresh,
    and nothing is kept between calls: `monzo-mcp auth` replaces them from
    another process while the server runs. The one exception is a grant whose
    save failed, held by `_current_tokens` while the file still holds the
    token it replaced.
    """
    global _unsaved
    tokens = _current_tokens()
    file_token = _unsaved[0] if _unsaved is not None else tokens.get("refresh_token")

    if _usable(tokens):
        return tokens["access_token"]

    if not tokens.get("refresh_token"):
        logger.error("Token expired and no refresh token. Run: monzo-mcp auth")
        raise TokenRefused("Token expired and no refresh token. Run: monzo-mcp auth")

    creds = _load_json(MONZO_CLIENT_PATH)
    data = urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "refresh_token": tokens["refresh_token"],
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
            # A refusal judges the refresh token sent and nothing else. If the
            # file now holds a different one, a concurrent refresh or
            # `monzo-mcp auth` replaced it, and the next call refreshes from it.
            on_disk = _reread_tokens()
            if on_disk is None:
                # Nothing to compare the refusal with. A held grant was the
                # token refused, so it goes, and the next call's read of the
                # file decides.
                _unsaved = None
                logger.warning("Token refresh refused while the token file was unreadable")
                raise RefreshNetworkError("The token file could not be read.") from e
            if on_disk.get("refresh_token") not in (tokens.get("refresh_token"), file_token):
                _unsaved = None
                if _usable(on_disk):
                    logger.warning("Token refresh refused, but the token file was replaced")
                    return on_disk["access_token"]
                logger.warning("Token refresh refused while the token file was changing")
                raise RefreshNetworkError("The token file changed during the refresh.") from e
            _unsaved = None
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

    refreshed = {
        "access_token": new_tokens["access_token"],
        "refresh_token": new_tokens.get("refresh_token", tokens["refresh_token"]),
        "token_type": new_tokens.get("token_type", "Bearer"),
        "expiry": datetime.now(timezone.utc).timestamp() + new_tokens.get("expires_in", 86400),
    }

    # Saving over a grant written during the request loses it. Anything else is
    # saved over: saves replace the file in one step, so an unreadable one is
    # gone or corrupt rather than part-way through being written.
    on_disk = _reread_tokens()
    if on_disk is not None and _replaced_by_a_later_grant(on_disk, tokens):
        logger.warning("Token file was replaced during refresh; using it instead")
        _unsaved = None
        return on_disk["access_token"]

    # Keyed to what the file holds now, which may have changed during the
    # request: that is what the next call compares against.
    _save(refreshed, on_disk.get("refresh_token") if on_disk is not None else file_token)
    return refreshed["access_token"]


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
        with _token_lock:
            return _refresh_token()
    except (TokenRefused, RefreshNetworkError):
        raise
    except Exception as e:
        logger.error("Token refresh failed: %s", type(e).__name__)
        raise RefreshNetworkError("Could not obtain a token from Monzo.") from e


def _save_or_exit(path, data, what):
    """Save for `setup_auth`, ending in a message rather than a traceback.

    The type, never the text: an OSError's message names this install's paths.
    """
    try:
        _save_json(path, data)
    except OSError as e:
        print(f"Error: the {what} could not be saved ({type(e).__name__}).", file=sys.stderr)
        sys.exit(1)


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
        _save_or_exit(MONZO_CLIENT_PATH, creds, "credentials")
        print("Credentials saved.")

    state = secrets.token_urlsafe(32)
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
            # Before the code is read, not after: a mismatch must not spend the
            # code it arrived with, even though nothing would be stored.
            #
            # Encoded, because compare_digest refuses a non-ASCII str and both
            # routes in reach one: parse_qs decodes percent-escapes with
            # errors="replace", and http.server reads the request line as
            # iso-8859-1. Comparing str crashes the handler on `state=%80`.
            if not secrets.compare_digest(qs.get("state", [""])[0].encode(), state.encode()):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"State did not match the request that started this flow.")
                return
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
        # raises from http.client, which is not an OSError at all. The status
        # or the type, never the text, which for a TLS or file failure names
        # a path.
        what = f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else type(e).__name__
        print(f"Error exchanging code: {what}.", file=sys.stderr)
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
    _save_or_exit(MONZO_TOKENS_PATH, token_store, "tokens")
    print("Monzo auth complete. Tokens saved.")
    print("IMPORTANT: Approve the login in your Monzo app within 5 minutes for full access.")
    print("\nAfter approving, use monzo_sync to fetch full transaction history.")
    print("(The 5-minute SCA window allows access to ALL transactions, not just the last 90 days.)")

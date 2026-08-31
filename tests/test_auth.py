"""Tests for OAuth token management and interactive setup.

No network and no real OAuth: the HTTP layer (`urllib.request.urlopen`), the
local callback HTTPServer, `input`, `webbrowser`, and the JSON file I/O are all
mocked. Time is pinned via a datetime subclass so the 5-minute expiry buffer and
the stored expiry timestamps are exact, not approximate.
"""

import ast
import inspect
import io
import json
import os
import pathlib
import socket
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, quote, urlparse

from monzo_mcp import auth

# Fixed "now" so expiry maths is deterministic.
NOW_TS = 1_700_000_000
CREDS = {"client_id": "cid_test", "client_secret": "csecret_test"}


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.fromtimestamp(NOW_TS, tz)


class _FakeResp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


# --------------------------------------------------------------------------- #
# refresh_token
# --------------------------------------------------------------------------- #
class TestRefreshToken(unittest.TestCase):
    def tearDown(self):
        # Belt-and-braces: the module-level token/cred caches are always mutated
        # inside patch.object here, but reset them so no state can leak to a
        # future test that forgets to patch (which could read the real files).
        auth._cached_tokens = None
        auth._cached_creds = None

    def _refresh(self, tokens, *, urlopen=None, creds=CREDS, save_capture=None):
        """Run refresh_token with the module cache preloaded (no file reads)."""
        saves = save_capture if save_capture is not None else {}
        stack = [
            patch.object(auth, "_cached_tokens", dict(tokens)),
            patch.object(auth, "_cached_creds", dict(creds)),
            patch.object(auth, "datetime", _FixedDatetime),
            patch.object(auth, "_save_json", lambda path, data: saves.update(data)),
        ]
        if urlopen is not None:
            stack.append(patch("urllib.request.urlopen", urlopen))
        with self._nest(stack):
            return auth.refresh_token()

    class _nest:
        def __init__(self, cms):
            self._cms = cms

        def __enter__(self):
            for cm in self._cms:
                cm.__enter__()
            return self

        def __exit__(self, *exc):
            for cm in reversed(self._cms):
                cm.__exit__(*exc)
            return False

    def test_valid_token_returned_without_refresh(self):
        # Expiry 301s ahead -> just outside the 300s buffer -> no refresh.
        called = {"n": 0}

        def urlopen(req, timeout=None):
            called["n"] += 1
            return _FakeResp({})

        tok = self._refresh(
            {"access_token": "live", "refresh_token": "r1", "expiry": NOW_TS + 301},
            urlopen=urlopen,
        )
        self.assertEqual(tok, "live")
        self.assertEqual(called["n"], 0)

    def test_token_at_buffer_boundary_is_refreshed(self):
        # Source guard is `now < expiry - 300`. At expiry = now + 300 the guard
        # is `now < now` -> False -> refresh. Pinning exactly 300 (not 299) makes
        # an off-by-one such as `- 300` -> `- 299` fail this test.
        captured = {}

        def urlopen(req, timeout=None):
            captured["req"] = req
            return _FakeResp({"access_token": "fresh", "refresh_token": "r2", "expires_in": 3600})

        saves = {}
        tok = self._refresh(
            {"access_token": "stale", "refresh_token": "r1", "expiry": NOW_TS + 300},
            urlopen=urlopen,
            save_capture=saves,
        )
        self.assertEqual(tok, "fresh")
        # The refresh POST carries the refresh_token grant with the stored creds.
        body = captured["req"].data
        self.assertIn(b"grant_type=refresh_token", body)
        self.assertIn(b"refresh_token=r1", body)
        self.assertIn(b"client_id=cid_test", body)
        # New expiry = fixed now + expires_in; rotated refresh token persisted.
        self.assertEqual(saves["expiry"], NOW_TS + 3600)
        self.assertEqual(saves["access_token"], "fresh")
        self.assertEqual(saves["refresh_token"], "r2")
        self.assertEqual(saves["token_type"], "Bearer")

    def test_refresh_keeps_old_refresh_token_when_response_omits_it(self):
        def urlopen(req, timeout=None):
            return _FakeResp({"access_token": "fresh", "expires_in": 3600})

        saves = {}
        self._refresh(
            {"access_token": "stale", "refresh_token": "keep_me", "expiry": 0},
            urlopen=urlopen,
            save_capture=saves,
        )
        self.assertEqual(saves["refresh_token"], "keep_me")

    def test_refresh_defaults_expires_in_to_one_day(self):
        def urlopen(req, timeout=None):
            return _FakeResp({"access_token": "fresh", "refresh_token": "r2"})

        saves = {}
        self._refresh(
            {"access_token": "stale", "refresh_token": "r1", "expiry": 0},
            urlopen=urlopen,
            save_capture=saves,
        )
        self.assertEqual(saves["expiry"], NOW_TS + 86400)

    def test_expired_without_refresh_token_raises(self):
        called = {"n": 0}

        def urlopen(req, timeout=None):
            called["n"] += 1
            return _FakeResp({})

        with self.assertRaises(RuntimeError) as ctx:
            self._refresh(
                {"access_token": "stale", "refresh_token": "", "expiry": 0},
                urlopen=urlopen,
            )
        self.assertIn("refresh token", str(ctx.exception).lower())
        self.assertEqual(called["n"], 0)  # never hit the network

    def test_refresh_network_error_does_not_advise_reauthorising(self):
        """A server that cannot be reached says nothing about the credentials.

        Advising re-authorisation here rewrites the token file the syncing
        host owns, in answer to something that clears on its own.
        """

        def urlopen(req, timeout=None):
            raise urllib.error.URLError("connection refused")

        with self.assertRaises(auth.RefreshNetworkError) as ctx:
            self._refresh(
                {"access_token": "stale", "refresh_token": "r1", "expiry": 0},
                urlopen=urlopen,
            )
        self.assertNotIn("monzo-mcp auth", str(ctx.exception))

    def test_refresh_refused_by_the_server_advises_reauthorising(self):
        def urlopen(req, timeout=None):
            raise urllib.error.HTTPError("https://example.invalid", 401, "no", {}, io.BytesIO(b""))

        with self.assertRaises(RuntimeError) as ctx:
            self._refresh(
                {"access_token": "stale", "refresh_token": "r1", "expiry": 0},
                urlopen=urlopen,
            )
        self.assertNotIsInstance(ctx.exception, auth.RefreshNetworkError)
        self.assertIn("monzo-mcp auth", str(ctx.exception))

    def test_a_read_timeout_is_not_treated_as_a_refusal(self):
        """urlopen wraps only connect-phase errors, so this arrives bare."""

        def urlopen(req, timeout=None):
            raise TimeoutError("timed out")

        with self.assertRaises(auth.RefreshNetworkError):
            self._refresh(
                {"access_token": "stale", "refresh_token": "r1", "expiry": 0},
                urlopen=urlopen,
            )

    def test_a_reset_connection_is_not_treated_as_a_refusal(self):
        def urlopen(req, timeout=None):
            raise ConnectionResetError("reset")

        with self.assertRaises(auth.RefreshNetworkError):
            self._refresh(
                {"access_token": "stale", "refresh_token": "r1", "expiry": 0},
                urlopen=urlopen,
            )

    def test_a_forbidden_response_is_not_treated_as_a_refusal(self):
        """403 is what a WAF returns, and this client reads it as SCA elsewhere."""

        def urlopen(req, timeout=None):
            raise urllib.error.HTTPError("https://example.invalid", 403, "no", {}, io.BytesIO(b""))

        with self.assertRaises(auth.RefreshNetworkError):
            self._refresh(
                {"access_token": "stale", "refresh_token": "r1", "expiry": 0},
                urlopen=urlopen,
            )

    def test_a_bad_request_is_treated_as_a_refusal(self):
        def urlopen(req, timeout=None):
            raise urllib.error.HTTPError("https://example.invalid", 400, "bad", {}, io.BytesIO(b""))

        with self.assertRaises(auth.TokenRefused):
            self._refresh(
                {"access_token": "stale", "refresh_token": "r1", "expiry": 0},
                urlopen=urlopen,
            )

    def test_an_undecodable_body_is_not_treated_as_a_refusal(self):
        """UnicodeDecodeError is a ValueError, which used to read as a refusal."""

        def urlopen(req, timeout=None):
            resp = MagicMock()
            resp.read.return_value = b"\xe9\xff not utf-8"
            cm = MagicMock()
            cm.__enter__.return_value = resp
            cm.__exit__.return_value = False
            return cm

        with self.assertRaises(auth.RefreshNetworkError):
            self._refresh(
                {"access_token": "stale", "refresh_token": "r1", "expiry": 0},
                urlopen=urlopen,
            )

    def test_a_non_http_response_is_not_treated_as_a_refusal(self):
        import http.client

        def urlopen(req, timeout=None):
            raise http.client.BadStatusLine("GARBAGE NOT HTTP")

        with self.assertRaises(auth.RefreshNetworkError):
            self._refresh(
                {"access_token": "stale", "refresh_token": "r1", "expiry": 0},
                urlopen=urlopen,
            )

    def test_a_response_that_is_not_json_is_not_treated_as_a_refusal(self):
        def urlopen(req, timeout=None):
            resp = MagicMock()
            resp.read.return_value = b"<html>captive portal</html>"
            cm = MagicMock()
            cm.__enter__.return_value = resp
            cm.__exit__.return_value = False
            return cm

        with self.assertRaises(auth.RefreshNetworkError):
            self._refresh(
                {"access_token": "stale", "refresh_token": "r1", "expiry": 0},
                urlopen=urlopen,
            )

    def test_a_response_of_the_wrong_shape_is_refused(self):
        def urlopen(req, timeout=None):
            resp = MagicMock()
            resp.read.return_value = b'["not", "an", "object"]'
            cm = MagicMock()
            cm.__enter__.return_value = resp
            cm.__exit__.return_value = False
            return cm

        with self.assertRaises(RuntimeError):
            self._refresh(
                {"access_token": "stale", "refresh_token": "r1", "expiry": 0},
                urlopen=urlopen,
            )

    def test_a_rate_limit_is_not_treated_as_a_refusal(self):
        def urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                "https://example.invalid", 429, "slow", {}, io.BytesIO(b"")
            )

        with self.assertRaises(auth.RefreshNetworkError):
            self._refresh(
                {"access_token": "stale", "refresh_token": "r1", "expiry": 0},
                urlopen=urlopen,
            )

    def test_tokens_and_creds_lazy_loaded_from_disk_when_cache_empty(self):
        loaded = []

        def fake_load(path):
            loaded.append(path)
            if path is auth.MONZO_TOKENS_PATH:
                return {"access_token": "disk_tok", "refresh_token": "r1", "expiry": NOW_TS + 999}
            return dict(CREDS)

        with (
            patch.object(auth, "_cached_tokens", None),
            patch.object(auth, "_cached_creds", None),
            patch.object(auth, "datetime", _FixedDatetime),
            patch.object(auth, "_load_json", fake_load),
        ):
            tok = auth.refresh_token()

        self.assertEqual(tok, "disk_tok")
        # Both credential files were read exactly once to populate the cache.
        self.assertIn(auth.MONZO_TOKENS_PATH, loaded)
        self.assertIn(auth.MONZO_CLIENT_PATH, loaded)


class TestJsonHelpers(unittest.TestCase):
    def test_save_then_load_roundtrips(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Nested path exercises the parent-mkdir in _save_json.
            path = Path(tmp) / "sub" / "tokens.json"
            data = {"access_token": "at", "expiry": 123}
            auth._save_json(path, data)
            self.assertTrue(path.exists())
            self.assertEqual(auth._load_json(path), data)


# --------------------------------------------------------------------------- #
# setup_auth
# --------------------------------------------------------------------------- #
class _FakeConfigDir:
    def mkdir(self, *a, **k):
        pass


def _server_factory(callback_path, recorded, bound=None, sent_state=None):
    """Return an HTTPServer stand-in that drives the callback handler once.

    `bound` collects the address tuple. What the server binds to is the whole
    of what makes the callback reachable from a published container port, and
    nothing the handler does afterwards reveals it.

    `callback_path` may carry a `{state}` placeholder, filled with the state
    the code actually put in the authorisation URL. Hard-coding it instead
    would let the URL and the comparison drift apart: sending one state and
    checking another breaks every real auth while every test still passes,
    because a test that supplies both ends never exercises the tie between
    them.
    """

    class _FakeServer:
        def __init__(self, addr, handler_cls):
            if bound is not None:
                bound.append(addr)
            self._handler_cls = handler_cls

        def handle_request(self):
            h = self._handler_cls.__new__(self._handler_cls)
            # Filled from the same run, which is the whole point: a near-miss
            # built from a previous run's state is just a different random
            # value, and a comparison looking at one character would refuse it
            # for the wrong reason.
            sent = sent_state[0] if sent_state else ""
            path = callback_path
            for token, value in (
                ("{state}", sent),
                ("{state_without_last}", sent[:-1]),
                ("{state_swapped_case}", sent.swapcase()),
            ):
                if token in path:
                    path = path.replace(token, value)
            h.path = path
            h.wfile = io.BytesIO()
            h.send_response = lambda code, *a, **k: recorded.append(code)
            h.send_header = lambda *a, **k: None
            h.end_headers = lambda *a, **k: None
            h.do_GET()

        def server_close(self):
            pass

    return _FakeServer


class _SetupResult:
    def __init__(self, saved, recorded, token_req, stdout, exit_code, bound=None, state=None):
        self.saved = saved  # {path: data}
        self.recorded = recorded  # HTTP response codes the handler emitted
        self.token_req = token_req  # the code-exchange Request, or None
        self.stdout = stdout
        self.exit_code = exit_code  # None on success, else the sys.exit code
        self.bound = bound or []  # (host, port) tuples the server bound to
        self.state = state  # the state the authorisation URL actually carried


def _run_setup(
    *,
    client_exists,
    inputs,
    callback_path,
    token_response=None,
    token_raises=None,
    existing_creds=None,
):
    saved = {}
    recorded = []
    bound = []
    box = {"token_req": None}

    class _Path:
        def __init__(self, exists):
            self._exists = exists

        def exists(self):
            return self._exists

    client_path = _Path(client_exists)
    tokens_path = _Path(False)

    def fake_save(path, data):
        saved[path] = data

    def fake_load(path):
        return dict(existing_creds) if existing_creds else {}

    def fake_urlopen(req, timeout=None):
        box["token_req"] = req
        if token_raises is not None:
            raise token_raises
        return _FakeResp(token_response)

    # Filled from the authorisation URL the code builds, and fed back to the
    # callback. Nothing here stubs the state: a stand-in would let the URL and
    # the comparison disagree without any test noticing.
    sent_state = []

    def fake_browser_open(url):
        sent_state.append(parse_qs(urlparse(url).query).get("state", [""])[0])

    out = StringIO()
    with (
        patch.object(auth, "CONFIG_DIR", _FakeConfigDir()),
        patch.object(auth, "MONZO_CLIENT_PATH", client_path),
        patch.object(auth, "MONZO_TOKENS_PATH", tokens_path),
        patch.object(auth, "datetime", _FixedDatetime),
        patch.object(auth, "_save_json", fake_save),
        patch.object(auth, "_load_json", fake_load),
        patch.object(auth.webbrowser, "open", fake_browser_open),
        # The name setup_auth actually constructs. Patching HTTPServer instead
        # binds a real socket on every one of these tests while still looking
        # like it is driving a stand-in.
        patch.object(
            auth,
            "_CallbackServer",
            _server_factory(callback_path, recorded, bound, sent_state),
        ),
        patch("urllib.request.urlopen", fake_urlopen),
        patch("builtins.input", side_effect=inputs),
        patch.object(sys, "stdout", out),
    ):
        exit_code = None
        try:
            auth.setup_auth()
        except SystemExit as e:
            exit_code = e.code

    result = _SetupResult(
        saved,
        recorded,
        box["token_req"],
        out.getvalue(),
        exit_code,
        bound,
        sent_state[0] if sent_state else None,
    )
    return result, client_path, tokens_path


class TestTheCallbackStateIsChecked(unittest.TestCase):
    """The callback is an unauthenticated local endpoint.

    Anything on the machine can reach `localhost:6600/callback` while `auth` is
    waiting, so a request arriving with someone else's authorisation code would be
    exchanged and written to the token file - and the account the server then
    reads is theirs, not the user's. `state` is what ties the callback to the
    request that started it, so it has to be both unguessable and compared.
    """

    def test_a_callback_with_the_wrong_state_saves_no_token(self):
        result, _client_path, tokens_path = _run_setup(
            client_exists=True,
            inputs=["y"],
            callback_path="/callback?code=attacker_code&state=not_the_one",
            token_response={"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
            existing_creds=CREDS,
        )
        self.assertNotIn(tokens_path, result.saved)
        # And the code was never exchanged: the refusal has to come before the
        # POST, or the attacker's code is spent even though nothing is stored.
        self.assertIsNone(result.token_req)
        # Refused rather than acknowledged. Checking the state after reading the
        # code stores nothing either, so the two assertions above both pass on
        # it - and it answers the caller 200 "Auth complete".
        self.assertEqual(result.recorded, [400])

    def test_a_callback_with_no_state_saves_no_token(self):
        result, _client_path, tokens_path = _run_setup(
            client_exists=True,
            inputs=["y"],
            callback_path="/callback?code=attacker_code",
            token_response={"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
            existing_creds=CREDS,
        )
        self.assertNotIn(tokens_path, result.saved)
        self.assertIsNone(result.token_req)
        self.assertEqual(result.recorded, [400])

    def _completed_run(self):
        return _run_setup(
            client_exists=True,
            inputs=["y"],
            callback_path="/callback?code=AC&state={state}",
            token_response={"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
            existing_creds=CREDS,
        )[0]

    def test_the_state_in_the_url_is_unguessable(self):
        """Asserted on the value the flow actually sent, not on the stdlib.

        Reading `secrets.token_urlsafe` back in a test proves a property of
        Python. What has to hold is that the URL carries something long, not
        derived from the clock, and different every run - the timestamp this
        replaced satisfied none of those and would still have been compared.
        """
        runs = [self._completed_run(), self._completed_run()]
        self.assertNotEqual(runs[0].state, runs[1].state)
        for run in runs:
            self.assertGreaterEqual(len(run.state), 32, run.state)
            self.assertFalse(run.state.isdigit(), run.state)
            # The printed URL is the other way the user receives it, and the
            # only one on a headless or container run where webbrowser opens
            # nothing. Untied, it can carry a state the listener will refuse.
            self.assertIn(run.state, run.stdout)

    def test_a_state_missing_its_last_character_is_refused(self):
        """A near miss from the same run, which is what makes it one.

        Built from a previous run's state it would just be another random
        value, and a comparison reading only the first character would refuse
        it for the wrong reason while still passing.
        """
        result, _client_path, tokens_path = _run_setup(
            client_exists=True,
            inputs=["y"],
            callback_path="/callback?code=AC&state={state_without_last}",
            token_response={"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
            existing_creds=CREDS,
        )
        self.assertNotIn(tokens_path, result.saved)
        self.assertEqual(result.recorded, [400])

    def test_a_state_differing_only_in_case_is_refused(self):
        result, _client_path, tokens_path = _run_setup(
            client_exists=True,
            inputs=["y"],
            callback_path="/callback?code=AC&state={state_swapped_case}",
            token_response={"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
            existing_creds=CREDS,
        )
        self.assertNotIn(tokens_path, result.saved)
        self.assertEqual(result.recorded, [400])

    def test_a_non_ascii_state_is_refused_rather_than_crashing(self):
        """`compare_digest` rejects a non-ASCII `str`, and both routes in reach
        one: `parse_qs` decodes percent-escapes, and `http.server` reads the
        request line as iso-8859-1. Compared as `str` this raises inside the
        handler, so the caller gets a dropped connection and the user a
        traceback."""
        result, _client_path, tokens_path = _run_setup(
            client_exists=True,
            inputs=["y"],
            callback_path="/callback?code=AC&state=%80",
            token_response={"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
            existing_creds=CREDS,
        )
        self.assertNotIn(tokens_path, result.saved)
        self.assertEqual(result.recorded, [400])


class TestSetupAuth(unittest.TestCase):
    def test_full_new_credential_flow(self):
        result, client_path, tokens_path = _run_setup(
            client_exists=False,
            inputs=["new_client", "new_secret"],
            callback_path="/callback?code=auth_code_xyz&state={state}",
            token_response={
                "access_token": "at_1",
                "refresh_token": "rt_1",
                "expires_in": 21600,
            },
        )
        # Entered credentials were saved to the client file.
        self.assertEqual(
            result.saved[client_path],
            {"client_id": "new_client", "client_secret": "new_secret"},
        )
        # The code-exchange POST used the authorization_code grant with the code
        # captured from the callback and the local redirect URI.
        body = result.token_req.data
        self.assertEqual(result.token_req.full_url, auth.MONZO_TOKEN_URL)
        self.assertIn(b"grant_type=authorization_code", body)
        self.assertIn(b"code=auth_code_xyz", body)
        self.assertIn(b"redirect_uri=http", body)
        # The callback handler acknowledged the browser with a 200.
        self.assertEqual(result.recorded, [200])
        # Tokens were stored with a computed expiry (fixed now + expires_in).
        stored = result.saved[tokens_path]
        self.assertEqual(stored["access_token"], "at_1")
        self.assertEqual(stored["refresh_token"], "rt_1")
        self.assertEqual(stored["token_type"], "Bearer")
        self.assertEqual(stored["expiry"], NOW_TS + 21600)
        # SCA window is surfaced to the user.
        self.assertIn("5 minutes", result.stdout)

    def test_reuse_existing_credentials(self):
        result, client_path, tokens_path = _run_setup(
            client_exists=True,
            inputs=[""],  # accept "Re-use existing credentials? [Y/n]" default
            callback_path="/callback?code=code_reuse&state={state}",
            token_response={"access_token": "at_2", "refresh_token": "rt_2", "expires_in": 3600},
            existing_creds=CREDS,
        )
        # Reused creds are NOT re-saved; only the token file is written.
        self.assertNotIn(client_path, result.saved)
        self.assertIn(tokens_path, result.saved)
        # The exchange used the existing client_id.
        self.assertIn(b"client_id=cid_test", result.token_req.data)

    def test_declining_reuse_prompts_for_new_credentials(self):
        # "n" at the reuse prompt discards the existing creds and re-prompts,
        # then saves the freshly entered client credentials.
        result, client_path, _tokens_path = _run_setup(
            client_exists=True,
            inputs=["n", "fresh_client", "fresh_secret"],
            callback_path="/callback?code=code_new&state={state}",
            token_response={"access_token": "at_3", "refresh_token": "rt_3", "expires_in": 3600},
            existing_creds=CREDS,
        )
        self.assertEqual(
            result.saved[client_path],
            {"client_id": "fresh_client", "client_secret": "fresh_secret"},
        )
        self.assertIn(b"client_id=fresh_client", result.token_req.data)

    def test_missing_code_exits_and_handler_emits_400(self):
        # Callback without a code -> handler returns 400, no auth code -> exit 1,
        # and no tokens are written.
        result, _client_path, tokens_path = _run_setup(
            client_exists=True,
            inputs=[""],
            callback_path="/callback?error=access_denied",
            existing_creds=CREDS,
        )
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.recorded, [400])
        self.assertNotIn(tokens_path, result.saved)

    def test_empty_client_id_exits_before_starting_server(self):
        result, _client_path, _tokens_path = _run_setup(
            client_exists=False,
            inputs=["", "some_secret"],  # empty client_id
            callback_path="/callback?code=x",
        )
        self.assertEqual(result.exit_code, 1)
        # Bailed before the OAuth server ran, so no callback was handled.
        self.assertEqual(result.recorded, [])
        self.assertIsNone(result.token_req)

    def test_code_exchange_network_error_exits(self):
        result, _client_path, tokens_path = _run_setup(
            client_exists=True,
            inputs=[""],
            callback_path="/callback?code=good_code",
            token_raises=urllib.error.URLError("boom"),
            existing_creds=CREDS,
        )
        self.assertEqual(result.exit_code, 1)
        self.assertNotIn(tokens_path, result.saved)  # nothing persisted on failure


class TestCredentialFilesAreRefusals(unittest.TestCase):
    """No usable credential file is a refusal, not a transport failure.

    It will not clear on its own, and the user does have to re-authorise - the
    one case where that advice is right.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._dir.name)
        auth._cached_tokens = None
        auth._cached_creds = None

    def tearDown(self):
        self._dir.cleanup()
        auth._cached_tokens = None
        auth._cached_creds = None

    def _refresh_with_files(self, tokens=None, creds='{"client_id": "i", "client_secret": "s"}'):
        tokens_path = self.dir / "monzo_tokens.json"
        creds_path = self.dir / "monzo_client.json"
        if tokens is not None:
            tokens_path.write_text(tokens)
        creds_path.write_text(creds)
        # Deliberate, not left over now that conftest isolates these paths as
        # well: the files written above are the ones under test, and without
        # the patches `auth` would read conftest's empty directory instead -
        # so every case here would exercise the missing-file branch and still
        # pass, `_load_json` reporting missing and unparseable alike.
        with (
            patch.object(auth, "MONZO_TOKENS_PATH", tokens_path),
            patch.object(auth, "MONZO_CLIENT_PATH", creds_path),
        ):
            return auth.refresh_token()

    def test_a_missing_token_file_is_a_refusal(self):
        with self.assertRaises(auth.TokenRefused):
            self._refresh_with_files(tokens=None)

    def test_an_unparseable_token_file_is_a_refusal(self):
        with self.assertRaises(auth.TokenRefused):
            self._refresh_with_files(tokens="{not json")

    def test_a_token_file_of_the_wrong_shape_is_a_refusal(self):
        with self.assertRaises(auth.TokenRefused):
            self._refresh_with_files(tokens='["not", "an", "object"]')


@unittest.skipIf(sys.platform == "win32", "POSIX mode bits; Windows uses ACLs")
class TestCredentialFilesAreOwnerOnly(unittest.TestCase):
    """A token file must never be readable by other local users.

    This one had no chmod at all, so the file kept whatever the umask gave it -
    0644 on a default umask, permanently, for a file holding a refresh token.

    POSIX only: on Windows the mode passed to os.open is ignored and access is
    governed by inherited ACLs, so the assertion below would fail there for a
    reason that says nothing about this code.
    """

    def test_a_new_token_file_is_created_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "monzo_tokens.json"
            auth._save_json(target, {"refresh_token": "fictional"})
            self.assertEqual(oct(target.stat().st_mode & 0o777), "0o600")

    def test_rewriting_does_not_loosen_an_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "monzo_tokens.json"
            auth._save_json(target, {"refresh_token": "first"})
            auth._save_json(target, {"refresh_token": "second"})
            self.assertEqual(oct(target.stat().st_mode & 0o777), "0o600")
            self.assertEqual(json.loads(target.read_text())["refresh_token"], "second")


@unittest.skipIf(sys.platform == "win32", "POSIX mode bits; Windows uses ACLs")
class TestExistingFilesAreTightened(unittest.TestCase):
    def test_an_existing_loose_token_file_is_tightened(self):
        """O_CREAT's mode applies only at creation, so upgrades kept 0644."""
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "monzo_tokens.json"
            path.write_text("{}")
            os.chmod(path, 0o644)
            auth._save_json(path, {"refresh_token": "fictional"})
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")

    def test_a_failure_to_tighten_does_not_take_the_token_with_it(self):
        """The tightening is best-effort, and it has to be.

        It needs ownership the writer does not always have - a file arriving
        under another uid, or a mount whose filesystem refuses fchmod. By the
        time it runs, O_TRUNC has emptied the file, so aborting the write
        would trade a readable token for no token at all. On a refresh the
        server has already rotated the old one, which makes that an outage
        rather than a permissions problem.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "monzo_tokens.json"
            path.write_text('{"refresh_token": "old"}')

            def refuse(fd, mode):
                raise PermissionError(1, "Operation not permitted")

            with patch.object(auth.os, "fchmod", refuse):
                auth._save_json(path, {"refresh_token": "rotated"})

            self.assertEqual(json.loads(path.read_text())["refresh_token"], "rotated")

    def test_the_mode_is_set_when_the_file_is_opened(self):
        """Pins the docstring's reason, not just its outcome.

        A chmod after the write produces the same final mode while leaving the
        token briefly readable, so asserting the result alone cannot tell the
        two apart.
        """
        seen = {}
        real_open = os.open

        def spy(path, flags, mode=0o777, **kwargs):
            seen["mode"] = mode
            return real_open(path, flags, mode, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "monzo_tokens.json"
            with patch.object(auth.os, "open", spy):
                auth._save_json(path, {"refresh_token": "fictional"})

        self.assertEqual(oct(seen["mode"]), "0o600")

    def test_a_shorter_rewrite_leaves_no_tail(self):
        """Equal-length payloads cannot catch a missing O_TRUNC."""
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "monzo_tokens.json"
            auth._save_json(path, {"padding": "x" * 500, "v": 1})
            auth._save_json(path, {"v": 2})
            self.assertEqual(json.loads(path.read_text()), {"v": 2})


class TestWhereTheCallbackServerBinds(unittest.TestCase):
    """The bind address decides whether a container's callback can arrive.

    Bound to loopback, a published container port is refused and `setup_auth`
    waits for a callback that cannot be delivered - with no timeout, so it
    hangs rather than failing. None of the other setup_auth tests observe the
    address, so without these a one-line revert restores that silently.

    The first two tests look redundant and are not: the first catches a bind
    hardcoded to `0.0.0.0`, which the patched test cannot see, and the second
    catches one hardcoded to `localhost`, which the first cannot. Deleting
    either leaves a hardcoded bind alive in that direction.
    """

    def test_it_binds_the_configured_interface(self):
        result, _, _ = _run_setup(
            client_exists=False,
            inputs=["cid", "csec"],
            callback_path="/callback?code=AC&state={state}",
            token_response={"access_token": "at", "refresh_token": "rt", "expires_in": 21600},
        )
        self.assertEqual(result.bound, [(auth.MONZO_CALLBACK_HOST, auth.MONZO_CALLBACK_PORT)])

    def test_a_widened_interface_reaches_the_bind(self):
        # The constant is only worth having if setup_auth reads it rather than
        # a literal, so drive the whole flow with it patched.
        with patch.object(auth, "MONZO_CALLBACK_HOST", "0.0.0.0"):
            result, _, _ = _run_setup(
                client_exists=False,
                inputs=["cid", "csec"],
                callback_path="/callback?code=AC&state={state}",
                token_response={"access_token": "at", "refresh_token": "rt", "expires_in": 21600},
            )
        self.assertEqual(result.bound, [("0.0.0.0", auth.MONZO_CALLBACK_PORT)])

    def test_the_redirect_uri_stays_loopback_whatever_the_bind(self):
        # The redirect URI is registered with Monzo and resolved by the browser
        # on the host. Making it "consistent" with the bind sends
        # redirect_uri=http://0.0.0.0:6600/callback, which Monzo rejects.
        with patch.object(auth, "MONZO_CALLBACK_HOST", "0.0.0.0"):
            result, _, _ = _run_setup(
                client_exists=False,
                inputs=["cid", "csec"],
                callback_path="/callback?code=AC&state={state}",
                token_response={"access_token": "at", "refresh_token": "rt", "expires_in": 21600},
            )
        expected = f"http://localhost:{auth.MONZO_CALLBACK_PORT}/callback"
        self.assertIn(f"redirect_uri={quote(expected, safe='')}", result.token_req.data.decode())
        self.assertIn(expected, result.stdout)
        self.assertNotIn("0.0.0.0", result.stdout)


if __name__ == "__main__":
    unittest.main()


# SO_EXCLUSIVEADDRUSE does not exist off Windows, so the branch is driven
# against this stand-in value.
_EXCLUSIVE = 0xFFFFFFFF


class _RecordingSocket:
    """Stands in for the real socket so server_bind's Windows branch can run."""

    def __init__(self):
        self.calls = []

    def setsockopt(self, level, option, value):
        self.calls.append(("setsockopt", level, option, value))

    def bind(self, address):
        self.calls.append(("bind", address))

    def getsockname(self):
        return ("127.0.0.1", auth.MONZO_CALLBACK_PORT)


class TestTheCallbackPortIsNotShared(unittest.TestCase):
    """The listener carries the authorisation code, so it must not be displaceable.

    `server_bind` chooses on `sys.platform` at call time, so the Windows branch
    is reachable from a POSIX runner against a recording socket. That is worth
    more than reading the source for it: a source assertion lets the option be
    set after the bind, at the wrong level, on the wrong socket, or with a
    value of 0, all of which pass a check that only looks for the name.
    """

    def _bind_as(self, platform):
        with (
            patch.object(auth.sys, "platform", platform),
            patch.object(auth.socket, "SO_EXCLUSIVEADDRUSE", _EXCLUSIVE, create=True),
            patch.object(auth.socket, "getfqdn", lambda host: host),
        ):
            server = auth._CallbackServer.__new__(auth._CallbackServer)
            server.socket = _RecordingSocket()
            server.server_address = (auth.MONZO_CALLBACK_HOST, auth.MONZO_CALLBACK_PORT)
            # Fixed at import against the real platform, so it is supplied here
            # rather than read; the class attribute is pinned separately.
            server.allow_reuse_address = platform != "win32"
            server.allow_reuse_port = False
            auth._CallbackServer.server_bind(server)
            return server.socket.calls

    def test_windows_asks_for_exclusive_use_before_binding(self):
        # _EXCLUSIVE is the sentinel _bind_as patches in, because the real
        # constant does not exist off Windows and the patch is gone by now.
        calls = self._bind_as("win32")
        self.assertEqual(
            calls[0],
            ("setsockopt", socket.SOL_SOCKET, _EXCLUSIVE, 1),
            calls,
        )
        self.assertTrue(any(call[0] == "bind" for call in calls), calls)
        # Never both: asking to share after asking not to is the configuration
        # Microsoft documents as insecure.
        self.assertFalse(
            any(
                call[:3] == ("setsockopt", socket.SOL_SOCKET, socket.SO_REUSEADDR) for call in calls
            ),
            calls,
        )

    def test_posix_asks_for_nothing_exclusive(self):
        calls = self._bind_as("linux")
        self.assertFalse(
            any(call[0] == "setsockopt" and call[2] == _EXCLUSIVE for call in calls),
            calls,
        )

    def test_reuse_is_allowed_off_windows_and_refused_on_it(self):
        """Not asking to share closes the specific-address case; the exclusive
        option is what still holds when the bind is a wildcard, which this
        server's is whenever MONZO_MCP_CALLBACK_HOST is widened."""
        self.assertEqual(auth._CallbackServer.allow_reuse_address, sys.platform != "win32")
        tree = ast.parse(inspect.getsource(auth._CallbackServer))
        assigned = [
            ast.unparse(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(getattr(t, "id", None) == "allow_reuse_address" for t in node.targets)
        ]
        self.assertTrue(assigned, "allow_reuse_address is no longer set here")
        self.assertTrue(all("platform" in value for value in assigned), assigned)

    def test_only_win32_is_named(self):
        """`sys.platform` is `win32` on 64-bit Windows too, so a `win64` test
        is a branch that never runs."""
        tree = ast.parse(inspect.getsource(auth._CallbackServer))
        compared = {
            const.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Compare) and ast.unparse(node.left) == "sys.platform"
            for const in node.comparators
            if isinstance(const, ast.Constant)
        }
        self.assertEqual(compared, {"win32"}, compared)

    @unittest.skipIf(sys.platform == "win32", "binds a real POSIX socket")
    def test_it_still_binds(self):
        """server_bind is overridden, so a mistake there breaks `auth` outright."""
        server = auth._CallbackServer(("localhost", 0), auth.BaseHTTPRequestHandler)
        try:
            self.assertEqual(server.server_address[0], "127.0.0.1")
            self.assertNotEqual(server.server_address[1], 0)
        finally:
            server.server_close()

    def test_no_bare_httpserver_is_constructed_anywhere(self):
        """Scoped to the module, not to setup_auth: a second listener added
        elsewhere would carry the default this class exists to refuse."""
        tree = ast.parse(inspect.getsource(auth))
        bare = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "HTTPServer"
        ]
        self.assertFalse(bare, bare)

"""Tests for OAuth token management and interactive setup.

No network and no real OAuth: the HTTP layer (`urllib.request.urlopen`), the
local callback HTTPServer, `input`, `webbrowser`, and the JSON file I/O are all
mocked. Time is pinned via a datetime subclass so the 5-minute expiry buffer and
the stored expiry timestamps are exact, not approximate.
"""

import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def _server_factory(callback_path, recorded):
    """Return an HTTPServer stand-in that drives the callback handler once."""

    class _FakeServer:
        def __init__(self, addr, handler_cls):
            self._handler_cls = handler_cls

        def handle_request(self):
            h = self._handler_cls.__new__(self._handler_cls)
            h.path = callback_path
            h.wfile = io.BytesIO()
            h.send_response = lambda code, *a, **k: recorded.append(code)
            h.send_header = lambda *a, **k: None
            h.end_headers = lambda *a, **k: None
            h.do_GET()

        def server_close(self):
            pass

    return _FakeServer


class _SetupResult:
    def __init__(self, saved, recorded, token_req, stdout, exit_code):
        self.saved = saved  # {path: data}
        self.recorded = recorded  # HTTP response codes the handler emitted
        self.token_req = token_req  # the code-exchange Request, or None
        self.stdout = stdout
        self.exit_code = exit_code  # None on success, else the sys.exit code


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

    out = StringIO()
    with (
        patch.object(auth, "CONFIG_DIR", _FakeConfigDir()),
        patch.object(auth, "MONZO_CLIENT_PATH", client_path),
        patch.object(auth, "MONZO_TOKENS_PATH", tokens_path),
        patch.object(auth, "datetime", _FixedDatetime),
        patch.object(auth, "_save_json", fake_save),
        patch.object(auth, "_load_json", fake_load),
        patch.object(auth, "webbrowser"),
        patch.object(auth, "HTTPServer", _server_factory(callback_path, recorded)),
        patch("urllib.request.urlopen", fake_urlopen),
        patch("builtins.input", side_effect=inputs),
        patch.object(sys, "stdout", out),
    ):
        exit_code = None
        try:
            auth.setup_auth()
        except SystemExit as e:
            exit_code = e.code

    result = _SetupResult(saved, recorded, box["token_req"], out.getvalue(), exit_code)
    return result, client_path, tokens_path


class TestSetupAuth(unittest.TestCase):
    def test_full_new_credential_flow(self):
        result, client_path, tokens_path = _run_setup(
            client_exists=False,
            inputs=["new_client", "new_secret"],
            callback_path="/callback?code=auth_code_xyz&state=s",
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
            callback_path="/callback?code=code_reuse&state=s",
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
            callback_path="/callback?code=code_new&state=s",
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


if __name__ == "__main__":
    unittest.main()

"""Tests for the read-only Monzo API GET wrapper.

The HTTP layer (`urllib.request.urlopen`) is mocked throughout - no network,
no credentials. Tests cover the happy path, the auto-refresh-on-expiry flow
driven through the *real* refresh_token, and the mapping from HTTP status codes
to typed exceptions. The wrapper is read-only by contract: assertions pin that
every request it issues is a GET with no body.
"""

import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from monzo_mcp import api, auth
from monzo_mcp.api import MonzoAPIError, MonzoAuthError, MonzoSCAError
from monzo_mcp.config import MONZO_API_BASE, MONZO_TOKEN_URL


class _FakeResp:
    """Minimal context-manager stand-in for urlopen's return value."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _http_error(code):
    return urllib.error.HTTPError(
        url="https://api.monzo.com/x", code=code, msg="err", hdrs=None, fp=io.BytesIO(b"")
    )


class TestGetHappyPath(unittest.TestCase):
    def test_returns_parsed_json_and_sends_authorized_get(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["req"] = req
            return _FakeResp({"accounts": [{"id": "acc_1"}]})

        with (
            patch.object(api, "refresh_token", lambda: "tok_abc"),
            patch("urllib.request.urlopen", fake_urlopen),
        ):
            result = api.get("/accounts")

        self.assertEqual(result, {"accounts": [{"id": "acc_1"}]})
        req = captured["req"]
        # URL is base + path; auth header carries the refreshed token.
        self.assertEqual(req.full_url, f"{MONZO_API_BASE}/accounts")
        self.assertEqual(req.get_header("Authorization"), "Bearer tok_abc")
        # Read-only contract: GET verb, no request body.
        self.assertEqual(req.get_method(), "GET")
        self.assertIsNone(req.data)


class TestAutoRefreshOnExpiry(unittest.TestCase):
    """api.get -> refresh_token: an expired token is refreshed transparently."""

    def test_expired_token_is_refreshed_then_request_uses_new_token(self):
        # Cached token already expired (expiry in the past). refresh_token should
        # POST to the token endpoint, then api.get should send the *new* token.
        expired = {
            "access_token": "old_tok",
            "refresh_token": "refresh_1",
            "token_type": "Bearer",
            "expiry": 0,
        }
        creds = {"client_id": "cid", "client_secret": "csecret"}
        seen = {"token_posts": 0, "auth_headers": []}

        def fake_urlopen(req, timeout=None):
            if req.full_url == MONZO_TOKEN_URL:
                seen["token_posts"] += 1
                # The refresh POST must carry a body (grant_type=refresh_token).
                self.assertIsNotNone(req.data)
                self.assertIn(b"grant_type=refresh_token", req.data)
                return _FakeResp(
                    {"access_token": "new_tok", "refresh_token": "refresh_2", "expires_in": 3600}
                )
            seen["auth_headers"].append(req.get_header("Authorization"))
            return _FakeResp({"balance": 500})

        with (
            patch.object(auth, "_cached_tokens", dict(expired)),
            patch.object(auth, "_cached_creds", dict(creds)),
            patch.object(auth, "_save_json", lambda path, data: None),
            patch("urllib.request.urlopen", fake_urlopen),
        ):
            result = api.get("/balance?account_id=acc_1")

        self.assertEqual(result, {"balance": 500})
        self.assertEqual(seen["token_posts"], 1)  # exactly one refresh
        self.assertEqual(seen["auth_headers"], ["Bearer new_tok"])

    def test_valid_token_is_not_refreshed(self):
        # Expiry far in the future (well beyond the 5-min buffer) -> no token POST.
        valid = {
            "access_token": "live_tok",
            "refresh_token": "refresh_1",
            "token_type": "Bearer",
            "expiry": 9_999_999_999,
        }
        seen = {"token_posts": 0}

        def fake_urlopen(req, timeout=None):
            if req.full_url == MONZO_TOKEN_URL:
                seen["token_posts"] += 1
                return _FakeResp({})
            return _FakeResp({"ok": True})

        with (
            patch.object(auth, "_cached_tokens", dict(valid)),
            patch.object(auth, "_cached_creds", {"client_id": "c", "client_secret": "s"}),
            patch("urllib.request.urlopen", fake_urlopen),
        ):
            result = api.get("/ping")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(seen["token_posts"], 0)


class TestGetErrorMapping(unittest.TestCase):
    def _get_raising(self, exc):
        def fake_urlopen(req, timeout=None):
            raise exc

        with (
            patch.object(api, "refresh_token", lambda: "tok"),
            patch("urllib.request.urlopen", fake_urlopen),
        ):
            return api.get("/accounts")

    def test_401_maps_to_auth_error(self):
        with self.assertRaises(MonzoAuthError):
            self._get_raising(_http_error(401))

    def test_403_maps_to_sca_error(self):
        with self.assertRaises(MonzoSCAError):
            self._get_raising(_http_error(403))

    def test_other_http_error_maps_to_api_error_with_code(self):
        with self.assertRaises(MonzoAPIError) as ctx:
            self._get_raising(_http_error(500))
        self.assertIn("500", str(ctx.exception))

    def test_network_error_maps_to_api_error(self):
        with self.assertRaises(MonzoAPIError) as ctx:
            self._get_raising(urllib.error.URLError("connection refused"))
        self.assertIn("Network error", str(ctx.exception))

    def test_error_message_does_not_leak_response_body(self):
        # The 500 HTTPError carries a body; the raised message must expose only
        # the status code, never the raw response payload.
        err = urllib.error.HTTPError(
            url="https://api.monzo.com/x",
            code=500,
            msg="err",
            hdrs=None,
            fp=io.BytesIO(b"secret-account-detail"),
        )
        with self.assertRaises(MonzoAPIError) as ctx:
            self._get_raising(err)
        self.assertNotIn("secret-account-detail", str(ctx.exception))


class TestReadOnlyContract(unittest.TestCase):
    def test_module_exposes_no_write_verbs(self):
        # The API surface is read-only: `get` is the only request function; there
        # is deliberately no post/put/patch/delete path.
        for verb in ("post", "put", "patch", "delete"):
            self.assertFalse(hasattr(api, verb), f"unexpected write verb: api.{verb}")


class TestRefreshFailuresAreClassified(unittest.TestCase):
    """api.get maps the two types refresh_token guarantees, and nothing else.

    It used to catch a tuple of builtins, so the classification depended on
    remembering every type auth could raise - which is how a bare OSError, an
    http.client exception and a decode failure were each graded a dead
    credential in turn. Monzo rotates the refresh token on use and the token
    file is shared, so that answer is the destructive one.
    """

    def _get_with_refresh_raising(self, exc):
        with patch.object(api, "refresh_token", side_effect=exc):
            return api.get("/accounts")

    def test_a_refusal_becomes_an_auth_error(self):
        with self.assertRaises(MonzoAuthError):
            self._get_with_refresh_raising(auth.TokenRefused("revoked"))

    def test_a_network_failure_is_not_an_auth_failure(self):
        with self.assertRaises(MonzoAPIError) as caught:
            self._get_with_refresh_raising(auth.RefreshNetworkError("no route"))
        self.assertNotIsInstance(caught.exception, MonzoAuthError)

    def test_the_auth_message_is_fixed_text(self):
        """This string reaches the MCP client, so nothing may be interpolated."""
        with self.assertRaises(MonzoAuthError) as caught:
            self._get_with_refresh_raising(auth.TokenRefused("/etc/secret/path is missing"))
        self.assertEqual(
            str(caught.exception), "Could not obtain an access token. Run: monzo-mcp auth"
        )

    def test_the_network_message_is_fixed_text(self):
        with self.assertRaises(MonzoAPIError) as caught:
            self._get_with_refresh_raising(auth.RefreshNetworkError("/etc/secret/path timed out"))
        self.assertEqual(str(caught.exception), "Network error. Check your connection.")


class TestTheRefreshBoundary(unittest.TestCase):
    """Every exit from refresh_token is one of two types, by construction."""

    def _refresh_with_worker_raising(self, exc):
        with patch.object(auth, "_refresh_token", side_effect=exc):
            return auth.refresh_token()

    def test_an_unclassified_failure_becomes_a_network_error(self):
        import http.client

        for exc in (
            TimeoutError("bare timeout"),
            ConnectionResetError("reset"),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"),
            KeyError("access_token"),
            ValueError("unparseable"),
            RuntimeError("something nobody classified"),
            http.client.BadStatusLine("garbage"),
        ):
            with self.subTest(exc=type(exc).__name__):
                with self.assertRaises(auth.RefreshNetworkError):
                    self._refresh_with_worker_raising(exc)

    def test_a_refusal_is_passed_through_unchanged(self):
        with self.assertRaises(auth.TokenRefused):
            self._refresh_with_worker_raising(auth.TokenRefused("revoked"))

    def test_the_boundary_does_not_swallow_a_successful_refresh(self):
        with patch.object(auth, "_refresh_token", return_value="a-token"):
            self.assertEqual(auth.refresh_token(), "a-token")


if __name__ == "__main__":
    unittest.main()

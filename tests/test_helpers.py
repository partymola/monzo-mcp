"""Tests for shared helpers."""

import asyncio
import json
import sqlite3
import unittest
from unittest.mock import patch

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from monzo_mcp.api import MonzoAPIError
from monzo_mcp.helpers import format_response, pence_to_pounds, require_auth


class TestFormatResponse(unittest.TestCase):
    def test_dict(self):
        result = format_response({"key": "value"})
        self.assertEqual(json.loads(result), {"key": "value"})

    def test_list(self):
        result = format_response([1, 2, 3])
        self.assertEqual(json.loads(result), [1, 2, 3])

    def test_none(self):
        result = format_response(None)
        self.assertIsNone(json.loads(result))

    def test_string(self):
        result = format_response("hello")
        self.assertEqual(json.loads(result), {"result": "hello"})


class TestPenceToPounds(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(pence_to_pounds(12345), 123.45)

    def test_negative(self):
        self.assertEqual(pence_to_pounds(-5000), -50.0)

    def test_zero(self):
        self.assertEqual(pence_to_pounds(0), 0.0)


class TestWhichErrorsAreExplainedToTheModel:
    """`mcp` 2.1 keeps a `ToolError`'s text and replaces every other
    exception's with "Error executing tool <name>".

    So an error a caller could act on has to be converted, and an unplanned one
    has to be left alone.
    """

    @patch("monzo_mcp.helpers.MONZO_CLIENT_PATH")
    @patch("monzo_mcp.helpers.MONZO_TOKENS_PATH")
    def test_a_deliberate_error_keeps_its_message(self, tokens_path, client_path):
        client_path.exists.return_value = True
        tokens_path.exists.return_value = True

        @require_auth
        async def tool_fn():
            raise MonzoAPIError("Monzo API error 503")

        with pytest.raises(ToolError) as excinfo:
            asyncio.run(tool_fn())
        assert "Monzo API error 503" in str(excinfo.value)

    # More than one type, because a partial widening is the realistic mistake
    # rather than `except Exception`. The two that look like tidying rather
    # than widening are the point: TokenRefused and RefreshNetworkError already
    # carry RuntimeError, and AccountNotFound already carries ValueError, so
    # adding either to the clause reads as making it agree with itself.
    @pytest.mark.parametrize(
        "unplanned",
        [
            OSError("/home/someone/.config/monzo-mcp/monzo_tokens.json"),
            RuntimeError("an unanticipated failure"),
            ValueError("an unanticipated bad value"),
            KeyError("access_token"),
            sqlite3.OperationalError("no such table: monzo_transactions"),
        ],
        ids=["oserror", "runtimeerror", "valueerror", "keyerror", "sqlite"],
    )
    @patch("monzo_mcp.helpers.MONZO_CLIENT_PATH")
    @patch("monzo_mcp.helpers.MONZO_TOKENS_PATH")
    def test_an_unplanned_error_is_left_to_be_masked(self, tokens_path, client_path, unplanned):
        # The OSError is the measured case: a failure to read the token file or
        # the cache names an absolute path. Converting everything puts it on
        # the wire.
        client_path.exists.return_value = True
        tokens_path.exists.return_value = True

        @require_auth
        async def tool_fn():
            raise unplanned

        with pytest.raises(type(unplanned)):
            asyncio.run(tool_fn())

    @patch("monzo_mcp.helpers.MONZO_CLIENT_PATH")
    @patch("monzo_mcp.helpers.MONZO_TOKENS_PATH")
    def test_a_missing_account_still_says_which_one(self, tokens_path, client_path):
        # Through the real resolver: it raises inside the tool body, so nothing
        # converts it unless the decorator does.
        client_path.exists.return_value = True
        tokens_path.exists.return_value = True

        from monzo_mcp.tools import account_tools

        with patch.object(account_tools, "_get_accounts", return_value=[]):
            with pytest.raises(ToolError) as excinfo:
                asyncio.run(account_tools.monzo_get_balance(account_type="joint"))

        assert "No open joint account found" in str(excinfo.value)

    @patch("monzo_mcp.helpers.MONZO_CLIENT_PATH")
    @patch("monzo_mcp.helpers.MONZO_TOKENS_PATH")
    def test_a_missing_credential_still_answers_rather_than_raising(self, tokens_path, client_path):
        # The gate returns JSON and must not become a ToolError with the rest:
        # a caller with no credentials gets an answer, not a failed call.
        client_path.exists.return_value = False
        tokens_path.exists.return_value = True

        @require_auth
        async def tool_fn():
            raise AssertionError("must not be reached")

        parsed = json.loads(asyncio.run(tool_fn()))
        assert "Run: monzo-mcp auth" in parsed["error"]


if __name__ == "__main__":
    unittest.main()

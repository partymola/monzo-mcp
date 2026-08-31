"""Tests for the pot listing tool.

The Monzo API is stubbed, auth checks are satisfied with temp files, and
balance snapshots are captured instead of written.
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from monzo_mcp import helpers
from monzo_mcp.mcp_instance import mcp
from monzo_mcp.tools import account_tools

ACCOUNTS = [
    {"id": "acc_personal", "type": "uk_retail", "closed": False},
    {"id": "acc_joint", "type": "uk_retail_joint", "closed": False},
]

POTS = {
    "acc_personal": [],
    "acc_joint": [
        {"id": "pot_bills", "name": "Bills", "balance": 694224, "currency": "GBP"},
        {"id": "pot_old", "name": "Retired", "balance": 0, "deleted": True},
        {"id": "pot_goal", "name": "Holiday", "balance": 5000, "goal_amount": 100000},
    ],
}


def _fake_get(path):
    if path == "/accounts":
        return {"accounts": ACCOUNTS}
    account_id = path.split("current_account_id=")[1]
    return {"pots": POTS[account_id]}


def _call(tool, saved=None, **kwargs):
    """Invoke a pot tool against the stubbed API and return parsed JSON."""
    # The tool caches /accounts across calls; each test starts from cold
    account_tools._accounts_cache = None
    with tempfile.NamedTemporaryFile() as f:
        fake_path = Path(f.name)
        with (
            patch.object(helpers, "MONZO_CLIENT_PATH", fake_path),
            patch.object(helpers, "MONZO_TOKENS_PATH", fake_path),
            patch.object(account_tools.api, "get", _fake_get),
            patch.object(account_tools, "get_db", lambda: _NullDB()),
            patch.object(
                account_tools,
                "save_balance",
                lambda db, atype, name, bal, cur: (saved if saved is not None else []).append(
                    (atype, name, bal)
                ),
            ),
        ):
            return json.loads(asyncio.run(tool(**kwargs)))


class _NullDB:
    def close(self):
        pass


class TestListPots(unittest.TestCase):
    def test_response_names_the_account_it_resolved(self):
        result = _call(account_tools.monzo_list_pots, account_type="joint")
        self.assertEqual(result["account_type"], "joint")
        self.assertEqual([p["name"] for p in result["pots"]], ["Bills", "Holiday"])

    def test_empty_result_still_names_the_account(self):
        # The failure this guards: a bare [] reads as "no pots anywhere" rather
        # than "no pots on the account that was actually resolved".
        result = _call(account_tools.monzo_list_pots, account_type="personal")
        self.assertEqual(result, {"account_type": "personal", "pots": []})

    def test_deleted_pots_are_excluded(self):
        result = _call(account_tools.monzo_list_pots, account_type="joint")
        self.assertNotIn("Retired", [p["name"] for p in result["pots"]])

    def test_balances_are_pounds_and_goal_only_when_set(self):
        result = _call(account_tools.monzo_list_pots, account_type="joint")
        by_name = {p["name"]: p for p in result["pots"]}
        self.assertEqual(by_name["Bills"]["balance"], 6942.24)
        self.assertNotIn("goal", by_name["Bills"])
        self.assertEqual(by_name["Holiday"]["goal"], 1000.0)

    def test_snapshot_saved_for_each_listed_pot(self):
        saved = []
        _call(account_tools.monzo_list_pots, saved=saved, account_type="joint")
        self.assertEqual(saved, [("joint", "Bills", 694224), ("joint", "Holiday", 5000)])

    def test_invalid_account_type_is_rejected(self):
        """Through the server, because the constraint is in the schema.

        Called directly the tool reaches `_resolve_account_id` and fails for a
        different reason, which would pass while proving nothing.
        """
        schema = next(t for t in asyncio.run(mcp.list_tools()) if t.name == "monzo_list_pots")
        self.assertEqual(
            schema.input_schema["properties"]["account_type"]["enum"],
            ["personal", "joint"],
        )
        with self.assertRaises(Exception) as caught:
            asyncio.run(mcp.call_tool("monzo_list_pots", {"account_type": "savings"}))
        self.assertIn("savings", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

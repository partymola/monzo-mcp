"""Tests for the list/search read tools' counterparty exposure.

The cache is in-memory SQLite seeded directly (no API), auth checks are
satisfied with temp files, and auto-sync is stubbed out.
"""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from monzo_mcp import helpers
from monzo_mcp.db import SCHEMA
from monzo_mcp.tools import transaction_tools


def _make_db():
    # check_same_thread=False: the tools run their queries via
    # anyio.to_thread.run_sync, i.e. on a different thread than the seeding
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def _insert(
    db,
    txn_id,
    *,
    created,
    amount=-1000,
    description="",
    merchant=None,
    notes="",
    cp_name=None,
    cp_sort=None,
    cp_account=None,
    cp_user=None,
):
    db.execute(
        """INSERT INTO monzo_transactions
           (id, account_id, account_type, created, amount, currency, description,
            merchant_name, category, notes, settled, counterparty_name,
            counterparty_sort_code, counterparty_account_number, counterparty_user_id)
           VALUES (?, 'acc_1', 'personal', ?, ?, 'GBP', ?, ?, 'general', ?,
                   '2026-02-01', ?, ?, ?, ?)""",
        (
            txn_id,
            created,
            amount,
            description,
            merchant,
            notes,
            cp_name,
            cp_sort,
            cp_account,
            cp_user,
        ),
    )
    db.commit()


def _call(tool, db, **kwargs):
    """Invoke an MCP read tool against a seeded DB and return parsed JSON."""
    with tempfile.NamedTemporaryFile() as f:
        fake_path = Path(f.name)
        with (
            patch.object(helpers, "MONZO_CLIENT_PATH", fake_path),
            patch.object(helpers, "MONZO_TOKENS_PATH", fake_path),
            patch.object(transaction_tools, "auto_sync_if_stale", lambda: None),
            patch.object(transaction_tools, "get_db", lambda: db),
        ):
            return json.loads(asyncio.run(tool(**kwargs)))


def _seed_mixed(db):
    _insert(
        db,
        "tx_transfer",
        created="2026-02-01T10:00:00Z",
        amount=-39900,
        description="Acme Solar LLP",
        cp_name="Acme Solar LLP",
        cp_sort="123456",
        cp_account="12345678",
        cp_user="anonuser_ext1",
    )
    _insert(
        db,
        "tx_p2p",
        created="2026-02-02T10:00:00Z",
        amount=-2500,
        cp_name="Jane Doe",
        cp_user="user_friend1",
    )
    _insert(
        db,
        "tx_card",
        created="2026-02-03T10:00:00Z",
        amount=-500,
        description="COFFEE SHOP",
        merchant="Coffee Shop",
    )


class TestAccountEcho(unittest.TestCase):
    """An empty result must say which account it looked in."""

    def test_list_names_the_account_filter(self):
        db = _make_db()
        _seed_mixed(db)
        result = _call(transaction_tools.monzo_list_transactions, db, account_type="personal")
        self.assertEqual(result["account_type"], "personal")

    def test_search_names_the_account_filter(self):
        db = _make_db()
        _seed_mixed(db)
        result = _call(
            transaction_tools.monzo_search_transactions, db, query="coffee", account_type="personal"
        )
        self.assertEqual(result["account_type"], "personal")

    def test_unfiltered_echo_is_explicit_null_not_absent(self):
        # null distinguishes "searched every account" from "the key is missing"
        db = _make_db()
        _seed_mixed(db)
        result = _call(transaction_tools.monzo_list_transactions, db)
        self.assertIn("account_type", result)
        self.assertIsNone(result["account_type"])

    def test_empty_search_still_names_the_account(self):
        db = _make_db()
        _seed_mixed(db)
        result = _call(
            transaction_tools.monzo_search_transactions, db, query="zzz", account_type="joint"
        )
        self.assertEqual(result, {"account_type": "joint", "transactions": []})


class TestListCounterparty(unittest.TestCase):
    def test_transfer_includes_counterparty_and_card_does_not(self):
        db = _make_db()
        _seed_mixed(db)
        result = _call(transaction_tools.monzo_list_transactions, db)
        by_id = {t["id"]: t for t in result["transactions"]}

        self.assertEqual(
            by_id["tx_transfer"]["counterparty"],
            {"name": "Acme Solar LLP", "sort_code": "123456", "account_number": "12345678"},
        )
        # p2p has no sort code / account number - only the name is present
        self.assertEqual(by_id["tx_p2p"]["counterparty"], {"name": "Jane Doe"})
        # Card transactions are unaffected: no counterparty key at all
        self.assertNotIn("counterparty", by_id["tx_card"])
        self.assertEqual(by_id["tx_card"]["merchant"], "Coffee Shop")


class TestSearchCounterparty(unittest.TestCase):
    def test_search_matches_counterparty_name(self):
        db = _make_db()
        _seed_mixed(db)
        result = _call(transaction_tools.monzo_search_transactions, db, query="acme solar")
        txns = result["transactions"]
        self.assertEqual([t["id"] for t in txns], ["tx_transfer"])
        self.assertEqual(txns[0]["counterparty"]["name"], "Acme Solar LLP")

    def test_search_matches_a_payee_the_description_does_not_name(self):
        """tx_transfer's description repeats its counterparty, so matching it
        proves nothing: the description clause alone would find it.

        tx_p2p has a blank description, so it is reachable only through
        counterparty_name.
        """
        db = _make_db()
        _seed_mixed(db)
        result = _call(transaction_tools.monzo_search_transactions, db, query="jane doe")
        self.assertEqual([t["id"] for t in result["transactions"]], ["tx_p2p"])

    def test_search_still_matches_merchant(self):
        db = _make_db()
        _seed_mixed(db)
        result = _call(transaction_tools.monzo_search_transactions, db, query="coffee")
        self.assertEqual([t["id"] for t in result["transactions"]], ["tx_card"])

    def test_search_no_match_returns_empty(self):
        db = _make_db()
        _seed_mixed(db)
        result = _call(transaction_tools.monzo_search_transactions, db, query="zzz-nomatch")
        self.assertEqual(result["transactions"], [])


if __name__ == "__main__":
    unittest.main()

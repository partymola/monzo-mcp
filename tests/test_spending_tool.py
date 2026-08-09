"""End-to-end tests for the monzo_spending tool.

Unlike test_spending.py (which checks the SQL patterns in isolation), these
drive the real monzo_spending tool against an in-memory database with auth,
auto-sync, and the DB connection patched out, and assert on its actual JSON
output - including the category summary, top merchants, detail mode, filters,
and the month-over-month vs_previous comparison.
"""

import asyncio
import json
import sqlite3
import unittest
from contextlib import contextmanager
from datetime import date
from unittest.mock import Mock, patch

from monzo_mcp import helpers
from monzo_mcp.db import SCHEMA
from monzo_mcp.tools import analysis_tools

# (id, account_id, account_type, created, amount_pence, currency,
#  description, merchant_name, category, notes, settled)
FIXTURE_TXNS = [
    # February 2026 spending
    (
        "f1",
        "acc_p",
        "personal",
        "2026-02-03T09:00:00Z",
        -5000,
        "GBP",
        "ACME GROCERS",
        "Acme Grocers",
        "groceries",
        "",
        "2026-02-03",
    ),
    (
        "f2",
        "acc_p",
        "personal",
        "2026-02-10T09:00:00Z",
        -3000,
        "GBP",
        "ACME GROCERS",
        "Acme Grocers",
        "groceries",
        "",
        "2026-02-10",
    ),
    (
        "f3",
        "acc_j",
        "joint",
        "2026-02-14T20:00:00Z",
        -2500,
        "GBP",
        "CORNER CAFE",
        "Corner Cafe",
        "eating_out",
        "",
        "2026-02-14",
    ),
    (
        "f4",
        "acc_j",
        "joint",
        "2026-02-20T12:00:00Z",
        -1000,
        "GBP",
        "Cash withdrawal",
        None,
        None,
        "",
        "2026-02-20",
    ),
    # February income - must be excluded from spending
    (
        "f5",
        "acc_p",
        "personal",
        "2026-02-01T00:00:00Z",
        100000,
        "GBP",
        "SALARY",
        None,
        "income",
        "",
        "2026-02-01",
    ),
    # January 2026 spending - the vs_previous baseline (total £40.00)
    (
        "f6",
        "acc_p",
        "personal",
        "2026-01-15T09:00:00Z",
        -4000,
        "GBP",
        "ACME GROCERS",
        "Acme Grocers",
        "groceries",
        "",
        "2026-01-15",
    ),
]


def make_db(txns=FIXTURE_TXNS):
    # check_same_thread=False: the tool runs its query in an anyio worker thread.
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    db.executemany(
        """INSERT INTO monzo_transactions
           (id, account_id, account_type, created, amount, currency,
            description, merchant_name, category, notes, settled)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        txns,
    )
    db.commit()
    return db


class _FixedDate(date):
    """date subclass with a pinned today() so vs_previous is deterministic."""

    @classmethod
    def today(cls):
        return cls(2026, 2, 15)


@contextmanager
def _patched(db):
    """Patch out auth, the DB, auto-sync, and today() for a tool call."""
    with (
        patch.object(helpers, "MONZO_CLIENT_PATH", _Exists()),
        patch.object(helpers, "MONZO_TOKENS_PATH", _Exists()),
        patch.object(analysis_tools, "get_db", lambda: db),
        patch.object(analysis_tools, "auto_sync_if_stale", lambda: None),
        patch.object(analysis_tools, "date", _FixedDate),
    ):
        yield


class _Exists:
    def exists(self):
        return True


class _NotExists:
    def exists(self):
        return False


def run_spending(db, **kwargs):
    with _patched(db):
        return json.loads(asyncio.run(analysis_tools.monzo_spending(**kwargs)))


class TestSpendingTool(unittest.TestCase):
    def test_category_summary(self):
        result = run_spending(make_db(), month="2026-02")
        self.assertEqual(result["month"], "2026-02")
        cats = {c["category"]: c for c in result["categories"]}
        self.assertEqual(cats["groceries"]["total"], 80.0)
        self.assertEqual(cats["groceries"]["count"], 2)
        self.assertEqual(cats["eating_out"]["total"], 25.0)
        # NULL category is surfaced with a readable label, not dropped.
        self.assertEqual(cats["(uncategorised)"]["total"], 10.0)
        self.assertEqual(result["grand_total"], 115.0)

    def test_categories_sorted_biggest_spend_first(self):
        result = run_spending(make_db(), month="2026-02")
        totals = [c["total"] for c in result["categories"]]
        self.assertEqual(totals, sorted(totals, reverse=True))
        self.assertEqual(result["categories"][0]["category"], "groceries")

    def test_income_excluded(self):
        # The +£1000 salary must not reduce or appear in spending.
        result = run_spending(make_db(), month="2026-02")
        self.assertNotIn("income", [c["category"] for c in result["categories"]])
        self.assertEqual(result["grand_total"], 115.0)

    def test_top_merchants(self):
        result = run_spending(make_db(), month="2026-02")
        merchants = {m["merchant"]: m for m in result["top_merchants"]}
        self.assertEqual(merchants["Acme Grocers"]["total"], 80.0)
        self.assertEqual(merchants["Acme Grocers"]["count"], 2)
        # NULL-merchant rows are excluded from the merchant breakdown.
        self.assertNotIn(None, merchants)
        self.assertEqual(result["top_merchants"][0]["merchant"], "Acme Grocers")

    def test_detail_mode(self):
        result = run_spending(make_db(), month="2026-02", detail=True)
        self.assertNotIn("categories", result)
        self.assertEqual(result["count"], 4)
        self.assertEqual(result["total"], -115.0)  # signed, spending is negative
        self.assertTrue(all(t["amount"] < 0 for t in result["transactions"]))

    def test_category_filter(self):
        result = run_spending(make_db(), month="2026-02", category="groceries")
        self.assertEqual([c["category"] for c in result["categories"]], ["groceries"])
        self.assertEqual(result["grand_total"], 80.0)

    def test_account_filter(self):
        result = run_spending(make_db(), month="2026-02", account_type="joint")
        cats = [c["category"] for c in result["categories"]]
        self.assertIn("eating_out", cats)
        self.assertNotIn("groceries", cats)  # groceries are personal-account here

    def test_empty_month(self):
        result = run_spending(make_db(), month="2026-05")
        self.assertEqual(result["categories"], [])
        self.assertEqual(result["grand_total"], 0)

    def test_no_data_returns_error(self):
        empty = sqlite3.connect(":memory:", check_same_thread=False)
        empty.row_factory = sqlite3.Row
        empty.executescript(SCHEMA)
        result = run_spending(empty, month="2026-02")
        self.assertIn("error", result)

    def test_vs_previous_month_comparison(self):
        # No explicit month -> defaults to pinned today (2026-02) and compares
        # against January (£40.00 baseline vs £115.00 this month).
        result = run_spending(make_db())
        self.assertEqual(result["month"], "2026-02")
        vs = result["vs_previous"]
        self.assertEqual(vs["previous_month"], "2026-01")
        self.assertEqual(vs["previous_total"], 40.0)
        self.assertEqual(vs["change"], 75.0)
        self.assertEqual(vs["change_pct"], 187.5)

    def test_vs_previous_applies_the_same_filters_as_the_month_it_compares(self):
        """A filtered month against an unfiltered one is not a comparison.

        It reports the difference between two different questions as a
        percentage change, in the tool whose whole purpose is comparison.
        """
        txns = [
            # This month: personal 10.00, joint 90.00.
            (
                "p1",
                "acc_p",
                "personal",
                "2026-02-05T09:00:00Z",
                -1000,
                "GBP",
                "ACME GROCERS",
                "Acme Grocers",
                "groceries",
                "",
                "2026-02-05",
            ),
            (
                "j1",
                "acc_j",
                "joint",
                "2026-02-06T09:00:00Z",
                -9000,
                "GBP",
                "CORNER CAFE",
                "Corner Cafe",
                "eating_out",
                "",
                "2026-02-06",
            ),
            # Last month: the same personal 10.00, the same joint 90.00.
            (
                "p0",
                "acc_p",
                "personal",
                "2026-01-05T09:00:00Z",
                -1000,
                "GBP",
                "ACME GROCERS",
                "Acme Grocers",
                "groceries",
                "",
                "2026-01-05",
            ),
            (
                "j0",
                "acc_j",
                "joint",
                "2026-01-06T09:00:00Z",
                -9000,
                "GBP",
                "CORNER CAFE",
                "Corner Cafe",
                "eating_out",
                "",
                "2026-01-06",
            ),
        ]
        result = run_spending(make_db(txns), account_type="personal")
        vs = result["vs_previous"]
        self.assertEqual(vs["previous_total"], 10.0)
        self.assertEqual(vs["change"], 0.0)
        self.assertEqual(vs["change_pct"], 0.0)

    def test_vs_previous_applies_a_category_filter_too(self):
        txns = [
            (
                "g1",
                "acc_p",
                "personal",
                "2026-02-05T09:00:00Z",
                -1000,
                "GBP",
                "ACME GROCERS",
                "Acme Grocers",
                "groceries",
                "",
                "2026-02-05",
            ),
            (
                "g0",
                "acc_p",
                "personal",
                "2026-01-05T09:00:00Z",
                -1000,
                "GBP",
                "ACME GROCERS",
                "Acme Grocers",
                "groceries",
                "",
                "2026-01-05",
            ),
            (
                "e0",
                "acc_p",
                "personal",
                "2026-01-06T09:00:00Z",
                -9000,
                "GBP",
                "CORNER CAFE",
                "Corner Cafe",
                "eating_out",
                "",
                "2026-01-06",
            ),
        ]
        result = run_spending(make_db(txns), category="groceries")
        vs = result["vs_previous"]
        self.assertEqual(vs["previous_total"], 10.0)
        self.assertEqual(vs["change_pct"], 0.0)

    def test_no_vs_previous_when_month_explicit(self):
        # An explicit month must not attach a month-over-month comparison.
        result = run_spending(make_db(), month="2026-02")
        self.assertNotIn("vs_previous", result)

    def test_detail_mode_empty(self):
        # Detail mode with no matching rows: empty list, zero total, no count key.
        result = run_spending(make_db(), month="2026-05", detail=True)
        self.assertEqual(result["transactions"], [])
        self.assertEqual(result["total"], 0)
        self.assertNotIn("count", result)

    def test_top_merchants_limited_to_15(self):
        # 16 distinct merchants -> only the 15 biggest spenders are returned.
        txns = [
            (
                f"m{i:02d}",
                "acc_p",
                "personal",
                "2026-02-05T09:00:00Z",
                -100 * (i + 1),
                "GBP",
                f"SHOP {i:02d}",
                f"Shop {i:02d}",
                "shopping",
                "",
                "2026-02-05",
            )
            for i in range(16)
        ]
        result = run_spending(make_db(txns), month="2026-02")
        self.assertEqual(len(result["top_merchants"]), 15)
        names = {m["merchant"] for m in result["top_merchants"]}
        self.assertNotIn("Shop 00", names)  # smallest spend (-£1.00) is dropped
        self.assertIn("Shop 15", names)  # biggest spend is retained

    def test_vs_previous_absent_when_no_prior_data(self):
        # February-only data, defaulting to the pinned today: no January baseline,
        # so vs_previous must be omitted (locks the `if prev_total_row` guard).
        feb_only = [t for t in FIXTURE_TXNS if t[3].startswith("2026-02") and t[4] < 0]
        result = run_spending(make_db(feb_only))
        self.assertNotIn("vs_previous", result)

    def test_auth_negative_path(self):
        # Missing credentials -> auth error, and the DB is never touched.
        db_factory = Mock()
        with (
            patch.object(helpers, "MONZO_CLIENT_PATH", _NotExists()),
            patch.object(helpers, "MONZO_TOKENS_PATH", _NotExists()),
            patch.object(analysis_tools, "get_db", db_factory),
        ):
            result = json.loads(asyncio.run(analysis_tools.monzo_spending(month="2026-02")))
        self.assertIn("error", result)
        db_factory.assert_not_called()


class TestAccountEcho(unittest.TestCase):
    """A zero total must say which account it was measuring."""

    def test_summary_names_the_account_filter(self):
        result = run_spending(make_db(), month="2026-02", account_type="personal")
        self.assertEqual(result["account_type"], "personal")

    def test_detail_names_the_account_filter(self):
        result = run_spending(make_db(), month="2026-02", detail=True, account_type="personal")
        self.assertEqual(result["account_type"], "personal")

    def test_empty_summary_still_names_the_account(self):
        # 2025-12 predates every fixture transaction, so this hits the no-rows path
        result = run_spending(make_db(), month="2025-12", account_type="joint")
        self.assertEqual(result["account_type"], "joint")
        self.assertEqual(result["categories"], [])
        self.assertEqual(result["grand_total"], 0)

    def test_empty_detail_still_names_the_account(self):
        result = run_spending(make_db(), month="2025-12", detail=True, account_type="joint")
        self.assertEqual(result["account_type"], "joint")
        self.assertEqual(result["transactions"], [])

    def test_unfiltered_echo_is_explicit_null_not_absent(self):
        result = run_spending(make_db(), month="2026-02")
        self.assertIn("account_type", result)
        self.assertIsNone(result["account_type"])


if __name__ == "__main__":
    unittest.main()

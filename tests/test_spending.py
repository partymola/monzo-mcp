"""Tests for spending analysis logic (pure SQLite, no API mocking needed)."""

import sqlite3
import unittest

from monzo_mcp.db import SCHEMA


def make_test_db_with_transactions():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    txns = [
        ("tx_001", "acc_1", "joint", "2026-02-05T10:00:00Z", -150000, "GBP", "REF-001", "Acme Housing", "bills", "", "2026-02-05"),
        ("tx_002", "acc_1", "joint", "2026-02-05T10:01:00Z", -20000, "GBP", "REF-002", "Local Council", "bills", "", "2026-02-05"),
        ("tx_003", "acc_1", "joint", "2026-02-07T12:00:00Z", -50000, "GBP", "Childcare", None, "general", "", "2026-02-07"),
        ("tx_004", "acc_1", "joint", "2026-02-07T12:01:00Z", -25000, "GBP", "Childcare", None, "general", "", "2026-02-07"),
        ("tx_005", "acc_2", "personal", "2026-02-10T14:00:00Z", -4500, "GBP", "Sparkle Clean", "Sparkle Cleaning", "shopping", "", "2026-02-10"),
        ("tx_006", "acc_1", "joint", "2026-02-15T18:00:00Z", -2500, "GBP", "FOOD APP", "Food App", "eating_out", "", "2026-02-15"),
        ("tx_007", "acc_1", "joint", "2026-02-02T03:00:00Z", 500000, "GBP", "Top-up", None, "general", "", "2026-02-02"),
        ("tx_008", "acc_1", "joint", "2026-01-15T10:00:00Z", -1800, "GBP", "FOOD APP", "Food App", "eating_out", "", "2026-01-15"),
    ]
    for tx in txns:
        db.execute(
            """INSERT INTO monzo_transactions
               (id, account_id, account_type, created, amount, currency,
                description, merchant_name, category, notes, settled)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", tx,
        )
    db.commit()
    return db


class TestSpendingQueries(unittest.TestCase):
    """Test the SQL patterns used by monzo_spending."""

    def setUp(self):
        self.db = make_test_db_with_transactions()

    def test_category_breakdown(self):
        rows = self.db.execute(
            """SELECT category, COUNT(*) as cnt, SUM(amount) as total
               FROM monzo_transactions
               WHERE amount < 0 AND created LIKE '2026-02%'
               GROUP BY category ORDER BY total ASC""",
        ).fetchall()
        totals = {r["category"]: r["total"] for r in rows}
        self.assertEqual(totals["bills"], -170000)
        self.assertIn("general", totals)
        self.assertIn("eating_out", totals)
        self.assertIn("shopping", totals)

    def test_spending_excludes_income(self):
        total = self.db.execute(
            "SELECT SUM(amount) FROM monzo_transactions WHERE amount < 0 AND created LIKE '2026-02%'"
        ).fetchone()[0]
        self.assertTrue(total < 0)
        income = self.db.execute(
            "SELECT SUM(amount) FROM monzo_transactions WHERE amount > 0 AND created LIKE '2026-02%'"
        ).fetchone()[0]
        self.assertEqual(income, 500000)

    def test_top_merchants(self):
        merchants = self.db.execute(
            """SELECT merchant_name, COUNT(*) as cnt, SUM(amount) as total
               FROM monzo_transactions
               WHERE amount < 0 AND created LIKE '2026-02%' AND merchant_name IS NOT NULL
               GROUP BY merchant_name ORDER BY total ASC LIMIT 15""",
        ).fetchall()
        self.assertEqual(merchants[0]["merchant_name"], "Acme Housing")

    def test_account_filter(self):
        rows = self.db.execute(
            "SELECT COUNT(*) FROM monzo_transactions WHERE amount < 0 AND account_type = 'personal'"
        ).fetchone()[0]
        self.assertEqual(rows, 1)

    def test_month_filter(self):
        jan = self.db.execute(
            "SELECT COUNT(*) FROM monzo_transactions WHERE created LIKE '2026-01%'"
        ).fetchone()[0]
        self.assertEqual(jan, 1)


class TestAuthHoldDedup(unittest.TestCase):
    """Test the auth-hold deduplication SQL."""

    def test_removes_unsettled_duplicate(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
        db.execute(
            """INSERT INTO monzo_transactions
               (id, account_id, account_type, created, amount, currency,
                description, merchant_name, category, notes, settled)
               VALUES ('tx_a', 'acc_1', 'joint', '2026-02-05T10:00:00Z', -500, 'GBP',
                       'test', 'Coffee Shop', 'eating_out', '', '2026-02-05')""")
        db.execute(
            """INSERT INTO monzo_transactions
               (id, account_id, account_type, created, amount, currency,
                description, merchant_name, category, notes, settled)
               VALUES ('tx_b', 'acc_1', 'joint', '2026-02-05T10:05:00Z', -500, 'GBP',
                       'test', 'Coffee Shop', 'eating_out', '', '')""")
        db.commit()

        dupes = db.execute("""
            DELETE FROM monzo_transactions WHERE id IN (
                SELECT a.id
                FROM monzo_transactions a
                JOIN monzo_transactions b ON a.amount = b.amount
                    AND a.merchant_name = b.merchant_name
                    AND a.account_type = b.account_type
                    AND a.id != b.id
                    AND ABS(JULIANDAY(a.created) - JULIANDAY(b.created)) < 0.01
                WHERE a.merchant_name IS NOT NULL
                AND (a.settled = '' OR a.settled IS NULL)
            )
        """).rowcount
        db.commit()

        self.assertEqual(dupes, 1)
        remaining = db.execute("SELECT id FROM monzo_transactions").fetchall()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["id"], "tx_a")

    def test_keeps_both_settled(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
        db.execute(
            """INSERT INTO monzo_transactions
               (id, account_id, account_type, created, amount, currency,
                description, merchant_name, category, notes, settled)
               VALUES ('tx_a', 'acc_1', 'joint', '2026-02-05T10:00:00Z', -500, 'GBP',
                       'test', 'Coffee Shop', 'eating_out', '', '2026-02-05')""")
        db.execute(
            """INSERT INTO monzo_transactions
               (id, account_id, account_type, created, amount, currency,
                description, merchant_name, category, notes, settled)
               VALUES ('tx_b', 'acc_1', 'joint', '2026-02-05T10:05:00Z', -500, 'GBP',
                       'test', 'Coffee Shop', 'eating_out', '', '2026-02-06')""")
        db.commit()

        dupes = db.execute("""
            DELETE FROM monzo_transactions WHERE id IN (
                SELECT a.id
                FROM monzo_transactions a
                JOIN monzo_transactions b ON a.amount = b.amount
                    AND a.merchant_name = b.merchant_name
                    AND a.account_type = b.account_type
                    AND a.id != b.id
                    AND ABS(JULIANDAY(a.created) - JULIANDAY(b.created)) < 0.01
                WHERE a.merchant_name IS NOT NULL
                AND (a.settled = '' OR a.settled IS NULL)
            )
        """).rowcount

        self.assertEqual(dupes, 0)
        count = db.execute("SELECT COUNT(*) FROM monzo_transactions").fetchone()[0]
        self.assertEqual(count, 2)


class TestTransactionSearch(unittest.TestCase):
    """Test search query patterns."""

    def setUp(self):
        self.db = make_test_db_with_transactions()

    def test_search_by_merchant(self):
        rows = self.db.execute(
            "SELECT * FROM monzo_transactions WHERE merchant_name LIKE ?",
            ("%Food App%",),
        ).fetchall()
        self.assertEqual(len(rows), 2)

    def test_search_by_description(self):
        rows = self.db.execute(
            "SELECT * FROM monzo_transactions WHERE description LIKE ?",
            ("%Childcare%",),
        ).fetchall()
        self.assertEqual(len(rows), 2)

    def test_search_case_insensitive(self):
        rows = self.db.execute(
            "SELECT * FROM monzo_transactions WHERE merchant_name LIKE ?",
            ("%food app%",),
        ).fetchall()
        self.assertEqual(len(rows), 2)


class TestMerchantExtraction(unittest.TestCase):
    """Test merchant name extraction from API response patterns."""

    def test_dict_merchant(self):
        tx = {"merchant": {"name": "Coffee Shop", "id": "merch_123"}}
        merchant_name = None
        if isinstance(tx.get("merchant"), dict):
            merchant_name = tx["merchant"].get("name")
        self.assertEqual(merchant_name, "Coffee Shop")

    def test_null_merchant(self):
        tx = {"merchant": None}
        merchant_name = None
        if isinstance(tx.get("merchant"), dict):
            merchant_name = tx["merchant"].get("name")
        self.assertIsNone(merchant_name)

    def test_missing_merchant(self):
        tx = {"description": "Bank transfer"}
        merchant_name = None
        if isinstance(tx.get("merchant"), dict):
            merchant_name = tx["merchant"].get("name")
        self.assertIsNone(merchant_name)

    def test_string_merchant_ignored(self):
        tx = {"merchant": "merch_123"}
        merchant_name = None
        if isinstance(tx.get("merchant"), dict):
            merchant_name = tx["merchant"].get("name")
        self.assertIsNone(merchant_name)


if __name__ == "__main__":
    unittest.main()

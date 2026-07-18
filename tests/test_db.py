"""Tests for database helpers and schema."""

import sqlite3
import unittest

from monzo_mcp.db import SCHEMA, log_sync, migrate, save_balance


def make_test_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


class TestSchema(unittest.TestCase):
    def test_creates_all_tables(self):
        db = make_test_db()
        tables = {
            r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        self.assertIn("monzo_transactions", tables)
        self.assertIn("balances", tables)
        self.assertIn("sync_log", tables)

    def test_creates_indexes(self):
        db = make_test_db()
        indexes = {
            r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
        self.assertIn("idx_txn_created", indexes)
        self.assertIn("idx_txn_account_type", indexes)
        self.assertIn("idx_txn_category", indexes)


# The transaction table as it existed before the counterparty columns were
# added, for exercising the ALTER TABLE migration path.
_OLD_SCHEMA = """
CREATE TABLE monzo_transactions (
    id TEXT PRIMARY KEY,
    account_id TEXT,
    account_type TEXT,
    created TEXT,
    amount INTEGER,
    currency TEXT,
    description TEXT,
    merchant_name TEXT,
    category TEXT,
    notes TEXT,
    settled TEXT
);
"""

_COUNTERPARTY_COLUMNS = {
    "counterparty_name",
    "counterparty_sort_code",
    "counterparty_account_number",
    "counterparty_user_id",
}


def _columns(db):
    return {r[1] for r in db.execute("PRAGMA table_info(monzo_transactions)").fetchall()}


class TestMigrate(unittest.TestCase):
    def test_adds_counterparty_columns_to_old_schema(self):
        db = sqlite3.connect(":memory:")
        db.executescript(_OLD_SCHEMA)
        db.execute("INSERT INTO monzo_transactions (id) VALUES ('tx_old')")
        self.assertFalse(_COUNTERPARTY_COLUMNS & _columns(db))

        migrate(db)

        self.assertTrue(_COUNTERPARTY_COLUMNS <= _columns(db))
        # Pre-existing rows survive with NULL counterparty fields
        row = db.execute("SELECT id, counterparty_name FROM monzo_transactions").fetchone()
        self.assertEqual(row[0], "tx_old")
        self.assertIsNone(row[1])

    def test_idempotent_on_current_schema(self):
        db = make_test_db()
        migrate(db)
        migrate(db)
        self.assertTrue(_COUNTERPARTY_COLUMNS <= _columns(db))

    def test_fresh_schema_already_has_counterparty_columns(self):
        db = make_test_db()
        self.assertTrue(_COUNTERPARTY_COLUMNS <= _columns(db))


class TestSaveBalance(unittest.TestCase):
    def test_saves_snapshot(self):
        db = make_test_db()
        save_balance(db, "personal", "Monzo Personal", 123456)
        row = db.execute("SELECT * FROM balances").fetchone()
        self.assertEqual(row["account_type"], "personal")
        self.assertEqual(row["name"], "Monzo Personal")
        self.assertEqual(row["balance"], 123456)
        self.assertEqual(row["currency"], "GBP")
        self.assertIsNotNone(row["captured_at"])

    def test_multiple_snapshots(self):
        db = make_test_db()
        save_balance(db, "joint", "Monzo Joint", 100000)
        save_balance(db, "joint", "Monzo Joint", 200000)
        count = db.execute("SELECT COUNT(*) FROM balances").fetchone()[0]
        self.assertEqual(count, 2)


class TestLogSync(unittest.TestCase):
    def test_logs_sync(self):
        db = make_test_db()
        log_sync(db, "ok", 42, "test notes")
        row = db.execute("SELECT * FROM sync_log").fetchone()
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["records_added"], 42)
        self.assertEqual(row["notes"], "test notes")
        self.assertIsNotNone(row["synced_at"])


if __name__ == "__main__":
    unittest.main()

"""Tests for database helpers and schema."""

import sqlite3
import unittest

from monzo_mcp.db import SCHEMA, save_balance, log_sync


def make_test_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


class TestSchema(unittest.TestCase):
    def test_creates_all_tables(self):
        db = make_test_db()
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        self.assertIn("monzo_transactions", tables)
        self.assertIn("balances", tables)
        self.assertIn("sync_log", tables)

    def test_creates_indexes(self):
        db = make_test_db()
        indexes = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()}
        self.assertIn("idx_txn_created", indexes)
        self.assertIn("idx_txn_account_type", indexes)
        self.assertIn("idx_txn_category", indexes)


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

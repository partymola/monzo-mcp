"""Tests for run_sync, focused on the explicit `since` backfill parameter.

The Monzo HTTP client is mocked (`api.get`) and the cache is an in-memory
SQLite DB, so these run without credentials or network access.
"""

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from monzo_mcp.db import SCHEMA
from monzo_mcp.tools import transaction_tools


def _make_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


class _FakeApi:
    """Records `api.get` calls and returns canned Monzo responses.

    One open account and a single page of transactions (fewer than 100, so the
    sync loop stops after one fetch).
    """

    def __init__(self):
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        if path == "/accounts":
            return {"accounts": [{"id": "acc_1", "type": "uk_retail"}]}
        if path.startswith("/balance"):
            return {"balance": 1000, "currency": "GBP"}
        if path.startswith("/pots"):
            return {"pots": []}
        if path.startswith("/transactions"):
            # Cursor-paginated follow-ups pass the last transaction id as
            # `since`; return nothing for those to end the loop.
            since = parse_qs(urlparse(path).query).get("since", [None])[0]
            if since and since.startswith("tx_"):
                return {"transactions": []}
            return {
                "transactions": [
                    {
                        "id": "tx_001",
                        "created": "2026-02-01T10:00:00Z",
                        "amount": -500,
                        "currency": "GBP",
                        "description": "Coffee",
                        "merchant": {"name": "Cafe"},
                        "category": "eating_out",
                        "notes": "",
                        "settled": "2026-02-01",
                    }
                ]
            }
        return {}

    def first_transactions_since(self):
        """The `since` query param of the first /transactions request."""
        for path in self.calls:
            if path.startswith("/transactions"):
                return parse_qs(urlparse(path).query).get("since", [None])[0]
        return None


class TestRunSyncSince(unittest.TestCase):
    def _run(self, **kwargs):
        fake = _FakeApi()
        db = _make_db()
        with (
            patch.object(transaction_tools.api, "get", fake.get),
            patch.object(transaction_tools, "get_db", lambda: db),
        ):
            result = transaction_tools.run_sync(**kwargs)
        return fake, result

    def test_date_only_since_coerced_to_midnight_utc(self):
        fake, result = self._run(since="2026-01-01")
        self.assertEqual(fake.first_transactions_since(), "2026-01-01T00:00:00Z")
        self.assertNotIn("error", result)

    def test_full_datetime_since_kept_as_z(self):
        fake, _ = self._run(since="2026-01-01T14:30:00Z")
        self.assertEqual(fake.first_transactions_since(), "2026-01-01T14:30:00Z")

    def test_naive_datetime_treated_as_utc(self):
        fake, _ = self._run(since="2026-01-01T14:30:00")
        self.assertEqual(fake.first_transactions_since(), "2026-01-01T14:30:00Z")

    def test_offset_datetime_converted_to_utc(self):
        fake, _ = self._run(since="2026-01-01T14:30:00+02:00")
        self.assertEqual(fake.first_transactions_since(), "2026-01-01T12:30:00Z")

    def test_invalid_since_returns_error_without_calling_api(self):
        fake = _FakeApi()
        db = _make_db()
        with (
            patch.object(transaction_tools.api, "get", fake.get),
            patch.object(transaction_tools, "get_db", lambda: db),
        ):
            result = transaction_tools.run_sync(since="not-a-date")
        self.assertIn("error", result)
        self.assertEqual(fake.calls, [])  # bailed before any API call

    def test_default_since_is_about_335_days_ago(self):
        fake, _ = self._run()
        since = fake.first_transactions_since()
        self.assertTrue(since.endswith("T00:00:00Z"))
        expected_day = (datetime.now(timezone.utc) - timedelta(days=335)).strftime("%Y-%m-%d")
        self.assertTrue(since.startswith(expected_day))

    def test_since_run_upserts_transactions(self):
        _, result = self._run(since="2026-01-01")
        self.assertEqual(result["transactions_upserted"], 1)
        self.assertEqual(result["accounts_synced"], 1)


if __name__ == "__main__":
    unittest.main()

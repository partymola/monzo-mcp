"""Tests for run_sync's paging, auth-hold dedup, SCA fallback, and filtering.

Complements test_sync.py (which covers the `since` coercion). The Monzo HTTP
client is mocked and the cache is in-memory SQLite, so no credentials or network
are needed. Expected values are hand-derived from the fixtures.
"""

import json
import pathlib
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from monzo_mcp.api import MonzoAPIError, MonzoAuthError, MonzoSCAError
from monzo_mcp.db import SCHEMA
from monzo_mcp.tools import transaction_tools


def _mk(txn_id, *, amount, created, settled, merchant="Acme Grocers", category="groceries"):
    """Build a Monzo-API-shaped transaction dict."""
    return {
        "id": txn_id,
        "created": created,
        "amount": amount,
        "currency": "GBP",
        "description": merchant.upper(),
        "merchant": {"name": merchant},
        "category": category,
        "notes": "",
        "settled": settled,
    }


class _FakeApi:
    """Configurable stand-in for monzo_mcp.api with call recording.

    `txns_for(account_id, since, call_n)` returns a page of transactions or
    raises; `balance` is returned for every /balance request.
    """

    def __init__(self, accounts, txns_for, balance=1000):
        self.accounts = accounts
        self.txns_for = txns_for
        self.balance = balance
        self.calls = []
        self._txn_calls = 0

    def get(self, path):
        self.calls.append(path)
        if path == "/accounts":
            return {"accounts": self.accounts}
        if path.startswith("/balance"):
            return {"balance": self.balance, "currency": "GBP"}
        if path.startswith("/pots"):
            return {"pots": []}
        if path.startswith("/transactions"):
            q = parse_qs(urlparse(path).query)
            self._txn_calls += 1
            return {
                "transactions": self.txns_for(
                    q["account_id"][0], q.get("since", [None])[0], self._txn_calls
                )
            }
        return {}

    def txn_calls_for(self, account_id):
        return [
            p
            for p in self.calls
            if p.startswith("/transactions") and f"account_id={account_id}" in p
        ]

    def balance_calls_for(self, account_id):
        return [
            p for p in self.calls if p.startswith("/balance") and f"account_id={account_id}" in p
        ]


def _make_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def _run(fake, db, **kwargs):
    with (
        patch.object(transaction_tools.api, "get", fake.get),
        patch.object(transaction_tools, "get_db", lambda: db),
    ):
        return transaction_tools.run_sync(**kwargs)


class TestPaging(unittest.TestCase):
    def test_full_page_triggers_a_second_fetch(self):
        # Distinct merchants/amounts keep paging orthogonal to the auth-hold
        # dedup pass, so the row counts below reflect paging alone.
        page0 = [
            _mk(
                f"tx_{i:03d}",
                amount=-100 * (i + 1),
                created=f"2026-02-01T10:{i % 60:02d}:00Z",
                settled="2026-02-01",
                merchant=f"Shop {i:03d}",
            )
            for i in range(100)
        ]
        page1 = [
            _mk(
                "tx_100",
                amount=-100,
                created="2026-02-02T10:00:00Z",
                settled="2026-02-02",
                merchant="Shop 100",
            )
        ]

        def txns_for(acct, since, n):
            if since is None or not since.startswith("tx_"):
                return page0  # first page: exactly 100 -> loop continues
            if since == "tx_099":
                return page1  # cursor follow-up: 1 row (<100) -> loop stops
            return []

        fake = _FakeApi([{"id": "acc_1", "type": "uk_retail"}], txns_for)
        result = _run(fake, _make_db())

        # 100 + 1 rows upserted across exactly two /transactions fetches.
        self.assertEqual(result["transactions_upserted"], 101)
        self.assertEqual(len(fake.txn_calls_for("acc_1")), 2)
        self.assertEqual(result["details"][0]["total_in_db"], 101)

    def test_partial_first_page_stops_immediately(self):
        def txns_for(acct, since, n):
            if since is None or not since.startswith("tx_"):
                return [_mk("tx_a", amount=-100, created="2026-02-01T10:00:00Z", settled="")]
            return []

        fake = _FakeApi([{"id": "acc_1", "type": "uk_retail"}], txns_for)
        result = _run(fake, _make_db())
        self.assertEqual(result["transactions_upserted"], 1)
        self.assertEqual(len(fake.txn_calls_for("acc_1")), 1)  # no needless follow-up


class TestAuthHoldDedup(unittest.TestCase):
    def _shared_named(self, name):
        # Shared-cache in-memory DB: a holder connection keeps the data alive
        # after run_sync closes its own connection, so we can inspect the rows.
        # A distinct name per test avoids cross-test sharing of the cache.
        uri = f"file:{name}?mode=memory&cache=shared"
        holder = sqlite3.connect(uri, uri=True)
        holder.row_factory = sqlite3.Row
        holder.executescript(SCHEMA)

        def get_db():
            c = sqlite3.connect(uri, uri=True)
            c.row_factory = sqlite3.Row
            return c

        return holder, get_db

    def test_unsettled_hold_removed_settled_and_distinct_kept(self):
        # dup_hold (unsettled) + dup_settled: same merchant/amount/account within
        # ~5 min -> the unsettled hold is the auth-hold duplicate and is removed.
        # both_a/both_b are both settled -> a genuine repeat pair, kept.
        # solo has a distinct amount -> kept.
        page = [
            _mk("dup_hold", amount=-1500, created="2026-02-01T10:00:00Z", settled=""),
            _mk("dup_settled", amount=-1500, created="2026-02-01T10:05:00Z", settled="2026-02-01"),
            _mk(
                "both_a",
                amount=-2000,
                created="2026-02-02T10:00:00Z",
                settled="2026-02-02",
                merchant="Beta Store",
            ),
            _mk(
                "both_b",
                amount=-2000,
                created="2026-02-02T10:05:00Z",
                settled="2026-02-02",
                merchant="Beta Store",
            ),
            _mk("solo", amount=-999, created="2026-02-03T10:00:00Z", settled="2026-02-03"),
        ]

        def txns_for(acct, since, n):
            if since is None or not since.startswith("tx_"):
                return page
            return []

        holder, get_db = self._shared_named("dedup_test")
        fake = _FakeApi([{"id": "acc_1", "type": "uk_retail"}], txns_for)
        with (
            patch.object(transaction_tools.api, "get", fake.get),
            patch.object(transaction_tools, "get_db", get_db),
        ):
            result = transaction_tools.run_sync()

        self.assertEqual(result["transactions_upserted"], 5)  # counted before dedup
        self.assertEqual(result["duplicates_removed"], 1)
        remaining = {
            r["id"] for r in holder.execute("SELECT id FROM monzo_transactions").fetchall()
        }
        self.assertEqual(remaining, {"dup_settled", "both_a", "both_b", "solo"})
        holder.close()

    def test_matching_pair_outside_time_window_is_kept(self):
        # Same merchant/amount/account and one unsettled, but ~1 hour apart -
        # outside the ~14 min auth-hold window - so it is a genuine repeat, not a
        # hold, and must NOT be deduped. Pins the upper bound of the time window.
        page = [
            _mk("far_hold", amount=-1500, created="2026-02-01T10:00:00Z", settled=""),
            _mk("far_settled", amount=-1500, created="2026-02-01T11:00:00Z", settled="2026-02-01"),
        ]

        def txns_for(acct, since, n):
            if since is None or not since.startswith("tx_"):
                return page
            return []

        holder, get_db = self._shared_named("window_test")
        fake = _FakeApi([{"id": "acc_1", "type": "uk_retail"}], txns_for)
        with (
            patch.object(transaction_tools.api, "get", fake.get),
            patch.object(transaction_tools, "get_db", get_db),
        ):
            result = transaction_tools.run_sync()

        self.assertEqual(result["duplicates_removed"], 0)
        remaining = {
            r["id"] for r in holder.execute("SELECT id FROM monzo_transactions").fetchall()
        }
        self.assertEqual(remaining, {"far_hold", "far_settled"})
        holder.close()


class TestCounterpartySync(unittest.TestCase):
    def test_counterparty_persisted_for_transfers_and_null_for_cards(self):
        transfer = _mk("tx_fp", amount=-39900, created="2026-02-01T10:00:00Z", settled="2026-02-01")
        transfer["merchant"] = None
        transfer["description"] = "Acme Solar LLP"
        transfer["counterparty"] = {
            "name": "Acme Solar LLP",
            "sort_code": "123456",
            "account_number": "12345678",
            "user_id": "anonuser_ext1",
        }
        p2p = _mk("tx_p2p", amount=-2500, created="2026-02-01T11:00:00Z", settled="2026-02-01")
        p2p["merchant"] = None
        p2p["counterparty"] = {"name": "Jane Doe", "user_id": "user_friend1"}
        card = _mk("tx_card", amount=-500, created="2026-02-02T10:00:00Z", settled="2026-02-02")

        def txns_for(acct, since, n):
            if since is None or not since.startswith("tx_"):
                return [transfer, p2p, card]
            return []

        uri = "file:counterparty_sync_test?mode=memory&cache=shared"
        holder = sqlite3.connect(uri, uri=True)
        holder.row_factory = sqlite3.Row
        holder.executescript(SCHEMA)

        def get_db():
            c = sqlite3.connect(uri, uri=True)
            c.row_factory = sqlite3.Row
            return c

        fake = _FakeApi([{"id": "acc_1", "type": "uk_retail"}], txns_for)
        with (
            patch.object(transaction_tools.api, "get", fake.get),
            patch.object(transaction_tools, "get_db", get_db),
        ):
            result = transaction_tools.run_sync()

        self.assertEqual(result["transactions_upserted"], 3)
        rows = {r["id"]: r for r in holder.execute("SELECT * FROM monzo_transactions").fetchall()}
        self.assertEqual(rows["tx_fp"]["counterparty_name"], "Acme Solar LLP")
        self.assertEqual(rows["tx_fp"]["counterparty_sort_code"], "123456")
        self.assertEqual(rows["tx_fp"]["counterparty_account_number"], "12345678")
        self.assertEqual(rows["tx_fp"]["counterparty_user_id"], "anonuser_ext1")
        # p2p payments carry only name + user_id
        self.assertEqual(rows["tx_p2p"]["counterparty_name"], "Jane Doe")
        self.assertIsNone(rows["tx_p2p"]["counterparty_sort_code"])
        # Card transactions have no counterparty at all
        self.assertIsNone(rows["tx_card"]["counterparty_name"])
        self.assertIsNone(rows["tx_card"]["counterparty_user_id"])
        holder.close()


class TestScaFallback(unittest.TestCase):
    def test_sca_on_first_fetch_falls_back_to_90_days(self):
        # First /transactions raises SCA; on page 0 run_sync retries with a
        # 90-day fallback window. With no prior rows, it records the sca_note.
        def txns_for(acct, since, n):
            if n == 1:
                raise MonzoSCAError("SCA required")
            if n == 2:
                return [_mk("tx_fb", amount=-100, created="2026-02-01T10:00:00Z", settled="")]
            return []

        fake = _FakeApi([{"id": "acc_1", "type": "uk_retail"}], txns_for)
        result = _run(fake, _make_db())

        self.assertEqual(result["transactions_upserted"], 1)
        self.assertEqual(
            result["details"][0]["sca_note"], "SCA window expired, fetched last 90 days only"
        )
        self.assertEqual(len(fake.txn_calls_for("acc_1")), 2)  # initial + fallback

    def test_sca_on_both_fetches_records_approval_note(self):
        def txns_for(acct, since, n):
            raise MonzoSCAError("SCA required")

        fake = _FakeApi([{"id": "acc_1", "type": "uk_retail"}], txns_for)
        result = _run(fake, _make_db())

        self.assertEqual(result["transactions_upserted"], 0)
        self.assertEqual(result["details"][0]["sca_note"], "SCA required - approve in Monzo app")


class TestAccountSelection(unittest.TestCase):
    def _txns_one_each(self):
        def txns_for(acct, since, n):
            if since is None or not since.startswith("tx_"):
                return [_mk(f"tx_{acct}", amount=-100, created="2026-02-01T10:00:00Z", settled="")]
            return []

        return txns_for

    def test_closed_account_is_skipped(self):
        accounts = [
            {"id": "acc_open", "type": "uk_retail"},
            {"id": "acc_closed", "type": "uk_retail", "closed": True},
        ]
        fake = _FakeApi(accounts, self._txns_one_each())
        result = _run(fake, _make_db())

        self.assertEqual(result["accounts_synced"], 1)
        # The closed account is never touched (no balance or transaction fetch).
        self.assertEqual(fake.balance_calls_for("acc_closed"), [])
        self.assertEqual(fake.txn_calls_for("acc_closed"), [])

    def test_account_type_filter_syncs_only_matching_account(self):
        accounts = [
            {"id": "acc_personal", "type": "uk_retail"},
            {"id": "acc_joint", "type": "uk_retail_joint"},
        ]
        fake = _FakeApi(accounts, self._txns_one_each())
        result = _run(fake, _make_db(), account_type="joint")

        self.assertEqual(result["accounts_synced"], 1)
        self.assertEqual(result["details"][0]["account_type"], "joint")
        # The personal account is filtered out before any transaction fetch.
        self.assertEqual(fake.txn_calls_for("acc_personal"), [])
        self.assertEqual(len(fake.txn_calls_for("acc_joint")), 1)

    def test_no_accounts_returns_error(self):
        fake = _FakeApi([], self._txns_one_each())
        result = _run(fake, _make_db())
        self.assertEqual(result["error"], "No Monzo accounts found")


class TestAnUnreadableResponseIsNotAnSCAPrompt(unittest.TestCase):
    """Only an SCA refusal earns the SCA note, and a failed sync is not "ok".

    Telling the user to approve something in the Monzo app is the wrong
    answer to a dropped connection, and logging the run as ok suppresses the
    retry for the rest of the day.
    """

    def _run_with_transactions_failing(self, exc):
        self._tmp = tempfile.TemporaryDirectory()
        db_path = pathlib.Path(self._tmp.name) / "monzo.db"
        db_conn = sqlite3.connect(db_path)
        db_conn.row_factory = sqlite3.Row
        db_conn.executescript(SCHEMA)

        def fake_get(path):
            if path == "/accounts":
                return {"accounts": [{"id": "acc_1", "type": "uk_retail", "closed": False}]}
            if path.startswith("/balance") or path.startswith("/pots"):
                return {"balance": 0, "currency": "GBP", "pots": []}
            raise exc

        with (
            patch.object(transaction_tools, "get_db", return_value=db_conn),
            patch.object(transaction_tools.api, "get", side_effect=fake_get),
        ):
            result = transaction_tools.run_sync()

        reopened = sqlite3.connect(db_path)
        rows = reopened.execute("SELECT status FROM sync_log").fetchall()
        reopened.close()
        self._tmp.cleanup()
        return result, [r[0] for r in rows]

    def test_an_api_error_is_not_reported_as_sca(self):
        result, _ = self._run_with_transactions_failing(
            MonzoAPIError("Monzo returned an unreadable response.")
        )
        detail = result["details"][0]
        assert "sca_note" not in detail
        assert "transactions_error" in detail

    def test_an_sca_error_is_still_reported_as_sca(self):
        result, _ = self._run_with_transactions_failing(MonzoSCAError("SCA"))
        assert "SCA" in result["details"][0]["sca_note"]

    def test_a_failed_sync_is_not_logged_as_ok(self):
        _, statuses = self._run_with_transactions_failing(
            MonzoAPIError("Monzo returned an unreadable response.")
        )
        assert statuses == ["error"]

    def test_a_clean_sync_is_still_logged_as_ok(self):
        tmp = tempfile.TemporaryDirectory()
        db_path = pathlib.Path(tmp.name) / "monzo.db"
        db_conn = sqlite3.connect(db_path)
        db_conn.row_factory = sqlite3.Row
        db_conn.executescript(SCHEMA)

        def fake_get(path):
            if path == "/accounts":
                return {"accounts": [{"id": "acc_1", "type": "uk_retail", "closed": False}]}
            if path.startswith("/transactions"):
                return {"transactions": []}
            return {"balance": 0, "currency": "GBP", "pots": []}

        with (
            patch.object(transaction_tools, "get_db", return_value=db_conn),
            patch.object(transaction_tools.api, "get", side_effect=fake_get),
        ):
            transaction_tools.run_sync()

        reopened = sqlite3.connect(db_path)
        rows = reopened.execute("SELECT status FROM sync_log").fetchall()
        reopened.close()
        tmp.cleanup()
        assert [r[0] for r in rows] == ["ok"]


class TestALaterPageFailingIsAlsoAFailure(unittest.TestCase):
    """Page 0 is not the only page, and a hole in the history is not a success.

    A later page failing used to break out of the loop recording nothing, so
    the run was logged ok with whatever it had managed to fetch, and the
    throttle then suppressed the next sync for the rest of the day over a
    partial cache.
    """

    def test_a_failure_on_a_later_page_is_recorded(self):
        tmp = tempfile.TemporaryDirectory()
        db_path = pathlib.Path(tmp.name) / "monzo.db"
        db_conn = sqlite3.connect(db_path)
        db_conn.row_factory = sqlite3.Row
        db_conn.executescript(SCHEMA)

        calls = {"n": 0}
        full_page = [
            _mk(f"tx_{i}", amount=-100, created=f"2026-03-{i % 28 + 1:02d}T10:00:00Z", settled="s")
            for i in range(100)
        ]

        def fake_get(path):
            if path == "/accounts":
                return {"accounts": [{"id": "acc_1", "type": "uk_retail", "closed": False}]}
            if path.startswith("/transactions"):
                calls["n"] += 1
                if calls["n"] == 1:
                    return {"transactions": full_page}
                raise MonzoAPIError("Monzo returned an unreadable response.")
            return {"balance": 0, "currency": "GBP", "pots": []}

        with (
            patch.object(transaction_tools, "get_db", return_value=db_conn),
            patch.object(transaction_tools.api, "get", side_effect=fake_get),
        ):
            result = transaction_tools.run_sync()

        reopened = sqlite3.connect(db_path)
        statuses = [r[0] for r in reopened.execute("SELECT status FROM sync_log").fetchall()]
        reopened.close()
        tmp.cleanup()

        assert "transactions_error" in result["details"][0]
        assert statuses == ["error"]


class TestTheAutoSyncThrottleCountsAttempts(unittest.TestCase):
    """The throttle has to count attempts, not successes.

    Gating on the last successful sync means a sync that keeps failing never
    advances the timestamp that gates it, so every tool call starts a fresh
    full sync - answering a rate limit or a captive portal by retrying
    continuously, and writing a sync_log row each time.
    """

    def _runs_after_a_failure_today(self):
        tmp = tempfile.TemporaryDirectory()
        db_path = pathlib.Path(tmp.name) / "monzo.db"
        db_conn = sqlite3.connect(db_path)
        db_conn.row_factory = sqlite3.Row
        db_conn.executescript(SCHEMA)
        db_conn.execute(
            "INSERT INTO sync_log (synced_at, status, records_added, notes) VALUES (?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), "error", 0, ""),
        )
        db_conn.commit()

        ran = {"n": 0}
        with (
            patch.object(transaction_tools, "get_db", return_value=db_conn),
            patch.object(transaction_tools, "run_sync", lambda *a, **k: ran.__setitem__("n", 1)),
        ):
            transaction_tools.auto_sync_if_stale()
        tmp.cleanup()
        return ran["n"]

    def test_a_failure_today_still_throttles_the_rest_of_the_day(self):
        assert self._runs_after_a_failure_today() == 0

    def test_a_local_date_ahead_of_utc_does_not_defeat_the_throttle(self):
        """log_sync stores UTC; comparing it to a local date reopens the storm.

        East of Greenwich there is a window each night where the row just
        written is dated behind the local today, so the gate never closes and
        never self-corrects - one hour in BST, half a day at UTC+13.
        """
        tmp = tempfile.TemporaryDirectory()
        db_path = pathlib.Path(tmp.name) / "monzo.db"
        db_conn = sqlite3.connect(db_path)
        db_conn.row_factory = sqlite3.Row
        db_conn.executescript(SCHEMA)
        # 23:30 UTC on the 9th: a sync that has only just finished.
        db_conn.execute(
            "INSERT INTO sync_log (synced_at, status, records_added, notes) VALUES (?, ?, ?, ?)",
            (datetime(2026, 8, 9, 23, 30, tzinfo=timezone.utc).isoformat(), "ok", 0, ""),
        )
        db_conn.commit()

        class _LocalIsAheadDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 8, 10)

        class _FixedUTC(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 8, 9, 23, 35, tzinfo=timezone.utc)

        ran = {"n": 0}
        with (
            patch.object(transaction_tools, "get_db", return_value=db_conn),
            # create=True so this still binds if the module stops importing
            # date at all - which is the shape that passes.
            patch.object(transaction_tools, "date", _LocalIsAheadDate, create=True),
            patch.object(transaction_tools, "datetime", _FixedUTC),
            patch.object(transaction_tools, "run_sync", lambda *a, **k: ran.__setitem__("n", 1)),
        ):
            transaction_tools.auto_sync_if_stale()
        tmp.cleanup()

        assert ran["n"] == 0


class TestNoExceptionTextReachesTheModel(unittest.TestCase):
    """A bare except cannot know what its message set holds.

    These details are returned to the model, so they carry the type only -
    the same rule the trailing catch-all already follows.
    """

    def _details_when_balance_and_pots_fail(self, exc):
        tmp = tempfile.TemporaryDirectory()
        db_path = pathlib.Path(tmp.name) / "monzo.db"
        db_conn = sqlite3.connect(db_path)
        db_conn.row_factory = sqlite3.Row
        db_conn.executescript(SCHEMA)

        def fake_get(path):
            if path == "/accounts":
                return {"accounts": [{"id": "acc_1", "type": "uk_retail", "closed": False}]}
            if path.startswith("/balance") or path.startswith("/pots"):
                raise exc
            return {"transactions": []}

        with (
            patch.object(transaction_tools, "get_db", return_value=db_conn),
            patch.object(transaction_tools.api, "get", side_effect=fake_get),
        ):
            result = transaction_tools.run_sync()
        tmp.cleanup()
        return result["details"][0]

    def test_neither_balance_nor_pots_repeats_the_message(self):
        secret = "token file /home/alice/.config/monzo_tokens.json unreadable"
        detail = self._details_when_balance_and_pots_fail(RuntimeError(secret))
        assert detail["balance_error"] == "RuntimeError"
        assert detail["pots_error"] == "RuntimeError"
        assert "/home/alice" not in json.dumps(detail)

    def test_a_stale_cache_does_still_trigger_a_sync(self):
        """The companion assertion.

        Without it, the throttle test above passes whether the throttle works
        or auto_sync_if_stale raises into its own bare except.
        """
        tmp = tempfile.TemporaryDirectory()
        db_path = pathlib.Path(tmp.name) / "monzo.db"
        db_conn = sqlite3.connect(db_path)
        db_conn.row_factory = sqlite3.Row
        db_conn.executescript(SCHEMA)
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        db_conn.execute(
            "INSERT INTO sync_log (synced_at, status, records_added, notes) VALUES (?, ?, ?, ?)",
            (yesterday.isoformat(), "ok", 0, ""),
        )
        db_conn.commit()

        ran = {"n": 0}
        with (
            patch.object(transaction_tools, "get_db", return_value=db_conn),
            patch.object(transaction_tools, "run_sync", lambda *a, **k: ran.__setitem__("n", 1)),
        ):
            transaction_tools.auto_sync_if_stale()
        tmp.cleanup()
        assert ran["n"] == 1


class TestEveryExitFromRunSyncLeavesARow(unittest.TestCase):
    """sync_log is the only record a run leaves.

    The database is opened before the first API call because /accounts is
    where a token failure surfaces, and the exit that fails there needs a
    connection to write on.
    """

    def _statuses_after(self, fake_get):
        tmp = tempfile.TemporaryDirectory()
        db_path = pathlib.Path(tmp.name) / "monzo.db"
        db_conn = sqlite3.connect(db_path)
        db_conn.row_factory = sqlite3.Row
        db_conn.executescript(SCHEMA)

        raised = None
        with (
            patch.object(transaction_tools, "get_db", return_value=db_conn),
            patch.object(transaction_tools.api, "get", side_effect=fake_get),
        ):
            try:
                transaction_tools.run_sync()
            except Exception as e:  # noqa: BLE001 - the test is what escapes
                raised = e

        reopened = sqlite3.connect(db_path)
        rows = reopened.execute("SELECT status, notes FROM sync_log").fetchall()
        reopened.close()
        tmp.cleanup()
        return raised, rows

    def test_a_failure_on_the_account_lookup_is_recorded(self):
        def fake_get(path):
            raise MonzoAuthError("Could not obtain an access token.")

        raised, rows = self._statuses_after(fake_get)
        assert isinstance(raised, MonzoAuthError)
        assert rows and rows[0][0] == "error"
        assert "MonzoAuthError" in rows[0][1]

    def test_finding_no_accounts_is_recorded(self):
        def fake_get(path):
            return {"accounts": []}

        raised, rows = self._statuses_after(fake_get)
        assert raised is None
        assert rows and rows[0][0] == "error"

    def test_a_database_that_cannot_record_it_does_not_replace_the_error(self):
        """The row is best-effort; losing it must not hide the cause."""
        tmp = tempfile.TemporaryDirectory()
        db_path = pathlib.Path(tmp.name) / "monzo.db"
        db_conn = sqlite3.connect(db_path)
        db_conn.row_factory = sqlite3.Row
        db_conn.executescript(SCHEMA)

        with (
            patch.object(transaction_tools, "get_db", return_value=db_conn),
            patch.object(
                transaction_tools.api,
                "get",
                side_effect=MonzoAuthError("Could not obtain an access token."),
            ),
            patch.object(
                transaction_tools,
                "log_sync",
                MagicMock(side_effect=sqlite3.OperationalError("readonly database")),
            ),
        ):
            with self.assertRaises(MonzoAuthError):
                transaction_tools.run_sync()
        tmp.cleanup()


class TestSCAIsNotAFailureOnAnyPage(unittest.TestCase):
    """Only the user approving in the Monzo app changes an SCA outcome.

    Logging it as a failure would have the throttle retry something no
    retry can fix. The page-0 branch always did this; the later-page branch
    did not exist until it was added, and briefly logged SCA as an error.
    """

    def _run_with_sca_on_page(self, page_index):
        tmp = tempfile.TemporaryDirectory()
        db_path = pathlib.Path(tmp.name) / "monzo.db"
        db_conn = sqlite3.connect(db_path)
        db_conn.row_factory = sqlite3.Row
        db_conn.executescript(SCHEMA)

        calls = {"n": 0}
        full_page = [
            _mk(f"tx_{i}", amount=-100, created=f"2026-03-{i % 28 + 1:02d}T10:00:00Z", settled="s")
            for i in range(100)
        ]

        def fake_get(path):
            if path == "/accounts":
                return {"accounts": [{"id": "acc_1", "type": "uk_retail", "closed": False}]}
            if path.startswith("/transactions"):
                calls["n"] += 1
                if calls["n"] > page_index:
                    raise MonzoSCAError("SCA required")
                return {"transactions": full_page}
            return {"balance": 0, "currency": "GBP", "pots": []}

        with (
            patch.object(transaction_tools, "get_db", return_value=db_conn),
            patch.object(transaction_tools.api, "get", side_effect=fake_get),
        ):
            result = transaction_tools.run_sync()

        reopened = sqlite3.connect(db_path)
        statuses = [r[0] for r in reopened.execute("SELECT status FROM sync_log").fetchall()]
        reopened.close()
        tmp.cleanup()
        return result["details"][0], statuses

    def test_sca_on_a_later_page_is_a_note_not_an_error(self):
        detail, statuses = self._run_with_sca_on_page(1)
        assert "SCA" in detail["sca_note"]
        assert "transactions_error" not in detail
        assert statuses == ["ok"]


class TestAnUnnamedFailureStillLeavesASyncLogRow(unittest.TestCase):
    """MonzoAuthError is a sibling of the two types the loop names, not a parent.

    It escaped run_sync leaving sync_log - the only record a run leaves -
    empty, so nothing showed that syncing had stopped, and the throttle did
    not count the attempt.
    """

    def _statuses_after(self, exc):
        tmp = tempfile.TemporaryDirectory()
        db_path = pathlib.Path(tmp.name) / "monzo.db"
        db_conn = sqlite3.connect(db_path)
        db_conn.row_factory = sqlite3.Row
        db_conn.executescript(SCHEMA)

        def fake_get(path):
            if path == "/accounts":
                return {"accounts": [{"id": "acc_1", "type": "uk_retail", "closed": False}]}
            raise exc

        with (
            patch.object(transaction_tools, "get_db", return_value=db_conn),
            patch.object(transaction_tools.api, "get", side_effect=fake_get),
        ):
            with self.assertRaises(type(exc)):
                transaction_tools.run_sync()

        reopened = sqlite3.connect(db_path)
        rows = reopened.execute("SELECT status, notes FROM sync_log").fetchall()
        reopened.close()
        tmp.cleanup()
        return rows

    def test_an_auth_failure_is_recorded_and_still_raised(self):
        rows = self._statuses_after(MonzoAuthError("Could not obtain an access token."))
        assert rows and rows[0][0] == "error"
        assert "MonzoAuthError" in rows[0][1]

    def test_the_recorded_note_carries_no_response_content(self):
        rows = self._statuses_after(MonzoAuthError("/etc/secret/path"))
        assert "/etc/secret/path" not in rows[0][1]


if __name__ == "__main__":
    unittest.main()

"""An invalid filter argument must be refused, never answered with an empty result.

These drive the registered tools through ``mcp.call_tool``, which is the layer
the model actually reaches and the only one that applies the argument schema.
Calling the Python function directly skips it.

Two failures are guarded, and the second is the worse one. A value that would
not parse became a filter matching nothing, so a tool answered ``grand_total:
0`` for a month it could not read. A value that parsed but was not normalised
sorted into the wrong range: ``month="2026-1"`` reported October, November and
December spending as January's, and ``since="2026-1-1"`` returned October
onwards. ``strptime`` is lenient about digit width in every field, so that
second failure is a property of the parser rather than of one argument.
"""

import asyncio
import json
import pkgutil
import sqlite3
import unittest
from datetime import datetime
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

from monzo_mcp import helpers, tools
from monzo_mcp.db import SCHEMA
from monzo_mcp.errors import InvalidDateError
from monzo_mcp.mcp_instance import mcp
from monzo_mcp.tools import account_tools, analysis_tools, transaction_tools

# Walked, not listed. A hand-written import list here is a second copy of the
# one in cli.py, and a tool added in a new module would be registered in
# production and invisible to every test below - which is the same "each new
# site has to remember" shape this whole file exists to remove.
TOOL_MODULES = sorted(name for _, name, _ in pkgutil.iter_modules(tools.__path__))
for _name in TOOL_MODULES:
    import_module(f"monzo_mcp.tools.{_name}")

# Two things this fixture has to carry, and both were missing before.
# Months in the back half of the year, because an unpadded "2026-1" matches
# "2026-10" as a LIKE prefix and a fixture of single-digit months cannot show
# it. And MILLISECONDS on `created`, which is what the Monzo API actually
# stores - without them no test here can observe the ordering boundary the
# parsers exist to protect.
FIXTURE_ROWS = [
    ("t1", "2026-01-03T09:00:00.123Z", -1000, "Jan Shop"),
    ("t2", "2026-02-03T09:00:00.001Z", -5000, "Feb Shop"),
    ("t3", "2026-10-03T09:00:00.999Z", -7000, "Oct Shop"),
    ("t4", "2026-11-03T09:00:00.500Z", -9000, "Nov Shop"),
]

# Realistic stored values, including the awkward fractional widths, for the
# ordering property below.
CREATED_CORPUS = [
    # Every fractional width the real cache holds, including none: the API
    # emits 0, 1, 2 and 3 digits, and a corpus of one width cannot show that
    # the comparison is decided before the fraction is reached.
    "2026-01-01T00:00:00Z",
    "2026-01-01T00:00:00.000Z",
    "2026-01-01T00:00:00.001Z",
    "2026-01-09T23:59:59.99Z",
    "2026-01-09T23:59:59.999Z",
    "2026-01-10T00:00:00.123Z",
    "2026-02-03T09:00:00.1Z",
    "2026-10-03T09:00:00.999Z",
    "2026-12-31T23:59:59.500Z",
]

# Every value that must be refused, per argument. This list is a floor, not
# the guarantee: it only covers forms someone thought of, which is why
# TestAnAcceptedBoundOrdersCorrectly asserts the ordering property instead.
BAD_VALUES = {
    "account_type": ["bogus"],
    "month": ["not-a-month", "2026-13", "26-01"],
    "since": [
        "not-a-date",
        "20260101",
        "2026-W01-1",
        "2026-01-01 14:30",
        # A time-of-day bound cannot be ordered as text against a stored value
        # whose fractional part varies in width, so it is refused rather than
        # accepted and compared wrongly.
        "2026-01-01T14:30:00Z",
    ],
    "before": ["not-a-date", "20260101", "2026-01-01 14:30", "2026-01-01T14:30:00Z"],
}

# `monzo_sync` sends `since` to the Monzo API rather than comparing it against
# a stored string, and `_coerce_since` normalises it to RFC3339 on the way. So
# the forms that sort wrongly are correct there, and only an unparseable value
# is wrong. Keyed by (tool, argument) because the constraint follows where the
# value is going, not what it is called.
ONLY_UNPARSEABLE = {("monzo_sync", "since")}

# Filled in for tools that require them, so the call reaches argument handling
# rather than failing on a missing required field.
REQUIRED = {"query": "anything"}


class _Exists:
    def exists(self):
        return True


_ACCOUNT = {"id": "acc_p", "type": "uk_retail", "closed": False}


def _fake_api_get(path):
    """Enough of the Monzo API for run_sync to complete without a network."""
    if path.startswith("/accounts"):
        return {"accounts": [_ACCOUNT]}
    if path.startswith("/balance"):
        return {"balance": 1000, "currency": "GBP"}
    if path.startswith("/pots"):
        return {"pots": []}
    return {"transactions": []}


def _db():
    # The tool bodies run in a worker thread via anyio, and this connection is
    # made on the test's thread.
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for txn_id, created, amount, merchant in FIXTURE_ROWS:
        conn.execute(
            "INSERT INTO monzo_transactions (id, account_id, account_type, created,"
            " amount, currency, description, merchant_name, category, notes, settled)"
            " VALUES (?, 'acc_p', 'personal', ?, ?, 'GBP', ?, ?, 'groceries', '', ?)",
            (txn_id, created, amount, merchant.upper(), merchant, created[:10]),
        )
    conn.commit()
    return conn


_TOOLS = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}


def call(name, args):
    """Call a registered tool, returning (raised_message, result_text)."""
    conn = _db()
    try:
        with (
            patch.object(helpers, "MONZO_CLIENT_PATH", _Exists()),
            patch.object(helpers, "MONZO_TOKENS_PATH", _Exists()),
            patch.object(analysis_tools, "get_db", lambda: conn),
            patch.object(transaction_tools, "get_db", lambda: conn),
            patch.object(analysis_tools, "auto_sync_if_stale", lambda: None),
            patch.object(transaction_tools, "auto_sync_if_stale", lambda: None),
            # `api`, not `run_sync`: patching run_sync would step over the very
            # argument handling monzo_sync is here to be tested on. Both tool
            # modules import the same module object, so this reaches
            # account_tools too, which is what keeps it off the network.
            patch.object(transaction_tools.api, "get", _fake_api_get),
            patch.object(account_tools, "_get_accounts", lambda: [_ACCOUNT]),
        ):
            try:
                result = asyncio.run(mcp.call_tool(name, args))
            except Exception as e:  # noqa: BLE001 - the message is what is under test
                return str(e), None
    finally:
        conn.close()
    text = "".join(c.text for c in result.content if getattr(c, "text", None))
    return None, text


def refusal(name, args, argument, value):
    """The refusal of *this* argument, or None if the tool answered regardless.

    A message is only counted when it names the argument or the value. The
    tools also answer `{"error": "No transaction data available..."}` when the
    cache is empty, and treating any error key as a refusal launders that into
    a pass - measured, it kept the structural test green with every call site
    deleted.
    """
    raised, text = call(name, args)
    said = raised
    if said is None:
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return None
        said = parsed.get("error") if isinstance(parsed, dict) else None
    if not said:
        return None
    return said if (argument in said or str(value) in said) else None


def bad_values_for(tool_name, arg):
    """The values this tool must refuse for this argument."""
    values = BAD_VALUES[arg]
    if (tool_name, arg) in ONLY_UNPARSEABLE:
        return values[:1]
    return values


def constrained_arguments():
    """(tool, argument, value) for every registered tool and every bad value."""
    return [
        (name, arg, value)
        for name, tool in sorted(_TOOLS.items())
        for arg in sorted(tool.input_schema.get("properties", {}))
        if arg in BAD_VALUES
        for value in bad_values_for(name, arg)
    ]


def arguments_for(tool_name, arg, value):
    """The bad argument, plus whatever else that tool requires.

    Raises rather than omitting an unknown required field: pydantic answers a
    missing one with a message quoting the whole submitted dict, which reads
    as a refusal of the argument under test. Measured - a new tool with a
    required argument otherwise passed both structural tests unvalidated.
    """
    args = {arg: value}
    for name in _TOOLS[tool_name].input_schema.get("required", []):
        if name == arg:
            continue
        if name not in REQUIRED:
            raise AssertionError(
                f"{tool_name} requires {name!r} and this test has no value for it - "
                f"add one to REQUIRED, or the tool is left untested"
            )
        args[name] = REQUIRED[name]
    return args


class TestAnInvalidArgumentIsRefused(unittest.TestCase):
    """Every constrained argument, on every tool that takes one.

    Driven off the registry rather than a hand-written list, so a tool added
    later with one of these arguments is covered without touching this file -
    which is the point: the same gap was open in four tools at once because
    each new tool had to remember a call site.
    """

    def test_every_tool_refuses_every_bad_argument(self):
        pairs = constrained_arguments()
        self.assertGreater(len(pairs), 0, "no constrained arguments found - registry empty?")
        for tool_name, arg, value in pairs:
            with self.subTest(tool=tool_name, argument=arg, value=value):
                said = refusal(tool_name, arguments_for(tool_name, arg, value), arg, value)
                self.assertIsNotNone(
                    said,
                    f"{tool_name} accepted {arg}={value!r} and answered as though it were valid",
                )

    def test_a_refusal_names_the_offending_value_or_the_expected_form(self):
        """A refusal the model cannot act on is barely better than a silent zero."""
        for tool_name, arg, value in constrained_arguments():
            with self.subTest(tool=tool_name, argument=arg, value=value):
                said = refusal(tool_name, arguments_for(tool_name, arg, value), arg, value) or ""
                self.assertTrue(
                    value in said or "personal" in said or "YYYY" in said or "2026-01-01" in said,
                    f"{tool_name} refused {arg} without saying what was wrong: {said!r}",
                )


class TestTheSchemaAdvertisesTheAccountTypes(unittest.TestCase):
    """Refusing is not enough on its own; the model has to be told the values.

    `monzo_get_balance` refuses a bogus type either way, because resolving it
    finds no account and raises - so the refusal tests above pass even with
    the constraint removed, and only this one fails. Measured by mutation.
    """

    def test_every_tool_taking_account_type_declares_the_two_values(self):
        for name, tool in sorted(_TOOLS.items()):
            schema = tool.input_schema.get("properties", {}).get("account_type")
            if schema is None:
                continue
            with self.subTest(tool=name):
                # Optional arguments wrap the enum in anyOf alongside a null.
                branches = schema.get("anyOf", [schema])
                enums = [b["enum"] for b in branches if "enum" in b]
                self.assertEqual(
                    enums,
                    [["personal", "joint"]],
                    f"{name} does not constrain account_type in its schema",
                )


class TestAMonthIsNormalisedNotJustChecked(unittest.TestCase):
    """The failure that is worse than the silent zero.

    `%m` accepts one digit, so `2026-1` parses. Left unpadded it becomes the
    LIKE prefix `2026-1%`, which matches October, November and December - so
    the tool answers a January question with the back half of the year, and
    the total looks entirely plausible.
    """

    def test_an_unpadded_month_reads_only_that_month(self):
        raised, text = call("monzo_spending", {"month": "2026-1"})
        self.assertIsNone(raised, raised)
        result = json.loads(text)
        self.assertEqual(result["month"], "2026-01")
        self.assertEqual(result["grand_total"], 10.0)
        self.assertEqual([m["merchant"] for m in result["top_merchants"]], ["Jan Shop"])

    def test_an_unpadded_month_with_data_is_not_reported_as_empty(self):
        raised, text = call("monzo_spending", {"month": "2026-2"})
        self.assertIsNone(raised, raised)
        result = json.loads(text)
        self.assertEqual(result["month"], "2026-02")
        self.assertEqual(result["grand_total"], 50.0)


class TestParsers(unittest.TestCase):
    """Directly, because every other assertion reaches these through a tool.

    The accept side is what has no other test surface, and it is where both
    live defects were: a month that parsed but was not padded, and date forms
    that parsed but do not sort against `created`.
    """

    def test_a_month_is_zero_padded(self):
        self.assertEqual(helpers.parse_month("2026-1"), "2026-01")
        self.assertEqual(helpers.parse_month("2026-01"), "2026-01")

    def test_surrounding_whitespace_is_accepted(self):
        # Deliberate, and otherwise unpinned: nothing sends a padded value
        # today, so a tidy-up could drop the strip and reintroduce a refusal.
        self.assertEqual(helpers.parse_month(" 2026-02 "), "2026-02")
        self.assertEqual(helpers.parse_day(" 2026-01-01 ", "since"), "2026-01-01")

    def test_a_month_outside_the_calendar_is_refused(self):
        with self.assertRaises(InvalidDateError):
            helpers.parse_month("2026-13")

    def test_a_day_is_zero_padded_in_every_field(self):
        # The same leniency that produced the month defect: strptime accepts
        # one digit for %m and %d too, and '2026-1-1' sorts above '2026-01'
        # and below '2026-10', so it selects a plausible wrong window.
        self.assertEqual(helpers.parse_day("2026-1-1", "since"), "2026-01-01")
        self.assertEqual(helpers.parse_day("2026-01-01", "since"), "2026-01-01")

    def test_a_year_is_padded_without_relying_on_strftime(self):
        # strftime("%Y") below year 1000 is documented as platform-dependent.
        self.assertEqual(helpers.parse_month("0099-3"), "0099-03")
        self.assertEqual(helpers.parse_day("0099-3-4", "since"), "0099-03-04")

    def test_a_time_of_day_bound_is_refused(self):
        # Not an oversight: it cannot be ordered as text against a stored
        # value whose fractional part varies in width. The tools document a
        # date, and the property test below is what holds this together.
        for bad in (
            "20260101",
            "2026-W01-1",
            "2026-01-01 14:30",
            "2026-01-01T14:30:00Z",
            "2026-01-01T14:30:00.123Z",
            "2026-01-01T14:30:00+01:00",
        ):
            with self.assertRaises(InvalidDateError, msg=bad):
                helpers.parse_day(bad, "since")


class TestAnAcceptedBoundOrdersCorrectly(unittest.TestCase):
    """The property the parsers exist for, rather than an enumerated list.

    A refuse-list only guards the forms someone thought of; this fails for any
    accepted form that cannot be compared as text against a stored `created`,
    including one added later.
    """

    @staticmethod
    def _moment(created):
        # Every fractional width the API emits, including none. A helper that
        # assumed one width raised on exactly the values the corpus most needs
        # to hold, so the gap defended itself: widening the corpus broke the
        # helper rather than failing an assertion.
        return datetime.fromisoformat(created.replace("Z", "+00:00"))

    def test_text_comparison_agrees_with_chronology_for_every_accepted_bound(self):
        bounds = ["2026-1-1", "2026-01-01", "2026-01-10", "2026-02-03", "2026-12-31", "0099-3-4"]
        for raw in bounds:
            parsed = helpers.parse_day(raw, "since")
            start_of_day = self._moment(f"{parsed}T00:00:00.000Z")
            for created in CREATED_CORPUS:
                with self.subTest(bound=raw, created=created):
                    self.assertEqual(
                        created >= parsed,
                        self._moment(created) >= start_of_day,
                        f"text order disagrees with chronology: {created!r} vs {parsed!r}",
                    )


class TestTheTestsSeeWhatProductionRegisters(unittest.TestCase):
    """Otherwise a tool in a new module ships unvalidated and fully green.

    This file walks `monzo_mcp.tools`, so it sees every module that exists.
    Production registers by a hand-written import list in cli.py, so the two
    can diverge in the other direction - a module that exists and is never
    registered. Measured: adding a tool module and wiring it into cli.py only
    left the whole suite passing with an unvalidated date argument.
    """

    def test_cli_imports_every_tool_module(self):
        source = (Path(helpers.__file__).parent / "cli.py").read_text()
        missing = [name for name in TOOL_MODULES if name not in source]
        self.assertEqual(missing, [], f"cli.py does not register: {missing}")

    def test_the_walk_found_the_modules_this_file_names(self):
        # Guards the walk itself: an empty TOOL_MODULES would make the test
        # above vacuously pass and silently uncover every tool.
        for expected in ("account_tools", "analysis_tools", "transaction_tools"):
            self.assertIn(expected, TOOL_MODULES)

    def test_the_argument_name_is_in_the_message(self):
        with self.assertRaises(InvalidDateError) as caught:
            helpers.parse_day("nope", "before")
        self.assertIn("before", str(caught.exception))


class TestValidArgumentsAreUnaffected(unittest.TestCase):
    """The refusals must not be so eager that real calls stop working."""

    def test_a_valid_month_still_answers(self):
        raised, text = call("monzo_spending", {"month": "2026-02"})
        self.assertIsNone(raised, raised)
        self.assertEqual(json.loads(text)["month"], "2026-02")

    def test_a_valid_account_type_still_answers(self):
        raised, text = call("monzo_list_transactions", {"account_type": "personal"})
        self.assertIsNone(raised, raised)
        self.assertEqual(json.loads(text)["account_type"], "personal")

    def test_a_valid_since_and_before_select_the_expected_rows(self):
        raised, text = call(
            "monzo_list_transactions", {"since": "2026-01-01", "before": "2026-03-01"}
        )
        self.assertIsNone(raised, raised)
        ids = sorted(t["id"] for t in json.loads(text)["transactions"])
        self.assertEqual(ids, ["t1", "t2"])

    def test_an_unpadded_date_selects_the_window_it_names(self):
        # Before normalisation this returned only the October row, because
        # '2026-1-1' sorts above every zero-padded month 01-09.
        raised, text = call("monzo_list_transactions", {"since": "2026-1-1"})
        self.assertIsNone(raised, raised)
        ids = sorted(t["id"] for t in json.loads(text)["transactions"])
        self.assertEqual(ids, ["t1", "t2", "t3", "t4"])

    def test_a_date_bound_includes_rows_recorded_during_that_day(self):
        # The millisecond boundary: t2 is at 09:00:00.001Z on 3 February.
        raised, text = call("monzo_list_transactions", {"since": "2026-02-03"})
        self.assertIsNone(raised, raised)
        ids = sorted(t["id"] for t in json.loads(text)["transactions"])
        self.assertEqual(ids, ["t2", "t3", "t4"])

    def test_an_omitted_argument_is_still_optional(self):
        raised, text = call("monzo_list_transactions", {})
        self.assertIsNone(raised, raised)
        self.assertIsNone(json.loads(text)["account_type"])

    def test_monzo_sync_still_takes_the_forms_it_normalises(self):
        """The exemption above is a claim about behaviour, so it is asserted.

        `_coerce_since` rewrites these to RFC3339 before they reach the API,
        so refusing them here - by "unifying" the two paths - would break a
        backfill that works today.
        """
        for value in ("20260101", "2026-01-01 14:30"):
            with self.subTest(since=value):
                raised, text = call("monzo_sync", {"since": value})
                self.assertIsNone(raised, raised)
                self.assertNotIn("Invalid", text)

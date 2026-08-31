"""Shared utilities for the Monzo MCP server."""

import functools
import json
import logging
from datetime import datetime
from typing import Any, Literal

from mcp.server.mcpserver.exceptions import ToolError

from .config import MONZO_CLIENT_PATH, MONZO_TOKENS_PATH
from .errors import InvalidDateError, MonzoError

logger = logging.getLogger(__name__)


def format_response(result: Any) -> str:
    """JSON-serialize a result for MCP transport."""
    if isinstance(result, (dict, list)):
        return json.dumps(result, indent=2, default=str)
    elif result is None:
        return json.dumps(None)
    else:
        return json.dumps({"result": str(result)})


# The constraint lives in the annotation so it reaches the tool schema, which
# is what the model reads and what the server validates a call against.
AccountType = Literal["personal", "joint"]


def pence_to_pounds(pence: int) -> float:
    """Convert pence to pounds for display."""
    return pence / 100.0


# Both parsers below guard a string comparison against `created`, which is
# RFC3339 Zulu with milliseconds. Two consequences, and neither is obvious.
#
# They normalise rather than only validating, with explicit field widths:
# `strptime` accepts one digit for %m, %d and %H, and an unpadded value used
# as a filter sorts above `01`-`09` and below `10`-`12`, so it selects a
# plausible wrong range rather than nothing. `strftime("%Y")` is documented as
# platform-dependent below year 1000, so the widths are spelled out here.
#
# `parse_day` takes a date and nothing else, which is all the tools document.
# A time-of-day bound cannot be compared as text against a stored value whose
# fractional part varies in width: `.1Z` sorts above `.123Z` because `Z` > `2`,
# and a bound with no fraction sorts above every value that has one.
def _parse(value, fmt, message):
    """`strptime`, with the failures a caller of these parsers can produce."""
    try:
        return datetime.strptime(value.strip() if isinstance(value, str) else value, fmt)
    except (AttributeError, TypeError, ValueError) as e:
        raise InvalidDateError(message) from e


def parse_month(value: str) -> str:
    """Return a zero-padded YYYY-MM month, or raise `InvalidDateError`."""
    parsed = _parse(value, "%Y-%m", f"Invalid month {value!r}. Use YYYY-MM, e.g. '2026-08'.")
    return f"{parsed.year:04d}-{parsed.month:02d}"


def parse_day(value: str, argument: str) -> str:
    """Return a zero-padded YYYY-MM-DD date, or raise `InvalidDateError`."""
    parsed = _parse(
        value, "%Y-%m-%d", f"Invalid {argument} {value!r}. Use a date, e.g. '2026-01-01'."
    )
    return f"{parsed.year:04d}-{parsed.month:02d}-{parsed.day:02d}"


def require_auth(func):
    """Gate a tool on credentials, and let its own errors keep their message.

    `mcp` 2.1 keeps a `ToolError`'s text and replaces every other exception's
    with "Error executing tool <name>", so a MonzoError is converted and
    nothing else is. Why only those: errors.py.
    """

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        if not MONZO_CLIENT_PATH.exists() or not MONZO_TOKENS_PATH.exists():
            return json.dumps(
                {
                    "error": "Monzo not configured. Run: monzo-mcp auth",
                }
            )
        try:
            return await func(*args, **kwargs)
        except MonzoError as e:
            raise ToolError(str(e)) from e

    return wrapper

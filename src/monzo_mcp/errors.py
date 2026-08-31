"""The exception base this package raises deliberately.

Imports nothing from the package, so `api`, `auth` and the tools can all raise
from it without a cycle.
"""


class MonzoError(Exception):
    """An error this package raises on purpose, with text written for the model.

    `require_auth` converts these into `ToolError`, whose message `mcp` keeps
    on the wire; every other exception reaches the client as `Error executing
    tool <name>`. Anything not descended from this is treated as unplanned,
    and its text is what must not travel: a filesystem error on the token file
    or the cache names an absolute path, which is measured rather than assumed
    and is what the masking test raises.

    Membership is not a promise about the message. What each raise site may
    say is governed by the Data Safety Rules in AGENTS.md. `TokenRefused`
    carries a credential file's basename, and `api.get` replaces both it and
    `RefreshNetworkError` with its own fixed text rather than passing either
    on, which `test_api.py` pins.
    """


class AccountNotFound(MonzoError, ValueError):
    """No open account of the type a tool was asked for.

    Keeps `ValueError`, which is what it raised before, so a caller catching
    that still does.
    """

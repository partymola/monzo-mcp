#!/usr/bin/env python3
"""Monzo MCP server entry point.

Usage:
    monzo-mcp          Start the MCP server (stdio transport)
    monzo-mcp auth     Interactive OAuth setup
"""

import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)

from monzo_mcp.mcp_instance import mcp  # noqa: E402

# Import tool modules - decorators register them with the mcp instance
from monzo_mcp.tools import account_tools  # noqa: E402, F401
from monzo_mcp.tools import transaction_tools  # noqa: E402, F401
from monzo_mcp.tools import analysis_tools  # noqa: E402, F401


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        from monzo_mcp.auth import setup_auth
        setup_auth()
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

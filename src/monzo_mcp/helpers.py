"""Shared utilities for the Monzo MCP server."""

import functools
import json
import logging
from typing import Any

from .config import MONZO_CLIENT_PATH, MONZO_TOKENS_PATH

logger = logging.getLogger(__name__)


def format_response(result: Any) -> str:
    """JSON-serialize a result for MCP transport."""
    if isinstance(result, (dict, list)):
        return json.dumps(result, indent=2, default=str)
    elif result is None:
        return json.dumps(None)
    else:
        return json.dumps({"result": str(result)})


VALID_ACCOUNT_TYPES = ("personal", "joint")


def pence_to_pounds(pence: int) -> float:
    """Convert pence to pounds for display."""
    return pence / 100.0


def validate_account_type(account_type: str | None) -> str | None:
    """Validate account_type parameter. Returns None if valid, error string if not."""
    if account_type is not None and account_type not in VALID_ACCOUNT_TYPES:
        return f"Invalid account_type '{account_type}'. Must be 'personal' or 'joint'."
    return None


def require_auth(func):
    """Decorator that checks credentials exist before calling a tool."""
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        if not MONZO_CLIENT_PATH.exists() or not MONZO_TOKENS_PATH.exists():
            return json.dumps({
                "error": "Monzo not configured. Run: monzo-mcp auth",
            })
        return await func(*args, **kwargs)
    return wrapper

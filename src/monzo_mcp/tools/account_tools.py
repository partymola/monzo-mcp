"""Account, balance, and pot tools."""

import time

import anyio

from ..mcp_instance import mcp
from ..helpers import format_response, require_auth, pence_to_pounds, validate_account_type
from .. import api
from ..db import get_db, save_balance

# Cache account list to avoid repeated /accounts API calls
_accounts_cache = None
_accounts_cache_time = 0
_ACCOUNTS_TTL = 300  # 5 minutes


def _get_accounts() -> list[dict]:
    """Fetch accounts, using a short-lived cache."""
    global _accounts_cache, _accounts_cache_time
    now = time.monotonic()
    if _accounts_cache is None or (now - _accounts_cache_time) > _ACCOUNTS_TTL:
        _accounts_cache = api.get("/accounts").get("accounts", [])
        _accounts_cache_time = now
    return _accounts_cache


def _resolve_account_id(account_type: str) -> tuple[str, str]:
    """Find account ID for the given type. Returns (account_id, account_type)."""
    for acct in _get_accounts():
        if acct.get("closed"):
            continue
        atype = "joint" if acct.get("type") == "uk_retail_joint" else "personal"
        if atype == account_type:
            return acct["id"], atype
    raise ValueError(f"No open {account_type} account found")


@mcp.tool()
@require_auth
async def monzo_list_accounts() -> str:
    """List all Monzo accounts with their types and IDs.

    Returns account details including whether each is personal or joint,
    and whether it is open or closed.
    """
    data = await anyio.to_thread.run_sync(_get_accounts)
    accounts = []
    for acct in data:
        atype = "joint" if acct.get("type") == "uk_retail_joint" else "personal"
        accounts.append({
            "id": acct["id"],
            "type": atype,
            "closed": acct.get("closed", False),
            "created": acct.get("created"),
        })
    return format_response(accounts)


@mcp.tool()
@require_auth
async def monzo_get_balance(account_type: str = "personal") -> str:
    """Get current balance for a Monzo account.

    Args:
        account_type: "personal" or "joint" (default: "personal")

    Returns balance, spend today, and currency. Also records a balance snapshot.
    """
    err = validate_account_type(account_type)
    if err:
        return format_response({"error": err})

    def _fetch():
        acct_id, atype = _resolve_account_id(account_type)
        bal = api.get(f"/balance?account_id={acct_id}")
        db = get_db()
        try:
            save_balance(db, atype, f"Monzo {atype.title()}", bal["balance"], bal.get("currency", "GBP"))
        finally:
            db.close()
        return {
            "account_type": atype,
            "balance": pence_to_pounds(bal["balance"]),
            "total_balance": pence_to_pounds(bal.get("total_balance", bal["balance"])),
            "spend_today": pence_to_pounds(bal.get("spend_today", 0)),
            "currency": bal.get("currency", "GBP"),
        }
    result = await anyio.to_thread.run_sync(_fetch)
    return format_response(result)


@mcp.tool()
@require_auth
async def monzo_list_pots(account_type: str = "personal") -> str:
    """List all pots (savings buckets) for a Monzo account.

    Args:
        account_type: "personal" or "joint" (default: "personal")

    Returns pot names and balances. Also records balance snapshots.
    """
    err = validate_account_type(account_type)
    if err:
        return format_response({"error": err})

    def _fetch():
        acct_id, atype = _resolve_account_id(account_type)
        data = api.get(f"/pots?current_account_id={acct_id}")
        db = get_db()
        try:
            pots = []
            for pot in data.get("pots", []):
                if pot.get("deleted"):
                    continue
                save_balance(db, atype, pot["name"], pot["balance"], pot.get("currency", "GBP"))
                entry = {
                    "id": pot["id"],
                    "name": pot["name"],
                    "balance": pence_to_pounds(pot["balance"]),
                    "currency": pot.get("currency", "GBP"),
                }
                if pot.get("goal_amount"):
                    entry["goal"] = pence_to_pounds(pot["goal_amount"])
                pots.append(entry)
        finally:
            db.close()
        return pots
    result = await anyio.to_thread.run_sync(_fetch)
    return format_response(result)

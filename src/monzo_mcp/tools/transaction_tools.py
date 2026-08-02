"""Transaction sync, listing, and search tools."""

from datetime import date, datetime, timedelta, timezone

import anyio

from .. import api
from ..api import MonzoAPIError, MonzoSCAError
from ..db import get_db, get_last_sync_time, log_sync, save_balance
from ..helpers import format_response, pence_to_pounds, require_auth
from ..mcp_instance import mcp


def _format_transaction(row) -> dict:
    """Format a DB row as a transaction dict with amounts in pounds."""
    result = {
        "id": row["id"],
        "account_type": row["account_type"],
        "date": row["created"][:10],
        "created": row["created"],
        "amount": pence_to_pounds(row["amount"]),
        "currency": row["currency"],
        "description": row["description"],
        "merchant": row["merchant_name"],
        "category": row["category"],
        "notes": row["notes"] or None,
        "settled": row["settled"] or None,
    }
    if row["counterparty_name"]:
        result["counterparty"] = {
            key: row[f"counterparty_{key}"]
            for key in ("name", "sort_code", "account_number")
            if row[f"counterparty_{key}"]
        }
    return result


def _coerce_since(value: str) -> str:
    """Coerce an ISO date or datetime to RFC3339 (``YYYY-MM-DDTHH:MM:SSZ``).

    Accepts a bare date (``2026-01-01``) or a full ISO datetime
    (``2026-01-01T14:30:00Z`` or ``...+00:00``). Naive values are treated as UTC.
    Raises ``ValueError`` if the value cannot be parsed.
    """
    dt = datetime.fromisoformat(value.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_sync(account_type: str | None = None, since: str | None = None) -> dict:
    """Sync transactions, balances, and pots from the Monzo API into the local cache.

    Fetches up to 11 months of history (within SCA window) or falls back to
    the last-synced timestamp / 90 days. Handles pagination and auth-hold
    deduplication automatically.

    Args:
        account_type: "personal", "joint", or None to sync all accounts
        since: Optional ISO date ("2026-01-01") or datetime ("2026-01-01T14:30:00Z")
            to start the backfill from, overriding last-sync resumption. Reaching
            beyond ~90 days only works inside the post-auth SCA window; outside it
            the API's 90-day fallback still applies (the value is passed straight to
            the Monzo API, not gated client-side).
    """
    since_override = None
    if since is not None:
        try:
            since_override = _coerce_since(since)
        except ValueError:
            return {
                "error": (
                    f"Invalid 'since' value: {since!r}. "
                    "Use an ISO date (2026-01-01) or datetime (2026-01-01T14:30:00Z)."
                )
            }

    accounts_data = api.get("/accounts")
    accounts = accounts_data.get("accounts", [])
    if not accounts:
        return {"error": "No Monzo accounts found"}

    db = get_db()
    try:
        total_added = 0
        accounts_synced = 0
        sync_details = []

        for acct in accounts:
            if acct.get("closed"):
                continue
            acct_id = acct["id"]
            atype = "joint" if acct.get("type") == "uk_retail_joint" else "personal"

            if account_type and atype != account_type:
                continue

            detail = {"account_type": atype}

            try:
                bal = api.get(f"/balance?account_id={acct_id}")
                save_balance(
                    db,
                    atype,
                    f"Monzo {atype.title()}",
                    bal["balance"],
                    bal.get("currency", "GBP"),
                )
                detail["balance"] = pence_to_pounds(bal["balance"])
            except Exception as e:
                detail["balance_error"] = str(e)

            try:
                pots_data = api.get(f"/pots?current_account_id={acct_id}")
                pot_count = 0
                for pot in pots_data.get("pots", []):
                    if not pot.get("deleted"):
                        save_balance(
                            db,
                            atype,
                            pot["name"],
                            pot["balance"],
                            pot.get("currency", "GBP"),
                        )
                        pot_count += 1
                detail["pots_synced"] = pot_count
            except Exception as e:
                detail["pots_error"] = str(e)

            since = since_override or (datetime.now(timezone.utc) - timedelta(days=335)).strftime(
                "%Y-%m-%dT00:00:00Z"
            )
            added = 0
            page = 0

            while True:
                try:
                    txns = api.get(
                        f"/transactions?account_id={acct_id}&since={since}&limit=100&expand[]=merchant"
                    ).get("transactions", [])
                except (MonzoSCAError, MonzoAPIError):
                    if page == 0:
                        last_sync = db.execute(
                            "SELECT MAX(created) FROM monzo_transactions WHERE account_type = ?",
                            (atype,),
                        ).fetchone()[0]
                        fallback = last_sync or (
                            datetime.now(timezone.utc) - timedelta(days=90)
                        ).strftime("%Y-%m-%dT00:00:00Z")
                        try:
                            txns = api.get(
                                f"/transactions?account_id={acct_id}&since={fallback}&limit=100&expand[]=merchant"
                            ).get("transactions", [])
                            if not last_sync:
                                detail["sca_note"] = "SCA window expired, fetched last 90 days only"
                        except (MonzoSCAError, MonzoAPIError):
                            detail["sca_note"] = "SCA required - approve in Monzo app"
                            break
                    else:
                        break

                if not txns:
                    break

                for tx in txns:
                    merchant_name = None
                    if isinstance(tx.get("merchant"), dict):
                        merchant_name = tx["merchant"].get("name")

                    # Bank transfers (faster payments, p2p, bacs) carry the payee
                    # in a counterparty object instead of a merchant
                    counterparty = tx.get("counterparty")
                    if not isinstance(counterparty, dict):
                        counterparty = {}

                    db.execute(
                        """INSERT OR REPLACE INTO monzo_transactions
                           (id, account_id, account_type, created, amount, currency,
                            description, merchant_name, category, notes, settled,
                            counterparty_name, counterparty_sort_code,
                            counterparty_account_number, counterparty_user_id)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            tx["id"],
                            acct_id,
                            atype,
                            tx["created"],
                            tx["amount"],
                            tx["currency"],
                            tx.get("description", ""),
                            merchant_name,
                            tx.get("category", ""),
                            tx.get("notes", ""),
                            tx.get("settled", ""),
                            counterparty.get("name"),
                            counterparty.get("sort_code"),
                            counterparty.get("account_number"),
                            counterparty.get("user_id"),
                        ),
                    )
                    added += 1

                db.commit()
                page += 1
                since = txns[-1]["id"]

                if len(txns) < 100:
                    break

            total_added += added
            total_in_db = db.execute(
                "SELECT COUNT(*) FROM monzo_transactions WHERE account_type = ?",
                (atype,),
            ).fetchone()[0]
            detail["transactions_upserted"] = added
            detail["total_in_db"] = total_in_db
            accounts_synced += 1
            sync_details.append(detail)

        # Auth-hold deduplication
        dupes = db.execute("""
            DELETE FROM monzo_transactions WHERE id IN (
                SELECT a.id
                FROM monzo_transactions a
                JOIN monzo_transactions b ON a.amount = b.amount
                    AND a.merchant_name = b.merchant_name
                    AND a.account_type = b.account_type
                    AND a.id != b.id
                    AND ABS(JULIANDAY(a.created) - JULIANDAY(b.created)) < 0.01
                WHERE a.merchant_name IS NOT NULL
                AND (a.settled = '' OR a.settled IS NULL)
            )
        """).rowcount
        if dupes:
            db.commit()

        log_sync(db, "ok", total_added, f"accounts={accounts_synced}, dupes_removed={dupes}")

        return {
            "accounts_synced": accounts_synced,
            "transactions_upserted": total_added,
            "duplicates_removed": dupes,
            "details": sync_details,
        }
    finally:
        db.close()


def auto_sync_if_stale() -> None:
    """Run an incremental sync if the cache has not been synced today.

    Silently swallows all errors.
    """
    try:
        last_sync = get_last_sync_time(get_db())
        today = date.today().isoformat()
        if last_sync is None or last_sync[:10] < today:
            run_sync()
    except Exception:
        pass


@mcp.tool()
@require_auth
async def monzo_sync(account_type: str | None = None, since: str | None = None) -> str:
    """Sync transactions, balances, and pots from the Monzo API into the local cache.

    Fetches up to 11 months of history (within SCA window) or falls back to
    the last-synced timestamp / 90 days. Handles pagination and auth-hold
    deduplication automatically.

    Args:
        account_type: "personal", "joint", or None to sync all accounts
        since: Optional ISO date ("2026-01-01") or datetime ("2026-01-01T14:30:00Z")
            to start the backfill from, overriding last-sync resumption. Reaching
            beyond ~90 days only works inside the post-auth SCA window.
    """
    result = await anyio.to_thread.run_sync(lambda: run_sync(account_type, since))
    return format_response(result)


@mcp.tool()
@require_auth
async def monzo_list_transactions(
    account_type: str | None = None,
    since: str | None = None,
    before: str | None = None,
    category: str | None = None,
    merchant: str | None = None,
    limit: int = 50,
) -> str:
    """List transactions from the local cache.

    Auto-syncs if the cache is stale (last sync before today).

    Queries the synced transaction database, not the live API. Bank transfers
    (faster payments, p2p, bacs) include a `counterparty` object with the payee
    name and, where the scheme provides them, sort code and account number.

    Returns `{"account_type": ..., "transactions": [...]}`, where account_type
    echoes the filter applied and is null when unfiltered.

    Args:
        account_type: "personal" or "joint" (default: all)
        since: Start date in ISO format, e.g. "2026-01-01" (inclusive)
        before: End date in ISO format, e.g. "2026-02-01" (exclusive)
        category: Exact category match, e.g. "groceries", "eating_out", "transport"
        merchant: Merchant name search (case-insensitive, partial match)
        limit: Max results (default 50)
    """

    def _query():
        auto_sync_if_stale()
        db = get_db()
        try:
            count = db.execute("SELECT COUNT(*) FROM monzo_transactions").fetchone()[0]
            if count == 0:
                return format_response(
                    {
                        "error": (
                            "No transaction data available. Check your Monzo auth "
                            "with `monzo-mcp auth`."
                        )
                    }
                )

            conditions = []
            params = []

            if account_type:
                conditions.append("account_type = ?")
                params.append(account_type)
            if since:
                conditions.append("created >= ?")
                params.append(since)
            if before:
                conditions.append("created < ?")
                params.append(before)
            if category:
                conditions.append("category = ?")
                params.append(category)
            if merchant:
                conditions.append("merchant_name LIKE ?")
                params.append(f"%{merchant}%")

            where = " AND ".join(conditions) if conditions else "1=1"
            params.append(limit)

            rows = db.execute(
                f"SELECT * FROM monzo_transactions WHERE {where} ORDER BY created DESC LIMIT ?",
                params,
            ).fetchall()
            return format_response(
                {
                    "account_type": account_type,
                    "transactions": [_format_transaction(r) for r in rows],
                }
            )
        finally:
            db.close()

    return await anyio.to_thread.run_sync(_query)


@mcp.tool()
@require_auth
async def monzo_search_transactions(
    query: str,
    account_type: str | None = None,
    since: str | None = None,
    before: str | None = None,
    limit: int = 30,
) -> str:
    """Search cached transactions by merchant, counterparty (payee), description, or notes.

    Auto-syncs if the cache is stale (last sync before today).

    Case-insensitive partial match across merchant_name, counterparty_name,
    description, and notes fields. Counterparty matching finds bank transfers
    (faster payments, p2p, bacs) by payee name.

    Returns `{"account_type": ..., "transactions": [...]}`, where account_type
    echoes the filter applied and is null when unfiltered.

    Args:
        query: Search term
        account_type: "personal" or "joint" (default: all)
        since: Start date in ISO format (inclusive)
        before: End date in ISO format (exclusive)
        limit: Max results (default 30)
    """

    def _query_db():
        auto_sync_if_stale()
        db = get_db()
        try:
            count = db.execute("SELECT COUNT(*) FROM monzo_transactions").fetchone()[0]
            if count == 0:
                return format_response(
                    {
                        "error": (
                            "No transaction data available. Check your Monzo auth "
                            "with `monzo-mcp auth`."
                        )
                    }
                )

            conditions = [
                "(merchant_name LIKE ? OR counterparty_name LIKE ? "
                "OR description LIKE ? OR notes LIKE ?)"
            ]
            params = [f"%{query}%"] * 4

            if account_type:
                conditions.append("account_type = ?")
                params.append(account_type)
            if since:
                conditions.append("created >= ?")
                params.append(since)
            if before:
                conditions.append("created < ?")
                params.append(before)

            where = " AND ".join(conditions)
            params.append(limit)

            rows = db.execute(
                f"SELECT * FROM monzo_transactions WHERE {where} ORDER BY created DESC LIMIT ?",
                params,
            ).fetchall()
            return format_response(
                {
                    "account_type": account_type,
                    "transactions": [_format_transaction(r) for r in rows],
                }
            )
        finally:
            db.close()

    return await anyio.to_thread.run_sync(_query_db)

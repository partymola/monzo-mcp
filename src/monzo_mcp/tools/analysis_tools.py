"""Spending analysis tools."""

from datetime import date, timedelta

import anyio

from ..mcp_instance import mcp
from ..helpers import format_response, require_auth, pence_to_pounds
from ..db import get_db


@mcp.tool()
@require_auth
async def monzo_spending(
    month: str | None = None,
    category: str | None = None,
    account_type: str | None = None,
    detail: bool = False,
) -> str:
    """Analyse spending from cached Monzo transactions.

    Run monzo_sync first to populate/update the cache.

    Args:
        month: Month in YYYY-MM format (default: current month)
        category: Filter by category, e.g. "groceries", "eating_out", "transport"
        account_type: "personal" or "joint" (default: all)
        detail: If true, return individual transactions instead of category summary
    """
    def _analyse():
        db = get_db()
        try:
            count = db.execute("SELECT COUNT(*) FROM monzo_transactions").fetchone()[0]
            if count == 0:
                return format_response({
                    "error": "No transaction data. Run monzo_sync first to populate the cache."
                })

            target_month = month or date.today().strftime("%Y-%m")

            conditions = ["amount < 0", "created LIKE ?"]
            params = [f"{target_month}%"]

            if category:
                conditions.append("category = ?")
                params.append(category)
            if account_type:
                conditions.append("account_type = ?")
                params.append(account_type)

            where = " AND ".join(conditions)

            if detail:
                rows = db.execute(
                    f"""SELECT created, amount, description, merchant_name, category, account_type
                        FROM monzo_transactions WHERE {where}
                        ORDER BY created ASC, amount ASC""",
                    params,
                ).fetchall()

                if not rows:
                    return format_response({"month": target_month, "transactions": [], "total": 0})

                transactions = []
                total = 0
                for r in rows:
                    amt = pence_to_pounds(r["amount"])
                    total += amt
                    transactions.append({
                        "date": r["created"][:10],
                        "amount": amt,
                        "description": r["description"],
                        "merchant": r["merchant_name"],
                        "category": r["category"],
                        "account_type": r["account_type"],
                    })

                return format_response({
                    "month": target_month,
                    "transactions": transactions,
                    "count": len(transactions),
                    "total": round(total, 2),
                })

            rows = db.execute(
                f"""SELECT category, COUNT(*) as cnt, SUM(amount) as total
                    FROM monzo_transactions WHERE {where}
                    GROUP BY category ORDER BY total ASC""",
                params,
            ).fetchall()

            if not rows:
                return format_response({"month": target_month, "categories": [], "grand_total": 0})

            categories = []
            grand_total = 0
            for row in rows:
                total = abs(pence_to_pounds(row["total"]))
                grand_total += total
                categories.append({
                    "category": row["category"] or "(uncategorised)",
                    "count": row["cnt"],
                    "total": round(total, 2),
                })

            merchants = db.execute(
                f"""SELECT merchant_name, COUNT(*) as cnt, SUM(amount) as total
                    FROM monzo_transactions WHERE {where} AND merchant_name IS NOT NULL
                    GROUP BY merchant_name ORDER BY total ASC LIMIT 15""",
                params,
            ).fetchall()

            top_merchants = []
            for m in merchants:
                top_merchants.append({
                    "merchant": m["merchant_name"],
                    "count": m["cnt"],
                    "total": round(abs(pence_to_pounds(m["total"])), 2),
                })

            result = {
                "month": target_month,
                "categories": categories,
                "grand_total": round(grand_total, 2),
                "top_merchants": top_merchants,
            }

            if not month:
                prev_month = (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
                prev_total_row = db.execute(
                    "SELECT SUM(amount) FROM monzo_transactions WHERE amount < 0 AND created LIKE ?",
                    (f"{prev_month}%",),
                ).fetchone()[0]

                if prev_total_row:
                    prev = abs(pence_to_pounds(prev_total_row))
                    diff = grand_total - prev
                    pct = (diff / prev * 100) if prev else 0
                    result["vs_previous"] = {
                        "previous_month": prev_month,
                        "previous_total": round(prev, 2),
                        "change": round(diff, 2),
                        "change_pct": round(pct, 1),
                    }

            return format_response(result)
        finally:
            db.close()

    return await anyio.to_thread.run_sync(_analyse)

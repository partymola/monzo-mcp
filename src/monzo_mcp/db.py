"""SQLite database for Monzo transaction cache and balance history."""

import sqlite3
from datetime import datetime, timezone

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS monzo_transactions (
    id TEXT PRIMARY KEY,
    account_id TEXT,
    account_type TEXT,
    created TEXT,
    amount INTEGER,
    currency TEXT,
    description TEXT,
    merchant_name TEXT,
    category TEXT,
    notes TEXT,
    settled TEXT,
    counterparty_name TEXT,
    counterparty_sort_code TEXT,
    counterparty_account_number TEXT,
    counterparty_user_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_txn_created ON monzo_transactions(created);
CREATE INDEX IF NOT EXISTS idx_txn_account_type ON monzo_transactions(account_type);
CREATE INDEX IF NOT EXISTS idx_txn_category ON monzo_transactions(category);

CREATE TABLE IF NOT EXISTS balances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_type TEXT,
    name TEXT,
    balance INTEGER,
    currency TEXT,
    captured_at TEXT
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    synced_at TEXT,
    status TEXT,
    records_added INTEGER,
    notes TEXT
);
"""

_schema_initialized = False

# Columns added after the original schema; CREATE TABLE IF NOT EXISTS does not
# extend existing tables, so these are applied via ALTER TABLE on first open.
_TXN_ADDED_COLUMNS = (
    "counterparty_name",
    "counterparty_sort_code",
    "counterparty_account_number",
    "counterparty_user_id",
)


def migrate(db: sqlite3.Connection) -> None:
    """Add any missing columns to a database created with an older schema."""
    existing = {row[1] for row in db.execute("PRAGMA table_info(monzo_transactions)").fetchall()}
    missing = [col for col in _TXN_ADDED_COLUMNS if col not in existing]
    for col in missing:
        try:
            db.execute(f"ALTER TABLE monzo_transactions ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass  # another process added the column concurrently
    if missing:
        db.commit()


def get_db() -> sqlite3.Connection:
    """Open monzo.db, ensure schema exists on first call, return connection."""
    global _schema_initialized
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    if not _schema_initialized:
        db.executescript(SCHEMA)
        migrate(db)
        _schema_initialized = True
    return db


def save_balance(
    db: sqlite3.Connection,
    account_type: str,
    name: str,
    balance_pence: int,
    currency: str = "GBP",
):
    """Record a balance snapshot."""
    db.execute(
        "INSERT INTO balances (account_type, name, balance, currency, captured_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            account_type,
            name,
            balance_pence,
            currency,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    db.commit()


def log_sync(db: sqlite3.Connection, status: str, records_added: int, notes: str = ""):
    """Record a sync event."""
    db.execute(
        "INSERT INTO sync_log (synced_at, status, records_added, notes) VALUES (?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), status, records_added, notes),
    )
    db.commit()


def get_last_sync_time(db: sqlite3.Connection) -> str | None:
    """Return the ISO timestamp of the most recent successful sync, or None.

    How fresh the cache is. The once-a-day throttle uses get_last_sync_attempt.
    """
    row = db.execute("SELECT MAX(synced_at) AS t FROM sync_log WHERE status = 'ok'").fetchone()
    if row and row["t"]:
        return row["t"]
    return None


def get_last_sync_attempt(db: sqlite3.Connection) -> str | None:
    """Return the ISO timestamp of the most recent sync of any status, or None.

    What throttles the automatic sync.
    """
    row = db.execute("SELECT MAX(synced_at) AS t FROM sync_log").fetchone()
    if row and row["t"]:
        return row["t"]
    return None

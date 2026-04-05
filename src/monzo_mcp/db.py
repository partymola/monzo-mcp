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
    settled TEXT
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


def get_db() -> sqlite3.Connection:
    """Open monzo.db, ensure schema exists on first call, return connection."""
    global _schema_initialized
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    if not _schema_initialized:
        db.executescript(SCHEMA)
        _schema_initialized = True
    return db


def save_balance(db: sqlite3.Connection, account_type: str, name: str,
                 balance_pence: int, currency: str = "GBP"):
    """Record a balance snapshot."""
    db.execute(
        "INSERT INTO balances (account_type, name, balance, currency, captured_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (account_type, name, balance_pence, currency,
         datetime.now(timezone.utc).isoformat()),
    )
    db.commit()


def log_sync(db: sqlite3.Connection, status: str, records_added: int,
             notes: str = ""):
    """Record a sync event."""
    db.execute(
        "INSERT INTO sync_log (synced_at, status, records_added, notes) "
        "VALUES (?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), status, records_added, notes),
    )
    db.commit()

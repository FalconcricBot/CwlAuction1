"""
database/db.py
---------------
SQLite connection management and schema definition.

Design notes:
- We use the built-in `sqlite3` module wrapped with `asyncio.to_thread` so the
  rest of the codebase can be fully async without pulling in an extra
  dependency (aiosqlite). A single module-level connection is reused with
  `check_same_thread=False`; all access is serialized through an
  `asyncio.Lock` to keep writes safe.
- WAL journal mode is enabled for better durability/concurrency.
- Foreign keys are enforced (`PRAGMA foreign_keys = ON`).
- All schema is created with `CREATE TABLE IF NOT EXISTS` so restarting the
  bot never destroys existing data (see requirement #37, bot restart safety).
"""
from __future__ import annotations

import asyncio
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable, Optional, Sequence

from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

_connection: Optional[sqlite3.Connection] = None
_db_lock = asyncio.Lock()


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

-- Every Telegram user who has ever interacted with the bot.
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id     INTEGER UNIQUE NOT NULL,
    username        TEXT,
    first_name      TEXT NOT NULL,
    last_name       TEXT,
    is_admin        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Registered players for the auction. registration_no is permanent and
-- never reused, never reassigned, even if the player is removed.
CREATE TABLE IF NOT EXISTS players (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id     INTEGER UNIQUE NOT NULL,
    username        TEXT,
    first_name      TEXT NOT NULL,
    last_name       TEXT,
    registration_no INTEGER UNIQUE NOT NULL,
    status          TEXT NOT NULL DEFAULT 'REGISTERED',
    base_price      INTEGER,
    category        TEXT,
    team_id         INTEGER REFERENCES teams(id),
    sold_price      INTEGER,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_players_status ON players(status);
CREATE INDEX IF NOT EXISTS idx_players_regno ON players(registration_no);

-- A monotonically increasing counter table so registration numbers are
-- NEVER reused, even after deletes/restores. We never derive the next
-- number from MAX(registration_no) because that could reuse a number if a
-- row were ever hard-deleted; instead we keep an explicit counter.
CREATE TABLE IF NOT EXISTS counters (
    name    TEXT PRIMARY KEY,
    value   INTEGER NOT NULL
);
INSERT OR IGNORE INTO counters (name, value) VALUES ('registration_no', 0);

-- Captain requests. A captain always maps 1:1 to a team once approved.
CREATE TABLE IF NOT EXISTS captains (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id     INTEGER NOT NULL,
    captain_name    TEXT NOT NULL,
    team_name       TEXT NOT NULL,
    short_name      TEXT NOT NULL,
    logo_file_id    TEXT,
    status          TEXT NOT NULL DEFAULT 'PENDING',
    team_id         INTEGER REFERENCES teams(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_captains_telegram ON captains(telegram_id);
CREATE INDEX IF NOT EXISTS idx_captains_status ON captains(status);

CREATE TABLE IF NOT EXISTS teams (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    short_name          TEXT NOT NULL,
    captain_id          INTEGER NOT NULL REFERENCES captains(id),
    captain_telegram_id INTEGER NOT NULL,
    vice_captain_telegram_id INTEGER,
    starting_purse      INTEGER NOT NULL,
    remaining_purse     INTEGER NOT NULL,
    max_squad_size      INTEGER NOT NULL,
    is_active           INTEGER NOT NULL DEFAULT 1,
    low_purse_alerted   INTEGER NOT NULL DEFAULT 0,
    bid_blocked         INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_teams_captain ON teams(captain_telegram_id);

-- Historical record of every player that ever belonged to a team.
-- (players.team_id gives current state; this gives full history even
-- across undo/return operations.)
CREATE TABLE IF NOT EXISTS team_players (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id     INTEGER NOT NULL REFERENCES teams(id),
    player_id   INTEGER NOT NULL REFERENCES players(id),
    price       INTEGER NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 1,   -- 0 once undone/returned
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    removed_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_team_players_team ON team_players(team_id);
CREATE INDEX IF NOT EXISTS idx_team_players_player ON team_players(player_id);

-- Players retained by a team BEFORE the auction at a fixed price. A retained
-- player is stored like a normal sold player (players.status='SOLD',
-- team_id, sold_price, team_players row) plus a row here so we can tell
-- retained players apart and enforce the per-team retention limit.
CREATE TABLE IF NOT EXISTS retentions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id   INTEGER UNIQUE NOT NULL REFERENCES players(id),
    team_id     INTEGER NOT NULL REFERENCES teams(id),
    price       INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_retentions_team ON retentions(team_id);

-- One row per auction "set" (batch of up to N players by registration_no).
CREATE TABLE IF NOT EXISTS auction_sets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    set_number      INTEGER UNIQUE NOT NULL,
    mode            TEXT NOT NULL DEFAULT 'REGULAR',
    from_reg_no     INTEGER,
    to_reg_no       INTEGER,
    status          TEXT NOT NULL DEFAULT 'READY',  -- READY, ACTIVE, COMPLETED
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at    TEXT
);

-- Players assigned to a given set, in fixed order (by registration_no).
CREATE TABLE IF NOT EXISTS auction_set_players (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    set_id          INTEGER NOT NULL REFERENCES auction_sets(id),
    player_id       INTEGER NOT NULL REFERENCES players(id),
    sequence_no     INTEGER NOT NULL,
    processed       INTEGER NOT NULL DEFAULT 0,
    result          TEXT   -- SOLD / UNSOLD / null while pending
);
CREATE INDEX IF NOT EXISTS idx_setplayers_set ON auction_set_players(set_id);

-- One row per player's turn on the auction block (regular, unsold-round, or
-- re-auction). This is the "live" auction record.
CREATE TABLE IF NOT EXISTS auctions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id       INTEGER NOT NULL REFERENCES players(id),
    set_id          INTEGER REFERENCES auction_sets(id),
    mode            TEXT NOT NULL DEFAULT 'REGULAR',
    base_price      INTEGER NOT NULL,
    current_bid     INTEGER NOT NULL DEFAULT 0,
    highest_team_id INTEGER REFERENCES teams(id),
    status          TEXT NOT NULL DEFAULT 'PLAYER_AUCTION', -- PLAYER_AUCTION/PAUSED/PLAYER_SOLD/PLAYER_UNSOLD
    timer_seconds   INTEGER NOT NULL,
    deadline_ts     REAL,             -- unix timestamp when timer expires (null while paused)
    remaining_at_pause REAL,          -- seconds left, saved when paused
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_auctions_player ON auctions(player_id);
CREATE INDEX IF NOT EXISTS idx_auctions_status ON auctions(status);

-- Every bid ever placed (full audit trail).
CREATE TABLE IF NOT EXISTS bids (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    auction_id      INTEGER NOT NULL REFERENCES auctions(id),
    player_id       INTEGER NOT NULL REFERENCES players(id),
    team_id         INTEGER NOT NULL REFERENCES teams(id),
    amount          INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_bids_auction ON bids(auction_id);

-- Finalized SOLD/UNSOLD outcomes + reversals (undo/return). This is the
-- permanent historical ledger; reversal rows are ADDED, never delete prior
-- rows, so full history is always reconstructable.
CREATE TABLE IF NOT EXISTS auction_transactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    auction_id      INTEGER NOT NULL REFERENCES auctions(id),
    player_id       INTEGER NOT NULL REFERENCES players(id),
    team_id         INTEGER REFERENCES teams(id),
    set_id          INTEGER REFERENCES auction_sets(id),
    result          TEXT NOT NULL,       -- SOLD / UNSOLD
    price            INTEGER,
    tx_type         TEXT NOT NULL DEFAULT 'SALE',  -- SALE / UNDO / RETURN
    reversed         INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tx_player ON auction_transactions(player_id);

-- Players manually returned to the pool by admin, awaiting re-auction, in a
-- separate queue so they never disturb the original registration order.
CREATE TABLE IF NOT EXISTS reauction_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id       INTEGER NOT NULL REFERENCES players(id),
    reason          TEXT,
    added_at        TEXT NOT NULL DEFAULT (datetime('now')),
    processed       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_telegram_id INTEGER,
    action          TEXT NOT NULL,
    player_id       INTEGER,
    team_id         INTEGER,
    details         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action);

-- Global key/value settings (base price, timer, increments, etc). Seeded
-- with defaults from config.py on first run; admin can update at runtime.
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

-- Admins added at runtime by an owner (via /addadmin). Combined with the
-- fixed ADMIN_IDS from .env to form the effective admin set. Owners
-- themselves are NOT stored here — they only ever come from OWNER_IDS in
-- .env, so ownership can never be granted or revoked at runtime.
CREATE TABLE IF NOT EXISTS dynamic_admins (
    telegram_id     INTEGER PRIMARY KEY,
    added_by        INTEGER,
    added_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Singleton row holding the overall auction progression state
-- (IDLE, SET_READY, PLAYER_AUCTION, ... AUCTION_COMPLETED) plus a pointer
-- to the currently active auction/set, so the bot can safely recover after
-- a restart.
CREATE TABLE IF NOT EXISTS auction_state (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    state               TEXT NOT NULL DEFAULT 'IDLE',
    current_set_id      INTEGER REFERENCES auction_sets(id),
    current_auction_id  INTEGER REFERENCES auctions(id),
    chat_id             INTEGER,
    message_id          INTEGER,
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT OR IGNORE INTO auction_state (id, state) VALUES (1, 'IDLE');
"""


def _run_migrations(conn: sqlite3.Connection) -> None:
    """
    Lightweight, idempotent migrations for columns added after the initial
    schema was deployed. `CREATE TABLE IF NOT EXISTS` doesn't add new
    columns to an existing table, so any column added later needs an
    explicit, safe-to-rerun ALTER TABLE guarded by a column-existence check.
    """
    def _column_exists(table: str, column: str) -> bool:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(row["name"] == column for row in rows)

    if not _column_exists("players", "category"):
        conn.execute("ALTER TABLE players ADD COLUMN category TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_players_category ON players(category)")

    if not _column_exists("teams", "low_purse_alerted"):
        conn.execute("ALTER TABLE teams ADD COLUMN low_purse_alerted INTEGER NOT NULL DEFAULT 0")

    if not _column_exists("teams", "bid_blocked"):
        conn.execute("ALTER TABLE teams ADD COLUMN bid_blocked INTEGER NOT NULL DEFAULT 0")

    if not _column_exists("teams", "vice_captain_telegram_id"):
        conn.execute("ALTER TABLE teams ADD COLUMN vice_captain_telegram_id INTEGER")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_teams_vice_captain "
            "ON teams(vice_captain_telegram_id)"
        )

    conn.commit()


def _create_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.database_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    _run_migrations(conn)
    return conn


def get_connection() -> sqlite3.Connection:
    global _connection
    if _connection is None:
        _connection = _create_connection()
        logger.info("Database connection established at %s", settings.database_path)
    return _connection


async def init_db() -> None:
    """Initialize (or re-verify) the schema. Safe to call multiple times."""
    async with _db_lock:
        await asyncio.to_thread(get_connection)
    logger.info("Database initialized / verified.")


async def execute(sql: str, params: Sequence[Any] = ()) -> int:
    """Run an INSERT/UPDATE/DELETE. Returns lastrowid."""
    def _run() -> int:
        conn = get_connection()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid

    async with _db_lock:
        return await asyncio.to_thread(_run)


async def executemany(sql: str, seq_of_params: Iterable[Sequence[Any]]) -> None:
    def _run() -> None:
        conn = get_connection()
        conn.executemany(sql, seq_of_params)
        conn.commit()

    async with _db_lock:
        await asyncio.to_thread(_run)


async def fetch_one(sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
    def _run() -> Optional[sqlite3.Row]:
        conn = get_connection()
        cur = conn.execute(sql, params)
        return cur.fetchone()

    async with _db_lock:
        return await asyncio.to_thread(_run)


async def fetch_all(sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    def _run() -> list[sqlite3.Row]:
        conn = get_connection()
        cur = conn.execute(sql, params)
        return cur.fetchall()

    async with _db_lock:
        return await asyncio.to_thread(_run)


@contextmanager
def _transaction_cursor():
    """Internal sync context manager used only inside `transaction()` below."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


async def run_transaction(fn) -> Any:
    """
    Run a synchronous function `fn(conn)` inside a single DB transaction,
    serialized behind the global lock. `fn` may execute multiple statements;
    if it raises, everything is rolled back atomically.

    Used for: bid placement, sale finalization, undo, refund, return player.
    """
    def _run() -> Any:
        conn = get_connection()
        try:
            result = fn(conn)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise

    async with _db_lock:
        return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# Owner-only maintenance: full reset and stats
# ---------------------------------------------------------------------------

# Tables that hold tournament DATA and get wiped by a full reset. Config
# tables (settings, dynamic_admins) and the counters table are intentionally
# excluded — a reset clears the tournament, not the bot's own configuration
# or the registration-number counter safety mechanism. The counter is reset
# separately (explicitly) only if requested, since keeping it running
# across tournaments avoids ever reusing a registration number by accident.
_RESET_TABLES = [
    "bids",                    # references auctions, teams, players
    "auction_transactions",    # references auctions, players, teams, sets
    "reauction_queue",         # references players
    "retentions",              # references teams, players
    "team_players",            # references teams, players
    "auction_set_players",     # references sets, players
    "auctions",                # references players, sets, teams
    "auction_sets",            # referenced by auctions/auction_set_players
    "players",                 # references teams
    "teams",                   # references captains
    "captains",
    "audit_logs",
    "users",
]


async def reset_all_tournament_data(also_reset_counter: bool = False) -> None:
    """
    Wipes all tournament data (players, captains, teams, auctions, bids,
    history, audit log) so a fresh tournament can start from zero. Bot
    configuration (settings, dynamic admins) is preserved unless the caller
    also wants the registration-number counter reset.

    This is a destructive, owner-only operation and is NOT reversible.
    """
    def _run() -> None:
        conn = get_connection()
        try:
            # auction_state holds FK references into auction_sets/auctions
            # (current_set_id, current_auction_id). Those must be cleared
            # FIRST, before those tables are deleted, or the deletes below
            # fail with FOREIGN KEY constraint failed.
            conn.execute(
                "UPDATE auction_state SET state = 'IDLE', current_set_id = NULL, "
                "current_auction_id = NULL, chat_id = NULL, message_id = NULL"
            )
            # captains.team_id -> teams(id) and teams.captain_id -> captains(id)
            # form a circular reference. Break the cycle by nulling out
            # captains.team_id before either table is deleted.
            conn.execute("UPDATE captains SET team_id = NULL")
            for table in _RESET_TABLES:
                conn.execute(f"DELETE FROM {table}")
            if also_reset_counter:
                conn.execute("UPDATE counters SET value = 0 WHERE name = 'registration_no'")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    async with _db_lock:
        await asyncio.to_thread(_run)


async def get_database_stats() -> dict[str, int]:
    """Row counts across key tables, for the owner-only /botstats command."""
    def _run() -> dict[str, int]:
        conn = get_connection()
        stats: dict[str, int] = {}
        tables = [
            "players", "captains", "teams", "auctions", "bids",
            "auction_transactions", "audit_logs", "users",
        ]
        for table in tables:
            row = conn.execute(f"SELECT COUNT(*) as cnt FROM {table}").fetchone()
            stats[table] = int(row["cnt"])
        return stats

    async with _db_lock:
        return await asyncio.to_thread(_run)


def get_database_file_size_bytes() -> int:
    import os
    try:
        return os.path.getsize(settings.database_path)
    except OSError:
        return 0

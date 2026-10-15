"""
database/queries.py
--------------------
Data access layer. All raw SQL lives here so the rest of the codebase
(services/handlers) never writes SQL directly. Functions are async and
delegate to database/db.py.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from config import settings as app_settings
from database import db
from database.models import AuditAction, PlayerStatus

# ---------------------------------------------------------------------------
# Settings (configurable auction parameters)
# ---------------------------------------------------------------------------

_DEFAULT_SETTINGS = {
    "starting_purse": str(app_settings.default_starting_purse),
    "base_price": str(app_settings.default_base_price),
    "auction_timer": str(app_settings.default_auction_timer),
    "max_squad_size": str(app_settings.default_max_squad_size),
    "max_retentions": "2",
    "max_players_per_set": str(app_settings.default_max_players_per_set),
    "captain_registration_open": "1",
    "min_players_to_start": str(app_settings.default_min_players_to_start),
    "max_teams": str(app_settings.default_max_teams),
    "registration_deadline": "",
    "low_purse_alert_percent": "10",
    "bot_language": "en",
}


async def ensure_default_settings() -> None:
    for key, value in _DEFAULT_SETTINGS.items():
        existing = await db.fetch_one("SELECT key FROM settings WHERE key = ?", (key,))
        if existing is None:
            await db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)", (key, value)
            )

    # dashboard_token needs randomness so it can't live in the static
    # _DEFAULT_SETTINGS dict above — generate it once on first run.
    existing_token = await db.fetch_one("SELECT key FROM settings WHERE key = 'dashboard_token'")
    if existing_token is None:
        import secrets
        await db.execute(
            "INSERT INTO settings (key, value) VALUES ('dashboard_token', ?)",
            (secrets.token_urlsafe(24),),
        )


async def get_setting(key: str) -> str:
    row = await db.fetch_one("SELECT value FROM settings WHERE key = ?", (key,))
    if row is None:
        return _DEFAULT_SETTINGS.get(key, "")
    return row["value"]


async def get_setting_int(key: str) -> int:
    return int(await get_setting(key))


async def set_setting(key: str, value: str) -> None:
    await db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


async def get_all_settings() -> dict[str, str]:
    rows = await db.fetch_all("SELECT key, value FROM settings")
    return {r["key"]: r["value"] for r in rows}


# ---------------------------------------------------------------------------
# Dynamic admins (owner-managed, runtime-added admins)
# ---------------------------------------------------------------------------

async def add_dynamic_admin(telegram_id: int, added_by: int) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO dynamic_admins (telegram_id, added_by) VALUES (?, ?)",
        (telegram_id, added_by),
    )


async def remove_dynamic_admin(telegram_id: int) -> bool:
    row = await db.fetch_one("SELECT telegram_id FROM dynamic_admins WHERE telegram_id = ?",
                              (telegram_id,))
    if row is None:
        return False
    await db.execute("DELETE FROM dynamic_admins WHERE telegram_id = ?", (telegram_id,))
    return True


async def list_dynamic_admins() -> list[sqlite3.Row]:
    return await db.fetch_all("SELECT * FROM dynamic_admins ORDER BY added_at ASC")


async def is_dynamic_admin(telegram_id: int) -> bool:
    row = await db.fetch_one("SELECT telegram_id FROM dynamic_admins WHERE telegram_id = ?",
                              (telegram_id,))
    return row is not None


async def get_lobby_message_ref() -> tuple[Optional[int], Optional[int], int]:
    """Returns (chat_id, message_id, page) for the pinned lobby message, if any."""
    chat_raw = await get_setting("lobby_chat_id")
    msg_raw = await get_setting("lobby_message_id")
    page_raw = await get_setting("lobby_page")
    chat_id = int(chat_raw) if chat_raw else None
    message_id = int(msg_raw) if msg_raw else None
    page = int(page_raw) if page_raw else 1
    return chat_id, message_id, page


async def set_lobby_message_ref(chat_id: int, message_id: int, page: int = 1) -> None:
    await set_setting("lobby_chat_id", str(chat_id))
    await set_setting("lobby_message_id", str(message_id))
    await set_setting("lobby_page", str(page))


async def set_lobby_page(page: int) -> None:
    await set_setting("lobby_page", str(page))


async def clear_lobby_message_ref() -> None:
    await set_setting("lobby_chat_id", "")
    await set_setting("lobby_message_id", "")


# ---------------------------------------------------------------------------
# Users (generic Telegram user tracking)
# ---------------------------------------------------------------------------

async def upsert_user(telegram_id: int, username: Optional[str], first_name: str,
                       last_name: Optional[str], is_admin: bool = False) -> None:
    await db.execute(
        """
        INSERT INTO users (telegram_id, username, first_name, last_name, is_admin)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            last_name = excluded.last_name
        """,
        (telegram_id, username, first_name, last_name, int(is_admin)),
    )


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

async def add_audit_log(actor_telegram_id: Optional[int], action: AuditAction,
                         player_id: Optional[int] = None, team_id: Optional[int] = None,
                         details: Optional[dict[str, Any]] = None) -> None:
    await db.execute(
        """
        INSERT INTO audit_logs (actor_telegram_id, action, player_id, team_id, details)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            actor_telegram_id,
            action.value if isinstance(action, AuditAction) else str(action),
            player_id,
            team_id,
            json.dumps(details) if details else None,
        ),
    )


async def get_audit_logs(limit: int = 50) -> list[sqlite3.Row]:
    return await db.fetch_all(
        "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,)
    )


# ---------------------------------------------------------------------------
# Registration number counter (never reused)
# ---------------------------------------------------------------------------

def _next_registration_no_sync(conn: sqlite3.Connection) -> int:
    conn.execute(
        "UPDATE counters SET value = value + 1 WHERE name = 'registration_no'"
    )
    row = conn.execute(
        "SELECT value FROM counters WHERE name = 'registration_no'"
    ).fetchone()
    return int(row["value"])


# ---------------------------------------------------------------------------
# Players
# ---------------------------------------------------------------------------

async def get_player_by_telegram_id(telegram_id: int) -> Optional[sqlite3.Row]:
    return await db.fetch_one(
        "SELECT * FROM players WHERE telegram_id = ?", (telegram_id,)
    )


async def get_player_by_id(player_id: int) -> Optional[sqlite3.Row]:
    return await db.fetch_one("SELECT * FROM players WHERE id = ?", (player_id,))


async def get_player_by_registration_no(reg_no: int) -> Optional[sqlite3.Row]:
    return await db.fetch_one(
        "SELECT * FROM players WHERE registration_no = ?", (reg_no,)
    )


async def register_player(telegram_id: int, username: Optional[str], first_name: str,
                           last_name: Optional[str]) -> "sqlite3.Row":
    """
    Registers a new player with a permanent, sequential registration number.

    If this telegram_id was previously registered and then REMOVED by an
    admin, this restores the existing row (same registration number,
    refreshed name/username) back to AVAILABLE instead of raising an error —
    a removed player re-registering is exactly what /register is for.
    Raises ValueError only if they are currently registered under any
    non-removed status.
    """
    existing = await get_player_by_telegram_id(telegram_id)
    if existing is not None:
        if existing["status"] != PlayerStatus.REMOVED.value:
            raise ValueError("This Telegram account is already registered as a player.")

        await db.execute(
            """
            UPDATE players
            SET status = ?, username = ?, first_name = ?, last_name = ?,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (PlayerStatus.AVAILABLE.value, username, first_name, last_name, existing["id"]),
        )
        return await get_player_by_id(existing["id"])

    base_price = await get_setting_int("base_price")

    def _run(conn: sqlite3.Connection) -> int:
        reg_no = _next_registration_no_sync(conn)
        cur = conn.execute(
            """
            INSERT INTO players
                (telegram_id, username, first_name, last_name, registration_no,
                 status, base_price)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (telegram_id, username, first_name, last_name, reg_no,
             PlayerStatus.AVAILABLE.value, base_price),
        )
        return cur.lastrowid

    player_id = await db.run_transaction(_run)
    return await get_player_by_id(player_id)


async def list_players(status: Optional[str] = None,
                        include_removed: bool = False) -> list[sqlite3.Row]:
    if status:
        return await db.fetch_all(
            "SELECT * FROM players WHERE status = ? ORDER BY registration_no ASC",
            (status,),
        )
    if include_removed:
        return await db.fetch_all("SELECT * FROM players ORDER BY registration_no ASC")
    return await db.fetch_all(
        "SELECT * FROM players WHERE status != ? ORDER BY registration_no ASC",
        (PlayerStatus.REMOVED.value,),
    )


async def search_players(query: str) -> list[sqlite3.Row]:
    like = f"%{query}%"
    return await db.fetch_all(
        """
        SELECT * FROM players
        WHERE first_name LIKE ? OR last_name LIKE ? OR username LIKE ?
           OR CAST(registration_no AS TEXT) = ?
        ORDER BY registration_no ASC
        """,
        (like, like, like, query),
    )


async def update_player_status(player_id: int, status: PlayerStatus) -> None:
    await db.execute(
        "UPDATE players SET status = ?, updated_at = datetime('now') WHERE id = ?",
        (status.value, player_id),
    )


async def set_player_removed(player_id: int) -> None:
    await update_player_status(player_id, PlayerStatus.REMOVED)


async def set_player_restored(player_id: int) -> None:
    await update_player_status(player_id, PlayerStatus.AVAILABLE)


async def edit_player(player_id: int, first_name: Optional[str] = None,
                       last_name: Optional[str] = None,
                       base_price: Optional[int] = None,
                       category: Optional[str] = None) -> None:
    fields = []
    values: list[Any] = []
    if first_name is not None:
        fields.append("first_name = ?")
        values.append(first_name)
    if last_name is not None:
        fields.append("last_name = ?")
        values.append(last_name)
    if base_price is not None:
        fields.append("base_price = ?")
        values.append(base_price)
    if category is not None:
        fields.append("category = ?")
        values.append(category)
    if not fields:
        return
    fields.append("updated_at = datetime('now')")
    values.append(player_id)
    await db.execute(f"UPDATE players SET {', '.join(fields)} WHERE id = ?", values)


async def set_player_category(player_id: int, category: str) -> None:
    await db.execute(
        "UPDATE players SET category = ?, updated_at = datetime('now') WHERE id = ?",
        (category, player_id),
    )


async def list_players_by_category(category: str) -> list[sqlite3.Row]:
    return await db.fetch_all(
        "SELECT * FROM players WHERE category = ? AND status != 'REMOVED' "
        "ORDER BY registration_no ASC",
        (category,),
    )


async def get_category_counts() -> dict[str, int]:
    rows = await db.fetch_all(
        "SELECT COALESCE(category, 'Uncategorized') as category, COUNT(*) as cnt "
        "FROM players WHERE status != 'REMOVED' GROUP BY category"
    )
    return {r["category"]: r["cnt"] for r in rows}


async def count_players_by_status() -> dict[str, int]:
    rows = await db.fetch_all(
        "SELECT status, COUNT(*) as cnt FROM players GROUP BY status"
    )
    return {r["status"]: r["cnt"] for r in rows}


async def get_available_players_from(min_reg_no: int, limit: int) -> list[sqlite3.Row]:
    """
    Returns up to `limit` AVAILABLE players with registration_no >= min_reg_no,
    ordered strictly by registration_no. Used to build regular auction sets.
    """
    return await db.fetch_all(
        """
        SELECT * FROM players
        WHERE status = ? AND registration_no >= ?
        ORDER BY registration_no ASC
        LIMIT ?
        """,
        (PlayerStatus.AVAILABLE.value, min_reg_no, limit),
    )


async def get_max_available_registration_no() -> Optional[int]:
    row = await db.fetch_one(
        "SELECT MAX(registration_no) as m FROM players WHERE status = ?",
        (PlayerStatus.AVAILABLE.value,),
    )
    return row["m"] if row and row["m"] is not None else None


async def count_available_players() -> int:
    row = await db.fetch_one(
        "SELECT COUNT(*) as cnt FROM players WHERE status = ?",
        (PlayerStatus.AVAILABLE.value,),
    )
    return int(row["cnt"]) if row else 0


async def list_sold_players() -> list[sqlite3.Row]:
    return await db.fetch_all(
        """
        SELECT p.*, t.name as team_name, t.short_name as team_short
        FROM players p
        LEFT JOIN teams t ON p.team_id = t.id
        WHERE p.status = ?
        ORDER BY p.updated_at ASC
        """,
        (PlayerStatus.SOLD.value,),
    )


async def list_unsold_players() -> list[sqlite3.Row]:
    return await db.fetch_all(
        "SELECT * FROM players WHERE status = ? ORDER BY registration_no ASC",
        (PlayerStatus.UNSOLD.value,),
    )


# ---------------------------------------------------------------------------
# Captains
# ---------------------------------------------------------------------------

async def get_captain_by_telegram_id(telegram_id: int) -> Optional[sqlite3.Row]:
    return await db.fetch_one(
        "SELECT * FROM captains WHERE telegram_id = ? ORDER BY id DESC LIMIT 1",
        (telegram_id,),
    )


async def get_captain_by_id(captain_id: int) -> Optional[sqlite3.Row]:
    return await db.fetch_one("SELECT * FROM captains WHERE id = ?", (captain_id,))


async def create_captain_request(telegram_id: int, captain_name: str, team_name: str,
                                  short_name: str, logo_file_id: Optional[str]) -> sqlite3.Row:
    existing = await get_captain_by_telegram_id(telegram_id)
    if existing and existing["status"] in ("PENDING", "APPROVED"):
        raise ValueError("You already have a pending or approved captain request.")

    captain_id = await db.execute(
        """
        INSERT INTO captains (telegram_id, captain_name, team_name, short_name,
                               logo_file_id, status)
        VALUES (?, ?, ?, ?, ?, 'PENDING')
        """,
        (telegram_id, captain_name, team_name, short_name, logo_file_id),
    )
    return await get_captain_by_id(captain_id)


async def list_captains(status: Optional[str] = None) -> list[sqlite3.Row]:
    if status:
        return await db.fetch_all(
            "SELECT * FROM captains WHERE status = ? ORDER BY id ASC", (status,)
        )
    return await db.fetch_all("SELECT * FROM captains ORDER BY id ASC")


async def update_captain_status(captain_id: int, status: str) -> None:
    await db.execute(
        "UPDATE captains SET status = ?, updated_at = datetime('now') WHERE id = ?",
        (status, captain_id),
    )


async def link_captain_to_team(captain_id: int, team_id: int) -> None:
    await db.execute("UPDATE captains SET team_id = ? WHERE id = ?", (team_id, captain_id))


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------

async def create_team(name: str, short_name: str, captain_id: int,
                       captain_telegram_id: int) -> sqlite3.Row:
    starting_purse = await get_setting_int("starting_purse")
    max_squad_size = await get_setting_int("max_squad_size")

    team_id = await db.execute(
        """
        INSERT INTO teams (name, short_name, captain_id, captain_telegram_id,
                            starting_purse, remaining_purse, max_squad_size)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (name, short_name, captain_id, captain_telegram_id,
         starting_purse, starting_purse, max_squad_size),
    )
    await link_captain_to_team(captain_id, team_id)
    return await get_team_by_id(team_id)


async def get_team_by_id(team_id: int) -> Optional[sqlite3.Row]:
    return await db.fetch_one("SELECT * FROM teams WHERE id = ?", (team_id,))


async def get_known_name_for_telegram_id(telegram_id: int) -> Optional[str]:
    """Best-effort display name for a Telegram user the bot has seen before
    (from the users table, then the players table). None if unknown."""
    for table in ("users", "players"):
        row = await db.fetch_one(
            f"SELECT first_name, last_name FROM {table} WHERE telegram_id = ?",
            (telegram_id,),
        )
        if row and row["first_name"]:
            return " ".join(x for x in (row["first_name"], row["last_name"]) if x)
    return None


async def get_team_by_captain_telegram_id(telegram_id: int) -> Optional[sqlite3.Row]:
    return await db.fetch_one(
        "SELECT * FROM teams WHERE captain_telegram_id = ? AND is_active = 1",
        (telegram_id,),
    )


async def get_team_by_captain_or_vice(telegram_id: int) -> Optional[sqlite3.Row]:
    """Resolves a team the given telegram_id can bid for — either as the
    primary captain or as the team's vice-captain."""
    return await db.fetch_one(
        "SELECT * FROM teams WHERE (captain_telegram_id = ? OR vice_captain_telegram_id = ?) "
        "AND is_active = 1",
        (telegram_id, telegram_id),
    )


async def set_vice_captain(team_id: int, vice_captain_telegram_id: Optional[int]) -> None:
    await db.execute(
        "UPDATE teams SET vice_captain_telegram_id = ? WHERE id = ?",
        (vice_captain_telegram_id, team_id),
    )


async def list_teams() -> list[sqlite3.Row]:
    return await db.fetch_all("SELECT * FROM teams ORDER BY id ASC")


async def get_team_squad_size(team_id: int) -> int:
    """Slots used by the team (captain + vice-captain + sold/retained)."""
    team = await get_team_by_id(team_id)
    squad = await get_team_squad(team_id)
    if team is None:
        return len(squad)
    return team_member_count(team, squad)


# ---------------------------------------------------------------------------
# Retentions
# ---------------------------------------------------------------------------

async def get_retained_player_ids(team_id: Optional[int] = None) -> set[int]:
    if team_id is None:
        rows = await db.fetch_all(
            "SELECT r.player_id FROM retentions r JOIN players p ON p.id = r.player_id "
            "WHERE p.status = 'SOLD'"
        )
    else:
        rows = await db.fetch_all(
            "SELECT r.player_id FROM retentions r JOIN players p ON p.id = r.player_id "
            "WHERE p.team_id = ? AND p.status = 'SOLD'",
            (team_id,),
        )
    return {r["player_id"] for r in rows}


async def count_team_retentions(team_id: int) -> int:
    return len(await get_retained_player_ids(team_id))


async def get_retention_for_player(player_id: int) -> Optional[sqlite3.Row]:
    return await db.fetch_one("SELECT * FROM retentions WHERE player_id = ?", (player_id,))


async def list_retained_players() -> list[sqlite3.Row]:
    return await db.fetch_all(
        """
        SELECT p.*, r.price AS retention_price, t.name AS team_name, t.short_name AS team_short
        FROM retentions r
        JOIN players p ON p.id = r.player_id AND p.status = 'SOLD'
        JOIN teams t ON t.id = p.team_id
        ORDER BY t.id ASC, r.price DESC
        """
    )


async def player_in_any_set(player_id: int) -> bool:
    row = await db.fetch_one(
        "SELECT 1 AS x FROM auction_set_players WHERE player_id = ? LIMIT 1", (player_id,)
    )
    return row is not None


async def retain_player_tx(player_id: int, team_id: int, price: int) -> None:
    """Atomically retains a player: marks them SOLD to the team at `price`,
    deducts the purse, and records the team_players + retentions rows."""
    def _run(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE players SET status = 'SOLD', team_id = ?, sold_price = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (team_id, price, player_id),
        )
        conn.execute(
            "UPDATE teams SET remaining_purse = remaining_purse - ? WHERE id = ?",
            (price, team_id),
        )
        conn.execute(
            "INSERT INTO team_players (team_id, player_id, price, is_active) VALUES (?, ?, ?, 1)",
            (team_id, player_id, price),
        )
        conn.execute(
            "INSERT INTO retentions (player_id, team_id, price) VALUES (?, ?, ?)",
            (player_id, team_id, price),
        )
    await db.run_transaction(_run)


async def release_retention_tx(player_id: int, refund: bool = True) -> None:
    """Undoes a retention: refunds the purse (if refund=True), returns the
    player to the AVAILABLE pool and removes the retention record."""
    def _run(conn: sqlite3.Connection) -> None:
        player = conn.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()
        ret = conn.execute("SELECT * FROM retentions WHERE player_id = ?", (player_id,)).fetchone()
        if player is None or ret is None:
            raise ValueError("This player is not retained.")
        if refund and player["team_id"] is not None:
            conn.execute(
                "UPDATE teams SET remaining_purse = remaining_purse + ? WHERE id = ?",
                (player["sold_price"] or ret["price"], player["team_id"]),
            )
        conn.execute(
            "UPDATE team_players SET is_active = 0, removed_at = datetime('now') "
            "WHERE player_id = ? AND is_active = 1",
            (player_id,),
        )
        conn.execute(
            "UPDATE players SET status = 'AVAILABLE', team_id = NULL, sold_price = NULL, "
            "updated_at = datetime('now') WHERE id = ?",
            (player_id,),
        )
        conn.execute("DELETE FROM retentions WHERE player_id = ?", (player_id,))
    await db.run_transaction(_run)


def team_member_count(team, squad) -> int:
    """Squad slots used by a team = captain + vice-captain (if set) + every
    sold/retained player. The captain and vice-captain are not double
    counted if they were also bought/retained as a player."""
    player_tg_ids = {p["telegram_id"] for p in squad}
    count = len(squad)
    if team["captain_telegram_id"] not in player_tg_ids:
        count += 1
    vice = team["vice_captain_telegram_id"]
    if vice and vice != team["captain_telegram_id"] and vice not in player_tg_ids:
        count += 1
    return count


async def get_team_squad(team_id: int) -> list[sqlite3.Row]:
    return await db.fetch_all(
        "SELECT * FROM players WHERE team_id = ? AND status = 'SOLD' ORDER BY sold_price DESC",
        (team_id,),
    )


async def deactivate_team(team_id: int) -> None:
    await db.execute("UPDATE teams SET is_active = 0 WHERE id = ?", (team_id,))


async def set_team_bid_blocked(team_id: int, blocked: bool) -> None:
    await db.execute("UPDATE teams SET bid_blocked = ? WHERE id = ?", (int(blocked), team_id))


async def mark_low_purse_alerted(team_id: int) -> None:
    await db.execute("UPDATE teams SET low_purse_alerted = 1 WHERE id = ?", (team_id,))


async def get_team_by_name_or_id(identifier: str) -> Optional[sqlite3.Row]:
    """Looks up a team by numeric id first, then by exact name/short_name."""
    if identifier.isdigit():
        row = await get_team_by_id(int(identifier))
        if row is not None:
            return row
    return await db.fetch_one(
        "SELECT * FROM teams WHERE name = ? OR short_name = ? COLLATE NOCASE",
        (identifier, identifier),
    )


# ---------------------------------------------------------------------------
# Auction sets
# ---------------------------------------------------------------------------

async def get_auction_set_by_number(set_number: int) -> Optional[sqlite3.Row]:
    return await db.fetch_one(
        "SELECT * FROM auction_sets WHERE set_number = ?", (set_number,)
    )


async def get_auction_set_by_id(set_id: int) -> Optional[sqlite3.Row]:
    return await db.fetch_one("SELECT * FROM auction_sets WHERE id = ?", (set_id,))


async def get_latest_set_number() -> int:
    row = await db.fetch_one("SELECT MAX(set_number) as m FROM auction_sets")
    return int(row["m"]) if row and row["m"] is not None else 0


async def create_auction_set(set_number: int, mode: str, from_reg_no: Optional[int],
                              to_reg_no: Optional[int],
                              player_ids_in_order: list[int]) -> sqlite3.Row:
    def _run(conn: sqlite3.Connection) -> int:
        cur = conn.execute(
            """
            INSERT INTO auction_sets (set_number, mode, from_reg_no, to_reg_no, status)
            VALUES (?, ?, ?, ?, 'READY')
            """,
            (set_number, mode, from_reg_no, to_reg_no),
        )
        set_id = cur.lastrowid
        for seq, pid in enumerate(player_ids_in_order, start=1):
            conn.execute(
                """
                INSERT INTO auction_set_players (set_id, player_id, sequence_no)
                VALUES (?, ?, ?)
                """,
                (set_id, pid, seq),
            )
        return set_id

    set_id = await db.run_transaction(_run)
    return await get_auction_set_by_id(set_id)


async def get_set_players(set_id: int) -> list[sqlite3.Row]:
    return await db.fetch_all(
        """
        SELECT sp.*, p.first_name, p.last_name, p.registration_no, p.status as player_status,
               p.base_price, p.telegram_id, p.username, p.category
        FROM auction_set_players sp
        JOIN players p ON sp.player_id = p.id
        WHERE sp.set_id = ?
        ORDER BY sp.sequence_no ASC
        """,
        (set_id,),
    )


async def get_next_unprocessed_set_player(set_id: int) -> Optional[sqlite3.Row]:
    return await db.fetch_one(
        """
        SELECT sp.*, p.first_name, p.last_name, p.registration_no, p.base_price,
               p.status as player_status
        FROM auction_set_players sp
        JOIN players p ON sp.player_id = p.id
        WHERE sp.set_id = ? AND sp.processed = 0
        ORDER BY sp.sequence_no ASC
        LIMIT 1
        """,
        (set_id,),
    )


async def mark_set_player_processed(set_id: int, player_id: int, result: str) -> None:
    await db.execute(
        """
        UPDATE auction_set_players
        SET processed = 1, result = ?
        WHERE set_id = ? AND player_id = ?
        """,
        (result, set_id, player_id),
    )


async def push_set_player_to_end(set_id: int, player_id: int) -> None:
    """
    Moves a player to the back of the current set's queue (highest
    sequence_no + 1) without marking them processed, so they come up again
    later in the SAME set instead of being finalized. Used for
    "skip and return later" — the admin defers this player without
    recording a SOLD/UNSOLD outcome.
    """
    def _run(conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT MAX(sequence_no) as m FROM auction_set_players WHERE set_id = ?",
            (set_id,),
        ).fetchone()
        max_seq = row["m"] or 0
        conn.execute(
            """
            UPDATE auction_set_players
            SET sequence_no = ?, processed = 0, result = NULL
            WHERE set_id = ? AND player_id = ?
            """,
            (max_seq + 1, set_id, player_id),
        )

    await db.run_transaction(_run)


async def update_auction_set_status(set_id: int, status: str,
                                     completed: bool = False) -> None:
    if completed:
        await db.execute(
            "UPDATE auction_sets SET status = ?, completed_at = datetime('now') WHERE id = ?",
            (status, set_id),
        )
    else:
        await db.execute(
            "UPDATE auction_sets SET status = ? WHERE id = ?", (status, set_id)
        )


async def get_set_progress(set_id: int) -> dict[str, int]:
    rows = await db.fetch_all(
        "SELECT result, COUNT(*) as cnt FROM auction_set_players "
        "WHERE set_id = ? AND processed = 1 GROUP BY result",
        (set_id,),
    )
    total_row = await db.fetch_one(
        "SELECT COUNT(*) as cnt FROM auction_set_players WHERE set_id = ?", (set_id,)
    )
    out = {"total": int(total_row["cnt"]) if total_row else 0, "SOLD": 0, "UNSOLD": 0}
    for r in rows:
        if r["result"] in out:
            out[r["result"]] = r["cnt"]
    return out


# ---------------------------------------------------------------------------
# Auctions (a player's live turn on the block) & bids
# ---------------------------------------------------------------------------

async def create_auction(player_id: int, set_id: Optional[int], mode: str,
                          base_price: int, timer_seconds: int,
                          deadline_ts: float) -> sqlite3.Row:
    auction_id = await db.execute(
        """
        INSERT INTO auctions (player_id, set_id, mode, base_price, current_bid,
                               status, timer_seconds, deadline_ts)
        VALUES (?, ?, ?, ?, 0, 'PLAYER_AUCTION', ?, ?)
        """,
        (player_id, set_id, mode, base_price, timer_seconds, deadline_ts),
    )
    return await get_auction_by_id(auction_id)


async def get_auction_by_id(auction_id: int) -> Optional[sqlite3.Row]:
    return await db.fetch_one("SELECT * FROM auctions WHERE id = ?", (auction_id,))


async def get_active_auction() -> Optional[sqlite3.Row]:
    return await db.fetch_one(
        "SELECT * FROM auctions WHERE status IN ('PLAYER_AUCTION') ORDER BY id DESC LIMIT 1"
    )


async def fetch_paused_auction() -> Optional[sqlite3.Row]:
    return await db.fetch_one(
        "SELECT * FROM auctions WHERE status = 'PAUSED' ORDER BY id DESC LIMIT 1"
    )


async def place_bid_tx(auction_id: int, team_id: int, amount: int,
                        new_deadline_ts: float) -> None:
    """Atomically record a bid and update the auction's current bid / bidder / timer."""
    def _run(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO bids (auction_id, player_id, team_id, amount) "
            "SELECT ?, player_id, ?, ? FROM auctions WHERE id = ?",
            (auction_id, team_id, amount, auction_id),
        )
        conn.execute(
            """
            UPDATE auctions
            SET current_bid = ?, highest_team_id = ?, deadline_ts = ?
            WHERE id = ?
            """,
            (amount, team_id, new_deadline_ts, auction_id),
        )

    await db.run_transaction(_run)


async def get_auction_bids(auction_id: int) -> list[sqlite3.Row]:
    return await db.fetch_all(
        """
        SELECT b.*, t.name as team_name FROM bids b
        JOIN teams t ON b.team_id = t.id
        WHERE b.auction_id = ?
        ORDER BY b.id ASC
        """,
        (auction_id,),
    )


async def get_bids_for_player(player_id: int) -> list[sqlite3.Row]:
    """All bids ever placed on a player, across every auction turn they've
    had (regular set, unsold round, re-auction), most recent turn first."""
    return await db.fetch_all(
        """
        SELECT b.*, t.name as team_name, a.mode as auction_mode
        FROM bids b
        JOIN teams t ON b.team_id = t.id
        JOIN auctions a ON b.auction_id = a.id
        WHERE b.player_id = ?
        ORDER BY b.auction_id ASC, b.id ASC
        """,
        (player_id,),
    )


async def pause_auction(auction_id: int, remaining_seconds: float) -> None:
    await db.execute(
        """
        UPDATE auctions
        SET status = 'PAUSED', remaining_at_pause = ?, deadline_ts = NULL
        WHERE id = ?
        """,
        (remaining_seconds, auction_id),
    )


async def resume_auction(auction_id: int, new_deadline_ts: float) -> None:
    await db.execute(
        """
        UPDATE auctions
        SET status = 'PLAYER_AUCTION', deadline_ts = ?, remaining_at_pause = NULL
        WHERE id = ?
        """,
        (new_deadline_ts, auction_id),
    )


async def finalize_auction_sold_tx(auction_id: int, player_id: int, team_id: int,
                                    price: int, set_id: Optional[int]) -> None:
    """
    Atomically: mark auction SOLD, update player (status/team/price), deduct
    team purse, record team_players row, record auction_transactions row,
    mark the set-player entry as processed (if part of a set).
    """
    def _run(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE auctions SET status = 'PLAYER_SOLD', ended_at = datetime('now') "
            "WHERE id = ?",
            (auction_id,),
        )
        conn.execute(
            """
            UPDATE players
            SET status = 'SOLD', team_id = ?, sold_price = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (team_id, price, player_id),
        )
        conn.execute(
            "UPDATE teams SET remaining_purse = remaining_purse - ? WHERE id = ?",
            (price, team_id),
        )
        conn.execute(
            "INSERT INTO team_players (team_id, player_id, price, is_active) "
            "VALUES (?, ?, ?, 1)",
            (team_id, player_id, price),
        )
        conn.execute(
            """
            INSERT INTO auction_transactions
                (auction_id, player_id, team_id, set_id, result, price, tx_type)
            VALUES (?, ?, ?, ?, 'SOLD', ?, 'SALE')
            """,
            (auction_id, player_id, team_id, set_id, price),
        )
        if set_id is not None:
            conn.execute(
                "UPDATE auction_set_players SET processed = 1, result = 'SOLD' "
                "WHERE set_id = ? AND player_id = ?",
                (set_id, player_id),
            )

    await db.run_transaction(_run)


async def finalize_auction_unsold_tx(auction_id: int, player_id: int,
                                      set_id: Optional[int]) -> None:
    def _run(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE auctions SET status = 'PLAYER_UNSOLD', ended_at = datetime('now') "
            "WHERE id = ?",
            (auction_id,),
        )
        conn.execute(
            "UPDATE players SET status = 'UNSOLD', updated_at = datetime('now') WHERE id = ?",
            (player_id,),
        )
        conn.execute(
            """
            INSERT INTO auction_transactions
                (auction_id, player_id, team_id, set_id, result, price, tx_type)
            VALUES (?, ?, NULL, ?, 'UNSOLD', NULL, 'SALE')
            """,
            (auction_id, player_id, set_id),
        )
        if set_id is not None:
            conn.execute(
                "UPDATE auction_set_players SET processed = 1, result = 'UNSOLD' "
                "WHERE set_id = ? AND player_id = ?",
                (set_id, player_id),
            )

    await db.run_transaction(_run)


# ---------------------------------------------------------------------------
# Undo / Return player (reversal transactions)
# ---------------------------------------------------------------------------

async def get_last_sale_transaction() -> Optional[sqlite3.Row]:
    return await db.fetch_one(
        """
        SELECT * FROM auction_transactions
        WHERE result = 'SOLD' AND tx_type = 'SALE' AND reversed = 0
        ORDER BY id DESC LIMIT 1
        """
    )


async def get_sale_transaction_for_player(player_id: int) -> Optional[sqlite3.Row]:
    return await db.fetch_one(
        """
        SELECT * FROM auction_transactions
        WHERE player_id = ? AND result = 'SOLD' AND tx_type = 'SALE' AND reversed = 0
        ORDER BY id DESC LIMIT 1
        """,
        (player_id,),
    )


async def reverse_sale_tx(transaction_id: int, player_id: int, team_id: int,
                           price: int, tx_type: str, new_player_status: str) -> None:
    """
    Atomically reverses a SOLD transaction: refund team, remove player from
    team, deactivate the team_players row, mark original transaction as
    reversed, and insert a new UNDO/RETURN audit transaction row (never
    deletes the original historical record).
    """
    def _run(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE teams SET remaining_purse = remaining_purse + ? WHERE id = ?",
            (price, team_id),
        )
        conn.execute(
            """
            UPDATE players
            SET status = ?, team_id = NULL, sold_price = NULL, updated_at = datetime('now')
            WHERE id = ?
            """,
            (new_player_status, player_id),
        )
        conn.execute(
            """
            UPDATE team_players SET is_active = 0, removed_at = datetime('now')
            WHERE team_id = ? AND player_id = ? AND is_active = 1
            """,
            (team_id, player_id),
        )
        conn.execute(
            "UPDATE auction_transactions SET reversed = 1 WHERE id = ?",
            (transaction_id,),
        )
        conn.execute(
            """
            INSERT INTO auction_transactions
                (auction_id, player_id, team_id, set_id, result, price, tx_type, reversed)
            SELECT auction_id, player_id, team_id, set_id, 'REVERSED', ?, ?, 0
            FROM auction_transactions WHERE id = ?
            """,
            (price, tx_type, transaction_id),
        )

    await db.run_transaction(_run)


# ---------------------------------------------------------------------------
# Re-auction queue
# ---------------------------------------------------------------------------

async def add_to_reauction_queue(player_id: int, reason: Optional[str] = None) -> None:
    await db.execute(
        "INSERT INTO reauction_queue (player_id, reason) VALUES (?, ?)",
        (player_id, reason),
    )


async def list_reauction_queue(unprocessed_only: bool = True) -> list[sqlite3.Row]:
    if unprocessed_only:
        return await db.fetch_all(
            """
            SELECT rq.*, p.first_name, p.last_name, p.registration_no, p.base_price
            FROM reauction_queue rq
            JOIN players p ON rq.player_id = p.id
            WHERE rq.processed = 0
            ORDER BY rq.id ASC
            """
        )
    return await db.fetch_all(
        """
        SELECT rq.*, p.first_name, p.last_name, p.registration_no, p.base_price
        FROM reauction_queue rq
        JOIN players p ON rq.player_id = p.id
        ORDER BY rq.id ASC
        """
    )


async def mark_reauction_processed(player_id: int) -> None:
    await db.execute(
        "UPDATE reauction_queue SET processed = 1 WHERE player_id = ? AND processed = 0",
        (player_id,),
    )


# ---------------------------------------------------------------------------
# Global auction state (singleton row, used for restart recovery)
# ---------------------------------------------------------------------------

async def get_auction_state() -> sqlite3.Row:
    row = await db.fetch_one("SELECT * FROM auction_state WHERE id = 1")
    assert row is not None
    return row


async def update_auction_state(state: Optional[str] = None,
                                current_set_id: Optional[int] = None,
                                current_auction_id: Optional[int] = None,
                                chat_id: Optional[int] = None,
                                message_id: Optional[int] = None,
                                clear_auction: bool = False,
                                clear_message: bool = False) -> None:
    current = await get_auction_state()
    new_state = state if state is not None else current["state"]
    new_set_id = current_set_id if current_set_id is not None else current["current_set_id"]
    if clear_auction:
        new_auction_id = None
    else:
        new_auction_id = (
            current_auction_id if current_auction_id is not None
            else current["current_auction_id"]
        )
    new_chat_id = chat_id if chat_id is not None else current["chat_id"]
    if clear_message:
        new_message_id = None
    else:
        new_message_id = message_id if message_id is not None else current["message_id"]

    await db.execute(
        """
        UPDATE auction_state
        SET state = ?, current_set_id = ?, current_auction_id = ?,
            chat_id = ?, message_id = ?, updated_at = datetime('now')
        WHERE id = 1
        """,
        (new_state, new_set_id, new_auction_id, new_chat_id, new_message_id),
    )


# ---------------------------------------------------------------------------
# Player trades between teams (post-auction, no purse change)
# ---------------------------------------------------------------------------

async def trade_player_tx(player_id: int, from_team_id: int, to_team_id: int,
                           initiated_by: int) -> None:
    """
    Atomically moves a SOLD player from one team to another. This is a pure
    trade — no purse is charged or refunded on either side, and the
    player's recorded sold_price is preserved (it's historical, not a new
    purchase). The old team_players row is deactivated and a new one is
    created for the receiving team, preserving full history the same way
    undo/return does. Logged to auction_transactions as tx_type='TRADE'.
    """
    def _run(conn: sqlite3.Connection) -> None:
        player = conn.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()
        if player is None:
            raise ValueError("Player not found.")

        conn.execute(
            "UPDATE players SET team_id = ?, updated_at = datetime('now') WHERE id = ?",
            (to_team_id, player_id),
        )
        conn.execute(
            """
            UPDATE team_players SET is_active = 0, removed_at = datetime('now')
            WHERE team_id = ? AND player_id = ? AND is_active = 1
            """,
            (from_team_id, player_id),
        )
        conn.execute(
            "INSERT INTO team_players (team_id, player_id, price, is_active) "
            "VALUES (?, ?, ?, 1)",
            (to_team_id, player_id, player["sold_price"] or 0),
        )
        conn.execute(
            """
            INSERT INTO auction_transactions
                (auction_id, player_id, team_id, set_id, result, price, tx_type)
            SELECT id, ?, ?, NULL, 'TRADED', ?, 'TRADE'
            FROM auctions WHERE player_id = ? ORDER BY id DESC LIMIT 1
            """,
            (player_id, to_team_id, player["sold_price"], player_id),
        )
        conn.execute(
            "INSERT INTO audit_logs (actor_telegram_id, action, player_id, team_id, details) "
            "VALUES (?, 'PLAYER_TRADED', ?, ?, ?)",
            (initiated_by, player_id, to_team_id,
             f'{{"from_team_id": {from_team_id}, "to_team_id": {to_team_id}}}'),
        )

    await db.run_transaction(_run)

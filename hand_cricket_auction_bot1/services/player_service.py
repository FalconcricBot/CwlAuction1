"""
services/player_service.py
---------------------------
Business logic for player registration, profile lookup, and admin
player-management operations (add/remove/restore/edit/search).
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from database import queries as q
from database.models import AuditAction, PlayerStatus
from utils.logger import get_logger

logger = get_logger(__name__)


class PlayerServiceError(Exception):
    """Raised for any invalid player-service operation with a user-facing message."""


async def register_player(telegram_id: int, username: Optional[str], first_name: str,
                           last_name: Optional[str]) -> sqlite3.Row:
    existing_captain = await q.get_captain_by_telegram_id(telegram_id)
    if existing_captain and existing_captain["status"] in ("PENDING", "APPROVED"):
        raise PlayerServiceError(
            "This Telegram account is already registered as a captain and "
            "cannot also register as a player."
        )

    try:
        player = await q.register_player(telegram_id, username, first_name, last_name)
    except ValueError as e:
        raise PlayerServiceError(str(e)) from e

    await q.add_audit_log(
        telegram_id, AuditAction.PLAYER_REGISTERED, player_id=player["id"],
        details={"registration_no": player["registration_no"]},
    )
    logger.info("Player registered: %s (#%s)", first_name, player["registration_no"])
    return player


async def get_profile(telegram_id: int) -> Optional[sqlite3.Row]:
    return await q.get_player_by_telegram_id(telegram_id)


async def get_profile_team(player_row: sqlite3.Row) -> Optional[sqlite3.Row]:
    if player_row["team_id"] is None:
        return None
    return await q.get_team_by_id(player_row["team_id"])


async def list_all_players(include_removed: bool = False):
    return await q.list_players(include_removed=include_removed)


async def search_players(query: str):
    return await q.search_players(query)


async def admin_add_player(admin_id: int, telegram_id: int, first_name: str,
                            last_name: Optional[str]) -> sqlite3.Row:
    """Admin manually registers a player on someone's behalf (e.g. no Telegram DM yet)."""
    try:
        player = await q.register_player(telegram_id, None, first_name, last_name)
    except ValueError as e:
        raise PlayerServiceError(str(e)) from e

    await q.add_audit_log(
        admin_id, AuditAction.PLAYER_REGISTERED, player_id=player["id"],
        details={"registration_no": player["registration_no"], "added_by_admin": True},
    )
    return player


async def remove_player(admin_id: int, player_id: int) -> sqlite3.Row:
    player = await q.get_player_by_id(player_id)
    if player is None:
        raise PlayerServiceError("Player not found.")
    if player["status"] == PlayerStatus.REMOVED.value:
        raise PlayerServiceError("Player is already removed.")
    if player["status"] == PlayerStatus.SOLD.value:
        raise PlayerServiceError(
            "This player is already SOLD. Use /returnplayer first if you need to remove them."
        )

    await q.set_player_removed(player_id)
    await q.add_audit_log(
        admin_id, AuditAction.PLAYER_REMOVED, player_id=player_id,
        details={"registration_no": player["registration_no"]},
    )
    logger.info("Player #%s removed by admin %s", player["registration_no"], admin_id)
    return await q.get_player_by_id(player_id)


async def restore_player(admin_id: int, player_id: int) -> sqlite3.Row:
    player = await q.get_player_by_id(player_id)
    if player is None:
        raise PlayerServiceError("Player not found.")
    if player["status"] != PlayerStatus.REMOVED.value:
        raise PlayerServiceError("Player is not currently removed.")

    await q.set_player_restored(player_id)
    await q.add_audit_log(
        admin_id, AuditAction.PLAYER_RESTORED, player_id=player_id,
        details={"registration_no": player["registration_no"]},
    )
    logger.info("Player #%s restored by admin %s", player["registration_no"], admin_id)
    return await q.get_player_by_id(player_id)


async def edit_player(admin_id: int, player_id: int, first_name: Optional[str] = None,
                       last_name: Optional[str] = None,
                       base_price: Optional[int] = None) -> sqlite3.Row:
    player = await q.get_player_by_id(player_id)
    if player is None:
        raise PlayerServiceError("Player not found.")

    await q.edit_player(player_id, first_name=first_name, last_name=last_name,
                         base_price=base_price)
    await q.add_audit_log(
        admin_id, AuditAction.PLAYER_EDITED, player_id=player_id,
        details={"first_name": first_name, "last_name": last_name, "base_price": base_price},
    )
    return await q.get_player_by_id(player_id)


async def get_status_counts() -> dict[str, int]:
    return await q.count_players_by_status()

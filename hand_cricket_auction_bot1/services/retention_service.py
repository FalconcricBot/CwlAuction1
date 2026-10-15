"""
services/retention_service.py
------------------------------
Pre-auction player retention. A retained player is attached to a team at a
fixed price (deducted from the purse) and never enters the auction pool.
"""
from __future__ import annotations

import sqlite3

from database import queries as q
from database.models import AuditAction
from utils.logger import get_logger

logger = get_logger(__name__)


class RetentionError(Exception):
    pass


_STATUS_MSG = {
    "SOLD": "is already SOLD / on a team",
    "IN_AUCTION": "is currently on the auction block",
    "UNSOLD": "has already gone through the auction as UNSOLD",
    "REMOVED": "has been removed",
}


async def retain_player(admin_id: int, reg_no: int, team: sqlite3.Row,
                        price: int | None) -> dict:
    player = await q.get_player_by_registration_no(reg_no)
    if player is None:
        raise RetentionError(f"No player found with registration #{reg_no}.")
    if player["status"] not in ("REGISTERED", "AVAILABLE"):
        why = _STATUS_MSG.get(player["status"], f"has status {player['status']}")
        raise RetentionError(f"#{reg_no} {why}, so can't be retained.")
    if await q.player_in_any_set(player["id"]):
        raise RetentionError(
            f"#{reg_no} is already part of an auction set. Retention must be done "
            "before the player's set is built."
        )
    if not team["is_active"]:
        raise RetentionError(f"{team['name']} is not an active team.")

    if price is None:
        price = player["base_price"] or await q.get_setting_int("base_price")
    if price <= 0:
        raise RetentionError("Retention price must be greater than 0.")
    if price > team["remaining_purse"]:
        raise RetentionError(
            f"{team['name']} only has ₹{team['remaining_purse']:,} remaining."
        )

    max_ret = await q.get_setting_int("max_retentions")
    used_ret = await q.count_team_retentions(team["id"])
    if used_ret >= max_ret:
        raise RetentionError(
            f"{team['name']} has already used all retentions ({used_ret}/{max_ret})."
        )

    used_slots = await q.get_team_squad_size(team["id"])
    if used_slots >= team["max_squad_size"]:
        raise RetentionError(
            f"{team['name']} already has a full squad ({used_slots}/{team['max_squad_size']})."
        )

    await q.retain_player_tx(player["id"], team["id"], price)
    await q.add_audit_log(
        admin_id, AuditAction.PLAYER_RETAINED, player_id=player["id"], team_id=team["id"],
        details={"price": price},
    )
    logger.info("Retained #%s for team %s at %s", reg_no, team["name"], price)
    return {
        "player": await q.get_player_by_id(player["id"]),
        "team": await q.get_team_by_id(team["id"]),
        "price": price,
        "retentions_used": used_ret + 1,
        "retentions_max": max_ret,
    }


async def remove_retention(admin_id: int, reg_no: int) -> dict:
    player = await q.get_player_by_registration_no(reg_no)
    if player is None:
        raise RetentionError(f"No player found with registration #{reg_no}.")
    if await q.get_retention_for_player(player["id"]) is None or player["status"] != "SOLD":
        raise RetentionError(f"#{reg_no} is not a retained player.")
    team = await q.get_team_by_id(player["team_id"]) if player["team_id"] else None
    price = player["sold_price"] or 0
    try:
        await q.release_retention_tx(player["id"], refund=team is not None)
    except ValueError as e:
        raise RetentionError(str(e)) from e
    await q.add_audit_log(
        admin_id, AuditAction.RETENTION_REMOVED, player_id=player["id"],
        team_id=team["id"] if team else None, details={"refund": price},
    )
    return {
        "player": await q.get_player_by_id(player["id"]),
        "team": await q.get_team_by_id(team["id"]) if team else None,
        "refund": price,
    }

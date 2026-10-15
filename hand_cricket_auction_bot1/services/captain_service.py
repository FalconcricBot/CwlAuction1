"""
services/captain_service.py
----------------------------
Captain registration/approval and team creation logic. A team is created
automatically the moment a captain is APPROVED (not before), so PENDING or
REJECTED captains never have a team and can never bid.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from database import queries as q
from database.models import AuditAction, CaptainStatus
from utils.logger import get_logger

logger = get_logger(__name__)


class CaptainServiceError(Exception):
    pass


async def is_registration_open() -> bool:
    return (await q.get_setting("captain_registration_open")) == "1"


async def set_registration_open(open_: bool) -> None:
    await q.set_setting("captain_registration_open", "1" if open_ else "0")


async def request_captain(telegram_id: int, captain_name: str, team_name: str,
                           short_name: str, logo_file_id: Optional[str]) -> sqlite3.Row:
    if not await is_registration_open():
        raise CaptainServiceError("Captain registration is currently closed by the admin.")

    existing_player = await q.get_player_by_telegram_id(telegram_id)
    if existing_player and existing_player["status"] != "REMOVED":
        raise CaptainServiceError(
            "This Telegram account is already registered as a player and "
            "cannot also become a captain."
        )

    try:
        captain = await q.create_captain_request(
            telegram_id, captain_name, team_name, short_name, logo_file_id
        )
    except ValueError as e:
        raise CaptainServiceError(str(e)) from e

    await q.add_audit_log(
        telegram_id, AuditAction.CAPTAIN_REQUESTED,
        details={"team_name": team_name, "short_name": short_name},
    )
    return captain


async def approve_captain(admin_id: int, captain_id: int) -> tuple[sqlite3.Row, sqlite3.Row]:
    captain = await q.get_captain_by_id(captain_id)
    if captain is None:
        raise CaptainServiceError("Captain request not found.")
    if captain["status"] == CaptainStatus.APPROVED.value:
        raise CaptainServiceError("This captain is already approved.")

    await q.update_captain_status(captain_id, CaptainStatus.APPROVED.value)
    team = await q.create_team(
        name=captain["team_name"], short_name=captain["short_name"],
        captain_id=captain_id, captain_telegram_id=captain["telegram_id"],
    )
    await q.add_audit_log(
        admin_id, AuditAction.CAPTAIN_APPROVED, team_id=team["id"],
        details={"captain_id": captain_id},
    )
    await q.add_audit_log(
        admin_id, AuditAction.TEAM_CREATED, team_id=team["id"],
        details={"name": team["name"]},
    )
    logger.info("Captain %s approved, team '%s' created", captain["captain_name"], team["name"])
    return await q.get_captain_by_id(captain_id), team


async def reject_captain(admin_id: int, captain_id: int) -> sqlite3.Row:
    captain = await q.get_captain_by_id(captain_id)
    if captain is None:
        raise CaptainServiceError("Captain request not found.")
    await q.update_captain_status(captain_id, CaptainStatus.REJECTED.value)
    await q.add_audit_log(admin_id, AuditAction.CAPTAIN_REJECTED, details={"captain_id": captain_id})
    return await q.get_captain_by_id(captain_id)


async def disable_captain(admin_id: int, captain_id: int) -> sqlite3.Row:
    captain = await q.get_captain_by_id(captain_id)
    if captain is None:
        raise CaptainServiceError("Captain request not found.")
    await q.update_captain_status(captain_id, CaptainStatus.DISABLED.value)
    await q.add_audit_log(admin_id, AuditAction.CAPTAIN_DISABLED, details={"captain_id": captain_id})
    return await q.get_captain_by_id(captain_id)


async def delete_captain(admin_id: int, captain_id: int) -> dict:
    """
    Fully removes a captain who was registered by mistake (typically before
    the auction has started). This:
      - returns any players their team already bought back to the AVAILABLE
        pool with a purse refund (handled the same way as /returnplayer, in
        case this is used slightly later than intended)
      - deactivates their team so it stops appearing in /teams and can no
        longer bid
      - marks the captain request REJECTED so the person could re-register
        as a captain or a player afterwards if needed

    The captain/team rows themselves are kept (soft-delete), preserving full
    history and audit trail, consistent with the rest of the system.
    """
    captain = await q.get_captain_by_id(captain_id)
    if captain is None:
        raise CaptainServiceError("Captain request not found.")

    returned_players: list[sqlite3.Row] = []
    team = None
    if captain["team_id"] is not None:
        team = await q.get_team_by_id(captain["team_id"])
        if team is not None:
            squad = await q.get_team_squad(team["id"])
            for player in squad:
                if await q.get_retention_for_player(player["id"]) is not None:
                    # Retained players have no sale transaction; release them
                    # back to the pool directly (team is being deactivated, so
                    # no refund is needed).
                    await q.release_retention_tx(player["id"], refund=False)
                    returned_players.append(player)
                    continue
                tx = await q.get_sale_transaction_for_player(player["id"])
                if tx is None:
                    continue
                await q.reverse_sale_tx(
                    transaction_id=tx["id"], player_id=player["id"], team_id=team["id"],
                    price=tx["price"], tx_type="RETURN", new_player_status="AVAILABLE",
                )
                await q.add_to_reauction_queue(player["id"], reason="CAPTAIN_DELETED")
                returned_players.append(player)

            await q.deactivate_team(team["id"])

    await q.update_captain_status(captain_id, CaptainStatus.REJECTED.value)
    await q.add_audit_log(
        admin_id, AuditAction.CAPTAIN_DISABLED, team_id=team["id"] if team else None,
        details={
            "captain_id": captain_id, "deleted": True,
            "players_returned": len(returned_players),
        },
    )
    logger.info("Captain %s deleted by admin %s (%d players returned)",
                captain["captain_name"], admin_id, len(returned_players))

    return {
        "captain": await q.get_captain_by_id(captain_id),
        "team": team,
        "returned_players": returned_players,
    }


async def list_pending_captains():
    return await q.list_captains(status=CaptainStatus.PENDING.value)


async def list_all_captains():
    return await q.list_captains()


async def get_my_team(telegram_id: int) -> Optional[sqlite3.Row]:
    return await q.get_team_by_captain_or_vice(telegram_id)


async def set_vice_captain(captain_telegram_id: int, vice_telegram_id: Optional[int]) -> sqlite3.Row:
    """Only the primary captain can set/clear their own team's vice-captain."""
    team = await q.get_team_by_captain_telegram_id(captain_telegram_id)
    if team is None:
        raise CaptainServiceError("You do not currently own a team.")
    if vice_telegram_id == captain_telegram_id:
        raise CaptainServiceError("You can't set yourself as your own vice-captain.")
    await q.set_vice_captain(team["id"], vice_telegram_id)
    return await q.get_team_by_id(team["id"])


async def list_teams_with_squad():
    teams = await q.list_teams()
    result = []
    for t in teams:
        squad = await q.get_team_squad(t["id"])
        result.append({"team": t, "squad": squad})
    return result

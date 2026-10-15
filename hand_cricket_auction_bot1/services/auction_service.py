"""
services/auction_service.py
----------------------------
Core auction orchestration logic:
  - Building auction sets strictly by registration_no order (skipping
    REMOVED/SOLD/UNSOLD players), max N players per set (configurable).
  - Starting/advancing through a set, one player at a time.
  - Sold / Unsold finalization.
  - Undo last sale / return any sold player (with refund + audit trail).
  - Pause / resume.
  - Unsold-round auction.
  - Re-auction queue processing.

This module has NO direct Telegram dependencies (no bot/update objects) so it
can be unit-tested and reused by both private-chat and group-chat handlers.
Timer scheduling (JobQueue) glue lives in handlers/auction.py, which calls
into this service and uses the returned `deadline_ts` / auction row to
schedule its own countdown job.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from database import queries as q
from database.models import AuditAction, AuctionState, PlayerStatus
from utils.logger import get_logger
from utils.timers import new_deadline, seconds_remaining

logger = get_logger(__name__)


class AuctionServiceError(Exception):
    """Raised for any invalid auction operation with a user-facing message."""


# ---------------------------------------------------------------------------
# Set building
# ---------------------------------------------------------------------------

async def build_next_set(admin_id: int, set_number: int) -> sqlite3.Row:
    """
    Builds (or returns existing) auction set `set_number`.

    Sets are strictly sequential: set N must be built only after set N-1
    exists (we don't strictly require it to be COMPLETED, since the admin
    might re-check /set N, but we do enforce that sets aren't skipped so the
    registration-number windows stay contiguous).
    """
    existing = await q.get_auction_set_by_number(set_number)
    if existing is not None:
        return existing

    latest = await q.get_latest_set_number()
    if set_number != latest + 1:
        raise AuctionServiceError(
            f"Cannot create set #{set_number} yet. The next set to create is "
            f"#{latest + 1}. Sets must be started in order."
        )

    max_per_set = await q.get_setting_int("max_players_per_set")

    # Determine the starting registration_no window. We track this via the
    # highest registration_no already assigned to any previous set (whether
    # or not those players ended up sold/unsold/removed) so windows are
    # contiguous by registration number, exactly as required.
    if set_number == 1:
        window_start = 1
    else:
        prev_set = await q.get_auction_set_by_number(set_number - 1)
        prev_players = await q.get_set_players(prev_set["id"])
        if not prev_players:
            window_start = 1
        else:
            window_start = max(p["registration_no"] for p in prev_players) + 1

    candidates = await q.get_available_players_from(window_start, max_per_set)

    if not candidates:
        raise AuctionServiceError(
            "There are no available players to build this set. "
            "All remaining registered players may already be SOLD, UNSOLD, or REMOVED. "
            "If the main auction is complete, use /unsold to run the unsold round."
        )

    player_ids = [p["id"] for p in candidates]
    from_reg = candidates[0]["registration_no"]
    to_reg = candidates[-1]["registration_no"]

    auction_set = await q.create_auction_set(
        set_number=set_number, mode="REGULAR",
        from_reg_no=from_reg, to_reg_no=to_reg,
        player_ids_in_order=player_ids,
    )
    await q.add_audit_log(
        admin_id, AuditAction.SET_STARTED, details={
            "set_number": set_number, "from_reg_no": from_reg, "to_reg_no": to_reg,
            "player_count": len(player_ids),
        },
    )
    logger.info("Built set #%s: players #%s-#%s (%d players)",
                set_number, from_reg, to_reg, len(player_ids))
    return auction_set


async def get_set_summary(auction_set: sqlite3.Row) -> dict:
    players = await q.get_set_players(auction_set["id"])
    return {
        "set": auction_set,
        "players": players,
        "total": len(players),
    }


async def is_main_auction_complete() -> bool:
    """
    True once there are no more AVAILABLE players left to place into a new
    regular set (i.e. everything is SOLD, UNSOLD, or REMOVED).
    """
    remaining = await q.count_available_players()
    return remaining == 0


# ---------------------------------------------------------------------------
# Set progression: get current/next player to auction within an active set
# ---------------------------------------------------------------------------

async def get_next_player_in_set(set_id: int) -> Optional[sqlite3.Row]:
    return await q.get_next_unprocessed_set_player(set_id)


async def complete_set_if_finished(set_id: int) -> Optional[dict]:
    """
    If every player in the set has been processed, marks the set COMPLETED
    and returns a summary dict for display. Otherwise returns None.
    """
    next_player = await q.get_next_unprocessed_set_player(set_id)
    if next_player is not None:
        return None

    await q.update_auction_set_status(set_id, "COMPLETED", completed=True)
    progress = await q.get_set_progress(set_id)
    await q.add_audit_log(None, AuditAction.SET_COMPLETED, details=progress)
    return progress


# ---------------------------------------------------------------------------
# Starting a player's auction turn
# ---------------------------------------------------------------------------

async def start_player_auction(player: sqlite3.Row, set_id: Optional[int],
                                mode: str = "REGULAR") -> sqlite3.Row:
    if player["status"] not in (PlayerStatus.AVAILABLE.value, PlayerStatus.UNSOLD.value):
        raise AuctionServiceError(
            f"Player {player['first_name']} is not available for auction "
            f"(current status: {player['status']})."
        )

    timer_seconds = await q.get_setting_int("auction_timer")
    base_price = player["base_price"] or await q.get_setting_int("base_price")
    deadline = new_deadline(timer_seconds)

    auction = await q.create_auction(
        player_id=player["id"], set_id=set_id, mode=mode,
        base_price=base_price, timer_seconds=timer_seconds, deadline_ts=deadline,
    )
    await q.update_player_status(player["id"], PlayerStatus.IN_AUCTION)
    await q.update_auction_state(
        state=AuctionState.PLAYER_AUCTION.value,
        current_set_id=set_id, current_auction_id=auction["id"],
    )
    return auction


# ---------------------------------------------------------------------------
# Finalization: SOLD / UNSOLD
# ---------------------------------------------------------------------------

async def finalize_sold(auction: sqlite3.Row) -> dict:
    """
    Finalizes a SOLD auction. Prevents duplicate finalization by checking
    the auction's current status first.
    """
    if auction["status"] != "PLAYER_AUCTION":
        raise AuctionServiceError("This auction has already been finalized.")
    if auction["highest_team_id"] is None:
        raise AuctionServiceError("Cannot mark SOLD: there is no highest bidder.")

    team = await q.get_team_by_id(auction["highest_team_id"])
    if team is None:
        raise AuctionServiceError("Highest bidding team no longer exists.")

    await q.finalize_auction_sold_tx(
        auction_id=auction["id"], player_id=auction["player_id"],
        team_id=team["id"], price=auction["current_bid"], set_id=auction["set_id"],
    )
    await q.add_audit_log(
        None, AuditAction.PLAYER_SOLD, player_id=auction["player_id"], team_id=team["id"],
        details={"price": auction["current_bid"], "auction_id": auction["id"]},
    )
    await q.update_auction_state(
        state=AuctionState.PLAYER_SOLD.value, clear_auction=True,
    )

    player = await q.get_player_by_id(auction["player_id"])
    updated_team = await q.get_team_by_id(team["id"])
    logger.info("SOLD: %s -> %s for %s", player["first_name"], team["name"],
                auction["current_bid"])
    return {"player": player, "team": updated_team, "price": auction["current_bid"]}


async def finalize_unsold(auction: sqlite3.Row) -> dict:
    if auction["status"] != "PLAYER_AUCTION":
        raise AuctionServiceError("This auction has already been finalized.")
    if auction["highest_team_id"] is not None:
        raise AuctionServiceError("Cannot mark UNSOLD: bids exist. Use SOLD instead.")

    await q.finalize_auction_unsold_tx(
        auction_id=auction["id"], player_id=auction["player_id"], set_id=auction["set_id"],
    )
    await q.add_audit_log(
        None, AuditAction.PLAYER_UNSOLD, player_id=auction["player_id"],
        details={"auction_id": auction["id"]},
    )
    await q.update_auction_state(
        state=AuctionState.PLAYER_UNSOLD.value, clear_auction=True,
    )

    player = await q.get_player_by_id(auction["player_id"])
    logger.info("UNSOLD: %s", player["first_name"])
    return {"player": player}


async def finalize_current_auction_by_timer(auction: sqlite3.Row) -> dict:
    """
    Called when the timer expires. Decides SOLD vs UNSOLD automatically
    based on whether a highest bidder exists.
    """
    if auction["highest_team_id"] is not None:
        result = await finalize_sold(auction)
        result["outcome"] = "SOLD"
        return result
    else:
        result = await finalize_unsold(auction)
        result["outcome"] = "UNSOLD"
        return result


# ---------------------------------------------------------------------------
# Pause / Resume
# ---------------------------------------------------------------------------

async def pause_current_auction(admin_id: int) -> sqlite3.Row:
    auction = await q.get_active_auction()
    if auction is None:
        raise AuctionServiceError("There is no active auction to pause.")
    if auction["status"] != "PLAYER_AUCTION":
        raise AuctionServiceError("The auction is not currently running.")

    remaining = seconds_remaining(auction["deadline_ts"])
    await q.pause_auction(auction["id"], remaining)
    await q.update_auction_state(state=AuctionState.PAUSED.value)
    await q.add_audit_log(admin_id, AuditAction.AUCTION_PAUSED,
                           player_id=auction["player_id"],
                           details={"remaining_seconds": remaining})
    return await q.get_auction_by_id(auction["id"])


async def resume_current_auction(admin_id: int) -> sqlite3.Row:
    auction = await q.fetch_paused_auction()
    if auction is None:
        raise AuctionServiceError("There is no paused auction to resume.")

    remaining = auction["remaining_at_pause"] or 0
    if remaining <= 0:
        remaining = 1  # avoid instantly re-expiring; give at least 1s grace
    deadline = new_deadline(remaining)
    await q.resume_auction(auction["id"], deadline)
    await q.update_auction_state(state=AuctionState.PLAYER_AUCTION.value)
    await q.add_audit_log(admin_id, AuditAction.AUCTION_RESUMED,
                           player_id=auction["player_id"],
                           details={"remaining_seconds": remaining})
    return await q.get_auction_by_id(auction["id"])


# ---------------------------------------------------------------------------
# Undo last sale / Return any sold player
# ---------------------------------------------------------------------------

async def undo_last_sale(admin_id: int) -> dict:
    tx = await q.get_last_sale_transaction()
    if tx is None:
        raise AuctionServiceError("There is no recent sale to undo.")
    return await _reverse_transaction(admin_id, tx, tx_type="UNDO")


async def return_sold_player(admin_id: int, player_id: int) -> dict:
    player = await q.get_player_by_id(player_id)
    if player is None:
        raise AuctionServiceError("Player not found.")
    if player["status"] != PlayerStatus.SOLD.value:
        raise AuctionServiceError(f"{player['first_name']} is not currently SOLD.")

    tx = await q.get_sale_transaction_for_player(player_id)
    if tx is None:
        raise AuctionServiceError("No sale record found for this player.")
    return await _reverse_transaction(admin_id, tx, tx_type="RETURN")


async def trade_player(admin_id: int, player_id: int, to_team_id: int) -> dict:
    """
    Trades a SOLD player from their current team to another team. No purse
    is charged or refunded — this is a straight swap of ownership, not a
    new sale. Squad-size limits on the receiving team are enforced.
    """
    player = await q.get_player_by_id(player_id)
    if player is None:
        raise AuctionServiceError("Player not found.")
    if player["status"] != PlayerStatus.SOLD.value:
        raise AuctionServiceError(f"{player['first_name']} is not currently SOLD, so can't be traded.")

    from_team_id = player["team_id"]
    if from_team_id is None:
        raise AuctionServiceError("This player has no current team on record.")
    if from_team_id == to_team_id:
        raise AuctionServiceError("Player is already on that team.")

    to_team = await q.get_team_by_id(to_team_id)
    if to_team is None:
        raise AuctionServiceError("Destination team not found.")
    if not to_team["is_active"]:
        raise AuctionServiceError(f"{to_team['name']} is not an active team.")

    current_squad_size = await q.get_team_squad_size(to_team_id)
    if current_squad_size >= to_team["max_squad_size"]:
        raise AuctionServiceError(
            f"{to_team['name']} already has a full squad "
            f"({current_squad_size}/{to_team['max_squad_size']})."
        )

    from_team = await q.get_team_by_id(from_team_id)

    try:
        await q.trade_player_tx(player_id, from_team_id, to_team_id, admin_id)
    except ValueError as e:
        raise AuctionServiceError(str(e)) from e

    logger.info("Trade: %s moved from %s to %s", player["first_name"],
                from_team["name"] if from_team else "?", to_team["name"])
    return {
        "player": await q.get_player_by_id(player_id),
        "from_team": from_team,
        "to_team": to_team,
    }


async def _reverse_transaction(admin_id: int, tx: sqlite3.Row, tx_type: str) -> dict:
    player = await q.get_player_by_id(tx["player_id"])
    team = await q.get_team_by_id(tx["team_id"]) if tx["team_id"] else None
    if player is None or team is None:
        raise AuctionServiceError("Could not locate the player or team for this sale.")

    # Returned/undone players go back to AVAILABLE and into the re-auction
    # queue so they don't silently reappear mid-window in a future regular
    # set and don't disturb original registration ordering.
    new_status = PlayerStatus.AVAILABLE.value

    await q.reverse_sale_tx(
        transaction_id=tx["id"], player_id=player["id"], team_id=team["id"],
        price=tx["price"], tx_type=tx_type, new_player_status=new_status,
    )
    await q.add_to_reauction_queue(player["id"], reason=tx_type)

    action = AuditAction.SALE_UNDONE if tx_type == "UNDO" else AuditAction.PLAYER_RETURNED
    await q.add_audit_log(
        admin_id, action, player_id=player["id"], team_id=team["id"],
        details={"price": tx["price"], "tx_type": tx_type},
    )

    updated_team = await q.get_team_by_id(team["id"])
    logger.info("%s: %s removed from %s, refunded %s", tx_type, player["first_name"],
                team["name"], tx["price"])
    return {
        "player": await q.get_player_by_id(player["id"]),
        "team": updated_team,
        "price": tx["price"],
    }


# ---------------------------------------------------------------------------
# Skip / next player (manual admin override)
# ---------------------------------------------------------------------------

async def skip_current_player(admin_id: int, outcome: str) -> dict:
    """
    outcome: "UNSOLD" (finalize as unsold, no bids), "RETURN" (send back to
    the general AVAILABLE pool, outside this set), or "DEFER" (skip and try
    again later in the SAME set — "skip and return later").
    """
    auction = await q.get_active_auction()
    if auction is None:
        raise AuctionServiceError("There is no active auction to skip.")

    player = await q.get_player_by_id(auction["player_id"])
    await q.add_audit_log(
        admin_id, AuditAction.PLAYER_SKIPPED, player_id=player["id"],
        details={"outcome": outcome},
    )

    if outcome == "UNSOLD" and auction["highest_team_id"] is None:
        result = await finalize_unsold(auction)
        return {"outcome": "UNSOLD", **result}

    if outcome == "DEFER":
        if auction["set_id"] is None:
            raise AuctionServiceError(
                "This player isn't part of a regular set, so they can't be "
                "deferred — use RETURN or UNSOLD instead."
            )
        await q.update_player_status(player["id"], PlayerStatus.AVAILABLE)
        await q.push_set_player_to_end(auction["set_id"], player["id"])
        # Cancel this auction turn without recording SOLD/UNSOLD.
        await q.update_auction_state(state=AuctionState.SET_READY.value, clear_auction=True)
        return {"outcome": "DEFERRED", "player": await q.get_player_by_id(player["id"])}

    # RETURN TO POOL: cancel this auction turn without recording a sale,
    # and remove them from this set's queue entirely (they'll only come
    # back via a future set/re-auction, not later in this same set).
    await q.update_player_status(player["id"], PlayerStatus.AVAILABLE)
    if auction["set_id"] is not None:
        await q.mark_set_player_processed(auction["set_id"], player["id"], "UNSOLD")
    await q.update_auction_state(state=AuctionState.SET_READY.value, clear_auction=True)
    return {"outcome": "RETURNED", "player": await q.get_player_by_id(player["id"])}


# ---------------------------------------------------------------------------
# Unsold round
# ---------------------------------------------------------------------------

async def get_next_unsold_player() -> Optional[sqlite3.Row]:
    unsold = await q.list_unsold_players()
    return unsold[0] if unsold else None


async def start_unsold_auction_state(admin_id: int) -> None:
    await q.update_auction_state(state=AuctionState.UNSOLD_ROUND.value)
    await q.add_audit_log(admin_id, AuditAction.AUCTION_STARTED,
                           details={"phase": "UNSOLD_ROUND"})


# ---------------------------------------------------------------------------
# Re-auction queue
# ---------------------------------------------------------------------------

async def get_reauction_queue():
    return await q.list_reauction_queue()


async def start_reauction_for_player(admin_id: int, player_id: int) -> sqlite3.Row:
    player = await q.get_player_by_id(player_id)
    if player is None:
        raise AuctionServiceError("Player not found.")
    if player["status"] != PlayerStatus.AVAILABLE.value:
        raise AuctionServiceError(
            f"{player['first_name']} is not in AVAILABLE status (currently {player['status']})."
        )

    auction = await start_player_auction(player, set_id=None, mode="REAUCTION")
    await q.mark_reauction_processed(player_id)
    return auction


# ---------------------------------------------------------------------------
# Overall completion check + report
# ---------------------------------------------------------------------------

async def check_main_auction_completed(admin_id: Optional[int] = None) -> bool:
    complete = await is_main_auction_complete()
    if complete:
        await q.update_auction_state(state=AuctionState.MAIN_AUCTION_COMPLETED.value)
        await q.add_audit_log(admin_id, AuditAction.AUCTION_COMPLETED,
                               details={"phase": "MAIN_AUCTION"})
    return complete


async def get_auction_status_summary() -> dict:
    state = await q.get_auction_state()
    counts = await q.count_players_by_status()
    current_auction = None
    current_player = None
    current_set = None
    bids = []

    if state["current_auction_id"]:
        current_auction = await q.get_auction_by_id(state["current_auction_id"])
        if current_auction:
            current_player = await q.get_player_by_id(current_auction["player_id"])
            bids = await q.get_auction_bids(current_auction["id"])
    if state["current_set_id"]:
        current_set = await q.get_auction_set_by_id(state["current_set_id"])

    return {
        "state": state,
        "counts": counts,
        "auction": current_auction,
        "player": current_player,
        "auction_set": current_set,
        "bids": bids,
    }

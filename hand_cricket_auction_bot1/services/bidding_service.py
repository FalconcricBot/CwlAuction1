"""
services/bidding_service.py
----------------------------
Validates and places bids. Every rule from the spec is checked explicitly,
in order, with a clear user-facing error message on failure:

  1. Auction is active (status == PLAYER_AUCTION, not paused/ended)
  2. Player is available (still IN_AUCTION, not already sold/unsold)
  3. Captain is APPROVED
  4. Captain owns a team
  5. Team is not admin-blocked from bidding
  6. Team has enough purse
  7. Bid is strictly higher than the current bid (no fixed increment —
     any amount above the current bid is valid, same as a real auction)
  8. Player is not already sold
  9. Timer has not expired
  10. Captain/team is not disabled
  11. The same team cannot bid twice in a row — once you're the highest
      bidder, another team must outbid you before you can bid again

After a successful bid, if the team's remaining purse has crossed below
the configured low-purse alert threshold, a one-time DM warning is sent to
that team's captain (see `_maybe_alert_low_purse`).
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from database import queries as q
from database.models import AuditAction, CaptainStatus
from utils.helpers import next_valid_bid
from utils.logger import get_logger
from utils.timers import is_expired, new_deadline

logger = get_logger(__name__)


class BiddingError(Exception):
    """User-facing bidding validation error."""


async def get_bidding_team(telegram_id: int) -> sqlite3.Row:
    """
    Resolves the team a user is allowed to bid for — either as the
    approved primary captain, or as that team's vice-captain (who bids on
    the team's behalf without needing a separate captain approval).
    Raises BiddingError with a clear message on any failure.
    """
    # Vice-captains bid for a team without being an approved captain
    # themselves, so check that path first as a fast, independent route.
    team_as_vice = await q.get_team_by_captain_or_vice(telegram_id)
    if team_as_vice is not None and team_as_vice["vice_captain_telegram_id"] == telegram_id:
        if not team_as_vice["is_active"]:
            raise BiddingError("❌ Your team has been disabled.")
        if team_as_vice["bid_blocked"]:
            raise BiddingError("❌ Your team has been temporarily blocked from bidding by an admin.")
        return team_as_vice

    captain = await q.get_captain_by_telegram_id(telegram_id)
    if captain is None:
        raise BiddingError("❌ You are not an approved captain.")
    if captain["status"] == CaptainStatus.REJECTED.value:
        raise BiddingError("❌ Your captain request was rejected.")
    if captain["status"] == CaptainStatus.DISABLED.value:
        raise BiddingError("❌ Your captain account has been disabled.")
    if captain["status"] == CaptainStatus.PENDING.value:
        raise BiddingError("❌ Your captain request is still pending admin approval.")
    if captain["status"] != CaptainStatus.APPROVED.value:
        raise BiddingError("❌ You are not an approved captain.")

    team = await q.get_team_by_captain_telegram_id(telegram_id)
    if team is None:
        raise BiddingError("❌ You do not currently own a team.")
    if not team["is_active"]:
        raise BiddingError("❌ Your team has been disabled.")
    if team["bid_blocked"]:
        raise BiddingError("❌ Your team has been temporarily blocked from bidding by an admin.")
    return team


async def validate_and_place_bid(telegram_id: int, requested_amount: Optional[int],
                                  all_in: bool = False) -> dict:
    """
    Validates every rule and, if valid, places the bid atomically.
    `requested_amount` is ignored if all_in=True (uses full remaining purse).
    Returns a dict with the updated auction + team for display.
    """
    team = await get_bidding_team(telegram_id)

    auction = await q.get_active_auction()
    if auction is None or auction["status"] != "PLAYER_AUCTION":
        raise BiddingError("❌ Auction has ended.")

    if is_expired(auction["deadline_ts"]):
        raise BiddingError("❌ Auction has ended.")

    player = await q.get_player_by_id(auction["player_id"])
    if player is None or player["status"] != "IN_AUCTION":
        raise BiddingError("❌ This player is no longer available for bidding.")

    # A team that is already the highest bidder cannot bid again until
    # another team outbids them — same as a real live auction, you can't
    # bid against yourself back-to-back.
    if auction["highest_team_id"] == team["id"]:
        raise BiddingError(
            "❌ You're already the highest bidder. Wait for another team to bid before raising again."
        )

    min_valid = next_valid_bid(auction["current_bid"], auction["base_price"])

    if all_in:
        amount = team["remaining_purse"]
    else:
        if requested_amount is None:
            raise BiddingError("❌ Invalid bid amount.")
        amount = requested_amount

    if amount > team["remaining_purse"]:
        raise BiddingError(
            f"❌ Insufficient purse. You have ₹{team['remaining_purse']:,} remaining."
        )

    if amount < min_valid:
        raise BiddingError(f"❌ Your bid must be higher than ₹{min_valid - 1:,}.")

    timer_seconds = await q.get_setting_int("auction_timer")
    new_deadline_ts = new_deadline(timer_seconds)

    await q.place_bid_tx(auction["id"], team["id"], amount, new_deadline_ts)
    await q.add_audit_log(
        telegram_id, AuditAction.BID_PLACED, player_id=player["id"], team_id=team["id"],
        details={"amount": amount, "all_in": all_in},
    )

    updated_auction = await q.get_auction_by_id(auction["id"])
    updated_team = await q.get_team_by_id(team["id"])
    logger.info("Bid placed: team=%s amount=%s player=%s", team["name"], amount,
                player["first_name"])
    return {
        "auction": updated_auction,
        "team": updated_team,
        "amount": amount,
        "deadline_ts": new_deadline_ts,
    }


async def maybe_alert_low_purse(context, team: sqlite3.Row) -> None:
    """
    Sends a one-time DM to a team's captain once their remaining purse
    drops at or below the configured alert threshold (percentage of their
    starting purse). Never raises — a failed DM should never break bidding.
    """
    if team["low_purse_alerted"]:
        return

    percent = await q.get_setting_int("low_purse_alert_percent")
    if percent <= 0:
        return

    threshold = team["starting_purse"] * percent / 100
    if team["remaining_purse"] > threshold:
        return

    await q.mark_low_purse_alerted(team["id"])
    try:
        from utils.helpers import fmt_money
        await context.bot.send_message(
            team["captain_telegram_id"],
            "⚠️ <b>LOW PURSE WARNING</b>\n\n"
            f"Your team <b>{team['name']}</b> has {fmt_money(team['remaining_purse'])} "
            f"remaining — that's {percent}% or less of your starting purse "
            f"({fmt_money(team['starting_purse'])}). Bid carefully!",
            parse_mode="HTML",
        )
    except Exception:
        logger.warning("Could not send low-purse alert to captain %s", team["captain_telegram_id"])

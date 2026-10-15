"""
handlers/auction.py
--------------------
Telegram glue for the auction flow: /set, starting a set, showing the live
auction card, timer via JobQueue, /pause /resume /next /undo /returnplayer,
/unsold, /unsold_list, /auction_status, /sold, /auctionhistory.

All business rules live in services/auction_service.py and
services/bidding_service.py; this module is responsible only for:
  - Telegram command/callback wiring
  - Rendering messages/keyboards
  - Scheduling/cancelling the countdown timer job
  - Enforcing that only admins can run admin-only commands
"""
from __future__ import annotations

import math

from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import BadRequest

from database import queries as q
from keyboards.auction import (
    lobby_keyboard, reauction_queue_keyboard,
    return_player_keyboard, skip_confirm_keyboard, skip_outcome_keyboard,
    start_set_keyboard, trade_confirm_keyboard, undo_confirm_keyboard, unsold_start_keyboard,
)
from services import auction_service, bidding_service, lobby_service
from utils.helpers import fmt_money, is_admin, player_display_name
from utils.logger import get_logger
from utils.media import send_announcement
from utils.timers import seconds_remaining

logger = get_logger(__name__)

TIMER_JOB_NAME = "auction_timer_tick"


async def _require_admin(update: Update) -> bool:
    if is_admin(update.effective_user.id):
        return True
    from utils.helpers import is_admin_async
    return await is_admin_async(update.effective_user.id)


async def _safe_reply(update: Update, text: str, **kwargs) -> None:
    """
    reply_text wrapper that swallows Telegram API errors (flood control,
    network blips) so a single failed notification never crashes a handler.
    Used for high-frequency paths like bidding where many messages can be
    sent in quick succession during active auctions.
    """
    from telegram.error import TelegramError
    try:
        await update.effective_message.reply_text(text, **kwargs)
    except TelegramError as e:
        logger.warning("Failed to send reply (%s): %s", text[:50], e)


async def _deny_admin(update: Update) -> None:
    """
    Silently ignore non-admin attempts at admin-only commands/callbacks.
    Admin commands should not be discoverable or usable by regular users.
    """
    if update.callback_query:
        await update.callback_query.answer()
    return


# ---------------------------------------------------------------------------
# /set <n>
# ---------------------------------------------------------------------------

async def set_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny_admin(update)
        return

    if not context.args:
        await update.effective_message.reply_text("Usage: /set <number>  e.g. /set 1")
        return
    try:
        set_number = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Set number must be an integer.")
        return
    if set_number < 1:
        await update.effective_message.reply_text("❌ Set number must be 1 or higher.")
        return

    try:
        auction_set = await auction_service.build_next_set(update.effective_user.id, set_number)
    except auction_service.AuctionServiceError as e:
        await update.effective_message.reply_text(f"❌ {e}")
        return

    summary = await auction_service.get_set_summary(auction_set)
    players = summary["players"]

    lines = [f"🏏 <b>AUCTION SET {set_number}</b>", "━━━━━━━━━━━━━━━━━━━━", "", "Players:", ""]
    for p in players:
        name = player_display_name(p["first_name"], p["last_name"])
        lines.append(f"#{p['registration_no']} {name}")
    lines.append("")
    lines.append(f"Total Players:\n<b>{len(players)}</b>")

    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=start_set_keyboard(set_number),
    )


async def set_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await _require_admin(update):
        await query.answer()
        return
    await query.answer()

    set_number = int(query.data.split(":")[1])
    auction_set = await q.get_auction_set_by_number(set_number)
    if auction_set is None:
        await query.edit_message_text("❌ Set not found.")
        return

    await q.update_auction_set_status(auction_set["id"], "ACTIVE")
    await q.update_auction_state(current_set_id=auction_set["id"], chat_id=query.message.chat_id)

    await query.edit_message_text(
        f"🏏 <b>SET {set_number} STARTED</b>\n\nBidding will begin shortly…",
        parse_mode="HTML",
    )
    await advance_to_next_player(context, chat_id=query.message.chat_id, set_id=auction_set["id"])


# ---------------------------------------------------------------------------
# /showset <number> - detailed player-by-player card view of a set's pool
# ---------------------------------------------------------------------------

_STATUS_ICON = {
    "AVAILABLE": "🟢", "REGISTERED": "🟡", "IN_AUCTION": "🔨",
    "SOLD": "✅", "UNSOLD": "🔴", "REMOVED": "🚫",
}


async def showset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Usage: /showset <number>  e.g. /showset 5")
        return
    try:
        set_number = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Set number must be an integer.")
        return

    fetching_msg = await update.effective_message.reply_text(f"📊 Fetching set {set_number}…")

    auction_set = await q.get_auction_set_by_number(set_number)
    if auction_set is None:
        await fetching_msg.edit_text(
            f"❌ Set {set_number} does not exist yet.\n\n"
            f"Use <code>/set {set_number}</code> first to build it, then "
            f"/showset {set_number} to view the full player list.",
            parse_mode="HTML",
        )
        return

    players = await q.get_set_players(auction_set["id"])
    if not players:
        await fetching_msg.edit_text(f"❌ Set {set_number} has no players.")
        return

    blocks = ["🗂️ <b>SET OVERVIEW — SET {0}</b>".format(set_number), "═" * 22, ""]
    for p in players:
        name = player_display_name(p["first_name"], p["last_name"])
        username_line = f"@{p['username']}" if p["username"] else "<i>none</i>"
        icon = _STATUS_ICON.get(p["player_status"], "⚪")
        block = (
            f"🎫 <b>#{p['registration_no']}</b> — {name}\n"
            f"   ↳ Handle: {username_line}\n"
            f"   ↳ Telegram ID: <code>{p['telegram_id']}</code>\n"
            f"   ↳ State: {icon} {p['player_status']}\n"
            f"{'┈' * 18}"
        )
        blocks.append(block)

    blocks.append("")
    blocks.append(f"📦 Set {set_number} • {len(players)} player(s) total")

    text = "\n".join(blocks)
    if len(text) <= 4096:
        await fetching_msg.edit_text(text, parse_mode="HTML")
        return

    # Telegram's 4096-char cap: send the header via edit, then chunk the rest.
    await fetching_msg.edit_text(blocks[0] + "\n" + ("═" * 22), parse_mode="HTML")
    chunk = ""
    for block in blocks[2:]:
        if len(chunk) + len(block) + 1 > 3800:
            await update.effective_message.reply_text(chunk, parse_mode="HTML")
            chunk = ""
        chunk += block + "\n"
    if chunk.strip():
        await update.effective_message.reply_text(chunk, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Rendering the live auction card
# ---------------------------------------------------------------------------

async def _render_auction_text(auction, player, bids) -> str:
    remaining = max(0, math.ceil(seconds_remaining(auction["deadline_ts"])))
    highest_team_name = "—"
    if auction["highest_team_id"]:
        team = await q.get_team_by_id(auction["highest_team_id"])
        highest_team_name = team["name"] if team else "—"

    from utils.helpers import next_valid_bid
    min_bid = next_valid_bid(auction["current_bid"], auction["base_price"])

    name = player_display_name(player["first_name"], player["last_name"])
    lines = [
        "🔨 <b>PLAYER AUCTION</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"👤 Player: <b>{name}</b>",
        f"🔢 Registration: #{player['registration_no']}",
        "",
        f"💰 Base Price: {fmt_money(auction['base_price'])}",
        "",
        f"💵 Current Bid: {fmt_money(auction['current_bid'])}",
        f"👑 Highest Bidder: {highest_team_name}",
        "",
        f"⏱️ Time Left: {remaining} seconds",
        "",
        f"Captains: place a bid with <code>/bid {min_bid}</code> (or higher), "
        "or go all-in with /allin.",
    ]
    return "\n".join(lines)


async def _render_and_send_auction_card(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                                          auction) -> None:
    player = await q.get_player_by_id(auction["player_id"])
    bids = await q.get_auction_bids(auction["id"])
    text = await _render_auction_text(auction, player, bids)

    msg = await context.bot.send_message(chat_id, text, parse_mode="HTML")
    await q.update_auction_state(chat_id=chat_id, message_id=msg.message_id)
    _schedule_timer(context, chat_id, auction["id"])


async def _update_auction_card(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                                message_id: int, auction) -> bool:
    """
    Edits the live auction card in place. Returns True if the edit actually
    went through (used by the timer loop to track when it last updated the
    display). Any Telegram API error (flood control, message not found,
    etc.) is swallowed here — a missed countdown refresh is harmless, and we
    never want a transient API error to crash the timer job or the bot.
    """
    from telegram.error import RetryAfter, TelegramError

    player = await q.get_player_by_id(auction["player_id"])
    text = await _render_auction_text(auction, player, [])
    try:
        await context.bot.edit_message_text(
            text, chat_id=chat_id, message_id=message_id, parse_mode="HTML",
        )
        return True
    except RetryAfter as e:
        logger.warning("Flood control hit while updating auction card; backing off %.1fs",
                        e.retry_after)
        return False
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            return True
        logger.warning("Failed to edit auction card: %s", e)
        return False
    except TelegramError as e:
        logger.warning("Telegram error while editing auction card: %s", e)
        return False


# ---------------------------------------------------------------------------
# Timer scheduling (JobQueue-driven countdown)
# ---------------------------------------------------------------------------

def _schedule_timer(context: ContextTypes.DEFAULT_TYPE, chat_id: int, auction_id: int) -> None:
    # Remove any existing timer job for this chat before scheduling a new one.
    _cancel_timer(context, chat_id)
    context.job_queue.run_repeating(
        _timer_tick, interval=1.0, first=1.0,
        chat_id=chat_id, name=f"{TIMER_JOB_NAME}:{chat_id}",
        data={"auction_id": auction_id, "last_edit_second": None},
    )


def _cancel_timer(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    jobs = context.job_queue.get_jobs_by_name(f"{TIMER_JOB_NAME}:{chat_id}")
    for job in jobs:
        job.schedule_removal()


# Telegram allows roughly 1 edit/sec per chat before flooding; editing every
# tick (once per second) reliably triggers RetryAfter under any real load.
# We only push a visible edit every CARD_EDIT_INTERVAL seconds (still
# checking expiry every second so finalization stays prompt), plus once
# more in the final few seconds so the countdown feels responsive as it
# nears zero.
CARD_EDIT_INTERVAL = 4


async def _timer_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    chat_id = job.chat_id
    auction_id = job.data["auction_id"]

    auction = await q.get_auction_by_id(auction_id)
    if auction is None or auction["status"] != "PLAYER_AUCTION":
        job.schedule_removal()
        return

    remaining = seconds_remaining(auction["deadline_ts"])
    if remaining > 0:
        remaining_int = int(remaining)

        # One-time "final countdown" warning at 5 seconds left, so bidders
        # get a clear nudge instead of only relying on the edited card.
        if remaining_int <= 5 and not job.data.get("warned_5s"):
            job.data["warned_5s"] = True
            player = await q.get_player_by_id(auction["player_id"])
            name = player_display_name(player["first_name"], player["last_name"])
            await context.bot.send_message(
                chat_id, f"⏰ <b>5 seconds left</b> for {name}! Last chance to bid.",
                parse_mode="HTML",
            )

        last_edit = job.data.get("last_edit_second")
        should_edit = (
            last_edit is None
            or last_edit - remaining_int >= CARD_EDIT_INTERVAL
            or remaining_int <= 5  # tighter updates in the final countdown
        )
        if should_edit:
            state = await q.get_auction_state()
            if state["message_id"]:
                edited = await _update_auction_card(context, chat_id, state["message_id"], auction)
                if edited:
                    job.data["last_edit_second"] = remaining_int
        return

    # Timer expired: finalize.
    job.schedule_removal()
    await _finalize_and_advance(context, chat_id, auction)


async def _finalize_and_advance(context: ContextTypes.DEFAULT_TYPE, chat_id: int, auction) -> None:
    try:
        result = await auction_service.finalize_current_auction_by_timer(auction)
    except auction_service.AuctionServiceError as e:
        logger.warning("Finalize error (likely already finalized): %s", e)
        return

    if result["outcome"] == "SOLD":
        await _send_sold_message(context, chat_id, result)
    else:
        await _send_unsold_message(context, chat_id, result)

    set_id = auction["set_id"]
    if set_id is not None:
        completed = await auction_service.complete_set_if_finished(set_id)
        if completed is not None:
            await _send_set_completed_message(context, chat_id, auction["set_id"], completed)
            await _maybe_announce_main_auction_completed(context, chat_id)
            return

    # Do NOT auto-advance. The admin must explicitly run /next or
    # /nextplayer to bring the next player onto the auction block.
    await context.bot.send_message(
        chat_id,
        "👉 <b>Admin, bring the next player up with</b> <code>/nextplayer</code>",
        parse_mode="HTML",
    )


async def _send_sold_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, result: dict) -> None:
    player, team, price = result["player"], result["team"], result["price"]
    name = player_display_name(player["first_name"], player["last_name"])
    text = (
        "🔨 <b>SOLD!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 {name}\n\n"
        f"🏳️ Team:\n{team['name']}\n\n"
        f"💰 Sold Price:\n{fmt_money(price)}\n\n"
        f"💰 Remaining Purse:\n{fmt_money(team['remaining_purse'])}"
    )
    if not await send_announcement(context, chat_id, "sold", text):
        await context.bot.send_message(chat_id, text, parse_mode="HTML")


async def _send_unsold_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, result: dict) -> None:
    player = result["player"]
    name = player_display_name(player["first_name"], player["last_name"])
    text = (
        "❌ <b>UNSOLD</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 {name}\n\n"
        f"💰 Base Price:\n{fmt_money(player['base_price'])}\n\n"
        "No valid bids received."
    )
    if not await send_announcement(context, chat_id, "unsold", text):
        await context.bot.send_message(chat_id, text, parse_mode="HTML")


async def _send_set_completed_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                                       set_id: int, progress: dict) -> None:
    auction_set = await q.get_auction_set_by_id(set_id)
    next_set_number = auction_set["set_number"] + 1
    text = (
        f"✅ <b>SET {auction_set['set_number']} COMPLETED</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Total Players: {progress['total']}\n"
        f"✅ Sold: {progress['SOLD']}\n"
        f"❌ Unsold: {progress['UNSOLD']}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👉 <b>Admin, start the next set with:</b>\n"
        f"<code>/set {next_set_number}</code>"
    )
    await context.bot.send_message(chat_id, text, parse_mode="HTML")


async def _maybe_announce_main_auction_completed(context: ContextTypes.DEFAULT_TYPE,
                                                   chat_id: int) -> None:
    complete = await auction_service.check_main_auction_completed()
    if not complete:
        return
    counts = await q.count_players_by_status()
    total = sum(counts.values())
    text = (
        "🎉 <b>MAIN AUCTION COMPLETED</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Total Players: {total}\n\n"
        f"✅ Sold: {counts.get('SOLD', 0)}\n"
        f"❌ Unsold: {counts.get('UNSOLD', 0)}\n"
        f"🚫 Removed: {counts.get('REMOVED', 0)}\n\n"
        "All regular sets are completed.\n"
        "UNSOLD ROUND IS NOW READY.\n\n"
        "Admin can use:\n👉 /unsold"
    )
    await context.bot.send_message(chat_id, text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Advancing to the next player within a set
# ---------------------------------------------------------------------------

async def advance_to_next_player(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                                  set_id: int | None) -> None:
    if set_id is None:
        return
    next_entry = await auction_service.get_next_player_in_set(set_id)
    if next_entry is None:
        completed = await auction_service.complete_set_if_finished(set_id)
        if completed is not None:
            await _send_set_completed_message(context, chat_id, set_id, completed)
            await _maybe_announce_main_auction_completed(context, chat_id)
        return

    player = await q.get_player_by_id(next_entry["player_id"])
    try:
        auction = await auction_service.start_player_auction(player, set_id=set_id, mode="REGULAR")
    except auction_service.AuctionServiceError as e:
        logger.error("Failed to start auction for player %s: %s", player["id"], e)
        return
    await q.update_auction_state(chat_id=chat_id)
    await _render_and_send_auction_card(context, chat_id, auction)


# ---------------------------------------------------------------------------
# Bidding via commands: /bid <amount> and /allin
# ---------------------------------------------------------------------------

async def bid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Captains place bids with /bid <amount>, e.g. /bid 1500.
    Only usable in the configured auction group chat (or anywhere if no
    group is configured) while a player is actively on the block.
    """
    user = update.effective_user

    if not context.args:
        await _safe_reply(update, "Usage: /bid <amount>  e.g. /bid 1500")
        return

    amount = None
    try:
        amount = int(context.args[0].replace(",", "").replace("₹", ""))
    except ValueError:
        await _safe_reply(update, "❌ Invalid bid amount. Usage: /bid <amount>")
        return

    if amount <= 0:
        await _safe_reply(update, "❌ Bid amount must be positive.")
        return

    auction = await q.get_active_auction()
    if auction is None:
        await _safe_reply(update, "❌ There is no active auction right now.")
        return

    try:
        result = await bidding_service.validate_and_place_bid(user.id, amount)
    except bidding_service.BiddingError as e:
        await _safe_reply(update, str(e))
        return

    team = result["team"]
    await _safe_reply(update, f"✅ Bid placed by {team['name']}: {fmt_money(result['amount'])}")

    state = await q.get_auction_state()
    if state["message_id"]:
        await _update_auction_card(context, update.effective_chat.id, state["message_id"],
                                    result["auction"])
    _schedule_timer(context, update.effective_chat.id, auction["id"])
    await bidding_service.maybe_alert_low_purse(context, team)


async def allin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /allin asks for confirmation first (shows the full remaining purse),
    then the captain confirms with /allin confirm.
    """
    user = update.effective_user

    auction = await q.get_active_auction()
    if auction is None:
        await _safe_reply(update, "❌ There is no active auction right now.")
        return

    try:
        team = await bidding_service.get_bidding_team(user.id)
    except bidding_service.BiddingError as e:
        await _safe_reply(update, str(e))
        return

    confirming = bool(context.args) and context.args[0].lower() in ("confirm", "yes", "y")

    if not confirming:
        await _safe_reply(
            update,
            "⚠️ <b>ALL IN CONFIRMATION</b>\n\n"
            f"Your entire remaining purse:\n{fmt_money(team['remaining_purse'])}\n\n"
            f"Bid:\n{fmt_money(team['remaining_purse'])}\n\n"
            "Type <code>/allin confirm</code> to place this bid, or ignore to cancel.",
            parse_mode="HTML",
        )
        return

    try:
        result = await bidding_service.validate_and_place_bid(user.id, None, all_in=True)
    except bidding_service.BiddingError as e:
        await _safe_reply(update, str(e))
        return

    await _safe_reply(update,
        f"🔥 ALL IN confirmed by {team['name']}: {fmt_money(result['amount'])}"
    )

    state = await q.get_auction_state()
    if state["message_id"]:
        await _update_auction_card(context, update.effective_chat.id, state["message_id"],
                                    result["auction"])
    _schedule_timer(context, update.effective_chat.id, auction["id"])
    await bidding_service.maybe_alert_low_purse(context, result["team"])


# ---------------------------------------------------------------------------
# /pause /resume
# ---------------------------------------------------------------------------

async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny_admin(update)
        return
    try:
        await auction_service.pause_current_auction(update.effective_user.id)
    except auction_service.AuctionServiceError as e:
        await update.effective_message.reply_text(f"❌ {e}")
        return
    _cancel_timer(context, update.effective_chat.id)
    await update.effective_message.reply_text(
        "⏸️ <b>AUCTION PAUSED</b>\n\nNo bids will be accepted until /resume.",
        parse_mode="HTML",
    )


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny_admin(update)
        return
    try:
        auction = await auction_service.resume_current_auction(update.effective_user.id)
    except auction_service.AuctionServiceError as e:
        await update.effective_message.reply_text(f"❌ {e}")
        return

    await update.effective_message.reply_text("▶️ Auction resumed.")
    await _render_and_send_auction_card(context, update.effective_chat.id, auction)


# ---------------------------------------------------------------------------
# /next /nextplayer — bring the next player onto the block. If a player is
# currently on the auction block, this asks for confirmation to skip them
# (with an explicit SOLD/UNSOLD choice). If no player is currently active
# (the previous one already finished), this directly advances to the next
# player in the current set.
# ---------------------------------------------------------------------------

async def next_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny_admin(update)
        return

    auction = await q.get_active_auction()
    if auction is not None:
        await update.effective_message.reply_text(
            "⚠️ Skip current player?", reply_markup=skip_confirm_keyboard()
        )
        return

    # No player currently on the block — advance to the next one directly.
    state = await q.get_auction_state()
    set_id = state["current_set_id"]
    if set_id is None:
        await update.effective_message.reply_text(
            "❌ There is no active set. Use /set <n> to build/start a set first."
        )
        return

    next_entry = await auction_service.get_next_player_in_set(set_id)
    if next_entry is None:
        completed = await auction_service.complete_set_if_finished(set_id)
        if completed is not None:
            await _send_set_completed_message(context, update.effective_chat.id, set_id, completed)
            await _maybe_announce_main_auction_completed(context, update.effective_chat.id)
        else:
            await update.effective_message.reply_text("❌ There are no more players in this set.")
        return

    await advance_to_next_player(context, update.effective_chat.id, set_id)


async def next_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await _require_admin(update):
        await query.answer()
        return
    await query.answer()
    await query.edit_message_text(
        "How should this player be handled?", reply_markup=skip_outcome_keyboard()
    )


async def next_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Cancelled.")
    await query.edit_message_text("❌ Skip cancelled.")


async def next_outcome_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await _require_admin(update):
        await query.answer()
        return

    outcome_map = {"next_unsold": "UNSOLD", "next_return": "RETURN", "next_defer": "DEFER"}
    requested_outcome = outcome_map.get(query.data, "RETURN")
    _cancel_timer(context, update.effective_chat.id)

    try:
        result = await auction_service.skip_current_player(update.effective_user.id, requested_outcome)
    except auction_service.AuctionServiceError as e:
        await query.answer(str(e), show_alert=True)
        return

    await query.answer()
    outcome = result["outcome"]
    player = result["player"]
    name = player_display_name(player["first_name"], player["last_name"])
    if outcome == "UNSOLD":
        await query.edit_message_text(f"❌ {name} marked UNSOLD (skipped by admin).")
    elif outcome == "DEFERRED":
        await query.edit_message_text(
            f"⏭️ {name} skipped — will come back up later in this same set."
        )
    else:
        await query.edit_message_text(f"↩️ {name} returned to the auction pool (skipped by admin).")

    state = await q.get_auction_state()
    set_id = state["current_set_id"]
    if set_id:
        completed = await auction_service.complete_set_if_finished(set_id)
        if completed is not None:
            await _send_set_completed_message(context, update.effective_chat.id, set_id, completed)
            await _maybe_announce_main_auction_completed(context, update.effective_chat.id)
            return
        await context.bot.send_message(
            update.effective_chat.id,
            "👉 <b>Admin, bring the next player up with</b> <code>/nextplayer</code>",
            parse_mode="HTML",
        )


# ---------------------------------------------------------------------------
# /undo (undo last sale)
# ---------------------------------------------------------------------------

async def undo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny_admin(update)
        return

    tx = await q.get_last_sale_transaction()
    if tx is None:
        await update.effective_message.reply_text("❌ There is no recent sale to undo.")
        return

    player = await q.get_player_by_id(tx["player_id"])
    team = await q.get_team_by_id(tx["team_id"])
    name = player_display_name(player["first_name"], player["last_name"])

    text = (
        "⚠️ <b>UNDO LAST SALE?</b>\n\n"
        f"👤 Player:\n{name}\n\n"
        f"🏳️ Team:\n{team['name']}\n\n"
        f"💰 Sold Price:\n{fmt_money(tx['price'])}\n\n"
        "This action will:\n"
        f"• Remove {name} from {team['name']}\n"
        f"• Refund {fmt_money(tx['price'])}\n"
        f"• Return {name} to the auction pool\n"
        "• Reverse the sale"
    )
    await update.effective_message.reply_text(
        text, parse_mode="HTML", reply_markup=undo_confirm_keyboard()
    )


async def undo_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await _require_admin(update):
        await query.answer()
        return

    try:
        result = await auction_service.undo_last_sale(update.effective_user.id)
    except auction_service.AuctionServiceError as e:
        await query.answer(str(e), show_alert=True)
        return

    await query.answer()
    player, team, price = result["player"], result["team"], result["price"]
    name = player_display_name(player["first_name"], player["last_name"])
    text = (
        "↩️ <b>SALE UNDONE</b>\n\n"
        f"{name} has been returned to the auction pool.\n\n"
        f"{team['name']} refunded:\n{fmt_money(price)}"
    )
    await query.edit_message_text(text, parse_mode="HTML")


async def undo_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Cancelled.")
    await query.edit_message_text("❌ Undo cancelled.")


# ---------------------------------------------------------------------------
# /returnplayer (return any specific sold player)
# ---------------------------------------------------------------------------

async def returnplayer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny_admin(update)
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /returnplayer <registration_no>\nExample: /returnplayer 7"
        )
        return

    try:
        reg_no = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Registration number must be an integer.")
        return

    player = await q.get_player_by_registration_no(reg_no)
    if player is None:
        await update.effective_message.reply_text("❌ Player not found.")
        return
    if player["status"] != "SOLD":
        await update.effective_message.reply_text(
            f"❌ {player_display_name(player['first_name'], player['last_name'])} is not SOLD."
        )
        return

    team = await q.get_team_by_id(player["team_id"])
    name = player_display_name(player["first_name"], player["last_name"])
    text = (
        "🔄 <b>RETURN PLAYER</b>\n\n"
        f"👤 {name}\n\n"
        f"🏳️ {team['name']}\n"
        f"💰 Sold Price: {fmt_money(player['sold_price'])}"
    )
    await update.effective_message.reply_text(
        text, parse_mode="HTML", reply_markup=return_player_keyboard(player["id"])
    )


async def return_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await _require_admin(update):
        await query.answer()
        return

    player_id = int(query.data.split(":")[1])
    try:
        result = await auction_service.return_sold_player(update.effective_user.id, player_id)
    except auction_service.AuctionServiceError as e:
        await query.answer(str(e), show_alert=True)
        return

    await query.answer()
    player, team, price = result["player"], result["team"], result["price"]
    name = player_display_name(player["first_name"], player["last_name"])
    text = (
        f"🔄 {name} has been returned to the auction pool.\n\n"
        f"{team['name']} refunded: {fmt_money(price)}\n\n"
        f"{name} has been added to the re-auction queue. "
        "Use /reauction_queue to view it."
    )
    await query.edit_message_text(text)


async def return_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Cancelled.")
    await query.edit_message_text("❌ Return cancelled.")


# ---------------------------------------------------------------------------
# /tradeplayer <registration_no> <team name or id> - trade a sold player
# between teams, no purse change.
# ---------------------------------------------------------------------------

async def tradeplayer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny_admin(update)
        return

    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "Usage: /tradeplayer <registration_no> <destination team name or id>\n"
            "Example: /tradeplayer 12 Thunder XI"
        )
        return

    reg_no = None
    try:
        reg_no = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Registration number must be an integer.")
        return

    player = await q.get_player_by_registration_no(reg_no)
    if player is None:
        await update.effective_message.reply_text(f"❌ No player found with registration #{reg_no}.")
        return
    if player["status"] != "SOLD":
        await update.effective_message.reply_text(
            f"❌ #{reg_no} {player_display_name(player['first_name'], player['last_name'])} "
            "is not currently SOLD, so can't be traded."
        )
        return

    team_identifier = " ".join(context.args[1:])
    to_team = await q.get_team_by_name_or_id(team_identifier)
    if to_team is None:
        await update.effective_message.reply_text(f"❌ No team found matching '{team_identifier}'.")
        return

    from_team = await q.get_team_by_id(player["team_id"]) if player["team_id"] else None
    if from_team and from_team["id"] == to_team["id"]:
        await update.effective_message.reply_text("❌ Player is already on that team.")
        return

    name = player_display_name(player["first_name"], player["last_name"])
    from_name = from_team["name"] if from_team else "—"
    text = (
        "🔄 <b>CONFIRM TRADE</b>\n\n"
        f"👤 {name} (#{reg_no})\n"
        f"🏳️ From: {from_name}\n"
        f"🏳️ To: {to_team['name']}\n\n"
        "No purse will be charged or refunded — this is a direct swap."
    )
    await update.effective_message.reply_text(
        text, parse_mode="HTML", reply_markup=trade_confirm_keyboard(player["id"], to_team["id"])
    )


async def trade_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await _require_admin(update):
        await query.answer()
        return

    _, player_id_str, to_team_id_str = query.data.split(":")
    player_id, to_team_id = int(player_id_str), int(to_team_id_str)

    try:
        result = await auction_service.trade_player(update.effective_user.id, player_id, to_team_id)
    except auction_service.AuctionServiceError as e:
        await query.answer(str(e), show_alert=True)
        return

    await query.answer("Traded!")
    player, from_team, to_team = result["player"], result["from_team"], result["to_team"]
    name = player_display_name(player["first_name"], player["last_name"])
    from_name = from_team["name"] if from_team else "—"
    text = f"🔄 {name} has been traded from {from_name} to {to_team['name']}."
    await query.edit_message_text(text)

    if settings.group_chat_id:
        try:
            await context.bot.send_message(
                settings.group_chat_id,
                f"🔄 <b>PLAYER TRADED</b>\n\n👤 {name}\n{from_name} ➜ {to_team['name']}",
                parse_mode="HTML",
            )
        except Exception:
            pass


async def trade_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Cancelled.")
    await query.edit_message_text("❌ Trade cancelled.")


# ---------------------------------------------------------------------------
# Re-auction queue
# ---------------------------------------------------------------------------

async def reauction_queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny_admin(update)
        return

    queue = await auction_service.get_reauction_queue()
    if not queue:
        await update.effective_message.reply_text("🔄 The re-auction queue is empty.")
        return

    lines = ["🔄 <b>RE-AUCTION QUEUE</b>", ""]
    for i, entry in enumerate(queue, start=1):
        name = player_display_name(entry["first_name"], entry["last_name"])
        lines.append(f"{i}. {name} #{entry['registration_no']}")

    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")
    for entry in queue:
        name = player_display_name(entry["first_name"], entry["last_name"])
        await update.effective_message.reply_text(
            f"#{entry['registration_no']} {name}",
            reply_markup=reauction_queue_keyboard(entry["player_id"]),
        )


async def reauction_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await _require_admin(update):
        await query.answer()
        return

    player_id = int(query.data.split(":")[1])
    try:
        auction = await auction_service.start_reauction_for_player(
            update.effective_user.id, player_id
        )
    except auction_service.AuctionServiceError as e:
        await query.answer(str(e), show_alert=True)
        return

    await query.answer()
    await query.edit_message_text("🔨 Re-auction started for this player.")
    await q.update_auction_state(chat_id=update.effective_chat.id)
    await _render_and_send_auction_card(context, update.effective_chat.id, auction)


# ---------------------------------------------------------------------------
# /unsold (unsold-round auction) and /unsold_list
# ---------------------------------------------------------------------------

async def unsold_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    players = await q.list_unsold_players()
    if not players:
        await update.effective_message.reply_text("❌ There are no unsold players.")
        return

    lines = ["❌ <b>UNSOLD PLAYERS</b>", ""]
    for i, p in enumerate(players, start=1):
        name = player_display_name(p["first_name"], p["last_name"])
        lines.append(f"{i}. {name} — #{p['registration_no']}")
    lines.append("")
    lines.append(f"Total:\n{len(players)}")

    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def unsold_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny_admin(update)
        return

    complete = await auction_service.is_main_auction_complete()
    if not complete:
        await update.effective_message.reply_text(
            "❌ The main auction is not complete yet. Finish all regular sets first."
        )
        return

    player = await auction_service.get_next_unsold_player()
    if player is None:
        await update.effective_message.reply_text("✅ There are no unsold players remaining.")
        return

    await auction_service.start_unsold_auction_state(update.effective_user.id)
    name = player_display_name(player["first_name"], player["last_name"])
    text = (
        "🔄 <b>UNSOLD AUCTION</b>\n\n"
        f"Player:\n{name}\n\n"
        f"Original Registration:\n#{player['registration_no']}\n\n"
        f"Base Price:\n{fmt_money(player['base_price'])}\n\n"
        "Previous Status:\nUNSOLD"
    )
    await update.effective_message.reply_text(
        text, parse_mode="HTML", reply_markup=unsold_start_keyboard(player["id"])
    )


# ---------------------------------------------------------------------------
# /auctionplayer <registration_no> - jump straight to a specific player
# (from the UNSOLD pool, or the AVAILABLE pool via the re-auction path),
# bypassing sequential set/unsold order.
# ---------------------------------------------------------------------------

async def auctionplayer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny_admin(update)
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /auctionplayer <registration_no>\nExample: /auctionplayer 34"
        )
        return

    reg_no = None
    try:
        reg_no = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Registration number must be an integer.")
        return

    existing_active = await q.get_active_auction()
    if existing_active is not None:
        await update.effective_message.reply_text(
            "❌ A player is already on the auction block. Finish or /pause the "
            "current auction before starting another."
        )
        return

    player = await q.get_player_by_registration_no(reg_no)
    if player is None:
        await update.effective_message.reply_text(f"❌ No player found with registration #{reg_no}.")
        return

    if player["status"] not in ("UNSOLD", "AVAILABLE"):
        await update.effective_message.reply_text(
            f"❌ #{reg_no} {player_display_name(player['first_name'], player['last_name'])} "
            f"is currently {player['status']} and cannot be sent to auction."
        )
        return

    mode = "UNSOLD_ROUND" if player["status"] == "UNSOLD" else "REAUCTION"
    try:
        auction = await auction_service.start_player_auction(player, set_id=None, mode=mode)
    except auction_service.AuctionServiceError as e:
        await update.effective_message.reply_text(f"❌ {e}")
        return

    name = player_display_name(player["first_name"], player["last_name"])
    await update.effective_message.reply_text(
        f"🔨 Bringing #{reg_no} {name} to the auction block…"
    )
    await q.update_auction_state(chat_id=update.effective_chat.id)
    await _render_and_send_auction_card(context, update.effective_chat.id, auction)


async def unsold_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await _require_admin(update):
        await query.answer()
        return

    player_id = int(query.data.split(":")[1])
    player = await q.get_player_by_id(player_id)
    if player is None or player["status"] != "UNSOLD":
        await query.answer("❌ This player is no longer in the unsold pool.", show_alert=True)
        return

    try:
        auction = await auction_service.start_player_auction(player, set_id=None, mode="UNSOLD_ROUND")
    except auction_service.AuctionServiceError as e:
        await query.answer(str(e), show_alert=True)
        return

    await query.answer()
    await query.edit_message_text("🔨 Bidding started.")
    await q.update_auction_state(chat_id=update.effective_chat.id)
    await _render_and_send_auction_card(context, update.effective_chat.id, auction)


# ---------------------------------------------------------------------------
# /auction_status
# ---------------------------------------------------------------------------

async def auction_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    summary = await auction_service.get_auction_status_summary()
    state = summary["state"]
    counts = summary["counts"]
    auction = summary["auction"]
    player = summary["player"]
    auction_set = summary["auction_set"]

    lines = ["🔨 <b>AUCTION STATUS</b>", "━━━━━━━━━━━━━━━━━━━━", "", f"State: {state['state']}"]

    if auction_set is not None:
        set_players = await q.get_set_players(auction_set["id"])
        processed = sum(1 for p in set_players if p["processed"])
        lines.append(f"\nSet:\n{auction_set['set_number']}")
        lines.append(f"\nPlayer:\n#{processed}/{len(set_players)}")

    if auction is not None and player is not None:
        remaining = max(0, int(seconds_remaining(auction["deadline_ts"])))
        highest_team_name = "—"
        if auction["highest_team_id"]:
            t = await q.get_team_by_id(auction["highest_team_id"])
            highest_team_name = t["name"] if t else "—"
        name = player_display_name(player["first_name"], player["last_name"])
        lines.append(f"\nCurrent Player:\n{name}")
        lines.append(f"\nCurrent Bid:\n{fmt_money(auction['current_bid'])}")
        lines.append(f"\nHighest Bidder:\n{highest_team_name}")
        lines.append(f"\nTimer:\n{remaining} seconds")
    else:
        lines.append("\nNo player is currently on the auction block.")

    lines.append(f"\nSold:\n{counts.get('SOLD', 0)}")
    lines.append(f"\nUnsold:\n{counts.get('UNSOLD', 0)}")
    lines.append(f"\nRemaining Available:\n{counts.get('AVAILABLE', 0)}")

    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# /sold
# ---------------------------------------------------------------------------

async def sold_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    players = await q.list_sold_players()
    if not players:
        await update.effective_message.reply_text("✅ No players have been sold yet.")
        return

    lines = ["✅ <b>SOLD PLAYERS</b>", ""]
    for i, p in enumerate(players, start=1):
        name = player_display_name(p["first_name"], p["last_name"])
        lines.append(f"{i}. {name} → {p['team_name']} — {fmt_money(p['sold_price'])}")
    lines.append("")
    lines.append(f"Total Sold:\n{len(players)}")

    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3800] + "\n\n… (truncated, use /export for full data)"
    await update.effective_message.reply_text(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# /bidhistory <registration_no> - all bids ever placed on a player
# ---------------------------------------------------------------------------

async def bidhistory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /bidhistory <registration_no>\nExample: /bidhistory 34"
        )
        return

    reg_no = None
    try:
        reg_no = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Registration number must be an integer.")
        return

    player = await q.get_player_by_registration_no(reg_no)
    if player is None:
        await update.effective_message.reply_text(f"❌ No player found with registration #{reg_no}.")
        return

    bids = await q.get_bids_for_player(player["id"])
    name = player_display_name(player["first_name"], player["last_name"])

    if not bids:
        await update.effective_message.reply_text(
            f"📜 No bids have been placed on #{reg_no} {name} yet."
        )
        return

    lines = [f"📜 <b>BID HISTORY — #{reg_no} {name}</b>", "━━━━━━━━━━━━━━━━━━━━", ""]
    current_auction_id = None
    turn_no = 0
    for b in bids:
        if b["auction_id"] != current_auction_id:
            current_auction_id = b["auction_id"]
            turn_no += 1
            lines.append(f"<b>Turn {turn_no} ({b['auction_mode']})</b>")
        lines.append(f"  • {b['team_name']} — {fmt_money(b['amount'])}")
    lines.append("")
    lines.append(f"Total bids: {len(bids)}")

    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3800] + "\n\n… (truncated)"
    await update.effective_message.reply_text(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# /topsold - highest sold players leaderboard
# ---------------------------------------------------------------------------

async def topsold_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    limit = 10
    if context.args:
        try:
            limit = max(1, min(50, int(context.args[0])))
        except ValueError:
            pass

    players = await q.list_sold_players()
    if not players:
        await update.effective_message.reply_text("✅ No players have been sold yet.")
        return

    ranked = sorted(players, key=lambda p: p["sold_price"] or 0, reverse=True)[:limit]
    medals = ["🥇", "🥈", "🥉"]

    lines = [f"🏆 <b>TOP {len(ranked)} SOLD PLAYERS</b>", "━━━━━━━━━━━━━━━━━━━━", ""]
    for i, p in enumerate(ranked):
        name = player_display_name(p["first_name"], p["last_name"])
        rank_icon = medals[i] if i < len(medals) else f"{i + 1}."
        lines.append(f"{rank_icon} {name} → {p['team_name']} — {fmt_money(p['sold_price'])}")

    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# /teamcompare - spending comparison across all teams
# ---------------------------------------------------------------------------

async def teamcompare_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from services import captain_service
    teams_data = await captain_service.list_teams_with_squad()
    if not teams_data:
        await update.effective_message.reply_text("🏳️ No teams have been created yet.")
        return

    rows = []
    for entry in teams_data:
        team, squad = entry["team"], entry["squad"]
        spent = team["starting_purse"] - team["remaining_purse"]
        avg = spent // len(squad) if squad else 0
        rows.append({
            "name": team["name"], "spent": spent, "remaining": team["remaining_purse"],
            "squad_size": len(squad), "avg": avg,
        })

    rows.sort(key=lambda r: r["spent"], reverse=True)

    lines = ["📊 <b>TEAM SPENDING COMPARISON</b>", "━━━━━━━━━━━━━━━━━━━━", ""]
    for i, r in enumerate(rows, start=1):
        lines.append(
            f"{i}. <b>{r['name']}</b>\n"
            f"   💸 Spent: {fmt_money(r['spent'])} | 💰 Left: {fmt_money(r['remaining'])}\n"
            f"   👥 {r['squad_size']} players | 📈 Avg: {fmt_money(r['avg'])}/player\n"
        )

    if rows:
        biggest_spender = rows[0]
        most_frugal = min(rows, key=lambda r: r["spent"])
        lines.append(f"🔥 Biggest spender: {biggest_spender['name']}")
        lines.append(f"💎 Most frugal: {most_frugal['name']}")

    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3800] + "\n\n… (truncated)"
    await update.effective_message.reply_text(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# /blockteam /unblockteam - temporarily block a team from bidding
# ---------------------------------------------------------------------------

async def blockteam_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny_admin(update)
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /blockteam <team name or id>\nExample: /blockteam Thunder XI"
        )
        return

    identifier = " ".join(context.args)
    team = await q.get_team_by_name_or_id(identifier)
    if team is None:
        await update.effective_message.reply_text(f"❌ No team found matching '{identifier}'.")
        return

    await q.set_team_bid_blocked(team["id"], True)
    await update.effective_message.reply_text(
        f"🚫 {team['name']} has been blocked from bidding. Use /unblockteam to lift it."
    )
    try:
        await context.bot.send_message(
            team["captain_telegram_id"],
            f"🚫 Your team {team['name']} has been temporarily blocked from bidding by an admin."
        )
    except Exception:
        pass


async def unblockteam_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny_admin(update)
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /unblockteam <team name or id>\nExample: /unblockteam Thunder XI"
        )
        return

    identifier = " ".join(context.args)
    team = await q.get_team_by_name_or_id(identifier)
    if team is None:
        await update.effective_message.reply_text(f"❌ No team found matching '{identifier}'.")
        return

    await q.set_team_bid_blocked(team["id"], False)
    await update.effective_message.reply_text(f"✅ {team['name']} can bid again.")
    try:
        await context.bot.send_message(
            team["captain_telegram_id"],
            f"✅ Your team {team['name']} can bid again — the block has been lifted."
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# /auctionhistory
# ---------------------------------------------------------------------------

async def auction_history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny_admin(update)
        return

    logs = await q.get_audit_logs(limit=40)
    if not logs:
        await update.effective_message.reply_text("No auction history recorded yet.")
        return

    lines = ["📜 <b>AUCTION HISTORY</b> (most recent 40 events)", ""]
    for log in logs:
        lines.append(f"• [{log['created_at']}] {log['action']}")

    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3800] + "\n\n… (truncated)"
    await update.effective_message.reply_text(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# /startauction /endauction
# ---------------------------------------------------------------------------

async def startauction_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny_admin(update)
        return

    min_players = await q.get_setting_int("min_players_to_start")
    if min_players > 0:
        available = await q.count_available_players()
        if available < min_players:
            text = (
                f"❌ Not enough players to start. Need at least {min_players}, "
                f"have {available}."
            )
            if update.callback_query:
                await update.callback_query.answer(text, show_alert=True)
            else:
                await update.effective_message.reply_text(text)
            return

    from database.models import AuctionState, AuditAction
    await q.update_auction_state(state=AuctionState.SET_READY.value,
                                  chat_id=update.effective_chat.id)
    await q.add_audit_log(update.effective_user.id, AuditAction.AUCTION_STARTED)
    text = "🏏 <b>AUCTION STARTED</b>\n\nUse /set 1 to build and begin the first auction set."
    if update.callback_query:
        await context.bot.send_message(update.effective_chat.id, text, parse_mode="HTML")
    else:
        await update.effective_message.reply_text(text, parse_mode="HTML")


async def endauction_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny_admin(update)
        return
    from database.models import AuctionState, AuditAction

    _cancel_timer(context, update.effective_chat.id)
    await q.update_auction_state(state=AuctionState.AUCTION_COMPLETED.value, clear_auction=True)
    await q.add_audit_log(update.effective_user.id, AuditAction.AUCTION_COMPLETED)

    from services import captain_service

    counts = await q.count_players_by_status()
    teams_data = await captain_service.list_teams_with_squad()

    lines = [
        "📊 <b>FINAL AUCTION REPORT</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"Total Registered:\n{sum(counts.values())}",
        "",
        f"Total Sold:\n{counts.get('SOLD', 0)}",
        "",
        f"Total Unsold:\n{counts.get('UNSOLD', 0)}",
        "",
        f"Total Removed:\n{counts.get('REMOVED', 0)}",
        "",
        f"Teams:\n{len(teams_data)}",
    ]
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")

    for entry in teams_data:
        team, squad = entry["team"], entry["squad"]
        team_lines = [f"🏳️ <b>{team['name'].upper()}</b>", ""]
        for i, p in enumerate(squad, start=1):
            name = player_display_name(p["first_name"], p["last_name"])
            team_lines.append(f"{i}. {name} — {fmt_money(p['sold_price'])}")
        team_lines.append("")
        team_lines.append(f"💰 Total Spent:\n{fmt_money(team['starting_purse'] - team['remaining_purse'])}")
        team_lines.append("")
        team_lines.append(f"💰 Remaining:\n{fmt_money(team['remaining_purse'])}")
        await update.effective_message.reply_text("\n".join(team_lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# /lobby - pinned, auto-refreshing group status message (view-only;
# registration itself still happens via DM /register and /becomecaptain).
# ---------------------------------------------------------------------------

async def lobby_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny_admin(update)
        return

    chat_id = update.effective_chat.id
    data = await lobby_service.get_lobby_data()
    text, total_pages = lobby_service.render_lobby_text(data, page=1)
    keyboard = lobby_keyboard(1, total_pages, is_admin=True)

    msg = await update.effective_message.reply_text(
        text, parse_mode="HTML", reply_markup=keyboard
    )
    await q.set_lobby_message_ref(chat_id, msg.message_id, page=1)

    try:
        await context.bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
    except BadRequest as e:
        logger.warning("Could not pin lobby message (bot may need admin rights): %s", e)


async def refresh_lobby_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Re-renders and edits the pinned lobby message in place, if one exists.
    Called automatically after any player registration or captain approval
    so the group view always reflects current state without an admin
    needing to manually refresh. Safe to call even if no lobby is pinned
    (no-op) or if the edit fails for any reason (flood control, message
    deleted, etc. — logged, never raised).

    Also sends a one-time "ready to start" nudge to the group once the
    minimum player requirement is first met, so the admin doesn't have to
    keep checking manually.
    """
    chat_id, message_id, page = await q.get_lobby_message_ref()
    if chat_id is None or message_id is None:
        return

    data = await lobby_service.get_lobby_data()
    text, total_pages = lobby_service.render_lobby_text(data, page=page)
    keyboard = lobby_keyboard(page, total_pages, is_admin=True)

    from telegram.error import TelegramError
    try:
        await context.bot.edit_message_text(
            text, chat_id=chat_id, message_id=message_id, parse_mode="HTML",
            reply_markup=keyboard,
        )
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.warning("Could not refresh lobby message: %s", e)
    except TelegramError as e:
        logger.warning("Could not refresh lobby message: %s", e)

    await _maybe_announce_ready_to_start(context, chat_id, data)


async def _maybe_announce_ready_to_start(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                                          lobby_data: dict) -> None:
    min_players = lobby_data["min_players_to_start"]
    if min_players <= 0:
        return  # no minimum configured, nothing to announce

    already_notified = (await q.get_setting("min_players_reached_notified")) == "1"
    if already_notified:
        return

    if len(lobby_data["pool"]) < min_players:
        return

    await q.set_setting("min_players_reached_notified", "1")
    try:
        await context.bot.send_message(
            chat_id,
            "🎉 <b>READY TO START!</b>\n\n"
            f"The minimum of {min_players} players has been reached. "
            "Admin can now run /startauction whenever ready.",
            parse_mode="HTML",
        )
    except Exception:
        logger.warning("Could not send ready-to-start announcement.")


async def lobby_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":")[1])

    data = await lobby_service.get_lobby_data()
    text, total_pages = lobby_service.render_lobby_text(data, page=page)
    is_admin_user = await _require_admin(update)
    keyboard = lobby_keyboard(page, total_pages, is_admin=is_admin_user)

    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.warning("Lobby page edit failed: %s", e)

    chat_id, message_id, _ = await q.get_lobby_message_ref()
    if chat_id is not None and message_id == query.message.message_id:
        await q.set_lobby_page(page)


async def lobby_refresh_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Refreshed")

    _, _, page = await q.get_lobby_message_ref()
    data = await lobby_service.get_lobby_data()
    text, total_pages = lobby_service.render_lobby_text(data, page=page)
    is_admin_user = await _require_admin(update)
    keyboard = lobby_keyboard(page, total_pages, is_admin=is_admin_user)

    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.warning("Lobby refresh edit failed: %s", e)


async def lobby_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await _require_admin(update):
        await query.answer()
        return

    data = await lobby_service.get_lobby_data()
    min_players = data["min_players_to_start"]
    if min_players > 0 and len(data["pool"]) < min_players:
        await query.answer(
            f"❌ Need at least {min_players} players (have {len(data['pool'])}).",
            show_alert=True,
        )
        return

    await query.answer("Starting auction…")
    await startauction_command(update, context)


async def lobby_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await _require_admin(update):
        await query.answer()
        return
    await query.answer("Lobby cancelled.")
    try:
        await query.edit_message_text("🛑 Lobby cancelled by admin.")
    except BadRequest:
        pass
    await q.clear_lobby_message_ref()


# ---------------------------------------------------------------------------
# /remind <minutes> <message> - schedule a one-off reminder in this chat
# ---------------------------------------------------------------------------

async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny_admin(update)
        return

    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "Usage: /remind <minutes> <message>\n"
            "Example: /remind 60 Auction starts in 1 hour, be ready!"
        )
        return

    try:
        minutes = float(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Minutes must be a number.")
        return
    if minutes <= 0 or minutes > 24 * 60:
        await update.effective_message.reply_text("❌ Minutes must be between 0 and 1440 (24 hours).")
        return

    message_text = " ".join(context.args[1:])
    chat_id = update.effective_chat.id

    context.job_queue.run_once(
        _send_scheduled_reminder, when=minutes * 60,
        chat_id=chat_id, name=f"reminder:{chat_id}:{update.effective_message.message_id}",
        data={"message": message_text},
    )

    when_str = f"{minutes:g} minute(s)"
    await update.effective_message.reply_text(
        f"⏰ Reminder scheduled for {when_str} from now:\n\"{message_text}\""
    )


async def _send_scheduled_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    message = job.data.get("message", "Reminder!")
    try:
        await context.bot.send_message(
            job.chat_id, f"⏰ <b>REMINDER</b>\n\n{message}", parse_mode="HTML",
        )
    except Exception:
        logger.warning("Failed to send scheduled reminder to chat %s", job.chat_id)

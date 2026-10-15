"""
handlers/registration.py
-------------------------
Player self-registration, profile viewing, and player listing.
"""
from __future__ import annotations

from telegram import Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from config import settings
from keyboards.main import back_to_menu_keyboard, category_selection_keyboard
from services import player_service
from utils.helpers import fmt_money, is_admin, player_display_name
from utils.logger import get_logger

logger = get_logger(__name__)


async def _send_or_edit(update: Update, text: str, reply_markup=None) -> None:
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="HTML", reply_markup=reply_markup
        )
    else:
        await update.effective_message.reply_text(
            text, parse_mode="HTML", reply_markup=reply_markup
        )


async def _registration_deadline_passed() -> bool:
    from database import queries as q
    from datetime import datetime, timezone

    deadline_raw = await q.get_setting("registration_deadline")
    if not deadline_raw:
        return False
    try:
        deadline = datetime.fromisoformat(deadline_raw)
    except ValueError:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= deadline


async def show_registration_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles both /register command and the 'Register Player' button."""
    user = update.effective_user
    existing = await player_service.get_profile(user.id)
    if existing is not None and existing["status"] != "REMOVED":
        await _send_or_edit(
            update,
            f"✅ You are already registered!\n\n🔢 Registration No: <b>#{existing['registration_no']}</b>",
            back_to_menu_keyboard(),
        )
        return

    # A user who already registered as a CAPTAIN cannot also register as a
    # player — one Telegram account can only hold one role.
    from database import queries as q
    existing_captain = await q.get_captain_by_telegram_id(user.id)
    if existing_captain and existing_captain["status"] in ("PENDING", "APPROVED"):
        await _send_or_edit(
            update,
            "❌ You are already registered as a <b>Captain</b> "
            f"(team: {existing_captain['team_name']}). A captain cannot also "
            "register as a player.",
            back_to_menu_keyboard(),
        )
        return

    if await _registration_deadline_passed():
        await _send_or_edit(
            update,
            "❌ <b>Registration is closed.</b>\n\nThe registration deadline has passed.",
            back_to_menu_keyboard(),
        )
        return

    from services import subscription_service
    if subscription_service.is_subscription_required():
        check = await subscription_service.check_subscription(context, user.id)
        if not check["ok"]:
            text, keyboard = subscription_service.build_join_prompt(check)
            await _send_or_edit(update, text, keyboard)
            return

    was_removed = existing is not None and existing["status"] == "REMOVED"

    try:
        player = await player_service.register_player(
            telegram_id=user.id, username=user.username,
            first_name=user.first_name, last_name=user.last_name,
        )
    except player_service.PlayerServiceError as e:
        await _send_or_edit(update, f"❌ {e}", back_to_menu_keyboard())
        return

    name = player_display_name(player["first_name"], player["last_name"])
    if was_removed:
        text = (
            "📝 <b>REGISTRATION RESTORED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🔢 Registration No: <b>#{player['registration_no']}</b>\n"
            f"👤 Name: {name}\n"
            f"📌 Status: {player['status']}\n\n"
            "Welcome back — you're in the auction pool again! 🏏"
        )
        group_header = "📝 <b>PLAYER RE-REGISTERED</b>"
    else:
        text = (
            "📝 <b>REGISTRATION SUCCESSFUL</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🔢 Registration No: <b>#{player['registration_no']}</b>\n"
            f"👤 Name: {name}\n"
            f"📌 Status: {player['status']}\n\n"
            "You are now in the auction pool. Good luck! 🏏"
        )
        group_header = "📝 <b>NEW PLAYER REGISTERED</b>"
    await _send_or_edit(update, text, back_to_menu_keyboard())

    # Post confirmation to the group automatically.
    if settings.group_chat_id:
        try:
            await context.bot.send_message(
                settings.group_chat_id,
                f"{group_header}\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🔢 Registration No: #{player['registration_no']}\n"
                f"👤 Name: {name}",
                parse_mode="HTML",
            )
        except Exception:
            logger.warning("Could not post player registration to group chat.")

    # Keep the pinned lobby status message (if any) in sync.
    from handlers.auction import refresh_lobby_message
    await refresh_lobby_message(context)

    # Ask for a playing category (Batsman/Bowler/All-rounder/Wicket-keeper),
    # optional — the player can skip it.
    await context.bot.send_message(
        user.id,
        "🏏 What's your playing role? (optional — helps with category-based sets)",
        reply_markup=category_selection_keyboard(),
    )


async def player_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    category = query.data.split(":", 1)[1]

    user = update.effective_user
    player = await player_service.get_profile(user.id)
    if player is None:
        await query.edit_message_text("❌ You're not registered yet.")
        return

    if category == "skip":
        await query.edit_message_text("👍 No problem — you can set this later from /profile.")
        return

    from database import queries as q
    await q.set_player_category(player["id"], category)
    await query.edit_message_text(f"✅ Playing role set to: <b>{category}</b>", parse_mode="HTML")


async def register_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_registration_prompt(update, context)


async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    player = await player_service.get_profile(user.id)
    if player is None:
        await _send_or_edit(
            update,
            "❌ You are not registered yet. Use /register to join the auction pool.",
            back_to_menu_keyboard(),
        )
        return

    lines = [
        "👤 <b>MY PROFILE</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"Registration No:\n<b>#{player['registration_no']}</b>",
        "",
        f"Name:\n{player_display_name(player['first_name'], player['last_name'])}",
        "",
        f"Username:\n@{player['username']}" if player["username"] else "Username:\n—",
        "",
        f"Role:\n{player['category'] if player['category'] else '—'}",
        "",
        f"Status:\n<b>{player['status']}</b>",
    ]

    if player["status"] == "SOLD":
        team = await player_service.get_profile_team(player)
        team_name = team["name"] if team else "—"
        lines += [
            "",
            f"Team:\n{team_name}",
            "",
            f"Sold Price:\n{fmt_money(player['sold_price'])}",
        ]

    await _send_or_edit(update, "\n".join(lines), back_to_menu_keyboard())


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_profile(update, context)


async def show_player_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: full player list is not shown to regular players/captains."""
    from utils.helpers import is_admin_async
    if not is_admin(update.effective_user.id) and not await is_admin_async(update.effective_user.id):
        if update.callback_query:
            await update.callback_query.answer()
        return

    players = await player_service.list_all_players()
    if not players:
        await _send_or_edit(update, "📋 No players registered yet.", back_to_menu_keyboard())
        return

    lines = ["📋 <b>PLAYER LIST</b>", "━━━━━━━━━━━━━━━━━━━━", ""]
    for p in players:
        status_icon = {
            "AVAILABLE": "🟢", "REGISTERED": "🟡", "SOLD": "✅",
            "UNSOLD": "🔴", "IN_AUCTION": "🔨", "REMOVED": "🚫",
        }.get(p["status"], "⚪")
        name = player_display_name(p["first_name"], p["last_name"])
        cat = f" [{p['category']}]" if p["category"] else ""
        lines.append(f"{status_icon} #{p['registration_no']} — {name}{cat} ({p['status']})")

    lines.append("")
    lines.append(f"Total: {len(players)}")

    text = "\n".join(lines)
    # Telegram message length safety: truncate very long lists.
    if len(text) > 3900:
        text = text[:3800] + "\n\n… (list truncated, use /export for full data)"

    await _send_or_edit(update, text, back_to_menu_keyboard())


async def players_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_player_list(update, context)

"""
handlers/setup_wizard.py
--------------------------
/setuptournament — a guided, step-by-step wizard for admins to configure a
new tournament's auction parameters (number of teams, squad size, purse,
base price, timer, minimum players to start) without needing to remember
individual /setconfig keys. Every answer is validated before moving to the
next step; a running summary is shown at the end for confirmation before
anything is saved.

Note: there is no "minimum bid increment" setting — bidding has no fixed
increment, any amount strictly above the current bid is valid, same as a
real live auction.
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler,
    MessageHandler, filters,
)

from database import queries as q
from database.models import AuditAction
from utils.helpers import fmt_money, is_admin, is_admin_async, safe_int
from utils.logger import get_logger

logger = get_logger(__name__)

(
    MAX_TEAMS, SQUAD_SIZE, STARTING_PURSE, BASE_PRICE,
    AUCTION_TIMER, MIN_PLAYERS, CONFIRM,
) = range(7)

_STEP_LABELS = {
    MAX_TEAMS: "1/6 — How many teams will there be?",
    SQUAD_SIZE: "2/6 — How many players per team (including captain)?",
    STARTING_PURSE: "3/6 — Starting purse per team (₹)?",
    BASE_PRICE: "4/6 — Base price per player (₹)?",
    AUCTION_TIMER: "5/6 — Auction timer per player (seconds)?",
    MIN_PLAYERS: "6/6 — Minimum players required to start the auction? "
                 "(send 0 for no minimum)",
}


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Save", callback_data="setup_confirm"),
            InlineKeyboardButton("❌ Discard", callback_data="setup_cancel"),
        ]
    ])


async def _deny(update: Update) -> None:
    if update.callback_query:
        await update.callback_query.answer()
    return


async def setuptournament_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id) and not await is_admin_async(update.effective_user.id):
        await _deny(update)
        return ConversationHandler.END

    context.user_data["setup_wizard"] = {}
    await update.effective_message.reply_text(
        "🛠️ <b>TOURNAMENT SETUP WIZARD</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "I'll ask a few quick questions to configure this auction. "
        "Send /cancel anytime to stop without saving.\n\n"
        f"{_STEP_LABELS[MAX_TEAMS]}",
        parse_mode="HTML",
    )
    return MAX_TEAMS


async def _ask_next(update: Update, step: int) -> int:
    await update.effective_message.reply_text(_STEP_LABELS[step])
    return step


async def max_teams_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = safe_int(update.effective_message.text.strip())
    if value is None or value <= 0:
        await update.effective_message.reply_text("❌ Please send a positive whole number.")
        return MAX_TEAMS
    context.user_data["setup_wizard"]["max_teams"] = value
    return await _ask_next(update, SQUAD_SIZE)


async def squad_size_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = safe_int(update.effective_message.text.strip())
    if value is None or value <= 0:
        await update.effective_message.reply_text("❌ Please send a positive whole number.")
        return SQUAD_SIZE
    context.user_data["setup_wizard"]["max_squad_size"] = value
    return await _ask_next(update, STARTING_PURSE)


async def starting_purse_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = safe_int(update.effective_message.text.strip().replace(",", ""))
    if value is None or value <= 0:
        await update.effective_message.reply_text("❌ Please send a positive whole number (₹).")
        return STARTING_PURSE
    context.user_data["setup_wizard"]["starting_purse"] = value
    return await _ask_next(update, BASE_PRICE)


async def base_price_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = safe_int(update.effective_message.text.strip().replace(",", ""))
    if value is None or value <= 0:
        await update.effective_message.reply_text("❌ Please send a positive whole number (₹).")
        return BASE_PRICE
    wizard = context.user_data["setup_wizard"]
    if value >= wizard["starting_purse"]:
        await update.effective_message.reply_text(
            f"❌ Base price must be less than the starting purse "
            f"({fmt_money(wizard['starting_purse'])})."
        )
        return BASE_PRICE
    wizard["base_price"] = value
    return await _ask_next(update, AUCTION_TIMER)


async def auction_timer_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = safe_int(update.effective_message.text.strip())
    if value is None or value <= 0:
        await update.effective_message.reply_text("❌ Please send a positive whole number (seconds).")
        return AUCTION_TIMER
    context.user_data["setup_wizard"]["auction_timer"] = value
    return await _ask_next(update, MIN_PLAYERS)


async def min_players_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = safe_int(update.effective_message.text.strip())
    if value is None or value < 0:
        await update.effective_message.reply_text(
            "❌ Please send a whole number (0 or higher). Send 0 for no minimum."
        )
        return MIN_PLAYERS
    context.user_data["setup_wizard"]["min_players_to_start"] = value

    wizard = context.user_data["setup_wizard"]
    summary = (
        "📋 <b>REVIEW YOUR SETUP</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🏟️ Teams: {wizard['max_teams']}\n"
        f"👥 Squad size: {wizard['max_squad_size']} (captain + others)\n"
        f"💼 Starting purse: {fmt_money(wizard['starting_purse'])}\n"
        f"💰 Base price: {fmt_money(wizard['base_price'])}\n"
        f"⏱️ Auction timer: {wizard['auction_timer']}s per player\n"
        f"📥 Min players to start: "
        f"{'no minimum' if wizard['min_players_to_start'] == 0 else wizard['min_players_to_start']}\n\n"
        "<i>Bidding has no fixed increment — any bid above the current bid "
        "is valid, just like a real auction.</i>\n\n"
        "Save this configuration?"
    )
    await update.effective_message.reply_text(
        summary, parse_mode="HTML", reply_markup=_confirm_keyboard()
    )
    return CONFIRM


async def setup_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not is_admin(update.effective_user.id) and not await is_admin_async(update.effective_user.id):
        await _deny(update)
        return ConversationHandler.END

    wizard = context.user_data.get("setup_wizard")
    if not wizard:
        await query.answer()
        await query.edit_message_text("❌ Setup session expired. Please run /setuptournament again.")
        return ConversationHandler.END

    for key, value in wizard.items():
        await q.set_setting(key, str(value))
    await q.add_audit_log(update.effective_user.id, AuditAction.SETTINGS_UPDATED,
                           details={"tournament_setup": wizard})

    await query.answer("✅ Saved!")
    await query.edit_message_text(
        "✅ <b>TOURNAMENT CONFIGURED</b>\n\n"
        "Settings saved. Use /lobby to post the live lobby status in the "
        "group, or /becomecaptain / /register can now begin.",
        parse_mode="HTML",
    )
    context.user_data.pop("setup_wizard", None)

    from handlers.auction import refresh_lobby_message
    await refresh_lobby_message(context)
    return ConversationHandler.END


async def setup_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("Discarded.")
    await query.edit_message_text("❌ Setup discarded. Nothing was changed.")
    context.user_data.pop("setup_wizard", None)
    return ConversationHandler.END


async def setuptournament_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("setup_wizard", None)
    await update.effective_message.reply_text("Setup cancelled. Nothing was changed.")
    return ConversationHandler.END


def build_setup_wizard_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("setuptournament", setuptournament_start)],
        states={
            MAX_TEAMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, max_teams_received)],
            SQUAD_SIZE: [MessageHandler(filters.TEXT & ~filters.COMMAND, squad_size_received)],
            STARTING_PURSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, starting_purse_received)],
            BASE_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, base_price_received)],
            AUCTION_TIMER: [MessageHandler(filters.TEXT & ~filters.COMMAND, auction_timer_received)],
            MIN_PLAYERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, min_players_received)],
            CONFIRM: [
                CallbackQueryHandler(setup_confirm_callback, pattern="^setup_confirm$"),
                CallbackQueryHandler(setup_cancel_callback, pattern="^setup_cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", setuptournament_cancel)],
        per_chat=True,
        per_user=True,
        name="tournament_setup_wizard",
    )

"""
handlers/captain.py
--------------------
Simple captain registration flow: player taps "Captain Registration" ->
bot asks for team name -> player confirms -> request is created as PENDING
(admin still approves via /admin or /approvecaptain). A confirmation notice
is also posted automatically to the configured group chat.
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler,
    MessageHandler, filters,
)

from config import settings
from database import queries as q
from keyboards.main import back_to_menu_keyboard
from services import captain_service
from utils.logger import get_logger

logger = get_logger(__name__)

TEAM_NAME, CONFIRM = range(2)


def _team_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm", callback_data="captain_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="captain_cancel"),
        ]
    ])


async def show_captain_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kept for compatibility; forwards straight into the registration flow."""
    await start_captain_flow(update, context)


async def start_captain_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user

    # A player who already registered as a PLAYER cannot also register as
    # captain, and vice versa — one Telegram account can only hold one role.
    existing_player = await q.get_player_by_telegram_id(user.id)
    if existing_player and existing_player["status"] != "REMOVED":
        text = (
            "❌ You are already registered as a <b>Player</b> "
            f"(#{existing_player['registration_no']}). A player cannot also "
            "become a captain."
        )
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                text, parse_mode="HTML", reply_markup=back_to_menu_keyboard()
            )
        else:
            await update.effective_message.reply_text(
                text, parse_mode="HTML", reply_markup=back_to_menu_keyboard()
            )
        return ConversationHandler.END

    existing_captain = await q.get_captain_by_telegram_id(user.id)
    if existing_captain and existing_captain["status"] in ("PENDING", "APPROVED"):
        text = (
            f"✅ You already have a captain request for team "
            f"<b>{existing_captain['team_name']}</b> (status: {existing_captain['status']})."
        )
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                text, parse_mode="HTML", reply_markup=back_to_menu_keyboard()
            )
        else:
            await update.effective_message.reply_text(
                text, parse_mode="HTML", reply_markup=back_to_menu_keyboard()
            )
        return ConversationHandler.END

    if not await captain_service.is_registration_open():
        text = "❌ Captain registration is currently closed by the admin."
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(text, reply_markup=back_to_menu_keyboard())
        else:
            await update.effective_message.reply_text(text, reply_markup=back_to_menu_keyboard())
        return ConversationHandler.END

    from services import subscription_service
    if subscription_service.is_subscription_required():
        check = await subscription_service.check_subscription(context, user.id)
        if not check["ok"]:
            text, keyboard = subscription_service.build_join_prompt(check)
            if update.callback_query:
                await update.callback_query.answer()
                await update.callback_query.edit_message_text(
                    text, parse_mode="HTML", reply_markup=keyboard
                )
            else:
                await update.effective_message.reply_text(
                    text, parse_mode="HTML", reply_markup=keyboard
                )
            return ConversationHandler.END

    prompt = "👑 <b>CAPTAIN REGISTRATION</b>\n\nWhat is your <b>team name</b>?"
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(prompt, parse_mode="HTML")
    else:
        await update.effective_message.reply_text(prompt, parse_mode="HTML")
    return TEAM_NAME


async def team_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    team_name = update.effective_message.text.strip()
    context.user_data["captain_team_name"] = team_name

    text = (
        "👑 <b>CONFIRM CAPTAIN REGISTRATION</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🏳️ Team Name: <b>{team_name}</b>\n\n"
        "Confirm to submit your captain request?"
    )
    await update.effective_message.reply_text(
        text, parse_mode="HTML", reply_markup=_team_confirm_keyboard()
    )
    return CONFIRM


async def captain_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user = update.effective_user
    team_name = context.user_data.get("captain_team_name")

    if not team_name:
        await query.answer()
        await query.edit_message_text("❌ Something went wrong. Please try again with /becomecaptain.")
        return ConversationHandler.END

    short_name = team_name.replace(" ", "")[:4].upper()
    captain_name = user.first_name + (f" {user.last_name}" if user.last_name else "")
    try:
        captain = await captain_service.request_captain(
            telegram_id=user.id,
            captain_name=captain_name,
            team_name=team_name,
            short_name=short_name,
            logo_file_id=None,
        )
    except captain_service.CaptainServiceError as e:
        await query.answer()
        await query.edit_message_text(f"❌ {e}")
        return ConversationHandler.END

    await query.answer("✅ Submitted!")
    await query.edit_message_text(
        "✅ <b>CAPTAIN REQUEST SUBMITTED</b>\n\n"
        f"🏳️ Team: {captain['team_name']}\n"
        "📌 Status: PENDING\n\n"
        "An admin will review your request shortly.",
        parse_mode="HTML",
    )

    # Post confirmation to the group automatically.
    if settings.group_chat_id:
        try:
            await context.bot.send_message(
                settings.group_chat_id,
                "👑 <b>NEW CAPTAIN REGISTRATION</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"👤 Captain: {captain['captain_name']}\n"
                f"🏳️ Team: {captain['team_name']}\n"
                "📌 Status: PENDING ADMIN APPROVAL",
                parse_mode="HTML",
            )
        except Exception:
            logger.warning("Could not post captain registration to group chat.")

    context.user_data.pop("captain_team_name", None)
    return ConversationHandler.END


async def captain_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("Cancelled.")
    await query.edit_message_text("❌ Captain registration cancelled.")
    context.user_data.pop("captain_team_name", None)
    return ConversationHandler.END


async def cancel_captain_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("Captain registration cancelled.")
    context.user_data.pop("captain_team_name", None)
    return ConversationHandler.END


def build_captain_conversation_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("becomecaptain", start_captain_flow),
            CallbackQueryHandler(start_captain_flow, pattern="^menu:captain$"),
        ],
        states={
            TEAM_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, team_name_received)],
            CONFIRM: [
                CallbackQueryHandler(captain_confirm_callback, pattern="^captain_confirm$"),
                CallbackQueryHandler(captain_cancel_callback, pattern="^captain_cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_captain_flow)],
        per_chat=True,
        per_user=True,
        name="captain_registration",
    )

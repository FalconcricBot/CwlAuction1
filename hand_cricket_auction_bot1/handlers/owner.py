"""
handlers/owner.py
------------------
Owner-only commands. Owners are the bot's developer/maintainer, configured
via OWNER_IDS in .env (fixed, cannot be changed at runtime for safety).
Owners can do everything admins can, plus:

  - /addadmin <telegram_id>    — grant admin access at runtime
  - /removeadmin <telegram_id> — revoke a runtime-granted admin
  - /listadmins                — see fixed (.env) and dynamic admins
  - /resetall                  — wipe all tournament data (destructive)
  - /botstats                  — bot health / database stats
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from config import settings
from database import db
from database import queries as q
from database.models import AuditAction
from utils.helpers import is_owner, safe_int
from utils.logger import get_logger

logger = get_logger(__name__)


async def _deny(update: Update) -> None:
    if update.callback_query:
        await update.callback_query.answer()
        return
    # Silent for owner-only commands too — non-owners shouldn't even learn
    # these commands exist.
    return


async def _require_owner(update: Update) -> bool:
    return is_owner(update.effective_user.id)


# ---------------------------------------------------------------------------
# Admin management
# ---------------------------------------------------------------------------

async def addadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(update):
        await _deny(update)
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /addadmin <telegram_id>")
        return
    telegram_id = safe_int(context.args[0])
    if telegram_id is None:
        await update.effective_message.reply_text("❌ Invalid Telegram ID.")
        return

    if telegram_id in settings.admin_ids or telegram_id in settings.owner_ids:
        await update.effective_message.reply_text("ℹ️ That user is already an admin.")
        return

    await q.add_dynamic_admin(telegram_id, update.effective_user.id)
    await q.add_audit_log(update.effective_user.id, AuditAction.SETTINGS_UPDATED,
                           details={"action": "add_admin", "telegram_id": telegram_id})
    await update.effective_message.reply_text(f"✅ Added {telegram_id} as an admin.")

    try:
        await context.bot.send_message(
            telegram_id,
            "🎉 You have been granted admin access to this auction bot by the owner."
        )
    except Exception:
        pass


async def removeadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(update):
        await _deny(update)
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /removeadmin <telegram_id>")
        return
    telegram_id = safe_int(context.args[0])
    if telegram_id is None:
        await update.effective_message.reply_text("❌ Invalid Telegram ID.")
        return

    if telegram_id in settings.admin_ids or telegram_id in settings.owner_ids:
        await update.effective_message.reply_text(
            "❌ That admin is set via .env (ADMIN_IDS/OWNER_IDS) and can't be "
            "removed at runtime — edit .env and restart the bot instead."
        )
        return

    removed = await q.remove_dynamic_admin(telegram_id)
    if not removed:
        await update.effective_message.reply_text("ℹ️ That user wasn't a runtime-added admin.")
        return

    await q.add_audit_log(update.effective_user.id, AuditAction.SETTINGS_UPDATED,
                           details={"action": "remove_admin", "telegram_id": telegram_id})
    await update.effective_message.reply_text(f"✅ Removed {telegram_id} from admins.")


async def listadmins_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(update):
        await _deny(update)
        return

    dynamic = await q.list_dynamic_admins()
    lines = ["🔐 <b>ADMINS</b>", "", "<b>Owners (.env, fixed):</b>"]
    for oid in settings.owner_ids:
        lines.append(f"  👑 {oid}")
    lines.append("")
    lines.append("<b>Admins (.env, fixed):</b>")
    if settings.admin_ids:
        for aid in settings.admin_ids:
            lines.append(f"  🔧 {aid}")
    else:
        lines.append("  <i>none</i>")
    lines.append("")
    lines.append("<b>Admins (added at runtime):</b>")
    if dynamic:
        for row in dynamic:
            lines.append(f"  🔧 {row['telegram_id']} (added {row['added_at']})")
    else:
        lines.append("  <i>none</i>")

    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Full reset
# ---------------------------------------------------------------------------

def _reset_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🗑️ Confirm Wipe", callback_data="ownerreset_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="ownerreset_cancel"),
        ]
    ])


async def resetall_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(update):
        await _deny(update)
        return

    await update.effective_message.reply_text(
        "⚠️ <b>FULL TOURNAMENT RESET</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "This will permanently delete ALL players, captains, teams, "
        "auctions, bids, sold/unsold history, and the audit log.\n\n"
        "Bot settings (purse, base price, timer, etc.) and admin list are "
        "kept. Registration numbers restart from #1 as part of this reset.\n\n"
        "<b>This cannot be undone.</b>",
        parse_mode="HTML",
        reply_markup=_reset_confirm_keyboard(),
    )


async def resetall_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await _require_owner(update):
        await _deny(update)
        return

    await db.reset_all_tournament_data(also_reset_counter=True)
    await q.set_setting("min_players_reached_notified", "0")
    await q.add_audit_log(update.effective_user.id, AuditAction.SETTINGS_UPDATED,
                           details={"action": "full_reset"})

    await query.answer("Wiped.")
    await query.edit_message_text(
        "🗑️ <b>ALL TOURNAMENT DATA WIPED</b>\n\n"
        "Players, captains, teams, and auction history have been cleared. "
        "Registration numbers restart from #1. Ready for a new tournament — "
        "try /setuptournament to configure it.",
        parse_mode="HTML",
    )
    logger.warning("FULL RESET performed by owner %s", update.effective_user.id)


async def resetall_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Cancelled.")
    await query.edit_message_text("❌ Reset cancelled. Nothing was deleted.")


# ---------------------------------------------------------------------------
# Bot stats / health
# ---------------------------------------------------------------------------

async def botstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(update):
        await _deny(update)
        return

    stats = await db.get_database_stats()
    db_size = db.get_database_file_size_bytes()
    db_size_str = f"{db_size / 1024:.1f} KB" if db_size < 1_048_576 else f"{db_size / 1_048_576:.2f} MB"

    auction_state = await q.get_auction_state()
    dynamic_admins = await q.list_dynamic_admins()

    lines = [
        "📊 <b>BOT STATUS</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"🗄️ Database size: {db_size_str}",
        f"🔧 Admins: {len(settings.admin_ids) + len(dynamic_admins)} "
        f"({len(settings.admin_ids)} fixed, {len(dynamic_admins)} runtime)",
        f"👑 Owners: {len(settings.owner_ids)}",
        "",
        f"👤 Players: {stats['players']}",
        f"👑 Captain requests: {stats['captains']}",
        f"🏳️ Teams: {stats['teams']}",
        f"🔨 Auction turns run: {stats['auctions']}",
        f"🪙 Bids placed: {stats['bids']}",
        f"📜 Audit log entries: {stats['audit_logs']}",
        f"👥 Known Telegram users: {stats['users']}",
        "",
        f"⚙️ Current auction state: <code>{auction_state['state']}</code>",
    ]
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


def register_owner_handlers(application) -> None:
    application.add_handler(CommandHandler("addadmin", addadmin_command))
    application.add_handler(CommandHandler("removeadmin", removeadmin_command))
    application.add_handler(CommandHandler("listadmins", listadmins_command))
    application.add_handler(CommandHandler("resetall", resetall_command))
    application.add_handler(CallbackQueryHandler(resetall_confirm_callback, pattern="^ownerreset_confirm$"))
    application.add_handler(CallbackQueryHandler(resetall_cancel_callback, pattern="^ownerreset_cancel$"))
    application.add_handler(CommandHandler("botstats", botstats_command))

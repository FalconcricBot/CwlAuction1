"""
handlers/admin.py
------------------
Admin panel (/admin) and all admin-only management commands:
player add/remove/restore/edit/search, captain approve/reject, team viewing,
settings, and CSV export.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime

from telegram import Update
from telegram.ext import (
    CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters,
)

from config import settings
from database import queries as q
from keyboards.admin import (
    admin_auction_menu_keyboard, admin_captain_menu_keyboard,
    admin_panel_keyboard, admin_player_menu_keyboard, captain_approval_keyboard,
    captain_delete_confirm_keyboard,
)
from services import captain_service, player_service
from utils.helpers import fmt_money, is_admin, player_display_name, safe_int
from utils.logger import get_logger

logger = get_logger(__name__)

ADD_PLAYER_ID, ADD_PLAYER_NAME = range(2)
IMPORT_FILE = range(100, 101)[0]


async def _require_admin(update: Update) -> bool:
    if is_admin(update.effective_user.id):
        return True
    from utils.helpers import is_admin_async
    return await is_admin_async(update.effective_user.id)


async def _deny(update: Update) -> None:
    """
    Silently ignore non-admin attempts at admin-only commands/callbacks.
    Admin commands should not be discoverable or usable by regular users.
    """
    if update.callback_query:
        await update.callback_query.answer()
    return


# ---------------------------------------------------------------------------
# /admin panel
# ---------------------------------------------------------------------------

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny(update)
        return
    await update.effective_message.reply_text(
        "🔐 <b>ADMIN PANEL</b>", parse_mode="HTML", reply_markup=admin_panel_keyboard()
    )


async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await _require_admin(update):
        await query.answer()
        return
    await query.answer()

    action = query.data.split(":")[1]
    if action == "home":
        await query.edit_message_text("🔐 <b>ADMIN PANEL</b>", parse_mode="HTML",
                                        reply_markup=admin_panel_keyboard())
    elif action == "players":
        await query.edit_message_text("👥 <b>PLAYER MANAGEMENT</b>", parse_mode="HTML",
                                        reply_markup=admin_player_menu_keyboard())
    elif action == "captains":
        await query.edit_message_text("👑 <b>CAPTAIN MANAGEMENT</b>", parse_mode="HTML",
                                        reply_markup=admin_captain_menu_keyboard())
    elif action == "teams":
        from handlers.team import teams_command
        await teams_command(update, context)
    elif action == "auction":
        await query.edit_message_text("🔨 <b>AUCTION MANAGEMENT</b>", parse_mode="HTML",
                                        reply_markup=admin_auction_menu_keyboard())
    elif action == "reports":
        await _show_reports(update, context)
    elif action == "settings":
        await _show_settings(update, context)


# ---------------------------------------------------------------------------
# Player management sub-menu
# ---------------------------------------------------------------------------

async def admin_player_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await _require_admin(update):
        await query.answer()
        return
    await query.answer()
    action = query.data.split(":")[1]

    if action == "view":
        players = await player_service.list_all_players()
        await _reply_player_list(query, players, "📋 ALL PLAYERS")
    elif action == "sold":
        rows = await q.list_sold_players()
        lines = ["✅ <b>SOLD PLAYERS</b>", ""]
        for p in rows:
            name = player_display_name(p["first_name"], p["last_name"])
            lines.append(f"#{p['registration_no']} {name} → {p['team_name']} — {fmt_money(p['sold_price'])}")
        await query.message.reply_text("\n".join(lines) or "No sold players.", parse_mode="HTML")
    elif action == "unsold":
        rows = await q.list_unsold_players()
        lines = ["❌ <b>UNSOLD PLAYERS</b>", ""]
        for p in rows:
            name = player_display_name(p["first_name"], p["last_name"])
            lines.append(f"#{p['registration_no']} {name}")
        await query.message.reply_text("\n".join(lines) or "No unsold players.", parse_mode="HTML")


async def _reply_player_list(query, players, title: str) -> None:
    lines = [f"{title}", ""]
    for p in players:
        name = player_display_name(p["first_name"], p["last_name"])
        lines.append(f"#{p['registration_no']} {name} — {p['status']}")
    text = "\n".join(lines) if players else f"{title}\n\nNo players found."
    if len(text) > 3900:
        text = text[:3800] + "\n\n… truncated, use /export"
    await query.message.reply_text(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# /addplayer conversation
# ---------------------------------------------------------------------------

async def addplayer_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_admin(update):
        await _deny(update)
        return ConversationHandler.END
    await update.effective_message.reply_text(
        "Send the player's Telegram numeric ID (or type 0 if unknown/placeholder):"
    )
    return ADD_PLAYER_ID


async def addplayer_id_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg_id = safe_int(update.effective_message.text.strip())
    if tg_id is None:
        await update.effective_message.reply_text("❌ Please send a valid numeric ID.")
        return ADD_PLAYER_ID
    context.user_data["add_player_tg_id"] = tg_id
    await update.effective_message.reply_text("Now send the player's full name:")
    return ADD_PLAYER_NAME


async def addplayer_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    full_name = update.effective_message.text.strip()
    parts = full_name.split(maxsplit=1)
    first_name = parts[0]
    last_name = parts[1] if len(parts) > 1 else None

    tg_id = context.user_data.pop("add_player_tg_id")
    if tg_id == 0:
        # Generate a synthetic unique negative ID for placeholder players
        # without a Telegram account yet.
        tg_id = -int(datetime.utcnow().timestamp() * 1000) % 2_147_000_000

    try:
        player = await player_service.admin_add_player(
            update.effective_user.id, tg_id, first_name, last_name
        )
    except player_service.PlayerServiceError as e:
        await update.effective_message.reply_text(f"❌ {e}")
        return ConversationHandler.END

    name = f"{first_name} {last_name or ''}".strip()
    await update.effective_message.reply_text(
        f"✅ Player added: #{player['registration_no']} {name}"
    )

    if settings.group_chat_id:
        try:
            await context.bot.send_message(
                settings.group_chat_id,
                "📝 <b>NEW PLAYER REGISTERED</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🔢 Registration No: #{player['registration_no']}\n"
                f"👤 Name: {name}",
                parse_mode="HTML",
            )
        except Exception:
            logger.warning("Could not post player registration to group chat.")

    return ConversationHandler.END


async def addplayer_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END


def build_addplayer_conversation_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("addplayer", addplayer_start)],
        states={
            ADD_PLAYER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, addplayer_id_received)],
            ADD_PLAYER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, addplayer_name_received)],
        },
        fallbacks=[CommandHandler("cancel", addplayer_cancel)],
        name="admin_add_player",
        per_chat=True, per_user=True,
    )


# ---------------------------------------------------------------------------
# /importplayers - bulk import players from a CSV file
# ---------------------------------------------------------------------------

async def importplayers_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_admin(update):
        await _deny(update)
        return ConversationHandler.END
    await update.effective_message.reply_text(
        "📥 <b>BULK IMPORT PLAYERS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send a CSV file with these columns (header row required):\n"
        "<code>telegram_id,first_name,last_name,category</code>\n\n"
        "• <code>telegram_id</code>: numeric, or 0/blank for a placeholder ID\n"
        "• <code>last_name</code> and <code>category</code> are optional\n\n"
        "Example row:\n<code>123456789,Rahul,Sharma,Batsman</code>\n\n"
        "Send /cancel to stop.",
        parse_mode="HTML",
    )
    return IMPORT_FILE


async def importplayers_file_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    document = update.effective_message.document
    if document is None:
        await update.effective_message.reply_text("❌ Please send a .csv file, or /cancel.")
        return IMPORT_FILE

    if not (document.file_name or "").lower().endswith(".csv"):
        await update.effective_message.reply_text("❌ That doesn't look like a .csv file. Please try again.")
        return IMPORT_FILE

    tg_file = await context.bot.get_file(document.file_id)
    raw_bytes = await tg_file.download_as_bytearray()
    try:
        text = bytes(raw_bytes).decode("utf-8-sig")
    except UnicodeDecodeError:
        await update.effective_message.reply_text("❌ Could not read the file as UTF-8 text.")
        return ConversationHandler.END

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        await update.effective_message.reply_text("❌ The CSV file appears to be empty.")
        return ConversationHandler.END

    normalized_fields = {f.strip().lower() for f in reader.fieldnames}
    if "first_name" not in normalized_fields:
        await update.effective_message.reply_text(
            "❌ CSV must have at least a 'first_name' column."
        )
        return ConversationHandler.END

    admin_id = update.effective_user.id
    added, skipped, errors = 0, 0, []
    from datetime import datetime as _dt

    for i, row in enumerate(reader, start=2):  # row 1 is the header
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        first_name = row.get("first_name", "")
        if not first_name:
            skipped += 1
            errors.append(f"Row {i}: missing first_name")
            continue

        last_name = row.get("last_name") or None
        category = row.get("category") or None
        tg_id_raw = row.get("telegram_id", "")

        tg_id = safe_int(tg_id_raw)
        if tg_id is None or tg_id == 0:
            tg_id = -int(_dt.utcnow().timestamp() * 1000000) % 2_147_000_000 - i

        try:
            player = await player_service.admin_add_player(admin_id, tg_id, first_name, last_name)
            if category:
                await q.set_player_category(player["id"], category)
            added += 1
        except player_service.PlayerServiceError as e:
            skipped += 1
            errors.append(f"Row {i} ({first_name}): {e}")

    summary_lines = [
        "✅ <b>IMPORT COMPLETE</b>",
        "",
        f"Added: {added}",
        f"Skipped: {skipped}",
    ]
    if errors:
        summary_lines.append("")
        summary_lines.append("<b>Issues:</b>")
        for err in errors[:15]:
            summary_lines.append(f"• {err}")
        if len(errors) > 15:
            summary_lines.append(f"… and {len(errors) - 15} more")

    text = "\n".join(summary_lines)
    if len(text) > 3900:
        text = text[:3800] + "\n\n… (truncated)"
    await update.effective_message.reply_text(text, parse_mode="HTML")

    if added and settings.group_chat_id:
        try:
            await context.bot.send_message(
                settings.group_chat_id,
                f"📥 <b>{added} players bulk-imported by admin.</b>",
                parse_mode="HTML",
            )
        except Exception:
            pass

    return ConversationHandler.END


async def importplayers_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("Import cancelled.")
    return ConversationHandler.END


def build_importplayers_conversation_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("importplayers", importplayers_start)],
        states={
            IMPORT_FILE: [MessageHandler(filters.Document.ALL, importplayers_file_received)],
        },
        fallbacks=[CommandHandler("cancel", importplayers_cancel)],
        name="admin_import_players",
        per_chat=True, per_user=True,
    )


# ---------------------------------------------------------------------------
# /removeplayer /restoreplayer /editplayer /searchplayer /players
# ---------------------------------------------------------------------------

async def deleteplayer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Alias for /removeplayer (soft delete — does not erase history)."""
    await removeplayer_command(update, context)


async def removeplayer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny(update)
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /removeplayer <registration_no>")
        return
    reg_no = safe_int(context.args[0])
    if reg_no is None:
        await update.effective_message.reply_text("❌ Invalid registration number.")
        return
    player = await q.get_player_by_registration_no(reg_no)
    if player is None:
        await update.effective_message.reply_text("❌ Player not found.")
        return
    try:
        updated = await player_service.remove_player(update.effective_user.id, player["id"])
    except player_service.PlayerServiceError as e:
        await update.effective_message.reply_text(f"❌ {e}")
        return
    name = player_display_name(updated["first_name"], updated["last_name"])
    await update.effective_message.reply_text(f"🚫 #{reg_no} {name} has been removed.")


async def restoreplayer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny(update)
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /restoreplayer <registration_no>")
        return
    reg_no = safe_int(context.args[0])
    if reg_no is None:
        await update.effective_message.reply_text("❌ Invalid registration number.")
        return
    player = await q.get_player_by_registration_no(reg_no)
    if player is None:
        await update.effective_message.reply_text("❌ Player not found.")
        return
    try:
        updated = await player_service.restore_player(update.effective_user.id, player["id"])
    except player_service.PlayerServiceError as e:
        await update.effective_message.reply_text(f"❌ {e}")
        return
    name = player_display_name(updated["first_name"], updated["last_name"])
    await update.effective_message.reply_text(f"✅ #{reg_no} {name} has been restored.")


async def editplayer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny(update)
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "Usage: /editplayer <registration_no> <new_first_name> [new_last_name]"
        )
        return
    reg_no = safe_int(context.args[0])
    if reg_no is None:
        await update.effective_message.reply_text("❌ Invalid registration number.")
        return
    player = await q.get_player_by_registration_no(reg_no)
    if player is None:
        await update.effective_message.reply_text("❌ Player not found.")
        return

    first_name = context.args[1]
    last_name = " ".join(context.args[2:]) if len(context.args) > 2 else None
    updated = await player_service.edit_player(
        update.effective_user.id, player["id"], first_name=first_name, last_name=last_name
    )
    name = player_display_name(updated["first_name"], updated["last_name"])
    await update.effective_message.reply_text(f"✅ #{reg_no} updated: {name}")


async def searchplayer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny(update)
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /searchplayer <name or registration_no>")
        return
    query_text = " ".join(context.args)
    results = await player_service.search_players(query_text)
    if not results:
        await update.effective_message.reply_text("No matching players found.")
        return
    lines = ["🔎 <b>SEARCH RESULTS</b>", ""]
    for p in results:
        name = player_display_name(p["first_name"], p["last_name"])
        lines.append(f"#{p['registration_no']} {name} — {p['status']}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def admin_players_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny(update)
        return
    players = await player_service.list_all_players(include_removed=True)
    lines = ["👥 <b>ALL PLAYERS</b> (incl. removed)", ""]
    for p in players:
        name = player_display_name(p["first_name"], p["last_name"])
        lines.append(f"#{p['registration_no']} {name} — {p['status']}")
    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3800] + "\n\n… truncated, use /export"
    await update.effective_message.reply_text(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Captain approval
# ---------------------------------------------------------------------------

async def captains_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny(update)
        return
    await _show_pending_captains(update.effective_message)


async def admin_captain_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await _require_admin(update):
        await query.answer()
        return
    await query.answer()
    action = query.data.split(":")[1]

    if action == "pending":
        await _show_pending_captains(query.message)
    elif action == "all":
        captains = await captain_service.list_all_captains()
        lines = ["👑 <b>ALL CAPTAINS</b>", ""]
        for c in captains:
            lines.append(f"ID {c['id']} — {c['captain_name']} — {c['team_name']} ({c['status']})")
        await query.message.reply_text("\n".join(lines) or "No captains yet.", parse_mode="HTML")


async def _show_pending_captains(message) -> None:
    pending = await captain_service.list_pending_captains()
    if not pending:
        await message.reply_text("⏳ No pending captain requests.")
        return
    for c in pending:
        text = (
            f"👑 <b>{c['captain_name']}</b>\n"
            f"🏳️ Team: {c['team_name']} ({c['short_name']})\n"
            f"🆔 Captain ID: {c['id']}\n"
            f"📌 Status: {c['status']}"
        )
        await message.reply_text(text, parse_mode="HTML",
                                  reply_markup=captain_approval_keyboard(c["id"]))


async def captain_approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await _require_admin(update):
        await query.answer()
        return
    captain_id = int(query.data.split(":")[1])
    try:
        captain, team = await captain_service.approve_captain(update.effective_user.id, captain_id)
    except captain_service.CaptainServiceError as e:
        await query.answer(str(e), show_alert=True)
        return
    await query.answer("✅ Approved")
    await query.edit_message_text(
        f"✅ <b>{captain['captain_name']}</b> approved.\n"
        f"🏳️ Team '{team['name']}' created with purse {fmt_money(team['starting_purse'])}.",
        parse_mode="HTML",
    )
    try:
        await context.bot.send_message(
            captain["telegram_id"],
            f"🎉 Your captain request has been APPROVED! Your team '{team['name']}' is ready.\n"
            f"Purse: {fmt_money(team['starting_purse'])}",
        )
    except Exception:
        logger.warning("Could not DM captain %s about approval", captain["telegram_id"])

    from handlers.auction import refresh_lobby_message
    await refresh_lobby_message(context)


async def captain_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await _require_admin(update):
        await query.answer()
        return
    captain_id = int(query.data.split(":")[1])
    try:
        captain = await captain_service.reject_captain(update.effective_user.id, captain_id)
    except captain_service.CaptainServiceError as e:
        await query.answer(str(e), show_alert=True)
        return
    await query.answer("Rejected")
    await query.edit_message_text(f"❌ {captain['captain_name']}'s request rejected.")
    try:
        await context.bot.send_message(
            captain["telegram_id"], "❌ Your captain request was not approved."
        )
    except Exception:
        pass


async def approvecaptain_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny(update)
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /approvecaptain <captain_id>")
        return
    captain_id = safe_int(context.args[0])
    if captain_id is None:
        await update.effective_message.reply_text("❌ Invalid captain id.")
        return
    try:
        captain, team = await captain_service.approve_captain(update.effective_user.id, captain_id)
    except captain_service.CaptainServiceError as e:
        await update.effective_message.reply_text(f"❌ {e}")
        return
    await update.effective_message.reply_text(
        f"✅ {captain['captain_name']} approved. Team '{team['name']}' created."
    )
    from handlers.auction import refresh_lobby_message
    await refresh_lobby_message(context)


async def rejectcaptain_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny(update)
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /rejectcaptain <captain_id>")
        return
    captain_id = safe_int(context.args[0])
    if captain_id is None:
        await update.effective_message.reply_text("❌ Invalid captain id.")
        return
    try:
        captain = await captain_service.reject_captain(update.effective_user.id, captain_id)
    except captain_service.CaptainServiceError as e:
        await update.effective_message.reply_text(f"❌ {e}")
        return
    await update.effective_message.reply_text(f"❌ {captain['captain_name']} rejected.")


# ---------------------------------------------------------------------------
# /deletecaptain - remove a mistakenly-registered captain (pre-auction cleanup)
# ---------------------------------------------------------------------------

async def deletecaptain_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny(update)
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /deletecaptain <captain_id>\n"
            "Tip: use /captains to see pending requests, or the Captain "
            "Management menu in /admin for the full list with IDs."
        )
        return
    captain_id = safe_int(context.args[0])
    if captain_id is None:
        await update.effective_message.reply_text("❌ Invalid captain id.")
        return

    captain = await q.get_captain_by_id(captain_id)
    if captain is None:
        await update.effective_message.reply_text("❌ Captain not found.")
        return

    warning_lines = [
        "⚠️ <b>DELETE CAPTAIN?</b>",
        "",
        f"👑 {captain['captain_name']}",
        f"🏳️ Team: {captain['team_name']}",
        f"📌 Status: {captain['status']}",
        "",
        "This will remove them as captain",
    ]
    if captain["team_id"] is not None:
        squad = await q.get_team_squad(captain["team_id"])
        if squad:
            warning_lines.append(
                f"and return {len(squad)} already-bought player(s) to the pool "
                "with a full purse refund."
            )
        else:
            warning_lines.append("and deactivate their (empty) team.")
    else:
        warning_lines.append("(no team was created yet).")

    await update.effective_message.reply_text(
        "\n".join(warning_lines), parse_mode="HTML",
        reply_markup=captain_delete_confirm_keyboard(captain_id),
    )


async def captain_delete_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await _require_admin(update):
        await query.answer()
        return

    captain_id = int(query.data.split(":")[1])
    try:
        result = await captain_service.delete_captain(update.effective_user.id, captain_id)
    except captain_service.CaptainServiceError as e:
        await query.answer(str(e), show_alert=True)
        return

    await query.answer("Deleted")
    captain = result["captain"]
    returned = result["returned_players"]
    lines = [f"✅ {captain['captain_name']} removed as captain."]
    if returned:
        names = ", ".join(
            player_display_name(p["first_name"], p["last_name"]) for p in returned
        )
        lines.append(f"↩️ Returned to pool with refund: {names}")
    await query.edit_message_text("\n".join(lines), parse_mode="HTML")

    try:
        await context.bot.send_message(
            captain["telegram_id"],
            "ℹ️ You have been removed as a captain by an admin. If this was a "
            "mistake, you can register again with /becomecaptain or as a "
            "player with /register.",
        )
    except Exception:
        pass

    from handlers.auction import refresh_lobby_message
    await refresh_lobby_message(context)


async def captain_delete_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Cancelled.")
    await query.edit_message_text("❌ Delete cancelled.")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

async def _show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from html import escape
    all_settings = await q.get_all_settings()
    lines = ["⚙️ <b>SETTINGS</b>", ""]
    for key, value in all_settings.items():
        lines.append(f"{escape(str(key))} = {escape(str(value))}")
    lines.append("")
    lines.append("To change a setting:\n<code>/setconfig key value</code>")
    await update.callback_query.message.reply_text("\n".join(lines), parse_mode="HTML")


async def setconfig_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny(update)
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "Usage: /setconfig <key> <value>\n\n"
            "Valid keys: starting_purse, base_price, auction_timer, "
            "max_squad_size, max_retentions, max_players_per_set, captain_registration_open, "
            "min_players_to_start, max_teams"
        )
        return
    key, value = context.args[0], context.args[1]
    valid_keys = {
        "starting_purse", "base_price", "auction_timer",
        "max_squad_size", "max_retentions", "max_players_per_set", "captain_registration_open",
        "min_players_to_start", "max_teams",
    }
    if key not in valid_keys:
        await update.effective_message.reply_text(f"❌ Unknown setting key: {key}")
        return
    if key != "captain_registration_open" and safe_int(value) is None:
        await update.effective_message.reply_text("❌ Value must be a number.")
        return

    await q.set_setting(key, value)
    from database.models import AuditAction
    await q.add_audit_log(update.effective_user.id, AuditAction.SETTINGS_UPDATED,
                           details={"key": key, "value": value})
    await update.effective_message.reply_text(f"✅ {key} set to {value}")


# ---------------------------------------------------------------------------
# Registration deadline
# ---------------------------------------------------------------------------

async def setregdeadline_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny(update)
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /setregdeadline <YYYY-MM-DD HH:MM> or 'off' to remove the deadline\n"
            "Example: /setregdeadline 2026-09-20 18:00\n\n"
            "Times are treated as UTC. Once the deadline passes, /register "
            "will be blocked automatically."
        )
        return

    raw = " ".join(context.args).strip()
    if raw.lower() in ("off", "none", "clear", "remove"):
        await q.set_setting("registration_deadline", "")
        await update.effective_message.reply_text("✅ Registration deadline removed.")
        return

    from datetime import datetime
    parsed = None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        await update.effective_message.reply_text(
            "❌ Couldn't parse that date/time. Use format: YYYY-MM-DD HH:MM (UTC)"
        )
        return

    await q.set_setting("registration_deadline", parsed.isoformat())
    from database.models import AuditAction
    await q.add_audit_log(update.effective_user.id, AuditAction.SETTINGS_UPDATED,
                           details={"key": "registration_deadline", "value": parsed.isoformat()})
    await update.effective_message.reply_text(
        f"✅ Registration deadline set to {parsed.isoformat()} UTC.\n"
        "Registration will automatically close after this time."
    )


async def regdeadline_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deadline_raw = await q.get_setting("registration_deadline")
    if not deadline_raw:
        await update.effective_message.reply_text("📥 No registration deadline is set.")
        return
    await update.effective_message.reply_text(f"📥 Registration closes at: {deadline_raw} UTC")


# ---------------------------------------------------------------------------
# /setlanguage - toggle the bot's player-facing language (English/Hindi)
# ---------------------------------------------------------------------------

async def setlanguage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny(update)
        return

    from utils.i18n import available_languages
    langs = available_languages()

    if not context.args or context.args[0].lower() not in langs:
        await update.effective_message.reply_text(
            f"Usage: /setlanguage <{'|'.join(langs)}>\nExample: /setlanguage hi"
        )
        return

    lang = context.args[0].lower()
    await q.set_setting("bot_language", lang)
    from database.models import AuditAction
    await q.add_audit_log(update.effective_user.id, AuditAction.SETTINGS_UPDATED,
                           details={"key": "bot_language", "value": lang})

    names = {"en": "English", "hi": "हिंदी (Hindi)"}
    await update.effective_message.reply_text(f"✅ Language set to {names.get(lang, lang)}.")


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

async def _show_reports(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from handlers.auction import endauction_command
    # Reuse the final report logic without ending the auction state.
    counts = await q.count_players_by_status()
    teams_data = await captain_service.list_teams_with_squad()

    lines = [
        "📊 <b>AUCTION REPORT</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"Total Registered:\n{sum(counts.values())}",
        f"Total Sold:\n{counts.get('SOLD', 0)}",
        f"Total Unsold:\n{counts.get('UNSOLD', 0)}",
        f"Total Removed:\n{counts.get('REMOVED', 0)}",
        f"Teams:\n{len(teams_data)}",
    ]
    await update.callback_query.message.reply_text("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# /export - CSV export
# ---------------------------------------------------------------------------

async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny(update)
        return

    players = await player_service.list_all_players(include_removed=True)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "registration_no", "player_name", "team", "sold_price", "status",
        "auction_set", "timestamp",
    ])

    for p in players:
        team_name = ""
        if p["team_id"]:
            team = await q.get_team_by_id(p["team_id"])
            team_name = team["name"] if team else ""

        set_number = ""
        tx = await q.get_sale_transaction_for_player(p["id"])
        if tx and tx["set_id"]:
            auction_set = await q.get_auction_set_by_id(tx["set_id"])
            set_number = auction_set["set_number"] if auction_set else ""

        writer.writerow([
            p["registration_no"],
            player_display_name(p["first_name"], p["last_name"]),
            team_name,
            p["sold_price"] or "",
            p["status"],
            set_number,
            p["updated_at"],
        ])

    buffer.seek(0)
    data_bytes = io.BytesIO(buffer.getvalue().encode("utf-8"))
    data_bytes.name = f"auction_export_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"

    await update.effective_message.reply_document(
        document=data_bytes, filename=data_bytes.name,
        caption="📊 Auction data export",
    )


# ---------------------------------------------------------------------------
# /exportpdf - final auction report as a PDF
# ---------------------------------------------------------------------------

async def exportpdf_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny(update)
        return

    try:
        from fpdf import FPDF
    except ImportError:
        await update.effective_message.reply_text(
            "❌ PDF export requires the 'fpdf2' package. Install it with:\n"
            "pip install fpdf2 --break-system-packages"
        )
        return

    def pdf_money(amount) -> str:
        """
        fpdf2's built-in Helvetica font only supports Latin-1, and ₹ falls
        outside that range (Latin-1 has no rupee sign), so using fmt_money()
        directly in the PDF crashes. This renders amounts as "Rs. 1,234"
        instead, safe for the built-in font.
        """
        if amount is None:
            return "Rs. 0"
        return f"Rs. {amount:,}"

    counts = await player_service.get_status_counts()
    teams_data = await captain_service.list_teams_with_squad()

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 12, "Hand Cricket Auction - Final Report", ln=True, align="C")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 8, f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC", ln=True, align="C")
    pdf.ln(6)

    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 10, "Summary", ln=True)
    pdf.set_font("Helvetica", "", 11)
    total = sum(counts.values())
    for label, value in [
        ("Total Registered", total),
        ("Sold", counts.get("SOLD", 0)),
        ("Unsold", counts.get("UNSOLD", 0)),
        ("Removed", counts.get("REMOVED", 0)),
        ("Teams", len(teams_data)),
    ]:
        pdf.cell(0, 7, f"{label}: {value}", ln=True)
    pdf.ln(4)

    for entry in teams_data:
        team, squad = entry["team"], entry["squad"]
        pdf.set_font("Helvetica", "B", 13)
        team_line = f"{team['name']} ({team['short_name']})".encode("latin-1", "replace").decode("latin-1")
        pdf.cell(0, 9, team_line, ln=True)
        pdf.set_font("Helvetica", "", 10)
        spent = team["starting_purse"] - team["remaining_purse"]
        pdf.cell(0, 6, f"Spent: {pdf_money(spent)}   Remaining: {pdf_money(team['remaining_purse'])}"
                        f"   Squad: {q.team_member_count(team, squad)}/{team['max_squad_size']}", ln=True)
        pdf.ln(1)
        if squad:
            pdf.set_font("Helvetica", "B", 10)
            pdf.cell(90, 6, "Player", border=0)
            pdf.cell(0, 6, "Price", border=0, ln=True)
            pdf.set_font("Helvetica", "", 10)
            for p in squad:
                name = player_display_name(p["first_name"], p["last_name"])
                safe_name = name.encode("latin-1", "replace").decode("latin-1")
                pdf.cell(90, 6, f"  {safe_name}", border=0)
                pdf.cell(0, 6, pdf_money(p["sold_price"]), border=0, ln=True)
        pdf.ln(4)

    pdf_bytes = bytes(pdf.output())
    buffer = io.BytesIO(pdf_bytes)
    filename = f"auction_report_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.pdf"
    buffer.name = filename

    await update.effective_message.reply_document(
        document=buffer, filename=filename, caption="📄 Final auction report (PDF)",
    )


# ---------------------------------------------------------------------------
# /backup - send the raw SQLite database file as a downloadable backup
# ---------------------------------------------------------------------------

async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny(update)
        return

    from config import settings as app_settings
    import os

    db_path = app_settings.database_path
    if not os.path.exists(db_path):
        await update.effective_message.reply_text("❌ Database file not found on disk.")
        return

    # Ensure everything is flushed to the main DB file (WAL checkpoint)
    # before copying, so the backup isn't missing recent writes that are
    # still sitting in the -wal file.
    from database import db as db_module
    conn = db_module.get_connection()
    try:
        conn.execute("PRAGMA wal_checkpoint(FULL);")
    except Exception:
        pass

    file_size = os.path.getsize(db_path)
    filename = f"backup_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.db"

    with open(db_path, "rb") as f:
        await update.effective_message.reply_document(
            document=f, filename=filename,
            caption=f"💾 Database backup ({file_size / 1024:.1f} KB)\n"
                    "Keep this safe — it contains every player, team, bid, and history record.",
        )


# ---------------------------------------------------------------------------
# /dashboard - generate a static HTML snapshot of the live auction
# ---------------------------------------------------------------------------

async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny(update)
        return

    from services import dashboard_service

    html = await dashboard_service.generate_dashboard_html()

    # The filename itself is the access control: a random, unguessable
    # token means whoever hosts this file publicly (GitHub Pages, Netlify
    # drop, a personal server, etc.) isn't exposing it at a predictable
    # URL. There's no server-side auth possible for a static file, so this
    # is the practical equivalent of a secret link.
    token = await q.get_setting("dashboard_token")
    if not token:
        import secrets
        token = secrets.token_urlsafe(24)
        await q.set_setting("dashboard_token", token)

    filename = f"auction-dashboard-{token[:16]}.html"
    buffer = io.BytesIO(html.encode("utf-8"))
    buffer.name = filename

    await update.effective_message.reply_document(
        document=buffer, filename=filename,
        caption=(
            "📊 <b>Live Auction Dashboard</b>\n\n"
            "This is a self-contained HTML file — upload it anywhere "
            "(a personal server, Netlify drop, GitHub Pages, etc.) and "
            "share that link.\n\n"
            "It's a snapshot of right now and refreshes itself in the "
            "browser every 30s, but the data only updates when you "
            "re-run /dashboard and re-upload the file to replace the old "
            "one.\n\n"
            "🔒 Keep the filename private — its random token is the only "
            "thing keeping this page from being guessable if hosted "
            "publicly. Use /dashboard_reset to invalidate the current "
            "link and get a fresh one."
        ),
        parse_mode="HTML",
    )


async def dashboard_reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        await _deny(update)
        return

    import secrets
    new_token = secrets.token_urlsafe(24)
    await q.set_setting("dashboard_token", new_token)
    await update.effective_message.reply_text(
        "🔄 Dashboard token rotated. Run /dashboard to get a new file "
        "with a new, unguessable filename.\n\n"
        "⚠️ Note: since this is a static file, any copy someone already "
        "downloaded or uploaded elsewhere still works and shows whatever "
        "data it had — rotating the token only affects future /dashboard "
        "files, it can't revoke access to a file that's already out there."
    )
"""
main.py
-------
Application entry point. Builds the Application, initializes the database,
registers every handler, and starts polling.
"""
from __future__ import annotations

from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    ContextTypes,
)

from config import settings
from database import db
from database import queries as q
from utils.logger import get_logger

from handlers.start import start_command, help_command, allcommands_command, main_menu_callback
from handlers.registration import (
    register_command, profile_command, players_command, player_category_callback,
)
from handlers.captain import build_captain_conversation_handler
from handlers.retention import retain_command, unretain_command, retained_command
from handlers.team import teams_command, my_team_command, my_purse_command, setvicecaptain_command
from handlers.admin import (
    admin_command, admin_panel_callback, admin_player_callback,
    build_addplayer_conversation_handler, removeplayer_command, deleteplayer_command,
    restoreplayer_command,
    editplayer_command, searchplayer_command, admin_players_command,
    captains_command, admin_captain_callback, captain_approve_callback,
    captain_reject_callback, approvecaptain_command, rejectcaptain_command,
    deletecaptain_command, captain_delete_confirm_callback, captain_delete_cancel_callback,
    setconfig_command, export_command,
    setregdeadline_command, regdeadline_command, setlanguage_command, exportpdf_command,
    build_importplayers_conversation_handler, backup_command,
    dashboard_command, dashboard_reset_command,
)
from handlers.auction import (
    set_command, set_start_callback, showset_command, pause_command, resume_command,
    next_command, next_confirm_callback, next_cancel_callback, next_outcome_callback,
    undo_command, undo_confirm_callback, undo_cancel_callback,
    returnplayer_command, return_confirm_callback, return_cancel_callback,
    reauction_queue_command, reauction_start_callback,
    unsold_command, unsold_list_command, unsold_start_callback, auctionplayer_command,
    auction_status_command, sold_command, auction_history_command,
    startauction_command, endauction_command,
    lobby_command, lobby_page_callback, lobby_refresh_callback,
    lobby_start_callback, lobby_cancel_callback,
    bidhistory_command, topsold_command, teamcompare_command,
    blockteam_command, unblockteam_command,
    tradeplayer_command, trade_confirm_callback, trade_cancel_callback,
    remind_command,
)
from handlers.bidding import bid_command, allin_command
from handlers.setup_wizard import build_setup_wizard_handler
from handlers.owner import register_owner_handlers

logger = get_logger(__name__)


async def _post_init(application: Application) -> None:
    await db.init_db()
    await q.ensure_default_settings()

    # Only expose player-facing commands in the default command menu (the
    # "/" suggestions), so admin-only commands aren't advertised to regular
    # users. Each admin additionally gets the full admin command list scoped
    # to their private chat with the bot.
    from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

    player_commands = [
        BotCommand("start", "Main menu"),
        BotCommand("allcmd", "See every command in this bot"),
        BotCommand("register", "Register as a player"),
        BotCommand("becomecaptain", "Register as a captain"),
        BotCommand("profile", "View your profile"),
        BotCommand("team", "View your team (captains)"),
        BotCommand("mypurse", "View your remaining purse (captains)"),
        BotCommand("setvicecaptain", "Set/clear your team's vice-captain (captains)"),
        BotCommand("bid", "Place a bid (captains): /bid <amount>"),
        BotCommand("allin", "Bid your entire remaining purse (captains)"),
        BotCommand("bidhistory", "Bid history for a specific player"),
        BotCommand("topsold", "Highest sold players leaderboard"),
        BotCommand("teamcompare", "Compare team spending"),
        BotCommand("regdeadline", "View the registration deadline"),
    ]
    await application.bot.set_my_commands(player_commands, scope=BotCommandScopeDefault())

    admin_commands = player_commands + [
        BotCommand("admin", "Open the admin panel"),
        BotCommand("addplayer", "Add a player"),
        BotCommand("importplayers", "Bulk import players from a CSV file"),
        BotCommand("removeplayer", "Remove (soft-delete) a player"),
        BotCommand("deleteplayer", "Delete (soft-delete) a player"),
        BotCommand("restoreplayer", "Restore a removed player"),
        BotCommand("editplayer", "Edit a player"),
        BotCommand("searchplayer", "Search players"),
        BotCommand("allplayers", "List all players"),
        BotCommand("captains", "Manage captain requests"),
        BotCommand("approvecaptain", "Approve a captain"),
        BotCommand("rejectcaptain", "Reject a captain"),
        BotCommand("deletecaptain", "Remove a mistakenly-registered captain"),
        BotCommand("teams", "List all teams"),
        BotCommand("startauction", "Start the auction"),
        BotCommand("lobby", "Post/pin the live lobby status in the group"),
        BotCommand("set", "Build/show an auction set"),
        BotCommand("showset", "Show full player list for a set"),
        BotCommand("pause", "Pause the auction"),
        BotCommand("resume", "Resume the auction"),
        BotCommand("next", "Skip current player"),
        BotCommand("nextplayer", "Skip current player (alias of /next)"),
        BotCommand("undo", "Undo last sale"),
        BotCommand("returnplayer", "Return a sold player"),
        BotCommand("tradeplayer", "Trade a sold player between teams"),
        BotCommand("retain", "Retain a player for a team (pre-auction)"),
        BotCommand("unretain", "Remove a player's retention"),
        BotCommand("reauction_queue", "View re-auction queue"),
        BotCommand("unsold", "Run unsold-round auction"),
        BotCommand("auctionplayer", "Jump straight to a specific player by registration no."),
        BotCommand("unsold_list", "List unsold players"),
        BotCommand("auction_status", "Live auction status"),
        BotCommand("remind", "Schedule a reminder message in this chat"),
        BotCommand("sold", "List sold players"),
        BotCommand("auctionhistory", "Full auction history"),
        BotCommand("bidhistory", "Bid history for a specific player"),
        BotCommand("topsold", "Highest sold players leaderboard"),
        BotCommand("teamcompare", "Compare team spending"),
        BotCommand("blockteam", "Temporarily block a team from bidding"),
        BotCommand("unblockteam", "Lift a bidding block on a team"),
        BotCommand("setconfig", "Update a setting"),
        BotCommand("setregdeadline", "Set/clear the registration deadline"),
        BotCommand("setlanguage", "Set bot language (en/hi)"),
        BotCommand("setuptournament", "Guided step-by-step tournament setup wizard"),
        BotCommand("export", "Export auction data (CSV)"),
        BotCommand("exportpdf", "Export final report (PDF)"),
        BotCommand("backup", "Download a full database backup"),
        BotCommand("dashboard", "Generate a live auction dashboard HTML file"),
        BotCommand("dashboard_reset", "Rotate the dashboard file's secret token"),
        BotCommand("endauction", "End the auction"),
    ]
    for admin_id in settings.admin_ids:
        try:
            await application.bot.set_my_commands(
                admin_commands, scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except Exception:
            logger.warning("Could not set admin command menu for %s (they may not "
                            "have started a chat with the bot yet).", admin_id)

    owner_commands = admin_commands + [
        BotCommand("addadmin", "Grant admin access at runtime"),
        BotCommand("removeadmin", "Revoke a runtime-granted admin"),
        BotCommand("listadmins", "List all owners/admins"),
        BotCommand("resetall", "Wipe all tournament data (destructive)"),
        BotCommand("botstats", "Bot health / database stats"),
    ]
    for owner_id in settings.owner_ids:
        try:
            await application.bot.set_my_commands(
                owner_commands, scope=BotCommandScopeChat(chat_id=owner_id)
            )
        except Exception:
            logger.warning("Could not set owner command menu for %s (they may not "
                            "have started a chat with the bot yet).", owner_id)

    logger.info("Bot initialized. Admins: %s, Owners: %s", settings.admin_ids, settings.owner_ids)


async def _error_handler(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Global fallback error handler. Ensures no single exception (a flood
    control error, a transient network blip, a bad callback, etc.) ever
    crashes the bot or leaves an update unhandled. We log everything and,
    where possible, let the user know something went wrong without leaking
    internals. Owners additionally get the real exception message inline,
    to make debugging in production fast without needing log file access.
    """
    import traceback
    from telegram.error import RetryAfter, TelegramError

    err = context.error
    logger.error("Unhandled exception while processing an update", exc_info=err)

    if isinstance(err, RetryAfter):
        # Flood control: nothing to notify the user about, just log and move on.
        return

    error_text = "".join(traceback.format_exception_only(type(err), err)).strip()
    from html import escape
    error_text_safe = escape(error_text)

    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong processing that. Please try again."
            )
        elif isinstance(update, Update) and update.callback_query:
            await update.callback_query.answer(
                "⚠️ Something went wrong. Please try again.", show_alert=True
            )
    except TelegramError:
        pass  # Never let error-reporting itself raise.

    if isinstance(update, Update) and update.effective_user:
        from utils.helpers import is_owner
        if is_owner(update.effective_user.id):
            try:
                await context.bot.send_message(
                    update.effective_user.id,
                    f"🐞 <b>Debug (owner only)</b>\n<code>{error_text_safe[:900]}</code>",
                    parse_mode="HTML",
                )
            except TelegramError:
                pass


def build_application() -> Application:
    application = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .post_init(_post_init)
        .build()
    )

    # --- Player commands -----------------------------------------------
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("allcmd", allcommands_command))
    application.add_handler(CommandHandler("register", register_command))
    application.add_handler(CommandHandler("profile", profile_command))
    application.add_handler(CommandHandler("players", players_command))
    application.add_handler(CallbackQueryHandler(player_category_callback, pattern="^playercat:"))
    application.add_handler(CommandHandler("team", my_team_command))
    application.add_handler(CommandHandler("mypurse", my_purse_command))
    application.add_handler(CommandHandler("setvicecaptain", setvicecaptain_command))
    application.add_handler(CommandHandler("teams", teams_command))
    application.add_handler(CommandHandler("retain", retain_command))
    application.add_handler(CommandHandler("unretain", unretain_command))
    application.add_handler(CommandHandler("retained", retained_command))

    # --- Captain registration conversation (handles menu:captain itself) --
    application.add_handler(build_captain_conversation_handler())

    # --- Main menu callbacks (register/profile/home) ----------------------
    application.add_handler(CallbackQueryHandler(main_menu_callback, pattern="^menu:(register|profile|home)$"))

    # --- Admin panel ------------------------------------------------------
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CallbackQueryHandler(admin_panel_callback, pattern="^admin:"))
    application.add_handler(CallbackQueryHandler(admin_player_callback, pattern="^adminp:"))
    application.add_handler(CallbackQueryHandler(admin_captain_callback, pattern="^adminc:"))

    # --- Admin player management -----------------------------------------
    application.add_handler(build_addplayer_conversation_handler())
    application.add_handler(CommandHandler("removeplayer", removeplayer_command))
    application.add_handler(CommandHandler("deleteplayer", deleteplayer_command))
    application.add_handler(CommandHandler("restoreplayer", restoreplayer_command))
    application.add_handler(CommandHandler("editplayer", editplayer_command))
    application.add_handler(CommandHandler("searchplayer", searchplayer_command))
    application.add_handler(CommandHandler("allplayers", admin_players_command))
    application.add_handler(build_importplayers_conversation_handler())

    # --- Admin captain management -----------------------------------------
    application.add_handler(CommandHandler("captains", captains_command))
    application.add_handler(CallbackQueryHandler(captain_approve_callback, pattern="^cap_approve:"))
    application.add_handler(CallbackQueryHandler(captain_reject_callback, pattern="^cap_reject:"))
    application.add_handler(CommandHandler("approvecaptain", approvecaptain_command))
    application.add_handler(CommandHandler("rejectcaptain", rejectcaptain_command))
    application.add_handler(CommandHandler("deletecaptain", deletecaptain_command))
    application.add_handler(CallbackQueryHandler(captain_delete_confirm_callback, pattern="^capdel_confirm:"))
    application.add_handler(CallbackQueryHandler(captain_delete_cancel_callback, pattern="^capdel_cancel$"))

    # --- Settings / export -------------------------------------------------
    application.add_handler(CommandHandler("setconfig", setconfig_command))
    application.add_handler(CommandHandler("setregdeadline", setregdeadline_command))
    application.add_handler(CommandHandler("regdeadline", regdeadline_command))
    application.add_handler(CommandHandler("setlanguage", setlanguage_command))
    application.add_handler(build_setup_wizard_handler())
    application.add_handler(CommandHandler("export", export_command))
    application.add_handler(CommandHandler("backup", backup_command))
    application.add_handler(CommandHandler("dashboard", dashboard_command))
    application.add_handler(CommandHandler("dashboard_reset", dashboard_reset_command))
    application.add_handler(CommandHandler("exportpdf", exportpdf_command))

    # --- Auction set flow ---------------------------------------------------
    application.add_handler(CommandHandler("startauction", startauction_command))

    # --- Group lobby status message ----------------------------------------
    application.add_handler(CommandHandler("lobby", lobby_command))
    application.add_handler(CallbackQueryHandler(lobby_page_callback, pattern="^lobby_page:"))
    application.add_handler(CallbackQueryHandler(lobby_refresh_callback, pattern="^lobby_refresh$"))
    application.add_handler(CallbackQueryHandler(lobby_start_callback, pattern="^lobby_start$"))
    application.add_handler(CallbackQueryHandler(lobby_cancel_callback, pattern="^lobby_cancel$"))
    application.add_handler(CommandHandler("endauction", endauction_command))
    application.add_handler(CommandHandler("set", set_command))
    application.add_handler(CommandHandler("showset", showset_command))
    application.add_handler(CallbackQueryHandler(set_start_callback, pattern="^setstart:"))

    # --- Pause / Resume / Next -----------------------------------------------
    application.add_handler(CommandHandler("pause", pause_command))
    application.add_handler(CommandHandler("resume", resume_command))
    application.add_handler(CommandHandler("next", next_command))
    application.add_handler(CommandHandler("nextplayer", next_command))
    application.add_handler(CallbackQueryHandler(next_confirm_callback, pattern="^next_confirm$"))
    application.add_handler(CallbackQueryHandler(next_cancel_callback, pattern="^next_cancel$"))
    application.add_handler(CallbackQueryHandler(next_outcome_callback, pattern="^next_(unsold|return|defer)$"))

    # --- Undo / Return player -------------------------------------------------
    application.add_handler(CommandHandler("undo", undo_command))
    application.add_handler(CallbackQueryHandler(undo_confirm_callback, pattern="^undo_confirm$"))
    application.add_handler(CallbackQueryHandler(undo_cancel_callback, pattern="^undo_cancel$"))
    application.add_handler(CommandHandler("returnplayer", returnplayer_command))
    application.add_handler(CallbackQueryHandler(return_confirm_callback, pattern="^return_confirm:"))
    application.add_handler(CallbackQueryHandler(return_cancel_callback, pattern="^return_cancel$"))
    application.add_handler(CommandHandler("tradeplayer", tradeplayer_command))
    application.add_handler(CallbackQueryHandler(trade_confirm_callback, pattern="^trade_confirm:"))
    application.add_handler(CallbackQueryHandler(trade_cancel_callback, pattern="^trade_cancel$"))

    # --- Re-auction queue -------------------------------------------------
    application.add_handler(CommandHandler("reauction_queue", reauction_queue_command))
    application.add_handler(CallbackQueryHandler(reauction_start_callback, pattern="^reauction_start:"))

    # --- Unsold round -------------------------------------------------------
    application.add_handler(CommandHandler("unsold", unsold_command))
    application.add_handler(CommandHandler("auctionplayer", auctionplayer_command))
    application.add_handler(CommandHandler("unsold_list", unsold_list_command))
    application.add_handler(CallbackQueryHandler(unsold_start_callback, pattern="^unsold_start:"))

    # --- Status / reports -----------------------------------------------------
    application.add_handler(CommandHandler("auction_status", auction_status_command))
    application.add_handler(CommandHandler("remind", remind_command))
    application.add_handler(CommandHandler("sold", sold_command))
    application.add_handler(CommandHandler("auctionhistory", auction_history_command))
    application.add_handler(CommandHandler("bidhistory", bidhistory_command))
    application.add_handler(CommandHandler("topsold", topsold_command))
    application.add_handler(CommandHandler("teamcompare", teamcompare_command))
    application.add_handler(CommandHandler("blockteam", blockteam_command))
    application.add_handler(CommandHandler("unblockteam", unblockteam_command))

    # --- Bidding commands -------------------------------------------------
    application.add_handler(CommandHandler("bid", bid_command))
    application.add_handler(CommandHandler("allin", allin_command))

    # --- Owner-only commands -----------------------------------------------
    register_owner_handlers(application)

    application.add_error_handler(_error_handler)

    return application


def main() -> None:
    application = build_application()
    logger.info("Starting Hand Cricket Auction Bot…")
    application.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()

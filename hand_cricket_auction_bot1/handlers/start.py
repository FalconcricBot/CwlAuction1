"""
handlers/start.py
------------------
/start command and the main menu it displays.
"""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from database import queries as q
from keyboards.main import main_menu_keyboard
from utils.i18n import t
from utils.logger import get_logger

logger = get_logger(__name__)

HELP_TEXT = (
    "ℹ️ <b>HELP</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "Use /allcmd to see the full list of everything this bot can do, "
    "organized by role.\n\n"
    "<b>Quick start</b>\n"
    "/start – main menu\n"
    "/register – register as a player\n"
    "/becomecaptain – register as a captain\n"
    "/profile – view your profile\n"
    "/team – view your team (captains)\n"
    "/mypurse – view your remaining purse (captains)"
)

ALL_COMMANDS_TEXT = (
    "📖 <b>ALL COMMANDS</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "Every command in this bot, shown to everyone regardless of access.\n\n"

    "👤 <b>PLAYER</b>\n"
    "/start – main menu\n"
    "/register – register as a player\n"
    "/becomecaptain – register as a captain\n"
    "/profile – view your profile\n"
    "/players – player list (admin-only)\n"
    "/regdeadline – check the registration deadline\n"
    "/bidhistory &lt;reg_no&gt; – bid history for a player\n"
    "/topsold [n] – highest-sold players leaderboard\n"
    "/teamcompare – compare team spending\n"
    "/help – quick orientation\n\n"

    "👑 <b>CAPTAIN / VICE-CAPTAIN</b>\n"
    "/team – your team's full details\n"
    "/mypurse – your remaining purse\n"
    "/bid &lt;amount&gt; – place a bid\n"
    "/allin – bid your entire remaining purse\n"
    "/setvicecaptain &lt;telegram_id&gt; – set/clear your vice-captain\n\n"

    "🔐 <b>ADMIN — Player Management</b>\n"
    "/admin – open the admin panel\n"
    "/addplayer – add a player manually\n"
    "/importplayers – bulk-import players from CSV\n"
    "/removeplayer, /deleteplayer &lt;reg_no&gt; – soft-delete a player\n"
    "/restoreplayer &lt;reg_no&gt; – restore a removed player\n"
    "/editplayer &lt;reg_no&gt; &lt;name&gt; – edit a player\n"
    "/searchplayer &lt;query&gt; – search players\n"
    "/allplayers – list all players (incl. removed)\n\n"

    "🔐 <b>ADMIN — Captains &amp; Teams</b>\n"
    "/captains – view pending captain requests\n"
    "/approvecaptain &lt;id&gt; – approve a captain\n"
    "/rejectcaptain &lt;id&gt; – reject a captain\n"
    "/deletecaptain &lt;id&gt; – remove a mistaken captain\n"
    "/teams – list all teams\n\n"

    "🔐 <b>ADMIN — Auction Flow</b>\n"
    "/lobby – post/pin live lobby status in the group\n"
    "/startauction – start the auction\n"
    "/set &lt;n&gt; – build/show auction set n\n"
    "/showset &lt;n&gt; – detailed player list for a set\n"
    "/pause, /resume – pause/resume the auction\n"
    "/next, /nextplayer – advance/skip current player\n"
    "/undo – undo the last sale\n"
    "/returnplayer &lt;reg_no&gt; – return a sold player to the pool\n"
    "/tradeplayer &lt;reg_no&gt; &lt;team&gt; – trade a sold player between teams\n"
    "/retain &lt;reg_no&gt; &lt;team&gt; [price] – retain a player before the auction\n"
    "/unretain &lt;reg_no&gt; – remove a retention (refunds purse)\n"
    "/retained – list all retained players\n"
    "/reauction_queue – view the re-auction queue\n"
    "/unsold, /unsold_list – run/view the unsold round\n"
    "/auctionplayer &lt;reg_no&gt; – jump straight to a specific player\n"
    "/auction_status – live auction status\n"
    "/sold – list sold players\n"
    "/blockteam, /unblockteam &lt;team&gt; – block/unblock a team from bidding\n"
    "/remind &lt;minutes&gt; &lt;message&gt; – schedule a reminder\n"
    "/endauction – end the auction, final report\n\n"

    "🔐 <b>ADMIN — Reports &amp; Settings</b>\n"
    "/auctionhistory – full audit trail\n"
    "/export – export data to CSV\n"
    "/exportpdf – export final report as PDF\n"
    "/backup – download a full database backup\n"
    "/dashboard – generate a live auction dashboard (HTML)\n"
    "/dashboard_reset – rotate the dashboard's secret link\n"
    "/setconfig &lt;key&gt; &lt;value&gt; – update a setting\n"
    "/setuptournament – guided tournament setup wizard\n"
    "/setregdeadline &lt;date&gt; – set/clear the registration deadline\n"
    "/setlanguage &lt;en|hi&gt; – set the bot's language\n\n"

    "🛠️ <b>OWNER</b>\n"
    "/addadmin &lt;telegram_id&gt; – grant admin access\n"
    "/removeadmin &lt;telegram_id&gt; – revoke a runtime-granted admin\n"
    "/listadmins – list all owners and admins\n"
    "/resetall – wipe all tournament data (destructive)\n"
    "/botstats – bot health and database stats"
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await q.upsert_user(user.id, user.username, user.first_name, user.last_name)
    welcome_text = await t("welcome")
    await update.effective_message.reply_text(
        welcome_text, parse_mode="HTML", reply_markup=main_menu_keyboard()
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT, parse_mode="HTML")


async def allcommands_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(ALL_COMMANDS_TEXT, parse_mode="HTML")


async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles menu:* callback buttons by delegating to the right handler."""
    query = update.callback_query
    await query.answer()
    _, args = query.data, query.data.split(":")
    action = args[1] if len(args) > 1 else "home"

    # Imports deferred to avoid circular imports between handler modules.
    from handlers.registration import show_registration_prompt, show_profile

    if action == "home":
        welcome_text = await t("welcome")
        await query.edit_message_text(
            welcome_text, parse_mode="HTML", reply_markup=main_menu_keyboard()
        )
    elif action == "register":
        await show_registration_prompt(update, context)
    elif action == "profile":
        await show_profile(update, context)
    elif action == "captain":
        await start_captain_flow(update, context)

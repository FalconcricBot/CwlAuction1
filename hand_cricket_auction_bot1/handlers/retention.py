"""
handlers/retention.py
----------------------
/retain, /unretain (admin) and /retained (everyone).
"""
from __future__ import annotations

from html import escape

from telegram import Update
from telegram.ext import ContextTypes

from database import queries as q
from services import retention_service
from utils.helpers import fmt_money, is_admin, player_display_name, safe_int


async def _require_admin(update: Update) -> bool:
    if is_admin(update.effective_user.id):
        return True
    from utils.helpers import is_admin_async
    return await is_admin_async(update.effective_user.id)


async def retain_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return
    args = context.args
    if len(args) < 2:
        await update.effective_message.reply_text(
            "Usage: /retain <reg_no> <team name/short name/id> [price]\n"
            "Example: /retain 12 RCB 50000\n\n"
            "If price is omitted, the player's base price is used. Retained "
            "players are deducted from the team purse and skip the auction.\n"
            "Limit per team: /setconfig max_retentions <n>"
        )
        return

    reg_no = safe_int(args[0])
    if reg_no is None:
        await update.effective_message.reply_text("❌ Registration number must be a number.")
        return

    # The last argument is the price if it's a number and there are 3+ args.
    price = None
    team_parts = args[1:]
    if len(args) >= 3 and safe_int(args[-1]) is not None:
        price = safe_int(args[-1])
        team_parts = args[1:-1]
    team_identifier = " ".join(team_parts)

    team = await q.get_team_by_name_or_id(team_identifier)
    if team is None:
        await update.effective_message.reply_text(f"❌ No team found matching '{team_identifier}'.")
        return

    try:
        result = await retention_service.retain_player(update.effective_user.id, reg_no, team, price)
    except retention_service.RetentionError as e:
        await update.effective_message.reply_text(f"❌ {e}")
        return

    p, t = result["player"], result["team"]
    name = player_display_name(p["first_name"], p["last_name"])
    await update.effective_message.reply_text(
        "🔒 <b>PLAYER RETAINED</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 {escape(name)} (#{p['registration_no']})\n"
        f"🏳️ Team: {escape(t['name'])}\n"
        f"💰 Retention Price: {fmt_money(result['price'])}\n"
        f"💰 Remaining Purse: {fmt_money(t['remaining_purse'])}\n"
        f"🔒 Retentions: {result['retentions_used']}/{result['retentions_max']}",
        parse_mode="HTML",
    )


async def unretain_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /unretain <reg_no>")
        return
    reg_no = safe_int(context.args[0])
    if reg_no is None:
        await update.effective_message.reply_text("❌ Registration number must be a number.")
        return
    try:
        result = await retention_service.remove_retention(update.effective_user.id, reg_no)
    except retention_service.RetentionError as e:
        await update.effective_message.reply_text(f"❌ {e}")
        return
    p, t = result["player"], result["team"]
    name = player_display_name(p["first_name"], p["last_name"])
    team_line = f"\n💰 {escape(t['name'])} refunded {fmt_money(result['refund'])}" if t else ""
    await update.effective_message.reply_text(
        f"🔓 Retention removed for {escape(name)} (#{p['registration_no']}). "
        f"They are back in the auction pool.{team_line}",
        parse_mode="HTML",
    )


async def retained_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = await q.list_retained_players()
    if not rows:
        await update.effective_message.reply_text("🔒 No players have been retained yet.")
        return
    lines = ["🔒 <b>RETAINED PLAYERS</b>", "━━━━━━━━━━━━━━━━━━━━", ""]
    current = None
    for r in rows:
        if r["team_id"] != current:
            current = r["team_id"]
            lines.append(f"🏳️ <b>{escape(r['team_name'])}</b>")
        name = player_display_name(r["first_name"], r["last_name"])
        lines.append(f"  • #{r['registration_no']} {escape(name)} — {fmt_money(r['retention_price'])}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")

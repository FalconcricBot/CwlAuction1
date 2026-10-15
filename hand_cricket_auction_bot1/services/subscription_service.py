"""
services/subscription_service.py
----------------------------------
Force-subscribe gate for player/captain registration. If REQUIRED_GROUP_ID
and/or REQUIRED_CHANNEL_ID are configured (see config.py / .env.example),
a user must be a member of both before /register or /becomecaptain will
proceed. The bot must be an admin in both chats to check membership via
Telegram's getChatMember API.

Design notes:
- Either or both requirements can be configured independently (group only,
  channel only, both, or neither — neither disables this gate entirely).
- "Member" includes creator/administrator/member statuses; "left" and
  "kicked" (banned) do not count as joined.
- Telegram API errors while checking (bot not admin, chat not found, etc.)
  fail OPEN for everyone except explicitly "left"/"kicked" results — we
  never want a misconfiguration to lock every player out of registering,
  but we log loudly so the admin notices and fixes the setup.
"""
from __future__ import annotations

from telegram.error import TelegramError
from telegram.ext import ContextTypes

from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

_JOINED_STATUSES = {"creator", "administrator", "member", "restricted"}
_NOT_JOINED_STATUSES = {"left", "kicked"}


def is_subscription_required() -> bool:
    return bool(settings.required_group_id or settings.required_channel_id)


async def _is_member(context: ContextTypes.DEFAULT_TYPE, chat_id: str, user_id: int) -> bool | None:
    """
    Returns True if joined, False if confirmed not joined, or None if the
    check itself failed (misconfiguration, bot not admin, etc.) — callers
    should treat None as "can't verify, don't block".
    """
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
    except TelegramError as e:
        logger.warning("Could not check membership for %s in %s: %s", user_id, chat_id, e)
        return None

    status = member.status
    if status in _NOT_JOINED_STATUSES:
        return False
    if status in _JOINED_STATUSES:
        return True
    # Unknown/future status value — don't block on something we don't
    # recognize; log it so it can be reviewed.
    logger.warning("Unrecognized chat member status '%s' for %s in %s", status, user_id, chat_id)
    return None


async def check_subscription(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> dict:
    """
    Checks the user's membership against whichever of REQUIRED_GROUP_ID /
    REQUIRED_CHANNEL_ID are configured. Returns a dict:
        {"ok": bool, "missing_group": bool, "missing_channel": bool}
    "ok" is True if every configured requirement is satisfied (or
    unverifiable, which fails open). A requirement that isn't configured
    is never "missing".
    """
    missing_group = False
    missing_channel = False

    if settings.required_group_id:
        result = await _is_member(context, settings.required_group_id, user_id)
        if result is False:
            missing_group = True

    if settings.required_channel_id:
        result = await _is_member(context, settings.required_channel_id, user_id)
        if result is False:
            missing_channel = True

    return {
        "ok": not (missing_group or missing_channel),
        "missing_group": missing_group,
        "missing_channel": missing_channel,
    }


def build_join_prompt(check_result: dict) -> tuple[str, "InlineKeyboardMarkup | None"]:
    """Builds the "please join" message + buttons for whatever is missing."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    lines = ["🔒 <b>Join to register</b>", "", "Please join before registering:"]
    buttons = []

    if check_result["missing_group"]:
        lines.append("• The group")
        link = settings.required_group_invite_link or _public_link(settings.required_group_id)
        if link:
            buttons.append([InlineKeyboardButton("➡️ Join Group", url=link)])

    if check_result["missing_channel"]:
        lines.append("• The channel")
        link = settings.required_channel_invite_link or _public_link(settings.required_channel_id)
        if link:
            buttons.append([InlineKeyboardButton("➡️ Join Channel", url=link)])

    lines.append("")
    lines.append("Then try again.")

    keyboard = InlineKeyboardMarkup(buttons) if buttons else None
    return "\n".join(lines), keyboard


def _public_link(chat_id: str | None) -> str | None:
    if chat_id and chat_id.startswith("@"):
        return f"https://t.me/{chat_id[1:]}"
    return None

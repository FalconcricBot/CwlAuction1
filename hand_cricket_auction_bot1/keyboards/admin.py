"""
keyboards/admin.py
-------------------
Keyboards for the /admin panel and admin sub-menus.
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("👥 Player Management", callback_data="admin:players")],
        [InlineKeyboardButton("👑 Captain Management", callback_data="admin:captains")],
        [InlineKeyboardButton("🏳️ Team Management", callback_data="admin:teams")],
        [InlineKeyboardButton("🔨 Auction Management", callback_data="admin:auction")],
        [InlineKeyboardButton("📊 Reports", callback_data="admin:reports")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="admin:settings")],
    ]
    return InlineKeyboardMarkup(rows)


def admin_player_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📋 View Players", callback_data="adminp:view")],
        [InlineKeyboardButton("✅ Sold Players", callback_data="adminp:sold")],
        [InlineKeyboardButton("❌ Unsold Players", callback_data="adminp:unsold")],
        [InlineKeyboardButton("⬅️ Back", callback_data="admin:home")],
    ]
    return InlineKeyboardMarkup(rows)


def admin_captain_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("⏳ Pending Requests", callback_data="adminc:pending")],
        [InlineKeyboardButton("📋 All Captains", callback_data="adminc:all")],
        [InlineKeyboardButton("⬅️ Back", callback_data="admin:home")],
    ]
    return InlineKeyboardMarkup(rows)


def captain_approval_keyboard(captain_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"cap_approve:{captain_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"cap_reject:{captain_id}"),
        ]
    ])


def admin_auction_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🔨 Auction Status", callback_data="admina:status")],
        [InlineKeyboardButton("⏸️ Pause", callback_data="admina:pause"),
         InlineKeyboardButton("▶️ Resume", callback_data="admina:resume")],
        [InlineKeyboardButton("↩️ Undo Last Sale", callback_data="admina:undo")],
        [InlineKeyboardButton("⬅️ Back", callback_data="admin:home")],
    ]
    return InlineKeyboardMarkup(rows)


def captain_delete_confirm_keyboard(captain_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Delete", callback_data=f"capdel_confirm:{captain_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data="capdel_cancel"),
        ]
    ])

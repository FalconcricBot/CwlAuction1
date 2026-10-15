"""
keyboards/main.py
------------------
Keyboards for the main /start menu and player-facing commands.
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📝 Register Player", callback_data="menu:register")],
        [InlineKeyboardButton("👑 Captain Registration", callback_data="menu:captain")],
        [InlineKeyboardButton("👤 My Profile", callback_data="menu:profile")],
    ]
    return InlineKeyboardMarkup(rows)


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu:home")]]
    )


def confirm_cancel_keyboard(confirm_data: str, cancel_data: str,
                             confirm_label: str = "✅ Confirm",
                             cancel_label: str = "❌ Cancel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(confirm_label, callback_data=confirm_data),
          InlineKeyboardButton(cancel_label, callback_data=cancel_data)]]
    )


def category_selection_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🏏 Batsman", callback_data="playercat:Batsman"),
            InlineKeyboardButton("🎯 Bowler", callback_data="playercat:Bowler"),
        ],
        [
            InlineKeyboardButton("⚡ All-rounder", callback_data="playercat:All-rounder"),
            InlineKeyboardButton("🧤 Wicket-keeper", callback_data="playercat:Wicket-keeper"),
        ],
        [InlineKeyboardButton("⏭️ Skip", callback_data="playercat:skip")],
    ])

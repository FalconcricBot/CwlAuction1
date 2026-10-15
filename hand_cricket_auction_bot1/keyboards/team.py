"""
keyboards/team.py
------------------
Keyboards for team / captain related flows.
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def captain_registration_start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👑 Start Captain Registration", callback_data="captain:start")]
    ])


def teams_list_keyboard(teams: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(name, callback_data=f"team_view:{team_id}")]
        for team_id, name in teams
    ]
    return InlineKeyboardMarkup(rows) if rows else InlineKeyboardMarkup([])

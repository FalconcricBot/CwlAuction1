"""
keyboards/auction.py
---------------------
Keyboards used during the live bidding process.

NOTE: Regular bid amounts are no longer placed via buttons — captains use
the `/bid <amount>` command (and `/allin`). The auction card is
button-free for bidding; only admin-facing confirmations (set start,
undo, return, skip) keep inline buttons.
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def start_set_keyboard(set_number: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ START SET", callback_data=f"setstart:{set_number}")]
    ])


def undo_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ CONFIRM", callback_data="undo_confirm"),
            InlineKeyboardButton("❌ CANCEL", callback_data="undo_cancel"),
        ]
    ])


def return_player_keyboard(player_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 RETURN TO AUCTION", callback_data=f"return_confirm:{player_id}"),
            InlineKeyboardButton("❌ CANCEL", callback_data="return_cancel"),
        ]
    ])


def trade_confirm_keyboard(player_id: int, to_team_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 CONFIRM TRADE", callback_data=f"trade_confirm:{player_id}:{to_team_id}"),
            InlineKeyboardButton("❌ CANCEL", callback_data="trade_cancel"),
        ]
    ])


def skip_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Skip", callback_data="next_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="next_cancel"),
        ]
    ])


def skip_outcome_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏭️ Skip & Return Later", callback_data="next_defer"),
        ],
        [
            InlineKeyboardButton("❌ Mark UNSOLD", callback_data="next_unsold"),
            InlineKeyboardButton("↩️ Return to Pool", callback_data="next_return"),
        ]
    ])


def reauction_queue_keyboard(player_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔨 AUCTION PLAYER", callback_data=f"reauction_start:{player_id}")]
    ])


def unsold_start_keyboard(player_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔨 START BIDDING", callback_data=f"unsold_start:{player_id}")]
    ])


def lobby_keyboard(page: int, total_pages: int, is_admin: bool) -> InlineKeyboardMarkup:
    rows = []
    if total_pages > 1:
        rows.append([
            InlineKeyboardButton("⬅️ Back", callback_data=f"lobby_page:{max(1, page - 1)}"),
            InlineKeyboardButton("➡️ Next", callback_data=f"lobby_page:{min(total_pages, page + 1)}"),
        ])
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="lobby_refresh")])
    if is_admin:
        rows.append([
            InlineKeyboardButton("▶️ Start", callback_data="lobby_start"),
            InlineKeyboardButton("🛑 Cancel", callback_data="lobby_cancel"),
        ])
    return InlineKeyboardMarkup(rows)

"""
utils/helpers.py
-----------------
Small, dependency-free helper functions used across handlers/services:
currency formatting, admin checks, name formatting, safe callback parsing.
"""
from __future__ import annotations

from typing import Optional

from config import settings


def is_admin(telegram_id: int) -> bool:
    """
    Fast, synchronous check against the fixed .env ADMIN_IDS/OWNER_IDS only.
    Does NOT include runtime-added (dynamic) admins — use
    `utils.helpers.is_admin_async` wherever a DB lookup is acceptable, which
    covers dynamic admins too. Owners always count as admins.
    """
    return telegram_id in settings.admin_ids or telegram_id in settings.owner_ids


def is_owner(telegram_id: int) -> bool:
    return telegram_id in settings.owner_ids


async def is_admin_async(telegram_id: int) -> bool:
    """Full admin check: .env ADMIN_IDS, .env OWNER_IDS, or a runtime-added admin."""
    if is_admin(telegram_id):
        return True
    from database import queries as q
    return await q.is_dynamic_admin(telegram_id)


def fmt_money(amount: Optional[int]) -> str:
    if amount is None:
        return "₹0"
    return f"₹{amount:,}"


def player_display_name(first_name: str, last_name: Optional[str]) -> str:
    if last_name:
        return f"{first_name} {last_name}"
    return first_name


def safe_int(value: str, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_callback(data: str) -> tuple[str, list[str]]:
    """
    Parses callback_data of the form "prefix:arg1:arg2" into
    ("prefix", ["arg1", "arg2"]). Never trust callback data blindly -
    callers must still validate every arg against DB state.
    """
    parts = data.split(":")
    return parts[0], parts[1:]


def next_valid_bid(current_bid: int, base_price: int) -> int:
    """
    The minimum amount a new bid must reach to be valid. There is no fixed
    increment — any amount strictly greater than the current bid is valid,
    same as a real auction. If there is no bid yet, the base price itself
    is the minimum valid opening bid.
    """
    if current_bid <= 0:
        return base_price
    return current_bid + 1

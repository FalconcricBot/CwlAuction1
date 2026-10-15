"""
config.py
---------
Central configuration for the Hand Cricket Auction Bot.

All secrets (bot token, admin ids) are loaded from environment variables via
python-dotenv. NEVER hardcode the bot token or admin ids in source code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv()


def _get_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_admin_ids(raw: str | None) -> List[int]:
    if not raw:
        return []
    ids: List[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.append(int(chunk))
        except ValueError:
            continue
    return ids


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: List[int] = field(default_factory=list)
    owner_ids: List[int] = field(default_factory=list)
    database_path: str = "auction.db"
    group_chat_id: Optional[int] = None
    required_group_id: Optional[str] = None
    required_channel_id: Optional[str] = None
    required_group_invite_link: Optional[str] = None
    required_channel_invite_link: Optional[str] = None

    # Default configurable auction parameters (overridable at runtime via
    # the `settings` DB table / admin panel).
    default_starting_purse: int = 10_000
    default_base_price: int = 500
    default_auction_timer: int = 20
    default_max_squad_size: int = 11
    default_max_players_per_set: int = 20
    default_min_players_to_start: int = 0
    default_max_teams: int = 16


def load_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError(
            "BOT_TOKEN is not set. Create a .env file (see .env.example) "
            "with your Telegram bot token before starting the bot."
        )

    group_chat_raw = os.getenv("GROUP_CHAT_ID", "").strip()
    group_chat_id = int(group_chat_raw) if group_chat_raw else None

    required_group_raw = os.getenv("REQUIRED_GROUP_ID", "").strip()
    required_channel_raw = os.getenv("REQUIRED_CHANNEL_ID", "").strip()

    return Settings(
        bot_token=bot_token,
        admin_ids=_parse_admin_ids(os.getenv("ADMIN_IDS")),
        owner_ids=_parse_admin_ids(os.getenv("OWNER_IDS")),
        database_path=os.getenv("DATABASE_PATH", "auction.db"),
        group_chat_id=group_chat_id,
        required_group_id=required_group_raw or None,
        required_channel_id=required_channel_raw or None,
        required_group_invite_link=os.getenv("REQUIRED_GROUP_INVITE_LINK", "").strip() or None,
        required_channel_invite_link=os.getenv("REQUIRED_CHANNEL_INVITE_LINK", "").strip() or None,
        default_starting_purse=_get_int_env("DEFAULT_STARTING_PURSE", 10_000),
        default_base_price=_get_int_env("DEFAULT_BASE_PRICE", 500),
        default_auction_timer=_get_int_env("DEFAULT_AUCTION_TIMER", 20),
        default_max_squad_size=_get_int_env("DEFAULT_MAX_SQUAD_SIZE", 11),
        default_max_players_per_set=_get_int_env("DEFAULT_MAX_PLAYERS_PER_SET", 20),
        default_min_players_to_start=_get_int_env("DEFAULT_MIN_PLAYERS_TO_START", 0),
        default_max_teams=_get_int_env("DEFAULT_MAX_TEAMS", 16),
    )


settings = load_settings()

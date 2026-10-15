"""Helpers for sending the SOLD / UNSOLD announcement media.

Drop a file named ``sold.<ext>`` and ``unsold.<ext>`` into the ``assets/``
folder (jpg, jpeg, png, webp, gif, mp4). The bot picks it up automatically.
If a file is missing or sending fails, the caller falls back to plain text.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from telegram.error import TelegramError
from telegram.ext import ContextTypes

from utils.logger import get_logger

logger = get_logger(__name__)

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
_EXTENSIONS = (".mp4", ".gif", ".jpg", ".jpeg", ".png", ".webp")

# Telegram file_id cache so the file is uploaded only once per process.
_file_id_cache: dict[str, str] = {}


def _find_asset(stem: str) -> Optional[Path]:
    for ext in _EXTENSIONS:
        path = ASSETS_DIR / f"{stem}{ext}"
        if path.is_file():
            return path
    return None


async def send_announcement(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                            stem: str, caption: str) -> bool:
    """Send media + caption. Returns True on success, False if the caller
    should fall back to a plain text message."""
    path = _find_asset(stem)
    if path is None:
        return False

    ext = path.suffix.lower()
    cached = _file_id_cache.get(stem)
    try:
        if cached:
            media = cached
            fh = None
        else:
            fh = open(path, "rb")
            media = fh
        try:
            if ext == ".gif":
                msg = await context.bot.send_animation(
                    chat_id, media, caption=caption, parse_mode="HTML")
                file_id = msg.animation.file_id if msg.animation else None
            elif ext == ".mp4":
                msg = await context.bot.send_animation(
                    chat_id, media, caption=caption, parse_mode="HTML")
                file_id = (msg.animation or msg.video).file_id if (msg.animation or msg.video) else None
            else:
                msg = await context.bot.send_photo(
                    chat_id, media, caption=caption, parse_mode="HTML")
                file_id = msg.photo[-1].file_id if msg.photo else None
        finally:
            if fh:
                fh.close()
        if file_id:
            _file_id_cache[stem] = file_id
        return True
    except TelegramError as e:
        logger.warning("Could not send %s media (%s); falling back to text.", stem, e)
        _file_id_cache.pop(stem, None)
        return False

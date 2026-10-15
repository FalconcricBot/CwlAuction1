"""
utils/i18n.py
--------------
Lightweight multi-language support (English / Hindi). Language is a global
bot setting (not per-user) stored in the settings table, toggled by an
admin with /setlanguage. Only a curated set of frequently-seen, player-
facing strings are translated — internal admin tooling stays in English to
avoid translating every error message in the codebase.

Usage:
    from utils.i18n import t
    text = await t("welcome")
"""
from __future__ import annotations

_TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        "welcome": (
            "🏏 <b>HAND CRICKET AUCTION</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Welcome! Use the menu below to register as a player or as a captain."
        ),
        "already_registered": "✅ You are already registered!",
        "registration_success": "📝 <b>REGISTRATION SUCCESSFUL</b>",
        "registration_restored": "📝 <b>REGISTRATION RESTORED</b>",
        "sold_header": "🔨 <b>SOLD!</b>",
        "unsold_header": "❌ <b>UNSOLD</b>",
        "bid_placed": "✅ Bid placed",
        "not_registered": "❌ You are not registered yet. Use /register to join the auction pool.",
        "captain_registration_title": "👑 <b>CAPTAIN REGISTRATION</b>",
        "profile_title": "👤 <b>MY PROFILE</b>",
    },
    "hi": {
        "welcome": (
            "🏏 <b>हैंड क्रिकेट नीलामी</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "स्वागत है! नीचे दिए मेनू से खिलाड़ी या कप्तान के रूप में रजिस्टर करें।"
        ),
        "already_registered": "✅ आप पहले से रजिस्टर्ड हैं!",
        "registration_success": "📝 <b>रजिस्ट्रेशन सफल</b>",
        "registration_restored": "📝 <b>रजिस्ट्रेशन बहाल हुआ</b>",
        "sold_header": "🔨 <b>बिक गया!</b>",
        "unsold_header": "❌ <b>नहीं बिका</b>",
        "bid_placed": "✅ बोली लगाई गई",
        "not_registered": "❌ आप अभी रजिस्टर्ड नहीं हैं। नीलामी पूल में शामिल होने के लिए /register भेजें।",
        "captain_registration_title": "👑 <b>कप्तान रजिस्ट्रेशन</b>",
        "profile_title": "👤 <b>मेरी प्रोफाइल</b>",
    },
}

_DEFAULT_LANG = "en"


async def get_language() -> str:
    from database import queries as q
    lang = await q.get_setting("bot_language")
    if lang not in _TRANSLATIONS:
        return _DEFAULT_LANG
    return lang


async def t(key: str) -> str:
    """Returns the translated string for `key` in the currently configured
    bot language, falling back to English, then to the key itself if
    entirely missing (so a missing translation never crashes a handler)."""
    lang = await get_language()
    table = _TRANSLATIONS.get(lang, _TRANSLATIONS[_DEFAULT_LANG])
    if key in table:
        return table[key]
    return _TRANSLATIONS[_DEFAULT_LANG].get(key, key)


def available_languages() -> list[str]:
    return list(_TRANSLATIONS.keys())

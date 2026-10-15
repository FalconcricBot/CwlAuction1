"""
services/lobby_service.py
--------------------------
Builds the "PLAYER AUCTION - LOBBY" status view that gets posted (and kept
pinned + auto-refreshed) in the group chat. View-only: registration itself
still happens via DM (/register, /becomecaptain) as per the existing flow.
This module only renders the current state — team/purse/timer config,
approved captains, and the player pool (paginated) — for a live group
overview, similar to other gully-cricket auction bots.
"""
from __future__ import annotations

from database import queries as q
from utils.helpers import fmt_money, player_display_name

PLAYERS_PER_PAGE = 25


async def get_lobby_data() -> dict:
    all_settings = await q.get_all_settings()
    max_teams = int(all_settings.get("max_teams", "16"))
    max_squad_size = int(all_settings.get("max_squad_size", "11"))
    starting_purse = int(all_settings.get("starting_purse", "10000"))
    base_price = int(all_settings.get("base_price", "500"))
    auction_timer = int(all_settings.get("auction_timer", "20"))
    min_players_to_start = int(all_settings.get("min_players_to_start", "0"))

    captains = await q.list_captains(status="APPROVED")
    players = await q.list_players(status="AVAILABLE")
    # Also include REGISTERED (not yet promoted to AVAILABLE, if any) so the
    # pool count matches what people expect to see right after registering.
    registered_extra = await q.list_players(status="REGISTERED")
    pool = list(players) + list(registered_extra)

    return {
        "max_teams": max_teams,
        "max_squad_size": max_squad_size,
        "starting_purse": starting_purse,
        "base_price": base_price,
        "auction_timer": auction_timer,
        "min_players_to_start": min_players_to_start,
        "captains": captains,
        "pool": pool,
    }


def _purse_display(amount: int) -> str:
    """Formats a purse amount the way these lobby posts usually show it
    (e.g. '5 Cr', '50 L') when it's a round crore/lakh figure, falling back
    to a plain ₹ amount otherwise."""
    if amount % 10_000_000 == 0 and amount >= 10_000_000:
        return f"{amount // 10_000_000} Cr"
    if amount % 100_000 == 0 and amount >= 100_000:
        return f"{amount // 100_000} L"
    return fmt_money(amount)


def render_lobby_text(data: dict, page: int = 1) -> tuple[str, int]:
    """Returns (text, total_pages)."""
    captains = data["captains"]
    pool = data["pool"]
    max_teams = data["max_teams"]
    total_pages = max(1, -(-len(pool) // PLAYERS_PER_PAGE))  # ceil div
    page = max(1, min(page, total_pages))

    lines = [
        "🚨 <b>PLAYER AUCTION - LOBBY</b>",
        "",
        f"🏟️ {max_teams} teams • {data['max_squad_size']} a side "
        "(captain + others)",
        f"💼 Purse {_purse_display(data['starting_purse'])} • "
        f"Base {_purse_display(data['base_price'])} • "
        f"{data['auction_timer']}s per player",
    ]
    if data["min_players_to_start"] > 0:
        lines.append(
            f"📥 Minimum pool: <b>{data['min_players_to_start']}</b> players "
            "(no upper limit)"
        )
    lines.append("")
    lines.append(
        "✍️ <i>Registration is manual — DM me /register to join the player "
        "pool, or /becomecaptain to register as a captain. Nobody is added "
        "just for talking here.</i>"
    )
    lines.append("")

    lines.append(f"<b>Captains ({len(captains)}/{max_teams})</b>")
    if captains:
        for i, c in enumerate(captains, start=1):
            lines.append(f"{i}. 👑 {c['team_name']} — {_purse_display(data['starting_purse'])}")
    else:
        lines.append("<i>No approved captains yet.</i>")
    lines.append("")

    min_note = f" · min {data['min_players_to_start']}" if data["min_players_to_start"] > 0 else ""
    lines.append(f"<b>Player pool ({len(pool)}{min_note}) — page {page}/{total_pages}</b>")
    start = (page - 1) * PLAYERS_PER_PAGE
    page_players = pool[start:start + PLAYERS_PER_PAGE]
    if page_players:
        for i, p in enumerate(page_players, start=start + 1):
            name = player_display_name(p["first_name"], p["last_name"])
            lines.append(f"{i}. {name}")
    else:
        lines.append("<i>No players registered yet.</i>")

    lines.append("")
    remaining_captains = max(0, max_teams - len(captains))
    remaining_players = max(0, data["min_players_to_start"] - len(pool))
    lines.append(
        f"<i>Waiting on {remaining_captains} more captain(s) and "
        f"{remaining_players} more player(s).</i>"
    )

    return "\n".join(lines), total_pages

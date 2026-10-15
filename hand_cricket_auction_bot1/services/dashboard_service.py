"""
services/dashboard_service.py
-------------------------------
Generates a self-contained, static HTML "dashboard" snapshot of the live
auction: current player on the block, bid/timer info, every team's purse
and squad, and sold/unsold lists. No backend, no API — just one HTML file
with inline CSS that can be uploaded anywhere (or opened locally).

Since there's no live server behind it, "auto-refresh" only reloads the
same static file (via a <meta refresh> tag) — the data itself is a
snapshot from whenever /dashboard was last run. The page clearly labels
itself with a generation timestamp and an "age" indicator so viewers know
when the file needs to be regenerated (via /dashboard again) and
re-uploaded to whatever host is serving it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from database import queries as q
from services import auction_service, captain_service
from utils.helpers import fmt_money, player_display_name
from utils.timers import seconds_remaining

REFRESH_SECONDS = 30


def _status_badge(status: str) -> str:
    colors = {
        "AVAILABLE": "#22c55e", "REGISTERED": "#eab308", "SOLD": "#3b82f6",
        "UNSOLD": "#ef4444", "IN_AUCTION": "#f97316", "REMOVED": "#6b7280",
    }
    color = colors.get(status, "#6b7280")
    return f'<span class="badge" style="background:{color}">{escape(status)}</span>'


async def generate_dashboard_html() -> str:
    summary = await auction_service.get_auction_status_summary()
    state = summary["state"]
    counts = summary["counts"]
    auction = summary["auction"]
    player = summary["player"]
    auction_set = summary["auction_set"]

    teams_data = await captain_service.list_teams_with_squad()
    sold_players = await q.list_sold_players()
    unsold_players = await q.list_unsold_players()

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # --- Current auction block ------------------------------------------------
    current_block_html = '<p class="muted">No player is currently on the auction block.</p>'
    if auction is not None and player is not None:
        remaining = max(0, int(seconds_remaining(auction["deadline_ts"])))
        highest_team_name = "—"
        if auction["highest_team_id"]:
            t = await q.get_team_by_id(auction["highest_team_id"])
            highest_team_name = t["name"] if t else "—"
        name = escape(player_display_name(player["first_name"], player["last_name"]))
        set_label = f"Set {auction_set['set_number']}" if auction_set else escape(auction["mode"])
        current_block_html = f"""
        <div class="current-player">
            <div class="cp-name">{name} <span class="reg">#{player['registration_no']}</span></div>
            <div class="cp-grid">
                <div><span class="label">Base Price</span><span class="value">{fmt_money(auction['base_price'])}</span></div>
                <div><span class="label">Current Bid</span><span class="value highlight">{fmt_money(auction['current_bid'])}</span></div>
                <div><span class="label">Highest Bidder</span><span class="value">{escape(highest_team_name)}</span></div>
                <div><span class="label">Time Left</span><span class="value">{remaining}s</span></div>
                <div><span class="label">Round</span><span class="value">{escape(set_label)}</span></div>
                <div><span class="label">State</span><span class="value">{escape(state['state'])}</span></div>
            </div>
        </div>
        """

    # --- Teams -------------------------------------------------------------
    team_cards = []
    for entry in teams_data:
        team, squad = entry["team"], entry["squad"]
        spent = team["starting_purse"] - team["remaining_purse"]
        squad_rows = "".join(
            f"<tr><td>{escape(player_display_name(p['first_name'], p['last_name']))}</td>"
            f"<td>{fmt_money(p['sold_price'])}</td></tr>"
            for p in squad
        ) or '<tr><td colspan="2" class="muted">No players yet</td></tr>'
        team_cards.append(f"""
        <div class="team-card">
            <h3>{escape(team['name'])} <span class="short">({escape(team['short_name'])})</span></h3>
            <div class="team-stats">
                <div><span class="label">Purse Left</span><span class="value">{fmt_money(team['remaining_purse'])}</span></div>
                <div><span class="label">Spent</span><span class="value">{fmt_money(spent)}</span></div>
                <div><span class="label">Squad</span><span class="value">{q.team_member_count(team, squad)}/{team['max_squad_size']}</span></div>
            </div>
            <table class="squad-table">{squad_rows}</table>
        </div>
        """)
    teams_html = "".join(team_cards) or '<p class="muted">No teams created yet.</p>'

    # --- Sold / Unsold lists -------------------------------------------------
    sold_rows = "".join(
        f"<tr><td>#{p['registration_no']}</td>"
        f"<td>{escape(player_display_name(p['first_name'], p['last_name']))}</td>"
        f"<td>{escape(p['team_name'] or '—')}</td>"
        f"<td>{fmt_money(p['sold_price'])}</td></tr>"
        for p in sold_players
    ) or '<tr><td colspan="4" class="muted">No sales yet</td></tr>'

    unsold_rows = "".join(
        f"<tr><td>#{p['registration_no']}</td>"
        f"<td>{escape(player_display_name(p['first_name'], p['last_name']))}</td></tr>"
        for p in unsold_players
    ) or '<tr><td colspan="2" class="muted">None</td></tr>'

    # --- Summary counts ------------------------------------------------------
    total = sum(counts.values())
    summary_html = "".join(
        f'<div class="stat"><span class="stat-value">{v}</span><span class="stat-label">{escape(k)}</span></div>'
        for k, v in [
            ("Total", total), ("Sold", counts.get("SOLD", 0)),
            ("Unsold", counts.get("UNSOLD", 0)), ("Available", counts.get("AVAILABLE", 0)),
            ("Teams", len(teams_data)),
        ]
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_SECONDS}">
<title>🏏 Live Auction Dashboard</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 24px; background: #0f172a; color: #e2e8f0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  h1 {{ margin: 0 0 4px; font-size: 1.6rem; }}
  .meta {{ color: #94a3b8; font-size: 0.85rem; margin-bottom: 20px; }}
  .meta .token-note {{ color: #64748b; }}
  section {{ margin-bottom: 28px; }}
  h2 {{ font-size: 1.1rem; border-bottom: 1px solid #1e293b; padding-bottom: 6px; margin-bottom: 14px; }}
  .badge {{ color: white; padding: 2px 8px; border-radius: 10px; font-size: 0.75rem; }}
  .muted {{ color: #64748b; font-style: italic; }}

  .summary-row {{ display: flex; flex-wrap: wrap; gap: 14px; }}
  .stat {{ background: #1e293b; border-radius: 10px; padding: 12px 18px; min-width: 90px; text-align: center; }}
  .stat-value {{ display: block; font-size: 1.5rem; font-weight: 700; }}
  .stat-label {{ display: block; font-size: 0.75rem; color: #94a3b8; margin-top: 2px; }}

  .current-player {{ background: linear-gradient(135deg, #1e3a5f, #0f172a); border: 1px solid #2563eb44; border-radius: 14px; padding: 20px; }}
  .cp-name {{ font-size: 1.4rem; font-weight: 700; margin-bottom: 14px; }}
  .cp-name .reg {{ color: #94a3b8; font-weight: 400; font-size: 1rem; }}
  .cp-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 14px 20px; }}
  .cp-grid .label {{ display: block; font-size: 0.75rem; color: #94a3b8; white-space: nowrap; }}
  .cp-grid .value {{ display: block; font-size: 1.05rem; font-weight: 600; word-break: break-word; }}
  .cp-grid .highlight {{ color: #4ade80; }}

  .teams-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 16px; }}
  .team-card {{ background: #1e293b; border-radius: 12px; padding: 16px; }}
  .team-card h3 {{ margin: 0 0 10px; font-size: 1.05rem; }}
  .team-card .short {{ color: #94a3b8; font-weight: 400; font-size: 0.85rem; }}
  .team-stats {{ display: flex; gap: 16px; margin-bottom: 12px; flex-wrap: wrap; }}
  .team-stats .label {{ display: block; font-size: 0.7rem; color: #94a3b8; }}
  .team-stats .value {{ display: block; font-weight: 600; }}
  .squad-table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  .squad-table td {{ padding: 4px 0; border-top: 1px solid #334155; }}
  .squad-table td:last-child {{ text-align: right; color: #4ade80; }}

  table.list-table {{ width: 100%; border-collapse: collapse; background: #1e293b; border-radius: 10px; overflow: hidden; }}
  table.list-table th {{ text-align: left; padding: 8px 12px; background: #273449; font-size: 0.8rem; color: #94a3b8; }}
  table.list-table td {{ padding: 7px 12px; border-top: 1px solid #334155; font-size: 0.9rem; }}
</style>
</head>
<body>
  <h1>🏏 Live Auction Dashboard</h1>
  <div class="meta">
    Generated {generated_at} · refreshes every {REFRESH_SECONDS}s ·
    <span class="token-note">this is a snapshot — re-run /dashboard and re-upload for newer data</span>
  </div>

  <section>
    <h2>Summary</h2>
    <div class="summary-row">{summary_html}</div>
  </section>

  <section>
    <h2>On the Block</h2>
    {current_block_html}
  </section>

  <section>
    <h2>Teams</h2>
    <div class="teams-grid">{teams_html}</div>
  </section>

  <section>
    <h2>Sold Players ({len(sold_players)})</h2>
    <table class="list-table">
      <tr><th>#</th><th>Player</th><th>Team</th><th>Price</th></tr>
      {sold_rows}
    </table>
  </section>

  <section>
    <h2>Unsold Players ({len(unsold_players)})</h2>
    <table class="list-table">
      <tr><th>#</th><th>Player</th></tr>
      {unsold_rows}
    </table>
  </section>
</body>
</html>"""

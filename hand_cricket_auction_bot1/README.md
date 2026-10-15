# Hand Cricket Player Auction Bot

A production-ready Telegram bot that manages **player registration, captain
approval, team creation, and the live player auction** for a Hand Cricket
league. It does **not** implement gameplay, matches, or scorecards — auction
management only.

## Setup

```bash
cd hand_cricket_auction_bot
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env: set BOT_TOKEN and ADMIN_IDS

python main.py
```

## Flow

```
/start → /register → /becomecaptain → admin approves → team created
   → admin: /startauction → /set 1 → bidding → SOLD/UNSOLD → /set 2 → …
   → /unsold (final round for all UNSOLD players)
   → /endauction → final report
```

## Key admin commands

| Command | Purpose |
|---|---|
| `/admin` | Admin panel |
| `/lobby` | Post & pin a live, auto-refreshing lobby status in the group (captains, player pool, min-players requirement) |
| `/set <n>` | Build & show auction set n (max 20 players, by registration order) |
| `/showset <n>` | Detailed player-by-player card view of a set's full pool |
| `/pause` `/resume` | Pause/resume the live auction (timer-safe) |
| `/next` / `/nextplayer` | Bring the next player onto the block (SOLD/UNSOLD no longer auto-advances — this is required after every player) |
| `/undo` | Undo the most recent sale (refund + reversal, audited) |
| `/returnplayer <reg_no>` | Return any sold player to the pool |
| `/reauction_queue` | View/start players returned via undo/return |
| `/unsold` `/unsold_list` | Run/inspect the unsold-players round (sequential, next-in-order) |
| `/auctionplayer <reg_no>` | Jump straight to a specific UNSOLD (or AVAILABLE) player, bypassing order |
| `/auction_status` | Live status: set, player, bid, timer, sold/unsold counts |
| `/sold` `/auctionhistory` | Sold list / full audit trail |
| `/export` | CSV export of all auction data |
| `/exportpdf` | Final auction report as a PDF (team-by-team squads and spend) |
| `/setconfig <key> <value>` | Configure purse, base price, timer, squad size, set size, min players to start, max teams |
| `/setregdeadline <date>` | Set/clear a registration deadline; `/register` auto-blocks after it passes |
| `/setlanguage <en\|hi>` | Toggle player-facing bot language (English/Hindi) |
| `/importplayers` | Bulk-import players from a CSV file (`telegram_id,first_name,last_name,category`) |
| `/blockteam <name>` `/unblockteam <name>` | Temporarily block/unblock a team from bidding |

## New: analytics & extras

| Command | Purpose |
|---|---|
| `/bidhistory <reg_no>` | Every bid ever placed on a player, turn by turn |
| `/topsold [n]` | Leaderboard of the highest-sold players |
| `/teamcompare` | Side-by-side team spending comparison, biggest spender / most frugal |
| `/regdeadline` | Anyone can check the current registration deadline |
| `/tradeplayer <reg_no> <team>` | Trade a SOLD player between teams — no purse change, just ownership swap, with confirmation |
| `/setvicecaptain <telegram_id>` | Primary captain designates a vice-captain who can also `/bid`/`/allin` for the team |
| `/backup` | Downloads the full SQLite database file (WAL-checkpointed first, so it's never missing recent writes) |
| `/remind <minutes> <message>` | Schedules a one-off reminder in the current chat (e.g. "Auction starts in 1 hour") |

"Skip & Return Later" is now a third option (alongside Mark UNSOLD / Return
to Pool) when using `/next` — the player is pushed to the back of the
current set's queue instead of being finalized, so they come back up again
before the set completes, without recording a SOLD/UNSOLD outcome.

Players can optionally tag a playing role (Batsman/Bowler/All-rounder/
Wicket-keeper) right after registering, shown on their `/profile` and in
admin player lists. A 5-second countdown warning is posted automatically
near the end of each player's auction timer, and captains get a one-time
low-purse DM warning once their remaining purse drops below a configurable
percentage (`low_purse_alert_percent`, default 10%) of their starting purse.
Once the group lobby's minimum-player target is reached, a one-time
"Ready to start!" announcement is posted automatically.

All state (players, registration numbers, teams, purses, bids, sold/unsold,
sets, undo/return history, audit log) is persisted in SQLite (`auction.db`),
so the bot recovers safely after a restart — nothing lives only in memory.
Schema changes (like the player category column) apply automatically via a
lightweight migration on startup, so upgrading an existing deployment never
requires manually touching the database.

## Owner-only commands

Owners (set via `OWNER_IDS` in `.env`, fixed and separate from `ADMIN_IDS`)
can do everything admins can, plus:

| Command | Purpose |
|---|---|
| `/addadmin <telegram_id>` | Grant admin access at runtime (no restart needed) |
| `/removeadmin <telegram_id>` | Revoke a runtime-granted admin |
| `/listadmins` | List all owners and admins (fixed + runtime) |
| `/resetall` | Wipe all tournament data for a fresh start (destructive, confirmation required) |
| `/botstats` | Database size, row counts, and current auction state |

## Architecture

- `database/` — schema (`db.py`), raw SQL data access (`queries.py`), enums/dataclasses (`models.py`)
- `services/` — business logic: `player_service`, `captain_service`, `auction_service`, `bidding_service`
- `handlers/` — Telegram glue only (commands/callbacks), no SQL
- `keyboards/` — inline keyboard builders
- `utils/` — logging, timers, formatting helpers

Registration numbers are permanent and never reused (backed by an explicit
counter table, not `MAX()`). Sales are never hard-deleted on undo/return —
every reversal adds a new audited transaction row, preserving full history.

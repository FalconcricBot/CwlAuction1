"""
database/models.py
-------------------
Enums and lightweight dataclasses shared across the whole project.

These are NOT ORM models (we use raw SQL via database/db.py + queries.py),
but they give us type safety and a single source of truth for valid states,
preventing invalid state transitions from being introduced by accident.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class PlayerStatus(str, Enum):
    REGISTERED = "REGISTERED"   # just registered, not yet in an auction pool
    AVAILABLE = "AVAILABLE"     # eligible to appear in the next auction set
    IN_AUCTION = "IN_AUCTION"   # currently on the auction block
    SOLD = "SOLD"
    UNSOLD = "UNSOLD"
    REMOVED = "REMOVED"         # soft-deleted by admin


# Valid forward transitions for a player's status. Used to guard against
# invalid state transitions anywhere in the codebase.
PLAYER_STATUS_TRANSITIONS = {
    PlayerStatus.REGISTERED: {PlayerStatus.AVAILABLE, PlayerStatus.REMOVED},
    PlayerStatus.AVAILABLE: {PlayerStatus.IN_AUCTION, PlayerStatus.REMOVED},
    PlayerStatus.IN_AUCTION: {PlayerStatus.SOLD, PlayerStatus.UNSOLD, PlayerStatus.AVAILABLE},
    PlayerStatus.SOLD: {PlayerStatus.AVAILABLE, PlayerStatus.REMOVED},  # via undo/return
    PlayerStatus.UNSOLD: {PlayerStatus.IN_AUCTION, PlayerStatus.AVAILABLE, PlayerStatus.REMOVED},
    PlayerStatus.REMOVED: {PlayerStatus.AVAILABLE},  # via restore
}


def is_valid_player_transition(old: PlayerStatus, new: PlayerStatus) -> bool:
    if old == new:
        return True
    return new in PLAYER_STATUS_TRANSITIONS.get(old, set())


class CaptainStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    DISABLED = "DISABLED"


class AuctionState(str, Enum):
    IDLE = "IDLE"
    SET_READY = "SET_READY"
    PLAYER_AUCTION = "PLAYER_AUCTION"
    PAUSED = "PAUSED"
    PLAYER_SOLD = "PLAYER_SOLD"
    PLAYER_UNSOLD = "PLAYER_UNSOLD"
    SET_COMPLETED = "SET_COMPLETED"
    MAIN_AUCTION_COMPLETED = "MAIN_AUCTION_COMPLETED"
    UNSOLD_ROUND = "UNSOLD_ROUND"
    AUCTION_COMPLETED = "AUCTION_COMPLETED"


class AuctionMode(str, Enum):
    REGULAR = "REGULAR"          # normal set-based auction (#1-#20, #21-#40 ...)
    UNSOLD_ROUND = "UNSOLD_ROUND"
    REAUCTION = "REAUCTION"      # admin-triggered re-auction of a returned player


class AuditAction(str, Enum):
    PLAYER_REGISTERED = "PLAYER_REGISTERED"
    PLAYER_REMOVED = "PLAYER_REMOVED"
    PLAYER_RESTORED = "PLAYER_RESTORED"
    PLAYER_EDITED = "PLAYER_EDITED"
    CAPTAIN_REQUESTED = "CAPTAIN_REQUESTED"
    CAPTAIN_APPROVED = "CAPTAIN_APPROVED"
    CAPTAIN_REJECTED = "CAPTAIN_REJECTED"
    CAPTAIN_DISABLED = "CAPTAIN_DISABLED"
    TEAM_CREATED = "TEAM_CREATED"
    AUCTION_STARTED = "AUCTION_STARTED"
    SET_STARTED = "SET_STARTED"
    BID_PLACED = "BID_PLACED"
    PLAYER_SOLD = "PLAYER_SOLD"
    PLAYER_UNSOLD = "PLAYER_UNSOLD"
    SALE_UNDONE = "SALE_UNDONE"
    PLAYER_RETURNED = "PLAYER_RETURNED"
    AUCTION_PAUSED = "AUCTION_PAUSED"
    AUCTION_RESUMED = "AUCTION_RESUMED"
    SET_COMPLETED = "SET_COMPLETED"
    AUCTION_COMPLETED = "AUCTION_COMPLETED"
    PLAYER_SKIPPED = "PLAYER_SKIPPED"
    SETTINGS_UPDATED = "SETTINGS_UPDATED"
    PLAYER_TRADED = "PLAYER_TRADED"
    PLAYER_RETAINED = "PLAYER_RETAINED"
    RETENTION_REMOVED = "RETENTION_REMOVED"


@dataclass
class Player:
    id: int
    telegram_id: int
    username: Optional[str]
    first_name: str
    last_name: Optional[str]
    registration_no: int
    status: str
    created_at: str
    team_id: Optional[int] = None
    sold_price: Optional[int] = None

    @property
    def full_name(self) -> str:
        if self.last_name:
            return f"{self.first_name} {self.last_name}"
        return self.first_name


@dataclass
class Team:
    id: int
    name: str
    short_name: str
    captain_id: int
    starting_purse: int
    remaining_purse: int
    max_squad_size: int
    created_at: str

    @property
    def spent(self) -> int:
        return self.starting_purse - self.remaining_purse

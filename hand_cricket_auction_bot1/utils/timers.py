"""
utils/timers.py
----------------
Auction countdown timer helper built on `asyncio` tasks + `python-telegram-bot`'s
JobQueue. The timer is driven by a `deadline_ts` (unix timestamp) persisted in
the database, NOT by in-memory state alone, so that:
  - a bid extends the deadline (timer resets to N seconds)
  - pausing stores "remaining seconds" and clears the deadline
  - resuming recomputes a fresh deadline from the stored remaining seconds
  - if the bot restarts mid-auction, we can recompute remaining time (or, if
    the deadline already passed while the bot was down, finalize immediately)

This module only contains pure helper functions; the actual JobQueue
scheduling glue lives in services/auction_service.py so that it has access
to application/bot context.
"""
from __future__ import annotations

import time


def new_deadline(seconds: int) -> float:
    """Return a unix timestamp `seconds` from now."""
    return time.time() + seconds


def seconds_remaining(deadline_ts: float | None) -> float:
    if deadline_ts is None:
        return 0.0
    return max(0.0, deadline_ts - time.time())


def is_expired(deadline_ts: float | None) -> bool:
    if deadline_ts is None:
        return True
    return time.time() >= deadline_ts

# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared UTC epoch-millisecond date and window helpers."""

from __future__ import annotations

import time
from datetime import UTC, datetime

MIN_MS = 60_000
HOUR_MS = 3_600_000
DAY_MS = 86_400_000


def now_utc_ms() -> int:
    """Return the current UTC wall-clock timestamp in epoch milliseconds."""
    return int(time.time() * 1000)


def ms_to_utc_date(ts_ms: int) -> str:
    """Format UTC epoch milliseconds as ``YYYY-MM-DD``."""
    if ts_ms < 0:
        raise ValueError(f"ts_ms must be >= 0, got {ts_ms}")
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC).strftime("%Y-%m-%d")


def ms_to_utc_month(ts_ms: int) -> str:
    """Format UTC epoch milliseconds as ``YYYY-MM``."""
    if ts_ms < 0:
        raise ValueError(f"ts_ms must be >= 0, got {ts_ms}")
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC).strftime("%Y-%m")


def utc_date_to_start_ms(date_str: str) -> int:
    """Parse ``YYYY-MM-DD`` into UTC midnight (``00:00:00.000``) epoch milliseconds."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def utc_day_floor_ms(ts_ms: int) -> int:
    """Return the UTC midnight epoch millisecond timestamp containing ``ts_ms``."""
    if ts_ms < 0:
        raise ValueError(f"ts_ms must be >= 0, got {ts_ms}")
    return (ts_ms // DAY_MS) * DAY_MS


def next_utc_day_start_ms(ts_ms: int) -> int:
    """Return the next UTC ``00:00:00.000`` boundary strictly after ``utc_day_floor_ms(ts_ms)``."""
    return utc_day_floor_ms(ts_ms) + DAY_MS


def recent_utc_dates(now_ms: int, window_days: int) -> set[str]:
    """Return the set of ``window_days`` UTC ``YYYY-MM-DD`` strings ending on ``now_ms``'s date."""
    if now_ms < 0:
        raise ValueError(f"now_ms must be >= 0, got {now_ms}")
    if window_days < 0:
        raise ValueError(f"window_days must be >= 0, got {window_days}")
    if window_days == 0:
        return set()
    day_floor = utc_day_floor_ms(now_ms)
    return {
        ms_to_utc_date(day_floor - offset * DAY_MS)
        for offset in range(window_days)
        if day_floor - offset * DAY_MS >= 0
    }

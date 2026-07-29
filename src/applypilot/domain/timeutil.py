"""Timestamp parsing. One implementation, because the naive/aware split is a real hazard.

Rows written before the writers standardised on UTC have no timezone
(`2026-07-22 18:50:17`). Subtracting one of those from an aware `now` raises TypeError —
which once took the entire /api/status endpoint down and blanked the dashboard, because the
same parsing was inlined in three places and only one of them was guarded.
"""

from __future__ import annotations

from datetime import datetime, timezone


def parse_ts(value: str | None) -> datetime | None:
    """Parse a stored timestamp as aware UTC. Returns None if absent or unparseable.

    A naive value is assumed UTC — that is what every writer in this codebase produces.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def hours_since(value: str | None, now: datetime) -> float | None:
    """Hours elapsed since `value`, or None if it can't be parsed."""
    dt = parse_ts(value)
    return None if dt is None else (now - dt).total_seconds() / 3600

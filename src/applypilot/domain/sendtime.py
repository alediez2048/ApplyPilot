"""A send promised for LATER — when it may fire, and when the promise has lapsed.

Scheduling is the only thing in this app that acts without the operator present, on the least
reversible action it has. So the interesting rules are not "store a timestamp" — they are the
two edges:

  * **How far ahead may a promise reach?** A month. Past that the draft is stale, the job may
    have closed and the person may have moved; a send written today and fired in April is not
    the message anybody meant.

  * **How LATE may it still fire?** This is the load-bearing one. Nothing fires while the
    dashboard is closed (there is no installed scheduler — CLAUDE.md §Lessons 31), so a send
    promised for 09:00 on a laptop that was shut is not cancelled, it is simply not yet run.
    Firing it at 09:20 is obviously right. Firing it a week later, unattended, on a job whose
    state has moved on, is obviously wrong. `GRACE_HOURS` is where that line is drawn, and past
    it the promise LAPSES rather than fires.

A lapsed promise is deliberately not deleted and not silently sent. It stays on the card
reading *missed*, because the operator needs to make that call with today's information —
"the dashboard was closed" is a fact about our machine, never a reason to mail somebody late.

Pure: no clock of its own, no storage, no imports outside the domain. `now` is always passed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from applypilot.domain.timeutil import parse_ts

#: How far ahead a send may be promised. A month is already generous for a follow-up whose
#: whole premise is that somebody has gone quiet.
MAX_AHEAD_DAYS = 30

#: Default grace window. Overridden by SCHEDULED_SEND_GRACE_HOURS; the settings registry owns
#: the value, this constant is the fallback so the domain stays importable on its own.
GRACE_HOURS = 24

#: What a scheduled send can be.
PENDING = "pending"   # promised, not yet due
DUE = "due"           # due now, inside the grace window — the poller will fire it
MISSED = "missed"     # due, but too long ago to fire unattended — needs the operator


def grace_hours() -> int:
    """The configured grace window.

    `is None` rather than `or GRACE_HOURS`: **0 is a real answer here** and means "never fire
    late — a promise missed by even a minute waits for me". Folding it into the default with
    `or` would silently turn the strictest setting available into the most permissive one,
    which is §Lessons 50 exactly (0 meant "unlimited" in two settings and "send nothing" in a
    third, and setting the daily limit to 0 to switch it OFF would have blocked every send).
    """
    from applypilot import settings
    values, _ = settings.resolve()
    v = values.get("SCHEDULED_SEND_GRACE_HOURS")
    return GRACE_HOURS if v is None else max(0, int(v))


def validate(raw: str, now: datetime | None = None) -> tuple[str, str]:
    """(iso_utc, error). Exactly one of the two is ever non-empty.

    Refuses rather than rounds. A time that cannot be parsed, is in the past, or reaches past
    `MAX_AHEAD_DAYS` is an operator mistake worth naming — silently clamping it to "now" would
    send immediately, which is the one outcome somebody choosing a future time did not want.
    """
    now = now or datetime.now(timezone.utc)
    when = parse_ts((raw or "").strip())
    if when is None:
        return "", "that is not a time I can read — pick a date and time"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if when <= now:
        return "", "that time has already passed — pick one in the future"
    if when > now + timedelta(days=MAX_AHEAD_DAYS):
        return "", (f"that is more than {MAX_AHEAD_DAYS} days out — a draft written today "
                    "will not still be the right message")
    return when.astimezone(timezone.utc).isoformat(), ""


def state(scheduled_at: str, now: datetime | None = None,
          grace_hours: int | None = None) -> str:
    """PENDING | DUE | MISSED, or '' when nothing is scheduled.

    The MISSED branch is why this is a function rather than a `<=` at the call site: the poller
    and the dashboard must agree on it exactly. If the poller thought a send was due and the
    card called it missed, the operator would be looking at a row saying "you have to do this
    yourself" while the machine did it anyway.
    """
    when = parse_ts((scheduled_at or "").strip())
    if when is None:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if when > now:
        return PENDING
    grace = GRACE_HOURS if grace_hours is None else grace_hours
    return DUE if when > now - timedelta(hours=grace) else MISSED


def lapsed_before(now: datetime | None = None, grace_hours: int | None = None) -> str:
    """The cutoff a store query compares against: anything older than this has lapsed.

    Returned as an ISO string so the caller can put it straight in SQL. Deriving it here keeps
    the grace window in ONE place — a query that hardcoded `-24 hours` would drift away from
    `state()` above the first time the setting moved, and the two disagreeing is precisely the
    failure this module exists to prevent.
    """
    now = now or datetime.now(timezone.utc)
    grace = GRACE_HOURS if grace_hours is None else grace_hours
    return (now - timedelta(hours=grace)).astimezone(timezone.utc).isoformat()

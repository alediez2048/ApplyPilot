"""Follow-up ladders — ONE engine, parameterised by channel.

Before ARCH-1 this was two near-identical implementations (email and LinkedIn) plus a third
copy of the same date arithmetic inside the checklist. ARCH-1 collapsed the arithmetic;
ARCH-3 collapsed the storage, which is what removed the last per-channel branches.

A channel differs only in: which timestamp starts the clock, what proves the channel has
been used at all, which schedule it follows, and whether we may send it ourselves. That is
a data difference, so it lives in CHANNELS — not in branching code.

Ladder STATE (how many touches have gone out, when the last one was, whether the sequence
is stopped) is no longer read off channel-specific columns. It arrives as a `ladder` dict
with the same shape for every channel, from `networking/touches.py`. That is why this module
now names no database column of its own beyond the anchor.

Adding SMS is one entry here plus one prompt. If it ever needs a new `if`, this has regressed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from applypilot.domain.timeutil import hours_since

# Statuses that stop a ladder permanently, on any channel. These live on the SEQUENCE,
# not on a touch — a reply is a fact about the conversation, not about a message we sent.
TERMINAL = ("stopped", "replied")

# The shape `touches.ladder_state()` returns. Duplicated here as a default so the domain
# stays importable without the networking package (the ARCH-1 boundary).
EMPTY_LADDER = {"count": 0, "last_sent_at": "", "sequence_status": "",
                "touch_status": "", "draft_subject": "", "draft_body": "", "error": ""}


@dataclass(frozen=True)
class Channel:
    """How one outreach channel schedules its follow-ups.

    `ready` is a tuple of (field, allowed_values | None) read off the contact. Both entries
    must hold for the channel to apply. Expressing readiness as DATA is what deleted the
    `if channel is EMAIL: ... if channel is LINKEDIN: ...` branch that used to live here —
    the branch the module docstring had warned about since ARCH-1.
    """
    name: str
    env_var: str                       # schedule override, comma-separated hours
    default_schedule: tuple[int, ...]
    start_field: str                   # contacts column: first message on this channel
    ready: tuple[tuple[str, tuple[str, ...] | None], ...] = field(default=())
    can_autosend: bool = True          # LinkedIn is copy-paste only — CLAUDE.md §Lessons
    prefix: str = ""                   # payload key prefix, for the dashboard's field names


EMAIL = Channel(
    name="email",
    env_var="FOLLOWUP_SCHEDULE",
    default_schedule=(48, 96, 168),          # 2d / 4d / 7d
    start_field="submitted_at",
    # `emailed` is derived (bool of sent_message_id) — an email we have no message id for
    # was never actually delivered, so it must not start a ladder.
    ready=(("email", None), ("emailed", None)),
    prefix="",
)

# Slower on purpose: someone who just accepted your invite is a long-lived, low-urgency
# thread, and nudging a brand-new connection after 48h reads badly.
LINKEDIN = Channel(
    name="linkedin",
    env_var="LINKEDIN_FOLLOWUP_SCHEDULE",
    default_schedule=(120, 288),             # 5d / 12d
    start_field="dm_sent_at",
    # An invite must have been RECORDED; dm_status is what proves one went out.
    ready=(("linkedin_url", None), ("dm_status", ("sent", "manual"))),
    can_autosend=False,
    prefix="li_",
)

CHANNELS = (EMAIL, LINKEDIN)


def channel_by_name(name: str) -> Channel | None:
    return next((c for c in CHANNELS if c.name == name), None)


def channel_schedule(channel: Channel) -> list[int]:
    """Hours after the previous message that each touch comes due.

    Parsing and validation live in `settings.py` (ARCH-6). This still falls back to the
    channel default on a bad value, because by the time the ladder is being computed the
    startup check has already refused to run with one — reaching the fallback here means
    something set the variable after startup, and dropping follow-ups is worse than using
    the documented default.
    """
    from applypilot import settings
    values, _ = settings.resolve()
    got = values.get(channel.env_var)
    return list(got) if got else list(channel.default_schedule)


def _is_ready(contact: dict, channel: Channel) -> bool:
    """Has this channel been used at all? No first message means nothing to follow up on."""
    for name, allowed in channel.ready:
        value = contact.get(name)
        if not value:
            return False
        if allowed and str(value) not in allowed:
            return False
    return True


def touch_state(contact: dict, channel: Channel, schedule: list[int], now: datetime,
                ladder: dict | None = None) -> tuple[str, int | None]:
    """(state, hours_until_due) for one contact on one channel.

    state ∈ '' | due | waiting | finished | stopped | replied.
    '' means this channel does not apply to this contact at all.
    """
    ladder = ladder if ladder is not None else EMPTY_LADDER
    if not _is_ready(contact, channel):
        return "", None
    status = ladder.get("sequence_status") or ""
    if status in TERMINAL:
        return status, None
    count = ladder.get("count") or 0
    if count >= len(schedule):
        return "finished", None
    # The clock runs from the most recent message we sent them on this channel.
    anchor = (ladder.get("last_sent_at") or contact.get(channel.start_field) or "").strip()
    since = hours_since(anchor, now)
    if since is None:
        return "", None
    need = schedule[count]
    return ("due", 0) if since >= need else ("waiting", round(need - since))


def followup_panel(contacts: list[dict], now: datetime | None = None,
                   ladders: dict[tuple[str, str], dict] | None = None) -> dict:
    """Who is owed a follow-up, per channel.

    `ladders` maps (contact_id, channel_name) -> ladder state, exactly what
    `touches.ladder_states()` returns. Omitted, every ladder reads as empty — which keeps
    this callable from a test with no database.

    Annotates each contact with `<prefix>followup_state` / `<prefix>followup_due_in_h` /
    `<prefix>followup_touch` so the per-contact buttons and this panel agree on who is due —
    one computation, not two that can drift.
    """
    now = now or datetime.now(timezone.utc)
    ladders = ladders or {}
    schedules = {c.name: channel_schedule(c) for c in CHANNELS}
    buckets: dict[str, dict[str, list[dict]]] = {
        c.name: {"due": [], "waiting": [], "finished": [], "stopped": []} for c in CHANNELS
    }

    for contact in contacts:
        for channel in CHANNELS:
            ladder = ladders.get((contact.get("id"), channel.name)) or EMPTY_LADDER
            pre = channel.prefix
            state, due_in = touch_state(contact, channel, schedules[channel.name], now, ladder)
            contact[f"{pre}followup_state"] = state
            contact[f"{pre}followup_due_in_h"] = due_in
            contact[f"{pre}followup_touch"] = (ladder.get("count") or 0) + 1
            if state in buckets[channel.name]:
                buckets[channel.name][state].append(contact)
            elif state in TERMINAL:
                buckets[channel.name]["stopped"].append(contact)
    # NOTE: `followup_due` is deliberately NOT set here. It belongs to the checklist, which
    # uses a different rule (FOLLOWUP_AFTER_DAYS since the FIRST email, ignoring the ladder
    # position). Setting it here silently overwrote the checklist's answer for any contact
    # already followed up once — caught by the byte-identical /api/status check.

    def brief(items: list[dict], channel: Channel) -> list[dict]:
        pre = channel.prefix
        return [{"id": c["id"], "full_name": c["full_name"], "title": c.get("title", ""),
                 "touch": c[f"{pre}followup_touch"], "due_in_h": c[f"{pre}followup_due_in_h"],
                 "state": c[f"{pre}followup_state"]} for c in items]

    e, li = buckets[EMAIL.name], buckets[LINKEDIN.name]
    return {
        "due": brief(e["due"], EMAIL), "waiting": brief(e["waiting"], EMAIL),
        "finished": brief(e["finished"], EMAIL), "stopped": brief(e["stopped"], EMAIL),
        "due_count": len(e["due"]), "total_touches": len(schedules[EMAIL.name]),
        "schedule": schedules[EMAIL.name],
        "li_due": brief(li["due"], LINKEDIN), "li_waiting": brief(li["waiting"], LINKEDIN),
        "li_due_count": len(li["due"]), "li_total_touches": len(schedules[LINKEDIN.name]),
        "li_schedule": schedules[LINKEDIN.name],
    }

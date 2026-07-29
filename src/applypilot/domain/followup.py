"""Follow-up ladders — ONE engine, parameterised by channel.

Before ARCH-1 this was two near-identical implementations (email and LinkedIn) plus a third
copy of the same date arithmetic inside the checklist. Three implementations of "is it due
yet" that had to agree, with no test forcing them to.

A channel differs only in: which timestamp starts the clock, which timestamps advance it,
which schedule it uses, which fields hold its state, and whether we may send it ourselves.
That is a data difference, so it lives in CHANNELS — not in branching code.

Adding SMS should be one entry here plus one prompt. If it ever needs a new `if`, this
module has regressed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from applypilot.domain.timeutil import hours_since

# Statuses that stop a ladder permanently, on any channel.
TERMINAL = ("stopped", "replied")


@dataclass(frozen=True)
class Channel:
    """How one outreach channel schedules and stores its follow-ups."""
    name: str
    env_var: str                # schedule override, comma-separated hours
    default_schedule: tuple[int, ...]
    start_field: str            # timestamp of the FIRST message on this channel
    last_field: str             # timestamp of the most recent follow-up
    count_field: str
    status_field: str
    draft_field: str
    ready: tuple[str, ...] = ()  # extra fields that must be truthy to schedule at all
    can_autosend: bool = True    # LinkedIn is copy-paste only — see CLAUDE.md §Lessons


EMAIL = Channel(
    name="email",
    env_var="FOLLOWUP_SCHEDULE",
    default_schedule=(48, 96, 168),          # 2d / 4d / 7d
    start_field="submitted_at",
    last_field="followed_up_at",
    count_field="followup_count",
    status_field="followup_status",
    draft_field="followup_message",
    ready=("email", "emailed"),
)

# Slower on purpose: someone who just accepted your invite is a long-lived, low-urgency
# thread, and nudging a brand-new connection after 48h reads badly.
LINKEDIN = Channel(
    name="linkedin",
    env_var="LINKEDIN_FOLLOWUP_SCHEDULE",
    default_schedule=(120, 288),             # 5d / 12d
    start_field="dm_sent_at",
    last_field="li_followed_up_at",
    count_field="li_followup_count",
    status_field="li_followup_status",
    draft_field="li_followup_message",
    ready=("linkedin_url",),
    can_autosend=False,
)

CHANNELS = (EMAIL, LINKEDIN)


def channel_schedule(channel: Channel) -> list[int]:
    """Hours after the previous message that each touch comes due."""
    raw = os.environ.get(channel.env_var, ",".join(str(h) for h in channel.default_schedule))
    out = []
    for part in raw.split(","):
        try:
            h = int(part.strip())
        except ValueError:
            continue
        if h > 0:
            out.append(h)
    return out or list(channel.default_schedule)


def _is_ready(contact: dict, channel: Channel) -> bool:
    """Has this channel been used at all? No first message means nothing to follow up on."""
    if channel is EMAIL:
        return bool(contact.get("email")) and bool(contact.get("emailed"))
    if channel is LINKEDIN:
        # An invite must have been RECORDED; dm_status is what proves one went out.
        return bool(contact.get("linkedin_url")) and \
            (contact.get("dm_status") or "") in ("sent", "manual")
    return all(contact.get(f) for f in channel.ready)


def touch_state(contact: dict, channel: Channel, schedule: list[int],
                now: datetime) -> tuple[str, int | None]:
    """(state, hours_until_due) for one contact on one channel.

    state ∈ '' | due | waiting | finished | stopped | replied.
    '' means this channel does not apply to this contact at all.
    """
    if not _is_ready(contact, channel):
        return "", None
    status = contact.get(channel.status_field) or ""
    if status in TERMINAL:
        return status, None
    count = contact.get(channel.count_field) or 0
    if count >= len(schedule):
        return "finished", None
    # The clock runs from the most recent message we sent them on this channel.
    anchor = (contact.get(channel.last_field) or contact.get(channel.start_field) or "").strip()
    since = hours_since(anchor, now)
    if since is None:
        return "", None
    need = schedule[count]
    return ("due", 0) if since >= need else ("waiting", round(need - since))


def followup_panel(contacts: list[dict], now: datetime | None = None) -> dict:
    """Who is owed a follow-up, per channel.

    Annotates each contact with `<prefix>followup_state` / `<prefix>followup_due_in_h` /
    `<prefix>followup_touch` so the per-contact buttons and this panel agree on who is due —
    one computation, not two that can drift.
    """
    now = now or datetime.now(timezone.utc)
    schedules = {c.name: channel_schedule(c) for c in CHANNELS}
    buckets: dict[str, dict[str, list[dict]]] = {
        c.name: {"due": [], "waiting": [], "finished": [], "stopped": []} for c in CHANNELS
    }

    for contact in contacts:
        for channel in CHANNELS:
            pre = "" if channel is EMAIL else "li_"
            state, due_in = touch_state(contact, channel, schedules[channel.name], now)
            contact[f"{pre}followup_state"] = state
            contact[f"{pre}followup_due_in_h"] = due_in
            contact[f"{pre}followup_touch"] = (contact.get(channel.count_field) or 0) + 1
            if state == "due":
                buckets[channel.name]["due"].append(contact)
            elif state == "waiting":
                buckets[channel.name]["waiting"].append(contact)
            elif state == "finished":
                buckets[channel.name]["finished"].append(contact)
            elif state in TERMINAL:
                buckets[channel.name]["stopped"].append(contact)
    # NOTE: `followup_due` is deliberately NOT set here. It belongs to the checklist, which
    # uses a different rule (FOLLOWUP_AFTER_DAYS since the FIRST email, ignoring the ladder
    # position). Setting it here silently overwrote the checklist's answer for any contact
    # already followed up once — caught by the byte-identical /api/status check.

    def brief(items: list[dict], channel: Channel) -> list[dict]:
        pre = "" if channel is EMAIL else "li_"
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

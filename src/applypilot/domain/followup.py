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
    # How this channel names itself in operator-facing text ("LinkedIn sequence stopped").
    # Email's is empty because it is the unmarked case — "sequence stopped" already means email.
    # This is a field rather than a lookup because the dashboard had the last per-channel
    # branch left in the codebase sitting on it: `"LinkedIn " if channel.name == "linkedin"`.
    label: str = ""


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
    label="LinkedIn ",
)

# Texting is the most intrusive channel here and the only one that arrives on a lock screen,
# so it is the slowest and the shortest ladder: two touches, 3d then 7d. A text at 24h reads
# as pressure from someone who is not yet owed a reply.
#
# `ready` requires BOTH a phone and proof one was actually sent. The phone alone is not enough
# — it is entered by hand for anyone the operator might text, so keying readiness on it would
# put a "follow-up due" badge on every contact with a number nobody has ever messaged. Email
# proves itself with `emailed` and LinkedIn with `dm_status`; this is the same rule, and it is
# the one thing the SMS channel in test_adding_a_channel_needs_no_schema_change gets wrong —
# that test drives the ENGINE, and its illustrative config was never a shipping config.
SMS = Channel(
    name="sms",
    env_var="SMS_FOLLOWUP_SCHEDULE",
    default_schedule=(72, 168),              # 3d / 7d
    start_field="sms_sent_at",
    ready=(("phone", None), ("sms_sent_at", None)),
    can_autosend=False,                      # copy → open Messages → you paste. §Lessons 3.
    prefix="sms_",
    label="text ",
)

CHANNELS = (EMAIL, LINKEDIN, SMS)


def channel_by_name(name: str) -> Channel | None:
    return next((c for c in CHANNELS if c.name == name), None)


def channel_schedule(channel: Channel, space=None) -> list[int]:
    """Hours after the previous message that each touch comes due.

    A Space may override the cadence per channel (SPACE-4). A C-suite pitch nudged at 48h
    reads as pressure from a stranger, where the same cadence to a recruiter is normal — so
    `schedules={"email": [120, 288]}` on the manifest replaces the global for that Space only.

    The override wins over the environment, which is the right precedence and worth stating:
    the env var is the default for every campaign, the manifest is this campaign's decision,
    and a stored decision that a global could silently override is not a decision.

    Parsing and validation live in `settings.py` (ARCH-6). This still falls back to the
    channel default on a bad value, because by the time the ladder is being computed the
    startup check has already refused to run with one — reaching the fallback here means
    something set the variable after startup, and dropping follow-ups is worse than using
    the documented default.
    """
    override = (getattr(space, "schedules", None) or {}).get(channel.name)
    if override:
        return [int(h) for h in override]
    from applypilot import settings
    values, _ = settings.resolve()
    got = values.get(channel.env_var)
    return list(got) if got else list(channel.default_schedule)


def normalize_for_ladder(contact: dict) -> dict:
    """Fill the DERIVED fields the ladder reads, so a raw DB row works as well as a UI payload.

    `emailed` is not a column — the dashboard computes it in `_contact_payload` from
    `sent_message_id`. Anything passing raw rows straight from the database therefore saw every
    email channel as "never used" and reported ZERO follow-ups due, silently. `applypilot tick`
    did exactly that: the dashboard showed 3 due while tick found none.

    Idempotent, so a payload that already carries the field is untouched.
    """
    if "emailed" in contact:
        return contact
    return {**contact, "emailed": bool((contact.get("sent_message_id") or "").strip())}


def _is_ready(contact: dict, channel: Channel) -> bool:
    """Has this channel been used at all? No first message means nothing to follow up on."""
    for name, allowed in channel.ready:
        value = contact.get(name)
        if not value:
            return False
        if allowed and str(value) not in allowed:
            return False
    return True


def exhausted(contact: dict, ladders: dict[str, dict] | None = None,
              now: datetime | None = None, space=None) -> bool:
    """Has every channel we actually used run out, with nothing to show for it?

    "No response" — the honest end state of an outreach attempt. Distinct from every other
    state the UI already shows:

      * `finished` is per-CHANNEL. Someone whose email ladder is done but whose LinkedIn
        sequence is still running has not gone quiet; they have one channel left.
      * `stopped` is a decision the operator made, not an outcome.
      * a contact nobody ever wrote to is not unresponsive — they are untouched, which is a
        completely different action (write to them) and must never wear this label.

    DERIVED, never stored. A column would have to be recomputed every time a touch is sent, a
    reply arrives, or a schedule changes, and the version on disk would be wrong in between —
    the §Lessons 21 failure with a new name. This is computed at render time from the same
    ladder state the follow-up panel reads, so the two cannot disagree.

    A reply of any kind disqualifies immediately: `replied_at` is the fact, and a sequence
    marked `replied` says the same thing from the other direction.
    """
    if (contact.get("replied_at") or "").strip():
        return False
    now = now or datetime.now(timezone.utc)
    ladders = ladders or {}
    contact = normalize_for_ladder(contact)

    used_any = False
    for channel in channels_for(space):
        ladder = ladders.get(channel.name) or EMPTY_LADDER
        state, _ = touch_state(contact, channel, channel_schedule(channel, space), now, ladder)
        if not state:
            continue                       # channel never applied to this person
        used_any = True
        if state == "replied":
            return False
        if state != "finished" and state != "stopped":
            return False                   # still due or waiting — the attempt is not over
    return used_any


def channels_for(space=None) -> tuple:
    """The channels a Space offers, in registry order.

    A Space may narrow the set (SPACE-4) — a business campaign that never texts should not show
    a Text tab or count an SMS ladder as outstanding work. Names that match no registered
    channel are IGNORED rather than raising: a manifest is operator-editable config, and a typo
    must not take the follow-up engine down for every Space at once.

    An EMPTY result falls back to all channels, deliberately. A Space with no channels can send
    nothing, and silently offering nothing is the failure §Lessons 15 is about — the panel would
    render empty and look identical to one where nobody is due.
    """
    names = getattr(space, "channels", None)
    if not names:
        return CHANNELS
    chosen = tuple(c for c in CHANNELS if c.name in set(names))
    return chosen or CHANNELS


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
                   ladders: dict[tuple[str, str], dict] | None = None, space=None) -> dict:
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
    active = channels_for(space)
    schedules = {c.name: channel_schedule(c, space) for c in active}
    buckets: dict[str, dict[str, list[dict]]] = {
        c.name: {"due": [], "waiting": [], "finished": [], "stopped": []} for c in active
    }

    # Normalise ONCE, here, rather than at each call site: the dashboard passes UI payloads
    # and `tick` passes raw DB rows, and only one of those carries the derived `emailed`
    # field the email ladder needs.
    contacts = [normalize_for_ladder(c) for c in contacts]

    for contact in contacts:
        for channel in active:
            ladder = ladders.get((contact.get("id"), channel.name)) or EMPTY_LADDER
            pre = channel.prefix
            state, due_in = touch_state(contact, channel, schedules[channel.name], now, ladder)
            contact[f"{pre}followup_state"] = state
            contact[f"{pre}followup_due_in_h"] = due_in
            contact[f"{pre}followup_touch"] = (ladder.get("count") or 0) + 1
            # The DENOMINATOR travels with the contact. Without it the per-contact card had to
            # be handed a literal, and the dashboard passed `3` — so with any schedule that is
            # not three entries the Email tab read "touch 2 of 3" while the Follow-ups tab read
            # the real total for the same person, on the same screen.
            contact[f"{pre}followup_total"] = len(schedules[channel.name])
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

    # Built FROM `CHANNELS`, not from two named locals. The old version destructured
    # `buckets[EMAIL.name], buckets[LINKEDIN.name]` and spelled out both key sets by hand, so a
    # third channel reached this line having passed through every other part of the engine
    # untouched — and then vanished, because nothing put it in the payload. The docstring
    # promised one registry row; this is the line that made that false.
    #
    # The email prefix is "" and LinkedIn's is "li_", so this emits exactly the keys that
    # already shipped. `li_finished` / `li_stopped` are new and additive — no consumer reads a
    # key it did not before.
    out: dict = {}
    for channel in active:
        b, pre = buckets[channel.name], channel.prefix
        out.update({
            f"{pre}due": brief(b["due"], channel),
            f"{pre}waiting": brief(b["waiting"], channel),
            f"{pre}finished": brief(b["finished"], channel),
            f"{pre}stopped": brief(b["stopped"], channel),
            f"{pre}due_count": len(b["due"]),
            f"{pre}total_touches": len(schedules[channel.name]),
            f"{pre}schedule": schedules[channel.name],
        })
    return out

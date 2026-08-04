"""When something last happened on a job, and WHO did it.

The dashboard could say how far a job had travelled — Found → Applied → Emailed → Follow up —
and not when anything last happened. Six sources knew, none of them joined:

    applied              jobs.applied_at
    outreach email       contacts.submitted_at
    follow-up sent       touches.sent_at, any channel
    they replied         contacts.replied_at / messages
    LinkedIn invite/DM   contacts.dm_sent_at, interactions (UX-2)
    deck open, booking   contacts.deck_last_at, interactions

Derived, never stored. A `last_interaction_at` column would have to be written by all six of
those paths and would be wrong the first time one of them forgot — which is §Lessons 21 with a
new name, and the reason `domain/interactions.py` derives its timeline too.

**Direction is the point.** "Emailed them 6 days ago" and "they replied 6 days ago" are the
same age and opposite situations: one is work you have done, the other is work you owe. A
single timestamp with no direction is the flat count the 🔔 counter already had to abandon.
"""

from __future__ import annotations

from applypilot.domain import interactions as ix
from applypilot.domain.timeutil import parse_ts

#: Their actions vs ours. Deliberately the same line `domain/interactions.py` draws for
#: engagement, so a signal cannot be "engagement" here and "our own action" there.
_THEIRS = set(ix.ENGAGEMENT)

#: How each kind reads with a name in front of it. `{who}` is a first name, never a full one:
#: this sits on a table row and "Sarah" fits where "Sarah Chen-Okonkwo" does not.
_INBOUND_LABEL = {
    ix.REPLIED: "{who} replied",
    ix.LINKEDIN_IN: "{who} messaged you on LinkedIn",
    ix.BOOKED: "{who} booked a call",
    ix.DECK: "{who} opened your deck",
    ix.PROFILE_VIEW: "{who} viewed your profile",
}
_OUTBOUND_LABEL = {
    ix.SENT: "You emailed {who}",
    ix.CONNECTED: "You invited {who} on LinkedIn",
    ix.LINKEDIN_OUT: "You replied to {who}",
    "followup": "You followed up with {who}",
    "sms": "You texted {who}",
    "applied": "You applied",
}


def _first_name(name: str | None) -> str:
    return (name or "").strip().split()[0] if (name or "").strip() else "them"


def _event(at: str, kind: str, who: str = "") -> dict | None:
    """One candidate, or None when the timestamp is unusable.

    Rows written before timezones were consistent are naive (§Lessons 6); `parse_ts` is the one
    implementation of that guard, and anything it cannot read is dropped rather than compared.
    """
    when = parse_ts(at)
    if not when:
        return None
    inbound = kind in _THEIRS
    template = (_INBOUND_LABEL if inbound else _OUTBOUND_LABEL).get(kind, "{who}")
    return {"at": at, "when": when, "kind": kind,
            "direction": "in" if inbound else "out",
            "who": who, "label": template.format(who=_first_name(who) if who else "them")}


def last_interaction(job: dict, contacts: list[dict] | None = None,
                     ladders: dict | None = None) -> dict | None:
    """The most recent thing that happened on this job, or None if nothing has.

    Everything here is already on the payload — the contact rows, their derived interaction
    timeline, and the ladder states the follow-up panel loads anyway. No query of its own, on a
    path that re-renders every 2.5 seconds against an 80-statement budget (§Lessons 11, 26).
    """
    candidates: list[dict] = []
    ladders = ladders or {}

    for contact in (contacts or []):
        who = contact.get("full_name") or ""
        cid = contact.get("id")
        # The derived timeline already covers sends, replies, deck opens, bookings, invites
        # and LinkedIn messages — reuse it rather than re-deriving from the same columns.
        for row in (contact.get("interactions") or []):
            candidates.append(_event(row.get("at", ""), row.get("kind", ""), who))
        # Follow-ups live in `touches` and are NOT in that timeline: it is per-contact state,
        # and a touch is per (contact, channel). Without these a job whose only recent activity
        # was a third follow-up reports the original email from two weeks earlier.
        for channel in ("email", "linkedin", "sms"):
            state = ladders.get((cid, channel)) or {}
            if state.get("last_sent_at"):
                candidates.append(_event(state["last_sent_at"], "followup", who))
        if (contact.get("sms_sent_at") or "").strip():
            candidates.append(_event(contact["sms_sent_at"], "sms", who))

    # The floor. A job with no contacts at all still has a date worth showing, and "nothing has
    # happened" is wrong for something you applied to.
    if (job.get("applied_at") or "").strip():
        candidates.append(_event(job["applied_at"], "applied"))

    usable = [c for c in candidates if c]
    if not usable:
        return None
    best = max(usable, key=lambda c: c["when"])
    return {k: v for k, v in best.items() if k != "when"}

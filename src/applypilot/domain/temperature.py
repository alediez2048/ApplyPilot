"""How an application is actually DOING, as opposed to how far it has travelled.

The status strip measures distance — Found → Applied → Emailed 4/4 → Follow up 2/4 — and every
number in it counts work the operator did. Two jobs reading exactly that can be a live
conversation and a dead one.

**The trap here is written down and this module is shaped to avoid it.** §Lessons 35: the first
Interactions tab counted our own LinkedIn invites as engagement, so three live jobs read "3/3
engaged" before anyone had done anything, while the honest number across every job was 2 of 58.
A temperature built on effort is that bug with a colour on it — and worse, because a colour is
read at a glance and trusted without checking.

So: **only their actions can raise it. Ours can only lower it.** Sending twelve emails and
hearing nothing is colder than sending one and getting an answer, and any design where those
two compare the other way round is wrong regardless of how the numbers are tuned.

Bands rather than a percentage. The inputs are six booleans and a clock; a number out of 100
implies a precision that is not there, and invites tuning the constant instead of answering the
email. Every band carries the sentence that produced it — an unexplained colour is ignored
within a week, which is the §Lessons 43 failure applied to information rather than controls.
"""

from __future__ import annotations

from datetime import datetime, timezone

from applypilot.domain import interactions as ix
from applypilot.domain.timeutil import parse_ts

#: Terminal and off the scale — an outcome, not a temperature. Ranking a won job against a
#: cooling one is comparing two different questions.
WON = "won"
#: Also terminal, and NOT cold: mail is being rejected. "Nobody is answering" and "nothing is
#: arriving" have opposite fixes, and calling the second one cold hides an address to correct.
UNDELIVERABLE = "undeliverable"
WARM, ACTIVE, COOLING, COLD, NEW = "warm", "active", "cooling", "cold", "new"

#: Days since THEIR last action. Deliberately generous at the top: a recruiter who replied nine
#: days ago is still a live conversation, and marking it cooling would send you chasing someone
#: who is simply busy.
_WARM_DAYS = 10
_ACTIVE_DAYS = 24

#: Effort that has gone unanswered. Only ever lowers the reading (§Lessons 35).
_COLD_TOUCHES = 3
_COLD_DAYS = 14

LABEL = {WON: "won", UNDELIVERABLE: "undeliverable", WARM: "warm", ACTIVE: "active",
         COOLING: "cooling", COLD: "cold", NEW: "new"}
#: A dot AND a word, never colour alone — colour-blind readers, and screenshots pasted into a
#: document, both have to survive.
ICON = {WON: "🏆", UNDELIVERABLE: "⚠", WARM: "●", ACTIVE: "●", COOLING: "◐", COLD: "○",
        NEW: "·"}


def _days_since(ts: str, now: datetime | None = None) -> float | None:
    when = parse_ts(ts)
    if not when:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - when).total_seconds() / 86400)


def _their_last_action(contacts: list[dict]) -> tuple[str, str, str]:
    """(iso, kind, who) of the most recent thing THEY did, or ("", "", "").

    Reads `ix.ENGAGEMENT`, the same line `domain/interactions` and `domain/lastinteraction`
    draw. A third list of "what counts as them" is the copy that falls behind.
    """
    best = ("", "", "")
    best_at = None
    for contact in contacts or []:
        for row in contact.get("interactions") or []:
            if row.get("kind") not in ix.ENGAGEMENT:
                continue
            when = parse_ts(row.get("at", ""))
            if when and (best_at is None or when > best_at):
                best_at, best = when, (row.get("at", ""), row.get("kind", ""),
                                       contact.get("full_name") or "")
    return best


def _effort(contacts: list[dict], ladders: dict | None) -> int:
    """Messages WE have sent across every contact and channel. Never raises the reading."""
    ladders = ladders or {}
    n = 0
    for contact in contacts or []:
        if (contact.get("submitted_at") or "").strip():
            n += 1
        for channel in ("email", "linkedin", "sms"):
            n += (ladders.get((contact.get("id"), channel)) or {}).get("count", 0) or 0
    return n


def temperature(job: dict, contacts: list[dict] | None = None,
                ladders: dict | None = None, now: datetime | None = None) -> dict:
    """`{band, label, icon, reason}` — how this application is doing, and why.

    `reason` is not decoration. A band with no explanation is a colour, and a colour nobody can
    interrogate stops being read.
    """
    contacts = contacts or []

    if (job.get("interview_at") or "").strip():
        return _band(WON, "An interview is scheduled.")

    emailed = [c for c in contacts if (c.get("submitted_at") or "").strip()]
    if emailed and all(c.get("email_status") == "bounced" for c in emailed):
        return _band(UNDELIVERABLE,
                     f"Mail to {'both' if len(emailed) == 2 else 'every'} address here is "
                     f"bouncing. Nothing is arriving — the addresses need fixing, not chasing."
                     if len(emailed) > 1 else
                     "Mail to the only address here is bouncing. Nothing is arriving.")

    at, kind, who = _their_last_action(contacts)
    effort = _effort(contacts, ladders)
    days = _days_since(at, now) if at else None
    first = who.split()[0] if who else "They"

    if days is not None:
        what = {ix.BOOKED: f"{first} booked a call", ix.REPLIED: f"{first} replied",
                ix.LINKEDIN_IN: f"{first} messaged you on LinkedIn",
                ix.DECK: f"{first} opened your deck",
                ix.PROFILE_VIEW: f"{first} viewed your profile"}.get(kind, f"{first} engaged")
        ago = "today" if days < 1 else f"{int(days)}d ago"
        if days <= _WARM_DAYS:
            return _band(WARM, f"{what} {ago}.")
        if days <= _ACTIVE_DAYS:
            return _band(ACTIVE, f"{what} {ago} — still recent, but going quiet.")
        return _band(COOLING, f"{what} {ago} and nothing since.")

    if not effort:
        return _band(NEW, "Nothing sent yet." if not contacts else
                     "Contacts found, nothing sent yet.")

    applied_days = _days_since(job.get("applied_at") or "", now)
    waited = int(applied_days) if applied_days is not None else 0
    if effort >= _COLD_TOUCHES and waited >= _COLD_DAYS:
        return _band(COLD, f"{effort} messages sent, no answer from anyone in {waited} days.")
    return _band(COOLING, f"{effort} message{'s' if effort != 1 else ''} sent, no reply yet.")


def _band(band: str, reason: str) -> dict:
    return {"band": band, "label": LABEL[band], "icon": ICON[band], "reason": reason}

"""How an application is actually DOING, as opposed to how far it has travelled.

The status strip measures distance — Found → Applied → Emailed 4/4 → Follow up 2/4 — and every
number in it counts work the operator did. Two jobs reading exactly that can be a live
conversation and a dead one.

**The trap here is written down and this module is shaped to avoid it.** §Lessons 35: the first
Interactions tab counted our own LinkedIn invites as engagement, so three live jobs read "3/3
engaged" before anyone had done anything, while the honest number across every job was 2 of 58.
A temperature built on effort is that bug with a colour on it — and worse, because a colour is
read at a glance and trusted without checking.

So: **only their actions can reach `warm`.** Sending twelve emails and hearing nothing is
colder than sending one and getting an answer, and any design where those two compare the other
way round is wrong regardless of how the numbers are tuned.

**What the first version got wrong** (reported 2026-08-04: "a number of applications marked
cooling, which is weird because they are fairly recent"). `COOLING` was the FALLBACK for every
job with any effort and no reply — there was no band between "nothing sent" and "cold". On the
live board it was catching **10 of 22 jobs**, including one applied to that morning with six of
eight emails already out. A band that covers half the table and means *decaying* is worse than
no band at all, and the reading was not even wrong so much as absent: Visa on day zero and
Webai with every email and every follow-up spent thirteen days earlier printed the same word.

What was missing is **runway** — how much of the plan is left. That is what tells a sequence
still running from one that is finished, and it is the difference between "wait" and "this one
is over". So the bands now answer *is anything still in motion*, and they run from an interview
backwards. Crucially this does NOT re-open §Lessons 35: finishing the plan moves a job DOWN
(`active` → `cooling`), so more messages with no answer still never reads better than fewer.

Bands rather than a percentage. The inputs are a handful of booleans and a clock; a number out
of 100 implies a precision that is not there, and invites tuning the constant instead of
answering the email. Every band carries the sentence that produced it — an unexplained colour
is ignored within a week, which is the §Lessons 43 failure applied to information rather than
controls.
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

#: Silence after a SPENT sequence. Cooling is "we finished and heard nothing"; cold is that,
#: for long enough that it is over.
_COLD_DAYS = 14

#: A plan with work left in it is only `active` while somebody is actually working it. Past
#: this, unsent outreach is not runway — it is an abandoned job, and calling that active is the
#: same lie in the other direction.
_STALE_DAYS = 10

#: How much of the email plan must be spent before "still running" stops being the honest
#: reading. Below this a job has real road left; above it, the plan is nearly over and nobody
#: has answered, which is what `cooling` is for.
#:
#: Two thirds rather than "is anything left at all", because *anything left* puts a job emailed
#: this morning and one with a single trailing touch after a fortnight of silence in the same
#: band — which is the §Lessons 54 failure (Visa and Webai printing the same word) rebuilt from
#: the other side. Checked against the whole board before it was chosen; see the commit.
_MOSTLY_SPENT = 2 / 3

#: LinkedIn is deliberately outside the runway maths, and this is the §Lessons 35 line drawn
#: once more. `dm_status` is 'sent' | 'manual', both meaning WE sent one; no `accepted` state
#: exists anywhere in the schema, so an uninvited contact is not a pending conversation.
#: Counting them also makes the band useless in practice — measured live, LinkedIn steps sit at
#: 3/16, 5/10, 3/6, so every job on the board would have runway forever and nothing could read
#: as spent. What it may NOT do is license the sentence "everything planned here is spent" on a
#: row whose own Next button says an invite is outstanding, so the copy says EMAILS throughout.
_EMAIL = "email"

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


def _last_effort(contacts: list[dict], ladders: dict | None) -> str:
    """When WE last did something here. Distinguishes a plan in progress from an abandoned one.

    Not `applied_at`: applying is one act at the start, and a job applied to yesterday whose
    outreach nobody has touched since is exactly the case this has to catch.
    """
    ladders = ladders or {}
    best = ""
    for contact in contacts or []:
        stamps = [contact.get("submitted_at") or "", contact.get("sms_sent_at") or "",
                  contact.get("dm_sent_at") or ""]
        for channel in ("email", "linkedin", "sms"):
            stamps.append((ladders.get((contact.get("id"), channel)) or {}).get("last_sent_at") or "")
        for s in stamps:
            if s.strip() and parse_ts(s) and (not best or parse_ts(s) > parse_ts(best)):
                best = s
    return best


def _plan_progress(contacts: list[dict], ladders: dict | None,
                   plan: dict | None) -> tuple[int, int]:
    """(sent, planned) email messages across this job — the cold email plus its whole ladder.

    **Runway is what is SCHEDULED, not what is owed right now**, and reading the wrong one of
    those is what put a job applied to seven minutes earlier in the `cooling` band.

    The previous version asked the checklist. But the checklist's follow-up step is built as
    `done / (done + due)` and its own comment says so — "reads 100% until a follow-up actually
    comes due" — which is right for a checklist, because you have not failed to do something
    that is not owed yet. Read as RUNWAY it inverts: a ladder scheduled for 48h from now
    reports zero work remaining, indistinguishable from a ladder that has finished. So five
    emails sent this morning, with fifteen follow-ups queued behind them, read as a spent plan.
    §Lessons 21 — a value one layer computes with a meaning the other layer cannot see.

    Measured on the live board when this was written: of the ten jobs reading `cooling`, six
    were 38% through their plan or less, and Expedia at 25% was reading "spent" while Saronic
    at 32% — further along — read "active". The bands were inverted against the very thing they
    claimed to measure.

    Counting rules, each of which was a wrong answer first:
      * only contacts with an ADDRESS are in the email plan; the rest are not runway.
      * a TERMINAL sequence (they replied, or it was stopped) has no remaining touches, so that
        contact's plan is whatever actually happened — otherwise stopping a ladder would make
        a job look like it had further to go.
      * LinkedIn is excluded, see `_EMAIL` above.
    """
    per = int((plan or {}).get("total_touches") or 0)
    ladders = ladders or {}
    sent = planned = 0
    for contact in contacts or []:
        if not (contact.get("email") or "").strip():
            continue
        ladder = ladders.get((contact.get("id"), _EMAIL)) or {}
        count = int(ladder.get("count") or 0)
        started = bool((contact.get("submitted_at") or "").strip())
        done_here = (1 if started else 0) + count
        sent += done_here
        # A terminal ladder is finished by definition, whatever the schedule still lists.
        planned += done_here if (ladder.get("sequence_status") or "").strip() else 1 + per
    return sent, planned


def _spent(plan: dict | None) -> str:
    """What the finished plan consisted of — "5 emails, 5 follow-ups"."""
    parts = []
    for step in (plan or {}).get("steps") or []:
        done = int(step.get("done") or 0)
        if not done:
            continue
        if step.get("key") == "emailed":
            parts.append(f"{done} email{'s' if done != 1 else ''}")
        elif step.get("key") == "followup":
            parts.append(f"{done} follow-up{'s' if done != 1 else ''}")
    return ", ".join(parts)


def temperature(job: dict, contacts: list[dict] | None = None, ladders: dict | None = None,
                plan: dict | None = None, now: datetime | None = None) -> dict:
    """`{band, label, icon, reason}` — how this application is doing, and why.

    The bands run from an interview backwards, and the question each one answers is **is
    anything still in motion**:

        won         an interview is scheduled                       (terminal)
        warm        they did something, recently
        active      something is moving — either they engaged a while back, or the
                    outreach plan is still running and being worked
        cooling     nothing is moving: the plan is spent, or stalled, or their last
                    move is old
        cold        spent, and silent long enough that it is over
        new         nothing sent yet
        undeliverable   mail is bouncing — not cold, and the opposite fix

    §Lessons 35 still holds and is what makes this safe: **only their actions can reach
    `warm`.** Effort cannot buy the top band, and FINISHING the plan moves a job DOWN
    (`active` → `cooling`), so more messages with no answer never reads better than fewer.
    What `plan` adds is the runway — a job with six of eight emails still to send has
    somewhere to go, and one that has spent every email and every follow-up does not.

    `reason` is not decoration. A band with no explanation is a colour, and a colour nobody
    can interrogate stops being read.
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

    # Nobody has engaged. What separates these jobs is how much of the plan could still produce
    # a reply, and whether anyone is still working it.
    sent, planned = _plan_progress(contacts, ladders, plan)
    left = max(0, planned - sent)
    quiet = _days_since(_last_effort(contacts, ladders), now)
    idle = int(quiet) if quiet is not None else 0

    if left and idle >= _STALE_DAYS:
        # Runway with nobody using it. Calling this active would be the same lie as calling a
        # fresh job cooling, pointing the other way.
        return _band(COOLING, f"{left} message{'s' if left != 1 else ''} still scheduled, but "
                              f"nothing has been sent in {_ago(idle)}.")
    if left and sent < planned * _MOSTLY_SPENT:
        moved = "sent today" if idle < 1 else f"last sent {_when(idle)}"
        return _band(ACTIVE, f"{sent} of {planned} planned emails sent, {left} still to go — "
                             f"{moved}.")
    if left:
        # Nearly through the plan with nothing back. There is road left, but not much, and
        # saying "still running" here is how Webai and a job applied to this morning ended up
        # printing the same word. Naming the remainder keeps it actionable rather than final.
        return _band(COOLING, f"{sent} of {planned} planned emails sent and nobody has "
                              f"answered. Only {left} left.")

    # Both of these say what was spent AND when the last message went, because those are two
    # different facts and only naming one misleads. A job applied to a fortnight ago whose last
    # follow-up went yesterday is not "no answer in 15 days" — it is a sequence that has just
    # finished, and the difference is whether there is anything left to wait for.
    #
    # "Every email" and not "everything": the LinkedIn ladder is outside this maths by design,
    # and the old wording contradicted the same row's own Next button reading "1 LinkedIn
    # invite left" — one row, two facts, §Lessons 56.
    spent_desc = _spent(plan) or f"{effort} message{'s' if effort != 1 else ''}"
    if idle >= _COLD_DAYS:
        return _band(COLD, f"Every email planned here is spent ({spent_desc}) and nothing has "
                           f"been sent or answered in {_ago(idle)}.")
    return _band(COOLING, f"Every email planned here is spent ({spent_desc}). Last message "
                          f"{_when(idle)}, no answer.")


def _ago(days: int) -> str:
    """A DURATION: "today" / "1 day" / "9 days". Reads after "in ..."."""
    return "today" if days < 1 else ("1 day" if days == 1 else f"{days} days")


def _when(days: int) -> str:
    """A POINT: "today" / "yesterday" / "9 days ago". Reads after "last message ...".

    Two helpers rather than one because "today ago" and "in yesterday" are both what a single
    one produces, and this string sits on the row where it is actually read.
    """
    return "today" if days < 1 else ("yesterday" if days == 1 else f"{days} days ago")


def _band(band: str, reason: str) -> dict:
    return {"band": band, "label": LABEL[band], "icon": ICON[band], "reason": reason}

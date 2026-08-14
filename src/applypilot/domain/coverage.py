"""How far a job has actually been WORKED — per person, per channel, in order.

Asked for as three things that turn out to be one:

  * *"the full sequence should be 3 emails, text + call, text + call again to close the loop"*
  * *"every card as soon as you open it should have its own summary tab... a quick summary
    outlining all interactions, emails sent, texts calls etc."*
  * *"I should not close out a job application without having texted and called some of the
    people whose contacts we found"*

All three need the same answer: **what has actually gone out to each person, and what is still
owed.** So there is one computation and three readers — the summary panel, the close guard, and
the per-person next action.

## Why a STAGE and not four independent ladders

Today each channel runs its own ladder, anchored on its own first message, unaware of the
others. That is right for the mechanism and wrong for the question the operator is asking. A
person can currently show "email follow-up 2 due", "text follow-up 1 due" and now "call due" at
once, in no order — three things owed and nothing saying which comes first.

An ordered plan says one thing at a time:

    Emails                    →  Text + call        →  Text + call again
    the email ladder             first text and        one more of each,
    (cold + its follow-ups)      first call            72h later

The stages are DERIVED from the channel ladders, never stored. A stored stage would need
recomputing every time a touch is sent, a reply lands, or a schedule changes, and the copy on
disk would be wrong in between — §Lessons 21, which is also why `exhausted` and the temperature
band are computed at render time.

**A reply ends everything.** Not "advances the stage" — ends it. Someone who answered is not
owed a chasing phone call, and that is the whole failure CRM-3a exists to prevent: a counter
that is mostly work you have decided not to do is one you stop reading.

## What "3 emails" was taken to mean

The request says three; the shipping ladder is a cold email plus three follow-ups, which is
four. This module does NOT change that, deliberately: `FOLLOWUP_SCHEDULE` is what decides it,
191 follow-ups have already been sent under the current cadence, and shortening the schedule
would silently mark ladders `finished` that have a touch still pending — retiring real work with
nothing raising. The email STAGE here is "whatever the email ladder is", and tightening it to
exactly three emails is one setting: `FOLLOWUP_SCHEDULE=48,96`.
"""

from __future__ import annotations

from applypilot.domain.followup import (CALL, CHANNELS, EMAIL, EMPTY_LADDER, SMS,
                                        channel_schedule, normalize_for_ladder, touch_state)

#: The ordered plan. Each stage names the channels that make it up; a stage is DONE when every
#: channel in it that CAN run has nothing left owed, and is only reachable once the one before
#: it is done. Data, not branches — the same shape as the channel registry, for the same reason.
STAGES: tuple[dict, ...] = (
    {"key": "email", "label": "Emails", "channels": ("email",)},
    {"key": "reach", "label": "Text + call", "channels": ("sms", "call")},
    {"key": "close", "label": "Text + call again", "channels": ("sms", "call")},
)

#: Terminal ladder states — nothing more is owed on that channel, for whatever reason.
_SPENT = ("finished", "stopped", "replied", "")


def _ladder(ladders: dict, cid: str, channel: str) -> dict:
    return (ladders or {}).get((cid, channel)) or EMPTY_LADDER


def contact_coverage(contact: dict, ladders: dict | None = None, now=None, space=None) -> dict:
    """One person: what has gone out on every channel, and what the next thing is.

    `sent` counts REAL messages, which is the first one plus its touches — not the touches
    alone. The first email lives on `contacts`, the first text and the first call live in their
    own anchor columns, and everything after each of them is a `touches` row. Counting only
    `touches` would report zero for somebody who has been emailed once, which is the majority
    of the live database.
    """
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)
    c = normalize_for_ladder(contact)
    cid = c.get("id") or ""
    replied = bool((c.get("replied_at") or "").strip())

    per: dict[str, dict] = {}
    for ch in CHANNELS:
        lad = _ladder(ladders, cid, ch.name)
        state, hours = touch_state(c, ch, channel_schedule(ch, space), now, lad)
        # The anchor is what proves a FIRST message went out on this channel; the ladder's
        # count is everything after it. Email's anchor is `emailed` (a message id from Gmail),
        # which is why it is read through the derived field rather than off a column.
        started = bool(c.get("emailed")) if ch is EMAIL \
            else bool((c.get(ch.start_field) or "").strip())
        per[ch.name] = {
            "channel": ch.name, "label": (ch.label or "email ").strip(),
            "started": started,
            "sent": (1 if started else 0) + (lad.get("count") or 0),
            "planned": (1 + len(channel_schedule(ch, space))) if ch.follows_up else 1,
            "state": state, "due": state == "due", "due_in_h": hours,
            "last_at": lad.get("last_sent_at") or (c.get(ch.start_field) or ""),
            "possible": _possible(c, ch),
        }

    stage, nxt = _stage(per, replied, _silent_for(c, now))
    return {"id": cid, "full_name": c.get("full_name") or "", "email": c.get("email") or "",
            "phone": c.get("phone") or "", "replied": replied,
            "channels": per, "stage": stage, "next": nxt,
            "emails": per["email"]["sent"], "texts": per["sms"]["sent"],
            "calls": per["call"]["sent"], "invites": per["linkedin"]["sent"]}


def _possible(contact: dict, channel) -> bool:
    """Could this channel EVER run for this person? A missing address or number is not a gap in
    the work — it is a channel that does not exist here, and counting it as unspent would make
    every job look unworked forever (the shape §Lessons 98 caught in the sheet import)."""
    if channel is EMAIL:
        return bool((contact.get("email") or "").strip())
    if channel in (SMS, CALL):
        return bool((contact.get("phone") or "").strip())
    return bool((contact.get("linkedin_url") or "").strip())


def _silent_for(contact: dict, now) -> float | None:
    """Hours since the FIRST email, when nobody has answered. None if we never wrote.

    Anchored on the first email rather than the last touch, because the question the operator
    asked is "have we heard back in the first week or so" — a week from when this started, not a
    week from whichever nudge happened to go most recently. Anchoring on the last touch would
    make the escalation slide further away every time a follow-up fired, so a person being
    chased diligently would be the last one you ever picked up the phone to.
    """
    from applypilot.domain.timeutil import hours_since
    anchor = (contact.get("submitted_at") or "").strip()
    return hours_since(anchor, now) if anchor else None


def reach_after_hours() -> int:
    """How long silence has to last before the phone opens. Default 168h — a week.

    Read through the settings registry like every other schedule (ARCH-6), so a bad value fails
    at startup naming the variable instead of quietly becoming zero and putting every contact on
    a call list (§Lessons 50: `0` has meant "unlimited" in two settings and "send nothing" in a
    third).
    """
    from applypilot import settings
    values, _ = settings.resolve()
    got = values.get("REACH_AFTER_HOURS")
    return int(got) if got else 168


def _stage(per: dict, replied: bool, silent_h: float | None = None) -> tuple[dict, dict | None]:
    """Which stage this person is at, and the single next thing to do.

    A reply ENDS the plan rather than advancing it. Chasing somebody who answered is the one
    follow-up guaranteed to cost something, and it is the same rule that halts every sequence
    when an interview is marked.
    """
    if replied:
        return {"key": "replied", "label": "They replied", "index": len(STAGES)}, None

    # Stage 1 is the email ladder. It is "done" when nothing more is owed on email — which
    # includes the case where email was never possible, so somebody with only a phone number
    # starts at Text + call rather than waiting forever on a stage that cannot run.
    #
    # ...OR when a week of silence has passed, whichever comes FIRST. Waiting for the ladder to
    # be spent means day 13 on the shipping schedule (48h, then 96h, then 168h), and the live
    # data says the third email is not what earns the reply: 40% of replies come from the cold
    # email, 40% from follow-up 1, 20% from follow-up 2, and **0 from follow-up 3** — which had
    # been sent 4 times in total when this was written.
    #
    # The email ladder is NOT cut short by this, and that is the whole point of it being an OR.
    # This module only DESCRIBES; sending is driven by each channel's own ladder in
    # `followup.py`, so follow-up 3 still goes out on schedule as the backstop. What changes is
    # which action the operator is pointed at on day 7 — and the two channels overlapping from
    # there is ordinary multichannel practice, not a conflict.
    email = per["email"]
    overdue = silent_h is not None and silent_h >= reach_after_hours()
    # Did the phone open because a week passed, or because the emails ran out? Different
    # sentences, and the operator needs the first one to be able to disagree with it.
    _by_time = overdue and email["state"] not in _SPENT
    if email["possible"] and email["state"] not in _SPENT and not overdue:
        nxt = {"channel": "email", "what": "follow up by email"} if email["due"] else None
        return {**STAGES[0], "index": 0}, nxt
    if email["possible"] and not email["started"]:
        return {**STAGES[0], "index": 0}, {"channel": "email", "what": "send the first email"}

    # Stage 2 and 3 are the same two channels; which one you are in is how far each has got.
    sms, call = per["sms"], per["call"]
    reachable = sms["possible"] or call["possible"]
    if not reachable:
        # The plan is genuinely stuck — but `next` must point at something DOABLE. Live, 168
        # contacts land here (a week past their first email, no reply, no number), and if an
        # email follow-up is due for one of them, hiding it behind a phone number they do not
        # have offers an action that cannot be taken while suppressing one that can. §Lessons 43
        # in its worst form: not a control nobody can find, a control nobody can use.
        #
        # So the STAGE stays honest about being blocked, and `next` prefers the email.
        if email["due"]:
            return {"key": "blocked", "label": "No phone number", "index": 1}, \
                   {"channel": "email", "what": "follow up by email — no number to call"}
        return {"key": "blocked", "label": "No phone number", "index": 1}, \
               {"channel": "phone", "what": "add a phone number to text or call"}

    if not sms["started"] and not call["started"]:
        # Say WHY the phone opened. "a week with no answer" is a fact the operator can act on
        # and disagree with; "text and call them" appearing from nowhere is an instruction.
        why = "no answer in a week — text and call them" if _by_time else "text and call them"
        return {**STAGES[1], "index": 1}, {"channel": "sms", "what": why}
    # First contact made on at least one of them: finish the pair, then wait for the second.
    if sms["possible"] and not sms["started"]:
        return {**STAGES[1], "index": 1}, {"channel": "sms", "what": "text them"}
    if call["possible"] and not call["started"]:
        return {**STAGES[1], "index": 1}, {"channel": "call", "what": "call them"}

    if sms["due"] or call["due"]:
        which = "text" if sms["due"] else "call"
        return {**STAGES[2], "index": 2}, {"channel": "sms" if sms["due"] else "call",
                                           "what": f"{which} them again"}
    if sms["state"] in _SPENT and call["state"] in _SPENT:
        return {"key": "done", "label": "Fully worked", "index": len(STAGES)}, None
    return {**STAGES[2], "index": 2}, None


def job_coverage(contacts: list[dict], ladders: dict | None = None, now=None,
                 space=None) -> dict:
    """The whole job: totals, and what is left unspent.

    `unworked` is what the close guard reads. It counts only people the channel is POSSIBLE for
    — somebody with no phone number is not an untexted person, they are a person who cannot be
    texted, and folding the two together would put a permanent warning on every job.
    """
    rows = [contact_coverage(c, ladders, now, space) for c in (contacts or [])]
    reach = [r for r in rows if r["channels"]["sms"]["possible"]
             or r["channels"]["call"]["possible"]]
    return {
        "people": len(rows),
        "rows": rows,
        "emails": sum(r["emails"] for r in rows),
        "texts": sum(r["texts"] for r in rows),
        "calls": sum(r["calls"] for r in rows),
        "invites": sum(r["invites"] for r in rows),
        "replied": sum(1 for r in rows if r["replied"]),
        "due": sum(1 for r in rows if r["next"]),
        "with_phone": len(reach),
        "unworked": {
            # Named per channel, because "you have not finished working this" is not actionable
            # and "4 of 6 people were never texted" is.
            "texted": [r["full_name"] for r in reach if not r["channels"]["sms"]["started"]],
            "called": [r["full_name"] for r in reach if not r["channels"]["call"]["started"]],
            "no_phone": [r["full_name"] for r in rows
                         if not (r["channels"]["sms"]["possible"]
                                 or r["channels"]["call"]["possible"])],
        },
    }


def close_warning(cov: dict) -> dict:
    """What to say when the operator closes a job they have not finished working.

    ADVISORY, never a refusal. A role that genuinely died — a pulled req, a freeze, a listing
    that was never real — has to be filable without first faking work nobody did, and the row
    does not know what the operator knows. §Lessons 69: reserve `disabled` for what is
    genuinely impossible; everything else is a sentence.

    Silent when somebody replied: the point of the outreach was a conversation, and one
    happened.
    """
    if not cov.get("people") or cov.get("replied"):
        return {"warn": False, "lines": []}
    un = cov.get("unworked") or {}
    n = cov.get("with_phone") or 0
    lines = []
    if n and len(un.get("texted") or []):
        lines.append(f"{len(un['texted'])} of {n} never texted")
    if n and len(un.get("called") or []):
        lines.append(f"{len(un['called'])} of {n} never called")
    # The no-number case, measured before it was written: only 8 of 373 contacts carry a phone,
    # so on the live database this is 24 of 36 jobs. It is TRUE every time — nobody was texted
    # because there was no number to text — but a sentence that names no fix is one the operator
    # trains themselves past within a week, which is the failure CRM-3a's badge exists to avoid.
    #
    # So it names the step instead. Apollo will not release a direct dial to a local tool
    # (verified three ways, §Lessons 4), which is precisely why the numbers have to be copied
    # out of Apollo's own UI by hand — and the card already carries an "Apollo ↗" link that
    # opens the page they are on.
    if not n and cov["people"]:
        lines.append(f"no phone number for any of the {cov['people']} people here — "
                     "Apollo shows direct dials in its own UI, copy one in from the ↗ link")
    return {"warn": bool(lines), "lines": lines,
            "names": (un.get("texted") or un.get("called") or [])[:6]}

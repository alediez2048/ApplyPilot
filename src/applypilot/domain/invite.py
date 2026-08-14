"""CAL-1 — what a calendar invite IS, before anything touches Google.

Pure: dicts in, dicts out. The API call lives in `networking/calendar_send.py`; everything that
can be decided without the network is decided here, because an invite is the most outward-facing
thing this app does — it puts an entry on a stranger's calendar and mails them about it, and
unlike an email there is no draft state to review afterwards.

## Why this is not the `.ics` that was already built and reverted

`3480c37` sent a real `.ics` with `METHOD:REQUEST` as a mail attachment and was reverted in
`82eb429`, with the operator's reason: *"the whole point of the cal.com link is to skip calendar
invites."* That was right for what it was. The distinction that makes this different is worth
stating because it is the entire justification:

    cal.com     for a time that has NOT been agreed — they pick from your availability
    an invite   for a time that HAS been agreed, usually named by them in the thread

Sending a booking link back to somebody who just wrote "how about Tuesday at 2?" is the wrong
reply. Both belong.

The `.ics` approach also cannot do what was asked for: a Google Meet link needs `conferenceData`
on `events.insert`, which needs the OAuth scope the `.ics` route existed to avoid.

## The trap that shapes the whole design

`sendUpdates=all` means **GOOGLE** mails the invitation, not us. So it bypasses `gmail_send`
entirely and the daily limit, the per-company cap and the address cooldown never see it — a send
path that is not counted, which is exactly §Lessons 77's shape (`sent_today()` counted first
contacts only while gating three doors). The guards are therefore checked HERE, before the API
call, rather than being inherited from a send path this does not go through.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

#: Meeting lengths offered. Anything longer than an hour is not a first conversation.
DURATIONS = (15, 30, 45, 60)
DEFAULT_DURATION = 30

#: How far ahead an invite may be scheduled. A year out is a typo, not a meeting.
MAX_DAYS_AHEAD = 365

#: IANA, never a UTC offset. Omitted, Google uses the calendar's default — which is not
#: necessarily the operator's — and a meeting an hour out is worse than no meeting. An offset
#: like "-05:00" would be correct today and wrong after a DST change.
DEFAULT_TZ = "America/Chicago"

_TZ_RE = re.compile(r"^[A-Za-z]+/[A-Za-z_+\-]+$")


def default_title(sender: str, them: str, company: str = "") -> str:
    """`Alejandro × Dana` — the shape a human types, not "Meeting Request"."""
    a = (sender or "").strip().split(" ")[0]
    b = (them or "").strip().split(" ")[0]
    if a and b:
        return f"{a} × {b}"
    return f"Intro call{f' — {company}' if company else ''}"


def parse_when(day: str, time: str, tz: str = "") -> tuple[str, str]:
    """(`YYYY-MM-DDTHH:MM:SS`, tz) from the two form fields. Raises ValueError with the reason.

    Returned WITHOUT an offset and paired with an IANA zone, which is how Google wants a local
    time: `dateTime` plus `timeZone`. Building an offset ourselves would bake in today's DST.
    """
    day, time, tz = (day or "").strip(), (time or "").strip(), (tz or "").strip() or DEFAULT_TZ
    if not day:
        raise ValueError("pick a day")
    if not time:
        raise ValueError("pick a time")
    if not _TZ_RE.match(tz):
        raise ValueError(f"{tz!r} is not an IANA time zone (e.g. America/Chicago)")
    try:
        dt = datetime.strptime(f"{day} {time}", "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError("that date and time did not parse") from None
    return dt.strftime("%Y-%m-%dT%H:%M:%S"), tz


def validate(*, title: str, day: str, time: str, duration: int, attendees: list[str],
             tz: str = "", now: datetime | None = None) -> dict:
    """Everything checkable without the network. Returns {ok, error, start, end, tz, ...}.

    Refuses rather than corrects. An invite is not a draft — the recipient sees it the moment
    it is created, so "we fixed up your input and sent it" is not an outcome anyone can review.
    """
    title = (title or "").strip()
    if not title:
        return {"ok": False, "error": "the invite needs a title"}
    if len(title) > 200:
        return {"ok": False, "error": "that title is too long for a calendar entry"}

    clean = [a.strip() for a in (attendees or []) if (a or "").strip()]
    if not clean:
        return {"ok": False, "error": "nobody to invite — this contact has no email address"}

    try:
        start, tzid = parse_when(day, time, tz)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    if duration not in DURATIONS:
        return {"ok": False, "error": f"pick a length: {', '.join(str(d) for d in DURATIONS)} min"}

    # A meeting in the past is almost always a mistyped year, and it is not recoverable: the
    # invitation goes out the instant the event is created.
    naive = datetime.strptime(start, "%Y-%m-%dT%H:%M:%S")
    ref = (now or datetime.now()).replace(tzinfo=None)
    if naive < ref - timedelta(minutes=5):
        return {"ok": False, "error": "that time is in the past"}
    if naive > ref + timedelta(days=MAX_DAYS_AHEAD):
        return {"ok": False, "error": f"that is more than {MAX_DAYS_AHEAD} days out — check the year"}

    end = (naive + timedelta(minutes=duration)).strftime("%Y-%m-%dT%H:%M:%S")
    return {"ok": True, "error": "", "title": title, "start": start, "end": end, "tz": tzid,
            "attendees": clean, "duration": duration}


def event_body(*, title: str, start: str, end: str, tz: str, attendees: list[str],
               agenda: str = "", meet: bool = True, request_id: str = "") -> dict:
    """The `events.insert` payload. Built here so it can be asserted without a network call.

    `conferenceData` is what produces a Google Meet link, and it needs `conferenceDataVersion=1`
    on the request — the caller's job, and the single easiest thing to omit: without it the field
    is accepted and silently ignored, so the event is created, the invitation goes out, and there
    is no Meet link on it. `requestId` must be stable per event or Google may mint a second
    conference on a retry.
    """
    body: dict = {
        "summary": title,
        "start": {"dateTime": start, "timeZone": tz},
        "end": {"dateTime": end, "timeZone": tz},
        "attendees": [{"email": a} for a in attendees],
        # The organiser's own calendar is implied by the credential, so `guestsCanModify` is the
        # only invitee permission worth setting — off, because this is an invitation to a time
        # already agreed, not a document to negotiate in.
        "guestsCanModify": False,
        "reminders": {"useDefault": True},
    }
    if (agenda or "").strip():
        body["description"] = agenda.strip()
    if meet:
        body["conferenceData"] = {
            "createRequest": {
                "requestId": request_id or f"applypilot-{abs(hash(start + title)) % 10**12}",
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
    return body


def describe(v: dict, meet: bool) -> str:
    """One line naming person, local time and Meet — for the confirm.

    §Lessons 29: this is outward-facing and lands on a stranger's calendar, so the operator has
    to be shown what it does before they can meaningfully click. The TIME is the part that must
    be echoed back — a mistyped hour is the failure mode, and it is unrecoverable.
    """
    when = datetime.strptime(v["start"], "%Y-%m-%dT%H:%M:%S")
    return (f"{v['title']} — {when.strftime('%a %d %b, %-I:%M %p')} "
            f"({v['duration']} min, {v['tz']})"
            f"{' · Google Meet' if meet else ''}\n\nTo: {', '.join(v['attendees'])}")

"""A calendar invitation as text. Pure — no SQL, no HTTP, no mailbox.

**Why an `.ics` attachment rather than the Google Calendar API.** The token on this machine holds
`gmail.send`, `gmail.metadata`, `gmail.readonly` and `gmail.settings.basic`. Creating an event
through Google would need `calendar.events`, which is a new consent screen, a new scope on a
token the apply agent is deliberately kept away from, and one more thing to revoke. A
`text/calendar; method=REQUEST` part costs none of that and is what every mail client already
speaks: Gmail, Outlook and Apple Mail all render RSVP buttons for it. The same reasoning as
pasting a spreadsheet instead of integrating Sheets.

**The consequence, stated rather than discovered:** because we send an invitation instead of
creating an event, it does NOT appear on the sender's own calendar. `organiser_link()` exists to
close that, and the UI has to offer it — an invite the sender forgets to accept is a meeting only
one side is holding.

Two parts of the format are easy to skip and fail quietly:

**Lines fold at 75 octets.** RFC 5545 folds with CRLF followed by one space. A long SUMMARY sent
unfolded is accepted by Gmail and dropped by stricter parsers, so the failure looks like "it
works" until the one recipient whose client is strict never sees it.

**TEXT values escape `\\`, `;`, `,` and newlines.** A company called "Acme, Inc." puts a comma in
a SUMMARY, and an unescaped comma is a VALUE SEPARATOR — the property silently truncates.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

#: Bounds on a meeting, not a policy. Under five minutes is a mistyped duration; over eight hours
#: is a mistyped end time. Both are refused so the recipient never gets an obviously wrong invite.
MIN_MINUTES = 5
MAX_MINUTES = 8 * 60
DEFAULT_MINUTES = 30

#: How far ahead an invite may be scheduled. A year out is a typo in the YEAR field, which is the
#: single most likely date mistake and the one nobody notices in a picker.
MAX_DAYS_AHEAD = 365


class InviteError(ValueError):
    """The invitation cannot be built — a bad time, a bad duration, a missing address."""


def _escape(value: str) -> str:
    """Escape a TEXT value. Order matters: backslash FIRST, or the escapes get re-escaped."""
    out = (value or "").replace("\\", "\\\\")
    out = out.replace(";", "\\;").replace(",", "\\,")
    return out.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def _fold(line: str) -> str:
    """Fold one content line to 75 OCTETS, continuation lines starting with a space.

    Counted in octets rather than characters, because a name with an accent in it is two bytes
    and a 75-character line can be 80 bytes. Folding must also never split a multi-byte
    character, so the split point walks back to a character boundary.
    """
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    pieces: list[str] = []
    first = True
    while raw:
        limit = 75 if first else 74          # a continuation spends one octet on its leading space
        cut = min(limit, len(raw))
        # Never cut a UTF-8 sequence in half: continuation bytes are 0b10xxxxxx, so walk the cut
        # back until the next byte starts a character. A split one produces mojibake in a
        # recipient's calendar, which is the kind of thing that only shows up on one name.
        while cut > 1 and cut < len(raw) and raw[cut] & 0xC0 == 0x80:
            cut -= 1
        pieces.append(raw[:cut].decode("utf-8"))
        raw = raw[cut:]
        first = False
    return pieces[0] + "".join("\r\n " + p for p in pieces[1:])


def _stamp(dt: datetime) -> str:
    """A UTC timestamp in iCalendar's basic format.

    Everything is emitted in UTC (`...Z`) on purpose: a local time needs an accompanying
    VTIMEZONE block with its own DST rules, and a wrong or missing one moves the meeting by an
    hour in the recipient's calendar without anything failing.
    """
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def uid_for(contact_id: str, organiser_email: str) -> str:
    """A stable UID for this sender-and-recipient pair.

    Stable per CONTACT rather than per time, so re-sending after a change is an UPDATE to the
    event already in their calendar rather than a second one beside it. That is the whole reason
    `sequence` exists as a parameter: same UID with a higher SEQUENCE is how iCalendar says "this
    replaces what I sent you".
    """
    seed = f"{contact_id}|{(organiser_email or '').lower()}"
    return f"{hashlib.sha256(seed.encode()).hexdigest()[:32]}@applypilot"


def when(start: datetime, minutes: int, now: datetime | None = None) -> datetime:
    """Validate a proposed start and duration; returns the START, unchanged, or raises.

    Refusing the past is not pedantry — a picker defaulting to today plus a time already gone is
    the ordinary way to send an invitation to a meeting that has finished.
    """
    if start.tzinfo is None:
        raise InviteError("the start time has no timezone")
    now = now or datetime.now(timezone.utc)
    if start <= now:
        raise InviteError("that time has already passed")
    if start > now + timedelta(days=MAX_DAYS_AHEAD):
        raise InviteError(f"that is more than {MAX_DAYS_AHEAD} days away — check the year")
    if not MIN_MINUTES <= minutes <= MAX_MINUTES:
        raise InviteError(f"the meeting must be between {MIN_MINUTES} and {MAX_MINUTES} minutes")
    return start


def build_ics(*, uid: str, start: datetime, minutes: int, summary: str,
              organiser_name: str, organiser_email: str,
              attendee_name: str, attendee_email: str,
              description: str = "", location: str = "",
              sequence: int = 0, now: datetime | None = None) -> str:
    """One VEVENT, as a METHOD:REQUEST calendar object. CRLF line endings, as the format requires.

    `sequence` must increase every time the same UID is re-sent, or the recipient's client is
    entitled to ignore the update as a duplicate it already has.
    """
    if not attendee_email or "@" not in attendee_email:
        raise InviteError("no email address to invite")
    start = when(start, minutes, now)
    end = start + timedelta(minutes=minutes)
    stamp = _stamp(now or datetime.now(timezone.utc))

    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//ApplyPilot//EN",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        # REQUEST, not PUBLISH: PUBLISH is an announcement and renders without RSVP buttons,
        # which is the whole reason for sending this rather than a line of text with a time in it.
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{_stamp(start)}",
        f"DTEND:{_stamp(end)}",
        f"SEQUENCE:{int(sequence)}",
        "STATUS:CONFIRMED",
        f"SUMMARY:{_escape(summary)}",
        f'ORGANIZER;CN="{_escape(organiser_name or organiser_email)}":mailto:{organiser_email}',
        # RSVP=TRUE is what asks the client for a yes/no; NEEDS-ACTION is what keeps it asking.
        f'ATTENDEE;CN="{_escape(attendee_name or attendee_email)}";ROLE=REQ-PARTICIPANT;'
        f"PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{attendee_email}",
    ]
    if location:
        lines.append(f"LOCATION:{_escape(location)}")
    if description:
        lines.append(f"DESCRIPTION:{_escape(description)}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def organiser_link(*, start: datetime, minutes: int, summary: str,
                   attendee_email: str, description: str = "", location: str = "") -> str:
    """A Google Calendar "add this" URL for the SENDER's own calendar.

    Needed because we send an invitation rather than creating an event: without this the meeting
    exists in the recipient's calendar and nowhere in the sender's, which is the failure mode
    that turns a working feature into a missed call.
    """
    end = start + timedelta(minutes=minutes)
    params = [
        ("action", "TEMPLATE"),
        ("text", summary),
        ("dates", f"{_stamp(start)}/{_stamp(end)}"),
        ("details", description),
        ("location", location),
        ("add", attendee_email),
    ]
    query = "&".join(f"{k}={quote(v, safe='')}" for k, v in params if v)
    return f"https://calendar.google.com/calendar/render?{query}"


_WS = re.compile(r"\s+")


def sender_name(full_name: str, preferred_name: str = "") -> str:
    """The name the RECIPIENT already knows, for ORGANIZER and the title.

    Caught by generating one against the live profile: `full_name` is "Jorge Alejandro Diez" and
    `preferred_name` is "Alejandro", so taking the first word of the full name put **"Intro call:
    Jorge and Dana Okafor"** in front of somebody whose every previous email was signed Alejandro.
    A calendar entry is read weeks later with no context around it; a name they do not recognise
    is a meeting they do not place.

    The preferred name REPLACES the first given name and keeps the surname, so "Jorge Alejandro
    Diez" becomes "Alejandro Diez" rather than the bare "Alejandro".
    """
    full = _WS.sub(" ", (full_name or "").strip())
    pref = _WS.sub(" ", (preferred_name or "").strip())
    if not pref:
        return full
    if not full:
        return pref
    parts = full.split(" ")
    if parts[0].lower() == pref.lower():
        return full
    # Keep everything from the surname on, dropping only the given name it replaces. A preferred
    # name already inside the full name (the common case) must not be duplicated.
    rest = [p for p in parts[1:] if p.lower() != pref.lower()]
    return " ".join([pref] + rest[-1:]) if rest else pref


def default_summary(organiser_name: str, attendee_name: str) -> str:
    """"Intro call: Alejandro and Dana Okafor" — both names, because the recipient sees this line
    in their calendar weeks later with no other context around it."""
    a = _WS.sub(" ", (organiser_name or "").strip()).split(" ")[0]
    b = _WS.sub(" ", (attendee_name or "").strip())
    if a and b:
        return f"Intro call: {a} and {b}"
    return f"Intro call with {b or a}" if (a or b) else "Intro call"

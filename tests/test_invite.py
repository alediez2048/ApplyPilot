"""A calendar invitation, built as text and sent as a mail attachment.

Two properties of iCalendar decide most of this file, and both fail QUIETLY when skipped — the
invite still arrives, still renders in Gmail, and is wrong or dropped somewhere else:

**Lines fold at 75 octets**, and an unfolded one is accepted by lenient parsers and rejected by
strict ones. **TEXT values escape `\\`, `;`, `,` and newlines**, and an unescaped comma is a
VALUE SEPARATOR, so "Acme, Inc." silently truncates the property it sits in.

The third is not a format rule: we send an INVITATION rather than creating an event, so the
meeting lands in the recipient's calendar and nowhere in the sender's unless they add it.
`organiser_link` is that, and a test holds it, because it is the difference between a working
feature and a missed call.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from applypilot.domain import invite

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
SOON = NOW + timedelta(days=3)


def ics(**over):
    args = dict(uid="u1@applypilot", start=SOON, minutes=30, summary="Intro call",
                organiser_name="Alejandro Diez", organiser_email="me@example.test",
                attendee_name="Dana Okafor", attendee_email="dana@ridgeline.test", now=NOW)
    args.update(over)
    return invite.build_ics(**args)


def unfold(text: str) -> str:
    """Undo the 75-octet folding, the way a real parser does, so assertions read the VALUE."""
    return text.replace("\r\n ", "")


def props(text: str) -> dict[str, str]:
    out = {}
    for line in unfold(text).split("\r\n"):
        if ":" in line:
            key = line.split(":", 1)[0].split(";", 1)[0]
            out.setdefault(key, line.split(":", 1)[1])
    return out


# ── the format ──────────────────────────────────────────────────────────────

def test_it_is_a_request_so_the_client_shows_rsvp_buttons():
    """PUBLISH renders as an announcement with no yes/no — which would make this a worse version
    of writing the time in a sentence."""
    body = ics()
    assert "METHOD:REQUEST" in body
    assert "RSVP=TRUE" in unfold(body)
    assert "PARTSTAT=NEEDS-ACTION" in unfold(body)


def test_every_line_fits_in_75_octets():
    """Counted in OCTETS, not characters: a name with an accent is two bytes, so a 75-character
    line can be 80 and a character-based fold passes this while emitting invalid output."""
    body = ics(summary="A rather long conversation about applied AI engineering roles in Austin",
               attendee_name="Chée Chew-Ångström de la Fuente")
    for line in body.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75, f"unfolded line: {line!r}"


def test_folding_never_splits_a_character():
    """A cut inside a UTF-8 sequence is mojibake in somebody's calendar, and it shows up on one
    name rather than on all of them."""
    body = ics(summary="Zürich " + "é" * 80)
    assert body.encode("utf-8").decode("utf-8")          # round-trips at all
    assert "é" * 5 in unfold(body)


def test_a_comma_in_a_name_does_not_truncate_the_property():
    """An unescaped comma is a VALUE SEPARATOR. "Acme, Inc." is the ordinary case."""
    body = ics(summary="Intro call: Acme, Inc.")
    assert "Acme\\, Inc." in unfold(body)
    assert props(body)["SUMMARY"] == "Intro call: Acme\\, Inc."


def test_semicolons_backslashes_and_newlines_are_escaped():
    body = ics(description="One; two\\three\nfour")
    assert props(body)["DESCRIPTION"] == "One\\; two\\\\three\\nfour"


def test_backslashes_are_escaped_first():
    r"""Order matters: escaping `,` before `\` turns `\` + `,` into `\\,` re-escaped into
    something else. A literal backslash in a note is rare and silent when wrong."""
    assert props(ics(description="a\\,b"))["DESCRIPTION"] == "a\\\\\\,b"


def test_times_are_utc_so_no_vtimezone_is_needed():
    """A local DTSTART needs a VTIMEZONE block with its own DST rules; a wrong or missing one
    moves the meeting by an hour and nothing fails."""
    p = props(ics())
    assert p["DTSTART"] == "20260815T120000Z"
    assert p["DTEND"] == "20260815T123000Z"           # 30 minutes later
    assert p["DTSTAMP"].endswith("Z")


def test_the_end_follows_the_duration():
    assert props(ics(minutes=45))["DTEND"] == "20260815T124500Z"


# ── the guards ──────────────────────────────────────────────────────────────

def test_a_time_that_has_passed_is_refused():
    """A picker defaulting to today plus a time already gone is the ordinary way to invite
    somebody to a meeting that has finished."""
    with pytest.raises(invite.InviteError, match="already passed"):
        ics(start=NOW - timedelta(minutes=1))


def test_a_year_typo_is_refused_rather_than_sent():
    """The year is the field nobody re-reads in a date picker."""
    with pytest.raises(invite.InviteError, match="check the year"):
        ics(start=NOW + timedelta(days=400))


@pytest.mark.parametrize("minutes", [0, 1, 4, 8 * 60 + 1, 10_000])
def test_an_implausible_duration_is_refused(minutes):
    with pytest.raises(invite.InviteError):
        ics(minutes=minutes)


def test_a_naive_start_is_refused_rather_than_assumed_to_be_utc():
    """Guessing the zone is how a 10am meeting arrives at 5am."""
    with pytest.raises(invite.InviteError, match="timezone"):
        ics(start=SOON.replace(tzinfo=None))


def test_no_address_is_refused():
    with pytest.raises(invite.InviteError, match="no email"):
        ics(attendee_email="")
    with pytest.raises(invite.InviteError):
        ics(attendee_email="not-an-address")


# ── re-sending moves the meeting, it does not add a second one ──────────────

def test_the_uid_is_stable_for_a_contact_so_a_resend_is_an_update():
    """Same UID plus a higher SEQUENCE is how iCalendar says "this replaces what I sent you". A
    time-derived UID would leave the original meeting sitting in their calendar."""
    a = invite.uid_for("c1", "me@example.test")
    assert a == invite.uid_for("c1", "ME@Example.test"), "the address case changed the UID"
    assert a != invite.uid_for("c2", "me@example.test")
    assert props(ics(uid=a, start=SOON))["UID"] == props(ics(uid=a, start=SOON + timedelta(1)))["UID"]


def test_the_sequence_is_carried_into_the_event():
    """Without an increasing SEQUENCE the recipient's client may ignore the update as a
    duplicate it already holds — so the meeting silently does not move."""
    assert props(ics(sequence=0))["SEQUENCE"] == "0"
    assert props(ics(sequence=3))["SEQUENCE"] == "3"


# ── the sender's own calendar ───────────────────────────────────────────────

def test_there_is_a_link_to_add_it_to_the_senders_own_calendar():
    """We send an invitation rather than creating an event, so without this the meeting exists
    in their calendar and nowhere in the sender's."""
    url = invite.organiser_link(start=SOON, minutes=30, summary="Intro call: A and B",
                                attendee_email="dana@ridgeline.test", description="hello")
    assert url.startswith("https://calendar.google.com/calendar/render?")
    assert "20260815T120000Z%2F20260815T123000Z" in url, "the time range is not in the link"
    assert "dana%40ridgeline.test" in url
    assert " " not in url, "an unencoded space breaks the link"


def test_the_organiser_link_encodes_characters_that_would_break_it():
    """An `&` inside a title is a PARAMETER SEPARATOR once it is in a query string, so an
    unencoded one truncates the title and invents a parameter after it."""
    from urllib.parse import parse_qs, urlparse
    url = invite.organiser_link(start=SOON, minutes=30, summary="A & B: notes, part 2",
                                attendee_email="x@y.test")
    q = parse_qs(urlparse(url).query)
    # Parsed back, the title must be intact — this is the assertion the substring version could
    # not make, and `x.count("&") == x.count("&")` is true whatever the code does (§Lessons 71).
    assert q["text"] == ["A & B: notes, part 2"]
    assert set(q) == {"action", "text", "dates", "add"}, f"unexpected parameters: {sorted(q)}"


# ── the default title ───────────────────────────────────────────────────────

def test_the_default_title_names_both_people():
    """They read this line in their calendar a fortnight later with nothing else around it."""
    assert invite.default_summary("Alejandro Diez", "Dana Okafor") == "Intro call: Alejandro and Dana Okafor"


def test_the_default_title_survives_a_missing_name():
    assert invite.default_summary("", "Dana Okafor") == "Intro call with Dana Okafor"
    assert invite.default_summary("Alejandro", "") == "Intro call with Alejandro"
    assert invite.default_summary("", "") == "Intro call"


# ── the name the recipient recognises ───────────────────────────────────────

@pytest.mark.parametrize("full,pref,want", [
    # The live profile, and the bug this exists for: every email is signed Alejandro, so an
    # invitation from "Jorge" is a meeting the recipient cannot place.
    ("Jorge Alejandro Diez", "Alejandro", "Alejandro Diez"),
    # A preferred name that IS the first given name changes nothing.
    ("Dana Okafor", "Dana", "Dana Okafor"),
    # A nickname unrelated to the full name still keeps the surname.
    ("Robert Smith", "Bob", "Bob Smith"),
    ("Robert Smith", "", "Robert Smith"),
    ("", "Bob", "Bob"),
    ("", "", ""),
    ("Cher", "Cher", "Cher"),
    ("Madonna", "", "Madonna"),
])
def test_the_sender_name_is_the_one_they_have_seen(full, pref, want):
    assert invite.sender_name(full, pref) == want


def test_the_preferred_name_is_never_duplicated():
    """"Jorge Alejandro Diez" + "Alejandro" must not become "Alejandro Alejandro Diez"."""
    out = invite.sender_name("Jorge Alejandro Diez", "Alejandro")
    assert out.lower().count("alejandro") == 1


def test_the_title_uses_that_name():
    assert invite.default_summary(invite.sender_name("Jorge Alejandro Diez", "Alejandro"),
                                  "Dana Okafor") == "Intro call: Alejandro and Dana Okafor"

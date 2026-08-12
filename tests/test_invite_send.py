"""Sending the invitation: the mail part, the guards, and what gets recorded.

The format itself is `tests/test_invite.py`. This is the half that touches the mailbox, so every
test here runs `dry_run` or stops before the transport — nothing in the suite may send.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import pytest

import applypilot.web_dashboard as wd
from applypilot.domain import interactions as ix_domain
from applypilot.domain import invite as inv
from applypilot.networking import gmail_send

SOON = datetime.now(timezone.utc) + timedelta(days=3)


# ── the mail part ───────────────────────────────────────────────────────────

def test_the_ics_rides_as_a_calendar_part_not_an_anonymous_file():
    """`method=REQUEST` on the Content-Type is what makes a client show RSVP buttons. The same
    bytes without it are an attachment nobody opens."""
    msg = EmailMessage()
    msg.set_content("body")
    gmail_send.attach_ics(msg, "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
    parts = [p for p in msg.walk() if p.get_content_type() == "text/calendar"]
    assert len(parts) == 1, "no text/calendar part was attached"
    assert parts[0].get_param("method") == "REQUEST"
    assert parts[0].get_filename() == "invite.ics"


def test_the_calendar_part_survives_the_transport_intact():
    """The format mandates CRLF, and a transport that rewrites line endings corrupts every
    folded line — which is most of them on a real invite."""
    body = inv.build_ics(uid="u@x", start=SOON, minutes=30, summary="Intro call",
                         organiser_name="A", organiser_email="a@x.test",
                         attendee_name="B", attendee_email="b@y.test")
    msg = EmailMessage()
    msg.set_content("body")
    gmail_send.attach_ics(msg, body)
    part = next(p for p in msg.walk() if p.get_content_type() == "text/calendar")
    assert part.get_payload(decode=True).decode("utf-8") == body


def test_an_empty_ics_attaches_nothing():
    """Every other send path calls the same builder, so a falsy value must be a no-op rather
    than an empty attachment on ordinary outreach."""
    for value in (None, "", "   "):
        msg = EmailMessage()
        msg.set_content("body")
        gmail_send.attach_ics(msg, value)
        assert not [p for p in msg.walk() if p.get_content_type() == "text/calendar"]


# ── the guards ──────────────────────────────────────────────────────────────

@pytest.fixture
def contact(tmp_path, monkeypatch):
    import applypilot.database as database
    from applypilot.networking import store
    db = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", db)
    database.close_connection(db)
    database.init_db(db)
    conn = database.get_connection(db)
    store.init_contacts(conn)
    store.upsert_contact({"id": "c1", "job_url": "target:sheet-search:apex",
                          "full_name": "Dana Okafor", "company": "Apex",
                          "email": "dana@apex.test", "source": "import"})
    store.upsert_contact({"id": "c2", "job_url": "target:sheet-search:apex",
                          "full_name": "No Address", "company": "Apex", "source": "import"})
    monkeypatch.setenv("OUTREACH_FROM_NAME", "Alejandro")
    monkeypatch.setattr(gmail_send, "_from_address", lambda: "me@example.test")
    return conn


def test_a_contact_with_no_address_cannot_be_invited(contact):
    """The tab is offered anyway, because that is where an address gets added — but the send
    itself has to refuse rather than build an invitation addressed to nobody."""
    out = gmail_send.send_invite("c2", SOON, 30, dry_run=True, conn=contact)
    assert out["ok"] is False
    assert "no email" in out["message"]


def test_an_unknown_contact_is_refused(contact):
    assert gmail_send.send_invite("nope", SOON, 30, dry_run=True, conn=contact)["ok"] is False


def test_a_bad_time_is_refused_before_anything_is_sent(contact):
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    out = gmail_send.send_invite("c1", past, 30, dry_run=True, conn=contact)
    assert out["ok"] is False and "passed" in out["message"]


def test_the_dry_run_builds_a_real_invitation(contact):
    out = gmail_send.send_invite("c1", SOON, 30, dry_run=True, conn=contact)
    assert out["ok"] is True
    assert "METHOD:REQUEST" in out["ics"]
    assert "mailto:dana@apex.test" in out["ics"].replace("\r\n ", "")


def test_the_title_defaults_to_both_names(contact):
    out = gmail_send.send_invite("c1", SOON, 30, dry_run=True, conn=contact)
    assert "Intro call: Alejandro and Dana Okafor" in out["ics"].replace("\r\n ", "")


def test_a_daily_limit_that_is_reached_stops_the_invite(contact, monkeypatch):
    """It consumes the mailbox's real quota like any other send, so it is bounded by the same
    number. The per-company cap deliberately is NOT applied — see `send_invite`."""
    monkeypatch.setattr(gmail_send, "_DAILY_LIMIT", 1)
    monkeypatch.setattr(gmail_send.store, "sent_today", lambda *a, **k: 5)
    out = gmail_send.send_invite("c1", SOON, 30, conn=contact)
    assert out["ok"] is False and "daily send limit" in out["message"]


# ── re-inviting moves the meeting ───────────────────────────────────────────

def test_the_sequence_counts_previous_invites_so_a_resend_is_an_update(contact):
    """Same UID with a HIGHER sequence is how iCalendar says "this replaces what I sent you".
    Left at 0, the recipient's client is entitled to ignore the change as a duplicate — so the
    meeting silently does not move, which looks exactly like a successful re-send.

    Counted from `interactions`, which costs no new column.
    """
    from applypilot.networking import interactions_store as ix

    def seq_of(out):
        return next(ln for ln in out["ics"].split("\r\n") if ln.startswith("SEQUENCE:"))

    assert seq_of(gmail_send.send_invite("c1", SOON, 30, dry_run=True, conn=contact)) == "SEQUENCE:0"
    ix.record("c1", gmail_send.INVITED_KIND, at="2026-08-01T10:00:00+00:00",
              job_url="target:sheet-search:apex", conn=contact)
    assert seq_of(gmail_send.send_invite("c1", SOON, 30, dry_run=True, conn=contact)) == "SEQUENCE:1"


def test_the_uid_does_not_move_between_sends(contact):
    a = gmail_send.send_invite("c1", SOON, 30, dry_run=True, conn=contact)["ics"]
    b = gmail_send.send_invite("c1", SOON + timedelta(days=1), 45, dry_run=True, conn=contact)["ics"]
    uid = lambda t: next(ln for ln in t.split("\r\n") if ln.startswith("UID:"))  # noqa: E731
    assert uid(a) == uid(b), "a changed time produced a second event instead of moving the first"


# ── what it counts as ───────────────────────────────────────────────────────

def test_an_invite_we_sent_is_not_engagement(contact):
    """§Lessons 35: `dm_status` counted our own LinkedIn invitation as engagement and three jobs
    read "3/3 engaged" before anyone had done anything. Proposing a time is our act; a detected
    cal.com BOOKED is theirs, and only that one carries weight."""
    assert gmail_send.INVITED_KIND == ix_domain.INVITED
    assert ix_domain.WEIGHT[ix_domain.INVITED] == 0
    assert ix_domain.INVITED not in ix_domain.ENGAGEMENT
    assert ix_domain.INVITED not in ix_domain.INBOUND
    assert ix_domain.WEIGHT[ix_domain.BOOKED] > 0, "a real booking must still count"


def test_it_renders_on_the_timeline_with_its_own_words(contact):
    """A kind with no LABEL renders blank, so the row appears and says nothing."""
    assert ix_domain.LABEL[ix_domain.INVITED]
    assert ix_domain.ICON[ix_domain.INVITED]
    assert ix_domain.LABEL[ix_domain.INVITED] != ix_domain.LABEL[ix_domain.BOOKED]


# ── the endpoint reads the BROWSER's timezone ───────────────────────────────

def test_the_time_is_interpreted_in_the_browsers_zone_not_the_servers():
    """The browser sends its own zone. Assuming the server's holds until the dashboard is opened
    from a laptop somewhere else, at which point every invitation is hours out and nothing
    fails."""
    chicago = wd._parse_local("2026-09-01", "10:00", "America/Chicago")
    london = wd._parse_local("2026-09-01", "10:00", "Europe/London")
    assert chicago.utcoffset() != london.utcoffset()
    assert chicago.astimezone(timezone.utc).hour == 15      # CDT is UTC-5 in September
    assert london.astimezone(timezone.utc).hour == 9        # BST is UTC+1


@pytest.mark.parametrize("date_s,time_s,tz", [
    ("", "10:00", "UTC"), ("2026-09-01", "", "UTC"),
    ("not-a-date", "10:00", "UTC"), ("2026-09-01", "25:00", "UTC"),
])
def test_an_unreadable_time_is_refused_with_a_sentence(date_s, time_s, tz):
    with pytest.raises(ValueError):
        wd._parse_local(date_s, time_s, tz)


def test_an_unknown_zone_is_refused_rather_than_silently_becoming_utc():
    """Falling back to UTC moves the meeting by up to a day and reports success."""
    with pytest.raises(ValueError, match="timezone"):
        wd._parse_local("2026-09-01", "10:00", "Mars/Olympus_Mons")


def test_the_endpoint_needs_a_contact():
    assert wd._send_invite({"date": "2026-09-01", "time": "10:00"})["ok"] is False

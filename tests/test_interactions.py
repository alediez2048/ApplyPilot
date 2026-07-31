"""The Interactions tab — what each person has actually DONE.

The pieces already existed and each lived somewhere else: sends on `contacts`, replies in
`messages`, deck clicks in three columns. Answering "has anyone engaged with this application?"
meant opening four panels and holding the result in your head.

What is detectable here was established by looking at the real mailbox, not by guessing:

  * a booked call — cal.com emails the host on every booking (`hello@cal.com`, "30 Min Meeting
    between …"), so it is a Gmail search, not an integration;
  * a deck open — a first-party beacon on our own site;
  * a LinkedIn profile view — **not detectable**. It is absent from the LinkedIn data export
    (checked: no such file in a Basic export) and generates no notification email (checked:
    zero such threads). It is operator-logged, and `source` keeps that visible.
"""

from __future__ import annotations

import applypilot.database as database
import pytest

from applypilot.domain import interactions as ix
from applypilot.networking import interactions_store, store


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    interactions_store.init_interactions(conn)
    return conn


# ── the derived timeline ─────────────────────────────────────────────────────────────────

def test_derived_facts_are_not_copied_into_the_table():
    """Sends, replies and deck clicks are DERIVED from the contact at render time.

    Copying them into `interactions` would create a second copy that drifts from the first —
    which is precisely what made `tick` report 0 follow-ups due while the dashboard showed 3
    (§Lessons 21). Only events with nowhere else to live are stored.
    """
    c = {"submitted_at": "2026-07-20T10:00:00+00:00",
         "replied_at": "2026-07-22T10:00:00+00:00",
         "deck_viewed_at": "2026-07-21T10:00:00+00:00", "deck_views": 1}
    kinds = [r["kind"] for r in ix.for_contact(c, stored=[])]
    assert set(kinds) == {ix.SENT, ix.REPLIED, ix.DECK}, "derived rows are missing"


def test_the_timeline_is_newest_first():
    c = {"submitted_at": "2026-07-20T10:00:00+00:00",
         "replied_at": "2026-07-22T10:00:00+00:00"}
    rows = ix.for_contact(c)
    assert rows[0]["kind"] == ix.REPLIED and rows[-1]["kind"] == ix.SENT


def test_repeat_deck_views_show_the_first_AND_the_latest():
    """The first view is the event worth acting on; reporting only the latest would move the
    date every time they looked again and lose when they first cared."""
    c = {"deck_viewed_at": "2026-07-01T10:00:00+00:00",
         "deck_last_at": "2026-07-20T10:00:00+00:00", "deck_views": 4}
    rows = [r for r in ix.for_contact(c) if r["kind"] == ix.DECK]
    assert len(rows) == 2
    assert any("first of 4" in r["detail"] for r in rows)
    assert any("most recent" in r["detail"] for r in rows)


def test_a_single_deck_view_is_not_reported_twice():
    c = {"deck_viewed_at": "2026-07-01T10:00:00+00:00",
         "deck_last_at": "2026-07-01T10:00:00+00:00", "deck_views": 1}
    assert len([r for r in ix.for_contact(c) if r["kind"] == ix.DECK]) == 1


# ── what counts as engagement ────────────────────────────────────────────────────────────

def test_a_linkedin_invite_WE_sent_is_not_engagement():
    """The mistake that made the first version of this tab worthless.

    `dm_status` is 'sent' or 'manual' — both mean WE sent an invite. There is no 'accepted'
    state anywhere in the schema, so nothing knows whether they ever responded. Counting it
    had three live jobs reading "3/3 engaged" and "5/5 engaged" before anyone had done a thing.
    """
    rows = ix.for_contact({"dm_sent_at": "2026-07-30T10:00:00+00:00", "dm_status": "manual"})
    assert [r["kind"] for r in rows] == [ix.CONNECTED], "the invite should still be shown"
    assert ix.summarise(rows)["engaged"] is False, "an invite we sent counted as engagement"
    assert ix.CONNECTED not in ix.ENGAGEMENT
    # And the label says whose action it was, so the row cannot be misread.
    assert ix.LABEL[ix.CONNECTED].startswith("You ")


def test_our_own_email_is_not_engagement():
    """Otherwise every contact looks engaged the moment they are emailed, and the tab answers
    its own question with 'yes' before anyone has done anything."""
    rows = ix.for_contact({"submitted_at": "2026-07-20T10:00:00+00:00"})
    assert ix.summarise(rows) == {"engaged": False, "top": "", "label": "", "icon": "",
                                  "count": 0}


def test_the_strongest_signal_is_the_one_reported():
    """A booking outranks a reply outranks a deck view — time, then words, then attention."""
    c = {"replied_at": "2026-07-22T10:00:00+00:00",
         "deck_viewed_at": "2026-07-21T10:00:00+00:00", "deck_views": 1}
    booked = [{"kind": ix.BOOKED, "at": "2026-07-23T10:00:00+00:00", "source": "detected"}]
    assert ix.summarise(ix.for_contact(c, booked))["top"] == ix.BOOKED
    assert ix.summarise(ix.for_contact(c))["top"] == ix.REPLIED
    assert ix.summarise(ix.for_contact({"deck_viewed_at": "2026-07-01T00:00:00+00:00",
                                        "deck_views": 1}))["top"] == ix.DECK


def test_people_with_no_engagement_are_still_listed():
    """A tab that lists only the people who did something cannot answer "has anyone?" — which
    is the question it exists to answer."""
    out = ix.for_job([{"id": "a", "full_name": "Quiet"},
                      {"id": "b", "full_name": "Replied",
                       "replied_at": "2026-07-22T10:00:00+00:00"}])
    assert len(out["people"]) == 2
    assert out["engaged"] == 1
    assert out["people"][0]["full_name"] == "Replied", "engaged people come first"
    assert out["people"][1]["engaged"] is False


# ── storage ──────────────────────────────────────────────────────────────────────────────

def test_recording_the_same_booking_twice_is_not_two_bookings(db):
    """The id is a hash of (contact, kind, when), so an hourly tick re-reading the same mailbox
    upserts instead of duplicating — the eleven BOUNCED lines lesson (§Lessons 22)."""
    at = "2026-07-30T15:00:00+00:00"
    assert interactions_store.record("c1", ix.BOOKED, at=at, job_url="http://j/1", conn=db) is True
    assert interactions_store.record("c1", ix.BOOKED, at=at, job_url="http://j/1", conn=db) is False
    assert db.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == 1


def test_a_manual_note_stays_labelled_manual(db):
    """`source` is the whole reason the LinkedIn case is honest: an operator note must never
    read as something the system detected."""
    interactions_store.record("c1", ix.PROFILE_VIEW, at="2026-07-30T10:00:00+00:00",
                              source="manual", job_url="http://j/1", conn=db)
    rows = interactions_store.for_job("http://j/1", db)["c1"]
    assert rows[0]["source"] == "manual"
    assert ix.for_contact({}, rows)[0]["source"] == "manual"


def test_interactions_are_removed_with_the_contact(db):
    """Contact ids are a hash of (job, identity), so a re-discovered person reproduces the id
    and would inherit a stranger's history."""
    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "X", "email": "x@y.com"}, db)
    interactions_store.record(cid, ix.PROFILE_VIEW, source="manual", job_url="http://j/1", conn=db)
    assert db.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == 1
    store.delete_contact(cid, db)
    assert db.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == 0


def test_the_job_query_does_not_scale_per_contact(db):
    """/api/status renders every job on a 2.5s refresh under a 50-statement budget."""
    for i in range(8):
        interactions_store.record(f"c{i}", ix.BOOKED, at=f"2026-07-0{i+1}T10:00:00+00:00",
                                  job_url="http://j/1", conn=db)
    n = {"i": 0}
    db.set_trace_callback(lambda _s: n.__setitem__("i", n["i"] + 1))
    try:
        out = interactions_store.for_job("http://j/1", db)
    finally:
        db.set_trace_callback(None)
    assert len(out) == 8
    assert n["i"] <= 2, f"{n['i']} statements for 8 contacts — this is per-row"


# ── booking detection ────────────────────────────────────────────────────────────────────

def test_a_reminder_is_not_a_second_booking(monkeypatch):
    """cal.com sends "Reminder: 30 Min Meeting …" for the SAME booking, and it arrives closer
    to the meeting — so counting it would report one call as two AND make the reminder the
    latest engagement."""
    from applypilot.networking import bookings, gmail_read

    msgs = [
        {"id": "m1", "from": "hello@cal.com", "to": "me@x.com, gina@co.com", "cc": "",
         "subject": "30 Min Meeting between Gina and Alejandro", "internalDate": "1780000000000"},
        {"id": "m2", "from": "hello@cal.com", "to": "me@x.com, gina@co.com", "cc": "",
         "subject": "Reminder: 30 Min Meeting between Gina and Alejandro",
         "internalDate": "1780090000000"},
    ]
    monkeypatch.setattr(gmail_read, "can_read_content", lambda: (True, "ok"))
    monkeypatch.setattr(gmail_read, "search_threads", lambda *a, **k: ["t1"])
    monkeypatch.setattr(gmail_read, "thread_messages", lambda tid, service=None: msgs)

    hits = bookings.find_for_contacts([{"id": "c1", "email": "gina@co.com",
                                        "job_url": "http://j/1"}])
    assert len(hits) == 1, f"expected one booking, got {[h['detail'] for h in hits]}"


def test_a_scheduler_lookalike_domain_is_not_a_scheduler(monkeypatch):
    """§Lessons 1 — never substring-match a host. "cal.com" as a substring matches
    "notcal.com" and "mycal.company"."""
    from applypilot.networking import bookings

    assert bookings._is_scheduler("hello@cal.com") is True
    assert bookings._is_scheduler("x@mail.cal.com") is True
    assert bookings._is_scheduler("x@notcal.com") is False
    assert bookings._is_scheduler("x@calendly.com.evil.net") is False


def test_a_booking_is_only_attributed_to_someone_actually_on_it(monkeypatch):
    from applypilot.networking import bookings, gmail_read

    msgs = [{"id": "m1", "from": "hello@cal.com", "to": "me@x.com, someone@else.com", "cc": "",
             "subject": "30 Min Meeting between Someone and Alejandro",
             "internalDate": "1780000000000"}]
    monkeypatch.setattr(gmail_read, "can_read_content", lambda: (True, "ok"))
    monkeypatch.setattr(gmail_read, "search_threads", lambda *a, **k: ["t1"])
    monkeypatch.setattr(gmail_read, "thread_messages", lambda tid, service=None: msgs)

    assert bookings.find_for_contacts([{"id": "c1", "email": "gina@co.com"}]) == []


def test_booking_detection_is_off_without_the_content_scope(monkeypatch):
    from applypilot.networking import bookings, gmail_read
    monkeypatch.setattr(gmail_read, "can_read_content", lambda: (False, "off"))
    assert bookings.find_for_contacts([{"id": "c1", "email": "g@co.com"}]) == []
    assert bookings.poll()["ok"] is False

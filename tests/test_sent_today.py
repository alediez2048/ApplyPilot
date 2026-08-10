"""`sent_today()` counted first contacts only — under half of what it was limiting.

Found by the ID-1 design review, and it is a bug today, with no second identity involved.
`gmail_send` gates all three send doors on this number (outreach :115, follow-up :383, reply
:467), so two of the three kinds of email it exists to limit were invisible to it while still
consuming the real Gmail quota it exists to protect.

Measured on this machine before the fix: 102 first contacts, 107 follow-ups.

The interesting half is what is NOT counted. `messages` looks like the obvious third leg —
`send_reply` writes there and nowhere else — and it is a trap: `messages` mirrors the mailbox,
so 133 of its 253 outbound rows were first contacts already counted. Adding it doubles the
number instead of correcting it, and for a cap a phantom doubling blocks real sends.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import applypilot.database as database
from applypilot.networking import store
from applypilot.networking.touches import init_touches


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    init_touches(conn)
    conn.execute("INSERT INTO jobs (url, title, company, site, strategy) VALUES (?,?,?,?,?)",
                 ("http://j/1", "Role", "Acme", "Greenhouse", "dashboard_upload"))
    conn.commit()
    return conn


def _contact(db, cid: str, submitted_hours_ago: float | None):
    db.execute(
        "INSERT INTO contacts (id, job_url, full_name, email, outreach_status, submitted_at) "
        "VALUES (?,?,?,?,?,?)",
        (cid, "http://j/1", cid, f"{cid}@acme.test",
         "submitted" if submitted_hours_ago is not None else "drafted",
         _iso(submitted_hours_ago) if submitted_hours_ago is not None else ""))
    db.commit()


def _touch(db, cid: str, seq: int, sent_hours_ago: float | None, channel: str = "email"):
    db.execute(
        "INSERT INTO touches (id, contact_id, channel, seq, sent_at, status) VALUES (?,?,?,?,?,?)",
        (f"{cid}-{channel}-{seq}", cid, channel, seq,
         _iso(sent_hours_ago) if sent_hours_ago is not None else "", "sent"))
    db.commit()


# ── the bug ─────────────────────────────────────────────────────────────────

def test_follow_ups_are_counted(db):
    """The whole point. Before this, a day of nothing but follow-ups read as zero sent."""
    _contact(db, "c1", submitted_hours_ago=None)   # drafted, never sent
    _touch(db, "c1", 1, sent_hours_ago=2)
    _touch(db, "c1", 2, sent_hours_ago=3)
    assert store.sent_today(db) == 2


def test_first_contacts_are_still_counted(db):
    _contact(db, "c1", submitted_hours_ago=2)
    assert store.sent_today(db) == 1


def test_both_legs_add_up(db):
    _contact(db, "c1", submitted_hours_ago=2)
    _contact(db, "c2", submitted_hours_ago=5)
    _touch(db, "c1", 1, sent_hours_ago=1)
    assert store.sent_today(db) == 3


# ── the window ──────────────────────────────────────────────────────────────

def test_the_window_is_24h_on_both_legs(db):
    """A leg that ignores the cutoff turns a daily cap into a lifetime one — and it would read
    as the cap simply being reached, forever."""
    _contact(db, "old", submitted_hours_ago=30)
    _contact(db, "new", submitted_hours_ago=2)
    _touch(db, "new", 1, sent_hours_ago=30)
    _touch(db, "new", 2, sent_hours_ago=2)
    assert store.sent_today(db) == 2


def test_an_unsent_touch_is_not_a_send(db):
    """A touch row exists from the moment it comes due; `sent_at` is what makes it a send."""
    _contact(db, "c1", submitted_hours_ago=None)
    _touch(db, "c1", 1, sent_hours_ago=None)
    assert store.sent_today(db) == 0


# ── what must NOT be counted ────────────────────────────────────────────────

def test_only_email_touches_count(db):
    """LinkedIn and SMS do not touch the Gmail quota this cap protects. Counting them would
    throttle email sending because the operator pasted a text."""
    _contact(db, "c1", submitted_hours_ago=None)
    _touch(db, "c1", 1, sent_hours_ago=1, channel="linkedin")
    _touch(db, "c1", 1, sent_hours_ago=1, channel="sms")
    assert store.sent_today(db) == 0


def test_a_drafted_contact_is_not_a_send(db):
    _contact(db, "c1", submitted_hours_ago=None)
    assert store.sent_today(db) == 0


def test_a_claimed_or_failed_send_is_not_a_send(db):
    """`claim_for_send` stamps `submitted_at` BEFORE the send goes out, and a failure clears the
    status rather than the timestamp. So a row can carry a recent `submitted_at` and never have
    reached anybody — counting it charges the quota for an email that does not exist.

    This is what the status filter is FOR, and dropping it survived the first mutation pass:
    `test_a_drafted_contact_is_not_a_send` also leaves `submitted_at` empty, so it excluded the
    row through the wrong clause and proved nothing about the filter. §Lessons 71.
    """
    _contact(db, "c1", submitted_hours_ago=2)
    db.execute("UPDATE contacts SET outreach_status='failed' WHERE id='c1'")
    db.commit()
    assert store.sent_today(db) == 0


def test_synced_outbound_mail_is_not_counted_again(db):
    """The trap this fix stepped into and backed out of.

    `messages` mirrors the mailbox — `_sync_thread` stores both directions of every thread it
    reads — so an outbound row there is usually an email already counted. Measured live: 133 of
    253 outbound rows were first contacts already counted via `contacts.sent_message_id`.
    Counting `messages` as a third leg roughly doubles the number, and a cap that doubles
    blocks real sends.
    """
    from applypilot.networking.messages import init_messages, upsert_messages
    init_messages(db)
    _contact(db, "c1", submitted_hours_ago=2)
    db.execute("UPDATE contacts SET sent_message_id='m1' WHERE id='c1'")
    db.commit()
    upsert_messages([{
        "message_id": "m1", "contact_id": "c1", "thread_id": "t1", "direction": "out",
        "from_addr": "me@test", "subject": "Hello", "sent_at": _iso(2),
    }], conn=db)
    assert store.sent_today(db) == 1, "the same email was counted twice"

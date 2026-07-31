"""CRM-4a — whose turn it is, and why "they replied and you never answered" is the one to catch.

Every other signal in this system chases people who said **nothing**: follow-up ladders, touch
schedules, LinkedIn nudges. The one thing none of them notice is the opposite and much worse
case — somebody answered, and the reply is sitting there. The system paid Apollo credits and an
email to earn that reply, then dropped it.

It was live in the database when this was written: Gina Johnson at Salesforce replied on
2026-07-31 and nothing in the dashboard said so.

Structural only, and honestly so. On `gmail.metadata` we know a message arrived and who sent it,
never what it said — and "they replied and you have not answered" needs no body to be true.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import applypilot.database as database
from applypilot.domain import conversations as cv
from applypilot.networking import messages as msg_store, store

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _m(direction, at, frm="them@co.com", name="Gina Johnson"):
    return {"direction": direction, "from_addr": frm if direction == "in" else "me@x.com",
            "from_name": name if direction == "in" else "", "to_addrs": [], "cc_addrs": [],
            "subject": "Re: role", "sent_at": at.isoformat(), "rfc_message_id": "<x>"}


def test_a_reply_nobody_answered_is_awaiting_us():
    thread = [_m("out", NOW - timedelta(days=5)), _m("in", NOW - timedelta(days=2))]
    state = cv.conversation_state(thread, now=NOW)
    assert state["state"] == cv.AWAITING_US
    assert state["days"] == 2
    assert state["who"] == "Gina Johnson"


def test_once_we_answer_it_is_their_turn():
    thread = [_m("out", NOW - timedelta(days=5)), _m("in", NOW - timedelta(days=2)),
              _m("out", NOW - timedelta(days=1))]
    state = cv.conversation_state(thread, now=NOW)
    assert state["state"] == cv.AWAITING_THEM
    assert state["days"] == 1


def test_an_unanswered_cold_email_is_not_a_conversation():
    """Otherwise every unsent-reply cold email lands in the same bucket as a live thread, and
    the signal that matters drowns in the 30 that do not."""
    assert cv.conversation_state([_m("out", NOW - timedelta(days=9))], now=NOW) is None
    assert cv.conversation_state([], now=NOW) is None
    assert cv.conversation_state(None, now=NOW) is None


def test_the_newest_message_decides_not_the_count():
    """Three of ours and one of theirs is still our turn if theirs came last."""
    thread = [_m("out", NOW - timedelta(days=9)), _m("out", NOW - timedelta(days=6)),
              _m("out", NOW - timedelta(days=4)), _m("in", NOW - timedelta(hours=3))]
    state = cv.conversation_state(thread, now=NOW)
    assert state["state"] == cv.AWAITING_US
    assert state["days"] == 0 and state["hours"] == 3


def test_an_unparseable_timestamp_does_not_raise():
    """Older rows have no timezone and some have no date at all (§Lessons 6)."""
    thread = [_m("out", NOW - timedelta(days=2)),
              {"direction": "in", "from_addr": "a@b.com", "sent_at": "not a date"}]
    state = cv.conversation_state(thread, now=NOW)
    assert state["state"] == cv.AWAITING_US and state["days"] is None


def test_junk_entries_are_skipped_rather_than_crashing():
    assert cv.conversation_state(["nope", None], now=NOW) is None


# ── the dashboard rollup ─────────────────────────────────────────────────────────────────

def test_the_job_rollup_lists_who_is_waiting_longest_first():
    from applypilot import web_dashboard as wd

    contacts = [
        {"id": "a", "full_name": "Recent Reply", "conversation": {"state": "awaiting_us",
                                                                  "days": 1, "hours": 30}},
        {"id": "b", "full_name": "Old Reply", "conversation": {"state": "awaiting_us",
                                                               "days": 6, "hours": 150}},
        {"id": "c", "full_name": "We Answered", "conversation": {"state": "awaiting_them",
                                                                 "days": 2, "hours": 50}},
        {"id": "d", "full_name": "Never Replied", "conversation": None},
    ]
    out = wd._awaiting_us(contacts)
    assert [r["full_name"] for r in out] == ["Old Reply", "Recent Reply"], (
        "the longest-ignored reply must come first — it is the one most likely to be lost")


def test_the_rollup_survives_a_contact_with_no_conversation_key():
    from applypilot import web_dashboard as wd
    assert wd._awaiting_us([{"id": "a", "full_name": "X"}]) == []
    assert wd._awaiting_us([]) == []


# ── tick ─────────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    msg_store.init_messages(conn)
    return conn


@pytest.fixture(autouse=True)
def no_gmail(monkeypatch):
    from applypilot.networking import gmail_read
    monkeypatch.setattr(gmail_read, "available", lambda: (False, "not connected in tests"))


def _seed_thread(db, name, company, last_direction):
    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": name,
                                "email": f"{name.lower()}@co.com", "company": company,
                                "sent_message_id": "m1"}, db)
    rows = [
        {"message_id": f"{cid}-1", "thread_id": "t", "contact_id": cid, "job_url": "http://j/1",
         "direction": "out", "from_addr": "me@x.com", "to_addrs": [], "cc_addrs": [],
         "subject": "hi", "sent_at": "2026-07-20T10:00:00+00:00"},
        {"message_id": f"{cid}-2", "thread_id": "t", "contact_id": cid, "job_url": "http://j/1",
         "direction": "in", "from_addr": f"{name.lower()}@co.com", "from_name": name,
         "to_addrs": [], "cc_addrs": [], "subject": "Re: hi",
         "sent_at": "2026-07-22T10:00:00+00:00"},
    ]
    if last_direction == "out":
        rows.append({"message_id": f"{cid}-3", "thread_id": "t", "contact_id": cid,
                     "job_url": "http://j/1", "direction": "out", "from_addr": "me@x.com",
                     "to_addrs": [], "cc_addrs": [], "subject": "Re: hi",
                     "sent_at": "2026-07-23T10:00:00+00:00"})
    msg_store.upsert_messages(rows, db)
    return cid


def test_tick_reports_an_unanswered_reply(db):
    from applypilot import tick

    _seed_thread(db, "Gina", "Salesforce", last_direction="in")
    _seed_thread(db, "Victoria", "Writer", last_direction="out")

    out = tick.run(conn=db)["steps"]["unanswered"]
    assert out["awaiting_us"] == 1, "the unanswered reply was not reported"
    assert out["names"] == ["Gina"]
    assert "Gina" in out["detail"]


def test_tick_says_so_explicitly_when_nothing_is_waiting(db):
    """A zero must be as loud as a number (§Lessons 15) — 'no unanswered replies' and a step
    that never ran must not look the same in the summary."""
    from applypilot import tick

    _seed_thread(db, "Victoria", "Writer", last_direction="out")
    out = tick.run(conn=db)["steps"]["unanswered"]
    assert out["awaiting_us"] == 0
    assert "no unanswered replies" in out["detail"]


def test_tick_still_never_sends_anything(db, monkeypatch):
    """The new step reports; it must not have quietly become a sender."""
    from applypilot import tick
    from applypilot.networking import gmail_send

    _seed_thread(db, "Gina", "Salesforce", last_direction="in")
    for name in ("send_followup", "send_outreach", "send_reply", "send"):
        if hasattr(gmail_send, name):
            monkeypatch.setattr(gmail_send, name,
                                lambda *a, **k: pytest.fail(f"tick called {name}"))
    tick.run(conn=db)


def test_the_unanswered_step_does_not_query_once_per_contact(db):
    """The N+1 this codebase keeps re-learning. Hourly instead of every 2.5s is not a reason
    to write it the other way."""
    from applypilot import tick

    for i in range(3):
        _seed_thread(db, f"P{i}", "Acme", last_direction="in")
    few = _count(db, lambda: tick._step_unanswered(db, False))
    for i in range(3, 12):
        _seed_thread(db, f"P{i}", "Acme", last_direction="in")
    many = _count(db, lambda: tick._step_unanswered(db, False))
    assert many <= few + 1, f"{few} statements for 3 contacts, {many} for 12 — scaling per row"


def _count(conn, fn):
    n = {"i": 0}
    conn.set_trace_callback(lambda _sql: n.__setitem__("i", n["i"] + 1))
    try:
        fn()
    finally:
        conn.set_trace_callback(None)
    return n["i"]

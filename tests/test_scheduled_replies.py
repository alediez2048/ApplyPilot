"""Scheduling a REPLY — the conversation half of scheduled sending.

Follow-up scheduling lives in `test_scheduled_followups.py`. This file covers what is different
about a reply, which is everything that follows from it being an ANSWER rather than a chase:

  * the Cc the operator approved has to survive until the send
  * a newer inbound message cancels the promise — nothing like it applies to a follow-up
  * the text lives ONLY here, so cancelling has to hand it back
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from applypilot.networking import reply_queue

JS = Path(__file__).resolve().parents[1] / "src" / "applypilot" / "static" / "dashboard.js"


def _at(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


@pytest.fixture()
def db():
    """The ambient test database, with only this file's rows cleared — see the note in
    `test_scheduled_followups.py` for why this is not a per-test APPLYPILOT_DIR."""
    from applypilot.database import get_connection, init_db
    from applypilot.networking import messages, store
    init_db()
    conn = get_connection()
    store.init_contacts(conn)
    messages.init_messages(conn)
    reply_queue.init_reply_queue(conn)
    for sql in ("DELETE FROM reply_queue WHERE contact_id IN ('r1','r2')",
                "DELETE FROM messages WHERE contact_id IN ('r1','r2')",
                "DELETE FROM contacts WHERE id IN ('r1','r2')"):
        conn.execute(sql)
    conn.commit()
    store.upsert_contact({"id": "r1", "job_url": "j9", "full_name": "Patrick",
                          "email": "p@x.test", "sent_message_id": "m1"}, conn)
    return conn


def _inbound(conn, at: str, mid: str = "in1", thread: str = "tA"):
    from applypilot.networking import messages
    messages.upsert_messages([{
        "message_id": mid, "contact_id": "r1", "thread_id": thread,
        "subject": "quick q", "direction": "in", "from_addr": "p@x.test",
        "from_name": "Patrick", "sent_at": at, "snippet": "hello",
    }], conn)


# ── storage ─────────────────────────────────────────────────────────────────

def test_a_promise_needs_text(db):
    assert reply_queue.schedule("r1", "tA", "   ", _at(3), conn=db) is False
    assert reply_queue.pending_all(db) == {}


def test_scheduling_twice_on_one_thread_REPLACES(db):
    """One composer per conversation means one piece of text. Two queued answers to one thread is
    not something the UI can express and not something a recipient should receive."""
    reply_queue.schedule("r1", "tA", "first", _at(3), conn=db)
    reply_queue.schedule("r1", "tA", "second", _at(5), conn=db)
    rows = reply_queue.pending_all(db)["r1"]
    assert list(rows) == ["tA"]
    assert rows["tA"]["body"] == "second"


def test_two_threads_with_one_person_are_separate_promises(db):
    reply_queue.schedule("r1", "tA", "about the role", _at(3), conn=db)
    reply_queue.schedule("r1", "tB", "about the invoice", _at(4), conn=db)
    assert set(reply_queue.pending_all(db)["r1"]) == {"tA", "tB"}


def test_the_cc_is_STORED_not_re_derived(db):
    """`reply_target` recomputes recipients at send time, but the operator can DROP someone in
    the composer — and re-deriving would silently put them back days later."""
    reply_queue.schedule("r1", "tA", "body", _at(3), cc=["a@x.test"], conn=db)
    assert reply_queue.pending_all(db)["r1"]["tA"]["cc"] == ["a@x.test"]


def test_an_empty_cc_survives_as_an_empty_cc(db):
    """"the operator removed everyone" and "keep whatever the thread had" are different
    instructions, and only one of them is an empty list."""
    reply_queue.schedule("r1", "tA", "body", _at(3), cc=[], conn=db)
    assert reply_queue.pending_all(db)["r1"]["tA"]["cc"] == []


def test_a_promise_is_claimed_once(db):
    from applypilot.domain import sendtime
    now = datetime.now(timezone.utc)
    reply_queue.schedule("r1", "tA", "body", _at(-1), conn=db)
    first = reply_queue.claim_due(now.isoformat(), sendtime.lapsed_before(now, 24), conn=db)
    second = reply_queue.claim_due(now.isoformat(), sendtime.lapsed_before(now, 24), conn=db)
    assert len(first) == 1 and second == []


def test_a_lapsed_promise_is_neither_fired_nor_erased(db):
    from applypilot.domain import sendtime
    now = datetime.now(timezone.utc)
    reply_queue.schedule("r1", "tA", "body", _at(-48), conn=db)
    assert reply_queue.claim_due(now.isoformat(),
                                 sendtime.lapsed_before(now, 24), conn=db) == []
    assert reply_queue.pending_all(db)["r1"]["tA"]["body"] == "body"


def test_deleting_a_contact_removes_their_queued_reply(db):
    """A row left behind is not stale state — it is an email the poller would still send, to a
    person the operator deleted, on a conversation that no longer renders anywhere."""
    from applypilot.networking import store
    reply_queue.schedule("r1", "tA", "body", _at(3), conn=db)
    store.delete_contact("r1", db)
    assert reply_queue.pending_all(db).get("r1") is None


# ── staleness: the guard that exists only for replies ───────────────────────

def test_a_newer_inbound_message_makes_a_queued_reply_stale():
    row = {"last_inbound_at": "2026-08-10T10:00:00+00:00"}
    msgs = [{"direction": "in", "sent_at": "2026-08-12T09:00:00+00:00"}]
    assert "wrote again" in reply_queue.stale_against(row, msgs)


def test_the_same_inbound_message_is_not_stale():
    row = {"last_inbound_at": "2026-08-10T10:00:00+00:00"}
    msgs = [{"direction": "in", "sent_at": "2026-08-10T10:00:00+00:00"}]
    assert reply_queue.stale_against(row, msgs) == ""


def test_our_OWN_later_messages_do_not_make_a_reply_stale():
    """The send path appends one itself, so counting outbound would make every queued reply
    cancel itself."""
    row = {"last_inbound_at": "2026-08-10T10:00:00+00:00"}
    msgs = [{"direction": "in", "sent_at": "2026-08-10T10:00:00+00:00"},
            {"direction": "out", "sent_at": "2026-08-14T09:00:00+00:00"}]
    assert reply_queue.stale_against(row, msgs) == ""


def test_missing_data_on_either_side_is_not_evidence_of_staleness():
    """Refusing on absent data blocks the ordinary case rather than the dangerous one
    (§Lessons 34) — a promise made before this column existed still sends as written."""
    assert reply_queue.stale_against({"last_inbound_at": ""},
                                     [{"direction": "in", "sent_at": "2026-08-12T09:00"}]) == ""
    assert reply_queue.stale_against({"last_inbound_at": "2026-08-10T10:00"}, []) == ""


# ── firing ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def wired(db, monkeypatch):
    """Only the TRANSPORT is stubbed, so every guard inside `send_reply` still runs."""
    from applypilot.networking import gmail_oauth
    import applypilot.networking.gmail_send as gs
    sent: list[dict] = []
    monkeypatch.setattr(gs, "transport", lambda: "oauth")
    monkeypatch.setattr(gs, "_from_address", lambda: "me@x.test")
    monkeypatch.setattr(gs, "_our_addresses", lambda: ["me@x.test"])
    monkeypatch.setattr(gmail_oauth, "connected_email", lambda: "me@x.test")
    monkeypatch.setattr(gmail_oauth, "send",
                        lambda to, subj, body, *a, **k:
                        (sent.append({"to": to, "body": body}), {"id": "x", "threadId": "tA"})[1])
    return sent


def _poll():
    from applypilot.web_dashboard import ReplyPoller
    now = datetime.now(timezone.utc)
    return ReplyPoller._poll_scheduled_replies(now, 24)


def test_a_due_reply_is_sent(db, wired):
    _inbound(db, "2026-08-10T10:00:00+00:00")
    reply_queue.schedule("r1", "tA", "here is my answer", _at(-1),
                         last_inbound_at="2026-08-10T10:00:00+00:00", conn=db)
    res = _poll()
    assert res["replies_sent"] == 1, res
    assert wired and wired[0]["to"] == "p@x.test"


def test_a_reply_is_NOT_sent_when_they_wrote_again(db, wired):
    """The guard that only exists here. Sending would answer a message the operator has never
    read, in a conversation where the other person is now waiting on something else."""
    _inbound(db, "2026-08-10T10:00:00+00:00", mid="in1")
    reply_queue.schedule("r1", "tA", "answer", _at(-1),
                         last_inbound_at="2026-08-10T10:00:00+00:00", conn=db)
    _inbound(db, "2026-08-14T10:00:00+00:00", mid="in2")     # they wrote again
    res = _poll()
    assert res["replies_sent"] == 0 and wired == []
    assert any("wrote again" in w for w in res["replies_why"]), res


def test_a_stale_promise_is_consumed_rather_than_retried_forever(db, wired):
    _inbound(db, "2026-08-10T10:00:00+00:00", mid="in1")
    reply_queue.schedule("r1", "tA", "answer", _at(-1),
                         last_inbound_at="2026-08-10T10:00:00+00:00", conn=db)
    _inbound(db, "2026-08-14T10:00:00+00:00", mid="in2")
    _poll()
    assert reply_queue.pending_all(db).get("r1") is None
    assert _poll()["replies_sent"] == 0


def test_a_space_with_autosend_off_never_fires_a_queued_reply(db, wired, monkeypatch):
    from applypilot import web_dashboard as wd

    class _Space:
        name, can_autosend = "Lead Sheet", False
    monkeypatch.setattr(wd, "_space_of_contact", lambda *a, **k: _Space())
    _inbound(db, "2026-08-10T10:00:00+00:00")
    reply_queue.schedule("r1", "tA", "answer", _at(-1),
                         last_inbound_at="2026-08-10T10:00:00+00:00", conn=db)
    res = _poll()
    assert res["replies_sent"] == 0 and wired == []
    assert any("auto-send off" in w for w in res["replies_why"]), res


def test_the_stored_cc_is_what_actually_goes(db, wired, monkeypatch):
    """The one thing a re-derivation would get wrong days later."""
    import applypilot.networking.gmail_send as gs
    seen = {}
    real = gs.send_reply
    monkeypatch.setattr(gs, "send_reply",
                        lambda cid, body, **kw: (seen.update(kw), real(cid, body, **kw))[1])
    _inbound(db, "2026-08-10T10:00:00+00:00")
    reply_queue.schedule("r1", "tA", "answer", _at(-1), cc=["boss@x.test"],
                         last_inbound_at="2026-08-10T10:00:00+00:00", conn=db)
    _poll()
    assert seen.get("cc") == ["boss@x.test"], seen


# ── the endpoint ────────────────────────────────────────────────────────────

def test_scheduling_takes_the_SAME_endpoint_as_sending(db):
    """A second endpoint would have to re-derive the contact, the thread and the Cc — three more
    chances to disagree about who a message reaches (§Lessons 49)."""
    import inspect
    from applypilot.web_dashboard import _send_reply
    assert "_schedule_reply" in inspect.getsource(_send_reply)


def test_a_scheduled_reply_REQUIRES_a_thread(db):
    """Sending tolerates no thread because a human picked the box. Something that fires days
    later must not answer whichever conversation happens to be newest by then.

    The inbound message is what makes this able to FAIL. Without it `reply_target` finds nothing
    and the endpoint refuses anyway — with a *different* message that also contains the word
    "conversation" — so deleting the guard entirely left the assertion green. §Lessons 71: an
    assertion that still passes when the thing under test is removed.
    """
    from applypilot.web_dashboard import _send_reply
    _inbound(db, "2026-08-10T10:00:00+00:00")
    res = _send_reply({"contact_id": "r1", "body": "hi", "at": _at(3)})
    assert res["ok"] is False, "a reply with no thread must not be scheduled"
    assert "open the conversation" in res["message"], res["message"]
    assert reply_queue.pending_all(db) == {}
    # ...and the SAME request with a thread is accepted, so this is testing the guard and not
    # some unrelated refusal standing in front of it.
    ok = _send_reply({"contact_id": "r1", "body": "hi", "thread": "tA", "at": _at(3)})
    assert ok["ok"] is True, ok


def test_the_ENDPOINT_records_which_inbound_message_was_answered(db):
    """The staleness guard is only as good as the timestamp the promise carries.

    Every firing test above passes `last_inbound_at` itself, so all of them stayed green against
    an endpoint that recorded nothing — and with it empty, `stale_against` reads "no evidence"
    and the guard can NEVER fire. §Lessons 100: a test that supplies the value it is checking
    cannot see the value that ships.
    """
    from applypilot.web_dashboard import _send_reply
    _inbound(db, "2026-08-10T10:00:00+00:00")
    _send_reply({"contact_id": "r1", "body": "hi", "thread": "tA", "at": _at(3)})
    row = reply_queue.pending_all(db)["r1"]["tA"]
    assert row["last_inbound_at"].startswith("2026-08-10"), row


def test_staleness_works_END_TO_END_from_the_endpoint(db, wired):
    """Schedule the way the browser does, then have them write again. Nothing in this test names
    a timestamp, so it fails if any link in the chain drops one."""
    from applypilot.web_dashboard import _send_reply
    _inbound(db, "2026-08-10T10:00:00+00:00", mid="in1")
    assert _send_reply({"contact_id": "r1", "body": "answer",
                        "thread": "tA", "at": _at(-1)}).get("ok") is not None
    # A promise for the past is refused, so schedule properly then move the clock by writing.
    reply_queue.cancel("r1", "tA", db)
    _send_reply({"contact_id": "r1", "body": "answer", "thread": "tA", "at": _at(3)})
    # Bring it due without touching last_inbound_at.
    db.execute("UPDATE reply_queue SET scheduled_at = ? WHERE contact_id = 'r1'", (_at(-1),))
    db.commit()
    _inbound(db, "2026-08-14T10:00:00+00:00", mid="in2")     # they wrote again
    res = _poll()
    assert res["replies_sent"] == 0 and wired == [], res
    assert any("wrote again" in w for w in res["replies_why"]), res


def test_a_bad_time_is_refused_before_anything_is_promised(db):
    from applypilot.web_dashboard import _send_reply
    _inbound(db, "2026-08-10T10:00:00+00:00")
    res = _send_reply({"contact_id": "r1", "body": "hi", "thread": "tA", "at": "yesterday"})
    assert res["ok"] is False
    assert reply_queue.pending_all(db) == {}


def test_cancelling_hands_the_text_BACK(db):
    """This table is the only copy. A cancel that merely deleted the row would throw away what
    the operator wrote."""
    from applypilot.web_dashboard import _send_reply
    reply_queue.schedule("r1", "tA", "my careful answer", _at(3), conn=db)
    res = _send_reply({"contact_id": "r1", "body": "x", "thread": "tA", "at": "cancel"})
    assert res["ok"] is True and res["body"] == "my careful answer"
    assert reply_queue.pending_all(db) == {}


# ── the browser ─────────────────────────────────────────────────────────────

def test_a_queued_reply_replaces_the_session_draft_in_the_composer():
    """The browser draft is per-session. After a reload the composer would otherwise be empty on
    a conversation that already has an answer waiting, and the operator writes it twice."""
    js = JS.read_text(encoding="utf-8")
    assert "const queued = (c.scheduled_replies || {})[tk];" in js
    assert "const body = queued ? queued.body : (REPLY_DRAFT.get(k) || '');" in js


def test_the_composer_says_when_a_queued_reply_will_go_and_offers_a_cancel():
    js = JS.read_text(encoding="utf-8")
    m = re.search(r"const queuedRow = queued \?(.+?): '';", js, re.S)
    assert m, "no queued row rendered"
    assert "fmtSched(queued.scheduled_at)" in m.group(1)
    assert "unscheduleReply" in m.group(1), "a state you cannot clear is a dead end"


def test_scheduling_a_reply_posts_UTC_and_names_the_time_in_the_confirm():
    js = JS.read_text(encoding="utf-8")
    m = re.search(r"async function scheduleReply\(cid, tk, btn, commit\)\s*\{(.+?)\n\}", js, re.S)
    assert m
    body = m.group(1)
    assert "schedToUtc(" in body, "the picker's local wall-clock must be converted"
    assert "fmtSched(at)" in body, "the confirm must name the time the operator will see"
    assert "card.dataset.thread" in body, "the thread must travel with the promise"


def test_the_send_later_button_hides_once_something_is_queued():
    """Two promises on one conversation is not a state the server can hold — `queue_id` is keyed
    on (contact, thread) — so offering it would be a control that silently overwrites."""
    js = JS.read_text(encoding="utf-8")
    assert "${queued ? '' : `<button class=\"secondary\" onclick=\"scheduleReply(" in js

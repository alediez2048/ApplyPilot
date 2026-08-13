"""The SERVER half of one-composer-per-thread.

`tests/test_thread_grouping_agrees.py` drives the browser, and it supplies its own
`reply_targets` fixture — so it proves the rendering and nothing about where that map comes
from. Six mutations survived it: the payload shipping no targets at all, every thread getting
one whether or not it can be answered, and the drafter ignoring the thread it was handed. Every
one of those is invisible from a test whose input it wrote itself (§Lessons 103).

`reply_target` itself is covered in `test_reply_stays_in_its_thread.py`. This is about the map,
the drafter and the paste.
"""

from __future__ import annotations

import pytest

import applypilot.database as database
from applypilot.networking import messages as msg_store, store

ME = "me@work.test"


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


def _m(mid, tid, direction, frm, subject, at, snippet=""):
    return {"message_id": mid, "thread_id": tid, "contact_id": "c1", "job_url": "http://j/1",
            "direction": direction, "from_addr": frm, "from_name": "", "to_addrs": [ME],
            "cc_addrs": [], "subject": subject, "sent_at": at,
            "rfc_message_id": f"<{mid}>", "snippet": snippet}


@pytest.fixture()
def three_threads(db, monkeypatch):
    """Two answerable conversations and one where only we have written."""
    monkeypatch.setattr("applypilot.networking.gmail_send._our_addresses", lambda: [ME])
    msg_store.upsert_messages([
        _m("a1", "deal", "out", ME, "Ormus <> AMSYS", "2026-08-01T10:00"),
        _m("a2", "deal", "in", "v@writer.test", "Re: Ormus <> AMSYS", "2026-08-02T10:00",
           "sounds good"),
        _m("b1", "inv", "in", "accounts@writer.test", "Invoice", "2026-08-05T10:00", "attached"),
        _m("c1m", "solo", "out", ME, "Intro deck", "2026-08-06T10:00"),
    ], db)
    return msg_store.thread_for_contact("c1", db)


# ── the map ─────────────────────────────────────────────────────────────────

def test_one_target_per_ANSWERABLE_thread(three_threads):
    """The whole feature. Seven threads and one reply box was the report; the map is what turns
    that into one box per conversation."""
    from applypilot.web_dashboard import _reply_targets
    got = _reply_targets(three_threads)
    assert set(got) == {"deal", "inv"}, f"expected both answerable threads, got {sorted(got)}"


def test_a_thread_nobody_answered_gets_NO_target(three_threads):
    """Replying to a conversation with no inbound message is a FOLLOW-UP — its own ladder, its
    own schedule, its own stop conditions. A target here is what would blur the two, and the
    browser renders a composer for exactly the threads this map names."""
    from applypilot.web_dashboard import _reply_targets
    assert "solo" not in _reply_targets(three_threads)


def test_each_target_addresses_ITS_OWN_thread(three_threads):
    """The dangerous half: two composers that both said "Reply to Victoria" would look correct
    and send one of them to the wrong person (§Lessons 29)."""
    from applypilot.web_dashboard import _reply_targets
    got = _reply_targets(three_threads)
    assert got["deal"]["to_addr"] == "v@writer.test"
    assert got["inv"]["to_addr"] == "accounts@writer.test"
    # And each chains only its own thread.
    assert got["deal"]["references"].split() == ["<a1>", "<a2>"]
    assert got["inv"]["references"].split() == ["<b1>"]


def test_it_reaches_the_wire(three_threads, db, monkeypatch):
    """A map computed and never shipped is a feature nobody can use. Asserts on the PAYLOAD
    rather than the helper, because that is what the browser actually receives."""
    from applypilot import web_dashboard as wd
    monkeypatch.setattr(wd, "get_connection", lambda *a, **k: db)
    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Victoria",
                                "email": "v@writer.test"}, db)
    payload = wd._contact_payload(dict(store.get_contact(cid, db)), company="Writer",
                                  ladders={}, conn_matches={}, thread=three_threads)
    assert set(payload.get("reply_targets") or {}) == {"deal", "inv"}
    # And the single default is still there — the banner and `openReplyHere` read it.
    assert (payload.get("reply_to") or {}).get("thread_key") == "inv"


def test_nothing_stored_means_nothing_offered(db):
    from applypilot.web_dashboard import _reply_targets
    assert _reply_targets([]) == {}
    assert _reply_targets(None) == {}


# ── the drafter ─────────────────────────────────────────────────────────────

def _draft(db, monkeypatch, thread, captured):
    """Run `_draft_reply` with the model stubbed, capturing the transcript it was given."""
    from applypilot import web_dashboard as wd
    monkeypatch.setattr(wd, "get_connection", lambda *a, **k: db)
    monkeypatch.setattr("applypilot.networking.gmail_send._our_addresses", lambda: [ME])

    def fake_draft(*a, **k):
        captured.append((a, k))
        return "drafted body"

    monkeypatch.setattr("applypilot.networking.outreach.draft_reply", fake_draft, raising=False)
    return wd._draft_reply({"contact_id": "c1", "thread": thread, "their_reply": ""})


def test_the_drafter_refuses_a_thread_that_is_not_there(three_threads, db, monkeypatch):
    """Falling back to the merged list is the bug: it would answer whichever message is newest
    across every conversation, from a composer sitting under a different subject."""
    from applypilot import web_dashboard as wd
    monkeypatch.setattr(wd, "get_connection", lambda *a, **k: db)
    store.upsert_contact({"job_url": "http://j/1", "full_name": "V", "email": "v@writer.test",
                          "id": "c1"}, db)
    res = wd._draft_reply({"contact_id": "c1", "thread": "no-such-thread"})
    assert res["ok"] is False
    assert "conversation" in res["message"], res


def test_a_paste_lands_on_THIS_threads_message(three_threads, db, monkeypatch):
    """`set_reply_text` attaches to the newest inbound. Unscoped that is the newest across every
    thread, so text pasted under one conversation is stored on a message in another — and that
    text then feeds every later drafter and the reply timeline."""
    from applypilot import web_dashboard as wd
    monkeypatch.setattr(wd, "get_connection", lambda *a, **k: db)
    monkeypatch.setattr("applypilot.networking.gmail_send._our_addresses", lambda: [ME])
    store.upsert_contact({"job_url": "http://j/1", "full_name": "V", "email": "v@writer.test",
                          "id": "c1"}, db)

    wd._draft_reply({"contact_id": "c1", "thread": "deal",
                     "their_reply": "PASTED INTO THE DEAL"})

    rows = {r[0]: r[1] for r in db.execute(
        "SELECT message_id, snippet FROM messages WHERE contact_id = 'c1'").fetchall()}
    assert rows["a2"] == "PASTED INTO THE DEAL", "the paste did not reach the deal thread"
    assert rows["b1"] == "attached", "the paste overwrote a message in ANOTHER conversation"


def test_set_reply_text_uses_the_message_it_was_given(three_threads, db):
    """The unscoped default stays for the CLI and for single-thread contacts, so both paths
    need holding."""
    assert msg_store.set_reply_text("c1", "ON THE DEAL", db, message_id="a2") is True
    rows = {r[0]: r[1] for r in db.execute(
        "SELECT message_id, snippet FROM messages WHERE contact_id = 'c1'").fetchall()}
    assert rows["a2"] == "ON THE DEAL"
    assert rows["b1"] == "attached"

    # No message_id -> the newest inbound overall, which here is the invoice.
    assert msg_store.set_reply_text("c1", "NEWEST", db) is True
    rows = {r[0]: r[1] for r in db.execute(
        "SELECT message_id, snippet FROM messages WHERE contact_id = 'c1'").fetchall()}
    assert rows["b1"] == "NEWEST"


def test_a_message_that_is_not_this_contacts_is_refused(three_threads, db):
    """The id comes from a caller holding a scoped thread, but it must still not be able to
    write onto somebody else's row."""
    assert msg_store.set_reply_text("c1", "x", db, message_id="nope") is False

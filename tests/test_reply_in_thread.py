"""CRM-4a — replying in-thread from the dashboard, with the Cc preserved.

The bug this exists to prevent is silent. Victoria answered by Cc'ing David, so David is the
person now handling the application; a reply addressed only to the contact row reaches Victoria,
looks completely normal on screen, and David never hears about it. Nothing errors, nothing is
logged, and the operator has no way to tell.

So the assertions below are mostly about WHO receives the message, not whether sending worked.
"""

from __future__ import annotations

import pytest

import applypilot.database as database
from applypilot.domain import conversations as cv
from applypilot.networking import messages as msg_store, store

ME = "jorgealejandrodiezm@gmail.com"


def _msg(direction, frm, to, cc=(), subject="Applied AI Engineer", at="2026-07-29T10:00:00+00:00",
         rfc="", name=""):
    return {"direction": direction, "from_addr": frm, "from_name": name,
            "to_addrs": list(to), "cc_addrs": list(cc), "subject": subject,
            "sent_at": at, "rfc_message_id": rfc, "thread_id": "t1"}


#: The real thread, as stored: we wrote, Victoria answered and Cc'd a colleague.
WRITER_THREAD = [
    _msg("out", ME, ["Victoria Shearer <victoria.shearer@writer.com>"], rfc="<a@us>",
         at="2026-07-28T09:00:00+00:00"),
    _msg("in", "victoria.shearer@writer.com", [ME], ["David Loveless <david@writer.com>"],
         rfc="<b@writer>", name="Victoria Shearer", at="2026-07-29T09:00:00+00:00"),
]


# ── the whole point: the Cc survives ─────────────────────────────────────────────────────

def test_a_reply_keeps_the_person_who_was_introduced():
    t = cv.reply_target(WRITER_THREAD, ME)
    assert t["to_addr"] == "victoria.shearer@writer.com"
    assert [cv.addr(c) for c in t["cc"]] == ["david@writer.com"], (
        "the introduced colleague was dropped from the reply — the exact silent failure "
        "CRM-4a exists to stop")


def test_the_cc_keeps_its_display_name():
    """Storing a bare address destroys the name for good: 'David Loveless' -> 'David'."""
    t = cv.reply_target(WRITER_THREAD, ME)
    assert "David Loveless" in t["cc"][0]


def test_we_are_never_cc_d_on_our_own_reply():
    t = cv.reply_target(WRITER_THREAD, ME)
    assert all(cv.addr(c) != cv.addr(ME) for c in t["cc"])


def test_every_address_of_ours_is_excluded_not_just_the_first():
    """Sending can authenticate as one account and set From to a verified alias. Both are us,
    and `_from_address()` is EMPTY on an OAuth-only setup, so one address cannot be the source."""
    alias = "alejandro@jorgealejandrodiez.com"
    thread = [WRITER_THREAD[0],
              _msg("in", "victoria.shearer@writer.com", [ME],
                   [f"<{alias}>", "David Loveless <david@writer.com>"], rfc="<b@writer>")]
    t = cv.reply_target(thread, [ME, alias])
    assert [cv.addr(c) for c in t["cc"]] == ["david@writer.com"]

    # And with only one of them known, the other really does leak — proving the list matters.
    leaky = cv.reply_target(thread, ME)
    assert alias in [cv.addr(c) for c in leaky["cc"]]


def test_the_sender_is_not_also_cc_d():
    """A reply-all that lists the recipient twice is a visible sloppiness in a real conversation."""
    thread = [WRITER_THREAD[0],
              _msg("in", "victoria.shearer@writer.com", [ME, "victoria.shearer@writer.com"],
                   ["david@writer.com"], rfc="<b@writer>")]
    t = cv.reply_target(thread, ME)
    assert [cv.addr(c) for c in t["cc"]] == ["david@writer.com"]


def test_robots_on_the_thread_are_not_cc_d():
    thread = [WRITER_THREAD[0],
              _msg("in", "victoria.shearer@writer.com", [ME],
                   ["no-reply@greenhouse.io", "David Loveless <david@writer.com>"], rfc="<b@w>")]
    t = cv.reply_target(thread, ME)
    assert [cv.addr(c) for c in t["cc"]] == ["david@writer.com"]


# ── which message we are answering ───────────────────────────────────────────────────────

def test_it_answers_the_last_INBOUND_message_not_the_last_message():
    """After we reply, the newest message in the thread is OURS. Reading recipients off it is
    reading a copy of our own copy — and it loses anyone added since."""
    thread = list(WRITER_THREAD) + [
        _msg("out", ME, ["victoria.shearer@writer.com"], ["david@writer.com"],
             rfc="<c@us>", at="2026-07-29T12:00:00+00:00"),
        _msg("in", "david@writer.com", [ME],
             ["Victoria Shearer <victoria.shearer@writer.com>", "Sam Ops <sam@writer.com>"],
             rfc="<d@writer>", name="David Loveless", at="2026-07-30T09:00:00+00:00"),
    ]
    t = cv.reply_target(thread, ME)
    assert t["to_addr"] == "david@writer.com", "replied to the wrong person"
    assert sorted(cv.addr(c) for c in t["cc"]) == ["sam@writer.com", "victoria.shearer@writer.com"]


def test_a_thread_nobody_answered_offers_no_reply():
    """A thread with no inbound message is a FOLLOW-UP, which has a ladder, a schedule and stop
    conditions. Silently answering it as a 'reply' would bypass all three."""
    assert cv.reply_target([WRITER_THREAD[0]], ME) is None
    assert cv.reply_target([], ME) is None
    assert cv.reply_target(None, ME) is None


def test_an_inbound_message_with_no_sender_is_not_repliable():
    assert cv.reply_target([_msg("in", "", [ME])], ME) is None


# ── threading headers ────────────────────────────────────────────────────────────────────

def test_references_chains_the_whole_thread_not_just_one_message():
    t = cv.reply_target(WRITER_THREAD, ME)
    assert t["in_reply_to"] == "<b@writer>", "must answer the message we actually read"
    assert t["references"] == "<a@us> <b@writer>", (
        "References must chain the thread; clients that walk the chain split the conversation "
        "in two without it")


def test_subject_does_not_accumulate_re_prefixes():
    thread = [WRITER_THREAD[0],
              _msg("in", "victoria.shearer@writer.com", [ME], subject="RE: Re: Fwd: AI Engineer",
                   rfc="<b@w>")]
    assert cv.reply_target(thread, ME)["subject"] == "Re: AI Engineer"


def test_a_subjectless_thread_does_not_produce_a_bare_re():
    thread = [WRITER_THREAD[0], _msg("in", "v@writer.com", [ME], subject="", rfc="<b@w>")]
    assert cv.reply_target(thread, ME)["subject"] == ""


# ── persistence ──────────────────────────────────────────────────────────────────────────

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


def test_the_rfc_id_survives_a_round_trip_through_the_database(db):
    """Without it a reply can only chain off our own FIRST email, and the answer shows up as a
    separate conversation beside the one it answers."""
    msg_store.upsert_messages([{"message_id": "m1", "thread_id": "t1", "contact_id": "c1",
                                "job_url": "http://j/1", "direction": "in",
                                "from_addr": "v@writer.com", "to_addrs": [ME],
                                "cc_addrs": ["david@writer.com"], "subject": "hi",
                                "sent_at": "2026-07-29T09:00:00+00:00",
                                "rfc_message_id": "<b@writer>"}], db)
    stored = msg_store.thread_for_contact("c1", db)
    assert stored[0]["rfc_message_id"] == "<b@writer>"
    t = cv.reply_target(stored, ME)
    assert t["in_reply_to"] == "<b@writer>"
    assert [cv.addr(c) for c in t["cc"]] == ["david@writer.com"]


def test_a_sent_reply_appears_in_the_thread_immediately(db):
    """Otherwise Send visibly does nothing until the next poll, which is indistinguishable from
    a button that did not fire (§Lessons 15)."""
    contact = {"id": "c1", "job_url": "http://j/1", "thread_id": "t1"}
    msg_store.record_outbound(contact, {"id": "sent1", "thread_id": "t1",
                                        "rfc_message_id": "<new@us>", "from_addr": ME},
                              "v@writer.com", ["david@writer.com"], "Re: hi", db)
    rows = msg_store.thread_for_contact("c1", db)
    assert [r["direction"] for r in rows] == ["out"]
    assert rows[0]["cc_addrs"] == ["david@writer.com"]


def test_recording_the_same_sent_message_twice_does_not_duplicate_it(db):
    """The poll that eventually covers this message must overwrite it, not add a second copy —
    `tick` re-syncs every open thread hourly, forever."""
    contact = {"id": "c1", "job_url": "http://j/1", "thread_id": "t1"}
    sent = {"id": "sent1", "thread_id": "t1", "rfc_message_id": "<new@us>", "from_addr": ME}
    for _ in range(3):
        msg_store.record_outbound(contact, sent, "v@writer.com", [], "Re: hi", db)
    assert len(msg_store.thread_for_contact("c1", db)) == 1


def test_a_send_with_no_message_id_is_not_stored_as_a_ghost(db):
    """No id means no dedupe key, so storing it would let every later poll add another copy."""
    msg_store.record_outbound({"id": "c1", "job_url": "http://j/1"}, {}, "v@writer.com", [],
                              "Re: hi", db)
    assert msg_store.thread_for_contact("c1", db) == []


# ── the send path ────────────────────────────────────────────────────────────────────────

def test_send_reply_refuses_an_empty_body(db):
    from applypilot.networking import gmail_send
    assert gmail_send.send_reply("c1", "   ", conn=db)["ok"] is False


def test_send_reply_refuses_a_thread_nobody_answered(db):
    """The follow-up ladder owns that case."""
    from applypilot.networking import gmail_send
    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Victoria",
                                "email": "v@writer.com", "sent_message_id": "m1"}, db)
    msg_store.upsert_messages([{"message_id": "m1", "thread_id": "t1", "contact_id": cid,
                                "job_url": "http://j/1", "direction": "out", "from_addr": ME,
                                "to_addrs": ["v@writer.com"], "subject": "hi",
                                "sent_at": "2026-07-28T09:00:00+00:00"}], db)
    res = gmail_send.send_reply(cid, "hello?", conn=db)
    assert res["ok"] is False and "follow-up" in res["message"]


def test_send_reply_never_attaches_the_resume(db, monkeypatch):
    """It went with email #1. Re-attaching it to a live conversation reads as automated."""
    from applypilot.networking import gmail_oauth, gmail_send

    seen = {}

    def fake_send(to_addr, subject, body, from_addr, from_name="", **kw):
        seen.update(kw, to=to_addr, subject=subject)
        return {"id": "sent1", "thread_id": "t1", "rfc_message_id": "<new@us>"}

    monkeypatch.setattr(gmail_send, "transport", lambda: "oauth")
    monkeypatch.setattr(gmail_send, "_our_addresses", lambda: [ME])
    monkeypatch.setattr(gmail_oauth, "send", fake_send)
    monkeypatch.setattr(gmail_oauth, "connected_email", lambda: ME)

    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Victoria",
                                "email": "victoria.shearer@writer.com",
                                "sent_message_id": "m1", "thread_id": "t1"}, db)
    rows = [dict(m, message_id=f"m{i}", contact_id=cid, job_url="http://j/1")
            for i, m in enumerate(WRITER_THREAD)]
    msg_store.upsert_messages(rows, db)

    res = gmail_send.send_reply(cid, "Thanks — happy to talk.", conn=db)
    assert res["ok"] is True
    assert seen.get("attachments") is None
    assert [cv.addr(c) for c in seen["cc"]] == ["david@writer.com"], "the Cc was dropped on send"
    assert seen["in_reply_to"] == "<b@writer>"
    assert seen["references"] == "<a@us> <b@writer>"
    assert seen["thread_id"] == "t1"
    # And it is visible right away, without waiting for a poll.
    assert any(m["direction"] == "out" and m["message_id"] == "sent1"
               for m in msg_store.thread_for_contact(cid, db))


def test_an_operator_who_removes_everyone_from_the_cc_is_obeyed(db, monkeypatch):
    """`cc=None` means 'keep the thread's Cc'; `cc=[]` means 'I removed them'. Collapsing the
    two would either ignore the operator or silently drop the introduced person."""
    from applypilot.networking import gmail_oauth, gmail_send

    seen = {}
    monkeypatch.setattr(gmail_send, "transport", lambda: "oauth")
    monkeypatch.setattr(gmail_send, "_our_addresses", lambda: [ME])
    monkeypatch.setattr(gmail_oauth, "connected_email", lambda: ME)
    monkeypatch.setattr(gmail_oauth, "send",
                        lambda *a, **kw: (seen.update(kw),
                                          {"id": "s1", "thread_id": "t1",
                                           "rfc_message_id": "<n@us>"})[1])

    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Victoria",
                                "email": "victoria.shearer@writer.com",
                                "sent_message_id": "m1", "thread_id": "t1"}, db)
    msg_store.upsert_messages(
        [dict(m, message_id=f"m{i}", contact_id=cid, job_url="http://j/1")
         for i, m in enumerate(WRITER_THREAD)], db)

    gmail_send.send_reply(cid, "just you", cc=[], conn=db)
    assert seen["cc"] == []
    gmail_send.send_reply(cid, "all of you", cc=None, conn=db)
    assert [cv.addr(c) for c in seen["cc"]] == ["david@writer.com"]


# ── the dashboard cannot be talked into emailing a stranger ──────────────────────────────

def test_the_endpoint_ignores_a_recipient_supplied_by_the_browser(db, monkeypatch):
    """Recipients come from the STORED thread. An endpoint that accepted a `to` would be an
    open relay aimed at whatever the page happened to be holding."""
    from applypilot import web_dashboard
    from applypilot.networking import gmail_oauth, gmail_send

    seen = {}
    monkeypatch.setattr(web_dashboard, "get_connection", lambda *a, **k: db)
    monkeypatch.setattr(gmail_send, "transport", lambda: "oauth")
    monkeypatch.setattr(gmail_send, "_our_addresses", lambda: [ME])
    monkeypatch.setattr(gmail_oauth, "connected_email", lambda: ME)
    monkeypatch.setattr(gmail_oauth, "send",
                        lambda to_addr, *a, **kw: (seen.update(kw, to=to_addr),
                                                   {"id": "s1", "thread_id": "t1",
                                                    "rfc_message_id": "<n@us>"})[1])

    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Victoria",
                                "email": "victoria.shearer@writer.com",
                                "sent_message_id": "m1", "thread_id": "t1"}, db)
    msg_store.upsert_messages(
        [dict(m, message_id=f"m{i}", contact_id=cid, job_url="http://j/1")
         for i, m in enumerate(WRITER_THREAD)], db)

    res = web_dashboard._send_reply({"contact_id": cid, "body": "hi",
                                     "to": "attacker@evil.com", "cc": ["attacker@evil.com"]})
    assert res["ok"] is True
    assert seen["to"] == "victoria.shearer@writer.com"
    # A Cc the operator genuinely edits is honoured, so the browser CAN name a Cc — but only
    # ever alongside the thread's real recipient, never instead of it.
    assert seen["to"] != "attacker@evil.com"


def test_the_endpoint_needs_a_body_and_a_real_contact(db, monkeypatch):
    from applypilot import web_dashboard
    monkeypatch.setattr(web_dashboard, "get_connection", lambda *a, **k: db)
    assert web_dashboard._send_reply({"contact_id": "", "body": "hi"})["ok"] is False
    assert web_dashboard._send_reply({"contact_id": "c1", "body": " "})["ok"] is False
    assert web_dashboard._send_reply({"contact_id": "nope", "body": "hi"})["ok"] is False


def test_a_reply_target_never_500s_the_dashboard(db):
    """Rendered on every 2.5s refresh, so one odd header must not take the whole page with it
    (§Lessons 6 — the same reason `_parse_ts` exists)."""
    from applypilot import web_dashboard
    assert web_dashboard._reply_target([]) is None
    assert web_dashboard._reply_target([{"direction": "in"}]) is None
    assert web_dashboard._reply_target(["not a dict"]) is None


# ── the thread a reply lands on (2026-08-12) ─────────────────────────────────────────────

#: A SECOND conversation with the same person. Older than the Writer thread, and answered by
#: somebody else — which is what makes it dangerous: both threads render the same composer.
OTHER_THREAD = [
    dict(_msg("out", ME, ["Victoria Shearer <victoria.shearer@writer.com>"], rfc="<x@us>",
              subject="Invoice", at="2026-07-30T09:00:00+00:00"), thread_id="t2"),
    dict(_msg("in", "accounts@writer.com", [ME], rfc="<y@writer>", subject="Re: Invoice",
              name="Accounts", at="2026-07-31T09:00:00+00:00"), thread_id="t2"),
]


def _two_threads(db, monkeypatch):
    """One contact, two Gmail conversations, and a fake transport that records the send."""
    from applypilot.networking import gmail_oauth, gmail_send
    seen = {}

    def fake_send(to_addr, subject, body, from_addr, from_name="", **kw):
        seen.update(kw, to=to_addr, subject=subject)
        return {"id": "sent9", "thread_id": kw.get("thread_id") or "", "rfc_message_id": "<n@us>"}

    monkeypatch.setattr(gmail_send, "transport", lambda: "oauth")
    monkeypatch.setattr(gmail_send, "_our_addresses", lambda: [ME])
    monkeypatch.setattr(gmail_oauth, "send", fake_send)
    monkeypatch.setattr(gmail_oauth, "connected_email", lambda: ME)

    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Victoria",
                                "email": "victoria.shearer@writer.com",
                                "sent_message_id": "m1", "thread_id": "t1"}, db)
    rows = [dict(m, message_id=f"m{i}", contact_id=cid, job_url="http://j/1")
            for i, m in enumerate(WRITER_THREAD + OTHER_THREAD)]
    msg_store.upsert_messages(rows, db)
    return cid, seen


def test_a_reply_goes_to_the_thread_the_operator_had_open(db, monkeypatch):
    """The whole fix. Both conversations are stored against one contact; without a thread the
    send answers whichever holds the newest inbound, which here is Accounts."""
    from applypilot.networking import gmail_send
    cid, seen = _two_threads(db, monkeypatch)

    assert gmail_send.send_reply(cid, "about the role", conn=db, thread="t1")["ok"] is True
    assert seen["to"] == "victoria.shearer@writer.com"
    assert seen["thread_id"] == "t1"


def test_the_references_header_does_not_span_both_conversations(db, monkeypatch):
    """Measured on the live database, every multi-thread contact had this: one at 87 message ids
    across 25 unrelated threads. A mail client reads that as one conversation."""
    from applypilot.networking import gmail_send
    cid, seen = _two_threads(db, monkeypatch)

    gmail_send.send_reply(cid, "about the role", conn=db, thread="t1")
    assert seen["references"] == "<a@us> <b@writer>"
    assert "<y@writer>" not in seen["references"], "the invoice thread leaked into the reply"


def test_naming_the_other_thread_reaches_the_other_person(db, monkeypatch):
    """The negative case: without it the test above passes on a function that ignores `thread`
    and always answers the first conversation."""
    from applypilot.networking import gmail_send
    cid, seen = _two_threads(db, monkeypatch)

    gmail_send.send_reply(cid, "about the invoice", conn=db, thread="t2")
    assert seen["to"] == "accounts@writer.com"
    assert seen["references"] == "<x@us> <y@writer>"


def test_an_unknown_thread_is_REFUSED_and_sends_nothing(db, monkeypatch):
    """Falling back to the merged list is the bug. A stale page naming a thread that no longer
    exists must not send to whoever happens to be newest."""
    from applypilot.networking import gmail_send
    cid, seen = _two_threads(db, monkeypatch)

    res = gmail_send.send_reply(cid, "hello", conn=db, thread="gone")
    assert res["ok"] is False
    assert not seen, "a refused reply still reached the transport"
    assert "thread" in res["message"], f"the refusal does not say what went wrong: {res}"


def test_with_no_thread_named_the_old_behaviour_survives(db, monkeypatch):
    """The CLI and any older caller pass nothing, and must still send — to one conversation."""
    from applypilot.networking import gmail_send
    cid, seen = _two_threads(db, monkeypatch)

    assert gmail_send.send_reply(cid, "hi", conn=db)["ok"] is True
    assert seen["to"] == "accounts@writer.com"      # the newest inbound
    assert seen["references"] == "<x@us> <y@writer>"           # and only that thread


def test_the_endpoint_passes_the_thread_through(db, monkeypatch):
    """A parameter the handler accepts and drops is §Lessons 39 — and here it silently restores
    the misaddressing while every unit test of the layer below still passes."""
    from applypilot import web_dashboard
    from applypilot.networking import gmail_send
    cid, seen = _two_threads(db, monkeypatch)
    monkeypatch.setattr(web_dashboard, "get_connection", lambda *a, **k: db)

    assert web_dashboard._send_reply({"contact_id": cid, "body": "hi", "thread": "t1"})["ok"]
    assert seen["to"] == "victoria.shearer@writer.com"
    assert gmail_send is not None


def test_the_composer_is_told_which_conversation_it_answers(db, monkeypatch):
    """The operator has to be able to SEE which thread the box is pointed at — the two render
    identically and only one of them is what they meant (§Lessons 29)."""
    from applypilot import web_dashboard
    cid, _ = _two_threads(db, monkeypatch)
    monkeypatch.setattr(web_dashboard, "_our_addresses", lambda: [ME], raising=False)

    t = web_dashboard._reply_target(msg_store.thread_for_contact(cid, db), key="t1")
    assert t["thread_key"] == "t1"
    assert t["thread_subject"] == "Applied AI Engineer"
    assert t["to_addr"] == "victoria.shearer@writer.com"


def test_a_named_thread_never_falls_back_to_the_contacts_own(db, monkeypatch):
    """`contacts.thread_id` is the FIRST-CONTACT conversation. It is a sane default only when
    nobody named a thread — once one is named, sending into a different one is the exact bug
    the parameter exists to close.

    The case is a thread with no Gmail `threadId` of its own: pasted messages and anything synced
    before threading was stored have none. References and In-Reply-To still carry the right
    conversation, so Gmail threads it correctly with no threadId at all.
    """
    from applypilot.networking import gmail_oauth, gmail_send
    seen = {}

    def fake_send(to_addr, subject, body, from_addr, from_name="", **kw):
        seen.update(kw, to=to_addr)
        return {"id": "s1", "thread_id": kw.get("thread_id") or "", "rfc_message_id": "<n@us>"}

    monkeypatch.setattr(gmail_send, "transport", lambda: "oauth")
    monkeypatch.setattr(gmail_send, "_our_addresses", lambda: [ME])
    monkeypatch.setattr(gmail_oauth, "send", fake_send)
    monkeypatch.setattr(gmail_oauth, "connected_email", lambda: ME)

    cid = store.upsert_contact({"job_url": "http://j/9", "full_name": "Kim",
                                "email": "kim@co.test", "thread_id": "OLD"}, db)
    # One conversation, no thread id anywhere on it.
    msg_store.upsert_messages([
        dict(_msg("in", "kim@co.test", [ME], subject="Invoice", rfc="<p@co>",
                  at="2026-08-01T09:00:00+00:00", name="Kim"),
             message_id="p1", contact_id=cid, job_url="http://j/9", thread_id=""),
    ], db)

    key = cv.thread_key(msg_store.thread_for_contact(cid, db)[0])
    assert gmail_send.send_reply(cid, "here you go", conn=db, thread=key)["ok"] is True
    assert seen["to"] == "kim@co.test"
    assert seen["thread_id"] == "", \
        "the reply was pushed into the contact's original Gmail thread, not the one named"
    assert seen["in_reply_to"] == "<p@co>", "the reply lost its In-Reply-To and will not thread"


def test_with_no_thread_named_the_contacts_own_is_still_the_fallback(db, monkeypatch):
    """The negative case. Removing the fallback altogether would break every caller that passes
    nothing — the assertion above must not be satisfiable by deleting it."""
    from applypilot.networking import gmail_oauth, gmail_send
    seen = {}

    def fake_send(to_addr, subject, body, from_addr, from_name="", **kw):
        seen.update(kw, to=to_addr)
        return {"id": "s2", "thread_id": kw.get("thread_id") or "", "rfc_message_id": "<n2@us>"}

    monkeypatch.setattr(gmail_send, "transport", lambda: "oauth")
    monkeypatch.setattr(gmail_send, "_our_addresses", lambda: [ME])
    monkeypatch.setattr(gmail_oauth, "send", fake_send)
    monkeypatch.setattr(gmail_oauth, "connected_email", lambda: ME)

    cid = store.upsert_contact({"job_url": "http://j/8", "full_name": "Kim",
                                "email": "kim@co.test", "thread_id": "OLD"}, db)
    msg_store.upsert_messages([
        dict(_msg("in", "kim@co.test", [ME], subject="Invoice", rfc="<q@co>",
                  at="2026-08-01T09:00:00+00:00", name="Kim"),
             message_id="q1", contact_id=cid, job_url="http://j/8", thread_id=""),
    ], db)

    assert gmail_send.send_reply(cid, "here you go", conn=db)["ok"] is True
    assert seen["thread_id"] == "OLD"

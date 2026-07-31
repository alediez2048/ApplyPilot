"""CRM-1 — noticing that somebody replied.

At the time this was written the live DB held **33 sent emails and exactly one recorded reply,
typed in by hand.** Every follow-up the system schedules is potentially nudging someone who
already answered, which is worse than not following up at all.

The dangerous direction is a FALSE positive, not a missed one. A missed reply costs one wasted
follow-up. A false one silently halts a live conversation and inflates the funnel — and the
easiest way to manufacture false positives is to count our own sent mail, which Gmail returns
in the very same thread we are reading.
"""

from __future__ import annotations

import pytest

import applypilot.database as database
from applypilot.domain import replies as dr
from applypilot.networking import store, touches

ME = "jorgealejandrodiezm@gmail.com"


def _msg(**over):
    m = {"id": "m1", "thread_id": "t1", "labelIds": ["INBOX"], "internalDate": "1000",
         "from": "PJ <pj@zello.com>", "in_reply_to": "", "references": ""}
    m.update(over)
    return m


def _contact(**over):
    c = {"id": "c1", "job_url": "http://j/1", "full_name": "PJ", "email": "pj@zello.com",
         "thread_id": "t1", "rfc_message_id": "<orig@applypilot>", "submitted_at": "2026-07-01"}
    c.update(over)
    return c


# ── is_inbound: the false-positive guard ─────────────────────────────────────────────────

def test_our_own_sent_mail_is_never_a_reply():
    """Gmail returns our own messages in the same thread. Counting one would mark every
    contact we ever emailed as having answered."""
    assert dr.is_inbound(_msg(labelIds=["SENT"]), ME) is False
    # ...even when the sender header looks like a third party (a send-as address).
    assert dr.is_inbound(_msg(labelIds=["SENT", "INBOX"], **{"from": "pj@zello.com"}), ME) is False


def test_a_message_from_our_own_address_is_not_a_reply_even_without_the_label():
    """The SENT label is missing on mail synced from another client, so the From check is not
    redundant — it is the other half of the same guard."""
    assert dr.is_inbound(_msg(labelIds=["INBOX"], **{"from": f"Me <{ME}>"}), ME) is False


def test_a_real_inbound_message_is_a_reply():
    assert dr.is_inbound(_msg(), ME) is True


def test_a_message_with_no_sender_is_not_counted():
    """Unparseable beats guessed: erring towards 'not a reply' costs one follow-up."""
    assert dr.is_inbound(_msg(**{"from": ""}), ME) is False


def test_the_display_name_does_not_confuse_the_address_check():
    assert dr.is_inbound(_msg(**{"from": f'"Someone Else" <{ME}>'}), ME) is False
    assert dr.is_inbound(_msg(**{"from": '"Me" <other@x.com>'}), ME) is True


def test_address_comparison_ignores_case():
    assert dr.is_inbound(_msg(**{"from": ME.upper()}), ME) is False


# ── match_contact ────────────────────────────────────────────────────────────────────────

def test_thread_id_is_the_strongest_match():
    c = _contact()
    assert dr.match_contact(_msg(thread_id="t1"), [c, _contact(id="c2", thread_id="t9")]) is c


def test_in_reply_to_matches_when_the_thread_id_does_not():
    """How a reply forwarded into a new thread still lands on the right contact."""
    c = _contact(thread_id="")
    got = dr.match_contact(_msg(thread_id="", in_reply_to="<orig@applypilot>"), [c])
    assert got is c


def test_references_header_also_matches():
    c = _contact(thread_id="")
    got = dr.match_contact(
        _msg(thread_id="", references="<a@x> <orig@applypilot> <b@x>"), [c])
    assert got is c


def test_sender_address_is_the_last_resort():
    c = _contact(thread_id="", rfc_message_id="")
    assert dr.match_contact(_msg(thread_id=""), [c]) is c


def test_an_ambiguous_sender_matches_NOTHING():
    """The same person is often a contact on several jobs. Guessing which one would attach the
    reply — and stop the ladder — on the wrong application."""
    a = _contact(id="c1", job_url="http://j/1", thread_id="", rfc_message_id="")
    b = _contact(id="c2", job_url="http://j/2", thread_id="", rfc_message_id="")
    assert dr.match_contact(_msg(thread_id=""), [a, b]) is None


def test_an_unknown_sender_matches_nothing():
    assert dr.match_contact(_msg(**{"from": "stranger@nowhere.com", "thread_id": ""}),
                            [_contact(thread_id="", rfc_message_id="")]) is None


# ── replies_in ───────────────────────────────────────────────────────────────────────────

def test_only_the_FIRST_inbound_message_counts():
    """A long back-and-forth must not keep pushing replied_at later — the first reply is when
    the conversation turned, and it is what time_to_reply (CRM-2) measures."""
    msgs = [_msg(id="m2", internalDate="5000"), _msg(id="m1", internalDate="1000")]
    hits = dr.replies_in(msgs, [_contact()], ME)
    assert len(hits) == 1
    assert hits[0]["message"]["id"] == "m1"


def test_timestamps_compare_numerically_not_lexicographically():
    """internalDate is a STRING of ms-since-epoch. "9999" > "10000" as text, so a naive compare
    picks the wrong message the moment the digit count changes."""
    msgs = [_msg(id="later", internalDate="10000"), _msg(id="earlier", internalDate="9999")]
    assert dr.replies_in(msgs, [_contact()], ME)[0]["message"]["id"] == "earlier"


def test_a_thread_of_only_our_own_mail_yields_nothing():
    msgs = [_msg(id="s1", labelIds=["SENT"]), _msg(id="s2", labelIds=["SENT"])]
    assert dr.replies_in(msgs, [_contact()], ME) == []


# ── persistence: halting the ladder ──────────────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    touches.init_touches(conn)
    return conn


def test_marking_a_reply_stops_the_email_ladder_via_sequences(db):
    """ARCH-3 removed `followup_status`. Terminal state lives in `sequences` and a column must
    NOT be reintroduced for it."""
    from applypilot.networking import replies

    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "PJ",
                                "email": "pj@zello.com", "sent_message_id": "m1"}, db)
    replies.mark_replied({"id": cid, "job_url": "http://j/1", "full_name": "PJ"},
                         "2026-07-28T10:00:00+00:00", db)

    row = db.execute("SELECT status FROM sequences WHERE contact_id=? AND channel='email'",
                     (cid,)).fetchone()
    assert dict(row)["status"] == "replied"
    assert store.get_contact(cid, db)["replied_at"].startswith("2026-07-28")
    assert "followup_status" not in [r[1] for r in db.execute("PRAGMA table_info(contacts)")]


def test_the_linkedin_ladder_keeps_running(db):
    """A different thread and a different conversation. Silently stopping it would hide a
    channel the operator is still working."""
    from applypilot.networking import replies

    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "PJ",
                                "email": "pj@zello.com", "sent_message_id": "m1"}, db)
    replies.mark_replied({"id": cid, "job_url": "http://j/1", "full_name": "PJ"},
                         "2026-07-28T10:00:00+00:00", db)
    li = db.execute("SELECT COUNT(*) FROM sequences WHERE contact_id=? AND channel='linkedin'",
                    (cid,)).fetchone()[0]
    assert li == 0, "the LinkedIn ladder was stopped too"


def test_the_reply_reaches_the_activity_log(db):
    from applypilot.networking import replies

    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "PJ",
                                "email": "pj@zello.com", "sent_message_id": "m1"}, db)
    replies.mark_replied({"id": cid, "job_url": "http://j/1", "full_name": "PJ"},
                         "2026-07-28T10:00:00+00:00", db)
    blob = " ".join(e["detail"] or "" for e in database.get_job_events("http://j/1", conn=db))
    assert "PJ replied" in blob and "2026-07-28" in blob


def test_an_already_replied_contact_is_not_polled_again(db):
    """Re-marking would overwrite replied_at with a later message in the same thread."""
    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "PJ",
                                "email": "pj@zello.com", "sent_message_id": "m1",
                                "replied_at": "2026-07-28T10:00:00+00:00"}, db)
    awaiting = [c["id"] for c in store.contacts_awaiting_reply(db)]
    assert cid not in awaiting


def test_a_contact_that_was_never_emailed_is_not_polled(db):
    store.upsert_contact({"job_url": "http://j/1", "full_name": "Nobody"}, db)
    assert store.contacts_awaiting_reply(db) == []


# ── graceful degradation ─────────────────────────────────────────────────────────────────

def test_polling_without_the_scope_is_a_no_op_not_a_crash(db, monkeypatch):
    """Sending must keep working when reply detection cannot."""
    from applypilot.networking import gmail_read, replies

    monkeypatch.setattr(gmail_read, "available", lambda: (False, "scope not granted"))
    res = replies.poll(db)
    assert res["ok"] is False and res["replied"] == 0
    assert "scope" in res["note"]


# ── bounces are NOT replies ──────────────────────────────────────────────────────────────
#
# Found on the very first live poll. An Affirm contact was recorded as having REPLIED the same
# day she was emailed. The message was:
#
#     from: Mail Delivery System <MAILER-DAEMON@mxa.us.…>
#     subject: Undelivered Mail Returned to Sender
#
# The email never reached her. It arrives IN OUR THREAD, so thread-id matching accepted it
# happily. Counting that as a reply is worse than missing a real one: it stops the ladder,
# records engagement that never happened, and poisons every rate CRM-2 will compute.

def _bounce(**over):
    m = _msg(id="b1", **{"from": "Mail Delivery System <MAILER-DAEMON@mxa.us.example.com>",
                         "subject": "Undelivered Mail Returned to Sender"})
    m.update(over)
    return m


def test_a_mailer_daemon_bounce_is_not_a_reply():
    """The exact false positive."""
    assert dr.is_bounce(_bounce()) is True
    assert dr.replies_in([_bounce()], [_contact()], ME) == []


@pytest.mark.parametrize("sender", [
    "MAILER-DAEMON@x.com", "postmaster@x.com", "no-reply@x.com", "noreply@x.com",
])
def test_mail_infrastructure_senders_are_never_the_human(sender):
    assert dr.is_bounce(_msg(**{"from": sender, "subject": "Re: your note"})) is True


@pytest.mark.parametrize("subject", [
    "Undelivered Mail Returned to Sender",
    "Delivery Status Notification (Failure)",
    "Mail delivery failed: returning message to sender",
    "Undeliverable: quick q about the role",
    "Failure notice",
])
def test_bounce_subjects_are_recognised_whatever_the_MTA_calls_them(subject):
    assert dr.is_bounce(_msg(**{"from": "someone@x.com", "subject": subject})) is True


def test_an_autoresponder_is_caught_by_its_header():
    """A vacation autoresponder can have a perfectly normal subject — Auto-Submitted is the
    only signal that catches it."""
    assert dr.is_bounce(_msg(auto_submitted="auto-replied",
                             **{"subject": "Re: quick q about the role"})) is True
    assert dr.is_bounce(_msg(auto_submitted="no")) is False


def test_a_genuine_human_reply_is_not_mistaken_for_a_bounce():
    """The other direction, and the one that matters most — over-filtering would throw away
    exactly the signal this whole feature exists to find."""
    real = _msg(**{"from": "Victoria Shearer <victoria.shearer@writer.com>",
                   "subject": "Re: quick q about the Writer role"})
    assert dr.is_bounce(real) is False
    assert len(dr.replies_in([real], [_contact()], ME)) == 1


def test_a_bounce_is_reported_separately_rather_than_silently_dropped():
    """An address that bounces will bounce for every follow-up too. Silently ignoring it is how
    outreach to a company fails for weeks without anyone noticing."""
    hits = dr.bounces_in([_bounce()], [_contact()], ME)
    assert len(hits) == 1 and hits[0]["contact"]["id"] == "c1"


def test_our_own_sent_mail_is_not_reported_as_a_bounce_either():
    assert dr.bounces_in([_bounce(labelIds=["SENT"])], [_contact()], ME) == []


def test_marking_a_bounce_stops_the_ladder_WITHOUT_claiming_a_reply(db):
    from applypilot.networking import replies

    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Ali",
                                "email": "ali@affirm.com", "sent_message_id": "m1"}, db)
    replies.mark_bounced({"id": cid, "job_url": "http://j/1", "full_name": "Ali",
                          "email": "ali@affirm.com"}, "2026-07-16T10:00:00+00:00", db)

    c = store.get_contact(cid, db)
    assert not c["replied_at"], "a bounce was recorded as a reply"
    assert c["email_status"] == "bounced"
    row = db.execute("SELECT status FROM sequences WHERE contact_id=? AND channel='email'",
                     (cid,)).fetchone()
    assert dict(row)["status"] == "stopped", "the ladder must stop, but not as 'replied'"


def test_a_bounce_is_visible_in_the_activity_log(db):
    from applypilot.networking import replies

    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Ali",
                                "email": "ali@affirm.com", "sent_message_id": "m1"}, db)
    replies.mark_bounced({"id": cid, "job_url": "http://j/1", "full_name": "Ali",
                          "email": "ali@affirm.com"}, "2026-07-16T10:00:00+00:00", db)
    blob = " ".join(e["detail"] or "" for e in database.get_job_events("http://j/1", conn=db))
    assert "BOUNCED" in blob and "ali@affirm.com" in blob


def test_a_known_bounce_is_not_polled_again(db):
    """Idempotence. A bounce is terminal — that mail will never arrive — so a bounced contact
    must leave the polling pool. Left in, every poll re-detected the same failure and appended
    another log line: `applypilot tick` running hourly produced ELEVEN identical BOUNCED
    entries for one address in an afternoon."""
    from applypilot.networking import replies

    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Ali",
                                "email": "ali@affirm.com", "sent_message_id": "m1"}, db)
    assert cid in [c["id"] for c in store.contacts_awaiting_reply(db)]

    replies.mark_bounced({"id": cid, "job_url": "http://j/1", "full_name": "Ali",
                          "email": "ali@affirm.com"}, "2026-07-16T10:00:00+00:00", db)
    assert cid not in [c["id"] for c in store.contacts_awaiting_reply(db)], \
        "a bounced address stayed in the pool and will be re-logged on every poll"


def test_polling_twice_does_not_duplicate_anything(db, monkeypatch):
    """The acceptance criterion for an unattended schedule: running it twice in a row produces
    no duplicate work and no duplicate log entries."""
    from applypilot.networking import gmail_read, replies

    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Ali",
                                "email": "ali@affirm.com", "sent_message_id": "m1",
                                "thread_id": "t1"}, db)
    monkeypatch.setattr(gmail_read, "available", lambda: (True, "ok"))
    monkeypatch.setattr(gmail_read, "_service", lambda: object())
    monkeypatch.setattr(gmail_read, "current_history_id", lambda s=None: "1")
    monkeypatch.setattr(gmail_read, "threads_with_activity", lambda h, s=None: set())
    monkeypatch.setattr(gmail_read, "thread_messages",
                        lambda tid, s=None: [_bounce(thread_id="t1")])
    from applypilot.networking import gmail_oauth
    monkeypatch.setattr(gmail_oauth, "connected_email", lambda: ME)

    replies.poll(db)
    replies.poll(db)
    n = sum(1 for e in database.get_job_events("http://j/1", conn=db)
            if "BOUNCED" in (e["detail"] or ""))
    assert n == 1, f"the same bounce was logged {n} times"
    assert cid  # keep the id referenced for clarity

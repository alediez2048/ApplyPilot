"""CRM-4b — reading what a reply SAYS, behind an explicit opt-in.

Everything else in this system runs on `gmail.metadata`: headers, threads, participants, and
no message body anywhere. 4b needs `gmail.readonly`, which can read **every message in the
mailbox** — a categorically wider grant, and the reason it is opt-in rather than a scope
addition nobody would notice.

So most of what is tested here is the OFF state. A privacy gate that only works when someone
remembers to check it is not a gate, and the failure mode is silent in the worst direction:
storing content nobody agreed to hand over.
"""

from __future__ import annotations

import pytest

import applypilot.database as database
from applypilot.domain import intent
from applypilot.networking import messages as msg_store, store

ME = "me@example.com"


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


# ── the scope is opt-in, and cannot be requested by accident ─────────────────────────────

def test_the_ordinary_connect_flow_never_asks_for_readonly():
    """The one that must never regress. A future scope addition must not drag this along."""
    from applypilot.networking import gmail_oauth
    assert gmail_oauth.CONTENT_SCOPE not in gmail_oauth.SCOPES
    assert gmail_oauth.CONTENT_SCOPE.endswith("gmail.readonly")
    assert gmail_oauth.READ_SCOPE.endswith("gmail.metadata"), "the default read scope widened"


def test_connect_requests_readonly_only_when_asked(monkeypatch, tmp_path):
    from applypilot.networking import gmail_oauth

    asked = {}

    class _Flow:
        @staticmethod
        def from_client_secrets_file(path, scopes):
            asked["scopes"] = list(scopes)
            return _Flow()

        def run_local_server(self, port=0):
            class _C:
                def to_json(self):
                    return "{}"
            return _C()

    secret = tmp_path / "client.json"
    secret.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gmail_oauth, "CLIENT_SECRET_PATH", secret)
    monkeypatch.setattr(gmail_oauth, "TOKEN_PATH", tmp_path / "token.json")
    monkeypatch.setattr(gmail_oauth, "_libs", lambda: (None, None, _Flow, None))

    gmail_oauth.connect()
    assert gmail_oauth.CONTENT_SCOPE not in asked["scopes"], "default connect asked for readonly"

    gmail_oauth.connect(with_content=True)
    assert gmail_oauth.CONTENT_SCOPE in asked["scopes"]


def test_can_read_content_follows_the_stored_token(monkeypatch):
    from applypilot.networking import gmail_oauth
    monkeypatch.setattr(gmail_oauth, "granted_scopes",
                        lambda: [gmail_oauth.SEND_SCOPE, gmail_oauth.READ_SCOPE])
    assert gmail_oauth.can_read_content() is False
    monkeypatch.setattr(gmail_oauth, "granted_scopes",
                        lambda: [gmail_oauth.SEND_SCOPE, gmail_oauth.CONTENT_SCOPE])
    assert gmail_oauth.can_read_content() is True


# ── nothing is stored without it ─────────────────────────────────────────────────────────

def _thread_msg(snippet=""):
    return {"id": "m1", "thread_id": "t1", "from": "Gina <gina@co.com>", "to": ME,
            "cc": "", "subject": "Re: role", "internalDate": "1780000000000",
            "rfc_message_id": "<a@co>", "snippet": snippet, "labelIds": [], "auto_submitted": ""}


def test_no_snippet_is_stored_when_the_scope_is_off(db, monkeypatch):
    """The whole gate. Gmail returns an empty snippet under metadata anyway — but relying on
    the API to withhold it is trusting someone else's default with the operator's mail."""
    from applypilot.networking import gmail_read, replies

    monkeypatch.setattr(gmail_read, "can_read_content", lambda: (False, "off"))
    contact = {"id": "c1", "job_url": "http://j/1", "thread_id": "t1", "email": "gina@co.com"}
    replies._sync_thread(contact, [_thread_msg("They said something private")], ME, db)

    rows = msg_store.thread_for_contact("c1", db)
    assert rows and all(not (r.get("snippet") or "") for r in rows), (
        "a message snippet was stored without the content scope")


def test_the_automatic_poll_stores_no_text_EVEN_WITH_the_scope(db, monkeypatch):
    """The whole point of the per-conversation model.

    The OAuth grant is all-or-nothing — Google has no per-thread scope — so the narrowing cannot
    live in what we are ALLOWED to read. It has to live in what we ever DO read. The poller and
    `tick` therefore store no message text at all, whatever the token permits; content arrives
    only when the operator names one conversation.
    """
    from applypilot.networking import gmail_read, replies

    monkeypatch.setattr(gmail_read, "can_read_content", lambda: (True, "ok"))
    contact = {"id": "c1", "job_url": "http://j/1", "thread_id": "t1", "email": "gina@co.com"}
    replies._sync_thread(contact, [_thread_msg("Happy to chat — are you free Thursday?")], ME, db)

    rows = msg_store.thread_for_contact("c1", db)
    assert rows, "the thread headers should still be synced"
    assert all(not (r.get("snippet") or "") for r in rows), (
        "the automatic poll stored message text — it must never do that, scope or no scope")


def test_fetching_one_conversation_stores_its_text(db, monkeypatch):
    """...and the explicit path does what the automatic one refuses to."""
    from applypilot.networking import gmail_oauth, gmail_read, replies

    monkeypatch.setattr(gmail_read, "can_read_content", lambda: (True, "ok"))
    monkeypatch.setattr(gmail_read, "thread_messages",
                        lambda tid, service=None: [_thread_msg("Happy to chat Thursday?")])
    monkeypatch.setattr(gmail_oauth, "connected_email", lambda: ME)

    contact = {"id": "c1", "job_url": "http://j/1", "thread_id": "t1", "email": "gina@co.com"}
    res = replies.fetch_thread_text(contact, db)
    assert res["ok"] is True and res["stored"] == 1
    assert msg_store.thread_for_contact("c1", db)[0]["snippet"].startswith("Happy to chat")


def test_fetching_refuses_without_the_scope_and_says_why(db, monkeypatch):
    from applypilot.networking import gmail_read, replies

    monkeypatch.setattr(gmail_read, "can_read_content",
                        lambda: (False, "reply content is off — enable with --with-content"))
    res = replies.fetch_thread_text({"id": "c1", "job_url": "http://j/1", "thread_id": "t1"}, db)
    assert res["ok"] is False and "--with-content" in res["message"]
    assert res["stored"] == 0


def test_fetching_never_stores_our_own_messages_as_theirs(db, monkeypatch):
    """Our sent text is already ours, and mislabelling it inbound would make the conversation
    claim they wrote something we did."""
    from applypilot.networking import gmail_oauth, gmail_read, replies

    ours = dict(_thread_msg("what I wrote to them"), **{"from": ME})
    monkeypatch.setattr(gmail_read, "can_read_content", lambda: (True, "ok"))
    monkeypatch.setattr(gmail_read, "thread_messages", lambda tid, service=None: [ours])
    monkeypatch.setattr(gmail_oauth, "connected_email", lambda: ME)

    res = replies.fetch_thread_text({"id": "c1", "job_url": "http://j/1", "thread_id": "t1"}, db)
    assert res["ok"] is False and res["stored"] == 0
    assert msg_store.thread_for_contact("c1", db) == []


def test_a_later_poll_does_not_erase_what_was_fetched(db, monkeypatch):
    """The poll writes an empty snippet on every row. `upsert_messages` preserves an existing
    one, and that is now load-bearing rather than defensive: without it, the automatic path
    would delete on every tick exactly what the operator explicitly asked for."""
    from applypilot.networking import gmail_oauth, gmail_read, replies

    monkeypatch.setattr(gmail_read, "can_read_content", lambda: (True, "ok"))
    monkeypatch.setattr(gmail_read, "thread_messages",
                        lambda tid, service=None: [_thread_msg("Happy to chat Thursday?")])
    monkeypatch.setattr(gmail_oauth, "connected_email", lambda: ME)
    contact = {"id": "c1", "job_url": "http://j/1", "thread_id": "t1", "email": "gina@co.com"}
    replies.fetch_thread_text(contact, db)

    for _ in range(3):
        replies._sync_thread(contact, [_thread_msg("")], ME, db)
    assert msg_store.thread_for_contact("c1", db)[0]["snippet"].startswith("Happy to chat"), (
        "an automatic poll erased text the operator had explicitly fetched")


def test_our_own_sent_text_is_never_stored_back(db, monkeypatch):
    """Outbound text is already ours; copying it into the thread store doubles the content in
    the database for no gain."""
    from applypilot.networking import gmail_read, replies

    monkeypatch.setattr(gmail_read, "can_read_content", lambda: (True, "ok"))
    out = dict(_thread_msg("what I wrote to them"), **{"from": ME, "to": "gina@co.com"})
    contact = {"id": "c1", "job_url": "http://j/1", "thread_id": "t1", "email": "gina@co.com"}
    replies._sync_thread(contact, [out], ME, db)

    rows = msg_store.thread_for_contact("c1", db)
    assert rows[0]["direction"] == "out" and not rows[0]["snippet"]


def test_pulling_all_gmail_finds_threads_applypilot_never_sent(db, monkeypatch):
    """The CRM's memory used to stop at its own outbox.

    `thread_id` is captured at SEND time and everything downstream looks a thread up by that id,
    so a conversation the other side began, an email sent straight from Gmail, or one where they
    merely CC'd you was invisible. Searching by ADDRESS is what fixes it — and search needs
    `gmail.readonly`, because `gmail.metadata` refuses `q=` outright.
    """
    from applypilot.networking import gmail_oauth, gmail_read, replies

    seen = {}

    def fake_search(query, limit=25, service=None):
        seen["query"] = query
        return ["thread-we-never-sent"]

    other = {"id": "m9", "thread_id": "thread-we-never-sent",
             "from": "Sarah <sarah@writer.com>", "to": ME, "cc": "",
             "subject": "intro", "internalDate": "1780000000000",
             "rfc_message_id": "<z@w>", "snippet": "Looping you in with David.",
             "labelIds": [], "auto_submitted": ""}

    monkeypatch.setattr(gmail_read, "can_read_content", lambda: (True, "ok"))
    monkeypatch.setattr(gmail_read, "search_threads", fake_search)
    monkeypatch.setattr(gmail_read, "thread_messages", lambda tid, service=None: [other])
    monkeypatch.setattr(gmail_oauth, "connected_email", lambda: ME)

    contact = {"id": "c1", "job_url": "http://j/1", "email": "sarah@writer.com",
               "full_name": "Sarah"}
    res = replies.sync_all_with(contact, db)
    assert res["ok"] is True and res["threads"] == 1 and res["messages"] == 1

    # cc: matters — the Writer case was a thread where the contact only CC'd us.
    for part in ("from:sarah@writer.com", "to:sarah@writer.com", "cc:sarah@writer.com"):
        assert part in seen["query"], f"the search would miss {part}"

    stored = msg_store.thread_for_contact("c1", db)
    assert stored[0]["direction"] == "in"
    assert stored[0]["snippet"].startswith("Looping you in")


def test_syncing_one_contact_does_not_steal_a_shared_thread_from_another(db, monkeypatch):
    """Measured on live data before it was fixed: clicking "Pull all Gmail" on David reassigned
    all three Writer messages to him and left Victoria's conversation EMPTY (3 → 0, one click).

    `message_id` was the primary key, so INSERT OR REPLACE moved ownership. But one Gmail
    message legitimately belongs to several contacts — Victoria and David are both on that
    thread, and each should keep their own view of it.
    """
    from applypilot.networking import gmail_oauth, gmail_read, replies

    shared = {"id": "shared1", "thread_id": "t-shared", "from": "victoria@w.com",
              "to": ME, "cc": "David <david@w.com>", "subject": "intro",
              "internalDate": "1780000000000", "rfc_message_id": "<s@w>",
              "snippet": "Looping in David.", "labelIds": [], "auto_submitted": ""}
    monkeypatch.setattr(gmail_read, "can_read_content", lambda: (True, "ok"))
    monkeypatch.setattr(gmail_read, "search_threads", lambda *a, **k: ["t-shared"])
    monkeypatch.setattr(gmail_read, "thread_messages", lambda tid, service=None: [shared])
    monkeypatch.setattr(gmail_oauth, "connected_email", lambda: ME)

    victoria = {"id": "v1", "job_url": "http://j/1", "email": "victoria@w.com"}
    david = {"id": "d1", "job_url": "http://j/1", "email": "david@w.com"}

    replies.sync_all_with(victoria, db)
    assert len(msg_store.thread_for_contact("v1", db)) == 1

    replies.sync_all_with(david, db)
    assert len(msg_store.thread_for_contact("d1", db)) == 1, "David did not get the thread"
    assert len(msg_store.thread_for_contact("v1", db)) == 1, (
        "syncing David emptied Victoria's conversation — the message was reassigned")

    # ...and re-syncing either one is still a no-op for both.
    assert replies.sync_all_with(victoria, db)["messages"] == 0
    assert len(msg_store.thread_for_contact("d1", db)) == 1


def test_the_messages_key_is_per_contact_not_per_message(db):
    """The schema itself, so nobody restores a message_id primary key by hand."""
    pk = [r[1] for r in db.execute("PRAGMA table_info(messages)").fetchall() if r[5]]
    assert sorted(pk) == ["contact_id", "message_id"], f"primary key is {pk}"


def test_pulling_all_gmail_needs_the_content_scope_and_an_address(db, monkeypatch):
    from applypilot.networking import gmail_read, replies

    monkeypatch.setattr(gmail_read, "can_read_content", lambda: (False, "content is off"))
    assert replies.sync_all_with({"id": "c1", "email": "a@b.com"}, db)["ok"] is False

    monkeypatch.setattr(gmail_read, "can_read_content", lambda: (True, "ok"))
    res = replies.sync_all_with({"id": "c1", "email": ""}, db)
    assert res["ok"] is False and "no email address" in res["message"]


def test_pulling_all_gmail_does_not_overwrite_what_we_sent(db, monkeypatch):
    """Our own text is recorded at send time, in full. Re-importing a truncated Gmail snippet
    over it would be a downgrade — `upsert_messages` keeps the existing one when handed ""."""
    from applypilot.networking import gmail_oauth, gmail_read, replies

    contact = {"id": "c1", "job_url": "http://j/1", "email": "s@w.com", "thread_id": "t1"}
    msg_store.record_outbound(contact, {"id": "mine", "thread_id": "t1",
                                        "rfc_message_id": "<a@us>", "from_addr": ME},
                              "s@w.com", [], "Re: x", db, body="THE FULL TEXT I WROTE")
    assert msg_store.thread_for_contact("c1", db)[0]["snippet"] == "THE FULL TEXT I WROTE"

    ours = {"id": "mine", "thread_id": "t1", "from": ME, "to": "s@w.com", "cc": "",
            "subject": "Re: x", "internalDate": "1780000000000", "rfc_message_id": "<a@us>",
            "snippet": "THE FULL TEXT I WR", "labelIds": [], "auto_submitted": ""}
    monkeypatch.setattr(gmail_read, "can_read_content", lambda: (True, "ok"))
    monkeypatch.setattr(gmail_read, "search_threads", lambda *a, **k: ["t1"])
    monkeypatch.setattr(gmail_read, "thread_messages", lambda tid, service=None: [ours])
    monkeypatch.setattr(gmail_oauth, "connected_email", lambda: ME)

    replies.sync_all_with(contact, db)
    assert msg_store.thread_for_contact("c1", db)[0]["snippet"] == "THE FULL TEXT I WROTE", (
        "a truncated Gmail snippet overwrote the full text we had sent")


def test_a_message_sent_from_gmail_directly_does_get_its_text(db, monkeypatch):
    """The other half of that rule. An outbound message ApplyPilot never sent has no stored
    text at all, and would otherwise render as a permanently blank row — which is exactly what
    "Sent from ApplyPilot." looked like on a reply the operator had actually written."""
    from applypilot.networking import gmail_oauth, gmail_read, replies

    sent_elsewhere = {"id": "from-gmail", "thread_id": "t2", "from": ME, "to": "s@w.com",
                      "cc": "", "subject": "hi", "internalDate": "1780000000000",
                      "rfc_message_id": "<b@us>", "snippet": "I typed this in Gmail.",
                      "labelIds": [], "auto_submitted": ""}
    monkeypatch.setattr(gmail_read, "can_read_content", lambda: (True, "ok"))
    monkeypatch.setattr(gmail_read, "search_threads", lambda *a, **k: ["t2"])
    monkeypatch.setattr(gmail_read, "thread_messages", lambda tid, service=None: [sent_elsewhere])
    monkeypatch.setattr(gmail_oauth, "connected_email", lambda: ME)

    replies.sync_all_with({"id": "c2", "job_url": "http://j/1", "email": "s@w.com"}, db)
    rows = msg_store.thread_for_contact("c2", db)
    assert rows and rows[0]["direction"] == "out"
    assert rows[0]["snippet"] == "I typed this in Gmail."


def test_our_sent_reply_shows_what_we_actually_wrote(db):
    """The thread showed "Sent from ApplyPilot." where the reply just written should be, which
    reads as the message having been lost. It is our own text — no scope question at all."""
    contact = {"id": "c1", "job_url": "http://j/1", "thread_id": "t1"}
    msg_store.record_outbound(contact, {"id": "s1", "thread_id": "t1",
                                        "rfc_message_id": "<n@us>", "from_addr": ME},
                              "g@co.com", [], "Re: role", db,
                              body="Thursday works. The job ID is JR349466.")
    assert msg_store.thread_for_contact("c1", db)[0]["snippet"].startswith("Thursday works")


def test_the_quoted_original_is_trimmed_off_a_reply(db):
    """Gmail's snippet runs straight through the quote header.

    Gina's real reply was 194 chars and ended `...job ID you applied for? On Thu, Jul 30, 2026
    at 12:57 PM <` — the last fifth being the beginning of Jorge's OWN email quoted back. Fed to
    the drafter, that is our text arriving as something she said.
    """
    from applypilot.domain import conversations as cv

    real = ("Hi Jorge great to hear from you!! I do not have any insight into these roles but I "
            "am happy to pass along your info! Do you have the job ID you applied for? "
            "On Thu, Jul 30, 2026 at 12:57 PM <")
    out = cv.strip_quoted_tail(real)
    assert out.endswith("job ID you applied for?")
    assert "On Thu" not in out

    for variant in ("Sure, sounds good.\nOn Mon, Jul 7, 2026 at 9:01 AM someone wrote:\nblah",
                    "Yes please.\n-------- Original Message --------\nold stuff",
                    "Works for me.\nFrom: Jorge\nquoted"):
        assert cv.strip_quoted_tail(variant).count("\n") == 0, variant

    # Conservative: never cut everything, and never touch a reply with no quote in it.
    assert cv.strip_quoted_tail("On Thursday I am free") == "On Thursday I am free"
    assert cv.strip_quoted_tail("Happy to chat Thursday.") == "Happy to chat Thursday."
    assert cv.strip_quoted_tail("") == ""
    assert cv.strip_quoted_tail(None) == ""


def test_pasted_text_is_bounded_too(db):
    """A larger cap than the auto path, for a different reason rather than a looser one —
    nothing was harvested, the operator chose to hand over one message. Still bounded, because
    "paste your whole inbox into the CRM" is not a feature either."""
    msg_store.upsert_messages([{"message_id": "m1", "thread_id": "t", "contact_id": "c1",
                                "job_url": "http://j/1", "direction": "in",
                                "from_addr": "g@co.com", "subject": "s",
                                "sent_at": "2026-07-30T10:00:00+00:00"}], db)
    assert msg_store.set_reply_text("c1", "y" * 50_000, db) is True
    assert len(msg_store.thread_for_contact("c1", db)[0]["snippet"]) == msg_store.PASTED_MAX
    assert msg_store.PASTED_MAX <= 4000, "at some size this stops being a message and becomes a mailbox"


def test_pasting_onto_a_thread_with_no_inbound_message_is_a_no_op(db):
    """Nothing to attach it to. Reported rather than silently invented as a new row — a
    fabricated inbound message would make the conversation claim they wrote when they did not."""
    msg_store.upsert_messages([{"message_id": "m1", "thread_id": "t", "contact_id": "c1",
                                "job_url": "http://j/1", "direction": "out",
                                "from_addr": ME, "subject": "s",
                                "sent_at": "2026-07-30T10:00:00+00:00"}], db)
    assert msg_store.set_reply_text("c1", "they said this", db) is False
    assert msg_store.thread_for_contact("c1", db)[0]["snippet"] == ""


def test_a_long_snippet_is_truncated_at_the_write(db):
    """Capped in the store, not the caller — a cap a new caller can forget is not a cap."""
    msg_store.upsert_messages([{"message_id": "m1", "thread_id": "t", "contact_id": "c1",
                                "job_url": "http://j/1", "direction": "in",
                                "from_addr": "g@co.com", "subject": "s",
                                "sent_at": "2026-07-30T10:00:00+00:00",
                                "snippet": "x" * 5000}], db)
    stored = msg_store.thread_for_contact("c1", db)[0]["snippet"]
    assert len(stored) == msg_store.SNIPPET_MAX <= 400


def test_revoking_the_scope_does_not_erase_what_was_already_stored(db, monkeypatch):
    """"Degrades cleanly" only means something if nothing is destroyed on the way down.

    `upsert_messages` is INSERT OR REPLACE, so a re-sync carrying no snippet would blank one
    already there — and `tick` re-syncs every open thread hourly, forever.
    """
    from applypilot.networking import gmail_oauth, gmail_read, replies

    contact = {"id": "c1", "job_url": "http://j/1", "thread_id": "t1", "email": "gina@co.com"}
    # Seeded through the EXPLICIT fetch, because that is now the only way text ever arrives —
    # `_sync_thread` stores none, scope or no scope.
    monkeypatch.setattr(gmail_read, "can_read_content", lambda: (True, "ok"))
    monkeypatch.setattr(gmail_read, "thread_messages",
                        lambda tid, service=None: [_thread_msg("Happy to chat Thursday")])
    monkeypatch.setattr(gmail_oauth, "connected_email", lambda: ME)
    replies.fetch_thread_text(contact, db)
    assert msg_store.thread_for_contact("c1", db)[0]["snippet"]

    monkeypatch.setattr(gmail_read, "can_read_content", lambda: (False, "revoked"))
    for _ in range(3):                       # three more hourly ticks
        replies._sync_thread(contact, [_thread_msg("")], ME, db)
    assert msg_store.thread_for_contact("c1", db)[0]["snippet"] == "Happy to chat Thursday", (
        "a stored snippet was erased by a poll made after the scope was revoked")


def test_there_is_still_no_body_column(db):
    """4b adds `snippet` and nothing else. A full body must remain impossible to store."""
    cols = {r[1] for r in db.execute("PRAGMA table_info(messages)").fetchall()}
    assert "snippet" in cols
    assert not (cols & {"body", "content", "text", "html", "payload", "raw"})


# ── intent ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("Unfortunately we've decided to move forward with another candidate.", intent.REJECTION),
    ("Hi! We're not hiring right now but will circle back next quarter.", intent.NOT_NOW),
    ("Happy to chat — are you free Thursday afternoon?", intent.INTERESTED),
    ("Looping in David who leads the team, he's the best person to talk to.", intent.INTRODUCTION),
    ("I am currently out of office and will have limited access to email.", intent.OUT_OF_OFFICE),
    ("What kind of infrastructure experience do you have?", intent.QUESTION),
])
def test_it_recognises_the_replies_that_change_what_you_do(text, expected):
    assert intent.classify(text) == expected


def test_an_out_of_office_is_not_read_as_a_rejection_or_interest():
    """An auto-reply can contain almost any phrase and mean none of it — 'unfortunately I am
    out of office' is not a no, and 'happy to chat when I return' is not a yes."""
    assert intent.classify("Unfortunately I am out of office until Monday.") == intent.OUT_OF_OFFICE
    assert intent.classify("Out of office — happy to chat when I'm back.") == intent.OUT_OF_OFFICE


def test_a_rejection_beats_a_question_in_the_same_message():
    """"Unfortunately we went another way. Would you like feedback?" is a rejection: that is the
    stronger fact about what to do next."""
    assert intent.classify(
        "Unfortunately we went another way. Would you like some feedback?") == intent.REJECTION


def test_it_says_unknown_rather_than_guessing():
    """A confident wrong label is worse than none — 'interested' on a rejection would have the
    operator write an eager reply to somebody who already said no."""
    assert intent.classify("Thanks for your note.") == intent.UNKNOWN
    assert intent.classify("") == intent.UNKNOWN
    assert intent.classify(None) == intent.UNKNOWN


def test_a_lone_question_mark_is_not_a_question():
    """A "?" alone is not somebody asking you something — it is a rhetorical "Right?" or a
    signature line. Requires an interrogative opener near it.

    NOTE the strings. "Sounds good?" was the obvious example and is USELESS here: it matches the
    `interested` pattern and returns before the question check ever runs, so the assertion passed
    no matter what `_ASKS` did. Mutation testing caught that — replacing the whole regex with a
    bare `\\?` left every test green (§Lessons 13).
    """
    for rhetorical in ("Right?", "Received?", "Ok?"):
        assert intent.classify(rhetorical) == intent.UNKNOWN, (
            f"{rhetorical!r} was read as somebody asking a question")
    # ...and a real question still is one, so this is not just asserting "never QUESTION".
    assert intent.classify("How many years of Python do you have?") == intent.QUESTION


def test_every_intent_offers_something_to_do_except_unknown():
    """A label that does not change the offered action is decoration."""
    for label in (intent.INTRODUCTION, intent.REJECTION, intent.NOT_NOW, intent.INTERESTED,
                  intent.QUESTION, intent.OUT_OF_OFFICE):
        s = intent.suggestion(label)
        assert s["label"] and s["action"], f"{label} suggests nothing"
    assert intent.suggestion(intent.UNKNOWN) == {"intent": "unknown", "label": "", "action": ""}


# ── drafting ─────────────────────────────────────────────────────────────────────────────

def test_drafting_refuses_rather_than_writing_a_generic_follow_up(db, monkeypatch):
    """A "contextual" reply written without the context is a generic follow-up wearing a Re:
    subject line — and it would look like a working feature until somebody read it.

    It refuses by naming BOTH ways out, because a dead end that only mentions the OAuth scope
    reads as "grant this or the feature is off" when pasting works right now.
    """
    from applypilot import web_dashboard as wd
    from applypilot.networking import gmail_read

    monkeypatch.setattr(wd, "get_connection", lambda *a, **k: db)
    monkeypatch.setattr(gmail_read, "can_read_content", lambda: (False, "reply content is off"))
    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Gina",
                                "email": "gina@co.com", "sent_message_id": "m1"}, db)
    msg_store.upsert_messages([{"message_id": "m1", "thread_id": "t", "contact_id": cid,
                                "job_url": "http://j/1", "direction": "in",
                                "from_addr": "gina@co.com", "subject": "Re: role",
                                "sent_at": "2026-07-30T10:00:00+00:00"}], db)
    res = wd._draft_reply({"contact_id": cid})
    assert res["ok"] is False
    assert "aste" in res["message"], "the refusal does not mention the way that works today"
    assert "--with-content" in res["message"]


def test_the_drafter_refuses_a_thread_with_no_readable_reply():
    from applypilot.networking import outreach
    thread = [{"direction": "in", "from_addr": "g@co.com", "snippet": "", "subject": "Re: x"}]
    with pytest.raises(ValueError, match="no reply text"):
        outreach.draft_reply({}, {"title": "Eng"}, {"full_name": "Gina"}, thread=thread)

    with pytest.raises(ValueError, match="nothing to reply to"):
        outreach.draft_reply({}, {"title": "Eng"}, {"full_name": "Gina"},
                             thread=[{"direction": "out", "snippet": "hi"}])


def test_a_drafted_reply_is_given_what_they_said_and_no_intro_deck(monkeypatch):
    """The deck belongs to cold outreach and follow-ups, whose job is to EARN a reply. Bolting
    it onto an answer in a live conversation is what makes a real exchange read like a sequence.
    """
    from applypilot.networking import outreach

    seen = {}

    class _Client:
        def chat(self, messages, **kw):
            seen["prompt"] = messages[-1]["content"]
            seen["system"] = messages[0]["content"]
            # No em dash: `sanitize_text` rewrites those on every drafted message, so asserting
            # on one would test the sanitiser rather than the drafter.
            return '{"subject": "Re: role", "body": "Thursday works. 2pm?"}'

    monkeypatch.setattr(outreach, "get_client", lambda *a, **k: _Client())
    thread = [{"direction": "out", "snippet": "", "subject": "role"},
              {"direction": "in", "from_addr": "gina@co.com", "from_name": "Gina Johnson",
               "cc_addrs": ["David Loveless <david@co.com>"], "subject": "Re: role",
               "snippet": "Happy to chat — are you free Thursday?"}]
    out = outreach.draft_reply({"name": "Alejandro"}, {"title": "Engineer"},
                               {"full_name": "Gina", "company": "Co"}, thread=thread)

    assert out["body"] == "Thursday works. 2pm?"
    assert "Happy to chat" in seen["prompt"], "the drafter was not told what they said"
    assert "David Loveless" in seen["prompt"], "the drafter does not know who else is on the thread"
    assert "intro" not in out["body"].lower()
    assert "jorgealejandrodiez.com" not in out["body"]


def test_a_pasted_reply_works_with_no_gmail_scope_at_all(db, monkeypatch):
    """The operator has the email open. Pasting it is a deliberate hand-over of ONE message —
    categorically different from granting read access to the whole mailbox, and it means the
    feature works on a default install instead of being gated behind a privacy decision."""
    from applypilot import web_dashboard as wd
    from applypilot.networking import gmail_read, outreach

    monkeypatch.setattr(wd, "get_connection", lambda *a, **k: db)
    monkeypatch.setattr(gmail_read, "can_read_content", lambda: (False, "off"))
    seen = {}

    class _Client:
        def chat(self, messages, **kw):
            seen["prompt"] = messages[-1]["content"]
            return '{"subject": "Re: role", "body": "Thursday works."}'

    monkeypatch.setattr(outreach, "get_client", lambda *a, **k: _Client())

    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Gina",
                                "email": "gina@co.com", "sent_message_id": "m1"}, db)
    msg_store.upsert_messages([{"message_id": "m1", "thread_id": "t", "contact_id": cid,
                                "job_url": "http://j/1", "direction": "in",
                                "from_addr": "gina@co.com", "from_name": "Gina",
                                "subject": "Re: role",
                                "sent_at": "2026-07-31T14:11:00+00:00"}], db)

    res = wd._draft_reply({"contact_id": cid,
                           "their_reply": "Thanks! Are you free Thursday to chat?"})
    assert res["ok"] is True, res.get("message")
    assert "Thursday" in seen["prompt"]
    # ...and it is PERSISTED, or the sequence has a hole exactly where the interesting part is
    # and the operator re-pastes it on every redraft.
    assert msg_store.thread_for_contact(cid, db)[0]["snippet"].startswith("Thanks!")


def test_the_vibe_knob_reaches_the_prompt(db, monkeypatch):
    """Same free-text control as cold outreach, resolved through the same `_resolve_style`, so
    OUTREACH_STYLE and the profile default apply here too — one tone control, not a second one
    that drifts away from the first."""
    from applypilot import web_dashboard as wd
    from applypilot.networking import outreach

    monkeypatch.setattr(wd, "get_connection", lambda *a, **k: db)
    seen = {}

    class _Client:
        def chat(self, messages, **kw):
            seen["prompt"] = messages[-1]["content"]
            return '{"subject": "Re: role", "body": "yep"}'

    monkeypatch.setattr(outreach, "get_client", lambda *a, **k: _Client())
    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Gina",
                                "email": "gina@co.com", "sent_message_id": "m1"}, db)
    msg_store.upsert_messages([{"message_id": "m1", "thread_id": "t", "contact_id": cid,
                                "job_url": "http://j/1", "direction": "in",
                                "from_addr": "gina@co.com", "subject": "Re: role",
                                "sent_at": "2026-07-31T14:11:00+00:00",
                                "snippet": "Are you free Thursday?"}], db)

    wd._draft_reply({"contact_id": cid, "style": "much warmer, mention I'm a Longhorn"})
    assert "Longhorn" in seen["prompt"] and "STYLE DIRECTION" in seen["prompt"]


def test_the_draft_sees_the_whole_sequence_not_just_the_reply(db, monkeypatch):
    """The first email is on `contacts`, the follow-ups are in `touches`, the reply is in
    `messages`. A draft written from any one of them repeats what the other two already said —
    which is precisely how a reply starts sounding automated."""
    from applypilot import web_dashboard as wd
    from applypilot.networking import outreach, touches

    monkeypatch.setattr(wd, "get_connection", lambda *a, **k: db)
    seen = {}

    class _Client:
        def chat(self, messages, **kw):
            seen["prompt"] = messages[-1]["content"]
            return '{"subject": "Re: role", "body": "yep"}'

    monkeypatch.setattr(outreach, "get_client", lambda *a, **k: _Client())
    touches.init_touches(db)
    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Gina",
                                "email": "gina@co.com", "sent_message_id": "m1",
                                "outreach_subject": "quick q about the FDE role",
                                "outreach_message": "MY-FIRST-EMAIL about forward deployed work",
                                "submitted_at": "2026-07-20T10:00:00+00:00"}, db)
    touches.set_draft(cid, "email", "Re: quick q", "MY-FOLLOWUP-ONE nudging politely", db)
    touches.mark_sent(cid, "email", db) if hasattr(touches, "mark_sent") else None
    db.execute("UPDATE touches SET sent_at = ? WHERE contact_id = ?",
               ("2026-07-24T10:00:00+00:00", cid))
    db.commit()
    msg_store.upsert_messages([{"message_id": "m2", "thread_id": "t", "contact_id": cid,
                                "job_url": "http://j/1", "direction": "in",
                                "from_addr": "gina@co.com", "subject": "Re: quick q",
                                "sent_at": "2026-07-31T14:11:00+00:00",
                                "snippet": "THEIR-REPLY: are you free Thursday?"}], db)

    wd._draft_reply({"contact_id": cid})
    p = seen["prompt"]
    assert "MY-FIRST-EMAIL" in p, "the drafter never saw the original email"
    assert "MY-FOLLOWUP-ONE" in p, "the drafter never saw the follow-up already sent"
    assert "THEIR-REPLY" in p
    assert "Do not repeat" in p, "nothing tells the model that all of it is already in the inbox"
    assert p.index("MY-FIRST-EMAIL") < p.index("MY-FOLLOWUP-ONE") < p.index("THEIR-REPLY"), (
        "the conversation was not given in order")


def test_the_draft_is_given_the_senders_REAL_background(db, monkeypatch):
    """The highest-stakes fabrication risk in the product.

    A recruiter asks "do you have experience deploying customer-facing LLM systems?" — with no
    grounded background in the prompt the model answers with a confident, invented yes, in a
    live conversation, to the one person positioned to check it. Caught on the first real draft
    against Gina's actual reply (§Lessons 9, stakes raised).
    """
    from applypilot import web_dashboard as wd
    from applypilot.networking import outreach

    monkeypatch.setattr(wd, "get_connection", lambda *a, **k: db)
    seen = {}

    class _Client:
        def chat(self, messages, **kw):
            seen["prompt"] = messages[-1]["content"]
            seen["system"] = messages[0]["content"]
            return '{"subject": "Re: role", "body": "yep"}'

    monkeypatch.setattr(outreach, "get_client", lambda *a, **k: _Client())
    monkeypatch.setattr(wd, "_jobs", wd._jobs)
    monkeypatch.setattr("applypilot.config.load_profile", lambda *a, **k: {
        "personal": {"full_name": "Alejandro Diez"},
        "linkedin": {"headline": "REAL-HEADLINE", "about": "REAL-ABOUT-TEXT",
                     "roles": [{"title": "REAL-ROLE", "company": "T-Mobile", "dates": "2019-2024"}]},
    })

    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Gina",
                                "email": "gina@co.com", "sent_message_id": "m1"}, db)
    msg_store.upsert_messages([{"message_id": "m1", "thread_id": "t", "contact_id": cid,
                                "job_url": "http://j/1", "direction": "in",
                                "from_addr": "gina@co.com", "subject": "Re: role",
                                "sent_at": "2026-07-31T14:11:00+00:00",
                                "snippet": "Do you have experience deploying LLM systems?"}], db)

    wd._draft_reply({"contact_id": cid})
    p, sysmsg = seen["prompt"], seen["system"]
    assert "REAL-ABOUT-TEXT" in p and "REAL-ROLE" in p, (
        "the drafter was asked about the sender's experience without being told what it is")
    assert "ABOUT YOU" in p
    assert "manufacture a yes" in sysmsg or "do NOT manufacture" in sysmsg


def test_cold_outreach_and_replies_draw_on_the_same_background():
    """One source. Two would drift, and the copy would start contradicting itself between the
    first email and the answer to its reply."""
    from applypilot.networking import outreach

    profile = {"personal": {"full_name": "A B"},
               "linkedin": {"about": "ABOUT", "roles": [{"title": "T", "company": "C"}]}}
    bits = outreach.sender_background(profile)
    assert any("ABOUT" in b for b in bits)
    assert bits == outreach.sender_background(profile), "not deterministic"


def test_the_real_requisition_id_reaches_the_drafter(db, monkeypatch):
    """Caught on the FIRST live draft against Gina's real reply.

    She asked "do you have the job ID you applied for?" and the draft answered **7894521** —
    a number that exists nowhere — while the real `JR349466` sat in the job URL the drafter had
    never been given. A recruiter checks a req ID in five seconds; a wrong one is worse than no
    answer, and it is the sender's credibility that pays.
    """
    from applypilot import web_dashboard as wd
    from applypilot.networking import outreach

    monkeypatch.setattr(wd, "get_connection", lambda *a, **k: db)
    seen = {}

    class _Client:
        def chat(self, messages, **kw):
            seen["prompt"] = messages[-1]["content"]
            seen["system"] = messages[0]["content"]
            return '{"subject": "Re: role", "body": "ok"}'

    monkeypatch.setattr(outreach, "get_client", lambda *a, **k: _Client())

    url = ("https://salesforce.wd12.myworkdayjobs.com/External_Career_Site/job/"
           "California---San-Francisco/Forward-Deployed-Engineer--All-Levels-_JR349466"
           "?source=LinkedIn_Jobs")
    from applypilot.repo import jobs as _jobs
    _jobs.upsert({"url": url, "title": "Forward Deployed Engineer", "site": "Salesforce",
                  "strategy": "manual"}, db) if hasattr(_jobs, "upsert") else None

    cid = store.upsert_contact({"job_url": url, "full_name": "Gina", "email": "g@co.com",
                                "sent_message_id": "m1"}, db)
    msg_store.upsert_messages([{"message_id": "m1", "thread_id": "t", "contact_id": cid,
                                "job_url": url, "direction": "in", "from_addr": "g@co.com",
                                "subject": "Re: role", "sent_at": "2026-07-31T14:11:00+00:00",
                                "snippet": "Do you have the job ID you applied for?"}], db)

    wd._draft_reply({"contact_id": cid})
    assert "JR349466" in seen["prompt"], "the drafter was asked for a req ID it was never given"
    assert "never state an identifier that is not here" in seen["prompt"]
    assert "NEVER INVENT AN IDENTIFIER" in seen["system"]


def test_job_facts_extracts_real_ids_and_refuses_to_supply_absent_ones():
    from applypilot.networking.outreach import job_facts

    wd_url = ("https://salesforce.wd12.myworkdayjobs.com/External_Career_Site/job/"
              "CA/Forward-Deployed-Engineer_JR349466?source=LinkedIn_Jobs")
    out = job_facts({"url": wd_url, "title": "FDE"})
    assert "JR349466" in out and wd_url in out

    # Greenhouse-style numeric id.
    assert "4683241005" in job_facts({"url": "https://x.com/careers?gh_jid=4683241005"})

    # No identifier available: say so rather than leaving the model to fill the gap.
    bare = job_facts({"url": "https://acme.com/careers/engineer", "title": "Eng"})
    assert "do NOT invent" in bare or "send it across" in bare
    assert "No posting details" in job_facts({})


def test_the_reply_prompt_tells_the_model_to_answer_not_to_pitch(monkeypatch):
    from applypilot.networking import outreach
    system = outreach._REPLY_SYSTEM.lower()
    assert "answer the message" in system
    assert "never re-pitch" in system or "not a follow-up" in system


# ── the payload stays empty without the scope ────────────────────────────────────────────

def test_the_payload_carries_no_reply_text_without_a_snippet():
    from applypilot import web_dashboard as wd
    thread = [{"direction": "in", "from_addr": "g@co.com", "sent_at": "2026-07-30T10:00:00+00:00"}]
    assert wd._last_reply(thread) is None
    assert wd._last_reply([]) is None


def test_the_payload_surfaces_the_snippet_and_what_to_do_about_it():
    from applypilot import web_dashboard as wd
    thread = [{"direction": "in", "from_addr": "g@co.com", "from_name": "Gina",
               "sent_at": "2026-07-30T10:00:00+00:00",
               "snippet": "Unfortunately we've decided to move forward with another candidate."}]
    out = wd._last_reply(thread)
    assert out["intent"] == intent.REJECTION
    assert out["action"] and "follow-up" in out["action"]
    assert out["from"] == "Gina"

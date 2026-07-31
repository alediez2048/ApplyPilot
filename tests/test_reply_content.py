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


def test_the_snippet_is_stored_when_the_scope_is_on(db, monkeypatch):
    from applypilot.networking import gmail_read, replies

    monkeypatch.setattr(gmail_read, "can_read_content", lambda: (True, "ok"))
    contact = {"id": "c1", "job_url": "http://j/1", "thread_id": "t1", "email": "gina@co.com"}
    replies._sync_thread(contact, [_thread_msg("Happy to chat — are you free Thursday?")], ME, db)

    rows = msg_store.thread_for_contact("c1", db)
    assert rows[0]["snippet"].startswith("Happy to chat")


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
    from applypilot.networking import gmail_read, replies

    contact = {"id": "c1", "job_url": "http://j/1", "thread_id": "t1", "email": "gina@co.com"}
    monkeypatch.setattr(gmail_read, "can_read_content", lambda: (True, "ok"))
    replies._sync_thread(contact, [_thread_msg("Happy to chat Thursday")], ME, db)
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
    subject line — and it would look like a working feature until somebody read it."""
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
    assert res["ok"] is False and "content is off" in res["message"]


def test_the_drafter_refuses_a_thread_with_no_readable_reply():
    from applypilot.networking import outreach
    thread = [{"direction": "in", "from_addr": "g@co.com", "snippet": "", "subject": "Re: x"}]
    with pytest.raises(ValueError, match="no readable reply text"):
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

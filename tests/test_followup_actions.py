"""ARCH-3: the /api/followup write paths, which had no direct coverage at all.

The byte-identical /api/status check proves the READ path survived the migration. It says
nothing about the WRITE paths — and ARCH-3 rewrote all of them: `_followup_action` went from
two mirrored per-channel blocks to one parameterised path, and the wire shape changed with it.

Every button in the follow-up panel goes through this one function. One of them sends email.
"""

from __future__ import annotations

import re

import pytest

import applypilot.database as database
from applypilot import web_dashboard as wd
from applypilot.networking import store, touches


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    touches.init_touches(conn)
    conn.execute("INSERT INTO jobs (url, title, site) VALUES (?,?,?)",
                 ("http://j/1", "PM", "Greenhouse"))
    conn.commit()
    return conn


@pytest.fixture()
def cid(db):
    return store.upsert_contact({
        "job_url": "http://j/1", "full_name": "Jane Doe", "email": "jane@acme.com",
        "linkedin_url": "https://l/in/jane", "dm_status": "manual",
        "outreach_status": "submitted", "sent_message_id": "gid-1",
    }, db)


def act(**kw):
    return wd._followup_action(kw)


# ── action parsing: the channel lives in the action name ────────────────────

@pytest.mark.parametrize("action,channel,verb", [
    ("draft", "email", "draft"), ("li_draft", "linkedin", "draft"),
    ("save", "email", "save"), ("li_save", "linkedin", "save"),
    ("stop", "email", "stop"), ("li_stop", "linkedin", "stop"),
    ("replied", "email", "replied"), ("li_replied", "linkedin", "replied"),
    ("sent", "email", "sent"), ("li_sent", "linkedin", "sent"),
])
def test_action_names_split_into_channel_and_verb(action, channel, verb):
    ch, v = wd._split_followup_action(action)
    assert (ch.name, v) == (channel, verb)


def test_email_prefix_is_empty_so_it_must_be_the_fallback():
    """EMAIL.prefix == '' — if it were matched like the others, every action would be email."""
    ch, verb = wd._split_followup_action("li_stop")
    assert ch.name == "linkedin" and verb == "stop"


def test_unknown_action_is_rejected_not_silently_ignored(db, cid):
    r = act(contact_id=cid, action="obliterate")
    assert r["ok"] is False and "unknown" in r["message"]


def test_missing_contact_is_rejected(db):
    assert act(contact_id="nope", action="stop")["ok"] is False


# ── both channels, one code path ────────────────────────────────────────────

@pytest.mark.parametrize("prefix,channel", [("", "email"), ("li_", "linkedin")])
def test_stop_replied_reopen_round_trip(db, cid, prefix, channel):
    assert act(contact_id=cid, action=f"{prefix}stop")["ok"]
    assert touches.ladder_state(cid, channel, db)["sequence_status"] == "stopped"

    assert act(contact_id=cid, action=f"{prefix}replied")["ok"]
    assert touches.ladder_state(cid, channel, db)["sequence_status"] == "replied"

    assert act(contact_id=cid, action=f"{prefix}reopen")["ok"]
    assert touches.ladder_state(cid, channel, db)["sequence_status"] == ""


@pytest.mark.parametrize("prefix,channel", [("", "email"), ("li_", "linkedin")])
def test_save_stores_a_draft_on_the_right_channel(db, cid, prefix, channel):
    r = act(contact_id=cid, action=f"{prefix}save", subject="Re: hi", body="still keen")
    assert r["ok"]
    assert touches.ladder_state(cid, channel, db)["draft_body"] == "still keen"
    # …and the other channel is untouched: separate rows, not separate columns
    other = "linkedin" if channel == "email" else "email"
    assert touches.ladder_state(cid, other, db)["draft_body"] == ""


def test_reopen_now_works_for_linkedin_too(db, cid):
    """There was no `li_reopen` before ARCH-3 — the LinkedIn block simply never had one.

    Collapsing the two blocks gave LinkedIn every verb email had, for free. This is the
    concrete payoff of the refactor, so it is worth a test rather than a comment.
    """
    act(contact_id=cid, action="li_stop")
    assert act(contact_id=cid, action="li_reopen")["ok"]
    assert touches.ladder_state(cid, "linkedin", db)["sequence_status"] == ""


def test_marking_a_touch_sent_advances_the_count(db, cid):
    r = act(contact_id=cid, action="li_sent")
    assert r["ok"] and r["touch"] == 1
    assert act(contact_id=cid, action="li_sent")["touch"] == 2
    assert touches.ladder_state(cid, "linkedin", db)["count"] == 2
    assert touches.ladder_state(cid, "email", db)["count"] == 0


def test_draft_uses_the_channel_drafter_and_stores_the_result(db, cid, monkeypatch):
    from applypilot.networking import outreach
    seen = {}

    def fake(channel, profile, job, contact, touch=1, style=""):
        seen.update(channel=channel, touch=touch)
        return {"subject": f"S{touch}", "body": f"B{touch}"}
    monkeypatch.setattr(outreach, "draft_for_channel", fake)

    r = act(contact_id=cid, action="li_draft")
    assert r["ok"] and seen["channel"] == "linkedin" and seen["touch"] == 1
    assert touches.ladder_state(cid, "linkedin", db)["draft_body"] == "B1"

    # touch number advances with the ladder, so the prompt differs per position
    act(contact_id=cid, action="li_sent")
    act(contact_id=cid, action="li_draft")
    assert seen["touch"] == 2


def test_linkedin_can_never_be_auto_sent(db, cid):
    """Structural, not a policy string. Driving LinkedIn from outside the browser was
    abandoned twice (CLAUDE.md §Lessons 3); `can_autosend=False` is what enforces it."""
    r = act(contact_id=cid, action="li_send")
    assert r["ok"] is False and "cannot be auto-sent" in r["message"]


def test_send_is_refused_without_a_draft(db, cid):
    """The email path reaches gmail_send, which must not fire on an empty ladder."""
    r = act(contact_id=cid, action="send")
    assert r["ok"] is False


# ── the cross-language contract ─────────────────────────────────────────────

def test_the_frontend_sends_the_keys_the_backend_reads():
    """ARCH-3 changed the wire shape: `li_save` used to post `message`, now it posts `body`.

    Nothing else would have caught a half-applied rename — Python would read `body`, get
    "", and cheerfully save an empty draft over the one you just typed. No error, no test
    failure, no clue. So this reads the actual JS and checks the keys line up.
    """
    js = (wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    posts = re.findall(r"/api/followup['\"]\s*,\s*(\{.*?\})", js, re.S)
    assert posts, "no /api/followup calls found — did the endpoint or the JS move?"

    sent_keys = set()
    for blob in posts:
        sent_keys |= {m for m in re.findall(r"(\w+)\s*:", blob)}
    # `body` is the object being built in fuAct, not a payload key there; both appear.
    allowed = {"contact_id", "action", "subject", "body", "message"}
    assert sent_keys <= allowed, f"JS posts unexpected keys: {sent_keys - allowed}"
    assert "message" not in sent_keys, \
        "JS still posts `message`; ARCH-3 renamed it to `body` and the backend reads `body`"

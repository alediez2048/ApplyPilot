"""Logging a LinkedIn message, in both directions.

Reported as "people reach out via LinkedIn and I don't know where to log it". There was
nowhere, for a structural reason:

* `messages` is Gmail-shaped — primary key `(message_id, contact_id)` where `message_id` is
  Gmail's own id, plus `thread_id`, `rfc_message_id`, `from_addr`. A DM has none of those, and
  inventing them to fit would corrupt the join reply detection runs on.
* `contacts.dm_status` is `sent` | `manual`, both meaning WE sent an invite. There is no
  `accepted` state anywhere in the schema, so nothing recorded what they sent back.

`interactions` needed no migration: `kind` is an open TEXT column and the row id is
`sha256(contact|kind|at)`, so re-logging the same message is an upsert.

Nothing reads LinkedIn. Automating it was abandoned twice (§Lessons 3) and it risks the account
the whole outreach ladder runs on, so this is typed in and tagged `manual`.
"""

from __future__ import annotations

import pytest

import applypilot.database as database
from applypilot import web_dashboard as wd
from applypilot.domain import interactions as ix
from applypilot.networking import interactions_store, store, touches


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    touches.init_touches(conn)
    interactions_store.init_interactions(conn)
    conn.execute("INSERT INTO jobs (url, title, site, strategy, applied_at) VALUES (?,?,?,?,?)",
                 ("http://j/1", "PM", "Greenhouse", "dashboard_upload",
                  "2026-07-20T10:00:00+00:00"))
    conn.commit()
    return conn


def _contact(db, **over):
    c = {"job_url": "http://j/1", "full_name": "Sarah Chen", "email": "s@acme.com",
         "linkedin_url": "https://linkedin.com/in/sarah"}
    c.update(over)
    return store.upsert_contact(c, db)


# ── writing ─────────────────────────────────────────────────────────────────

def test_an_inbound_message_is_stored_with_its_text(db):
    cid = _contact(db)
    r = wd._log_interaction({"contact_id": cid, "kind": ix.LINKEDIN_IN,
                             "detail": "Hi Alejandro, saw your note — what role was it?"})
    assert r["ok"], r
    rows = interactions_store.for_job("http://j/1", db).get(cid, [])
    assert [x["kind"] for x in rows] == [ix.LINKEDIN_IN]
    assert "what role" in rows[0]["detail"]
    assert rows[0]["source"] == "manual", "a typed-in message must never read as detected"


def test_both_directions_are_loggable(db):
    cid = _contact(db)
    assert wd._log_interaction({"contact_id": cid, "kind": ix.LINKEDIN_IN,
                                "detail": "they wrote", "at": "2026-08-01T10:00:00+00:00"})["ok"]
    assert wd._log_interaction({"contact_id": cid, "kind": ix.LINKEDIN_OUT,
                                "detail": "you wrote", "at": "2026-08-02T10:00:00+00:00"})["ok"]
    assert len(interactions_store.for_job("http://j/1", db).get(cid, [])) == 2


def test_an_empty_message_is_refused(db):
    """The whole value is the text. A row with no body records that something happened and
    loses what it was — which is the state `dm_status` was already in."""
    cid = _contact(db)
    r = wd._log_interaction({"contact_id": cid, "kind": ix.LINKEDIN_IN, "detail": "   "})
    assert r["ok"] is False and "paste" in r["message"].lower()
    assert interactions_store.for_job("http://j/1", db).get(cid, []) == []


def test_logging_the_same_message_twice_is_idempotent(db):
    """§Lessons 22 — run it twice, do not reason about it. A bounced contact once produced 11
    identical log lines because nobody did."""
    cid = _contact(db)
    payload = {"contact_id": cid, "kind": ix.LINKEDIN_IN, "detail": "hello",
               "at": "2026-08-01T10:00:00+00:00"}
    wd._log_interaction(dict(payload))
    wd._log_interaction(dict(payload))
    assert len(interactions_store.for_job("http://j/1", db).get(cid, [])) == 1


def test_the_text_is_capped_at_the_write(db):
    """Correspondence in an unencrypted file. Trimming at render time would leave the whole
    thing on disk, which is the opposite of the point."""
    from applypilot.networking.messages import PASTED_MAX
    cid = _contact(db)
    wd._log_interaction({"contact_id": cid, "kind": ix.LINKEDIN_IN, "detail": "x" * 9000})
    assert len(interactions_store.for_job("http://j/1", db).get(cid, [])[0]["detail"]) == PASTED_MAX


# ── it stops the ladder ─────────────────────────────────────────────────────

def test_an_inbound_message_stops_the_linkedin_ladder(db):
    """A reply on LinkedIn ends the LinkedIn sequence, exactly as a detected email reply ends
    the email one. Reuses `sequences` rather than adding a second terminal-state mechanism —
    one engine already decides whether a ladder is live, and `tick` reads it."""
    cid = _contact(db)
    wd._log_interaction({"contact_id": cid, "kind": ix.LINKEDIN_IN, "detail": "hi"})
    assert touches.ladder_state(cid, "linkedin", db)["sequence_status"] == "replied"


def test_it_stops_only_that_channel(db):
    """They answered on LinkedIn; the email conversation is untouched and may still be owed a
    follow-up."""
    cid = _contact(db)
    wd._log_interaction({"contact_id": cid, "kind": ix.LINKEDIN_IN, "detail": "hi"})
    assert touches.ladder_state(cid, "email", db)["sequence_status"] == ""
    assert touches.ladder_state(cid, "sms", db)["sequence_status"] == ""


def test_our_own_reply_does_not_stop_anything(db):
    """Logging what YOU sent is bookkeeping. Only their message is a signal."""
    cid = _contact(db)
    wd._log_interaction({"contact_id": cid, "kind": ix.LINKEDIN_OUT, "detail": "mine"})
    assert touches.ladder_state(cid, "linkedin", db)["sequence_status"] == ""


# ── it counts as engagement, but not as an email reply ──────────────────────

def test_an_inbound_message_is_engagement(db):
    rows = ix.for_contact({"id": "c1"}, [{"kind": ix.LINKEDIN_IN, "at": "2026-08-01T10:00:00+00:00",
                                          "detail": "hi", "source": "manual"}])
    assert ix.summarise(rows)["engaged"] is True
    assert ix.has_inbound(rows) is True


def test_our_own_linkedin_message_is_not_engagement(db):
    """§Lessons 35. `dm_status` counted our own invite as engagement and every job read
    "3/3 engaged"; the honest number was 2 of 58."""
    rows = ix.for_contact({"id": "c1"}, [{"kind": ix.LINKEDIN_OUT, "at": "2026-08-01T10:00:00+00:00",
                                          "detail": "mine", "source": "manual"}])
    assert ix.summarise(rows)["engaged"] is False
    assert ix.has_inbound(rows) is False


def test_it_never_writes_replied_at(db):
    """`replied_at` means a DETECTED email reply and is what `metrics.by_variant` divides by.
    Mixing a typed-in number into a measured one makes the copy experiment unreadable."""
    cid = _contact(db)
    wd._log_interaction({"contact_id": cid, "kind": ix.LINKEDIN_IN, "detail": "hi"})
    assert not (store.get_contact(cid, db).get("replied_at") or "")


def test_it_reaches_the_counter(db):
    """It should still interrupt you — being on LinkedIn does not make it less of a reply.
    Joined at the counter, not in the rate."""
    cid = _contact(db)
    wd._log_interaction({"contact_id": cid, "kind": ix.LINKEDIN_IN, "detail": "hi"})
    job = next(j for j in wd._status_payload()["jobs"] if j["url"] == "http://j/1")
    assert [w["id"] for w in job["awaiting_reply"]] == [cid]


def test_answering_them_clears_the_counter(db):
    cid = _contact(db)
    wd._log_interaction({"contact_id": cid, "kind": ix.LINKEDIN_IN, "detail": "hi",
                         "at": "2026-08-01T10:00:00+00:00"})
    wd._log_interaction({"contact_id": cid, "kind": ix.LINKEDIN_OUT, "detail": "answered",
                         "at": "2026-08-02T10:00:00+00:00"})
    job = next(j for j in wd._status_payload()["jobs"] if j["url"] == "http://j/1")
    assert job["awaiting_reply"] == [], "answered on LinkedIn and still counted as owed"


# ── the draft stops contradicting the record ────────────────────────────────

def test_the_next_draft_is_told_what_was_actually_said():
    """The prompt asserted "they accepted the invite and have not replied" unconditionally —
    a claim, not an observation, and false the moment anything is logged. Two instructions in
    one prompt disagreeing is a code bug, not a wording problem (§Lessons 40)."""
    from applypilot.networking.outreach import _li_state
    cold = _li_state({}, "2026-08-01", None)
    assert "have not replied" in cold

    warm = _li_state({}, "2026-08-01", [
        {"kind": ix.LINKEDIN_IN, "detail": "what role was it?"},
        {"kind": ix.LINKEDIN_OUT, "detail": "the TPM one"}])
    assert "have not replied" not in warm, "the draft still claims silence"
    assert "what role was it?" in warm and "the TPM one" in warm
    assert "re-introduce" in warm


def test_nothing_reads_linkedin():
    """§Lessons 3, abandoned twice. This whole feature is a paste box for that reason."""
    import pathlib
    src = pathlib.Path(wd.__file__).read_text(encoding="utf-8")
    block = src[src.index("def _log_interaction"):]
    block = block[:block.index("\ndef ")]
    assert "linkedin.com" not in block and "requests" not in block

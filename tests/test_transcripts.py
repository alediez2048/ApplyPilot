"""Meeting transcripts on a contact — GRAN-1.

Three things decide this feature and all three came from measuring rather than designing:

**Granola's API is Business/Enterprise only**, so the paste is not a shortcut, it is the only
route that exists without a subscription. **Transcripts are diarized by AUDIO SOURCE** (mic vs
system), not by speaker, so nothing here may claim to know which of three attendees said
something. And a transcript is **20–50 KB** against `messages.snippet`'s 200/2000 cap, so it
needed its own table rather than a home in one of the two that already hold text.
"""

from __future__ import annotations

import pytest

import applypilot.web_dashboard as wd
from applypilot.domain import interactions as ix_domain
from applypilot.domain import transcript as t
from applypilot.networking import transcripts as store

BODY = "Hi Dana.\n\n[00:12] Dana: we are hiring two engineers this quarter.\nGreat, thanks."


# ── the pure half ───────────────────────────────────────────────────────────

def test_a_body_is_cleaned_without_being_reformatted():
    """Every tool lays a transcript out differently; a parser that "tidies" one mangles the next.
    The body is EVIDENCE and is stored the way it arrived."""
    out = t.clean_body("  a\r\nb\t\tc\n\n\n\nd  ")
    assert out == "a\nb c\n\nd"
    assert "Dana: we are hiring" in t.clean_body(BODY), "the words were altered"


def test_an_empty_paste_and_an_oversized_one_are_different_refusals():
    with pytest.raises(t.TranscriptError, match="nothing pasted"):
        t.validate("   \n  ")
    with pytest.raises(t.TranscriptError, match="cap"):
        t.validate("x " * t.TRANSCRIPT_MAX)


def test_an_oversized_body_is_refused_rather_than_truncated():
    """A silently shortened transcript is evidence the operator believes they have and do not
    (§Lessons 90). The refusal names the size AND the cap so it can be acted on."""
    with pytest.raises(t.TranscriptError) as e:
        t.validate("x " * t.TRANSCRIPT_MAX)
    assert str(t.TRANSCRIPT_MAX) in str(e.value).replace(",", "")


def test_the_id_is_stable_for_the_same_meeting_and_differs_between_meetings():
    a = t.transcript_id("granola", "note-1")
    assert a == t.transcript_id("granola", "note-1")
    assert a != t.transcript_id("granola", "note-2")
    assert a != t.transcript_id("otter", "note-1"), "two sources collided on one id"


def test_a_paste_with_no_external_id_keys_on_its_CONTENT():
    """There is no id to key on, so the text is the identity — that is what makes re-pasting the
    same note a no-op rather than a second row."""
    a = t.transcript_id("paste", "", body=BODY, started_at="")
    assert a == t.transcript_id("paste", "", body=BODY, started_at="")
    assert a != t.transcript_id("paste", "", body=BODY + " and one more thing", started_at="")


def test_an_excerpt_skips_scaffolding_and_stops_on_a_word():
    """It stands in when no summary was supplied, so it should be words rather than timestamps."""
    out = t.excerpt("[00:00]\nSpeaker 1\nmicrophone\nWe talked about the roadmap.", limit=200)
    assert out == "Hi." or "roadmap" in out
    assert "00:00" not in out and "Speaker 1" not in out
    long = t.excerpt("word " * 500, limit=100)
    assert len(long) <= 100 and not long.endswith("wor")


# ── what may reach a prompt ─────────────────────────────────────────────────

def test_only_the_summary_reaches_a_prompt_never_the_body():
    """A body is 20–50 KB and §Lessons 40 is that the loudest block in a prompt wins — dropped in
    whole it would swamp the posting, the premise and the voice at once."""
    block = t.prompt_block([{"summary": "They are hiring two engineers.", "body": BODY,
                             "title": "Intro call", "started_at": "2026-08-01T10:00:00+00:00"}])
    assert "hiring two engineers" in block
    assert "[00:12]" not in block, "the raw transcript reached the prompt"


def test_the_prompt_forbids_revealing_that_a_recording_exists():
    """§Lessons 83 with higher stakes than the deck beacon: a sentence that only makes sense to
    somebody who was on the call tells them you are working from a recording of them."""
    block = t.prompt_block([{"summary": "s", "started_at": "2026-08-01", "title": "Call"}])
    low = block.lower()
    assert "never quote" in low
    assert "recording" in low and "transcript" in low
    assert "would not make sense to" in low, "the falsifiable test for a leaky sentence is gone"


def test_the_block_is_empty_when_there_is_nothing_to_say():
    """An empty block must be EMPTY, not a heading with nothing under it — a prompt section that
    says "notes from that conversation:" and then stops invites the model to invent some."""
    assert t.prompt_block([]) == ""
    assert t.prompt_block(None) == ""
    assert t.prompt_block([{"summary": "  "}]) == "", "a blank summary produced a heading"


def test_it_is_bounded_so_ten_meetings_do_not_become_the_prompt():
    """Asserted with the DEFAULT, not with an explicit limit.

    The first version passed `limit=2` and therefore proved only that the parameter works —
    raising the default to 999 left it green while every prompt carried every meeting ever
    stored. A test that supplies the value it is checking cannot see the value that ships.
    """
    rows = [{"summary": f"call {i}", "started_at": "2026-08-01", "title": "x"} for i in range(9)]
    assert t.prompt_block(rows).count("- 2026") == 2, "the default is not bounded"
    assert t.prompt_block(rows, limit=1).count("- 2026") == 1


def test_the_short_channels_get_less_of_it():
    """`brief` is the text, LinkedIn note and reply path — a 300-character channel cannot carry
    two meeting summaries and the posting and the premise."""
    from applypilot.networking.outreach import _met_block
    rows = [{"summary": f"call {i}", "started_at": "2026-08-0%d" % (i + 1), "title": "x"}
            for i in range(4)]
    c = {"id": "c1", "transcripts": rows}
    assert _met_block(c, brief=True).count("- 2026") == 1
    assert _met_block(c).count("- 2026") == 2


def test_a_contact_with_no_meetings_adds_nothing_to_a_prompt():
    """An empty contribution must be EMPTY. `_met_block` is concatenated into six prompts, so a
    stray heading would appear in every draft the app writes."""
    from applypilot.networking.outreach import _met_block
    assert _met_block({"id": "c1", "transcripts": []}) == ""


# ── the store ───────────────────────────────────────────────────────────────

@pytest.fixture
def conn(tmp_path, monkeypatch):
    import applypilot.database as database
    from applypilot.networking import store as cstore
    db = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", db)
    database.close_connection(db)
    database.init_db(db)
    c = database.get_connection(db)
    cstore.init_contacts(c)
    for cid, name in (("c1", "Dana Okafor"), ("c2", "Sam Iyer")):
        cstore.upsert_contact({"id": cid, "job_url": "target:s:apex", "full_name": name,
                               "email": f"{cid}@apex.test", "company": "Apex"})
    return c


def test_pasting_the_same_transcript_twice_stores_it_once(conn):
    """Idempotence is tested by RUNNING it twice, never by reasoning (§Lessons 22).

    The first version failed this: the id was seeded with a `started_at` that defaulted to
    `now()`, so the default moved between the two calls and every paste was a new row. Re-pasting
    after fixing a typo is the ordinary way to use this.
    """
    a = store.save(body=BODY, contact_ids=["c1"], title="Intro", conn=conn)
    b = store.save(body=BODY, contact_ids=["c1"], title="Intro", conn=conn)
    assert a["id"] == b["id"]
    assert a["added"] is True and b["added"] is False
    assert len(store.for_contact("c1", conn)) == 1


def test_one_meeting_is_stored_once_and_attached_to_everyone_on_it(conn):
    """A call with three people is one 40 KB row, not three. Storing it per contact duplicates
    the blob and lets the copies drift."""
    out = store.save(body=BODY, contact_ids=["c1", "c2"], title="Intro", conn=conn)
    assert out["attached"] == 2
    assert conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 1
    assert len(store.for_contact("c1", conn)) == 1
    assert len(store.for_contact("c2", conn)) == 1


def test_the_list_never_carries_the_body(conn):
    """It feeds a panel that re-renders every 2.5s; 40 KB per contact on that path forever is
    §Lessons 26 with bytes instead of round-trips."""
    store.save(body=BODY, contact_ids=["c1"], conn=conn)
    row = store.for_contact("c1", conn)[0]
    assert "body" not in row
    assert row["body_len"] == len(t.clean_body(BODY)), "no way to show how long the call was"
    assert store.body_for(row["id"], conn)["body"] == t.clean_body(BODY)


def test_a_supplied_summary_wins_over_the_excerpt_and_says_which_it_is(conn):
    """The first 1,200 characters of a call are the greetings. Presenting that as a summary is a
    claim the data does not support, so the caller is told which one it got."""
    a = store.save(body=BODY, contact_ids=["c1"], summary="They are hiring.", conn=conn)
    assert a["summary_is_excerpt"] is False
    assert store.for_contact("c1", conn)[0]["summary"] == "They are hiring."
    b = store.save(body=BODY + " x", contact_ids=["c2"], conn=conn)
    assert b["summary_is_excerpt"] is True


def test_detaching_one_person_leaves_the_meeting_for_the_others(conn):
    """Deleting the transcript while somebody else is attached would empty their record from
    under them."""
    store.save(body=BODY, contact_ids=["c1", "c2"], conn=conn)
    store.detach(store.for_contact("c1", conn)[0]["id"], "c1", conn)
    assert store.for_contact("c1", conn) == []
    assert len(store.for_contact("c2", conn)) == 1, "removing one person deleted the meeting"
    assert conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 1


def test_the_meeting_goes_when_the_last_person_leaves(conn):
    store.save(body=BODY, contact_ids=["c1"], conn=conn)
    store.detach(store.for_contact("c1", conn)[0]["id"], "c1", conn)
    assert conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 0, "orphan left"


def test_deleting_a_contact_does_not_take_a_shared_meeting_with_it(conn):
    """`delete_contact` bulk-DELETEs from four tables by contact_id. A transcript is SHARED, so
    the same treatment would leave the other attendee pointing at a row that no longer exists."""
    from applypilot.networking import store as cstore
    store.save(body=BODY, contact_ids=["c1", "c2"], conn=conn)
    cstore.delete_contact("c1", conn)
    assert store.for_contact("c1", conn) == []
    assert len(store.for_contact("c2", conn)) == 1, "a shared meeting was destroyed"


def test_many_contacts_are_read_in_one_query(conn):
    """`/api/status` is held to 80 statements and had six spare; a per-contact read blows it."""
    store.save(body=BODY, contact_ids=["c1", "c2"], conn=conn)
    out = store.for_contacts(["c1", "c2", "missing"], conn)
    assert set(out) == {"c1", "c2"}
    assert store.for_contacts([], conn) == {}


# ── the endpoint ────────────────────────────────────────────────────────────

def test_the_endpoint_refuses_a_transcript_with_nobody_on_it(conn):
    """Attribution is the operator's CHOICE — Granola diarizes by audio source, not by speaker,
    so nothing can infer who a call was with."""
    out = wd._save_transcript({"body": BODY, "contact_ids": []})
    assert out["ok"] is False and "at least one person" in out["message"]


def test_the_endpoint_refuses_an_unknown_contact(conn):
    out = wd._save_transcript({"body": BODY, "contact_ids": ["nope"]})
    assert out["ok"] is False and "unknown contact" in out["message"]


def test_the_endpoint_says_when_the_summary_is_only_an_excerpt(conn):
    out = wd._save_transcript({"body": BODY, "contact_ids": ["c1"]})
    assert out["ok"] is True
    assert "No summary supplied" in out["message"]
    with_sum = wd._save_transcript({"body": BODY + " x", "contact_ids": ["c1"],
                                    "summary": "They are hiring."})
    assert "No summary supplied" not in with_sum["message"]


def test_an_empty_body_is_refused_with_its_reason(conn):
    out = wd._save_transcript({"body": "  ", "contact_ids": ["c1"]})
    assert out["ok"] is False and "nothing pasted" in out["message"]


def test_storing_one_records_the_meeting_on_the_timeline(conn):
    wd._save_transcript({"body": BODY, "contact_ids": ["c1"], "title": "Intro call"})
    from applypilot.networking import interactions_store as ix
    kinds = [r["kind"] for r in ix.for_job("target:s:apex", conn).get("c1", [])]
    assert ix_domain.MET in kinds


# ── what it counts as ───────────────────────────────────────────────────────

def test_a_meeting_we_attended_is_not_engagement():
    """§Lessons 35, third time. Whether a call means the other side is interested depends on who
    ASKED for it, and nothing here knows that. A detected cal.com booking still counts, because
    that one is them spending their own time."""
    assert ix_domain.WEIGHT[ix_domain.MET] == 0
    assert ix_domain.MET not in ix_domain.ENGAGEMENT
    assert ix_domain.MET not in ix_domain.INBOUND
    assert ix_domain.WEIGHT[ix_domain.BOOKED] > 0
    assert ix_domain.LABEL[ix_domain.MET] and ix_domain.ICON[ix_domain.MET]
    assert ix_domain.ICON[ix_domain.MET] != ix_domain.ICON[ix_domain.NOTE], \
        "two kinds sharing an icon read as one kind on the timeline"

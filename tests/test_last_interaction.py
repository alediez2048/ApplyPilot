"""When something last happened on a job, and who did it.

Six sources knew and none of them were joined: `jobs.applied_at`, `contacts.submitted_at`,
`touches.sent_at`, `contacts.replied_at`, `contacts.dm_sent_at` / `interactions`, and the deck
columns. `grep -rn "last_touch\\|lastTouch\\|last_interaction"` across the dashboard, the domain
and the JS returned zero hits before this.

Derived, never stored: a `last_interaction_at` column would have to be written by all six of
those paths and would be wrong the first time one forgot — §Lessons 21 with a new name.

**Direction is the point.** "You emailed them 6 days ago" and "they replied 6 days ago" are the
same age and opposite situations. A timestamp with no direction is the flat count the 🔔
counter already had to abandon.
"""

from __future__ import annotations

from applypilot.domain import interactions as ix
from applypilot.domain.lastinteraction import last_interaction

JOB = {"applied_at": "2026-07-20T10:00:00+00:00"}


def _c(cid="c1", name="Sarah Chen", **over):
    c = {"id": cid, "full_name": name, "interactions": []}
    c.update(over)
    return c


def _ix(kind, at):
    return {"kind": kind, "at": at, "detail": "", "source": "detected"}


# ── it finds the newest, whatever produced it ───────────────────────────────

def test_it_reports_the_most_recent_event_across_every_source():
    """One per source, deliberately out of order — the newest must win regardless of which
    list it came from."""
    contacts = [_c(interactions=[
        _ix(ix.SENT, "2026-07-21T10:00:00+00:00"),
        _ix(ix.DECK, "2026-07-25T10:00:00+00:00"),
        _ix(ix.REPLIED, "2026-08-01T10:00:00+00:00"),   # newest
        _ix(ix.CONNECTED, "2026-07-22T10:00:00+00:00"),
    ])]
    out = last_interaction(JOB, contacts, {})
    assert out["kind"] == ix.REPLIED and out["at"].startswith("2026-08-01")


def test_a_follow_up_counts_even_though_it_is_not_in_the_timeline():
    """Follow-ups live in `touches`, keyed per (contact, channel), and are absent from the
    per-contact interaction timeline. Without them a job whose only recent activity was a third
    follow-up reports the original email from two weeks earlier."""
    contacts = [_c(interactions=[_ix(ix.SENT, "2026-07-21T10:00:00+00:00")])]
    ladders = {("c1", "email"): {"last_sent_at": "2026-08-02T10:00:00+00:00"}}
    out = last_interaction(JOB, contacts, ladders)
    assert out["kind"] == "followup" and out["direction"] == "out"


def test_a_job_with_no_contacts_still_reports_that_you_applied():
    """"Nothing has happened" is wrong for something you applied to."""
    out = last_interaction(JOB, [], {})
    assert out["kind"] == "applied" and out["label"] == "You applied"


def test_a_job_with_nothing_at_all_reports_nothing():
    """Rendering "just now" for a job nobody has touched would be a lie with a clock on it."""
    assert last_interaction({"applied_at": ""}, [], {}) is None


# ── direction is carried, and it is the point ───────────────────────────────

def test_their_actions_and_ours_are_distinguished():
    theirs = last_interaction(JOB, [_c(interactions=[_ix(ix.REPLIED, "2026-08-01T10:00:00+00:00")])], {})
    ours = last_interaction(JOB, [_c(interactions=[_ix(ix.SENT, "2026-08-01T10:00:00+00:00")])], {})
    assert theirs["direction"] == "in" and ours["direction"] == "out"
    assert theirs["at"] == ours["at"], "same age, opposite situations — that is the whole point"


def test_the_direction_line_matches_the_engagement_line():
    """A signal cannot be "engagement" in one module and "our own action" in another. Both read
    `ix.ENGAGEMENT`, so the deck open that counts as engagement also counts as inbound here."""
    for kind in ix.ENGAGEMENT:
        out = last_interaction(JOB, [_c(interactions=[_ix(kind, "2026-08-01T10:00:00+00:00")])], {})
        assert out["direction"] == "in", f"{kind} counts as engagement but reads as ours"
    for kind in (ix.SENT, ix.CONNECTED, ix.LINKEDIN_OUT):
        out = last_interaction(JOB, [_c(interactions=[_ix(kind, "2026-08-01T10:00:00+00:00")])], {})
        assert out["direction"] == "out", f"{kind} is our own action and reads as theirs"


def test_a_linkedin_message_they_sent_is_inbound():
    out = last_interaction(JOB, [_c(interactions=[_ix(ix.LINKEDIN_IN, "2026-08-03T10:00:00+00:00")])], {})
    assert out["direction"] == "in" and "messaged you" in out["label"]


# ── it reads like a sentence ────────────────────────────────────────────────

def test_the_label_names_the_person_by_first_name():
    """It sits on a table row: "Sarah" fits where "Sarah Chen-Okonkwo" does not."""
    out = last_interaction(JOB, [_c(name="Sarah Chen-Okonkwo",
                                    interactions=[_ix(ix.REPLIED, "2026-08-01T10:00:00+00:00")])], {})
    assert out["label"] == "Sarah replied"


def test_an_unnamed_contact_does_not_render_a_blank():
    out = last_interaction(JOB, [_c(name="", interactions=[_ix(ix.REPLIED, "2026-08-01T10:00:00+00:00")])], {})
    assert out["label"] == "them replied" or "them" in out["label"]


# ── the edges that have burned this codebase before ─────────────────────────

def test_a_naive_timestamp_does_not_raise():
    """Older rows have no timezone; subtracting one from an aware now raises and 500s the whole
    dashboard (§Lessons 6). Unparseable candidates are dropped, not compared."""
    contacts = [_c(interactions=[_ix(ix.REPLIED, "2026-08-01 10:00:00"),
                                 _ix(ix.SENT, "not a date")])]
    out = last_interaction(JOB, contacts, {})
    assert out is not None


def test_it_never_invents_a_time():
    """A row with no timestamp must not become "just now"."""
    out = last_interaction({"applied_at": ""}, [_c(interactions=[_ix(ix.REPLIED, "")])], {})
    assert out is None


def test_it_reads_only_what_is_already_on_the_payload():
    """`/api/status` re-renders every 2.5s against an 80-statement budget. This must stay a
    join in Python over data already loaded — §Lessons 26 is the reminder that the budget only
    counts SQL, so a new source here would not show up in it."""
    import inspect

    from applypilot.domain import lastinteraction
    src = inspect.getsource(lastinteraction)
    for forbidden in ("get_connection", "execute(", "sqlite3", "import httpx", "requests"):
        assert forbidden not in src, f"{forbidden} — this must be pure"


# ── it reaches the row ──────────────────────────────────────────────────────

def test_it_reaches_the_payload(tmp_path, monkeypatch):
    """A derivation nobody renders is the `network_note` bug: computed correctly, shipped, and
    read by no JS at all (§Lessons 15)."""
    import applypilot.database as database
    from applypilot import web_dashboard as wd
    from applypilot.networking import store

    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    conn.execute("INSERT INTO jobs (url, title, site, strategy, applied_at) VALUES (?,?,?,?,?)",
                 ("http://j/1", "PM", "Greenhouse", "dashboard_upload",
                  "2026-07-20T10:00:00+00:00"))
    conn.commit()
    store.upsert_contact({"job_url": "http://j/1", "full_name": "Sarah Chen",
                          "email": "s@x.com", "replied_at": "2026-08-01T10:00:00+00:00"}, conn)

    jobs = wd._status_payload()["jobs"]
    assert jobs, "empty payload — this test would measure nothing (§Lessons 13)"
    li = next(j for j in jobs if j["url"] == "http://j/1")["last_interaction"]
    assert li and li["direction"] == "in" and li["label"] == "Sarah replied"

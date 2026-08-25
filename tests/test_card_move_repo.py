"""The card move itself: two columns, one transaction, and nothing re-keyed.

The property that makes this safe is that the ANCHOR does not move. `store.contact_id` hashes
it, so re-keying would orphan every contact, ladder, message and transcript on the card — the
exact failure `repo.attach_posting` exists to avoid. These tests assert the anchor is untouched
and that everything hanging off it still resolves afterwards.
"""
import pytest

from applypilot.database import get_connection, init_db
from applypilot.networking import store, touches as _touches
from applypilot.repo import cardmove as cm
from applypilot.repo import spaces as _spaces

SRC, DST = "professional-network", "partnerships"
ANCHOR = "target:professional-network:acme"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A card, one contact, two Spaces — reset from scratch every time.

    The rows are DELETED before being written, not merely upserted. `get_connection` caches by
    path and `DB_PATH` is read at import, so every test in this process shares one database;
    and `upsert_contact` deliberately leaves a field the caller did not send alone. Together
    those let `outreach_status='submitted'` from one test leak into the next, which made a
    later test pass for the wrong reason and then fail when a new one was added.
    """
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    init_db()
    conn = get_connection()
    store.init_contacts(conn)
    _touches.init_touches(conn)
    conn.execute("DELETE FROM contacts WHERE id = 'c1'")
    conn.execute("DELETE FROM jobs WHERE url = ?", (ANCHOR,))
    conn.execute("DELETE FROM touches WHERE contact_id = 'c1'")
    for sid, name in ((SRC, "Professional Network"), (DST, "Business Network")):
        if not _spaces.get_space(sid, conn):
            _spaces.create_space(sid, name, "outreach", shape="pipeline/targets", conn=conn)
        else:
            # One test sets `voice` on the destination; without this reset the next test
            # inherits it and its "identical manifests" premise is quietly false.
            from applypilot.domain import space as _sp
            _spaces.save(_sp.from_template(sid, name, "outreach", shape="pipeline/targets"), conn)
    conn.execute("INSERT OR REPLACE INTO jobs (url, title, company, site, strategy, space_id) "
                 "VALUES (?,?,?,?,?,?)", (ANCHOR, "Acme", "Acme", "Acme", "dashboard_upload", SRC))
    store.init_contacts(conn)
    store.upsert_contact({"id": "c1", "job_url": ANCHOR, "full_name": "Ian Duke",
                          "email": "ian@acme.test", "space_id": SRC,
                          "outreach_subject": "hello", "outreach_message": "a draft",
                          "outreach_status": "drafted"}, conn)
    conn.commit()
    return conn


def test_the_card_and_its_contacts_move(db):
    out = cm.apply(ANCHOR, DST, db)
    assert out["ok"], out
    assert db.execute("SELECT space_id FROM jobs WHERE url=?", (ANCHOR,)).fetchone()[0] == DST
    assert db.execute("SELECT space_id FROM contacts WHERE id='c1'").fetchone()[0] == DST


def test_the_anchor_is_never_rewritten(db):
    """The whole reason nothing orphans. `contact_id` hashes this string."""
    cm.apply(ANCHOR, DST, db)
    assert db.execute("SELECT url FROM jobs WHERE url=?", (ANCHOR,)).fetchone()[0] == ANCHOR
    assert db.execute("SELECT job_url FROM contacts WHERE id='c1'").fetchone()[0] == ANCHOR
    assert store.get_contacts_for_job(ANCHOR, db), "the card lost its people"


def test_touches_and_messages_are_not_touched(db):
    """They carry no `space_id` at all — the move must not need to know they exist."""
    _touches.init_touches(db)
    db.execute("INSERT INTO touches (contact_id, channel, seq, status, sent_at, job_url) "
               "VALUES ('c1','email',1,'sent','2026-08-01T00:00:00+00:00',?)", (ANCHOR,))
    db.commit()
    before = db.execute("SELECT COUNT(*) FROM touches WHERE contact_id='c1'").fetchone()[0]
    cm.apply(ANCHOR, DST, db)
    after = db.execute("SELECT COUNT(*) FROM touches WHERE contact_id='c1'").fetchone()[0]
    assert before == after == 1


def test_an_identical_manifest_keeps_the_draft(db):
    """The operator's real case. Discarding a draft when nothing about the voice changed would
    destroy work for no reason."""
    assert cm.plan(ANCHOR, DST, db)["discards"] == []
    cm.apply(ANCHOR, DST, db)
    assert db.execute("SELECT outreach_message FROM contacts WHERE id='c1'").fetchone()[0] == "a draft"


def test_a_voice_change_discards_the_unsent_draft_and_undo_restores_it(db):
    from dataclasses import replace
    _spaces.save(replace(_spaces.load(DST, db), voice="premise"), db)
    plan = cm.plan(ANCHOR, DST, db)
    assert plan["discards"] == ["Ian Duke"], plan
    out = cm.apply(ANCHOR, DST, db)
    assert db.execute("SELECT outreach_message FROM contacts WHERE id='c1'").fetchone()[0] == ""
    cm.undo(out["undo"], db)
    assert db.execute("SELECT outreach_message FROM contacts WHERE id='c1'").fetchone()[0] == "a draft"
    assert db.execute("SELECT space_id FROM jobs WHERE url=?", (ANCHOR,)).fetchone()[0] == SRC


def test_a_sent_draft_is_never_discarded(db):
    """A sent message is the only record of what went out."""
    from dataclasses import replace
    _spaces.save(replace(_spaces.load(DST, db), voice="premise"), db)
    store.upsert_contact({"id": "c1", "job_url": ANCHOR, "full_name": "Ian Duke",
                          "outreach_status": "submitted", "sent_message_id": "m1"}, db)
    db.commit()
    assert cm.plan(ANCHOR, DST, db)["discards"] == []
    cm.apply(ANCHOR, DST, db)
    assert db.execute("SELECT outreach_message FROM contacts WHERE id='c1'").fetchone()[0] == "a draft"


def test_undo_puts_everything_back(db):
    out = cm.apply(ANCHOR, DST, db)
    assert cm.undo(out["undo"], db)["ok"]
    assert db.execute("SELECT space_id FROM jobs WHERE url=?", (ANCHOR,)).fetchone()[0] == SRC
    assert db.execute("SELECT space_id FROM contacts WHERE id='c1'").fetchone()[0] == SRC


def test_undo_is_single_use(db):
    out = cm.apply(ANCHOR, DST, db)
    assert cm.undo(out["undo"], db)["ok"]
    assert cm.undo(out["undo"], db)["ok"] is False


def test_destinations_offer_only_same_shape_and_never_itself(db):
    ids = [d["id"] for d in cm.destinations(ANCHOR, db)]
    assert SRC not in ids
    assert DST in ids


def test_plan_refuses_before_apply_can_run(db):
    """`plan` re-checks even though the menu filters, because a guard that exists only on the
    path the UI happens to take is not a guard (§Lessons 110)."""
    assert cm.apply(ANCHOR, SRC, db)["ok"] is False
    assert cm.apply(ANCHOR, "no-such-space", db)["ok"] is False
    assert db.execute("SELECT space_id FROM jobs WHERE url=?", (ANCHOR,)).fetchone()[0] == SRC


def test_a_failure_part_way_through_leaves_nothing_moved(db, monkeypatch):
    """One transaction, or a move that dies half-done splits a card from its people.

    Forced by making the draft-clear raise, so the failure lands AFTER the job row and the
    contacts have already been updated inside the transaction — the only ordering where a
    missing rollback actually shows.
    """
    from dataclasses import replace
    _spaces.save(replace(_spaces.load(DST, db), voice="premise"), db)

    def boom(*a, **k):
        raise RuntimeError("disk went away")

    monkeypatch.setattr(store, "clear_unsent_draft", boom)
    with pytest.raises(Exception):
        cm.apply(ANCHOR, DST, db)
    assert db.execute("SELECT space_id FROM jobs WHERE url=?", (ANCHOR,)).fetchone()[0] == SRC
    assert db.execute("SELECT space_id FROM contacts WHERE id='c1'").fetchone()[0] == SRC
    assert db.execute("SELECT outreach_message FROM contacts WHERE id='c1'").fetchone()[0] == "a draft"

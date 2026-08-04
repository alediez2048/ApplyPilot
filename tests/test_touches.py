"""ARCH-3: one touches table for every channel, and the migration that fills it.

The ticket's stated acceptance test is "adding SMS is one registry row plus one prompt,
with no schema change and no new scheduling code". test_adding_a_channel_needs_no_schema_change
is that test, executed rather than asserted in prose.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import applypilot.database as database
from applypilot.domain.followup import CHANNELS, EMPTY_LADDER, Channel, channel_schedule, touch_state
from applypilot.networking import backfill_touches as B
from applypilot.networking import store, touches

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


def ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat()


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


def _contact(conn, name="Jane Doe", **kw) -> str:
    row = {"job_url": "http://j/1", "full_name": name, "email": f"{name[0]}@x.com",
           "linkedin_url": f"https://l/in/{name.split()[0].lower()}"}
    row.update(kw)
    return store.upsert_contact(row, conn)


# ── the table ───────────────────────────────────────────────────────────────

def test_a_touch_records_and_counts(db):
    cid = _contact(db)
    assert touches.record_sent(cid, "email", conn=db) == 1
    assert touches.record_sent(cid, "email", conn=db) == 2
    st = touches.ladder_state(cid, "email", db)
    assert st["count"] == 2 and st["last_sent_at"]


def test_channels_do_not_share_a_counter(db):
    """`seq` is per (contact, channel). A global counter would label LinkedIn's first
    touch "touch 3 of 2" for anyone who had already had two emails."""
    cid = _contact(db)
    touches.record_sent(cid, "email", conn=db)
    touches.record_sent(cid, "email", conn=db)
    assert touches.record_sent(cid, "linkedin", conn=db) == 1
    assert touches.ladder_state(cid, "email", db)["count"] == 2


def test_draft_then_send_consumes_the_draft(db):
    cid = _contact(db)
    touches.set_draft(cid, "email", "Re: hi", "body text")
    assert touches.ladder_state(cid, "email", db)["draft_body"] == "body text"
    touches.record_sent(cid, "email", conn=db)
    st = touches.ladder_state(cid, "email", db)
    assert st["count"] == 1 and st["draft_body"] == ""


def test_redrafting_reuses_the_pending_row(db):
    """Otherwise "Regenerate" would silently burn a seq per click and finish the ladder."""
    cid = _contact(db)
    touches.set_draft(cid, "email", "a", "one")
    touches.set_draft(cid, "email", "b", "two")
    n = db.execute("SELECT COUNT(*) FROM touches WHERE contact_id=?", (cid,)).fetchone()[0]
    assert n == 1
    assert touches.ladder_state(cid, "email", db)["draft_body"] == "two"


def test_claim_is_atomic_and_respects_a_stopped_sequence(db):
    cid = _contact(db)
    touches.set_draft(cid, "email", "s", "b")
    assert touches.claim_send(cid, "email", db) is True
    assert touches.claim_send(cid, "email", db) is False      # already sending

    other = _contact(db, "Sam Roe")
    touches.set_draft(other, "email", "s", "b")
    touches.set_sequence_status(other, "email", "replied")
    assert touches.claim_send(other, "email", db) is False    # sequence is terminal


def test_the_two_lifecycles_are_independent(db):
    """The whole reason for two tables: a touch's delivery state and the sequence's
    terminal state used to be one column, and `claim_followup_send` guarded on both
    in a single condition."""
    cid = _contact(db)
    touches.set_draft(cid, "email", "s", "b")
    touches.set_sequence_status(cid, "email", "replied")
    st = touches.ladder_state(cid, "email", db)
    assert st["sequence_status"] == "replied"   # the conversation ended
    assert st["touch_status"] == "drafted"      # the draft still exists, unsent


def test_failed_send_can_be_retried(db):
    cid = _contact(db)
    touches.set_draft(cid, "email", "s", "b")
    touches.claim_send(cid, "email", db)
    touches.mark_failed(cid, "email", "smtp exploded", db)
    assert touches.ladder_state(cid, "email", db)["error"] == "smtp exploded"
    assert touches.claim_send(cid, "email", db) is True       # claimable again


def test_bulk_load_matches_single_load(db):
    a, b = _contact(db), _contact(db, "Sam Roe")
    touches.record_sent(a, "email", conn=db)
    touches.set_sequence_status(b, "linkedin", "stopped")
    bulk = touches.ladder_states([a, b], db)
    assert bulk[(a, "email")] == touches.ladder_state(a, "email", db)
    assert bulk[(b, "linkedin")]["sequence_status"] == "stopped"


def test_deleting_a_contact_takes_its_touches(db):
    cid = _contact(db)
    touches.record_sent(cid, "email", conn=db)
    touches.set_sequence_status(cid, "email", "stopped")
    touches.delete_for_contact(cid, db)
    assert db.execute("SELECT COUNT(*) FROM touches WHERE contact_id=?", (cid,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM sequences WHERE contact_id=?", (cid,)).fetchone()[0] == 0


# ── the acceptance test the ticket actually asks for ────────────────────────

def test_adding_a_channel_needs_no_schema_change(db):
    """"Adding a channel is one registry row + one prompt" — executed, not asserted in prose.

    This defines a channel that does not exist anywhere in the codebase and drives it
    end to end: storage, scheduling, terminal state. Nothing is registered, no table is
    altered, no branch is added. If a future change makes `touches` or the ladder engine
    channel-aware, this is the test that fails.

    It used to use SMS. SMS shipped, which broke this in a way worth recording: the fake
    channel declared `default_schedule=(24, 72)`, but `channel_schedule()` resolves through
    the settings registry, and the moment SMS_FOLLOWUP_SCHEDULE became a REAL setting the
    fake channel silently inherited the real [72, 168] and the arithmetic below stopped
    holding. A test that proves "an unknown channel works" has to name one that is actually
    unknown, so this is WhatsApp — which the codebase has never heard of.
    """
    whatsapp = Channel(
        name="whatsapp",
        env_var="WHATSAPP_FOLLOWUP_SCHEDULE",   # deliberately NOT in settings.py
        default_schedule=(24, 72),
        start_field="submitted_at",
        ready=(("phone", None),),
        can_autosend=False,
        prefix="wa_",
    )
    cid = _contact(db, phone="+1 555 0100")
    contact = dict(store.get_contact(cid))

    # storage: same table, same functions, a value in a column
    touches.set_draft(cid, whatsapp.name, "", "quick nudge")
    assert touches.ladder_state(cid, whatsapp.name, db)["draft_body"] == "quick nudge"
    assert touches.record_sent(cid, whatsapp.name, conn=db) == 1

    # scheduling: the same engine, no new code. `record_sent` stamps the real clock, so
    # the evaluation time has to be relative to it, not to this module's frozen NOW.
    later = datetime.now(timezone.utc) + timedelta(hours=73)
    contact["submitted_at"] = ago(days=10)
    state, _ = touch_state(contact, whatsapp, channel_schedule(whatsapp), later,
                           touches.ladder_state(cid, whatsapp.name, db))
    assert state == "due", "second SMS touch should be due 72h after the first"

    # terminal state: the same table
    touches.set_sequence_status(cid, whatsapp.name, "replied")
    assert touch_state(contact, whatsapp, channel_schedule(whatsapp), later,
                       touches.ladder_state(cid, whatsapp.name, db))[0] == "replied"

    # readiness is data: no phone, no ladder
    assert touch_state({**contact, "phone": ""}, whatsapp, channel_schedule(whatsapp), later,
                       EMPTY_LADDER)[0] == ""

    # and the schema never learned it exists
    cols = {r[1] for r in db.execute("PRAGMA table_info(touches)")}
    assert not any("whatsapp" in c for c in cols)


def test_no_channel_specific_ladder_functions():
    """`mark_li_followup_sent` / `set_li_followup_draft` / `set_li_sequence_status` are gone.

    Their existence was the duplication ARCH-3 removes; a new one appearing means the
    per-channel copy-paste is growing back.
    """
    leaked = [n for n in dir(store) if n.startswith(("mark_li_followup", "set_li_followup",
                                                     "set_li_sequence"))]
    assert not leaked, f"channel-specific ladder functions are back: {leaked}"
    for name in ("record_sent", "set_draft", "claim_send", "set_sequence_status"):
        assert "channel" in touches.__dict__[name].__code__.co_varnames, \
            f"touches.{name} must take a channel rather than assuming one"


def test_every_registered_channel_round_trips(db):
    """Parameterised over the real registry, so a new channel is covered the day it lands."""
    cid = _contact(db, dm_status="manual")
    for ch in CHANNELS:
        touches.set_draft(cid, ch.name, "s", "b")
        assert touches.record_sent(cid, ch.name, conn=db) == 1
        assert touches.ladder_state(cid, ch.name, db)["count"] == 1


# ── the backfill ────────────────────────────────────────────────────────────

def _legacy_schema(conn) -> None:
    """Recreate the pre-ARCH-3 columns.

    A fresh database no longer has them — that is the point of the ticket. The backfill
    must still be exercised against the shape it was written for, so these tests build an
    OLD database on purpose rather than relying on one lingering in the schema.
    """
    existing = {r[1] for r in conn.execute("PRAGMA table_info(contacts)")}
    for col in B.LEGACY_COLUMNS:
        if col not in existing:
            kind = "INTEGER DEFAULT 0" if col.endswith("_count") else "TEXT"
            conn.execute(f"ALTER TABLE contacts ADD COLUMN {col} {kind}")
    conn.commit()


def _legacy_contact(conn, name, **cols) -> str:
    _legacy_schema(conn)
    cid = _contact(conn, name)
    sets = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE contacts SET {sets} WHERE id = ?", (*cols.values(), cid))
    conn.commit()
    return cid


def test_a_migrated_database_has_no_ladder_columns_left(db):
    """The acceptance criterion, pinned: 42 columns down to 32, and the ten stay gone.

    `ensure_contacts_columns` re-adds anything still listed in `_CONTACT_COLUMNS`, so
    leaving one behind there would silently resurrect all ten on the next startup.

    The total is pinned too, so a new column is a DELIBERATE edit here rather than something
    that drifts in. 33 since CRM-1 added `replied_at` — which is a date for the UI and for
    time_to_reply, NOT ladder state: the halt still goes through `sequences`.

    **37** since intro-deck tracking added `deck_slug` / `deck_viewed_at` / `deck_last_at` /
    `deck_views`.
    Those are engagement facts about a CLICK, not ladder state either — nothing in
    `domain/followup.py` reads them, and a deck view neither starts nor stops a sequence.
    Note what is deliberately absent: no `deck_token` column. The token is DERIVED from the
    contact id (`domain/deck.py`), so it survives a restore and cannot drift from the links
    already sitting in somebody's inbox.
    """
    store.init_contacts(db)
    cols = {r[1] for r in db.execute("PRAGMA table_info(contacts)")}
    assert not [c for c in cols if "followup" in c or "followed_up" in c]
    assert "replied_at" in cols
    # `sms_sent_at` is the ONE column the SMS channel added, and it is the same kind of fact as
    # `dm_sent_at`: proof a first message went out on that channel. Everything after it is a
    # `touches` row, which is why adding a whole channel cost one column and not ten.
    assert "sms_sent_at" in cols
    # `noticed` is what the operator saw on the profile — the personalisation input a LinkedIn
    # scraper was considered for and rejected (§Lessons 3). Kept separate from `notes`, which is
    # scratch and would be noise in a drafting prompt.
    assert "noticed" in cols
    # `draft_variant` records WHAT PRODUCED a draft, so reply rate can be attributed. Without it
    # reply rate is one number that moves for reasons nobody can name.
    assert "draft_variant" in cols
    # SPACE-1a: membership, and the only key that decides which panel a row appears in. NOT
    # ladder state and not part of `contact_id` — hashing it would re-key every contact and
    # detach exactly the touches this test exists to protect.
    assert "space_id" in cols
    assert len(cols) == 41, f"unexpected contacts columns: {sorted(cols)}"


def test_backfill_moves_state_and_verifies_clean(db):
    a = _legacy_contact(db, "Ali C", followup_count=1, followed_up_at=ago(days=2),
                        followup_status="sent")
    b = _legacy_contact(db, "Sumit S", followup_status="replied")
    c = _legacy_contact(db, "Lin K", li_followup_count=2, li_followed_up_at=ago(days=1),
                        li_followup_status="sent")
    B.apply(db)
    assert B.verify(db) == []
    assert touches.ladder_state(a, "email", db)["count"] == 1
    assert touches.ladder_state(b, "email", db)["sequence_status"] == "replied"
    assert touches.ladder_state(c, "linkedin", db)["count"] == 2


def test_backfill_is_idempotent(db):
    _legacy_contact(db, "Ali C", followup_count=2, followed_up_at=ago(days=2),
                    followup_status="sent")
    B.apply(db)
    first = db.execute("SELECT COUNT(*) FROM touches").fetchone()[0]
    B.apply(db)
    B.apply(db)
    assert db.execute("SELECT COUNT(*) FROM touches").fetchone()[0] == first


def test_backfill_preserves_an_unsent_draft(db):
    cid = _legacy_contact(db, "Ali C", followup_status="drafted",
                          followup_subject="Re: hi", followup_message="still keen")
    B.apply(db)
    st = touches.ladder_state(cid, "email", db)
    assert st["count"] == 0 and st["draft_body"] == "still keen"
    assert st["touch_status"] == "drafted"


def test_verify_is_not_vacuous(db):
    """A verifier that cannot fail is worse than none — this migration is irreversible
    once the columns are dropped, and `verify` is the only thing standing between the
    two representations."""
    cid = _legacy_contact(db, "Ali C", followup_count=1, followed_up_at=ago(days=2),
                          followup_status="sent")
    B.apply(db)
    assert B.verify(db) == []
    db.execute("DELETE FROM touches WHERE contact_id = ?", (cid,))
    db.commit()
    assert B.verify(db), "verify did not notice the touch row vanishing"

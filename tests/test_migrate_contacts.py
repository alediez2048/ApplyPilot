"""CO-2 — moving contacts from a dead role to a live one.

The Google case this was measured on: *Startups Performance Lead* is cancelled with 16 contacts
(7 emailed, 1 replied) and *AI Sales Specialist* is live with 1. The move carries 25 messages,
13 touches and 5 sequences, and `touches`/`sequences` have no `job_url` at all — they follow
`contact_id` blindly, which is what makes this the only operation in the app that can silently
destroy a ladder.

The measurement that decided the design: **16 of 16** stored messages and drafts name the
cancelled role by name. So the naive build — rewrite the foreign keys and stop — leaves unsent
drafts pitching a dead role one click from being sent, and ladders whose next touch follows up
on a job that no longer exists. Every row in the right place and the feature worse than not
having it.
"""

from __future__ import annotations

import pytest

import applypilot.database as database
from applypilot.networking import interactions_store as _inter
from applypilot.networking import messages as _msg
from applypilot.networking import migrate, store, touches as _touches
from applypilot.repo import jobs as _jobs

DEAD = "https://boards.example.com/google/startups-performance-lead"
LIVE = "https://boards.example.com/google/ai-sales-specialist"
OTHER = "https://boards.example.com/acme/staff-engineer"
ME = "me@work.test"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    _msg.init_messages(conn)
    _touches.init_touches(conn)
    _inter.init_interactions(conn)
    _jobs.insert_imported(DEAD, "Startups Performance Lead", "Google", "Google", DEAD, conn)
    _jobs.insert_imported(LIVE, "AI Sales Specialist", "Google", "Google", LIVE, conn)
    _jobs.insert_imported(OTHER, "Staff Engineer", "Acme", "Acme", OTHER, conn)
    return conn


def _person(conn, *, job=DEAD, name="Carol Reed", email="carol@google.test", **kw):
    return store.upsert_contact({"job_url": job, "full_name": name, "email": email,
                                 "company": "Google", **kw}, conn)


def _emailed(conn, **kw):
    """Somebody a real cold email went out to, mid-ladder — the live 6."""
    cid = _person(conn, outreach_status="submitted", submitted_at="2026-08-01T09:00",
                  sent_message_id="gm1", outreach_subject="quick question about the Startups "
                  "Performance Lead role", outreach_message="Hey Carol, I just applied for the "
                  "Startups Performance Lead role at Google Cloud...", **kw)
    _msg.upsert_messages([{
        "message_id": "gm1", "thread_id": "t1", "contact_id": cid, "job_url": DEAD,
        "direction": "out", "from_addr": ME, "from_name": "", "to_addrs": ["carol@google.test"],
        "cc_addrs": [], "subject": "quick question about the Startups Performance Lead role",
        "sent_at": "2026-08-01T09:00", "rfc_message_id": "<gm1>", "snippet": "I just applied"}],
        conn)
    return cid


# ── the ladder, which is the half that can be destroyed silently ────────────

def test_a_moved_ladder_does_not_arrive_FINISHED(db):
    """The whole reason `touches.job_url` exists.

    The email schedule has three entries, so three sent touches carried onto a new role make
    `count >= len(schedule)` immediately: the channel reads `finished` and never follows up
    again. That is the ladder being destroyed with nothing raising.
    """
    from applypilot.domain import followup as fu
    cid = _emailed(db)
    for seq in (1, 2, 3):
        _touches.record_sent(cid, "email", conn=db)
    assert _touches.ladder_state(cid, "email", db)["count"] == 3

    got = migrate.apply(DEAD, LIVE, [cid], db)
    assert got["ok"], got.get("error")
    new_id = store.contact_id(LIVE, None, "Carol Reed")
    assert _touches.ladder_state(new_id, "email", db)["count"] == 0, \
        "the old role's touches are being counted against the new role's plan"

    c = store.get_contact(new_id, db)
    state, _ = fu.touch_state(fu.normalize_for_ladder(c), fu.EMAIL,
                              [48, 96, 168], _now(), _touches.ladder_state(new_id, "email", db))
    assert state == "", f"the new card claims a follow-up state of {state!r} on arrival"


def test_a_moved_TEXT_ladder_does_not_come_due_on_arrival(db):
    """The mutation that survived a first pass, and the SMS shape all over again.

    Email is covered twice over — `emailed` is False for a moved contact, so `_is_ready` fails
    before any anchor is read. SMS's readiness is `(phone, sms_sent_at)` and touches NEITHER of
    those, so blanking the anchors is the only thing standing between a moved contact and a text
    that comes due the instant they land, about a role that no longer exists.

    Written by driving CHANNELS rather than naming email, which is what `followup_panel` got
    wrong: a third channel passed correctly through the whole engine and then vanished at the
    return statement.
    """
    from applypilot.domain import followup as fu
    # No touch row at all, deliberately: the FIRST text is the one anchored on `sms_sent_at`,
    # so this is the state where the column is the only thing the schedule has to read.
    cid = _person(db, phone="+15125550101", sms_sent_at="2026-08-01T09:00")
    before, _ = fu.touch_state(fu.normalize_for_ladder(store.get_contact(cid, db)), fu.SMS,
                               [72, 168], _now(), _touches.ladder_state(cid, "sms", db))
    assert before == "due", "the fixture never had a live text ladder to begin with"

    migrate.apply(DEAD, LIVE, [cid], db)
    new_id = store.contact_id(LIVE, None, "Carol Reed")
    after, _ = fu.touch_state(fu.normalize_for_ladder(store.get_contact(new_id, db)), fu.SMS,
                              [72, 168], _now(), _touches.ladder_state(new_id, "sms", db))
    assert after == "", f"a text about the dead role reads as {after!r} on the new card"
    assert store.get_contact(new_id, db)["sms_sent_at"] == "2026-08-01T09:00", \
        "the stored proof of a real text was destroyed rather than scoped"


def test_the_touches_are_still_READABLE_as_history(db):
    """The other half, and the two must disagree: `sent_touches` answers "what have we ever said
    to this person" and hands it to the drafter so it does not repeat itself."""
    cid = _emailed(db)
    _touches.set_draft(cid, "email", "following up", "circling back on the Startups role", db)
    _touches.record_sent(cid, "email", conn=db)
    migrate.apply(DEAD, LIVE, [cid], db)
    new_id = store.contact_id(LIVE, None, "Carol Reed")
    history = _touches.sent_touches(new_id, "email", db)
    assert len(history) == 1 and "circling back" in history[0]["body"]


def test_a_touch_id_is_RECOMPUTED_not_carried(db):
    """`touches.id` is sha1(contact|channel|seq). Left alone, the next `_upsert_touch` derives
    an id matching no row, inserts, and hits the unique index on (contact_id, channel, seq)."""
    cid = _emailed(db)
    _touches.record_sent(cid, "email", conn=db)
    migrate.apply(DEAD, LIVE, [cid], db)
    new_id = store.contact_id(LIVE, None, "Carol Reed")
    row = db.execute("SELECT id, seq FROM touches WHERE contact_id = ?", (new_id,)).fetchone()
    assert row["id"] == _touches.touch_id(new_id, "email", row["seq"])
    _touches.record_sent(new_id, "email", conn=db)          # must not raise
    assert db.execute("SELECT COUNT(*) FROM touches WHERE contact_id = ?",
                      (new_id,)).fetchone()[0] == 2


def test_no_touch_is_left_ORPHANED(db):
    cid = _emailed(db)
    _touches.record_sent(cid, "email", conn=db)
    migrate.apply(DEAD, LIVE, [cid], db)
    left = db.execute("SELECT COUNT(*) FROM touches WHERE contact_id = ?", (cid,)).fetchone()[0]
    assert left == 0, "touches still point at a contact id that no longer exists"


def test_an_untouched_contacts_ladder_is_UNAFFECTED_by_the_new_column(db):
    """All 233 live touch rows have an empty `job_url`, which must keep meaning "this contact's
    own job". A filter that dropped them would silently reset every ladder in the app."""
    cid = _emailed(db)
    _touches.record_sent(cid, "email", conn=db)
    db.execute("UPDATE touches SET job_url = NULL")       # exactly the pre-CO-2 shape
    db.commit()
    assert _touches.ladder_state(cid, "email", db)["count"] == 1


# ── what is destroyed and what is not ───────────────────────────────────────

def test_a_SENT_email_survives_the_move_whole(db):
    """Clearing `submitted_at` and `sent_message_id` would read correctly on the new card and
    drop the send out of the CRM-2 funnel while its reply stayed in it, and disarm the cross-job
    cooldown. §Lessons 86: a guard a legitimate write can switch off is the wrong guard."""
    cid = _emailed(db)
    migrate.apply(DEAD, LIVE, [cid], db)
    c = store.get_contact(store.contact_id(LIVE, None, "Carol Reed"), db)
    assert c["sent_message_id"] == "gm1"
    assert c["submitted_at"] == "2026-08-01T09:00"
    assert "Startups Performance Lead" in c["outreach_message"]
    assert c["outreach_job_url"] == DEAD, "the send is not stamped with the role it was for"


def test_an_UNSENT_draft_is_cleared_because_it_names_the_dead_role(db):
    cid = _person(db, outreach_status="drafted",
                  outreach_subject="the Startups Performance Lead role",
                  outreach_message="I just applied for the Startups Performance Lead role...")
    migrate.apply(DEAD, LIVE, [cid], db)
    c = store.get_contact(store.contact_id(LIVE, None, "Carol Reed"), db)
    assert c["outreach_message"] == "" and c["outreach_subject"] == ""
    assert c["outreach_status"] == "none"


def test_the_card_offers_a_FIRST_contact_not_a_follow_up(db):
    """What the operator sees, from what the SERVER produces. A moved contact whose stored
    outreach belongs to the dead role must read as not-yet-emailed HERE."""
    from applypilot import web_dashboard as wd
    cid = _emailed(db)
    migrate.apply(DEAD, LIVE, [cid], db)
    c = store.get_contact(store.contact_id(LIVE, None, "Carol Reed"), db)
    payload = wd._contact_payload(c, company="Google", ladders={}, conn_matches={})
    assert payload["emailed"] is False
    assert payload["submitted_at"] == ""
    assert payload["outreach_from_job"] == DEAD


def test_a_contact_that_never_moved_still_reads_as_emailed(db):
    """The negative case. `outreach_job_url` is empty on all 352 live contacts, so the scoping
    must be invisible until somebody is actually moved."""
    from applypilot import web_dashboard as wd
    c = store.get_contact(_emailed(db), db)
    payload = wd._contact_payload(c, company="Google", ladders={}, conn_matches={})
    assert payload["emailed"] is True and payload["submitted_at"] == "2026-08-01T09:00"


def test_messages_follow_the_person(db):
    """The operator's own answer: continuity is the point, and every message's subject already
    says which role it was about."""
    cid = _emailed(db)
    migrate.apply(DEAD, LIVE, [cid], db)
    new_id = store.contact_id(LIVE, None, "Carol Reed")
    thread = _msg.thread_for_contact(new_id, db)
    assert len(thread) == 1
    assert _msg.threads_for_job(LIVE, db).get(new_id), "the new card cannot see the history"


def test_replied_at_survives_and_submitted_at_is_not_wiped(db):
    cid = _emailed(db, replied_at="2026-08-04T10:00")
    migrate.apply(DEAD, LIVE, [cid], db)
    c = store.get_contact(store.contact_id(LIVE, None, "Carol Reed"), db)
    assert c["replied_at"] == "2026-08-04T10:00"


def test_the_contact_lands_in_the_destinations_SPACE(db):
    """§Lessons 70 at a fourth write path: 14 live contacts already disagree with their job's
    Space because a write path left the column DEFAULT to decide.

    BOTH jobs are moved to `gauntlet` — a cross-Space move is refused outright now (the same
    person tracked in two campaigns is not a duplicate), so the guarantee under test is the one
    that survives: the contact takes the job's Space rather than the column default of
    `job-search`.
    """
    db.execute("UPDATE jobs SET space_id = 'gauntlet' WHERE url IN (?, ?)", (LIVE, DEAD))
    db.commit()
    cid = _person(db)
    migrate.apply(DEAD, LIVE, [cid], db)
    c = store.get_contact(store.contact_id(LIVE, None, "Carol Reed"), db)
    assert c["space_id"] == "gauntlet"


# ── who may move ────────────────────────────────────────────────────────────

def test_somebody_with_no_email_does_NOT_move(db):
    """The operator's rule, and their reason: the premise is continuing an outreach that already
    began. Live that excludes 5 of 16 — shown with the reason, never hidden."""
    cid = _person(db, email="", name="Nameless Nick")
    got = migrate.plan(DEAD, LIVE, db)
    assert [r["id"] for r in got["movable"]] == []
    assert got["excluded"][0]["why"] == migrate.NO_EMAIL
    assert migrate.apply(DEAD, LIVE, [cid], db)["ok"] is False


def test_a_different_employer_is_refused(db):
    got = migrate.plan(DEAD, OTHER, db)
    assert got["ok"] is False and "different employers" in got["error"]


def test_apply_re_derives_the_plan_and_will_not_move_an_excluded_person(db):
    """A stale dialog must not be able to move somebody the preview did not offer."""
    cid = _person(db, email="")
    assert migrate.apply(DEAD, LIVE, [cid], db)["ok"] is False
    assert store.get_contact(cid, db)["job_url"] == DEAD


def test_the_groups_are_the_three_the_dialog_shows(db):
    _emailed(db, name="Carol Reed", email="carol@google.test")
    _emailed(db, name="Pat Ryan", email="pat@google.test", replied_at="2026-08-05T08:00")
    _person(db, name="Sam Lowe", email="sam@google.test")
    got = migrate.plan(DEAD, LIVE, db)
    assert [len(got["groups"][k]) for k in ("replied", "emailed", "fresh")] == [1, 1, 1]


# ── collisions ──────────────────────────────────────────────────────────────

def test_the_richer_row_wins_and_the_empty_duplicate_goes(db):
    """The live one: `omalleyp@google.com` is on BOTH jobs. The destination row is `drafted`
    with zero messages; the source has the whole conversation."""
    src = _emailed(db, name="Patrick", email="p@google.test")
    dst = _person(db, job=LIVE, name="Patrick Omalley", email="p@google.test",
                  outreach_status="drafted")
    assert src != dst
    got = migrate.apply(DEAD, LIVE, [src], db)
    assert got["ok"], got.get("error")
    assert store.get_contact(dst, db) is None, "the empty duplicate is still there"
    kept = store.get_contact(store.contact_id(LIVE, None, "Patrick"), db)
    assert len(_msg.thread_for_contact(kept["id"], db)) == 1


def test_two_real_conversations_are_REFUSED_rather_than_interleaved(db):
    """Merging them is unrecoverable, and live there are zero such pairs — so refusing costs
    nothing today and leaves the operator deciding."""
    src = _emailed(db, name="Patrick", email="p@google.test")
    other = _person(db, job=LIVE, name="Patrick Omalley", email="p@google.test")
    _msg.upsert_messages([{
        "message_id": "gm9", "thread_id": "t9", "contact_id": other, "job_url": LIVE,
        "direction": "in", "from_addr": "p@google.test", "from_name": "Patrick",
        "to_addrs": [ME], "cc_addrs": [], "subject": "re: the AI role",
        "sent_at": "2026-08-09T09:00", "rfc_message_id": "<gm9>", "snippet": "hi"}], db)
    got = migrate.plan(DEAD, LIVE, db)
    assert [r["id"] for r in got["movable"]] == []
    # "SEPARATE", not merely "both": the destination holds a message the source does not, which
    # is what distinguishes a second exchange from one conversation the address-based Gmail sync
    # filed under two contact rows.
    assert "SEPARATE" in got["refused"][0]["why"]
    assert migrate.apply(DEAD, LIVE, [src], db)["ok"] is False


def test_the_destination_row_wins_when_it_is_the_fuller_one(db):
    src = _person(db, name="Patrick", email="p@google.test")
    dst = _person(db, job=LIVE, name="Patrick Omalley", email="p@google.test")
    _msg.upsert_messages([{
        "message_id": "gm9", "thread_id": "t9", "contact_id": dst, "job_url": LIVE,
        "direction": "in", "from_addr": "p@google.test", "from_name": "Patrick",
        "to_addrs": [ME], "cc_addrs": [], "subject": "re", "sent_at": "2026-08-09T09:00",
        "rfc_message_id": "<gm9>", "snippet": "hi"}], db)
    got = migrate.plan(DEAD, LIVE, db)
    assert [r["id"] for r in got["movable"]] == []
    assert "fuller record" in got["excluded"][0]["why"]
    assert store.get_contact(src, db) is not None


# ── undo ────────────────────────────────────────────────────────────────────

def test_undo_puts_everything_back(db):
    cid = _emailed(db)
    _touches.record_sent(cid, "email", conn=db)
    _inter.record(cid, "booked", at="2026-08-06T12:00", job_url=DEAD, conn=db)
    before = dict(store.get_contact(cid, db))

    token = migrate.apply(DEAD, LIVE, [cid], db)["undo"]
    assert migrate.undo(token, db)["ok"]

    after = store.get_contact(cid, db)
    assert after is not None, "the original contact id was not restored"
    assert after["job_url"] == DEAD and after["outreach_job_url"] == ""
    assert after["outreach_message"] == before["outreach_message"]
    assert _touches.ladder_state(cid, "email", db)["count"] == 1
    assert len(_msg.thread_for_contact(cid, db)) == 1
    assert db.execute("SELECT COUNT(*) FROM interactions WHERE contact_id = ?",
                      (cid,)).fetchone()[0] == 1


def test_undo_restores_a_cleared_draft(db):
    cid = _person(db, outreach_status="drafted", outreach_subject="the Startups role",
                  outreach_message="I just applied for the Startups Performance Lead role...")
    token = migrate.apply(DEAD, LIVE, [cid], db)["undo"]
    migrate.undo(token, db)
    c = store.get_contact(cid, db)
    assert c["outreach_message"].startswith("I just applied")
    assert c["outreach_status"] == "drafted"


def test_undo_restores_a_deleted_duplicate_whole(db):
    src = _emailed(db, name="Patrick", email="p@google.test")
    dst = _person(db, job=LIVE, name="Patrick Omalley", email="p@google.test",
                  outreach_status="drafted", outreach_message="draft on the live role")
    token = migrate.apply(DEAD, LIVE, [src], db)["undo"]
    migrate.undo(token, db)
    back = store.get_contact(dst, db)
    assert back is not None and back["outreach_message"] == "draft on the live role"


def test_a_token_can_only_be_spent_ONCE(db):
    cid = _person(db)
    token = migrate.apply(DEAD, LIVE, [cid], db)["undo"]
    assert migrate.undo(token, db)["ok"]
    assert migrate.undo(token, db)["ok"] is False


# ── the target list ─────────────────────────────────────────────────────────

def test_targets_are_same_employer_and_exclude_closed_rows(db):
    assert [t["url"] for t in migrate.targets_for(DEAD, db)] == [LIVE]
    db.execute("UPDATE jobs SET rejected_at = '2026-08-01', apply_status = 'cancelled' "
               "WHERE url = ?", (LIVE,))
    db.commit()
    assert migrate.targets_for(DEAD, db) == []


def _now():
    from datetime import datetime, timezone
    return datetime(2026, 8, 13, tzinfo=timezone.utc)


# ── one conversation stored twice is NOT two conversations ──────────────────

def test_a_COPIED_conversation_does_not_block_the_merge(db):
    """The live WebAI case, and the reason the refusal was comparing the wrong thing.

    `replies.sync_all_with()` searches Gmail by ADDRESS and files what it finds under whichever
    contact row asked, so the same thread lands on BOTH rows of a duplicated person with
    identical message ids. Counting rows called that two conversations and refused — blocking the
    exact repair the feature exists for. Measured: both WebAI pairs hold the SAME 4 messages.
    """
    src = _emailed(db, name="Marcus", email="m@google.test")
    dst = _person(db, job=LIVE, name="Marcus Godin", email="m@google.test")
    # The identical message, filed under the second row too — what the address search does.
    _msg.upsert_messages([{
        "message_id": "gm1", "thread_id": "t1", "contact_id": dst, "job_url": LIVE,
        "direction": "out", "from_addr": ME, "from_name": "", "to_addrs": ["m@google.test"],
        "cc_addrs": [], "subject": "quick question about the Startups Performance Lead role",
        "sent_at": "2026-08-01T09:00", "rfc_message_id": "<gm1>", "snippet": "I just applied"}],
        db)
    got = migrate.plan(DEAD, LIVE, db)
    assert got["refused"] == [], f"a copied thread was treated as a second conversation: {got['refused']}"
    assert [r["id"] for r in got["movable"]] == [src]
    assert got["collisions"][0]["keeps"] == "moved"
    assert migrate.apply(DEAD, LIVE, [src], db)["ok"]
    assert store.get_contact(dst, db) is None


def test_a_GENUINELY_separate_conversation_is_still_refused(db):
    """The negative control. Without it, the fix above collapses into "never refuse", and two
    real exchanges get interleaved unrecoverably."""
    src = _emailed(db, name="Marcus", email="m@google.test")
    dst = _person(db, job=LIVE, name="Marcus Godin", email="m@google.test")
    _msg.upsert_messages([{
        "message_id": "OTHER", "thread_id": "t9", "contact_id": dst, "job_url": LIVE,
        "direction": "in", "from_addr": "m@google.test", "from_name": "Marcus",
        "to_addrs": [ME], "cc_addrs": [], "subject": "different thread entirely",
        "sent_at": "2026-08-09T09:00", "rfc_message_id": "<o>", "snippet": "hi"}], db)
    got = migrate.plan(DEAD, LIVE, db)
    assert [r["id"] for r in got["movable"]] == []
    assert "SEPARATE" in got["refused"][0]["why"]
    assert migrate.apply(DEAD, LIVE, [src], db)["ok"] is False


# ── pulling FROM the live role, which is where the duplicate is noticed ─────

def test_sources_for_lists_the_roles_worth_pulling_FROM(db):
    """`targets_for`'s mirror. The duplicate is noticed on the LIVE role ("2 of these are
    already on another role here"), and offering only an outward move meant navigating to a
    different card to act on what you had just read (§Lessons 89)."""
    _emailed(db, name="Marcus", email="m@google.test")
    got = migrate.sources_for(LIVE, db)
    assert [s["url"] for s in got] == [DEAD]
    assert got[0]["people"] == 1


def test_a_CLOSED_role_is_included_as_a_source(db):
    """The opposite of `targets_for`, deliberately: a cancelled role is exactly the thing you
    pull people off."""
    _emailed(db, name="Marcus", email="m@google.test")
    db.execute("UPDATE jobs SET rejected_at='2026-08-01', apply_status='cancelled' WHERE url=?",
               (DEAD,))
    db.commit()
    got = migrate.sources_for(LIVE, db)
    assert [s["url"] for s in got] == [DEAD] and got[0]["closed"] is True


def test_a_role_with_NOBODY_on_it_is_not_offered_as_a_source(db):
    assert migrate.sources_for(LIVE, db) == []


def test_a_different_employer_is_never_a_source(db):
    _person(db, job=OTHER, name="Someone", email="s@acme.test")
    assert migrate.sources_for(LIVE, db) == []


def test_a_moved_contact_is_not_offered_the_OLD_ROLES_copy(db):
    """Found on the live WebAI cards after a real move, and it is the miscommunication the whole
    feature exists to prevent.

    Two correct rules interacted badly. `outreach_job_url` scoping makes `emailed` read False on
    the new card — right, that outreach was for the other role — so the card offers a first
    contact. And the sent copy is preserved — right, it is the only record of what went out.
    Together they pre-filled the fresh compose box with the dead role's words: three contacts
    holding "I just applied for the Forward Deployed Engineer role" on the AI Software Engineer
    card, one click from sending.

    The text is not destroyed. It still renders in the conversation. It just is not this role's
    draft.
    """
    from applypilot import web_dashboard as wd
    cid = _emailed(db)
    migrate.apply(DEAD, LIVE, [cid], db)
    new_id = store.contact_id(LIVE, None, "Carol Reed")
    c = store.get_contact(new_id, db)
    # The record survives in the database — that is the half that must NOT change.
    assert "Startups Performance Lead" in (c["outreach_message"] or "")

    payload = wd._contact_payload(c, company="Google", ladders={}, conn_matches={})
    assert payload["outreach_message"] == "", "the dead role's copy is offered as a new draft"
    assert payload["outreach_subject"] == ""
    assert payload["outreach_status"] == "none"


def test_a_contact_that_never_moved_keeps_its_draft(db):
    """The negative control. Scoping this must not blank the draft on the 373 rows that have
    never been moved — which is all of them until the button is used."""
    from applypilot import web_dashboard as wd
    c = store.get_contact(_person(db, outreach_status="drafted",
                                  outreach_message="hello there"), db)
    payload = wd._contact_payload(c, company="Google", ladders={}, conn_matches={})
    assert payload["outreach_message"] == "hello there"
    assert payload["outreach_status"] == "drafted"

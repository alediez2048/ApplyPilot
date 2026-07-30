"""Removing a contact discovery got wrong.

Verification deliberately errs towards KEEPING an unconfirmed person — dropping a real contact
is worse than showing a doubtful one — so people who do not work at the employer reach the
list. `store.delete_contact` existed with no endpoint and no button, so there was no way out.

The trap is `touches` / `sequences`: both are keyed by contact_id with NO foreign key. Deleting
only the contact row leaves a live follow-up ladder pointing at somebody who no longer exists —
a due count that can never be cleared. Worse, contact ids are a hash of (job, identity), so
re-running discovery on the same person reproduces the SAME id and would silently re-attach the
old sequence, including a `stopped` verdict.
"""

from __future__ import annotations

import pytest

import applypilot.database as database
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
    return conn


@pytest.fixture()
def wd(db, monkeypatch):
    from applypilot import web_dashboard
    monkeypatch.setattr(web_dashboard, "get_connection", lambda *a, **k: db)
    monkeypatch.setattr(web_dashboard, "init_db", lambda *a, **k: db)
    return web_dashboard


def _contact(conn, **over):
    c = {"job_url": "http://j/1", "full_name": "Wrong Person", "email": "x@elsewhere.com",
         "company": "Elsewhere Inc"}
    c.update(over)
    return store.upsert_contact(c, conn)


def test_a_wrong_contact_can_be_removed(wd, db):
    cid = _contact(db)
    res = wd._delete_contact(cid)
    assert res["ok"] is True and "Wrong Person" in res["message"]
    assert store.get_contact(cid, db) is None


def test_its_followup_state_goes_with_it(wd, db):
    """The actual trap. A surviving ladder means a due count nobody can ever clear."""
    cid = _contact(db)
    touches.record_sent(cid, "email", conn=db)
    touches.set_sequence_status(cid, "email", "stopped", conn=db)
    assert db.execute("SELECT COUNT(*) FROM touches WHERE contact_id=?", (cid,)).fetchone()[0]

    wd._delete_contact(cid)
    assert db.execute("SELECT COUNT(*) FROM touches WHERE contact_id=?", (cid,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM sequences WHERE contact_id=?",
                      (cid,)).fetchone()[0] == 0


def test_rediscovering_the_same_person_does_not_inherit_the_old_sequence(wd, db):
    """Ids are a hash of (job, identity), so the same person gets the SAME id on a re-run. If
    the old `stopped` row survived, the rediscovered contact would arrive already stopped."""
    cid = _contact(db)
    touches.set_sequence_status(cid, "email", "stopped", conn=db)
    wd._delete_contact(cid)

    again = _contact(db)
    assert again == cid, "id is not stable; this test no longer covers what it claims"
    assert db.execute("SELECT COUNT(*) FROM sequences WHERE contact_id=?",
                      (cid,)).fetchone()[0] == 0


def test_an_already_emailed_contact_can_still_be_removed(wd, db):
    """A wrong person may have been emailed before you noticed. Refusing to remove them would
    leave the mistake on the list permanently."""
    cid = _contact(db, sent_message_id="msg-1", outreach_status="submitted")
    assert wd._delete_contact(cid)["ok"] is True


def test_the_send_survives_in_the_activity_log(wd, db):
    """The contact row is the ONLY record that an email went out. Deleting it silently would
    erase the outreach from the job's history — it would look like it never happened."""
    cid = _contact(db, sent_message_id="msg-1", outreach_status="submitted")
    wd._delete_contact(cid)
    blob = " ".join(e["detail"] or "" for e in database.get_job_events("http://j/1", conn=db))
    assert "Wrong Person" in blob
    assert "email" in blob.lower(), blob


def test_removal_is_logged_even_when_nothing_was_sent(wd, db):
    cid = _contact(db)
    wd._delete_contact(cid)
    blob = " ".join(e["detail"] or "" for e in database.get_job_events("http://j/1", conn=db))
    assert "Removed contact" in blob


def test_an_unknown_id_is_refused(wd, db):
    assert wd._delete_contact("nope")["ok"] is False
    assert wd._delete_contact("")["ok"] is False


def test_other_contacts_on_the_job_are_untouched(wd, db):
    keep = _contact(db, full_name="Real Person", email="r@acme.com")
    drop = _contact(db, full_name="Wrong Person")
    wd._delete_contact(drop)
    assert store.get_contact(keep, db) is not None

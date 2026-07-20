"""Bulk-send tests: eligibility selection, LinkedIn single-flight batch, email bulk."""

from __future__ import annotations

import time

import applypilot.database as database
import applypilot.web_dashboard as wd
from applypilot.networking import store


def _fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", db)
    database.close_connection(db)
    database.init_db(db)
    store.init_contacts()


def _c(**over):
    base = {"job_url": "http://j/1", "full_name": "P", "email": "p@x.com",
            "email_status": "verified", "outreach_message": "hi", "outreach_status": "drafted",
            "linkedin_url": "https://linkedin.com/in/p", "linkedin_message": "hey",
            "source": "apollo"}
    base.update(over)
    return base


def _wait(pred, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


# ── eligibility ──────────────────────────────────────────────────────────────

def test_eligible_email_verified_only(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    good = store.upsert_contact(_c())
    store.upsert_contact(_c(full_name="U", email="u@x.com", email_status="unverified",
                            linkedin_url="https://linkedin.com/in/u"))
    store.upsert_contact(_c(full_name="S", email="s@x.com", outreach_status="submitted",
                            linkedin_url="https://linkedin.com/in/s"))
    ids = wd._eligible_contact_ids("http://j/1", "email", confirm_unverified=False)
    assert ids == [good]  # unverified + already-submitted excluded
    # opting into unverified widens the set
    assert len(wd._eligible_contact_ids("http://j/1", "email", confirm_unverified=True)) == 2


def test_eligible_linkedin_ready_only(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    a = store.upsert_contact(_c())
    store.upsert_contact(_c(full_name="N", linkedin_url="", email="n@x.com"))  # no LI url
    sent = store.upsert_contact(_c(full_name="D", linkedin_url="https://linkedin.com/in/d",
                                   email="d@x.com"))
    store.claim_dm_send(sent)
    store.mark_dm_sent(sent)  # already sent → excluded
    ids = wd._eligible_contact_ids("http://j/1", "linkedin")
    assert ids == [a]


# (LinkedIn sending automation was removed — it's now a client-side "copy note + open
#  profile" button; the user pastes and clicks Send in their own browser. Nothing to test
#  server-side. Email bulk below still runs through the backend.)


# ── email bulk: sends + counts, honors skip ──────────────────────────────────

def test_bulk_email_runs_and_counts(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    ids = [store.upsert_contact(_c(full_name=f"E{i}", email=f"e{i}@x.com",
                                   linkedin_url=f"https://linkedin.com/in/e{i}")) for i in range(3)]

    def fake_send_outreach(cid, confirm_unverified=False):
        time.sleep(0.05)  # keep the job "running" long enough to test single-flight
        return {"ok": cid != ids[1], "message": "x"}  # middle one "skipped"

    monkeypatch.setattr("applypilot.networking.gmail_send.send_outreach", fake_send_outreach)

    runner = wd.BulkEmailRunner()
    ok, _ = runner.start("http://j/1", ids, confirm_unverified=False)
    assert ok
    ok2, msg2 = runner.start("http://j/1", ids, confirm_unverified=False)
    assert not ok2 and "already running" in msg2

    assert _wait(lambda: not runner.status("http://j/1").get("running"))
    st = runner.status("http://j/1")
    assert st["sent"] == 2 and st["skipped"] == 1


def test_bulk_runners_reject_empty():
    assert wd.BulkEmailRunner().start("http://j/1", [], confirm_unverified=False)[0] is False

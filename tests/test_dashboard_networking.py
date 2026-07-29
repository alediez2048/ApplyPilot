"""NET-2 tests: dashboard contact payload, Origin guard, network task registry."""

from __future__ import annotations

from applypilot import web_dashboard as wd


class _Headers:
    def __init__(self, d):
        self._d = d

    def get(self, k, default=None):
        return self._d.get(k, default)


class _Handler:
    def __init__(self, headers):
        self.headers = _Headers(headers)


def test_origin_ok_allows_localhost():
    assert wd._origin_ok(_Handler({"Host": "127.0.0.1:8765"})) is True
    assert wd._origin_ok(_Handler({"Origin": "http://localhost:8765", "Host": "localhost:8765"})) is True


def test_origin_ok_rejects_cross_origin():
    assert wd._origin_ok(_Handler({"Origin": "http://evil.com", "Host": "127.0.0.1:8765"})) is False


def test_contact_payload_shape(tmp_path, monkeypatch):
    import applypilot.database as database
    db = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", db)
    database.close_connection(db)
    database.init_db(db)  # connections.match() needs the DB (no connections imported -> no match)

    p = wd._contact_payload({
        "id": "c1", "full_name": "Jane", "title": "Eng", "email": "j@x.com",
        "email_status": "verified", "linkedin_url": "https://l/in/j", "match_reason": "same role",
        "outreach_subject": "Hi", "outreach_message": "Body", "outreach_status": "drafted",
    }, "Acme")
    assert p["id"] == "c1" and p["full_name"] == "Jane" and p["email"] == "j@x.com"
    assert p["outreach_subject"] == "Hi" and p["outreach_message"] == "Body"
    assert p["outreach_status"] == "drafted"
    assert p["is_connection"] is False  # none imported
    # missing fields default cleanly
    empty = wd._contact_payload({})
    assert empty["email_status"] == "none" and empty["outreach_status"] == "none"


def test_network_runner_rejects_concurrent_same_job(monkeypatch):
    runner = wd.NetworkRunner()
    # make the worker block so the task stays "running"
    import threading
    gate = threading.Event()
    monkeypatch.setattr(runner, "_run", lambda *a: gate.wait(timeout=2))
    ok1, _ = runner.start("http://j/1", 5, False)
    ok2, msg = runner.start("http://j/1", 5, False)  # same job, still running
    assert ok1 is True and ok2 is False and "already" in msg
    ok3, _ = runner.start("http://j/2", 5, False)     # different job runs
    assert ok3 is True
    gate.set()


# ── phone / notes (operator-entered; Apollo won't API-release a direct dial) ──

def test_apollo_profile_url():
    assert wd._apollo_profile_url("68e7946230488600010dc85a") == \
        "https://app.apollo.io/#/people/68e7946230488600010dc85a"
    # no id (e.g. a contact found via your LinkedIn connections) -> no link, not a broken one
    assert wd._apollo_profile_url(None) == ""
    assert wd._apollo_profile_url("  ") == ""


def test_contact_payload_exposes_phone_notes_and_apollo_link(tmp_path, monkeypatch):
    import applypilot.database as database
    db = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", db)
    database.close_connection(db)
    database.init_db(db)

    p = wd._contact_payload({"id": "c1", "full_name": "Jane", "apollo_id": "abc123",
                             "phone": "+1 555 010 9999", "notes": "call after 5pm"})
    assert p["phone"] == "+1 555 010 9999"
    assert p["notes"] == "call after 5pm"
    assert p["apollo_url"].endswith("/people/abc123")
    # absent -> empty strings, never None (the template interpolates these directly)
    empty = wd._contact_payload({})
    assert empty["phone"] == "" and empty["notes"] == "" and empty["apollo_url"] == ""


def test_save_contact_details_persists_and_caps(tmp_path, monkeypatch):
    import applypilot.database as database
    from applypilot.networking import store
    db = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", db)
    database.close_connection(db)
    database.init_db(db)
    conn = database.get_connection(db)
    store.init_contacts(conn)
    store.upsert_contact({"id": "c1", "job_url": "http://j/1", "full_name": "Jane",
                          "outreach_status": "submitted"})

    r = wd._save_contact_details({"contact_id": "c1", "phone": "  +1 555 010 9999  ",
                                  "notes": "picked up, call back Tue"})
    assert r["ok"] is True
    row = conn.execute("SELECT phone, notes, outreach_status FROM contacts WHERE id='c1'").fetchone()
    assert row["phone"] == "+1 555 010 9999"          # trimmed
    assert row["notes"] == "picked up, call back Tue"
    # saving a phone must NOT reopen an already-sent contact (that's _save_or_regen_draft's job)
    assert row["outreach_status"] == "submitted"

    # oversized input is capped, not rejected
    assert wd._save_contact_details({"contact_id": "c1", "notes": "x" * 5000})["ok"] is True
    assert len(conn.execute("SELECT notes FROM contacts WHERE id='c1'").fetchone()["notes"]) == 2000


def test_save_contact_details_rejects_unknown_contact(tmp_path, monkeypatch):
    import applypilot.database as database
    from applypilot.networking import store
    db = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", db)
    database.close_connection(db)
    database.init_db(db)
    store.init_contacts(database.get_connection(db))
    assert wd._save_contact_details({"contact_id": "nope", "phone": "1"})["ok"] is False
    assert wd._save_contact_details({"phone": "1"})["ok"] is False


# ── activity log: outreach events (emails, LinkedIn, phone) ──────────────────

def _seed(tmp_path, monkeypatch):
    import applypilot.database as database
    from applypilot.networking import store
    db = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", db)
    database.close_connection(db)
    database.init_db(db)
    conn = database.get_connection(db)
    store.init_contacts(conn)
    conn.execute("INSERT INTO jobs (url, title, company, site) VALUES (?,?,?,?)",
                 ("http://j/1", "SWE", "Acme", "Greenhouse"))
    conn.commit()
    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Jane Doe",
                                "title": "Recruiter", "email": "jane@acme.com",
                                "linkedin_url": "https://l/in/jane", "linkedin_message": "hi"})
    return database, store, conn, cid


def test_email_send_logs_activity(tmp_path, monkeypatch):
    database, store, conn, cid = _seed(tmp_path, monkeypatch)
    store.mark_sent(cid, "<m1@x>")
    ev = database.get_job_events("http://j/1", conn=conn)
    assert len(ev) == 1
    assert ev[0]["stage"] == "outreach" and ev[0]["status"] == "ok"
    assert "Emailed Jane Doe" in ev[0]["detail"] and "jane@acme.com" in ev[0]["detail"]


def test_send_failure_logs_activity(tmp_path, monkeypatch):
    database, store, conn, cid = _seed(tmp_path, monkeypatch)
    store.mark_send_failed(cid, "550 mailbox unavailable")
    ev = database.get_job_events("http://j/1", conn=conn)
    assert ev[-1]["status"] == "failed" and "550" in ev[-1]["detail"]


def test_linkedin_connect_logs_activity(tmp_path, monkeypatch):
    database, store, conn, cid = _seed(tmp_path, monkeypatch)
    # the dashboard's "✓ I sent it" confirm and the extension POST share this path
    body, code = wd._apply_dm_status(cid, "manual")
    assert code == 200 and body["ok"] is True
    ev = database.get_job_events("http://j/1", conn=conn)
    assert any("Connected on LinkedIn" in e["detail"] for e in ev)


def test_phone_logged_once_not_on_every_resave(tmp_path, monkeypatch):
    database, store, conn, cid = _seed(tmp_path, monkeypatch)
    wd._save_contact_details({"contact_id": cid, "phone": "+1 555 010 2020"})
    wd._save_contact_details({"contact_id": cid, "phone": "+1 555 010 2020", "notes": "again"})
    phone_events = [e for e in database.get_job_events("http://j/1", conn=conn)
                    if "phone number" in (e["detail"] or "")]
    assert len(phone_events) == 1          # unchanged phone must not re-log
    wd._save_contact_details({"contact_id": cid, "phone": "+1 555 999 0000"})
    phone_events = [e for e in database.get_job_events("http://j/1", conn=conn)
                    if "phone number" in (e["detail"] or "")]
    assert len(phone_events) == 2          # a real change does log


def test_log_contact_event_survives_missing_contact(tmp_path, monkeypatch):
    """Activity logging must never raise into a send path."""
    database, store, conn, cid = _seed(tmp_path, monkeypatch)
    store.log_contact_event("does-not-exist", "ok", "should be dropped silently", conn)
    assert database.get_job_events("http://j/1", conn=conn) == []


# ── completion checklist (gamified per-job progress) ─────────────────────────

def _dt(days_ago=0):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_checklist_empty_job_is_zero_percent():
    cl = wd._job_checklist("ready", "", [])
    assert cl["pct"] == 0 and cl["complete"] is False
    # nothing to email / connect with yet -> those steps must be n/a, not failures
    st = {s["key"]: s["state"] for s in cl["steps"]}
    assert st["emailed"] == "na" and st["linkedin"] == "na" and st["followup"] == "na"


def test_checklist_zero_denominator_steps_excluded_from_pct():
    """A job with no emailable contacts must still be able to reach 100%."""
    cl = wd._job_checklist("applied", _dt(), [{"linkedin_url": "L1", "dm_status": "manual"}])
    assert cl["pct"] == 100 and cl["complete"] is True


def test_checklist_partial_progress():
    cl = wd._job_checklist("applied", _dt(), [
        {"email": "a@x.com", "linkedin_url": "L1", "emailed": True, "submitted_at": _dt()},
        {"email": "b@x.com", "linkedin_url": "L2"},
    ])
    st = {s["key"]: s for s in cl["steps"]}
    assert st["emailed"]["state"] == "partial" and st["emailed"]["done"] == 1
    assert st["linkedin"]["state"] == "todo"
    assert 0 < cl["pct"] < 100 and cl["complete"] is False


def test_followup_not_due_until_the_window_passes():
    """An email sent moments ago must not count against you."""
    fresh = [{"email": "a@x.com", "linkedin_url": "L1", "emailed": True,
              "submitted_at": _dt(0), "dm_status": "manual"}]
    cl = wd._job_checklist("applied", _dt(), fresh)
    assert cl["followups_due"] == 0
    assert cl["pct"] == 100 and cl["complete"] is True
    assert fresh[0]["followup_due"] is False


def test_followup_comes_due_and_drops_completion():
    stale = [{"email": "a@x.com", "linkedin_url": "L1", "emailed": True,
              "submitted_at": _dt(5), "dm_status": "manual"}]
    cl = wd._job_checklist("applied", _dt(), stale)
    assert cl["followups_due"] == 1 and cl["complete"] is False
    assert stale[0]["followup_due"] is True          # drives the per-contact button
    # recording the follow-up restores 100%
    stale[0]["followed_up_at"] = _dt(0)
    cl2 = wd._job_checklist("applied", _dt(), stale)
    assert cl2["followups_due"] == 0 and cl2["pct"] == 100 and cl2["complete"] is True
    assert stale[0]["followup_due"] is False


def test_followup_window_is_configurable(monkeypatch):
    contacts = [{"email": "a@x.com", "emailed": True, "submitted_at": _dt(3)}]
    monkeypatch.setenv("FOLLOWUP_AFTER_DAYS", "7")
    assert wd._job_checklist("applied", _dt(), contacts)["followups_due"] == 0
    monkeypatch.setenv("FOLLOWUP_AFTER_DAYS", "1")
    assert wd._job_checklist("applied", _dt(), contacts)["followups_due"] == 1


def test_checklist_tolerates_a_bad_timestamp():
    contacts = [{"email": "a@x.com", "emailed": True, "submitted_at": "not-a-date"}]
    cl = wd._job_checklist("applied", _dt(), contacts)   # must not raise
    assert cl["followups_due"] == 0


def test_mark_followed_up_is_idempotent(tmp_path, monkeypatch):
    database, store, conn, cid = _seed(tmp_path, monkeypatch)
    from applypilot.networking import touches

    def when():
        return touches.ladder_state(cid, "email", conn)["last_sent_at"]

    assert store.mark_followed_up(cid, conn=conn) is True
    first = when()
    assert store.mark_followed_up(cid, conn=conn) is False   # second call is a no-op
    assert when() == first                                   # timestamp not rewritten
    logged = [e for e in database.get_job_events("http://j/1", conn=conn)
              if "Followed up" in (e["detail"] or "")]
    assert len(logged) == 1                                  # and only logged once


# ── LinkedIn follow-up ladder (accepted the invite, then went quiet) ─────────

def _li_contact(**kw):
    c = {"id": "c1", "full_name": "Sumit Singh", "title": "Sr. Recruiter",
         "linkedin_url": "https://l/in/sumit", "dm_status": "manual",
         "dm_sent_at": _dt(7), "email": "", "emailed": False}
    c.update(kw)
    return c


def _ladders(**by_channel):
    """ARCH-3: ladder state is (contact, channel) -> state, not columns on the contact.

    Passing it in keeps these tests database-free while exercising the real engine.
    """
    from applypilot.domain.followup import EMPTY_LADDER
    return {("c1", ch): {**EMPTY_LADDER, **vals} for ch, vals in by_channel.items()}


def test_linkedin_followup_due_after_the_invite_window():
    cl = wd._followup_panel([_li_contact()])
    assert cl["li_due_count"] == 1
    assert cl["li_due"][0]["full_name"] == "Sumit Singh" and cl["li_due"][0]["touch"] == 1


def test_linkedin_followup_not_due_immediately_after_connecting():
    """Nudging a brand-new connection after a day reads badly — default is 5 days."""
    cl = wd._followup_panel([_li_contact(dm_sent_at=_dt(1))])
    assert cl["li_due_count"] == 0 and len(cl["li_waiting"]) == 1


def test_linkedin_needs_a_recorded_invite_to_schedule_anything():
    """Sumit's real row had dm_status='' — an invite sent before tracking existed."""
    cl = wd._followup_panel([_li_contact(dm_status="", dm_sent_at="")])
    assert cl["li_due_count"] == 0 and cl["li_waiting"] == []


def test_linkedin_replied_or_stopped_halts_the_ladder():
    for status in ("replied", "stopped"):
        cl = wd._followup_panel([_li_contact()],
                                _ladders(linkedin={"sequence_status": status}))
        assert cl["li_due_count"] == 0, f"{status} must stop the sequence"


def test_linkedin_ladder_finishes_after_the_last_touch():
    # default LinkedIn ladder is 2 touches
    cl = wd._followup_panel([_li_contact()], _ladders(linkedin={"count": 2}))
    assert cl["li_due_count"] == 0


def test_linkedin_schedule_is_configurable(monkeypatch):
    c = [_li_contact(dm_sent_at=_dt(3))]
    monkeypatch.setenv("LINKEDIN_FOLLOWUP_SCHEDULE", "24,48")
    assert wd._followup_panel(c)["li_due_count"] == 1
    monkeypatch.setenv("LINKEDIN_FOLLOWUP_SCHEDULE", "240")
    assert wd._followup_panel(c)["li_due_count"] == 0


def test_email_and_linkedin_ladders_are_independent(tmp_path, monkeypatch):
    """An emailed-and-followed-up contact can still owe a LinkedIn message, and vice versa."""
    c = _li_contact(email="s@x.com", emailed=True, submitted_at=_dt(9))
    cl = wd._followup_panel([c], _ladders(email={"count": 1, "last_sent_at": _dt(0)}))
    assert cl["due_count"] == 0          # email follow-up was just sent
    assert cl["li_due_count"] == 1       # LinkedIn still owed


def test_marking_a_linkedin_touch_goes_through_the_same_function_as_email(tmp_path, monkeypatch):
    """ARCH-3: there is no `mark_li_followup_sent` any more. One function, one `channel` arg.

    If someone reintroduces a LinkedIn-specific writer, this test still passes — but
    test_no_channel_specific_ladder_functions below will not.
    """
    database, store, conn, cid = _seed(tmp_path, monkeypatch)
    from applypilot.networking import touches
    n = store.mark_followup_sent(cid, "linkedin", conn)
    assert n == 1
    st = touches.ladder_state(cid, "linkedin", conn)
    assert st["count"] == 1 and st["last_sent_at"]
    assert any("LinkedIn follow-up #1" in (e["detail"] or "")
               for e in database.get_job_events("http://j/1", conn=conn))
    # …and the email ladder is untouched: separate rows, not separate columns.
    assert touches.ladder_state(cid, "email", conn)["count"] == 0


def test_mark_connected_now_starts_the_clock(tmp_path, monkeypatch):
    """For invites sent before ApplyPilot tracked them there is no anchor to schedule from."""
    database, store, conn, cid = _seed(tmp_path, monkeypatch)
    store.mark_connected_now(cid, conn)
    row = conn.execute("SELECT dm_status, dm_sent_at FROM contacts WHERE id=?", (cid,)).fetchone()
    assert row["dm_status"] == "manual" and row["dm_sent_at"]
    first = row["dm_sent_at"]
    store.mark_connected_now(cid, conn)      # must not rewrite an existing anchor
    assert conn.execute("SELECT dm_sent_at FROM contacts WHERE id=?", (cid,)).fetchone()[0] == first

"""A real application that went through must not end up recorded as a failure.

2026-07-30, a Salesforce application that succeeded end to end:

    16:31:32  Filled, then paused for you: login (41s)
    16:40:00  Apply failed: copilot_violation_agent_submitted (464s)

The operator signed in at the wall, clicked Continue, finished the form and submitted it
themselves. The resumed agent then truthfully reported the application as submitted — and
co-pilot read `RESULT: APPLIED` as "the agent submitted without review", which is a safety
violation, and marked the job FAILED with applied_at NULL.

Then the correction was refused too: "Mark submitted ✓" required status == 'ready_to_submit'
exactly, so a successful application was stuck as a failure with no way to fix it from the UI.

Two fixes, and the distinction between them matters:
  * on RESUME the operator is at the keyboard by definition, so APPLIED is expected — hand it
    back for confirmation instead of calling it a violation;
  * a FRESH co-pilot run that submits on its own is still a genuine breach and stays a failure.
"""

from __future__ import annotations

import inspect

import pytest

import applypilot.database as database
from applypilot.apply import launcher


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    return database.get_connection(path)


@pytest.fixture()
def wd(db, monkeypatch):
    from applypilot import web_dashboard
    monkeypatch.setattr(web_dashboard, "get_connection", lambda *a, **k: db)
    monkeypatch.setattr(web_dashboard, "init_db", lambda *a, **k: db)
    return web_dashboard


def _job(conn, url, title, **cols):
    base = {"site": title, "strategy": "dashboard_upload",
            "discovered_at": "2026-07-30T10:00:00+00:00"}
    base.update(cols)
    keys = ", ".join(["url", "title", *base])
    marks = ", ".join("?" for _ in range(len(base) + 2))
    conn.execute(f"INSERT INTO jobs ({keys}) VALUES ({marks})", (url, title, *base.values()))
    conn.commit()


# ── the classification ───────────────────────────────────────────────────────────────────

def test_applied_after_a_resume_is_not_treated_as_a_violation():
    """The exact Salesforce loss."""
    src = inspect.getsource(launcher.run_job)
    applied_at = src.index('RESULT:\\s*APPLIED')
    violation_at = src.index("CO-PILOT VIOLATION", applied_at)
    # Compare POSITIONS rather than slicing a fixed window: the branch is long enough that a
    # guessed window silently missed the violation text and the test errored instead of
    # asserting. The resume check has to come BEFORE the violation, or it never runs.
    resume_at = src.index("if resume:", applied_at)
    assert resume_at < violation_at, \
        "the violation path is reached before the resume check, so resume never wins"
    assert "needs_review" in src[resume_at:violation_at], \
        "a resumed run does not hand back for confirmation"


def test_a_fresh_copilot_run_that_submits_is_still_a_violation():
    """This guard is the reason co-pilot is trustworthy — nobody reviewed that application.
    Loosening it for resume must not loosen it here."""
    src = inspect.getsource(launcher.run_job)
    assert "failed:copilot_violation_agent_submitted" in src
    branch = src[src.index('RESULT:\\s*APPLIED'):]
    assert "CO-PILOT VIOLATION" in branch


# ── the correction the operator makes ────────────────────────────────────────────────────

def test_a_job_the_agent_called_failed_can_still_be_confirmed(wd, db):
    """The operator is the authority on whether they submitted something. Refusing their
    correction left a real application recorded as a failure permanently."""
    _job(db, "http://j/sf", "Salesforce", apply_status="failed",
         apply_error="copilot_violation_agent_submitted",
         last_attempted_at="2026-07-30T16:32:15+00:00", apply_attempts=1)

    res = wd._mark_submitted("http://j/sf")
    assert res["ok"] is True, res
    row = db.execute("SELECT apply_status, applied_at FROM jobs WHERE url=?",
                     ("http://j/sf",)).fetchone()
    assert dict(row)["apply_status"] == "applied"
    assert dict(row)["applied_at"], "applied_at left NULL, so it still is not really applied"


@pytest.mark.parametrize("status", ["ready_to_submit", "needs_human", "failed", "in_progress"])
def test_any_attempted_state_can_be_confirmed(wd, db, status):
    _job(db, f"http://j/{status}", "X", apply_status=status,
         last_attempted_at="2026-07-30T16:00:00+00:00", apply_attempts=1)
    assert wd._mark_submitted(f"http://j/{status}")["ok"] is True


def test_a_job_that_was_never_run_cannot_be_marked_submitted(wd, db):
    """The guard that still matters: without it the button becomes a way to fabricate an
    application record for something the app never even opened."""
    _job(db, "http://j/new", "Never run")
    res = wd._mark_submitted("http://j/new")
    assert res["ok"] is False
    assert "never filled" in res["message"], res["message"]


def test_marking_twice_is_refused_rather_than_resetting_the_date(wd, db):
    """applied_at anchors the whole follow-up ladder — silently rewriting it would reschedule
    every touch."""
    _job(db, "http://j/a", "A", apply_status="applied",
         applied_at="2026-07-01T00:00:00+00:00", last_attempted_at="2026-07-01T00:00:00+00:00")
    res = wd._mark_submitted("http://j/a")
    assert res["ok"] is False
    row = dict(db.execute("SELECT applied_at FROM jobs WHERE url=?", ("http://j/a",)).fetchone())
    assert row["applied_at"] == "2026-07-01T00:00:00+00:00"


def test_the_confirmation_is_recorded_with_where_it_came_from(wd, db):
    """"applied" that was really "failed until I said otherwise" is worth being able to see."""
    _job(db, "http://j/sf", "Salesforce", apply_status="failed",
         last_attempted_at="2026-07-30T16:32:15+00:00", apply_attempts=1)
    wd._mark_submitted("http://j/sf")
    details = " ".join(e["detail"] or "" for e in database.get_job_events("http://j/sf", conn=db))
    assert "confirmed" in details.lower() and "failed" in details, details

"""A killed dashboard must not strand a job as permanently "in progress".

Found the hard way on 2026-07-29: `run_dashboard_restart` runs the apply as a SYNCHRONOUS
CHILD of the dashboard (`subprocess.run`), so restarting the server killed two in-flight
applies. Each had taken an `apply_status='in_progress'` lock at acquisition and neither
ever released it.

The failure is silent in the worst way: the UI says "in progress" forever, `acquire_job`
explicitly skips in-progress rows, so pressing apply again does nothing at all — no error,
no event, no log line.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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


def _locked_job(conn, url: str, minutes_ago: float, title: str = "PM") -> None:
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    conn.execute(
        "INSERT INTO jobs (url, title, site, tailored_resume_path, apply_status, agent_id, "
        "last_attempted_at) VALUES (?,?,?,?,'in_progress','worker-0',?)",
        (url, title, "Greenhouse", "/tmp/r.pdf", ts),
    )
    conn.commit()


def _status(conn, url: str):
    return conn.execute("SELECT apply_status, agent_id FROM jobs WHERE url=?", (url,)).fetchone()


def test_a_lock_older_than_the_cutoff_is_released(db):
    _locked_job(db, "http://j/stale", minutes_ago=31)
    assert launcher.release_stale_locks(conn=db) == ["http://j/stale"]
    row = _status(db, "http://j/stale")
    assert row["apply_status"] is None and row["agent_id"] is None


def test_a_fresh_lock_is_left_alone(db):
    """`applypilot apply` can be running in a terminal while the dashboard restarts.

    Stealing that lock would let a second agent start on the same application — worse
    than showing a stale row for another ten minutes.
    """
    _locked_job(db, "http://j/live", minutes_ago=2)
    assert launcher.release_stale_locks(conn=db) == []
    assert _status(db, "http://j/live")["apply_status"] == "in_progress"


def test_release_is_logged_so_the_lock_does_not_vanish_silently(db):
    """The original bug went unnoticed because nothing recorded it. A lock that
    disappears with no trace is the same failure in the other direction."""
    _locked_job(db, "http://j/stale", minutes_ago=45)
    launcher.release_stale_locks(conn=db)
    details = [e["detail"] or "" for e in database.get_job_events("http://j/stale", conn=db)]
    assert any("stale in-progress lock" in d.lower() for d in details), details


def test_only_in_progress_rows_are_touched(db):
    """`needs_human` and `ready_to_submit` are jobs waiting on YOU, not stuck ones."""
    for status in ("needs_human", "ready_to_submit", "applied", "failed"):
        ts = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        db.execute("INSERT INTO jobs (url, title, site, apply_status, last_attempted_at) "
                   "VALUES (?,?,?,?,?)", (f"http://j/{status}", "PM", "GH", status, ts))
    db.commit()
    assert launcher.release_stale_locks(conn=db) == []
    for status in ("needs_human", "ready_to_submit", "applied", "failed"):
        assert _status(db, f"http://j/{status}")["apply_status"] == status


def test_a_released_job_can_be_acquired_again(db, monkeypatch):
    """The point of the fix: the retry that silently did nothing now works.

    Without the release, `acquire_job`'s `apply_status != 'in_progress'` guard means
    pressing apply again matches no rows and reports nothing.
    """
    monkeypatch.setattr(launcher, "get_connection", lambda *a, **k: db)
    _locked_job(db, "http://j/stuck", minutes_ago=60)
    assert launcher.acquire_job(target_url="http://j/stuck") is None   # the silent failure
    launcher.release_stale_locks(conn=db)
    assert launcher.acquire_job(target_url="http://j/stuck") is not None


def test_the_cutoff_is_configurable(db, monkeypatch):
    _locked_job(db, "http://j/x", minutes_ago=10)
    assert launcher.release_stale_locks(conn=db) == []
    assert launcher.release_stale_locks(max_age_minutes=5, conn=db) == ["http://j/x"]

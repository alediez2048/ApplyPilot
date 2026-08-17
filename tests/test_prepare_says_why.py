"""Prepare has to say why it did nothing.

Reported as "I'm getting an error when trying to apply for this role", with a prepare run that
returned `{'enriched': 0, 'detail_errors': 0, ... 'covers': 0}` and no error anywhere. Nothing was
broken: the posting had been applied to twelve days earlier, so every queue was legitimately
empty. But three separate paths said so in a way that reads as a silent failure — import called
it a duplicate, prepare printed seven zeros, and Apply ran the QUEUE rather than the row on
screen and started an unrelated job.

§Lessons 15: a zero result must be as loud as an error. Seven zeros describe the QUEUES, which
is the one thing already visible; what the operator cannot see is that the job is finished rather
than stuck.
"""

from __future__ import annotations

import pytest

import applypilot.database as database


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "prep.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    from applypilot import web_dashboard as wd
    monkeypatch.setattr(wd, "get_connection", lambda *a, **k: conn)
    return conn


def _job(conn, url, **kw):
    row = {"url": url, "title": "Project Manager", "company": "Arm", "site": "Arm",
           "strategy": "dashboard_upload", "space_id": "job-search"}
    row.update(kw)
    cols = ", ".join(row)
    conn.execute(f"INSERT OR REPLACE INTO jobs ({cols}) VALUES ({','.join('?' * len(row))})",
                 list(row.values()))
    conn.commit()


# ── counting the JOBS, not the queues ───────────────────────────────────────

def test_an_applied_job_is_counted_as_applied(db):
    from applypilot.repo import jobs as jr
    _job(db, "u1", applied_at="2026-08-05T21:38:17+00:00", tailored_resume_path="/cv.txt")
    assert jr.dashboard_upload_states(db) == {"total": 1, "applied": 1, "ready": 0}


def test_a_prepared_but_unapplied_job_is_counted_as_ready(db):
    from applypilot.repo import jobs as jr
    _job(db, "u1", tailored_resume_path="/cv.txt")
    assert jr.dashboard_upload_states(db) == {"total": 1, "applied": 0, "ready": 1}


def test_applied_and_ready_are_MUTUALLY_exclusive(db):
    """An applied job usually still has its résumé path, so counting `ready` on the résumé alone
    would report the same job twice and the two numbers would exceed the total."""
    from applypilot.repo import jobs as jr
    _job(db, "u1", applied_at="2026-08-05T00:00:00+00:00", tailored_resume_path="/cv.txt")
    _job(db, "u2", tailored_resume_path="/cv2.txt")
    got = jr.dashboard_upload_states(db)
    assert got == {"total": 2, "applied": 1, "ready": 1}
    assert got["applied"] + got["ready"] <= got["total"]


def test_a_job_with_nothing_done_is_neither(db):
    from applypilot.repo import jobs as jr
    _job(db, "u1")
    assert jr.dashboard_upload_states(db) == {"total": 1, "applied": 0, "ready": 0}


# ── what the operator is told ───────────────────────────────────────────────

def test_the_note_names_RE_APPLY_when_everything_is_already_applied(db):
    """The actual next action. "0 tailored" does not tell you the row you are looking at is
    finished, and Re-apply is the only path that redoes one."""
    from applypilot import web_dashboard as wd
    _job(db, "u1", applied_at="2026-08-05T00:00:00+00:00", tailored_resume_path="/cv.txt")
    note = wd._nothing_to_prepare(db)
    assert "already applied" in note and "Re-apply" in note


def test_the_note_points_at_APPLY_when_materials_are_ready(db):
    from applypilot import web_dashboard as wd
    _job(db, "u1", tailored_resume_path="/cv.txt")
    note = wd._nothing_to_prepare(db)
    assert "already prepared" in note and "Apply" in note


def test_an_empty_database_says_to_paste_a_URL(db):
    """"Nothing needed preparing" is misleading with nothing imported — the honest answer names
    the step that was never taken."""
    from applypilot import web_dashboard as wd
    assert "paste" in wd._nothing_to_prepare(db)


def test_the_note_never_takes_the_run_down_with_it(db, monkeypatch):
    """A diagnostic that raises turns "nothing to do" into a real failure — the opposite of the
    point."""
    from applypilot import web_dashboard as wd
    from applypilot.repo import jobs as jr
    monkeypatch.setattr(jr, "dashboard_upload_states",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert wd._nothing_to_prepare(db)


# ── it only fires when there really was nothing ─────────────────────────────

def _prepare(db, monkeypatch, **queues):
    from applypilot import web_dashboard as wd
    from applypilot.repo import jobs as jr
    monkeypatch.setattr(jr, "queue_needing_detail", lambda *a, **k: [])
    monkeypatch.setattr(jr, "bypass_scoring", lambda *a, **k: queues.get("scored", 0))
    monkeypatch.setattr(jr, "queue_for_tailor", lambda *a, **k: [])
    monkeypatch.setattr(jr, "queue_for_cover", lambda *a, **k: [])
    return wd.run_dashboard_prepare(validation_mode="normal")


def test_a_run_that_did_NOTHING_carries_the_note(db, monkeypatch):
    _job(db, "u1", applied_at="2026-08-05T00:00:00+00:00", tailored_resume_path="/cv.txt")
    assert "already applied" in _prepare(db, monkeypatch).get("note", "")


def test_a_run_that_DID_something_carries_no_note(db, monkeypatch):
    """The note explains an empty result. On a run that worked it would be noise, and worse, it
    would claim there was nothing to do while something was done."""
    _job(db, "u1")
    assert "note" not in _prepare(db, monkeypatch, scored=3)


def test_an_ERROR_counts_as_something_happening():
    """A run that hit three tailor errors did not do "nothing", and saying so would bury the
    errors under a reassuring sentence.

    Read off the SOURCE, and labelled as the weaker check it is: driving the real error paths
    needs the tailor, the renderer and a scraper stubbed, and the first version of this test
    faked it by setting `tailor_errors` on the returned dict AFTER the call — which tests the
    dict, not the guard. It passed, for the wrong reason (§Lessons 71).
    """
    import inspect
    from applypilot import web_dashboard as wd
    src = inspect.getsource(wd.run_dashboard_prepare)
    guard = src[src.index("if not any(("):src.index('result["note"]')]
    for counter in ("enriched", "tailored", "covers", "detail_errors",
                    "tailor_errors", "cover_errors", "score_bypassed"):
        assert counter in guard, f"{counter} is not in the did-anything-happen guard"

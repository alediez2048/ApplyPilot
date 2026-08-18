"""Apply acts on the row you clicked.

Reported as "I'm getting an error when trying to apply for this role", pointing at an Arm posting.
Nothing errored. `/api/apply` took no URL at all — it is a queue runner that selects by STATE — so
clicking Apply beside a job applied to twelve days earlier started **a billing assistant role at a
sports academy**, the only eligible row in the database. The job it picked was eligible and the
one on screen was not, so every layer behaved correctly and the operator got somebody else's
application.

CLAUDE.md had already recorded this once, reported and unfixed:

    Reported as "it is applying for the Texas Sports Academy role, this should not be the
    behavior", and correctly. Not fixed.

The queue behaviour is KEPT — filling ten prepared applications in one go is what the console
button is for. What changes is that a row can drive it, that a scoped run refuses in terms the
operator can act on, and that the console confirm NAMES what it is about to do.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import applypilot.database as database

JS = Path(__file__).resolve().parents[1] / "src" / "applypilot" / "static" / "dashboard.js"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "apply.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    from applypilot import web_dashboard as wd
    monkeypatch.setattr(wd, "get_connection", lambda *a, **k: conn)
    return conn


def _job(conn, url, **kw):
    row = {"url": url, "title": "Project Manager", "company": "Arm", "site": "Arm",
           "strategy": "dashboard_upload", "space_id": "job-search",
           "tailored_resume_path": "/cv.txt"}
    row.update(kw)
    cols = ", ".join(row)
    conn.execute(f"INSERT OR REPLACE INTO jobs ({cols}) VALUES ({','.join('?' * len(row))})",
                 list(row.values()))
    conn.commit()


def _run(monkeypatch, **kw):
    """Drive `run_dashboard_apply` with the launcher stubbed — nothing opens a browser."""
    from applypilot import web_dashboard as wd
    spawned = []
    monkeypatch.setattr(wd, "_busy_worker_ids", lambda n: set())
    monkeypatch.setattr(wd, "_review_browser_alive", lambda *a, **k: False)
    monkeypatch.setattr(wd.subprocess, "run",
                        lambda args, **k: spawned.append(args) or type("P", (), {"returncode": 0})())
    return wd.run_dashboard_apply(**kw), spawned


# ── the scoped run ──────────────────────────────────────────────────────────

def test_a_url_applies_to_THAT_job_and_no_other(db, monkeypatch):
    """The whole ticket. Two eligible jobs, one named — the other must not be touched."""
    _job(db, "https://careers.arm.com/job/1", title="Project Manager")
    _job(db, "https://apply.workable.com/tsa/j/2", title="Billing Assistant", company="TSA")
    res, spawned = _run(monkeypatch, url="https://careers.arm.com/job/1")
    assert res["queued"] == 1, res
    joined = " ".join(" ".join(map(str, a)) for a in spawned)
    assert "careers.arm.com/job/1" in joined
    assert "workable.com/tsa" not in joined, "it applied to a job the operator did not click"


def test_WITHOUT_a_url_it_is_still_the_queue_runner(db, monkeypatch):
    """Kept deliberately: filling every prepared application in one go is what the console button
    is for, and narrowing it to one job would be a different product."""
    _job(db, "https://careers.arm.com/job/1")
    _job(db, "https://apply.workable.com/tsa/j/2", company="TSA")
    res, _ = _run(monkeypatch)
    assert res["queued"] == 2


def test_a_scoped_run_cannot_reach_a_job_the_QUEUE_would_refuse(db, monkeypatch):
    """Filtered from the same eligibility query rather than applied to directly, so `in_progress`,
    over-cap and already-applied stay unreachable however the caller asks."""
    _job(db, "https://careers.arm.com/job/1", applied_at="2026-08-05T21:38:17+00:00",
         apply_status="applied")
    res, spawned = _run(monkeypatch, url="https://careers.arm.com/job/1")
    assert res["queued"] == 0 and spawned == []
    assert "blocked" in res


# ── the refusal has to be actionable ────────────────────────────────────────

def test_an_already_applied_job_says_so_and_names_RE_APPLY(db):
    """This is the live case: the Arm posting was applied to on 2026-08-05, and every path said
    'nothing happened' instead."""
    from applypilot import web_dashboard as wd
    _job(db, "u1", applied_at="2026-08-05T21:38:17+00:00", apply_status="applied")
    why = wd._why_not_applicable("u1", db)
    assert "already applied" in why and "2026-08-05" in why and "Re-apply" in why


def test_an_unprepared_job_points_at_PREPARE(db):
    from applypilot import web_dashboard as wd
    _job(db, "u1", tailored_resume_path=None)
    assert "Prepare" in wd._why_not_applicable("u1", db)


def test_a_job_already_filled_and_waiting_says_to_go_and_submit_it(db):
    from applypilot import web_dashboard as wd
    _job(db, "u1", apply_status="ready_to_submit")
    why = wd._why_not_applicable("u1", db)
    assert "waiting for you" in why and "Chrome" in why


def test_a_closed_job_says_to_restore_it_first(db):
    """Generated from `repo.jobs.CLOSED_STATUSES`, never a second hand-written list — leaving
    `cancelled` out of one is §Lessons 93."""
    from applypilot import web_dashboard as wd
    from applypilot.repo import jobs as jr
    for status in jr.CLOSED_STATUSES:
        _job(db, "u1", apply_status=status, rejected_at="2026-08-01T00:00:00+00:00")
        assert "Restore" in wd._why_not_applicable("u1", db), status


def test_a_url_that_is_not_in_the_database_says_to_paste_it(db):
    from applypilot import web_dashboard as wd
    assert "paste" in wd._why_not_applicable("https://nowhere.test/job/9", db)


def test_the_refusal_is_never_silent(db, monkeypatch):
    """A blocked scoped run must carry a message. "queued: 0" with nothing else is the shape the
    whole report was about (§Lessons 15)."""
    _job(db, "u1", applied_at="2026-08-05T00:00:00+00:00")
    res, _ = _run(monkeypatch, url="u1")
    assert res.get("blocked")


# ── the per-row button shares the guarded path ──────────────────────────────

def test_fill_one_goes_through_the_GUARDED_apply(db):
    """It used to shell straight to `applypilot apply --url`, inheriting none of the slot guards —
    so a row's Fill button could take the CDP port of a browser somebody was mid-review on and
    destroy a filled application. §Lessons 8's failure on the one path that skipped its fix."""
    import inspect
    from applypilot import web_dashboard as wd
    src = inspect.getsource(wd.run_dashboard_fill_one)
    assert "run_dashboard_apply(" in src
    assert "subprocess.run" not in src, "it still spawns the CLI, bypassing the slot guards"


def test_the_endpoint_accepts_a_url(db):
    import inspect
    from applypilot import web_dashboard as wd
    src = inspect.getsource(wd.DashboardHandler)
    # Sliced to the ROUTE boundary, not a guessed character width: the first version used
    # `[:600]` and failed because the call sits at ~640, which measures where I put the comment
    # rather than what the code does (§Lessons 98, a window sliced around a match).
    start = src.index('if path == "/api/apply"')
    end = src.index('if path ==', start + 10)
    block = src[start:end]
    assert 'data.get("url")' in block, "the endpoint drops the row's URL"
    assert "_start_apply(" in block


def test_the_url_reaches_the_subprocess_as_a_repr(db, monkeypatch):
    """A job URL carries quotes, ampersands and percent-escapes, and this is interpolated into a
    `python -c` program."""
    from applypilot import web_dashboard as wd
    seen = {}
    monkeypatch.setattr(wd._runner, "start", lambda name, args: (seen.update(a=args), (True, ""))[1])
    wd._start_apply(10, 1, False, True, url="https://x.test/j?a=1&b='2'")
    prog = seen["a"][-1]
    assert "url=" in prog and repr("https://x.test/j?a=1&b='2'") in prog


# ── the console button names what it will do ────────────────────────────────

def test_the_console_confirm_NAMES_the_jobs():
    """"Fill 1 application(s) for your review?" is the same sentence whichever job the queue
    picked, and this button does not act on the row on screen. A count you cannot check is not a
    confirmation."""
    js = JS.read_text(encoding="utf-8")
    fn = js[js.index("async function applyJobs()"):js.index("async function fillOne(")]
    assert "applyQueuePreview()" in fn, "the confirm still shows only a count"


def test_the_preview_uses_the_same_rule_as_the_server():
    js = JS.read_text(encoding="utf-8")
    m = re.search(r"function applyQueuePreview\(max = 8\)\s*\{(.+?)\n\}", js, re.S)
    assert m
    body = m.group(1)
    for clause in ("!isClosed(j)", "!j.applied_at", "j.status === 'ready'"):
        assert clause in body, clause
    assert "slice(0, max)" in body and "more" in body, "an unbounded list in a confirm() dialog"


def test_the_CALLERS_limit_does_not_decide_whether_one_job_is_eligible(db, monkeypatch):
    """`limit` means "how many to fill in one go". `queue_for_apply` orders by discovery date and
    truncates, so with the default limit of 10 the eleventh prepared job was refused as
    ineligible while being perfectly applicable — a wrong answer whose likelihood depends on how
    many OTHER jobs happen to be waiting."""
    for i in range(12):
        _job(db, f"https://x.test/job/{i}", title=f"Role {i}")
    res, spawned = _run(monkeypatch, limit=1, url="https://x.test/job/0")
    assert res["queued"] == 1, res
    assert "job/0" in " ".join(" ".join(map(str, a)) for a in spawned)

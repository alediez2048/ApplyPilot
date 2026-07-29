"""A co-pilot review must never be closed by the next queued application.

2026-07-29, on a real run. A Zello application was filled correctly in 78 seconds and handed
over: the agent even said "Leaving the browser open on the completed application — do not
click Submit." The next queued job started 428 MILLISECONDS later and killed that browser,
because launching an apply clears whatever holds the CDP port.

    21:24:30.724  Zello    -> ready_to_submit, Chrome kept alive for review
    21:24:31.152  Deloitte -> in_progress          <- 428ms later, same port

The row still read `ready_to_submit`, so the status claimed a form was waiting that no longer
existed. Nothing errored. Batching N jobs in co-pilot mode leaves every one un-reviewable
except the last, and the filled work is unrecoverable — the form dies with the browser.

Co-pilot mode ends by asking a human to act, so it is inherently one-at-a-time. These tests
enforce that at both points where it can go wrong: starting a batch, and continuing one.
"""

from __future__ import annotations

import pytest

import applypilot.database as database


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    return database.get_connection(path)


def _job(conn, url, title, **cols):
    base = {"site": title, "strategy": "dashboard_upload", "tailored_resume_path": "/tmp/r.pdf",
            "discovered_at": "2026-07-29T10:00:00+00:00"}
    base.update(cols)
    keys = ", ".join(["url", "title", *base])
    marks = ", ".join("?" for _ in range(len(base) + 2))
    conn.execute(f"INSERT INTO jobs ({keys}) VALUES ({marks})", (url, title, *base.values()))
    conn.commit()


@pytest.fixture()
def wd(db, monkeypatch):
    from applypilot import web_dashboard
    monkeypatch.setattr(web_dashboard, "get_connection", lambda *a, **k: db)
    monkeypatch.setattr(web_dashboard, "init_db", lambda *a, **k: db)
    monkeypatch.setattr(web_dashboard.config, "load_env", lambda *a, **k: None)
    monkeypatch.setattr(web_dashboard.config, "ensure_dirs", lambda *a, **k: None)
    return web_dashboard


def _never_runs(*a, **k):
    raise AssertionError("an apply subprocess was launched when it must not have been")


@pytest.fixture()
def live_review_browser(monkeypatch):
    """Pretend the co-pilot review window is still open.

    The guard blocks on a pending review only while its browser actually exists — a row alone
    is not enough, because `apply_status` outlives the process that set it (see
    test_copilot_stale_reviews.py). These tests are about a REAL open review, so they have to
    say so; without this they would pass for the wrong reason on a machine with no Chrome.
    """
    from applypilot.apply import chrome
    monkeypatch.setattr(chrome, "chrome_alive_on_port", lambda *a, **k: True)


def test_a_batch_will_not_start_while_a_review_is_open(wd, db, monkeypatch, live_review_browser):
    """The exact loss: an open review browser is invisible to `queue_for_apply`, which
    filters on the JOB, not on whether a browser is in use."""
    _job(db, "http://j/zello", "Zello", apply_status="ready_to_submit")
    _job(db, "http://j/next", "NextCo")
    monkeypatch.setattr(wd.subprocess, "run", _never_runs)

    res = wd.run_dashboard_apply(limit=10, copilot=True)
    assert res["queued"] == 0
    assert "waiting for you" in res["blocked"]
    assert "Zello" in res["blocked"], "the operator needs to know WHICH review is open"


def test_a_needs_human_blocker_also_blocks(wd, db, monkeypatch, live_review_browser):
    """`needs_human` (captcha/login/registration wall) is also an open browser."""
    _job(db, "http://j/arm", "Arm", apply_status="needs_human", apply_error="login")
    _job(db, "http://j/next", "NextCo")
    monkeypatch.setattr(wd.subprocess, "run", _never_runs)
    assert wd.run_dashboard_apply(limit=10, copilot=True)["queued"] == 0


def test_the_batch_stops_after_the_first_job_needs_review(wd, db, monkeypatch):
    """Three eligible jobs, the first hands over -> the other two must NOT start."""
    for i, name in enumerate(("Zello", "Deloitte", "Affirm")):
        _job(db, f"http://j/{i}", name)
    launched: list[str] = []

    class Done:
        returncode = 0

    def fake_run(args, **kw):
        url = args[args.index("--url") + 1]
        launched.append(url)
        # Simulate the co-pilot handoff the real agent performs.
        db.execute("UPDATE jobs SET apply_status='ready_to_submit' WHERE url=?", (url,))
        db.commit()
        return Done()

    monkeypatch.setattr(wd.subprocess, "run", fake_run)
    res = wd.run_dashboard_apply(limit=10, copilot=True)

    assert len(launched) == 1, f"launched {len(launched)} applies; each kills the previous review"
    assert res["needs_review"] == 1
    assert res["held_back"] == 2


def test_a_paused_queue_says_so_in_the_activity_log(wd, db, monkeypatch):
    """Silently stopping is its own bug — the operator would assume the rest had run."""
    for i, name in enumerate(("Zello", "Deloitte")):
        _job(db, f"http://j/{i}", name)

    class Done:
        returncode = 0

    def fake_run(args, **kw):
        url = args[args.index("--url") + 1]
        db.execute("UPDATE jobs SET apply_status='ready_to_submit' WHERE url=?", (url,))
        db.commit()
        return Done()

    monkeypatch.setattr(wd.subprocess, "run", fake_run)
    wd.run_dashboard_apply(limit=10, copilot=True)
    # Queue ORDER is a UI-precedence decision, not something this test should assume — look
    # for the note on whichever job paused. Asserting on "http://j/0" made this fail for the
    # wrong reason when the queue happened to run the other job first.
    details = [e["detail"] for url in ("http://j/0", "http://j/1")
               for e in database.get_job_events(url, conn=db)]
    assert any("held back" in (d or "") for d in details), details


def test_applied_jobs_do_not_pause_the_queue(wd, db, monkeypatch):
    """Only a PENDING HUMAN blocks. A job that fully applied needs no browser, so the
    queue must keep going — over-blocking would make batch apply useless."""
    for i, name in enumerate(("A", "B")):
        _job(db, f"http://j/{i}", name)
    launched = []

    class Done:
        returncode = 0

    def fake_run(args, **kw):
        url = args[args.index("--url") + 1]
        launched.append(url)
        db.execute("UPDATE jobs SET apply_status='applied', applied_at='2026-07-29T00:00:00+00:00' "
                   "WHERE url=?", (url,))
        db.commit()
        return Done()

    monkeypatch.setattr(wd.subprocess, "run", fake_run)
    res = wd.run_dashboard_apply(limit=10, copilot=True)
    assert len(launched) == 2 and res["applied"] == 2


def test_dry_run_is_not_gated(wd, db, monkeypatch, live_review_browser):
    """A dry run opens no browser, so a pending review cannot be harmed by it."""
    _job(db, "http://j/zello", "Zello", apply_status="ready_to_submit")
    _job(db, "http://j/next", "NextCo")
    launched = []

    class Done:
        returncode = 0

    monkeypatch.setattr(wd.subprocess, "run",
                        lambda args, **kw: (launched.append(1), Done())[1])
    wd.run_dashboard_apply(limit=10, copilot=True, dry_run=True)
    assert launched, "a dry run was blocked even though it opens no browser"

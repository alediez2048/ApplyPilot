"""A pending co-pilot review only blocks the queue while its browser is actually alive.

2026-07-29. The queue guard added earlier the same day (see test_copilot_queue.py) refuses to
start an apply while a filled application is waiting for the operator — correct, and it saved
real work. But it asks only the DATABASE:

    WHERE apply_status IN ('ready_to_submit', 'needs_human')

Nothing in that query knows whether the browser still exists. After a dashboard restart every
such row is a fossil: the browser died with the server, but the row still says a form is
waiting. So a brand-new application was refused with

    BLOCKED: 3 application(s) are filled and waiting for you (Affirm, Deloitte, Arm).

while NOTHING was listening on port 9222 — one of those rows was from the previous day. The
guard was protecting three reviews that no longer existed, and would have blocked every future
apply until the operator manually cleared them by hand.

`chrome_alive_on_port()` already existed for exactly this question; the guard just never asked
it. Liveness is the right discriminator rather than a timeout, because it is the same fact the
guard actually cares about: would starting an apply close a window someone needs?
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


def _no_browser(monkeypatch):
    """No CDP port anywhere is listening — every pending review is a fossil."""
    from applypilot.apply import chrome
    monkeypatch.setattr(chrome, "chrome_alive_on_port", lambda *a, **k: False)


def _browser_alive(monkeypatch):
    from applypilot.apply import chrome
    monkeypatch.setattr(chrome, "chrome_alive_on_port", lambda *a, **k: True)


class _Done:
    returncode = 0


def test_a_dead_review_does_not_block_a_new_application(db, wd, monkeypatch):
    """The exact incident: 3 stale rows, no browser, a brand-new job refused to start."""
    _job(db, "http://j/arm", "Arm", apply_status="needs_human", apply_error="field",
         last_attempted_at="2026-07-28T19:33:51+00:00")
    _job(db, "http://j/deloitte", "Deloitte", apply_status="ready_to_submit",
         last_attempted_at="2026-07-29T21:37:48+00:00")
    _job(db, "http://j/google", "Google")
    _no_browser(monkeypatch)
    launched: list[str] = []

    def fake_run(args, **kw):
        launched.append(args[args.index("--url") + 1])
        db.execute("UPDATE jobs SET apply_status='applied', "
                   "applied_at='2026-07-29T23:00:00+00:00' WHERE url=?", (launched[-1],))
        db.commit()
        return _Done()

    monkeypatch.setattr(wd.subprocess, "run", fake_run)
    res = wd.run_dashboard_apply(limit=10, copilot=True)

    assert not res.get("blocked"), f"blocked on dead reviews: {res.get('blocked')}"
    assert launched == ["http://j/google"], launched


def test_a_live_review_still_blocks(db, wd, monkeypatch):
    """The original guard must survive. A real open browser is exactly what it protects, and
    over-correcting here re-opens the bug that cost two filled applications."""
    _job(db, "http://j/zello", "Zello", apply_status="ready_to_submit",
         last_attempted_at="2026-07-29T21:24:30+00:00")
    _job(db, "http://j/next", "NextCo")
    _browser_alive(monkeypatch)

    def never(*a, **k):
        raise AssertionError("an apply launched while a REAL review browser was open")

    monkeypatch.setattr(wd.subprocess, "run", never)
    res = wd.run_dashboard_apply(limit=10, copilot=True)

    assert res["queued"] == 0
    assert "waiting for you" in res["blocked"]
    assert "Zello" in res["blocked"]


def test_the_operator_is_told_the_stale_rows_were_ignored(db, wd, monkeypatch):
    """Silently ignoring them is its own bug: the row still reads 'ready_to_submit', so the
    dashboard claims a form is waiting. The operator needs to know it is gone, not just that
    the queue moved on."""
    _job(db, "http://j/deloitte", "Deloitte", apply_status="ready_to_submit",
         last_attempted_at="2026-07-29T21:37:48+00:00")
    _job(db, "http://j/google", "Google")
    _no_browser(monkeypatch)
    monkeypatch.setattr(wd.subprocess, "run", lambda a, **k: _Done())

    res = wd.run_dashboard_apply(limit=10, copilot=True)
    note = (res.get("stale_reviews_note") or "")
    assert "Deloitte" in note, f"the abandoned review was never named: {res}"


def test_liveness_is_only_consulted_for_copilot_runs(db, wd, monkeypatch):
    """A non-copilot batch never hands a browser over, so it was never gated — checking
    liveness there would add a network probe to every run for nothing."""
    calls: list[int] = []
    from applypilot.apply import chrome
    monkeypatch.setattr(chrome, "chrome_alive_on_port",
                        lambda *a, **k: (calls.append(1), False)[1])
    _job(db, "http://j/a", "A")
    monkeypatch.setattr(wd.subprocess, "run", lambda a, **k: _Done())

    wd.run_dashboard_apply(limit=10, copilot=False)
    assert not calls, "probed the CDP port on a non-copilot run"


def test_a_dry_run_never_probes_and_never_blocks(db, wd, monkeypatch):
    _job(db, "http://j/zello", "Zello", apply_status="ready_to_submit")
    _job(db, "http://j/next", "NextCo")
    _browser_alive(monkeypatch)
    launched: list[int] = []
    monkeypatch.setattr(wd.subprocess, "run", lambda a, **k: (launched.append(1), _Done())[1])

    wd.run_dashboard_apply(limit=10, copilot=True, dry_run=True)
    assert launched, "a dry run was blocked even though it opens no browser"

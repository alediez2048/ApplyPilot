"""A failed scrape is not automatically a verdict.

Reported as "the pipeline is fully broken — I tried a simple job board and it could not even do
that". The run reported `{'enriched': 0, 'detail_errors': 1, ...}` and exited 0.

The posting was fine. Scraped by hand immediately afterwards it returned **13,602 characters in
5.1 seconds, tier 1**. What had happened was a single 45-second timeout — and the error branch
stamped `detail_scraped_at`, which is exactly what `queue_needing_detail` uses to decide a row
is done. So one network blip retired a job permanently: never re-enriched, so never tailored,
never covered, never applied to, and nothing in the UI distinguished it from a page that
genuinely cannot be read.

This is §Lessons 44's twin. That lesson routed the empty-description case INTO the error
branch — without noticing the error branch was itself a dead end.
"""

from __future__ import annotations

import pytest

import applypilot.database as database
from applypilot.enrichment import detail
from applypilot.repo import jobs as jobsrepo


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    conn = database.init_db(path)
    conn.execute("INSERT INTO jobs (url, title, site, strategy) VALUES (?,?,?,?)",
                 ("http://j/1", "Stanford uploaded job", "Stanford", "dashboard_upload"))
    conn.commit()
    return conn


# ── which failures deserve another go ───────────────────────────────────────

@pytest.mark.parametrize("err", [
    "timeout", "Timeout 45000ms exceeded", "net::ERR_CONNECTION_RESET",
    "socket hang up", "HTTP 503", "HTTP 429",
])
def test_a_failure_about_the_moment_is_transient(err):
    assert detail._is_transient(err)


@pytest.mark.parametrize("err", [
    "HTTP 404", "HTTP 410", "blocked", "login required",
    "No description could be read from this page",
])
def test_a_failure_about_the_page_is_not(err):
    """A 404 does not get better by asking again."""
    assert not detail._is_transient(err)


def test_an_unknown_error_is_treated_as_permanent():
    """Retrying forever on an error nobody classified is how a queue starts grinding. The
    operator always has the paste box, so the safe default is to stop and say so."""
    assert not detail._is_transient("something nobody has seen before")
    assert not detail._is_transient("")
    assert not detail._is_transient(None)


# ── the queue consequence, which is the actual bug ──────────────────────────

def _scrape_returns(monkeypatch, *results):
    """Drive the loop with a scripted sequence of scrape outcomes."""
    calls = {"n": 0}

    def fake(page, url):
        r = results[min(calls["n"], len(results) - 1)]
        calls["n"] += 1
        return {"status": "error", "full_description": None, "application_url": None,
                "tier_used": None, "elapsed": 0.1, **r}

    monkeypatch.setattr(detail, "scrape_detail_page", fake)
    monkeypatch.setattr(detail.time, "sleep", lambda *_a: None)
    return calls


def test_a_transient_failure_leaves_the_job_in_the_queue(db, monkeypatch):
    """The whole point. `detail_scraped_at` must stay NULL, or the row is gone for good."""
    _scrape_returns(monkeypatch, {"error": "timeout"})
    detail.scrape_site_batch(db, "Stanford", [("http://j/1", "Stanford uploaded job")], delay=0)

    row = db.execute("SELECT detail_scraped_at, detail_error, detail_attempts "
                     "FROM jobs WHERE url = 'http://j/1'").fetchone()
    assert row["detail_scraped_at"] is None, (
        "a timeout stamped detail_scraped_at, so this job can never be enriched again")
    assert [r["url"] for r in jobsrepo.queue_needing_detail(0, db)] == ["http://j/1"]
    assert "retry" in (row["detail_error"] or "").lower()


def test_it_retries_inside_the_same_run(db, monkeypatch):
    """One click on Prepare has to recover from a blip. Reporting "enriched: 0" and requiring a
    second click is what read as the pipeline being broken."""
    calls = _scrape_returns(
        monkeypatch,
        {"error": "timeout"},
        {"status": "ok", "error": None, "full_description": "About the role. " * 40,
         "application_url": "http://j/apply", "tier_used": 1},
    )
    stats = detail.scrape_site_batch(db, "Stanford", [("http://j/1", "Stanford uploaded job")], delay=0)

    assert calls["n"] == 2, "it gave up without retrying"
    assert stats["ok"] == 1 and stats["error"] == 0
    row = db.execute("SELECT full_description, detail_error, detail_scraped_at "
                     "FROM jobs WHERE url = 'http://j/1'").fetchone()
    assert row["full_description"] and row["detail_error"] is None and row["detail_scraped_at"]


def test_one_run_never_spends_the_whole_budget(db, monkeypatch):
    """The flaw a first version of this shipped with.

    In-run retry originally looped to MAX_DETAIL_ATTEMPTS, so three consecutive timeouts inside
    one pass retired the row. That means a thirty-second network outage permanently kills EVERY
    job in the queue at once — the same unrecoverable dead end, at a worse scale. One run may
    only ever spend `RETRIES_PER_RUN` of the budget.
    """
    calls = _scrape_returns(monkeypatch, {"error": "timeout"})
    detail.scrape_site_batch(db, "Stanford", [("http://j/1", "Stanford uploaded job")], delay=0)

    assert calls["n"] == detail.RETRIES_PER_RUN < detail.MAX_DETAIL_ATTEMPTS
    assert jobsrepo.queue_needing_detail(0, db), "one run of timeouts retired the job"


def test_it_gives_up_after_a_bounded_number_of_runs(db, monkeypatch):
    """The other side: a genuinely dead host must not be reattempted forever, and the operator
    has to be told what to do instead."""
    _scrape_returns(monkeypatch, {"error": "timeout"})
    for _ in range(10):
        if not jobsrepo.queue_needing_detail(0, db):
            break
        detail.scrape_site_batch(db, "Stanford", [("http://j/1", "Stanford uploaded job")], delay=0)

    row = db.execute("SELECT detail_scraped_at, detail_error, detail_attempts "
                     "FROM jobs WHERE url = 'http://j/1'").fetchone()
    assert row["detail_scraped_at"], "it never stops retrying"
    assert row["detail_attempts"] >= detail.MAX_DETAIL_ATTEMPTS
    assert "gave up" in row["detail_error"]
    assert "Paste" in row["detail_error"], "the dead end does not name the way out (§Lessons 15)"
    assert not jobsrepo.queue_needing_detail(0, db)


def test_attempts_accumulate_across_runs(db, monkeypatch):
    """Otherwise a row that stays queued is retried three times per run, forever."""
    monkeypatch.setattr(detail, "MAX_DETAIL_ATTEMPTS", 4)
    _scrape_returns(monkeypatch, {"error": "timeout"})
    detail.scrape_site_batch(db, "Stanford", [("http://j/1", "Stanford uploaded job")], delay=0)
    first = db.execute("SELECT detail_attempts FROM jobs WHERE url='http://j/1'").fetchone()[0]
    assert first == detail.RETRIES_PER_RUN

    db.execute("UPDATE jobs SET detail_scraped_at = NULL WHERE url='http://j/1'")  # operator retry
    db.commit()
    detail.scrape_site_batch(db, "Stanford", [("http://j/1", "Stanford uploaded job")], delay=0)
    assert db.execute(
        "SELECT detail_attempts FROM jobs WHERE url='http://j/1'").fetchone()[0] > first


def test_a_permanent_failure_is_retired_immediately(db, monkeypatch):
    """A 404 must not burn three page loads on its way to the same answer."""
    calls = _scrape_returns(monkeypatch, {"error": "HTTP 404"})
    detail.scrape_site_batch(db, "Stanford", [("http://j/1", "Stanford uploaded job")], delay=0)

    assert calls["n"] == 1
    row = db.execute("SELECT detail_scraped_at FROM jobs WHERE url='http://j/1'").fetchone()
    assert row["detail_scraped_at"], "a permanent failure left the row queued forever"

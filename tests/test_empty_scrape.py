"""A scrape that reads nothing must say so.

Reported as "the dashboard is no longer working" after pasting a Google careers URL. The
dashboard was fine. The scrape fetched a JavaScript-rendered page, got an empty shell, and took
the SUCCESS branch — because `status` is "partial" whenever no application_url was found, and
that branch writes the (empty) description, stamps `detail_scraped_at`, and sets `detail_error`
to NULL.

The row then failed `queue_needing_detail` forever, since it requires `detail_scraped_at IS
NULL`. With no description, tailor and cover never ran either. Nothing in the UI said anything:
the row looked exactly like a healthy one, which is why it read as the whole dashboard breaking.
"""

from __future__ import annotations

import pytest

import applypilot.database as database
from applypilot import web_dashboard as wd
from applypilot.repo import jobs as jobsrepo


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    conn.execute("INSERT INTO jobs (url, title, site, strategy) VALUES (?,?,?,?)",
                 ("http://j/1", "Uploaded uploaded job", "Uploaded", "dashboard_upload"))
    conn.commit()
    return conn


# ── the silent failure ──────────────────────────────────────────────────────

def test_an_empty_description_is_recorded_as_an_error():
    """`status == "partial"` only means no application_url was found. A page that rendered
    NOTHING took the same branch and was written down as a success."""
    src = open(__import__("applypilot.enrichment.detail", fromlist=["x"]).__file__,
               encoding="utf-8").read()
    block = src[src.index('if status in ("ok", "partial")'):]
    block = block[:block.index("conn.commit()")]
    assert 'not (result.get("full_description") or "").strip()' in block, (
        "an empty description still takes the success branch, which stamps detail_scraped_at "
        "and NULLs detail_error — the row is then never re-queued and nothing says why")
    assert 'detail_error = ?' in block


def test_the_error_names_the_way_out():
    """§Lessons 15. An error the operator cannot act on is barely better than silence, and this
    one has an obvious action: the page is in front of them."""
    src = open(__import__("applypilot.enrichment.detail", fromlist=["x"]).__file__,
               encoding="utf-8").read()
    assert "Paste it into the Job tab" in src


# ── the escape hatch ────────────────────────────────────────────────────────

def test_pasting_a_description_unsticks_the_job(db):
    text = "About the role. " * 40
    assert wd._save_job_description("http://j/1", text)["ok"]
    row = db.execute("SELECT full_description, detail_error FROM jobs WHERE url = ?",
                     ("http://j/1",)).fetchone()
    assert row[0] == text.strip() and row[1] is None


def test_a_stub_is_refused(db):
    """Worse than nothing: it clears the error, satisfies the tailor queue, and produces a
    résumé written against three sentences."""
    r = wd._save_job_description("http://j/1", "Software engineer wanted.")
    assert r["ok"] is False and "too short" in r["message"]
    row = db.execute("SELECT full_description FROM jobs WHERE url = ?", ("http://j/1",)).fetchone()
    assert not row[0], "a stub was stored anyway"


def test_a_missing_job_is_refused(db):
    assert wd._save_job_description("http://nope/", "x" * 300)["ok"] is False
    assert wd._save_job_description("", "x" * 300)["ok"] is False


def test_it_does_not_claim_a_paste_was_a_scrape(db):
    """`detail_scraped_at` records when the PAGE was visited. Overwriting it would say the
    scraper succeeded at the moment somebody pasted, which is the opposite of what happened."""
    db.execute("UPDATE jobs SET detail_scraped_at = ? WHERE url = ?",
               ("2026-08-01T00:00:00+00:00", "http://j/1"))
    db.commit()
    wd._save_job_description("http://j/1", "About the role. " * 40)
    row = db.execute("SELECT detail_scraped_at FROM jobs WHERE url = ?",
                     ("http://j/1",)).fetchone()
    assert row[0] == "2026-08-01T00:00:00+00:00"


def test_pasting_reaches_the_activity_log(db):
    wd._save_job_description("http://j/1", "About the role. " * 40)
    details = [r[0] for r in db.execute(
        "SELECT detail FROM job_events WHERE job_url = ?", ("http://j/1",)).fetchall()]
    assert any("pasted by hand" in d for d in details)


def test_a_pasted_job_becomes_eligible_for_tailoring(db):
    """The whole point. Before the paste the job is invisible to every downstream queue."""
    assert not jobsrepo.queue_for_tailor(0, 5, db)
    wd._save_job_description("http://j/1", "About the role. " * 40)
    assert [r["url"] for r in jobsrepo.queue_for_tailor(0, 5, db)] == ["http://j/1"]


def test_the_dashboard_still_runs_no_sql():
    """ARCH-4. The write went into web_dashboard.py first and the boundary test caught it."""
    src = open(wd.__file__, encoding="utf-8").read()
    assert "UPDATE jobs SET full_description" not in src, (
        "the description write is back in the view layer; it belongs in repo/jobs.py")
    assert "_jobs.set_description(" in src

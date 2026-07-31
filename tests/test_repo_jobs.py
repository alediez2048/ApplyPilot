"""ARCH-4: repo/jobs.py, and the dashboard endpoints that go through it.

Written because the extraction broke `_mark_submitted` — the row fetch was replaced with
an `exists()` check while the next line still read `row["apply_status"]` — and the whole
suite stayed green. Ruff caught it as an undefined name. Ruff is not a test.

That guard is not cosmetic: it is the only thing stopping "Mark submitted ✓" from recording
an application that was never filled or reviewed.
"""

from __future__ import annotations

import pytest

import applypilot.database as database
from applypilot.repo import jobs as repo


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    return database.get_connection(path)


def _job(conn, url="http://j/1", **cols):
    base = {"title": "PM", "site": "Greenhouse", "strategy": "dashboard_upload",
            "discovered_at": "2026-07-20T10:00:00+00:00"}
    base.update(cols)
    keys = ", ".join(["url", *base])
    marks = ", ".join("?" for _ in range(len(base) + 1))
    conn.execute(f"INSERT INTO jobs ({keys}) VALUES ({marks})", (url, *base.values()))
    conn.commit()
    return url


# ── the guard that was silently removed ─────────────────────────────────────

def test_mark_submitted_requires_a_job_awaiting_review(db, monkeypatch):
    from applypilot import web_dashboard as wd
    monkeypatch.setattr(wd, "get_connection", lambda *a, **k: db)
    monkeypatch.setattr(wd, "init_db", lambda *a, **k: db)

    url = _job(db, apply_status="ready")
    r = wd._mark_submitted(url)
    assert r["ok"] is False and "not awaiting review" in r["message"]
    assert repo.applied_at(url, db) is None, "a job that was never filled got marked applied"


def test_mark_submitted_accepts_a_copilot_filled_job(db, monkeypatch):
    from applypilot import web_dashboard as wd
    monkeypatch.setattr(wd, "get_connection", lambda *a, **k: db)
    monkeypatch.setattr(wd, "init_db", lambda *a, **k: db)

    url = _job(db, apply_status="ready_to_submit")
    assert wd._mark_submitted(url)["ok"] is True
    assert repo.applied_at(url, db)
    assert repo.apply_status(url, db) == "applied"


def test_mark_submitted_rejects_unknown_and_empty_urls(db, monkeypatch):
    from applypilot import web_dashboard as wd
    monkeypatch.setattr(wd, "get_connection", lambda *a, **k: db)
    monkeypatch.setattr(wd, "init_db", lambda *a, **k: db)
    assert wd._mark_submitted("")["ok"] is False
    assert wd._mark_submitted("http://nope")["ok"] is False


# ── reject / restore round trip ─────────────────────────────────────────────

def test_reject_then_restore_preserves_that_you_applied(db):
    """`applied_at` must survive a rejection — the record that you DID apply is the point."""
    url = _job(db, apply_status="applied", applied_at="2026-07-21T00:00:00+00:00")
    repo.mark_rejected(url, db)
    assert repo.apply_status(url, db) == "rejected"
    assert repo.applied_at(url, db), "applied_at was cleared by rejecting"

    repo.unmark_rejected(url, "applied" if repo.applied_at(url, db) else None, db)
    assert repo.apply_status(url, db) == "applied"
    assert repo.get(url, db)["rejected_at"] is None


def test_restoring_a_never_applied_job_clears_the_status(db):
    url = _job(db, apply_status="ready")
    repo.mark_rejected(url, db)
    repo.unmark_rejected(url, "applied" if repo.applied_at(url, db) else None, db)
    assert repo.apply_status(url, db) is None


# ── queues ──────────────────────────────────────────────────────────────────

def test_queues_only_return_operator_added_jobs(db):
    _job(db, "http://j/mine")
    _job(db, "http://j/found", strategy="jobspy")     # discovery-sourced
    assert [j["url"] for j in repo.queue_needing_detail(conn=db)] == ["http://j/mine"]


def test_tailor_queue_respects_the_attempt_cap(db):
    """Five failed attempts is where a job stops burning LLM calls forever."""
    _job(db, "http://j/a", full_description="d", tailor_attempts=4)
    _job(db, "http://j/b", full_description="d", tailor_attempts=5)
    urls = [j["url"] for j in repo.queue_for_tailor(conn=db)]
    assert urls == ["http://j/a"]


def test_cover_queue_treats_empty_string_as_missing(db):
    """`cover_letter_path = ''` is not a cover letter; an IS NULL check alone misses it."""
    _job(db, "http://j/a", full_description="d", tailored_resume_path="/r", cover_letter_path="")
    assert [j["url"] for j in repo.queue_for_cover(conn=db)] == ["http://j/a"]


def test_apply_queue_skips_in_progress_and_applied(db):
    _job(db, "http://j/ok", tailored_resume_path="/r")
    _job(db, "http://j/busy", tailored_resume_path="/r", apply_status="in_progress")
    _job(db, "http://j/done", tailored_resume_path="/r", applied_at="2026-07-21T00:00:00+00:00")
    urls = [j["url"] for j in repo.queue_for_apply(10, 3, db)]
    assert urls == ["http://j/ok"]


def test_bypass_scoring_only_touches_unscored_enriched_queue_rows(db):
    _job(db, "http://j/a", full_description="d")
    _job(db, "http://j/b", full_description="d", fit_score=3)     # already scored
    _job(db, "http://j/c")                                        # not enriched
    assert repo.bypass_scoring(db) == 1
    assert repo.get("http://j/a", db)["fit_score"] == 10
    assert repo.get("http://j/b", db)["fit_score"] == 3


# ── import ──────────────────────────────────────────────────────────────────

def test_touch_import_matches_on_the_application_url_too(db):
    """A job can be known by either URL; pasting the ATS link must adopt the existing row
    rather than quietly failing to match and creating a duplicate."""
    _job(db, "http://j/canonical", strategy="jobspy",
         application_url="http://ats/apply/1")
    repo.touch_import("http://ats/apply/1", "http://ats/apply/1", db)
    assert repo.get("http://j/canonical", db)["strategy"] == "dashboard_upload"


def test_delete_removes_contacts_but_only_for_operator_added_jobs(db):
    from applypilot.networking import store
    store.init_contacts(db)
    url = _job(db, "http://j/mine")
    store.upsert_contact({"job_url": url, "full_name": "Jane"}, db)
    found = _job(db, "http://j/found", strategy="jobspy")

    assert repo.delete(found, db) == 0, "a discovered job should not be deletable here"
    assert repo.delete(url, db) == 1
    assert repo.get(url, db) is None
    assert db.execute("SELECT COUNT(*) FROM contacts WHERE job_url=?", (url,)).fetchone()[0] == 0


# ── the boundary itself ─────────────────────────────────────────────────────

def test_web_dashboard_runs_no_sql_at_all():
    """ARCH-4's headline criterion: web_dashboard.py contains ZERO SQL (was 39 statements).

    A view layer that can reach the database will, and every statement added there is one
    more place a schema change has to be found. The boundary only holds if crossing it fails.
    """
    from applypilot import web_dashboard as wd
    src = (wd._STATIC_DIR.parent / "web_dashboard.py").read_text(encoding="utf-8")
    hits = [f"{i}: {ln.strip()[:90]}" for i, ln in enumerate(src.splitlines(), 1)
            if ".execute(" in ln]
    assert not hits, "SQL is back in web_dashboard.py:\n  " + "\n  ".join(hits[:8])


def test_sql_lives_only_in_the_data_layer():
    """The wider boundary. Listed explicitly so adding a module here is a decision.

    Still-unmigrated modules are named rather than wildcarded — the list is the remaining
    ARCH-4 scope, and it should only ever shrink.
    """
    import pathlib

    import applypilot
    root = pathlib.Path(applypilot.__file__).parent
    allowed = {
        "database.py", "repo/jobs.py",                       # the data layer proper
        "migrations/__init__.py",                            # the migration runner (ARCH-5)
        "migrations/m001_touches_backfill.py",               # migrations ARE schema changes
        "migrations/m002_messages_per_contact.py",           # same — a table rebuild, by definition
        "networking/store.py", "networking/touches.py",      # per-table repositories
        "networking/messages.py",                            # the CRM-4 conversation store
        "networking/connections.py", "networking/backfill_touches.py",
        # --- not yet migrated (remaining ARCH-4 scope) ---
        "enrichment/detail.py", "apply/launcher.py", "view.py", "cli.py", "pipeline.py",
        "discovery/jobspy.py", "discovery/workday.py", "discovery/smartextract.py",
        "scoring/scorer.py", "scoring/cover_letter.py", "scoring/tailor.py",
        "networking/gmail_send.py", "networking/service.py",
        "networking/linkedin_agent.py",
    }
    # Detect SQL, not the string ".execute(". The Google API client uses the same call shape —
    # `service.users().threads().get(...).execute()` — so the old heuristic flagged every Gmail
    # module as a data-layer violation, which is why gmail_oauth.py and gmail_send.py sit in the
    # allowlist below for a rule they never actually broke. A false positive here is not
    # harmless: it pushes real modules onto an allowlist that is supposed to only ever shrink.
    import re
    sql_execute = re.compile(
        r"""(?:conn|cur|cursor|db|self\._conn)\.execute(?:many|script)?\(     # a DB handle
          | \.execute(?:many|script)?\(\s*                                    # or a literal
            (?:f|r|u|b)*["']{1,3}\s*
            (?:SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|PRAGMA|BEGIN|WITH|REPLACE)\b""",
        re.IGNORECASE | re.VERBOSE,
    )
    offenders = sorted(
        str(p.relative_to(root)) for p in root.rglob("*.py")
        if sql_execute.search(p.read_text(encoding="utf-8"))
        and str(p.relative_to(root)) not in allowed
    )
    assert not offenders, (
        "new modules are executing SQL directly: " + ", ".join(offenders) +
        "\nRoute them through repo/ or a table's repository instead."
    )

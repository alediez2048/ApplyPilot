"""Queries against the `jobs` table (ARCH-4).

Every statement that `web_dashboard.py` used to run inline. Grouped by what the caller
wants, not by what SQL it takes — `queue_for_tailor()` rather than a query builder.

`QUEUE_SQL` is the one piece of shared vocabulary: "a job the operator pasted in", as
opposed to one discovery found. It lived in `web_dashboard.py` as `_URL_QUEUE_SQL`, which
meant the definition of the dashboard's working set was owned by the view layer.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from applypilot.database import get_connection

# Jobs the operator added by hand. Discovery-sourced rows are deliberately excluded from
# the dashboard's prepare/apply queues (DISC-1 will give them their own bucket).
QUEUE_STRATEGIES = ("dashboard_upload", "manual_url_batch")
QUEUE_SQL = "strategy IN ('dashboard_upload', 'manual_url_batch')"


def _c(conn: sqlite3.Connection | None) -> sqlite3.Connection:
    return conn if conn is not None else get_connection()


def _dicts(rows) -> list[dict]:
    return [dict(zip(r.keys(), r)) for r in rows]


def _dict(row) -> dict | None:
    return dict(zip(row.keys(), row)) if row else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── single-row reads ────────────────────────────────────────────────────────

def get(url: str, conn: sqlite3.Connection | None = None) -> dict | None:
    return _dict(_c(conn).execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone())


def find_by_any_url(url: str, conn: sqlite3.Connection | None = None) -> dict | None:
    """Match on either the canonical URL or the ATS application URL.

    An operator may paste either one, and imports store both.
    """
    return _dict(_c(conn).execute(
        "SELECT url, title, company, site, application_url, full_description "
        "FROM jobs WHERE url = ? OR application_url = ? LIMIT 1", (url, url)).fetchone())


def import_state(url: str, conn: sqlite3.Connection | None = None) -> dict | None:
    """What an existing row looks like to the importer (dedupe + 'already applied' notice)."""
    return _dict(_c(conn).execute(
        "SELECT url, strategy, applied_at, apply_status, apply_error, tailored_resume_path "
        "FROM jobs WHERE url = ? OR application_url = ? LIMIT 1", (url, url)).fetchone())


def apply_status(url: str, conn: sqlite3.Connection | None = None) -> str | None:
    row = _c(conn).execute("SELECT apply_status FROM jobs WHERE url = ?", (url,)).fetchone()
    return (row["apply_status"] if row else None)


def apply_state(url: str, conn: sqlite3.Connection | None = None) -> dict | None:
    return _dict(_c(conn).execute(
        "SELECT apply_status, applied_at FROM jobs WHERE url = ?", (url,)).fetchone())


def exists(url: str, conn: sqlite3.Connection | None = None) -> bool:
    return _c(conn).execute("SELECT 1 FROM jobs WHERE url = ?", (url,)).fetchone() is not None


def applied_at(url: str, conn: sqlite3.Connection | None = None) -> str | None:
    row = _c(conn).execute("SELECT applied_at FROM jobs WHERE url = ?", (url,)).fetchone()
    return (row["applied_at"] if row else None)


def detail_outcome(url: str, conn: sqlite3.Connection | None = None) -> dict | None:
    return _dict(_c(conn).execute(
        "SELECT detail_error, full_description FROM jobs WHERE url = ?", (url,)).fetchone())


def materials_present(url: str, conn: sqlite3.Connection | None = None) -> dict | None:
    """Which of the three prep artefacts a job already has."""
    return _dict(_c(conn).execute(
        "SELECT (full_description IS NOT NULL) AS enr, (tailored_resume_path IS NOT NULL) AS res, "
        "(cover_letter_path IS NOT NULL) AS cov FROM jobs WHERE url = ?", (url,)).fetchone())


def queued_for_delete(url: str, conn: sqlite3.Connection | None = None) -> dict | None:
    """Only operator-added jobs are deletable — a discovered row would just come back."""
    return _dict(_c(conn).execute(
        f"SELECT title, site FROM jobs WHERE url = ? AND {QUEUE_SQL}", (url,)).fetchone())


# ── queue reads (the prepare pipeline) ──────────────────────────────────────

def queue_needing_detail(limit: int = 0, conn: sqlite3.Connection | None = None) -> list[dict]:
    rows = _c(conn).execute(
        f"SELECT url, title, site FROM jobs WHERE {QUEUE_SQL} AND detail_scraped_at IS NULL "
        f"ORDER BY discovered_at DESC, rowid DESC").fetchall()
    return _dicts(rows[:limit] if limit > 0 else rows)


def queue_for_tailor(limit: int = 0, max_attempts: int = 5,
                     conn: sqlite3.Connection | None = None) -> list[dict]:
    rows = _c(conn).execute(
        f"SELECT * FROM jobs WHERE {QUEUE_SQL} AND full_description IS NOT NULL "
        f"AND tailored_resume_path IS NULL AND COALESCE(tailor_attempts, 0) < ? "
        f"ORDER BY discovered_at DESC, rowid DESC", (max_attempts,)).fetchall()
    return _dicts(rows[:limit] if limit > 0 else rows)


def queue_for_cover(limit: int = 0, max_attempts: int = 5,
                    conn: sqlite3.Connection | None = None) -> list[dict]:
    rows = _c(conn).execute(
        f"SELECT * FROM jobs WHERE {QUEUE_SQL} AND full_description IS NOT NULL "
        f"AND tailored_resume_path IS NOT NULL "
        f"AND (cover_letter_path IS NULL OR cover_letter_path = '') "
        f"AND COALESCE(cover_attempts, 0) < ? "
        f"ORDER BY discovered_at DESC, rowid DESC", (max_attempts,)).fetchall()
    return _dicts(rows[:limit] if limit > 0 else rows)


def queue_for_apply(limit: int, max_attempts: int,
                    conn: sqlite3.Connection | None = None) -> list[dict]:
    """Deliberately narrower than the others: 'dashboard_upload' only, and it leaves
    `in_progress` and attempt-exhausted jobs alone so a retry never double-applies."""
    return _dicts(_c(conn).execute(
        "SELECT url, title, site FROM jobs "
        "WHERE strategy = 'dashboard_upload' AND tailored_resume_path IS NOT NULL "
        "AND applied_at IS NULL "
        "AND (apply_status IS NULL OR apply_status = '' OR apply_status = 'failed' "
        "     OR apply_status = 'dryrun') "
        "AND COALESCE(apply_attempts, 0) < ? "
        "ORDER BY discovered_at DESC, rowid DESC LIMIT ?", (max_attempts, limit)).fetchall())


def dashboard_rows(limit: int = 500, conn: sqlite3.Connection | None = None) -> list:
    """The main table. Returns raw Rows — the caller reads columns by name and the
    ordering below is UI precedence, not a data rule."""
    return _c(conn).execute(f"""
        SELECT url, title, site, salary, location, full_description, application_url, detail_error,
               fit_score, score_reasoning, tailored_resume_path, cover_letter_path,
               apply_status, apply_error, apply_attempts, applied_at,
               last_attempted_at, apply_duration_ms, rejected_at
        FROM jobs
        WHERE {QUEUE_SQL}
        ORDER BY
          CASE
            WHEN apply_status = 'rejected' THEN 6            -- rejected pile sinks to the bottom
            WHEN applied_at IS NOT NULL THEN 0
            WHEN apply_status = 'in_progress' THEN 1
            WHEN tailored_resume_path IS NOT NULL THEN 2
            WHEN {QUEUE_SQL} AND (full_description IS NULL OR lower(trim(full_description)) = 'null') THEN 3
            WHEN fit_score IS NOT NULL THEN 4
            ELSE 5
          END,
          rejected_at DESC NULLS LAST,
          applied_at DESC NULLS LAST,
          discovered_at DESC,
          fit_score DESC NULLS LAST
        LIMIT ?
    """, (limit,)).fetchall()


def awaiting_human(conn: sqlite3.Connection | None = None) -> list[dict]:
    """Jobs whose co-pilot browser is open and waiting on the operator.

    Starting another apply closes that browser (launch clears the CDP port), so this is the
    gate on batch apply — see the queue guard in `run_dashboard_apply`.
    """
    return _dicts(_c(conn).execute(
        "SELECT url, title, apply_status FROM jobs "
        "WHERE apply_status IN ('ready_to_submit', 'needs_human') "
        "ORDER BY last_attempted_at DESC").fetchall())


# ── aggregates ──────────────────────────────────────────────────────────────

def queue_stats(conn: sqlite3.Connection | None = None) -> dict:
    return _dict(_c(conn).execute(f"""
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN full_description IS NOT NULL AND lower(trim(full_description)) != 'null' THEN 1 ELSE 0 END) AS enriched,
          SUM(CASE WHEN fit_score IS NOT NULL THEN 1 ELSE 0 END) AS scored,
          SUM(CASE WHEN tailored_resume_path IS NOT NULL THEN 1 ELSE 0 END) AS tailored,
          SUM(CASE WHEN cover_letter_path IS NOT NULL THEN 1 ELSE 0 END) AS covers,
          SUM(CASE WHEN tailored_resume_path IS NOT NULL AND applied_at IS NULL AND (apply_status IS NULL OR apply_status = '') THEN 1 ELSE 0 END) AS ready,
          SUM(CASE WHEN applied_at IS NOT NULL THEN 1 ELSE 0 END) AS applied,
          SUM(CASE WHEN apply_error IS NOT NULL THEN 1 ELSE 0 END) AS errors,
          SUM(CASE WHEN apply_status = 'in_progress' THEN 1 ELSE 0 END) AS in_progress
        FROM jobs
        WHERE {QUEUE_SQL}
    """).fetchone())


def lifetime_stats(conn: sqlite3.Connection | None = None) -> dict:
    """Across every job ever, including discovery-sourced ones."""
    return _dict(_c(conn).execute("""
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN applied_at IS NOT NULL THEN 1 ELSE 0 END) AS applied,
          SUM(CASE WHEN apply_error IS NOT NULL THEN 1 ELSE 0 END) AS errors
        FROM jobs
    """).fetchone())


# ── writes ──────────────────────────────────────────────────────────────────

def bypass_scoring(conn: sqlite3.Connection | None = None) -> int:
    """A pasted URL is an explicit decision to apply, so it skips fit scoring.

    Returns the number of rows marked.
    """
    conn = _c(conn)
    n = conn.execute(
        f"UPDATE jobs SET fit_score = 10, "
        f"score_reasoning = 'User-imported URL. Fit scoring intentionally bypassed.', "
        f"scored_at = ? WHERE {QUEUE_SQL} AND full_description IS NOT NULL AND fit_score IS NULL",
        (_now(),)).rowcount
    conn.commit()
    return n


def set_tailored(url: str, path: str, conn: sqlite3.Connection | None = None) -> None:
    conn = _c(conn)
    conn.execute("UPDATE jobs SET tailored_resume_path=?, tailored_at=?, "
                 "tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?", (path, _now(), url))
    conn.commit()


def bump_tailor_attempts(url: str, conn: sqlite3.Connection | None = None) -> None:
    conn = _c(conn)
    conn.execute("UPDATE jobs SET tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?", (url,))
    conn.commit()


def set_cover(url: str, path: str, conn: sqlite3.Connection | None = None) -> None:
    conn = _c(conn)
    conn.execute("UPDATE jobs SET cover_letter_path=?, cover_letter_at=?, "
                 "cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?", (path, _now(), url))
    conn.commit()


def bump_cover_attempts(url: str, conn: sqlite3.Connection | None = None) -> None:
    conn = _c(conn)
    conn.execute("UPDATE jobs SET cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?", (url,))
    conn.commit()


def reset_apply_state(url: str, conn: sqlite3.Connection | None = None) -> None:
    """Clean slate for an explicit re-apply.

    Clears `applied_at` too — that is the entire point of Re-apply, and the UI
    double-confirms before calling it.
    """
    conn = _c(conn)
    conn.execute("UPDATE jobs SET apply_status=NULL, apply_error=NULL, apply_attempts=0, "
                 "agent_id=NULL, applied_at=NULL WHERE url=?", (url,))
    conn.commit()


def mark_applied(url: str, conn: sqlite3.Connection | None = None) -> str:
    conn = _c(conn)
    now = _now()
    conn.execute("UPDATE jobs SET apply_status = 'applied', applied_at = ?, apply_error = NULL "
                 "WHERE url = ?", (now, url))
    conn.commit()
    return now


def mark_rejected(url: str, conn: sqlite3.Connection | None = None) -> str:
    conn = _c(conn)
    now = _now()
    conn.execute("UPDATE jobs SET apply_status = 'rejected', rejected_at = ? WHERE url = ?",
                 (now, url))
    conn.commit()
    return now


def unmark_rejected(url: str, restored_status: str | None,
                    conn: sqlite3.Connection | None = None) -> None:
    conn = _c(conn)
    conn.execute("UPDATE jobs SET apply_status = ?, rejected_at = NULL WHERE url = ?",
                 (restored_status, url))
    conn.commit()


def touch_import(url: str, application_url: str, conn: sqlite3.Connection | None = None) -> None:
    """Re-importing a URL refreshes its queue membership without losing prep work.

    Matches on `url OR application_url` — the same row can be known by either, and pasting
    the ATS link for a job discovered under its canonical URL must adopt the existing row
    rather than silently failing to match.
    """
    conn = _c(conn)
    conn.execute(
        "UPDATE jobs SET strategy = 'dashboard_upload', discovered_at = ?, "
        "application_url = COALESCE(NULLIF(application_url, ''), ?) "
        "WHERE url = ? OR application_url = ?",
        (_now(), application_url, url, url))
    conn.commit()


def insert_imported(url: str, title: str, company: str, site: str, application_url: str,
                    conn: sqlite3.Connection | None = None) -> None:
    conn = _c(conn)
    conn.execute(
        "INSERT INTO jobs (url, title, company, site, strategy, discovered_at, application_url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (url, title, company, site, "dashboard_upload", _now(), application_url))
    conn.commit()


def delete(url: str, conn: sqlite3.Connection | None = None) -> int:
    """Delete an operator-added job and its contacts.

    SQLite has no FK cascade here, so the contacts go explicitly. Touches/sequences are
    keyed by contact_id and are cleaned by `touches.delete_for_contact`.
    """
    conn = _c(conn)
    n = conn.execute(f"DELETE FROM jobs WHERE url = ? AND {QUEUE_SQL}", (url,)).rowcount
    conn.execute("DELETE FROM contacts WHERE job_url = ?", (url,))
    conn.commit()
    return n

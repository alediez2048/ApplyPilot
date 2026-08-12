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
from applypilot.repo import spaces as _spaces

# Jobs the operator added by hand. Discovery-sourced rows are deliberately excluded from
# the dashboard's prepare/apply queues (DISC-1 will give them their own bucket).
#
# PROVENANCE, not membership (SPACE-1a D2). This says how a row arrived; `space_id` says where
# it belongs, and the two are never asked to do each other's job. A target row keeps
# `dashboard_upload` — the operator did paste it in — which is why the shape gate below exists
# rather than a private strategy value that would make targets invisible to `delete_job`.
QUEUE_STRATEGIES = ("dashboard_upload", "manual_url_batch")
QUEUE_SQL = "strategy IN ('dashboard_upload', 'manual_url_batch')"


def _c(conn: sqlite3.Connection | None) -> sqlite3.Connection:
    return conn if conn is not None else get_connection()


def _in_spaces(ids: list[str]) -> tuple[str, list[str]]:
    """`AND space_id IN (?, ?)` plus its bindings, for the pipeline stage queues.

    Built rather than interpolated because a Space id is operator-supplied text. It is also
    never empty: `repo/spaces` raises rather than handing back `[]`, so there is no path here
    that produces `IN ()` and silently selects nothing.
    """
    return " AND space_id IN (" + ", ".join("?" for _ in ids) + ")", list(ids)


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
        # This row is what the DRAFTING path is handed — `_network._run` builds the job dict from
        # here and passes it to `find_contacts_for_job` → `draft_email`. So every column the
        # prompts read has to be in this list, and each one added below was missing:
        #
        # `space_id` decides WHICH prompt writes the email; without it a targets contact was
        # drafted with the job-seeker prompt.
        #
        # `job_context` / `job_ask` are CTX-2's operator boxes. They were absent for two days:
        # the operator could type context onto a row, click Find contacts, and every draft came
        # back written without it — while REGENERATING a draft went through `get()`'s `SELECT *`
        # and read it correctly. So the feature worked on the path nobody takes first. It went
        # unnoticed only because no row has carried a context yet.
        #
        # Same failure both times, and §Lessons 47 names it: a column the caller needs belongs in
        # the SELECT. Note what makes this shape so durable — the WRITE side is perfect, the read
        # is silent, and the value that arrives is a plausible empty string rather than an error.
        "SELECT url, title, company, site, application_url, full_description, space_id, "
        "job_context, job_ask "
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
    c = _c(conn)
    scope, ids = _in_spaces(_spaces.jobs_shaped_ids(c))
    rows = c.execute(
        f"SELECT url, title, site FROM jobs WHERE {QUEUE_SQL}{scope} "
        f"AND detail_scraped_at IS NULL "
        f"ORDER BY discovered_at DESC, rowid DESC", ids).fetchall()
    return _dicts(rows[:limit] if limit > 0 else rows)


def queue_for_tailor(limit: int = 0, max_attempts: int = 5,
                     conn: sqlite3.Connection | None = None) -> list[dict]:
    c = _c(conn)
    scope, ids = _in_spaces(_spaces.document_making_ids(c))
    rows = c.execute(
        f"SELECT * FROM jobs WHERE {QUEUE_SQL}{scope} AND full_description IS NOT NULL "
        f"AND tailored_resume_path IS NULL AND COALESCE(tailor_attempts, 0) < ? "
        f"ORDER BY discovered_at DESC, rowid DESC", (*ids, max_attempts)).fetchall()
    return _dicts(rows[:limit] if limit > 0 else rows)


def queue_for_cover(limit: int = 0, max_attempts: int = 5,
                    conn: sqlite3.Connection | None = None) -> list[dict]:
    c = _c(conn)
    scope, ids = _in_spaces(_spaces.document_making_ids(c))
    rows = c.execute(
        f"SELECT * FROM jobs WHERE {QUEUE_SQL}{scope} AND full_description IS NOT NULL "
        f"AND tailored_resume_path IS NOT NULL "
        f"AND (cover_letter_path IS NULL OR cover_letter_path = '') "
        f"AND COALESCE(cover_attempts, 0) < ? "
        f"ORDER BY discovered_at DESC, rowid DESC", (*ids, max_attempts)).fetchall()
    return _dicts(rows[:limit] if limit > 0 else rows)


def queue_for_apply(limit: int, max_attempts: int,
                    conn: sqlite3.Connection | None = None) -> list[dict]:
    """Deliberately narrower than the others: 'dashboard_upload' only, and it leaves
    `in_progress` and attempt-exhausted jobs alone so a retry never double-applies."""
    c = _c(conn)
    scope, ids = _in_spaces(_spaces.jobs_shaped_ids(c))
    return _dicts(c.execute(
        f"SELECT url, title, site FROM jobs "
        f"WHERE strategy = 'dashboard_upload'{scope} AND tailored_resume_path IS NOT NULL "
        f"AND applied_at IS NULL "
        f"AND (apply_status IS NULL OR apply_status = '' OR apply_status = 'failed' "
        f"     OR apply_status = 'dryrun') "
        f"AND COALESCE(apply_attempts, 0) < ? "
        f"ORDER BY discovered_at DESC, rowid DESC LIMIT ?",
        (*ids, max_attempts, limit)).fetchall())


def all_descriptions(conn: sqlite3.Connection | None = None,
                     space_id: str | None = None) -> dict[str, str]:
    """{url: full_description} for every operator-added job, in ONE query.

    Search needs the whole text — `/api/status` ships a 900-char excerpt, so a term in
    paragraph six of a posting was unfindable. Shipping the full text on that payload instead
    would add ~130KB to a refresh that runs every 2.5 seconds, for a field used only while
    typing. Fetched once per session on the first search instead (UX-6).
    """
    scope, args = _one_space(space_id)
    rows = _c(conn).execute(
        f"SELECT url, full_description FROM jobs WHERE {QUEUE_SQL}{scope}", args).fetchall()
    out = {}
    for r in rows:
        text = (r["full_description"] or "").strip()
        out[r["url"]] = "" if text.lower() == "null" else text
    return out


def _one_space(space_id: str | None) -> tuple[str, list[str]]:
    """`AND space_id = ?` plus its binding, or nothing at all when no Space is named.

    Distinct from `_in_spaces`, and the difference is which question is being asked. The stage
    queues ask *may the pipeline touch this row* and have no safe empty answer, so they resolve
    a list from the registry and raise when it is empty. This asks *which panel is on screen*,
    where "all of them" is a legitimate answer and is what every caller predating Spaces means.
    """
    return (" AND space_id = ?", [space_id]) if space_id else ("", [])


def dashboard_rows(limit: int = 500, conn: sqlite3.Connection | None = None,
                   space_id: str | None = None) -> list:
    """The main table. Returns raw Rows — the caller reads columns by name and the
    ordering below is UI precedence, not a data rule.

    `space_id` is a WHERE clause and nothing more (SPACE-2). The budget test holds at 80
    because filtering costs no statement — the cost of Spaces on this path is resolving the nav
    list, which is one SELECT whatever the number of Spaces.
    """
    scope, args = _one_space(space_id)
    return _c(conn).execute(f"""
        SELECT url, title, company, site, salary, location, full_description, application_url, detail_error,
               fit_score, score_reasoning, tailored_resume_path, cover_letter_path,
               apply_status, apply_error, apply_attempts, applied_at,
               last_attempted_at, apply_duration_ms, rejected_at, interview_at,
               job_context, job_ask
        FROM jobs
        WHERE {QUEUE_SQL}{scope}
        ORDER BY
          CASE
            -- EVERY closed state sinks, and the list is generated from CLOSED_STATUSES rather
            -- than typed out. Naming only 'rejected' here left a cancelled job sorting with LIVE
            -- work, above jobs still being prepared — a silent miss found by sweeping for the
            -- string, and one a hand-written list would have repeated for `ghost`.
            WHEN apply_status IN ({_CLOSED_SQL}) THEN 6
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
    """, (*args, limit)).fetchall()


def add_target(space_id: str, name: str, domain: str = "",
               conn: sqlite3.Connection | None = None) -> dict:
    """Add one company to a `pipeline/targets` Space. Idempotent on the anchor.

    Returns `{"url", "name", "added"}` — `added` False means the row was already there, which
    is a normal outcome of pasting a list twice and must not read as a failure.

    `strategy` is `dashboard_upload` because the operator did paste it in (SPACE-1a D2). The
    row is kept off the six-stage pipeline by its Space's SHAPE, not by hiding it behind a
    private strategy value — that would also hide it from `delete_job`, which is
    `DELETE ... AND {QUEUE_SQL}`, leaving a target you could create and never remove.

    `detail_scraped_at` is stamped at creation. There is no page to fetch — the operator stated
    the company — and leaving it NULL would mark the row as owing a scrape forever, which is
    how a target ends up looking like a job that failed to enrich.
    """
    from applypilot.domain import target as _target

    url = _target.anchor(space_id, name)
    if not url:
        raise ValueError(f"{name!r} does not yield a usable slug")
    c = _c(conn)
    existing = c.execute("SELECT url FROM jobs WHERE url = ?", (url,)).fetchone()
    if existing:
        return {"url": url, "name": name, "added": False}
    now = _now()
    c.execute(
        "INSERT INTO jobs (url, title, company, site, strategy, space_id, discovered_at, "
        "detail_scraped_at, application_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (url, name, name, domain or "", "dashboard_upload", space_id, now, now,
         f"https://{domain}" if domain else ""))
    c.commit()
    return {"url": url, "name": name, "added": True}


def set_about(url: str, text: str, conn: sqlite3.Connection | None = None) -> bool:
    """What a company DOES, on its card. Returns whether anything changed.

    Separate from `attach_posting` on purpose, even though both end up in `full_description`:
    that one means a job posting arrived and stamps the scrape columns to say the row is no
    longer owed a fetch. This is a blurb the operator supplied about the employer, and claiming
    a page was scraped for it would make a hand-typed sentence indistinguishable from a real
    description later.

    An empty `text` is a NO-OP, never a clear. A sheet re-imported without the About column must
    not wipe what an earlier one supplied — the same rule the contact fields follow, and the bug
    that erased a LinkedIn URL before it was caught.
    """
    body = (text or "").strip()
    if not body:
        return False
    c = _c(conn)
    row = c.execute("SELECT full_description FROM jobs WHERE url = ?", (url,)).fetchone()
    if not row or (row["full_description"] or "").strip() == body:
        return False
    c.execute("UPDATE jobs SET full_description = ? WHERE url = ?", (body, url))
    c.commit()
    return True


def attach_posting(url: str, *, title: str = "", application_url: str = "",
                   description: str = "", conn: sqlite3.Connection | None = None) -> dict:
    """Give an existing card a job posting, IN PLACE (SHEET-1 C4).

    **The anchor never moves, and that is the whole function.** `store.contact_id()` hashes
    `job_url`, and on a target row the anchor IS the `job_url` — so replacing it with the
    posting's URL silently orphans every contact, every `touches` ladder, every `sequences` row
    and every stored message on that card. Nothing would error; the card would simply render
    with no people. That is CO-1's failure with a new trigger, and it is why this is an UPDATE of
    three columns rather than the insert it looks like it should be.

    `title`, `application_url` and `full_description` already exist on every `jobs` row and sit
    empty on a target, so this needs no schema change — the row was always able to hold a
    posting, nothing had ever put one there.

    Only fills what it is GIVEN: a caller that knows the link but not the description must not
    blank a description the operator typed. Returns the fields that changed.
    """
    c = _c(conn)
    row = c.execute("SELECT url, company FROM jobs WHERE url = ?", (url,)).fetchone()
    if not row:
        raise ValueError(f"no such row: {url!r}")

    sets: dict[str, str] = {}
    if (title or "").strip():
        sets["title"] = title.strip()
    if (application_url or "").strip():
        sets["application_url"] = application_url.strip()
    if (description or "").strip():
        sets["full_description"] = description.strip()
        # It has a description now, so it is no longer owed a scrape and must not read as one
        # that failed (§Lessons 44: a partial with nothing in it is a permanent silent death).
        sets["detail_error"] = ""
        sets["detail_scraped_at"] = _now()
    if not sets:
        return {"url": url, "changed": []}

    assignments = ", ".join(f"{k} = ?" for k in sets)
    c.execute(f"UPDATE jobs SET {assignments} WHERE url = ?", [*sets.values(), url])
    c.commit()
    return {"url": url, "changed": sorted(sets)}


def in_progress(conn: sqlite3.Connection | None = None) -> list[dict]:
    """Jobs an apply agent is working on RIGHT NOW.

    Used by the pause action to tell the operator what it is about to interrupt, and to refuse
    when nothing is running rather than leaving a pause flag behind for the next run to trip on.
    """
    return _dicts(_c(conn).execute(
        "SELECT url, title, last_attempted_at FROM jobs "
        "WHERE apply_status = 'in_progress' ORDER BY last_attempted_at DESC").fetchall())


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

def queue_stats(conn: sqlite3.Connection | None = None,
                space_id: str | None = None) -> dict:
    """Counts for the status strip. Scoped to one Space when the panel is."""
    scope, args = _one_space(space_id)
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
        WHERE {QUEUE_SQL}{scope}
    """, args).fetchone())


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
    scope, ids = _in_spaces(_spaces.jobs_shaped_ids(conn))
    n = conn.execute(
        f"UPDATE jobs SET fit_score = 10, "
        f"score_reasoning = 'User-imported URL. Fit scoring intentionally bypassed.', "
        f"scored_at = ? WHERE {QUEUE_SQL}{scope} "
        f"AND full_description IS NOT NULL AND fit_score IS NULL",
        (_now(), *ids)).rowcount
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


def was_attempted(url: str, conn: sqlite3.Connection | None = None) -> bool:
    """Has an apply agent ever actually opened this job?

    The gate on "Mark submitted ✓". The operator is the authority on whether they submitted an
    application, but they should not be able to bless one the app never even ran — that would
    quietly turn the button into a way to fabricate an application record.
    """
    row = _c(conn).execute(
        "SELECT last_attempted_at, apply_attempts, apply_status FROM jobs WHERE url = ?",
        (url,)).fetchone()
    if not row:
        return False
    d = dict(row)
    if d.get("last_attempted_at") or d.get("apply_attempts"):
        return True
    # These statuses can ONLY be reached by an agent run, so they are evidence in their own
    # right — a co-pilot handover proves the form was opened and filled even if the timestamp
    # is missing. 'ready' and 'rejected' are deliberately absent: 'ready' means materials were
    # prepared and nothing more.
    return d.get("apply_status") in ("ready_to_submit", "needs_human", "in_progress", "failed")


def mark_applied(url: str, conn: sqlite3.Connection | None = None) -> str:
    conn = _c(conn)
    now = _now()
    conn.execute("UPDATE jobs SET apply_status = 'applied', applied_at = ?, apply_error = NULL "
                 "WHERE url = ?", (now, url))
    conn.commit()
    return now


def unmark_applied(url: str, conn: sqlite3.Connection | None = None) -> None:
    """Undo an operator-asserted "applied". Clears the stamp, keeps everything else.

    Deliberately NOT `reset_apply_state`, which also wipes `apply_attempts`, `agent_id` and
    `apply_error` because Re-apply is about to overwrite them anyway. Undoing a misclick must
    not also erase the record that an agent ran and what happened to it — that history is the
    only account of a real attempt.
    """
    conn = _c(conn)
    conn.execute("UPDATE jobs SET apply_status = NULL, applied_at = NULL WHERE url = ?", (url,))
    conn.commit()


#: The three ways a job leaves the pipeline without an interview. They share a TIMESTAMP and
#: differ in REASON, which is the whole point of keeping them apart:
#:
#:   rejected   they evaluated you and said no. An outcome, and it belongs in the funnel.
#:   cancelled  the posting is gone — req pulled, hiring freeze, filled internally. Nothing to
#:              do with you, and counting it as a rejection makes the rejection rate a lie.
#:   ghost      the opening was never real. An evergreen req collecting résumés, a role reposted
#:              every few weeks, a listing kept up to look like the company is growing. Nobody
#:              read it and nobody was ever going to.
#:
#: `ghost` is deliberately its own state rather than a flavour of `cancelled`. Cancelled means
#: something real STOPPED, and the honest read of it is bad luck. A ghost job never started, and
#: what it measures is the SOURCE — a board or a company worth avoiding next time. Folding them
#: together loses the only lesson either one carries.
#:
#: Neither counts as a rejection, and that is not the same as neither mattering: a rejection rate
#: computed over jobs nobody ever intended to fill describes the market, not the application.
#:
#: `rejected_at` carries all three because the column means "when this left the pipeline", and
#: every consumer that reads it — the ORDER BY that sinks closed rows, the temperature band that
#: refuses to rate them — wants exactly that. `apply_status` carries the why. Renaming the column
#: would cost a migration to say something the pair already says (the `job_url` → `anchor`
#: rename is deferred for the same reason).
CLOSED_STATUSES = ("rejected", "cancelled", "ghost")

#: The same tuple as a SQL literal list, so the ORDER BY that sinks closed rows cannot fall
#: behind the states themselves. Values are internal constants, never operator input.
_CLOSED_SQL = ", ".join(f"'{s}'" for s in CLOSED_STATUSES)


def mark_rejected(url: str, conn: sqlite3.Connection | None = None,
                  status: str = "rejected") -> str:
    """Close a job: `rejected` (they said no) or `cancelled` (the posting went away)."""
    if status not in CLOSED_STATUSES:
        raise ValueError(f"unknown closed status {status!r}; expected one of {CLOSED_STATUSES}")
    conn = _c(conn)
    now = _now()
    conn.execute("UPDATE jobs SET apply_status = ?, rejected_at = ? WHERE url = ?",
                 (status, now, url))
    conn.commit()
    return now


def set_description(url: str, text: str, conn: sqlite3.Connection | None = None) -> None:
    """Store a description the operator pasted, and clear the scrape error.

    `COALESCE` on detail_scraped_at keeps the original scrape time when there was one: the page
    really was visited, and overwriting it would claim the paste was a scrape.
    """
    conn = _c(conn)
    conn.execute(
        "UPDATE jobs SET full_description = ?, detail_error = NULL, "
        "detail_scraped_at = COALESCE(detail_scraped_at, ?) WHERE url = ?",
        (text, _now(), url))
    conn.commit()


#: What the operator may type per row, and how much of it reaches a prompt (CTX-2). Capped at
#: the WRITE, like every other free-text field in this schema. An ask that runs long is a second
#: ask, and a second ask in one email gets neither answered.
CONTEXT_MAX = 1200
ASK_MAX = 200


def set_context(url: str, context: str | None = None, ask: str | None = None,
                conn: sqlite3.Connection | None = None) -> dict:
    """Store what the operator knows about this job. Returns what was actually written.

    `None` means "this caller did not show that field", which is not the same as the operator
    clearing it — the panel renders both boxes today, but a future caller showing one would
    otherwise silently blank the other. That is the bug `_save_draft` already carries a comment
    about: the channel tabs each render half a form, and defaulting a missing key to "" had the
    LinkedIn tab blanking the outreach email.
    """
    conn = _c(conn)
    sets, args = [], []
    if context is not None:
        sets.append("job_context = ?")
        args.append(context.strip()[:CONTEXT_MAX])
    if ask is not None:
        sets.append("job_ask = ?")
        args.append(ask.strip()[:ASK_MAX])
    if not sets:
        return {}
    args.append(url)
    conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE url = ?", args)
    conn.commit()
    row = conn.execute("SELECT job_context, job_ask FROM jobs WHERE url = ?", (url,)).fetchone()
    return {"context": (row["job_context"] or ""), "ask": (row["job_ask"] or "")} if row else {}


#: The fields the operator may edit on a row, and the LENGTH each is capped at.
#:
#: A whitelist rather than "everything not on a blocklist", because the dangerous columns are
#: dangerous in ways that are silent:
#:
#:   url            the ANCHOR. `store.contact_id()` hashes it, so changing it orphans every
#:                  contact, ladder and message on the row and nothing raises.
#:   space_id       moving a row between Spaces is a different feature with its own questions.
#:   fit_score      the model's judgement. Editable, it stops being a signal and starts being
#:                  a note — and `queue_for_*` and the score filter both read it as a signal.
#:   applied_at     the state machine. `apply_status`, `rejected_at` and `interview_at` are
#:   apply_status   driven by the row menu, which also writes the events that explain them.
#:
#: `company` IS editable, and that is the subtle one. On a targets card the anchor was BUILT from
#: the name (`target:<space>:<slug>`), so this changes what the row is CALLED and deliberately
#: not what it is keyed on. The two can disagree afterwards, and that is the correct trade: the
#: alternative is re-keying, which is the orphaning failure above.
EDITABLE_FIELDS = {
    "title": 200,
    "company": 120,
    "location": 120,
    "salary": 80,
    "full_description": 20000,
}


def set_fields(url: str, fields: dict, conn: sqlite3.Connection | None = None) -> dict:
    """Edit the descriptive fields on a row. Returns {field: stored value} for what changed.

    `None` means "this caller did not show that field"; `""` means the operator CLEARED it. Those
    are different intents and conflating them is §Lessons 75, which shipped as a one-directional
    guard whose untested half dropped the operator's typed paragraph.

    An unknown key RAISES rather than being ignored. A silently dropped field is an edit the
    operator watched succeed and which never happened — the worst outcome available here, and
    the reason this is a whitelist with a loud edge rather than a filter.
    """
    unknown = sorted(set(fields) - set(EDITABLE_FIELDS))
    if unknown:
        raise ValueError(
            f"not editable: {', '.join(unknown)}. Editable fields are "
            f"{', '.join(sorted(EDITABLE_FIELDS))}.")

    sets, args, wrote = [], [], {}
    for name, cap in EDITABLE_FIELDS.items():
        value = fields.get(name)
        if value is None:
            continue
        text = str(value).strip()[:cap]
        sets.append(f"{name} = ?")
        args.append(text)
        wrote[name] = text
    if not sets:
        return {}

    c = _c(conn)
    if not c.execute("SELECT 1 FROM jobs WHERE url = ?", (url,)).fetchone():
        raise ValueError(f"no such row: {url!r}")
    # A description arriving here is TYPED, so the row is no longer owed a scrape and must not
    # keep rendering as one that failed (§Lessons 44). `COALESCE` keeps an original scrape time
    # when there was one: the page really was visited, and overwriting it would claim the typing
    # was a fetch.
    if "full_description" in wrote:
        sets.append("detail_error = NULL")
        sets.append("detail_scraped_at = COALESCE(detail_scraped_at, ?)")
        args.append(_now())
    args.append(url)
    c.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE url = ?", args)
    c.commit()
    return wrote


def mark_interview(url: str, conn: sqlite3.Connection | None = None) -> str:
    """Record that an interview is scheduled. Returns the timestamp.

    Deliberately does NOT touch `apply_status`. Rejection overwrites it because a rejected job
    has left the pipeline; an interview has not — you still applied, the materials still exist,
    and the status strip should still read "Applied". Interview is a fact ON TOP of the state,
    not a replacement for it, which is also what makes it undoable without having to remember
    what the status used to be.
    """
    conn = _c(conn)
    now = _now()
    conn.execute("UPDATE jobs SET interview_at = ? WHERE url = ?", (now, url))
    conn.commit()
    return now


def unmark_interview(url: str, conn: sqlite3.Connection | None = None) -> None:
    conn = _c(conn)
    conn.execute("UPDATE jobs SET interview_at = NULL WHERE url = ?", (url,))
    conn.commit()


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
                    conn: sqlite3.Connection | None = None, space_id: str = "") -> None:
    """Store a pasted job URL, in the Space the operator was looking at.

    `space_id` is NAMED here rather than left to the column DEFAULT. The default is
    'job-search', which is right for a fresh install and wrong for every paste made while
    standing in another Space — the row lands correctly in the table and then appears under a
    different tab, which is the one outcome tabs exist to prevent. Reported on a Peak6 posting
    pasted into Gauntlet; the whole point of a Space is that its outreach stays its own.

    Empty means "do not name it", so an install with no registry keeps the column default and
    every caller predating Spaces behaves exactly as before.
    """
    conn = _c(conn)
    cols = "url, title, company, site, strategy, discovered_at, application_url"
    vals = [url, title, company, site, "dashboard_upload", _now(), application_url]
    if space_id:
        cols += ", space_id"
        vals.append(space_id)
    conn.execute(f"INSERT INTO jobs ({cols}) VALUES ({','.join('?' * len(vals))})", vals)
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

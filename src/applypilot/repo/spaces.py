"""The Space and Identity registries — every query against them, as a named function.

ARCH-4's boundary: `web_dashboard.py` and the pipeline read Spaces through here, never with
SQL of their own.

A Space is a manifest, not a fork (`spaces-prd.md` §Headline 1): a row saying which
capabilities are on, which tone, which cadence, and which mailbox sends. The pipeline itself is
shared. This module owns the two questions the rest of the codebase asks of that registry —
*what Spaces are there* and *which of them are job-shaped* — and deliberately owns nothing
about what a Space DOES, which is the manifest's job.

**`space_id` is the only key that decides membership** (SPACE-1a D2). `strategy` keeps meaning
provenance — how a row arrived — and is never read here. Two partition keys over one table is
how §Lessons 49 happens.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from applypilot.database import get_connection

#: The Space every row belongs to until another one exists. This is the value sitting on 25
#: jobs and 196 contacts via the column default in `_ALL_COLUMNS` / `_CONTACT_COLUMNS`, and
#: migration 003 seeds the matching row. Changing it is a data migration, not a config edit.
DEFAULT_SPACE_ID = "job-search"

#: The shapes a Space can take (`spaces-prd.md` §5). A shape decides what a ROW is; the
#: template decides what the copy sounds like. There is no third shape.
JOBS_SHAPE = "pipeline/jobs"
TARGETS_SHAPE = "pipeline/targets"
SHAPES = (JOBS_SHAPE, TARGETS_SHAPE)


class RegistryEmpty(RuntimeError):
    """No Spaces are registered, so no row can be said to belong anywhere.

    Raised rather than returning an empty list, because the callers are the pipeline stage
    queues. An empty `IN ()` makes every one of them select nothing, and a prepare run that
    quietly does no work is byte-identical to a healthy one that had nothing to do — which is
    §Lessons 15 ("a zero result must be as loud as an error") and §Lessons 44 in a single line.

    Reachable only if migration 003 failed, which is non-fatal by design and reported by
    `applypilot migrate --status`. The message says so, because the operator needs the way out
    and not just the fact.
    """


def _c(conn: sqlite3.Connection | None) -> sqlite3.Connection:
    return conn if conn is not None else get_connection()


def _dicts(rows) -> list[dict]:
    return [dict(zip(r.keys(), r)) for r in rows]


def _registered(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='spaces'").fetchone() is not None


# ── reads ───────────────────────────────────────────────────────────────────

def all_spaces(include_archived: bool = False,
               conn: sqlite3.Connection | None = None) -> list[dict]:
    """Every Space, in nav order. `[]` when the registry has not been created yet.

    Unlike `jobs_shaped_ids()` this one tolerates a missing table: a caller asking "what Spaces
    exist" can meaningfully be told "none yet", whereas a caller asking "which rows may the
    pipeline touch" cannot.
    """
    c = _c(conn)
    if not _registered(c):
        return []
    where = "" if include_archived else "WHERE archived_at IS NULL"
    return _dicts(c.execute(
        f"SELECT * FROM spaces {where} ORDER BY position ASC, created_at ASC").fetchall())


def get_space(space_id: str, conn: sqlite3.Connection | None = None) -> dict | None:
    c = _c(conn)
    if not space_id or not _registered(c):
        return None
    row = c.execute("SELECT * FROM spaces WHERE id = ?", (space_id,)).fetchone()
    return dict(zip(row.keys(), row)) if row else None


def jobs_shaped_ids(conn: sqlite3.Connection | None = None) -> list[str]:
    """The ids of every Space whose rows are job postings. Raises when there are none.

    This is what keeps the six-stage pipeline off a targets row (SPACE-1a D3). A target lives in
    `jobs` with `strategy = 'dashboard_upload'` — the operator did add it by hand — so
    `QUEUE_SQL` alone selects it, and `queue_needing_detail` would hand `target:acme:ridgeline`
    to the scraper. §Lessons 44 says what happens next: the partial result stamps
    `detail_scraped_at`, clears `detail_error`, and the row can never be retried.

    Read from the `spaces` table rather than a `shape` column on `jobs`. A denormalised copy is
    a second source of truth that drifts the first time a Space is edited, and there is no
    render-path cost to avoiding it — this is a handful of ids, resolved once per stage run, not
    per row.
    """
    c = _c(conn)
    if not _registered(c):
        raise RegistryEmpty(
            "the `spaces` registry does not exist — migration 003 has not run. "
            "Check `applypilot migrate --status`.")
    ids = [r[0] for r in c.execute(
        "SELECT id FROM spaces WHERE shape = ? ORDER BY position ASC, created_at ASC",
        (JOBS_SHAPE,)).fetchall()]
    if not ids:
        raise RegistryEmpty(
            "no job-shaped Space is registered, so every pipeline queue would silently "
            "select nothing. Check `applypilot migrate --status`.")
    return ids


def identities(conn: sqlite3.Connection | None = None) -> list[dict]:
    c = _c(conn)
    if c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='identities'"
                 ).fetchone() is None:
        return []
    return _dicts(c.execute("SELECT * FROM identities ORDER BY created_at ASC").fetchall())


# ── writes ──────────────────────────────────────────────────────────────────

def create_space(space_id: str, name: str, template: str, shape: str,
                 identity_id: str = "personal",
                 conn: sqlite3.Connection | None = None) -> dict:
    """Register a Space. The `id` is permanent from this moment (`spaces-prd.md` §13.2).

    For a `pipeline/targets` Space the id is hashed into every `contact_id` via the anchor
    `target:<space_id>:<slug>`, so renaming it later would re-key every contact in the Space and
    detach its touches, sequences and messages — the exact failure SPACE-1a is written to avoid.
    `rename()` moves the display name and refuses the id.
    """
    if shape not in SHAPES:
        raise ValueError(f"unknown shape {shape!r}; expected one of {SHAPES}")
    c = _c(conn)
    now = datetime.now(timezone.utc).isoformat()
    position = (c.execute("SELECT COALESCE(MAX(position), -1) FROM spaces").fetchone()[0] or 0) + 1
    c.execute(
        "INSERT INTO spaces (id, name, template, shape, identity_id, position, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (space_id, name, template, shape, identity_id, position, now))
    c.commit()
    return get_space(space_id, c) or {}


def rename(space_id: str, name: str, conn: sqlite3.Connection | None = None) -> None:
    """Change what a Space is CALLED. There is deliberately no way to change what it is keyed on."""
    c = _c(conn)
    c.execute("UPDATE spaces SET name = ? WHERE id = ?", (name, space_id))
    c.commit()

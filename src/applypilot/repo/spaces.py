"""The Space and Identity registries — every query against them, as a named function.

ARCH-4's boundary: `web_dashboard.py` and the pipeline read Spaces through here, never with
SQL of their own. What a Space MEANS lives in `domain/space.py` and is pure; this module only
stores and retrieves it.

The split is load-bearing for the same reason ARCH-1's was: `domain/space.py` can be imported
and driven without a database, which is what lets the falsifier test define a Space that exists
nowhere in this codebase and run it end to end.

**`space_id` is the only key that decides membership** (SPACE-1a D2). Provenance — how a row
arrived — is a different question with a different column, and this module answers neither with
the other. Two partition keys over one table is how §Lessons 49 happens.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from applypilot.database import get_connection
from applypilot.domain.space import JOBS_SHAPE, SHAPES, TARGETS_SHAPE, Space, from_template

__all__ = ["DEFAULT_SPACE_ID", "JOBS_SHAPE", "TARGETS_SHAPE", "SHAPES", "RegistryEmpty",
           "all_spaces", "get_space", "load", "load_all", "jobs_shaped_ids",
           "document_making_ids", "identities", "create_space", "save", "rename"]

#: The Space every row belongs to until another one exists. This is the value sitting on 25
#: jobs and 196 contacts via the column default in `_ALL_COLUMNS` / `_CONTACT_COLUMNS`, and
#: migration 003 seeds the matching row. Changing it is a data migration, not a config edit.
DEFAULT_SPACE_ID = "job-search"


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
    """Every Space as a raw row, in nav order. `[]` when the registry does not exist yet.

    Unlike `jobs_shaped_ids()` this tolerates a missing table: a caller asking "what Spaces
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


def load(space_id: str, conn: sqlite3.Connection | None = None) -> Space | None:
    """One Space as its MANIFEST — columns plus the `config` blob, validated."""
    row = get_space(space_id, conn)
    return Space.from_row(row) if row else None


def load_all(include_archived: bool = False,
             conn: sqlite3.Connection | None = None) -> list[Space]:
    return [Space.from_row(r) for r in all_spaces(include_archived, conn)]


def _ids_where(predicate, why: str, conn: sqlite3.Connection | None = None) -> list[str]:
    """Space ids whose manifest satisfies `predicate`. Raises when there are none.

    The filter runs in PYTHON, over loaded manifests, because most of a manifest lives in the
    `config` JSON blob and querying inside it would be both slow and a second parser for the
    same data. This is a handful of rows resolved once per stage run, never per job — it is not
    on the render path, and `/api/status` does not call it (§Lessons 26: the hot path is hot for
    everything, so keep new work off it deliberately rather than by accident).
    """
    c = _c(conn)
    if not _registered(c):
        raise RegistryEmpty(
            "the `spaces` registry does not exist — migration 003 has not run. "
            "Check `applypilot migrate --status`.")
    ids = [s.id for s in load_all(conn=c) if predicate(s)]
    if not ids:
        raise RegistryEmpty(
            f"no Space {why}, so every pipeline queue would silently select nothing. "
            f"Check `applypilot migrate --status`.")
    return ids


def jobs_shaped_ids(conn: sqlite3.Connection | None = None) -> list[str]:
    """The ids of every Space whose rows are job postings.

    This is what keeps the six-stage pipeline off a target row (SPACE-1a D3). A target lives in
    `jobs` with `strategy = 'dashboard_upload'` — the operator did paste it in, and a private
    strategy value would make it invisible to `delete_job`, which is
    `DELETE ... AND {QUEUE_SQL}`. So `QUEUE_SQL` alone selects it and `queue_needing_detail`
    would hand `target:acme:ridgeline` to the scraper. §Lessons 44 says what happens next: the
    partial result stamps `detail_scraped_at`, clears `detail_error`, and the row can never be
    retried.

    Read from the registry rather than a `shape` column on `jobs`. A denormalised copy is a
    second source of truth that drifts the first time a Space is edited.
    """
    return _ids_where(lambda s: s.runs_job_pipeline, "runs the job pipeline", conn)


def document_making_ids(conn: sqlite3.Connection | None = None) -> list[str]:
    """Spaces whose rows get a tailored résumé and a cover letter.

    Narrower than `jobs_shaped_ids` and deliberately not the same call: a jobs-shaped Space may
    turn document generation off, and a targets Space never had it. Collapsing the two would
    make the first case unexpressible.
    """
    return _ids_where(lambda s: s.makes_documents, "generates documents", conn)


def identities(conn: sqlite3.Connection | None = None) -> list[dict]:
    c = _c(conn)
    if c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='identities'"
                 ).fetchone() is None:
        return []
    return _dicts(c.execute("SELECT * FROM identities ORDER BY created_at ASC").fetchall())


# ── writes ──────────────────────────────────────────────────────────────────

def create_space(space_id: str, name: str, template: str, shape: str | None = None,
                 identity_id: str = "personal",
                 conn: sqlite3.Connection | None = None, **overrides) -> Space:
    """Register a Space from its template. The `id` is permanent from this moment (§13.2).

    For a `pipeline/targets` Space the id is hashed into every `contact_id` via the anchor
    `target:<space_id>:<slug>`, so renaming it later would re-key every contact in the Space and
    detach its touches, sequences and messages — the exact failure SPACE-1a is written to avoid.
    `rename()` moves the display name and there is deliberately no way to move the id.
    """
    if shape is not None:
        overrides["shape"] = shape
    space = from_template(space_id, name, template, identity_id=identity_id, **overrides)
    c = _c(conn)
    now = datetime.now(timezone.utc).isoformat()
    position = (c.execute("SELECT COALESCE(MAX(position), -1) FROM spaces").fetchone()[0] or 0) + 1
    c.execute(
        "INSERT INTO spaces (id, name, template, shape, identity_id, config, position, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (space.id, space.name, space.template, space.shape, space.identity_id,
         space.config_json(), position, now))
    c.commit()
    return space


def save(space: Space, conn: sqlite3.Connection | None = None) -> Space:
    """Persist an edited manifest.

    `id` and `shape` are not in the UPDATE at all. `Space.with_()` already refuses to change
    them, and leaving them out here means a caller that builds a manifest by hand cannot move
    them either — the guarantee holds at both ends rather than at whichever one is remembered
    (§Lessons 49: a rule implemented at one of its call sites is not implemented).
    """
    c = _c(conn)
    c.execute("UPDATE spaces SET name=?, template=?, identity_id=?, config=? WHERE id=?",
              (space.name, space.template, space.identity_id, space.config_json(), space.id))
    c.commit()
    return space


def rename(space_id: str, name: str, conn: sqlite3.Connection | None = None) -> None:
    """Change what a Space is CALLED. There is deliberately no way to change what it is keyed on."""
    c = _c(conn)
    c.execute("UPDATE spaces SET name = ? WHERE id = ?", (name, space_id))
    c.commit()

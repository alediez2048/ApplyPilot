"""003 — the `spaces` and `identities` registries, seeded so nothing can read an empty one.

SPACE-1a. `spaces-prd.md` §6 put three changes in this migration; two of them belong in the
additive column dicts and are there instead:

    _ALL_COLUMNS["space_id"]      = "TEXT NOT NULL DEFAULT 'job-search'"   (database.py)
    _CONTACT_COLUMNS["space_id"]  = "TEXT NOT NULL DEFAULT 'job-search'"   (store.py)

That is not tidying. The dicts and a numbered migration are two sources of truth for one
schema, and they RACE: `get_connection()` does not call `init_db`, so `ensure_contacts_columns`
(`store.py`) can run before the migration runner does, and which happens first depends on the
entry point. As long as no migration touches a column the dicts declare, that race cannot fire.
It is also why the column DEFAULT does the backfill rather than an `UPDATE` in here — a default
is applied by the ALTER itself and cannot miss a row inserted while a backfill is running.

So this migration is left with the one thing the dicts genuinely cannot express: creating two
tables and putting a row in each.

**Seeding is the load-bearing part, not the tables.** `repo/spaces.jobs_shaped_ids()` gates the
five pipeline stage queues, and an empty registry would make every one of them return zero rows
— a completed run byte-identical to a dead pipeline (§Lessons 15, §Lessons 44). That function
raises rather than returning nothing, and this migration is what guarantees it never has to.

Idempotent, checked by running it three times: `CREATE TABLE IF NOT EXISTS` plus
`INSERT OR IGNORE` on a known primary key. Re-running it after the operator has renamed their
Space must not rename it back, which is why the seed is OR IGNORE and never OR REPLACE.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

NOTE = "spaces + identities registries created and seeded with the current job search"

#: The Space every existing row already belongs to, via the column default. Changing this
#: string is a data migration, not a config edit — it is the `space_id` sitting on 25 jobs and
#: 196 contacts, and for a targets Space it would be inside every `contact_id` (§13.2).
DEFAULT_SPACE_ID = "job-search"
DEFAULT_IDENTITY_ID = "personal"

#: Schema AS OF VERSION 3, duplicated from `repo/spaces.py` on purpose. A migration that
#: imports today's column list rebuilds the table with today's columns and silently changes
#: what version 3 meant — the convention m002 established.
_IDENTITIES = """
CREATE TABLE IF NOT EXISTS identities (
    id                   TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    token_path           TEXT,
    from_name            TEXT,
    from_address         TEXT,
    signature_html       TEXT,
    deck_base_url        TEXT,
    deck_collector_url   TEXT,
    deck_collector_token TEXT,
    daily_limit          INTEGER,
    created_at           TEXT
)
"""

_SPACES = """
CREATE TABLE IF NOT EXISTS spaces (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    template    TEXT NOT NULL,
    shape       TEXT NOT NULL,
    identity_id TEXT NOT NULL DEFAULT 'personal',
    config      TEXT,
    position    INTEGER,
    archived_at TEXT,
    created_at  TEXT
)
"""


def up(conn: sqlite3.Connection) -> dict:
    conn.execute(_IDENTITIES)
    conn.execute(_SPACES)

    now = datetime.now(timezone.utc).isoformat()

    # Every field but the name is EMPTY, and that is the seed's meaning: "resolve this from the
    # globals that are in force today" — TOKEN_PATH, OUTREACH_FROM_*, INTRO_DECK_URL. Copying
    # their current values in here would freeze whatever happened to be set at migration time
    # into a row nothing reads yet, and ID-1 would then be unable to tell a deliberate override
    # from a snapshot. Behaviour after this migration is byte-identical to before it.
    conn.execute(
        "INSERT OR IGNORE INTO identities (id, name, created_at) VALUES (?, ?, ?)",
        (DEFAULT_IDENTITY_ID, "Personal", now))

    conn.execute(
        "INSERT OR IGNORE INTO spaces "
        "(id, name, template, shape, identity_id, position, created_at) "
        "VALUES (?, ?, 'jobs', 'pipeline/jobs', ?, 0, ?)",
        (DEFAULT_SPACE_ID, "Job Search", DEFAULT_IDENTITY_ID, now))

    conn.commit()

    spaces = conn.execute("SELECT COUNT(*) FROM spaces").fetchone()[0]
    identities = conn.execute("SELECT COUNT(*) FROM identities").fetchone()[0]
    return {"spaces": spaces, "identities": identities, "note": NOTE}

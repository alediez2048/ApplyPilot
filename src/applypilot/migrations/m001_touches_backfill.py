"""001 — follow-up state: ten `contacts` columns → `touches` / `sequences`.

This is the ARCH-3 migration, re-expressed as a numbered migration rather than a CLI command
you had to know to run. It is the deliberate choice of a first migration: it already ran
successfully against the real database on 2026-07-29, so the framework is being proved on a
case whose correct outcome is known rather than on a toy.

What it does on each kind of database:

  - **a database that already migrated** (this machine): the legacy columns are gone, `plan()`
    finds nothing, and nothing is written. It records version 1 and moves on.
  - **an old database** (a backup, another machine): backfills every ladder, verifies the new
    tables re-derive the old columns exactly, and only then drops them.
  - **a fresh database**: the legacy columns never existed. No-op, so a fresh DB and a fully
    migrated old DB converge on the same schema — which is one of ARCH-5's criteria.

**Idempotent**, as every migration here must be: touch ids are derived from
(contact_id, channel, seq), so re-running upserts the same rows rather than duplicating them.

It refuses to drop the legacy columns unless `verify()` is clean. A failed verify leaves both
representations in place and raises — the data is still there and still readable by the old
code path, which is the only safe direction for an irreversible change.
"""

from __future__ import annotations

import sqlite3

NOTE = "follow-up state moved to touches/sequences"


def up(conn: sqlite3.Connection) -> None:
    from applypilot.networking import backfill_touches as B
    from applypilot.networking import store, touches

    store.init_contacts(conn)
    touches.init_touches(conn)

    existing = {r[1] for r in conn.execute("PRAGMA table_info(contacts)")}
    legacy_present = [c for c in B.LEGACY_COLUMNS if c in existing]
    if not legacy_present:
        return                      # already migrated, or a fresh database

    B.apply(conn)

    problems = B.verify(conn)
    if problems:
        # Do NOT drop. Both representations survive and the old columns still hold the
        # truth, which is the only recoverable state for a one-way change.
        raise RuntimeError(
            f"backfill did not round-trip ({len(problems)} mismatch(es)); legacy columns kept. "
            f"First: {problems[0]}"
        )

    B.drop_legacy_columns(conn)

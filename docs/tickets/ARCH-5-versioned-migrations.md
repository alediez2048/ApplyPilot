# ARCH-5 — Versioned migrations

**Phase:** 4 · **Size:** S (~0.5d) · **Depends on:** ARCH-3 · **Status:** Todo
**PRD:** `architecture-prd.md` §Q4, ARCH-5

## Problem

Migrations are forward-only: add a key to `_ALL_COLUMNS` / `_CONTACT_COLUMNS` and the column
appears. Genuinely elegant, and it absorbed ~15 schema changes in one session without friction.

But there is **no rename, drop, backfill, version, or down-migration.** Both real data changes
so far needed hand-written one-off scripts: reducing `preserved_companies` after the base
résumé changed, and re-rendering the four cover-letter PDFs that still carried a stale `.edu`
address. At 28 contacts you can always fix it by hand. That stops being true at CRM scale.

## Scope / tasks

- [ ] `schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT, name TEXT)`
- [ ] `migrations/NNN_description.py`, each exposing `up(conn)`; applied in order, recorded once
- [ ] Run pending migrations at startup, after the additive column pass
- [ ] **Keep the column dicts** for additive changes — they work; add real migrations only for
      rename / drop / backfill / data fixes
- [ ] Express the ARCH-3 `touches` backfill as migration `001`, proving the framework on a real case
- [ ] `applypilot doctor` prints the schema version; `applypilot migrate --status` lists pending

## Acceptance criteria

- [ ] Version recorded and asserted at startup
- [ ] A migration runs exactly once, even across concurrent dashboard + CLI starts
- [ ] The ARCH-3 backfill is a numbered migration, not a script
- [ ] A fresh DB and a fully migrated old DB end up with identical schemas (diff them)
- [ ] Failure mid-migration leaves the DB usable and says which version failed

## Risks / notes

- SQLite has limited `ALTER TABLE`: renames and drops mean create-copy-swap. Wrap in a
  transaction and back up first.
- Do not build down-migrations. Single-user local app; a backup is the rollback.

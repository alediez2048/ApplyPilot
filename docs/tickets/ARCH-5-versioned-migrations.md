# ARCH-5 — Versioned migrations

**Phase:** 4 · **Size:** S (~0.5d) · **Depends on:** ARCH-3 · **Status:** ✅ Done (2026-07-29)
**PRD:** `architecture-prd.md` §Q4, ARCH-5

## Problem

Migrations are forward-only: add a key to `_ALL_COLUMNS` / `_CONTACT_COLUMNS` and the column
appears. Genuinely elegant, and it absorbed ~15 schema changes in one session without friction.

But there is **no rename, drop, backfill, version, or down-migration.** Both real data changes
so far needed hand-written one-off scripts: reducing `preserved_companies` after the base
résumé changed, and re-rendering the four cover-letter PDFs that still carried a stale `.edu`
address. At 28 contacts you can always fix it by hand. That stops being true at CRM scale.

## Scope / tasks

- [x] `schema_migrations(version, name, status, applied_at, claimed_at, error)` — more columns
      than sketched, because "which version failed and why" and the concurrency lease both
      need somewhere to live
- [x] `migrations/mNNN_description.py`, each exposing `up(conn)`; applied in order, recorded once
- [x] Run pending migrations at startup, after the additive column pass
- [x] **Kept the column dicts** for additive changes — untouched
- [x] ARCH-3 `touches` backfill is migration `001`
- [x] `applypilot doctor` prints the schema version; `applypilot migrate --status` lists pending

## Acceptance criteria

- [x] Version recorded and asserted at startup
- [x] A migration runs exactly once, even across concurrent dashboard + CLI starts —
      **this did not work on the first attempt; see below**
- [x] The ARCH-3 backfill is a numbered migration, not a script
- [x] A fresh DB and a fully migrated old DB end up with identical schemas — verified in a
      test AND against the real pre-ARCH-3 backup
- [x] Failure mid-migration leaves the DB usable and says which version failed

## Risks / notes

- SQLite has limited `ALTER TABLE`: renames and drops mean create-copy-swap. Wrap in a
  transaction and back up first.
- Do not build down-migrations. Single-user local app; a backup is the rollback.

## The concurrency bug this ticket nearly shipped

The first `_claim()` skipped only `done` rows and reclaimed anything else, so an interrupted
run could be retried. That is a real requirement — this app gets killed mid-operation
routinely (two applies died that way on 2026-07-29). But it collapsed two different states
into one branch, and `test_concurrent_starts_do_not_double_apply` **ran the migration 6 times
out of 6**.

"Retry after a crash" and "don't double-apply" are only compatible if you can tell a live
`running` claim from a dead one. Hence `claimed_at` and a 300s lease:

```
done     -> skip
failed   -> reclaim now      (the process finished and reported failure)
running  -> reclaim only if the lease expired
```

Mutation-tested: removing the lease check fails two tests.

## Verified against real data

Migration 001 was run against the actual pre-ARCH-3 backup
(`applypilot-2026-07-29-pre-arch3.db`, still 42 columns):

```
before: contacts 42 cols, 10 ladder columns, no touches table
after:  contacts 32 cols, 0 ladder columns, 7 touches + 1 sequence, version 1
```

Identical to what the hand-run ARCH-3 migration produced on the live database. Column SETS
for every table then compared across migrated-old / live / fresh: identical.

Two apparent divergences turned out to be artifacts worth recording, because the next person
will hit them: column ORDER differs (ALTER TABLE appends, so an upgraded DB lists the same
columns in a different order and its stored CREATE sql is historic — compare sets, not text),
and `connections` is absent from a fresh DB because it is created lazily on first use, not by
a migration.

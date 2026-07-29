# ARCH-4 — Repository boundary

**Phase:** 4 · **Size:** L (~1–2d) · **Depends on:** ARCH-1 · **Status:** ✅ Done (2026-07-29) — scope narrowed, see below
**PRD:** `architecture-prd.md` ARCH-4

## Problem

**17 of ~30 modules execute SQL directly.** `web_dashboard.py` contains 40 `conn.execute`
calls — the same count as `store.py`, whose entire job is SQL. Scrapers, scoring stages, the
apply launcher and the web layer all reach into the database themselves.

Consequences: domain logic cannot be tested without a database, a schema change means grepping
17 files, and there is no single place to add caching, logging, or a transaction boundary.

## Scope / tasks

- [x] `src/applypilot/repo/jobs.py` — plain functions, dicts out, **not an ORM** (8 deps stay 8)
- [x] Move the dashboard queries into it — 39 statements, all of them
- [x] Leave `database.py` owning connections, WAL, migrations — `repo/` sits on top
- [x] **`repo/contacts.py`, `repo/events.py`, `repo/touches.py` deliberately NOT created.**
      `store.py`, `database.log_event`/`get_job_events`, and `touches.py` already are
      repositories for those tables. Building a second abstraction over each is the exact
      failure this ticket's own risk note warns about. The four contacts readers the
      dashboard needed were added to `store.py` instead.
- [ ] ~~Migrate the pipeline stages one module at a time~~ — **deferred, see Narrowed scope**

## Acceptance criteria

- [x] `web_dashboard.py` contains **zero** SQL (from 39) — pinned by
      `test_web_dashboard_runs_no_sql_at_all`, mutation-tested
- [x] `domain/` tests need no database at all
- [x] Query count per `/api/status` measured and guarded — **313 → 50**, plus an explicit
      N+1 test that fails if a per-contact lookup is added
- [x] Tests green after each migrated module, not just at the end (311 → 324 → 325)
- [ ] ~~`conn.execute` appears only in `repo/`, `database.py`, `networking/store.py`~~ —
      **not met, deliberately.** 14 modules still hold SQL. They are named in an allowlist
      in `test_sql_lives_only_in_the_data_layer`, so the list can only shrink and a NEW
      module cannot quietly join it.

## Narrowed scope (Jorge, 2026-07-29)

The remaining 14 modules — `enrichment/detail.py` (16 statements), `apply/launcher.py` (15),
`view.py` (7), the discovery and scoring stages — are mechanical edits to code that is not
changing and has not caused a problem. The pain this ticket exists to fix ("domain logic
cannot be tested without a database", "no single place to add caching") was concentrated in
the dashboard, which is the surface actually being worked on. That part is done.

The counter-argument, recorded so it is not forgotten: "17 files to grep on a schema change"
is only partly solved, and ARCH-5 would be simpler if everything already went through one
layer. Judged not worth the blast radius today; the allowlist makes finishing it cheap later.

## Risks / notes

- **Widest-blast-radius ticket here.** Do it strictly one module at a time, each its own commit.
- Real risk of N+1: today's dashboard does a few big queries; a naive repository turns those
  into per-row lookups. Assert the query count in a test.
- `store.py` predates this and already *is* a repository for `contacts` — fold it in or leave
  it, but do not create a second competing abstraction over the same table.

## What measuring first actually found

The ticket asked for the query count to be "measured and not worse than today". Measuring
it before touching anything turned out to be the whole value of the ticket:

```
313 statements per /api/status — and it fires every 2.5 seconds
  199  CREATE TABLE IF NOT EXISTS / PRAGMA, re-run on EVERY request
        (init_connections alone: 108 — it was called once per contact)
   36  SELECT connections — count_at_company full-scanned 899 rows PER JOB
```

Nothing failed. Nothing was slow enough to notice. Idempotent-at-the-SQL-level had made it
look free to call `init_*` from every read path. Now 50.

Two bugs the work surfaced that no test would have:

- **`_mark_submitted` broke during the extraction and the suite stayed green.** Its row
  fetch was replaced with `exists()` while the next line still read `row["apply_status"]`.
  Ruff caught it as an undefined name. That guard is the only thing stopping "Mark
  submitted ✓" from recording an application that was never filled — and it had no coverage.
- **The first query-budget tests were vacuous.** The seed omitted `strategy`, which
  /api/status filters on, so the payload came back empty and every assertion measured
  nothing; reverting the batching left them all green. Only mutation testing exposed it.

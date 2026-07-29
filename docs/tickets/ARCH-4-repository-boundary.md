# ARCH-4 — Repository boundary

**Phase:** 4 · **Size:** L (~1–2d) · **Depends on:** ARCH-1 · **Status:** Todo
**PRD:** `architecture-prd.md` ARCH-4

## Problem

**17 of ~30 modules execute SQL directly.** `web_dashboard.py` contains 40 `conn.execute`
calls — the same count as `store.py`, whose entire job is SQL. Scrapers, scoring stages, the
apply launcher and the web layer all reach into the database themselves.

Consequences: domain logic cannot be tested without a database, a schema change means grepping
17 files, and there is no single place to add caching, logging, or a transaction boundary.

## Scope / tasks

- [ ] `src/applypilot/repo/` — `jobs.py`, `contacts.py`, `events.py`, `touches.py`
- [ ] Plain functions returning dicts: `get_job(url)`, `jobs_by_stage(stage)`,
      `contacts_for_job(url)`, `due_touches(now)`. **Not an ORM** — 8 dependencies stay 8.
- [ ] Move the 40 dashboard queries into `repo/`; ARCH-1 should already have removed most of
      the *reason* the dashboard needed them
- [ ] Migrate the pipeline stages (`scoring/`, `enrichment/`, `discovery/`) one module at a time
- [ ] Leave `database.py` owning connections, WAL, and migrations — `repo/` sits on top

## Acceptance criteria

- [ ] `conn.execute` appears only in `repo/`, `database.py`, and `networking/store.py`
- [ ] `web_dashboard.py` contains **zero** SQL (from 40)
- [ ] `domain/` tests need no database at all
- [ ] Query count per `/api/status` is measured and not worse than today (watch for N+1)
- [ ] 228 tests green after each migrated module, not just at the end

## Risks / notes

- **Widest-blast-radius ticket here.** Do it strictly one module at a time, each its own commit.
- Real risk of N+1: today's dashboard does a few big queries; a naive repository turns those
  into per-row lookups. Assert the query count in a test.
- `store.py` predates this and already *is* a repository for `contacts` — fold it in or leave
  it, but do not create a second competing abstraction over the same table.

# SPACE-1a — Where a target row lives, and which key partitions it

**Status:** ✅ decided 2026-08-04 · **Branch:** `spaces` · **Blocks:** SPACE-1, SPACE-2, SPACE-3
**Prerequisite for:** every estimate in `spaces-prd.md` §8

`spaces-prd.md` §5 defines `pipeline/targets` as "a row is a company you want to work with"
and §6 gives two new tables plus `ALTER TABLE jobs ADD COLUMN space_id`. **There is no
`targets` table in §6**, so by elimination a target is a row in `jobs` — and the document never
says so, never names its primary key, and never says what happens to the twenty job-shaped
columns it will never use.

That gap is worth a factor of three on SPACE-3, so it is settled here before anything is built.

---

## D1 — A target row lives in `jobs`, keyed `target:<space_id>:<slug>`

`jobs.url` is `TEXT PRIMARY KEY` (`database.py:137`). A target has no URL, so its key is the
same string §5 already assigns to the contact anchor. One string, one row, one anchor.

**Measured, not assumed:** almost nothing downstream actually reads the `jobs` table. The
anchor travels as an opaque string through `log_event` / `get_job_events` (`database.py:216`,
`:238`), `job_checklist`, `temperature`, `lastinteraction`, `job_followups`, `metrics` and
`bookings`. Only **three** call sites SELECT from `jobs` given an anchor: `dashboard_rows`,
`job_attachments` (`gmail_send.py:214`) and `messages.threads_for_job`.

| | Rows in `jobs` | A separate `targets` table |
|---|---|---|
| String-keyed consumers | work unchanged | every one needs a second branch |
| `job_attachments`, `threads_for_job`, `dashboard_rows` | need a shape branch (3 sites) | need a shape branch (3 sites) |
| Dead columns | 20 of 34, permanently NULL | none |
| Cost | ~1 day | ~4 days |

The dead columns are the honest cost of this decision and they are the cheaper half. Twenty
NULL columns are visible and harmless; twenty duplicated join paths are neither.

**Not renaming the `jobs` table.** `url` is its primary key and is referenced by name at 40+
sites. A table rename buys a better noun and risks a migration, which is the trade this ticket
exists to refuse.

## D2 — `space_id` is the only key that decides which panel a row appears in

`QUEUE_SQL = "strategy IN ('dashboard_upload', 'manual_url_batch')"` (`repo/jobs.py:21`)
already partitions `jobs`, at **13 call sites**. SPACE-2 adds `space_id` over the same table.
Two partition keys over one table is how §Lessons 49 happens — a rule implemented at one of
its call sites is not implemented, and last time that cost 8 unsearched Google connections.

So the two keys get **disjoint jobs**, written down:

- **`strategy` means provenance** — how this row arrived. `dashboard_upload`, `manual_url_batch`,
  and whatever DISC-1 gives discovery. It is *never* consulted to decide which Space a row is in.
- **`space_id` means membership.** It alone answers "does this row belong in this panel".

`QUEUE_SQL` keeps its current meaning ("the operator pasted this in, as opposed to discovery
finding it") and gains a space filter alongside it. It does not absorb the new job.

**A target row therefore keeps `strategy = 'dashboard_upload'`** when the operator types it in.
That looks wrong and is not: the operator did add it by hand. Giving targets a private strategy
value would make them invisible to `delete_job`, which is `DELETE FROM jobs WHERE url = ? AND
{QUEUE_SQL}` (`repo/jobs.py:413`) — a target you could create and never remove.

## D3 — The pipeline stages gate on SHAPE, and an empty registry fails loud

D2 leaves one real hazard: a target row with `strategy = 'dashboard_upload'` and a NULL
`detail_scraped_at` is exactly what `queue_needing_detail` (`repo/jobs.py:104`) selects. The
enrich stage would try to HTTP-fetch `target:acme:ridgeline`, and §Lessons 44 says what happens
next — a partial result stamps `detail_scraped_at`, sets `detail_error` to NULL, and the row can
never be retried.

The five stage queues (`queue_needing_detail`, `queue_needing_score` ×2, `mark_unscorable`,
`prepare_status`) gate on the row's Space having `shape = 'pipeline/jobs'`.

**Resolved from the `spaces` table, not from a column on the row.** A denormalised `shape` on
`jobs` is a second source of truth that drifts the first time a Space is edited.

**An empty registry must raise, not return zero.** `WHERE space_id IN (SELECT id FROM spaces
WHERE shape='pipeline/jobs')` against an unseeded `spaces` returns no rows, and every stage
reports "nothing to do" — a completed run byte-identical to a dead pipeline, which is
§Lessons 15 and §Lessons 44 in one line. `repo/spaces.jobs_shaped_ids()` raises when the
registry is empty; the migration seeds it before anything can ask.

## D4 — `space_id` ships in the additive dicts; the migration only does what they cannot

`spaces-prd.md` §6 puts all three schema changes in migration 003. Two of them do not belong
there. The additive column dicts absorbed ~15 schema changes without friction and express a
default fine (`_ALL_COLUMNS` already holds `"INTEGER DEFAULT 0"`):

```python
"space_id": "TEXT NOT NULL DEFAULT 'job-search'"     # _ALL_COLUMNS and _CONTACT_COLUMNS
```

That backfills all 25 jobs and all 196 contacts as a property of the ALTER, with no UPDATE
statement, no ordering dependency on the migration, and nothing to re-run.

Migration 003 is left with the one thing the dicts cannot express: **creating `spaces` and
`identities` and seeding the default rows.**

This also removes the collision the grill found. The dicts and a numbered migration are two
sources of truth for one schema, and `ensure_contacts_columns` (`store.py:128`) can run
*before* the migration runner — `get_connection()` does not call `init_db`, so which runs first
depends on the entry point. As long as no migration touches a column the dicts declare, that
race cannot fire.

## D5 — The `job_url` → `anchor` rename is separated out, and should be deferred

**Recommendation: do not rename in this phase.** Split to **SPACE-1b**, decide on its own merits.

The rename is the only step in the whole PRD that can destroy data, and it buys nothing
functional. Every goal in the document — targets, spaces, identities, per-space cadence — works
with the column named `job_url`, because `contact_id()` (`store.py:104`) hashes the *value* and
has never cared what the column is called.

What it actually costs, measured:

| | |
|---|---|
| `job_url` references in `src/` | **169**, across 18 files |
| in `tests/` | **140**, across 30 files |
| Files `spaces-prd.md` §4 lists as "ports unchanged" that reference it | **4** — `replies.py` (10), `messages.py` (6), `bookings.py` (3), `domain/metrics.py` (2) |
| Rows whose history detaches if the hash input moves | 196 contacts → 55 touches, 24 sequences, 142 messages, 10 interactions |

And it is **partial no matter what**: `messages.job_url` (`messages.py:27`) and
`job_events.job_url` (`database.py:189`) carry the same value under the old name. §6's "one
renamed column" is three, or it is a rename that leaves two columns disagreeing with the one it
just fixed.

Against that, the argument *for* renaming is real and should not be waved away: this codebase's
culture is that a name must not lie, and a future reader finding `job_url =
'target:acme:ridgeline'` is owed better. Doing it later is also strictly harder. So it is a
ticket, not a "no".

**If SPACE-1b is taken, the migration must converge rather than rename**, because the dicts
race it (D4) and because this app gets killed mid-operation:

```python
def up(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(contacts)")}
    if "anchor" in cols and "job_url" in cols:
        # The additive pass won the race and created an empty `anchor`. A plain RENAME here
        # fails with "duplicate column name" and leaves 196 contacts holding a NULL anchor.
        conn.execute("UPDATE contacts SET anchor = job_url WHERE anchor IS NULL OR anchor = ''")
        conn.execute("ALTER TABLE contacts DROP COLUMN job_url")
    elif "job_url" in cols:
        conn.execute("ALTER TABLE contacts RENAME COLUMN job_url TO anchor")
    # else: already converged — the third state, and the one a `rename` migration forgets.
    conn.commit()
```

Three states, three answers, same shape as `migrations._claim`. And the dict change ships in
**the same commit**, or `ensure_contacts_columns` tries `ADD COLUMN job_url TEXT NOT NULL`,
which SQLite refuses outright.

---

## Invariants this ticket establishes

1. Every `jobs` row and every `contacts` row has a non-empty `space_id`. Enforced by the column
   default, so it is true of rows written by code that has never heard of Spaces.
2. Every `space_id` in use names a row in `spaces`. Not an FK — SQLite foreign keys are off by
   default here, and turning them on introduces ordering coupling between the additive pass and
   the migration for no benefit. Asserted by a test instead.
3. `strategy` is never read to decide Space membership. Asserted by a test.
4. A Space's `id` is immutable (`spaces-prd.md` §13.2) — it is hashed into `contact_id` for
   targets rows. The UI must not offer to edit it.

## Tests

| Test | Kills |
|---|---|
| `test_space_id_defaults_without_a_backfill` | an UPDATE-based migration that misses rows written mid-run |
| `test_every_contact_id_is_unchanged_by_003` | any re-keying; snapshots ids before and after |
| `test_003_is_idempotent` | three runs, identical schema and rows (existing convention) |
| `test_fresh_and_migrated_databases_have_identical_schemas` | already exists; must keep passing |
| `test_an_empty_space_registry_raises_rather_than_returning_nothing` | D3's silent-zero |
| `test_strategy_is_never_consulted_for_space_membership` | D2 drifting back to one key |
| `test_the_query_budget_does_not_move` | already exists at 80 |

## Deliberately not decided here

- What a targets row shows in the panel (SPACE-3).
- Whether `identities` values fall back to the current globals or replace them (ID-1). 003 seeds
  one identity row with empty fields, which reads as "use the global" and keeps today's
  behaviour byte-identical.
- Per-space overrides of `company_cap`. The grill argues that cap belongs on the identity for
  the same reason §Headline 4 moved the daily limit there — the recipient does not know what a
  Space is. Left open; `spaces-prd.md` §7 needs revising either way.

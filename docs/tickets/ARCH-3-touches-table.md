# ARCH-3 — `touches` table

**Phase:** 3 · **Size:** M (~1d) · **Depends on:** ARCH-1 · **Status:** Todo
**PRD:** `architecture-prd.md` §Q2, ARCH-3

## Problem

`contacts` has 42 columns and two of everything:

```
followup_count      followup_message      followup_status      followed_up_at
li_followup_count   li_followup_message   li_followup_status   li_followed_up_at
```

That is **one concept — a touch sequence — copy-pasted per channel.** Adding SMS makes it
three; adding Spaces (`crm-prd.md`) makes it a cartesian product. The cost is not storage at
28 rows; it is duplicated scheduling code, which is the expensive kind.

## Scope / tasks

- [ ] New table (own migration, mirroring the `_CONTACT_COLUMNS` pattern):
      `touches(id, contact_id, channel, seq, due_at, sent_at, subject, body, status, error)`
- [ ] Index on `(contact_id, channel, seq)` and on `(status, due_at)`
- [ ] `domain/followup.py` reads/writes `touches` — one ladder engine, already merged in ARCH-1
- [ ] A **channel registry**: `{name, schedule_env, anchor_field, sender, can_autosend}` so a
      new channel is one row plus one prompt
- [ ] **Backfill migration** for all 28 existing contacts, both ladders, no data loss
- [ ] `--dry-run` that prints the row-by-row diff before writing; back up the DB first
- [ ] Drop the eight `li_followup_*` / `followup_*` columns only after the backfill verifies

## Acceptance criteria

- [ ] `contacts` down to ~32 columns; the eight ladder columns gone from `_CONTACT_COLUMNS`
- [ ] Both ladders run through one code path — deleting the LinkedIn branch breaks both tests
- [ ] Backfill is idempotent and reversible from the pre-migration backup
- [ ] Every existing follow-up state survives: counts, timestamps, drafts, stopped/replied
- [ ] **The real test:** adding SMS is one registry row + one prompt, with no schema change
      and no new scheduling code
- [ ] 228 tests green; follow-up due-dates identical before and after for the live DB

## Risks / notes

- **First real data migration.** Backup, dry-run diff, then write — in that order.
- Sequence numbering: use `seq` per `(contact_id, channel)`, not a global counter, or the
  "touch 2 of 3" label breaks.
- Do not migrate `dm_status` / `dm_sent_at` into `touches` — those record the *invite*, which
  is a different event from a follow-up touch. Keep them on `contacts`.

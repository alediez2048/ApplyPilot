# ARCH-3 — `touches` table

**Phase:** 3 · **Size:** M (~1d) · **Depends on:** ARCH-1 · **Status:** ✅ Done (2026-07-29)
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

- [x] New table: `touches(id, contact_id, channel, seq, due_at, sent_at, subject, body,
      status, error, created_at, updated_at)`
- [x] **A second table, deviating from the sketch above:** `sequences(contact_id, channel,
      status, note, updated_at)`. `followup_status` was doing two unrelated jobs —
      `drafted|sending|sent|failed` is one touch's delivery lifecycle, `stopped|replied` is
      the whole sequence's terminal state, and `claim_followup_send` had to guard on both in
      one condition. That conflation is *why* the LinkedIn copy drifted: whoever copied it
      dropped `subject` and `error` because it wasn't clear which lifecycle they belonged to.
      A reply is a fact about a conversation, not about a message we sent.
- [x] Index on `(contact_id, channel, seq)` (unique) and on `(status, due_at)`
- [x] `domain/followup.py` takes ladder state as data; names no ladder column of its own
- [x] Channel registry — and `_is_ready` is now data too: `ready=(("phone", None),)` replaced
      the `if channel is EMAIL: … if channel is LINKEDIN: …` the ARCH-1 docstring warned about
- [x] Backfill for all existing contacts, both ladders, no data loss
- [x] `--dry-run` printing the row-by-row plan; DB backed up first via sqlite's backup API
- [x] Dropped the legacy columns after `--verify` came back clean

## Acceptance criteria

- [x] `contacts` down from **42 to 32** columns — the ticket said "eight"; there were **ten**
      (email had `subject` and `error`, LinkedIn silently did not). Pinned by
      `test_a_migrated_database_has_no_ladder_columns_left`, which also guards the trap:
      leaving one name in `_CONTACT_COLUMNS` re-adds all ten on the next startup.
- [x] One code path — `_followup_action` was two mirrored blocks (`stop`/`li_stop`,
      `save`/`li_save`, `draft`/`li_draft`); there is no LinkedIn half left to delete.
      `test_no_channel_specific_ladder_functions` fails if one grows back.
- [x] Backfill idempotent (asserted over three runs) and reversible from the backup
- [x] Every existing follow-up state survives — see Verification
- [x] **The real test:** `test_adding_a_channel_needs_no_schema_change` defines an SMS channel
      that exists nowhere in the codebase and drives it end to end — storage, scheduling,
      terminal state, readiness — with no registration, no `ALTER TABLE`, no new branch.
- [x] **276 tests green**; `/api/status` byte-identical before and after on the live DB

## Risks / notes

- **First real data migration.** Backup, dry-run diff, then write — in that order.
- Sequence numbering: use `seq` per `(contact_id, channel)`, not a global counter, or the
  "touch 2 of 3" label breaks.
- Do not migrate `dm_status` / `dm_sent_at` into `touches` — those record the *invite*, which
  is a different event from a follow-up touch. Keep them on `contacts`.

## Verification (live DB, 2026-07-29)

Backup → dry-run → apply → verify → drop, in that order, with the dashboard stopped.

- 8 ladders migrated: 7 email touches + 1 `replied` sequence. All four `li_followup_*`
  columns were empty across all 28 contacts — the LinkedIn ladder had never run, so that
  half of the migration was a no-op and the real risk was far lower than assumed.
- `verify()` re-derives the OLD columns from the NEW tables and diffs. Clean. It is also
  proven non-vacuous (`test_verify_is_not_vacuous`) — deleting a touch row makes it fail.
- **`/api/status` diffed byte-for-byte against the pre-migration payload**, captured from a
  git worktree at the previous commit. The first run **DIFFERED on one field**:
  `followup_status` reported `''` where it used to report `'sent'`, because a fully-sent
  ladder has no pending touch row. Only `'replied'` is read by the frontend, so nothing was
  visibly broken — but no unit test caught it, and a payload diff you wave through is a check
  nobody can trust next time. `_legacy_followup_status()` restores the old precedence, and
  the payload is now identical before the drop, after the drop, and after restart.

Backup: `~/.applypilot/backups/applypilot-20260729-163729-pre-touches.db`

# CRM-1 — Reply detection (make the system see)

**Phase:** 1 · **Size:** M (~1d) · **Depends on:** Gmail `gmail.metadata` scope (granted) · **Status:** Todo
**PRD:** `architecture-prd.md` §4.1 · `crm-prd.md` §6.1
**Why first:** it is the single most important signal in the product, the system is blind to it,
and **the blindness compounds with every send.**

> Revised 2026-07-30 after the apply/outreach work. Two instructions in the original were
> factually wrong (see §What changed) and would have sent an implementer down a dead end.

## Problem

Measured on the live DB (2026-07-30): **33 emails sent, 7 follow-up touches, 12 applications —
and exactly 1 reply recorded, typed in by hand.** It was 13 emails two days ago. Nothing reads
the inbox, so:

- Follow-ups nudge people who may have replied days ago. That is worse than not following up.
- The checklist keeps a job below 100% for a loop that is actually closed.
- No downstream metric is possible: "which emails worked?" is unanswerable (blocks CRM-2).

Everything needed is already stored. **All 33 sent contacts have both `thread_id` and
`rfc_message_id`** — verified live. The only missing piece is reading.

## Scope / tasks

- [ ] **`networking/gmail_read.py`** (new)
  - [ ] `poll_replies(since_history_id=None) -> list[dict]` — `users.messages.list` newest-first,
        watermarked by `historyId` so each poll is incremental
  - [ ] `thread_has_inbound(thread_id) -> dict|None` — list the thread, return the first message
        whose `From` is **not** the connected account (headers only; metadata scope cannot read bodies)
  - [ ] Store the watermark in a small `kv` table or `~/.applypilot/gmail_watermark`
- [ ] **Matching** — join inbound → contact on `contacts.thread_id`; fall back to
      `In-Reply-To` matching `rfc_message_id`; last resort, sender address.
      **Put the matching rules in `domain/` as pure functions** (dicts in, verdict out) — the
      boundary exists and forbids `http`/`sqlite3` there, which is exactly what makes this
      testable without a mailbox.
- [ ] **Halting the ladder — NOT `followup_status`.** That column was removed by ARCH-3.
      Terminal state lives in `sequences`:
      `touches.set_sequence_status(contact_id, "email", "replied", note=...)`.
      There is no per-channel column to write and none should be added.
- [ ] **`replied_at`** — new column on `contacts` (add to `_CONTACT_COLUMNS`; it forward-migrates).
- [ ] **`job_events`** — `Sumit Singh replied on Jul 28.`
- [ ] **Suppression** — a reply stops that contact's EMAIL ladder. Do **not** auto-stop the
      LinkedIn ladder (different thread, different conversation) — flag it instead.
- [ ] **Checklist** — a replied contact no longer counts as "follow-up owed".
- [ ] **Dashboard** — `✓ replied Jul 28` pill on the contact row; a "Replied" filter pill.
- [ ] **Trigger** — poll on dashboard start and every N minutes while it runs. Unattended
      polling is CRM-3, not this ticket.
- [ ] **`doctor`** — report last poll time and whether the metadata scope is granted.

## Acceptance criteria

- [ ] A real reply in Gmail flips the contact to `replied` within one poll cycle
- [ ] That contact disappears from the Follow-ups due list **without any manual action**
- [ ] The job's checklist stops counting it as owed
- [ ] Polling is incremental — a second poll with no new mail makes no per-message API calls
- [ ] Works with `gmail.metadata` only; **never** requests `gmail.readonly`
- [ ] Missing scope degrades gracefully: feature off, `doctor` says why, sending unaffected
- [ ] `/api/status` stays inside its query budget — `tests/test_query_budget.py` will fail if
      reply state is fetched per contact instead of batched
- [ ] Tests: thread matching, `In-Reply-To` fallback, own-messages-are-not-replies,
      watermark advance, missing-scope path, **a reply for a contact that was since deleted**

## Risks / notes

- **Own messages must not count as replies.** Filter on `From` != connected account, and on
  `labelIds` not containing `SENT`.
- `gmail.metadata` **cannot** use the `q=` search parameter — list by thread, don't query.
- **A matched contact may no longer exist.** Deleting a contact now also clears its `touches`
  and `sequences` rows (2026-07-30), so a reply arriving for a deleted contact must be a no-op,
  not a resurrection or a crash. Contact ids are a hash of (job, identity), so the id in an old
  thread can still be *reconstructible* — do not let that recreate the row.
- Snippets may contain PII — store the minimum needed to show "they replied".
- **A detected reply is worth nothing if nobody sees it.** The dashboard has no notification of
  any kind (§Known debt 2a). Landing CRM-1 makes that gap sharper, not smaller — pair it with
  the tab-title badge, which needs no scheduler and no permissions.

## What changed since this ticket was written (2026-07-28)

| Original said | Now |
|---|---|
| `sets followup_status='replied'` | **that column no longer exists** — ARCH-3 moved terminal state to `sequences`; use `touches.set_sequence_status()` |
| "The 11 pre-threading contacts have no `thread_id`; `backfill_thread_ids()` must run first" | **obsolete** — all 33 sent contacts have `thread_id` *and* `rfc_message_id`; the backfill has nothing left to recover |
| 13 emails, 7 follow-ups, 6 applications | **33 emails, 7 touches, 12 applications** |
| — | `domain/` exists; matching belongs there as pure functions |
| — | contacts can now be **deleted**, taking their ladder with them |

# CRM-1 — Reply detection (make the system see)

**Phase:** 1 · **Size:** M (~1d) · **Depends on:** Gmail `gmail.metadata` scope · **Status:** Todo
**PRD:** `architecture-prd.md` §4.1 · `crm-prd.md` §6.1
**Why first:** it is the single most important signal in the product and the system is blind to it.

## Problem

Measured on the live DB: **13 emails sent, 7 follow-ups sent, 1 reply recorded** — and that
one was typed in by hand. Nothing reads the inbox, so:

- Follow-ups nudge people who may have replied days ago. That is worse than not following up.
- The checklist keeps a job below 100% for a loop that is actually closed.
- No downstream metric is possible: "which emails worked?" is unanswerable (blocks CRM-2).

Everything needed is already stored — `thread_id` and `rfc_message_id` are captured at send
time. The only missing piece is reading.

## Scope / tasks

- [ ] **`networking/gmail_read.py`** (new)
  - [ ] `poll_replies(since_history_id=None) -> list[dict]` — `users.messages.list` newest-first,
        watermarked by `historyId` so each poll is incremental
  - [ ] `thread_has_inbound(thread_id) -> dict|None` — list the thread, return the first message
        whose `From` is **not** the connected account (headers only; metadata scope cannot read bodies)
  - [ ] Store the watermark in a small `kv` table or `~/.applypilot/gmail_watermark`
- [ ] **Matching** — join inbound → contact on `contacts.thread_id`; fall back to
      `In-Reply-To` matching `rfc_message_id`; last resort, sender address.
- [ ] **`store.py`** — `mark_replied(contact_id, replied_at, snippet)`:
  - [ ] sets `followup_status='replied'` (halts the email ladder)
  - [ ] writes `replied_at` (new column)
  - [ ] appends a `job_events` row: `Sumit Singh replied on Jul 28.`
- [ ] **Suppression** — a reply on ANY channel stops that contact's email ladder. Do **not**
      auto-stop the LinkedIn ladder (different thread, different conversation) — flag it instead.
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
- [ ] Tests: thread matching, `In-Reply-To` fallback, own-messages-are-not-replies,
      watermark advance, missing-scope path

## Risks / notes

- **Own messages must not count as replies.** Filter on `From` != connected account, and on
  `labelIds` not containing `SENT`.
- `gmail.metadata` **cannot** use the `q=` search parameter — list by thread, don't query.
- The 11 pre-threading contacts have no `thread_id`; `backfill_thread_ids()` (already built)
  must run first or they can never be matched.
- Snippets may contain PII — store the minimum needed to show "they replied".

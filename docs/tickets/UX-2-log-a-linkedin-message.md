# UX-3 — Log an inbound LinkedIn message

**Size:** M · **Depends on:** nothing · **Status:** DONE 2026-08-04
**Reported:** 2026-08-04 ("people reach out via LinkedIn and I don't know where to log it")

## Diagnosis

There is nowhere, and the reason is structural.

**`messages` is Gmail-shaped.** Primary key `(message_id, contact_id)` where `message_id` is
Gmail's own id; the table also carries `thread_id`, `rfc_message_id`, `from_addr`, `cc_addrs`.
Every writer is `replies.py` / `gmail_read.py`. A LinkedIn DM has none of those and inventing
fake ids to fit would corrupt the reply-detection join.

**`contacts` records only what WE sent.** `dm_status` is `sent` | `manual` — live counts: 71
manual, 6 sent, 106 empty. There is no `accepted`, no inbound, no text. CLAUDE.md §Lessons 35
already documents that `dm_status` proves nothing about them.

**`interactions` is the right home and needs no migration.** `kind` is an open `TEXT NOT NULL`
column and `record(contact_id, kind, at, detail, source)` accepts anything — it is holding
`booked` events today. The id is `sha256(contact|kind|at)`, so re-logging the same message is
an upsert rather than a duplicate.

**Design constraint (§Lessons 3, twice-abandoned):** nothing may read LinkedIn. This is
operator-entered, and it must be tagged `source='manual'` so it never reads as detected — the
same distinction the profile-view note already makes.

## Scope / tasks

- [x] `kind='linkedin_in'` / `linkedin_out`, `detail` carries the text, capped at `PASTED_MAX`
      **at the write** — trimming at render time leaves the whole thing on disk.
- [x] Paste box + `They messaged me` / `I replied` on the LinkedIn tab. An empty paste is
      refused: a row with no body records that something happened and loses what it was, which
      is the state `dm_status` was already in.
- [x] `linkedinThread(c)` renders the exchange above the composer.
- [x] A logged inbound message sets `sequences.status='replied'` for **linkedin only** — email
      and SMS keep running. Our own outbound stops nothing.
- [x] `_li_state()` replaces the prompt's unconditional "they accepted the invite and have not
      replied" with what is actually true. That sentence was a claim, not an observation, and
      became false the moment anything was logged — §Lessons 40: two instructions disagreeing
      is a code bug, not a wording problem.

## Decided 2026-08-04 — separate, joined at the counter

An inbound LinkedIn message reaches the 🔔 counter and never writes `replied_at`. It is joined
in `_awaiting_us`, which reads the rendered timeline rather than a new column — so there is
nothing to keep in sync and nothing that can drift (§Lessons 21).

`replied_at` keeps meaning *a detected email reply*, because it is what `metrics.by_variant`
divides by: mixing a typed-in number into a measured one makes the copy experiment unreadable,
and that experiment is the only thing that can eventually say which drafts work.

## Tests

15 tests in `tests/test_linkedin_messages.py`. Mutation-verified: un-stopping the ladder,
counting our own outbound as engagement, dropping it from the counter, and reverting the
prompt to assert silence each kill a test.

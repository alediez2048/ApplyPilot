# UX-3 — Log an inbound LinkedIn message

**Size:** M (~half a day) · **Depends on:** nothing · **Status:** Todo
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

- [ ] `kind='linkedin_in'` (they wrote) and `linkedin_out` (you replied outside the app).
      `detail` carries the text, capped like `PASTED_MAX` (2000) — this is correspondence and
      the DB is unencrypted.
- [ ] On the contact's **🔗 LinkedIn** tab: a paste box + `They messaged me` / `I replied`,
      with a date defaulting to now and editable.
- [ ] Render the LinkedIn tab as a **timeline**, like the email one — invite sent, they
      accepted, they wrote, you replied — instead of only the outbound note composer.
      `hasConversation()` is the existing branch to copy (§Lessons 31).
- [ ] **A logged inbound message stops the LinkedIn ladder**, exactly as an email reply stops
      the email one. Reuse `sequences`; do not add a second terminal-state mechanism.
- [ ] Feed logged text to `draft_followup` for that channel, so touch 2 knows what was said
      (§Lessons 39: a function that takes a thread must actually read it).

## Open question

Should an inbound LinkedIn message also mark the CONTACT as replied for the 🔔 counter and the
reply-rate metric? Argument for: a reply is a reply, and the counter's job is "who is waiting
on me". Argument against: `replied_at` currently means *a detected email reply*, and
overloading it makes `metrics.by_variant` compare a measured number with a typed-in one.
Recommendation: separate field, joined at the counter, kept out of email reply rate.

## Tests

- [ ] `test_a_logged_linkedin_reply_stops_that_ladder_only` — email keeps running.
- [ ] `test_logging_the_same_message_twice_is_idempotent` — run it twice (§Lessons 22).
- [ ] `test_the_next_linkedin_draft_reads_what_they_said` — the §Lessons 39 check.
- [ ] `test_nothing_reads_linkedin` — no new network path to linkedin.com.

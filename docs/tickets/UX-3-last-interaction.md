# UX-4 — "Last interaction" on the row

**Size:** M · **Depends on:** UX-2 (LinkedIn events feed it) · **Status:** DONE 2026-08-04

## Diagnosis

The concept does not exist. `grep -rn "last_touch\|lastTouch\|last_interaction"` across
`dashboard.js`, `web_dashboard.py` and `domain/` returns **zero hits**.

Every fact needed is already stored, in six places, none of them joined:

| Event | Where |
|---|---|
| applied | `jobs.applied_at` |
| email sent | `contacts.submitted_at` |
| follow-up sent | `touches.sent_at` (any channel) |
| they replied | `messages` (inbound) / `contacts.replied_at` |
| LinkedIn invite / message | `contacts.dm_sent_at`, and UX-2's `interactions` |
| text sent | `contacts.sms_sent_at` |
| deck opened / call booked | `contacts.deck_last_at`, `interactions` |

So this is a derivation, not a schema change — which is the right shape, because a stored
"last interaction" column would drift the moment any of those six paths wrote without updating
it. §Lessons 21 is the same failure in the other direction.

**The one real design decision:** last interaction must record **who acted**. "Emailed them 6
days ago" and "they replied 6 days ago" are the same age and opposite situations. A single
timestamp with no direction is the flat-count mistake the 🔔 counter already avoided.

## Scope / tasks

- [x] `domain/lastinteraction.py` — pure, asserted by a test that greps it for `sqlite3`,
      `execute(` and any HTTP client.
- [x] Reads as a sentence: *"Sarah replied · 2d ago"*, *"You invited Joshua on LinkedIn"*.
      First name only — this sits on a table row, and "Sarah Chen-Okonkwo" does not fit.
- [x] On the collapsed row, between the steps and Next.
- [x] Inbound is accent-coloured and bold with a `←`; outbound is faint with a `→`.
- [x] Zero extra queries — reuses the contact timeline `_attach_interactions` already builds
      and the ladder states `_followup_panel` already loads. Query budget unmoved at 8 tests.

### Two things the build turned up

**Follow-ups are not in the interaction timeline.** `domain/interactions.for_contact` derives
from contact COLUMNS; a follow-up lives in `touches`, keyed per (contact, channel). Without
reading the ladders too, a job whose only recent activity was a third follow-up would report
the original email from two weeks earlier. Pinned by a test.

**Direction reuses `ix.ENGAGEMENT`** rather than listing kinds again. A signal cannot be
"engagement" in one module and "our own action" in another, and a second list is the copy that
falls behind — which is exactly what ARCH-3 removed for follow-up channels.

### Live after the change

20 of 22 jobs report one: 17 outbound, 3 inbound. The three inbound are the ones worth seeing —
including a Zendesk contact whose profile view was the most recent thing that happened on
that job and was previously invisible on the row.

## Tests

13 tests in `tests/test_last_interaction.py` plus a render test. Mutation-verified: dropping
direction, ignoring `touches`, and removing it from the strip each kill a test.

Includes the naive-timestamp case (§Lessons 6 — an unparseable value is dropped, never
compared) and "it never invents a time": a row with no timestamp must not become "just now".

# UX-4 — "Last interaction" on the row

**Size:** M (~half a day) · **Depends on:** UX-2 (LinkedIn events feed it) · **Status:** Todo

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

- [ ] `domain/lastinteraction.py` — pure. In: job + contacts + touches + messages +
      interactions. Out: `{at, kind, direction: 'in'|'out', who, label}`.
- [ ] Label reads as a sentence, not a field: *"Sarah replied · 2 days ago"*,
      *"You followed up · 6 days ago"*, *"Applied · 12 days ago"*.
- [ ] Render on the **collapsed** row, next to the status strip. A state you must expand a job
      to discover is a state nobody sees for days (§Lessons 27).
- [ ] Inbound gets visual weight; outbound is muted. The asymmetry IS the information.
- [ ] Budget: one query per payload, joined in Python from data `/api/status` already loads.
      `tests/test_query_budget.py` must not move.

## Tests

- [ ] `test_it_reports_the_most_recent_event_across_all_sources` — one per source, shuffled.
- [ ] `test_direction_is_carried` — an inbound and an outbound at the same instant render
      differently. Assert the direction field, not the copy.
- [ ] `test_a_job_with_no_contacts_still_reports_applied`.
- [ ] `test_it_costs_no_extra_queries` — the budget test, unchanged.

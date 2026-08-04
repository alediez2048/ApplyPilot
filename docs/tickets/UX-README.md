# UX-* — dashboard defects and gaps, 2026-08-04

Six items reported in one pass after the pipeline reached daily use. **Numbered in the order
they were reported**, not in the order I would have done them — an earlier version renumbered
them by my own priority while keeping the same labels, which made "start with UX-1" mean two
different tickets to the two of us.

| # | Ticket | Size | Kind | Status |
|---|---|---|---|---|
| 1 | `UX-1` retire the Interactions tab | S | cleanup | **DONE** 2026-08-04 |
| 2 | `UX-2` log a LinkedIn message | M | gap | **DONE** 2026-08-04 |
| 3 | `UX-3` last interaction on the row | M | gap | **DONE** 2026-08-04 |
| 4 | `UX-4` interview state is real | S | **bug** | **DONE** 2026-08-04 |
| 5 | `UX-5` application temperature | M | feature | **DONE** 2026-08-04 |
| 6 | `UX-6` search everything | S | gap | **DONE** 2026-08-04 |

**All six shipped 2026-08-04.**

## The one real ordering constraint

**UX-1 must not delete the ledger.** The Interactions tab is worthless — 2 rows across the
whole table — but it holds the app's only manual-log affordance, and the `interactions` store
is where UX-2's LinkedIn messages belong and where UX-3 reads its events. Retiring the tab
means moving that button onto the contact card, not removing the capability underneath it.

Everything else is independent. UX-5 reads UX-3's output, so it is easier after; UX-6 touches
nothing.

## The finding that generalises

Three of these six are the same failure: **a value one layer computes that the other cannot
see.**

- UX-4: `interview_at` was written to the DB and absent from `dashboard_rows()`'s SELECT, so
  the browser never saw it. Two "fixes" moved the button and changed the grey — the wrong
  layer twice, because the payload was never checked.
- UX-2: `dm_status` records what we sent; nothing records what they sent.
- UX-3: six sources of "when did something last happen", none of them joined.

§Lessons 21 already names this — a derived field is not a column, and the gap is silent. The
tests in each ticket assert against the ENGINE, not the view, for that reason.

# UX-* — dashboard defects and gaps, 2026-08-04

Six items reported in one pass after the pipeline reached daily use. Diagnosed before any
code was written; each ticket carries its evidence.

**Implement one at a time, in this order.**

| # | Ticket | Size | Kind | Depends on |
|---|---|---|---|---|
| 1 | `UX-1` interview state is real | S | **bug** | — |
| 2 | `UX-3` log a LinkedIn message | M | gap | — |
| 3 | `UX-2` retire the Interactions tab | S | cleanup | UX-3 |
| 4 | `UX-4` last interaction on the row | M | gap | UX-3 |
| 5 | `UX-6` search everything | S | gap | — |
| 6 | `UX-5` application temperature | M | feature | UX-4 |

## Why this order

**UX-1 first** because it is the only actual bug: a state that is written, never read, and
whose dialog promises something the engine does not do. It is also small.

**UX-3 before UX-2.** The Interactions tab holds the app's only manual-log affordance, and the
`interactions` store is where LinkedIn messages belong. Deleting the tab first would remove
the foundation of the next two tickets and then require rebuilding it.

**UX-4 before UX-5.** Temperature reads last-interaction as an input; building the colour
before the fact it depends on means guessing twice.

**UX-6 anywhere** — it touches nothing else.

## The finding that generalises

Three of these six are the same failure: **a value the dashboard computes but the engine
cannot see, or vice versa.**

- UX-1: `interview_at` written to the DB, absent from the payload; and the ladder never learns
  about it, so the UI hides work the CLI would still do.
- UX-3: `dm_status` records what we sent, nothing records what they sent.
- UX-4: six sources of "when did something last happen", none joined.

§Lessons 21 already names this — a derived field is not a column, and the gap is silent. The
tests in each ticket are written to fail on the ENGINE, not on the view, for that reason.

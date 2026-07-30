# CRM-2 — Outcome metrics (make the system learn)

**Phase:** 2 · **Size:** M (~1d) · **Depends on:** CRM-1, ARCH-1 · **Status:** Todo
**PRD:** `architecture-prd.md` §4.0, §4.1

> Revised 2026-07-30. Numbers refreshed, and three new dimensions became measurable because
> the data to split on now exists.

## Problem

**33 emails sent, 7 follow-up touches, 12 applications, 50 contacts** — and the system cannot
answer a single one of:

- Which subject lines got replies?
- Do warm connections outperform cold recruiters?
- Is LinkedIn worth the manual effort versus email?
- Does `TAILOR_AGGRESSIVE=1` actually convert, or does it just feel productive?
- Which companies never respond, so stop spending Apollo credits there?

The raw events are already in `job_events`, `contacts` and `touches`. Nothing aggregates them.
**A CRM that generates but never counts is a very sophisticated mail merge.**

## Scope / tasks

- [ ] `domain/metrics.py` — pure aggregation over rows (no DB, no HTTP):
  - [ ] `funnel()` — discovered → applied → contacted → replied, with conversion at each step
  - [ ] `by_channel()` — reply rate for email vs LinkedIn
  - [ ] `by_layer()` — cold Apollo vs warm connection. **Now genuinely splittable**:
        `contacts.source` is `apollo` (42) / `connection` (3) / `hunter` (5, legacy — fold into
        cold or exclude, but say which)
  - [ ] `by_touch()` — reply rate for touch 1 / 2 / 3 (does the third message earn its place?).
        Read from `touches.seq`, not a column
  - [ ] `by_company()` — who never responds
  - [ ] `time_to_reply()` — median, to calibrate `FOLLOWUP_SCHEDULE` from data instead of guessing
- [ ] **New dimensions worth a look, all now populated:**
  - [ ] `by_confidence()` — do `unconfirmed` contacts ever reply? If they never do, verification
        should reject more aggressively; if they do, it should reject less. Currently 20 `high`
        / 30 unset (pre-verification rows) — the split only becomes meaningful as new contacts
        accumulate, so ship the cut and let it fill in
  - [ ] `hot_vs_cold_by_reply` — with only 3 connection-sourced contacts this is anecdote, not
        data. **Mark it as such rather than reporting a rate.**
- [ ] A **Metrics** panel in the dashboard header: funnel bar + the three or four rates that
      change behaviour. Not a chart wall.
- [ ] `applypilot stats --outreach` for the same numbers in the terminal.
- [ ] Small-sample honesty: show `n` beside every rate and grey out anything under n=10.
      With 33 emails the top-level reply rate is becoming meaningful; **every per-slice cut is
      still far too small** — `by_touch()` has 7 data points across three touches.

## Acceptance criteria

- [ ] Every metric is a pure function over rows, unit-testable with fixtures
- [ ] The dashboard answers "is email or LinkedIn working better?" in one glance
- [ ] Rates below n=10 are visibly marked as not-yet-meaningful
- [ ] Zero new runtime dependencies (no pandas in the hot path, no charting lib)
- [ ] Numbers reconcile with hand-counted SQL on the live DB
- [ ] The panel does not push `/api/status` over its query budget — aggregate in one pass

## Risks / notes

- **Garbage in.** Metrics are only as good as CRM-1's reply detection; ship it first. With
  1 recorded reply, every rate here is currently 3% or 0% and means nothing.
- Resist a dashboard of twelve charts. The test is whether it changes a decision — if a number
  wouldn't make you do something differently, cut it.
- Attribution is genuinely hard: a reply may follow an email *and* a LinkedIn note. Attribute
  to the last touch before the reply and say so in the UI rather than pretending precision.
- **Deleted contacts silently bias every rate.** Removing a wrong contact (2026-07-30) erases
  them and their touches, so a rejected-as-wrong person never appears in the denominator. That
  is usually right — they were never a real prospect — but a company you deleted five people
  from will look untouched rather than unproductive. Decide the treatment and write it down.
- **One variable at a time is impossible here.** The intro-deck link, the schedule-a-call CTA
  and the warm-opening rewrite all shipped 2026-07-29/30, mid-corpus. Emails before and after
  are not comparable, and no amount of slicing fixes that. Record the change dates so a future
  reading of "reply rate went up" is not attributed to the wrong cause.

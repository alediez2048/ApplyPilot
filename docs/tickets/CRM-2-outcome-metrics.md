# CRM-2 — Outcome metrics (make the system learn)

**Phase:** 2 · **Size:** M (~1d) · **Depends on:** CRM-1, ARCH-1 · **Status:** Todo
**PRD:** `architecture-prd.md` §4.0, §4.1

## Problem

13 emails sent, 7 follow-ups, 6 applications — and the system cannot answer a single one of:

- Which subject lines got replies?
- Do warm connections outperform cold recruiters?
- Is LinkedIn worth the manual effort versus email?
- Does `TAILOR_AGGRESSIVE=1` actually convert, or does it just feel productive?
- Which companies never respond, so stop spending Apollo credits there?

The raw events are already in `job_events` and `contacts`. Nothing aggregates them. **A CRM
that generates but never counts is a very sophisticated mail merge.**

## Scope / tasks

- [ ] `domain/metrics.py` — pure aggregation over rows (no DB, no HTTP):
  - [ ] `funnel()` — discovered → applied → contacted → replied, with conversion at each step
  - [ ] `by_channel()` — reply rate for email vs LinkedIn
  - [ ] `by_layer()` — cold Apollo vs warm connection
  - [ ] `by_touch()` — reply rate for touch 1 / 2 / 3 (does the third message earn its place?)
  - [ ] `by_company()` — who never responds
  - [ ] `time_to_reply()` — median, to calibrate `FOLLOWUP_SCHEDULE` from data instead of guessing
- [ ] A **Metrics** panel in the dashboard header: funnel bar + the three or four rates that
      change behaviour. Not a chart wall.
- [ ] `applypilot stats --outreach` for the same numbers in the terminal.
- [ ] Small-sample honesty: show `n` beside every rate and grey out anything under n=10.
      With 13 emails, "23% reply rate" is noise presented as insight.

## Acceptance criteria

- [ ] Every metric is a pure function over rows, unit-testable with fixtures
- [ ] The dashboard answers "is email or LinkedIn working better?" in one glance
- [ ] Rates below n=10 are visibly marked as not-yet-meaningful
- [ ] Zero new runtime dependencies (no pandas in the hot path, no charting lib)
- [ ] Numbers reconcile with hand-counted SQL on the live DB

## Risks / notes

- **Garbage in.** Metrics are only as good as CRM-1's reply detection; ship it first.
- Resist a dashboard of twelve charts. The test is whether it changes a decision — if a number
  wouldn't make you do something differently, cut it.
- Attribution is genuinely hard: a reply may follow an email *and* a LinkedIn note. Attribute
  to the last touch before the reply and say so in the UI rather than pretending precision.

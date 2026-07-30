# CRM-3 — Scheduler (make the system act unattended)

**Phase:** 2 · **Size:** S (~0.5d) · **Depends on:** ARCH-1 · **Status:** Todo
**PRD:** `architecture-prd.md` §4.1

> Revised 2026-07-30. The notification half of this ticket got sharply more valuable — the
> co-pilot apply flow now *ends* by waiting for the operator — and a cheap subset of it can
> ship on its own without any scheduler. See §Split this ticket.

## Problem

Nothing runs unless the dashboard is open. Every action needs a click. That caps the leverage
permanently — **the system can never surface something you did not think to ask for.**

The "alert me at 48 hours" behaviour only fires if you happen to be looking at the tab.

Since 2026-07-30 there is a second, sharper version of the same problem: **co-pilot apply ends
by handing an open browser to the operator and waiting.** The queue stays paused until they
notice. There is no sound, no desktop notification, no tab-title badge — only a 2.5s refresh
that helps if you are already looking. A filled application left sitting is one a restart
eventually closes, and the filled form is unrecoverable.

## Split this ticket

**3a — Notification (do this first, independently).** A tab-title badge — `(1) ⚠ ApplyPilot` —
needs no scheduler, no permissions, no new dependency, and covers the common case where the
dashboard tab is open but not focused. A desktop notification behind an opt-in toggle covers
the rest. **This is worth shipping before CRM-1**, because it is hours of work and it protects
filled applications today.

**3b — `applypilot tick` (the rest of this ticket).** Only useful once CRM-1 exists; polling an
inbox you cannot read achieves nothing.

## Scope / tasks

- [ ] **3a** — needs-you badge in `document.title`, driven by the same states the row uses
      (`ready_to_submit`, `needs_human`, follow-ups due). Restore the plain title when clear.
- [ ] **3a** — optional desktop notification, permission requested only on opt-in; clicking it
      focuses the dashboard.
- [ ] `applypilot tick` — one idempotent command, safe to run repeatedly:
  - [ ] poll for replies (CRM-1)
  - [ ] recompute follow-up due states
  - [ ] optionally draft follow-ups that came due, leaving them **queued, not sent**
  - [ ] `release_stale_locks()` — already exists; an interrupted apply leaves `in_progress`
        forever and `acquire_job` then silently skips that job
  - [ ] write a summary to `job_events`
- [ ] macOS `launchd` plist template + `applypilot schedule --install` / `--uninstall`,
      defaulting to hourly during working hours.
- [ ] `applypilot tick --dry-run` prints what it *would* do and changes nothing.
- [ ] `doctor` reports whether the schedule is installed and when it last ran.

## Acceptance criteria

- [ ] `tick` runs to completion with the dashboard closed
- [ ] Running it twice in a row produces no duplicate work and no duplicate notifications
- [ ] **It never sends anything.** Drafting and queueing only — sending stays a human click,
      consistent with the product's human-in-the-loop stance
- [ ] **It never starts an apply.** Co-pilot ends by handing a browser to a human, so an
      unattended apply would fill a form nobody is there to review — and starting one closes
      whatever review browser is already open (§Lessons 8)
- [ ] A crash in one step does not abort the rest, and is logged
- [ ] Uninstall fully removes the plist
- [ ] The badge clears when nothing needs attention, and never counts a stale row (a
      `ready_to_submit` whose browser is gone is not something you can act on)

## Risks / notes

- **The temptation is auto-send.** Don't. Every safeguard in `gmail_send.py` assumes a human
  initiated the action; unattended sending changes the product's whole risk profile and there
  is still no per-company cap — 5 contacts × 3 touches is 15 emails at one employer.
- Concurrency: `tick` and an open dashboard can run simultaneously. Rely on the existing
  atomic claims; do not add a lock file that can go stale.
- **`tick` must not touch `~/.applypilot/apply.pause`.** That flag is consumed by a running
  agent, and `main()` clears a stale one at start-up. A scheduler that wrote or cleared it
  would either pause a live application or un-pause one the operator paused deliberately.
- Keep it a real CLI command, not a daemon — `launchd` handles supervision, and a command is
  testable by hand.
- **`release_stale_locks()` in `tick` is a mitigation of open debt, not a fix.** The real cause
  is that applies run synchronously inside the HTTP request thread and die with the dashboard
  (§Known debt 1). Sweeping the wreckage on a timer is worth doing and is not the same as
  fixing it.

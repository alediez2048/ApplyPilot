# CRM-3 — Scheduler (make the system act unattended)

**Phase:** 2 · **Size:** S (~0.5d) · **Depends on:** ARCH-1 · **Status:** Todo
**PRD:** `architecture-prd.md` §4.1

## Problem

Nothing runs unless the dashboard is open. Every action needs a click. That caps the leverage
permanently — **the system can never surface something you did not think to ask for.**

The "alert me at 48 hours" behaviour only fires if you happen to be looking at the tab.

## Scope / tasks

- [ ] `applypilot tick` — one idempotent command, safe to run repeatedly:
  - [ ] poll for replies (CRM-1)
  - [ ] recompute follow-up due states
  - [ ] optionally draft follow-ups that came due, leaving them **queued, not sent**
  - [ ] write a summary to `job_events`
- [ ] macOS `launchd` plist template + `applypilot schedule --install` / `--uninstall`,
      defaulting to hourly during working hours.
- [ ] **Notification** when something needs attention: macOS notification via `osascript`,
      or a digest email to yourself. No new dependencies.
- [ ] `applypilot tick --dry-run` prints what it *would* do and changes nothing.
- [ ] `doctor` reports whether the schedule is installed and when it last ran.

## Acceptance criteria

- [ ] `tick` runs to completion with the dashboard closed
- [ ] Running it twice in a row produces no duplicate work and no duplicate notifications
- [ ] **It never sends anything.** Drafting and queueing only — sending stays a human click,
      consistent with the product's human-in-the-loop stance
- [ ] A crash in one step does not abort the rest, and is logged
- [ ] Uninstall fully removes the plist

## Risks / notes

- **The temptation is auto-send.** Don't. Every safeguard in `gmail_send.py` assumes a human
  initiated the action; unattended sending changes the product's whole risk profile and there
  is still no per-company cap.
- Concurrency: `tick` and an open dashboard can run simultaneously. Rely on the existing
  atomic claims; do not add a lock file that can go stale.
- Keep it a real CLI command, not a daemon — `launchd` handles supervision, and a command is
  testable by hand.

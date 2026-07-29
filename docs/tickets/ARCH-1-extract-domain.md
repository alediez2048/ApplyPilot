# ARCH-1 — Extract `applypilot/domain/`

**Phase:** 1 · **Size:** M (~1d) · **Depends on:** — · **Status:** ✅ Done (2026-07-28)
**PRD:** `architecture-prd.md` §Q1, ARCH-1 · **Blocks:** CRM-2, CRM-3, ARCH-3, ARCH-4
**Why now:** the only refactor the product work actually needs.

## Problem

`_job_checklist()` and `_followup_panel()` decide what work exists — the most
business-critical logic in the system — and both live in the HTTP server module next to CSS.

**The tell:** `tests/test_eval_resolution.py` has to `import web_dashboard` to test
*scheduling rules*. If testing a domain rule requires importing a web server, it isn't a
domain rule; it's a view helper that got promoted.

Three of one session's bugs came out of this file: a duplicated `rowMenu` that removed the
Re-apply button, a naive-timestamp crash that 500'd `/api/status` and blanked the dashboard,
and JS runtime errors that needed a bespoke DOM-stub harness to detect at all.

There are also **three separate implementations of "is it due yet"** (`_followup_after_days`,
`_followup_schedule`, `_li_followup_schedule`) that must agree and have no test forcing them to.

## Scope / tasks

- [ ] Create `src/applypilot/domain/`:
  - [ ] `types.py` — `TypedDict`s for Job, Contact, Touch, Step
  - [ ] `checklist.py` — move `_job_checklist`, `_followup_after_days`
  - [ ] `followup.py` — **one** ladder engine, channel-parameterised; absorbs
        `_followup_panel`, `_followup_schedule`, `_li_followup_schedule`
  - [ ] `verification.py` — re-home `networking/verify.py` unchanged
  - [ ] `time.py` — `parse_ts()` (the naive-timestamp guard), one implementation
- [ ] Rewrite the ladder as `due(anchor, schedule, count, now) -> DueState` so email and
      LinkedIn differ only by which schedule and which anchor field they pass.
- [ ] `web_dashboard.py` loads rows and calls `domain`; it computes nothing.
- [ ] Point `test_eval_resolution.py` at `domain`, not `web_dashboard`.
- [ ] Move the pure scheduling tests out of `test_dashboard_networking.py` into
      `tests/test_domain_followup.py`.

## Acceptance criteria

- [x] `domain/` imports nothing from `web_dashboard`, `http`, `sqlite3`, or `networking`
- [x] `domain/` functions take dicts and return dicts; no DB handles, no request objects
- [x] The three "is it due" implementations collapse to **one** (`touch_state`)
- [x] `test_eval_resolution.py` imports `applypilot.domain`, not the web server
- [x] No scheduling, checklist, or verification logic remains in `web_dashboard.py` —
      only two thin delegates
- [x] Tests green (228 → **245**); `evals/resolution.jsonl` green
- [x] **No behaviour change** — `/api/status` byte-identical on the live DB (105,446 bytes,
      7 jobs, 28 contacts)

**Superseded criterion:** the original "`web_dashboard.py` < 3,300 lines" was a badly chosen
metric — it conflated this ticket with ARCH-2. Of the 3,500 lines remaining, **1,602 are the
HTML/CSS/JS template**, which is ARCH-2's scope. ARCH-1 removed the 210 lines of domain logic
that actually existed. The meaningful number for this ticket is *what* is left, not how much:
no business rules remain in the web layer. The line target moves to ARCH-2, which should land
`web_dashboard.py` near 1,800.

## Risks / notes

- Pure moves plus one genuine merge (the two ladders). Keep the merge as its own commit so a
  regression bisects cleanly.
- `_followup_panel` currently mutates contacts in place (`followup_due`, `li_followup_state`).
  Preserve that contract or update both call sites together — the per-contact button and the
  panel must agree on who is due.

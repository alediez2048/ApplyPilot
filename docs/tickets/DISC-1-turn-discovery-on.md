# DISC-1 — Turn discovery on (make the funnel real)

**Phase:** 1 · **Size:** S (~0.5d) · **Depends on:** — · **Status:** Todo
**PRD:** `architecture-prd.md` §4.0, §4.1
**Why:** the best-built subsystem in the codebase has produced zero jobs.

## Problem

```
jobs entered via discovery     0
jobs entered by pasting a URL  7   ← all of them
```

There are ~2,500 lines of working, tested discovery — JobSpy across five boards, 48 Workday
portals via the CXS API, and an AI scraper for direct career sites. None of it is being used.
Every job in the system was pasted in by hand.

The result is a fully automated pipeline fed by a manual top-of-funnel. Six applications is a
pilot, not a job search — and no amount of downstream polish changes that.

**This is a configuration and trust problem, not a code problem.** The likely reasons it went
unused: `searches.yaml` was never tuned past the wizard defaults, and a bulk discovery run
produces low-relevance rows that feel like noise next to hand-picked URLs.

## Scope / tasks

- [ ] **Audit `~/.applypilot/searches.yaml`** — last edited Jun 8, never revisited. Rewrite
      against the *current* target role, not the one from two months ago.
- [ ] **Calibrate scoring before volume.** Run `discover` + `score` on a throwaway DB first and
      eyeball the 1–10 spread. If everything lands 7–10 the score is useless as a filter and
      volume will just be noise. Tune the scorer prompt until the distribution separates.
- [ ] **Set a relevance floor** — `DISCOVERY_MIN_SCORE` (default 7). Rows below it stay in the
      DB but are hidden behind a dashboard filter rather than deleted.
- [ ] **Dedupe against applied/rejected** — never re-surface a job already applied to or
      rejected. (`url` is the PK, so this is a filter, not a schema change.)
- [ ] **Dashboard: a "Discovered" bucket** distinct from jobs you added, so an automated find
      never silently mixes into your curated list. Explicit "keep / reject" per row.
- [ ] **Run it for real** — one `applypilot run discover enrich score` and triage the output
      together. This ticket is not done until a discovered job has been applied to.
- [ ] **Workday employers** — review `config/employers.yaml` (~48 portals) and cut the ones
      that are irrelevant to the target role.

## Acceptance criteria

- [ ] `searches.yaml` reflects the role currently being targeted
- [ ] `discover` yields ≥ 20 jobs in one run, and ≥ 5 score at or above the floor
- [ ] The score distribution is not degenerate — not everything in one band
- [ ] Discovered jobs land in their own dashboard bucket, never mixed with pasted ones
- [ ] Already-applied and rejected URLs never reappear
- [ ] **At least one discovered job has an application submitted** — the real test

## Risks / notes

- **Volume without precision is worse than nothing.** 50 bad jobs cost real time to triage and
  will burn Apollo credits if contacts get found for them. The relevance floor is the guard.
- Don't run contact discovery automatically on discovered jobs — Apollo credits are per-reveal,
  and a low-relevance job doesn't warrant them. Keep "Find contacts" a deliberate click.
- `python-jobspy` pins an incompatible numpy and is installed `--no-deps`; verify it still
  imports before assuming a dry run is meaningful.

# ARCH-2 — Frontend to static files

**Phase:** 3 · **Size:** M (~1d) · **Depends on:** — · **Status:** Todo
**PRD:** `architecture-prd.md` §Q3, ARCH-2

## Problem

996 lines of JavaScript and 519 of CSS live inside Python strings. `serve_dashboard()` is a
**1,633-line function**, almost all of it template.

No linter, no type checker, no module boundaries, no dead-code detection, no browser caching,
and stack traces point at a generated blob. Two tests exist *solely* to compensate:
`test_dashboard_js_valid.py` (does it parse?) and `test_dashboard_render.py` (does it throw?).
A `.js` file gives the first for free.

## Scope / tasks

- [ ] `src/applypilot/static/` — `index.html`, `dashboard.css`, `dashboard.js`
- [ ] Serve them from the existing server; `?v=<pkg version>` cache-buster so a stale bundle
      can't survive an upgrade
- [ ] Ship the files with the package (Hatchling `force-include` or package data) — verify
      the console script still works from a clean install, not just from the repo
- [ ] Replace the handful of Python→JS interpolations with a `/api/config` call or a
      `<script type="application/json">` block
- [ ] Add ESLint (dev-only dependency, not runtime) with a minimal config
- [ ] Retire `test_dashboard_js_valid.py`; **keep** `test_dashboard_render.py` — it tests
      behaviour, not syntax, and has already caught a real regression
- [ ] Split `dashboard.js` into modules only if it stays under ~5 files; do not add a bundler

## Acceptance criteria

- [ ] Zero JS/CSS inside any `.py` file
- [ ] `web_dashboard.py` < 1,800 lines
- [ ] ESLint passes; unused functions are reported rather than accumulating silently
- [ ] `test_dashboard_render.py` still passes against the extracted file
- [ ] A fresh `pip install .` in a clean venv serves the dashboard correctly
- [ ] No new **runtime** dependency (8 stays 8)

## Risks / notes

- Mechanical but wide. Do it in one sitting; a half-migrated template is worse than either end.
- Cache: without the `?v=` buster you will chase phantom bugs after every edit.
- Keep the no-bundler, no-framework stance — that is a §5 non-goal, not an oversight.

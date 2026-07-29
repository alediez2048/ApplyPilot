# ARCH-2 — Frontend to static files

**Phase:** 3 · **Size:** M (~1d) · **Depends on:** — · **Status:** ✅ Done (2026-07-28)
**PRD:** `architecture-prd.md` §Q3, ARCH-2

## Problem

996 lines of JavaScript and 519 of CSS live inside Python strings. `serve_dashboard()` is a
**1,633-line function**, almost all of it template.

No linter, no type checker, no module boundaries, no dead-code detection, no browser caching,
and stack traces point at a generated blob. Two tests exist *solely* to compensate:
`test_dashboard_js_valid.py` (does it parse?) and `test_dashboard_render.py` (does it throw?).
A `.js` file gives the first for free.

## Scope / tasks

- [x] `src/applypilot/static/` — `index.html` (86), `dashboard.css` (518), `dashboard.js` (965)
- [x] Serve them from the existing server; `?v=` cache-buster so a stale bundle
      can't survive an upgrade
- [x] Ship the files with the package — verified by serving from a clean-venv `pip install .`,
      not just from the repo
- [x] Replace the handful of Python→JS interpolations — there were **none**. `_INDEX_HTML`
      was an `r"""…"""`, so no `/api/config` endpoint was needed; the only substitution is
      the `__ASSET_V__` cache-buster token.
- [x] Add ESLint (dev-only dependency, not runtime) with a minimal config
- [x] Retire `test_dashboard_js_valid.py`; **keep** `test_dashboard_render.py`
- [x] Split `dashboard.js` into modules only if it stays under ~5 files; do not add a bundler
      — **not split.** One classic script, because ~56 inline `onclick=` attributes resolve
      against the global object and `type="module"` would break every one of them silently.

## Acceptance criteria

- [x] Zero JS/CSS inside any `.py` file — pinned by `test_no_javascript_or_css_left_in_python`
- [ ] ~~`web_dashboard.py` < 1,800 lines~~ — **superseded; the number was wrong.**
      Actual: **1,953**. The estimate assumed the template was the only thing over the line,
      but CLAUDE.md had already measured the Python half at **1,898** before this ticket
      started, and the static-serving layer adds ~55. Extraction removed 1,602 lines of
      template and could not have hit 1,800 by arithmetic. The remaining Python contains
      ~430 lines of pipeline orchestration (`run_dashboard_prepare/apply/fill_one/restart/
      continue`) that are not HTTP concerns — that is **ARCH-4**'s scope, not this one.
      Not fudged, not quietly dropped.
- [x] ESLint passes; unused functions are reported rather than accumulating silently
      — ESLint found 7 issues, all fixed. Attribute-referenced functions are invisible to
      ESLint, so `test_no_dead_functions` reads the HTML too; it found **5 dead functions
      and 3 dead Sets** left over from the ARCH-1 tabs restructure, all now deleted.
- [x] `test_dashboard_render.py` still passes against the extracted file
- [x] A fresh `pip install .` in a clean venv serves the dashboard correctly
- [x] No new **runtime** dependency (8 stays 8)

## Verification

Reassembling `index.html` + `dashboard.css` + `dashboard.js` reproduces the previous
`_INDEX_HTML` **byte for byte** — the extraction is provably lossless, so anything that
broke afterwards was introduced by the serving layer, not the cut. Live probes against a
clean-venv install: index `200 no-store`, both assets `200 immutable` on the versioned URL,
traversal and unknown names `404`. 255 tests pass; ruff and ESLint clean.

## Risks / notes

- Mechanical but wide. Do it in one sitting; a half-migrated template is worse than either end.
- Cache: without the `?v=` buster you will chase phantom bugs after every edit.
- Keep the no-bundler, no-framework stance — that is a §5 non-goal, not an oversight.

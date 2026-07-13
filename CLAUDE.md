# ApplyPilot — Codebase Index

AI-powered, end-to-end autonomous job-application pipeline. Discovers jobs across
many boards, scores them against your resume with an LLM, tailors a resume and
cover letter per job, and then drives a real Chrome browser via Claude Code to
submit applications hands-free.

- **Language / runtime:** Python ≥ 3.11
- **Packaging:** Hatchling, `src/` layout, single package `applypilot`
- **Entry point:** `applypilot = "applypilot.cli:app"` (Typer CLI)
- **License:** AGPL-3.0-only
- **Version:** 0.4.0 (`pyproject.toml`)

## Quick orientation

The whole app is a **6-stage data pipeline over a single SQLite `jobs` table**.
Each stage reads rows at one state, does work, and writes columns that advance
the row to the next state. Stages are independent and idempotent — any stage can
run alone. A separate **apply** subsystem consumes fully-prepared rows.

```
discover → enrich → score → tailor → cover → pdf →  [apply]
```

Two run surfaces:
- `applypilot run [stages...]` — the prep pipeline (stages 1–6), sequential or `--stream`.
- `applypilot apply` — autonomous browser submission (separate command, Tier 3).

## Tiers (feature gating — `config.py`)

Detected at runtime from installed deps / env; gates commands via `check_tier()`.
- **Tier 1 — Discovery:** Python only. `init`, `run discover/enrich`, `status`, `dashboard`.
- **Tier 2 — AI Scoring & Tailoring:** + LLM API key. `run score/tailor/cover/pdf`.
- **Tier 3 — Full Auto-Apply:** + Claude Code CLI + Chrome + Node.js. `apply`.

## Source map (`src/applypilot/`)

| Path | Role |
|------|------|
| `cli.py` | **Typer CLI** — all commands: `init`, `run`, `apply`, `status`, `dashboard`, `doctor`. Bootstraps env/dirs/DB, validates args, gates tiers, dispatches. |
| `pipeline.py` | **Orchestrator** for `run`. Defines stage order + upstream deps, sequential and streaming (thread-per-stage, DB-as-conveyor-belt) runners. |
| `config.py` | Paths (`~/.applypilot/`), Chrome auto-detection, profile/YAML loaders, `DEFAULTS`, and the **tier system** (`get_tier`, `check_tier`). |
| `database.py` | SQLite layer. **Single `jobs` table** = source of truth. Thread-local WAL connections, forward-only column migrations, `get_stats`, `store_jobs`, `get_jobs_by_stage`. |
| `llm.py` | Unified LLM client. Auto-detects provider from env (Gemini → OpenAI → local), retries w/ backoff for rate limits. OpenAI-compatible + native Gemini endpoints. |
| `view.py` | Generates a self-contained static **HTML results dashboard**. |
| `web_dashboard.py` | Localhost-only **interactive operator dashboard** HTTP server (`dashboard --serve`): application tracker + URL import. |
| `__main__.py` | `python -m applypilot` shim. |

### `discovery/` — Stage 1 (populates rows)
| File | Role |
|------|------|
| `jobspy.py` | Scrapes Indeed/LinkedIn/Glassdoor/ZipRecruiter/Google Jobs via `python-jobspy`. Dedup + salary parse. |
| `workday.py` | Workday ATS scraper via undocumented CXS JSON API (no browser/LLM). Employer list in `config/employers.yaml`. |
| `smartextract.py` | AI-powered generic scraper. Phase 1 gathers page intel (JSON-LD, API responses, data-testids); LLM picks an extraction strategy; Phase 2 extracts. Sites in `config/sites.yaml`. |

### `enrichment/` — Stage 2
| File | Role |
|------|------|
| `detail.py` | Visits each job URL, extracts `full_description` + `application_url`. 3-tier cascade: JSON-LD → CSS selectors → AI extraction. |

### `scoring/` — Stages 3–6 (all LLM/profile-driven, no hardcoded PII)
| File | Role |
|------|------|
| `scorer.py` | Stage 3 — rates each job `fit_score` 1–10 vs resume/profile. |
| `tailor.py` | Stage 4 — rewrites resume per job (reorder/emphasize/keyword), preserves `resume_facts`, never fabricates. |
| `cover_letter.py` | Stage 5 — targeted cover letter per job. |
| `validator.py` | Shared validation: banned words, fabrication detection, structural checks. Profile-driven. |
| `resume_render.py` | Maps tailor JSON + profile → RenderRequest and drives the bundled React-PDF Node renderer (`resume_renderer/`). Materializes a writable runtime (`~/.applypilot/resume_renderer_runtime/`) + `npm install` on first use. |
| `pdf.py` | Stage 6 — renders tailored resume → PDF. Prefers React-PDF via `resume_render`; falls back to a headless-Chromium HTML template (Playwright) when Node is absent. |

**`resume_renderer/`** (Node, no build step): headless `@react-pdf/renderer` templates —
resume (`document.mjs` + theme `styles.mjs` + content-density one-page fitter `onePage.mjs`)
and cover letter (`cover.mjs`, classic Times business letter) — driven by `render.mjs`
(`node render.mjs <request.json> <out.pdf>`, dispatched on `options.kind`). Ported from the
Resume Formatting Tool. `node_modules` is git-ignored and installed at runtime. See
`docs/resume-renderer-plan.md`.

### `apply/` — autonomous submission (Tier 3, `applypilot apply`)
| File | Role |
|------|------|
| `launcher.py` | **Main apply entry.** Atomically acquires jobs from DB, spawns Chrome + Claude Code per job, parallel workers, parses results, updates DB. Also utility modes (`mark_applied/failed`, `reset_failed`, `gen`). |
| `chrome.py` | Chrome lifecycle: isolated instance w/ remote debugging (CDP), per-worker profile clone, cross-platform process cleanup. |
| `prompt.py` | Builds the instruction prompt telling the AI agent how to fill the form via **Playwright MCP** tools. All PII from profile. |
| `dashboard.py` | Rich live terminal dashboard of worker/job status during apply. |

Apply drives Claude Code (`claude` CLI) with an auto-generated MCP config
(Playwright MCP over a per-worker CDP port + Gmail MCP). No manual MCP setup.

### `wizard/`
| File | Role |
|------|------|
| `init.py` | First-time `init` wizard: creates `~/.applypilot/` with `resume.txt`, `profile.json`, `searches.yaml`, `.env`. |

## Data / config locations

**User data → `~/.applypilot/`** (override with `APPLYPILOT_DIR`):
`applypilot.db`, `profile.json`, `resume.txt`/`.pdf`, `searches.yaml`, `.env`,
`tailored_resumes/`, `cover_letters/`, `logs/`, `chrome-workers/`, `apply-workers/`.

**Package-shipped config → `src/applypilot/config/`:**
- `employers.yaml` — Workday employer registry (~48 portals)
- `sites.yaml` — direct career sites, blocked sites/patterns, base URLs, manual-ATS domains
- `searches.example.yaml` — example search config (fallback if user has none)

**Env vars** (`.env.example`): `GEMINI_API_KEY` / `OPENAI_API_KEY` / `LLM_URL`,
`LLM_MODEL`, `CAPSOLVER_API_KEY` (optional CAPTCHA), `PROXY`, `CHROME_PATH`.

## The `jobs` table (state machine)

One row per job URL (`url` PRIMARY KEY). Columns are grouped by the stage that
writes them — this grouping *is* the pipeline state:
- **discover:** `title, salary, description, location, site, strategy, discovered_at`
- **enrich:** `full_description, application_url, detail_scraped_at, detail_error`
- **score:** `fit_score, score_reasoning, scored_at`
- **tailor:** `tailored_resume_path, tailored_at, tailor_attempts`
- **cover:** `cover_letter_path, cover_letter_at, cover_attempts`
- **apply:** `applied_at, apply_status, apply_error, apply_attempts, agent_id, last_attempted_at, apply_duration_ms, apply_task_id, verification_confidence`

`database._ALL_COLUMNS` is the single source of truth; adding a key there
auto-migrates old DBs. Retry caps: tailor/cover ≤ 5 attempts, apply ≤ 3.

## Common commands

```bash
applypilot init                 # setup wizard
applypilot doctor               # diagnose deps/keys, show current tier
applypilot run                  # full prep pipeline (discover→pdf), sequential
applypilot run -w 4 --stream    # parallel discovery/enrich + concurrent stages
applypilot run score tailor cover
applypilot apply -w 3           # autonomous submit, 3 Chrome workers
applypilot apply --dry-run      # fill forms without submitting
applypilot status               # DB stats table
applypilot dashboard --serve    # interactive local operator dashboard
```

## Dev

- Deps: `typer, rich, httpx, beautifulsoup4, playwright, python-dotenv, pyyaml, pandas`.
  `python-jobspy` installed separately (`--no-deps`) — pins an incompatible numpy.
- Dev extras: `pytest`, `ruff` (line-length 120, target py311). No test suite present in-tree yet.
- CI: `.github/workflows/ci.yml`, publish: `publish.yml`.
- See `CONTRIBUTING.md`, `CHANGELOG.md`.
```

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
| `database.py` | SQLite layer. Owns **`jobs`** (the pipeline state machine) + **`job_events`** (activity log). Thread-local WAL connections, forward-only column migrations, `get_stats`, `store_jobs`, `get_jobs_by_stage`. `networking/store.py` owns two more tables — see the schema section. |
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

### `networking/` — contacts & outreach (`applypilot network`) — details in §3, §11
| File | Role |
|------|------|
| `service.py` | Orchestrator: find → rank → draft, per job. Owns the COLD (Apollo) + HOT (your connections) layers. |
| `store.py` | The **`contacts` table** — own migration (`_CONTACT_COLUMNS`), NOT in `database._ALL_COLUMNS`. Atomic claims for send/DM. |
| `providers.py` / `apollo.py` | Contact discovery. Apollo is the **sole** provider (paid plan required). |
| `derive.py` / `rank.py` | Recover the real employer domain from a job row; pick 3–5 people (peers + recruiter). |
| `connections.py` | Offline LinkedIn `Connections.csv` import → warm/"🤝 connection" flagging. |
| `outreach.py` / `prompt.py` | One LLM call → email {subject, body} + a ≤300-char LinkedIn note. |
| `gmail_send.py` / `gmail_oauth.py` | Send via OAuth (preferred) or SMTP, with résumé + cover letter + intro-deck attachments and send safeguards. |
| `linkedin_dm.py` / `dm_prompt.py` | Dormant CLI-only **compose** helpers (auto-send abandoned — §7). |
| `linkedin_agent.py` | Opt-in, read-only LinkedIn augmentation. Never sends. |

### `wizard/`
| File | Role |
|------|------|
| `init.py` | First-time `init` wizard: creates `~/.applypilot/` with `resume.txt`, `profile.json`, `searches.yaml`, `.env`. |

### `extension/` (repo root, not a Python package)
MV3 Chrome extension **"ApplyPilot Contacts"** — a popup only. Pulls the outreach queue from the local
dashboard, copies a drafted note, opens the LinkedIn profile. Never touches a LinkedIn page. See §8.

## Data / config locations

**User data → `~/.applypilot/`** (override with `APPLYPILOT_DIR`):
`applypilot.db`, `profile.json`, `resume.txt`/`.pdf`, `searches.yaml`, `.env`,
`tailored_resumes/`, `cover_letters/`, `logs/`, `chrome-workers/`, `apply-workers/`.
Also present at runtime: `ext_token` (extension shared secret), `gmail_oauth_client.json` +
`gmail_token.json` (outreach send), `intro_deck.pdf` (auto-attached), `linkedin-dm-profile/`
(isolated browser profile), `.linkedin_dm_consent`, `resume_renderer_runtime/` (npm-installed).
**None of this is in git** — the repo carries no secrets.

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

Plus **`rejected_at`** — set when you mark a job rejected (the "rejected pile", §10).

`database._ALL_COLUMNS` is the single source of truth **for `jobs`**; adding a key there
auto-migrates old DBs. Retry caps: tailor/cover ≤ 5 attempts, apply ≤ 3.

### The other three tables (NOT in `_ALL_COLUMNS` — each migrates itself)

`jobs` is still the pipeline's state machine, but the DB now holds **four** tables:

| Table | Owner | Purpose |
|-------|-------|---------|
| `jobs` | `database.py` | The 6-stage pipeline state machine (above). |
| `contacts` | `networking/store.py` | People found per job + drafted outreach + send/DM status. `_CONTACT_COLUMNS` is its source of truth. |
| `connections` | `networking/connections.py` | Your imported LinkedIn `Connections.csv` (name/company normalized for matching). |
| `job_events` | `database.py` | Per-job **activity log** — one row per lifecycle event (`stage`, `status`, `detail`, `ts`). Append is best-effort and never raises into the pipeline. |

Live counts on the dev machine (2026-07-27): jobs 5, contacts 19, connections 899, job_events 20.

## Common commands

```bash
applypilot init                 # setup wizard
applypilot doctor               # diagnose deps/keys, show current tier
applypilot run                  # full prep pipeline (discover→pdf), sequential
applypilot run -w 4 --stream    # parallel discovery/enrich + concurrent stages
applypilot run score tailor cover
applypilot apply -w 3           # autonomous submit, 3 Chrome workers
applypilot apply --dry-run      # fill forms without submitting
applypilot apply --copilot      # fill everything, then STOP — you review + submit (see §9)
applypilot apply --url <u> --copilot --resume   # reconnect to a paused co-pilot browser
applypilot status               # DB stats table
applypilot dashboard --serve    # interactive local operator dashboard → :8765
applypilot doctor               # deps/keys/tier + contact-provider + Gmail + DM readiness

# networking / outreach
applypilot network                          # find contacts for jobs + draft outreach
applypilot network --import-connections <csv>   # import LinkedIn Connections.csv
applypilot network --dm-list                # contacts with a drafted LinkedIn note
applypilot network --compose-dm --dm-contact <id>   # compose invite note; YOU click Send
```

## Dev

- Deps: `typer, rich, httpx, beautifulsoup4, playwright, python-dotenv, pyyaml, pandas`.
  `python-jobspy` installed separately (`--no-deps`) — pins an incompatible numpy.
- Dev extras: `pytest`, `ruff` (line-length 120, target py311). No test suite present in-tree yet.
- CI: `.github/workflows/ci.yml`, publish: `publish.yml`.
- See `CONTRIBUTING.md`, `CHANGELOG.md`.
```

---

# Session developments (current state)

Everything below was built on top of the original index above. All committed to
`main` and pushed to `github.com/alediez2048/ApplyPilot`. **Nothing sensitive is in
git** — all secrets/config/DB live in `~/.applypilot/` (outside the repo).

## 1. Resume/cover-letter rendering — React-PDF (replaced the old HTML/Chromium path)

- **`src/applypilot/resume_renderer/`** (Node, no build step): headless `@react-pdf/renderer`
  templates — resume (`document.mjs` + `styles.mjs` theme + `onePage.mjs` density fitter) and
  cover letter (`cover.mjs`). Driven by `render.mjs` (`node render.mjs <request.json> <out.pdf>`).
  `node_modules` git-ignored, `npm install`ed at runtime into `~/.applypilot/resume_renderer_runtime/`.
- **`scoring/resume_render.py`** maps tailor JSON + profile → RenderRequest and shells out to Node.
- **`scoring/pdf.py`** prefers React-PDF; falls back to the old Chromium HTML template if Node absent.
  The tailor stage now persists `*_DATA.json` (structured) so the renderer skips the lossy text re-parse.
- **Theme = the user's real reference resume** (`~/Downloads/Technical SEO Manager Resume-2.pdf`):
  Times New Roman, bold centered name, blue "–"-separated contact links, no rules, small dense fonts,
  1" margins. Cover letter shares the résumé header exactly. Both verified 1-page on real data.

## 2. Pipeline hardening

- **ATS API enrichment** — `enrichment/ats.py`: Tier-0 fetch of full JD via Greenhouse/Lever/Ashby
  public APIs before any browser scrape (fixed the Affirm greenhouse URL that returned "no data").
- **Multi-provider LLM** — `llm.py` rewritten as round-robin + failover over OPENAI/GEMINI/
  ANTHROPIC/LLM_URL. `LLM_PROVIDER_ORDER` overrides. Claude via Anthropic OpenAI-compat endpoint
  (default `claude-haiku-4-5`). Fixes single-provider 429 stalls.

## 3. Networking & outreach epic (NET-1..5) — LIVE

New subsystem **`src/applypilot/networking/`** + a `contacts` table (own migration in `store.py`,
NOT `_ALL_COLUMNS`). Full cycle: **find people → show in dashboard → draft email + LinkedIn note → send**.

- **Contact discovery** — `providers.py` registry picks **Hunter.io** (preferred, free-tier API) or
  Apollo. `hunter.py` Domain Search returns people + verified emails + titles + LinkedIn in one call.
  `apollo.py` kept (needs PAID plan — free tier 403s; `probe()` is honest about it). `derive.py`
  recovers the real employer/domain (pipeline stores job-board name in `site`, not the company).
  `rank.py` picks 3–5 (peers + a recruiter/hiring contact). Gated by `require_contacts_provider`.
- **Dashboard** (`web_dashboard.py`) — "People at {company}" panel; "Find contacts" button →
  `NetworkRunner` keyed background tasks (by job_url); Origin/CSRF guard on state-changing POSTs.
- **Outreach drafting** — `outreach.py`: one LLM call → email {subject, body} **+** a LinkedIn note
  (≤300 chars, hard-capped). Editable in dashboard (Save/Regenerate/Copy per channel).
- **Gmail send** — `gmail_send.py` (transport = OAuth preferred, else SMTP) with safeguards: atomic
  claim (no double-send), verified-email gate, daily cap, cross-job dedupe, dry-run. `gmail_oauth.py`
  = self-contained send-only OAuth (no third party). Footer removed — sends verbatim.
  `OUTREACH_FROM_ADDRESS` can override the From (unused; defaults to connected account).
- **LinkedIn read-only fallback** — `linkedin_agent.py` (NET-5): opt-in, off by default, tool-enforced
  read-only, consent gate, daily cap. Augments Apollo/Hunter when coverage is thin. Does NOT send.
- **LinkedIn connections** — `connections.py`: import LinkedIn's Connections.csv
  (`network --import-connections`), match found contacts → green "🤝 Connection / Connection here"
  badge + "you have N connections here" hint. Offline, no scraping. Live-computed per dashboard load.

## 4. Security fix

- The autonomous **apply agent is now tool-scoped** (`apply/launcher.py`): `--allowedTools
  mcp__playwright,mcp__gmail__send_email` + a hard `--disallowedTools` deny-list (Bash/Read/Write/
  WebFetch/etc.). Blast radius of a prompt-injection on a malicious careers page dropped from
  "arbitrary code exec + secret exfiltration" to "drive the browser / send an email."

## 5. LinkedIn DM auto-send (LDM) — **SUPERSEDED — see §7 (abandoned) + §8 (extension). Historical below.**

**`networking/linkedin_dm.py`** + `dm_prompt.py` drive the installed **agent-browser** binary
(`~/.local/bin/agent-browser`, now v0.32.1) as a subprocess. Repos stay SEPARATE — ApplyPilot shells
out to the CLI (like claude/npx/Chrome). **Auto-send was abandoned (§7); these modules survive only as
CLI-only helpers for the COMPOSE path** (fill the invite dialog, human clicks Send).

- **Mechanism:** agent-browser's deterministic CLI (`open --profile`, `snapshot`, `keyboard inserttext`,
  `click`) keeps ONE persistent browser session across calls. Controller loop = `snapshot → LLM picks ONE
  action → execute` over a fixed action set (`dm_prompt.ACTIONS`). The note is inserted **VERBATIM** —
  the model never supplies text, so prompt-injection can't change what you say.
- **Delivery paths:** A — Connect → Add a note → Send invitation (most contacts aren't connections);
  B — Message composer (already connected / open profile). Aborts on any InMail/Premium paywall.
- **Safeguards** (`linkedin_dm.send()`): off by default (`NETWORKING_LINKEDIN_DM=0`), one-time consent
  file (`.linkedin_dm_consent`), login precheck, isolated profile (`~/.applypilot/linkedin-dm-profile`),
  daily cap (`LINKEDIN_DM_DAILY_LIMIT=5`), 30-day dedupe on normalized `linkedin_url`, atomic claim
  (`claim_dm_send`). DB cols `dm_status/dm_sent_at/dm_error` (own migration in `store.py`).
- **CLI today:** `network --dm-list`, `--compose-dm --dm-contact <id>` (composes, leaves it open for YOU
  to click Send). `--send-dm` is now just an **alias of `--compose-dm`** — auto-send is disabled.
  `--dm-login` does the one-time consent + headed login. Tests: `tests/test_linkedin_dm.py` (13).

## 6. Pipeline overhaul (committed `8da452e`) — Apollo, aggressive tailoring, email attachments, apply fixes

- **Apollo is the SOLE contact provider; Hunter fully removed.** `hunter.py` + its test deleted;
  `providers.py` is Apollo-only. Apollo needs a **paid plan** (Basic $49/mo+) for API access — the
  user upgraded (free tier 403s the people-search API). Apollo's title/department targeting surfaces
  the *right* people (recruiters/hiring managers) vs. Hunter's "whoever has an email." `derive.py` now
  strips careers-portal subdomains (`careers.amd.com → amd.com`). `NETWORKING_PROVIDER` no longer needed.
- **Aggressive JD-matching tailoring** (opt-in `TAILOR_AGGRESSIVE=1`, LIVE): `tailor.py`
  `_build_aggressive_tailor_prompt` mirrors the JD's skills/keywords into the resume + skips the
  fabrication judge (forces `lenient`). Preserves real employers/school/degrees (background-checkable);
  everything else matches the JD. Cover letter honors the same mode. **User explicitly chose this over
  honest tailoring** ("I care about getting an interview").
- **Email unified to Gmail:** profile `personal.email` → `jorgealejandrodiezm@gmail.com`; applications
  AND outreach now both use it (was applying under `.edu`).
- **Email attachments:** `gmail_send.py` attaches the job's tailored **résumé + cover letter PDFs**
  (recruiter-friendly filenames) to every outreach email. `OUTREACH_ATTACH_DOCS` toggle. Wired through
  both OAuth + SMTP.
- **Apply fixes:** (a) **dry-run was broken** — the agent submitted real applications; now the dry-run
  banner dominates the whole prompt + a hard launcher safety-net (a dry-run can never record "applied";
  agent-submit-during-dry-run = flagged violation). (b) **résumé upload `file_access_denied`** — stage
  the PDF into the worker dir (Playwright's cwd), reset-before-stage. (c) `--strict-mcp-config` so the
  apply agent uses ONLY ApplyPilot's real-Chrome Playwright, not a globally-registered agent-browser MCP
  (which got 403-blocked at AMD). **BetterUp applied successfully via Ashby** (real submit).

## 7. LinkedIn auto-send — ABANDONED (the long saga); external browser automation does NOT work

Spent heavily trying to auto-send LinkedIn connection notes via `linkedin_dm.py` (agent-browser CLI
driving real Chrome). **Verdict: fully-automated LinkedIn sending is not reliably achievable.** Proven
empirically: the a11y snapshot misses LinkedIn's React modals; synthetic `.click()` doesn't fire React
handlers; sessions hang; every profile's DOM varies; and LinkedIn soft-blocks/delays automated sends
(some invites landed — Kumar, Michael, Sage showed Pending — many silently didn't). **The dashboard
LinkedIn auto-send/compose buttons were REMOVED.** `linkedin_dm.py`/`dm_prompt.py` remain as dormant
CLI-only helpers. Interim UX: a **"Copy note + open LinkedIn"** button per contact (client-side, zero
risk). Key lesson: **driving LinkedIn from OUTSIDE the browser is the wrong architecture** → led to §8.

## 8. Chrome extension "ApplyPilot Contacts" — REBUILT as a dead-simple contact list (`d4288a5`)

The first extension (EXT-0..5) was a full MV3 auto-composer: background service worker + content script
that drove the LinkedIn DOM to fill the invite dialog. **It was deleted.** Live debugging never got past
LinkedIn's CSP + React DOM churn (same root cause as §7). Commit `d4288a5` removed `background.js`,
`content.js`, `selectors.json`, and `shared/*` — ~2,900 lines gone.

**What exists now** (`extension/`, MV3, no build step) is a **popup and nothing else**:
- `manifest.json` — permissions are just `storage` + `host_permissions: http://localhost:8765/*`.
  **No LinkedIn host permission, no content scripts, no background worker** — it is structurally
  incapable of touching a LinkedIn page.
- `popup.html` / `popup.css` / `popup.js` — the entire UI + logic. Fetches `GET /api/ext/queue` from the
  local dashboard (host_permissions bypasses CORS), groups contacts by company, and gives each an
  editable note, **Copy note**, and **Open LinkedIn ↗**. All text inserted via `textContent`, never
  `innerHTML`. `popup.js` also reads the OLD `extToken` storage key so the token carried over.
- The loop is manual by design: copy → open their profile → paste → **you** send.

**Local API (EXT-0) survives unchanged** and is what the popup talks to: `GET /api/ext/queue[?job_url]`,
`POST /api/ext/status`, `POST /api/ext/note` on `:8765`, guarded by a shared token at
`~/.applypilot/ext_token` (generated on dashboard startup, printed to console; paste into the popup).
Tests: `tests/test_ext_api.py` + `tests/test_ext_queue_urls.py`. **Verified live this session** — the
endpoint 401s without the token and returns the contact list with it.

Historical docs (describe the DELETED auto-composer, keep for rationale only):
`docs/chrome-extension-prd.md`, `docs/chrome-extension-review.md` (51-agent adversarial review),
`docs/tickets/EXT-0..6`. Two real bugs that review found still live in the app: `_DM_DONE_STATUSES =
{sent,manual,skipped}` gating queue eligibility, and `mark_dm_sent` stamping `dm_sent_at`.

## 9. Co-pilot apply mode — the answer to "don't let the agent submit"

`applypilot apply --copilot` (`cli.py:153`): the agent fills the **entire** application, then **STOPS**
and leaves the browser open for you to review and submit yourself. It never auto-submits — the launcher
treats an agent submit during co-pilot as a hard violation (`failed:copilot_violation_agent_submitted`,
`launcher.py:571`). This is the practical middle ground between `--dry-run` (throwaway) and full auto.

- **Chrome keep-alive contract:** a co-pilot browser must survive ALL cleanup paths — per-worker cleanup,
  `kill_all_chrome`, and the `atexit` handler. `chrome.py` tracks `_keep_alive_ports`; pinned by
  `tests/test_copilot_keepalive.py`. A kept-alive worker is never reaped; normal workers still are.
- **`--resume`** (`cli.py:154`): reconnect to a still-open co-pilot browser that paused on a blocker and
  continue from the current state. Use with `--url --copilot`.
- Dashboard exposes this per row as **"Fill application"** for ready jobs (`6c906b3`).
- `bf96e7d` fixed the activity log wrongly recording a co-pilot success as "Apply failed".

## 10. Operator dashboard — application-tracker features

`web_dashboard.py` grew a real tracking UI (it is by far the largest churn surface in recent commits):
- **Per-job activity log** — `database.py` logs one row per lifecycle event (enrich/score/apply/...);
  `log_activity` is best-effort and never raises into the pipeline. Rendered as a per-row dropdown that
  stays open across auto-refresh (`0d4813d`).
- **Rejected pile** — `rejected_at` column (`database.py:234`); mark/restore a job as rejected.
- **Filter pills** — status buckets across the application list.
- **Restart end-to-end** — re-run a job from scratch when an application didn't go through; also offered
  on already-applied jobs behind a stronger confirm.
- **People (contacts) toggle** co-located with Activity in the job footer.

## 11. Outreach upgrades

- **Hot-contacts layer** (`8acad38`) — surfaces your existing 1st-degree LinkedIn connections at the
  target company as a separate WARM layer alongside the cold Apollo results. `service._find_hot_contacts`
  enriches them via Apollo identity match, stores them as `source='connection'`, and drafts *warmer*
  copy (reconnect email + a DM to a known connection). Badged "🤝 connection — you already know them".
- **No-email contacts still get materials** (`8f0fd4d`) — a LinkedIn note is drafted and shown even when
  no verified email exists, so the manual path is never empty-handed.
- **Schedule-a-call CTA + intro deck** (`39dcb7c`) — outreach emails can carry a call-scheduling CTA and
  auto-attach `~/.applypilot/intro_deck.pdf` (`INTRO_DECK_PATH`, `OUTREACH_ATTACH_DECK=1`).
- **Send dedupe by `sent_message_id`** (`e93ef92`) — regenerating a draft no longer re-sends.
- **Tailoring rule** (`f0d5e83`) — never list Gauntlet AI (a bootcamp) as work experience.

## Current environment state (the user's machine)

- **Contacts: Apollo.io (PAID Basic plan), sole provider.** Hunter removed. Apollo surfaces recruiters/
  hiring managers by title. (Legacy Affirm contacts are old Hunter data with junk/dead URLs.)
- **Gmail: connected via OAuth**, sends from **jorgealejandrodiezm@gmail.com** (both apps + outreach).
  Outreach emails carry résumé + cover letter PDF attachments.
- **`TAILOR_AGGRESSIVE=1`** set — resumes aggressively match the JD (fabrication guard off).
- **899 LinkedIn connections imported** (`doctor` confirms warm-contact flagging is on).
- **Current DB (verified this session):** 5 jobs, all scored/tailored/covered — **4 applied, 1 ready to
  apply, 0 apply errors**. Sources: webAI, SpaceX, DevRev, BetterUp, Affirm. Scores: 4× 10/10, 1× 4/10
  (the earlier "everything scores 4/10" calibration complaint appears resolved).
- **16 contacts queued for outreach** across webAI / SpaceX / DevRev, each with a drafted LinkedIn note
  — served live by `/api/ext/queue`.
- **Extension token** lives at `~/.applypilot/ext_token`; the dashboard prints it on startup.
- **Dashboard:** `.venv/bin/applypilot dashboard --serve` → http://localhost:8765. Restart after code
  changes; kill a stale one with `lsof -ti:8765 | xargs kill -9`. Hard-refresh the browser after
  frontend edits.

## Dev workflow notes (important)

- Run via **`.venv/bin/applypilot ...`** (or `PYTHONPATH=src .venv/bin/python`). The editable install
  is flaky; after source edits run **`.venv/bin/python -m pip install ".[gmail]" --quiet`** to refresh
  the installed console script, then restart the dashboard.
- **Tests:** `APPLYPILOT_DIR=$(mktemp -d) PYTHONPATH=src .venv/bin/python -m pytest tests/ -q`.
  **126 passing** (verified 2026-07-27). The `APPLYPILOT_DIR=$(mktemp -d)` prefix is required — without it
  tests run against your real `~/.applypilot/`.
- **Ruff clean** — `.venv/bin/python -m ruff check .` (line-length 120, target py311). Two long-standing
  `F841` dead assignments (`apply/prompt.py` `auth_info`, `scoring/tailor.py` `projects_str`/`projects`)
  were removed 2026-07-27; the generated prompts were diffed old-vs-new and are byte-identical.
  Note `preserved_projects` is deliberately NOT fed to either tailor prompt — only companies, school,
  and real metrics are. That's existing behavior, not an oversight of the cleanup.
- Extension behavior is manual, but there's much less to test now — it's a popup only (`extension/README.md`).
  `MANUAL-TEST.md` and `CONTRACTS.md` were deleted along with the auto-composer.
- **Gmail optional dep:** `pip install ".[gmail]"` (google-api-python-client, google-auth-oauthlib).
- **Big decisions get an adversarial multi-agent review first** (Workflow) — caught 13 issues on the
  networking PRD, the agent-browser blocker on the DM PRD, and 5 blockers + the MV3 issue on the
  extension PRD. Worth it before building risky things. **Extension was built by a coordinated Workflow
  team** (freeze contracts → parallel per-file build → integrate → verify).
- **Working tree is clean, but `main` is 10 commits AHEAD of `origin/main`** (as of 2026-07-27) — the
  whole dashboard-tracker + hot-contacts + extension-rebuild run is local only. Push when ready.
  Nothing sensitive in git (secrets/DB/token all in `~/.applypilot/`, outside the repo).
- `.venv/bin/` has accumulated `applypilot 2`…`applypilot 9` duplicate console scripts from repeated
  reinstalls. Harmless clutter — `applypilot` is the live one.

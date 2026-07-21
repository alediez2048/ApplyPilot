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

New module **`src/applypilot/networking/linkedin_dm.py`** + `dm_prompt.py`. Drives the installed
**agent-browser** binary (`~/.local/bin/agent-browser` v0.27.0) as a subprocess to send drafted
LinkedIn notes. **Repos stay SEPARATE** — ApplyPilot shells out to the CLI (like claude/npx/Chrome).

- **The MCP blocker was designed around, not fixed.** The original LDM-1 plan (`claude --mcp-config
  <agent-browser mcp>`) is dead on 0.27.0 (no `mcp` subcommand). Instead the driver uses 0.27.0's
  **deterministic CLI**: `open --profile <dir>` (persistent logged-in profile, fixes the wrong-browser
  blocker), `snapshot` (a11y tree w/ refs), `keyboard inserttext` (real keystrokes into LinkedIn's
  `contenteditable` composer), `click`, `screenshot`. **agent-browser keeps ONE persistent browser
  session across CLI calls** (verified), so a send = a sequence of subprocess calls.
- **Controller loop**: `snapshot → LLM picks ONE action → execute`, over a tiny fixed action set
  (`click`/`type_message`/`send`/`abort`/`done`, see `dm_prompt.ACTIONS`). The note is inserted
  **VERBATIM** — the model never supplies text (prompt-injection can't change what you say).
- **Two delivery paths (the note is a ≤300-char connection-request note by design):** PATH A —
  **Connect → Add a note → Send invitation** (the common case; works for people you're NOT connected
  to, which is most contacts). PATH B — **Message** composer (when already connected / open-profile).
  The controller prefers A unless a Message button is present; aborts on any InMail/Premium paywall.
- **Safeguards** (all in `linkedin_dm.send()`): off by default (`NETWORKING_LINKEDIN_DM=0`), one-time
  consent file (`.linkedin_dm_consent`), login precheck, dedicated isolated profile
  (`~/.applypilot/linkedin-dm-profile`), daily cap (`LINKEDIN_DM_DAILY_LIMIT=5`), 30-day cross-contact
  dedupe on normalized `linkedin_url`, atomic claim (`claim_dm_send`, `dm_sent_at IS NULL`), **dry-run**
  (composes but never clicks Send). DB cols `dm_status/dm_sent_at/dm_error` (own migration in `store.py`).
- **CLI**: `network --dm-login` (consent + headed login, polls for auth), `--dm-list`,
  `--send-dm --dm-contact <id> [--dry-run]`. **Dashboard**: "Dry-run DM" + "Send DM" buttons per contact
  (single-flight `DMRunner` — one browser session), Origin-guarded `POST /api/outreach/send-linkedin`,
  `_dm_available()` gate. **doctor** shows an `agent-browser` + DM-readiness line. `.env.example` updated.
- **Tests**: `tests/test_linkedin_dm.py` (13, subprocess+LLM mocked): bin discovery, verbatim prompt,
  atomic claim race, url-normalized dedupe, all send() refusal paths, dry-run-composes-but-never-sends.
  Full suite **96 passing**, ruff clean.
- **STILL PENDING (needs the user, interactive):** the one-time `network --dm-login` (I can't type
  LinkedIn creds), then the first dry-run + real send. **Account-risk:** user chose primary/unrestricted;
  I kept an automatic dry-run pre-flight before the first live send. The 5 Affirm DM-eligible contacts
  are non-connections but ARE reachable via **Connect + note** (Path A) — no InMail needed. LinkedIn also
  rate-limits invitations (~100–200/week); the 5/day cap stays well under.

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

## 8. Chrome extension "LinkedIn Assistant" — BUILT (EXT-0..5), in live debugging

The pivot: run INSIDE the user's real browser as an MV3 extension. It composes the note into the invite
dialog and **the HUMAN clicks Send** (reliable + safe — LinkedIn blocks *robot* Sends, not human ones).
Full PRD + review + build:
- **Docs:** `docs/chrome-extension-prd.md` (v2 after review), `docs/chrome-extension-review.md`
  (51-agent adversarial review → GO WITH CHANGES; caught the MV3-stateless-worker issue + 2 real code
  bugs), `docs/tickets/EXT-0..6` + `EXT-README`.
- **Code bugs the review fixed in the main app** (`web_dashboard.py`/`store.py`): **B3** — LinkedIn
  queue eligibility now excludes `_DM_DONE_STATUSES = {sent,manual,skipped}` (was only `sent`); **B5** —
  `mark_dm_sent` now stamps `dm_sent_at` (was invisible to the CLI dedupe/cap). Added
  `mark_dm_manual`/`mark_dm_skipped`.
- **`extension/`** (MV3, no build step): `manifest.json` (minimal perms: storage+alarms+host only),
  stateless `background.js` (ALL run-state in `chrome.storage`; idempotent advance; timestamp pacing;
  owns/validates a dedicated tab; validates linkedin_url), `content.js` (React-safe textarea fill,
  identity cross-check, `textContent`-only overlay, positive send-detection, **NEVER clicks Send** —
  verified), `popup.*`, `selectors.json`. `shared/constants.js` = frozen contract.
- **Local API (EXT-0):** `GET /api/ext/queue[?job_url]`, `POST /api/ext/status`, `POST /api/ext/note` on
  `:8765`; mutual **shared token** at `~/.applypilot/ext_token` (generated on dashboard startup, printed
  to console; paste into the popup). 19 tests in `tests/test_ext_api.py`.
- **Live-debug fixes so far:** (1) token chicken-and-egg → generate on startup; (2) dead Hunter URLs →
  auto-skip; (3) content script inert on LinkedIn → **CSP blocks dynamic `import()`/`fetch()` of
  extension resources** → inject constants+selectors as CLASSIC content scripts
  (`shared/constants.content.js` + `shared/selectors.content.js`, before `content.js`); (4) mass
  auto-skip (all 9 contacts) → `profileUnavailable()` false-positived on nav phrases + auto-skipped the
  ambiguous "name didn't load" case → now STRONG 404 signals only + PAUSE on ambiguous.
- **STILL PENDING (needs the user, interactive):** first successful live compose on a real BetterUp
  profile. **After ANY extension reload, the user MUST also Cmd+R the LinkedIn tab** (reload orphans the
  content script). Debug via DevTools console → Verbose → `[ApplyPilot]` logs.

## Current environment state (the user's machine)

- **Contacts: Apollo.io (PAID Basic plan), sole provider.** Hunter removed. Apollo surfaces recruiters/
  hiring managers by title. (Legacy Affirm contacts are old Hunter data with junk/dead URLs.)
- **Gmail: connected via OAuth**, sends from **jorgealejandrodiezm@gmail.com** (both apps + outreach).
  Outreach emails carry résumé + cover letter PDF attachments.
- **`TAILOR_AGGRESSIVE=1`** set — resumes aggressively match the JD (fabrication guard off).
- **899 LinkedIn connections imported.** Real jobs: Affirm, Visa, **AMD** (careers site 403-blocks
  automation), **BetterUp** (Ashby — applied successfully). Both AMD + BetterUp scored 4/10 (reach roles;
  the scorer reads the user as PM/SEO, not eng — recalibration may be worth it).
- **Extension token** lives at `~/.applypilot/ext_token`; the dashboard prints it on startup.
- **Dashboard:** `.venv/bin/applypilot dashboard --serve` → http://localhost:8765. Restart after code
  changes; kill a stale one with `lsof -ti:8765 | xargs kill -9`. Hard-refresh the browser after
  frontend edits.

## Dev workflow notes (important)

- Run via **`.venv/bin/applypilot ...`** (or `PYTHONPATH=src .venv/bin/python`). The editable install
  is flaky; after source edits run **`.venv/bin/python -m pip install ".[gmail]" --quiet`** to refresh
  the installed console script, then restart the dashboard.
- **Tests:** `APPLYPILOT_DIR=$(mktemp -d) PYTHONPATH=src .venv/bin/python -m pytest tests/ -q`.
  **~110 passing**, ruff clean (line-length 120). Extension browser behavior is MANUAL (`extension/MANUAL-TEST.md`).
- **Gmail optional dep:** `pip install ".[gmail]"` (google-api-python-client, google-auth-oauthlib).
- **Big decisions get an adversarial multi-agent review first** (Workflow) — caught 13 issues on the
  networking PRD, the agent-browser blocker on the DM PRD, and 5 blockers + the MV3 issue on the
  extension PRD. Worth it before building risky things. **Extension was built by a coordinated Workflow
  team** (freeze contracts → parallel per-file build → integrate → verify).
- **All work committed + pushed to `main`** (github.com/alediez2048/ApplyPilot). Nothing sensitive in git
  (secrets/DB/token all in `~/.applypilot/`, outside the repo).

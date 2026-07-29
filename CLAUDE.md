# ApplyPilot — Codebase Index

An AI job-search CRM. Discovers jobs across many boards, scores them against your resume,
tailors a resume + cover letter per job, drives a real Chrome browser to submit the
application, finds real people at the company, drafts and sends outreach, and tracks every
follow-up until someone replies.

It started as a bulk auto-applier. It is now closer to a **single-operator CRM whose first
campaign happens to be a job search** — see `docs/crm-prd.md` for where that goes next.

- **Language / runtime:** Python ≥ 3.11
- **Packaging:** Hatchling, `src/` layout, single package `applypilot`
- **Entry point:** `applypilot = "applypilot.cli:app"` (Typer CLI)
- **License:** AGPL-3.0-only · **Version:** 0.4.0 (`pyproject.toml`)
- **Tests:** 393 passing (`tests/`, 30 files) · ruff clean (line-length 120, py311) · ESLint clean

## Quick orientation

A **6-stage pipeline over a SQLite `jobs` table**. Each stage reads rows at one state and
writes columns that advance them. Stages are idempotent and independently runnable.

```
discover → enrich → score → tailor → cover → pdf →  [apply]
                                                    [network → outreach → follow-up]
```

Surfaces:
- `applypilot run [stages...]` — prep pipeline, sequential or `--stream`
- `applypilot apply` — browser submission (Tier 3); `--copilot` fills and stops
- `applypilot dashboard --serve` — the operator UI, and where you actually live
- `applypilot network` — contact discovery + outreach

## Tiers (`config.py`, gated by `check_tier()`)

**1 Discovery** (Python only) · **2 AI Scoring & Tailoring** (+ LLM key) ·
**3 Full Auto-Apply** (+ Claude Code CLI + Chrome + Node).

---

## Source map (`src/applypilot/`)

| Path | Role |
|------|------|
| `domain/` | **Business rules, pure.** No `sqlite3`, no `http`, no imports from `web_dashboard` or `networking`. Everything else calls into it. |
| `cli.py` | Typer CLI: `init`, `run`, `apply`, `status`, `dashboard`, `doctor`, `network`. |
| `pipeline.py` | Orchestrates `run` — stage order, deps, sequential + streaming runners. |
| `config.py` | Paths (`~/.applypilot/`), Chrome detection, profile/YAML loaders, tier system. |
| `database.py` | SQLite layer. Owns `jobs` + `job_events`. Thread-local WAL, forward-only migrations. |
| `llm.py` | Multi-provider client (round-robin + failover: OpenAI/Gemini/Anthropic/local). |
| `view.py` | Static HTML results export. |
| `web_dashboard.py` | **The operator dashboard.** 1,861 lines, **zero SQL** — data access goes through `repo/` and `store.py` (ARCH-4). |
| `repo/jobs.py` | Every `jobs` query as a named function. Owns `QUEUE_SQL` (what counts as an operator-added job). |
| `settings.py` | **Every env var, one registry.** Types, defaults, validators, secret flags. Malformed values fail at startup naming the variable. `.env.example` is generated from it. |
| `migrations/` | Numbered `mNNN_*.py` with `up(conn)`, run at startup after the additive column pass. **Migrations must be idempotent** — this app gets killed mid-operation. `001` = the ARCH-3 touches backfill. |
| `static/` | `index.html` · `dashboard.css` · `dashboard.js`. Served from `/static/…?v=<version>-<mtime>`; the page itself is `no-store`. One **classic** script, not a module — ~56 inline `onclick=` attributes resolve against the global object. |

### `discovery/` · `enrichment/` · `scoring/`
`jobspy.py` (Indeed/LinkedIn/Glassdoor/ZipRecruiter/Google) · `workday.py` (CXS API) ·
`smartextract.py` (AI scraper) — `ats.py` (Greenhouse/Lever/Ashby APIs, tier-0) ·
`detail.py` (3-tier scrape cascade) — `scorer.py` · `tailor.py` · `cover_letter.py` ·
`validator.py` · `resume_render.py` · `pdf.py`.

`resume_renderer/` is a Node/React-PDF renderer (`node render.mjs <request.json> <out.pdf>`),
`npm install`ed at runtime into `~/.applypilot/resume_renderer_runtime/`.

**The base résumé is the template** (`resume_sections.py`, 2026-07-29). Its section titles and
order flow through tailor → `_DATA.json` → the renderer. Tailoring rewrites content *inside*
those sections and may not rename, drop, reorder or invent one; a section the model omits falls
back to the original, and bullets are padded to the original count from the trailing originals
(NOT merged by similarity — a real rewrite doesn't resemble its source, so prefix-matching
turns 3 rewrites into 6 duplicated bullets). Cover letters must name the employer.

### `apply/` — autonomous submission
`launcher.py` (acquires jobs, spawns Chrome + Claude Code per job) · `chrome.py` ·
`prompt.py` · `dashboard.py`. The agent is **tool-scoped**: `--allowedTools
mcp__playwright,mcp__gmail__send_email` plus a hard deny-list. It browses attacker-controlled
careers pages, so inbox read is denied twice (allowlist + deny-list) — pinned by a test.

### `domain/` — the business rules (ARCH-1, 2026-07-28)

470 lines of pure functions. Dicts in, dicts out. **`domain/` may not import `http`,
`sqlite3`, `web_dashboard`, or `networking`** — a test-able boundary, and the reason the eval
harness no longer has to import a web server to test scheduling.

| File | Role |
|------|------|
| `followup.py` | **ONE ladder engine.** `Channel` dataclass + `touch_state()`. Since ARCH-3 it names no ladder column at all — state arrives as a `ladder` dict from `touches`, identical in shape for every channel. |
| `checklist.py` | `job_checklist()` — completion state; zero-denominator steps are `na` and excluded from the percentage. |
| `verification.py` | `verify_contact()` — moved from `networking/` (it never was a networking concern). `networking/verify.py` is a re-export shim. |
| `company.py` | `companies_match()` — shared by connections, Apollo org resolution, and verification, which must all agree. |
| `timeutil.py` | `parse_ts()` — one implementation of the naive/aware guard. |

**Adding a channel (e.g. SMS) is one `Channel` entry plus one prompt** — executed, not
claimed: `test_adding_a_channel_needs_no_schema_change` defines an SMS channel that exists
nowhere in the codebase and drives it end to end. Readiness is data too (`ready=(("phone",
None),)`), which is what removed the last `if channel is EMAIL` from `_is_ready`.

### `networking/` — contacts, outreach, follow-up
| File | Role |
|------|------|
| `service.py` | Orchestrator: derive → search → rank → verify → persist → draft. COLD (Apollo) + HOT (your connections) layers. |
| `store.py` | The `contacts` table (42 cols, own migration in `_CONTACT_COLUMNS`). Atomic claims for every send. |
| `derive.py` | Job URL → employer name + domain. ATS path slugs, board rejection. |
| `providers.py` / `apollo.py` | Apollo is the sole provider (paid plan). `resolve_orgs()` disambiguates fuzzy name search. |
| `verify.py` | **Self-check**: does this person actually work there? Runs before contacts reach you. |
| `rank.py` | Pick 3–5 (peers + a recruiter). |
| `connections.py` | LinkedIn `Connections.csv` import + `companies_match()` (word-aware, strict/lenient). |
| `outreach.py` / `prompt.py` | Drafts: cold email + LinkedIn note + **email follow-ups** + **LinkedIn follow-ups**, each written for its touch position. |
| `gmail_send.py` / `gmail_oauth.py` | Send via OAuth (preferred) or SMTP. Threading, signature, attachments, safeguards. |
| `linkedin_dm.py` / `dm_prompt.py` | Dormant CLI-only compose helpers (auto-send abandoned — §Lessons). |
| `linkedin_agent.py` | Opt-in read-only LinkedIn augmentation. Never sends. |

### `extension/` (repo root)
MV3 popup only — pulls the outreach queue from `:8765`, copies a note, opens the profile.
**No LinkedIn host permission, no content script, no background worker** — structurally
incapable of touching a LinkedIn page. See §Lessons.

---

## Data model

| Table | Owner | Purpose |
|-------|-------|---------|
| `jobs` (32 cols) | `database.py` | The 6-stage state machine. `_ALL_COLUMNS` is its source of truth. |
| `contacts` (32 cols) | `networking/store.py` | People per job + outreach + verification. |
| `touches` | `networking/touches.py` | One follow-up touch per row, ANY channel. `seq` is per (contact, channel). |
| `sequences` | `networking/touches.py` | Terminal state per (contact, channel): `stopped` / `replied`. |
| `connections` | `networking/connections.py` | Imported LinkedIn CSV. |
| `job_events` | `database.py` | Per-job activity log. Append is best-effort, never raises. |

Live counts (2026-07-28): jobs 7, contacts 28, connections 899, job_events 52.

**`jobs` columns by stage:** discover(`title,salary,description,location,site,strategy`) →
enrich(`full_description,application_url,detail_error`) → score(`fit_score,score_reasoning`) →
tailor(`tailored_resume_path`) → cover(`cover_letter_path`) → apply(`applied_at,apply_status,
apply_error,agent_id,verification_confidence`), plus `rejected_at`.

**`contacts` groups:** identity · outreach(`outreach_subject/message/status,sent_message_id`) ·
threading(`thread_id,rfc_message_id`) · LinkedIn invite(`dm_status,dm_sent_at`) ·
operator(`phone,notes`) · verification(`confidence,verify_note`).
**Follow-up state is NOT here** — it is `touches` / `sequences`, keyed by channel (ARCH-3).

---

## The dashboard (`web_dashboard.py`)

Localhost-only (`127.0.0.1:8765`), Origin/CSRF-guarded. Restructured 2026-07-28 from four
sibling accordions into:

- **Status strip** (always visible, never a toggle) — a left-to-right path
  `✓ Found → ✓ Applied → ✓ Emailed 4/4 → ↻ Follow up 0/4 → · Reply`, first unfinished step
  amber, plus **one** `Next` action (`nextAction()`) and a visible `🔄 Re-apply`.
- **One tabbed panel**: People · Follow-ups · Materials · Activity. `PANEL_OPEN` / `TAB_OPEN`
  survive the 2.5s refresh.
- **Contacts collapse to one line** with channel pills (`✉ sent · 🔗 connected · ↻ due`).
  Opening one shows channels as tabs so email/LinkedIn/phone stop stacking.
- `⋯` row menu holds destructive actions (rejected, delete).

The 2.5s refresh replaces `#jobs` wholesale, so `refresh()` **skips while any input in that
subtree has focus** — otherwise it eats what you're typing.

`/api/status` costs **50 SQL statements** (was 313 before ARCH-4 measured it — 199 of those
were `CREATE TABLE IF NOT EXISTS` re-run every request). `tests/test_query_budget.py` holds
the line and fails on a per-contact N+1. Batch a new query; don't raise the budget.

---

## Follow-up sequences

Two independent ladders, both human-in-the-loop. Nothing auto-sends.

| | Email | LinkedIn |
|---|---|---|
| Anchor | `submitted_at` → last `touches.sent_at` | `dm_sent_at` → last `touches.sent_at` |
| Default | `FOLLOWUP_SCHEDULE=48,96,168` (2d/4d/7d) | `LINKEDIN_FOLLOWUP_SCHEDULE=120,288` (5d/12d) |
| Send | `gmail_send.send_followup()`, threaded | copy → open profile → you paste → `✓ I sent it` |
| Stop | reply / stop / sequence complete | same |

Per-touch prompts differ by position: touch 1 adds something new, touch 2 offers a redirect
("is someone else the right person?"), touch 3 says plainly it's the last one. All are told
to give an explicit out and never to restate the previous message. LinkedIn copy is much
shorter (it lands in a chat window) and must ask exactly one answerable question.

**Threading works with no extra OAuth scope**: Gmail returns `threadId` on the send response
and we generate the RFC `Message-ID` ourselves — both persisted at send time. Emails sent
before those columns existed can't thread; those cards say **⚠ won't thread**, and
`backfill_thread_ids()` recovers them once the read scope is granted.

**Not built:** no scheduler (nothing fires while the dashboard is closed), no reply
detection, **no per-company cap** — 5 contacts × 3 touches is 15 emails at one company.

---

## Correctness: verify + evals

Contact discovery is a fuzzy chain — **job URL → employer → domain → Apollo org → people** —
and a wrong answer anywhere yields *real humans who work somewhere else*. Four such bugs
shipped; none raised an error. Two layers now cover it:

**`networking/verify.py` (runtime).** Judges every candidate before it reaches the UI.
Signals: work-email domain (a contradiction is near-proof) and Apollo's org name (catches
people with **no email**, which the domain check can't). "No evidence" is `unverified`, never
a rejection — dropping a real contact is worse than showing an unconfirmed one. Verdicts
surface as a `? unconfirmed` chip and a reasons line; rejections go to the activity log.

**`evals/resolution.jsonl` + `tests/test_eval_resolution.py` (regression).** 49 labelled
cases scoring employer resolution, company matching, and verification. Runs offline — no
keys, no network, no credits. **Every shipped bug is a negative case** (Armanino, State Farm,
Centrient Ph·arm·aceuticals, Writer Corporation, clever.com, Meta Platforms, Hamming AI); a
test asserts they can't be pruned. A happy-path set would have passed all four bugs.

It earned its keep on the first run, catching `"lever.co" in "careers.clever.com"` →
company `"Jobs"` — the same substring bug class, inside the function written to fix it.

---

## Lessons that cost real time (do not relitigate)

1. **Never substring-match entities.** Four bugs, one root cause: `"arm" in "armanino"`,
   `"lever" in "clever.com"`, `"jobs" in "jobsight.com"`, `"lever.co" in "careers.clever.com"`.
   Compare whole **words** (`companies_match`) or whole **host labels** (`_is_board_host`).
2. **`lstrip("www.")` strips a character set, not a prefix** — it ate the `w` in `webai.com`.
   Use `removeprefix`.
3. **Driving LinkedIn from outside the browser does not work.** Abandoned twice (agent-browser
   CLI, then an MV3 auto-composer, ~2,900 lines deleted). The a11y tree misses React modals,
   synthetic clicks don't fire handlers, and LinkedIn soft-blocks. Copy-paste is the design.
4. **Apollo will not release a direct dial to a local tool.** Verified three ways: search only
   returns `has_direct_phone`, `people/match` 400s without a *public* webhook (loopback
   rejected), and creating a contact yields only the org switchboard. Phones are manual.
5. **Apollo's company name search is fuzzy.** "WRITER" returns five orgs including Writer
   Corporation and a freelance resume writer. Always disambiguate; never pass all ids.
6. **Timestamps may be naive.** Older rows have no timezone; subtracting from an aware `now`
   raises and 500s the whole dashboard. Parse via `_parse_ts()`.
7. **A JS `ReferenceError` blanks the entire jobs table as silently as a syntax error.**
   `test_dashboard_js_valid.py` only parses; `test_dashboard_render.py` executes the render
   path under DOM stubs. ARCH-2 retired the parse-only test — building the `Function` in the
   render test already throws on a syntax error, and ESLint covers the rest.

---

## Where the work goes next

`docs/architecture-prd.md` — current state, the architecture grilling, and the plan.
`docs/tickets/ARCH-README.md` — the ordered ticket list. Read that before starting anything.

**Chosen order (Jorge, 2026-07-28): finish the ARCH set first**, then the product tickets —
`ARCH-1` ✅ → `ARCH-2` ✅ → `ARCH-3` ✅ → `ARCH-4` ✅ → `ARCH-5` ✅ → `ARCH-6` ✅, then `CRM-1` → `DISC-1`
→ `CRM-2` → `CRM-3`.

The analysis recommended the reverse, and the reason is measured, not aesthetic: all 7 jobs
were pasted in by hand (discovery produced **0**), only 1 reply is recorded (typed in
manually), and nothing is aggregated. **The ARCH set delivers no user-visible change** — the
funnel stays at 7 hand-pasted jobs and the system stays blind to replies until `CRM-1` lands.
The tradeoff was raised and accepted; do not re-litigate it, but do not forget it either.

**The ARCH set is complete.** Next is the product work, in the order agreed on 2026-07-28:
`CRM-1` (reply detection — the system is still blind to replies) → `DISC-1` (turn discovery
on; it has produced 0 jobs) → `CRM-2` → `CRM-3`.

`CRM-1` is the one that changes what the app can do: 13 emails sent, 7 follow-ups, **1 reply
recorded — typed in by hand.** Everything it needs is already stored (`thread_id`,
`rfc_message_id` captured at send time); the only missing piece is reading.

`docs/crm-prd.md` — the multi-campaign "Spaces" direction. **After** the above.

## Known debt (architecture review, 2026-07-28)

Ordered by leverage. Nothing here is blocking; all of it compounds.

1. ~~**Extract `domain/`**~~ — **done (ARCH-1, 2026-07-28).** Three duplicate "is it due"
   implementations collapsed to one; `/api/status` verified byte-identical.
2. ~~**`contacts` has two of everything**~~ — **done (ARCH-3, 2026-07-29).** 42 columns → 32.
   The copy was already *incomplete*: email had `followup_subject`/`followup_error`, LinkedIn
   silently had neither. Root cause was `followup_status` doing two jobs — one touch's
   delivery lifecycle AND the sequence's terminal state — so `touches` + `sequences` are two
   tables, not the one the ticket sketched.
3. ~~**996 lines of JS in a Python string**~~ — **done (ARCH-2, 2026-07-28).** The cut was
   verified by reassembling the three files back into the old string **byte for byte**.
   ESLint now runs (`npm run lint`, dev-only). It cannot see functions called from `onclick=`
   attributes, so `test_no_dead_functions` reads the HTML too — on the first run it found
   **5 dead functions and 3 dead Sets** stranded by the ARCH-1 tabs restructure.
   **`web_dashboard.py` is 1,953 lines and all Python.** ~430 of them are pipeline
   orchestration (`run_dashboard_prepare/apply/fill_one/restart/continue`) that are not HTTP
   concerns and belong to ARCH-4. The ticket's "< 1,800" criterion was arithmetically
   unreachable and is marked superseded rather than fudged.
4. ~~**Forward-only migrations.**~~ — **done (ARCH-5, 2026-07-29).** The additive column dicts
   stay for adding columns; `migrations/mNNN_*.py` handles rename/drop/backfill. Version is in
   `doctor` and `applypilot migrate --status`. A 300s lease on `claimed_at` is what lets a
   crashed run retry WITHOUT letting concurrent starts double-apply — the first version
   collapsed those two cases and ran a migration 6 times out of 6 concurrent starts.
5. ~~**40 env vars, no schema.**~~ — **done (ARCH-6, 2026-07-29).** 38 declared in
   `settings.py`; `FOLLOWUP_AFTER_DAYS` is now derived from `FOLLOWUP_SCHEDULE[0]`
   (deprecated, still honoured, warns). `applypilot doctor --config` shows value + source;
   a test fails if code reads a variable the registry does not declare.
6. **14 modules still touch the DB directly** (was 21) — `enrichment/detail.py`,
   `apply/launcher.py`, `view.py`, the discovery/scoring stages. The dashboard no longer
   does (ARCH-4), and `test_sql_lives_only_in_the_data_layer` names the rest in an
   allowlist so the list can only shrink and no NEW module can join it.
7. **Apply runs synchronously inside the HTTP request thread** (`run_dashboard_restart` →
   `subprocess.run`). It blocks a dashboard worker for minutes and dies with the server —
   which is exactly how two applies were killed on 2026-07-29. `release_stale_locks()`
   cleans up after it; making it a real background task is still open.

---

## Environment (this machine)

- **Apollo.io** (paid Basic) is the sole contact provider. Hunter removed.
- **Gmail OAuth** connected, sends from `jorgealejandrodiezm@gmail.com`. Requested scopes are
  now `gmail.send` + `gmail.metadata` + `gmail.settings.basic` — **metadata, not readonly**:
  it gives headers/threads and *cannot read message bodies*. The stored token may still be
  send-only; `doctor` reports missing scopes, and `_load_creds()` loads with the token's own
  scopes so an older token keeps working.
- **Base resume** = `~/.applypilot/resume.txt` (replaced 2026-07-28 with the Technical Project
  Manager version: PERSONAL STATEMENT / WORK EXPERIENCE / EDUCATION / KEY STRENGTHS, 3
  employers). `preserved_companies` was reduced to T-Mobile/Verizon/Kordami to match —
  a preserved company missing from the base resume is a **hard validation failure**.
  **Open:** the renderer still emits the old section names/order, the résumé says
  "Diez Magni" while `profile.json` says "Diez", and it uses `alediez2408@gmail.com` while
  sending happens from `jorgealejandrodiezm@gmail.com`.
- `TAILOR_AGGRESSIVE=1` — **voice only now.** It used to force `validation_mode="lenient"`,
  which silently disabled the fabrication judge AND every banned-word check. Content
  preservation no longer depends on it (the assembler enforces sections + bullet counts), so
  the mode has no reason to buy JD-matching vocabulary with fabrication detection.
  **The real lever was the dashboard**, which hardcoded `lenient` in three places — so every
  dashboard-driven run skipped the judge regardless of the flag. Now `normal`: banned words
  are warnings, and warnings reach the job's Activity tab instead of only `_REPORT.json`.
- 899 LinkedIn connections imported. Secrets in `~/.applypilot/` are `chmod 600`; FileVault on.

## Dev workflow

```bash
.venv/bin/python -m pip install ".[gmail]" --quiet     # after ANY source edit
lsof -ti:8765 | xargs kill -9 && .venv/bin/applypilot dashboard --serve --no-open
APPLYPILOT_DIR=$(mktemp -d) PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
.venv/bin/python -m ruff check .
npm install && npm run lint                            # frontend only; dev-only, never shipped
.venv/bin/applypilot migrate --status                  # schema version + pending migrations
.venv/bin/applypilot doctor --config                   # every setting, its value, its source
```

The `APPLYPILOT_DIR=$(mktemp -d)` prefix is **required** — without it tests run against your
real `~/.applypilot/`. The editable install is flaky; reinstall then restart the dashboard.

**Frontend edits no longer need a hard refresh.** The install is not editable, so a frontend
change still needs the `pip install` above — but that copy gives the file a new mtime, the
`?v=` changes with it, and the browser fetches the new bundle on a normal reload.
`index.html` is served `no-store`, so it is always re-read.

- Big/risky designs get an adversarial multi-agent review first (Workflow). It has caught 13
  issues on the networking PRD, the agent-browser blocker, and 5 blockers on the extension PRD.
- **Check for in-flight applies before restarting the dashboard.** `run_dashboard_restart`
  runs the apply as a synchronous child, so `kill -9` on the server kills the application
  mid-flight. Two were lost that way on 2026-07-29. `curl -s localhost:8765/api/status` and
  look for `in_progress` first.
- Validator warnings now reach the job's **Activity tab** (`Résumé note:` / `Cover letter
  note:`), not just `{prefix}_REPORT.json`. They were invisible before, which is how banned
  filler and dropped tools shipped unnoticed.
- Severity ladder: `preserved_companies`/`preserved_school` missing = **error** (blocks);
  `preserved_projects` missing = warning; banned words = strict-mode only.
- Working tree currently has **24 uncommitted files** — the whole of the 2026-07-28 session.

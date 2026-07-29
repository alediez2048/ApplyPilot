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
- **Tests:** 245 passing (`tests/`, 21 files) · ruff clean (line-length 120, py311)

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
| `web_dashboard.py` | **The operator dashboard.** 3,500 lines (1,602 of them the HTML/CSS/JS template) — see *Known debt*. |

### `discovery/` · `enrichment/` · `scoring/`
`jobspy.py` (Indeed/LinkedIn/Glassdoor/ZipRecruiter/Google) · `workday.py` (CXS API) ·
`smartextract.py` (AI scraper) — `ats.py` (Greenhouse/Lever/Ashby APIs, tier-0) ·
`detail.py` (3-tier scrape cascade) — `scorer.py` · `tailor.py` · `cover_letter.py` ·
`validator.py` · `resume_render.py` · `pdf.py`.

`resume_renderer/` is a Node/React-PDF renderer (`node render.mjs <request.json> <out.pdf>`),
`npm install`ed at runtime into `~/.applypilot/resume_renderer_runtime/`.

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
| `followup.py` | **ONE ladder engine.** `Channel` dataclass + `touch_state()`. Email and LinkedIn differ only in data (anchor field, schedule, state fields, `can_autosend`) — there is no per-channel branch, and a test enforces that. |
| `checklist.py` | `job_checklist()` — completion state; zero-denominator steps are `na` and excluded from the percentage. |
| `verification.py` | `verify_contact()` — moved from `networking/` (it never was a networking concern). `networking/verify.py` is a re-export shim. |
| `company.py` | `companies_match()` — shared by connections, Apollo org resolution, and verification, which must all agree. |
| `timeutil.py` | `parse_ts()` — one implementation of the naive/aware guard. |

**Adding a channel (e.g. SMS) should be one `Channel` entry plus one prompt.** If it ever
needs a new `if`, `followup.py` has regressed.

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
| `contacts` (42 cols) | `networking/store.py` | People per job + outreach + both follow-up ladders + verification. |
| `connections` | `networking/connections.py` | Imported LinkedIn CSV. |
| `job_events` | `database.py` | Per-job activity log. Append is best-effort, never raises. |

Live counts (2026-07-28): jobs 7, contacts 28, connections 899, job_events 52.

**`jobs` columns by stage:** discover(`title,salary,description,location,site,strategy`) →
enrich(`full_description,application_url,detail_error`) → score(`fit_score,score_reasoning`) →
tailor(`tailored_resume_path`) → cover(`cover_letter_path`) → apply(`applied_at,apply_status,
apply_error,agent_id,verification_confidence`), plus `rejected_at`.

**`contacts` groups:** identity · outreach(`outreach_subject/message/status,sent_message_id`) ·
threading(`thread_id,rfc_message_id`) · email follow-up(`followup_count,followed_up_at,
followup_message,followup_status`) · LinkedIn(`dm_status,dm_sent_at`, `li_followup_*`) ·
operator(`phone,notes`) · verification(`confidence,verify_note`).

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

---

## Follow-up sequences

Two independent ladders, both human-in-the-loop. Nothing auto-sends.

| | Email | LinkedIn |
|---|---|---|
| Anchor | `submitted_at` → `followed_up_at` | `dm_sent_at` → `li_followed_up_at` |
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
   path under DOM stubs. Both exist because the JS lives in a Python string.

---

## Where the work goes next

`docs/architecture-prd.md` — current state, the architecture grilling, and the plan.
`docs/tickets/ARCH-README.md` — the ordered ticket list. Read that before starting anything.

**Chosen order (Jorge, 2026-07-28): finish the ARCH set first**, then the product tickets —
`ARCH-1` ✅ → `ARCH-2` → `ARCH-3` → `ARCH-4` → `ARCH-5` → `ARCH-6`, then `CRM-1` → `DISC-1`
→ `CRM-2` → `CRM-3`.

The analysis recommended the reverse, and the reason is measured, not aesthetic: all 7 jobs
were pasted in by hand (discovery produced **0**), only 1 reply is recorded (typed in
manually), and nothing is aggregated. **The ARCH set delivers no user-visible change** — the
funnel stays at 7 hand-pasted jobs and the system stays blind to replies until `CRM-1` lands.
The tradeoff was raised and accepted; do not re-litigate it, but do not forget it either.

**Next up: `ARCH-2`** (frontend → static files). That is where the 1,602-line template goes.

`docs/crm-prd.md` — the multi-campaign "Spaces" direction. **After** the above.

## Known debt (architecture review, 2026-07-28)

Ordered by leverage. Nothing here is blocking; all of it compounds.

1. ~~**Extract `domain/`**~~ — **done (ARCH-1, 2026-07-28).** Three duplicate "is it due"
   implementations collapsed to one; `/api/status` verified byte-identical.
   **`web_dashboard.py` is still 3,500 lines**, but **1,602 of those are the HTML/CSS/JS
   template** — that is ARCH-2's scope, not leftover domain logic. Actual Python: 1,898.
2. **`contacts` has two of everything** — `followup_*` / `li_followup_*` is one concept
   (a touch sequence) copy-pasted per channel. A `touches(contact_id, channel, seq, …)` table
   collapses it and makes SMS free. Defensible at 28 contacts; the cost is duplicated *code*.
3. **996 lines of JS in a Python string** — no linter, no types, no modules. Moving to a
   static file costs little and buys ESLint + real stack traces.
4. **Forward-only migrations.** No rename/drop/backfill/version. `preserved_companies` and the
   stale `.edu` PDFs each needed a hand-written fix.
5. **40 env vars, no schema.** `FOLLOWUP_SCHEDULE` / `LINKEDIN_FOLLOWUP_SCHEDULE` /
   `FOLLOWUP_AFTER_DAYS` overlap, parse differently, and fail silently on a typo.
6. **17 of ~30 modules touch the DB directly.** No repository boundary.

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
- `TAILOR_AGGRESSIVE=1` — resumes mirror the JD, fabrication judge off. Deliberate.
- 899 LinkedIn connections imported. Secrets in `~/.applypilot/` are `chmod 600`; FileVault on.

## Dev workflow

```bash
.venv/bin/python -m pip install ".[gmail]" --quiet     # after ANY source edit
lsof -ti:8765 | xargs kill -9 && .venv/bin/applypilot dashboard --serve --no-open
APPLYPILOT_DIR=$(mktemp -d) PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
.venv/bin/python -m ruff check .
```

The `APPLYPILOT_DIR=$(mktemp -d)` prefix is **required** — without it tests run against your
real `~/.applypilot/`. The editable install is flaky; reinstall then restart the dashboard,
and hard-refresh the browser after frontend edits.

- Big/risky designs get an adversarial multi-agent review first (Workflow). It has caught 13
  issues on the networking PRD, the agent-browser blocker, and 5 blockers on the extension PRD.
- Validator **warnings are never printed** — they only land in `{prefix}_REPORT.json`.
- Severity ladder: `preserved_companies`/`preserved_school` missing = **error** (blocks);
  `preserved_projects` missing = warning; banned words = strict-mode only.
- Working tree currently has **24 uncommitted files** — the whole of the 2026-07-28 session.

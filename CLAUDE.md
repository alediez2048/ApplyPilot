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
- **Tests:** 764 passing (`tests/`, 50 files) · ruff clean (line-length 120, py311) · ESLint clean
- **Schema version:** 1 (`applypilot migrate --status`) · **Settings:** 41 declared in `settings.py`

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
- `applypilot migrate --status` · `applypilot doctor --config` — schema version, settings

**Co-pilot is one-at-a-time.** It ends by handing an open browser to a human, and starting
another apply closes that browser. The queue refuses to start while a review is pending and
stops the moment a job hands over — see §Lessons 8, which cost two filled applications.

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
| `database.py` | SQLite layer. Owns `jobs` + `job_events`. Thread-local WAL, additive column pass, then numbered migrations. `get_connection()` returns a subclass carrying a per-connection schema memo — see §Lessons 11. |
| `llm.py` | Multi-provider client (round-robin + failover: OpenAI/Gemini/Anthropic/local). |
| `view.py` | Static HTML results export. |
| `web_dashboard.py` | **The operator dashboard.** 2,362 lines, **zero SQL** — data access goes through `repo/` and `store.py` (ARCH-4). |
| `repo/jobs.py` | Every `jobs` query as a named function. Owns `QUEUE_SQL` (what counts as an operator-added job). |
| `scoring/resume_sections.py` | Parses the BASE résumé into its own sections. The base résumé is the template; tailoring rewrites content inside it. |
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

**The header states the TARGET role**, the employment history keeps its real titles. The
summary must not open by restating a previous job title — a résumé aimed at "Applied AI
Engineer" that begins "Technical Project Manager with 10+ years" tells the reader they have
the wrong document. Employment titles are background-checkable and never move.

**Worked examples in the prompt must be OFF-DOMAIN.** Three separate times an example written
in the candidate's own field came back almost verbatim: a bullet example became his opening
bullet, a summary example became his summary, and an example's "Eight years" turned "10+ years"
into "Seven years". `test_no_prompt_example_can_be_lifted_into_the_resume` checks every
illustrative block against his own vocabulary.

**The base résumé is the template** (`resume_sections.py`, 2026-07-29). Its section titles and
order flow through tailor → `_DATA.json` → the renderer. Tailoring rewrites content *inside*
those sections and may not rename, drop, reorder or invent one; a section the model omits falls
back to the original, and bullets are padded to the original count from the trailing originals
(NOT merged by similarity — a real rewrite doesn't resemble its source, so prefix-matching
turns 3 rewrites into 6 duplicated bullets). Cover letters must name the employer.

### `apply/` — autonomous submission
`launcher.py` (acquires jobs, spawns Chrome + Claude Code per job) · `chrome.py` ·
`prompt.py` · `dashboard.py` · `pause.py`. The agent is **tool-scoped**: `--allowedTools
mcp__playwright,mcp__gmail__send_email` plus a hard deny-list. It browses attacker-controlled
careers pages, so inbox read is denied twice (allowlist + deny-list) — pinned by a test.
**Do not grant it inbox read for account verification codes**: it would turn a prompt injection
on a careers page into a mailbox-exfiltration path. Registration is the human's job (§The
human-in-the-loop apply model).

`pause.py` is the ⏸ handover: a **file** flag in `APP_DIR`, because the apply runs in a
separate OS process from the dashboard, polled once per agent action in `run_job`'s stdout
loop. Three guards keep a flag from outliving its run — the loop consumes what it acts on,
`main()` clears a stale one at startup, and the endpoint sets nothing when no job is
`in_progress`. A leftover would pause every future apply the instant it began.

The agent is spawned with `start_new_session=True`. Without it `_kill_process_tree`'s
`killpg` walks up to *our own* group — see §Lessons 17. `_reap_agents_on_exit` (atexit) stops
it outliving the run now that it no longer dies with us.

`AGENT_TIMEOUT_SECONDS` (`APPLY_AGENT_TIMEOUT`, default **900**) was a bare `timeout=300`
literal; a real Deloitte fill took 208s, so long forms were killed mid-application.

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
| `conversations.py` | Who is on a thread, who was just introduced, **who owes whom a reply** (`conversation_state`), and who a reply must reach (`reply_target` — the Cc is carried forward, never rebuilt). |
| `intent.py` | CRM-4b. What a reply wants — rejection / not now / interested / introduction / question / auto-reply — from a ~200-char snippet. Rule-based and quick to say `unknown`. |
| `metrics.py` | CRM-2 funnel + reply rates. Every rate carries its `n`; bounces leave the denominator. |

**Adding a channel (e.g. SMS) is one `Channel` entry plus one prompt** — executed, not
claimed: `test_adding_a_channel_needs_no_schema_change` defines an SMS channel that exists
nowhere in the codebase and drives it end to end. Readiness is data too (`ready=(("phone",
None),)`), which is what removed the last `if channel is EMAIL` from `_is_ready`.

### `networking/` — contacts, outreach, follow-up
| File | Role |
|------|------|
| `service.py` | Orchestrator: derive → search → rank → verify → persist → draft. COLD (Apollo) + HOT (your connections) layers. |
| `store.py` | The `contacts` table (42 cols, own migration in `_CONTACT_COLUMNS`). Atomic claims for every send. |
| `derive.py` | Job URL → employer name + domain. ATS path slugs, board rejection, tenant subdomains. |
| `providers.py` / `apollo.py` | Apollo is the sole provider (paid plan). `resolve_orgs()` disambiguates fuzzy name search; `confirm_employer_domain()` recovers a missing domain. |
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
| `messages` | `networking/messages.py` | **CRM-4 conversation memory.** Thread HEADERS only — no body/snippet column exists, and a test asserts it. Keyed by Gmail's message id, so re-syncing is a no-op. `rfc_message_id` is what lets a reply chain `References` across the whole thread. |
| `job_events` | `database.py` | Per-job activity log. Append is best-effort, never raises. |

| `schema_migrations` | `migrations/` | Version, status, `claimed_at` lease. See §Lessons on the 300s lease. |

Live counts (2026-07-31): jobs 15, contacts 51 (**33 emailed, 2 replied, 1 bounced**),
messages 36, connections 899, job_events 232, touches 10, sequences 13. Schema version 1.

**The second reply arrived on its own** — Gina Johnson at Salesforce, 2026-07-31, detected by
the CRM-1 background poller with nobody watching. That is the whole point of the heartbeat:
before CRM-1 the only recorded reply in the database had been typed in by hand.

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
- `⋯` row menu holds destructive actions (rejected, delete). It is anchored `right:0` and
  flips up near the bottom: `.table-wrap` clips with `overflow:hidden` to round the table's
  corners, so an absolutely-positioned menu is CUT, never scrolled to (it rendered as "✕ Ma",
  "🗑 De"). Whether a row is the last one is runtime geometry, so `positionRowMenu()` measures.
- Row actions added 2026-07-30: **🔐 Sign in first** (`/api/signin`, `/api/signin-done`) and
  **⏸ Pause & take over** (`/api/pause-apply`). Pause is NOT `/api/stop` — stop `killpg`s the
  run and takes Chrome with it, destroying a part-filled form.
- **`network_note` renders under the Find-contacts button.** `/api/status` had always sent it
  and no JS read it, so a search that ran, spent credits and kept nobody looked exactly like a
  button that never fired.

The 2.5s refresh replaces `#jobs` wholesale, so `refresh()` **skips while any input in that
subtree has focus** — otherwise it eats what you're typing.

`/api/status` costs **50 SQL statements** (was 313 before ARCH-4 measured it — 199 of those
were `CREATE TABLE IF NOT EXISTS` re-run every request). `tests/test_query_budget.py` holds
the line and fails on a per-contact N+1. Batch a new query; don't raise the budget.

**That budget counts SQL and nothing else**, which is exactly how CRM-4a slipped a Gmail HTTP
call per job onto this path and took the endpoint to **2.4s against a 2.5s refresh** — see
§Lessons 26. Now **0.043s**, with a test that counts network round-trips too. Anything you add
here that touches the network needs the same treatment.

---

## Follow-up sequences

Two independent ladders, both human-in-the-loop. Nothing auto-sends.

| | Email | LinkedIn |
|---|---|---|
| Anchor | `submitted_at` → last `touches.sent_at` | `dm_sent_at` → last `touches.sent_at` |
| Default | `FOLLOWUP_SCHEDULE=48,96,168` (2d/4d/7d) | `LINKEDIN_FOLLOWUP_SCHEDULE=120,288` (5d/12d) |
| Send | `gmail_send.send_followup()`, threaded | copy → open profile → you paste → `✓ I sent it` |
| Stop | reply / stop / sequence complete | same |

**Every outreach email offers the intro deck** (`INTRO_DECK_URL`, default
`https://www.jorgealejandrodiez.com/intro/`) — cold email and all three follow-up touches.
Both prompts are told the URL and the wording, AND `ensure_intro_deck()` appends the exact
sentence if the model drops it: a prompt instruction is not a guarantee (§Lessons 9, 12). It is
idempotent, tolerates a missing trailing slash or a wrapped URL, and inserts **above** the
sign-off — a link under "Thanks, Alejandro" reads as a footer. LinkedIn notes never get it
(300-char cap, and LinkedIn penalises links), same as the scheduling link. Distinct from
`INTRO_DECK_PATH`, which *attaches* a PDF.

**Warm (hot-layer) copy opens by naming the gap and the employer** on BOTH channels — "Hey
Gina, long time without connecting, hope everything is well at Salesforce" — and is told never
to re-introduce the sender to someone who already knows them.

Per-touch prompts differ by position: touch 1 adds something new, touch 2 offers a redirect
("is someone else the right person?"), touch 3 says plainly it's the last one. All are told
to give an explicit out and never to restate the previous message. LinkedIn copy is much
shorter (it lands in a chat window) and must ask exactly one answerable question.

**Threading works with no extra OAuth scope**: Gmail returns `threadId` on the send response
and we generate the RFC `Message-ID` ourselves — both persisted at send time. As of 2026-07-29
**all 13 sent emails have both ids and all 13 threads resolve live** against the mailbox, so
`backfill_thread_ids()` has nothing left to recover.

**Not built:** no scheduler (nothing fires while the dashboard is closed), no reply
detection, **no per-company cap** — 5 contacts × 3 touches is 15 emails at one company.

---

## Résumé + cover letter generation

**The base résumé (`~/.applypilot/resume.txt`) is the template.** `resume_sections.py` parses
its sections and they flow through tailor → `_DATA.json` → the Node renderer. Tailoring
rewrites content *inside* those sections and may not rename, drop, reorder or invent one.

| Guarantee | Enforced by |
|---|---|
| No section lost | assembler falls back to the original when the model omits one |
| No bullets lost | padded to the original COUNT from the trailing originals |
| Bullets actually rewritten | `verbatim_bullets()` — experience only, warning |
| Named tools survive | `skills_boundary` ∩ base résumé, warning |
| Experience never understated | `understated_experience()` — **error**, retryable |
| Employer names / school present | preserved_* checks — **error** |
| Cover letter names the employer | `validate_cover_letter(company=…)` — **error** |

**Padding is by COUNT, not by similarity.** A genuine rewrite doesn't resemble its source, so
prefix-matching classifies every rewrite as new and appends the originals too — 3 rewrites
become 6 bullets saying the same thing twice.

**The header states the TARGET role; employment history keeps its real titles.** Employment
titles are background-checkable. The summary must not open by restating a previous title — a
résumé aimed at "Applied AI Engineer" that begins "Technical Project Manager with 10+ years"
tells the reader they have the wrong document.

**`TAILOR_AGGRESSIVE` is voice-only.** It used to force `validation_mode="lenient"`, disabling
the fabrication judge and every banned-word check. The real lever was the dashboard, which
hardcoded `lenient` in three places — so every dashboard run skipped the judge regardless of
the flag. Now `normal`, and validator warnings reach the job's **Activity tab** rather than
only `{prefix}_REPORT.json`.

**Worked examples in prompts must be off-domain** — see §Lessons 9. This cost three rounds of
rework and one factual error.

---

## Contact discovery: how the chain actually resolves (2026-07-30)

**Employer name.** An ATS host is never the employer, and an employer may also be a board:

| URL | Employer | Why |
|---|---|---|
| `ats.rippling.com/wander/jobs/…` | **Wander** | path slug; without a `rippling.com` rule it became **"Ats"** |
| `salesforce.wd12.myworkdayjobs.com/…` | **Salesforce** | tenant subdomain, not the ATS |
| `google.com/about/careers/…` | **Google** | own careers site, even though Google Jobs is a board |
| `ycombinator.com/companies/hamming-ai/…` | **Hamming AI** | YC hosts for others; employer is in the path |
| `ycombinator.com/jobs` | *(none)* | that is YC's listing index, not YC hiring |

`_BOARD_NAMES` = `_BOARD_SITES ∪ _BOARD_HOSTS` (they had drifted — "Greenhouse" passed as an
employer because it was only in the host set). A board name is accepted as the employer only
when `_company_owns_the_posting()` agrees: the company must be the **only** meaningful host
label, the path must name no other employer, and the path must look like a careers section.
`jobs`/`job` are deliberately NOT careers markers — on a board's own domain that is its product.

**Domain.** Board hosts yield none, and without a domain Apollo does a fuzzy NAME search.
`confirm_employer_domain()` guesses `<slug>.<tld>` and makes Apollo **corroborate** it: accepted
only if people at that domain report a matching employer name. A wrong guess returns `""`, never
a plausible lie. ("Wander" → 4 unrelated Wanders in Apollo; the real one, `wander.com`, is not in
the name search at all but has the CEO, CMO and engineers.)

**Selection.** Ranking scores TITLE relevance and knows nothing about the employer, while the
strongest verification signal (work-email domain) only exists after enrichment. So a whole batch
can be rejected while real colleagues sit further down the pool. `_TOPUP_ROUNDS` (3) keeps
walking the ranked pool when a batch is dropped, bounded because enrichment costs credits. And a
title filter matching nobody widens to the whole company — but only when the company is already
confirmed, never for an unanchored keyword search.

**Every exit logs.** A search that found nobody used to log nothing, making a completed run
byte-identical to a dead button (§Lessons 15).

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
   `test_dashboard_render.py` executes the render path under DOM stubs. ARCH-2 retired the
   parse-only test — building the `Function` there already throws on a syntax error. Note the
   render test calls functions DIRECTLY, which does not prove the browser can find them from
   an `onclick=` attribute; `test_every_inline_handler_resolves_at_global_scope` does that,
   and module scoping breaks 26 of 33 handlers.

8. **A co-pilot review dies the moment the next apply starts.** Launching an apply clears
   whatever holds the CDP port, so the browser the operator was asked to review in is closed.
   Measured: `21:24:30.724 Zello → ready_to_submit` then `21:24:31.152 Deloitte → in_progress`
   — **428ms**. It then happened again in reverse within the hour. The row still read
   `ready_to_submit`, claiming a form was waiting that no longer existed. Batching N jobs in
   co-pilot mode leaves every one un-reviewable except the last, and the filled form is
   unrecoverable. **Co-pilot is inherently one-at-a-time** — it ends by asking a human to act.
   Guarded at both ends now (refuse to start, and stop on handover).

9. **Never put a worked example in the prompt using the candidate's own domain.** It gets
   parroted. Three times in one session: a bullet example became his opening T-Mobile bullet
   (and then every bullet took that same shape), a summary example became his summary, and an
   example's *"Eight years"* turned a **10+ year résumé into "Seven years"** — a factual error
   understating him. Examples are off-domain with `N` placeholders, and
   `test_no_prompt_example_can_be_lifted_into_the_resume` checks each illustrative block
   against his real vocabulary.

10. **A `@react-pdf` layout error at one font size is usually fine at another.** textkit threw
    `Cannot read properties of undefined (reading 'overflowLeft')` at scale 1 and 0.97 and
    rendered the SAME content at 0.94 / 0.90 / 0.88 / 0.82 / 0.76 / 1.05. Treating the first
    failure as fatal discarded a good document, and the Python fallback then wrote a
    **380-character PDF missing WORK EXPERIENCE** — worse than an error, because it looks like
    a résumé. Three wrong hypotheses were tested first (em dashes, hyphenation, a bad
    character); the fix is not depending on one scale succeeding.

11. **Idempotent-at-the-SQL-level is not free.** `CREATE TABLE IF NOT EXISTS` made `init_*()`
    look safe to call from every read path. `/api/status` was running **313 statements per
    request** at a 2.5s refresh — 199 of them schema setup, 108 from `init_connections` alone
    (once per contact). Nothing failed, nothing was slow enough to notice, and only counting
    surfaced it. Now 50, with `tests/test_query_budget.py` holding the line.

12. **A validator can check for something the prompt just asked for.** `preserved_school` is
    `"Gauntlet AI; University of Texas"` — a concatenation appearing nowhere in the résumé. It
    passed for months because the prompt literally instructed the model to emit
    `"{school} | {level}"`. A check whose input is derived from its own demand cannot fail.

13. **WRITE THE TEST, THEN BREAK THE THING IT GUARDS.** Five vacuous tests shipped-and-were-
    caught in one session: the query-budget seed omitted `strategy` so `/api/status` returned
    an empty payload and every assertion measured nothing; a bullet-count test spanned to
    end-of-document and swept in other sections' bullets; a schema-convergence assert ended in
    `or True`; `years_claim` was digits-only and returned `None` for "Ten years", the phrasing
    the model actually writes. **Every one passed on first run.** Mutation testing is the only
    thing that found them.

14. **Filtering AFTER narrowing throws away the good candidates.** "Find contacts is not
    working" on Zello: Apollo returned 25 people, `rank.select()` cut that to the 5
    best-*titled*, verification then dropped all 5 as working elsewhere → zero contacts. The
    two real `@zello.com` recruiters were candidates 6–7 and were **never enriched or
    examined**. Ranking scores title relevance and knows nothing about the employer; the
    strongest verification signal (work-email domain) only exists *after* enrichment. So the
    narrow step and the correctness step were ordered so that being right produced nothing.
    Fixed by ranking the whole pool and **topping up from the rest when a batch is rejected**,
    bounded at `_TOPUP_ROUNDS` batches because enrichment costs credits.
    Root cause of the ambiguity: **Apollo lists THREE orgs named Zello/ZELLO, none with a
    `primary_domain`** — Lesson 5 with no domain available to disambiguate.

15. **A zero result must be as loud as an error.** The same incident: the search ran, spent
    credits, dropped everyone — and logged *nothing*. `log_event` was gated on
    `stored_contacts` being non-empty, and `network_note` was returned by `/api/status` and
    **rendered by no JS at all**. So a completed search that kept nobody was byte-identical in
    the UI to a button that had never been clicked. That is what made it undiagnosable, not the
    dropping. Every exit from `find_contacts_for_job` now logs.

16. **A same-second, same-byte-length edit can be invisible to Python's `.pyc` cache.**
    Reverting `_TOPUP_ROUNDS = 1` → `= 3` inside one second left the *cached bytecode* in
    force: `grep` said 3, the import said 1, and a passing test started failing for no visible
    reason. Bytecode staleness is checked by source **mtime + size**, and `1` and `3` are the
    same size. When a mutation test's result contradicts the file, clear `__pycache__` before
    believing either.

17. **`os.killpg` on a child you spawned kills YOU.** Pause was clicked on a live Deloitte
    application; the flag was consumed, the agent died — and the apply CLI died with it,
    `exit -9`. `_kill_process_tree` does `killpg(getpgid(pid))`, and the agent was spawned
    without `start_new_session`, so that group was *our own*. The handover was never recorded:
    job stuck `in_progress`, browser open, no Continue button. The feature looked like it did
    nothing when it actually did too much. Latent long before pause — the Ctrl+C skip paths
    call the same helper. Agents now get their own session, with an atexit reaper so they
    cannot outlive the run.

18. **`dict(zip(row.keys(), row))` on a DICT maps every key to itself.** `repo.find_by_any_url`
    already returns a dict, so the dashboard's Find-contacts button handed discovery
    `{"company": "company", "url": "url", …}` and searched Apollo for a company named
    "company". Verification then correctly dropped everything. The CLI path passed a real row
    and worked — which is exactly why it read as an Apollo *coverage* problem for days. Use
    `dict(row)`; it is correct for both `sqlite3.Row` and `dict`.

19. **A guard that only accepts one exact state will refuse a true correction.** A Salesforce
    application that was signed into and submitted BY HAND was recorded
    `failed:copilot_violation_agent_submitted` — the resumed agent truthfully reported it
    submitted, and co-pilot reads `RESULT:APPLIED` as a safety breach. Then "Mark submitted ✓"
    refused the fix, because it required `apply_status == 'ready_to_submit'` exactly. A real
    application was stuck as a failure with no UI path out. **On resume the human is at the
    keyboard by definition**, so APPLIED is expected there; a *fresh* co-pilot run submitting
    alone is still a real breach. And the operator is the authority on whether they submitted
    something — gate on "was this ever attempted", not on one state name.

20. **An employer can also be a job board, and an ATS host is never the employer.** Three
    variants of the same bug in one day: `google.com` was rejected as an employer because
    Google Jobs is a discovery source (17 known connections never searched);
    `ats.rippling.com/wander/...` imported as the company **"Ats"** and produced a cover letter
    addressed to nobody; `acme.breezy.hr` resolved to **"Hr"**. The employer is the tenant, not
    the vendor — match the *only* meaningful host label, and keep one shared TLD list.

21. **A derived field is not a column, and the gap is silent.** `emailed` is computed by the
    dashboard in `_contact_payload`; it does not exist in `contacts`. So `applypilot tick`,
    passing raw DB rows, saw every email ladder as "never used" and reported **0 follow-ups due
    while the dashboard showed 3**. Fixed at the ONE shared entry point
    (`followup_panel` normalises) so the two cannot drift. Same session, same class: reading
    `ladder.get("body")` when the key is `draft_body` made the idempotence check never fire —
    an hourly tick would have re-drafted and re-PAID for the same follow-up forever.

22. **Idempotence has to be tested by running it twice, not by reasoning.** A bounced contact
    stayed in the reply-polling pool, so every poll re-detected the same failure and appended
    another log line — an afternoon of ticks produced **11 identical BOUNCED entries** for one
    address. Terminal states must leave the pool.

23. **The pool you exclude from is as important as the one you poll.** Reply detection excluded
    anyone who had already replied — which is exactly BACKWARDS for conversation memory, since
    a replied contact is the one with a LIVE thread. Victoria's thread stopped being read the
    moment she answered, so the handoff could never be seen. Widen the read, gate the WRITE.

24. **A regex cannot split an email header.** `,(?![^<]*>)` breaks on a comma inside a quoted
    name: `"Loveless, David" <david@writer.com>` became two recipients, one of them the garbage
    address `"loveless`. Scan with quote/angle state instead. Related: storing bare addresses
    destroys the display name permanently — "David Loveless" degrades to "David", because the
    only fallback left is the local part.

25. **A test heuristic that matches the wrong thing pushes real code onto an allowlist.** The
    SQL-boundary test flagged `gmail_read.py` because it matched the string `.execute(` — which
    the Google API client also uses. That false positive is why `gmail_oauth.py` had been sitting
    on the exemption list for a rule it never broke. Detect SQL, not a method name.

26. **A query budget only counts queries.** `connected_email()` is an HTTP round-trip to Gmail
    (~0.12s), and CRM-4a called it **once per job** inside `/api/status` — which re-renders
    every 2.5 seconds. Measured at **2.4s per request with 15 jobs**: the dashboard was
    refreshing back-to-back and spending nearly all of it asking Gmail the same unchanging
    question. `tests/test_query_budget.py` passed the whole time, because none of it was SQL.
    Cached on the token file's mtime (a new token invalidates it, so reconnecting a different
    account cannot serve the old address) — **2.4s → 0.043s**. Lesson 11 with a different unit:
    the hot path is hot for *everything*, not just the thing you happened to instrument.

27. **Everything chased people who said NOTHING; nothing noticed the ones who answered.**
    Follow-up ladders, touch schedules, LinkedIn nudges — every signal in the system was built
    around silence. The opposite case is rarer and much worse: somebody replied and it sat
    there. It was live while CRM-4a was being finished — Gina Johnson at Salesforce replied and
    the row still read "1 follow-up due". An unanswered reply now outranks every ladder, and the
    pill is on the COLLAPSED row: a state you must expand a contact to discover is a state
    nobody sees for days.

28. **Measure the bug before fixing the bug the ticket describes.** CRM-4 said the introduced
    contact would have no ladder anchor and would "silently never follow up", and prescribed
    back-dating one to the introduction date. Checking the live Writer job showed the failure
    does not happen — no `sent_message_id` means the email ladder correctly does not apply (you
    cannot follow up on an email you never sent), and the checklist already counts him under
    `emailed 2/3` so the job reads *partial*, not finished. **Implementing the prescribed fix
    would have told the ladder we had emailed somebody we had not.** Two CRM tickets had already
    shipped factually wrong instructions; a ticket is a hypothesis.

29. **The dangerous half of a feature is the half that looks identical when it is wrong.**
    Replying in-thread either reaches David or it does not, and both outcomes render the same
    screen and log the same success. So the recipients are computed from the stored thread
    rather than posted by the browser, and the Cc is drawn as visible chips — the operator has
    to be shown who a message reaches before they can meaningfully click Send. A bare
    `"david@writer.com" in html` assertion passed with the chips deleted entirely, because the
    address was also sitting in a hidden `data-cc` attribute; only matching the rendered chip
    caught it. Mutation testing found that, not review.

30. **A mutation harness that reports SURVIVED without running anything is worse than none.**
    Eleven mutations came back clean in one batch; all eleven ran zero tests, because zsh does
    **not** word-split an unquoted `$VAR`, so `pytest $TESTS` got one bogus path. It printed
    exactly what a perfectly-tested codebase prints. The harness now fails loudly on "no tests
    ran". Same session, third vacuous-test find: `classify("Sounds good?") != QUESTION` passes
    no matter what the question regex does, because `sounds good` matches `interested` and
    returns first — replacing the entire regex with a bare `\?` left every test green.

---

## The CRM phase (2026-07-30, branch `crm-phase-1`)

Shipped in one session, in this order: **CRM-3a → CRM-1 → CRM-2 → CRM-3b → CRM-4a.**
Tickets in `docs/tickets/CRM-*.md`; two of them had instructions that were factually wrong
before being revised (they told you to write `followup_status`, removed by ARCH-3).

| | What it does | Where |
|---|---|---|
| **3a** | `(2) ⚠ ApplyPilot` tab badge. Counts what is NEW since you last looked — a badge that counts every actionable row is permanently lit and trains you to ignore it. | `dashboard.js` |
| **1** | Reply detection. Gmail poll every 5 min + `📥 Check replies`. | `networking/replies.py`, `gmail_read.py`, `domain/replies.py` |
| **2** | Outcome metrics: funnel, reply rates, time-to-reply. | `domain/metrics.py`, `stats --outreach` |
| **3b** | `applypilot tick` — unattended heartbeat, `schedule --install` for launchd. | `tick.py`, `schedule.py` |
| **4a** | Conversation memory: thread view, handoff detection, add an introduced contact. | `domain/conversations.py`, `networking/messages.py` |

**It paid for itself on the first live poll.** CRM-1 found a **real reply nobody knew about**
(Writer, Jul 29) and an address that had been **bouncing silently since Jul 16** — follow-ups
were still scheduled against it. CRM-4a then found what that reply actually was: an
**introduction**. Victoria CC'd a colleague, and the system had recorded a boolean.

**Everything runs on `gmail.metadata`, never `readonly`** — headers, threads and participants;
it cannot read a body. Two consequences shape the whole design: `q=` search is unavailable, so
threads are listed by id and never queried; and there is no snippet, so what a reply SAID is
unknown.

**CRM-4b can lift that, and is OFF.** `gmail.readonly` reads every message in the mailbox, so
`CONTENT_SCOPE` is deliberately **not** in `SCOPES` — no future scope addition can drag it
along, and a test pins that. Enabling it is `network --gmail-connect --with-content`, which
prints the trade before the browser opens. With it on, only ~200-char snippets of replies to our
own outreach are stored (`SNIPPET_MAX`, truncated **at the write**, never at the caller), and
`domain/intent.py` labels them so a rejection offers *Mark rejected* rather than *draft a
follow-up*. Revoking it stops adding content and **destroys nothing already stored** — which
took real work, because `upsert_messages` is INSERT OR REPLACE and an hourly `tick` re-syncs
every open thread.

**Bounces are not replies, and not non-answers.** A bounce arrives inside our own thread, so
thread-id matching accepts it happily; counting it as a reply stops the ladder and inflates
every rate. Detected separately, the address is marked `bounced`, and CRM-2 excludes it from
every denominator — it never arrived, so "emailed, no reply" would be a lie.

**Introductions are surfaced, never auto-created.** A contact created from a thread is one an
automated ladder would then EMAIL, and threads collect schedulers, assistants and ATS robots.
`conversations.is_robot()` filters the obvious ones; the operator confirms the rest.

**Replying in-thread keeps the Cc** (`domain/conversations.reply_target()` → `send_reply()` →
`/api/contact/reply`, 2026-07-31). It answers the **last inbound** message, not the last message
overall — after we reply, the newest message is ours, and reading recipients off it loses anyone
added since. `References` chains the whole thread. A thread with **no** inbound message returns
`None` rather than falling back to the contact's address: that case is a *follow-up*, which has
a ladder, a schedule and stop conditions, and quietly answering it as a "reply" bypasses all
three. The endpoint takes recipients from the stored thread and ignores any `to` the browser
sends; the operator may drop a Cc, never invent one. §Lessons 27.

**`tick` never sends, never starts an apply, and never touches `apply.pause`.** Each is a test.
An unattended apply would fill a form nobody is there to review and close whatever review
browser is open; writing the pause flag would pause a live application.

## The human-in-the-loop apply model (2026-07-30)

**The agent never submits. The operator always does.** Every path ends at `Mark submitted ✓`.

| Ending | `apply_status` | Browser | Operator's move |
|---|---|---|---|
| Filled, waiting | `ready_to_submit` | **open** | review → Mark submitted ✓ |
| Auth / account wall | `needs_human:login` | **open** | register → Continue |
| Captcha, stuck field | `needs_human:*` | **open** | resolve → Continue |
| ⏸ Pause & take over | `needs_human:paused` | **open** | anything → Continue |
| Ran past `APPLY_AGENT_TIMEOUT` | `needs_human:timeout` | **open** | finish → Continue |
| Agent stopped with no verdict | `needs_human:no_result_line` | **open** | check — may be done |
| Expired / not eligible / captcha-dead | `failed` | closed | Re-apply or reject |

**Anything a human can fix keeps the browser.** Only genuine dead ends close it. `Continue`
runs `apply --copilot --resume`, which reconnects on the live CDP port
(`resume_now = resume and chrome_alive_on_port(port)`) and falls back to a fresh launch only
when the window is gone.

**🔐 Sign in first** opens that same persistent profile with **no agent attached**, so an
account is created deliberately *before* a run rather than discovered mid-form. Sessions
persist (830+ cookie hosts), so it is once per employer. It refuses while an apply or review
browser is live — Chrome cannot share a `user-data-dir`.

**Not built: any notification.** The dashboard self-refreshes every 2.5s, but there is no
sound, desktop notification or tab-title badge. If the operator is in another tab, a filled
application waits indefinitely — and the longer it waits, the likelier something closes it.

---

## Where the work goes next

`docs/architecture-prd.md` — current state, the architecture grilling, and the plan.
`docs/tickets/ARCH-README.md` — the ordered ticket list. Read that before starting anything.

**The ARCH set is complete** (`ARCH-1` … `ARCH-6`, all ✅ 2026-07-28/29). `ARCH-4` was
deliberately narrowed — see its ticket.

**The CRM set is complete and CLOSED** (`CRM-3a`, `CRM-1`, `CRM-2`, `CRM-3b`, `CRM-4a`,
`CRM-4b` — 2026-07-30/31 on branch `crm-phase-1`). `CRM-4b` is **built and switched off**: it
needs `gmail.readonly`, which reads the whole mailbox, so enabling it is a deliberate act
(`network --gmail-connect --with-content`). Everything passes with it off, which is how this
machine runs.

**Open:** `DISC-1` — discovery has produced **0** jobs; all 15 were pasted by hand. It is now
the only open ticket, and by a wide margin the biggest gap: everything downstream of discovery
works end to end, on jobs the operator has to find themselves.

`CRM-1` (reply detection) is the one that changes what the app can *do*: 13 emails sent, 7
follow-ups, **1 reply recorded — typed in by hand.** Everything it needs is already stored
(`thread_id` + `rfc_message_id` captured at send time, all 13 verified live against the
mailbox); the only missing piece is reading. The `gmail.metadata` scope is now granted.

The ARCH-first order was chosen against the analysis's advice. The reason it was contentious
is still true and worth remembering: **the ARCH set delivered no user-visible change.** All 9
jobs were pasted in by hand (discovery has produced **0**), and the system is still blind to
replies. The tradeoff was raised and accepted; don't re-litigate it, don't forget it either.

What the ARCH set *did* buy became visible immediately afterwards: the first real end-to-end
apply surfaced three bugs (§Lessons 8–10), and every one was diagnosable in minutes because
the data layer, the query budget and the validators existed to measure against.

`docs/crm-prd.md` — the multi-campaign "Spaces" direction. **After** the above.

## Known debt

The 2026-07-28 architecture review listed six items; **all six were closed by the ARCH set**
(ARCH-1 `domain/`, ARCH-2 static frontend, ARCH-3 `touches`, ARCH-4 repository boundary,
ARCH-5 migrations, ARCH-6 settings). Per-ticket detail and the reasoning lives in
`docs/tickets/ARCH-*.md` — those write down what was *not* done and why, which matters more
than the checkmarks.

What is actually open now, ordered by leverage:

1. **Apply runs synchronously inside the HTTP request thread.** `run_dashboard_restart` →
   `subprocess.run` blocks a dashboard worker for minutes and dies with the server. This is the
   root cause of the two applies lost on 2026-07-29, and it is only *mitigated*:
   `release_stale_locks()` cleans up the orphan lock, and the co-pilot queue guard stops the
   batch from closing a pending review. Making it a real background task is the actual fix and
   is still open. **Check for `in_progress` before restarting the dashboard** (§Dev workflow).

2. ~~**The system is blind to replies.**~~ **CLOSED 2026-07-30 by CRM-1.** Kept for the
   lesson: it was the highest-value gap in the repo for two days, and the first poll after
   shipping found a real reply and a silent bounce. The old text read: **33 emails sent** across 50 contacts as of 2026-07-30,
   and still exactly one reply — recorded by hand. Follow-ups nudge people who may have replied
   days ago, and no funnel metric is possible. This gets worse with every send: it was 13
   emails this morning. `CRM-1`, and by some distance the highest-value thing left.

2a. ~~**Nothing tells the operator an application is waiting.**~~ **CLOSED by CRM-3a** (tab
   badge). Desktop notifications are still not built. The old text read: Co-pilot ends by handing over a
   browser, the queue stays paused until they act, and the dashboard has **no notification of
   any kind** — no sound, no desktop notification, no tab-title badge. It only self-refreshes
   every 2.5s, which helps only if you are looking at it. A filled form left sitting is a form
   that eventually gets closed by a restart. A tab-title badge is near-free and covers most of
   it; a desktop notification behind an opt-in toggle covers the rest.

3. **Discovery has produced 0 jobs.** ~2,500 lines of working, tested discovery (JobSpy across
   five boards, 48 Workday portals, an AI scraper) and every one of the **13** jobs was pasted
   in by hand. A configuration and trust problem, not a code problem — `DISC-1`.

4. **15 modules still execute SQL directly** (was 21) — `apply/launcher.py`,
   `enrichment/detail.py`, `view.py`, `cli.py`, `pipeline.py`, the three discovery scrapers,
   three scoring stages, and four `networking/` modules. The dashboard is at zero.
   `test_sql_lives_only_in_the_data_layer` names the remainder in an allowlist, so the list can
   only shrink and no NEW module can join it. Deliberately deferred; see ARCH-4's ticket.

5. **`web_dashboard.py` is 2,362 lines**, all Python, zero SQL. ~430 lines are pipeline
   orchestration (`run_dashboard_prepare/apply/fill_one/restart/continue`) that are not HTTP
   concerns. Extracting them is the natural companion to debt item 1.

6. **No per-company outreach cap.** 5 contacts × 3 touches is 15 emails at one company.

7. **`@react-pdf` is a major version behind** (3.4.5 installed, 4.5.1 current). The textkit
   layout crash in §Lessons 10 may be fixed upstream; the renderer now survives it either way,
   so this is cleanup rather than a fix.

8. **Résumé quality is measured but not judged.** `verbatim_bullets`, `understated_experience`
   and the dropped-tool check are mechanical. The LLM fabrication judge now *runs* (ARCH-6 era
   fix: the dashboard had hardcoded `lenient` in three places) but has not yet caught a real
   fabrication — it is unproven, not trusted.

---

## Environment (this machine)

- **Apollo.io** (paid Basic) is the sole contact provider. Hunter removed.
- **Gmail OAuth** connected, sends from `jorgealejandrodiezm@gmail.com`. **All three scopes
  granted and verified on the token (2026-07-29):** `gmail.send` + `gmail.metadata` +
  `gmail.settings.basic` — **metadata, not readonly**: headers and threads, *cannot read
  message bodies*. Signature fetch works (5,172 chars). `doctor` reports missing scopes, and
  `_load_creds()` loads with the token's own scopes so an older token keeps working.
- **Base resume** = `~/.applypilot/resume.txt` — PERSONAL STATEMENT / WORK EXPERIENCE /
  EDUCATION / KEY STRENGTHS, 3 employers (T-Mobile 5 bullets, Verizon 5, Kordami 4), 4,341
  chars, claims 10+ years. `preserved_companies` = T-Mobile/Verizon/Kordami; a preserved
  company missing from the output is a **hard validation failure**.
  `preserved_school` is `"Gauntlet AI; University of Texas"` — a CONCATENATION that appears
  nowhere in the résumé, so it is split and checked school by school (§Lessons 12).
  **Still open:** `profile.json` says "Diez" while the résumé says "Diez Magni" (the résumé
  wins — the assembler reads the header from it), and the résumé lists
  `alediez2408@gmail.com` while sending happens from `jorgealejandrodiezm@gmail.com`, so the
  .txt and the PDF show different addresses. Decide which one recruiters should reply to.
- `TAILOR_AGGRESSIVE=1` — voice only; see §Résumé + cover letter generation.
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
- Severity ladder: `preserved_companies` / `preserved_school` missing, understated years, and
  a cover letter that never names the employer = **errors** (block + retry).
  `preserved_projects` missing, verbatim bullets, dropped tools, banned words = **warnings**
  (surface in Activity, never block). Banned words become errors only in `strict`.
- **Never `pip install` while an apply is running.** The apply is a live subprocess; rewriting
  site-packages under it is how you get a half-loaded module. Wait for it to exit.
- **Never chain `pip install` with backgrounding the dashboard in one command.** That command
  shape hit the 2-minute tool timeout and was killed mid-write, leaving site-packages with no
  `applypilot/__init__.py` and no dist-info `RECORD` — so even `pip --force-reinstall` refused.
  The dashboard kept serving (its modules were already imported), so ONLY the buttons that
  spawn subprocesses broke: Re-apply, Prepare, Fill, Continue all died with
  `ImportError: cannot import name '__version__' from 'applypilot' (unknown location)`, buried
  in a command log nobody opens. Recovery: `rm -rf` both directories in site-packages, reinstall.
  Run the install alone, and verify with `python -c "import applypilot; print(applypilot.__version__)"`.
- **Check for in-flight applies before ANY restart or reinstall.** The apply is a child of the
  dashboard, so it dies with the server. This happened three times on 2026-07-30 alone.
- Working tree clean. **`main` is at `stable-crm-20260731`** — the CRM phase is merged and
  pushed. Tags: `stable-arch2/3/5/6` · `stable-e2e-20260730` · `stable-crm-20260731`.
- **A tag restores CODE only.** `~/.applypilot/` — 15 jobs, 51 contacts, 33 sent emails, 36
  stored messages, 899 connections — is not in git and needs its own backup
  (`~/.applypilot/backups/`). Nothing does this automatically. Latest:
  `applypilot-20260731-crm-phase-closed.db`. Use the **sqlite3 backup API, not `cp`** — the WAL
  routinely holds more than the main file does (4.1 MB against 688 KB once), so a file copy
  silently loses everything recent. Still unprotected and not in git: `resume.txt` (the template
  every tailored résumé derives from), `profile.json`, `.env`, `gmail_token.json`.

# PRD — ApplyPilot Architectural Refactor

**Status:** Draft v1 · **Date:** 2026-07-28
**Owner:** Jorge · **Author:** Jorge + Claude
**Ticket prefix:** `ARCH-*`
**Companion:** `docs/crm-prd.md` (product direction — Spaces / multi-campaign CRM)

> **Read this first.** This document covers *how the code is built* **and** the product work
> that has to interleave with it. An earlier draft sequenced the architecture first and
> nothing else; that was wrong, and §4 explains why.
>
> **The correction, in one line:** the refactor makes the code easier to change and improves
> nothing a user can see — by its own guardrail, "no behaviour changes inside a refactor
> ticket." Shipping all six ARCH tickets would leave you with cleaner code and exactly the
> same number of interviews. Architecture is table stakes; §4.1 is the level-up.

---

## Part 1 — Current state

### 1.1 What it is

An **AI job-search CRM for a single operator**. It runs the whole loop:

```
find a job → score it → tailor a résumé + cover letter → submit the application
           → find real people at that company → draft outreach → send it
           → follow up until someone replies
```

It began as a bulk auto-applier that used an LLM-driven browser to submit at volume. It is
now something different in kind: a system of record for a job search, where the automation
is in service of *tracked relationships* rather than throughput.

### 1.2 Purpose and design stance

Three commitments distinguish it from a generic outreach tool, and every one of them was
learned the hard way:

1. **Human-in-the-loop by default.** Nothing sends itself. `--copilot` fills an application
   and stops. Follow-ups are drafted and queued, never fired. LinkedIn is copy-paste only.
   Full automation exists (`apply`) but is the exception, not the design centre.
2. **Local-first.** SQLite, localhost dashboard, no server, no accounts, no telemetry. All
   secrets live in `~/.applypilot/`, outside the repo, `chmod 600`.
3. **Truthful materials.** The tailor stage may reframe but not fabricate; `preserved_companies`
   and `preserved_school` are hard validation errors. (`TAILOR_AGGRESSIVE=1` relaxes tone
   matching, deliberately, but never the preserved entities.)

### 1.3 Tech stack

| Layer | Choice | Note |
|---|---|---|
| Language | Python ≥ 3.11 | `src/` layout, Hatchling |
| CLI | Typer + Rich | `applypilot` console script |
| Storage | SQLite (WAL, thread-local conns) | 4 tables, forward-only migrations |
| Web UI | `http.server.ThreadingHTTPServer` | no framework; HTML/CSS/JS in Python strings |
| Scraping | httpx + BeautifulSoup + Playwright | 3-tier cascade, ATS APIs first |
| LLM | Multi-provider (OpenAI / Gemini / Anthropic / local) | round-robin + failover |
| PDF | Node + `@react-pdf/renderer` | subprocess, `npm install`ed at runtime |
| Automation | Claude Code CLI + Playwright MCP + real Chrome | tool-scoped |
| Contacts | Apollo.io (paid) | sole provider |
| Email | Gmail API (OAuth) or SMTP | OAuth preferred |

**Runtime dependencies: 8.** That leanness is a genuine asset — keep it.

### 1.4 Architecture

**Core abstraction — a pipeline over one table.** Each stage reads rows in one state and
writes columns that advance them. Stages are idempotent and independently runnable.

```
discover → enrich → score → tailor → cover → pdf → [apply]
                                                   [network → outreach → follow-up]
```

**Subsystems:** `discovery/` · `enrichment/` · `scoring/` (+ Node `resume_renderer/`) ·
`apply/` · `networking/` · `wizard/` · `extension/` (MV3 popup).

**Data model** — 4 tables, each owning its own migration:

| Table | Cols | Owner |
|---|---|---|
| `jobs` | 32 | `database.py` — the state machine |
| `contacts` | 42 | `networking/store.py` |
| `connections` | — | `networking/connections.py` |
| `job_events` | — | `database.py` — activity log |

**Correctness layer** (added 2026-07-28): `networking/verify.py` checks each contact at
runtime; `evals/resolution.jsonl` scores the resolution chain offline.

### 1.5 Baseline metrics

```
source                18,978 LOC across 46 modules
tests                  3,030 LOC, 228 tests, 19 files      ratio 0.16 : 1
web_dashboard.py        3,710 LOC  ← 20% of all source, 76 functions
  serve_dashboard()     1,633 LOC  ← one function (the HTML template)
  embedded JavaScript     996 LOC  ← no linter, no types, no modules
  embedded CSS            519 LOC
  raw SQL                    40 statements (= store.py, whose job is SQL)
modules touching the DB    17 of ~30
env vars                   40, unvalidated
runtime dependencies        8
```

### 1.6 What is genuinely good (do not "refactor" these)

- **The 6-stage idempotent pipeline.** Any stage re-runnable in isolation is why any job can
  be restarted from any point. This is the best idea in the codebase.
- **Self-migrating column dicts** (`_ALL_COLUMNS`, `_CONTACT_COLUMNS`). ~15 schema changes in
  one session, zero migration friction.
- **Tool-scoping the apply agent.** It browses attacker-controlled pages; allowlist + deny-list
  + a test asserting `send_email` is its only Gmail capability.
- **`verify.py` + `evals/`.** Runtime self-checking for the unknown, a scored set for
  regressions. Correct layering; better than most production systems have.
- **8 runtime dependencies.** Resist adding more.

---

## Part 2 — The grilling

Five questions, the evidence behind each, and the verdict.

### Q1. Why is the presentation layer also the domain layer?

`_job_checklist()` and `_followup_panel()` decide what work exists — the most
business-critical logic in the system. Both live in the HTTP server module, beside CSS.

**Evidence:** the eval harness must `import web_dashboard` to test *scheduling rules*.
Three of one session's bugs originated here: a duplicated `rowMenu` that removed the
Re-apply button, a naive-timestamp crash that 500'd `/api/status` and blanked the dashboard,
and JS runtime errors that required inventing a DOM-stub harness to detect.

**Verdict: the most serious problem in the codebase.** If testing a domain rule requires
importing a web server, it isn't a domain rule — it's a view helper that got promoted.

### Q2. Why does `contacts` have 42 columns and two of everything?

```
followup_count   followup_message   followup_status   followed_up_at
li_followup_count  li_followup_message  li_followup_status  li_followed_up_at
```

**Verdict: one concept copy-pasted per channel.** A touch sequence is a touch sequence.
Adding SMS makes it three; adding Spaces makes it a cartesian product. The counter-argument
(wide tables are fast and obvious at 28 rows) is real, but the cost isn't storage — it's
duplicated *scheduling code*, and that's the expensive kind.

### Q3. Is 996 lines of JavaScript in a Python string a decision or an accident?

An accident that became load-bearing. No linter, no type checker, no module boundaries, no
dead-code detection.

**Evidence:** `test_dashboard_js_valid.py` (does it parse?) and `test_dashboard_render.py`
(does it throw?) both exist *solely* to compensate for tooling a `.js` file gives free.

**Verdict: move it.** The only real loss is f-string interpolation, which is barely used.

### Q4. What happens when a column must change, not just be added?

Forward-only: add a key, it migrates. No rename, drop, backfill, version, or down-migration.

**Evidence:** fixing `preserved_companies` and re-rendering the stale `.edu` cover letters
each required a hand-written one-off script.

**Verdict: acceptable now, not at CRM scale.** Needs a version table before Spaces.

### Q5. Forty environment variables and no schema?

`FOLLOWUP_SCHEDULE`, `LINKEDIN_FOLLOWUP_SCHEDULE`, and `FOLLOWUP_AFTER_DAYS` overlap, parse
differently, default independently, and **fail silently** on a typo.

**Verdict: real but low-severity.** Fix opportunistically, not as a project.

---

## Part 3 — The refactor

Six tickets. Each is independently shippable and leaves the app working.

### ARCH-1 — Extract `applypilot/domain/` ★ highest leverage

Move business rules out of the web layer into pure, dependency-free functions.

```
domain/
  checklist.py     completion state for a job
  followup.py      ONE ladder engine (channel-parameterised)
  verification.py  re-home networking/verify.py
  types.py         TypedDicts for Job, Contact, Touch
```

Rules: no imports from `web_dashboard`, `http`, or `sqlite3`. Dicts in, dicts out. The web
layer loads rows and calls these; it does not compute.

**Acceptance:** `grep -rE "^(import|from).*(web_dashboard|http|sqlite3)" src/applypilot/domain/`
returns nothing · `test_eval_resolution.py` imports only `domain` · the three duplicate
"is it due" implementations collapse to one · `web_dashboard.py` drops below 3,300 lines.
**Effort:** 1 day. **Risk:** low — pure moves plus one merge.

### ARCH-2 — Frontend to static files

```
src/applypilot/static/{dashboard.js, dashboard.css, index.html}
```

Served by the same localhost server; no build step, no bundler, no framework.

**Acceptance:** zero JS/CSS in `.py` files · ESLint runs in CI · `test_dashboard_js_valid.py`
retired (the linter subsumes it) · `test_dashboard_render.py` kept — it tests behaviour, not
syntax · `web_dashboard.py` drops below 1,800 lines.
**Effort:** 1 day. **Risk:** low, mechanical. Caching gets a cheap `?v=` cache-buster.

### ARCH-3 — `touches` table

```sql
touches(id, contact_id, channel, seq, due_at, sent_at, subject, body, status)
```

Collapses eight `contacts` columns; makes SMS free; one ladder implementation.

**Acceptance:** `li_followup_*` and `followup_*` gone from `_CONTACT_COLUMNS` · both ladders
run through one code path · backfill migrates all 28 existing contacts with no data loss ·
`contacts` drops to ~32 columns.
**Effort:** 1 day. **Risk:** medium — the first real data migration. Ship with a backup
step and a dry-run that prints the diff before writing.

### ARCH-4 — Repository boundary

A thin `repo/` layer so only it holds SQL. Not an ORM — 8 dependencies stay 8.

**Acceptance:** `conn.execute` appears only in `repo/` and `database.py` · `web_dashboard.py`
contains zero SQL (from 40) · domain tests need no DB at all.
**Effort:** 1–2 days. **Risk:** medium — touches 17 modules. Do it *after* ARCH-1, which
removes most of the dashboard's need for SQL.

### ARCH-5 — Versioned migrations

A `schema_migrations` table plus numbered scripts. Keep the self-migrating column dicts for
additive changes (they work); add real migrations for rename/drop/backfill.

**Acceptance:** version recorded and asserted at startup · one backfill expressed as a
migration, not a script · `applypilot doctor` reports schema version.
**Effort:** half a day. **Risk:** low.

### ARCH-6 — Config schema

One `config/settings.py` with a typed dataclass, defaults, validation, and a `doctor` section
that prints resolved values. Collapse `FOLLOWUP_AFTER_DAYS` into `FOLLOWUP_SCHEDULE`.

**Acceptance:** a malformed value fails loudly at startup · `doctor` prints every setting and
its source (env / default) · no duplicate knobs for one concept.
**Effort:** half a day. **Risk:** low.

---

## Part 4 — Sequencing

### 4.0 The data that changed the plan

Measured 2026-07-28, on the live database:

```
jobs entered via discovery          0      ← the discovery tier is switched off
jobs entered by pasting a URL       7      ← all of them

contacts found                     28
emails sent                        13
follow-ups sent                     7
applications submitted              6
replies recorded                    1      ← recorded by hand; the system cannot see replies
```

Three conclusions, and they reorder everything:

1. **The system is blind.** The single most important signal in the product — *did anyone
   respond?* — is invisible to it. Every downstream decision is guessing: follow-ups nudge
   people who may already have replied, and nothing knows which of 13 emails worked.
2. **The best-built subsystem is idle.** ~2,500 lines of working, tested discovery (JobSpy
   across five boards, 48 Workday portals, an AI scraper) produced zero jobs. The top of the
   funnel is manual, and everything downstream of it is automated. That is backwards.
3. **Nothing is measured.** 13 emails, 7 follow-ups, no reply rate, no per-channel comparison,
   no idea whether aggressive tailoring converts. The events are in `job_events`; nothing
   aggregates them.

**The machine is now better than the input being given to it.** Six more days of refactoring
does not change that.

### 4.1 Revised sequence

Product work that makes the system *see, feed itself, learn, and act* comes first, with the
one refactor those depend on slotted in early.

| # | Ticket | What it changes | Size | Depends |
|---|--------|-----------------|------|---------|
| 1 | **CRM-1** Reply detection | The system can see. Stops nudging people who answered; unblocks every metric. | M (1d) | Gmail metadata scope |
| 2 | **DISC-1** Turn discovery on | The funnel stops being one human pasting URLs. | S (0.5d) | — |
| 3 | **ARCH-1** Extract `domain/` | The one refactor the product work needs — CRM-2 and CRM-3 both want testable scheduling. | M (1d) | — |
| 4 | **CRM-2** Outcome metrics | The system can learn. Reply rate by channel, company, template. | M (1d) | CRM-1, ARCH-1 |
| 5 | **CRM-3** Scheduler | The system can act unattended, instead of only when watched. | S (0.5d) | ARCH-1 |
| 6 | **ARCH-2** Static frontend | Real tooling for 1,515 lines of JS/CSS. | M (1d) | — |
| 7 | **ARCH-3** `touches` table | One ladder engine; new channels become free. | M (1d) | ARCH-1 |
| 8 | **ARCH-4** Repository boundary | 40 SQL statements out of the web layer. | L (1–2d) | ARCH-1 |
| 9 | **ARCH-5** Versioned migrations | Rename/drop/backfill become possible. | S (0.5d) | ARCH-3 |
| 10 | **ARCH-6** Config schema | 40 unvalidated env vars → one typed surface. | S (0.5d) | — |

```
CRM-1  reply detection    ██        ← makes it SEE
DISC-1 discovery on       █         ← makes the funnel REAL
ARCH-1 extract domain/    ██        ← the refactor the product work needs
CRM-2  metrics            ██        ← makes it LEARN
CRM-3  scheduler          █         ← makes it ACT unattended
ARCH-2 static frontend    ██
ARCH-3 touches table      ██
ARCH-4 repo boundary      ███
ARCH-5 migrations         █
ARCH-6 config schema      █
                          ──────────
                          ~9-10 days total; first 3 days deliver all the user-visible value
```

**Why ARCH-1 stays near the top and the other five slide.** Extracting `domain/` is the one
piece the product work genuinely needs: reply detection has to update scheduling state, and
metrics has to aggregate it — both want that logic testable and out of the web server. The
remaining five are hygiene, and hygiene is easier to aim correctly once you know which
direction the product is growing.

**Why ARCH-3 before ARCH-5.** So the migration framework is validated against a real
migration rather than a toy one.

### 4.2 Guardrails — non-negotiable
- **228 tests green after every ticket.** No ticket lands red.
- **The eval set is the safety net for behaviour.** `evals/resolution.jsonl` must stay green;
  it already caught one bug that all 228 unit tests missed.
- **No behaviour changes inside a refactor ticket.** Anything user-visible is a separate commit.
- **Byte-identical output where verifiable.** Diff generated prompts and rendered PDFs
  old-vs-new, as was done for the ruff F841 cleanup.

---

## Part 5 — Non-goals

Explicitly rejected, so they don't get reintroduced:

- **No web framework.** FastAPI/Flask would add dependencies to solve a routing problem that
  is ~40 lines of `if path ==`. Revisit only if the dashboard becomes multi-user.
- **No frontend framework or build step.** Static files, no bundler. The UI is a few thousand
  lines of DOM manipulation and doesn't need React.
- **No ORM.** A thin repository, not SQLAlchemy.
- **No async rewrite.** Threads are sufficient for one operator.
- **No microservices, no Docker, no cloud.** Local-first is a product commitment.
- **Not rewriting the pipeline.** It is the best part of the system.

---

## Part 6 — Success criteria

**Quantitative:**

| Metric | Now | Target |
|---|---|---|
| `web_dashboard.py` LOC | 3,710 | < 1,200 |
| JS/CSS inside `.py` | 1,515 | 0 |
| SQL in the web layer | 40 | 0 |
| `contacts` columns | 42 | ~32 |
| Duplicate "is it due" impls | 3 | 1 |
| Test : source ratio | 0.16 | > 0.25 |
| Runtime dependencies | 8 | 8 |

**Qualitative — the real test:** a new follow-up channel (SMS) should be **one row in a
channel registry plus one prompt**, touching no schema and no scheduling code. If that isn't
true when this is done, the refactor didn't work.

**And the honest one:** the next entity-resolution bug should be caught by `evals/` before it
reaches a real job — not by Jorge noticing a stranger's email address in the dashboard.

**Product criteria (§4.1), which matter more:**

| Question the system cannot answer today | Ticket that fixes it |
|---|---|
| "Did anyone reply?" | CRM-1 |
| "What should I apply to this week?" | DISC-1 |
| "Is email or LinkedIn working better?" | CRM-2 |
| "What needs my attention right now?" (without opening the dashboard) | CRM-3 |

If all four are answerable and the ARCH targets are met, this is done.

---

## Appendix — Open product questions carried forward

Not architecture, but they'd be lost otherwise:

1. **Résumé format.** The new base résumé is PERSONAL STATEMENT / WORK EXPERIENCE / EDUCATION
   / KEY STRENGTHS; the renderer still emits Professional Summary / Technical Skills / Work
   Experience / Projects / Education. Tailored content is right, layout is not.
2. **Identity conflicts.** Résumé says "Jorge Alejandro Diez Magni"; `profile.json` says
   "Jorge Alejandro Diez" (drives filenames + cover-letter header). Résumé and Gmail signature
   use `alediez2408@gmail.com`; sending happens from `jorgealejandrodiezm@gmail.com`.
3. **Per-company cap.** Still unbuilt. 5 contacts × 3 touches = 15 emails at one company.
4. **Reply detection** (`CRM-1`). The only remaining use for the Gmail metadata scope.
5. **Truncated bullet** in the base résumé: `"...RAG implementation, knowledgegraphs, k"`.

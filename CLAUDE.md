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
- **Tests:** 1481 passing (`tests/`, 82 files) · ruff clean (line-length 120, py311) · ESLint clean
- **Schema version:** 3 (`applypilot migrate --status`) · **Settings:** 47 declared in `settings.py`
- **Branch:** the Spaces work lives on `spaces`, **12 commits ahead of `main` and unmerged**.
  Check `git log --oneline -1` before believing anything here (§Dev workflow).

## Quick orientation

A **6-stage pipeline over a SQLite `jobs` table**, now scoped by **Space** (§Spaces). Each
stage reads rows at one state and writes columns that advance them. Stages are idempotent and
independently runnable.

```
discover → enrich → score → tailor → cover → pdf →  [apply]
                                                    [network → outreach → follow-up]
```

Surfaces:
- `applypilot run [stages...]` — prep pipeline, sequential or `--stream`
- `applypilot apply` — browser submission (Tier 3); `--copilot` fills and stops
- `applypilot dashboard --serve` — the operator UI, and where you actually live. Multi-Space
  since SPACE-2: a tab strip, `?space=<id>`, and a `＋` that creates one from a template
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
| `web_dashboard.py` | **The operator dashboard.** 3,568 lines, **zero SQL** — data access goes through `repo/` and `store.py` (ARCH-4). |
| `repo/jobs.py` | Every `jobs` query as a named function. Owns `QUEUE_SQL` (provenance — how a row arrived) and `_in_spaces` / `_one_space` (membership — which panel it is in). Those two never do each other's job (SPACE-1a D2). |
| `repo/spaces.py` | The `spaces` / `identities` registries. `jobs_shaped_ids()` and `document_making_ids()` gate the pipeline stages and RAISE on an empty registry rather than returning `[]`. |
| `scoring/resume_sections.py` | Parses the BASE résumé into its own sections. The base résumé is the template; tailoring rewrites content inside it. |
| `settings.py` | **Every env var, one registry.** Types, defaults, validators, secret flags. Malformed values fail at startup naming the variable. `.env.example` is generated from it. |
| `migrations/` | Numbered `mNNN_*.py` with `up(conn)`, run at startup after the additive column pass. **Migrations must be idempotent** — this app gets killed mid-operation. `001` = the ARCH-3 touches backfill, `002` = messages per contact, `003` = the `spaces` / `identities` registries. **A migration must never touch a column the additive dicts declare** — they race (§Spaces). |
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
| `jobdesc.py` | `role_essentials()` — the parts of a posting that say what the JOB IS. Outreach read `full_description[:1200]`, which on a real posting is the mission statement and the org chart. |
| `intent.py` | CRM-4b. What a reply wants — rejection / not now / interested / introduction / question / auto-reply — from a ~200-char snippet. Rule-based and quick to say `unknown`. |
| `metrics.py` | CRM-2 funnel + reply rates. Every rate carries its `n`; bounces leave the denominator. |
| `deck.py` | Intro-deck links. `slugify`/`disambiguate` → `/intro/gina`, **not** `?v=<token>`. `relink()` rewrites an existing draft's link without regenerating the copy. |
| `interactions.py` | What a contact has actually DONE, from several sources. Our own actions (an email sent, a LinkedIn invite) are context, never engagement. |
| `authrealm.py` | **What one sign-in covers.** URL → the ATS tenant an account belongs to. `host_is_tenant` is load-bearing: every employer on `wd1.myworkdaysite.com` shares that host, so a cookie there proves nothing about any one of them. |
| `linkedin_thread.py` | Reading an open LinkedIn thread: who a display name is (word-level, never substring) and de-colliding the one-time-per-GROUP timestamps that would otherwise destroy messages. |
| `lastinteraction.py` | When something last happened on a job **and who did it**, from six sources that were never joined. Direction is the point — "you emailed them 6 days ago" and "they replied 6 days ago" are the same age and opposite situations. |
| `space.py` | **What a Space IS** — a frozen manifest, shaped after `followup.Channel`. `shape` + `tailor_docs` gate the pipeline queues; `tone`/`offer` reach the prompts; `schedules`/`channels` drive the ladders; `offer_deck` and `can_autosend` gate the deck link and the send path. Everything but the five COLUMNS rides in a `config` JSON blob, so a new field is never a schema change. |
| `target.py` | A company you STATE, not an employer recovered from a URL. `anchor(space, name)` → `target:<space>:<slug>`, hashed into every contact key. `parse_input()` returns rejects rather than dropping them. |
| `temperature.py` | How an application is DOING, not how far it has travelled. Bands answer **is anything still in motion**, from an interview backwards. **Only a PERSON can reach `warm`**, and finishing the plan moves a job DOWN. §Lessons 54, 55. |

**Adding a channel is one `Channel` entry plus one prompt** — executed, not claimed:
`test_adding_a_channel_needs_no_schema_change` defines a channel that exists nowhere in the
codebase and drives it end to end. Readiness is data too, which is what removed the last
`if channel is EMAIL` from `_is_ready`.

**SMS proved the claim and found the one line where it was false** (2026-08-01). Storage,
scheduling, readiness and terminal state all worked untouched — but `followup_panel` ended with
`e, li = buckets[EMAIL.name], buckets[LINKEDIN.name]` and spelled both key sets out by hand, so
a third channel passed through every part of the engine correctly and then **vanished at the
return statement**. It is built from `CHANNELS` now. Two more branches became data: the
dashboard's `"LinkedIn " if channel.name == "linkedin"` is `channel.label`, and the anchor
setter is a `{channel: store-function}` map.

Real cost of the third channel: **one column** (`sms_sent_at`), one registry row, one prompt.

That test used to name SMS, and shipping SMS broke it in a way worth keeping: its fake channel
declared `default_schedule=(24, 72)`, but `channel_schedule()` resolves through the settings
registry — so the moment `SMS_FOLLOWUP_SCHEDULE` became real, the fake channel silently
inherited `[72, 168]` and the arithmetic stopped holding. **A test proving "an unknown channel
works" has to name one that is actually unknown.** It is WhatsApp now.

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
| `outreach.py` / `prompt.py` | Drafts: cold email + LinkedIn note + **email follow-ups** + **LinkedIn follow-ups** + **texts** (`draft_sms`), each written for its touch position. |
| `gmail_send.py` / `gmail_oauth.py` | Send via OAuth (preferred) or SMTP. Threading, signature, attachments, safeguards. |
| `linkedin_dm.py` / `dm_prompt.py` | Dormant CLI-only compose helpers (auto-send abandoned — §Lessons). |
| `linkedin_agent.py` | Opt-in read-only LinkedIn augmentation. Never sends. |

### `extension/` (repo root)
MV3 popup only — pulls the outreach queue from `:8765`, copies a note, opens the profile, and
since 2026-08-04 **reads a LinkedIn thread you already have open** and logs it.

**Still no `linkedin.com` host permission, no content script, no background worker.** The read
runs on `activeTab` + `scripting`: Chrome grants `activeTab` for ONE tab, only on the click that
opened the popup, only while it is open. The extension went from *cannot* touch a LinkedIn page
to *cannot unless you click*, which is a real change and was the operator's call to make — it
reads a page already on screen and never clicks, types, sends or requests anything.

`thread_parser.js` is a separate file so it can run against a real DOM in the suite
(`tests/test_linkedin_thread.py`, jsdom — a devDependency alongside eslint). Two properties of
LinkedIn's markup, read off the live page rather than assumed, drive the whole design:

- **Messages are GROUPED.** Consecutive messages from one person share one name and one
  timestamp, carried only on the first. Reading per-message drops the sender on every
  continuation, and a message with no sender cannot be given a direction — so a two-message
  reply logs as one inbound and one outbound, i.e. as you having already answered.
- **There is no machine-readable timestamp.** `<time>` carries a class and nothing else; no
  element in the list holds an ISO string or an epoch. The date heading ("TODAY") and the group
  time ("2:38 AM") are all there is, so `at` is DERIVED — which is why the popup shows the
  resolved timestamp per message before anything is written, and says so when one is unreadable.

**Direction is decided by matching the sender to the CONTACT**, not by LinkedIn's `--other`
class: the thread has two participants, whoever is the contact is them and anyone else is you.
That needs no selector for "me" and no stored copy of your own name, so it survives the rename
that would break the class. Names match **word by word**, never by substring (§Lessons 1), and
`domain/linkedin_thread.py` runs the same test server-side.

`dedupe_times()` is not cosmetic: `interactions` keys a row on `sha256(contact|kind|at)`, so
three messages in one group are ONE row with two silently overwritten. It is deterministic, so
re-reading a thread you have already logged is a no-op rather than a duplicate.

---

## Data model

| Table | Owner | Purpose |
|-------|-------|---------|
| `jobs` (35 cols) | `database.py` | The 6-stage state machine. `_ALL_COLUMNS` is its source of truth. Holds **targets too** — a targets row is a `jobs` row keyed `target:<space>:<slug>` (SPACE-1a D1). |
| `contacts` (41 cols) | `networking/store.py` | People per job + outreach + verification. |
| `touches` | `networking/touches.py` | One follow-up touch per row, ANY channel. `seq` is per (contact, channel). |
| `sequences` | `networking/touches.py` | Terminal state per (contact, channel): `stopped` / `replied`. |
| `connections` | `networking/connections.py` | Imported LinkedIn CSV. |
| `messages` | `networking/messages.py` | **CRM-4 conversation memory.** Thread HEADERS only — no body/snippet column exists, and a test asserts it. Keyed by Gmail's message id, so re-syncing is a no-op. `rfc_message_id` is what lets a reply chain `References` across the whole thread. |
| `interactions` | `networking/interactions_store.py` | Events with nowhere else to live: a detected booking, an operator-logged LinkedIn profile view. Derived facts are NOT copied here — they are computed at render time so they cannot drift. |
| `job_events` | `database.py` | Per-job activity log. Append is best-effort, never raises. |

| `ats_accounts` | `repo/accounts.py` | One row per **auth realm** — the thing one sign-in covers. `have_account` (about us) is deliberately separate from `kind` (about the site). |
| `spaces` | `repo/spaces.py` | One row per campaign. Five columns + a `config` JSON blob. Seeded by migration 003. |
| `identities` | `repo/spaces.py` | One row per SENDER (mailbox, from-name, deck, limits). Created by 003, **read by nothing yet** — ID-1. |
| `schema_migrations` | `migrations/` | Version, status, `claimed_at` lease. See §Lessons on the 300s lease. |

Live counts (2026-08-05, a snapshot — these move within minutes of real use, so treat them as
orders of magnitude and re-measure before reasoning from one): jobs **27** (27 applied,
**1 interview scheduled**), contacts **206**
(113 emailed, **5 replied**), touches 69, messages 182, interactions 10, connections 899,
**2 recorded deck opens** (the first ever — see §Engagement signals). Three Spaces: `job-search`,
`partnerships`, `gauntlet`. **Schema version 3**.

Contacts nearly tripled on 2026-08-04 — 66 → 185 — because employer resolution was broken in
three separate ways and every one of them returned zero people rather than an error. See
§Contact discovery.

**77 emails for 2 replies** was the number that prompted the 2026-08-03 audit. Engineering
quality scored 9/10 and outcomes about 3/10, and the gap was the whole finding: the system
optimised the middle of the funnel while nothing could tell you what worked. `draft_variant` and
the per-company cap came out of that. Discovery scored 1/10 and was then correctly re-scored —
LinkedIn's job feed is a RECOMMENDER and ApplyPilot's discovery is a keyword SEARCH, so the
operator finding jobs by hand is a better tool for the job, not a gap.

**The second reply arrived on its own** — Gina Johnson at Salesforce, 2026-07-31, detected by
the CRM-1 background poller with nobody watching. That is the whole point of the heartbeat:
before CRM-1 the only recorded reply in the database had been typed in by hand.

**`jobs` columns by stage:** discover(`title,salary,description,location,site,strategy`) →
enrich(`full_description,application_url,detail_error`) → score(`fit_score,score_reasoning`) →
tailor(`tailored_resume_path`) → cover(`cover_letter_path`) → apply(`applied_at,apply_status,
apply_error,agent_id,verification_confidence`), plus `rejected_at`.

**`contacts` groups:** identity · outreach(`outreach_subject/message/status,sent_message_id`) ·
threading(`thread_id,rfc_message_id`) · LinkedIn invite(`dm_status,dm_sent_at`) ·
operator(`phone,notes`) · verification(`confidence,verify_note`) · SMS(`sms_sent_at`) · deck(`deck_slug,deck_viewed_at,deck_last_at,deck_views`).
**Follow-up state is NOT here** — it is `touches` / `sequences`, keyed by channel (ARCH-3).

---

## The dashboard (`web_dashboard.py`)

Localhost-only (`127.0.0.1:8765`), Origin/CSRF-guarded. Restructured 2026-07-28 from four
sibling accordions into:

**Five tabs**: People · Follow-ups · Materials · Activity · **Job**. The Interactions tab was
retired 2026-08-04 (UX-1) — see §The row, and §Lessons on why its ledger was kept.

**The 🔔 counter, top right** (2026-08-02) — every outstanding action across every application,
grouped and ORDERED. A flat count of 49 tells you nothing, and a list putting "3 LinkedIn
invites left" above "someone replied 4 days ago" is worse than none. Replies outrank ladders
(§Lessons 27); rejected and interviewing jobs are excluded, because a badge that is permanently
lit trains you to ignore it. Everything reads ONE `dueByChannel()` — the tab badge, the Next
button, the Follow-ups tab count and this all summed email + LinkedIn by name and silently
ignored SMS the moment that channel shipped.

**Tags replaced the Links column** (2026-08-02). Those links were already redundant: the table's
`job` link is truncated and uncopyable, which is why the Job tab exists. Tags are DERIVED from
fields already on the wire, so a tag cannot drift from its job. Clicking one filters; chips AND
rather than OR, because with OR a second click returns MORE rows and reads as broken. The search
box is STATIC markup — `refresh()` replaces `#jobs` wholesale every 2.5s, so anything rendered
into it is destroyed mid-keystroke. Typing re-filters from `LAST_JOBS` and never refetches
`/api/status` (§Lessons 11, 26 — 74 SQL statements behind a keystroke).

**🎯 Interview scheduled is the success metric** (2026-08-03), on the row next to Re-apply. Every
other number counts EFFORT; this is the only outcome, and the funnel now ends at it. It is also
the only state that means STOP: marking it greys the row, removes the job from the counter, and
HALTS every sequence — chasing somebody after they agreed to meet is the one follow-up
guaranteed to cost something. It does not touch `apply_status`: a rejected job has left the
pipeline, an interviewing one has arrived.
`Job` carries the posting's links in full — the table's `job` link is truncated and uncopyable,
which is why the tab exists — with the description fetched on demand (the list payload holds a
900-char excerpt; real ones run 4–10KB). `Interactions` answers "has anyone actually engaged?".

**A contact who has REPLIED gets a conversation, not a form.** `emailed` used to open on an
editable copy of an email delivered days earlier, with Copy/Regenerate, while the live exchange
sat collapsed below it. A sent email cannot be edited: offering it as a form is offering an
action that does not exist, and it pushed the only actionable thing off screen. `hasConversation()`
is the single branch — timeline first, composer anchored under it, the sent outreach as one entry
in that timeline. §Lessons 31.

**The row carries three derived things** (UX-3/5/6, 2026-08-04), all computed from data the
payload already loads — no new query, budget unmoved:

- **Temperature** — `warm · active · cooling · cold`, plus `won`, `undeliverable` and `new`,
  which are not temperatures. **A dot AND a word**, never colour alone, and every band carries
  the sentence that produced it. `undeliverable` and `new` were not in the ticket and both came
  from running it: a bounce is not cold (opposite fixes), and a job imported this morning is not
  failing. **Rebuilt around RUNWAY on 2026-08-04 — see §Lessons 54.** First live reading was
  cooling 10 · new 6 · warm 4 · won 1 · cold 1; it is now active 7 · new 6 · warm 4 · cooling 4
  · won 1.
- **Last interaction, with its DIRECTION** — `← Sarah replied · 2d ago` accented,
  `→ You emailed Sarah` faint. Same age, opposite situations.
- **Search reaches people and whole postings.** It covered nine fields and never looked at
  `j.contacts`, so a recruiter's name returned nothing while the dashboard displayed that name
  one click away; and `j.description` is a 900-char excerpt. Full text arrives from
  `/api/job-descriptions` **once per session on the first keystroke** — 132KB measured, which
  is what shipping it on the 2.5s refresh would have added forever. A row matched through a
  person says **"matched: Sarah Chen"**, or it reads as a bug.

**Engagement lives on the PERSON, not in a tab** (UX-1). The Interactions tab held 2 rows across
185 contacts — because replies and deck opens are derived at render time and deliberately never
stored — while asking "has anyone engaged?" in a different room from the people. The ledger
under it was KEPT: it is the only record of an operator-noted event and the home for LinkedIn
messages. First attempt put the block on the LinkedIn tab, which reproduced the same bug one
level down and was reported as unchanged.

**LinkedIn messages are logged by hand** (UX-2). `messages` is keyed on Gmail's own message id
and carries `thread_id` / `rfc_message_id` / `from_addr` — a DM has none, and faking them would
corrupt reply detection. `dm_status` only ever recorded what WE sent. So both directions go in
`interactions`, and an inbound one **stops the LinkedIn ladder only** and reaches the 🔔 counter
**without writing `replied_at`** — that field means a DETECTED email reply and is what
`metrics.by_variant` divides by.

- **Status strip** (always visible, never a toggle) — a left-to-right path
  `✓ Found → ✓ Applied → ✓ Emailed 4/4 → ↻ Follow up 0/4 → · Reply`, first unfinished step
  amber, plus **one** `Next` action (`nextAction()`) and a visible `🔄 Re-apply`.
- **One tabbed panel**: People · Follow-ups · Materials · Activity. `PANEL_OPEN` / `TAB_OPEN`
  survive the 2.5s refresh.
- **Contacts collapse to one line** with channel pills (`✉ sent · 🔗 connected · ↻ due`).
  Opening one shows channels as tabs: **✉ Email · 🔗 LinkedIn · 💬 Text**. Email and LinkedIn
  hide when there is nothing behind them; **Text is always offered**, which looks inconsistent
  and is not — that tab is where a phone number gets entered, so hiding it without one hides the
  only way to add one. With no number the composer renders **disabled** rather than being
  described in prose (§Lessons 41).
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

`/api/status` costs **74 SQL statements against a budget of 80** (was 313 before ARCH-4
measured it — 199 of those were `CREATE TABLE IF NOT EXISTS` re-run every request). This file
said "50" for two sessions while the test said 80; check `MAX_STATEMENTS` in
`tests/test_query_budget.py`, not this line. It holds the line and fails on a per-contact N+1.
Batch a new query; don't raise the budget.

**Six statements of headroom is thin.** Spaces cost exactly ONE — the nav list — because the
Space manifest is built from the row `_resolve_space` already read rather than by calling
`load()` again, and `_registered()` memoises its TRUE answer per connection. Anything added
here needs the same treatment.

**That budget counts SQL and nothing else**, which is exactly how CRM-4a slipped a Gmail HTTP
call per job onto this path and took the endpoint to **2.4s against a 2.5s refresh** — see
§Lessons 26. Now **0.043s**, with a test that counts network round-trips too. Anything you add
here that touches the network needs the same treatment.

---

## Spaces (SPACE-1a … SPACE-4b, 2026-08-04/05 — branch `spaces`, unmerged)

**A Space is a manifest, not a fork** — a row of config over one shared engine, shaped after
`domain/followup.Channel` because that pattern already made a third follow-up channel cost one
column. `docs/spaces-prd.md` is the plan; `docs/tickets/SPACE-1a-*.md` is the authority where
they disagree, because three of the PRD's claims did not survive contact with the code.

**Two shapes.** `pipeline/jobs` (rows are postings) and `pipeline/targets` (rows are companies
you pitch). Three templates, two of which share the targets shape — that is the central claim,
and `test_adding_a_space_needs_no_schema_change` is where it gets falsified: it defines a Space
named `lighthouse-tenders`, drives it end to end, and asserts no migration ran. A second test
asserts that name appears nowhere in the codebase, because the channel version of this test
originally named SMS and silently broke when SMS shipped.

| Decision | Why |
|---|---|
| A target row lives in `jobs`, keyed `target:<space>:<slug>` | Almost nothing reads the table — the anchor travels as an opaque string. A separate table forks every join. 20 dead columns is the cheaper half. |
| `space_id` alone decides membership; `strategy` keeps meaning provenance | Two partition keys over one table is §Lessons 49 waiting. A target keeps `dashboard_upload` — a private strategy would hide it from `delete_job`. |
| `space_id` ships in the additive DICTS, never a migration | They RACE: `get_connection()` does not call `init_db`, so `ensure_contacts_columns` can run first. A migration touching a declared column is a duplicate-column error one way and a NOT-NULL-without-default error the other. The column DEFAULT also does the backfill, which no `UPDATE` can do without a window. |
| `company_cap` is NOT on the manifest | Same mistake §Headline 4 corrected for the daily limit: the cap exists because a company sees one sender, and **the recipient does not know what a Space is**. |
| `job_url` → `anchor` was NOT renamed | 169 refs in 18 source files, 140 in tests, and it leaves `messages.job_url` disagreeing with it. Pure hygiene, and the only step that can destroy data. SPACE-1b, deferred. |

**The manifest reaches everything, and the guarantee is a golden file.** A targets Space gets
its OWN system prompt (`_PITCH_SYSTEM`), not the job-seeker one with caveats appended —
§Lessons 40. Follow-ups too (`_PITCH_FOLLOWUP_SYSTEM`), which the first pass forgot, so touch 1
read correctly and touch 2 claimed an application that does not exist.
`tests/golden/jobs_outreach_prompt.txt` pins the jobs prompt byte-for-byte. **Its first version
was vacuous**: it compared `space=None` against a default manifest and passed under a mutation
that leaked a field into BOTH paths. Two things moving together is not a regression test.

**Creating a Space is a `＋` on the nav strip.** Two templates offered, not three — `business`
differs from `outreach` by `identity_id` alone, that field FREEZES after the first send, and
ID-1 has not shipped, so a business Space made today would send from the personal mailbox
forever. The refusal says so. The nav shows from ONE Space up, because the `＋` lives in it and
hiding the strip below two hid the only way to make a second.

**Not built:** ID-1/ID-2 (per-identity mailbox, deck, limits — `identities` exists and is read
by nothing), SPACE-0 (archive terminal rows), SPACE-6 (the business Space as a falsifier).
Copy debt: the bucket filters still say "In progress / Applied / Rejected" and the search
placeholder names salary, both wrong words in a targets Space.

---

## Follow-up sequences

**Three** independent ladders, all human-in-the-loop. Only email can auto-send.

| | Email | LinkedIn | SMS / iMessage |
|---|---|---|---|
| Anchor | `submitted_at` | `dm_sent_at` | `sms_sent_at` |
| Proof it started | `sent_message_id` | `dm_status` | operator clicks `✓ I sent it` |
| Default | `48,96,168` (2d/4d/7d) | `120,288` (5d/12d) | `72,168` (3d/7d) |
| Send | `send_followup()`, threaded | copy → open profile → paste | copy → open Messages → paste |
| Stop | reply / stop / complete | same | same |

All three anchor to the last `touches.sent_at` once a ladder is running; the column above is
only the *first* message. **A phone number does NOT start the SMS ladder** — it is typed in by
hand for anyone the operator MIGHT text, so keying readiness on it would mark a follow-up due
for people nobody has ever messaged. `sms_sent_at` is the proof, and it is operator-asserted
because nothing can watch Messages.app.

SMS is the slowest and shortest ladder on purpose: a text interrupts, and the three-touch
cadence that is normal in email reads as harassment on a phone. **Texts never carry a link** —
a URL from an unrecognised number is the strongest spam signal there is, so `_intro_deck_url`
is deliberately never consulted on that path, unlike every other channel.

**A follow-up READS THE THREAD** (2026-08-03). `draft_followup` saw only
`contact.outreach_message` — the first email, truncated to 700 chars — so touch 2 did not know
what touch 1 said and touch 3 knew neither. The prompt has always said "do NOT repeat it" while
being handed a third of what there was not to repeat. `conversation_transcript` and
`touches.sent_touches` both already existed and `_draft_reply` already used them; this path
simply never did (§Lessons 39).

**The deck is offered ONCE.** It used to go in every touch, and `ensure_intro_deck()`
force-appended the link when the model correctly left it out — a guarantee that guaranteed the
repetition. Both are conditional now. The already-sent check compares the BASE url: the earlier
emails went out as `/intro/` and `INTRO_DECK_PATHS` now builds `/intro/michael`, so matching the
full link found nothing and re-pitched the deck to a man who had already had it twice.

**The PDF attachment is gone** (2026-08-03). 3.1 MB riding alongside a link to the same deck, on
all 34 sent emails, and ON BY ACCIDENT: `_intro_deck_path` defaulted `OUTREACH_ATTACH_DECK` to
"1" while `settings.py` declared `False`, so `doctor --config` reported it off the whole time. A
default in two places is two defaults. Résumé and cover letter still attach.

**The job description is SPENT, not truncated** (`domain/jobdesc.role_essentials`, 2026-08-03).
`draft_email` read `full_description[:1200]`. On the live Affirm posting that is 180 chars of
mission statement, 340 of org chart, and then the role STARTS at ~520 — the sentence saying what
the person does began exactly where the budget ran out. Sections are classified by header and
the shared ones dropped; 1.5–2.6KB of role content instead of 1.2KB of preamble.

**`contacts.noticed`** — what the operator saw on the profile, typed into a box on the LinkedIn
tab. This is the LinkedIn-post idea WITHOUT the crawl (§Lessons 3): the "Copy note + open
LinkedIn" flow already puts a human on the page, and five seconds of their judgement beats
"posted about X three days ago". The prompt bans the SHAPE, not a verb list — the first version
forbade "I saw your post" and the model wrote "I noticed your post about…" (§Lessons 42).

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

**Per-company cap** (`OUTREACH_COMPANY_CAP`, default 8, 2026-08-03). The other two limits leave
this wide open: the daily limit is global and the cooldown is per ADDRESS, so seven people at one
company times three touches is 21 emails and nothing objects. Measured before writing it — Webai,
Wander and Salesforce had already received **10 emails each**. Counted per COMPANY across every
job, follow-ups included, because that is the unit the recipient experiences.

**`draft_variant`** records what produced each draft (`cold+jd2k+noticed+deck+cal`) so reply rate
can be attributed. Inputs, not a version number: a version goes stale the moment a prompt is
edited and pools two different things under one label. `metrics.by_variant` reads it. Before
this, every improvement to the copy was unfalsifiable.

**Stalled conversations** (`conversations.STALLED_AFTER_HOURS`, default 72). `replied` is
TERMINAL so the cold ladder halts — correct — but nothing replaced it, and a warm thread that
went quiet had no mechanism at all. The system chased strangers and abandoned everyone who
engaged. `unanswered` counts messages since they last spoke; 2+ means the nudge is spent. NOT a
Channel: it is a conversation state, with different copy and a different cadence. Known tension:
at 72h the EMAIL ladder still nudges a stranger sooner, at 48h.

**Threads we did not start.** `poll()` read threads by `thread_id`, captured at send time, and
skipped every contact without one — so anyone who wrote to us FIRST, replied from another
address, or was introduced into somebody else's thread was invisible forever. That is the Writer
case: Victoria CC'd David, David wrote on a NEW thread, nothing saw it. Resolved in ONE batched
Gmail query, only for people in play (§Lessons 26).

**Round two** (`skip_known`). When every contact is spent and nobody replied, search the same
company again excluding everyone already stored — by `contact_id`, the same function that stores
them, because a second name/email match would be a competing answer to "is this the same
person". Without the exclusion the button is an expensive no-op: `select()` is deterministic and
returns the same top five. A contact is `exhausted` when every channel USED has run out with no
reply; a phone number or an unwritten address is NOT unresponsive, it is untouched.

**Not built:** no scheduler (nothing fires while the dashboard is closed — `applypilot
schedule --install` exists and has never been run), **no per-company cap** — 5 contacts × 3
touches is 15 emails at one company, and SMS now adds 2 more per person on top. Reply detection
is CLOSED (CRM-1).

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

### Three ways the employer was wrong, all returning zero instead of an error (2026-08-04)

Contacts went **66 → 185** the day these were fixed. Each produced a truthful "0 found", which
is why none of them looked like a bug.

| Stored as | Should be | Cause |
|---|---|---|
| **Ouryahoo** | Yahoo | the Workday TENANT SLUG is not the company name |
| **Edu** | Stanford | `.edu` missing from `_TLD_LABELS` — third after "Ats" and "Hr" |
| **Uploaded** | Google | the board-owns-the-posting rule was applied at one of its two call sites |

**A tenant slug is chosen by an HR team and wraps the real name** — `ouryahoo`,
`WellsFargoJobs`, `acme-external`. Measured live: `company_lookup("Ouryahoo")` returns nothing,
`company_lookup("Yahoo")` returns six organizations. Trimming affixes blind is not acceptable —
**OurCrowd is a real company** — so `refine_company_from_posting()` accepts a variant only when
the POSTING'S OWN TEXT names it as a whole word. The first version tested `variant in text` and
turned OurCrowd into "Crowd": §Lessons 1, inside the function written to respect it.

**`derive_company` step 1 applied `_company_owns_the_posting` to a stored name; `_host_label`
never did**, so `google.com/about/careers` resolved to no employer at all — on a job already
applied to, with **8 known connections at Google** sitting unsearched. §Lessons 20 wrote the
rule down and half of it was implemented.

**A zero result now says WHICH zero it is.** `no candidates from apollo (coverage or plan/key)`
named three unrelated problems and pointed at none; a missing API key printed the identical
sentence as an unknown company, which cost a wrong diagnosis. Three distinct messages now, with
a test that they cannot collapse back into one.

### Colleagues and recruiters are separate searches (2026-08-04)

Reported as "too many talent acquisition people". Measured against the live API on a Yahoo job:

    blended query (role + recruiter titles)  ->  25 candidates, 0 peers, 25 recruiters
    "AI Operations Strategist"               ->   0
    "Strategist"                             ->  25 peers

**A bespoke multi-word title matches nobody** — employers invent titles, Apollo indexes what
people put on LinkedIn — so the recruiter titles took every slot and `select()` was choosing
five recruiters out of five. **One query cannot produce a mix, because the provider decides the
composition.** Two searches now, `OUTREACH_MIN_PEERS` / `OUTREACH_MIN_RECRUITERS` (4 each),
results **interleaved** rather than scored into one list: the caller drops whoever fails
verification, so front-loading four peers rebuilds the bug one step later.

**`peer_titles()` widens from the FRONT** — "AI Operations Strategist" → "Operations
Strategist" → "Strategist" — because English puts the qualifier first and the function last.
It never widens to a bare rank ("Manager" is a level, not a role).

**Every pasted job was titled `"{company} uploaded job"`** and nothing ever replaced it, so the
peer search was looking for the word **"job"** and duly found people at Yahoo titled "Job",
"Student Job" and "No job". `collect_detail_intelligence` had captured `page_title` since it was
written and no caller had ever read it. The scrape now recovers the real role and overwrites
**only** the placeholder. Backfilled 17 of 22 jobs; the 5 that failed are expired postings and
auth walls, which is the honest answer.

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

31. **A feature that only runs from a scheduler nobody installed does not exist.** Booking
    detection and deck-click pulling were both built as `applypilot tick` steps. `tick` is
    scheduled by launchd and `schedule.installed()` was False — so neither had ever fired once,
    while a manual "📅 They booked a call" button sat beside them looking like the intended
    path. Both now ride the dashboard's existing 5-minute poller, which needs no system change
    and no permission. Ask "what actually calls this?" before calling a detector automatic.

32. **Never issue a link the site cannot serve.** Outreach was switched to `/intro/<name>` while
    the Netlify rewrite serving those paths was still uncommitted — four recruiters were sent
    emails whose deck link 404'd. `INTRO_DECK_PATHS` now gates the scheme and defaults OFF. A
    personalised link that does not resolve is far worse than an un-attributed one that does:
    it costs the conversation, which is the entire reason for sending it.

33. **Ship the urgent fix ALONE.** The repair commit bundled the rewrite, two Netlify Functions
    and an `@netlify/blobs` dependency. Netlify builds all-or-nothing, the build failed, and the
    one urgent part went down with the two that could have waited. Reshipped as four lines of
    `netlify.toml` — no install step, nothing to bundle — live in two minutes.
    Diagnosis came from reading the LIVE bundle, not guessing: the previous commit's text was in
    it and mine was not, which proved auto-deploy worked and my build had broken. Two wrong
    theories were tested first (a `package-lock` mismatch — `npm ci` fails on the ORIGINAL
    lockfile too, so npm was never the tool; and yarn's `--frozen-lockfile`, which passes).

34. **A rejection is only as good as the thing it contradicts.** An Avathon job found three
    people and dropped all three, the CEO included. The employer domain `avathongov.com` was
    read off the careers-site HOST — an inference — while their real mail domain is
    `@sparkcognition.com` (the company was SparkCognition before the rename). That guess was
    then fed to a check whose docstring calls a mismatch "near-proof". An eval case had already
    written the rule down — *"domain comes from Apollo, never guessed"* — and nothing enforced
    it. Provenance is explicit now (`domain_source`): Apollo-derived domains still reject a
    contradiction, guessed ones cannot.

35. **The tab that answers its own question with "yes" is worth nothing.** The first Interactions
    tab counted the LinkedIn invite as engagement and every job read "3/3 engaged", "5/5
    engaged" — but `dm_status` is `sent`/`manual`, both meaning WE sent it, and no `accepted`
    state exists anywhere in the schema. Reclassified as our own action; the honest number
    across 14 jobs is 2 of 58.

36. **`INSERT OR REPLACE` on a shared key silently reassigns ownership.** `messages` was keyed on
    `message_id` alone, so "Pull all Gmail" on David moved all three Writer messages to him and
    left Victoria's conversation EMPTY — measured on live data, 3 → 0, one click. One message
    legitimately belongs to several contacts; the key is now `(message_id, contact_id)`
    (migration 002).

37. **The repair tool has to be reachable from the broken state.** The Gmail fetch button was
    built inside `conversationView`, which only renders once an inbound message exists — so the
    control for "my thread is missing" was hidden inside the thread that was missing. It lives
    on the contact meta row now, which always renders.

38. **The cause was not in the repo, and nothing that reads the repo could ever have found it.**
    Two deploys of the deck collector "failed" five hours apart with no visible error. Three
    agents ran 157 tool calls and correctly exonerated every suspect — the `@netlify/blobs`
    dependency (`git diff d33b6ad HEAD -- package.json yarn.lock` is EMPTY, so the tree that
    "failed" is the one building green), the bundler (real zip-it-and-ship-it zips both
    functions at nodejs20.x), `gatsby-adapter-netlify` (it writes to `.netlify/functions-internal`,
    a different directory), and the lockfile split. All correct, all useless: the cause was
    **`AWS_LAMBDA_JS_RUNTIME = nodejs12.x`**, an environment variable set on the Netlify site
    years ago and forgotten. `NODE_VERSION=20` and `.nvmrc` govern the BUILD node; the functions
    runtime is a separate setting that lives only in the hosting account. Nothing had needed it
    before because the site had never had a function. **When every in-repo hypothesis is
    eliminated, stop generating more — go read the deploy log.** One click settled what a large
    fan-out could not, and the agents' own synthesis said so and was ignored for one more round.

39. **A function that takes a thread may not read the thread.** `conversation_transcript(contact,
    thread)` renders the sender's own email and uses `thread` ONLY for the replier's name and
    date — the reply TEXT must be handed over separately as `their_reply`, which is why
    `_draft_reply` extracts the snippet first. Calling it with just the thread produced a
    transcript containing one side of the conversation, so the SMS draft for a contact who had
    replied could only restate, never continue. **The model caught it, not a test**: it refused
    with *"only Alejandro's initial email is shown"* instead of inventing a continuation. It was
    right and the prompt was wrong. A parameter being accepted is not evidence it is used.

40. **A contradiction in a prompt is not fixed by saying the other side louder.** The SMS
    standing block told the model a contact had replied and to never ask whether the email
    arrived. The touch ladder — arriving under the heading `THIS MESSAGE:` — told it to earn the
    channel, give the prior touchpoint and ask a yes/no, because it describes COLD outreach.
    The heading won every time, and drafts for someone who had answered still asked whether the
    message came through. Strengthening the standing text changed nothing. The fix was to
    REPLACE the ladder with a continuation intent, not append to it. Two instructions in one
    prompt disagreeing is a code bug, not a wording problem.

41. **Describing a control is not showing it, and accurate copy hides that.** The SMS tab
    rendered "No phone number for Blake — add one below to text them" plus the notes block. True,
    helpful, and reported TWICE as "I'm not seeing the text UI" by someone looking straight at
    it. An empty state that only describes what would appear reads as an empty tab. It renders
    the whole composer disabled now. The render test made it worse: it asserted on the sentence,
    so it passed happily for a tab showing nothing but the right words — the assertion has to be
    that the control EXISTS and is disabled, never that the copy is correct.

42. **The prompt's own example comes back verbatim — including when the example is a rule.**
    §Lessons 9 again, third occurrence, this time in prose rather than a worked example: the SMS
    prompt said to concede the channel with *"hope a text is okay — happy to move this back to
    email"*, and that exact sentence appeared in **all five** generated drafts. Several people at
    one company get texted, so a stock sentence across them proves a machine wrote it — worse
    than omitting the move entirely. Naming the phrasings as burned and demanding variation
    fixed it (0/5). **Generate against real data before believing a prompt**: reading it would
    never have shown this, and the same pass also caught a raw scraper title reaching a draft as
    "the Betterup uploaded job".

43. **A control nobody can find is a broken feature, and this shipped FOUR times in one day.**
    The SMS composer rendered a sentence describing a box instead of the box. The round-two
    panel returned `''` when it was not the right moment. The 🎯 Interview button went into the
    `⋯` overflow menu. And the won row was greyed with `#f8f9fa` against a white row — a **2.7%
    difference**, so the click saved state and changed nothing visible. Every one was reported as
    "it does nothing", and the last is the sharpest: the feature worked perfectly and the result
    was imperceptible. `restartButton` already carried the comment *"burying it made it
    unfindable"* directly above the interview button, and it was repeated anyway. Render the
    control disabled with the reason, put the important one on the row, and make the state change
    visible enough to see without looking for it.

44. **A partial result with nothing in it is a permanent silent death.** A Google careers URL
    (JavaScript-rendered) returned an empty shell. `status` is `"partial"` whenever no
    application_url is found, and that is the SUCCESS branch — so it stored an empty description,
    stamped `detail_scraped_at`, and set `detail_error` to **NULL**, erasing the one signal
    anything was wrong. `queue_needing_detail` requires `detail_scraped_at IS NULL`, so the row
    could never be retried; with no description, tailor and cover never ran. `prepare` completed
    in 0.54s reporting "0 imported URL(s)" and returncode 0. The row rendered exactly like a
    healthy one, which is why it was reported as *the whole dashboard breaking*. An empty result
    is a failure whatever the status field says, and it needs an error that names the way out.

45. **A sanitiser cannot clean a string that does not exist yet.** Every Python guard against em
    dashes ran correctly, and the PDF still had two. The `.txt` and `_DATA.json` were clean;
    `document.mjs` builds the education line itself with `join(' — ')`. The renderer runs AFTER
    the payload is scrubbed, so the check had to move to the renderer source. Found by the ATS
    round-trip check on its first live run, in a file every other check called clean — which is
    the argument for verifying the artifact that actually ships rather than the one before it.

46. **Read the file that gets SENT, not the one on the way to it.** "14 of 16 résumés have a junk
    header" was reported, loudly, from the `.txt` intermediate. Zero delivered PDFs contained the
    string. The renderer builds its header separately and infers a real role. Two further claims
    built on that same mistake also collapsed on inspection: the missing target-role line and the
    stale summary opening were both real ONCE and fixed on 2026-07-29, visible as a clean split
    in the file mtimes. Check the artifact, then check when it was made.

---

47. **A defensive read turns a missing column into a plausible value.** The interview button was
    reported broken three times. `dashboard_rows()` never SELECTed `interview_at`, so the
    payload shipped `""` forever and every downstream branch was dead — no grey row, no chip,
    no undo in the menu — while the WRITE worked perfectly and two live jobs carried a
    timestamp. What hid it for two rounds:
    `(row["interview_at"] if "interview_at" in row.keys() else "")`. Without that guard it
    would have 500'd on the first render and been fixed in a minute. **A column the payload
    needs belongs in the SELECT; if it is absent, crash.** Both earlier "fixes" — moving the
    button, changing the grey — were real improvements to code that was never running.

48. **A correct grep can support a wrong inference.** The same ticket claimed the follow-up
    ladders never stop, because `interview_at` appears in ZERO Python follow-up paths. True,
    and the conclusion was wrong: the stopping is imperative at mark time in
    `_mark_interview`, and more careful than the replacement written for it — it halts only
    channels with a real ladder. The duplicate also resurrected a sequence stopped by a REPLY,
    contradicting a tested decision. Two existing tests killed it on the first run. **Grep
    proves where a string is, not what the code does.**

49. **A rule implemented at one of its two call sites is not implemented.** §Lessons 20 wrote
    down that a board name counts as the employer when `_company_owns_the_posting` agrees.
    `derive_company` step 1 applied it to a stored name; `_host_label` never did. So
    `google.com/about/careers` resolved to no employer at all, on a job already applied to,
    with **8 known connections at Google** never searched. The rule had been written down for
    six days.

50. **Zero meant "unlimited" in two settings and "send nothing" in a third.** Asked to remove
    the outreach caps. `_COMPANY_CAP` was guarded by `> 0` and a zero-day cooldown matches
    nothing — but the daily limit compared `sent_today() >= 0`, which is true before the first
    email of the day. Setting it to 0 to turn the limit OFF would have blocked every send, and
    the refusal would have read *"daily send limit reached (0)"* — a message describing a cap
    that had just been switched off.

51. **A failure is not a verdict.** Reported as "the pipeline is fully broken" on a posting that
    scraped clean 90 seconds later: **13,602 characters, 5.1s, tier 1**. One 45-second timeout
    had stamped `detail_scraped_at`, which is what `queue_needing_detail` uses to decide a row
    is done — so a network blip retired a job permanently, and the run reported returncode 0.
    §Lessons 44's twin: that fix routed the empty-description case INTO a branch that was
    itself a dead end. Transient failures now stay queued; and **one run may only spend part of
    the retry budget**, or a thirty-second outage retires the entire queue at once.

52. **An ATS tenant slug is not the employer's name.** "There's no way Apollo has no contacts
    for Yahoo" — Apollo was right. The job was stored as **Ouryahoo**, read off
    `ouryahoo.wd5.myworkdayjobs.com`, and no such company exists. Slugs are chosen by an HR
    team and wrap the real name (`ouryahoo`, `WellsFargoJobs`, `acme-external`). Trimming
    affixes blind is not acceptable — **OurCrowd is a real company** — so a variant is accepted
    only when the posting's own text names it as a WHOLE WORD. The first version tested
    `variant in text` and produced "Crowd", which is §Lessons 1 inside the function written to
    respect it, and worse than the bug it fixed: it would have mailed strangers with more
    confidence than the wrong name had.

53. **A blended query cannot produce a mix, because the provider decides the composition.**
    "Too many talent acquisition people" — measured, the one query asking for the role title OR
    recruiter titles returned **25 recruiters and 0 peers**, because a bespoke multi-word title
    matches nobody. The ranking stage was then choosing five recruiters out of five and doing
    exactly what it was told. Two searches with a minimum each, INTERLEAVED — front-loading one
    side rebuilds the imbalance as soon as verification drops anyone.

54. **A band that catches half the table is not a reading, it is a default.** `COOLING` was the
    FALLBACK for every job with any effort and no reply — there was no band between "nothing
    sent" and "cold" — so it caught **10 of 22 jobs**, including Visa, applied that morning with
    six of eight emails already out. Reported as "weird, they are fairly recent", and the word
    *cooling* is the whole problem: it means decaying, and nothing had had time to decay. What
    was missing is **runway** — how much of the plan is left, which is what tells a sequence
    still running from one that is finished. Visa (6/8 emailed, applied today) and Webai (5/5
    emailed, 5/5 follow-ups, thirteen days) printed the same word and are opposite instructions.
    Now active 7 · cooling 4 · cold 0. **This did NOT re-open §Lessons 35**, and that is the
    part to keep: finishing the plan moves a job DOWN (`active` → `cooling`), so more messages
    with no answer still never reads better than fewer, and only a PERSON can reach `warm`.
    LinkedIn invites are excluded from runway for the same reason — `dm_status` has no
    `accepted` state, and counting them means nothing is ever spent.

55. **`cold` was measuring silence from `applied_at`, which is not when we last spoke.**
    Betterup read *"no answer from anyone in 15 days"* while the final follow-up had gone out
    **the day before**. Same job, opposite instruction: "give up" versus "you just nudged them,
    wait". Measured from the last thing WE sent, across every channel and every ladder — not
    from the one act at the start.

56. **Rounding applied to one of two paths is not rounding.** `_awaiting_us` builds the Next
    button's payload twice: the email path goes through `conversation_state`, which rounds, and
    the LinkedIn path added by UX-2 handed over the raw division. It shipped to the row as
    **`Answer Anna (0.20683377833333333h)`**. The helper's own docstring said "Whole hours" and
    it never was. §Lessons 49 in a new place, and the fix belongs at the payload rather than in
    the template because the JS also SORTS on that field. Second half: even a clean integer
    renders `0h` under an hour, while the last-interaction line an inch away says *just now* —
    one row, two facts.

57. **Go and read the DOM before writing the parser.** Two properties of LinkedIn's messaging
    markup decide the whole design of the thread reader, and neither is guessable: messages are
    **grouped**, so only the first of a run carries the sender and the timestamp; and there is
    **no machine-readable time anywhere** — `<time>` has a class and nothing else, no ISO
    string, no epoch, on any element in the list. Guessing either one produces a parser that
    looks right: continuations silently take the wrong direction (a two-message reply logs as
    "they wrote, you answered"), and every message in a group collides on
    `sha256(contact|kind|at)` so two of three vanish with a success response. One structural
    probe, no message text read, settled both.

58. **The deck beacon existed, was deployed, and could not see the name.** *(Closed 2026-08-06:
    the fix is a parse-time capture in `gatsby-ssr.js` → `window.__deckSlug`, and a real browser
    load is now recorded end to end.)* "Nobody has opened
    the deck" after ~98 emails was not false, it was UNKNOWABLE. Netlify rewrites `/intro/*` to
    `/intro/index.html` with a 200, so the browser keeps the name — then Gatsby hydrates, does
    not recognise `/intro/gina` as a route, and **replaces the URL with `/intro/`**. Measured:
    the tab title still read `/intro/zzprobe-live-check-b2` while `location.pathname` was
    already `/intro/`. The component's `useEffect` ran after that, hit its own
    `seg === "intro"` guard, and sent nothing. Two real browser loads produced zero hits while
    direct POSTs to the same endpoint landed fine.
    The README offered an inline `<script>` "or as a `useEffect` in the component". **The
    inline one runs at parse time and would have worked**; the parenthetical is the broken
    option. Fix: capture the slug before hydration (`gatsby-ssr.js` → `window.__deckSlug`).
    Live since 2026-08-05 — the first two real opens ever recorded arrived minutes later.

59. **I diagnosed that by grepping the HTML, and the HTML was the wrong artifact.** A `useEffect`
    compiles into a LAZILY-LOADED page chunk that the document references only by hash through
    the webpack runtime. Grepping the served HTML — and even the four `<script src>` bundles —
    reports "no beacon" against a working one. §Lessons 46 again, two commits after writing a
    check whose whole purpose was to stop exactly this. Resolve the chunk the way the framework
    does (`page-data.json` → `componentChunkName` → runtime id → hash), never by pattern.

60. **A regression test comparing two moving things proves nothing.**
    `test_a_default_space_changes_the_prompt_by_nothing` compared `space=None` against a default
    manifest and PASSED under a mutation that leaked a manifest field into both paths. It proved
    they were equal to each other, not that either matched what shipped. A golden FILE fixed it.
    The same shape as §Lessons 13, one level up: the baseline has to be still.

61. **A guard against a field being used must not grep for one spelling of it.** `UNAPPLIED`
    names manifest fields nothing reads yet, and its test looked for the literal `space.<field>`
    — surviving a mutation that read `manifest.tone`, which is the real holder's name in
    `_status_payload`. §Lessons 48 INSIDE the guard written to stop a field being declared and
    quietly used. It parses attribute access now.

62. **`hidden` is a user-agent rule, so any author `display` beats it.** `.controls{display:grid}`
    kept the jobs console on screen in a targets Space with `hidden` correctly set. The Node
    test asserted the PROPERTY, which was true and did nothing — §Lessons 41's shape. One line
    (`[hidden]{display:none !important}`) kills the whole class.

63. **I restarted the dashboard while an apply was running**, after the check printed
    `in_progress: 1`, because the check was chained into the same command as the restart instead
    of gating it. The documented failure, walked into with the warning on screen. `pgrep -fl
    "applypilot apply"` before ANY restart, as a separate step whose output you actually read.

64. **A correction that the system silently undoes is not a correction.** Three contacts were
    marked as having opened the deck and none had — the hits were direct POSTs from testing,
    made while no working beacon existed. Clearing `deck_viewed_at` did not stick: the collector
    is a ROLLING WINDOW of 500 that the poller re-reads IN FULL every five minutes, and it has
    no delete (POST a hit, GET the list, nothing else). Both contacts were back before the next
    command finished. `deck_hits.dismiss(slug, at)` is the suppression, keyed on the HIT and
    never the slug — "ignore katherine-j" would suppress her real open forever, and a missing
    signal nobody knows to look for is worse than a wrong one you can see.

65. **`deck_views` counted POLLS, not opens, and read 99 from one click.** Same root cause: the
    poller replays the whole window every five minutes, and `mark_deck_viewed` incremented on
    every call. 99 × 5 minutes = 8.2 hours, which matched that hit's age exactly. The column
    measured how long the dashboard had been open and looked like engagement. It has two modes
    now, because two callers know different things: the poller can COUNT the window, the manual
    import can only say "one more". **The test asserted the bug** —
    `assert deck_views == 2` after polling twice with ONE hit, with the word "Idempotent"
    written in a comment directly above it.

66. **Verifying a browser feature needs a browser, and the artifact is not the HTML.** Three
    wrong calls in one day on the same feature: I said the beacon was missing (it was in a
    lazily-loaded chunk the HTML names only by hash), then said two opens were real (no beacon
    existed at the time), then said a probe had failed after waiting 12 seconds (the collector
    lags up to a minute). Every one came from checking something adjacent to the thing that
    matters. `scripts/deck-check.sh` now resolves the chunk the way Gatsby does — `page-data.json`
    → `componentChunkName` → runtime id → hash — and checks the parse-time capture separately,
    because "no beacon" and "beacon that cannot see the name" have different fixes and look
    identical from the API side.

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

**CRM-4b lifts that, and `gmail.readonly` IS NOW GRANTED (2026-07-31).** `CONTENT_SCOPE` is
still deliberately **not** in `SCOPES` — no future scope addition can drag it along, and a test
pins that; it is added only by `network --gmail-connect --with-content`.

**Nothing is read automatically, ever.** The OAuth grant is all-or-nothing (Google has no
per-thread scope), so the narrowing cannot live in what we are *allowed* to read — it lives in
what we ever *do* read. `_sync_thread` stores **no message text at all**. Text arrives only via
`replies.fetch_thread_text()` (⤓ Fetch from Gmail, one thread) or by the operator pasting it.
`upsert_messages` preserving an existing snippet is therefore load-bearing, not defensive.

**`replies.sync_all_with()` searches by ADDRESS**, so a thread the other side started, an email
sent straight from Gmail, or one where they merely Cc'd you all arrive — the conversations the
CRM's memory used to stop short of, because everything was looked up by a `thread_id` captured
at send time. `q=` search needs `readonly`; metadata refuses it outright.

Stored text is capped (`SNIPPET_MAX` 200 auto / `PASTED_MAX` 2000 pasted) **at the write**, and
`cv.strip_quoted_tail()` drops the quoted original first — Gmail's snippet runs through the
quote header, so a short reply can be a third our own email quoted back.

## Texting a recruiter (2026-08-01)

The channel where getting it wrong costs the relationship, not the reply. A text interrupts:
lock screen, whatever hour it is sent, next to messages from their family. And **in most cases
the number came from Apollo, not from the recipient** — that is the whole difficulty, and the
prompt says so out loud rather than implying it. A prompt that only says "be respectful"
produces a polite sales text.

Every draft must: identify the sender in the first clause (they have not saved this number),
give the real prior touchpoint, concede the channel and offer to retreat to email, ask ONE
yes/no answerable at a traffic light, and be explicitly fine to ignore. **Never a link** — a URL
from an unrecognised number is the strongest spam signal there is, and carriers filter on it.
No urgency, no scarcity, no calendar ask.

**Standing is graded across five tiers**, not "did we email them": replied → emailed+invited →
emailed → invited → fully cold. The cold case needs the copy to work hardest and was previously
indistinguishable from having sent an invite; it is told to be shortest, not to sell, and not to
be charming. `_sms_permission()` — and a test asserts all five render differently AND that
exactly one is the weakest, after a mutation relabelled a tier while keeping its own wording.

**A contact who has REPLIED gets a different message, not a gentler one.** The touch ladder is
replaced, not appended to — see §Lessons 40 — and their actual words are passed as
`their_reply`, see §Lessons 39. Without both, the draft asks whether the email arrived, of
someone who answered it.

`applypilot`'s own numbers: 15 tests in `tests/test_sms_prompt.py`, mutation-verified. Two
survived the first pass — one was `"WEAK" in "WEAKEST…"`, **§Lessons 1 inside the test written
to guard the grading**.

## No em dashes, anywhere a recipient reads (2026-08-03)

An em dash in a cold email is the clearest "pasted out of a chatbot" signal there is, and a
reader who spots one re-reads the whole message as machine-written. Belt AND braces, because a
prompt instruction is not a guarantee (§Lessons 9, 12): every generation prompt forbids them AND
`strip_ai_dashes` removes them anyway. Covers emails, LinkedIn notes, texts, cover letters,
résumés. Source comments are internal and untouched.

Four holes the obvious fix missed: `U+2015`/`U+2212` lookalikes (`U+2212` maps to a HYPHEN — a
comma would invert "−20%"); a dash opening a LINE is a bullet, not a clause break; the résumé
assembler's ORIGINAL-text fallbacks; and the base résumé itself, which had one. Then two more:
`outreach.py` contained **93 em dashes**, so the rule banning them arrived in a prompt saturated
with them, and the Node renderer adds its own downstream of every Python guard (§Lessons 45).

## ATS: verify what a parser reads back (`scoring/ats.py`, 2026-08-03)

Asked for invisible text and keyword stuffing; this is the legitimate version, and it found more
than the trick would have. Two checks, no new dependency.

**The round trip** extracts the text back OUT of the PDF and asserts a screener can see the
name, email, phone, employers and standard headings. This is what §Lessons 10 needed: a layout
crash fell through to the HTML renderer, which wrote a **380-character PDF with no WORK
EXPERIENCE** and it went out on real applications. Runs on BOTH render paths, advisory rather
than blocking, and failures reach the Activity tab. With no extractor installed it returns
`ok=None` — "could not check" and "the PDF is fine" are opposite findings.

**Keyword coverage is a REPORT, never an inserter.** Where a term is true, using the posting's
literal word helps a parser that matches strings rather than meanings; where it is not true the
gap stays. About half of any posting's terms describe work the candidate has never done and only
they can say which half. Term quality took four passes against the real corpus — the first
version's top "missing keywords" were `Additional Perks`, `BENEFITS` and `California Employees`.
`applypilot ats [--job X]` runs both.

## Engagement signals — what is detectable, and what is not

Established by LOOKING at the real mailbox, not by guessing:

| Signal | How | Status |
|---|---|---|
| Replied | `messages` | automatic |
| **Booked a call** | cal.com emails the host — verified (`hello@cal.com`, "30 Min Meeting between …") | automatic |
| **Opened the intro deck** | first-party beacon on the sender's OWN site → Netlify Blobs → polled every 5 min | **PROVEN live 2026-08-06 17:07** — a real browser load of a named link recorded end to end, the first ever. Believed live from 2026-08-01 and recorded nothing for six weeks and ~113 emails (§Lessons 58). Verify with `sh scripts/deck-check.sh`; BOTH lines must be affirmative, and the collector lags a real click by up to a minute |
| **Viewed your LinkedIn profile** | **not detectable** — absent from the LinkedIn data export AND no notification email exists. Only LinkedIn's UI has it, and automating that was abandoned twice (§Lessons 3) | operator-logged, tagged `noted` |

**Email OPEN tracking was rejected outright.** A pixel fires when Gmail proxies and caches the
image, when Apple Mail pre-fetches it, and when a corporate gateway scans the message — it
measures machines. A click does not.

**Deck links are a NAMED PATH**: `/intro/gina`, never `?v=<token>`. Both identify the reader;
only one looks like it, in the one message whose point is sounding personal. The slug is
STORED (`contacts.deck_slug`) because it cannot be derived — two people are often called Gina —
assigned once and never moved, since links are already in inboxes. A second Gina gets `gina-b`,
not `gina-2f9c`. Gated by `INTRO_DECK_PATHS` (default OFF in code, **ON here since
2026-08-01**) — §Lessons 32: the scheme was switched once while the rewrite was still
uncommitted and four recruiters got a 404. The Netlify rewrite `/intro/* → /intro/index.html`
(status 200, a REWRITE — a 301 would strip the name) is a wildcard: any name works, unlimited,
no rebuild, **no page is created per person and there is nothing to clean up**.
`applypilot deck-relink` repoints existing drafts without regenerating them, and never touches a
sent one — that draft is the only record of what went out.

**The collector is two Netlify Functions** (`deploy/netlify/functions/`), deployed alone.
`deck-hit.mjs` stores `{slug, at}` in a Netlify Blob (rolling window of 500, self-trimming — so
retention needs no cleanup job); `deck-hits.mjs` serves them behind `DECK_HITS_TOKEN` and
returns **503 rather than serving openly** when that variable is unset, because a collector that
answers everyone because its config is missing looks like it is working. ApplyPilot POLLS over
outbound HTTPS — the dashboard binds `127.0.0.1` and cannot receive a webhook, and that property
is worth keeping. `deck_hits.poll()` is idempotent, so the window is re-read whole every time.

**`deploy/netlify/` is the canonical copy.** The site repo diverged from it once and the drift
was a `deck-hit.mjs` validating `body.v` against an 8-hex regex while the live beacon sent
`{slug:"gina"}` — it would return **204, its success code**, on every real click and store
nothing. `git checkout <old-commit> -- netlify/functions/` is the obvious move and the wrong one.

**Never open your own `/intro/<name>` link** — it records that person opening the deck. Any
made-up name returns 200 and matches nobody, which is what `deck-check.sh --probe` generates.
Append **`?notrack=1`** once per browser to opt that device out permanently; without it,
previewing what you sent somebody records them as having read it, which is exactly how three
false "opens" ended up in the database on 2026-08-05/06 (§Lessons 64).

**The beacon must run at PARSE TIME, not in a `useEffect`.** The Netlify rewrite keeps the name
in the URL; Gatsby then hydrates, does not recognise `/intro/gina` as a route, and replaces the
URL with `/intro/` before any effect runs. `gatsby-ssr.js` captures it into `window.__deckSlug`
first. `deploy/netlify/README.md` step 3 has the exact snippet and says why the other option
cannot work.

## Sign-in walls are a per-EMPLOYER cost (2026-08-03)

**5 of the first 19 applications died at one** — Arm, Salesforce, Deloitte, Google, Yahoo — and
every one was treated as a per-JOB failure. It is not. **An account is per ATS tenant**: one
Salesforce Workday account covers every Salesforce job forever, and Greenhouse, Lever and Ashby
need none at all.

| | |
|---|---|
| `domain/authrealm.py` | URL → the realm one account covers. Pure. |
| `repo/accounts.py` | `ats_accounts`. `kind` (does this site wall you) is separate from `have_account` (do we have one) — a registration and an expired session look identical in the browser and are not the same problem. |
| `apply/accounts.py` | `preflight()` before the agent launches; `note_wall()` learns from one. |
| `apply/profile_scan.py` | Reads the apply browser for evidence. **Never reads a password value** — the queries select `origin_url` and the column name appears nowhere in the file. |

**Seeded from the browser, which already knew.** Saved credentials proved accounts at Google,
Yahoo and Salesforce — three of the five employers that had blocked an application — and
nothing had ever asked. **Cookies are NOT accepted as proof**: Workday sets one on an anonymous
job view, so a cookie is a hint to ask the operator about, never an answer (§Lessons 34).

**`preflight` skips the launch entirely** when a realm needs an account we do not have. Before
it, that discovery cost a Chrome launch, a Claude run and 59 seconds — repeated for every job at
the same employer, because the finding was recorded on the JOB.

**The apply profile no longer carries credentials.** `chrome.py:setup_worker_profile` copies the
operator's real Chrome profile, which is how sessions persist — and was also copying **682 saved
passwords, 2 credit cards and 831 autofill entries** into the browser the agent drives with
`bypassPermissions` on attacker-controlled careers pages. Excluded from the copy and purgeable
from the Accounts panel. **Cookies stay**, so no wall is paid twice.

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
`CRM-4b` — 2026-07-30/31). `gmail.readonly` is now GRANTED on this machine, but nothing reads
automatically — see §Engagement signals.

**`DISC-1` was re-scored on 2026-08-03 and is NOT the gap it looked like.** Discovery has
produced 0 jobs and all 19 were pasted by hand — but the operator finds them on LinkedIn, whose
job feed is a RECOMMENDER (profile, network, behaviour, a ranking model). ApplyPilot's discovery
is 2,137 lines of keyword SEARCH across boards. Those are not two implementations of the same
idea; for RELEVANCE the recommender wins, and running ours would buy throughput at worse
precision that the operator would then have to filter back down. Do not "fix" this by default.

**Open, and the real ceiling:** nothing could tell you what works. 77 emails, 2 replies, and no
way to ask whether the personalised ones did better — so every improvement to the copy was
unfalsifiable. `draft_variant` (2026-08-03) starts fixing it, but nothing is readable until
enough tagged sends accumulate; `MIN_MEANINGFUL_N` is 10.

~~**Half-finished:** intro-deck click collection.~~ **Genuinely done 2026-08-06** — a real
browser load of `/intro/<name>` recorded end to end for the first time. It was believed done on
2026-08-01 and recorded nothing for six weeks; see §Lessons 58/59/64. The 2026-08-01 note below
is kept because its own diagnosis was right and its conclusion was wrong:

~~**DONE 2026-08-01 — the whole chain is live**, and the cause of the two "build failures" was finally seen: **`AWS_LAMBDA_JS_RUNTIME =
nodejs12.x`**, a variable set on the Netlify site years ago and forgotten. Deleting it was the
entire fix; the function code shipped unchanged (§Lessons 38). `INTRO_DECK_PATHS=1`, 12 drafts
relinked, 26 already-sent ones correctly untouched.

Verified against the live site rather than assumed: write 204, invalid slug 204, wrong method
405, unauthenticated read 401, wrong token 401, authenticated read round-trips the hit, and
`/intro/<name>` still 200. ApplyPilot then pulled the probes and recorded **0** — they match no
contact, which is the designed behaviour and the reason throwaway slugs were used.

**Do not open your own `/intro/<name>` links.** Loading `/intro/dinara` records *Dinara opened
the deck*. `/intro/<anything-made-up>` returns 200 and matches nobody.

`CRM-1` (reply detection) is the one that changed what the app can *do*: it found a real reply
nobody knew about and an address that had been bouncing silently since Jul 16.

The ARCH-first order was chosen against the analysis's advice. The reason it was contentious
is still true and worth remembering: **the ARCH set delivered no user-visible change.** All 9
jobs were pasted in by hand (discovery has produced **0**), and the system is still blind to
replies. The tradeoff was raised and accepted; don't re-litigate it, don't forget it either.

What the ARCH set *did* buy became visible immediately afterwards: the first real end-to-end
apply surfaced three bugs (§Lessons 8–10), and every one was diagnosable in minutes because
the data layer, the query budget and the validators existed to measure against.

**`docs/spaces-prd.md` is largely BUILT** (v2, 2026-08-04): SPACE-1a, 1, 2, 3, 3b, 4 and 4b all
shipped 2026-08-04/05 on the `spaces` branch — see §Spaces. Read `docs/tickets/SPACE-1a-*.md`
first: it is the authority where it and the PRD disagree, and three of the PRD's claims did not
survive contact with the code.

Still open, in the order they matter: **ID-1/ID-2** (per-identity mailbox, deck and limits — the
`identities` table exists and is read by nothing, and until it is wired a business Space cannot
exist), **SPACE-0** (archive terminal rows, independent, half a day, and it is the
endless-scroll complaint that started all of this), **SPACE-6** (the business Space as a
falsifier — if it costs code, the PRD's central claim was wrong and should say so).

`docs/crm-prd.md` is the larger person-as-root version of the same idea. Not superseded — it is
the upgrade path §11 of the Spaces PRD points at. Do the graph when a real question needs it.

`docs/tickets/UX-README.md` — six dashboard defects reported 2026-08-04, **all six shipped**.
Three were the same failure: a value one layer computes that the other cannot see.

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

3. ~~**`derive_company` returns None for some employers' own careers sites.**~~ **CLOSED
   2026-08-04** — §Lessons 49. Kept for the shape: the rule had been written down in §Lessons
   20 for six days and was implemented at one of its two call sites, so a job already applied
   to had 8 known connections at Google sitting unsearched. Fixing it, plus the tenant-slug and
   `.edu` bugs, took contacts from **66 to 185**.

4. **15 modules still execute SQL directly** (was 21) — `apply/launcher.py`,
   `enrichment/detail.py`, `view.py`, `cli.py`, `pipeline.py`, the three discovery scrapers,
   three scoring stages, and four `networking/` modules. The dashboard is at zero.
   `test_sql_lives_only_in_the_data_layer` names the remainder in an allowlist, so the list can
   only shrink and no NEW module can join it. Deliberately deferred; see ARCH-4's ticket.

5. **`web_dashboard.py` is 3,568 lines**, all Python, zero SQL. ~430 lines are pipeline
   orchestration (`run_dashboard_prepare/apply/fill_one/restart/continue`) that are not HTTP
   concerns. Extracting them is the natural companion to debt item 1.

9. **`identities` exists and nothing reads it.** Created by migration 003 with `token_path`,
   `from_name`, `deck_base_url`, `daily_limit` and the collector columns; ID-1 is what wires
   them. Until then every Space sends from the one personal mailbox, and `identity_id` freezes
   on first send — so **do not create a business Space yet**.

10. **The `spaces` branch is 12 commits ahead of `main` and unmerged.** Everything above is on
    it. `main` has none of it.

6. ~~**No per-company outreach cap.**~~ **CLOSED 2026-08-03** (`OUTREACH_COMPANY_CAP`, default
   8). Kept for the number: six companies were already OVER the cap the moment it shipped, three
   of them at 10 emails. Nothing had counted per employer, because the daily limit is global and
   the cooldown is per address. **All three caps are set to 0 (unlimited) on this machine since
   2026-08-04, deliberately** — see §Lessons 50 for what 0 used to mean.

7. **`@react-pdf` is a major version behind** (3.4.5 installed, 4.5.1 current). The textkit
   layout crash in §Lessons 10 may be fixed upstream; the renderer now survives it either way,
   so this is cleanup rather than a fix.

8. **Résumé quality is measured but not judged.** `verbatim_bullets`, `understated_experience`
   and the dropped-tool check are mechanical. The LLM fabrication judge now *runs* (ARCH-6 era
   fix: the dashboard had hardcoded `lenient` in three places) but has not yet caught a real
   fabrication — it is unproven, not trusted.

---

## Security posture (audited 2026-07-31)

**The GitHub repo is a PUBLIC fork of `Pickle-Pixel/ApplyPilot` and cannot be made private** —
GitHub refuses to change a fork's visibility, because it would let private history escape a
public network. The routes are: ask Support to detach the fork, or migrate to a new private
repo. Neither has been done; public was accepted deliberately.

That is safe *today*, and this was verified rather than assumed:

- **Nothing sensitive is tracked, and nothing sensitive was ever committed** — checked across
  all branches with `git log --all --diff-filter=A`. The only match is `.env.example`.
- **Every secret lives in `~/.applypilot/`, outside the repo directory.** A clone gets code and
  docs. `.gitignore` (`*.db`, `profile.json`, `resume.txt`, `*.env`) is the second layer, not
  the first.
- **The dashboard binds `127.0.0.1` only** (confirmed with `lsof`, not just the source) and has
  an Origin/CSRF guard against DNS rebinding.
- **Granting `gmail.readonly` did NOT widen the apply agent's blast radius.** The agent runs
  `bypassPermissions` on attacker-controlled careers pages, but is denied `Bash`, `Read`,
  `Write`, `Edit`, `Glob`, `Grep`, `WebFetch`, `WebSearch` and `Task` — so it cannot read
  `~/.applypilot/gmail_token.json`. Gmail read tools are denied twice (allowlist + deny-list).
  This is the check to re-run before granting the agent anything new.

**`.githooks/pre-commit` blocks a commit that would publish a secret** — credential-shaped
filenames and secret-shaped content. Enable on a fresh clone with `sh scripts/install-hooks.sh`
(`core.hooksPath` is local config, so it does NOT travel with the repo). 19 tests run the real
hook, including 5 benign edits that must NOT be blocked — a noisy guard gets `--no-verify`d out
of habit. It found a bug in itself on first use: the `PRIVATE KEY` pattern starts with `-`, so
grep parsed it as an option and that check had never run.

**The apply browser no longer carries credentials** (2026-08-03). `setup_worker_profile` was
copying the operator's whole Chrome profile — **682 saved passwords, 2 credit cards, 831
autofill entries**, including a bank and an Apple ID — into the browser the agent drives with
`bypassPermissions` on attacker-controlled careers pages. `Login Data`, `Web Data` and the
autofill databases are excluded from the copy and purgeable from the Accounts panel. **Cookies
stay**, so sessions persist and no sign-in wall is paid twice. `apply/profile_scan.py` never
reads a password VALUE — its queries select `origin_url`, and the column name appears nowhere
in the file, which is a property a reader can check rather than an intention.

**What remains genuinely exposed:** the token is unencrypted on disk (`chmod 600` + FileVault
is the whole protection, so anything running as this user can read it), and the DB now holds
correspondence snippets — including, since UX-2, pasted LinkedIn messages. Kill switch:
<https://myaccount.google.com/permissions>.

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
- **Check for in-flight applies before ANY restart or reinstall, as a SEPARATE command whose
  output you read.** The apply is a child of the dashboard, so it dies with the server. Three
  times on 2026-07-30, and again on 2026-08-05 — that time the check printed `in_progress: 1`
  and the restart ran anyway, because both were in one chained command (§Lessons 63). Use
  `pgrep -fl "applypilot apply"`; recover an orphaned lock with
  `release_stale_locks(max_age_minutes=0)` and ONLY after pgrep comes back empty.
- **On branch `spaces`, 12 commits ahead of `main`, unmerged and unpushed** (2026-08-05).
  `main` last pushed at **`e1f0be6`**. Tags: `stable-arch2/3/5/6` · `stable-e2e-20260730` ·
  `stable-crm-20260731`.
- **Deck tracking has its own check**: `sh scripts/deck-check.sh --probe`. It tests the PAGE,
  not the API — every API-level check passed for the six weeks it was broken. Both lines must
  read affirmatively; open the probe URL in a real browser (curl runs no JavaScript) and allow
  up to a minute before calling it a failure.
- **`applypilot ats`** is the fastest way to check a résumé is readable and see keyword gaps.
- **All 28 existing résumé PDFs carry an em dash** from the old renderer and need a re-render;
  nothing rewrites a PDF in place.
- Working tree was previously at **`b9163ac`** — deck tracking live + the SMS channel,
  merged and pushed. Tags: `stable-arch2/3/5/6` · `stable-e2e-20260730` · `stable-crm-20260731`.
- **Check `git log --oneline -1` before believing anything about this repo.** Found on
  2026-08-02 checked out on `crm-phase-1` (an ancestor of `main`, nothing unique on it) with
  ~50 of main's files showing as *staged* — they only look staged because HEAD is an older
  commit. Nothing was lost; `git checkout main` fixed it, and the tree already matched.
  Alongside it were four **`" 2"`-suffixed duplicate files** (`web_dashboard 2.py`,
  `service 2.py`, `dashboard 2.css`, `test_repo_jobs 2.py`) — macOS/iCloud sync-conflict copies
  dated two days stale, containing none of the recent work. They **fail two tests**: the
  SQL-boundary check counts them as new SQL-executing modules, and the duplicate test file runs
  twice. Delete them; verify with `git log` and a clean `pytest` before diagnosing anything.
- **A tag restores CODE only.** `~/.applypilot/` — 16 jobs, 64 contacts, 34 sent emails, 54
  stored messages, 899 connections — is not in git and needs its own backup
  (`~/.applypilot/backups/`). Nothing does this automatically. Latest:
  `applypilot-20260731-crm-phase-closed.db`. Use the **sqlite3 backup API, not `cp`** — the WAL
  routinely holds more than the main file does (4.1 MB against 688 KB once), so a file copy
  silently loses everything recent. Still unprotected and not in git: `resume.txt` (the template
  every tailored résumé derives from), `profile.json`, `.env`, `gmail_token.json`.

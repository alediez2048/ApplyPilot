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
- **Tests:** 2511 passing (`tests/`, 128 files) · ruff clean (line-length 120, py311) · ESLint clean
- **Schema version:** 4 (`applypilot migrate --status`) · **Settings:** 52 declared in `settings.py`
- **Branch:** everything current lives on `context`, **90 commits ahead of `main`**, pushed to `origin/context`, working tree CLEAN as of 2026-08-13 (§Dev workflow). `main` has
  none of it. Check `git log --oneline -1` before believing anything here (§Dev workflow).

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

**Co-pilot is one job per BROWSER** (`APPLY_WORKERS`, default 3 — 2026-08-13). It ends by handing
an open browser to a human, and starting another apply *on that slot* closes it. Each slot has
its own CDP port (`9222 + id`) and its own Chrome profile, so N applications fill in parallel and
each waits for the operator independently. A slot stops pulling work while it holds a filled form
or a login wall; a slot whose job applied or failed takes the next one. See §Parallel apply and
§Lessons 8 — which cost two filled applications, and whose real cause was a shared PORT rather
than a shared queue.

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
| `web_dashboard.py` | **The operator dashboard.** 4,095 lines, **zero SQL** — data access goes through `repo/` and `store.py` (ARCH-4). |
| `repo/jobs.py` | Every `jobs` query as a named function. Owns `QUEUE_SQL` (provenance — how a row arrived) and `_in_spaces` / `_one_space` (membership — which panel it is in). Those two never do each other's job (SPACE-1a D2). |
| `repo/spaces.py` | The `spaces` / `identities` registries. `jobs_shaped_ids()` and `document_making_ids()` gate the pipeline stages and RAISE on an empty registry rather than returning `[]`. |
| `scoring/resume_sections.py` | Parses the BASE résumé into its own sections. The base résumé is the template; tailoring rewrites content inside it. |
| `settings.py` | **Every env var, one registry.** Types, defaults, validators, secret flags. Malformed values fail at startup naming the variable. `.env.example` is generated from it. |
| `migrations/` | Numbered `mNNN_*.py` with `up(conn)`, run at startup after the additive column pass. **Migrations must be idempotent** — this app gets killed mid-operation. `001` = the ARCH-3 touches backfill, `002` = messages per contact, `003` = the `spaces` / `identities` registries, `004` = GRAN-1's transcripts. **A migration must never touch a column the additive dicts declare** — they race (§Spaces). |
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
| `space.py` | **What a Space IS** — a frozen manifest, shaped after `followup.Channel`. `shape` + `tailor_docs` gate the pipeline queues; `tone`/`offer` reach the prompts; `schedules`/`channels` drive the ladders; `offer_deck` and `can_autosend` gate the deck link and the send path; **`voice` decides which system prompt writes the email** (SHEET-1). Everything but the five COLUMNS rides in a `config` JSON blob, so a new field is never a schema change. |
| `target.py` | A company you STATE, not an employer recovered from a URL. `anchor(space, name)` → `target:<space>:<slug>`, hashed into every contact key. `parse_input()` returns rejects rather than dropping them. A line containing a TAB is refused — it is a spreadsheet row in the wrong box, and accepting one made 106 cards each named after a whole row. A URL **path** is never a company name either (§Lessons 92). |
| `sheet.py` | **A pasted spreadsheet → companies and the people at them** (SHEET-1). One PERSON per row with their company repeated; grouping them under the company IS the bundling. Columns are matched by HEADER, never position — every export orders them differently and a positional parser files job titles as names. `First`/`Last` are joined. Rejects come back per ROW with the sheet's own line number. Google Sheets puts TSV on the clipboard, so **pasting is the entire integration**: no OAuth, no API key, nothing to revoke — and a pasted LINK is refused with the reason, because reading a sheet from its URL needs credentials. |
| `temperature.py` | How an application is DOING, not how far it has travelled. Bands answer **is anything still in motion**, from an interview backwards. **Only a PERSON can reach `warm`**, and finishing the plan moves a job DOWN. §Lessons 54, 55. |
| `joblink.py` | URL normalisation, for SEEING THROUGH rather than sending. `derive.unwrap_job_urls` calls `clean_link` before any employer rule reads a hostname — without it an ad redirect makes every rule describe the distributor (§Lessons 79). Strips attribution params, unwraps redirects, **never rewrites paths**. |
| `jobref.py` | How to NAME a job in a message: `{title, req}`. The title is the hard half — 11 of 33 live rows carry one that must never be quoted (§Lessons 84). `""` is a real answer and the caller must handle it. |
| `geo.py` | Places contact discovery will not keep people from (`OUTREACH_EXCLUDE_LOCATIONS`, default `India`). A country is matched through its CITIES, because Apollo returns both "Bengaluru" and "Bengaluru, Karnataka, India". Whole words — "India" is inside **Indiana**. Three cities are AMBIGUOUS (`delhi`, `madras`, `hyderabad`) and need their country named, because Delhi/Ohio and Madras/Oregon are real. A blank location is always KEPT. |

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
| `outreach.py` / `prompt.py` | Drafts: cold email + LinkedIn note + **email follow-ups** + **texts** (`draft_sms`), each written for its touch position. THREE system prompts, chosen by `Space.voice`: `_SYSTEM` (job seeker), `_PITCH_SYSTEM` (proposing work), `_PREMISE_SYSTEM` (the campaign paragraph IS the message). LinkedIn follow-up drafting still exists and is unreachable — that channel has no ladder since 2026-08-11. |
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
| `jobs` (37 cols) | `database.py` | The 6-stage state machine. `_ALL_COLUMNS` is its source of truth. Holds **targets too** — a targets row is a `jobs` row keyed `target:<space>:<slug>` (SPACE-1a D1). |
| `contacts` (42 cols) | `networking/store.py` | People per job + outreach + verification. `test_a_migrated_database_has_no_ladder_columns_left` locks the COUNT, so a new column has to be argued for in that test. |
| `touches` | `networking/touches.py` | One follow-up touch per row, ANY channel. `seq` is per (contact, channel). |
| `sequences` | `networking/touches.py` | Terminal state per (contact, channel): `stopped` / `replied`. |
| `connections` | `networking/connections.py` | Imported LinkedIn CSV. |
| `messages` | `networking/messages.py` | **CRM-4 conversation memory.** Headers plus ONE content column: `snippet`, capped at the WRITE (`SNIPPET_MAX` 200 auto / `PASTED_MAX` 2000 pasted) and **decoded** there too, since Gmail returns it HTML-escaped (§Lessons 90). *This row said "no body/snippet column exists, and a test asserts it" for two sessions — CRM-4b added one and the index was never corrected.* Keyed by `(message_id, contact_id)`, so re-syncing is a no-op. `rfc_message_id` is what lets a reply chain `References` across the whole thread. |
| `interactions` | `networking/interactions_store.py` | Events with nowhere else to live: a detected booking, an operator-logged LinkedIn profile view. Derived facts are NOT copied here — they are computed at render time so they cannot drift. |
| `transcripts` / `transcript_contacts` | `networking/transcripts.py` | **GRAN-1 meeting transcripts.** One row per MEETING plus a join, because a call has several attendees and storing 20-50KB per person duplicates the blob. NOT in `messages` — that is keyed on Gmail's own message id with `thread_id`/`rfc_message_id`, which a meeting has none of, and its `snippet` caps at 200/2000 (live mean 75). `matched_by` is provenance: `manual` (the operator chose) vs `email` (an exact address match) — §Lessons 34/86, a guess laundered into a stored fact. |
| `job_events` | `database.py` | Per-job activity log. Append is best-effort, never raises. |

| `ats_accounts` | `repo/accounts.py` | One row per **auth realm** — the thing one sign-in covers. `have_account` (about us) is deliberately separate from `kind` (about the site). |
| `spaces` | `repo/spaces.py` | One row per campaign. Five columns + a `config` JSON blob. Seeded by migration 003. |
| `identities` | `repo/spaces.py` | One row per SENDER (mailbox, from-name, deck, limits). Created by 003, **read by nothing yet** — ID-1. |
| `schema_migrations` | `migrations/` | Version, status, `claimed_at` lease. See §Lessons on the 300s lease. |

Live counts (2026-08-12, a snapshot — these move within minutes of real use, so treat them as
orders of magnitude and re-measure before reasoning from one): jobs **80** (29 applied,
2 rejected, **1 ghost**, 1 needs_human, 1 failed — and **46 with no apply_status at all**,
because 45 of them are company cards that were never applied to), contacts **352**
(156 emailed, **12 replied**), touches 233, messages 393, connections 899.
**Four Spaces**, and the row count is no longer mostly job-search:

| Space | template | shape | rows | contacts (by job) |
|---|---|---|---|---|
| `job-search` | jobs | pipeline/jobs | 34 | 250 |
| `gauntlet` | jobs | pipeline/jobs | 2 | 10 |
| `partnerships` | outreach | pipeline/targets | **3** | 0 |
| `sheet-search` ("Lead Sheet") | **sheet** | pipeline/targets | 45 | 105 |

**`partnerships` is no longer empty** (3 rows, 2026-08-12). It held zero from SPACE-3 until now,
which is why SHEET-1 doubled as the falsifier the PRD asked for — the targets shape had never
once run (§Spaces, §The sheet Space).

**`contacts.space_id` disagrees with the job's on 14 rows, and nothing reports it** (measured
2026-08-12): 10 on gauntlet, 3 on partnerships, 1 on sheet-search, every one of them filed
`job-search` — the column DEFAULT. §Lessons 70 at a third write path, and the read side hides it
exactly as before, because every panel scopes by the JOB rather than by this column. What does
read it is CRM-2's `all_contacts_for_metrics(space_id=…)`, so per-Space reply rates are wrong by
those 14. Not yet fixed; a backfill from `jobs.space_id` is the whole repair.
**Schema version 3**, and SHEET-1 needed no migration.

`contacts.source` is now the honest split: apollo 206, **import 105**, connection 28,
**manual 10**, hunter 5, introduction 1. Manual went 6 → 10 the day the ＋ tabs shipped. That column is what CRM-2's `by_layer()` divides by, and it
is the reason an imported contact is never filed as `apollo`.

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
apply_error,agent_id,verification_confidence`), plus `rejected_at`, and the two operator-typed ones (`job_context`, `job_ask` — CTX-2, §Outreach context).

**`contacts` groups:** identity · outreach(`outreach_subject/message/status,sent_message_id`) ·
threading(`thread_id,rfc_message_id`) · LinkedIn invite(`dm_status,dm_sent_at`) ·
operator(`phone,notes`,**`flagged_at`** — the 💡 marker, the one signal on a contact the human DECIDES rather than the system derives) · verification(`confidence,verify_note`) · SMS(`sms_sent_at`) · deck(`deck_slug,deck_viewed_at,deck_last_at,deck_views`) · **`location`** (from ENRICHMENT, never the search — §Lessons 91).
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

**Two filter axes, ANDed** (2026-08-06). Status buckets (`All · Needs you · In progress ·
Applied · Rejected`) answer *where is this in my pipeline*; band pills (`won · warm · active ·
cooling · cold · new · undeliverable`) answer *how is it doing*. Folding the bands into
`JOB_BUCKETS` would have made picking one silently clear the other, and "applied AND warm" is
the actual question — so they are separate groups with a visible divider. Each group's counts
are computed with the OTHER group's filter applied, so a pill's number is what clicking it
yields. An empty band gets no pill **except the selected one**, or the active filter vanishes
when its last job changes band and the table looks broken with no way back. There is no "All"
pill in the band group (seven plus an eighth is a wall of chips), so clicking the selected band
clears it. Band tints are `tb-` prefixed because **one band is called `active`, which is also
the selected-state class** — unprefixed, that pill renders as permanently applied.

**Every filter pill explains itself on hover** — `data-tip`, not `title` (the native one waits a
second, cannot be styled, and would stack a second box under the CSS one), with `aria-label`
carrying the same words because a `::after` is invisible to a screen reader. A standing legend
was built first and removed the same day: an explanation attached to the control it describes is
read at the moment it is needed. **Each tip states its axis** ("They responded — …" against
"Your outreach — …"), composed from `axis + meaning` rather than written per band. The legend's
GROUPING was the actual explanation — four bands count our own sending, two are about what they
did — and a per-pill tip has nowhere to put it, so dropping the prefix would keep the vocabulary
and throw away the point.

**THE COUNTER IS GLOBAL, THE TAB IS PER JOB** (2026-08-13). Reported as the follow-ups feature
not working end to end: the counter said follow-ups were pending, the tab had none to draft or
send. **Both numbers were right.** Measured on `job-search`: **19 due across 6 jobs, and 24 jobs
showing an EMPTY Follow-ups tab** — so four times out of five, opening the tab the badge sent you
looking for lands on one with nothing in it, saying *"Nothing due right now"*, which is true of
that job and useless beside a counter reporting the Space.
Verified end to end before changing anything, because *not working* and *working and pointing
nowhere* need different fixes: the panel matches the contacts on every job in every Space, the
tab renders its cards, the click path opens the right job with 5 cards and 7 buttons on screen,
and the console is clean. Nothing was broken. The empty tab now names the count, the number of
other applications and the first one, and is silent when there is genuinely nothing anywhere.

**Bulk email follow-ups — on the JOB's Follow-ups tab** (2026-08-10). Fifty-seven were due and
none had a draft, so clicking through them one at a time was the alternative.

It shipped first as `✉ Follow-ups (57)` in the top console and was **reported three times as not
existing** (§Lessons 89). It was there and it was in the wrong room. `fuBulkBar` now renders
above the per-contact cards on a job's Follow-ups tab, where each person already has their own
`✍ Draft follow-up`: `3 due · 0 drafted · 3 not yet · [✍ Draft all 3] [Send all drafted]`.
**Draft and Send act on DIFFERENT people** — drafting skips anyone who already has one, or it
silently discards hand-edits; sending touches only what is written. Each card highlights as the
batch reaches it (blue → green/red), and the highlight is applied BEFORE the request so the set
is visible before anything happens rather than only after. It does not render below two
contacts, because one person is just the button already on their card.

It **loops `_followup_action`** rather than reimplementing a send: every guard the single-contact path has —
the Space's `can_autosend`, the channel's, the daily limit, the per-company cap, a terminal
sequence, "no draft yet" — is inherited, and a new one is inherited too. A second send path would
be §Lessons 49 aimed at the least reversible action in the app.

**The browser names the contacts; the server never re-derives "everything due"** — the reply
poller moves that set every five minutes, so a re-derivation could send a message the operator
was never shown. The panel groups by **employer** because that is the unit the recipient
experiences (6 to Okta inside a minute is not the same act as 6 to six companies, and a flat
count of 57 hides it), and the send confirm names the count and the employers rather than asking
"are you sure?". A batch-wide refusal (daily limit, auto-send off) stops the run; a per-contact
one does not, or one stale row cancels the other fifty-six. Email only — LinkedIn and SMS are
copy-paste by design, and `stop`/`replied`/`reopen` are refused because bulk-stopping every
sequence is a different feature. Capped at 100 per click.

**THREE ways out that are not an interview** (2026-08-10, third added 2026-08-12).
`✕ Mark rejected` is an OUTCOME — somebody read it and said no. `⊘ Job removed / cancelled` is
the posting ceasing to exist: a req pulled, a freeze, a role filled internally.
**`👻 Ghost job`** is a listing that was never a real opening — an evergreen requisition, a role
reposted every few weeks, a board kept stocked to look like growth.

Ghost is its own state rather than a flavour of cancelled, and that difference is the only thing
it can teach: **cancelled means something real STOPPED, ghost means it never started.** One is
luck, the other is a SOURCE worth avoiding. Neither counts as a rejection. Same terminal
behaviour for all three (row sinks, sequences leave the 🔔 counter and the bulk list, no
temperature reading, one shared `↩ Restore`) and a separate `apply_status`, badge and filter pill
each.

They share `rejected_at`, which means **when this left the pipeline** — the ORDER BY that sinks
closed rows and the temperature guard that refuses to rate them both read it and neither cares
why. `apply_status` carries the reason.

**The risk was never the state, it is the hand-written lists that fall behind it.** Both misses
so far were exactly that, and the second was live for the whole life of `cancelled`:

- the SQL ORDER BY named only `'rejected'`, so a cancelled job sorted above jobs still being
  prepared. Found by sweeping for the string.
- **`_status_payload` named only `'rejected'` too**, so a cancelled job that had been applied to
  came over the wire as `applied`, and one that had not came over as `imported` — no ⊘ badge,
  absent from its own filter, and `isClosed()` false, which left it in the 🔔 counter still
  being offered follow-ups. One such row was live ("Zello uploaded job") until 2026-08-12.
  `test_cancelled_job.py` has fifteen tests and caught none of it: they check the repo or grep
  the JS, and the one that EXECUTES the frontend feeds `{status:'cancelled'}` by hand — a value
  the server never emitted (§Lessons 93).

Both are generated from `CLOSED_STATUSES` now, both sides go through one `isClosed()` predicate,
and a test asserts the payload no longer names a status directly.

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

**...and so does a contact who has merely been EMAILED** (2026-08-13). The rule above was applied
to `replied` and never to `emailed`, because `hasConversation()` asks whether an INBOUND message
exists — so anyone written to who had not answered still got the compose box. Measured: **126 of
141 emailed contacts.** Reported after writing to somebody at SpaceX already contacted: opening
the card showed a subject and a body, which reads as *here is what to send* rather than *here is
what you sent*, and that is how the same person gets written to twice. The rule was always about
whether an email had GONE OUT (§Lessons 49, half a rule, for three weeks).
History renders whenever any message is held, ABOVE the action. A due follow-up still takes the
action slot — it is the more urgent thing to write — but no longer replaces the record. A sent
draft is not repeated below as a disabled compose box; its ACTIONS survive, because the
follow-up button and the sent tag are the only parts of a sent draft still worth having.

**ONE PERSON, TWO CARDS: the second borrows the conversation** (2026-08-13). `contact_id` hashes
`(job_url, linkedin_url, name)`, so the same human found for a second role is a second row with
an empty history — and `messages` is keyed on `contact_id`. Opening that card offered a fresh
cold email to somebody nine messages into a conversation with three replies.
`messages.threads_by_shared_address()` is ONE query for the whole page (hoisted above the job
loop, restricted to addresses that genuinely appear twice), and `_thread_for` hands a card its
own messages or, failing that, the same person's from elsewhere — marked with the job it belongs
to. Live: **5 cards**, and **zero** addresses with history on BOTH rows, which is what makes
this a display fix rather than a merge.
Rendered read-only under a banner naming the other application, and with **no composer** — not
tidiness: `send_reply` resolves recipients from the row's OWN messages, of which there are none,
so a reply box would render, look entirely normal and refuse on click (§Lessons 88 aimed at the
least reversible action in the app). **CO-2 is the real repair** — see §Where the work goes next.

**ONE SEPARATOR PER GMAIL THREAD** (2026-08-12). `thread_for_contact` returns every message
stored for a person, merged and sorted by date — the name says thread and it returns all of them.
One live contact had **18 messages across 7 threads** (a calendar invite, an introduction, two
deals) rendered as a single continuous stream with four people interleaved, so it read as one
exchange that kept changing subject. Reported as *"this just looks like one long conversation
which is not"*.

Grouped by `thread_id`, ordered by each thread's **LAST** message so the live conversation sits at
the bottom beside the composer, and **only that one opens by default** — seven expanded threads is
the wall this replaced. The subject shown is the SHORTEST in the thread (`Re: Re: Ormus <> AMSYS`
is the same conversation as `Ormus <> AMSYS`); messages with no `thread_id` group by normalised
subject rather than bucketing under `""`, which would rebuild the merge. `CONV_OPEN` and
`CONV_SHUT` are two sets, not one, because the DEFAULT differs by thread.

**CLOSED 2026-08-13, and the misaddressing was the SMALLER half.** `reply_target` now takes a
`thread` key and scopes everything to that one conversation. What the merge was really costing
was the `References` header, on **all 7** contacts with more than one thread — one at **87
message ids across 25 unrelated conversations**, which tells the recipient's mail client that a
calendar invite and two deals are one thread. Live after: 87 → 20, 61 → 1, 18 → 1.

The browser names the thread it is showing (`data-thread`, posted by `sendReply`); the server
still derives every address from the stored rows of that thread, so this NARROWS rather than
widens — a `to` has never been accepted from the page and still is not. A key that resolves to
nothing **refuses**, because falling back to the merged list is the bug itself.

`thread_key`/`group_threads` exist in `domain/conversations.py` AND in `dashboard.js`, because
the browser picks and the server resolves. `tests/test_thread_grouping_agrees.py` runs both over
one fixture — **they already disagreed**: the JS stripped one `Re:` while `_strip_re` strips
repeated `Re:`/`Fwd:`, so a forwarded message split off in the browser and joined the main thread
on the server, with nothing rendering wrong (§Lessons 49, deciding recipients).
Two smaller ones went with it: a named thread no longer falls back to `contacts.thread_id`, and
the SMTP path files a sent reply under the thread it answered rather than re-merging it.

**A COMPOSER ON EVERY THREAD, not only the newest** (2026-08-13). Reported as *"I'm only able
to answer the latest thread"*: seven conversations, one reply box pinned to the last of them, six
that could be read and not answered. The composer now lives INSIDE each thread, beside the
messages it answers (§Lessons 89), and only on threads somebody has actually written on — live,
**72 of 238 threads are replyable** and the rest correctly offer nothing, because replying to a
conversation only we have spoken in is a FOLLOW-UP with its own ladder.
Composers only render in an EXPANDED thread, and one thread opens by default — so answerable
threads carry a **↩** on the collapsed header, without which the card would still read as "six
closed rows and no way to reply" and be reported unchanged.
The thread that opens by default is the newest ANSWERABLE one, not the newest by date: the last
thing on a card is often our own unanswered email or a calendar acceptance, which put the box
the operator was sent to behind a click under a banner reading "your turn".
All composer state is keyed by `(contact, thread)` through one `rkey()` — contact-only keys made
seven boxes share a draft, a vibe directive and a status line.
**`_draft_reply` and `set_reply_text` had the SAME merged-thread assumption** and were fixed with
it: a draft written under one subject answered the newest message across every thread, and a
paste landed on a message in another conversation, where it then fed every later drafter
(§Lessons 49, third and fourth call sites of one rule).

**A long thread collapses in the middle and the gap is a BUTTON** (2026-08-11, §Lessons 90). Over
six messages it renders first + last two — the panel is rewritten every 2.5s and an unbounded list
pushes the composer off screen — but the gap was PLAIN TEXT for months, so five stored messages of
a live Google conversation were countable and unreadable. `CONV_EXPANDED` lives outside the DOM
like `PANEL_OPEN`. A message sitting on `SNIPPET_MAX` carries `…truncated` pointing at
⤓ Fetch from Gmail, because a sentence that stops mid-word is indistinguishable from one that was
lost; the cap is SERVED from `messages.py`, never written twice.

**💡 flags a contact** (2026-08-11). Every other signal on a contact row is derived — replied,
due, exhausted, opened the deck. This is the only one the operator DECIDES. On the collapsed row
(a marker you must expand to see is one nobody sees), a real `<button>`, and the whole row is
accented rather than just the icon, because "the people I care about here" has to survive scanning
fifteen of them. The browser decides the state and the server stores what it is told — a
server-side toggle races the 2.5s refresh.

**A Gmail link sits beside LinkedIn and Apollo** on the contact meta row. It reads
`contacts.thread_id` (143 contacts) as well as `reply_to.thread_id` (11, and the only source it
used to have), falling back to a Gmail SEARCH by address — which is the only thing that finds a
thread they started, a reply from another address, or one we were merely Cc'd on.

**The row carries three derived things** (UX-3/5/6, 2026-08-04), all computed from data the
payload already loads — no new query, budget unmoved:

- **Temperature** — `warm · active · cooling · cold`, plus `won`, `undeliverable` and `new`,
  which are not temperatures. **A dot AND a word**, never colour alone, and every band carries
  the sentence that produced it. `undeliverable` and `new` were not in the ticket and both came
  from running it: a bounce is not cold (opposite fixes), and a job imported this morning is not
  failing. **Rebuilt around RUNWAY on 2026-08-04 (§Lessons 54), and runway was rebuilt again on
  2026-08-06 (§Lessons 67)** — it was reading the checklist, whose follow-up step counts work
  OWED rather than work SCHEDULED, so a ladder that had not started was indistinguishable from
  one that had finished. Runway is now `_plan_progress()`: the cold email plus its whole ladder
  (`followups.total_touches`), per contact with an address, minus what has actually gone. The
  band reads the PROPORTION left (`_MOSTLY_SPENT`, two thirds) rather than whether any is left,
  because "anything remaining" puts a job emailed this morning and one trailing touch after a
  fortnight of silence back in the same band. Readings: cooling 10 · warm 4 · won 1 · cold 1 →
  active 7 · cooling 4 → **won 1 · warm 7 · active 13 · cooling 3 · new 4**.
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
`interactions`, and an inbound one reaches the 🔔 counter
**without writing `replied_at`** — that field means a DETECTED email reply and is what
`metrics.by_variant` divides by.

- **Status strip** (always visible, never a toggle) — a left-to-right path
  `✓ Found → ✓ Applied → ✓ Emailed 4/4 → ↻ Follow up 0/4 → · Reply`, first unfinished step
  amber, plus **one** `Next` action (`nextAction()`) and a visible `🔄 Re-apply`.
  **Overdue follow-ups highlight out of turn** (2026-08-06). The strip greys everything after
  the first unfinished step, so `! Emailed 9/10` claimed the amber and `· Follow up 4/9` — the
  only overdue thing on the row — rendered faint while the Next button said "10 follow-ups due"
  an inch away (§Lessons 56). Follow-up is the one step whose `total` is `done + currently due`,
  so a gap there is LATE work, not later work. Same amber as `now`, not a louder colour: nine of
  twenty-eight rows have follow-ups outstanding, and an alarm that is always on is trained away
  within a week. The ↻ mark separates late from in-flight.
  **An interview owes no follow-ups** (2026-08-06). Marking one already halts every ladder, but
  the checklist measures from `submitted_at` and knew nothing about `interview_at` — so WRITER
  read `won`, had an empty Next slot, was excluded from the 🔔 counter, and the strip still said
  `↻ Follow up 1/2`. The flag is computed BEFORE the loop that stamps `followup_due` on each
  contact, or the row goes quiet while the person inside it still asks to be chased. It reads
  `Follow up 1/1` now — quiet, not blank, so the send that really happened survives.
- **One tabbed panel**: People · Follow-ups · Materials · Activity. `PANEL_OPEN` / `TAB_OPEN`
  survive the 2.5s refresh.
- **Contacts collapse to one line** with channel pills (`✉ sent · 🔗 connected · ↻ due`).
  Opening one shows channels as tabs: **✉ Email · 🔗 LinkedIn · 💬 Text · 📝 Meetings**.
  **All are always offered, and an empty one is where its identifier gets ENTERED**
  (2026-08-12) — the rule Text had always followed alone, which made the others §Lessons 49. An empty tab is marked `＋`
  and dashed, so which identifiers are missing is legible from the strip without opening all
  three; opening one renders the INPUT, never a sentence about the absence (§Lessons 41). With no
  number the SMS composer still renders **disabled**, unchanged.
  Email and LinkedIn used to hide when empty, to kill a pane reading *"No LinkedIn profile."*
  That treated the sentence as the cost when the sentence WAS the cost: it removed the dead end
  and the only place the missing profile could ever be supplied. 85 of the first 105 imported
  people had no address and none had a LinkedIn URL, and nothing in the app could add one.
  `/api/contact/details` takes `email` and `linkedin_url` beside `phone`/`notes`/`noticed`.
  A field the caller did not SEND is left alone and one sent empty is a clear, so the pane can
  render one box without wiping the other two (§Lessons 75). A typed address is `unverified` —
  never `verified`, which is a claim about the ADDRESS — and the status only moves when the
  address actually CHANGED, or saving the notes box beside it would silently un-verify one
  Apollo had confirmed. A malformed address is **refused with the reason**, not dropped.
  `domain/contactfield.py` does the cleaning and **the sheet parser imports the same functions**:
  two paths writing one field is how one enforces a rule and the other quietly does not. A bare
  LinkedIn handle becomes a URL (it is what you have in your hand after copying the address bar);
  anything with a dot, a slash or a space is left exactly as typed, because a sheet legitimately
  carries a company page or a search link and rewriting one invents a profile.
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

**Edit any field in place** (EDIT-1, 2026-08-12). Double-click a job title, a company, or the
description CELL in the table; Location and Salary are on the Job tab, which is where they get
edited at all — they render only as tag CHIPS, and both were empty on all 81 rows, so two of the
five tag types had never appeared once. **Double-click a Space tab to rename it** — `repo.rename`
had existed since SPACE-2 reachable from no endpoint and no button (§Lessons 31).

**Tags are not edited directly**, and that is deliberate: they are DERIVED, and a tag that can
drift from its job is the invariant this whole table rests on. Editing the FIELDS keeps it true.
Free-form stored labels remain a separate, later feature.

`EDITABLE_FIELDS` is a whitelist and every exclusion is chosen for how SILENTLY it would fail —
`url` is the anchor `contact_id` hashes, `fit_score` is the model's judgement that the score
filter reads as a signal, `applied_at`/`apply_status` are the state machine the row menu drives
and logs. An unknown key **raises**: a silently dropped field is an edit the operator watched
succeed and which never happened. `None` means "this caller did not show that field" and `""`
means they cleared it (§Lessons 75).

**The 2.5s refresh replaces `#jobs` wholesale**, so it is guarded three ways, and the third is
what makes the first two safe to have:

- it **skips while any input in that subtree has focus**, or it eats what you are typing;
- it **skips while a SELECTION is live** — a range dragged across a description is not an input,
  so the focus check never saw it, and the rebuild collapsed the selection every 2.5s. That is
  what "I can't copy the text" was;
- it **does not write at all when the generated HTML is unchanged.** Measured: two `/api/status`
  bodies three seconds apart were IDENTICAL across **1.65 MB** except two `due_in_h` countdowns
  that change hourly, so the steady state was rebuilding a byte-identical tree ~1,440 times an
  hour — resetting every textarea's `scrollTop` to 0 each time. That is what "it keeps taking me
  back up when I scroll" was.

A string compare of what is about to be written is the whole guard: no diffing library, no keyed
nodes, and it cannot go stale because it IS the output.

**A deliberate render is not a refresh**, and conflating them broke the editor on arrival:
`startEdit` sets `EDITING`, calls `rerenderJobs()`, and `isEditingJobs()` returns true BECAUSE
`EDITING` is set — so the table bailed and the `<input>` was never written. `rerenderJobs(force)`
threads past the guard, and the `force` has to reach the check INSIDE `renderJobsTable`, not just
the argument passed to it (§Lessons 94).

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

**Two shapes, FOUR templates.** `pipeline/jobs` (rows are postings) and `pipeline/targets` (rows
are companies). Three of the four templates share the targets shape — `outreach`, `business` and
now `sheet` — which is the central claim,
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

**The manifest reaches everything, and the guarantee is a golden file.** A Space gets its OWN
system prompt — not another one with caveats appended (§Lessons 40). There are THREE now, chosen
by `voice` rather than by shape since SHEET-1: `_SYSTEM`, `_PITCH_SYSTEM` and `_PREMISE_SYSTEM`
(§The sheet Space). Follow-ups too (`_PITCH_FOLLOWUP_SYSTEM`), which the first pass forgot, so touch 1
read correctly and touch 2 claimed an application that does not exist.
`tests/golden/jobs_outreach_prompt.txt` pins the jobs prompt byte-for-byte. **Its first version
was vacuous**: it compared `space=None` against a default manifest and passed under a mutation
that leaked a field into BOTH paths. Two things moving together is not a regression test.

**Creating a Space is a `＋` on the nav strip.** THREE templates offered (`jobs`, `outreach`,
`sheet`), not four — `business` differs from `outreach` by `identity_id` alone, that field
FREEZES after the first send, and ID-1 has not shipped, so a business Space made today would
send from the personal mailbox forever. The refusal says so. The nav shows from ONE Space up, because the `＋` lives in it and
hiding the strip below two hid the only way to make a second.

**A Space is only as separate as its WRITE PATHS**, and two of them were not (2026-08-06/07).
Both shipped with SPACE-1..4 and both went unnoticed for days, because a Space that is filtered
correctly on READ looks completely finished:

- **`/api/import` carried no Space at all** — not from the browser, not through the handler, not
  into `insert_imported`. The column DEFAULT (`job-search`) decided, so a Peak6 posting pasted
  while standing in Gauntlet appeared under another tab and **Gauntlet held zero jobs since the
  day it was created**. `_add_targets` had carried its Space since SPACE-3; this is its twin.
  Resolved through `_resolve_space`, not the posted id — filing a row under an id that does not
  exist puts it in a Space with no tab, the one place nothing can reach it again.
- **The accounts banner counted every Space's employers.** See §Sign-in walls: the fix is to
  scope the PANEL, never the registry.

The lesson generalises past these two: **anything that WRITES a `jobs` or `contacts` row from
the dashboard needs the Space threaded to it**, and the read-side filter will hide the mistake.
`insert_imported` and `add_target` are the two that exist; a third would need the same.

**Not built:** ID-1/ID-2 (per-identity mailbox, deck, limits — `identities` exists and is read
by nothing), SPACE-0 (archive terminal rows), SPACE-6 (the business Space as a falsifier).
Copy debt: the bucket filters still say "In progress / Applied / Rejected" and the search
placeholder names salary, both wrong words in a targets Space. The four discovery scrapers also
leave `space_id` to the default — defensible, they run from a CLI with no Space on screen, and
discovery has produced zero rows to date.

---

## The sheet Space — a lead list as cards (SHEET-1/1b/2, 2026-08-11/12)

`docs/tickets/SHEET-1-the-spreadsheet-space.md`. Asked for as *"creating cards without
necessarily the cards having a job to apply to — the main unit for this template is the company,
the employees, and the outreach"*.

**Most of it already existed and had never run.** `pipeline/targets` was already a
company-as-the-row card with no posting, and `partnerships` held **zero** of them. So this
doubled as SPACE-6, the falsifier the PRD asked for — and it needed **no schema change**.

**A row is a PERSON, a card is a COMPANY.** One paste, one card per distinct company slug, the
people hanging off it:

    Company               First Name  Last Name  Position          Email
    Ridgeline Logistics   Dana        Okafor     VP Engineering    dana@…
    Ridgeline Logistics   Sam         Iyer       Staff Engineer
    Northwind Analytics   Alex        Roy        Head of Talent    alex@…
        ->  2 cards, 3 contacts

Live: **45 cards, 105 people** from one paste. Only `Company` and a name are required.

### `Space.voice` — what a ROW is and what the EMAIL is are two decisions

They used to be one. `draft_email` branched on `shape`, so a company-shaped Space was **forced**
into `_PITCH_SYSTEM` and "a card per company, in the job search's own words" could not be
expressed at all. `voice` splits them:

| voice | prompt | what the email IS |
|---|---|---|
| `jobseeker` | `_SYSTEM` | writing to someone at a company you applied to |
| `pitch` | `_PITCH_SYSTEM` | proposing a specific piece of work |
| **`premise`** | **`_PREMISE_SYSTEM`** | **the campaign's own paragraph IS the message** |

`voice_or_default()` returns exactly what the shape branch used to hardcode, so **no existing
Space moved** — `tests/golden/jobs_outreach_prompt.txt` staying byte-identical is the proof
rather than the claim. An unknown voice RAISES at construction: it would fall through to the
shape default and send the wrong KIND of email silently.

The third prompt was necessary rather than a flag, and `_premise_block`'s own text is why. It
says *"It is background, not the subject. A message that is only the premise is about the
sender"* and *"say it in your own words, or leave it out"* — correct for a jobs Space, where the
POSTING is the subject, and the exact opposite of what is needed with no posting at all.
Appending "…but here it IS the subject" is §Lessons 40: the heading wins. `_premise_led_block`
replaces it and the original is untouched for the two shapes that still want it as background.

The `sheet` template is `shape=targets · voice=premise · terminal=interview · tailor_docs=False`
— a job search organised by company, so success is still an interview and there is no posting to
tailor against.

### The import says what the sheet does not carry (SHEET-3, 2026-08-12)

**Rows imported and rows you can act on are different numbers, and only the first was shown.**
The first real sheet imported 45 companies and 105 people with **zero rejected rows** — and 85 of
those people had no email address, **none** had a LinkedIn URL, 55 were a first name alone, and
**30 of the 45 companies had nobody reachable at all**. The message read *"Imported 45 companies,
105 people."* That is §Lessons 15 in a new place: a result that cannot be acted on, rendering
exactly like one that can. It went unnoticed for a fortnight, and the operator found it by
noticing that no contact anywhere had a LinkedIn URL.

`sheet.coverage()` is pure, runs over the PARSE, and reports per field. Three decisions:

- **A MISSING COLUMN and an EMPTY one are separate findings.** Different fixes — add a heading,
  or fill cells in — and a single percentage cannot say which you are looking at. `First Name`
  with no `Last Name` column is the operator's real sheet and is a header fix; both columns with
  55 blanks is a data fix.
- **Every field says what the gap COSTS.** "linkedin: 0 of 105" is a statistic; *"no LinkedIn
  invite, and no profile to read before writing"* is what decides whether to go back to the sheet.
- **A name is not a name.** Every person that survives `parse` HAS one — a nameless row is a
  rejection — so counting non-empty names reports **100%** on exactly the sheet this exists to
  catch. `full_name` requires both halves.

Company fields (`About`, `Website`) are counted over COMPANIES, not rows: one blurb filled on one
of a company's rows is a sheet filled in correctly, and reporting it out of 105 sends the operator
to repeat a paragraph down a column — the thing first-non-empty-wins removed.

**Nothing needs redoing, and the panel says so.** Re-importing a grown sheet updates in place
(SHEET-1b), so the fix is always *add the columns, fill them, paste it all again* — a list of four
gaps without that sentence reads as a demand to start over.

**`/api/sheet-columns` serves the recognised headers from `_FIELDS`**, fetched on first open
rather than shipped in `/api/status` (which re-sends every 2.5s with six statements of headroom).
A hand-written list of what the importer reads is a second source of truth, and nobody can tell
which of the two is lying — nothing else says that `What they do`, `Summary` and `Overview` all
land in `About`.

### Contacts you SUPPLY, not discover

Three decisions, cheap now and expensive later:

- **`source='import'`**, never `apollo`. CRM-2's `by_layer()` exists to compare a warm channel
  against a cold list, and filing a hand-built sheet as a cold find makes that unanswerable
  forever. Live: apollo 207, **import 105**.
- **Verification is SKIPPED, not run and passed.** `verify_contact` catches people who work
  somewhere ELSE, which cannot happen to a name the operator typed (§Lessons 19) — and running
  it would drop every imported person for having no Apollo record (§Lessons 14).
- **`email_status='unverified'`** with an address, `'none'` without. `verified` is a claim about
  the ADDRESS and a spreadsheet is not evidence.

### Re-importing a GROWN sheet is the normal way to use this

Which is why two bugs here were found by running it, not by reading it:

**A re-import without the LinkedIn column DUPLICATED everyone.** `contact_id` hashes
(job_url, linkedin_url, name), so the same person from a sheet missing that column hashes
differently and lands as a second contact with their own ladder. People are matched on the card
by **email, then name** — CO-1's keying problem surfacing somewhere new, fixed HERE rather than
in `contact_id`, because changing that hash would re-key all 244 stored contacts.

**Then a sparser sheet ERASED what an earlier one supplied.** `upsert_contact` skips `None` and
WRITES `""`, so `p.get("linkedin_url") or ""` blanked a stored profile URL the moment the
operator re-exported without that column. Optional fields pass `None`.

### Context per company: the `About` column

Recognised alongside `Description` / `What they do` / `Summary` / `Overview`. It lands on the
**CARD**, never the contact — it feeds "WHAT THIS COMPANY DOES" in every draft for everyone
there, and filed on a person it would reach one of them and hide from their colleagues. First
non-empty wins, so filling one row covers the company, and a re-import without the column does
not erase it.

**Automating it was considered and rejected.** Apollo's name search returns the WRONG company
for 4 of 5 on the live list — Steno → *Steno Diabetes Center Copenhagen*, Bissell → *BISSELL Pet
Foundation*, Nerdy → `nerdy.com` when the contacts are at `varsitytutors.com` (§Lessons 5) — and
only 15 of 45 cards carry an email domain to disambiguate with. A wrong summary is a claim about
someone's employer sent to someone who works there, which is §Lessons 68's cost. The operator has
the judgement; one cell beats an inference.

`SHEET-2` also fixed the gap it exposed: **`job_context` reached the jobs prompt and nowhere
else**, so the Space whose rows are companies had a context box on every card feeding nothing at
all (§Lessons 49, 72). The two are kept as separate headings — what the company IS (public,
automatable later) against what the OPERATOR knows (typed, not public).

### Attaching a posting later

`repo.attach_posting` fills `title` / `application_url` / `full_description` **in place**. The
anchor never moves, and that is the whole function: `contact_id` hashes `job_url`, and on a
target card the anchor IS the `job_url`, so swapping in a posting URL orphans every contact,
ladder and message on it with nothing raising.

---

## Outreach context — four tiers, one cascade (CTX-1…3, 2026-08-07/08)

`docs/outreach-context-prd.md`, tickets `docs/tickets/CTX-*.md`. **Not a new architecture — the
missing tier in one that already worked.** Context cascades from the sender to the person, and
the more specific layer always wins.

| Tier | Answers | Changes | Lives in |
|---|---|---|---|
| **Identity** | who is sending | ~never | `identities` — deck, mailbox, limits. **Read by nothing** (ID-1) |
| **Space** | what is this campaign about | per campaign | `tone` (voice) + `offer` (substance) |
| **Job** | what do I know about THIS company | per row | `job_context` + `job_ask` |
| **Contact** | what do I know about THIS person | per person | `contacts.noticed` |

**Three builders, not five inline constructions** — `_voice_block`, `_premise_block`,
`_known_block` in `outreach.py`. The voice goes LAST on every path, immediately before the
instruction to write; facts go early. `brief=True` for the short channels (text, LinkedIn,
reply) shortens the GUIDANCE and **never drops a field**.

**`Space.must_mention` is a REQUIREMENT, and that is why it is not the premise** (2026-08-10).
The `gauntlet` premise names GauntletAI twice and **zero of eight drafts mentioned it** —
including the two that had the premise, because `_premise_block` hands over FACTS and permits
"leave it out". A requirement says the opposite. Enforced by a RETRY naming what was missing,
never by appending: a deck LINK can be force-appended because it is one correct string, while a
required mention is a sentence, and a canned sentence lands identically in every inbox at one
company (§Lessons 42, 87). Rides the `config` blob, so it cost no schema change. Live: **0/8 →
4/4**, four different phrasings.

**`job_ask` REPLACES the CTA, it does not join it.** `sched_block` already sets one, and a
prompt carrying both writes an email that does both, badly (§Lessons 40). The scheduling link
survives — what the operator overrides is what to ask for, not whether a calendar exists. An
empty ask behaves exactly as before CTX-2. **`job_ask` is email-only**: the follow-up ladder
sets a per-touch intent and overriding that is a second contradiction, deliberately deferred.

**The repetition exposure is the whole risk, and it grows with the tier.** `noticed` is per
PERSON, so a parroted sentence reaches one reader. `job_context` is per ROW — one paragraph,
every contact at that company. `offer` is per SPACE — `job-search` holds 30 jobs and 131
emailed contacts, so a premise quoted verbatim is one paragraph landing in ~200 inboxes. Every
block says facts-never-phrasing, and `burned_block` sees what the company was already sent.
**Still unproven against a live model** — §Lessons 42 was invisible to inspection.

**Staleness is derived from `draft_variant`, not stored.** A draft tagged `ctx` was written with
context; one without predates it. No third column and no timestamp to drift. Known limit, stated
rather than discovered: editing context afterwards leaves the old tag, so it counts "used SOME
context", not "used THIS context". Sent drafts are counted separately and **never** offered for
regeneration — a sent draft is the only record of what went out.

`draft_variant` now carries `premise`, `ctx` and `ask`. `premise` is constant within a Space so
it only separates before/after; `ctx` and `ask` VARY across rows, which makes them the first
inputs comparable INSIDE one campaign.

**The premise box is shape-neutral** (`#premiseControls`). It lived inside `#targetControls`,
hidden on a jobs Space, so the field could be read by nothing AND typed by no one. Labels come
from `space.OFFER_COPY` via the payload so the panel cannot describe the field differently from
the manifest.

## Follow-up sequences

**TWO** independent ladders, all human-in-the-loop. Only email can auto-send.

| | Email | SMS / iMessage | ~~LinkedIn~~ |
|---|---|---|---|
| Anchor | `submitted_at` | `sms_sent_at` | — |
| Proof it started | `sent_message_id` | operator clicks `✓ I sent it` | — |
| Default | `48,96,168` (2d/4d/7d) | `72,168` (3d/7d) | **no ladder** |
| Send | `send_followup()`, threaded | copy → open Messages → paste | — |
| Stop | reply / stop / complete | same | — |

**LinkedIn has no follow-up ladder (2026-08-11).** The connection invitation is the whole
channel: send it and you are done. `Channel.follows_up=False`, and everything else about
LinkedIn is unchanged — it still drafts the note, still records `dm_sent_at`, still shows its
🔗 tab and pill, and the checklist's "LinkedIn invites sent" step still counts invites.

The schema had always agreed. `dm_status` holds only `sent` and `manual`, both meaning WE sent
it, and no `accepted` state exists anywhere (§Lessons 35) — so an invite is not a delivered
message, and a ladder anchored on `dm_sent_at` was scheduling nudges to people who may never
have seen the first one, then counting each as work the operator had failed to do. Measured at
the moment it was switched off:

    170  contacts carried dm_sent_at, so the ladder applied to all of them
     13  LinkedIn follow-ups had ever been sent, against 177 emails
     30  sat drafted and unsent — written, paid for, abandoned
     87  sequences had been STOPPED BY HAND, against 13 sends
     32  were due right now, against 11 for email

87 manual stops against 13 sends is the argument: the operator had been switching it off one
contact at a time for weeks. At 32 of 43 it was three quarters of the 🔔 counter, and a badge
that is mostly work you have decided not to do is one you stop reading — the failure CRM-3a
exists to prevent. Live effect: **43 due → 5**, all email.

`follows_up` is DATA on the registry, not a branch, and the filter ORDER in `channels_for` is
load-bearing: the Space narrows first (so a manifest typo still falls back to every channel),
then `follows_up` applies and is never undone by that fallback — reversed, a LinkedIn-only
Space would come back with email and SMS ladders it never asked for. Nothing was deleted: the
13 sent touches, 2 replies and 87 stopped sequences remain as history, and re-enabling is one
word.

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

**Opening the deck REPLACES the touch intent** (2026-08-10). `interactions.deck_opened_since_we_wrote`
— the only follow-up with something real to be about. It replaces `_TOUCH_INTENT` rather than
joining it (§Lessons 40: appending never resolves a contradiction), and the prompt is forbidden
from revealing how we know, by shape and by named phrasing (§Lessons 83). Anchored on *opened
since we last wrote*, so the operator previewing their own link before sending is not engagement,
and later touches do not re-ask about the same click. A reply outranks it; an open implies the
deck was received, so it is never re-pitched. **Email only** — the deck link is only ever emailed.

**Every job-search email NAMES the role** (2026-08-10, `domain/jobref.py`). It carried the
posting URL for about an hour; the drafts read badly and it came straight back out — 141
characters of Workday link inline in an opening sentence is a machine-assembled tell (§Lessons
84). The title is the hard half, not the link: **11 of 33 live rows carry one that must never be
quoted** (`Webai uploaded job`, `LegalZoom Careers`, `Program Manager, Customer &amp; …`), so an
unusable title resolves to `""` and the prompt is told to read the role out of the POSTING or
stay general — never to invent one.
**A requisition number goes only to a recruiter** (8 of 33 rows have one). It is how they find
the application in their own ATS; to a peer it is noise that reads as machine-generated.
`_wants_requisition` is the ONE predicate the prompt and the guarantee share, and
`ensure_requisition` splices it after the role's first mention — never appends, because a bare
`REQ-12289` under the sign-off is the footer this change removed. Asking alone got it into 2 of
4 live drafts; with the guarantee, 4 of 4.

**The deck is offered ONCE.** It used to go in every touch, and `ensure_intro_deck()`
force-appended the link when the model correctly left it out — a guarantee that guaranteed the
repetition. Both are conditional now. The already-sent check compares the BASE url: the earlier
emails went out as `/intro/` and `INTRO_DECK_PATHS` now builds `/intro/michael`, so matching the
full link found nothing and re-pitched the deck to a man who had already had it twice.

**Attachments are a toggle on every email card** (`📎 Docs ON · all emails`, 2026-08-10).
`OUTREACH_ATTACH_DOCS` is read at startup, which makes it a deployment setting rather than
something you flip between two sends. The override is a FILE in `APP_DIR` — the same reason
`apply/pause.py` is one — because a toggle that reverts on the 2.5s refresh or a restart sends
documents somebody had decided not to send, and the only place they would find out is their Sent
folder. The env var stays the DEFAULT, never the authority. The dashboard renders from
`gmail_send.attachments_enabled()` itself, so the badge cannot disagree with what goes out — the
intro-deck PDF rode along on 34 emails while `doctor --config` reported it off, because a default
lived in two places. It shipped first as a `<span>` beside the EMAIL label and was reported
broken within the hour (§Lessons 88). **Only the FIRST email attaches anything**; follow-ups
never did.

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

**Excluded locations** (`OUTREACH_EXCLUDE_LOCATIONS`, default `India`, 2026-08-11 — §Lessons 91).
A US job search reaches a company's US recruiting org; the same employer's Bangalore desk is the
wrong one for an Austin req. It is a **FILTER at discovery and never a delete** — nothing stored
is altered, including anyone already emailed. Two layers: Apollo's own `person_not_locations` (so
an excluded person is never enriched and costs no credit — verified live, zero overlap between
the include and exclude sets) plus our own check on the ENRICHED location, which is the only one
the hot layer sees. Skips are reported **separately from rejections**: "does not work there" and
"works there, wrong desk" are different findings, and merged they make a targeting choice read as
a data-quality failure. An empty result caused by the filter names the setting rather than
blaming an ambiguous employer.

**`contacts.location` is populated again** as of the same change. It had been empty on all 244
rows because the search-response mapper read `city or state or country` off a payload that
carries only `has_city`/`has_state`/`has_country`.

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

**THE OPERATOR HAS MORE THAN ONE ADDRESS, and six places assumed one** (2026-08-12).
`MY_ADDRESSES` (csv) feeds `_our_addresses()`, and `conversations.me_set()` is the single answer
to "is this us".

Found on live data: one contact had **61 messages synced, 50 filed `in` — and 30 of those were
the operator's own**, sent from the address on their résumé rather than the account the app
authenticates as. `direction` decides who owes whom a reply, whether a handoff banner fires,
whether a ladder halts on a "reply" and what the temperature band reads, so half a thread was
attributed to the wrong person everywhere at once. The visible symptom was **eleven stacked
`👋 X added Y to the thread` banners**, one offering the operator their OWN address as
"+ Add as contact" and one claiming they had introduced somebody to their own thread.

`reply_target` already took `str | list[str]` **with a comment explaining exactly why**. The
other six kept taking a bare string — §Lessons 49 where the rule was not merely written down but
IMPLEMENTED, once. A parametrised test now fails any function that takes `me: str`.

`pending_introductions` also refuses to trust the STORED direction: rows written before this
carry `in` on the operator's own mail, so a message from us is never an introduction whatever the
column says — which is what makes the banners stop without a backfill. The banners themselves
collapse at two with a *"N more people were added"* toggle.

**The 30 stored rows are REPAIRED** (2026-08-13, `doctor --directions` / `--fix-directions`).
All 599 messages now carry the right side; the contact whose composer offered to reply to the
operator's own address targets the real person again. A command rather than a typed-out UPDATE
because `MY_ADDRESSES` can GROW — a fourth alias recreates this on everything synced under the
old set.

**Its own first live run is the lesson, and it inverted.** The CLI does not load `~/.applypilot/
.env`, so `_our_addresses()` saw one address and the audit proposed flipping **42 of the
operator's own emails TO inbound** — the exact opposite of the repair, on a tool whose whole
job is to fix direction. Two guards now: `load_env(strict=False)` runs first, and **`out → in`
is refused outright and explained**. The asymmetry is real rather than defensive: `in → out`
means we recognised more of our own mail, which is monotone and safe; `out → in` means an
address this app has SENT AS is now being read as a stranger's, and a row is only ever written
`out` by our own send path or by `cv.timeline` under a then-correct config. So that direction is
almost always a shrunken config, not bad data — and calling our own mail a reply would halt
ladders and light the 🔔 counter for conversations nobody had.

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
**The panel ADVISES, it no longer refuses** (2026-08-06). The button was disabled whenever a
reply was waiting, a sequence was running, or a contact was untouched — all true statements
about what is CHEAPEST next, none of them a reason the operator cannot spend their own credits.
The row does not know what they know: a hiring manager named in the posting, a team that just
reorganised, or a first round that resolved to the wrong company entirely and whose "still
running" sequences are aimed at strangers (which is exactly what Texas Children's was). The
reason survives, the refusal does not; `ready` still drives the accent styling. The one real
constraint kept is `network_running` — a second search would double-spend credits and race the
write.

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
| Cover letter is addressed to the EMPLOYER, not the ATS | salutation read out of the letter — **error** |

**Padding is by COUNT, not by similarity.** A genuine rewrite doesn't resemble its source, so
prefix-matching classifies every rewrite as new and appends the originals too — 3 rewrites
become 6 bullets saying the same thing twice.

**The header states the TARGET role; employment history keeps its real titles.** Employment
titles are background-checkable. The summary must not open by restating a previous title — a
résumé aimed at "Applied AI Engineer" that begins "Technical Project Manager with 10+ years"
tells the reader they have the wrong document.

**The letter is addressed to the RESOLVED employer, and the guard reads the letter** (2026-08-10).
Six applications went out saying "Dear Uploaded Hiring Team" (to Google), "Dear Jobvite" (to
LegalZoom), "Dear Oraclecloud", "Dear Ouryahoo", "Dear Q2ebanking", "Dear Costargroup" — see
§Lessons 85. `generate_cover_letter` read `job['site']`, the DISCOVERY SOURCE; it calls
`derive.resolve_employer` now, and an unknown employer reaches the prompt as a refusal
("COMPANY: not known… do NOT name or invent an employer") rather than as a blank, because a
model handed an empty heading invents one.

`_salutation_name` reads who the letter is addressed to OUT OF THE LETTER and compares it to the
resolved employer, taking nothing from the caller — the old check was handed the same wrong
string that wrote the letter and passed every time. It is POSITIVE rather than a blocklist: four
of the six were tenant slugs no list would contain. `_same_employer` tolerates spelling
("Scale AI" == "Scaleai") by comparing alphanumerics as WHOLE strings, never a substring, so
"Arm" still cannot match "Armanino". `_infer_company` returns `""` instead of the literal
"Uploaded", and `company` (the employer) is no longer written with `site` (the source).

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

### The seventh vendor, and the end of the blocklist (2026-08-10)

`derive_company` is now the WHOLE chain — URL rules, then `refine_company_from_posting` (tenant
affixes: Ouryahoo → Yahoo), then `challenge_company_from_path` (wrong entity: Jobvite →
LegalZoom). It was the URL half only, with the corrections bolted onto ONE of four call sites;
the import path stored the uncorrected name and step 1 then trusted it forever (§Lessons 79).

**The challenge asks whether the posting names the employer we resolved.** A name the posting
mentions is never challenged; a challenger must be named 3× as a whole word, and only from the
two structural tenant slots — the host's first label and the FIRST path segment. No vendor list,
so an ATS nobody has heard of costs nothing.

**The host must BE the employer's** (`_host_is_the_employers`, 2026-08-10). The provenance rule
below is not enough on its own, and the way it failed is the lesson: `doctor --fix-employers`
backfilled the CORRECT name into `jobs.company`, so the resolver began answering from step 1 with
source `stored`, the challenge stopped running, and the domain guard keyed on `challenged` never
fired. Four `@jobvite.com` people were stored against a LegalZoom role hours after the fix
(§Lessons 86). The stable question needs no provenance and no list: `legalzoom` vs `jobvite.com`
is no; `costar` vs `costargroup.com` is yes (a corporate suffix); `arm` vs `armanino.com` is no,
because the remainder must be a KNOWN suffix rather than any prefix match.

**`resolve_employer()` returns the name AND its provenance**, and provenance is load-bearing
twice: `json_ld` is never corrected (it turned "Acme Corp" into "Acme"), and a `challenged` name
returns **no domain**, because the URL's host belongs to somebody else (§Lessons 80). A
`refined` name keeps its domain.

**`applypilot doctor --employers`** audits every row at once and separates `challenged` (a
different company — its contacts work elsewhere) from `refined` (same company, different
spelling). `--fix-employers` writes them back and never touches contacts. Live: 8 rows
corrected, and an audit of all 133 sent emails found **0** that reached a domain disagreeing
with the corrected employer.

### The fourth way, and the one that did not return zero (2026-08-06)

`eohh.fa.us2.oraclecloud.com` is **Texas Children's Hospital**. Oracle Recruiting Cloud pods
carry the employer NOWHERE — unlike a Workday tenant (`salesforce.wd12`) that at least wraps the
real name, an Oracle pod is an opaque code. `_host_label` took the last non-TLD label and
produced the employer **"Oraclecloud"**: the "Ats"/"Hr"/"Edu" shape a fourth time, and by far
the most expensive, because the same string also became the DOMAIN. Apollo has `oraclecloud.com`
filed under the **City of Atlanta** (whose own careers portal is Oracle-hosted), so asking "who
works at this domain" returned five Atlanta city employees. Four were emailed, one with the
subject line *"Exploring the IS Technology Business Partner role at Oraclecloud"*.

**Verification caught it twice and excused it twice.** The email contradiction (`@atlantaga.gov`)
was waived because the domain was only guessed; the org-name contradiction (`"City of Atlanta"`)
was waived because they came from a domain search — *the same guessed domain*. Each guard
excused itself using the other's weakness, and the system wrote the whole truth into a
`verify_note` nobody reads. **Not fixed by tightening either exemption**: that rejects the seven
real Avathon colleagues at `@sparkcognition.com` §Lessons 34 exists to protect. The discriminator
is that this "employer domain" is a five-label ATS host no human has an address at.

Three fixes, because each exposed the next: `oraclecloud` joins `_BOARD_HOSTS`;
`_OPAQUE_TENANT_HOSTS` stops `_host_label` falling back to `labels[0]` and returning **"Eohh"**
(the pod); and the `site` fallback checked `_BOARD_SITES` while step 1 checks `_BOARD_NAMES`, so
the live row's `site='Oraclecloud'` would have resolved through it regardless of the first two
(§Lessons 49, found only because the new eval case failed). Three eval cases, one per fix,
including the fix's own failure mode. Stanford, on the same Oracle product at its own domain,
still resolves.

### Adding a contact by hand (2026-08-06)

`/api/contact/add-introduced` now takes a LinkedIn URL, an email, or both. It was CRM-4a's
thread-handoff path and only reachable from `introBanner`, which fires on a **Cc detected in a
live Gmail thread** — so "you should talk to Priya, here's her LinkedIn", said on a call or in a
reply that names her without copying her, had nowhere to go. §Lessons 37 again: the tool has to
be reachable from the state it repairs. **＋ Add someone by hand** sits in the People tab,
including when it is empty, which is exactly when a referral for a job Apollo found nobody at
arrives.

One write path, two doors, and what they do NOT share:
`email_status='verified'` is a claim about the ADDRESS — true of a Cc off a real thread
(`on_thread`), false of one typed from memory, which is `unverified`. And `source='introduction'`
stays for a real handoff while a plain manual add is `'manual'`, or CRM-2's `by_layer()` can
never prove a warm handoff beats a cold list. `confidence: high` with no unconfirmed chip:
verification exists to catch people who work somewhere ELSE, which cannot happen to a name the
operator chose (§Lessons 19).

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
   unrecoverable. Guarded at both ends (refuse to start, and stop on handover).
   **The conclusion drawn from this was too strong, and it stood for a fortnight** (corrected
   2026-08-13). "Co-pilot is inherently one-at-a-time" was written down as a property of
   co-pilot; it was a property of the PATH — `run_dashboard_apply` spawned every apply as
   worker 0, so there was exactly one browser and a second launch always cleared port 9222. The
   real invariant is **one job per BROWSER**, and the multi-worker machinery to satisfy it
   (per-slot ports, per-slot profiles, atomic claims) already existed and was simply never used
   from the dashboard. The safety property is unchanged and now stated per slot; what went away
   is a throughput cap nobody had measured.
   **The generalisable half:** an incident gives you a true observation and a proposed cause,
   and they are not the same evidence. "Two jobs shared a port" was observed. "Co-pilot cannot
   be parallel" was inferred, written into this file as a fact, and repeated in a test name.
   Re-derive the constraint before treating a lesson as a ceiling.

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

67. **"Owed right now" and "left in the plan" are opposite, and one number was serving both.**
    §Lessons 54 rebuilt the temperature band around RUNWAY and then asked the CHECKLIST for it.
    The checklist's follow-up step is `done / (done + due)` and its own comment says "reads 100%
    until a follow-up actually comes due" — correct for a checklist, because you have not failed
    to do something that is not owed yet. Read as runway it inverts: the email ladder is
    48/96/168h, so nothing is due on the day you send, and a ladder that had **not started**
    reported the same zero as one that had **finished**. Expedia, applied 12:57, screenshotted at
    13:04: `cooling`, tooltip "Everything planned here is spent (5 emails)", with fifteen
    follow-ups queued. §Lessons 21's shape — a value one layer computes with a meaning the other
    cannot see.
    **What made it undeniable was measuring the claim rather than the band.** Counting planned
    vs sent messages per job: of the ten reading `cooling`, six were ≤38% through their plan, and
    Expedia at 25% read "spent" while Saronic at 32% — further along — read "active". The bands
    were inverted against the very thing they claimed to measure. Two more things fell out of
    that table: the jobs reading `active` were only doing so because their EMAILED step was still
    partial, so finishing the emailing flipped a job to cooling with the whole ladder ahead; and
    the fix's own trap is that counting scheduled touches makes "anything left" true nearly
    everywhere, which would put Webai back beside a job emailed this morning — §Lessons 54
    rebuilt from the other side. Hence a PROPORTION, not a boolean.
    Also fixed here: a terminal ladder is finished whatever the schedule lists (Devrev was
    promising a follow-up on a `replied` sequence that could never send), and the spent sentence
    says "every EMAIL" — the old wording contradicted the same row's Next button reading
    "1 LinkedIn invite left" (§Lessons 56).

68. **Two guards, each excused by the other's weakness, and neither can be tightened alone.**
    `eohh.fa.us2.oraclecloud.com` resolved to the employer "Oraclecloud", which then became the
    DOMAIN, and Apollo has `oraclecloud.com` filed under the City of Atlanta. Five Atlanta city
    employees were found for a Texas Children's Hospital job and four were emailed. Verification
    fired **both** signals — the address contradicted the domain, and Apollo's org name
    contradicted the employer — and waived both: the first because the domain was only guessed,
    the second because they came from a domain search, *which was that same guessed domain*.
    Each exemption was written for a real earlier bug and both are still right in isolation;
    tightening either one rejects the seven genuine Avathon colleagues §Lessons 34 protects. The
    discriminator was neither guard's business: a five-label ATS host is not an employer domain
    at all, so the fix belongs in `_BOARD_HOSTS`, one layer up from where the symptom appeared.
    **The system wrote the whole answer down** — *"Apollo lists them at 'City of Atlanta'"* — in
    a `verify_note` that renders nowhere anyone looks.
    Then the fix reproduced the bug twice more before landing: `_host_label` fell back to
    `labels[0]` and returned **"Eohh"** (the pod code — §Lessons 1's shape, one label to the
    left), and the `site` fallback checked `_BOARD_SITES` while step 1 checks `_BOARD_NAMES`, so
    the live row's `site='Oraclecloud'` would have resolved through it regardless (§Lessons 49).
    Only the eval case caught the third.

69. **An advisory disguised as a lock.** "Find a new round of contacts" was DISABLED whenever a
    reply was waiting, a sequence was running, or a contact was untouched. All three are true
    statements about what is cheapest to do next; none is a reason the operator cannot spend
    their own credits. The row does not know what they know — and the case that proved it was
    live on the board: Texas Children's had five "still running" sequences aimed at City of
    Atlanta employees (§Lessons 68), so the wrong first round was itself what blocked searching
    again. Reserve `disabled` for what is genuinely impossible (`network_running` would
    double-spend and race the write); everything else is a sentence, not a lock.

70. **A Space is only as separate as its WRITE paths, and the read filter hides the gap.**
    `/api/import` carried no Space — not from the browser, not through the handler, not into
    `insert_imported` — so the column DEFAULT filed every paste under `job-search` and
    **Gauntlet held zero jobs from the day it was created**. `_add_targets` had carried its
    Space since SPACE-3; nobody checked its twin. The accounts banner had the same shape one
    surface over, counting eight employers on a tab holding one job.
    The reason both survived four Space tickets: **a Space that filters correctly on READ looks
    finished.** Every panel was scoped, every query took `space_id`, and the rows were still
    landing in the wrong place. Ask what WRITES, not what reads.
    Its fix has a second half worth keeping separately: scope the PANEL, never the registry.
    `ats_accounts` is per ATS TENANT on purpose — partitioning it by Space would make you pay
    the same sign-in wall once per tab.

71. **Three tests in one session asserted something true for the wrong reason.**
    `meaning in html` passed against a blanked meaning, because `"" in html` is True for every
    string. `"<details" in html` passed against a legend rewritten as a `<div>`, because the
    metrics panel is also a `<details>` — §Lessons 1's shape in a file rather than a name. And a
    strip-class helper split on the label and took the last `<span class="`, which finds the
    inner `.mk` marker, so it asserted against `'mk'` and passed. All three were found by
    mutation and none by reading. **The common tell is an assertion that cannot fail when the
    thing under test is emptied** — check that first, before checking whether it is correct.

72. **A field wired into one of TWO SHAPES is not wired, and the guard that should have caught
    it asked the wrong question.** `Space.offer` was declared, documented for exactly the jobs
    case — *"in a job search the DESCRIPTION varies per row and the pitch is constant"* — and
    handed only to `_pitch_user_prompt`. The jobs branch never received it, so `job-search` and
    `gauntlet` wrote the same email as each other for days. `UNAPPLIED` was empty the whole
    time and `test_unapplied_fields_are_really_unapplied` was honest: it asks *"is this field
    read ANYWHERE?"* and the answer was yes, on one of two shapes. §Lessons 49 with a shape
    instead of a call site.
    The second half made the first worthless: `#offerInput` lived inside `#targetControls`,
    which is `hidden` on a jobs Space, **so the field could be read by nothing AND typed by no
    one** — and the hint beneath it argued the field was a targets concern. Wiring the prompt
    without moving the box ships a field nobody can fill (§Lessons 43, sixth occurrence).

73. **A signature audit proves ACCEPTANCE, not use — and I wrote one and briefly believed it.**
    CTX-3 opened by measuring which draft functions take `space`: four of six did, two did not.
    That table was true and the conclusion drawn from it was wrong. Of the four that accepted a
    manifest, **three never read `tone`** — `draft_followup` and `draft_linkedin_followup` take
    `space` and use it for `shape` alone. The campaign's standing voice reached **one of six**
    entry points, not four.
    §Lessons 39 already ends with the exact sentence — *"A parameter being accepted is not
    evidence it is used"* — and the guard written in this very ticket,
    `test_every_draft_entry_point_accepts_a_space`, is itself only an acceptance check. It is
    worth keeping (a new entry point fails it by default) but it is the parametrised
    behaviour tests that prove reading. **Cheap check, weak claim; do not let the cheap one
    stand in for the strong one.**
    It survived four Space tickets because it is invisible from the output: a text with no
    campaign voice is a *perfectly good text*. Nothing errors and nothing renders wrong.

74. **Refactor onto the frozen artifact FIRST, then extend.** CTX-3 needed the same three
    context blocks in five prompts. Building them as shared functions and rewriting
    `draft_email` to use them *before* touching anything else meant
    `tests/golden/jobs_outreach_prompt.txt` had to stay byte-identical — which it did, so the
    extraction was **proven** correct rather than believed, and every later change was built on
    a verified base. Doing it the other way round (add to four prompts, extract later) gives
    the golden file nothing to say, because by then the baseline has moved for a real reason
    and any drift hides inside it. A frozen artifact is only leverage if you spend it before
    you change behaviour.

75. **A two-directional guard tested in one direction.** `repo.set_context(url, context=None,
    ask=None)` must leave an unshown field alone — a missing key means "this caller did not
    render that box", never "the operator cleared it". The test exercised the ASK side only, so
    a mutation making the CONTEXT branch fire on `None` **survived**. Its twin in the same batch:
    the failed-save path in the browser was never exercised at all, so dropping the operator's
    typed paragraph on a failed save — losing the work AND re-rendering the stale server copy,
    which looks like it saved — went unnoticed. Both tests read as complete and neither had ever
    entered the branch. And writing the second one caught a bug in the test itself:
    `saveJobContext` reads the DOM and never populates the buffer, so the first version asserted
    against something nothing had filled (§Lessons 13, again).

76. **The ticket named a cause, the browser named a different one, and the gap was 30x.**
    SPACE-0 was written as "archive terminal rows" — applied, rejected and interviewing rows
    "render forever". Measured before building: terminal rows were **1 of 31**, so the
    prescribed fix removed 2.5% of the page; and the applied pile is not stale either (median
    5 days, none past 21), so archiving it would have hidden 28 jobs with live follow-up
    ladders running. §Lessons 28, third occurrence — a ticket is a hypothesis.
    What the page actually was, measured in a real browser rather than reasoned about:
    **8,939px, of which the table was 73%**, at 130px per row — and the `desc` cell WAS the row
    height, every other cell on the row holding 25 to 33 characters. All 30 excerpts sat exactly
    on their 900-char cap. The lever was `-webkit-line-clamp:6`, already there, already a knob.
    Six to two, plus collapsing the premise box CTX-1 had added above the table the same day:
    **8,939 → 7,423px**, rows 130 → 89, premise box 290 → 65.
    Two things worth keeping separately. The measurement had to be RENDERED — no amount of
    reading the payload says which cell owns the row height. And the fix I had shipped hours
    earlier was itself the second-largest block on the page; a feature's cost is not visible
    from inside the feature.


77. **A MIRROR is not a LOG, and the rows look identical.** `sent_today()` counted only first
    contacts — `gmail_send` gates all three send doors on it, so follow-ups and replies were
    invisible to the cap while consuming the real Gmail quota it exists to protect. Measured:
    102 first contacts against 107 follow-ups, so it saw under half of what it limited.
    The fix was two legs. The instructive part is the third leg I added and then removed.
    `send_reply` records nowhere but `messages`, so `messages` looks like the obvious source —
    and it is a **mirror of the mailbox**, not a log of what this app did: `_sync_thread` stores
    both directions of every thread it reads. Measured before believing it: of 253 rows with
    `direction='out'`, **133 were first contacts already counted** via `contacts.sent_message_id`.
    Adding that leg roughly DOUBLES the number instead of correcting it, and for a cap a phantom
    doubling blocks real sends — a worse bug than the undercount, in the opposite direction.
    The general tell: **a table populated by SYNC has different semantics from one populated by
    ACTION, even when its rows are the same shape.** Ask what WRITES a row before counting it —
    §Lessons 70's question, pointed at a read instead of a Space.
    It cannot currently be deduped: `contacts.sent_message_id` gives a join for first contacts,
    but `_TOUCH_COLUMNS` has no message id, so a synced follow-up cannot be matched to its
    touch. Giving `touches` one is what would close it.

78. **Designing a feature is the cheapest way to find bugs in its neighbourhood.** A 10-agent
    map + adversarial review of ID-1 — a feature that does not exist — surfaced **five defects
    that are live today and need no second identity**: the `sent_today()` undercount above;
    `can_autosend` enforced at one of four send doors and `offer_deck` read at one of three
    drafters (§Lessons 49, twice, in lines the feature would have edited anyway); the documented
    `identity_id` freeze not existing at all; `.gitignore` carrying no token pattern while the
    pre-commit hook's globs miss `tokens/business.json`, on a PUBLIC fork; and
    `_adopt_threads_by_address` collapsing two contacts who share an address, last-writer-wins.
    None was the thing being designed. They were found because making one value per-identity
    forces you to ask who reads it, who writes it, and what it is keyed on — questions nobody
    asks about code that already works.

79. **Seven vendors, one bug, and every fix was another blocklist entry.** Ats, Hr, Edu,
    Ouryahoo, Oraclecloud, Recruitics, and finally `jobs.jobvite.com/legalzoom/…` resolving to
    **"Jobvite"** with three `@jobvite.com` contacts stored at `confidence: high` against a
    LegalZoom role. A blocklist only ever protects against a vendor somebody has already been
    burned by, so the seventh cost exactly as much as the first.
    The general rule needs no list: **the employer of a role is named in the posting for that
    role.** "Jobvite" appears 0 times in its own posting; "LegalZoom" appears 7 and it opens
    *"About LegalZoom"*. Measured on all 32 rows before writing it — three flip, twenty-nine do
    not, zero false positives, including **nine rows whose resolved name appears zero times and
    is still correct**, which is why absence of corroboration can never itself be the trigger.
    The root cause was PLACEMENT, not logic. `derive_company` was the URL half only, while two
    correction steps lived at one call site each — and the one that mattered was the import,
    which stores the uncorrected name so step 1 then TRUSTS it. **A hostname guess laundered
    into a stored fact outranks the posting forever**, which is precisely why a blocklist was
    needed at all. §Lessons 49 with four call sites instead of two.
    Its own failure modes cost two more rounds: blocking the vendor name alone yielded **"Jsv3"**
    (the pod code, one label left — the Oracle fix's exact mistake), and allowing three path
    segments instead of one came a coincidence away from renaming PEAK6 to **"Technology"**.

80. **A correct name is worthless while the DOMAIN is still the vendor's.** LegalZoom resolved
    perfectly and `derive_domain` went on handing Apollo `jobvite.com` — §Lessons 68's whole
    mechanism, where Apollo returns the people who really do work there and verification
    confirms them because they genuinely do. Fixing the name is the visible half; the domain is
    the half that spends credits on strangers.
    The discriminator is the difference between the two corrections, and it is exact:
    **`challenged` means a DIFFERENT entity** (Jobvite → LegalZoom, so `jobvite.com` is somebody
    else's) **while `refined` means the same one spelled differently** (Expediagroup → Expedia,
    so `expediagroup.com` is still theirs — every live contact on that row is at it). No domain
    is the SAFE outcome, not a failure: Apollo falls back to a name search, and the name is the
    one thing the posting corroborated.

81. **The column the operator reads had never been the employer.** The dashboard's Company field
    was `row["site"]` — the discovery SOURCE — so a LegalZoom application displayed **"Jobvite"**
    and a Meta one **"Recruitics"**, every day, on the row. `contact_company` sat beside it
    holding the resolved employer the whole time and driving the connection counts, so the data
    was present and the label ignored it. Seven of thirty-two rows disagreed, and
    `dashboard_rows` did not select `company` at all.
    This is what "the problem has been ongoing" actually was. The resolver bugs were real and
    rare; the wrong label was on screen constantly. **When a user reports a long-running
    problem, find what they have been LOOKING at before fixing what you can measure.**

82. **`application_url` is the FORM, not the posting, whatever the name says.** Asked to put the
    job link in every email; almost all the work was choosing the string. Google's is the
    RELATIVE `./apply?jobId=…` (unsendable), Peak6's and Expedia's are Workday `/apply`
    endpoints, Stanford's carries two `?` and is malformed, and nearly every stored `url` is
    tagged with the aggregator we came through (`utm_source=linkedin`, `gh_src=`, `source=`).
    Emailing somebody who works there a link to their own application form, labelled with which
    board we found it on, is the wrong artifact twice over.
    The rule that fell out: **strip parameters and unwrap redirects, never rewrite paths.** A
    dropped param cannot change which page loads and an unwrapped redirect resolves to its own
    destination — both provable. Trimming a trailing `/application` to "recover the posting" is
    a guess about a URL space we do not own, and §Lessons 32 is what that costs. Result across
    32 rows: 1,052 characters of tracking removed, 0 rows left unsendable, and a 434-character
    Recruitics ad redirect unwrapped to the metacareers.com posting inside it.
    **The link itself did not survive contact with the drafts** — see §Lessons 84. The cleaning
    did, one layer down, because `derive` needs it to see past an ad redirect before any employer
    rule reads a hostname.

83. **The deck-open follow-up is the one message with something real to be about — and the one
    that can least afford to say so.** Every other touch chases silence; this one answers an
    act. But the signal comes from a beacon on our own site, so revealing it tells a stranger
    their reading was watched, which converts the best signal in the sequence into the reason
    they stop replying. Same ban as `noticed` ("NEVER ANNOUNCE THE NOTICING") with higher
    stakes, because a LinkedIn post is public and someone's browsing is not. The test that
    makes it checkable: **if a sentence would not make sense to someone who had NOT opened it,
    do not write it.** The live draft passed by asking a substantive question and offering to
    "walk through any of it if something stood out" — conditional, and true either way.
    Anchored on *opened since we last wrote*, never *has ever opened*, and the live data is the
    argument: of two recorded opens, one is stamped **ninety seconds BEFORE** the email carrying
    the link — the operator previewing their own `/intro/<name>` (§Lessons 64). "Has ever
    opened" writes that person a follow-up about a deck they were never sent.
84. **Built the wrong half, and the artifact said so within the hour.** "Include the job" was
    implemented as the posting URL, shipped, and reverted on sight of real drafts: 141 characters
    of Workday link inline in an opening sentence is a machine-assembled tell, the same family as
    the em dash. What a human writes is the role's NAME.
    The reversal is not the lesson. **The lesson is which half was hard, and I had it backwards
    both times.** Measured on 33 rows: a requisition is recoverable on 8 and appears in the
    posting text on none, while the TITLE — the part assumed to be free, sitting right there in
    a column — is unusable on **11**: five scraper placeholders (`Webai uploaded job`), a careers
    index heading (`LegalZoom Careers`), an undecoded HTML entity (`Customer &amp; Community`),
    the employer prefixed onto its own role, and a board's sentence *about* a posting (`Google
    hiring AI Sales Specialist…`). Quoting any of them is worse than naming no role: §Lessons 42
    already caught "the Betterup uploaded job" reaching a live draft.
    So `""` is a real answer the caller must handle, and the prompt is told to recover the role
    from the POSTING TEXT if it can — which the model then did, correctly, on the row whose
    stored title is `LegalZoom Careers`. **Reading a name from the description is not inventing
    it**, and the first wording forbade both.
    Two more things only generation could show. The requisition reached **2 of 4** drafts on a
    prompt instruction alone, so it needed `ensure_requisition` (§Lessons 9, 12) — which
    SPLICES after the role's first mention and never appends, because a bare `REQ-12289` under
    the sign-off is the footer the whole change existed to remove. And `\b` does not match
    between `_` and `REQ`, so two live Workday rows had no requisition at all until the boundary
    became a character class.


85. **Six applications went out addressed to the ATS, and a passing test held the placeholder in
    place.** "Dear Uploaded Hiring Team" to **Google**. Also "Dear Jobvite" to LegalZoom, "Dear
    Oraclecloud" to Texas Children's, "Dear Ouryahoo" to Yahoo, "Dear Q2ebanking" to Q2, "Dear
    Costargroup" to CoStar. All six already submitted by the time anyone read one.
    Three failures stacked, and only the third is unusual. The letter was generated from
    `job['site']` — the DISCOVERY SOURCE — so §Lessons 81's bug had a twin in the documents that
    the dashboard fix did not reach (§Lessons 49, with the most expensive call site left out).
    `_infer_company` invented the literal **"Uploaded"** when a LinkedIn job-view URL carried no
    employer, and wrote it into `company` AND `site`, so a guess became a fact in two columns.
    And `validate_cover_letter` ALREADY required the letter to name the employer, as a blocking
    error — it passed every time, because it was handed the same wrong string that wrote the
    letter and the letter did name it (§Lessons 12, in the guard built for exactly this).
    The part to keep: **`_infer_company("not-a-url") == "Uploaded"` was asserted by a green
    test.** The placeholder was not an oversight sitting in the code, it was PINNED as correct,
    so removing it broke a test and anyone who tried put it back. A test can hold a bug in place
    more firmly than the code does.
    The new guard reads the salutation OUT OF THE LETTER and takes nothing from the caller, and
    it is POSITIVE rather than a blocklist — four of the six were tenant slugs ("Ouryahoo",
    "Costargroup", "Q2ebanking") that no list will ever contain, and "Jobvite" was not on the
    board list either because it had been fixed by corroboration instead.

86. **A rule about HOW WE ARRIVED at an answer is not a rule about the answer, and storing the
    right answer disarmed it.** §Lessons 80's domain guard read `if source == "challenged":
    return None`. It worked. Then `doctor --fix-employers` backfilled `jobs.company` to
    "LegalZoom" — the correct name — so the resolver began answering from step 1 with source
    `stored`, the challenge never ran, and the guard never fired. A Find-contacts run stored
    four people at `@jobvite.com` and `@talemetry.com` against a LegalZoom role, hours after the
    bug was declared fixed.
    Both guards that should have stopped it were about PROVENANCE, and provenance is a fact
    about this run rather than about the row. The replacement asks the stable question instead:
    **is this hostname the employer's name?** `legalzoom` vs `jobvite.com` is no;
    `costar` vs `costargroup.com` is yes (a corporate suffix); `arm` vs `armanino.com` is no,
    because the remainder must be a known suffix and not merely a prefix match (§Lessons 1, in
    the comparison that decides whose payroll gets emailed).
    **Writing the correct answer into the database is a legitimate thing to do, so any guard it
    can switch off is the wrong guard.**

87. **A requirement is not a fact, and `_premise_block` says so out loud.** Asked to make every
    `gauntlet` email mention the GauntletAI programme. Its premise ALREADY named GauntletAI
    twice, and **zero of eight live drafts mentioned it** — including the two that had the
    premise. Nothing was broken: the premise block hands over facts and explicitly permits
    *"say it in your own words, or leave it out"*, so the model kept the decade at T-Mobile and
    Verizon and dropped the rest, exactly as instructed.
    `Space.must_mention` states the opposite thing, and the enforcement is a RETRY, never an
    append. `ensure_intro_deck` may append because a deck link is a URL — one correct string,
    and repeating it costs nothing. A required mention has to be a SENTENCE, and a canned
    sentence lands identically in every inbox at one company, which is §Lessons 42 with the
    volume turned up. Asking again gets a different sentence; appending gets the same one
    forever. Measured against the live model: **0/8 → 4/4**, four different phrasings.

88. **A `<span>` that looks like a button is worse than no control at all.** The attachment
    toggle shipped as a badge beside the EMAIL label — button-shaped, pill-styled, and inert.
    The first thing it received was a click and a bug report, which was the correct response.
    §Lessons 43 has now fired seven times as "a control nobody can find"; this is its inverse
    and it is worse. A missing control is merely absent, and the operator goes looking. A fake
    one is a promise the page does not keep, and it spends the one click you get. Render a
    `<button>`, put it in the row where the other actions already are, and if a global setting
    is displayed on a per-contact card then the LABEL has to say it is global ("Docs ON · all
    emails") — a per-contact-looking control with global effect is the shape that gets clicked
    by mistake.

89. **A control in the wrong room is reported as not existing, three times.** The bulk
    follow-up button shipped as `✉ Follow-ups (57)` in the top console. It rendered, the count
    updated live, the panel opened, the endpoint worked — and the answer to "can you add a bulk
    follow-up button" was *"I built that"*, twice, before I went and looked at what the operator
    was looking at: the **Follow-ups tab of one job**, where every contact already has their own
    `✍ Draft follow-up`. That is where a bulk version of the same act belongs, and no amount of
    it working two screens away was going to substitute.
    §Lessons 43 has been "a control nobody can find" seven times and §Lessons 88 added the
    inverse. This is the third form: **findable is not the same as findable FROM WHERE THE WORK
    IS.** The test for a control's placement is not "can it be reached" but "is it beside the
    thing it acts on" — and the operator saying *I still don't see it* is the measurement,
    however loudly the DOM disagrees.
    Two smaller things fell out of the same session. The screenshot I took to prove the button
    existed was scaled down from the real viewport, so my own click missed it by 165px and I
    briefly diagnosed a live bug that was not there (§Lessons 46's shape, in a tool rather than
    an artifact). And the fix has a real design consequence worth keeping: scoped to ONE job, the
    confirm can name every recipient — eight names you can read, rather than fifty-seven you
    cannot.

90. **"I'm not getting the entire interaction" — and every message was already there.** Reported
    on the Google thread. Measured before touching anything: **all 8 messages stored, with text,
    on the wire.** Three unrelated things made a complete conversation read as a truncated one,
    and the diagnosis had to separate them before any of them could be fixed.
    **The collapse had no way out.** Over six messages the panel rendered first + last two and
    printed `· 5 earlier messages ·` **as plain text** — so the middle of a live conversation,
    including the reply that asked a question, was stored and unreachable. The collapsing itself
    is right (the panel re-renders every 2.5s and an unbounded list pushes the composer off
    screen); the bug is that a count is not a control. §Lessons 43's family, and the sharpest
    version of it: the data was on screen as a NUMBER and could not be read.
    **Every apostrophe rendered `I&#39;m`, on 70 of 99 stored messages.** Gmail's API returns
    `snippet` HTML-ESCAPED and we stored it raw, so the dashboard's own `esc()` escaped the `&` a
    second time. Fixed at the WRITE, not at render — decoding on display fixes the screen and
    leaves the table holding markup that the prompts, the metrics and every later reader would
    have to know about. Decoded BEFORE the cap, too, or 200 means 200 bytes and an apostrophe
    costs five of them.
    **A message at the cap stopped mid-sentence and said nothing.** `SNIPPET_MAX` is a
    deliberate bound, not data loss — but the two look identical, and the way to get more
    (⤓ Fetch from Gmail, at `PASTED_MAX`) was a button whose purpose was invisible until pressed.
    The cap is SERVED from `messages.py` rather than written in the frontend: a bound in two
    places is two bounds, which is how the intro-deck PDF rode along on 34 real emails.
    Known artifact, stated rather than discovered: decoding shortened the 71 backfilled rows
    below 200, so they are truncated and will NOT carry the mark. Only new messages will.

91. **A filter is not a delete, and the field it needed was empty on every row.** Asked to
    "auto delete contacts we find where the people are based in India". Two things had to change
    before the request could even be evaluated.
    **It is a FILTER at discovery.** `delete_contact` also wipes `touches`, `sequences`,
    `messages` and `interactions` — so an automatic, irreversible delete driven by a fuzzy
    provider field would destroy a live ladder, or a replied-to thread, on one bad location
    string. Filtering before the write makes that unreachable and touches no stored contact.
    **`contacts.location` was empty on all 244 rows**, so the filter as described could not have
    matched anybody. Apollo's SEARCH response carries `has_city` / `has_state` / `has_country` —
    BOOLEANS about whether the data exists — and never the values, so `city or state or country`
    was `None` every time. The values come from ENRICHMENT, where the mapper read four fields and
    dropped the rest: the same bug, in the same function, that had left 162 of 185 contacts
    first-name-only. A plausible-looking mapping is the hardest kind to see.
    **The vendor can filter, and that is worth checking before writing one.** Verified live:
    `person_locations:[India]` and `person_not_locations:[India]` return sets with ZERO overlap,
    so an excluded person is never enriched and never costs a credit. Our own check still runs on
    the enriched location, because a filter enforced only by the vendor is not enforced and the
    hot layer has no search to filter at all.
    **The false positive was found by a test, not by review**: "Delhi Township, Ohio" excluded as
    India. Delhi, Madras and Hyderabad are all real places elsewhere, so those three need their
    country named — §Lessons 1 with geography instead of company names, and `India` is inside
    **Indiana**. A blank location is always KEPT: dropping on a missing field shrinks every
    search on data quality rather than on the rule (§Lessons 34).

92. **The eighth vendor was a spreadsheet, and it arrived through a box the operator was
    looking at.** A Google Sheets URL pasted into the JOBS import box became
    `title="Docs uploaded job", company="Docs"` — `docs.google.com`'s host label as the employer,
    after Ats · Hr · Edu · Ouryahoo · Oraclecloud · Recruitics · Jobvite. Then the same link in
    the TARGETS box became the card
    `spreadsheets/d/1HreblDeVn3vDFlmy4fROR9OThwd5tsld2PQkmtvG8qQ/edit?gid=…`, because
    `parse_line` stripped the host as a domain and kept the PATH as the name.
    Neither fix is a list. **A URL path is not a company name** — a slash means we are looking at
    a path, and falling back to the host's label is the exact mechanism that produced "Docs" one
    layer earlier. **A company name never contains a TAB** — a line with one is a spreadsheet row
    in the wrong box, and accepting it made **106 cards each named after a whole row**, the
    HEADER row included.
    The measurement that mattered came before any of it: 106 cards, **zero contacts**. The import
    had never run. "Find contacts is not working" was Apollo being asked for a company called
    `Tracy Stdic\tZapier\t…` and truthfully saying no such company exists.
    And the operator's data was still there — one row per junk card's `company` column.
    Reconstructed, parsed clean (45 companies, 105 people, 0 rejects), imported properly, junk
    deleted. **Check whether the bad rows still CONTAIN the input before asking anyone to redo
    the work.**

93. **A guard on the write, a hand-written list on the read, and the state never reached the
    screen.** Adding `ghost` meant sweeping for `cancelled`, and the sweep found
    `_status_payload` still saying `if apply_status == "rejected"`. So for the entire life of
    the `cancelled` state, a cancelled job that had been applied to came over the wire as
    **`applied`**, and one that had not came over as **`imported`** — no badge, absent from its
    own filter, and `isClosed()` false, so it stayed in the 🔔 counter and kept being offered
    follow-ups. One row was live in the database.
    `test_cancelled_job.py` has FIFTEEN tests and caught none of it. They check the repo layer or
    grep the JS, and the single test that executes the frontend feeds `{status:'cancelled'}` by
    hand — **a value the server never actually emitted**. That is the tell worth keeping: a
    frontend test that supplies its own input can only prove the frontend is consistent with
    itself. At least one assertion has to start from what the SERVER produces.
    §Lessons 47's shape with the halves swapped: there the write was fine and the SELECT dropped
    the column; here the write was fine and the derivation dropped the state.

94. **The guard that protects an open editor vetoed the render that opens it.** `startEdit` sets
    `EDITING`, calls `rerenderJobs()`, and `isEditingJobs()` returns true BECAUSE `EDITING` is
    set — so `renderJobsTable` bailed and the `<input>` was never written. Reported as "I click
    and nothing happens", and nothing did: the handler fired, the state changed, and the render
    that would have shown it refused to run.
    All 36 tests passed while the feature was completely broken, because every one of them called
    `editable()` or `commitEdit()` directly and **none drove the render** (§Lessons 48). The same
    gap let the employer-banding mutation survive a fortnight earlier: fourteen tests of the
    grouping functions, none of which rendered a table.
    Fixing it needed the `force` to reach the check INSIDE `renderJobsTable`, not just the
    argument passed to it — the first attempt set the argument, and the function re-checks the
    flag on its own line. **A guard duplicated for a good reason is still two guards.**

95. **A limit doing something its author never aimed it at.** Description edits failed silently
    on every sheet card: the endpoint refuses anything under 120 characters. That minimum exists
    so a STUB cannot reach the résumé tailor — on a Space that makes documents an empty
    description blocks the queue and three pasted sentences would clear the error, satisfy it and
    produce a résumé written against them. On a Space with `tailor_docs=False` the field is a
    company blurb, and *"Court reporting and legal transcription."* is 40 characters and exactly
    right. §Lessons 50's shape, where `0` meant "unlimited" in two settings and "send nothing" in
    a third.
    The guard now lives in ONE function both the paste path and the inline editor call. Two
    copies is how one path enforces a rule and the other quietly does not (§Lessons 49) — and
    here the inline editor was the one that would have skipped it.

96. **"Everything disappeared" was a column that had never been filled.** All 45 cards on the
    Lead Sheet showed an empty description and the operator reasonably read that as data loss.
    Checked against two backups BEFORE touching anything: the pre-recovery snapshot has 0 of 106
    rows with a description, and the source sheet has no About column at all. Nothing was lost.
    The defect was real anyway, one level up: **an empty cell rendering as nothing is
    indistinguishable from a cell whose contents vanished**, and it offers no target to
    double-click either. It says "No description — double-click to add one" now.
    The rule: when a report is "X disappeared", verify against a backup before believing it OR
    dismissing it. Both mistakes are available, and the second is the expensive one.

97. **The control was three clicks from the thing it edits, and that is the same as absent.**
    §Lessons 43/88/89's family, now at ten occurrences. The ✎ Edit description button shipped
    inside the Job tab — open the row, switch tabs, scroll — while the description the operator
    is looking at is the table CELL, sitting next to a job name that double-clicks fine. It was a
    plain `<div>`. Reported as "I still can't edit any descriptions across any space", which was
    true of everywhere they would think to try.
    The test for placement is not "can it be reached". It is **"is it on the thing it acts on"**,
    and the operator saying *I still can't* is the measurement however much markup exists two
    screens away.

98. **A clean import of an unusable sheet, and the success message was accurate.** 45 companies,
    105 people, **zero rejected rows** — and 85 of those people had no address, 0 had a LinkedIn
    URL, 55 were a first name alone, and 30 of the 45 cards had nobody reachable. Only two columns
    are required, so a sheet of bare names is a valid sheet; "Imported 45 companies, 105 people."
    was true and complete about the thing it measured, and measured the wrong thing. §Lessons 15
    at one remove: not a zero result rendering as silence, but a **partial** result rendering as a
    success, which is harder to see because there is something to look at.
    The operator found it a fortnight later, from the absence of LinkedIn URLs — and their
    diagnosis ("we need a good spreadsheet") was right and understated by a factor of four.
    Two things fell out of writing the check. **`full_name` is the field that cannot be counted
    the obvious way**: every person who survives `parse` has a name, because a nameless row is a
    rejection, so `bool(name)` reports 100% on precisely the sheet being tested for. And **my own
    test of the "only an absent column is tagged" rule could not fail** — it sliced
    `html[i-200:i+60]` around a label, `i-200` went negative on the FIRST row, Python read it as
    an offset from the end, and the window came back empty. It survived a mutation that tagged
    every row (§Lessons 71, and the second time this month that a window-slice assertion was the
    vacuous one). Split on the row boundary; do not slice a guess around a match.

99. **The fix for a dead end removed the dead end and the only way out of it.** An empty LinkedIn
    tab rendered *"No LinkedIn profile."*, so the tab was hidden — correct about the bug, wrong
    about the cost. The sentence WAS the cost (§Lessons 41), and hiding the tab took with it the
    one place a missing profile could ever be supplied. 85 of 105 imported people had no address
    and **none** had a LinkedIn URL; there was nowhere in the app to type one in, for weeks.
    The Text tab had been unconditional the whole time and its comment said exactly why — *"that
    tab is where a phone number gets entered, so hiding it without one hides the only way to add
    one"* — which makes this §Lessons 49 with a rule that was not merely written down but
    IMPLEMENTED at one of its three call sites, and commented at that one.
    **The test pinned the wrong half.** `test_a_channel_tab_with_nothing_behind_it_is_not_offered`
    asserted the hiding, so the fix broke a green test and anyone who tried would have put it
    back — §Lessons 85's `_infer_company("not-a-url") == "Uploaded"`, where a test held a bug in
    place more firmly than the code did. It is rewritten around the new decision rather than
    deleted, and it now asserts the pane contains an INPUT, not that the sentence is gone.

100. **A test that supplies the value it is checking cannot see the value that ships.**
    `prompt_block(rows, limit=2)` is what the test called, so raising the DEFAULT from 2 to 999
    left it green — and every draft would then have carried every meeting ever stored with that
    person. The parameter worked perfectly; the shipped behaviour was unguarded. Assert the
    default, and pass an explicit value only to prove the override ALSO works.
    Two more from the same build, both already written down and both walked into again:
    **the same paste stored twice**, because the transcript id was seeded with a `started_at`
    that defaulted to `now()` — so the default moved between two calls and idempotence was
    impossible (§Lessons 22, found by calling `save` twice and reading the ids). And
    **`/api/status` went 74 → 90 statements**: the readiness probe ran a `SELECT` on every call
    (§Lessons 11) and the attach was hooked inside the per-job loop instead of once for the
    payload. Six statements of headroom is thin enough that a feature can eat it in one commit.
    And a mutation that hit the WRONG function reported as a survivor: the line
    `if (!r.ok) { msg.textContent = ...; return; }` exists in two handlers, so `replace(old, new, 1)`
    mutated the first one. A mutation harness must assert its target is UNIQUE, not merely present.

101. **A `display` on a table CELL silently un-does `colspan`, and no markup test can see it.**
    `tr.co-solo > td { display:flex }` replaces the cell's `display:table-cell` and drops it out
    of the table formatting context, so `colspan="4"` stops applying and the cell collapses to the
    width of the first column. The new employer band rendered "Ey / 6 / people / · 5 / emailed",
    one word per line and ~130px tall, on every single-role row. Reported as *"it is breaking the
    cards"*.
    The rule looks correct in isolation and the MARKUP was correct — which is why 28 passing tests
    in that file saw nothing. §Lessons 62's family, where `hidden` lost to an author `display` and
    the Node test asserted the property, true and useless. The multi-role band had it right the
    whole time: its flex lives on `.co-toggle` INSIDE the td.
    The guard had to move to the CSS itself — **no selector whose last element is a `td` may set
    `display:flex/grid/block`** — and it is verified against the exact rule that shipped.
    **This was the second layout bug in one session that only a rendered page revealed** (the
    first: the description clamp). Run the browser after a CSS change; the suite cannot.
    Its fix then broke the probe: `co-solo` is a SUBSTRING of the new `co-solo-inner`, so every
    solo band counted as two — §Lessons 1, in a test helper rather than in a company name.

102. **The rule was written down AND implemented — once — and six other places kept the bug.**
    `reply_target` takes `me: str | list[str]`, with a comment saying the operator genuinely has
    several addresses. `timeline`, `participants`, `introductions`, `pending_introductions`,
    `is_inbound` and one inline `addr(me)` all kept taking a bare string.
    The cost, measured live: **30 of 61 messages on one contact were the operator's own mail
    filed as INBOUND**, because it came from the address on their résumé rather than the account
    the app authenticates as. `direction` decides who owes whom a reply, whether a ladder halts
    on a "reply", whether a handoff banner fires and what the temperature band reads — so half a
    thread was attributed to the wrong person in four subsystems at once, and the app believed a
    stranger had written to it thirty times.
    §Lessons 49 is usually "a rule at one of its two call sites". This is the version where the
    rule is documented, correct, and present in exactly one of seven places. The guard is a
    parametrised test over the FUNCTION SIGNATURES, because the seventh will be added by
    somebody who never reads this file.

103. **My fixture invented a field, so the code and the test agreed and the feature was blank.**
    The thread separator read `m.at`; the payload calls it `sent_at`. My test fixture carried
    `at` because I had put it there — so every assertion passed while the live separators
    rendered `2 messages ·` with **no date at all**. Only opening the browser caught it.
    §Lessons 93 with the halves swapped: there a frontend test fed itself a status the server
    never emitted; here it fed itself a FIELD the server never sends. The rule generalises —
    **at least one assertion has to start from what the server actually produces**, and a fixture
    you wrote to match your code proves only that your code matches your fixture.
    From the same change: the ordering test inserted its threads already in chronological order,
    so deleting the sort entirely left it green. **A test whose input is already sorted cannot
    see a sort.**

104. **Both numbers were right, and neither pointed at the other.** Reported as the follow-ups
    feature not working end to end: the counter said follow-ups were pending, the tab had none.
    Measured before touching anything — the panel matches the contacts on every job in every
    Space, the tab renders its cards, the click path opens the right job with 5 cards and 7
    buttons on screen, the console is clean. **Nothing was broken.** The counter is GLOBAL and
    the tab is PER JOB: 19 due across 6 jobs, and **24 jobs showing an empty Follow-ups tab**, so
    four times out of five you land on one that correctly says "Nothing due right now".
    The lesson is the order of work: *not working* and *working and pointing nowhere* have
    different fixes, and starting to repair the first would have been repairing something that
    was already correct. Verify the reported thing END TO END before believing the report's
    diagnosis — the operator is reliable about the EXPERIENCE and is not obliged to be right
    about the cause.

105. **A theme override for ONE element in an app with no themes.** The borrowed-contact banner
    rendered as a near-black bar with dark text on it, illegible. It hardcoded a cream background
    plus a `@media (prefers-color-scheme: dark)` rule — and this stylesheet has NO dark theme;
    that block was **the only one in the file**. On a machine whose OS is dark (confirmed live)
    it fired alone: the background went dark, the text did not move, everything around it stayed
    light. Two rules fell out. **Build from the palette's tokens** — `--yellow-soft` adapts with
    the app or with nothing, and either way agrees with its neighbours. And **a background and a
    foreground are one decision**: setting one without the other is the whole of how it became
    unreadable.

106. **One unplaced child in a grid whose siblings are all pinned.** The rebuilt meeting row put
    the buttons on `grid-row:1` and the meta line on `grid-row:2` and left the TITLE to
    auto-placement — which put it on row THREE, rendering the name of the meeting underneath its
    own date and summary. The markup is byte-identical either way and every test passed; only
    the browser showed it (§Lessons 101, again, in the same file). Name the row on every cell,
    and a test now fails if one is left unplaced.
    The row it replaced is worth keeping too: six columns on one line with the title in a
    `minmax(0,1fr)` and no overflow rule, so a real meeting name wrapped into five stacked words
    while the summary beside it was clipped to nothing. **Titles are whatever the operator
    pasted**, so they are long by default — the one-line grid only ever worked for a short
    fixture.

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
the same employer, because the finding was recorded on the JOB. That skip takes under a second
and writes only to the Activity tab, which is why "Restart end-to-end does nothing" is what it
looks like from the row — the fastest of the three refusals that all render as silence
(§Lessons 43, and still open as a UI gap).

**The banner is scoped to the Space; the REGISTRY is not** (2026-08-07). `ats_accounts` has no
`space_id` and must not grow one: an account covers an ATS TENANT, so one Salesforce Workday
login is the same fact on every tab, and partitioning the table would make you pay the same wall
once per Space. What is not shared is the banner's SENTENCE — *"8 employers need an account
before their jobs can run"* is a claim about jobs, and on Gauntlet (holding one) it named eight
belonging to job-search. `panel(jobs=…)` filters to realms a row on screen would actually hit;
`jobs=None` keeps the old behaviour for the CLI and is distinct from an empty list. Costs no
query — `realm_for` reads two URL strings and the caller already holds the rows.
**Applied and rejected rows drop out**, which was the larger effect: of the eight realms named,
five were walls on applications already submitted. Both tabs now read 1, and both are true.

**The apply profile no longer carries credentials.** `chrome.py:setup_worker_profile` copies the
operator's real Chrome profile, which is how sessions persist — and was also copying **682 saved
passwords, 2 credit cards and 831 autofill entries** into the browser the agent drives with
`bypassPermissions` on attacker-controlled careers pages. Excluded from the copy and purgeable
from the Accounts panel. **Cookies stay**, so no wall is paid twice.

## The human-in-the-loop apply model (2026-07-30)

**`APPLY_ALLOW_CONTRACT` widens what the agent will finish** (2026-08-12, default OFF, **ON
here**). An Ethos "Expert Opportunity" was refused six times with `not_a_job_application` in ~20s
each — correctly: `Compensation: $80/hour`, `Commitment: 5-20 hours per week`, and Ethos calls
itself an expert network, while the prompt said *"FULL-TIME salaried positions only"*.

The rule is **REPLACED, never caveated** (§Lessons 40). **Widening scope does not widen SAFETY** —
permissions, biometrics, payment details, SSN and executables are byte-identical either way, with
a parametrised test per rule and a second that diffs the whole prompt and fails if the flag moves
any line that is not about scope or rate. What survives in BOTH modes is the line that matters:
applying to a NAMED opportunity with its own description is an application; creating an account
to be listed is not.

**The salary floor had to move with it or the change bought nothing** — the floor is $200,000,
which is $96/hr, so an $80/hr posting is "below floor" and the refusal simply happens one step
later (§Lessons 49). A contract posting that states its rate is stating the employer's terms.

**The apply BUTTON is a queue runner, not a single job** — `/api/apply` calls
`queue_for_apply(limit=10)`, which selects by STATE (prepared, unapplied, never-attempted or
failed-under-cap) rather than by "just imported", whatever its docstring says. Pasting one URL and
clicking Apply worked through the queue: Ethos failed at 22:39:39 and Texas Sports Academy was
claimed at **22:39:40**, one second later. Reported as *"it is applying for the Texas Sports
Academy role, this should not be the behavior"*, and correctly. Not fixed.

### Parallel apply — `APPLY_WORKERS`, default 3 (2026-08-13)

Asked for as *"increase the number of applications this template can apply at a time"*. Measuring
first moved the work twice.

**The IMPORT was never the bottleneck.** The box already reads "Paste one or more job URLs" and
`_URL_RE.findall` already returns all of them — three pasted URLs import as three jobs, and did
before this change.

**Parallel apply already existed and the dashboard never used it.** `worker_loop` takes a
`worker_id`, each slot gets `BASE_CDP_PORT + id` and its own Chrome profile, jobs are claimed
atomically, and `_review_browser_alive` already probed four ports. But `run_dashboard_apply`
spawned `apply --url <one>` sequentially, **always on worker 0** — so there was one browser, and
stopping at the first handover was the only correct thing it could do. See §Lessons 8's
correction.

The rule, restated per slot and pinned by `tests/test_copilot_queue.py`:

- a slot **stops pulling** while it holds a filled form OR a login wall (`needs_human` is an open
  browser too — a captcha or a half-finished registration the operator is standing in);
- a slot whose job **applied or failed holds nothing**, so it takes the next one. The first
  version capped at one job per slot and silently regressed the auto-apply path from ten
  sequential to three; a test with seven jobs against two slots is what catches that;
- a slot with a **live review from an earlier run is routed around**, never reused;
- only a **full house** refuses to start.

`--worker-id` on the CLI is what makes it work: N single-worker processes on N distinct slots.
`tests/test_apply_worker_slots.py` proves the id reaches a different port, a different profile
and `worker_loop` itself — the dashboard's own tests fake `subprocess.run`, so a flag that were
accepted and dropped would leave every application on port 9222 with all of them green
(§Lessons 39).

**Clamped to [1, 6], and the cap is about the human.** Past a handful, filled forms get closed or
forgotten before anyone reaches them, which is §Lessons 8's loss in a slower shape. `1` restores
the old behaviour exactly and a test pins it. A malformed value reads as 3, never 0 or unlimited
(§Lessons 50).

**A worker profile was 7.6 GB, and 4.4 GB of it was Chrome's on-device ML.**
`OptGuideOnDeviceModel` (Gemini Nano, 4.0 GB), `SODALanguagePacks` (offline speech, 194 MB) and
two classifier stores, cloned per worker so an agent can type into a text box. Excluded, so a
slot costs ~3.2 GB. **Cookies still copy** — that is the only thing the clone was ever wanted
for, and a test pins it alongside the credential exclusions §Security posture added.

**The bug the tests caught:** the worker threads were using the request thread's sqlite
connection. `get_connection()` is thread-local by design, so every application would have failed
with a database error rather than a form.

**Still sequential, and the next thing to look at:** prepare (enrich → score → tailor → cover →
pdf). `queue_for_apply` requires `tailored_resume_path`, so nothing can be applied to until that
pass finishes, and it is the LLM-heavy half.

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

**A failed row says WHY in English** (2026-08-11, §Lessons 90's sibling). It used to print the
raw code with the Restart button's own description appended to it, so a live row read
`copilot_violation_agent_submitted Regenerates materials, then re-applies.` — two unrelated
sentences welded together, reported twice as "what does this mean?". The button's description
moved onto the button; `FAIL_WHY` carries a sentence for all **19** codes the launcher can
produce, with the raw token kept as hover text because it is what you grep the apply log for. A
test reads those codes OUT of `launcher.py`, since nothing else connects the two files. The
copilot wording deliberately refuses to say the application did not arrive — `applied_at` is
empty and the agent said it submitted, and nothing can tell which is true (§Lessons 19). **The
UI still offers no `Mark submitted ✓` on a `failed` row**, though the server endpoint accepts it;
the way out today is `⋯` → ✅ Mark as applied.

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

**Open, and the real ceiling:** nothing could tell you what works. 131 emails, 7 replies, and no
way to ask whether the personalised ones did better — so every improvement to the copy was
unfalsifiable. `draft_variant` (2026-08-03) starts fixing it, but nothing is readable until
enough tagged sends accumulate; `MIN_MEANINGFUL_N` is 10.

**MEASURED 2026-08-12, and the answer was not what was predicted.** Every CTX test asserts the
prompt *tells* the model to treat operator text as facts rather than phrasing, which is not the
same as it obeying — so all 13 drafts were generated against the live model for the Superbuilders
card, the worst case available: 13 people, no titles, no emails, no About.

**The BODIES held.** Median pairwise 6-gram similarity **2%** (max 19%), exactly ONE repeated
sentence across 13 and it is the cal.com URL, **0** em dashes, GauntletAI in **13/13** (the
`must_mention` retry works). The 41 shared phrases are the two URLs plus *"enterprise platforms
at Verizon and T-Mobile"*, which is a FACT appearing in 6 of 13. §Lessons 42 did NOT reproduce.

**The SUBJECT LINES did not.** `ai` in 13/13, `austin` in 12/13, `engineering` in 10/13, max
pairwise word overlap **89%**:

    ai engineering in austin, growth and sales focus
    exploring ai engineering opportunities in austin
    building ai for sales and growth in austin
    ai engineering for sales automation, austin

The system prompt's *"several people at the same company get these"* rule is aimed at the BODY;
the subject spec says only "lowercase-ish, specific, no quick question". And the subject is the
most visible surface there is — you do not have to open anything to see thirteen versions of one
sentence. **This is the next prompt fix and it is cheap.**

Two more from the same run, both prompt-compliance rather than repetition: **5 of 13 blow the
120-word cap** (up to 151, while the prompt says to be SHORTER when nothing is known about the
company), and **11 of 13 carry both a deck link and a calendar link** — two links in a cold first
email reads as a funnel.

The general lesson is the one worth keeping: **generating against real data answered in twenty
minutes a question that inspection had left open for weeks, and it answered it in the opposite
direction to the prediction.** Do it before the next prompt change, not after.

**Open, and reported twice:** the status BAND is still four words describing your own effort
(`new → active → cooling → cold`) plus two describing theirs (`warm`, `won`), presented as one
scale. The tooltips make the vocabulary learnable; they do not fix the conflation. The proposal
on the table is a **plan progress bar** (`5 of 20 sent` — self-explanatory, no glossary) plus a
separate **response marker**, which is what the four effort words were always approximating.
Not built, and it must never become a single bar to an interview: that is §Lessons 35 with a
percentage on it, and it would look most encouraging exactly when a job is most dead.

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
exist), ~~**SPACE-0**~~ **DONE 2026-08-08, and its diagnosis was wrong** — terminal rows
were 1 of 31, so archiving would have hidden 2.5% of the page. The scroll was a six-line
description clamp plus the premise box; 8,939px → 7,423px (§Lessons 76), **SPACE-6** (the business Space as a
falsifier — if it costs code, the PRD's central claim was wrong and should say so).

`docs/crm-prd.md` is the larger person-as-root version of the same idea. Not superseded — it is
the upgrade path §11 of the Spaces PRD points at. Do the graph when a real question needs it.

`docs/tickets/UX-README.md` — six dashboard defects reported 2026-08-04, **all six shipped**.
Three were the same failure: a value one layer computes that the other cannot see.

**`docs/tickets/SHEET-1-the-spreadsheet-space.md`** (2026-08-11) — **BUILT**, plus SHEET-1b and
SHEET-2 which are not in the ticket because both came out of running it. See §The sheet Space.
It doubled as SPACE-6's falsifier and needed no schema change.

**`docs/tickets/GRAN-1-meeting-transcripts.md`** (2026-08-12) — **phases 1 and 2 BUILT**, phase 3
blocked on a subscription. Paste a meeting transcript onto anyone; its SUMMARY then reaches all
six drafters. Three measurements decided it and all three came before any code: Granola is not
installed on this machine, its public API is **Business/Enterprise only** (so the paste is not a
shortcut, it is the only route that exists), and transcripts are diarized by **audio source**
rather than by speaker — so attribution to a named contact is the operator's CHOICE and can never
be inferred. Storage is one row per MEETING plus a join (migration 004), because a call has
several attendees and `messages.snippet` caps at 200/2000 against a 20–50 KB transcript.
**Only the summary reaches a prompt**, and the prompt is forbidden from quoting the call or
naming a recording (§Lessons 83, higher stakes than the deck beacon). **Unproven against a live
model** — every test asserts the prompt *says* so, which §Lessons 42 is precisely the gap.

**`docs/tickets/HIST-1-the-archive-lens.md`** (2026-08-11) — **designed, NOT built**, and pinned
by the operator. Asked for as a Space for old applications; narrowed by their own follow-up
question ("do you need my entire email history, or can you crawl it when I ask?") into a lens
over Gmail that stores nothing until a person is promoted. Measured first, and the measurement
is the ticket: a company-name crawl of Google returns 400+ threads and **8 humans, 7 of whom are
already contacts** — while the real history is employer-agnostic (400+ threads, 135 domains,
2017–2026) and every employer narrows to an **11-second crawl or less**. `gmail.readonly` is
already granted, so `q=` search works; ~2–3 days.

**`docs/tickets/CO-2-migrate-contacts-to-another-role.md`** (2026-08-13) — **SCOPED, answered,
NOT built.** CO-1's other half, from the direction that hurts. The live Google case: *Startups
Performance Lead* is **cancelled with 16 contacts (7 emailed)** and *AI Sales Specialist* is live
with 1. A move carries 25 messages, 13 touches and 5 sequences — and `touches`/`sequences` have
no `job_url` at all, so they follow `contact_id` blindly, which makes this **the only operation
in the app that can silently destroy a ladder**.
**The measurement that decides it: 16 of 16 stored messages and drafts name the cancelled role by
name.** So the obvious build — rewrite the foreign keys and stop — leaves unsent drafts pitching
a dead role on a live card, one click from sending, and ladders whose next touch follows up on a
job that no longer exists. Every row in the right place and the feature worse than not having it.
Decided: re-key rather than copy; unsent drafts CLEARED and sent messages never touched; ladders
reset while touches move as history (so the new role is a genuine first contact and
`burned_block` finally has something to read); the richer row wins a collision and two real
conversations are refused rather than interleaved; same employer only. Operator answered:
**a contact with no email does not move** (5 of 16, so the Google move is **11 people**, shown as
excluded with the reason rather than hidden), messages follow the person, and **undo ships in the
first cut**. ~2 days.

**`docs/tickets/CAL-1-send-a-calendar-invite.md`** (2026-08-13) — **SCOPED, NOT built, and gated
on a new OAuth consent.** The live token carries four Gmail scopes and nothing else; creating an
event needs `calendar.events`, which is a new consent screen rather than a config change. A Meet
link needs `conferenceData` on `events.insert`, which is why the previously built-and-reverted
`.ics` approach (`3480c37`, reverted `82eb429`) cannot be revived for this — it deliberately
avoided the scope and so could not produce a Meet link or reach the sender's own calendar.
It answers the operator's own objection to that revert: **cal.com is for a time NOT yet agreed;
a direct invite is for one that HAS been**, usually named by them in the thread. Two traps
recorded: `sendUpdates=all` means GOOGLE mails it, so it bypasses `gmail_send` and the daily
limit, company cap and cooldown never see it (§Lessons 77's shape); and cancel/reschedule must
ship WITH send. The agent check was re-run — its allowlist is two tools and `Read`/`Bash` are
denied, so the scope widens a STOLEN token, not the agent.

**`docs/tickets/CO-1-one-employer-many-roles.md`** (2026-08-11) — **the band is BUILT**; the
contact-keying half is not. Asked for as "bundle the cards for two jobs at one company", and the
measurement moved it: 0 of 33 employers held a second job, so the first version would have
rendered for nobody. Fixed by moving the `Google hiring AI Sales Specialist` row out of
`gauntlet` — which needed more than a Space change, because it carried §Lessons 85's `Uploaded`
as its company and §Lessons 84's board-sentence as its title, so it would have sat beside the
other Google job under a different name.

**EVERY employer gets the band now** (2026-08-12), not only the ones with two roles. The 2+
threshold was right about the STATS — "1 role" over one row is furniture — and wrong about the
NAME: with a single role the company appeared only as a small grey subtitle under the job title,
so you read the employer in the top-left of a grouped block and hunted for it on every other row.
Reported as *"when jobs are individual the name of the company is nowhere to be seen"*.

A solo band is deliberately a DIFFERENT control: no caret (collapsing one row is the furniture
the threshold was right about), no roles count, and **the name is double-click editable** —
banding a row hides the company subtitle that used to be its editor, and moving a name without
its editor leaves the only fix three clicks away in the Job tab (§Lessons 97). A multi-role band
is unchanged and is NOT editable: which of its rows an edit would write to has no answer.
A band needs a NAME, not a count — rows whose employer never resolved keep the subtitle, which is
the only place they can be given one.

**Its first version broke every card, and no test could see it** (§Lessons 100). `tr.co-solo > td
{ display:flex }` replaces the cell's `display:table-cell`, which drops it out of table layout —
`colspan="4"` stops applying and the cell collapses to the first column's width, so the band
rendered one word per line and ~130px tall. The flex belongs on a wrapper INSIDE the td, which is
what `.co-toggle` had always done. The guard is now a CSS rule rather than a markup assertion: no
selector ending in a `td` may set `display:flex/grid/block`.

A `tr.co-head` band renders above any employer, collapsible when it has 2+ rows, state
outside the DOM like `PANEL_OPEN`. **Every number on it is deduplicated across the roles** — two
rows each reporting "16 contacts" for the same sixteen humans reads as thirty-two — plus
`⚠ N on both`, which no other surface can show. Order came free: a `Map` over the already-sorted
list puts each group at its first member's slot, so there is no second ranking to disagree with
the `ORDER BY`. Live: `▾ Google · 2 roles · 16 people · 7 emailed · 1 replied · 5 follow-ups due`,
one band across 32 rows.
**The mutation that survived the first round of tests is the lesson**: fourteen tests of the
grouping functions all passed against a version that bands every single-role employer, because
none of them drove `renderJobsTable`. A test that never renders cannot see where a band lands.

The half that is not
layout is the reason it is written down anyway — `store.contact_id()` hashes **`job_url`**, so
the same recruiter found for a second role is a second contact row with its own ladder, and
`skip_known` cannot see them because it excludes per job. **`OUTREACH_COOLDOWN_DAYS` is back on
(30, 2026-08-11)** and is currently the only guard covering it; its refusal already reads
*"already emailed X for another role on …"*. It had been `0` since 2026-08-03, and 0 there means
"matches nothing", not "unlimited" (§Lessons 50). Costs nothing today: 109 sends, 109 distinct
addresses. `burned_block` is the open half — it is fed per contact, so it is empty in exactly the
case it exists for.

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

5. **`web_dashboard.py` is 4,095 lines** (and `dashboard.js` is 4,154), all Python, zero SQL. ~430 lines are pipeline
   orchestration (`run_dashboard_prepare/apply/fill_one/restart/continue`) that are not HTTP
   concerns. Extracting them is the natural companion to debt item 1.

9. **`identities` exists and nothing reads it — and now has a designed ticket.**
   `docs/tickets/ID-1-per-identity-sender.md` (2026-08-09), from a 10-agent map + adversarial
   review: 14 commits C0-C13, all three lenses `ship-with-changes`. Until it lands every Space
   sends from the one personal mailbox, so **do not create a business Space yet**.
   Three decisions taken, recorded here because they are cheap now and expensive later:
   **the freeze is strict** — any sent message freezes `identity_id`, email, LinkedIn DM or SMS
   ("no switching"); **a second identity gets its own HOST**, never a second path on the same
   site (`/intro/` is hardcoded in three places in `domain/deck.py` and a second path silently
   breaks `deck-relink` — 0 matches, exit 0); and **a NULL identity column means "inherit the
   globals" for `personal` ONLY**, because every one of those globals IS the personal mailbox's.
   An unconditional fallback means a half-configured business identity sends pitches
   authenticating as, From, and signed as the job-search account, with nothing raising.
   The rule: **credentials and voice refuse or derive from the identity's own token; policy
   numbers inherit.**
   Still open in that ticket and NOT yet built: `can_autosend` is enforced at one of four send
   doors, `offer_deck` is read at one of three deck-emitting drafters (both §Lessons 49), and
   the documented `identity_id` freeze **does not exist** — `domain/space.py:240` freezes
   `("id", "shape")` only, so a Space with 133 sent emails is repointable today with no error.

10. **`context` is 90 commits ahead of `main`, pushed, and the working tree is CLEAN**
    (2026-08-13, `2eae04d`). The 2026-08-11/13 run added the sheet Space (SHEET-1/1b/2), the
    ghost state, edit-in-place (EDIT-1), the LinkedIn ladder removal, the jobs-table render
    fixes, the HIST-1 spec, the multi-address identity fix, thread-scoped replies and parallel
    apply — each with its own tests and mutations.
    Merging to `main` is still deliberately deferred, and checking out `main` gets you a build
    without Spaces, the sheet import, the deck fix, the Oracle fix, any of the UX work or any
    outreach context.
    **The `~/.applypilot/` database is not in git either** — latest backup
    `applypilot-20260812-pre-direction-fix.db`, taken with the sqlite backup API because the WAL
    routinely holds more than the main file (4.1 MB against 1.8 MB once).

6. ~~**No per-company outreach cap.**~~ **CLOSED 2026-08-03** (`OUTREACH_COMPANY_CAP`, default
   8). Kept for the number: six companies were already OVER the cap the moment it shipped, three
   of them at 10 emails. Nothing had counted per employer, because the daily limit is global and
   the cooldown is per address. All three were set to 0 on this machine on 2026-08-04,
   deliberately — see §Lessons 50 for what 0 used to mean. **`OUTREACH_COOLDOWN_DAYS` went back
   to 30 on 2026-08-11** (CO-1): it is the only guard against the same person being emailed about
   a SECOND role, which the contact keying makes invisible everywhere else. The daily limit and
   the company cap are still 0.

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
- **On branch `context`** (2026-08-13, `2eae04d`), **90 commits ahead of `main`**, pushed to
  `origin/context`, nothing uncommitted. `main` last pushed at **`e1f0be6`**. Tags:
  `stable-arch2/3/5/6` · `stable-e2e-20260730` · `stable-crm-20260731`.
- **A frontend-only edit needs the `pip install` but NOT a dashboard restart** — the copy gives
  the file a new mtime, `?v=` changes with it, and a normal reload fetches it. A **Python** edit
  needs the restart, because the running server has those modules imported already. Getting this
  backwards wastes a restart, or worse, tests a change that is not loaded.
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

# PRD — ApplyPilot as a Relationship CRM (Spaces)

**Status:** Draft v1
**Owner:** Jorge · **Author:** Jorge + Claude
**Extends:** `networking-outreach-prd.md` (NET-1..5, shipped)
**Ticket prefix:** `CRM-*`

---

## Headline assumptions (read first)

1. **This is relationship building, not outbound sales. There is no pitch.** Decided
   2026-07-27. Every design choice below follows from it. A cadence/sequence engine, a
   separate cold-sending domain, and bulk-send tooling are **explicitly out of scope** —
   for this use case they are not just unnecessary, they are actively harmful. Nobody
   wants to discover they were put in a drip campaign by a family friend.

2. **The unit of value is warmth decay, not funnel progression.** An outbound CRM asks
   "where is this deal in the pipeline?" A relationship CRM asks **"who that I care about
   am I losing touch with?"** There is no "close." This single difference determines the
   data model, the default view, and what the AI is for.

3. **Two capabilities Jorge assumed exist do not.** There is no scheduling, cadence,
   follow-up, reminder, or due-date logic anywhere in the codebase (grepped 2026-07-27 —
   zero hits). The "schedule a call" feature is a Calendly link pasted into email copy
   (`outreach.py:65`). And nothing reads replies: outreach state is write-only
   (`drafted → sent → failed`). **These are the two largest net-new builds in this PRD**,
   not ports of existing work.

4. **The tool layer is already ~85% portable; the dashboard is the expensive part.**
   `outreach.draft_email()` reads exactly four fields off a job row. `web_dashboard.py` is
   2,898 lines with ~30 direct references to the `jobs` table. The data model is cheap to
   generalize; the UI is not (§4, §7).

5. **Personal Gmail is the correct sender here.** Because there is no pitch, the usual
   cold-outreach infrastructure (separate warmed domain, suppression lists, unsubscribe
   footers) is not required. This outreach *is* personal, sent one at a time, from Jorge's
   real address. Volume stays low by design — that is what keeps it appropriate.

---

## 1. Problem

ApplyPilot began as a bulk job-application runner. Through the networking, dashboard, and
co-pilot work it has become something closer to a CRM — but one whose root object is a
**job**. `contacts.job_url` is `NOT NULL`, and `store.contact_id()` hashes the job URL into
the contact's primary key, so **a person cannot exist in the system without a job attached
to them.**

That blocks the actual goal: one source of truth for *all* outreach and relationships, with
a dedicated window per context. Concretely, today:

- The same human at two companies is two unrelated rows.
- "Who do I know at Stripe?" is unanswerable; only "who did we find for the Stripe job?" is.
- The 899 imported LinkedIn connections are a render-time lookup table, not entities.
- There is nowhere to put a person who isn't attached to a job — e.g. a YPO member.
- Nothing detects a reply, so the system cannot tell "waiting on them" from "waiting on me."

## 2. Product thesis

**A Space is a dedicated dashboard over a shared tool layer.** "Job Search" is one Space.
"YPO Austin" is another. Both get contact enrichment, outreach drafting, phone/notes,
activity history, reply detection, and warmth tracking. What differs is the audience source,
the center panel, and the tone of the AI.

The engine already exists and is more general than its current naming suggests:

| Today's name | What it actually is | Job Space | YPO Space |
|---|---|---|---|
| `discover` | audience sourcing | scrape boards | import directory CSV |
| `score` | qualification | fit 1–10 vs. resume | *not used* |
| `tailor` | tailored artifact | resume per JD | *not used — no pitch* |
| `derive`+`rank`+Apollo | contact enrichment | find recruiters | find the CEO's details |
| `outreach.py` | message generation | "I applied for X" | "great to meet at the dinner" |

The Job Space keeps its 6-stage pipeline. The YPO Space skips scoring and tailoring
entirely — there is no artifact to generate, because there is no pitch.

## 3. Non-goals

Enumerated because each was considered and deliberately rejected:

- **No cadence / drip / sequence engine.** Follow-ups are suggested, never auto-sent.
- **No bulk send in relationship Spaces.** "Send all emails" stays a Job-Space affordance.
  A relationship Space sends one message at a time, each reviewed.
- **No separate cold-sending domain.** Personal Gmail is correct (§Headline 5).
- **No pitch artifacts** (decks, proposals, one-pagers) for relationship Spaces.
- **No lead scoring of humans.** Ranking people by predicted value is wrong here.
- **No LinkedIn automation.** Settled in §7 of `CLAUDE.md`; the manual copy → open →
  paste → send loop stays.
- **Not multi-user.** Single operator, localhost-only, as today.

## 4. What already works and ports for free

Verified by reading the code, 2026-07-27.

**Fully portable — zero job knowledge:** `apollo.py`, `rank.py`, `providers.py`,
`connections.py`, `gmail_oauth.py`, `linkedin_dm.py`/`dm_prompt.py` (compose helpers), and
the phone/notes panel. These take a company and a domain, not a job.

**Thin coupling — a 4-field swap:** `outreach.draft_email(profile, job, contact)` reads
only `job.get("title" | "company" | "full_description" | "site")`. Replacing `job` with a
generic `context` dict makes the drafting engine universal:

```python
context = {"title": ..., "company": ..., "description": ..., "source": ...}
# Job Space:  role,        employer,   job description,   "Greenhouse"
# YPO Space:  connection angle, their company, what I know about them, "YPO Austin directory"
```

**Real coupling — needs work:**

| Site | Problem |
|---|---|
| `store.py:21` | `job_url TEXT NOT NULL` — the person's existence requires a job |
| `store.py:54` | `contact_id()` hashes `job_url` into the PK |
| `store.py:360` | `get_contacts_for_job()` is the only read path |
| `gmail_send.py:106` | `job_attachments()` queries `jobs` for resume/cover PDFs |
| `derive.py` | job-row shaped; unused by YPO (company is already known) |
| `web_dashboard.py` | 2,898 lines, single hardcoded view over `jobs` |

## 5. Data model

```
spaces          id, name, kind(job|relationship), view_config, tone_directive,
                sending_identity, created_at
people          id, full_name, primary_email, linkedin_url, phone, notes,
                do_not_contact, created_at            ← THE ROOT OBJECT
organizations   id, name, domain
affiliations    person_id, org_id, title, is_current   ← survives job changes
memberships     person_id, space_id, source, warmth, last_touch_at,
                next_touch_due, stage                 ← per-Space state
interactions    id, person_id, space_id, kind(email_out|email_in|call|dm|note|meeting),
                body, occurred_at, gmail_message_id   ← the timeline
threads         gmail_thread_id, person_id, last_inbound_at, awaiting(them|me)
opportunities   id, org_id, kind(job), external_url    ← a job becomes one of these
person_context  person_id, key, value, learned_at      ← durable memory (§6.4)
```

Two structural decisions:

- **`person` is the root, not `job`.** A job becomes an `opportunity` attached to an
  organization; the Job Space is a Space whose audience source is the job pipeline.
- **Per-Space state lives in `memberships`, not on the person.** The same human can be in
  both the Job Space (as a recruiter) and the YPO Space (as a friend) with different
  warmth, different next touch, and a different tone — without duplicate rows.

## 6. Features

### 6.1 CRM-1 — Inbound engine (reply detection) — **build first**

The highest-leverage item in this document and the only one that requires no migration.

- Add the Gmail **read** scope to the existing OAuth flow (`gmail_oauth.py` already owns
  the token; this is a scope addition + re-consent, not new infrastructure).
- Poll `users.messages.list` on a background thread, newest-first, watermarked by
  `historyId` so each poll is incremental.
- Match inbound → outbound via `threadId` and `In-Reply-To`. `contacts.sent_message_id` is
  already persisted, so the join is available the moment anything reads.
- Write an `interactions` row per message and update `threads.awaiting`.
- Surface **"Waiting on them" / "They replied" / "Waiting on you"** in every Space.

Why first: it works against the 4 jobs already applied to, needs zero schema migration
beyond `threads`/`interactions`, and every later feature depends on knowing who replied.
Reply detection is what makes this a CRM rather than a nicer send button.

**Explicit non-goal:** do not use a Chrome job for this. Gmail has an API; driving a web UI
from outside is the mistake already made and abandoned twice (§7, §8 of `CLAUDE.md`).

### 6.2 CRM-2 — Person as root + Spaces

The migration (§7). Ships behind the existing dashboard with one Space ("Job Search")
backfilled, so nothing visibly changes until CRM-4.

### 6.3 CRM-3 — YPO Space: import + enrichment

- CSV/paste import → `people` + `organizations` + a `memberships` row, `source` recorded.
- Fuzzy-dedupe on email, then normalized name + company; **propose** merges rather than
  auto-merging (a wrong merge is far more destructive than a duplicate).
- Cross-reference the 899 LinkedIn connections on import — a member you already know
  should arrive pre-flagged as warm, not treated as cold.
- Apollo enrichment reuses the shipped path (company + domain → title, email, LinkedIn),
  plus the manual "Apollo ↗ → paste phone" loop for direct dials.

### 6.4 CRM-4 — Warmth, next touch, and the shell

The relationship-native replacement for a pipeline:

- **Warmth** derived, not manually maintained: `last_touch_at`, reply history, and whether
  they're a 1st-degree connection. Cheap and honest — no engagement scoring theater.
- **Next touch due** — a *suggested* date per person, respecting a per-Space default
  interval (e.g. 90 days for YPO). Suggested, never auto-sent.
- **The default view is "going cold"** — people you care about whose warmth is decaying,
  ranked by decay, not by predicted value. This is the home screen of a relationship CRM.
- **Dashboard shell extraction**: pull nav, contact cards, drafts, phone/notes, activity,
  and send out of `web_dashboard.py` into a Space-agnostic shell; the job pipeline becomes
  one pluggable center panel. This is the bulk of the UI work and it is what makes the YPO
  window nearly free (§7).

### 6.5 CRM-5 — Context memory

What makes relationship outreach not read like a template. A per-person `person_context`
store — where you met, what they do, what they care about, what you last discussed —
accumulated from notes, meeting logs, and (with review) inbound replies, then fed into
`draft_email`'s context.

For the Job Space the tailoring input is a job description. Here it is *the relationship
itself*. This is the single highest-value AI feature for the YPO use case: it's the
difference between "great to connect" and "great to meet at the Austin dinner — how did the
logistics acquisition land?"

## 7. Migration plan

Sequenced so nothing breaks and each step is independently shippable. The data cost is
near zero today — **5 jobs, 20 contacts** — which makes now the cheapest this will ever be.

1. **Additive only.** Create `spaces`, `people`, `organizations`, `affiliations`,
   `memberships`, `interactions`, `threads`, `person_context`. Each owns its own migration,
   following the `store.py` `_CONTACT_COLUMNS` pattern rather than `database._ALL_COLUMNS`.
2. **Backfill.** One Space, "Job Search," `kind=job`. Every existing contact gets a
   `people` row and a `memberships` row. `organizations` from `derive_company`.
3. **Relax the FK.** `contacts.job_url` becomes nullable. `contact_id()` keeps hashing
   `job_url` **when present** so all 20 existing IDs stay byte-identical — the extension
   API contract (`/api/ext/queue`) and every stored draft keep working untouched. New
   Space-only contacts hash `space_id` instead.
4. **Generalize drafting.** `draft_email(profile, context, contact)`. Keep a thin
   `job → context` adapter so the Job Space path is provably unchanged; diff generated
   prompts old-vs-new and require them byte-identical, exactly as was done for the ruff
   F841 cleanup.
5. **Extract the shell.** Space-agnostic frame + pluggable center panel.
6. **Add the YPO Space.** Import, enrich, draft, track.

Steps 1–4 are invisible to the user. The pipeline keeps running throughout.

## 8. Risks

| Risk | Mitigation |
|---|---|
| **Rewriting a working system.** The job pipeline works and is in active use. | Additive migrations; adapter keeps the job path byte-identical; ship behind one backfilled Space. |
| **Dashboard extraction is the real cost.** 2,898 lines, ~30 `jobs` references. | Treat as its own ticket with pinned tests (`test_dashboard_js_valid.py` already exists). Do not bundle with the model change. |
| **Gmail read scope widens blast radius.** The token gains inbox read. | Scope to `gmail.readonly`, never `modify`. Store only thread/message IDs plus matched bodies, not the whole mailbox. |
| **A wrong identity merge is destructive.** | Propose merges, never auto-merge; keep an undo. |
| **Relationship outreach drifting toward a pitch.** The engine can do outbound; a Space could quietly become one. | `kind=relationship` structurally disables bulk send and artifact generation, so the guardrail is in the model, not in discipline. |
| **YPO directory norms.** Member directories typically carry no-solicitation expectations. With no pitch this is largely moot, but volume is the remaining variable. | Low volume + high personalization by design; honor `do_not_contact` globally; no bulk send in relationship Spaces. |
| **Notes/context become the crown jewels.** Personal detail about real people accrues locally. | Stays in `~/.applypilot/` outside git, as today. Never sent anywhere except the LLM call that needs it. |

## 9. Open questions

1. **Naming.** "Space" vs. "Workspace" vs. "Campaign." Avoided "Campaign" — it carries
   outbound connotations this product explicitly rejects.
2. **Meeting capture.** Calendar read for "did the call happen?" — worth it, or is a manual
   note enough for v1?
3. **Warm-intro path-finding.** With 899 connections, "who can introduce me to X" is
   tractable. Its own ticket, or part of CRM-4?
4. **Do the two Spaces share a Person?** Recommended yes (that's the point of one source of
   truth), but it means a recruiter and a family friend live in one table. Confirm.
5. **Warmth interval per person or per Space?** Per-Space default with a per-person override
   is the obvious answer; confirm it's not over-building for v1.

## 10. Success criteria

- One question answers correctly across all Spaces: **"who do I know at $COMPANY?"**
- Every reply to any outreach is detected and surfaced without opening Gmail.
- A YPO member can be imported, enriched, drafted to, sent, tracked, and followed up
  without touching the job pipeline.
- The Job Space behaves identically to today — same prompts, same IDs, same PDFs.
- The default view answers **"who am I losing touch with?"** on load.

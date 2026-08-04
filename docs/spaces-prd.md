# PRD — Spaces: three templates, two shapes, N identities

**Status:** Draft v2 · 2026-08-04 (v1 2026-08-03)
**Owner:** Jorge · **Author:** Jorge + Claude
**Ticket prefixes:** `SPACE-*` (the lens) · `ID-*` (the sender)
**Relationship to `crm-prd.md`:** that document proposes person-as-root. This is the subset
that ships in days, deliberately compatible with it — §11.

---

## Headline decisions (read first)

1. **A Space is a manifest, not a fork.** A Space is a row of configuration — which
   capabilities are on, which tone, which cadence — not a copy of the pipeline. This codebase
   proved that pattern once already: `domain/followup.Channel` turned per-channel branching
   into data, after which a third channel cost **one column, one registry row, one prompt**.
   Spaces follow it, including the executable proof (§9).

2. **Three templates, but only TWO shapes.** *Business outreach* (freelance/contract) and
   *Business CRM* are the same machine — target company → find the C-suite → pitch →
   sequence → reply → booked call. Confirmed by the decision that the Business CRM tracks
   **prospects only**, no existing clients. The difference between them is who is sending.
   Building them as separate pipelines would pay twice for one panel.

3. **Identity is a first-class object, not a field on a Space.** "My business email, a
   separate business website" is the only genuinely new subsystem in this document.
   Everything about the sender is a single global today:

   ```
   TOKEN_PATH = ~/.applypilot/gmail_token.json      one account, period
   OUTREACH_FROM_NAME / _ADDRESS / _SIGNATURE       global env vars
   INTRO_DECK_URL = jorgealejandrodiez.com/intro/   one site
   DECK_HITS_TOKEN                                  one collector
   ```

4. **Send limits belong to the IDENTITY, not the Space.** v1 of this document put them on the
   Space and that was wrong: Gmail's ceiling is per account (~500/day personal, ~2,000
   Workspace). Two Spaces on one address share the real budget; a business address has an
   independent one. Per-Space caps may exist on top, but the binding constraint is the
   mailbox.

5. **Existing contact IDs must not change.** 187 contacts, 52 touches, 99 messages and every
   `sequences` row key off `contact_id`, which today hashes `job_url`. The migration in §6 is
   designed so the hash input for every existing row is **byte-identical** — a rename plus a
   default, not a re-keying.

6. **One database.** `space_id` keeps cross-space questions answerable and keeps backup to one
   file. Separate databases would be two things to remember to back up and would make "have I
   already emailed this person" unanswerable across Spaces — which is the exact failure the
   per-address cooldown exists to prevent.

---

## 1. Problem

The dashboard is one endless list over one table, and it can hold exactly one kind of thing:
a job posting. Three strategies exist and only one is representable.

| Template | Rows are | Artifact | Sender | Success |
|---|---|---|---|---|
| **1. Jobs** | job postings | résumé + cover letter | personal | 🎯 interview scheduled |
| **2. Business outreach** | target companies | capability deck / proposal | personal | 📞 call booked |
| **3. Business CRM** | target companies | the business's deck | **the business** | 📞 call booked |

Nothing today can hold a contact who has no job attached — `contacts.job_url` is `NOT NULL`
and `contact_id()` hashes it into the primary key. **A person cannot exist in this system
without a job.** That is the wall, and the nav bar is not it.

## 2. Thesis

**A Space is a saved view plus a capability manifest, over one shared engine, sending as one
identity.**

Everything expensive is already built and already generic: contact discovery, Apollo
enrichment, verification, drafting, Gmail send + threading, reply detection, intent
classification, three follow-up ladders, deck-click tracking, booking detection, metrics.
That is the half a separate CRM would have to duplicate, and it is the half that ports free.

What differs per Space is small and declarative. What differs per Identity is a mailbox, a
domain, and a set of limits.

## 3. Non-goals

Considered and rejected for *this* document, not forever.

- **No existing-client / account management.** Decided 2026-08-04: the Business CRM is
  prospects only. Revisit when a prospect actually converts and there is a real relationship
  to keep — the trigger is a won deal, not a hunch.
- **No person-as-root migration.** No `people` / `organizations` / `affiliations`. The same
  human in two Spaces is two rows, exactly as the same human at two companies is two rows
  today. §11 is the upgrade path; do not pre-build it.
- **No deal stages, forecasting, or pipeline value.** Prospecting is `contacted → replied →
  booked`. A deal stage machine with no deals is theatre.
- **No cadence engine that auto-sends to relationships.** Carried from `crm-prd.md`.
- **No cross-space dedupe or merge UI.** A duplicate is survivable; a wrong merge is not.
- **Not multi-user.** Single operator, localhost. If the business ever gains a second person
  this document is void and `crm-prd.md`'s model is the starting point.
- **No new sending infrastructure.** Gmail/Workspace OAuth throughout, one message at a time.

## 4. What ports unchanged

Verified against the code. These take a company, a domain and a contact — no job knowledge,
no edit required:

`apollo.py` · `providers.py` · `rank.py` · `connections.py` · `verify.py` ·
`domain/verification.py` · `domain/company.py` · `domain/followup.py` · `touches.py` ·
`messages.py` · `gmail_read.py` · `replies.py` · `domain/conversations.py` ·
`domain/intent.py` · `domain/interactions.py` · `domain/metrics.py` · `bookings.py`

**Thin coupling — a field swap.** `outreach.draft_email(profile, job, contact)` reads four
keys off the job row. Replacing `job` with a `context` dict makes drafting universal:

```
context = {"title", "company", "description", "source"}
Jobs:      the role      employer    the job description      "LinkedIn"
Outreach:  the opening   the target  what THEY do             "YPO directory"
```

**Real coupling — the work:**

| Site | Problem |
|---|---|
| `networking/store.py` | `job_url TEXT NOT NULL`; `contact_id()` hashes it into the PK |
| `networking/gmail_oauth.py` | one `TOKEN_PATH`; `connected_email()` cached on its mtime |
| `networking/gmail_send.py` | `job_attachments()` queries `jobs`; from/signature are globals |
| `networking/deck_hits.py` | one collector URL, one token |
| `networking/service.py` | `find_contacts_for_job()` derives the employer from a job URL |
| `web_dashboard.py` | 3,026 lines, one hardcoded view over `jobs` |
| `static/dashboard.js` | 2,503 lines, one table renderer |
| `settings.py` | limits, cooldowns, deck URL — all global |

## 5. The two shapes

**`pipeline/jobs`** — a row is a job posting, contacts nested. Anchor: the job URL. Unchanged
from today.

**`pipeline/targets`** — a row is a company you want to work with, contacts nested. Anchor:
`target:<space_id>:<slug>`, space-scoped so the same company pursued in two Spaces is
deliberately two rows.

There is no third shape. A flat people list was in v1 for a relationship Space; the
prospects-only decision removed the need for it.

**Why `targets` is not a new pipeline:** structurally it is the jobs panel with the discovery
stages removed. No scoring, no tailoring, no cover letter, no apply agent, no ATS check, no
sign-in walls. It *adds* one field, §7's offer.

## 6. Data model

Two new tables, one renamed column.

```sql
CREATE TABLE identities (
    id                   TEXT PRIMARY KEY,   -- 'personal', 'acme'
    name                 TEXT NOT NULL,
    token_path           TEXT NOT NULL,      -- ~/.applypilot/gmail_token_<id>.json
    from_name            TEXT,
    from_address         TEXT,
    signature_html       TEXT,
    deck_base_url        TEXT,
    deck_collector_url   TEXT,
    deck_collector_token TEXT,
    daily_limit          INTEGER,            -- 0 = unlimited; per MAILBOX (§Headline 4)
    created_at           TEXT
);

CREATE TABLE spaces (
    id           TEXT PRIMARY KEY,           -- 'job-search', 'partnerships', 'acme'
    name         TEXT NOT NULL,
    template     TEXT NOT NULL,              -- 'jobs' | 'outreach' | 'business'
    shape        TEXT NOT NULL,              -- 'pipeline/jobs' | 'pipeline/targets'
    identity_id  TEXT NOT NULL,
    config       TEXT,                       -- JSON manifest (§7)
    position     INTEGER,
    archived_at  TEXT,
    created_at   TEXT
);

-- migration 003
ALTER TABLE contacts RENAME COLUMN job_url TO anchor;
ALTER TABLE contacts ADD COLUMN space_id TEXT NOT NULL DEFAULT 'job-search';
ALTER TABLE jobs     ADD COLUMN space_id TEXT NOT NULL DEFAULT 'job-search';
```

`anchor` is a job URL for `pipeline/jobs` and `target:<space>:<slug>` for
`pipeline/targets`. `contact_id(anchor, linkedin_url, name)` is otherwise untouched, so
**every existing contact keeps its id** and every touch, sequence, message and interaction
stays attached. §9 tests exactly this.

## 7. The manifests

Shaped deliberately like `domain/followup.Channel`.

```python
@dataclass(frozen=True)
class Space:
    id: str
    name: str
    template: str                  # 'jobs' | 'outreach' | 'business'
    shape: str                     # 'pipeline/jobs' | 'pipeline/targets'
    identity_id: str = "personal"
    tone: str = ""                 # injected into every draft
    offer: str = ""                # §7.1 — the constant pitch, outreach shapes only
    channels: tuple = ("email", "linkedin", "sms")
    schedules: dict = ...          # per-channel hour overrides
    tailor_docs: bool = True       # résumé + cover generation and attachment
    offer_deck: bool = True
    can_autosend: bool = True
    company_cap: int = 0           # 0 = unlimited
    terminal: str = "interview"    # 'interview' | 'booked' — what success means
```

The three templates as data:

```python
JOBS      = Space("job-search", "Job Search", template="jobs",
                  shape="pipeline/jobs", identity_id="personal")

OUTREACH  = Space("partnerships", "Partnerships", template="outreach",
                  shape="pipeline/targets", identity_id="personal",
                  tailor_docs=False, terminal="booked",
                  schedules={"email": (120, 288)},
                  tone="A peer proposing work, not an applicant.",
                  offer="<one paragraph, §7.1>")

BUSINESS  = Space("acme", "Acme Labs", template="business",
                  shape="pipeline/targets", identity_id="acme",
                  tailor_docs=False, terminal="booked",
                  schedules={"email": (120, 288)},
                  tone="Writing as Acme Labs, not personally.",
                  offer="<one paragraph>")
```

**`BUSINESS` differs from `OUTREACH` by `identity_id` and wording.** That is the whole claim
of this document, and §9's `SPACE-6` exists to falsify it.

### 7.1 The offer — the one genuinely new field

In the jobs pipeline the *description* varies per row and the *pitch* is constant (your
résumé). In an outreach pipeline that inverts: **your pitch is constant and their situation
varies.** So the Space carries one paragraph describing what you are proposing, written once,
feeding every draft:

> *"I build autonomous agent systems — pipelines that take a real operational workflow and run
> it end to end."*

It occupies the slot `full_description` fills today. Nothing else about drafting changes.

## 8. Phases

Ordered so that template 2 is usable before any identity work exists — it sends as the
personal identity that is already connected.

| Ticket | What | Notes |
|---|---|---|
| **SPACE-0** | Archive terminal rows | Independent, ships first. §8.1 |
| **SPACE-1** | Migration 003 + manifests, backfilled to one Space | Invisible on screen |
| **SPACE-2** | Nav, `?space=`, `/api/status` filtering | Job Search only; structural |
| **SPACE-3** | `pipeline/targets` panel + target import + the offer field | Template 2 exists |
| **SPACE-4** | Manifest actually applied: tone, cadence, docs, deck, terminal state | |
| **ID-1** | `identities` table; per-identity token path, from, signature, limits | The real work |
| **ID-2** | Per-identity reply polling + a second deck collector | §8.2 |
| **SPACE-6** | The business Space — **a config row** | The falsifier |

### 8.1 SPACE-0 — archive (ships first, independent)

The complaint that started this was "endless infinite scroll". The cause is that **nothing
ever leaves**: applied, rejected, interviewing and expired rows render forever. A `Done`
bucket collapsing everything terminal, on by default, fixes it. Half a day, no schema change,
useful whatever happens to the rest of this document.

### 8.2 What Workspace changes (ID-1 / ID-2)

Assumed for the business identities. Three consequences:

- **App passwords are usually disabled by Workspace admins**, so the SMTP fallback is not
  available and OAuth is mandatory for a business identity. `transport()` must not offer SMTP
  there. `gmail_send` already detects a 535 and surfaces guidance; this makes it a precondition
  rather than a surprise.
- **The OAuth client may need admin allowlisting.** If the Workspace restricts third-party
  apps, the admin must approve the client ID. That is an operator action, not code, and it
  belongs in the connect flow's error text.
- **The daily ceiling differs** — ~2,000/day Workspace vs ~500 personal. `identities.daily_limit`
  defaults accordingly, and this is why §Headline 4 puts limits here.

**One trap to carry forward:** `connected_email()` is cached on the token file's mtime
(§Lessons 26 — it was an HTTP call per job inside a 2.5s refresh). With several tokens the
cache key must include the **path**, or one identity is served another's address. That is the
same shape as every substring bug this codebase has already paid for: a cache keyed on less
than the thing it identifies.

Two tokens also means two `chmod 600` files holding live mail credentials. The security
posture note in `CLAUDE.md` needs the count updated, not its conclusion.

## 9. How this is proven, not claimed

Mirrors `test_adding_a_channel_needs_no_schema_change`, which is why SMS really did cost one
column — and which caught the one line where the claim was false.

- **`test_adding_a_space_needs_no_schema_change`** — define a Space that exists nowhere in the
  codebase, drive it end to end, assert no migration ran. **Name a Space that is genuinely
  unknown**: the channel version of this test originally named SMS, and shipping SMS silently
  broke its arithmetic because the fake channel started resolving real settings.
- **`test_every_contact_id_survives_the_migration`** — snapshot all 187 ids, run 003, assert
  the set is identical and every touch/sequence/message still joins. A re-key here silently
  detaches 99 messages.
- **`test_the_business_space_is_only_config`** — SPACE-6 asserted as a diff: adding it touches
  `spaces` and `identities` rows and no Python.
- **`test_limits_are_per_mailbox_not_per_space`** — two Spaces on one identity share a budget;
  two identities do not.
- **`test_connected_email_is_cached_per_identity`** — two tokens, assert neither serves the
  other's address.
- **`test_a_business_identity_refuses_smtp`** — Workspace precondition, asserted at the send
  path rather than by hiding a button.
- **`test_the_query_budget_does_not_move`** — the existing 80-statement ceiling, unchanged.

## 10. Risks

| Risk | Mitigation |
|---|---|
| **Re-keying contacts detaches all history** | `anchor == job_url` for existing rows; hash input byte-identical. Tested. |
| **Identity leaks across Spaces** — business mail sent from the personal address, or vice versa | Identity resolved from the Space at send time, never from a global. The from-address is drawn on screen before Send, same reasoning as the Cc chips (§Lessons 29). |
| **A Space becomes a silo** | Contacts stay in ONE table with `space_id`. Cross-space questions stay answerable by dropping a filter. |
| **The abstraction leaks; each template needs code** | SPACE-6 is the falsifier. If it costs code, say so rather than absorbing it. |
| **Workspace OAuth blocked by an admin** | Detected at connect, surfaced with the client ID to allowlist. Not discoverable at send time. |
| **`/api/status` slows down** | Filtering is a WHERE clause; the budget test holds. §Lessons 26 — it only counts SQL, so per-identity Gmail calls must stay off that path. |
| **Scope creep into `crm-prd.md`** | §3 is explicit. Warmth, orgs, merges, clients are out. |

## 11. Relationship to `crm-prd.md`

Not superseded. That document is this with `person` as the root object — which correctly
answers "who do I know at Stripe?" and lets one human sit in two Spaces under one identity.

This ships the lens without the graph. The upgrade path is intact: `spaces` is already its
table, `contacts` splits into `people` + `memberships` with `space_id` becoming the membership
row, and `anchor` becomes the opportunity link. Nothing here has to be undone.

**Do the graph when a real question needs it** — the first time one person's history across
two Spaces matters, or duplicates become annoying. Not before.

## 12. Decisions taken 2026-08-04

1. Business CRM is **prospects only**. No client/account object. Trigger to revisit: a won deal.
2. **One database**, `space_id` scoped.
3. Business identities are **Google Workspace**; OAuth mandatory, SMTP unavailable.
4. Limits move from Space to **Identity** (correction to v1).
5. Templates 2 and 3 share one shape; the third template is a config row, not a build.

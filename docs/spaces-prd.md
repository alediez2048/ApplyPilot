# PRD — Spaces: one dashboard per prospecting strategy

**Status:** Draft v1 · 2026-08-03
**Owner:** Jorge · **Author:** Jorge + Claude
**Ticket prefix:** `SPACE-*`
**Relationship to `crm-prd.md`:** that document proposes person-as-root with
`people` / `organizations` / `affiliations` / `memberships`. This one is the subset that
ships in days instead of weeks, and is deliberately compatible with it — §10.

---

## Headline decisions (read first)

1. **A Space is a manifest, not a fork.** The thing that makes this affordable is that a
   Space is a row of configuration — which capabilities are on, which tone, which limits —
   and *not* a copy of the pipeline. This codebase has already proved that pattern once:
   `domain/followup.Channel` turned per-channel branching into data, and the third channel
   (SMS) then cost **one column, one registry row, one prompt**. Spaces follow that
   precedent exactly, including its executable proof (§8).

2. **This resolves the "is Gauntlet a tag or a Space?" argument by making it not matter.**
   The objection to Spaces was that building a heavyweight abstraction for a
   tag-sized problem is waste. If a Space is one config row, that objection dissolves:
   Gauntlet becomes a Space because it costs a row, not because it earned an abstraction.

3. **The nav bar is the cheap half. `contacts.job_url TEXT NOT NULL` is the expensive
   half.** `contact_id()` hashes the job URL into the primary key, so **a person cannot
   exist in this system without a job attached**. A YPO member has no job and never will.
   That one column is the whole migration.

4. **Existing contact IDs must not change.** 187 contacts, 52 touches, 99 messages and every
   `sequences` row key off `contact_id`. The migration below is designed so the hash input
   for every existing row is **byte-identical**, which makes it a rename plus a default
   rather than a re-keying.

5. **Relationship Spaces never auto-send.** Carried forward from `crm-prd.md` §Headline 1 and
   non-negotiable: *"Nobody wants to discover they were put in a drip campaign by a family
   friend."* YPO is a peer network where reputation is the asset being spent. Drafting is
   automated; sending is not.

---

## 1. Problem

The dashboard is one endless list over one table. Every job the operator has ever pasted —
applied, rejected, expired, interviewing — renders forever in a single scroll, and there is
exactly one category of thing it can hold.

Three prospecting strategies exist today and only one is representable:

| Strategy | Audience source | Job posting? | Résumé / tailoring? | Tone |
|---|---|---|---|---|
| **LinkedIn** (in use) | operator, by hand | yes | yes | applicant |
| **Gauntlet partners** | a partner list | often none | **yes** | applicant |
| **YPO members** | a member directory | none | **no** | peer |

Gauntlet is the job pipeline with a different audience source. YPO is a different mode
entirely — no artifact, no pitch, and the copy that works in one is actively wrong in the
other. Mixing them in one list is not merely untidy: a YPO member reached with job-applicant
copy is a relationship spent for nothing.

## 2. Thesis

**A Space is a saved view + a capability manifest over one shared engine.**

Everything expensive is already built and already generic: contact discovery, Apollo
enrichment, verification, outreach drafting, Gmail send + threading, reply detection,
follow-up ladders across three channels, deck-click tracking, metrics. That is the half that
would have to be duplicated by a separate CRM, and it is the half that ports for free.

What differs per Space is small and declarative: where the audience comes from, what the
centre panel renders, which capabilities are enabled, what tone the drafting prompt takes,
and what the sending limits are.

## 3. Non-goals

Each considered and rejected for *this* PRD, not forever.

- **No person-as-root migration.** No `people` / `organizations` / `affiliations` tables. The
  same human in two Spaces is two rows, exactly as the same human at two companies is two
  rows today. §10 is the upgrade path; do not pre-build it.
- **No warmth decay, no "going cold" home screen.** Correct for a relationship CRM, and worth
  building once a relationship Space has real usage rather than before.
- **No cadence engine for relationship Spaces.** See §Headline 5.
- **No cross-space dedupe or merge UI.** A duplicate is survivable; a wrong merge is not.
- **Not multi-user.** Single operator, localhost, as today.
- **No new sending infrastructure.** Personal Gmail throughout.

## 4. What ports unchanged

Verified against the code, 2026-08-03. These already take a company, a domain and a contact —
they have no job knowledge and need no edit:

`apollo.py` · `providers.py` · `rank.py` · `connections.py` · `verify.py` ·
`domain/verification.py` · `domain/company.py` · `domain/followup.py` · `touches.py` ·
`sequences` · `messages.py` · `gmail_oauth.py` · `gmail_send.py` (transport) ·
`gmail_read.py` · `replies.py` · `deck_hits.py` · `domain/conversations.py` ·
`domain/intent.py` · `domain/interactions.py`

**Thin coupling — a field swap.** `outreach.draft_email(profile, job, contact)` reads four
keys off the job row (`title`, `company`, `full_description`, `site`). Replacing `job` with a
`context` dict makes drafting universal:

```
context = {"title", "company", "description", "source"}
Job Space:  role         employer   job description        "LinkedIn"
YPO Space:  how we met   their co.  what I know about them "YPO Austin"
```

**Real coupling — the work of this PRD:**

| Site | Problem |
|---|---|
| `networking/store.py` | `job_url TEXT NOT NULL`; `contact_id()` hashes it into the PK |
| `networking/store.py` | `get_contacts_for_job()` is the only read path |
| `networking/gmail_send.py` | `job_attachments()` queries `jobs` for résumé/cover PDFs |
| `networking/service.py` | `find_contacts_for_job()` takes a job row and derives from a URL |
| `web_dashboard.py` | 3,026 lines, one hardcoded view over `jobs` |
| `static/dashboard.js` | 2,503 lines, one table renderer |
| `settings.py` | send limits, cooldowns and caps are **global**, not per-Space |

## 5. Data model

Two tables and one renamed column. That is the entire schema change.

```sql
CREATE TABLE spaces (
    id           TEXT PRIMARY KEY,   -- 'job-search', 'gauntlet', 'ypo'
    name         TEXT NOT NULL,      -- 'Job Search'
    kind         TEXT NOT NULL,      -- 'pipeline' | 'list'
    config       TEXT,               -- JSON manifest (§6)
    position     INTEGER,            -- nav order
    archived_at  TEXT,
    created_at   TEXT
);

-- contacts: job_url -> anchor, plus a space
ALTER TABLE contacts RENAME COLUMN job_url TO anchor;   -- migration 003
ALTER TABLE contacts ADD COLUMN space_id TEXT NOT NULL DEFAULT 'job-search';
```

**`anchor` is what a contact hangs off.** For a pipeline Space it is the job URL, exactly as
today. For a list Space it is `space:<id>`. `contact_id(anchor, linkedin_url, name)` is
otherwise unchanged, so **every existing contact keeps its id** and every `touches`,
`sequences`, `messages` and `interactions` row stays attached. This is the single most
important property of the migration and §8 tests it directly.

`jobs` is untouched and gains `space_id TEXT NOT NULL DEFAULT 'job-search'` only if a second
pipeline Space (Gauntlet) needs its own queue — which it does, so it is in scope.

## 6. The manifest

A `domain/space.py` dataclass, deliberately shaped like `domain/followup.Channel`:

```python
@dataclass(frozen=True)
class Space:
    id: str
    name: str
    kind: str                     # 'pipeline' | 'list'
    centre: str                   # 'jobs' | 'people'
    tone: str = ""                # prompt directive, injected into every draft
    channels: tuple = ("email", "linkedin", "sms")
    schedules: dict = ...         # per-channel hour overrides
    can_autosend: bool = True     # False disables Send entirely (§Headline 5)
    tailor_docs: bool = True      # résumé + cover generation and attachment
    offer_deck: bool = True
    daily_limit: int = 0          # 0 = unlimited, matching the send-cap convention
    company_cap: int = 0
    audience: str = "manual_url"  # 'manual_url' | 'csv_import'
```

The three Spaces as data, which is the whole point:

```python
JOB_SEARCH = Space("job-search", "Job Search", kind="pipeline", centre="jobs",
                   audience="manual_url")

GAUNTLET   = Space("gauntlet", "Gauntlet Partners", kind="pipeline", centre="jobs",
                   audience="manual_url",
                   tone="Reference the Gauntlet AI program as the shared context.")

YPO        = Space("ypo", "YPO Austin", kind="list", centre="people",
                   audience="csv_import",
                   tone="A peer, not an applicant. No pitch, no ask in the first message.",
                   channels=("email",), schedules={"email": (168, 720)},
                   can_autosend=False, tailor_docs=False, offer_deck=False,
                   company_cap=2)
```

Gauntlet differs from Job Search by **one line of tone**. That is the test of whether this
abstraction is real.

## 7. Features

### SPACE-0 — Archive the finished (independent, ship first)

Not part of the abstraction and does not wait for it. The "endless scroll" complaint is that
**nothing ever leaves**: applied, rejected, interviewing and expired rows all render forever.
A `Done` bucket that collapses everything terminal, on by default, fixes the complaint the
operator actually opened with. Half a day, no schema change, useful whatever happens to the
rest of this document.

### SPACE-1 — Schema + manifest (invisible)

Migration 003: rename the column, add `space_id`, create `spaces`, backfill one row
(`job-search`) over all 187 contacts and 22 jobs. `store.py` reads switch to `anchor` with
`get_contacts_for_job()` kept as a shim. **Nothing changes on screen.** Ships alone,
verified by the id-stability test in §8.

### SPACE-2 — Nav + space switching

A top nav listing Spaces, a `space` query param, and `/api/status?space=<id>` filtering. The
jobs table becomes the `centre="jobs"` panel rather than the only panel. Job Search is the
only Space, so the UI is one tab — the change is structural, not visual.

**Query budget:** `/api/status` is held to 80 statements and re-runs every 2.5s. Space
filtering is a `WHERE space_id = ?`, not a new query. `tests/test_query_budget.py` must not
move.

### SPACE-3 — The `list` centre panel + import → YPO exists

A people table instead of a jobs table: name, company, title, last touch, channel pills,
reply state. Reuses `contactRow`, the channel tabs, the composer and the conversation view
verbatim — those already take a contact, not a job. Plus CSV/paste import →
`contacts` rows with `anchor='space:ypo'`, cross-referenced against the 899 imported LinkedIn
connections so someone already known arrives flagged warm rather than treated as cold.

### SPACE-4 — The manifest is actually applied

Tone injected into the drafting prompts. `can_autosend=False` removes Send rather than
disabling it. `tailor_docs=False` skips résumé/cover generation and attachment.
`offer_deck=False` stops `ensure_intro_deck` firing. Limits read per Space instead of from
the global env vars — **today `OUTREACH_DAILY_LIMIT` is one number across everything, so a
YPO push would silently eat the job-search sending budget.**

### SPACE-5 — Gauntlet, as the proof

Add one row. If SPACE-1..4 are right, a second pipeline Space with its own queue, its own
tone and its own limits costs a config entry and nothing else. If it costs code, this
abstraction is wrong and the PRD should say so out loud rather than absorbing the difference.

## 8. How this is proven, not claimed

Mirrors `test_adding_a_channel_needs_no_schema_change`, which is why the SMS channel really
did cost one column — and which caught the one line where the claim was false
(`followup_panel` spelled both channel keys out by hand, so a third channel passed through
every part of the engine and then vanished at the return statement).

- **`test_adding_a_space_needs_no_schema_change`** — defines a Space that exists nowhere in
  the codebase, drives it end to end (import → contacts → draft → follow-up → reply), and
  asserts no migration ran. **Name a Space that is genuinely unknown**: the channel version
  of this test originally named SMS, and shipping SMS silently broke its arithmetic because
  the fake channel started resolving real settings.
- **`test_every_contact_id_survives_the_migration`** — snapshot all 187 ids before, run
  migration 003, assert the set is identical and that every `touches` / `sequences` /
  `messages` row still joins. A re-keying here silently detaches 99 messages and 52 touches.
- **`test_a_relationship_space_cannot_autosend`** — assert the send path refuses, not that
  the button is hidden. A hidden control with a live endpoint is not a guarantee.
- **`test_send_limits_are_per_space`** — exhaust one Space's daily limit, assert another can
  still send.
- **`test_the_query_budget_does_not_move`** — the existing test, unchanged.

## 9. Risks

| Risk | Mitigation |
|---|---|
| **Re-keying contacts detaches all history** | `anchor == job_url` for existing rows, so the hash input is byte-identical. Tested explicitly. |
| **A Space becomes a silo** | Contacts stay in ONE table with a `space_id` column. Cross-space questions stay answerable by dropping the filter — unlike a separate CRM, where they become impossible. |
| **The abstraction leaks and each Space needs code** | SPACE-5 is the falsifier. If Gauntlet costs code, say so. |
| **YPO outreach reads as automated** | `can_autosend=False`, one message at a time, per-Space cap of 2 per company. |
| **`/api/status` slows down** | Filtering is a WHERE clause. The budget test holds the line; §Lessons 26 is the reminder that it only counts SQL. |
| **Scope creep into `crm-prd.md`** | §3 is explicit. Warmth, orgs and merges are out. |

## 10. Relationship to `crm-prd.md`

That document is not superseded and it is not wrong — it is the version of this with `person`
as the root object, which correctly answers "who do I know at Stripe?" and lets one human be
in two Spaces with different warmth and one identity.

This PRD deliberately ships the lens without the graph. The upgrade path is intact: `spaces`
becomes the `spaces` table it already describes, `contacts` splits into `people` +
`memberships` with `space_id` becoming the membership row, and `anchor` becomes the
opportunity link. Nothing here has to be undone to get there.

**Do the graph when a real question needs it** — the first time the operator wants one
person's history across two Spaces, or the duplicate count becomes annoying. Not before.

## 11. Open questions

1. **Does Gauntlet outreach attach a résumé?** If yes it is `kind="pipeline"` as modelled. If
   no, it is a third shape and the manifest needs `tailor_docs=False` on a pipeline Space —
   which the design allows but which nothing yet exercises.
2. **"Send automated scripts to his team"** — to the CEO's *recruiting* team (a pipeline
   behaviour, fine) or to fellow YPO members (refused by §Headline 5)? This changes
   `can_autosend` for the YPO Space and nothing else.
3. **Does a job ever belong to two Spaces?** A Gauntlet partner who also posts on LinkedIn.
   Modelled as two rows today. Acceptable, or is `space_id` on `jobs` wrong?

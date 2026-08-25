# PRD — The knowledge graph: one person, one page, three projections

**Status:** Draft v2 · 2026-08-24 — **adversarially reviewed, see §15**
**Owner:** Jorge · **Author:** Jorge + Claude
**Ticket prefix:** `KG-*` — deliberately NOT `CRM-*`. `CRM-2`…`CRM-5` are taken by shipped
features (`ARCH-README.md` records CRM-2 as outcome metrics, CRM-3 as the scheduler, CRM-4 as
conversations), so `crm-prd.md` §6's own numbering no longer resolves. §13.4 amends that document
rather than reusing its ids.
**Extends:** `crm-prd.md` §5 (person as root, `organizations`, `person_context`) and
`spaces-prd.md` §11 (the stated upgrade path). Neither is superseded; this executes §11's trigger.
**Adjacent, not merged:** `docs/tickets/HIST-1-the-archive-lens.md` — see §3.

**What changed in v2.** Three adversarial reviews (breaks-the-app, wrong-abstraction,
never-gets-used) returned 7 blockers, 9 highs and 7 mediums. Nineteen are fixed in the design
below; four are argued in **§14 Challenged and answered**. The four structural changes are worth
stating up front, because v1 is wrong about all four:

1. **The person id is a stored surrogate, not a hash of mutable fields.** v1's derived cascade
   re-keys ~48% of the corpus the first time somebody types in an email address. §5.1.
2. **An organisation id is a surrogate too, with `org_names` and `org_domains` beside it.** v1
   keyed orgs on `slug(company)` — a field `doctor --fix-employers` and the row editor both
   rewrite — and had no bridge at all between the CRM key space and the email-domain one. §5.2.
3. **`upsert_contact` must not refuse a missing Space.** Two live INSERT paths supply none; v1's
   KG-2 would have broken the hot layer and hand-added contacts on the day it shipped. §6.5.
4. **The poller must not start unconditionally.** v1 turned "a lapsed Gmail token defers scheduled
   sends" into "a lapsed token consumes and destroys them". §8.3.

---

## Headline assumptions (read first)

Each states what would falsify it. Where a shipped document and the code disagree, the
disagreement is written down rather than resolved silently.

1. **Eleven people carry every real relationship, and the system treats all 533 the same.**
   Measured on the live database: of 533 contacts, **330 have zero stored messages**, 143 have
   1–3, 52 have 4–10, 6 have 11–30 and 2 have more than 30. The **top ten contacts hold 247 of
   791 messages — 31%**. The product's whole retrieval problem is therefore not scale, it is
   *salience*: an agent asked "what do I know about X" is nearly always being asked about one of
   a few dozen people.
   **v1's falsifier tested the wrong thing and is replaced.** It said the assumption fails "if
   the 1–3 bucket starts producing replies at a rate near the 11+ bucket" — but reply rate is not
   what tiering claims. Tiering claims the *questions* concentrate. **Falsified by
   `knowledge/calls.log`** (§7.6): if the tool calls name people outside the top ~200 more often
   than inside it, tiering is ranking noise and §5/§6 should be replaced by uniform pages.

2. **Person-as-root does NOT require re-keying `contacts` — but it DOES require storing the
   person id.** A deterministic first-non-empty cascade — email → `linkedin_url` → name+company —
   merges 533 contact rows into **530 people**, with exactly **3 groups holding more than one
   row**. `contact_id` is untouched and every satellite table still joins on it.
   **v1 said the person id is derived and never stored. That is wrong, and the code proves it:**
   **258 of 533 contacts have no email address** (82 have neither email nor LinkedIn), and
   `_save_contact_details` (`web_dashboard.py:4339`, `POST /api/contact/details`) exists
   specifically so the operator can type one in. Under a derived id, ~48% of the corpus re-keys
   on one keystroke: the page filename moves, every cross-link in ~530 files repoints, and any
   `person:<hex>` an agent cached goes dead. §5.1 stores a surrogate and resolves keys onto it.
   **Falsified if** the cascade cannot be made safe. Of those 3 groups, one is keyed by NAME
   alone and is **wrong** — `('James', no email, Automattic)` merged with `('James', no email,
   Peak6)`. 48 rows are a first name only, across 47 distinct names. §5.1 constrains the name
   leg; if that constraint cannot hold, the answer is fewer people merged, never a fuzzier rule.

3. **The graph is DERIVED and stores nothing it can recompute.** Full extraction of 1,627 nodes
   and 2,353 edges measures **40ms**; rendering all 183 markdown pages measures **50ms**; a full
   FTS5 index over 771 messages builds in **7ms** and a `MATCH` returns in under 1ms. FTS5 is
   compiled into the installed SQLite (**3.53.1**).
   **Two things are exempt, and both for the same reason: they are not recomputable.** Identity
   (§5.1), organisation naming and domain mappings (§5.2), and operator-typed facts (§5.3) are
   RECORDED. Everything downstream of them — edges, weights, tiers, whose turn it is — is
   recomputed every time.
   **Falsified at a stated threshold** (§8.5): if a full rebuild exceeds **1 second** wall clock,
   or the poller step exceeds **250ms**, or the corpus passes ~10× today's size (≈8,000
   messages / ≈5,000 people), the "recompute everything, cache nothing but files" design has to
   be revisited — that is when incremental invalidation starts paying for its own complexity.

4. **`/api/status` has ZERO headroom, not six statements.** `CLAUDE.md` says "74 SQL statements
   against a budget of 80". Measured today with the suite's own fixture and helper
   (`tests/test_query_budget.py::_seed(jobs=8, contacts_per_job=4)` then `_count_statements`):
   **80**. `MAX_STATEMENTS = 80` and the assertion is `<=`, so the suite passes at exactly the
   ceiling and fails at 81. Worse, that fixture is 8 jobs; measured against a copy of the live
   database the same payload costs **340 statements on `job-search` (43 jobs)** and **847 on
   `linkedin-hiring` (104 jobs)** — roughly 8 statements per job, plus **39 schema statements on
   every request** because the server is HTTP/1.0 thread-per-request and each request gets a
   fresh thread-local connection (382 statements on a cold thread).
   **Consequence, not a preference:** nothing in this document may touch `/api/status`. One
   added SELECT fails the suite immediately. This is a constraint on ONE endpoint, not a ban on
   dashboard surfaces — see §3 and §14.3.
   **Falsified if** a measurement shows otherwise; re-run the two commands in §13.3 before
   believing either number.

5. **Facts are RECORDED, never inferred by a model.** Settled by the operator. Everything on a
   page is either a stored row, a pure function of stored rows, or something the operator typed.
   No LLM writes to `person_context`, no LLM summarises a person, and nothing a model produced is
   stored as though the system knew it. This is §Lessons 34/86 stated as a build rule: a guess
   laundered into a stored fact outranks the truth forever.
   **This assumption is what forces §5.2's `org_domains` table.** "Which company owns
   `swiftfitevents.com`" is not derivable from any string rule (§5.2), so it is recorded or it
   does not exist.
   **Falsified if** the pages are unusable without inference. Test it by reading ten of them
   before wiring any drafter to them — which is why §10 reorders so the first page ships at
   commit five rather than commit eight.

6. **Weight is TWO NUMBERS and they are never summed.** `engaged` (what THEY did) and `invested`
   (what WE did) are separate fields at every layer. Live, **36 contacts have four or more
   outbound touches and zero inbound**; a sum presents four unanswered emails as a strong
   relationship. The codebase has already written this rule down —
   `domain/lastinteraction.py:27-29` says a signal must not be "engagement" in one file and "our
   own action" in another — so `engaged` is imported from `domain/interactions.ENGAGEMENT`
   (`domain/interactions.py:75`), never re-listed.
   **v1 claimed no consumer needs a single ordering. That was false** — every tool in §7 takes a
   `limit`, and 533 candidates cannot be truncated to 10 without a total order. The order is
   **lexicographic on the pair, never a scalar**: `engaged` desc, then `invested` desc, then last
   inbound desc. A zero-engaged person can therefore never outrank a positive-engaged one at any
   invested value. Stated once in §7.0 and tested over the TOOL RESULT, not only the page.
   **Falsified if** a consumer needs the two collapsed into one number to do its job. None does;
   the ordering above is total and needs no sum.

7. **Weight is LIFETIME, and this reverses `crm-prd.md`'s headline.** That document says "the
   unit of value is warmth decay" and "the default view is *going cold* … ranked by decay". On
   this corpus a decay ranking puts **330 people never spoken to** above the **8 with a real
   exchange**. Lifetime weight ranks; recency is a separate, stated line (`last inbound 6d ago`,
   with direction). §Lessons 28 — a ticket is a hypothesis, and this one is refuted by its own
   data.
   **Falsified if** the operator's real question turns out to be "who is going cold" rather than
   "what do I know about this person". **The measurement is `knowledge/calls.log`** (§7.6): if
   `kg_owed` dominates the calls, the decay model was right and this headline is wrong.

8. **The database is already correspondence, and one docstring still denies it.**
   `networking/messages.py:1-6` says "HEADERS ONLY. No bodies, and no snippets… Bodies would make
   it correspondence." The table holds **252 messages over 200 characters, max 1,539, 198,130
   characters of message text**. The CRM-4b narrowing is about *reads* — the poller stores
   snippets, bodies arrive only when the operator names one conversation — and that narrowing is
   intact and must stay intact. But this PRD is judged on what it ADDS, not against a promise the
   code stopped keeping. §13.4 fixes the docstring.

**A note on every number in this document.** They are measured, dated, and they move. The corpus
read **791 messages** at drafting and **793** during the review pass hours later, and §1's table
moved with it — the 1–3 and 4–10 buckets went 143/52 to **142/53** in that window, one contact
crossing a boundary. Treat every count as an order of magnitude and re-run §13.3 before reasoning
from one. Nothing in the design keys on any of them.

---

## 1. Problem

The measured salience distribution is the problem statement:

| stored messages | people | share |
|---|---|---|
| 0 | **330** | 62% |
| 1–3 | 143 | 27% |
| 4–10 | 52 | 10% |
| 11–30 | 6 | 1.1% |
| 30+ | 2 | 0.4% |

**Eleven people carry every real relationship in this system, and nothing anywhere ranks them.**
The dashboard renders 533 contacts as equals inside 206 jobs across 6 Spaces. The two heaviest
relationships have `submitted_at` and `replied_at` both empty, so any ranking read off
`contacts.outreach_status` misses them entirely.

Three questions are unanswerable today, and each is unanswerable for a different structural
reason:

- **"What do I know about this person?"** — `store.contact_id()` hashes `(job_url,
  linkedin_url, name)`, so one human found for two roles is two rows with two histories. CO-2
  built a migration to move them by hand; there is still no read path that says "these rows are
  one person". Live, **exactly one email address appears on two contact rows**
  (`waheed.brown@arm.com`, and it is correct data — two Spaces, two campaigns), so this is a
  *display and retrieval* gap, not a data-corruption one.
- **"Who do I know at $COMPANY?"** — **there is no company node.** `company` is a free string on
  both `jobs` and `contacts`: 193 distinct values on one, 190 on the other, **194 distinct
  overall**. It is already drifting: 8 contact rows say `Peak6` while their job row says
  `Apex Fintech Solutions`, and 1 says `Ycombinator` against `Hamming AI`. `companies_match()`
  returns **False** for both pairs — correctly, since one is an acquisition and the other is
  §Lessons 20's "YC hosts for others".
- **"Where was that said?"** — 791 messages, no index. Grep over `snippet` is the only search,
  and it is not exposed anywhere.

And the graph that would answer them **already exists in the stored data**. Derived, nothing
new fetched: **1,627 nodes and 2,353 edges** — `works_at` 739, `on_thread_with` 874, `member_of`
533, `known_via` 206, `attended` 1. There are **874 person↔person edges sitting in stored mail**
that no surface reads. **77 humans appear on threads and are in `contacts` at all.**

**The email-domain half of that gain needs a RECORDED mapping, and v1 pretended it did not.**
Measured over the live corpus: 194 company strings against **73 distinct mail domains**, of which
**45 companies match a domain's registrable label exactly** (`Amsysis`→`amsysis.com`,
`Acrisure`→`acrisure.com`, `CoStar`→`costar.com`). The rest do not, and the ones that matter most
are among them: **`Swiftfit` is `swiftfitevents.com`**, `Texas Children's Hospital` is
`texaschildrens.org`, `Nerdy` is `varsitytutors.com` (§Lessons 5's live case), `Avathon
Government` is both `avathon.com` and `sparkcognition.com` (§Lessons 34's rename). No string rule
reaches those without becoming a prefix match — §Lessons 1's failure four times over, and
§Lessons 68's Oraclecloud mechanism exactly: a hostname laundered into an employer claim, then
counted as people who work there. So §5.2 records domain↔org in a table, and an unmapped domain
stays its own node.

## 2. Product thesis

**A person is a page, an organisation is a page, and both are pure functions of the database —
so an agent can read them with `cat`, or query them with a tool, and get the same answer.**

What an agent can do afterwards that it cannot do now:

| Question | Today | After |
|---|---|---|
| "What do I know about Yukiko?" | open the dashboard, expand a job, expand a contact, read 7 threads | `cat knowledge/people/<slug>.md` — identity, orgs, both weight numbers, recency with direction, whose turn it is, thread list, recorded facts |
| "Who do I know at Amsysis?" | nothing — no company node | `kg_at_org("amsysis")` → 1 contact + people at mapped domains + people at unmapped domains, **three counts, never merged** |
| "Who could introduce me to X?" | nothing — the 874 edges are unread | `kg_neighbors(person)` → co-participants, ranked by shared threads |
| "Where did they mention the timeline?" | nothing | `kg_search("timeline", person=…)` → message ids, resolved back to people |
| "Who owes whom a reply, everywhere?" | per-job Follow-ups tab; the counter is global and the tab is per job (§Lessons 104) | `kg_owed()` — one list, person-rooted, computed by the same `domain/conversations.conversation_state` the dashboard uses |
| Drafting | each drafter assembles its own context in `networking/outreach.py` | every drafter reads the SAME assembler (§6.4) |

The thesis in one line: **the expensive half is already built and already pure.**
`domain/interactions`, `conversations`, `coverage`, `metrics`, `temperature`, `lastinteraction`,
`company`, `followup`, `timeutil` are all pure functions over rows, measured at **1.2ms**
(metrics over the whole corpus), **1.0ms** (`for_contact` × 533), **4.8ms**
(`conversation_state` × 203). What is missing is an identity function, a company node, and a
place to put the answer.

## 3. Non-goals

Each considered and rejected, with the trigger to revisit.

- **No graph database.** Neo4j, SQLite graph extensions, RDF — none. The graph is 1,627 nodes
  and extracts in 40ms from tables that already exist. Revisit at §8.5's threshold.
- **No vector store yet.** FTS5 is compiled in, builds the whole index in **7ms** and matches in
  under 1ms. Semantic search buys recall this corpus does not need; an embedding store buys a
  model dependency, a second index to invalidate, and a second copy of correspondence on disk.
  **Trigger to revisit:** a real question that keyword search demonstrably fails — write it down
  when it happens, do not pre-build.
- **No LLM-inferred facts.** Settled (Headline 5). No model summarises a person, infers a
  relationship, guesses an employer, or writes to `person_context` or `org_domains`. The one
  place inference is permitted is org-from-email-domain, and it is reported as a **separate
  count**, defaults OFF (§7.2), and never folds into a named org's membership unless a domain
  mapping was RECORDED — "in your CRM", "seen in your mail at a domain we mapped" and "seen in
  your mail at a domain nobody has mapped" are three different kinds of fact (§Lessons 86, 91).
- **No re-keying of `contacts`.** `contact_id` stays exactly as it is. Every satellite table —
  `messages`, `touches`, `sequences`, `interactions`, `transcript_contacts` — keys on
  `contact_id`, and re-keying detaches all of them. The person id is a NEW surrogate; see §5.1.
- **No merge UI, no auto-merge.** Two contact rows that resolve to one person are *displayed*
  as one person. `networking/migrate.py` already exists for a real move and is operator-driven,
  non-destructive and undoable. A wrong merge is worse than a duplicate (`crm-prd.md` §8).
- **No new Gmail reads.** The assembler reads SQLite only. It must not import `gmail_read` or
  `replies.fetch_thread_text`. Regenerating pages must never be a reason to touch a mailbox.
- **No writes from the retrieval surface.** The MCP server SERVES `mode=ro`; the refusal is
  enforced by SQLite, not by discipline. A second send path is §Lessons 49 aimed at the least
  reversible action in the app. **One exception, and it is a start-up exception rather than a
  serving one:** `kg-serve` opens the database read-WRITE once to run `init_db()`, closes it, and
  opens `mode=ro` to serve from — see §7.7 and the measurement that forces it.
- **Not `HIST-1`.** The archive lens crawls mail we do **not** hold; this indexes the 791
  messages we do. Merging them would break HIST-1's own falsifier (`test_the_crawl_writes_
  nothing`). They share exactly one artifact, and it is named in §6.1.
- **Not multi-identity.** One sending identity. `identities` holds 1 row and is read by nothing;
  `messages` has no account column, so identity attribution of stored mail is out of scope even
  after ID-1. See §13.2 for the forward dependency.
- **Nothing on `/api/status`.** v1 said "no dashboard UI", which is a larger and more expensive
  claim than the measurement supports, and which §13.1 then contradicted in its own mitigation
  column. The real constraint is Headline 4: the 2.5-second refresh path is at 80/80 and must not
  be touched. Two dashboard surfaces ARE in scope because neither rides that path: **the write
  door for `person_context`** (§5.3 — a table with no writer is `identities` repeated, §14.4) and
  **the regeneration status line** (§8.3 — a named failure nobody renders is `network_note`,
  §Lessons 15). Both are request-scoped, on their own connection, with a test that the budgeted
  payload is unchanged. A third — linking the search box to a person page — is deferred with a
  named trigger in §13.2 q5.

## 4. Headline assumptions — see the block above

Stated first, deliberately, in the house style. §13.1 carries the risks, §13.2 the open questions,
and §14 the reviewer findings where the design stands rather than moves.

## 5. Data model

**Six new tables, up from v1's two.** The four extra exist because v1 derived two ids from mutable
columns, and this codebase rewrites both of those columns on purpose.

### 5.1 Identity — a stored surrogate, resolved by a derived cascade

**The cascade is derived. The id it produces is not.**

```sql
CREATE TABLE IF NOT EXISTS people (
    id          TEXT PRIMARY KEY,   -- opaque surrogate: 'p_' + 12 hex, minted once, never moves
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS person_keys (
    key_kind    TEXT NOT NULL,      -- 'email' | 'linkedin' | 'namecompany'
    key_value   TEXT NOT NULL,      -- normalised
    person_id   TEXT NOT NULL,
    source      TEXT NOT NULL,      -- 'resolver' | 'operator'
    created_at  TEXT,
    PRIMARY KEY (key_kind, key_value)
);
CREATE INDEX IF NOT EXISTS idx_pk_person ON person_keys(person_id);   -- AFTER the column pass
```

**Why v1 was wrong.** It derived the id as `sha256` of the first non-empty of email → linkedin →
name+company, and stated the consequence in its own table — *"a person id MOVES when someone gains
an email address"* — while §12 simultaneously required
`test_adding_an_email_does_not_move_the_page`. Those two sentences cannot both hold. And it is not
a corner case: **258 of 533 contacts have no email**, `POST /api/contact/details`
(`web_dashboard.py:4339`) exists so the operator can type one in, and its own docstring records
that 85 imported people had no address and no way to fix it. Roughly half the corpus is one
keystroke from re-keying, which renames the file, repoints every cross-link in ~530 pages, and
kills any `person:<hex>` an agent held.

v1's argument against storing an id — `doctor --fix-employers` legitimately rewrites the fields an
id derives from (§Lessons 86) — is **correct, and is an argument against DERIVING it.** It proves
the cascade output is unstable, not that storage is wrong. What must never be stored is a
*computed answer* (a weight, an edge, a tier). An *identity* is not computed; it is asserted once
and then referred to.

**Resolution, and it is the whole algorithm:**

1. Build the candidate key for a contact row: `lower(email)`, else `norm(linkedin_url)`, else
   `norm(name)+norm(company)` — **first non-empty, never transitive.**
2. Look it up in `person_keys`. Hit → that `person_id`.
3. Miss → mint a new `people` row and INSERT the key.
4. **A NEW key on a person already resolved is INSERTed against the EXISTING id.** This is the
   whole point: typing in an email adds a key, it does not mint a person.

**Two `contacts` rows resolving to one person is a JOIN, not a merge.** Nothing in `contacts`
changes; `person_keys` simply points two keys at one `people` row.

**The name leg is constrained, and this is the one place the design can still be wrong.**
Measured: a cascade that unions on ANY matching key (rather than first-non-empty) yields **511
people** and merges four different Alexes into one node, merges `erica.blum@betterup.co` with
`ecoulter@salesforce.com`, and merges `marcus.stefanide@webai.com` with `marcus.godin@webai.com`
— **6 resolved people would hold more than one distinct email address**. So:

- the cascade is **first non-empty, never transitive**;
- the name key is `norm(name) + norm(company)`, never a bare name — the live false merge is
  `James`/Automattic with `James`/Peak6, and a bare-name key is what produces it;
- **invariant, enforced by the schema rather than by discipline:** `person_keys` is PRIMARY KEY on
  `(key_kind, key_value)`, so one email points at exactly one person. The reciprocal — no person
  holds two different emails — stays a test.

The accepted cost is stated rather than discovered: a row with an email and a row with only that
person's name do **not** merge. 82 rows carry neither identifier; 48 are a first name alone.
Those stay separate, and that is the correct failure direction.

**What is still derived, and why:**

| Derived | Rule | Why not a column |
|---|---|---|
| **every edge** | recomputed from `messages`, `contacts`, `jobs`, `touches`, `interactions` | 40ms for the whole graph. A stored edge table is a cache that can disagree with its source. |
| **both weight numbers** | §5.4 | Recomputing costs nothing; a stored score goes stale on the next reply and there is no invalidation hook that fires on the poller's writes. |
| **whose turn it is** | `domain.conversations.conversation_state` | A second implementation of "who owes whom" is §Lessons 21 aimed at the field that decides whether anything gets sent. |
| **tier** | §5.4 — and it is an argument to the RENDERER, never a filter inside the assembler | §14.6 |

**No FOREIGN KEY, on any of the six new tables.** There is **not one foreign key anywhere in this
database** and `PRAGMA foreign_keys` is never enabled, so a declared FK would be decoration
reading as a guarantee. Referential integrity is `kg-check`'s job, and it reports rather than
enforces.

### 5.2 Organisations — a surrogate, its names, and its domains (`m005`)

v1 keyed this table on `slug(company)`. **`company` is a field this application rewrites on
purpose, in two places:** `repo/jobs.py:646` `EDITABLE_FIELDS` includes `company` (double-click on
the row; CO-1's solo band makes the company name the editable element), and `doctor
--fix-employers` writes corrected employers back to `jobs.company` — `CLAUDE.md` records 8 rows
already corrected that way. Rename Peak6 → Apex Fintech Solutions — **the exact drift §1 uses as
motivation** — and under v1 every row resolves to `org:apex-fintech-solutions`, which does not
exist, while the operator's notes sit on an orphaned `org:peak6` that the `INSERT OR IGNORE`
backfill never repairs, and §8.2's full overwrite does not delete the stale page either.

```sql
CREATE TABLE IF NOT EXISTS organizations (
    id          TEXT PRIMARY KEY,   -- opaque surrogate: 'o_' + 12 hex. NOT slug(company).
    name        TEXT NOT NULL,      -- the display name, operator-editable
    notes       TEXT,               -- operator-typed, e.g. the SHEET-1 About column's home
    created_at  TEXT,
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS org_names (
    name_slug   TEXT PRIMARY KEY,   -- domain.target.slug(company); 194 distinct today
    org_id      TEXT NOT NULL,
    source      TEXT NOT NULL,      -- 'backfill' | 'operator'
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS org_domains (
    domain      TEXT PRIMARY KEY,   -- registrable domain, lowercased
    org_id      TEXT NOT NULL,
    source      TEXT NOT NULL,      -- 'operator' | 'corroborated'. NEVER 'guess'.
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_org_names_org   ON org_names(org_id);     -- AFTER the column pass
CREATE INDEX IF NOT EXISTS idx_org_domains_org ON org_domains(org_id);   -- AFTER the column pass
```

**A rename INSERTs a name row; it never mints an org.** `Peak6` and `Apex Fintech Solutions`
become two `org_names` rows pointing at one `organizations.id` the moment the operator says so —
which is what v1's `aliases` JSON column was gesturing at without a resolution order or a writer.
Until they say so, two names are two orgs (record, never infer — Headline 5).

**`org_domains` is the bridge v1 did not have, and the measurement says no string rule can replace
it.** Over the live corpus: 194 company strings, **73 mail domains, 45 exact
slug↔registrable-label matches**. The misses are the interesting half —

| company | live mail domain | why no rule reaches it |
|---|---|---|
| `Swiftfit` | `swiftfitevents.com` | a suffix nobody can enumerate |
| `Texas Children's Hospital` | `texaschildrens.org`, `texaschildrenspeople.org` | punctuation loss plus a second brand domain |
| `Nerdy` | `varsitytutors.com` | a different brand entirely (§Lessons 5) |
| `Avathon Government` | `avathon.com`, `sparkcognition.com` | one company, two unrelated names (§Lessons 34) |

Anything loose enough to join `swiftfitevents.com` to `swiftfit` is a prefix match — §Lessons 1
four times over, and §Lessons 68's Oraclecloud mechanism, where a guessed employer domain returned
five City-of-Atlanta employees for a Texas Children's job and four of them were emailed.

**So a domain with no `org_domains` row is its own node** — `org:domain:<host>`, returned by
`kg_at_org` under its own count and never folded into a named org (§7.2). Populating the table is
an operator act with an audit to propose from: **`applypilot kg-check --org-domains` proposes**
exact matches and same-registrable-domain groupings and **prints them for confirmation**; nothing
is written without one. The corpus already holds the corroboration a proposal needs — contacts at
`varsitytutors.com` whose company string is `Nerdy` is precisely the case the mail headers get
right and the CRM gets wrong.

**Registrable-domain collapse is a separate, mechanical normalisation and is NOT an alias:**
`slac.stanford.edu` → `stanford.edu`; `uk.ey.com` and `ca.ey.com` → `ey.com`. All three pairs are
live. It is a public-suffix operation, stated and tested on its own. What it does NOT cover, and
what must therefore be a recorded `org_domains` row: `pushthebutton.net` +
`pushthebutton.onmicrosoft.com`, `texaschildrens.org` + `texaschildrenspeople.org`, `avathon.com`
+ `sparkcognition.com`.

**Four decisions on the migration itself:**

- **A numbered migration, because it backfills.** The discriminator is written in the migrations'
  own docstrings: `m003` exists for "the one thing the dicts genuinely cannot express: creating
  two tables and putting a row in each"; `m004` says "no seed. Nothing gates on these tables
  being non-empty". This backfills 194 `organizations` + 194 `org_names` rows, so it earns version
  5. The live `schema_migrations` tops out at `4|m004_transcripts|done`, and the discoverer parses
  `int(name[1:4])`, so the file is literally `m005_organizations.py`.
- **Backfill is `INSERT OR IGNORE`, never `OR REPLACE`.** Migrations get re-run — a failed one is
  retried and a crashed claim is reclaimed after the 300s lease — and `OR REPLACE` would revert
  an operator's rename. Source: `SELECT company FROM jobs UNION SELECT company FROM contacts`,
  slugs computed in Python inside `up()`. Coverage is 100%: **zero rows on either table have an
  empty company**. `org_domains` is backfilled with **nothing at all** — every row in it is a
  recorded fact.
- **NO `space_id`.** Five slugs (`hijack-poker`, `nerdy`, `superbuilders`, `nvidia`, `streetfc`)
  already appear in two Spaces. This mirrors `ats_accounts`, which is deliberately Space-agnostic:
  **scope the PANEL, never the registry** (§Lessons 70).
- **The migration duplicates its schema constants as of version 5**, exactly as `m002` and `m003`
  do. Importing the live column dicts would mean re-running m005 in a year rebuilds the tables
  with that year's columns and silently changes what version 5 meant.

### 5.3 `person_context` — keyed on the PERSON, and it ships with its door

```sql
CREATE TABLE IF NOT EXISTS person_context (
    id          TEXT PRIMARY KEY,   -- sha256(person_id|kind|value)  -> idempotent re-entry
    person_id   TEXT NOT NULL,      -- the §5.1 surrogate, NOT contact_id
    kind        TEXT NOT NULL,      -- 'met' | 'intro_by' | 'prefers' | 'role' | 'note' | …
    value       TEXT NOT NULL,
    at          TEXT,               -- when the fact was true, operator-supplied, may be ''
    source      TEXT NOT NULL,      -- 'operator' only, today. Never 'llm'.
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_pc_person ON person_context(person_id);   -- AFTER the pass
```

**v1 keyed this on `contact_id`, and that is the wrong root for the one table that cannot be
recomputed.** Two live code paths would have stranded or orphaned operator-typed facts:

- **`store.delete_contact`** (`store.py:998`) carries a HAND-WRITTEN tuple —
  `("touches", "sequences", "messages", "interactions", "reply_queue")` — and its docstring argues
  each entry individually. `person_context` would not be in it, so deleting a duplicate row leaves
  an orphaned fact; and the same docstring notes contact ids are reproducible, so a re-minted id
  could silently re-attach somebody else's fact.
- **`networking/migrate.py` has NINE hand-written `UPDATE … SET contact_id` statements** across
  `apply()` and `undo()` (lines 326, 329, 337, 362, 377, 425, 428, 431, 455), plus two more that
  rewrite the `contacts` row itself (320, 423). `person_context` would be in none of them, so a
  CO-2 move leaves the operator's recorded facts on the dead role. §Lessons 49 aimed at the only
  table whose contents cannot be rebuilt.

**Keying on the person id makes both problems disappear rather than adding a twelfth UPDATE.**
Moving a contact row between jobs stops touching facts about the human, because a fact was never
about the row. `delete_contact` still gets attention — deleting the last contact row for a person
must not leave a fact nothing can reach — but as a *reachability finding in `kg-check`*, not a
cascade delete: an operator's typed fact outliving a deleted duplicate row is the safe direction.

**It seeds nothing and backfills nothing (0 rows exist today — verified), so it gets a
module-owned `init_person_context()`** copied literally from `networking/reply_queue.py:69-86` —
the precedent for a brand-new table with no seed, alongside `messages`, `interactions`,
`connections` and `ats_accounts`, none of which spent a version number. `people` and `person_keys`
follow the same rule for the same reason: they are populated by the resolver at read time, not by
a backfill.

**`source` exists so an inferred fact is impossible to file as a recorded one.** A non-`operator`
value is refused at the write path. **`kind` is an open TEXT column**, following
`interactions.kind` — a new kind of fact is a prompt block, not a migration.

**THE DOOR SHIPS IN THE SAME COMMIT AS THE TABLE.** `identities` has existed and been read by
nothing for weeks and `CLAUDE.md` flags it twice; a table WRITTEN by nothing is the same failure
with the arrow reversed, and decision 7 (operator-typed facts live in the database, never in the
disposable file) rests entirely on this table being reachable. v1 specified `init_person_context()`
plus a source refusal and no caller: §3 forbade a dashboard UI, §3 forbade writes from the MCP
surface, and §9's CLI was build/check/serve. **Nothing could have put a row in it.** So:

- `POST /api/person-fact` — off the 2.5s path, same shape and connection discipline as the
  existing `/api/contact/details`, which is the precedent for a request-scoped contact write;
- a box on the contact card **where `noticed` already lives**, because that is where the operator
  already types this kind of thing, and §Lessons 89 has fired three times on controls that were
  findable but not beside the work.

`test_a_recorded_fact_reaches_a_page` drives the endpoint and then reads the rendered page — the
whole round trip, never the table alone.

**Two note stores already exist and this is a third — stated, not silently added.**
`contacts.notes` holds 7 non-empty rows and is scratch; `contacts.noticed` holds 2 and **already
reaches the drafting prompts** (`networking/outreach.py:500`). Decision: **`noticed` and `notes`
stay authoritative for their two kinds and are PROJECTED by the assembler, not copied.** One
fact, one store, one reader. Migrating them into `person_context` is a separate, later decision
with its own prompt-path change; doing it here would give the drafter two places to read
`noticed` from, which is §Lessons 21.

### 5.4 The two weight numbers

```
engaged  = sum(interactions.WEIGHT[k]) over THEIR actions
           — replied 4, booked 5, linkedin_in 4, profile_view 3, deck 2
invested = outbound messages + sent touches + the first email + linkedin invite + sms + call
```

`engaged` is `domain.interactions.ENGAGEMENT` (`interactions.py:75`), **imported not re-listed** —
three modules already agree on that line (`temperature.py:13`, `lastinteraction.py:27-29`,
`interactions.py:75`) and a fourth definition is how a signal becomes "engagement" in one file and
"our own action" in another.

`invested` may **not** be computed from `interactions.for_contact`: that timeline does not see
`touches`, and **314 sent touches exist** (email 297, linkedin 12, sms 5) that it would silently
miss. It comes from `contacts` + `touches` + `messages(direction='out')`.

**Lifetime, no decay** (Headline 7). Recency is a separate stated line carrying its DIRECTION —
"they replied 6 days ago" and "you emailed them 6 days ago" are the same age and opposite
situations (`domain/lastinteraction.py`).

**Ordering is lexicographic on the pair** (Headline 6): `engaged` desc, `invested` desc, last
inbound desc. Total, needs no sum, and makes it impossible for four unanswered emails to outrank
one real reply. Specified once in §7.0 and used by every tool that takes a `limit`.

**Tiers are a rule, not a population — and the tier is a RENDERER argument.** Measured today: **8**
people have >10 stored messages, **195** have 1–10, **330** have none. The brief's ~11/180/340
came from an earlier snapshot; the counts move weekly and must never be asserted.

| tier | rule | today |
|---|---|---|
| **full page** | `engaged > 0` **and** ≥ 11 stored messages | 8 |
| **brief page** | any stored message, or any outbound touch | 195 |
| **one line in `INDEX.md`** | everyone else | 330 |

**`dossier.person()` always returns everything.** Tiering happens in the page writer, never inside
the assembler — otherwise a drafter writing to a brief-tier person silently gets less context than
one writing to a full-tier person, with nothing saying so, and §13.5's criterion ("a drafter and a
filesystem agent produce the same facts") is false by construction. **Every rendered page states
its tier and what it omitted** — *"brief page · 6 threads, 3 not listed"* — the same way SHEET-3's
coverage panel states what a sheet does not carry instead of reporting a clean success
(§Lessons 98). A page can genuinely get thinner between rebuilds (a CO-2 collision destroys the
emptier half; `delete_contact` wipes a row's messages; a deck open is dismissible via
`deck_hits.dismiss`, §Lessons 64), and a thinner page must never be indistinguishable from a
person about whom less is known.

Tiering is **not** the privacy control (§11.3).

## 6. Architecture — identity → assembler → three projections

```
  SQLite (read)                    domain/  (PURE)                         projections
  ─────────────                    ──────────────                          ───────────
  contacts   ┐                 ┌ identity.resolve()  ─┐              ┌─ 1. markdown files
  messages   ├─ repo/knowledge ┤ weight.of()          ├─ dossier ────┼─ 2. kg_* MCP tools
  touches    │   (SQL + env)   │ graph.edges()        │  .person()   └─ 3. drafter prompt blocks
  jobs       │                 └ conversations.*      │  .org()
  interactions                                        │  .index()
  people · person_keys · person_context ──────────────┘
  organizations · org_names · org_domains ────────────┘
```

`identity.resolve()` is pure: rows in, groupings out. **Minting a `people` row is a WRITE and
lives in `repo/knowledge.py`**, not in `domain/`. The domain function answers "which of these rows
are the same person"; the repo answers "and what is that person's id".

### 6.1 The purity boundary, and the guard that does not exist yet

`domain/` is genuinely pure today: an AST scan of all 29 modules finds exactly one out-of-package
import, `from applypilot import settings`, in 5 files. No `sqlite3`, no `httpx`, no `networking`,
no `web_dashboard`.

**Nothing enforces that package-wide.** There are exactly two purity tests and both are
per-module: an AST check for `domain/sheet.py` and a source-substring scan for
`domain/temperature.py` — which a `from applypilot.networking import x` would sail straight past.
A new `domain/dossier.py` would be covered by neither. **`KG-0` ships the package-wide AST guard
before any of this is written** (§10).

So: `domain/identity.py`, `domain/weight.py`, `domain/graph.py`, `domain/dossier.py` are pure —
stdlib, other `domain` modules, and `applypilot.settings`, nothing else. They take rows and return
dicts and strings. `repo/knowledge.py` owns every SELECT and the surrogate-minting INSERTs, calls
`config.load_env(strict=False)` as its first statement, and resolves `me`. The file writer lives
in `cli.py`.

`domain/burned.burned_block` and `domain/transcript.prompt_block` are the precedent for a pure
domain module returning formatted prompt text; `dossier` returns markdown the same way.

**Shared with HIST-1, once:** `domain/archive.classify(address, headers) → person|robot|self` is
specified in HIST-1:152-175 and is exactly the filter the 77 non-contact thread participants need.
Whichever ships first writes it; both call it. Say so in both documents.

### 6.2 Three preconditions the assembler must not discover at runtime

- **`me` is a parameter, typed `str | list[str]`.** `_our_addresses()` lives in `networking`, so
  `domain/` cannot call it — and it is wrong by one address unless `load_env()` runs first. Live:
  without `config.load_env(strict=False)` it returns one address; with it, two — and **70 of 791
  stored messages come from the second one**. A CLI entry point that forgets this re-derives 8.9%
  of the corpus as inbound, which is the exact inversion `doctor --directions` already had to be
  guarded against (§Lessons 102). The new function joins the parametrised signature guard at
  `tests/test_my_addresses.py:131`.
- **`contact['interactions']` must be stamped before `temperature()` or `last_interaction()` is
  called.** Both read a key that is not a column; it is stamped only by the dashboard at
  `web_dashboard.py:3192`. Measured over the live corpus, WITH the key vs WITHOUT:
  temperature `warm` goes **2 → 0** (the band vanishes entirely), and last-interaction kinds
  collapse from `{connected 22, replied 8, sent 4, applied 4, deck 3, sms 3}` to
  `{applied 34, sms 7}` — **34 jobs would report "You applied" when the last real event was a
  reply or a deck open**. The loader is the only entry point and stamps it, mirroring
  `followup.normalize_for_ladder`.
- **Every per-channel loop derives from `followup.CHANNELS`.** Three hand-written
  `("email","linkedin","sms")` tuples in `domain/` (`temperature.py:129`, `temperature.py:145`,
  `lastinteraction.py:91`, plus `web_dashboard.py:1750`) still omit the `call` channel shipped
  2026-08-14. It costs nothing today only because zero calls have been logged — **17 contacts
  carry a phone number**. Fixed as part of `KG-5`.

### 6.3 One measured performance rule

`coverage.contact_coverage` costs **86.2ms over 533 contacts**, and essentially all of it is
`settings.resolve()`, which is **not memoised** at 0.021ms a call and is invoked ~9 times per
contact. Everything else in `domain/` is under 5ms over the whole corpus. **Hoist the channel
schedules once, outside the contact loop.** Nested inside a jobs × contacts loop this alone
multiplies into seconds.

### 6.4 Projection 3 is an EXTRACTION, not an addition — and it needs a SECOND golden file

The context assembly the operator wants already exists — and every piece is on the wrong side of
the boundary, in `networking/outreach.py`: `conversation_transcript` (:1602), `_transcript_events`
(:1710), `_turn_block` (:1822), `_turn_task` (:1863), `_met_block` (:696), `_known_block` (:721),
`_premise_block` (:671), `_voice_block` (:609), `job_facts` (:1572). `_met_block` even does a lazy
DB read inside a prompt builder (`transcripts.for_contact`).

These **move** into `domain/`; they are not called from it. The lever is §Lessons 74: refactor
onto the frozen artifact first, which *proves* the extraction rather than assuming it.

**But `tests/golden/jobs_outreach_prompt.txt` cannot see most of what moves, and v1 called it "the
proof".** Verified: the file is **806 bytes**, pins `draft_email` only (`test_space_applied.py:73`)
and contains **zero** transcript markers. Of the nine functions listed, four —
`conversation_transcript`, `_transcript_events`, `_turn_block`, `_turn_task` — are reached only
from `draft_followup` (:1398), `_draft_reply` (:1924) and the SMS path (:2246), never from
`draft_email`. The two that ARE on the cold-email path render empty against that fixture:
`_met_block` gets no transcripts, `_known_block` returns `""` with no `job_context`. **You could
delete `conversation_transcript` outright and the golden file stays byte-identical** — §Lessons
71's exact tell, an assertion that still passes when the thing under test is emptied, inside the
riskiest commit in the plan.

**So a SECOND golden artifact is frozen before anything moves:**
`tests/golden/followup_reply_prompt.txt`, generated from a fixture carrying a multi-message
thread, sent touches, a transcript row, `noticed` and `job_context` — so every moved function
contributes bytes. The cold-email golden is **necessary and not sufficient**, and KG-11a says so
in its own docstring.

Two cleanups ride along: `_met_block`'s DB read becomes a passed-in list, and the dead second copy
of the clipping rule (`_ENDS_CLEANLY` :1661, `_SIGNOFF_RE` :1669, both referenced zero times since
the function delegates to `cv.is_clipped`) is deleted, so nobody greps their way to the stale one.

### 6.5 Space membership reads the ANCHOR — and the writer RESOLVES rather than refuses

Measured: **32 contact rows disagree with their anchor job's Space**, all wrongly filed as
`job-search` — gauntlet 10, sheet-search 12, partnerships 7, professional-network 3. The cause is
`store.py:224-225`: a caller supplying no Space gets `DEFAULT_SPACE_ID`. The newest offending row
is dated **2026-08-21**. Two functions in one file already read different authorities —
`known_at_company` scopes on `j.space_id`, `all_contacts_for_metrics` on `contacts.space_id`.

**Decision: the anchor wins.** It is inside the contact id and cannot drift. The 32 rows are
repaired by `doctor --spaces` (`KG-2`).

**v1 then said "the writer is changed to refuse rather than default", and that breaks the app on
the day KG-2 ships.** Verified — three live INSERT paths supply no usable Space:

| path | what it passes | outcome under a refusal |
|---|---|---|
| HOT layer, `service.py:1128-1148` | no `space_id` key at all | every LinkedIn-connection contact raises |
| `＋ Add someone by hand` / add-introduced, `web_dashboard.py:2877` | 11 named fields, `space_id` not among them | every hand-added contact raises |
| COLD layer, `service.py:367` | `(job or {}).get("space_id") or ""` | `""` is exactly what today's `if not row.get("space_id")` catches |

`replies.py:45,64,338` pass `{id, job_url, replied_at}` and are safe **only because the row already
exists** — the guard at `store.py:224-225` is INSERT-only. A contact deleted mid-poll turns one of
those into an INSERT, and the refusal then fires inside the unattended poller, where §8.3 records
that failures are swallowed.

**So the writer RESOLVES; it does not refuse.** `upsert_contact` derives `space_id` from the
anchor job row when the caller omits it — the anchor is already authoritative and is already
inside the contact id — and refuses **only when the anchor itself resolves to nothing**, which is
a real error rather than an ordinary omission. KG-2's tests are therefore
`test_the_hot_layer_still_stores_a_contact` and
`test_a_hand_added_contact_inherits_the_jobs_space` **first**, and
`test_a_contact_whose_anchor_does_not_exist_is_refused` second. A refusal test alone would have
shipped green over a broken hot layer.

Absorbing the drift into the graph would mean `member_of` is wrong for 32 people and invisible,
because a read filter hides it (§Lessons 70).

## 7. Retrieval contract

Three question shapes, and every tool answers exactly one of them.

**Ids are prefix-typed so an agent can tell what it is holding:** `person:<12hex>`,
`org:<12hex>`, `org:domain:<host>`, `job:<12hex of anchor>`, `thread:<gmail thread id>`,
`message:<message_id>:<contact_id>`, `space:<id>`. Every result also carries `page` — the path of
the markdown file — so the MCP agent and the filesystem agent land on the same artifact.

Tool names are `kg_*` to match the ticket prefix. Everything served is read-only; the serving
connection is `file:…?mode=ro`, which refuses a write at the SQLite layer.

### 7.0 One ordering rule, stated once

Every tool that takes a `limit` sorts by **`engaged` desc → `invested` desc → last inbound desc**,
and reports `truncated`. This replaces v1's claim that no consumer needs an ordering: three of the
five tools take a `limit`, and `kg_at_org` can face 533 candidates.

Lexicographic, never a scalar — a zero-engaged person cannot precede a positive-engaged one at any
invested value. **`test_a_zero_engaged_person_never_precedes_a_positive_engaged_one_at_any_
invested_value` runs over the TOOL RESULT**, because §12's page parse cannot see a ranking that is
never rendered on a page — which is precisely where a sum would otherwise have been written, with
no test in front of it.

### 7.1 "Who is this / what do I know about them?"

```
kg_find(query: str, kind: str|null = null, limit: int = 10)
  -> {results:[{id, kind, name, headline, weight:{engaged,invested}, page}], truncated}
```
The resolver that turns a human string into an id. Nothing else accepts a name.

```
kg_person(id: str, include: string[] = ["facts","threads"], since: str|null = null)
  -> {id, name, page, tier, omitted:{threads, messages},
      identity:{emails[], linkedin, contact_ids[]},
      orgs:[{id, name, title, current}],
      weight:{engaged, invested},                    -- never a third combined field
      recency:{last_inbound, last_outbound, direction},
      owed:{state, hours, thread_id}|null,
      facts:[{id, kind, text, source, at}],
      threads:[{id, subject, n, first, last}],
      neighbors_top:[person ids]}
```
`contact_ids` is load-bearing: it maps the merged person back to the underlying rows so a drafter
or the dashboard can act on **one** of them. `tier` and `omitted` are on the wire for the same
reason they are on the page (§5.4): a thinner answer must never look like a smaller life.

### 7.2 "Who do I know at X / who connects me to Y?"

```
kg_neighbors(id: str, via: string[]|null = null, min_shared: int = 1, limit: int = 25)
  -> {id, neighbors:[{id, name, via:[edge kinds], strength, shared:[thread ids], page}]}

kg_at_org(org: str, include_inferred: bool = false, limit: int = 50)
  -> {org:{id, name, names[], domains[]},
      people:[{id, name, title, in_contacts, weight, page}],
      counts:{contacts, inferred_mapped, inferred_unmapped}}
```

**THREE counts, not two, and that is the fix for v1's missing bridge.**

| count | means | example |
|---|---|---|
| `contacts` | in your CRM, resolved through `org_names` | the 1 Amsysis contact |
| `inferred_mapped` | seen in stored mail at a domain **recorded** in `org_domains` | the 8 at `amsysis.com`, once that mapping is recorded |
| `inferred_unmapped` | seen at a domain nobody has mapped — returned as `org:domain:<host>`, never folded in | the 10 at `swiftfitevents.com` before anybody records it |

**`include_inferred` defaults to FALSE** and the counts are always reported separately. Merging
them makes an inference read as stored data, which is §Lessons 86's mechanism and §Lessons 91's
separation of skips from rejections.

**Three MECHANICAL filters on the inferred side**, each with a different fix and each live:
- free-mail domains yield no org — **32 of the uncaptured addresses are `@gmail.com`**;
- **the operator's own addresses are excluded entirely** — 2 of the 3 gmail senders absent from
  `contacts` are `jorgealejandrodiezm@` and `alediez2408@`;
- a mail-security relay is not an employer — `mxa.us.inbound.cf-emailsecurity.net` is live.

**Registrable-domain collapse is a fourth, separate step, and it is a NORMALISATION rather than a
filter.** `slac.stanford.edu` → `stanford.edu`; `uk.ey.com`/`ca.ey.com` → `ey.com`. v1 listed
`pushthebutton.net` + `pushthebutton.onmicrosoft.com` as a filter bullet with no mechanism, and
§12 then folded it into a parametrized test with the other three under "a shared assertion" —
which cannot hold, since its expected outcome is one node rather than zero. That pair, plus
`texaschildrens.org`/`texaschildrenspeople.org` and `avathon.com`/`sparkcognition.com`, are
**recorded `org_domains` rows**. Two unmapped domains stay two nodes until the operator says
otherwise. Four tests: three "yields nothing", one "yields one node".

### 7.3 "Where was that said?"

```
kg_search(q: str, kind: str = "message", person: str|null = null,
          org: str|null = null, limit: int = 20)
  -> {hits:[{id, kind, person_id, thread_id, at, snippet, page}], n, truncated}
```
**Every hit must carry `person_id` or the result is a dead end.** Snippets only — the stored
snippet, never a fetched body. The id is `message:<message_id>:<contact_id>` because that is the
real primary key; §8.4 explains why the rowid is not.

### 7.4 The fourth shape, because it is the one the dashboard cannot answer

```
kg_owed(scope: str = "all", space: str|null = null, limit: int = 30)
  -> {owed:[{person_id, name, thread_id, state, hours, last_inbound_at,
             contact_ids[], job_id, page}],
      counts:{awaiting_us, stalled}}
```
It **calls `domain.conversations.conversation_state`** and `domain/coverage`, never a second
implementation. The counter is global and the Follow-ups tab is per job (§Lessons 104); this is
the first surface that is person-rooted, which is exactly the gap that report described.

### 7.5 The filesystem projection

```
~/.applypilot/knowledge/
  INDEX.md              one line per person — name, id, org, engaged/invested, path
  people/<slug>-<hash>.md
  orgs/<slug>.md
  index.db              the FTS index (§8.4), disposable
  calls.log             one line per tool call (§7.6), disposable
```

`INDEX.md` **is** the one-liner tier: an agent greps it, opens a page, and follows the typed ids
in that page's front matter to sibling files. Add **one** pointer line to `CLAUDE.md` naming
`knowledge/INDEX.md` and nothing else — that file is already 290KB and brain content cannot live
in it.

**Filenames are `<full-name-slug>-<short hash of the PERSON SURROGATE>.md`.** The surrogate is
stable by construction (§5.1), which is what makes `test_adding_an_email_does_not_move_the_page`
satisfiable — under v1's derived id that test could not have been written at all.
`deck.slugify` cannot be reused: it is first-name-only by design and collides on **175 of 533
live rows** (david ×7, alex ×7, chris ×7), and `disambiguate` breaks ties by iteration order, so
filenames would MOVE between rebuilds and every cross-link in 530 files would silently repoint at
a different human. Full-name slugs still collide on 40 rows, which is why the hash is not
optional.

**An org page renders CRM members only, and NAMES the counts it withheld.** `include_inferred` is
a parameter on ONE tool; the markdown page takes no parameters, so whatever the page builder
happens to do becomes the permanent unflagged default for every agent that reads a file rather
than calling a tool — the guard existing at one of its two call sites (§Lessons 49). So the page
carries one line, and it is tested:

> *8 more people appear at mapped domains for this org in your stored mail —
> `kg_at_org("amsysis", include_inferred=true)`. 3 more at unmapped domains.*

`test_an_org_page_names_no_inferred_person` and `test_an_org_page_states_the_withheld_count`.

### 7.6 One line per call, because two headline falsifiers depend on it

`kg-serve` appends one line to `knowledge/calls.log` per call: **timestamp, tool name, id kind,
nothing else.** No names, no query text — consistent with §11.1's rule about what may be
materialised.

This is not telemetry, it is the measurement two headlines already name and neither could
otherwise check: Headline 1's tiering claim ("nearly always one of a few dozen people") and
Headline 7's decay-versus-lifetime question ("the measurement to watch is which tool gets
called"). **A zero-length log after two weeks is the honest answer to whether this got used at
all**, which matters more here than usual: the 8 full-page people are by definition the ones the
operator already knows best, and 62% of the corpus gets one line.

### 7.7 `kg-serve` opens the database twice, and the measurement is why

**A `mode=ro` connection cannot migrate schema.** Verified on this machine against the live
database:

```
CREATE TABLE IF NOT EXISTS contacts (…)   -> OK      (a no-op on an existing table)
ALTER TABLE contacts ADD COLUMN zz_probe  -> OperationalError: attempt to write a readonly database
SELECT * FROM organizations               -> OperationalError: no such table: organizations
```

`organizations` will exist only after `migrations.run_pending` inside `init_db`
(`database.py:199-212`), and `get_connection()` deliberately does not call `init_db` (stated in
`m003_spaces.py:9-13`). So an agent that launches `kg-serve` after a fresh pull — before the
dashboard or any CLI command has ever run — gets `no such table: organizations`. Worse, any future
additive column on `contacts`/`touches`/`messages` makes every `init_*()` helper the tools reach
(e.g. `touches.ladder_states` → `init_touches`) raise on a read-only connection instead of
self-healing the way it does everywhere else in this codebase.

**So `kg-serve` opens read-WRITE once at start-up, calls `init_db()`, closes it, and opens
`mode=ro` to serve from.** The non-goal in §3 stands unchanged: nothing the SERVER answers can
write. Tests: `test_kg_serve_on_a_database_that_has_never_been_migrated` and
`test_the_tools_never_call_an_init_helper_on_the_read_only_connection`.

## 8. Freshness

### 8.1 Computed live, every time
The entire graph, both weight numbers, whose turn it is, tiers, org membership. **40ms.** Nothing
derived is stored, so nothing derived can be stale. The six recorded tables (§5.1–5.3) hold
identity and operator facts, which are not recomputable and therefore not "stale" — they are the
record.

### 8.2 Cached — and the cache is a file you can delete
The markdown pages. **50ms for 183 pages.** Every page carries `built_at`, its tier, its omitted
counts and the source watermark it was built from, rendered visibly at the top rather than hidden
in front matter — a stale page looks exactly like a fresh one otherwise. Regeneration is a **full
overwrite**, so a deleted person's page cannot outlive them.

### 8.3 Where regeneration runs — one place, and the poller is NOT started unconditionally

**A fifth entry on the poller's step tuple** (`web_dashboard.py:1221` — today four entries:
`bookings`, `deck`, `accounts`, `scheduled`), appended **after `scheduled`** so pages reflect that
poll's own writes. Not `applypilot tick`: `schedule.installed()` is False,
`~/Library/LaunchAgents/com.applypilot.tick.plist` does not exist, and `launchctl list` is empty —
a step hung there silently never fires (§Lessons 31).

**v1 said "KG-8 starts the poller unconditionally". That converts a lapsed token from an
inconvenience into destroyed promises, and the code is unambiguous:**

- the poller start is gated at `web_dashboard.py:5481-5485` on `gmail_read.available()`, which is
  False when the token is missing OR when `gmail.metadata` is not granted (`gmail_read.py:44-50`);
- `_poll_scheduled` calls `touches.claim_due_scheduled`, which **NULLs `scheduled_at` in the same
  call, before the send** (`touches.py:452-456`), and whose docstring says *"firing consumes the
  promise whatever the outcome"*;
- `_poll_scheduled_replies` (reached from `_poll_scheduled` at `web_dashboard.py:1307`) calls
  `reply_queue.claim_due`, which **DELETEs the row** (`reply_queue.py:153-173`).

Today, with a lapsed token, nothing is claimed and every promise survives. Start it
unconditionally and every due follow-up and every queued reply is claimed, fails to send, is
logged as a refusal on the card, and **the promise is gone**. That claim-before-send design is
correct (§Lessons 119 — clearing afterwards re-sends the same email every five minutes forever);
what is wrong is firing it into a transport known to be down.

**So the tuple is split by DEPENDENCY, not by Gmail alone:**

| step | starts | gate |
|---|---|---|
| `deck`, `accounts`, `knowledge` | **unconditionally** | none — none of them touch Gmail |
| `bookings` | unconditionally | its own `can_read_content()` check, which it already has |
| `replies` | unconditionally | `gmail_read.available()`, evaluated per poll rather than once at boot |
| `scheduled`, `scheduled_replies` | unconditionally | **a live-transport check at FIRE time**, and `claim_due_scheduled` / `claim_due` **refuse to claim when the transport is unavailable**, so an unsendable promise stays a promise |

`test_a_lapsed_token_leaves_a_scheduled_send_scheduled` ships with KG-8. Patch only the
TRANSPORT — patching `send_followup` itself deletes the guard under test and passes, the mistake
the scheduled-send work already made once.

Two more constraints that fall out of reading that code:

- **`poll_now()` is also reachable from a request thread** — the `📥 Check replies` button calls
  it synchronously (`web_dashboard.py:5427`). So the step is budgeted like a request and gated on
  a cheap watermark so a no-change poll costs one query and writes nothing. **The watermark is NOT
  `max(rowid)`.** v1 proposed that, and rowid only moves on INSERT while most of what changes a
  page is an UPDATE: `contacts.replied_at` / `outreach_status` (`replies.py:45,64`), a touch going
  drafted→sent, `scheduled_at` cleared by `claim_due_scheduled`. `sequences` — which decides
  terminal state and therefore whose turn it is — was not in v1's watermark at all, and neither
  were the recorded tables whose entire purpose is content the operator types. Under `max(rowid)`
  a page whose facts have changed is not regenerated **while `built_at` at the top certifies it
  fresh**, which is worse than the unmarked staleness the visible timestamp was added to prevent.
  The watermark is `PRAGMA data_version` on the connection, plus `max(updated_at)` over
  `contacts`/`touches`/`sequences`/`person_context`/`organizations`. Mutation:
  `test_an_update_with_no_insert_still_regenerates_the_page`, mutated by reverting to
  `max(rowid)`.
- **A failing step is swallowed.** `try: res[name] = fn() except Exception: log.debug(...)` — on
  failure `res[name]` is never assigned, so a regeneration that raises every poll is
  indistinguishable from one that was never wired (§Lessons 15). **v1's mitigation was to leave a
  named failure in the returned dict, "which already rides the payload via `_replies.status()`".
  Verified: `dashboard.js` reads `data.replies` ZERO times** — a grep for `.replies` in that file
  returns nothing at all. That is `network_note` exactly: returned by the server, rendered by no
  JS, which made a completed search indistinguishable from a dead button for weeks. So KG-8
  **renders it**: one line under the `📥 Check replies` button carrying the last regeneration time
  and any named failure, tested by executing the render path and asserting the string reaches the
  DOM — never by grepping the payload.

**Any cross-request memo must be a module-level global.** The server is HTTP/1.0 thread-per-request
and connections are thread-local, so a connection-attached or thread-local memo dies with the
request. `gmail_oauth._EMAIL_CACHE` is the working precedent.

### 8.4 Self-maintaining, keyed on the real primary key, and the trigger design that was REJECTED

The obvious FTS5 design is an external-content table over `messages` with AFTER
INSERT/DELETE/UPDATE triggers. **It desyncs on this app's own write path, and both obvious guards
are green while it is broken.** Reproduced: `messages.upsert_messages` issues
`INSERT OR REPLACE` (`messages.py:160`); after a re-sync of the same `(message_id, contact_id)` the
rowid went 1 → 2, `SELECT count(*) … MATCH '<old term>'` still returned **1** (stale), selecting
the column raised `fts5: missing row 1 from content table`, and
`INSERT INTO fts(fts) VALUES('integrity-check')` returned **OK in that same state**.
`PRAGMA recursive_triggers` is OFF by default and is per-connection; `get_connection()` sets only
WAL and `busy_timeout`.

**So there are no triggers.** The index is a derived artifact **rebuilt in full** at each
regeneration — **7ms for 771 messages**. A stale row is unreachable by construction.

**And it does NOT key on `messages.rowid`.** v1 measured the rowid moving 1 → 2 on a re-sync and
then chose it as the join key anyway. `messages` has a composite TEXT primary key —
`PRIMARY KEY (message_id, contact_id)` (`messages.py:78`) — so its rowid is implicit and unstable.
The index is rebuilt only at regeneration; **the poller writes `messages` continuously in
between**, so between two rebuilds a re-synced or deleted message shifts rowids and `kg_search`
resolves a hit to a DIFFERENT row. Because §7.3 requires every hit to carry a `person_id` derived
from that join, the failure mode is **correspondence attributed to the wrong human** — silently,
and rendering perfectly.

So the FTS table stores `message_id` and `contact_id` as `UNINDEXED` columns and the reader joins
back on those. `test_a_resync_between_rebuilds_never_resolves_a_hit_to_another_message`: index,
then `upsert_messages` a *different* message, then query **without rebuilding**, and assert the hit
still resolves to the original `(message_id, contact_id)` or to nothing. v1's proposed test
rebuilt before asserting and therefore could not see this at all.

It lives in `~/.applypilot/knowledge/index.db`, not in `applypilot.db`, so the main schema gains
nothing, no migration is needed for it, and readers stay `mode=ro`. It is **contentless**
(`content=''`): the FTS table stores the index and **no readable copy of the text**, and the reader
joins back to the main database read-only for the snippet. That keeps 198KB of correspondence from
being duplicated onto disk in a second file. Be honest about what this is not: an FTS index is
derived from text and partially reconstructible; it is a duplication control, not a security
boundary. Mode 0600, same as the pages.

**Readers must open `mode=ro` and must NEVER use `immutable=1`.** `immutable=1` ignores the WAL:
live, the WAL is **7,988,712 bytes against a 3,567,616-byte main file**, and a post-crash repro
gave the correct row under `mode=ro` and `no such table` under `immutable=1` — i.e. on this
database it would serve a snapshot missing more than half the data, silently.

### 8.5 The measured threshold at which this design must be revisited

| signal | today | revisit at |
|---|---|---|
| full graph extraction | 40ms | **> 400ms** |
| all pages rendered | 50ms (183 pages) | **> 500ms** |
| FTS full rebuild | 7ms (771 messages) | **> 250ms** |
| total poller step | ~100ms | **> 250ms**, or any measurable effect on `📥 Check replies` |
| corpus | 533 people / 791 messages | **~5,000 people / ~8,000 messages** |

Past those lines, the honest answers in order are: incremental page invalidation keyed on the
watermark, then trigger-maintained FTS *with the `INSERT OR REPLACE` write path fixed first*, then
— and only with a written-down failing question — a vector index. Not before.

## 9. Tech stack changes

**Net new runtime dependencies: zero.**

| Considered | Verdict |
|---|---|
| Neo4j / any graph store | **No.** 1,627 nodes, 40ms, from tables that exist. |
| A vector store + embedding model | **No** (§3). FTS5 is compiled in (SQLite 3.53.1) and builds in 7ms. |
| The Python `mcp` SDK | **No.** Resolved live at **17 packages**, including `httpx2 2.12.0` — a **second httpx major** alongside the pinned `httpx 0.28.1` — plus `starlette`, `uvicorn`, `sse-starlette`, `python-multipart` (all unconditional `Requires-Dist`, there is no stdio-only extra), `opentelemetry-api`, `jsonschema` and the Rust extension `rpds-py`. `pyproject.toml` has **8** runtime dependencies today. Adding an ASGI stack to serve a **stdio** protocol, in a repo whose own web server is stdlib `http.server`, is not a trade this needs. |
| **Hand-rolled stdio JSON-RPC** | **Yes.** MCP over stdio is newline-delimited JSON-RPC 2.0 on stdin/stdout: `initialize`, `tools/list`, `tools/call`. ~200 lines in `applypilot/kgserve.py`, zero dependencies, and the same choice the dashboard already made for HTTP. |

**Transport is stdio, not HTTP.** The client launches the server as a subprocess, so there is no
listening socket, no port to authenticate, and no second CSRF/Origin surface — the dashboard
needed `_origin_ok`, `_ext_origin_ok` and an hmac token for exactly one such port. It also
preserves the property that this app makes outbound requests and receives none.

### 9.1 Registration is a DELIVERABLE, not a pattern reference

**v1 said registration "reuses the pattern the repo already has as an MCP consumer:
`apply/launcher.py:89`". That is the wrong consumer twice, and nothing would have been
registered.** Verified: **there is no `.mcp.json` anywhere in this repo and no `~/.claude.json`
entry**, and no commit in v1's plan creates one. `_make_mcp_config` is written per apply-run
(`launcher.py:435,:521`), handed to the apply agent with `--strict-mcp-config` (:533) and
`--allowedTools mcp__playwright,mcp__gmail__send_email` (:539). That agent is deliberately denied
`Read`/`Grep`/`Bash` (`launcher.py:110-135`) so a careers-page prompt injection cannot reach
`~/.applypilot` — handing IT a tool returning correspondence, addresses and §11.1's 11
salary/visa messages **reopens the exact door the deny-list exists to close**.

So:

- **`applypilot kg-install`** is a deliverable inside KG-10. It writes an `mcpServers` entry for
  the OPERATOR's session — `.mcp.json` at the repo root or `~/.claude.json`, chosen by a flag:
  `{"applypilot-kg": {"command": ".venv/bin/applypilot", "args": ["kg-serve"]}}`.
- `test_kg_install_writes_a_resolvable_entry` reads the config back and asserts the command
  resolves to an executable. A protocol round-trip over a pipe proves the server speaks JSON-RPC;
  it does not prove anything is connected to it (§Lessons 31).
- **`test_the_apply_agent_config_never_names_the_kg_server`** pins the absence, the same shape as
  the existing `CONTENT_SCOPE not in SCOPES` guard. Stated here in the PRD so nobody later "fixes"
  the apply agent's missing brain access.

**CLI: three commands plus the server, hyphenated-flat.** The CLI is one flat Typer app with
**zero** `add_typer` calls; namespacing is done with hyphens (`deck-hits`, `deck-relink`,
`migrate-touches`). So:

```
applypilot kg-build   [--rebuild] [--dry-run] [--only <slug>]
applypilot kg-check   [--fix-links] [--org-domains]  # stale pages, dangling links, iCloud dupes,
                                                     # unreachable facts, and PROPOSED domain→org
applypilot kg-install [--project | --user]           # register the MCP server
applypilot kg-serve                                  # the MCP stdio entry point
```

`kg-check` pairs an audit with a `--fix`, the shape `doctor --employers`/`--fix-employers`
already sets. **No `kg-find` / `kg-person` CLI commands** — that is the MCP surface, and two
implementations of one answer is the failure this whole document is trying to remove. Every
parameter takes a default: tests call CLI commands as plain Python functions with keyword args and
there is no `CliRunner` anywhere in the suite.

**There is no `--json` convention in this CLI** (zero matches for `--json` or `json.dumps`). The
machine-readable surfaces are the MCP tools and the markdown files; the CLI stays human-facing.
Stated so nobody adds a third.

## 10. Implementation

Numbered, each independently shippable, ordered so nothing breaks.

**The order changed in v2.** v1 put seven invisible commits in front of the first artifact, two of
which — the org migration and the fact table — are not needed to render a page and are *shaped by*
what those pages turn out to be missing. Headline 5's own falsifier is "read ten of them before
wiring any drafter", and fixing `person_context`'s `kind` values against a guess before a single
page exists is the one thing this document is otherwise careful never to do. **The first readable
page now arrives at commit five instead of commit eight.**

| # | Commit | Test that proves it |
|---|---|---|
| **KG-0** | Package-wide `domain/` purity guard (AST over all 29 modules, allowlisting only `applypilot.settings` and `applypilot.domain.*`). Record the real `/api/status` numbers in `CLAUDE.md`: 80/80 on the fixture, 340 / 847 live. | `test_every_domain_module_is_pure` — parametrised, so the next module is covered by default. Mutation: add `import sqlite3` to any domain module. |
| **KG-1** | **Extraction only.** `domain/identity.py` holds the cascade; `people` + `person_keys` + the minting function in `repo/knowledge.py`. Re-point the **read-only** consumers: `messages.threads_by_shared_address`, the card grouping, and the new graph. **`migrate._collision` and `known_at_company` are NOT re-pointed** — §14.2. | `test_the_read_only_callers_agree`; **`test_which_callers_deliberately_disagree_and_why`** pins the two that keep their own keys; `test_a_new_key_attaches_to_an_existing_person`. Mutation: mint a new person for a second key. |
| **KG-5** | `domain/weight.py` — two numbers, lifetime, §7.0's lexicographic ordering; and the `followup.CHANNELS` derivation replacing the four hand-written channel tuples. | §12's summing mutations; `test_a_call_reaches_every_channel_loop`; `test_a_zero_engaged_person_never_precedes_…` over a tool-shaped result. |
| **KG-6** | `domain/graph.py` + `domain/dossier.py` (pure, returns EVERYTHING at every tier) and `repo/knowledge.py` (SQL, `load_env`, `me`, stamps `contact['interactions']`). | `test_raw_rows_give_the_same_answer_as_the_dashboard_payload`, pinned on the measured regression (warm 2 → 0); `test_the_assembler_returns_everything_at_every_tier`. |
| **KG-7** | **Projection 1** — `applypilot kg-build`: pages + `INDEX.md` under `~/.applypilot/knowledge/`, dir 0700, files 0600, tier and omitted counts on every page. **Ships its guards in the same commit**: `knowledge/` in `.gitignore`, a path rule in `.githooks/pre-commit`, an entry in `LEAKS` in `tests/test_secret_guard.py`, and the hook's `':(exclude)*.md'` narrowed to the tracked docs paths. | `test_rebuilding_twice_yields_a_byte_identical_listing`; `test_a_knowledge_page_cannot_be_committed`; `test_the_assembler_makes_zero_gmail_calls`; `test_a_page_states_its_tier_and_what_it_omitted`. |
| — | **⟵ STOP AND READ TEN PAGES.** Headline 5's falsifier. KG-2/3/4 below are shaped by what is missing from them. | — |
| **KG-2** | `doctor --spaces` / `--fix-spaces`: audit and repair the 32 drifted rows; `store.upsert_contact` **resolves** `space_id` from the anchor when omitted, refusing only when the anchor does not resolve. | `test_the_hot_layer_still_stores_a_contact`; `test_a_hand_added_contact_inherits_the_jobs_space`; `test_a_contact_whose_anchor_does_not_exist_is_refused`; the audit reports before it writes and is re-runnable. |
| **KG-3** | `m005_organizations.py` (version 5): `organizations` + `org_names` + `org_domains`, surrogate ids, schema constants duplicated in the migration. Backfill `INSERT OR IGNORE`, 194 orgs + 194 names, **zero domains**. **Indexes after the column pass.** `repo/organizations.py`. `kg-check --org-domains` proposes mappings for confirmation. Add `m005` and the new repo to the SQL-boundary allowlist with a reason line. | m005 run three times and diffed (`test_migrations.py:216`'s shape); `test_fresh_and_fully_migrated_databases_have_identical_schemas`; **`test_a_renamed_company_keeps_its_org_and_its_notes`**; an index test that builds the OLD table shape by hand — a fresh-database test **cannot** see that trap. |
| **KG-4** | `init_person_context()` (module-owned, `reply_queue.py:69-86` pattern), keyed on `person_id`; a write path refusing `source != 'operator'`; **`POST /api/person-fact` and the box on the contact card, in this same commit.** | `test_a_non_operator_source_is_refused`; `test_init_is_idempotent_on_an_old_table_shape`; **`test_a_recorded_fact_reaches_a_page`** — driven through the endpoint, never the table; `test_status_payload_is_unchanged_by_the_person_fact_endpoint`. |
| **KG-8** | Regeneration on the poller: tuple split by dependency (§8.3); `scheduled`/`scheduled_replies` refuse to CLAIM without a live transport; `data_version` + `max(updated_at)` watermark; named failure **rendered** under `📥 Check replies`. | `test_the_poller_regenerates_pages`; `test_tick_alone_does_not_regenerate`; `test_a_lapsed_token_leaves_a_scheduled_send_scheduled` (patch the transport only); `test_a_failing_regeneration_reaches_the_dom`; `test_an_update_with_no_insert_still_regenerates_the_page`. |
| **KG-9** | FTS: `knowledge/index.db`, contentless FTS5, **full rebuild**, no triggers, keyed on `(message_id, contact_id)` UNINDEXED columns. `kg_search` behind it. | `test_a_resync_between_rebuilds_never_resolves_a_hit_to_another_message`; `test_readers_never_use_immutable`. |
| **KG-10** | **Projection 2** — `applypilot kg-serve` (`init_db()` read-write once, then `mode=ro`), stdio JSON-RPC, the five tools of §7, `calls.log`. **`applypilot kg-install` ships here.** | `test_the_serving_connection_refuses_a_write`; `test_kg_serve_on_a_database_that_has_never_been_migrated`; `test_every_search_hit_carries_a_person_id`; `test_kg_install_writes_a_resolvable_entry`; `test_the_apply_agent_config_never_names_the_kg_server`; a protocol round-trip over a pipe. |
| **KG-11a** | **Extraction, alone.** Freeze `tests/golden/followup_reply_prompt.txt` FIRST, then move the `outreach.py` context blocks into `domain/`, drop `_met_block`'s DB read, delete the dead clipping constants. **No behaviour change.** | **Both** golden files byte-identical (§6.4 — the cold-email one is necessary and not sufficient). Nothing else lands in this commit. |
| **KG-11b** | **Projection 3** — feed the drafters from the assembler. | A behavioural test that the assembler's facts reach the prompt, **plus a before/after on real generated drafts for one of the 8 full-page people** — §Lessons 42/118: generating against real data settles in twenty minutes what inspection leaves open for weeks. |
| **KG-12** | Doc amendments (§13.4) — the budget, the `messages.py` docstring, `spaces-prd` §3/§11, `crm-prd` §5/§6, HIST-1's stale duplicate claim, ID-1's name collision. | — |

**KG-0, KG-1 and KG-2 are useful even if the rest is abandoned:** a purity guard, one fewer
duplicate identity rule, and 32 repaired rows. KG-2 moved below the first page deliberately — it is
independent of everything above it and its repair is not a prerequisite for rendering.

## 11. Privacy and security

### 11.1 The rule for what a page may contain

**A page carries DERIVED FACTS and HEADERS. Never message bodies.**

| Allowed | Not allowed |
|---|---|
| name, org, role, both weight numbers, message counts, first/last dates, direction, **subject lines**, thread ranges, application state, deck opens, whose turn it is, tier and omitted counts | quoted message text, snippets, transcript bodies, phone numbers, and the operator's typed notes |

This keeps a page **strictly less sensitive than the row it derives from**, which is the property
that makes decision 7 ("pages are freely disposable") true rather than aspirational. Bodies stay
in SQLite and are referenced by pointer: a page says *"8 messages, last inbound Aug 13, subject:
Re: Applied AI Engineer"*, and the drafter resolves the text at draft time from `messages`, scoped
to one thread, exactly as it does today. Regenerating every page therefore never touches
correspondence. `calls.log` (§7.6) is held to the same rule: tool name and id kind, never a name
and never a query string.

Live, the corpus this protects: **11 messages mention salary / compensation / visa /
sponsorship**, 7 contain a phone-shaped string, 16 subjects are calendar invitations. That is the
content class that makes a materialised page worse than a database row — greppable, human
readable, and it survives being copied out of context.

### 11.2 Pages never live in the repo, and here is the measurement that decides it

The GitHub repo is a **PUBLIC fork** and cannot be made private. Three findings, each verified by
running the real hook:

- **The pre-commit content scan EXCLUDES `*.md`.** A file containing `ya29.a0AfH6SMB…` was
  **allowed (exit 0)** as `brain/people/alex-roy.md` and **blocked (exit 1)** as the byte-identical
  `.txt`. Markdown is precisely the format proposed here.
- **A full page with correspondence, an address and a phone number passes the hook untouched**
  (exit 0). No filename rule and no content rule fires.
- **No candidate page path is gitignored, and the iCloud duplicate is** — `brain/people/yukiko.md`
  is trackable while `brain/people/yukiko 2.md` matches the existing `* [0-9].md` pattern. So
  `git add -A` would commit the real page and silently skip the copy.

`~/Desktop` is iCloud-synced (`~/Library/Mobile Documents/com~apple~CloudDocs/Desktop` exists) and
rapid rewrites there produce `<name> N` duplicates — the mechanism that put `dashboard 2.js` into
a commit and 38 `applypilot` binaries into `.venv/bin`. `~/.applypilot/` is outside it. **Writing
under `APP_DIR` resolves the public fork, the `.md` hook blindness and iCloud sync in one
decision**, and `kg-check` reports any `* [0-9].md` found under the knowledge directory as a hard
finding, because a stale duplicate beside a live page is a wrong fact served to a drafter.

### 11.3 Permissions, and what tiering does not buy

`~/.applypilot` is **0755** and `applypilot.db` is **0644** — world-readable, under umask 022.
`CLAUDE.md`'s "secrets in `~/.applypilot/` are chmod 600" is true of tokens, `.env`,
`profile.json` and `resume.txt`; it is **not** true of the file holding the correspondence, and the
only two `chmod` calls in the source protect tokens. Generated content today (`cover_letters/`,
`tailored_resumes/`) is all 0644.

So the knowledge directory is created **0700** and files written **0600**, explicitly, with a test
— umask differs per machine. This is not a claim of safety: the home directory is 0750 with one
human account, so the real threat model is "anything running as this user" (backup tools,
Spotlight, sync agents, other agents with filesystem read), exactly as `CLAUDE.md` already says.
0600 removes an unnecessary widening; it does not create a boundary.

**Tiering is not a privacy control.** Measured: the ~11 full-page people hold **45,750 characters
of stored message text (23%)**; everyone else holds **152,380 (77%)**. "Only 11 full pages"
protects nothing. §11.1's body/no-body rule applies identically at every tier.

### 11.4 Two people-shaped risks

- **No person page without an outbound relationship.** The 77 uncaptured thread humans include
  32 `@gmail.com` addresses plus `utexas.edu`, `thefireshow.com`, `upstreamliteracy.com` —
  personal correspondence pulled in because `sync_all_with` searches by ADDRESS. A header row is a
  byproduct of threading; a titled page is an artifact ABOUT a person. They render as participant
  names on the counterpart's page, and as graph nodes keyed by address hash in memory — never as a
  file named after a human nobody contacted, and never minted into `people`.
- **The disk page and the PROMPT payload are separate.** Today a drafter sends one scoped thread;
  a page would send an assembled dossier — other people's messages, salary and visa discussion —
  to a third-party LLM, and `FailoverClient` round-robins OpenAI / Anthropic / Gemini, so *which
  company receives it* is non-deterministic. §11.1's rule is what keeps this safe; if a full page
  must ever reach a model, pin the provider for that call and say in the PRD which one sees what.

## 12. Testing strategy — phrased as the mutation each test must fail against

| Test | Mutation it must fail | Vacuity trap (measured) |
|---|---|---|
| **`test_a_resync_between_rebuilds_never_resolves_a_hit_to_another_message`** — index, `upsert_messages` a DIFFERENT message, query **without rebuilding** | keying the FTS index on `messages.rowid` | v1's version rebuilt before asserting and could not see it at all. Separately: `SELECT COUNT(*) … MATCH` returns a stale hit as 1 and never raises, and `INSERT INTO fts(fts) VALUES('integrity-check')` returns **OK** against a provably broken index. All verified. |
| `test_the_index_survives_a_write_from_another_connection` — write via a raw `sqlite3.connect` | a per-connection pragma fix | the fixture connection alone cannot see it: `recursive_triggers` is per-connection |
| `test_two_first_name_only_rows_at_different_companies_stay_two_people` (James/Automattic, James/Peak6) | applying the name leg without the company qualifier | **`assert len(resolve(rows)) == 530` is vacuous** — 530 is exactly what the resolver containing the James bug produces. Never assert the count. |
| `test_no_resolved_person_holds_two_different_emails` (fixture from the live Alex / Erica / Marcus shapes) | switching first-non-empty to union-on-any-key (yields 511 people, 6 bad groups) | a two-row fixture leaves the mutation alive |
| **`test_adding_an_email_does_not_move_the_page`** — resolve a name-only row, mint the person, THEN add an email, re-resolve | minting a second person for the new key; deriving the id from the cascade output rather than storing it | under v1's derived id this test was unwritable — that contradiction is why §5.1 changed |
| **`test_which_callers_deliberately_disagree_and_why`** — pins `migrate._collision` and `known_at_company` on their OWN keys | re-pointing either at the §5.1 cascade | §14.2: a "they all agree now" test pins a behaviour change as if it were a refactor |
| `test_identity_resolution_agrees_with_migrate_plan` — pinned on `waheed.brown@arm.com` | scoping one side by Space and not the other | §Lessons 110: a guard only on the UI path is not a guard |
| **`test_a_renamed_company_keeps_its_org_and_its_notes`** — write a note on `Peak6`, rename to `Apex Fintech Solutions`, re-resolve | keying `organizations` on `slug(company)` | v1's `INSERT OR IGNORE` backfill makes the orphan invisible: the old row still exists and still holds the notes |
| `test_a_high_invested_zero_engaged_person_is_not_ranked_as_a_relationship` — **(engaged 0, invested 6)** vs **(engaged 5, invested 1)**, equal totals, different tiers **and different rendered text** | `score = engaged + invested`, and separately `max()` / `mean()` | the obvious fixture (one at 6, one at 0) leaves a summing mutation **alive**; live data offers only one distinct pair summing to 6, so the fixture must be synthetic and the docstring must say why |
| `test_the_page_never_renders_the_two_as_one_number` — **parse** the emitted page | a combined field added downstream | grepping the source proves where a string is, not what the code does (§Lessons 48) |
| **`test_a_zero_engaged_person_never_precedes_a_positive_engaged_one_at_any_invested_value`** — over the TOOL RESULT | a sum used as the `limit` ordering | the page parse above cannot see it: **a ranking behind `limit` is never rendered on any page**, which is exactly where a sum would have been written |
| `test_weight_is_lifetime_not_decayed` — move `now` a year, both numbers unchanged, the recency LINE still moves | a smuggled recency multiplier | if the recency line does not move, the test proves the wrong thing |
| `test_the_tier_boundaries_are_the_defaults` — call with **no** threshold argument | raising a default while the parametrised override stays correct | §Lessons 100: `prompt_block(rows, limit=2)` stayed green while the default went 2 → 999 |
| **`test_a_page_states_its_tier_and_what_it_omitted`** / **`test_the_assembler_returns_everything_at_every_tier`** | tiering inside `dossier.person()` | a thinner page is indistinguishable from a smaller life; a brief-tier drafter silently getting less context renders perfectly (§Lessons 98) |
| `test_raw_rows_give_the_same_answer_as_the_dashboard_payload` | dropping the `contact['interactions']` stamp | pinned on the measured regression: warm **2 → 0**, kinds collapsing to `{applied 34, sms 7}` |
| `test_a_free_mail_domain_yields_no_org` / `…our_own_addresses_are_never_an_unknown_human` / `…a_relay_is_not_an_employer` — three tests, **expected outcome: no org** | deleting the respective filter | v1 merged these into one parametrize **with a fourth case whose expected outcome is the opposite** (one node, not zero); a shared assertion cannot cover both |
| **`test_a_subdomain_collapses_to_its_registrable_domain`** (`slac.stanford.edu`, `uk.ey.com`) and **`test_two_unmapped_domains_stay_two_nodes`** (`pushthebutton.net` vs `.onmicrosoft.com`, before a mapping is recorded) | folding an unmapped domain into a named org | §Lessons 1/68 — a prefix rule loose enough to join `swiftfitevents.com` to `swiftfit` is the Oraclecloud mechanism |
| **`test_an_org_page_names_no_inferred_person`** / **`test_an_org_page_states_the_withheld_count`** | letting the page builder pick its own default for `include_inferred` | the tool has the parameter and the page does not; whatever the builder does becomes the permanent unflagged default (§Lessons 49) |
| `test_the_org_name_is_not_simply_the_contact_string` — fixture **must** include Ycombinator/Hamming AI | `return contact['company']` | the Peak6/Apex pair alone makes either side defensible; only the YC row has a right answer |
| `test_status_payload_does_not_build_the_graph` + the existing `test_query_count_does_not_grow_with_contacts` + **`test_status_payload_is_unchanged_by_the_person_fact_endpoint`** | importing or calling the assembler from `_status_payload`; adding the new endpoint's query to it | a naive per-person pass measures **1 statement per person** (533 vs a budget of 80); batched it is 2 |
| **`test_the_hot_layer_still_stores_a_contact`** / **`test_a_hand_added_contact_inherits_the_jobs_space`** / `test_a_contact_whose_anchor_does_not_exist_is_refused` | making `upsert_contact` refuse a missing Space | v1 shipped only the refusal test, which is green while two live INSERT paths raise |
| `test_the_poller_regenerates_pages` / `test_tick_alone_does_not_regenerate` / **`test_a_lapsed_token_leaves_a_scheduled_send_scheduled`** / **`test_a_failing_regeneration_reaches_the_dom`** / **`test_an_update_with_no_insert_still_regenerates_the_page`** | starting the poller unconditionally; `max(rowid)` as the watermark; letting the `except` swallow it; returning the failure into a payload field nobody reads | patching `send_followup` itself deletes the guard under test and passes; and **`dashboard.js` reads `data.replies` zero times**, so "it rides the payload" is not a mitigation |
| `test_rebuilding_twice_yields_a_byte_identical_listing` | an ordinal from a `taken` set instead of the stored surrogate | cross-links silently repointing at a different human renders perfectly |
| `test_a_knowledge_page_cannot_be_committed` — the real hook, in a temp repo | removing the `.gitignore` entry or the hook path rule | verified today: a full page passes the current hook **exit 0** |
| `test_the_assembler_makes_zero_gmail_calls` | a page builder that loops calling `fetch_thread_text` | modelled on `test_no_scope_means_the_api_is_never_CALLED` |
| `test_the_assembler_ignores_icloud_duplicates` — `alex.md` beside `alex 2.md` yields one page | reading the directory naively | `.gitignore` covers the git half; nothing covers the read half |
| **`test_kg_serve_on_a_database_that_has_never_been_migrated`** / **`test_the_tools_never_call_an_init_helper_on_the_read_only_connection`** | serving straight from `mode=ro` | verified: `ALTER TABLE` raises `attempt to write a readonly database` and a missing table raises `no such table` — and every `init_*()` helper does exactly that on a future additive column |
| **`test_kg_install_writes_a_resolvable_entry`** / **`test_the_apply_agent_config_never_names_the_kg_server`** | shipping a server nothing registers; granting the injection-exposed apply agent brain access | a protocol round-trip over a pipe proves JSON-RPC, not connection (§Lessons 31) |
| **`test_a_recorded_fact_reaches_a_page`** — driven through `POST /api/person-fact` | shipping the table without the door | `identities` has been read by nothing for weeks; a table written by nothing is the same failure reversed |
| `test_every_domain_module_is_pure` (parametrised over the package) | `import sqlite3` in any domain module | today only `domain/sheet.py` is guarded, and `temperature.py`'s guard is a substring scan a `from applypilot.networking import x` sails past |

Two suite-level rules carried from §Lessons: **a mutation harness must assert its target is
UNIQUE**, not merely present (a `replace(old, new, 1)` hit the wrong one of two identical
handlers); and **check first whether an assertion can still pass when the thing under test is
emptied** — that is the tell shared by every vacuous test this repo has found.

## 13. Risks, open questions, and success criteria

### 13.1 Risks

| Risk | Severity | Mitigation |
|---|---|---|
| **Anything lands on `/api/status`.** Measured at 80/80 against a hard ceiling; live it is 340 (43 jobs) and 847 (104 jobs), and the endpoint **serializes**: 0.26s at concurrency 1, 0.99s at 4, **3.18s at 8, 5.99s at 12**, while `dashboard.js:5643` has `setInterval(refresh, 2500)` with **no in-flight guard**. Past 2.5s the browser queues faster than the server drains. | blocker | The retrieval surface is off that path entirely: own process, own connection. The two dashboard touch points (§3) are request-scoped, and `test_status_payload_is_unchanged_by_the_person_fact_endpoint` holds the line. |
| **A scheduled send is claimed and destroyed by a poller running without a transport** (§8.3) | blocker | The tuple splits by dependency; `claim_due_scheduled`/`claim_due` refuse to claim when the transport is down; `test_a_lapsed_token_leaves_a_scheduled_send_scheduled`, patching the transport only. |
| **`upsert_contact` refuses and the hot layer stops storing contacts** (§6.5) | blocker | The writer RESOLVES from the anchor; three tests, and the two "still works" ones ship first. |
| **The FTS index resolves a hit to the wrong human** because it keys on an unstable rowid (§8.4) | blocker | Keyed on `(message_id, contact_id)` UNINDEXED; the test queries WITHOUT rebuilding. |
| **The FTS index desyncs silently** (§8.4) | blocker | No triggers; full rebuild at 7ms; the `integrity-check` vacuity written into the test's docstring. |
| **Identity merges two humans and a drafter uses the merged node** — CO-2's failure with the blast radius inverted | blocker | First-non-empty; name key qualified by company; `person_keys` PRIMARY KEY enforces one-email-one-person at the schema; the live false merge as the fixture. |
| **The person page renames itself when the operator types in an email** — 258 of 533 rows are one keystroke away | blocker | Stored surrogate (§5.1); `test_adding_an_email_does_not_move_the_page`. |
| **Org membership is a prefix match on a hostname** — §Lessons 68's Oraclecloud mechanism, which emailed four City-of-Atlanta employees about a Texas Children's job | blocker | `org_domains` is recorded, never guessed; an unmapped domain is its own node; 45 of 194 match exactly and the rest are proposals a human confirms. |
| **The two numbers get summed downstream** — 36 contacts have ≥4 outbound and 0 inbound | high | Equal-sum fixture; a parse (not a grep) of the emitted page; **and a test over the tool result, because the ranking behind `limit` is never rendered**. |
| **A fifth copy of the identity cascade.** Four exist and they already disagree — `service.py` is company- and Space-scoped, `migrate.py` has no name leg, `messages.py` is address-only | high | KG-1 extracts and re-points **only the read-only consumers**; §14.2 records which two keep their own keys and why re-pointing them would be a destructive behaviour change wearing a refactor's clothes. |
| **The extraction is "proven" by a golden file that cannot see it** — 806 bytes, zero transcript markers, `draft_email` only | high | A second golden artifact frozen before KG-11a; the cold-email one stated as necessary and not sufficient. |
| **Pages leak to the public fork** (§11.2) | high | `APP_DIR` + three guards shipped in the same commit as the writer. |
| **Regeneration silently never runs** — a failing step is swallowed, and the payload field carrying the failure is read by **no JS at all** | high | KG-8 renders the line under `📥 Check replies` and tests the DOM; `built_at` rendered visibly so "never ran" is legible. |
| **Operator-typed facts are stranded by a CO-2 move or orphaned by a delete** — 9 hand-written `contact_id` UPDATEs and a hand-written delete tuple | high | `person_context` keys on the person surrogate, so neither path touches it; `kg-check` reports unreachable facts. |
| **`person_context` is written by nothing** — `identities` reversed | high | The endpoint and the card box ship in KG-4; `test_a_recorded_fact_reaches_a_page` drives the endpoint. |
| **The MCP server is registered by nothing** — no `.mcp.json` exists anywhere in the repo | high | `applypilot kg-install` is a KG-10 deliverable, with a config-read-back test and a guard pinning that the apply agent's config never names it. |
| **`kg-serve` fails on a database nobody has migrated** — `mode=ro` cannot `ALTER TABLE` | high | Read-write `init_db()` once at start-up, then `mode=ro`; two tests. |
| **Membership read from `contacts.space_id`** — wrong on 32 rows and hidden by the read filter | high | Anchor is authoritative; `doctor --spaces` repairs. |
| **The drafters get a third context assembler** because the blocks stay in `networking/` and are merely *called* from `domain/` | high | KG-11a moves them alone; KG-11b wires them with a behavioural test **and a live before/after on real drafts**. |
| **`m005` imports the live schema dicts**, so re-running it in a year changes what version 5 meant | high | Constants duplicated inside the migration (m002/m003 precedent). |
| **An index created before the additive column pass** — raises `no such column` on **every real database and none a test builds** | high | `reply_queue.py:69-86` order, copied literally; the guard test constructs the OLD table shape by hand. |
| **A page whose facts changed is not regenerated, while `built_at` certifies it fresh** | high | `data_version` + `max(updated_at)`, never `max(rowid)`. |
| **Pages read stale because an iCloud duplicate shadows the live file** | medium | `APP_DIR` is not synced; `kg-check` reports `* [0-9].md` as a finding. |
| **Nobody ever calls any of it** | medium | `calls.log`; a zero-length log after two weeks is the answer, and it is a cheap one. |

**One pre-existing defect, filed separately so it is neither blamed on this work nor "fixed" by
slowing regeneration down:** the live dashboard process holds **29 open handles on
`applypilot.db`** (request threads leak their thread-local connection until a full GC — reproduced:
20 threads → 42 handles, still 42 after join, 0 only after `gc.collect()`), and the WAL is
**7,988,712 bytes against a 3,567,616-byte main file** — roughly **2× past `wal_autocheckpoint`'s
own 4MB threshold**, and `wal_checkpoint` appears nowhere in the source. That, plus the missing
in-flight guard on the 2.5s refresh, is the mechanism behind the reported multi-second hang.

### 13.2 Open questions

1. **Does a person span Spaces?** Exactly one address is on two contact rows today
   (`waheed.brown@arm.com`), and it is correct data — two campaigns. `migrate.plan()` **refuses** a
   cross-Space move. Recommended: one person node, membership as an attribute, and
   `test_identity_resolution_agrees_with_migrate_plan` pinning whichever answer is chosen.
2. **Where do `noticed` and `notes` end up long-term?** §5.3 keeps them where they are and
   projects them. Migrating `noticed` into `person_context` means changing the prompt path at
   `outreach.py:500` in the same commit, or having two stores for one fact.
3. **Does `organizations.name` become authoritative over `jobs.company`?** Today it is a display
   name and the string columns still hold the record. `doctor --fix-employers` writes to
   `jobs.company`; under §5.2 that rename adds an `org_names` row rather than orphaning an org, so
   the two coexist — but if the org name ever WINS, `doctor` has to be taught about it.
4. **Should `kg_owed` be the dashboard's global list too?** It computes what the 🔔 counter
   already computes, person-rooted rather than job-rooted. Wiring it in means touching the
   budgeted path; deferred deliberately.
5. **Should the dashboard search box link to a person page?** The never-gets-used lens argued the
   operator's existing habit for "who is this person" is that box, which already matches contact
   names (`dashboard.js:848`), and that answering the same question in a directory they must
   remember to `cat` is §Lessons 89. Correct, and deferred rather than dismissed: it needs an
   endpoint on its own connection (the `/api/job-descriptions` pattern — fetched on the first
   keystroke, off the 2.5s refresh). **Trigger: after the ten-page read at KG-7, if the pages are
   good, this is the cheapest way to make them reachable and it should be the next commit.**
6. **What is the first question keyword search actually fails?** Write it down when it happens —
   that sentence is the entire trigger for reconsidering a vector index.
7. **Who confirms `org_domains` mappings, and how often?** `kg-check --org-domains` proposes; the
   operator confirms. If that never happens, `inferred_mapped` stays 0 and §13.5's first criterion
   is unmet — which is the honest outcome of a design that refuses to guess, and is worth watching
   rather than pre-empting.

### 13.3 How to re-measure before believing any number here

```bash
# the budget — the two numbers CLAUDE.md gets wrong
APPLYPILOT_DIR=$(mktemp -d) PYTHONPATH=src:tests .venv/bin/python -c "
import tempfile,pathlib,applypilot.database as d
from applypilot.networking import connections,store,touches
import test_query_budget as q
p=pathlib.Path(tempfile.mkdtemp())/'t.db'; d.DB_PATH=p; d.init_db(p)
c=d.get_connection(p); store.init_contacts(c); touches.init_touches(c); connections.init_connections(c)
q._seed(c,jobs=8,contacts_per_job=4)
from applypilot import web_dashboard as wd
print('STATEMENTS:', q._count_statements(c, wd._status_payload))"

# the salience distribution
sqlite3 "file:$HOME/.applypilot/applypilot.db?mode=ro" "
with m as (select contact_id,count(*) n from messages group by 1)
select case when coalesce(n,0)=0 then '0' when n<=3 then '1-3' when n<=10 then '4-10'
            when n<=30 then '11-30' else '30+' end, count(*)
from contacts c left join m on m.contact_id=c.id group by 1 order by 1"

# how many people are one keystroke from re-keying (§5.1) — 533 / 258 / 82 at time of writing
sqlite3 "file:$HOME/.applypilot/applypilot.db?mode=ro" "
select count(*) total,
       sum(coalesce(email,'')='') no_email,
       sum(coalesce(email,'')='' and coalesce(linkedin_url,'')='') neither
from contacts"

# company strings vs live mail domains (§5.2) — 194 / 73 / 45 at time of writing
APPLYPILOT_DIR=$HOME/.applypilot PYTHONPATH=src .venv/bin/python -c "
import sqlite3,os,re
from applypilot.domain.target import slug
ro=sqlite3.connect('file:'+os.path.expanduser('~/.applypilot/applypilot.db')+'?mode=ro',uri=True)
comps=[r[0] for r in ro.execute(\"SELECT DISTINCT company FROM jobs WHERE company!='' UNION SELECT DISTINCT company FROM contacts WHERE company!=''\")]
doms=set()
for (e,) in ro.execute(\"SELECT DISTINCT email FROM contacts WHERE email LIKE '%@%'\"): doms.add(e.split('@')[-1].lower())
for (f,) in ro.execute(\"SELECT DISTINCT from_addr FROM messages WHERE from_addr LIKE '%@%'\"):
    m=re.search(r'[\w.+-]+@([\w.-]+)',f); doms.add(m.group(1).lower()) if m else None
print(len(comps),'companies',len(doms),'domains',
      len({c for c in comps for d in doms if slug(c)==d.rsplit('.',1)[0].replace('.','-')}),'exact')"

# the mode=ro claim (§7.7) — CREATE is a no-op, ALTER raises, a missing table raises
```

### 13.4 Documents this PRD amends

Each needs a dated line, or a future reader resolves the reference to the wrong thing:

- **`CLAUDE.md`** — the query budget (74/80 → **80/80** on the fixture, 340 / 847 live, +39 schema
  statements per request); the live counts (jobs 206, contacts 533, messages 791–793, touches 358,
  interactions 13, transcripts 3, phone numbers **17**); the Space table, which is missing
  `linkedin-hiring` (**104 jobs**, the largest Space in the database) and lists `sheet-search` under
  its old name; and the one-duplicate-address figure.
- **`networking/messages.py:1-6`** — the docstring says "HEADERS ONLY. No bodies, and no snippets"
  against 252 rows over 200 characters and 198,130 characters stored.
- **`spaces-prd.md` §3 and §11** — §3 lists person-as-root as an explicit non-goal; §11 sets the
  trigger ("do the graph when a real question needs it — the first time one person's history across
  two Spaces matters, or duplicates become annoying"). Both need a line saying the trigger is met
  and this document is the execution. §11's mechanical claim is still exactly right and is adopted
  verbatim.
- **`crm-prd.md` §5 and §6** — §5's seven tables: **none exists**, and `interactions` exists with
  *different* columns `(id, contact_id, job_url, kind, at, detail, source)`, 13 rows, no `body`,
  no `space_id`, no `person_id`. §6's `CRM-2`…`CRM-5` numbering was superseded on 2026-07-30.
  §5's `person_context` sketch is keyed on the contact; §5.3 here explains why it must key on the
  person instead, and names the nine `UPDATE` statements that decide it.
- **`HIST-1`** — its motivating duplicate ("one `<flast>@google.com` recruiter is in contacts
  twice") is no longer true: 11 `@google.com` contacts, all distinct; **one** duplicate address
  database-wide. Add the shared `domain/archive.classify()` note and that the cascade must accept
  a `past:<company>:<role>` anchor.
- **`ID-1`** — its C0 name-collision warning now has a third claimant, and the forward dependency
  matters: own outbound from a second alias classified as a reply **never self-heals**
  (`replies.py:348`), which would invert *both* weight numbers for that identity.
- **`ARCH-README.md`** — one row for the `KG-*` set.

### 13.5 Success criteria

- **"Who do I know at $COMPANY?"** answers across every Space — `crm-prd.md`'s own first success
  criterion, still unmet after two PRDs. **Stated honestly in v2:** Amsysis returns **1 contact +
  8 at a mapped domain** *once `amsysis.com` is recorded in `org_domains`*, and Swiftfit returns
  **1 contact + 10 at `swiftfitevents.com` counted as UNMAPPED** until somebody records that
  mapping. v1 quoted "1 → 9" and "1 → 10" as though both fell out of a string rule; only the first
  has an exact slug match, and it is one of 45 out of 194.
- A drafter and a filesystem agent asked about one person produce the **same** facts, because both
  read one assembler and tiering happens in the renderer.
- **Both** golden prompt files are **byte-identical** after KG-11a — the extraction is proven, not
  believed — and KG-11b shows a real before/after on generated drafts.
- `/api/status` measures **≤ 80 statements** on the fixture after every commit in §10.
- A full rebuild stays under the §8.5 thresholds and the poller step is invisible in
  `📥 Check replies`.
- **Zero** knowledge files in `git status`, and the pre-commit hook blocks one when forced.
- No page contains a message body, and deleting the whole knowledge directory costs nothing but a
  rebuild.
- `knowledge/calls.log` is non-empty two weeks after KG-10.

## 14. Challenged and answered

Findings a reviewer raised where the design stands, or stands with a smaller change than proposed.
Everything else from the three reviews is fixed in the body above.

### 14.1 "Only one company slug matches a mail domain" — the number is wrong, the conclusion is right

The wrong-abstraction lens claimed `Amsysis`→`amsysis.com` "is the only clean match in the
corpus". **Measured: 45 of 194 company strings match a live mail domain's registrable label
exactly.** The correction matters — a 1-in-194 hit rate would argue for abandoning domain
inference entirely, while 45-in-194 argues for a propose-and-confirm workflow, which is what
§5.2's `kg-check --org-domains` is.

**The conclusion survives the correction unchanged**, because the misses are the cases the PRD led
with: Swiftfit, Texas Children's, Nerdy, Avathon. So `org_domains` is adopted in full and the
"1 → 10" success criterion is restated in §13.5.

### 14.2 KG-1 does NOT re-point `migrate._collision` — and v1's test would have pinned the bug

The breaks-the-app lens is right that re-pointing all four cascades at one function is a behaviour
change at three of them, and that v1's `test_the_four_callers_agree` **pins the change rather than
guarding against it**. Verified: `_collision` (`migrate.py:182-193`) matches on email, then
`store.contact_id(dst_url, linkedin_url, full_name)` — destination-scoped and
linkedin-qualified — not §5.1's `norm(name)+norm(company)`. A move is almost always within one
company, so the new name leg fires on shared first names, and 48 rows are first-name-only across
47 distinct names.

A false collision there is not cosmetic: at `migrate.py:145-167` it either excludes the source row
from the move or, when `theirs and not separate`, **deletes the destination row**.

So KG-1 re-points only the read-only consumers. `_collision` and `known_at_company` keep their own
keys, and **`test_which_callers_deliberately_disagree_and_why` pins that they do**. Changing either
is a separate commit that names the behaviour change and tests it; it is not a refactor.

### 14.3 "No dashboard UI" is restated, not defended

§3's v1 wording contradicted §13.1's own mitigation column ("own endpoint, own connection, own
process"), and the never-gets-used lens is right that the budget argument applies to
`/api/status`, not to every endpoint — `/api/job-descriptions` is the precedent, fetched once on
the first keystroke, off the refresh.

**Partly adopted.** §3 now says "nothing on `/api/status`", and two dashboard surfaces are in scope
because other findings force them: the `person_context` write door (§5.3) and the regeneration
status line (§8.3). **The third — linking the search box to a person page — is deferred with a
named trigger** (§13.2 q5) rather than built, because it is the surface most improved by first
reading ten real pages, and Headline 5's falsifier says to do that before wiring anything to them.

### 14.4 "Cut KG-4 rather than ship a writer" — rejected; the writer ships

The never-gets-used lens offered two options: ship a door for `person_context`, or cut the table
and defer. **The door ships.** Decision 7 is an operator decision — operator-typed facts live in
the database, not in the disposable file — so cutting the table leaves the design with no home for
the one category of content it is not allowed to infer. What v1 got wrong was the ORDER: the
table's `kind` values were being fixed against a guess. §10 now puts KG-4 **after** the ten-page
read, which is where the real `kind` values come from.

### 14.5 The `/api/status` budget is a constraint on one endpoint, restated

Several findings treat "the budget" as a global. It is a test on one payload
(`tests/test_query_budget.py::MAX_STATEMENTS = 80`, assertion `<=`, currently at exactly 80). Both
new dashboard touch points are separately budgeted and carry
`test_status_payload_is_unchanged_by_the_person_fact_endpoint`.

### 14.6 Tiering moved to the renderer, which is stronger than the reviewer asked for

Rated medium, with the proposal that the assembler stop tiering. Adopted, and extended: the tier
and the omitted counts are **on the wire** (§7.1) as well as on the page, because an MCP client
gets the same thinning problem and had no way to see it either.

### 14.7 Headline 1's falsifier was measuring the wrong thing

Not raised as a finding, but noticed while acting on §7.6. Headline 1 claimed tiering would be
falsified by reply rates flattening across message-count buckets. Reply rate is not what tiering
claims — tiering claims the *questions* concentrate. The falsifier is now `calls.log`. Recorded
here because a falsifier that tests the wrong thing is §Lessons 12's shape: a check whose input
cannot make it fail.

### 14.8 Not acted on, deliberately

- **"Give `organizations` a `doctor --org-rename`"** (the medium half of the wrong-abstraction
  lens's H3). Unnecessary under §5.2's surrogate: a rename INSERTs an `org_names` row and nothing
  orphans, so there is no migration to write. `kg-check` still reports an org with zero resolving
  names as a finding, which is the residual case.
- **"State in §7.5 whether an org page includes inferred people"** — adopted as a *line on the
  page plus two tests*, rather than as a parameter on the page. A page takes no parameters, and
  giving it one recreates the two-surfaces-one-answer problem this document exists to remove.
- **`aliases` as a JSON column** — deleted rather than specified. It had no resolution order and no
  writer; `org_names` is the same feature as rows, which is queryable and has a primary key.

## 15. Review record

Three adversarial lenses, run against v1 on 2026-08-24. Every blocker and high was either fixed in
the body above or answered in §14; nothing was silently dropped. Where a reviewer and the codebase
disagreed, the codebase won and the PRD was corrected — that happened seven times.

| Lens | Verdict on v1 | blocker | high | medium | Disposition |
|---|---|---|---|---|---|
| **breaks-the-app** | ship-with-changes | 3 | 3 | 1 | **All 7 fixed:** §6.5 (writer resolves from the anchor), §8.3 (poller gating + watermark), §8.4 (FTS keyed on the real PK), §6.4 (second golden file), §14.2 (KG-1's scope narrowed), §7.7 (`init_db` before `mode=ro`). |
| **wrong-abstraction** | **do-not-ship** | 2 | 3 | 3 | **6 fixed:** §5.1 (person surrogate), §5.2 (org surrogate + `org_names` + `org_domains`), §7.0 (ordering rule), §5.3 (person-keyed facts), §5.4/§7.1 (tier at the renderer), §7.2 (normalisation vs recorded alias). **2 answered:** §14.1 (its match count was wrong; its conclusion was not) and §14.8. |
| **never-gets-used** | ship-with-changes | 2 | 3 | 3 | **6 fixed:** §5.3 (the write door), §9.1 (`kg-install` + the apply-agent guard), §8.3 (render the failure), §10 (reordered), §3/§14.3 (the UI non-goal restated), §7.6 (`calls.log`). **2 answered:** §14.3 and §14.4. |

**Totals: 7 blockers, 9 highs, 7 mediums. 19 fixed in the design, 4 answered in §14, 0 ignored.**

The single most expensive finding was the wrong-abstraction lens's second blocker — that a derived
person id moves the moment somebody types in an email address, on **258 of 533 rows**. v1 stated
that consequence in its own table and then wrote a test that could not exist. It is worth recording
as its own lesson: **a document that names a consequence and then asserts the opposite has already
found its own bug; read the two sentences together.**

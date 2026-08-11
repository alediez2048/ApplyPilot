# HIST-1 — The archive lens

**Size:** M · **Depends on:** `gmail.readonly` (granted 2026-07-31) · **Status:** Designed 2026-08-11. Not built.

Asked for as: *"a space dedicated exclusively to old job applications and conversations — write
the name Google and have it crawl all my gmail conversations with people at Google, the role I
was interviewing for, the people I was in contact with, their emails, linkedin, the email
sequences etc — for revision and contact nurturing."*

Then narrowed by the operator's own follow-up question, which is the better one:
*"do you need access to my entire email history, or can you crawl it whenever I ask?"*

**The answer to that question is the whole design.** It is a lens over Gmail, not a copy of it.

---

## Diagnosis

Everything below was measured against the live mailbox before any of it was designed.

### The company name is a poor entry point, and Google is the worst possible first example

    {from:google.com to:google.com}                  500+ threads (cap)
    ... minus noreply                                307

Of the first **400** of those, **13 addresses** survive a robot filter and only **8** are people:

    8  <flast>@google.com           "quick q about the Startups Performance Lead role"
    5  <firstlast>@google.com       "OOO Re: quick q about the Startups Performance Lead role"
    4  <flast>@google.com           "Slow to Respond Re: ..."
    3  four more, all on that same thread
    1  <firstlast>@google.com       "Touching Base"

    (addresses redacted to their SHAPE — this repo is a public fork, and the shape is the
     only part the classifier in C1 actually reasons about)

**Seven of the eight are already in `contacts`.** They are ApplyPilot's own outreach — *"quick q
about the Startups Performance Lead role"* is copy this app wrote. The single genuinely new human
in 400 threads is the last one. Everything else is Drive shares, payments, Cloud billing and
security alerts.

So the literal feature — type a company, crawl it — spends 400 thread reads to surface one
person, and the past Google interviews the operator remembers **are not in that result set**.

### The history is real, employer-agnostic, and nine years deep

    subject:("thanks for applying" OR "your application") OR subject:(interview)
    -> 400+ threads, 135 distinct sender domains

| 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|---|
| 9 | 25 | 27 | 44 | 51 | 42 | 25 | 46 | 69 | 62 |

Top senders are ATSes, not employers: `us.greenhouse-mail.io` 28, `myworkday.com` 24,
`ashbyhq.com` 18, `hire.lever.co` 10, `smartrecruiters.com` 8, `candidates.workablemail.com` 7,
`recruiting.facebook.com` 6, `jobvite.com` 5.

### Crawl cost is a consumer-relationship problem, not a hiring-history one

Broad `{from:<domain> to:<domain>}`, 20 employers:

    median 18 threads · mean 90 · 13 of 20 under 50

    500 chase.com    500 google.com    214 verizon.com    163 medium.com    142 affirm.com
     60 yahoo.com     40 aexp.com       24 expedia.com     19 wework.com     16 salesforce.com
     13 zillowgroup   13 arm.com        11 mongodb.com     10 okta.com        8 writer.com

The five heavy ones are heavy because the operator **banks with Chase, uses Google Drive, worked
at Verizon and subscribes to Medium**. Hiring volume has nothing to do with it.

Adding a hiring-shaped clause collapses the tail completely:

| domain | broad | + hiring terms | + drop noreply/promotions | crawl |
|---|---|---|---|---|
| chase.com | 500 | 98 | **89** | ~11s |
| google.com | 500 | 175 | **68** | ~8s |
| verizon.com | 214 | 77 | **75** | ~9s |
| medium.com | 163 | 64 | **2** | ~1s |
| affirm.com | 142 | 19 | **14** | ~2s |

**Every employer is an 11-second crawl or less.** That is what makes on-demand viable and an
import unnecessary.

### Latency, measured rather than assumed

    threads().list  400 ids   0.9s   (5 API calls, stateless)
    threads().get   metadata  0.11s per thread, unbatched, single-threaded

So a narrowed employer crawl is **1–11s** today, before any batching or parallelism.

### Roles ARE recoverable from subject lines, for normal employers

    MongoDB   "MongoDB Interview Request! LLM Optimization Lead"
              "LLM Optimization Lead Opening at MongoDB"
              "MongoDB Interview Confirmation: Wednesday January 14, 11:00"
    Verizon   "SEO Strategist Opening Follow Up"
    WeWork    "TPM Interview Follow Up" · "WeWork Interview - You're on the calendar!"
    Zillow    "Zoom Video Interview Confirmation"          <- no role, and this is the common case

Partial, not free. §Lessons 84 is the standing warning: the title is the hard half, and 11 of 33
live `jobs` rows carry one that must never be quoted. Here it must be recovered from a subject
line, so `""` is a real answer and the renderer must handle it.

---

## What this is NOT

**Not a Space.** A Space is a container for rows, with a shape, a registry entry and a
`space_id` on every row it owns. This stores nothing until the operator promotes something, so
there are no rows to contain. Filing it as a Space would need a third `shape` — `SHAPES` has
exactly two and `Space.__post_init__` raises on a third — and would put archive rows into
`jobs_shaped_ids()`, where enrich and score would start scraping dead 2019 URLs.

**Not an import.** An import writes ~450 contact rows keyed on `job_url` that no longer exists,
through `contact_id()`, which is CO-1's unfixed half. The duplicate is already live and visible:
one `<flast>@google.com` recruiter is in `contacts` **twice**, once `submitted` and once `drafted`. Importing
nine years multiplies that by every recruiter who ever appeared on two roles, and it is not
undoable without a migration over a corpus nobody can check by hand.

**Not a reversal of "nothing is read automatically".** That commitment
(§CRM-4b, `_sync_thread` stores no message text) survives intact, because nothing here is
automatic. It reads when asked. The OAuth grant is all-or-nothing and was made in July; this
ticket does not widen it by one byte.

---

## Design

    operator types "Google"
      -> build a narrowed Gmail query          (no writes)
      -> list thread ids                       (0.9s)
      -> fetch metadata per thread             (0.11s each)
      -> classify humans / robots / self
      -> group threads into ROLES
      -> render: people, roles, a timeline
      -> nothing has been written

    operator clicks "keep" on a person
      -> NOW a contacts row exists, with an anchor date and a ladder

### C1 · `domain/archive.py` — the query and the classifier, pure

No `http`, no `sqlite3`, per the `domain/` boundary. Two jobs:

**`query(company, *, since=None)`** builds the Gmail search. Composed, never a format string of
operator input — a company containing `OR` or a quote would otherwise rewrite the query.

    {from:<domain> to:<domain>}  <HIRE_TERMS>  -in:promotions -in:social
    -from:no-reply -from:noreply -from:no_reply

`HIRE_TERMS` is the measured clause above. `since` is optional and off by default: the 2017 mail
is the point of the feature, so a default date floor would hide exactly what was asked for.

**`classify(address, headers)` → `person | robot | self`.** This is where the honesty goes. A
regex gets most of it and the misses are systematic — measured, from the runs above:

| Slipped through | Why the obvious regex misses it |
|---|---|
| `no_reply@mcmap.chase.com` | underscore, not hyphen |
| `customer.satisfaction@experience.chase.com` | a product subdomain, no noreply token |
| `survey@corp.zillowgroup.com`, `surveys@` | a role address the list did not name |
| `mongodb-atlas@`, `team@voyage.mongodb.com` | product mail on the employer's own domain |
| `privacyoffice@verizon.com` | a department, not a person |
| `candidate-experience@feedback.google.com` | a survey robot that looks like recruiting |

So `classify` is **two-layer**: the token list, plus a shape rule — an address whose local part
has no separator-joined name (`first.last`, `flast`, `firstl`) and whose host carries a service
subdomain is a robot. It returns a REASON, not a bool, because "dropped 6" is unverifiable and
"dropped 6 robots, 2 surveys" is not — the same argument as `geo.is_excluded` returning the place.

**A `self` bucket is required, and it is not hypothetical.** The operator's OWN work address
came back in the Verizon crawl: they **worked there**. A former employer's domain returns years of
his own work mail, which is neither an application nor a contact. Classifying it as a person puts
his own colleagues in a nurture list.

### C2 · `networking/archive_search.py` — the crawl

Wraps `gmail_read.search_threads` + `thread_messages`, both of which already exist and already
return exactly the fields needed. Adds:

- a **batched/parallel fetch** — 0.11s × 89 is 10s serial and the API supports batching. Bounded
  concurrency, because this shares a token with the reply poller.
- a **hard cap** with a visible statement of what it dropped. §Lessons: *no silent caps.* A crawl
  that stops at 200 threads must say "showed 200 of 340", or it reads as "that is all there is".
- **no writes at all.** A test asserts the module imports no store and executes no SQL, in the
  same shape as `test_sql_lives_only_in_the_data_layer`.

### C3 · Role grouping

Threads → applications. Signal, in order of confidence:

1. Gmail's own `threadId` — messages already grouped, free and exact.
2. Subject after stripping `Re:`/`Fwd:`/`RE: FW:` — joins a thread that forked.
3. A role phrase extracted from the subject (`"… LLM Optimization Lead …"`).
4. Date clustering — threads within a window are probably one process.

1 and 2 are mechanical and ship first. **3 is where an LLM pass belongs and nowhere else**: the
role is prose in a subject line, and the same measurement that produced "LLM Optimization Lead"
also produced "Zoom Video Interview Confirmation - Response Requested", which names none.
Ungrouped threads render under **"other conversations"** rather than being forced into a role —
a wrong grouping is worse than none, because it puts a Zillow interview under a MongoDB role.

### C4 · The surface

A search box that is **not** the jobs table's filter — that one filters `LAST_JOBS` in memory and
must keep doing so. This is its own view, reached from the nav, rendering:

    Google · 2017–2026 · 68 threads · 8 people · 2 roles          [showed 68 of 68]

    ROLES
      Startups Performance Lead (2026)     7 people · 12 threads · last: 4d ago
      — no role identified — (2019)        1 person · 3 threads

    PEOPLE
      <recruiter>       <flast>@google.com    8 threads  2026    [already a contact]
      <hiring manager>  <firstlast>@google.com 1 thread  2024    [+ keep]

    DROPPED  59 robots · 3 surveys · 0 self                        [show]

`[already a contact]` is load-bearing: seven of eight Google humans are already stored, so
without it the panel reads as eight discoveries when it is one.

**LinkedIn URLs are NOT in Gmail.** The ask named them; they would have to come from Apollo
(a credit per person) or from the 899-row `connections` table (free, and it is a local join).
The connections table only, at first — spending credits on a nine-year-old contact before the
operator has said they want them is the wrong order.

### C5 · Promote — the only write path

`[+ keep]` on a person creates a `contacts` row. Three things must be decided at that moment
rather than inherited:

- **`job_url`** — a past application has none. Use the `domain/target.py` pattern that already
  solves this: `anchor()` → `past:<company>:<role-slug>`. Proven, and it is why targets rows
  live in `jobs` without a URL.
- **`source`** — `'archive'`, never `'apollo'` or `'introduction'`. CRM-2's `by_layer()` cannot
  prove a warm channel beats a cold one if a nine-year-old thread is filed as a cold find.
- **`email_status`** — `verified` is a claim about the ADDRESS. A person who actually emailed you
  from it is the strongest possible evidence, so `verified` is right here and the reasoning
  should be written down, because it is the opposite of the manual-add case
  (`unverified`, typed from memory).

**Ladder anchors stay empty.** Promoting is not sending. `submitted_at` means *we emailed them*,
and back-dating it to a 2019 thread would tell the ladder we sent something we did not —
§Lessons 28, where a ticket prescribed exactly that fix and measuring showed the failure did not
exist.

---

## Commits

| | | |
|---|---|---|
| C1 | `domain/archive.py` — query builder + 3-way classifier | pure, no I/O |
| C2 | `networking/archive_search.py` — batched crawl | no writes, asserted |
| C3 | thread → role grouping (mechanical only) | LLM pass deferred |
| C4 | the search surface + dropped-count disclosure | |
| C5 | promote-one-person write path | the only write |
| C6 | connections-table LinkedIn join | free, local |

**~2–3 days.** Against 3–4 days plus an irreversible migration for the import version.

---

## What could go wrong

**The classifier is the whole feature and it is fuzzy.** Every measured hole above is a person
who is not a person. The failure is asymmetric: a robot shown as a person costs one glance, a
person dropped as a robot is invisible and unrecoverable — so the default on an unknown address
is **person**, and the dropped list is one click away. Same argument as `geo.py` keeping a blank
location.

**Nine years is long enough for an address to have died.** A 2017 recruiter has moved on. Nothing
here verifies that, and promoting them is a live send to a dead address. Out of scope, worth
saying: the archive shows what WAS true.

**Every search re-pays the crawl.** 1–11s is fine interactively and is not fine for "who went
quiet across all employers", which needs stored state. A thread-id-and-date cache — no names, no
addresses, no text — would fix the latency later without becoming a copy of the mailbox. Not in
this ticket, and deliberately not designed in advance.

**The heavy-tail measurement is 20 employers, not 135.** Chase and Google both hit a 500 cap even
narrowed to 98 and 175, so an employer with more consumer mail than either would be slower than
anything measured here.

## Falsifiers

- `test_the_crawl_writes_nothing` — the module executes no SQL and imports no store. This is the
  claim the whole design rests on; if it fails, this is an import wearing a lens's name.
- `test_a_former_employer_is_not_a_contact_list` — the operator's own former work address at a
  past employer classifies as `self`. Taken from a real crawl; the fixture carries the shape,
  never the address.
- `test_every_measured_robot_is_dropped` — the six escapes in the C1 table, as cases. Each one
  shipped past a regex once.
- `test_an_unknown_address_defaults_to_person` — the asymmetry, pinned.
- `test_a_capped_crawl_says_it_was_capped` — no silent caps.
- `test_promote_leaves_ladder_anchors_empty` — §Lessons 28, mutation-checked.
- `test_a_company_name_cannot_rewrite_the_query` — `Foo" OR from:me` stays one term.

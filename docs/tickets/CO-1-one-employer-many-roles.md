# CO-1 — One employer, many roles

**Size:** M · **Depends on:** — · **Status:** Designed 2026-08-11. Step 1 shipped, rest not built.

## Diagnosis

Asked for as a layout problem: *"aggregate their cards, at least bundle them somehow"*. Measured
before designing, and the measurement moves the whole ticket:

    33 jobs  →  32 distinct resolved employers  →  0 employers holding more than one job
    243 contacts  →  0 people appearing on two job rows

**The case has never happened.** So this is not a fix for something on screen, and anything built
here renders for nobody until it does. §Lessons 28 and 76 are both the shape where a ticket named
a cause the data disagreed with; this one names a cause the data has not reached yet, which is a
different thing and worth stating rather than quietly building past.

It is still worth designing, because the cost is asymmetric. The visual half costs a header row.
The other half sends two cold emails to one recruiter about two roles, and that is not
recoverable — §Lessons 42's problem with the volume turned up, because the tell is no longer a
shared phrasing across a company, it is the same person receiving the same pitch twice.

### The real defect is in the keying, not the table

`store.contact_id()` hashes **`job_url` | linkedin | name**. A recruiter found for a second role
at the same employer is therefore a *different contact row*: own `submitted_at`, own
`sent_message_id`, own `touches` ladder, own three-touch schedule. Nothing joins them.

`skip_known` does not help. It excludes `store.get_contacts_for_job(job_url)` — by design, and
correctly for round two on one job — but a second job has no known contacts by definition, so the
whole company is new to it.

Three guards sit between that and a duplicate send. All three were `0`:

| Guard | Scope | Was | What it says |
|---|---|---|---|
| `OUTREACH_COOLDOWN_DAYS` | per ADDRESS, across jobs | 0 → **30** | *"already emailed X **for another role** on …"* |
| `OUTREACH_COMPANY_CAP` | per COMPANY, across jobs | 0 | *"a company sees one sender, not seven threads"* |
| `OUTREACH_DAILY_LIMIT` | global | 0 | — |

The cooldown's refusal message was written for exactly this situation and had been switched off
since 2026-08-03. **Turned back on 2026-08-11** (step 1 below). Note `0` there does not mean
unlimited — the cutoff becomes `now`, so it matches nothing. §Lessons 50, in the one guard whose
wording already anticipated this ticket.

### What is shared, and what only looks like it

The split is the whole design. An **application** is per role; a **relationship** is per employer.

| | Shared across roles | Why |
|---|---|---|
| The people | **yes** | same humans, same inboxes |
| The email thread | **yes** | they will reply on one of them, not both |
| ATS account / sign-in | **yes, already** | `ats_accounts` is per TENANT on purpose (§Lessons 70) |
| Company cap, cooldown | **yes, already** | both count across jobs |
| `job_context` | **yes, but stored per row** | CTX-2 calls it *"what do I know about THIS company"* and keys it on the job. Two rows at one employer means typing the same paragraph twice, and drifting |
| Résumé, cover letter | no | tailored per posting; that is the point |
| `apply_status`, `applied_at`, `interview_at` | no | you are rejected from a role, not a company |
| Temperature, last interaction | **no, but** | the reading is per application; the reply that drives it arrives once and belongs to the person. Two rows would both go `warm` off one reply, which is true and reads as two pieces of good news |

## Design

- [x] **1. Cooldown back on** (`OUTREACH_COOLDOWN_DAYS=30`). One line, and the only thing standing
      between today and a duplicate send. Verified functionally rather than by reading the number:
      `can_send` on a real emailed address now returns
      *"already emailed … for another role on 2026-08-10"*, and an unseen address still returns
      `ok`. Refuses nothing that has ever happened — 109 sends across 109 distinct addresses.
- [x] **2. A collapsible company band ABOVE its job rows.** Rows stay rows. Live on the board:
      `▾ Google · 2 roles · 16 people · 7 emailed · 1 replied · 5 follow-ups due`
      Every number is **deduplicated across the roles** — that is the only part that is not
      cosmetic. Two rows each reporting "16 contacts" for the same sixteen humans reads as
      thirty-two, and thirty-two is the number that would justify sending thirty-two emails.
      Built from the payload already on screen: no new query, `/api/status` unmoved.
- [x] **2b. `⚠ N on both`** — the count of people stored on more than one of the roles. Styled as
      a warning rather than as another statistic, because it is the only thing on the band that
      asks you to do something, and **no other surface can show it**: each job's own panel lists
      its own contacts and both look complete.
- [x] **3. The band renders only at 2+.** A band over one row is furniture, and with 32 rows and
      31 employers it would be 31 pieces of it. This is the one thing the first round of tests
      missed — mutating `length > 1` to `> 0` survived everything until the end-to-end render
      test was added.
- [x] **4. The group sits at its BEST member's position.** The table's `ORDER BY` deliberately
      sinks closed rows, so a company holding one live and one cancelled role has no single
      position, and taking the worst buries live work under a dead requisition. It came free:
      a `Map` keeps insertion order, so walking the already-sorted list puts each group at its
      first member's slot. **No second ranking** — one that could disagree with the sort beside
      it is a bug waiting.
- [ ] **5. `job_context` is offered from the sibling row.** Not moved, not auto-copied — a
      *"use the context from your other Google role"* affordance. Moving it to a company would be a
      second partition key over `jobs`, which §Spaces already refused once.
- [ ] **6. The second-role email knows about the first.** See below; this is the one that needs
      real work.

### `burned_block` is empty exactly when it is needed

`burned_block(previous)` shows the model what this person has already been sent, and it is fed
per contact. Under the current keying a second-role contact **is a new contact with no history**,
so the block is empty in the only situation it was written for. Whatever step 6 becomes, it has
to gather `previous` by **address**, not by contact id.

That is also the argument against fixing this purely in the UI: grouping the cards makes the
duplicate legible to the operator and completely invisible to the drafter.

## Explicitly not

- **Not one merged card per company.** Every per-row control — the `⋯` menu, the four tabs, the
  status strip, `nextAction()`, Re-apply, the two documents — is written against one job, and an
  application genuinely is the unit for all of them. §Lessons 43 (seven times) and §Lessons 89:
  the control belongs beside the work, and the work is the application.
- **Not a `companies` table.** The employer is already derivable from the row via
  `derive.resolve_employer`, with provenance. A table would be a fourth place the employer name
  lives and a fourth place it can disagree (§Lessons 81).
- **Not a second partition key on `jobs`.** SPACE-1a settled that: `space_id` alone decides
  membership, and two partition keys over one table is §Lessons 49 waiting to happen.
- **Not grouped by `site`.** That is the discovery source. Grouping by it would put every
  Greenhouse job in one box and split LegalZoom from itself — §Lessons 81, which is the bug of
  showing `site` where the employer belongs.
- **Not a company-level Space.** A Space is a campaign. Two roles at Google are one campaign.

## Tests

23 in `tests/test_employer_grouping.py`, all executed under the shared browser stub rather than
grepped (§Lessons 48, and the `isClosed` case where four grep assertions survived a real
mutation). Six mutations, all killed:

| Mutation | Killed by |
|---|---|
| no grouping at all (one group per job) | 9 tests |
| `shared` counts every person | 4 |
| people counted per job, not deduplicated | 3 |
| `person()` keyed on email only (address-less contacts collapse into one) | 1 |
| employer name not escaped | 1 |
| **band renders for a single role too** | 3 — and this one **survived the first round** |

That last row is the finding. Fourteen tests of the grouping functions all passed against a
version that puts a band over every single-role employer, because none of them drove the render
path. `_render()` calls `renderJobsTable` and reads the markup out of `#jobs`, which is the only
thing that proves the band reaches the page.

The falsifier still open is a **second job at an employer that already has contacts, driven
through discovery** — the live Google pair is the fixture for it now.

- `test_the_same_person_is_not_emailed_about_two_roles` — two jobs, one employer, run the send
  path twice against the same address, assert the second is refused **and that the refusal names
  the other role**. A generic "already sent" would satisfy a weaker assertion and tell the
  operator nothing.
- `test_an_unseen_address_still_sends` — guard the guard. A cooldown that refuses everything
  passes the test above and stops the app working.
- `test_a_single_role_employer_gets_no_header` — non-vacuity for the grouping: a version that
  emits one group per job satisfies every other assertion in the file.
- `test_the_group_takes_its_position_from_its_best_member` — with one closed and one live row.
- The grouping predicate must be **executed, not grepped** (§Lessons 48, and the `isClosed`
  case in `test_cancelled_job.py` where four grep assertions survived a real mutation). Use the
  shared `tests/browser_stubs.py`.

## Open

**Does a reply to a shared person warm both rows?** It is one piece of news and would render as
two. Leaving it is defensible — each application really is progressing — but it inflates the only
honest signal on the board, and §Lessons 35 is what inflating a signal costs. Decide when the
case exists; do not guess now.

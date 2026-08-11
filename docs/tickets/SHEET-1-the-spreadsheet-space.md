# SHEET-1 — The spreadsheet Space

**Size:** M · **Depends on:** SPACE-1a..4 (shipped) · **Status:** Designed 2026-08-11. Not built.

Asked for as: *"importing a spreadsheet / Google Sheet of company/name/position information …
creating cards without necessarily the cards having a job to apply to, we can add the job later
if any, but the main unit for this template is the company, the employees, and the outreach."*

Two decisions taken by the operator up front, and both simplify the ticket:

- **The draft comes from the CAMPAIGN PREMISE.** Not a job-seeker email missing its posting, not
  a consultant's pitch. The Space's one constant paragraph is the message.
- **A NEW Space beside Gauntlet**, not a repurposed one. `shape` is frozen after creation and
  Gauntlet holds a live job with 10 contacts (see below).

---

## What already exists, which is most of it

`pipeline/targets` (SPACE-3) is already "the company is the row, no posting required":

| Piece | State |
|---|---|
| A row keyed `target:<space>:<slug>` from a name the operator STATES | built |
| `target.parse_line` / `parse_input` — pasted lines → companies, rejects returned | built |
| `repo.add_target` — idempotent, stamps `detail_scraped_at` so it never looks like a failed scrape | built |
| Contacts anchored to that row; `contact_id` hashes the anchor | built |
| Follow-up ladders, deck, reply detection, temperature | built, channel-agnostic |
| `_pitch_user_prompt` — sender · contact name/title/company · the campaign's constant paragraph · **no posting** | built |
| The card + its People tab (`peopleList` reads `j.contacts`, shape-blind) | built |

**None of it has ever run.** Measured 2026-08-11:

    job-search     jobs    pipeline/jobs      32 rows   235 contacts
    gauntlet       jobs    pipeline/jobs       2 rows    10 contacts
    partnerships   outreach  pipeline/targets   0 rows     0 contacts

Zero targets have ever been created, so this ticket is also SPACE-6 — the falsifier the PRD asked
for. If it costs a schema change, the central claim of `spaces-prd.md` was wrong.

### The paste that prompted this

The operator pasted a Google Sheets URL into Gauntlet's import box. It is a **jobs** Space, so
`_import_urls` ran `_URL_RE.findall`, took the link as a posting, and stored:

    title    "Docs uploaded job"
    company  "Docs"          <- docs.google.com, host label as employer
    error    "no data extracted"

§Lessons 20/52 for the eighth time (Ats · Hr · Edu · Ouryahoo · Oraclecloud · Recruitics ·
Jobvite · **Docs**). Nothing crashed and the row looks like a real one. Delete it as step 0.

Note what did NOT happen: no contacts were created, and the sheet's columns were discarded
entirely. There is no path today from a name and a position to a contact — contacts come from
Apollo, or one at a time through `＋ Add someone by hand`.

---

## The four gaps

### G1 · Rows are PEOPLE, and nothing parses columns

`parse_input` is one COMPANY per line. The sheet is one PERSON per line, with the company
repeated:

    Company     Name          Position           Email              LinkedIn
    Ridgeline   Dana Okafor   VP Engineering     dana@ridge.com     /in/danaokafor
    Ridgeline   Sam Iyer      Staff Engineer
    Northwind   Alex Roy      Head of Talent     alex@northwind.io

→ **2 company rows, 3 contacts.** The grouping IS the bundling the operator asked for.

**Google Sheets puts TSV on the clipboard.** So paste is the entire integration: no OAuth, no
API key, no token on disk, nothing to revoke. Worth stating because the obvious build is a
Sheets integration and it buys nothing — a URL cannot be read without credentials anyway, which
is exactly what the failed paste above proves.

### G2 · Contacts you SUPPLY, not discover

Three decisions, cheap now and expensive later:

- **`source='import'`.** Not `apollo`. CRM-2's `by_layer()` exists to compare a warm channel
  against a cold list, and filing a hand-built list as a cold find makes that unanswerable
  forever.
- **Verification is SKIPPED, not run and passed.** `verify_contact` exists to catch people who
  work somewhere ELSE, which cannot happen to a name the operator typed (§Lessons 19). Run it
  and every imported person is dropped for having no Apollo record — §Lessons 14, where being
  right produced nothing.
- **`email_status='unverified'`** when an address is given, `'none'` when it is not. `verified`
  is a claim about the ADDRESS and a spreadsheet is not evidence; the same rule already
  separates a Cc off a live thread from one typed from memory.

### G3 · The premise has to become the SUBJECT, and today it is explicitly not

This is the only genuinely new writing, and `_premise_block` says the opposite in as many words:

    "It is background, not the subject. A message that is only the premise is about the sender."
    "Say it in your own words each time, or leave it out."
    "It answers WHY THIS ROLE"          <- there is no role here

Appending "…but here it IS the subject" is §Lessons 40 — two instructions disagreeing is a code
bug, and the heading wins. It needs its own block where the premise is central, and the existing
one is left exactly as it is for the two shapes that still want it as background.

The system prompt is a **third** one, and it is smaller than it looks. `_PITCH_SYSTEM` is ~80%
rules this shares verbatim — under 120 words, exactly one answerable question, an explicit out,
no em dashes, no urgency, and *"several people at the same company get these and they sit near
each other"*. Five lines differ, and they are the five that say the email is a proposal:

| `_PITCH_SYSTEM` says | this Space needs |
|---|---|
| "proposing a piece of work. Not a job application." | neither — the premise says what it is |
| "Say the concrete thing you would do. One specific piece of work." | drop: the premise is the substance |
| "Lead with THEM, not with you" | keep, and it is HARDER — often the only fact about them is a job title |
| "Never imply you applied to anything. There is no job here." | keep, and it is now literally true |

`must_mention` already enforces a required phrase by RETRY rather than by appending (§Lessons 87),
and Gauntlet's `['GauntletAI']` carries over unchanged.

### G4 · "Add the job later" must never re-key the row

`store.contact_id()` hashes `job_url`, and on a target row the anchor IS the `job_url`. Swapping
in a posting URL orphans every contact, every ladder and every message on that card — CO-1's
failure with a new trigger.

**The posting is an ATTRIBUTE, not an identity.** `title`, `application_url` and
`full_description` are already columns on a `jobs` row and sit empty on a target. Fill them in
place; the anchor never moves. One card gains a posting and keeps its people, their sequences and
their history.

---

## Commits

| | | |
|---|---|---|
| C0 | Delete the `Docs uploaded job` row | minutes |
| C1 | `domain/sheet.py` — TSV/CSV → companies + people, pure | ~1d |
| C2 | Import endpoint + bulk contact create (G2's three rules) | ~1d |
| C3 | Premise-central block + third system prompt | ~1d |
| C4 | Attach a posting to an existing card, in place | ~½d |
| C5 | The Space itself: new targets-shaped Space carrying Gauntlet's premise and `must_mention` | ~½d |

**~4 days.** Down from the first estimate because `_pitch_user_prompt` already has the right
SHAPE — sender, person, company, one constant paragraph, no posting — and only its framing is
wrong.

### C1 in detail

**Header detection, not column order.** A sheet exported from anywhere puts the columns in its
own order, and a positional parser silently files positions as names. Match on the header row by
normalised name (`company`, `name`/`full name`, `title`/`position`/`role`, `email`, `linkedin`,
`domain`, `notes`), and REFUSE with the list of headers found when the company column is missing
— that is the one field with no fallback.

**A first/last name pair is normal** and must be joined rather than rejected: the sheet the
operator has may carry `First Name` / `Last Name` as separate columns, which is what every CRM
export does.

**Rejects are returned per ROW with their line number**, the same rule `parse_input` already
follows. "Imported 38 of 41" with three named lines, never "imported 38".

**Deduplicate people within the paste** by (company slug, lowercased email) then by
(company slug, lowercased name) — a sheet accumulated by hand repeats people, and two rows for
one person is two ladders aimed at one inbox.

**Company slugging is `target.slug`, already written**, so "Ridgeline", "Ridgeline Inc." and
"Ridgeline Logistics, Inc." land on one card rather than three. That function already strips
corporate suffixes from the END only, because "Inc" is noise in "Ridgeline Inc" and load-bearing
in "Inc Magazine".

---

## What could go wrong

**A sheet is bigger than a paste.** 41 rows is fine; 2,000 is a different feature and would want
a file upload, a progress bar and a resumable write. Cap the paste, say what was dropped, and do
not pretend otherwise (§Lessons: no silent caps).

**Nothing verifies these people.** That is the decision in G2 and the cost is real: a stale sheet
emails someone who left two years ago. The row shows `source: import` and `unverified` so the
operator can see which contacts carry no corroboration at all.

**The per-company cap and the cooldown now matter much more.** A sheet with eight people at one
company is eight emails to one employer from one importer. `OUTREACH_COMPANY_CAP` is **0** on this
machine — off — and the cooldown went back to 30 days on 2026-08-11 (CO-1). Turning the cap on
before the first import is a one-line change and is strongly advised in the ticket rather than
done silently, because it is the operator's call how loud a campaign is.

**The premise is per-SPACE, so a parroted sentence reaches every inbox in the campaign.** This is
the largest repetition exposure in the app — `job_context` is per row, `noticed` is per person,
this is per campaign — and CTX-1's guarantee here is still unproven against a live model
(§Lessons 42 was invisible to inspection). **Generate all of one company's people before sending
any of them, and count shared sentences.**

---

## Falsifiers

- `test_a_spreadsheet_space_needs_no_schema_change` — the SPACE-6 claim. Drives a sheet import
  end to end and asserts no migration ran. If it fails, `spaces-prd.md` is wrong and should say so.
- `test_columns_are_found_by_header_not_by_position` — shuffle the columns; the same people import.
- `test_a_missing_company_column_refuses_and_names_the_headers_it_found`.
- `test_first_and_last_name_columns_are_joined`.
- `test_the_same_person_twice_is_one_contact` — and the same company spelled three ways is one card.
- `test_rejected_rows_are_returned_with_their_line_numbers`.
- `test_an_imported_contact_is_never_verified_and_is_never_sourced_apollo`.
- `test_attaching_a_posting_does_not_change_the_anchor` — mutation-checked, because the failure is
  silent and destroys every ladder on the card.
- `test_the_premise_is_the_subject_here_and_background_everywhere_else` — the two blocks must
  differ; a shared one means §Lessons 40 was re-introduced.

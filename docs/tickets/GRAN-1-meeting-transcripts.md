# GRAN-1 — Meeting transcripts on a contact

**Size:** M (paste) · L (Granola API) · **Depends on:** nothing · **Status:** Designed 2026-08-12. Not built.

Asked for as: *"the ability to add transcripts of conference meetings, phone calls and others to
any contact I interact with."*

---

## Four measurements, taken before designing, and three of them move the ticket

**1. Granola is not installed on this machine.** No `Granola.app`, no
`~/Library/Application Support/Granola`, nothing in Spotlight. So every claim anyone makes about
its local cache format is unverified here and cannot be checked without installing it first.

**2. The public API is Business/Enterprise only.** `docs.granola.ai`: personal API keys require a
**Business plan minimum**; Free and Pro are not listed. Endpoints are `GET /v1/notes`,
`GET /v1/notes/{id}`, `GET /v1/notes/{id}/transcript`, auth `Bearer grn_…`, 5 req/s. A note
carries `id`, `title`, `owner` (name/email), `summary`, `transcript`, timestamps — **and only
notes that already have an AI summary are returned at all**; anything still processing 404s.

> **This is the gate, and it is the whole reason the ticket is phased.** ApplyPilot is a
> single-operator tool. If the operator is on Free or Pro, the API route does not exist at any
> price short of a plan upgrade, and no amount of code changes that.

**3. Transcripts are diarized by AUDIO SOURCE, not by speaker.** The community export tool
describes "word-level transcripts with diarization (mic vs system audio)". So a transcript tells
you which lines are the OPERATOR and which are *everyone else on the call* — it does not tell you
which of three attendees said what. **Automatic attribution of a transcript to a named contact
is therefore not available from the transcript itself.** It has to come from the calendar
attendees, or from the operator choosing.

**4. The keying problem is real and SMALL.** `contact_id` hashes `(job_url, linkedin_url, name)`,
so one human across two roles is two rows. Measured on the live database:

    213 contacts with an address, 209 distinct addresses
      4 addresses appear under more than one job
     15 names appear under more than one job

Out of 244 contacts. **I described this as "the hard part" before measuring and that was wrong**
— it affects about 8% of contacts and it is an edge case to handle, not a blocker to clear first.
CO-1 stays open on its own merits.

### And one measurement about where a transcript could live

    messages.snippet    cap 200 auto / 2000 pasted   ·  live max 200, mean 75
    interactions.detail live max 244

A real 45-minute transcript is **20–50 KB**. That is two to three orders of magnitude past both,
so neither table is its home — and forcing it into `messages` would be worse than a size problem:
that table is keyed on Gmail's own message id and carries `thread_id` / `rfc_message_id`, none of
which a meeting has. Inventing them corrupts the join reply detection runs on. This is exactly
why UX-2 put LinkedIn DMs in `interactions` instead.

`/api/status` costs **74 statements against a budget of 80** and re-renders every 2.5s, so
nothing about transcripts may be read on that path.

---

## The shape

**A transcript is about a MEETING; a meeting has attendees; an attendee maps to contacts.**
Three nouns, and collapsing any two of them is the mistake:

- Storing the transcript on a contact duplicates a 40 KB blob per attendee and lets two copies
  drift.
- Storing it on a job is wrong twice over — a call can span two roles at one employer, and on a
  `pipeline/targets` Space there is no posting at all.

So: **one row per meeting, and a join.** Same shape as `messages`, and for the same reason
(§Lessons 36: `INSERT OR REPLACE` on a shared key silently reassigned three Writer messages to
one contact and emptied another's conversation).

```
transcripts            one row per meeting
  id                   sha256(source, external_id) — deterministic, so re-import is a no-op
  source               'granola' | 'paste' | 'otter' | …   never collapsed with the next field
  external_id          Granola's note id, or '' for a paste
  title                "Intro call with Dana"
  started_at           ISO
  duration_s
  summary              the SHORT form — what reaches a prompt
  body                 the full text. Capped at TRANSCRIPT_MAX (see below)
  attendees_json       whatever the source supplied, verbatim and unparsed
  created_at

transcript_contacts    the join — who this meeting was WITH
  transcript_id
  contact_id
  matched_by           'email' | 'name' | 'manual'   ← provenance, load-bearing, see below
  PRIMARY KEY (transcript_id, contact_id)
```

**`matched_by` is not decoration.** §Lessons 34 and §Lessons 86 are both the same failure — a
guess laundered into a stored fact and then trusted forever. A transcript attached because the
operator picked the contact is a different claim from one attached because a name in a calendar
invite looked similar, and only the first should ever be quoted back with confidence.

---

## Phase 1 — Paste a transcript (ship this first, and possibly only this)

**Needs no plan, no install, no API key, no credential, nothing to revoke.** It is the same
integration strategy that already works twice here: the sheet import is a paste because Google
Sheets puts TSV on the clipboard, and the LinkedIn thread reader is a paste because driving
LinkedIn from outside the browser was abandoned twice (§Lessons 3).

- **Where:** a `📝 Transcripts` section in the contact panel — beside the conversation, not in a
  separate tab. §Lessons 89 has fired three times on placement; the transcript belongs where the
  person is.
- **What the operator does:** open Granola, ⌘A ⌘C the note, paste, pick the date, Save. Granola's
  own AI summary can be pasted into the summary box or left empty.
- **Attribution is a CHOICE, not an inference** — the contact whose panel you are standing in is
  attached, plus a checkbox list of the other people on that card. This is the only honest
  option given measurement 3.
- **`TRANSCRIPT_MAX`** ~200 KB, enforced at the WRITE, and the cap is SERVED to the frontend
  rather than written twice (§Lessons 90: a bound in two places is two bounds).
- **A stored transcript is `interactions`-visible** as a new kind (`met`, weight 0 — a meeting
  the operator attended is our own record of an event, and whether it counts as *engagement*
  depends on who called it, which we do not know).

**Cost:** ~1 day including tests. One migration, one endpoint, one panel section.

## Phase 2 — What a transcript is FOR

Storing it is worthless on its own; this is the half that pays.

A transcript is the strongest possible tier-4 context in the CTX cascade — *what do I know about
THIS person* — far stronger than `contacts.noticed`, which is one line the operator typed after
glancing at a profile. It should reach `_known_block`, and therefore every drafter.

**Two constraints, both learned the expensive way:**

- **Only the SUMMARY reaches a prompt, never the body.** 40 KB in a prompt is most of the context
  window and would swamp the posting, the premise and the voice — and §Lessons 40 says the loudest
  block wins.
- **The prompt is forbidden from QUOTING it.** §Lessons 83, with higher stakes than the deck
  beacon: a sentence that only makes sense to someone who was on that call tells the reader you
  are working from a recording of them. The test is the same one that worked for the deck — *if a
  sentence would not make sense to someone who had NOT been on the call, do not write it.*

**And it must be generated against the live model before being believed.** §Lessons 42 is that a
prompt instruction is invisible to inspection: the SMS "concede the channel" line came back
verbatim in 5 of 5 drafts, and the CTX measurement of 2026-08-12 found the bodies clean and the
SUBJECT LINES 89% identical. Reading the prompt would have shown neither.

**Cost:** ~1 day, most of it generating drafts and reading them.

## Phase 3 — Granola API, IF the plan allows it

**Do not start this without confirming the plan.** One check settles it: Granola desktop →
Settings → Connectors → API keys. If there is no key to create, this phase does not exist.

Given a key, it is small, because Phase 1 built the storage:

- `GRANOLA_API_KEY` in `settings.py` (secret flag), poll `GET /v1/notes?created_after=…`
- Ride the dashboard's existing 5-minute poller, **not** `applypilot tick` — §Lessons 31: booking
  detection and deck pulling were both built as `tick` steps, `schedule.installed()` was False,
  and neither had ever fired once.
- Idempotent by construction: `id = sha256('granola', note.id)`, so re-reading the window is a
  no-op. §Lessons 65's `deck_views` counted POLLS and read 99 from one click; the same rolling
  re-read is the shape here.
- **Matching stays conservative.** Attach automatically only on an exact email match against a
  known contact (`matched_by='email'`). A name match is offered for confirmation and never
  written silently — §Lessons 68 is what a fuzzy match costs when both guards excuse each other.
- Notes matching nobody are stored unattached and listed, rather than dropped. §Lessons 15: a
  zero result must be as loud as an error.

**Cost:** ~1 day given a key. Unknown and unstartable without one.

## Not doing, and why

| | |
|---|---|
| **Reading Granola's local cache** | Reverse-engineered, undocumented, breaks on their updates — and unverifiable here, since Granola is not installed. §Lessons 3's shape: the two LinkedIn automations that were abandoned both looked cheap until they were load-bearing. |
| **Recording anything ourselves** | Out of scope and a different product. The app stores what the operator hands it. Consent for recording a call is the operator's to obtain, and keeping ApplyPilot on the storage side of that line is deliberate. |
| **The official Granola MCP server** | It connects a *chat client* to Granola. Useful for the operator (ask Claude to summarise a call), but it does not put a transcript in the CRM — and an MCP server that is only authenticated interactively is absent from headless runs. |
| **Auto-summarising a pasted transcript** | Phase 1 takes the summary Granola already wrote. An LLM call per paste is real money for a field the source hands over free. Add it only for `source='paste'` with no summary, and only if that case turns out to be common. |
| **Speaker attribution inside the body** | Not available (measurement 3). Anything claiming to know who said what would be inventing it. |

## The falsifier

**Phase 1 must not need a schema change to accept a second source.** If adding `otter` or a
plain paste from a phone call costs a migration, the `(source, external_id)` key was the wrong
shape. The test to write is the one `test_adding_a_space_needs_no_schema_change` already models:
define a source that exists nowhere in the codebase, drive it end to end, assert no migration ran.

## Open questions for the operator

1. **Which Granola plan?** Settings → Connectors → API keys. This decides whether Phase 3 exists.
2. **Is paste enough?** If the volume is a few calls a week, Phase 1 may be the whole feature and
   Phase 3 is never worth building.
3. **Should a transcript reach the DRAFTS at all**, or is it a record you read yourself? Phase 2
   is the expensive half and the one with the repetition and disclosure risks; Phase 1 stands
   alone without it.

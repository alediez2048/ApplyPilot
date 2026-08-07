# CTX-2 — Context and ask on the row

**Size:** M · **Depends on:** CTX-1 (block ordering) · **Status:** TODO
**PRD:** `docs/outreach-context-prd.md` §5, §6.1, §7

## Why

Everything the prompt knows about a job is **scraped**:

```python
# networking/outreach.py:477
jd = role_essentials(job.get("full_description"))
```

There is nowhere to put what the operator knows and the scraper cannot: that they met the Head
of Engineering at a meetup, that the team is rebuilding the thing they already built once, that
what they want here is an intro rather than a call.

The targets shape already has this tier — `_pitch_user_prompt` takes
`job.get("full_description")` as *"what the operator pasted about the company"* (`:531`). On
the jobs shape that same column holds the scraped posting. **A targets row has operator context
and a jobs row does not**, and that asymmetry is the whole ticket.

## Design

### Two fields, not four and not one

The operator named four things: the company, how they know them, why they fit, what they want.
Three land in the same paragraph of the email and do not need separating for the model. The
fourth is different in kind — it changes the **ask**, and the ask is currently decided by
`sched_block` and `deck_block`, so it needs its own slot to replace them from.

Four empty boxes per row is friction that guarantees a zero fill rate.

```python
# database.py, _ALL_COLUMNS  (:253)
"job_context": "TEXT",   # the company, how you know it, why you fit
"job_ask":     "TEXT",   # what you want from this person
```

**Additive dict, never a migration.** They race: `get_connection()` does not call `init_db`,
so `ensure_contacts_columns` can run first, and a migration touching a declared column is a
duplicate-column error one way and a NOT-NULL-without-default error the other. Nullable TEXT,
nothing to backfill.

### The ask REPLACES the CTA — it does not join it

`sched_block` already sets an ask ("invite them to book a call, here is the link"). A prompt
carrying both produces a draft that does both, badly. §Lessons 40: *two instructions in one
prompt disagreeing is a code bug, not a wording problem* — and the fix there was to replace the
ladder, not to say the other side louder.

So `job_ask` is composed **into** `sched_block`, not appended after it. The scheduling link
still appears when there is one; what changes is what the CTA asks for.

### Where it goes

Per PRD §5, new blocks in **bold**:

```
3  WHAT THE ROLE ACTUALLY INVOLVES          (scraped)
4  WHAT THE SENDER KNOWS ABOUT THIS COMPANY AND ROLE     job_context
5  WHAT THE SENDER NOTICED ABOUT THIS PERSON             noticed
6  THE PREMISE OF THIS CAMPAIGN                          space.offer   (CTX-1)
7  warm_block
8  sched_block + deck_block — CTA framing replaced by    job_ask
```

`job_context` above `noticed` deliberately: the existing comment says `noticed` *"takes
precedence over the posting"*, which stays true — what precedes it here is not the posting but
the operator's words about the company, and person-specific still reads closest to the write
instruction.

## The risk this ticket is built around

**One context field, five contacts, one company.** `job_context` is shared across every person
at that employer by construction, which makes it the likeliest block in the prompt to send five
colleagues the same sentence.

This has fired before, twice, with worse odds:

- §Lessons 42 — the SMS prompt's own concession sentence, *"hope a text is okay"*, appeared in
  **5 of 5** generated drafts. Reading the prompt would never have shown it.
- `burned_block` exists because ten people at Google received the identical subject line and
  one CTA sentence appeared 48 times across 189 drafts.

Mitigations, all three required:

1. The block says **facts, not phrasing** — the model is told to use what is there and never
   the operator's wording, and told to vary it per recipient.
2. `burned_block(previous)` must see drafts written from context, so the second contact at a
   company is shown what the first one got.
3. The guard test runs **against the live model**, not a stub.

## Scope / tasks

- [ ] Two columns in `_ALL_COLUMNS`. Capped at write — `job_context` ~1,200 chars, `job_ask`
      ~200. An ask that runs long is a second ask.
- [ ] Add both to `dashboard_rows()`'s SELECT. **No `if "col" in row.keys()` guard** — a
      column the payload needs belongs in the SELECT, and if it is absent the render must
      crash. That guard is what hid UX-4 for two rounds.
- [ ] Prompt blocks per §5, off-domain examples only (§Lessons 9).
- [ ] Compose `job_ask` into `sched_block`, replacing the default CTA framing.
- [ ] `draft_variant`: add a `ctx` bit (`outreach.py:269`). Without it the highest-leverage
      input in the system is the one input whose effect can never be measured — which is the
      ceiling `draft_variant` was built to lift.
- [ ] Job-tab UI: one collapsed `<details>`, two textareas, a `✓ used in N drafts` indicator
      that goes stale-amber when the context is newer than the drafts.
- [ ] Write path carries the Space. §Lessons 70 — a Space is only as separate as its WRITE
      paths, and the read filter hides the gap. `insert_imported` and `add_target` are the two
      that already exist; this is the third thing that writes a `jobs` row from the dashboard.
- [ ] Re-draft **unsent** drafts only. A sent draft is the only record of what actually went
      out — the `deck-relink` rule.
- [ ] Verify the textarea survives the 2.5s refresh **on the real page**. `#jobs` is replaced
      wholesale and `isEditingJobs()` (`dashboard.js:1857`) only holds off while a field HAS
      focus. A comment at `dashboard.js:1121` already flags moving between fields as the sharp
      edge.

## Not in scope

- **The résumé and cover letter.** "Why I'm a good fit" is exactly what the cover letter wants,
  and it is guarded by `preserved_*` hard failures, `understated_experience` and the
  fabrication judge. Separate ticket, much larger blast radius.
- **The apply agent.** `job_context` is what a cover-letter field on an application form wants,
  and it would ride into a `bypassPermissions` browser on attacker-controlled pages. Not here.
- **Targets rows.** They have `full_description` doing this job. Two fields meaning the same
  thing on one shape is how `_BOARD_SITES` and `_BOARD_NAMES` drifted (§Lessons 49). The UI
  does not render these on a targets row.
- **LLM-generated context.** The point is that this is what the operator knows and the scraper
  cannot.

## Tests

- [ ] `test_operator_context_is_never_parroted` — **the one that decides whether this feature
      is good or embarrassing.** Five contacts at one job, real model, real context. Assert no
      run of ≥8 words from `job_context` appears verbatim in any draft, and no sentence is
      shared between two drafts. Must not be stubbed: §Lessons 42 was invisible to inspection
      and only appeared when drafts were generated against real data.
- [ ] `test_the_ask_replaces_the_default_cta` — with `job_ask` set, assert the default
      book-a-call framing is **absent**, not merely outnumbered. Mutation: appending instead of
      replacing must fail this.
- [ ] `test_the_scheduling_link_survives_a_custom_ask` — replacing the framing must not drop
      the URL.
- [ ] `test_context_is_absent_when_empty` — no heading, no empty block. Assert on the heading
      string, not on `context in prompt`, which is True for `""` (§Lessons 71).
- [ ] `test_a_sent_message_is_never_rewritten` — edit context on a job with one sent and one
      unsent draft; assert exactly one changed.
- [ ] `test_draft_variant_records_context` — `+ctx` present with context, absent without.
- [ ] `test_no_payload_key_is_silently_optional` — the existing UX-4 guard already scans for
      the defensive-read pattern; both new columns must be in the SELECT.
- [ ] `test_the_query_budget_does_not_move` — 74 against `MAX_STATEMENTS = 80`
      (`tests/test_query_budget.py:26`). Two more columns on a row already SELECTed costs zero
      statements; the payload grows, the budget must not.
- [ ] Mutation-verified, `__pycache__` cleared first (§Lessons 16).

## Decide before starting

1. **Auto-redraft on save, or a button?** Auto is fewer clicks and spends credits on every
   save. A button is one more click and honest about cost. *Leaning: button.*
2. **Does an empty `job_ask` fall back to the default CTA, or to nothing?** *Leaning: default —
   a row with no ask must behave exactly as it does today.*

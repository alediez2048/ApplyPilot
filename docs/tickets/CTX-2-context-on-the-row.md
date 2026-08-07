# CTX-2 — Context and ask on the row

**Size:** M · **Depends on:** CTX-1 (block ordering) · **Status:** DONE 2026-08-07
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

- [x] `job_context` + `job_ask` in `_ALL_COLUMNS`. Capped at the write (1200 / 200) in
      `repo.set_context`, and again where `draft_email` reads them — a job dict can reach that
      function from a caller that never went through the repo.
- [x] Both added to `dashboard_rows()`'s SELECT and read straight off the row. **No
      `if "col" in row.keys()` guard** — that defensive read is what hid `interview_at` from the
      browser for two rounds while the write worked perfectly (§Lessons 47).
- [x] Context block above `noticed`, below the scraped posting, and told the operator's words
      outrank the posting where they disagree.
- [x] `job_ask` composed INTO `sched_block`, replacing the default CTA. The scheduling link
      survives — what the operator overrides is what to ask for, not whether a calendar exists.
- [x] `draft_variant` gained `ctx` and `ask`. Unlike `premise` these VARY across rows of one
      Space, so they are the first inputs that can be compared inside a campaign rather than
      only before/after.
- [x] `/api/job/context` + `_save_job_context`. Writes only the fields the client SENT; a
      missing key means "this caller did not render that box", never "cleared".
- [x] Job-tab `<details>`, open when empty, collapsed once filled. Both textareas rendered even
      when empty rather than described (§Lessons 41).
- [x] Staleness read off `draft_variant` rather than a timestamp — **no third column**, and it
      reports what actually went into each draft rather than what existed when it was made.
- [x] Sent drafts counted separately and never offered for regeneration.
- [x] Unsaved text held in `CTX_FORM`, the `ADD_FORM` pattern. `isEditingJobs()` only holds the
      refresh off while a field HAS focus, so clicking from the textarea to anything that is not
      an input hands the next 2.5s tick a paragraph to destroy.
- [x] Verified live on the real Peak6 row after a reinstall and restart; DB backed up first with
      the sqlite backup API (`applypilot-20260807-pre-ctx2.db`) — the WAL was 4.1 MB against a
      1.8 MB main file, so `cp` would have lost everything recent.

### Decisions taken

1. **Button, not auto-redraft.** Regenerating eight drafts the moment somebody stops typing
   spends real credits on a paragraph they may still be editing. `test_saving_does_not_redraft`
   pins it.
2. **An empty ask falls back to the default CTA**, so a row with no ask behaves exactly as it
   did before CTX-2.
3. **Staleness is derived, not stored.** Known limit, stated rather than discovered: editing the
   context after a draft was written leaves that draft tagged `ctx`, so this counts "used SOME
   context", not "used THIS context". It catches the case that matters — context typed onto a
   job that already has drafts, which is every job in the table today.

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

`tests/test_job_context.py`, 32 tests. 30 passed on the first run — §Lessons 13 says that is
when to distrust them, and mutation testing then found **two survivors**, both real gaps:

- `set_context` clearing an unshown field was only tested in ONE direction, so a mutation
  making the *context* branch fire on `None` survived. Both directions now.
- The failed-save path was never exercised at all, so dropping the typed buffer on failure —
  which loses the paragraph AND re-renders the stale server copy — went unnoticed. Writing that
  test also caught a mistake in the test itself: `saveJobContext` reads the DOM and never
  populates `CTX_FORM`, so the first version asserted against a buffer nothing had filled.

Highlights:

- [x] `test_the_ask_replaces_the_default_call_booking` — the default framing must be **absent**,
      not outnumbered. Mutation: appending instead of replacing fails it.
- [x] `test_the_scheduling_link_survives_a_custom_ask` — guards what the fix above could break.
- [x] `test_the_context_is_not_the_premise` — per-ROW against per-SPACE, both directions.
- [x] `test_no_context_adds_nothing` — asserted on the HEADING, never `context in prompt`
      (§Lessons 71).
- [x] `test_ctx_is_matched_as_a_whole_token` — `ctxfoo` is not `ctx` (§Lessons 1).
- [x] `test_both_columns_are_additive_not_a_migration` — reads every `migrations/m*.py` and
      asserts neither column is named there. The dicts and a migration RACE.
- [x] `test_a_failed_save_keeps_the_paragraph` / `test_a_successful_save_lets_go_of_the_buffer`
      — the second guards the first, which would pass against a buffer never cleared.
- [x] `test_the_context_is_escaped_into_the_textarea` — `</textarea>` cannot break out.
- [x] **Mutation-verified, 17 of 17 killed** after the two fixes, `__pycache__` cleared between
      each (§Lessons 16).

Suite **1594 passed, 1 skipped**. Query budget unmoved — two columns on a row already SELECTed
cost no statement, and `_context_use` reads contacts the payload already holds. ruff and eslint
clean.

## Still open

- **The parroting measurement.** `test_the_context_is_told_it_is_facts_and_not_phrasing` asserts
  the INSTRUCTION is in the prompt, which is not the same as the model obeying it. The real
  check is 8 drafts at one company generated against the live model and counted for shared
  sentences — the Peak6 row is exactly that shape and is waiting for context to be typed into
  it. Until then this feature is unproven in the only way that matters (§Lessons 42 was
  invisible to inspection).

## Decide before starting

1. **Auto-redraft on save, or a button?** Auto is fewer clicks and spends credits on every
   save. A button is one more click and honest about cost. *Leaning: button.*
2. **Does an empty `job_ask` fall back to the default CTA, or to nothing?** *Leaning: default —
   a row with no ask must behave exactly as it does today.*

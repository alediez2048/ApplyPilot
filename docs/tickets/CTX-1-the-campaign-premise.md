# CTX-1 — The campaign premise never reaches a jobs draft

**Size:** S/M · **Depends on:** nothing · **Status:** DONE 2026-08-07
**PRD:** `docs/outreach-context-prd.md` §1, §6.2
**Reported:** 2026-08-07 — *"the same message for job applications won't be the same as gauntlet"*

## Diagnosis

The field exists. It is documented for exactly this case. One of the two shapes reads it.

```python
# domain/space.py:103
#: The constant pitch (§7.1). In a job search the DESCRIPTION varies per row and the pitch
#: is constant; in an outreach Space that inverts, so this lives here and not on a row.
offer: str = ""
```

That docstring describes a **job search**. Now the dispatch:

```python
# networking/outreach.py:528
if shape == "pipeline/targets":
    user = _pitch_user_prompt(sender_bits, contact, company,
                              job.get("full_description"),
                              (getattr(space, "offer", "") or "").strip(),   # ← here
                              noticed, sched_block, deck_block, style_block,
                              tone_block, previous)
    system = _PITCH_SYSTEM
else:
    user = _job_user_prompt(sender_bits, contact, relationship, role, company, jd,
                            noticed, sched_block, deck_block, warm_block,
                            style_block, tone_block, previous)   # ← no offer
    system = _SYSTEM
```

`_job_user_prompt` has no `offer` parameter (`:391`). A jobs Space can carry a premise and
nothing will ever read it.

**Why this passed every guard.** `UNAPPLIED` at `space.py:75` is empty and
`test_unapplied_fields_are_really_unapplied` parses attribute access to hold that honest — it
even survived a mutation reading `manifest.tone` instead of `space.tone`. Both correct. The
guard asks *"is this field read anywhere?"*, and the answer is yes, on one shape. §Lessons 49:
a rule implemented at one of its two call sites is not implemented.

### What the operator does instead today

Types a one-off `style` directive on every regeneration and remembers what was typed last
time. That is the **per-run tier doing a per-campaign job**, and it is unrepeatable by
construction — nothing stores it, so `metrics.by_variant` pools every Gauntlet email with
every Job Search email under `style` and the comparison can never be made.

### Live state

| Space | shape | jobs | premise readable |
|---|---|---|---|
| `job-search` | `pipeline/jobs` | 30 | ❌ |
| `gauntlet` | `pipeline/jobs` | 1 | ❌ |
| `partnerships` | `pipeline/targets` | 0 | ✅ |

The only Space where the premise works holds no rows.

### Second defect: there is no way to TYPE a premise on a jobs Space

Found while sizing this ticket, and it is why the size is S/M rather than S.

The backend is already shape-agnostic — `_save_offer` (`web_dashboard.py:2359`) has no shape
guard, `/api/space/offer` accepts any Space, and `repo.spaces.save()` persists it. The block is
purely in the UI:

```html
<!-- static/index.html:93 -->
<div class="controls" id="targetControls" hidden>
  ...
  <textarea id="offerInput" placeholder="One paragraph: what you are proposing…"></textarea>
```

```js
// dashboard.js:134  renderSpaceShape()
if (tgt) tgt.hidden = !targets;      // #offerInput lives inside #targetControls
```

So on `job-search` and `gauntlet` the box is not merely unlabelled, it is **not on the page** —
and `[hidden]{display:none !important}` makes that real (§Lessons 62). Worse, the hint beneath
it argues against ever showing it there:

> *"In a job search the posting varies per row and your pitch is constant. Here it inverts…"*

That sentence is true about `full_description` and false as a reason to withhold the field. Both
halves have to move: the control out of `#targetControls`, and the copy rewritten so it does not
contradict the ticket.

**Wiring the prompt without this ships a field nobody can fill** — §Lessons 43, five occurrences,
every one reported as *"it does nothing"*.

## Scope / tasks

- [x] Added a `premise` parameter to `_job_user_prompt` and passed it from the jobs branch.
      Named `premise`, not `offer`: in this prompt that is what it IS, and the ticket already
      records that the field's name is worse than the thing.
- [x] New block, **THE PREMISE OF THIS CAMPAIGN**, below `noticed` and above the CTA blocks —
      the more general of the two operator inputs, so person-specific still reads closest to
      the instruction to write.
- [x] Framed as substance, not voice, with `test_the_premise_is_not_the_tone` holding the two
      apart. Rendering one from the other would have passed every other test in the file.
- [x] **No worked example in the block.** §Lessons 9. The repetition rule is stated as a rule.
- [x] ~~Regenerate the golden file~~ — **it did not move, and that is the better outcome.**
      See the correction below.
- [x] Moved `#offerInput` out of `#targetControls` into `#premiseControls`, which no shape
      hides. No backend change needed: `_save_offer` never had a shape guard.
- [x] Heading, placeholder and hint per shape, from `space.OFFER_COPY` / `offer_copy()` in
      `domain/space.py`, shipped on the payload as `space_offer_copy`. The old hint argued the
      field was a targets concern; rewritten.
- [x] `_save_offer`'s confirmation uses the label above the box — "Offer saved." is the wrong
      word on a jobs Space.
- [x] `draft_variant` gained a `premise` bit. Not in the original scope; added because the
      argument for `+ctx` in CTX-2 applies identically, and tagging has to start at the first
      premise-driven send or the before/after comparison is lost.
- [ ] **Write the premise for `gauntlet` and `job-search`.** Left to the operator — the whole
      point of the field is that it says something only they know. The mechanism is verified
      end to end against the real Peak6 row (below); nothing was written to either Space.

### Correction: the golden file did not need regenerating

The ticket assumed the block always renders and therefore that
`tests/golden/jobs_outreach_prompt.txt` would move. It does not: the block is conditional on a
non-empty premise, exactly as `noticed` and `tone_block` are, so a Space without one produces
the byte-identical prompt it produced before CTX-1.

That is strictly better than a regenerated baseline. `test_the_jobs_prompt_matches_the_fixed_baseline`
and `test_a_default_space_changes_the_prompt_by_nothing` both still pass **untouched**, which
proves the change is additive rather than asking a reader to believe it — and 30 live jobs'
worth of outreach is provably unchanged until somebody types a premise.

### Verified live

Against the real Gauntlet row (`Solutions Engineer @ Peak6`) and its real contact, with the
prompt captured rather than sent:

```
premise present  : True
premise absent   : True          (same job, same contact, no premise)
delta            : 920 chars
position         : after WHAT THE SENDER NOTICED, before SCHEDULING LINK
```

## Not in scope

- No new column, no migration, no manifest field. If this ticket adds one, the diagnosis was
  wrong.
- Not `job_context` — that is CTX-2 and needs this ticket's block ordering decided first.
- Not the SMS or reply paths — they cannot see a manifest at all. CTX-3.

## Tests

`tests/test_premise.py`, 22 tests. **All 22 passed on the first run**, which §Lessons 13 says is
exactly when to distrust them, so all twelve mutations below were run before believing any of it.

- [x] `test_two_jobs_spaces_produce_different_prompts` — the objective, asserted on the artifact.
      Each premise present in its own prompt and **absent** from the other's; comparing the two
      prompts to each other proves only that they differ (§Lessons 60).
- [x] Both premises asserted non-empty first. `"" in prompt` is True for every string, which
      shipped three separate times in one session (§Lessons 71).
- [x] `test_an_empty_premise_adds_nothing` / `test_whitespace_is_not_a_premise` — asserted on the
      HEADING, never on `offer in prompt`.
- [x] `test_the_premise_is_not_the_tone` — both directions.
- [x] `test_the_premise_is_told_not_to_be_reused_verbatim` — the §Lessons 42 instruction is
      present in the block, not merely intended.
- [x] `test_the_targets_offer_still_works` — the path that already read the field did not move,
      and the jobs heading does not leak into the pitch prompt.
- [x] `test_every_shape_has_copy_for_the_box`, `test_an_unknown_shape_falls_back_to_a_label`,
      `test_offer_copy_hands_back_a_copy` — the label cannot go blank, and a caller cannot
      mutate the shared dict the payload ships.
- [x] `test_the_confirmation_uses_the_label_above_the_box`.
- [x] Browser half under Node with **real nodes**, not `el()` stubs that swallow every write
      (§Lessons 41): the panel is unhidden on both shapes, labelled from the payload, and the
      2.5s refresh still does not eat a paragraph mid-sentence.
- [x] `test_the_markup_no_longer_hides_the_box_behind_the_shape` — reads the shipped HTML and
      asserts the textarea has LEFT `#targetControls`. `hidden` is a user-agent rule any author
      `display` beats (§Lessons 62), so the attribute's absence is the assertion.
- [x] **Mutation-verified, 12 of 12 killed**, `__pycache__` cleared between each (§Lessons 16):
      dropping `offer` at the call site · rendering the block unconditionally · removing the
      variant bit · collapsing `offer_copy` to one wording · returning the live dict · restoring
      "Offer saved." · emptying the payload key · not setting the title · not setting the
      placeholder · re-hiding the panel on jobs · overwriting a focused textarea · renaming the
      textarea back out of the panel.

Full suite **1562 passed, 1 skipped**; ruff and eslint clean.

## Open question carried to CTX-2

`offer` will now mean "what I'm selling" on one shape and "why I want this" on the other.
Renaming costs a manifest field and a config-blob path. Leaning: keep the field, label it per
shape, and record here that **the name is worse than the thing** so the next reader does not
have to rediscover it.

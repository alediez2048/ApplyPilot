# CTX-1 — The campaign premise never reaches a jobs draft

**Size:** S/M · **Depends on:** nothing · **Status:** TODO
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

- [ ] Add an `offer` parameter to `_job_user_prompt` and pass it from `outreach.py:538`.
- [ ] New block, **THE PREMISE OF THIS CAMPAIGN**, positioned per PRD §5 — below `noticed`,
      above `warm_block`. It is the most general fact in the prompt, so it sits furthest from
      the write instruction, mirroring why `tone_block` is last.
- [ ] Frame it as **substance, not voice**, and say so in the block: `tone_block` is already
      in this prompt and the two must not read as the same instruction (§Lessons 40). The
      premise says *what is true of every role in this Space*; tone says *how to sound*.
- [ ] **No worked example in the block.** §Lessons 9, three occurrences — an example in the
      candidate's own domain comes back verbatim. If an example is unavoidable it is
      off-domain with `N` placeholders.
- [ ] Regenerate `tests/golden/jobs_outreach_prompt.txt` **in its own commit**, with the diff
      read rather than accepted. That file exists to make exactly this change visible.
- [ ] **Move `#offerInput` out of `#targetControls`** into a shape-neutral section, so it
      renders on both shapes. No backend change — `_save_offer` already accepts any Space.
- [ ] Label and hint per shape:
      `pipeline/jobs` → **"The premise of this campaign"**;
      `pipeline/targets` → **"What you're offering"** (unchanged).
      The strings live beside `TEMPLATE_BLURB` in `domain/space.py`, not in the HTML, so the
      panel cannot describe a field differently from what the manifest does.
- [ ] Rewrite the hint under the box. The current one explains why the field is a *targets*
      concern, which is the sentence this ticket disproves.
- [ ] Write the premise for `gauntlet` and for `job-search`, and read one real draft from each.

## Not in scope

- No new column, no migration, no manifest field. If this ticket adds one, the diagnosis was
  wrong.
- Not `job_context` — that is CTX-2 and needs this ticket's block ordering decided first.
- Not the SMS or reply paths — they cannot see a manifest at all. CTX-3.

## Tests

- [ ] `test_two_jobs_spaces_produce_different_prompts` — **the objective, asserted on the
      artifact.** Same job, same contact, two Spaces with different premises; assert each
      premise appears in its own prompt and is **absent** from the other's. Comparing the two
      prompts to each other proves only that they differ (§Lessons 60 — the first version of
      `test_a_default_space_changes_the_prompt_by_nothing` compared two moving things and
      survived a mutation that leaked a field into both paths).
- [ ] **Assert both premises are non-empty first.** `"" in prompt` is True for every string;
      that exact shape shipped three times in one session last week (§Lessons 71).
- [ ] `test_a_default_space_changes_the_prompt_by_nothing` — must still pass. A Space with no
      premise produces the byte-identical string it produced before Spaces existed.
- [ ] `test_the_premise_is_not_the_tone` — set `tone` and leave `offer` empty; assert the
      premise block is absent. Then the reverse. A mutation that renders one from the other
      must fail.
- [ ] `test_the_golden_file_moved_once` — the regenerated file contains the premise heading
      when a premise is set and not otherwise.
- [ ] `test_the_premise_box_renders_on_a_jobs_space` — assert the control **exists**, not that
      the copy is right. §Lessons 41: a render test that asserts on a sentence passes happily
      for a panel showing nothing but the right words. And `hidden` is a user-agent rule that
      any author `display` beats (§Lessons 62), so assert the attribute is absent rather than
      that a property computes.
- [ ] Mutation-verified: deleting the `offer` argument at the call site kills
      `test_two_jobs_spaces_produce_different_prompts`. Clear `__pycache__` before believing a
      contradictory result (§Lessons 16 — a same-second, same-length edit is invisible to the
      bytecode cache).

## Open question carried to CTX-2

`offer` will now mean "what I'm selling" on one shape and "why I want this" on the other.
Renaming costs a manifest field and a config-blob path. Leaning: keep the field, label it per
shape, and record here that **the name is worse than the thing** so the next reader does not
have to rediscover it.

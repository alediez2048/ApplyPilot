# PRD — Outreach context: four tiers, one cascade

**Status:** Draft v1 · 2026-08-07
**Owner:** Jorge · **Author:** Jorge + Claude
**Ticket prefix:** `CTX-*`
**Branch:** `context`, off `spaces` @ `1e923f8`
**Relationship to `spaces-prd.md`:** this finishes what §7.1 started. That document gave a
Space one constant pitch (`offer`) and wired it into one of the two shapes. This wires it into
the other, and adds the per-row tier neither document has.

---

## Headline decisions (read first)

1. **This is not a new architecture. It is a missing tier in one that already works.**
   `draft_email` already composes operator-controllable context from three layers — the Space's
   `tone`, a per-run `style`, and the per-contact `noticed`. There is no per-JOB layer, and
   everything the prompt knows about a job is scraped (`role_essentials(full_description)`).
   The cascade has a hole in the middle, not a missing floor.

2. **The Space-level substance field already exists and is dead on the jobs path.** `offer` is
   documented as *"in a job search the DESCRIPTION varies per row and the pitch is constant"* —
   exactly the case this PRD is about — but `draft_email` hands it only to
   `_pitch_user_prompt`. The jobs branch never receives it. **Wire the existing field; do not
   add a fifth free-text box.** Three boxes an operator must distinguish (`tone`, `offer`, and
   a new one) is a *which box does this go in?* problem, and the failure mode of an ambiguous
   box is an empty box.

3. **The never-changes half belongs to the IDENTITY, not the Space.** The intro deck, the
   scheduling link, the mailbox, the from-name and the signature identify the **sender**.
   `identities` already holds all but one of them and is read by nothing (ID-1). Two Spaces on
   one mailbox share one deck and one calendar; putting them on the Space stores them twice and
   lets them drift. This is the same argument `domain/space.py` already makes for
   `company_cap`: *the recipient does not know what a Space is.*

4. **Creating a Space must not ask for the unchanging things — it inherits them.** Four config
   fields on the moment someone wants a new tab is friction paid every time, for values that
   are identical every time. The create dialog picks an identity. Today there is one
   (`personal`) and it already has the answers.

5. **Operator context is FACTS, never PHRASING, and this field is the worst offender in the
   system.** §Lessons 9 and 42: the SMS prompt's own concession sentence came back in **5 of 5**
   drafts, and `burned_block` exists because ten people at Google got an identical subject line.
   Job context is shared across *every contact at that company by construction*, which makes it
   structurally the likeliest block in the prompt to produce five identical sentences to five
   colleagues. Designing against that is not polish; it is the feature working or not.

6. **Two of the six draft entry points cannot see a manifest at all.** `draft_email`,
   `draft_followup`, `draft_for_channel` and `draft_linkedin_followup` take `space`.
   **`draft_reply` and `draft_sms` do not** — so `tone` does not reach a text or a reply today,
   and neither will anything this PRD adds until that is fixed. Measured, not assumed.

---

## 1. Problem

`job-search` holds 30 jobs and `gauntlet` holds 1. Both are `pipeline/jobs`. Both send the same
message.

Nothing distinguishes them in a draft, because the only Space-level lever that reaches a jobs
prompt is `tone`, and tone is **voice, not substance** — it changes how a sentence sounds, not
what the sentence claims. "Why I want this" for a Gauntlet role and for a staff engineering
posting are different *facts*, and there is nowhere to put either one.

The asymmetry is sharpest between the two shapes:

| | jobs shape | targets shape |
|---|---|---|
| Per-row operator context | **nothing** | `full_description` — what the operator pasted about the company |
| Constant campaign pitch | `offer`, declared and unread | `offer`, wired |

A targets Space already has both tiers. A jobs Space has neither. That asymmetry is the bug,
and it is why the same machine writes the same email for two campaigns that share nothing but
a schema.

## 2. Thesis

**Context cascades from the sender, through the campaign, through the row, to the person — and
the more specific layer always wins.**

Four tiers, each answering a different question, each changing at a different rate:

| Tier | Answers | Changes | Lives in |
|---|---|---|---|
| **Identity** | Who is sending? | ~never | `identities` — deck, calendar, mailbox, signature, limits |
| **Space** | What is this campaign about? | per campaign | `spaces.config` — `tone` (voice) + `offer` (substance) |
| **Job** | What do I know about *this* company and role? | per row | **new** — `job_context` + `job_ask` |
| **Contact** | What do I know about *this* person? | per person | `contacts.noticed` |

Three of the four exist. One tier is missing and one field is unwired.

## 3. Non-goals

Considered and rejected for *this* document, not forever.

- **No résumé or cover-letter changes.** "Why I'm a good fit" is exactly what the cover letter
  wants, and the tailor path sees only the base résumé and the JD. It is also guarded by the
  `preserved_*` hard failures, `understated_experience`, and the fabrication judge. Real work,
  much larger blast radius, and it does not need to ship in the same change to be correct.
- **No ID-1.** The identity half is *described* here (§6.3) so the boundary is written down,
  and *built* elsewhere. Nothing in CTX-1..3 depends on it. Today the deck URL and calendar
  link are global and identical across all three Spaces, which is correct behaviour for one
  sender — making them per-Space buys nothing until a second sender exists.
- **No new template.** Gauntlet and Job Search stay `pipeline/jobs`. If the difference between
  two campaigns needs a template, the manifest claim was wrong; see §10.
- **No auto-send behaviour change.** `can_autosend` is untouched. Context changes what a draft
  says, never whether it goes.
- **No per-contact tier beyond `noticed`.** It exists, it works, it has a UI box. Leave it.
- **No LLM-generated context.** The whole point is that this is what the *operator* knows and
  the scraper cannot. A model filling it in reproduces the state we are trying to leave.

## 4. What exists already — measured

Read off `outreach.py` on 2026-08-07, not assumed:

```
draft_email(profile, job, contact, style="", warm=False, previous=None, space=None)
  ├── tone_block     ← space.tone           placed LAST, before "Write the email"
  ├── style_block    ← _resolve_style()      SCHEDULING_LINK/profile/one-off arg
  ├── noticed        ← contact["noticed"]    capped at 400 chars
  ├── jd             ← role_essentials(job["full_description"])   SCRAPED
  ├── sched_block    ← _scheduling_link()    SCHEDULING_LINK env → profile.personal
  ├── deck_block     ← _intro_deck_url()     INTRO_DECK_URL env → profile.personal
  ├── warm_block     ← 1st-degree LinkedIn
  └── burned_block(previous)                 what this employer was already sent
```

Entry points and whether they receive the manifest:

| Function | takes `space` |
|---|---|
| `draft_email` | ✅ |
| `draft_followup` | ✅ |
| `draft_for_channel` | ✅ |
| `draft_linkedin_followup` | ✅ |
| `draft_reply` | ❌ |
| `draft_sms` | ❌ |

`tone` therefore does not reach a text message or a reply. That is pre-existing and is CTX-3.

## 5. The cascade, and what wins

One rule, stated once, so it does not have to be rediscovered per block:

> **Facts before direction. More specific replaces more general. The instruction to write comes
> last.**

"Replaces" is load-bearing and is §Lessons 40: *two instructions in one prompt disagreeing is a
code bug, not a wording problem.* Strengthening the wording of the losing block does not fix it.
The concrete case here is `job_ask` — it must **replace** the CTA framing inside `sched_block`,
not sit next to it. A prompt that says "invite them to book a call" and "ask whether they can
point you at the hiring manager" produces a draft that does both, badly.

Proposed order for `_job_user_prompt` (new blocks in **bold**):

```
1  SENDER
2  TARGET CONTACT
3  JOB APPLIED TO  +  WHAT THE ROLE ACTUALLY INVOLVES        (scraped)
4  WHAT THE SENDER KNOWS ABOUT THIS COMPANY AND ROLE         job_context
5  WHAT THE SENDER NOTICED ABOUT THIS PERSON                 noticed
6  THE PREMISE OF THIS CAMPAIGN                              space.offer
7  warm_block
8  sched_block  +  deck_block   — CTA framing replaced by    job_ask
9  style_block
10 tone_block                                                 (unchanged, last)
11 burned_block(previous)
12 "Write the outreach email. Return the JSON."
```

Two placements are deliberate and worth arguing with before building:

- **`job_context` above `noticed`** (4 before 5) even though `noticed` is more specific. The
  existing comment on `noticed` says it *"takes precedence over the posting"* — that stays true;
  what precedes it here is not the posting but the operator's own words about the company.
  Person-specific still reads last of the two and therefore closest to the instruction.
- **`space.offer` below both** (6). The campaign premise is the most general fact in the prompt,
  so it goes furthest from the write instruction, mirroring why `tone_block` sits at 10.

## 6. Data model

### 6.1 The job tier — two columns

```python
# database.py, _ALL_COLUMNS
"job_context": "TEXT",   # what the operator knows: the company, how they know it, why they fit
"job_ask":     "TEXT",   # what they want from this person — replaces the default CTA framing
```

**In the additive dict, never a migration.** They race: `get_connection()` does not call
`init_db`, so `ensure_contacts_columns` can run first, and a migration touching a declared
column is a duplicate-column error one way and a NOT-NULL-without-default error the other. Both
are nullable TEXT, so there is nothing to backfill.

**Two fields, not four and not one.** The operator named four things — the company, how they
know them, why they fit, what they want. Three of those land in the same paragraph of the email
and do not need separating for the model. The fourth changes the **ask**, and the ask is
currently decided by `sched_block` and `deck_block`, so it needs its own slot to replace them
from. Four empty boxes per row is friction that guarantees a zero fill rate.

Capped at write, like `noticed` and the snippet fields. `job_context` ~1,200 chars, `job_ask`
~200 — an ask that runs long is a second ask.

### 6.2 The Space tier — no schema change

`offer` already exists on the manifest and rides in the `config` JSON blob. CTX-1 passes it to
`_job_user_prompt`. The UI labels it per shape, because the same slot means two things:

| shape | label | placeholder |
|---|---|---|
| `pipeline/jobs` | **The premise of this campaign** | *What you're looking for and why — the thing that's true of every role in this Space.* |
| `pipeline/targets` | **What you're offering** | *(unchanged)* |

### 6.3 The identity tier — described, not built

`identities` (migration 003, read by nothing) already holds:

```
id · name · token_path · from_name · from_address · signature_html
deck_base_url · deck_collector_url · deck_collector_token · daily_limit · created_at
```

One field on the never-changes list is missing: **`scheduling_link`**, which today resolves
`SCHEDULING_LINK` env → `profile.personal.scheduling_link`. Adding it is ID-1's, not this
document's — recorded here so ID-1 does not have to rediscover it.

**`identity_id` freezes on first send.** Anything that writes an identity before ID-1 ships is a
decision that cannot be taken back from the UI.

## 7. What the operator sees

**Job tab, under the posting links.** One `<details>` block, collapsed when empty, labelled
with whether it is in play — the `noticed` box already established this pattern and its
`✓ in the draft` indicator is the part that makes an invisible input visible.

```
▾ Context for this application                      ✓ used in 3 drafts

  What you know about this company and role
  ┌────────────────────────────────────────────────────────┐
  │ Met their Head of Eng at the Austin AI meetup in June.  │
  │ They're rebuilding intake on agents — same problem I    │
  │ hit at T-Mobile.                                        │
  └────────────────────────────────────────────────────────┘

  What you want from this person
  ┌────────────────────────────────────────────────────────┐
  │ An intro to whoever owns the intake rebuild.            │
  └────────────────────────────────────────────────────────┘

  Facts, not phrasing — this gets rewritten for each person, never pasted.
  Changing this re-drafts unsent messages. Sent ones are never touched.
```

Two constraints from prior scar tissue:

- **The `refresh()` hazard.** `#jobs` is replaced wholesale every 2.5s and `isEditingJobs()`
  only holds off while an input **has focus**. A textarea that loses focus mid-thought gets
  destroyed. `noticed` lives inside a contact panel for this reason; the job-level box needs the
  same treatment or an explicit save, and this is the first thing to check on the real page
  rather than in a render test.
- **Describing a control is not showing it** (§Lessons 41). Empty state renders the boxes,
  disabled or not, never a sentence about boxes that would appear.

## 8. Phases

Ordered so the stated objective — *Gauntlet should not sound like Job Search* — is met by the
first ticket, before any schema change exists.

| Ticket | What | Size | Notes |
|---|---|---|---|
| **CTX-1** | `space.offer` → `_job_user_prompt`, relabelled per shape | S | No schema. **Changes `tests/golden/jobs_outreach_prompt.txt` deliberately** — that file exists to make exactly this visible |
| **CTX-2** | `job_context` + `job_ask`: 2 additive columns, Job-tab UI, prompt blocks, `+ctx` variant bit | M | The hyper-customisation tier |
| **CTX-3** | `space` onto `draft_reply` + `draft_sms`; context into all six entry points | S/M | Closes the gap §4 measured. Fixes `tone` not reaching texts, which predates this PRD |
| **CTX-4** | *(deferred, = ID-1's slice)* identity-owned deck + calendar + `scheduling_link` column | — | Not on this critical path. Do not build a business Space before it |

CTX-1 alone answers the objective. CTX-2 is the larger prize and the larger risk.

## 9. How this is proven, not claimed

Mirrors `test_adding_a_channel_needs_no_schema_change` and
`test_adding_a_space_needs_no_schema_change`, both of which caught the one line where their
claim was false.

- **`test_two_jobs_spaces_produce_different_prompts`** — the objective itself, asserted on the
  prompt **artifact**, not on two drafts compared to each other (§Lessons 60: comparing two
  moving things proves nothing). Same job, same contact, two Spaces with different premises;
  assert each premise appears in its own prompt and **not** in the other's. Guard the
  non-emptiness of both premises first — `"" in prompt` is True for every string, which is
  §Lessons 71 and was hit three times in one session last week.
- **`test_operator_context_is_never_parroted`** — the §Lessons 42 guard, and the one that
  decides whether this feature is good or embarrassing. Generate drafts for five contacts at
  one job against real data; assert no run of ≥8 words from `job_context` appears verbatim in
  any draft, and no sentence is shared across two drafts. **This must be run against the live
  model, not a stub** — reading the prompt would never have caught the SMS case.
- **`test_the_ask_replaces_the_default_cta`** — mutation: with `job_ask` set, assert the default
  "invite them to book a call" framing is **absent** from the prompt, not merely outnumbered.
- **`test_context_reaches_every_channel`** — parametrised over all six entry points. Its
  predecessor is the 2026-08-03 follow-up bug, where touch 2 could not see what touch 1 said.
- **`test_a_sent_message_is_never_rewritten`** — editing context re-drafts unsent drafts only.
  The `deck-relink` precedent: a sent draft is the only record of what actually went out.
- **`test_draft_variant_records_context`** — `+ctx` in the signature. Without it, the
  highest-leverage input in the system is also the one input whose effect can never be measured,
  which is the ceiling `draft_variant` was built to lift.
- **`test_the_query_budget_does_not_move`** — 74/80 today. Two more columns on a row
  `dashboard_rows()` already SELECTs costs zero statements; the payload grows, the budget must
  not.
- **`test_no_prompt_example_can_be_lifted`** — the existing résumé-side guard, extended. Any
  worked example added to the new blocks must be off-domain (§Lessons 9, three occurrences).

## 10. Risks

| Risk | Mitigation |
|---|---|
| **Five colleagues get the same sentence.** The structural failure mode: one context field, five contacts, one company. | The block is framed as facts and explicitly forbidden as phrasing; `burned_block` must see it. `test_operator_context_is_never_parroted`, run live. |
| **`job_context` contradicts `warm_block`.** "How I know them" vs a computed 1st-degree relationship framing. | Boundary, not a longer prompt: **job-level context is how you know the COMPANY.** Person-level relationship stays `warm` + `noticed`. |
| **`job_ask` and `sched_block` both set a CTA.** | Replace, never append (§5, §Lessons 40). Asserted by mutation. |
| **The boxes stay empty.** A field nobody fills is a schema change that bought nothing. | Two fields, not four. Placed where the operator already reads the posting. Measure fill rate after 10 jobs and say so if it is zero. |
| **A textarea is destroyed by the 2.5s refresh.** | Verify on the real page, not in a render test. §Lessons 41 and 43 are both about controls that were correct and unusable. |
| **Context leaks across Spaces.** | `job_context` is on the row, `offer` on the manifest — both already scoped by `space_id`. But §Lessons 70: a Space is only as separate as its WRITE paths, and the read filter hides the gap. Whatever writes these fields carries the Space. |
| **Golden file churn hides a real change.** | The jobs prompt is pinned byte-for-byte. CTX-1 changes it once, deliberately, in its own commit, with the diff read rather than regenerated. |
| **Scope creep into the résumé.** | §3. "Why I'm a good fit" reaching the cover letter is a separate ticket behind the validators. |

## 11. Decisions to take, rather than discover

Answer these before CTX-2, not during it.

1. **Does editing context auto-redraft, or offer a button?** Auto is fewer clicks and spends
   LLM credits on every keystroke-save. A button is one more click and is honest about cost.
   *Leaning: button, with the `✓ used in N drafts` indicator going stale-amber when the context
   is newer than the drafts.*
2. **Does `job_context` reach the apply agent?** It is exactly the sort of thing a cover-letter
   field on an application form wants. It also rides into a `bypassPermissions` browser on
   attacker-controlled pages. *Leaning: no, not in CTX-2.*
3. **Does a targets row get `job_context` too?** It already has `full_description` doing that
   job. Two fields meaning the same thing on one shape is how `_BOARD_SITES` and `_BOARD_NAMES`
   drifted (§Lessons 49). *Leaning: targets keeps `full_description`; the new columns are read
   on the jobs path only, and the UI does not render them on a targets row.*
4. **Is `offer` the right name once it means two things?** It reads as "what I'm selling" and
   will now also mean "why I want this". Renaming costs a manifest field and a config-blob
   migration path. *Leaning: keep the field, label it per shape (§6.2), and write down here
   that the name is worse than the thing.*

---

## Appendix — what this replaces in the operator's head

Today, making a Gauntlet email sound different from a Job Search email means typing a one-off
`style` directive on every regeneration and remembering what was typed last time. That is the
per-run tier doing a per-campaign job, and it is unrepeatable by construction — nothing records
it, so nothing can compare it. CTX-1 moves that sentence into the Space, where it is written
once and applies to every draft in it. CTX-2 gives the row its own version. `draft_variant`
makes both falsifiable.

# CTX-3 — Two of the six draft entry points cannot see a Space

**Size:** S/M · **Depends on:** nothing (independent of CTX-1/2) · **Status:** DONE 2026-08-08
**PRD:** `docs/outreach-context-prd.md` §4
**Predates this PRD.** Found while measuring for it, not introduced by it.

## Diagnosis

Measured against `networking/outreach.py` on 2026-08-07:

| Function | line | takes `space` |
|---|---|---|
| `draft_email` | 441 | ✅ |
| `draft_followup` | 664 | ✅ |
| `draft_for_channel` | 784 | ✅ |
| `draft_linkedin_followup` | 998 | ✅ |
| **`draft_reply`** | **907** | **❌** |
| **`draft_sms`** | **1193** | **❌** |

```python
def draft_reply(profile: dict, job: dict, contact: dict, thread: list | None = None,
                subject: str = "", style: str = "", their_reply: str = "",
                touches: list | None = None) -> dict:

def draft_sms(profile: dict, job: dict, contact: dict, touch: int = 0,
              style: str = "", thread: list | None = None) -> dict:
```

A parameter that does not exist cannot be read, so this is settled without tracing further:
**`space.tone` has never reached a text message or a reply.**

### Correction, found while building: it was worse than that

Two of six could not accept a manifest. But of the four that COULD, three never read `tone` —
`draft_followup` and `draft_linkedin_followup` take `space` and use it for `shape` alone, to
pick between the pitch and job-seeker system prompts. Measured:

```
draft_email              tone ✓   premise ✓   context ✓
draft_followup           tone ✗   premise ✗   context ✗   (takes space, reads only shape)
draft_for_channel        tone ✗   premise ✗   context ✗
draft_linkedin_followup  tone ✗   premise ✗   context ✗   (takes space, reads only shape)
draft_reply              — cannot accept a manifest at all —
draft_sms                — cannot accept a manifest at all —
```

**The campaign's voice reached ONE of six entry points**, not four. Accepting a manifest and
reading it are different things, and the ticket's own table measured the first while claiming
the second.

`draft_for_channel` (`:784`) does take `space` and dispatches to `draft_sms`, so the value is
in the caller's hand and is dropped at the call.

### Why nobody noticed

Same shape as SPACE-4's own bug, one layer over. `followup_panel` spelled out
`buckets[EMAIL.name], buckets[LINKEDIN.name]` by hand, so a third channel passed correctly
through the entire engine and vanished at the return statement. Here a manifest passes
correctly through four entry points and vanishes at two signatures.

It is also invisible from the output: a text with no campaign voice is a *perfectly good text*.
Nothing errors, nothing renders wrong, and the only way to see it is to ask which parameters
exist. §Lessons 15's cousin — a silent omission that looks exactly like the intended result.

### One thing that is NOT a bug here

`offer_deck` not reaching `draft_sms` is correct and must stay correct. **Texts never carry a
link** — a URL from an unrecognised number is the strongest spam signal there is, and
`_intro_deck_url` is deliberately never consulted on that path. Threading `space` into
`draft_sms` must not quietly hand the deck gate a way in.

## Scope / tasks

- [x] `space=None` on `draft_reply` and `draft_sms`, matching the four that had it.
- [x] Passed from every call site: `draft_for_channel`, and `web_dashboard._draft_reply` through
      `service.space_for(job, conn)`.
- [x] **Three shared builders rather than five inline constructions** — `_voice_block`,
      `_premise_block`, `_known_block`. Repeating the assembly per channel is precisely how this
      drifted in the first place (§Lessons 49), and `followup.Channel` already showed what
      turning per-channel branching into data buys.
- [x] `draft_email` refactored onto the same builders **first**, with the golden file as the
      proof: `tests/golden/jobs_outreach_prompt.txt` did not move, so the extraction is
      byte-identical rather than merely believed to be.
- [x] `brief=True` for the short channels (text, LinkedIn, reply). It shortens the GUIDANCE and
      **never drops a field** — a channel silently missing a layer is the bug this closes.
- [x] The voice goes LAST on every path, immediately before the instruction to write.
- [x] `offer_deck` still unconsulted on the SMS path, asserted rather than merely avoided.
- [x] Premise and row context carried through the same threading, so all three layers arrive
      together instead of one shipping now and two later.

### Deliberately not done

**`job_ask` stays email-only.** The follow-up ladder already sets a per-touch intent (touch 2
offers a redirect, touch 3 says plainly it is the last), and overriding that from a row-level
field is a second contradiction of exactly the §Lessons 40 kind — not a threading problem. If it
is wanted, it is its own ticket with its own decision about which wins.

## Not in scope

- No change to what SMS or replies *say* beyond honouring the manifest. The five-tier standing
  grading in `_sms_permission()` and the reply's continuation intent are correct and tested.
- No new channel.

## Tests

`tests/test_every_channel_sees_the_space.py`, 45 tests — five properties parametrised across all
seven entry points (six functions plus `draft_for_channel` routing to both a leaf and itself).

- [x] `test_the_campaign_voice_reaches_every_channel` — **passed for one of seven before this
      ticket.**
- [x] `test_the_premise_reaches_every_channel`, `test_the_row_context_reaches_every_channel`.
- [x] `test_no_space_adds_nothing_anywhere` — additive on every path, not only the one with a
      golden file.
- [x] `test_the_voice_is_the_last_thing_before_the_instruction` — asserts nothing the operator
      supplied comes after it.
- [x] `test_every_draft_entry_point_accepts_a_space` — introspects the signatures. Mechanical,
      cheap, and what would have caught this weeks ago; a new entry point fails it by default.
- [x] `test_the_test_voice_is_genuinely_unknown_to_the_codebase` — scans `src/` for the fixture
      string. The channel version of this test named SMS and silently broke the day SMS shipped.
- [x] `test_a_text_never_consults_the_deck` — guards what this ticket could break while fixing
      what it is for: threading `space` into `draft_sms` hands it `offer_deck`, and a URL from an
      unrecognised number is the strongest spam signal there is.
- [x] `test_the_short_channels_get_the_short_guidance` — brief is shorter AND still contains
      every field.
- [x] **Mutation-verified, 11 of 11 killed**, `__pycache__` cleared between each: the voice
      dropped from each of the four prompts individually, the router not passing `space`, either
      signature losing the parameter, an unconditional voice block, brief falling back to long,
      and the deck reaching a text.

Suite **1639 passed, 1 skipped**. ruff and eslint clean. Verified live against the real Gauntlet
premise: it now reaches the cold email, the email follow-up, the LinkedIn follow-up and the text,
where before it reached the first only.

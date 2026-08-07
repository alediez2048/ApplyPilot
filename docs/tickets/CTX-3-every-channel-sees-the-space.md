# CTX-3 — Two of the six draft entry points cannot see a Space

**Size:** S/M · **Depends on:** nothing (independent of CTX-1/2) · **Status:** TODO
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
**`space.tone` has never reached a text message or a reply.** The campaign's standing voice
applies to email and LinkedIn and silently stops at the two channels where the operator is
closest to the person.

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

- [ ] Add `space=None` to `draft_reply` and `draft_sms`, matching the four that have it.
- [ ] Pass it from every call site: `draft_for_channel` (`:784`), `web_dashboard.py:2967`,
      `tick.py:172`, and the reply path.
- [ ] `tone_block` in both, in the same position the other four use — last, immediately before
      the instruction to write (`outreach.py` already carries the comment explaining that a
      constraint placed above the scheduling and deck blocks competes with them and loses).
- [ ] **The SMS tone block must not be a stock sentence.** §Lessons 42 fired on this exact
      prompt: a phrasing quoted as a model came back in 5 of 5 drafts. Tone is a direction to
      follow, never a sentence to reuse.
- [ ] `offer_deck` stays unconsulted on the SMS path. Assert it, do not just avoid it.
- [ ] Carry CTX-1's premise and CTX-2's context through the same threading if those have
      shipped — but this ticket stands alone and must be mergeable without them.

## Not in scope

- No change to what SMS or replies *say* beyond honouring the manifest. The five-tier standing
  grading in `_sms_permission()` and the reply's continuation intent are correct and tested.
- No new channel.

## Tests

- [ ] `test_the_campaign_voice_reaches_every_channel` — parametrised over all six entry points,
      one Space with a distinctive tone, assert the tone reaches each prompt. **Name a tone
      string that appears nowhere in the codebase**, for the reason
      `test_adding_a_channel_needs_no_schema_change` had to stop naming SMS: a fixture that
      collides with something real starts passing for the wrong reason the moment that thing
      ships.
- [ ] `test_a_text_never_consults_the_deck` — with `offer_deck=True` and a deck URL set,
      assert no URL appears in an SMS prompt or draft. This guards the thing this ticket could
      break while fixing the thing it is for.
- [ ] `test_every_draft_entry_point_accepts_a_space` — introspect the signatures. Mechanical,
      cheap, and it is what would have caught this two weeks ago. A new entry point added
      later fails it by default, which is the point.
- [ ] Mutation-verified: dropping `space` at any one call site kills the parametrised test for
      that channel and no other.

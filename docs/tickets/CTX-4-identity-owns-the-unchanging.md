# CTX-4 — The identity owns the unchanging things

**Size:** — · **Depends on:** ID-1 · **Status:** DEFERRED — do not build under this number
**PRD:** `docs/outreach-context-prd.md` §6.3

This is **ID-1's slice**, not a CTX ticket. It exists because the CTX work established the
boundary and turned up one finding that nothing else in the repo records, and a boundary
written down in a PRD section is easier to lose than one with a number on it.

## The boundary

Asked during CTX design: *should the unchanging things — the intro deck, the scheduling URL —
be set when a Space is created?*

**No. They belong to the identity, and Space creation inherits them.**

The deck and the calendar link identify the **sender**, not the campaign. `job-search` and
`gauntlet` both send from one mailbox with one deck and one calendar. Putting those on the
Space stores them twice and lets them drift.

`domain/space.py` already makes this argument for `company_cap`, and it transfers verbatim:

> *the recipient does not know what a Space is.*

Two Spaces on one mailbox each capped at 8 send 16 emails to one employer — the arithmetic
`spaces-prd.md` §Headline 4 already rejected for the daily send limit.

The second half is UX: **four config fields on the moment someone wants a new tab is friction
paid every time, for values that are identical every time.** The create dialog picks an
identity. Today there is one, `personal`, and it already has the answers.

## The finding

`identities` (migration 003, read by nothing) already holds almost the entire never-changes
list:

```
id · name · token_path · from_name · from_address · signature_html
deck_base_url · deck_collector_url · deck_collector_token · daily_limit · created_at
```

**One field is missing: `scheduling_link`.** It resolves today at `outreach.py:83`:

```python
def _scheduling_link(profile: dict) -> str:
    """Priority: SCHEDULING_LINK env → profile['personal']['scheduling_link'] → ''."""
```

A global, exactly as `INTRO_DECK_URL` was before `deck_base_url` was declared for it. When ID-1
wires identities it needs that column too, or the deck moves to the identity and the calendar
link stays a global — half the sender on one object and half on another, which is the split
this ticket exists to prevent.

## Why it is not urgent

The deck URL and the calendar link are global and identical across all three Spaces today,
which is **correct behaviour for one sender**. Making them per-Space buys nothing until a
second sender exists. Nothing in CTX-1..3 depends on this.

## Two warnings for whoever picks up ID-1

- **`identity_id` freezes on first send** (`spaces-prd.md` §13.2). Anything that writes an
  identity before ID-1 ships is a decision that cannot be taken back from the UI. This is why
  the `business` template is not in `OFFERED_TEMPLATES` and why a business Space must not be
  created yet.
- **Scope the panel, never the registry.** The lesson from the accounts banner (§Lessons 70,
  second half): `ats_accounts` has no `space_id` on purpose, because an account covers an ATS
  tenant on every tab. An identity is the same kind of object — shared, referenced by Spaces,
  never partitioned by them. What gets scoped is the sentence a surface makes about it.

## Scope

None under this number. Fold into ID-1:

- [ ] `scheduling_link` column on `identities`.
- [ ] Resolve deck base, collector, from-name, signature and scheduling link **through the
      Space's identity**, with the current globals as the fallback for `personal`.
- [ ] Space creation picks an identity; it does not ask for any of the above.

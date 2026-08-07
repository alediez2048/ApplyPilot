# CTX-* — outreach context: four tiers, one cascade

**PRD:** `docs/outreach-context-prd.md` · **Branch:** `context`, off `spaces` @ `1e923f8`

The objective in the operator's words: *"the same message for job applications won't be the
same as gauntlet."* Both Spaces are `pipeline/jobs`, both hold job postings, and today both
produce the same email — because the only Space-level lever that reaches a jobs draft is
`tone`, and tone is **voice, not substance**.

| # | Ticket | Size | Kind | Status |
|---|---|---|---|---|
| 1 | `CTX-1` the campaign premise reaches a jobs draft | S/M | **bug** | TODO |
| 2 | `CTX-2` context and ask on the row | M | feature | TODO |
| 3 | `CTX-3` every channel sees the Space | S/M | gap | TODO |
| 4 | `CTX-4` the identity owns the unchanging things | — | **deferred** → ID-1 | NOT NOW |

## Order, and the one real constraint

**CTX-1 first, and it answers the objective on its own.** It is filed as a bug rather than a
feature because the field already exists: `Space.offer` is declared at `domain/space.py:103`,
documented for exactly this case, and `draft_email` hands it only to `_pitch_user_prompt`
(`outreach.py:531`). The jobs branch at `:538` never receives it. Nothing needs a schema
change to make Gauntlet sound different from Job Search.

It is S/**M** rather than S because of a second defect found while sizing it: `#offerInput`
lives inside `#targetControls`, which is `hidden` on a jobs Space — so there is no way to TYPE
a premise there either. The backend already accepts one. Both halves are in that ticket.

**CTX-2 depends on CTX-1** only for the block ordering it establishes (PRD §5). Building the
row tier first would mean deciding precedence twice.

**CTX-3 is independent** and could ship at any point. It closes a hole that predates this PRD:
`draft_reply` and `draft_sms` do not take `space`, so `tone` has never reached a text message
or a reply.

**CTX-4 is not on this critical path.** It is ID-1's slice, filed here only because it carries
one finding nothing else records — see the ticket.

## What makes this different from `noticed`

`contacts.noticed` is the same idea one tier down, already built, and it is the model to copy:
a text box, a prompt block that bans the SHAPE rather than a verb list, a `draft_variant` bit,
and a `✓ in the draft` indicator that makes an invisible input visible.

One thing does not carry over. `noticed` is **per person**, so its worst case is one repeated
sentence to one reader. `job_context` is **per row, shared across every contact at that
company by construction** — which makes it structurally the likeliest block in the whole prompt
to send five colleagues the same sentence. That is §Lessons 42 (the SMS concession line, 5 of 5
drafts) with better odds of firing, and it is the risk CTX-2 is mostly built around.

## The finding that generalises

**A field can be declared, documented, tested for purity, and still reach nothing.**
`UNAPPLIED` at `domain/space.py:75` is empty and `test_unapplied_fields_are_really_unapplied`
holds it honest — both true, and `offer` is still dead on the jobs path, because that guard
asks *"is this field read anywhere?"* and the answer is yes: on one of two shapes.

§Lessons 49 in a new place. A rule implemented at one of its two call sites is not implemented;
a field wired into one of two shapes is not wired.

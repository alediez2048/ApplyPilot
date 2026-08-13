# CO-2 — Move contacts from a dead role to a live one

**Size:** M · **Depends on:** nothing · **Status:** SCOPED, not built

Asked for as: *"The startup lead role was canceled… I already have a full list of contacts that
I can continue reaching out to and would like to migrate over to a brand-new job application…
a button that says Migrate Contacts."*

This is the other half of CO-1 — the keying problem, from the direction that actually hurts.

---

## The Google case, measured

| | job | status | contacts | emailed |
|---|---|---|---|---|
| from | Startups Performance Lead, Google Cloud | **cancelled** | **16** | 7 |
| to | AI Sales Specialist, Startups, Google Cloud | applied | 1 | 0 |

A move would carry **25 messages, 13 touches, 5 sequences** (0 interactions, 0 transcripts).
`touches` and `sequences` have no `job_url` at all — they follow `contact_id` blindly, which is
what makes this the only operation in the app that can silently destroy a ladder.

**The 16 are three different problems, not one:**

| group | n | what they are |
|---|---|---|
| replied | **1** | Patrick — 9 messages, a live conversation |
| emailed, no reply | **6** | 1–3 messages, 0–3 touches, mid-ladder |
| never contacted | **9** | `drafted`, no messages — and **5 have no email address at all** |

**One collision:** `omalleyp@google.com` is on BOTH jobs. The destination row is `drafted` with
zero messages; the source row has the whole conversation.

### And the measurement that decides the design

**16 of 16** stored messages and drafts name *"Startups Performance Lead"* by name. A real one:

> **subject:** quick question about the Startups Performance Lead role
> Hey Carol, I just applied for the Startups Performance Lead role at Google Cloud…

So a naive move — rewrite the foreign keys and stop — leaves **9 unsent drafts pitching a
cancelled role, sitting on a live card ready to send**, and 6 ladders whose next touch follows
up on a job that no longer exists. The rows would all be in the right place and the feature
would be worse than not having it (§Lessons 15's shape: the failure renders exactly like
success).

---

## The shape

**A button on the CLOSED job's card**, because that is where the operator is standing when they
find out the role died: `→ Move contacts to another application`. It offers other jobs **at the
same resolved employer** and nothing else — the whole premise is that these people work there,
and offering a different company would move real humans onto a card where they do not belong
(§Lessons 68's cost, arrived at deliberately).

The dialog shows the three groups above, pre-ticked, with the collision called out by name:

    Move 11 people from "Startups Performance Lead" to "AI Sales Specialist"

    ✓ 1 in conversation        Patrick — 9 messages, he replied
    ✓ 6 emailed, no reply      their ladders will RESET (see below)
    ✓ 4 never contacted        no outreach yet

    — 5 not moving             no email address, so there is nothing to continue

    ⚠ Patrick is on both cards. The one with the conversation is kept.
    ⚠ 4 unsent drafts name the old role and will be cleared, not moved.

---

## Decisions, and why

| Decision | Why |
|---|---|
| **Re-key, do not copy** | `contact_id` hashes `(job_url, linkedin_url, name)`, so a copy creates a second row for the same human — which is the exact confusion that had Patrick's conversation on one card and a compose box on the other. Copying to solve a duplication problem makes it worse. One `UPDATE` per table, one transaction. |
| **Unsent drafts are CLEARED, not moved** | All 16 name the dead role. A draft that pitches a cancelled job is worse than no draft, because it is one click from being sent and looks ready. `outreach_subject` / `outreach_message` are blanked and `outreach_status` returns to `found`; the operator regenerates against the new posting. |
| **A SENT message is never touched** | It is the only record of what actually went out (the same rule `deck-relink` follows). It keeps its text, its subject naming the old role, and its place in the thread. |
| **Ladders RESET; the history stays** | Carrying `touches` means touch 3 chases a cancelled role. Dropping them means cold-emailing somebody already written to three times. So the touches move as HISTORY and the sequence is closed on arrival — the new role is a genuine first contact, and `burned_block` finally has something to read (CLAUDE.md notes it is "empty in exactly the case it exists for"; this is that case). |
| **`submitted_at` clears, `replied_at` does not** | The first is about an outreach that is over. The second is a fact about the person, and it is what keeps Patrick out of a cold-email queue. |
| **Collision: the richer row wins** | Measured rule, not a guess — keep the row with messages, delete the empty one. If BOTH have messages, refuse the pair and say so rather than merging two conversations (live: zero such pairs exist, so refusing costs nothing today). |
| **`messages.job_url` moves with the person** | `threads_for_job` keys the per-job payload on it, so leaving it behind hides the history from the card the person is now on. Nothing is lost: every message's subject already says which role it was about. |
| **Same employer only** | `derive.resolve_employer` on both, compared with `companies_match`. Not a nicety: these are people who work at that company, and the button must not be able to move them somewhere they do not. |
| **Back up first, and say so** | This is the one operation that can destroy a ladder, and `touches`/`sequences` have no `job_url` to reconstruct from. The dialog names the backup file it wrote before it runs. |

---

## Phases

**Phase 1 — the move.** `repo/contacts.migrate(src_url, dst_url, ids)`: one transaction, five
tables, the collision rule, drafts cleared, sequences closed. Pure repo, fully testable offline.
~1 day.

**Phase 2 — the button and the dialog.** On the closed card, employer-matched targets, the three
groups, the two warnings. ~half a day.

**Phase 3 — undo.** Same transaction in reverse, available until the page is reloaded. Cheap
because phase 1 is one function, and it is what makes the button safe to press. ~half a day.

---

## Not doing, and why

| | |
|---|---|
| **Merging two people who BOTH have conversations** | Interleaving two threads is unrecoverable and there are zero such pairs live. Refuse, name them, let the operator decide. |
| **Moving to a different employer** | They do not work there. |
| **Auto-migrating when a role is cancelled** | The operator may want to let those contacts go. Cancelling a role is not consent to re-pitch fifteen people. |
| **Moving `interactions` selectively** | 0 rows here, and they carry `job_url`; move them with everything else. |
| **Fixing `contact_id` to stop hashing `job_url`** | That re-keys all 244 stored contacts and is CO-1's deferred half. This ticket works with the hash as it is. |

---

## Answered by the operator, 2026-08-13

**1. A contact with NO email address does not move.** *"The idea of migrating an existing
contact to a new job is that we already began outreach, so we can continue the conversations."*
That premise is the whole feature, and somebody with no address was never part of it and cannot
be. Live, that excludes **5 of 16** — so the Google move is **11 people**, not 16. They are shown
in the dialog as excluded with the reason, not hidden: they are still real people on the old
card, and silently dropping five of sixteen would read as a bug.

**2. Messages move with the person.** Continuity is the point. The old card keeps its
`job_events` and its own record of having been worked; the correspondence follows the human.

**3. Undo ships in the first cut.** It is what makes the button safe to press, and it is cheap
because the move is one transaction in one function.

## Still open

**Is one target enough?** The Google case has exactly one live role, so the first cut may pick
it automatically and say so. Two would need the dialog to choose.

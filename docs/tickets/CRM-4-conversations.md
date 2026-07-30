# CRM-4 — Conversations (memory, context, and replying from the dashboard)

**Phase:** 2 · **Size:** L (~2d, split 4a / 4b) · **Depends on:** CRM-1 · **Status:** Todo
**PRD:** `crm-prd.md` §6.1
**Why:** CRM-1 made the system *notice* a reply. It still has no idea what the conversation is.

## Problem

The very first reply CRM-1 detected proves the gap. Real thread, 2026-07-29:

```
1.  me        →  victoria.shearer@writer.com
2.  Victoria  →  me        CC: David Loveless <david@writer.com>     ← a handoff
3.  me        →  Victoria  CC: David
```

Victoria replied by **introducing someone else on the same thread**. What ApplyPilot did with
that: recorded `replied_at`, stopped her email ladder, and moved on. Which means

- **David does not exist** anywhere in the system. The person now actually handling this
  application is invisible, has no ladder, and will never be followed up.
- The Writer job reads as *finished* — one reply, ladder stopped, checklist satisfied — when it
  is in fact the only live conversation in the whole database.
- Replying means leaving the dashboard, finding the thread in Gmail, and remembering the
  context yourself. The CRM knows a reply happened and nothing about what it was.

**A CRM whose memory of a conversation is a boolean is not tracking the conversation.**

## The scope decision (read this before scoping the work)

The token holds `gmail.metadata`, deliberately **not** `gmail.readonly`. That is not an
oversight — metadata can read headers, threads and participants and **cannot read a single
message body**, so a leaked token exposes who you talked to, never what was said.

Verified against the live thread above:

| Signal | metadata (today) | needs `readonly` |
|---|---|---|
| Who is on the thread (From / **To** / **Cc**) | ✅ | |
| A new participant appearing = a handoff | ✅ | |
| Message order, dates, subject | ✅ | |
| **What the reply said** | ❌ (`snippet` returns empty) | ✅ |
| A drafted reply that answers it | ❌ | ✅ |

So the ticket splits along that line, and **4a is not a stepping stone — it is most of the
value**. The Victoria case is entirely solvable without new permissions.

---

## 4a — Conversation memory (NO scope change)

- [ ] **`messages` table** (new, own repository like `touches`): `thread_id`, `message_id`,
      `contact_id`, `job_url`, `direction` (in/out), `from_addr`, `to_addrs`, `cc_addrs`,
      `subject`, `sent_at`. Headers only. **No bodies, no snippets** — the schema itself is the
      guarantee, not a policy note.
- [ ] **`gmail_read.thread_messages()`** already returns most of this; add `To` / `Cc` to the
      requested `metadataHeaders`.
- [ ] **Handoff detection** — an address on the thread that is neither us nor the contact.
      Surface as: *"Victoria introduced David Loveless (david@writer.com)"*.
- [ ] **Introduced contacts** — offer to add them, with `source='introduction'` (distinct from
      `apollo` / `connection`, and a far warmer lead than either). **Offer, do not auto-create**
      — see Risks.
- [ ] **Thread view in the dashboard**: a per-contact conversation timeline — who wrote, when,
      who was added. This is the "memory".
- [ ] **Reply from the dashboard**, threaded correctly: reuse `thread_id` + `rfc_message_id`
      (already captured at send) and preserve the Cc list, so a reply reaches David too.
- [ ] **Ladder correctness after a handoff** — the original contact stays `replied`; the
      introduced contact starts a fresh ladder. Today the job would read as finished.
- [ ] **Structural suggestions only** (all that is honest without bodies):
      *"They introduced someone — email David"* · *"You replied 3 days ago, no answer — nudge?"*
      · *"They replied and you never answered"* ← that last one is a real failure mode.

## 4b — Reply context (REQUIRES `gmail.readonly`, opt-in)

- [ ] `doctor` explains the trade in one line, and the feature is **off** unless the scope is
      granted. Never request it silently.
- [ ] **Store the snippet only (~200 chars), never the full body.** Enough to draft against,
      an order of magnitude less sitting in a plaintext SQLite file that is not encrypted at
      rest beyond FileVault.
- [ ] **Contextual reply drafting** — a response that answers what they said, not a generic
      follow-up.
- [ ] **Intent classification** for better suggestions: introduction · interested · not now ·
      rejection · asked a question. A rejection should offer *Mark rejected*, not *draft a
      follow-up*.
- [ ] Revocation must degrade cleanly: drop to 4a behaviour, keep everything already stored.

---

## Acceptance criteria

- [ ] The Writer thread renders in the dashboard with all three messages and both participants
- [ ] David is surfaced as an introduced contact, on the Writer job, marked as an introduction
- [ ] The Writer job stops reading as "finished" while a live conversation is open
- [ ] A reply sent from the dashboard lands **in the same thread** and keeps the Cc
- [ ] 4a works on `gmail.metadata` alone; **nothing** requests `readonly` without an explicit act
- [ ] No message body is ever written to the database in 4a; no full body in 4b either
- [ ] `/api/status` stays inside its query budget — thread data batched, never per contact
- [ ] Tests: handoff detection, us-vs-them address matching, a Cc that is a mailing list or an
      assistant, threading a reply, and a thread whose contact has since been deleted

## Risks / notes

- **The apply agent stays locked out of the inbox, permanently.** It browses attacker-controlled
  careers pages with `bypassPermissions`; inbox read is denied twice and pinned by a test. This
  feature lives in the dashboard's outreach path, which never touches untrusted content. Do not
  "simplify" by giving the agent one shared Gmail client.
- **Do not auto-create contacts from Cc.** Threads collect schedulers, assistants,
  `noreply@`, ATS notification addresses and mailing lists. An auto-added contact is one an
  automated ladder will then email. Detect, surface, let the operator confirm.
- **Storing conversations changes what a DB leak costs.** Today `~/.applypilot/applypilot.db`
  holds names, addresses and drafts. Add threads and it holds correspondence. That is a real
  step up in sensitivity and the reason 4b stores snippets rather than bodies.
- **Attribution gets harder, not easier** (CRM-2). Once a thread has three participants, "who
  replied" and "what worked" stop being the same question. Say what is being counted.
- **Metadata cannot use `q=` search.** Threads are listed by id, never queried — the same
  constraint that shaped CRM-1.
- **The introduced contact has no `submitted_at`**, so the existing ladder anchor does not
  apply. Decide the anchor deliberately (probably the introduction date) rather than letting
  `touch_state` fall through to "not ready" and silently never follow up — the same class of
  silent-zero bug that made `tick` report 0 due while the dashboard showed 3.

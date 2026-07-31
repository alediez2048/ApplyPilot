# CRM-4 — Conversations (memory, context, and replying from the dashboard)

**Phase:** 2 · **Size:** L (~2d, split 4a / 4b) · **Depends on:** CRM-1
**Status:** ✅ **DONE 2026-07-31.** 4a shipped and live · 4b built and **switched off**
(needs `gmail.readonly`, which is the operator's call — `network --gmail-connect --with-content`)
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

## 4a — Conversation memory (NO scope change) — ✅ SHIPPED 2026-07-31

- [x] **`messages` table** (new, own repository like `touches`): `thread_id`, `message_id`,
      `contact_id`, `job_url`, `direction` (in/out), `from_addr`, `to_addrs`, `cc_addrs`,
      `subject`, `sent_at`, `rfc_message_id`. Headers only. **No bodies, no snippets** — the
      schema itself is the guarantee, not a policy note, and a test asserts it.
- [x] **`gmail_read.thread_messages()`** already returned most of this; `To` / `Cc` added to the
      requested `metadataHeaders`.
- [x] **Handoff detection** — an address on the thread that is neither us nor the contact.
- [x] **Introduced contacts** — offered with `source='introduction'`. **Offer, never auto-create.**
- [x] **Thread view in the dashboard**: a per-contact conversation timeline.
- [x] **Reply from the dashboard**, threaded, **Cc preserved** — `domain.conversations.reply_target()`
      + `gmail_send.send_reply()` + `/api/contact/reply`. Answers the last **inbound** message,
      chains `References` across the whole thread, and shows the Cc as removable chips so the
      operator can *see* who it reaches before clicking Send.
- [x] **Ladder correctness after a handoff** — **the feared bug does not exist, and inventing an
      anchor would have created one.** Measured on the live Writer job rather than assumed: the
      introduced contact has no `sent_message_id`, so `_is_ready` returns False and the email
      ladder correctly does not apply — you cannot follow up on an email you never sent. He is
      not invisible either: the checklist counts him under `emailed 2/3`, so the job reads
      *partial*, not finished. Once he IS emailed, `mark_sent` stamps `submitted_at` and the
      ladder anchors normally. Back-dating an anchor to the introduction date, as this ticket
      originally proposed, would have told the ladder we had emailed somebody we had not.
- [x] **Structural suggestions** — `conversation_state()` + `awaiting_reply`. *"They replied and
      you never answered"* is the one that mattered and it was **live in the database**: Gina
      Johnson at Salesforce replied 2026-07-31 and the row still said "1 follow-up due". An
      unanswered reply now outranks every ladder in `nextAction()`, shows on the collapsed
      contact row, and `tick` reports it.

### What the reply work actually cost, and what it found

The Cc is the whole feature and it is **invisible when wrong**: answering only the sender drops
whoever they introduced, and the screen looks identical either way. So the recipients are
computed from the stored thread, never from the browser (`/api/contact/reply` accepts a `cc`
the operator edited, and ignores any `to` — a test posts `attacker@evil.com` and asserts it goes
to Victoria anyway), and the composer renders the Cc as visible chips rather than trusting it
silently. Twelve mutations were run against the domain function; all twelve were caught.

**It also exposed a performance bug 4a itself had shipped.** `connected_email()` is an HTTP
round-trip to Gmail, and CRM-4a called it once per job inside `/api/status` — a path that
re-renders every 2.5s. Measured at **2.4 seconds per request with 15 jobs**: the dashboard was
refreshing back-to-back and spending nearly all of it asking Gmail the same unchanging question.
The statement budget could not see it because none of it was SQL. Cached on the token file's
mtime (so reconnecting a different account still invalidates), **2.4s → 0.043s**, with a test
that counts the profile fetches.

## 4b — Reply context (REQUIRES `gmail.readonly`, opt-in) — ✅ BUILT 2026-07-31, **OFF**

Shipped inert. The code is in place and every test passes with the scope **not** granted, which
is the state this machine is in and stays in until somebody types `--with-content`.

- [x] `doctor` explains the trade in one line (`Reply content  off — headers only…`), and the
      feature is off unless the scope is granted. `CONTENT_SCOPE` is deliberately **not** in
      `SCOPES`, so no future scope addition can drag it along; a test pins that.
- [x] **Snippet only (~200 chars), never the full body** — `SNIPPET_MAX`, truncated **at the
      write** in `upsert_messages`, not at the caller. A cap a new caller can forget is not a cap.
- [x] **Contextual reply drafting** — `outreach.draft_reply()` + `/api/contact/draft-reply`.
      Refuses rather than producing something when there is no readable text: a "contextual"
      reply written without the context is a generic follow-up wearing a `Re:` subject line, and
      it would look like a working feature until somebody read it. No intro deck — that belongs
      to outreach whose job is to EARN a reply, not to an answer inside a live conversation.
- [x] **Intent classification** — `domain/intent.py`, rule-based and quick to say `unknown`.
      Auto-replies are matched FIRST: an out-of-office can contain almost any phrase and mean
      none of it, so "Unfortunately I am out of office" must not read as a rejection.
- [x] Revocation degrades cleanly — and this needed real work. `upsert_messages` is INSERT OR
      **REPLACE**, so a re-sync carrying no snippet would have blanked one already stored, and
      `tick` re-syncs every open thread hourly. Existing snippets are now carried forward.

**Why rules and not an LLM for intent:** the input is a ~200-char snippet, the output picks a
button, and it runs in a loop that may execute hourly forever. A confident wrong label is worse
than none — "interested" on a rejection would have the operator write an eager reply to somebody
who already said no.

---

## Acceptance criteria

- [x] The Writer thread renders in the dashboard with all three messages and both participants
- [x] David is surfaced as an introduced contact, on the Writer job, marked as an introduction
- [x] The Writer job stops reading as "finished" while a live conversation is open
- [x] A reply sent from the dashboard lands **in the same thread** and keeps the Cc —
      verified live: `to=victoria.shearer@writer.com`, `cc=[David Loveless <david@writer.com>]`
- [x] 4a works on `gmail.metadata` alone; **nothing** requests `readonly` without an explicit act
- [x] No message body is ever written to the database in 4a; no full body in 4b either
- [x] `/api/status` stays inside its query budget — thread data batched, never per contact
      (and now inside a *network* budget too, which nothing was measuring)
- [x] Tests: handoff detection, us-vs-them address matching, a robot Cc, threading a reply, a
      reply target on a thread nobody answered, and a deleted contact's thread

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

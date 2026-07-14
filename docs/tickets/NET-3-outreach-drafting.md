# NET-3 — AI outreach drafting

**Phase:** 3 · **Size:** M · **Depends on:** NET-1, NET-2 · **Status:** Todo
**PRD:** §6, §7

## Summary
For each contact, the LLM drafts a short, tailored outreach **email** (subject + body)
referencing the specific role, one relevant proof point, and a soft ask. Editable in the
dashboard before any send. Reuses `llm.py` (multi-provider failover) and the tailoring
guardrails (no fabrication, plain voice).

## Scope / tasks
- [ ] **`networking/outreach.py`** — `draft_email(profile, job, contact) -> {subject, body}`:
  - [ ] inputs: `profile.json`, job (`title`, `full_description` snippet, company),
        contact (`full_name`, `title`, `match_reason`)
  - [ ] body 3–4 sentences; mention the exact role applied to; soft ask (15-min chat)
  - [ ] optional `linkedin_note` variant (≤ 300 chars)
  - [ ] guardrails: reuse validator patterns (no fabricated facts, banned words)
- [ ] **Persist** — store `outreach_subject`/`outreach_message`, set `outreach_status='drafted'`.
- [ ] **service hook** — `find_contacts_for_job(..., draft=True)` drafts after enrich;
      also a standalone `draft_for_contact(contact_id)` for regenerate.
- [ ] **CLI** — `applypilot network --draft/--no-draft` (default draft on).
- [ ] **Dashboard** — show editable **subject** + **body** per contact; `[edit]` (inline
      textarea, save → `POST /api/outreach`), `[regenerate]`, `[copy]` (copies subject+body).
- [ ] **Endpoint** `POST /api/outreach` — `{contact_id, subject?, body?, regenerate?}` →
      save edits or regenerate; returns updated draft.

## Acceptance criteria
- Each found contact has a drafted subject + body that names the specific role and reads
  like a human wrote it (spot-check on Affirm contacts).
- Editing a draft in the dashboard persists (survives reload).
- Regenerate produces a fresh draft without duplicating the contact.
- Drafting failures (LLM error) leave the contact intact with `outreach_status` unchanged.

## Tests
- `outreach.draft_email` with a stubbed LLM → asserts subject+body non-empty, role present.
- Validator guardrail applied (no banned/fabricated content) — reuse existing validator.
- Endpoint: save edit → row updated; regenerate → new text.

## Out of scope
Actually sending (NET-4).

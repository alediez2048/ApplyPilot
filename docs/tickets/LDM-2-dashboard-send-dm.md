# LDM-2 — Dashboard "Send DM" button + background task

**Phase:** 2 · **Size:** M · **Depends on:** LDM-1 · **Status:** Todo
**PRD:** §5 · The UX the user asked for: "same as Send email, but for LinkedIn DMs."

## Summary
Add a **Send DM** button next to Copy note. Clicking it fires the LDM-1 sender as a
**background task** (like Find contacts) and shows live status on the contact row — the
twin of the existing Send email flow.

## Scope / tasks
- [ ] **payload** — `_contact_payload` gains `dm_status`, `dm_sent_at`, `dm_error`, and a
      `dm_available` capability flag (agent-browser present + DM enabled + logged in + note +
      linkedin_url + under cap + not sent).
- [ ] **background task** — reuse the keyed task registry (per contact_id) so a DM send runs
      without blocking prepare/apply/find; single DM concurrency (human-paced).
- [ ] **endpoint** — `POST /api/outreach/send-linkedin` `{contact_id, dry_run?}`; Origin-guarded;
      enforces `dm_available`; returns started/refused.
- [ ] **button** — `[Send DM]` in the LinkedIn-note button row; enabled only when
      `dm_available`; one confirm ("Send this LinkedIn DM to {name}? Drives your real account.").
- [ ] **row status** — sending… (live) → **sent ✓ {time}** or **failed {reason}**; button
      disabled after sent; disabled-state tooltips (connect agent-browser / log in / cap / sent).

## Acceptance criteria
- On a contact with a drafted note + linkedin_url, **Send DM** starts a background task and the
  row shows sending… then sent ✓ (dry-run confirmed first).
- Runs alongside other dashboard actions without collision (keyed registry).
- Cross-origin POST rejected (Origin guard, like the other send endpoints).
- Button correctly disabled + tooltipped when any precondition is missing.

## Tests
- payload `dm_*` + `dm_available` computation (mock caps/login).
- endpoint: happy start, refusal when `dm_available` false, Origin rejection.
- registry: concurrent DM starts on the same contact rejected; different contacts allowed.
- Manual: click-through on a real contact (dry-run, then live to a secondary account).

## Out of scope
Consent gate + doctor + dry-run toggle polish (LDM-3).

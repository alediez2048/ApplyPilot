# LDM-3 — Safeguards, consent gate, dry-run, doctor

**Phase:** 3 · **Size:** M · **Depends on:** LDM-1, LDM-2 · **Status:** Todo
**PRD:** §7, §8 · Makes the DM channel safe to actually use.

## Summary
Wire the full safeguard set around the DM sender and surface readiness/consent, so a real
LinkedIn DM only goes out under explicit, informed, capped, human-in-the-loop conditions.

## Scope / tasks
- [ ] **consent gate** — one-time `--linkedin-dm-consent` (or reuse the NET-5 consent file)
      that prints the real stake (permanent account restriction) and requires acknowledgement;
      `NETWORKING_LINKEDIN_DM=0` off by default. No send without both.
- [ ] **daily cap** — `LINKEDIN_DM_DAILY_LIMIT` (default 5) enforced atomically with the claim;
      persisted across runs; dashboard + CLI show usage (N/limit).
- [ ] **cross-contact dedupe** — `LINKEDIN_DM_COOLDOWN_DAYS` (default 30) on normalized
      linkedin_url; "already DM'd for another role" surfaced.
- [ ] **tool scoping** — the send agent gets only navigate/read/type/click within the message
      flow (allow-list); no arbitrary browsing, connect/follow/endorse, or other tools.
- [ ] **isolated profile + local-port hygiene** — DM profile separate from apply; document the
      CDP-port exposure; ensure the browser closes after the task.
- [ ] **dry-run** everywhere — CLI flag + dashboard toggle; logs the composed message instead
      of sending.
- [ ] **doctor** — lines for: agent-browser found (+version), DM profile logged in, DM enabled +
      consent, daily-cap usage. `.env.example` gains the DM vars.

## Acceptance criteria
- No DM sends unless: DM enabled **and** consent recorded **and** logged in **and** under cap
  **and** not deduped — each failure gives a clear, specific message.
- Exceeding the daily cap refuses further sends with a countdown to reset.
- `doctor` accurately reflects every precondition.
- Dry-run never sends (verified) yet exercises the full compose path.

## Tests
- gate matrix: each precondition independently blocks; all-green allows.
- daily-cap boundary + cooldown dedupe.
- tool-scope allow-list excludes connect/follow/other tools (assert on the spawned args).
- doctor readiness strings.

## Out of scope
Deterministic fast-path + retries/observability (LDM-4).

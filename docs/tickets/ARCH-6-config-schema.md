# ARCH-6 — Config schema

**Phase:** 4 · **Size:** S (~0.5d) · **Depends on:** — · **Status:** Todo
**PRD:** `architecture-prd.md` §Q5, ARCH-6

## Problem

**40 environment variables, none validated.** `FOLLOWUP_SCHEDULE`,
`LINKEDIN_FOLLOWUP_SCHEDULE` and `FOLLOWUP_AFTER_DAYS` overlap conceptually, are parsed by
three different functions, default independently, and **fail silently**: typo
`FOLLOWUP_SCHEDULE=48;96` and it quietly falls back to defaults with no warning.

## Scope / tasks

- [ ] `config/settings.py` — one frozen dataclass with types, defaults, and validators
- [ ] Parse once at startup; fail loudly and specifically on a malformed value
- [ ] Collapse `FOLLOWUP_AFTER_DAYS` into `FOLLOWUP_SCHEDULE` (one knob per concept);
      keep the old name working for one release with a deprecation warning
- [ ] `applypilot doctor --config` prints every setting, its resolved value, and its source
      (env / `.env` / default)
- [ ] Regenerate `.env.example` from the dataclass so it cannot drift
- [ ] Group by subsystem: `llm.*`, `outreach.*`, `followup.*`, `apply.*`, `networking.*`

## Acceptance criteria

- [ ] A malformed value fails at startup with the variable name and what was expected
- [ ] `doctor --config` shows all 40 with values and sources
- [ ] No two variables control the same concept
- [ ] `.env.example` is generated, not hand-maintained
- [ ] Secrets are shown as `set` / `not set`, never printed

## Risks / notes

- Low risk, high annoyance-reduction. Good ticket for a tired afternoon.
- Do not add pydantic for this — a dataclass plus a few validators keeps the runtime
  dependency count at 8, which is a §5 non-goal to protect.

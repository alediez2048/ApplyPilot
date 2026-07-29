# ARCH-6 — Config schema

**Phase:** 4 · **Size:** S (~0.5d) · **Depends on:** — · **Status:** ✅ Done (2026-07-29)
**PRD:** `architecture-prd.md` §Q5, ARCH-6

## Problem

**40 environment variables, none validated.** `FOLLOWUP_SCHEDULE`,
`LINKEDIN_FOLLOWUP_SCHEDULE` and `FOLLOWUP_AFTER_DAYS` overlap conceptually, are parsed by
three different functions, default independently, and **fail silently**: typo
`FOLLOWUP_SCHEDULE=48;96` and it quietly falls back to defaults with no warning.

## Scope / tasks

- [x] `settings.py` — a frozen `Setting` dataclass and a registry of 38, with validators.
      Top level, not `config/`: that name is already a package of YAML data files.
- [x] Parse once at startup; fail loudly and specifically. `_bootstrap()` catches
      `ConfigError` and prints the problem instead of a stack trace (exit 2).
- [x] Collapsed `FOLLOWUP_AFTER_DAYS` into `FOLLOWUP_SCHEDULE` — derived from the first
      entry (48h → 2 days). The old name still overrides, with a startup warning.
- [x] `applypilot doctor --config` — every setting, value, and source (env / .env / default)
- [x] `.env.example` generated via `applypilot doctor --write-env-example`
- [x] Grouped: llm · followup · outreach · networking · apply · paths

## Acceptance criteria

- [x] A malformed value fails at startup naming the variable and what was expected — and
      guesses the likely mistake: `FOLLOWUP_SCHEDULE='48;96' — '48;96' is not a number …
      (did you use ';' or a space instead of ','?)`
- [x] `doctor --config` shows all 38 with values and sources
- [x] No two variables control the same concept
- [x] `.env.example` is generated, and a test fails if the checked-in copy goes stale
- [x] Secrets shown as `set` / `not set`, never printed

## Risks / notes

- Low risk, high annoyance-reduction. Good ticket for a tired afternoon.
- Do not add pydantic for this — a dataclass plus a few validators keeps the runtime
  dependency count at 8, which is a §5 non-goal to protect.

## Notes from doing it

**Source detection needs a snapshot at import.** python-dotenv writes `.env` values straight
into `os.environ`, so afterwards "exported in your shell" and "written in .env" are
indistinguishable. `settings.py` captures `frozenset(os.environ)` at import — before
`load_env()` runs — which is the only point where the two are still separable.

**Empty means "use the default", not "malformed".** `FOLLOWUP_SCHEDULE=` is how a .env line
gets disabled without deleting it; failing startup on that would be worse than the bug being
fixed. Whitespace-only is treated the same way. A test pins both.

**The registry has to be kept honest by a test, not by discipline.**
`test_the_registry_covers_every_variable_the_code_reads` scans the source for
`os.environ.get("X")` and fails if it is undeclared. A config schema that drifts is worse
than none, because it looks authoritative while lying. Mutation-tested by adding an
undeclared variable to `llm.py` — it fails.

**Sharing config does not merge rules.** `FOLLOWUP_AFTER_DAYS` and `FOLLOWUP_SCHEDULE` now
come from one knob, but the checklist still measures from the FIRST email while the ladder
measures from the most recent touch. Those are deliberately different rules — see the
`followup_due` note in `domain/followup.py`, which a byte-identical /api/status check caught
during ARCH-1.

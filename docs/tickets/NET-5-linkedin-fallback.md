# NET-5 — LinkedIn browser-agent fallback

**Phase:** 5 · **Size:** M · **Depends on:** NET-1 · **Status:** Todo
**PRD:** §5 · **Gate:** Tier 3 (Claude CLI + Chrome + Node); opt-in, off by default
**Revised** after review (B7, B12, minors). **Kept for v1 but hardened.**

## Review corrections (v2 — must-fix)
- **B12a — enforce read-only.** Don't rely on prompt wording. Pass `--allowedTools`
  (read/navigate/snapshot only) + `--disallowedTools` for `browser_click`/`browser_fill_form`
  (mirror the Gmail restriction at `launcher.py:333-342`). Read-only is **tool-enforced**.
- **B12b — login is best-effort.** Nothing in the codebase logs into LinkedIn. Add a one-time
  `applypilot network --linkedin-login` (opens the worker profile to log in) + a **login-state
  precheck** that aborts cleanly before spawning. Isolate networking's Chrome user-data-dir +
  CDP port from apply's.
- **B7 — no `run_agent`.** The real fn is `run_job` (`:298`), apply-coupled. Write a **new**
  thin spawner (copy only the Popen/stream-json pattern), a **Playwright-only** MCP config
  (drop Gmail from `_make_mcp_config`), and a new JSON-array parser (not the `RESULT:` regex).
- **B12c — enrich by `linkedin_url`** (Apollo's strongest key), not name+company. Expect misses
  in low-coverage companies → surface email-less contacts with Send disabled.
- **Global cap + consent.** `NETWORKING_LINKEDIN_DAILY_LIMIT` (default 3–5 companies/day),
  persisted. One-time explicit consent naming the real stake: **possible permanent restriction
  of your primary LinkedIn account.** Recommend a secondary account. Off by default.

## Summary
When Apollo returns fewer than `--per-job` contacts, fall back to a LinkedIn browser
agent that reuses the apply stack (`claude -p` + Playwright MCP + the persistent
authenticated Chrome profile) to read the company's People results and extract 3–5
names + profile URLs. Read-only, low-volume, opt-in. Extracted people are then
Apollo-enriched (by name+company) to recover email/phone where possible.

## Scope / tasks
- [ ] **`networking/prompt.py`** — build the agent instruction: open LinkedIn, search the
      company, open **People**, filter by role keywords, read page 1, return
      `[{name, title, profile_url}]` as JSON. Explicitly **read-only** (no Connect/message).
- [ ] **`networking/linkedin_agent.py`** — spawn the agent reusing apply primitives:
  - [ ] `apply/chrome.py::launch_chrome` (persistent worker profile → LinkedIn session)
  - [ ] `claude -p --mcp-config <playwright-only> --permission-mode bypassPermissions`
        (mirror `apply/launcher.py::run_agent`), pipe prompt, parse JSON from stream
  - [ ] hard caps: 1 company, ≤5 profiles, 1 search page; per-run cooldown
- [ ] **service wiring** — in `find_contacts_for_job`, if `use_linkedin` and Apollo < N:
      run agent → for each result, Apollo-enrich by name+company → merge/dedupe by
      `linkedin_url` (or name+company); `source='linkedin'`.
- [ ] **flags** — `--no-linkedin` (default respects `NETWORKING_LINKEDIN`, off by default),
      env `NETWORKING_LINKEDIN=0/1`.
- [ ] **`doctor`** — LinkedIn-fallback readiness (Tier 3 + flag on) + a ToS caution note.

## Safeguards (must all hold)
- Opt-in only; **off by default**; disableable via flag + env.
- Read-only tool scope in the prompt — no Connect/InMail/message actions.
- ≤5 profiles/company, single page, cooldown between companies.
- Falls back gracefully (empty list) on CAPTCHA / login wall / parse failure — never crashes.

## Acceptance criteria
- With `--no-linkedin` (default), behavior is Apollo-only (NET-1 unchanged).
- With LinkedIn enabled on a company Apollo under-covers, the agent returns ≤5
  `{name, title, profile_url}` and they're merged/deduped into `contacts` as `source=linkedin`.
- Agent hitting a login wall/CAPTCHA yields an empty result + a logged reason, no exception.

## Tests
- `prompt.py` builder unit test (company/keywords present, read-only wording present).
- Merge/dedupe logic (Apollo + LinkedIn same person → one row).
- Agent run itself is manual/opt-in (not in CI).

## Out of scope
Any write actions on LinkedIn; connection/message automation (explicitly excluded).

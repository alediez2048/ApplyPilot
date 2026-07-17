# LDM-1 — agent-browser bridge + one-DM send (CLI)

**Phase:** 1 · **Size:** L · **Depends on:** NET-1..3, `agent-browser` installed · **Status:** Todo
**PRD:** §0, §3, §4, §6, §7 · **Load-bearing spike**

## Summary
Prove ApplyPilot can drive the installed `agent-browser` binary to deliver one drafted
LinkedIn note to one contact, end-to-end, with the send-state persisted and all core
safeguards. No dashboard yet — a CLI path that LDM-2 wraps in a button.

## Scope / tasks
- [ ] **binary discovery** — `linkedin_dm.agent_browser_bin()`: `AGENT_BROWSER_BIN` env →
      `shutil.which("agent-browser")` → known local build; `version()` for doctor.
- [ ] **dedicated DM profile** — a LinkedIn-only agent-browser `--profile` dir under APP_DIR,
      isolated from apply workers + everyday Chrome; `--linkedin-login` (reuse) points here.
- [ ] **`dm_prompt.py`** — build the tight instruction: open `{profile_url}`, open the message
      composer, type EXACTLY `{message}` (verbatim, no rewrite), click Send; do nothing else.
- [ ] **`linkedin_dm.send(contact)`** — spawn `claude -p --mcp-config <agent-browser mcp>` with
      a read-mostly tool scope; pipe the prompt; parse success/failure from output. Fails soft
      (returns a result dict, never raises).
- [ ] **login precheck** — abort cleanly with an actionable message if the DM profile isn't
      logged into LinkedIn.
- [ ] **`store.py`** — add `dm_status`, `dm_sent_at`, `dm_error` (auto-migrate); `claim_dm_send`
      (atomic `dm_sent_at IS NULL`), `mark_dm_sent`, `mark_dm_failed`, `dm_sent_today`,
      `already_dmed(linkedin_url)`.
- [ ] **gating in send()** — enforce: DM enabled (`NETWORKING_LINKEDIN_DM`), consent present,
      under daily cap, not deduped, agent-browser present, logged in. Return a clear refusal.
- [ ] **dry-run** — `dry_run=True` composes but does NOT click Send.
- [ ] **CLI** — a minimal entry to send/test one DM (e.g. `network --send-dm --contact <id>`
      or `--dm-test --url <profile>`), honoring `--dry-run`.

## Acceptance criteria
- With agent-browser + a logged-in DM profile, a **dry-run** on a real contact opens their
  profile and composes the exact drafted note **without sending** (verified visually).
- A real send (behind the opt-in flag, to a test/secondary account) delivers the note; the
  contact row persists `dm_status='sent'` + `dm_sent_at`.
- Re-sending the same contact is a no-op (atomic claim); the same `linkedin_url` is deduped.
- Missing binary / not-logged-in / cap-reached each return a clear, non-crashing refusal.
- No change to apply or the email-outreach flow.

## Tests (agent-browser subprocess mocked; no live LinkedIn in CI)
- binary discovery precedence (env > PATH > local).
- `dm_prompt` contains the verbatim message + "send only / do nothing else" wording.
- `claim_dm_send` single-winner under a simulated race; dedupe + daily-cap logic.
- `send()` refusal paths (no binary, not logged in, cap, deduped, dry-run).
- **Manual/opt-in:** real dry-run then a real send to a secondary account (not CI).

## Out of scope
Dashboard button (LDM-2), consent-gate UX + doctor lines (LDM-3), deterministic path (LDM-4).

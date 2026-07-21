# EXT-3 — Detect sent + auto-advance the queue

**Phase:** 3 · **Size:** M · **Depends on:** EXT-2 · **Status:** Todo
**PRD:** §2 (steps 6–8) · Turn single-contact compose into a "click Send, click Send" flow down
the whole list.

## Summary
After the human clicks Send, the content script detects the sent/Pending state, reports it to
ApplyPilot, and the background worker opens the next contact automatically — so the user flows
through the queue with one click each.

## Scope / tasks
- [ ] **Sent detection (content script):** MutationObserver on the profile action area; the
      invitation is sent when the dialog closes AND the profile shows **Pending** (or the sent
      toast appears). Debounce so a manual Cancel/close is NOT counted as sent.
- [ ] **Report status:** on confirmed send, `POST /api/ext/status {contact_id, status:"sent"}`
      (background relays it). On Cancel/close-without-send, leave status unchanged (stays ready).
- [ ] **Queue advance (background):** maintain the ordered queue + a cursor; on `sent` (or user
      "Next"/"Skip"), pick the next ready contact, set it active, navigate the tab to its
      profile, re-arm the content script.
- [ ] **Progress:** background tracks `sent / total`; popup reflects it live (per-row chips +
      overall count).
- [ ] **Pacing hook:** insert a short configurable delay between advancing (default a few
      seconds) — wired here, tuned in EXT-4.

## Acceptance criteria
- With a 3-contact queue: compose #1 → human Send → auto-advances to #2 → … → done, one human
  click each; popup shows `1/3 → 2/3 → 3/3`.
- Cancelling an invite (not sending) does NOT mark it sent and does NOT skip it.
- `dm_status` in ApplyPilot ends at `sent` for each completed contact (dashboard agrees).

## Tests
- Unit: sent-detection logic (dialog-closed + Pending) vs. cancel (dialog-closed, no Pending).
- background: queue cursor advance, skip, and completion.
- Manual: full 3-contact click-through, verify DB + dashboard reflect `sent`.

## Out of scope
Fallbacks for unusual profiles, caps, robust pacing (EXT-4). Message flow (EXT-6).

---

## Review deltas (v2 — folded from `chrome-extension-review.md`)

- **B2 — cold-wake advance, spec'd + idempotent.** Sequence: content-script send-event →
  `chrome.runtime.sendMessage({contact_id})` → worker `onMessage` rehydrates cursor from
  storage → advances → persists. Dedupe on `contact_id` so a re-fired MutationObserver / double
  message can't double-advance.
- **H1 — no worker `setTimeout` for pacing.** Store `lastAdvanceTs`; on next wake, if
  `now - lastAdvanceTs < delay` defer, else advance. `chrome.alarms` has a ~30s floor (note it).
  Daily cap = stored `windowStart`+count evaluated on advance, never an in-worker countdown.
- **H2 — own + validate the target tab.** Persist `activeTabId` at Start (open a dedicated,
  labeled ApplyPilot tab, don't hijack the current one). Before every navigate: `chrome.tabs.get`
  → verify it exists and is on `linkedin.com`; if gone/repurposed, pause + prompt.
- **H4 — validate `linkedin_url`** against `^https://([a-z]+\.)?linkedin\.com/in/` before
  `tabs.update`; drop anything else (defends the foreground tab against a poisoned queue).
- **Sent-detection is positive, not inferred.** Add an `unknown`/ambiguous state that does NOT
  mark `sent`; require a positive toast/aria-live confirmation. Treat a **pre-existing** Pending
  badge as already-invited → skip (don't compose, don't false-mark sent).

# EXT-4 — Resilience + safety (fallbacks, caps, pacing)

**Phase:** 4 · **Size:** M · **Depends on:** EXT-2, EXT-3 · **Status:** Todo
**PRD:** §3.3, §6, §8 · Make it survive LinkedIn's UI variation and stay safe — the difference
between a demo and something usable across many real profiles.

## Summary
Layered selectors, handling for every profile state, a never-break "paste manually" fallback, a
daily cap, and human pacing — so the queue always makes progress, never sends the wrong thing,
and never trips LinkedIn's limits.

## Scope / tasks
- [ ] **Layered selectors (`selectors.json`, versioned):** for each target (Connect, More,
      Add-a-note, note textarea, Send-to-highlight) a ranked list: `aria-label` → visible text →
      role → structural fallback. A LinkedIn change becomes a config edit, not a rewrite.
- [ ] **Profile-state handling:**
  - [ ] already-connected / 1st-degree → skip (or hand to EXT-6 Message flow if present)
  - [ ] InMail/Premium-only (no free Connect) → skip with a clear reason
  - [ ] "Add a note" limit reached / weekly-invite-limit banner → **pause the queue**, tell the
        user, do not keep firing
  - [ ] profile 404 / redirected / stale URL → skip with reason
- [ ] **Never-break fallback:** if auto-compose can't complete, copy the note to the clipboard,
      show *"Couldn't auto-open the invite — your note is copied, paste it manually,"* mark the
      contact `manual`, and advance. The queue never stalls.
- [ ] **Daily cap:** configurable (default 20); enforced in the background worker; blocks
      advancing past the cap with a countdown to reset. Persisted across sessions.
- [ ] **Pacing:** configurable min delay between contacts (default a few seconds), plus small
      jitter, so it reads as human.
- [ ] **Pause / resume** from the popup at any point.

## Acceptance criteria
- Across ≥20 varied real profiles, the extension either auto-fills correctly or falls back to
  paste — and **never gets stuck** or sends the wrong recipient/note.
- Hitting the daily cap stops advancing with a clear message + reset countdown.
- A weekly-limit banner pauses the queue rather than hammering it.
- Selector changes can be made in `selectors.json` without touching logic.

## Tests
- Unit: selector-resolution order (first match wins; fallback used when earlier ones miss).
- Unit: cap enforcement + pacing gate.
- Manual: run a varied batch; deliberately hit an already-connected + an InMail-only profile;
  confirm graceful skip + clear reasons.

## Out of scope
Popup polish (EXT-5). Message flow (EXT-6).

---

## Review deltas (v2 — folded from `chrome-extension-review.md`)

- **B4 — free-note quota is a first-class path, not an edge.** Detect the remaining
  personalized-invite quota up front and cap the queue to it (or honor the chosen PRD §8
  posture); message clearly when exhausted. ~5/month on free accounts is the *default* state.
- **Cap counts human-sent invites, not just `sent`.** Manual-paste fallbacks are real invites —
  count `sent` + `manual` toward the daily cap (or a separate human-attempted counter), else
  volume silently exceeds the limit.
- **Clipboard fallback needs a user gesture.** `navigator.clipboard.writeText` from an
  auto-triggered content script lacks transient activation and fails silently. Use the overlay's
  gesture-backed **Copy note** button; only claim "copied" after `writeText` resolves.
- **Weekly-invite-limit ≠ daily cap.** Message distinctly ("resumes next week"), pause the queue
  rather than showing a daily countdown.
- **Selector self-check:** on N consecutive fallbacks-to-manual, surface a loud "LinkedIn layout
  changed — selectors need updating" state instead of pasting-manually forever.

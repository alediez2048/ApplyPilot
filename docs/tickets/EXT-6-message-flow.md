# EXT-6 — Message flow for 1st-degree connections (optional)

**Phase:** 6 · **Size:** M · **Depends on:** EXT-2 · **Status:** Backlog
**PRD:** §7 (Phase 6) · Extend the same human-sends model to direct messages for people you're
already connected to.

## Summary
For 1st-degree connections there's no "Connect" — the right action is **Message**. Auto-detect
degree and drive the message composer (open Message → fill the note verbatim → highlight Send →
human sends), reusing everything from EXT-2/EXT-3.

## Scope / tasks
- [ ] **Degree detection:** read the profile's connection degree (1st vs 2nd/3rd) from the DOM.
- [ ] **Path selection:** 1st-degree → **Message** composer; else → the Connect+note flow (EXT-2).
      Prefer Connect unless a free Message is clearly available.
- [ ] **Message compose:** open the composer, React-safe fill the note into the message box,
      highlight Send. **Human clicks Send** (same non-negotiable).
- [ ] **Sent detection:** message appears in the thread / composer clears → report `sent`,
      advance (reuse EXT-3).
- [ ] **Note variant (optional):** allow a distinct message-vs-connection note per contact if the
      pipeline drafts both (it currently drafts one ≤300-char note; reuse it if not).

## Acceptance criteria
- On a 1st-degree connection, the Message composer opens pre-filled with the note; the human
  sends; status records `sent` and the queue advances.
- Degree misdetection is safe: defaults to the Connect flow, never sends via the wrong channel.

## Tests
- Unit: degree detection + path selection.
- Manual: send a message to a 1st-degree connection via the flow; confirm status + advance.

## Out of scope
Anything not about the 1st-degree Message path. Bulk/unattended sending (never).

---

## Review deltas (v2 — folded from `chrome-extension-review.md`)

- **Do NOT claim "reuse EXT-2's React-safe fill."** LinkedIn's Message composer is a
  **contenteditable** rich editor; `nativeInputValueSetter` no-ops there. This ticket needs a
  separate, verified insertion path (`execCommand insertText` / synthetic keystrokes) — treat it
  as its own spike, not a reuse.

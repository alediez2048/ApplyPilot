# EXT-2 — Auto-compose the note into the invite dialog (core)

**Phase:** 2 · **Size:** L · **Depends on:** EXT-1 · **Status:** Todo · **LOAD-BEARING SPIKE**
**PRD:** §2 (steps 4–5), §3.2, §3.3, §6 · This is the whole thesis: prove the note fills
correctly into a real LinkedIn React invite dialog, from inside the page, with the human doing
Send.

## Summary
The content script, on a target profile, dismisses interstitials, opens the Connect invitation
(directly or via **More**), clicks **Add a note**, fills the drafted note **verbatim** into the
textarea using the React-safe pattern, then highlights **Send** and shows an ApplyPilot overlay
prompting the human. **It never clicks Send.**

## Why the extension succeeds where the external agent failed
- **Live DOM, not a snapshot** — sees LinkedIn's modal the instant it renders.
- **React-safe fill** — `nativeInputValueSetter.call(textarea, note)` + dispatch a real `input`
  event so LinkedIn's React state actually registers the text (synthetic `.click()`/paste did
  not, which is why prior sends silently dropped).
- Runs as the real user → no CDP/bot fingerprint.

## Scope / tasks
- [ ] **Trigger:** background sets the "active contact" (note + name) for the current tab, then
      navigates the tab to `linkedin_url`; content script activates on load.
- [ ] **`content.js` compose sequence** (deterministic, layered selectors — see EXT-4):
  - [ ] dismiss any promo/interstitial modal (Escape / close button) before acting
  - [ ] locate + click **Connect** (top action bar), or open **More** then click **Connect**
  - [ ] click **Add a note**
  - [ ] focus the note textarea (NOT the top-nav search box) and **React-safe fill** the note
  - [ ] verify the textarea now holds the exact note (length + content check)
- [ ] **Overlay UI:** a small ApplyPilot card near the dialog: *"✍️ Note filled — review, then
      click Send."* with a subtle pointer to Send. Includes **Copy note** + **Skip** affordances.
      **Must never cover the Send button.**
- [ ] **Verbatim guarantee:** the note text comes only from ApplyPilot's stored draft (passed
      from background); the content script never generates or rewrites it.
- [ ] **Report `composed`** back to background (for popup status), but do not mark `sent` yet
      (that's EXT-3, on human Send).

## Acceptance criteria
- On a real 2nd/3rd-degree profile, the invite dialog opens and the **exact** drafted note is in
  the note field, correctly registered (visible char count matches), with the human only needing
  to click Send.
- Works when Connect is direct AND when it's hidden under **More**.
- If a promo modal is present, it's dismissed first (no lost clicks).
- The content script **never** clicks Send; verified by inspecting it never targets the Send
  control.

## Tests
- Unit (jsdom or a small harness): React-safe fill sets a controlled `<textarea>`'s value + fires
  `input`; the "find the note textarea, not the search box" selector logic.
- Manual: run across ≥5 varied real profiles; confirm the note lands every time (or falls back —
  EXT-4).

## Out of scope
Auto-advance / sent-detection (EXT-3). Fallback + caps (EXT-4). 1st-degree Message path (EXT-6).

---

## Review deltas (v2 — folded from `chrome-extension-review.md`)

- **H2 — pull assignment from storage by `tabId` on load** (not a live message to a possibly-
  dead worker). The content script reads its active-contact from `chrome.storage` keyed by its
  own tab.
- **H3 — recipient identity on the overlay + cross-check.** The overlay must show
  `Inviting: {full_name} — {title} at {company}` (same record as the note) and the content
  script must cross-check the on-page profile name against the intended contact **before**
  showing "ready to Send"; mismatch → refuse + fallback. This is the human safety gate for a
  desynced pointer.
- **Confirm the field type + branch the fill.** The spike must explicitly confirm the invite
  note is still a real `<textarea>` (LinkedIn A/B-tests this). `nativeInputValueSetter` works for
  input/textarea only; contenteditable (EXT-6) needs `execCommand insertText`/synthetic
  keystrokes — branch by element type.
- **Non-negotiables:** overlay built with `textContent`/DOM APIs, **never `innerHTML`** (queue
  strings are attacker-influenceable); accept instructions only over `chrome.runtime` (verify
  `sender`), ignore `window.postMessage`; engineered Send-collision avoidance (measure Send's
  live rect, anchor to a corner, `pointer-events:none`) — "never covers Send" is a spec, not an
  assertion.

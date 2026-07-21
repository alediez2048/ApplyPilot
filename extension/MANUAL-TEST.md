# Manual test checklist — ApplyPilot LinkedIn Assistant

These verify the **browser-facing** behavior that automated tests can't reach: real LinkedIn DOM,
MV3 worker eviction, clipboard gestures, and the human-clicks-Send boundary. Run them against a
**test LinkedIn account** you don't mind sending a few real invitations from, with a small
`dailyCap` (e.g. 2–3) while testing.

Setup for every test:
- `applypilot dashboard --serve` running on `:8765`, with a few LinkedIn contacts drafted
  (each has a `linkedin_url` + `linkedin_message`, `dm_status` not done).
- Extension loaded unpacked, token pasted, connection dot green.
- DevTools open on the LinkedIn tab (**Console**) and, separately, on the service worker
  (`chrome://extensions` → the extension → **service worker** link) so you can watch/kill it.

Legend: ☐ = check.

---

## 0. Sanity / load
- ☐ `chrome://extensions` shows the extension with **no "Errors" button**. (If `alarms` or a
  web-accessible resource were misconfigured, the worker throws on load and an error shows here.)
- ☐ Service-worker console logs no red errors on load.
- ☐ LinkedIn profile console shows `[ApplyPilot] content loaded; selectors vN` (N = the
  `selectors.json` version). If it says "using empty table," `selectors.json` isn't
  web-accessible — fail.

## 1. Token / auth boundary
- ☐ With **no token**, the popup shows "Connect to ApplyPilot" and **Start is disabled**.
- ☐ Paste a **wrong** token → dot stays red, error says the token was rejected; Start does not
  fetch a queue.
- ☐ Paste the **correct** token (`cat ~/.applypilot/ext_token`) → dot turns green, queue count
  populates on Refresh.

## 2. THE MV3 STATELESS GATE (the load-bearing test)
This proves run-state survives worker eviction between compose and Send.
1. ☐ Start a queue; let it compose the first contact (overlay: "Note filled — review, then click
   Send").
2. ☐ In the **service-worker** DevTools, click **"Stop"/terminate** the worker (or run
   `chrome.runtime.reload()`-free eviction: in `chrome://extensions` toggle the worker off via the
   Stop button). The worker is now dead; the LinkedIn tab and its overlay are untouched.
3. ☐ **Click Send on LinkedIn yourself.** The dead worker wakes on the content script's
   `SEND_DETECTED` message.
4. ☐ Confirm: the contact is recorded **sent** (check the dashboard / DB `dm_status`), the daily
   meter increments, and after the pacing delay the **same tab advances to the next contact**.
5. ☐ Repeat but kill the worker **during the pacing wait** (after a send, before the next
   navigate). Confirm the **≥30s alarm backstop** still advances to the next contact without any
   popup open. (If it never advances, the `alarms` permission/backstop is broken.)
6. ☐ Open the popup fresh (first render comes from storage): progress, queue, and phase match
   reality — proving the popup is a view, not the loop.

## 3. Compose on a real 3rd-degree profile (happy path)
- ☐ Point the queue at a genuine **2nd/3rd-degree** contact (not connected).
- ☐ The extension opens **Connect → Add a note** (directly or via **More → Connect**), fills the
  note, and the note box contains your draft **verbatim** (exact text, exact length — no
  truncation, no added signature).
- ☐ The real **Send** button is outlined; the overlay does **not** cover it.
- ☐ You click Send → invite goes out → overlay shows "Invitation sent ✓" → advances.

## 4. Identity-mismatch refusal (never wrong recipient)
- ☐ In the dashboard, temporarily point a queue row's `linkedin_url` at a **different** person's
  profile (name won't match `full_name`). Refresh the queue, Start.
- ☐ On that profile the extension **does not open Connect** and **never shows "ready to Send."**
  The overlay says "Wrong profile — not composing"; the popup shows a paused identity-mismatch
  error naming both the expected and on-page names.
- ☐ It stays paused (no auto-skip, no auto-send) until you intervene. Restore the URL afterward.

## 5. Never-break fallback (gesture-backed Copy note → manual)
- ☐ Force a compose failure: temporarily break a selector (e.g. blank out `connectButton` in
  `selectors.json`, bump version, reload) OR use a profile where Connect isn't directly available.
- ☐ The overlay shows "Couldn't auto-open the invite" with **Copy note** — and the queue is **not**
  stuck (you can also Skip).
- ☐ Click **Copy note**: it must say "Copied ✓" only after the copy actually succeeds, and the
  note is on your clipboard (paste to verify it's verbatim).
- ☐ That copy click advances the contact as **manual** (dashboard `dm_status=manual`,
  `dm_sent_at` stamped) and the **daily cap counts it** (meter +1). Restore selectors after.
- ☐ Do **three** fallbacks in a row → the popup/overlay warns that LinkedIn's layout may have
  changed.

## 6. Sent-detection accuracy (no false positives)
- ☐ Compose a contact, then **click Cancel / press Escape** instead of Send. Confirm it is **NOT**
  marked sent; the overlay offers re-open / copy / skip; status is unchanged.
- ☐ Compose a contact that is **already Pending** before you start (send them an invite manually
  first, then queue them). The extension must **skip** it (reason: already invited) — never
  mark it `sent` off the pre-existing Pending badge.
- ☐ Genuinely click Send → detection fires from the **new** Pending badge / sent toast, not from a
  stale one.

## 7. Daily cap
- ☐ Set `dailyCap` low (e.g. 2). Send/manual that many → the queue **pauses** with a distinct
  "Daily cap reached (n/cap) … resets in Xh Ym" message; it does not advance further.
- ☐ Skips do **not** count toward the cap (skip a contact and confirm the meter is unchanged).
- ☐ Confirm the window rolls: after 24h (or temporarily shorten to test), the count resets to 0.

## 8. Free-account note-quota + weekly-invite limits
- ☐ On a free account that has exhausted its ~monthly personalized-note allowance, when the invite
  dialog has **no "Add a note"** option (or shows the note-quota banner), the extension **pauses**
  with a **note-quota** message — distinct from the daily cap, and does **not** send a note-less
  invite.
- ☐ If you hit LinkedIn's **weekly invitation limit** banner, the extension pauses with a
  **weekly** message ("resumes next week") — explicitly different wording from the daily cap.

## 9. Tab ownership / never hijack
- ☐ While running, open other tabs and switch around. The extension only ever drives its **one
  dedicated** LinkedIn tab; it never composes in a tab you opened yourself.
- ☐ Close the dedicated tab mid-run → the queue **pauses** with "tab was closed — Resume to
  reopen." Resume reopens it and re-arms the active contact.
- ☐ Manually navigate the dedicated tab to a non-LinkedIn site → the next advance **pauses** rather
  than navigating a repurposed tab.

## 10. URL validation
- ☐ Put a bad `linkedin_url` in the queue (e.g. `https://evil.example.com/in/x` or a
  `linkedin.com/company/...` URL). The extension **auto-skips** it (never navigates there) and
  moves on. Only `https://…linkedin.com/in/…` URLs are ever opened.

## 11. Overlay safety (XSS / clicks)
- ☐ Set a contact's name/title/note (in the DB) to include HTML like
  `<img src=x onerror=alert(1)>`. Start. Confirm the overlay and popup render it as **literal
  text** — no alert, no injected node (proves `textContent`, not `innerHTML`).
- ☐ From the LinkedIn page console, run
  `window.postMessage({type:'ASSIGNMENT',contact:{id:'x'}}, '*')`. Confirm it is **ignored** (no
  compose starts) — the content script only accepts `chrome.runtime` messages from our extension.

## 12. Note edit round-trip
- ☐ Edit a note in the popup (respecting the 300-char counter) → Save. It POSTs to
  `/api/ext/note`, the stored/queue note updates, and if that contact is the active one on the
  page, the on-page note is re-filled with the new text.

---

### What "pass" means
Every failure path lands in exactly one of: **skip**, **manual (+advance)**, or **paused with a
clear reason** — never a stuck queue, never an auto-click on Send, never the wrong recipient.

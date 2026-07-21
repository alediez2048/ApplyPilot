# EXT-1 — MV3 extension skeleton + read-only queue

**Phase:** 1 · **Size:** M · **Depends on:** EXT-0 · **Status:** Todo
**PRD:** §4 · Stand up the extension shell and prove it can read the queue from ApplyPilot. No
page interaction yet.

## Summary
A loadable Manifest V3 extension with a popup, a background service worker, and localhost
connectivity, rendering the real outreach queue read-only. This is the scaffold EXT-2 builds on.

## Scope / tasks
- [ ] **Repo location:** `extension/` at the ApplyPilot root (ships with the repo; git-tracked;
      no build step required — plain JS/HTML/CSS to keep load-unpacked trivial).
- [ ] **`manifest.json`** (MV3): name, version, `background.service_worker`, `action` (popup),
      `content_scripts` registered for `https://*.linkedin.com/*` (empty stub for now),
      permissions: `storage`, `tabs`, `scripting`; host permissions `https://*.linkedin.com/*`
      and `http://localhost:8765/*`. **No `<all_urls>`, no remote hosts.**
- [ ] **`background.js`** (service worker): fetch `GET /api/ext/queue` from `localhost:8765`;
      cache the queue in `chrome.storage`; expose message handlers (`getQueue`, `refresh`).
      Handle "ApplyPilot not running" gracefully.
- [ ] **`popup.html` / `popup.js` / `popup.css`:** connection-status indicator (green dot when
      the server responds); render the queue list (name, title, company, note preview) read-only;
      a Refresh button.
- [ ] **Icons + minimal styling** (ApplyPilot mark).

## Acceptance criteria
- Extension loads unpacked in Chrome with no manifest/permission errors.
- With the dashboard running and a prepared queue, the popup shows the real contacts + notes.
- With the dashboard **off**, the popup shows a clear "ApplyPilot not connected" state, no crash.
- No LinkedIn interaction occurs yet (content script is a no-op stub).

## Tests
- Manual: load unpacked, open popup, confirm queue renders from the live API.
- background: queue fetch + cache + error path (server down) — exercised via the popup.

## Out of scope
Filling notes / any DOM work (EXT-2). Edit/skip/status controls (EXT-5).

---

## Review deltas (v2 — folded from `chrome-extension-review.md`)

- **B1 — persisted-state schema is the source of truth.** The worker holds NO durable state.
  Define one `chrome.storage` schema (`queue, cursor, activeContactId, activeTabId,
  per-contact status, progress, dailyCount, windowStart, lastAdvanceTs, settings`). Contract:
  every handler rehydrates on entry, writes back before returning; popup renders via
  `chrome.storage.onChanged`, not worker memory. (See PRD §4.1.)
- **Permissions minimality:** drop `tabs` (navigate via `chrome.tabs.update(tabId,{url})` under
  host permission) and `activeTab`; drop `scripting` (static content scripts need no
  `executeScript`). Keep `storage` + host perms only. Narrow the content-script match to
  `https://*.linkedin.com/in/*`.
- **Extension identity:** add a manifest `key` (stable ID) OR adopt the mutual shared token
  (preferred, H4) so the server↔extension trust survives reinstall/other machines.

# ApplyPilot Contacts — Chrome extension

A deliberately tiny extension. It does **three things, nothing else**:

1. **Pull contacts** from your latest ApplyPilot run
2. **Copy** the drafted outreach note for a contact
3. **Open** that contact's LinkedIn profile in a new tab

You copy the note, open their LinkedIn, paste, and send — by hand. The extension never
touches LinkedIn's page, never auto-composes, never auto-sends. That's on purpose: the old
auto-compose approach was unreliable, so it's gone.

## Install

1. `chrome://extensions` → enable **Developer mode** (top-right).
2. **Load unpacked** → select this `extension/` folder.
3. Start the dashboard: `applypilot dashboard --serve`. On startup it prints an
   **ext token** (also saved in `~/.applypilot/ext_token`).
4. Click the extension icon → paste the token → **Save**.

## Use

- Click the icon → **↻ Refresh contacts** to pull the latest.
- Contacts are grouped by company. Each card has the drafted note (editable),
  a **Copy note** button, and an **Open LinkedIn ↗** button.
- Copy → open their LinkedIn → paste into a connection note or message → send.

## How it works

Just a popup. It fetches `GET http://localhost:8765/api/ext/queue` from the local
dashboard (the `host_permissions` for localhost bypasses CORS) and renders the list.
No background service worker, no content scripts, no LinkedIn DOM automation.

Permissions: `storage` (remember the token) + host access to `localhost:8765` only.

## Files

- `manifest.json` — MV3, popup-only, minimal permissions
- `popup.html` / `popup.css` / `popup.js` — the entire UI + logic

# ApplyPilot LinkedIn Assistant (browser extension)

A Manifest V3 Chrome/Edge extension that turns your ApplyPilot outreach queue into a
**human-in-the-loop** LinkedIn invite workflow. For each drafted contact it opens the profile,
opens **Connect → Add a note**, fills your **verbatim** note, cross-checks the on-page name, and
highlights the **Send** button.

> **It never clicks Send. You always click Send.** The extension composes and highlights only.

It talks only to your local ApplyPilot dashboard (`http://localhost:8765`) and to
`linkedin.com`. Nothing leaves your machine.

---

## What it does (and does not) do

| Does | Does NOT |
|---|---|
| Navigates a single dedicated LinkedIn tab through your queue | Click Send / submit invitations for you |
| Opens Connect, adds your note verbatim, verifies the field holds it exactly | Generate, rewrite, or truncate your note (that's the dashboard) |
| Cross-checks the on-page profile name vs the intended contact before showing "ready to Send" | Ever compose on the wrong person (it pauses on a mismatch) |
| Falls back to a gesture-backed **Copy note** you paste by hand, then advances | Get stuck — every failure routes to skip / manual / pause |
| Enforces a daily cap + pacing between contacts | Bypass LinkedIn's weekly-invite or note-quota limits (it pauses and tells you) |
| Detects a real Send (Pending appears / sent toast) and advances | Mark "sent" on a guess — a pre-existing Pending is treated as already-invited (skip) |

The extension is **stateless where it matters**: all run-state lives in `chrome.storage.local`,
so the loop survives the MV3 service worker being evicted mid-run.

---

## Requirements

- Google Chrome or Microsoft Edge (Chromium) **116+**.
- The **ApplyPilot dashboard running locally**:
  ```bash
  applypilot dashboard --serve        # serves http://localhost:8765
  ```
  This is what generates the auth token and serves the queue of drafted LinkedIn contacts. The
  extension is inert without it.
- Contacts in your ApplyPilot DB that have **both** a `linkedin_url` **and** a drafted
  `linkedin_message` (the note), and whose `dm_status` is not already `sent`/`manual`/`skipped`.
  Draft notes in the dashboard's outreach panel first.
- You must be **logged into LinkedIn** in the same browser profile.

---

## Install (load unpacked)

1. Open `chrome://extensions` (or `edge://extensions`).
2. Toggle **Developer mode** on (top-right).
3. Click **Load unpacked**.
4. Select this folder:
   ```
   /Users/jorgealejandrodiez/Desktop/ApplyPilot/extension
   ```
5. The **ApplyPilot LinkedIn Assistant** card appears. Pin it (puzzle-piece icon → pin) so the
   popup is one click away.

The extension ID will look random — that is expected and fine. Trust between the extension and
the dashboard is established by the **shared token** (below), not by a fixed ID, so you never
need to configure an allowlist.

### Permissions it asks for (and why they are minimal)

- `storage` — the entire run-state (queue, cursor, daily count, settings, token).
- `alarms` — a **≥30-second backstop timer** only, to wake the stateless worker and finish a
  paced advance. No data access. (The worker never uses `setTimeout` for pacing.)
- Host access to `https://*.linkedin.com/*` and `http://localhost:8765/*` — the only two hosts
  it can touch.

It deliberately does **not** request `tabs`, `activeTab`, `scripting`, or `<all_urls>`, and its
content script runs **only** on `https://*.linkedin.com/in/*` (profile pages).

---

## One-time setup: the `~/.applypilot/ext_token`

The dashboard generates a random token on first run and writes it to:

```
~/.applypilot/ext_token          (permissions 0600)
```

Every request the extension makes carries this token in the `X-ApplyPilot-Token` header, and the
server rejects any request without the right token with `401`. This is what stops a rogue process
squatting on port `:8765` from feeding the extension a poisoned queue — and the extension likewise
refuses a server that won't accept the token.

**To link the extension once:**

1. Start the dashboard (`applypilot dashboard --serve`) and open `http://localhost:8765`.
   The dashboard displays the extension token (it is also the file above — you can read it with
   `cat ~/.applypilot/ext_token`).
2. Click the extension's toolbar icon. The popup shows **"Connect to ApplyPilot"**.
3. Paste the token and click **Save**. The connection dot turns green ("Connected").

The token persists in the extension's storage; you only do this once (repeat if you regenerate
the token or reinstall the extension).

---

## Running a queue

1. Confirm the popup's connection dot is **green** (dashboard reachable + token accepted).
2. (Optional) Open **Settings** in the popup to set:
   - **Daily cap** — max invites (sent + manual) per rolling 24h window (default 20).
   - **Pacing** — minimum seconds between advancing to the next contact (default 8).
   - **Job filter** — blank = all jobs (deduped by profile URL); or paste one job URL to scope
     the queue to that job's contacts.
3. Click **Start**. The extension:
   - fetches the queue from `GET /api/ext/queue`,
   - opens one **dedicated** LinkedIn tab,
   - navigates it to the first contact's profile.
4. On the profile, the on-page **overlay** (bottom corner, kept clear of the Send button) shows
   who you're inviting and the compose status. When it says **"Note filled — review, then click
   Send,"** the note is in the box and the real **Send** button is outlined in indigo.
5. **Read the note, then click Send yourself.** The extension detects the send (the Pending badge
   / sent toast) and, after the pacing delay, advances the same tab to the next contact.
6. Repeat until the queue is exhausted, the daily cap is hit, or LinkedIn shows a weekly/note
   limit (the overlay and popup explain each stop distinctly).

### Controls
- **On the page overlay** (works even though the popup closes when you click Send): **Pause /
  Resume**, **Skip**, **Copy note**, live progress. This is the surface the running loop uses.
- **In the popup**: Start / Pause / Resume, **Next** (manual advance), **Refresh** (re-fetch the
  queue), per-contact **Edit note** / **Skip** / **Open profile**, the daily-cap meter, and
  Settings. The popup is for setup and queue management — it is never required for a send.

### When something can't be auto-composed
You are never stuck and never at risk of the wrong recipient:
- **Wrong profile / name mismatch** → it refuses to show "ready to Send" and pauses.
- **Already connected / already invited (Pending) / InMail-only / dead URL** → skipped.
- **Weekly invite limit / personalized-note quota** → paused with a distinct message (weekly
  "resumes next week"; note-quota is per free account).
- **Anything else** → the overlay offers **Copy note**; copying (a real click) is your gesture to
  paste + Send by hand, and the contact is recorded as **manual** and the queue advances.

---

## Files

| File | Role |
|---|---|
| `manifest.json` | MV3 manifest — permissions, content-script match, service worker, WAR. |
| `background.js` | Stateless service worker: queue/advance state machine, pacing, caps, tab ownership, URL validation, API calls. |
| `content.js` | The only file that touches the LinkedIn DOM: compose sequence, identity gate, `textContent`-only overlay, positive send-detection. |
| `popup.html` / `popup.js` / `popup.css` | Setup + queue-management panel; renders from `chrome.storage` via `onChanged`. |
| `selectors.json` | Versioned, layered LinkedIn selector table. A LinkedIn UI change is a config edit here. |
| `shared/constants.js` | **Frozen** single source of truth for message types, storage keys, API paths, enums. |
| `CONTRACTS.md` | The frozen contract (schemas, protocol, invariants) all files implement. |
| `MANUAL-TEST.md` | Manual verification checklist for browser-facing behavior. |

## Updating selectors when LinkedIn changes
Edit `selectors.json`, bump its `version`, and reload the extension. Each `target` is a ranked
list of strategies (`aria-label` → visible text → role → structural CSS); the first visible,
enabled match wins. `sendButton` is resolved only to highlight/measure — it is never clicked.

## Troubleshooting
- **Dot stays red** → is `applypilot dashboard --serve` running on `:8765`? Is the token current?
  Re-paste from `cat ~/.applypilot/ext_token`.
- **"No eligible LinkedIn contacts"** → draft LinkedIn notes in the dashboard first; contacts need
  a `linkedin_url` + a `linkedin_message` and must not already be sent/manual/skipped.
- **Every contact falls back to Copy note** → LinkedIn likely changed its DOM; update
  `selectors.json`. The overlay/popup will also warn after several fallbacks in a row.
- **The tab was closed** → the queue pauses; click **Resume** to reopen it.

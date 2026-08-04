# ApplyPilot Contacts — Chrome extension

A deliberately tiny extension. It does **four things, nothing else**:

1. **Pull contacts** from your latest ApplyPilot run
2. **Copy** the drafted outreach note for a contact
3. **Open** that contact's LinkedIn profile in a new tab
4. **Read a conversation you have open** and log it against the right person

It never auto-composes and never auto-sends. You copy the note, open their LinkedIn, paste,
and send — by hand. The old auto-compose approach was abandoned twice and it's gone.

## Install

1. `chrome://extensions` → enable **Developer mode** (top-right).
2. **Load unpacked** → select this `extension/` folder.
3. Start the dashboard: `applypilot dashboard --serve`. On startup it prints an
   **ext token** (also saved in `~/.applypilot/ext_token`).
4. Click the extension icon → paste the token → **Save**.

## Use

**Sending a note.** Click the icon → **↻ Refresh contacts**. Contacts are grouped by company;
each card has the drafted note (editable), **Copy note**, and **Open LinkedIn ↗**. Copy → open
their LinkedIn → paste → send.

**Logging a conversation.** With a LinkedIn thread open in the active tab, click the icon →
**📥 Read this thread**. Every message is listed with its direction and the timestamp it will
be stored under; flip any direction, skip any message, confirm who it's for, then **Log**.
Logging an inbound message stops that person's LinkedIn follow-up ladder, the same way a
detected email reply stops the email one.

Re-reading a thread you've already logged is a no-op — the same messages resolve to the same
timestamps, so nothing duplicates. Go back for the new message at the bottom whenever you like.

## Permissions, and what they can't do

| | |
|---|---|
| `storage` | remembers the token |
| host: `localhost:8765` | the local dashboard API (and bypasses CORS for it) |
| `activeTab` + `scripting` | reading the thread |

**`activeTab` is not a host permission.** Chrome grants it for one tab, only when you click the
extension icon, and only while the popup is open. There is still **no `linkedin.com` in the
manifest, no content script and no background service worker** — nothing here can run unattended
or touch a page you didn't click on. It reads a page already on your screen; it never clicks,
types, sends, or issues a request to LinkedIn.

## How the thread read works

`thread_parser.js` is injected by `chrome.scripting.executeScript` and returns what it finds.
It's a separate file so it can be run against a real DOM in the test suite
(`tests/test_linkedin_thread.py`, via jsdom) — a selector-driven parser that nothing exercises
goes dead the next time LinkedIn ships a redesign, and goes dead *quietly*.

Two things about LinkedIn's markup drive the whole design, both read off the live page rather
than assumed:

- **Messages are grouped.** Consecutive messages from one person share one name and one
  timestamp; only the first carries them. Reading per-message drops the sender on every
  continuation, and a message with no sender can't be given a direction.
- **There is no machine-readable timestamp.** `<time>` carries a class and nothing else. The
  date heading ("TODAY") and the group time ("2:38 AM") are all there is, so the stored time is
  *derived* — which is why the popup shows you each resolved timestamp before you log, and says
  so plainly when one couldn't be read.

Direction is decided by matching the **sender name to the contact**: the thread has two
participants, whichever one is the contact is *them*, and anyone else is *you*. That needs no
selector for "me" and no stored copy of your own name, so it survives the class rename that
would break LinkedIn's own `--other` marker. Names are matched **word by word**, never by
substring.

If LinkedIn does change its markup, you get *"found no messages — LinkedIn may have changed its
markup"*. Never a silent empty success.

## Files

- `manifest.json` — MV3, popup-only, minimal permissions
- `popup.html` / `popup.css` / `popup.js` — the UI + logic
- `thread_parser.js` — the injected reader (self-contained; tested under jsdom)

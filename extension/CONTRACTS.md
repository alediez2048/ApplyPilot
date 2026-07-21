# ApplyPilot LinkedIn Assistant — FROZEN CONTRACTS

**Status:** FROZEN — this is law for all EXT-0..EXT-6 builders. Do not diverge. A change here
is a contract change: update `shared/constants.js` in the same commit, bump `manifest.version`,
and note it in this file's changelog.

This document + `shared/constants.js` are the single source of truth for: the `chrome.storage`
run-state schema, the `chrome.runtime` message protocol, the local API, `selectors.json`, and the
`dm_status` lifecycle. The constants file holds the exact strings; this file holds the shapes,
semantics, and invariants.

---

## 0. Non-negotiables (every surface upholds these)

1. **The extension NEVER clicks Send.** It composes + highlights + shows the human "ready to
   Send." The human clicks Send. No code path targets the Send control for a click.
2. **The MV3 worker is STATELESS.** All run-state lives in `chrome.storage.local` under
   `STORAGE_KEYS`. Every handler rehydrates on entry, mutates, writes back before returning.
   Advance is **idempotent on `contact_id`**. Pacing is a **timestamp-compare on wake**
   (`now - lastAdvanceTs < delay`), never a worker `setTimeout`/`chrome.alarms` sub-30s timer.
   The popup renders from storage via `onChanged` and is NOT required for the running loop.
3. **The overlay is built with `textContent`/DOM APIs, NEVER `innerHTML`.** Queue strings
   (name/title/company/note) are attacker-influenceable. Content scripts accept instructions
   only over `chrome.runtime` and **must verify `sender`**; ignore `window.postMessage`.
4. **Never-break contract.** Any auto-compose failure → gesture-backed "Copy note" fallback +
   mark `manual` + advance. Never stuck, never wrong recipient. The content script cross-checks
   the on-page profile name against the intended contact before showing "ready to Send."
5. **Validate every `linkedin_url`** against `LINKEDIN_PROFILE_RE`
   (`^https://([a-z]+\.)?linkedin\.com/in/`) in the background BEFORE navigating. Drop anything
   else.
6. **Minimal permissions:** `storage` + host permissions for `https://*.linkedin.com/*` and
   `http://localhost:8765/*` only. No `tabs`, no `activeTab`, no `scripting`, no `<all_urls>`.
   Content-script match is narrowed to `https://*.linkedin.com/in/*`.

---

## 1. `chrome.storage.local` run-state schema (source of truth)

Keys are the values of `STORAGE_KEYS` in `shared/constants.js`. The worker owns none of this in
memory. Types below are authoritative.

| Key (`STORAGE_KEYS.*`) | JSON key | Type | Meaning |
|---|---|---|---|
| `QUEUE` | `queue` | `QueueContact[]` | Ordered, deduped-by-`linkedin_url` list of ready contacts. |
| `CURSOR` | `cursor` | `number` | Index into `queue` of the active contact; `-1` when idle. |
| `ACTIVE_CONTACT_ID` | `activeContactId` | `string \| null` | `queue[cursor].id`. The authority for "who is being invited now." |
| `ACTIVE_TAB_ID` | `activeTabId` | `number \| null` | The dedicated ApplyPilot LinkedIn tab. Never "the current active tab." |
| `STATUS_MAP` | `statusMap` | `{ [contactId: string]: UI_STATE }` | Transient per-contact chip state for popup + overlay. |
| `PROGRESS` | `progress` | `{ sent: number, total: number }` | Live progress; drives `N/total` on overlay + popup. |
| `DAILY_COUNT` | `dailyCount` | `number` | Human-sent invites (`sent`+`manual`) in the current window. |
| `WINDOW_START` | `windowStart` | `number` (epoch ms) | Start of the current 24h daily-cap window. |
| `LAST_ADVANCE_TS` | `lastAdvanceTs` | `number` (epoch ms) | Timestamp of the last advance; pacing compares against it on wake. |
| `SETTINGS` | `settings` | `Settings` | See `DEFAULT_SETTINGS`. |
| `RUNNING` | `running` | `boolean` | The "queue active" gate. Content script is inert when `false`. |
| `PAUSED` | `paused` | `boolean` | User/limit hold. `running` stays `true` while paused. |
| `PHASE` | `phase` | `RUN_PHASE` | Coarse lifecycle of the active contact. |
| `CONSECUTIVE_FALLBACKS` | `consecutiveFallbacks` | `number` | Count for the "layout changed" self-check. Reset to 0 on any success. |
| `SERVER_ONLINE` | `serverOnline` | `boolean` | Last known `localhost:8765` reachability (connection dot). |
| `TOKEN` | `extToken` | `string` | Mutual shared token; sent on every API call. |
| `LAST_ERROR` | `lastError` | `string` | Last surfaced error/limit message for the popup + overlay. |

### `QueueContact` (one queue row — from `GET /api/ext/queue`, plus derived note edits)
```jsonc
{
  "id": "string",            // contact_id (contacts.id); the idempotency key for advance
  "full_name": "string",
  "title": "string",
  "company": "string",
  "linkedin_url": "string",  // MUST match LINKEDIN_PROFILE_RE before navigation
  "note": "string"           // <=300 chars; verbatim text the content script fills
}
```

### `Settings` (`DEFAULT_SETTINGS`)
```jsonc
{
  "dailyCap": 20,            // max sent+manual per 24h window
  "pacingSeconds": 8,        // min seconds between advancing to the next contact
  "confirmBeforeEach": false,// require an explicit overlay confirm before composing next
  "jobFilter": null          // null = all jobs; else a specific job_url string
}
```

### Invariants
- **Rehydrate → mutate → persist**, every handler, every wake. Never read state from a
  module-level variable that outlives a message.
- **Idempotent advance:** advancing is keyed on the incoming `contactId`. If
  `contactId !== activeContactId` (already advanced past it), the advance is a no-op. A re-fired
  MutationObserver or a double `SEND_DETECTED` must not double-advance.
- **Pacing:** on wake, if `now - lastAdvanceTs < settings.pacingSeconds*1000`, set
  `PHASE=pacing` and defer (re-evaluate on the next wake / a `chrome.alarms` tick ≥30s);
  else advance and stamp `lastAdvanceTs = now`. Never `setTimeout(advance, ms)` in the worker.
- **Daily cap:** evaluated on advance from stored `windowStart` + `dailyCount`. If
  `now - windowStart >= 24h`, roll the window (`windowStart=now`, `dailyCount=0`). Both `sent`
  and `manual` increment `dailyCount` (manual fallbacks are real invites). When
  `dailyCount >= dailyCap`, set `PAUSED=true`, `PHASE=paused`, write `LAST_ERROR`, stop advancing.
- **Tab ownership:** before every navigation, `chrome.tabs.get(activeTabId)`; if it doesn't
  exist or isn't on `linkedin.com`, set `PAUSED=true` and surface a re-open prompt. Never fall
  back to the current active tab.

---

## 2. `chrome.runtime` message protocol

Every message is `{ type: <MSG.*>, ...payload }`. Every `onMessage` handler that acts on page
state MUST verify `sender` (a content-script message has `sender.tab`; a popup message does not).
Content scripts ignore `window.postMessage` entirely.

### Assignment model — PULL by tabId (not push)
The background never pushes an assignment to a possibly-dead worker's counterpart. Instead:

1. On content-script load (and after each navigation completes), the content script sends
   `GET_ASSIGNMENT { tabId }`. It doesn't reliably know its own tabId, so `tabId` is best-effort;
   **the background's authoritative source is `sender.tab.id`** and it cross-checks
   `payload.tabId === sender.tab.id` when present.
2. The background rehydrates from storage. If `sender.tab.id === activeTabId` **and** `running`
   **and** `!paused`, it responds with `ASSIGNMENT { contact: queue[cursor], settings, running,
   phase }`. Otherwise it responds `ASSIGNMENT { contact: null, running, phase }` and the content
   script stays inert.
3. The content script composes for `contact`, cross-checks the on-page name, and reports back.

This is "pull": the live content script initiates; the background only reads storage. No message
is ever sent to a worker that might be evicted.

### Content/overlay → background

| `MSG.*` | Payload | Background behavior |
|---|---|---|
| `GET_ASSIGNMENT` | `{ tabId }` | Rehydrate; respond `ASSIGNMENT` (see above). |
| `COMPOSE_RESULT` | `{ contactId, ok, reason }` | `ok`→ set `STATUS_MAP[contactId]=composed`, `PHASE=ready_to_send`. `!ok`→ route by `reason` (fallback/skip/pause) per §5. `reason` ∈ `COMPOSE_FAIL_REASON`. |
| `SEND_DETECTED` | `{ contactId }` | **Idempotent.** If `contactId===activeContactId`: `POST /status {sent}`, `STATUS_MAP=sent`, `progress.sent++`, `dailyCount++`, then advance. Else no-op. |
| `FALLBACK_MANUAL` | `{ contactId, reason }` | `POST /status {manual}`, `STATUS_MAP=manual`, `dailyCount++`, `progress.sent++`, advance. `consecutiveFallbacks++`. |
| `SKIP_CONTACT` | `{ contactId, reason }` | `POST /status {skipped}`, `STATUS_MAP=skipped`, advance. Does NOT increment `dailyCount`. |
| `IDENTITY_MISMATCH` | `{ contactId, onPageName }` | `PAUSED=true`, `PHASE=paused`, write `LAST_ERROR`. Do NOT show ready-to-Send. Human resolves. |
| `LAYOUT_CHANGED` | `{ consecutive }` | Set `LAST_ERROR` to the loud "LinkedIn layout changed — selectors need updating" state; `PAUSED=true`. |
| `LIMIT_BANNER` | `{ contactId, kind }` | `kind∈{"weekly_invite","note_quota"}`. `PAUSED=true`; `LAST_ERROR` messaged **distinctly** from the daily cap ("resumes next week"). |
| `OVERLAY_PAUSE` | `{}` | `PAUSED=true`, `PHASE=paused`. |
| `OVERLAY_RESUME` | `{}` | `PAUSED=false`; resume from `cursor`. |
| `OVERLAY_SKIP` | `{ contactId }` | Same as `SKIP_CONTACT` with `reason="user_skip"`. |

### Background → content/overlay

| `MSG.*` | Payload | Content behavior |
|---|---|---|
| `ASSIGNMENT` | `{ contact, settings, running, phase }` | If `contact` & `running` & `!paused`: run the compose sequence. Else stand down (inert). |
| `ABORT` | `{}` | Stop any in-flight compose, tear down the overlay's action affordances, stand down. |

### Popup ↔ background

| `MSG.*` | Payload | Behavior |
|---|---|---|
| `GET_STATE` | `{}` | Respond with the full state object (all `STORAGE_KEYS`). Popup then subscribes to `chrome.storage.onChanged`. |
| `START_QUEUE` | `{ jobFilter }` | Fetch queue (§3), open the dedicated tab, store `activeTabId`, `cursor=0`, `running=true`, navigate to `queue[0]`. |
| `PAUSE_QUEUE` | `{}` | `PAUSED=true`. |
| `RESUME_QUEUE` | `{}` | `PAUSED=false`; re-arm active contact. |
| `NEXT` | `{}` | Manual advance to the next ready contact. |
| `SKIP` | `{ contactId }` | Mark `skipped` (POST), advance. |
| `REFRESH_QUEUE` | `{}` | Re-fetch `GET /api/ext/queue`, rebuild `queue` (preserve `cursor`/`activeContactId` if still present). |
| `UPDATE_SETTINGS` | `{ settings }` | Merge into `SETTINGS`, persist. |
| `SAVE_NOTE` | `{ contactId, note }` | `POST /api/ext/note`; on ok update `queue[i].note`. If it's the active/composed contact, re-run the React-safe fill (or lock — see EXT-5). |
| `SET_TOKEN` | `{ token }` | Persist `STORAGE_KEYS.TOKEN`. |

> **Note:** the popup closes on every Send (focus moves to the page). The running loop MUST NOT
> depend on the popup. Live progress, Pause, Skip, and recipient identity live on the on-page
> overlay. The popup is for setup/queue management + settings only.

---

## 3. Local API contract (ApplyPilot dashboard, `localhost:8765`)

New endpoints on the existing loopback server (`web_dashboard.py`). Base URL, paths, and the auth
header are `API.*` in `shared/constants.js`.

### Auth — mutual shared token
- The server generates a random token at first run and writes it to **`~/.applypilot/ext_token`**
  (0600). It also serves it to the local operator (dashboard shows it) so the user can paste it
  into the extension popup once. The extension persists it in `STORAGE_KEYS.TOKEN`.
- **Every** extension request sends `API.TOKEN_HEADER` (`X-ApplyPilot-Token: <token>`).
- The server rejects a missing/wrong token with `401`. The extension treats a server that
  rejects/omits the token as untrusted (`serverOnline=false`) and refuses its queue — this is the
  defense against a `:8765` squatter feeding a poisoned queue, and it sidesteps the unstable
  load-unpacked extension ID.
- `GET /api/ext/queue` additionally applies the **Host-loopback half** of `_origin_ok` (Host must
  be loopback). It does NOT apply the Origin half (the extension's `chrome-extension://` Origin
  would fail it). **No CORS headers** — the extension reads via host-permission bypass.
- State-changing POSTs apply the token check; the existing `_origin_ok` Origin allowlist is
  extended to accept the `chrome-extension` scheme (by scheme, never a hardcoded ID).

### `GET /api/ext/queue[?job_url=<url>]`
Ready LinkedIn contacts. `job_url` present → per-job via `_eligible_contact_ids(job_url,
"linkedin")`. Omitted → **all-jobs variant**: single SELECT over `contacts` filtered by
`linkedin_url` present AND `linkedin_message` present AND `dm_status NOT IN
('sent','manual','skipped')`, then **deduped by normalized `linkedin_url`** (same person across
two jobs = one queue row).

Response:
```jsonc
{
  "ok": true,
  "contacts": [
    { "id": "c_123", "full_name": "Jane Roe", "title": "Eng Manager",
      "company": "BetterUp", "linkedin_url": "https://www.linkedin.com/in/janeroe/",
      "note": "Hi Jane — ..." }   // note = contacts.linkedin_message, <=300 chars
  ]
}
```
Only contacts with a `linkedin_url` **and** a `note` **and** `dm_status` not in the done-set
appear. `composed` contacts remain eligible (the human hasn't sent yet).

### `POST /api/ext/status`
```jsonc
// request
{ "contact_id": "c_123", "status": "sent" }   // status ∈ POSTABLE_STATUSES: sent|manual|skipped
// response
{ "ok": true }
```
Mapping (server): `sent → store.mark_dm_sent` (stamps `dm_sent_at` via COALESCE),
`manual → store.mark_dm_manual` (stamps `dm_sent_at`; counts toward dedupe/cap),
`skipped → store.mark_dm_skipped` (no stamp; excluded from queue). Unknown status → `400`.

### `POST /api/ext/note`
```jsonc
// request
{ "contact_id": "c_123", "note": "Hi Jane — ..." }   // capped to NOTE_MAX_LEN (300) server-side
// response
{ "ok": true, "note": "Hi Jane — ..." }              // the stored (possibly truncated) note
```
Server calls `upsert_contact({'id': contact_id, 'linkedin_message': note[:300]})` **directly**.
It must NOT reuse `_save_or_regen_draft` (that path sets `outreach_status='drafted'`, blanks
`outreach_subject/message`, clobbers email state, and has no cap).

### Error envelope
Non-2xx responses return `{ "ok": false, "error": "<message>" }`. The extension surfaces the
message and keeps the queue safe (does not advance on a failed status POST).

---

## 4. `selectors.json` structure (versioned, layered)

Shipped in the extension, editable without touching logic. A LinkedIn UI change is a config edit.

```jsonc
{
  "version": 1,                    // bump on every edit; content script logs the active version
  "targets": {
    "connectButton":   [ /* ranked Strategy[] */ ],
    "moreButton":      [ ... ],
    "connectMenuItem": [ ... ],    // "Connect" inside the More dropdown
    "addNoteButton":   [ ... ],
    "noteTextarea":    [ ... ],    // the invite note field (must be a real <textarea>)
    "sendButton":      [ ... ],    // located only to HIGHLIGHT + collision-avoid; NEVER clicked
    "pendingBadge":    [ ... ],    // "Pending" state = invitation sent / pre-existing
    "dismissModal":    [ ... ]     // promo/interstitial close/Escape target
  }
}
```

Each target is a **ranked array of `Strategy` objects; first that resolves to a live, visible
element wins**. Layer order (also the recommended ranking): `aria-label` → visible `text` →
`role` → `structural`.

```jsonc
// Strategy
{
  "by": "aria-label" | "text" | "role" | "structural",
  "value": "string",         // aria-label substring, visible text, ARIA role, or CSS selector
  "match": "exact" | "contains", // default "contains" for aria-label/text; ignored for role/structural
  "scope": "document" | "dialog" | "actionBar" // where to search; default "document"
}
```

Resolution rules:
- Try strategies in array order; return the first **visible, enabled** match.
- `noteTextarea` MUST resolve to an `<input>`/`<textarea>` for the React-safe native-setter fill;
  if a contenteditable is found instead, treat as `NOTE_FIELD_NOT_FOUND` (branch is EXT-6).
- `sendButton` is resolved ONLY to measure its rect (overlay collision-avoidance) and to add a
  highlight. No code path issues a click on it.
- `pendingBadge` present **before** composing → the contact is already invited → `SKIP_CONTACT`
  with `reason=PENDING_ALREADY` (do not compose, do not mark `sent`).

---

## 5. `dm_status` lifecycle

Server enum (contacts.dm_status): `none | sending | composed | sent | manual | skipped | failed`.
The extension participates in these (`DM_STATUS` in constants):

```
none ──compose──▶ composed ──human Send (detected)──▶ sent      [DONE]
  │                   │
  │                   └──auto-compose fails──▶ (fallback) manual [DONE]
  │
  ├──InMail-only / already-connected / stale / pending──▶ skipped [DONE]
  └──(never touched by extension: sending/failed belong to the CLI path)
```

- **DONE set** = `sent | manual | skipped` (mirrors `web_dashboard._DM_DONE_STATUSES` and
  `DM_DONE_STATUSES` in constants). Done contacts are excluded from `/api/ext/queue` and never
  re-offered.
- **`composed` is NOT done** — the human hasn't sent, so it stays eligible and re-surfaces on the
  next fetch (the extension re-composes). This is intentional (survives worker eviction).
- **`sent`** requires a *positive* send signal (Pending appears after a fresh compose, or a sent
  toast/aria-live). A pre-existing Pending badge is `skipped` (already invited), never `sent`.
- **`manual`** and **`sent`** both stamp `dm_sent_at` server-side and both count toward the daily
  cap + 30-day cross-job dedupe. **`skipped`** stamps nothing.

---

## 6. Ownership map (which ticket implements which part of the contract)

| Contract section | Owning ticket(s) |
|---|---|
| §3 API endpoints, token, `store.mark_dm_*`, all-jobs variant | EXT-0 |
| §1 storage schema + rehydrate contract, manifest/permissions, token persist | EXT-1 |
| §2 pull-by-tabId, §4 selectors (fill), overlay + identity cross-check, `textContent`-only | EXT-2 |
| §2 cold-wake advance (idempotent), pacing, tab validation, `linkedin_url` validation, positive sent-detect | EXT-3 |
| §4 layered selectors, §5 skip/manual routing, caps (sent+manual), weekly/note-quota banners, layout self-check | EXT-4 |
| Popup polish, overlay-hosts-the-loop, note-edit-vs-composed, all-jobs dedupe | EXT-5 |
| Message-flow (contenteditable fill branch) — backlog | EXT-6 |

---

## 7. Importing the constants

- `background.js` and `popup.js` are ES modules (`"type": "module"` service worker; popup script
  `<script type="module">`): `import { MSG, STORAGE_KEYS, API, DM_STATUS, DEFAULT_SETTINGS } from
  "./shared/constants.js";`
- `content.js` (classic content script) loads the same file. Register `shared/constants.js` +
  `content.js` together in `content_scripts.js`, and have `constants.js` also assign to a
  namespaced global (guarded) for the classic context — OR dynamically
  `import(chrome.runtime.getURL("shared/constants.js"))`. Either way, **the same file** is the
  source; no string is ever re-typed elsewhere.

---

## Changelog
- v1 (frozen): initial contract. Storage schema, message protocol, local API, selectors,
  dm_status lifecycle. Matches `shared/constants.js` v1.

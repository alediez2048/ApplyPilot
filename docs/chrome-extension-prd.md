# PRD — ApplyPilot LinkedIn Assistant (Browser Extension)

**Status:** Draft v2 (revised after adversarial review — see `chrome-extension-review.md`)
**Owner:** Jorge · **Author:** Jorge + Claude
**Supersedes:** the browser-automation LinkedIn sender (`linkedin_dm.py`, now dormant)

---

## Headline assumptions (read first)

1. **The MV3 background is stateless.** Chrome evicts the service worker after ~30s idle —
   which is *exactly* the compose → (human reads) → Send pause. **All run state
   (queue, cursor, active-contact-id, active-tab-id, per-contact status, progress, daily
   count, window start, settings) lives in `chrome.storage`, not worker memory.** Every
   handler rehydrates from storage on entry and writes back before returning; the popup
   renders from storage via `chrome.storage.onChanged`. This is a load-bearing constraint,
   not an implementation detail (see §4.1).

2. **LinkedIn's personalized-invite quota is the common case, not an edge case.** Free/Basic
   LinkedIn accounts can attach a note to only **~5 connection invites per month**; past that,
   "Add a note" is greyed out and the extension's entire value is gone until reset. **Decision
   required (§8):** assume/require Premium, OR detect the remaining note quota up front and cap
   the queue to it, OR support note-less invites as a first-class path. Until decided, treat
   the tool as most useful with Premium.

3. **This is reliability, not invisibility.** In-page Connect/Add-a-note clicks are still
   `isTrusted=false` and observable to LinkedIn's own page JS. The extension's real win is that
   it (a) reliably fills the note into React state and (b) keeps volume human-paced with a human
   Send — **not** that it hides from LinkedIn. Account-restriction risk is *lower* than external
   automation, not *zero*. Per-account LinkedIn ToS is the true ceiling.

---

## 0. Why this exists (the thesis)

We spent significant effort trying to send LinkedIn connection notes by **driving a browser
from the outside** (agent-browser over CDP, an LLM choosing each click). It failed
repeatedly and inconsistently. The root causes, proven empirically:

1. **Driving a modern SPA from outside is fragile.** The accessibility snapshot couldn't
   see LinkedIn's React modals; synthetic `.click()` didn't trigger React handlers; CDP
   sessions hung; modal state was ephemeral across calls.
2. **An LLM deciding each click is non-deterministic.** Same page, different action — it
   mis-navigated, looped, and returned no-op. LinkedIn's UI also varies per profile by
   design, so a hand-coded deterministic path broke on the next profile.
3. **LinkedIn is actively adversarial.** Soft-blocks, silent drops, rate limits, and bot
   detection. External automation (CDP, headless artifacts, machine-timed clicks) is
   *itself a detection signal*. The more "agentic" it is, the more it looks like a bot.

**The reframe:** stop driving the browser from outside. Run the automation **inside the
user's own browser session** as an extension. This flips every failure mode at once:

| Failure mode | External agent (what we did) | Browser extension |
|---|---|---|
| See the invite modal / DOM | ✗ snapshot misses it | ✓ it *is* the page — full live DOM |
| Fill a React field | ✗ synthetic events ignored | ✓ native-setter + input event React honors |
| Detectability | ✗ CDP/headless = bot fingerprint | ✓ real session, human-paced (lower risk, not invisible) |
| Per-profile UI variation | ✗ breaks | ✓ resilient in-page selectors + retries + graceful fallback |
| Ban risk | ✗ high | ✓ low — the **human** clicks Send, human-paced |

The extension automates the tedious 90% (navigate, open Connect, fill the exact note,
advance the queue) and leaves the **one platform-defended action — clicking Send — to the
human**, which is exactly what makes it both *reliable* and *safe*.

---

## 1. Goals / Non-goals

### Goals
- Turn a prepared ApplyPilot outreach queue into a fast, guided "click Send, click Send"
  flow on LinkedIn, with each note pre-filled correctly.
- 100% reliable note composition (no fabricated/empty notes, no wrong recipient).
- Near-zero account-restriction risk (human sends, human-paced, daily cap).
- Zero external servers: the extension talks only to the local ApplyPilot dashboard.

### Non-goals
- **No automated clicking of Send.** Ever. That is the human's job (and the safety gate).
- No bulk/unattended blasting. Human-in-the-loop by design.
- No scraping/harvesting of LinkedIn data beyond what's needed to fill the current invite.
- Not (initially) a Chrome Web Store product — personal, load-unpacked use first (§8).

---

## 2. Primary user flow (end to end)

1. **Prepare in ApplyPilot** (existing): Apollo finds the right people; the pipeline drafts
   a ≤300-char note per contact. Everything lands in the local dashboard.
2. **Install once:** load the extension (dev mode). It auto-connects to `localhost:8765`.
3. **Open the queue:** the extension popup shows *"BetterUp — 5 contacts ready"* with each
   name, title, and its drafted note (editable inline).
4. **Start:** user clicks **Start queue**. The extension opens contact #1's profile in the
   active tab.
5. **Auto-compose (content script):**
   - dismisses any promo/interstitial modal,
   - finds and clicks **Connect** (or **More → Connect**),
   - clicks **Add a note**,
   - fills the note **verbatim** into the textarea (React-safe),
   - highlights the **Send** button and shows a small ApplyPilot overlay:
     *"Note ready — review and click Send."*
6. **Human sends:** user reads it, clicks **Send** (the only human action).
7. **Detect + advance:** the content script detects the "Pending"/sent state, reports
   `sent` back to ApplyPilot (updates `dm_status`), and **auto-opens the next contact**.
8. **Repeat** until the queue is empty. Popup shows live progress: *"3 / 5 sent."*
9. **Graceful fallbacks:** if it can't find Connect (already connected, InMail-only, or an
   unusual layout), it does **not** break — it surfaces *"Couldn't auto-open the invite —
   here's your note, paste it manually"* (note copied to clipboard) and still advances.

**Time cost to the user:** ~1 human click per contact, ~5–10 seconds each.

---

## 3. UX details

### 3.1 Popup (the control panel)
- **Header:** connection status to ApplyPilot (green dot = connected to localhost:8765).
- **Queue list:** one row per ready contact — name, title, company, note preview, ✏️ edit,
  ⏭️ skip. Status chip per row: `ready | composing | sent ✓ | needs manual | skipped`.
- **Controls:** `Start queue` / `Pause` / `Next`. Daily-cap meter (e.g. *"3 / 20 today"*).
- **Settings:** daily cap, pacing delay (min seconds between contacts), "confirm before
  each" toggle.

### 3.2 On-page overlay (content script)
- A small, non-intrusive ApplyPilot card pinned to the invite dialog area:
  - *"✍️ Note filled — review, then click Send."* with a subtle arrow to the Send button.
  - A **Skip** and **Copy note** affordance for the fallback case.
- Never covers the Send button. Never auto-clicks anything the user must approve.

### 3.3 States the content script must handle (per profile)
- Connect visible directly → click it.
- Connect hidden under **More** → open More, click Connect.
- Already connected / 1st-degree → offer **Message** flow instead (Phase 6) or skip.
- InMail/Premium-only (no free Connect) → skip with a clear reason.
- Promo/interstitial modal open → dismiss first.
- Weekly-invite-limit banner → pause the queue, tell the user.

---

## 4. Architecture (Manifest V3)

```
┌──────────────────────────────────────────────────────────────┐
│ Browser (user's real, logged-in session)                      │
│                                                                │
│  ┌────────────┐   messages   ┌──────────────────────────┐     │
│  │  Popup UI  │◀────────────▶│ Background service worker │     │
│  └────────────┘              │  - holds queue state      │     │
│                              │  - talks to localhost API │     │
│                              └───────────┬──────────────┘     │
│                                          │ inject / message    │
│                              ┌───────────▼──────────────┐     │
│  linkedin.com tab  ────────▶ │  Content script           │     │
│                              │  - read DOM, fill note    │     │
│                              │  - detect sent, advance   │     │
│                              └──────────────────────────┘     │
└───────────────────────────────┬──────────────────────────────┘
                                 │ HTTP (localhost only)
                    ┌────────────▼─────────────┐
                    │ ApplyPilot dashboard      │
                    │ localhost:8765            │
                    │  - serves outreach queue  │
                    │  - records sent status    │
                    └───────────────────────────┘
```

- **Content script** (`content.js`, injected on `linkedin.com/in/*`): the only piece that
  touches the LinkedIn DOM. Fills the note with the React-safe pattern
  (`nativeInputValueSetter.call(el, note)` + dispatch `input`), detects dialog + sent state,
  reports events over `chrome.runtime` only. **Never clicks Send.** On load it *pulls* its
  assignment from `chrome.storage` keyed by its `tabId` (pull, not push — no live message to a
  possibly-dead worker). Builds the overlay with `textContent`/DOM APIs, never `innerHTML`
  (queue strings are attacker-influenceable). Inert unless a "queue active" flag is set.
- **Background service worker** (`background.js`): **stateless — owns nothing in memory.**
  Coordinates the queue/pacing/cap by reading and writing `chrome.storage` on every wake;
  fetches the queue from and reports status to the local ApplyPilot API; navigates a
  dedicated, stored `activeTabId` (never "the current active tab").
- **Popup** (`popup.html/js`): setup + queue management only. It **closes on every Send**
  (clicking Send requires focusing the page), so the *running loop must not depend on it* —
  live progress, Pause, and recipient identity live on the on-page overlay (§3.2, H3). Renders
  from `chrome.storage` via `onChanged`.
- **Permissions (minimal):** `storage` + host permissions for `https://*.linkedin.com/*` and
  `http://localhost:8765/*`. Navigation uses `chrome.tabs.update(tabId, {url})` under host
  permission — **no `tabs` permission**, no `activeTab`. Static content scripts need no
  `scripting`. No `<all_urls>`, no remote hosts.

### 4.1 Persisted run-state (MV3 is stateless — load-bearing)

Because the worker is evicted mid-run, **one storage schema is the single source of truth:**

```
{ queue: [{contact_id, full_name, title, company, linkedin_url, note, status}],
  cursor: <index>, activeContactId, activeTabId,
  progress: {sent, total}, dailyCount, windowStart, lastAdvanceTs, settings }
```

Rules: every handler rehydrates from storage on entry, mutates, writes back before returning;
advance is **idempotent on `contact_id`** (a re-fired MutationObserver or double message must
not double-advance); pacing is a **timestamp compare on wake** (`now - lastAdvanceTs < delay`),
never a worker `setTimeout` (killed on teardown; `chrome.alarms` has a ~30s floor). **Test gate:**
kill the worker between compose and Send (`chrome://serviceworker-internals` or 30s idle) and
confirm advance still works.

---

## 5. Local API contract (new ApplyPilot dashboard endpoints)

The extension is a thin client over the existing local server. New endpoints:

- `GET  /api/ext/queue?job_url=<optional>` → the ready LinkedIn contacts:
  `[{ id, full_name, title, company, linkedin_url, note }]` (note = the ≤300-char draft;
  only contacts with a URL + note + not already sent).
- `POST /api/ext/status` `{ contact_id, status: "sent" | "manual" | "skipped", ts }` →
  updates `dm_status` (reuse `store.mark_dm_sent` / a new `mark_dm_manual`).
- `POST /api/ext/note` `{ contact_id, note }` → persist an inline edit (reuse the outreach
  save path).
- Guarded by the existing loopback Origin/Host check; extension origin
  (`chrome-extension://<id>`) added to the allowlist.

No new data model needed — it reuses the `contacts` table and its `dm_status` column.

---

## 6. Reliability strategy (what makes this different from the agent)

- **Live DOM, cheap polling.** The content script can re-query the real DOM on a short
  interval (MutationObserver) instead of a one-shot snapshot — it sees the modal the instant
  it renders.
- **Resilient, layered selectors.** For each target (Connect, More, Add-a-note, textarea,
  Send-highlight), try a ranked list: `aria-label` → visible text → role → structural
  fallback. Ship these as a versioned `selectors.json` so a LinkedIn change is a config
  edit, not a code rewrite.
- **React-safe fill.** Use the native value setter + `input` event so LinkedIn's React state
  registers the note (this is the piece synthetic automation kept getting wrong).
- **Never-break contract.** Any failure → fall back to "here's your note, paste it" and
  advance. The queue always makes progress; the user is never stuck.
- **Human gate = correctness + safety.** Because a person clicks Send, wrong-recipient and
  malformed-note errors are caught by a human, and LinkedIn sees a real human action.

---

## 7. Implementation phases

**Phase 0 — Local API (small, in ApplyPilot).**
`/api/ext/queue`, `/api/ext/status`, `/api/ext/note`; extension-origin allowlist; tests.

**Phase 1 — Extension skeleton.**
MV3 manifest, popup shell, background worker, localhost connectivity, render the queue list
(read-only). No page interaction yet. *Exit:* popup shows the real queue from ApplyPilot.

**Phase 2 — Auto-compose (the core).**
Content script: dismiss modal → Connect (incl. via More) → Add a note → React-safe fill →
highlight Send + overlay. Manual advance (user clicks Next). *Exit:* on a real profile, the
note is correctly filled and the user just clicks Send.

**Phase 3 — Detect + auto-advance.**
Detect Pending/sent, POST `sent`, open the next contact automatically, update progress.
*Exit:* flow through a 3-contact queue with one human click each.

**Phase 4 — Resilience + safety.**
Selector fallbacks, already-connected / InMail-only / weekly-limit handling, graceful
"paste manually" fallback, daily cap, pacing delay, pause/resume. *Exit:* runs across a
varied set of profiles without ever getting stuck.

**Phase 5 — Polish.**
Inline note edit, per-row status, skip, progress UI, connection-status indicator, settings.

**Phase 6 — (optional) Message flow.**
For 1st-degree connections, drive the Message composer instead of Connect (same human-sends
model). Auto-detect degree and pick the path.

---

## 8. Risks & open decisions

- **⚠ Free-account note quota (decision required — see Headline Assumption 2).** ~5 note
  invites/month on free/Basic; the extension's whole value paywalls by ~invite #6. Pick a
  posture: (a) require/assume Premium, (b) detect remaining quota up front + cap the queue,
  (c) support note-less invites as a first-class path. *Default lean: (b) detect + cap, and
  message clearly.*
- **LinkedIn DOM changes** break selectors → mitigated by layered/versioned `selectors.json`
  + never-break fallback + an "N consecutive fallbacks → layout changed" alarm. Accept
  periodic maintenance; the "stable selectors" assumption is optimistic by default.
- **Detectability is not zero.** In-page synthetic Connect/Add-a-note clicks are
  `isTrusted=false` and visible to LinkedIn's JS. The win is reliability + human-paced volume;
  do **not** market this as invisible. Keep daily cap + pacing conservative.
- **Distribution.** Personal use = **load unpacked** (dev mode). Note the dev-mode startup nag
  and manual updates. An *unlisted* Web Store listing is **not** an easy escape hatch — it goes
  through the same automation-policy review. *Decision needed: personal-only for now? (default:
  yes.)*
- **ToS posture / true ceiling.** Per-account LinkedIn ToS is the real limit on §9's "zero
  restrictions." This is an *assistant* (human sends, human-paced) — the lowest-risk stance,
  but still ToS-adjacent.
- **Browser scope.** Chrome first (MV3); Edge/Brave compatible. Firefox later. *Default:
  Chrome-only v1.*
- **Local API auth.** Loopback + Host guard is the floor; adopt the optional shared token
  (`~/.applypilot`) as **mutual** auth — the extension refuses a server that can't present it
  (defends against a `:8765` squatter feeding a poisoned queue) and also sidesteps the unstable
  load-unpacked extension ID. *Default lean: ship the token.*

---

## 9. Success criteria

- A user with 5 prepared contacts sends 5 personalized connection requests in under ~2
  minutes, with **1 click each** and **0 malformed/wrong-recipient** notes.
- Across 20+ varied profiles, the extension either auto-fills correctly or falls back to
  paste — and **never gets stuck** or sends the wrong thing.
- **Survives worker eviction:** killing the service worker between compose and Send does not
  break advance (the MV3 stateless test gate, §4.1).
- **Ban-risk validation plan:** run a small, capped, human-paced batch over several days on a
  low-stakes account and monitor for warnings/restrictions before relying on it — "zero
  restrictions" is a hypothesis to validate, not an assumption, and is bounded by per-account
  ToS.

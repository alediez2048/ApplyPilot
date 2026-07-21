# Chrome Extension PRD + Ticket Board — Review

**Reviewed:** `docs/chrome-extension-prd.md` + `docs/tickets/EXT-0…EXT-6`
**Scope:** MV3 LinkedIn connection-note assistant (the pivot after external CDP automation failed)
**Method:** 5-dimension adversarial review (MV3 lifecycle, API/data integrity, security, product/UX, LinkedIn-platform reality), findings verified against the actual `web_dashboard.py` / `store.py` code.

---

## 1. Verdict: **GO WITH CHANGES**

The core thesis is sound and the pivot is correct: running inside the real session with a React-safe fill and a human Send genuinely flips the failure modes that sank the external agent — verified by the epic's own history (the prior `linkedin_dm.py` used *trusted* CDP keystrokes and still got soft-blocked, so the win here is reliability + human-paced volume, not invisibility). No finding sinks a phase; every EXT ticket remains buildable as sequenced. But the PRD/tickets are written as if the MV3 background were a persistent page — they put the queue cursor, active-contact→tab pointer, and pacing in worker memory that Chrome evicts after ~30s idle, which is *exactly* the compose→(human reads)→Send window. That plus two real data-integrity bugs in EXT-0 (skipped/manual contacts re-surface; sent rows never stamp `dm_sent_at`) and one product-posture gap (free LinkedIn accounts can attach a note to only ~5 invites/month — the extension's whole value hits a paywall by contact #6) must be fixed in the specs before coding. All are cheap, well-understood fixes that land inside existing tickets. Proceed once the blockers below are folded in.

---

## 2. Blockers (must fix before building)

**B1 — Treat the MV3 service worker as stateless; persist all run state to `chrome.storage`.**
The worker is evicted after ~30s idle — precisely during the human's read-and-Send pause (PRD §2 step 6; worse with "confirm before each"). EXT-1 persists only the queue *list*; the cursor, active-contact-id, active-tab-id, and progress are described as in-worker (EXT-3 "maintain the ordered queue + a cursor"), so on the respawn triggered by the content script's `sent` message the worker wakes with empty memory and auto-advance restarts or stalls.
*Fix:* Define one explicit persisted-state schema (`queue`, `cursor`, `activeContactId`, `activeTabId`, per-contact status map, `progress`, `dailyCount`, `windowStart`, `settings`). Every message handler rehydrates from storage on entry and writes back before returning. Popup renders from storage via `chrome.storage.onChanged`, not from worker memory. Add a verification step: kill the worker (`chrome://serviceworker-internals` or 30s idle) *between compose and Send* and confirm advance still works.
*Lands in:* **EXT-1** (state schema + rehydrate contract) and **EXT-3** (cold-wake advance uses it). Consolidates findings `mv3-worker-ephemeral-queue`, `mv3-storage-model-underspecified`, `mv3-worker-ephemeral-state`.

**B2 — Specify the cold-wake advance path end-to-end and make it idempotent.**
On detected Send the content script messages the worker to record `sent` + advance. That message cold-wakes a dead worker; no ticket defines "wake → rehydrate cursor from storage → advance → persist." A re-fired MutationObserver or double message could double-advance after a wake.
*Fix:* Spec the exact sequence: content-script send-event → `chrome.runtime.sendMessage` (carrying `contact_id`) → worker `onMessage` rehydrates from storage, advances, persists. Make the handler idempotent (dedupe on `contact_id`).
*Lands in:* **EXT-3**. Finding `mv3-sent-detection-wake-path`.

**B3 — EXT-0: exclude `manual` and `skipped` from queue eligibility (and from `dm_ready`).**
`_eligible_contact_ids(...,'linkedin')` (web_dashboard.py:297) drops only `dm_status=='sent'`; `dm_ready` (web_dashboard.py:674-676) does the same. EXT-0 adds new `manual`/`skipped` statuses and *asserts* they won't re-surface — but reusing the filter verbatim leaves them eligible, so every re-fetch (constant, because the worker keeps re-reading the server as the durable source) re-offers already-skipped/InMail-only/manual contacts, which then re-fail. This directly violates EXT-0's own acceptance criterion.
*Fix:* Change both gates to exclude a set: `dm_status in ('sent','manual','skipped')`. Explicitly decide `composed` is *not* excluded (human hasn't sent). Update the `store.py:42` enum comment (`none|sending|sent|failed` is already stale — missing `composed`). Add a queue-eligibility test covering manual/skipped. Make the ticket say "reusing `_eligible_contact_ids` requires *modifying* it, not just calling it."
*Lands in:* **EXT-0**. Findings `ext0-manual-skipped-not-excluded`, `manual-skipped-resurface`, `manual-vs-skipped-collapsed`.

**B4 — Product posture: state the free-account note quota; the extension's core value hits a paywall by ~invite #6.**
LinkedIn caps free/Basic accounts at ~5 *personalized* connection invites/month; past that, "Add a note" is greyed out. Pre-filling a ≤300-char note is the entire product, so on a free account this is the default state after a handful of contacts, not the EXT-4 edge case it's filed as. §9's "5 personalized connection requests" is essentially the whole free monthly allotment — the demo succeeds once, then the core function is dead until reset.
*Fix:* Add a headline assumption to the PRD: decide posture — (a) assume/require LinkedIn Premium, (b) detect remaining free-note quota up front and cap the queue to it, or (c) support note-less invites as a first-class path. Promote "note limit" from an EXT-4 edge bullet to a PRD §1/§8 assumption.
*Lands in:* **PRD §1/§8** (posture) + **EXT-4** (detect + cap behavior). Finding `free-note-limit-is-common-case-not-edge`.

**B5 — EXT-0: stamp `dm_sent_at` when `POST status:sent` flips a row to `sent`.**
EXT-0 maps `status:sent` straight to `store.mark_dm_sent` with no claim step, but `mark_dm_sent` (store.py:287) never sets `dm_sent_at` — in the old flow `claim_dm_send` set it first. Result: extension-sent rows have `dm_status='sent' AND dm_sent_at IS NULL`, so the still-live automated `network --send-dm` path's `already_dmed`/`dm_sent_today` window queries (which filter `dm_sent_at >= cutoff`) silently ignore them — breaking 30-day cross-job dedupe and undercounting the CLI daily cap, enabling a duplicate send to the same person.
*Fix:* Make `mark_dm_sent` stamp `dm_sent_at=now()` when it flips to `sent` (cleaner), or call `claim_dm_send` before it. Add a test asserting `dm_sent_at` is non-NULL after a `status:sent` POST and that `already_dmed` then finds the contact.
*Lands in:* **EXT-0** (+ `store.py`). Finding `mark-dm-sent-no-dm-sent-at`.

---

## 3. High-priority changes

**H1 — Pacing/timers must not rely on a worker `setTimeout`; use timestamp-compare-on-wake.**
EXT-3's "short delay between advancing" and EXT-4's "min delay + jitter" invite `setTimeout(advance, ms)` in the worker, which does not keep the worker alive and is lost on teardown — and `chrome.alarms` has a ~30s floor so it can't express a "3-second" delay. A conservative user-set 30–60s pace (which the PRD itself recommends) is squarely in the danger zone and would silently stall the queue, violating EXT-4's "never gets stuck."
*Fix:* Store `lastAdvanceTs`; on the next wake, if `now - lastAdvanceTs < delay`, defer; else advance. Drive any sub-second UI pacing from the content-script/popup context (alive with the page), not the worker. Note the 30s `chrome.alarms` floor in the ticket so nobody designs a sub-30s alarm. (The daily-cap "countdown" is already spec'd "persisted across sessions" — implement it as stored `windowStart`+count evaluated on each advance, never an in-worker countdown.)
*Lands in:* **EXT-3 / EXT-4**. Findings `mv3-timers-setTimeout-killed`, `mv3-worker-ephemeral-pacing-cursor`.

**H2 — Own a dedicated tab; persist and validate its `tabId`; never navigate a repurposed tab.**
The flow drives "the active tab" (PRD §2.4, EXT-2, EXT-3) with no stored tab identity. On worker restart or a popup-driven Next/Skip (where `sender.tab` is undefined), `chrome.tabs.update({url})` with no id falls back to the *current* active tab — clobbering whatever the user switched to and losing unsaved work; the running foreground tab is also effectively unusable during a batch.
*Fix:* Capture and store `activeTabId` in `chrome.storage` at Start (open a dedicated, clearly-labeled ApplyPilot tab rather than hijacking the current one). Before every `tabs.update`, `chrome.tabs.get` + verify the tab exists and is on `linkedin.com`; if closed/repurposed, pause and prompt to re-open. Content script pulls its assignment from storage keyed by `tabId` on load (pull, not push) so no live message to a possibly-dead worker is needed.
*Lands in:* **EXT-2 / EXT-3**. Findings `active-tab-hijack`, `active-tab-id-not-persisted`, `mv3-active-contact-handoff-race`.

**H3 — Move live progress + Pause/Skip + recipient identity onto the persistent on-page overlay.**
The chrome.action popup closes on focus loss — and clicking Send *requires* clicking the page, so the popup (the only place §3.1/EXT-5 put progress "3/5", Pause, Next) is closed on every single contact. Separately, the human-review-is-the-safety-gate thesis (PRD §6) is undermined because the overlay never shows *who* the invite goes to, so a desynced active-contact pointer (see B1) fills contact A's note on contact B's page with no cue for the human to catch it.
*Fix:* Put live progress (`N/total`), Pause/Hold, Skip, and — critically — `Inviting: {full_name} — {title} at {company}` (from the same record as the note) on the on-page overlay, which survives clicks. Have the content script cross-check the on-page profile name against the intended contact before showing "ready to Send"; mismatch → refuse/fallback. Keep the popup for setup/queue management.
*Lands in:* **EXT-2** (overlay content + identity check) + **EXT-5** (don't make the running loop depend on the popup). Findings `popup-closes-during-send`, `overlay-no-recipient`.

**H4 — Validate `linkedin_url` before navigating; the extension must authenticate the server, not just the client.**
When ApplyPilot is off, any local process can squat `:8765` and hand the extension a queue whose `linkedin_url` redirects the user's foreground tab anywhere. `_origin_ok` only authenticates inbound POST *clients*; it does nothing to authenticate the *server*, and no ticket validates the URL before `chrome.tabs.update`.
*Fix:* Hard-validate every `linkedin_url` against `^https://([a-z]+\.)?linkedin\.com/in/` in the background before navigating; drop/skip anything else. Keep the content-script host match to `linkedin.com` so a bad navigation can't carry injection. Adopt PRD §8's optional shared token as *mutual* (extension refuses a server that can't present it) — this is the clean fix and also solves the extension-ID instability (H5).
*Lands in:* **EXT-3** (URL validation) + **EXT-0/EXT-1** (token). Finding `rogue-listener-poisoned-queue-navigation`.

---

## 4. Medium / low

- **Origin guard for the extension is underspecified and fragile.** A load-unpacked extension's ID is path-derived (no `key` in EXT-1's manifest), so a hardcoded `chrome-extension://<id>` allowlist entry silently breaks on reinstall/other machine. Prefer allow-listing by *scheme* (accept `chrome-extension` origin, still Host-loopback-guarded) or pin the ID with a manifest `key`, or gate on the shared token (H4). *(EXT-0/EXT-1; `chrome-extension-id-unstable`, `ext-id-allowlist-instability`)*
- **`GET /api/ext/queue` has no loopback guard.** `_origin_ok` runs only in `do_POST`; the new GET returns contact PII + drafted notes and is readable via DNS-rebinding. Apply the Host-loopback half of `_origin_ok` to the GET (not the full check — the extension's `chrome-extension://` Origin would fail the Origin half). Do **not** add permissive CORS — the extension reads via host-permission bypass, so no `Access-Control-*` headers are needed. *(EXT-0; `queue-get-no-loopback-guard`, `get-queue-unguarded-cors`)*
- **`POST /api/ext/note` must not reuse the email outreach save path.** `_save_or_regen_draft` (web_dashboard.py:956-965) sets `outreach_status='drafted'` and writes empty `outreach_subject/message`, clobbering email state and dropping the contact from email dedupe/cap accounting; it also has no 300 cap. Call `upsert_contact({'id':cid,'linkedin_message':note[:300]})` directly and enforce the 300 cap server-side (the frontend only visually flags over-length). *(EXT-0; `note-save-reuse-side-effects`)*
- **Daily cap counts only `sent`; manual-paste fallbacks are real invites that don't count.** When auto-compose fails often, human-pasted invitations bypass the cap and can push real volume past the intended limit. Count every advance that plausibly resulted in a human-sent invite (`sent` + `manual`) toward the cap, or track a separate "human-attempted" counter. *(EXT-4; `cap-undercounts-manual`)*
- **`GET /api/ext/queue` with `job_url` omitted has no reuse path** — `_eligible_contact_ids` is per-job only, and EXT-5 adds an "all jobs" setting. Add an all-jobs variant (single SELECT over contacts filtered by `linkedin_url`/`linkedin_message`/`dm_status not in (sent,manual,skipped)`) and dispatch on presence of `job_url`. *(EXT-0; `queue-all-jobs-no-helper`)*
- **`nativeInputValueSetter` no-ops on contenteditable** — fine for the invite note (`<textarea#custom-message>`), but EXT-6's Message composer is a contenteditable rich editor; "reuse EXT-2's React-safe fill" cannot work there. Branch fill by element type (setter for input/textarea; `execCommand insertText`/synthetic keystrokes for contenteditable). Also have EXT-2's spike explicitly confirm the note field is still a real `<textarea>` (LinkedIn A/B-tests these). *(EXT-6, EXT-2; `react-fill-fails-on-contenteditable`)*
- **Same person across two jobs = two queue rows** sharing one `linkedin_url` (contact_id keys on job_url+url+name). In the all-jobs view this yields a duplicate invite attempt (2nd finds "Pending", falls to paste-manually). Dedupe the queue by normalized `linkedin_url`; when one instance is sent, auto-mark siblings. *(EXT-5; `duplicate-contact-across-jobs`)*
- **Never-break clipboard fallback likely fails silently** — `navigator.clipboard.writeText` from a content script needs transient user activation, which the auto-triggered fallback lacks; the user is told "copied" while the clipboard holds stale data. Use the overlay's existing gesture-backed **Copy note** button; only claim "copied" after `writeText` resolves. *(EXT-4; `clipboard-fallback-no-gesture`)*
- **Sent-detection is inferred, not positive.** "dialog closed + Pending" can false-positive on a pre-existing Pending badge (advances past an unactioned contact, poisons 30-day dedupe) and false-negative on a missed mutation. Add an explicit `unknown`/ambiguous state that does *not* mark `sent`; require a positive toast/aria-live confirmation; treat pre-existing Pending as already-invited → skip, don't compose. *(EXT-3; `sent-detection-false-positive-and-negative`, `pending-false-sent`)*
- **Editing a note after it's already composed onto the page has undefined effect** — the on-page textarea keeps v1 while the popup shows v2. Re-run the React-safe fill on edit of the active/composed contact, or disable editing for it with a clear note. *(EXT-5; `note-edit-vs-composed-timing`)*
- **Permission minimality:** drop `tabs` (navigation via `chrome.tabs.update(tabId,{url})` works with host permissions alone) and reconsider `scripting` (static content scripts need no `executeScript`); narrow the content-script match from `*.linkedin.com/*` to `.../in/*` and gate DOM action behind a "queue active" flag so the script is inert during normal browsing. *(EXT-1; `tabs-permission-overbroad`, `scripting-permission-and-broad-match`)*
- **Overlay XSS / message-trust hygiene:** build the overlay with `textContent`/DOM APIs, never `innerHTML`, for any queue-derived string (company/title/note are attacker-influenceable); accept instructions only over `chrome.runtime` (verify `sender`), ignore `window.postMessage`. State as an EXT-2 non-negotiable. *(EXT-2; `overlay-xss-and-message-trust`)*
- **Overlay-positioning:** "never covers Send" + "arrow pointing at Send" needs an engineered collision-avoidance spec (anchor to a viewport corner, measure Send's live rect, reposition/shrink, `pointer-events:none`), not an asserted property. *(EXT-2; `overlay-covers-send`)*
- **Doc-accuracy nits:** drop "near-invisible / near-zero ban risk" claims (in-page synthetic Connect/Add-a-note clicks are `isTrusted=false` and observable to LinkedIn's own JS; the real win is *reliability* + human-paced volume). Add a real ban-risk validation plan to §9. Weekly-invite-limit pause should message distinctly from the daily cap ("resumes next week") rather than showing a daily countdown. Document the load-unpacked lifecycle (dev-mode startup nag, manual updates) and state per-account ToS/termination as the true ceiling on §9's "zero restrictions" — unlisted Web Store is *not* an easy escape hatch (same automation-policy review). *(PRD §0/§8/§9, EXT-4; `synthetic-fill-not-laundered-by-human-send`, `detectability-claim-overstated`, `weekly-limit-no-resume`, `distribution-loadunpacked-and-tos`, `selector-stability-assumption-optimistic`)*
- **Auto-advance breathing room:** put Pause/Hold on the overlay (H3) and consider defaulting to "confirm to advance" rather than a timer, since the human is already right there. *(EXT-3/EXT-5; `auto-advance-no-breathing-room`)*
- **Selector self-check:** on N consecutive fallbacks-to-manual, surface a loud "LinkedIn layout changed — selectors need updating" state instead of silently pasting-manually forever. *(EXT-4; `selector-stability-assumption-optimistic`)*

---

## 5. Ticket-by-ticket deltas

**EXT-0 — Local API** (most-affected ticket)
- Extend the `linkedin` branch of `_eligible_contact_ids` **and** `dm_ready` to exclude `dm_status in ('sent','manual','skipped')` — state that reuse requires *modifying* the helper (B3).
- Make `mark_dm_sent` stamp `dm_sent_at=now()` on flip to `sent` (or claim first); add a test asserting non-NULL + `already_dmed` finds it (B5).
- `POST /api/ext/note`: use `upsert_contact` with `linkedin_message` only, cap 300 server-side — do **not** reuse the email save path (medium).
- Guard the new **GET** with the Host-loopback check; allow the extension by *scheme* or shared token, not a hardcoded ID; no CORS headers (medium, H4/H5).
- Add an all-jobs eligibility variant for `job_url`-omitted (medium).
- Update the `store.py:42` `dm_status` enum comment to `none|sending|composed|sent|manual|skipped|failed`.

**EXT-1 — Skeleton**
- Add an explicit persisted-state schema in `chrome.storage` (queue, cursor, activeContactId, activeTabId, per-contact status, progress, dailyCount, windowStart, settings); worker holds no durable state, rehydrates per wake; popup renders from storage via `onChanged` (B1).
- Drop `tabs`; reconsider `scripting`; narrow content-script match to `.../in/*` (medium). Add a manifest `key` or adopt the token (H5).

**EXT-2 — Auto-compose**
- Content script pulls its assignment from storage keyed by `tabId` on load (H2).
- Overlay shows `Inviting: {name} — {title} at {company}` + cross-checks on-page profile identity before "ready to Send" (H3).
- Spike explicitly confirms the note field is a `<textarea>`; branch fill by element type (medium, for EXT-6 reuse).
- Non-negotiables: `textContent`-only overlay, `chrome.runtime`-only messaging, engineered Send-collision avoidance (medium).

**EXT-3 — Detect + advance**
- Spec the cold-wake advance path end-to-end + idempotent on `contact_id` (B2).
- Pacing via timestamp-compare-on-wake, not worker `setTimeout`; note the 30s alarms floor (H1).
- Persist + validate `activeTabId` before every navigate; pause if the tab is gone/repurposed (H2).
- Validate `linkedin_url` against the LinkedIn `/in/` pattern before navigating (H4).
- Add an `unknown`/ambiguous sent state; require a positive send signal; skip pre-existing Pending (medium).

**EXT-4 — Resilience + safety**
- Promote the free-note-quota constraint from an edge bullet to first-class detection + queue cap (B4).
- Count `manual` fallbacks toward the daily cap (medium).
- Fix the clipboard fallback to use the gesture-backed Copy button (medium).
- Daily cap as stored `windowStart`+count evaluated on advance; weekly-limit messaging distinct from daily (H1, low).
- Add the "N consecutive fallbacks → layout-changed" self-check (low).

**EXT-5 — Polish**
- Move live progress + Pause/Skip onto the on-page overlay; popup is for setup, not the running loop (H3).
- Re-fill (or lock) the note on edit of the already-composed active contact (medium).
- Dedupe the all-jobs queue view by normalized `linkedin_url` (medium).

**EXT-6 — Message flow** (backlog)
- Rewrite to NOT claim "reuse EXT-2's React-safe fill" — the contenteditable composer needs `execCommand insertText`/synthetic keystrokes, a separate verified insertion path (medium).

---

## 6. What's solid

- **The central pivot is correct and evidence-backed.** Running inside the real session with the native-setter + `input` React-safe fill directly fixes the two things that sank the external agent (snapshot missing React modals, synthetic value-sets silently dropping). The epic's own history confirms the *external* transport — not field-fill provenance — was the detection signal, so the extension removes the real fingerprint.
- **Human-clicks-Send as the safety + correctness gate is the right architecture.** It keeps the one platform-defended action human, gives a natural review point for wrong-recipient/malformed notes (once the overlay actually shows the recipient — H3), and is the lowest-risk ToS posture.
- **The never-break contract and load-bearing-spike sequencing are good engineering discipline.** EXT-2 as a prove-it-first spike before building UI, and "any failure → paste-manually + advance," are exactly the right instincts for a fragile-DOM target.
- **Reusing the existing `contacts` table / `dm_status` and the loopback dashboard server** keeps the surface tiny and avoids a new data model or external servers — the security posture (no remote hosts, no `<all_urls>`) starts from the right default.

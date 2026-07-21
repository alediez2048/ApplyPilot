// ApplyPilot LinkedIn Assistant — background service worker (MV3, STATELESS).
//
// Ownership (CONTRACTS.md §6): EXT-1 storage schema + token persist, EXT-3 cold-wake advance
// (idempotent) + pacing + tab validation + linkedin_url validation + positive sent-detect,
// EXT-4 skip/manual routing + caps (sent+manual) + weekly/note-quota + layout self-check.
//
// The worker owns NO durable state. Every handler: rehydrate (loadState) → mutate → persist.
// The only in-memory value that survives a message is a tiny mutex used to serialize handlers so
// two concurrent messages cannot interleave a read-modify-write and lose an update — it holds no
// run-state and is safe to lose on eviction.
//
// Non-negotiables upheld here:
//   - NEVER clicks Send (no code path targets a Send control).
//   - No worker setTimeout / sub-30s chrome.alarms for pacing — pacing is a timestamp compare on
//     wake (now - lastAdvanceTs); a 30s alarm is only a backstop to re-evaluate a deferred advance.
//   - Every linkedin_url is validated against LINKEDIN_PROFILE_RE before ANY navigation.
//   - Only ever navigates the stored activeTabId (never "the current active tab"); the tab is
//     re-validated (exists + on linkedin.com) before every navigate.
//   - Advance is idempotent on contact_id (a re-fired observer / double SEND_DETECTED is a no-op).
//   - Sender is verified on every message; window.postMessage is never trusted (that's content.js).

import {
  MSG,
  STORAGE_KEYS,
  API,
  DM_STATUS,
  UI_STATE,
  RUN_PHASE,
  COMPOSE_FAIL_REASON,
  DEFAULT_SETTINGS,
  LINKEDIN_PROFILE_RE,
  NOTE_MAX_LEN,
} from "./shared/constants.js";

// ---------------------------------------------------------------------------
// Tunables (worker-local constants, not run-state).
// ---------------------------------------------------------------------------
const DAY_MS = 24 * 60 * 60 * 1000;
const PACING_ALARM = "applypilot_pacing"; // 30s backstop to re-evaluate a deferred advance
const PACING_ALARM_MIN = 0.5;             // chrome.alarms floor is ~30s — noted per H1
const LAYOUT_FALLBACK_HINT = 3;           // Nth consecutive fallback → soft "layout changed" hint

// The transient per-contact UI states that mean "done for this run" (mirror DM_DONE_STATUSES).
const TERMINAL_UI = new Set([UI_STATE.SENT, UI_STATE.MANUAL, UI_STATE.SKIPPED]);

const STORAGE_KEY_LIST = Object.values(STORAGE_KEYS);

// ---------------------------------------------------------------------------
// State I/O — the ONLY durable state lives in chrome.storage.local under STORAGE_KEYS.
// loadState() returns a plain object keyed by the JSON key names (== the STORAGE_KEYS values);
// persist() writes exactly those keys back. No module-level cache of run-state, ever.
// ---------------------------------------------------------------------------
function defaultState() {
  return {
    [STORAGE_KEYS.QUEUE]: [],
    [STORAGE_KEYS.CURSOR]: -1,
    [STORAGE_KEYS.ACTIVE_CONTACT_ID]: null,
    [STORAGE_KEYS.ACTIVE_TAB_ID]: null,
    [STORAGE_KEYS.STATUS_MAP]: {},
    [STORAGE_KEYS.PROGRESS]: { sent: 0, total: 0 },
    [STORAGE_KEYS.DAILY_COUNT]: 0,
    [STORAGE_KEYS.WINDOW_START]: 0,
    [STORAGE_KEYS.LAST_ADVANCE_TS]: 0,
    [STORAGE_KEYS.SETTINGS]: { ...DEFAULT_SETTINGS },
    [STORAGE_KEYS.RUNNING]: false,
    [STORAGE_KEYS.PAUSED]: false,
    [STORAGE_KEYS.PHASE]: RUN_PHASE.IDLE,
    [STORAGE_KEYS.CONSECUTIVE_FALLBACKS]: 0,
    [STORAGE_KEYS.SERVER_ONLINE]: false,
    [STORAGE_KEYS.TOKEN]: "",
    [STORAGE_KEYS.LAST_ERROR]: "",
  };
}

async function loadState() {
  const raw = await chrome.storage.local.get(STORAGE_KEY_LIST);
  const d = defaultState();
  const s = {};
  for (const k of STORAGE_KEY_LIST) s[k] = raw[k] !== undefined ? raw[k] : d[k];
  // settings may be a partial from an older version — backfill missing fields.
  s[STORAGE_KEYS.SETTINGS] = { ...DEFAULT_SETTINGS, ...(s[STORAGE_KEYS.SETTINGS] || {}) };
  return s;
}

async function persist(state) {
  const out = {};
  for (const k of STORAGE_KEY_LIST) out[k] = state[k];
  await chrome.storage.local.set(out);
}

// ---------------------------------------------------------------------------
// Mutex — serialize handlers so rehydrate→mutate→persist is atomic w.r.t. other messages.
// This is NOT run-state; it is a scheduling primitive and may be lost on eviction harmlessly.
// ---------------------------------------------------------------------------
let _chain = Promise.resolve();
function withLock(fn) {
  const run = _chain.then(fn, fn);
  _chain = run.then(
    () => {},
    () => {},
  );
  return run;
}

// ---------------------------------------------------------------------------
// Small pure helpers.
// ---------------------------------------------------------------------------
function isValidLinkedIn(url) {
  return typeof url === "string" && LINKEDIN_PROFILE_RE.test(url);
}

function isLinkedInTab(tab) {
  // tab.url is only readable for hosts we hold permission for; a repurposed (non-linkedin) tab
  // yields an empty/undefined url under host-permission scoping — which we correctly reject.
  return !!(tab && tab.url && /^https:\/\/([a-z]+\.)?linkedin\.com\//.test(tab.url));
}

function normUrl(u) {
  if (!u || typeof u !== "string") return "";
  let s = u.trim().toLowerCase();
  s = s.replace(/^https?:\/\//, "").replace(/^www\./, "");
  s = s.split("?")[0].split("#")[0];
  s = s.replace(/\/+$/, "");
  return s;
}

function dedupeContacts(contacts) {
  const seen = new Set();
  const out = [];
  for (const c of contacts) {
    if (!c || !c.id) continue;
    const k = normUrl(c.linkedin_url);
    if (k && seen.has(k)) continue;
    if (k) seen.add(k);
    out.push(c);
  }
  return out;
}

function nextReadyIndex(queue, statusMap, from) {
  for (let i = Math.max(0, from); i < queue.length; i++) {
    const id = queue[i] && queue[i].id;
    if (id && !TERMINAL_UI.has(statusMap[id])) return i;
  }
  return -1;
}

function countSent(statusMap) {
  let n = 0;
  for (const k in statusMap) {
    if (statusMap[k] === UI_STATE.SENT || statusMap[k] === UI_STATE.MANUAL) n++;
  }
  return n;
}

function isTerminal(uiStatus) {
  return TERMINAL_UI.has(uiStatus);
}

function rollWindow(state, now) {
  if (!state[STORAGE_KEYS.WINDOW_START]) {
    state[STORAGE_KEYS.WINDOW_START] = now;
  } else if (now - state[STORAGE_KEYS.WINDOW_START] >= DAY_MS) {
    state[STORAGE_KEYS.WINDOW_START] = now;
    state[STORAGE_KEYS.DAILY_COUNT] = 0;
  }
}

function capMessage(state) {
  const cap = state[STORAGE_KEYS.SETTINGS].dailyCap;
  const count = state[STORAGE_KEYS.DAILY_COUNT];
  const resetAt = (state[STORAGE_KEYS.WINDOW_START] || Date.now()) + DAY_MS;
  const ms = Math.max(0, resetAt - Date.now());
  const h = Math.floor(ms / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  return `Daily cap reached (${count}/${cap} invites). Resets in ${h}h ${m}m.`;
}

function scheduleAlarm() {
  // Backstop only. Never a sub-30s timer for the actual pacing delay — pacing is a timestamp
  // compare done on wake; this alarm just guarantees a wake to re-evaluate the deferred advance.
  chrome.alarms.create(PACING_ALARM, { delayInMinutes: PACING_ALARM_MIN });
}

// ---------------------------------------------------------------------------
// Local API (localhost:8765, mutual shared token). No CORS; host-permission bypass.
// ---------------------------------------------------------------------------
function authHeaders(token, withBody) {
  const h = { [API.TOKEN_HEADER]: token || "" };
  if (withBody) h["Content-Type"] = "application/json";
  return h;
}

async function fetchQueue(token, jobFilter) {
  const q = jobFilter ? "?job_url=" + encodeURIComponent(jobFilter) : "";
  try {
    const res = await fetch(API.BASE_URL + API.QUEUE + q, { method: "GET", headers: authHeaders(token, false) });
    if (!res.ok) return { ok: false, status: res.status, error: "HTTP " + res.status };
    const data = await res.json();
    if (!data || data.ok === false) return { ok: false, error: (data && data.error) || "bad response" };
    return { ok: true, contacts: Array.isArray(data.contacts) ? data.contacts : [] };
  } catch (e) {
    return { ok: false, network: true, error: String(e && e.message ? e.message : e) };
  }
}

async function postStatus(token, contactId, status) {
  try {
    const res = await fetch(API.BASE_URL + API.STATUS, {
      method: "POST",
      headers: authHeaders(token, true),
      body: JSON.stringify({ contact_id: contactId, status }),
    });
    if (!res.ok) return { ok: false, status: res.status, error: "HTTP " + res.status };
    const d = await res.json().catch(() => ({ ok: true }));
    return { ok: d.ok !== false, error: d && d.error };
  } catch (e) {
    return { ok: false, network: true, error: String(e && e.message ? e.message : e) };
  }
}

async function postNote(token, contactId, note) {
  try {
    const res = await fetch(API.BASE_URL + API.NOTE, {
      method: "POST",
      headers: authHeaders(token, true),
      body: JSON.stringify({ contact_id: contactId, note: (note || "").slice(0, NOTE_MAX_LEN) }),
    });
    if (!res.ok) return { ok: false, status: res.status, error: "HTTP " + res.status };
    const d = await res.json();
    if (!d || d.ok === false) return { ok: false, error: (d && d.error) || "note rejected" };
    return { ok: true, note: typeof d.note === "string" ? d.note : (note || "").slice(0, NOTE_MAX_LEN) };
  } catch (e) {
    return { ok: false, network: true, error: String(e && e.message ? e.message : e) };
  }
}

// Fire-and-forget status POST (used for URL-invalid auto-skips where we can't block advance).
function fireStatus(token, contactId, status) {
  postStatus(token, contactId, status).catch(() => {});
}

// ---------------------------------------------------------------------------
// Tab ownership — only ever the stored activeTabId; validated before every navigate (H2).
// ---------------------------------------------------------------------------
async function getActiveTab(state) {
  const id = state[STORAGE_KEYS.ACTIVE_TAB_ID];
  if (id == null) return null;
  try {
    return await chrome.tabs.get(id);
  } catch {
    return null;
  }
}

function pauseForMissingTab(state, why) {
  state[STORAGE_KEYS.ACTIVE_TAB_ID] = null;
  state[STORAGE_KEYS.PAUSED] = true;
  state[STORAGE_KEYS.PHASE] = RUN_PHASE.PAUSED;
  state[STORAGE_KEYS.LAST_ERROR] =
    why || "The ApplyPilot LinkedIn tab was closed or navigated away. Resume to reopen it.";
}

// Open (or reuse) the dedicated tab and point it at a contact. Used by Start.
async function ensureTabAndNavigate(state, contact) {
  const existing = await getActiveTab(state);
  if (existing && isLinkedInTab(existing)) {
    try {
      await chrome.tabs.update(state[STORAGE_KEYS.ACTIVE_TAB_ID], { url: contact.linkedin_url, active: true });
      return true;
    } catch {
      /* fall through to create */
    }
  }
  const created = await chrome.tabs.create({ url: contact.linkedin_url, active: true });
  state[STORAGE_KEYS.ACTIVE_TAB_ID] = created.id;
  return true;
}

// Navigate the (already-owned) active tab to a contact, validating existence + host first.
// Returns true on navigate; false means we paused (tab gone/repurposed) — caller should stop.
async function navigateActiveTab(state, contact) {
  const tab = await getActiveTab(state);
  if (!tab || !isLinkedInTab(tab)) {
    pauseForMissingTab(state);
    return false;
  }
  try {
    await chrome.tabs.update(state[STORAGE_KEYS.ACTIVE_TAB_ID], { url: contact.linkedin_url, active: true });
    return true;
  } catch {
    pauseForMissingTab(state);
    return false;
  }
}

// Re-arm a LIVE overlay in the active tab (used for resume / note edit). The content script is a
// live page, not the possibly-dead SW counterpart, so a direct sendMessage is fine here. If the
// overlay isn't listening, fall back to re-navigating the same profile so it re-pulls on load.
async function rearmActiveTab(state) {
  const cursor = state[STORAGE_KEYS.CURSOR];
  const contact = state[STORAGE_KEYS.QUEUE][cursor] || null;
  if (state[STORAGE_KEYS.ACTIVE_TAB_ID] == null || !contact) return;
  const payload = {
    type: MSG.ASSIGNMENT,
    contact,
    settings: state[STORAGE_KEYS.SETTINGS],
    running: state[STORAGE_KEYS.RUNNING],
    phase: state[STORAGE_KEYS.PHASE],
  };
  try {
    await chrome.tabs.sendMessage(state[STORAGE_KEYS.ACTIVE_TAB_ID], payload);
  } catch {
    // The content script isn't reachable — almost always because the extension was reloaded
    // (which orphans content scripts in already-open tabs). Get a fresh one running.
    const tab = await getActiveTab(state);
    if (!tab || !isLinkedInTab(tab)) return;
    try {
      const onContactUrl = normUrl(tab.url) === normUrl(contact.linkedin_url);
      if (onContactUrl) {
        // Same URL — chrome.tabs.update({url}) would be a no-op, so RELOAD to re-inject
        // the (now fresh) content script, which then pulls its assignment and composes/skips.
        await chrome.tabs.reload(state[STORAGE_KEYS.ACTIVE_TAB_ID]);
      } else if (isValidLinkedIn(contact.linkedin_url)) {
        await chrome.tabs.update(state[STORAGE_KEYS.ACTIVE_TAB_ID], { url: contact.linkedin_url });
      } else {
        await chrome.tabs.reload(state[STORAGE_KEYS.ACTIVE_TAB_ID]);
      }
    } catch {
      /* ignore — user can Resume again */
    }
  }
}

// ---------------------------------------------------------------------------
// The advance state machine — idempotent, pacing-aware, cap-aware. Persists before returning.
// Precondition: the just-finished contact (if any) is already recorded terminal by the caller.
// ---------------------------------------------------------------------------
async function advanceToNext(state, { bypassPacing = false } = {}) {
  if (!state[STORAGE_KEYS.RUNNING] || state[STORAGE_KEYS.PAUSED]) {
    await persist(state);
    return;
  }

  const now = Date.now();
  rollWindow(state, now);

  // Daily cap — counts sent + manual (both increment dailyCount). Blocks advancing past the cap.
  const cap = state[STORAGE_KEYS.SETTINGS].dailyCap;
  if (state[STORAGE_KEYS.DAILY_COUNT] >= cap) {
    state[STORAGE_KEYS.PAUSED] = true;
    state[STORAGE_KEYS.PHASE] = RUN_PHASE.PAUSED;
    state[STORAGE_KEYS.LAST_ERROR] = capMessage(state);
    await persist(state);
    return;
  }

  // Pacing — timestamp compare on wake; never a worker setTimeout. Defer + set a 30s backstop.
  if (!bypassPacing) {
    const pacingMs = (state[STORAGE_KEYS.SETTINGS].pacingSeconds || 0) * 1000;
    if (pacingMs > 0 && now - state[STORAGE_KEYS.LAST_ADVANCE_TS] < pacingMs) {
      state[STORAGE_KEYS.PHASE] = RUN_PHASE.PACING;
      await persist(state);
      scheduleAlarm();
      return;
    }
  }

  // Pick the next ready contact, auto-skipping any with an invalid/poisoned linkedin_url (H4).
  const queue = state[STORAGE_KEYS.QUEUE];
  const statusMap = state[STORAGE_KEYS.STATUS_MAP];
  let idx = nextReadyIndex(queue, statusMap, state[STORAGE_KEYS.CURSOR] + 1);
  while (idx !== -1 && !isValidLinkedIn(queue[idx].linkedin_url)) {
    statusMap[queue[idx].id] = UI_STATE.SKIPPED;
    fireStatus(state[STORAGE_KEYS.TOKEN], queue[idx].id, DM_STATUS.SKIPPED);
    idx = nextReadyIndex(queue, statusMap, idx + 1);
  }

  if (idx === -1) {
    state[STORAGE_KEYS.CURSOR] = queue.length;
    state[STORAGE_KEYS.ACTIVE_CONTACT_ID] = null;
    state[STORAGE_KEYS.PHASE] = RUN_PHASE.DONE;
    state[STORAGE_KEYS.LAST_ERROR] = "";
    await persist(state);
    return;
  }

  const contact = queue[idx];
  state[STORAGE_KEYS.CURSOR] = idx;
  state[STORAGE_KEYS.ACTIVE_CONTACT_ID] = contact.id;
  state[STORAGE_KEYS.STATUS_MAP][contact.id] = UI_STATE.READY;
  state[STORAGE_KEYS.PHASE] = RUN_PHASE.NAVIGATING;
  state[STORAGE_KEYS.LAST_ADVANCE_TS] = now;
  state[STORAGE_KEYS.LAST_ERROR] = "";
  // Persist the advanced cursor/activeContactId BEFORE navigating so the content script's
  // GET_ASSIGNMENT (fired on load) reads the correct, already-committed assignment.
  await persist(state);

  const ok = await navigateActiveTab(state, contact);
  if (!ok) await persist(state); // navigate paused us (tab gone) — persist the paused state.
}

// Record a completed contact + advance. Idempotent on contactId. Used for sent & manual.
async function completeAndAdvance(state, contactId, uiStatus, dmStatus) {
  const posted = await postStatus(state[STORAGE_KEYS.TOKEN], contactId, dmStatus);
  if (!posted.ok) {
    // Contract: do NOT advance on a failed status POST — keep the queue safe.
    state[STORAGE_KEYS.SERVER_ONLINE] = false;
    state[STORAGE_KEYS.LAST_ERROR] =
      "Couldn't record " + dmStatus + " to ApplyPilot (server offline?). Not advancing.";
    await persist(state);
    return false;
  }
  state[STORAGE_KEYS.SERVER_ONLINE] = true;
  rollWindow(state, Date.now());
  state[STORAGE_KEYS.STATUS_MAP][contactId] = uiStatus;
  const prog = state[STORAGE_KEYS.PROGRESS] || { sent: 0, total: 0 };
  state[STORAGE_KEYS.PROGRESS] = { sent: (prog.sent || 0) + 1, total: prog.total || 0 };
  state[STORAGE_KEYS.DAILY_COUNT] = (state[STORAGE_KEYS.DAILY_COUNT] || 0) + 1;
  await advanceToNext(state, {});
  return true;
}

// Skip a contact (no invite sent → does not count toward the cap). Advances if it was active.
async function skipContact(state, contactId, reason) {
  if (isTerminal(state[STORAGE_KEYS.STATUS_MAP][contactId])) {
    await persist(state);
    return;
  }
  const posted = await postStatus(state[STORAGE_KEYS.TOKEN], contactId, DM_STATUS.SKIPPED);
  if (!posted.ok) {
    state[STORAGE_KEYS.SERVER_ONLINE] = false;
    state[STORAGE_KEYS.LAST_ERROR] = "Couldn't record skip to ApplyPilot (server offline?).";
    await persist(state);
    return;
  }
  state[STORAGE_KEYS.SERVER_ONLINE] = true;
  state[STORAGE_KEYS.STATUS_MAP][contactId] = UI_STATE.SKIPPED;
  if (reason) state[STORAGE_KEYS.LAST_ERROR] = "";
  if (contactId === state[STORAGE_KEYS.ACTIVE_CONTACT_ID]) {
    await advanceToNext(state, {});
  } else {
    await persist(state); // skipping a non-active queued contact (popup) — no navigation.
  }
}

// ---------------------------------------------------------------------------
// Start / refresh queue.
// ---------------------------------------------------------------------------
async function startQueue(state, jobFilterArg) {
  const filter = jobFilterArg !== undefined ? jobFilterArg : state[STORAGE_KEYS.SETTINGS].jobFilter;
  const resp = await fetchQueue(state[STORAGE_KEYS.TOKEN], filter);
  if (!resp.ok) {
    state[STORAGE_KEYS.SERVER_ONLINE] = false;
    state[STORAGE_KEYS.RUNNING] = false;
    state[STORAGE_KEYS.PHASE] = RUN_PHASE.IDLE;
    state[STORAGE_KEYS.LAST_ERROR] =
      resp.status === 401
        ? "ApplyPilot rejected the token — paste the token shown in the dashboard."
        : "ApplyPilot not connected (is the dashboard running on localhost:8765?).";
    await persist(state);
    return { ok: false, error: state[STORAGE_KEYS.LAST_ERROR] };
  }

  state[STORAGE_KEYS.SERVER_ONLINE] = true;
  const contacts = dedupeContacts(resp.contacts);
  state[STORAGE_KEYS.QUEUE] = contacts;
  state[STORAGE_KEYS.STATUS_MAP] = {};
  state[STORAGE_KEYS.PROGRESS] = { sent: 0, total: contacts.length };
  state[STORAGE_KEYS.CURSOR] = -1;
  state[STORAGE_KEYS.ACTIVE_CONTACT_ID] = null;
  state[STORAGE_KEYS.RUNNING] = true;
  state[STORAGE_KEYS.PAUSED] = false;
  state[STORAGE_KEYS.CONSECUTIVE_FALLBACKS] = 0;
  state[STORAGE_KEYS.LAST_ERROR] = "";
  state[STORAGE_KEYS.SETTINGS] = { ...state[STORAGE_KEYS.SETTINGS], jobFilter: filter };

  const now = Date.now();
  rollWindow(state, now); // dailyCount/windowStart persist across sessions; only roll if stale.

  if (contacts.length === 0) {
    state[STORAGE_KEYS.RUNNING] = false;
    state[STORAGE_KEYS.PHASE] = RUN_PHASE.IDLE;
    state[STORAGE_KEYS.LAST_ERROR] = "No eligible LinkedIn contacts in the queue.";
    await persist(state);
    return { ok: true, empty: true };
  }

  if (state[STORAGE_KEYS.DAILY_COUNT] >= state[STORAGE_KEYS.SETTINGS].dailyCap) {
    state[STORAGE_KEYS.PAUSED] = true;
    state[STORAGE_KEYS.PHASE] = RUN_PHASE.PAUSED;
    state[STORAGE_KEYS.LAST_ERROR] = capMessage(state);
    await persist(state);
    return { ok: true, capped: true };
  }

  // First ready + valid contact (auto-skip invalid URLs).
  let idx = nextReadyIndex(contacts, state[STORAGE_KEYS.STATUS_MAP], 0);
  while (idx !== -1 && !isValidLinkedIn(contacts[idx].linkedin_url)) {
    state[STORAGE_KEYS.STATUS_MAP][contacts[idx].id] = UI_STATE.SKIPPED;
    fireStatus(state[STORAGE_KEYS.TOKEN], contacts[idx].id, DM_STATUS.SKIPPED);
    idx = nextReadyIndex(contacts, state[STORAGE_KEYS.STATUS_MAP], idx + 1);
  }
  if (idx === -1) {
    state[STORAGE_KEYS.RUNNING] = false;
    state[STORAGE_KEYS.PHASE] = RUN_PHASE.IDLE;
    state[STORAGE_KEYS.LAST_ERROR] = "No valid LinkedIn profile URLs in the queue.";
    await persist(state);
    return { ok: true, empty: true };
  }

  const contact = contacts[idx];
  state[STORAGE_KEYS.CURSOR] = idx;
  state[STORAGE_KEYS.ACTIVE_CONTACT_ID] = contact.id;
  state[STORAGE_KEYS.STATUS_MAP][contact.id] = UI_STATE.READY;
  state[STORAGE_KEYS.PHASE] = RUN_PHASE.NAVIGATING;
  state[STORAGE_KEYS.LAST_ADVANCE_TS] = now;
  await ensureTabAndNavigate(state, contact);
  await persist(state);
  return { ok: true };
}

async function refreshQueue(state) {
  const resp = await fetchQueue(state[STORAGE_KEYS.TOKEN], state[STORAGE_KEYS.SETTINGS].jobFilter);
  if (!resp.ok) {
    state[STORAGE_KEYS.SERVER_ONLINE] = false;
    state[STORAGE_KEYS.LAST_ERROR] =
      resp.status === 401 ? "Token rejected by ApplyPilot." : "ApplyPilot not reachable.";
    await persist(state);
    return { ok: false, error: state[STORAGE_KEYS.LAST_ERROR] };
  }
  state[STORAGE_KEYS.SERVER_ONLINE] = true;
  const fresh = dedupeContacts(resp.contacts);
  const activeId = state[STORAGE_KEYS.ACTIVE_CONTACT_ID];

  // Preserve per-contact status only for contacts that are still present.
  const oldStatus = state[STORAGE_KEYS.STATUS_MAP] || {};
  const newStatus = {};
  for (const c of fresh) if (oldStatus[c.id]) newStatus[c.id] = oldStatus[c.id];

  state[STORAGE_KEYS.QUEUE] = fresh;
  state[STORAGE_KEYS.STATUS_MAP] = newStatus;
  state[STORAGE_KEYS.PROGRESS] = { sent: countSent(newStatus), total: fresh.length };

  if (activeId) {
    const i = fresh.findIndex((c) => c.id === activeId);
    state[STORAGE_KEYS.CURSOR] = i;
    if (i < 0) state[STORAGE_KEYS.ACTIVE_CONTACT_ID] = null;
  }
  await persist(state);
  return { ok: true };
}

// ---------------------------------------------------------------------------
// Message routing. Every handler rehydrates, mutates, persists. Sender is verified.
// ---------------------------------------------------------------------------
function classifySender(sender) {
  const fromExt = !!(sender && sender.id === chrome.runtime.id);
  const tab = sender && sender.tab;
  const url = (sender && sender.url) || (tab && tab.url) || "";
  const fromContent = fromExt && tab && typeof tab.id === "number" && /^https:\/\/([a-z]+\.)?linkedin\.com\//.test(url);
  const fromPopup = fromExt && !tab; // extension page (popup) — no tab on the sender
  return { fromExt, fromContent, fromPopup, tab };
}

async function handleMessage(msg, sender) {
  if (!msg || typeof msg.type !== "string") return { ok: false, error: "malformed message" };
  const who = classifySender(msg && sender ? sender : {});

  // --- content/overlay → background (require a verified LinkedIn content sender) ------------
  const CONTENT_TYPES = new Set([
    MSG.GET_ASSIGNMENT,
    MSG.COMPOSE_RESULT,
    MSG.SEND_DETECTED,
    MSG.FALLBACK_MANUAL,
    MSG.SKIP_CONTACT,
    MSG.IDENTITY_MISMATCH,
    MSG.LAYOUT_CHANGED,
    MSG.LIMIT_BANNER,
    MSG.OVERLAY_PAUSE,
    MSG.OVERLAY_RESUME,
    MSG.OVERLAY_SKIP,
  ]);
  const POPUP_TYPES = new Set([
    MSG.GET_STATE,
    MSG.START_QUEUE,
    MSG.PAUSE_QUEUE,
    MSG.RESUME_QUEUE,
    MSG.NEXT,
    MSG.SKIP,
    MSG.REFRESH_QUEUE,
    MSG.UPDATE_SETTINGS,
    MSG.SAVE_NOTE,
    MSG.SET_TOKEN,
  ]);

  if (CONTENT_TYPES.has(msg.type) && !who.fromContent) {
    return msg.type === MSG.GET_ASSIGNMENT ? { type: MSG.ASSIGNMENT, contact: null } : { ok: false, error: "unauthorized" };
  }
  if (POPUP_TYPES.has(msg.type) && !who.fromPopup) {
    return { ok: false, error: "unauthorized" };
  }

  const senderTabId = who.tab ? who.tab.id : null;
  const state = await loadState();

  switch (msg.type) {
    // ----- assignment PULL by tabId (authority = sender.tab.id) -----------------------------
    case MSG.GET_ASSIGNMENT: {
      const active =
        senderTabId === state[STORAGE_KEYS.ACTIVE_TAB_ID] &&
        state[STORAGE_KEYS.RUNNING] &&
        !state[STORAGE_KEYS.PAUSED] &&
        !!state[STORAGE_KEYS.ACTIVE_CONTACT_ID];
      let contact = null;
      if (active) {
        const c = state[STORAGE_KEYS.QUEUE][state[STORAGE_KEYS.CURSOR]] || null;
        if (c && c.id === state[STORAGE_KEYS.ACTIVE_CONTACT_ID]) contact = c;
      }
      if (contact && state[STORAGE_KEYS.PHASE] === RUN_PHASE.NAVIGATING) {
        // Page just loaded → move to composing so the popup/overlay reflect it.
        state[STORAGE_KEYS.PHASE] = RUN_PHASE.COMPOSING;
        state[STORAGE_KEYS.STATUS_MAP][contact.id] = UI_STATE.COMPOSING;
        await persist(state);
      }
      return {
        type: MSG.ASSIGNMENT,
        contact,
        settings: state[STORAGE_KEYS.SETTINGS],
        running: state[STORAGE_KEYS.RUNNING],
        phase: state[STORAGE_KEYS.PHASE],
      };
    }

    // ----- compose outcome ------------------------------------------------------------------
    case MSG.COMPOSE_RESULT: {
      if (senderTabId !== state[STORAGE_KEYS.ACTIVE_TAB_ID]) return { ok: true }; // stale tab
      const cid = msg.contactId;
      if (cid !== state[STORAGE_KEYS.ACTIVE_CONTACT_ID]) return { ok: true }; // stale contact
      if (isTerminal(state[STORAGE_KEYS.STATUS_MAP][cid])) return { ok: true }; // already done

      if (msg.ok) {
        state[STORAGE_KEYS.STATUS_MAP][cid] = UI_STATE.COMPOSED;
        state[STORAGE_KEYS.PHASE] = RUN_PHASE.READY_TO_SEND;
        state[STORAGE_KEYS.CONSECUTIVE_FALLBACKS] = 0;
        state[STORAGE_KEYS.LAST_ERROR] = "";
        await persist(state);
        return { ok: true };
      }

      // Failure routing (COMPOSE_FAIL_REASON). Skip-worthy states resolve here; genuine
      // auto-compose failures leave an `error` chip and wait for the content script's
      // gesture-backed FALLBACK_MANUAL (clipboard needs a user gesture — H/EXT-4).
      const reason = msg.reason || COMPOSE_FAIL_REASON.UNKNOWN;
      const SKIP_REASONS = new Set([
        COMPOSE_FAIL_REASON.PENDING_ALREADY,
        COMPOSE_FAIL_REASON.ALREADY_CONNECTED,
        COMPOSE_FAIL_REASON.INMAIL_ONLY,
        COMPOSE_FAIL_REASON.PROFILE_404,
      ]);
      const PAUSE_REASONS = new Set([
        COMPOSE_FAIL_REASON.WEEKLY_LIMIT,
        COMPOSE_FAIL_REASON.NOTE_QUOTA_REACHED,
      ]);
      if (SKIP_REASONS.has(reason)) {
        await skipContact(state, cid, reason);
        return { ok: true };
      }
      if (PAUSE_REASONS.has(reason)) {
        state[STORAGE_KEYS.PAUSED] = true;
        state[STORAGE_KEYS.PHASE] = RUN_PHASE.PAUSED;
        state[STORAGE_KEYS.LAST_ERROR] =
          reason === COMPOSE_FAIL_REASON.WEEKLY_LIMIT
            ? "LinkedIn weekly invitation limit reached — paused. Resumes next week."
            : "LinkedIn personalized-note quota reached for this account — paused.";
        await persist(state);
        return { ok: true };
      }
      if (reason === COMPOSE_FAIL_REASON.IDENTITY_MISMATCH) {
        state[STORAGE_KEYS.PAUSED] = true;
        state[STORAGE_KEYS.PHASE] = RUN_PHASE.PAUSED;
        state[STORAGE_KEYS.STATUS_MAP][cid] = UI_STATE.ERROR;
        state[STORAGE_KEYS.LAST_ERROR] = "Identity mismatch — paused to protect against a wrong recipient.";
        await persist(state);
        return { ok: true };
      }
      // no_connect_button / note_field_not_found / fill_verify_failed / unknown → await fallback.
      state[STORAGE_KEYS.STATUS_MAP][cid] = UI_STATE.ERROR;
      state[STORAGE_KEYS.LAST_ERROR] = "Auto-compose failed (" + reason + ") — use Copy note to paste manually.";
      await persist(state);
      return { ok: true };
    }

    // ----- human clicked Send, positively detected (idempotent) ------------------------------
    case MSG.SEND_DETECTED: {
      if (senderTabId !== state[STORAGE_KEYS.ACTIVE_TAB_ID]) return { ok: true };
      const cid = msg.contactId;
      if (cid !== state[STORAGE_KEYS.ACTIVE_CONTACT_ID]) return { ok: true }; // already advanced
      if (isTerminal(state[STORAGE_KEYS.STATUS_MAP][cid])) return { ok: true }; // double signal
      state[STORAGE_KEYS.CONSECUTIVE_FALLBACKS] = 0;
      await completeAndAdvance(state, cid, UI_STATE.SENT, DM_STATUS.SENT);
      return { ok: true };
    }

    // ----- never-break fallback: gesture-backed Copy note used → mark manual ----------------
    case MSG.FALLBACK_MANUAL: {
      if (senderTabId !== state[STORAGE_KEYS.ACTIVE_TAB_ID]) return { ok: true };
      const cid = msg.contactId;
      if (cid !== state[STORAGE_KEYS.ACTIVE_CONTACT_ID]) return { ok: true };
      if (isTerminal(state[STORAGE_KEYS.STATUS_MAP][cid])) return { ok: true };
      state[STORAGE_KEYS.CONSECUTIVE_FALLBACKS] = (state[STORAGE_KEYS.CONSECUTIVE_FALLBACKS] || 0) + 1;
      if (state[STORAGE_KEYS.CONSECUTIVE_FALLBACKS] >= LAYOUT_FALLBACK_HINT) {
        state[STORAGE_KEYS.LAST_ERROR] =
          "Heads up: " +
          state[STORAGE_KEYS.CONSECUTIVE_FALLBACKS] +
          " auto-composes fell back to manual in a row — LinkedIn's layout may have changed.";
      }
      // manual is a real invite → counts toward the daily cap (completeAndAdvance increments it).
      await completeAndAdvance(state, cid, UI_STATE.MANUAL, DM_STATUS.MANUAL);
      return { ok: true };
    }

    // ----- explicit skip from content --------------------------------------------------------
    case MSG.SKIP_CONTACT: {
      if (senderTabId !== state[STORAGE_KEYS.ACTIVE_TAB_ID]) return { ok: true };
      const cid = msg.contactId;
      if (cid !== state[STORAGE_KEYS.ACTIVE_CONTACT_ID]) return { ok: true };
      await skipContact(state, cid, msg.reason);
      return { ok: true };
    }

    // ----- identity cross-check failed → pause (never show ready-to-Send) -------------------
    case MSG.IDENTITY_MISMATCH: {
      if (senderTabId !== state[STORAGE_KEYS.ACTIVE_TAB_ID]) return { ok: true };
      const cid = msg.contactId;
      if (cid !== state[STORAGE_KEYS.ACTIVE_CONTACT_ID]) return { ok: true };
      const intended = (state[STORAGE_KEYS.QUEUE][state[STORAGE_KEYS.CURSOR]] || {}).full_name || "the intended contact";
      state[STORAGE_KEYS.PAUSED] = true;
      state[STORAGE_KEYS.PHASE] = RUN_PHASE.PAUSED;
      state[STORAGE_KEYS.STATUS_MAP][cid] = UI_STATE.ERROR;
      state[STORAGE_KEYS.LAST_ERROR] =
        'Identity mismatch: the profile page shows "' +
        (msg.onPageName || "?") +
        '" but the queue expects ' +
        intended +
        ". Paused to protect against a wrong recipient.";
      await persist(state);
      return { ok: true };
    }

    // ----- selectors likely stale ------------------------------------------------------------
    case MSG.LAYOUT_CHANGED: {
      if (senderTabId !== state[STORAGE_KEYS.ACTIVE_TAB_ID]) return { ok: true };
      state[STORAGE_KEYS.PAUSED] = true;
      state[STORAGE_KEYS.PHASE] = RUN_PHASE.PAUSED;
      state[STORAGE_KEYS.LAST_ERROR] =
        "LinkedIn layout appears to have changed — auto-compose failed " +
        (msg.consecutive || "several") +
        " times in a row. Update selectors.json before resuming.";
      await persist(state);
      return { ok: true };
    }

    // ----- weekly-invite / note-quota banner → distinct pause -------------------------------
    case MSG.LIMIT_BANNER: {
      if (senderTabId !== state[STORAGE_KEYS.ACTIVE_TAB_ID]) return { ok: true };
      state[STORAGE_KEYS.PAUSED] = true;
      state[STORAGE_KEYS.PHASE] = RUN_PHASE.PAUSED;
      state[STORAGE_KEYS.LAST_ERROR] =
        msg.kind === "weekly_invite"
          ? "LinkedIn weekly invitation limit reached — paused. Resumes next week (not a daily cap)."
          : "LinkedIn personalized-note quota reached for this account — paused.";
      await persist(state);
      return { ok: true };
    }

    // ----- overlay controls (popup is closed during Send) -----------------------------------
    case MSG.OVERLAY_PAUSE:
    case MSG.PAUSE_QUEUE: {
      state[STORAGE_KEYS.PAUSED] = true;
      state[STORAGE_KEYS.PHASE] = RUN_PHASE.PAUSED;
      await persist(state);
      return { ok: true };
    }

    case MSG.OVERLAY_RESUME:
    case MSG.RESUME_QUEUE: {
      state[STORAGE_KEYS.PAUSED] = false;
      if (state[STORAGE_KEYS.PHASE] === RUN_PHASE.PAUSED) {
        state[STORAGE_KEYS.PHASE] = state[STORAGE_KEYS.ACTIVE_CONTACT_ID] ? RUN_PHASE.COMPOSING : RUN_PHASE.IDLE;
      }
      state[STORAGE_KEYS.LAST_ERROR] = "";
      await persist(state);
      await rearmActiveTab(state); // re-arm the live overlay for the active contact
      return { ok: true };
    }

    case MSG.OVERLAY_SKIP: {
      if (senderTabId !== state[STORAGE_KEYS.ACTIVE_TAB_ID]) return { ok: true };
      await skipContact(state, msg.contactId, "user_skip");
      return { ok: true };
    }

    // ----- popup ↔ background ----------------------------------------------------------------
    case MSG.GET_STATE: {
      const out = {};
      for (const k of STORAGE_KEY_LIST) out[k] = state[k];
      return out;
    }

    case MSG.START_QUEUE:
      return await startQueue(state, msg.jobFilter);

    case MSG.NEXT: {
      if (!state[STORAGE_KEYS.RUNNING]) return { ok: false, error: "queue not running" };
      state[STORAGE_KEYS.PAUSED] = false;
      // Manual advance — bypass pacing (this IS the user's gesture). Current contact stays
      // non-terminal (it can re-surface on a later refresh); we just move past it.
      await advanceToNext(state, { bypassPacing: true });
      return { ok: true };
    }

    case MSG.SKIP: {
      await skipContact(state, msg.contactId, "user_skip");
      return { ok: true };
    }

    case MSG.REFRESH_QUEUE:
      return await refreshQueue(state);

    case MSG.UPDATE_SETTINGS: {
      state[STORAGE_KEYS.SETTINGS] = { ...state[STORAGE_KEYS.SETTINGS], ...(msg.settings || {}) };
      await persist(state);
      return { ok: true, settings: state[STORAGE_KEYS.SETTINGS] };
    }

    case MSG.SAVE_NOTE: {
      const resp = await postNote(state[STORAGE_KEYS.TOKEN], msg.contactId, msg.note);
      if (!resp.ok) {
        state[STORAGE_KEYS.LAST_ERROR] = "Couldn't save note: " + (resp.error || "unknown");
        await persist(state);
        return { ok: false, error: resp.error };
      }
      const i = state[STORAGE_KEYS.QUEUE].findIndex((c) => c.id === msg.contactId);
      if (i >= 0) state[STORAGE_KEYS.QUEUE][i] = { ...state[STORAGE_KEYS.QUEUE][i], note: resp.note };
      await persist(state);
      if (msg.contactId === state[STORAGE_KEYS.ACTIVE_CONTACT_ID]) await rearmActiveTab(state);
      return { ok: true, note: resp.note };
    }

    case MSG.SET_TOKEN: {
      state[STORAGE_KEYS.TOKEN] = (msg.token || "").trim();
      const probe = await fetchQueue(state[STORAGE_KEYS.TOKEN], state[STORAGE_KEYS.SETTINGS].jobFilter);
      state[STORAGE_KEYS.SERVER_ONLINE] = probe.ok;
      state[STORAGE_KEYS.LAST_ERROR] = probe.ok
        ? ""
        : probe.status === 401
          ? "Token rejected by ApplyPilot."
          : "Saved token, but ApplyPilot is not reachable on localhost:8765.";
      await persist(state);
      return { ok: probe.ok, serverOnline: probe.ok };
    }

    default:
      return { ok: false, error: "unknown message type: " + msg.type };
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  withLock(() => handleMessage(msg, sender))
    .then((r) => sendResponse(r === undefined ? { ok: true } : r))
    .catch((e) => sendResponse({ ok: false, error: String(e && e.message ? e.message : e) }));
  return true; // async sendResponse — keep the channel open
});

// ---------------------------------------------------------------------------
// Alarms — the pacing backstop. On wake, if we deferred an advance for pacing, re-evaluate.
// ---------------------------------------------------------------------------
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== PACING_ALARM) return;
  withLock(async () => {
    const state = await loadState();
    if (state[STORAGE_KEYS.RUNNING] && !state[STORAGE_KEYS.PAUSED] && state[STORAGE_KEYS.PHASE] === RUN_PHASE.PACING) {
      await advanceToNext(state, {}); // ≥30s elapsed → pacing gate now passes
    } else {
      await persist(state);
    }
  });
});

// ---------------------------------------------------------------------------
// Tab lifecycle — if the dedicated tab is closed, pause and prompt (never hijack another tab).
// ---------------------------------------------------------------------------
chrome.tabs.onRemoved.addListener((tabId) => {
  withLock(async () => {
    const state = await loadState();
    if (tabId === state[STORAGE_KEYS.ACTIVE_TAB_ID]) {
      const wasRunning = state[STORAGE_KEYS.RUNNING];
      state[STORAGE_KEYS.ACTIVE_TAB_ID] = null;
      if (wasRunning) {
        state[STORAGE_KEYS.PAUSED] = true;
        state[STORAGE_KEYS.PHASE] = RUN_PHASE.PAUSED;
        state[STORAGE_KEYS.LAST_ERROR] = "The ApplyPilot LinkedIn tab was closed. Resume to reopen it.";
      }
      await persist(state);
    }
  });
});

// ---------------------------------------------------------------------------
// Lifecycle — initialize missing keys on install; clear the (now-dead) tab on browser restart.
// A mere SW eviction+wake does NOT fire these, so the persisted run-state survives untouched.
// ---------------------------------------------------------------------------
chrome.runtime.onInstalled.addListener(() => {
  withLock(async () => {
    const raw = await chrome.storage.local.get(STORAGE_KEY_LIST);
    const d = defaultState();
    const patch = {};
    for (const k of STORAGE_KEY_LIST) if (raw[k] === undefined) patch[k] = d[k];
    if (Object.keys(patch).length) await chrome.storage.local.set(patch);
  });
});

chrome.runtime.onStartup.addListener(() => {
  withLock(async () => {
    const state = await loadState();
    // The browser restarted → the dedicated tab is gone. Stand the loop down cleanly; keep the
    // dailyCount/windowStart/settings/token (those legitimately persist across sessions).
    state[STORAGE_KEYS.RUNNING] = false;
    state[STORAGE_KEYS.PAUSED] = false;
    state[STORAGE_KEYS.PHASE] = RUN_PHASE.IDLE;
    state[STORAGE_KEYS.ACTIVE_TAB_ID] = null;
    state[STORAGE_KEYS.ACTIVE_CONTACT_ID] = null;
    state[STORAGE_KEYS.CURSOR] = -1;
    await persist(state);
  });
});

// ApplyPilot LinkedIn Assistant — FROZEN shared constants (single source of truth).
//
// This module is imported verbatim by background.js, content.js, and popup.js so every
// surface uses the EXACT same strings. Do NOT redefine these anywhere else. Changing a
// value here is a contract change (bump manifest version + update CONTRACTS.md).
//
// Plain ES module, no build step. Import with:
//   import { MSG, STORAGE_KEYS, API, DM_STATUS, DEFAULT_SETTINGS } from "./shared/constants.js";
// Content scripts (classic, non-module) load it via a small loader or a build-free
// `chrome.runtime.getURL` dynamic import; see CONTRACTS.md §7.

// ---------------------------------------------------------------------------
// MSG — chrome.runtime message `type` strings. Every message is { type, ...payload }.
// Direction key: C=content script, B=background service worker, P=popup, O=on-page overlay
// (the overlay is part of the content script, so O messages travel content→background).
// ---------------------------------------------------------------------------
export const MSG = Object.freeze({
  // --- content/overlay -> background ---------------------------------------
  // Content pulls its assignment on load (pull model, never a push to a dead worker).
  // Payload: { tabId }  (background authoritative source is sender.tab.id; payload.tabId
  // is a cross-check only). Response: ASSIGNMENT shape (see CONTRACTS §2).
  GET_ASSIGNMENT: "GET_ASSIGNMENT",
  // Result of the auto-compose attempt. Payload: { contactId, ok:boolean, reason:string }
  // reason is "" on success, else a COMPOSE_FAIL_REASON value.
  COMPOSE_RESULT: "COMPOSE_RESULT",
  // Human clicked Send and the content script positively confirmed it. Payload: { contactId }
  SEND_DETECTED: "SEND_DETECTED",
  // Never-break fallback fired (gesture-backed Copy note used). Payload: { contactId, reason }
  // Background marks the contact `manual` and advances.
  FALLBACK_MANUAL: "FALLBACK_MANUAL",
  // Contact could not be actioned and should be skipped. Payload: { contactId, reason }
  SKIP_CONTACT: "SKIP_CONTACT",
  // On-page profile name != intended contact. Payload: { contactId, onPageName }
  // Background refuses "ready to Send", pauses, surfaces the mismatch.
  IDENTITY_MISMATCH: "IDENTITY_MISMATCH",
  // N consecutive fallbacks-to-manual — selectors likely stale. Payload: { consecutive }
  LAYOUT_CHANGED: "LAYOUT_CHANGED",
  // LinkedIn weekly-invite / note-quota banner seen. Payload: { contactId, kind } where
  // kind is "weekly_invite" | "note_quota". Background pauses with a distinct message.
  LIMIT_BANNER: "LIMIT_BANNER",
  // Overlay controls (popup is closed during Send, so these live on the page).
  OVERLAY_PAUSE: "OVERLAY_PAUSE",
  OVERLAY_RESUME: "OVERLAY_RESUME",
  OVERLAY_SKIP: "OVERLAY_SKIP", // payload: { contactId }

  // --- background -> content/overlay ---------------------------------------
  // Response to GET_ASSIGNMENT, also pushed to re-arm after an advance/navigate.
  // Payload: { contact:QueueContact|null, settings, running:boolean, phase:string }
  ASSIGNMENT: "ASSIGNMENT",
  // Tell an armed content script to stop/stand down (pause, cap reached, tab reused).
  ABORT: "ABORT",

  // --- popup <-> background ------------------------------------------------
  // Popup requests the full persisted state for first render. Response: full state object
  // (all STORAGE_KEYS). Popup thereafter renders from chrome.storage.onChanged.
  GET_STATE: "GET_STATE",
  START_QUEUE: "START_QUEUE",     // payload: { jobFilter:string|null }
  PAUSE_QUEUE: "PAUSE_QUEUE",
  RESUME_QUEUE: "RESUME_QUEUE",
  NEXT: "NEXT",                   // advance to next ready contact (manual)
  SKIP: "SKIP",                   // payload: { contactId }
  REFRESH_QUEUE: "REFRESH_QUEUE", // re-fetch GET /api/ext/queue and rebuild queue
  UPDATE_SETTINGS: "UPDATE_SETTINGS", // payload: { settings: Partial<Settings> }
  SAVE_NOTE: "SAVE_NOTE",         // payload: { contactId, note } -> POST /api/ext/note
  SET_TOKEN: "SET_TOKEN",         // payload: { token } persist the mutual shared token
});

// COMPOSE_RESULT.reason / SKIP_CONTACT.reason / FALLBACK_MANUAL.reason enum.
export const COMPOSE_FAIL_REASON = Object.freeze({
  OK: "",                              // success sentinel
  NO_CONNECT_BUTTON: "no_connect_button",
  ALREADY_CONNECTED: "already_connected",   // 1st-degree; hand to Message flow (EXT-6) or skip
  INMAIL_ONLY: "inmail_only",               // no free Connect (Premium/InMail wall)
  WEEKLY_LIMIT: "weekly_limit",             // weekly invite cap banner
  NOTE_QUOTA_REACHED: "note_quota_reached", // free-account ~5 personalized notes/month
  PROFILE_404: "profile_404",               // stale/redirected URL
  IDENTITY_MISMATCH: "identity_mismatch",   // on-page name != intended contact
  NOTE_FIELD_NOT_FOUND: "note_field_not_found",
  FILL_VERIFY_FAILED: "fill_verify_failed", // textarea did not hold the exact note
  PENDING_ALREADY: "pending_already",       // pre-existing Pending badge -> already invited
  UNKNOWN: "unknown",
});

// ---------------------------------------------------------------------------
// STORAGE_KEYS — the ONLY keys written to chrome.storage.local. The worker owns NO durable
// state; this map IS the run-state. Every handler rehydrates on entry, writes back before
// returning. Popup + overlay render via chrome.storage.onChanged. See CONTRACTS §1.
// ---------------------------------------------------------------------------
export const STORAGE_KEYS = Object.freeze({
  QUEUE: "queue",                   // QueueContact[]  (ordered, deduped by linkedin_url)
  CURSOR: "cursor",                 // number — index into queue of the active contact (-1 idle)
  ACTIVE_CONTACT_ID: "activeContactId", // string|null — queue[cursor].id, the authority
  ACTIVE_TAB_ID: "activeTabId",     // number|null — the dedicated ApplyPilot LinkedIn tab
  STATUS_MAP: "statusMap",          // { [contactId]: UI_STATE } transient per-contact status
  PROGRESS: "progress",             // { sent:number, total:number }
  DAILY_COUNT: "dailyCount",        // number — human-sent invites in the current 24h window
  WINDOW_START: "windowStart",      // number (epoch ms) — start of the current daily-cap window
  LAST_ADVANCE_TS: "lastAdvanceTs", // number (epoch ms) — for timestamp-compare pacing on wake
  SETTINGS: "settings",             // Settings (see DEFAULT_SETTINGS)
  RUNNING: "running",               // boolean — the "queue active" gate; content is inert when false
  PAUSED: "paused",                 // boolean — user/limit hold (running stays true)
  PHASE: "phase",                   // RUN_PHASE — coarse state of the active contact
  CONSECUTIVE_FALLBACKS: "consecutiveFallbacks", // number — for the layout-changed self-check
  SERVER_ONLINE: "serverOnline",    // boolean — last known localhost:8765 reachability
  TOKEN: "extToken",                // string — mutual shared token (from ~/.applypilot/ext_token)
  LAST_ERROR: "lastError",          // string — last surfaced error/limit message (for popup)
});

// RUN_PHASE — coarse lifecycle of the currently-active contact (STORAGE_KEYS.PHASE).
export const RUN_PHASE = Object.freeze({
  IDLE: "idle",             // no queue running
  NAVIGATING: "navigating", // tab is loading the profile
  COMPOSING: "composing",   // content script is filling the note
  READY_TO_SEND: "ready_to_send", // note filled + identity verified; waiting on human Send
  PACING: "pacing",         // between contacts, waiting out the pacing delay
  PAUSED: "paused",         // held (user, cap, weekly-limit)
  DONE: "done",             // queue exhausted
});

// UI_STATE — per-contact chip states (STORAGE_KEYS.STATUS_MAP values) for popup + overlay.
// Superset of DM_STATUS: adds the transient client-only states. On send/fallback/skip these
// collapse to the persisted DM_STATUS the server records.
export const UI_STATE = Object.freeze({
  READY: "ready",
  COMPOSING: "composing",
  COMPOSED: "composed",     // note filled, waiting on human Send
  SENT: "sent",
  MANUAL: "manual",         // needs-manual / pasted by hand
  SKIPPED: "skipped",
  ERROR: "error",           // transient failure, will fall back
});

// ---------------------------------------------------------------------------
// API — the local ApplyPilot dashboard contract. localhost only; no remote hosts.
// Auth: every request carries TOKEN_HEADER: <token from chrome.storage[TOKEN]>. The token
// originates in ~/.applypilot/ext_token (the server generates it; the user pastes it into
// the popup once). Mutual: the extension refuses a server that doesn't echo/accept the token
// (defends against a :8765 squatter feeding a poisoned queue).
// ---------------------------------------------------------------------------
export const API = Object.freeze({
  BASE_URL: "http://localhost:8765",
  QUEUE: "/api/ext/queue",   // GET  [?job_url=...]  -> { ok, contacts: QueueContact[] }
  STATUS: "/api/ext/status", // POST { contact_id, status }  -> { ok }
  NOTE: "/api/ext/note",     // POST { contact_id, note }    -> { ok, note }
  TOKEN_HEADER: "X-ApplyPilot-Token",
  TOKEN_FILE: "~/.applypilot/ext_token", // informational: where the server writes the token
});

// ---------------------------------------------------------------------------
// DM_STATUS — the server-persisted lifecycle (contacts.dm_status). These are the values the
// extension may POST to /api/ext/status and read from /api/ext/queue. The full DB enum is
// none|sending|composed|sent|manual|skipped|failed; the extension only WRITES sent/manual/
// skipped and only READS not-done contacts. DONE = terminal for the queue (excluded).
// ---------------------------------------------------------------------------
export const DM_STATUS = Object.freeze({
  NONE: "none",
  COMPOSED: "composed", // note filled on page but NOT yet sent — still eligible (human hasn't sent)
  SENT: "sent",         // human clicked Send, positively detected — DONE
  MANUAL: "manual",     // pasted by hand via fallback — DONE, counts toward dedupe/cap
  SKIPPED: "skipped",   // InMail-only / already-connected / stale — DONE, no invite sent
});

// The set the server excludes from queue eligibility (mirrors web_dashboard._DM_DONE_STATUSES).
export const DM_DONE_STATUSES = Object.freeze([
  DM_STATUS.SENT, DM_STATUS.MANUAL, DM_STATUS.SKIPPED,
]);

// Statuses the extension is allowed to POST to /api/ext/status.
export const POSTABLE_STATUSES = Object.freeze([
  DM_STATUS.SENT, DM_STATUS.MANUAL, DM_STATUS.SKIPPED,
]);

// ---------------------------------------------------------------------------
// DEFAULT_SETTINGS — Settings shape + defaults (STORAGE_KEYS.SETTINGS). Persisted; user edits
// in the popup Settings panel. jobFilter=null means "all jobs" (server all-jobs queue variant).
// ---------------------------------------------------------------------------
export const DEFAULT_SETTINGS = Object.freeze({
  dailyCap: 20,           // max human-sent invites (sent+manual) per 24h window
  pacingSeconds: 8,       // min seconds between advancing to the next contact
  confirmBeforeEach: false, // if true, wait for an explicit overlay confirm before composing next
  jobFilter: null,        // null = all jobs; else a specific job_url string
});

// The LinkedIn profile URL the background must validate before ANY navigation (H4).
export const LINKEDIN_PROFILE_RE = /^https:\/\/([a-z]+\.)?linkedin\.com\/in\//;

// Hard cap on the note length, enforced client- and server-side.
export const NOTE_MAX_LEN = 300;

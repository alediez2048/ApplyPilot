// ApplyPilot LinkedIn Assistant — CLASSIC content-script mirror of shared/constants.js.
//
// WHY THIS FILE EXISTS: a content script cannot `import()` shared/constants.js — dynamic
// import inside a content script is subject to the HOST PAGE's CSP, and LinkedIn's strict
// CSP blocks it (the whole content script would silently go inert). So the same constants
// are exposed here as a CLASSIC script (no import/export) that the manifest injects BEFORE
// content.js; both run in the same isolated world, so content.js reads the global below.
//
// ⚠ MUST STAY IN SYNC with shared/constants.js (the module version background.js imports).
// If you change a value in constants.js, change it here too. Kept literal (not generated)
// to avoid a build step.

(function () {
  const MSG = Object.freeze({
    GET_ASSIGNMENT: "GET_ASSIGNMENT",
    COMPOSE_RESULT: "COMPOSE_RESULT",
    SEND_DETECTED: "SEND_DETECTED",
    FALLBACK_MANUAL: "FALLBACK_MANUAL",
    SKIP_CONTACT: "SKIP_CONTACT",
    IDENTITY_MISMATCH: "IDENTITY_MISMATCH",
    LAYOUT_CHANGED: "LAYOUT_CHANGED",
    LIMIT_BANNER: "LIMIT_BANNER",
    OVERLAY_PAUSE: "OVERLAY_PAUSE",
    OVERLAY_RESUME: "OVERLAY_RESUME",
    OVERLAY_SKIP: "OVERLAY_SKIP",
    ASSIGNMENT: "ASSIGNMENT",
    ABORT: "ABORT",
    GET_STATE: "GET_STATE",
    START_QUEUE: "START_QUEUE",
    PAUSE_QUEUE: "PAUSE_QUEUE",
    RESUME_QUEUE: "RESUME_QUEUE",
    NEXT: "NEXT",
    SKIP: "SKIP",
    REFRESH_QUEUE: "REFRESH_QUEUE",
    UPDATE_SETTINGS: "UPDATE_SETTINGS",
    SAVE_NOTE: "SAVE_NOTE",
    SET_TOKEN: "SET_TOKEN",
  });

  const COMPOSE_FAIL_REASON = Object.freeze({
    OK: "",
    NO_CONNECT_BUTTON: "no_connect_button",
    ALREADY_CONNECTED: "already_connected",
    INMAIL_ONLY: "inmail_only",
    WEEKLY_LIMIT: "weekly_limit",
    NOTE_QUOTA_REACHED: "note_quota_reached",
    PROFILE_404: "profile_404",
    IDENTITY_MISMATCH: "identity_mismatch",
    NOTE_FIELD_NOT_FOUND: "note_field_not_found",
    FILL_VERIFY_FAILED: "fill_verify_failed",
    PENDING_ALREADY: "pending_already",
    UNKNOWN: "unknown",
  });

  const STORAGE_KEYS = Object.freeze({
    QUEUE: "queue",
    CURSOR: "cursor",
    ACTIVE_CONTACT_ID: "activeContactId",
    ACTIVE_TAB_ID: "activeTabId",
    STATUS_MAP: "statusMap",
    PROGRESS: "progress",
    DAILY_COUNT: "dailyCount",
    WINDOW_START: "windowStart",
    LAST_ADVANCE_TS: "lastAdvanceTs",
    SETTINGS: "settings",
    RUNNING: "running",
    PAUSED: "paused",
    PHASE: "phase",
    CONSECUTIVE_FALLBACKS: "consecutiveFallbacks",
    SERVER_ONLINE: "serverOnline",
    TOKEN: "extToken",
    LAST_ERROR: "lastError",
  });

  const RUN_PHASE = Object.freeze({
    IDLE: "idle",
    NAVIGATING: "navigating",
    COMPOSING: "composing",
    READY_TO_SEND: "ready_to_send",
    PACING: "pacing",
    PAUSED: "paused",
    DONE: "done",
  });

  const UI_STATE = Object.freeze({
    READY: "ready",
    COMPOSING: "composing",
    COMPOSED: "composed",
    SENT: "sent",
    MANUAL: "manual",
    SKIPPED: "skipped",
    ERROR: "error",
  });

  const API = Object.freeze({
    BASE_URL: "http://localhost:8765",
    QUEUE: "/api/ext/queue",
    STATUS: "/api/ext/status",
    NOTE: "/api/ext/note",
    TOKEN_HEADER: "X-ApplyPilot-Token",
    TOKEN_FILE: "~/.applypilot/ext_token",
  });

  const DM_STATUS = Object.freeze({
    NONE: "none",
    COMPOSED: "composed",
    SENT: "sent",
    MANUAL: "manual",
    SKIPPED: "skipped",
  });

  const DEFAULT_SETTINGS = Object.freeze({
    dailyCap: 20,
    pacingSeconds: 8,
    confirmBeforeEach: false,
    jobFilter: null,
  });

  const LINKEDIN_PROFILE_RE = /^https:\/\/([a-z]+\.)?linkedin\.com\/in\//;
  const NOTE_MAX_LEN = 300;

  // Expose to the isolated-world global for content.js (runs after this script).
  globalThis.__APPLYPILOT_CONSTANTS__ = Object.freeze({
    MSG, COMPOSE_FAIL_REASON, STORAGE_KEYS, RUN_PHASE, UI_STATE, API, DM_STATUS,
    DEFAULT_SETTINGS, LINKEDIN_PROFILE_RE, NOTE_MAX_LEN,
  });
})();

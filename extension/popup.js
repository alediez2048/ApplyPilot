// ApplyPilot LinkedIn Assistant — popup control panel (EXT-1 skeleton + EXT-5 polish).
//
// Contract compliance:
//  - Renders ENTIRELY from chrome.storage.local (the stateless worker's run-state) and stays
//    live via chrome.storage.onChanged. The running loop never depends on this popup.
//  - All queue-derived strings (name/title/company/note) are inserted with textContent — NEVER
//    innerHTML — because they are attacker-influenceable.
//  - Every mutation goes through the background via chrome.runtime messages (MSG.*). The popup
//    itself never touches the network or the LinkedIn DOM.
//
// See extension/CONTRACTS.md §1 (storage schema) and §2 (popup ↔ background messages).

import {
  MSG,
  STORAGE_KEYS,
  UI_STATE,
  RUN_PHASE,
  DEFAULT_SETTINGS,
  LINKEDIN_PROFILE_RE,
  NOTE_MAX_LEN,
} from "./shared/constants.js";

// ---------------------------------------------------------------------------
// Local render state — a snapshot of chrome.storage.local, kept live by onChanged.
// This is a CACHE for rendering only; chrome.storage remains the source of truth.
// ---------------------------------------------------------------------------
let state = {};

// Note-editor UI state (only one row edits at a time; preserved across re-renders).
let editingId = null;
let editDraft = "";

// Human-readable labels for each per-contact UI_STATE (STATUS_MAP values).
const STATUS_LABELS = Object.freeze({
  [UI_STATE.READY]: "ready",
  [UI_STATE.COMPOSING]: "composing",
  [UI_STATE.COMPOSED]: "ready to send",
  [UI_STATE.SENT]: "sent",
  [UI_STATE.MANUAL]: "needs manual",
  [UI_STATE.SKIPPED]: "skipped",
  [UI_STATE.ERROR]: "error",
});

// Terminal statuses — row actions (edit/skip) are disabled for these.
const DONE_UI_STATES = new Set([UI_STATE.SENT, UI_STATE.MANUAL, UI_STATE.SKIPPED]);

// ---------------------------------------------------------------------------
// State accessors (with defaults) — never read storage keys inline elsewhere.
// ---------------------------------------------------------------------------
const getQueue = () => (Array.isArray(state[STORAGE_KEYS.QUEUE]) ? state[STORAGE_KEYS.QUEUE] : []);
const getSettings = () => ({ ...DEFAULT_SETTINGS, ...(state[STORAGE_KEYS.SETTINGS] || {}) });
const getStatusMap = () => state[STORAGE_KEYS.STATUS_MAP] || {};
const getProgress = () => state[STORAGE_KEYS.PROGRESS] || { sent: 0, total: 0 };
const getDailyCount = () => Number(state[STORAGE_KEYS.DAILY_COUNT] || 0);
const getWindowStart = () => Number(state[STORAGE_KEYS.WINDOW_START] || 0);
const getRunning = () => !!state[STORAGE_KEYS.RUNNING];
const getPaused = () => !!state[STORAGE_KEYS.PAUSED];
const getServerOnline = () => !!state[STORAGE_KEYS.SERVER_ONLINE];
const getToken = () => state[STORAGE_KEYS.TOKEN] || "";
const getActiveContactId = () => state[STORAGE_KEYS.ACTIVE_CONTACT_ID] || null;
const getLastError = () => state[STORAGE_KEYS.LAST_ERROR] || "";
const getPhase = () => state[STORAGE_KEYS.PHASE] || RUN_PHASE.IDLE;

const statusFor = (id) => getStatusMap()[id] || UI_STATE.READY;

// ---------------------------------------------------------------------------
// Messaging helper — background may be waking, so tolerate rejections.
// ---------------------------------------------------------------------------
async function send(msg) {
  try {
    return await chrome.runtime.sendMessage(msg);
  } catch (err) {
    // Service worker asleep / no receiver — non-fatal for the popup.
    console.debug("[popup] sendMessage failed", msg?.type, err?.message || err);
    return null;
  }
}

// ---------------------------------------------------------------------------
// Element cache
// ---------------------------------------------------------------------------
const $ = (id) => document.getElementById(id);

const el = {
  connDot: $("conn-dot"),
  connText: $("conn-text"),
  tokenBanner: $("token-banner"),
  tokenInput: $("token-input"),
  tokenSave: $("token-save"),
  errorBanner: $("error-banner"),
  errorText: $("error-text"),
  btnPrimary: $("btn-primary"),
  btnNext: $("btn-next"),
  btnRefresh: $("btn-refresh"),
  progressText: $("progress-text"),
  phaseText: $("phase-text"),
  meterCount: $("meter-count"),
  meterFill: $("meter-fill"),
  meterReset: $("meter-reset"),
  queueList: $("queue-list"),
  queueCount: $("queue-count"),
  queueEmpty: $("queue-empty"),
  setDailyCap: $("set-dailyCap"),
  setPacing: $("set-pacingSeconds"),
  setConfirm: $("set-confirmBeforeEach"),
  setJobFilter: $("set-jobFilter"),
  setToken: $("set-token"),
  setTokenSave: $("set-token-save"),
  settingsSave: $("settings-save"),
  settingsSaved: $("settings-saved"),
};

// ---------------------------------------------------------------------------
// Render — connection dot
// ---------------------------------------------------------------------------
function renderConnection() {
  const online = getServerOnline();
  el.connDot.classList.toggle("dot--on", online);
  el.connDot.classList.toggle("dot--off", !online);
  el.connText.textContent = online ? "Connected" : "Not connected";
}

// ---------------------------------------------------------------------------
// Render — token setup banner (only when no token stored)
// ---------------------------------------------------------------------------
function renderTokenBanner() {
  el.tokenBanner.hidden = !!getToken();
}

// ---------------------------------------------------------------------------
// Render — last error / limit banner
// ---------------------------------------------------------------------------
function renderError() {
  const msg = getLastError();
  if (msg) {
    el.errorText.textContent = msg;
    el.errorBanner.hidden = false;
  } else {
    el.errorText.textContent = "";
    el.errorBanner.hidden = true;
  }
}

// ---------------------------------------------------------------------------
// Render — queue controls (Start/Pause/Resume, Next), progress, phase
// ---------------------------------------------------------------------------
function renderControls() {
  const running = getRunning();
  const paused = getPaused();
  const hasToken = !!getToken();

  let label, kind;
  if (!running) {
    label = "Start";
    kind = "start";
  } else if (paused) {
    label = "Resume";
    kind = "resume";
  } else {
    label = "Pause";
    kind = "pause";
  }
  el.btnPrimary.textContent = label;
  el.btnPrimary.dataset.kind = kind;
  // Starting requires a token (the server rejects an unauthenticated queue fetch).
  el.btnPrimary.disabled = kind === "start" && !hasToken;

  el.btnNext.disabled = !running || paused;

  const { sent, total } = getProgress();
  el.progressText.textContent = `${sent} / ${total} sent`;

  const phase = getPhase();
  el.phaseText.textContent = phase && phase !== RUN_PHASE.IDLE ? phase.replace(/_/g, " ") : "";
}

// ---------------------------------------------------------------------------
// Render — daily-cap meter
// ---------------------------------------------------------------------------
function renderMeter() {
  const count = getDailyCount();
  const cap = getSettings().dailyCap || DEFAULT_SETTINGS.dailyCap;
  el.meterCount.textContent = `${count} / ${cap}`;

  const pct = cap > 0 ? Math.min(100, Math.round((count / cap) * 100)) : 0;
  el.meterFill.style.width = `${pct}%`;
  el.meterFill.classList.toggle("meter-fill--full", count >= cap);

  const start = getWindowStart();
  if (start > 0) {
    const resetAt = new Date(start + 24 * 60 * 60 * 1000);
    const time = resetAt.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    el.meterReset.textContent = count > 0 ? `Resets ${time}` : `Window opens on first send`;
  } else {
    el.meterReset.textContent = "";
  }
}

// ---------------------------------------------------------------------------
// Render — queue list. Rebuilt from scratch each time; textContent only.
// ---------------------------------------------------------------------------
function renderQueue() {
  const queue = getQueue();
  const activeId = getActiveContactId();

  el.queueCount.textContent = queue.length ? String(queue.length) : "";
  el.queueEmpty.hidden = queue.length > 0;

  // Clear existing rows without innerHTML.
  el.queueList.replaceChildren();

  for (const contact of queue) {
    el.queueList.appendChild(buildRow(contact, contact.id === activeId));
  }
}

function buildRow(contact, isActive) {
  const li = document.createElement("li");
  li.className = "row-item";
  if (isActive) li.classList.add("row-item--active");
  li.dataset.contactId = contact.id;

  // --- header line: name + status chip ---
  const head = document.createElement("div");
  head.className = "row-head";

  const name = document.createElement("span");
  name.className = "row-name";
  name.textContent = contact.full_name || "(unknown)";
  head.appendChild(name);

  const st = statusFor(contact.id);
  const chip = document.createElement("span");
  chip.className = `chip chip--${st}`;
  chip.textContent = STATUS_LABELS[st] || st;
  head.appendChild(chip);

  li.appendChild(head);

  // --- sub line: title · company ---
  const meta = [contact.title, contact.company].filter(Boolean).join(" · ");
  if (meta) {
    const sub = document.createElement("div");
    sub.className = "row-sub";
    sub.textContent = meta;
    li.appendChild(sub);
  }

  const isDone = DONE_UI_STATES.has(st);
  const isEditing = editingId === contact.id;

  if (isEditing) {
    li.appendChild(buildEditor(contact));
  } else {
    // --- note preview ---
    const note = document.createElement("div");
    note.className = "row-note";
    note.textContent = contact.note || "(no note drafted)";
    li.appendChild(note);

    // --- actions ---
    li.appendChild(buildActions(contact, isActive, isDone));
  }

  return li;
}

function buildActions(contact, isActive, isDone) {
  const actions = document.createElement("div");
  actions.className = "row-actions";

  // Edit note
  const editBtn = document.createElement("button");
  editBtn.className = "link-btn";
  editBtn.textContent = "Edit note";
  editBtn.disabled = isDone;
  editBtn.addEventListener("click", () => openEditor(contact));
  actions.appendChild(editBtn);

  // Skip
  const skipBtn = document.createElement("button");
  skipBtn.className = "link-btn";
  skipBtn.textContent = "Skip";
  skipBtn.disabled = isDone;
  skipBtn.addEventListener("click", () => onSkip(contact.id));
  actions.appendChild(skipBtn);

  // Open profile — only if the URL passes the LinkedIn profile guard.
  if (contact.linkedin_url && LINKEDIN_PROFILE_RE.test(contact.linkedin_url)) {
    const open = document.createElement("a");
    open.className = "link-btn";
    open.textContent = "Open profile";
    open.href = contact.linkedin_url;
    open.target = "_blank";
    open.rel = "noopener noreferrer";
    actions.appendChild(open);
  }

  return actions;
}

function buildEditor(contact) {
  const wrap = document.createElement("div");
  wrap.className = "row-editor";

  // Warn when editing a contact that is already composed on the page.
  const st = statusFor(contact.id);
  if (st === UI_STATE.COMPOSED || contact.id === getActiveContactId()) {
    const warn = document.createElement("div");
    warn.className = "editor-warn";
    warn.textContent = "This contact is being composed on the page — saving re-fills the note.";
    wrap.appendChild(warn);
  }

  const ta = document.createElement("textarea");
  ta.className = "editor-textarea";
  ta.maxLength = NOTE_MAX_LEN;
  ta.value = editDraft;
  ta.rows = 4;
  wrap.appendChild(ta);

  const foot = document.createElement("div");
  foot.className = "editor-foot";

  const counter = document.createElement("span");
  counter.className = "editor-counter";
  const setCounter = () => {
    counter.textContent = `${ta.value.length} / ${NOTE_MAX_LEN}`;
    counter.classList.toggle("editor-counter--max", ta.value.length >= NOTE_MAX_LEN);
  };
  setCounter();

  ta.addEventListener("input", () => {
    // Hard cap (belt-and-suspenders alongside maxLength).
    if (ta.value.length > NOTE_MAX_LEN) ta.value = ta.value.slice(0, NOTE_MAX_LEN);
    editDraft = ta.value;
    setCounter();
  });

  const btns = document.createElement("div");
  btns.className = "editor-btns";

  const saveBtn = document.createElement("button");
  saveBtn.className = "btn btn--primary btn--sm";
  saveBtn.textContent = "Save";
  saveBtn.addEventListener("click", () => onSaveNote(contact.id, ta.value));
  btns.appendChild(saveBtn);

  const cancelBtn = document.createElement("button");
  cancelBtn.className = "btn btn--ghost btn--sm";
  cancelBtn.textContent = "Cancel";
  cancelBtn.addEventListener("click", closeEditor);
  btns.appendChild(cancelBtn);

  foot.appendChild(counter);
  foot.appendChild(btns);
  wrap.appendChild(foot);

  // Focus after the row is in the DOM.
  queueMicrotask(() => {
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
  });

  return wrap;
}

// ---------------------------------------------------------------------------
// Render — settings inputs. Only written when NOT focused, so live storage
// changes never clobber what the user is typing.
// ---------------------------------------------------------------------------
function syncSettings() {
  const s = getSettings();
  setIfIdle(el.setDailyCap, s.dailyCap);
  setIfIdle(el.setPacing, s.pacingSeconds);
  if (document.activeElement !== el.setConfirm) el.setConfirm.checked = !!s.confirmBeforeEach;
  setIfIdle(el.setJobFilter, s.jobFilter == null ? "" : s.jobFilter);
  // Token field is left as the user typed it; we only mirror a placeholder-style hint.
  if (document.activeElement !== el.setToken && !el.setToken.value) {
    el.setToken.placeholder = getToken() ? "Token set — enter to replace" : "X-ApplyPilot-Token";
  }
}

function setIfIdle(input, value) {
  if (document.activeElement !== input) input.value = value;
}

// ---------------------------------------------------------------------------
// Master render
// ---------------------------------------------------------------------------
function render() {
  renderConnection();
  renderTokenBanner();
  renderError();
  renderControls();
  renderMeter();
  renderQueue();
  syncSettings();
}

// ---------------------------------------------------------------------------
// Editor open/close
// ---------------------------------------------------------------------------
function openEditor(contact) {
  editingId = contact.id;
  editDraft = contact.note || "";
  renderQueue();
}

function closeEditor() {
  editingId = null;
  editDraft = "";
  renderQueue();
}

// ---------------------------------------------------------------------------
// Actions → background messages
// ---------------------------------------------------------------------------
async function onSaveNote(contactId, note) {
  const trimmed = (note || "").slice(0, NOTE_MAX_LEN);
  await send({ type: MSG.SAVE_NOTE, contactId, note: trimmed });
  // Background POSTs and updates queue[i].note in storage → onChanged re-renders.
  closeEditor();
}

async function onSkip(contactId) {
  await send({ type: MSG.SKIP, contactId });
}

async function onPrimary() {
  const kind = el.btnPrimary.dataset.kind;
  if (kind === "start") {
    const jobFilter = getSettings().jobFilter || null;
    await send({ type: MSG.START_QUEUE, jobFilter });
  } else if (kind === "pause") {
    await send({ type: MSG.PAUSE_QUEUE });
  } else if (kind === "resume") {
    await send({ type: MSG.RESUME_QUEUE });
  }
}

async function onNext() {
  await send({ type: MSG.NEXT });
}

async function onRefresh() {
  await send({ type: MSG.REFRESH_QUEUE });
}

async function onSaveSettings() {
  const dailyCap = clampInt(el.setDailyCap.value, 1, 200, DEFAULT_SETTINGS.dailyCap);
  const pacingSeconds = clampInt(el.setPacing.value, 0, 600, DEFAULT_SETTINGS.pacingSeconds);
  const confirmBeforeEach = !!el.setConfirm.checked;
  const jobFilterRaw = (el.setJobFilter.value || "").trim();
  const jobFilter = jobFilterRaw === "" ? null : jobFilterRaw;

  await send({
    type: MSG.UPDATE_SETTINGS,
    settings: { dailyCap, pacingSeconds, confirmBeforeEach, jobFilter },
  });
  flashSaved();
}

async function onSaveToken(inputEl) {
  const token = (inputEl.value || "").trim();
  if (!token) return;
  await send({ type: MSG.SET_TOKEN, token });
  inputEl.value = "";
  // Re-fetch so the connection dot + queue reflect the freshly-authenticated server.
  await send({ type: MSG.REFRESH_QUEUE });
}

function clampInt(raw, min, max, fallback) {
  const n = parseInt(raw, 10);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(min, Math.min(max, n));
}

let savedTimer = null;
function flashSaved() {
  el.settingsSaved.hidden = false;
  clearTimeout(savedTimer);
  savedTimer = setTimeout(() => {
    el.settingsSaved.hidden = true;
  }, 1500);
}

// ---------------------------------------------------------------------------
// Storage wiring — first render from GET_STATE, then live via onChanged.
// ---------------------------------------------------------------------------
async function loadInitialState() {
  // Ask the background to seed defaults + refresh reachability; response (if any) is the
  // full persisted state. Fall back to reading storage directly for a guaranteed shape.
  const fromBg = await send({ type: MSG.GET_STATE });
  if (fromBg && typeof fromBg === "object") {
    state = fromBg;
  } else {
    state = await chrome.storage.local.get(Object.values(STORAGE_KEYS));
  }
  render();

  // Nudge a queue + connectivity refresh on open when we have a token.
  if (getToken()) send({ type: MSG.REFRESH_QUEUE });
}

function onStorageChanged(changes, areaName) {
  if (areaName !== "local") return;
  let touched = false;
  for (const [key, { newValue }] of Object.entries(changes)) {
    state[key] = newValue;
    touched = true;
  }
  if (touched) render();
}

// ---------------------------------------------------------------------------
// Static event wiring
// ---------------------------------------------------------------------------
function wireEvents() {
  el.btnPrimary.addEventListener("click", onPrimary);
  el.btnNext.addEventListener("click", onNext);
  el.btnRefresh.addEventListener("click", onRefresh);
  el.settingsSave.addEventListener("click", onSaveSettings);

  el.tokenSave.addEventListener("click", () => onSaveToken(el.tokenInput));
  el.tokenInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") onSaveToken(el.tokenInput);
  });
  el.setTokenSave.addEventListener("click", () => onSaveToken(el.setToken));
  el.setToken.addEventListener("keydown", (e) => {
    if (e.key === "Enter") onSaveToken(el.setToken);
  });
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
function boot() {
  wireEvents();
  chrome.storage.onChanged.addListener(onStorageChanged);
  loadInitialState();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}

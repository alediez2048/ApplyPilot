// ApplyPilot LinkedIn Assistant — content.js
// =============================================================================
// THE ONLY FILE THAT TOUCHES THE LINKEDIN DOM. Injected on https://*.linkedin.com/in/*.
// Owner tickets: EXT-2 (auto-compose + overlay + identity), EXT-3 (positive sent-detect),
// EXT-4 (layered selector resolver, skip/manual/pause routing). Frozen contract:
// extension/CONTRACTS.md + extension/shared/constants.js + extension/selectors.json.
//
// NON-NEGOTIABLES (upheld here):
//   1. NEVER clicks Send. `sendButton` is resolved only to highlight + measure its rect for
//      overlay collision-avoidance. No code path issues .click() on it. The human clicks Send.
//   2. This is a CONTENT script (page-lifetime), not the MV3 worker — it may hold transient
//      in-memory state. All DURABLE run-state lives in the worker's chrome.storage. On load it
//      PULLS its assignment (GET_ASSIGNMENT) rather than trusting a push to a dead worker.
//   3. Overlay built with textContent / DOM APIs ONLY — NEVER innerHTML. Queue strings
//      (name/title/company/note) are attacker-influenceable.
//   4. Never-break: any auto-compose failure -> gesture-backed Copy-note fallback (+ manual
//      advance on the gesture) OR a routed skip/pause. Never stuck, never the wrong recipient:
//      the on-page profile name is cross-checked against the intended contact BEFORE we ever
//      show "ready to Send".
//   5. Instructions accepted ONLY over chrome.runtime, and only when sender.id === our id.
//      window.postMessage is ignored entirely (no listener is ever registered for it).
//
// The note is filled VERBATIM from the ApplyPilot draft (contact.note); the content script
// never generates, rewrites, or truncates it. React-safe fill (the load-bearing pattern):
//     const set = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,'value').set;
//     set.call(el, note);
//     el.dispatchEvent(new Event('input', { bubbles: true }));
// (branched by element type below so <input> works too, and contenteditable is a clean
//  NOTE_FIELD_NOT_FOUND hook for EXT-6).
// =============================================================================

(async () => {
  "use strict";

  // constants.js is an ES module; a classic content script loads it via a dynamic import of
  // the extension-packaged URL (must be a web_accessible_resource — manifest/EXT-1). Same file,
  // no string re-typed. selectors.json ships alongside and is the versioned §4 selector table.
  let MSG, STORAGE_KEYS, COMPOSE_FAIL_REASON, RUN_PHASE, NOTE_MAX_LEN;
  let SELECTORS = { version: 0, targets: {} };
  // constants.content.js (a CLASSIC content script) is injected before us by the manifest and
  // sets this global. We do NOT dynamic-import() the module version — LinkedIn's CSP blocks
  // dynamic import in content scripts, which would silently make this whole script inert.
  const C = globalThis.__APPLYPILOT_CONSTANTS__;
  if (!C) {
    console.warn("[ApplyPilot] constants global missing — content script inert");
    return;
  }
  ({ MSG, STORAGE_KEYS, COMPOSE_FAIL_REASON, RUN_PHASE, NOTE_MAX_LEN } = C);
  // selectors.content.js (classic, injected before us) sets this global — no CSP-subject
  // fetch of an extension resource (which LinkedIn can also block).
  if (globalThis.__APPLYPILOT_SELECTORS__ && globalThis.__APPLYPILOT_SELECTORS__.targets) {
    SELECTORS = globalThis.__APPLYPILOT_SELECTORS__;
  } else {
    console.warn("[ApplyPilot] selectors global missing — using empty table");
  }
  const REASON = COMPOSE_FAIL_REASON;
  const log = (...a) => console.debug("[ApplyPilot]", ...a);
  log("content loaded; selectors v" + (SELECTORS.version | 0));

  // ---------------------------------------------------------------------------
  // Transient page-lifetime state (NOT durable run-state; that lives in the worker).
  // ---------------------------------------------------------------------------
  const state = {
    gen: 0,              // generation token; bumped per assignment so stale async flows abort
    contact: null,       // the intended QueueContact for THIS tab (from the worker)
    settings: null,
    contactId: null,
    phase: RUN_PHASE.IDLE,
    preExistingPending: false,
    dialogEl: null,      // the open invite dialog we are composing into
    observer: null,      // sent-detection MutationObserver
    ambTimer: 0,         // ambiguous-close debounce timer
    sendResolved: false, // positive send confirmed for the active contact
    sendBtn: null,       // resolved Send button (highlight + rect only — NEVER clicked)
    note: "",            // verbatim note for the active contact
  };

  // ===========================================================================
  // Messaging — chrome.runtime only; verify sender is OUR extension.
  // ===========================================================================
  function sendBg(type, payload = {}) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage({ type, ...payload }, (resp) => {
          if (chrome.runtime.lastError) return resolve(null);
          resolve(resp);
        });
      } catch (_e) {
        resolve(null);
      }
    });
  }

  chrome.runtime.onMessage.addListener((msg, sender) => {
    // Only accept instructions from our own background/popup. A web page cannot reach this
    // listener (no externally_connectable); still, hard-verify. window.postMessage is ignored
    // by construction (we never listen for it).
    if (!sender || sender.id !== chrome.runtime.id) return;
    if (!msg || typeof msg.type !== "string") return;
    if (msg.type === MSG.ASSIGNMENT) applyAssignment(msg);
    else if (msg.type === MSG.ABORT) standDown("abort");
  });

  // ===========================================================================
  // Layered selector resolver (§4). Returns the first VISIBLE + ENABLED match, trying
  // strategies in array order. `by`: aria-label -> text -> role -> structural.
  // ===========================================================================
  function scopeRoots(scope) {
    if (scope === "dialog") {
      const dialogs = [...document.querySelectorAll('[role="dialog"], .artdeco-modal')];
      const vis = dialogs.filter(isVisible);
      return vis.length ? vis : dialogs;
    }
    if (scope === "actionBar") {
      const sels = [
        ".pvs-profile-actions",
        ".pv-top-card-v2-ctas",
        ".ph5.pb5",
        ".pv-top-card",
        "main section",
      ];
      for (const s of sels) {
        const el = document.querySelector(s);
        if (el) return [el];
      }
    }
    return [document];
  }

  function isVisible(el) {
    if (!el || !el.getClientRects) return false;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== "hidden" && cs.display !== "none" && cs.opacity !== "0";
  }
  function isEnabled(el) {
    return !el.disabled && el.getAttribute("aria-disabled") !== "true";
  }
  function visibleText(el) {
    return (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
  }
  function textMatch(hay, val, match) {
    if (match === "exact") return hay.toLowerCase() === String(val).toLowerCase();
    return hay.toLowerCase().includes(String(val).toLowerCase());
  }

  function candidatesForStrategy(root, strat) {
    const q = (sel) => {
      try { return [...root.querySelectorAll(sel)]; } catch (_e) { return []; }
    };
    switch (strat.by) {
      case "aria-label": {
        const match = strat.match || "contains";
        return q("[aria-label]").filter((el) =>
          textMatch(el.getAttribute("aria-label") || "", strat.value, match)
        );
      }
      case "text": {
        const match = strat.match || "contains";
        return q('button, a, [role="button"], [role="menuitem"]').filter((el) =>
          textMatch(visibleText(el), strat.value, match)
        );
      }
      case "role": {
        const sel = strat.value === "button" ? 'button, [role="button"]' : `[role="${strat.value}"]`;
        return q(sel);
      }
      case "structural":
        return q(strat.value);
      default:
        return [];
    }
  }

  function resolve(targetName) {
    const strategies = (SELECTORS.targets && SELECTORS.targets[targetName]) || [];
    for (const strat of strategies) {
      for (const root of scopeRoots(strat.scope || "document")) {
        for (const el of candidatesForStrategy(root, strat)) {
          if (isVisible(el) && isEnabled(el)) {
            log("resolved", targetName, "via", strat.by, JSON.stringify(strat.value));
            return el;
          }
        }
      }
    }
    return null;
  }

  // ===========================================================================
  // Small helpers
  // ===========================================================================
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  async function waitFor(fn, { timeout = 8000, interval = 150 } = {}) {
    const end = Date.now() + timeout;
    for (;;) {
      let v;
      try { v = fn(); } catch (_e) { v = null; }
      if (v) return v;
      if (Date.now() >= end) return null;
      await sleep(interval);
    }
  }
  function clickEl(el) {
    if (!el) return false;
    el.click();
    return true;
  }
  function currentDialog() {
    const d = [...document.querySelectorAll('[role="dialog"], .artdeco-modal')].filter(isVisible);
    return d[0] || null;
  }

  // On-page profile identity (H3 safety gate).
  function onPageName() {
    const el = document.querySelector("main h1") || document.querySelector("h1");
    const t = el ? visibleText(el) : "";
    // Reject placeholders/skeletons ("?", single chars, non-name junk) so waitFor keeps
    // polling until the real SPA-loaded name renders, instead of matching a placeholder.
    const cleaned = t.replace(/[^\p{L}\p{N}\s'.-]/gu, "").trim();
    if (cleaned.length < 3 || !/\p{L}/u.test(cleaned)) return "";
    return t;
  }
  // A dead / unavailable / walled profile — auto-skip (not a "wrong person" pause).
  function profileUnavailable() {
    const url = location.href.toLowerCase();
    if (/\/(404|authwall|unavailable)|linkedin\.com\/(login|checkpoint)/.test(url)) return true;
    const body = (document.body ? document.body.innerText : "").toLowerCase();
    return (
      body.includes("this page doesn't exist") ||
      body.includes("this page doesn’t exist") ||
      body.includes("page not found") ||
      body.includes("profile unavailable") ||
      body.includes("this profile is not available") ||
      body.includes("go to your feed")   // LinkedIn's 404 CTA
    );
  }
  function nameTokens(s) {
    return String(s || "")
      .toLowerCase()
      .normalize("NFD").replace(/[̀-ͯ]/g, "") // strip diacritics
      .replace(/[^a-z0-9\s]/g, " ")
      .split(/\s+/)
      .filter((t) => t.length >= 2);
  }
  // Intended name must have its first AND last significant token present on the page.
  function identityMatches(intended, page) {
    const ti = nameTokens(intended);
    const tp = nameTokens(page);
    if (!ti.length || !tp.length) return false;
    const first = ti[0];
    const last = ti[ti.length - 1];
    return tp.includes(first) && tp.includes(last);
  }
  // Connection degree from the top card (1st => already connected).
  function profileDegree() {
    const root = document.querySelector("main") || document.body;
    const t = visibleText(root).slice(0, 400);
    const m = t.match(/\b(1st|2nd|3rd)\b/);
    return m ? m[1] : null;
  }
  // LinkedIn limit banners (weekly invite cap / personalized-note quota).
  function scanLimitBanner() {
    const t = (document.body.innerText || "").toLowerCase();
    if (/weekly invitation limit|reached the weekly|you're out of invitations for now/.test(t)) return "weekly";
    if (/no longer add a note|reached the maximum number of personalized|personalize your invitations/.test(t)) return "note_quota";
    return null;
  }

  // ===========================================================================
  // React-safe fill — branched by element type. contenteditable is intentionally a
  // NOTE_FIELD_NOT_FOUND hook (EXT-6 owns the rich-composer insertion path).
  // ===========================================================================
  function reactSafeFill(el, value) {
    let proto = null;
    if (el instanceof HTMLTextAreaElement) proto = window.HTMLTextAreaElement.prototype;
    else if (el instanceof HTMLInputElement) proto = window.HTMLInputElement.prototype;
    if (!proto) return false; // contenteditable / unknown -> caller treats as NOTE_FIELD_NOT_FOUND
    const set = Object.getOwnPropertyDescriptor(proto, "value").set;
    el.focus();
    set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }

  // ===========================================================================
  // Assignment handling (pull model). applyAssignment runs for both the GET_ASSIGNMENT
  // response and any pushed re-arm.
  // ===========================================================================
  async function pullAssignment() {
    // We can't reliably know our own tabId; the background authority is sender.tab.id and
    // payload.tabId is a best-effort cross-check only (send null).
    const resp = await sendBg(MSG.GET_ASSIGNMENT, { tabId: null });
    if (resp && resp.type === MSG.ASSIGNMENT) applyAssignment(resp);
    else if (resp && ("contact" in resp)) applyAssignment(resp);
  }

  function applyAssignment(a) {
    if (a.settings) state.settings = a.settings;
    // Inert unless a live queue hands us a concrete contact for THIS tab.
    if (!a.running || !a.contact) return standDown("no-assignment");
    const c = a.contact;
    // Idempotent: if we're already handling this contact, don't restart the flow.
    if (state.contactId === c.id && state.phase !== RUN_PHASE.IDLE) return;
    beginContact(c);
  }

  function standDown(_why) {
    state.gen++; // abort any in-flight async compose
    teardownObserver();
    clearHighlight();
    removeOverlay();
    state.contact = null;
    state.contactId = null;
    state.phase = RUN_PHASE.IDLE;
    state.sendResolved = false;
    state.dialogEl = null;
  }

  function beginContact(contact) {
    const gen = ++state.gen;
    teardownObserver();
    clearHighlight();
    state.contact = contact;
    state.contactId = contact.id;
    state.note = typeof contact.note === "string" ? contact.note.slice(0, NOTE_MAX_LEN) : "";
    state.preExistingPending = false;
    state.sendResolved = false;
    state.dialogEl = null;
    state.phase = RUN_PHASE.COMPOSING;
    runCompose(contact, gen).catch((e) => {
      if (gen !== state.gen) return;
      log("compose threw", e);
      fallbackManual(REASON.UNKNOWN, contact);
    });
  }

  // ===========================================================================
  // Compose sequence (deterministic; layered selectors). Never clicks Send.
  // ===========================================================================
  async function runCompose(contact, gen) {
    const alive = () => gen === state.gen;

    // 0) Wait for the profile shell (the real name) to render — or a 404 to appear.
    await waitFor(() => onPageName() || profileUnavailable(), { timeout: 10000 });
    if (!alive()) return;

    // 0a) Dead/stale/walled URL (common for guessed contact URLs) => AUTO-SKIP + advance,
    //     never stall the whole queue on a bad link. Not a "wrong person" event.
    if (profileUnavailable() || !onPageName()) {
      log("profile unavailable / name unreadable — skipping", contact.full_name);
      renderOverlay({
        mode: "skip",
        heading: "Profile unavailable — skipping",
        detail: `Couldn't open ${contact.full_name}'s profile (dead or private link).`,
      });
      sendBg(MSG.SKIP_CONTACT, { contactId: contact.id, reason: REASON.PROFILE_404 });
      return;
    }

    // 1) IDENTITY GATE — a real, DIFFERENT name is on the page => pause for the human
    //    (genuine safety event; do not auto-skip a real person).
    const pageName = onPageName();
    if (!identityMatches(contact.full_name, pageName)) {
      log("identity mismatch: intended", contact.full_name, "on-page", pageName);
      renderOverlay({
        mode: "mismatch",
        heading: "Wrong profile — not composing",
        detail: `Expected ${contact.full_name}, this page is ${pageName}.`,
      });
      sendBg(MSG.IDENTITY_MISMATCH, { contactId: contact.id, onPageName: pageName });
      state.phase = RUN_PHASE.PAUSED;
      return;
    }

    // 2) Pre-existing Pending => already invited => skip (never compose, never false-mark sent).
    const pendingNow = resolve("pendingBadge");
    if (pendingNow) {
      state.preExistingPending = true;
      renderOverlay({ mode: "info", heading: "Already invited", detail: "Skipping — invitation already pending.", contact });
      sendBg(MSG.SKIP_CONTACT, { contactId: contact.id, reason: REASON.PENDING_ALREADY });
      return;
    }

    // Show the "composing" overlay (recipient identity is visible from now on).
    renderOverlay({ mode: "composing", heading: "Preparing invitation…", contact });

    // 3) Dismiss any promo/interstitial modal (Escape + a close target) before acting.
    const promo = resolve("dismissModal");
    if (promo) {
      clickEl(promo);
      await sleep(250);
    } else {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      await sleep(120);
    }
    if (!alive()) return;

    // 4) Open the Connect invitation — directly, else via More -> Connect.
    let connect = resolve("connectButton");
    if (!connect) {
      const more = resolve("moreButton");
      if (more) {
        clickEl(more);
        connect = await waitFor(() => resolve("connectMenuItem"), { timeout: 4000 });
      }
    }
    if (!alive()) return;
    if (!connect) {
      // Could not find Connect. Disambiguate the common terminal states.
      if (resolve("pendingBadge")) {
        sendBg(MSG.SKIP_CONTACT, { contactId: contact.id, reason: REASON.PENDING_ALREADY });
        return;
      }
      if (profileDegree() === "1st") {
        renderOverlay({ mode: "info", heading: "Already connected", detail: "1st-degree — skipping the invite.", contact });
        sendBg(MSG.SKIP_CONTACT, { contactId: contact.id, reason: REASON.ALREADY_CONNECTED });
        return;
      }
      // Otherwise ambiguous (InMail-only / unusual layout): never-break paste fallback.
      return fallbackManual(REASON.NO_CONNECT_BUTTON, contact);
    }
    clickEl(connect);

    // 5) Wait for the invite dialog, then click "Add a note".
    const dialog = await waitFor(() => currentDialog(), { timeout: 5000 });
    if (!alive()) return;
    if (!dialog) return fallbackManual(REASON.NO_CONNECT_BUTTON, contact);
    state.dialogEl = dialog;

    // A limit banner can appear right here (weekly cap / note quota) -> pause distinctly.
    const banner1 = scanLimitBanner();
    if (banner1 === "weekly") { pauseLimit(contact, "weekly_invite", REASON.WEEKLY_LIMIT); return; }
    if (banner1 === "note_quota") { pauseLimit(contact, "note_quota", REASON.NOTE_QUOTA_REACHED); return; }

    const addNote = await waitFor(() => resolve("addNoteButton"), { timeout: 3000 });
    if (!alive()) return;
    if (!addNote) {
      // No "Add a note" affordance is the classic free-account note-quota state.
      if (scanLimitBanner() === "note_quota") { pauseLimit(contact, "note_quota", REASON.NOTE_QUOTA_REACHED); return; }
      pauseLimit(contact, "note_quota", REASON.NOTE_QUOTA_REACHED);
      return;
    }
    clickEl(addNote);

    // 6) Focus the note TEXTAREA (scoped to the dialog — never the top-nav search box) + fill.
    const field = await waitFor(() => resolve("noteTextarea"), { timeout: 4000 });
    if (!alive()) return;
    if (!field) return fallbackManual(REASON.NOTE_FIELD_NOT_FOUND, contact);

    // Confirm the field is a real <textarea>/<input> (LinkedIn A/B-tests a contenteditable).
    const isFillable = field instanceof HTMLTextAreaElement || field instanceof HTMLInputElement;
    if (!isFillable || field.isContentEditable) {
      return fallbackManual(REASON.NOTE_FIELD_NOT_FOUND, contact); // EXT-6 contenteditable hook
    }

    const filled = reactSafeFill(field, state.note);
    if (!filled) return fallbackManual(REASON.NOTE_FIELD_NOT_FOUND, contact);

    // 7) Verify VERBATIM: exact length + content.
    await sleep(80);
    if (!alive()) return;
    if (field.value !== state.note || field.value.length !== state.note.length) {
      log("fill verify failed", { got: field.value.length, want: state.note.length });
      return fallbackManual(REASON.FILL_VERIFY_FAILED, contact);
    }

    // 8) Locate Send ONLY to highlight + measure (collision-avoid). NEVER clicked.
    state.sendBtn = resolve("sendButton");
    highlightSend(state.sendBtn);

    // 9) Ready to send — hand off to the human.
    state.phase = RUN_PHASE.READY_TO_SEND;
    renderOverlay({ mode: "ready", heading: "Note filled — review, then click Send", contact });
    sendBg(MSG.COMPOSE_RESULT, { contactId: contact.id, ok: true, reason: REASON.OK });

    // 10) Arm positive sent-detection.
    armSentDetection(contact, gen);
  }

  // ===========================================================================
  // Routed failure helpers
  // ===========================================================================
  function pauseLimit(contact, kind, reason) {
    state.phase = RUN_PHASE.PAUSED;
    renderOverlay({
      mode: "limit",
      heading: kind === "weekly_invite" ? "Weekly invite limit reached" : "Personalized-note limit reached",
      detail: kind === "weekly_invite" ? "Queue paused — resumes next week." : "LinkedIn won't let you add a note right now.",
      contact,
    });
    sendBg(MSG.LIMIT_BANNER, { contactId: contact.id, kind });
    // reason retained for COMPOSE_FAIL_REASON parity / logs
    log("limit banner", kind, reason);
  }

  // Never-break fallback: gesture-backed Copy note. We do NOT auto-mark manual — marking
  // `manual` counts as a real human-sent invite (cap + dedupe), so it only fires when the
  // human actually uses the Copy gesture (transient activation) to paste + send by hand.
  function fallbackManual(reason, contact) {
    state.phase = RUN_PHASE.PAUSED;
    teardownObserver();
    clearHighlight();
    renderOverlay({
      mode: "fallback",
      heading: "Couldn't auto-open the invite",
      detail: "Copy your note and paste it manually, then Send. Or Skip.",
      contact,
      reason,
    });
  }

  // ===========================================================================
  // Positive sent-detection (EXT-3). POSITIVE signal only:
  //   (a) a sent toast/aria-live announcement, OR
  //   (b) the invite dialog closes AND a NEW Pending badge appears (absent at compose start).
  // Pre-existing Pending is handled as skip earlier. Ambiguous close (cancel) => NOT sent.
  // ===========================================================================
  function sentToastPresent() {
    const live = [...document.querySelectorAll('[role="alert"], .artdeco-toast-item, [aria-live="assertive"], [aria-live="polite"]')];
    return live.some((el) => {
      if (!isVisible(el)) return false;
      const t = visibleText(el).toLowerCase();
      return /invitation sent|invite sent|sent your invitation|invitation to connect was sent/.test(t);
    });
  }
  function newPendingPresent() {
    return !state.preExistingPending && !!resolve("pendingBadge");
  }

  function armSentDetection(contact, gen) {
    teardownObserver();
    const obs = new MutationObserver(() => {
      if (gen !== state.gen || state.sendResolved) return;

      // (a) explicit positive toast
      if (sentToastPresent()) return confirmSend(contact);

      const dialogGone = !state.dialogEl || !document.contains(state.dialogEl) || !isVisible(state.dialogEl);
      if (dialogGone) {
        // (b) dialog closed + a NEW pending badge => sent.
        if (newPendingPresent()) return confirmSend(contact);
        // Ambiguous close (likely Cancel): debounce, then treat as interrupted (NOT sent).
        if (!state.ambTimer) {
          state.ambTimer = setTimeout(() => {
            state.ambTimer = 0;
            if (gen !== state.gen || state.sendResolved) return;
            if (sentToastPresent() || newPendingPresent()) return confirmSend(contact);
            ambiguousClose(contact);
          }, 1500);
        }
      }
    });
    obs.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["aria-label", "class"] });
    state.observer = obs;
  }

  function confirmSend(contact) {
    if (state.sendResolved) return;
    state.sendResolved = true;
    teardownObserver();
    clearHighlight();
    state.phase = RUN_PHASE.DONE;
    renderOverlay({ mode: "sent", heading: "Invitation sent ✓", contact });
    sendBg(MSG.SEND_DETECTED, { contactId: contact.id });
  }

  // Dialog closed without a positive signal: leave status unchanged (do NOT mark sent, do NOT
  // skip). Offer the human a re-open / copy / skip path so the queue is never in a wrong state.
  function ambiguousClose(contact) {
    if (state.sendResolved) return;
    teardownObserver();
    clearHighlight();
    state.phase = RUN_PHASE.PAUSED;
    renderOverlay({
      mode: "fallback",
      heading: "Invite closed without sending",
      detail: "Re-open Connect to retry, copy your note to paste manually, or Skip.",
      contact,
      reason: REASON.UNKNOWN,
    });
  }

  function teardownObserver() {
    if (state.observer) { state.observer.disconnect(); state.observer = null; }
    if (state.ambTimer) { clearTimeout(state.ambTimer); state.ambTimer = 0; }
  }

  // ===========================================================================
  // Send highlight (measure + outline the real Send button; never clicks it).
  // ===========================================================================
  const HILITE_ID = "applypilot-send-hilite";
  function highlightSend(el) {
    clearHighlight();
    if (!el) return;
    el.setAttribute("data-applypilot-send", "1");
    el.style.outline = "3px solid #4f46e5";
    el.style.outlineOffset = "2px";
    el.style.borderRadius = getComputedStyle(el).borderRadius || "4px";
    positionOverlayAwayFrom(el.getBoundingClientRect());
  }
  function clearHighlight() {
    const el = document.querySelector('[data-applypilot-send="1"]');
    if (el) {
      el.removeAttribute("data-applypilot-send");
      el.style.outline = "";
      el.style.outlineOffset = "";
    }
  }

  // ===========================================================================
  // Overlay — textContent / DOM APIs ONLY. NEVER innerHTML. Anchored to a viewport corner,
  // engineered to never cover the Send button (measure Send's live rect, pick a free corner,
  // container pointer-events:none so stray overlap passes clicks through; buttons re-enable).
  // ===========================================================================
  const OVERLAY_ID = "applypilot-overlay";
  let els = null; // cached overlay element refs

  function ensureOverlay() {
    let root = document.getElementById(OVERLAY_ID);
    if (root && els) return els;
    if (root) root.remove();

    root = document.createElement("div");
    root.id = OVERLAY_ID;
    Object.assign(root.style, {
      position: "fixed",
      zIndex: "2147483646",
      maxWidth: "320px",
      width: "300px",
      bottom: "20px",
      right: "20px",
      background: "#111827",
      color: "#f9fafb",
      font: "13px/1.45 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif",
      borderRadius: "12px",
      boxShadow: "0 10px 30px rgba(0,0,0,.35)",
      padding: "14px 14px 12px",
      pointerEvents: "none", // clicks pass through if this ever overlaps Send
    });

    const brand = document.createElement("div");
    Object.assign(brand.style, { fontSize: "11px", letterSpacing: ".04em", textTransform: "uppercase", color: "#a5b4fc", marginBottom: "6px" });
    brand.textContent = "ApplyPilot";

    const heading = document.createElement("div");
    Object.assign(heading.style, { fontWeight: "600", fontSize: "14px", marginBottom: "6px" });

    const recipient = document.createElement("div");
    Object.assign(recipient.style, { fontSize: "12px", color: "#d1d5db", marginBottom: "6px" });

    const detail = document.createElement("div");
    Object.assign(detail.style, { fontSize: "12px", color: "#9ca3af", marginBottom: "10px" });

    const progress = document.createElement("div");
    Object.assign(progress.style, { fontSize: "11px", color: "#6b7280", marginBottom: "10px" });

    const btnRow = document.createElement("div");
    Object.assign(btnRow.style, { display: "flex", gap: "8px", flexWrap: "wrap", pointerEvents: "auto" });

    const mkBtn = (label, bg) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      Object.assign(b.style, {
        pointerEvents: "auto",
        cursor: "pointer",
        border: "0",
        borderRadius: "8px",
        padding: "6px 10px",
        fontSize: "12px",
        fontWeight: "600",
        color: "#fff",
        background: bg,
      });
      return b;
    };
    const copyBtn = mkBtn("Copy note", "#4f46e5");
    const skipBtn = mkBtn("Skip", "#374151");
    const pauseBtn = mkBtn("Pause", "#374151");

    btnRow.append(copyBtn, skipBtn, pauseBtn);
    root.append(brand, heading, recipient, detail, progress, btnRow);
    document.body.appendChild(root);

    els = { root, heading, recipient, detail, progress, btnRow, copyBtn, skipBtn, pauseBtn };
    return els;
  }

  function renderOverlay(opts) {
    const e = ensureOverlay();
    const c = opts.contact || state.contact;
    e.heading.textContent = opts.heading || "";
    e.recipient.textContent = c ? `Inviting: ${c.full_name} — ${c.title || "?"} at ${c.company || "?"}` : "";
    e.detail.textContent = opts.detail || "";

    // Copy note: gesture-backed. Only claim "copied" after writeText resolves. In the fallback
    // modes, a successful copy is the human's transient-activation gesture to advance as `manual`.
    const advanceOnCopy = opts.mode === "fallback";
    e.copyBtn.style.display = state.note ? "inline-block" : "none";
    e.copyBtn.textContent = "Copy note";
    e.copyBtn.onclick = () => {
      const note = state.note;
      navigator.clipboard.writeText(note).then(
        () => {
          e.copyBtn.textContent = "Copied ✓";
          if (advanceOnCopy && c) {
            sendBg(MSG.FALLBACK_MANUAL, { contactId: c.id, reason: opts.reason || REASON.UNKNOWN });
          }
        },
        () => { e.copyBtn.textContent = "Copy failed"; }
      );
    };

    e.skipBtn.onclick = () => { if (c) sendBg(MSG.OVERLAY_SKIP, { contactId: c.id }); };

    // Pause / Resume reflect stored PAUSED (set below via storage read + onChanged).
    e.pauseBtn.onclick = () => {
      if (e.pauseBtn.dataset.paused === "1") sendBg(MSG.OVERLAY_RESUME, {});
      else sendBg(MSG.OVERLAY_PAUSE, {});
    };

    refreshOverlayFromStorage();
    // Keep clear of Send if we know its rect.
    if (state.sendBtn && document.contains(state.sendBtn)) {
      positionOverlayAwayFrom(state.sendBtn.getBoundingClientRect());
    }
  }

  function removeOverlay() {
    const root = document.getElementById(OVERLAY_ID);
    if (root) root.remove();
    els = null;
  }

  // Collision-avoidance: try corners in order; pick the first whose overlay rect does not
  // intersect the Send rect (expanded by a margin). Container stays pointer-events:none.
  function positionOverlayAwayFrom(sendRect) {
    if (!els) return;
    const root = els.root;
    const margin = 16;
    const w = root.offsetWidth || 300;
    const h = root.offsetHeight || 160;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const corners = [
      { bottom: 20, right: 20, x: vw - 20 - w, y: vh - 20 - h },
      { top: 20, right: 20, x: vw - 20 - w, y: 20 },
      { bottom: 20, left: 20, x: 20, y: vh - 20 - h },
      { top: 20, left: 20, x: 20, y: 20 },
    ];
    const intersects = (x, y) =>
      sendRect &&
      x < sendRect.right + margin &&
      x + w > sendRect.left - margin &&
      y < sendRect.bottom + margin &&
      y + h > sendRect.top - margin;
    const pick = corners.find((c) => !intersects(c.x, c.y)) || corners[0];
    root.style.top = pick.top != null ? pick.top + "px" : "";
    root.style.bottom = pick.bottom != null ? pick.bottom + "px" : "";
    root.style.left = pick.left != null ? pick.left + "px" : "";
    root.style.right = pick.right != null ? pick.right + "px" : "";
  }

  // ---------------------------------------------------------------------------
  // Live overlay data from chrome.storage (progress, paused) — the running loop must not
  // depend on the popup, so progress/Pause live here and update via onChanged.
  // ---------------------------------------------------------------------------
  function refreshOverlayFromStorage() {
    try {
      chrome.storage.local.get([STORAGE_KEYS.PROGRESS, STORAGE_KEYS.PAUSED], (s) => {
        if (chrome.runtime.lastError || !els) return;
        const p = s[STORAGE_KEYS.PROGRESS];
        els.progress.textContent = p ? `Progress: ${p.sent} / ${p.total}` : "";
        const paused = !!s[STORAGE_KEYS.PAUSED];
        els.pauseBtn.dataset.paused = paused ? "1" : "0";
        els.pauseBtn.textContent = paused ? "Resume" : "Pause";
      });
    } catch (_e) { /* no-op */ }
  }
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local" || !els) return;
    if (changes[STORAGE_KEYS.PROGRESS] || changes[STORAGE_KEYS.PAUSED]) refreshOverlayFromStorage();
  });

  window.addEventListener("resize", () => {
    if (els && state.sendBtn && document.contains(state.sendBtn)) {
      positionOverlayAwayFrom(state.sendBtn.getBoundingClientRect());
    }
  });

  // ===========================================================================
  // Boot: pull the assignment for this tab. If none / queue inactive, stay inert.
  // ===========================================================================
  pullAssignment();
})();

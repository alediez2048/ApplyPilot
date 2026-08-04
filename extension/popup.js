// ApplyPilot Contacts — dead-simple popup.
// Four jobs, nothing else:
//   1. Pull contacts from the latest ApplyPilot run  (GET /api/ext/queue)
//   2. Copy the drafted outreach note                (clipboard)
//   3. Open the right LinkedIn profile               (new tab)
//   4. Read a LinkedIn thread you already have open and log it  (activeTab, on click)
//
// No content script, no background worker, no LinkedIn DOM AUTOMATION — (4) reads a page that
// is already on screen and never clicks, types or sends. The popup fetches the contact list
// from the local dashboard (host_permissions bypasses CORS) and renders it. All contact text
// and every message read off LinkedIn is inserted via textContent — never innerHTML — so a
// malicious name, note or message can't inject markup.

"use strict";

const API_BASE = "http://localhost:8765";
const QUEUE_URL = API_BASE + "/api/ext/queue?include_skipped=1"; // include_skipped: never hide a contact
const TOKEN_HEADER = "X-ApplyPilot-Token";
const TOKEN_KEY = "applypilot_token";

const MATCH_URL = API_BASE + "/api/ext/match";
const MESSAGES_URL = API_BASE + "/api/ext/messages";

const $ = (id) => document.getElementById(id);
const el = {
  conn: $("conn"),
  setup: $("setup"),
  setupErr: $("setup-err"),
  token: $("token"),
  tokenSave: $("token-save"),
  main: $("main"),
  refresh: $("refresh"),
  settingsToggle: $("settings-toggle"),
  count: $("count"),
  list: $("list"),
  empty: $("empty"),
  thread: $("thread"),
  threadRead: $("thread-read"),
  threadStatus: $("thread-status"),
  threadWho: $("thread-who"),
  threadContact: $("thread-contact"),
  threadMsgs: $("thread-msgs"),
  threadSave: $("thread-save"),
};

// ---- token storage -------------------------------------------------------
function getToken() {
  // Also read the OLD extension's key ("extToken") so an existing token carries over — the user
  // shouldn't have to re-paste after the rebuild.
  return new Promise((res) => {
    try {
      chrome.storage.local.get([TOKEN_KEY, "extToken"], (s) =>
        res((s && (s[TOKEN_KEY] || s.extToken)) || "")
      );
    } catch (_e) {
      res("");
    }
  });
}
function saveToken(tok) {
  return new Promise((res) => {
    try {
      chrome.storage.local.set({ [TOKEN_KEY]: tok }, () => res());
    } catch (_e) {
      res();
    }
  });
}

// ---- connection state ----------------------------------------------------
function setConn(ok) {
  el.conn.textContent = ok ? "Connected" : "Not connected";
  el.conn.className = "conn " + (ok ? "conn--on" : "conn--off");
}
function showSetup(errMsg) {
  el.setup.hidden = false;
  el.main.hidden = true;
  if (errMsg) {
    el.setupErr.textContent = errMsg;
    el.setupErr.hidden = false;
  } else {
    el.setupErr.hidden = true;
  }
}
function showMain() {
  el.setup.hidden = true;
  el.main.hidden = false;
}

// ---- fetch the contact queue --------------------------------------------
async function fetchContacts(token) {
  const res = await fetch(QUEUE_URL, { method: "GET", headers: { [TOKEN_HEADER]: token } });
  if (res.status === 401) throw new Error("Token rejected — paste the token shown in the dashboard.");
  if (!res.ok) throw new Error("HTTP " + res.status);
  const data = await res.json();
  if (!data || data.ok === false) throw new Error((data && data.error) || "bad response");
  return Array.isArray(data.contacts) ? data.contacts : [];
}

// ---- render --------------------------------------------------------------
function initials(name) {
  const p = String(name || "").trim().split(/\s+/).filter(Boolean);
  if (!p.length) return "?";
  return ((p[0][0] || "") + (p.length > 1 ? p[p.length - 1][0] : "")).toUpperCase();
}
const AVATAR_COLORS = ["#0a66c2", "#057642", "#915907", "#7a3e9d", "#0e7490", "#b45309", "#9f1239", "#3730a3"];
function avatarColor(name) {
  let h = 0;
  const s = String(name || "");
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}

function makeCard(c) {
  const card = document.createElement("div");
  card.className = "card";

  const top = document.createElement("div");
  top.className = "card-top";

  const av = document.createElement("div");
  av.className = "avatar";
  av.style.background = avatarColor(c.full_name);
  av.textContent = initials(c.full_name);

  const who = document.createElement("div");
  who.className = "who";
  const nm = document.createElement("div");
  nm.className = "name";
  nm.textContent = c.full_name || "(no name)";
  const sub = document.createElement("div");
  sub.className = "sub";
  sub.textContent = [c.title, c.company].filter(Boolean).join(" · ");
  who.append(nm, sub);
  top.append(av, who);

  // The drafted note (editable so you can tweak before copying).
  const note = document.createElement("textarea");
  note.className = "note";
  note.rows = 3;
  note.value = c.note || "";
  note.placeholder = "No note drafted yet — draft it in the dashboard.";

  const actions = document.createElement("div");
  actions.className = "actions";

  const copyBtn = document.createElement("button");
  copyBtn.className = "btn btn--primary";
  copyBtn.textContent = "Copy note";
  copyBtn.disabled = !note.value.trim();
  copyBtn.addEventListener("click", () => {
    navigator.clipboard.writeText(note.value).then(
      () => {
        copyBtn.textContent = "Copied ✓";
        setTimeout(() => (copyBtn.textContent = "Copy note"), 1400);
      },
      () => {
        copyBtn.textContent = "Copy failed";
        setTimeout(() => (copyBtn.textContent = "Copy note"), 1400);
      }
    );
  });
  note.addEventListener("input", () => {
    copyBtn.disabled = !note.value.trim();
  });

  const liBtn = document.createElement("button");
  const hasLi = /^https:\/\/([a-z]+\.)?linkedin\.com\/in\//i.test(c.linkedin_url || "");
  liBtn.className = "btn " + (hasLi ? "btn--li" : "btn--ghost");
  liBtn.textContent = "Open LinkedIn ↗";
  liBtn.disabled = !hasLi;
  liBtn.title = hasLi ? c.linkedin_url : "No LinkedIn URL for this contact";
  liBtn.addEventListener("click", () => {
    if (hasLi) window.open(c.linkedin_url, "_blank", "noopener");
  });

  actions.append(copyBtn, liBtn);
  card.append(top, note, actions);
  return card;
}

function render(contacts) {
  el.list.replaceChildren();
  el.count.textContent = contacts.length
    ? `${contacts.length} contact${contacts.length > 1 ? "s" : ""}`
    : "";
  el.empty.hidden = contacts.length > 0;

  // Group by company for readability.
  const byCompany = new Map();
  for (const c of contacts) {
    const key = c.company || "Other";
    if (!byCompany.has(key)) byCompany.set(key, []);
    byCompany.get(key).push(c);
  }
  for (const [company, group] of byCompany) {
    const h = document.createElement("div");
    h.className = "group-h";
    h.textContent = `${company} (${group.length})`;
    el.list.append(h);
    for (const c of group) el.list.append(makeCard(c));
  }
}

// ---- reading an open LinkedIn thread -------------------------------------
// Nothing below runs on its own. `activeTab` is granted by Chrome only for the tab that was
// active when the icon was clicked, and only for as long as this popup is open — there is
// still no content script, no background worker and no linkedin.com host permission, so the
// extension cannot touch a page unless you click. It reads; it never clicks, types or sends.

let THREAD = null;        // the last read: { messages, senders, headerName }
let CANDIDATES = [];      // contacts the sender name could be
let ALL_CONTACTS = [];    // everyone, for when the name matched nobody

function threadSay(msg, bad) {
  el.threadStatus.hidden = !msg;
  el.threadStatus.textContent = msg || "";
  el.threadStatus.className = "thread-status" + (bad ? " thread-status--bad" : "");
}

function activeTab() {
  return new Promise((res) => {
    try {
      chrome.tabs.query({ active: true, currentWindow: true }, (t) => res((t && t[0]) || null));
    } catch (_e) {
      res(null);
    }
  });
}

const isThreadUrl = (u) => /^https:\/\/([a-z]+\.)?linkedin\.com\/messaging\//i.test(u || "");

// Show the control only where it works. Everywhere else it is absent, not disabled-and-silent.
async function syncThreadPanel() {
  const tab = await activeTab();
  el.thread.hidden = !(tab && isThreadUrl(tab.url));
}

async function readThread() {
  const tab = await activeTab();
  if (!tab || !isThreadUrl(tab.url)) {
    threadSay("Open a LinkedIn conversation in this tab first.", true);
    return;
  }
  el.threadRead.disabled = true;
  threadSay("Reading…");
  let found;
  try {
    const res = await chrome.scripting.executeScript({
      target: { tabId: tab.id }, func: readLinkedInThread,
    });
    found = res && res[0] && res[0].result;
  } catch (e) {
    // Missing permission, a page Chrome refuses to inject into, or a thrown parser. All of
    // them are "you got nothing", and saying so is the whole point — a selector-driven read
    // that fails quietly is indistinguishable from an empty conversation.
    threadSay("Couldn't read the page: " + (e && e.message ? e.message : e), true);
    el.threadRead.disabled = false;
    return;
  }
  el.threadRead.disabled = false;

  if (!found || !found.ok) {
    threadSay((found && found.error) || "Couldn't read this conversation.", true);
    return;
  }
  if (!found.messages.length) {
    threadSay("Read the page but found no messages — LinkedIn may have changed its markup.", true);
    return;
  }
  THREAD = found;

  // Who is the other person? The thread header names them; the senders are the fallback.
  // Whoever is NOT us appears in `senders`, but we cannot tell which is which yet — that is
  // exactly what the contact match decides.
  const guess = found.headerName || found.senders[0] || "";
  threadSay(`${found.messages.length} message${found.messages.length > 1 ? "s" : ""} read.`);
  await offerContacts(guess, found.senders);
}

async function offerContacts(guess, senders) {
  const token = await getToken();
  const tried = [];
  CANDIDATES = [];
  // Try the header name, then each distinct sender. One of the senders is you, and yours
  // simply matches no contact — which costs one request and needs no idea of your own name.
  for (const name of [guess].concat(senders || []).filter(Boolean)) {
    if (tried.indexOf(name) >= 0) continue;
    tried.push(name);
    try {
      const res = await fetch(MATCH_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json", [TOKEN_HEADER]: token },
        body: JSON.stringify({ name }),
      });
      const data = await res.json();
      if (data && data.ok) {
        ALL_CONTACTS = data.all || ALL_CONTACTS;
        if (data.candidates.length) { CANDIDATES = data.candidates; break; }
      }
    } catch (_e) { /* fall through to the next name */ }
  }

  el.threadContact.replaceChildren();
  // Two contact rows for the same person under the same job exist on the live board (Marcus at
  // webAI). They render identically here, and an ambiguous pick is a message filed against a
  // half-empty duplicate. Show that there are two rather than hiding one.
  const label = (c, all) => {
    const base = c.full_name + (c.company ? " · " + c.company : "");
    const twins = all.filter((o) => o.full_name === c.full_name && o.company === c.company);
    return base + (twins.length > 1 ? "  #" + String(c.id).slice(-4) : "");
  };
  if (!CANDIDATES.length) {
    // Everyone we know is offered, so an unmatched name is still loggable. The alternative is
    // a dead end whose only exit is the dashboard, which is the flow this replaces.
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "No contact matched — pick one below";
    el.threadContact.append(opt);
    for (const c of ALL_CONTACTS) {
      const o = document.createElement("option");
      o.value = c.id;
      o.textContent = label(c, ALL_CONTACTS);
      el.threadContact.append(o);
    }
  } else {
    for (const c of CANDIDATES) {
      const o = document.createElement("option");
      o.value = c.id;
      o.textContent = label(c, CANDIDATES)
        + (c.match_basis === "first-name" ? "  (first name only)" : "");
      el.threadContact.append(o);
    }
    // Most stored contacts have no surname — Apollo's people search redacts it — so this is
    // the common case, not an edge one. It must not look the same as a full-name match.
    if (CANDIDATES.every((c) => c.match_basis === "first-name")) {
      threadSay("Matched on a first name only — check this is the right person.", true);
    }
  }
  el.threadWho.hidden = false;
  renderThreadMessages();
}

// Every message, with its direction and the timestamp it will actually be stored under.
// Showing the resolved time is not decoration: LinkedIn has no machine-readable timestamp, so
// this is a derived value, and a derived value the operator cannot see is one they cannot
// correct (§Lessons 29 — the dangerous half of a feature is the half that looks identical
// when it is wrong).
function renderThreadMessages() {
  el.threadMsgs.replaceChildren();
  if (!THREAD) return;
  const picked = CANDIDATES.concat(ALL_CONTACTS).find((c) => c.id === el.threadContact.value);
  const contactName = (picked && picked.full_name) || "";

  THREAD.messages.forEach((m) => {
    if (m.dir === undefined || m.dirAuto) {
      m.dir = linkedInDirection(m.sender, contactName, m.markedOther);
      m.dirAuto = true;
    }
    const row = document.createElement("div");
    row.className = "msg msg--" + (m.dir === "linkedin_in" ? "in" : "out");

    const head = document.createElement("div");
    head.className = "msg-head";

    const flip = document.createElement("button");
    flip.className = "msg-dir";
    flip.textContent = m.dir === "linkedin_in" ? "← them" : "→ you";
    flip.title = "Click to flip the direction";
    flip.addEventListener("click", () => {
      m.dir = m.dir === "linkedin_in" ? "linkedin_out" : "linkedin_in";
      m.dirAuto = false;   // an operator decision is never re-guessed on the next render
      renderThreadMessages();
    });

    const when = document.createElement("span");
    when.className = "msg-when";
    when.textContent = m.at
      ? new Date(m.at).toLocaleString()
      : (m.timeLabel || "") + " — time unreadable, will be stored without one";
    if (!m.at) when.className += " msg-when--bad";

    head.append(flip, when);

    const body = document.createElement("div");
    body.className = "msg-text";
    body.textContent = m.text;      // textContent, never innerHTML

    const skip = document.createElement("label");
    skip.className = "msg-skip";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = m.skip === true;
    box.addEventListener("change", () => { m.skip = box.checked; renderThreadMessages(); });
    skip.append(box, document.createTextNode(" skip"));
    head.append(skip);

    row.append(head, body);
    if (m.skip) row.className += " msg--skipped";
    el.threadMsgs.append(row);
  });
  el.threadSave.hidden = false;
  const n = THREAD.messages.filter((m) => !m.skip).length;
  el.threadSave.disabled = !n || !el.threadContact.value;
  el.threadSave.textContent = n ? `Log ${n} message${n > 1 ? "s" : ""}` : "Nothing selected";
}

async function saveThread() {
  if (!THREAD || !el.threadContact.value) return;
  const messages = THREAD.messages
    .filter((m) => !m.skip)
    .map((m) => ({ kind: m.dir, detail: m.text, at: m.at || "" }));
  if (!messages.length) return;
  el.threadSave.disabled = true;
  threadSay("Saving…");
  try {
    const token = await getToken();
    const res = await fetch(MESSAGES_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json", [TOKEN_HEADER]: token },
      body: JSON.stringify({ contact_id: el.threadContact.value, messages }),
    });
    const data = await res.json();
    if (!data || !data.ok) {
      threadSay((data && data.error) || "Save failed.", true);
      el.threadSave.disabled = false;
      return;
    }
    // "Already recorded" is reported, not hidden: re-reading a thread you have logged before
    // is the normal case, and a silent success there looks identical to a duplicate write.
    const bits = [];
    if (data.stored) bits.push(`${data.stored} logged`);
    if (data.already) bits.push(`${data.already} already recorded`);
    threadSay(bits.join(", ") + ".");
    el.threadSave.hidden = true;
    el.threadMsgs.replaceChildren();
    THREAD = null;
  } catch (e) {
    threadSay("Save failed: " + (e && e.message ? e.message : e), true);
    el.threadSave.disabled = false;
  }
}

// ---- load flow -----------------------------------------------------------
async function load() {
  const token = await getToken();
  if (!token) {
    setConn(false);
    showSetup();
    return;
  }
  el.count.textContent = "Loading…";
  try {
    const contacts = await fetchContacts(token);
    setConn(true);
    showMain();
    render(contacts);
    await syncThreadPanel();
  } catch (e) {
    setConn(false);
    // If the token is bad, drop back to setup; a network error keeps the main view with a note.
    if (/rejected|401/i.test(e.message)) {
      showSetup(e.message);
    } else {
      showMain();
      el.count.textContent = "";
      el.list.replaceChildren();
      el.empty.hidden = false;
      el.empty.textContent =
        "Couldn't reach ApplyPilot on localhost:8765. Is the dashboard running? (" + e.message + ")";
    }
  }
}

// ---- events --------------------------------------------------------------
el.tokenSave.addEventListener("click", async () => {
  const tok = (el.token.value || "").trim();
  if (!tok) {
    showSetup("Paste a token first.");
    return;
  }
  await saveToken(tok);
  await load();
});
el.token.addEventListener("keydown", (e) => {
  if (e.key === "Enter") el.tokenSave.click();
});
el.refresh.addEventListener("click", load);
el.threadRead.addEventListener("click", readThread);
el.threadSave.addEventListener("click", saveThread);
// Changing who this is re-derives every direction that the operator has not overridden — the
// whole match is "which sender is the contact", so a different contact is a different answer.
el.threadContact.addEventListener("change", renderThreadMessages);
el.settingsToggle.addEventListener("click", async () => {
  el.token.value = await getToken();
  showSetup();
});

document.addEventListener("DOMContentLoaded", load);
load();

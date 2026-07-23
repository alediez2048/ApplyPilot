// ApplyPilot Contacts — dead-simple popup.
// Three jobs, nothing else:
//   1. Pull contacts from the latest ApplyPilot run  (GET /api/ext/queue)
//   2. Copy the drafted outreach note                (clipboard)
//   3. Open the right LinkedIn profile               (new tab)
//
// No content script, no background worker, no LinkedIn DOM automation. The popup fetches the
// contact list from the local dashboard (host_permissions bypasses CORS) and renders it.
// All contact text is inserted via textContent — never innerHTML — so a malicious name/note
// can't inject markup.

"use strict";

const API_BASE = "http://localhost:8765";
const QUEUE_URL = API_BASE + "/api/ext/queue?include_skipped=1"; // include_skipped: never hide a contact
const TOKEN_HEADER = "X-ApplyPilot-Token";
const TOKEN_KEY = "applypilot_token";

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
el.settingsToggle.addEventListener("click", async () => {
  el.token.value = await getToken();
  showSetup();
});

document.addEventListener("DOMContentLoaded", load);
load();

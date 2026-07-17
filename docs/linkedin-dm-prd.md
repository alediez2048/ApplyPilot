# PRD: LinkedIn DM outreach — "Send DM" from the dashboard, via agent-browser

**Status:** Draft v1 · **Depends on:** networking epic (NET-1..5), `agent-browser` binary
**Companion to:** `networking-outreach-prd.md` (this is the LinkedIn-DM channel of the same feature)

Give each found contact a **"Send DM"** button next to "Send email". You review the
drafted LinkedIn note (≤300 chars, already generated), click Send DM, and a **background
browser-agent task** drives a logged-in LinkedIn session to deliver the message — then the
row flips **sending… → sent ✓**. The twin of the email send you already have.

---

## 0. Architecture in one line

**Two repos stay separate.** ApplyPilot does **not** merge or vendor agent-browser; it
**drives the installed `agent-browser` binary as a background subprocess** — exactly how it
already drives `claude`, `npx`, and Chrome. agent-browser is the "hands" (browser actions);
ApplyPilot decides when to call it and with what message.

```
ApplyPilot (this repo)                         agent-browser (separate, installed tool)
  dashboard "Send DM"  ──background task──►     drives a logged-in LinkedIn Chrome:
  (linkedin_url + drafted note)                 open profile → compose → paste → send
                          ◄── status/result ──
```

---

## 1. Goals / non-goals

**Goals**
- A **"Send DM"** button per contact in the operator dashboard, mirroring "Send email".
- On click: fire a **background task** that sends the drafted LinkedIn note to that person.
- Reuse everything already built: the drafted `linkedin_message`, the contact's
  `linkedin_url`, the connection flag, the keyed background-task registry, and the NET-5
  safeguards.
- Human-in-the-loop: you see the draft and click each send — nothing auto-fires.

**Non-goals (v1)**
- No bulk / "DM everyone" — one contact per click, capped daily.
- No connection-request automation (only messaging existing connections / open profiles).
- No merging of the agent-browser codebase into ApplyPilot.
- No LLM *writing* at send time — the note is already drafted; the agent only navigates.

---

## 2. Why agent-browser (not the existing apply stack)

The apply flow uses `claude` + Playwright MCP to fill *unknown* ATS forms. LinkedIn is a
different problem: a **known site that aggressively detects and blocks automation**. The
"Claude for Chrome" extension is specifically detected/blocked; agent-browser drives
Chrome-for-Testing over CDP with a **real persistent profile**, indistinguishable from a
human session — which is why it can send DMs where the extension can't. So: right tool per
job. Apply keeps Playwright MCP; LinkedIn DM uses agent-browser.

---

## 3. How the send is driven

agent-browser is a CLI of deterministic primitives (`open`, `snapshot`, `click @ref`,
`fill`, `press`, `--profile`). Two ways to drive a single DM:

- **(A) `claude` + agent-browser MCP (recommended).** agent-browser exposes an `mcp` server;
  spawn `claude -p --mcp-config <agent-browser-mcp>` with a tight, read-mostly prompt: "open
  {profile_url}, open the message composer, type EXACTLY this text, click Send." Robust to
  LinkedIn's shifting DOM (the LLM re-locates the composer via snapshots). Mirrors the apply
  spawn pattern. Message text is fixed input — the LLM does not rewrite it.
- **(B) Deterministic agent-browser script.** `open → snapshot → click Message → fill → send`
  with selector heuristics. Faster/cheaper, no LLM, but brittle when LinkedIn A/B-tests the UI.

**Decision:** ship **(A)** for robustness; keep the deterministic path as a possible fast-path
later. Either way the agent is constrained to: open the given profile, compose, send — and
**nothing else** (enforced tool scope, see §6).

**Binary discovery:** `AGENT_BROWSER_BIN` env override → else `agent-browser` on PATH → else a
known local build. `doctor` reports which is found + its version. (Note: npm registry is
0.27.0; a newer local build (0.32.1) can be selected via the override.)

---

## 4. Data model (extend the existing `contacts` table)

DM is a separate channel from email, so it gets its own state columns (auto-migrated by
`store.ensure_contacts_columns`):

```
linkedin_message   TEXT      -- already exists (the ≤300-char draft)
dm_status          TEXT DEFAULT 'none'   -- none | drafted | sending | sent | failed
dm_sent_at         TEXT      -- when delivered (atomic-claim timestamp)
dm_error           TEXT      -- failure reason
```

- `linkedin_message` non-empty ⇒ `dm_status='drafted'` (Send DM enabled).
- Atomic claim on send (like email + apply): `UPDATE … SET dm_status='sending',
  dm_sent_at=now WHERE id=? AND dm_sent_at IS NULL` → only one racer proceeds.
- Cross-contact dedupe: never DM the same `linkedin_url` twice within a cooldown.

---

## 5. Dashboard UX (twin of Send email)

Under each contact's LinkedIn-note box (already there), next to **Copy note**:

```
LINKEDIN NOTE  264/300
  Hi Ali, I just applied for the AI Engineer role… Would love to connect.
  [ Save note ]  [ Copy note ]  [ Send DM ]
```

- **[Send DM]** enabled only when: a note exists, a `linkedin_url` exists, agent-browser +
  LinkedIn login are ready, under the daily cap, and not already sent.
- Click → one confirm ("Send this LinkedIn DM to {name}? This drives your real LinkedIn
  account.") → fires `POST /api/outreach/send-linkedin` → a **background task** (keyed
  registry, like Find contacts).
- Row shows **sending…** (live) → **sent ✓ {time}** or **failed {reason}**.
- Disabled-state tooltips: "connect agent-browser", "log into LinkedIn
  (`network --linkedin-login`)", "daily DM limit reached", "already sent".

New endpoint: `POST /api/outreach/send-linkedin` (Origin-guarded). Contact payload gains
`dm_status`, `dm_sent_at`, `dm_error`, and a `dm_available` capability flag.

---

## 6. Module map (`src/applypilot/networking/`)

| File | Role |
|------|------|
| `linkedin_dm.py` | **New.** Drives agent-browser to send one DM. Binary discovery, dedicated login profile, spawn (claude+MCP), read-mostly tool scope, parse success/failure. Fails soft. |
| `dm_prompt.py` | **New.** Builds the tight send-DM instruction (profile_url + exact message + "send only, do nothing else"). |
| `store.py` | +`dm_*` columns, `claim_dm_send()`, `mark_dm_sent()/mark_dm_failed()`, `dm_sent_today()`, `already_dmed(linkedin_url)`. |
| `web_dashboard.py` | +`/api/outreach/send-linkedin`, background task via the existing `NetworkRunner`-style registry, Send DM button + status, `dm_available`. |
| `cli.py` | `network --linkedin-login` already exists (reuse for the DM profile); `doctor` gains agent-browser + DM-readiness lines. |

---

## 7. Safeguards (all enforced — this is the risky part)

LinkedIn automation on your **primary account** violates LinkedIn's ToS and can cause
**permanent restriction** (not just a temporary block) plus the monthly commercial-use lock.
Mitigations, enforced in code:

- **Opt-in, off by default** — `NETWORKING_LINKEDIN_DM=0`; a one-time consent gate that names
  the real stake (permanent account restriction); recommend a secondary account.
- **Human-in-the-loop** — every DM is one click + one confirm on a draft you can see/edit.
- **Global daily cap** — `LINKEDIN_DM_DAILY_LIMIT` (default low, e.g. 5–10), persisted across
  runs; refuse beyond it.
- **Cross-contact dedupe** — never DM the same person twice within a cooldown.
- **Atomic claim** — no double-send under the threading server.
- **Tool-scoped agent** — the send agent may only navigate + read + type + click within the
  message flow; no arbitrary browsing, no connect/endorse/follow, no other tools.
- **Dedicated, isolated profile** — a LinkedIn-only agent-browser profile, separate from the
  apply workers and your everyday Chrome; one-time login via a precheck-guarded flow.
- **Human-paced** — a cooldown between sends; single concurrency (one DM task at a time).
- **Dry-run** — a mode that navigates + composes but does **not** click Send, for testing.

---

## 8. Config / gating

- **agent-browser** installed (PATH or `AGENT_BROWSER_BIN`) + its Chrome (`agent-browser install`).
- **`NETWORKING_LINKEDIN_DM`** (0/1, off), **`LINKEDIN_DM_DAILY_LIMIT`** (default 5),
  **`LINKEDIN_DM_COOLDOWN_DAYS`**, **`AGENT_BROWSER_BIN`** (optional path override).
- One-time LinkedIn login into the DM profile (reuse `network --linkedin-login`, pointed at
  the DM profile) + consent acknowledgement.
- `doctor`: agent-browser found (+version), DM profile logged in, daily-cap usage.

---

## 9. Phased rollout (tickets → `docs/tickets/`)

1. **LDM-1 — agent-browser bridge + one-DM send (CLI).** `linkedin_dm.py` + `dm_prompt.py` +
   binary discovery + login profile + `applypilot network --dm --url <job> --contact <id>`
   (or a test send). Store `dm_*` columns + atomic claim + caps/dedupe. No dashboard.
2. **LDM-2 — Dashboard "Send DM".** button + `/api/outreach/send-linkedin` + background task +
   live row status + disabled-state tooltips + Origin guard.
3. **LDM-3 — Safeguards & doctor.** consent gate, daily cap wiring, dedupe, dry-run,
   `doctor` lines, `.env.example`.
4. **LDM-4 (later) — deterministic fast-path** + optional retries/observability.

Each phase independently testable; LDM-1 is the load-bearing spike (does agent-browser
reliably send a DM end-to-end?).

---

## 10. Testing

- **Pure/unit:** binary discovery precedence, `claim_dm_send` single-winner, daily-cap +
  dedupe logic, `dm_prompt` builder (contains the exact message + "send only" wording),
  payload `dm_*` shape. agent-browser subprocess mocked.
- **Gated integration (manual/opt-in, not CI):** a real send to **your own alternate account**
  or LinkedIn's "Message yourself"/a test connection, behind an env flag.
- **Dry-run:** compose-without-send verified on the real UI before any live send.

---

## 11. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| **Permanent LinkedIn restriction** | opt-in + off by default; consent gate; low daily cap; human-in-loop; secondary-account rec |
| LinkedIn UI changes break the flow | claude+MCP re-locates via snapshots; dry-run; graceful failure (row → failed, no crash) |
| Wrong-person / wrong-text DM | you review the exact draft; agent types it verbatim; dry-run |
| Double-send / cap bypass | atomic `dm_sent_at IS NULL` claim + cap in same txn; cross-contact dedupe |
| agent-browser version drift (0.27 vs 0.32) | `AGENT_BROWSER_BIN` override; `doctor` shows version |
| Not logged in → silent no-op | login precheck aborts cleanly with an actionable message |
| Local CDP exposure (agent-browser opens a debug port) | isolated profile; localhost only; close when done (documented) |

---

## 12. Open questions

- Default `LINKEDIN_DM_DAILY_LIMIT` (proposed 5) and cooldown window (proposed 30 days).
- Which agent-browser build to standardize on (global 0.27.0 vs local 0.32.1).
- Should Send DM be limited to **existing connections** (safer, and you can actually message
  them directly) vs. also open profiles (which may only allow connect, not message)?
- Secondary LinkedIn account: use one, or accept the risk on the primary?

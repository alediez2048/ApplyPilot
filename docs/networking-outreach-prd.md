# PRD: Networking & Outreach — find people at target companies, get contacts, draft & send outreach

**Status:** Draft v2 (revised after adversarial review — see `tickets/` and the review punch list)
**Depends on:** existing apply stack, `llm.py` (multi-provider), operator dashboard

For each job you apply to, automatically find 3–5 relevant people at that company
(peers in the role, recruiters, hiring managers), retrieve their **email + LinkedIn URL**,
**display them in the operator dashboard**, draft a tailored outreach email, and **send it
via Gmail** — with review and explicit confirmation.

> **v2 changes from review:** phone dropped (Apollo delivers it async via webhook — not
> obtainable by a localhost tool); Apollo requires a **paid plan + master API key**;
> Apollo endpoints/params corrected; `company` must be derived (the pipeline stores the
> job-board name, not the employer); `contacts` is its own table; sends are atomically
> claimed and de-duped across jobs; LinkedIn fallback kept but hardened; Gmail sender is
> the user's @utexas.edu (Workspace) so NET-4 must handle SMTP **and** an OAuth fallback.

---

## 1. Goals / non-goals

**Goals**
- Given an applied/prepared job, find **3–5 relevant people** at that company.
- Retrieve **contact info**: full name, position/title, **email (+status)**, LinkedIn URL.
- **Populate the current operator dashboard** with those contacts per job.
- **Draft a tailored outreach email** per contact (editable).
- **Send it via Gmail** from a dashboard button — user-initiated, confirmed, verified-gated,
  daily-capped, cross-job de-duped.
- Reuse existing infra: `jobs` DB, `llm.py`, the operator dashboard, the apply
  browser-agent primitives (for the LinkedIn fallback).

**Non-goals (v1)**
- **Phone numbers** — Apollo reveals phone only via an async public HTTPS webhook a local
  tool can't receive. Out of scope for v1 (revisit with a hosted callback later).
- **Silent/bulk auto-send** — every send is user-initiated + confirmed; no campaigns/sequencing.
- **LinkedIn messaging/connection automation** (Connect/InMail) — high ban risk, excluded.
- No bulk prospecting / CRM.

---

## 2. Feasibility findings (why this design)

| Path | Verdict | Notes |
|------|---------|-------|
| **Official LinkedIn API** | ❌ Not viable | No public people-search-by-company API; enterprise-partner gated. |
| **LinkedIn browser agent** | ⚠️ Kept, hardened, opt-in | Reuses the apply Chrome/agent primitives. **Real risk:** automating your *primary* LinkedIn account against LinkedIn's no-bots ToS can cause **permanent account restriction** (not just a "soft ban") + the monthly commercial-use search lock. Auth is **best-effort** — only works if the cloned Chrome profile was LinkedIn-logged-in; needs a one-time login + precheck. See §5. |
| **Apollo.io API** | ✅ Primary source | Real REST API. People Search by **org + titles + keywords** → names, titles, seniority; enrichment reveals **verified email** + LinkedIn URL. **Requires a paid plan + master API key** (free tier has no API access as of late 2025). |

**Decision:** Apollo API is primary; the hardened LinkedIn agent is an opt-in fallback when
Apollo returns too few people.

---

## 3. Architecture

New subsystem `networking/`, a new `contacts` table (its **own** init, not `_ALL_COLUMNS`),
a `network` CLI verb, and a dashboard panel. Nothing in the existing pipeline changes.

```
applied/prepared job
   │  role = job.title
   │  company = derived (JSON-LD > careers hostname > LLM from full_description > job.site)
   │  domain  = derived company domain (for Apollo people search)
   ▼
networking/apollo.py ─► People Search (domain/org + titles + keywords)  → candidates (masked; no email/LinkedIn yet)
   │                                              │
   │  rank & pick 3–5                             │  if fewer than N found AND LinkedIn enabled
   ▼                                              ▼
Apollo bulk enrichment (reveal email + LinkedIn)  networking/linkedin_agent.py (hardened, opt-in)
   │                                              │  names + profile URLs
   └───────────────► merge + dedupe ◄────────────┘  (Apollo-enrich by linkedin_url)
                           │
                           ▼
              networking/outreach.py  (LLM drafts subject + body per contact)
                           │
                           ▼
                 contacts table  ──►  operator dashboard panel  ──►  Gmail send (§8)
```

**Company/domain derivation (NET-1, before any Apollo call):** the pipeline does **not**
store the employer — jobspy parses `company` then discards it and `site` holds the
job-board name (Indeed/LinkedIn). NET-1 adds a `company` column (auto-migrated), populates
it going forward (jobspy store path + dashboard `_infer_company`), and derives it for
existing rows from JSON-LD → careers hostname → LLM-from-`full_description` → fallback.
Company **domain** (for `q_organization_domains_list[]`) is derived from the company /
`application_url` hostname.

### 3.1 Data model — new `contacts` table (owned by `store.py`, not `_ALL_COLUMNS`)

`store.py` provides `init_contacts()` / `ensure_contacts_columns()` (mirrors the pattern in
`database.py` but for its own table) and is invoked from `init_db`-adjacent startup, the
dashboard server, and the `network` CLI so every read path guarantees the table exists.

```sql
CREATE TABLE IF NOT EXISTS contacts (
    id               TEXT PRIMARY KEY,   -- sha1("{job_url}\x1f{linkedin_url or ''}\x1f{name or ''}")  (delimited)
    job_url          TEXT NOT NULL,      -- references jobs.url (no FK enforcement in SQLite; cleaned up in _delete_job)
    full_name        TEXT,
    title            TEXT,
    company          TEXT,
    linkedin_url     TEXT,               -- from enrichment (NOT from search)
    email            TEXT,
    email_status     TEXT,               -- internal: verified | unverified | none  (mapped from Apollo)
    location         TEXT,
    seniority        TEXT,
    match_reason     TEXT,               -- same role | recruiter | hiring manager | same team
    source           TEXT,               -- apollo | linkedin
    apollo_id        TEXT,
    outreach_subject TEXT,
    outreach_message TEXT,
    outreach_status  TEXT DEFAULT 'none',-- none | drafted | sending | submitted | failed
    outreach_channel TEXT,               -- email
    submitted_at     TEXT,               -- set atomically at send-claim (see §8); "submitted" ≠ delivered
    sent_message_id  TEXT,               -- client-generated Message-ID (SMTP returns none)
    send_error       TEXT,
    discovered_at    TEXT,
    updated_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_contacts_job ON contacts(job_url);
CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email);
CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(outreach_status, submitted_at);
```

**Key + dedupe:** the PK hash is **delimited** (`\x1f`) to avoid collisions. Contacts never
switch identity once stored. **Cross-job dedupe** for sending is keyed on **normalized email**
(and/or normalized `linkedin_url`) across *all* jobs — the same human is emailed at most once
within a cooldown window, even across multiple applications. The dashboard surfaces
"already contacted for another role."

**`email_status` mapping (Apollo → internal):** `verified → verified`; `unverified` /
`extrapolated` / any non-verified with an address → `unverified` (needs 2nd confirm to send);
no address revealed → `none` (Send disabled). `locked` is **not** an Apollo value.

### 3.2 Module map (`src/applypilot/networking/`)

| File | Role |
|------|------|
| `apollo.py` | Apollo client (paid + **master** key): company/domain resolution, People Search (`mixed_people/api_search`), bulk enrichment (`people/bulk_match`) to reveal email + LinkedIn. Credit-aware. |
| `linkedin_agent.py` | **New** thin agent spawner (copies only the Popen/stream-json pattern from `apply/launcher.py::run_job` — not verbatim reuse). Parameterized Playwright-only MCP config (drops Gmail), enforced read-only tools, login precheck, JSON-array parser. |
| `prompt.py` | Builds the LinkedIn-agent instruction (company, role keywords, "return N people as JSON"). |
| `rank.py` | Pure: select best 3–5 (title/seniority similarity + role mix; peers + ≥1 recruiter/hiring manager). Ranks on title/seniority only (LinkedIn URL isn't available pre-enrichment). |
| `outreach.py` | LLM drafts subject + body per contact. Reuses `llm.py` + validator guardrails. |
| `gmail_send.py` | Gmail send: SMTP app-password **and** OAuth fallback (the user's @utexas.edu is Workspace). Atomic send-claim, verified-gate, daily cap, dry-run, `doctor` AUTH probe. |
| `service.py` | Orchestrator: `find_contacts_for_job(job, opts)` → derive company/domain → Apollo search → rank → enrich top-N → (opt-in LinkedIn fallback) → merge → draft → persist. |
| `store.py` | Owns `contacts` table (`init_contacts`, upserts, queries, atomic send-claim, cross-job dedupe). |

### 3.3 Pipeline & CLI

```bash
applypilot network --url URL        # one job (primary usage)
applypilot network                  # jobs missing contacts
applypilot network --per-job 5      # how many contacts (default 5)
applypilot network --no-linkedin    # Apollo only (default respects NETWORKING_LINKEDIN)
applypilot network --linkedin-login # one-time: open Chrome to log into LinkedIn
applypilot network --draft/--no-draft
applypilot network --dry-run        # search + rank only; no reveal, no send
```

Gated by a real `require_apollo_key()` helper (mirrors `config.check_tier`'s stderr +
`SystemExit(1)`), independent of the numeric tier system. Also a **dashboard button** per job.

---

## 4. Apollo integration (`apollo.py`) — corrected against current docs

**Auth:** `APOLLO_API_KEY` (a **master** key; **paid plan required**) in `.env`, header `X-Api-Key`.
Base URL: `https://api.apollo.io/api/v1` (note the `/api`).

1. **Company/domain resolution.** Prefer **skipping** org-enrich and passing the derived
   company domain directly to people search via `q_organization_domains_list[]`. If org IDs
   are needed, use company **search** (`mixed_companies/search`) to get `organization_ids[]`.
   Avoid `GET /organizations/enrich` — it **consumes a credit per record**.
2. **People Search** — `POST /mixed_people/api_search` (`api_search`, not the deprecated
   `search`). Params: `q_organization_domains_list[]` (or `organization_ids[]`),
   `person_titles[]`, `person_seniorities[]`, `q_keywords`, `page`, `per_page`. Returns
   candidates with name, title, seniority, `id`. **Email + LinkedIn URL are NOT in this
   response** (cheap; masked).
3. **Bulk enrichment / reveal** — `POST /people/bulk_match` with the selected `id`s and
   `reveal_personal_emails=true` → **verified email + `linkedin_url`**. **Consumes credits.**
   (Phone requires an async `webhook_url` → out of scope, see §1.)

**Credit discipline:** search is cheap and masked. **Rank first, then bulk-enrich only the
selected 3–5.** Never enrich the full result set. `--per-job` caps reveals; `--dry-run`
performs zero reveals. Log remaining-credit headers when Apollo returns them.

**Title derivation:** map the job title to `person_titles[]` + synonyms and always add
recruiter/talent titles so a hiring contact surfaces. Small static map (+ optional LLM
expansion, off by default).

**Gating:** `doctor` and the CLI probe the key against a cheap endpoint (auth/usage health)
and detect **403 / master-key** errors — presence of the env var is not sufficient.

---

## 5. LinkedIn fallback (`linkedin_agent.py`) — hardened, opt-in

Invoked **only** when Apollo yields `< per_job` AND LinkedIn is enabled. Reuses the apply
Chrome primitives; **spawns a new thin agent** (copies the Popen/stream-json pattern from
`run_job` — not verbatim reuse).

- **Enforced read-only** — pass `--allowedTools` limited to read/navigate/snapshot Playwright
  tools and `--disallowedTools` for `browser_click`/`browser_fill_form`/etc. (mirrors how
  apply restricts Gmail tools). Read-only is **tool-enforced**, not prompt-only.
- **Parameterized MCP** — a Playwright-only config (drops the Gmail server that
  `_make_mcp_config` hardcodes).
- **Login is best-effort** — a one-time `applypilot network --linkedin-login` opens the worker
  profile to log in once; a **login-state precheck** aborts cleanly ("not logged into
  LinkedIn") before spawning. Isolate the networking Chrome user-data-dir + CDP port range
  from apply's to avoid collisions.
- **The agent:** open LinkedIn → search company → **People** → filter by role keywords → read
  page 1 → return `[{name, title, profile_url}]` as JSON. Read-only; no Connect/message.
- **Recovery:** Apollo-enrich the found people by **`linkedin_url`** (Apollo's strongest match
  key). Expect misses in low-Apollo-coverage companies → some contacts have no email; the
  dashboard shows them with Send disabled.

**Caps & consent (enforced in code):**
- Global **`NETWORKING_LINKEDIN_DAILY_LIMIT`** (default 3–5 companies/day), persisted across runs.
- ≤5 profiles/company, single page, cooldown between companies.
- **Off by default** (`NETWORKING_LINKEDIN=0`, `--no-linkedin`); a one-time explicit consent
  acknowledgement that names the real stake: *possible permanent restriction of your primary
  LinkedIn account.* Recommend a secondary account.
- Graceful empty result on login wall / CAPTCHA / parse failure — never crashes.

---

## 6. Outreach drafting (`outreach.py`)

LLM drafts a short email (subject + body) using `profile.json`, the job (`title`,
`full_description` snippet, company), and the contact (`full_name`, `title`, `match_reason`).
Body 3–4 sentences: name the exact role, one proof point, a soft ask. Reuses the tailoring
guardrails (no fabrication, plain voice). Stored in `outreach_subject`/`outreach_message`,
editable in the dashboard before send.

---

## 7. Dashboard UX (operator dashboard) — contact display is core

Per job, a **"People at {company}"** section in the current dashboard. Per contact:
full name · position/title · email (+status badge) · LinkedIn URL · `match_reason` chip ·
editable subject + draft · `[copy] [edit] [send email]`. (**Phone shows `—`** in v1.)

```
People at Affirm                                   [ Find contacts ]
─────────────────────────────────────────────────────────────────
● Jane Smith — Staff AI Engineer            [same role]
  ✉ jane.smith@affirm.com  ✅ verified        🔗 linkedin.com/in/janesmith
  Subject: Question about the AI Solutions Engineer role
  ▸ "Hi Jane — I just applied for the AI Solutions Engineer role…"   (editable)
      [ copy ]   [ edit ]   [ send email ]
● Omar Reyes — Technical Recruiter          [recruiter]
  ✉ omar@affirm.com  ⚠ unverified            🔗 —
      [ copy ]   [ edit ]   [ send email — confirm (unverified) ]
```

**Scope note (must-fix):** the current dashboard only renders **URL-imported** jobs
(`strategy IN ('dashboard_upload','manual_url_batch')`). NET-2 either (a) scopes contacts to
URL-imported jobs, or (b) broadens the job query to include applied jobs regardless of
strategy. The PRD must not imply all applied jobs appear until (b) is done.

**Concurrency (must-fix):** the dashboard has one global `CommandRunner` that refuses to
start if busy and shares one log. NET-2 adds a **keyed background-task registry** (task per
`job_url`, independent logs) so "Find contacts" can run without colliding with prepare/apply.

**Send semantics:** verified → one confirm; **unverified → second explicit confirm**; **no
address (`none`) → Send disabled**. Row flips to **submitted {timestamp}** (not "delivered")
or **failed**. Cross-job dedupe warns "already contacted for another role."

**Security (must-fix):** state-changing POSTs (`/api/network`, `/api/outreach`,
`/api/outreach/send`) get an **Origin/Host allowlist** check (the send endpoint fires
irreversible email; guards against DNS-rebinding to localhost).

Endpoints: `POST /api/network`, contacts folded into the job payload (`GET`),
`POST /api/outreach` (save/regenerate draft), `POST /api/outreach/send`.

---

## 8. Gmail send automation (`gmail_send.py`)

A dashboard **Send** button that emails the contact from the user's Gmail
(**sender: jorgediez2408@utexas.edu**, a Google **Workspace** account).

**Transport — SMTP with an OAuth fallback (both in NET-4, not deferred):**
1. **SMTP + App Password** (`smtplib.SMTP_SSL('smtp.gmail.com', 465)`). Simple, no deps.
   **Workspace caveat:** admins can disable app-password generation; a blocked tenant fails
   as SMTP **535**. Detect 535 → show "your Google/Workspace admin may have disabled app
   passwords — use the OAuth path."
2. **Gmail API OAuth (fallback, in scope for NET-4 given the Workspace sender).** Adds
   `google-api-python-client` + a one-time OAuth. Needed if the utexas tenant blocks app
   passwords. Yields Gmail's real message id + better deliverability.

`doctor` performs a **live AUTH-only** login test (connect+login+quit, no send) so a
blocked/revoked credential is caught up front, not at send time.

**Send flow (atomic, race-safe):** claim the row like `apply/launcher.py::acquire_job`:
`UPDATE contacts SET outreach_status='sending', submitted_at=<now> WHERE id=? AND submitted_at IS NULL`;
send only if `rowcount==1`; roll status back on SMTP/OAuth failure. Enforce the daily cap in
the same transaction. Store a **client-generated `Message-ID`** (`email.utils.make_msgid()`);
SMTP returns no server id.

**Safeguards (all enforced):**
- User-initiated only; one click + confirm per email; **no auto-send**.
- **Verified-gate:** `verified` → one confirm; `unverified` → second explicit confirm;
  `none` (no address) → Send disabled.
- **Daily cap** `OUTREACH_DAILY_LIMIT` (default 20), enforced atomically.
- **Cross-job dedupe** on normalized email — never twice to one human within a cooldown.
- **Dry-run** logs the email instead of sending.
- Honest footer; state labeled **submitted** (SMTP acceptance ≠ delivery; async bounces from
  unverified addresses harm sender reputation — warn, and prefer verified-only).
- Credentials read only from `.env`; never logged; TLS only.

> **.edu note:** cold outreach from an institutional address can violate the university AUP
> and risk the account. Surfaced to the user; a personal gmail.com remains the safer sender
> if utexas proves restricted.

---

## 9. Config / gating

- **`APOLLO_API_KEY`** — a **master** key on a **paid** Apollo plan. Gated via
  `require_apollo_key()` + a live `doctor` probe (detects 403/master-key errors).
- **`GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD`** (SMTP) and/or Gmail OAuth creds; `OUTREACH_FROM_NAME`,
  `OUTREACH_DAILY_LIMIT` (20). `doctor` does an AUTH-only probe.
- **`NETWORKING_LINKEDIN`** (0/1, off), **`NETWORKING_LINKEDIN_DAILY_LIMIT`** (3–5).
- `.env.example` gains `APOLLO_API_KEY`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`.
- LinkedIn fallback needs Tier 3 (Claude CLI + Chrome + Node).

---

## 10. Phased rollout

1. **NET-1 — Apollo core:** `company` column + derivation; `store.py` (`init_contacts`);
   `apollo.py` (corrected endpoints, master-key gate); `rank.py`; `service.py` (Apollo-only);
   `applypilot network` CLI + `require_apollo_key`. Contacts (name/title/email/LinkedIn) in DB.
2. **NET-2 — Dashboard contacts:** populate the dashboard (job-scope fix + keyed task registry
   + Origin check); "Find contacts" button + endpoints.
3. **NET-3 — Outreach drafting:** `outreach.py`, editable subject/body.
4. **NET-4 — Gmail send:** `gmail_send.py` (SMTP **+ OAuth fallback**), atomic claim,
   verified-gate, daily cap, cross-job dedupe, dry-run, `doctor` AUTH probe.
5. **NET-5 — LinkedIn fallback (hardened):** enforced read-only, login setup + precheck,
   global daily cap, consent gate, enrich-by-linkedin_url.
6. **NET-6 (backlog):** phone via hosted webhook, threaded follow-ups, reply tracking,
   opt-in verified-only auto-send.

---

## 11. Testing

- **Pure/unit:** `rank.select` (title/seniority + role mix), title→`person_titles`, company/
  domain derivation, contact key/dedupe, `store` upsert + atomic send-claim (concurrent
  attempts), `gmail_send` MIME + verified-gate + cap. Apollo & SMTP mocked. No network in CI.
- **Integration (gated, skipped in CI):** real Apollo call + a real Gmail AUTH/send-to-self,
  behind env flags — kept out of the per-ticket CI DoD (they bake in live paid deps).
- **LinkedIn agent:** prompt-builder + read-only tool-scoping unit test; agent run manual/opt-in.

---

## 12. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| Apollo paid/master-key required | Gate on live probe; document the paid dependency |
| Apollo credit burn | Reveal only selected 3–5; `--per-job`; `--dry-run`; log credits |
| Emailing unverified/guessed address | Status badge; 2nd confirm; Send disabled when no address; dry-run |
| Cold-email spam / sender reputation | User-initiated + confirm; daily cap; cross-job dedupe; "submitted" label; prefer verified |
| Workspace app-password blocked (@utexas) | Detect 535; OAuth fallback in NET-4; `doctor` AUTH probe |
| .edu AUP for cold email | Surface to user; personal gmail.com recommended alternative |
| Send race (double-send / cap bypass) | Atomic DB claim (`submitted_at IS NULL`) + cap in same txn |
| localhost CSRF / DNS-rebinding | Origin/Host allowlist on state-changing POSTs |
| **LinkedIn permanent account restriction** | Opt-in + off by default; consent gate naming the real stake; global daily cap; enforced read-only; secondary-account recommendation |
| Gmail MCP prompt-injection (`send_email` allowed in apply) | Prefer deterministic SMTP button (no LLM in loop); note the exposure; keep agent send restricted |
| Orphaned contacts on job delete | Delete contacts in `_delete_job` (no SQLite FK enforcement) |
| PII | Contacts stored locally only; no external sync |

---

## 13. Resolved / remaining questions

- **Resolved:** phone → out of v1; Apollo → paid + master key; sender → @utexas.edu with
  OAuth fallback; NET-5 → kept but hardened.
- **Remaining:** Apollo plan tier & monthly credit budget (sets default `--per-job` + reveal
  policy); whether networking auto-triggers after a successful apply or stays manual;
  cross-job send cooldown window (proposed 30 days).

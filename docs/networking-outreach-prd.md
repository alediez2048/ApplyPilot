# PRD: Networking & Outreach — find people at target companies, get contacts, draft outreach

**Status:** Draft · **Author:** ApplyPilot · **Depends on:** existing apply stack, `llm.py` (multi-provider), operator dashboard

For each job you apply to, automatically find 3–5 relevant people at that company
(peers in the role, recruiters, hiring managers), retrieve their contact info,
**display it in the operator dashboard** (names, positions, emails, phone numbers,
LinkedIn), draft a tailored outreach email, and **send it via Gmail** — with review
and explicit confirmation.

---

## 1. Goals / non-goals

**Goals**
- Given an applied (or prepared) job, find **3–5 relevant people** at that company.
- Retrieve **contact info**: full name, position/title, **email, phone**, LinkedIn URL.
- **Populate the current operator dashboard** with those contacts per job (a first-class
  requirement, not a side panel afterthought).
- **Draft a tailored outreach email** per contact (editable).
- **Send the outreach email via Gmail** from a dashboard button — with review + a
  per-send confirmation, verified-email gating, and a daily cap.
- Reuse existing infrastructure: the `jobs` DB, the multi-provider LLM client, the
  operator dashboard, the already-wired Gmail integration, and — only as a fallback —
  the apply browser-agent stack.

**Non-goals (v1)**
- No **silent/bulk auto-send**. Every send is user-initiated and confirmed; no campaign
  sequencing, no send-to-everyone. (Sequencing/auto-send-after-apply is a later phase.)
- No bulk prospecting / CRM. Scope is the companies you're actually applying to.
- No LinkedIn connection automation (clicking "Connect"/InMail) — high ban risk.

---

## 2. Feasibility findings (why this design)

| Path | Verdict | Notes |
|------|---------|-------|
| **Official LinkedIn API** | ❌ Not viable | No public people-search-by-company API. Sales Navigator / Talent APIs are enterprise-partner gated and disallow third-party people search. |
| **LinkedIn browser agent** | ⚠️ Works, use sparingly | Auth is solved (reuse the persistent authenticated Chrome profile the apply flow already clones). Risk is LinkedIn's anti-automation: CAPTCHAs, the "commercial use limit" on search, soft bans. Safe only at low volume (3–5/company), opt-in, human-paced. |
| **Apollo.io API** | ✅ Primary source | Real REST API. People Search by **org + titles + keywords** → names, titles, LinkedIn URLs; Enrichment reveals **verified email + phone**. Legal, reliable, no scraping. |

**Decision:** Apollo API is the primary source; the LinkedIn browser agent is an
**optional fallback** invoked only when Apollo returns too few results.

---

## 3. Architecture

New subsystem `networking/`, a new `contacts` table, a `network` pipeline verb, and
a dashboard panel. Nothing in the existing pipeline changes.

```
applied/prepared job
   │  (company = job.site, role = job.title)
   ▼
networking/apollo.py ──► People Search (org+titles+keywords)  →  candidates (masked)
   │                                              │
   │  rank & pick 3–5                             │  if fewer than N found
   ▼                                              ▼
Apollo Enrichment (reveal email/phone)   networking/linkedin_agent.py  (reuses apply stack:
   │                                       claude -p + Playwright MCP + persistent Chrome)
   │                                              │  names + profile URLs
   └───────────────► merge + dedupe ◄────────────┘
                           │
                           ▼
              networking/outreach.py  (LLM drafts a message per contact)
                           │
                           ▼
                 contacts table  ──►  operator dashboard panel
```

### 3.1 Data model — new `contacts` table

One row per person per job. Added to `database._ALL_COLUMNS`-style registry (its own
table + forward-only migration, mirroring the `jobs` pattern).

```sql
CREATE TABLE IF NOT EXISTS contacts (
    id              TEXT PRIMARY KEY,   -- sha1(job_url + linkedin_url|name)
    job_url         TEXT NOT NULL,      -- FK -> jobs.url
    full_name       TEXT,
    title           TEXT,
    company         TEXT,
    linkedin_url    TEXT,
    email           TEXT,
    email_status    TEXT,               -- verified | guessed | locked | none
    phone           TEXT,
    location        TEXT,
    seniority       TEXT,               -- e.g. senior, manager, director
    match_reason    TEXT,               -- "same role", "recruiter", "hiring manager", "same team"
    source          TEXT,               -- apollo | linkedin
    apollo_id       TEXT,
    outreach_subject TEXT,              -- drafted email subject
    outreach_message TEXT,              -- drafted email body (editable)
    outreach_status TEXT DEFAULT 'none',-- none | drafted | sent | failed
    outreach_channel TEXT,              -- email | linkedin
    sent_at         TEXT,               -- when the email was actually sent
    sent_message_id TEXT,               -- Gmail message id / SMTP id for audit
    send_error      TEXT,
    discovered_at   TEXT,
    updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_contacts_job ON contacts(job_url);
```

### 3.2 Module map (`src/applypilot/networking/`)

| File | Role |
|------|------|
| `apollo.py` | Apollo client: org resolution, People Search, People Enrichment (reveal). Credit-aware. |
| `linkedin_agent.py` | Fallback: spawns `claude -p` + Playwright MCP against the persistent Chrome profile to search LinkedIn People and extract names + profile URLs. Reuses `apply/chrome.py` + `apply/launcher.py` primitives. |
| `prompt.py` | Builds the LinkedIn-agent instruction prompt (company, role keywords, "return N people as JSON"). |
| `rank.py` | Selects the best 3–5 candidates: title similarity to the role + a useful mix (peers + 1–2 recruiters/hiring managers). Pure, testable. |
| `outreach.py` | LLM drafts a tailored email per contact (profile + job + contact). Reuses `llm.py`. |
| `gmail_send.py` | Sends an outreach email via Gmail (SMTP app-password default; Gmail API OAuth optional). Records send status; enforces daily cap + verified-email gate. |
| `service.py` | Orchestrator: `find_contacts_for_job(job, opts)` → Apollo → (fallback) → merge → enrich → draft → persist. |
| `store.py` | `contacts` table schema, upserts, queries (mirrors `database.py`). |

### 3.3 Pipeline & CLI

New verb, gated like apply (needs Apollo key; LinkedIn fallback needs Tier 3):

```bash
applypilot network                 # for all applied jobs missing contacts
applypilot network --url URL       # one specific job
applypilot network --per-job 5     # how many contacts to find (default 5)
applypilot network --no-linkedin   # Apollo only, skip browser fallback
applypilot network --draft/--no-draft   # toggle outreach drafting (default on)
```

Runs after `apply` (you network on jobs you've applied to), but can run on any
prepared job. Also a **dashboard button** ("Find contacts") per job.

---

## 4. Apollo integration (`apollo.py`)

**Auth:** `APOLLO_API_KEY` in `~/.applypilot/.env`, sent as `X-Api-Key` header.

**Endpoints (verify against current Apollo docs before implementing):**
1. **Org resolution** — `POST /v1/organizations/enrich` (or search) with the company
   name → canonical `organization_id` + primary domain. Improves people-search precision.
2. **People Search** — `POST /v1/mixed_people/search` with
   `organization_ids`/`q_organization_domains`, `person_titles[]` (derived from the
   job title + synonyms), `q_keywords`, `page`, `per_page`. Returns candidates with
   name, title, seniority, `linkedin_url`, `id`. **Emails are masked here** (cheap).
3. **People Enrichment / reveal** — `POST /v1/people/match` with the person `id`
   (and `reveal_personal_emails`/`reveal_phone_number`) → **verified email + phone**.
   **Consumes credits.**

**Credit discipline (important):** Search is cheap and returns masked candidates.
We **rank first, then reveal only the selected 3–5** — never enrich the full result
set. `--per-job` caps reveals. Log remaining-credit headers if Apollo returns them.

**Title derivation:** map the job title to `person_titles` + synonyms
(e.g. "Senior Technical Product Manager" → ["Technical Product Manager", "Product
Manager", "Senior Product Manager"]) and always add recruiter/talent titles so we
surface a hiring contact. Small static synonym map + optional LLM expansion.

---

## 5. LinkedIn fallback (`linkedin_agent.py`)

Invoked **only** when Apollo yields `< per_job` contacts. Reuses the apply stack verbatim:

- `apply/chrome.py::launch_chrome` → isolated Chrome on a CDP port, **persistent
  worker profile** (already carries your LinkedIn session cookies).
- Spawn `claude -p --mcp-config <playwright-only> --permission-mode bypassPermissions`
  exactly like `apply/launcher.py::run_agent`, piping the networking prompt.
- The agent: open `linkedin.com`, search the company, click **People**, filter by the
  role keywords, read the first page, return **N people as JSON**
  `[{name, title, profile_url}]`. **Read-only** — no Connect/message clicks.

**Risk mitigations (encoded, not aspirational):**
- Hard cap: one company at a time, ≤ 5 profiles, one search page.
- Human-paced (the agent naturally is); a per-run cooldown between companies.
- Read-only tool scope (no connect/InMail actions in the prompt).
- Feature flag `NETWORKING_LINKEDIN=0` to disable entirely.
- Names+URLs from LinkedIn are then **Apollo-enriched by name+company** to get email.
- Doc + dashboard note: LinkedIn automation is best-effort and at the user's discretion
  (ToS). Off by default via `--no-linkedin` if the user prefers Apollo-only.

---

## 6. Outreach drafting (`outreach.py`)

For each stored contact, the LLM drafts a short, specific **email** (subject + body) using:
- your `profile.json` (who you are, relevant skills),
- the job (`title`, `full_description` snippet, company),
- the contact (`full_name`, `title`, `match_reason`).

Body: 3–4 sentences — mention the specific role you applied to, one relevant proof point,
a soft ask (15-min chat / question about the team). Optionally a short LinkedIn-note
variant (≤ 300 chars). Reuses the tailoring guardrails (no fabrication, plain voice).
Stored in `outreach_subject`/`outreach_message`; **editable in the dashboard before send**.

---

## 7. Dashboard UX (operator dashboard) — contact display is core

Per applied job, a **"People at {company}"** section populated in the **current**
operator dashboard. Each contact shows the full set of fields:

```
People at Affirm                                   [ Find contacts ]
─────────────────────────────────────────────────────────────────
● Jane Smith                                       [same role]
  Staff AI Engineer
  ✉ jane.smith@affirm.com  ✅ verified   ☎ +1 415-555-0142
  🔗 linkedin.com/in/janesmith
  Subject: Question about the AI Solutions Engineer role
  ▸ "Hi Jane — I just applied for the AI Solutions Engineer role…"   (editable)
      [ copy ]   [ edit ]   [ send email ✅ verified ]
● Omar Reyes                                       [recruiter]
  Technical Recruiter
  ✉ omar@affirm.com  ⚠ guessed          ☎ —
  🔗 linkedin.com/in/omarreyes
      [ copy ]   [ edit ]   [ send email — confirm (guessed) ]
```

**Displayed fields (hard requirement):** full name, position/title, email (+status
badge), phone number, LinkedIn URL, `match_reason` chip, editable subject + draft.

- **[Find contacts]** → `POST /api/network` → runs `service.find_contacts_for_job`; live
  status via the existing command-log pattern; contacts render as they land.
- **[Send email]** → `POST /api/outreach/send` → sends via Gmail (see §8). Verified
  emails send after one confirm; **guessed/unverified require an extra explicit confirm**.
  Row flips to **✅ sent {timestamp}** (or **failed** with the error).
- **[copy]** / **[edit]** for manual sending or tweaking the draft first.

New endpoints: `POST /api/network` (run for a job), contacts folded into the existing
job payload (`GET`), `POST /api/outreach` (regenerate/save a draft),
`POST /api/outreach/send` (send one email).

---

## 8. Gmail send automation (`gmail_send.py`)

A dashboard **Send** button that actually emails the contact from your Gmail.

**Auth (pick one; detected at runtime):**
1. **SMTP app-password (default, zero new deps).** `smtplib` (stdlib) + a Gmail
   **App Password**. Set `GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD` (and optional
   `OUTREACH_FROM_NAME`) in `.env`. Simplest and most reliable for a local tool;
   requires 2-Step Verification on the account.
2. **Gmail API OAuth (optional upgrade).** `google-api-python-client` + OAuth; sends
   as the user, better threading/deliverability. Reuses the same OAuth the wired Gmail
   MCP performs (token under `~/.gmail-mcp/`). Added only if the app-password path
   proves insufficient.
3. **Existing Gmail MCP** (`@gongrzhe/server-gmail-autoauth-mcp`, already in the apply
   stack, `send_email` already allowed) — available for an agent-driven send, but heavier
   than a direct SMTP call for a button.

**Send flow:** build a MIME message (from = `GMAIL_ADDRESS`, reply-to = same,
`OUTREACH_FROM_NAME` for display), send, record `sent_at` + `sent_message_id`, set
`outreach_status='sent'`. On failure record `send_error`, status `'failed'`.

**Safeguards (sending is outward-facing and unreversible):**
- **User-initiated only** — never auto-sends; one click + confirm per email.
- **Verified-email gate** — `email_status='guessed'|'locked'` requires a second explicit
  confirm; never silently emails a guessed address.
- **Daily cap** — `OUTREACH_DAILY_LIMIT` (default 20) across all jobs; refuses beyond it.
- **De-dupe** — never send twice to the same contact for the same job (`sent_at` guard).
- **Dry-run** — `applypilot network --dry-run` / a dashboard toggle logs the email
  instead of sending, for review.
- **Footer** — appends a short, honest sign-off; no misleading headers.

---

## 9. Config / tiers

- **`APOLLO_API_KEY`** → gates contact discovery (like the LLM key gates Tier 2).
- **`GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD`** → enable the Send button (SMTP path).
  `OUTREACH_FROM_NAME`, `OUTREACH_DAILY_LIMIT` optional.
- `doctor` reports: Apollo key, Gmail-send readiness, LinkedIn-fallback readiness.
- LinkedIn fallback requires Tier 3 (Claude CLI + Chrome + Node) — same as apply.
- `NETWORKING_LINKEDIN=0` and `--no-linkedin` disable the browser path.

---

## 10. Phased rollout

1. **Phase 1 — Apollo core (ship first):** `apollo.py` + `store.py` + `rank.py` +
   `service.py` (Apollo-only) + `applypilot network` CLI. Contacts (name, title, email,
   phone, LinkedIn) land in the DB.
2. **Phase 2 — Dashboard contacts:** populate the current operator dashboard with the
   contact fields + "Find contacts" button + endpoints. (Core requirement.)
3. **Phase 3 — Outreach drafting:** `outreach.py` + editable subject/body in the dashboard.
4. **Phase 4 — Gmail send:** `gmail_send.py` (SMTP app-password) + "Send email" button +
   `POST /api/outreach/send` + safeguards (confirm, verified-gate, daily cap, dry-run).
5. **Phase 5 — LinkedIn fallback:** `linkedin_agent.py` + `prompt.py`, gated + capped.
6. **Phase 6 (future):** optional Gmail API OAuth path, threaded follow-ups, reply
   tracking, opt-in auto-send-after-apply.

Each phase is independently useful and testable.

---

## 11. Testing

- **Pure/unit:** `rank.py` selection (title-similarity + role mix), title→`person_titles`
  synonym mapping, contact dedupe/merge, `store.py` upserts. Apollo client with mocked
  HTTP (search masked → enrich reveal). `gmail_send.py` MIME assembly + daily-cap /
  verified-gate / dedupe logic with SMTP stubbed. No network in tests.
- **Integration (gated):** real Apollo call and a real Gmail send-to-self behind
  env-flag tests (skipped in CI), mirroring the gated Node-render test.
- **LinkedIn agent:** prompt-builder unit test; the agent run itself is manual/opt-in.

---

## 12. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| Apollo credit burn | Reveal only selected 3–5; `--per-job` cap; log remaining credits |
| Apollo coverage gaps (small companies) | LinkedIn fallback; graceful "no contacts found" |
| LinkedIn ToS / bans | Opt-in, ≤5/company, read-only, cooldowns, `--no-linkedin`, off-by-default flag |
| **Emailing the wrong / guessed address** | `email_status` badge; guessed needs 2nd confirm; never silent-send; dry-run |
| **Cold-email spam / sender reputation** | User-initiated + confirm per send; daily cap; no bulk; honest footer; de-dupe |
| Gmail app-password security | Stored only in the local `.env`; never logged; SMTP over TLS |
| PII handling | Contacts stored locally in the user's own DB only; no external sync |

---

## 13. Open questions

- Apollo plan/credit budget per run (affects default `--per-job` and reveal policy).
- Should networking auto-trigger after a successful apply, or stay manual (button/CLI)?
- `OUTREACH_DAILY_LIMIT` default (proposed 20) and per-company send cap.
- Gmail auth preference: app-password (default, simplest) vs Gmail API OAuth.

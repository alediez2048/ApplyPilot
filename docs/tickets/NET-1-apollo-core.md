# NET-1 — Apollo core: contact discovery + `applypilot network` CLI

**Phase:** 1 · **Size:** L · **Depends on:** — · **Status:** Todo
**PRD:** §3.1, §3.2, §3.3, §4 · **Tier gate:** `APOLLO_API_KEY`

## Summary
Given an applied/prepared job, find 3–5 relevant people at the company via the Apollo
API, reveal contact info for the selected few, and persist them to a new `contacts`
table. Expose via a new `applypilot network` CLI verb. No dashboard, no drafting, no
sending yet — this ticket is the data spine everything else builds on.

## Scope / tasks
- [ ] **`networking/store.py`** — `contacts` table (schema per PRD §3.1) with forward-only
      migration (mirror `database.ensure_columns`). Helpers: `upsert_contact`,
      `get_contacts_for_job`, `contact_id(job_url, linkedin_url|name)`.
- [ ] **`networking/apollo.py`** — Apollo client (`X-Api-Key` header):
  - [ ] `resolve_org(company_name) -> {org_id, domain}` (org enrich/search)
  - [ ] `search_people(org, titles, keywords, per_page) -> [candidate]` (masked; cheap)
  - [ ] `enrich_person(apollo_id, reveal_email, reveal_phone) -> {email, email_status, phone}`
  - [ ] credit-aware: log remaining-credit headers; only called for selected contacts
- [ ] **`networking/rank.py`** (pure) — `select(candidates, role, n)`:
      title-similarity to the role + a useful mix (peers + ≥1 recruiter/hiring manager);
      returns ordered top-N with a `match_reason`.
- [ ] **title synonyms** — `role_to_person_titles(job_title) -> [str]` (static map +
      always add recruiter/talent titles). Small; LLM expansion optional/off by default.
- [ ] **`networking/service.py`** — `find_contacts_for_job(job, per_job=5, use_linkedin=False)`:
      resolve org → search → rank → enrich top-N → upsert. Returns stored contacts.
- [ ] **CLI** `applypilot network` in `cli.py`: `--url`, `--per-job` (5), `--limit`,
      `--no-linkedin` (default no-op here; wired in NET-5), `--dry-run` (skip reveal).
      Gate behind `APOLLO_API_KEY` via `check_tier`-style guard.
- [ ] **`config.py`** — `APOLLO_API_KEY` accessor; `.env.example` entry.
- [ ] **`doctor`** — report Apollo key presence.

## Data contract (Apollo → contact row)
`{full_name, title, company, linkedin_url, email, email_status, phone, location,
seniority, apollo_id, match_reason, source='apollo'}` → `contacts` table.

> ⚠️ Verify exact Apollo endpoint paths/params against current Apollo API docs before
> implementing (`/v1/mixed_people/search`, `/v1/people/match`, org enrich). Wrap all
> HTTP in try/except → return partial/empty rather than crashing the pipeline.

## Acceptance criteria
- `applypilot network --url <Affirm URL>` finds ≥1 real contact and writes rows to
  `contacts` with name, title, LinkedIn, and (for revealed ones) email/phone.
- Reveal is called **only** for the selected `--per-job` contacts (verified by log/credit).
- `--dry-run` searches + ranks but performs **no** reveal (0 credits spent).
- Runs cleanly with no `APOLLO_API_KEY` → clear "set APOLLO_API_KEY" message, exit 1.
- No changes to existing pipeline behavior; `applypilot status`/`run`/`apply` unaffected.

## Tests
- `rank.select` ordering + role-mix (pure).
- `role_to_person_titles` mapping.
- `apollo.py` with mocked httpx: search→masked, enrich→revealed; error paths return empty.
- `store` upsert/idempotency (same person twice → one row, updated).
- Gated integration test (env flag) hitting real Apollo, skipped in CI.

## Out of scope
Dashboard UI (NET-2), drafting (NET-3), sending (NET-4), LinkedIn (NET-5).

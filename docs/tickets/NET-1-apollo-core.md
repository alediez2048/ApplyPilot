# NET-1 — Apollo core: contact discovery + `applypilot network` CLI

**Phase:** 1 · **Size:** L · **Depends on:** — · **Status:** Todo
**PRD:** §3, §3.1, §3.2, §3.3, §4 · **Gate:** paid Apollo plan + **master** `APOLLO_API_KEY`
**Revised** after review (B1–B5, B11, B13).

## Summary
Given an applied/prepared job, derive the real employer, find 3–5 relevant people at that
company via the Apollo API, enrich the selected few (verified email + LinkedIn URL), and
persist them to a new `contacts` table. Expose via `applypilot network`. No dashboard,
drafting, or sending yet.

## Blockers this ticket must resolve first
- **B1 — `company` is not stored.** The pipeline keeps the job-board name in `site`
  (jobspy discards the parsed company; `store_jobs` has no company param). Apollo would get
  "Indeed" as the employer. **Fix before any Apollo call.**
- **B2 — no phone.** Apollo delivers phone async via webhook; a localhost tool can't receive
  it. Phone is **not** in this ticket's contract.
- **B3 — paid + master key.** Free tier has no API access; people-search needs a master key.
- **B4 — corrected Apollo endpoints** (base `https://api.apollo.io/api/v1`, `api_search`,
  `q_organization_domains_list[]`, enrich via `people/bulk_match`).
- **B5 — `contacts` is its own table**, not `_ALL_COLUMNS`.

## Scope / tasks
- [ ] **`company` column** — add to `database._ALL_COLUMNS` (auto-migrates the `jobs` table).
      Populate in the jobspy store path (`store_jobs`) and dashboard `_infer_company`.
      `derive_company(job) -> str`: JSON-LD `hiringOrganization` → careers hostname →
      LLM-from-`full_description` → fallback (`site`). `derive_domain(job) -> str|None` from
      company / `application_url` hostname.
- [ ] **`networking/store.py`** — owns the `contacts` table (schema per PRD §3.1):
  - [ ] `init_contacts()` / `ensure_contacts_columns()` (independent of `jobs`); called from
        startup, dashboard server, and the `network` CLI
  - [ ] `contact_id(job_url, linkedin_url, name)` = sha1 of **delimited** parts (`\x1f`)
  - [ ] `upsert_contact`, `get_contacts_for_job`; identity never switches once stored
  - [ ] indexes on `job_url`, `email`, `(outreach_status, submitted_at)`
- [ ] **`networking/apollo.py`** (header `X-Api-Key`, base `https://api.apollo.io/api/v1`):
  - [ ] `search_people(domain|org_ids, titles, seniorities, keywords, per_page)` →
        `POST /mixed_people/api_search`; returns masked candidates (name, title, seniority,
        `id`; **no email/LinkedIn**)
  - [ ] `bulk_enrich(ids, reveal_personal_emails=True)` → `POST /people/bulk_match` →
        `{email, email_status, linkedin_url}`; **consumes credits** — selected 3–5 only
  - [ ] optional `company_search(name)` → `organization_ids[]` (avoid `organizations/enrich`,
        which costs a credit); prefer `q_organization_domains_list[]`
  - [ ] `probe() -> bool` against a cheap auth/usage endpoint (403/master-key detection)
  - [ ] credit-aware: log remaining-credit headers
- [ ] **`networking/rank.py`** (pure) — `select(candidates, role, n)`: title/seniority
      similarity + role mix (peers + ≥1 recruiter/hiring manager); returns top-N with
      `match_reason`. **Ranks on title/seniority only** (no LinkedIn URL pre-enrichment).
- [ ] **title synonyms** — `role_to_person_titles(job_title) -> [str]` (static map + always
      add recruiter/talent titles).
- [ ] **`email_status` mapping** — Apollo `verified→verified`; other-with-address→`unverified`;
      no address→`none`.
- [ ] **`networking/service.py`** — `find_contacts_for_job(job, per_job=5, use_linkedin=False)`:
      derive company/domain → search → rank → `bulk_enrich` top-N → map status → upsert.
- [ ] **CLI** `applypilot network` (`cli.py`): `--url`, `--per-job` (5), `--limit`,
      `--no-linkedin` (no-op here; NET-5), `--dry-run` (search+rank, **no** reveal).
- [ ] **`config.py`** — `require_apollo_key()` helper (mirrors `check_tier` stderr +
      `SystemExit(1)`; runs `apollo.probe()`); `.env.example` gains `APOLLO_API_KEY`.
- [ ] **`doctor`** — Apollo readiness via live probe (not mere env presence).

## Contact row written
`{full_name, title, company, linkedin_url, email, email_status, location, seniority,
apollo_id, match_reason, source='apollo'}` (no `phone`).

## Acceptance criteria
- `applypilot network --url <Affirm URL>` derives **"Affirm"** (not "greenhouse"/"Indeed"),
  finds ≥1 person, and writes rows with name, title, and (for enriched) verified email +
  LinkedIn URL.
- Reveal (`bulk_match`) runs **only** for the selected `--per-job` (verified via log/credit).
- `--dry-run` → search + rank, **zero** reveals/credits.
- No/invalid Apollo key → `require_apollo_key()` prints an actionable message (paid + master)
  and exits 1; `doctor` shows a red probe result, not a false green.
- Fresh DB: `get_contacts_for_job` never raises "no such table" (own `init_contacts`).
- No change to existing pipeline behavior.

## Tests (mocked; no network in CI)
- `derive_company`/`derive_domain` across JSON-LD / hostname / fallback.
- `rank.select` ordering + role mix; `role_to_person_titles`.
- `apollo` with mocked httpx: search→masked, `bulk_match`→revealed; 403 path → probe false.
- `email_status` mapping; `contact_id` delimiter/collision; `store` upsert idempotency.
- **Gated integration** (env flag, skipped in CI): real Apollo call on the Affirm job.

## Out of scope
Dashboard (NET-2), drafting (NET-3), sending (NET-4), LinkedIn (NET-5), phone (NET-6).

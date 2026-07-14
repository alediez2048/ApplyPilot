# NET-2 — Dashboard contacts panel

**Phase:** 2 · **Size:** M · **Depends on:** NET-1 · **Status:** Todo
**PRD:** §7 · **Core requirement**

## Summary
Populate the **current** operator dashboard with the contacts found in NET-1. Each
applied job gets a "People at {company}" section listing full name, position, email
(+status badge), phone, and LinkedIn, plus a "Find contacts" button to trigger discovery.

## Scope / tasks
- [ ] **Job payload** — extend the dashboard's per-job JSON (`web_dashboard.py`) with
      `contacts: [...]` via `store.get_contacts_for_job(job_url)`.
- [ ] **UI** — render a "People at {company}" section per job card. Per contact:
  - [ ] full name (bold), position/title
  - [ ] email + status badge (✅ verified / ⚠ guessed / 🔒 locked / —)
  - [ ] phone (or —), LinkedIn link (opens in new tab)
  - [ ] `match_reason` chip (same role / recruiter / hiring manager / same team)
  - [ ] `[copy]` (copies email; whole block later in NET-3)
- [ ] **Endpoint** `POST /api/network` — body `{url, per_job}` → runs
      `service.find_contacts_for_job` (background, like the prepare flow); streams status
      into the existing command-log; contacts appear on refresh/poll.
- [ ] **Button** — "Find contacts" per job → calls the endpoint; disabled while running;
      shows count when done ("5 contacts").
- [ ] **Empty/err states** — "No contacts found", "Apollo key missing", spinner while running.

## Acceptance criteria
- Clicking "Find contacts" on the Affirm job populates its card with real people showing
  name, title, email(+badge), phone, LinkedIn.
- Contacts persist across dashboard reloads (read from `contacts` table).
- No Apollo key → button shows a clear "set APOLLO_API_KEY" message, no crash.
- Existing dashboard views (applied tracker, URL import, prepare) unchanged.

## Tests
- `store.get_contacts_for_job` shape.
- Endpoint handler: mock `service.find_contacts_for_job`, assert rows returned + status.
- Manual: click-through on the real Affirm job in `~/applypilot-local`.

## Out of scope
Draft display/editing (NET-3), send button (NET-4).

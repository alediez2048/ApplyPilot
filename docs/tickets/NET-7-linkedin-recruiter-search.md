# NET-7 - LinkedIn manual contact assist

**Phase:** 7 · **Size:** M · **Depends on:** NET-1, NET-2 · **Status:** Built
**PRD:** Pivoted experiment · **Gate:** Apollo only
**Intent:** Use LinkedIn People search without automating LinkedIn.

## Summary
Add a People-tab workflow that opens a normal LinkedIn People search for
`"{company}"`, then lets the operator paste selected contact rows back into
ApplyPilot. ApplyPilot parses those pasted identities, Apollo-enriches them, dedupes them,
stores contacts, and drafts outreach.

This replaces the browser-agent recruiter-search approach. LinkedIn remains a manual research
surface; ApplyPilot automates only the reliable parts: enrichment, verification, persistence,
dedupe, and drafting.

## Thesis to measure
The operator believes LinkedIn's top People results for company searches are a useful contact
source. Recruiter-like titles remain measurable as `source='linkedin_manual_recruiter'`, while
other operator-selected rows are stored as `source='linkedin_manual'`.

## Product Flow
1. The operator opens a job row's People tab.
2. They click **Open LinkedIn people search**.
3. LinkedIn opens in the operator's browser with a People search for `"{company}"`.
4. The operator copies one or more contact rows.
5. They click **Paste contacts from LinkedIn**.
6. They paste lines like `Name - Title - https://www.linkedin.com/in/profile`.
7. ApplyPilot parses the rows, dedupes known contacts, enriches with Apollo, stores contacts,
   tags recruiter-like rows separately, and drafts outreach.

## Scope / Tasks
- [x] **Dashboard UI**
  - [x] Add **Open LinkedIn people search** link in the People tab.
  - [x] Add **Paste contacts from LinkedIn** form.
  - [x] Preserve the existing **Find contacts** button.
  - [x] Do not require LinkedIn automation consent/login.
- [x] **Dashboard endpoint**
  - [x] `POST /api/network/linkedin-contacts`
  - [x] Keep `/api/network/linkedin-recruiters` as a compatibility alias.
  - [x] body: `{url, text}`
  - [x] parse immediately and return the import result.
- [x] **Networking service**
  - [x] parse forgiving pasted rows.
  - [x] enrich through `apollo.match_by_identity`.
  - [x] dedupe by normalized LinkedIn URL or name.
  - [x] preserve LinkedIn-only contacts when Apollo finds no email.
  - [x] store recruiter-like rows as `source='linkedin_manual_recruiter'`.
  - [x] store other operator-selected rows as `source='linkedin_manual'`.
  - [x] draft outreach through the existing `_draft_and_store` path.
- [x] **Remove obsolete browser-agent recruiter path**
  - [x] no recruiter-specific Claude/Playwright prompt.
  - [x] no recruiter `linkedin_agent.find_recruiters`.
  - [x] no doctor readiness line for automated recruiter search.

## Data Contract
No schema change is required.

Contact rows written by this path:

Recruiter-like rows:

`{full_name, title, company, linkedin_url, email, email_status, location, apollo_id,
match_reason='manual LinkedIn recruiter import', source='linkedin_manual_recruiter'}`

Other operator-selected rows:

`{full_name, title, company, linkedin_url, email, email_status, location, apollo_id,
match_reason='manual LinkedIn import', source='linkedin_manual'}`

## Safeguards
- No LinkedIn scraping, typing, clicking, messaging, profile opening, or automated browser agent.
- Apollo credits are spent only for rows the operator pasted.
- Recruiter title detection is attribution only. The operator is allowed to import non-recruiter
  people LinkedIn ranked highly for the company.
- Duplicate people already on the job or another role at the employer are skipped before Apollo
  enrichment where possible.
- LinkedIn-only rows are kept so the operator can still use the profile.

## Acceptance Criteria
- People tab shows both normal **Find contacts** and manual LinkedIn contact assist.
- Clicking the LinkedIn search link opens a query for `"{company}"`.
- Pasted recruiter rows create contacts with `source='linkedin_manual_recruiter'`.
- Pasted non-recruiter rows create contacts with `source='linkedin_manual'`.
- Apollo enrichment fills email and Apollo id when a match is available.
- Apollo misses still create contacts when a LinkedIn URL or name is present.
- Existing contacts are not duplicated.
- No LinkedIn automation consent/login is required for this workflow.

## Tests
- Parser test for `Name - Title - LinkedIn URL`, pipe-delimited rows, and URL-only rows.
- Service test for Apollo identity enrichment and source preservation.
- Service test for Apollo misses preserving LinkedIn-only contacts.
- Dedupe test by normalized LinkedIn URL/name.
- Render test for the People-tab LinkedIn search and paste controls.

## Manual Verification
1. Open a job's People tab.
2. Click **Open LinkedIn people search**.
3. Copy one or more contact rows from LinkedIn.
4. Click **Paste contacts from LinkedIn**.
5. Paste rows and click **Enrich with Apollo**.
6. Confirm contacts appear on the card and can use the existing outreach flow.

## Out Of Scope
- LinkedIn scraping or browser-agent automation.
- Sending LinkedIn connection requests/messages.
- Pagination automation.
- Scraping posts, activity, or "recently active" signals.

# UX-6 — Search that finds contacts and full descriptions

**Size:** S (~3h) · **Depends on:** nothing · **Status:** Todo
**Reported:** 2026-08-04 ("only filtering by job name")

## Diagnosis

Not quite only job name — it already searches nine fields:

```js
// dashboard.js:366
const hay = [j.title, j.company, j.location, j.salary, j.description, j.status,
             j.url, j.application_url, ...jobTags(j).map(t => t.value)]
```

Two real gaps, and both match exactly what was reported:

**1. Contacts are not in the haystack at all.** `j.contacts` is on the wire — the People tab
renders from it — and the search never looks at it. Searching a recruiter's name returns
nothing even though the dashboard is displaying that name one click away. This is the whole
of "if I look for a contact's name, nothing shows up".

**2. `j.description` is a 900-char EXCERPT, not the description.** `_job_payload` deliberately
ships a truncated field because real descriptions run 4–10KB and this payload re-renders every
2.5 seconds. So a term that appears in paragraph six of a JD is unfindable, which is the whole
of "job description, nothing shows up".

## The tradeoff on #2

22 jobs × ~6KB = **~130KB added to every 2.5s refresh**, for a field used only while typing.
Three options:

| | |
|---|---|
| **(a) Ship the full description** | simplest; +130KB every 2.5s forever, and it grows with the job count |
| **(b) Search server-side** when the query is long enough | correct, costs a round trip per keystroke-batch and a new endpoint |
| **(c) Fetch descriptions once, lazily, and cache client-side** | one extra request per session; search stays instant; nothing added to the hot path |

**Recommendation: (c).** The Job tab already fetches a full description on demand, so the
endpoint exists. Pull all of them once on first search, keep them in a `Map`, and the 2.5s
refresh is untouched. It also degrades honestly — until the fetch lands, search covers the
excerpt, which is what it does today.

## Scope / tasks

- [ ] Add contacts to the haystack: `full_name`, `title`, `email`, `company`.
- [ ] **Say what matched.** When a job matches only via a contact, the row should show
      *"matched: Sarah Chen"* — otherwise it looks like a bug, since the visible row contains
      none of the search text.
- [ ] Lazy full-description cache per option (c), keyed by URL, invalidated on refresh only if
      the description changed.
- [ ] Keep the AND-across-terms behaviour: `google engineer` means both.
- [ ] Leave the search box as STATIC markup. `refresh()` replaces `#jobs` wholesale every
      2.5s; anything rendered into the toolbar is destroyed mid-keystroke (already documented
      in `index.html`).

## Tests

- [ ] `test_search_matches_a_contact_name` — the reported case.
- [ ] `test_search_matches_deep_in_a_full_description` — a term past the 900-char excerpt.
- [ ] `test_a_contact_match_says_so` — assert the "matched:" element EXISTS, not that some copy
      is right (§Lessons 41).
- [ ] `test_search_does_not_refetch_status` — typing must not hit `/api/status`; it re-filters
      `LAST_JOBS` (§Lessons 11, 26 — 50 SQL statements behind a keystroke).
- [ ] `test_the_query_budget_does_not_move`.

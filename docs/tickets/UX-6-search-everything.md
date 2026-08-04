# UX-6 — Search that finds contacts and full descriptions

**Size:** S · **Depends on:** nothing · **Status:** DONE 2026-08-04
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

- [x] Contacts in the haystack: `full_name`, `title`, `email`, `company`.
- [x] `matchedVia(j)` renders *"matched: Sarah Chen"* under the title, and ONLY when the row
      matched through a person — a row matched on its own fields must not claim one.
- [x] Option (c): `/api/job-descriptions` returns every posting in ONE query, fetched once per
      session on the first keystroke, into the `JOB_DESC` map the Job tab already uses.
      Measured live: **22 descriptions, 132KB, one request** — which is exactly the number
      option (a) would have added to every 2.5-second refresh, forever.
- [x] AND is now per TERM rather than per field: a term may be satisfied by the job OR by any
      one person, so `saronic sarah` matches even though no single field holds both. Adding a
      word still only ever narrows.
- [x] Search box untouched; still static markup.

### It degrades to the old behaviour, not to nothing

Until the warm-up lands, search covers the 900-char excerpt — which is what it did before. A
failed fetch resets the guard so the next keystroke retries.

## Tests

7 new tests in `tests/test_job_tags_and_search.py`, all running the real functions under node.
Mutation-verified: dropping contacts from the haystack, dropping the cached description,
removing the matched-via chip, and calling `refresh()` on every keystroke each kill a test.

`test_typing_never_refetches_the_status_payload` counts actual `fetch` calls across four
keystrokes: zero to `/api/status`, at most one to the bulk endpoint. §Lessons 11 and 26 — 50
SQL statements behind a keystroke.

`test_the_bulk_description_endpoint_is_one_query` also pins that the literal string `"null"`,
which scrapers write, never reaches search as text.

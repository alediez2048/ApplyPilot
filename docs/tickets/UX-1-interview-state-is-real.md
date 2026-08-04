# UX-1 — The interview state never reaches the browser

**Size:** S (~2h) · **Depends on:** nothing · **Status:** Todo
**Reported:** 2026-08-04, third time ("the button is still not doing anything")

## Diagnosis

The write works. The read does not exist.

```sql
-- repo/jobs.py:146  dashboard_rows()
SELECT url, title, site, salary, location, full_description, application_url, detail_error,
       fit_score, score_reasoning, tailored_resume_path, cover_letter_path,
       apply_status, apply_error, apply_attempts, applied_at,
       last_attempted_at, apply_duration_ms, rejected_at
```

`interview_at` is not in that list. Live DB: WRITER and Zendesk both carry an
`interview_at` — the click persisted correctly both times.

What silenced it:

```python
# web_dashboard.py:1633
"interview_at": (row["interview_at"] if "interview_at" in row.keys() else "") or "",
```

A defensive guard that turns a `KeyError` into a plausible empty string. Without it this
would have 500'd on the first render and been fixed in a minute.

Every symptom follows from `j.interview_at` being permanently falsy in JS:

| Symptom | Line |
|---|---|
| row never greys | `dashboard.js:1752` `${j.interview_at ? 'row-won' : ''}` |
| no 🎯 chip | `dashboard.js:1753` |
| Next never reads "Interview scheduled" | `dashboard.js:1955` |
| 🎯 button never disappears | `dashboard.js:2400` |
| ⋯ menu never offers the revert | `dashboard.js:2441` |

**Two earlier "fixes" were the wrong layer** — moving the button onto the row, then changing
the grey from #f8f9fa to #eef2ee. Both were real improvements to code that was never running.
§Lessons 46: check the artifact that actually ships. The payload was never read.

### Second defect: the dialog makes a false promise

> "The row greys out and **every follow-up sequence for this job stops**."

`interview_at` appears in **zero** Python follow-up paths — `domain/followup.py`,
`networking/touches.py`, `networking/service.py`, `tick.py`, `gmail_send.py`, `outreach.py`
all have 0 references. The dashboard *hides* due follow-ups for an interviewing job
(`dashboard.js:1427`) but the engine still considers them due, so `applypilot tick`, the send
path and "Send all emails" would fire anyway.

That is §Lessons 21 exactly: a derived state the dashboard computes and the CLI cannot see,
so the two disagree silently.

### Third defect: the revert exists but is unfindable

`unmarkInterview` is wired and correct — it lives in the `⋯` overflow menu, which is where
the 🎯 button itself was buried before it was reported missing. §Lessons 43, fifth occurrence.

## Scope / tasks

- [ ] Add `interview_at` to `dashboard_rows()`'s SELECT.
- [ ] **Delete the `if "interview_at" in row.keys()` guard.** A missing column must raise, not
      degrade. Audit the other payload keys for the same pattern while there.
- [ ] Make the ladder honour it: `interview_at` is a terminal state for every channel on that
      job. Decide the layer — cleanest is `sequences` gaining a `stopped_reason='won'` row per
      contact at mark time, so the existing terminal-state machinery does the work and `tick`
      inherits it for free.
- [ ] Put `↩ Not scheduled after all` on the row next to 🎯, not only in `⋯`.
- [ ] Backfill: WRITER and Zendesk are already marked; their sequences must stop on deploy.

## Tests

- [ ] `test_interview_at_is_in_the_dashboard_payload` — the regression. Assert the key is
      present AND non-empty for a marked job, via `_status_payload()`, not by reading the SQL.
- [ ] `test_no_payload_key_is_silently_optional` — scan `_job_payload` for
      `if "..." in row.keys()`; a column the payload needs belongs in the SELECT.
- [ ] `test_marking_an_interview_stops_every_channel` — mark, then assert `followup_panel`
      reports nothing due **and** that `tick` sends nothing. Test the engine, not the view.
- [ ] `test_unmarking_restores_the_ladder` — and that it is reachable from the row.

# UX-1 — The interview state never reaches the browser

**Size:** S · **Depends on:** nothing · **Status:** DONE 2026-08-04
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

### Second defect: ~~the dialog makes a false promise~~ — WRONG, corrected 2026-08-04

The original diagnosis claimed the ladders never stop, because `interview_at` appears in zero
Python follow-up paths. The grep was right and the inference was wrong: the stopping is
**imperative at mark time**, in `web_dashboard._mark_interview:1164`, and it is more careful
than the replacement written for it — it only halts channels with a real ladder
(`st["count"] or st["draft_body"]`), so a contact does not look deliberately closed on a
channel nobody ever used. Undo deliberately does NOT restart them, which is a documented and
tested decision (`test_undo_does_not_silently_restart_outreach`).

A cascade added in `repo.mark_interview` duplicated it, was worse, and contradicted that
decision by resurrecting a ladder that had been stopped by a REPLY. Reverted before it shipped;
two existing tests caught it immediately, which is what they were for.

**The payload was the whole bug.**

### Third defect: the revert exists but is unfindable

`unmarkInterview` is wired and correct — it lives in the `⋯` overflow menu, which is where
the 🎯 button itself was buried before it was reported missing. §Lessons 43, fifth occurrence.

## Scope / tasks

- [x] Add `interview_at` to `dashboard_rows()`'s SELECT.
- [x] **Delete the `if "..." in row.keys()` guards** — both of them. A missing column must
      raise, not degrade.
- [x] ~~Make the ladder honour it~~ — already did. See the correction above.
- [x] Put `↩ Not scheduled` on the row next to 🎯, not only in `⋯`.
- [x] Fix the undo's tooltip, which promised to "restart the sequences this stopped". It does
      not, deliberately. The same class of false promise this ticket exists to remove.
- [x] Verified live: WRITER and Zendesk now reach the browser with their timestamps.

## Tests

- [x] `test_interview_at_is_in_the_dashboard_payload` — asserted on `_status_payload()`, and
      it asserts the payload is non-empty first: the seed omitted `strategy`, so `QUEUE_SQL`
      excluded the job and the test measured nothing on its first run (§Lessons 13, in a test
      written to catch a bug).
- [x] `test_no_payload_key_is_silently_optional` — scans for the guard pattern, skipping
      comments.
- [x] `test_the_button_is_on_the_row_not_only_in_the_menu` — **sharpened**. It asserted on
      `stepStrip`, which CONTAINS the ⋯ menu, so a mutation moving the undo back into the menu
      passed. Now calls `interviewButton()` directly. It also had to match
      `onclick="markInterview(` rather than `markInterview`, because `unmarkInterview` contains
      it — §Lessons 1, in the test guarding the placement, for the second time in one file.
- [x] Mutation-verified: restoring the missing SELECT column kills two tests; moving the undo
      back into the menu kills one.

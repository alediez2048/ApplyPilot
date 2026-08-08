# SPACE-0 — The endless scroll is the description column, not the terminal rows

**Size:** S · **Depends on:** nothing · **Status:** DONE 2026-08-08
**PRD:** `docs/spaces-prd.md` §8.1 — **and this ticket is the authority where they disagree.**

## The prescribed fix does not survive the data

§8.1 says:

> *"The complaint that started this was 'endless infinite scroll'. The cause is that **nothing
> ever leaves**: applied, rejected, interviewing and expired rows render forever. A `Done`
> bucket collapsing everything terminal, on by default, fixes it."*

Measured on the live board before building it:

| | |
|---|---|
| jobs | 31 |
| terminal (rejected or interview) | **1** |
| applied and still in play | 28 |
| age of the applied pile | median **5 days**, max **19**, none past 21 |

A `Done` bucket would have hidden **one row of thirty-one** — about 2.5% of the page — and
"applied" is emphatically not terminal: every one of those 28 is inside a follow-up ladder that
runs 2/4/7 days by email and 5/12 by LinkedIn. Archiving them hides the active work.

§Lessons 28: measure the bug before fixing the bug the ticket describes. Two CRM tickets had
already shipped factually wrong instructions; this is the third.

## What the page actually was

Measured in a real browser, not reasoned about (§Lessons 46 — check the artifact that ships):

```
page                8,939px          ten screens
table               6,569px  (73%)   30 rows at 130px each
  the `desc` cell IS the row height — every other cell on the row holds 25-33 characters
  all 30 excerpts sat exactly on their 900-char cap
jobControls           349px
premiseControls       290px          second largest block — and added the same day, by CTX-1
stats / progress / accounts / metrics      177 / 160 / 82 / 53
```

So the scroll was two things, neither of them terminal rows.

**1. A six-line clamp.** `td.desc .desc-text` already carried `-webkit-line-clamp:6`. The lever
existed and was set wide.

Nothing is lost by shortening it. The first 900 characters of a posting are the mission
statement and the org chart — `domain/jobdesc.role_essentials` exists precisely because the role
itself starts around character 520 — so this column was six lines of *"About Acme, Acme is on a
mission to"*, thirty times. Recognising a row is done by title, company and tags; READING the
posting is what the Job tab is for.

**2. A write-once box rendered open forever.** The premise is typed once per campaign and then
read almost never, and CTX-1 had just put 290px of it above the table on every render.

## Scope / tasks

- [x] `-webkit-line-clamp: 6 → 2`. One value.
- [x] `#premiseBox` is a `<details>`, collapsed on the first render that finds a premise,
      **open while empty** — shipping it collapsed-by-default would rebuild the exact bug CTX-1
      existed to fix (§Lessons 43).
- [x] Collapses ONCE, not on every render. `refresh()` runs every 2.5s; auto-collapsing each
      time makes the box impossible to edit. `PREMISE_SETTLED` latches, and re-arms if the
      premise is cleared.
- [x] A `✓ in every draft here` marker on the summary. Collapsed with no marker reads as an
      empty box — the operator could not tell a written premise from a missing one without
      opening it.
- [x] Search untouched. The clamp is presentation; `j.description` still ships in full and
      UX-6's `matched: …` line still works.

## Result, same page and same method

| | before | after |
|---|---|---|
| page | 8,939px | **7,423px** (−17%) |
| table | 6,569px | **5,066px** (−1,503px) |
| data row | 130px | **89px** |
| premise box (with a premise) | 290px | **65px** |

## Not done, deliberately

**The `Done` bucket.** It is not wrong, it is *premature* — it becomes worth building when
terminal rows are a meaningful share of the table, and today they are one row. Revisit when
rejections accumulate; the trigger is a number, not a hunch.

**Smarter excerpts.** Showing `role_essentials` instead of the raw first 900 characters would
make the two visible lines actually informative. It is real work on the 2.5s path for 30 rows,
which is §Lessons 26's territory, and it is a content change rather than a height one. Separate
ticket if wanted.

## Tests

`tests/test_page_height.py`, 10 tests.

- [x] `test_the_description_is_clamped_to_two_lines` — asserts the number, so widening it back
      fails.
- [x] `test_the_clamp_still_actually_clamps` — `-webkit-line-clamp` does nothing without
      `display:-webkit-box` and `overflow:hidden`. **A mutation switching the display to `block`
      — which disables the clamp and renders all 900 characters — survived the first version**,
      because it asserted `"-webkit-box" in rule` and `-webkit-box-orient` contains that
      substring. §Lessons 1, inside the test written to guard the clamp.
- [x] `test_it_starts_open_so_it_cannot_become_invisible`.
- [x] `test_the_refresh_does_not_slam_it_shut_while_editing` and
      `test_clearing_the_premise_re_arms_the_collapse` — the second guards the first, which
      would pass against a latch that never released.
- [x] `test_the_summary_heading_is_not_left_block_level` — an `<h2>` inside a `<summary>` puts
      the marker on its own line unless the summary is flex and the h2's margins are cleared.
- [x] **Mutation-verified, 9 of 9 killed** after the fix above.

Suite **1649 passed, 1 skipped**. ruff and eslint clean. Verified live on both a Space with a
premise and one without.

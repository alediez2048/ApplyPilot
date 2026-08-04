# UX-2 — Retire the Interactions tab, keep the ledger

**Size:** S · **Depends on:** nothing — the constraint below is met by MOVING, not deferring
**Status:** DONE 2026-08-04 · **Reported:** 2026-08-04 ("offering no real value")

## Diagnosis

Correct, and the numbers say why. The whole `interactions` table holds **2 rows**, both
`booked`, across 187 contacts and 22 jobs. So the tab renders "Nobody has engaged yet" on
almost every job, plus a five-line explanatory note about what is and is not detectable.

The tab is a **container for a signal that mostly does not exist**. Its content — a per-person
timeline — is also the wrong shape: engagement belongs next to the person, not in a separate
room you have to remember to visit.

**But do not delete the capability.** `interactionsPane` holds the app's only manual-log
button ("🔗 Note: they viewed my LinkedIn"), and the `interactions` store is the correct home
for UX-2 (LinkedIn messages) and the source of truth for UX-3 (last interaction). Deleting the
tab and the ledger together would remove the foundation of the next two tickets.

This is the §Lessons 35 tab — the one that answered its own question "yes" by counting our own
LinkedIn invites as engagement. It was corrected to be honest, and being honest revealed there
was nothing to show.

## Scope / tasks

- [x] Removed `interactions` from the tab list and the `jobPane` dispatch.
- [x] Deleted `interactionsPane()` and the six CSS rules only it used.
- [x] **Kept** `_interactions_for_job`, `domain/interactions.py`, `interactions_store.py`,
      `/api/contact/interaction` and `logInteraction()`.
- [x] `_attach_interactions()` hangs each person's rows on that person in the payload; the
      job-level `interactions` key is gone. Same single query — verified by a test that counts.
- [x] `engagementLog(c)` renders on the contact's **🔗 LinkedIn** tab, with the log button.
- [x] The detected-vs-noted explanation is one line under the button instead of a paragraph
      per job.

### Live after the change

87 people now carry engagement rows on their own card. The tab that displayed this had two
rows in its own table across all of them, because it only counted `interactions` — the derived
signals (a reply, a deck open) were computed at render time and never stored, which is
deliberate (`test_derived_facts_are_not_copied_into_the_table`) and is why the tab looked
empty while the data existed.

## Tests

- [x] `test_the_interactions_tab_is_gone` — and that removing it took no other tab with it.
- [x] `test_engagement_still_renders_on_the_person` — asserts the ROW exists, not that some
      copy is right (§Lessons 41).
- [x] `test_the_only_manual_log_button_survived_the_move`.
- [x] `test_a_person_with_no_engagement_says_so` — an empty block reads as broken.
- [x] `test_engagement_is_attached_to_the_contact_not_the_job` — payload-level.
- [x] `test_attaching_it_is_still_one_query_for_the_whole_job` — the N+1 guard.
- [x] Mutation-verified: restoring the tab, dropping `engagementLog`, and removing
      `_attach_interactions` each kill a test.

**Note on the second payload test:** its first run asserted against an empty payload, because
the fixture creates no job and `QUEUE_SQL` only returns operator-added rows. Caught by the
"empty payload" guard in the test itself — §Lessons 13, which is why that guard is written
before the real assertions.

# UX-2 — Retire the Interactions tab, keep the ledger

**Size:** S (~2h) · **Depends on:** UX-2 (do not delete the only logging affordance first)
**Status:** Todo · **Reported:** 2026-08-04 ("offering no real value")

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

- [ ] Remove `interactions` from the tab list (`dashboard.js:2021`) and the
      `t === 'interactions'` branch (`:2187`).
- [ ] Delete `interactionsPane()` and its CSS.
- [ ] **Keep** `_interactions_for_job`, `domain/interactions.py`, `interactions_store.py`,
      `/api/contact/interaction` and `logInteraction()`.
- [ ] Move the per-person engagement rows into the expanded contact card, under the channel
      tabs, where the rest of that person's history already lives.
- [ ] Move the "profile view" log button onto the contact's **🔗 LinkedIn** tab (UX-2 gives it
      neighbours there).
- [ ] Keep the "what is detected vs noted" note, once, on the LinkedIn tab rather than per job.

## Tests

- [ ] `test_the_interactions_tab_is_gone` — asserted on the rendered tab list.
- [ ] `test_engagement_still_renders_on_the_contact_card` — the events survive the move. Assert
      the ROW exists, not that some copy is present (§Lessons 41).
- [ ] `test_the_interaction_endpoint_still_works` — the store outlives its tab.

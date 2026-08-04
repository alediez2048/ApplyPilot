# UX-5 — Application temperature

**Size:** M (~1d) · **Depends on:** UX-4 (last interaction is an input) · **Status:** Todo

## Diagnosis

Nothing today says how an application is *doing*. The status strip says how far it has
**travelled** (Found → Applied → Emailed → Follow up → Reply), which is a measure of effort,
not of health. Two jobs both reading "Emailed 4/4 · Follow up 2/4" can be a live conversation
and a dead one.

**The trap this must not fall into is documented.** §Lessons 35: the first Interactions tab
counted our own LinkedIn invites as engagement and every job read "3/3 engaged". The honest
number was 2 of 58. A temperature built on effort would do the same thing with a colour.

**So: temperature counts THEIR actions, decayed by time, penalised by our unanswered effort.**
`domain/interactions.py` already states the principle — our own actions are context, never
engagement.

Signals available today, all of them theirs:

| Signal | Weight | Source |
|---|---|---|
| reply | strongest | `messages` inbound, `contacts.replied_at` |
| call booked | strongest | `interactions` kind=`booked` |
| intro-deck opened | medium | `contacts.deck_views`, `deck_last_at` |
| LinkedIn message in | medium | UX-3 |
| bounce | negative, terminal | `contacts` send_error |
| touches spent with no reply | negative, compounding | `touches` |
| days since last inbound | decay | UX-4 |

## Design

- [ ] Four bands, not a percentage: **warm · active · cooling · cold** (plus **won** for
      interview/booked, which is terminal and not on the scale). A percentage implies a
      precision that is not there and invites tuning instead of acting.
- [ ] **The band shows its reason on hover** — "cooling: 3 follow-ups, no reply in 9 days".
      An unexplained colour gets ignored within a week.
- [ ] Colour is not the only channel. A dot plus a word: colour-blind readers, and a screenshot
      pasted into a doc, both still work.
- [ ] Rejected and won are excluded from the scale, same as the 🔔 counter — a permanently lit
      indicator trains you to ignore it.
- [ ] Renders on the collapsed row AND in the summary strip.

## Explicitly not

- No score out of 100, no "AI health score". The inputs are six booleans and a clock.
- No lead scoring of PEOPLE (`crm-prd.md` §3). This scores an application, not a human.
- No email-open tracking as an input — rejected outright; a pixel measures Gmail's proxy,
  Apple Mail's prefetch and corporate scanners. A click does not.

## Tests

- [ ] `test_our_own_effort_never_raises_the_temperature` — the §Lessons 35 mutation. Send 12
      emails, receive nothing, assert it reads colder than a job with 1 email and 1 reply.
- [ ] `test_a_reply_beats_everything` — one reply outranks any amount of outbound.
- [ ] `test_it_decays` — the same reply at 2 days and 40 days lands in different bands.
- [ ] `test_a_bounce_is_terminal_not_cold`.
- [ ] `test_every_band_states_its_reason` — a band with no explanation is a colour.

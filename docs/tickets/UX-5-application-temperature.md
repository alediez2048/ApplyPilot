# UX-5 — Application temperature

**Size:** M · **Depends on:** UX-3 · **Status:** DONE 2026-08-04

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

- [x] Bands, not a percentage: **warm · active · cooling · cold**, plus three that are not
      temperatures at all — **won**, **undeliverable**, **new**.
- [x] Every band carries the sentence that produced it, in the `title`.
- [x] A dot AND a word, so colour is never the only channel.
- [x] A rejected job gets `None` — no reading at all rather than a permanent grey chip.
- [x] On the collapsed row, before the last-interaction line.

### Two bands the ticket did not anticipate

**`undeliverable`** — a bounce is not cold. "Nobody is answering" and "nothing is arriving"
have opposite fixes, and calling the second one cold hides an address to correct. Only when
EVERY emailed address on the job is bouncing; one bad address among several is not the job's
problem.

**`new`** — a job imported this morning has sent nothing and is not failing. Without it the
whole table reads amber on day one, which trains the operator to ignore the colour. Six of the
current 22 are in this state.

### Live reading, first run

    cooling 10 · new 6 · warm 4 · won 1 · cold 1

    Betterup   cold     12 messages sent, no answer from anyone in 14 days.
    Salesforce warm     Gina replied 4d ago.
    Zendesk    warm     Anna viewed your profile today.
    WRITER     won      An interview is scheduled.

Betterup being the only `cold` is the number worth looking at: 12 messages into total silence.

## Explicitly not

- No score out of 100, no "AI health score". The inputs are six booleans and a clock.
- No lead scoring of PEOPLE (`crm-prd.md` §3). This scores an application, not a human.
- No email-open tracking as an input — rejected outright; a pixel measures Gmail's proxy,
  Apple Mail's prefetch and corporate scanners. A click does not.

## Tests

17 tests in `tests/test_temperature.py` plus a render test. Mutation-verified: letting our own
sends count as engagement, removing the decay, treating a bounce as cold, and dropping the
reason each kill a test.

`test_every_band_states_its_reason` also asserts the cases produce **at least five distinct
bands** — otherwise a version where everything collapses to one band passes every other
assertion in the file.

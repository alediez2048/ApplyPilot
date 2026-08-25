# PRD — Nudgy: the CRM as a creature you can disappoint

**Status:** Draft v1 · 2026-08-25
**Owner:** Jorge · **Author:** Jorge + Claude
**Ticket prefix:** `NUDGE-*`
**Branch:** `context`
**Audience shift:** this document assumes the core user is a **graduating student or recent
grad**, not a mid-career operator. §2.4 records what that changes and what it costs.

**Relationship to the other PRDs:**

| Document | Relationship |
|---|---|
| `outreach-context-prd.md` | CTX built four tiers of operator context. **This PRD measures how empty they are** (§1.3) and is the first thing in the product with a reason to ask you to fill them. Nudgy is CTX's missing incentive. |
| `crm-prd.md` | person-as-root. Nudgy's *assistant* half is a consumer of that graph, never a competitor to it. |
| `spaces-prd.md` | Nudgy is **global**, deliberately unlike every other panel (§3.4). |
| Knowledge-graph work (separate session, in flight) | §8. The graph may inform what Nudgy **says**. It may never decide how he **feels**. |

---

## Headline decisions (read first)

1. **Nudgy is CLINGY, and that is the design, not the flavour.** Every other signal in this
   product measures your *output* — emails sent, follow-ups due, replies received. Nudgy also
   measures **whether you showed up**. That is a fifth need (`SEEN`, §4.1) and it is the one
   that makes him a companion rather than a progress bar with a face.

2. **He bids for attention. He never blocks.** An attention-seeking mascot is one bad decision
   away from a dark pattern. The rule that prevents it: **ignored bids get sadder, never
   louder.** No modal, no interstitial, no sound by default, nothing that gates a click. He
   gets quieter and more tragic as he is ignored, which is the register we picked anyway and
   is the *opposite* of a nagging app.

3. **The state machine is the product. The renderer is swappable.** `domain/mascot.py` is pure
   and testable; the drawing reads a state vector and knows nothing about the CRM. Name the
   outputs the way Rive names its inputs and the renderer becomes a swap, not a rewrite (§7).

4. **Health measures promises KEPT, never volume SENT.** This is §Lessons 35 with a face on it.
   The first Interactions tab counted our own LinkedIn invites as engagement and every job read
   "3/3 engaged". A mascot that cheers up when you send more email is that bug with a heartbeat,
   and it would reward blasting — the one behaviour every other guard in this codebase exists to
   prevent (`OUTREACH_COMPANY_CAP`, `_PER_COMPANY_PER_POLL`, the cooldown).

5. **Two axes that behave differently. Mood decays; growth ratchets and never decays.**
   Neglect makes Nudgy sad. It never makes him dumber, smaller, or takes anything away. Growth
   is the accumulated context you typed, and it is permanent.

6. **Growth counts operator-typed context ONLY — never Apollo's.** 419 contacts carry a LinkedIn
   URL that Apollo handed over. That is not context you added, and counting it would let Nudgy
   grow fat on a credit card.

7. **Every line Nudgy says must be provable from a row in the database.** He is dry in *framing*
   and never invents a *fact*. Same rule as every prompt in this codebase (§Lessons 40, 42, 116)
   — a false claim rendered confidently is the failure mode, whether it is in an email or a
   speech bubble.

8. **`FLAT` is earned by two conditions at once and recovers in one tick.** A badge that is
   permanently lit is one you stop reading inside a week — the exact failure CRM-3a exists to
   prevent, and the reason the LinkedIn ladder was switched off at 32 of 43 due. Recovery must
   be faster than decay or this is a punishment machine.

9. **Zero new SQL statements and zero network calls on the 2.5s path — and the budget has NO
   headroom.** `CLAUDE.md` says "74 of 80, six statements spare". **That is stale.** Measured
   independently for this document with the suite's own fixture: **80 statements against
   `MAX_STATEMENTS = 80`, and the assertion is `<=`.** The suite passes at exactly the ceiling
   and fails at 81. `knowledge-graph-prd.md` Headline 4 measured the same thing and went
   further — against a copy of the live database the same payload costs **340 statements on
   `job-search` (43 jobs)** and **847 on `linkedin-hiring` (104 jobs)**, roughly 8 per job.
   **One added SELECT fails the suite immediately.** Nudgy's entire state is therefore a pure
   aggregation over lists `_status_payload` already holds in memory — see §9.2, which is now a
   load-bearing constraint rather than a nicety. §Lessons 26 is the second precedent: a per-job
   Gmail call took this endpoint to 2.4s while the SQL budget passed the whole time.

10. **The knowledge graph may inform what Nudgy SAYS. It may never decide how he FEELS.**
    If his mood depends on a graph that has not shipped, Nudgy does not exist until it does —
    §Lessons 31, the feature that only runs from a scheduler nobody installed.

11. **No runtime dependency in v1.** Inline SVG + CSS + the Web Animations API. The frontend is
    one classic `<script>`, no bundler, three static files, offline-first (§1.4).

12. **Disappointed, never cruel — and he never comments on OUTCOMES, only ACTIONS.** Rejections,
    ghostings and silence from employers are not his business. Whether you wrote back is. For a
    new grad four months into a bad search this is the line between a companion and a reason to
    close the tab.

13. **Labubu is not available; the register is.** Labubu is Kasing Lung's character, licensed to
    Pop Mart. The properties worth taking are decomposable and unowned: ugly-cute, a grin with
    too many teeth, earned variants, and a silhouette that reads at 16px (§5).

---

## 1. Baseline — what exists today

All figures measured against the live database on **2026-08-25**. They move within minutes of
real use; re-measure before reasoning from one.

### 1.1 The database

| Table | Rows |
|---|---|
| `jobs` | 207 |
| `contacts` | 542 |
| `touches` | 348 |
| `messages` | 771 |
| `sequences` | 150 |
| `connections` | 899 |
| `job_events` | 2,153 |
| `interactions` | 11 |
| `transcripts` | 3 |
| `spaces` | 6 |
| `ats_accounts` | 46 |

Schema version 4.

### 1.2 Outreach is HEALTHY, and this is the finding that shapes the whole design

```
82 touches sent in the last 7 days
last touch sent TODAY at 20:50
193 people contacted, 16 replies  (8.3%, upper quartile for cold outreach)
```

**A mascot whose health tracked outreach volume would be permanently content on this account.**
Permanently content is furniture. Within a week you stop looking at it, which is the same
failure as a permanently-lit badge, arrived at from the opposite direction.

### 1.3 One person has been waiting seven days

```
Anurag Panyala   replied 2026-08-18   7 days ago   0 messages back
```

Every other reply in the database was answered — Aaron (2), Elisabeth (3), Zoë (1), Theresa (1),
Alex (2), and eleven more. Sixteen replies, fifteen answered.

This single row is the argument for the entire design. On the same day a follow-up went out and
82 touches cleared in a week, **the one thing that actually mattered sat untouched.** Effort was
excellent; the outcome that was owed was not delivered. No existing surface makes that legible:
the 🔔 counter does list it, but it lists it *beside* 82 units of work that were done, where it
reads as one item among many rather than as the only one that counts.

### 1.4 Context — the tank that is actually empty

Operator-typed context across 207 jobs and 542 contacts:

| Field | Filled | Of |
|---|---|---|
| `contacts.noticed` | **2** | 542 |
| `contacts.notes` | **7** | 542 |
| `contacts.phone` (hand-typed) | 17 | 542 |
| `contacts.flagged_at` | 21 | 542 |
| `jobs.job_context` | **1** | 207 |
| `jobs.job_ask` | **1** | 207 |
| `transcripts` | 3 | — |
| `interactions` (operator-logged) | 11 | — |
| **Total** | **~63 items** | **across 749 rows** |

The `outreach-context-prd.md` cascade — identity → space → job → contact — was built, shipped,
tested and is **fed by almost nothing**. CTX-2 put a context box on every row; one row in 207
has anything in it.

This is the axis with room to move, and it is the axis the product's own value proposition rests
on: *the experience gets better as you add context.* Nothing in the product has ever asked.

### 1.5 The frontend, and what it will accept

Verified, not assumed:

- **One classic `<script>` tag.** `index.html:256`. Not a module; ~56 inline `onclick=`
  attributes resolve against the global object.
- **No bundler and no runtime dependencies.** `package.json` is dev-only (eslint, jsdom) and
  says so: *"Not a runtime dependency, not bundled, not shipped."*
- **Three static files**, `dashboard.js` already at 331 KB.
- **`#jobs` is replaced wholesale every 2.5s.** The guard is a string compare of the generated
  HTML. Anything stateful rendered inside it is destroyed on the next tick.
- **Stateful nodes are written INTO, never replaced** — `#todoCount`, `#todoList`. This is the
  pattern Nudgy must follow, and it is non-negotiable.
- **Localhost only, offline.** No CDN is available at runtime.

---

## 2. Problem

### 2.1 Everything in this product chases silence

Follow-up ladders, touch schedules, the coverage plan, the 🔔 counter — every mechanism was
built around people who said nothing. The rarer and far more valuable case is somebody who
**answered**, and the product's own history says so: §Lessons 27 was written when Gina Johnson
replied and the row still read "1 follow-up due".

The 🔔 counter fixed the ordering. It did not fix the *weighting*: a reply is item one in a list
of forty-nine, which is not the same as being the only thing that matters.

### 2.2 The context cascade has no incentive attached to it

Four tiers were built. One row in 207 uses the job tier. Nothing in the product ever says *this
would be better if you told me something*, and a text box with no consequence is a text box
nobody fills. §Lessons 98: a clean import of an unusable sheet, and the success message was
accurate.

### 2.3 There is no reason to open the dashboard on a bad day

A job search is a long, mostly-losing loop. The product currently offers a table. It has no
mechanism for continuity, no reason to come back on a day when nothing good happened, and
nothing that persists a relationship across weeks.

### 2.4 The audience shift, and what it costs

**Reading "Gen Z and earlier" as Gen Z and younger** — Gen Z is after millennials.

What gets *better* with this audience:

- **The growth ratchet was designed for an empty tank, and they arrive with one.** No 899
  connections, no ten-year résumé. Every piece of context they add is visible progress from zero.
- **The states actually swing.** With 542 contacts one reply is noise. With eleven it is an
  event, and the mascot moves visibly.
- **The register is native.** Duolingo's owl is the proof case: a guilt mascot, that audience,
  at scale, where the memes did more than the features. Nudgy is in that lineage.

What gets *worse*, and must be designed against:

- **The guilt is heavier.** A mid-career operator seeing a sad creature is a joke. A grad four
  months into a bad search is not in the same place. Headline decision 12 exists for this.
- **A new grad's Nudgy is quiet by default**, because they have three contacts, not 542. The
  mood ladder must not read `FLAT` on day one for somebody who simply started yesterday. The
  `new` guard (§4.3) is load-bearing.

**Out of scope for this PRD, and named because the mascot cannot fix it:** ApplyPilot today
needs a paid Apollo plan, a Gmail OAuth grant, Claude Code CLI, Node and a local Python install.
That is not a new-grad product. Repositioning is a real programme; this document does not
pretend the mascot is it.

---

## 3. Requirements

### 3.1 Functional

| # | Requirement |
|---|---|
| F1 | Nudgy renders in the dashboard header, visible on every tab and every Space, at all times. |
| F2 | His **mood** is derived from live CRM state and updates within one 2.5s tick of a change. |
| F3 | His **growth stage** is derived from accumulated operator-typed context and never decreases. |
| F4 | He **tracks the cursor** and, when the cursor is idle, looks at the most urgent thing on screen. |
| F5 | He **bids for attention** when idle: small, escalating-then-decaying, never blocking. |
| F6 | He **speaks** — one line at a time, every line provable from a row (§5.3). |
| F7 | Clicking him opens a panel with the **daily brief** (§6.1). |
| F8 | Every line he says is **clickable through to the thing it names** (the contact, the job). |
| F9 | His mood drives the **favicon**, so he is alive in a background tab (closes debt item 2a). |
| F10 | **Variants** unlock on real milestones, are permanent, and are never purchasable (§5.5). |
| F11 | He can be **muted** — collapsed to a static face, no bids — and un-muted, in one click. |

### 3.2 Non-functional

| # | Requirement | Enforced by |
|---|---|---|
| N1 | **Zero new SQL statements** on `/api/status`. The budget is **80/80 with zero headroom** (measured 2026-08-25); one added SELECT fails the suite. | `tests/test_query_budget.py` |
| N2 | **Zero network round-trips** added to the 2.5s path. | the round-trip counter added after §Lessons 26 |
| N3 | **No new runtime dependency.** | `package.json` stays dev-only |
| N4 | Animation touches **`transform` and `opacity` only** — never layout. | code review + a CSS guard (§Lessons 101) |
| N5 | `prefers-reduced-motion` → state changes only, no idle loops. | render test |
| N6 | `document.hidden` → all animation pauses; favicon continues. | render test |
| N7 | `domain/mascot.py` imports no `sqlite3`, no `http`, no `web_dashboard`, no `networking`. | ARCH-1's existing boundary test |
| N8 | Nudgy failing must **never** take the dashboard down. | try/except at the payload seam, as `_metrics_payload` already does |

### 3.3 The bid budget — the anti-dark-pattern constraint

This is the requirement most likely to be violated by a well-meaning change, so it is stated as
a number rather than a principle:

- **At most one bid per 90 seconds**, and only while the tab is focused.
- **Bids decay.** Three ignored bids in a row and the interval doubles; after five he stops
  bidding entirely until state changes or you interact. He does not get louder — that is the
  whole character.
- **No sound by default.** Opt-in, one 80ms tone, and only on a state *improvement* (a reply
  landing), never on a nag.
- **Nothing Nudgy does ever gates a click, steals focus, or covers content.**

### 3.4 One Nudgy, global — not one per Space

Every other panel in this dashboard is Space-scoped. Nudgy is not, for two reasons:

- **Six pets is no pet.** The parasocial bond is the entire mechanic, and it does not survive
  being divided six ways. Growth accumulated in `job-search` must count for the same creature
  you see in `sheet-search`.
- **§Lessons 104.** A global counter pointing at per-job tabs was reported as broken when both
  numbers were right. Nudgy avoids this by being global *and naming the Space* in his lines —
  "Anurag, in Job Search" — so the number and the destination never disagree.

---

## 4. The state model — `domain/mascot.py`

Pure functions. Dicts in, a state vector out. No I/O, no clock except an injected `now`.

### 4.1 Five needs

| Need | Question | Source (all already on the payload) | Decays |
|---|---|---|---|
| **HEARD** | Is anyone waiting on a reply from you? | `contacts.replied_at` vs later outbound `messages` | fastest |
| **FED** | Are the promises the ladder made being kept? | `followups.*due_count` — the same `dueByChannel()` the 🔔 counter reads | yes |
| **WARM** | How long since you said anything to anyone? | `max(touches.sent_at)` | slowly |
| **SEEN** | Did *you* show up? | last dashboard interaction (browser-local, §4.6) | slowly |
| **KNOWN** | How much context have you given him? | the eight operator-typed fields in §1.4 | **never** |

**HEARD outranks everything.** One person waiting three days sinks Nudgy further than forty late
follow-ups. That is §Lessons 27 expressed as a weight rather than a sort order.

**SEEN is the clingy need and the genuinely new one.** Every other signal measures your work.
This one measures your presence, and it is why Nudgy reads as a companion rather than a
dashboard widget. It is also the only need that a *bad day* can satisfy: opening the tab and
doing nothing still counts for something, which matters enormously for §2.3.

### 4.2 Mood — five states

```
✦ THRIVING    nothing late · nobody waiting · you answered someone within 48h
              bouncing. leans toward the cursor. small celebratory nudges.

● CONTENT     nothing late · nobody waiting
              upright, eyebrow raised, expectant. the default resting state.

◐ RESTLESS    1-2 promises slipping, or 3+ days quiet, or 2+ days unseen
              sitting. watching you. bids more often.

◑ DEJECTED    someone waiting 2+ days, or many late, or a week silent
              half-lidded. posture gone. bids rarely and they are sadder.

▬ FLAT        (reply unanswered 4+ days) AND (5+ days silent), sustained 24h
              horizontal. one paw twitching toward the screen. barely bids at all.
```

**`FLAT` requires two conditions AND a duration.** On the live account it would have been reached
roughly once this month. If it fires weekly it is mistuned and the tuning is wrong, not the
threshold — re-measure before moving a constant.

**Recovery is immediate.** Answering Anurag returns Nudgy to `CONTENT` on the next 2.5s tick, with
a visible transition. There is no penalty tail, no cooldown, no "earning back". Decay is slow;
recovery is instant. Anything else is a punishment machine.

### 4.3 The `new` guard

A Space created this morning, or an account three days old, is **not** neglected. Nudgy reads
`new` — awake, curious, no guilt, no bids about work that does not exist yet — until there is
something real to be late about. This is the same distinction `domain/temperature.py` draws with
its own `NEW` band, for the same reason: *a job imported this morning is not failing.*

For the grad audience this guard is doing most of the work in week one.

### 4.4 Growth — five stages, unlocking real capability

Growth is the count of operator-typed context items (§1.4). It ratchets: it is a **high-water
mark**, and deleting a note never moves Nudgy down.

| Stage | Threshold | What Nudgy can now do | Backed by |
|---|---|---|---|
| **0 · Blank** | 0-24 items *(you are here: ~63 — see note)* | tells you who is waiting and what is late | payload |
| **1 · Listening** | 25 contacts with `noticed` or `notes` | tells you what you tend to notice about people who answer | payload |
| **2 · Remembering** | 20 jobs with `job_context` | drafts that do not sound like the other twelve | prompts (CTX) |
| **3 · Connecting** | 60 hand-typed fields + 10 transcripts | "you have met three people at this company" | **knowledge graph** |
| **4 · Knowing** | sustained | patterns across the whole history — what actually earns replies | **knowledge graph** |

*Note on the live account: the raw count is ~63, but 21 of those are `flagged_at` and 17 are
phones. The stage thresholds are per-FIELD-TYPE deliberately (25 contacts with `noticed`/`notes`,
not 25 items of anything), so that filling one cheap field cannot buy a stage. On that basis the
live account is **Stage 0** with 9 of 25 toward Stage 1.*

**Growth is honest, and that is the point.** Nudgy genuinely cannot tell you why people reply if
you never wrote anything down. The locked stages are not a paywall dressed as a game; they are a
true statement about what is computable from what you have given him. This is the mechanic that
makes the whole product's value proposition legible.

**Stages 3 and 4 are the knowledge graph's stages.** See §8.

### 4.5 The state vector — the contract

Named the way Rive names its inputs, so the renderer is a swap and not a rewrite:

```jsonc
{
  "stage":    1,             // number  0-4, monotone, high-water mark
  "health":   38,            // number  0-100, for continuous visual params
  "mood":     "dejected",    // enum    thriving|content|restless|dejected|flat|new
  "needs":    { "heard": 5, "fed": 70, "warm": 95, "seen": 80, "known": 36 },
  "triggers": ["reply_unanswered"],   // one-shot events since the last tick
  "line":     { "text": "Anurag answered you a week ago and hasn't heard back.",
                "href": "?space=job-search&contact=...",   // F8: always clickable through
                "kind": "heard",
                "proof": { "contact_id": "...", "replied_at": "2026-08-18T..." } },
  "variants": ["first_reply", "first_interview"],
  "muted":    false
}
```

`proof` is not decoration. It is the machine-checkable half of headline decision 7: a line with
no `proof` object **cannot be rendered**, which makes "every line is provable" a test rather than
an intention (§11).

### 4.6 Where `SEEN` is stored, and why it is not a column

`SEEN` is the only need that is about the browser rather than the database, and it is kept in
`localStorage`, deliberately:

- It is **per-device and per-human**, which is what "did you show up" means. A server-side
  column would be advanced by the poller, by a cron, by a second tab — none of which is you.
- It needs **no schema change**, and this whole PRD is designed to need none (§9.3).
- Losing it is harmless: a cleared browser reads as `new`, not as neglect.

---

## 5. The character

### 5.1 What he is

A small, fuzzy, ugly-cute creature with big glossy eyes and a grin slightly too wide, with
visible teeth. He lives in the header at roughly 40px and must read as a **silhouette before he
reads as a face**.

### 5.2 The one visual mechanic the whole design rests on

**When Nudgy is neglected, the grin does not change. The eyes do.**

Same smile, all the way down to `FLAT`. The eyes go from glossy and wide, to half-lidded, to
flat and unfocused. A creature still smiling at you with dead eyes is genuinely unsettling in a
way that a frown is not — and mechanically it is one interpolated parameter, two SVG shapes, and
no second character to draw.

It is also **ownable**. Nothing else in this space is doing it, and it is the property that makes
Nudgy screenshot-able (§5.4) rather than generic.

### 5.3 Voice

**Register:** disappointed, never cruel. Dry. Short. He does not yell, and he does not joke about
your prospects.

**The three rules:**

1. **Every line is provable from a row.** He is dry in framing and never invents a fact. Your
   example line — *"it's been 11 days since you talked to anyone who isn't a delivery driver"* —
   is funny and Nudgy can never say it, because he has no idea about your delivery drivers. The
   true version is worse, which is exactly why it works:

   > *"Anurag answered you a week ago and hasn't heard back."*

2. **He comments on ACTIONS, never OUTCOMES.** Rejections, ghostings, and silence from employers
   are not his business. Whether you wrote back is.

3. **He never speaks about work that does not exist.** No bids on an empty Space, no guilt on
   day one (§4.3).

**Real lines, generated against the live database on 2026-08-25:**

```
CONTENT      Nothing's late. 82 touches out this week. Ask me something.

RESTLESS     Two came due at Okta overnight. Same company, so space them out.

DEJECTED     Anurag answered you a week ago and hasn't heard back.        <- ACTUAL STATE
             He's the only one waiting. You answered everyone else.

FLAT         Five days quiet. Anurag's been waiting seven.
             (twitches paw at the screen)

CONTEXT      I know 419 LinkedIn URLs and almost nothing you noticed about anyone.
             One line on Anurag's card and I'd write a better follow-up than I can now.

GOOD DAY     Anurag replied. First one from that Space. I filed it.

CLINGY       You've had this open for six minutes and haven't clicked anything.
             (this is fine. I'm just noting it.)
```

**Does he use your name?** Recorded as an open question (§13) because it materially changes the
register: *"you haven't written back"* against *"Alejandro, you haven't written back."* The
second lands much harder and is much worse if the name is wrong.

### 5.4 Screenshot-ability is a requirement, not a bonus

For this audience part of Nudgy's job is to get posted. That imposes two real constraints:

- **States must read out of context.** A `FLAT` screenshot has to be legible to somebody who has
  never seen the product, which means the mood must be carried by the *drawing*, not by the
  surrounding UI.
- **Rare states must feel like a find.** This is the other half of why `FLAT` is gated behind two
  conditions: a state you see every day is not worth showing anyone.

### 5.5 Variants — the collection loop

The Labubu/blind-box mechanic, running on the growth ratchet and costing a sprite and a boolean.

- **Earned, never purchased.** Milestones: first reply, first interview, ten cards with real
  context, a full ladder completed, a 7-day streak of showing up.
- **Permanent**, like growth. Nothing is ever taken away.
- **Cosmetic only.** A variant never changes what Nudgy can do — that is the growth axis's job,
  and conflating them turns an honest capability ladder into a loot box.

### 5.6 What we are not doing

**Not Labubu.** It is Kasing Lung's character, licensed to Pop Mart, and actively merchandised.
A legally-distinct near-copy reads as a knockoff to precisely the audience that recognises the
original. The decomposed properties in headline decision 13 are unowned; the character is not.

---

## 6. The assistant half

Three capabilities, deliberately shipped in this order — each is useful alone, and each is a
strictly harder correctness problem than the one before it.

### 6.1 The daily brief — deterministic, no LLM

One panel when you click Nudgy, built entirely from data already on the payload. **No model
call, no network, no possibility of a hallucination.**

```
NUDGY · this morning

  Anurag answered you a week ago and is still waiting. He's the only one.
  Two follow-ups came due at Okta overnight — same company, so space them.
  Zapier has gone quiet 11 days after the last touch; that ladder is spent.
  You have context written on 1 job out of 207.
```

Every clause is a row lookup. This ships first because it is the highest value per unit of risk
in the whole document, and it is what makes Nudgy *useful* rather than *decorative* on day one.

### 6.2 Proactive nudges — the reply poller he already has

The 5-minute reply poller already exists and already runs from the dashboard (never from
`applypilot tick`, which has never been installed — §Lessons 31). Nudgy rides it. A reply
landing, a promise about to go stale, a company about to be over-contacted: state changes, mood
moves, he bids once.

Subject to the bid budget in §3.3 without exception.

### 6.3 Ask him anything — the one that needs retrieval

A text box that answers over the accumulated history. **This is where the knowledge graph plugs
in**, and it is last because it is the only capability that can be confidently wrong.

```
> who replied but I never answered?

  One. Anurag Panyala, Job Search — replied Aug 18, seven days ago.
  Everyone else you answered.

> which of my openers actually got replies?

  Not answerable yet. Your last 13 drafts share 89% of their subject
  words, so there is no variance to measure. This is a known open item.
```

The second answer matters more than the first. **Nudgy saying "not answerable yet" and naming
why is the behaviour that makes the first answer trustworthy** — §Lessons 15, a zero result must
be as loud as an error.

---

## 7. Rendering and liveliness

### 7.1 The stack, and what was rejected

| Option | Vendored | Wiring | Verdict |
|---|---|---|---|
| **Inline SVG + CSS + Web Animations API** | 0 | none — it is markup | **ship this** |
| Rive | ~200 KB WASM + runtime, a 4th static file | classic-script global; canvas must survive the 2.5s rebuild; **the character must be rigged first** | later, if ever |
| Lottie | ~250 KB | same | **no** — linear timeline exports cannot react to state |
| Spline / Three.js | 600 KB+ | build-step pressure | no. This is a 40px header creature |
| Live2D | proprietary, heavy | high | no |

Nudgy is **6 moods × 5 stages ≈ 11 distinct forms**, not a rigged character doing continuous
skeletal deformation. Rive solves the second problem. Paying 200 KB, an offline-vendoring
problem and a rigging workflow for a problem we do not have is the wrong trade — and with the
state vector named Rive-style (§4.5), upgrading later is a renderer swap that touches no Python.

### 7.2 Liveliness is not a library feature

Five layers, none of which is what Rive is for, all of which are cheap in SVG:

**L1 — Idle motion that never visibly repeats.** Coprime durations plus randomised events:

```css
.nudgy-body     { animation: breathe  3.2s ease-in-out infinite; }
.nudgy-ears     { animation: sway     4.7s ease-in-out infinite; }
.nudgy-drift    { animation: drift   11.3s ease-in-out infinite; }
```

Those three realign every ~170 seconds; blinks are scheduled on a randomised timer (2.2-7s, with
a 22% chance of a double-blink) and never align at all. **Nobody can find the loop.** A single
loop reads as a GIF, and that one detail does more for perceived life than a rig would.

**L2 — He looks at things.** Pupils track the cursor, clamped inside the sclera — the distance
constraint from the research, six lines instead of a rig. When the cursor goes idle **he looks at
the most urgent thing on the page instead**: a reply waiting means he is facing that row before
you are. This is F4, and it is the clearest expression of clinginess in the whole build.

**L3 — Secondary motion.** Ears and paws lag the body by 60-90ms on any state change. One
`transition-delay`. It is the difference between a sticker and a creature.

**L4 — Reactions to real events.** `refresh()` already diffs the payload every 2.5s. One-shots
fire through `element.animate()`, which layers *on top of* the CSS idle loops instead of fighting
them and composites off the main thread. Easing overshoots (`cubic-bezier(.34,1.56,.64,1)`) —
anticipation and settle are why it reads as alive; a linear fade reads as a CSS transition.

**L5 — The mood morph.** SVG `d` interpolation between eye shapes with matching node counts.
Chrome interpolates `d` natively, and this is a localhost console opened in Chrome — a free pass
most projects do not get.

### 7.3 Where he lives, and the rule that cannot be broken

**Nudgy renders into a static node in `<header>` that `refresh()` writes INTO and never
replaces** — the `#todoCount` pattern. Put him inside `#jobs` and the 2.5s wholesale rebuild
destroys every animation mid-frame, forever, and the bug will present as "he's twitchy" rather
than as a lifecycle error.

The SVG DOM is **built once**. Refreshes mutate attributes and classes only.

### 7.4 The favicon — free, and it closes a known debt item

Render Nudgy to a 32×32 `<canvas>`, `toDataURL()`, swap the `<link rel=icon>`. He stays alive in
a background tab, which is the one place a header creature cannot reach you.

Debt item 2a currently reads: *"Nothing tells the operator an application is waiting — no sound,
no desktop notification, no tab-title badge."* This closes it as a side effect, in about 30 lines
and with no dependency.

---

## 8. The knowledge graph — the contract between two documents

*`docs/knowledge-graph-prd.md` exists (1,509 lines, v2, drafted in a parallel session). This
section was originally written blind and has been rewritten against the real document. **Where
the two disagree, the KG PRD wins on anything inside the graph, and this one wins on anything
inside the mascot.** Nothing below re-proposes a data model; §5 and §6 of that document own it.*

### 8.1 The two documents complete each other, and this is the strongest reason to build both

The KG PRD's §5.3 is `person_context` — a table of **operator-typed facts about a person**,
shipped deliberately *with its write door* because "a table with no writer is `identities`
repeated" (§14.4).

This PRD's §1.4 is the measurement of why that door will stay shut: **1 of 207 jobs has
`job_context`, 2 of 542 contacts have `noticed`.** The four-tier cascade in
`outreach-context-prd.md` was built, shipped, tested, and is fed by almost nothing.

**Nudgy's `KNOWN` axis is the only mechanism in the product that gives anyone a reason to type
into that door.** The KG builds the place facts live; Nudgy is why they get written. Neither
document is wrong alone, but the graph of a corpus nobody annotates is a graph of Apollo's data,
and that is exactly what Headline 6 of this PRD refuses to let Nudgy grow on.

### 8.2 The load-bearing separation is unchanged

```
                    MOOD                          ANSWERS
                    ────                          ───────
  source            the payload, always           kg_* tools, when present
  latency budget    2.5s tick, 0 new statements   deliberate ask only
  if graph absent   unaffected                    degrades and says which questions it lost
  if graph wrong    unaffected                    wrong answer  <- the whole risk
```

**Nudgy's mood may never depend on the graph.** If it does, Nudgy does not exist until the graph
ships — §Lessons 31, the feature that only ran from a scheduler nobody installed. His state is
derived from rows `_status_payload` already loads, today.

This is now doubly forced: the KG PRD's Headline 4 and §3 both state that **nothing may touch
`/api/status`**, and §9's independent measurement confirms 80/80. Nudgy's payload key is a pure
aggregation over in-memory lists or it does not ship.

### 8.3 Nudgy binds to the published contract, not to a parallel one

An earlier draft of this section invented a `ContextSource` protocol with `ask()` and
`neighbours()`. **That is withdrawn.** A second retrieval interface over one graph is §Lessons 49
aimed at the layer where a wrong answer is least visible. Nudgy consumes the `kg_*` tools as
published in §7:

| What Nudgy uses | Why |
|---|---|
| `person:<12hex>` etc. — **prefix-typed ids** | Nudgy's `proof` object (§4.5) stores these verbatim; F8's click-through resolves them |
| **`page`** on every result | Nudgy links to the same artifact the filesystem agent reads. One destination, not two |
| **§7.0's ordering** — `engaged` desc → `invested` desc → last inbound desc | Nudgy never re-sorts. A second ranking is a second answer to "who matters" |
| **`mode=ro`** | Nudgy is a reader. His only write path is the operator typing into `person_context`, which is the KG's own door (§5.3), not a new one |

The thin adapter that remains is a **capability check, not an abstraction**: `graph_available()`
→ if false, Nudgy names the questions he cannot answer and why (§Lessons 15). It has one
implementation and no second backend.

### 8.4 Three of their headlines land directly on this design

**Headline 5 — facts are RECORDED, never inferred by a model.** This is Nudgy's §5.3 rule
arrived at independently, and the KG's phrasing is better: *a guess laundered into a stored fact
outranks the truth forever.* Two consequences here, both now explicit:

- The daily brief (§6.1) is deterministic and stays that way. It is not a model call with a
  short prompt; it is row lookups.
- **NUDGE-9 may never write.** Nothing Nudgy's ask-anything produces is stored — not to
  `person_context`, not to `org_domains`, not anywhere. It renders, cites, and is forgotten.

**Headline 6 — weight is TWO NUMBERS and they are never summed.** `engaged` (what they did) and
`invested` (what we did), because 36 contacts have four or more outbound touches and zero
inbound, and a sum presents that as a strong relationship.

This is the same trap as this PRD's Headline 4, one layer down, and Nudgy's model already
respects it — but only by accident of structure, so it is now stated:

- **`health` is a rendering parameter, never a ranking and never a relationship measure.** It
  drives continuous visual params (eye gloss, posture). **Mood is decided by predicates over the
  needs, never by thresholding the scalar.**
- The needs vector keeps their split intact: **`heard` is `engaged`; `fed` and `warm` are
  `invested`.** They are separate fields at every layer and Nudgy must never add them.
- `test_volume_never_improves_mood` (§11) is the executable form of both headlines at once.

**Headline 7 — weight is LIFETIME, not decay**, which reverses `crm-prd.md`. No conflict here,
and the reason is worth writing down because it looks like one: **the KG ranks PEOPLE by lifetime
weight; Nudgy ranks ACTIONS by recency.** "Who do I know best" and "whose turn is it, and how
long have they been waiting" are different questions and correctly use different clocks. Anurag
is not Nudgy's headline because he is important; he is Nudgy's headline because he has been
waiting seven days.

### 8.5 A finding to carry back: Nudgy will destroy `knowledge/calls.log` if nothing changes

**This is the one place the two documents actively collide, and it is worth fixing in theirs
rather than working around in mine.**

§7.6 appends one line per call — *timestamp, tool name, id kind, nothing else* — and it is not
telemetry. It is the **only** falsifier for two headline assumptions: Headline 1's tiering claim
(are the questions concentrated on a few dozen people?) and Headline 7's decay-versus-lifetime
question (which tool actually gets called?). §7.6 says explicitly that *a zero-length log after
two weeks is the honest answer to whether this got used at all.*

**Nudgy is a machine that would call these tools.** If NUDGE-9 ships and Nudgy queries on card
open, on refresh, or to enrich a line, the log fills with calls **no human made** — and both
falsifiers silently stop measuring the operator. Worse, they stop measuring in the direction that
makes the graph look successful, so nothing raises.

Two possible fixes, and the first is much cheaper:

1. **Add a caller field to the log line** — `human` vs an agent id. Three characters of data, no
   names, no query text, consistent with §11.1. Their falsifiers then filter to `human` and are
   unaffected by anything Nudgy does.
2. **Nudgy never calls a `kg_*` tool except in direct response to a typed question**, and the
   brief stays payload-only. Preserves the log's meaning but permanently caps what Nudgy can do
   proactively.

**Recommendation: do 1, and adopt 2 as a rule anyway.** The brief being deterministic is already
this PRD's §6.1, so 2 costs nothing today, and 1 protects the measurement if that ever changes.

### 8.6 What Nudgy inherits and must not re-derive

The KG PRD's §5.1 settles person identity: a **stored surrogate** resolved by a first-non-empty
cascade (email → `linkedin_url` → name+company), merging 533 contact rows into **530 people**,
with `contact_id` untouched. It stores the id rather than deriving it because **258 of 533
contacts have no email** and typing one in would otherwise re-key ~48% of the corpus on a single
keystroke.

**Nudgy stores `person:<id>` in his `proof` objects and never computes identity himself.** An
earlier draft of this section proposed reusing `store.contact_id()` and `known_at_company()`
directly; that is superseded and would have produced a second, disagreeing answer to "is this the
same human" — the CO-2 bug, rebuilt at the mascot layer.

The one number Nudgy should carry from their §1: **330 of 533 contacts have zero stored
messages, and the top ten hold 31% of all messages.** Nudgy's lines must concentrate where the
relationships actually are. A mascot that nags uniformly across 542 rows is nagging about 330
people nobody has ever spoken to.

### 8.7 Growth stages 3 and 4 are the graph's

Stages 0-2 are payload-answerable and ship without it. **Stage 3 ("you have met three people at
this company") is `kg_at_org`; Stage 4 ("what actually earns replies") is a pattern across the
whole corpus.** If the graph slips, Nudgy caps at Stage 2 and says so, in the same voice he uses
for everything else he cannot prove.

## 9. Systems design

### 9.1 Data flow

```
  SQLite (authoritative)
        │
        ├──▶ repo/ + store.py ──▶ _status_payload()  ← rows ALREADY loaded. 80/80. NO headroom.
        │                              │
        │                              ├──▶ domain/mascot.py   PURE. state vector. 0 queries.
        │                              │         │
        │                              │         └──▶ /api/status  ["nudgy"] key
        │                              │                     │
        │                              │                     ▼
        │                              │            dashboard.js  refresh()
        │                              │                     │
        │                              │            ┌────────┴────────┐
        │                              │       static #nudgy      favicon canvas
        │                              │       (written INTO)     (32x32, mood-driven)
        │                              │
        │                              └──▶ nudgy/brief.py    deterministic daily brief
        │
        └──▶ [derived, rebuildable] ──▶ knowledge graph ──▶ GraphSource ──▶ nudgy/ask.py
                                        NEVER on the 2.5s path      deliberate asks only
```

### 9.2 Why the state vector costs zero queries

Every input is already in `_status_payload`'s hands:

| Need | Already loaded by |
|---|---|
| HEARD | the per-job contact payload — `replied_at`, `last_replies`, `awaiting_reply` |
| FED | `followups.*due_count`, the same fields `dueByChannel()` reads |
| WARM | `touches` rows already loaded for the follow-up panel |
| KNOWN | `jobs` rows (`job_context`, `job_ask`) + contact rows (`noticed`, `notes`, `phone`, `flagged_at`) |
| SEEN | `localStorage`, never the server |

`mascot.state(jobs, contacts, now)` is **one pass of pure aggregation over lists already in
memory** — the same shape as `_metrics_payload`, which is why it inherits that function's
try/except (N8) rather than inventing a new failure mode.

### 9.3 No schema change

Nothing in this PRD adds a column, a table or a migration. Variants and the growth high-water
mark ride `localStorage` plus a single JSON blob in the existing `spaces.config` pattern if they
need to survive a browser wipe — deferred until they demonstrably do.

Stated because it is falsifiable: `test_nudgy_needs_no_schema_change` (§11).

---

## 10. Tickets

| Ticket | What | Depends on | Ships alone? |
|---|---|---|---|
| **NUDGE-1** | `domain/mascot.py` — five needs, six moods, growth ratchet, the `new` guard. Pure, tested, mutation-verified. **No UI.** | — | yes (CLI: `applypilot nudgy --state`) |
| **NUDGE-2** | Payload wiring + the query-budget and round-trip guards | 1 | yes |
| **NUDGE-3** | The creature: SVG, the eyes/grin mechanic, L1-L5 liveliness | 2 | yes |
| **NUDGE-4** | Voice: the line generator, the `proof` requirement, the click-through | 2 | yes |
| **NUDGE-5** | Clinginess: cursor tracking, gaze targeting, the bid budget, mute | 3 | yes |
| **NUDGE-6** | The daily brief (deterministic) + `PayloadSource` | 2 | yes |
| **NUDGE-7** | Favicon + opt-in chirp — **closes debt 2a** | 3 | yes |
| **NUDGE-8** | Variants and milestones | 3 | yes |
| **NUDGE-9** | `GraphSource` + ask-anything | 6, **graph** | **blocked** |

**NUDGE-1 is the product.** If only one ticket ships, that is the one: it is where every rule
lives, it is testable without a browser, and it is what the renderer and the graph both consume.

---

## 11. How we will know it works — falsifiable tests

Written as tests that can **fail**, because this codebase's recurring defect is the assertion
that cannot (§Lessons 13, 71, 98, 100, 113).

| Test | What it kills |
|---|---|
| `test_volume_never_improves_mood` | Doubling every contact's sent count with no replies must not raise `health` by one point. The §Lessons 35 guard, executable. |
| `test_a_line_without_proof_cannot_render` | A line whose `proof` object does not resolve to a real row raises. Makes headline decision 7 mechanical. |
| `test_growth_never_decreases` | Delete every note; `stage` does not move. |
| `test_answering_recovers_in_one_tick` | `FLAT` → answer the reply → `CONTENT` on the next state call, no tail. |
| `test_flat_needs_both_conditions` | Either condition alone must not reach `FLAT`. |
| `test_a_new_space_is_not_neglected` | A Space created today reads `new`, never `dejected`. |
| `test_nudgy_adds_no_statements` | `/api/status` statement count is **unchanged**, not merely under 80. |
| `test_nudgy_adds_no_roundtrips` | The §Lessons 26 counter. |
| `test_nudgy_needs_no_schema_change` | Drives Nudgy end to end and asserts no migration ran. Modelled on `test_adding_a_space_needs_no_schema_change`. |
| `test_bids_decay_and_never_escalate` | Five ignored bids → interval strictly increasing, then zero. |
| `test_the_render_actually_runs` | Drives `renderNudgy` under DOM stubs, **fed from a real `_status_payload` output** — never a hand-written fixture (§Lessons 93, 103). |
| `test_no_td_gets_display_flex` | The CSS guard from §Lessons 101, extended to the new rules. |

**The last two exist because of specific past failures.** §Lessons 103: a fixture invented a
field name, so the code and the test agreed and the feature rendered blank. §Lessons 94: 36 tests
passed while the inline editor was completely broken, because none of them drove the render.

---

## 12. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| **Nudgy becomes a nag** | **highest** — it is the failure that gets the product uninstalled | §3.3 bid budget as a hard number; ignored bids get sadder, never louder; mute in one click |
| **The guilt lands wrong for a grad on a bad day** | high | headline 12 — actions never outcomes; the `new` guard; recovery instant |
| **He is permanently content**, therefore furniture | high | this is §1.2 measured; the fix is that health tracks promises, not volume — and `KNOWN` has 700 rows of room |
| **He is permanently sad**, therefore ignored | high | `FLAT` needs two conditions and a duration; re-measure if it fires weekly |
| **The graph lands wrong and Nudgy states falsehoods confidently** | high | §8.3 — citations or no render; the graph never touches mood |
| **The 2.5s path regresses** | medium | N1/N2, both already-existing test harnesses |
| **A cute mascot on a serious tool reads as unserious** | medium | mute is one click and persists; the brief (§6.1) is useful with the creature collapsed |
| **Scope creep into a chat product** | medium | NUDGE-9 is last and explicitly blocked; everything before it is deterministic |

---

## 13. Open questions

1. **The drawing.** The eyes/grin mechanic (§5.2) is the spec; the actual creature is not drawn.
2. **Does he use your name?** *"you haven't written back"* vs *"Alejandro, you haven't written
   back."* Materially different register, and much worse if the name is wrong.
3. **Is `SEEN` too clingy?** Guilt for not opening a tab is the most defensible-in-theory and
   most annoying-in-practice of the five needs. It may want to be the *quietest* input rather
   than an equal one.
4. **Does Nudgy belong on this account at all, or on the grad product?** §2.4 — the audience the
   mascot is designed for cannot currently install ApplyPilot.
5. **Graph timing.** NUDGE-9 and growth stages 3-4 are the only things that depend on it. If it
   slips, Nudgy caps at Stage 2 and says so.

---

## Appendix — the live account, 2026-08-25

What Nudgy would read right now, and it is not what you would guess:

```
mood     DEJECTED
health   ~38
stage    0  (9 of 25 toward Listening)
line     "Anurag answered you a week ago and hasn't heard back.
          He's the only one waiting. You answered everyone else."
```

**Not because the work is bad.** 82 touches went out this week, one of them today, and 15 of 16
replies were answered — a genuinely strong record. `DEJECTED` is reached on **one row**, seven
days old, sitting underneath 82 units of completed work where nothing surfaces it as the only
thing that counts.

That is the entire case for building this.

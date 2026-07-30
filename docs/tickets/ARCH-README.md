# ARCH / CRM / DISC — refactor + product tickets

Source: `docs/architecture-prd.md` (current state → grilling → plan).
Companion: `docs/crm-prd.md` (Spaces / multi-campaign direction — **after** these).

## Order

> **DECISION 2026-07-28 (Jorge): do the ARCH tickets first.** The table below is the
> analysis-recommended order; the chosen order is **ARCH-1 → 2 → 3 → 4 → 5 → 6, then CRM-1 →
> DISC-1 → CRM-2 → CRM-3.** The tradeoff is understood and accepted: the ARCH set delivers no
> user-visible change, so the funnel stays at 7 hand-pasted jobs and the system stays blind to
> replies until CRM-1 lands. In exchange, every product ticket afterwards lands on clean
> foundations instead of into a 3,710-line file. Dependencies inside the ARCH set are unchanged.

Analysis-recommended order (product first: the refactor makes the code easier to change and
improves nothing a user can see). See `architecture-prd.md` §4.0 for the data behind it.

> **Revised 2026-07-30**, after the apply pipeline closed end to end (agent fills → hands over
> at auth walls → operator signs in → Continue resumes → operator submits). Two changes to the
> plan, both driven by what the live system now does:
>
> 1. **`CRM-3a` (notification) split out and moved to the front.** Co-pilot apply now *ends* by
>    waiting for the operator, and there is no notification of any kind — a filled application
>    waits until someone happens to look, and a restart eventually closes it. A tab-title badge
>    is hours of work, needs no scheduler and no permissions, and protects work today.
> 2. **`CRM-1` is more urgent than it was, not less.** It was 13 sent emails when this order was
>    set; it is now **33, still with exactly 1 reply — recorded by hand.** The blindness
>    compounds with every send.
>
> `CRM-1` and `CRM-2` also had instructions that are now factually wrong (`followup_status` was
> removed by ARCH-3); both tickets carry a "what changed" table.

| # | Ticket | Makes the system… | Size | Depends | Status |
|---|--------|-------------------|------|---------|--------|
| 1 | `CRM-1` reply detection | **see** | M | Gmail metadata scope | Todo |
| 2 | `DISC-1` turn discovery on | have a **real funnel** | S | — | Todo |
| 3 | `ARCH-1` extract `domain/` | **testable** (needed by 4 & 5) | M | — | ✅ **Done 2026-07-28** |
| 4 | `CRM-2` outcome metrics | **learn** | M | CRM-1, ARCH-1 | Todo |
| 5 | `CRM-3` scheduler | **act unattended** | S | ARCH-1 (3b needs CRM-1) | Todo |
| 0 | `CRM-3a` needs-you notification | **not be missed** | XS | — | Todo — *split out 2026-07-30, do first* |
| 6 | `ARCH-2` static frontend | — | M | — | ✅ **Done 2026-07-28** |
| 7 | `ARCH-3` touches table | extensible to new channels | M | ARCH-1 | ✅ **Done 2026-07-29** |
| 8 | `ARCH-4` repository boundary | — | L | ARCH-1 | ✅ **Done 2026-07-29** (narrowed) |
| 9 | `ARCH-5` versioned migrations | — | S | ARCH-3 | ✅ **Done 2026-07-29** |
| 10 | `ARCH-6` config schema | — | S | — | ✅ **Done 2026-07-29** |

Roughly 9–10 days. **The first three deliver essentially all the user-visible value.**

## Guardrails (every ticket)

- All tests green before merge — no ticket lands red (364 as of ARCH-6 — the ARCH set is complete)
- `evals/resolution.jsonl` green — it has already caught a bug every unit test missed
- No behaviour change inside a refactor ticket; user-visible changes are separate commits
- Byte-identical output where verifiable (prompts, rendered PDFs, `/api/status`)
- Runtime dependencies stay at **8**

## Definition of done for the whole set

The system can answer, without a human doing the work:

1. "Did anyone reply?" → CRM-1
2. "What should I apply to this week?" → DISC-1
3. "Is email or LinkedIn working better?" → CRM-2
4. "What needs my attention right now?" (dashboard closed) → CRM-3

…and adding an SMS channel is one registry row plus one prompt → ARCH-3.

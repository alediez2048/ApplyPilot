# Networking & Outreach — Ticket Board

Epic: **NET** — find people at target companies, surface contacts in the dashboard,
draft and send outreach email. Source of truth: [`../networking-outreach-prd.md`](../networking-outreach-prd.md).

| Ticket | Phase | Title | Depends on | Size | Status |
|--------|-------|-------|-----------|------|--------|
| [NET-1](NET-1-apollo-core.md) | 1 | Apollo core: contact discovery + `applypilot network` CLI | — | L | Todo |
| [NET-2](NET-2-dashboard-contacts.md) | 2 | Dashboard contacts panel (name/title/email/phone/LinkedIn) | NET-1 | M | Todo |
| [NET-3](NET-3-outreach-drafting.md) | 3 | AI outreach drafting (subject + body, editable) | NET-1, NET-2 | M | Todo |
| [NET-4](NET-4-gmail-send.md) | 4 | Gmail send automation + safeguards | NET-2, NET-3 | M | Todo |
| [NET-5](NET-5-linkedin-fallback.md) | 5 | LinkedIn browser-agent fallback | NET-1 | M | Todo |
| [NET-6](NET-6-future.md) | 6 | Future: OAuth send, follow-ups, reply tracking, auto-send | NET-4 | L | Backlog |

**Build order:** NET-1 → NET-2 → NET-3 → NET-4 → NET-5. NET-5 (LinkedIn) can slot in
any time after NET-1; NET-6 is backlog.

**Size key:** S ≈ <0.5d · M ≈ 0.5–1.5d · L ≈ 2–4d (rough, solo).

**Prereqs for testing:** `APOLLO_API_KEY` (all), `GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD` (NET-4),
Tier 3 stack — Claude CLI + Chrome + Node (NET-5).

**Definition of done (every ticket):** code + unit tests pass, `ruff` clean, `doctor`
updated where relevant, no regression to the existing pipeline, verified end-to-end on
the real Affirm job in `~/applypilot-local`.

# LinkedIn Assistant Extension — Ticket Board

Epic: **EXT** — a Chrome (MV3) extension that runs *inside* the user's real LinkedIn session,
auto-composes each drafted connection note into the invite dialog, and lets the **human click
Send**. Replaces the failed external browser-automation approach. Source of truth:
[`../chrome-extension-prd.md`](../chrome-extension-prd.md).

> **Why this epic exists:** driving LinkedIn from *outside* the browser (agent-browser/CDP +
> an LLM picking clicks) proved unreliable and account-risky — the a11y snapshot missed React
> modals, synthetic clicks were ignored, and LinkedIn soft-blocked automated Sends. An
> extension flips every failure mode: it *is* the page (sees the DOM, fills React fields), it's
> indistinguishable from the real user, and the **human sends** — which is both what makes it
> reliable and what keeps the account safe.

> **Reviewed → GO WITH CHANGES** (`../chrome-extension-review.md`, 51-agent adversarial review).
> 5 blockers + 4 high-priority findings folded into the PRD (v2) and each ticket's **Review
> deltas (v2)** section. Two real code bugs (B3 queue re-surfacing, B5 missing `dm_sent_at`
> stamp) were **fixed immediately** in `web_dashboard.py` / `store.py` with tests. Key spec
> changes: MV3 worker is stateless (all run-state in `chrome.storage`, §4.1); free-account note
> quota is a first-class path, not an edge; "reliability, not invisibility."

| Ticket | Phase | Title | Depends on | Size | Status |
|--------|-------|-------|-----------|------|--------|
| [EXT-0](EXT-0-local-api.md) | 0 | Local API: queue + status endpoints in ApplyPilot | — | S | Todo |
| [EXT-1](EXT-1-skeleton.md) | 1 | MV3 extension skeleton + read-only queue | EXT-0 | M | Todo |
| [EXT-2](EXT-2-autocompose.md) | 2 | Auto-compose the note into the invite dialog (core) | EXT-1 | L | Todo |
| [EXT-3](EXT-3-advance.md) | 3 | Detect sent + auto-advance the queue | EXT-2 | M | Todo |
| [EXT-4](EXT-4-resilience.md) | 4 | Resilience + safety (fallbacks, caps, pacing) | EXT-2, EXT-3 | M | Todo |
| [EXT-5](EXT-5-polish.md) | 5 | Popup polish: edit, status, skip, progress, settings | EXT-3 | M | Todo |
| [EXT-6](EXT-6-message-flow.md) | 6 | Message flow for 1st-degree connections | EXT-2 | M | Backlog |

**Build order:** EXT-0 → EXT-1 → EXT-2 → EXT-3 → EXT-4 → EXT-5. EXT-2 is the load-bearing
spike (prove the note fills correctly into a real React invite dialog before building UI on
top). EXT-6 is optional/backlog.

**Size key:** S ≈ <0.5d · M ≈ 0.5–1.5d · L ≈ 2–4d (rough, solo).

**Architecture (recap):** *content script* (only piece that touches the LinkedIn DOM; fills the
note React-safe; **never clicks Send**) + *background service worker* (owns queue, pacing, cap;
talks to `localhost:8765`) + *popup* (control panel). Permissions minimal: `linkedin.com` +
`localhost:8765` only, no `<all_urls>`, no remote hosts.

**Non-negotiables (every ticket must uphold):**
- The extension **never clicks Send** — that action is always the human's (reliability + safety).
- No data leaves the machine; the extension talks only to the local ApplyPilot server.
- **Never-break contract:** any auto-compose failure falls back to "here's your note, paste it"
  and still advances — the user is never stuck and the wrong thing is never sent.

**Prereqs for testing:** ApplyPilot dashboard running (`dashboard --serve`), a prepared queue of
LinkedIn contacts (URL + drafted note) via the Apollo pipeline, Chrome with the extension loaded
unpacked, a logged-in LinkedIn session.

**Definition of done (every ticket):** code + tests pass, `ruff` clean (Python side), no
regression to the pipeline/dashboard, manually verified on real LinkedIn profiles, and — for
compose paths — the note fills correctly with the human doing the final Send.

**Distribution:** personal **load-unpacked** (dev mode) for v1 — no Chrome Web Store review
(strict on LinkedIn automation). Chrome-only v1. Revisit both if going public.

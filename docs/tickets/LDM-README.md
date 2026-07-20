# LinkedIn DM Outreach — Ticket Board

Epic: **LDM** — a "Send DM" button (twin of "Send email") that drives the installed
`agent-browser` binary to deliver drafted LinkedIn notes. Source of truth:
[`../linkedin-dm-prd.md`](../linkedin-dm-prd.md).

**Architecture:** ApplyPilot and agent-browser stay **separate repos**. ApplyPilot drives
the installed `agent-browser` binary as a background subprocess (same model as `claude` /
`npx` / Chrome). No merge, no vendoring.

| Ticket | Phase | Title | Depends on | Size | Status |
|--------|-------|-------|-----------|------|--------|
| [LDM-1](LDM-1-bridge-and-send.md) | 1 | agent-browser bridge + one-DM send (CLI) | NET-1..3, agent-browser | L | **Built** (pending live verify) |
| [LDM-2](LDM-2-dashboard-send-dm.md) | 2 | Dashboard "Send DM" button + background task | LDM-1 | M | **Built** |
| [LDM-3](LDM-3-safeguards-doctor.md) | 3 | Safeguards, consent gate, dry-run, doctor | LDM-1, LDM-2 | M | **Built** |
| [LDM-4](LDM-4-fastpath.md) | 4 | Deterministic fast-path + observability | LDM-1 | M | Backlog |

> **Implementation note (supersedes the MCP plan in the tickets):** the installed agent-browser
> (0.27.0) has no `mcp` subcommand, so LDM-1 was built on 0.27.0's **deterministic CLI** instead
> (`open --profile` / `snapshot` / `keyboard inserttext` / `click`), with a `snapshot → LLM → one
> action` controller loop that inserts the note **verbatim**. Repos stay separate (subprocess).
> Code: `src/applypilot/networking/linkedin_dm.py`, `dm_prompt.py`. Tests: `tests/test_linkedin_dm.py`.

**Build order:** LDM-1 → LDM-2 → LDM-3. LDM-1 is the load-bearing spike (prove agent-browser
reliably sends a DM end-to-end before building UI).

**Prereqs:** `agent-browser` installed + `agent-browser install` (Chrome); a one-time
LinkedIn login into the dedicated DM profile; drafted LinkedIn notes on contacts (already
produced by the networking flow).

**Definition of done (every ticket):** code + unit tests pass, `ruff` clean, `doctor` updated
where relevant, no regression to apply/networking, all safeguards enforced, and — for send
paths — verified with **dry-run** before any live DM.

**⚠️ Risk:** automated LinkedIn messaging on the primary account can cause **permanent
account restriction**. Off by default, consent-gated, capped, human-in-the-loop. Consider a
secondary account.

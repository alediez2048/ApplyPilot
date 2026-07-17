# LDM-4 — Deterministic fast-path + observability (backlog)

**Phase:** 4 · **Size:** M · **Depends on:** LDM-1 · **Status:** Backlog
**PRD:** §3 (option B)

## Summary
Post-MVP: an optional deterministic agent-browser script that sends a DM without an LLM in
the loop (faster, cheaper), used as a fast-path when LinkedIn's message UI matches known
selectors — falling back to the LDM-1 claude+MCP path when it doesn't. Plus richer
observability.

## Candidate items
- [ ] Deterministic send: `open {url} → snapshot → click Message → fill → send` with
      selector heuristics; validate the composer + recipient before typing.
- [ ] Fast-path/fallback selector: try deterministic; on any mismatch, defer to the
      claude+MCP path (LDM-1). Never send if neither can confirm the composer/recipient.
- [ ] Observability: per-send trace/screenshot artifact (agent-browser `trace`/`screenshot`)
      stored under logs for auditing what was sent to whom.
- [ ] Optional retry with backoff on transient nav failures (never on ambiguous send state).
- [ ] Standardize the agent-browser version (pin) once the flow is proven.

## Notes
Anything that sends must keep all LDM-3 safeguards. Promote items to LDM-5+ tickets when
prioritized. Deterministic ≠ unsafe: it must still verify recipient + composer before typing,
and honor caps/dedupe/consent.

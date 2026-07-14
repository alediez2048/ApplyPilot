# NET-6 — Future enhancements (backlog)

**Phase:** 6 · **Size:** L · **Depends on:** NET-4 · **Status:** Backlog
**PRD:** §10 (phase 6), §13

## Summary
Post-MVP improvements to the networking/outreach feature. Not scheduled; pull items
out into their own tickets when prioritized.

## Candidate items
- [ ] **Gmail API OAuth transport** — alternative to SMTP app-password: send via the
      Gmail API (reusing the wired Gmail MCP's OAuth token under `~/.gmail-mcp/`), for
      better threading/deliverability. Add `google-api-python-client`; auto-detect and
      prefer over SMTP when a token exists.
- [ ] **Threaded follow-ups** — schedule a polite follow-up N days later if no reply;
      track thread ids; respect the daily cap.
- [ ] **Reply tracking** — read inbox (Gmail MCP / API) to detect replies and flag
      contacts as "replied" in the dashboard.
- [ ] **Opt-in auto-send-after-apply** — configurable: on a successful apply, auto-find
      contacts and (optionally) auto-send to **verified** emails only, under a strict cap.
- [ ] **Sequencing / campaign view** — lightweight multi-touch cadence per contact.
- [ ] **Contact quality scoring** — rank/label contacts by relevance & seniority; surface
      "best person to reach" per job.
- [ ] **Export** — CSV export of contacts + outreach status for external CRM.
- [ ] **LinkedIn note send (guarded)** — optionally send the connection note via the
      browser agent (higher ToS risk; heavily gated, off by default).

## Notes
Each item is independently valuable; promote to a numbered ticket (NET-7+) with its own
acceptance criteria when picked up. Anything that **sends** must keep the NET-4 safeguards
(user-initiated or strict verified-only + caps; never silent bulk).

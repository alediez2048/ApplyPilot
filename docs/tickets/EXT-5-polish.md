# EXT-5 — Popup polish: edit, status, skip, progress, settings

**Phase:** 5 · **Size:** M · **Depends on:** EXT-3 · **Status:** Todo
**PRD:** §3.1 · Make the control panel pleasant and complete for daily use.

## Summary
Turn the read-only skeleton popup (EXT-1) into a real control panel: inline note editing,
per-contact status, skip, live progress, daily-cap meter, and settings.

## Scope / tasks
- [ ] **Inline note edit:** ✏️ per row → edit the ≤300-char note (live counter) → `POST
      /api/ext/note`; the edited note is what EXT-2 fills.
- [ ] **Per-row status chips:** `ready | composing | sent ✓ | needs manual | skipped`, updated
      live from background/content events.
- [ ] **Row actions:** ⏭️ Skip (marks `skipped`, advances), open-profile link.
- [ ] **Queue controls:** `Start` / `Pause` / `Next`; overall progress (`3 / 5 sent`).
- [ ] **Daily-cap meter:** `N / limit today` with reset time.
- [ ] **Settings panel:** daily cap, pacing delay, "confirm before each" toggle, target job
      filter (all jobs vs. a specific `job_url`).
- [ ] **Connection indicator:** green/red dot for ApplyPilot reachability; friendly reconnect.

## Acceptance criteria
- User can edit a note in the popup and the edited version is the one filled on the page.
- Every row shows an accurate live status; skip works and advances.
- Progress + cap meter update as the user sends; settings persist across sessions.

## Tests
- Unit: note-edit save (cap at 300) + status-chip state machine.
- Manual: edit a note, run the queue, confirm the edited note is sent; skip mid-queue.

## Out of scope
Message flow (EXT-6). Web Store packaging (§8, not in scope for v1).

---

## Review deltas (v2 — folded from `chrome-extension-review.md`)

- **H3 — the running loop must NOT depend on the popup.** The popup closes on every Send. Move
  live progress (`N/total`), Pause/Hold, and Skip onto the **on-page overlay** (survives clicks);
  keep the popup for setup/queue management.
- **Note-edit vs. already-composed:** editing a note after it's filled onto the page leaves the
  textarea on v1 while the popup shows v2. Re-run the React-safe fill on edit of the
  active/composed contact, OR lock editing for it with a clear note.
- **Dedupe the all-jobs view by normalized `linkedin_url`** (same person across two jobs =
  duplicate invite attempt); when one instance is sent, auto-mark siblings.
- Consider defaulting to **confirm-to-advance** rather than a timer (the human is already there).

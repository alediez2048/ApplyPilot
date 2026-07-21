# EXT-0 — Local API: queue + status endpoints in ApplyPilot

**Phase:** 0 · **Size:** S · **Depends on:** — · **Status:** Todo
**PRD:** §5 · The thin server contract the extension is a client of. Pure ApplyPilot (Python);
no extension code yet.

## Summary
Add three loopback-only endpoints to the existing dashboard server so an extension can pull the
ready LinkedIn contacts, persist an inline note edit, and report send status. Reuses the
`contacts` table and its `dm_status` column — no new data model.

## Scope / tasks
- [ ] **`GET /api/ext/queue`** (optional `?job_url=`) → JSON array of ready LinkedIn contacts:
      `[{ id, full_name, title, company, linkedin_url, note }]`. "Ready" = has `linkedin_url`
      + `linkedin_message` + `dm_status != 'sent'`. Reuse `_eligible_contact_ids(..., "linkedin")`.
- [ ] **`POST /api/ext/status`** `{ contact_id, status: "sent"|"manual"|"skipped" }` →
      update `dm_status` (`sent` → `store.mark_dm_sent`; add `mark_dm_manual`/`mark_dm_skipped`
      or map both to a single `manual` state). Returns `{ ok }`.
- [ ] **`POST /api/ext/note`** `{ contact_id, note }` → persist the ≤300-char note (reuse the
      existing outreach save path / `upsert_contact` with `linkedin_message`). Cap at 300.
- [ ] **Origin/CSRF:** extend `_origin_ok` to allow the extension origin
      (`chrome-extension://<id>`) in addition to loopback. Keep the Host loopback check.
- [ ] **`store.py`:** add `mark_dm_manual(contact_id)` (status `manual`, no claim/dedupe).

## Acceptance criteria
- `curl localhost:8765/api/ext/queue` returns only ready LinkedIn contacts with their notes.
- Posting `status:sent` flips `dm_status` to `sent` (dashboard reflects it); `manual`/`skipped`
  recorded distinctly enough to not re-surface the contact in the queue.
- Note edit persists and is returned on the next `/queue` fetch.
- A cross-origin POST from a non-allowlisted origin is rejected (existing guard).

## Tests
- queue eligibility (ready vs. no-url vs. no-note vs. already-sent).
- status transitions update `dm_status`; note edit persists + caps at 300.
- Origin guard: extension origin allowed, arbitrary origin rejected.

## Out of scope
Any extension code (EXT-1+). Auth token (loopback + Origin allowlist is v1; token is a §8 option).

---

## Review deltas (v2 — folded from `chrome-extension-review.md`)

- **B3 — exclude the done-set, not just `sent`.** Reusing `_eligible_contact_ids` requires
  *modifying* it: `dm_status in ('sent','manual','skipped')` (not `== 'sent'`) — and the same
  in `dm_ready`. `composed` stays eligible (human hasn't sent). **Done in code** (shared
  `_DM_DONE_STATUSES` in `web_dashboard.py`); this ticket now just wires the `manual`/`skipped`
  statuses that flow into it. Test covers manual/skipped exclusion + composed inclusion.
- **B5 — `mark_dm_sent` stamps `dm_sent_at`.** **Done in code** (`store.py`, `COALESCE` to keep
  a prior claim ts) + `mark_dm_manual` (stamps, counts toward dedupe/cap) + `mark_dm_skipped`
  (no stamp). `POST /api/ext/status` maps `sent → mark_dm_sent`, `manual → mark_dm_manual`,
  `skipped → mark_dm_skipped`. Test asserts `dm_sent_at` non-NULL + `already_dmed` finds it.
- **`POST /api/ext/note` must NOT reuse `_save_or_regen_draft`** — that path sets
  `outreach_status='drafted'` + blanks `outreach_subject/message` (clobbers email state) and has
  no cap. Call `upsert_contact({'id':cid,'linkedin_message':note[:300]})` directly; enforce 300
  server-side.
- **Guard the new GET.** `_origin_ok` only runs in `do_POST`; `/api/ext/queue` returns PII +
  notes. Apply the **Host-loopback half** of `_origin_ok` to the GET (not the Origin half — the
  extension's `chrome-extension://` Origin would fail it). **No CORS headers** (extension reads
  via host-permission bypass).
- **Extension identity:** allow by *scheme* (`chrome-extension`) or the shared token, not a
  hardcoded `chrome-extension://<id>` (unstable for load-unpacked). Prefer the **mutual shared
  token** (H4) — also lets the extension refuse a `:8765` squatter.
- **All-jobs queue variant:** `job_url` omitted has no helper (`_eligible_contact_ids` is
  per-job). Add a single SELECT over `contacts` filtered by
  `linkedin_url`/`linkedin_message`/`dm_status NOT IN (sent,manual,skipped)`; dispatch on
  presence of `job_url`. Dedupe by normalized `linkedin_url` (same person across two jobs).
- **Enum comment** `store.py` updated to `none|sending|composed|sent|manual|skipped|failed`. ✅

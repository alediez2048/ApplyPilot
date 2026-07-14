# NET-4 — Gmail send automation + safeguards

**Phase:** 4 · **Size:** M · **Depends on:** NET-2, NET-3 · **Status:** Todo
**PRD:** §8, §7 · **Tier gate:** `GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD`

## Summary
A dashboard **"Send email"** button that actually emails the contact from the user's
Gmail. Default transport is **SMTP + Gmail App Password** (stdlib `smtplib`, zero new
deps). Every send is user-initiated and confirmed; guessed emails require an extra
confirm; a daily cap and de-dupe prevent spam/mistakes.

## Scope / tasks
- [ ] **`networking/gmail_send.py`**:
  - [ ] `send_email(to, subject, body, from_addr, from_name) -> {ok, message_id, error}`
        via `smtplib.SMTP_SSL('smtp.gmail.com', 465)` + app password
  - [ ] MIME assembly (plain text + honest sign-off footer); reply-to = from
  - [ ] `can_send(contact) -> (bool, reason)`: verified-gate, daily-cap, dedupe checks
  - [ ] `sent_today()` counter from `contacts.sent_at` for the daily cap
- [ ] **`store.py`** — add send columns (`sent_at`, `sent_message_id`, `send_error`) +
      `mark_sent` / `mark_send_failed`.
- [ ] **Endpoint** `POST /api/outreach/send` — `{contact_id, confirm_guessed?}`:
  - [ ] enforce `can_send`; send; record result; return new status
  - [ ] refuse (clear message) if unverified without `confirm_guessed`, cap hit, or already sent
- [ ] **Dashboard** — `[send email]` button per contact:
  - [ ] verified → one confirm dialog → send
  - [ ] guessed/locked → second explicit confirm ("email is a guess — send anyway?")
  - [ ] row flips to ✅ **sent {timestamp}** or ⛔ **failed {error}**; button disabled after sent
- [ ] **Dry-run** — `applypilot network --dry-run` / dashboard toggle logs the email
      (to/subject/body) instead of sending.
- [ ] **Config** — `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `OUTREACH_FROM_NAME`,
      `OUTREACH_DAILY_LIMIT` (default 20); `.env.example` + `doctor` readiness line.

## Safeguards (must all hold)
- Never auto-sends: one user click + confirm per email.
- `email_status in (guessed, locked)` requires a second explicit confirm.
- Hard **daily cap** across all jobs; refuse beyond it with a clear message.
- **De-dupe**: `sent_at` set → refuse re-send for that contact/job.
- App password read only from `.env`; never logged; TLS only.

## Acceptance criteria
- Send-to-self test: `[send email]` on a contact whose email is your own address delivers
  a correctly-formatted email; row shows ✅ sent with a timestamp + message id.
- Guessed-email contact requires the second confirm before sending.
- After `OUTREACH_DAILY_LIMIT` sends, further sends are refused with a clear message.
- Re-clicking send on a sent contact is a no-op (already sent).
- Missing Gmail creds → button shows "configure GMAIL_ADDRESS/GMAIL_APP_PASSWORD", no crash.

## Tests
- `gmail_send` MIME assembly (subject/body/from/footer) with SMTP stubbed.
- `can_send`: verified passes, guessed blocked w/o confirm, cap reached blocks, dedupe blocks.
- `sent_today` counting.
- Endpoint: happy path, guessed-without-confirm rejected, cap rejected, dedupe rejected.
- Gated integration: real send-to-self behind an env flag (skipped in CI).

## Out of scope
Gmail API OAuth transport, threaded follow-ups, auto-send-after-apply (NET-6).

# CAL-1 — Send a Google Calendar invite from the contact view

**Size:** M · **Depends on:** a new OAuth consent · **Status:** SCOPED, not built

Asked for as: *"choose a day, time, description, google meet invite and send straight from
there."*

---

## Five measurements, taken before designing. Three move the ticket.

**1. The calendar scope is NOT granted, and it would be the first non-Gmail one.** The live
token carries exactly four:

    gmail.send · gmail.metadata · gmail.settings.basic · gmail.readonly

Creating an event needs `https://www.googleapis.com/auth/calendar.events`. That is a **new
consent screen** and a re-authorisation — not a config change. It is the single biggest cost in
this ticket and everything below assumes the operator agrees to it.

**2. Google Meet is the reason this cannot be the thing that was already built and reverted.**
`3480c37` (reverted in `82eb429`) sent a real `.ics` with `METHOD:REQUEST` as a mail attachment,
deliberately to avoid a new scope. That approach **cannot** produce a Meet link and does not put
the meeting on the sender's own calendar — its own commit message says so. A Meet link requires
`conferenceData` with `conferenceDataVersion=1` on `events.insert`, which requires the scope.
So the reverted work is not revivable for this ask; only its ics builder and its "who is the
organiser" handling are reusable, and neither is the hard part.

**3. It answers the operator's own objection to the last attempt.** That revert was
*"the whole point of the cal.com link is to skip calendar invites"*, and it was right for what
was built. The distinction that makes THIS different is worth stating because it is the whole
justification: **cal.com is for a time that has NOT been agreed** — they pick from your
availability. A direct invite is for a time that HAS been agreed, in the thread, usually by
them: *"how about Tuesday at 2?"* Sending a booking link back at that point is the wrong reply.

**4. The population is small, engaged, and exactly the right one.** 28 contacts have written
back, 15 carry a `replied_at`, 2 booked calls were detected, 1 meeting transcript is stored.
This is not a bulk feature and must never become one — it is for the handful of conversations
that got somewhere.

**5. No new dependency.** `google-api-python-client` and `google-auth-oauthlib` are already
required by the `gmail` extra. The Calendar API is the same client, a different `build()`.

### And one measurement about safety, because CLAUDE.md says to re-run it

The apply agent's allowlist is `mcp__playwright,mcp__gmail__send_email`, with `Read`, `Bash`,
`Write`, `Edit`, `Glob`, `Grep`, `WebFetch` and `Task` denied — so it cannot read
`~/.applypilot/gmail_token.json` and cannot call a calendar tool that is not in its allowlist.
**Adding `calendar.events` does not widen what the AGENT can do.** It widens what a *stolen
token* can do, which is a real but different exposure: the token is unencrypted on disk behind
`chmod 600` + FileVault. State it; do not pretend it is nothing.

---

## The shape

**It belongs on the `📝 Meetings` tab**, which today holds transcripts. A meeting you are going
to have and a meeting you had are the same noun at two times, and splitting them across two tabs
would put "when are we speaking" in a different room from "what we said" — the mistake UX-1
already corrected once for engagement.

    📝 Meetings
      ── Upcoming ──────────────────────────────
      Tue 19 Aug, 2:00 PM (30 min) · Google Meet      Edit  Cancel
      ── Past ─────────────────────────────────
      Aug 13 · 420 chars · "I had a really interesting…"   Read  ✕
      [ + Add a transcript ]
      [ + Schedule a call ]

**The form is deliberately short**, because everything in it is already known: date, time,
duration (15/30/45/60), title (defaults to `{Your name} × {Their name}`), an optional
description, and a **Meet** toggle. Attendees default to this contact; anyone Cc'd on the live
thread is offered as a checkbox, using `reply_target`'s Cc — the same list a reply would carry,
so the meeting reaches whoever the conversation reaches.

**Google sends the invitation, not us** (`sendUpdates=all` on insert). That is the point — it
arrives as a real invite with RSVP buttons from the operator's own account, which is what the
.ics attempt was approximating.

---

## Decisions, and why

| Decision | Why |
|---|---|
| `calendar.events`, never `calendar` | Full `calendar` grants read of every event on every calendar. `events` is enough to create, patch and delete. Ask for the smaller one. |
| The scope is **opt-in**, exactly like `CONTENT_SCOPE` | `gmail_oauth.SCOPES` must not gain it, or the ordinary `--gmail-connect` silently starts asking for calendar access. Added only by an explicit `--with-calendar`, and a test pins that it is absent from `SCOPES` — the pattern CRM-4b already established for `gmail.readonly`. |
| The send guards still apply | `sendUpdates=all` means Google mails it, so it bypasses `gmail_send` entirely — the daily limit, the per-company cap and the cooldown never see it. Check them BEFORE `events.insert`, or "no more than N a day" quietly stops being true. §Lessons 77 is this exact shape: a path that sends without being counted. |
| Cancel and reschedule ship WITH send | An invite you can send and not withdraw is half a feature, and the half you need in a hurry. `events.patch` / `events.delete`, both `sendUpdates=all`. This is why the event id must be stored. |
| The event id lives in `interactions` | It already has a `booked` kind and 2 rows. A new kind `invited`, with the event id in `detail`, needs no schema change — and `interactions` is exactly where "an event with nowhere else to live" belongs. |
| An explicit `timeZone` on every event | Omitted, Google uses the calendar's default, which is not necessarily the operator's — and a meeting an hour out is worse than no meeting. Send IANA (`America/Chicago`), never a UTC offset, or DST moves it. |
| The confirm names **person, local time, and Meet** | This is outward-facing and lands on a stranger's phone. §Lessons 29's rule: the operator must see who it reaches before they can meaningfully click. |
| Never automatic, never bulk | Nothing schedules a meeting on the operator's behalf. Not on the poller, not in `tick`, not in the bulk follow-up path. |

---

## Phases

**Phase 1 — the grant.** `--with-calendar` on `network --gmail-connect`, `calendar.events`
kept out of `SCOPES`, `doctor` reports whether it is held. **Nothing else works without this**,
and it is the step that needs the operator at the keyboard. ~half a day.

**Phase 2 — send.** The form on the Meetings tab, `events.insert` with `conferenceData` when
Meet is on, guards checked first, `interactions` row written with the event id. ~1 day.

**Phase 3 — cancel and reschedule.** `patch` / `delete`, and the Upcoming block rendering from
stored ids. ~half a day.

**Phase 4 (optional) — read back.** Detect that they ACCEPTED, which is a genuine engagement
signal and strictly better than the cal.com booking-email detection already in `interactions`.
Needs `calendar.events.readonly` or a poll of the same event. Only worth it if phases 1–3 get
used.

---

## Not doing, and why

| | |
|---|---|
| **Replacing the cal.com link** | It answers a different question — a time not yet agreed. Both belong; the invite is for after they name one. |
| **An `.ics` attachment as well** | Two invitations for one meeting is how a calendar ends up with duplicates. The API path supersedes `3480c37`. |
| **Availability / free-busy** | That is what cal.com already does, properly, and it needs a wider scope. |
| **Anything in the apply agent's reach** | Its allowlist is two tools. Keep it that way; a calendar MCP tool must never be added to it. |
| **Auto-scheduling from a detected "how about Tuesday?"** | `domain/intent.py` could classify it, but acting on a parsed time would put a wrong meeting in a real calendar. Offer to open the form pre-filled at most, and only later. |

---

## Open questions for the operator

1. **Is the new consent acceptable?** Everything here is behind it. If not, this ticket ends
   and the honest answer is the cal.com link you already have.
2. **Which calendar?** `primary` is the obvious default; a second one would need to be named.
3. **Does it need cancel/reschedule on day one, or is send enough to try it?** Phase 3 is small
   but it is the difference between an experiment and something you can rely on.

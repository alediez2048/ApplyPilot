"""004 — `transcripts` and `transcript_contacts`, for GRAN-1.

Two tables rather than a column on `contacts`, and the split is the design:

**One row per MEETING.** A call is one event with several attendees. Storing the text per contact
duplicates a 20–50 KB blob per person and lets the copies drift, which is the `emailed` bug's
shape (§Lessons 21) at a hundred times the size.

**A JOIN for who was on it**, keyed `(transcript_id, contact_id)` — exactly what migration 002
had to do to `messages` after `INSERT OR REPLACE` on a shared key moved all three Writer messages
onto one contact and left another's conversation empty (§Lessons 36). One meeting legitimately
belongs to several people.

**Not in `messages`.** That table is keyed on Gmail's own message id and carries `thread_id` /
`rfc_message_id`; a meeting has none of them, and inventing them corrupts the join reply detection
runs on. `messages.snippet` also caps at 200 (auto) / 2000 (pasted) — live mean 75 — against a
transcript three orders of magnitude larger. UX-2 hit the same wall with LinkedIn DMs and put them
in `interactions` instead; this is too big for that too.

Idempotent — `CREATE TABLE IF NOT EXISTS` only, no seed. Nothing gates on these tables being
non-empty, unlike 003's registries, so there is nothing to seed and no `INSERT` to get wrong.
"""

from __future__ import annotations

import sqlite3

NOTE = "transcripts + transcript_contacts created (GRAN-1)"

_TRANSCRIPTS = """
CREATE TABLE IF NOT EXISTS transcripts (
    id            TEXT PRIMARY KEY,
    source        TEXT NOT NULL DEFAULT 'paste',
    external_id   TEXT DEFAULT '',
    title         TEXT DEFAULT '',
    started_at    TEXT DEFAULT '',
    duration_s    INTEGER DEFAULT 0,
    summary       TEXT DEFAULT '',
    body          TEXT DEFAULT '',
    attendees     TEXT DEFAULT '',
    created_at    TEXT DEFAULT ''
)
"""

#: `matched_by` is provenance, and it is load-bearing rather than decoration. §Lessons 34 and
#: §Lessons 86 are one failure twice: a guess laundered into a stored fact and then trusted
#: forever — a domain read off a hostname was fed to a check whose docstring called a mismatch
#: "near-proof", and a correct name written back by `doctor --fix-employers` disarmed the guard
#: that depended on how the name had been derived.
#:
#: A transcript attached because the OPERATOR chose the contact is a different claim from one
#: attached because a calendar name looked similar, and only the first should ever reach a
#: prompt with confidence.
_JOIN = """
CREATE TABLE IF NOT EXISTS transcript_contacts (
    transcript_id TEXT NOT NULL,
    contact_id    TEXT NOT NULL,
    matched_by    TEXT DEFAULT 'manual',
    created_at    TEXT DEFAULT '',
    PRIMARY KEY (transcript_id, contact_id)
)
"""


def up(conn: sqlite3.Connection) -> dict:
    conn.execute(_TRANSCRIPTS)
    conn.execute(_JOIN)
    # Every read is "the meetings with this person", so the contact side is the index that
    # matters. Without it the contact panel table-scans the join on every open.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tc_contact "
                 "ON transcript_contacts(contact_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_transcripts_started "
                 "ON transcripts(started_at)")
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
    return {"transcripts": n, "note": NOTE}

"""Storing meeting transcripts and who was on them. GRAN-1's write half.

The judgement is all in `domain/transcript.py`; this is the layer that touches SQLite.

Idempotent by construction, like `messages` and `interactions` before it: the id is derived from
`(source, external_id)` or from the content, so re-pasting a note you already stored is an UPDATE
and re-polling a rolling window is a no-op. §Lessons 22 — idempotence has to be tested by running
it twice, not by reasoning about it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from applypilot.database import get_connection, schema_ready
from applypilot.domain import transcript as _t

#: Matched because the OPERATOR said so. The only provenance a paste can honestly claim.
MANUAL = "manual"
#: An exact address match against a stored contact. The only one an automated import may write
#: without asking — §Lessons 68 is what a fuzzy match costs when it is trusted.
BY_EMAIL = "email"
#: A name that looked similar. OFFERED, never written silently.
BY_NAME = "name"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ready(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Create the tables if the migration has not run in this process yet.

    Cheap and idempotent, and it is what lets a test drive this module against a bare database
    without booting the migration runner.
    """
    if schema_ready(conn, "transcript_contacts"):
        return conn
    from applypilot.migrations.m004_transcripts import up
    up(conn)
    return conn


def save(*, body: str, contact_ids: list[str], source: str = "paste", external_id: str = "",
         title: str = "", started_at: str = "", duration_s: int = 0, summary: str = "",
         attendees: list | None = None, matched_by: str = MANUAL,
         conn: sqlite3.Connection | None = None) -> dict:
    """Store one meeting and attach it to people. Returns {id, added, attached}.

    Raises `TranscriptError` for an empty or oversized body — refused with a reason rather than
    truncated, because a silently shortened transcript is evidence the operator believes they
    have and do not (§Lessons 90).
    """
    conn = _ready(conn or get_connection())
    text = _t.validate(body)
    # The id is derived from what the CALLER supplied, never from the defaulted timestamp.
    #
    # Seeding it with `started or _now()` made every paste unique: the same note pasted twice
    # produced two rows, because the default moved between the two calls. Found by calling
    # `save` twice and reading the ids, which is the only way this shows up — §Lessons 22, where
    # a bounced contact re-detected on every poll wrote eleven identical log lines.
    tid = _t.transcript_id(source, external_id, body=text,
                           started_at=(started_at or "").strip())
    started = (started_at or "").strip() or _now()

    # A supplied summary is kept verbatim (bounded); with none, an EXCERPT stands in and the
    # caller is told which it is by `summary_is_excerpt`. They are not the same thing and the
    # panel must not present the first 1,200 characters of greetings as a summary.
    supplied = _t.clamp_summary(summary)
    stored_summary = supplied or _t.excerpt(text)

    existed = conn.execute("SELECT 1 FROM transcripts WHERE id = ?", (tid,)).fetchone() is not None
    conn.execute(
        "INSERT OR REPLACE INTO transcripts (id, source, external_id, title, started_at, "
        "duration_s, summary, body, attendees, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (tid, (source or "paste").strip().lower(), (external_id or "").strip(),
         (title or "").strip(), started, int(duration_s or 0), stored_summary, text,
         json.dumps(attendees or []), _now()))

    attached = 0
    for cid in dict.fromkeys(c for c in (contact_ids or []) if c):
        cur = conn.execute(
            "INSERT OR IGNORE INTO transcript_contacts (transcript_id, contact_id, matched_by, "
            "created_at) VALUES (?,?,?,?)", (tid, cid, matched_by, _now()))
        attached += cur.rowcount
    conn.commit()
    return {"id": tid, "added": not existed, "attached": attached,
            "summary_is_excerpt": not supplied}


def for_contact(contact_id: str, conn: sqlite3.Connection | None = None) -> list[dict]:
    """Meetings with one person, newest first. **Never returns the body.**

    The body is 20–50 KB and this feeds a panel that re-renders every 2.5 seconds; shipping it
    on that path is §Lessons 26's shape with bytes instead of round-trips. `body_for` fetches one
    on demand.
    """
    conn = _ready(conn or get_connection())
    rows = conn.execute(
        "SELECT t.id, t.source, t.title, t.started_at, t.duration_s, t.summary, "
        "       LENGTH(t.body) AS body_len, tc.matched_by "
        "FROM transcripts t JOIN transcript_contacts tc ON tc.transcript_id = t.id "
        "WHERE tc.contact_id = ? ORDER BY t.started_at DESC", (contact_id,)).fetchall()
    return [dict(zip(r.keys(), r)) for r in rows]


def for_contacts(contact_ids: list[str],
                 conn: sqlite3.Connection | None = None) -> dict[str, list[dict]]:
    """The same, for many people, in ONE query.

    `/api/status` renders every contact on every job under an 80-statement budget, so a
    per-contact read here would blow it the moment a job had a few people — the exact shape
    `interactions_store.for_job` exists to avoid.
    """
    ids = [c for c in dict.fromkeys(contact_ids or []) if c]
    if not ids:
        return {}
    conn = _ready(conn or get_connection())
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT tc.contact_id, t.id, t.source, t.title, t.started_at, t.duration_s, "  # noqa: S608
        f"       t.summary, LENGTH(t.body) AS body_len, tc.matched_by "
        f"FROM transcripts t JOIN transcript_contacts tc ON tc.transcript_id = t.id "
        f"WHERE tc.contact_id IN ({marks}) ORDER BY t.started_at DESC", ids).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        d = dict(zip(r.keys(), r))
        out.setdefault(d.pop("contact_id"), []).append(d)
    return out


def body_for(transcript_id: str, conn: sqlite3.Connection | None = None) -> dict | None:
    """The full text of one meeting, fetched deliberately."""
    conn = _ready(conn or get_connection())
    row = conn.execute(
        "SELECT id, source, title, started_at, duration_s, summary, body, attendees "
        "FROM transcripts WHERE id = ?", (transcript_id,)).fetchone()
    return dict(zip(row.keys(), row)) if row else None


def detach(transcript_id: str, contact_id: str,
           conn: sqlite3.Connection | None = None) -> int:
    """Remove one person from a meeting, and the meeting itself once nobody is left.

    Deleting the transcript while another contact is still attached would empty that person's
    record from under them — the same reasoning `delete_for_contact` uses in every other store
    here, applied to a row that several people share.
    """
    conn = _ready(conn or get_connection())
    cur = conn.execute(
        "DELETE FROM transcript_contacts WHERE transcript_id = ? AND contact_id = ?",
        (transcript_id, contact_id))
    left = conn.execute(
        "SELECT COUNT(*) FROM transcript_contacts WHERE transcript_id = ?",
        (transcript_id,)).fetchone()[0]
    if not left:
        conn.execute("DELETE FROM transcripts WHERE id = ?", (transcript_id,))
    conn.commit()
    return cur.rowcount


def delete_for_contact(contact_id: str, conn: sqlite3.Connection | None = None) -> int:
    """Every attachment for one contact, for when the contact is deleted.

    Same reasoning as `touches`, `messages` and `interactions`: contact ids are a hash of
    (job, identity), so a re-discovered person reproduces the id and would inherit a stranger's
    meetings.
    """
    conn = _ready(conn or get_connection())
    ids = [r[0] for r in conn.execute(
        "SELECT transcript_id FROM transcript_contacts WHERE contact_id = ?",
        (contact_id,)).fetchall()]
    n = 0
    for tid in ids:
        n += detach(tid, contact_id, conn)
    return n

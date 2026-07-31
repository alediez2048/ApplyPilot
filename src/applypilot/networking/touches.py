"""Follow-up touches — one table for every channel (ARCH-3).

Replaces ten columns on `contacts` that were one concept copy-pasted per channel:

    followed_up_at  followup_count  followup_status  followup_message  followup_subject  followup_error
    li_followed_up_at  li_followup_count  li_followup_status  li_followup_message   (no subject, no error)

The LinkedIn copy was already missing two of the six. That is the cost this table removes:
not storage at 28 rows, but the fact that every change had to be made twice and one of the
two copies quietly fell behind.

TWO tables, not one — because `followup_status` was doing two unrelated jobs:

    drafted | sending | sent | failed    the delivery lifecycle of ONE touch      -> touches.status
    stopped | replied                    the terminal state of the WHOLE sequence -> sequences.status

`claim_followup_send` had to guard on `NOT IN ('sending','stopped','replied')` — one column,
two lifecycles, one condition covering both. Splitting them is what lets the ladder engine
stop caring which channel it is looking at, and it gives CRM-1 (reply detection) a single
row to write instead of a per-channel column to find.

Adding SMS is a Channel entry in `domain/followup.py` plus a prompt. No schema change:
`channel` is data here, not a column name.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from hashlib import sha1

from applypilot.database import get_connection, schema_ready

# A touch's own delivery lifecycle. Sequence-terminal states are NOT in this list —
# they live in `sequences.status`, which is the entire point of the split.
TOUCH_STATUSES = ("drafted", "sending", "sent", "failed")

_TOUCH_COLUMNS: dict[str, str] = {
    "id": "TEXT PRIMARY KEY",
    "contact_id": "TEXT NOT NULL",
    "channel": "TEXT NOT NULL",     # email | linkedin | … — data, never a column name
    "seq": "INTEGER NOT NULL",      # 1-based, per (contact_id, channel). NOT global.
    "due_at": "TEXT",               # when this touch came due (recorded, not scheduled)
    "sent_at": "TEXT",
    "subject": "TEXT",              # LinkedIn leaves this empty; it is not a missing column
    "body": "TEXT",
    "status": "TEXT",               # see TOUCH_STATUSES
    "error": "TEXT",
    "created_at": "TEXT",
    "updated_at": "TEXT",
}

_SEQUENCE_COLUMNS: dict[str, str] = {
    "contact_id": "TEXT NOT NULL",
    "channel": "TEXT NOT NULL",
    "status": "TEXT",               # '' | stopped | replied — halts the ladder
    "note": "TEXT",                 # why, when we know (CRM-1 will fill this)
    "updated_at": "TEXT",
}


def touch_id(contact_id: str, channel: str, seq: int) -> str:
    """Deterministic, so a replayed write updates rather than duplicating a touch."""
    return sha1(f"{contact_id}\x1f{channel}\x1f{seq}".encode()).hexdigest()[:16]


def init_touches(conn: sqlite3.Connection | None = None) -> sqlite3.Connection:
    """Create both tables + indexes. Idempotent; safe to call from every read path."""
    if conn is None:
        conn = get_connection()
    if schema_ready(conn, "touches"):
        return conn
    cols = ", ".join(f"{n} {t}" for n, t in _TOUCH_COLUMNS.items())
    conn.execute(f"CREATE TABLE IF NOT EXISTS touches ({cols})")
    seq_cols = ", ".join(f"{n} {t}" for n, t in _SEQUENCE_COLUMNS.items())
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS sequences ({seq_cols}, PRIMARY KEY (contact_id, channel))"
    )
    # Reads are always "this contact, this channel, in order".
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_touches_ladder "
                 "ON touches(contact_id, channel, seq)")
    # What a scheduler asks for (CRM-3): what is owed, oldest first.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_touches_due ON touches(status, due_at)")
    conn.commit()
    _ensure_columns(conn, "touches", _TOUCH_COLUMNS)
    _ensure_columns(conn, "sequences", _SEQUENCE_COLUMNS)
    return conn


def _ensure_columns(conn: sqlite3.Connection, table: str, spec: dict[str, str]) -> list[str]:
    """Forward-only column migration, same pattern as contacts/jobs."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    added = []
    for col, dtype in spec.items():
        if col not in existing and "PRIMARY KEY" not in dtype:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {dtype}")
            added.append(col)
    if added:
        conn.commit()
    return added


# ── reads ───────────────────────────────────────────────────────────────────

def _empty_state() -> dict:
    return {"count": 0, "last_sent_at": "", "sequence_status": "",
            "touch_status": "", "draft_subject": "", "draft_body": "", "error": ""}


def ladder_state(contact_id: str, channel: str,
                 conn: sqlite3.Connection | None = None) -> dict:
    """Everything the scheduling engine needs about one (contact, channel).

    Shape is identical for every channel — that is what removed the per-channel branches.
    """
    if conn is None:
        conn = get_connection()
    return ladder_states([contact_id], conn).get((contact_id, channel)) or _empty_state()


def sent_touches(contact_id: str, channel: str = "email",
                 conn: sqlite3.Connection | None = None) -> list[dict]:
    """The follow-ups actually SENT to one contact, oldest first, with their text.

    This is the missing middle of a conversation. The first email lives on `contacts`, the
    reply lives in `messages`, and everything in between is here — so a reply drafted without
    it can cheerfully repeat a point that was already made twice.
    """
    if conn is None:
        conn = get_connection()
    init_touches(conn)
    rows = conn.execute(
        "SELECT seq, subject, body, sent_at FROM touches "
        "WHERE contact_id = ? AND channel = ? AND sent_at IS NOT NULL ORDER BY seq",
        (contact_id, channel)).fetchall()
    return [dict(zip(r.keys(), r)) for r in rows]


def all_sent_touches(conn: sqlite3.Connection | None = None) -> list[dict]:
    """Every touch that was actually SENT — the input to by_touch() (CRM-2).

    Only sent ones: a drafted-but-unsent follow-up did not influence whether anybody replied,
    and counting it would credit the schedule for work that never left the outbox.
    """
    if conn is None:
        conn = get_connection()
    init_touches(conn)
    rows = conn.execute(
        "SELECT contact_id, channel, seq, sent_at FROM touches WHERE sent_at IS NOT NULL"
    ).fetchall()
    return [dict(zip(r.keys(), r)) for r in rows]


def ladder_states(contact_ids: list[str],
                  conn: sqlite3.Connection | None = None) -> dict[tuple[str, str], dict]:
    """Bulk load, keyed by (contact_id, channel).

    The dashboard renders every contact on every refresh (2.5s). Doing this per contact
    per channel would be an N+1 against SQLite on the request path.
    """
    if conn is None:
        conn = get_connection()
    if not contact_ids:
        return {}
    out: dict[tuple[str, str], dict] = {}
    marks = ",".join("?" for _ in contact_ids)

    for row in conn.execute(
        f"SELECT contact_id, channel, seq, sent_at, status, subject, body, error "
        f"FROM touches WHERE contact_id IN ({marks}) ORDER BY contact_id, channel, seq",
        contact_ids,
    ):
        key = (row["contact_id"], row["channel"])
        st = out.setdefault(key, _empty_state())
        if row["status"] == "sent":
            st["count"] += 1
            # Rows come back in seq order, so the last one to land is the most recent.
            st["last_sent_at"] = row["sent_at"] or st["last_sent_at"]
        else:
            # An unsent row is the pending draft for the NEXT touch.
            st["touch_status"] = row["status"] or ""
            st["draft_subject"] = row["subject"] or ""
            st["draft_body"] = row["body"] or ""
            st["error"] = row["error"] or ""

    for row in conn.execute(
        f"SELECT contact_id, channel, status FROM sequences WHERE contact_id IN ({marks})",
        contact_ids,
    ):
        key = (row["contact_id"], row["channel"])
        out.setdefault(key, _empty_state())["sequence_status"] = row["status"] or ""
    return out


# ── writes ──────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _upsert_touch(conn: sqlite3.Connection, contact_id: str, channel: str, seq: int,
                  **fields) -> None:
    now = _now()
    tid = touch_id(contact_id, channel, seq)
    exists = conn.execute("SELECT 1 FROM touches WHERE id = ?", (tid,)).fetchone()
    if exists:
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE touches SET {sets}, updated_at = ? WHERE id = ?",
                     (*fields.values(), now, tid))
    else:
        keys = ["id", "contact_id", "channel", "seq", "created_at", "updated_at", *fields]
        vals = [tid, contact_id, channel, seq, now, now, *fields.values()]
        conn.execute(f"INSERT INTO touches ({','.join(keys)}) "
                     f"VALUES ({','.join('?' for _ in keys)})", vals)


def next_seq(contact_id: str, channel: str, conn: sqlite3.Connection | None = None) -> int:
    """1-based, per (contact_id, channel) — never a global counter.

    A global counter would label the LinkedIn ladder's first touch "touch 3 of 2" for
    anyone who had already had two emails.
    """
    if conn is None:
        conn = get_connection()
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM touches "
                       "WHERE contact_id = ? AND channel = ?", (contact_id, channel)).fetchone()
    return (row["m"] or 0) + 1


def set_draft(contact_id: str, channel: str, subject: str, body: str,
              conn: sqlite3.Connection | None = None) -> int:
    """Stage the next touch. Returns its seq."""
    if conn is None:
        conn = get_connection()
    pending = conn.execute(
        "SELECT seq FROM touches WHERE contact_id = ? AND channel = ? AND status != 'sent' "
        "ORDER BY seq LIMIT 1", (contact_id, channel)).fetchone()
    seq = pending["seq"] if pending else next_seq(contact_id, channel, conn)
    _upsert_touch(conn, contact_id, channel, seq,
                  subject=subject or "", body=body or "", status="drafted", error=None)
    conn.commit()
    return seq


def claim_send(contact_id: str, channel: str,
               conn: sqlite3.Connection | None = None) -> bool:
    """Atomically claim the pending touch for sending.

    Mirrors store.claim_for_send: under the threading server two clicks race, and a
    duplicate follow-up is the worst bug this subsystem can produce. The sequence-terminal
    check is now a separate table read rather than a value smuggled into the same column.
    """
    if conn is None:
        conn = get_connection()
    seq_row = conn.execute("SELECT status FROM sequences WHERE contact_id = ? AND channel = ?",
                           (contact_id, channel)).fetchone()
    if seq_row and (seq_row["status"] or "") in ("stopped", "replied"):
        return False
    cur = conn.execute(
        "UPDATE touches SET status = 'sending', updated_at = ? "
        "WHERE contact_id = ? AND channel = ? AND status IN ('drafted', 'failed')",
        (_now(), contact_id, channel),
    )
    conn.commit()
    return cur.rowcount >= 1


def record_sent(contact_id: str, channel: str, due_at: str = "",
                conn: sqlite3.Connection | None = None) -> int:
    """Mark the pending touch sent. Returns the new sent-count (the "touch N" label)."""
    if conn is None:
        conn = get_connection()
    now = _now()
    pending = conn.execute(
        "SELECT seq FROM touches WHERE contact_id = ? AND channel = ? AND status != 'sent' "
        "ORDER BY seq LIMIT 1", (contact_id, channel)).fetchone()
    seq = pending["seq"] if pending else next_seq(contact_id, channel, conn)
    _upsert_touch(conn, contact_id, channel, seq,
                  status="sent", sent_at=now, error=None, due_at=due_at or None)
    conn.commit()
    row = conn.execute("SELECT COUNT(*) AS n FROM touches WHERE contact_id = ? AND channel = ? "
                       "AND status = 'sent'", (contact_id, channel)).fetchone()
    return (row["n"] if row else 1) or 1


def mark_failed(contact_id: str, channel: str, error: str,
                conn: sqlite3.Connection | None = None) -> None:
    """Release the claim so the touch can be retried."""
    if conn is None:
        conn = get_connection()
    conn.execute(
        "UPDATE touches SET status = 'failed', error = ?, updated_at = ? "
        "WHERE contact_id = ? AND channel = ? AND status = 'sending'",
        (error[:300], _now(), contact_id, channel),
    )
    conn.commit()


def set_sequence_status(contact_id: str, channel: str, status: str, note: str = "",
                        conn: sqlite3.Connection | None = None) -> None:
    """Stop or reopen a ladder. 'stopped' and 'replied' both halt further touches."""
    if conn is None:
        conn = get_connection()
    conn.execute(
        "INSERT INTO sequences (contact_id, channel, status, note, updated_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(contact_id, channel) DO UPDATE SET "
        "status = excluded.status, note = excluded.note, updated_at = excluded.updated_at",
        (contact_id, channel, status or "", note or "", _now()),
    )
    conn.commit()


def delete_for_contact(contact_id: str, conn: sqlite3.Connection | None = None) -> None:
    """Cascade — contacts are deletable from the dashboard's row menu."""
    if conn is None:
        conn = get_connection()
    conn.execute("DELETE FROM touches WHERE contact_id = ?", (contact_id,))
    conn.execute("DELETE FROM sequences WHERE contact_id = ?", (contact_id,))
    conn.commit()

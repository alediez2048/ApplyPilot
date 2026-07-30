"""The `messages` table — conversation memory.

HEADERS ONLY. No bodies, and no snippets: the schema is the guarantee, not a policy note in a
docstring. Adding threads to this database already changes what a leak of
`~/.applypilot/applypilot.db` costs — today it holds names and drafts, now it holds who spoke
to whom and when. Bodies would make it correspondence.

Rows are keyed by Gmail's own `message_id`, so syncing the same thread repeatedly is a no-op.
That matters because `tick` may run hourly forever.
"""

from __future__ import annotations

import json
import sqlite3

from applypilot.database import get_connection, schema_ready

_MESSAGE_COLUMNS: dict[str, str] = {
    "message_id": "TEXT PRIMARY KEY",   # Gmail's id — the natural dedupe key
    "thread_id": "TEXT NOT NULL",
    "contact_id": "TEXT",
    "job_url": "TEXT",
    "direction": "TEXT",                # in | out
    "from_addr": "TEXT",
    "from_name": "TEXT",
    "to_addrs": "TEXT",                 # JSON list
    "cc_addrs": "TEXT",                 # JSON list
    "subject": "TEXT",
    "sent_at": "TEXT",                  # ISO 8601
    "synced_at": "TEXT",
}


def init_messages(conn: sqlite3.Connection | None = None) -> sqlite3.Connection:
    if conn is None:
        conn = get_connection()
    if schema_ready(conn, "messages"):
        return conn
    cols = ", ".join(f"{n} {t}" for n, t in _MESSAGE_COLUMNS.items())
    conn.execute(f"CREATE TABLE IF NOT EXISTS messages ({cols})")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_contact ON messages(contact_id)")
    conn.commit()
    existing = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    for col, dtype in _MESSAGE_COLUMNS.items():
        if col not in existing and "PRIMARY KEY" not in dtype:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {dtype}")
    conn.commit()
    return conn


def upsert_messages(rows: list[dict], conn: sqlite3.Connection | None = None) -> int:
    """Store thread messages. Returns how many were NEW.

    `INSERT OR REPLACE` on Gmail's message id: re-syncing a thread must not duplicate it, and
    an hourly `tick` re-syncs every open thread by design.
    """
    if not rows:
        return 0
    if conn is None:
        conn = get_connection()
    init_messages(conn)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    known = {r[0] for r in conn.execute("SELECT message_id FROM messages").fetchall()}
    new = 0
    for r in rows:
        mid = r.get("message_id") or r.get("id")
        if not mid:
            continue
        if mid not in known:
            new += 1
        conn.execute(
            "INSERT OR REPLACE INTO messages (message_id, thread_id, contact_id, job_url, "
            "direction, from_addr, from_name, to_addrs, cc_addrs, subject, sent_at, synced_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, r.get("thread_id"), r.get("contact_id"), r.get("job_url"),
             r.get("direction"), r.get("from_addr"), r.get("from_name"),
             json.dumps(r.get("to_addrs") or []), json.dumps(r.get("cc_addrs") or []),
             r.get("subject"), r.get("sent_at"), now))
    conn.commit()
    return new


def thread_for_contact(contact_id: str, conn: sqlite3.Connection | None = None) -> list[dict]:
    """The stored conversation for one contact, oldest first."""
    if conn is None:
        conn = get_connection()
    init_messages(conn)
    rows = conn.execute(
        "SELECT * FROM messages WHERE contact_id = ? ORDER BY sent_at", (contact_id,)).fetchall()
    return [_row(r) for r in rows]


def threads_for_job(job_url: str, conn: sqlite3.Connection | None = None) -> dict:
    """contact_id -> messages, for one job. One query, not one per contact.

    `/api/status` renders every job on a 2.5s refresh and is held to a 50-statement budget, so a
    per-contact query here would blow it the moment a job has a few contacts.
    """
    if conn is None:
        conn = get_connection()
    init_messages(conn)
    rows = conn.execute(
        "SELECT * FROM messages WHERE job_url = ? ORDER BY sent_at", (job_url,)).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        d = _row(r)
        out.setdefault(d.get("contact_id") or "", []).append(d)
    return out


def delete_for_contact(contact_id: str, conn: sqlite3.Connection | None = None) -> int:
    """Drop a contact's stored conversation — called when the contact is deleted.

    Same reasoning as `touches`/`sequences`: contact ids are a hash of (job, identity), so a
    rediscovered person reproduces the id exactly and would inherit a stranger's thread.
    """
    if conn is None:
        conn = get_connection()
    init_messages(conn)
    cur = conn.execute("DELETE FROM messages WHERE contact_id = ?", (contact_id,))
    conn.commit()
    return cur.rowcount


def _row(r) -> dict:
    d = dict(zip(r.keys(), r))
    for key in ("to_addrs", "cc_addrs"):
        try:
            d[key] = json.loads(d.get(key) or "[]")
        except (ValueError, TypeError):
            d[key] = []
    return d

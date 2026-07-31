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
    # The RFC Message-ID header — still a header, still not content. Without it a reply can
    # only chain off our own FIRST email, so a mail client shows the answer as a new
    # conversation next to the one it answers.
    "rfc_message_id": "TEXT",
    # CRM-4b, and the ONLY content column that will ever exist here. A Gmail snippet, hard-
    # truncated to SNIPPET_MAX at the STORE layer rather than at the caller — a cap enforced
    # where the write happens cannot be bypassed by a new caller that forgets it. Populated
    # only when the token carries `gmail.readonly`; empty on every metadata-only install.
    "snippet": "TEXT",
}

#: Enough to draft a reply against, an order of magnitude less than a message body sitting in
#: a plaintext SQLite file. Adding threads already changed what a leak of applypilot.db costs;
#: full bodies would make it correspondence.
#:
#: This cap governs the AUTOMATIC path — text Gmail hands us because a scope was granted.
SNIPPET_MAX = 200

#: The cap when the OPERATOR pastes a reply in themselves. Larger on purpose, and the reasoning
#: is different rather than looser: nothing was harvested. They had the message open, selected
#: it and chose to hand it over, one contact at a time — the same act as typing into `notes`.
#: Still bounded, because "paste your inbox into the CRM" is not a feature either.
PASTED_MAX = 2000


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

    # Existing snippets come along for the ride because this is INSERT OR **REPLACE**: a
    # re-sync that carries no snippet would otherwise blank one already stored. That is
    # exactly what happens the moment `gmail.readonly` is revoked — every poll would quietly
    # erase the content it had, instead of simply not adding more (the ticket's "degrade
    # cleanly", which only means anything if nothing is destroyed on the way down).
    existing = {r[0]: (r[1] or "")
                for r in conn.execute("SELECT message_id, snippet FROM messages").fetchall()}
    known = set(existing)
    new = 0
    for r in rows:
        mid = r.get("message_id") or r.get("id")
        if not mid:
            continue
        if mid not in known:
            new += 1
        # Truncated HERE, at the write, not at the caller. A cap that lives in the caller is
        # one a future caller forgets; this one cannot be bypassed by any path into the table.
        snippet = ((r.get("snippet") or "").strip() or existing.get(mid, ""))[:SNIPPET_MAX]
        conn.execute(
            "INSERT OR REPLACE INTO messages (message_id, thread_id, contact_id, job_url, "
            "direction, from_addr, from_name, to_addrs, cc_addrs, subject, sent_at, synced_at, "
            "rfc_message_id, snippet) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, r.get("thread_id"), r.get("contact_id"), r.get("job_url"),
             r.get("direction"), r.get("from_addr"), r.get("from_name"),
             json.dumps(r.get("to_addrs") or []), json.dumps(r.get("cc_addrs") or []),
             r.get("subject"), r.get("sent_at"), now, r.get("rfc_message_id"), snippet))
    conn.commit()
    return new


def record_outbound(contact: dict, sent: dict, to_addr: str, cc: list[str], subject: str,
                    conn: sqlite3.Connection | None = None) -> None:
    """Store a message WE just sent, immediately.

    The alternative is waiting for the next Gmail poll, which means the operator clicks Send,
    the thread does not change, and the only honest reading of the screen is that nothing
    happened. Keyed by Gmail's message id like every other row, so the poll that eventually
    covers this message overwrites it rather than duplicating it.
    """
    from datetime import datetime, timezone
    mid = (sent or {}).get("id")
    if not mid:
        return  # nothing to key on; the next poll will pick it up
    upsert_messages([{
        "message_id": mid,
        "thread_id": sent.get("thread_id") or contact.get("thread_id"),
        "contact_id": contact.get("id"),
        "job_url": contact.get("job_url"),
        "direction": "out",
        "from_addr": sent.get("from_addr") or "",
        "from_name": "",
        "to_addrs": [to_addr],
        "cc_addrs": list(cc or []),
        "subject": subject,
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "rfc_message_id": sent.get("rfc_message_id") or "",
    }], conn)


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


def set_reply_text(contact_id: str, text: str, conn: sqlite3.Connection | None = None) -> bool:
    """Record what they said, pasted by the operator, onto the newest INBOUND message.

    A deliberately separate entry point from `upsert_messages`, not a parameter on it. The two
    have different provenance — one is text Gmail handed us because a scope was granted, the
    other is text a human chose to paste — and collapsing them would make the auto-ingest cap
    depend on which caller happened to be running.

    Attaches to the last inbound message so the sequence stays a sequence: the reply text lands
    ON the reply, not in a field beside the conversation.
    """
    if conn is None:
        conn = get_connection()
    init_messages(conn)
    row = conn.execute(
        "SELECT message_id FROM messages WHERE contact_id = ? AND direction = 'in' "
        "ORDER BY sent_at DESC LIMIT 1", (contact_id,)).fetchone()
    if not row:
        return False
    conn.execute("UPDATE messages SET snippet = ? WHERE message_id = ?",
                 ((text or "").strip()[:PASTED_MAX], row[0]))
    conn.commit()
    return True


def threads_by_contact(conn: sqlite3.Connection | None = None) -> dict:
    """Every stored conversation, keyed by contact id. ONE query for the whole database.

    `tick` walks all contacts looking for unanswered replies; doing that with a query per
    contact is the N+1 this codebase keeps re-learning. Hourly rather than every 2.5s is not
    a reason to write it the other way.
    """
    if conn is None:
        conn = get_connection()
    init_messages(conn)
    out: dict[str, list[dict]] = {}
    for r in conn.execute("SELECT * FROM messages ORDER BY sent_at").fetchall():
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

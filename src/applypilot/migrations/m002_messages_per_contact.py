"""002 — `messages` keyed by (message_id, contact_id), not message_id alone.

A single Gmail message legitimately belongs to more than one contact. The Writer thread is the
proof: our outreach to Victoria, her reply, and our answer all sit in one thread that BOTH
Victoria and David Loveless are on. Under a `message_id` primary key only one of them could
hold it.

That was harmless while every thread was one ApplyPilot sent to one person. "Pull all Gmail"
broke it immediately: it searches by ADDRESS, so David's sync found the same thread and
`INSERT OR REPLACE` reassigned all three rows to him — **emptying Victoria's conversation**.
Measured, not theorised: Victoria 3 → 0, David 0 → 3, on one click.

Rebuilding with a composite key means each contact keeps their own view of a shared thread, and
re-syncing either one changes nothing about the other.

Idempotent: it checks the existing primary key first and does nothing when already correct —
this app gets killed mid-operation, so a migration that assumes it has not run is a migration
that eventually corrupts something.
"""

from __future__ import annotations

import sqlite3

NOTE = "messages keyed per (message, contact) so a shared thread belongs to everyone on it"

#: Kept in step with `networking/messages._MESSAGE_COLUMNS`. Duplicated on purpose: a migration
#: must describe the schema AS OF ITS OWN VERSION, or re-running it years later rebuilds the
#: table with today's columns and silently changes what version 2 meant.
_COLUMNS = (
    ("message_id", "TEXT NOT NULL"),
    ("thread_id", "TEXT NOT NULL"),
    ("contact_id", "TEXT NOT NULL DEFAULT ''"),
    ("job_url", "TEXT"),
    ("direction", "TEXT"),
    ("from_addr", "TEXT"),
    ("from_name", "TEXT"),
    ("to_addrs", "TEXT"),
    ("cc_addrs", "TEXT"),
    ("subject", "TEXT"),
    ("sent_at", "TEXT"),
    ("synced_at", "TEXT"),
    ("rfc_message_id", "TEXT"),
    ("snippet", "TEXT"),
)


def _needs_rebuild(conn: sqlite3.Connection) -> bool:
    """True when `messages` exists and is still keyed on message_id alone."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'").fetchone()
    if not row:
        return False                      # fresh database: messages.py creates it correctly
    pk = [r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall() if r[5]]
    return pk == ["message_id"]


def up(conn: sqlite3.Connection) -> dict:
    if not _needs_rebuild(conn):
        return {"rebuilt": False, "rows": 0, "note": "already keyed per contact"}

    have = [r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
    carried = [c for c, _ in _COLUMNS if c in have]
    cols_sql = ", ".join(f"{n} {t}" for n, t in _COLUMNS)

    conn.execute("DROP TABLE IF EXISTS messages_v2")
    conn.execute(f"CREATE TABLE messages_v2 ({cols_sql}, "
                 f"PRIMARY KEY (message_id, contact_id))")
    # COALESCE on contact_id because the old table allowed NULL there and the new key cannot.
    # A NULL contact_id was an orphan row anyway — it belonged to nobody and rendered nowhere.
    select = ", ".join(f"COALESCE({c}, '')" if c == "contact_id" else c for c in carried)
    conn.execute(f"INSERT OR IGNORE INTO messages_v2 ({', '.join(carried)}) "
                 f"SELECT {select} FROM messages")
    moved = conn.execute("SELECT COUNT(*) FROM messages_v2").fetchone()[0]

    conn.execute("DROP TABLE messages")
    conn.execute("ALTER TABLE messages_v2 RENAME TO messages")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_contact ON messages(contact_id)")
    conn.commit()
    return {"rebuilt": True, "rows": moved, "note": NOTE}

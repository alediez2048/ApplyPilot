"""The `interactions` table — events with nowhere else to live.

Deliberately narrow. Sends, replies, LinkedIn invites and deck clicks are all already recorded
on `contacts`, and copying them here would create a second copy that drifts from the first —
which is exactly what made `tick` report 0 follow-ups due while the dashboard showed 3
(§Lessons 21). `domain/interactions.py` derives those at render time.

What lands here is what has no column of its own:

  * `booked` — detected from a cal.com confirmation in the mailbox
  * `profile_view` — LOGGED BY THE OPERATOR. LinkedIn profile views are not in the data export
    and generate no notification email; the only source is LinkedIn's UI, which this project
    abandoned automating twice (§Lessons 3). `source='manual'` keeps that distinction visible
    rather than letting an operator note masquerade as a detection.
  * `note` — anything else worth remembering about an interaction.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone

from applypilot.database import get_connection, schema_ready

_COLUMNS: dict[str, str] = {
    # Derived from (contact, kind, at) so re-detecting the same booking is an upsert, not a
    # duplicate. An hourly tick re-reads the same mailbox forever.
    "id": "TEXT PRIMARY KEY",
    "contact_id": "TEXT NOT NULL",
    "job_url": "TEXT",
    "kind": "TEXT NOT NULL",
    "at": "TEXT",                  # when the interaction happened
    "detail": "TEXT",
    "source": "TEXT",              # detected | manual — never collapse these
    "created_at": "TEXT",
}


def init_interactions(conn: sqlite3.Connection | None = None) -> sqlite3.Connection:
    if conn is None:
        conn = get_connection()
    if schema_ready(conn, "interactions"):
        return conn
    cols = ", ".join(f"{n} {t}" for n, t in _COLUMNS.items())
    conn.execute(f"CREATE TABLE IF NOT EXISTS interactions ({cols})")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_interactions_contact "
                 "ON interactions(contact_id)")
    conn.commit()
    existing = {r[1] for r in conn.execute("PRAGMA table_info(interactions)").fetchall()}
    for col, dtype in _COLUMNS.items():
        if col not in existing and "PRIMARY KEY" not in dtype:
            conn.execute(f"ALTER TABLE interactions ADD COLUMN {col} {dtype}")
    conn.commit()
    return conn


def _make_id(contact_id: str, kind: str, at: str) -> str:
    return hashlib.sha256(f"{contact_id}|{kind}|{at}".encode()).hexdigest()[:16]


def record(contact_id: str, kind: str, at: str = "", detail: str = "",
           source: str = "detected", job_url: str = "",
           conn: sqlite3.Connection | None = None) -> bool:
    """Store one interaction. Returns True if it is NEW.

    Idempotent by construction: the id is a hash of (contact, kind, when), so re-detecting the
    same cal.com booking on the next tick updates the row instead of adding another. The eleven
    duplicate BOUNCED log lines (§Lessons 22) are what this shape is avoiding.
    """
    if conn is None:
        conn = get_connection()
    init_interactions(conn)
    at = (at or datetime.now(timezone.utc).isoformat()).strip()
    iid = _make_id(contact_id, kind, at)
    seen = conn.execute("SELECT 1 FROM interactions WHERE id = ?", (iid,)).fetchone()
    conn.execute(
        "INSERT OR REPLACE INTO interactions (id, contact_id, job_url, kind, at, detail, "
        "source, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (iid, contact_id, job_url, kind, at, detail, source,
         datetime.now(timezone.utc).isoformat()))
    conn.commit()
    return seen is None


def for_job(job_url: str, conn: sqlite3.Connection | None = None) -> dict:
    """contact_id -> rows, for one job. ONE query, not one per contact.

    `/api/status` renders every job on a 2.5s refresh under a 50-statement budget, so a
    per-contact query here would blow it as soon as a job had a few people.
    """
    if conn is None:
        conn = get_connection()
    init_interactions(conn)
    out: dict[str, list[dict]] = {}
    for r in conn.execute(
            "SELECT contact_id, kind, at, detail, source FROM interactions "
            "WHERE job_url = ? ORDER BY at DESC", (job_url,)).fetchall():
        out.setdefault(r[0], []).append(
            {"kind": r[1], "at": r[2], "detail": r[3], "source": r[4]})
    return out


def delete_for_contact(contact_id: str, conn: sqlite3.Connection | None = None) -> int:
    """Same reasoning as `touches` and `messages`: contact ids are a hash of (job, identity),
    so a re-discovered person reproduces the id and would inherit a stranger's history."""
    if conn is None:
        conn = get_connection()
    init_interactions(conn)
    cur = conn.execute("DELETE FROM interactions WHERE contact_id = ?", (contact_id,))
    conn.commit()
    return cur.rowcount

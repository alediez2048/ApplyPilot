"""Contacts table — owned by the networking subsystem (independent of the jobs table).

Mirrors the forward-migration pattern in database.py but for its own `contacts` table.
`init_contacts()` is idempotent and must be called from every read path (CLI, dashboard,
service) so a fresh DB never raises "no such table: contacts".
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from hashlib import sha1

from applypilot.database import get_connection

_DELIM = "\x1f"  # unit separator — avoids hash collisions across concatenated fields

# Single source of truth for the contacts schema. Adding a key here auto-migrates.
_CONTACT_COLUMNS: dict[str, str] = {
    "id": "TEXT PRIMARY KEY",
    "job_url": "TEXT NOT NULL",
    "full_name": "TEXT",
    "title": "TEXT",
    "company": "TEXT",
    "linkedin_url": "TEXT",
    "email": "TEXT",
    "email_status": "TEXT",       # verified | unverified | none
    "location": "TEXT",
    "seniority": "TEXT",
    "match_reason": "TEXT",
    "source": "TEXT",             # apollo | linkedin
    "apollo_id": "TEXT",
    "outreach_subject": "TEXT",
    "outreach_message": "TEXT",
    "outreach_status": "TEXT DEFAULT 'none'",  # none|drafted|sending|submitted|failed
    "outreach_channel": "TEXT",
    "submitted_at": "TEXT",
    "sent_message_id": "TEXT",
    "send_error": "TEXT",
    "discovered_at": "TEXT",
    "updated_at": "TEXT",
}


def contact_id(job_url: str, linkedin_url: str | None, name: str | None) -> str:
    """Stable id from delimited parts (avoids collisions from naive concatenation)."""
    key = _DELIM.join([job_url or "", (linkedin_url or "").lower(), (name or "").lower()])
    return sha1(key.encode("utf-8")).hexdigest()[:16]


def init_contacts(conn: sqlite3.Connection | None = None) -> sqlite3.Connection:
    """Create the contacts table + indexes if absent, then forward-migrate columns."""
    if conn is None:
        conn = get_connection()
    cols = ", ".join(f"{name} {dtype}" for name, dtype in _CONTACT_COLUMNS.items())
    conn.execute(f"CREATE TABLE IF NOT EXISTS contacts ({cols})")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_contacts_job ON contacts(job_url)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(outreach_status, submitted_at)"
    )
    conn.commit()
    ensure_contacts_columns(conn)
    return conn


def ensure_contacts_columns(conn: sqlite3.Connection | None = None) -> list[str]:
    """Add any missing columns to the contacts table (forward-only)."""
    if conn is None:
        conn = get_connection()
    existing = {row[1] for row in conn.execute("PRAGMA table_info(contacts)").fetchall()}
    added = []
    for col, dtype in _CONTACT_COLUMNS.items():
        if col not in existing:
            if "PRIMARY KEY" in dtype:
                continue
            conn.execute(f"ALTER TABLE contacts ADD COLUMN {col} {dtype}")
            added.append(col)
    if added:
        conn.commit()
    return added


def upsert_contact(contact: dict, conn: sqlite3.Connection | None = None) -> str:
    """Insert or update a contact. Identity (id) never switches once stored.

    `contact` must include job_url; id is derived if absent. Returns the id.
    """
    if conn is None:
        conn = get_connection()
    init_contacts(conn)

    cid = contact.get("id") or contact_id(
        contact["job_url"], contact.get("linkedin_url"), contact.get("full_name")
    )
    now = datetime.now(timezone.utc).isoformat()

    row = {k: contact.get(k) for k in _CONTACT_COLUMNS if k not in ("id",)}
    row["updated_at"] = now

    existing = conn.execute("SELECT id FROM contacts WHERE id = ?", (cid,)).fetchone()
    if existing:
        # Update only provided (non-None) fields; preserve send/draft state otherwise.
        sets = {k: v for k, v in row.items() if v is not None}
        if sets:
            assignments = ", ".join(f"{k} = ?" for k in sets)
            conn.execute(
                f"UPDATE contacts SET {assignments} WHERE id = ?",
                (*sets.values(), cid),
            )
    else:
        row["discovered_at"] = now
        row.setdefault("outreach_status", "none")
        cols = ["id"] + list(row.keys())
        placeholders = ", ".join("?" for _ in cols)
        conn.execute(
            f"INSERT INTO contacts ({', '.join(cols)}) VALUES ({placeholders})",
            (cid, *row.values()),
        )
    conn.commit()
    return cid


def get_contact(contact_id: str, conn: sqlite3.Connection | None = None) -> dict | None:
    if conn is None:
        conn = get_connection()
    init_contacts(conn)
    row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    return dict(zip(row.keys(), row)) if row else None


def _norm_email(email: str | None) -> str:
    return (email or "").strip().lower()


def sent_today(conn: sqlite3.Connection | None = None) -> int:
    """Count emails submitted in the last 24h (for the daily cap)."""
    if conn is None:
        conn = get_connection()
    init_contacts(conn)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) FROM contacts WHERE outreach_status = 'submitted' AND submitted_at >= ?",
        (cutoff,),
    ).fetchone()
    return row[0] if row else 0


def already_contacted_email(
    email: str, cooldown_days: int = 30, exclude_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> str | None:
    """Cross-job dedupe: return the submitted_at if this email was emailed recently."""
    norm = _norm_email(email)
    if not norm:
        return None
    if conn is None:
        conn = get_connection()
    init_contacts(conn)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=cooldown_days)).isoformat()
    row = conn.execute(
        "SELECT submitted_at FROM contacts WHERE lower(trim(email)) = ? "
        "AND outreach_status = 'submitted' AND submitted_at >= ? AND id != ? "
        "ORDER BY submitted_at DESC LIMIT 1",
        (norm, cutoff, exclude_id or ""),
    ).fetchone()
    return row[0] if row else None


def claim_for_send(contact_id: str, conn: sqlite3.Connection | None = None) -> bool:
    """Atomically claim a contact for sending. Returns True only for the winning caller.

    Mirrors apply/launcher.py::acquire_job — the UPDATE succeeds for exactly one racer
    (submitted_at IS NULL guard), preventing double-send under the threading server.
    """
    if conn is None:
        conn = get_connection()
    init_contacts(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("BEGIN IMMEDIATE")
    cur = conn.execute(
        "UPDATE contacts SET outreach_status = 'sending', submitted_at = ? "
        "WHERE id = ? AND submitted_at IS NULL",
        (now, contact_id),
    )
    conn.commit()
    return cur.rowcount == 1


def mark_sent(contact_id: str, message_id: str, conn: sqlite3.Connection | None = None) -> None:
    if conn is None:
        conn = get_connection()
    conn.execute(
        "UPDATE contacts SET outreach_status = 'submitted', sent_message_id = ?, "
        "send_error = NULL, updated_at = ? WHERE id = ?",
        (message_id, datetime.now(timezone.utc).isoformat(), contact_id),
    )
    conn.commit()


def mark_send_failed(contact_id: str, error: str, conn: sqlite3.Connection | None = None) -> None:
    """Roll a claimed send back to 'failed' and clear submitted_at so it can be retried."""
    if conn is None:
        conn = get_connection()
    conn.execute(
        "UPDATE contacts SET outreach_status = 'failed', submitted_at = NULL, "
        "send_error = ?, updated_at = ? WHERE id = ?",
        (error[:300], datetime.now(timezone.utc).isoformat(), contact_id),
    )
    conn.commit()


def get_contacts_for_job(job_url: str, conn: sqlite3.Connection | None = None) -> list[dict]:
    """Return contacts for a job as dicts (ordered by discovery)."""
    if conn is None:
        conn = get_connection()
    init_contacts(conn)
    rows = conn.execute(
        "SELECT * FROM contacts WHERE job_url = ? ORDER BY discovered_at ASC", (job_url,)
    ).fetchall()
    return [dict(zip(r.keys(), r)) for r in rows] if rows else []

"""Contacts table — owned by the networking subsystem (independent of the jobs table).

Mirrors the forward-migration pattern in database.py but for its own `contacts` table.
`init_contacts()` is idempotent and must be called from every read path (CLI, dashboard,
service) so a fresh DB never raises "no such table: contacts".
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from hashlib import sha1

from applypilot.database import get_connection, schema_ready

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
    "linkedin_message": "TEXT",   # short LinkedIn connection note (<= 300 chars)
    "outreach_status": "TEXT DEFAULT 'none'",  # none|drafted|sending|submitted|failed
    "outreach_channel": "TEXT",
    "submitted_at": "TEXT",
    "sent_message_id": "TEXT",
    "send_error": "TEXT",
    # LinkedIn DM channel (independent of the email send state above).
    "dm_status": "TEXT",          # none|sending|composed|sent|manual|skipped|failed
    "dm_sent_at": "TEXT",
    "dm_error": "TEXT",
    # Operator-entered, NOT from a provider. Apollo only releases a direct dial to a public
    # webhook, so the number is copied by hand out of the Apollo UI (see the "Apollo ↗" button).
    "phone": "TEXT",
    "notes": "TEXT",
    # iMessage/SMS channel. ONE timestamp, not a status: the ladder only needs to know a first
    # text went out, and everything after it is a row in `touches` like every other channel.
    #
    # This exists because `phone` cannot prove it. The number is typed in by hand for anyone the
    # operator MIGHT text, so keying the ladder on it would mark a follow-up due for people
    # nobody has ever messaged. Email proves itself with sent_message_id and LinkedIn with
    # dm_status; this is that same fact for texting, and the operator sets it by clicking
    # "✓ I sent it" — we cannot observe an iMessage leaving the Messages app.
    "sms_sent_at": "TEXT",
    # NOTE: the ten follow-up columns that used to live here (followup_* and li_followup_*)
    # moved to the `touches` / `sequences` tables in ARCH-3. Do NOT add them back — a
    # channel is a value in those tables, not a column-name prefix here. See touches.py.
    # Threading. Captured at SEND time from Gmail's own response + the RFC header we
    # generate, so a follow-up lands inside the original conversation instead of as a
    # second cold email. Needs no extra OAuth scope — both are already in hand at send.
    "thread_id": "TEXT",           # Gmail threadId
    "rfc_message_id": "TEXT",      # the RFC 5322 Message-ID header (for In-Reply-To)
    # Self-check result (networking/verify.py): does the evidence agree this person
    # actually works at the employer? high|medium|low, plus the reasoning shown in the UI.
    "confidence": "TEXT",
    "verify_note": "TEXT",
    # CRM-1. Set when an inbound message is matched to this contact. The email ladder halts via
    # `sequences` (ARCH-3) — this column is the DATE, for the UI and for time_to_reply (CRM-2).
    "replied_at": "TEXT",
    # Intro-deck engagement. A CLICK, not an open: nobody's spam filter follows a link and reads
    # a deck, so unlike an open-tracking pixel this means a person chose to look. No token column
    # — the token is DERIVED from the contact id (`domain/deck.py`), so it survives a restore and
    # cannot drift from the links already sitting in somebody's inbox.
    # The person's URL segment: /intro/gina. Assigned ONCE, when the first link is made, and
    # never changed — links are already sitting in inboxes and a reassigned slug would credit
    # the wrong person. Stored rather than derived because it cannot be derived: two contacts
    # are often called Gina, so uniqueness needs to see the others.
    "deck_slug": "TEXT",
    "deck_viewed_at": "TEXT",     # first click
    "deck_last_at": "TEXT",       # most recent click
    "deck_views": "INTEGER",      # how many times
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
    if schema_ready(conn, "contacts"):
        return conn
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


def log_contact_event(contact_id: str, status: str, detail: str,
                      conn: sqlite3.Connection | None = None) -> None:
    """Append an `outreach`-stage row to the job's activity log for a contact action.

    Lives here rather than in the dashboard so EVERY path is recorded — dashboard button,
    CLI, and the Chrome extension all funnel through these store helpers. Best-effort:
    log_event already swallows its own errors, and a missing contact is simply skipped.
    """
    try:
        c = conn or get_connection()
        row = c.execute("SELECT job_url FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        if not row or not row["job_url"]:
            return
        from applypilot.database import log_event
        log_event(row["job_url"], "outreach", status, detail, c)
    except Exception:  # noqa: BLE001 — activity logging must never break a send
        pass


def _contact_label(contact_id: str, conn: sqlite3.Connection | None = None) -> str:
    """'Jane Doe' for log lines; falls back to the id so a line is never nameless."""
    try:
        c = conn or get_connection()
        row = c.execute("SELECT full_name FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        return (row["full_name"] if row and row["full_name"] else contact_id) or contact_id
    except Exception:  # noqa: BLE001
        return contact_id


def ensure_deck_slug(contact_id: str, full_name: str = "",
                     conn: sqlite3.Connection | None = None) -> str:
    """The contact's deck slug, assigning one on first use. Stable forever after.

    Unique across the install, because the slug IS the identifier — two people called Gina
    sharing /intro/gina would make every click ambiguous, and the older link is already out
    there. `disambiguate` prefers a last initial ("gina-j") over a number, since a number is
    where a friendly URL starts looking like a token again.
    """
    from applypilot.domain import deck as _deck

    if conn is None:
        conn = get_connection()
    init_contacts(conn)
    row = conn.execute("SELECT deck_slug, full_name FROM contacts WHERE id = ?",
                       (contact_id,)).fetchone()
    if row is None:
        return ""
    if (row[0] or "").strip():
        return row[0].strip()

    name = full_name or row[1] or ""
    base = _deck.slugify(name)
    if not base:
        return ""
    taken = {r[0] for r in conn.execute(
        "SELECT deck_slug FROM contacts WHERE deck_slug IS NOT NULL AND deck_slug != ''"
    ).fetchall()}
    slug = _deck.disambiguate(base, taken, name)
    conn.execute("UPDATE contacts SET deck_slug = ? WHERE id = ?", (slug, contact_id))
    conn.commit()
    return slug


def mark_deck_viewed(contact_id: str, at: str = "",
                     conn: sqlite3.Connection | None = None) -> bool:
    """Record an intro-deck click. Returns True if this is the FIRST one for this contact.

    `deck_viewed_at` keeps the first click (COALESCE), `deck_last_at` moves. The distinction
    earns its keep: the first click is the event worth acting on, and re-importing the same
    analytics export must not keep re-announcing it — the same idempotence lesson as the eleven
    duplicate BOUNCED log lines (§Lessons 22).
    """
    if conn is None:
        conn = get_connection()
    init_contacts(conn)
    when = at or datetime.now(timezone.utc).isoformat()
    row = conn.execute("SELECT deck_viewed_at FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    if row is None:
        return False
    first = not (row[0] or "").strip()
    conn.execute(
        "UPDATE contacts SET deck_viewed_at = COALESCE(NULLIF(deck_viewed_at,''), ?), "
        "deck_last_at = ?, deck_views = COALESCE(deck_views, 0) + 1, updated_at = ? WHERE id = ?",
        (when, when, datetime.now(timezone.utc).isoformat(), contact_id),
    )
    conn.commit()
    return first


def mark_sent(contact_id: str, message_id: str, conn: sqlite3.Connection | None = None,
              thread_id: str = "", rfc_message_id: str = "") -> None:
    """Record a successful send. thread_id / rfc_message_id are what let a later
    follow-up thread into this same conversation (COALESCE keeps the FIRST message's
    ids, which are the ones a reply must reference)."""
    if conn is None:
        conn = get_connection()
    conn.execute(
        "UPDATE contacts SET outreach_status = 'submitted', sent_message_id = ?, "
        "thread_id = COALESCE(NULLIF(thread_id,''), ?), "
        "rfc_message_id = COALESCE(NULLIF(rfc_message_id,''), ?), "
        "send_error = NULL, updated_at = ? WHERE id = ?",
        (message_id, thread_id or "", rfc_message_id or "",
         datetime.now(timezone.utc).isoformat(), contact_id),
    )
    conn.commit()
    row = conn.execute("SELECT full_name, email, title FROM contacts WHERE id = ?",
                       (contact_id,)).fetchone()
    who = (row["full_name"] if row else None) or contact_id
    role = f" ({row['title']})" if row and row["title"] else ""
    addr = f" <{row['email']}>" if row and row["email"] else ""
    log_contact_event(contact_id, "ok", f"Emailed {who}{role}{addr}.", conn)


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
    log_contact_event(contact_id, "failed",
                      f"Email to {_contact_label(contact_id, conn)} failed: {error[:200]}", conn)


# ── LinkedIn DM channel ──────────────────────────────────────────────────────
# Mirrors the email send helpers above, but on the dm_* columns so the two
# channels never clobber each other's state.

def _norm_linkedin(url: str | None) -> str:
    """Normalize a LinkedIn profile URL for dedupe (lowercase, strip query/trailing slash)."""
    u = (url or "").strip().lower()
    if not u:
        return ""
    u = u.split("?")[0].split("#")[0]
    return u.rstrip("/")


def dm_sent_today(conn: sqlite3.Connection | None = None) -> int:
    """Count LinkedIn DMs sent in the last 24h (for the daily cap)."""
    if conn is None:
        conn = get_connection()
    init_contacts(conn)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) FROM contacts WHERE dm_status = 'sent' AND dm_sent_at >= ?",
        (cutoff,),
    ).fetchone()
    return row[0] if row else 0


def already_dmed(
    linkedin_url: str, cooldown_days: int = 30, exclude_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> str | None:
    """Cross-job dedupe: return the dm_sent_at if this profile was DM'd recently."""
    norm = _norm_linkedin(linkedin_url)
    if not norm:
        return None
    if conn is None:
        conn = get_connection()
    init_contacts(conn)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=cooldown_days)).isoformat()
    row = conn.execute(
        "SELECT dm_sent_at, linkedin_url FROM contacts "
        "WHERE dm_status = 'sent' AND dm_sent_at >= ? AND id != ?",
        (cutoff, exclude_id or ""),
    ).fetchall()
    for sent_at, url in row:
        if _norm_linkedin(url) == norm:
            return sent_at
    return None


def claim_dm_send(contact_id: str, conn: sqlite3.Connection | None = None) -> bool:
    """Atomically claim a contact for a LinkedIn DM. True only for the winning caller.

    Mirrors claim_for_send but on dm_sent_at — the UPDATE succeeds for exactly one
    racer (dm_sent_at IS NULL guard), preventing a double-DM under the threading server.
    """
    if conn is None:
        conn = get_connection()
    init_contacts(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("BEGIN IMMEDIATE")
    cur = conn.execute(
        "UPDATE contacts SET dm_status = 'sending', dm_sent_at = ? "
        "WHERE id = ? AND dm_sent_at IS NULL",
        (now, contact_id),
    )
    conn.commit()
    return cur.rowcount == 1


def mark_dm_sent(contact_id: str, conn: sqlite3.Connection | None = None) -> None:
    """Mark a contact's LinkedIn note as sent. Stamps dm_sent_at (COALESCE preserves an
    existing claim timestamp) so the automated `network --send-dm` dedupe/cap window
    queries — which filter `dm_sent_at >= cutoff` — actually see extension/manual sends.
    Without this stamp those sends are invisible and the same person can be DM'd twice."""
    if conn is None:
        conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE contacts SET dm_status = 'sent', dm_sent_at = COALESCE(dm_sent_at, ?), "
        "dm_error = NULL, updated_at = ? WHERE id = ?",
        (now, now, contact_id),
    )
    conn.commit()
    log_contact_event(contact_id, "ok",
                      f"Connected on LinkedIn — invite note sent to {_contact_label(contact_id, conn)}.", conn)


def mark_dm_composed(contact_id: str, conn: sqlite3.Connection | None = None) -> None:
    """Mark that the note was composed into the invite dialog (the human sends manually).
    Does NOT claim/dedupe or stamp dm_sent_at — composing is idempotent and re-runnable,
    and no invite has actually gone out yet."""
    if conn is None:
        conn = get_connection()
    conn.execute(
        "UPDATE contacts SET dm_status = 'composed', updated_at = ? WHERE id = ? "
        "AND (dm_status IS NULL OR dm_status NOT IN ('sent', 'manual', 'skipped'))",
        (datetime.now(timezone.utc).isoformat(), contact_id),
    )
    conn.commit()


def mark_dm_manual(contact_id: str, conn: sqlite3.Connection | None = None) -> None:
    """Mark a contact as handled via the manual/paste fallback — a real invite the human
    sent outside our compose flow. Stamps dm_sent_at so it counts toward dedupe/cap and
    won't re-surface in the queue."""
    if conn is None:
        conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE contacts SET dm_status = 'manual', dm_sent_at = COALESCE(dm_sent_at, ?), "
        "updated_at = ? WHERE id = ?",
        (now, now, contact_id),
    )
    conn.commit()
    log_contact_event(contact_id, "ok",
                      f"Connected on LinkedIn — sent {_contact_label(contact_id, conn)} an invite manually.", conn)


def mark_dm_skipped(contact_id: str, conn: sqlite3.Connection | None = None) -> None:
    """Mark a contact as deliberately skipped (already connected, InMail-only, user skip).
    No dm_sent_at (no invite sent), but excluded from the queue so it doesn't re-offer."""
    if conn is None:
        conn = get_connection()
    conn.execute(
        "UPDATE contacts SET dm_status = 'skipped', updated_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), contact_id),
    )
    conn.commit()
    log_contact_event(contact_id, "skipped",
                      f"Skipped LinkedIn outreach to {_contact_label(contact_id, conn)}.", conn)


def mark_dm_failed(contact_id: str, error: str, conn: sqlite3.Connection | None = None) -> None:
    """Roll a claimed DM back to 'failed' and clear dm_sent_at so it can be retried."""
    if conn is None:
        conn = get_connection()
    conn.execute(
        "UPDATE contacts SET dm_status = 'failed', dm_sent_at = NULL, "
        "dm_error = ?, updated_at = ? WHERE id = ?",
        (error[:300], datetime.now(timezone.utc).isoformat(), contact_id),
    )
    conn.commit()
    log_contact_event(
        contact_id, "failed",
        f"LinkedIn outreach to {_contact_label(contact_id, conn)} failed: {error[:200]}", conn)


# ── follow-up ladders: delegated to touches.py (ARCH-3) ─────────────────────
# These nine functions used to be email/LinkedIn pairs writing ten columns on `contacts`.
# They are now thin, channel-parameterised delegates. `mark_followed_up` stays here because
# it records a MANUAL touch from the checklist, not a ladder send.

def mark_followed_up(contact_id: str, channel: str = "email",
                     conn: sqlite3.Connection | None = None) -> bool:
    """Record a follow-up touch. Idempotent — the first one wins, so double-clicking
    the button (or a stale tab replaying it) can't record two."""
    from applypilot.networking import touches
    if conn is None:
        conn = get_connection()
    touches.init_touches(conn)
    if (touches.ladder_state(contact_id, channel, conn)["count"] or 0) > 0:
        return False
    touches.record_sent(contact_id, channel, conn=conn)
    log_contact_event(contact_id, "ok",
                      f"Followed up with {_contact_label(contact_id, conn)}.", conn)
    return True


def mark_sms_sent(contact_id: str, conn: sqlite3.Connection | None = None) -> bool:
    """Record that the FIRST text went out. Returns False if one was already recorded.

    Idempotent on purpose, and it is the operator who calls it: an iMessage is sent from the
    Messages app, so nothing here can observe it leaving. Clicking "✓ I sent it" is the only
    evidence that exists, which makes double-clicking the obvious failure — and a second stamp
    would silently move the ladder's anchor forward and push every touch later.

    Only the FIRST text lands here. Subsequent ones are `touches` rows like every other channel,
    which is what keeps the ladder engine from needing to know SMS exists.
    """
    if conn is None:
        conn = get_connection()
    init_contacts(conn)
    cur = conn.execute(
        "UPDATE contacts SET sms_sent_at = ? WHERE id = ? "
        "AND (sms_sent_at IS NULL OR sms_sent_at = '')",
        (datetime.now(timezone.utc).isoformat(), contact_id))
    conn.commit()
    if not cur.rowcount:
        return False
    log_contact_event(contact_id, "ok", f"Texted {_contact_label(contact_id, conn)}.", conn)
    return True


def claim_followup_send(contact_id: str, channel: str = "email",
                        conn: sqlite3.Connection | None = None) -> bool:
    """Atomically claim a follow-up send. Mirrors claim_for_send — under the threading
    server two clicks can race, and a duplicate follow-up is the worst bug here."""
    from applypilot.networking import touches
    if conn is None:
        conn = get_connection()
    touches.init_touches(conn)
    return touches.claim_send(contact_id, channel, conn)


def mark_followup_sent(contact_id: str, channel: str = "email",
                       conn: sqlite3.Connection | None = None) -> int:
    """Record a sent follow-up and advance the touch counter. Returns the new count."""
    from applypilot.networking import touches
    if conn is None:
        conn = get_connection()
    touches.init_touches(conn)
    n = touches.record_sent(contact_id, channel, conn=conn)
    label = "LinkedIn follow-up" if channel == "linkedin" else "Follow-up"
    log_contact_event(contact_id, "ok",
                      f"{label} #{n} sent to {_contact_label(contact_id, conn)}.", conn)
    return n


def mark_followup_failed(contact_id: str, error: str, channel: str = "email",
                         conn: sqlite3.Connection | None = None) -> None:
    """Release the claim so the follow-up can be retried."""
    from applypilot.networking import touches
    if conn is None:
        conn = get_connection()
    touches.init_touches(conn)
    touches.mark_failed(contact_id, channel, error, conn)
    log_contact_event(contact_id, "failed",
                      f"Follow-up to {_contact_label(contact_id, conn)} failed: {error[:200]}", conn)


def set_followup_draft(contact_id: str, subject: str, body: str, channel: str = "email",
                       conn: sqlite3.Connection | None = None) -> None:
    from applypilot.networking import touches
    if conn is None:
        conn = get_connection()
    touches.init_touches(conn)
    touches.set_draft(contact_id, channel, subject, body, conn)


def set_sequence_status(contact_id: str, status: str, channel: str = "email",
                        conn: sqlite3.Connection | None = None) -> None:
    """Stop or reopen a sequence. 'stopped' and 'replied' both halt further follow-ups."""
    from applypilot.networking import touches
    if conn is None:
        conn = get_connection()
    touches.init_touches(conn)
    touches.set_sequence_status(contact_id, channel, status, conn=conn)
    if status in ("stopped", "replied"):
        where = " on LinkedIn" if channel == "linkedin" else ""
        word = f"replied{where} — sequence stopped" if status == "replied" else "sequence stopped"
        log_contact_event(contact_id, "info", f"{_contact_label(contact_id, conn)}: {word}.", conn)


def mark_connected_now(contact_id: str, conn: sqlite3.Connection | None = None) -> None:
    """Record that an invite went out (or was accepted) so the LinkedIn clock can start.

    Needed because invites sent before ApplyPilot tracked them leave dm_sent_at empty,
    and without an anchor there is nothing to schedule a follow-up from.
    """
    if conn is None:
        conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE contacts SET dm_status = 'manual', dm_sent_at = COALESCE(NULLIF(dm_sent_at,''), ?), "
        "updated_at = ? WHERE id = ?",
        (now, now, contact_id),
    )
    conn.commit()


def delete_contact(contact_id: str, conn: sqlite3.Connection | None = None) -> bool:
    """Remove a contact AND its follow-up state. Returns True if a contact row was deleted.

    `touches`, `sequences`, `messages` and `interactions` are keyed by contact_id with no
    foreign key, so deleting only the
    contact leaves a live follow-up ladder pointing at somebody who no longer exists — due
    counts that can never be cleared, and a `sequences` row that would silently re-attach if
    the same contact id were ever minted again (ids are a hash of job + identity, so
    re-running discovery on the same person reproduces it exactly).
    """
    if conn is None:
        conn = get_connection()
    for table in ("touches", "sequences", "messages", "interactions"):
        try:
            conn.execute(f"DELETE FROM {table} WHERE contact_id = ?", (contact_id,))
        except sqlite3.OperationalError:
            pass  # table not created yet on a fresh DB
    cur = conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
    conn.commit()
    return cur.rowcount == 1


def get_contacts_for_job(job_url: str, conn: sqlite3.Connection | None = None) -> list[dict]:
    """Return contacts for a job as dicts (ordered by discovery)."""
    if conn is None:
        conn = get_connection()
    init_contacts(conn)
    rows = conn.execute(
        "SELECT * FROM contacts WHERE job_url = ? ORDER BY discovered_at ASC", (job_url,)
    ).fetchall()
    return [dict(zip(r.keys(), r)) for r in rows] if rows else []


# ── readers the dashboard used to run inline (ARCH-4) ───────────────────────
# `store.py` already was the repository for `contacts`; these are the four statements
# `web_dashboard.py` was still executing itself. Adding them here rather than creating a
# `repo/contacts.py` — two abstractions over one table is the failure mode the ticket
# explicitly warns about.

def contact_ref(contact_id: str, conn: sqlite3.Connection | None = None) -> dict | None:
    """Just identity: does this contact exist, and which job is it under?

    The dashboard's save/draft handlers need the job_url to build an upsert, and nothing
    else off the row — fetching `SELECT *` for two columns read a 32-column row per keystroke.
    """
    if conn is None:
        conn = get_connection()
    init_contacts(conn)
    row = conn.execute("SELECT id, job_url FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    return dict(zip(row.keys(), row)) if row else None


def all_contacts_for_metrics(conn: sqlite3.Connection | None = None) -> list[dict]:
    """Every contact, with only the columns metrics reads (CRM-2).

    A narrow projection on purpose: the panel runs on every /api/status, and `SELECT *` over a
    33-column table 50 rows deep every 2.5 seconds is exactly the kind of cost the query budget
    exists to catch.
    """
    if conn is None:
        conn = get_connection()
    init_contacts(conn)
    rows = conn.execute(
        "SELECT id, job_url, company, source, confidence, email_status, "
        "sent_message_id, submitted_at, replied_at FROM contacts"
    ).fetchall()
    return [dict(zip(r.keys(), r)) for r in rows]


def contacts_with_threads(conn: sqlite3.Connection | None = None) -> list[dict]:
    """Every contact whose conversation is worth re-reading (CRM-4).

    Wider than `contacts_awaiting_reply` on purpose, and the difference matters: that pool
    excludes anyone who already replied, which is exactly BACKWARDS for conversation memory —
    a replied contact is the one with a LIVE thread. Excluding them meant the Writer handoff
    (Victoria introducing a colleague) could never be seen, because her thread stopped being
    read the moment she answered.

    Bounced addresses are still excluded: that mail never arrived and never will.
    """
    if conn is None:
        conn = get_connection()
    init_contacts(conn)
    rows = conn.execute(
        "SELECT id, job_url, full_name, email, thread_id, rfc_message_id, submitted_at, "
        "replied_at FROM contacts WHERE thread_id IS NOT NULL AND thread_id != '' "
        "AND COALESCE(email_status, '') != 'bounced'"
    ).fetchall()
    return [dict(zip(r.keys(), r)) for r in rows]


def contacts_awaiting_reply(conn: sqlite3.Connection | None = None) -> list[dict]:
    """Contacts we emailed who have not yet been recorded as replying (CRM-1).

    Only these can receive a reply, and excluding the already-replied matters: re-marking one
    would overwrite `replied_at` with a LATER message in the same thread, losing when the
    conversation actually turned — which is exactly what time_to_reply (CRM-2) measures.
    """
    if conn is None:
        conn = get_connection()
    init_contacts(conn)
    # Excludes BOUNCED addresses too. A bounce is terminal — that mail will never arrive and
    # the person can never answer — so leaving them in meant every poll re-detected the same
    # failure and appended another "BOUNCED" line to the activity log. `applypilot tick` running
    # hourly turned that into 11 identical entries for one address in a single afternoon.
    rows = conn.execute(
        "SELECT id, job_url, full_name, email, thread_id, rfc_message_id, submitted_at "
        "FROM contacts WHERE sent_message_id IS NOT NULL "
        "AND (replied_at IS NULL OR replied_at = '') "
        "AND COALESCE(email_status, '') != 'bounced'"
    ).fetchall()
    return [dict(zip(r.keys(), r)) for r in rows]


def contact_for_delete(contact_id: str, conn: sqlite3.Connection | None = None) -> dict | None:
    """Exactly what removing a contact needs to log the removal honestly.

    Deliberately NOT `contact_ref`, which returns only id + job_url — it was narrowed on
    purpose so a per-keystroke save would stop fetching a 32-column row. Widening it for this
    would put that cost back on the hot path.
    """
    if conn is None:
        conn = get_connection()
    init_contacts(conn)
    row = conn.execute(
        "SELECT id, job_url, full_name, sent_message_id FROM contacts WHERE id = ?",
        (contact_id,)).fetchone()
    return dict(zip(row.keys(), row)) if row else None


def contact_name_and_phone(contact_id: str,
                           conn: sqlite3.Connection | None = None) -> dict | None:
    """Pre-save snapshot, so only a phone that ACTUALLY changed gets logged.

    Re-saving a note would otherwise spam the activity timeline with phone events.
    """
    if conn is None:
        conn = get_connection()
    init_contacts(conn)
    row = conn.execute("SELECT full_name, phone FROM contacts WHERE id = ?",
                       (contact_id,)).fetchone()
    return dict(zip(row.keys(), row)) if row else None


def unskip_dms(conn: sqlite3.Connection | None = None) -> int:
    """Put previously-skipped contacts back in the extension queue.

    `sent` / `manual` are genuinely done and deliberately untouched — only `skipped`
    (a decision you can change your mind about) is reset.
    """
    if conn is None:
        conn = get_connection()
    init_contacts(conn)
    n = conn.execute(
        "UPDATE contacts SET dm_status = 'none' "
        "WHERE dm_status = 'skipped' "
        "AND linkedin_url IS NOT NULL AND trim(linkedin_url) != '' "
        "AND linkedin_message IS NOT NULL AND trim(linkedin_message) != ''"
    ).rowcount
    conn.commit()
    return n


def dm_queue(exclude_statuses: tuple[str, ...],
             conn: sqlite3.Connection | None = None) -> list[dict]:
    """Everyone with a LinkedIn profile and a drafted note who hasn't been messaged.

    Ordered oldest-first so the queue is stable across polls; the caller dedupes by
    normalized profile URL, since one person can surface under two jobs.
    """
    if conn is None:
        conn = get_connection()
    init_contacts(conn)
    marks = ", ".join("?" for _ in exclude_statuses)
    rows = conn.execute(
        "SELECT * FROM contacts "
        "WHERE linkedin_url IS NOT NULL AND trim(linkedin_url) != '' "
        "AND linkedin_message IS NOT NULL AND trim(linkedin_message) != '' "
        f"AND (dm_status IS NULL OR dm_status NOT IN ({marks})) "
        "ORDER BY discovered_at ASC",
        tuple(exclude_statuses),
    ).fetchall()
    return [dict(zip(r.keys(), r)) for r in rows]

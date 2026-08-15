"""Replies promised for later — the conversation half of scheduled sending.

Follow-ups schedule themselves on `touches.scheduled_at`, because a follow-up draft already HAS
a row there: its ladder position, its text, its delivery status. A reply has none of that. It is
deliberately not a touch — `send_reply`'s own docstring says why: a follow-up is a ladder step
with a touch number, a schedule, a stop condition and a per-position prompt, and a reply is none
of those. It answers a person, so it is bounded by the conversation.

Which left a reply draft living **only in the browser** (`REPLY_DRAFT`, keyed by contact+thread)
and disappearing on reload. Nothing on the server has ever held one. So promising to send one
later needs storage, and this is it: one row per (contact, thread), holding the text, the Cc the
operator approved, and when it should go.

Two things make this table different from `touches.scheduled_at`, and both come from the fact
that a reply answers somebody:

  * **The Cc is stored.** `reply_target` recomputes recipients at send time from the thread, but
    the operator can DROP someone in the composer, and that decision has to survive until the
    send. Re-deriving it would silently put back a person they removed.

  * **A newer inbound message cancels the promise.** If they write again between the promise and
    the firing, the queued text was written against a conversation that has moved — see
    `stale_against` below. Nothing like this applies to a follow-up, which by definition chases
    silence.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from hashlib import sha1

from applypilot.database import get_connection, schema_ready

_COLUMNS: dict[str, str] = {
    "id": "TEXT PRIMARY KEY",
    "contact_id": "TEXT NOT NULL",
    # The thread KEY as `domain/conversations.thread_key` computes it — a Gmail thread id, or a
    # normalised subject when a message has none. Stored rather than derived because it is the
    # selector the send path needs, and it is what the browser posted.
    "thread_key": "TEXT NOT NULL",
    "subject": "TEXT",
    "body": "TEXT",
    "cc": "TEXT",                  # JSON list. The operator's decision, not a re-derivation.
    "scheduled_at": "TEXT",
    # The newest inbound message in that thread when the promise was made. If a newer one has
    # arrived by firing time the answer is stale — see `stale_against`.
    "last_inbound_at": "TEXT",
    "created_at": "TEXT",
    "updated_at": "TEXT",
}


def queue_id(contact_id: str, thread_key: str) -> str:
    """One promise per (contact, thread) — deliberately not per draft.

    Scheduling twice on the same conversation REPLACES, because there is one composer per thread
    and the operator is looking at one piece of text. Two queued answers to one conversation is
    not something the UI can express and not something a recipient should receive.
    """
    return sha1(f"{contact_id}\x1f{thread_key}".encode()).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_reply_queue(conn: sqlite3.Connection | None = None) -> sqlite3.Connection:
    """Idempotent; safe from any read path."""
    if conn is None:
        conn = get_connection()
    if schema_ready(conn, "reply_queue"):
        return conn
    cols = ", ".join(f"{n} {t}" for n, t in _COLUMNS.items())
    conn.execute(f"CREATE TABLE IF NOT EXISTS reply_queue ({cols})")
    conn.commit()
    _ensure_columns(conn)
    # After the column pass, never before — on an existing install `CREATE TABLE IF NOT EXISTS`
    # is a no-op, so indexing a newly declared column any earlier raises `no such column`.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reply_queue_due "
                 "ON reply_queue(scheduled_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reply_queue_contact "
                 "ON reply_queue(contact_id)")
    conn.commit()
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> list[str]:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(reply_queue)").fetchall()}
    added = []
    for col, dtype in _COLUMNS.items():
        if col not in existing and "PRIMARY KEY" not in dtype:
            conn.execute(f"ALTER TABLE reply_queue ADD COLUMN {col} {dtype}")
            added.append(col)
    if added:
        conn.commit()
    return added


def _row(r: sqlite3.Row) -> dict:
    d = dict(zip(r.keys(), r))
    try:
        d["cc"] = json.loads(d.get("cc") or "[]")
    except Exception:  # noqa: BLE001 — a corrupt blob must not take the dashboard down
        d["cc"] = []
    return d


# ── writes ──────────────────────────────────────────────────────────────────

def schedule(contact_id: str, thread_key: str, body: str, at_iso: str,
             subject: str = "", cc: list[str] | None = None,
             last_inbound_at: str = "", conn: sqlite3.Connection | None = None) -> bool:
    """Promise to send this reply at `at_iso`. False when there is nothing to promise."""
    if conn is None:
        conn = get_connection()
    init_reply_queue(conn)
    body = (body or "").strip()
    if not (contact_id and thread_key and body and at_iso):
        return False
    qid = queue_id(contact_id, thread_key)
    now = _now()
    conn.execute(
        "INSERT INTO reply_queue (id, contact_id, thread_key, subject, body, cc, scheduled_at, "
        "last_inbound_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET subject=excluded.subject, body=excluded.body, "
        "cc=excluded.cc, scheduled_at=excluded.scheduled_at, "
        "last_inbound_at=excluded.last_inbound_at, updated_at=excluded.updated_at",
        (qid, contact_id, thread_key, subject or "", body,
         json.dumps(list(cc or [])), at_iso, last_inbound_at or "", now, now))
    conn.commit()
    return True


def cancel(contact_id: str, thread_key: str,
           conn: sqlite3.Connection | None = None) -> bool:
    """Withdraw the promise. The row goes entirely — unlike a follow-up there is no draft
    underneath it to preserve, because this table IS the only copy of that text.

    Which is why the endpoint hands the body back to the browser on cancel: the operator asked
    to not send it yet, not to lose what they wrote.
    """
    if conn is None:
        conn = get_connection()
    init_reply_queue(conn)
    cur = conn.execute("DELETE FROM reply_queue WHERE id = ?",
                       (queue_id(contact_id, thread_key),))
    conn.commit()
    return cur.rowcount >= 1


def claim_due(now_iso: str, lapsed_before: str, limit: int = 50,
              conn: sqlite3.Connection | None = None) -> list[dict]:
    """Take every reply whose time has come, DELETING the row in the same call.

    Same two properties as `touches.claim_due_scheduled`, for the same reasons: claiming before
    the send means a crash loses the promise rather than replaying the email every five minutes,
    and firing consumes the promise whatever the outcome so a refusal cannot retry itself into
    the same refusal forever.

    A promise older than `lapsed_before` is left in place, unclaimed — it shows on the card as
    missed and waits for a human, because "the dashboard was closed" is never a reason to answer
    somebody days late.
    """
    if conn is None:
        conn = get_connection()
    init_reply_queue(conn)
    rows = conn.execute(
        "SELECT * FROM reply_queue WHERE scheduled_at <= ? AND scheduled_at > ? "
        "ORDER BY scheduled_at LIMIT ?", (now_iso, lapsed_before, limit)).fetchall()
    claimed = [_row(r) for r in rows]
    for row in claimed:
        conn.execute("DELETE FROM reply_queue WHERE id = ?", (row["id"],))
    conn.commit()
    return claimed


def delete_for_contact(contact_id: str, conn: sqlite3.Connection | None = None) -> None:
    """Cascade — contacts are deletable from the dashboard's row menu."""
    if conn is None:
        conn = get_connection()
    init_reply_queue(conn)
    conn.execute("DELETE FROM reply_queue WHERE contact_id = ?", (contact_id,))
    conn.commit()


# ── reads ───────────────────────────────────────────────────────────────────

def pending_for(contact_ids: list[str],
                conn: sqlite3.Connection | None = None) -> dict[str, dict[str, dict]]:
    """{contact_id: {thread_key: row}} — ONE statement for the whole page.

    The dashboard renders every contact every 2.5 seconds against a budget with a handful of
    statements to spare, so this is a bulk load like `ladder_states` and never a per-contact
    read (§Lessons 11).
    """
    if conn is None:
        conn = get_connection()
    if not contact_ids:
        return {}
    init_reply_queue(conn)
    marks = ",".join("?" for _ in contact_ids)
    out: dict[str, dict[str, dict]] = {}
    for r in conn.execute(f"SELECT * FROM reply_queue WHERE contact_id IN ({marks})",
                          contact_ids):
        row = _row(r)
        out.setdefault(row["contact_id"], {})[row["thread_key"]] = row
    return out


def pending_all(conn: sqlite3.Connection | None = None) -> dict[str, dict[str, dict]]:
    """Every outstanding promise, {contact_id: {thread_key: row}} — ONE statement, no WHERE.

    Hoisted above the job loop in `/api/status` rather than loaded per job. `pending_for` above
    takes ids and is the right shape for a single card; on the dashboard it would run once per
    JOB, which is N statements against a budget with a handful to spare. This table holds one row
    per outstanding promise — single digits in practice — so fetching all of it costs less than
    filtering it repeatedly (§Lessons 11).
    """
    if conn is None:
        conn = get_connection()
    init_reply_queue(conn)
    out: dict[str, dict[str, dict]] = {}
    for r in conn.execute("SELECT * FROM reply_queue"):
        row = _row(r)
        out.setdefault(row["contact_id"], {})[row["thread_key"]] = row
    return out


def stale_against(row: dict, thread_messages: list[dict]) -> str:
    """'' when the queued reply is still current, else why it is not.

    **A reply is an answer, so it can go out of date in a way a follow-up cannot.** If they wrote
    again between the promise and the firing, the text was composed against a conversation that
    has since moved — and sending it unattended means answering a message the operator has never
    read, in a thread where the other person is waiting on something else.

    Deliberately compared against the newest INBOUND only. Our own later messages in the thread
    do not invalidate an answer we wrote (and the send path itself appends one), and treating
    them as staleness would make every queued reply cancel itself.
    """
    was = (row.get("last_inbound_at") or "").strip()
    newest = ""
    for m in thread_messages or []:
        if (m.get("direction") or "") == "in":
            at = str(m.get("sent_at") or m.get("at") or "")
            newest = max(newest, at)
    if not newest or not was:
        # Nothing recorded on either side is not evidence of staleness. A promise made before
        # this column existed, or a thread we hold no inbound for, sends as written — refusing on
        # missing data would block the ordinary case (§Lessons 34).
        return ""
    if newest > was:
        return ("they wrote again on "
                f"{newest[:10]} after this answer was written — read it before sending")
    return ""

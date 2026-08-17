"""The `messages` table — conversation memory.

HEADERS ONLY. No bodies, and no snippets: the schema is the guarantee, not a policy note in a
docstring. Adding threads to this database already changes what a leak of
`~/.applypilot/applypilot.db` costs — today it holds names and drafts, now it holds who spoke
to whom and when. Bodies would make it correspondence.

Rows are keyed by Gmail's own `message_id`, so syncing the same thread repeatedly is a no-op.
That matters because `tick` may run hourly forever.
"""

from __future__ import annotations

import html
import json
import sqlite3

from applypilot.database import get_connection, schema_ready


def _decode(text: str | None) -> str:
    """Gmail hands back HTML-escaped snippet text. Store what a person would read.

    `html.unescape` is idempotent on already-clean text, which is what makes it safe to run over
    the existing rows as well as over every new one.
    """
    return html.unescape(text or "")

_MESSAGE_COLUMNS: dict[str, str] = {
    # Gmail's id dedupes, but it is NOT the key on its own: one message legitimately belongs to
    # several contacts. The Writer thread has both Victoria and David on it, and under a
    # message_id primary key "Pull all Gmail" on David reassigned all three rows to him and
    # emptied Victoria's conversation — measured, 3 → 0, on one click. See migration 002.
    "message_id": "TEXT NOT NULL",
    "thread_id": "TEXT NOT NULL",
    "contact_id": "TEXT NOT NULL DEFAULT ''",
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
    conn.execute(f"CREATE TABLE IF NOT EXISTS messages ({cols}, "
                 f"PRIMARY KEY (message_id, contact_id))")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_contact ON messages(contact_id)")
    conn.commit()
    existing = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    for col, dtype in _MESSAGE_COLUMNS.items():
        if col not in existing and "PRIMARY KEY" not in dtype:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {dtype}")
    conn.commit()
    return conn


def upsert_messages(rows: list[dict], conn: sqlite3.Connection | None = None,
                    full: bool = False) -> int:
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
    # Keyed by (message, contact) to match the primary key. Keying on message_id alone made
    # "already stored" mean "stored for SOMEBODY", so syncing a shared thread for a second
    # contact reported 0 new while quietly reassigning the rows.
    existing = {(r[0], r[1] or ""): (r[2] or "")
                for r in conn.execute(
                    "SELECT message_id, contact_id, snippet FROM messages").fetchall()}
    # `rfc_message_id` gets the same protection as the snippet, and it is the more dangerous of
    # the two to lose: it is what a later reply chains `References` from, so blanking it tells the
    # recipient's mail client that this conversation is a different conversation. `INSERT OR
    # REPLACE` writes every column, so a caller that legitimately does not know the RFC header —
    # the send paths know Gmail's id and not the header it generated — would erase one the poller
    # had already read from the message itself.
    existing_rfc = {(r[0], r[1] or ""): (r[2] or "")
                    for r in conn.execute(
                        "SELECT message_id, contact_id, rfc_message_id FROM messages").fetchall()}
    known = set(existing)
    new = 0
    for r in rows:
        mid = r.get("message_id") or r.get("id")
        if not mid:
            continue
        key = (mid, r.get("contact_id") or "")
        if key not in known:
            new += 1
        # Truncated HERE, at the write, not at the caller. A cap that lives in the caller is
        # one a future caller forgets; this one cannot be bypassed by any path into the table.
        #
        # DECODED first, and before the cap. Gmail's API returns `snippet` HTML-ESCAPED — the
        # apostrophe in "I'm" arrives as `&#39;` — and storing that raw put the entity through
        # the dashboard's own `esc()` a second time, so 70 of 99 stored messages rendered
        # "I&#39;m" on screen. Decoding at render would fix the display and leave the table
        # holding markup; decoding here means one representation, and the cap then counts
        # CHARACTERS a person reads rather than the 5 bytes an apostrophe costs.
        #
        # TWO declared bounds, chosen by the kind of read that produced the row — never a
        # number a caller invents. `SNIPPET_MAX` is Gmail's ~200-character preview, which is
        # all an AUTOMATIC sync ever sees; `PASTED_MAX` is for text somebody deliberately asked
        # for, either by pasting it or by clicking "⤓ Fetch from Gmail". Without the
        # distinction the explicit fetch was capped at the automatic bound, so the button that
        # exists to read a message in full stored the same preview.
        #
        # And the cap applies ONLY to what is ARRIVING. Text being preserved was already capped
        # correctly when it was written, so re-capping it truncates a full body this call had
        # just declined to overwrite — live proof: a thread fetched at 613/486/262 characters
        # came back 200/200/200/200 after one automatic sync that stored nothing new.
        incoming = _decode(r.get("snippet")).strip()
        snippet = (incoming[:(PASTED_MAX if full else SNIPPET_MAX)] if incoming
                   else existing.get(key, ""))
        rfc = (r.get("rfc_message_id") or "").strip() or existing_rfc.get(key, "")
        conn.execute(
            "INSERT OR REPLACE INTO messages (message_id, thread_id, contact_id, job_url, "
            "direction, from_addr, from_name, to_addrs, cc_addrs, subject, sent_at, synced_at, "
            "rfc_message_id, snippet) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, r.get("thread_id"), r.get("contact_id"), r.get("job_url"),
             r.get("direction"), r.get("from_addr"), r.get("from_name"),
             json.dumps(r.get("to_addrs") or []), json.dumps(r.get("cc_addrs") or []),
             r.get("subject"), r.get("sent_at"), now, rfc, snippet))
    conn.commit()
    return new


def record_outbound(contact: dict, sent: dict, to_addr: str, cc: list[str], subject: str,
                    conn: sqlite3.Connection | None = None, body: str = "") -> None:
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
        # OUR OWN words. No scope question and no privacy trade — the operator typed this.
        # Without it the thread showed "Sent from ApplyPilot." where the reply they had just
        # written should be, which reads as the message having been lost.
        "snippet": (body or "").strip()[:PASTED_MAX],
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


def set_reply_text(contact_id: str, text: str, conn: sqlite3.Connection | None = None,
                   message_id: str | None = None) -> bool:
    """Record what they said, pasted by the operator, onto the newest INBOUND message.

    A deliberately separate entry point from `upsert_messages`, not a parameter on it. The two
    have different provenance — one is text Gmail handed us because a scope was granted, the
    other is text a human chose to paste — and collapsing them would make the auto-ingest cap
    depend on which caller happened to be running.

    Attaches to the last inbound message so the sequence stays a sequence: the reply text lands
    ON the reply, not in a field beside the conversation.

    **`message_id` names WHICH message**, and a caller with more than one conversation open must
    pass it. Without it this takes the newest inbound across every thread the contact has — so
    text pasted under one conversation lands on a message in another, which is the merged-thread
    assumption `reply_target` and `_draft_reply` both had (§Lessons 49, a third call site). The
    unscoped default is kept for the CLI and for a contact with a single thread, where it is the
    same answer.
    """
    if conn is None:
        conn = get_connection()
    init_messages(conn)
    if message_id:
        row = conn.execute(
            "SELECT message_id FROM messages WHERE contact_id = ? AND message_id = ?",
            (contact_id, message_id)).fetchone()
    else:
        row = conn.execute(
            "SELECT message_id FROM messages WHERE contact_id = ? AND direction = 'in' "
            "ORDER BY sent_at DESC LIMIT 1", (contact_id,)).fetchone()
    if not row:
        return False
    # Decoded here too. This path does its own UPDATE rather than going through
    # `upsert_messages`, so the fix there does not reach it — and "⤓ Fetch from Gmail" pulls
    # from the same API that escapes the text in the first place (§Lessons 49).
    conn.execute("UPDATE messages SET snippet = ? WHERE message_id = ?",
                 (_decode(text).strip()[:PASTED_MAX], row[0]))
    conn.commit()
    return True


def threads_by_shared_address(conn: sqlite3.Connection | None = None) -> dict:
    """`{address: [messages]}` for people who exist on MORE THAN ONE contact row.

    `store.contact_id()` hashes `(job_url, linkedin_url, name)`, so the same human found for a
    second role is a second row with its own empty history — and this table is keyed on
    `contact_id`. Opening that second card showed a compose box for somebody already
    mid-conversation, which is how the same person gets written to twice (CO-1's keying half).

    Measured live: **5 addresses**, one of them nine messages deep with three replies. **Zero**
    have history on BOTH rows, which is what makes surfacing it safe rather than a merge.

    ONE query, and restricted to addresses that genuinely appear twice — the ordinary card pays
    nothing and the map stays small enough to hoist above the job loop on a 2.5s refresh.
    """
    if conn is None:
        conn = get_connection()
    init_messages(conn)
    out: dict[str, list[dict]] = {}
    for r in conn.execute(
        "SELECT m.*, LOWER(TRIM(c.email)) AS _addr FROM messages m "
        "JOIN contacts c ON c.id = m.contact_id "
        "WHERE LOWER(TRIM(COALESCE(c.email,''))) IN ("
        "  SELECT LOWER(TRIM(email)) FROM contacts "
        "  WHERE email IS NOT NULL AND TRIM(email) != '' "
        "  GROUP BY LOWER(TRIM(email)) HAVING COUNT(*) > 1) "
        "ORDER BY m.sent_at"
    ).fetchall():
        out.setdefault(r["_addr"], []).append(_row(r))
    return out


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


def recoverable_outbound_bodies(conn: sqlite3.Connection | None = None) -> list[dict]:
    """Our own sent messages stored with NO text, and where the text still is.

    The poller writes a row for every message it sees, including ours, with `snippet: ""` — right
    for it, since the automatic path reads no text at all. But nothing else filled them in until
    2026-08-17, so the conversation carries a hole wherever we spoke: the card renders
    "Sent from ApplyPilot." and the drafter's transcript is missing our own half.

    Measured when this was written: **261 empty outbound rows, 254 of them recoverable** — 168
    from the `touches` row that sent them and 86 from `contacts.outreach_message`, with ZERO
    ambiguous. The remaining 7 were sent from Gmail directly and were never ours to hold.

    Two sources, in priority order, and the FIRST-EMAIL one is exact:

      * `contacts.sent_message_id` identifies the first cold email by Gmail's own id, so that
        match cannot be wrong.
      * A touch is matched by MINUTE, because `_TOUCH_COLUMNS` has no message id (§Lessons 77).
        Deterministic, and verified to have no collisions on the live data — but it is an
        inference, so it is reported as one and applied second.

    Returns rows rather than writing, so the audit and the repair read the same list and the
    operator sees the count before anything changes.
    """
    if conn is None:
        conn = get_connection()
    init_messages(conn)
    from applypilot.networking.store import init_contacts
    init_contacts(conn)
    out: list[dict] = []
    for r in conn.execute("""
        SELECT m.message_id, m.contact_id, m.sent_at, ct.full_name, ct.sent_message_id,
               ct.outreach_message,
               (SELECT t.body FROM touches t
                 WHERE t.contact_id = m.contact_id
                   AND substr(COALESCE(t.sent_at,''),1,16) = substr(COALESCE(m.sent_at,''),1,16)
                   AND TRIM(COALESCE(t.body,'')) != ''
                 ORDER BY t.seq LIMIT 1) AS touch_body
          FROM messages m LEFT JOIN contacts ct ON ct.id = m.contact_id
         WHERE m.direction = 'out' AND TRIM(COALESCE(m.snippet,'')) = ''
         ORDER BY m.sent_at"""):
        row = dict(zip(r.keys(), r))
        first = (row.get("outreach_message") or "").strip()
        touch = (row.get("touch_body") or "").strip()
        if row.get("sent_message_id") and row["message_id"] == row["sent_message_id"] and first:
            body, how = first, "first email (exact message id)"
        elif touch:
            body, how = touch, "follow-up touch (matched by minute)"
        else:
            continue
        out.append({"message_id": row["message_id"], "contact_id": row["contact_id"],
                    "full_name": row.get("full_name") or "", "sent_at": row.get("sent_at") or "",
                    "body": body, "how": how})
    return out


def restore_outbound_bodies(rows: list[dict],
                            conn: sqlite3.Connection | None = None) -> int:
    """Write recovered text back. Only ever fills a row that is still EMPTY.

    The `TRIM(...) = ''` guard is not belt-and-braces: this runs long after the rows were
    written, and anything that has since been fetched or pasted is better than what is being
    restored here. A repair that can overwrite real text is not a repair.
    """
    if conn is None:
        conn = get_connection()
    init_messages(conn)
    n = 0
    for row in rows:
        body = (row.get("body") or "").strip()[:PASTED_MAX]
        if not body:
            continue
        cur = conn.execute(
            "UPDATE messages SET snippet = ? WHERE message_id = ? AND contact_id = ? "
            "AND TRIM(COALESCE(snippet,'')) = ''",
            (body, row["message_id"], row["contact_id"]))
        n += cur.rowcount
    conn.commit()
    return n

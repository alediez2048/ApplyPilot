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
    # Which APPLICATION this touch was part of (CO-2). Empty means "whatever job the contact is
    # on now", so all 233 existing rows are unchanged and no backfill is needed.
    #
    # A touch is keyed on the contact and follows them blindly, which is what makes moving
    # somebody between roles the one operation here that can silently destroy a ladder: three
    # sent touches carried onto a new role make `count >= len(schedule)` immediately, so the
    # channel reads `finished` and never follows up again. Stamping the job lets the two
    # questions be asked separately, which they always were:
    #
    #   `sent_touches()`  — what have we ever said to this person?      ALL of them.
    #   `ladder_states()` — how far through THIS role's plan are we?    only this job's.
    #
    # The disagreement is deliberate. A follow-up drafter wants everything (so it does not
    # repeat itself); the scheduler wants only the plan it is executing.
    "job_url": "TEXT",
    # When this drafted touch should be SENT without anybody clicking (bulk scheduling).
    #
    # Deliberately a column here rather than a `scheduled_sends` table: the pending touch row
    # already IS this follow-up — its draft, its seq, its delivery status. A promise to send it
    # is a property of that row, and putting it anywhere else creates two records of one thing
    # that can disagree about which text goes out.
    #
    # Empty on every row that was never scheduled, so nothing is backfilled and the ordinary
    # draft-then-click path is unchanged. `due_at` above is NOT this: that one records when a
    # touch came due after the fact, this one is a promise about the future.
    "scheduled_at": "TEXT",
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
    conn.commit()
    _ensure_columns(conn, "touches", _TOUCH_COLUMNS)
    _ensure_columns(conn, "sequences", _SEQUENCE_COLUMNS)
    # Indexes come AFTER the column pass, and that order is load-bearing rather than tidy.
    # On an existing database `CREATE TABLE IF NOT EXISTS` above is a no-op, so a column added
    # to `_TOUCH_COLUMNS` does not exist until `_ensure_columns` runs — indexing it any earlier
    # raises `no such column` on every install that already had this table, and on none of the
    # fresh ones a test would build. Same race the migrations warn about, one layer down.
    #
    # Reads are always "this contact, this channel, in order".
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_touches_ladder "
                 "ON touches(contact_id, channel, seq)")
    # What a scheduler asks for (CRM-3): what is owed, oldest first.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_touches_due ON touches(status, due_at)")
    # What the POLLER asks for, every five minutes, forever: which promises have come due.
    # The index above looks like it covers this and does not — `due_at` records when a touch
    # became owed, `scheduled_at` is when we promised to send it.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_touches_scheduled "
                 "ON touches(status, scheduled_at)")
    conn.commit()
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
            "touch_status": "", "draft_subject": "", "draft_body": "", "error": "",
            "scheduled_at": ""}


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


def sent_touch_bodies(conn: sqlite3.Connection | None = None) -> dict:
    """`{contact_id: {"YYYY-MM-DDTHH:MM": body}}` for every SENT touch that has text.

    ONE query for the whole page, like `all_sent_touches` beside it — the contact card renders
    every follow-up we sent, and looking each one up per contact is the N+1 the query budget
    exists to catch (§Lessons 11: 313 statements per request before anyone counted).

    Keyed by MINUTE because `touches` carries no message id, which is the same join the drafter
    makes (§Lessons 77 records the gap; §Lessons 123 measured it at max 1.3 seconds of skew
    before trusting it).
    """
    if conn is None:
        conn = get_connection()
    init_touches(conn)
    rows = conn.execute(
        "SELECT contact_id, sent_at, body FROM touches "
        "WHERE sent_at IS NOT NULL AND body IS NOT NULL AND body != ''"
    ).fetchall()
    out: dict = {}
    for r in rows:
        d = dict(zip(r.keys(), r))
        cid, at = d.get("contact_id"), (d.get("sent_at") or "")[:16]
        if cid and at:
            out.setdefault(cid, {})[at] = d.get("body") or ""
    return out


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
    init_touches(conn)
    # The scoping JOIN below reads `contacts`, which this module has never needed before. Both
    # are memoised by `schema_ready`, so on the refresh path they cost nothing after the first
    # call — but a test that drives touches against a bare database would otherwise fail on a
    # missing table rather than on anything it is testing.
    from applypilot.networking.store import init_contacts
    init_contacts(conn)
    out: dict[tuple[str, str], dict] = {}
    marks = ",".join("?" for _ in contact_ids)

    # Only the touches belonging to the job the contact is on NOW (CO-2). An empty
    # `t.job_url` is every touch written before that column existed and every one written
    # since by the ordinary send path — it means "this contact's own job", so the filter is a
    # no-op until somebody is actually moved.
    #
    # A LEFT JOIN, so a touch whose contact row has gone still comes back rather than
    # vanishing: an orphaned ladder is a bug to see, not one to hide (see the reasoning on
    # `store.delete_contact`). Still ONE statement — this runs on the 2.5s refresh path.
    for row in conn.execute(
        f"SELECT t.contact_id, t.channel, t.seq, t.sent_at, t.status, t.subject, t.body, "
        f"t.error, t.scheduled_at FROM touches t LEFT JOIN contacts c ON c.id = t.contact_id "
        f"WHERE t.contact_id IN ({marks}) "
        f"AND COALESCE(t.job_url, '') IN ('', COALESCE(c.job_url, '')) "
        f"ORDER BY t.contact_id, t.channel, t.seq",
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
            # Rides the SAME query the dashboard already runs for every contact on every 2.5s
            # refresh. Reading it separately would be one statement per contact against a
            # budget with six of eighty to spare (§Lessons 11) — and the whole reason
            # `ladder_states` is a bulk load in the first place.
            st["scheduled_at"] = row["scheduled_at"] or ""

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
    # `scheduled_at` is deliberately NOT in this list, so re-drafting a scheduled touch keeps
    # its appointment. Revising the words and withdrawing the promise are different intentions,
    # and only one of them has a button: silently unscheduling on every redraft would mean an
    # operator who fixed a typo at 5pm quietly stopped tomorrow's send with nothing on screen
    # to say so. `cancel_schedule` is how you take it back.
    _upsert_touch(conn, contact_id, channel, seq,
                  subject=subject or "", body=body or "", status="drafted", error=None)
    conn.commit()
    return seq


def schedule_send(contact_id: str, channel: str, at_iso: str,
                  conn: sqlite3.Connection | None = None) -> bool:
    """Promise to send the pending DRAFT at `at_iso`. False when there is nothing to promise.

    Only ever touches a row that is already drafted — you cannot schedule text that does not
    exist yet. That is not a storage limitation, it is the rule: a send you have not read is a
    send you cannot judge, and this is the one path in the app that acts while nobody is
    watching. Drafting first is a click, and it is the click where the operator sees the words.

    `status` is left at 'drafted'. A scheduled send is an ordinary pending touch with a time on
    it, so every reader that already understood drafts — the card, the ladder, the counter —
    keeps working untouched, and a schedule that never fires degrades to exactly the state the
    operator was in before they set one.
    """
    if conn is None:
        conn = get_connection()
    init_touches(conn)
    cur = conn.execute(
        "UPDATE touches SET scheduled_at = ?, updated_at = ? "
        "WHERE contact_id = ? AND channel = ? AND status = 'drafted' "
        "AND TRIM(COALESCE(body, '')) != ''",
        (at_iso, _now(), contact_id, channel))
    conn.commit()
    return cur.rowcount >= 1


def cancel_schedule(contact_id: str, channel: str,
                    conn: sqlite3.Connection | None = None) -> bool:
    """Withdraw the promise, keep the draft.

    Cancelling a scheduled send must never destroy the words — the operator asked to not send
    it *now*, which is a different request from "throw it away", and the draft may have been
    hand-edited.
    """
    if conn is None:
        conn = get_connection()
    init_touches(conn)
    cur = conn.execute(
        "UPDATE touches SET scheduled_at = NULL, updated_at = ? "
        "WHERE contact_id = ? AND channel = ? AND COALESCE(scheduled_at, '') != ''",
        (_now(), contact_id, channel))
    conn.commit()
    return cur.rowcount >= 1


#: How many promises to ONE employer may fire in a single poll. The unit is the employer because
#: that is the unit the RECIPIENT experiences — five people at one company comparing notes see
#: five emails, and what makes them look machine-sent is the timestamps being identical.
#:
#: Measured on the first real batch (2026-08-17): 33 sends, and **Acrisure got 5 in two seconds**,
#: Texas Children's 5 in three, Miro 3 in one. Both existing guards were blind to it — the daily
#: limit is global and the cooldown is per address — and both are set to 0 on this machine anyway.
#:
#: The spacing is the POLLER'S OWN CADENCE rather than a sleep: hold the rest back and they are
#: claimed on the next pass, so five at one company spread across ~20 minutes with no timer, no
#: blocked thread, and no new failure mode. Anything not claimed is not cleared either, so a
#: held-back promise is still exactly a promise.
_PER_COMPANY_PER_POLL = 1


def _one_per_company(rows: list[dict], cap: int) -> list[dict]:
    """Keep at most `cap` rows per employer, earliest first. Rows arrive ordered by due time.

    An empty company is NOT collapsed into one bucket: a target card or an unresolved employer
    would otherwise throttle every unrelated row down to one per poll. Those are keyed on the
    contact instead, which makes them their own bucket and preserves the old behaviour exactly.
    """
    seen: dict[str, int] = {}
    out = []
    for r in rows:
        key = (r.get("company") or "").strip().lower() or f"~{r.get('contact_id')}"
        if seen.get(key, 0) >= cap:
            continue
        seen[key] = seen.get(key, 0) + 1
        out.append(r)
    return out


def claim_due_scheduled(now_iso: str, lapsed_before: str,
                        limit: int = 50, per_company: int = _PER_COMPANY_PER_POLL,
                        conn: sqlite3.Connection | None = None) -> list[dict]:
    """Take every promise that has come due, CLEARING it in the same statement.

    Two properties, and both are about the fact that this runs unattended every five minutes:

    **The claim happens before the send, never after.** If the process dies mid-send the promise
    is already gone, so the worst case is a follow-up that went out and is not marked scheduled
    any more — recoverable, visible, and one email. Clearing afterwards inverts that into a
    crash-loop that re-sends the same message every five minutes, which is the worst outcome
    this subsystem can produce.

    **Firing consumes the promise whatever the outcome.** A send refused by the daily limit or
    the per-company cap does not stay scheduled and retry itself into the same refusal forever;
    the draft survives, the card shows it as ordinary due work, and the operator decides. A
    scheduled send is a promise to try once at a time, not a standing order.

    `lapsed_before` excludes promises too old to fire on their own — see `domain/sendtime.py`.
    They are deliberately NOT claimed and NOT cleared, so they stay visible on the card as
    missed rather than disappearing or arriving a week late.
    """
    if conn is None:
        conn = get_connection()
    init_touches(conn)
    from applypilot.networking.store import init_contacts
    init_contacts(conn)
    rows = conn.execute(
        "SELECT t.contact_id, t.channel, t.scheduled_at, COALESCE(c.company, '') AS company "
        "FROM touches t LEFT JOIN contacts c ON c.id = t.contact_id "
        "WHERE t.status = 'drafted' AND COALESCE(t.scheduled_at, '') != '' "
        "AND t.scheduled_at <= ? AND t.scheduled_at > ? "
        "ORDER BY t.scheduled_at LIMIT ?",
        (now_iso, lapsed_before, limit)).fetchall()
    claimed = _one_per_company([dict(zip(r.keys(), r)) for r in rows], per_company)
    for row in claimed:
        conn.execute("UPDATE touches SET scheduled_at = NULL, updated_at = ? "
                     "WHERE contact_id = ? AND channel = ? AND scheduled_at = ?",
                     (_now(), row["contact_id"], row["channel"], row["scheduled_at"]))
    conn.commit()
    return claimed


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
    # `scheduled_at` is cleared here too. The poller already clears it when it claims a promise,
    # so this covers the other way a scheduled touch leaves the queue: the operator got there
    # first and sent it by hand. Leaving the timestamp on a sent row would put a future
    # appointment on a message that has already gone.
    _upsert_touch(conn, contact_id, channel, seq,
                  status="sent", sent_at=now, error=None, due_at=due_at or None,
                  scheduled_at=None)
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

"""ARCH-3 backfill: ten `contacts` columns → the `touches` / `sequences` tables.

The first migration in this codebase that moves DATA rather than adding a column, so the
order is backup → dry-run diff → write, and every step is checkable:

    applypilot migrate-touches --dry-run     # prints the row-by-row plan, writes nothing
    applypilot migrate-touches               # backs up first, then writes
    applypilot migrate-touches --verify      # re-derives old state from the new tables

`--verify` is the one that matters. It reads the new tables, reconstructs what the old
columns WOULD say, and diffs that against what they DO say. Anything that does not round-trip
is a bug in the backfill, and it is far better to learn that while the old columns still hold
the truth than after they are dropped.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from applypilot import config
from applypilot.database import get_connection
from applypilot.networking import touches as T

# (channel, count_col, last_col, status_col, subject_col, body_col, error_col)
# `None` where the old schema simply never had that column for that channel — which is
# itself the drift this ticket removes.
_LEGACY = (
    ("email", "followup_count", "followed_up_at", "followup_status",
     "followup_subject", "followup_message", "followup_error"),
    ("linkedin", "li_followup_count", "li_followed_up_at", "li_followup_status",
     None, "li_followup_message", None),
)

LEGACY_COLUMNS = tuple(
    c for spec in _LEGACY for c in spec[1:] if c is not None
)

_SEQUENCE_TERMINAL = ("stopped", "replied")
_PENDING = ("drafted", "sending", "failed")


def _get(row: dict, col: str | None) -> str:
    if not col:
        return ""
    return str(row.get(col) or "").strip()


def plan(conn: sqlite3.Connection | None = None) -> list[dict]:
    """What the backfill WOULD write. Pure — touches nothing."""
    if conn is None:
        conn = get_connection()
    existing = {r[1] for r in conn.execute("PRAGMA table_info(contacts)")}
    out: list[dict] = []
    for raw in conn.execute("SELECT * FROM contacts ORDER BY discovered_at, id"):
        row = dict(raw)
        for channel, count_c, last_c, status_c, subj_c, body_c, err_c in _LEGACY:
            if count_c not in existing:      # already dropped — nothing to migrate
                continue
            count = int(row.get(count_c) or 0)
            last = _get(row, last_c)
            status = _get(row, status_c)
            subject, body, error = _get(row, subj_c), _get(row, body_c), _get(row, err_c)
            if not (count or last or status or subject or body or error):
                continue

            item = {"contact_id": row["id"], "name": row.get("full_name") or "",
                    "channel": channel, "touches": [], "sequence_status": ""}
            # One row per touch already sent. Only the LAST timestamp was ever stored, so
            # earlier touches get an empty sent_at — the old schema genuinely did not know.
            for seq in range(1, count + 1):
                item["touches"].append({
                    "seq": seq, "status": "sent",
                    "sent_at": last if seq == count else "",
                })
            if status in _SEQUENCE_TERMINAL:
                item["sequence_status"] = status
            elif status in _PENDING or subject or body:
                # A staged-but-unsent draft becomes the pending row for the next touch.
                item["touches"].append({
                    "seq": count + 1, "status": status if status in _PENDING else "drafted",
                    "sent_at": "", "subject": subject, "body": body, "error": error,
                })
            out.append(item)
    return out


def describe(items: list[dict]) -> str:
    lines = []
    for it in items:
        sent = [t for t in it["touches"] if t["status"] == "sent"]
        pend = [t for t in it["touches"] if t["status"] != "sent"]
        bits = [f"{len(sent)} sent"]
        if pend:
            bits.append(f"1 pending ({pend[0]['status']})")
        if it["sequence_status"]:
            bits.append(f"sequence={it['sequence_status']}")
        last = next((t["sent_at"] for t in reversed(sent) if t["sent_at"]), "")
        lines.append(f"  {it['name'][:22]:24} {it['channel']:9} {', '.join(bits):32} {last[:19]}")
    if not lines:
        return "  (nothing to migrate)"
    return "\n".join(lines)


def backup_db() -> Path:
    """sqlite's own backup API — consistent with WAL, which `cp` is not.

    A plain copy of applypilot.db misses everything still in the -wal file, which on this
    machine was 2.2MB against a 400KB db. That is exactly the follow-up state we cannot lose.
    """
    src = Path(config.DB_PATH)
    dest_dir = src.parent / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = dest_dir / f"applypilot-{stamp}-pre-touches.db"
    con = sqlite3.connect(str(src))
    out = sqlite3.connect(str(dest))
    try:
        con.backup(out)
    finally:
        out.close()
        con.close()
    return dest


def apply(conn: sqlite3.Connection | None = None, items: list[dict] | None = None) -> dict:
    """Write the plan. Idempotent — touch ids are derived from (contact, channel, seq)."""
    if conn is None:
        conn = get_connection()
    T.init_touches(conn)
    items = plan(conn) if items is None else items
    n_touches = n_seq = 0
    for it in items:
        for t in it["touches"]:
            T._upsert_touch(
                conn, it["contact_id"], it["channel"], t["seq"],
                status=t["status"], sent_at=t.get("sent_at") or None,
                subject=t.get("subject") or None, body=t.get("body") or None,
                error=t.get("error") or None,
            )
            n_touches += 1
        if it["sequence_status"]:
            T.set_sequence_status(it["contact_id"], it["channel"], it["sequence_status"],
                                  note="backfilled from contacts columns", conn=conn)
            n_seq += 1
    conn.commit()
    return {"contacts": len({i["contact_id"] for i in items}),
            "touches": n_touches, "sequences": n_seq}


def verify(conn: sqlite3.Connection | None = None) -> list[str]:
    """Re-derive the OLD columns from the NEW tables and diff. Empty list == clean.

    Run this while both representations still exist. Once the columns are dropped there
    is nothing left to check against.
    """
    if conn is None:
        conn = get_connection()
    existing = {r[1] for r in conn.execute("PRAGMA table_info(contacts)")}
    problems: list[str] = []
    rows = [dict(r) for r in conn.execute("SELECT * FROM contacts")]
    states = T.ladder_states([r["id"] for r in rows], conn)

    for row in rows:
        for channel, count_c, last_c, status_c, _subj, _body, _err in _LEGACY:
            if count_c not in existing:
                continue
            st = states.get((row["id"], channel)) or T._empty_state()
            who = f"{(row.get('full_name') or row['id'])[:20]}/{channel}"

            want_count = int(row.get(count_c) or 0)
            if st["count"] != want_count:
                problems.append(f"{who}: count {st['count']} != {want_count}")

            want_last = _get(row, last_c)
            if want_last and st["last_sent_at"] != want_last:
                problems.append(f"{who}: last_sent_at {st['last_sent_at']!r} != {want_last!r}")

            want_status = _get(row, status_c)
            if want_status in _SEQUENCE_TERMINAL and st["sequence_status"] != want_status:
                problems.append(f"{who}: sequence {st['sequence_status']!r} != {want_status!r}")
            if want_status in _PENDING and st["touch_status"] != want_status:
                problems.append(f"{who}: pending touch {st['touch_status']!r} != {want_status!r}")
    return problems


def drop_legacy_columns(conn: sqlite3.Connection | None = None) -> list[str]:
    """Remove the ten migrated columns. Only ever call this after verify() is clean."""
    if conn is None:
        conn = get_connection()
    existing = {r[1] for r in conn.execute("PRAGMA table_info(contacts)")}
    dropped = []
    for col in LEGACY_COLUMNS:
        if col in existing:
            conn.execute(f"ALTER TABLE contacts DROP COLUMN {col}")
            dropped.append(col)
    conn.commit()
    return dropped

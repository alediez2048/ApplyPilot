"""CO-2 — move contacts from a dead role to a live one.

Asked for as: *"The startup lead role was canceled… I already have a full list of contacts that
I can continue reaching out to and would like to migrate over to a brand-new job application."*

This is the other half of CO-1's keying problem, from the direction that hurts.
`store.contact_id()` hashes `(job_url, linkedin_url, name)`, so a person's identity is bound to
the application they were found for — and `touches` and `sequences` carry no `job_url` at all
and follow that id blindly. Moving somebody is therefore the ONE operation here that can
silently destroy a ladder, which is why every step below is either non-destructive or recorded
for undo.

It lives in `networking/` rather than a new `repo/contacts.py` on purpose: `store.py` already
IS the repository for `contacts`, and two abstractions over one table is the failure mode
ARCH-4's ticket warns about. This is an OPERATION across five tables, so it gets its own module
next to them rather than a second door onto one of them.

## What is destroyed, and what is not

Nothing about a send that really happened. `submitted_at`, `sent_message_id`, `replied_at` and
the sent copy all survive the move untouched — the outreach is STAMPED with the job it was for
(`contacts.outreach_job_url`, `touches.job_url`) and every reader that asks "has this person
been contacted about THIS role" consults the stamp.

Clearing them instead would read correctly on the new card and would also drop those sends out
of the CRM-2 funnel while their replies stayed in it, and disarm the cross-job cooldown that is
the only guard against emailing one person about a second role. §Lessons 86: a guard that a
legitimate write can switch off is the wrong guard.

Two things ARE destroyed, both recorded in the undo record before they go:

  * an **unsent draft**, because all 16 of them name the cancelled role by name. A draft that
    pitches a dead job is worse than no draft: it is one click from being sent and looks ready.
  * the **poorer half of a collision**, when the same person is already on the destination as an
    empty row. Keeping both is how Patrick ended up with his conversation on one card and a
    compose box on the other.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from applypilot.database import get_connection
from applypilot.domain.company import companies_match
from applypilot.networking import interactions_store as _inter
from applypilot.networking import messages as _msg
from applypilot.networking import store, touches as _touches

#: Undo records, in memory only. The ticket's own scope: undo is available until the page is
#: reloaded. A table would outlive the decision it exists to reverse, and the destroyed set is
#: two small things — a draft and an empty duplicate row.
_UNDO: dict[str, dict] = {}

#: What a contact must have before moving is even meaningful. The operator's own rule:
#: *"the idea of migrating an existing contact to a new job is that we already began outreach,
#: so we can continue the conversations."* Somebody with no address was never part of that and
#: cannot be. They are REPORTED as excluded rather than hidden — live that is 5 of 16, and
#: silently dropping five of sixteen reads as a bug.
NO_EMAIL = "no email address, so there is nothing to continue"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _employer(job: dict) -> str:
    from applypilot.networking import derive
    return (derive.resolve_employer(job)[0] or job.get("company") or "").strip()


# ── the preview ─────────────────────────────────────────────────────────────

def plan(src_url: str, dst_url: str, conn: sqlite3.Connection | None = None) -> dict:
    """What a move would do, without doing any of it.

    Returns {ok, error, employer, groups, movable, excluded, collisions, refused}. The dialog
    renders this; `apply()` re-derives it so a stale page cannot move somebody the operator was
    never shown.
    """
    if conn is None:
        conn = get_connection()
    from applypilot.repo import jobs as _jobs

    if not src_url or not dst_url or src_url == dst_url:
        return {"ok": False, "error": "pick two different applications"}
    src_job = _jobs.find_by_any_url(src_url, conn)
    dst_job = _jobs.find_by_any_url(dst_url, conn)
    if not src_job or not dst_job:
        return {"ok": False, "error": "one of those applications no longer exists"}

    # Same employer only. Not a nicety: these are people who WORK at that company, and a button
    # that can file them under a different one puts real humans on a card where they do not
    # belong — §Lessons 68's cost, arrived at deliberately rather than discovered.
    a, b = _employer(src_job), _employer(dst_job)
    if not (a and b and companies_match(a, b)):
        return {"ok": False,
                "error": f"different employers ({a or 'unknown'} → {b or 'unknown'}); "
                         "contacts only move between roles at the same company"}

    here = {c["id"]: c for c in store.get_contacts_for_job(dst_url, conn)}
    by_email = {store._norm_email(c.get("email")): c for c in here.values()
                if store._norm_email(c.get("email"))}
    threads = _msg.threads_for_job(src_url, conn)
    dst_threads = _msg.threads_for_job(dst_url, conn)

    movable, excluded, collisions, refused = [], [], [], []
    for c in store.get_contacts_for_job(src_url, conn):
        row = {"id": c["id"], "full_name": c.get("full_name") or "",
               "title": c.get("title") or "", "email": c.get("email") or "",
               "messages": len(threads.get(c["id"]) or []),
               "replied": bool((c.get("replied_at") or "").strip()),
               "emailed": bool((c.get("sent_message_id") or "").strip())
                          or c.get("outreach_status") == "submitted"}
        if not store._norm_email(c.get("email")):
            excluded.append({**row, "why": NO_EMAIL})
            continue
        victim = _collision(c, dst_url, here, by_email)
        if victim is not None:
            src_msgs = {m.get("message_id") for m in (threads.get(c["id"]) or [])}
            dst_msgs = {m.get("message_id") for m in (dst_threads.get(victim["id"]) or [])}
            mine, theirs = row["messages"], len(dst_msgs)
            # Two conversations, or ONE conversation stored twice? The distinction is the
            # message IDS, not the counts.
            #
            # `replies.sync_all_with()` searches Gmail by ADDRESS and files what it finds under
            # whichever contact row asked — so the same thread lands on both of a duplicated
            # person's rows, with identical message ids. Counting rows called that two
            # conversations and refused the merge, which blocked the exact repair this exists
            # for: live, both WebAI pairs hold the SAME 4 messages, `identical=True`.
            #
            # A genuine second exchange is one the destination holds and the source does not.
            separate = bool(dst_msgs - src_msgs)
            if mine and theirs and separate:
                # Interleaving two real conversations is unrecoverable, so it is refused and
                # named rather than guessed at.
                refused.append({**row, "why": f"{row['full_name']} has a SEPARATE conversation on "
                                              "both applications; merging them is not reversible"})
                continue
            if theirs and not separate:
                # A copy, not a conversation. The destination row adds nothing, so the source
                # wins on whatever else it carries and the duplicate goes.
                theirs = 0
            keep_src = mine >= theirs
            collisions.append({**row, "keeps": "moved" if keep_src else "existing",
                               "other_messages": theirs})
            if not keep_src:
                # The destination row is the richer one; the source has nothing to add.
                excluded.append({**row, "why": f"already on that application with {theirs} "
                                               "messages, which is the fuller record"})
                continue
        movable.append(row)

    groups = {
        "replied": [r for r in movable if r["replied"]],
        "emailed": [r for r in movable if r["emailed"] and not r["replied"]],
        "fresh": [r for r in movable if not r["emailed"] and not r["replied"]],
    }
    drafts = _draft_count([r["id"] for r in movable], conn)
    return {"ok": True, "error": "", "employer": b, "src": src_url, "dst": dst_url,
            "src_title": src_job.get("title") or "", "dst_title": dst_job.get("title") or "",
            "groups": groups, "movable": movable, "excluded": excluded,
            "collisions": collisions, "refused": refused, "drafts_cleared": drafts}


def _collision(c: dict, dst_url: str, here: dict, by_email: dict) -> dict | None:
    """Is this person already on the destination?

    Matched on EMAIL first and the re-derived id second — the same order the card already uses
    to put one person's two rows together (SHEET-1b). Matching on the id alone would miss
    "Patrick" against "Patrick Omalley", which is the live case.
    """
    hit = by_email.get(store._norm_email(c.get("email")))
    if hit is not None:
        return hit
    new_id = store.contact_id(dst_url, c.get("linkedin_url"), c.get("full_name"))
    return here.get(new_id)


def _draft_count(ids: list[str], conn: sqlite3.Connection) -> int:
    if not ids:
        return 0
    marks = ",".join("?" for _ in ids)
    return int(conn.execute(
        f"SELECT COUNT(*) FROM contacts WHERE id IN ({marks}) "
        f"AND COALESCE(TRIM(outreach_message), '') != '' "
        f"AND COALESCE(sent_message_id, '') = '' AND COALESCE(outreach_status,'') != 'submitted'",
        ids).fetchone()[0])


# ── the move ────────────────────────────────────────────────────────────────

def apply(src_url: str, dst_url: str, ids: list[str],
          conn: sqlite3.Connection | None = None) -> dict:
    """Move the named contacts. Returns {ok, moved, undo, ...} — `undo` is the token.

    `ids` names WHO, and the plan decides whether each of them may go: the browser cannot move
    somebody the preview excluded, and it cannot move anybody at all if the employers stopped
    matching between the dialog opening and the click.
    """
    if conn is None:
        conn = get_connection()
    preview = plan(src_url, dst_url, conn)
    if not preview["ok"]:
        return preview
    allowed = {r["id"]: r for r in preview["movable"]}
    chosen = [i for i in (ids or []) if i in allowed]
    if not chosen:
        return {"ok": False, "error": "nothing to move"}

    # Undo lives in memory and dies with the process, so a bulk re-key of eleven people gets a
    # file on disk as well. Cheap, and the one thing that survives a restart.
    backup = _backup()
    space_id = _dst_space(dst_url, conn)
    record = {"src": src_url, "dst": dst_url, "at": _now(), "moves": [], "deleted": []}
    try:
        for cid in chosen:
            record["moves"].append(_move_one(cid, src_url, dst_url, space_id, conn))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    token = uuid4().hex[:12]
    _UNDO[token] = record
    return {"ok": True, "error": "", "moved": len(chosen), "undo": token, "backup": backup,
            "employer": preview["employer"], "dst_title": preview["dst_title"],
            "excluded": preview["excluded"], "refused": preview["refused"]}


def _backup() -> str:
    """sqlite's own backup API, never `cp` — a file copy misses everything still in the -wal,
    which on this machine has held 4.1 MB against a 1.8 MB main file. That is exactly the
    follow-up state this operation exists to protect. Returns '' if it could not be written,
    because failing to back up is not a reason to refuse a reversible move.
    """
    from pathlib import Path

    from applypilot import config
    try:
        src = Path(config.DB_PATH)
        dest_dir = src.parent / "backups"
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        dest = dest_dir / f"applypilot-{stamp}-pre-migrate.db"
        con, out = sqlite3.connect(str(src)), sqlite3.connect(str(dest))
        try:
            con.backup(out)
        finally:
            out.close()
            con.close()
        return dest.name
    except Exception:
        return ""


def _dst_space(dst_url: str, conn: sqlite3.Connection) -> str:
    from applypilot.repo import jobs as _jobs
    job = _jobs.find_by_any_url(dst_url, conn) or {}
    from applypilot.repo import spaces as _spaces
    return job.get("space_id") or _spaces.DEFAULT_SPACE_ID


def _move_one(cid: str, src_url: str, dst_url: str, space_id: str,
              conn: sqlite3.Connection) -> dict:
    """One person, one destination. Everything here is inside the caller's transaction."""
    src = store.get_contact(cid, conn) or {}
    new_id = store.contact_id(dst_url, src.get("linkedin_url"), src.get("full_name"))
    step: dict = {"old_id": cid, "new_id": new_id, "name": src.get("full_name") or "",
                  "cleared": None, "deleted": None}

    # 1. The collision, if any. The plan has already decided the source is the richer row, so
    #    the destination duplicate is the empty one and it goes — after being recorded whole.
    victim = conn.execute("SELECT * FROM contacts WHERE id = ? AND id != ?",
                          (new_id, cid)).fetchone()
    if victim is None:
        norm = store._norm_email(src.get("email"))
        if norm:
            victim = conn.execute(
                "SELECT * FROM contacts WHERE job_url = ? AND lower(trim(COALESCE(email,''))) = ?"
                " AND id != ?", (dst_url, norm, cid)).fetchone()
    if victim is not None:
        # Matched by EMAIL as well as by id, so the row that goes is often not the id we are
        # about to mint — "Patrick" and "Patrick Omalley" hash differently and are one human.
        step["deleted"] = _snapshot(victim["id"], conn)
        store.delete_contact(victim["id"], conn)

    # 2. The contact row itself. Re-keyed, never copied — a copy is a second row for one person,
    #    which is precisely the confusion this feature exists to end.
    unsent = (not (src.get("sent_message_id") or "").strip()
              and (src.get("outreach_status") or "") != "submitted")
    if unsent and (src.get("outreach_message") or src.get("outreach_subject")
                   or src.get("linkedin_message")):
        step["cleared"] = {"outreach_subject": src.get("outreach_subject") or "",
                           "outreach_message": src.get("outreach_message") or "",
                           "linkedin_message": src.get("linkedin_message") or "",
                           "outreach_status": src.get("outreach_status") or "none"}
    stamp = src_url if _has_outreach(src) else (src.get("outreach_job_url") or "")
    sets = {"id": new_id, "job_url": dst_url, "space_id": space_id,
            "outreach_job_url": stamp, "updated_at": _now()}
    if step["cleared"]:
        sets.update({"outreach_subject": "", "outreach_message": "", "linkedin_message": "",
                     "outreach_status": "none"})
    conn.execute(f"UPDATE contacts SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                 (*sets.values(), cid))

    # 3. The four tables keyed on the contact. `messages` follows the person — the operator's
    #    own answer, and continuity is the whole point; every message's subject already says
    #    which role it was about.
    conn.execute("UPDATE messages SET contact_id = ?, job_url = ? WHERE contact_id = ?",
                 (new_id, dst_url, cid))
    _move_touches(cid, new_id, src_url, conn)
    conn.execute("UPDATE sequences SET contact_id = ? WHERE contact_id = ?", (new_id, cid))
    _move_interactions(cid, new_id, dst_url, conn)
    # `transcript_contacts` is created by migration 004 and `interactions` lazily on first use,
    # so on a database where nobody has ever had a booking detected or pasted a transcript the
    # table genuinely is not there. Missing is a normal state, not an error — `delete_contact`
    # takes the same care for the same reason. Caught NARROWLY: swallowing every exception here
    # would hide a real failure in the middle of a transaction.
    try:
        conn.execute("UPDATE transcript_contacts SET contact_id = ? WHERE contact_id = ?",
                     (new_id, cid))
    except sqlite3.OperationalError:
        pass
    return step


def _has_outreach(c: dict) -> bool:
    return bool((c.get("sent_message_id") or "").strip()
                or (c.get("submitted_at") or "").strip()
                or (c.get("sms_sent_at") or "").strip())


def _move_touches(cid: str, new_id: str, src_url: str, conn: sqlite3.Connection) -> None:
    """Re-key the ladder, stamping each touch with the application it was part of.

    `touches.id` is `sha1(contact|channel|seq)`, so it has to be RECOMPUTED rather than carried:
    left alone, the next `_upsert_touch` for that contact derives an id that matches no row,
    inserts, and hits the unique index on (contact_id, channel, seq).
    """
    rows = conn.execute(
        "SELECT id, channel, seq, COALESCE(job_url,'') AS job_url FROM touches "
        "WHERE contact_id = ?", (cid,)).fetchall()
    for r in rows:
        conn.execute(
            "UPDATE touches SET id = ?, contact_id = ?, job_url = ? WHERE id = ?",
            (_touches.touch_id(new_id, r["channel"], r["seq"]), new_id,
             r["job_url"] or src_url, r["id"]))


def _move_interactions(cid: str, new_id: str, dst_url: str, conn: sqlite3.Connection) -> None:
    """Same recompute, same reason: the id is `sha256(contact|kind|at)` and `record()` upserts
    on it, so a stale id turns one re-detected booking into a second row."""
    try:
        rows = conn.execute("SELECT id, kind, COALESCE(at,'') AS at FROM interactions "
                            "WHERE contact_id = ?", (cid,)).fetchall()
    except sqlite3.OperationalError:
        return          # nothing has ever been recorded, so the table does not exist yet
    for r in rows:
        conn.execute(
            "UPDATE interactions SET id = ?, contact_id = ?, job_url = ? WHERE id = ?",
            (_inter._make_id(new_id, r["kind"], r["at"]), new_id, dst_url, r["id"]))


def _snapshot(cid: str, conn: sqlite3.Connection) -> dict:
    """Everything about one contact, for undo. Taken BEFORE the delete, obviously."""
    row = conn.execute("SELECT * FROM contacts WHERE id = ?", (cid,)).fetchone()
    out = {"contact": dict(zip(row.keys(), row)) if row else None,
           "messages": [], "touches": [], "sequences": [], "interactions": [],
           "transcripts": []}
    for table in ("messages", "touches", "sequences", "interactions"):
        try:
            rows = conn.execute(f"SELECT * FROM {table} WHERE contact_id = ?", (cid,)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        out[table] = [dict(zip(r.keys(), r)) for r in rows]
    try:
        rows = conn.execute("SELECT * FROM transcript_contacts WHERE contact_id = ?",
                            (cid,)).fetchall()
        out["transcripts"] = [dict(zip(r.keys(), r)) for r in rows]
    except sqlite3.OperationalError:
        pass
    return out


# ── undo ────────────────────────────────────────────────────────────────────

def undo(token: str, conn: sqlite3.Connection | None = None) -> dict:
    """Put everything back. Ships with the first cut, because it is what makes the button safe.

    Cheap for exactly the reason the move is non-destructive: reversing it is the same five
    UPDATEs with the ids swapped, plus re-inserting the two things that were destroyed.
    """
    if conn is None:
        conn = get_connection()
    record = _UNDO.pop(token, None)
    if not record:
        return {"ok": False, "error": "that move can no longer be undone"}
    src = record["src"]
    try:
        for step in reversed(record["moves"]):
            old, new = step["old_id"], step["new_id"]
            sets = {"id": old, "job_url": src, "outreach_job_url": "", "updated_at": _now()}
            if step.get("cleared"):
                sets.update(step["cleared"])
            conn.execute(
                f"UPDATE contacts SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                (*sets.values(), new))
            conn.execute("UPDATE messages SET contact_id = ?, job_url = ? WHERE contact_id = ?",
                         (old, src, new))
            _unmove_touches(new, old, conn)
            conn.execute("UPDATE sequences SET contact_id = ? WHERE contact_id = ?", (old, new))
            _move_interactions(new, old, src, conn)
            try:
                conn.execute("UPDATE transcript_contacts SET contact_id = ? "
                             "WHERE contact_id = ?", (old, new))
            except sqlite3.OperationalError:
                pass
            if step.get("deleted"):
                _restore(step["deleted"], conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"ok": True, "error": "", "restored": len(record["moves"])}


def _unmove_touches(new_id: str, old_id: str, conn: sqlite3.Connection) -> None:
    """The reverse of `_move_touches`, and it clears the stamp it wrote.

    The stamp is cleared rather than restored to its old value because the only value it can
    have had is empty: a touch already stamped with another job belongs to a contact who was
    moved twice, and `_move_touches` preserves that one untouched.
    """
    rows = conn.execute(
        "SELECT id, channel, seq, COALESCE(job_url,'') AS job_url FROM touches "
        "WHERE contact_id = ?", (new_id,)).fetchall()
    for r in rows:
        conn.execute("UPDATE touches SET id = ?, contact_id = ?, job_url = '' WHERE id = ?",
                     (_touches.touch_id(old_id, r["channel"], r["seq"]), old_id, r["id"]))


def _restore(snap: dict, conn: sqlite3.Connection) -> None:
    for table, rows in (("contacts", [snap["contact"]] if snap.get("contact") else []),
                        ("messages", snap.get("messages") or []),
                        ("touches", snap.get("touches") or []),
                        ("sequences", snap.get("sequences") or []),
                        ("interactions", snap.get("interactions") or []),
                        ("transcript_contacts", snap.get("transcripts") or [])):
        for row in rows:
            cols = ", ".join(row.keys())
            marks = ", ".join("?" for _ in row)
            conn.execute(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({marks})",
                         tuple(row.values()))


def sources_for(dst_url: str, conn: sqlite3.Connection | None = None) -> list[dict]:
    """Other roles at this employer that HAVE contacts — the ones worth pulling from.

    The mirror of `targets_for`, and it exists because of where the operator is standing. The
    duplicate is noticed on the LIVE role ("2 of these are already on another role here"), and
    `targets_for` only offers a move outward from the dead one — so acting on what you just read
    meant navigating to a different card to find the button. §Lessons 89: findable is not the
    same as findable FROM WHERE THE WORK IS.

    Closed roles are INCLUDED here, unlike `targets_for` which excludes them. That is the point:
    a cancelled role is exactly the thing you pull people off.
    """
    if conn is None:
        conn = get_connection()
    from applypilot.repo import jobs as _jobs
    dst_job = _jobs.find_by_any_url(dst_url, conn)
    if not dst_job:
        return []
    mine = _employer(dst_job)
    if not mine:
        return []
    out = []
    for row in _jobs.dashboard_rows(conn=conn, space_id=dst_job.get("space_id")):
        job = dict(row)
        if job.get("url") == dst_url:
            continue
        if not companies_match(mine, _employer(job)):
            continue
        n = len(store.get_contacts_for_job(job["url"], conn))
        if n:
            out.append({"url": job.get("url") or "", "title": job.get("title") or "",
                        "people": n, "closed": bool((job.get("rejected_at") or "").strip())})
    return out


def targets_for(src_url: str, conn: sqlite3.Connection | None = None) -> list[dict]:
    """Which applications this job's contacts could move TO — same employer, still open.

    Offered rather than guessed even when there is only one, because "move sixteen people" is
    not an action to perform on an inference about which role the operator meant.
    """
    if conn is None:
        conn = get_connection()
    from applypilot.repo import jobs as _jobs
    src_job = _jobs.find_by_any_url(src_url, conn)
    if not src_job:
        return []
    mine = _employer(src_job)
    if not mine:
        return []
    out = []
    # Raw `sqlite3.Row`s, so this converts rather than calling `.get` on them — the shape of
    # §Lessons 18, where `dict(zip(row.keys(), row))` on something that was already a dict
    # mapped every key to itself and searched Apollo for a company called "company".
    for row in _jobs.dashboard_rows(conn=conn, space_id=src_job.get("space_id")):
        job = dict(row)
        if job.get("url") == src_url or (job.get("rejected_at") or "").strip():
            continue
        if companies_match(mine, _employer(job)):
            out.append({"url": job.get("url") or "", "title": job.get("title") or "",
                        "company": job.get("company") or ""})
    return out

"""Move a card (and everything hanging off it) from one Space to another.

`plan()` → `apply()` → `undo()`, one transaction, mirroring `networking/migrate.py`, which
solved the analogous problem for contacts.

**The move is two columns.** Only `jobs` and `contacts` carry `space_id`; touches, messages,
sequences, interactions, reply_queue and transcripts are keyed on the contact and the anchor,
and the anchor does not move. So nothing is re-keyed and nothing orphans — see
`domain/spacemove` for why rewriting the anchor would be the destructive option rather than the
tidy one.

The one thing that IS destroyed is an unsent draft written in the old Space's voice, and only
when the voice actually differs. It is snapshotted for undo first.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from applypilot.database import get_connection
from applypilot.domain import spacemove as _rules
from applypilot.repo import spaces as _spaces

#: In memory, like `migrate._UNDO`: undo is available until the page is reloaded. A table would
#: outlive the decision it exists to reverse.
_UNDO: dict[str, dict] = {}

#: The draft columns an unsent card carries. Cleared together or not at all — a half-cleared
#: draft is a subject with no body, which renders as a real draft and sends as nonsense.
_DRAFT_COLS = ("outreach_subject", "outreach_message", "outreach_status", "draft_variant",
               "linkedin_message")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _c(conn: sqlite3.Connection | None) -> sqlite3.Connection:
    return conn if conn is not None else get_connection()


def destinations(url: str, conn: sqlite3.Connection | None = None) -> list[dict]:
    """Spaces this card could move to — SAME SHAPE only, never itself.

    Filtered here rather than in the browser so the menu cannot offer a move that `plan` will
    then refuse. A guard that exists only on the path the UI happens to take is not a guard
    (§Lessons 110), so `plan` re-checks anyway.
    """
    conn = _c(conn)
    from applypilot.repo import jobs as _jobs
    job = _jobs.find_by_any_url(url, conn)
    if not job:
        return []
    src = _spaces.load(job.get("space_id") or _spaces.DEFAULT_SPACE_ID, conn)
    if src is None:
        return []
    out = []
    for s in _spaces.load_all(conn=conn):
        if s.id != src.id and s.shape == src.shape:
            out.append({"id": s.id, "name": s.name})
    return out


def plan(url: str, dst_space_id: str, conn: sqlite3.Connection | None = None) -> dict:
    """What this move would do, stated before anything happens.

    Everything the operator needs to decide: what changes, how many people come along, whether
    any of it has already been sent, and what would be discarded.
    """
    conn = _c(conn)
    from applypilot.networking import store as _store
    from applypilot.repo import jobs as _jobs

    job = _jobs.find_by_any_url(url, conn)
    if not job:
        return {"ok": False, "error": "card not found"}
    src = _spaces.load(job.get("space_id") or _spaces.DEFAULT_SPACE_ID, conn)
    dst = _spaces.load(dst_space_id, conn)

    contacts = _store.get_contacts_for_job(job["url"], conn)
    sent = [c for c in contacts if _has_sent(c)]
    why = _rules.refusal(src, dst, has_sent=bool(sent))
    if why:
        return {"ok": False, "error": why,
                "card": job.get("title") or job.get("company") or job["url"]}

    stale = _rules.drafts_are_stale(src, dst)
    drafts = [c for c in contacts if _has_unsent_draft(c)] if stale else []
    return {
        "ok": True, "error": "",
        "url": job["url"],
        "card": job.get("title") or job.get("company") or job["url"],
        "src": {"id": src.id, "name": src.name},
        "dst": {"id": dst.id, "name": dst.name},
        "contacts": len(contacts),
        "sent": len(sent),
        "changes": _rules.differences(src, dst),
        "ladder_warning": _rules.ladder_warning(src, dst),
        # Named, not counted: "2 drafts will be discarded" is a number, and the operator needs
        # to know WHOSE words are about to go.
        "discards": [(c.get("full_name") or c.get("email") or c.get("id")) for c in drafts],
    }


def _has_sent(c: dict) -> bool:
    return bool((c.get("sent_message_id") or "").strip()
                or (c.get("outreach_status") or "") == "submitted")


def _has_unsent_draft(c: dict) -> bool:
    if _has_sent(c):
        return False
    return bool((c.get("outreach_message") or "").strip()
                or (c.get("linkedin_message") or "").strip())


def apply(url: str, dst_space_id: str, conn: sqlite3.Connection | None = None) -> dict:
    """Do it. Returns {ok, undo, ...}; `undo` is the token."""
    conn = _c(conn)
    from applypilot.networking import store as _store
    from applypilot.repo import jobs as _jobs

    preview = plan(url, dst_space_id, conn)
    if not preview["ok"]:
        return preview

    job = _jobs.find_by_any_url(url, conn)
    contacts = _store.get_contacts_for_job(job["url"], conn)
    backup = _backup()
    record = {"url": job["url"], "at": _now(),
              "job_space": job.get("space_id"),
              "contacts": [{"id": c.get("id"), "space_id": c.get("space_id")} for c in contacts],
              "drafts": []}
    discard = {(c.get("full_name") or c.get("email") or c.get("id")) for c in
               [x for x in contacts if _has_unsent_draft(x)]} if preview["discards"] else set()
    try:
        # No SQL here: `jobs` writes belong to `repo.jobs` and `contacts` writes to
        # `networking.store`, one place per table (ARCH-4). This module decides WHAT happens and
        # in what order; the repositories know how.
        _jobs.set_space(job["url"], dst_space_id, conn)
        # The mirror is REPAIRED rather than carried across: live, one contact sat on a
        # `professional-network` anchor with `space_id='job-search'`, so preserving the old
        # value per row would preserve a bug.
        _store.set_space_for_job(job["url"], dst_space_id, conn)
        for c in contacts:
            name = c.get("full_name") or c.get("email") or c.get("id")
            if name in discard:
                before = _store.clear_unsent_draft(c.get("id"), conn)
                record["drafts"].append({"id": c.get("id"), **before})
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    token = uuid4().hex[:12]
    _UNDO[token] = record
    _log(job["url"], preview, conn)
    return {"ok": True, "error": "", "undo": token, "backup": backup,
            "card": preview["card"], "src": preview["src"], "dst": preview["dst"],
            "contacts": preview["contacts"], "discarded": len(record["drafts"])}


def _log(url: str, preview: dict, conn: sqlite3.Connection) -> None:
    """Best-effort, like every other event append."""
    try:
        from applypilot.database import log_event
        note = f"Moved from {preview['src']['name']} to {preview['dst']['name']}."
        if preview["discards"]:
            note += f" Discarded {len(preview['discards'])} unsent draft(s) written in the old voice."
        log_event(url, "system", "ok", note, conn)
    except Exception:  # noqa: BLE001
        pass


def undo(token: str, conn: sqlite3.Connection | None = None) -> dict:
    """Put the card, its contacts and any discarded draft back."""
    conn = _c(conn)
    rec = _UNDO.get(token)
    if not rec:
        return {"ok": False, "error": "that move is no longer undoable"}
    from applypilot.networking import store as _store
    from applypilot.repo import jobs as _jobs
    try:
        _jobs.set_space(rec["url"], rec["job_space"], conn)
        # Per CONTACT on the way back, because the card's contacts may not all have started in
        # the same Space — the drift this repairs on the way out is real data.
        for c in rec["contacts"]:
            _store.set_contact_space(c["id"], c["space_id"], conn)
        for d in rec["drafts"]:
            _store.restore_draft(d["id"], {k: v for k, v in d.items() if k != "id"}, conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    _UNDO.pop(token, None)
    return {"ok": True, "error": "", "url": rec["url"], "restored": len(rec["contacts"])}


def _backup() -> str:
    """sqlite's own backup API, never `cp` — a file copy misses everything still in the -wal."""
    from pathlib import Path

    from applypilot import config
    try:
        src = Path(config.DB_PATH)
        dest_dir = src.parent / "backups"
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        dest = dest_dir / f"applypilot-{stamp}-pre-cardmove.db"
        con, out = sqlite3.connect(str(src)), sqlite3.connect(str(dest))
        try:
            con.backup(out)
        finally:
            out.close()
            con.close()
        return dest.name
    except Exception:
        return ""

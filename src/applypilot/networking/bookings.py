"""Detect a booked call from the scheduler's own confirmation email.

Every outreach message offers a scheduling link, and until now a booking was invisible to the
CRM — the strongest signal a contact can produce (someone giving you time, not just words) had
no representation at all.

The link goes to cal.com, which we do not control and cannot read. But cal.com **emails the
host on every booking**, and that email is in the mailbox we already have permission to search.
Verified against the real account before this was written:

    from=hello@cal.com   subject="30 Min Meeting between Andrew Shindyapin and Alejandro Diez"

So the detector is a Gmail search, not an integration: no API key, no webhook, no third-party
account, and it works for Calendly and Google appointment schedules the same way.

Requires `gmail.readonly` for `q=` search — metadata refuses it. Reads headers only; the
attendee is identified from the confirmation's participants, never from a message body.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

#: Senders that confirm a booking. Matched on the sender's DOMAIN as a whole label, never as a
#: substring — §Lessons 1, and "cal.com" as a substring matches "notcal.com".
_SCHEDULERS = ("cal.com", "calendly.com", "savvycal.com", "hubspot.com", "zcal.co")

#: A confirmation, not a marketing email from the same sender. cal.com sends both.
_CONFIRMS = re.compile(
    r"\b(meeting between|booking confirmed|new event|invitation:|scheduled|"
    r"confirmed:|has been scheduled|booked)\b", re.IGNORECASE)

#: A reminder is the SAME booking seen again. Recording it as a separate interaction would
#: report one call as two, and the reminder arrives closer to the meeting so it would also
#: become the "latest" engagement.
_REMINDER = re.compile(r"^\s*(reminder|upcoming)\b[: ]", re.IGNORECASE)


def _is_scheduler(sender: str) -> bool:
    from applypilot.domain.conversations import addr
    host = addr(sender).rsplit("@", 1)[-1]
    labels = host.split(".")
    return any(s == host or host.endswith("." + s) or
               ".".join(labels[-len(s.split(".")):]) == s for s in _SCHEDULERS)


def find_for_contacts(contacts: list[dict], limit: int = 25) -> list[dict]:
    """Bookings that involve one of these contacts. Returns [{contact_id, at, detail}].

    Matches by the contact's email appearing on the confirmation, which is what makes this
    attributable at all — a scheduler confirmation names both parties.
    """
    from applypilot.networking import gmail_read
    ok, why = gmail_read.can_read_content()
    if not ok:
        return []

    by_email = {(c.get("email") or "").strip().lower(): c
                for c in (contacts or []) if (c.get("email") or "").strip()}
    if not by_email:
        return []

    from applypilot.domain import conversations as cv
    query = " OR ".join(f"from:{s}" for s in _SCHEDULERS)
    out: list[dict] = []
    for tid in gmail_read.search_threads(query, limit=limit):
        for m in gmail_read.thread_messages(tid):
            sender, subject = m.get("from", ""), (m.get("subject") or "")
            if not _is_scheduler(sender) or not _CONFIRMS.search(subject):
                continue
            if _REMINDER.match(subject):
                continue
            # Whose booking? Everyone the confirmation was addressed to, minus us.
            parties = {cv.addr(x) for x in
                       cv.split_parts(m.get("to")) + cv.split_parts(m.get("cc"))}
            parties.add(cv.addr(sender))
            for email, contact in by_email.items():
                if email in parties:
                    out.append({"contact_id": contact.get("id"),
                                "job_url": contact.get("job_url"),
                                "at": _iso(m.get("internalDate", "")),
                                "detail": subject[:120]})
    return out


def _iso(ms: str) -> str:
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (ValueError, TypeError):
        return ""


def poll(conn=None) -> dict:
    """Find and record bookings. Idempotent; safe on the hourly tick."""
    from applypilot.domain.interactions import BOOKED
    from applypilot.database import log_event
    from applypilot.networking import interactions_store, store

    from applypilot.networking import gmail_read
    ok, why = gmail_read.can_read_content()
    if not ok:
        return {"ok": False, "note": why, "found": 0, "new": 0}

    if conn is None:
        from applypilot.database import get_connection
        conn = get_connection()
    contacts = [store.get_contact(c["id"], conn)
                for c in store.all_contacts_for_metrics(conn)]
    contacts = [c for c in contacts if c]

    hits = find_for_contacts(contacts)
    by_id = {c["id"]: c for c in contacts}
    new = []
    for h in hits:
        if interactions_store.record(h["contact_id"], BOOKED, at=h["at"],
                                     detail=h["detail"], source="detected",
                                     job_url=h.get("job_url") or "", conn=conn):
            who = (by_id.get(h["contact_id"]) or {}).get("full_name") or h["contact_id"]
            new.append(who)
            log_event(h.get("job_url") or "", "outreach", "ok",
                      f"{who} booked a call — {h['detail']}", conn)
    return {"ok": True, "found": len(hits), "new": len(new), "names": new,
            "note": (f"{len(new)} new booking(s): {', '.join(new)}" if new
                     else f"{len(hits)} booking(s), none new")}

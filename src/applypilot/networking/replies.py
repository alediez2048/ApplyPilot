"""Poll Gmail, match inbound mail to contacts, halt the ladder for anyone who replied.

The orchestration layer for CRM-1: `gmail_read` fetches, `domain.replies` decides, this
persists. Keeping the decision in `domain/` is what makes the hard part testable without a
mailbox.

Why this matters more than it looks: at the time it was written the live DB held **33 sent
emails and exactly one recorded reply — typed in by hand.** Every follow-up the system schedules
is potentially nudging someone who already answered, which is worse than not following up at
all.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from applypilot.database import get_connection, log_event
from applypilot.domain import replies as domain_replies
from applypilot.networking import gmail_oauth, gmail_read, store, touches

log = logging.getLogger(__name__)


def _iso(ms: str) -> str:
    """Gmail's internalDate (ms since epoch, as a string) -> ISO 8601."""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return datetime.now(timezone.utc).isoformat()


def mark_replied(contact: dict, replied_at: str, conn=None) -> None:
    """Record the reply and STOP that contact's email ladder.

    The halt goes through `sequences` (ARCH-3), not a column on `contacts` — `followup_status`
    was removed and must not come back. The LinkedIn ladder is deliberately left running: it is
    a different thread and a different conversation, and silently stopping it would hide a
    channel the operator is still working.
    """
    conn = conn or get_connection()
    cid = contact["id"]
    store.upsert_contact({"id": cid, "job_url": contact["job_url"], "replied_at": replied_at}, conn)
    touches.set_sequence_status(cid, "email", "replied", note="inbound reply detected", conn=conn)
    when = (replied_at or "")[:10] or "recently"
    log_event(contact["job_url"], "outreach", "ok",
              f"{contact.get('full_name') or 'A contact'} replied on {when} — "
              f"email follow-ups stopped.", conn)
    log.info("Reply detected from %s (%s)", contact.get("full_name"), contact.get("email"))


def mark_bounced(contact: dict, when: str, conn=None) -> None:
    """Record that mail to this address is being REJECTED, and stop retrying it.

    Not a reply. The ladder stops for a different reason and says so, the email is flagged so
    the UI stops presenting it as reachable, and the operator finds out — an address that
    bounces will bounce for every follow-up, and silently retrying it is how outreach to a
    company fails for weeks without anyone noticing.
    """
    conn = conn or get_connection()
    cid = contact["id"]
    store.upsert_contact({"id": cid, "job_url": contact["job_url"],
                          "email_status": "bounced",
                          "send_error": "delivery failed (bounced)"}, conn)
    touches.set_sequence_status(cid, "email", "stopped", note="email bounced", conn=conn)
    log_event(contact["job_url"], "outreach", "warn",
              f"Email to {contact.get('full_name') or 'a contact'} BOUNCED "
              f"({contact.get('email') or 'no address'}) — follow-ups stopped. "
              f"The address is wrong or dead.", conn)
    log.info("Bounce detected for %s (%s)", contact.get("full_name"), contact.get("email"))


def poll(conn=None, force_full: bool = False) -> dict:
    """One incremental poll. Safe to run repeatedly; returns a summary for the caller to log.

    Only threads we started are ever read — the watermark narrows to threads with new mail, and
    the contact list narrows to conversations we own. A poll with no new mail costs one
    `history.list` call and reads no threads at all.
    """
    ok, why = gmail_read.available()
    if not ok:
        return {"ok": False, "note": why, "checked": 0, "replied": 0}

    conn = conn or get_connection()
    contacts = store.contacts_awaiting_reply(conn)
    if not contacts:
        gmail_read.save_watermark(checked_at=datetime.now(timezone.utc).isoformat())
        return {"ok": True, "note": "no awaiting-reply contacts", "checked": 0, "replied": 0}

    service = gmail_read._service()
    if service is None:
        return {"ok": False, "note": "Gmail client unavailable", "checked": 0, "replied": 0}

    watermark = gmail_read.load_watermark().get("history_id") or ""
    active: set[str] | None = None
    if watermark and not force_full:
        touched = gmail_read.threads_with_activity(watermark, service)
        # An EMPTY set means "unknown", not "nothing happened" — Gmail expires history ids after
        # roughly a week and returns an error, and treating that as quiet would stop reply
        # detection forever on an idle mailbox. Only a NON-empty result may narrow the work.
        if touched:
            active = touched

    me = gmail_oauth.connected_email()
    checked = 0
    found: list[dict] = []
    bounced: list[dict] = []
    for c in contacts:
        tid = (c.get("thread_id") or "").strip()
        if not tid:
            continue
        if active is not None and tid not in active:
            continue
        checked += 1
        msgs = gmail_read.thread_messages(tid, service)
        found.extend(domain_replies.replies_in(msgs, [c], me))
        bounced.extend(domain_replies.bounces_in(msgs, [c], me))

    for hit in found:
        mark_replied(hit["contact"], _iso(hit["message"].get("internalDate", "")), conn)
    for hit in bounced:
        mark_bounced(hit["contact"], _iso(hit["message"].get("internalDate", "")), conn)

    gmail_read.save_watermark(history_id=gmail_read.current_history_id(service),
                              checked_at=datetime.now(timezone.utc).isoformat())
    return {"ok": True, "note": "ok", "checked": checked,
            "replied": len(found), "bounced": len(bounced),
            "names": [h["contact"].get("full_name") for h in found],
            "bounced_names": [h["contact"].get("full_name") for h in bounced]}

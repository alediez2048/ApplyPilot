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
from applypilot.networking import gmail_oauth, gmail_read, messages as msg_store
from applypilot.networking import store, touches

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


def _sync_thread(contact: dict, msgs: list[dict], me: str, conn) -> dict:
    """Persist the conversation and report anyone the OTHER side introduced (CRM-4).

    Stored per message so re-syncing is a no-op — `tick` re-reads every open thread hourly.
    """
    from applypilot.domain import conversations as cv

    rows = []
    for m in cv.timeline(msgs, me):
        rows.append({"message_id": m["id"], "thread_id": contact.get("thread_id"),
                     "contact_id": contact["id"], "job_url": contact["job_url"],
                     "direction": m["direction"], "from_addr": m["from_addr"],
                     "from_name": m["from_name"], "to_addrs": m["to_addrs"],
                     "cc_addrs": m["cc_addrs"], "subject": m["subject"],
                     "sent_at": _iso(m["at"]),
                     "rfc_message_id": m.get("rfc_message_id") or ""})
    new = msg_store.upsert_messages(rows, conn)
    intro = cv.introductions(msgs, me, known=[contact.get("email")])
    return {"new_messages": new, "introductions": intro}


def note_introductions(contact: dict, intros: list[dict], conn) -> None:
    """Surface a handoff. Deliberately does NOT create the contact.

    Threads collect schedulers, assistants and ATS robots, and an auto-created contact is one an
    automated ladder will then email. `is_robot()` filters the obvious ones, but "obvious" is
    not "all" — so this reports, and the operator confirms.
    """
    for person in intros:
        who = person.get("name") or person["email"]
        by = person.get("introduced_by_name") or person.get("introduced_by") or "someone"
        log_event(contact["job_url"], "outreach", "ok",
                  f"{by} introduced {who} ({person['email']}) on the thread — "
                  f"they may be the person actually handling this now.", conn)
        log.info("Introduction on %s: %s -> %s", contact["job_url"], by, person["email"])


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
    # Read every live thread, not just the silent ones — a replied contact's thread is
    # the one still moving. Reply/bounce MARKING is still gated below, so re-reading an
    # answered thread cannot overwrite `replied_at` or re-log anything.
    contacts = store.contacts_with_threads(conn)
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
    intros: list[dict] = []
    for c in contacts:
        tid = (c.get("thread_id") or "").strip()
        if not tid:
            continue
        if active is not None and tid not in active:
            continue
        checked += 1
        msgs = gmail_read.thread_messages(tid, service)
        if not (c.get("replied_at") or "").strip():
            # Already-answered contacts are re-read for conversation memory but never re-marked:
            # re-marking would push `replied_at` forward to a later message in the thread and
            # lose when the conversation actually turned.
            found.extend(domain_replies.replies_in(msgs, [c], me))
            bounced.extend(domain_replies.bounces_in(msgs, [c], me))
        try:
            synced = _sync_thread(c, msgs, me, conn)
            if synced["introductions"]:
                # Only announce an introduction the FIRST time the message carrying it is seen,
                # or an hourly tick would re-log the same handoff forever — the exact duplicate
                # -logging bug bounces already hit.
                if synced["new_messages"]:
                    note_introductions(c, synced["introductions"], conn)
                intros.extend(synced["introductions"])
        except Exception:  # noqa: BLE001
            # Conversation memory is additive. It must never break reply detection.
            log.debug("Thread sync failed for %s", c.get("full_name"), exc_info=True)

    for hit in found:
        mark_replied(hit["contact"], _iso(hit["message"].get("internalDate", "")), conn)
    for hit in bounced:
        mark_bounced(hit["contact"], _iso(hit["message"].get("internalDate", "")), conn)

    gmail_read.save_watermark(history_id=gmail_read.current_history_id(service),
                              checked_at=datetime.now(timezone.utc).isoformat())
    return {"ok": True, "note": "ok", "checked": checked,
            "replied": len(found), "bounced": len(bounced),
            "introduced": len(intros),
            "names": [h["contact"].get("full_name") for h in found],
            "bounced_names": [h["contact"].get("full_name") for h in bounced],
            "introduced_names": [i.get("name") or i.get("email") for i in intros]}

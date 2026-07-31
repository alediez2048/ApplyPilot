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
from applypilot.domain import conversations as cv, replies as domain_replies
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

    # CRM-4b: the automatic poll stores NO message text, ever, even when `gmail.readonly` is
    # granted. Content arrives only when the operator asks for one conversation by name
    # (`fetch_thread_text`) or pastes it. The OAuth grant is all-or-nothing — Google has no
    # per-thread scope — so this is the only place the narrowing can actually be expressed:
    # not in what we are ALLOWED to read, but in what we ever DO read.
    rows = []
    for m in cv.timeline(msgs, me):
        rows.append({"message_id": m["id"], "thread_id": contact.get("thread_id"),
                     "contact_id": contact["id"], "job_url": contact["job_url"],
                     "direction": m["direction"], "from_addr": m["from_addr"],
                     "from_name": m["from_name"], "to_addrs": m["to_addrs"],
                     "cc_addrs": m["cc_addrs"], "subject": m["subject"],
                     "sent_at": _iso(m["at"]),
                     "rfc_message_id": m.get("rfc_message_id") or "",
                     # Never on the automatic path. `upsert_messages` preserves an existing
                     # snippet when handed an empty one, so a poll cannot erase text the
                     # operator fetched or pasted earlier.
                     "snippet": ""})
    new = msg_store.upsert_messages(rows, conn)
    intro = cv.introductions(msgs, me, known=[contact.get("email")])
    return {"new_messages": new, "introductions": intro}


def sync_all_with(contact: dict, conn=None, limit: int = 25) -> dict:
    """Pull EVERY Gmail conversation with this person, not just the one ApplyPilot started.

    Until now the system could only see threads it had sent itself: `thread_id` is captured at
    send time, and everything downstream looks a thread up by that id. So a conversation the
    other side began, an email sent straight from Gmail, or a thread where somebody CC'd you was
    invisible — the CRM's memory stopped at its own outbox.

    `gmail.readonly` lifts that, because it permits `q=` search (metadata does not, at all). We
    search on their ADDRESS rather than a thread id, so every exchange with them lands, whoever
    started it and wherever it was sent from.

    Inbound message text is stored; ours is left alone here (the send path records what we typed
    at the moment we typed it). Everything is keyed by Gmail's message id, so this is idempotent
    and can be run as often as the operator likes.
    """
    ok, why = gmail_read.can_read_content()
    if not ok:
        return {"ok": False, "message": why, "threads": 0, "messages": 0}
    email = (contact.get("email") or "").strip()
    if not email:
        return {"ok": False, "message": "no email address for this contact",
                "threads": 0, "messages": 0}

    me = gmail_oauth.connected_email()
    # Both directions, and `from:`/`to:` alone would miss a thread where they were only CC'd —
    # which is exactly the Writer case that prompted this.
    thread_ids = gmail_read.search_threads(f"from:{email} OR to:{email} OR cc:{email}",
                                           limit=limit)
    if not thread_ids:
        return {"ok": True, "threads": 0, "messages": 0,
                "message": f"no Gmail conversations found with {email}"}

    # What we already hold for this contact, so our own full sent text is never downgraded to
    # Gmail's truncated snippet — but a message sent from Gmail directly, which we have no text
    # for at all, still gets filled in.
    have = {m.get("message_id"): (m.get("snippet") or "")
            for m in msg_store.thread_for_contact(contact["id"], conn)}

    total_new = 0
    for tid in thread_ids:
        msgs = gmail_read.thread_messages(tid)
        if not msgs:
            continue
        rows = []
        for m in cv.timeline(msgs, me):
            raw = next((x for x in msgs if x.get("id") == m["id"]), {})
            inbound = m["direction"] == "in"
            rows.append({
                "message_id": m["id"], "thread_id": tid,
                "contact_id": contact["id"], "job_url": contact.get("job_url"),
                "direction": m["direction"], "from_addr": m["from_addr"],
                "from_name": m["from_name"], "to_addrs": m["to_addrs"],
                "cc_addrs": m["cc_addrs"], "subject": m["subject"],
                "sent_at": _iso(m["at"]), "rfc_message_id": m.get("rfc_message_id") or "",
                # Inbound: take Gmail's text. Outbound: only when we hold NOTHING for it —
                # a reply ApplyPilot sent was recorded in full at send time and must not be
                # downgraded to a truncated snippet, but a message sent straight from Gmail
                # has no stored text at all and would otherwise render as a blank row forever.
                "snippet": (cv.strip_quoted_tail(raw.get("snippet")) if inbound
                            else ("" if (have.get(m["id"]) or "").strip()
                                  else cv.strip_quoted_tail(raw.get("snippet")))),
            })
        total_new += msg_store.upsert_messages(rows, conn)

    return {"ok": True, "threads": len(thread_ids), "messages": total_new,
            "message": (f"Found {len(thread_ids)} conversation(s) with {email}"
                        f"{f', {total_new} new message(s)' if total_new else ' — nothing new'}.")}


def fetch_thread_text(contact: dict, conn=None) -> dict:
    """Read ONE conversation's text, because the operator asked for this one.

    The scope is all-or-nothing, so this cannot narrow what we are permitted to read. What it
    narrows is what we ever actually read: one thread, on a click, instead of every open thread
    on every poll forever. That distinction is worth having even though the grant is identical —
    it is the difference between a tool that can read your mail and a tool that is reading it.

    Inbound messages only. Our own sent text is already ours.
    """
    ok, why = gmail_read.can_read_content()
    if not ok:
        return {"ok": False, "message": why, "stored": 0}
    thread_id = (contact.get("thread_id") or "").strip()
    if not thread_id:
        return {"ok": False, "message": "no Gmail thread recorded for this contact", "stored": 0}

    msgs = gmail_read.thread_messages(thread_id)
    if not msgs:
        return {"ok": False, "message": "Gmail returned nothing for this thread", "stored": 0}

    me = gmail_oauth.connected_email()
    rows, stored = [], 0
    for m in msgs:
        # Trim the quoted original: Gmail's snippet runs through the quote header, so a short
        # reply can be a third our own email quoted back — and that would reach the drafter as
        # something they wrote.
        text = cv.strip_quoted_tail(m.get("snippet"))
        if not text or cv.addr(m.get("from")) == cv.addr(me):
            continue
        rows.append({"message_id": m.get("id"), "thread_id": thread_id,
                     "contact_id": contact["id"], "job_url": contact.get("job_url"),
                     "direction": "in", "from_addr": cv.addr(m.get("from")),
                     "from_name": cv.display_name(m.get("from")),
                     "to_addrs": cv.split_parts(m.get("to")),
                     "cc_addrs": cv.split_parts(m.get("cc")),
                     "subject": m.get("subject") or "", "sent_at": _iso(m.get("internalDate", "")),
                     "rfc_message_id": m.get("rfc_message_id") or "", "snippet": text})
        stored += 1
    if not rows:
        return {"ok": False, "stored": 0,
                "message": "nothing readable in this thread — Gmail returned no text"}
    msg_store.upsert_messages(rows, conn)
    return {"ok": True, "stored": stored,
            "message": f"Read {stored} message{'s' if stored != 1 else ''} from this thread."}


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

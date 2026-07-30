"""Deciding whether an inbound message is a reply, and whose.

Pure functions: dicts in, verdict out. No `http`, no `sqlite3` — the `domain/` boundary is what
lets this be tested without a mailbox, which matters here more than anywhere else in the
codebase, because the alternative is testing against real mail.

Two questions, deliberately separated:

    is_inbound()   — is this message FROM someone else, or is it our own sent mail?
    match_contact() — which contact does it belong to?

The first is a safety check. Gmail returns our own sent messages in the same thread, and
counting one as a reply would mark every contact we ever emailed as having answered — the
follow-up ladder would stop for all of them and the funnel would read 100%.
"""

from __future__ import annotations

import re

#: Gmail label on our own outgoing mail.
SENT_LABEL = "SENT"

#: Senders that are mail INFRASTRUCTURE, never the human we wrote to.
_SYSTEM_SENDERS = ("mailer-daemon@", "postmaster@", "no-reply@", "noreply@", "donotreply@")

#: Subjects that mean "this never arrived". Matched case-insensitively as substrings because
#: every MTA words it differently.
_BOUNCE_SUBJECTS = (
    "undelivered mail returned to sender", "delivery status notification",
    "returned mail", "mail delivery failed", "delivery has failed",
    "undeliverable", "failure notice", "message not delivered",
)


def is_bounce(message: dict) -> bool:
    """True if this inbound message is a delivery failure or an automated response.

    Found the hard way on the first live poll: an Affirm contact was recorded as having
    REPLIED the same day she was emailed. The message was
    `Mail Delivery System <MAILER-DAEMON@…>` / "Undelivered Mail Returned to Sender" — the
    email never reached her. It arrived in our thread, so thread-id matching accepted it.

    Counting that as a reply is worse than missing a real one: it stops the ladder, records
    engagement that never happened, and would poison every conversion rate CRM-2 computes.
    """
    sender = _addr(message.get("from") or (message.get("headers") or {}).get("from"))
    if any(sender.startswith(p) for p in _SYSTEM_SENDERS):
        return True
    auto = str(message.get("auto_submitted") or
               (message.get("headers") or {}).get("auto-submitted") or "").lower()
    if auto and auto != "no":
        return True
    subject = str(message.get("subject") or "").lower()
    return any(pat in subject for pat in _BOUNCE_SUBJECTS)


def _addr(value: str | None) -> str:
    """Bare address out of a From header: 'Jo <jo@x.com>' -> 'jo@x.com'."""
    raw = (value or "").strip()
    m = re.search(r"<([^>]+)>", raw)
    if m:
        raw = m.group(1)
    return raw.strip().strip("<>").lower()


def is_inbound(message: dict, connected_email: str) -> bool:
    """True if this message came FROM someone else.

    Both signals are checked, because either alone has a hole: the SENT label is missing on
    messages synced from another client, and a From comparison alone fails for an alias or a
    send-as address. Requiring "not labelled SENT AND not from us" errs towards NOT calling
    something a reply — a missed reply costs one wasted follow-up, while a false one silently
    halts a live conversation.
    """
    labels = message.get("labelIds") or message.get("labels") or []
    if SENT_LABEL in labels:
        return False
    sender = _addr(message.get("from") or (message.get("headers") or {}).get("from"))
    me = _addr(connected_email)
    if not sender:
        return False
    return not (me and sender == me)


def match_contact(message: dict, contacts: list[dict]) -> dict | None:
    """Which contact this inbound message belongs to, or None.

    Three strategies, strongest first:

      1. `thread_id` — the message is literally in the thread we started. Unambiguous.
      2. `In-Reply-To` / `References` containing our `rfc_message_id`. Survives a thread id
         changing, and is how a reply forwarded into a new thread still matches.
      3. Sender address. Last resort and genuinely ambiguous: the same person may be a contact
         on several jobs, so this only matches when it is unique across ALL contacts. Guessing
         a job here would attach a reply to the wrong application.
    """
    thread_id = (message.get("thread_id") or "").strip()
    if thread_id:
        hits = [c for c in contacts if (c.get("thread_id") or "").strip() == thread_id]
        if len(hits) == 1:
            return hits[0]
        if hits:
            # Same thread on several contacts should be impossible (a thread is per-send), but
            # if it happens, the oldest send is the one that started it.
            return sorted(hits, key=lambda c: c.get("submitted_at") or "")[0]

    refs = " ".join(str(message.get(k) or "") for k in ("in_reply_to", "references"))
    if refs:
        for c in contacts:
            rfc = (c.get("rfc_message_id") or "").strip()
            if rfc and rfc in refs:
                return c

    sender = _addr(message.get("from") or (message.get("headers") or {}).get("from"))
    if sender:
        hits = [c for c in contacts if _addr(c.get("email")) == sender]
        if len(hits) == 1:
            return hits[0]

    return None


def replies_in(messages: list[dict], contacts: list[dict], connected_email: str) -> list[dict]:
    """[{contact, message}] for every inbound message that maps to a contact.

    One entry per CONTACT, keeping the earliest inbound — the first reply is when the
    conversation actually turned, and it is what `time_to_reply` (CRM-2) should measure. A
    later message in the same thread must not overwrite that timestamp.
    """
    out: dict[str, dict] = {}
    for msg in messages or []:
        if not is_inbound(msg, connected_email) or is_bounce(msg):
            continue
        contact = match_contact(msg, contacts)
        if not contact:
            continue
        cid = contact.get("id")
        if not cid:
            continue
        prev = out.get(cid)
        if prev is None or _ts(msg) < _ts(prev["message"]):
            out[cid] = {"contact": contact, "message": msg}
    return list(out.values())


def bounces_in(messages: list[dict], contacts: list[dict], connected_email: str) -> list[dict]:
    """[{contact, message}] for delivery failures — a dead address, not a reply.

    Reported separately rather than merely ignored: an address that bounces will bounce for
    every follow-up too, and silently retrying it is how outreach to a company fails for weeks
    without anyone noticing.
    """
    out: dict[str, dict] = {}
    for msg in messages or []:
        if not is_inbound(msg, connected_email) or not is_bounce(msg):
            continue
        contact = match_contact(msg, contacts)
        cid = (contact or {}).get("id")
        if cid and cid not in out:
            out[cid] = {"contact": contact, "message": msg}
    return list(out.values())


def _ts(message: dict) -> str:
    """Sort key. Gmail's internalDate is ms-since-epoch as a STRING, so compare numerically —
    lexicographic order breaks the moment the digit count changes."""
    raw = str(message.get("received_at") or message.get("internalDate") or "")
    return raw.zfill(20) if raw.isdigit() else raw

"""Read enough of Gmail to notice a reply. Headers only — never bodies.

Runs on `gmail.metadata`, deliberately NOT `gmail.readonly`. That scope grants headers, thread
ids and senders and CANNOT read a message body, which is everything needed here at a fraction
of the blast radius if the token leaks.

Two consequences of that choice, both load-bearing:

  * `q=` search is NOT permitted on metadata. Every "just search for unread from X" shortcut is
    unavailable — we list threads we already know about and look at who is in them.
  * There is no snippet. "They replied, on this date" is the whole payload, which is also all
    the UI needs and the least PII we can store.

The watermark is the reason this is cheap. Without it every poll re-reads every thread; with it
a poll that finds nothing new costs one `history.list` call.
"""

from __future__ import annotations

import json
import logging

from applypilot import config
from applypilot.networking import gmail_oauth

log = logging.getLogger(__name__)

WATERMARK_PATH = config.APP_DIR / "gmail_watermark.json"


def _service():
    """Gmail client, or None when unusable. Never raises — a missing scope is a normal state."""
    creds = gmail_oauth._load_creds()
    if creds is None or not gmail_oauth.has_scope(gmail_oauth.READ_SCOPE):
        return None
    try:
        _R, _C, _F, build = gmail_oauth._libs()
        return build("gmail", "v1", credentials=creds, cache_discovery=False)
    except Exception as e:  # noqa: BLE001
        log.debug("Gmail read client unavailable: %s", e)
        return None


def available() -> tuple[bool, str]:
    """(usable, why-not) — what `doctor` reports."""
    if gmail_oauth._load_creds() is None:
        return False, "Gmail not connected (run: applypilot network --gmail-connect)"
    if not gmail_oauth.has_scope(gmail_oauth.READ_SCOPE):
        return False, "gmail.metadata scope not granted — reconnect to enable reply detection"
    return True, "reply detection available"


def load_watermark() -> dict:
    try:
        return json.loads(WATERMARK_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_watermark(history_id: str | None = None, checked_at: str = "") -> None:
    data = load_watermark()
    if history_id:
        data["history_id"] = str(history_id)
    if checked_at:
        data["checked_at"] = checked_at
    try:
        config.APP_DIR.mkdir(parents=True, exist_ok=True)
        WATERMARK_PATH.write_text(json.dumps(data), encoding="utf-8")
    except OSError as e:  # noqa: BLE001
        log.warning("Could not persist the Gmail watermark: %s", e)


def _headers(payload: dict) -> dict:
    return {h.get("name", "").lower(): h.get("value", "")
            for h in ((payload or {}).get("headers") or [])}


def thread_messages(thread_id: str, service=None) -> list[dict]:
    """Every message in a thread, flattened to the fields the matcher needs.

    `format="metadata"` with an explicit header list: asking for full would be refused under
    the metadata scope, and asking for all headers pulls far more than we need.
    """
    svc = service or _service()
    if svc is None or not thread_id:
        return []
    try:
        thread = svc.users().threads().get(
            userId="me", id=thread_id, format="metadata",
            metadataHeaders=["From", "To", "Cc", "Date", "Message-ID", "In-Reply-To",
                             "References", "Subject", "Auto-Submitted"],
        ).execute()
    except Exception as e:  # noqa: BLE001
        log.debug("Gmail thread %s unreadable: %s", thread_id, e)
        return []

    out = []
    for msg in thread.get("messages") or []:
        h = _headers(msg.get("payload"))
        out.append({
            "id": msg.get("id"),
            "thread_id": msg.get("threadId") or thread_id,
            "labelIds": msg.get("labelIds") or [],
            "internalDate": msg.get("internalDate") or "",
            "from": h.get("from", ""),
            # To/Cc are how a HANDOFF is visible: an introduction usually arrives as a Cc, and
            # reading senders alone misses it entirely (CRM-4).
            "to": h.get("to", ""),
            "cc": h.get("cc", ""),
            "subject": h.get("subject", ""),
            "in_reply_to": h.get("in-reply-to", ""),
            "references": h.get("references", ""),
            "rfc_message_id": h.get("message-id", ""),
            # Bounces and vacation autoresponders set this. Cheap to ask for, and the only
            # signal that catches an autoresponder whose subject looks like a normal reply.
            "auto_submitted": h.get("auto-submitted", ""),
        })
    return out


def current_history_id(service=None) -> str:
    """The mailbox's history id right now — the watermark for the NEXT poll."""
    svc = service or _service()
    if svc is None:
        return ""
    try:
        return str((svc.users().getProfile(userId="me").execute() or {}).get("historyId") or "")
    except Exception as e:  # noqa: BLE001
        log.debug("Gmail profile unreadable: %s", e)
        return ""


def threads_with_activity(since_history_id: str, service=None) -> set[str]:
    """Thread ids touched since the watermark, or an empty set meaning "no idea, check all".

    An empty return is deliberately ambiguous and the caller MUST treat it as "unknown", not as
    "nothing happened": Gmail expires history ids after about a week, and a stale one returns an
    error rather than a list. Treating that as "nothing changed" would silently stop reply
    detection forever on any mailbox left idle.
    """
    svc = service or _service()
    if svc is None or not since_history_id:
        return set()
    ids: set[str] = set()
    try:
        page = None
        while True:
            resp = svc.users().history().list(
                userId="me", startHistoryId=str(since_history_id),
                historyTypes=["messageAdded"], pageToken=page,
            ).execute() or {}
            for h in resp.get("history") or []:
                for added in h.get("messagesAdded") or []:
                    tid = (added.get("message") or {}).get("threadId")
                    if tid:
                        ids.add(tid)
            page = resp.get("nextPageToken")
            if not page:
                break
    except Exception as e:  # noqa: BLE001
        log.info("Gmail history unavailable (watermark likely expired): %s", e)
        return set()
    return ids

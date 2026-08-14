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
            # CRM-4b. Gmail returns `snippet` on the SAME call — under `gmail.metadata` it
            # comes back empty, and it populates only once the token carries `gmail.readonly`.
            # So this needs no second request and no wider `format=`: the scope alone decides
            # whether there is anything here. Storing it is gated separately in replies.py.
            "snippet": (msg.get("snippet") or "").strip(),
        })
    return out


def search_threads(query: str, limit: int = 25, service=None) -> list[str]:
    """Thread ids matching a Gmail search.

    **Requires `gmail.readonly`.** Under `gmail.metadata` the API refuses `q=` entirely, which
    is why every earlier feature listed threads by an id we already held and never searched.
    That is also why ApplyPilot could only ever see conversations it had started itself: a
    thread somebody else began, or one sent from Gmail directly, had no id to look up.
    """
    svc = service or _service()
    if svc is None or not (query or "").strip():
        return []
    try:
        res = svc.users().threads().list(userId="me", q=query, maxResults=limit).execute()
    except Exception as e:  # noqa: BLE001
        log.debug("Gmail thread search failed (%s): %s", query, e)
        return []
    return [t.get("id") for t in (res.get("threads") or []) if t.get("id")]


def message_body(message_id: str, service=None) -> str:
    """The actual TEXT of one message. '' when it cannot be read.

    `thread_messages` above asks for `format="metadata"`, which by design returns headers and
    Gmail's own `snippet` and no body at all. That was the only option when the token carried
    `gmail.metadata`; `gmail.readonly` has been granted since 2026-07-31 and nothing was changed
    to use it — so "⤓ Fetch from Gmail" pulled the same ~200-character preview the automatic
    sync already had, while `PASTED_MAX = 2000` recorded an intent that was never met.

    Measured when this was written: of 646 stored messages, **none exceeded 200 characters** and
    half sat at 151-199, which is the shape of Gmail's snippet rather than of anybody's writing.

    ONLY on an explicit request. The five-minute poller and the card-open sync still store the
    snippet and nothing else — the documented narrowing is what we ever *do* read, not what the
    grant allows, and reading a body is the line that stays behind a click.

    `text/plain` is preferred over `text/html`: it is what the sender's client generated, and the
    HTML alternative carries markup that would reach a drafting prompt as content.
    """
    ok, _why = can_read_content()
    if not ok:
        return ""
    svc = service or _service()
    if svc is None or not message_id:
        return ""
    try:
        msg = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
    except Exception as e:  # noqa: BLE001
        log.debug("Gmail message %s unreadable in full: %s", message_id, e)
        return ""
    return message_body_from(msg.get("payload") or {})


def message_body_from(payload: dict) -> str:
    """The text of one already-fetched payload. Split out so the plain-over-html preference can
    be tested without a Gmail client — testing `_walk_parts` alone proves what was FOUND and not
    which one is chosen, and the mutation that preferred html survived exactly that gap."""
    plain, html = _walk_parts(payload)
    if plain:
        return plain
    return _strip_html(html) if html else ""


def _walk_parts(payload: dict) -> tuple[str, str]:
    """(text/plain, text/html) from a MIME tree. Both may be ''.

    Recursive because a real message nests: `multipart/mixed` wrapping `multipart/alternative`
    wrapping the two bodies is ordinary, and reading only the top level finds neither.
    """
    plain, html = "", ""
    mime = (payload.get("mimeType") or "").lower()
    data = ((payload.get("body") or {}).get("data")) or ""
    if data:
        text = _decode(data)
        if mime.startswith("text/plain"):
            plain = text
        elif mime.startswith("text/html"):
            html = text
    for part in (payload.get("parts") or []):
        p, h = _walk_parts(part)
        plain = plain or p
        html = html or h
    return plain, html


def _decode(data: str) -> str:
    """Gmail returns base64URL, not standard base64 — '-' and '_' for '+' and '/'."""
    import base64
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def _strip_html(html: str) -> str:
    """Last resort when a sender's client emitted no plain-text alternative."""
    import re as _re
    from html import unescape
    text = _re.sub(r"(?is)<(script|style).*?</\1>", " ", html or "")
    text = _re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = _re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    return _re.sub(r"[ \t]{2,}", " ", _re.sub(r"\n{3,}", "\n\n", text)).strip()


def can_read_content() -> tuple[bool, str]:
    """May we look at what a reply SAYS? (CRM-4b)

    Separate from `available()` because the answer is different: a normal install can read
    headers and threads and must keep working exactly as before when this is False.
    """
    from applypilot.networking import gmail_oauth
    if not gmail_oauth.available():
        return False, "Gmail not connected"
    if not gmail_oauth.can_read_content():
        return False, ("reply content is off — only headers are readable. Enable with "
                       "`applypilot network --gmail-connect --with-content` (grants "
                       "gmail.readonly: it can read every message in the mailbox).")
    return True, "ok"


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

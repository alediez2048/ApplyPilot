"""CAL-1 — creating, moving and cancelling a Google Calendar event.

The thin layer over the API. Every decision that can be made without the network is in
`domain/invite.py`; this is the part that needs credentials.

Same client as Gmail (`google-api-python-client` is already required by the `gmail` extra), a
different `build()`. No new dependency.

**Nothing here is ever automatic.** Not on the reply poller, not in `tick`, not in the bulk
follow-up path. A meeting on somebody's calendar is the least reversible thing this app can do,
and it is always one operator clicking one button for one person.
"""

from __future__ import annotations

import logging

from applypilot.networking import gmail_oauth

log = logging.getLogger(__name__)

#: `primary` is the account's own calendar. A second one would have to be named, and there is no
#: UI for choosing — so this is stated rather than configurable, and changing it is a code change
#: somebody has to argue for.
CALENDAR_ID = "primary"


def _service():
    """The Calendar client, or (None, reason). Never raises into a request handler."""
    if not gmail_oauth.available():
        return None, "Gmail/Calendar is not connected — run `applypilot network --gmail-connect`"
    if not gmail_oauth.can_send_invites():
        return None, ("Calendar access has not been granted. Run:\n"
                      "  applypilot network --gmail-connect --with-calendar\n"
                      "It opens a Google consent screen and keeps your existing Gmail access.")
    try:
        _R, _C, _F, build = gmail_oauth._libs()
    except ImportError:
        return None, "Install deps: pip install google-api-python-client google-auth-oauthlib"
    creds = gmail_oauth._load_creds()
    if creds is None:
        return None, "the stored token is invalid — reconnect"
    try:
        return build("calendar", "v3", credentials=creds, cache_discovery=False), ""
    except Exception as e:  # noqa: BLE001
        return None, f"could not reach Google Calendar: {e}"


def create(body: dict, notify: bool = True) -> dict:
    """Create the event and let GOOGLE send the invitation. Returns {ok, error, id, link, meet}.

    `sendUpdates="all"` is the point of the whole feature — the invitation arrives as a real
    calendar invite with RSVP buttons, from the operator's own account, which is what the
    reverted `.ics` attempt was approximating.

    It is also the trap: because Google mails it, this never passes through `gmail_send`, so the
    daily limit, the per-company cap and the address cooldown do not see it. Those are checked by
    the CALLER before we get here (§Lessons 77 — a send path that is not counted).

    `conferenceDataVersion=1` is required for `conferenceData` to be honoured. Without it the
    field is accepted and silently dropped: the event is created, the invitation goes out, and
    there is no Meet link on it — a success response for a half-made meeting.
    """
    svc, why = _service()
    if svc is None:
        return {"ok": False, "error": why, "id": "", "link": "", "meet": ""}
    try:
        ev = svc.events().insert(
            calendarId=CALENDAR_ID,
            body=body,
            sendUpdates="all" if notify else "none",
            conferenceDataVersion=1 if body.get("conferenceData") else 0,
        ).execute()
    except Exception as e:  # noqa: BLE001
        log.warning("calendar insert failed", exc_info=True)
        return {"ok": False, "error": _readable(e), "id": "", "link": "", "meet": ""}
    return {"ok": True, "error": "", "id": ev.get("id") or "",
            "link": ev.get("htmlLink") or "", "meet": _meet_url(ev)}


def cancel(event_id: str, notify: bool = True) -> dict:
    """Delete the event; Google mails the cancellation.

    Ships WITH send rather than after it. An invite you can create and not withdraw is half a
    feature, and it is the half you need in a hurry — a wrong time or a wrong person is
    discovered in the minute after clicking, not next week.
    """
    if not (event_id or "").strip():
        return {"ok": False, "error": "no event to cancel"}
    svc, why = _service()
    if svc is None:
        return {"ok": False, "error": why}
    try:
        svc.events().delete(calendarId=CALENDAR_ID, eventId=event_id,
                            sendUpdates="all" if notify else "none").execute()
    except Exception as e:  # noqa: BLE001
        # An event the operator already deleted in Google's own UI is GONE, which is the state
        # they asked for. Reporting it as a failure would leave a stale row on the card that
        # nothing can clear.
        if _status(e) in (404, 410):
            return {"ok": True, "error": "", "note": "it was already gone"}
        log.warning("calendar delete failed", exc_info=True)
        return {"ok": False, "error": _readable(e)}
    return {"ok": True, "error": ""}


def _meet_url(ev: dict) -> str:
    """The Meet link off a created event, whichever field carries it.

    `hangoutLink` is the convenient one and is not always populated on the insert response, so
    the entry points are checked too — otherwise a meeting WITH a Meet link renders as one
    without, and the operator sends a second invite to fix a problem that does not exist.
    """
    if ev.get("hangoutLink"):
        return ev["hangoutLink"]
    for ep in ((ev.get("conferenceData") or {}).get("entryPoints") or []):
        if ep.get("entryPointType") == "video" and ep.get("uri"):
            return ep["uri"]
    return ""


def _status(e) -> int:
    return int(getattr(getattr(e, "resp", None), "status", 0) or 0)


def _readable(e) -> str:
    """Google's errors are JSON blobs. The two that actually happen get a sentence."""
    code = _status(e)
    if code == 403:
        return ("Google refused the request (403). The token may not carry calendar access — "
                "run `applypilot network --gmail-connect --with-calendar`.")
    if code == 404:
        return "that event no longer exists"
    text = str(e)
    return text[:300] if text else "Google Calendar rejected the request"

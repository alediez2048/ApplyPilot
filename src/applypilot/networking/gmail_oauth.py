"""Self-contained Gmail OAuth transport — no third-party service.

The user creates a Google Cloud OAuth *Desktop* client once and drops the client-secret
JSON at ~/.applypilot/gmail_oauth_client.json. `connect()` runs the local OAuth flow
(opens a browser) and stores the token at ~/.applypilot/gmail_token.json.

Scopes, and why each is needed:
  gmail.send            send outreach + follow-ups
  gmail.metadata        headers / thread ids only — CANNOT read any message body. Lets a
                        follow-up land INSIDE the original conversation instead of
                        starting a new one. Deliberately narrower than gmail.readonly.
  gmail.settings.basic  read the account's real signature so API-sent mail looks like
                        mail composed in Gmail (the web UI appends it; the API does not)

Everything stays local — tokens never leave the machine. Tokens issued before the extra
scopes were added still work for sending; `missing_scopes()` reports what a re-consent
would unlock rather than breaking the send path.
"""

from __future__ import annotations

import base64
import logging
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from applypilot import config

log = logging.getLogger(__name__)

SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
# metadata, NOT readonly: this grants headers / thread ids / senders and CANNOT read a
# single message body. It covers everything we need (threading a follow-up, and later
# noticing that a reply arrived), at a fraction of the blast radius if the token leaks.
READ_SCOPE = "https://www.googleapis.com/auth/gmail.metadata"
SETTINGS_SCOPE = "https://www.googleapis.com/auth/gmail.settings.basic"
SCOPES = [SEND_SCOPE, READ_SCOPE, SETTINGS_SCOPE]
CLIENT_SECRET_PATH = config.APP_DIR / "gmail_oauth_client.json"
TOKEN_PATH = config.APP_DIR / "gmail_token.json"

_SETUP_HELP = (
    "Gmail OAuth not set up. One-time steps:\n"
    "  1. https://console.cloud.google.com → create/select a project\n"
    "  2. Enable the Gmail API\n"
    "  3. Create an OAuth client ID of type 'Desktop app'\n"
    f"  4. Download the JSON and save it to: {CLIENT_SECRET_PATH}\n"
    "  5. Run: applypilot network --gmail-connect"
)


def _libs():
    """Import the google libs lazily so the base install doesn't require them."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    return Request, Credentials, InstalledAppFlow, build


def _lock_down(path) -> None:
    """chmod 600. A Gmail refresh token is a long-lived credential — it should not be
    readable by every process and account on the machine. Best-effort (no-op on Windows)."""
    try:
        path.chmod(0o600)
    except Exception:  # noqa: BLE001
        pass


def granted_scopes() -> list[str]:
    """Scopes the STORED token actually carries (may predate a scope addition)."""
    if not TOKEN_PATH.exists():
        return []
    try:
        import json
        return list(json.loads(TOKEN_PATH.read_text(encoding="utf-8")).get("scopes") or [])
    except Exception:  # noqa: BLE001
        return []


def missing_scopes() -> list[str]:
    """Scopes this build wants that the stored token lacks. Empty when fully authorized."""
    have = set(granted_scopes())
    return [s for s in SCOPES if s not in have]


def has_scope(scope: str) -> bool:
    return scope in set(granted_scopes())


def _load_creds(refresh: bool = True):
    """Load stored credentials, refreshing if expired. Returns creds or None.

    Loads with the token's OWN scopes, not this build's SCOPES. Forcing a wider list on
    an older token makes google-auth treat the refresh as a scope change and fail — which
    would break sending for anyone who authorized before the read scope was added.
    """
    if not TOKEN_PATH.exists():
        return None
    try:
        Request, Credentials, _flow, _build = _libs()
    except ImportError:
        return None
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), granted_scopes() or SCOPES)
    if refresh and creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
            _lock_down(TOKEN_PATH)
        except Exception as e:  # noqa: BLE001
            log.warning("Gmail token refresh failed: %s", e)
            return None
    return creds if creds and creds.valid else None


def available() -> bool:
    """True if a valid Gmail OAuth token is present (transport ready)."""
    return _load_creds() is not None


def connect() -> tuple[bool, str]:
    """Run the one-time OAuth flow (opens a browser). Stores the token. Returns (ok, msg)."""
    if not CLIENT_SECRET_PATH.exists():
        return False, _SETUP_HELP
    try:
        _Request, _Credentials, InstalledAppFlow, _build = _libs()
    except ImportError:
        return False, "Install deps: pip install google-api-python-client google-auth-oauthlib"
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
        creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        _lock_down(TOKEN_PATH)
    except Exception as e:  # noqa: BLE001
        return False, f"OAuth flow failed: {e}"
    return True, f"Gmail connected. Token stored at {TOKEN_PATH}"


def probe() -> tuple[bool, str]:
    """Readiness check for `doctor` — token present + valid (no send)."""
    if not CLIENT_SECRET_PATH.exists():
        return False, "OAuth client JSON missing (see `network --gmail-connect`)"
    if not TOKEN_PATH.exists():
        return False, "not connected — run `applypilot network --gmail-connect`"
    try:
        _libs()
    except ImportError:
        return False, "pip install google-api-python-client google-auth-oauthlib"
    return (True, "Gmail OAuth connected") if _load_creds() else (False, "token invalid — reconnect")


def _body_to_html(body: str) -> str:
    """Plain-text body → minimal HTML, preserving paragraph breaks. Escaped, never raw."""
    from html import escape
    paras = [p.strip() for p in (body or "").split("\n\n")]
    return "".join(f"<p>{escape(p).replace(chr(10), '<br>')}</p>" for p in paras if p)


def fetch_signature(for_address: str = "") -> str:
    """The account's own Gmail signature (HTML), or '' if unavailable.

    Gmail's WEB UI inserts the signature when you compose; the API sends exactly the MIME
    we build, which is why API-sent mail arrives unsigned. Reading it from settings keeps
    one source of truth instead of a second copy that drifts.
    """
    creds = _load_creds()
    if creds is None or not has_scope(SETTINGS_SCOPE):
        return ""
    try:
        _R, _C, _F, build = _libs()
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        aliases = service.users().settings().sendAs().list(userId="me").execute()
    except Exception as e:  # noqa: BLE001
        log.debug("Gmail signature fetch failed: %s", e)
        return ""
    entries = aliases.get("sendAs") or []
    want = (for_address or "").strip().lower()
    # Prefer the alias we actually send from; else the account default.
    for e in entries:
        if want and (e.get("sendAsEmail") or "").lower() == want:
            return e.get("signature") or ""
    for e in entries:
        if e.get("isDefault"):
            return e.get("signature") or ""
    return (entries[0].get("signature") or "") if entries else ""


def message_thread_info(message_id: str) -> dict:
    """threadId + RFC Message-ID header for an already-sent message.

    Lets follow-ups thread into conversations that were sent before those ids were being
    persisted. Returns {} when the read scope is absent or the message is gone.
    """
    if not message_id:
        return {}
    creds = _load_creds()
    if creds is None or not has_scope(READ_SCOPE):
        return {}
    try:
        _R, _C, _F, build = _libs()
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        msg = service.users().messages().get(
            userId="me", id=message_id, format="metadata",
            metadataHeaders=["Message-ID", "Subject"],
        ).execute()
    except Exception as e:  # noqa: BLE001
        log.debug("Gmail message lookup failed for %s: %s", message_id, e)
        return {}
    headers = {h.get("name", "").lower(): h.get("value", "")
               for h in ((msg.get("payload") or {}).get("headers") or [])}
    return {"thread_id": msg.get("threadId", ""),
            "rfc_message_id": headers.get("message-id", ""),
            "subject": headers.get("subject", "")}


#: (token mtime) -> address. Keyed on the token file so reconnecting a DIFFERENT account
#: invalidates it, while an unchanged token costs nothing.
_EMAIL_CACHE: dict[float, str] = {}


def connected_email() -> str:
    """The authenticated account's email address (empty if unavailable).

    Cached, because this is an HTTP round-trip to Gmail and it is called from a RENDER path.
    `/api/status` asks once per job, refreshes every 2.5s, and was measured at **2.4s per
    request** with 15 jobs — the dashboard was refreshing back-to-back and spending most of it
    asking Gmail the same unchanging question (§Lessons 11: idempotent is not free).
    """
    try:
        key = TOKEN_PATH.stat().st_mtime
    except OSError:
        key = 0.0
    if key in _EMAIL_CACHE:
        return _EMAIL_CACHE[key]

    creds = _load_creds()
    if creds is None:
        return ""
    try:
        _R, _C, _F, build = _libs()
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        email = service.users().getProfile(userId="me").execute().get("emailAddress", "")
    except Exception:  # noqa: BLE001
        return ""  # not cached: a transient failure must not pin an empty address forever
    if email:
        _EMAIL_CACHE.clear()  # only ever one account connected at a time
        _EMAIL_CACHE[key] = email
    return email


def send(to_addr: str, subject: str, body: str, from_addr: str,
         from_name: str = "", attachments: list[tuple[str, str]] | None = None,
         thread_id: str | None = None, in_reply_to: str | None = None,
         cc: list[str] | None = None, references: str | None = None) -> dict:
    """Send via the Gmail API. Raises on failure.

    Returns {"id", "thread_id", "rfc_message_id"} — the caller persists the last two so a
    later follow-up can land INSIDE this conversation. Gmail hands back threadId on the
    send response and we generate the RFC Message-ID ourselves, so threading costs no
    extra OAuth scope.

    attachments: list of (path, display_filename) PDFs to attach.
    thread_id / in_reply_to: set both to make this message a reply in an existing thread.
    """
    creds = _load_creds()
    if creds is None:
        raise RuntimeError("Gmail OAuth not connected")
    _Request, _Credentials, _flow, build = _libs()

    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_addr)) if from_name else from_addr
    msg["To"] = to_addr
    msg["Reply-To"] = from_addr
    msg["Subject"] = subject
    # Carried forward from the message being answered, not rebuilt: dropping a Cc drops the
    # person the other side introduced, and does it silently (CRM-4a).
    if cc:
        msg["Cc"] = ", ".join(c for c in cc if c)
    rfc_id = make_msgid(domain=(from_addr.split("@")[-1] if "@" in from_addr else None))
    msg["Message-ID"] = rfc_id
    # Gmail groups by these headers; threadId alone is not enough for the RECIPIENT's client.
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        # References chains the whole thread when we have it. Falling back to In-Reply-To alone
        # still threads, just more fragilely in clients that walk the chain.
        msg["References"] = references or in_reply_to
    msg.set_content(body)
    # Signature: Gmail only appends it in the web UI, so add it ourselves. Sent as an HTML
    # alternative alongside the plain text — the body stays verbatim in both parts.
    from applypilot.networking.gmail_send import signature_html
    sig = signature_html(from_addr)
    if sig:
        msg.add_alternative(f"<div>{_body_to_html(body)}</div><br>{sig}", subtype="html")
    from applypilot.networking.gmail_send import attach_pdfs
    attach_pdfs(msg, attachments)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    payload: dict = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    sent = service.users().messages().send(userId="me", body=payload).execute()
    return {"id": sent.get("id", rfc_id), "thread_id": sent.get("threadId", ""),
            "rfc_message_id": rfc_id}

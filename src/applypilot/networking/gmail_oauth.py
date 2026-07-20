"""Self-contained Gmail OAuth transport (send-only) — no third-party service.

The user creates a Google Cloud OAuth *Desktop* client once and drops the client-secret
JSON at ~/.applypilot/gmail_oauth_client.json. `connect()` runs the local OAuth flow
(opens a browser, authorizes the send-only scope) and stores the token at
~/.applypilot/gmail_token.json. Sending uses the Gmail API directly.

Scope is minimal: gmail.send only (cannot read the inbox). Everything stays local —
tokens never leave the machine.
"""

from __future__ import annotations

import base64
import logging
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from applypilot import config

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
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


def _load_creds(refresh: bool = True):
    """Load stored credentials, refreshing if expired. Returns creds or None."""
    if not TOKEN_PATH.exists():
        return None
    try:
        Request, Credentials, _flow, _build = _libs()
    except ImportError:
        return None
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if refresh and creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
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


def connected_email() -> str:
    """The authenticated account's email address (empty if unavailable)."""
    creds = _load_creds()
    if creds is None:
        return ""
    try:
        _R, _C, _F, build = _libs()
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return service.users().getProfile(userId="me").execute().get("emailAddress", "")
    except Exception:  # noqa: BLE001
        return ""


def send(to_addr: str, subject: str, body: str, from_addr: str,
         from_name: str = "", attachments: list[tuple[str, str]] | None = None) -> str:
    """Send via the Gmail API. Returns Gmail's real message id. Raises on failure.

    attachments: list of (path, display_filename) PDFs to attach.
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
    msg["Message-ID"] = make_msgid(domain=(from_addr.split("@")[-1] if "@" in from_addr else None))
    msg.set_content(body)
    from applypilot.networking.gmail_send import attach_pdfs
    attach_pdfs(msg, attachments)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return sent.get("id", msg["Message-ID"])

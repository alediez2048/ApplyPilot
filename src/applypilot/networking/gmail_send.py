"""Gmail outreach sending — SMTP (app password) with safeguards.

Every send is user-initiated. Guardrails, all enforced here:
  - verified-gate: unverified addresses require explicit confirm; no address → blocked
  - daily cap: OUTREACH_DAILY_LIMIT across all jobs
  - cross-job dedupe: never email one person twice within a cooldown window
  - atomic claim: submitted_at IS NULL guard prevents double-send under the threading server
  - "submitted" (not "delivered"): SMTP acceptance ≠ delivery

Sender is GMAIL_ADDRESS (a Workspace @utexas.edu account may need OAuth if the admin
disabled app passwords — a 535 is detected and surfaced with actionable guidance).

OAuth transport is a documented follow-up (NET-6); SMTP is the v1 path.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from applypilot.networking import store

log = logging.getLogger(__name__)

_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 465

_DAILY_LIMIT = int(os.environ.get("OUTREACH_DAILY_LIMIT", "20") or "20")
_COOLDOWN_DAYS = int(os.environ.get("OUTREACH_COOLDOWN_DAYS", "30") or "30")

_FOOTER = "\n\n—\nSent via ApplyPilot on my own behalf. Reply here to reach me directly."


def _creds() -> tuple[str, str]:
    return os.environ.get("GMAIL_ADDRESS", ""), os.environ.get("GMAIL_APP_PASSWORD", "")


def configured() -> bool:
    addr, pw = _creds()
    return bool(addr and pw)


def can_send(contact: dict, confirm_unverified: bool = False) -> tuple[bool, str]:
    """Gate a send. Returns (ok, reason). Does NOT claim or send."""
    if not configured():
        return False, "Gmail not configured (set GMAIL_ADDRESS + GMAIL_APP_PASSWORD)"
    email = (contact.get("email") or "").strip()
    if not email:
        return False, "no email address for this contact"
    status = contact.get("email_status") or "none"
    if status != "verified" and not confirm_unverified:
        return False, "email is unverified — confirm to send anyway"
    if contact.get("outreach_status") == "submitted":
        return False, "already sent to this contact"
    if store.sent_today() >= _DAILY_LIMIT:
        return False, f"daily send limit reached ({_DAILY_LIMIT})"
    prior = store.already_contacted_email(email, _COOLDOWN_DAYS, exclude_id=contact.get("id"))
    if prior:
        return False, f"already emailed {email} for another role on {prior[:10]}"
    return True, "ok"


def _smtp_send(to_addr: str, subject: str, body: str, message_id: str) -> None:
    """Send one email over SMTP_SSL. Raises smtplib errors (caller handles)."""
    addr, pw = _creds()
    from_name = os.environ.get("OUTREACH_FROM_NAME", "")
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, addr)) if from_name else addr
    msg["To"] = to_addr
    msg["Reply-To"] = addr
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg.set_content(body + _FOOTER)

    with smtplib.SMTP_SSL(_SMTP_HOST, _SMTP_PORT, timeout=30) as smtp:
        smtp.login(addr, pw)
        smtp.send_message(msg)


def send_outreach(contact_id: str, confirm_unverified: bool = False,
                  dry_run: bool = False) -> dict:
    """Send the drafted outreach email for a contact, with all safeguards.

    Returns {"ok": bool, "message": str, "status": str}.
    """
    contact = store.get_contact(contact_id)
    if not contact:
        return {"ok": False, "message": "contact not found", "status": "none"}

    ok, reason = can_send(contact, confirm_unverified=confirm_unverified)
    if not ok:
        return {"ok": False, "message": reason, "status": contact.get("outreach_status", "none")}

    subject = contact.get("outreach_subject") or ""
    body = contact.get("outreach_message") or ""
    if not body:
        return {"ok": False, "message": "no draft to send — generate one first", "status": "drafted"}

    to_addr = contact["email"]
    if dry_run:
        log.info("[dry-run] would email %s <%s>: %s", contact.get("full_name"), to_addr, subject)
        return {"ok": True, "message": f"dry-run: not sent to {to_addr}", "status": "drafted"}

    # Atomic claim — only the winner proceeds to actually send.
    if not store.claim_for_send(contact_id):
        return {"ok": False, "message": "send already in progress / done", "status": "sending"}

    addr, _ = _creds()
    message_id = make_msgid(domain=(addr.split("@")[-1] if "@" in addr else None))
    try:
        _smtp_send(to_addr, subject, body, message_id)
    except smtplib.SMTPAuthenticationError as e:
        store.mark_send_failed(contact_id, f"auth failed (535?): {e}")
        return {"ok": False, "status": "failed",
                "message": "Gmail auth failed (535). If this is a Workspace/@utexas.edu "
                           "account, an admin may have disabled app passwords — OAuth needed."}
    except Exception as e:  # noqa: BLE001
        store.mark_send_failed(contact_id, str(e))
        return {"ok": False, "message": f"send failed: {e}", "status": "failed"}

    store.mark_sent(contact_id, message_id)
    return {"ok": True, "message": f"submitted to {to_addr}", "status": "submitted"}


def auth_probe() -> tuple[bool, str]:
    """AUTH-only login test for `doctor` (connect + login + quit, no send)."""
    if not configured():
        return False, "GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set"
    addr, pw = _creds()
    try:
        with smtplib.SMTP_SSL(_SMTP_HOST, _SMTP_PORT, timeout=20) as smtp:
            smtp.login(addr, pw)
        return True, f"Gmail SMTP auth OK ({addr})"
    except smtplib.SMTPAuthenticationError:
        return False, ("Gmail auth failed (535) — bad app password, or a Workspace/@utexas.edu "
                       "admin disabled app passwords (use OAuth)")
    except Exception as e:  # noqa: BLE001
        return False, f"Gmail SMTP error: {e}"

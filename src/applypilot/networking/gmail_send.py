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



def _creds() -> tuple[str, str]:
    return os.environ.get("GMAIL_ADDRESS", ""), os.environ.get("GMAIL_APP_PASSWORD", "")


def transport() -> str | None:
    """Which send transport is ready: 'oauth' (preferred) | 'smtp' | None."""
    from applypilot.networking import gmail_oauth
    if gmail_oauth.available():
        return "oauth"
    addr, pw = _creds()
    if addr and pw:
        return "smtp"
    return None


def _from_address() -> str:
    """The address recipients see in the From line.

    OUTREACH_FROM_ADDRESS wins (lets you send from a different verified alias, e.g.
    a .edu, while authenticating as another Gmail account). Falls back to
    GMAIL_ADDRESS, then the connected OAuth account.

    NOTE: Gmail only honors a From that differs from the authenticated account if
    it is a *verified* "Send mail as" alias in that account — otherwise Gmail
    rewrites it. Verify the alias in Gmail settings before relying on this.
    """
    return os.environ.get("OUTREACH_FROM_ADDRESS", "") or os.environ.get("GMAIL_ADDRESS", "")


def configured() -> bool:
    return transport() is not None


def can_send(contact: dict, confirm_unverified: bool = False) -> tuple[bool, str]:
    """Gate a send. Returns (ok, reason). Does NOT claim or send."""
    if not configured():
        return False, "Gmail not connected (run `applypilot network --gmail-connect`)"
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


def _attachments_enabled() -> bool:
    """Attach the tailored resume + cover letter PDFs to outreach emails (default on)."""
    return os.environ.get("OUTREACH_ATTACH_DOCS", "1").lower() in {"1", "true", "yes", "on"}


def _applicant_slug() -> str:
    """Recruiter-friendly filename prefix from the profile name (e.g. Jorge_Alejandro_Diez)."""
    try:
        from applypilot.config import load_profile
        name = (load_profile().get("personal", {}).get("full_name") or "").strip()
        return name.replace(" ", "_") if name else "Resume"
    except Exception:  # noqa: BLE001
        return "Resume"


def job_attachments(job_url: str) -> list[tuple[str, str]]:
    """Resolve (path, display_filename) for the job's resume + cover letter PDFs.

    Looks up the job by the contact's job_url and returns whichever PDFs exist, named
    for the applicant so recruiters see e.g. `Jorge_Alejandro_Diez_Resume.pdf`.
    """
    from pathlib import Path

    if not job_url or not _attachments_enabled():
        return []
    from applypilot.database import get_connection
    conn = get_connection()
    row = conn.execute(
        "SELECT tailored_resume_path, cover_letter_path FROM jobs "
        "WHERE url = ? OR application_url = ? LIMIT 1",
        (job_url, job_url),
    ).fetchone()
    if not row:
        return []
    slug = _applicant_slug()
    out: list[tuple[str, str]] = []
    resume_pdf = Path(row[0]).with_suffix(".pdf") if row[0] else None
    if resume_pdf and resume_pdf.exists():
        out.append((str(resume_pdf), f"{slug}_Resume.pdf"))
    cover_pdf = Path(row[1]).with_suffix(".pdf") if row[1] else None
    if cover_pdf and cover_pdf.exists():
        out.append((str(cover_pdf), f"{slug}_Cover_Letter.pdf"))
    return out


def attach_pdfs(msg: EmailMessage, attachments: list[tuple[str, str]] | None) -> None:
    """Attach each (path, filename) PDF to an EmailMessage. Missing files are skipped."""
    from pathlib import Path

    for path, filename in (attachments or []):
        p = Path(path)
        if not p.exists():
            continue
        msg.add_attachment(p.read_bytes(), maintype="application",
                           subtype="pdf", filename=filename)


def _smtp_send(to_addr: str, subject: str, body: str, message_id: str,
               attachments: list[tuple[str, str]] | None = None) -> None:
    """Send one email over SMTP_SSL. `body` is sent verbatim."""
    addr, pw = _creds()
    from_name = os.environ.get("OUTREACH_FROM_NAME", "")
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, addr)) if from_name else addr
    msg["To"] = to_addr
    msg["Reply-To"] = addr
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg.set_content(body)
    attach_pdfs(msg, attachments)

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
    attachments = job_attachments(contact.get("job_url", ""))
    if dry_run:
        att = ", ".join(f for _, f in attachments) or "none"
        log.info("[dry-run] would email %s <%s>: %s (attach: %s)",
                 contact.get("full_name"), to_addr, subject, att)
        return {"ok": True, "message": f"dry-run: not sent to {to_addr} (attach: {att})",
                "status": "drafted"}

    # Atomic claim — only the winner proceeds to actually send.
    if not store.claim_for_send(contact_id):
        return {"ok": False, "message": "send already in progress / done", "status": "sending"}

    from_name = os.environ.get("OUTREACH_FROM_NAME", "")
    mode = transport()
    body_out = body  # send exactly as drafted/edited (no appended footer)
    try:
        if mode == "oauth":
            from applypilot.networking import gmail_oauth
            from_addr = _from_address() or gmail_oauth.connected_email()
            message_id = gmail_oauth.send(to_addr, subject, body_out, from_addr, from_name,
                                          attachments=attachments)
        else:
            addr, _ = _creds()
            message_id = make_msgid(domain=(addr.split("@")[-1] if "@" in addr else None))
            _smtp_send(to_addr, subject, body_out, message_id, attachments=attachments)
    except smtplib.SMTPAuthenticationError as e:
        store.mark_send_failed(contact_id, f"auth failed (535?): {e}")
        return {"ok": False, "status": "failed",
                "message": "Gmail SMTP auth failed (535). If this is a Workspace/@utexas.edu "
                           "account, app passwords may be disabled — use OAuth "
                           "(`applypilot network --gmail-connect`)."}
    except Exception as e:  # noqa: BLE001
        store.mark_send_failed(contact_id, str(e))
        return {"ok": False, "message": f"send failed: {e}", "status": "failed"}

    store.mark_sent(contact_id, message_id)
    return {"ok": True, "message": f"submitted to {to_addr} (via {mode})", "status": "submitted"}


def auth_probe() -> tuple[bool, str]:
    """Readiness for `doctor`. Prefers OAuth; else AUTH-only SMTP test (no send)."""
    mode = transport()
    if mode == "oauth":
        from applypilot.networking import gmail_oauth
        return gmail_oauth.probe()
    if mode == "smtp":
        addr, pw = _creds()
        try:
            with smtplib.SMTP_SSL(_SMTP_HOST, _SMTP_PORT, timeout=20) as smtp:
                smtp.login(addr, pw)
            return True, f"Gmail SMTP auth OK ({addr})"
        except smtplib.SMTPAuthenticationError:
            return False, ("Gmail SMTP auth failed (535) — bad app password, or a Workspace/"
                           "@utexas.edu admin disabled app passwords (use `--gmail-connect`)")
        except Exception as e:  # noqa: BLE001
            return False, f"Gmail SMTP error: {e}"
    return False, "not configured — run `applypilot network --gmail-connect` or set GMAIL_ADDRESS/APP_PASSWORD"

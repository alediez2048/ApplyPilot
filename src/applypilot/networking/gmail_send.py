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
import re
import smtplib
from datetime import datetime, timezone
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
    # Already emailed: Gmail returned a message id (ground truth; survives a later draft
    # regenerate that resets outreach_status), or the status is explicitly submitted.
    if (contact.get("sent_message_id") or "").strip() or contact.get("outreach_status") == "submitted":
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


_SIG_CACHE: dict[str, str] = {}


def signature_path():
    """Local override / fallback signature (HTML). Used when the settings scope is absent."""
    from applypilot import config
    return config.APP_DIR / "signature.html"


def signature_html(from_addr: str = "") -> str:
    """The HTML signature to append to outgoing mail, or '' to send unsigned.

    Order: local ~/.applypilot/signature.html (explicit override) → the account's real
    Gmail signature via the settings API. Cached per address for the process lifetime so
    a bulk send doesn't hit the API once per message. OUTREACH_SIGNATURE=0 disables it.
    """
    if os.environ.get("OUTREACH_SIGNATURE", "1").lower() not in {"1", "true", "yes", "on"}:
        return ""
    key = (from_addr or "").lower()
    if key in _SIG_CACHE:
        return _SIG_CACHE[key]
    sig = ""
    try:
        p = signature_path()
        if p.exists():
            sig = p.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        sig = ""
    if not sig:
        try:
            from applypilot.networking import gmail_oauth
            sig = gmail_oauth.fetch_signature(from_addr)
        except Exception:  # noqa: BLE001
            sig = ""
    _SIG_CACHE[key] = sig
    return sig


def _company_slug(job_url: str, conn) -> str:
    """Company suffix for attachment filenames, e.g. `_Arm`. Empty if undeterminable."""
    try:
        row = conn.execute(
            "SELECT * FROM jobs WHERE url = ? OR application_url = ? LIMIT 1",
            (job_url, job_url),
        ).fetchone()
        if not row:
            return ""
        from applypilot.networking import derive
        name = derive.derive_company(dict(zip(row.keys(), row))) or ""
    except Exception:  # noqa: BLE001
        return ""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    # A 1-char slug is derivation noise (a bare host like "http://j/1" yields "J"), and
    # `Resume_J.pdf` looks more like a bug to a recruiter than no suffix at all.
    return f"_{slug}" if len(slug) >= 2 else ""


def job_attachments(job_url: str) -> list[tuple[str, str]]:
    """Resolve (path, display_filename) for the job's resume + cover letter PDFs.

    Filenames carry the COMPANY (`Jorge_Alejandro_Diez_Resume_Arm.pdf`). Six tailored
    résumés all named `..._Resume.pdf` are indistinguishable in a Sent folder, which
    makes real per-job tailoring look like the same document sent everywhere.
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
    co = _company_slug(job_url, conn)
    out: list[tuple[str, str]] = []
    resume_pdf = Path(row[0]).with_suffix(".pdf") if row[0] else None
    if resume_pdf and resume_pdf.exists():
        out.append((str(resume_pdf), f"{slug}_Resume{co}.pdf"))
    cover_pdf = Path(row[1]).with_suffix(".pdf") if row[1] else None
    if cover_pdf and cover_pdf.exists():
        out.append((str(cover_pdf), f"{slug}_Cover_Letter{co}.pdf"))
    # Intro deck / one-pager — the same static PDF on every outreach (not per-job). Drop a PDF at
    # ~/.applypilot/intro_deck.pdf (or point INTRO_DECK_PATH at one) and it rides along.
    deck = _intro_deck_path()
    if deck and deck.exists():
        out.append((str(deck), f"{slug}_Intro_Deck.pdf"))
    return out


def _intro_deck_path():
    """Path to the intro deck PDF to attach to outreach, or None if disabled/absent."""
    from pathlib import Path
    from applypilot import config
    if os.environ.get("OUTREACH_ATTACH_DECK", "1").lower() not in {"1", "true", "yes", "on"}:
        return None
    override = os.environ.get("INTRO_DECK_PATH")
    return Path(override).expanduser() if override else (config.APP_DIR / "intro_deck.pdf")


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
               attachments: list[tuple[str, str]] | None = None,
               in_reply_to: str | None = None) -> None:
    """Send one email over SMTP_SSL. `body` is sent verbatim."""
    addr, pw = _creds()
    from_name = os.environ.get("OUTREACH_FROM_NAME", "")
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, addr)) if from_name else addr
    msg["To"] = to_addr
    msg["Reply-To"] = addr
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    sig = signature_html(addr)
    if sig:
        from applypilot.networking.gmail_oauth import _body_to_html
        msg.add_alternative(f"<div>{_body_to_html(body)}</div><br>{sig}", subtype="html")
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
    thread_id, rfc_id = "", ""
    try:
        if mode == "oauth":
            from applypilot.networking import gmail_oauth
            from_addr = _from_address() or gmail_oauth.connected_email()
            res = gmail_oauth.send(to_addr, subject, body_out, from_addr, from_name,
                                   attachments=attachments)
            message_id = res["id"]
            thread_id, rfc_id = res.get("thread_id", ""), res.get("rfc_message_id", "")
        else:
            addr, _ = _creds()
            message_id = make_msgid(domain=(addr.split("@")[-1] if "@" in addr else None))
            rfc_id = message_id  # SMTP: the RFC id IS what we generated and sent
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

    store.mark_sent(contact_id, message_id, thread_id=thread_id, rfc_message_id=rfc_id)
    return {"ok": True, "message": f"submitted to {to_addr} (via {mode})", "status": "submitted"}


def send_followup(contact_id: str, dry_run: bool = False) -> dict:
    """Send the drafted follow-up for a contact, threaded into the original conversation.

    Separate from send_outreach because the preconditions are different: the first email
    must ALREADY have gone out, there must be a follow-up draft, and the sequence must not
    have been stopped. Attachments are deliberately NOT re-sent — they went with email #1.
    """
    contact = store.get_contact(contact_id)
    if not contact:
        return {"ok": False, "message": "contact not found"}
    if not (contact.get("sent_message_id") or "").strip():
        return {"ok": False, "message": "no first email was sent — nothing to follow up on"}
    if (contact.get("followup_status") or "") == "sending":
        return {"ok": False, "message": "a follow-up is already sending"}
    if (contact.get("followup_status") or "") in ("stopped", "replied"):
        return {"ok": False, "message": f"sequence {contact['followup_status']} — not sending"}

    subject = (contact.get("followup_subject") or "").strip()
    body = (contact.get("followup_message") or "").strip()
    if not body:
        return {"ok": False, "message": "no follow-up draft — generate one first"}
    to_addr = (contact.get("email") or "").strip()
    if not to_addr:
        return {"ok": False, "message": "no email address"}
    if store.sent_today() >= _DAILY_LIMIT:
        return {"ok": False, "message": f"daily send limit reached ({_DAILY_LIMIT})"}

    if dry_run:
        return {"ok": True, "message": f"dry-run: would follow up with {to_addr}"}
    if not store.claim_followup_send(contact_id):
        return {"ok": False, "message": "follow-up already in progress"}

    from_name = os.environ.get("OUTREACH_FROM_NAME", "")
    mode = transport()
    try:
        if mode == "oauth":
            from applypilot.networking import gmail_oauth
            from_addr = _from_address() or gmail_oauth.connected_email()
            res = gmail_oauth.send(to_addr, subject, body, from_addr, from_name,
                                   thread_id=contact.get("thread_id") or None,
                                   in_reply_to=contact.get("rfc_message_id") or None)
            message_id = res["id"]
        else:
            addr, _ = _creds()
            message_id = make_msgid(domain=(addr.split("@")[-1] if "@" in addr else None))
            _smtp_send(to_addr, subject, body, message_id,
                       in_reply_to=contact.get("rfc_message_id") or None)
    except Exception as e:  # noqa: BLE001
        store.mark_followup_failed(contact_id, str(e))
        return {"ok": False, "message": f"follow-up failed: {e}"}

    n = store.mark_followup_sent(contact_id)
    threaded = bool(contact.get("thread_id") or contact.get("rfc_message_id"))
    return {"ok": True, "touch": n,
            "message": f"follow-up #{n} sent to {to_addr}"
                       + ("" if threaded else " (as a new email — the original predates threading)")}


def backfill_thread_ids(limit: int = 200) -> dict:
    """Recover threadId + RFC Message-ID for emails sent before those were persisted.

    Without them a follow-up starts a NEW conversation instead of replying inside the
    original one. We stored Gmail's message id at send time, so the ids are recoverable —
    it just needs the read scope. Idempotent: only touches rows still missing them.
    """
    from applypilot.networking import gmail_oauth
    if not gmail_oauth.has_scope(gmail_oauth.READ_SCOPE):
        return {"ok": False, "updated": 0, "missing": 0,
                "message": "Gmail read scope not granted — run `applypilot network --gmail-connect`"}
    from applypilot.database import get_connection
    store.init_contacts()
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, full_name, sent_message_id FROM contacts "
        "WHERE COALESCE(sent_message_id,'') != '' AND COALESCE(thread_id,'') = '' LIMIT ?",
        (limit,),
    ).fetchall()
    updated, failed = 0, []
    for r in rows:
        info = gmail_oauth.message_thread_info(r["sent_message_id"])
        if not info.get("thread_id"):
            failed.append(r["full_name"] or r["id"])
            continue
        conn.execute(
            "UPDATE contacts SET thread_id = ?, rfc_message_id = ?, updated_at = ? WHERE id = ?",
            (info["thread_id"], info.get("rfc_message_id", ""),
             datetime.now(timezone.utc).isoformat(), r["id"]),
        )
        updated += 1
    conn.commit()
    return {"ok": True, "updated": updated, "missing": len(failed), "not_found": failed,
            "message": f"threading restored for {updated} contact(s)"
                       + (f"; {len(failed)} message(s) no longer in the mailbox" if failed else "")}


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

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

#: ZERO MEANS UNLIMITED, for all three caps below.
#:
#: It already did for the company cap (`if _COMPANY_CAP > 0`) and for the cooldown (a zero-day
#: window matches nothing), but NOT here: `sent_today() >= 0` is true before the first email of
#: the day, so setting this to 0 to "turn the limit off" silently blocked every send instead.
#: The same word meaning "unlimited" in two settings and "send nothing" in a third is a trap
#: with no failure message — outreach would simply stop, and the note would read like a cap had
#: been reached.
_DAILY_LIMIT = int(os.environ.get("OUTREACH_DAILY_LIMIT", "20") or "20")
#: Total emails ONE employer may receive — first contacts plus every follow-up, across every
#: job. The gap the other two caps leave wide open: the daily limit is global and the cooldown
#: is per address, so 7 people at one company × 3 touches is 21 emails and nothing objects.
#: Measured before this existed: Webai and Salesforce had already had 10 each, Wander and
#: Affirm 9. A company sees one sender, not seven conversations.
_COMPANY_CAP = int(os.environ.get("OUTREACH_COMPANY_CAP", "8") or "8")
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


def _our_addresses() -> list[str]:
    """Every address that is us — the alias we send AS, and the account we authenticate as.

    Both matter when deciding who to keep on a reply's Cc: leaving either one in means every
    reply copies us on our own mail, and `_from_address()` alone is empty on an OAuth-only
    setup, so it cannot be the single source.
    """
    out = [os.environ.get("OUTREACH_FROM_ADDRESS", ""), os.environ.get("GMAIL_ADDRESS", "")]
    try:
        from applypilot.networking import gmail_oauth
        out.append(gmail_oauth.connected_email())
    except Exception:  # noqa: BLE001
        pass
    return list(dict.fromkeys(a for a in out if a))   # ordered, deduped


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
    if _DAILY_LIMIT > 0 and store.sent_today() >= _DAILY_LIMIT:
        return False, f"daily send limit reached ({_DAILY_LIMIT})"
    prior = store.already_contacted_email(email, _COOLDOWN_DAYS, exclude_id=contact.get("id"))
    if prior:
        return False, f"already emailed {email} for another role on {prior[:10]}"
    company = (contact.get("company") or "").strip()
    if company and _COMPANY_CAP > 0:
        n = store.emails_sent_to_company(company)
        if n >= _COMPANY_CAP:
            return False, (f"{company} has already had {n} emails from you "
                           f"(cap {_COMPANY_CAP}) — a company sees one sender, not seven threads")
    return True, "ok"


#: The operator's override, a FILE rather than a process variable. It has to outlive a dashboard
#: restart: a toggle that silently reverts to "attach" sends documents somebody had decided not
#: to send, and they would only find out from their Sent folder.
_ATTACH_FLAG = "attach_docs.flag"


def _attach_flag_path():
    from applypilot import config
    return config.APP_DIR / _ATTACH_FLAG


def attachments_enabled() -> bool:
    """Whether the first email carries the résumé + cover letter PDFs.

    Operator override first, `OUTREACH_ATTACH_DOCS` as the default. The env var stays the
    DEFAULT rather than the authority, because a setting read at startup cannot be a control the
    operator flips between two sends — and a default in two places is two defaults, which is how
    the intro-deck PDF rode along on all 34 sent emails while `doctor --config` reported it off.
    """
    override = _read_attach_flag()
    if override is not None:
        return override
    return os.environ.get("OUTREACH_ATTACH_DOCS", "1").lower() in {"1", "true", "yes", "on"}


def _read_attach_flag() -> bool | None:
    """True/False from the flag file, or None when the operator has never set one."""
    try:
        raw = _attach_flag_path().read_text(encoding="utf-8").strip().lower()
    except Exception:  # noqa: BLE001 — no flag, unreadable flag: fall back to the default
        return None
    if raw in ("1", "true", "on", "yes"):
        return True
    if raw in ("0", "false", "off", "no"):
        return False
    return None


def set_attachments_enabled(on: bool) -> bool:
    """Persist the override. Returns what is now in force, read back rather than assumed."""
    from applypilot import config
    try:
        config.APP_DIR.mkdir(parents=True, exist_ok=True)
        _attach_flag_path().write_text("1" if on else "0", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not persist the attachment toggle: %s", exc)
    return attachments_enabled()


#: Kept as the old private name so nothing that already calls it changes behaviour.
_attachments_enabled = attachments_enabled


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
    # The intro deck is a LINK, never an attachment. Removed 2026-08-03.
    #
    # It used to attach ~/.applypilot/intro_deck.pdf to every outreach email — 3.1 MB riding
    # alongside a link to the same deck, so recipients got it twice. Three reasons it is gone:
    # a multi-megabyte attachment from an unknown sender is a spam-filter magnet; the link is
    # the only version whose opens can be counted (`/intro/<name>`, see domain/deck.py); and a
    # deck that changes on the site is stale in every inbox it was ever mailed to.
    #
    # It was also on by ACCIDENT. `OUTREACH_ATTACH_DECK` defaulted to "1" here while
    # settings.py declared the default False, so `doctor --config` reported it off while all 34
    # sent emails carried it — a default living in two places is two defaults (§Lessons 21).
    # The résumé and cover letter still attach; those are per-job and genuinely wanted.
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


def attach_ics(msg: EmailMessage, ics: str | None) -> None:
    """Attach a calendar invitation as `text/calendar; method=REQUEST`.

    The `method` PARAMETER is what makes a mail client render RSVP buttons instead of a file to
    download — the same bytes without it are an attachment nobody opens. Sent as `invite.ics`
    because clients that do not understand the part still show something recognisable.

    `add_attachment` with maintype/subtype base64-encodes the payload, which is correct here:
    the format mandates CRLF line endings, and quoted-printable or 8bit transports rewrite line
    endings in ways that corrupt folded lines.
    """
    if not (ics or "").strip():
        return
    msg.add_attachment(ics.encode("utf-8"), maintype="text", subtype="calendar",
                       filename="invite.ics", params={"method": "REQUEST",
                                                      "charset": "UTF-8"})


def _smtp_send(to_addr: str, subject: str, body: str, message_id: str,
               attachments: list[tuple[str, str]] | None = None,
               in_reply_to: str | None = None, cc: list[str] | None = None,
               references: str | None = None, ics: str | None = None) -> None:
    """Send one email over SMTP_SSL. `body` is sent verbatim.

    `send_message` derives the envelope from To/Cc, so a Cc'd person really is delivered to.
    """
    addr, pw = _creds()
    from_name = os.environ.get("OUTREACH_FROM_NAME", "")
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, addr)) if from_name else addr
    msg["To"] = to_addr
    if cc:
        msg["Cc"] = ", ".join(c for c in cc if c)
    msg["Reply-To"] = addr
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    msg.set_content(body)
    sig = signature_html(addr)
    if sig:
        from applypilot.networking.gmail_oauth import _body_to_html
        msg.add_alternative(f"<div>{_body_to_html(body)}</div><br>{sig}", subtype="html")
    attach_pdfs(msg, attachments)
    attach_ics(msg, ics)

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
    # ARCH-3: ladder state comes from `touches`, and the two lifecycles it used to conflate
    # are now separate — an in-flight touch is `touch_status`, a halted ladder is
    # `sequence_status`. The old single column made these one condition by accident.
    from applypilot.networking import touches as _touches
    _touches.init_touches()
    ladder = _touches.ladder_state(contact_id, "email")
    if ladder["touch_status"] == "sending":
        return {"ok": False, "message": "a follow-up is already sending"}
    if ladder["sequence_status"] in ("stopped", "replied"):
        return {"ok": False, "message": f"sequence {ladder['sequence_status']} — not sending"}

    subject = (ladder["draft_subject"] or "").strip()
    body = (ladder["draft_body"] or "").strip()
    if not body:
        return {"ok": False, "message": "no follow-up draft — generate one first"}
    to_addr = (contact.get("email") or "").strip()
    if not to_addr:
        return {"ok": False, "message": "no email address"}
    if _DAILY_LIMIT > 0 and store.sent_today() >= _DAILY_LIMIT:
        return {"ok": False, "message": f"daily send limit reached ({_DAILY_LIMIT})"}
    # The cap counts follow-ups too — they are the reason it is needed. Live data: 43 follow-ups
    # against 34 first contacts, so most of the volume reaching any one employer is chasing.
    company = (contact.get("company") or "").strip()
    if company and _COMPANY_CAP > 0:
        n = store.emails_sent_to_company(company)
        if n >= _COMPANY_CAP:
            return {"ok": False,
                    "message": (f"{company} has already had {n} emails from you "
                                f"(cap {_COMPANY_CAP}) — stop here or raise "
                                f"OUTREACH_COMPANY_CAP deliberately")}

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


def send_reply(contact_id: str, body: str, subject: str = "", cc: list[str] | None = None,
               dry_run: bool = False, conn=None) -> dict:
    """Answer a live conversation from the dashboard, in-thread, keeping the Cc.

    Separate from `send_followup` for a reason that is not stylistic: a follow-up is a ladder
    step — it has a touch number, a schedule, a stop condition and a per-position prompt. A
    reply is none of those. It answers a person who wrote to us, so it is bounded by the
    conversation rather than by `FOLLOWUP_SCHEDULE`, and sending one must not consume a touch.

    The recipients come from `domain.conversations.reply_target()` — from the LAST INBOUND
    message, not from the contact row. That is what carries the Cc forward, which is the entire
    point: Victoria answered by Cc'ing David, and a reply addressed only to the contact drops
    the person now handling the application without any visible sign that it did.

    No attachments: the résumé went with email #1, and re-attaching it to a live conversation
    reads as automated.
    """
    from applypilot.domain import conversations as cv
    from applypilot.networking import messages as msg_store

    body = (body or "").strip()
    if not body:
        return {"ok": False, "message": "nothing to send — write a reply first"}

    contact = store.get_contact(contact_id, conn)
    if not contact:
        return {"ok": False, "message": "contact not found"}

    thread = msg_store.thread_for_contact(contact_id, conn)
    target = cv.reply_target(thread, _our_addresses())
    if not target:
        return {"ok": False,
                "message": "no inbound message to reply to — use a follow-up instead"}

    to_addr = target["to_addr"]
    # An operator-supplied Cc wins (they can drop someone from the composer); `None` means
    # "unchanged", which is NOT the same as an empty list meaning "send to nobody else".
    cc_list = list(target["cc"]) if cc is None else [c for c in cc if (c or "").strip()]
    subject = (subject or target["subject"] or "").strip()

    if _DAILY_LIMIT > 0 and store.sent_today() >= _DAILY_LIMIT:
        return {"ok": False, "message": f"daily send limit reached ({_DAILY_LIMIT})"}
    if dry_run:
        return {"ok": True, "message": f"dry-run: would reply to {to_addr}"
                                       + (f", cc {', '.join(cc_list)}" if cc_list else "")}

    mode = transport()
    from_name = os.environ.get("OUTREACH_FROM_NAME", "")
    try:
        if mode == "oauth":
            from applypilot.networking import gmail_oauth
            from_addr = _from_address() or gmail_oauth.connected_email()
            sent = gmail_oauth.send(to_addr, subject, body, from_addr, from_name,
                                    thread_id=target["thread_id"] or contact.get("thread_id"),
                                    in_reply_to=target["in_reply_to"] or None,
                                    cc=cc_list, references=target["references"] or None)
            sent["from_addr"] = from_addr
        else:
            addr, _ = _creds()
            mid = make_msgid(domain=(addr.split("@")[-1] if "@" in addr else None))
            _smtp_send(to_addr, subject, body, mid, in_reply_to=target["in_reply_to"] or None,
                       cc=cc_list, references=target["references"] or None)
            sent = {"id": mid, "rfc_message_id": mid,
                    "thread_id": contact.get("thread_id") or "", "from_addr": addr}
    except Exception as e:  # noqa: BLE001
        log.warning("Reply to %s failed: %s", to_addr, e)
        return {"ok": False, "message": f"reply failed: {e}"}

    # Store it now rather than at the next poll: otherwise Send visibly does nothing.
    try:
        msg_store.record_outbound(contact, sent, to_addr, cc_list, subject, conn, body=body)
    except Exception:  # noqa: BLE001
        log.debug("Could not record the sent reply", exc_info=True)

    also = f" (cc {', '.join(cc_list)})" if cc_list else ""
    return {"ok": True, "message": f"replied to {to_addr}{also}",
            "to": to_addr, "cc": cc_list}


#: An invitation WE sent. Beside `CONNECTED` and `SENT` in `domain/interactions.py`, weight 0:
#: proposing a time is our own action, and counting it as engagement is the bug that made three
#: jobs read "3/3 engaged" before anyone had done anything (§Lessons 35). Them ACCEPTING is a
#: different event and arrives, if at all, as a detected booking.
INVITED_KIND = "invited"


def _profile_name() -> str:
    """The sender's name as the RECIPIENT knows it, for the ORGANIZER line.

    `preferred_name` wins over the first given name — see `invite.sender_name`, which exists
    because the live profile is "Jorge Alejandro Diez" / "Alejandro" and the naive version put a
    name into a calendar entry that no recipient had ever seen.

    Falls back to "" rather than raising: a missing profile must not stop an invitation, and
    `build_ics` uses the address instead.
    """
    try:
        from applypilot.config import load_profile
        from applypilot.domain.invite import sender_name
        p = load_profile().get("personal", {})
        return sender_name(p.get("full_name") or "", p.get("preferred_name") or "")
    except Exception:  # noqa: BLE001
        return ""


def send_invite(contact_id: str, start, minutes: int, summary: str = "", body: str = "",
                location: str = "", dry_run: bool = False, conn=None) -> dict:
    """Send a calendar invitation to one contact. Operator-initiated, never automatic.

    Modelled on `send_reply` rather than on `send_outreach`, and the difference is which guards
    apply. The DAILY LIMIT applies — it protects the mailbox's real quota, and this consumes it.
    The per-company cap does NOT: that exists because a company should see one sender making one
    approach, and it is counted over cold outreach. An invitation is a deliberate act aimed at a
    specific person, usually one who has already answered, and blocking it because seven of their
    colleagues got a cold email would refuse the one message the whole ladder exists to produce.

    Threaded into the conversation when there is one. An invite that arrives as a fresh thread,
    detached from the exchange where the time was agreed, reads as machine-sent.

    **SEQUENCE comes from how many invites this contact has already had.** Same UID with a higher
    sequence is how iCalendar says "this replaces the one I sent you", so re-sending after a
    change MOVES the meeting in their calendar instead of leaving two. Counting the prior ones
    from `interactions` costs no new column.
    """
    from applypilot.domain import invite as inv
    from applypilot.networking import interactions_store as ix
    from applypilot.networking import messages as msg_store

    contact = store.get_contact(contact_id, conn)
    if not contact:
        return {"ok": False, "message": "contact not found"}
    to_addr = (contact.get("email") or "").strip()
    if not to_addr:
        return {"ok": False, "message": "no email address for this contact — add one first"}

    from_addr = _from_address() or ""
    from_name = os.environ.get("OUTREACH_FROM_NAME", "") or _profile_name()
    if not from_addr:
        try:
            from applypilot.networking import gmail_oauth
            from_addr = gmail_oauth.connected_email()
        except Exception:  # noqa: BLE001 — resolved below into a real refusal
            from_addr = ""
    if not from_addr:
        return {"ok": False, "message": "no sending address — connect Gmail first"}

    who = contact.get("full_name") or to_addr
    summary = (summary or "").strip() or inv.default_summary(from_name, who)
    prior = [r for r in ix.for_contact(contact_id, conn) if r.get("kind") == INVITED_KIND]
    try:
        ics = inv.build_ics(
            uid=inv.uid_for(contact_id, from_addr), start=start, minutes=minutes,
            summary=summary, organiser_name=from_name, organiser_email=from_addr,
            attendee_name=who, attendee_email=to_addr,
            description=(body or "").strip(), location=(location or "").strip(),
            sequence=len(prior))
    except inv.InviteError as e:
        return {"ok": False, "message": str(e)}

    if _DAILY_LIMIT > 0 and store.sent_today() >= _DAILY_LIMIT:
        return {"ok": False, "message": f"daily send limit reached ({_DAILY_LIMIT})"}
    if dry_run:
        return {"ok": True, "message": f"dry-run: would invite {to_addr}", "ics": ics}

    # In-thread when a conversation exists. `thread_for_contact` is already loaded elsewhere on
    # this path; a missing thread simply means a standalone message, not a failure.
    thread_id = contact.get("thread_id") or ""
    in_reply_to = contact.get("rfc_message_id") or ""
    try:
        thread = msg_store.thread_for_contact(contact_id, conn)
        from applypilot.domain import conversations as cv
        target = cv.reply_target(thread, _our_addresses())
        if target:
            thread_id = target["thread_id"] or thread_id
            in_reply_to = target["in_reply_to"] or in_reply_to
    except Exception:  # noqa: BLE001 — threading is a nicety; never block the send on it
        log.debug("Could not resolve a thread for the invite", exc_info=True)

    text = (body or "").strip() or f"Sending an invite for {summary}. Let me know if another time is better."
    mode = transport()
    try:
        if mode == "oauth":
            from applypilot.networking import gmail_oauth
            sent = gmail_oauth.send(to_addr, summary, text, from_addr, from_name,
                                    thread_id=thread_id or None,
                                    in_reply_to=in_reply_to or None, ics=ics)
        else:
            addr, _ = _creds()
            mid = make_msgid(domain=(addr.split("@")[-1] if "@" in addr else None))
            _smtp_send(to_addr, summary, text, mid, in_reply_to=in_reply_to or None, ics=ics)
            sent = {"id": mid, "rfc_message_id": mid, "thread_id": thread_id}
    except Exception as e:  # noqa: BLE001
        log.warning("Invite to %s failed: %s", to_addr, e)
        return {"ok": False, "message": f"invite failed: {e}"}

    when_txt = start.isoformat()
    # Our own action, so `source='manual'` and a kind that carries NO engagement weight — the
    # same rule that keeps a LinkedIn invite out of the engagement count (§Lessons 35). Us
    # proposing a time is not them agreeing to one; a detected cal.com booking still is.
    try:
        ix.record(contact_id, INVITED_KIND, at=when_txt,
                  detail=f"{summary} ({minutes} min)", source="manual",
                  job_url=contact.get("job_url", ""), conn=conn)
        store.log_contact_event(contact_id, "info",
                                f"Sent a calendar invite to {who}: {summary}, {when_txt}.", conn)
    except Exception:  # noqa: BLE001 — logging must never break a send
        log.debug("Could not record the invite", exc_info=True)

    return {"ok": True, "message": f"invite sent to {to_addr}", "to": to_addr,
            "sent_id": sent.get("id", ""),
            "organiser_link": inv.organiser_link(
                start=start, minutes=minutes, summary=summary,
                attendee_email=to_addr, description=text, location=location)}


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

"""LLM outreach drafting — a short, specific email per contact.

Reuses the multi-provider LLM client and the tailor JSON-extraction + sanitize
helpers. Produces {subject, body}; the user reviews/edits before any send (NET-4).
"""

from __future__ import annotations

import logging
import os

from applypilot.llm import get_client
from applypilot.scoring.tailor import extract_json
from applypilot.scoring.validator import sanitize_text

log = logging.getLogger(__name__)

_LINKEDIN_LIMIT = 300

_SYSTEM = """You write short, casual networking messages for a job seeker reaching out to
someone at a company they just applied to. Think: a friendly, real message you'd actually
send another human — warm, a little personable, genuinely curious. NOT a cover letter, NOT a
hard sell, NOT corporate.

Voice:
- Casual and conversational. Use contractions ("I'm", "I'd", "it's"). Sound like a real person,
  not a template.
- A touch of genuine warmth or personality is great — keep it grounded, never cheesy or fake.
- Absolutely no buzzwords, no "I hope this finds you well", no "I am writing to", no
  "leverage/synergy/circle back". If it sounds like HR wrote it, rewrite it.
- Never invent facts about the sender, and don't flatter the recipient with made-up specifics.
- NEVER attach a number of years to a specific tool or framework unless the profile explicitly
  says so. A total career length is TOTAL experience — never "N years of PyTorch/LangChain/etc."
  Prefer honest framing like "the last few years focused on AI engineering" over false tenure.

Produce TWO things:

1. An EMAIL (subject + body):
   - 3–4 short sentences. Open warm and human, not stiff.
   - Name the SPECIFIC role the sender applied to and the company, plus one real, relevant thing
     about the sender (from their profile).
   - CALL TO ACTION: invite them to a quick call to connect. If a SCHEDULING LINK is provided
     below, weave it in naturally so they can grab a time directly (e.g. "if you're open to a
     quick call, grab a time that works here: <link>"). If no link is provided, just suggest a
     short call/chat. Keep it low-pressure, not pushy.
   - Sign off casually with the sender's first name only. No signature block. The ONLY link
     allowed is the scheduling link (when provided).
   - Subject: short, casual, specific (e.g. "quick q about the <role> role").

2. A LINKEDIN connection note (linkedin_note):
   - MUST be 300 characters or fewer (hard limit — count carefully, aim for ~230).
   - 1–2 warm sentences. Shorter and friendlier than the email; it's a connection request note.
   - Mention the role + a quick genuine hook, and that you'd love to connect and maybe find a
     time to chat. Do NOT paste the scheduling link here (LinkedIn connect notes strip/penalize
     links and space is tight) — just express interest in connecting/talking.
   - Sign with the first name.

If the user provides a STYLE DIRECTION below, follow it closely while keeping the messages
honest, casual, and concise.

Return ONLY a JSON object: {"subject": "...", "body": "...", "linkedin_note": "..."}"""


def _scheduling_link(profile: dict) -> str:
    """The sender's calendar/scheduling link (Calendly, cal.com, Google appt schedule, …).

    Priority: SCHEDULING_LINK env → profile['personal']['scheduling_link'] → ''. When present, the
    email CTA invites a call and includes this link so recipients can book a time directly.
    """
    import os
    return (os.environ.get("SCHEDULING_LINK", "").strip()
            or ((profile or {}).get("personal", {}).get("scheduling_link") or "").strip())


def _resolve_style(profile: dict, style: str = "") -> str:
    """The custom style directive, in priority order: explicit arg → env → profile field."""
    return (
        (style or "").strip()
        or os.environ.get("OUTREACH_STYLE", "").strip()
        or ((profile or {}).get("outreach_style") or "").strip()
    )


def _sender_name(profile: dict) -> str:
    p = (profile or {}).get("personal", {})
    full = p.get("preferred_name") or p.get("full_name") or ""
    return full.split()[0] if full else "there"


def draft_email(profile: dict, job: dict, contact: dict, style: str = "", warm: bool = False) -> dict:
    """Return {"subject": str, "body": str} for one contact. Raises on LLM/parse failure.

    `style` is an optional free-text directive (e.g. "keep it super casual", "mention I'm a
    Longhorn", "make it a little witty") that steers the tone. Falls back to OUTREACH_STYLE env
    or profile["outreach_style"] via _resolve_style.

    `warm=True` = the HOT layer: this person is an EXISTING 1st-degree LinkedIn connection at the
    company. The copy should acknowledge the existing relationship (reconnect, not cold intro),
    and the LinkedIn note becomes a direct MESSAGE to a connection (not a connect request).
    """
    role = job.get("title") or "the role"
    company = contact.get("company") or job.get("company") or job.get("site") or "your company"
    personal = (profile or {}).get("personal", {})
    experience = (profile or {}).get("experience", {})

    sender_bits = [
        f"Sender name: {personal.get('full_name', '')}",
        f"Sender first name: {_sender_name(profile)}",
    ]
    # Prefer the LinkedIn-derived background (accurate, from the real profile) over loose skill
    # lists — this is what keeps the copy TRUE (no "10 years of PyTorch" when that's the total
    # career length). The About + recent roles give the model real, groundable facts to draw on.
    li = (profile or {}).get("linkedin") or {}
    if li.get("about") or li.get("roles"):
        if li.get("headline"):
            sender_bits.append(f"Sender headline: {li['headline']}")
        if li.get("about"):
            sender_bits.append(f"Sender background (LinkedIn About): {li['about']}")
        roles = li.get("roles") or []
        if roles:
            recent = "; ".join(f"{r.get('title','')} at {r.get('company','')} ({r.get('dates','')})" for r in roles[:4])
            sender_bits.append(f"Recent roles: {recent}")
        if li.get("positioning"):
            sender_bits.append(f"IMPORTANT framing (do not misstate): {li['positioning']}")
    else:
        # Fallback to the older fields only if no LinkedIn block is present.
        skills = (profile or {}).get("skills_boundary", {})
        sender_bits += [
            f"Sender target role: {experience.get('target_role', '')}",
            f"Total years of experience: {experience.get('years_of_experience_total', '')}",
            f"Sender skills: {', '.join((skills.get('frameworks') or []))[:200]}",
        ]
    jd = (job.get("full_description") or "")[:1200]

    directive = _resolve_style(profile, style)
    style_block = f"STYLE DIRECTION (follow closely):\n{directive}\n\n" if directive else ""

    if warm:
        relationship = ("EXISTING 1st-degree LinkedIn connection who currently works at the company "
                        "(you already know each other).")
        warm_block = (
            "WARM / HOT OUTREACH — you are ALREADY CONNECTED with this person on LinkedIn and they "
            "work at this company. Write it as reconnecting with someone you know, NOT a cold intro:\n"
            "- Open warmly and acknowledge you're already connected (e.g. 'Hope you're doing well!' / "
            "'It's been a while'). Do NOT introduce yourself as a stranger.\n"
            "- Mention you just applied for the role at their company and would love their read on it, "
            "an internal referral, or just to reconnect.\n"
            "- The LinkedIn note is a DIRECT MESSAGE to an existing connection (NOT a connection "
            "request) — it can be a bit longer/warmer and does not need the 300-char connect-note "
            "limit framing, though still keep it concise.\n\n"
        )
    else:
        relationship = contact.get("match_reason", "works at the company")
        warm_block = ""

    link = _scheduling_link(profile)
    sched_block = (
        f"SCHEDULING LINK (include in the EMAIL CTA so they can book a call directly): {link}\n\n"
        if link else
        "SCHEDULING LINK: none provided — invite a quick call/chat without a link.\n\n"
    )

    user = (
        "SENDER:\n" + "\n".join(sender_bits) + "\n\n"
        "TARGET CONTACT:\n"
        f"Name: {contact.get('full_name', '')}\n"
        f"Title: {contact.get('title', '')}\n"
        f"Relationship: {relationship}\n\n"
        f"JOB APPLIED TO:\nRole: {role}\nCompany: {company}\n"
        f"Description (excerpt):\n{jd}\n\n"
        + sched_block + warm_block + style_block +
        "Write the outreach email. Return the JSON."
    )

    client = get_client("light")
    raw = client.chat(
        [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
        max_tokens=400, temperature=0.8,  # a bit higher for warmth/variety
    )
    data = extract_json(raw)
    subject = sanitize_text(str(data.get("subject", ""))).strip()
    body = sanitize_text(str(data.get("body", ""))).strip()
    note = sanitize_text(str(data.get("linkedin_note", ""))).strip()
    if not subject:
        subject = f"Question about the {role} role"
    if not body:
        raise ValueError("empty outreach body")
    note = _cap_linkedin(note)
    return {"subject": subject, "body": body, "linkedin_note": note}


def _cap_linkedin(note: str) -> str:
    """Enforce LinkedIn's 300-char note limit (inclusive), trimming at a word boundary."""
    if len(note) <= _LINKEDIN_LIMIT:
        return note
    cut = note[:_LINKEDIN_LIMIT - 1]  # leave room for the ellipsis
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,;:-") + "…"

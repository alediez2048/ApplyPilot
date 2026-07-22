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
   - A light, low-pressure ask: a quick chat, or a genuine question about the team/role.
   - Sign off casually with the sender's first name only. No signature block, no links.
   - Subject: short, casual, specific (e.g. "quick q about the <role> role").

2. A LINKEDIN connection note (linkedin_note):
   - MUST be 300 characters or fewer (hard limit — count carefully, aim for ~230).
   - 1–2 warm sentences. Shorter and friendlier than the email; it's a connection request note.
   - Mention the role + a quick genuine hook, and that you'd love to connect.
   - Sign with the first name. No links.

If the user provides a STYLE DIRECTION below, follow it closely while keeping the messages
honest, casual, and concise.

Return ONLY a JSON object: {"subject": "...", "body": "...", "linkedin_note": "..."}"""


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


def draft_email(profile: dict, job: dict, contact: dict, style: str = "") -> dict:
    """Return {"subject": str, "body": str} for one contact. Raises on LLM/parse failure.

    `style` is an optional free-text directive (e.g. "keep it super casual", "mention I'm a
    Longhorn", "make it a little witty") that steers the tone. Falls back to OUTREACH_STYLE env
    or profile["outreach_style"] via _resolve_style.
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

    user = (
        "SENDER:\n" + "\n".join(sender_bits) + "\n\n"
        "TARGET CONTACT:\n"
        f"Name: {contact.get('full_name', '')}\n"
        f"Title: {contact.get('title', '')}\n"
        f"Relationship: {contact.get('match_reason', 'works at the company')}\n\n"
        f"JOB APPLIED TO:\nRole: {role}\nCompany: {company}\n"
        f"Description (excerpt):\n{jd}\n\n"
        + style_block +
        "Write the outreach email. Return the JSON."
    )

    client = get_client()
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

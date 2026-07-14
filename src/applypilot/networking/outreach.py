"""LLM outreach drafting — a short, specific email per contact.

Reuses the multi-provider LLM client and the tailor JSON-extraction + sanitize
helpers. Produces {subject, body}; the user reviews/edits before any send (NET-4).
"""

from __future__ import annotations

import logging

from applypilot.llm import get_client
from applypilot.scoring.tailor import extract_json
from applypilot.scoring.validator import sanitize_text

log = logging.getLogger(__name__)

_SYSTEM = """You write short, genuine networking emails for a job seeker reaching out to
someone at a company they just applied to. Goal: a brief, human message that could start a
conversation — NOT a hard sell.

Rules:
- 3–4 sentences max. Plain, direct voice. No buzzwords, no "I hope this finds you well".
- Name the SPECIFIC role the sender applied to and the company.
- One concrete, relevant point about the sender (from their profile) — never invent facts.
- A soft ask: a brief chat, or a question about the team/role.
- Sign with the sender's first name only. No signature block, no links.
- Subject: specific and low-key (e.g. "Question about the <role> role").

Return ONLY a JSON object: {"subject": "...", "body": "..."}"""


def _sender_name(profile: dict) -> str:
    p = (profile or {}).get("personal", {})
    full = p.get("preferred_name") or p.get("full_name") or ""
    return full.split()[0] if full else "there"


def draft_email(profile: dict, job: dict, contact: dict) -> dict:
    """Return {"subject": str, "body": str} for one contact. Raises on LLM/parse failure."""
    role = job.get("title") or "the role"
    company = contact.get("company") or job.get("company") or job.get("site") or "your company"
    personal = (profile or {}).get("personal", {})
    experience = (profile or {}).get("experience", {})
    skills = (profile or {}).get("skills_boundary", {})

    sender_bits = [
        f"Sender name: {personal.get('full_name', '')}",
        f"Sender first name: {_sender_name(profile)}",
        f"Sender target role: {experience.get('target_role', '')}",
        f"Years of experience: {experience.get('years_of_experience_total', '')}",
        f"Sender skills: {', '.join(skills.get('languages', []) + skills.get('frameworks', []))[:200]}",
    ]
    jd = (job.get("full_description") or "")[:1200]

    user = (
        "SENDER:\n" + "\n".join(sender_bits) + "\n\n"
        "TARGET CONTACT:\n"
        f"Name: {contact.get('full_name', '')}\n"
        f"Title: {contact.get('title', '')}\n"
        f"Relationship: {contact.get('match_reason', 'works at the company')}\n\n"
        f"JOB APPLIED TO:\nRole: {role}\nCompany: {company}\n"
        f"Description (excerpt):\n{jd}\n\n"
        f"Write the outreach email. Return the JSON."
    )

    client = get_client()
    raw = client.chat(
        [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
        max_tokens=400, temperature=0.6,
    )
    data = extract_json(raw)
    subject = sanitize_text(str(data.get("subject", ""))).strip()
    body = sanitize_text(str(data.get("body", ""))).strip()
    if not subject:
        subject = f"Question about the {role} role"
    if not body:
        raise ValueError("empty outreach body")
    return {"subject": subject, "body": body}

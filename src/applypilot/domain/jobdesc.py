"""Pull the parts of a job description worth putting in an email.

Outreach used `full_description[:1200]`. On a real posting that window is spent almost entirely
on things nobody wants quoted back at them. Measured on a live Affirm description:

    0–180    "Affirm is reinventing credit to make it more honest and friendly…"   company boilerplate
    180–520  "About the Team: People Tech & Analytics builds and owns…"            team background
    520+     "About the Role: You will build, deploy and maintain…"                ← the actual job

So the model was handed the mission statement and the org chart, and the sentence that says what
the person actually does began right where the budget ran out. The resulting emails were
enthusiastic and non-specific, because that is all the input supported.

This keeps the sections a candidate would answer and drops the ones every posting shares.
Deterministic and pure — no LLM call to decide what an LLM should read, which would double the
cost of every draft to save a paragraph.
"""

from __future__ import annotations

import re

#: Headers whose content says what the job IS. Matched as a prefix of the header line, lowercased.
_KEEP = (
    "about the role", "the role", "role overview", "what you'll do", "what you will do",
    "what you’ll do", "what you'll be doing", "responsibilities", "key responsibilities",
    "your impact", "the impact", "in this role", "day to day", "day-to-day",
    "the opportunity", "what you'll own", "what you will own", "who you are",
    "requirements", "qualifications", "what you'll bring", "what you will bring",
    "what you’ll bring", "what we're looking for", "what we are looking for",
    "minimum qualifications", "basic qualifications", "preferred qualifications",
    "about the team", "the team", "our team",
)

#: Headers every posting shares. Quoting any of these back is the definition of generic.
#: Written as STEMS, because these headers vary more than they look: "Equal Opportunities at
#: Arm" is not matched by "equal opportunity", and that one missed plural leaked 405 characters
#: of EEO text into a cold email.
_DROP = (
    "about us", "about the company", "who we are", "our mission", "our values", "why join",
    "why work", "benefit", "perk", "what we offer", "compensation", "salary", "pay range",
    "pay transparency", "equal opportunit", "equal employment", "eeo", "diversity",
    "inclusion", "accommodation", "how to apply", "application process", "privacy", "legal",
    "disclaimer", "e-verify", "background check", "visa", "sponsorship", "covid",
    "hybrid working", "remote work", "work arrangement", "our commitment", "note to",
    "recruitment agenc", "agency notice",
)

#: A header line: short, its own line, usually ending in a colon. The colon is optional because
#: plenty of postings bold the header instead, and bold does not survive text extraction.
_HEADER = re.compile(r"^\s{0,4}(?:#+\s*|\*\*)?([A-Z][^.!?]{2,60}?)\s*:?\s*(?:\*\*)?$")


def _classify(header: str) -> str:
    h = header.strip().lower().rstrip(":").strip()
    # DROP wins ties: "About the company culture" is boilerplate even though "the c…" is close
    # to nothing in _KEEP. Checking drop first also stops "about the role" matching "about us".
    for d in _DROP:
        if h.startswith(d):
            return "drop"
    for k in _KEEP:
        if h.startswith(k):
            return "keep"
    return "unknown"


def split_sections(text: str) -> list[tuple[str, str]]:
    """[(header, body)] in document order. The lead-in before any header gets header ''."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[tuple[str, list[str]]] = [("", [])]
    for line in lines:
        stripped = line.strip()
        m = _HEADER.match(line) if stripped else None
        # A "header" that is really a sentence, or a bullet, is not a header. Requiring few
        # words is what stops a one-line paragraph from splitting the document into confetti.
        if m and len(stripped.split()) <= 8 and not stripped.startswith(("-", "•", "*", "·")):
            out.append((m.group(1).strip(), []))
        else:
            out[-1][1].append(line)
    return [(h, "\n".join(b).strip()) for h, b in out]


def role_essentials(text: str | None, limit: int = 2600) -> str:
    """The parts of `text` that describe the job, capped at `limit` characters.

    Never returns empty when there is input: an unrecognisable posting falls back to the plain
    truncation this replaced, because a draft with a thin description beats no draft at all.
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    # No length gate. There used to be one — "short postings are already all signal" — and it
    # was wrong: a 1,500-character posting can still be half benefits and EEO text, and the gate
    # silently turned the whole feature off for it. The `if not joined` fallback below already
    # covers the case this was really hedging against, which is a posting we cannot parse.
    sections = split_sections(raw)
    kept: list[str] = []
    for header, body in sections:
        if not body.strip():
            continue
        kind = _classify(header) if header else "lead"
        if kind == "drop":
            continue
        if kind == "lead":
            # The opening paragraph before any header is the company pitch far more often than
            # it is the job. Skipped, but only when the document HAS headers to fall back on.
            if any(h for h, _ in sections):
                continue
            kept.append(body)
            continue
        # Unknown headers are KEPT. They were capped at one, which cost the Arm posting its
        # "Required Skills & Experience" section — 1,463 characters of exactly what an email
        # should reference — because "Job Overview" had already spent the single slot. The cap
        # was hedging against boilerplate leaking; that is the drop list's job, and a section
        # this list has not seen before is far more often the role than a mission statement.
        kept.append(f"{header}\n{body}" if header else body)

    joined = "\n\n".join(k.strip() for k in kept if k.strip()).strip()
    if not joined:
        return raw[:limit]
    if len(joined) <= limit:
        return joined
    # Cut at a paragraph boundary rather than mid-sentence — a description that stops halfway
    # through a responsibility invites the model to finish the thought itself.
    cut = joined[:limit]
    for sep in ("\n\n", ". ", "\n"):
        i = cut.rfind(sep)
        if i > limit * 0.6:
            return cut[:i].strip()
    return cut.strip()

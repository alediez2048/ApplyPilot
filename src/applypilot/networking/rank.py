"""Pure ranking + title-synonym helpers for contact selection.

Ranks Apollo candidates (which are masked — no email/LinkedIn yet) purely on title/
seniority, and picks a useful mix: peers in the role plus at least one recruiter / hiring
manager. No I/O — trivially testable.
"""

from __future__ import annotations

import re

_RECRUITER_TITLES = [
    "Technical Recruiter", "Recruiter", "Talent Acquisition", "Talent Partner",
    "Sourcer", "People Operations", "Head of Talent",
]

# Words that mark a hiring-side contact.
_RECRUITER_RE = re.compile(
    r"\b(recruit|talent|sourc|people ops|people operations|hr\b|human resources)", re.I
)
_HIRING_MGR_RE = re.compile(r"\b(hiring manager|engineering manager|director|head of|vp|chief)\b", re.I)

_STOP = {"senior", "sr", "staff", "lead", "principal", "junior", "jr", "i", "ii", "iii",
         "the", "of", "and", "&", "a", "an", "at"}


def _tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if w not in _STOP and len(w) > 1}


def role_to_person_titles(job_title: str | None) -> list[str]:
    """Map a job title to Apollo person_titles[] + synonyms + recruiter titles."""
    titles: list[str] = []
    if job_title:
        base = job_title.strip()
        titles.append(base)
        # de-seniored variant (drop leading Senior/Staff/Lead/Principal)
        stripped = re.sub(r"^(senior|sr\.?|staff|lead|principal|junior|jr\.?)\s+", "", base, flags=re.I).strip()
        if stripped and stripped != base:
            titles.append(stripped)
    # always include recruiter/talent so a hiring contact surfaces
    titles.extend(_RECRUITER_TITLES[:3])
    # de-dup preserving order
    seen: set[str] = set()
    out = []
    for t in titles:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


def _match_reason(title: str | None, role: str | None, overlap: int) -> str:
    if title and _RECRUITER_RE.search(title):
        return "recruiter"
    if title and _HIRING_MGR_RE.search(title):
        return "hiring manager"
    if overlap >= 2:
        return "same role"
    return "same team"


def _score(candidate: dict, role_tokens: set[str]) -> tuple[int, int]:
    """(title overlap with role, seniority weight) — higher is better."""
    title = candidate.get("title") or ""
    overlap = len(_tokens(title) & role_tokens)
    seniority = (candidate.get("seniority") or "").lower()
    weight = {"c_suite": 1, "vp": 2, "head": 3, "director": 4, "manager": 5,
              "senior": 6, "entry": 4, "intern": 1}.get(seniority, 5)
    return (overlap, weight)


def select(candidates: list[dict], role: str | None, n: int = 5) -> list[dict]:
    """Pick the best up-to-n candidates: relevant peers + ≥1 recruiter/hiring contact.

    Returns candidates annotated with `match_reason`, ordered best-first.
    """
    if not candidates:
        return []
    role_tokens = _tokens(role)

    scored = []
    for c in candidates:
        title = c.get("title") or ""
        overlap = len(_tokens(title) & role_tokens)
        is_hiring = bool(_RECRUITER_RE.search(title) or _HIRING_MGR_RE.search(title))
        scored.append((c, overlap, is_hiring, _score(c, role_tokens)))

    # peers (non-hiring), ranked by title overlap then seniority
    peers = sorted(
        [s for s in scored if not s[2]], key=lambda s: s[3], reverse=True
    )
    hiring = sorted(
        [s for s in scored if s[2]], key=lambda s: s[3], reverse=True
    )

    chosen: list[tuple] = []
    # guarantee at least one hiring contact if available
    if hiring:
        chosen.append(hiring[0])
    # fill the rest with top peers, then remaining hiring
    for pool in (peers, hiring[1:]):
        for s in pool:
            if len(chosen) >= n:
                break
            if s not in chosen:
                chosen.append(s)

    out = []
    for c, overlap, _is_hiring, _sc in chosen[:n]:
        annotated = dict(c)
        annotated["match_reason"] = _match_reason(c.get("title"), role, overlap)
        out.append(annotated)
    return out

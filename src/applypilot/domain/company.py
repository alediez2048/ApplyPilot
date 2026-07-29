"""Is company A the same employer as company B?

A shared domain rule, used by three subsystems that must agree:
  - connections  — is this LinkedIn contact currently at the target company?
  - providers    — which of Apollo's fuzzy name matches is the real employer?
  - verification — does this person's employer contradict the job's?

It lived in `networking/connections.py` until ARCH-1, which meant `domain/` had to import
from `networking/` to answer a question that involves no network at all.
"""

from __future__ import annotations

import re

# Legal suffixes that never change which company is meant.
_COMPANY_SUFFIXES = re.compile(
    r"\b(inc|inc\.|llc|l\.l\.c\.|ltd|ltd\.|corp|corp\.|co|co\.|company|gmbh|plc|sa|nv|ag)\b",
    re.IGNORECASE,
)

# Generic words that trail a real company name without changing which company it is, so
# "Arm" and "Arm Holdings" are the same employer. Kept deliberately tight — anything not
# listed here (Bank, Corporation, Wrestling, Studios…) makes it a DIFFERENT company.
_CORPORATE_DESCRIPTORS = frozenset({
    "holdings", "holding", "group", "technologies", "technology", "tech", "labs",
    "laboratories", "systems", "solutions", "services", "international", "global",
    "worldwide", "ventures", "partners", "capital", "industries", "enterprises",
    "software", "digital", "media", "health", "healthcare", "consulting",
    "platforms", "networks", "communications", "brands", "motors",
})


def norm_company(s: str | None) -> str:
    """Lowercase, punctuation-stripped, legal-suffix-free form used for comparison."""
    base = re.sub(r"[^a-z0-9 &]", " ", (s or "").lower())
    base = _COMPANY_SUFFIXES.sub("", base)
    return re.sub(r"\s+", " ", base).strip()


def companies_match(a: str | None, b: str | None, strict: bool = False) -> bool:
    """True if two company strings name the same employer.

    Compares whole WORDS, never raw substrings. The rule this replaced was
    `a in b or b in a`, which made the 3-letter employer "Arm" match Armanino, Armadillo
    World Headquarters, State Farm, Dharma Capital and Centrient Pharmaceuticals
    (ph-ARM-aceuticals) — 6 false positives out of 8 "connections at Arm".

    Exact on the normalized token sequence, plus one narrow allowance: the longer name may
    add trailing corporate descriptors ("Arm" == "Arm Holdings"), because LinkedIn and job
    boards disagree about those. It must not add anything else, so "Apple" != "Apple Bank".

    `strict=True` drops that allowance. Choosing WHICH Apollo org to search needs it:
    "Affirm Health" and "Affirm Partners" are separate companies, and treating them as
    Affirm pulls a different payroll onto the job. Matching a person's self-reported
    employer is the opposite problem, where the allowance prevents false negatives.
    """
    ta, tb = norm_company(a).split(), norm_company(b).split()
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    if strict:
        return False
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if long_[:len(short)] != short:
        return False
    return all(w in _CORPORATE_DESCRIPTORS for w in long_[len(short):])

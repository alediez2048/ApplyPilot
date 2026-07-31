"""Does this person actually work at the employer we're applying to?

Contact discovery is a chain of fuzzy steps — job URL → employer name → domain → Apollo
org → people — and a wrong answer anywhere produces real humans who work somewhere else.
Four separate bugs shipped that way (Y Combinator's recruiters on a Hamming AI job;
Armanino and State Farm on an "Arm" job; Writer Corporation on a "Writer" job), and none
of them raised an error: every one looked like a perfectly good contact.

This checks each candidate against the evidence we already have BEFORE it reaches the
dashboard, so a bad match is dropped or flagged instead of silently presented. It is the
half that covers cases no test anticipated — the golden set in evals/ covers regressions.

Signals, strongest first:
  email domain    a work address at a different company is near-proof (writer.com vs
                  writercorporation.com). Absence proves nothing — many real people have
                  no address on file — so only a CONTRADICTION counts.
  org name        Apollo tells us the employer it has on file. This catches the people an
                  email check cannot, because they have no email at all.
"""

from __future__ import annotations

# Verdicts, in order of severity.
OK = "ok"            # positively corroborated
UNVERIFIED = "unverified"  # nothing contradicts, nothing confirms
REJECT = "reject"    # evidence says they work somewhere else


def email_domain_agrees(email: str | None, employer_domain: str) -> bool | None:
    """True/False when the address is decisive, None when there's nothing to judge."""
    addr = (email or "").strip().lower()
    domain = (employer_domain or "").strip().lower().removeprefix("www.")
    if not addr or "@" not in addr or not domain:
        return None
    got = addr.rsplit("@", 1)[-1]
    return got == domain or got.endswith("." + domain) or domain.endswith("." + got)


def org_name_agrees(contact_company: str | None, employer: str | None) -> bool | None:
    """True/False when both names are known, None otherwise.

    Uses the strict word-wise comparison: "Writer Corporation" and "Affirm Health" are
    different companies, not spelling variants of Writer and Affirm.
    """
    from applypilot.domain.company import companies_match
    a = (contact_company or "").strip()
    b = (employer or "").strip()
    if not a or not b:
        return None
    if companies_match(b, a, strict=True):
        return True
    # Fall back to the lenient rule before calling it a mismatch, so a legitimate
    # "Arm" vs "Arm Holdings" difference is not treated as a different employer.
    return True if companies_match(b, a) else False


def verify_contact(contact: dict, employer: str | None, employer_domain: str = "") -> dict:
    """Judge one candidate. Returns {verdict, confidence, reasons[]}.

    `confidence` is for display; `verdict` is what the caller acts on. Nothing here
    guesses — with no evidence either way the answer is UNVERIFIED, not a rejection,
    because dropping real contacts is worse than showing an unconfirmed one.
    """
    reasons: list[str] = []
    verdict = UNVERIFIED

    dom = email_domain_agrees(contact.get("email"), employer_domain)
    org = org_name_agrees(contact.get("company"), employer)

    if dom is False:
        got = (contact.get("email") or "").rsplit("@", 1)[-1]
        reasons.append(f"email is @{got}, not @{employer_domain}")
        verdict = REJECT
    if org is False:
        # Apollo returning this person FOR the employer's own domain is corroboration that
        # outranks a name mismatch: we did not find them by name, we found them by asking
        # "who works at this domain?" and Apollo answered.
        #
        # A live Avathon Government job dropped all three candidates — including the CEO —
        # because Apollo files avathongov.com people under the org name "Avathon", and
        # "Avathon" != "Avathon Government" (Government is not a corporate descriptor). The
        # people were right; the name comparison was the only thing that disagreed.
        #
        # This does NOT loosen name matching, which is why the pinned Writer Corporation cases
        # still reject: those candidates came from a fuzzy NAME search, so nothing corroborates
        # them and the org mismatch remains the only evidence there is.
        if contact.get("from_domain_search") and dom is not False:
            reasons.append(f"Apollo lists them at {contact.get('company')!r}, but returned them "
                           f"for @{employer_domain} — treating as unconfirmed, not wrong")
        else:
            reasons.append(f"Apollo lists them at {contact.get('company')!r}, not {employer!r}")
            verdict = REJECT

    if verdict != REJECT:
        if dom is True:
            reasons.append(f"work email is @{employer_domain}")
        if org is True:
            reasons.append(f"Apollo lists them at {employer!r}")
        verdict = OK if (dom is True or org is True) else UNVERIFIED

    confidence = {OK: "high", UNVERIFIED: "medium", REJECT: "low"}[verdict]
    return {"verdict": verdict, "confidence": confidence, "reasons": reasons}

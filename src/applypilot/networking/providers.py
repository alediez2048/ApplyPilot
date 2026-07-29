"""Contact-provider registry — Apollo.io (hyper-accurate title/department targeting).

`service.py` talks only to this module so the provider stays swappable. Apollo is the
sole provider: its masked people-search + bulk enrichment give precise, role-relevant
contacts (technical recruiters, hiring managers, peers) rather than "whoever has an email."

  active() -> "apollo" | None
  probe()  -> (ok, message)
  search(company, domain, role, titles, per_page) -> [candidate]   # each has "key"
  enrich(selected) -> { key: {email, email_status, linkedin_url} }
"""

from __future__ import annotations

import logging

from applypilot.networking import apollo

log = logging.getLogger(__name__)


def active() -> str | None:
    return "apollo" if apollo._api_key() else None


def available() -> bool:
    return active() is not None


def probe() -> tuple[bool, str]:
    if active() == "apollo":
        return apollo.probe()
    return False, "no contact provider — set APOLLO_API_KEY (paid plan required for API access)"


def resolve_orgs(company: str | None, per_page: int = 5) -> tuple[list[dict], str]:
    """Apollo orgs whose NAME really is `company`, plus the employer domain if agreed.

    Apollo's company search is fuzzy. "WRITER" comes back as five organizations — Writer
    (writer.com), Writer Corporation, The Writer, Content Writer, and a freelance resume
    writer — and searching all of them puts five companies' employees on one job. Match on
    whole words (companies_match) so only the real employer survives; "Writer Corporation"
    and "The Writer" are genuinely different companies, not variants.
    """
    if not company:
        return [], ""
    from applypilot.domain.company import companies_match
    orgs = apollo.company_lookup(company, per_page=per_page)
    strict = [o for o in orgs if companies_match(company, o.get("name"), strict=True)]
    lenient = [o for o in orgs if companies_match(company, o.get("name"))]
    # Exactly one match is the only case confident enough to also trust its domain for the
    # per-contact email cross-check.
    for candidates in (strict, lenient):
        if len(candidates) == 1:
            return candidates, candidates[0].get("domain", "")
    return (strict or lenient), ""


def search(company: str | None, domain: str | None, role: str | None,
           titles: list[str], per_page: int = 25) -> list[dict]:
    """Return ranked-ready candidates, each with a stable "key" field."""
    if active() != "apollo":
        return []
    org_ids: list[str] = []
    resolved_domain = ""
    if not domain and company:
        orgs, resolved_domain = resolve_orgs(company)
        org_ids = [o["id"] for o in orgs]
        if not orgs:
            # Apollo's name search does not always surface the real employer (asking for
            # "BetterUp" returns BetterUp Government and Better Up Now, but not BetterUp
            # itself). Fall back to the keyword search rather than finding nobody — the
            # per-contact email check below is the remaining guard.
            log.info("Apollo: no organization confidently matches %r — falling back to keywords",
                     company)
    cands = apollo.search_people(
        domains=[domain] if domain else None,
        organization_ids=org_ids or None,
        keywords=None if (domain or org_ids) else company,
        titles=titles,
        per_page=per_page,
    )
    for c in cands:
        c["key"] = c.get("apollo_id")
        c["employer_domain"] = domain or resolved_domain
    return cands


def enrich(selected: list[dict]) -> dict[str, dict]:
    """key -> {email, email_status, linkedin_url} for the selected candidates."""
    if active() != "apollo":
        return {}
    # apollo keys results by apollo_id, which equals candidate["key"]
    return apollo.bulk_enrich([c.get("apollo_id") for c in selected if c.get("apollo_id")])

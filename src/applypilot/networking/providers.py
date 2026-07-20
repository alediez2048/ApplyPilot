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

from applypilot.networking import apollo


def active() -> str | None:
    return "apollo" if apollo._api_key() else None


def available() -> bool:
    return active() is not None


def probe() -> tuple[bool, str]:
    if active() == "apollo":
        return apollo.probe()
    return False, "no contact provider — set APOLLO_API_KEY (paid plan required for API access)"


def search(company: str | None, domain: str | None, role: str | None,
           titles: list[str], per_page: int = 25) -> list[dict]:
    """Return ranked-ready candidates, each with a stable "key" field."""
    if active() != "apollo":
        return []
    org_ids = [] if domain else (apollo.company_search(company) if company else [])
    cands = apollo.search_people(
        domains=[domain] if domain else None,
        organization_ids=org_ids or None,
        keywords=None if (domain or org_ids) else company,
        titles=titles,
        per_page=per_page,
    )
    for c in cands:
        c["key"] = c.get("apollo_id")
    return cands


def enrich(selected: list[dict]) -> dict[str, dict]:
    """key -> {email, email_status, linkedin_url} for the selected candidates."""
    if active() != "apollo":
        return {}
    # apollo keys results by apollo_id, which equals candidate["key"]
    return apollo.bulk_enrich([c.get("apollo_id") for c in selected if c.get("apollo_id")])

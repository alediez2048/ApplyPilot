"""Contact-provider registry — Hunter.io or Apollo.io, interchangeable.

`service.py` talks only to this module. Selection order: an explicit
NETWORKING_PROVIDER env override, else the first provider with a key present
(Hunter preferred — cheaper, free-tier API). Both providers are normalized to:

  active() -> "hunter" | "apollo" | None
  probe()  -> (ok, message)
  search(company, domain, role, titles, per_page) -> [candidate]   # each has "key"
  enrich(selected) -> { key: {email, email_status, linkedin_url} }
"""

from __future__ import annotations

import os

from applypilot.networking import apollo, hunter

_ORDER = ["hunter", "apollo"]


def active() -> str | None:
    forced = os.environ.get("NETWORKING_PROVIDER", "").strip().lower()
    if forced == "hunter" and hunter.has_key():
        return "hunter"
    if forced == "apollo" and apollo._api_key():
        return "apollo"
    for p in _ORDER:
        if p == "hunter" and hunter.has_key():
            return "hunter"
        if p == "apollo" and apollo._api_key():
            return "apollo"
    return None


def available() -> bool:
    return active() is not None


def probe() -> tuple[bool, str]:
    a = active()
    if a == "hunter":
        return hunter.probe()
    if a == "apollo":
        return apollo.probe()
    return False, "no contact provider — set HUNTER_API_KEY or APOLLO_API_KEY"


def search(company: str | None, domain: str | None, role: str | None,
           titles: list[str], per_page: int = 25) -> list[dict]:
    """Return ranked-ready candidates, each with a stable "key" field."""
    a = active()
    if a == "hunter":
        return hunter.search_candidates(company, domain, per_page=per_page)
    if a == "apollo":
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
    return []


def enrich(selected: list[dict]) -> dict[str, dict]:
    """key -> {email, email_status, linkedin_url} for the selected candidates."""
    a = active()
    if a == "hunter":
        return hunter.enrich(selected)
    if a == "apollo":
        by_id = apollo.bulk_enrich([c.get("apollo_id") for c in selected if c.get("apollo_id")])
        # apollo keys results by apollo_id, which equals candidate["key"]
        return by_id
    return {}

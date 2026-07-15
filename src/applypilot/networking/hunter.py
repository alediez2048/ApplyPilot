"""Hunter.io client — a cheaper contact provider (free tier includes API access).

Hunter's Domain Search returns people at a company WITH their emails, titles,
seniority, department, and LinkedIn in a single call — so there is no separate
credit-consuming enrich step (unlike Apollo). It also accepts a company *name*
when we don't have the domain, which covers ATS-hosted jobs (greenhouse etc.).

Exposes the provider interface used by networking.providers:
  has_key(), probe(), search_candidates(company, domain, per_page), enrich(selected)

Fails soft: any error logs and returns empty so the pipeline degrades gracefully.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.hunter.io/v2"
_TIMEOUT = 25


def _api_key() -> str:
    return os.environ.get("HUNTER_API_KEY", "")


def has_key() -> bool:
    return bool(_api_key())


def probe() -> tuple[bool, str]:
    """Auth + remaining-quota check against the free /account endpoint."""
    if not _api_key():
        return False, "HUNTER_API_KEY not set"
    try:
        r = httpx.get(f"{BASE_URL}/account", params={"api_key": _api_key()}, timeout=_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        return False, f"Hunter unreachable: {e}"
    if r.status_code == 200:
        d = r.json().get("data", {})
        s = (d.get("requests", {}) or {}).get("searches", {}) or {}
        avail = s.get("available")
        used = s.get("used")
        plan = d.get("plan_name", "?")
        if avail is not None and used is not None:
            return True, f"Hunter ({plan}) — {max(0, avail - used)} searches left"
        return True, f"Hunter ({plan}) reachable"
    if r.status_code in (401, 403):
        return False, "Hunter key invalid/unauthorized"
    return False, f"Hunter HTTP {r.status_code}"


def _map_status(verification: dict | None) -> str:
    """Hunter verification.status -> internal {verified|unverified}."""
    status = (verification or {}).get("status")
    return "verified" if status == "valid" else "unverified"


def _clean_linkedin(val: str | None) -> str | None:
    if not val:
        return None
    v = str(val).strip()
    if v.startswith("http"):
        return v
    if "linkedin.com" in v:
        return "https://" + v.lstrip("/")
    return f"https://www.linkedin.com/in/{v}"


def search_candidates(company: str | None, domain: str | None, per_page: int = 25) -> list[dict]:
    """Domain Search → candidates already carrying email/status/LinkedIn.

    Prefers the company domain; falls back to the company name (Hunter resolves it),
    which is what we need for ATS-hosted jobs where no employer domain is derivable.
    """
    if not _api_key() or not (domain or company):
        return []
    # Free plan caps results at 10; requesting more returns a pagination_error.
    params: dict = {
        "api_key": _api_key(),
        "limit": min(max(per_page, 1), 10),
        "type": "personal",
    }
    if domain:
        params["domain"] = domain
    else:
        params["company"] = company

    try:
        r = httpx.get(f"{BASE_URL}/domain-search", params=params, timeout=_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        log.warning("Hunter domain-search failed: %s", e)
        return []
    if r.status_code != 200:
        log.warning("Hunter domain-search HTTP %s: %s", r.status_code, r.text[:200])
        return []

    body = r.json()
    if body.get("errors"):
        log.warning("Hunter domain-search error: %s", body["errors"])
        return []
    data = body.get("data", {})
    org = data.get("organization") or company
    out: list[dict] = []
    for e in data.get("emails", []) or []:
        email = e.get("value")
        if not email:
            continue
        name = " ".join(x for x in (e.get("first_name"), e.get("last_name")) if x).strip()
        out.append({
            "key": email,                       # stable per-person key for enrich mapping
            "full_name": name or email,
            "title": e.get("position") or e.get("position_raw"),
            "seniority": e.get("seniority"),
            "location": None,
            "company": org,
            # contact fields already resolved by the search (no enrich step needed)
            "email": email,
            "email_status": _map_status(e.get("verification")),
            "linkedin_url": _clean_linkedin(e.get("linkedin")),
        })
    log.info("Hunter: %d people for %s", len(out), org)
    return out


def enrich(selected: list[dict]) -> dict[str, dict]:
    """No-op enrich: Domain Search already returned emails. Read them back by key."""
    result: dict[str, dict] = {}
    for c in selected:
        key = c.get("key")
        if key:
            result[key] = {
                "email": c.get("email"),
                "email_status": c.get("email_status", "none"),
                "linkedin_url": c.get("linkedin_url"),
            }
    return result

"""Apollo.io client — people search + bulk enrichment.

Endpoints corrected against Apollo's current API (base includes /api):
  - People search:  POST https://api.apollo.io/api/v1/mixed_people/api_search   (masked; cheap)
  - Bulk enrich:    POST https://api.apollo.io/api/v1/people/bulk_match          (reveals email + LinkedIn; credits)
  - Auth probe:     GET  https://api.apollo.io/api/v1/auth/health

Requires a PAID plan + a MASTER API key (free tier has no API access). Phone is NOT
fetched — Apollo delivers it asynchronously to a public webhook a local tool can't receive.

All calls fail soft: on any error they log and return empty/None so the caller degrades
gracefully rather than crashing the pipeline.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.apollo.io/api/v1"
_TIMEOUT = 30


def _api_key() -> str:
    return os.environ.get("APOLLO_API_KEY", "")


def _headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "X-Api-Key": _api_key(),
    }


def _log_credits(resp: httpx.Response) -> None:
    for h in ("x-24hour-usage", "x-minute-usage", "x-rate-limit-remaining"):
        if h in resp.headers:
            log.debug("Apollo %s = %s", h, resp.headers[h])


def probe() -> tuple[bool, str]:
    """Honest access check. Verifies the key can actually run a people search.

    auth/health passes even on the free plan (which has NO API search access), so we
    additionally issue a minimal, credit-free api_search and detect the free-plan 403.
    """
    if not _api_key():
        return False, "APOLLO_API_KEY not set"
    try:
        health = httpx.get(f"{BASE_URL}/auth/health", headers=_headers(), timeout=_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        return False, f"Apollo unreachable: {e}"
    if health.status_code == 401:
        return False, "Apollo key invalid (401)"

    # The real gate: can this key run a people search? (search consumes no credits)
    try:
        s = httpx.post(f"{BASE_URL}/mixed_people/api_search", headers=_headers(),
                       json={"per_page": 1, "page": 1}, timeout=_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        return False, f"Apollo search unreachable: {e}"
    if s.status_code == 200:
        return True, "Apollo people search available"
    if s.status_code == 403:
        return False, ("Apollo key works but the PLAN has no API access — people search "
                       "requires a PAID plan (upgrade at app.apollo.io)")
    if s.status_code == 401:
        return False, "Apollo key invalid / not a master key (401)"
    return False, f"Apollo search HTTP {s.status_code}: {s.text[:120]}"


def _map_email_status(apollo_status: str | None, email: str | None) -> str:
    """Apollo email status → internal {verified|unverified|none}."""
    if not email:
        return "none"
    if (apollo_status or "").lower() == "verified":
        return "verified"
    return "unverified"


def search_people(
    *,
    domains: list[str] | None = None,
    organization_ids: list[str] | None = None,
    titles: list[str] | None = None,
    seniorities: list[str] | None = None,
    keywords: str | None = None,
    per_page: int = 25,
    page: int = 1,
) -> list[dict]:
    """People search (masked — no email/LinkedIn in this response). Returns candidates."""
    if not _api_key():
        return []
    payload: dict = {"page": page, "per_page": per_page}
    if domains:
        payload["q_organization_domains_list"] = domains
    if organization_ids:
        payload["organization_ids"] = organization_ids
    if titles:
        payload["person_titles"] = titles
    if seniorities:
        payload["person_seniorities"] = seniorities
    if keywords:
        payload["q_keywords"] = keywords

    try:
        resp = httpx.post(
            f"{BASE_URL}/mixed_people/api_search", headers=_headers(), json=payload, timeout=_TIMEOUT
        )
        _log_credits(resp)
        if resp.status_code != 200:
            log.warning("Apollo people search HTTP %s: %s", resp.status_code, resp.text[:200])
            return []
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        log.warning("Apollo people search failed: %s", e)
        return []

    out = []
    for p in (data.get("people") or []):
        out.append({
            "apollo_id": p.get("id"),
            "full_name": p.get("name") or " ".join(
                x for x in (p.get("first_name"), p.get("last_name")) if x),
            "title": p.get("title"),
            "seniority": p.get("seniority"),
            "location": p.get("city") or p.get("state") or p.get("country"),
            "company": (p.get("organization") or {}).get("name"),
        })
    return out


def company_search(name: str, per_page: int = 5) -> list[str]:
    """Resolve a company name to Apollo organization_ids[] (no per-record credit).

    Used when we don't have the employer domain, to give people-search a precise org
    filter instead of a weak keyword match.
    """
    if not name or not _api_key():
        return []
    try:
        resp = httpx.post(
            f"{BASE_URL}/mixed_companies/search",
            headers=_headers(),
            json={"q_organization_name": name, "page": 1, "per_page": per_page},
            timeout=_TIMEOUT,
        )
        _log_credits(resp)
        if resp.status_code != 200:
            log.warning("Apollo company search HTTP %s: %s", resp.status_code, resp.text[:200])
            return []
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        log.warning("Apollo company search failed: %s", e)
        return []
    orgs = data.get("organizations") or data.get("accounts") or []
    return [o.get("id") for o in orgs if o.get("id")][:per_page]


def bulk_enrich(apollo_ids: list[str], *, reveal_personal_emails: bool = True) -> dict[str, dict]:
    """Reveal email + linkedin_url for the given ids. Consumes credits. id -> {email,...}."""
    ids = [i for i in apollo_ids if i]
    if not ids or not _api_key():
        return {}
    payload = {
        "details": [{"id": i} for i in ids],
        "reveal_personal_emails": reveal_personal_emails,
    }
    try:
        resp = httpx.post(
            f"{BASE_URL}/people/bulk_match", headers=_headers(), json=payload, timeout=_TIMEOUT
        )
        _log_credits(resp)
        if resp.status_code != 200:
            log.warning("Apollo bulk_match HTTP %s: %s", resp.status_code, resp.text[:200])
            return {}
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        log.warning("Apollo bulk_match failed: %s", e)
        return {}

    result: dict[str, dict] = {}
    for m in (data.get("matches") or []):
        if not m:
            continue
        email = m.get("email")
        result[m.get("id")] = {
            "email": email,
            "email_status": _map_email_status(m.get("email_status"), email),
            "linkedin_url": m.get("linkedin_url"),
        }
    return result

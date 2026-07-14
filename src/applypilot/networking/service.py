"""Networking orchestrator: job → contacts.

find_contacts_for_job derives the employer/domain, searches Apollo (masked), ranks,
reveals contact info for the selected few, and persists them. LinkedIn fallback (NET-5)
is a no-op here (use_linkedin is accepted but not yet wired).
"""

from __future__ import annotations

import logging

from applypilot.networking import apollo, derive, rank, store

log = logging.getLogger(__name__)


def find_contacts_for_job(
    job: dict,
    per_job: int = 5,
    use_linkedin: bool = False,
    dry_run: bool = False,
) -> dict:
    """Find + persist up to `per_job` contacts for a job.

    Args:
        job: job row dict (needs url; ideally title, company, application_url, full_description).
        per_job: how many contacts to find/reveal.
        use_linkedin: reserved for NET-5 (fallback); no-op in NET-1.
        dry_run: search + rank only — no reveal (no Apollo credits), no persistence of email.

    Returns:
        {"company": str|None, "found": int, "revealed": int, "contacts": [dict], "note": str}
    """
    job_url = job.get("url")
    role = job.get("title")
    company = derive.derive_company(job)
    domain = derive.derive_domain(job, company)

    result = {"company": company, "found": 0, "revealed": 0, "contacts": [], "note": ""}

    if not company and not domain:
        result["note"] = "could not determine employer/domain"
        return result

    titles = rank.role_to_person_titles(role)

    # Precise org filter: prefer the domain; else resolve the company name to org ids
    # (a keyword-only people search returns people matching the word, not employees).
    org_ids = [] if domain else apollo.company_search(company) if company else []
    candidates = apollo.search_people(
        domains=[domain] if domain else None,
        organization_ids=org_ids or None,
        keywords=None if (domain or org_ids) else company,
        titles=titles,
        per_page=25,
    )
    if not candidates:
        result["note"] = "no candidates from Apollo (coverage or plan/key)"
        return result

    selected = rank.select(candidates, role, n=per_job)
    result["found"] = len(selected)

    # Reveal contact info only for the selected few (credit discipline).
    revealed: dict[str, dict] = {}
    if not dry_run:
        revealed = apollo.bulk_enrich([c["apollo_id"] for c in selected if c.get("apollo_id")])
        result["revealed"] = sum(1 for r in revealed.values() if r.get("email"))

    stored_contacts = []
    for c in selected:
        rev = revealed.get(c.get("apollo_id"), {})
        contact = {
            "job_url": job_url,
            "full_name": c.get("full_name"),
            "title": c.get("title"),
            "company": company or c.get("company"),
            "linkedin_url": rev.get("linkedin_url"),
            "email": rev.get("email"),
            "email_status": rev.get("email_status", "none"),
            "location": c.get("location"),
            "seniority": c.get("seniority"),
            "match_reason": c.get("match_reason"),
            "source": "apollo",
            "apollo_id": c.get("apollo_id"),
        }
        if not dry_run:
            store.upsert_contact(contact)
        stored_contacts.append(contact)

    result["contacts"] = stored_contacts
    result["note"] = "dry-run (no reveal)" if dry_run else "ok"
    log.info("Networking: %s → %d contacts (%d with email)%s",
             company, result["found"], result["revealed"], " [dry-run]" if dry_run else "")
    return result

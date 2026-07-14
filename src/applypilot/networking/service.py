"""Networking orchestrator: job → contacts.

find_contacts_for_job derives the employer/domain, searches Apollo (masked), ranks,
reveals contact info for the selected few, and persists them. LinkedIn fallback (NET-5)
is a no-op here (use_linkedin is accepted but not yet wired).
"""

from __future__ import annotations

import logging

from applypilot.networking import apollo, derive, rank, store

log = logging.getLogger(__name__)


def _draft_and_store(profile: dict, job: dict, contact: dict) -> None:
    """Best-effort outreach draft for one contact; failures are non-fatal."""
    from applypilot.networking import outreach
    try:
        draft = outreach.draft_email(profile, job, contact)
        store.upsert_contact({
            "id": contact.get("id"),
            "job_url": contact["job_url"],
            "linkedin_url": contact.get("linkedin_url"),
            "full_name": contact.get("full_name"),
            "outreach_subject": draft["subject"],
            "outreach_message": draft["body"],
            "outreach_status": "drafted",
            "outreach_channel": "email",
        })
    except Exception as e:  # noqa: BLE001
        log.debug("Outreach draft failed for %s: %s", contact.get("full_name"), e)


def draft_for_contact(contact_id: str) -> dict | None:
    """Regenerate the outreach draft for a stored contact. Returns the new draft or None."""
    from applypilot.config import load_profile
    from applypilot.database import get_connection
    from applypilot.networking import outreach

    conn = get_connection()
    store.init_contacts(conn)
    row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    if not row:
        return None
    contact = dict(zip(row.keys(), row))
    jrow = conn.execute(
        "SELECT url, title, company, site, full_description FROM jobs WHERE url = ?",
        (contact["job_url"],),
    ).fetchone()
    job = dict(zip(jrow.keys(), jrow)) if jrow else {"title": contact.get("title")}
    try:
        profile = load_profile()
    except Exception:  # noqa: BLE001
        profile = {}
    try:
        draft = outreach.draft_email(profile, job, contact)
    except Exception as e:  # noqa: BLE001
        log.warning("Regenerate draft failed for %s: %s", contact_id, e)
        return None
    store.upsert_contact({
        "id": contact_id, "job_url": contact["job_url"],
        "linkedin_url": contact.get("linkedin_url"), "full_name": contact.get("full_name"),
        "outreach_subject": draft["subject"], "outreach_message": draft["body"],
        "outreach_status": "drafted", "outreach_channel": "email",
    })
    return draft


def find_contacts_for_job(
    job: dict,
    per_job: int = 5,
    use_linkedin: bool = False,
    dry_run: bool = False,
    draft: bool = True,
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

    _profile_cache: dict = {}

    def _profile_for_drafting() -> dict:
        if "p" not in _profile_cache:
            from applypilot.config import load_profile
            try:
                _profile_cache["p"] = load_profile()
            except Exception:  # noqa: BLE001
                _profile_cache["p"] = {}
        return _profile_cache["p"]

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
            cid = store.upsert_contact(contact)
            contact["id"] = cid
            # Draft outreach for contacts that have an email (skip no-address ones).
            if draft and contact.get("email"):
                _draft_and_store(_profile_for_drafting(), job, contact)
        stored_contacts.append(contact)

    result["contacts"] = stored_contacts
    result["note"] = "dry-run (no reveal)" if dry_run else "ok"
    log.info("Networking: %s → %d contacts (%d with email)%s",
             company, result["found"], result["revealed"], " [dry-run]" if dry_run else "")
    return result

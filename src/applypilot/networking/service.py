"""Networking orchestrator: job → contacts.

find_contacts_for_job derives the employer/domain, searches Apollo (masked), ranks,
reveals contact info for the selected few, and persists them. LinkedIn fallback (NET-5)
is a no-op here (use_linkedin is accepted but not yet wired).
"""

from __future__ import annotations

import logging

from applypilot.networking import derive, providers, rank, store

log = logging.getLogger(__name__)


def _draft_and_store(profile: dict, job: dict, contact: dict, warm: bool = False) -> None:
    """Best-effort outreach draft for one contact; failures are non-fatal.

    warm=True → the hot layer (existing connection): warmer email + a DM to a known connection.
    """
    from applypilot.networking import outreach
    try:
        draft = outreach.draft_email(profile, job, contact, warm=warm)
        store.upsert_contact({
            "id": contact.get("id"),
            "job_url": contact["job_url"],
            "linkedin_url": contact.get("linkedin_url"),
            "full_name": contact.get("full_name"),
            "outreach_subject": draft["subject"],
            "outreach_message": draft["body"],
            "linkedin_message": draft.get("linkedin_note", ""),
            "outreach_status": "drafted",
            "outreach_channel": "email",
        })
    except Exception as e:  # noqa: BLE001
        log.debug("Outreach draft failed for %s: %s", contact.get("full_name"), e)


def draft_for_contact(contact_id: str, style: str = "") -> dict | None:
    """Regenerate the outreach draft for a stored contact. Returns the new draft or None.

    `style` is an optional free-text tone directive passed through to outreach.draft_email.
    """
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
        draft = outreach.draft_email(profile, job, contact, style=style)
    except Exception as e:  # noqa: BLE001
        log.warning("Regenerate draft failed for %s: %s", contact_id, e)
        return None
    store.upsert_contact({
        "id": contact_id, "job_url": contact["job_url"],
        "linkedin_url": contact.get("linkedin_url"), "full_name": contact.get("full_name"),
        "outreach_subject": draft["subject"], "outreach_message": draft["body"],
        "linkedin_message": draft.get("linkedin_note", ""),
        "outreach_status": "drafted", "outreach_channel": "email",
    })
    return draft


def _augment_with_linkedin(selected: list[dict], company: str | None,
                           role: str | None, per_job: int, result: dict) -> list[dict]:
    """Fill the gap with LinkedIn-found people (read-only), Apollo-enriched by URL."""
    from applypilot.networking import linkedin_agent
    need = per_job - len(selected)
    people = linkedin_agent.find_people(company or "", role, n=need)
    if not people:
        return selected
    have_urls = {(c.get("linkedin_url") or "").lower() for c in selected}
    added = 0
    for p in people:
        url = (p.get("linkedin_url") or "").lower()
        if not url or url in have_urls:
            continue
        p = dict(p)
        p["key"] = url  # linkedin_url as the dedupe/enrich key
        p["match_reason"] = "same team"
        p["source"] = "linkedin"
        p.setdefault("company", company)
        selected.append(p)
        have_urls.add(url)
        added += 1
        if len(selected) >= per_job:
            break
    if added:
        result["note"] = f"{added} via LinkedIn fallback"
    return selected


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

    from applypilot.networking import connections
    conns_at_company = connections.count_at_company(company)
    result = {"company": company, "found": 0, "revealed": 0, "contacts": [],
              "connections_at_company": conns_at_company, "note": ""}

    if not company and not domain:
        result["note"] = "could not determine employer/domain"
        return result

    titles = rank.role_to_person_titles(role)

    # Query the active contact provider (Apollo).
    candidates = providers.search(company, domain, role, titles, per_page=25)
    if not candidates:
        result["note"] = f"no candidates from {providers.active() or 'provider'} (coverage or plan/key)"
        return result

    selected = rank.select(candidates, role, n=per_job)

    # LinkedIn fallback (opt-in): when the provider under-covers this company, read
    # the company People page and merge the found profiles.
    if use_linkedin and len(selected) < per_job:
        selected = _augment_with_linkedin(selected, company, role, per_job, result)

    result["found"] = len(selected)

    # Reveal contact info for the selected few (Apollo bulk enrichment; consumes credits).
    revealed: dict[str, dict] = {}
    if not dry_run:
        revealed = providers.enrich(selected)
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
        rev = revealed.get(c.get("key"), {})
        contact = {
            "job_url": job_url,
            "full_name": c.get("full_name"),
            "title": c.get("title"),
            "company": company or c.get("company"),
            "linkedin_url": rev.get("linkedin_url") or c.get("linkedin_url"),
            "email": rev.get("email"),
            "email_status": rev.get("email_status", "none"),
            "location": c.get("location"),
            "seniority": c.get("seniority"),
            "match_reason": c.get("match_reason"),
            "source": c.get("source") or providers.active() or "apollo",
            "apollo_id": c.get("apollo_id"),
        }
        if not dry_run:
            cid = store.upsert_contact(contact)
            contact["id"] = cid
            # Draft outreach for anyone reachable — an EMAIL or a LINKEDIN profile. A no-email
            # contact still has a LinkedIn note (Copy note + open LinkedIn); only truly
            # unreachable contacts (no email AND no LinkedIn) are skipped.
            if draft and (contact.get("email") or contact.get("linkedin_url")):
                _draft_and_store(_profile_for_drafting(), job, contact)
        stored_contacts.append(contact)

    # ── HOT layer: your existing 1st-degree connections at this company (the warm approach). ──
    # Cold (Apollo, above) = strangers. Hot = people you already know there. Enrich their email
    # via Apollo (name+company+LinkedIn → email) and draft WARM outreach (reconnect email + a DM).
    if not dry_run:
        try:
            hot = _find_hot_contacts(job, company, selected, per_job=per_job,
                                     profile_fn=_profile_for_drafting, draft=draft)
            stored_contacts = hot + stored_contacts  # warm contacts first
            result["hot"] = len(hot)
        except Exception as e:  # noqa: BLE001
            log.debug("Hot (connections) layer failed: %s", e)

    result["contacts"] = stored_contacts
    result["note"] = "dry-run (no reveal)" if dry_run else "ok"
    log.info("Networking: %s → %d cold + %d hot contacts (%d with email)%s",
             company, result["found"], result.get("hot", 0), result["revealed"],
             " [dry-run]" if dry_run else "")
    return result


def _find_hot_contacts(job: dict, company: str | None, cold_selected: list[dict],
                       per_job: int, profile_fn, draft: bool) -> list[dict]:
    """Surface + enrich + draft outreach for your existing connections at `company`.

    Skips anyone already covered by the cold Apollo layer (dedupe by normalized name). Enriches
    email via Apollo identity match (name/company/LinkedIn), stores as source='connection', and
    drafts WARM outreach (reconnect email + a DM to a known connection).
    """
    from applypilot.networking import apollo, connections
    job_url = job.get("url")
    conns = connections.at_company(company, limit=per_job)
    if not conns:
        return []

    # Don't double-list someone the cold layer already found.
    cold_names = {(c.get("full_name") or "").strip().lower() for c in cold_selected}
    conns = [c for c in conns if (c.get("full_name") or "").strip().lower() not in cold_names][:per_job]
    if not conns:
        return []

    # Enrich emails via Apollo (name + company + LinkedIn URL → verified email). One credit each.
    people = [{"key": c.get("url") or c.get("full_name"), "full_name": c.get("full_name"),
               "company": company or c.get("company"), "linkedin_url": c.get("url")} for c in conns]
    enriched = apollo.match_by_identity(people)

    out = []
    for c, p in zip(conns, people):
        rev = enriched.get(p["key"], {})
        contact = {
            "job_url": job_url,
            "full_name": c.get("full_name"),
            "title": c.get("position"),
            "company": company or c.get("company"),
            "linkedin_url": rev.get("linkedin_url") or c.get("url"),
            "email": rev.get("email"),
            "email_status": rev.get("email_status", "none"),
            "match_reason": "🤝 connection — you already know them",
            "source": "connection",  # marks the HOT layer
            "apollo_id": rev.get("apollo_id"),
        }
        cid = store.upsert_contact(contact)
        contact["id"] = cid
        if draft:  # warm draft even without an email (the DM path works for connections)
            _draft_and_store(profile_fn(), job, contact, warm=True)
        out.append(contact)
    return out

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
import re

from applypilot.networking import apollo, rank

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


def _resolve_targets(company: str | None, domain: str | None) -> tuple[list[str], str]:
    """Apollo org ids + a corroborated domain for this employer. Shared by both searches so a
    mixed search resolves the company ONCE rather than paying for it twice."""
    if domain or not company:
        return [], ""
    orgs, resolved = resolve_orgs(company)
    if not orgs:
        # Apollo's name search does not always surface the real employer (asking for "BetterUp"
        # returns BetterUp Government and Better Up Now, but not BetterUp itself). The caller
        # falls back to keywords rather than finding nobody.
        log.info("Apollo: no organization confidently matches %r — falling back to keywords",
                 company)
    return [o["id"] for o in orgs], resolved


def search_mix(company: str | None, domain: str | None, role: str | None,
               per_page: int = 25) -> dict[str, list[dict]]:
    """Two separate searches — colleagues and recruiters — instead of one blended query.

    A single query cannot produce a mix, because Apollo decides the composition. Measured on a
    real Yahoo job, the blended query returned **25 recruiters and 0 peers**: the bespoke title
    "AI Operations Strategist" matches nobody in Apollo, so the recruiter titles took every
    slot, and the ranking stage was left choosing five recruiters out of five recruiters.

    Peer titles are widened progressively and the FIRST non-empty result wins, so a company that
    really does have someone with the exact title gets them, and one that does not still gets
    colleagues rather than nobody. Same measurement:
    "AI Operations Strategist" 0, "Operations Strategist" 0, "Strategist" 25.
    """
    if active() != "apollo":
        return {"peers": [], "recruiters": []}
    org_ids, resolved_domain = _resolve_targets(company, domain)
    keywords = None if (domain or org_ids) else company
    common = {"domains": [domain] if domain else None,
              "organization_ids": org_ids or None,
              "keywords": keywords, "per_page": per_page}

    peers: list[dict] = []
    tried: list[str] = []
    for title in rank.peer_titles(role):
        tried.append(title)
        found = apollo.search_people(titles=[title], **common)
        # Exclude hiring-side people the peer query happens to match — a "Talent Acquisition
        # Strategist" answers a search for "Strategist", and counting them as colleagues would
        # rebuild the very imbalance this split exists to prevent.
        found = [p for p in found if not rank.is_recruiter(p.get("title"))]
        if found:
            peers = found
            log.info("Apollo: %d colleague(s) via title %r (tried %s)",
                     len(peers), title, " -> ".join(tried))
            break
    if not peers and tried:
        log.info("Apollo: no colleagues for any of %s", " -> ".join(tried))

    recruiters = [p for p in apollo.search_people(titles=rank.RECRUITER_TITLES, **common)
                  if rank.is_recruiter(p.get("title"))]

    for pool in (peers, recruiters):
        for c in pool:
            c["key"] = c.get("apollo_id")
            c["employer_domain"] = domain or resolved_domain
            c["from_domain_search"] = bool(domain)
    return {"peers": peers, "recruiters": recruiters}


def company_known(company: str | None, domain: str | None = None) -> bool:
    """Has the provider ever heard of this employer, under this name?

    Used only to explain a ZERO result, which is the whole point: "Apollo has no company called
    'Ouryahoo'" and "Apollo has nobody matching this role at Yahoo" are different problems with
    different fixes, and the old message described them with one sentence naming neither.

    Measured on the live API: `Ouryahoo` returns nothing at all, `Yahoo` returns six
    organizations. A domain is treated as sufficient on its own — if one was corroborated
    earlier, the employer is certainly known.
    """
    if domain:
        return True
    if not company:
        return False
    try:
        return bool(apollo.company_lookup(company, per_page=3))
    except Exception:  # noqa: BLE001 — an explanation must never raise into the search path
        return True  # cannot tell; do not claim the company is unknown


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
    # Whether we asked Apollo "who works at this DOMAIN?" rather than searching by name. That
    # distinction is evidence downstream: a person Apollo returns for the employer's own domain
    # is corroborated by Apollo itself, so a differing org NAME is ambiguity rather than proof
    # they work somewhere else (see domain/verification.py — the Avathon Government case).
    by_domain = bool(domain)
    cands = apollo.search_people(
        domains=[domain] if domain else None,
        organization_ids=org_ids or None,
        keywords=None if (domain or org_ids) else company,
        titles=titles,
        per_page=per_page,
    )
    # A title filter that matches nobody is worse than no filter when we already KNOW the
    # company. At wander.com the role synonyms for "Forward Deployed Engineer" returned 0 while
    # the company had 10 people listed, including the CEO and VP Product — exactly who you want
    # on a 50-person startup. Ranking still decides who is most relevant; this only widens the
    # pool it gets to choose from, and only when the narrow query came back empty.
    if not cands and titles and (domain or org_ids):
        cands = apollo.search_people(
            domains=[domain] if domain else None,
            organization_ids=org_ids or None,
            titles=None,
            per_page=per_page,
        )
        if cands:
            log.info("Apollo: no title match at %s — widened to anyone there (%d)",
                     domain or "the matched org(s)", len(cands))
    for c in cands:
        c["key"] = c.get("apollo_id")
        c["employer_domain"] = domain or resolved_domain
        c["from_domain_search"] = by_domain
    return cands


def enrich(selected: list[dict]) -> dict[str, dict]:
    """key -> {email, email_status, linkedin_url} for the selected candidates."""
    if active() != "apollo":
        return {}
    # apollo keys results by apollo_id, which equals candidate["key"]
    return apollo.bulk_enrich([c.get("apollo_id") for c in selected if c.get("apollo_id")])


#: TLDs tried when recovering an employer domain from its name. Ordered by how often a
#: venture-backed US company actually uses them; the search stops at the first CORROBORATED hit.
_GUESS_TLDS = ("com", "io", "co", "ai", "app")


def confirm_employer_domain(company: str | None, slug: str | None = None) -> str:
    """Recover the employer's domain by guessing it and making Apollo confirm the guess.

    Needed because an ATS-hosted posting carries no employer domain — ats.rippling.com is the
    vendor's — and Apollo's NAME search is fuzzy enough to be useless for a common word. A live
    Wander application found nobody: Apollo lists four companies called Wander
    (wearewander.co.uk, welovewander.com, wander.ch, wandermaps.com) and the real employer,
    wander.com, is not among them. Every candidate came from "Wander AG" and verification
    correctly dropped all 15 — while Apollo held the CEO, President and CMO at wander.com the
    whole time.

    A blind guess would be dangerous (§Lessons: a wrong domain yields real humans at the wrong
    company), so the guess is only accepted when Apollo's own people at that domain report an
    employer NAME matching `company` by whole-word comparison. A wrong guess therefore returns
    "" rather than a plausible lie.

    Returns the confirmed domain, or "" when nothing corroborates.
    """
    if active() != "apollo":
        return ""
    from applypilot.domain.company import companies_match

    name = (company or "").strip()
    if not name:
        return ""
    # Prefer the URL slug — it is the employer's own chosen identifier — then the name itself.
    stems: list[str] = []
    for raw in (slug, name):
        stem = re.sub(r"[^a-z0-9]", "", (raw or "").lower())
        if stem and stem not in stems:
            stems.append(stem)

    for stem in stems:
        for tld in _GUESS_TLDS:
            candidate = f"{stem}.{tld}"
            # No title filter: this asks "who works here at all", which is the only question
            # that can confirm the domain. A narrow title list returns nobody at a small
            # company and would make a correct guess look wrong.
            try:
                people = apollo.search_people(domains=[candidate], per_page=5)
            except Exception as e:  # noqa: BLE001
                log.debug("Domain probe failed for %s: %s", candidate, e)
                continue
            if not people:
                continue
            if any(companies_match(name, p.get("company")) for p in people):
                log.info("Recovered employer domain for %r: %s", name, candidate)
                return candidate
            log.debug("%s hosts %r, not %r — rejected",
                      candidate, (people[0] or {}).get("company"), name)
    return ""

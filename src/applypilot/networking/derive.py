"""Derive the real employer + domain for a job.

The pipeline stores the job-*board* name in `jobs.site` (Indeed/LinkedIn/greenhouse),
not the employer. Apollo people-search needs the actual company (and ideally its
domain). This module recovers both from whatever signal the row carries.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Job-board / ATS hosts whose hostname is NOT the employer.
_BOARD_HOSTS = {
    "indeed", "linkedin", "glassdoor", "ziprecruiter", "google", "greenhouse",
    "lever", "ashbyhq", "workday", "myworkdayjobs", "jobs", "boards", "job-boards",
    "smartrecruiters", "bamboohr", "icims", "taleo", "workable", "breezy", "rippling",
}

# site values that are clearly job boards (not employers)
_BOARD_SITES = {
    "indeed", "linkedin", "glassdoor", "zip_recruiter", "ziprecruiter", "google",
    "uploaded",
}

# Leading subdomain labels on an employer's own careers portal (careers.amd.com -> amd.com).
_CAREERS_SUBDOMAINS = {
    "careers", "career", "jobs", "job", "apply", "applying", "recruiting", "recruit",
    "talent", "work", "hire", "hiring", "join", "people", "eu", "us", "www2",
}


def _clean_company(name: str | None) -> str | None:
    if not name:
        return None
    n = name.strip()
    if not n or n.lower() in ("nan", "none", "n/a"):
        return None
    # strip trailing "uploaded job" artifacts from dashboard imports
    n = re.sub(r"\s+uploaded\s+job$", "", n, flags=re.IGNORECASE).strip()
    return n or None


def _from_json_ld(full_description: str | None) -> str | None:
    """Look for a JSON-LD JobPosting hiringOrganization name embedded in the text."""
    if not full_description or "hiringOrganization" not in full_description:
        return None
    for m in re.finditer(r'"hiringOrganization"\s*:\s*({.*?})', full_description, re.DOTALL):
        try:
            org = json.loads(m.group(1))
            name = _clean_company(org.get("name"))
            if name:
                return name
        except (json.JSONDecodeError, AttributeError):
            continue
    return None


def _host_label(url: str | None) -> str | None:
    """Return the registrable-ish label from a careers hostname, if it's an employer host."""
    if not url:
        return None
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return None
    host = host.lower().lstrip("www.")
    if not host:
        return None
    parts = host.split(".")
    # e.g. careers.affirm.com -> affirm ; jobs.lever.co/acme -> lever (board, rejected)
    labels = [p for p in parts if p not in ("com", "io", "co", "net", "org", "ai", "app")]
    if not labels:
        return None
    label = labels[-1] if labels[-1] not in _BOARD_HOSTS else (labels[0] if labels else None)
    if not label or label in _BOARD_HOSTS:
        return None
    return label


def derive_company(job: dict) -> str | None:
    """Best-effort employer name. JSON-LD > stored company > careers hostname > site."""
    # 1. explicit stored company (jobspy now persists it) if it's not a board name
    stored = _clean_company(job.get("company"))
    if stored and stored.lower() not in _BOARD_SITES:
        return stored

    # 2. JSON-LD hiringOrganization from the enriched description
    jl = _from_json_ld(job.get("full_description"))
    if jl:
        return jl

    # 3. careers hostname from application_url (skip known board hosts)
    host_label = _host_label(job.get("application_url")) or _host_label(job.get("url"))
    if host_label:
        return host_label.capitalize()

    # 4. fall back to site only if it's not a generic board
    site = _clean_company(job.get("site"))
    if site and site.lower() not in _BOARD_SITES:
        return site

    return stored or None


def derive_domain(job: dict, company: str | None = None) -> str | None:
    """Best-effort employer domain for Apollo's q_organization_domains_list[]."""
    # Prefer an employer careers hostname that is not a board/ATS host.
    for key in ("application_url", "url"):
        url = job.get(key)
        if not url:
            continue
        try:
            host = (urlparse(url).hostname or "").lower().lstrip("www.")
        except ValueError:
            continue
        if not host:
            continue
        # reject board/ATS hosts (their domain is not the employer's)
        if any(b in host for b in _BOARD_HOSTS):
            continue
        return _employer_domain(host)
    return None


def _employer_domain(host: str) -> str:
    """Strip a leading careers-portal subdomain: careers.amd.com -> amd.com."""
    parts = host.split(".")
    while len(parts) > 2 and parts[0] in _CAREERS_SUBDOMAINS:
        parts = parts[1:]
    return ".".join(parts)

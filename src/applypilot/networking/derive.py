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

# Job-board / ATS identities. These are matched as whole hostname LABELS, never as raw
# substrings — `"lever" in "careers.clever.com"` is true and would reject a real employer.
_BOARD_HOSTS = {
    "indeed", "linkedin", "glassdoor", "ziprecruiter", "google", "greenhouse",
    "lever", "ashbyhq", "workday", "myworkdayjobs",
    "smartrecruiters", "bamboohr", "icims", "taleo", "workable", "breezy", "rippling",
    "ycombinator", "workatastartup",
}

# Labels that are never the employer's name but don't make the host a job board either
# (jobs.stripe.com is Stripe's own careers portal, not a board).
_GENERIC_HOST_LABELS = {"jobs", "job", "boards", "job-boards", "careers", "career"}

# site values that are clearly job boards (not employers)
_BOARD_SITES = {
    "indeed", "linkedin", "glassdoor", "zip_recruiter", "ziprecruiter", "google",
    "uploaded", "ycombinator", "y combinator", "workatastartup",
}

# Every name that must not be returned as an employer on its own. _BOARD_SITES alone was not
# enough: it lists discovery SOURCES, while _BOARD_HOSTS lists ATS/board hostnames, and
# 'greenhouse' appeared only in the latter — so a company field reading "Greenhouse" was
# returned as the employer.
_BOARD_NAMES = {n.lower() for n in (_BOARD_SITES | _BOARD_HOSTS)}

# Path segments that mark a company's OWN careers section, as opposed to a board's listing
# pages. www.google.com/about/careers/... is Google hiring; www.indeed.com/viewjob?jk=... is
# Indeed showing someone else's job.
#
# "jobs"/"job" are deliberately NOT here. This set is only ever consulted for a company whose
# name is a board, and on a board's own domain /jobs is their PRODUCT, not their careers page:
# ycombinator.com/jobs is YC's listing index, and treating it as "YC hiring" would search YC's
# own staff for someone else's job. The eval case `no-signal-at-all` pins that.
_OWN_CAREERS_PATH = {"careers", "career", "openings", "opening", "apply",
                     "applications", "hiring", "join", "work-with-us", "join-us"}

# Leading subdomain labels on an employer's own careers portal (careers.amd.com -> amd.com).
_CAREERS_SUBDOMAINS = {
    "careers", "career", "jobs", "job", "apply", "applying", "recruiting", "recruit",
    "talent", "work", "hire", "hiring", "join", "people", "eu", "us", "www2",
}


# Board / ATS URLs that carry the employer as a path slug. Each entry maps a host
# substring to a callable taking the non-empty path segments and returning the slug.
_ATS_PATH_SLUG = {
    "greenhouse.io": lambda p: p[0],
    "ashbyhq.com": lambda p: p[0],
    "lever.co": lambda p: p[0],
    "smartrecruiters.com": lambda p: p[0],
    # myworkdayjobs hosts look like acme.wd1.myworkdayjobs.com/en-US/External — the
    # employer is the first host label, not the path (handled by _host_label).
    "workdayjobs.com": lambda p: p[0].split("_")[0],
    # YC lists OTHER companies' jobs: /companies/hamming-ai/jobs/XTCQPuO-product-engineer
    "ycombinator.com": lambda p: p[1] if p[0] == "companies" and len(p) >= 2 else None,
    "workatastartup.com": lambda p: p[1] if p[0] == "companies" and len(p) >= 2 else None,
}

_SLUG_WORD_OVERRIDES = {"ai": "AI", "ml": "ML", "hr": "HR", "api": "API"}
_SLUG_FULL_OVERRIDES = {"ai": "AI", "xai": "xAI", "openai": "OpenAI"}


def titleize_slug(value: str) -> str:
    """Render a URL slug as a company name. 'hamming-ai' -> 'Hamming AI'.

    A slug that already carries internal capitals (Ashby preserves them, e.g. 'webAI')
    is trusted as-is — .title() would flatten it to 'Webai'.
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    if any(c.isupper() for c in raw[1:]):
        return re.sub(r"[-_]+", " ", raw).strip()
    words = [w for w in re.split(r"[-_\s]+", raw) if w]
    if not words:
        return ""
    full = "".join(words).lower()
    if full in _SLUG_FULL_OVERRIDES:
        return _SLUG_FULL_OVERRIDES[full]
    return " ".join(_SLUG_WORD_OVERRIDES.get(w.lower(), w.title()) for w in words)


def employer_slug_from_url(url: str | None) -> str | None:
    """Employer slug embedded in a job-board / ATS URL path, if the host has one.

    The employer is in the PATH on these hosts (job-boards.greenhouse.io/affirm/...),
    so the hostname alone is useless — this is what keeps a YC listing from being
    attributed to Y Combinator instead of the startup actually hiring.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().removeprefix("www.")
    parts = [p for p in parsed.path.split("/") if p]
    if not host or not parts:
        return None
    for marker, pick in _ATS_PATH_SLUG.items():
        # Anchored at a label boundary, never a bare substring: "lever.co" is inside
        # "c-lever.co-m", which made careers.clever.com look like a Lever board and
        # yield the company "Jobs". Same bug class this module already fixed twice.
        if host == marker or host.endswith("." + marker):
            try:
                return pick(parts) or None
            except (IndexError, KeyError):
                return None
    return None


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


def _norm_name(name: str) -> str:
    """Company name -> comparable host label ("Y Combinator" -> "ycombinator")."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _company_owns_the_posting(company: str, job: dict) -> bool:
    """True when this posting is on the company's OWN careers site, so the board list is wrong.

    Several of the biggest employers are also job boards — Google, LinkedIn, Indeed, Glassdoor.
    Blocking their names outright meant an application to Google resolved to no employer at all
    and contact discovery never ran, missing 17 known connections there.

    All three conditions must hold, and each one is load-bearing:
      * the host's registrable label matches the company name (google == google.com);
      * the path has NO employer slug — ycombinator.com/companies/hamming-ai names a DIFFERENT
        employer, which is exactly the case the board list exists to catch;
      * the path looks like a careers section — google.com/about/careers is Google hiring,
        while indeed.com/viewjob is Indeed showing someone else's posting.
    """
    target = _norm_name(company)
    if not target:
        return False
    for key in ("application_url", "url"):
        url = job.get(key)
        if not url:
            continue
        if employer_slug_from_url(url):
            return False
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower().removeprefix("www.")
        except ValueError:
            continue
        if not host:
            continue
        labels = [p for p in host.split(".")
                  if p not in ("com", "io", "co", "net", "org", "ai", "app")
                  and p not in _GENERIC_HOST_LABELS]
        # The company must be the ONLY meaningful label. A tenant prefix in front of the board
        # means the board is hosting for someone else, and that someone else is the employer:
        #   google.com                          -> ['google']                        Google's own
        #   salesforce.wd12.myworkdayjobs.com   -> ['salesforce','wd12','myworkdayjobs']
        #                                          Workday hosting SALESFORCE
        # Matching any label let "Myworkdayjobs" claim a Salesforce posting, which sent a
        # Salesforce application through tailoring as though the employer were the ATS.
        if [_norm_name(lbl) for lbl in labels] != [target]:
            continue
        segments = {s.lower() for s in (parsed.path or "").split("/") if s}
        if segments & _OWN_CAREERS_PATH:
            return True
    return False


def _is_board_host(host: str) -> bool:
    """True if any dot-separated label of `host` is a known job board / ATS."""
    return any(label in _BOARD_HOSTS for label in (host or "").lower().split("."))


def _host_label(url: str | None) -> str | None:
    """Return the registrable-ish label from a careers hostname, if it's an employer host."""
    if not url:
        return None
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return None
    host = host.lower().removeprefix("www.")
    if not host:
        return None
    parts = host.split(".")
    # e.g. careers.affirm.com -> affirm ; jobs.lever.co/acme -> lever (board, rejected)
    labels = [p for p in parts if p not in ("com", "io", "co", "net", "org", "ai", "app")]
    if not labels:
        return None
    # A label is unusable as a company name if it's a board identity OR a generic
    # careers-portal word ("job-boards.greenhouse.io" must not yield "Job Boards").
    unusable = _BOARD_HOSTS | _GENERIC_HOST_LABELS
    label = labels[-1] if labels[-1] not in unusable else (labels[0] if labels else None)
    if not label or label in unusable:
        return None
    return label


def derive_company(job: dict) -> str | None:
    """Best-effort employer name.

    stored company > JSON-LD > board/ATS path slug > careers hostname > site. A value
    that is really a job board (Ycombinator, Indeed) is never returned — searching a
    board for "people who work there" finds the board's own recruiters, not the employer's.
    """
    # 1. explicit stored company (jobspy now persists it) if it's not a board name.
    #    _BOARD_HOSTS is folded in: 'greenhouse' was in the host list but NOT in _BOARD_SITES,
    #    so a company field reading "Greenhouse" sailed straight through as the employer.
    stored = _clean_company(job.get("company"))
    if stored and (stored.lower() not in _BOARD_NAMES or _company_owns_the_posting(stored, job)):
        return stored

    # 2. JSON-LD hiringOrganization from the enriched description
    jl = _from_json_ld(job.get("full_description"))
    if jl:
        return jl

    # 3. employer slug in a board/ATS URL path (job-boards.greenhouse.io/affirm/...,
    #    ycombinator.com/companies/hamming-ai/...) — the host is the board, not the employer
    for key in ("application_url", "url"):
        slug = employer_slug_from_url(job.get(key))
        if slug:
            name = _clean_company(titleize_slug(slug))
            if name:
                return name

    # 4. careers hostname from application_url (skip known board hosts)
    host_label = _host_label(job.get("application_url")) or _host_label(job.get("url"))
    if host_label:
        return host_label.capitalize()

    # 5. fall back to site only if it's not a generic board
    site = _clean_company(job.get("site"))
    if site and site.lower() not in _BOARD_SITES:
        return site

    # `stored` is only reachable here when it IS a board name — return None instead so the
    # caller reports "could not determine employer" rather than searching the board itself.
    return None


def derive_domain(job: dict, company: str | None = None) -> str | None:
    """Best-effort employer domain for Apollo's q_organization_domains_list[]."""
    # Prefer an employer careers hostname that is not a board/ATS host.
    for key in ("application_url", "url"):
        url = job.get(key)
        if not url:
            continue
        try:
            host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        except ValueError:
            continue
        if not host:
            continue
        # reject board/ATS hosts (their domain is not the employer's). Label-wise, NOT a
        # substring test: "lever" is inside "clever.com" and "jobs" inside "jobsight.com".
        # ...unless the board name IS the employer and this is their own careers site, or
        # Apollo gets no domain and falls back to a fuzzy name search — the thing that put
        # five people from the wrong "Zello" on a Zello job.
        if _is_board_host(host) and not _company_owns_the_posting(company or job.get("company") or "", job):
            continue
        return _employer_domain(host)
    return None


def _employer_domain(host: str) -> str:
    """Strip a leading careers-portal subdomain: careers.amd.com -> amd.com."""
    parts = host.split(".")
    while len(parts) > 2 and parts[0] in _CAREERS_SUBDOMAINS:
        parts = parts[1:]
    return ".".join(parts)

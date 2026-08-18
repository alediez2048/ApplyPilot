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
    # Oracle Recruiting Cloud. `eohh.fa.us2.oraclecloud.com` is Texas Children's Hospital,
    # and the employer appears NOWHERE in that host: Oracle pods are opaque codes, unlike a
    # Workday tenant (`salesforce.wd12`) that at least wraps the real name. Without this the
    # last non-TLD label wins and the employer becomes "Oraclecloud" — the "Ats"/"Hr"/"Edu"
    # shape a fourth time, and the one that did the most damage, because it also became the
    # DOMAIN. Apollo has oraclecloud.com filed under the City of Atlanta (whose own careers
    # portal is Oracle-hosted), so asking "who works at this domain" returned five Atlanta
    # city employees and four of them were emailed. Better to resolve to nothing and say so.
    "oraclecloud",
    # Recruitics is a job-ad DISTRIBUTOR, not an ATS and not an employer. Found by generating a
    # real draft (§Lessons 42, which is the only thing that ever finds these): the email read
    # "I just applied for the Marketing Science Consultant position at Recruitics" and carried a
    # link to **metacareers.com**. The employer is Meta. Fifth in the family after Ats, Hr, Edu,
    # Ouryahoo and Oraclecloud, and the domain went the same way as the Oracle one —
    # `derive_domain` returned `jsv3.recruitics.com`, a guessed "employer domain" that Apollo
    # would resolve to whoever it has filed there (§Lessons 68). Three contacts were stored on
    # that row; none had been emailed, which is the only reason this was free.
    "recruitics",
}

# ATS hosts where NO label is the employer — the tenant is an internal code, not a name.
# Every other vendor here carries the tenant somewhere recoverable: Workday in the first
# label, Greenhouse and Lever in the path. Oracle Fusion carries it nowhere
# (`eohh.fa.us2.oraclecloud.com`), so the only honest answer from the URL alone is "no idea",
# and `_host_label` must not reach for the nearest label-shaped thing instead.
#
# `recruitics` is here as well as in `_BOARD_HOSTS` because blocking the vendor name ALONE
# produced the employer **"Jsv3"** — the pod code, one label to the left, which is precisely how
# the Oracle fix reproduced its own bug before landing. Unwrapping should mean this line is never
# reached; it is here for the wrapper that does not carry a destination.
_OPAQUE_TENANT_HOSTS = {"oraclecloud", "recruitics"}

# Labels that are never the employer's name but don't make the host a job board either
# (jobs.stripe.com is Stripe's own careers portal, not a board).
# Suffix labels that are never part of a company name. `hr` was missing, so acme.breezy.hr
# resolved to the employer "Hr" — the same shape as the "Ats" bug.
#
# `edu` was missing, so careersearch.stanford.edu resolved to the employer "Edu" — the third
# instance of this exact shape after "Ats" and "Hr", and the one that shipped a résumé and a
# cover letter written for a company called Edu. Every entry here is a real suffix; a label
# that is merely GENERIC ("careers", "apply") belongs in _GENERIC_HOST_LABELS instead, because
# those are legal company names and these are not.
_TLD_LABELS = (
    "com", "io", "co", "net", "org", "ai", "app", "hr", "jobs", "dev",
    # non-commercial suffixes: universities, national labs, government
    "edu", "gov", "mil", "int", "ac",
    # country codes seen on careers hosts
    "us", "uk", "ca", "de", "fr", "es", "it", "nl", "se", "no", "dk", "fi", "ch", "at",
    "be", "ie", "pt", "pl", "au", "nz", "jp", "sg", "in", "br", "mx", "eu",
    # newer gTLDs
    "info", "biz", "tech", "cloud", "xyz", "team", "group", "global", "works",
)

# Host labels that describe INFRASTRUCTURE, not an employer. "ats" and "apply" are the
# general fix for ats.<vendor>.com: without them the fallback returns whichever label comes
# first, which is how a Wander posting on ats.rippling.com became the company "Ats" and
# produced a cover letter addressed to nobody.
_GENERIC_HOST_LABELS = {"jobs", "job", "boards", "job-boards", "careers", "career",
                        "ats", "apply", "applications", "recruiting", "hire", "hiring"}

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
    # ats.rippling.com/<employer>/jobs/<uuid> — no rule here meant the employer slug was
    # never read and the host fallback produced "Ats".
    "rippling.com": lambda p: p[0],
    # apply.workable.com/<employer>/j/<id> — same shape.
    "workable.com": lambda p: p[0],
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


#: Words an ATS tenant slug wraps a company name in. `ouryahoo.wd5.myworkdayjobs.com` is Yahoo;
#: `wd1.myworkdaysite.com/.../WellsFargoJobs` is Wells Fargo. Neither trimmed name is a guess we
#: are willing to act on alone — see `refine_company_from_posting`.
#: Separator characters allowed inside a corroborated name. Two covers "Bank of America"; the
#: point is to stop a name being assembled letter-by-letter out of a sentence.
_MAX_NAME_GAPS = 2

_TENANT_PREFIXES = ("our", "the", "my", "join", "work", "life", "team", "careers", "jobs")
_TENANT_SUFFIXES = ("jobs", "careers", "career", "corp", "corporate", "inc", "llc", "global",
                    "external", "hcm", "recruiting", "talent", "hiring", "group", "holdings")


def refine_company_from_posting(company: str | None, full_description: str | None) -> str | None:
    """A better employer name for an ATS tenant slug, CORROBORATED by the posting itself.

    Apollo returned "0 found, 0 with email" for a Yahoo job, which was correct: the employer had
    been read off the Workday tenant `ouryahoo.wd5.myworkdayjobs.com` and stored as **Ouryahoo**,
    a company that does not exist. The tenant slug is chosen by the employer's HR team and
    routinely wraps the real name — `ouryahoo`, `WellsFargoJobs`, `acme-external`.

    Stripping those affixes blind is not acceptable: "OurCrowd" is a real company, and trimming
    it to "Crowd" would send outreach to strangers with more confidence than before. So a variant
    is only accepted when the POSTING'S OWN TEXT contains it — the description for this job says
    "Yahoo" repeatedly. That makes this a corroboration in the same shape as
    `confirm_employer_domain`, not an inference (§Lessons 34).

    Returns the name as the posting actually spells it, so "wellsfargo" comes back "Wells Fargo".
    None when nothing corroborates, which leaves the original name untouched.
    """
    if not company or not full_description:
        return None
    slug = _norm_name(company)
    if not slug:
        return None

    variants: list[str] = []
    for affix in _TENANT_PREFIXES:
        if slug.startswith(affix) and len(slug) > len(affix) + 2:
            variants.append(slug[len(affix):])
    for affix in _TENANT_SUFFIXES:
        if slug.endswith(affix) and len(slug) > len(affix) + 2:
            variants.append(slug[: -len(affix)])
    if not variants:
        return None

    for variant in sorted(set(variants), key=len, reverse=True):
        spelled = _spelling_in_text(variant, full_description)
        if spelled and _norm_name(spelled) != slug:
            return spelled
    return None


#: Path and host labels that are never a company. Structural furniture of an ATS URL: the
#: product's own nouns, locale codes, and Oracle/Workday UI segments. Deliberately SHORT — the
#: corroboration requirement below does most of the work, and a long list is another thing to
#: maintain per vendor, which is the pattern this whole function exists to escape.
_NEVER_A_TENANT = {
    "job", "jobs", "career", "careers", "apply", "application", "search", "company",
    "companies", "about", "detail", "view", "viewjob", "opening", "openings", "position",
    "positions", "vacancy", "vacancies", "role", "roles", "listing", "listings", "redirect",
    "go", "www", "en", "us", "en-us", "hcmui", "sites", "external_career_site", "index",
}

#: How many times a challenger must be named in the posting before it may replace the incumbent.
#: One mention is a passing reference (a partner, a customer, a competitor); the employer of the
#: role a posting describes is named repeatedly, in the "About X" block if nowhere else. Measured
#: on the live corpus: the three real corrections score 3, 7 and 10, and nothing else scores at
#: all — so this threshold separates cleanly rather than being tuned to the edge.
_MIN_CHALLENGER_MENTIONS = 3


def _tenant_slots(job: dict) -> list[str]:
    """The two positions in a URL where an ATS puts its tenant, and no others.

    This is structural rather than statistical, and that is the point. Every vendor in the corpus
    puts the employer in one of exactly two places:

        jobs.jobvite.com/legalzoom/job/oWLtAfwu          first PATH segment
        job-boards.greenhouse.io/affirm/jobs/7778204003  first PATH segment
        q2ebanking.wd5.myworkdayjobs.com/Q2/job/...      first HOST label, and first path segment
        peak6group.wd1.myworkdayjobs.com/apexfintechsolutions/job/...   both

    Everything DEEPER is location, discipline or a requisition id. Allowing those in was the
    first version of this function, and on the live corpus it was one lucky coincidence away
    from renaming the PEAK6 row to "Technology" — the path is
    `/jobs/technology/austin-texas-united-states-of-america/solutions-engineer/JR104975`, and a
    posting for an engineering job says "technology" plenty of times.
    """
    out: list[str] = []
    for key in ("url", "application_url"):
        raw = (job.get(key) or "").strip()
        if not raw or raw.startswith("target:"):
            continue
        try:
            parts = urlparse(raw)
        except ValueError:
            continue
        labels = [x for x in (parts.hostname or "").lower().removeprefix("www.").split(".") if x]
        segments = [s for s in parts.path.split("/") if s]
        for cand in labels[:1] + segments[:1]:
            flat = cand.lower()
            if flat in _NEVER_A_TENANT or len(flat) < 2:
                continue
            # A requisition id or a uuid, not a name.
            if re.search(r"\d{4,}", flat) or re.fullmatch(r"[0-9a-f-]{16,}", flat):
                continue
            out.append(cand)
    return out


def challenge_company_from_path(company: str | None, job: dict) -> str | None:
    """Replace an employer name the posting never mentions with one it names repeatedly.

    The general form of a bug this codebase has now paid for SIX times — Ats, Hr, Edu, Ouryahoo,
    Oraclecloud, Recruitics — plus Jobvite, which is what prompted it. Every previous fix added
    the vendor's name to a blocklist, which only ever works for a vendor somebody has already
    been burned by. This needs no list: it asks whether the name we resolved is the name the
    posting talks about.

        jobs.jobvite.com/legalzoom/...   ->  "Jobvite" appears 0 times, "LegalZoom" appears 7
                                             and the posting opens "About LegalZoom"

    Two conditions, and both are what keep it safe:

    * **The incumbent must be uncorroborated.** A name the posting names is never challenged, so
      this can only ever fire where the current answer is already unsupported. Nine live rows
      resolve to a name that appears zero times and are still CORRECT (Scale AI writes itself
      "Scale AI" against a `scaleai` slug; Texas Children's Hospital and Peak6's parent do not
      introduce themselves at all) — none of them has a corroborated challenger, so none moves.

    * **The challenger must be corroborated, as a whole word, `_MIN_CHALLENGER_MENTIONS` times.**
      Same standard `refine_company_from_posting` uses, and for the same reason (§Lessons 52):
      the posting's own text is the only evidence available that is not another inference. A
      single mention is a partner or a customer.

    Returns the name AS THE POSTING SPELLS IT — "LegalZoom", not "legalzoom" — or None, which
    leaves the incumbent exactly as it was.
    """
    text = job.get("full_description") or ""
    if not text:
        return None
    # A corroborated incumbent is never challenged.
    if company and _spelling_in_text(_norm_name(company), text):
        return None

    best, best_n = None, 0
    for cand in _tenant_slots(job):
        spelled = _spelling_in_text(_norm_name(cand), text)
        if not spelled:
            continue
        n = len(re.findall(rf"(?<![A-Za-z0-9]){re.escape(spelled)}(?![A-Za-z0-9])",
                           text, re.IGNORECASE))
        if n >= _MIN_CHALLENGER_MENTIONS and n > best_n:
            best, best_n = spelled, n
    # No "and this is a different name from the incumbent" check here, deliberately. It was
    # written, and mutation proved it UNREACHABLE: a candidate that normalises to the incumbent's
    # name looks the incumbent up in the same text, so the corroboration guard at the top would
    # already have returned. A guard that cannot fire is worse than no guard, because the next
    # reader trusts it.
    return best


def _spelling_in_text(flat_name: str, text: str) -> str | None:
    """How the posting writes a name whose letters are `flat_name` — as a WHOLE word.

    The boundary is the entire safety mechanism. A plain substring test accepted
    "OurCrowd" -> "Crowd", because the letters of the trimmed variant are of course still inside
    the untrimmed name wherever the posting mentions it. That is §Lessons 1 for the fifth time,
    inside the function written to be careful about it — and here it would have sent outreach to
    strangers at a company called Crowd with more confidence than the original had.

    Requiring that the match is not flanked by other letters fixes it: in "OurCrowd" the "C" is
    preceded by "r", so the variant is refused and the original name survives.

    A match may also contain at most `_MAX_NAME_GAPS` separator characters in total. One space
    gives "Wells Fargo", two give "Bank of America" — and nine would let the letters of a company
    name be assembled out of unrelated words spread across a sentence, which is not a mention.
    """
    body = r"[^A-Za-z0-9]?".join(re.escape(c) for c in flat_name)
    for m in re.finditer(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", text, re.IGNORECASE):
        span = m.group(0)
        if sum(not c.isalnum() for c in span) <= _MAX_NAME_GAPS:
            return span.strip()
    return None


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
                  if p not in _TLD_LABELS
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


def _host_label(url: str | None, job: dict | None = None) -> str | None:
    """The registrable-ish label from a careers hostname, if it is an employer host.

    `job` is only needed to decide whether a BOARD name is acting as the employer here
    (google.com/about/careers is Google hiring; indeed.com/viewjob is not).
    """
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
    labels = [p for p in parts if p not in _TLD_LABELS]
    if not labels:
        return None
    # A label is unusable as a company name if it's a board identity OR a generic
    # careers-portal word ("job-boards.greenhouse.io" must not yield "Job Boards").
    unusable = _BOARD_HOSTS | _GENERIC_HOST_LABELS
    # Falling back to the FIRST label assumes the vendor puts the tenant there, which is true
    # of `salesforce.wd12.myworkdayjobs.com` and `acme.breezy.hr` and false of the opaque-pod
    # vendors below. Adding `oraclecloud` to _BOARD_HOSTS without this reproduced the original
    # bug one label to the left: `eohh.fa.us2.oraclecloud.com` stopped resolving to
    # "Oraclecloud" and started resolving to "Eohh", which is the pod, not the hospital.
    if any(p in _OPAQUE_TENANT_HOSTS for p in parts):
        return None
    label = labels[-1] if labels[-1] not in unusable else (labels[0] if labels else None)
    if not label:
        return None
    if label in unusable:
        # ...unless the board name IS the employer and this is their own careers site.
        # `derive_company` step 1 already applies exactly this rule to a STORED company, and
        # this step did not — so google.com/about/careers resolved to no employer at all and
        # contact discovery never ran, on a role that had already been applied to. §Lessons 20
        # wrote the rule down and one of its two call sites never got it.
        #
        # Only a BOARD name is rescued. A generic portal word ("careers", "ats", "boards") is
        # never a company however the path reads — and it needs no clause of its own here,
        # because the two sets are disjoint (pinned by
        # `test_the_two_label_sets_are_disjoint`) and `_company_owns_the_posting` refuses one
        # anyway. An earlier version spelled that condition out and it was dead code: a
        # mutation deleting it passed the entire suite, and a test written to catch the
        # mutation could not make it fire either.
        if label in _BOARD_HOSTS and _company_owns_the_posting(label, job or {"url": url}):
            return label
        return None
    return label


def unwrap_job_urls(job: dict) -> dict:
    """See through an ad-network redirect to the posting it points at.

    A distributor's URL describes the DISTRIBUTOR. `jsv3.recruitics.com/redirect?rx_url=…` had
    every rule below reading `recruitics`, so the employer resolved to "Recruitics" and the
    domain to `jsv3.recruitics.com` — a guessed "employer domain" that Apollo will happily file
    strangers under (§Lessons 68, where exactly that put five City of Atlanta employees on a
    Texas Children's Hospital job and emailed four of them).

    The destination is already in the URL, percent-encoded, so this needs no network call and no
    new parsing: `domain.joblink` unwraps it for the outreach link and this is the same string.
    One implementation, two consumers — the alternative is §Lessons 49 for the sixth time.

    Returns a job dict; the original is never mutated, because callers pass rows they go on to
    use for other things.
    """
    from applypilot.domain.joblink import clean_link
    out = dict(job or {})
    for key in ("url", "application_url"):
        raw = (out.get(key) or "").strip()
        if not raw or raw.startswith("target:"):
            continue
        cleaned = clean_link(raw)
        # Only when it actually pointed somewhere ELSE. `clean_link` also strips tracking
        # params, and rewriting a URL here for cosmetic reasons would change what
        # `find_by_any_url` matches on a row whose stored value is the dirty one.
        if cleaned and _registrable(cleaned) != _registrable(raw):
            out[key] = cleaned
    return out


def _registrable(url: str) -> str:
    """The host of a URL, lowercased, or "" — enough to say "this points somewhere else"."""
    from urllib.parse import urlsplit
    try:
        return urlsplit(url).netloc.lower()
    except ValueError:
        return ""


def derive_company(job: dict) -> str | None:
    """The employer, fully resolved and corroborated where the posting allows it.

    THE entry point. It used to be only the first half — the URL rules below — while two
    CORRECTION steps lived at one call site each, and that is the shape of the recurring bug
    rather than a detail of it:

        service.py     derive_company + refine_company_from_posting     <- the only complete one
        web_dashboard  derive_company                                   <- writes `company` AT IMPORT
        gmail_send     derive_company                                   <- counts the per-company cap
        eval harness   derive_company                                   <- scored an intermediate

    The import one is the expensive one. It stores the uncorrected name in `jobs.company`, and
    step 1 below then TRUSTS it, so a hostname guess is laundered into a stored fact that
    outranks the posting for the rest of the row's life. That is why every previous fix had to be
    a blocklist entry: by the time anything could see the description, the wrong answer already
    looked like an operator-supplied one.

    Order: stored company > JSON-LD > board/ATS path slug > careers hostname > site, then
    `refine_company_from_posting` (tenant affixes: Ouryahoo -> Yahoo) and
    `challenge_company_from_path` (wrong entity: Jobvite -> LegalZoom). A value that is really a
    job board is never returned — searching a board for "people who work there" finds the board's
    own recruiters, not the employer's.
    """
    # An ad-network wrapper must be seen through BEFORE any rule reads a host, or every one of
    # them describes the distributor.
    return resolve_employer(job)[0]


def resolve_employer(job: dict) -> tuple[str | None, str]:
    """The employer AND how it was arrived at. `derive_company` is this without the second half.

    The provenance is not bookkeeping. It decides two things that were previously decided by
    enumerating vendors:

    * **Whether a name may be corrected at all.** `hiringOrganization` is the employer stating
      its own name in machine-readable form on its own posting — evidence, not inference. Running
      the tenant-slug repair over it trimmed a legal suffix it mistook for an ATS affix and
      turned "Acme Corp" into "Acme". Correcting a guess is the job; correcting a fact is damage.

    * **Whether the URL's host can be the employer's DOMAIN** (`derive_domain`). This is the
      distinction that matters most, and it is exactly the difference between the two
      corrections:

          refined   "Expediagroup" -> "Expedia"      SAME entity, different spelling
                                                     -> expediagroup.com is still Expedia's
          challenged "Jobvite"     -> "LegalZoom"    DIFFERENT entity
                                                     -> jobvite.com belongs to somebody else

      Getting that backwards is §Lessons 68's whole mechanism: a host that is not the employer's
      becomes the "employer domain", Apollo returns the people who really do work there, and
      verification confirms them because they genuinely do.
    """
    job = unwrap_job_urls(job)
    name, source = _derive_company_from_urls(job)
    if not name or source == "json_ld":
        return name, source
    text = job.get("full_description")
    # Both corrections need the posting, so both are no-ops on a row that has not been enriched
    # yet — which is exactly the import case, and why the stored name must not be trusted
    # forever. `challenge` runs LAST because it is the only one that can overrule step 1.
    refined = refine_company_from_posting(name, text)
    if refined:
        name, source = refined, "refined"
    challenged = challenge_company_from_path(name, job)
    if challenged:
        name, source = challenged, "challenged"
    return name, source


def _derive_company_from_urls(job: dict) -> tuple[str | None, str]:
    """The URL-and-stored-field half, with WHERE the answer came from.

    Split out so the corrections above cannot be skipped, and returns its provenance so they can
    be skipped deliberately for the one source that outranks them.
    """
    # 1. explicit stored company (jobspy now persists it) if it's not a board name.
    #    _BOARD_HOSTS is folded in: 'greenhouse' was in the host list but NOT in _BOARD_SITES,
    #    so a company field reading "Greenhouse" sailed straight through as the employer.
    stored = _clean_company(job.get("company"))
    if stored and (stored.lower() not in _BOARD_NAMES or _company_owns_the_posting(stored, job)):
        return stored, "stored"

    # 2. JSON-LD hiringOrganization from the enriched description
    jl = _from_json_ld(job.get("full_description"))
    if jl:
        return jl, "json_ld"

    # 3. employer slug in a board/ATS URL path (job-boards.greenhouse.io/affirm/...,
    #    ycombinator.com/companies/hamming-ai/...) — the host is the board, not the employer
    for key in ("application_url", "url"):
        slug = employer_slug_from_url(job.get(key))
        if slug:
            name = _clean_company(titleize_slug(slug))
            if name:
                return name, "path_slug"

    # 4. careers hostname from application_url (skip known board hosts)
    host_label = (_host_label(job.get("application_url"), job)
                  or _host_label(job.get("url"), job))
    if host_label:
        return host_label.capitalize(), "host_label"

    # 5. fall back to site only if it's not a board or ATS name.
    #    _BOARD_NAMES, not _BOARD_SITES: step 1 rejects a board name arriving in `company` and
    #    this one used the narrower set, so the same name was refused in one field and accepted
    #    in the other. The live Texas Children's row carries site='Oraclecloud' — every rule
    #    above correctly declined to name an employer, and this line handed back the ATS vendor
    #    anyway. §Lessons 49: a rule implemented at one of its call sites is not implemented.
    site = _clean_company(job.get("site"))
    if site and site.lower() not in _BOARD_NAMES:
        return site, "site"

    # `stored` is only reachable here when it IS a board name — return None instead so the
    # caller reports "could not determine employer" rather than searching the board itself.
    return None, ""


#: Suffixes a company bolts onto its own CORPORATE hostname — the one its people have addresses
#: at. `costargroup.com` is CoStar's, `expediagroup.com` is Expedia's. Nothing else may be
#: appended: the remainder must be one of these words exactly, so "arm" does NOT match
#: "armanino" (remainder "anino") — §Lessons 1, in the comparison that decides whose payroll
#: gets emailed.
_HOST_SUFFIXES = {"group", "groupinc", "inc", "corp", "corporation", "co", "holdings",
                  "hq", "global"}

#: Suffixes that make a host the employer's RECRUITING site rather than their mail domain.
#: `schwabjobs.com` really is Charles Schwab's careers site and NOBODY has an address at it —
#: which is the whole distinction. Kept as its own set rather than deleted because the words are
#: still evidence the host belongs to the employer; what they are not is evidence about email.
#: On ambiguity a word belongs HERE, since rejecting only costs a corroboration call.
_CAREERS_HOST_SUFFIXES = {"careers", "career", "jobs", "job", "hiring", "talent", "people"}


def _host_is_the_employers(host: str, company: str) -> bool:
    """Whether this hostname belongs to the employer we resolved.

    The guard that was missing when a LegalZoom search was handed `jobvite.com` and returned four
    Jobvite employees. There WAS a guard — `derive_domain` skipped a host on the board list — and
    two things defeated it: `jobvite` was never on that list (it was fixed by corroboration
    instead), and the newer provenance check keyed on `source == "challenged"`, which stopped
    being true the moment the corrected name was written back to `jobs.company`. **A rule that
    depends on how we arrived at an answer this run is not a rule about the answer.**

    So this asks the stable question instead: is the host the company's name? It needs no list,
    and it cannot be defeated by storing the right answer.

        legalzoom  vs jobvite.com       -> no
        apexfintechsolutions vs peak6.com -> no
        costar     vs costargroup.com   -> yes, "group"
        schwab     vs schwabjobs.com    -> NO, "jobs" is a careers site (see below)
        arm        vs arm.com           -> yes, exactly

    **A careers suffix is not a mail domain, and that distinction cost a live search.** This used
    to accept `<name>jobs` / `<name>careers` too, on the reasoning that such a host is obviously
    the employer's — which is true, and is the wrong question. The single caller is
    `derive_domain`, whose answer goes to Apollo as `q_organization_domains_list[]`: a claim about
    where these people's EMAIL is. Nobody has an address at a careers site.

    What it cost: a Charles Schwab posting on `www.schwabjobs.com` resolved to the employer
    "Schwab" (correctly) and the domain `schwabjobs.com` (not). Apollo maps that host to **Charles
    Schwab India** — a different legal entity — which has no product managers indexed, so the
    titled search returned 0 and the widen-to-the-whole-company fallback returned five people in
    a location `OUTREACH_EXCLUDE_LOCATIONS` correctly drops. Three searches, zero contacts, and a
    log line reading "apollo knows the company but returned nobody for these titles" that was
    true of the company it had found. §Lessons 68's mechanism with the entity one step sideways.

    Rejecting is SAFE and accepting wrongly is not: with no domain Apollo falls back to a name
    search on the employer the posting corroborated, and `confirm_employer_domain` can still
    recover one with Apollo's agreement. With a wrong domain it returns real people who really
    do work somewhere else, and verification confirms them because they genuinely do
    (§Lessons 68). Measured on both live rows before this changed, and it is why the careers set
    is a rejection rather than a second accept list:

        Meta   metacareers.com rejected -> confirm_employer_domain('Meta')   -> 'meta.com'  ✓
        Schwab schwabjobs.com  rejected -> confirm_employer_domain('Schwab') -> ''
                                        -> name search -> 'Charles Schwab' first  ✓

    Meta ends up BETTER than before (meta.com is where its people actually are), which is the
    argument that this is not merely a Schwab special case.
    """
    # The REGISTRABLE label, not the first one. `careers.arm.com` is Arm's — reading "careers"
    # off the front rejected arm.com, stanford.edu and ey.com, every one of them correct and
    # confirmed by the addresses their contacts actually use. `_employer_domain` already strips
    # the hiring-portal prefixes and is the one implementation of that.
    label = _norm_name(_employer_domain(host or "").split(".")[0])
    name = _norm_name(company or "")
    if not label or not name:
        return False
    if label == name:
        return True
    if label.startswith(name):
        remainder = label[len(name):]
        # Spelled out rather than left to fall through the corporate check, so the rejection
        # reads as a decision. A careers host is the employer's and is still not their mail
        # domain; deleting this branch would look identical and mean something else.
        if remainder in _CAREERS_HOST_SUFFIXES:
            return False
        return remainder in _HOST_SUFFIXES
    return False


def derive_domain(job: dict, company: str | None = None) -> str | None:
    """Best-effort employer domain for Apollo's q_organization_domains_list[].

    Derives the company itself when the caller did not. The board-as-employer check below
    needs a company name, and reading it off the raw row gets "Uploaded" — so a Google
    careers URL yielded no domain unless the caller happened to have resolved the employer
    first. A function whose correctness depends on the order its caller does things is a
    function that will be wrong from the second call site.
    """
    # Same reason as `derive_company`, and this is the half that did the damage in §Lessons 68:
    # an un-unwrapped wrapper yields `jsv3.recruitics.com` as the "employer domain".
    job = unwrap_job_urls(job)
    resolved, source = resolve_employer(job)
    company = company or resolved
    # A CHALLENGED name means the posting named a different employer than the URL did, so the
    # URL's host is somebody else's — the ATS vendor's (jobvite.com behind LegalZoom) or a parent
    # brand's (peak6.com behind Apex Fintech Solutions). Returning it here is the precise
    # mechanism of §Lessons 68: Apollo is handed a domain, returns the people who really do work
    # at it, and verification confirms them because they genuinely do.
    #
    # No domain is the SAFE outcome, not a failure — Apollo falls back to a name search, and the
    # name is the one thing the posting corroborated. This deliberately does NOT fire for a
    # `refined` name: "Expediagroup" -> "Expedia" is one company spelled two ways, and
    # expediagroup.com is still Expedia's (the live contacts on that row are all at it).
    if source == "challenged":
        return None
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
        # And the host must actually be the employer's. The board list above only catches
        # vendors somebody has already been burned by — `jobvite` was not on it, and a LegalZoom
        # search was handed `jobvite.com` and stored four Jobvite employees as the contacts.
        if not _host_is_the_employers(host, company or job.get("company") or ""):
            continue
        return _employer_domain(host)
    return None


def _is_careers_label(label: str) -> bool:
    """A leading host label that names a hiring portal rather than the employer.

    The exact-word set missed `careersearch.stanford.edu`, which yielded the employer domain
    `careersearch.stanford.edu` — a host no human has an address at, and one that then gets fed
    to contact verification as though it were Stanford's mail domain (§Lessons 34: a guessed
    domain handed to a check whose docstring calls a mismatch "near-proof").

    Prefix-matching is safe HERE, unlike §Lessons 1, for two reasons: these are generic English
    words rather than entity names, and `_employer_domain` only ever strips leading labels while
    more than two remain — so the registrable domain itself can never be eaten.
    """
    return label in _CAREERS_SUBDOMAINS or label.startswith(("career", "job", "recruit"))


def _employer_domain(host: str) -> str:
    """Strip a leading careers-portal subdomain: careers.amd.com -> amd.com."""
    parts = host.split(".")
    while len(parts) > 2 and _is_careers_label(parts[0]):
        parts = parts[1:]
    return ".".join(parts)

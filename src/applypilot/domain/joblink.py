"""The link to a job POSTING, fit to send to a human.

A cold email that says "the role I applied for" and names nothing is asking the reader to
guess. At a company with forty openings that is a real cost, and it is worst exactly where the
scrape failed: five rows still carry the placeholder title `{company} uploaded job`, and one
live draft went out reading *"I just applied for the Betterup role"*.

So the email should carry a reference. What it must NOT carry is the string in the database.
Measured across the 32 live rows before writing any of this:

    Betterup     url  .../544ff316-...?utm_source=linkedin_promoted
                 appl .../544ff316-.../application          <- the FORM, not the posting
    Google       appl ./apply?jobId=CiUAL2Fck...            <- RELATIVE. not sendable at all
    Stanford     appl ...?utm_medium=jobboard&utm_source=linkedin?utm_medium=search+engine&
                                                            <- malformed, two `?`
    Recruitics   url  jsv3.recruitics.com/redirect?rx_cid=3239&rx_jobId=...&rx_url=https%3A%2F%2F
                      www.metacareers.com%2Fjobs%2F...      <- 434 chars of ad redirect
    Peak6/Expedia appl .../apply?utm_source=linkedin&utm_medium=referral

Two conclusions, both load-bearing:

**`url` is the posting; `application_url` is the FORM.** The name says otherwise, which is why
this is written down. `application_url` exists so the apply agent knows where to type; it is
frequently a Workday `/apply` endpoint, an Ashby `/application`, or — for Google — a relative
path that means nothing outside the page it was scraped from. Emailing somebody who works at
the company a link to their own application form is the wrong artifact.

**Nearly every stored URL is tagged with where WE found it.** `utm_source=linkedin`,
`gh_src=689c81d53us`, `source=LinkedIn`, `__jvsd=LinkedIn`, `utm_source=syn_li`. Those are
aggregator attribution params, and forwarding one to a recruiter tells them which board the
sender came through in a message whose whole point is not reading as a blast. Same family as
the em dash: a tell that a machine assembled the text.

**What this deliberately does NOT do is rewrite paths.** Stripping a trailing `/application` to
recover "the posting" is a guess about a URL space we do not own, and §Lessons 32 is the price
of issuing a link the site cannot serve — four recruiters got a 404 deck link that way. Dropping
a query parameter is provably safe (it is additive metadata) and unwrapping a redirect is
provably safe (the wrapped URL is literally the destination). Path surgery is neither, so a
`/application` link ships as-is: it renders the posting above the form, which is imperfect and
alive, rather than tidy and hypothetical.

Empty string means NO LINK, and every caller must treat it that way. There is no fallback to a
half-usable URL — a broken reference to the job is worse than no reference, because the reader
clicks it.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

#: Query parameters that record where WE found the posting. Removing one cannot change which
#: page loads; that is the whole reason this is a blocklist and not an allowlist. An allowlist
#: would silently drop the job id on every ATS whose scheme nobody has looked at yet — and
#: `gh_jid`, `jobId`, `req`, `pid` ARE the posting on their respective sites.
_TRACKING_PARAMS = {
    "source", "src", "ref", "referrer", "referer", "trk", "trackingid", "jobsite",
    "share_id",            # Visa: `share_id=LinkedIn_corporate_page`
    "gh_src",              # Greenhouse source tag. NOT `gh_jid`, which IS the job.
    "ashby_jid",           # duplicates the uuid already in the Ashby path
    "__jvst", "__jvsd",    # Jobvite
    "recruiter", "jobboardid", "jobpostingid", "applicationsource",
}

#: Whole namespaces, every member of which is attribution. `rx_` is Recruitics', and it survives
#: unwrapping — the destination URL carries `rx_ch`, `rx_id`, `rx_job`, `rx_r`, `rx_vp` of its
#: own, so listing the wrapper's parameters by name cleaned the outside and left the inside.
_TRACKING_PREFIXES = ("utm_", "rx_")

#: Left ALONE on purpose, and asserted rather than described — a comment saying "do not strip
#: these" is not checkable, and the next person to look at a long URL will reach for the
#: blocklist. `gh_jid` IS the posting on a company's own careers page (Avathon, Miro). `group`
#: and `feedId` are unproven either way, and an unproven parameter keeps its place: dropping one
#: wrongly yields a link that does not resolve, which is the failure this module exists to
#: prevent (§Lessons 32), while keeping one merely leaves a URL longer than it needed to be.
KEPT_ON_PURPOSE = frozenset({"gh_jid", "jobid", "job_id", "req", "reqid", "pid", "id",
                             "group", "feedid", "posting_id", "vacancy"})

#: Parameters whose value is the real destination. A wrapper exists to count the click; the
#: page the reader wants is inside it, percent-encoded.
_REDIRECT_PARAMS = ("rx_url", "redirect_url", "redirecturl", "destination", "redirect", "url")

_MAX_UNWRAP = 3


def _is_tracking(name: str) -> bool:
    n = name.strip().lower()
    return n.startswith(_TRACKING_PREFIXES) or n in _TRACKING_PARAMS


def _absolute(url: str) -> bool:
    """An `http(s)://host/...` URL and nothing else.

    Google's `application_url` is `./apply?jobId=…`, which `urlsplit` parses without complaint
    into a relative path. Sending that to somebody resolves against THEIR mail client, which is
    to say nowhere.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _unwrap(url: str, depth: int = 0) -> str:
    """Follow a redirect wrapper to the destination it encodes, without a network call.

    Bounded, and it only ever unwraps to an ABSOLUTE http(s) URL — so a job posting that happens
    to carry `?url=/some/path` is left exactly as it is rather than being replaced by a fragment.
    """
    if depth >= _MAX_UNWRAP:
        return url
    try:
        query = parse_qsl(urlsplit(url).query, keep_blank_values=True)
    except ValueError:
        return url
    for name, value in query:
        if name.strip().lower() in _REDIRECT_PARAMS:
            inner = unquote(value or "").strip()
            if _absolute(inner):
                return _unwrap(inner, depth + 1)
    return url


def clean_link(url: str) -> str:
    """Strip the attribution tags off a posting URL. Returns "" for anything unsendable.

    Rebuilding through `urlsplit`/`urlencode` also repairs the Stanford row, whose stored URL
    contains two `?` — a malformed string that a mail client may or may not linkify, which is
    the worst of both outcomes.
    """
    url = (url or "").strip()
    if not url or not _absolute(url):
        return ""
    url = _unwrap(url)
    if not _absolute(url):                       # a wrapper that unwrapped to junk
        return ""
    parts = urlsplit(url)
    try:
        kept = [(n, v) for n, v in parse_qsl(parts.query, keep_blank_values=True)
                if not _is_tracking(n)]
    except ValueError:
        return ""
    # `fragment` goes too. `#jobDetails` is a scroll position on the page we are already
    # linking, and it is another thing that reads as pasted.
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(kept, doseq=True), ""))


def posting_link(job: dict) -> str:
    """The URL to reference in a message about this job, or "" when there is nothing to send.

    Prefers `url` — see the module docstring; the field named `application_url` is the form.
    Falls back to it only when `url` yields nothing sendable, because a form link is still a
    page about the role and a missing link is nothing at all.
    """
    job = job or {}
    anchor = (job.get("url") or "").strip()
    # A targets row is keyed `target:<space>:<slug>` (SPACE-1a D1). It is a company you pitch,
    # not a posting, and there is no link — the caller should never have asked, but a Space is
    # only as separate as its write paths (§Lessons 70) so this refuses rather than trusts.
    if anchor.startswith("target:"):
        return ""
    return clean_link(anchor) or clean_link(job.get("application_url") or "")

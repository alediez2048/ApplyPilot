"""Job URLs, normalised — for SEEING THROUGH, not for sending.

This began as "put the posting link in every email". That shipped, and the drafts read badly: a
Workday URL runs to 141 characters and inline in an opening sentence it turns a personal note
into something a bot forwarded. Naming the role is what a human does, so that is what outreach
does now — `domain/jobref.py`, and the honest note is that the TITLE turned out to be the hard
part, not the link.

What survives here is the normalisation, and it is load-bearing somewhere else entirely:
`networking/derive.unwrap_job_urls` uses `clean_link` to see through an ad-network redirect
before any rule reads a hostname. Without it `jsv3.recruitics.com/redirect?rx_url=…` makes every
employer rule describe the DISTRIBUTOR — which is how a Meta job resolved to "Recruitics" and
stored three of that vendor's own recruiters as the contacts.

`posting_link` — which chose between `url` and `application_url` — was deleted with the
send-a-link feature rather than left as an unused public function. Its finding is worth keeping
even though its code is not: **`application_url` is the FORM, not the posting, whatever the name
says.** Measured across the 32 live rows:

    Betterup     url  .../544ff316-...?utm_source=linkedin_promoted
                 appl .../544ff316-.../application          <- the FORM, not the posting
    Google       appl ./apply?jobId=CiUAL2Fck...            <- RELATIVE. not sendable at all
    Stanford     appl ...?utm_medium=jobboard&utm_source=linkedin?utm_medium=search+engine&
                                                            <- malformed, two `?`
    Recruitics   url  jsv3.recruitics.com/redirect?rx_cid=3239&rx_jobId=...&rx_url=https%3A%2F%2F
                      www.metacareers.com%2Fjobs%2F...      <- 434 chars of ad redirect
    Peak6/Expedia appl .../apply?utm_source=linkedin&utm_medium=referral

`application_url` exists so the apply agent knows where to type: it is frequently a Workday
`/apply` endpoint, an Ashby `/application`, or — for Google — a relative path meaningless
outside the page it was scraped from. Anything that later wants "the posting" should prefer
`url` and know why.

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

Empty string means "nothing usable here", and every caller must treat it that way. There is no
fallback to a half-usable URL.
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
